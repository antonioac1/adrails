"""Join Google Search Console data onto an AdRails site-audit crawl (adsops/seo).

Read-only and pure: no network, disk or clock access at import time, no global mutable state and no I/O at import.
The Search Console client is injected (duck-typed: ``sites()``, ``query(site, start, end, dims, row_limit=)``,
``inspect(site, url)``) and every call may raise ``ToolError`` (403, quota, "not connected"); those are caught,
scrubbed and reported instead of propagated. Three things are produced:

* URL Inspection per crawled URL (verdict, coverage state, Google-selected canonical) next to the crawl's own
  explanation for the same page (``explain`` uses ``why_not_indexable`` from checks_content);
* search analytics by page for a trailing window ending three days back (GSC data lags), aggregated with
  ``adsops.metrics.gsc.aggregate``;
* "excluded pages and why", merging Google's coverage state with the crawl-side reason.

Results are plain JSON-serialisable dicts; nothing here raises on hostile crawl data, hostile clients or missing
keys. Findings follow the shared contract {"code", "urls", "detail", "data"} with lists capped at 20 examples.
"""
import datetime
from urllib.parse import urlsplit

from adsops.errors import ToolError, scrub
from adsops.metrics import gsc
from adsops.seo.checks_content import is_indexable, why_not_indexable

MAX_STR = 500
MAX_LIST = 20
MAX_URLS = 500
MAX_PAGES = 2000
MAX_INSPECT = 2000
LAG_DAYS = 3
DEFAULT_DAYS = 28
DEFAULT_INSPECT = 50

NOTE_NOT_CONNECTED = ("Search Console not connected: add the Search Console scope to your Google credential "
                      "(see adsops_search_console) or pass site and credentials; the crawl findings stand on their own.")
NOTE_NO_PROPERTY = "no Search Console property for %s is visible to this credential"


# ---------------------------------------------------------------------------------------------------------------
# total helpers: never raise, never touch the outside world
# ---------------------------------------------------------------------------------------------------------------
def _str(value, limit=MAX_STR):
    """Whitespace-collapsed string of `value` ('' for None/garbage), truncated to `limit` characters."""
    if value is None:
        return ""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    s = " ".join(s.split())
    return s[:limit] if limit else s


def _int(value, default=0):
    """Integer of `value`, or `default` when it is missing or not number-like."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi, default):
    """`value` as an integer clamped to [lo, hi]; malformed input uses `default`."""
    v = _int(value, default)
    return max(lo, min(hi, v))


def _num(value, default=0.0):
    """Float of `value` (blank/garbage -> default); never NaN/Infinity leaking out."""
    if value is None or value == "":
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _round(value, nd):
    return round(_num(value), nd)


def _as_date(value):
    """date of `value` (date/datetime/ISO string), else None."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _host(url):
    """Lowercased hostname of `url` ('' when there is none); tolerates scheme-less input."""
    s = _str(url)
    if not s:
        return ""
    try:
        host = urlsplit(s if "//" in s else "//" + s).hostname or ""
    except ValueError:
        return ""
    return host.lower().strip(".")


def _pages(crawl):
    """-> {url: page} for the crawl's pages, tolerating a list of page dicts or garbage."""
    raw = crawl.get("pages") if isinstance(crawl, dict) else None
    out = {}
    if isinstance(raw, dict):
        for key, page in raw.items():
            if isinstance(key, str):
                out[key] = page
    elif isinstance(raw, (list, tuple)):
        for page in raw:
            if isinstance(page, dict) and isinstance(page.get("url"), str):
                out.setdefault(page["url"], page)
    return out


def _page_url(key, page):
    """The URL a page dict should be addressed by: its own "url" when sane, else the crawl key."""
    url = page.get("url") if isinstance(page, dict) else None
    if isinstance(url, str) and url.strip():
        return url
    return key if isinstance(key, str) else ""


def _is_pass(verdict):
    return _str(verdict, 40).upper() == "PASS"


def _safe_indexable(page):
    """`is_indexable(page)` that cannot raise on hostile documents."""
    try:
        return bool(is_indexable(page))
    except Exception:
        return False


