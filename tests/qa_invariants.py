#!/usr/bin/env python3
"""Adversarial, property-based QA for the adsops tool surface.

Not a unit test: a *fuzzer* of the rails.  Every seed builds a fresh temporary
deployment (config + desired-state file + policy file + plan ledger + heartbeat +
fake git), a SIMULATED Google Ads account behind a fake client, and then drives
30 random tool calls through ``protocol.Server.handle`` -- the exact surface an
agent talks to.  After every call the nine rails below are re-checked against
what the fake client actually sent to "Google".

The simulated account
---------------------
``SimAccount.search(gaql)`` answers the three GAQL shapes ``google_ads.live_state``
/ ``search_terms`` issue, in the same camelCase row shape the selftest at the
bottom of ``adsops/tools.py`` uses.  ``SimAccount.mutate(service, ops,
validate_only, partial)`` records *every* call and only applies operations to
the simulated campaign when ``validate_only is False`` -- exactly like Google.
It can be told to raise ``ToolError`` on the next commit to exercise the
reconcile-and-never-retry path.

The invariants (checked after every step)
-----------------------------------------
 1  the simulated account's daily budget is never above policy
    ``max_daily_budget_usd`` nor below ``min_daily_budget_usd``            CRITICAL
 2  no committed (validate_only=False) mutate is sent without an identical
    validate_only=True call earlier in the *same* tool call                CRITICAL
 3  status is never committed to ENABLED by ``adsops_guard``               HIGH
 4  ENABLED is never committed while policy ``allow_enable`` is false, or
    while ``spend_today >= daily_spend_pause_usd``                         CRITICAL
 5  an exposure increase (budget up / enable / end-date extension) is never
    committed while the guard heartbeat is stale, in the future or missing CRITICAL
 6  a plan_id is never accepted twice (no duplicate "consumed" ledger event) HIGH
 7  the state file never carries a ``guards`` object different from the
    original, and the policy file bytes never change                      MEDIUM
 8  no committed negative would block a protected term (every word of the
    negative appears in a protected term)                                 CRITICAL
 9  no tool call raises out of ``server.handle``                          MEDIUM

8b is *informational only* (not counted): a committed negative whose words
strictly contain a protected term ("cheap core term").  It cannot block the
protected query itself, so it is not a rail break -- reported for review.

Usage
-----
    cd <repo root>
    python3 tests/qa_invariants.py            # seeds 1..200

Prints ``QA: N sequences, M violations`` plus one line per violation, and
writes ``tests/QA-invariants.md``.
"""
import copy
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adsops import config, safety, tools  # noqa: E402
from adsops.errors import ToolError  # noqa: E402

SEEDS = list(range(1, 201))
STEPS = 30
CAMPAIGN = "c42"
CUSTOMER = "111-222-3333"
CAMPAIGN_ID = 42
PROTECTED = ["core term"]
END_FIELD = "end_date_time"

# policy caps used for every seed (the ONLY source of caps when a policy file exists)
POLICY_GUARDS = {"max_end_date": "2035-12-31", "min_daily_budget_usd": 5,
                 "max_daily_budget_usd": 20, "lifetime_spend_pause_usd": 500,
                 "daily_spend_pause_usd": 25, "max_ops_per_run": 10}
# the state file carries *different* (looser) caps on purpose: the policy must win
STATE_GUARDS = {"max_end_date": "2035-12-31", "min_daily_budget_usd": 5,
                "max_daily_budget_usd": 30, "lifetime_spend_pause_usd": 500,
                "daily_spend_pause_usd": 25, "max_ops_per_run": 10}

