"""Integration QA for the ChatGPT Ads growth suite (oai_suite_tools) through the REAL server build: registration, the
enterprise harden() wrapper (RBAC/approval/audit chain), account modes, and privacy. Reuses qa_openai_ads.Env + FakeAPI.
Run: PYTHONPATH=. python3 tests/qa_openai_suite.py      the maintainers, 2026-11-06."""
import http.server, json, os, sys, threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qa_openai_ads as Q                                   # noqa: E402
from adsops import auditchain, oai_engine, oai_policy       # noqa: E402

N = [0, 0]


def check(cond, what):
    N[0] += 1
    if not cond:
        N[1] += 1
        print("FAIL", what)


PII = ["pat.owner@example.com", "8135550100", "+18135550100", "Owner"]
ROWS = [{"id": "L1", "created_at": "", "email": "Pat.Owner@Example.com ", "phone": "(813) 555-0100", "first_name": "Pat", "last_name": "Owner",
         "value": 149}, {"id": "L2", "created_at": "", "email": "sam@example.org", "value": 0}]


class Capi(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        Capi.seen.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def call(env, tool, **args):
    """Like Env.call, but tool arguments may include 'name' (Env.call's first parameter is called name)."""
    r = env.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})["result"]
    return (not r.get("isError")), r.get("structuredContent"), "".join(c.get("text", "") for c in r.get("content", []))


