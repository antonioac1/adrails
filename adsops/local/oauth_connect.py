"""Google Ads OAuth "Connect Google" helper for AdRails.

Implements the OAuth 2.0 installed-app loopback flow with PKCE, saves refresh
credentials with 0600 permissions, and provides a refresh-token access provider.
All external effects (HTTP, browser opening, server binding, clock) are
injectable so tests never touch the network. The module never returns or logs
client secrets, refresh tokens, or access tokens; callers receive only the
refresh token they explicitly asked to obtain. Tool code should call
``run_loopback`` once during setup, then use ``UserTokenProvider`` for API calls.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

from adsops.errors import ToolError, RailViolation, scrub

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPES = ("https://www.googleapis.com/auth/adwords", "https://www.googleapis.com/auth/webmasters.readonly")


def _now_iso() -> str:
    """UTC timestamp as an ISO string (never a datetime object)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_body(raw) -> dict:
    """Decode a token-endpoint body defensively; never leak the raw body."""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    else:
        text = str(raw or "")
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scope_string(scopes) -> str:
    if isinstance(scopes, str):
        parts = [p for p in scopes.split() if p]
    else:
        parts = [str(p) for p in (scopes or []) if p]
    if not parts:
        raise ToolError("at least one OAuth scope is required")
    return " ".join(parts)