BUDGETS = [3.0, 4.0, 4.99, 5.0, 5.5, 8.0, 10.0, 12.0, 15.0, 19.99, 20.0, 20.5, 25.0, 30.0, 50.0]
ENDS = ["2020-01-15", "2030-01-01", "2033-06-30", "2035-12-31", "2036-01-01"]
NEGS = [{"text": "free"},
        {"text": "jobs", "match_type": "EXACT"},
        {"text": "core"},
        {"text": "core term"},
        {"text": "CORE TERM", "match_type": "BROAD"},
        {"text": "term", "match_type": "EXACT"},
        {"text": "cheap core term"},
        {"text": "core term extra"},
        {"text": "hardcore"},
        {"text": "property management"}]
SPENDS_TODAY = [0.0, 1.0, 3.0, 10.0, 24.9, 25.0, 30.0, 60.0]
SPENDS_LIFE = [0.0, 100.0, 400.0, 499.0, 500.0, 650.0]
JUMPS = [0, 0, 0, 1, 30, 120, 300, 601, 3600]

SEVERITY = {1: "CRITICAL", 2: "CRITICAL", 3: "HIGH", 4: "CRITICAL", 5: "CRITICAL",
            6: "HIGH", 7: "MEDIUM", 8: "CRITICAL", 9: "MEDIUM"}

VIOLATIONS = []      # dicts; every one is listed in the report
INFO_8B = []         # informational: negatives that strictly contain a protected term
COUNTER = {"calls": 0}


def _canon(obj):
    return json.dumps(obj, sort_keys=True, default=str)


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class R:
    """subprocess.run stand-in."""

    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


# -- simulated Google Ads account -------------------------------------------------------------
class SimAccount:
    """A tiny in-memory campaign behind the client interface engine.execute expects."""

    def __init__(self):
        self.campaign_id = CAMPAIGN_ID
        self.name = "c42"
        self.customer_id = CUSTOMER.replace("-", "")
        self.budget_micros = 8_000_000          # live budget $8.00
        self.status = "ENABLED"
        self.end = "2035-01-01"
        self.spend_life = 100.0
        self.spend_today = 3.0
        self.negatives = set()                  # {(text.lower(), MATCH)}
        self.records = []                       # every mutate() call, in order
        self.applied = []                       # every change that really landed
        self.fail_next_commit = False           # inject one ambiguous commit failure
        self.call = 0
        self.tool = None

    # harness hooks ---------------------------------------------------------------------
    @property
    def budget_usd(self):
        return self.budget_micros / 1e6

    def begin(self, call_idx, tool):
        self.call, self.tool = call_idx, tool

    def live_snapshot(self):
        """The subset of live_state() that the safety rails reason about."""
        return {"id": str(self.campaign_id), "name": self.name, "status": self.status,
                "end": self.end, "budget_usd": self.budget_usd, "budget_shared": False,
                "budget_rn": "customers/%s/campaignBudgets/9" % self.customer_id,
                "spend_life": self.spend_life, "spend_today": self.spend_today,
                "negatives": sorted(self.negatives)}

    # client surface --------------------------------------------------------------------
    def search(self, gaql):
        q = str(gaql)
        if "campaign_criterion" in q:
            return [{"campaignCriterion": {"keyword": {"text": t, "matchType": m}}}
                    for t, m in sorted(self.negatives)]
        if "search_term_view" in q:
            return [{"searchTermView": {"searchTerm": "core term near me"},
                     "segments": {"keyword": {"info": {"text": "core term"}}},
                     "metrics": {"clicks": "2", "costMicros": "1500000"}}]
        if "TODAY" in q:
            return [self._row(self.spend_today)]
        return [self._row(self.spend_life)]

    def _row(self, cost_usd):
        return {"campaign": {"id": str(self.campaign_id), "name": self.name,
                             "status": self.status,
                             "endDateTime": self.end + " 23:59:59",
                             "campaignBudget": "customers/%s/campaignBudgets/9" % self.customer_id},
                "campaignBudget": {"amountMicros": str(self.budget_micros),
                                   "explicitlyShared": False},
                "metrics": {"costMicros": str(int(round(cost_usd * 1e6)))}}

    def mutate(self, service, ops, validate_only, partial=False):
        ops = list(ops or [])
        rec = {"call": self.call, "tool": self.tool, "service": service,
               "validate_only": bool(validate_only), "partial": bool(partial),
               "ops": copy.deepcopy(ops), "spend_today": self.spend_today,
               "spend_life": self.spend_life, "budget_usd_before": self.budget_usd,
               "status_before": self.status}
        self.records.append(rec)
        if validate_only:
            return {"results": [{} for _ in ops]}
        if self.fail_next_commit:               # ambiguous failure on the commit pass only
            self.fail_next_commit = False
            rec["raised"] = "ToolError"
            raise ToolError("HTTP 503 on mutate (injected by the QA harness)")
        for entry in ops:
            if service == "campaignBudgets":
                upd = entry.get("update") or {}
                self.budget_micros = int(upd.get("amountMicros") or self.budget_micros)
                self.applied.append(("budget", self.budget_micros / 1e6))
            elif service == "campaigns":
                upd = entry.get("update") or {}
                if upd.get("status"):
                    self.status = upd["status"]
                    self.applied.append(("status", self.status))
                for key in ("endDateTime", "endDate"):
                    if key in upd:
                        self.end = str(upd[key])[:10]
                        self.applied.append(("end", self.end))
            elif service == "campaignCriteria":
                kw = ((entry.get("create") or {}).get("keyword") or {})
                neg = (str(kw.get("text", "")).strip().lower(),
                       str(kw.get("matchType", "PHRASE")).upper())
                self.negatives.add(neg)
                self.applied.append(("negative", neg))
        rec["applied"] = True
        return {"results": [{} for _ in ops]}


