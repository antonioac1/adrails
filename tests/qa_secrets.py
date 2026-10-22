#!/usr/bin/env python3
"""Adversarial QA: hunt for canary credential leakage across every adsops path.

Run from the repo root:  python3 tests/qa_secrets.py
Prints 'QA: N checks, M leaks' followed by one line per leak (and INFO lines for the
user-supplied-reason echoes the brief allows).

Nothing here edits the package. Canaries are injected through: a fake client, the real urllib
transport (with urllib.request.urlopen monkeypatched), 1Password/openssl credential flows,
service-account JSON loading, StaticToken, state/policy files and git output. Every JSON-RPC
response, the audit/meter/plan-ledger files, every file written under the case tmpdir, the git
commit messages, captured stdout/stderr and every exception message are scanned.
"""
import base64
import io
import json
import os
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adsops import auth, config, tools                       # noqa: E402
from adsops.errors import RailViolation, ToolError, scrub    # noqa: E402
from adsops.platforms import google_ads                      # noqa: E402

# -- canaries --------------------------------------------------------------------------------
C_ACCESS = "ya29.CANARYtoken_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
C_KEY = "-----BEGIN PRIVATE KEY-----\nCANARYKEYMATERIALxyz\n-----END PRIVATE KEY-----"
C_OP = "ops_CANARYSERVICEACCOUNTTOKEN0123456789abcdef"
C_BEARER = "Bearer CANARYBEARER.abc.def"
C_BLOB = base64.b64encode(b"CANARYBLOB" + b"Q" * 35).decode()          # 60 chars

CANARIES = {"access_token": C_ACCESS, "private_key": C_KEY, "op_service_token": C_OP,
            "bearer_header": C_BEARER, "b64_blob": C_BLOB}
MARKERS = {"access_token": "CANARYtoken", "private_key": "CANARYKEYMATERIAL",
           "op_service_token": "CANARYSERVICEACCOUNTTOKEN", "bearer_header": "CANARYBEARER",
           "b64_blob": C_BLOB}
ALL = "\n".join(CANARIES.values())

CHECKS = 0
LEAKS = []
LATENT = []
INFOS = []
_SEEN = set()


def _snip(text, idx):
    return text[max(0, idx - 80):idx + 90].replace("\n", "\\n").replace("\r", " ")


def scan(text, scenario, where, severity="HIGH", kind="secret-egress"):
    """Scan *text* for every canary and record any appearance as a leak."""
    global CHECKS
    CHECKS += 1
    if not isinstance(text, str):
        try:
            text = json.dumps(text, default=str)
        except Exception:
            text = repr(text)
    for name, marker in MARKERS.items():
        idx = text.find(marker)
        if idx < 0:
            continue
        key = (scenario, name, where)
        if key in _SEEN:
            continue
        _SEEN.add(key)
        entry = {"scenario": scenario, "canary": name, "where": where,
                 "severity": severity, "kind": kind, "snippet": _snip(text, idx)}
        (LATENT if kind == "latent-chain" else LEAKS).append(entry)


def note_info(scenario, where, text):
    """A canary the user themselves typed into a reason field: allowed, reported separately."""
    global CHECKS
    CHECKS += 1
    hits = [n for n, m in MARKERS.items() if m in text]
    if not hits:
        return
    key = (scenario, "INFO", where)
    if key in _SEEN:
        return
    _SEEN.add(key)
    INFOS.append({"scenario": scenario, "canaries": hits, "where": where})


def scan_exception(fn, scenario, where):
    """Call fn(); scan every exception message in its chain (never our own source lines)."""
    global CHECKS
    try:
        fn()
    except BaseException as e:                                    # noqa: BLE001
        scan(type(e).__name__ + ": " + str(e) + "\n" + repr(e), scenario, where)
        # chained causes: never emitted by adsops (no traceback printer), but a caller that
        # formats the full chain would surface them -> reported as LATENT, not as egress.
        cur, seen = (e.__cause__ or e.__context__), {id(e)}
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            scan(type(cur).__name__ + ": " + str(cur) + "\n" + repr(cur), scenario,
                 where + " [chained cause]", severity="MEDIUM", kind="latent-chain")
            cur = cur.__cause__ or cur.__context__
        return str(e)
    CHECKS += 1
    return None


