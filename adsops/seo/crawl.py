"""Polite, read-only site crawler for the AdRails technical SEO audit (adsops/seo). Read-only and pure.

Nothing here touches the network, the disk or the clock by itself: every request goes through the injected
`fetch(url)` callable, every wait through `sleep`, every timestamp through `now`. The crawler follows redirects
itself, hop by hop, so each hop is re-checked (and rate limited) like any other request. robots.txt is honoured
through `adsops.aeo.crawl_audit.parse_robots` / `can_fetch`, and sitemaps are read with regular expressions only
(no XML parser, no entity expansion). `crawl()` validates the start URL only (ToolError) and otherwise never
raises: dead hosts, redirect loops, hostile HTML, robots traps and a misbehaving injected fetcher all end up as
data (status-0 hops, notes, skipped lists). Output is the Crawl result dict of SPEC-B: JSON-serialisable only,
lists instead of sets/tuples, no datetime objects, and every size is capped so one huge site cannot blow up RAM.
"""
import datetime
import html
import re
import secrets
import time
import urllib.parse
from collections import deque

from adsops.aeo.crawl_audit import can_fetch, parse_robots
from adsops.errors import ToolError
from adsops.seo import extract, fetcher

REDIRECTS = (301, 302, 303, 307, 308)
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_ROBOTS_TEXT = 100000
MAX_BODY_CHARS = 5_000_000
MAX_DISALLOWED = 500
MAX_EXTERNAL = 5000
MAX_SITEMAP_FILES = 20
MAX_CHILD_SITEMAPS = 200
MAX_LOCATION = 500
MAX_HREF = 2048

_LOC_OPEN = re.compile(r"<loc\b[^>]{0,200}>", re.I)
_URL_OPEN = re.compile(r"<url\b[^>]{0,200}>", re.I)
_LASTMOD = re.compile(r"<lastmod[^>]{0,200}>([^<]{0,100})</lastmod>", re.I)
MAX_TEXT_KEEP = 20000          # page text kept per page for duplicate checks (memory: 2,000 pages x 20 KB)
MAX_EDGES = 300000
DEFAULT_MAX_SECONDS = 3600


def _blocks(body, opener, close, limit):
    """Linear scan for opener...close blocks (no backtracking regex: a hostile '<url><url>...' body cannot stall it)."""
    out, pos, low = [], 0, body.lower()
    while len(out) < limit:
        m = opener.search(body, pos)
        if not m:
            break
        end = low.find(close, m.end())
        if end < 0:
            break
        out.append(body[m.end():end])
        pos = end + len(close)
    return out
_SITEMAPINDEX = re.compile(r"<sitemapindex", re.I)
_NON_HEX = re.compile(r"[^0-9A-Za-z]")


# ── small defensive helpers ───────────────────────────────────────────────────────────────────────────────────
def _int(value, default=0, low=None, high=None):
    """int(value) with a default and clamps; never raises."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    if low is not None and v < low:
        v = low
    if high is not None and v > high:
        v = high
    return v


def _float(value, default=0.0, low=None, high=None):
    """float(value) with a default, NaN/inf guards, clamps and millisecond rounding; never raises."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    if v != v or v in (float("inf"), float("-inf")):
        v = default
    v = round(v, 3)
    if low is not None and v < low:
        v = low
    if high is not None and v > high:
        v = high
    return v


def _text(value):
    """str for anything that claims to be a body; bytes are decoded defensively, output capped."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            s = bytes(value).decode("utf-8", "replace")
        except Exception:                                  # noqa: BLE001 - hostile buffer object
            s = ""
    elif isinstance(value, str):
        s = value
    elif value is None:
        s = ""
    else:
        try:
            s = str(value)
        except Exception:                                  # noqa: BLE001
            s = ""
    return s[:MAX_BODY_CHARS]


def _clean(value, limit=MAX_LOCATION):
    """Whitespace-collapsed, truncated string ('' for None)."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else _text(value)
    s = " ".join(s.split())
    return s[:limit]


def _norm(url, base=None):
    """extract.normalize wrapped so hostile input can never raise."""
    try:
        return extract.normalize(url, base) or ""
    except Exception:                                      # noqa: BLE001
        return ""


def _host(url):
    """Lower-case hostname of an absolute URL ('' when unparsable)."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except (ValueError, TypeError):
        return ""


def _path_query(url):
    """Path plus '?query' for robots matching ('/' when empty)."""
    try:
        u = urllib.parse.urlsplit(url)
        return (u.path or "/") + ("?" + u.query if u.query else "")
    except (ValueError, TypeError):
        return "/"


def _headers(raw):
    """-> {lowercased name: str} capped in count and length; anything else -> {}."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, val in list(raw.items())[:60]:
        if not isinstance(key, str):
            continue
        out[key.lower()[:64]] = (val if isinstance(val, str) else _text(val))[:1000]
    return out


