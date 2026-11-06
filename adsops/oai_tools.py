"""MCP tools for OpenAI (ChatGPT) Ads accounts: adsops_openai_*.

Every mutation path: campaign key (config) -> committed state file + immutable policy file -> plan
(with plan_id) -> local + optional Bulk validate_only -> commit under the account lock. Activation
(the spend gate) additionally needs policy allow_activate, a matching human approval block in the
committed state file, a fresh openai guard heartbeat, and room under the cap (2x overdelivery).
The guard only ever pauses.

Account `mode` in the config: "disabled" (no key yet: nothing runs, the guard reports "disabled"),
"read_only" (reads, plans, validate-only, and guard pauses; no other writes), "live".
"""
import datetime as dt
import os
import time

from . import config, oai_engine, oai_policy, safety
from .errors import RailViolation, ToolError, scrub
from .platforms import openai_ads

ACCT = {"account": {"type": "string", "description": "openai_ads account key from the config"}}
CAMP = {"campaign": {"type": "string", "description": "openai_ads campaign key from adsops_campaigns"}}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


class OAI:
    def __init__(self, ctx, cfg, client_factory=None, capi_http=None):
        self.ctx, self.cfg = ctx, cfg
        self._clients = {}
        self.client_factory = client_factory or self._default_client
        self.capi_http = capi_http

    def _default_client(self, account):
        if account["key"] not in self._clients:
            loader = openai_ads.key_loader(account.get("credentials"))
            self._clients[account["key"]] = openai_ads.OpenAIAdsClient(loader, account_id=account["ad_account_id"])
        return self._clients[account["key"]]

    def account(self, key):
        for a in self.cfg.get("accounts", []):
            if a.get("key") == key and a.get("platform") == "openai_ads":
                return a
        raise ToolError(f"unknown openai_ads account key: {key}")

    def client(self, account, need="read"):
        mode = account.get("mode", "disabled")
        if mode == "disabled":
            raise RailViolation(f"openai_ads account {account['key']!r} is disabled in the config (mode=disabled); "
                                "create its API key, store it at the configured ref, then set mode=read_only")
        if need == "write" and mode != "live":
            raise RailViolation(f"openai_ads account {account['key']!r} is mode={mode}; writes need mode=live")
        c = self.client_factory(account)
        if not getattr(c, "_adrails_verified", False):      # the key must belong to the configured account
            got = c.ad_account().get("id")
            if got != account["ad_account_id"]:
                raise RailViolation(f"the API key for {account['key']!r} belongs to {got!r}, not {account['ad_account_id']!r}")
            c._adrails_verified = True
        return c

    def open(self, key, need="read"):
        account, camp = config.resolve_campaign(self.cfg, key)
        if account.get("platform") != "openai_ads":
            raise ToolError(f"campaign {key!r} is a {account.get('platform')} campaign: use the adsops_* tools")
        st = oai_engine.load_state(camp["state_file"])
        pol = oai_policy.load(camp["policy_file"])
        if st["campaign_key"] != key or pol["campaign_key"] != key:
            raise RailViolation("state/policy campaign_key must equal the config campaign key")
        if st["ad_account_id"] != account["ad_account_id"] or pol["ad_account_id"] != account["ad_account_id"]:
            raise RailViolation("state/policy ad_account_id must equal the config account's ad_account_id")
        return account, camp, st, pol, self.client(account, need)

    def tree(self, client, st, pol, with_spend=True):
        return openai_ads.live_tree(client, st["campaign"]["id"], st["campaign"]["name"], now=self.ctx.now,
                                    with_spend=with_spend, campaign_key=st["campaign_key"],
                                    account_since=pol["caps"].get("account_spend_since")
                                    if pol["caps"].get("max_account_spend_usd") is not None else None)

    @staticmethod
    def approval_hash(st):
        import hashlib
        import json as _j
        return "approval:" + hashlib.sha256(_j.dumps([st["campaign_key"], st["approval"]], sort_keys=True,
                                                     default=str).encode()).hexdigest()

    def approval_used(self, st):
        h = self.approval_hash(st)
        return any(e.get("event") == "consumed" and e.get("pid") == h for e in self.ctx.ledger()._events())

    def heartbeat(self, account):
        return os.path.expanduser(account.get("guard_heartbeat") or f"~/.adsops/openai-{account['key']}.heartbeat")

    def today(self, tree):
        return openai_ads.local_date(self.ctx.now(), tree["account"].get("timezone") or "UTC")

    def gate(self, account, st, pol, tree, info):
        """Spend gate checks. -> list of blockers ([] = may proceed)."""
        if not info["spend_gate"]:
            return []
        out = []
        def single_use():
            if self.approval_used(st):
                raise RailViolation("activation refused: this approval block was already used once; a new spend "
                                    "decision needs a new approval (e.g. after the guard paused the campaign)")
        for fn in (lambda: oai_policy.check_approval(st, pol, self.today(tree)), single_use,
                   lambda: oai_policy.check_activation_room(st["campaign"]["budget"], pol, tree["spend_today"],
                                                            tree["spend_life"], tree.get("account_spend"))):
            try:
                fn()
            except RailViolation as e:
                out.append(str(e))
        age = safety.heartbeat_age_min(self.heartbeat(account), now=self.ctx.now)
        mx = pol.get("max_guard_age_min")
        if mx is None or age is None or age < -5 or age > mx:
            out.append(f"openai guard heartbeat {'missing' if age is None else f'{age:.0f} min old'} "
                       f"(max {mx} min): schedule `adsops guard-all` before activating")
        return out

    def plan_digest(self, account, key, camp, ops, tree):
        from . import policy as pm
        return pm.plan_id(account["key"], key, ops, pm.file_sha256(os.path.expanduser(camp["policy_file"])),
                          oai_engine.fingerprint(tree))


