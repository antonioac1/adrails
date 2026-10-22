"""The adsops tool surface: binds config + engine + platform clients onto a protocol.Server.

Rails live here and in engine.py. Every mutation path:
  campaign key (allowlisted in config) -> state file -> engine.plan -> validateOnly -> commit
and commit additionally needs commit_policy.allow_commit and, by default, a clean git-tracked state file.
"""
import datetime as dt
import json
import os
import re
import secrets
import time

from adsops import __version__, auth, config, engine, meter as meter_mod, policy as policy_mod, protocol, safety, statestore
from adsops.errors import RailViolation, ToolError, scrub
from adsops.platforms import google_ads

KEY = {"type": "object", "properties": {"campaign": {"type": "string", "description": "campaign key from adsops_campaigns"}},
       "required": ["campaign"], "additionalProperties": False}


def _schema(extra=None, required=("campaign",)):
    props = dict(KEY["properties"])
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


class Context:
    def __init__(self, cfg, client_factory=None, git_runner=None, now=time.time):
        self.cfg, self.now = cfg, now
        self.git_runner = git_runner
        self._tokens = None
        self.client_factory = client_factory or self._default_client

    def _default_client(self, account):
        if account["platform"] != "google_ads":
            raise ToolError(f"unsupported platform {account['platform']!r}")
        if self._tokens is None:
            cred = self.cfg.get("credentials", {}).get("google_ads")
            if not cred:
                raise ToolError("config has no credentials.google_ads")
            self._tokens = auth.from_config(cred)
        g = self.cfg.get("google_ads", {})
        return google_ads.GoogleAdsClient(account["customer_id"], self._tokens,
                                          api_version=g.get("api_version", "v24"),
                                          login_customer_id=g.get("login_customer_id"),
                                          developer_token=os.environ.get("ADSOPS_GOOGLE_DEVELOPER_TOKEN"))

    def end_field(self):
        return self.cfg.get("google_ads", {}).get("end_field", "end_date_time")

    def open(self, key):
        """-> (account, campaign, raw state, effective state, policy|None, client).

        The policy file, when configured, is the ONLY source of caps and allow_enable; the state file's are ignored."""
        account, camp = config.resolve_campaign(self.cfg, key)
        raw = engine.load_state(camp["state_file"])
        if str(raw["customer_id"]).replace("-", "") != str(account["customer_id"]).replace("-", ""):
            raise RailViolation(f"state file customer_id does not match account {account['key']!r}")
        pol = None
        if camp.get("policy_file"):
            pol = policy_mod.load_policy(self.path(camp["policy_file"]))
        eff = policy_mod.effective_state(raw, pol) if pol else raw
        return account, camp, raw, eff, pol, self.client_factory(account)

    @staticmethod
    def path(p):
        return os.path.expanduser(p)

    def ledger(self):
        return policy_mod.PlanLedger(self.path(self.cfg.get("plan_ledger", "~/.adsops/plans.jsonl")), now=self.now)

    def lock(self, account):
        return safety.AccountLock(self.path(self.cfg.get("lock_dir", "~/.adsops/locks")), account["key"])

    def heartbeat(self):
        return self.path(self.cfg.get("guard_heartbeat", "~/.adsops/guard.heartbeat"))

    def plan_digest(self, account, key, camp, pol, live, ops):
        ph = policy_mod.file_sha256(self.path(camp["policy_file"])) if pol else "legacy"
        return policy_mod.plan_id(account["key"], key, ops, ph, policy_mod.live_fingerprint(live))

    def new_plan_id(self, account, key, camp, pol, live, ops):
        # digest binds the exact plan; the nonce makes every issue single-use on its own (identical plans can recur)
        return self.plan_digest(account, key, camp, pol, live, ops) + "." + secrets.token_hex(8)

    def check_plan_id(self, account, key, camp, pol, live, ops, given):
        """Raise unless `given` names exactly this plan. Returns the plan id (or None when not required)."""
        if not (pol and pol.get("require_plan_id")):
            return None
        if not given:
            raise RailViolation("plan_id required: run adsops_plan (or adsops_set dry_run=true) and pass its plan_id")
        if str(given).split(".")[0] != self.plan_digest(account, key, camp, pol, live, ops):
            raise RailViolation("plan_id does not match the current plan (state, policy or live account changed): plan again")
        return given

    def audit(self, entry):
        path = os.path.expanduser(self.cfg.get("audit_log", "~/.adsops/audit.jsonl"))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        at = dt.datetime.fromtimestamp(self.now(), dt.timezone.utc).isoformat(timespec="seconds")
        with open(path, "a") as fh:
            fh.write(json.dumps({"at": at, "tenant": self.cfg["tenant"], "via": "adsops", **entry}, default=str) + "\n")