# -- fixtures --------------------------------------------------------------------------------
STATE = {
    "customer_id": "111-222-3333", "campaign_id": 42, "campaign_name": "c42",
    "status": "ENABLED", "allow_enable": True, "end_date": "2099-12-31", "daily_budget_usd": 10,
    "negatives_add": [{"text": "free", "match_type": "PHRASE"}],
    "guards": {"max_end_date": "2099-12-31", "min_daily_budget_usd": 5, "max_daily_budget_usd": 25,
               "lifetime_spend_pause_usd": 500, "daily_spend_pause_usd": 25, "max_ops_per_run": 10},
}

POLICY = {
    "customer_id": "111-222-3333", "campaign_id": 42, "allow_enable": True,
    "require_plan_id": False, "max_guard_age_min": None, "plan_ttl_s": 600,
    "guards": {"max_end_date": "2099-12-31", "min_daily_budget_usd": 5, "max_daily_budget_usd": 25,
               "lifetime_spend_pause_usd": 500, "daily_spend_pause_usd": 25, "max_ops_per_run": 10},
}


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Git:
    """Fake git runner: records every command and every commit message it is handed."""

    def __init__(self, root, state_file, dirty="", sha="f00dcafe", commit_err=""):
        self.root, self.state_file, self.dirty, self.sha, self.commit_err = (
            root, state_file, dirty, sha, commit_err)
        self.messages, self.calls = [], []

    def __call__(self, cmd, cwd=None, **kw):
        self.calls.append(list(cmd))
        if "ls-files" in cmd:
            return R(0, self.state_file + "\n")
        if "--show-toplevel" in cmd:
            return R(0, self.root + "\n")
        if len(cmd) > 1 and cmd[1] == "rev-parse":
            return R(0, self.sha + "\n")
        if len(cmd) > 1 and cmd[1] == "commit" and "--only" in cmd:
            if self.commit_err:
                return R(1, "", self.commit_err)
            self.messages.append(cmd[cmd.index("-m") + 1])
            return R(0)
        if len(cmd) > 1 and cmd[1] in ("add", "commit"):
            return R(0)
        return R(0, self.dirty)


class FakeClient:
    """Google Ads client double: controllable live state + injectable failures."""

    def __init__(self, spend_today=3.0, spend_life=100.0, budget=8.0, status="ENABLED",
                 end="2099-12-31 23:59:59", negatives=(("free", "PHRASE"),),
                 search_error=None, mutate_error=None):
        self.spend_today, self.spend_life, self.budget = spend_today, spend_life, budget
        self.status, self.end, self.negatives = status, end, list(negatives)
        self.search_error, self.mutate_error = search_error, mutate_error
        self.calls = []

    def search(self, q):
        self.calls.append(("search", q))
        if self.search_error:
            raise self.search_error()
        if "campaign_criterion" in q:
            return [{"campaignCriterion": {"keyword": {"text": t, "matchType": m}}}
                    for t, m in self.negatives]
        if "search_term_view" in q:
            return [{"searchTermView": {"searchTerm": "pm near me"},
                     "segments": {"keyword": {"info": {"text": "pm"}}},
                     "metrics": {"clicks": "3", "costMicros": "4500000"}}]
        micros = self.spend_today if "TODAY" in q else self.spend_life
        return [{"campaign": {"id": "42", "name": "c42", "status": self.status, "endDateTime": self.end,
                              "campaignBudget": "customers/1112223333/campaignBudgets/9"},
                 "campaignBudget": {"amountMicros": str(int(self.budget * 1e6)),
                                    "explicitlyShared": False},
                 "metrics": {"costMicros": str(int(micros * 1e6))}}]

    def mutate(self, svc, ops, validate_only, partial=False):
        self.calls.append(("mutate", svc, validate_only))
        if self.mutate_error and not validate_only:
            raise self.mutate_error()
        return {"results": [{}] * len(ops)}