def main():
    os.environ["QA_OAI_KEY"] = Q.KEY
    e = Q.Env(mode="live")
    names = [t["name"] for t in e.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    for t in ("adsops_openai_ad_check", "adsops_openai_draft_campaign", "adsops_openai_ab_report", "adsops_openai_send_conversions",
              "adsops_openai_audience"):
        check(t in names, "registered: " + t)

    # ad check: policy from the campaign's policy file (allowed domains + required UTM)
    ok, r, txt = e.call("adsops_openai_ad_check", campaign="bt_chatgpt_owners",
                        ads=[{"title": "Tampa property management", "body": "One manager.", "target_url": "https://evil.example/x", "file_id": "file_1"},
                             {"title": "Tampa property management", "body": "One manager.", "target_url": "https://evil.example/x", "file_id": "file_1"}])
    check(ok and not r["ok"] and r["duplicates"], "ad_check flags foreign domain / missing UTM / duplicates: " + txt[:160])

    # draft: engine + policy accept, everything paused, activation refused, write never overwrites
    brief = {"key": "drafted_one", "ad_account_id": Q.AID, "business": "B", "landing_url": "https://acmepm.example/owners/", "offer": "x",
             "daily_budget": 30, "lifetime_cap": 400, "max_bid": 4, "countries": ["US"], "context_hints": ["property manager tampa"],
             "ads": [{"title": "Tampa property management", "body": "One manager from lease to repairs.", "file_id": "file_abc123"}],
             "utm_campaign": "c", "approvers": ["jane"]}
    ok, d, txt = e.call("adsops_openai_draft_campaign", brief=brief, write=True)
    check(ok, "draft ok: " + txt[:200])
    if ok:
        st = oai_engine.validate_state(d["state"]); pol = oai_policy.validate(d["policy"])
        check(st["campaign"]["status"] == "paused" and pol["allow_activate"] is False, "draft paused + activation off")
        check(os.path.isfile(d["written"]["state_file"]) and d["written"]["state_file"].startswith(e.td), "draft written beside config")
        ok2, _, t2 = e.call("adsops_openai_draft_campaign", brief=brief, write=True)
        check(not ok2 and "never overwrite" in t2, "draft never overwrites")

    # A/B report from rows
    ok, r, _ = e.call("adsops_openai_ab_report", rows=[{"id": "a", "name": "A", "impressions": 20000, "clicks": 600, "conversions": 20, "spend": 900},
                                                       {"id": "b", "name": "B", "impressions": 20000, "clicks": 300, "conversions": 9, "spend": 900}])
    check(ok and r["leader"] == "a" and r["recommend_pause"] == ["b"], "ab_report leader + pause only the significant loser")

    # conversions: validate-only by default, hashed, key never echoed, real socket
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Capi); threading.Thread(target=srv.serve_forever, daemon=True).start()
    import adsops.oai_conversions as C
    real_send = C.send
    C.send = lambda ev, pid, key, http, **kw: real_send(ev, pid, key, lambda m, u, h, b: http(m, "http://127.0.0.1:%d/v1/events?pid=%s" % (srv.server_address[1], pid), h, b), **kw)
    keyf = e.cfg_raw["accounts"][0]["capi_key_file"]; open(keyf, "w").write("capi-SECRET-123\n"); os.chmod(keyf, 0o600)
    now_iso = __import__("datetime").datetime.fromtimestamp(Q.NOW - 3600, __import__("datetime").timezone.utc).isoformat()
    rows = [dict(r, created_at=now_iso) for r in ROWS]
    ok, r, txt = e.call("adsops_openai_send_conversions", account="acme_openai", rows=rows, pixel_id="px_123")
    check(ok and r.get("validate_only") is True, "send_conversions defaults to validate_only: " + txt[:200])
    sent = b"".join(s[2] for s in Capi.seen).decode()
    check(Capi.seen and json.loads(Capi.seen[-1][2]).get("validate_only") is True, "validate_only true on the wire")
    check(not any(p.lower() in sent.lower() for p in PII), "no raw PII on the wire")
    check("capi-SECRET-123" not in txt, "CAPI key never echoed")
    os.chmod(keyf, 0o644)
    ok, _, t = e.call("adsops_openai_send_conversions", account="acme_openai", rows=rows, pixel_id="px_123")
    check(not ok and "chmod 600" in t, "world-readable key file refused")
    os.chmod(keyf, 0o600)

    # audiences: plan only by default; read_only account refuses commit; no raw PII in output
    ok, r, txt = call(e, "adsops_openai_audience", account="acme_openai", op="create", name="Current clients", rows=ROWS)
    check(ok and r["stats"]["rows_out"] == 2 and not any(p.lower() in txt.lower() for p in PII), "audience plan hashed, no PII: " + txt[:160])
    e2 = Q.Env(mode="read_only")
    ok, _, t = call(e2, "adsops_openai_audience", account="acme_openai", op="create", name="Current clients", rows=ROWS, commit=True)
    check(not ok and "mode=read_only" in t, "read_only account refuses audience commit")
    ok, _, t = e2.call("adsops_openai_send_conversions", account="acme_openai", rows=rows, pixel_id="px_123", commit=True)
    check(not ok and "mode=read_only" in t, "read_only account refuses conversion commit")

    # audit chain recorded the mutating suite calls (beside the config, never ~/.adsops), and verifies
    chain = os.path.join(e.td, "audit-chain.jsonl")
    check(os.path.isfile(chain) and auditchain.AuditChain(chain).verify()["ok"], "audit chain beside config and valid")
    body = open(chain).read() if os.path.isfile(chain) else ""
    check("adsops_openai_send_conversions" in body and "adsops_openai_audience" in body, "suite mutations audited")
    check(not any(p.lower() in body.lower() for p in PII) and "capi-SECRET-123" not in body, "no PII or key in the audit chain")
    # Live-API rule: insights end must be a full hour. Q.NOW is exactly 15:00, which can't exercise it, so run at 15:20:34.
    saved = Q.NOW
    Q.NOW = saved + 20 * 60 + 34
    try:
        e3 = Q.Env(mode="live")
        _, (okc, _, tc) = e3.plan_apply()                     # create the campaign so status reads real spend
        check(okc, "off-hour: campaign created: " + tc[:120])
        e3.api.requests.clear()
        ok, _, t = e3.call("adsops_openai_status", campaign="bt_chatgpt_owners")
        ends = [int(json.loads(tr)["end"]) for r in e3.api.requests if r["path"].endswith("/insights") for tr in r["q"].get("time_ranges[]", [])]
        check(ok and ends and all(x % 3600 == 0 and x >= Q.NOW for x in ends), "off-hour status: insights ends rounded UP to full hours: " + t[:120])
    finally:
        Q.NOW = saved
    print("QA: %d checks, %d failures" % (N[0], N[1]))
    return 1 if N[1] else 0


if __name__ == "__main__":
    sys.exit(main())