def _commit_gate(ctx, camp):
    pol = ctx.cfg.get("commit_policy", {})
    if not pol.get("allow_commit", False):
        raise RailViolation("commit disabled by commit_policy.allow_commit; run without commit to validate")
    if pol.get("require_clean_git", True):
        kw = {"runner": ctx.git_runner} if ctx.git_runner else {}
        ok, why = config.git_clean(camp["state_file"], **kw)
        if not ok:
            raise RailViolation(f"state file must be committed and unchanged in git before commit: {why}")


def _preflight(ctx, eff, pol, live, ops):
    """Rails checked before ANY real mutation (and before adsops_set writes the state file)."""
    safety.require_fresh_guard(ops, live, ctx.heartbeat(), (pol or {}).get("max_guard_age_min"),
                               now=ctx.now, end_field=ctx.end_field())
    safety.require_enable_allowed(ops, live, eff.get("guards"))
    hits = safety.protected_negatives(ops, (pol or {}).get("protected_terms"))
    if hits:
        raise RailViolation(f"refusing negatives that would block protected terms: {', '.join(map(str, hits))}")


def _commit(ctx, account, key, camp, eff, pol, client, live, ops, given_pid):
    """The one path to a real mutation: plan-id check + consume, guard freshness, execute, reconcile on failure."""
    pid = ctx.check_plan_id(account, key, camp, pol, live, ops, given_pid)
    _preflight(ctx, eff, pol, live, ops)
    if pid:
        ctx.ledger().consume(pid)
    try:
        return engine.execute(client, ops, True)
    except ToolError as e:
        try:
            live2 = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
            rec = safety.reconcile(ops, engine.plan(eff, live2, end_field=ctx.end_field())[0])
        except Exception as e2:   # noqa: BLE001 — reconciliation is best-effort; say so
            rec = {"error": f"could not reconcile: {type(e2).__name__}"}
        ctx.audit({"tool": "commit_failed", "campaign": key, "error": scrub(str(e), 300), "reconcile": rec})
        raise ToolError(f"commit failed: {scrub(str(e), 300)}. Reconciled against live, NOT retried: {json.dumps(rec)}") from None


