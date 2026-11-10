"""Ad landing-page guard for the AdRails site audit (adsops/seo).

AdRails is an ads tool, so its own site audit gets its own angle: take the final URLs that the live ad
campaigns point at and check them as landing pages -- do they answer 200, are they deliberately noindex,
do they carry a canonical that sends signals elsewhere, are they near-duplicates of an organic page (so the
two compete for the same queries), and are indexable pages wasting link equity on them.

Read-only and pure: this module never fetches, never writes, never reads the clock and touches no disk at
import time. The only disk access anywhere is the default state-file reader used by `final_urls_from_config`,
and that is injected (`load=`) in every test. Everything returned is JSON-serialisable (dicts/lists/str/
int/float/bool/None). Nothing here raises on hostile input: malformed state shapes, configs, crawl dicts and
edge rows all degrade to defaults. Every `run_*`-style function returns (findings, passed) where `passed`
names the checks that ran and found nothing.

Findings follow the shared contract: {"code", "urls", "detail", "data"} with a non-empty URL list, one
plain-English sentence and a small JSON-safe data dict (lists capped at 20 items).
"""
import json
import os
import urllib.parse

from adsops.errors import ToolError
from adsops.seo.extract import normalize
from adsops.seo.checks_content import is_indexable, shingles, jaccard
from adsops.seo.checks_links import resolve

MAX_LIST = 20
MAX_URLS = 500
MAX_CONFIG_URLS = 5000
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_DUP_PAGES = 1500
DEFAULT_DUP_THRESHOLD = 0.8
HTML_CTYPES = ("text/html", "application/xhtml+xml")
CHECKS = ("ad_lp_non_200", "ad_lp_noindex", "ad_lp_canonical_elsewhere", "ad_lp_duplicates_organic",
          "ad_lp_equity_sink")


# ---------------------------------------------------------------------------------------------------------------
# small, total helpers (never raise, never touch the outside world)
# ---------------------------------------------------------------------------------------------------------------
def _text(value, limit=500):
    """Whitespace-collapsed string of `value` ('' for None/garbage), truncated to `limit` characters."""
    if value is None:
        return ""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    s = " ".join(s.split())
    return s[:limit] if limit else s


def _list(value):
    """`value` if it is a list/tuple, else an empty list."""
    return value if isinstance(value, (list, tuple)) else []


def _int(value, default=0):
    """`value` if it is a real int (bools excluded), else `default`."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _float(value, default):
    """`value` as a finite float (bools and NaN/inf excluded), else `default`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    out = float(value)
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def _pages(crawl):
    """-> {url: page} from the crawl result, {} when missing or of the wrong type."""
    pages = crawl.get("pages") if isinstance(crawl, dict) else None
    return pages if isinstance(pages, dict) else {}


def _url(page):
    """Requested URL of `page` ('' when absent)."""
    value = page.get("url") if isinstance(page, dict) else None
    return value if isinstance(value, str) else ""


def _doc(page):
    """PageDocument of `page`, or None when absent / not a dict."""
    doc = page.get("doc") if isinstance(page, dict) else None
    return doc if isinstance(doc, dict) else None


def _doc_text(page):
    """Visible text recorded in the page document ('' when absent)."""
    doc = _doc(page)
    value = doc.get("text") if doc else None
    return value if isinstance(value, str) else ""


def _ctype(page):
    """Bare, lowercased content type ('text/html; charset=utf-8' -> 'text/html')."""
    value = page.get("ctype") if isinstance(page, dict) else ""
    return _text(value, 200).split(";")[0].strip().lower()


def _canonicals(page):
    """Non-empty canonical URLs of `page`, in document order."""
    doc = _doc(page)
    out = []
    for value in _list(doc.get("canonicals") if doc else None):
        cleaned = _text(value)
        if cleaned:
            out.append(cleaned)
    return out


def _uniq(items, cap=MAX_URLS):
    """-> distinct non-empty strings of `items`, in order, capped at `cap`."""
    out, seen = [], set()
    for item in _list(items):
        if not isinstance(item, str) or not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= cap:
            break
    return out


