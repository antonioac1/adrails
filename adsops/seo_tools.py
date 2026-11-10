"""Technical SEO site audit (AdRails 0.11, 2026-11-10): an agent says "audit this site" and gets the prioritised issue table.

Four tools over the modules in adsops/seo/ (DeepSeek-built from SPEC-B, PM-reviewed line by line):
  seo_audit_run       crawl one site (Googlebot user-agent by default, sitemap seeding, robots.txt honoured, redirects followed
                      hop by hop), run every check, optionally JS render parity (Playwright), Lighthouse per template and the
                      Search Console join; store the run locally and diff it against the previous run of the same host.
  seo_audit_issues    read a stored run's issue table (JSON, Markdown or CSV), filtered by priority or code.
  seo_audit_diff      what changed between two stored runs: new and fixed issues, regressions (new noindex, broken
                      canonical, lost inlinks, LCP jump, status errors).
  seo_gsc_exclusions  Search Console URL Inspection for a stored run's pages: which pages Google excludes and why, next to
                      what the crawl itself says about each page.

Read-only towards every website: GET requests only, nothing is ever posted to a site. Polite by default: one request per
second, at most 200 pages, same host (and its www twin) only, robots.txt honoured, 3 MB and 15 s per response, redirects
re-checked on every hop. Fetching reuses the AEO SSRF guard (aeo_tools.public_addresses): public addresses only, the TCP
connection is pinned to the checked address, default ports only. Runs live in `seo-audits/` beside the config's audit log
(cfg "seo_audit_dir" overrides); nothing leaves the machine except the crawl itself and, when asked, the read-only Google calls.

CLI: `adsops-mcp site-audit --url https://example.com/ [--max-pages N] [--format md|json|csv] ...` (same engine, prints the table).
"""
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse

from .errors import ToolError, scrub
from .seo import adguard, checks_content, checks_links, checks_site, crawl as crawl_mod, extract, fetcher, issues, runs

MAX_PAGES_TOOL = 2000
DEFAULT_MAX_PAGES = 200
MIN_RATE_TOOL = 0.5
MAX_AD_URLS = 500
_UA_OK = re.compile(r"^[\x20-\x7e]{1,300}$")


def store_dir(cfg):
    from .enterprise_tools import state_path
    return state_path(cfg or {}, "seo_audit_dir", "seo-audits")


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _page_snapshot(crawl, graph, perf_by_url):
    """Per-page facts kept in the stored run (for diff and the GSC join): small, no page text."""
    out = {}
    for url, p in (crawl.get("pages") or {}).items():
        doc = p.get("doc") or {}
        meta_ni = any("noindex" in (r or "") or (r or "").strip() == "none" for r in doc.get("robots") or [])
        head_ni = "noindex" in (p.get("headers") or {}).get("x-robots-tag", "").lower()
        noindex = meta_ni or head_ni
        out[url] = {"status": p.get("status"), "final": p.get("final"), "ctype": p.get("ctype"),
                    "indexable": checks_content.is_indexable(p), "noindex": bool(noindex),
                    "noindex_header": bool(head_ni),
                    "canonical": (doc.get("canonicals") or [None])[0], "why": checks_content.why_not_indexable(p),
                    "inlinks": (graph.get("inlinks") or {}).get(url), "depth": p.get("depth"),
                    "in_sitemap": bool(p.get("in_sitemap")),
                    "lcp_ms": ((perf_by_url.get(url) or {}).get("metrics") or {}).get("lcp_ms")}
    return out


def _crawl_from_run(run):
    """Rebuild the minimal crawl shape the GSC join needs from a stored run's page snapshot."""
    pages = {}
    for url, s in (run.get("pages") or {}).items():
        doc = None
        if s.get("status") == 200 and (s.get("ctype") or "").startswith(("text/html", "application/xhtml")):
            doc = {"robots": ["noindex"] if s.get("noindex") else [], "canonicals": [s["canonical"]] if s.get("canonical") else []}
        if doc is not None and s.get("noindex_header"):
            doc["robots"] = []                     # the noindex came from the X-Robots-Tag header: keep that reason
        pages[url] = {"url": url, "status": s.get("status"), "final": s.get("final") or url, "ctype": s.get("ctype") or "",
                      "headers": {"x-robots-tag": "noindex"} if s.get("noindex_header") else {}, "doc": doc,
                      "depth": s.get("depth"), "in_sitemap": s.get("in_sitemap")}
    return {"settings": run.get("settings") or {}, "pages": pages}