def build_server(cfg, client_factory=None, git_runner=None, now=time.time, meter=None):
    ctx = Context(cfg, client_factory, git_runner, now)
    meter = meter or meter_mod.Meter(os.path.expanduser(cfg.get("meter_log", "~/.adsops/meter.jsonl")), cfg["tenant"])
    srv = protocol.Server("adsops", __version__, on_call=meter_mod.hook(meter))

    @srv.tool("adsops_campaigns", "List the campaigns this server may operate (keys, accounts, state files).",
              {"type": "object", "properties": {}, "additionalProperties": False})
    def campaigns(args):
        return {"campaigns": config.list_campaigns(cfg), "commit_policy": cfg.get("commit_policy", {})}

    @srv.tool("adsops_status", "Live read: status, end date, daily budget, spend today/lifetime, negative count.", KEY)
    def status(args):
        _, camp, raw, eff, pol, client = ctx.open(args["campaign"])
        live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
        out = {k: (len(v) if k == "negatives" else v) for k, v in live.items() if k != "budget_rn"}
        out["caps_from"] = "policy" if pol else "state file (legacy)"
        if pol and "guards" in raw and raw["guards"] != pol["guards"]:
            out["warning"] = "state file carries guards that differ from the policy file; the policy file wins"
        age = safety.heartbeat_age_min(ctx.heartbeat(), now=ctx.now)
        out["guard_heartbeat_age_min"] = None if age is None else round(age, 1)
        return {"campaign": args["campaign"], **out}

    @srv.tool("adsops_terms", "Clicked search terms over the last N days (term, matched keyword, clicks, cost).",
              _schema({"days": {"type": "integer", "description": "1-90, default 14"}}))
    def terms(args):
        days = args.get("days", 14)
        if not 1 <= days <= 90:
            raise ToolError("days must be 1..90")
        _, _, _, eff, _, client = ctx.open(args["campaign"])
        rows = google_ads.search_terms(client, eff["campaign_id"], days)
        return {"campaign": args["campaign"], "days": days, "terms": rows}

    def _issue(account, key, camp, pol, live, ops, out):
        out["exposure_increases"] = safety.exposure_increases(ops, live, ctx.end_field())
        if ops:
            pid = ctx.new_plan_id(account, key, camp, pol, live, ops)
            ent = ctx.ledger().issue(pid, (pol or {}).get("plan_ttl_s", 900))
            out.update(plan_id=pid, plan_expires=ent["expires"])
        return out

    @srv.tool("adsops_plan", "Diff the committed desired-state file against live. No writes. Returns a plan_id "
              "that adsops_apply commit=true must quote (single use, expires).", KEY)
    def plan(args):
        account, camp, _, eff, pol, client = ctx.open(args["campaign"])
        live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
        ops, notes = engine.plan(eff, live, end_field=ctx.end_field())
        return _issue(account, args["campaign"], camp, pol, live, ops,
                      {"campaign": args["campaign"], "ops": ops, "notes": notes, "ops_count": engine.ops_count(ops)})

    @srv.tool("adsops_apply", "Apply the desired-state file. Validate-only unless commit=true (needs a clean, "
              "committed state file and commit_policy.allow_commit).",
              _schema({"commit": {"type": "boolean", "description": "default false = Google validateOnly"},
                       "plan_id": {"type": "string", "description": "from adsops_plan; required for commit when the policy says so"}}))
    def apply(args):
        commit = bool(args.get("commit", False))
        account, camp, _, eff, pol, client = ctx.open(args["campaign"])
        if not commit:
            live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
            ops, notes = engine.plan(eff, live, end_field=ctx.end_field())
            n = engine.ops_count(ops)
            res = engine.execute(client, ops, False) if n else {}
        else:
            _commit_gate(ctx, camp)
            with ctx.lock(account):
                live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
                ops, notes = engine.plan(eff, live, end_field=ctx.end_field())
                n = engine.ops_count(ops)
                res = _commit(ctx, account, args["campaign"], camp, eff, pol, client, live, ops,
                              args.get("plan_id")) if n else {}
        ctx.audit({"tool": "adsops_apply", "campaign": args["campaign"], "commit": commit, "notes": notes,
                   "ops": {k: len(v) for k, v in ops.items()}, "res": res})
        return {"campaign": args["campaign"], "validated": True, "committed": commit and n > 0,
                "notes": notes, "ops_count": n, "res": res}

    @srv.tool("adsops_set", "Change budget / status / end date / negatives. Checked against the state file's guards "
              "and the live account, written to the state file and committed to git with your reason. "
              "apply=true then pushes it live (validateOnly first). dry_run=true previews without writing.",
              _schema({"daily_budget_usd": {"type": "number"},
                       "status": {"type": "string", "enum": ["ENABLED", "PAUSED"]},
                       "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                       "negatives_add": {"type": "array", "items": {"type": "object"}},
                       "reason": {"type": "string", "description": "why — recorded in the commit and audit log"},
                       "apply": {"type": "boolean"}, "dry_run": {"type": "boolean"},
                       "plan_id": {"type": "string", "description": "from a dry_run; required for apply=true when the policy says so"}},
                      required=("campaign", "reason")))
    def set_(args):
        if not cfg.get("commit_policy", {}).get("allow_set", False):
            raise RailViolation("adsops_set disabled by commit_policy.allow_set")
        reason = args["reason"].strip()
        if len(reason) < 8:
            raise ToolError("reason must say why (8+ characters)")
        changes = {k: args[k] for k in statestore.SETTABLE if k in args}
        if not changes:
            raise ToolError("nothing to change")
        key = args["campaign"]
        account, camp, raw, eff, pol, client = ctx.open(key)
        new_raw, summary = statestore.apply_changes(raw, changes)          # the file we write: never gains caps
        if not summary:
            return {"campaign": key, "changed": False, "summary": [], "ops_count": 0}
        new_eff = policy_mod.effective_state(new_raw, pol) if pol else new_raw
        live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
        ops, notes = engine.plan(new_eff, live, end_field=ctx.end_field())  # caps enforced here, before any write
        out = {"campaign": key, "changed": True, "summary": summary, "notes": notes,
               "ops_count": engine.ops_count(ops), "dry_run": bool(args.get("dry_run"))}
        if args.get("dry_run"):
            out["ops"] = ops
            return _issue(account, key, camp, pol, live, ops, out)
        _commit_gate(ctx, camp)        # state file must be clean before we touch it (no foreign edits)
        with ctx.lock(account):
            # re-read everything under the lock: another writer may have committed since we planned
            account, camp, raw, eff, pol, client = ctx.open(key)
            new_raw, summary = statestore.apply_changes(raw, changes)
            new_eff = policy_mod.effective_state(new_raw, pol) if pol else new_raw
            live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
            ops, notes = engine.plan(new_eff, live, end_field=ctx.end_field())
            out.update(summary=summary, notes=notes, ops_count=engine.ops_count(ops))
            if not summary:
                return {**out, "changed": False}
            if args.get("apply") and ops:   # refuse BEFORE writing anything if the commit could not proceed
                ctx.check_plan_id(account, key, camp, pol, live, ops, args.get("plan_id"))
                _preflight(ctx, new_eff, pol, live, ops)
            msg = f"adsops_set {key}: {'; '.join(summary)} — {reason} [via adsops]"
            kw = {"runner": ctx.git_runner} if ctx.git_runner else {}
            sha = statestore.write_and_commit(camp["state_file"], new_raw, msg, **kw)
            out["commit"] = sha if re.fullmatch(r"[0-9a-f]{7,64}", str(sha or "")) else "unknown"
            if args.get("apply") and ops:
                out["res"] = _commit(ctx, account, key, camp, new_eff, pol, client, live, ops, args.get("plan_id"))
                out["applied"] = True
        ctx.audit({"tool": "adsops_set", "campaign": args["campaign"], "summary": summary, "reason": reason,
                   "commit": out["commit"], "applied": bool(out.get("applied")), "res": out.get("res")})
        return out

    @srv.tool("adsops_guard", "Spend guard: PAUSE the campaign if a daily or lifetime cap is crossed. Never enables. "
              "Validate-only unless commit=true (pausing is always permitted).",
              _schema({"commit": {"type": "boolean"}}))
    def guard(args):
        commit = bool(args.get("commit", False))
        account, _, _, eff, _, client = ctx.open(args["campaign"])
        live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
        ops, why = engine.guard_plan(eff, live)      # pausing is never gated by plan ids or guard freshness
        if ops and commit:
            with ctx.lock(account):
                res = engine.execute(client, ops, True)
        else:
            res = engine.execute(client, ops, False) if ops else {}
        ctx.audit({"tool": "adsops_guard", "campaign": args["campaign"], "commit": commit, "why": why,
                   "paused": bool(ops) and commit, "spend_today": live["spend_today"], "spend_life": live["spend_life"]})
        return {"campaign": args["campaign"], "action": "PAUSE" if ops else "none", "why": why,
                "committed": bool(ops) and commit, "ops_count": engine.ops_count(ops), "res": res}

    @srv.tool("adsops_usage", "Per-tool usage counts from the metering ledger (billing-ready, no prices).",
              {"type": "object", "properties": {"since": {"type": "string", "description": "ISO-8601 lower bound"}},
               "additionalProperties": False})
    def usage(args):
        return meter.summary(args.get("since"))

    return srv


