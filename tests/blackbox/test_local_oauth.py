import base64
import hashlib
import importlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

from adsops.local import oauth_connect as oc


# ---------------------------------------------------------------------------
# Locate the public exception classes without assuming where they live.
# ---------------------------------------------------------------------------
def _find_exception(name):
    exc = getattr(oc, name, None)
    if exc is not None:
        return exc
    for modname in (
        "adsops.local.errors",
        "adsops.local.common",
        "adsops.local.rail",
        "adsops.local.safety",
    ):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        exc = getattr(mod, name, None)
        if exc is not None:
            return exc
    raise ImportError("could not locate %s" % name)


ToolError = _find_exception("ToolError")
RailViolation = _find_exception("RailViolation")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _form(body):
    """Decode a token-request body regardless of bytes/str/dict."""
    if body is None:
        return {}
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if isinstance(body, dict):
        return {k: (v if isinstance(v, list) else [v]) for k, v in body.items()}
    text = str(body).strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            pass
        else:
            return {k: (v if isinstance(v, list) else [v]) for k, v in data.items()}
    return urllib.parse.parse_qs(text, keep_blank_values=True)


class FakeHTTP(object):
    def __init__(self, responder):
        self.calls = []
        self._responder = responder

    def __call__(self, method, url, headers=None, body=None):
        call = {"method": method, "url": url, "headers": headers, "body": body}
        self.calls.append(call)
        return self._responder(method, url, headers, body)


def _token_success(token="AT-secret", refresh="RT-123", expires_in=3600,
                   scope="https://www.googleapis.com/auth/adwords"):
    def responder(method, url, headers, body):
        payload = {
            "access_token": token,
            "refresh_token": refresh,
            "expires_in": expires_in,
            "scope": scope,
            "token_type": "Bearer",
        }
        return 200, json.dumps(payload).encode("utf-8")

    return responder


def _refresh_success(token="AT1", expires_in=3600):
    def responder(method, url, headers, body):
        payload = {
            "access_token": token,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }
        return 200, json.dumps(payload).encode("utf-8")

    return responder


