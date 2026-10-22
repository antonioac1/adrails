"""Credential providers for adsops.

Nothing in this module touches the network, the filesystem or a subprocess at import time. Every
external effect is injectable (`info_loader=`, `runner=`, `now=`, `refresher=`) so tests never need
a real credential, the `op` CLI, or google-auth. No token or key is ever placed in an exception
message, a log line or a return value.
"""
import json
import os
import subprocess
import time

from .errors import ToolError, scrub

SCOPE_GOOGLE_ADS = "https://www.googleapis.com/auth/adwords"

_REDACTED = "[REDACTED]"


def _hide(text, secret=None):
    """Scrub anything credential-shaped, then hard-remove a specific known secret."""
    s = scrub(str(text), 300)
    if secret:
        tok = str(secret)
        if tok:
            s = s.replace(tok, _REDACTED)
    return s


# -- providers ---------------------------------------------------------------------------------
class TokenProvider:
    """Interface: hands out a short-lived OAuth2 access token."""

    def access_token(self):
        raise NotImplementedError


class StaticToken(TokenProvider):
    """Bring-your-own-token (tests, short-lived manual runs)."""

    def __init__(self, token):
        if not isinstance(token, str) or not token.strip():
            raise ToolError("static token must be a non-empty string")
        self._token = token.strip()

    def access_token(self):
        return self._token

    def __repr__(self):
        return "StaticToken(token=[REDACTED])"


def _google_refresher(info, scope):
    """Default refresher. The ONLY optional third-party import in adsops, done lazily here."""
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except Exception as e:  # pragma: no cover - depends on the host
        raise ToolError("google-auth is not installed: " + _hide(e))
    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=[scope])
        creds.refresh(Request())
    except Exception as e:
        raise ToolError("service-account token refresh failed: " + _hide(e))
    token = getattr(creds, "token", None)
    expiry = getattr(creds, "expiry", None)
    try:
        expiry_epoch = expiry.timestamp() if expiry is not None else time.time() + 3000
    except Exception:
        expiry_epoch = time.time() + 3000
    return token, expiry_epoch


class ServiceAccount(TokenProvider):
    """Service-account credential with a cached token (refreshed when expiry-60s is reached)."""

    def __init__(self, info_loader, scope=SCOPE_GOOGLE_ADS, now=time.time, refresher=None):
        if not callable(info_loader):
            raise ToolError("ServiceAccount needs a callable info_loader")
        self.info_loader = info_loader
        self.scope = scope
        self.now = now
        self.refresher = refresher or _google_refresher
        self._token = None
        self._expiry = 0.0

    def access_token(self):
        if self._token is not None and float(self.now()) < self._expiry - 60:
            return self._token
        info = self.info_loader()  # a ToolError from a loader propagates untouched
        if not isinstance(info, dict) or not info:
            raise ToolError("service-account info loader returned no credential object")
        try:
            pair = self.refresher(info, self.scope)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError("service-account token refresh failed: " + _hide(e))
        try:
            token, expiry = pair
        except Exception:
            raise ToolError("service-account refresher returned a malformed (token, expiry) pair")
        if not token:
            raise ToolError("service-account refresher returned no token")
        try:
            self._expiry = float(expiry)
        except (TypeError, ValueError):
            self._expiry = float(self.now()) + 3000
        self._token = str(token)
        return self._token

    def __repr__(self):
        return "ServiceAccount(scope=%r)" % (self.scope,)


