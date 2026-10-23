#!/usr/bin/env python3
"""Adversarial QA for adsops.protocol: JSON-RPC / stdio robustness on the real tool surface.

Not a unit test: a fuzzer.  It builds one real deployment in a temp dir (config +
desired-state + policy + plan ledger + heartbeat + fake git runner) with a simulated
Google Ads account, wraps it with tools.build_server(), then drives protocol.Server
through server.handle() and server.serve() with hostile JSON-RPC.

INVARIANTS (checked for every case):
  * no exception escapes handle()/serve()
  * every stdout line is exactly one strict-JSON JSON-RPC 2.0 object
    (jsonrpc=="2.0", has "id", exactly one of result/error, no NaN/Infinity)
  * nothing else is written to stdout (no stray print)
  * responses never contain "Traceback" or 'File "'
  * no file is created or written outside the temp deployment dir

Read-only: never touches adsops/.  Prints ``QA: N checks, M failures`` plus one block
per failure.

Run: python3 tests/qa_protocol.py
"""
import builtins
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adsops import config, safety, tools  # noqa: E402

N = 0
FAILS = []


def check(cond, label, detail=""):
    global N
    N += 1
    if not cond:
        FAILS.append((label, detail))
        print("FAIL [%s] %s" % (label, detail))


# ── fake deployment: same shape as tests/qa_invariants.py ───────────────────────────────────
class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class SimAccount:
    """A tiny in-memory campaign behind the client interface engine.execute expects."""

    def __init__(self):
        self.customer_id, self.campaign_id, self.name = "1112223333", 42, "c42"
        self.budget_micros, self.status, self.end = 8_000_000, "ENABLED", "2035-01-01"
        self.spend_life, self.spend_today = 100.0, 3.0
        self.negatives, self.records = set(), []

    def search(self, gaql):
        q = str(gaql)
        if "campaign_criterion" in q:
            return [{"campaignCriterion": {"keyword": {"text": t, "matchType": m}}}
                    for t, m in sorted(self.negatives)]
        if "search_term_view" in q:
            return [{"searchTermView": {"searchTerm": "core term near me"},
                     "segments": {"keyword": {"info": {"text": "core term"}}},
                     "metrics": {"clicks": "2", "costMicros": "1500000"}}]
        return [self._row(self.spend_today if "TODAY" in q else self.spend_life)]

    def _row(self, cost):
        return {"campaign": {"id": "42", "name": self.name, "status": self.status,
                             "endDateTime": self.end + " 23:59:59",
                             "campaignBudget": "customers/%s/campaignBudgets/9" % self.customer_id},
                "campaignBudget": {"amountMicros": str(self.budget_micros), "explicitlyShared": False},
                "metrics": {"costMicros": str(int(round(cost * 1e6)))}}

    def mutate(self, service, ops, validate_only, partial=False):
        ops = list(ops or [])
        self.records.append({"service": service, "validate_only": bool(validate_only), "ops": ops})
        if validate_only:
            return {"results": [{} for _ in ops]}
        for entry in ops:
            if service == "campaignBudgets":
                self.budget_micros = int((entry.get("update") or {}).get("amountMicros") or self.budget_micros)
            elif service == "campaigns":
                upd = entry.get("update") or {}
                if upd.get("status"):
                    self.status = upd["status"]
                for k in ("endDateTime", "endDate"):
                    if k in upd:
                        self.end = str(upd[k])[:10]
            elif service == "campaignCriteria":
                kw = (entry.get("create") or {}).get("keyword") or {}
                self.negatives.add((str(kw.get("text", "")).strip().lower(),
                                    str(kw.get("matchType", "PHRASE")).upper()))
        return {"results": [{} for _ in ops]}