def build_auth_url(client_id: str, redirect_uri: str, scopes, state: str, code_challenge: str) -> str:
    """Build the Google OAuth consent URL for the loopback+PKCE flow."""
    if not client_id:
        raise ToolError("client_id is required")
    if not redirect_uri:
        raise ToolError("redirect_uri is required")
    if not state:
        raise ToolError("state is required")
    if not code_challenge:
        raise ToolError("code_challenge is required")
    params = {
        "client_id": str(client_id),
        "redirect_uri": str(redirect_uri),
        "response_type": "code",
        "scope": _scope_string(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": str(state),
        "code_challenge": str(code_challenge),
        "code_challenge_method": "S256",
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _s256(verifier: str) -> str:
    """base64url(SHA-256(verifier)) without padding, per RFC 7636."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def pkce_pair(rand=os.urandom):
    """Return (64-char urlsafe verifier, S256 challenge)."""
    raw = rand(48)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 48:
        raise ToolError("PKCE random source must return 48 bytes")
    verifier = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
    return verifier, _s256(verifier)


def _default_http(method, url, headers, body):
    """Small stdlib HTTP transport used only when the caller injects nothing."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(getattr(resp, "status", resp.getcode())), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


class _LoopbackServer:
    """One-shot HTTP listener bound to 127.0.0.1 on an ephemeral port."""

    def __init__(self, host: str, expected_state: str):
        if host != "127.0.0.1":
            raise RailViolation("OAuth loopback must bind 127.0.0.1 only")
        self._expected_state = str(expected_state)
        self._query = {}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(
                    parsed.query, keep_blank_values=True
                ).items()}
                owner._query = q
                err = str(q.get("error") or "")
                got_state = str(q.get("state") or "")
                state_ok = bool(got_state) and hmac.compare_digest(got_state, owner._expected_state)

                if err:
                    msg = "Sign-in was not completed."
                    status = 200
                    if err == "access_denied":
                        msg = "Sign-in was cancelled."
                elif not state_ok:
                    msg = "Invalid sign-in state. Please try again."
                    status = 400
                else:
                    msg = "You're connected - you can close this tab."
                    status = 200

                body = (
                    "<!doctype html><html><body><p>"
                    + html.escape(msg)
                    + "</p></body></html>"
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = http.server.HTTPServer((host, 0), Handler)
        self.port = int(self._httpd.server_address[1])

    def serve(self, timeout_s: float):
        try:
            self._httpd.timeout = max(0.1, float(timeout_s))
        except (TypeError, ValueError):
            self._httpd.timeout = 300.0
        try:
            self._httpd.handle_request()
        except OSError:
            pass
        return dict(self._query)

    def shutdown(self):
        try:
            self._httpd.server_close()
        except OSError:
            pass


def _default_server_factory(host: str, expected_state: str):
    return _LoopbackServer(host, expected_state)


def _exchange_code(http, client_id, client_secret, code, redirect_uri, verifier):
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode("utf-8")
    status, raw = http(
        "POST",
        TOKEN_URL,
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    data = _json_body(raw)
    err = str(data.get("error") or "")
    if err in ("access_denied", "invalid_grant"):
        raise ToolError("Google sign-in was cancelled")
    if _as_int(status, 0) != 200 or err:
        detail = data.get("error_description") or err or "token exchange failed"
        raise ToolError("Google sign-in failed: " + scrub(str(detail)))
    if not data.get("refresh_token"):
        raise ToolError("Google did not return a refresh token; try connecting again")
    return data


def run_loopback(
    client_id: str,
    client_secret: str,
    scopes,
    open_browser=webbrowser.open,
    http=None,
    timeout_s: float = 300,
    server_factory=None,
    rand=os.urandom,
):
    """Run the one-shot loopback OAuth flow and return refresh credentials.

    ``server_factory(host, expected_state)`` may be injected for tests; it must
    return an object with ``port``, ``serve(timeout_s)->dict``, and ``shutdown()``.
    The return value intentionally contains only refresh_token, scopes, and
    obtained_at; access tokens and client secrets stay internal.
    """
    if not client_id:
        raise ToolError("client_id is required")
    if not client_secret:
        raise ToolError("client_secret is required")
    if http is None:
        http = _default_http

    verifier, challenge = pkce_pair(rand=rand)
    state = secrets.token_urlsafe(24)
    factory = server_factory or _default_server_factory

    try:
        server = factory("127.0.0.1", state)
    except Exception as exc:
        raise ToolError("Could not start local sign-in listener: " + scrub(str(exc)))

    try:
        port = _as_int(getattr(server, "port", 0), 0)
        if port <= 0:
            raise ToolError("local sign-in listener did not provide a valid port")
        redirect_uri = f"http://127.0.0.1:{port}/"
        auth_url = build_auth_url(client_id, redirect_uri, scopes, state, challenge)
        try:
            opened = open_browser(auth_url)
        except Exception:
            opened = False
        if opened is False:
            # no browser (webbrowser.open -> False): print the link instead of silently waiting 5 minutes (review minor)
            import sys as _sys
            print("Open this link to sign in to Google:\n" + auth_url, file=_sys.stderr)

        try:
            query = server.serve(timeout_s) or {}
        except TimeoutError:
            raise ToolError("Timed out waiting for Google sign-in")
        if not query:
            raise ToolError("Timed out waiting for Google sign-in")

        # Validate state even when Google returns an error, to prevent CSRF.
        got_state = str(query.get("state") or "")
        if not hmac.compare_digest(got_state, state):
            raise RailViolation("OAuth state mismatch")

        err = str(query.get("error") or "")
        if err:
            if err == "access_denied":
                raise ToolError("Google sign-in was cancelled")
            raise ToolError("Google sign-in failed: " + scrub(err))

        code = str(query.get("code") or "")
        if not code:
            raise ToolError("Google did not return an authorization code")

        data = _exchange_code(http, client_id, client_secret, code, redirect_uri, verifier)
        return {
            "refresh_token": str(data["refresh_token"]),
            "scopes": _scopes_from(data, scopes),
            "obtained_at": _now_iso(),
        }
    finally:
        try:
            server.shutdown()
        except Exception:
            pass


def _scopes_from(data: dict, fallback) -> list:
    raw = data.get("scope") if isinstance(data, dict) else None
    if isinstance(raw, str):
        return [s for s in raw.split() if s]
    if isinstance(raw, list):
        return [str(s) for s in raw if s]
    if isinstance(fallback, str):
        return [s for s in fallback.split() if s]
    return [str(s) for s in (fallback or []) if s]


def save_credentials(
    path,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    developer_token: str | None = None,
    login_customer_id: str | None = None,
    overwrite: bool = False,
):
    """Write OAuth credentials as JSON with mode 0600; never overwrite by default."""
    if not path:
        raise ToolError("credentials path is required")
    payload = {
        "type": "oauth_user",
        "client_id": str(client_id),
        "client_secret": str(client_secret),
        "refresh_token": str(refresh_token),
        "created_at": _now_iso(),
    }
    if developer_token is not None:
        payload["developer_token"] = str(developer_token)
    if login_customer_id is not None:
        payload["login_customer_id"] = str(login_customer_id)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(os.fspath(path), flags, 0o600)
    except FileExistsError:
        raise ToolError("credentials file already exists; pass overwrite=True")
    except OSError as exc:
        raise ToolError("could not write credentials: " + scrub(str(exc)))

    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        raise ToolError("could not write credentials: " + scrub(str(exc)))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {"path": os.fspath(path), "type": "oauth_user"}


class UserTokenProvider:
    """Refresh-token backed access-token provider with a small in-memory cache."""

    def __init__(self, info: dict, http, now=time.time):
        if not isinstance(info, dict):
            raise ToolError("credentials must be a JSON object")
        self._client_id = str(info.get("client_id") or "")
        self._client_secret = str(info.get("client_secret") or "")
        self._refresh_token = str(info.get("refresh_token") or "")
        if not (self._client_id and self._client_secret and self._refresh_token):
            raise ToolError("credentials are incomplete")
        self._http = http
        self._now = now
        self._token = None
        self._expires_at = 0.0

    def invalidate(self):
        self._token = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        if self._token and self._now() < self._expires_at:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }).encode("utf-8")
        status, raw = self._http(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        )
        data = _json_body(raw)
        err = str(data.get("error") or "")
        if err == "invalid_grant":
            raise ToolError("Google access expired or was revoked: run `adsops-mcp connect` again")
        if _as_int(status, 0) != 200 or err:
            detail = data.get("error_description") or err or "token refresh failed"
            raise ToolError("Google token refresh failed: " + scrub(str(detail)))
        token = data.get("access_token")
        if not token:
            raise ToolError("Google token refresh did not return an access token")
        expires_in = _as_float(data.get("expires_in"), 3600.0)
        self._token = str(token)
        # Refresh slightly early so a just-expired token is never used.
        self._expires_at = self._now() + max(0.0, expires_in - 60.0)
        return self._token

    def __repr__(self):
        return (
            "UserTokenProvider("
            f"client_id={self._client_id!r}, "
            "refresh_token='***', "
            f"access_token={'***' if self._token else None})"
        )


def selftest() -> int:
    import stat
    import tempfile

    n = 0

    def check(cond, msg):
        nonlocal n
        assert cond, msg
        n += 1

    def never_http(*args, **kwargs):
        raise AssertionError("http should not have been called")

    # --- Auth URL construction ------------------------------------------------
    url = build_auth_url("cid", "http://127.0.0.1:1234/", ["s1", "s2"], "st", "chal")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    check(url.startswith(AUTH_URL + "?"), "auth URL base")
    check(q["response_type"] == ["code"], "response_type")
    check(q["access_type"] == ["offline"], "offline access")
    check(q["prompt"] == ["consent"], "consent prompt")
    check(q["code_challenge_method"] == ["S256"], "PKCE method")
    check(q["include_granted_scopes"] == ["true"], "granted scopes")
    check(q["scope"] == ["s1 s2"], "scopes joined")
    check(q["state"] == ["st"], "state present")
    try:
        build_auth_url("cid", "http://x/", [], "st", "chal")
        check(False, "empty scopes must fail")
    except ToolError:
        check(True, "empty scopes refused")

    # --- PKCE and RFC 7636 known vector --------------------------------------
    verifier, challenge = pkce_pair(rand=lambda n: b"\x01" * n)
    check(len(verifier) == 64, "verifier length 64")
    check(challenge == _s256(verifier), "challenge matches verifier")
    check(len(challenge) == 43, "challenge length 43")
    check(
        _s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "RFC 7636 challenge vector",
    )

    class FakeServer:
        def __init__(self, port, query):
            self.port = port
            self.query = query
            self.closed = False

        def serve(self, timeout_s):
            return dict(self.query)

        def shutdown(self):
            self.closed = True

    # --- State mismatch is a rail violation ----------------------------------
    captured = {}

    def bad_factory(host, expected_state):
        captured["host"] = host
        srv = FakeServer(12000, {"code": "c", "state": "wrong"})
        captured["server"] = srv
        return srv

    try:
        run_loopback(
            "cid", "sec", ["s"], http=never_http,
            open_browser=lambda u: None, server_factory=bad_factory,
        )
        check(False, "state mismatch must raise")
    except RailViolation:
        check(True, "state mismatch refused")
    check(captured["host"] == "127.0.0.1", "binds loopback only")
    check(captured["server"].closed, "server closed after mismatch")

    # --- access_denied maps to a clean cancellation --------------------------
    def denied_factory(host, expected_state):
        return FakeServer(12001, {"error": "access_denied", "state": expected_state})

    try:
        run_loopback(
            "cid", "sec", ["s"], http=never_http,
            open_browser=lambda u: None, server_factory=denied_factory,
        )
        check(False, "access_denied must raise")
    except ToolError as exc:
        check(str(exc) == "Google sign-in was cancelled", "cancellation message")

    # --- Happy path with mocked token exchange -------------------------------
    captured = {}

    def happy_http(method, url, headers, body):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = body.decode("utf-8")
        return 200, json.dumps({
            "refresh_token": "refresh-123",
            "access_token": "access-secret",
            "expires_in": 3600,
            "scope": "s1 s2",
        }).encode("utf-8")

    def happy_factory(host, expected_state):
        captured["state"] = expected_state
        srv = FakeServer(13000, {"code": "the-code", "state": expected_state})
        captured["server"] = srv
        return srv

    result = run_loopback(
        "cid", "sec", ["s1"],
        http=happy_http,
        open_browser=lambda u: captured.__setitem__("auth_url", u),
        server_factory=happy_factory,
        rand=lambda n: b"\x02" * n,
    )
    check(result["refresh_token"] == "refresh-123", "refresh token returned")
    check("access_token" not in result, "access token not returned")
    check(result["scopes"] == ["s1", "s2"], "granted scopes returned")
    check(isinstance(result["obtained_at"], str), "timestamp is a string")
    check(captured["server"].closed, "server closed on success")
    check(captured["method"] == "POST", "token exchange uses POST")
    check(captured["url"] == TOKEN_URL, "token endpoint used")
    check("grant_type=authorization_code" in captured["body"], "auth code grant")
    check("code_verifier=" in captured["body"], "verifier sent")
    check("code=the-code" in captured["body"], "code sent")
    check("redirect_uri=http%3A%2F%2F127.0.0.1%3A13000%2F" in captured["body"], "redirect uri")
    check("state=" + captured["state"] in captured["auth_url"], "state in auth URL")

    # --- Timeout and token-endpoint errors -----------------------------------
    def empty_factory(host, expected_state):
        return FakeServer(13001, {})

    try:
        run_loopback(
            "cid", "sec", ["s"], http=never_http,
            open_browser=lambda u: None, server_factory=empty_factory,
        )
        check(False, "timeout must raise")
    except ToolError as exc:
        check("Timed out" in str(exc), "timeout message")

    def error_http(method, url, headers, body):
        return 400, b'{"error":"invalid_grant"}'

    def ok_factory(host, expected_state):
        return FakeServer(13002, {"code": "c", "state": expected_state})

    try:
        run_loopback(
            "cid", "sec", ["s"], http=error_http,
            open_browser=lambda u: None, server_factory=ok_factory,
        )
        check(False, "exchange error must raise")
    except ToolError as exc:
        check("cancelled" in str(exc), "exchange error surfaced")

    # --- Credential file writing ---------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "credentials.json")
        out = save_credentials(
            path, "cid", "client-secret-xyz", "refresh-abc",
            developer_token="dev-token", login_customer_id="123",
        )
        check(os.path.exists(path), "credentials file created")
        check(stat.S_IMODE(os.stat(path).st_mode) == 0o600, "mode 0600")
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        check(saved["type"] == "oauth_user", "credential type")
        check(saved["refresh_token"] == "refresh-abc", "refresh token saved")
        check("client-secret-xyz" not in json.dumps(out), "no secret in return")
        try:
            save_credentials(path, "cid2", "s2", "r2")
            check(False, "existing file must not be overwritten")
        except ToolError:
            check(True, "overwrite refused")
        save_credentials(path, "cid2", "s2", "r2", overwrite=True)
        with open(path, "r", encoding="utf-8") as fh:
            saved2 = json.load(fh)
        check(saved2["client_id"] == "cid2", "overwrite updates content")
        check(stat.S_IMODE(os.stat(path).st_mode) == 0o600, "overwrite keeps 0600")

    # --- Token provider caching, expiry, errors, repr ------------------------
    clock = [1000.0]
    calls = []

    def token_http(method, url, headers, body):
        calls.append(body.decode("utf-8"))
        return 200, json.dumps({
            "access_token": "tok" + str(len(calls)),
            "expires_in": 120,
        }).encode("utf-8")

    provider = UserTokenProvider(
        {"client_id": "cid", "client_secret": "sec-xyz", "refresh_token": "refresh-xyz"},
        http=token_http,
        now=lambda: clock[0],
    )
    check(provider.access_token() == "tok1", "first access token")
    check(provider.access_token() == "tok1", "cached access token")
    check(len(calls) == 1, "cache avoids second refresh")
    check("grant_type=refresh_token" in calls[0], "refresh grant sent")
    check("refresh_token=refresh-xyz" in calls[0], "refresh token sent internally")
    clock[0] += 61.0
    check(provider.access_token() == "tok2", "refresh after expiry window")
    check(len(calls) == 2, "second refresh occurred")

    def invalid_http(method, url, headers, body):
        return 400, b'{"error":"invalid_grant"}'

    bad = UserTokenProvider(
        {"client_id": "cid", "client_secret": "sec-xyz", "refresh_token": "refresh-xyz"},
        http=invalid_http,
        now=lambda: 0,
    )
    try:
        bad.access_token()
        check(False, "invalid_grant must raise")
    except ToolError as exc:
        check(
            str(exc) == "Google access expired or was revoked: run `adsops-mcp connect` again",
            "invalid_grant message",
        )

    check("sec-xyz" not in repr(provider), "repr hides client secret")
    check("refresh-xyz" not in repr(provider), "repr hides refresh token")
    check("tok1" not in repr(provider), "repr hides access token")

    try:
        UserTokenProvider({}, http=never_http)
        check(False, "incomplete credentials must raise")
    except ToolError:
        check(True, "incomplete credentials refused")

    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