def gsc_client_for(cfg):
    """A read-only Search Console client from the deployment's Google credential (same path as adsops_search_console)."""
    from .metrics import gsc
    from .metrics_tools import _gsc_http, _gsc_tokens
    return gsc.Client(_gsc_tokens(cfg or {}), _gsc_http)


def audit(url, cfg=None, *, max_pages=DEFAULT_MAX_PAGES, rate_limit_s=1.0, user_agent=fetcher.GOOGLEBOT_UA,
          respect_robots=True, sitemap_seeding=True, ad_urls=(), ad_states=True, render=False, render_pages=5,
          lighthouse=False, lighthouse_pages=5, gsc=False, gsc_site=None, gsc_client=None, max_inspect=50,
          fetch=None, sleep=time.sleep, now=time.time, store=None, perf_runner=None, render_factory=None,
          render_allow=None, starved_threshold=3, today=None):
    """Run the whole audit and return the stored run (+ 'diff' vs the previous run of the host, + 'files')."""
    start = extract.normalize(url) if isinstance(url, str) else ""
    if not start:
        raise ToolError("url must be an absolute http(s) URL")
    if not isinstance(user_agent, str) or not _UA_OK.match(user_agent):
        raise ToolError("user_agent must be 1-300 printable ASCII characters")
    if not isinstance(rate_limit_s, (int, float)) or not math.isfinite(rate_limit_s):
        raise ToolError("rate_limit_s must be a number of seconds")
    if len(ad_urls or ()) > MAX_AD_URLS:
        raise ToolError("at most %d ad_urls" % MAX_AD_URLS)
    if fetch is None:                                    # the real thing: public addresses only, pinned connection
        fetcher.check_url(start)
    notes, passed, findings = [], [], []

    # ad landing pages: explicit list + the desired-state files this deployment already manages
    ads = [u for u in (ad_urls or []) if isinstance(u, str) and len(u) <= 2048]
    if ad_states and cfg:
        from_cfg = adguard.final_urls_from_config(cfg)
        ads += from_cfg["urls"][:MAX_AD_URLS]
        notes += from_cfg["errors"]
    scope = {_host(start), _host(start)[4:] if _host(start).startswith("www.") else "www." + _host(start)}
    ads = [u for u in dict.fromkeys(extract.normalize(a) for a in ads) if u and _host(u) in scope]

    cr = crawl_mod.crawl(start, fetch=fetch, user_agent=user_agent, max_pages=max_pages, rate_limit_s=rate_limit_s,
                         respect_robots=respect_robots, sitemap_seeding=sitemap_seeding, extra_urls=ads, sleep=sleep,
                         now=now)
    notes += cr.get("notes") or []
    for mod in (checks_site, checks_content):
        f, p = mod.run(cr)
        findings += f
        passed += p
    f, p = checks_links.run(cr, starved_threshold=starved_threshold)
    findings += f
    passed += p
    if ads:
        f, p = adguard.run(cr, ads)
        findings += f
        passed += p
    ad_summary = adguard.summary(cr, ads) if ads else {"ad_urls": 0}

    # optional: JS render parity
    render_out = {"ran": False}
    if render:
        from .seo import perf as perf_mod, render as render_mod
        ok, note = render_mod.available() if render_factory is None else (True, "")
        if not ok:
            notes.append(note)
        else:
            picked = perf_mod.pick_pages(cr, max_pages=render_pages)
            try:
                if render_factory is None:            # real browser: in a child process with a hard time limit
                    rendered = render_mod.render_isolated(picked, user_agent=user_agent, max_pages=render_pages)
                else:
                    rendered = render_mod.render_pages(picked, user_agent=user_agent, max_pages=render_pages,
                                                       playwright_factory=render_factory, allow=render_allow)
                f, p = render_mod.parity(cr, rendered)
                findings += f
                passed += p
                render_out = {"ran": True, "pages": len(rendered),
                              "errors": {u: r.get("error") for u, r in rendered.items() if r.get("error")}}
            except ToolError as e:
                notes.append("JS render parity skipped: " + scrub(str(e), 200))
    else:
        notes.append("JS render parity not run (render=true to compare raw vs rendered links and text)")

    # optional: Lighthouse per template
    perf_results, perf_by_url, perf_out = [], {}, {"ran": False}
    if lighthouse:
        from .seo import perf as perf_mod
        backend = perf_mod.choose_backend(cfg)
        if backend["kind"] == "none":
            notes.append(backend["note"])
        else:
            for u in perf_mod.pick_pages(cr, max_pages=lighthouse_pages):
                try:
                    if backend["kind"] == "cli":
                        kw = {"runner": perf_runner} if perf_runner else {}
                        chrome = ((cfg or {}).get("seo") or {}).get("chrome_path")
                        lhr = perf_mod.run_cli(u, backend["cmd"], chrome_path=chrome, **kw)
                    else:
                        from .metrics_tools import _gsc_http
                        lhr = perf_mod.run_psi(u, backend["key"], _gsc_http)
                    res = perf_mod.analyze_lhr(lhr, (cr["pages"].get(u) or {}).get("doc"))
                    res["url"] = u
                    perf_results.append(res)
                    perf_by_url[u] = res
                except ToolError as e:
                    notes.append("Lighthouse: " + scrub(str(e), 200))
            if perf_results:
                f, p = perf_mod.findings(perf_results)
                findings += f
                passed += p
            perf_out = {"ran": bool(perf_results), "backend": backend["kind"],
                        "pages": [{"url": r["url"], "score": r.get("score"), "metrics": r.get("metrics"),
                                   "lcp_element": r.get("lcp_element"), "preload_mismatch": r.get("preload_mismatch"),
                                   "opportunities": r.get("opportunities", [])[:8]} for r in perf_results]}
    else:
        notes.append("Lighthouse not run (lighthouse=true; needs a local lighthouse CLI or a PageSpeed API key)")

    # optional: Search Console join
    gsc_out = {"connected": False, "note": "Search Console not requested (gsc=true)"}
    if gsc:
        from .seo import gsc_join
        client = gsc_client
        if client is None:
            try:
                client = gsc_client_for(cfg)
            except ToolError as e:
                client = None
                notes.append("Search Console not connected: " + scrub(str(e), 200))
        if client is not None:
            gsc_out = gsc_join.join(cr, client, site=gsc_site, today=today, max_inspect=max_inspect)
            findings += gsc_out.get("findings") or []
            passed += gsc_out.get("passed") or []
        else:
            gsc_out = gsc_join.join(cr, None)

    rows = issues.build_table(findings)
    graph = checks_links.graph(cr)
    stats = checks_content.stats(cr)
    host = _host(start)
    summary = {**issues.summarize(rows), "pages_crawled": len(cr["pages"]), "html_pages": stats["html_pages"],
               "indexable": stats["indexable"], "indexable_share": stats["indexable_share"],
               "templates": stats["templates"], "sitemap_urls": len(cr["sitemap"]["urls"]),
               "robots_disallowed_skipped": len(cr["skipped"]["robots_disallowed"]),
               "cap_reached": cr["skipped"]["cap_reached"], "passed": sorted(set(passed)), "ad_landing_pages": ad_summary}
    run = {"host": host, "settings": cr["settings"], "summary": summary, "rows": rows,
           "schema_matrix": stats["schema_matrix"], "pages": _page_snapshot(cr, graph, perf_by_url),
           "probes": cr["probes"], "robots": {k: v for k, v in cr["robots"].items() if k != "text"},
           "render": render_out, "lighthouse": perf_out,
           "gsc": {k: v for k, v in gsc_out.items() if k not in ("findings", "pages")}, "notes": notes}
    st = store or runs.RunStore(store_dir(cfg), now=now)
    prev = st.latest(host)
    run_id = st.save(run)
    run["diff"] = runs.diff(prev, run)
    run["files"] = _write_reports(st, host, run_id, run)
    return run


