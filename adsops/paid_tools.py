"""adsops_paid: optional paid services hosted by the AdRails team (2026-11-07).

The core of AdRails is free and runs on the owner's machine; nothing here is needed for it. Three extras cost us money to run
(model calls, Bitcoin anchoring), so they are paid per job, with no account and no subscription:

  panel   AI visibility panel: ChatGPT, Perplexity, Gemini and Claude answer the business's questions from clean sessions with live
          web search. The answers feed straight into aeo_visibility_record, so the history stays on the owner's machine.
  review  Second opinion: a different AI (OpenAI GPT-5) reviews a planned change before adsops_apply commit=true.
  stamp   Bitcoin timestamp: anchors SHA-256 digests (by default this install's audit-chain head) via OpenTimestamps. The proof
          files are saved locally, so anyone can later check the log existed unedited at that time (`ots verify`).

Payment: the API answers with a Bitcoin address + amount (any wallet) and an x402 URL (USDC on Base, for agent wallets).
THIS TOOL NEVER PAYS. The owner pays from their own wallet, or an x402-capable agent wallet pays within a budget the owner set.
Server code is not in this package; the API lives at https://adrails.dev/api/v1 (override: cfg "paid_api" or ADRAILS_API_BASE).
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

from .errors import ToolError

DEFAULT_API = "https://adrails.dev/api"
_JOB = re.compile(r"^[0-9a-f]{24}$")


def api_base(cfg):
    return (os.environ.get("ADRAILS_API_BASE") or cfg.get("paid_api") or DEFAULT_API).rstrip("/")


def _http(method, url, body=None, timeout=60):
    """-> (status, parsed JSON). 402 and 4xx bodies are returned, not raised: they carry the payment details or the reason."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "adrails-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": "HTTP %d" % e.code}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ToolError("the AdRails paid-services API is unreachable (%s); the free tools are unaffected" % type(e).__name__)


def register(srv, ctx, cfg, http=None):
    from .enterprise_tools import state_path
    from . import auditchain
    call = http or _http
    jobs_log = state_path(cfg, "paid_jobs", "paid-jobs.jsonl")
    proof_dir = os.path.join(os.path.dirname(jobs_log), "proofs")

    def _remember(job):
        try:
            with open(jobs_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": int(ctx.now()), "id": job.get("id"), "service": job.get("service"),
                                     "status": job.get("status")}) + "\n")
        except OSError:
            pass

    def _save_proofs(job):
        """Stamp results: write each proof to proofs/<digest>.ots (overwritten as the proof is upgraded)."""
        saved = []
        res = job.get("result") or {}
        items = list(res.get("proofs") or [])
        rc = res.get("receipt") or {}
        if rc.get("ots_b64") and rc.get("sha256"):
            items.append({"digest": rc["sha256"], "ots_b64": rc["ots_b64"]})
        for p in items:
            d, b = str(p.get("digest") or ""), p.get("ots_b64")
            if re.fullmatch(r"[0-9a-f]{64}", d) and b:
                os.makedirs(proof_dir, exist_ok=True)
                path = os.path.join(proof_dir, d + ".ots")
                with open(path, "wb") as fh:
                    fh.write(base64.b64decode(b))
                saved.append(path)
        return saved

    @srv.tool("adsops_paid", "OPTIONAL paid services from the AdRails team (the free tools never need them). action=catalog lists "
              "services and current prices. action=order {service, input} returns a quote: a Bitcoin address + amount and an x402 "
              "URL (USDC on Base). COSTS REAL MONEY: tell the owner the service and price and get a clear yes before anyone pays. "
              "This tool never pays; the owner pays from their wallet (or your x402 wallet pays, only within a budget the owner "
              "set). action=status {job_id} polls; when done it returns the result. Services: 'panel' (ChatGPT, Perplexity, Gemini "
              "and Claude answer up to 8 questions from clean sessions; then pass result.answers, brand and competitors to "
              "aeo_visibility_record), 'review' (a different AI reviews a plan before adsops_apply commit=true), 'stamp' (anchor "
              "digests in Bitcoin via OpenTimestamps; with no input it stamps this install's audit-chain head, and proofs are "
              "saved locally). Never put customer names, phones or emails in an input.",
              {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["catalog", "order", "status", "list"]},
                  "service": {"type": "string", "enum": ["panel", "review", "stamp"]},
                  "input": {"type": "object"}, "job_id": {"type": "string"}},
               "required": ["action"], "additionalProperties": False})
    def paid(args):
        a, base = args["action"], api_base(cfg)
        if a == "catalog":
            st, body = call("GET", base + "/v1")
            return body
        if a == "list":
            rows = []
            if os.path.isfile(jobs_log):
                for line in open(jobs_log, encoding="utf-8"):
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
            latest = {}
            for r in rows:
                latest[r.get("id")] = r
            return {"jobs": list(latest.values())[-50:]}
        if a == "order":
            svc = args.get("service")
            if not svc:
                raise ToolError("order needs service: panel, review or stamp")
            inp = args.get("input")
            if svc == "stamp" and not inp:
                ch = auditchain.AuditChain(state_path(cfg, "audit_chain", "audit-chain.jsonl"))
                if not os.path.isfile(ch._path):
                    raise ToolError("no audit chain yet on this install; pass input.digests (64-char SHA-256 hex) to stamp")
                inp = {"digests": [auditchain.anchor_digest(ch._path)["head_hash"]]}
            if not isinstance(inp, dict):
                raise ToolError("order needs input (an object); see action=catalog")
            st, body = call("POST", base + "/v1/jobs", {"service": svc, "input": inp})
            if st >= 400:
                raise ToolError("order refused (%d): %s" % (st, body.get("error") or body))
            _remember(body)
            return {**body, "next": "show the owner the price and BOTH ways to pay; after they pay, call action=status with "
                                    "job_id (Bitcoin: unconfirmed counts up to 20,000 sats)"}
        if a == "status":
            jid = str(args.get("job_id") or "")
            if not _JOB.match(jid):
                raise ToolError("job_id must be the 24-character id from action=order")
            st, body = call("GET", base + "/v1/jobs/" + jid)
            if st == 404:
                raise ToolError("no such job")
            if st >= 400:
                raise ToolError("status failed (%d): %s" % (st, body.get("error") or body))
            _remember(body)
            if body.get("status") == "done":
                saved = _save_proofs(body)
                if saved:
                    body = {**body, "proofs_saved": saved,
                            "note": "proofs stay pending until Bitcoin confirms them (a few hours); call status again later "
                                    "to save the final proofs"}
            return body
        raise ToolError("action must be catalog, order, status or list")