class Case:
    """One isolated deployment: tmp dir, state file, config, fake git, live server."""

    def __init__(self, name, client_factory=None, credentials=None, policy=None,
                 allow_commit=True, allow_set=True, require_clean_git=True, git=None):
        self.name = name
        self.tmp = tempfile.mkdtemp(prefix="adsops-qa-")
        self.inputs = set()
        self.poisoned = set()
        self._scanned = set()
        self.reasons = []
        self.state_path = os.path.join(self.tmp, "c42.json")
        with open(self.state_path, "w") as fh:
            json.dump(STATE, fh, indent=1)
        self.inputs.add(self.state_path)
        cfg = {
            "tenant": "qa",
            "meter_log": os.path.join(self.tmp, "meter.jsonl"),
            "audit_log": os.path.join(self.tmp, "audit.jsonl"),
            "plan_ledger": os.path.join(self.tmp, "plans.jsonl"),
            "lock_dir": os.path.join(self.tmp, "locks"),
            "guard_heartbeat": os.path.join(self.tmp, "guard.heartbeat"),
            "commit_policy": {"allow_commit": allow_commit, "allow_set": allow_set,
                              "require_clean_git": require_clean_git},
            "accounts": [{"key": "acct", "platform": "google_ads", "customer_id": "111-222-3333",
                          "campaigns": [{"key": "c42", "state_file": self.state_path}]}],
        }
        if credentials is not None:
            cfg["credentials"] = credentials
        if policy is not None:
            pp = os.path.join(self.tmp, "c42.policy.json")
            with open(pp, "w") as fh:
                json.dump(policy, fh)
            self.inputs.add(pp)
            cfg["accounts"][0]["campaigns"][0]["policy_file"] = pp
            self.policy_path = pp
        cp = os.path.join(self.tmp, "cfg.json")
        with open(cp, "w") as fh:
            json.dump(cfg, fh)
        self.inputs.add(cp)
        self.cfg = config.load_config(cp)
        self.git = git or Git(self.tmp, self.state_path)
        self.client = (client_factory or (lambda a: FakeClient()))(
            {"key": "acct", "platform": "google_ads", "customer_id": "111-222-3333"})
        self.server = tools.build_server(self.cfg, client_factory=lambda a: self.client,
                                         git_runner=self.git, now=time.time)

    def call(self, tool, args=None):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": args or {}}})
        scan(json.dumps(resp, default=str), self.name, "jsonrpc response: %s" % tool)
        if isinstance(args, dict) and isinstance(args.get("reason"), str):
            self.reasons.append(args["reason"])
        return resp

    # -- artifact sweeps ------------------------------------------------------------------
    def _scan_file(self, label, path):
        real = os.path.abspath(path)
        if real in self.poisoned or real in self._scanned:
            return
        self._scanned.add(real)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            return
        text = raw
        for r in self.reasons:                       # user-authored reason text is allowed
            text = text.replace(r, "[USER-REASON]")
        for r in self.reasons:
            if r and r in raw:
                note_info(self.name, "%s (user reason field)" % label, r)
        scan(text, self.name, label)

    def close(self):
        self._scan_file("audit_log", self.cfg["audit_log"])
        self._scan_file("meter_log", self.cfg["meter_log"])
        self._scan_file("plan_ledger", self.cfg["plan_ledger"])
        self._scan_file("state_file", self.state_path)
        for dirpath, _dirs, files in os.walk(self.tmp):
            for f in files:
                p = os.path.join(dirpath, f)
                if p in self.inputs:
                    continue
                self._scan_file("tmp:%s" % os.path.relpath(p, self.tmp), p)
        for msg in self.git.messages:
            red = msg
            for r in self.reasons:
                red = red.replace(r, "[USER-REASON]")
            scan(red, self.name, "git commit message")
            for r in self.reasons:
                if r and r in msg:
                    note_info(self.name, "git commit message (reason field)", msg)


# -- cases -----------------------------------------------------------------------------------
def case_scrub_unit():
    """Sanity: scrub() must redact every canary, otherwise nothing below proves anything."""
    scan(scrub(ALL, 5000), "scrub-unit", "scrub() output")
    scan(scrub(json.dumps({"private_key": C_KEY}), 5000), "scrub-unit", "scrub() of JSON key")
    scan(scrub("Authorization: " + C_BEARER, 5000), "scrub-unit", "scrub() of bearer header")
    for name, val in CANARIES.items():
        scan(scrub(val, 5000), "scrub-unit", "scrub() of %s" % name)


