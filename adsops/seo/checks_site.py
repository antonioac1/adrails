"""Site-level checks for the AdRails site audit (adsops/seo): robots.txt, XML sitemaps, redirects, host
consolidation, soft 404s, server errors, HSTS and the other response-header checks.

Read-only and pure: no network, disk or clock access, no global mutable state. Everything is passed in (the crawl
result dict described in SPEC-B) and everything returned is JSON-serialisable. Nothing here raises on hostile data:
missing robots/sitemap/probes blocks simply mean those checks do not run, and a crawl with no pages yields no
findings. `run()` returns (findings, passed), where `passed` names the checks that ran and found nothing wrong.

Findings follow the shared contract: {"code", "urls", "detail", "data"} with a non-empty url list, one plain-English
sentence and a small JSON-safe data dict (lists capped at 20 items). Site-level findings use urls=[start_url] and
data.site_level=True.
"""
import re
from urllib.parse import urlsplit

from adsops.aeo.crawl_audit import parse_robots, can_fetch
from adsops.errors import ToolError
from adsops.seo.checks_content import is_indexable, why_not_indexable

MAX_STR = 500
MAX_LIST = 20
MAX_URLS = 500
MAX_HOPS = 50
LOOP_LOCS = ("redirect loop", "too many redirects")
SECURITY_HEADERS = ("x-content-type-options", "referrer-policy", "content-security-policy")


# ---------------------------------------------------------------------------------------------------------------
# small, total helpers (never raise, never touch the outside world)
# ---------------------------------------------------------------------------------------------------------------
def _text(value, limit=MAX_STR):
    """Whitespace-collapsed string of `value` ('' for None/garbage), truncated to `limit` characters."""
    if value is None:
        return ""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:                                            # noqa: BLE001 - hostile __str__
        return ""
    s = " ".join(s.split())
    return s[:limit] if limit else s


def _list(value):
    """`value` if it is a list/tuple, else an empty list."""
    return value if isinstance(value, (list, tuple)) else []


def _int(value, default=0):
    """Best-effort int of `value`, `default` when that is impossible (never raises)."""
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip():
            return int(float(value.strip()))
    except Exception:                                            # noqa: BLE001 - NaN/inf/garbage
        pass
    return default