def _hdr(headers, name):
    """Header lookup on a plain dict of lowercased keys."""
    if not isinstance(headers, dict):
        return ""
    return _clean(headers.get(name.lower()) or "")


def _result(raw, url):
    """Normalise whatever an injected fetch returned into the fetcher.fetch() shape."""
    out = {"url": url, "status": 0, "headers": {}, "body": "", "bytes": 0, "truncated": False, "ms": 0,
           "ctype": "", "error": None}
    if not isinstance(raw, dict):
        out["error"] = "no result from fetch"
        return out
    out["status"] = _int(raw.get("status"), 0, 0, 999)
    out["headers"] = _headers(raw.get("headers"))
    out["body"] = _text(raw.get("body"))
    out["bytes"] = _int(raw.get("bytes"), 0, 0, 10 ** 9)
    out["truncated"] = bool(raw.get("truncated"))
    out["ms"] = _int(raw.get("ms"), 0, 0, 10 ** 9)
    ctype = raw.get("ctype") or out["headers"].get("content-type") or ""
    out["ctype"] = _clean(ctype, 120).split(";")[0].strip().lower()        # "text/html; charset=utf-8" -> "text/html"
    err = raw.get("error")
    out["error"] = None if err in (None, "") else _text(err)[:200]
    return out


def _iso(ts):
    """ISO-8601 UTC string from a unix timestamp; never raises."""
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError, TypeError):
        return "1970-01-01T00:00:00+00:00"