def case_client_toolerror():
    """A client that raises ToolError carrying every canary reaches every tool."""
    c = Case("client-toolerror",
             client_factory=lambda a: FakeClient(
                 search_error=lambda: ToolError("upstream said: " + ALL)))
    for tool in ("adsops_status", "adsops_terms", "adsops_plan", "adsops_apply", "adsops_guard"):
        c.call(tool, {"campaign": "c42"})
    c.call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12,
                          "reason": "rotate the credential before the next cycle"})
    c.close()
    return c


def case_client_runtimeerror():
    """A non-ToolError exception with canaries in its text."""
    c = Case("client-runtimeerror",
             client_factory=lambda a: FakeClient(
                 search_error=lambda: RuntimeError("socket died: " + ALL)))
    c.call("adsops_status", {"campaign": "c42"})
    c.call("adsops_plan", {"campaign": "c42"})
    c.close()
    return c


def case_commit_failure():
    """A failing commit mutate: _commit audits str(e) and reconciles."""
    c = Case("commit-failure",
             client_factory=lambda a: FakeClient(mutate_error=lambda: ToolError("HTTP 503: " + ALL)))
    c.call("adsops_apply", {"campaign": "c42", "commit": True})
    c.close()
    return c


def case_validate_only_failure():
    """Even the validateOnly leg can fail with a canary-laden upstream error."""
    class AlwaysFail(FakeClient):
        def mutate(self, svc, ops, validate_only, partial=False):
            raise ToolError("HTTP 400 invalid argument: " + ALL)

    c = Case("validate-only-failure", client_factory=lambda a: AlwaysFail())
    c.call("adsops_apply", {"campaign": "c42"})
    c.call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12, "apply": True,
                          "reason": "raise the budget after review of the terms report"})
    c.close()
    return c


def case_state_identity_echo():
    """State-file fields carrying canaries are echoed by plan identity checks."""
    c = Case("state-identity-echo")
    c.poisoned.add(os.path.abspath(c.state_path))
    st = json.load(open(c.state_path))
    st["campaign_name"] = C_ACCESS
    json.dump(st, open(c.state_path, "w"))
    c.call("adsops_plan", {"campaign": "c42"})
    c.close()
    return c


def case_git_output():
    """Git output (sha, commit stderr) carrying canaries flows back through the tool."""
    g = Git(tempfile.mkdtemp(prefix="adsops-qa-git-"), "/placeholder/c42.json", sha=C_BLOB)
    c = Case("git-sha-canary", git=g)
    g.state_file = c.state_path
    c.call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12,
                          "reason": "routine budget raise for the week"})
    c.close()
    g2 = Git(tempfile.mkdtemp(prefix="adsops-qa-git-"), "/placeholder/c42.json",
             commit_err="fatal: " + C_OP)
    c2 = Case("git-stderr-canary", git=g2)
    g2.state_file = c2.state_path
    c2.call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12,
                           "reason": "routine budget raise for the week"})
    c2.close()
    return c, c2


