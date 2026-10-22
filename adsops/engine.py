"""adsops.engine — desired state -> guarded plan -> execute.

A faithful port of gads_ctl.py's load_state / plan / guard_plan / execute. Behaviour is
identical to the reference implementation; only the error types differ (rail refusals raise
RailViolation, malformed input raises ToolError) and the campaign end-date field is a parameter
instead of a module global.

Pure: no network, no subprocess, no file I/O outside load_state().
"""
import datetime as dt
import json

from . import strict
from .errors import RailViolation, ToolError, scrub

MATCH = {"BROAD", "PHRASE", "EXACT"}
ORDER = ("campaignBudgets", "campaigns", "campaignCriteria")

_REQUIRED_STATE = ("customer_id", "campaign_id", "campaign_name", "guards")
_REQUIRED_GUARDS = ("max_daily_budget_usd", "max_end_date", "lifetime_spend_pause_usd",
                    "daily_spend_pause_usd", "max_ops_per_run")
_REQUIRED_LIVE = ("id", "name", "status", "end", "spend_life", "negatives")
_REQUIRED_LIVE_BUDGET = ("budget_usd", "budget_shared", "budget_rn")


def _end_key(end_field):
    """campaign.end_date_time -> endDateTime (same camelisation as gads_ctl)."""
    return "".join(w.capitalize() if i else w for i, w in enumerate(end_field.split("_")))


def _require(obj, keys, what):
    if not isinstance(obj, dict):
        raise ToolError(f"{what} must be an object")
    for k in keys:
        if k not in obj:
            raise ToolError(f"{what} missing '{k}'")


# ── state ─────────────────────────────────────────────────────────────────────────────────────
def load_state(path):
    """Read + validate a desired-state JSON file. Unreadable/malformed -> ToolError."""
    state = strict.load_json_file(path, "state file")
    if not isinstance(state, dict):
        raise ToolError("state must be a JSON object")
    for k in _REQUIRED_STATE:
        if k not in state:
            raise ToolError(f"state missing '{k}'")
    _require(state["guards"], _REQUIRED_GUARDS, "state.guards")
    if state.get("daily_budget_usd") is not None:
        strict.finite_number(state["daily_budget_usd"], "state.daily_budget_usd", 0, allow_equal_min=False)
    if state.get("end_date") is not None:
        strict.iso_date(state["end_date"], "state.end_date")
    if state.get("status") not in (None, "ENABLED", "PAUSED"):
        raise ToolError("state.status must be ENABLED or PAUSED")
    if "negatives_add" in state:
        strict.negatives_list(state["negatives_add"], "state.negatives_add")
    for k in ("max_daily_budget_usd", "lifetime_spend_pause_usd", "daily_spend_pause_usd", "min_daily_budget_usd"):
        if k in state["guards"]:
            strict.finite_number(state["guards"][k], f"state.guards.{k}", 0)
    return state