# ── the crawl ────────────────────────────────────────────────────────────────────────────────────────────────
def crawl(start_url, fetch=None, user_agent=fetcher.GOOGLEBOT_UA, max_pages=500, rate_limit_s=1.0,
          respect_robots=True, sitemap_seeding=True, timeout_s=15, max_bytes=3_000_000, max_redirects=5,
          extra_urls=(), sleep=time.sleep, now=time.time, rand_hex=None, max_sitemap_urls=5000,
          max_seconds=DEFAULT_MAX_SECONDS):
    """Crawl one site politely and return the Crawl result dict of SPEC-B. Only ToolError on a bad start_url."""
    start = _norm(start_url)
    if not start:
        raise ToolError("start_url must be an absolute http(s) URL")
    max_pages = _int(max_pages, 500, 1, 5000)
    max_redirects = _int(max_redirects, 5, 0, 10)
    max_sitemap_urls = _int(max_sitemap_urls, 5000, 1, 200000)
    timeout_s = _int(timeout_s, 15, 1, 300)
    max_bytes = _int(max_bytes, 3_000_000, 1024, 50_000_000)
    rate = _float(rate_limit_s, 1.0, 0.2, 600.0)                 # 0.2 s is the polite floor
    if fetch is None:
        def fetch(u, _ua=user_agent, _to=timeout_s, _mb=max_bytes):
            return fetcher.fetch(u, user_agent=_ua, timeout=_to, max_bytes=_mb)
    try:
        seed = str(rand_hex()) if callable(rand_hex) else str(rand_hex or "")
    except Exception:                                            # noqa: BLE001
        seed = ""
    nonce = _NON_HEX.sub("", seed)[:32] or secrets.token_hex(6)

    started_at = _iso(now())
    parts = urllib.parse.urlsplit(start)
    scheme = parts.scheme or "https"
    host = (parts.hostname or "").lower()
    port_sfx = (":%d" % parts.port) if parts.port else ""          # normalize() already dropped default ports
    origin = scheme + "://" + host + port_sfx
    bare = host[4:] if host.startswith("www.") else host
    other_www = bare if host != bare else "www." + bare          # the www variant we are not on
    scope_hosts = list(dict.fromkeys([host, other_www]))
    scope_set = set(scope_hosts)
    notes = []
    queue_cap = max(500, max_pages * 20)
    budget = max_pages * 3 + 60                                   # hard cap on fetch() calls

    def in_scope(url):
        return _host(url) in scope_set

    # ── politeness + request budget: every single request goes through _get ──────────────────────────────────
    state = {"calls": 0, "last": None, "budget_noted": False, "t0": now(), "deadline_noted": False}
    max_secs = _float(max_seconds, DEFAULT_MAX_SECONDS, 10.0, 6 * 3600.0)
    allowed = lambda u: True                                      # replaced once robots.txt is read

    def _get(url):
        if now() - state["t0"] > max_secs:
            if not state["deadline_noted"]:
                state["deadline_noted"] = True
                notes.append("stopped after max_seconds=%d; remaining URLs were not fetched" % max_secs)
            return _result({"status": 0, "error": "crawl deadline reached"}, url)
        if state["calls"] >= budget:
            if not state["budget_noted"]:
                state["budget_noted"] = True
                notes.append("request budget of %d fetches was exhausted; remaining URLs were not fetched" % budget)
            return _result({"status": 0, "error": "request budget exhausted"}, url)
        t = now()
        if state["last"] is not None:
            wait = rate - (t - state["last"])
            if wait > 0:
                sleep(wait)
        state["last"] = now()
        state["calls"] += 1
        try:
            raw = fetch(url)
        except Exception as e:                                   # noqa: BLE001 - a dead URL is data, not a crash
            raw = {"url": url, "status": 0, "error": "fetch raised " + type(e).__name__}
        return _result(raw, url)

    def chain(url):
        """Follow redirects manually -> ([[url, status, location], ...], last fetch result)."""
        hops_list, cur, hops = [], url, 0
        res = _result({"status": 0, "error": "not fetched"}, cur)
        while True:
            res = _get(cur)
            status = _int(res.get("status"), 0, 0, 999)
            loc = _hdr(res.get("headers"), "location")
            hops_list.append([cur, status, loc])
            if status not in REDIRECTS or not loc:
                break
            nxt = _norm(loc, cur)
            if not nxt:
                break
            if not allowed(nxt):
                hops_list.append([nxt, 0, "robots.txt disallows this redirect target (not followed)"])
                break
            if any(entry[0] == nxt for entry in hops_list):
                hops_list.append([nxt, 0, "redirect loop"])
                break
            if not in_scope(nxt):
                hops_list.append([nxt, 0, "off-scope redirect (not followed)"])
                break
            hops += 1
            if hops > max_redirects:
                hops_list.append([nxt, 0, "too many redirects"])
                break
            cur = nxt
        return hops_list, res

    # ── robots.txt ──────────────────────────────────────────────────────────────────────────────────────────
    robots_url = origin + "/robots.txt"
    r_chain, r_res = chain(robots_url)
    r_status = r_chain[-1][1]
    r_text = _text(r_res.get("body"))[:MAX_ROBOTS_TEXT] if r_status == 200 else ""
    try:
        parsed = parse_robots(r_text) or {}
    except Exception:                                            # noqa: BLE001
        parsed = {}
    robots_sitemaps = []
    for raw in parsed.get("sitemaps") or []:
        s = _clean(raw, 2000)
        if s:
            robots_sitemaps.append(_norm(s, robots_url) or s)
    robots = {"url": robots_url, "status": r_status, "text": r_text, "sitemaps": robots_sitemaps}
    if r_status == 0 or r_status >= 500 or r_status == 429:
        notes.append("robots.txt returned HTTP %d; Google would pause crawling; this audit crawled anyway" % r_status)
    elif r_status not in (200, 404, 410):
        notes.append("robots.txt returned HTTP %d; like Google, the audit treats that as no restrictions" % r_status)
    agent = "Googlebot" if "googlebot" in str(user_agent).lower() else (str(user_agent).split("/")[0] or "*")

    def allowed(url):
        if not respect_robots or r_status != 200:
            return True
        try:
            return bool(can_fetch(parsed, agent, _path_query(url)))
        except Exception:                                        # noqa: BLE001 - fail closed, once noted
            if "robots.txt rule evaluation failed; affected URLs were skipped" not in notes:
                notes.append("robots.txt rule evaluation failed; affected URLs were skipped")
            return False

    # ── sitemap seeding (regex only, no XML parser) ─────────────────────────────────────────────────────────
    sitemap = {"urls": [], "lastmod": {}, "sources": [], "errors": [], "truncated": False}
    sitemap_set = set()
    if sitemap_seeding:
        candidates = [u for u in robots_sitemaps if in_scope(u)]
        if not candidates:
            candidates = [origin + "/sitemap.xml"]
        pending, seen_sm = [], set()
        for cand in candidates:
            if cand not in seen_sm:
                seen_sm.add(cand)
                pending.append(cand)
        idx, stop = 0, False
        while idx < len(pending) and len(sitemap["sources"]) < MAX_SITEMAP_FILES and not stop:
            sm_url = pending[idx]
            idx += 1
            if sm_url.split("?")[0].lower().endswith(".gz"):
                sitemap["errors"].append("gzip sitemap not read: " + sm_url)
                continue
            sm_chain, sm_res = chain(sm_url)
            sm_status = sm_chain[-1][1]
            sitemap["sources"].append(sm_url)
            if sm_status != 200:
                sitemap["errors"].append("sitemap %s returned HTTP %d" % (sm_url, sm_status))
                continue
            body = _text(sm_res.get("body"))
            base = sm_chain[-1][0]
            if _SITEMAPINDEX.search(body):
                children = 0
                for loc in _blocks(body, _LOC_OPEN, "</loc>", MAX_CHILD_SITEMAPS * 2):
                    if children >= MAX_CHILD_SITEMAPS:
                        break
                    child = _norm(_unescape(loc), base)
                    if not child or not in_scope(child) or child in seen_sm:
                        continue
                    seen_sm.add(child)
                    pending.append(child)
                    children += 1
                continue
            for block in _blocks(body, _URL_OPEN, "</url>", max_sitemap_urls * 2 + 10):
                locs = _blocks(block, _LOC_OPEN, "</loc>", 1)
                if not locs:
                    continue
                loc_url = _norm(_unescape(locs[0][:2048]), base)
                if not loc_url or not in_scope(loc_url) or loc_url in sitemap_set:
                    continue
                if len(sitemap["urls"]) >= max_sitemap_urls:
                    sitemap["truncated"] = True
                    stop = True
                    break
                sitemap["urls"].append(loc_url)
                sitemap_set.add(loc_url)
                lm = _LASTMOD.search(block)
                if lm:
                    value = _clean(_unescape(lm.group(1)), 64)
                    if value:
                        sitemap["lastmod"][loc_url] = value
        if sitemap["errors"]:
            notes.append("sitemap reading produced %d error(s)" % len(sitemap["errors"]))

    # ── queue: link_q first, side_q (extra + sitemap) only when links are exhausted ─────────────────────────
    link_q, side_q = deque([start]), deque()
    seen, kinds = {start}, {start: "start"}
    for extra in list(extra_urls or ())[:2000]:
        url = extract.strip_tracking(_norm(extra)) if isinstance(extra, str) else ""
        if not url or not in_scope(url) or url in seen:
            continue
        seen.add(url)
        kinds[url] = "ad"
        side_q.append(url)
    if sitemap_seeding:
        for url in sitemap["urls"]:
            url = extract.strip_tracking(url)
            if url in seen:
                continue
            seen.add(url)
            kinds[url] = "sitemap"
            side_q.append(url)

    pages, edges, external_links = {}, [], {}
    skipped = {"robots_disallowed": [], "off_scope": 0, "cap_reached": False, "non_html": 0}
    dropped = False

    while (link_q or side_q) and len(pages) < max_pages:
        url = link_q.popleft() if link_q else side_q.popleft()
        if url in pages:
            continue
        if not allowed(url):
            if len(skipped["robots_disallowed"]) < MAX_DISALLOWED:
                skipped["robots_disallowed"].append(url)
            continue
        ch, res = chain(url)
        status = ch[-1][1]
        final = ch[-1][0]
        synthetic = len(ch) > 1 and ch[-1][1] == 0 and not res.get("error")
        if len(ch) > 1 and str(ch[-1][2]).startswith("off-scope"):
            status = ch[-2][1]                    # a redirect to another site is a redirect, not a dead page
        ctype = _clean(res.get("ctype") or "", 120).lower()
        err = res.get("error")
        page = {"url": url, "chain": ch, "status": status, "final": final, "ctype": ctype,
                "bytes": _int(res.get("bytes"), 0, 0, 10 ** 9), "truncated": bool(res.get("truncated")),
                "ms": _int(res.get("ms"), 0, 0, 10 ** 9), "headers": res.get("headers") or {},
                "error": (str(ch[-1][2])[:200] if synthetic and status == 0 else
                          (None if err in (None, "") else _text(err)[:200])), "depth": None,
                "in_sitemap": url in sitemap_set, "discovered": kinds.get(url, "link"), "doc": None}
        pages[url] = page
        if final != url and final not in seen and in_scope(final):
            seen.add(final)
            kinds[final] = kinds.get(url, "link")
            link_q.append(final)
        if status == 200 and ctype in HTML_TYPES and in_scope(final) and not page["error"]:
            try:
                doc = extract.parse(res.get("body") or "", final)
            except Exception:                                    # noqa: BLE001 - parse() should never raise
                doc = None
            page["doc"] = doc
            for link in (doc or {}).get("links") or []:
                href = link.get("href") if isinstance(link, dict) else None
                if not isinstance(href, str) or not href or len(href) > MAX_HREF:
                    continue                                          # hostile/absurd href: never queue it
                if in_scope(href):
                    if len(edges) < MAX_EDGES:
                        edges.append([url, href, _clean(link.get("anchor"), 500), _clean(link.get("rel"), 200),
                                      bool(link.get("in_nav"))])
                    else:
                        dropped = True
                    href = extract.strip_tracking(href)       # one fetch per page; tagged links stay in the edges
                    if href not in seen:
                        if len(link_q) + len(side_q) >= queue_cap:
                            dropped = True
                        else:
                            seen.add(href)
                            kinds[href] = "link"
                            link_q.append(href)
                else:
                    skipped["off_scope"] += 1
                    if href in external_links or len(external_links) < MAX_EXTERNAL:
                        external_links[href] = external_links.get(href, 0) + 1
            if doc and len(doc.get("text") or "") > MAX_TEXT_KEEP:
                doc["text"] = doc["text"][:MAX_TEXT_KEEP]          # words keeps the full count
        elif status == 200:
            skipped["non_html"] += 1

    if (link_q or side_q) and len(pages) >= max_pages:
        skipped["cap_reached"] = True
        notes.append("stopped after max_pages=%d; more URLs were still queued" % max_pages)
    if dropped:
        skipped["cap_reached"] = True
        notes.append("the link queue or edge list hit its size cap; some discovered URLs were not crawled")

    # ── depth: BFS clicks from start over the edges, redirecting pages pass their depth to their final ──────
    adj = {}
    for src, dst, *_rest in edges:
        adj.setdefault(src, set()).add(dst)
    hop = {u: p.get("final") for u, p in pages.items() if p.get("final") and p.get("final") != u}
    depth = {start: 0}
    queue = deque([start])                       # 0-1 BFS: a link costs one click, a redirect hop costs none
    while queue:
        node = queue.popleft()
        d = depth[node]
        fin = hop.get(node)
        if fin and (fin not in depth or depth[fin] > d):
            depth[fin] = d
            queue.appendleft(fin)
        for nxt in adj.get(node, ()):
            if nxt not in depth or depth[nxt] > d + 1:
                depth[nxt] = d + 1
                queue.append(nxt)
    for url, page in pages.items():
        page["depth"] = depth.get(url)

    # ── probes (always, after the crawl) ────────────────────────────────────────────────────────────────────
    other_scheme = "https" if scheme == "http" else "http"
    probes = {"http_home": chain(other_scheme + "://" + host + "/")[0],
              "www_home": chain(scheme + "://" + other_www + port_sfx + "/")[0],
              "not_found": {}}
    nf_url = origin + "/adrails-404-check-" + nonce + "/"
    probes["not_found"] = {"url": nf_url, "status": _int(_get(nf_url).get("status"), 0, 0, 999)}

    settings = {"start_url": start, "user_agent": str(user_agent), "max_pages": max_pages,
                "rate_limit_s": rate, "scope_hosts": scope_hosts, "respect_robots": bool(respect_robots),
                "timeout_s": timeout_s, "max_bytes": max_bytes, "sitemap_seeding": bool(sitemap_seeding),
                "started_at": started_at, "finished_at": _iso(now())}
    return {"settings": settings, "robots": robots, "sitemap": sitemap, "pages": pages, "edges": edges,
            "external_links": external_links, "probes": probes, "skipped": skipped, "notes": notes}