def case_static_token_urllib():
    """Real urllib transport: urlopen raises HTTPError/URLError echoing auth headers + canaries."""
    creds = {"google_ads": {"type": "static", "token": C_ACCESS}}

    def factory(account):
        return google_ads.GoogleAdsClient(account["customer_id"], auth.StaticToken(C_ACCESS),
                                          developer_token=C_BLOB)

    c = Case("urllib-httperror-echo", credentials=creds, client_factory=factory)
    real = urllib.request.urlopen

    def echo(req, timeout=None):
        body = json.dumps({
            "error": {"code": 401, "status": "UNAUTHENTICATED",
                      "message": "Request had invalid authentication credentials.",
                      "details": {"headers": dict(req.headers), "token": C_ACCESS,
                                  "key": C_KEY, "op": C_OP, "developer": C_BLOB}}})
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body.encode()))

    urllib.request.urlopen = echo
    try:
        c.call("adsops_status", {"campaign": "c42"})
        c.call("adsops_plan", {"campaign": "c42"})
    finally:
        urllib.request.urlopen = real

    def bad_json(req, timeout=None):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return ("<html>proxy error: " + ALL + "</html>").encode()
        return Resp()

    c2 = Case("urllib-badjson", credentials=creds, client_factory=factory)
    urllib.request.urlopen = bad_json
    try:
        c2.call("adsops_status", {"campaign": "c42"})
    finally:
        urllib.request.urlopen = real

    c3 = Case("urllib-urlerror", credentials=creds, client_factory=factory)

    def net_down(req, timeout=None):
        raise urllib.error.URLError("Name or service not known: " + C_BEARER + " " + C_OP)

    urllib.request.urlopen = net_down
    try:
        c3.call("adsops_status", {"campaign": "c42"})
    finally:
        urllib.request.urlopen = real

    c4 = Case("urllib-direct", credentials=creds, client_factory=factory)

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                     io.BytesIO(("Authorization: " + C_BEARER).encode()))
    urllib.request.urlopen = boom
    try:
        scan_exception(lambda: google_ads.urllib_http(
            "POST", google_ads.BASE + "/v24/customers/1/googleAds:search",
            {"Authorization": "Bearer " + C_ACCESS, "developer-token": C_BLOB}, {}),
            "urllib-direct", "urllib_http ToolError message")
    finally:
        urllib.request.urlopen = real
    st = auth.StaticToken(C_ACCESS)
    scan(repr(st), "static-token", "StaticToken repr")
    scan_exception(lambda: auth.StaticToken(5), "static-token", "StaticToken(bad) error")
    for cc in (c, c2, c3, c4):
        cc.close()
    return (c, c2, c3, c4)


def case_upstream_data_echo():
    """Google returns campaign/term text containing canaries: how far does it travel?"""
    c = Case("upstream-data-echo", client_factory=lambda a: FakeClient(budget=8.0))
    c.client.negatives = [(C_OP, "PHRASE")]
    c.call("adsops_status", {"campaign": "c42"})
    c.close()
    return c


def case_sa_file_canary():
    """Service-account JSON whose private_key is the canary and which fails to load."""
    tmp = tempfile.mkdtemp(prefix="adsops-qa-sa-")
    bad = os.path.join(tmp, "sa-broken.json")
    with open(bad, "w") as fh:
        fh.write('{"type": "service_account", "private_key": "' + C_KEY.replace("\n", "\\n") + '", OOPS}')
    good = os.path.join(tmp, "sa-good.json")
    with open(good, "w") as fh:
        json.dump({"type": "service_account", "client_email": "sa@example",
                   "private_key": C_KEY}, fh)
    missing_key = os.path.join(tmp, "sa-nokey.json")
    with open(missing_key, "w") as fh:
        json.dump({"type": "service_account", "client_email": "sa@example"}, fh)

    creds = {"google_ads": {"type": "service_account",
                            "sources": [{"kind": "file", "path": bad}]}}
    c = Case("sa-file-broken-json", credentials=creds)
    c.call("adsops_status", {"campaign": "c42"})
    c.close()

    creds2 = {"google_ads": {"type": "service_account",
                             "sources": [{"kind": "file", "path": missing_key}]}}
    c2 = Case("sa-file-missing-key", credentials=creds2)
    real_refresh = auth._google_refresher

    def failing_refresher(info, scope):
        raise RuntimeError("google-auth rejected: " + ALL)

    auth._google_refresher = failing_refresher
    try:
        c2.call("adsops_status", {"campaign": "c42"})
    finally:
        auth._google_refresher = real_refresh
    c2.close()

    scan_exception(auth.sa_file_loader(bad), "sa-loader", "sa_file_loader(broken) ToolError")
    scan_exception(auth.sa_file_loader(os.path.join(tmp, "nope.json")), "sa-loader",
                   "sa_file_loader(missing) ToolError")
    scan_exception(auth.ServiceAccount(auth.sa_file_loader(good),
                                       refresher=lambda i, s: (_ for _ in ()).throw(RuntimeError(ALL))).access_token,
                   "sa-loader", "ServiceAccount refresher error")
    scan_exception(auth.ServiceAccount(auth.sa_file_loader(good),
                                       refresher=lambda i, s: (None, None)).access_token,
                   "sa-loader", "ServiceAccount empty token error")
    return c, c2