def _bulk_validate(client, ops, now):
    operations, skipped = oai_engine.bulk_operations(ops)
    if not operations:
        return {"bulk": "skipped (nothing the Bulk API can express)", "skipped": skipped}
    try:
        import hashlib
        import json as _j
        job = client.bulk_validate(operations, "adrails-validate-" + hashlib.sha256(
            _j.dumps([operations, int(now())], sort_keys=True).encode()).hexdigest()[:40])
    except ToolError as e:
        if getattr(e, "status", None) == 404:
            return {"bulk": "unavailable (Bulk API not enabled on this account); local validation only"}
        raise
    for _ in range(10):
        if job.get("status") in ("completed", "partially_failed", "failed"):
            break
        time.sleep(1)
        job = client.bulk_job(job["id"])
    res = client.bulk_results(job["id"]) if job.get("id") else []
    failed = [f"{r.get('operation_id')}: {r.get('error_code')} {r.get('error')}" for r in res
              if r.get("status") not in ("validated",)]
    if job.get("status") not in ("completed", "partially_failed", "failed"):
        return {"bulk": "failed", "bulk_failures": ["validation job did not finish within 10 s"], "skipped": skipped}
    return {"bulk": "validated" if not failed and job.get("status") == "completed" else "failed",
            "bulk_failures": failed, "skipped": skipped}