def _strip_query(url):
    """`url` without its query string or fragment ('' for garbage)."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return url.split("?")[0].split("#")[0]


def _path(url):
    """Path part of `url` ('/' when it cannot be parsed)."""
    try:
        return urllib.parse.urlsplit(url).path or "/"
    except ValueError:
        return "/"


def _clean_url(raw):
    """Normalised absolute http(s) URL of `raw` ('' when it is not a usable URL string)."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        return normalize(raw.strip())
    except (ToolError, ValueError, TypeError):
        return ""


def _noindex(page):
    """True when a meta robots value or the X-Robots-Tag header of `page` carries the noindex token."""
    doc = _doc(page)
    for value in _list(doc.get("robots") if doc else None):
        if isinstance(value, str) and "noindex" in value.lower():
            return True
    headers = page.get("headers") if isinstance(page, dict) else None
    tag = headers.get("x-robots-tag") if isinstance(headers, dict) else None
    return isinstance(tag, str) and "noindex" in tag.lower()


def _is_html_200(page):
    """True when `page` is a 200 HTML page with a parsed document."""
    return _int(page.get("status"), 0) == 200 and _ctype(page) in HTML_CTYPES and _doc(page) is not None


def _edges(crawl):
    """-> the well-formed [src, dst, ...] rows of crawl["edges"]; anything else is skipped."""
    raw = crawl.get("edges") if isinstance(crawl, dict) else None
    rows = []
    for row in _list(raw):
        if isinstance(row, (list, tuple)) and len(row) >= 2 and isinstance(row[0], str) and isinstance(row[1], str):
            rows.append(row)
    return rows


def _links_to(crawl, dst, page_url):
    """True when the link target `dst` lands on `page_url` (utm-tagged and redirected links included)."""
    if not isinstance(dst, str) or not page_url:
        return False
    if _strip_query(dst) == page_url:
        return True
    try:
        target = resolve(crawl, dst)
    except (ToolError, ValueError, TypeError):
        return False
    return target == page_url or _strip_query(target) == page_url


# ---------------------------------------------------------------------------------------------------------------
# 1. desired-state -> ad final URLs
# ---------------------------------------------------------------------------------------------------------------
def final_urls_from_state(state):
    """-> every http(s) final URL of one parsed desired-state dict, normalised, unique, in order (cap 500).

    Handles the Google Ads shape (``ad_groups`` is a dict of name -> ``{"ad_final_urls": [urls]}``) and the
    OpenAI Ads shape (``ad_groups`` is a list of ``{"ads": [{"target_url": url}]}``, ``platform == "openai_ads"``).
    The container type decides, so a state that carries the "wrong" shape still yields its URLs. Any other
    shape -- wrong types, non-strings, non-URL strings -- is skipped, never raised on.
    """
    out, seen = [], set()

    def add(raw):
        if len(out) >= MAX_URLS:
            return
        url = _clean_url(raw)
        if url and url not in seen:
            seen.add(url)
            out.append(url)

    if not isinstance(state, dict):
        return out
    groups = state.get("ad_groups")
    if isinstance(groups, dict):
        for _name, group in groups.items():
            if not isinstance(group, dict):
                continue
            for raw in _list(group.get("ad_final_urls")):
                add(raw)
            for ad in _list(group.get("ads")):
                if isinstance(ad, dict):
                    add(ad.get("target_url"))
    elif isinstance(groups, (list, tuple)):
        for group in groups:
            if not isinstance(group, dict):
                continue
            for raw in _list(group.get("ad_final_urls")):
                add(raw)
            for ad in _list(group.get("ads")):
                if isinstance(ad, dict):
                    add(ad.get("target_url"))
    return out


def _load_json_file(path, limit=MAX_STATE_BYTES):
    """Read one JSON state file from `path`; raises on a missing, oversized or malformed file."""
    with open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ToolError("state file is larger than %d bytes" % limit)
    return json.loads(raw.decode("utf-8", "replace"))