def case_onepassword():
    """1Password loader: canaries in stderr, stdout, token file, exec failure."""
    tmp = tempfile.mkdtemp(prefix="adsops-qa-op-")
    tok_path = os.path.join(tmp, "op-token")
    with open(tok_path, "w") as fh:
        fh.write(C_OP + "\n")

    def runner_stderr(argv, **kw):
        return R(2, "", "error: authorization failed for token " + C_OP + " " + C_BEARER + " " + C_BLOB)

    scan_exception(auth.onepassword_loader("op://Vault/Item/credential", tok_path, op_bin="/fake/op",
                                           runner=runner_stderr),
                   "onepassword", "onepassword stderr canary")

    def runner_stdout(argv, **kw):
        return R(0, C_KEY + "\n", "")

    scan_exception(auth.onepassword_loader("op://Vault/Item/credential", tok_path, op_bin="/fake/op",
                                           runner=runner_stdout),
                   "onepassword", "onepassword non-JSON stdout canary")

    def runner_explode(argv, **kw):
        raise OSError("cannot exec /fake/op while holding " + C_OP)

    scan_exception(auth.onepassword_loader("op://Vault/Item/credential", tok_path, op_bin="/fake/op",
                                           runner=runner_explode),
                   "onepassword", "onepassword exec failure canary")

    def runner_badjson(argv, **kw):
        return R(0, "not json: " + C_ACCESS, "")

    scan_exception(auth.onepassword_loader("op://Vault/Item/credential", tok_path, op_bin="/fake/op",
                                           runner=runner_badjson),
                   "onepassword", "onepassword invalid JSON canary")

    empty = os.path.join(tmp, "empty")
    open(empty, "w").close()
    scan_exception(auth.onepassword_loader("op://Vault/Item/credential", empty),
                   "onepassword", "onepassword empty token file")

    def runner_key(argv, **kw):
        return R(0, json.dumps({"type": "service_account", "private_key": C_KEY}), "")

    ld = auth.onepassword_loader("op://Vault/Item/credential", tok_path, op_bin="/fake/op",
                                 runner=runner_key)
    assert isinstance(ld(), dict)          # the loader's return value stays in-process
    scan_exception(auth.chain_loader(ld, lambda: (_ for _ in ()).throw(ToolError("x"))),
                   "onepassword", "chain_loader failure after a canary source")


def case_openssl():
    """Zero-dependency openssl token flow with canaries in the key and the failures."""
    info = {"client_email": "sa@example", "private_key": C_KEY}
    seen = {}

    class Bad:
        returncode, stdout, stderr = 1, b"", ("openssl failed: " + C_KEY).encode()

    def runner_fail(cmd, input=None, capture_output=None):
        seen["key_path"] = cmd[-1]
        return Bad()

    def http_canary(uri, body):
        raise RuntimeError("token endpoint said 400: " + ALL)

    def http_error_body(uri, body):
        return {"error": "invalid_grant", "error_description": C_ACCESS + " " + C_OP}

    scan_exception(lambda: auth._openssl_refresher(info, auth.SCOPE_GOOGLE_ADS, runner=runner_fail,
                                                   http=http_canary),
                   "openssl", "openssl signing failure w/ key in stderr")
    if "key_path" in seen and os.path.exists(seen["key_path"]):
        LEAKS.append({"scenario": "openssl", "canary": "private_key", "where": "temp key file left on disk",
                      "severity": "HIGH", "kind": "disk-residue", "snippet": seen["key_path"]})

    def runner_ok(cmd, input=None, capture_output=None):
        seen["key_path2"] = cmd[-1]

        class OK:
            returncode, stdout, stderr = 0, b"sig", b""
        return OK()

    scan_exception(lambda: auth._openssl_refresher(info, auth.SCOPE_GOOGLE_ADS, runner=runner_ok,
                                                   http=http_canary),
                   "openssl", "openssl token-exchange failure w/ canary body")
    scan_exception(lambda: auth._openssl_refresher(info, auth.SCOPE_GOOGLE_ADS, runner=runner_ok,
                                                   http=http_error_body),
                   "openssl", "openssl token endpoint error JSON w/ canary")

    def runner_explode(cmd, input=None, capture_output=None):
        raise OSError("spawn failed for key " + C_KEY)

    scan_exception(auth.ServiceAccount(lambda: info, refresher=lambda i, s: auth._openssl_refresher(
        i, s, runner=runner_explode, http=http_canary)).access_token,
        "openssl", "openssl runner OSError w/ key canary (via ServiceAccount)")
    scan_exception(auth.ServiceAccount(lambda: info, refresher=lambda i, s: auth._openssl_refresher(
        i, s, runner=runner_fail, http=http_canary)).access_token,
        "openssl", "ServiceAccount wrapping openssl failure")