class CredInfo(dict):
    """dict that also supports attribute access, to tolerate either style."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _provider_info():
    return CredInfo(
        type="oauth_user",
        client_id="cid.apps.googleusercontent.com",
        client_secret="top-secret-client-value",
        refresh_token="refresh-token-xyz",
        scopes=["https://www.googleapis.com/auth/adwords"],
    )


def _drive_loopback(make_query, http, timeout_s=5.0, open_browser=None):
    """Run run_loopback, replying to the one GET from a background thread."""
    holder = {"failures": []}

    def default_opener(url):
        holder["url"] = url
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        state = qs.get("state", [""])[0]
        redirect_uri = qs.get("redirect_uri", [""])[0]
        holder["state"] = state
        holder["redirect_uri"] = redirect_uri
        query = make_query(state, redirect_uri)
        sep = "&" if "?" in redirect_uri else "?"
        target = redirect_uri + sep + query
        holder["target"] = target

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def worker():
            for _ in range(250):
                try:
                    with opener.open(target, timeout=2) as resp:
                        resp.read()
                    return
                except Exception as exc:  # noqa: BLE001 - diagnostics only
                    holder["failures"].append(exc)
                    time.sleep(0.02)

        threading.Thread(target=worker, daemon=True).start()

    opener = open_browser if open_browser is not None else default_opener
    try:
        result = oc.run_loopback(
            "cid.apps.googleusercontent.com",
            "client-secret-value",
            ["https://www.googleapis.com/auth/adwords"],
            open_browser=opener,
            http=http,
            timeout_s=timeout_s,
        )
        return result, None, holder
    except Exception as exc:  # noqa: BLE001 - tests inspect the exception
        return None, exc, holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class BuildAuthUrlTests(unittest.TestCase):
    def test_required_google_parameters(self):
        url = oc.build_auth_url(
            "client-id-123",
            "http://127.0.0.1:4444/",
            ["scopeA"],
            "the-state",
            "the-challenge",
        )
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(parsed.path, "/o/oauth2/v2/auth")

        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs["response_type"], ["code"])
        self.assertEqual(qs["access_type"], ["offline"])
        self.assertEqual(qs["prompt"], ["consent"])
        self.assertEqual(qs["code_challenge_method"], ["S256"])
        self.assertEqual(qs["include_granted_scopes"], ["true"])
        self.assertEqual(qs["client_id"], ["client-id-123"])
        self.assertEqual(qs["redirect_uri"], ["http://127.0.0.1:4444/"])
        self.assertEqual(qs["state"], ["the-state"])
        self.assertEqual(qs["code_challenge"], ["the-challenge"])

    def test_scopes_are_space_joined(self):
        scopes = [
            "https://www.googleapis.com/auth/adwords",
            "https://www.googleapis.com/auth/business.manage",
        ]
        url = oc.build_auth_url("cid", "http://127.0.0.1:1/", scopes, "s", "c")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["scope"], [" ".join(scopes)])


class PkceTests(unittest.TestCase):
    def test_verifier_length_and_charset(self):
        verifier, challenge = oc.pkce_pair()
        self.assertEqual(len(verifier), 64)
        allowed = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_"
        )
        self.assertTrue(set(verifier) <= allowed)
        self.assertNotIn("=", verifier)
        self.assertNotIn("=", challenge)

    def test_challenge_is_base64url_sha256_of_verifier(self):
        verifier, challenge = oc.pkce_pair()
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)

    def test_challenge_with_deterministic_randomness(self):
        def rand_stub(n=32):
            return b"\xAB" * n

        verifier, challenge = oc.pkce_pair(rand=rand_stub)
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)
        self.assertEqual(len(verifier), 64)

    def test_pairs_are_not_reused(self):
        first, _ = oc.pkce_pair()
        second, _ = oc.pkce_pair()
        self.assertNotEqual(first, second)


class LoopbackSuccessTests(unittest.TestCase):
    def test_successful_flow_returns_documented_keys(self):
        def make_query(state, redirect_uri):
            return urllib.parse.urlencode({"code": "auth-code-xyz", "state": state})

        http = FakeHTTP(_token_success())
        result, err, holder = _drive_loopback(make_query, http)

        self.assertIsNone(err, "run_loopback raised: %r" % (err,))
        self.assertIsNotNone(result)
        self.assertEqual(result["refresh_token"], "RT-123")
        self.assertIn("scopes", result)
        self.assertIn("obtained_at", result)
        self.assertIn("adwords", json.dumps(result["scopes"]))
        self.assertIsInstance(result["obtained_at"], (int, float))
        # The access token must never be returned.
        self.assertNotIn("access_token", result)

        # Exactly one exchange happened.
        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://oauth2.googleapis.com/token")

        params = _form(call["body"])
        self.assertEqual(params["grant_type"], ["authorization_code"])
        self.assertEqual(params["code"], ["auth-code-xyz"])
        self.assertIn("code_verifier", params)
        self.assertEqual(params["redirect_uri"], [holder["redirect_uri"]])

    def test_binds_loopback_on_ephemeral_port(self):
        def make_query(state, redirect_uri):
            return urllib.parse.urlencode({"code": "c", "state": state})

        result, err, holder = _drive_loopback(make_query, FakeHTTP(_token_success()))
        self.assertIsNone(err, "run_loopback raised: %r" % (err,))

        parsed = urllib.parse.urlparse(holder["redirect_uri"])
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertIsNotNone(parsed.port)
        self.assertNotEqual(parsed.port, 0)

    def test_code_verifier_matches_advertised_challenge(self):
        def make_query(state, redirect_uri):
            return urllib.parse.urlencode({"code": "c", "state": state})

        http = FakeHTTP(_token_success())
        result, err, holder = _drive_loopback(make_query, http)
        self.assertIsNone(err, "run_loopback raised: %r" % (err,))

        auth_qs = urllib.parse.parse_qs(urllib.parse.urlparse(holder["url"]).query)
        self.assertEqual(auth_qs["code_challenge_method"], ["S256"])
        challenge = auth_qs["code_challenge"][0]
        self.assertEqual(auth_qs["state"], [holder["state"]])

        params = _form(http.calls[0]["body"])
        verifier = params["code_verifier"][0]
        self.assertEqual(
            _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            challenge,
        )


class LoopbackRefusalTests(unittest.TestCase):
    def test_state_mismatch_raises_railviolation_and_no_exchange(self):
        def make_query(state, redirect_uri):
            return urllib.parse.urlencode(
                {"code": "auth-code", "state": state + "-tampered"}
            )

        http = FakeHTTP(_token_success())
        result, err, holder = _drive_loopback(make_query, http)

        self.assertIsNone(result)
        self.assertIsInstance(err, RailViolation)
        self.assertEqual(http.calls, [])
        # Secrets must not leak into error text.
        self.assertNotIn("client-secret-value", str(err))

    def test_access_denied_raises_toolerror(self):
        def make_query(state, redirect_uri):
            return urllib.parse.urlencode({"error": "access_denied", "state": state})

        http = FakeHTTP(_token_success())
        result, err, holder = _drive_loopback(make_query, http)

        self.assertIsNone(result)
        self.assertIsInstance(err, ToolError)
        self.assertIn("Google sign-in was cancelled", str(err))
        self.assertEqual(http.calls, [])

    def test_timeout_raises_toolerror(self):
        # open_browser does nothing and nobody ever hits the loopback server.
        result, err, holder = _drive_loopback(
            lambda state, redirect_uri: "",
            FakeHTTP(_token_success()),
            timeout_s=0.5,
            open_browser=lambda url: None,
        )
        self.assertIsNone(result)
        self.assertIsInstance(err, ToolError)


class SaveCredentialsTests(unittest.TestCase):
    def test_writes_oauth_user_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "creds.json")
            oc.save_credentials(path, "cid", "csecret", "rtok")
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["type"], "oauth_user")
            self.assertEqual(data["refresh_token"], "rtok")
            self.assertIn("csecret", json.dumps(data))

    def test_file_is_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "creds.json")
            oc.save_credentials(path, "cid", "csecret", "rtok")
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

    def test_refuses_to_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "creds.json")
            oc.save_credentials(path, "cid", "csecret", "rtok-1")
            with self.assertRaises(Exception):
                oc.save_credentials(path, "cid", "csecret", "rtok-2")
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["refresh_token"], "rtok-1")

    def test_overwrite_true_replaces_file_and_keeps_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "creds.json")
            oc.save_credentials(path, "cid", "csecret", "rtok-1")
            oc.save_credentials(path, "cid", "csecret", "rtok-2", overwrite=True)
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["refresh_token"], "rtok-2")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_optional_developer_token_and_login_customer_id_are_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "creds.json")
            oc.save_credentials(
                path,
                "cid",
                "csecret",
                "rtok",
                developer_token="dev-token-value",
                login_customer_id="1234567890",
            )
            blob = json.dumps(json.load(open(path, encoding="utf-8")))
            self.assertIn("dev-token-value", blob)
            self.assertIn("1234567890", blob)


class UserTokenProviderTests(unittest.TestCase):
    def test_access_token_refreshes_with_refresh_token_grant(self):
        http = FakeHTTP(_refresh_success("AT1"))
        provider = oc.UserTokenProvider(_provider_info(), http)

        self.assertEqual(provider.access_token(), "AT1")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["url"], "https://oauth2.googleapis.com/token")

        params = _form(http.calls[0]["body"])
        self.assertEqual(params["grant_type"], ["refresh_token"])
        self.assertEqual(params["refresh_token"], ["refresh-token-xyz"])

    def test_access_token_is_cached(self):
        http = FakeHTTP(_refresh_success("AT1"))
        provider = oc.UserTokenProvider(_provider_info(), http)

        self.assertEqual(provider.access_token(), "AT1")
        self.assertEqual(provider.access_token(), "AT1")
        self.assertEqual(len(http.calls), 1)

    def test_cache_expires_one_minute_before_expires_in(self):
        clock = [1000.0]
        http = FakeHTTP(_refresh_success("AT1", expires_in=3600))
        provider = oc.UserTokenProvider(_provider_info(), http, now=lambda: clock[0])

        self.assertEqual(provider.access_token(), "AT1")
        self.assertEqual(len(http.calls), 1)

        clock[0] += 3500.0  # inside the expires_in - 60 window
        self.assertEqual(provider.access_token(), "AT1")
        self.assertEqual(len(http.calls), 1)

        clock[0] += 100.0   # past the safety window
        self.assertEqual(provider.access_token(), "AT1")
        self.assertEqual(len(http.calls), 2)

    def test_invalidate_forces_refresh(self):
        http = FakeHTTP(_refresh_success("AT1"))
        provider = oc.UserTokenProvider(_provider_info(), http)

        provider.access_token()
        provider.invalidate()
        provider.access_token()
        self.assertEqual(len(http.calls), 2)

    def test_invalid_grant_raises_toolerror_with_documented_hint(self):
        def responder(method, url, headers, body):
            return 400, json.dumps(
                {"error": "invalid_grant", "error_description": "expired"}
            ).encode("utf-8")

        provider = oc.UserTokenProvider(_provider_info(), FakeHTTP(responder))
        with self.assertRaises(ToolError) as ctx:
            provider.access_token()
        self.assertIn(
            "Google access expired or was revoked: run `adsops-mcp connect` again",
            str(ctx.exception),
        )

    def test_repr_hides_secrets(self):
        http = FakeHTTP(_refresh_success("AT-SUPER-SECRET"))
        provider = oc.UserTokenProvider(_provider_info(), http)
        provider.access_token()

        text = repr(provider)
        self.assertNotIn("top-secret-client-value", text)
        self.assertNotIn("refresh-token-xyz", text)
        self.assertNotIn("AT-SUPER-SECRET", text)


if __name__ == '__main__':
    unittest.main()
