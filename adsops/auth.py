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
from datetime import timezone

from .errors import ToolError, scrub

SCOPE_GOOGLE_ADS = "https://www.googleapis.com/auth/adwords"

_REDACTED = "[REDACTED]"
MAX_TOKEN_LIFETIME_S = 3600   # Google access tokens live 1 h; never trust an expiry further out than this


def _expiry_epoch(expiry, now=time.time):
    """Epoch seconds for a google-auth `creds.expiry`.

    google-auth returns a NAIVE datetime that is in UTC. `naive.timestamp()` interprets it as LOCAL
    time, so on a Mac in America/New_York a token was believed valid ~4-5 h past its real 1 h expiry
    and calls failed with 401 ACCESS_TOKEN_EXPIRED (observed live 2026-11-01). Naive means UTC here.
    """
    if expiry is None:
        return float(now()) + 3000
    try:
        if getattr(expiry, "tzinfo", None) is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return float(expiry.timestamp())
    except Exception:
        return float(now()) + 3000


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

    def invalidate(self):
        """Forget any cached token so the next access_token() fetches a fresh one (no-op by default)."""
        return None


class StaticToken(TokenProvider):
    """Bring-your-own-token (tests, short-lived manual runs)."""

    def __init__(self, token):
        if not isinstance(token, str) or not token.strip():
            raise ToolError("static token must be a non-empty string") from None
        self._token = token.strip()

    def access_token(self):
        return self._token

    def __repr__(self):
        return "StaticToken(token=[REDACTED])"


def _openssl_refresher(info, scope, runner=subprocess.run, http=None, now=time.time):
    """Zero-dependency service-account flow: RS256-sign a JWT with the system `openssl`, exchange it at
    Google's token endpoint over urllib. Used when google-auth is not installed (e.g. MCPB bundles)."""
    import base64, json as _json, os as _os, tempfile, urllib.parse, urllib.request
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")
    for k in ("client_email", "private_key"):
        if not info.get(k):
            raise ToolError(f"service-account JSON is missing '{k}'") from None
    uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    t = int(now())
    claims = {"iss": info["client_email"], "scope": scope, "aud": uri, "iat": t, "exp": t + 3600}
    signing_input = b64(b'{"alg":"RS256","typ":"JWT"}') + b"." + b64(_json.dumps(claims).encode())
    fd, key_path = tempfile.mkstemp(prefix="adsops-", suffix=".pem")
    try:
        _os.chmod(key_path, 0o600)
        with _os.fdopen(fd, "w") as fh:
            fh.write(info["private_key"])
        r = runner(["openssl", "dgst", "-sha256", "-sign", key_path], input=signing_input, capture_output=True)
    except FileNotFoundError:
        raise ToolError("neither google-auth nor openssl is available to sign the service-account token") from None
    finally:
        try:
            _os.unlink(key_path)
        except OSError:
            pass
    if r.returncode != 0 or not r.stdout:
        raise ToolError("openssl could not sign the service-account token") from None
    assertion = (signing_input + b"." + b64(r.stdout)).decode()
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": assertion}).encode()
    try:
        if http:
            data = http(uri, body)
        else:
            req = urllib.request.Request(uri, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read())
    except Exception as e:
        raise ToolError("service-account token exchange failed: " + _hide(e)) from None
    if not data.get("access_token"):
        raise ToolError("service-account token exchange returned no access_token") from None
    return data["access_token"], t + int(data.get("expires_in", 3600))


def _google_refresher(info, scope):
    """Default refresher: google-auth when installed, else the zero-dependency openssl flow."""
    try:
        from google.oauth2 import service_account
    except Exception:
        return _openssl_refresher(info, scope)
    try:
        from google.auth.transport.requests import Request
    except Exception:  # no `requests`: google-auth's stdlib http.client transport keeps bundles pure-Python
        from google.auth.transport._http_client import Request
    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=[scope])
        creds.refresh(Request())
    except Exception as e:
        raise ToolError("service-account token refresh failed: " + _hide(e)) from None
    token = getattr(creds, "token", None)
    return token, _expiry_epoch(getattr(creds, "expiry", None))


class ServiceAccount(TokenProvider):
    """Service-account credential with a cached token (refreshed when expiry-60s is reached)."""

    def __init__(self, info_loader, scope=SCOPE_GOOGLE_ADS, now=time.time, refresher=None):
        if not callable(info_loader):
            raise ToolError("ServiceAccount needs a callable info_loader") from None
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
            raise ToolError("service-account info loader returned no credential object") from None
        try:
            pair = self.refresher(info, self.scope)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError("service-account token refresh failed: " + _hide(e)) from None
        try:
            token, expiry = pair
        except Exception:
            raise ToolError("service-account refresher returned a malformed (token, expiry) pair") from None
        if not token:
            raise ToolError("service-account refresher returned no token") from None
        t = float(self.now())
        try:
            self._expiry = float(expiry)
        except (TypeError, ValueError):
            self._expiry = t + 3000
        self._expiry = min(self._expiry, t + MAX_TOKEN_LIFETIME_S)   # defensive cap: a bad clock/TZ can't stretch it
        self._token = str(token)
        return self._token

    def invalidate(self):
        """Drop the cached token (e.g. after a 401 ACCESS_TOKEN_EXPIRED) so the next call refreshes."""
        self._token, self._expiry = None, 0.0

    def __repr__(self):
        return "ServiceAccount(scope=%r)" % (self.scope,)


