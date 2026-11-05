"""Real-socket tests (no mocks) for every code path that sends HTTP outside Google Ads: IndexNow submit and monitor webhooks.
Added after 0.4.0 shipped with both broken behind mocked selftests. Run: PYTHONPATH=. python3 tests/aggressive/test_real_http.py"""
import http.server, json, threading, unittest
from adsops import aeo_tools
from adsops.aeo import indexnow
from adsops import monitors

class H(http.server.BaseHTTPRequestHandler):
    seen = []
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        H.seen.append((self.path, self.headers.get("Content-Type"), body))
        code = 202 if self.path.startswith("/indexnow") else (500 if self.path == "/fail" else 200)
        self.send_response(code); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass

class RealHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def test_raw_http_bytes_and_status(self):
        st, body = aeo_tools.raw_http("POST", self.base + "/x", {"Content-Type": "application/json"}, b'{"a":1}')
        self.assertEqual((st, body), (200, "ok")); self.assertEqual(H.seen[-1][2], b'{"a":1}')
        self.assertEqual(aeo_tools.raw_http("POST", self.base + "/fail", {}, b"")[0], 500)   # status returned, not raised

    def test_indexnow_submit_over_a_real_socket(self):
        url, headers, body = indexnow.build_indexnow("adrails.dev", "361d4590f8065ef5cc8c94f941bf44cf", ["https://adrails.dev/"])
        # point the real submit function at the local server by wrapping raw_http (URL rewrite only)
        http_fn = lambda m, u, h, b: aeo_tools.raw_http(m, self.base + "/indexnow", h, b)
        r = indexnow.submit_indexnow("adrails.dev", "361d4590f8065ef5cc8c94f941bf44cf", ["https://adrails.dev/"], http_fn)
        self.assertTrue(r["ok"], r)
        sent = json.loads(H.seen[-1][2])
        self.assertEqual(sent["host"], "adrails.dev"); self.assertEqual(sent["urlList"], ["https://adrails.dev/"])

    def test_monitor_webhook_over_a_real_socket(self):
        # webhook_notifier requires https; exercise its http callable contract directly with the same raw_http
        notify = monitors.webhook_notifier("https://hooks.example/x", lambda m, u, h, b: aeo_tools.raw_http(m, self.base + "/hook", h, b))
        r = monitors.deliver("hello", notify)
        self.assertTrue(r["delivered"], r)
        self.assertEqual(json.loads(H.seen[-1][2])["text"], "hello")

if __name__ == "__main__":
    unittest.main(verbosity=1)