# -- info loaders ------------------------------------------------------------------------------
def sa_file_loader(path):
    """Return a loader that reads a service-account JSON file (path expanded, read lazily)."""
    p = os.path.expanduser(str(path))
    if not p:
        raise ToolError("service-account file path is empty")

    def load():
        if not os.path.isfile(p):
            raise ToolError("service-account file not found: %s" % p)
        try:
            with open(p, encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception as e:
            raise ToolError("service-account file is not valid JSON: " + _hide(e))
        if not isinstance(info, dict) or not info:
            raise ToolError("service-account file does not contain a JSON object")
        return info

    load.kind = "file"
    load.path = p
    return load


def onepassword_loader(ref, token_file, op_bin="/opt/homebrew/bin/op", runner=subprocess.run):
    """Return a loader that reads `ref` from 1Password using a service-account token file."""
    ref_s = str(ref)
    token_path = os.path.expanduser(str(token_file))
    hint = "check the token/vault pair"

    def _fail(why):
        raise ToolError("1Password read of %s failed: %s - %s" % (ref_s, why, hint))

    def load():
        try:
            with open(token_path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except Exception as e:
            raise ToolError("1Password token file for %s is unreadable (%s) - %s: %s"
                            % (ref_s, token_path, hint, _hide(e)))
        if not token:
            raise ToolError("1Password token file for %s is empty (%s) - %s"
                            % (ref_s, token_path, hint))
        env = dict(os.environ)
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
        try:
            r = runner([op_bin, "read", ref_s], capture_output=True, text=True, env=env)
        except Exception as e:
            _fail("%s could not be executed: %s" % (op_bin, _hide(e, token)))
        rc = getattr(r, "returncode", 1)
        if rc != 0:
            err = _hide(getattr(r, "stderr", "") or "", token)
            _fail("rc=%s%s" % (rc, (": " + err) if err else ""))
        out = (getattr(r, "stdout", "") or "").strip()
        if not out:
            _fail("empty value")
        try:
            info = json.loads(out)
        except Exception:
            raise ToolError("1Password value for %s is not valid JSON - %s" % (ref_s, hint))
        if not isinstance(info, dict) or not info:
            raise ToolError("1Password value for %s is not a JSON object - %s" % (ref_s, hint))
        return info

    load.kind = "1password"
    load.ref = ref_s
    return load


def chain_loader(*loaders):
    """First loader that returns a usable dict wins; if all fail, ToolError naming the kinds tried."""
    loaders = [ld for ld in loaders if callable(ld)]

    def load():
        tried = []
        for i, ld in enumerate(loaders):
            tried.append(str(getattr(ld, "kind", None) or getattr(ld, "__name__", "loader%d" % (i + 1))))
            try:
                info = ld()
            except Exception:  # ToolError or not: a failing source just means "try the next one"
                continue
            if isinstance(info, dict) and info:
                return info
        raise ToolError("no credential source worked (tried: %s)" % ", ".join(tried or ["none"]))

    load.kind = "chain"
    return load


def from_config(cred):
    """Build a TokenProvider from a config `credentials.google_ads` block."""
    if not isinstance(cred, dict):
        raise ToolError("credentials must be a JSON object with a 'type'")
    kind = str(cred.get("type") or "").strip()
    if kind == "static":
        token = cred.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ToolError("credentials.token is required and must be a non-empty string "
                            "for type 'static'")
        return StaticToken(token)
    if kind == "service_account":
        sources = cred.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ToolError("credentials.sources must be a non-empty list for type "
                            "'service_account'")
        loaders = []
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                raise ToolError("credentials.sources[%d] must be an object" % i)
            skind = str(src.get("kind") or "").strip()
            if skind == "file":
                path = src.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise ToolError("credentials.sources[%d].path is required for kind 'file'" % i)
                loaders.append(sa_file_loader(path))
            elif skind == "1password":
                ref, tok = src.get("ref"), src.get("token_file")
                if not isinstance(ref, str) or not ref.strip():
                    raise ToolError("credentials.sources[%d].ref is required for kind '1password'" % i)
                if not isinstance(tok, str) or not tok.strip():
                    raise ToolError("credentials.sources[%d].token_file is required for kind "
                                    "'1password'" % i)
                loaders.append(onepassword_loader(ref, tok))
            else:
                raise ToolError("credentials.sources[%d].kind %r is not supported "
                                "(file|1password)" % (i, skind))
        return ServiceAccount(chain_loader(*loaders))
    if not kind:
        raise ToolError("credentials.type is required (static|service_account)")
    raise ToolError("credentials.type %r is not supported (static|service_account)" % (kind,))


# -- selftest ----------------------------------------------------------------------------------
def selftest():
    import tempfile

    n = 0

    def check(cond, label):
        nonlocal n
        assert cond, "auth.selftest: " + label
        n += 1

    def raises(fn, needle=None, forbid=None):
        try:
            fn()
        except Exception as e:
            text = str(e)
            if needle is not None:
                assert needle in text, "expected %r in error, got %r" % (needle, text)
            if forbid is not None:
                assert forbid not in text, "secret leaked into error text"
            return text
        raise AssertionError("expected an exception")

    # interface
    check(isinstance(StaticToken("abc"), TokenProvider), "StaticToken is a TokenProvider")
    raises(lambda: TokenProvider().access_token())

    # static token
    st = StaticToken("tok-123")
    check(st.access_token() == "tok-123", "static token returned")
    check("tok-123" not in repr(st), "static token repr hides the token")
    raises(lambda: StaticToken(""))

    # service-account caching with injected now/refresher
    clock = [1000.0]
    calls = []

    def refresher(info, scope):
        calls.append(scope)
        return "tok%d" % len(calls), clock[0] + 3600

    sa = ServiceAccount(lambda: {"client_email": "sa@example"}, now=lambda: clock[0],
                        refresher=refresher)
    check(sa.access_token() == "tok1", "first access_token refreshes")
    check(sa.access_token() == "tok1" and len(calls) == 1, "token cached on second call")
    check(calls == [SCOPE_GOOGLE_ADS], "scope passed to refresher")
    clock[0] = 1000.0 + 3600 - 61
    check(sa.access_token() == "tok1" and len(calls) == 1, "still cached just before expiry-60")
    clock[0] = 1000.0 + 3600 - 60
    check(sa.access_token() == "tok2" and len(calls) == 2, "refreshed at expiry-60s")

    # refresher failures are ToolError and scrubbed
    def bad_refresher(info, scope):
        raise RuntimeError("refresh rejected: Authorization: Bearer abc.def.ghi")

    sa_bad = ServiceAccount(lambda: {"k": 1}, refresher=bad_refresher)
    text = raises(sa_bad.access_token, needle="refresh failed")
    check("abc.def.ghi" not in text, "refresher error scrubbed")
    raises(lambda: ServiceAccount(lambda: {"k": 1}, refresher=lambda i, s: ("", 0.0)).access_token(),
           needle="no token")
    raises(lambda: ServiceAccount(lambda: None).access_token(), needle="no credential object")
    raises(lambda: ServiceAccount(lambda: {}).access_token(), needle="no credential object")
    raises(ServiceAccount(lambda: (_ for _ in ()).throw(ToolError("loader boom"))).access_token,
           needle="loader boom")

    # sa_file_loader: temp file + missing file
    td = tempfile.mkdtemp()
    good = os.path.join(td, "sa.json")
    with open(good, "w", encoding="utf-8") as fh:
        json.dump({"type": "service_account", "client_email": "sa@example"}, fh)
    ld = sa_file_loader(good)
    check(ld()["client_email"] == "sa@example", "sa_file_loader reads JSON")
    check(getattr(ld, "kind", None) == "file", "sa_file_loader kind tag")
    raises(sa_file_loader(os.path.join(td, "nope.json")), needle="service-account file not found")
    bad = os.path.join(td, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    raises(sa_file_loader(bad), needle="not valid JSON")

    # onepassword_loader with a fake runner
    token_path = os.path.join(td, "op-token")
    secret_token = "ops_SuperSecretValue_0123456789abcdef"
    with open(token_path, "w", encoding="utf-8") as fh:
        fh.write(secret_token + "\n")
    seen = {}

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_runner(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env", {})
        return R(0, json.dumps({"type": "service_account", "client_email": "op@example"}))

    op_ref = "op://Vault/Item/credential"
    op = onepassword_loader(op_ref, token_path, op_bin="/fake/op", runner=fake_runner)
    check(op()["client_email"] == "op@example", "onepassword loader parses JSON value")
    check(seen["argv"] == ["/fake/op", "read", op_ref], "onepassword argv")
    check(seen["env"].get("OP_SERVICE_ACCOUNT_TOKEN") == secret_token, "token passed via env only")
    check(getattr(op, "kind", None) == "1password", "onepassword kind tag")

    def failing_runner(argv, **kw):
        return R(2, "", "error: " + secret_token)

    op_bad = onepassword_loader(op_ref, token_path, op_bin="/fake/op", runner=failing_runner)
    text = raises(op_bad, needle=op_ref)
    check("check the token/vault pair" in text, "1password failure names the hint")
    check(secret_token not in text, "1password token never in the error")

    def exploding_runner(argv, **kw):
        raise OSError("no such binary " + secret_token)

    raises(onepassword_loader(op_ref, token_path, runner=exploding_runner),
           needle="could not be executed", forbid=secret_token)

    # chain_loader
    def first():
        raise ToolError("nope")

    def second():
        return {"client_email": "second@example"}

    chained = chain_loader(first, second)
    check(chained()["client_email"] == "second@example", "chain falls back to the next loader")
    text = raises(chain_loader(first, lambda: (_ for _ in ()).throw(ToolError("x"))),
                  needle="no credential source worked")
    check("first" in text, "chain lists the kinds it tried")
    check(chain_loader(sa_file_loader(good), first)()["client_email"] == "sa@example",
          "chain prefers the first loader that works")

    # from_config
    prov = from_config({"type": "static", "token": "abc"})
    check(isinstance(prov, StaticToken) and prov.access_token() == "abc", "from_config static")
    prov = from_config({"type": "service_account",
                        "sources": [{"kind": "1password", "ref": op_ref, "token_file": token_path},
                                    {"kind": "file", "path": good}]})
    check(isinstance(prov, ServiceAccount), "from_config service_account")
    check(prov.info_loader()["client_email"] == "sa@example",
          "from_config builds a working source chain")
    raises(lambda: from_config({"type": "service_account", "sources": []}), needle="sources")
    raises(lambda: from_config({"type": "service_account",
                                "sources": [{"kind": "telepathy"}]}), needle="not supported")
    raises(lambda: from_config({"type": "magic", "token": "x"}), needle="not supported")
    raises(lambda: from_config({"token": "x"}), needle="type is required")
    return n