def register(srv, ctx, cfg, client_factory=None):
    oai = OAI(ctx, cfg, client_factory)
    if not any(a.get("platform") == "openai_ads" for a in cfg.get("accounts", [])):
        return oai

    @srv.tool("adsops_openai_account", "OpenAI Ads: verify the account's API key (GET /ad_account) and show "
              "names, status, currency, timezone and brand/integrity review state. Read-only.", _schema(ACCT, ["account"]))
    def account(args):
        a = oai.account(args["account"])
        acct = oai.client(a).ad_account()
        if acct.get("id") != a["ad_account_id"]:
            raise RailViolation(f"the key belongs to {acct.get('id')!r}, not {a['ad_account_id']!r}")
        keep = ("id", "account_name", "brand_name", "legal_name", "url", "status", "currency_code", "timezone",
                "review", "account_integrity_review")
        return {"account": a["key"], "mode": a.get("mode"), **{k: acct.get(k) for k in keep if k in acct}}

    @srv.tool("adsops_openai_status", "OpenAI Ads: live campaign, ad groups, ads (with review status and serving "
              "issues), spend today/lifetime, the caps and the guard's pause point. Read-only.", _schema(CAMP, ["campaign"]))
    def status(args):
        account, camp, st, pol, client = oai.open(args["campaign"])
        tree = oai.tree(client, st, pol)
        c = tree["campaign"]
        L = oai_engine.norm_campaign(c) if c else None
        budget = L["budget"] if L else st["campaign"]["budget"]
        age = safety.heartbeat_age_min(oai.heartbeat(account), now=ctx.now)
        return {"campaign": args["campaign"], "exists": bool(c), "live": L,
                "serving_issues": (c or {}).get("serving_issues"),
                "ad_groups": [{**oai_engine.norm_ad_group(g), "ads": [oai_engine.norm_ad(x) for x in g["ads"]]}
                              for g in tree["ad_groups"]],
                "spend_today": tree["spend_today"], "spend_life": tree["spend_life"],
                "cap_usd": pol["caps"]["max_cumulative_spend_usd"], "pause_at_usd": oai_policy.pause_at(budget, pol),
                "daily_pause_at_usd": oai_policy.daily_pause_at(budget, pol),
                "guard_heartbeat_age_min": None if age is None else round(age, 1)}

    @srv.tool("adsops_openai_insights", "OpenAI Ads: daily impressions/clicks/spend for the last N days (1-90) "
              "at campaign, ad_group or ad level. Read-only.",
              _schema({**CAMP, "days": {"type": "integer"}, "level": {"type": "string", "enum": ["campaign", "ad_group", "ad"]}},
                      ["campaign"]))
    def insights(args):
        days = args.get("days", 14)
        if not 1 <= days <= 90:
            raise ToolError("days must be 1..90")
        level = args.get("level", "campaign")
        _, _, st, pol, client = oai.open(args["campaign"])
        tree = oai.tree(client, st, pol, with_spend=False)
        if not tree["campaign"]:
            return {"campaign": args["campaign"], "rows": [], "note": "campaign not created yet"}
        t = int(ctx.now())
        start = openai_ads.local_midnight(t, tree["account"].get("timezone") or "UTC") - (days - 1) * 86400
        import json as _j
        rows = client.insights("/campaigns/" + tree["campaign"]["id"], {
            "time_granularity": "daily", "aggregation_level": level, "limit": 2000,
            "fields[]": ["metadata.readable_time", f"{level}.id", f"{level}.impressions", f"{level}.clicks", f"{level}.spend"],
            "time_ranges[]": [_j.dumps({"type": "unix_range", "start": start,
                                          "end": openai_ads.insights_end(t, tree["account"].get("timezone") or "UTC")})]})
        return {"campaign": args["campaign"], "days": days, "level": level, "rows": rows}

    @srv.tool("adsops_openai_geo_lookup", "OpenAI Ads: search targetable locations (countries, regions, markets) "
              "and get their ids for the state file's targeting.location_ids. Read-only.",
              _schema({**ACCT, "q": {"type": "string"}, "limit": {"type": "integer"}}, ["account", "q"]))
    def geo(args):
        lim = args.get("limit", 10)
        if not 1 <= lim <= 100 or not args["q"].strip():
            raise ToolError("q must be non-empty and limit 1..100")
        return oai.client(oai.account(args["account"])).geo_lookup(args["q"].strip(), lim)

    def _plan(args, need):
        account, camp, st, pol, client = oai.open(args["campaign"], need)
        tree = oai.tree(client, st, pol)
        ops, notes, info = oai_engine.plan(st, pol, tree)
        return account, camp, st, pol, client, tree, ops, notes, info

    @srv.tool("adsops_openai_plan", "OpenAI Ads: diff the committed desired-state file against live. No writes. "
              "Shows ops, notes, exposure increases, spend-gate blockers, and a single-use plan_id.", _schema(CAMP, ["campaign"]))
    def plan(args):
        account, camp, st, pol, client, tree, ops, notes, info = _plan(args, "read")
        out = {"campaign": args["campaign"], "ops": ops, "notes": notes, "ops_count": len(ops), **info,
               "spend_gate_blockers": oai.gate(account, st, pol, tree, info),
               "local_validation": oai_engine.validate_ops(ops) or "ok"}
        if ops:
            pid = oai.plan_digest(account, args["campaign"], camp, ops, tree) + "." + __import__("secrets").token_hex(8)
            ent = ctx.ledger().issue(pid, pol["plan_ttl_s"])
            out.update(plan_id=pid, plan_expires=ent["expires"])
        return out

    @srv.tool("adsops_openai_apply", "OpenAI Ads: apply the desired-state file. Validate-only unless commit=true "
              "(needs mode=live, commit_policy.allow_commit, committed state+policy files, the plan_id, and for "
              "activation the spend gate). New resources are always created paused.",
              _schema({**CAMP, "commit": {"type": "boolean"}, "plan_id": {"type": "string"}}, ["campaign"]))
    def apply(args):
        commit = bool(args.get("commit", False))
        key = args["campaign"]
        if not commit:
            account, camp, st, pol, client, tree, ops, notes, info = _plan(args, "read")
            bad = oai_engine.validate_ops(ops)
            val = _bulk_validate(client, ops, ctx.now) if (ops and not bad and account.get("bulk_validate")) else \
                {"bulk": "not enabled for this account (config bulk_validate)"}
            ctx.audit({"tool": "adsops_openai_apply", "campaign": key, "commit": False, "ops": len(ops)})
            return {"campaign": key, "validated": not bad, "local_problems": bad, **val, "committed": False,
                    "notes": notes, "ops_count": len(ops), "spend_gate": info["spend_gate"],
                    "spend_gate_blockers": oai.gate(account, st, pol, tree, info)}
        account, camp = config.resolve_campaign(cfg, key)
        from .tools import _commit_gate

        def gates():   # state AND policy must be committed and unchanged; checked before and after reading
            _commit_gate(ctx, camp)
            _commit_gate(ctx, {"state_file": os.path.expanduser(camp["policy_file"])})
        gates()
        with ctx.lock(account):
            account, camp, st, pol, client, tree, ops, notes, info = _plan(args, "write")
            gates()
            if not ops:
                return {"campaign": key, "committed": False, "ops_count": 0, "notes": notes}
            if pol["require_plan_id"]:
                given = args.get("plan_id")
                if not given:
                    raise RailViolation("plan_id required: run adsops_openai_plan and pass its plan_id")
                if str(given).split(".")[0] != oai.plan_digest(account, key, camp, ops, tree):
                    raise RailViolation("plan_id does not match the current plan (state, policy or live changed): plan again")
            bad = oai_engine.validate_ops(ops)
            if bad:
                raise RailViolation("local validation failed: " + "; ".join(bad))
            if account.get("bulk_validate"):
                val = _bulk_validate(client, ops, ctx.now)
                if val["bulk"] == "failed":
                    raise RailViolation(f"Bulk validate_only rejected the plan: {val.get('bulk_failures')}")
            blockers = oai.gate(account, st, pol, tree, info)        # last check before consuming the plan
            if blockers:
                raise RailViolation("spend gate: " + " | ".join(blockers))
            allowed = {"pause", "create", "update"} | ({"archive"} if pol["allow_archive"] else set())
            if info["spend_gate"]:
                allowed.add("activate")
            elif any(o["op"] == "activate" for o in ops):
                # only reachable when the campaign ends PAUSED: child activations cannot deliver
                if any(o["op"] == "activate" and o["kind"] == "campaign" for o in ops):
                    raise RailViolation("campaign activation must pass the spend gate")
                allowed.add("activate")

            def recheck():      # spend can move between planning and the first activation: read it again
                t2 = oai.tree(client, st, pol)
                oai_policy.check_activation_room(st["campaign"]["budget"], pol, t2["spend_today"], t2["spend_life"],
                                                 t2.get("account_spend"))
            if pol["require_plan_id"]:
                ctx.ledger().consume(args["plan_id"])
            if info["spend_gate"]:
                h = oai.approval_hash(st)
                ctx.ledger().issue(h, 60)
                ctx.ledger().consume(h)           # one approval funds one spend decision
            try:
                res = oai_engine.execute(client, ops, allowed, before_activate=recheck)
            except ToolError as e:
                ctx.audit({"tool": "openai_commit_failed", "campaign": key, "error": scrub(str(e), 600)})
                raise
        ctx.audit({"tool": "adsops_openai_apply", "campaign": key, "commit": True, "notes": notes, "res": res})
        out = {"campaign": key, "committed": True, "ops_count": len(ops), "notes": notes, "res": res}
        if res.get("created_ids"):
            out["next"] = "record these ids in the state file (campaign.id / ad_groups[].id / ads[].id) and commit it"
        return out

    @srv.tool("adsops_openai_guard", "OpenAI Ads spend guard: PAUSE the campaign if lifetime spend reaches the pause "
              "point (cap minus 2x-overdelivery headroom), today's spend is anomalous, or live budgets/bids exceed the "
              "policy. Never activates or raises anything. Validate-only unless commit=true.",
              _schema({**CAMP, "commit": {"type": "boolean"}}, ["campaign"]))
    def guard(args):
        commit, key = bool(args.get("commit", False)), args["campaign"]
        account, camp = config.resolve_campaign(cfg, key)
        if account.get("platform") != "openai_ads":
            raise ToolError(f"campaign {key!r} is not an openai_ads campaign")
        if account.get("mode", "disabled") == "disabled":
            return {"campaign": key, "action": "none", "why": ["account mode=disabled (no key yet)"], "committed": False}
        pol = oai_policy.load(camp["policy_file"])          # caps are required; a broken policy fails closed
        client = oai.client(account, "read")
        st_err, cid, cname = None, None, None
        try:                                                 # the state file is agent-editable: never depend on it
            st = oai_engine.load_state(camp["state_file"])
            cid, cname = st["campaign"]["id"], st["campaign"]["name"]
        except ToolError as e:
            st_err = str(e)
        tree = openai_ads.live_tree(client, cid, cname, now=ctx.now, campaign_key=key,
                                    account_since=pol["caps"].get("account_spend_since")
                                    if pol["caps"].get("max_account_spend_usd") is not None else None)
        ops, why = oai_engine.guard_plan(pol, tree)
        res = {}
        if ops and commit:
            with ctx.lock(account):
                res = oai_engine.execute(client, ops, {"pause"})
        ctx.audit({"tool": "adsops_openai_guard", "campaign": key, "commit": commit, "why": why,
                   "paused": [o["id"] for o in ops] if commit else [], "spend_today": tree["spend_today"],
                   "spend_life": tree["spend_life"], "account_spend": tree["account_spend"], "state_error": st_err})
        if st_err:     # we still guarded every tagged campaign; fail so the heartbeat goes stale
            raise ToolError(f"state file unreadable ({st_err}); guarded the key's tagged campaigns anyway: "
                            f"{'paused ' + str([o['id'] for o in ops]) if ops and commit else 'no pause needed'}")
        return {"campaign": key, "action": "PAUSE" if ops else "none", "why": why, "committed": bool(ops) and commit,
                "spend_today": tree["spend_today"], "spend_life": tree["spend_life"],
                "account_spend": tree["account_spend"], "ops_count": len(ops), "res": res}

    from . import oai_assets
    oai_assets.register(srv, ctx, cfg, oai)
    from . import oai_suite_tools
    oai_suite_tools.register(srv, ctx, cfg, oai)   # growth suite: ad check, drafts, A/B, conversions, audiences
    return oai