def _write_reports(st, host, run_id, run):
    """Markdown + CSV beside the stored JSON (mode 0600, same directory as the run)."""
    d = os.path.join(st.base_dir, runs.safe_host(host))
    out = {"json": os.path.join(d, run_id + ".json")}
    title = "Site audit: %s (%s)" % (host, run_id)
    for ext, text in (("md", issues.to_markdown(run["rows"], title=title, summary=run["summary"])),
                      ("csv", issues.to_csv(run["rows"]))):
        path = os.path.join(d, run_id + "." + ext)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        out[ext] = path
    return out


def _filter_rows(rows, priority=None, code=None, limit=50):
    out = [r for r in rows if (priority is None or r.get("priority") <= priority) and (not code or r.get("code") == code)]
    return out[:max(1, min(int(limit or 50), 500))]


def _brief_row(r):
    return {k: r.get(k) for k in ("priority", "hours", "issue", "detail", "recommendation", "example_url", "pages_affected",
                                   "template", "status", "code", "id")}


def _resolve_host(args):
    h = args.get("host") or (_host(extract.normalize(args.get("url") or "")) if args.get("url") else "")
    if not h:
        raise ToolError("pass host (example.com) or url")
    return runs.safe_host(h)


def register(srv, ctx, cfg, gsc_client=None, fetch=None, sleep=time.sleep):
    now = (ctx.now if ctx else time.time)

    def store():
        return runs.RunStore(store_dir(cfg), now=now)

    @srv.tool("seo_audit_run", "Read-only technical SEO audit of one website, as a senior SEO would do it: crawls the site the way "
              "Googlebot does (sitemap seeding, robots.txt honoured, redirect chains recorded, same host only, polite: 1 request/s, "
              "max_pages default 200), groups pages by URL template and checks indexability, sitemap vs crawl, titles/H1/meta, "
              "near-duplicates, canonicals, doctype/lang, hreflang, click depth, internal PageRank and starved pages, template "
              "hard-coded sibling links, internal links carrying utm_ tags (they corrupt ad attribution), the ad landing pages from "
              "your Google Ads/ChatGPT Ads state files, schema coverage per template and security headers. Optional: render=true "
              "(JS render parity, needs Playwright), lighthouse=true (one page per template; local lighthouse CLI or PageSpeed key), "
              "gsc=true (Search Console URL Inspection + clicks). Returns the prioritised issue table (priority 1 blocks indexing ... "
              "4 polish, hours, recommendation, example URL, pages affected), stores the run and diffs it against the last one. "
              "Never writes to the site.",
              {"type": "object", "properties": {
                  "url": {"type": "string", "description": "start URL, e.g. https://example.com/"},
                  "max_pages": {"type": "integer", "description": "1..2000 (default 200)"},
                  "rate_limit_s": {"type": "number", "description": "seconds between requests, >= 0.5 (default 1.0)"},
                  "user_agent": {"type": "string", "description": "default: the Googlebot user-agent string"},
                  "respect_robots": {"type": "boolean"}, "ad_urls": {"type": "array", "items": {"type": "string"}},
                  "ad_states": {"type": "boolean", "description": "also read ad final URLs from the configured state files (default true)"},
                  "render": {"type": "boolean"}, "lighthouse": {"type": "boolean"}, "gsc": {"type": "boolean"},
                  "gsc_site": {"type": "string"}, "max_inspect": {"type": "integer"},
                  "limit": {"type": "integer", "description": "issue rows to return (default 40; all rows are stored)"}},
               "required": ["url"], "additionalProperties": False})
    def run_tool(args):
        mp = int(args.get("max_pages") or DEFAULT_MAX_PAGES)          # types are enforced by the protocol layer
        if not 1 <= mp <= MAX_PAGES_TOOL:
            raise ToolError("max_pages must be 1..%d" % MAX_PAGES_TOOL)
        rate = float(args.get("rate_limit_s") or 1.0)
        if not math.isfinite(rate) or rate < MIN_RATE_TOOL:
            raise ToolError("rate_limit_s must be >= %.1f (be polite to the site)" % MIN_RATE_TOOL)
        mi = int(args.get("max_inspect") or 50)
        if not 0 <= mi <= 500:
            raise ToolError("max_inspect must be 0..500 (Search Console allows ~2,000 inspections per property per day)")
        r = audit(args["url"], cfg, max_pages=mp, rate_limit_s=rate,
                  user_agent=args.get("user_agent") or fetcher.GOOGLEBOT_UA,
                  respect_robots=args.get("respect_robots", True), ad_urls=args.get("ad_urls") or (),
                  ad_states=args.get("ad_states", True), render=bool(args.get("render")),
                  lighthouse=bool(args.get("lighthouse")), gsc=bool(args.get("gsc")), gsc_site=args.get("gsc_site"),
                  gsc_client=gsc_client, max_inspect=mi, fetch=fetch, sleep=sleep, now=now, store=store(),
                  today=dt.date.fromtimestamp(now()))
        return {"run_id": r["run_id"], "host": r["host"], "summary": r["summary"],
                "issues": [_brief_row(x) for x in _filter_rows(r["rows"], limit=args.get("limit") or 40)],
                "diff": r["diff"]["summary"], "regressions": r["diff"]["regressions"][:20], "files": r["files"],
                "lighthouse": r["lighthouse"], "render": r["render"], "gsc": r["gsc"], "notes": r["notes"],
                "settings": r["settings"]}

    @srv.tool("seo_audit_issues", "Read a stored site audit's prioritised issue table (from seo_audit_run): latest run of host "
              "unless run_id is given. format=json (default) | markdown | csv; filter by max priority (1-4) or code.",
              {"type": "object", "properties": {"host": {"type": "string"}, "url": {"type": "string"},
                                                "run_id": {"type": "string"}, "priority": {"type": "integer"},
                                                "code": {"type": "string"}, "limit": {"type": "integer"},
                                                "format": {"type": "string", "enum": ["json", "markdown", "csv"]}},
               "required": [], "additionalProperties": False})
    def issues_tool(args):
        host, st = _resolve_host(args), store()
        r = st.load(host, args["run_id"]) if args.get("run_id") else st.latest(host)
        if not r:
            raise ToolError("no stored audit for %s: run seo_audit_run first" % host)
        rows = _filter_rows(r.get("rows") or [], args.get("priority"), args.get("code"), args.get("limit") or 50)
        out = {"run_id": r.get("run_id"), "host": host, "at": r.get("at"), "summary": r.get("summary"),
               "runs": st.list(host)[:10]}
        fmt = args.get("format") or "json"
        if fmt == "markdown":
            out["markdown"] = issues.to_markdown(rows, title="Site audit: %s (%s)" % (host, r.get("run_id")), summary=r.get("summary"))
        elif fmt == "csv":
            out["csv"] = issues.to_csv(rows)
        else:
            out["issues"] = [dict(_brief_row(x), urls=x.get("urls", [])[:20], templates=x.get("templates")) for x in rows]
            out["schema_matrix"] = r.get("schema_matrix")
        return out

    @srv.tool("seo_audit_diff", "Compare two stored site audits of one host (default: the latest run vs the one before): new and "
              "fixed issues, and regressions (new noindex, pages that stopped being indexable, broken canonicals, lost internal "
              "links, LCP jumps, pages now erroring or gone).",
              {"type": "object", "properties": {"host": {"type": "string"}, "url": {"type": "string"},
                                                "run_id": {"type": "string"}, "against": {"type": "string"}},
               "required": [], "additionalProperties": False})
    def diff_tool(args):
        host, st = _resolve_host(args), store()
        cur = st.load(host, args["run_id"]) if args.get("run_id") else st.latest(host)
        if not cur:
            raise ToolError("no stored audit for %s: run seo_audit_run first" % host)
        prev = st.load(host, args["against"]) if args.get("against") else st.latest(host, before=cur["run_id"])
        return runs.diff(prev, cur)

    @srv.tool("seo_gsc_exclusions", "Search Console view of a stored site audit (read-only): URL Inspection for the audited pages "
              "(sitemap pages first, max_inspect default 50; Google allows ~2,000 per property per day) plus clicks by page, then "
              "'excluded pages and why' (Google's coverage state and selected canonical) next to what the crawl says about each "
              "page. Uses the deployment's Google credential with the Search Console read-only scope; says 'not connected' "
              "cleanly when it has none.",
              {"type": "object", "properties": {"host": {"type": "string"}, "url": {"type": "string"},
                                                "site": {"type": "string"}, "max_inspect": {"type": "integer"},
                                                "run_id": {"type": "string"}},
               "required": [], "additionalProperties": False})
    def gsc_tool(args):
        from .seo import gsc_join
        host, st = _resolve_host(args), store()
        r = st.load(host, args["run_id"]) if args.get("run_id") else st.latest(host)
        if not r:
            raise ToolError("no stored audit for %s: run seo_audit_run first" % host)
        mi = int(args.get("max_inspect") or 50)
        if not 0 <= mi <= 500:
            raise ToolError("max_inspect must be 0..500")
        client = gsc_client
        if client is None:
            try:
                client = gsc_client_for(cfg)
            except ToolError as e:
                return {"connected": False, "note": "Search Console not connected: " + scrub(str(e), 200)}
        out = gsc_join.join(_crawl_from_run(r), client, site=args.get("site"), today=dt.date.fromtimestamp(now()),
                            max_inspect=mi)
        out.pop("pages", None)
        out["run_id"] = r.get("run_id")
        return out


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def main(argv=None, cfg=None):
    """adsops-mcp site-audit --url URL [...]: run the audit and print the table (md default, json or csv)."""
    import argparse
    ap = argparse.ArgumentParser(prog="adsops-mcp site-audit")
    ap.add_argument("--url", required=True)
    ap.add_argument("--config")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    ap.add_argument("--rate", type=float, default=1.0, help="seconds between requests (>= 0.5)")
    ap.add_argument("--user-agent", default=fetcher.GOOGLEBOT_UA)
    ap.add_argument("--ignore-robots", action="store_true")
    ap.add_argument("--ad-url", action="append", default=[])
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--lighthouse", action="store_true")
    ap.add_argument("--gsc", action="store_true")
    ap.add_argument("--gsc-site")
    ap.add_argument("--max-inspect", type=int, default=50)
    ap.add_argument("--store", help="directory for stored runs (default: beside the config's audit log, or ~/.adsops/seo-audits)")
    ap.add_argument("--format", choices=["md", "json", "csv"], default="md")
    a = ap.parse_args(argv)
    if a.rate < MIN_RATE_TOOL or not 1 <= a.max_pages <= MAX_PAGES_TOOL:
        ap.error("--rate must be >= 0.5 and --max-pages 1..2000")
    if cfg is None and a.config:
        from . import config as config_mod
        cfg = config_mod.load_config(a.config)
    st = runs.RunStore(os.path.expanduser(a.store) if a.store else store_dir(cfg or {"audit_log": "~/.adsops/audit.jsonl"}))
    try:
        r = audit(a.url, cfg, max_pages=a.max_pages, rate_limit_s=a.rate, user_agent=a.user_agent,
                  respect_robots=not a.ignore_robots, ad_urls=a.ad_url, render=a.render, lighthouse=a.lighthouse,
                  gsc=a.gsc, gsc_site=a.gsc_site, max_inspect=a.max_inspect, store=st)
    except ToolError as e:
        print("error: " + scrub(str(e), 500), file=sys.stderr)
        return 2
    if a.format == "json":
        print(json.dumps(r, indent=1, default=str))
    elif a.format == "csv":
        print(issues.to_csv(r["rows"]), end="")
    else:
        print(issues.to_markdown(r["rows"], title="Site audit: %s (%s)" % (r["host"], r["run_id"]), summary=r["summary"]))
        print("\nStored: %s\nDiff vs previous run: %s\nNotes: %s" % (r["files"]["json"], json.dumps(r["diff"]["summary"]),
                                                                    "; ".join(r["notes"])))
    return 0