def build(tmp):
    state_path = os.path.join(tmp, "c42.json")
    policy_path = os.path.join(tmp, "c42.policy.json")
    cfg_path = os.path.join(tmp, "cfg.json")
    guards = {"max_end_date": "2035-12-31", "min_daily_budget_usd": 5, "max_daily_budget_usd": 20,
              "lifetime_spend_pause_usd": 500, "daily_spend_pause_usd": 25, "max_ops_per_run": 10}
    policy = {"customer_id": "111-222-3333", "campaign_id": 42, "allow_enable": True,
              "require_plan_id": True, "max_guard_age_min": 30, "plan_ttl_s": 600,
              "protected_terms": ["core term"], "guards": dict(guards)}
    state = {"customer_id": "111-222-3333", "campaign_id": 42, "campaign_name": "c42",
             "status": "ENABLED", "allow_enable": True, "end_date": "2035-01-01",
             "daily_budget_usd": 10, "negatives_add": [],
             "guards": dict(guards, max_daily_budget_usd=30)}      # looser on purpose; policy wins
    for path, obj in ((state_path, state), (policy_path, policy)):
        with open(path, "w") as fh:
            json.dump(obj, fh)
    cfg_d = {"tenant": "qa", "meter_log": os.path.join(tmp, "meter.jsonl"),
             "audit_log": os.path.join(tmp, "audit.jsonl"),
             "plan_ledger": os.path.join(tmp, "plans.jsonl"),
             "lock_dir": os.path.join(tmp, "locks"),
             "guard_heartbeat": os.path.join(tmp, "guard.heartbeat"),
             "credentials": {"google_ads": {"type": "static", "token": "x"}},
             "commit_policy": {"require_clean_git": True, "allow_commit": True, "allow_set": True},
             "google_ads": {"end_field": "end_date_time"},
             "accounts": [{"key": "acct", "platform": "google_ads", "customer_id": "111-222-3333",
                           "campaigns": [{"key": "c42", "state_file": state_path,
                                          "policy_file": policy_path}]}]}
    with open(cfg_path, "w") as fh:
        json.dump(cfg_d, fh)
    cfg = config.load_config(cfg_path)
    sim = SimAccount()
    clock = [1_800_000_000.0]

    def runner(cmd, cwd=None, **kw):                      # fake git: tracked, clean, commits fine
        cmd = list(cmd)
        if "ls-files" in cmd:
            return R(0, state_path + "\n")
        if "--show-toplevel" in cmd:
            return R(0, tmp + "\n")
        if len(cmd) > 1 and cmd[1] == "rev-parse":
            return R(0, "f00dcafe\n")
        return R(0)

    safety.write_heartbeat(os.path.join(tmp, "guard.heartbeat"), now=lambda: clock[0])
    return tools.build_server(cfg, client_factory=lambda account: sim,
                              git_runner=runner, now=lambda: clock[0])


# ── filesystem containment tracer ────────────────────────────────────────────────────────────
class WriteTrace:
    def __init__(self, root):
        self.root, self.hits = os.path.realpath(root), []

    def _rec(self, p):
        try:
            self.hits.append(os.path.realpath(os.fspath(p)))
        except TypeError:
            pass

    def __enter__(self):
        self._open, self._osopen, self._mkdir = builtins.open, os.open, os.mkdir

        def open_(file, mode="r", *a, **k):
            if isinstance(file, (str, bytes, os.PathLike)) and any(c in str(mode) for c in "wax+"):
                self._rec(file)
            return self._open(file, mode, *a, **k)

        def osopen(path, flags, *a, **k):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
                self._rec(path)
            return self._osopen(path, flags, *a, **k)

        def mkdir(path, *a, **k):
            self._rec(path)
            return self._mkdir(path, *a, **k)

        builtins.open, os.open, os.mkdir = open_, osopen, mkdir
        return self

    def __exit__(self, *exc):
        builtins.open, os.open, os.mkdir = self._open, self._osopen, self._mkdir

    def outside(self):
        return sorted({p for p in self.hits
                       if p != self.root and not p.startswith(self.root + os.sep)})


# ── JSON-RPC invariants ──────────────────────────────────────────────────────────────────────
def _no_const(x):
    raise ValueError("non-JSON constant %r" % (x,))


def strict_loads(s):
    return json.loads(s, parse_constant=_no_const)


def _same_id(a, b):
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    return a == b


def check_envelope(obj, label, where):
    check(isinstance(obj, dict), label, "%s: not a JSON object: %.120r" % (where, obj))
    if not isinstance(obj, dict):
        return
    check(obj.get("jsonrpc") == "2.0", label, "%s: jsonrpc=%r" % (where, obj.get("jsonrpc")))
    check("id" in obj, label, "%s: response carries no id" % where)
    check(("result" in obj) != ("error" in obj), label, "%s: needs exactly one of result/error" % where)
    blob = json.dumps(obj, default=str)
    check("Traceback" not in blob and 'File "' not in blob, label,
          "%s: response leaks internals: %s" % (where, blob[:160]))