# -- reporting --------------------------------------------------------------------------------
def report(seed, step, invariant, call, expected, actual, detail=""):
    v = {"seed": seed, "step": step, "invariant": invariant,
         "severity": SEVERITY.get(invariant, "MEDIUM"), "call": call,
         "expected": expected, "actual": actual, "detail": detail}
    VIOLATIONS.append(v)
    print("VIOLATION seed=%d step=%d invariant=%d severity=%s call=%s\n"
          "          expected: %s\n          actual:   %s"
          % (seed, step, invariant, v["severity"], call, expected, actual))


def _kb(text, n=220):
    text = str(text)
    return text if len(text) <= n else text[:n] + "..."


# -- one seed ---------------------------------------------------------------------------------
def run_seed(seed):
    rnd = random.Random(seed)
    tmp = tempfile.mkdtemp(prefix="adsops-qa-%03d-" % seed)
    state_path = os.path.join(tmp, "c42.json")
    policy_path = os.path.join(tmp, "c42.policy.json")
    cfg_path = os.path.join(tmp, "cfg.json")
    ledger_path = os.path.join(tmp, "plans.jsonl")
    hb_path = os.path.join(tmp, "guard.heartbeat")
    audit_path = os.path.join(tmp, "audit.jsonl")

    allow_enable = (seed % 2 == 0)          # half the seeds forbid ENABLE outright
    policy = {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN_ID,
              "allow_enable": allow_enable, "require_plan_id": True,
              "max_guard_age_min": 30, "plan_ttl_s": 600,
              "protected_terms": list(PROTECTED), "guards": dict(POLICY_GUARDS)}
    state = {"customer_id": CUSTOMER, "campaign_id": CAMPAIGN_ID, "campaign_name": "c42",
             "status": "ENABLED", "allow_enable": True, "end_date": "2035-01-01",
             "daily_budget_usd": 10, "negatives_add": [], "guards": dict(STATE_GUARDS)}
    guards0 = copy.deepcopy(STATE_GUARDS)

    with open(state_path, "w") as fh:
        json.dump(state, fh)
    with open(policy_path, "w") as fh:
        json.dump(policy, fh)
    cfg_d = {"tenant": "qa", "meter_log": os.path.join(tmp, "meter.jsonl"),
             "audit_log": audit_path, "plan_ledger": ledger_path,
             "lock_dir": os.path.join(tmp, "locks"), "guard_heartbeat": hb_path,
             "credentials": {"google_ads": {"type": "static", "token": "x"}},
             "commit_policy": {"require_clean_git": True, "allow_commit": True, "allow_set": True},
             "google_ads": {"end_field": END_FIELD},
             "accounts": [{"key": "acct", "platform": "google_ads", "customer_id": CUSTOMER,
                           "campaigns": [{"key": CAMPAIGN, "state_file": state_path,
                                          "policy_file": policy_path}]}]}
    with open(cfg_path, "w") as fh:
        json.dump(cfg_d, fh)
    cfg = config.load_config(cfg_path)
    policy_sha0 = _sha(policy_path)

    clock = {"t": 1_800_000_000.0 + seed * 1000.0}
    sim = SimAccount()
    git = {"out": "", "ops": []}

    def runner(cmd, cwd=None, **kw):
        cmd = list(cmd)
        if "ls-files" in cmd:
            return R(0, state_path + "\n")
        if "--show-toplevel" in cmd:
            return R(0, tmp + "\n")
        if len(cmd) > 1 and cmd[1] == "rev-parse":
            return R(0, "f00dcafe\n")
        if len(cmd) > 1 and cmd[1] in ("add", "commit"):
            git["ops"].append(cmd[1])
            return R(0)
        return R(0, git["out"])

    srv = tools.build_server(cfg, client_factory=lambda account: sim,
                             git_runner=runner, now=lambda: clock["t"])

    # -- heartbeat helpers --------------------------------------------------------------
    def set_heartbeat(kind):
        if kind == "missing":
            try:
                os.unlink(hb_path)
            except OSError:
                pass
            return
        if kind == "fresh":
            safety.write_heartbeat(hb_path, now=lambda: clock["t"])
            os.utime(hb_path, (clock["t"], clock["t"]))
        elif kind == "stale":
            safety.write_heartbeat(hb_path, now=lambda: clock["t"])
            os.utime(hb_path, (clock["t"] - 3600, clock["t"] - 3600))
        elif kind == "future":
            safety.write_heartbeat(hb_path, now=lambda: clock["t"])
            os.utime(hb_path, (clock["t"] + 3600, clock["t"] + 3600))
        # "keep": leave whatever is there

    def hb_age():
        return safety.heartbeat_age_min(hb_path, now=lambda: clock["t"])

    # -- call generators ----------------------------------------------------------------
    issued_pids = []
    last_generic = {"pid": None}
    last_set = {"args": None, "pid": None}

    def pick_pid():
        r = rnd.random()
        if issued_pids and r < 0.60:
            return rnd.choice(issued_pids)
        if r < 0.85:
            return "%064x" % rnd.getrandbits(256)      # plausible garbage digest
        return "0" * 64

    def gen_set():
        base_reason = "qa fuzz seed %d step %d" % (seed, rnd.randint(1, 10 ** 6))
        if last_set["pid"] and rnd.random() < 0.45:     # replay the last dry_run, now applying
            args = dict(last_set["args"])
            args["campaign"] = CAMPAIGN
            args["reason"] = base_reason
            args["apply"] = True
            args["plan_id"] = last_set["pid"] if rnd.random() < 0.85 else pick_pid()
            return args
        args = {"campaign": CAMPAIGN, "reason": base_reason}
        picks = rnd.sample(["budget", "status", "end", "neg"], rnd.randint(1, 3))
        if "budget" in picks:
            args["daily_budget_usd"] = rnd.choice(BUDGETS)
        if "status" in picks:
            args["status"] = rnd.choice(["ENABLED", "PAUSED"])
        if "end" in picks:
            args["end_date"] = rnd.choice(ENDS)
        if "neg" in picks:
            args["negatives_add"] = [dict(n) for n in rnd.sample(NEGS, rnd.randint(1, 2))]
        m = rnd.random()
        if m < 0.35:
            args["dry_run"] = True
        elif m < 0.78:
            args["apply"] = True
            if rnd.random() < 0.5:
                args["plan_id"] = pick_pid()
        elif rnd.random() < 0.1:
            args["plan_id"] = "0" * 64
        return args

    def gen_apply():
        commit = rnd.random() < 0.65
        args = {"campaign": CAMPAIGN, "commit": commit}
        if commit:
            if last_generic["pid"] and rnd.random() < 0.6:
                args["plan_id"] = last_generic["pid"]
            elif rnd.random() < 0.5:
                args["plan_id"] = pick_pid()
        return args

    def gen_call():
        r = rnd.random()
        if r < 0.42:
            return "adsops_set", gen_set()
        if r < 0.62:
            return "adsops_apply", gen_apply()
        if r < 0.80:
            return "adsops_plan", {"campaign": CAMPAIGN}
        if r < 0.95:
            return "adsops_guard", {"campaign": CAMPAIGN, "commit": rnd.random() < 0.7}
        if r < 0.98:
            return "adsops_status", {"campaign": CAMPAIGN}
        return "adsops_terms", {"campaign": CAMPAIGN, "days": rnd.choice([1, 7, 14, 90])}

    seen_dup_pid = set()

    for step in range(1, STEPS + 1):
        # random world drift: clock, spend, heartbeat, dirt in git
        clock["t"] += rnd.choice(JUMPS)
        if rnd.random() < 0.5:
            sim.spend_today = rnd.choice(SPENDS_TODAY)
        if rnd.random() < 0.20:
            sim.spend_life = rnd.choice(SPENDS_LIFE)
        set_heartbeat(rnd.choices(["fresh", "stale", "future", "missing", "keep"],
                                  weights=[55, 18, 10, 7, 10])[0])
        age = hb_age()
        git["out"] = " M c42.json\n" if rnd.random() < 0.05 else ""

        tool, args = gen_call()
        sim.fail_next_commit = rnd.random() < 0.12
        live_before = sim.live_snapshot()
        COUNTER["calls"] += 1
        sim.begin(COUNTER["calls"], tool)
        mark = len(sim.records)
        call_desc = "%s %s" % (tool, _kb(json.dumps(args, sort_keys=True)))
        resp = None
        try:
            resp = srv.handle({"jsonrpc": "2.0", "id": step, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}})
        except BaseException as e:                                  # invariant 9
            report(seed, step, 9, call_desc, "server.handle returns a JSON-RPC response",
                   "raised %s: %s" % (type(e).__name__, e), traceback.format_exc()[-400:])
        if resp is None:
            continue
        if "result" not in resp:
            report(seed, step, 9, call_desc, "a tools/call result", _kb(json.dumps(resp)))
            continue
        res = resp["result"]
        ok = not res.get("isError")
        sc = res.get("structuredContent") if ok else None

        # remember plan ids the surface handed out
        if ok and isinstance(sc, dict) and sc.get("plan_id"):
            pid = sc["plan_id"]
            issued_pids.append(pid)
            if tool == "adsops_plan":
                last_generic["pid"] = pid
            if tool == "adsops_set" and args.get("dry_run"):
                last_set["args"] = {k: v for k, v in args.items()
                                    if k not in ("dry_run", "apply", "plan_id", "reason")}
                last_set["pid"] = pid

        recs = sim.records[mark:]
        val = [r for r in recs if r["validate_only"]]
        commits = [r for r in recs if not r["validate_only"]]

        # -- invariant 2: validate-before-commit, per call, identical payload ----------
        for i, rec in enumerate(recs):
            if rec["validate_only"]:
                continue
            twin = [r for r in recs[:i] if r["validate_only"]
                    and r["service"] == rec["service"]
                    and _canon(r["ops"]) == _canon(rec["ops"])]
            if not twin:
                report(seed, step, 2, call_desc,
                       "a validate_only=True mutate of the identical %s payload earlier in "
                       "this tool call" % rec["service"],
                       "committed %s without validation (validate calls seen: %s)"
                       % (rec["service"], [r["service"] for r in val]))

        # -- invariants 3, 4, 8 on committed payloads --------------------------------
        for rec in commits:
            if rec["service"] == "campaigns":
                for entry in rec["ops"]:
                    upd = entry.get("update") or {}
                    if upd.get("status") != "ENABLED":
                        continue
                    if rec["tool"] == "adsops_guard":
                        report(seed, step, 3, call_desc,
                               "adsops_guard only ever commits PAUSE",
                               "committed status=ENABLED via adsops_guard")
                    if not allow_enable:
                        report(seed, step, 4, call_desc,
                               "no ENABLED commit while policy allow_enable=false",
                               "committed status=ENABLED with allow_enable=false")
                    if rec["spend_today"] >= POLICY_GUARDS["daily_spend_pause_usd"]:
                        report(seed, step, 4, call_desc,
                               "no ENABLED commit while spend_today >= daily_spend_pause_usd (%s)"
                               % POLICY_GUARDS["daily_spend_pause_usd"],
                               "committed status=ENABLED with spend_today=%s" % rec["spend_today"])
            if rec["service"] == "campaignBudgets":
                for entry in rec["ops"]:
                    try:
                        usd = int(str((entry.get("update") or {}).get("amountMicros"))) / 1e6
                    except (TypeError, ValueError):
                        continue
                    lo, hi = POLICY_GUARDS["min_daily_budget_usd"], POLICY_GUARDS["max_daily_budget_usd"]
                    if usd > hi + 1e-9 or usd < lo - 1e-9:
                        report(seed, step, 1, call_desc,
                               "committed budget within [%s, %s]" % (lo, hi),
                               "committed budget %s" % usd)
            if rec["service"] == "campaignCriteria":
                for entry in rec["ops"]:
                    kw = ((entry.get("create") or {}).get("keyword") or {})
                    words = str(kw.get("text", "")).lower().split()
                    if not words:
                        continue
                    for term in PROTECTED:
                        tw = term.split()
                        if all(w in tw for w in words):                 # invariant 8 (harm)
                            report(seed, step, 8, call_desc,
                                   "refuse negatives that block protected term %r" % term,
                                   "committed negative %r (%s)"
                                   % (kw.get("text"), kw.get("matchType")))
                            break
                        if all(w in words for w in tw):                  # 8b: informational
                            INFO_8B.append({"seed": seed, "step": step, "call": call_desc,
                                            "negative": kw.get("text"),
                                            "match_type": kw.get("matchType"),
                                            "why": "words strictly contain protected term %r" % term})

        # -- invariant 5: no exposure increase committed without a fresh guard -------
        union = {}
        for rec in commits:
            union.setdefault(rec["service"], []).extend(copy.deepcopy(rec["ops"]))
        reasons = safety.exposure_increases(union, live_before, end_field=END_FIELD)
        if reasons:
            fresh = age is not None and -5.0 <= age <= POLICY_GUARDS.get("max_guard_age_min", 30)
            if not fresh:
                report(seed, step, 5, call_desc,
                       "refuse exposure increases while the guard heartbeat is stale/future/"
                       "missing (max_guard_age_min=%s)" % policy["max_guard_age_min"],
                       "committed %s with heartbeat age %s min"
                       % ("; ".join(reasons), "missing" if age is None else "%.1f" % age))

        # -- invariant 1 (post-state), 6 (ledger), 7 (immutable files) ---------------
        lo, hi = POLICY_GUARDS["min_daily_budget_usd"], POLICY_GUARDS["max_daily_budget_usd"]
        if sim.budget_usd > hi + 1e-9 or sim.budget_usd < lo - 1e-9:
            report(seed, step, 1, call_desc, "live budget within [%s, %s]" % (lo, hi),
                   "live budget is %s after the call" % sim.budget_usd)

        try:
            with open(ledger_path) as fh:
                events = [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            events = []
        counts = {}
        for ev in events:
            if isinstance(ev, dict) and ev.get("event") == "consumed" and isinstance(ev.get("pid"), str):
                counts[ev["pid"]] = counts.get(ev["pid"], 0) + 1
        for pid, n in counts.items():
            if n > 1 and pid not in seen_dup_pid:
                seen_dup_pid.add(pid)
                report(seed, step, 6, call_desc, "each plan_id consumed at most once",
                       "plan_id %s consumed %d times" % (pid[:16] + "...", n))

        try:
            with open(state_path) as fh:
                now_state = json.load(fh)
        except (OSError, ValueError) as e:
            report(seed, step, 7, call_desc, "state file stays valid JSON with the original guards",
                   "state file unreadable/invalid: %s" % e)
            now_state = None
        if isinstance(now_state, dict):
            if now_state.get("guards") != guards0:
                report(seed, step, 7, call_desc,
                       "state file guards identical to the original %s" % _canon(guards0),
                       "state file guards are %s" % _canon(now_state.get("guards")))
        if _sha(policy_path) != policy_sha0:
            report(seed, step, 7, call_desc, "policy file bytes unchanged",
                   "policy file was rewritten")

    shutil.rmtree(tmp, ignore_errors=True)


# -- report ------------------------------------------------------------------------------------
def write_report(path):
    by_inv = {}
    for v in VIOLATIONS:
        by_inv.setdefault(v["invariant"], []).append(v)
    lines = []
    lines.append("# adsops QA - adversarial property run (invariants)\n")
    lines.append("Generated by `tests/qa_invariants.py` "
                 "(stdlib only, `random.Random(seed)` for seeds 1..200).\n")
    lines.append("```\ncd <repo root>\n"
                 "python3 tests/qa_invariants.py\n```\n")
    lines.append("## What was run\n")
    lines.append("* %d seeds x %d steps = %d `tools/call` requests through "
                 "`adsops.protocol.Server.handle`.\n" % (len(SEEDS), STEPS, len(SEEDS) * STEPS))
    lines.append("* Each seed: fresh temp config / desired-state file / policy file "
                 "(`require_plan_id=true`, `max_guard_age_min=30`, "
                 "`protected_terms=['core term']`, caps `%s`) / plan ledger / heartbeat / fake git "
                 "runner, against a simulated Google Ads account (`search`/`mutate`, applying ops "
                 "only when `validate_only is False`).\n" % _canon(POLICY_GUARDS))
    lines.append("* Half the seeds run with `allow_enable=false`. Random budgets (in and out of "
                 "range), statuses, end dates (past / at max / beyond max), negatives (some "
                 "containing `core`), clocks (+0s..+1h), spend today/lifetime, heartbeats "
                 "(fresh/stale/future/missing), plan ids (fresh / reused / expired / garbage), "
                 "and 12% injected `ToolError` commit failures (reconcile path).\n")
    lines.append("\n## Result\n")
    lines.append("**%d violations** across %d sequences (%d tool calls).\n"
                 % (len(VIOLATIONS), len(SEEDS), COUNTER["calls"]))
    lines.append("\n| # | invariant | severity | violations |\n|---|---|---|---|\n")
    titles = {1: "budget stays inside policy min/max",
              2: "no commit without an identical validateOnly in the same call",
              3: "adsops_guard never enables",
              4: "no ENABLE when allow_enable=false or spend_today >= daily cap",
              5: "no exposure increase committed on a stale/future/missing heartbeat",
              6: "a plan_id is never accepted twice",
              7: "state-file guards unchanged, policy bytes unchanged",
              8: "no committed negative blocks a protected term",
              9: "no tool call raises out of server.handle"}
    for inv in sorted(titles):
        lines.append("| %d | %s | %s | %d |\n"
                     % (inv, titles[inv], SEVERITY[inv], len(by_inv.get(inv, []))))
    lines.append("\n")
    if VIOLATIONS:
        lines.append("## Violations\n")
        for v in VIOLATIONS:
            lines.append("### seed %d, step %d - invariant %d (%s, %s)\n\n"
                         % (v["seed"], v["step"], v["invariant"], titles.get(v["invariant"], "?"),
                            v["severity"]))
            lines.append("* **call:** `%s`\n* **expected:** %s\n* **actual:** %s\n"
                         % (v["call"], v["expected"], v["actual"]))
            if v["detail"]:
                lines.append("* **detail:**\n\n```\n%s\n```\n" % v["detail"])
            lines.append("* **reproduce:** seed %d (see `run_seed`), step %d\n\n" % (v["seed"], v["step"]))
    else:
        lines.append("## Violations\n\nNone. Every one of the nine rails held for all %d tool "
                     "calls.\n\n" % COUNTER["calls"])
    lines.append("## Informational (not counted as violations): invariant 8b\n")
    lines.append("Committed negatives whose words *strictly contain* a protected term "
                 "(e.g. `cheap core term`). These cannot block the protected query itself "
                 "(`core term`) under BROAD/PHRASE/EXACT matching, so they are not rail breaks - "
                 "listed for review only.\n\n")
    if INFO_8B:
        lines.append("| seed | step | negative | match type | why |\n|---|---|---|---|---|\n")
        seen = set()
        for item in INFO_8B:
            key = (item["negative"], item["match_type"])
            if key in seen:
                continue
            seen.add(key)
            lines.append("| %d | %d | `%s` | %s | %s |\n"
                         % (item["seed"], item["step"], item["negative"],
                            item["match_type"], item["why"]))
        lines.append("\n%d such commits in total.\n" % len(INFO_8B))
    else:
        lines.append("None.\n")
    lines.append("\n## Notes on the checks\n")
    lines.append("* Invariant 2 is evaluated per `server.handle` call, comparing the exact ops "
                 "payload of each `validate_only=False` mutate against every `validate_only=True` "
                 "mutate that preceded it in the same call (JSON-canonical comparison, service "
                 "must match too).\n")
    lines.append("* Invariant 5 uses `safety.exposure_increases()` on the union of committed ops "
                 "against the simulated live state captured *before* the call, and the heartbeat "
                 "mtime read after the call's setup. `require_fresh_guard` tolerates clock skew up "
                 "to -5 min and a max age of 30 min, so that window is what 'fresh' means here.\n")
    lines.append("* Invariant 6 is read off the plan ledger itself (`consumed` events per pid); a "
                 "second acceptance would append a second `consumed` event because "
                 "`PlanLedger.consume` appends only after all of its checks pass.\n")
    lines.append("* Invariant 1 is checked both on the committed `amountMicros` payload and on "
                 "the simulated account's budget after the call.\n")
    lines.append("* Invariant 7 compares the state file's `guards` object to the original "
                 "(the policy file is the only source of caps; the state file must never gain, "
                 "lose or mutate them) and the policy file's sha256.\n")
    with open(path, "w") as fh:
        fh.writelines(lines)


def main():
    for seed in SEEDS:
        run_seed(seed)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "QA-invariants.md")
    write_report(out)
    print("")
    print("QA: %d sequences, %d violations" % (len(SEEDS), len(VIOLATIONS)))
    by_inv = {}
    for v in VIOLATIONS:
        by_inv[v["invariant"]] = by_inv.get(v["invariant"], 0) + 1
    print("QA detail: %d tool calls; by invariant %s; informational 8b commits: %d; report: %s"
          % (COUNTER["calls"], by_inv or "none", len(INFO_8B), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