def _dict(value):
    """`value` when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _pages(crawl):
    """-> the pages mapping of the crawl (empty when missing or malformed)."""
    return _dict(_dict(crawl).get("pages"))


def _uniq(items, cap=MAX_URLS):
    """Deduplicated, non-empty strings of `items`, in order, capped at `cap` entries."""
    out, seen = [], set()
    for item in _list(items):
        s = _text(item, 2000)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= cap:
                break
    return out


def _ex(urls, n=3):
    """'a, b, c' of the first `n` entries of `urls`, for detail sentences."""
    items = [_text(u, 120) for u in _list(urls)]
    items = [i for i in items if i][:n]
    return ", ".join(items) if items else "(none)"


def _nofrag(url):
    """`url` without its fragment (used for every membership test)."""
    s = _text(url, 2000)
    return s.split("#", 1)[0]


def _split(url):
    """urlsplit that never raises ('' parts for hostile input)."""
    try:
        return urlsplit(_text(url, 2000))
    except Exception:                                            # noqa: BLE001 - bad IPv6 hosts and friends
        return urlsplit("")


def _host(url):
    """Lowercase hostname of `url` ('' when unparsable)."""
    try:
        return (_split(url).hostname or "").lower()
    except Exception:                                            # noqa: BLE001
        return ""


def _scheme(url):
    """Lowercase scheme of `url` ('' when unparsable)."""
    try:
        return (_split(url).scheme or "").lower()
    except Exception:                                            # noqa: BLE001
        return ""


def _path_query(url):
    """Path plus '?query' of `url` ('/' when empty) - the shape robots.txt rules match against."""
    parts = _split(url)
    path = parts.path or "/"
    return path + "?" + parts.query if parts.query else path


def _hops(value):
    """Normalise a redirect chain ([ [url, status, location], ... ]) into lists, dropping garbage."""
    out = []
    for raw in _list(value):
        if len(out) >= MAX_HOPS:
            break
        if isinstance(raw, (list, tuple)):
            url = _text(raw[0] if len(raw) > 0 else "", 500)
            status = _int(raw[1] if len(raw) > 1 else 0)
            loc = _text(raw[2] if len(raw) > 2 else "", 500)
        elif isinstance(raw, dict):
            url, status, loc = _text(raw.get("url"), 500), _int(raw.get("status")), _text(raw.get("location"), 500)
        else:
            url, status, loc = _text(raw, 500), 0, ""
        out.append([url, status, loc])
    return out


def _headers(page):
    """Lowercased header dict of a page (empty when missing or malformed)."""
    out = {}
    for key, value in _dict(_dict(page).get("headers")).items():
        if isinstance(key, str):
            out.setdefault(key.strip().lower(), _text(value, 500))
    return out


def _chain_broken(hops):
    """True when a chain means 'too many redirects' / a loop, or carries more than one redirect."""
    if len(hops) > 2 or len({h[0] for h in hops}) < len(hops):
        return True
    return bool(hops) and _text(hops[-1][2], 100).lower() in LOOP_LOCS


def _by_url(pages):
    """Index pages by their own URL field (falls back to the mapping key)."""
    out = {}
    for key, page in pages.items():
        if not isinstance(page, dict):
            continue
        url = _text(page.get("url"), 500) or _text(key, 500)
        if url and url not in out:
            out[url] = page
    return out


def _lookup(pages, index, url):
    """The crawled page for `url` (None when it was not crawled)."""
    page = pages.get(url)
    if isinstance(page, dict):
        return page
    return index.get(url) or index.get(_nofrag(url))


def _plain_file(page):
    """A 200 non-HTML file (llms.txt, PDF) that answers directly: fine in a sitemap, nothing to index-check."""
    ctype = _text(page.get("ctype"), 100).split(";")[0].strip().lower()
    return (_int(page.get("status"), 0) == 200 and page.get("final") in (None, page.get("url"))
            and ctype not in ("text/html", "application/xhtml+xml", "")
            and "noindex" not in _headers(page).get("x-robots-tag", "").lower())


def _statuses(hops):
    """The status code of every hop of a chain."""
    return [_int(h[1]) for h in hops]


# ---------------------------------------------------------------------------------------------------------------
# the run() entry point
# ---------------------------------------------------------------------------------------------------------------
def run(crawl):
    """-> (findings, passed) for every site-level check in SPEC-B, over one crawl result."""
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("checks_site.run: crawl must be a dict")
    crawl = _dict(crawl)
    settings = _dict(crawl.get("settings"))
    start = _text(settings.get("start_url"), 500)
    pages = _pages(crawl)
    index = _by_url(pages)
    robots = crawl.get("robots") if isinstance(crawl.get("robots"), dict) else None
    sitemap = crawl.get("sitemap") if isinstance(crawl.get("sitemap"), dict) else None
    probes = _dict(crawl.get("probes"))
    sm_urls = _uniq([_text(u, 500) for u in _list(_dict(sitemap).get("urls"))])
    sm_set = set(_nofrag(u) for u in sm_urls)
    r_status = _int(_dict(robots).get("status"), 0)
    r_url = _text(_dict(robots).get("url"), 500) or (start.rstrip("/") + "/robots.txt" if start else "robots.txt")
    findings, passed = [], []

    def emit(code, urls, detail, data=None):
        """Append one finding; returns True when something was reported (so the check did not pass)."""
        clean = _uniq(urls)
        if not clean:
            return False
        text = _text(detail, 600)
        if not text:
            text = "%d URLs affected." % len(clean)
        elif not text.endswith("."):
            text += "."                                       # findings always read as one full sentence
        findings.append({"code": code, "urls": clean, "detail": text,
                         "data": data if isinstance(data, dict) else {}})
        return True

    def settle(code, fired, ran=True):
        """Record `code` in `passed` when it ran and stayed silent."""
        if ran and not fired:
            passed.append(code)

    # --- robots.txt ------------------------------------------------------------------------------------------
    if robots is not None and start:
        fired = False
        if r_status == 0 or r_status >= 500:
            fired = emit("robots_unreachable", [start],
                         ("%s could not be fetched at all (no HTTP answer)" % r_url if r_status == 0 else
                          "%s answered HTTP %d" % (r_url, r_status))
                         + "; while robots.txt fails with a server error or times out, Google stops crawling the site "
                           "and treats a long-lasting failure as a full block",
                         {"status": r_status, "site_level": True, "robots_url": r_url})
        settle("robots_unreachable", fired)

    parsed = {}
    if robots is not None and r_status == 200:
        try:
            parsed = parse_robots(robots.get("text") if isinstance(robots.get("text"), str) else "")
        except Exception:                                    # noqa: BLE001 - never trust the parser with hostile text
            parsed = {}
        parsed = parsed if isinstance(parsed, dict) else {}
        root_blocked = not can_fetch(parsed, "Googlebot", "/")
        bing_blocked = not can_fetch(parsed, "Bingbot", "/")
        blocked = [u for u in sm_urls if not can_fetch(parsed, "Googlebot", _path_query(u))]
        urls = ([start] if root_blocked and start else []) + blocked
        if root_blocked and blocked:
            detail = ("robots.txt disallows Googlebot from / and from %d sitemap URL(s) (e.g. %s), which blocks "
                      "indexing of both" % (len(blocked), _ex(blocked)))
        elif root_blocked:
            detail = "robots.txt disallows Googlebot from /, which blocks crawling of every page on the site"
        else:
            detail = ("robots.txt disallows Googlebot from %d URL(s) that are listed in the sitemap (e.g. %s)"
                      % (len(blocked), _ex(blocked)))
        settle("robots_blocks_googlebot", emit(
            "robots_blocks_googlebot", urls, detail,
            {"site_level": bool(root_blocked and start), "root_blocked": bool(root_blocked),
             "bingbot_root_blocked": bool(bing_blocked), "blocked_in_sitemap": blocked[:MAX_LIST],
             "count": len(blocked)}), ran=bool(robots is not None and r_status == 200))

        fired = False
        if start and not _uniq(_list(robots.get("sitemaps"))):
            fired = emit("robots_no_sitemap_line", [start],
                         "%s answers 200 but declares no Sitemap: line; adding one lets every crawler (not only those "
                         "you submitted the sitemap to) discover it" % r_url, {"site_level": True, "robots_url": r_url})
        settle("robots_no_sitemap_line", fired, ran=bool(start))

    # --- sitemaps --------------------------------------------------------------------------------------------
    if sitemap is not None:
        errors = [_text(e, 300) for e in _list(sitemap.get("errors"))]
        errors = [e for e in errors if e]
        fired = False
        if start and not sm_urls:
            fired = emit("sitemap_missing", [start],
                         "No usable XML sitemap was found for %s (%d error(s) while trying, e.g. %s)"
                         % (start, len(errors), _ex(errors)),
                         {"site_level": True, "errors": errors[:MAX_LIST], "error_count": len(errors),
                          "sources": [_text(s, 300) for s in _list(sitemap.get("sources"))[:MAX_LIST]]})
        settle("sitemap_missing", fired, ran=bool(start))

        bad, reasons = [], []
        for url in sm_urls:
            page = _lookup(pages, index, url)
            if page is None or is_indexable(page) or _plain_file(page):
                continue
            bad.append(url)
            reasons.append([url, _text(why_not_indexable(page), 200)])
        settle("sitemap_non_indexable", emit(
            "sitemap_non_indexable", bad,
            "%d of %d URLs in the sitemap cannot be indexed (e.g. %s -> %s)"
            % (len(bad), len(sm_urls), _ex(bad, 1), reasons[0][1] if reasons else "not indexable"),
            {"count": len(bad), "sitemap_size": len(sm_urls), "reasons": reasons[:MAX_LIST]}),
            ran=bool(pages))

        missing = []
        for key, page in (pages.items() if sm_urls else ()):
            if not isinstance(page, dict):
                continue
            url = _text(page.get("url"), 500) or _text(key, 500)
            if url and is_indexable(page) and _nofrag(url) not in sm_set:
                missing.append(url)
        missing = _uniq(missing)
        settle("sitemap_missing_indexable", emit(
            "sitemap_missing_indexable", missing,
            "%d indexable pages are missing from the sitemap (e.g. %s)" % (len(missing), _ex(missing)),
            {"count": len(missing), "examples": missing[:MAX_LIST], "sitemap_size": len(sm_urls)}),
            ran=bool(sm_urls))

        orphans = []
        for url in (sm_urls if not _dict(crawl.get("skipped")).get("cap_reached") else ()):   # a capped crawl
            # cannot tell an orphan from a page it simply did not reach
            page = _lookup(pages, index, url)
            if page is None or _int(page.get("status"), 0) != 200 or _plain_file(page):
                continue
            if page.get("depth") is None:
                orphans.append(url)
        settle("sitemap_orphans", emit(
            "sitemap_orphans", orphans,
            "%d URLs in the sitemap are not reachable through internal links from %s (e.g. %s)"
            % (len(orphans), start or "the start page", _ex(orphans)),
            {"count": len(orphans), "examples": orphans[:MAX_LIST]}),
            ran=bool(pages and sm_urls) and not _dict(crawl.get("skipped")).get("cap_reached"))

    # --- redirects and host consolidation --------------------------------------------------------------------
    start_scheme = _scheme(start)
    http_hops = _hops(probes.get("http_home"))
    if http_hops and start and start_scheme == "https":
        last = http_hops[-1]
        first_status = _int(http_hops[0][1], 0)
        plain = _text(last[0], 500)
        fired = False
        if _int(last[1], 0) == 200 and plain.startswith("http://"):
            fired = emit("http_not_redirected", [start],
                         "%s answers HTTP 200 without redirecting to https, so the insecure scheme stays crawlable"
                         % plain,
                         {"site_level": True, "http_url": plain, "status": 200,
                          "temporary": first_status in (302, 303, 307), "chain": http_hops[:MAX_LIST]})
        settle("http_not_redirected", fired, ran=_int(last[1], 0) > 0)     # a failed probe proves nothing

    chains = []
    broken_pages = []
    for key, page in pages.items():
        if not isinstance(page, dict):
            continue
        hops = _hops(page.get("chain"))
        if not hops or not _chain_broken(hops):
            continue
        url = _text(page.get("url"), 500) or _text(key, 500) or _text(hops[0][0], 500)
        if not url:
            continue
        broken_pages.append(url)
        chains.append([url, _statuses(hops)])
    for name in ("http_home", "www_home"):
        hops = _hops(probes.get(name))
        if hops and _chain_broken(hops):
            url = _text(hops[0][0], 500)
            if url:
                broken_pages.append(url)
                chains.append([url, _statuses(hops)])
    broken_pages = _uniq(broken_pages)
    settle("redirect_chain", emit(
        "redirect_chain", broken_pages,
        "%d URLs need more than one redirect (or loop) before reaching their final page (e.g. %s)"
        % (len(broken_pages), _ex(broken_pages)),
        {"count": len(broken_pages), "chains": chains[:MAX_LIST]}),
        ran=bool(pages) or bool(probes))

    www_hops = _hops(probes.get("www_home"))
    if www_hops and start:
        last = www_hops[-1]
        www_url = _text(last[0], 500)
        fired = False
        if _int(last[1], 0) == 200 and www_url and _host(www_url) and _host(www_url) != _host(start):
            fired = emit("www_not_consolidated", [www_url],
                         "%s answers HTTP 200 while the crawl starts on %s, so both hostnames serve the site"
                         % (www_url, start),
                         {"site_level": True, "www_url": www_url, "start_host": _host(start),
                          "www_host": _host(www_url), "chain": www_hops[:MAX_LIST]})
        settle("www_not_consolidated", fired, ran=_int(last[1], 0) > 0)

    # --- soft 404 and server errors --------------------------------------------------------------------------
    not_found = _dict(probes.get("not_found"))
    if not_found and "status" in not_found:
        nf_url = _text(not_found.get("url"), 500) or start
        nf_status = _int(not_found.get("status"), 0)
        fired = False
        if nf_url and nf_status == 200:
            fired = emit("soft_404", [nf_url],
                         "%s returns HTTP 200 for a random non-existent URL, so Google reads error pages as real "
                         "pages" % nf_url, {"status": nf_status, "url": nf_url, "site_level": True})
        settle("soft_404", fired, ran=bool(nf_url) and nf_status > 0)

    errors = []
    if pages:
        for key, page in pages.items():
            if not isinstance(page, dict):
                continue
            url = _text(page.get("url"), 500) or _text(key, 500)
            if not url:
                continue
            status = _int(page.get("status"), 0)
            err = _text(page.get("error"), 300)
            if status >= 500:
                errors.append([url, status, err])
            elif status == 0 and not err.lower().startswith("refused") and err != "request budget exhausted":
                errors.append([url, status, err])
    settle("server_errors", emit(
        "server_errors", [row[0] for row in errors],
        "%d pages returned a server error or no response at all (e.g. %s -> HTTP %s%s)"
        % (len(errors), errors[0][0] if errors else "(none)", errors[0][1] if errors else 0,
           (" (" + errors[0][2] + ")") if errors and errors[0][2] else ""),
        {"count": len(errors), "examples": errors[:MAX_LIST]}),
        ran=bool(pages))

    # --- response headers ------------------------------------------------------------------------------------
    start_page = _lookup(pages, index, start) if start else None
    start_https = start_scheme == "https" or (isinstance(start_page, dict) and _scheme(_text(start_page.get("url")))
                                              == "https")
    if isinstance(start_page, dict) and start:
        hdrs = _headers(start_page)
        fired = False
        sts = hdrs.get("strict-transport-security", "")
        sts_age = re.search(r"max-age\s*=\s*\"?(\d+)", sts, re.I)
        if start_https and _int(start_page.get("status"), 0) == 200 and not (sts_age and int(sts_age.group(1)) > 0):
            fired = emit("hsts_missing", [start],
                         "%s is served over https without a Strict-Transport-Security header, so browsers never "
                         "learn to skip http" % start,
                         {"site_level": True, "header": "strict-transport-security"})
        settle("hsts_missing", fired, ran=bool(start_https) and _int(start_page.get("status"), 0) == 200)

        fired = False
        if _int(start_page.get("status"), 0) == 200:
            missing_hdrs = [h for h in SECURITY_HEADERS if not hdrs.get(h)
                            or (h == "x-content-type-options" and hdrs.get(h, "").strip().lower() != "nosniff")]
            if missing_hdrs:
                fired = emit("security_headers_missing", [start],
                             "%s is missing %d security response header(s): %s"
                             % (start, len(missing_hdrs), ", ".join(missing_hdrs)),
                             {"site_level": True, "missing": missing_hdrs, "count": len(missing_hdrs)})
        settle("security_headers_missing", fired, ran=_int(start_page.get("status"), 0) == 200)

    return findings, passed


# ---------------------------------------------------------------------------------------------------------------
# offline self-test
# ---------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    """Offline assertions over fake crawl dicts; returns the number of checks performed."""
    import copy

    n = 0

    def chk(cond, msg):
        nonlocal n
        n += 1
        assert cond, "seo.checks_site.selftest: " + msg

    ALL = ["robots_unreachable", "robots_blocks_googlebot", "robots_no_sitemap_line", "sitemap_missing",
           "sitemap_non_indexable", "sitemap_missing_indexable", "sitemap_orphans", "http_not_redirected",
           "redirect_chain", "www_not_consolidated", "soft_404", "server_errors", "hsts_missing",
           "security_headers_missing"]

    def page(url, status=200, depth=1, chain=None, drop=(), headers=None, error=None):
        """One crawl page that is indexable by default (200, final == url, HTML, no noindex, self-canonical)."""
        hdrs = {"strict-transport-security": "max-age=31536000; includeSubDomains",
                "x-content-type-options": "nosniff", "referrer-policy": "strict-origin-when-cross-origin",
                "content-security-policy": "default-src 'self'"}
        hdrs.update(_dict(headers))
        for name in drop:
            hdrs.pop(name, None)
        return {"url": url, "chain": chain if chain is not None else [[url, status, ""]], "status": status,
                "final": url, "ctype": "text/html; charset=utf-8", "bytes": 1000, "truncated": False, "ms": 3,
                "headers": hdrs, "error": error, "depth": depth, "in_sitemap": True, "discovered": "link",
                "doc": {"robots": [], "canonicals": []}}

    def base():
        """A fully clean crawl: all 15 codes run and stay silent."""
        start, a = "https://ex.test/", "https://ex.test/a/"
        return {
            "settings": {"start_url": start, "user_agent": "Googlebot/2.1", "max_pages": 50, "rate_limit_s": 1.0,
                         "scope_hosts": ["ex.test", "www.ex.test"], "respect_robots": True, "timeout_s": 15,
                         "max_bytes": 3000000, "sitemap_seeding": True,
                         "started_at": "2024-01-01T00:00:00Z", "finished_at": "2024-01-01T00:01:00Z"},
            "robots": {"url": "https://ex.test/robots.txt", "status": 200,
                       "text": "User-agent: *\nAllow: /\n\nSitemap: https://ex.test/sitemap.xml\n",
                       "sitemaps": ["https://ex.test/sitemap.xml"]},
            "sitemap": {"urls": [start, a], "lastmod": {}, "sources": ["https://ex.test/sitemap.xml"],
                        "errors": [], "truncated": False},
            "pages": {start: page(start, depth=0), a: page(a, depth=1)},
            "edges": [[start, a, "Child", "", False]],
            "external_links": {"https://other.test/": 2},
            "probes": {"http_home": [["http://ex.test/", 301, "https://ex.test/"],
                                     ["https://ex.test/", 200, ""]],
                       "www_home": [["https://www.ex.test/", 301, "https://ex.test/"],
                                    ["https://ex.test/", 200, ""]],
                       "not_found": {"url": "https://ex.test/adrails-404-check-abc123/", "status": 404}},
            "skipped": {"robots_disallowed": [], "off_scope": 0, "cap_reached": False, "non_html": 0},
            "notes": [],
        }

    def of(crawl, code=None):
        """Findings of a run, optionally only those with `code`."""
        found, _ = run(crawl)
        return found if code is None else [f for f in found if f["code"] == code]

    def names(crawl):
        return [f["code"] for f in of(crawl)]

    # --- the clean site -----------------------------------------------------------------------------------
    clean, clean_passed = run(base())
    chk(clean == [], "a clean crawl produces no findings (got %r)" % (names(base()),))
    chk(sorted(clean_passed) == sorted(ALL), "a clean crawl passes every check (got %r)" % (clean_passed,))
    chk(all(isinstance(x, str) for x in clean_passed), "passed names are strings")
    chk(len(set(clean_passed)) == len(clean_passed), "passed has no duplicate names")

    # --- contract of every finding -------------------------------------------------------------------------
    noisy = base()
    noisy["robots"]["status"] = 503
    noisy["sitemap"]["errors"] = ["sitemap https://ex.test/sitemap.xml returned HTTP 500"]
    noisy["sitemap"]["urls"] = []
    noisy["probes"]["not_found"]["status"] = 200
    rows = of(noisy)
    chk(bool(rows), "the noisy crawl reports something")
    chk(all(set(r) == {"code", "urls", "detail", "data"} for r in rows), "every finding has the four keys")
    chk(all(r["urls"] and isinstance(r["urls"], list) for r in rows), "urls are non-empty lists")
    chk(all(r["detail"].endswith(".") and "  " not in r["detail"] for r in rows),
        "details are single tidy sentences")
    chk(all(isinstance(r["data"], dict) for r in rows), "data is always a dict")
    chk(all(len(v) <= MAX_LIST for r in rows for v in r["data"].values() if isinstance(v, list)),
        "data lists are capped at 20")

    # --- robots ---------------------------------------------------------------------------------------------
    c = base()
    c["robots"]["status"] = 503
    got = of(c, "robots_unreachable")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/"], "robots_unreachable fires with the start URL")
    chk(got[0]["data"].get("status") == 503 and got[0]["data"].get("site_level") is True,
        "robots_unreachable data names the status and is site-level")
    chk("503" in got[0]["detail"], "robots_unreachable detail names the status")
    c["robots"]["status"] = 0
    chk("robots_unreachable" in names(c), "robots_unreachable fires for status 0")
    c["robots"]["status"] = 410
    chk("robots_unreachable" not in names(c), "a 410 robots.txt is fine (everything allowed)")
    c["robots"]["status"] = 404
    chk("robots_unreachable" not in names(c), "a 404 robots.txt is fine")

    c = base()
    c["robots"]["text"] = "User-agent: Googlebot\nDisallow: /\n"
    got = of(c, "robots_blocks_googlebot")
    chk(len(got) == 1, "Googlebot Disallow: / fires robots_blocks_googlebot")
    chk(got[0]["urls"][0] == "https://ex.test/" and got[0]["data"]["site_level"] is True,
        "a root block is site-level and lists the start URL first")
    chk(got[0]["data"]["root_blocked"] is True and got[0]["data"]["bingbot_root_blocked"] is False,
        "Googlebot blocked does not block Bingbot")
    c["robots"]["text"] = "User-agent: *\nDisallow: /\n"
    got = of(c, "robots_blocks_googlebot")
    chk(got and got[0]["data"]["bingbot_root_blocked"] is True, "a '*' block blocks Bingbot too")

    c = base()
    c["robots"]["text"] = "User-agent: *\nDisallow: /private/\nSitemap: https://ex.test/sitemap.xml\n"
    c["sitemap"]["urls"] = ["https://ex.test/", "https://ex.test/private/x", "https://ex.test/private/y?z=1"]
    got = of(c, "robots_blocks_googlebot")
    chk(len(got) == 1, "a blocked sitemap URL fires robots_blocks_googlebot")
    chk(got[0]["data"]["blocked_in_sitemap"] == ["https://ex.test/private/x", "https://ex.test/private/y?z=1"],
        "blocked_in_sitemap lists the disallowed sitemap URLs (path+query matching)")
    chk(got[0]["data"]["root_blocked"] is False, "an unrelated Disallow does not block /")

    c = base()
    c["robots"]["sitemaps"] = []
    got = of(c, "robots_no_sitemap_line")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/"], "missing Sitemap: line fires")
    c["robots"]["status"] = 404
    chk("robots_no_sitemap_line" not in names(c), "the Sitemap: line check only runs on a 200 robots.txt")

    # --- sitemaps -------------------------------------------------------------------------------------------
    c = base()
    c["sitemap"]["urls"] = []
    c["sitemap"]["errors"] = ["sitemap https://ex.test/sitemap.xml returned HTTP 404"]
    got = of(c, "sitemap_missing")
    chk(len(got) == 1 and got[0]["data"]["errors"] == c["sitemap"]["errors"], "sitemap_missing carries the errors")

    c = base()
    c["pages"]["https://ex.test/a/"] = page("https://ex.test/a/", status=404)
    got = of(c, "sitemap_non_indexable")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/a/"], "a 404 in the sitemap fires sitemap_non_indexable")
    chk(got[0]["data"]["reasons"][0] == ["https://ex.test/a/", "HTTP 404"], "the reason is why_not_indexable")
    c["pages"]["https://ex.test/a/"] = page("https://ex.test/a/", drop=())
    c["pages"]["https://ex.test/a/"]["doc"] = {"robots": ["noindex"], "canonicals": []}
    got = of(c, "sitemap_non_indexable")
    chk(got and "noindex" in got[0]["data"]["reasons"][0][1], "a noindex page in the sitemap is reported")

    c = base()
    c["sitemap"]["urls"] = ["https://ex.test/"]
    got = of(c, "sitemap_missing_indexable")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/a/"], "an indexable page outside the sitemap fires")
    c["sitemap"]["urls"] = []
    chk("sitemap_missing_indexable" not in names(c), "the missing-indexable check needs a non-empty sitemap")

    c = base()
    c["sitemap"]["urls"] = ["https://ex.test/", "https://ex.test/b/"]
    c["pages"]["https://ex.test/b/"] = page("https://ex.test/b/", depth=None)
    got = of(c, "sitemap_orphans")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/b/"], "a sitemap URL with depth None is an orphan")
    c["pages"]["https://ex.test/b/"]["status"] = 404
    chk("sitemap_orphans" not in names(c), "an orphan needs a 200 page")

    # --- redirects / hosts ----------------------------------------------------------------------------------
    c = base()
    c["probes"]["http_home"] = [["http://ex.test/", 200, ""]]
    got = of(c, "http_not_redirected")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/"], "http:// answering 200 fires")
    chk(got[0]["data"]["site_level"] is True and got[0]["data"]["temporary"] is False,
        "site_level True; a direct 200 is not a temporary redirect")
    chk("http://ex.test/" in got[0]["detail"], "the detail names the http URL")
    c["probes"]["http_home"] = [["http://ex.test/", 302, "http://ex.test/x/"],
                                ["http://ex.test/x/", 200, ""]]
    got = of(c, "http_not_redirected")
    chk(got and got[0]["data"]["temporary"] is True, "a temporary first hop sets data.temporary")
    c["settings"]["start_url"] = "http://ex.test/"
    c["probes"]["http_home"] = [["http://ex.test/", 200, ""]]
    chk("http_not_redirected" not in names(c), "the http probe is irrelevant when the crawl starts on http")

    c = base()
    c["pages"]["https://ex.test/a/"]["chain"] = [["https://ex.test/a/", 301, "https://ex.test/b/"],
                                                 ["https://ex.test/b/", 301, "https://ex.test/c/"],
                                                 ["https://ex.test/c/", 200, ""]]
    got = of(c, "redirect_chain")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/a/"], "a two-hop chain fires redirect_chain")
    chk(got[0]["data"]["chains"][0] == ["https://ex.test/a/", [301, 301, 200]], "data.chains lists the statuses")
    c["pages"]["https://ex.test/a/"]["chain"] = [["https://ex.test/a/", 301, "https://ex.test/a/"],
                                                 ["https://ex.test/a/", 0, "redirect loop"]]
    got = of(c, "redirect_chain")
    chk(bool(got) and "https://ex.test/a/" in got[0]["urls"], "a redirect loop fires redirect_chain")
    c["pages"]["https://ex.test/a/"]["chain"] = [["https://ex.test/a/", 301, "https://ex.test/x/"],
                                                 ["https://ex.test/x/", 0, "too many redirects"]]
    chk("redirect_chain" in names(c), "'too many redirects' fires redirect_chain")
    c["pages"]["https://ex.test/a/"]["chain"] = [["https://ex.test/a/", 200, ""]]
    c["probes"]["http_home"] = [["http://ex.test/", 301, "https://ex.test/"],
                                ["https://ex.test/", 301, "https://ex.test/home/"],
                                ["https://ex.test/home/", 200, ""]]
    got = of(c, "redirect_chain")
    chk(got and got[0]["urls"] == ["http://ex.test/"], "a long probe chain is reported on the probe's first URL")

    c = base()
    c["probes"]["www_home"] = [["https://www.ex.test/", 200, ""]]
    got = of(c, "www_not_consolidated")
    chk(len(got) == 1 and got[0]["urls"] == ["https://www.ex.test/"], "both hosts answering 200 fires")
    chk(got[0]["data"]["site_level"] is True, "www_not_consolidated is site-level")
    c["probes"]["www_home"] = [["http://www.ex.test/", 301, "https://ex.test/"], ["https://ex.test/", 200, ""]]
    chk("www_not_consolidated" not in names(c), "a redirected www host is consolidated")

    # --- soft 404 / server errors ---------------------------------------------------------------------------
    c = base()
    c["probes"]["not_found"]["status"] = 200
    got = of(c, "soft_404")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/adrails-404-check-abc123/"], "a 200 on a random URL is a soft 404")
    c["probes"]["not_found"]["status"] = 410
    chk("soft_404" not in names(c), "a 410 on a random URL is not a soft 404")

    c = base()
    c["pages"]["https://ex.test/a/"]["status"] = 500
    c["pages"]["https://ex.test/a/"]["error"] = "HTTP 500"
    got = of(c, "server_errors")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/a/"], "a 500 fires server_errors")
    chk(got[0]["data"]["examples"][0] == ["https://ex.test/a/", 500, "HTTP 500"], "the example carries status+error")
    c["pages"]["https://ex.test/a/"] = page("https://ex.test/a/", status=0, error="refused: connection refused")
    chk("server_errors" not in names(c), "a refused connection is not a server error")
    c["pages"]["https://ex.test/a/"] = page("https://ex.test/a/", status=0, error="request budget exhausted")
    chk("server_errors" not in names(c), "an exhausted budget is not a server error")
    c["pages"]["https://ex.test/a/"] = page("https://ex.test/a/", status=0, error="timeout")
    chk("server_errors" in names(c), "a timeout (status 0) is a server error")
    c["pages"]["https://ex.test/a/"] = page("https://ex.test/a/", status=503)
    chk("server_errors" in names(c), "a 503 fires server_errors")

    # --- headers --------------------------------------------------------------------------------------------
    c = base()
    c["pages"]["https://ex.test/"] = page("https://ex.test/", depth=0, drop=("strict-transport-security",))
    got = of(c, "hsts_missing")
    chk(len(got) == 1 and got[0]["urls"] == ["https://ex.test/"], "a missing HSTS header fires on https")
    chk(got[0]["data"]["site_level"] is True, "hsts_missing is site-level")
    c["pages"]["https://ex.test/"]["headers"]["strict-transport-security"] = "max-age=60"
    chk("hsts_missing" not in names(c), "any HSTS value is enough")
    c = base()
    c["pages"]["https://ex.test/"]["url"] = "http://ex.test/"
    c["pages"]["http://ex.test/"] = c["pages"].pop("https://ex.test/")
    c["settings"]["start_url"] = "http://ex.test/"
    found, passed = run(c)
    chk("hsts_missing" not in [f["code"] for f in found] and "hsts_missing" not in passed,
        "hsts_missing does not even run for an http start page")

    c = base()
    c["pages"]["https://ex.test/"] = page("https://ex.test/", depth=0, drop=("content-security-policy",))
    got = of(c, "security_headers_missing")
    chk(len(got) == 1 and got[0]["data"]["missing"] == ["content-security-policy"], "a missing CSP is reported")
    c["pages"]["https://ex.test/"] = page("https://ex.test/", depth=0,
                                          drop=("x-content-type-options", "referrer-policy"))
    got = of(c, "security_headers_missing")
    chk(got and got[0]["data"]["missing"] == ["x-content-type-options", "referrer-policy"],
        "several missing headers are all listed")
    c["pages"]["https://ex.test/"]["status"] = 500
    chk("security_headers_missing" not in names(c), "the header check needs a 200 start page")

    # --- hostile input --------------------------------------------------------------------------------------
    found, passed = run({})
    chk(found == [] and passed == [], "an empty crawl gives no findings and no passes")
    chk(run(None) == ([], []), "None is treated as an empty crawl")
    try:
        run(["not", "a", "dict"])
        chk(False, "a non-dict crawl raises ToolError")
    except ToolError:
        chk(True, "a non-dict crawl raises ToolError")
    hostile = {"settings": {"start_url": None}, "robots": {"status": "abc", "text": None, "sitemaps": None},
               "sitemap": {"urls": [None, 5, "https://ex.test/x]", "https://ex.test/x"], "errors": None},
               "pages": {"https://ex.test/": None, "x": {"status": "500", "chain": "nope", "headers": 7},
                         "y": {"status": 0}},
               "probes": {"http_home": "no", "www_home": [None, ["a"]], "not_found": {"status": "200"}}}
    found, passed = run(hostile)
    chk(isinstance(found, list) and isinstance(passed, list), "a hostile crawl does not raise")
    chk(all(f["urls"] for f in found) and all(f["code"] for f in found), "hostile findings keep a url and a code")
    chk(copy.deepcopy(hostile) == hostile, "the crawl dict is never mutated")
    import json
    chk(json.dumps(found) is not None, "findings are JSON-serialisable")

    c = base()
    c["pages"]["https://ex.test/a/"]["chain"] = "garbage"
    c["sitemap"]["urls"] = [None, "", {"x": 1}, 42]
    found = of(c)
    chk(all(f["urls"] for f in found), "garbage sitemap entries never produce an empty url list")

    return n
