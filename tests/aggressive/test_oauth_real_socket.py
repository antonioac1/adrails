"""REAL-socket test of the Google sign-in loopback (lesson: mocks hid broken IndexNow/webhooks in 0.4.0). A real 127.0.0.1 server,
a 'browser' thread that really GETs the redirect, and a fake token endpoint. Run: PYTHONPATH=. python3 tests/aggressive/test_oauth_real_socket.py"""
import json, threading, urllib.parse, urllib.request
from adsops.errors import RailViolation
from adsops.local import oauth_connect as O

threads, seen = [], {}


def browser(state_override=None):
    def open_(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen["redirect"], state = q["redirect_uri"][0], state_override or q["state"][0]
        def go():
            try:
                seen["page"] = urllib.request.urlopen(seen["redirect"] + "?" + urllib.parse.urlencode({"code": "AUTHCODE", "state": state}),
                                                      timeout=10).read().decode()
            except Exception as e:
                seen["page"] = "ERR " + str(e)
        t = threading.Thread(target=go); t.start(); threads.append(t)
        return True
    return open_


def http(method, url, headers, body):
    b = urllib.parse.parse_qs(body.decode() if isinstance(body, bytes) else body)
    seen["body"] = b
    return 200, json.dumps({"access_token": "AT", "refresh_token": "RT-REAL", "expires_in": 3600})


r = O.run_loopback("cid", "sec", ["https://www.googleapis.com/auth/adwords"], open_browser=browser(), http=http, timeout_s=20)
for t in threads: t.join(10)
assert seen["redirect"].startswith("http://127.0.0.1:"), seen["redirect"]
assert r["refresh_token"] == "RT-REAL" and seen["body"]["code"] == ["AUTHCODE"] and "code_verifier" in seen["body"]
assert "connected" in seen["page"].lower(), seen["page"][:200]
try:
    O.run_loopback("cid", "sec", ["s"], open_browser=browser("WRONG"), http=http, timeout_s=10)
    raise SystemExit("FAIL: wrong state accepted")
except RailViolation:
    pass
for t in threads: t.join(10)
print("OK real-socket oauth loopback (127.0.0.1, PKCE, state check, success page)")