def selftest():
    import tempfile
    n = 0
    td = tempfile.mkdtemp(prefix="adrails-paid-")
    tools = {}

    class Srv:
        def tool(self, name, desc, schema):
            def deco(fn):
                tools[name] = fn
                return fn
            return deco

    class Ctx:
        now = staticmethod(time.time)

    seen = []
    proof = base64.b64encode(b"\x00OpenTimestamps").decode()

    def fake(method, url, body=None, timeout=60):
        seen.append((method, url, body))
        if url.endswith("/v1"):
            return 200, {"services": {"panel": {}, "review": {}, "stamp": {}}}
        if method == "POST":
            if body["service"] == "panel" and not body["input"].get("prompts"):
                return 400, {"error": "prompts required"}
            return 201, {"id": "a" * 24, "service": body["service"], "status": "awaiting_payment",
                         "pay": {"bitcoin": {"address": "bc1qx", "amount_sats": 1000}}}
        return 200, {"id": "a" * 24, "service": "stamp", "status": "done",
                     "result": {"proofs": [{"digest": "b" * 64, "ots_b64": proof}]}}

    cfg = {"audit_log": os.path.join(td, "audit.jsonl"), "paid_api": "https://example.test/api/"}   # never the real ~/.adsops
    register(Srv(), Ctx(), cfg, http=fake)
    t = tools["adsops_paid"]
    assert set(t({"action": "catalog"})["services"]) == {"panel", "review", "stamp"} and seen[-1][1] == "https://example.test/api/v1"; n += 1
    r = t({"action": "order", "service": "review", "input": {"plan": {"x": 1}}})
    assert r["status"] == "awaiting_payment" and "next" in r; n += 1
    try:
        t({"action": "order", "service": "panel", "input": {"brand": {"name": "Acme"}}})
        raise AssertionError("400 must raise")
    except ToolError as e:
        assert "prompts required" in str(e); n += 1
    for bad in ("../x", "A" * 24, ""):
        try:
            t({"action": "status", "job_id": bad})
            raise AssertionError("bad job id must be refused")
        except ToolError:
            n += 1
    s = t({"action": "status", "job_id": "a" * 24})
    assert s["proofs_saved"] and open(s["proofs_saved"][0], "rb").read() == b"\x00OpenTimestamps"; n += 1
    assert len(t({"action": "list"})["jobs"]) == 1; n += 1      # same id twice -> one row
    try:
        t({"action": "order", "service": "stamp"})            # no audit chain on a fresh install
        raise AssertionError("stamp with no chain and no digests must explain")
    except ToolError as e:
        assert "audit chain" in str(e); n += 1
    return n
