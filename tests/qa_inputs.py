#!/usr/bin/env python3
"""Adversarial QA for the adsops file/value parsers.

Read-only: imports adsops, never edits it. Every hostile input is fed through
temp files and the result is classified. INVARIANT under test:

    every bad input must raise adsops.errors.ToolError (incl. RailViolation),
    never TypeError/ValueError/KeyError/AttributeError/RecursionError, and never
    be silently accepted when that silence would let a budget exceed the policy
    maximum or an invalid date pass validation.

Run: cd <repo root> && python3 tests/qa_inputs.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adsops.errors import RailViolation, ToolError  # noqa: E402
from adsops.policy import load_policy  # noqa: E402
from adsops.engine import load_state, plan  # noqa: E402
from adsops.statestore import apply_changes  # noqa: E402
from adsops.config import load_config  # noqa: E402

NAN, INF = float("nan"), float("inf")
TODAY = "2026-10-12"

N = 0                     # checks run
FAILS: list[tuple] = []   # (section, name, expected, actual, severity, note)
TD = tempfile.mkdtemp(prefix="adsops-qa-")


def short(v, n=110):
    s = repr(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "..."


def _file(name, payload, mode="w"):
    p = os.path.join(TD, name)
    if isinstance(payload, (bytes, bytearray)):
        with open(p, "wb") as fh:
            fh.write(payload)
    else:
        with open(p, mode, encoding="utf-8") as fh:
            fh.write(payload)
    return p


def _json(name, obj):
    return _file(name, json.dumps(obj))          # json.dumps emits NaN/Infinity tokens


# ── recorders ────────────────────────────────────────────────────────────────────────────────
def want_error(section, name, fn, sev="high", note=""):
    """A hostile input: any outcome other than ToolError/RailViolation is a failure."""
    global N
    N += 1
    try:
        out = fn()
    except (ToolError, RailViolation):
        return True
    except BaseException as e:                                  # noqa: BLE001
        FAILS.append((section, name, "ToolError or RailViolation",
                      f"{type(e).__name__}: {short(str(e), 90)}", sev, note))
        return False
    FAILS.append((section, name, "ToolError or RailViolation",
                  f"ACCEPTED -> {short(out)}", "critical", note or "silently accepted"))
    return False


def want_clean(section, name, fn, sev="medium", note=""):
    """Either refuse with ToolError/RailViolation or return; a foreign exception is a bug."""
    global N
    N += 1
    try:
        fn()
        return True
    except (ToolError, RailViolation):
        return True
    except BaseException as e:                                  # noqa: BLE001
        FAILS.append((section, name, "ToolError/RailViolation or a clean load",
                      f"{type(e).__name__}: {short(str(e), 90)}", sev, note))
        return False


def want_ok(section, name, fn, pred=None, note=""):
    """A legitimate input that must NOT be rejected."""
    global N
    N += 1
    try:
        out = fn()
    except BaseException as e:                                  # noqa: BLE001
        FAILS.append((section, name, "accepted", f"{type(e).__name__}: {short(str(e), 90)}",
                      "medium", note))
        return False
    if pred is not None and not pred(out):
        FAILS.append((section, name, "accepted", f"accepted but {short(out)}", "medium", note))
        return False
    return True


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────
def base_policy():
    return {"customer_id": "123-456-7890", "campaign_id": 555,
            "guards": {"max_daily_budget_usd": 50, "max_end_date": "2026-12-31",
                       "lifetime_spend_pause_usd": 400, "daily_spend_pause_usd": 20,
                       "max_ops_per_run": 10}}


def pol(**over):
    p = base_policy()
    for k, v in over.items():
        if k.startswith("g_"):
            p["guards"][k[2:]] = v
        else:
            p[k] = v
    return p


def state(**over):
    s = {"customer_id": "123-456-7890", "campaign_id": 9876543210,
         "campaign_name": "acme_search", "negatives_add": [],
         "guards": {"max_daily_budget_usd": 30, "min_daily_budget_usd": 5,
                    "max_end_date": "2026-11-30", "lifetime_spend_pause_usd": 300,
                    "daily_spend_pause_usd": 50, "max_ops_per_run": 60}}
    s.update(over)
    return s


def live(**over):
    l = {"id": "9876543210", "name": "acme_search", "status": "ENABLED",
         "end": "2026-10-14", "budget_rn": "customers/1234567890/campaignBudgets/1",
         "budget_usd": 25.0, "budget_shared": False, "spend_life": 40.0,
         "spend_today": 5.0, "negatives": [("tenant", "BROAD")]}
    l.update(over)
    return l


def cfg(**over):
    c = {"tenant": "example-agency",
         "accounts": [{"key": "acme", "platform": "google_ads", "customer_id": "000-000-0000",
                       "campaigns": [{"key": "acme_search", "state_file": "~/ads/a.json"}]}]}
    c.update(over)
    return c


def st_state():
    return {"customer_id": "1", "campaign_id": 2, "campaign_name": "c", "status": "PAUSED",
            "daily_budget_usd": 8, "negatives_add": [{"text": "free", "match_type": "PHRASE"}],
            "guards": {"max_daily_budget_usd": 25}}


# ═══ 1. policy.load_policy — file level ══════════════════════════════════════════════════════
P = "policy.load_policy"
good = _json("ok.json", base_policy())
want_ok(P, "valid baseline policy loads", lambda: load_policy(good))
want_error(P, "missing file", lambda: load_policy(os.path.join(TD, "absent.json")))
want_error(P, "empty file (0 bytes)", lambda: load_policy(_file("empty.json", "")))
want_error(P, "whitespace-only file", lambda: load_policy(_file("ws.json", "   \n\t\n")))
want_error(P, "top-level JSON array", lambda: load_policy(_json("arr.json", [1, 2, 3])))
want_error(P, "top-level JSON string", lambda: load_policy(_json("str.json", "policy")))
want_error(P, "top-level JSON null", lambda: load_policy(_json("null.json", None)))
want_error(P, "truncated JSON", lambda: load_policy(_file("trunc.json", '{"customer_id": "1",')))
want_error(P, "trailing garbage after object",
           lambda: load_policy(_file("trail.json", json.dumps(base_policy()) + " }")))
want_clean(P, "UTF-8 BOM prefixed policy JSON",
           lambda: load_policy(_file("bom.json",
                                     b"\xef\xbb\xbf" + json.dumps(base_policy()).encode())),
           note="BOM is a common editor artefact; refusing it is fine, crashing is not")
want_error(P, "invalid UTF-8 bytes", lambda: load_policy(
    _file("badutf.json", b'{"customer_id": "\xff\xfe\xfd", "campaign_id": 1}')))
want_error(P, "invalid UTF-8 in an otherwise valid policy", lambda: load_policy(
    _file("badutf2.json", json.dumps(base_policy()).encode() + b'\xff')))
want_error(P, "deeply nested JSON (200k brackets)",
           lambda: load_policy(_file("deep.json", "[" * 200000)))
want_error(P, "50MB file of garbage",
           lambda: load_policy(_file("big.json", b"x" * (50 * 1024 * 1024))))
want_error(P, "directory instead of file", lambda: load_policy(TD))
want_error(P, "policy path is None (API misuse)", lambda: load_policy(None), sev="low")

# ═══ 2. policy.load_policy — value level ═════════════════════════════════════════════════════
for label, v in (("empty string", ""), ("blank string", "   "), ("int", 123),
                 ("None", None), ("bool", True), ("list", ["1"])):
    want_error(P, f"customer_id = {label}", lambda v=v, l=label: load_policy(_json(f"cid_{l}.json", pol(customer_id=v))))
for label, v in (("bool", True), ("float", 5.5), ("empty string", ""), ("None", None),
                 ("list", [555]), ("dict", {})):
    want_error(P, f"campaign_id = {label}", lambda v=v, l=label: load_policy(_json(f"camp_{l}.json", pol(campaign_id=v))))

# NaN / Infinity / -Infinity / overflow — raw JSON tokens, which json.load accepts by default
for label, v in (("NaN", NAN), ("Infinity", INF), ("-Infinity", -INF), ("1e309 (overflow->inf)", float("1e309"))):
    want_error(P, f"guards.max_daily_budget_usd = {label}",
               lambda v=v, l=label: load_policy(_json(f"maxb_{l}.json", pol(g_max_daily_budget_usd=v))),
               sev="critical",
               note="a non-finite cap disables the budget ceiling entirely")
    want_error(P, f"guards.daily_spend_pause_usd = {label}",
               lambda v=v, l=label: load_policy(_json(f"dsp_{l}.json", pol(g_daily_spend_pause_usd=v))),
               sev="critical", note="non-finite spend cap never fires")
    want_error(P, f"guards.lifetime_spend_pause_usd = {label}",
               lambda v=v, l=label: load_policy(_json(f"lsp_{l}.json", pol(g_lifetime_spend_pause_usd=v))),
               sev="critical", note="non-finite lifetime cap never fires")
    want_error(P, f"guards.min_daily_budget_usd = {label}",
               lambda v=v, l=label: load_policy(_json(f"minb_{l}.json", pol(g_min_daily_budget_usd=v))),
               sev="high", note="NaN bypasses the min<=max comparison")

for label, v in (("negative", -1), ("negative float", -0.5), ("string", "50"),
                 ("bool", True), ("None", None), ("list", [50])):
    want_error(P, f"guards.max_daily_budget_usd = {label}",
               lambda v=v, l=label: load_policy(_json(f"neg_{l}.json", pol(g_max_daily_budget_usd=v))))
want_error(P, "guards.min_daily_budget_usd > max", lambda: load_policy(
    _json("minmax.json", pol(g_min_daily_budget_usd=60))))
want_error(P, "guards missing entirely", lambda: load_policy(_json("nog.json", {k: v for k, v in base_policy().items() if k != "guards"})))
want_error(P, "guards is a list", lambda: load_policy(_json("glist.json", pol(guards=["x"]))))
want_error(P, "guards.max_ops_per_run = True", lambda: load_policy(_json("ops_bool.json", pol(g_max_ops_per_run=True))))
want_error(P, "guards.max_ops_per_run = 2.5", lambda: load_policy(_json("ops_f.json", pol(g_max_ops_per_run=2.5))))
want_error(P, "guards.max_ops_per_run = -1", lambda: load_policy(_json("ops_neg.json", pol(g_max_ops_per_run=-1))))

for label, v in (("13th month", "2026-13-01"), ("Feb 30", "2026-02-30"),
                 ("Feb 29 non-leap", "2026-02-29"), ("slash form", "2026/12/31"),
                 ("digits only", 20261231), ("padded", " 2026-12-31"),
                 ("empty", ""), ("None", None)):
    want_error(P, f"guards.max_end_date = {label}",
               lambda v=v, l=label: load_policy(_json(f"end_{l}.json", pol(g_max_end_date=v))))
want_ok(P, "guards.max_end_date = 2024-02-29 (real leap day)",
        lambda: load_policy(_json("leap.json", pol(g_max_end_date="2024-02-29"))),
        pred=lambda c: c["guards"]["max_end_date"] == "2024-02-29")

for ttl, ok in ((59, False), (60, True), (86400, True), (86401, False),
                (0, False), (-1, False), (900.0, False), (True, False), ("900", False), (None, False)):
    fn = lambda ttl=ttl: load_policy(_json(f"ttl_{ttl}.json", pol(plan_ttl_s=ttl)))
    if ok:
        want_ok(P, f"plan_ttl_s = {ttl!r} (in range)", fn)
    else:
        want_error(P, f"plan_ttl_s = {ttl!r} (out of range / wrong type)", fn)

for label, v in (("int", 10), ("empty string", ""), ("nested list", [["a"]]),
                 ("None entry", [None]), ("bool", [True])):
    want_error(P, f"protected_terms = {label}",
               lambda v=v, l=label: load_policy(_json(f"prot_{l}.json", pol(protected_terms=v))))
want_ok(P, "protected_terms = 5000 valid strings", lambda: load_policy(
    _json("prot_big.json", pol(protected_terms=[f"term{i}" for i in range(5000)]))))
for label, v in (("string 'true'", "true"), ("int 1", 1), ("int 0", 0), ("None", None)):
    want_error(P, f"allow_enable = {label}", lambda v=v, l=label: load_policy(_json(f"ae_{l}.json", pol(allow_enable=v))))
for label, v in (("1", 1), ("'true'", "true"), ("None", None)):
    want_error(P, f"require_plan_id = {label}", lambda v=v, l=label: load_policy(_json(f"rpid_{l}.json", pol(require_plan_id=v))))
for label, v in (("0", 0), ("1.5", 1.5), ("-3", -3), ("'15'", "15"), ("True", True)):
    want_error(P, f"max_guard_age_min = {label}", lambda v=v, l=label: load_policy(_json(f"age_{l}.json", pol(max_guard_age_min=v))))

# ═══ 3. engine.load_state + engine.plan ══════════════════════════════════════════════════════
E = "engine.load_state/plan"
want_ok(E, "valid state loads", lambda: load_state(_json("s_ok.json", state())))
want_error(E, "state file missing", lambda: load_state(os.path.join(TD, "no_state.json")))
want_error(E, "state is empty file", lambda: load_state(_file("s_empty.json", "")))
want_error(E, "state is top-level list", lambda: load_state(_json("s_list.json", [1])))
want_error(E, "state missing campaign_name", lambda: load_state(
    _json("s_missing.json", {k: v for k, v in state().items() if k != "campaign_name"})))
want_error(E, "state guards not an object", lambda: load_state(_json("s_g.json", state(guards=[]))))
want_error(E, "state invalid UTF-8", lambda: load_state(_file("s_utf.json", b'{"a": "\xff\xfe"}')))
want_error(E, "state UTF-8 BOM", lambda: load_state(
    _file("s_bom.json", b"\xef\xbb\xbf" + json.dumps(state()).encode())))

for label, v in (("string '10'", "10"), ("0", 0), ("-1", -1), ("1e-9", 1e-9),
                 ("NaN", NAN), ("infinite", INF), ("huge 1e12", 1e12), ("bool True", True),
                 ("list", [10]), ("None", None)):
    # The composed invariant: whatever load_state lets through, plan must refuse cleanly.
    p = _json(f"s_budget_{label.replace(' ', '_').replace('/', '_')}.json", state(daily_budget_usd=v))
    want_error(E, f"load_state+plan with daily_budget_usd = {label}", lambda p=p: plan(load_state(p), live(), today=TODAY),
               sev="critical" if label in ("NaN",) else "high",
               note="a malformed budget must be refused, never crash or vanish as a silent no-op")

for label, v in (("int 5", 5), ("string", "abc"), ("dict", {"a": 1})):
    p = _json(f"s_neg_{label.replace(' ', '_')}.json", state(negatives_add=v))
    want_error(E, f"load_state+plan with negatives_add = {label}", lambda p=p: plan(load_state(p), live(), today=TODAY))
for label, v in (("2026-13-01", "2026-13-01"), ("2026-02-30", "2026-02-30"),
                 ("2026-10-32 (future, impossible)", "2026-10-32"),
                 ("2026-00-00", "2026-00-00"), ("padded ' 2026-11-30'", " 2026-11-30")):
    p = _json(f"s_end_{label.split(' ')[0]}.json", state(end_date=v))
    want_error(E, f"load_state+plan with end_date = {label}", lambda p=p: plan(load_state(p), live(), today=TODAY),
               sev="high", note="an impossible calendar date must not reach a mutate payload")
for label, v in (("unknown FUZZY", "FUZZY"), ("EXACT with space", "EXACT "), ("int 3", 3), ("empty", "")):
    p = _json(f"s_mt_{label.replace(' ', '_')}.json", state(negatives_add=[{"text": "x", "match_type": v}]))
    want_error(E, f"load_state+plan with match_type = {label}", lambda p=p: plan(load_state(p), live(), today=TODAY))


def no_budget_op(label, budget):
    """engine.plan with a non-finite budget must never emit a campaigns/campaignBudgets payload."""
    global N
    N += 1
    try:
        ops, _ = plan(state(daily_budget_usd=budget), live(), today=TODAY)
    except (ToolError, RailViolation):
        return True
    except BaseException as e:                                  # noqa: BLE001
        FAILS.append((E, f"plan with budget {label}", "no ops or clean refusal",
                      f"{type(e).__name__}: {short(str(e), 90)}", "high", ""))
        return False
    if "campaignBudgets" in ops:
        FAILS.append((E, f"plan with budget {label}", "no campaigns/campaignBudgets payload",
                      f"emitted {short(ops)}", "critical", "non-finite budget written to the API"))
        return False
    return True


no_budget_op("NaN", NAN)
no_budget_op("-Infinity", -INF)
no_budget_op("+Infinity", INF)

# ═══ 4. statestore.apply_changes ═════════════════════════════════════════════════════════════
S = "statestore.apply_changes"
want_ok(S, "valid change set", lambda: apply_changes(st_state(), {"daily_budget_usd": 12.345, "status": "ENABLED"}),
        pred=lambda r: r[0]["daily_budget_usd"] == 12.35)
for label, v in (("string '10'", "10"), ("0", 0), ("-1", -1), ("bool True", True),
                 ("1e-9 (rounds to 0.00)", 1e-9), ("NaN", NAN), ("infinite", INF),
                 ("list", [10])):
    want_error(S, f"daily_budget_usd = {label}", lambda v=v: apply_changes(st_state(), {"daily_budget_usd": v}),
               sev="critical" if label in ("NaN", "infinite") else "high",
               note="non-finite / zero budgets must not be written into the state file")
for label, v in (("lowercase", "enabled"), ("REMOVED", "REMOVED"), ("UNKNOWN", "UNKNOWN"),
                 ("None", None), ("int", 1)):
    want_error(S, f"status = {label}", lambda v=v: apply_changes(st_state(), {"status": v}))
for label, v in (("2026-13-01", "2026-13-01"), ("2026-02-30", "2026-02-30"),
                 ("padded ' 2026-11-30'", " 2026-11-30"), ("2026-1-5", "2026-1-5"),
                 ("slash form", "11/30/2026"), ("None", None), ("int", 20261130)):
    want_error(S, f"end_date = {label}", lambda v=v: apply_changes(st_state(), {"end_date": v}),
               sev="high" if label in ("2026-13-01", "2026-02-30") else "medium",
               note="regex-only date check accepts impossible calendar dates")
for label, v in (("int 5", 5), ("None", None), ("string 'abc'", "abc"), ("dict", {"a": 1})):
    want_error(S, f"negatives_add = {label}", lambda v=v: apply_changes(st_state(), {"negatives_add": v}))
for label, v in (("dict without text", {"match_type": "PHRASE"}), ("text only whitespace", {"text": "   "}),
                 ("text only newlines", {"text": "\n\t\n"}), ("text empty", {"text": ""}),
                 ("entry not a dict", "free"), ("text None", {"text": None})):
    want_error(S, f"negatives_add entry = {label}",
               lambda v=v: apply_changes(st_state(), {"negatives_add": [v]}))
for label, v in (("unknown FUZZY", "FUZZY"), ("EXACT with trailing space", "EXACT "), ("int 3", 3),
                 ("empty", ""), ("lower PHRASE ok?", "phrase")):
    want_error(S, f"negatives_add match_type = {label}",
               lambda v=v: apply_changes(st_state(), {"negatives_add": [{"text": "x", "match_type": v}]}),
               sev="high" if label.startswith(("unknown", "EXACT")) else "medium",
               note="an invalid match type is committed to git and only rejected at plan time")
want_ok(S, "negatives_add unicode + 1000 chars",
        lambda: apply_changes(st_state(), {"negatives_add": [{"text": "é" * 500}, {"text": "ü" * 1000}]}),
        pred=lambda r: len(r[0]["negatives_add"]) == 3)
want_error(S, "changes = None", lambda: apply_changes(st_state(), None))
want_error(S, "changes = 5", lambda: apply_changes(st_state(), 5))
want_error(S, "changes is a list of non-settable", lambda: apply_changes(st_state(), ["status", "guards"]))
want_error(S, "guard edit via changes", lambda: apply_changes(st_state(), {"guards": {"max_daily_budget_usd": 9999}}))

# ═══ 5. config.load_config ═══════════════════════════════════════════════════════════════════
C = "config.load_config"
want_ok(C, "valid config loads", lambda: load_config(_json("c_ok.json", cfg())))
want_error(C, "missing config file", lambda: load_config(os.path.join(TD, "no_cfg.json")))
want_error(C, "config empty file", lambda: load_config(_file("c_empty.json", "")))
want_error(C, "config top-level array", lambda: load_config(_json("c_arr.json", [cfg()])))
want_error(C, "config invalid UTF-8", lambda: load_config(_file("c_utf.json", b'{"tenant":"\xff\xfe"}')))
want_error(C, "tenant is a number", lambda: load_config(_json("c_ten.json", cfg(tenant=7))))
want_error(C, "tenant missing", lambda: load_config(_json("c_ten2.json", {"accounts": cfg()["accounts"]})))
want_error(C, "accounts empty list", lambda: load_config(_json("c_acc0.json", cfg(accounts=[]))))
want_error(C, "accounts not a list", lambda: load_config(_json("c_accs.json", cfg(accounts="acme"))))
want_error(C, "account without key", lambda: load_config(_json("c_akey.json", cfg(
    accounts=[{"platform": "google_ads", "customer_id": "1", "campaigns": []}]))))
want_error(C, "unknown platform tiktok", lambda: load_config(_json("c_plat.json", cfg(
    accounts=[{"key": "a", "platform": "tiktok", "customer_id": "1", "campaigns": []}]))))
want_error(C, "campaign key duplicated across accounts", lambda: load_config(_json("c_dup.json", cfg(
    accounts=[{"key": "a1", "platform": "google_ads", "customer_id": "1",
               "campaigns": [{"key": "dup", "state_file": "~/1.json"}]},
              {"key": "a2", "platform": "google_ads", "customer_id": "2",
               "campaigns": [{"key": "dup", "state_file": "~/2.json"}]}]))))
want_error(C, "campaign key duplicated inside one account", lambda: load_config(_json("c_dup2.json", cfg(
    accounts=[{"key": "a1", "platform": "google_ads", "customer_id": "1",
               "campaigns": [{"key": "dup", "state_file": "~/1.json"},
                             {"key": "dup", "state_file": "~/2.json"}]}]))))
want_error(C, "duplicate account key (two 'acme' accounts)", lambda: load_config(_json("c_dupacct.json", cfg(
    accounts=[{"key": "acme", "platform": "google_ads", "customer_id": "000-000-0000",
               "campaigns": [{"key": "c1", "state_file": "~/1.json"}]},
              {"key": "acme", "platform": "google_ads", "customer_id": "999-999-9999",
               "campaigns": [{"key": "c2", "state_file": "~/2.json"}]}]))),
           sev="medium", note="account keys are never deduplicated; two accounts can share a key")
want_error(C, "campaign missing state_file", lambda: load_config(_json("c_nosf.json", cfg(
    accounts=[{"key": "a", "platform": "google_ads", "customer_id": "1",
               "campaigns": [{"key": "c"}]}]))))
want_error(C, "campaign state_file non-string", lambda: load_config(_json("c_sfn.json", cfg(
    accounts=[{"key": "a", "platform": "google_ads", "customer_id": "1",
               "campaigns": [{"key": "c", "state_file": 5}]}]))))
want_error(C, "commit_policy.allow_commit = 'yes'", lambda: load_config(_json("c_cp.json", cfg(
    commit_policy={"allow_commit": "yes"}))))
want_error(C, "credentials.google_ads.sources not a list", lambda: load_config(_json("c_cred.json", cfg(
    credentials={"google_ads": {"type": "service_account", "sources": "sa.json"}}))))
want_ok(C, "state_file with ~ and ..", lambda: load_config(_json("c_paths.json", cfg(
    accounts=[{"key": "a", "platform": "google_ads", "customer_id": "1",
               "campaigns": [{"key": "c", "state_file": "~/../ads/./x.json"}]}]))))
want_ok(C, "huge protected_terms + plan_ttl_s boundary in policy", lambda: load_policy(
    _json("p_bound.json", pol(plan_ttl_s=86400, protected_terms=[f"t{i}" for i in range(200)]))),
    pred=lambda c: c["plan_ttl_s"] == 86400)


# ═══ 6. composed exploits: does the loosened parser actually move money? ═════════════════════
X = "composed"
from adsops.policy import effective_state  # noqa: E402


def inf_cap_lets_huge_budget_through():
    """policy cap = Infinity -> effective_state -> plan emits a $1,000,000 budget."""
    pol_path = _json("x_pol_inf.json", pol(g_max_daily_budget_usd=INF))
    policy = load_policy(pol_path)
    st = state(daily_budget_usd=1_000_000.0)
    eff = effective_state(st, policy)
    return plan(eff, live(budget_usd=25.0), today=TODAY)


want_error(X, "policy max_daily_budget_usd=Infinity lets a $1,000,000 budget pass plan",
           inf_cap_lets_huge_budget_through, sev="critical",
           note="operator-visible impact of the non-finite cap: the ceiling is simply gone")


def impossible_date_reaches_payload_via_statestore():
    """apply_changes accepts 2026-02-30, commits it, and plan then mutates with that date."""
    new, _ = apply_changes(state(end_date="2026-11-01"), {"end_date": "2026-02-30"})
    return plan(new, live(), today="2026-01-05")      # max_end_date guard is 2026-11-30


want_error(X, "apply_changes writes end_date=2026-02-30 and plan emits it",
           impossible_date_reaches_payload_via_statestore, sev="critical",
           note="regex-only date validation + string compare = impossible date sent to the API")


def nan_budget_written_then_vanishes():
    """NaN written by apply_changes is a silent plan no-op: the requested change is lost."""
    new, _ = apply_changes(state(), {"daily_budget_usd": NAN})
    ops, _ = plan(new, live(), today=TODAY)
    if "campaignBudgets" not in ops:
        raise RailViolation("silent no-op")            # classify as the refusal that is missing
    return ops


want_error(X, "NaN budget round-trips through apply_changes and then plan silently ignores it",
           nan_budget_written_then_vanishes, sev="high",
           note="state file carries NaN; plan sees abs(nan-x)>0.005 == False and drops the change")


# ═══ report ══════════════════════════════════════════════════════════════════════════════════
def main():
    print(f"QA: {N} checks, {len(FAILS)} failures")
    for i, (section, name, expected, actual, sev, note) in enumerate(FAILS, 1):
        print(f"[{i:02d}] FAIL ({sev}) {section}: {name}")
        print(f"     expected: {expected}")
        print(f"     actual  : {actual}")
        if note:
            print(f"     note    : {note}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