def case_policy_and_reason():
    """User-supplied reason + negatives + protected_terms policy carrying canaries."""
    pol = dict(POLICY)
    pol["protected_terms"] = [C_ACCESS]
    c = Case("policy-protected-term", policy=pol)
    c.call("adsops_set", {"campaign": "c42", "negatives_add": [{"text": "near me"}],
                          "reason": "block a noisy close variant of a core term"})
    c.close()

    c2 = Case("user-reason-canary")
    c2.call("adsops_set", {"campaign": "c42", "daily_budget_usd": 12,
                           "reason": "operator pasted token " + C_ACCESS + " into the reason"})
    c2.close()

    c3 = Case("user-negatives-canary")
    c3.call("adsops_set", {"campaign": "c42", "negatives_add": [{"text": C_OP}],
                           "reason": "block a scraped junk term"})
    c3.call("adsops_plan", {"campaign": "c42"})
    c3.close()

    pol2 = dict(POLICY)
    pol2["customer_id"] = C_ACCESS
    c4 = Case("policy-bad-customer-id", policy=pol2)
    c4.call("adsops_status", {"campaign": "c42"})
    c4.close()
    return c, c2, c3, c4


def case_bad_state_file():
    """State file carrying canaries in odd places / not valid JSON at all."""
    c = Case("state-canary-fields")
    c.poisoned.add(os.path.abspath(c.state_path))
    st = json.load(open(c.state_path))
    st["negatives_add"] = [C_OP]
    json.dump(st, open(c.state_path, "w"))
    c.call("adsops_plan", {"campaign": "c42"})
    with open(c.state_path, "w") as fh:
        fh.write('{"private_key": "' + C_KEY.replace("\n", "\\n") + '", oops}')
    c.call("adsops_status", {"campaign": "c42"})
    c.close()
    return c


def main():
    out = io.StringIO()
    err = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    cases = []
    try:
        sys.stdout, sys.stderr = out, err
        for fn in (case_scrub_unit, case_client_toolerror, case_client_runtimeerror, case_commit_failure,
                   case_validate_only_failure, case_state_identity_echo, case_git_output, case_static_token_urllib, case_upstream_data_echo,
                   case_sa_file_canary, case_onepassword, case_openssl,
                   case_policy_and_reason, case_bad_state_file):
            try:
                res = fn()
            except Exception:                                  # a harness crash is a finding too
                scan(traceback.format_exc(), fn.__name__, "harness traceback")
                continue
            for item in (res if isinstance(res, tuple) else (res,)):
                if isinstance(item, Case):
                    cases.append(item)
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    scan(out.getvalue(), "capture", "captured sys.stdout")
    scan(err.getvalue(), "capture", "captured sys.stderr")

    print("QA: %d checks, %d leaks" % (CHECKS, len(LEAKS)))
    for l in LEAKS:
        print("LEAK  [%s] %-18s %s | %s | %s"
              % (l["severity"], l["canary"], l["scenario"], l["where"], l["snippet"]))
    for l in LATENT:
        print("LATENT[%s] %-18s %s | %s | %s"
              % (l["severity"], l["canary"], l["scenario"], l["where"], l["snippet"]))
    for i in INFOS:
        print("INFO  %s | %s | %s" % (",".join(i["canaries"]), i["scenario"], i["where"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