# ── selftest: loopback fixture site, real HTTP, no outside network ────────────────────────────────────────────────
def selftest():
    import tempfile
    from .seo import fixture_site
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "seo_tools.selftest: " + msg
        n += 1

    res = fixture_site.resolver()

    def fx_fetch(u):
        if u.startswith("https://"):                 # the fixture is http-only; never knock on local port 443
            return {"url": u, "status": 0, "error": "ConnectionRefusedError"}
        return fetcher.fetch(u, resolver=res, allow_private=True, timeout=5)

    tmp = tempfile.mkdtemp(prefix="adrails-seo-")
    cfg = {"audit_log": os.path.join(tmp, "audit.jsonl")}
    clock = {"t": 1_794_300_000.0}

    sleeps = []

    def fnow():
        clock["t"] += 0.01
        return clock["t"]

    def fsleep(x):
        sleeps.append(x)
        clock["t"] += x

    with fixture_site.serve() as site:
        base = site["base"]
        st = runs.RunStore(store_dir(cfg), now=fnow)
        r = audit(base + "/", cfg, fetch=fx_fetch, sleep=fsleep, now=fnow, store=st, rate_limit_s=0.2,
                  ad_urls=[base + "/owners/", "https://elsewhere.example/lp"])
        codes = {row["code"]: row for row in r["rows"]}
        ck(not any("/private/" in h for h in site["hits"]), "robots.txt Disallow honoured: /private/ never requested")
        ck(r["summary"]["robots_disallowed_skipped"] == 1, "disallowed URL reported as skipped")
        for code in ("starved_pages", "hardcoded_sibling_skew", "near_duplicate_self_canonical", "internal_utm_links",
                     "doctype_missing", "lang_missing", "schema_gaps", "redirect_chain", "internal_links_to_redirects",
                     "internal_links_to_errors", "sitemap_non_indexable", "sitemap_orphans", "ad_lp_noindex",
                     "ad_lp_equity_sink", "noindex_with_inlinks", "security_headers_missing", "empty_anchors"):
            ck(code in codes, "fixture defect found: " + code)
        ck(base + "/services/" in codes["starved_pages"]["urls"]
           and [base + "/services/", 1, True] in codes["starved_pages"]["data"]["pages"], "hub /services/ starved, flagged as hub")
        ck(codes["hardcoded_sibling_skew"]["data"]["targets"][0][0] == base + "/services/a/", "sibling skew names /services/a/")
        ck(sorted(codes["near_duplicate_self_canonical"]["urls"]) == [base + "/guides/one/", base + "/guides/two/"],
           "near-duplicate guide pair, both self-canonical")
        utm = codes["internal_utm_links"]["data"]
        ck(utm["links"] == 6 and utm["targets"][0][0] == base + "/owners/" and utm["targets"][0][2] is True,
           "6 utm-tagged internal links to /owners/, which is noindex")
        ck(codes["doctype_missing"]["urls"] == [base + "/about/"], "doctype missing only on /about/")
        ck(any(p[0] == "/services/*/" and p[1] == "Service" and p[2:] == [4, 5] for p in codes["schema_gaps"]["data"]["partial"])
           and "/guides/*/" in codes["schema_gaps"]["data"]["article_templates"], "schema gaps: 4/5 Service, guides no Article")
        ck(codes["internal_links_to_errors"]["data"]["examples"][0][1:] == [base + "/missing/", 404], "link to the 404 found")
        ck(codes["ad_lp_equity_sink"]["data"]["targets"][0][1] == 6, "ad page equity sink counts the 6 utm links")
        ck(codes["redirect_chain"]["data"]["chains"][0][1] == [301, 301, 200], "two-hop redirect chain recorded")
        ck(r["summary"]["ad_landing_pages"]["ad_urls"] == 1, "off-site ad URL ignored, on-site one audited")
        ck(r["pages"][base + "/about/"]["depth"] == 1 and r["pages"][base + "/orphan/"]["depth"] is None, "click depth")
        ck(r["rows"][0]["priority"] == 1 and all(a["priority"] <= b["priority"] for a, b in zip(r["rows"], r["rows"][1:])),
           "issue table sorted by priority")
        ck(all(k in r["rows"][0] for k in ("priority", "hours", "issue", "recommendation", "example_url", "pages_affected",
                                            "status", "template")), "every row has the table columns")
        ck(os.path.isfile(r["files"]["md"]) and "| # | Priority |" in open(r["files"]["md"]).read()
           and open(r["files"]["csv"]).read().startswith("priority,hours,issue"), "markdown + csv written")
        ck(oct(os.stat(r["files"]["json"]).st_mode & 0o777) == "0o600", "stored run is private (0600)")
        ck(r["diff"]["previous"] is None and r["diff"]["summary"]["regressions"] == 0, "first run: nothing to diff")
        ck(sleeps and min(sleeps) > 0 and "JS render parity not run" in " ".join(r["notes"]), "rate limit slept; notes say what did not run")
        ck("hsts_missing" not in codes, "HSTS not demanded of an http-only site")
        ck("server_errors" not in codes and all(base + "/tenants/" != ex[1] for ex in
                                                 codes["internal_links_to_errors"]["data"]["examples"])
           and all(base + "/tenants/" != ex[1] for ex in codes["internal_links_to_redirects"]["data"]["examples"]),
           "a redirect to another site (tenant portal) is neither an error nor a crawl-waste redirect")
        ck(r["pages"][base + "/tenants/"]["status"] == 302 and not any("utm_" in u for u in r["pages"]),
           "off-site redirect keeps its 302; utm-tagged URLs are not crawled as separate pages")
        ck(not any(p[1] in ("ImageObject", "Organization") for p in codes["schema_gaps"]["data"]["partial"]),
           "schema gaps compare page-level types only")
        # change the site: /services/b/ becomes noindex, the guide duplicate is fixed
        p = site["pages"]
        st_b, h_b, body_b = p["/services/b/"]
        p["/services/b/"] = (st_b, h_b, body_b.replace("<head>", "<head><meta name=\"robots\" content=\"noindex\">"))
        p["/guides/two/"] = (200, {"Content-Type": "text/html"}, fixture_site._page("Guide two", "<p>Different.</p>"))
        r2 = audit(base + "/", cfg, fetch=fx_fetch, sleep=fsleep, now=fnow, store=st, rate_limit_s=0.2,
                   ad_urls=[base + "/owners/"])
        kinds = {(g["kind"], g["url"]) for g in r2["diff"]["regressions"]}
        ck(("new_noindex", base + "/services/b/") in kinds, "diff: new noindex flagged as a regression")
        ck(any(f["code"] == "near_duplicate_self_canonical" for f in r2["diff"]["fixed"]), "diff: duplicate pair reported fixed")
        ck(r2["diff"]["previous"] == r["run_id"], "diff is against the previous run")

        # MCP tools end to end (in-process server, injected fixture fetch)
        from . import protocol
        srv = protocol.Server("t", "0")

        class Ctx:
            now = staticmethod(fnow)

        class FakeGsc:
            def sites(self):
                return [{"site": "sc-domain:fixture.test"}]

            def query(self, site, s, e, dims, **k):
                return [{"keys": [base + "/"], "clicks": 5, "impressions": 50, "ctr": 0.1, "position": 4}]

            def inspect(self, site, url):
                ok_ = url == base + "/"
                return {"url": url, "verdict": "PASS" if ok_ else "NEUTRAL",
                        "coverage_state": "Submitted and indexed" if ok_ else "Crawled - currently not indexed",
                        "google_canonical": url, "user_canonical": url}

        register(srv, Ctx(), cfg, gsc_client=FakeGsc(), fetch=fx_fetch, sleep=lambda s: None)

        def call(name, a):
            out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": a}})["result"]
            return out

        bad = call("seo_audit_run", {"url": base + "/", "rate_limit_s": 0.1})
        ck(bad["isError"] and "polite" in bad["content"][0]["text"], "tool refuses a rate faster than 0.5 s")
        ck(call("seo_audit_run", {"url": base + "/", "max_pages": 5000})["isError"], "tool refuses max_pages > 2000")
        ck(call("seo_audit_run", {"url": base + "/", "user_agent": "x\r\nX-Injected: 1"})["isError"]
           and call("seo_audit_run", {"url": "http://[::1"})["isError"]
           and call("seo_audit_run", {"url": base + "/", "ad_urls": [base + "/"] * 501})["isError"],
           "header-injecting user agents, malformed URLs and oversized ad lists are refused cleanly")
        t = call("seo_audit_run", {"url": base + "/", "rate_limit_s": 0.5, "limit": 5, "max_pages": 50})
        ck(not t["isError"] and len(t["structuredContent"]["issues"]) == 5, "seo_audit_run via MCP returns the top rows")
        iss = call("seo_audit_issues", {"url": base + "/", "priority": 2, "format": "json"})["structuredContent"]
        ck(iss["issues"] and all(x["priority"] <= 2 for x in iss["issues"]), "seo_audit_issues filters by priority")
        md = call("seo_audit_issues", {"host": "fixture.test", "format": "markdown"})["structuredContent"]["markdown"]
        ck(md.startswith("# Site audit: fixture.test"), "markdown via MCP")
        d = call("seo_audit_diff", {"host": "fixture.test"})["structuredContent"]
        ck(d["previous"] and d["current"] and "regressions" in d, "seo_audit_diff via MCP")
        g = call("seo_gsc_exclusions", {"host": "fixture.test", "max_inspect": 3})["structuredContent"]
        ck(g["connected"] and len(g["inspected"]) == 3 and g["excluded"] and g["excluded"][0]["crawl_says"],
           "seo_gsc_exclusions merges Google's coverage state with the crawl's explanation")
        ck(call("seo_audit_issues", {"host": "../etc"})["isError"] and call("seo_audit_issues",
                                                                              {"host": "fixture.test", "run_id": "../../x"})["isError"],
           "path traversal refused")
    # the real (non-fixture) path refuses private targets before any request
    ck(_raises(lambda: audit("http://127.0.0.1/", cfg)) and _raises(lambda: audit("http://localhost:8080/", cfg)),
       "real audit refuses loopback targets")
    return n


def _raises(fn):
    try:
        fn()
    except ToolError:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