def check_response(resp, msg, label):
    if not isinstance(msg, dict):
        check(isinstance(resp, dict) and resp.get("id") is None
              and (resp.get("error") or {}).get("code") == -32600, label,
              "non-object request must yield -32600/id null, got %.160r" % (resp,))
        if isinstance(resp, dict):
            check_envelope(resp, label, "handle")
        return
    method = msg.get("method")
    if "id" not in msg or (isinstance(method, str) and method.startswith("notifications/")):
        check(resp is None, label, "expected no response, got %.160r" % (resp,))
        return
    check(isinstance(resp, dict), label, "expected a JSON-RPC response, got %.160r" % (resp,))
    if not isinstance(resp, dict):
        return
    check_envelope(resp, label, "handle")
    rid = msg.get("id")
    # JSON-RPC 2.0: an id must be a string, a finite number or null; otherwise the answer is Invalid Request, id null
    if not (rid is None or (isinstance(rid, (str, int, float)) and not isinstance(rid, bool) and rid == rid
                            and rid not in (float("inf"), float("-inf")))):
        check(resp.get("id") is None and (resp.get("error") or {}).get("code") == -32600, label,
              "invalid id %.60r must get Invalid Request with id null, got %.160r" % (rid, resp))
        return
    check(_same_id(resp.get("id"), rid), label,
          "response id %.60r != request id %.60r" % (resp.get("id"), rid))


def call_handle(srv, msg, label):
    buf, old = io.StringIO(), sys.stdout
    sys.stdout, exc, resp = buf, None, None
    try:
        resp = srv.handle(msg)
    except BaseException as e:                            # invariant: nothing escapes
        exc = e
    finally:
        sys.stdout = old
    check(exc is None, label, "handle() raised %s: %s" % (type(exc).__name__, exc))
    check(buf.getvalue() == "", label, "handle() wrote to stdout: %.120r" % buf.getvalue())
    return resp, exc


def run_serve(srv, raw, label, expect_ids=None):
    out, err, old = io.StringIO(), io.StringIO(), sys.stdout
    sys.stdout, exc = err, None
    try:
        srv.serve(stdin=io.StringIO(raw), stdout=out)
    except BaseException as e:
        exc = e
    finally:
        sys.stdout = old
    where = "serve[%s]" % label
    check(exc is None, label, "%s: serve() raised %s: %s" % (where, type(exc).__name__, exc))
    check(err.getvalue() == "", label, "%s: wrote to the real stdout: %.120r" % (where, err.getvalue()))
    body = out.getvalue()
    check(body == "" or body.endswith("\n"), label, "%s: stdout does not end with a newline" % where)
    objs = []
    for i, line in enumerate(body.splitlines()):
        try:
            obj, bad = strict_loads(line), None
        except ValueError as e:
            obj, bad = None, str(e)
        check(bad is None, label, "%s line %d is not one strict JSON value: %.120r (%s)"
              % (where, i, line, bad))
        if bad is None:
            check_envelope(obj, label, "%s line %d" % (where, i))
            objs.append(obj)
    if exc is None and expect_ids is not None:
        got = [o.get("id") if isinstance(o, dict) else "<not-object>" for o in objs]
        check(len(got) == len(expect_ids) and all(_same_id(a, b) for a, b in zip(got, expect_ids)),
              label, "%s: ids %r != expected %r" % (where, got, expect_ids))
    return objs


# ── hostile inputs ───────────────────────────────────────────────────────────────────────────
def tc(mid, name, args):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def _deep(n):
    d = cur = {}
    for _ in range(n):
        nxt = {}
        cur["n"] = nxt
        cur = nxt
    return d


WEIRD = "caf\u00e9 \U0001f600 \x00\x01\x1b[31m ../../etc/passwd"
BIG = "A" * 1_000_000 + "-1MB"
DEEP = _deep(5000)