# ── plan (pure) ───────────────────────────────────────────────────────────────────────────────
def plan(state, live, today=None, end_field="end_date_time"):
    """Return (ops_by_service, notes). Pure: no I/O.

    Rails raise RailViolation; malformed state raises ToolError.
    """
    today = today or dt.date.today().isoformat()
    _require(state, ("guards",), "state")
    _require(state["guards"], _REQUIRED_GUARDS, "state.guards")
    _require(live, _REQUIRED_LIVE, "live state")
    g, ops, notes = state["guards"], {}, []

    if live["id"] != str(state["campaign_id"]) or live["name"] != state["campaign_name"]:
        raise RailViolation(f"campaign identity mismatch: live {live['id']}/{live['name']!r}")
    rn = f"customers/{str(state['customer_id']).replace('-', '')}/campaigns/{live['id']}"
    camp, mask = {"resourceName": rn}, []

    want_status = state.get("status")
    if want_status and want_status != live["status"]:
        if want_status == "ENABLED":
            if not state.get("allow_enable"):
                raise RailViolation("refusing to ENABLE: state lacks allow_enable=true")
            if live["spend_life"] >= g["lifetime_spend_pause_usd"]:
                raise RailViolation("refusing to ENABLE: lifetime spend at/over cap")
        elif want_status != "PAUSED":
            raise RailViolation(f"status {want_status!r} not allowed (ENABLED|PAUSED only)")
        camp["status"] = want_status
        mask.append("status")

    want_end = state.get("end_date")
    if want_end and want_end != live["end"]:
        if want_end > g["max_end_date"]:
            raise RailViolation(f"end_date {want_end} beyond guard max_end_date {g['max_end_date']}")
        if want_end < today:
            raise RailViolation(f"end_date {want_end} is in the past")
        key = _end_key(end_field)
        camp[key] = want_end + (" 23:59:59" if end_field.endswith("time") else "")
        mask.append(key)
    if mask:
        ops["campaigns"] = [{"update": camp, "updateMask": ",".join(mask)}]

    want_b = state.get("daily_budget_usd")
    if want_b is not None:
        _require(live, _REQUIRED_LIVE_BUDGET, "live state")
        if abs(want_b - live["budget_usd"]) > 0.005:
            if live["budget_shared"]:
                raise RailViolation("refusing budget change: budget is explicitly shared")
            if not (g.get("min_daily_budget_usd", 1) <= want_b <= g["max_daily_budget_usd"]):
                raise RailViolation(f"daily_budget_usd {want_b} outside guard range")
            ops["campaignBudgets"] = [{"update": {"resourceName": live["budget_rn"],
                                                  "amountMicros": str(int(round(want_b * 1e6)))},
                                       "updateMask": "amountMicros"}]

    have = {(t, m) for t, m in live["negatives"]}
    adds = []
    for n in state.get("negatives_add", []) or []:
        if not isinstance(n, dict) or "text" not in n:
            raise ToolError(f"state.negatives_add entry is malformed: {n!r}")
        t, m = n["text"].strip().lower(), n.get("match_type", "PHRASE").upper()
        if m not in MATCH or not t:
            raise ToolError(f"bad negative {n!r}")
        if (t, m) not in have and (t, m) not in {(a[0], a[1]) for a in adds}:
            adds.append((t, m))
    if adds:
        ops["campaignCriteria"] = [{"create": {"campaign": rn, "negative": True,
                                               "keyword": {"text": t, "matchType": m}}} for t, m in adds]
        notes.append(f"{len(adds)} new negatives")
    total = sum(len(v) for v in ops.values())
    if total > g["max_ops_per_run"]:
        raise RailViolation(f"{total} ops exceeds guard max_ops_per_run {g['max_ops_per_run']}")
    return ops, notes


def guard_plan(state, live):
    """Spend caps -> a PAUSE-only plan. Never enables, never raises a budget."""
    _require(state, ("guards",), "state")
    g = state["guards"]
    _require(g, ("lifetime_spend_pause_usd", "daily_spend_pause_usd"), "state.guards")
    _require(live, ("status", "spend_life", "spend_today", "id"), "live state")
    if live["status"] != "ENABLED":
        return None, "not enabled"
    if live["spend_life"] >= g["lifetime_spend_pause_usd"]:
        why = f"lifetime ${live['spend_life']:.2f} >= ${g['lifetime_spend_pause_usd']}"
    elif live["spend_today"] >= g["daily_spend_pause_usd"]:
        why = f"today ${live['spend_today']:.2f} >= ${g['daily_spend_pause_usd']}"
    else:
        return None, "under caps"
    rn = f"customers/{str(state['customer_id']).replace('-', '')}/campaigns/{live['id']}"
    return {"campaigns": [{"update": {"resourceName": rn, "status": "PAUSED"},
                           "updateMask": "status"}]}, why