def _stats(pages, url):
    """Stats row for `url` from the aggregated analytics dict, trying equivalent spellings."""
    if not isinstance(url, str) or not isinstance(pages, dict):
        return {}
    bare = url.split("#", 1)[0]
    for candidate in (url, bare, url.rstrip("/"), bare.rstrip("/")):
        if candidate and candidate in pages and isinstance(pages[candidate], dict):
            return pages[candidate]
    return {}


def _call(fn, *args, **kwargs):
    """Call an injected client method -> (result, scrubbed_error_text); nothing propagates."""
    try:
        return fn(*args, **kwargs), ""
    except ToolError as exc:
        return None, scrub(str(exc))
    except Exception as exc:  # a duck-typed client may break in any way; that is not our crash
        return None, scrub("%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------------------------------------------
# public helpers
# ---------------------------------------------------------------------------------------------------------------
def pick_site(crawl_start_url, visible_sites):
    """Pick the Search Console property for a start URL: sc-domain first, then URL-prefix variants, else None."""
    host = _host(crawl_start_url)
    if not host:
        return None
    bare = host[4:] if host.startswith("www.") else host
    candidates = ["sc-domain:" + bare, "https://" + host + "/", "https://www." + bare + "/",
                  "https://" + bare + "/", "http://" + host + "/"]
    available = {}
    for item in visible_sites if isinstance(visible_sites, (list, tuple)) else []:
        name = item.get("site") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name.strip():
            continue
        available.setdefault(name.strip().lower().rstrip("/"), name)
    for candidate in candidates:
        key = candidate.lower().rstrip("/")
        if key in available:
            return available[key]
    return None


def explain(page):
    """Short crawl-side explanation for a page: why it is not indexable, or orphan / not-in-sitemap / indexable."""
    page = page if isinstance(page, dict) else {}
    if "status" not in page:
        return "unknown page"
    try:
        reason = why_not_indexable(page)
    except Exception:
        reason = None
    if reason:
        return _str(reason)
    if page.get("depth") is None:
        return "orphan: no internal links"
    if not page.get("in_sitemap"):
        return "not in sitemap"
    return "indexable"


def inspect_order(crawl):
    """Inspection priority: 200-HTML pages in the sitemap, then other indexable pages, then the rest of 200 HTML."""
    seen = set()
    sitemap, indexable, rest = [], [], []
    for key, page in _pages(crawl).items():
        if not isinstance(page, dict):
            continue
        url = _page_url(key, page)
        if not url or url in seen:
            continue
        seen.add(url)
        html_200 = _int(page.get("status"), -1) == 200 and isinstance(page.get("doc"), dict)
        if html_200 and page.get("in_sitemap"):
            sitemap.append(url)
        elif _safe_indexable(page):
            indexable.append(url)
        elif html_200:
            rest.append(url)
        else:
            continue
    return sitemap + indexable + rest


# ---------------------------------------------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------------------------------------------
def join(crawl, client, site=None, today=None, days=DEFAULT_DAYS, max_inspect=DEFAULT_INSPECT):
    """Join GSC search analytics + URL Inspection onto `crawl` (see module docstring); read-only, never raises."""
    crawl = crawl if isinstance(crawl, dict) else {}
    settings = crawl.get("settings") if isinstance(crawl.get("settings"), dict) else {}
    start_url = _str(settings.get("start_url"))
    budget = _clamp(max_inspect, 0, MAX_INSPECT, DEFAULT_INSPECT)
    window = _clamp(days, 1, 3650, DEFAULT_DAYS)
    if client is None:
        return {"connected": False, "note": NOTE_NOT_CONNECTED}

    errors, notes, inspected = [], [], []
    if not site:
        raw_sites, err = _call(getattr(client, "sites", lambda: []))
        if err:
            low = err.lower()
            if "403" in low or "not connected" in low or "credential" in low:
                return {"connected": False, "note": err}
            errors.append(err)
        site = pick_site(start_url, raw_sites)
        if not site:
            return {"connected": False, "note": NOTE_NO_PROPERTY % (_host(start_url) or "this site")}
    site = _str(site)

    end = (_as_date(today) or datetime.date.today()) - datetime.timedelta(days=LAG_DAYS)
    start = end - datetime.timedelta(days=window - 1)
    rows, err = _call(getattr(client, "query", None) or (lambda *a, **k: []),
                      site, start.isoformat(), end.isoformat(), ["page"])
    if err:
        errors.append(err)
        notes.append("search analytics could not be read; clicks below are unknown, not zero")
    try:
        aggregated = gsc.aggregate(rows if isinstance(rows, (list, tuple)) else [], ["page"], "page")
    except ToolError as exc:
        errors.append(scrub(str(exc)))
        aggregated = []
    pages = {}
    for row in aggregated if isinstance(aggregated, list) else []:
        if not isinstance(row, dict):
            continue
        url = _str(row.get("page"))
        if not url:
            continue
        pages[url] = {"clicks": _round(row.get("clicks"), 1), "impressions": _round(row.get("impressions"), 1),
                      "ctr": _round(row.get("ctr"), 4), "position": _round(row.get("position"), 1)}
    if len(pages) > MAX_PAGES:
        ranked = sorted(pages.items(), key=lambda kv: (-_num(kv[1].get("clicks")), kv[0]))[:MAX_PAGES]
        pages = dict(ranked)

    order = inspect_order(crawl)
    if len(order) > budget:
        notes.append("%d of %d crawled pages left uninspected (inspection budget %d)"
                     % (len(order) - budget, len(order), budget))
    by_url = {}
    for key, page in _pages(crawl).items():
        by_url.setdefault(_page_url(key, page), page)
    stopped = False
    for url in order[:budget]:
        result, err = _call(getattr(client, "inspect", None) or (lambda *a: None), site, url)
        if err:
            errors.append(err)
            if "quota" in err.lower() or "429" in err:
                notes.append("stopped inspecting after a quota error: %s" % err)
                stopped = True
                break
            continue
        result = result if isinstance(result, dict) else {}
        inspected.append({
            "url": url,
            "verdict": _str(result.get("verdict"), 80),
            "coverage_state": _str(result.get("coverage_state"), 200),
            "google_canonical": _str(result.get("google_canonical")),
            "user_canonical": _str(result.get("user_canonical")),
            "last_crawl_time": _str(result.get("last_crawl_time"), 60),
            "crawl_says": explain(by_url.get(url)),
            "clicks": _stats(pages, url).get("clicks", 0),
        })
    inspected_with_verdict = [row for row in inspected if row["verdict"]]
    if len(inspected_with_verdict) < len(inspected):
        notes.append("%d inspections returned no verdict and were left out of the findings"
                     % (len(inspected) - len(inspected_with_verdict)))
    inspected_all, inspected = inspected, inspected_with_verdict

    excluded = sorted([{"url": row["url"], "coverage_state": row["coverage_state"],
                        "google_canonical": row["google_canonical"], "crawl_says": row["crawl_says"]}
                       for row in inspected if not _is_pass(row["verdict"])], key=lambda row: row["url"])

    findings, passed = [], []
    if not inspected:
        notes.append("no URL Inspection results, so the Search Console checks did not run")
    else:
        not_indexed = [row for row in inspected
                       if not _is_pass(row["verdict"]) and _safe_indexable(by_url.get(row["url"]))]
        if not_indexed:
            findings.append({
                "code": "gsc_not_indexed",
                "urls": [row["url"] for row in not_indexed],
                "detail": ("%d inspected page(s) look indexable in the crawl but Search Console reports them as not "
                           "indexed (e.g. %s -> %s)." % (len(not_indexed), not_indexed[0]["url"],
                                                         not_indexed[0]["coverage_state"] or "no coverage state")),
                "data": {"count": len(not_indexed), "inspected": len(inspected),
                         "examples": [[row["url"], row["coverage_state"]] for row in not_indexed[:MAX_LIST]]},
            })
        elif not stopped:
            passed.append("gsc_not_indexed")
        mismatched = [row for row in inspected
                      if row["google_canonical"] and row["google_canonical"] != row["url"]
                      and (not row["user_canonical"] or row["user_canonical"] != row["google_canonical"])]
        if mismatched:
            findings.append({
                "code": "gsc_canonical_mismatch",
                "urls": [row["url"] for row in mismatched],
                "detail": ("%d inspected page(s) have a Google-selected canonical that differs from the page URL and "
                           "from its declared canonical (e.g. %s -> %s)."
                           % (len(mismatched), mismatched[0]["url"], mismatched[0]["google_canonical"])),
                "data": {"count": len(mismatched), "inspected": len(inspected),
                         "examples": [[row["url"], row["user_canonical"], row["google_canonical"]]
                                      for row in mismatched[:MAX_LIST]]},
            })
        elif not stopped:
            passed.append("gsc_canonical_mismatch")

    for finding in findings:
        finding["urls"] = finding["urls"][:MAX_URLS]

    return {"connected": True, "site": site, "period": [start.isoformat(), end.isoformat()], "pages": pages,
            "inspected": inspected_all, "excluded": excluded, "findings": findings, "passed": passed,
            "errors": errors, "notes": notes, "inspect_budget": budget}


# ---------------------------------------------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------------------------------------------
class _FakeClient:
    """Duck-typed Search Console client: no network, no clock, deterministic answers."""

    def __init__(self, sites=None, rows=None, inspections=None, sites_error=None, fail_after=None, fail_once=None,
                 fail_message="URL Inspection daily quota exceeded (429)"):
        self._sites = [{"site": "sc-domain:ex.com"}] if sites is None else sites
        self._rows = [{"keys": ["https://ex.com/"], "clicks": 10, "impressions": 100, "ctr": 0.1, "position": 3.2},
                      {"keys": ["https://ex.com/a/"], "clicks": 0, "impressions": 5, "ctr": 0.0, "position": 8.0}
                      ] if rows is None else rows
        self._inspections = inspections or {}
        self._sites_error = sites_error
        self.fail_after = fail_after
        self.fail_once = fail_once
        self.fail_message = fail_message
        self.sites_calls = 0
        self.inspect_calls = []
        self.queries = []

    def sites(self):
        self.sites_calls += 1
        if self._sites_error:
            raise ToolError(self._sites_error)
        return list(self._sites)

    def query(self, site, start, end, dims, row_limit=25000):
        self.queries.append((site, start, end, list(dims), row_limit))
        return list(self._rows)

    def inspect(self, site, url):
        self.inspect_calls.append(url)
        if self.fail_after is not None and len(self.inspect_calls) > self.fail_after:
            raise ToolError(self.fail_message)
        if self.fail_once is not None and len(self.inspect_calls) == self.fail_once:
            raise ToolError(self.fail_message)
        return dict(self._inspections.get(url, {"verdict": "PASS", "coverage_state": "Submitted and indexed",
                                                "google_canonical": url, "user_canonical": url,
                                                "last_crawl_time": "2024-04-01T00:00:00Z"}))


def _fake_inspections():
    return {
        "https://ex.com/": {"url": "https://ex.com/", "verdict": "PASS", "coverage_state": "Submitted and indexed",
                            "google_canonical": "https://ex.com/", "user_canonical": "https://ex.com/",
                            "last_crawl_time": "2024-04-01T00:00:00Z"},
        "https://ex.com/a/": {"url": "https://ex.com/a/", "verdict": "NEUTRAL",
                              "coverage_state": "Crawled - currently not indexed",
                              "google_canonical": "https://ex.com/a/", "user_canonical": "https://ex.com/a/",
                              "last_crawl_time": "2024-04-02T00:00:00Z"},
        "https://ex.com/c/": {"url": "https://ex.com/c/", "verdict": "PASS", "coverage_state": "Submitted and indexed",
                              "google_canonical": "https://ex.com/b/", "user_canonical": "",
                              "last_crawl_time": "2024-04-03T00:00:00Z"},
        "https://ex.com/d/": {"url": "https://ex.com/d/", "verdict": "PASS", "coverage_state": "Submitted and indexed",
                              "google_canonical": "https://ex.com/d/", "user_canonical": "https://ex.com/d/",
                              "last_crawl_time": "2024-04-04T00:00:00Z"},
    }


def _fake_crawl():
    def page(url, sitemap, depth, robots=None):
        return {"url": url, "chain": [[url, 200, ""]], "status": 200, "final": url, "ctype": "text/html",
                "bytes": 1200, "truncated": False, "ms": 20, "headers": {}, "error": None, "depth": depth,
                "in_sitemap": sitemap, "discovered": "link", "doc": {"robots": robots or [], "canonicals": []}}

    return {"settings": {"start_url": "https://www.ex.com/"}, "pages": {
        "https://ex.com/": page("https://ex.com/", True, 0),
        "https://ex.com/a/": page("https://ex.com/a/", True, 1),
        "https://ex.com/c/": page("https://ex.com/c/", False, 2),
        "https://ex.com/d/": page("https://ex.com/d/", False, None, robots=["noindex"]),
    }}


class _NullClient:
    """Hostile client: everything returns None, nothing raises."""

    def sites(self):
        return None

    def query(self, *args, **kwargs):
        return None

    def inspect(self, *args, **kwargs):
        return None


def selftest() -> int:
    """Offline assertions covering site picking, explanations, the join, budgets and hostile clients."""
    n = 0
    today = datetime.date(2024, 5, 20)

    assert pick_site("https://www.ex.com/x", [{"site": "https://ex.com/"}, {"site": "sc-domain:ex.com"}]) == \
        "sc-domain:ex.com", "seo.gsc_join.selftest: sc-domain property not preferred"
    n += 1
    assert pick_site("https://www.ex.com/", ["sc-domain:other.com", "https://www.ex.com/"]) == "https://www.ex.com/", \
        "seo.gsc_join.selftest: exact URL-prefix property not picked"
    n += 1
    assert pick_site("https://ex.com/", ["http://ex.com/", "https://ex.com/"]) == "https://ex.com/", \
        "seo.gsc_join.selftest: https property should beat http"
    n += 1
    assert pick_site("https://nope.test/", ["sc-domain:ex.com"]) is None, \
        "seo.gsc_join.selftest: unrelated property matched"
    n += 1
    assert pick_site("", ["sc-domain:ex.com"]) is None and pick_site("https://ex.com/", None) is None, \
        "seo.gsc_join.selftest: empty start url / missing sites not tolerated"
    n += 1
    assert pick_site("ex.com/no-scheme", [{"site": "sc-domain:ex.com"}]) == "sc-domain:ex.com", \
        "seo.gsc_join.selftest: scheme-less start url not parsed"
    n += 1

    assert explain(_fake_crawl()["pages"]["https://ex.com/"]) == "indexable", \
        "seo.gsc_join.selftest: indexable page not explained as indexable"
    n += 1
    assert explain(_fake_crawl()["pages"]["https://ex.com/d/"]) == "noindex (meta robots)", \
        "seo.gsc_join.selftest: noindex page not explained"
    n += 1
    assert explain({"url": "https://ex.com/x", "status": 200, "final": "https://ex.com/x", "depth": None,
                    "in_sitemap": True, "ctype": "text/html", "doc": {"robots": [], "canonicals": []}}) == \
        "orphan: no internal links", \
        "seo.gsc_join.selftest: orphan page not reported"
    n += 1
    assert explain({"url": "https://ex.com/x", "status": 200, "final": "https://ex.com/x", "depth": 2,
                    "in_sitemap": False, "ctype": "text/html",
                    "doc": {"robots": [], "canonicals": []}}) == "not in sitemap", \
        "seo.gsc_join.selftest: not-in-sitemap page not reported"
    n += 1
    assert explain(None) == "unknown page" and explain("junk") == "unknown page", \
        "seo.gsc_join.selftest: hostile page dict raised or returned junk"
    n += 1

    crawl = _fake_crawl()
    client = _FakeClient(inspections=_fake_inspections())
    out = join(crawl, client, today=today)
    assert out.get("connected") is True, "seo.gsc_join.selftest: join not connected"
    n += 1
    assert out.get("site") == "sc-domain:ex.com", "seo.gsc_join.selftest: site not resolved from settings"
    n += 1
    assert out.get("period") == ["2024-04-20", "2024-05-17"], "seo.gsc_join.selftest: period window wrong: %r" % (
        out.get("period"),)
    n += 1
    assert out["pages"].get("https://ex.com/", {}).get("clicks") == 10, "seo.gsc_join.selftest: analytics join lost"
    n += 1
    assert out["pages"].get("https://ex.com/a/", {}).get("impressions") == 5, \
        "seo.gsc_join.selftest: impressions not joined"
    n += 1
    assert len(out["inspected"]) == 4, "seo.gsc_join.selftest: expected 4 inspected pages, got %d" % len(
        out["inspected"])
    n += 1
    assert [row["url"] for row in out["inspected"]] == ["https://ex.com/", "https://ex.com/a/", "https://ex.com/c/",
                                                        "https://ex.com/d/"], \
        "seo.gsc_join.selftest: inspection order wrong: %r" % ([r["url"] for r in out["inspected"]],)
    n += 1
    assert out["inspected"][0].get("clicks") == 10, "seo.gsc_join.selftest: clicks not attached to inspected row"
    n += 1
    assert out["inspected"][3].get("crawl_says") == "noindex (meta robots)", \
        "seo.gsc_join.selftest: crawl-side reason missing on inspected row"
    n += 1
    assert len(out["excluded"]) == 1 and out["excluded"][0]["url"] == "https://ex.com/a/", \
        "seo.gsc_join.selftest: excluded pages wrong: %r" % (out["excluded"],)
    n += 1
    assert out["excluded"][0]["coverage_state"] == "Crawled - currently not indexed", \
        "seo.gsc_join.selftest: coverage state lost in excluded list"
    n += 1
    codes = [f["code"] for f in out["findings"]]
    assert codes == ["gsc_not_indexed", "gsc_canonical_mismatch"], \
        "seo.gsc_join.selftest: findings wrong: %r" % (codes,)
    n += 1
    assert out["findings"][0]["urls"] == ["https://ex.com/a/"], \
        "seo.gsc_join.selftest: gsc_not_indexed urls wrong"
    n += 1
    assert out["findings"][0]["data"]["examples"] == [["https://ex.com/a/", "Crawled - currently not indexed"]], \
        "seo.gsc_join.selftest: gsc_not_indexed examples wrong"
    n += 1
    assert out["findings"][1]["urls"] == ["https://ex.com/c/"], \
        "seo.gsc_join.selftest: gsc_canonical_mismatch urls wrong"
    n += 1
    assert out["findings"][1]["data"]["examples"] == [["https://ex.com/c/", "", "https://ex.com/b/"]], \
        "seo.gsc_join.selftest: canonical mismatch example wrong: %r" % (out["findings"][1]["data"]["examples"],)
    n += 1
    assert out["passed"] == [] and out["errors"] == [], "seo.gsc_join.selftest: unexpected passed/errors: %r %r" % (
        out["passed"], out["errors"])
    n += 1
    assert isinstance(out["inspect_budget"], int) and out["inspect_budget"] == 50, \
        "seo.gsc_join.selftest: inspect_budget not reported"
    n += 1
    assert all(isinstance(f["detail"], str) and f["detail"].endswith(".") for f in out["findings"]), \
        "seo.gsc_join.selftest: finding detail is not a plain sentence"
    n += 1

    tight = join(crawl, _FakeClient(inspections=_fake_inspections()), today=today, max_inspect=1)
    assert [row["url"] for row in tight["inspected"]] == ["https://ex.com/"], \
        "seo.gsc_join.selftest: max_inspect=1 did not stop at the first sitemap page: %r" % (
            [r["url"] for r in tight["inspected"]],)
    n += 1
    assert tight["notes"] and tight["inspect_budget"] == 1, "seo.gsc_join.selftest: budget note missing"
    n += 1
    zero = join(crawl, _FakeClient(inspections=_fake_inspections()), today=today, max_inspect=-7)
    assert zero["inspect_budget"] == 0 and zero["inspected"] == [] and zero["notes"], \
        "seo.gsc_join.selftest: negative budget not clamped to zero"
    n += 1
    big = join(crawl, _FakeClient(inspections=_fake_inspections()), today=today, max_inspect=99999)
    assert big["inspect_budget"] == MAX_INSPECT, "seo.gsc_join.selftest: huge budget not clamped to 2000"
    n += 1

    given = _FakeClient(inspections=_fake_inspections())
    with_site = join(crawl, given, site="sc-domain:ex.com", today=today)
    assert with_site["site"] == "sc-domain:ex.com" and given.sites_calls == 0, \
        "seo.gsc_join.selftest: explicit site still listed properties"
    n += 1

    closed = join(crawl, _FakeClient(sites_error="HttpError 403: insufficient permission ya29.SECRET-TOKEN"), today=today)
    assert closed.get("connected") is False and "403" in closed.get("note", ""), \
        "seo.gsc_join.selftest: 403 on sites() not reported as not connected"
    n += 1
    assert "ya29" not in closed.get("note", "") and len(closed) == 2, \
        "seo.gsc_join.selftest: note not scrubbed or extra keys returned"
    n += 1
    quota = join(crawl, _FakeClient(inspections=_fake_inspections(), fail_after=1), today=today)
    assert [row["url"] for row in quota["inspected"]] == ["https://ex.com/"], \
        "seo.gsc_join.selftest: quota error did not stop inspection"
    n += 1
    assert quota["connected"] is True and quota["errors"] and any("quota" in note for note in quota["notes"]), \
        "seo.gsc_join.selftest: quota error not noted"
    n += 1
    broken = join(crawl, _FakeClient(inspections=_fake_inspections(), fail_once=2,
                                     fail_message="Internal error on inspect"), today=today)
    assert broken["connected"] is True and len(broken["inspected"]) == 3 and broken["errors"], \
        "seo.gsc_join.selftest: non-quota inspect error did not continue past the failure"
    n += 1
    assert join(crawl, None)["connected"] is False and "Search Console" in join(crawl, None)["note"], \
        "seo.gsc_join.selftest: client None not reported"
    n += 1
    no_prop = join(crawl, _FakeClient(sites=[{"site": "sc-domain:other.test"}]), today=today)
    assert no_prop["connected"] is False and "ex.com" in no_prop["note"], \
        "seo.gsc_join.selftest: missing property not reported with the host"
    n += 1
    never = join(crawl, _FakeClient(sites_error="backend exploded"), today=today)
    assert never["connected"] is False, "seo.gsc_join.selftest: unresolved site should be not connected"
    n += 1

    hostile = join({"pages": "garbage", "settings": None}, _NullClient(), site="sc-domain:ex.com", today=today)
    assert hostile["connected"] is True and hostile["inspected"] == [] and hostile["pages"] == {}, \
        "seo.gsc_join.selftest: hostile crawl data was not degraded safely"
    n += 1
    hostile2 = join(crawl, _NullClient(), site="sc-domain:ex.com", today=today, days=0, max_inspect="junk")
    assert hostile2["connected"] is True and len(hostile2["inspected"]) == 4, \
        "seo.gsc_join.selftest: None-returning client broke the join"
    n += 1
    assert all(row["verdict"] == "" and row["google_canonical"] == "" for row in hostile2["inspected"]) and \
        hostile2["findings"] == [] and any("no verdict" in x for x in hostile2["notes"]), \
        "seo.gsc_join.selftest: empty inspections never become findings (review 2026-11-10)"
    n += 1
    assert hostile2["period"] == ["2024-05-17", "2024-05-17"], \
        "seo.gsc_join.selftest: days=0 not clamped to a 1 day window: %r" % (hostile2["period"],)
    n += 1
    assert join(None, _NullClient(), site="sc-domain:ex.com")["connected"] is True, \
        "seo.gsc_join.selftest: crawl=None raised"
    n += 1
    assert explain({"doc": "not-a-dict", "status": 404, "url": "https://ex.com/x"}) == "HTTP 404", \
        "seo.gsc_join.selftest: a non-dict doc is tolerated by explain"
    n += 1

    import json
    assert json.dumps(out, sort_keys=True), "seo.gsc_join.selftest: join result is not JSON-serialisable"
    n += 1
    assert json.dumps(hostile2, sort_keys=True), "seo.gsc_join.selftest: degraded result is not JSON-serialisable"
    n += 1
    return n
