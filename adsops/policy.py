"""Policy file, spend-exposure identity checks, plan ids, and the plan ledger.

The policy file is the single source of truth for caps; no adsops tool writes it.
A plan id binds the exact reviewed ops to the policy and live state that produced
them, and PlanLedger makes each plan one-shot: applied at most once, before expiry.

Pure apart from load_policy / file_sha256 / PlanLedger file access.
"""
import copy
import hashlib
import json
import os
import time
from datetime import datetime, timezone

from .errors import RailViolation, ToolError, scrub

REQUIRED_GUARDS = ("max_daily_budget_usd", "max_end_date", "lifetime_spend_pause_usd",
                   "daily_spend_pause_usd", "max_ops_per_run")
_NUMERIC_GUARDS = ("max_daily_budget_usd", "lifetime_spend_pause_usd",
                   "daily_spend_pause_usd", "min_daily_budget_usd")
DEFAULT_PLAN_TTL_S, MIN_PLAN_TTL_S, MAX_PLAN_TTL_S = 900, 60, 86400

def _expand(path):
    return os.path.expanduser(os.fspath(path)) if path is not None else path

def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def _is_date(value):
    try:
        return (isinstance(value, str) and len(value) == 10
                and bool(datetime.strptime(value, "%Y-%m-%d")))
    except ValueError:
        return False

def _iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _parse_ts(value):
    """Epoch seconds from an ISO-8601 string or a number; None if unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None

def _validate(raw):
    if not isinstance(raw, dict):
        raise ToolError("policy: top level must be a JSON object")
    pol = copy.deepcopy(raw)
    cid = pol.get("customer_id")
    if not isinstance(cid, str) or not cid.strip():
        raise ToolError("policy: 'customer_id' must be a non-empty string")
    camp = pol.get("campaign_id")
    if isinstance(camp, bool) or not isinstance(camp, (int, str)):
        raise ToolError("policy: 'campaign_id' must be an int or a non-empty string")
    if isinstance(camp, str) and not camp.strip():
        raise ToolError("policy: 'campaign_id' must be an int or a non-empty string")
    guards = pol.get("guards")
    if not isinstance(guards, dict):
        raise ToolError("policy: 'guards' must be an object")
    for key in REQUIRED_GUARDS:
        if key not in guards:
            raise ToolError(f"policy: guards['{key}'] is required")
    for key in _NUMERIC_GUARDS:
        if key in guards and (not _is_num(guards[key]) or guards[key] < 0):
            raise ToolError(f"policy: guards['{key}'] must be a number >= 0")
    ops = guards["max_ops_per_run"]
    if isinstance(ops, bool) or not isinstance(ops, int) or ops < 0:
        raise ToolError("policy: guards['max_ops_per_run'] must be an integer >= 0")
    if not _is_date(guards["max_end_date"]):
        raise ToolError("policy: guards['max_end_date'] must be 'YYYY-MM-DD'")
    if ("min_daily_budget_usd" in guards
            and guards["min_daily_budget_usd"] > guards["max_daily_budget_usd"]):
        raise ToolError("policy: guards['min_daily_budget_usd'] must not exceed "
                        "guards['max_daily_budget_usd']")
    allow = pol.get("allow_enable", False)
    if not isinstance(allow, bool):
        raise ToolError("policy: 'allow_enable' must be a boolean")
    age = pol.get("max_guard_age_min")
    if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 1):
        raise ToolError("policy: 'max_guard_age_min' must be a positive integer or null")
    require_pid = pol.get("require_plan_id", False)
    if not isinstance(require_pid, bool):
        raise ToolError("policy: 'require_plan_id' must be a boolean")
    prot = pol.get("protected_terms", [])
    if not isinstance(prot, list) or not all(isinstance(t, str) and t.strip() for t in prot):
        raise ToolError("policy: 'protected_terms' must be a list of non-empty strings")
    pol["protected_terms"] = [t.strip().lower() for t in prot]
    ttl = pol.get("plan_ttl_s", DEFAULT_PLAN_TTL_S)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not MIN_PLAN_TTL_S <= ttl <= MAX_PLAN_TTL_S:
        raise ToolError(f"policy: 'plan_ttl_s' must be an integer in "
                        f"{MIN_PLAN_TTL_S}..{MAX_PLAN_TTL_S}")
    pol["allow_enable"] = allow
    pol["max_guard_age_min"] = age
    pol["require_plan_id"] = require_pid
    pol["plan_ttl_s"] = ttl
    return pol

def load_policy(path):
    """Read + validate a policy JSON file. Any problem raises ToolError."""
    path = _expand(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ToolError(f"policy: file not found: {path}")
    except json.JSONDecodeError as e:
        raise ToolError(f"policy: invalid JSON in {path}: {scrub(e)}")
    except OSError as e:
        raise ToolError(f"policy: cannot read {path}: {scrub(e)}")
    return _validate(raw)

def effective_state(state, policy):
    """Deep copy of state with guards/allow_enable taken from policy only."""
    if not isinstance(state, dict):
        raise ToolError("state: must be an object")
    if not isinstance(policy, dict):
        raise ToolError("policy: must be an object")
    pol_cid = str(policy.get("customer_id", "")).strip().replace("-", "")
    st_cid = str(state.get("customer_id", "")).strip().replace("-", "")
    if pol_cid != st_cid:
        raise RailViolation(
            f"policy customer_id {policy.get('customer_id')!r} does not match "
            f"state customer_id {state.get('customer_id')!r}")
    if str(policy.get("campaign_id")) != str(state.get("campaign_id")):
        raise RailViolation(
            f"policy campaign_id {policy.get('campaign_id')!r} does not match "
            f"state campaign_id {state.get('campaign_id')!r}")
    eff = copy.deepcopy(state)
    eff["guards"] = copy.deepcopy(policy.get("guards") or {})
    eff["allow_enable"] = bool(policy.get("allow_enable", False))
    return eff

def file_sha256(path):
    """Hex sha256 of the file bytes."""
    path = _expand(path)
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
    except OSError as e:
        raise ToolError(f"cannot read file for hashing: {path}: {scrub(e)}")
    return digest.hexdigest()

def canonical(obj):
    """Stable JSON encoding used for every hash in adsops."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)