# ── execute ───────────────────────────────────────────────────────────────────────────────────
def execute(client, ops, commit):
    """validateOnly first, always. Commit only if commit and the validation pass raised nothing."""
    if not ops:
        return {}
    if not isinstance(ops, dict):
        raise ToolError("ops must be an object")
    res = {}
    for svc in ORDER:
        if svc in ops:
            client.mutate(svc, ops[svc], validate_only=True, partial=(svc == "campaignCriteria"))
    if commit:
        for svc in ORDER:
            if svc in ops:
                r = client.mutate(svc, ops[svc], validate_only=False,
                                  partial=(svc == "campaignCriteria"))
                if not isinstance(r, dict):
                    r = {}
                res[svc] = {"results": len(r.get("results", [])),
                            "partialFailure": bool(r.get("partialFailureError"))}
    return res


def ops_count(ops):
    """Number of operations in an ops dict (0 for None / non-dict)."""
    if not isinstance(ops, dict):
        return 0
    return sum(len(v) for v in ops.values() if isinstance(v, (list, tuple)))


# ── selftest ──────────────────────────────────────────────────────────────────────────────────
GADS_CTL_PATH = __import__("os").environ.get("ADSOPS_PARITY_REF", "")  # optional reference impl for parity checks
TODAY = "2026-10-12"