def _unescape(value):
    """HTML-unescape and strip a sitemap text node (entities decoded, never expanded into markup)."""
    if not isinstance(value, str):
        return ""
    try:
        return html.unescape(value).strip()
    except Exception:                                            # noqa: BLE001
        return value.strip()


# ── selftest ────────────────────────────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    """Offline assertions over a fake site (fake fetch, fake sleep, fake clock). Returns the number of checks."""
    import json
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "seo.crawl.selftest: " + msg
        n += 1

    HOME = ('<html lang="en"><head><title>Home</title></head><body><h1>Home</h1>'
            '<a href="/a/">A</a><a href="/old">old</a><a href="/loop1">loop</a>'
            '<a href="http://ex.test/b">b</a><a href="https://other.test/">ext</a>'
            '<a href="/private/x">p</a><a href="/pdf">pdf</a><a href="mailto:x@y.test">mail</a></body></html>')
    A_PAGE = '<html><body><a href="/c/">c</a></body></html>'
    C_PAGE = '<html><body><p>plain c page with words</p></body></html>'
    CHILD = ('<urlset><url><loc>https://ex.test/</loc><lastmod>2024-01-01</lastmod></url>'
             '<url><loc>https://ex.test/a/</loc></url><url><loc> https://ex.test/orphan/ </loc></url>'
             '<url><loc>https://ex.test/private/x</loc></url><url><loc>https://other.test/z</loc></url></urlset>')
    SITE = {
        "https://ex.test/robots.txt": (200, "text/plain",
                                       "User-agent: *\nDisallow: /private/\n\nSitemap: https://ex.test/sitemap.xml\n",
                                       ""),
        "https://ex.test/sitemap.xml": (200, "application/xml",
                                        "<sitemapindex><sitemap><loc>https://ex.test/sitemap-child.xml</loc>"
                                        "</sitemap></sitemapindex>", ""),
        "https://ex.test/sitemap-child.xml": (200, "application/xml", CHILD, ""),
        "https://ex.test/": (200, "text/html", HOME, ""),
        "https://ex.test/a/": (200, "text/html", A_PAGE, ""),
        "https://ex.test/c/": (200, "text/html", C_PAGE, ""),
        "https://ex.test/extra/": (200, "text/html", C_PAGE, ""),
        "https://ex.test/orphan/": (200, "text/html", C_PAGE, ""),
        "https://ex.test/old": (301, "", "", "/a/"),
        "https://ex.test/loop1": (301, "", "", "/loop2"),
        "https://ex.test/loop2": (301, "", "", "/loop1"),
        "https://ex.test/pdf": (200, "application/pdf", "%PDF-1.4 body", ""),
        "http://ex.test/b": (200, "text/html", C_PAGE, ""),
        "http://ex.test/": (301, "", "", "https://ex.test/"),
        "https://www.ex.test/": (301, "", "", "https://ex.test/"),
    }

    def run(**kw):
        clock = {"t": 0.0}
        sleeps, calls = [], []

        def f_now():
            clock["t"] += 0.01
            return clock["t"]

        def f_sleep(x):
            sleeps.append(x)
            clock["t"] += max(0.0, float(x))

        def f_fetch(url):
            calls.append((url, f_now()))
            rec = SITE.get(url)
            if rec is None and "adrails-404-check-" in url:
                return {"url": url, "status": 404, "headers": {"content-type": "text/html"}, "body": "nope",
                        "bytes": 4, "truncated": False, "ms": 1, "ctype": "text/html", "error": None}
            if rec is None:
                return {"url": url, "status": 0, "headers": {}, "body": "", "bytes": 0, "truncated": False,
                        "ms": 0, "ctype": "", "error": "ConnectError"}
            status, ctype, body, loc = rec
            headers = {"content-type": ctype} if ctype else {}
            if loc:
                headers["location"] = loc
            return {"url": url, "status": status, "headers": headers, "body": body,
                    "bytes": len(body.encode("utf-8")), "truncated": False, "ms": 2,
                    "ctype": ctype.split(";")[0].strip().lower(), "error": None}

        args = {"fetch": f_fetch, "sleep": f_sleep, "now": f_now, "rand_hex": lambda: "abc123def456",
                "rate_limit_s": 0.2}
        args.update(kw)
        return crawl("https://ex.test/", **args), calls, sleeps, clock

    out, calls, sleeps, _clock = run()
    fetched = [u for u, _t in calls]

    ck(json.dumps(out) is not None and isinstance(json.dumps(out), str), "result is json.dumps-able")
    ck(set(out["pages"]) == {"https://ex.test/", "https://ex.test/a/", "https://ex.test/c/", "https://ex.test/old",
                             "https://ex.test/loop1", "https://ex.test/orphan/", "https://ex.test/pdf",
                             "http://ex.test/b"}, "pages keys are the sites pages")
    ck(out["pages"]["https://ex.test/old"]["chain"] == [[
        "https://ex.test/old", 301, "/a/"], ["https://ex.test/a/", 200, ""]], "chain records every hop")
    ck(out["pages"]["https://ex.test/old"]["status"] == 200
       and out["pages"]["https://ex.test/old"]["final"] == "https://ex.test/a/", "final hop wins status/final")
    ck(out["pages"]["https://ex.test/loop1"]["chain"][-1] == ["https://ex.test/loop1", 0, "redirect loop"],
       "redirect loop detected and stopped")
    ck(out["pages"]["https://ex.test/pdf"]["doc"] is None and out["skipped"]["non_html"] == 1,
       "non-html 200 page kept without a doc")
    ck(out["robots"]["status"] == 200 and out["robots"]["sitemaps"] == ["https://ex.test/sitemap.xml"],
       "robots.txt read and sitemap line found")
    ck(out["skipped"]["robots_disallowed"] == ["https://ex.test/private/x"], "disallowed URL recorded")
    ck("https://ex.test/private/x" not in fetched, "disallowed URL never fetched")
    ck(not any("other.test" in u for u in fetched), "off-scope URLs never fetched")
    ck(out["external_links"] == {"https://other.test/": 1} and out["skipped"]["off_scope"] == 1,
       "external link counted once, off-scope counted")
    ck(out["pages"]["https://ex.test/"]["depth"] == 0 and out["pages"]["https://ex.test/a/"]["depth"] == 1,
       "depths: home 0, /a/ 1")
    ck(out["pages"]["https://ex.test/c/"]["depth"] == 2 and out["pages"]["https://ex.test/orphan/"]["depth"] is None,
       "depths: /c/ 2, orphan unreachable")
    ck(out["pages"]["https://ex.test/"]["in_sitemap"] and out["pages"]["https://ex.test/orphan/"]["in_sitemap"]
       and not out["pages"]["https://ex.test/c/"]["in_sitemap"], "in_sitemap flags")
    ck(out["pages"]["https://ex.test/"]["discovered"] == "start"
       and out["pages"]["https://ex.test/orphan/"]["discovered"] == "sitemap"
       and out["pages"]["https://ex.test/c/"]["discovered"] == "link", "discovered values")
    ck(out["sitemap"]["urls"] == ["https://ex.test/", "https://ex.test/a/", "https://ex.test/orphan/",
                                  "https://ex.test/private/x"], "sitemap urls normalised, in scope, in order")
    ck(out["sitemap"]["lastmod"] == {"https://ex.test/": "2024-01-01"} and not out["sitemap"]["truncated"],
       "sitemap lastmod captured, not truncated")
    ck(out["sitemap"]["sources"] == ["https://ex.test/sitemap.xml", "https://ex.test/sitemap-child.xml"],
       "sitemap index child sitemap followed")
    ck(out["probes"]["http_home"] == [["http://ex.test/", 301, "https://ex.test/"],
                                      ["https://ex.test/", 200, ""]], "http_home probe chain")
    ck(out["probes"]["www_home"] == [["https://www.ex.test/", 301, "https://ex.test/"],
                                     ["https://ex.test/", 200, ""]], "www_home probe chain")
    ck(out["probes"]["not_found"]["status"] == 404
       and out["probes"]["not_found"]["url"] == "https://ex.test/adrails-404-check-abc123def456/",
       "not_found probe with the injected nonce")
    gaps = [b[1] - a[1] for a, b in zip(calls, calls[1:])]
    ck(bool(gaps) and min(gaps) >= 0.2 - 1e-9, "every request start is >= rate_limit_s apart")
    ck(all(x >= 0 for x in sleeps), "sleep is only called with a positive wait")
    ck(out["settings"]["scope_hosts"] == ["ex.test", "www.ex.test"]
       and out["settings"]["rate_limit_s"] == 0.2, "settings: scope hosts and clamped rate")
    ck(out["settings"]["started_at"].endswith("+00:00") and out["settings"]["finished_at"] >=
       out["settings"]["started_at"], "settings timestamps are ISO-8601 UTC")
    ck(["https://ex.test/", "https://ex.test/a/", "A", "", False] in out["edges"], "internal edge recorded")

    out2, calls2, _s2, _c2 = run(max_pages=2)
    ck(len(out2["pages"]) == 2 and out2["skipped"]["cap_reached"] is True, "max_pages stops the crawl")
    ck(len(calls2) < 2 * 3 + 60 + 20, "max_pages=2 crawl stays well inside the request budget")

    out3, _c3, _s3, _c3b = run(respect_robots=False)
    ck("https://ex.test/private/x" in out3["pages"], "respect_robots=False crawls the disallowed URL")

    out4, calls4, _s4, _c4 = run(extra_urls=["https://ex.test/extra/", "https://other.test/q", "/relative"])
    ck(out4["pages"].get("https://ex.test/extra/", {}).get("discovered") == "ad"
       and "https://other.test/q" not in [u for u, _t in calls4], "extra_urls: in-scope seeded, off-scope dropped")

    out5, _c5, _s5, _c5b = run(sitemap_seeding=False, max_pages=3)
    ck(out5["sitemap"] == {"urls": [], "lastmod": {}, "sources": [], "errors": [], "truncated": False},
       "sitemap_seeding=False leaves the sitemap empty")

    for bad in ("", "not a url", "ftp://ex.test/", "mailto:a@b.test", None, 42):
        try:
            crawl(bad, fetch=lambda u: {}, sleep=lambda x: None)
            ck(False, "bad start_url must raise ToolError")
        except ToolError:
            ck(True, "bad start_url raises ToolError")
    try:
        empty_out = crawl("https://ex.test/", fetch=lambda u: {}, sleep=lambda x: None, respect_robots=True)
        ck(isinstance(empty_out.get("pages"), dict), "an empty fetch result never raises")
    except Exception:                                            # noqa: BLE001
        ck(False, "an empty fetch result never raises")

    hostile = '<html><body><div>' * 2000 + '<a href="/x">' + 'y' * 50000 + '</a>\x00<a href="' + "z" * 100000 + '">'

    def hostile_fetch(url):
        if "adrails-404-check" in url:
            return {"status": 404}
        return {"status": 200, "ctype": "text/html", "headers": {"content-type": "text/html"}, "body": hostile}
    h_out = crawl("https://ex.test/", fetch=hostile_fetch, rate_limit_s=0.5, sleep=lambda x: None,
                  now=lambda: 0.0, max_pages=2, respect_robots=False, sitemap_seeding=False)
    ck(isinstance(h_out["pages"]["https://ex.test/"]["doc"], dict), "hostile HTML still yields a document")
    lk = h_out["pages"]["https://ex.test/"]["doc"]["links"]
    ck(json.dumps(h_out) is not None and isinstance(lk, list) and all(isinstance(x, dict) for x in lk)
       and all(len(x.get("href", "")) < 2000 for x in lk), "hostile HTML: links capped, result serialisable")

    def raising_fetch(url):
        raise ValueError("boom")
    e_out = crawl("https://ex.test/", fetch=raising_fetch, respect_robots=False, sitemap_seeding=False,
                  rate_limit_s=0.2, sleep=lambda x: None, now=lambda: 0.0)
    page = e_out["pages"]["https://ex.test/"]
    ck(page["status"] == 0 and page["error"] and page["doc"] is None, "a raising fetcher becomes a status-0 page")

    junk_out = crawl("https://ex.test/", fetch=lambda u: None, respect_robots=False, sitemap_seeding=False,
                     rate_limit_s=0.2, sleep=lambda x: None, now=lambda: 0.0)
    ck(junk_out["pages"]["https://ex.test/"]["error"] == "no result from fetch", "a None fetch result is data")

    SITE["https://ex.test/go"] = (301, "", "", "/private/x")
    rb, rb_calls, _r1, _r2 = run(extra_urls=["https://ex.test/go"])
    ck("https://ex.test/private/x" not in [u for u, _t in rb_calls]
       and rb["pages"]["https://ex.test/go"]["chain"][-1][2].startswith("robots.txt disallows"),
       "a redirect into a Disallowed path is not followed")
    del SITE["https://ex.test/go"]
    ck(out["pages"]["https://ex.test/loop1"]["error"] == "redirect loop", "a loop page carries its reason as error")
    import time as _t
    t0 = _t.perf_counter()
    _blocks("<url>" * 400000, _URL_OPEN, "</url>", 1000)
    _blocks("<url><loc>" * 200000, _LOC_OPEN, "</loc>", 1000)
    ck(_t.perf_counter() - t0 < 2.0, "hostile sitemap bodies are scanned in linear time")
    many = '<html><body>' + "".join('<a href="/p%d">p</a>' % i for i in range(3000)) + '</body></html>'
    q_out = crawl("https://ex.test/", fetch=lambda u: {"status": 200, "ctype": "text/html", "body": many},
                  sleep=lambda x: None, now=lambda: 0.0, max_pages=1, respect_robots=False, sitemap_seeding=False)
    ck(q_out["skipped"]["cap_reached"] and q_out["notes"], "a full queue is reported, never silent")
    clock2 = {"t": 0.0}

    def slow_now():
        clock2["t"] += 30.0
        return clock2["t"]
    d_out = crawl("https://ex.test/", fetch=lambda u: {"status": 200, "ctype": "text/html", "body": A_PAGE},
                  sleep=lambda x: None, now=slow_now, max_seconds=60, respect_robots=False, sitemap_seeding=False)
    ck(any("max_seconds" in x for x in d_out["notes"]), "the crawl stops at its wall-clock deadline")

    tight, tight_calls, _ts, _tc = run(max_pages=3, max_redirects=0)
    ck(tight["pages"]["https://ex.test/loop1"]["chain"][-1][2] == "too many redirects",
       "max_redirects=0 refuses to follow any redirect")
    ck(len(tight["pages"]) == 3 and tight["skipped"]["cap_reached"] is True, "capped crawl reports cap_reached")
    return n