HANDLE_CASES = [
    ("initialize with client protocolVersion",
     {"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
    ("initialize with default protocolVersion",
     {"jsonrpc": "2.0", "id": 101, "method": "initialize", "params": {}}),
    ("initialize twice",
     {"jsonrpc": "2.0", "id": 102, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}),
    ("initialize with list params", {"jsonrpc": "2.0", "id": 103, "method": "initialize", "params": [1, 2]}),
    ("initialize with null params", {"jsonrpc": "2.0", "id": 104, "method": "initialize", "params": None}),
    ("ping", {"jsonrpc": "2.0", "id": 105, "method": "ping"}),
    ("ping with id null", {"jsonrpc": "2.0", "id": None, "method": "ping"}),
    ("ping with string id", {"jsonrpc": "2.0", "id": "id-as-string", "method": "ping"}),
    ("ping with float id", {"jsonrpc": "2.0", "id": 1.5, "method": "ping"}),
    ("ping with object id", {"jsonrpc": "2.0", "id": {"a": [1, None], "b": True}, "method": "ping"}),
    ("ping with array id", {"jsonrpc": "2.0", "id": [1, 2, 3], "method": "ping"}),
    ("ping with NaN id", {"jsonrpc": "2.0", "id": float("nan"), "method": "ping"}),
    ("notification without id", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ("notification cancelled", {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}),
    ("notification carrying an id", {"jsonrpc": "2.0", "id": 106, "method": "notifications/initialized"}),
    ("missing method", {"jsonrpc": "2.0", "id": 107}),
    ("method null", {"jsonrpc": "2.0", "id": 108, "method": None}),
    ("method empty string", {"jsonrpc": "2.0", "id": 109, "method": ""}),
    ("method int", {"jsonrpc": "2.0", "id": 110, "method": 5}),
    ("method array", {"jsonrpc": "2.0", "id": 111, "method": ["tools/list"]}),
    ("method object", {"jsonrpc": "2.0", "id": 112, "method": {"m": 1}}),
    ("unknown method", {"jsonrpc": "2.0", "id": 113, "method": "resources/list"}),
    ("tools/list", {"jsonrpc": "2.0", "id": 114, "method": "tools/list"}),
    ("list params on ping", {"jsonrpc": "2.0", "id": 115, "method": "ping", "params": [1, 2]}),
    ("string params on tools/call", {"jsonrpc": "2.0", "id": 116, "method": "tools/call", "params": "oops"}),
    ("null params on tools/call", {"jsonrpc": "2.0", "id": 117, "method": "tools/call", "params": None}),
    ("unknown tool", {"jsonrpc": "2.0", "id": 118, "method": "tools/call",
                      "params": {"name": "no_such_tool"}}),
    ("non-string tool name", {"jsonrpc": "2.0", "id": 119, "method": "tools/call", "params": {"name": 5}}),
    ("tools/call without name", {"jsonrpc": "2.0", "id": 120, "method": "tools/call", "params": {}}),
    ("arguments as array", tc(121, "adsops_status", [1, 2])),
    ("arguments as string", tc(122, "adsops_status", "c42")),
    ("arguments null", tc(123, "adsops_status", None)),
    ("extra property", tc(124, "adsops_status", {"campaign": "c42", "evil": 1})),
    ("empty campaign", tc(125, "adsops_status", {"campaign": ""})),
    ("path traversal campaign", tc(126, "adsops_status", {"campaign": "../../etc/passwd"})),
    ("unicode/NUL/control campaign", tc(127, "adsops_status", {"campaign": WEIRD})),
    ("1MB campaign", tc(128, "adsops_status", {"campaign": BIG})),
    ("1MB reason", tc(129, "adsops_set", {"campaign": "c42", "reason": BIG})),
    ("5000-deep argument", tc(130, "adsops_status", {"campaign": "c42", "deep": DEEP})),
    ("budget 1e308", tc(131, "adsops_set",
                        {"campaign": "c42", "reason": "qa fuzz budget", "daily_budget_usd": 1e308})),
    ("budget -0.0", tc(132, "adsops_set",
                       {"campaign": "c42", "reason": "qa fuzz budget", "daily_budget_usd": -0.0})),
    ("budget huge int", tc(133, "adsops_set",
                           {"campaign": "c42", "reason": "qa fuzz budget", "daily_budget_usd": 10 ** 400})),
    ("budget bool", tc(134, "adsops_set",
                       {"campaign": "c42", "reason": "qa fuzz budget", "daily_budget_usd": True})),
    ("budget string", tc(135, "adsops_set",
                         {"campaign": "c42", "reason": "qa fuzz budget", "daily_budget_usd": "10.0"})),
    ("budget 12.0 (writes the state file)", tc(136, "adsops_set",
                                               {"campaign": "c42", "reason": "qa fuzz 12", "daily_budget_usd": 12.0})),
    ("status PAUSED (writes the state file)", tc(137, "adsops_set",
                                                 {"campaign": "c42", "reason": "qa fuzz pause", "status": "PAUSED"})),
    ("status injection", tc(138, "adsops_set",
                            {"campaign": "c42", "reason": "qa fuzz bad status", "status": "ENABLED\nPAUSED"})),
    ("end_date zero", tc(139, "adsops_set",
                         {"campaign": "c42", "reason": "qa fuzz bad date", "end_date": "0000-00-00"})),
    ("end_date beyond policy max", tc(140, "adsops_set",
                                      {"campaign": "c42", "reason": "qa fuzz end", "end_date": "2036-01-01"})),
    ("negative blocking a protected term", tc(141, "adsops_set",
                                              {"campaign": "c42", "reason": "qa fuzz neg",
                                               "negatives_add": [{"text": "core"}]})),
    ("jsonrpc 1.0", {"jsonrpc": "1.0", "id": 142, "method": "ping"}),
    ("no jsonrpc field", {"id": 143, "method": "ping"}),
    ("campaigns with extra property", tc(144, "adsops_campaigns", {"x": 1})),
    ("terms days bool", tc(145, "adsops_terms", {"campaign": "c42", "days": True})),
    ("terms days overflow", tc(146, "adsops_terms", {"campaign": "c42", "days": 10 ** 9})),
    ("plan unknown campaign", tc(147, "adsops_plan", {"campaign": WEIRD})),
    ("guard commit string", tc(148, "adsops_guard", {"campaign": "c42", "commit": "yes"})),
    ("apply commit without plan_id", tc(149, "adsops_apply", {"campaign": "c42", "commit": True})),
    ("request is a bare string", "hello"),
    ("request is a bare number", 42),
    ("request is an array", [1, 2, 3]),
    ("request is null", None),
    ("request is true", True),
]

S1 = ["{not json", "not json at all", "{", "]}+", "[1, 2, 3]", "42", '"hello"', "null", "", "   ",
      '{"jsonrpc":"2.0","id":21,"method":"ping"}',
      '{"jsonrpc":"2.0","method":"notifications/initialized"}',
      '{"jsonrpc":"2.0","id":"abc","method":"ping"}',
      '{"jsonrpc":"2.0","id":null,"method":"ping"}',
      '{"jsonrpc":"2.0","id":22,"method":"tools/list"}',
      '{"jsonrpc":"2.0","id":23,"method":"tools/call","params":{"name":"no_such_tool"}}',
      '{"jsonrpc":"2.0","id":24,"method":"ping"}']
S2 = ['{"jsonrpc":"2.0","id":31,"method":"tools/call","params":[1,2]}',
      '{"jsonrpc":"2.0","id":32,"method":"tools/call","params":"x"}',
      '{"jsonrpc":"2.0","id":33,"method":"tools/call","params":null}',
      '{"jsonrpc":"2.0","id":34,"method":"tools/call","params":{"name":"adsops_status","arguments":[1,2]}}',
      '{"jsonrpc":"2.0","id":35,"method":"tools/call","params":{"name":"adsops_status","arguments":"c42"}}',
      '{"jsonrpc":"2.0","id":36,"method":"tools/call","params":{"name":"adsops_status","arguments":null}}',
      json.dumps(tc(37, "adsops_set", {"campaign": "c42", "reason": BIG})),
      json.dumps(tc(38, "adsops_status", {"campaign": "c42", "deep": DEEP})),
      json.dumps(tc(39, "adsops_status", {"campaign": WEIRD})),
      json.dumps(tc(40, "adsops_plan", {"campaign": "c42"})),
      '{"jsonrpc":"2.0","id":41,"method":"ping","extra":"ignored"}']

SERVE_CASES = [
    ("malformed/array/scalar lines then good requests", "\n".join(S1) + "\n",
     [None] * 8 + [21, "abc", None, 22, 23, 24]),
    ("hostile params/arguments, big and deep payloads", "\n".join(S2) + "\n", list(range(31, 42))),
    ("initialize twice + ping, CRLF, no trailing newline",
     '{"jsonrpc":"2.0","id":51,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}\r\n'
     '{"jsonrpc":"2.0","id":52,"method":"initialize","params":{}}\r\n'
     '{"jsonrpc":"2.0","id":53,"method":"ping"}', [51, 52, 53]),
    ("non-string method line followed by ping",
     '{"jsonrpc":"2.0","id":61,"method":5}\n{"jsonrpc":"2.0","id":62,"method":"ping"}\n', [61, 62]),
    ("NaN id line (strict JSON check)", '{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n', None),
    ("empty stdin", "", []),
]


def main():
    tmp = tempfile.mkdtemp(prefix="adsops-qa-proto-")
    try:
        srv = build(tmp)
        with WriteTrace(tmp) as wt:
            for label, msg in HANDLE_CASES:
                resp, exc = call_handle(srv, msg, label)
                if exc is None:
                    check_response(resp, msg, label)
            for label, raw, ids in SERVE_CASES:
                run_serve(srv, raw, label, ids)
            bad = wt.outside()
            check(not bad, "filesystem containment: writes stay inside the temp dir",
                  "wrote outside the temp dir: %r" % (bad[:5],))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("")
    print("QA: %d checks, %d failures" % (N, len(FAILS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