def _load_gads_ctl():
    """Import the reference implementation for parity checks; None if unavailable."""
    import importlib.util
    import os
    try:
        if not os.path.exists(GADS_CTL_PATH):
            return None
        spec = importlib.util.spec_from_file_location("_gads_ctl_reference", GADS_CTL_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _state(**over):
    return {"customer_id": "123-456-7890", "campaign_id": 9876543210,
            "campaign_name": "acme_search", "negatives_add": [],
            "guards": {"max_daily_budget_usd": 30, "min_daily_budget_usd": 5,
                       "max_end_date": "2026-11-30", "lifetime_spend_pause_usd": 300,
                       "daily_spend_pause_usd": 50, "max_ops_per_run": 60}, **over}


def _live(**over):
    return {"id": "9876543210", "name": "acme_search", "status": "ENABLED",
            "end": "2026-10-14", "budget_rn": "customers/1234567890/campaignBudgets/1",
            "budget_usd": 25.0, "budget_shared": False, "spend_life": 40.0,
            "spend_today": 5.0, "negatives": [("tenant", "BROAD")], **over}


def _attempt(fn):
    try:
        return "ok", fn()
    except Exception as e:  # noqa: BLE001 — parity compares exception classes
        return "err", e


class _FakeClient:
    """Records mutate calls; never touches a network."""

    def __init__(self, partial_error=False):
        self.calls, self.partial_error = [], partial_error

    def mutate(self, service, ops, validate_only, partial=False):
        self.calls.append((service, validate_only, partial))
        out = {"results": [{}] * len(ops)}
        if self.partial_error and service == "campaignCriteria":
            out["partialFailureError"] = {"code": 3}
        return out


def selftest():
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, msg
        n += 1

    # ── load_state (tempfile only) ────────────────────────────────────────────────────────────
    import os
    import tempfile

    td = tempfile.mkdtemp()

    def dump(name, obj):
        p = os.path.join(td, name)
        with open(p, "w") as fh:
            json.dump(obj, fh)
        return p

    ck(load_state(dump("good.json", _state()))["campaign_name"] == "acme_search",
       "load_state roundtrip")
    g2 = dict(_state()["guards"])
    g2.pop("max_ops_per_run")
    bad = [("missing_key.json", {"customer_id": "1", "campaign_id": 2,
                                 "guards": dict(_state()["guards"])}, "campaign_name"),
           ("bad_guard.json", _state(guards=g2), "max_ops_per_run")]
    for name, obj, frag in bad:
        r = _attempt(lambda: load_state(dump(name, obj)))
        ck(r[0] == "err" and isinstance(r[1], ToolError) and frag in str(r[1]),
           f"load_state {name} -> ToolError naming {frag}")
    r = _attempt(lambda: load_state(os.path.join(td, "absent.json")))
    ck(r[0] == "err" and isinstance(r[1], ToolError), "load_state absent file -> ToolError")

    # ── PARITY: plan vs gads_ctl.plan ────────────────────────────────────────────────────────
    ref = _load_gads_ctl()
    end_field = getattr(ref, "END_FIELD", "end_date_time") if ref else "end_date_time"
    S, L = _state, _live
    dups = [{"text": "Tenant", "match_type": "BROAD"}, {"text": " jobs ", "match_type": "BROAD"},
            {"text": "jobs", "match_type": "BROAD"},
            {"text": "property management jobs", "match_type": "PHRASE"}]
    fixtures = [
        ("no-op", S(), L()),
        ("status pause", S(status="PAUSED"), L()),
        ("enable allowed", S(status="ENABLED", allow_enable=True), L(status="PAUSED")),
        ("enable refused no allow_enable", S(status="ENABLED"), L(status="PAUSED")),
        ("enable refused lifetime cap", S(status="ENABLED", allow_enable=True),
         L(status="PAUSED", spend_life=400.0)),
        ("end date change", S(end_date="2026-11-05"), L()),
        ("end beyond max", S(end_date="2027-01-01"), L()),
        ("end in past", S(end_date="2020-01-01"), L()),
        ("budget change", S(daily_budget_usd=20.0), L()),
        ("budget out of range", S(daily_budget_usd=80.0), L()),
        ("shared budget", S(daily_budget_usd=20.0), L(budget_shared=True)),
        ("negatives add with dupes", S(negatives_add=dups), L()),
        ("too many ops", S(negatives_add=[{"text": f"n{i}"} for i in range(80)]), L()),
        ("bad match type", S(negatives_add=[{"text": "x", "match_type": "FUZZY"}]), L()),
        ("identity mismatch", S(campaign_name="some_other_campaign"), L()),
        ("removed status", S(status="REMOVED", allow_enable=True), L()),
    ]
    results = {}
    for name, state, live in fixtures:
        mine = _attempt(lambda: plan(state, live, today=TODAY, end_field=end_field))
        results[name] = mine
        if ref is None:
            continue
        theirs = _attempt(lambda: ref.plan(state, live, today=TODAY))
        if theirs[0] == "ok":
            ck(mine[0] == "ok" and mine[1] == theirs[1],
               f"parity plan [{name}]: engine={mine!r} gads_ctl={theirs!r}")
        else:
            ck(isinstance(theirs[1], ref.Abort), f"parity [{name}]: reference raised Abort")
            ck(mine[0] == "err" and isinstance(mine[1], ToolError),
               f"parity plan [{name}]: engine must raise ToolError, got {mine!r}")

    # engine-only shape assertions (also run when the reference is absent)
    ops, notes = results["negatives add with dupes"][1]
    ck(len(ops["campaignCriteria"]) == 2 and notes == ["2 new negatives"],
       "dedupe: strip + lowercase + in-batch dupes + already-live negatives")
    ck({o["create"]["keyword"]["text"] for o in ops["campaignCriteria"]}
       == {"jobs", "property management jobs"}, "negatives lowercased")
    ops, _ = results["end date change"][1]
    ck(ops["campaigns"][0]["update"][_end_key(end_field)] == "2026-11-05 23:59:59",
       "end date gets 23:59:59 suffix")
    ck(ops["campaigns"][0]["updateMask"].endswith(_end_key(end_field)), "end date updateMask")
    ck(results["budget change"][1][0]["campaignBudgets"][0]["update"]["amountMicros"]
       == "20000000", "budget micros")
    ck(results["enable allowed"][1][0]["campaigns"][0]["update"]["status"] == "ENABLED",
       "enable allowed produces ENABLED")
    ck(results["no-op"][1] == ({}, []), "no-op plan is empty")
    for name in ("enable refused no allow_enable", "enable refused lifetime cap", "shared budget",
                 "budget out of range", "end beyond max", "end in past", "too many ops",
                 "identity mismatch", "removed status"):
        ck(isinstance(results[name][1], RailViolation), f"rail violation: {name}")
    ck(isinstance(results["bad match type"][1], ToolError)
       and not isinstance(results["bad match type"][1], RailViolation),
       "bad match type is malformed state -> ToolError")
    ops, _ = plan(S(end_date="2026-11-05"), L(), today=TODAY, end_field="end_date")
    ck(ops["campaigns"][0]["update"].get("endDate") == "2026-11-05"
       and "endDateTime" not in ops["campaigns"][0]["update"], "end_field honoured (no time suffix)")
    state, live = fixtures[5][1], fixtures[5][2]
    before = (json.dumps(state, sort_keys=True), json.dumps(live, sort_keys=True))
    plan(state, live, today=TODAY)
    ck(before == (json.dumps(state, sort_keys=True), json.dumps(live, sort_keys=True)),
       "plan is pure (no input mutation)")

    # ── PARITY: guard_plan ───────────────────────────────────────────────────────────────────
    for name, state, live in [("under caps", S(), L()),
                              ("daily over", S(), L(spend_today=60.0)),
                              ("lifetime over", S(), L(spend_life=400.0)),
                              ("not enabled", S(), L(status="PAUSED", spend_today=90.0))]:
        mine = _attempt(lambda: guard_plan(state, live))
        ck(mine[0] == "ok", f"guard_plan [{name}] runs")
        if ref is not None:
            theirs = _attempt(lambda: ref.guard_plan(state, live))
            ck(theirs[0] == "ok" and mine[1] == theirs[1], f"parity guard_plan [{name}]")
    ops, why = guard_plan(S(), L(spend_today=60.0))
    ck(list(ops) == ["campaigns"] and ops["campaigns"][0]["update"]["status"] == "PAUSED"
       and ops["campaigns"][0]["updateMask"] == "status", "guard_plan is PAUSE-only")
    ck("today $" in why and ">=" in why, "guard_plan explains the daily cap")
    ck(guard_plan(S(), L())[0] is None, "guard_plan idle under caps")
    ck(guard_plan(S(), L(status="PAUSED", spend_life=400.0)) == (None, "not enabled"),
       "guard_plan never enables")
    ck("lifetime $" in guard_plan(S(), L(spend_life=400.0, spend_today=5.0))[1],
       "guard_plan explains the lifetime cap")

    # ── execute ──────────────────────────────────────────────────────────────────────────────
    ops = {"campaignBudgets": [{"update": {}, "updateMask": "amountMicros"}],
           "campaigns": [{"update": {}, "updateMask": "status"}],
           "campaignCriteria": [{"create": {}}, {"create": {}}]}
    c = _FakeClient()
    ck(execute(c, ops, commit=False) == {} and c.calls == [
        ("campaignBudgets", True, False), ("campaigns", True, False),
        ("campaignCriteria", True, True)],
        f"commit=False: validateOnly only, ORDER, partial only for criteria: {c.calls}")
    c = _FakeClient()
    res = execute(c, ops, commit=True)
    seq = [(svc, vo) for svc, vo, _ in c.calls]
    ck(seq == [(svc, v) for v in (True, False) for svc in ORDER],
       f"validate pass precedes commit pass, ORDER respected: {seq}")
    ck(all(p == (svc == "campaignCriteria") for svc, _, p in c.calls),
       "partial=True only for campaignCriteria")
    ck(res == {"campaignBudgets": {"results": 1, "partialFailure": False},
               "campaigns": {"results": 1, "partialFailure": False},
               "campaignCriteria": {"results": 2, "partialFailure": False}}, f"results: {res}")
    c = _FakeClient(partial_error=True)
    ck(execute(c, {"campaignCriteria": [{"create": {}}]}, True)["campaignCriteria"]
       == {"results": 1, "partialFailure": True}, "partialFailureError -> partialFailure")
    c = _FakeClient()
    ck(execute(c, {}, commit=True) == {} and c.calls == [], "empty ops -> no calls")
    ck(ops_count(ops) == 4, "ops_count sums operations")
    ck(ops_count(None) == 0 and ops_count({}) == 0, "ops_count tolerates None/{}")
    ck(MATCH == {"BROAD", "PHRASE", "EXACT"}
       and ORDER == ("campaignBudgets", "campaigns", "campaignCriteria"), "constants")
    return n