# ── selftest ───────────────────────────────────────────────────────────────────────────────────
def selftest():
    import tempfile
    n = 0
    tmp = tempfile.mkdtemp()
    state = {"customer_id": "111-222-3333", "campaign_id": 42, "campaign_name": "c42", "status": "ENABLED",
             "allow_enable": True, "end_date": "2099-12-31", "daily_budget_usd": 10,
             "negatives_add": [{"text": "free", "match_type": "PHRASE"}],
             "guards": {"max_end_date": "2099-12-31", "min_daily_budget_usd": 5, "max_daily_budget_usd": 25,
                        "lifetime_spend_pause_usd": 500, "daily_spend_pause_usd": 25, "max_ops_per_run": 10}}
    sp = os.path.join(tmp, "c42.json")
    json.dump(state, open(sp, "w"))
    cfg_d = {"tenant": "t1", "meter_log": os.path.join(tmp, "m.jsonl"), "audit_log": os.path.join(tmp, "a.jsonl"),
             "plan_ledger": os.path.join(tmp, "plans.jsonl"), "lock_dir": os.path.join(tmp, "locks"),
             "guard_heartbeat": os.path.join(tmp, "guard.hb"),
             "credentials": {"google_ads": {"type": "static", "token": "x"}},
             "commit_policy": {"require_clean_git": True, "allow_commit": True},
             "accounts": [{"key": "acct", "platform": "google_ads", "customer_id": "111-222-3333",
                           "campaigns": [{"key": "c42", "state_file": sp}]}]}
    cp = os.path.join(tmp, "cfg.json")
    json.dump(cfg_d, open(cp, "w"))
    cfg = config.load_config(cp)

    calls = []
    spend = {"life": 100.0, "today": 3.0}

    class FakeClient:
        def search(self, q):
            calls.append(("search", q))
            if "campaign_criterion" in q:
                return [{"campaignCriterion": {"keyword": {"text": "free", "matchType": "PHRASE"}}}]
            if "search_term_view" in q:
                return [{"searchTermView": {"searchTerm": "pm near me"}, "segments": {"keyword": {"info": {"text": "pm"}}},
                         "metrics": {"clicks": "3", "costMicros": "4500000"}}]
            micros = spend["today"] if "TODAY" in q else spend["life"]
            return [{"campaign": {"id": "42", "name": "c42", "status": "ENABLED", "endDateTime": "2099-12-31 23:59:59",
                                  "campaignBudget": "customers/1112223333/campaignBudgets/9"},
                     "campaignBudget": {"amountMicros": "8000000", "explicitlyShared": False},
                     "metrics": {"costMicros": str(int(micros * 1e6))}}]

        def mutate(self, svc, ops, validate_only, partial=False):
            calls.append(("mutate", svc, validate_only))
            return {"results": [{}] * len(ops)}

    git = {"out": ""}

    class R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def runner(cmd, **kw):
        if "ls-files" in cmd:
            return R(0, sp + "\n")
        if "--show-toplevel" in cmd:
            return R(0, tmp + "\n")
        if cmd[1] == "rev-parse":
            return R(0, "f00dcafe\n")
        if cmd[1] in ("add", "commit"):
            git.setdefault("ops", []).append(cmd[1])
            return R(0)
        return R(0, git["out"])

    srv = build_server(cfg, client_factory=lambda a: FakeClient(), git_runner=runner)

    def call(name, args=None):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
        return r["result"]

    names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == ["adsops_campaigns", "adsops_status", "adsops_terms", "adsops_plan", "adsops_apply",
                     "adsops_set", "adsops_guard", "adsops_usage"], names; n += 1
    assert call("adsops_campaigns")["structuredContent"]["campaigns"][0]["key"] == "c42"; n += 1
    assert call("adsops_status", {"campaign": "nope"})["isError"]; n += 1
    st = call("adsops_status", {"campaign": "c42"})["structuredContent"]
    assert st["budget_usd"] == 8 and st["negatives"] == 1 and "budget_rn" not in st and st["spend_today"] == 3; n += 1
    t = call("adsops_terms", {"campaign": "c42", "days": 7})["structuredContent"]
    assert t["terms"][0]["clicks"] == 3 and t["terms"][0]["cost"] == 4.5; n += 1
    assert call("adsops_terms", {"campaign": "c42", "days": 500})["isError"]; n += 1
    p = call("adsops_plan", {"campaign": "c42"})["structuredContent"]
    assert "campaignBudgets" in p["ops"] and p["ops_count"] == 1; n += 1      # 8 -> 10; negative already present

    calls.clear()
    a = call("adsops_apply", {"campaign": "c42"})["structuredContent"]
    assert a["committed"] is False and [c for c in calls if c[0] == "mutate"] == [("mutate", "campaignBudgets", True)]; n += 1

    git["out"] = " M c42.json\n"
    r = call("adsops_apply", {"campaign": "c42", "commit": True})
    assert r["isError"] and "committed and unchanged" in r["content"][0]["text"]; n += 1
    git["out"] = ""
    calls.clear()
    a = call("adsops_apply", {"campaign": "c42", "commit": True})["structuredContent"]
    assert a["committed"] and [c[2] for c in calls if c[0] == "mutate"] == [True, False]; n += 1

    cfg["commit_policy"]["allow_commit"] = False
    assert call("adsops_apply", {"campaign": "c42", "commit": True})["isError"]; n += 1
    cfg["commit_policy"]["allow_commit"] = True
    assert call("adsops_apply", {"campaign": "c42", "bogus": 1})["isError"]; n += 1   # additionalProperties

    # adsops_set
    r = call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "reason": "scale winning terms"})
    assert r["isError"] and "allow_set" in r["content"][0]["text"]; n += 1
    cfg["commit_policy"]["allow_set"] = True
    assert call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "reason": "x"})["isError"]; n += 1
    assert call("adsops_set", {"campaign": "c42", "reason": "nothing here at all"})["isError"]; n += 1
    r = call("adsops_set", {"campaign": "c42", "daily_budget_usd": 30, "reason": "over the guard cap"})
    assert r["isError"] and "outside guard range" in r["content"][0]["text"]; n += 1
    assert json.load(open(sp))["daily_budget_usd"] == 10; n += 1                 # refused change never written
    d = call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "dry_run": True, "reason": "preview raise"})["structuredContent"]
    assert d["dry_run"] and d["ops"]["campaignBudgets"] and json.load(open(sp))["daily_budget_usd"] == 10; n += 1
    assert call("adsops_set", {"campaign": "c42", "guards": {}, "reason": "try to lift caps"})["isError"]; n += 1
    git["out"] = " M c42.json\n"
    assert call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "reason": "foreign edit present"})["isError"]; n += 1
    git["out"] = ""
    calls.clear()
    s1 = call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "status": "PAUSED", "apply": True,
                             "reason": "test raise and pause"})["structuredContent"]
    assert s1["commit"] == "f00dcafe" and s1["applied"] and git["ops"][-2:] == ["add", "commit"]; n += 1
    assert json.load(open(sp))["daily_budget_usd"] == 12 and json.load(open(sp))["status"] == "PAUSED"; n += 1
    assert [c[2] for c in calls if c[0] == "mutate"] == [True, True, False, False]; n += 1   # validate both, then commit both
    assert json.load(open(sp))["guards"] == state["guards"]; n += 1
    nn = call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "reason": "same value again"})["structuredContent"]
    assert nn["changed"] is False; n += 1
    json.dump(state, open(sp, "w"))

    g = call("adsops_guard", {"campaign": "c42"})["structuredContent"]
    assert g["action"] == "none"; n += 1
    spend["today"] = 30.0
    calls.clear()
    g = call("adsops_guard", {"campaign": "c42", "commit": True})["structuredContent"]
    assert g["action"] == "PAUSE" and g["committed"]; n += 1
    muts = [c for c in calls if c[0] == "mutate"]
    assert muts == [("mutate", "campaigns", True), ("mutate", "campaigns", False)]; n += 1

    bad = dict(state, customer_id="999-999-9999")
    json.dump(bad, open(sp, "w"))
    assert call("adsops_status", {"campaign": "c42"})["isError"]; n += 1
    json.dump(state, open(sp, "w"))

    lines = [json.loads(l) for l in open(cfg_d["audit_log"])]
    assert lines and all(l["via"] == "adsops" and l["tenant"] == "t1" for l in lines); n += 1
    assert not any("token" in json.dumps(l) for l in lines); n += 1
    u = call("adsops_usage")["structuredContent"]
    assert u["t1"]["adsops_apply"]["calls"] >= 4 and u["t1"]["adsops_guard"]["ops"] >= 1; n += 1

    # ── Phase 0: policy file, plan ids, guard freshness, reconcile ───────────────────────────
    clock = {"t": 1_800_000_000.0}
    pol = {"customer_id": "111-222-3333", "campaign_id": 42, "allow_enable": True, "require_plan_id": True,
           "max_guard_age_min": 30, "plan_ttl_s": 600,
           "guards": {"max_end_date": "2099-12-31", "min_daily_budget_usd": 5, "max_daily_budget_usd": 20,
                      "lifetime_spend_pause_usd": 500, "daily_spend_pause_usd": 25, "max_ops_per_run": 10}}
    pp = os.path.join(tmp, "c42.policy.json")
    json.dump(pol, open(pp, "w"))
    cfg["accounts"][0]["campaigns"][0]["policy_file"] = pp
    spend["today"] = 3.0
    srv2 = build_server(cfg, client_factory=lambda a: FakeClient(), git_runner=runner, now=lambda: clock["t"])

    def call2(name, args=None):
        return srv2.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}})["result"]
    hb = cfg_d["guard_heartbeat"]
    safety.write_heartbeat(hb, now=lambda: clock["t"])
    os.utime(hb, (clock["t"], clock["t"]))
    st2 = call2("adsops_status", {"campaign": "c42"})["structuredContent"]
    assert st2["caps_from"] == "policy" and "warning" in st2; n += 1          # state guards (25) != policy (20)
    r = call2("adsops_set", {"campaign": "c42", "daily_budget_usd": 22, "dry_run": True, "reason": "over policy cap"})
    assert r["isError"] and "outside guard range" in r["content"][0]["text"]; n += 1   # the POLICY cap (20) governs
    p2 = call2("adsops_plan", {"campaign": "c42"})["structuredContent"]
    assert p2["plan_id"] and p2["exposure_increases"] == ["budget up 8.00 -> 10.00"]; n += 1
    r = call2("adsops_apply", {"campaign": "c42", "commit": True})
    assert r["isError"] and "plan_id required" in r["content"][0]["text"]; n += 1
    r = call2("adsops_apply", {"campaign": "c42", "commit": True, "plan_id": "0" * 64})
    assert r["isError"] and "does not match" in r["content"][0]["text"]; n += 1
    os.utime(hb, (clock["t"] - 3600, clock["t"] - 3600))                       # guard silent for an hour
    calls.clear()
    r = call2("adsops_apply", {"campaign": "c42", "commit": True, "plan_id": p2["plan_id"]})
    assert r["isError"] and "budget up" in r["content"][0]["text"] and not [c for c in calls if c[0] == "mutate"]; n += 1
    os.utime(hb, (clock["t"], clock["t"]))
    a2 = call2("adsops_apply", {"campaign": "c42", "commit": True, "plan_id": p2["plan_id"]})["structuredContent"]
    assert a2["committed"]; n += 1
    r = call2("adsops_apply", {"campaign": "c42", "commit": True, "plan_id": p2["plan_id"]})
    assert r["isError"] and "already used" in r["content"][0]["text"]; n += 1
    p3 = call2("adsops_plan", {"campaign": "c42"})["structuredContent"]
    clock["t"] += 601
    os.utime(hb, (clock["t"], clock["t"]))
    r = call2("adsops_apply", {"campaign": "c42", "commit": True, "plan_id": p3["plan_id"]})
    assert r["isError"] and "expired" in r["content"][0]["text"]; n += 1
    # a pause is never blocked by a dead guard
    os.utime(hb, (clock["t"] - 7200, clock["t"] - 7200))
    spend["today"] = 30.0
    g2 = call2("adsops_guard", {"campaign": "c42", "commit": True})["structuredContent"]
    assert g2["action"] == "PAUSE" and g2["committed"]; n += 1
    spend["today"] = 3.0
    # set never writes caps into the state file; stale guard refuses apply BEFORE writing
    before = open(sp).read()
    d2 = call2("adsops_set", {"campaign": "c42", "daily_budget_usd": 15, "dry_run": True, "reason": "raise within policy"})["structuredContent"]
    r = call2("adsops_set", {"campaign": "c42", "daily_budget_usd": 15, "apply": True, "plan_id": d2["plan_id"],
                             "reason": "raise within policy"})
    assert r["isError"] and open(sp).read() == before; n += 1
    os.utime(hb, (clock["t"], clock["t"]))
    d2 = call2("adsops_set", {"campaign": "c42", "daily_budget_usd": 15, "dry_run": True, "reason": "raise within policy"})["structuredContent"]
    s2 = call2("adsops_set", {"campaign": "c42", "daily_budget_usd": 15, "apply": True, "plan_id": d2["plan_id"],
                              "reason": "raise within policy"})["structuredContent"]
    assert s2["applied"] and json.load(open(sp))["guards"] == state["guards"]; n += 1
    json.dump(state, open(sp, "w"))
    # an ambiguous commit failure is reconciled, never retried
    class FlakyClient(FakeClient):
        def mutate(self, svc, ops, validate_only, partial=False):
            calls.append(("mutate", svc, validate_only))
            if not validate_only:
                raise ToolError("HTTP 503 on mutate")
            return {"results": [{}] * len(ops)}
    srv3 = build_server(cfg, client_factory=lambda a: FlakyClient(), git_runner=runner, now=lambda: clock["t"])
    p4 = srv3.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "adsops_plan", "arguments": {"campaign": "c42"}}})["result"]["structuredContent"]
    calls.clear()
    r = srv3.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "adsops_apply",
                     "arguments": {"campaign": "c42", "commit": True, "plan_id": p4["plan_id"]}}})["result"]
    t = r["content"][0]["text"]
    assert r["isError"] and "NOT retried" in t and "not_landed" in t; n += 1
    assert [c for c in calls if c[0] == "mutate" and not c[2]] == [("mutate", "campaignBudgets", False)]; n += 1
    # same-day re-enable after a daily-cap pause is refused; protected terms can't be negated
    try:
        safety.require_enable_allowed({"campaigns": [{"update": {"status": "ENABLED"}}]},
                                      {"status": "PAUSED", "spend_today": 30.0}, {"daily_spend_pause_usd": 25})
        raise AssertionError("re-enable over daily cap allowed")
    except RailViolation:
        n += 1
    assert safety.require_enable_allowed({"campaigns": [{"update": {"status": "ENABLED"}}]},
                                         {"status": "PAUSED", "spend_today": 3.0}, {"daily_spend_pause_usd": 25}) is None; n += 1
    pol["protected_terms"] = ["property management near me"]
    json.dump(pol, open(pp, "w"))
    os.utime(hb, (clock["t"], clock["t"]))
    d3 = call2("adsops_set", {"campaign": "c42", "negatives_add": [{"text": "near me"}], "dry_run": True,
                              "reason": "try to block a core term"})["structuredContent"]
    r = call2("adsops_set", {"campaign": "c42", "negatives_add": [{"text": "near me"}], "apply": True,
                             "plan_id": d3["plan_id"], "reason": "try to block a core term"})
    assert r["isError"] and "protected terms" in r["content"][0]["text"] and json.load(open(sp)) == state; n += 1
    os.utime(hb, (clock["t"] + 3600, clock["t"] + 3600))                 # heartbeat from the future
    p5 = call2("adsops_plan", {"campaign": "c42"})["structuredContent"]
    r = call2("adsops_apply", {"campaign": "c42", "commit": True, "plan_id": p5["plan_id"]})
    assert r["isError"] and "future" in r["content"][0]["text"]; n += 1
    del cfg["accounts"][0]["campaigns"][0]["policy_file"]
    return n