# -- info loaders ------------------------------------------------------------------------------
def sa_file_loader(path):
    """Return a loader that reads a service-account JSON file (path expanded, read lazily)."""
    p = os.path.expanduser(str(path))
    if not p:
        raise ToolError("service-account file path is empty") from None

    def load():
        if not os.path.isfile(p):
            raise ToolError("service-account file not found: %s" % p) from None
        try:
            with open(p, encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception as e:
            raise ToolError("service-account file is not valid JSON: " + _hide(e)) from None
        if not isinstance(info, dict) or not info:
            raise ToolError("service-account file does not contain a JSON object") from None
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
        raise ToolError("1Password read of %s failed: %s - %s" % (ref_s, why, hint)) from None

    def load():
        try:
            with open(token_path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except Exception as e:
            raise ToolError("1Password token file for %s is unreadable (%s) - %s: %s"
                            % (ref_s, token_path, hint, _hide(e))) from None
        if not token:
            raise ToolError("1Password token file for %s is empty (%s) - %s"
                            % (ref_s, token_path, hint)) from None
        env = dict(os.environ)
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
        try:
            r = runner([op_bin, "read", ref_s], capture_output=True, text=True, env=env, timeout=60)   # a hung op must not hang the server
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
            raise ToolError("1Password value for %s is not valid JSON - %s" % (ref_s, hint)) from None
        if not isinstance(info, dict) or not info:
            raise ToolError("1Password value for %s is not a JSON object - %s" % (ref_s, hint)) from None
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
        raise ToolError("no credential source worked (tried: %s)" % ", ".join(tried or ["none"])) from None

    load.kind = "chain"
    return load


def from_config(cred):
    """Build a TokenProvider from a config `credentials.google_ads` block."""
    if not isinstance(cred, dict):
        raise ToolError("credentials must be a JSON object with a 'type'") from None
    kind = str(cred.get("type") or "").strip()
    if kind == "static":
        token = cred.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ToolError("credentials.token is required and must be a non-empty string "
                            "for type 'static'") from None
        return StaticToken(token)
    if kind == "service_account":
        sources = cred.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ToolError("credentials.sources must be a non-empty list for type "
                            "'service_account'") from None
        loaders = []
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                raise ToolError("credentials.sources[%d] must be an object" % i) from None
            skind = str(src.get("kind") or "").strip()
            if skind == "file":
                path = src.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise ToolError("credentials.sources[%d].path is required for kind 'file'" % i) from None
                loaders.append(sa_file_loader(path))
            elif skind == "1password":
                ref, tok = src.get("ref"), src.get("token_file")
                if not isinstance(ref, str) or not ref.strip():
                    raise ToolError("credentials.sources[%d].ref is required for kind '1password'" % i) from None
                if not isinstance(tok, str) or not tok.strip():
                    raise ToolError("credentials.sources[%d].token_file is required for kind "
                                    "'1password'" % i) from None
                op_bin = src.get("op_bin")
                loaders.append(onepassword_loader(ref, tok, op_bin=op_bin) if isinstance(op_bin, str) and op_bin.strip()
                               else onepassword_loader(ref, tok))
            else:
                raise ToolError("credentials.sources[%d].kind %r is not supported "
                                "(file|1password)" % (i, skind)) from None
        return ServiceAccount(chain_loader(*loaders))
    if kind == "oauth_user":
        # Small-business path (2026-11-06): the owner signs in with his own Google login once (`adsops-mcp connect`), and the
        # refresh token lives in a chmod-600 file beside the config. Either inline fields or {"file": path} written by connect.
        info = cred
        if cred.get("file"):
            import json as _json
            p = os.path.expanduser(str(cred["file"]))
            if not os.path.isfile(p):
                raise ToolError("Google sign-in file not found: run `adsops-mcp connect` first") from None
            # POSIX only: Windows st_mode always reports 0o666, and its ACLs (user profile dir) are the protection (review #9)
            if os.name != "nt" and os.stat(p).st_mode & 0o077:
                raise ToolError("the Google sign-in file must be private (chmod 600)") from None
            with open(p, encoding="utf-8") as fh:
                info = _json.load(fh)
        for k in ("client_id", "client_secret", "refresh_token"):
            if not isinstance(info.get(k), str) or not info.get(k):
                raise ToolError("oauth_user credentials need %s (run `adsops-mcp connect`)" % k) from None
        from adsops.local.oauth_connect import UserTokenProvider
        from adsops.aeo_tools import raw_http
        tp = UserTokenProvider(info, http=raw_http)
        tp.developer_token = info.get("developer_token") or None   # optional; Google is moving access to the Cloud project
        return tp
    if not kind:
        raise ToolError("credentials.type is required (static|service_account|oauth_user)") from None
    raise ToolError("credentials.type %r is not supported (static|service_account|oauth_user)" % (kind,)) from None


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
    sa.invalidate()
    check(sa.access_token() == "tok3" and len(calls) == 3, "invalidate() forces a refresh")
    TokenProvider.invalidate(StaticToken("x"))   # base no-op is callable

    # defensive cap: an expiry claimed 5 h out is trusted for at most MAX_TOKEN_LIFETIME_S
    clock2, calls2 = [5000.0], []
    def far_refresher(info, scope):
        calls2.append(1)
        return "far%d" % len(calls2), clock2[0] + 5 * 3600
    sa_far = ServiceAccount(lambda: {"k": 1}, now=lambda: clock2[0], refresher=far_refresher)
    check(sa_far.access_token() == "far1" and sa_far._expiry == 5000.0 + MAX_TOKEN_LIFETIME_S,
          "expiry capped at now + 3600 s")
    clock2[0] = 5000.0 + MAX_TOKEN_LIFETIME_S - 59
    check(sa_far.access_token() == "far2" and len(calls2) == 2, "capped token refreshed before the real 1 h expiry")

    # naive-UTC expiry (google-auth's creds.expiry) under a non-UTC local timezone
    import datetime as _dt
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        real_now = time.time()
        naive = _dt.datetime.now(timezone.utc).replace(tzinfo=None) + _dt.timedelta(seconds=3600)
        check(abs(naive.timestamp() - (real_now + 3600)) > 3 * 3600,
              "control: naive .timestamp() is hours off under America/New_York (the bug)")
        check(abs(_expiry_epoch(naive) - (real_now + 3600)) < 5, "naive expiry read as UTC")
        aware = _dt.datetime.now(timezone.utc) + _dt.timedelta(seconds=1800)
        check(abs(_expiry_epoch(aware) - (real_now + 1800)) < 5, "aware expiry unchanged")
        check(abs(_expiry_epoch(None, now=lambda: 100.0) - 3100.0) < 1e-6, "missing expiry -> now + 3000")
        clock3 = [real_now]
        sa_tz = ServiceAccount(lambda: {"k": 1}, now=lambda: clock3[0],
                               refresher=lambda i, s: ("tz-tok", _expiry_epoch(naive)))
        sa_tz.access_token()
        check(abs(sa_tz._expiry - (real_now + 3600)) < 5, "service account caches the real 1 h expiry")
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()

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
        raise ToolError("nope") from None

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
                        "sources": [{"kind": "1password", "ref": op_ref, "token_file": token_path,
                                     "op_bin": "/nonexistent/adrails-selftest/op"},   # hermetic: never runs the real op
                                    {"kind": "file", "path": good}]})
    check(isinstance(prov, ServiceAccount), "from_config service_account")
    check(prov.info_loader()["client_email"] == "sa@example",
          "from_config builds a working source chain")
    raises(lambda: from_config({"type": "service_account", "sources": []}), needle="sources")
    raises(lambda: from_config({"type": "service_account",
                                "sources": [{"kind": "telepathy"}]}), needle="not supported")
    raises(lambda: from_config({"type": "magic", "token": "x"}), needle="not supported")
    raises(lambda: from_config({"token": "x"}), needle="type is required")
    # zero-dependency openssl flow (mocked runner + http): JWT shape, key file removed, token returned
    seen = {}
    class _R:
        returncode, stdout = 0, b"sig"
    def _run(cmd, input=None, capture_output=None):
        seen["cmd"], seen["input"], seen["keyfile"] = cmd, input, cmd[-1]
        seen["mode"] = oct(os.stat(cmd[-1]).st_mode & 0o777)
        return _R()
    def _http(uri, body):
        seen["body"] = body
        return {"access_token": "tok123", "expires_in": 3599}
    tok, exp = _openssl_refresher({"client_email": "a@b", "private_key": "KEY"}, "s", runner=_run, http=_http, now=lambda: 1000)
    check(tok == "tok123" and exp == 1000 + 3599, "openssl flow returns token + expiry")
    check(seen["cmd"][:4] == ["openssl", "dgst", "-sha256", "-sign"] and seen["mode"] == "0o600", "key file 0600 for openssl")
    check(not os.path.exists(seen["keyfile"]), "temp key file removed")
    check(b"jwt-bearer" in seen["body"] and seen["input"].count(b".") == 1, "jwt-bearer grant with header.claims signing input")
    try:
        _openssl_refresher({"client_email": "a@b"}, "s", runner=_run, http=_http)
        check(False, "missing private_key refused")
    except ToolError:
        check(True, "missing private_key refused")

    return n