def live_fingerprint(live):
    """Hash of live state ignoring spend counters (they move constantly)."""
    data = dict(live) if isinstance(live, dict) else live
    if isinstance(data, dict):
        data.pop("spend_today", None)
        data.pop("spend_life", None)
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()

def plan_id(account_key, campaign_key, ops, policy_hash, live_fp):
    """A deterministic id for exactly this reviewed plan."""
    payload = [account_key, campaign_key, ops, policy_hash, live_fp]
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()

class PlanLedger:
    """Append-only JSONL ledger: a plan id may be issued and consumed once."""
    def __init__(self, path, now=time.time):
        self.path = path
        self.now = now
    def _file(self):
        return _expand(self.path)
    def _append(self, entry):
        path = self._file()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as e:
            raise ToolError(f"plan ledger unwritable: {scrub(e)}")
        return entry
    def _events(self):
        """Valid ledger events in file order; malformed lines are skipped."""
        try:
            with open(self._file(), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            raise ToolError(f"plan ledger unreadable: {scrub(e)}")
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(ev, dict):
                continue
            pid = ev.get("pid")
            if not isinstance(pid, str) or not pid:
                continue
            if ev.get("event") == "issued":
                if _parse_ts(ev.get("expires")) is not None:
                    events.append(ev)
            elif ev.get("event") == "consumed":
                events.append(ev)
        return events
    def issue(self, pid, ttl_s):
        t = float(self.now())
        entry = {"event": "issued", "pid": str(pid), "at": _iso(t),
                 "expires": _iso(t + float(ttl_s))}
        return self._append(entry)
    def consume(self, pid):
        pid = str(pid)
        t = float(self.now())
        events = self._events()
        issued = [e for e in events if e["event"] == "issued" and e["pid"] == pid]
        if not issued:
            raise RailViolation("unknown plan_id")
        expires = _parse_ts(issued[-1].get("expires"))
        if expires is None or t > expires:
            raise RailViolation("plan_id expired")
        if any(e["event"] == "consumed" and e["pid"] == pid for e in events):
            raise RailViolation("plan_id already used")
        return self._append({"event": "consumed", "pid": pid, "at": _iso(t)})

def selftest():
    import pathlib
    import tempfile
    n = 0
    def ok(cond, msg="check failed"):
        nonlocal n
        assert cond, msg
        n += 1
    def raises(exc, fn, *needles):
        nonlocal n
        try:
            fn()
        except exc as e:
            msg = str(e)
            assert all(x in msg for x in needles), (needles, msg)
            n += 1
        else:
            raise AssertionError(f"expected {exc.__name__}")
    def fails(fn, *needles):
        raises(ToolError, fn, *needles)
    def refuses(fn, *needles):
        raises(RailViolation, fn, *needles)
    def base():
        return {"customer_id": "123-456-7890", "campaign_id": 555, "guards": {
            "max_daily_budget_usd": 50, "max_end_date": "2026-12-31",
            "lifetime_spend_pause_usd": 400, "daily_spend_pause_usd": 20,
            "max_ops_per_run": 10}}
    with tempfile.TemporaryDirectory() as td:
        def dump(obj, name="policy.json"):
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
            return p
        def bad(top=None, guard=None, *needles):
            b = base()
            if top:
                b.update(top)
            if guard:
                b["guards"].update(guard)
            fails(lambda: load_policy(dump(b, "bad.json")), *needles)
        # file-level failures
        fails(lambda: load_policy(os.path.join(td, "nope.json")), "not found")
        bad_json = os.path.join(td, "malformed.json")
        pathlib.Path(bad_json).write_text("{not json", encoding="utf-8")
        fails(lambda: load_policy(bad_json), "invalid JSON")
        fails(lambda: load_policy(dump([1, 2], "root.json")), "JSON object")
        # identity + guard types
        for v in ("", "   ", 123, None, True):
            bad({"customer_id": v}, None, "customer_id")
        for v in (True, 5.5, "", None, [555]):
            bad({"campaign_id": v}, None, "campaign_id")
        b = base()
        del b["guards"]
        fails(lambda: load_policy(dump(b, "g1.json")), "guards")
        b = base()
        b["guards"] = ["nope"]
        fails(lambda: load_policy(dump(b, "g2.json")), "guards")
        for key in REQUIRED_GUARDS:
            b = base()
            del b["guards"][key]
            fails(lambda b=b: load_policy(dump(b, "g3.json")), key)
        for key in _NUMERIC_GUARDS:
            for v in ("50", True, -1, None):
                bad(None, {key: v}, key)
        for v in ("10", True, -1, 2.5, None):
            bad(None, {"max_ops_per_run": v}, "max_ops_per_run")
        for v in ("2026/12/31", "2026-13-01", 20261231, "soon", None):
            bad(None, {"max_end_date": v}, "max_end_date")
        bad(None, {"min_daily_budget_usd": 60}, "min_daily_budget_usd")
        for v in ("true", 1, 0, None):
            bad({"allow_enable": v}, None, "allow_enable")
        for v in ("15", 1.5, -3, 0, True):
            bad({"max_guard_age_min": v}, None, "max_guard_age_min")
        for v in ("yes", 1, None):
            bad({"require_plan_id": v}, None, "require_plan_id")
        for v in (10, 59, 86401, True, "900", 900.0, None):
            bad({"plan_ttl_s": v}, None, "plan_ttl_s")
        # valid + defaults
        cfg = load_policy(dump(base(), "ok.json"))
        ok((cfg["customer_id"], cfg["campaign_id"], cfg["allow_enable"], cfg["max_guard_age_min"],
            cfg["require_plan_id"], cfg["plan_ttl_s"], cfg["guards"]["max_ops_per_run"])
           == ("123-456-7890", 555, False, None, False, DEFAULT_PLAN_TTL_S, 10))
        b = base()
        b["guards"]["min_daily_budget_usd"] = 5
        b.update(allow_enable=True, max_guard_age_min=15, require_plan_id=True, plan_ttl_s=600)
        c2 = load_policy(dump(b, "ok2.json"))
        ok((c2["allow_enable"], c2["max_guard_age_min"], c2["require_plan_id"],
            c2["plan_ttl_s"], c2["guards"]["min_daily_budget_usd"]) == (True, 15, True, 600, 5))
        # effective_state: policy wins, state untouched, both mismatches refuse
        pol = load_policy(dump(base(), "eff.json"))
        state = {"customer_id": "1234567890", "campaign_id": "555", "campaign_name": "acme",
                 "guards": {k: 999 for k in REQUIRED_GUARDS}, "allow_enable": True,
                 "daily_budget_usd": 7}
        eff = effective_state(state, pol)
        ok(eff["guards"] == pol["guards"] and eff["guards"] is not pol["guards"])
        ok(eff["allow_enable"] is False and state["allow_enable"] is True)
        ok(state["guards"]["max_ops_per_run"] == 999)
        ok(eff["daily_budget_usd"] == 7 and eff["campaign_name"] == "acme")
        pol_on = dict(pol)
        pol_on["allow_enable"] = True
        ok(effective_state(state, pol_on)["allow_enable"] is True)
        refuses(lambda: effective_state(state, {**base(), "customer_id": "999-999-9999"}),
                "customer_id")
        refuses(lambda: effective_state(state, {**base(), "campaign_id": 556}), "campaign_id")
        # hashing
        f1 = dump(base(), "sha1.json")
        f2 = dump({**base(), "campaign_id": 556}, "sha2.json")
        ok(len(file_sha256(f1)) == 64 and file_sha256(f1) == file_sha256(f1))
        ok(file_sha256(f1) != file_sha256(f2))
        fails(lambda: file_sha256(os.path.join(td, "nope.bin")), "cannot read file")
        ok(canonical({"b": 1, "a": 2}) == '{"a":2,"b":1}' and canonical([1, 2]) == "[1,2]")
        live = {"id": "555", "name": "acme", "status": "ENABLED", "end": "2026-01-01",
                "budget_usd": 10.0, "spend_today": 3.0, "spend_life": 100.0,
                "negatives": [["free", "PHRASE"]]}
        fp = live_fingerprint(live)
        ok(len(fp) == 64)
        ok(live_fingerprint({**live, "spend_today": 99.0, "spend_life": 12345.0}) == fp)
        ok(live_fingerprint({**live, "budget_usd": 11.0}) != fp)
        ok("spend_today" in live and "spend_life" in live)
        # plan_id changes with ANY input
        ops = {"campaigns": [{"update": {"resourceName": "rn", "status": "PAUSED"},
                              "updateMask": "status"}]}
        pid0 = plan_id("acme", "acme_search", ops, "ph", fp)
        ok(len(pid0) == 64
           and pid0 == plan_id("acme", "acme_search", copy.deepcopy(ops), "ph", fp))
        ok(plan_id("other", "acme_search", ops, "ph", fp) != pid0)
        ok(plan_id("acme", "other", ops, "ph", fp) != pid0)
        ok(plan_id("acme", "acme_search", {"campaigns": []}, "ph", fp) != pid0)
        ok(plan_id("acme", "acme_search", ops, "ph2", fp) != pid0)
        ok(plan_id("acme", "acme_search", ops, "ph", "fp2") != pid0)
        # ledger: issue/consume, double use, expiry, unknown, newest issue, malformed lines
        clock = [1000.0]
        lpath = os.path.join(td, "nested", "plans.jsonl")
        led = PlanLedger(lpath, now=lambda: clock[0])
        e = led.issue("pid-1", 900)
        ok(e["event"] == "issued" and e["pid"] == "pid-1" and "at" in e and "expires" in e)
        ok(os.path.isfile(lpath) and os.path.isdir(os.path.dirname(lpath)))
        clock[0] = 1899.0
        ok(led.consume("pid-1")["event"] == "consumed")
        refuses(lambda: led.consume("pid-1"), "already used")
        led.issue("pid-2", 900)  # at 1899 -> expires 2799
        clock[0] = 2800.0
        refuses(lambda: led.consume("pid-2"), "expired")
        refuses(lambda: led.consume("never-issued"), "unknown plan_id")
        clock[0] = 3000.0
        led.issue("pid-3", 60)
        clock[0] = 3050.0
        led.issue("pid-3", 900)  # newest issue wins
        clock[0] = 3100.0
        ok(led.consume("pid-3")["event"] == "consumed")
        clock[0] = 4000.0
        led.issue("pid-4", 60)
        clock[0] = 4060.0  # exactly at expiry is still valid
        ok(led.consume("pid-4")["event"] == "consumed")
        with open(lpath, "a", encoding="utf-8") as fh:
            fh.write("not json\n[]\n{}\n")
        led.issue("pid-5", 900)  # at 4060 -> expires 4960
        ok(led.consume("pid-5")["event"] == "consumed")
        refuses(lambda: led.consume("pid-5"), "already used")
        refuses(lambda: led.consume("pid-1"), "expired")
    return n