def _campaign_key(account, campaign, index):
    """Stable label for a campaign row: its own key, else the account key plus the row index."""
    for candidate in (campaign.get("key"), campaign.get("campaign_key"), campaign.get("state_key")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    prefix = account.get("key") if isinstance(account.get("key"), str) and account.get("key").strip() else "account"
    return "%s#%d" % (prefix, index)


def final_urls_from_config(cfg, load=None):
    """-> {"urls", "sources", "errors"} for every campaign state file named in a deployment config.

    Each unreadable state file (missing, oversized, malformed, non-JSON) becomes one errors entry naming the
    campaign key only -- the exception text never leaks. `load` is injected (path -> parsed dict); the default
    reads JSON from disk with a 2 MB ceiling.
    """
    if cfg is not None and not isinstance(cfg, dict):
        raise ToolError("adguard.final_urls_from_config: cfg must be a dict")
    cfg = cfg if isinstance(cfg, dict) else {}
    reader = load if callable(load) else _load_json_file
    urls, seen, sources, errors = [], set(), [], []
    for account in _list(cfg.get("accounts")):
        if not isinstance(account, dict):
            continue
        rows = _list(account.get("campaigns"))
        for index, campaign in enumerate(rows):
            if not isinstance(campaign, dict):
                continue
            key = _campaign_key(account, campaign, index)
            raw_path = campaign.get("state_file")
            path = os.path.expanduser(raw_path) if isinstance(raw_path, str) and raw_path.strip() else ""
            state = None
            if path:
                try:
                    state = reader(path)
                except Exception:
                    state = None
            if not isinstance(state, dict):
                errors.append("state file for %s unreadable" % key)
                continue
            found = final_urls_from_state(state)
            sources.append([key, len(found)])
            for url in found:
                if url not in seen and len(urls) < MAX_CONFIG_URLS:
                    seen.add(url)
                    urls.append(url)
    return {"urls": urls, "sources": sources, "errors": errors}


# ---------------------------------------------------------------------------------------------------------------
# 2. crawl + ad URLs -> findings
# ---------------------------------------------------------------------------------------------------------------
def _as_url_list(final_urls):
    """-> the final URLs given to run()/summary(): a list, or the "urls" of a config result, else ToolError."""
    if isinstance(final_urls, dict):
        final_urls = final_urls.get("urls")
    if final_urls is None:
        return []
    if not isinstance(final_urls, (list, tuple)):
        raise ToolError("adguard: final_urls must be a list of URLs")
    return list(final_urls)


def _resolve_ads(crawl, final_urls):
    """-> (ad URLs in order, [(ad_url, page)] for the crawled ones, [ad URLs never crawled]).

    An ad URL that carries a query string falls back to the page crawled for the bare URL, so utm-tagged ad
    targets still match the canonical crawl entry.
    """
    pages = _pages(crawl)
    ad_urls, seen, rows, missing = [], set(), [], []
    for raw in final_urls:
        url = _clean_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        if len(ad_urls) >= MAX_URLS:
            break
        ad_urls.append(url)
        page = pages.get(url)
        if not isinstance(page, dict):
            page = pages.get(_strip_query(url))
        if not isinstance(page, dict):
            missing.append(url)
            continue
        rows.append((url, page))
    return ad_urls, rows, missing


def _sink_reason(page):
    """Why a non-indexable ad page loses equity ('noindex', 'canonical -> X'), or '' when it is not a sink."""
    if _noindex(page):
        return "noindex"
    url = _url(page)
    for canonical in _canonicals(page):
        if canonical != url:
            return "canonical -> %s" % canonical
    return ""


def run(crawl, final_urls, dup_threshold=DEFAULT_DUP_THRESHOLD):
    """-> (findings, passed) for the five ad landing-page checks over one crawl result and its ad URLs."""
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("adguard.run: crawl must be a dict")
    crawl = crawl if isinstance(crawl, dict) else {}
    ads = _as_url_list(final_urls)
    thr = _float(dup_threshold, DEFAULT_DUP_THRESHOLD)
    ad_urls, rows, _missing = _resolve_ads(crawl, ads)
    findings, passed = [], []
    if not rows:
        return findings, passed

    def emit(code, urls, detail, data=None):
        """Append one finding (when `urls` is non-empty); returns True when something was reported."""
        clean = _uniq(urls)
        if not clean:
            return False
        detail = _text(detail, 600)
        if not detail:
            detail = "%d URLs affected." % len(clean)
        elif not detail.endswith("."):
            detail += "."
        findings.append({"code": code, "urls": clean, "detail": detail, "data": data if isinstance(data, dict) else {}})
        return True

    def settle(code, fired):
        """Record `code` in `passed` when it ran and stayed silent."""
        if not fired:
            passed.append(code)

    # --- ad_lp_non_200: the ad click must land on a 200 at the ad URL ---------------------------------------
    bad = []
    for url, page in rows:
        status = _int(page.get("status"), 0)
        page_url = _url(page)
        final = page.get("final") if isinstance(page.get("final"), str) else ""
        if status != 200 or (page_url and final and final != page_url):
            bad.append([url, status, final])
    settle("ad_lp_non_200", emit(
        "ad_lp_non_200", [row[0] for row in bad],
        "%d of %d ad landing pages do not answer 200 at the ad URL (e.g. %s returns %d, final %s)"
        % (len(bad), len(rows), bad[0][0] if bad else "(none)", bad[0][1] if bad else 0,
           bad[0][2] if bad else "(none)"),
        {"count": len(bad), "crawled": len(rows), "examples": bad[:MAX_LIST]}))

    # --- ad_lp_noindex: fine for the ads, only needs to be deliberate ---------------------------------------
    noinx = [url for url, page in rows if _is_html_200(page) and _noindex(page)]
    settle("ad_lp_noindex", emit(
        "ad_lp_noindex", noinx,
        "%d ad landing pages are noindex: that is fine for the ads themselves (paid clicks still land), so it "
        "only needs to be deliberate rather than accidental" % len(noinx),
        {"count": len(noinx), "examples": noinx[:MAX_LIST]}))

    # --- ad_lp_canonical_elsewhere: the ad URL is never the indexed page -------------------------------------
    canon_bad = []
    for url, page in rows:
        canon = _canonicals(page)
        if canon and canon[0] != url and canon[0] != _strip_query(url):
            canon_bad.append([url, canon[0]])
    settle("ad_lp_canonical_elsewhere", emit(
        "ad_lp_canonical_elsewhere", [row[0] for row in canon_bad],
        "%d ad landing pages send a canonical somewhere else (e.g. %s -> %s), so the ad URL is never the indexed "
        "page" % (len(canon_bad), canon_bad[0][0] if canon_bad else "(none)",
                  canon_bad[0][1] if canon_bad else "(none)"),
        {"count": len(canon_bad), "pairs": canon_bad[:MAX_LIST]}))

    # --- ad_lp_duplicates_organic: the ad page competes with an organic page for the same queries -----------
    ad_set = set(ad_urls) | set(_strip_query(url) for url in ad_urls)
    pool = []
    for _key, page in _pages(crawl).items():
        purl = _url(page)
        if not purl or purl in ad_set or not is_indexable(page):
            continue
        mine = shingles(_doc_text(page))
        if mine:
            pool.append((purl, mine))
        if len(pool) >= MAX_DUP_PAGES:
            break
    dup_rows = []
    for url, page in rows:
        if not is_indexable(page):
            continue
        mine = shingles(_doc_text(page))
        if not mine:
            continue
        best = None
        for purl, other in pool:
            if purl == _url(page):
                continue
            score = jaccard(mine, other)
            if score >= thr and (best is None or score > best[1]):
                best = (purl, score)
        if best is not None:
            dup_rows.append([url, best[0], round(best[1], 2)])
    settle("ad_lp_duplicates_organic", emit(
        "ad_lp_duplicates_organic", [row[0] for row in dup_rows],
        "%d ad landing pages are near-duplicates of an organic page (e.g. %s and %s are %d%% identical), so both "
        "compete for the same queries" % (len(dup_rows), dup_rows[0][0] if dup_rows else "(none)",
                                          dup_rows[0][1] if dup_rows else "(none)",
                                          int(round(dup_rows[0][2] * 100)) if dup_rows else 0),
        {"count": len(dup_rows), "threshold": round(thr, 2), "pairs": dup_rows[:MAX_LIST]}))

    # --- ad_lp_equity_sink: indexable pages linking at an ad-only page waste their equity -------------------
    idx_src = set()
    for _key, page in _pages(crawl).items():
        purl = _url(page)
        if purl and is_indexable(page):
            idx_src.add(purl)
    sink_links, sink_reasons, source_urls, seen_pages = [], {}, [], set()
    crawl_edges = _edges(crawl)
    for url, page in rows:
        purl = _url(page)
        reason = _sink_reason(page)
        if not purl or not reason or is_indexable(page) or purl in seen_pages:
            continue
        seen_pages.add(purl)
        links, sources, per_source = 0, [], set()
        for edge in crawl_edges:
            src = edge[0]
            if src not in idx_src or not _links_to(crawl, edge[1], purl):
                continue
            links += 1
            if src not in per_source:
                per_source.add(src)
                sources.append(src)
        if links:
            sink_links.append([url, links, len(sources)])
            sink_reasons[url] = reason
            source_urls.extend(sources)
    first = sink_links[0] if sink_links else [None, 0, 0]
    total_links = sum(row[1] for row in sink_links)
    total_sources = len(set(source_urls))                     # a page linking to two sinks counts once
    settle("ad_lp_equity_sink", emit(
        "ad_lp_equity_sink", source_urls,
        "%d internal links on %d indexable pages point at %d ad-only page%s (e.g. %s, %s), so their equity is lost"
        % (total_links, total_sources, len(sink_links), "" if len(sink_links) == 1 else "s",
           _path(first[0]) if first[0] else "(none)", sink_reasons.get(first[0]) or "not indexable"),
        {"count": len(sink_links), "links": total_links, "targets": sink_links[:MAX_LIST]}))

    return findings, passed


# ---------------------------------------------------------------------------------------------------------------
# 3. one-line-per-ad summary for the report
# ---------------------------------------------------------------------------------------------------------------
def _why(page):
    """Short human verdict for one ad landing page: indexable / noindex / canonical -> X / HTTP n / redirects."""
    status = _int(page.get("status"), 0)
    if status != 200:
        return "HTTP %d" % status
    page_url = _url(page)
    final = page.get("final") if isinstance(page.get("final"), str) else ""
    if page_url and final != page_url:
        return "redirects -> %s" % (final or "(unknown)")
    if _ctype(page) not in HTML_CTYPES or _doc(page) is None:
        return "not HTML"
    if _noindex(page):
        return "noindex"
    for canonical in _canonicals(page):
        if canonical != page_url:
            return "canonical -> %s" % canonical
    return "indexable"


def summary(crawl, final_urls):
    """-> {"ad_urls", "crawled", "not_crawled", "rows"} with rows [ad_url, status, indexable, why]."""
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("adguard.summary: crawl must be a dict")
    crawl = crawl if isinstance(crawl, dict) else {}
    ad_urls, rows, missing = _resolve_ads(crawl, _as_url_list(final_urls))
    out = []
    for url, page in rows:
        out.append([url, _int(page.get("status"), 0), bool(is_indexable(page)), _why(page)])
    return {"ad_urls": len(ad_urls), "crawled": len(rows), "not_crawled": missing, "rows": out}


# ---------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    """Offline assertions over fake state files, configs and crawls; returns the number of checks performed."""
    n = 0

    def chk(cond, msg):
        nonlocal n
        n += 1
        assert cond, "seo.adguard.selftest: " + msg

    # --- final_urls_from_state: both platforms, hostile shapes -----------------------------------------------
    gstate = {"ad_groups": {"pasco": {"ad_final_urls": ["https://ex.com/owners/"]}, "bad": 7}}
    chk(final_urls_from_state(gstate) == ["https://ex.com/owners/"], "google state yields the one final URL")
    ostate = {"platform": "openai_ads",
              "ad_groups": [{"ads": [{"target_url": "https://ex.com/lp/?utm_source=chatgpt"}]}]}
    chk(final_urls_from_state(ostate) == ["https://ex.com/lp/?utm_source=chatgpt"],
        "openai state yields the card target URL with its query")
    chk(final_urls_from_state(None) == [] and final_urls_from_state("nope") == [], "non-dict states yield nothing")
    junk = {"ad_groups": {"g": {"ad_final_urls": [None, 7, "", "http://ex.com/a/", "http://ex.com/a/#frag",
                                                  "mailto:a@ex.com", " https://ex.com/b/ "]}}}
    chk(final_urls_from_state(junk) == ["http://ex.com/a/", "https://ex.com/b/"],
        "non-URL entries are skipped, fragments dropped, duplicates collapsed")
    chk(final_urls_from_state({"ad_groups": {"g": "nope", "h": {"ad_final_urls": "nope"}}}) == [],
        "wrong inner types yield nothing")
    many = {"ad_groups": {"g": {"ad_final_urls": ["https://ex.com/p%d/" % i for i in range(600)]}}}
    chk(len(final_urls_from_state(many)) == 500, "state URL list is capped at 500")
    upper = {"ad_groups": {"g": {"ad_final_urls": ["HTTPS://EX.com/Owners"]}}}
    chk(final_urls_from_state(upper) == ["https://ex.com/Owners"], "URLs come back normalised")

    # --- final_urls_from_config with an injected loader ------------------------------------------------------
    p1 = os.path.expanduser("~/adsops-adguard-selftest-1.json")
    p2 = os.path.expanduser("~/adsops-adguard-selftest-2.json")
    p3 = os.path.expanduser("~/adsops-adguard-selftest-3.json")
    cfg = {"accounts": [
        {"key": "acme", "campaigns": [
            {"key": "acme_search", "state_file": "~/adsops-adguard-selftest-1.json"},
            {"key": "acme_chatgpt", "state_file": "~/adsops-adguard-selftest-2.json"},
            {"key": "acme_broken", "state_file": "~/adsops-adguard-selftest-3.json"},
            {"key": "acme_no_file"},
            "not a campaign"]},
        {"campaigns": [{"key": "second", "state_file": "~/adsops-adguard-selftest-1.json"}]},
        "not an account"]}
    files = {p1: gstate, p2: ostate}

    def fake_load(path):
        if path not in files:
            raise ValueError("boom: /secret/path.json")
        return files[path]

    got = final_urls_from_config(cfg, load=fake_load)
    chk(got["urls"] == ["https://ex.com/owners/", "https://ex.com/lp/?utm_source=chatgpt"],
        "config union keeps order and collapses the repeated campaign")
    chk(got["sources"] == [["acme_search", 1], ["acme_chatgpt", 1], ["second", 1]],
        "sources lists one row per readable campaign")
    chk(got["errors"] == ["state file for acme_broken unreadable", "state file for acme_no_file unreadable"],
        "unreadable and missing state files are reported by campaign key only")
    chk(all("boom" not in err and "secret" not in err for err in got["errors"]), "error text never leaks the exception")
    chk(final_urls_from_config({"accounts": []}, load=fake_load) == {"urls": [], "sources": [], "errors": []},
        "an empty config yields empty lists")
    chk(final_urls_from_config({"accounts": "nope"}, load=fake_load)["urls"] == [], "wrong account container skipped")
    raised = False
    try:
        final_urls_from_config(["not", "a", "dict"])
    except ToolError:
        raised = True
    chk(raised, "a non-dict config raises ToolError")
    missing_raised = False
    try:
        _load_json_file(os.path.expanduser("~/adsops-adguard-selftest-missing-9385.json"))
    except Exception:
        missing_raised = True
    chk(missing_raised, "the default state-file loader raises on a missing file")

    # --- fake crawl: one noindex ad page, one near-duplicate ad page, a 404, a foreign canonical ------------
    def doc_of(**over):
        doc = {"doctype": True, "lang": "en", "titles": ["T"], "descriptions": ["D"], "robots": [], "canonicals": [],
               "h1": ["H"], "h2_count": 1, "hreflang": [], "links": [], "jsonld_types": [], "jsonld_errors": 0,
               "text": "text here", "words": 2, "preload_images": [], "images": [], "video_posters": [],
               "viewport": True, "og_image": True, "meta_refresh": None}
        doc.update(over)
        return doc

    def page_of(url, status=200, final=None, ctype="text/html", doc=None, headers=None):
        return {"url": url, "chain": [[url, status, None]], "status": status, "final": url if final is None else final,
                "ctype": ctype, "bytes": 1000, "truncated": False, "ms": 5, "headers": dict(headers or {}),
                "error": None, "depth": 1, "in_sitemap": True, "discovered": "link", "doc": doc}

    home = "https://ex.com/"
    owners = "https://ex.com/owners/"
    lp = "https://ex.com/lp/"
    pricing = "https://ex.com/pricing/"
    about = "https://ex.com/about/"
    special = "https://ex.com/special/"
    gone = "https://ex.com/gone/"
    words = ["w%d" % i for i in range(300)]
    base_text = " ".join(words)
    near_words = list(words)
    near_words[150] = "differentword"
    near_text = " ".join(near_words)
    pages = {
        home: page_of(home, doc=doc_of(text="home page about the property company", titles=["Home"])),
        owners: page_of(owners, doc=doc_of(robots=["noindex, follow"], text="owners page for landlords")),
        lp: page_of(lp, doc=doc_of(text=base_text)),
        pricing: page_of(pricing, doc=doc_of(text=near_text)),
        about: page_of(about, doc=doc_of(text="about this property company and its team")),
        special: page_of(special, doc=doc_of(canonicals=[pricing], text="special offer landing page")),
        gone: page_of(gone, status=404, doc=None),
    }
    edges = [
        [home, owners + "?utm_source=google&utm_medium=cpc", "Owners", "", False],
        [home, owners + "?utm_source=google&utm_campaign=spring", "Landlords", "", True],
        [about, owners + "?utm_source=newsletter", "Owners", "", False],
        [owners, owners + "?utm_source=self", "Owners", "", False],
        [home, pricing, "Pricing", "", True],
    ]
    crawl = {"settings": {"start_url": home}, "pages": pages, "edges": edges, "robots": {}, "sitemap": {},
             "external_links": {}, "probes": {}, "skipped": {}, "notes": []}
    ads = [owners, lp + "?utm_source=chatgpt", gone + "?utm_source=google&utm_medium=cpc", special,
           "https://ex.com/never-seen/"]
    findings, passed = run(crawl, ads)
    found = {f["code"]: f for f in findings}
    chk(sorted(found) == ["ad_lp_canonical_elsewhere", "ad_lp_duplicates_organic", "ad_lp_equity_sink",
                          "ad_lp_noindex", "ad_lp_non_200"], "every guard check fires on the fixture crawl")
    chk(passed == [], "nothing is reported as passing when every check fires")
    chk(all(f["urls"] for f in findings), "no finding has an empty URL list")
    chk(all(set(f) == {"code", "urls", "detail", "data"} for f in findings), "findings follow the shared contract")
    chk(all(f["detail"].endswith(".") for f in findings), "every detail is one full sentence")
    chk(len({f["code"] for f in findings}) == len(findings), "one finding per code")
    try:
        blob = json.dumps(findings)
    except (TypeError, ValueError):
        blob = ""
    chk(bool(blob), "findings are JSON-serialisable")

    # --- the individual verdicts -----------------------------------------------------------------------------
    non200 = found.get("ad_lp_non_200", {})
    chk(non200.get("urls") == [gone + "?utm_source=google&utm_medium=cpc"], "the 404 ad URL is the only non-200")
    chk(non200.get("data", {}).get("examples", [[None, None, None]])[0][:2] ==
        [gone + "?utm_source=google&utm_medium=cpc", 404], "the 404 example carries the status")
    noindex = found.get("ad_lp_noindex", {})
    chk(noindex.get("urls") == [owners], "the noindex ad URL is reported")
    chk("deliberate" in noindex.get("detail", ""), "the noindex detail says it only needs to be deliberate")
    chk("fine for the ads themselves" in noindex.get("detail", ""), "the noindex detail says it is fine for the ads")
    canon = found.get("ad_lp_canonical_elsewhere", {})
    chk(canon.get("urls") == [special], "the canonical-elsewhere ad URL is reported")
    chk(canon.get("data", {}).get("pairs") == [[special, pricing]], "the canonical pair is reported")
    dups = found.get("ad_lp_duplicates_organic", {})
    expected_j = round(jaccard(shingles(base_text), shingles(near_text)), 2)
    chk(0.9 <= expected_j < 0.99, "the fixture pair is ~97% identical")
    chk(dups.get("urls") == [lp + "?utm_source=chatgpt"], "the near-duplicate ad URL is reported with its query")
    chk(dups.get("data", {}).get("pairs") == [[lp + "?utm_source=chatgpt", pricing, expected_j]],
        "the duplicate pair carries the similarity score")
    sink = found.get("ad_lp_equity_sink", {})
    chk(sink.get("data", {}).get("targets") == [[owners, 3, 2]], "three utm links from two indexable pages")
    chk(sorted(sink.get("urls", [])) == sorted([home, about]), "the linking pages are the finding sources")
    chk(sink.get("data", {}).get("links") == 3, "the sink total counts the utm links")
    chk("equity is lost" in sink.get("detail", ""), "the sink detail explains the lost equity")

    # --- summary ------------------------------------------------------------------------------------------
    summ = summary(crawl, ads)
    chk(summ["ad_urls"] == 5 and summ["crawled"] == 4, "summary counts ad URLs and crawled ones")
    chk(summ["not_crawled"] == ["https://ex.com/never-seen/"], "summary lists the uncrawled ad URL")
    rows = {row[0]: row for row in summ["rows"]}
    chk(rows[owners][3] == "noindex", "summary says why /owners/ is not indexable")
    chk(rows[special][3] == "canonical -> " + pricing, "summary names the canonical target")
    chk(rows[gone + "?utm_source=google&utm_medium=cpc"][3] == "HTTP 404", "summary reports the HTTP status")
    chk(rows[lp + "?utm_source=chatgpt"][2] is True and rows[lp + "?utm_source=chatgpt"][3] == "indexable",
        "the utm ad URL that resolves to an indexable page reads indexable")
    chk(rows[gone + "?utm_source=google&utm_medium=cpc"][2] is False, "the 404 page is not indexable")

    # --- redirects, thresholds, clean crawls ----------------------------------------------------------------
    promo = "https://ex.com/promo/"
    redir = {"pages": {promo: page_of(promo, final=lp, doc=doc_of(text="promo page"))}, "edges": []}
    rfound = {f["code"]: f for f in run(redir, [promo])[0]}
    chk(rfound.get("ad_lp_non_200", {}).get("data", {}).get("examples") == [[promo, 200, lp]],
        "a redirected ad URL counts as non-200 with its final target")
    chk(summary(redir, [promo])["rows"][0][3] == "redirects -> " + lp, "summary explains the redirect")
    strict_findings, strict_passed = run(crawl, ads, dup_threshold=0.99)
    chk("ad_lp_duplicates_organic" not in [f["code"] for f in strict_findings],
        "a 0.99 threshold suppresses the 97% pair")
    chk("ad_lp_duplicates_organic" in strict_passed, "the suppressed check is reported as passing")
    clean = {"pages": {pricing: page_of(pricing, doc=doc_of(text=base_text))}, "edges": []}
    chk(run(clean, [pricing]) == ([], list(CHECKS)), "a clean ad URL passes every check")
    chk(run(crawl, got)[0] and "ad_lp_noindex" in [f["code"] for f in run(crawl, got)[0]],
        "run accepts the final_urls_from_config dict")

    # --- empty and hostile input ----------------------------------------------------------------------------
    chk(run({}, []) == ([], []), "empty crawl and no ad URLs give no findings")
    chk(run(crawl, []) == ([], []), "no ad URLs means no findings even with a full crawl")
    chk(run(None, None) == ([], []), "None inputs are tolerated")
    chk(summary({}, []) == {"ad_urls": 0, "crawled": 0, "not_crawled": [], "rows": []}, "empty summary")
    bad_raise = False
    try:
        run(crawl, 7)
    except ToolError:
        bad_raise = True
    chk(bad_raise, "a non-list final_urls raises ToolError")
    hostile = {"pages": {lp: {"url": lp, "status": 200, "final": lp, "ctype": "text/html", "doc": {}},
                         about: "garbage"},
               "edges": [[None], "x", [1, 2], [home, None], [home, lp]]}
    hf, hp = run(hostile, [lp, about, 42, "", None, "not a url", "javascript:void(0)", {"x": 1}])
    chk(isinstance(hf, list) and isinstance(hp, list), "hostile pages and edges never crash run")
    chk(summary(hostile, [42, None])["rows"] == [], "non-string ad URLs are skipped by summary")
    png = {"pages": {"https://ex.com/x.png": page_of("https://ex.com/x.png", ctype="image/png", doc=None)},
           "edges": [[home, "https://ex.com/x.png"]]}
    chk(run(png, ["https://ex.com/x.png"])[0] == [], "a non-HTML ad URL is neither noindex nor an equity sink")
    chk(summary(png, ["https://ex.com/x.png"])["rows"][0][3] == "not HTML", "summary names a non-HTML ad URL")
    return n
