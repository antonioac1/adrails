"""AEO tools for AdRails: answer-engine optimisation as MCP tools (the maintainers glue, 2026-11-05).

AdRails is the only ads server that also tells you whether AI answer engines can read, extract and cite your site,
and turns your paid-search terms into the questions your pages should answer. Every tool is READ-ONLY except
aeo_indexnow with submit=true (it pings the IndexNow API with public URLs and the public key; nothing private).

Fetching: safe_fetch() resolves the host and refuses private, loopback, link-local and reserved addresses (on every
redirect hop too). This closes the DNS-rebinding/SSRF hole a plain hostname check leaves. Bodies are capped at 3 MB.
Logic lives in adsops/aeo/* (DeepSeek-built from specs/ent/aeo_*.md, Lane-A reviewed); this file only wires them.
"""
import datetime as dt
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from adsops.aeo import ads_to_aeo, ai_traffic, crawl_audit, indexnow, llms_txt, page_audit, schema_audit, visibility
from adsops.errors import ToolError
from adsops.platforms import google_ads

UA = "AdRails-AEO/1.0 (+https://adrails.dev; read-only site audit)"
MAX_BODY = 3_000_000


def raw_http(method, url, headers, body):
    """(method, url, headers, body bytes|str|None) -> (status:int, body:str). Never raises on HTTP status (callers interpret it).

    The contract IndexNow and the monitor webhooks expect. google_ads.urllib_http is a JSON client (dict in, parsed JSON out),
    and passing it raw bytes broke both in 0.4.0 (found by dogfooding on the live site, 2026-11-05; fixed in 0.4.1)."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(str(url), method=str(method).upper(), data=data, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read(65536).decode("utf-8", "replace")
        except Exception:
            text = ""
        return e.code, text
    except urllib.error.URLError as e:
        raise ToolError("network error: %s" % (getattr(e, "reason", None) or "connection failed")) from None


def _check_host(host, resolver=socket.getaddrinfo):
    if not host or host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localhost")):
        raise ToolError("refusing to fetch a local host")
    try:
        infos = resolver(host, None)
    except socket.gaierror:
        raise ToolError(f"cannot resolve {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ToolError(f"refusing to fetch {host}: it resolves to a non-public address")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None                                      # we follow redirects ourselves, re-checking each hop


def safe_fetch(url, resolver=socket.getaddrinfo, opener=None, max_hops=5):
    """-> (status, headers{lower: value}, body str). Public http(s) only; every redirect hop is re-validated."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(max_hops + 1):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https"):
            raise ToolError("only http(s) URLs can be fetched")
        _check_host(u.hostname, resolver)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,application/xml,*/*"})
        try:
            resp = opener.open(req, timeout=15)
            status, headers, raw = resp.status, resp.headers, resp.read(MAX_BODY)
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, (e.read(MAX_BODY) if hasattr(e, "read") else b"")
        hdrs = {k.lower(): v for k, v in (headers.items() if headers else [])}
        if status in (301, 302, 303, 307, 308) and hdrs.get("location"):
            url = urllib.parse.urljoin(url, hdrs["location"])
            continue
        return status, hdrs, raw.decode("utf-8", "replace")
    raise ToolError("too many redirects")


class _Outline(HTMLParser):
    """Headings + visible text, for coverage checks."""
    SKIP = {"script", "style", "nav", "footer", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.headings, self.text, self._skip, self._h = [], [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("h1", "h2", "h3"):
            self._h = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in ("h1", "h2", "h3") and self._h is not None:
            self.headings.append(" ".join("".join(self._h).split()))
            self._h = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._h is not None:
            self._h.append(data)
        self.text.append(data)


def outline(url, html):
    p = _Outline()
    p.feed(html)
    return {"url": url, "headings": p.headings, "text": " ".join(" ".join(p.text).split())}


def _fetch_ok(fetch, url):
    status, headers, body = fetch(url)
    if status != 200:
        raise ToolError(f"{url} returned HTTP {status}")
    return headers, body


def register(srv, ctx, cfg, fetch=None):
    fetch = fetch or safe_fetch
    url_prop = {"url": {"type": "string", "description": "absolute https URL"}}

    def schema(props, req):
        return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}

    @srv.tool("aeo_site_audit", "Read-only. Can AI answer engines reach this site? Checks robots.txt rules for 18 AI and search "
              "crawlers (GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, Claude-User, PerplexityBot, Google-Extended, Bingbot, "
              "Applebot...), llms.txt, the sitemap, and the homepage's noindex/canonical/body-text signals. Returns a 0-100 score "
              "and ranked fixes.", schema(url_prop, ["url"]))
    def site_audit(args):
        return crawl_audit.audit_site(args["url"], fetch)

    @srv.tool("aeo_page_audit", "Read-only. Answer-readiness of one page: answer-first paragraph, question headings, FAQ, structured "
              "data, specifics, freshness, citations, scannability, plus a JSON-LD audit (required/recommended fields per type) and "
              "crawler directives. Returns scores, extractable answer blocks, and missing questions.",
              schema({**url_prop, "questions": {"type": "array", "items": {"type": "string"}}, "brand": {"type": "string"}}, ["url"]))
    def page(args):
        headers, html = _fetch_ok(fetch, args["url"])
        today = dt.date.fromtimestamp(ctx.now()) if ctx else None
        return {"answer_readiness": page_audit.analyze(html, args["url"], args.get("questions") or (), args.get("brand"), today),
                "structured_data": schema_audit.audit_html(html, today),
                "crawl": crawl_audit.audit_page(args["url"], html, headers)}

    @srv.tool("aeo_llms_txt", "Read-only. Validate a site's /llms.txt (llmstxt.org format), or build one: pass pages "
              "[{url,title,description,section}] with title and summary and it returns a ready file that passes validation.",
              schema({**url_prop, "title": {"type": "string"}, "summary": {"type": "string"},
                      "pages": {"type": "array", "items": {"type": "object"}}}, []))
    def llms(args):
        if args.get("pages"):
            text = llms_txt.build_from_pages(args.get("title") or "Site", args.get("summary") or "", args["pages"])
            return {"llms_txt": text, "validation": llms_txt.validate(text)}
        if not args.get("url"):
            raise ToolError("pass url (to validate) or pages (to build)")
        u = urllib.parse.urlparse(args["url"])
        target = args["url"] if u.path.endswith(".txt") else f"{u.scheme}://{u.netloc}/llms.txt"
        _, text = _fetch_ok(fetch, target)
        return {"url": target, "bytes": len(text.encode()), "validation": llms_txt.validate(text, f"{u.scheme}://{u.netloc}"),
                "parsed": {k: v for k, v in llms_txt.parse(text).items() if k != "sections"},
                "sections": [{"name": s["name"], "links": len(s["links"])} for s in llms_txt.parse(text)["sections"]]}

    @srv.tool("aeo_schema_generate", "Pure. Generate valid JSON-LD for a page: kind = faq (qas [[q,a]...]), software (name,url,"
              "description,price), org (name,url,logo,same_as) or breadcrumbs (trail [[name,url]...]). Returns the object, its own "
              "audit, and a ready <script> tag.",
              schema({"kind": {"type": "string"}, "data": {"type": "object"}}, ["kind", "data"]))
    def gen(args):
        d, k = args["data"], args["kind"]
        makers = {"faq": lambda: schema_audit.make_faq([tuple(x) for x in d.get("qas", [])]),
                  "software": lambda: schema_audit.make_software(d["name"], d["url"], d.get("description", ""),
                                                                price=str(d.get("price", "0"))),
                  "org": lambda: schema_audit.make_org(d["name"], d["url"], d.get("logo"), tuple(d.get("same_as", ()))),
                  "breadcrumbs": lambda: schema_audit.make_breadcrumbs([tuple(x) for x in d.get("trail", [])])}
        if k not in makers:
            raise ToolError("kind must be faq | software | org | breadcrumbs")
        try:
            obj = makers[k]()
        except KeyError as e:
            raise ToolError(f"missing field {e}") from None
        return {"jsonld": obj, "audit": schema_audit.audit_objects([obj]), "script_tag": schema_audit.to_script_tag(obj)}

    @srv.tool("aeo_visibility_record", "Record an AI share-of-answer panel. YOU (the calling agent) ask each engine the prompts and "
              "pass the answers here [{engine,prompt,text,citations}]; AdRails scores mentions, rank vs competitors, citations and "
              "sentiment, stores the run, and returns the score plus the change since the last run.",
              schema({"answers": {"type": "array", "items": {"type": "object"}}, "brand": {"type": "object"},
                      "competitors": {"type": "object"}, "run_id": {"type": "string"}}, ["answers", "brand"]))
    def vis_record(args):
        table = {}
        for a in args["answers"]:
            table.setdefault((str(a.get("engine")), str(a.get("prompt"))), []).append(
                {"text": str(a.get("text", "")), "citations": list(a.get("citations") or [])})
        cursor = {}

        def ask(engine, prompt):
            i = cursor.get((engine, prompt), 0)
            cursor[(engine, prompt)] = i + 1
            return table[(engine, prompt)][i % len(table[(engine, prompt)])]

        prompts = sorted({p for _, p in table})
        engines = sorted({e for e, _ in table})
        reps = max(len(v) for v in table.values()) if table else 1
        res = visibility.run_panel(prompts, engines, ask, args["brand"], args.get("competitors") or {}, repeats=reps)
        path = os.path.expanduser((cfg.get("aeo") or {}).get("visibility_log", "~/.adsops/aeo-visibility.jsonl"))
        hist = visibility.load_history(path) if os.path.isfile(path) else []
        now_iso = dt.datetime.fromtimestamp(ctx.now(), dt.timezone.utc).isoformat()
        visibility.store(path, args.get("run_id") or now_iso, res["results"], now_iso)
        cur = visibility.score(res["results"])
        prev = visibility.score(hist[-1]["results"]) if hist and hist[-1].get("results") else None
        return {"score": cur, "changes": visibility.diff(prev, cur) if prev else [], "errors": res["errors"]}

    @srv.tool("aeo_indexnow", "Tell Bing, Yandex, Seznam and Naver (IndexNow) that URLs changed. Checks the key file at "
              "https://host/<key>.txt first. submit=false (default) only validates. Optionally pass sitemap_since (ISO date) to send "
              "every sitemap URL modified since then.",
              schema({"host": {"type": "string"}, "key": {"type": "string"}, "urls": {"type": "array", "items": {"type": "string"}},
                      "sitemap_since": {"type": "string"}, "submit": {"type": "boolean"}}, ["host", "key"]))
    def idx(args):
        host, key = args["host"], indexnow.validate_key(args["key"])
        urls = list(args.get("urls") or [])
        if args.get("sitemap_since"):
            _, sm = _fetch_ok(fetch, f"https://{host}/sitemap.xml")
            ch = indexnow.changed_urls(sm, args["sitemap_since"])
            urls += ch if isinstance(ch, list) else ch.get("urls", [])
        if not urls:
            raise ToolError("no URLs to submit")
        keyfile = indexnow.check_key_file(host, key, lambda u: fetch(u)[::2])
        url, headers, body = indexnow.build_indexnow(host, key, urls)
        out = {"urls": len(body["urlList"]), "key_file": keyfile, "submitted": False}
        if args.get("submit"):
            if not keyfile.get("ok"):
                raise ToolError("key file check failed; IndexNow would reject the submission")
            out.update(indexnow.submit_indexnow(host, key, body["urlList"], raw_http))
            out["submitted"] = True
        return out

    @srv.tool("aeo_ai_traffic", "Read-only. Measure AI-driven traffic from a local web-server log (nginx combined or JSON lines): "
              "referrals from ChatGPT, Perplexity, Claude, Gemini and Copilot with landing pages, and AI crawler fetches split into "
              "verified / unverified / spoofed (by the providers' published IP ranges and scanner paths).",
              schema({"log_path": {"type": "string"}, "format": {"type": "string"}, "ranges_file": {"type": "string"},
                      "site": {"type": "string"}, "max_lines": {"type": "integer"}}, ["log_path"]))
    def traffic(args):
        p = os.path.expanduser(args["log_path"])
        if not os.path.isfile(p):
            raise ToolError("log file not found")
        ranges = None
        if args.get("ranges_file"):
            ranges = ai_traffic.load_ranges(json.load(open(os.path.expanduser(args["ranges_file"]))))
        entries, cap = [], min(int(args.get("max_lines", 500_000)), 2_000_000)
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= cap:
                    break
                e = ai_traffic.parse_line(line, args.get("format", "combined"))
                if e:
                    entries.append(e)
        s = ai_traffic.summarize(entries, ranges, args.get("site", ""))
        s["line"] = ai_traffic.weekly_line(s)
        return s

    @srv.tool("aeo_content_gaps", "Read-only. The ads-to-answers bridge: turn the campaign's real paid search terms into the questions "
              "people ask, check which of your pages already answer them, and return content briefs for the biggest gaps (facts "
              "only from site_facts, never invented).",
              schema({"campaign": {"type": "string"}, "pages": {"type": "array", "items": {"type": "string"}},
                      "days": {"type": "integer"}, "city": {"type": "string"}, "top": {"type": "integer"},
                      "site_facts": {"type": "object"}}, ["campaign", "pages"]))
    def gaps(args):
        if not ctx:
            raise ToolError("no ads context")
        _, _, _, eff, _, client = ctx.open(args["campaign"])
        d = args.get("days", 30)
        if not 1 <= d <= 90:
            raise ToolError("days must be 1..90")
        until = dt.date.fromtimestamp(ctx.now())
        rows = client.search("SELECT search_term_view.search_term, metrics.impressions, metrics.clicks, metrics.cost_micros, "
                             "metrics.conversions FROM search_term_view WHERE campaign.id = %d AND segments.date BETWEEN '%s' AND '%s'"
                             % (int(eff["campaign_id"]), (until - dt.timedelta(days=d)).isoformat(), until.isoformat()))
        terms = [{"term": (r.get("searchTermView") or {}).get("searchTerm", ""), "impressions": float((r.get("metrics") or {}).get("impressions") or 0),
                  "clicks": float((r.get("metrics") or {}).get("clicks") or 0), "cost": float((r.get("metrics") or {}).get("costMicros") or 0) / 1e6,
                  "conversions": float((r.get("metrics") or {}).get("conversions") or 0)} for r in rows]
        pages = []
        for u in args["pages"][:25]:
            try:
                pages.append(outline(u, _fetch_ok(fetch, u)[1]))
            except ToolError:
                continue
        return ads_to_aeo.roadmap(terms, pages, top=min(args.get("top", 10), 25), site_facts=args.get("site_facts"))


def selftest():
    from adsops import protocol
    n = 0
    # SSRF guard: private resolution is refused, public allowed
    priv = lambda h, p: [(2, 1, 6, "", ("10.0.0.5", 0))]
    pub = lambda h, p: [(2, 1, 6, "", ("93.184.216.34", 0))]
    for bad in (lambda: _check_host("evil.example", priv), lambda: _check_host("localhost"), lambda: safe_fetch("file:///etc/passwd")):
        try:
            bad()
            raise AssertionError("not refused")
        except ToolError:
            n += 1
    _check_host("example.com", pub); n += 1

    class Resp:
        def __init__(self, status, headers, body):
            self.status, self.headers, self._b = status, headers, body

        def read(self, n):
            return self._b

    class Opener:                       # first hop redirects to a host that resolves privately
        def open(self, req, timeout=None):
            if "first" in req.full_url:
                return Resp(302, {"Location": "https://inner.example/x"}, b"")
            return Resp(200, {}, b"secret")

    resolver = lambda h, p: priv(h, p) if h == "inner.example" else pub(h, p)
    try:
        safe_fetch("https://first.example/", resolver, Opener())
        raise AssertionError("redirect to private host followed")
    except ToolError:
        n += 1

    site = {"https://a.example/robots.txt": (200, {}, "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\nSitemap: https://a.example/sitemap.xml\n"),
            "https://a.example/llms.txt": (200, {}, "# A\n> Ads tool.\n## Docs\n- [Home](https://a.example/): start\n"),
            "https://a.example/sitemap.xml": (200, {}, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                                      "<url><loc>https://a.example/</loc><lastmod>2026-11-04</lastmod></url></urlset>"),
            "https://a.example/": (200, {"content-type": "text/html"}, "<html lang=en><head><title>A</title><meta name=description content='x'>"
                                   "<link rel=canonical href='https://a.example/'></head><body><h1>What is A?</h1><p>" + "A is an ads tool. " * 30 +
                                   "</p><h2>How much does A cost?</h2><p>Free.</p></body></html>"),
            "https://a.example/a1b2c3d4e5.txt": (200, {}, "a1b2c3d4e5")}
    fetch = lambda u: site.get(u, (404, {}, ""))
    srv = protocol.Server("t", "0")

    class Ctx:
        now = staticmethod(lambda: 1793880000.0)

        def open(self, key):
            class C:
                def search(self, q):
                    return [{"searchTermView": {"searchTerm": "property management cost tampa"},
                             "metrics": {"impressions": "40", "clicks": "3", "costMicros": "9000000", "conversions": 0}}]
            return {}, {}, {}, {"campaign_id": 1}, {}, C()

    tmp = __import__("tempfile").mkdtemp()
    register(srv, Ctx(), {"aeo": {"visibility_log": os.path.join(tmp, "v.jsonl")}}, fetch=fetch)

    def call(name, a):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": a}})["result"]
        assert not r["isError"], (name, r["content"][0]["text"][:300])
        return r["structuredContent"]

    s = call("aeo_site_audit", {"url": "https://a.example/"})
    gpt = [b for b in s["robots"]["bots"] if b["bot"] == "GPTBot"][0]
    assert gpt["allowed"]["/"] is False and isinstance(s["score"], int); n += 1
    p = call("aeo_page_audit", {"url": "https://a.example/", "questions": ["Is A secure?"]})
    assert "score" in p["answer_readiness"] and "Is A secure?" in p["answer_readiness"]["suggested_questions"]; n += 1
    assert call("aeo_llms_txt", {"url": "https://a.example/"})["validation"]["ok"]; n += 1
    g = call("aeo_schema_generate", {"kind": "faq", "data": {"qas": [["What is A?", "A is an ads tool for agents with guardrails."]]}})
    assert g["jsonld"]["@type"] == "FAQPage" and "<\\/" not in g["script_tag"][:30]; n += 1
    ans = [{"engine": "chatgpt", "prompt": "best google ads mcp", "text": "Try AdRails or Adspirer.", "citations": ["https://adrails.dev/"]},
           {"engine": "claude", "prompt": "best google ads mcp", "text": "Adspirer is popular.", "citations": []}]
    v = call("aeo_visibility_record", {"answers": ans, "brand": {"name": "AdRails", "domains": ["adrails.dev"]},
                                       "competitors": {"Adspirer": ["adspirer.com"]}})
    assert abs(v["score"]["share_of_answer"] - 0.5) < 1e-9; n += 1
    v2 = call("aeo_visibility_record", {"answers": ans[:1], "brand": {"name": "AdRails", "domains": ["adrails.dev"]}})
    assert v2["score"]["share_of_answer"] == 1.0 and isinstance(v2["changes"], list); n += 1
    i = call("aeo_indexnow", {"host": "a.example", "key": "a1b2c3d4e5", "urls": ["https://a.example/"]})
    assert i["submitted"] is False and i["key_file"]["ok"] and i["urls"] == 1; n += 1
    lp = os.path.join(tmp, "access.log")
    open(lp, "w").write('1.2.3.4 - - [05/Nov/2026:10:00:00 +0000] "GET /pricing/ HTTP/1.1" 200 512 "https://chatgpt.com/" "Mozilla/5.0"\n'
                        '5.6.7.8 - - [05/Nov/2026:10:01:00 +0000] "GET /.env HTTP/1.1" 404 0 "-" "GPTBot/1.1"\n')
    t = call("aeo_ai_traffic", {"log_path": lp})
    assert t["ai_referrals"].get("chatgpt") == 1 and t["spoofed_total"] >= 1; n += 1
    c = call("aeo_content_gaps", {"campaign": "x", "pages": ["https://a.example/"], "top": 3})
    assert "gaps" in c; n += 1
    return n
