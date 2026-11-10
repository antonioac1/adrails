"""Content checks for the AdRails site audit (adsops/seo): indexability, titles, headings, duplicates, canonicals,
doctype/lang, hreflang, JSON-LD validity and the schema matrix.

Read-only and pure: no network, disk or clock access, no global mutable state. Everything is passed in (the crawl
result dict described in SPEC-B) and everything returned is JSON-serialisable (dicts/lists/str/int/float/None).
Nothing here raises on hostile data: missing keys fall back to defaults, a page without a document is simply not
HTML, and a crawl with no pages yields no findings. `run()` returns (findings, passed), where `passed` names the
checks that ran and found nothing wrong.

Findings follow the shared contract: {"code", "urls", "detail", "data"} with a non-empty url list, one plain-English
sentence and a small JSON-safe data dict (lists capped at 20 items).
"""
import re
import zlib
import json
from urllib.parse import urlsplit

from adsops.errors import ToolError
from adsops.seo.extract import template_of

MAX_STR = 500
MAX_LIST = 20
MAX_URLS = 500
MAX_TEXT = 60000
TITLE_MAX = 60
DESC_MAX = 160
SHINGLE_K = 5
DEFAULT_THIN_WORDS = 250
DEFAULT_DUP_THRESHOLD = 0.8
DEFAULT_MAX_DUP_PAGES = 1500
LOW_SHARE_MIN_PAGES = 10
LOW_SHARE_THRESHOLD = 0.6
HTML_CTYPES = ("text/html", "application/xhtml+xml")
ARTICLE_TYPES = ("Article", "BlogPosting", "NewsArticle", "TechArticle")
ARTICLE_TEMPLATE_HINTS = ("guide", "blog", "news", "article", "resources", "insights", "learn")
HREFLANG_RX = re.compile(r"^(x-default|[a-z]{2,3}(-([a-z]{2}|[0-9]{3}|[a-z]{4}))?)$", re.I)
_AUTO = object()


# ---------------------------------------------------------------------------------------------------------------
# small, total helpers (never raise, never touch the outside world)
# ---------------------------------------------------------------------------------------------------------------
def _text(value, limit=MAX_STR):
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


def _doc_key(page, key):
    """List value of `key` inside the page document ([] when absent)."""
    doc = _doc(page)
    return _list(doc.get(key)) if doc else []


def _ctype(page):
    """Bare, lowercased content type ('text/html; charset=utf-8' -> 'text/html')."""
    return _text(page.get("ctype") if isinstance(page, dict) else "", 200).split(";")[0].strip().lower()


def _first_text(page_or_doc, key):
    """First non-empty cleaned string under `key` of a page (or a document), else ''."""
    doc = page_or_doc if isinstance(page_or_doc, dict) and key in page_or_doc else _doc(page_or_doc)
    source = _list(doc.get(key)) if doc else []
    for value in source:
        cleaned = _text(value)
        if cleaned:
            return cleaned
    return ""


def _canonicals(page):
    """Non-empty canonical URLs of `page`, in document order."""
    out = []
    for value in _doc_key(page, "canonicals"):
        cleaned = _text(value)
        if cleaned:
            out.append(cleaned)
    return out


def _host(url):
    """Lowercased hostname of `url` ('' when it cannot be parsed)."""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _has_noindex_token(value):
    """True when `value` carries a noindex token ('noindex, nofollow' -> True; 'none' = noindex+nofollow -> True).
    X-Robots-Tag may be agent-scoped ('googlebot: noindex'); the scope prefix is ignored (conservative)."""
    s = value if isinstance(value, str) else ""
    for token in s.replace(",", " ").replace(";", " ").replace(":", " ").split():
        if token.lower() in ("noindex", "none"):
            return True
    return False


def _meta_noindex(page):
    """True when any <meta name=robots|googlebot> content carries a noindex token."""
    for entry in _doc_key(page, "robots"):
        if _has_noindex_token(entry):
            return True
    return False


def _header_noindex(page):
    """True when the X-Robots-Tag response header carries a noindex token."""
    headers = page.get("headers") if isinstance(page, dict) else None
    if not isinstance(headers, dict):
        return False
    for key, value in headers.items():
        if isinstance(key, str) and key.strip().lower() == "x-robots-tag" and _has_noindex_token(value):
            return True
    return False


def _words(page):
    """Visible word count of `page` (doc['words'] when sane, else counted from doc['text'])."""
    doc = _doc(page)
    if doc is None:
        return 0
    value = doc.get("words")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return len(_text(doc.get("text"), MAX_TEXT).split())


def _types(page):
    """Unique JSON-LD @type values of `page`, in document order."""
    out = []
    for value in _doc_key(page, "jsonld_types"):
        cleaned = _text(value, 100)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _template(url):
    """Report template of `url` via extract.template_of, never raising."""
    try:
        return template_of(url if isinstance(url, str) else "")
    except Exception:
        return "unknown"


def _pairs(pages):
    """-> [(url, page)] for every page that has a usable url (findings must always name a URL)."""
    out = []
    for page in pages:
        url = _url(page)
        if url:
            out.append((url, page))
    return out


def _uniq(items, cap=MAX_URLS):
    """Order-preserving, capped, de-duplicated list of non-empty strings."""
    out = []
    seen = set()
    for item in items:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            out.append(item)
            if len(out) >= cap:
                break
    return out


def _ex(urls):
    """First url for a 'e.g. ...' clause ('(none)' when empty)."""
    items = [u for u in urls if u]
    return items[0] if items else "(none)"


def _first_desc(rows):
    """First [url, n] row rendered as 'url (n)' ('(none)' when empty)."""
    return "%s (%d)" % (rows[0][0], rows[0][1]) if rows else "(none)"


def _first_pair(rows):
    """First [url, other] row rendered as 'url -> other' ('(none)' when empty)."""
    return "%s -> %s" % (rows[0][0], rows[0][1]) if rows else "(none)"


def _first_triple(rows):
    """First [url, other, n] row rendered as 'url -> other (n)' ('(none)' when empty)."""
    return "%s -> %s (%s)" % (rows[0][0], rows[0][1], rows[0][2]) if rows else "(none)"


# ---------------------------------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------------------------------
def is_indexable(page):
    """True when the page can be indexed: 200, final == url, HTML, no noindex, self-canonical (or no canonical)."""
    if not isinstance(page, dict):
        return False
    if _int(page.get("status"), -1) != 200:
        return False
    url = _url(page)
    if not url or page.get("final") != url:
        return False
    if _ctype(page) not in HTML_CTYPES:
        return False
    if _doc(page) is None:
        return False
    if _meta_noindex(page) or _header_noindex(page):
        return False
    canon = _canonicals(page)
    if canon and canon[0] != url:
        return False
    return True


def why_not_indexable(page):
    """-> the first blocking reason as a short phrase, or None when the page is indexable."""
    page = page if isinstance(page, dict) else {}
    status = _int(page.get("status"), 0)
    if status != 200:
        return "HTTP %d" % status
    url = _url(page)
    final = page.get("final") if isinstance(page.get("final"), str) else ""
    if final != url:
        return "redirects to %s" % (final or "(unknown)")
    ctype = _ctype(page)
    if ctype not in HTML_CTYPES:
        return "not HTML (%s)" % (ctype or "unknown")
    if _meta_noindex(page):
        return "noindex (meta robots)"
    if _header_noindex(page):
        return "noindex (X-Robots-Tag)"
    canon = _canonicals(page)
    if canon and canon[0] != url:
        return "canonical points to %s" % canon[0]
    if _doc(page) is None:
        return "no document"
    return None


def html_pages(crawl):
    """-> HTML pages of the crawl (status 200, final == url, document present), in crawl order."""
    out = []
    for key, page in _pages(crawl).items():
        if not isinstance(page, dict):
            continue
        url = _url(page) or (key if isinstance(key, str) else "")
        if not url:
            continue
        if _int(page.get("status"), -1) != 200:
            continue
        if page.get("final") != url:
            continue
        if _doc(page) is None:
            continue
        out.append(page)
    return out


def shingles(text, k=SHINGLE_K):
    """-> set of k-word shingles of the lowercased words of `text`; shorter texts give one whole-text shingle."""
    if text is None:
        return set()
    try:
        s = text if isinstance(text, str) else str(text)
        words = s.lower().split()
    except Exception:
        return set()
    kk = _int(k, SHINGLE_K)
    if kk <= 0:
        kk = SHINGLE_K
    if not words:
        return set()
    if len(words) <= kk:
        return {" ".join(words)}
    return {" ".join(words[i:i + kk]) for i in range(len(words) - kk + 1)}


def jaccard(a, b):
    """Jaccard similarity of two shingle sets (0.0 when either side is empty or not a set)."""
    if not isinstance(a, (set, frozenset)) or not isinstance(b, (set, frozenset)):
        return 0.0
    if not a or not b:
        return 0.0
    union = len(a | b)
    return 0.0 if union == 0 else len(a & b) / union


def stats(crawl):
    """-> headline content numbers: html_pages, indexable, indexable_share, templates, schema_matrix."""
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("checks_content.stats: crawl must be a dict")
    crawl = crawl if isinstance(crawl, dict) else {}
    h_pages = html_pages(crawl)
    idx_pages = [page for page in h_pages if is_indexable(page)]
    templates = {}
    for url, page in _pairs(h_pages):
        name = _template(url)
        templates[name] = templates.get(name, 0) + 1
    matrix = {}
    for url, page in _pairs(idx_pages):
        row = matrix.setdefault(_template(url), {"pages": 0, "types": {}})
        row["pages"] += 1
        for typ in _types(page):
            row["types"][typ] = row["types"].get(typ, 0) + 1
    share = (len(idx_pages) / len(h_pages)) if h_pages else 0
    return {"html_pages": len(h_pages), "indexable": len(idx_pages), "indexable_share": round(share, 3),
            "templates": templates, "schema_matrix": matrix}


def _groups(pairs):
    """-> (groups, urls) for identical non-empty values: groups are [value, [urls]], biggest first."""
    bucket = {}
    for url, value in pairs:
        if value:
            bucket.setdefault(value, []).append(url)
    groups = [[value, urls] for value, urls in bucket.items() if len(urls) > 1]
    groups.sort(key=lambda g: (-len(g[1]), g[0]))
    urls = [u for group in groups for u in group[1]]
    return groups, urls


def _dup_pairs(pages, sets, thr):
    """-> (pairs_both_self_canonical, other_pairs) of [url, url, round(score, 2)] at or above `thr`."""
    own, other = [], []
    urls = [_url(page) for page in pages]
    for i in range(len(pages)):
        sa = sets.get(urls[i]) or set()
        if not sa:
            continue
        for j in range(i + 1, len(pages)):
            sb = sets.get(urls[j]) or set()
            if not sb:
                continue
            lo, hi = (len(sa), len(sb)) if len(sa) <= len(sb) else (len(sb), len(sa))
            if hi <= 0 or (lo / hi) < thr:            # cannot possibly reach the threshold: skip early
                continue
            score = jaccard(sa, sb)
            if score < thr:
                continue
            pair = [urls[i], urls[j], round(score, 2)]
            left = _canonicals(pages[i])
            right = _canonicals(pages[j])
            if left and left[0] == urls[i] and right and right[0] == urls[j]:
                own.append(pair)
            else:
                other.append(pair)
    return own, other


def _dup_desc(pairs):
    """'a vs b (0.95)' for the first pair ('(none)' when empty)."""
    if not pairs:
        return "(none)"
    return "%s vs %s" % (pairs[0][0], pairs[0][1])


def run(crawl, thin_words=DEFAULT_THIN_WORDS, dup_threshold=DEFAULT_DUP_THRESHOLD, max_dup_pages=DEFAULT_MAX_DUP_PAGES):
    """-> (findings, passed) for every content check in SPEC-B, over one crawl result."""
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("checks_content.run: crawl must be a dict")
    crawl = crawl if isinstance(crawl, dict) else {}
    pages = _pages(crawl)
    h_pages = html_pages(crawl)                                   # H: 200, final == url, document present
    idx_pages = [page for page in h_pages if is_indexable(page)]   # P: indexable
    HP, IP = _pairs(h_pages), _pairs(idx_pages)
    thin = _int(thin_words, DEFAULT_THIN_WORDS)
    thr = dup_threshold if isinstance(dup_threshold, (int, float)) and not isinstance(dup_threshold, bool) \
        else DEFAULT_DUP_THRESHOLD
    max_dup = _int(max_dup_pages, DEFAULT_MAX_DUP_PAGES)
    findings, passed = [], []

    def emit(code, urls, detail, data=None):
        """Append one finding; returns True when something was reported (so the check did not pass)."""
        clean = _uniq(urls)
        if not clean:
            return False
        detail = _text(detail, 600)
        if not detail:
            detail = "%d URLs affected." % len(clean)
        elif not detail.endswith("."):
            detail += "."                                   # findings always read as one full sentence
        findings.append({"code": code, "urls": clean, "detail": detail,
                         "data": data if isinstance(data, dict) else {}})
        return True

    def settle(code, fired, ran=True):
        """Record `code` in `passed` when it ran and stayed silent."""
        if ran and not fired:
            passed.append(code)

    # --- titles ---------------------------------------------------------------------------------------------
    missing = [u for u, p in IP if not _first_text(p, "titles")]
    settle("title_missing", emit(
        "title_missing", missing,
        "%d of %d indexable pages have no non-empty <title> element (e.g. %s)" % (len(missing), len(IP), _ex(missing)),
        {"count": len(missing), "examples": missing[:MAX_LIST]}), ran=bool(IP))

    many = [[u, len(_doc_key(p, "titles"))] for u, p in HP if len(_doc_key(p, "titles")) > 1]
    settle("title_multiple", emit(
        "title_multiple", [u for u, _ in many],
        "%d of %d HTML pages carry more than one <title> element (e.g. %s)" % (len(many), len(HP), _first_desc(many)),
        {"count": len(many), "examples": many[:MAX_LIST]}), ran=bool(HP))

    groups, dup_urls = _groups([(u, _first_text(p, "titles")) for u, p in IP])
    settle("title_duplicate", emit(
        "title_duplicate", dup_urls,
        "%d <title> values are shared by %d indexable pages (e.g. '%s' on %s)"
        % (len(groups), len(dup_urls), groups[0][0] if groups else "", _ex(groups[0][1]) if groups else "(none)"),
        {"count": len(groups), "groups": [[g[0], _uniq(g[1], MAX_LIST)] for g in groups[:MAX_LIST]]}), ran=bool(IP))

    long_titles = []
    for u, p in IP:
        title = _first_text(p, "titles")
        if len(title) > TITLE_MAX:
            long_titles.append([u, len(title)])
    settle("title_too_long", emit(
        "title_too_long", [u for u, _ in long_titles],
        "%d indexable pages have a <title> longer than %d characters (e.g. %s)"
        % (len(long_titles), TITLE_MAX, _first_desc(long_titles)),
        {"examples": long_titles[:MAX_LIST], "limit": TITLE_MAX}), ran=bool(IP))

    # --- meta descriptions ----------------------------------------------------------------------------------
    missing = [u for u, p in IP if not _first_text(p, "descriptions")]
    settle("description_missing", emit(
        "description_missing", missing,
        "%d of %d indexable pages have no meta description (e.g. %s)" % (len(missing), len(IP), _ex(missing)),
        {"count": len(missing), "examples": missing[:MAX_LIST]}), ran=bool(IP))

    groups, dup_urls = _groups([(u, _first_text(p, "descriptions")) for u, p in IP])
    settle("description_duplicate", emit(
        "description_duplicate", dup_urls,
        "%d meta descriptions are shared by %d indexable pages (e.g. '%s' on %s)"
        % (len(groups), len(dup_urls), groups[0][0] if groups else "", _ex(groups[0][1]) if groups else "(none)"),
        {"count": len(groups), "groups": [[g[0], _uniq(g[1], MAX_LIST)] for g in groups[:MAX_LIST]]}), ran=bool(IP))

    long_desc = []
    for u, p in IP:
        desc = _first_text(p, "descriptions")
        if len(desc) > DESC_MAX:
            long_desc.append([u, len(desc)])
    settle("description_too_long", emit(
        "description_too_long", [u for u, _ in long_desc],
        "%d indexable pages have a meta description longer than %d characters (e.g. %s)"
        % (len(long_desc), DESC_MAX, _first_desc(long_desc)),
        {"examples": long_desc[:MAX_LIST], "limit": DESC_MAX}), ran=bool(IP))

    # --- h1 -------------------------------------------------------------------------------------------------
    missing = [u for u, p in IP if not _first_text(p, "h1")]
    settle("h1_missing", emit(
        "h1_missing", missing,
        "%d of %d indexable pages have no non-empty <h1> (e.g. %s)" % (len(missing), len(IP), _ex(missing)),
        {"count": len(missing), "examples": missing[:MAX_LIST]}), ran=bool(IP))

    many = [[u, len(_doc_key(p, "h1"))] for u, p in HP if len(_doc_key(p, "h1")) > 1]
    settle("h1_multiple", emit(
        "h1_multiple", [u for u, _ in many],
        "%d of %d HTML pages carry more than one <h1> (e.g. %s)" % (len(many), len(HP), _first_desc(many)),
        {"count": len(many), "examples": many[:MAX_LIST]}), ran=bool(HP))

    groups, dup_urls = _groups([(u, _first_text(p, "h1")) for u, p in IP])
    settle("h1_duplicate", emit(
        "h1_duplicate", dup_urls,
        "%d <h1> texts are shared by %d indexable pages (e.g. '%s' on %s)"
        % (len(groups), len(dup_urls), groups[0][0] if groups else "", _ex(groups[0][1]) if groups else "(none)"),
        {"count": len(groups), "groups": [[g[0], _uniq(g[1], MAX_LIST)] for g in groups[:MAX_LIST]]}), ran=bool(IP))

    # --- thin content ---------------------------------------------------------------------------------------
    thin_rows = [[u, _words(p)] for u, p in IP if _words(p) < thin]
    settle("thin_content", emit(
        "thin_content", [u for u, _ in thin_rows],
        "%d indexable pages carry fewer than %d words of visible text (e.g. %s)"
        % (len(thin_rows), thin, _first_desc(thin_rows)),
        {"examples": thin_rows[:MAX_LIST], "threshold": thin}), ran=bool(IP))

    # --- near duplicates ------------------------------------------------------------------------------------
    sample = idx_pages
    if max_dup > 0 and len(idx_pages) > max_dup:
        sample = idx_pages[:max_dup]
    truncated = len(sample) < len(idx_pages)
    sets = {_url(page): shingles((_doc(page) or {}).get("text", "")) for page in sample}
    own_pairs, other_pairs = _dup_pairs(sample, sets, thr)
    settled = []

    self_urls = []
    for pair in own_pairs:
        self_urls.extend(pair[:2])
    settle("near_duplicate_self_canonical", emit(
        "near_duplicate_self_canonical", self_urls,
        "%d pairs of self-canonical indexable pages are near-duplicates at %.0f%% word-shingle overlap (e.g. %s)"
        % (len(own_pairs), thr * 100, _dup_desc(own_pairs)),
        {"pairs": own_pairs[:MAX_LIST], "truncated": truncated, "threshold": thr}), ran=len(sample) >= 2)
    settled.append("near_duplicate_self_canonical")

    other_urls = []
    for pair in other_pairs:
        other_urls.extend(pair[:2])
    settle("near_duplicate", emit(
        "near_duplicate", other_urls,
        "%d pairs of indexable pages are near-duplicates at %.0f%% word-shingle overlap (e.g. %s)"
        % (len(other_pairs), thr * 100, _dup_desc(other_pairs)),
        {"pairs": other_pairs[:MAX_LIST], "truncated": truncated, "threshold": thr}), ran=len(sample) >= 2)
    settled.append("near_duplicate")

    # --- canonicals -----------------------------------------------------------------------------------------
    multiple = [[u, _canonicals(p)] for u, p in HP if len(set(_canonicals(p))) > 1]
    settle("canonical_multiple", emit(
        "canonical_multiple", [u for u, _ in multiple],
        "%d of %d HTML pages declare more than one distinct canonical URL (e.g. %s)"
        % (len(multiple), len(HP), _first_desc([[u, len(c)] for u, c in multiple])),
        {"count": len(multiple), "examples": [[u, _uniq(c, MAX_LIST)] for u, c in multiple[:MAX_LIST]]}), ran=bool(HP))

    cross = []
    for u, p in HP:
        canon = _canonicals(p)
        if canon and _host(canon[0]) and _host(canon[0]) != _host(u):
            cross.append([u, canon[0]])
    settle("canonical_cross_host", emit(
        "canonical_cross_host", [u for u, _ in cross],
        "%d pages point their canonical at another host (e.g. %s)" % (len(cross), _first_pair(cross)),
        {"count": len(cross), "pairs": cross[:MAX_LIST]}), ran=bool(HP))

    non_self = []
    for u, p in HP:
        canon = _canonicals(p)
        if canon and canon[0] != u:
            non_self.append([u, canon[0]])
    settle("canonical_non_self", emit(
        "canonical_non_self", [u for u, _ in non_self],
        "%d of %d HTML pages canonicalise to a different URL (e.g. %s)"
        % (len(non_self), len(HP), _first_pair(non_self)),
        {"count": len(non_self), "pairs": non_self[:MAX_LIST]}), ran=bool(HP))

    bad_targets = []
    for u, p in HP:
        canon = _canonicals(p)
        if not canon:
            continue
        target = pages.get(canon[0])
        if not isinstance(target, dict):
            continue                                  # canonical targets we never crawled are ignored
        status = _int(target.get("status"), 0)
        if status != 200 or target.get("final") != canon[0]:
            bad_targets.append([u, canon[0], status])
    settle("canonical_to_non_200", emit(
        "canonical_to_non_200", [u for u, _, _ in bad_targets],
        "%d pages canonicalise to a crawled URL that does not answer 200 directly (e.g. %s)"
        % (len(bad_targets), _first_triple(bad_targets)),
        {"count": len(bad_targets), "pairs": bad_targets[:MAX_LIST]}), ran=bool(HP))

    # --- doctype / lang -------------------------------------------------------------------------------------
    no_doctype = [u for u, p in HP if not (_doc(p) or {}).get("doctype")]
    settle("doctype_missing", emit(
        "doctype_missing", no_doctype,
        "%d of %d HTML pages have no <!doctype html> declaration (e.g. %s)"
        % (len(no_doctype), len(HP), _ex(no_doctype)),
        {"count": len(no_doctype), "examples": no_doctype[:MAX_LIST]}), ran=bool(HP))

    no_lang = [u for u, p in HP if not _text((_doc(p) or {}).get("lang"), 40)]
    settle("lang_missing", emit(
        "lang_missing", no_lang,
        "%d of %d HTML pages have no lang attribute on <html> (e.g. %s)" % (len(no_lang), len(HP), _ex(no_lang)),
        {"count": len(no_lang), "examples": no_lang[:MAX_LIST]}), ran=bool(HP))

    # --- hreflang -------------------------------------------------------------------------------------------
    annotated = [(u, p) for u, p in HP if _doc_key(p, "hreflang")]
    errors = []
    for u, p in annotated:
        for entry in _doc_key(p, "hreflang"):
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            lang = _text(entry[0], 40)
            href = entry[1] if isinstance(entry[1], str) else ""
            if not HREFLANG_RX.match(lang):
                errors.append([u, lang, href, "invalid code"])
                continue
            target = pages.get(href)
            if isinstance(target, dict):
                status = _int(target.get("status"), 0)
                if status != 200:
                    errors.append([u, lang, href, "target status %d" % status])
                    continue
                back = [e for e in _doc_key(target, "hreflang") if isinstance(e, (list, tuple)) and len(e) >= 2
                        and e[1] == u]
                if not back:
                    errors.append([u, lang, href, "no return link"])
    kinds = {}
    for row in errors:
        kinds[row[3]] = kinds.get(row[3], 0) + 1
    settle("hreflang_errors", emit(
        "hreflang_errors", [row[0] for row in errors],
        "%d hreflang annotations on %d pages are wrong (%s), e.g. %s"
        % (len(errors), len({row[0] for row in errors}),
           ", ".join("%s x%d" % (k, v) for k, v in sorted(kinds.items())) or "none",
           "%s -> %s %s (%s)" % (errors[0][0], errors[0][1], errors[0][2], errors[0][3]) if errors else "(none)"),
        {"errors": errors[:MAX_LIST], "count": len(errors)}), ran=bool(annotated))

    # --- JSON-LD --------------------------------------------------------------------------------------------
    bad_jsonld = [[u, _int((_doc(p) or {}).get("jsonld_errors"), 0)] for u, p in HP
                  if _int((_doc(p) or {}).get("jsonld_errors"), 0) > 0]
    settle("jsonld_invalid", emit(
        "jsonld_invalid", [u for u, _ in bad_jsonld],
        "%d JSON-LD blocks across %d HTML pages failed to parse (e.g. %s)"
        % (sum(n for _, n in bad_jsonld), len(bad_jsonld), _first_desc(bad_jsonld)),
        {"blocks": sum(n for _, n in bad_jsonld), "examples": bad_jsonld[:MAX_LIST]}), ran=bool(HP))

    # --- schema coverage ------------------------------------------------------------------------------------
    matrix = {}
    for u, p in IP:
        row = matrix.setdefault(_template(u), {"pages": 0, "types": {}, "members": []})
        row["pages"] += 1
        types = _types(p)
        for typ in types:
            row["types"][typ] = row["types"].get(typ, 0) + 1
        row["members"].append((u, set(types)))

    partial, gap_urls = [], []
    for name in sorted(matrix):
        row = matrix[name]
        total = row["pages"]
        if total < 3:
            continue
        for typ in sorted(row["types"]):
            have = row["types"][typ]
            if have * 2 >= total and have < total:
                partial.append([name, typ, have, total])
                gap_urls.extend([u for u, types in row["members"] if typ not in types])

    article_templates, article_urls = [], []
    for name in sorted(matrix):
        row = matrix[name]
        if row["pages"] < 1 or not any(hint in name.lower() for hint in ARTICLE_TEMPLATE_HINTS):
            continue
        lacking = [u for u, types in row["members"] if not (types & set(ARTICLE_TYPES))]
        if lacking:
            article_templates.append(name)
            article_urls.extend(lacking)

    phrases = []
    if partial:
        phrases.append("%d of %d %s pages lack %s"
                       % (partial[0][3] - partial[0][2], partial[0][3], partial[0][0], partial[0][1]))
    if article_templates:
        phrases.append("%d pages in article-style templates (e.g. %s) carry no Article, BlogPosting, NewsArticle "
                       "or TechArticle markup" % (len(article_urls), article_templates[0]))
    detail = "Schema coverage is incomplete: %s." % (" and ".join(phrases) if phrases else "no gaps found")
    settle("schema_gaps", emit(
        "schema_gaps", gap_urls + article_urls, detail,
        {"partial": partial[:MAX_LIST], "article_templates": article_templates[:MAX_LIST],
         "article_pages": len(article_urls)}), ran=bool(IP))

    # --- images ---------------------------------------------------------------------------------------------
    no_alt, alt_total = [], 0
    for u, p in HP:
        count = 0
        for image in _doc_key(p, "images"):
            if isinstance(image, dict) and image.get("alt") is None:
                count += 1
        if count:
            no_alt.append([u, count])
            alt_total += count
    settle("images_missing_alt", emit(
        "images_missing_alt", [u for u, _ in no_alt],
        "%d images on %d pages have no alt attribute at all (e.g. %s)"
        % (alt_total, len(no_alt), _first_desc(no_alt)),
        {"count": alt_total, "pages": len(no_alt), "examples": no_alt[:MAX_LIST]}), ran=bool(HP))

    # --- indexable share ------------------------------------------------------------------------------------
    share = (len(idx_pages) / len(h_pages)) if h_pages else 0.0
    reasons = {}
    for u, p in HP:
        reason = why_not_indexable(p)
        if reason is None:
            continue
        kind = reason.split(" ")[0]
        reasons[kind] = reasons.get(kind, 0) + 1
    non_indexable = [u for u, p in HP if not is_indexable(p)]
    settle("low_indexable_share", emit(
        "low_indexable_share", non_indexable if (len(h_pages) >= LOW_SHARE_MIN_PAGES and share < LOW_SHARE_THRESHOLD)
        else [],
        "Only %d of %d HTML pages (%.1f%%) are indexable (%s)"
        % (len(idx_pages), len(h_pages), share * 100,
           ", ".join("%s x%d" % (k, v) for k, v in sorted(reasons.items())) or "no pages"),
        {"share": round(share, 3), "reasons": reasons}),
        ran=len(h_pages) >= LOW_SHARE_MIN_PAGES)

    return findings, passed


# ---------------------------------------------------------------------------------------------------------------
# offline self-test
# ---------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    """Offline assertions over fake crawl dicts; returns the number of checks performed."""
    n = 0

    def chk(cond, msg):
        nonlocal n
        n += 1
        assert cond, "seo.checks_content.selftest: " + msg

    def body(url, words=300):
        """Unique 300-word filler per URL, so untouched pages are never near-duplicates."""
        start = (zlib.crc32(url.encode("utf-8")) & 0xFFFFF) * 1000
        return " ".join("t%d" % (start + i) for i in range(words))

    def mkpage(url, status=200, final=None, ctype="text/html; charset=utf-8", headers=None, doc=_AUTO,
               doc_over=None):
        if doc is _AUTO:
            doc = {"doctype": True, "lang": "en", "titles": ["Title of " + url],
                   "descriptions": ["Description of " + url], "robots": [], "canonicals": [], "h1": ["H1 of " + url],
                   "h2_count": 2, "hreflang": [], "links": [], "jsonld_types": [], "jsonld_errors": 0,
                   "text": body(url), "words": 300, "preload_images": [], "images": [], "video_posters": [],
                   "viewport": True, "og_image": True, "meta_refresh": None}
            if isinstance(doc_over, dict):
                doc.update(doc_over)
        return {"url": url, "chain": [], "status": status, "final": url if final is None else final, "ctype": ctype,
                "bytes": 1000, "truncated": False, "ms": 5, "headers": headers or {}, "error": None, "depth": 1,
                "in_sitemap": True, "discovered": "link", "doc": doc}

    def crawl_of(pages):
        return {"settings": {}, "pages": pages, "edges": [], "external_links": {}, "notes": []}

    def codes(result):
        return [f["code"] for f in result]

    # --- is_indexable / why_not_indexable --------------------------------------------------------------------
    chk(is_indexable(mkpage("https://ex.com/a/")), "a 200 self-consistent HTML page is indexable")
    chk(is_indexable(mkpage("https://ex.com/a/", ctype="application/xhtml+xml; charset=utf-8")),
        "xhtml counts as HTML")
    chk(is_indexable(mkpage("https://ex.com/a/", doc_over={"canonicals": ["https://ex.com/a/"]})),
        "a self canonical is fine")
    chk(not is_indexable(mkpage("https://ex.com/a/", doc=None)), "a page without a document is not indexable")
    chk(not is_indexable(mkpage("https://ex.com/a/", status=404)), "a 404 is not indexable")
    chk(not is_indexable(mkpage("https://ex.com/a/", final="https://ex.com/b/")), "a redirect is not indexable")
    chk(not is_indexable(mkpage("https://ex.com/a/", ctype="application/pdf")), "a PDF is not indexable")
    chk(not is_indexable(mkpage("https://ex.com/a/", doc_over={"robots": ["noindex, follow"]})),
        "meta robots noindex blocks indexing")
    chk(not is_indexable(mkpage("https://ex.com/a/", headers={"x-robots-tag": "noindex, nofollow"})),
        "X-Robots-Tag noindex blocks indexing")
    chk(is_indexable(mkpage("https://ex.com/a/", headers={"X-Robots-Tag": "max-snippet:-1"})),
        "a non-noindex X-Robots-Tag is harmless")
    chk(not is_indexable(mkpage("https://ex.com/a/", doc_over={"canonicals": ["https://ex.com/b/"]})),
        "a canonical elsewhere blocks indexing")
    chk(not is_indexable({}), "an empty dict is not indexable")
    chk(not is_indexable(None), "None is not indexable")

    chk(why_not_indexable(mkpage("https://ex.com/a/")) is None, "indexable pages have no reason")
    chk(why_not_indexable(mkpage("https://ex.com/a/", status=404)) == "HTTP 404", "reason for a 404")
    chk(why_not_indexable(mkpage("https://ex.com/a/", final="https://ex.com/b/"))
        == "redirects to https://ex.com/b/", "reason for a redirect")
    chk(why_not_indexable(mkpage("https://ex.com/a/", ctype="application/pdf")).startswith("not HTML"),
        "reason for a non-HTML page")
    chk(why_not_indexable(mkpage("https://ex.com/a/", doc_over={"robots": ["noindex"]}))
        == "noindex (meta robots)", "reason for meta noindex")
    chk(why_not_indexable(mkpage("https://ex.com/a/", headers={"X-Robots-Tag": "noindex"}))
        == "noindex (X-Robots-Tag)", "reason for header noindex")
    chk(why_not_indexable(mkpage("https://ex.com/a/", doc_over={"canonicals": ["https://ex.com/b/"]}))
        == "canonical points to https://ex.com/b/", "reason for a foreign canonical")

    # --- html_pages / shingles / jaccard / stats -------------------------------------------------------------
    mixed = crawl_of({"https://ex.com/": mkpage("https://ex.com/"),
                      "https://ex.com/doc.pdf": mkpage("https://ex.com/doc.pdf", ctype="application/pdf"),
                      "https://ex.com/go/": mkpage("https://ex.com/go/", final="https://ex.com/")})
    chk(len(html_pages(mixed)) == 2, "html_pages keeps 200 HTML pages and drops redirects")
    chk(html_pages({}) == [], "html_pages of an empty crawl is empty")
    chk(shingles("a b c d e f") == {"a b c d e", "b c d e f"}, "six words give two five-word shingles")
    chk(shingles("a b") == {"a b"}, "a very short text gives one whole-text shingle")
    chk(shingles("   ") == set(), "blank text gives no shingles")
    chk(shingles(None) == set() and shingles(12) == {"12"}, "hostile shingle input does not raise")
    chk(jaccard(shingles("a b c d e"), shingles("a b c d e")) == 1.0, "identical texts score 1.0")
    chk(jaccard(set(), shingles("a b")) == 0.0, "an empty set scores 0.0")
    chk(jaccard(shingles("a b c d e"), shingles("v w x y z")) == 0.0, "unrelated texts score 0.0")
    chk(jaccard("nope", {"a"}) == 0.0, "non-set input scores 0.0")

    one = crawl_of({"https://ex.com/": mkpage("https://ex.com/")})
    st = stats(one)
    chk(st["html_pages"] == 1 and st["indexable"] == 1 and st["indexable_share"] == 1.0, "stats on one clean page")
    chk(st["templates"] == {"home": 1}, "templates counts HTML pages per template")
    chk(st["schema_matrix"] == {"home": {"pages": 1, "types": {}}}, "schema matrix counts indexable pages")
    chk(stats(crawl_of({})) == {"html_pages": 0, "indexable": 0, "indexable_share": 0, "templates": {},
                                "schema_matrix": {}}, "stats of an empty crawl is zeroed")

    # --- clean crawl ------------------------------------------------------------------------------------------
    findings, passed = run(one)
    chk(findings == [], "a clean page produces no finding")
    chk("title_missing" in passed and "h1_missing" in passed and "canonical_non_self" in passed,
        "passed names the checks that ran and found nothing")

    two = crawl_of({"https://ex.com/": mkpage("https://ex.com/"),
                    "https://ex.com/about/": mkpage("https://ex.com/about/")})
    findings, passed = run(two)
    chk(findings == [], "two clean pages produce no finding")
    chk("near_duplicate" in passed and "near_duplicate_self_canonical" in passed,
        "distinct texts pass the near-duplicate checks")

    # --- a crawl where every check fires ----------------------------------------------------------------------
    same = " ".join("dup%d" % i for i in range(300))
    other = " ".join("other%d" % i for i in range(300))
    pages = {}
    pages["https://ex.com/"] = mkpage("https://ex.com/")
    pages["https://ex.com/no-title/"] = mkpage("https://ex.com/no-title/", doc_over={"titles": []})
    pages["https://ex.com/two-titles/"] = mkpage("https://ex.com/two-titles/",
                                                 doc_over={"titles": ["First title", "Second title"]})
    pages["https://ex.com/dup-a/"] = mkpage("https://ex.com/dup-a/", doc_over={"titles": ["Shared title"]})
    pages["https://ex.com/dup-b/"] = mkpage("https://ex.com/dup-b/", doc_over={"titles": ["Shared title"]})
    pages["https://ex.com/long-title/"] = mkpage("https://ex.com/long-title/", doc_over={"titles": ["L" * 70]})
    pages["https://ex.com/no-desc/"] = mkpage("https://ex.com/no-desc/", doc_over={"descriptions": []})
    pages["https://ex.com/dup-desc-a/"] = mkpage("https://ex.com/dup-desc-a/",
                                                 doc_over={"descriptions": ["Shared description"]})
    pages["https://ex.com/dup-desc-b/"] = mkpage("https://ex.com/dup-desc-b/",
                                                 doc_over={"descriptions": ["Shared description"]})
    pages["https://ex.com/long-desc/"] = mkpage("https://ex.com/long-desc/", doc_over={"descriptions": ["D" * 200]})
    pages["https://ex.com/no-h1/"] = mkpage("https://ex.com/no-h1/", doc_over={"h1": []})
    pages["https://ex.com/two-h1/"] = mkpage("https://ex.com/two-h1/", doc_over={"h1": ["One", "Two"]})
    pages["https://ex.com/dup-h1-a/"] = mkpage("https://ex.com/dup-h1-a/", doc_over={"h1": ["Shared heading"]})
    pages["https://ex.com/dup-h1-b/"] = mkpage("https://ex.com/dup-h1-b/", doc_over={"h1": ["Shared heading"]})
    pages["https://ex.com/thin/"] = mkpage("https://ex.com/thin/", doc_over={"text": "tiny page", "words": 2})
    pages["https://ex.com/self-a/"] = mkpage("https://ex.com/self-a/",
                                             doc_over={"text": same, "canonicals": ["https://ex.com/self-a/"]})
    pages["https://ex.com/self-b/"] = mkpage("https://ex.com/self-b/",
                                             doc_over={"text": same, "canonicals": ["https://ex.com/self-b/"]})
    pages["https://ex.com/plain-a/"] = mkpage("https://ex.com/plain-a/", doc_over={"text": other})
    pages["https://ex.com/plain-b/"] = mkpage("https://ex.com/plain-b/", doc_over={"text": other})
    pages["https://ex.com/canon-multi/"] = mkpage("https://ex.com/canon-multi/", doc_over={
        "canonicals": ["https://ex.com/canon-multi/", "https://ex.com/elsewhere/"]})
    pages["https://ex.com/canon-host/"] = mkpage("https://ex.com/canon-host/",
                                                 doc_over={"canonicals": ["https://other.example/x/"]})
    pages["https://ex.com/canon-404/"] = mkpage("https://ex.com/canon-404/",
                                                doc_over={"canonicals": ["https://ex.com/gone/"]})
    pages["https://ex.com/gone/"] = mkpage("https://ex.com/gone/", status=404, doc=None)
    pages["https://ex.com/no-doctype/"] = mkpage("https://ex.com/no-doctype/", doc_over={"doctype": False})
    pages["https://ex.com/no-lang/"] = mkpage("https://ex.com/no-lang/", doc_over={"lang": None})
    pages["https://ex.com/hl-src/"] = mkpage("https://ex.com/hl-src/", doc_over={"hreflang": [
        ["en-gb", "https://ex.com/hl-ok/"], ["bad code", "https://ex.com/"], ["fr", "https://ex.com/gone/"],
        ["en", "https://ex.com/"]]})
    pages["https://ex.com/hl-ok/"] = mkpage("https://ex.com/hl-ok/",
                                            doc_over={"hreflang": [["en-GB", "https://ex.com/hl-src/"]]})
    pages["https://ex.com/bad-jsonld/"] = mkpage("https://ex.com/bad-jsonld/", doc_over={"jsonld_errors": 3})
    pages["https://ex.com/prod/a/"] = mkpage("https://ex.com/prod/a/", doc_over={"jsonld_types": ["Service"]})
    pages["https://ex.com/prod/b/"] = mkpage("https://ex.com/prod/b/", doc_over={"jsonld_types": ["Service"]})
    pages["https://ex.com/prod/c/"] = mkpage("https://ex.com/prod/c/", doc_over={"jsonld_types": []})
    pages["https://ex.com/blog/post/"] = mkpage("https://ex.com/blog/post/", doc_over={"jsonld_types": ["Thing"]})
    pages["https://ex.com/img/"] = mkpage("https://ex.com/img/", doc_over={"images": [
        {"src": "https://ex.com/a.png", "alt": None, "width": None, "height": None, "loading": None},
        {"src": "https://ex.com/b.png", "alt": "", "width": None, "height": None, "loading": None}]})

    dirty = crawl_of(pages)
    findings, passed = run(dirty)
    fired = codes(findings)
    for wanted in ("title_missing", "title_multiple", "title_duplicate", "title_too_long", "description_missing",
                   "description_duplicate", "description_too_long", "h1_missing", "h1_multiple", "h1_duplicate",
                   "thin_content", "near_duplicate_self_canonical", "near_duplicate", "canonical_multiple",
                   "canonical_cross_host", "canonical_non_self", "canonical_to_non_200", "doctype_missing",
                   "lang_missing", "hreflang_errors", "jsonld_invalid", "schema_gaps", "images_missing_alt"):
        chk(wanted in fired, "the dirty crawl must fire %s (got %s)" % (wanted, sorted(set(fired))))

    chk(fired.count("title_duplicate") == 1, "each code appears at most once")
    chk(all(f["urls"] and all(isinstance(u, str) and u for u in f["urls"]) for f in findings),
        "every finding has a non-empty url list of strings")
    chk(all(isinstance(f.get("detail"), str) and f["detail"] for f in findings), "every finding has a detail sentence")
    chk(all(f["detail"].endswith(".") or not f["detail"] for f in findings), "details read as sentences")
    chk(all(len(f["data"].get(k, [])) <= MAX_LIST for f in findings for k in ("pairs", "groups", "examples", "errors",
                                                                             "partial")),
        "data lists stay capped at 20 items")
    chk(json.dumps(findings) is not None, "findings are JSON-serialisable")
    chk("low_indexable_share" not in fired, "a mostly indexable crawl has no share finding")

    by_code = {}
    for finding in findings:
        by_code.setdefault(finding["code"], finding)
    self_dup = by_code.get("near_duplicate_self_canonical", {})
    chk(["https://ex.com/self-a/", "https://ex.com/self-b/", 1.0] in self_dup.get("data", {}).get("pairs", []),
        "a pair where both pages self-canonicalise goes to near_duplicate_self_canonical")
    chk(any(p[0] == "https://ex.com/plain-a/" for p in by_code.get("near_duplicate", {}).get("data", {}).get("pairs", [])),
        "a pair without self canonicals goes to near_duplicate")
    chk(set(self_dup.get("urls", [])) == {"https://ex.com/self-a/", "https://ex.com/self-b/"},
        "self-canonical duplicates list exactly both pages")
    hreflang = by_code.get("hreflang_errors", {}).get("data", {}).get("errors", [])
    reasons = sorted({row[3] for row in hreflang})
    chk("invalid code" in reasons and "no return link" in reasons and any(r.startswith("target status")
                                                                          for r in reasons),
        "hreflang errors cover invalid code, dead target and missing return link")
    schema = by_code.get("schema_gaps", {}).get("data", {})
    chk(["/prod/*/", "Service", 2, 3] in schema.get("partial", []), "partial Service coverage is reported")
    chk("https://ex.com/prod/c/" in by_code.get("schema_gaps", {}).get("urls", []),
        "the page missing the partial type is listed")
    chk("/blog/*/" in schema.get("article_templates", []), "article-style templates without Article schema are listed")
    chk(by_code.get("images_missing_alt", {}).get("data", {}).get("count") == 1,
        "only images without any alt attribute are counted")
    chk("https://ex.com/gone/" not in fired and "https://ex.com/no-title/" in by_code.get("title_missing", {})
        .get("urls", []), "sanity: the no-title page is reported")
    chk([u for u in by_code.get("canonical_to_non_200", {}).get("data", {}).get("pairs", [])
         if u[0] == "https://ex.com/canon-404/"], "a canonical pointing at a crawled 404 is reported")

    # --- low indexable share ----------------------------------------------------------------------------------
    low = {}
    for i in range(10):
        url = "https://ex.com/p%d/" % i
        if i < 4:
            low[url] = mkpage(url)
        elif i < 8:
            low[url] = mkpage(url, doc_over={"robots": ["noindex"]})
        else:
            low[url] = mkpage(url, doc_over={"canonicals": ["https://ex.com/x/"]})
    findings, passed = run(crawl_of(low))
    share_finding = [f for f in findings if f["code"] == "low_indexable_share"]
    chk(len(share_finding) == 1, "a crawl with 40% indexable pages fires low_indexable_share")
    chk(share_finding[0]["data"]["share"] == 0.4, "the reported share is exact")
    chk(share_finding[0]["data"]["reasons"] == {"canonical": 2, "noindex": 4}, "reasons are grouped by first word")
    chk(len(share_finding[0]["urls"]) == 6, "every non-indexable page is listed")

    findings, passed = run(crawl_of(low), thin_words=250, dup_threshold=0.95, max_dup_pages=1)
    chk(all(f["code"] != "near_duplicate" for f in findings), "max_dup_pages limits the duplicate comparison")

    # --- hostile input ---------------------------------------------------------------------------------------
    chk(run({}) == ([], []), "an empty crawl gives no findings and no passes")
    chk(run({"pages": None}) == ([], []), "a non-dict pages value is ignored")
    chk(run({"pages": {"https://ex.com/x/": {"url": "https://ex.com/x/", "doc": None}}}) == ([], []),
        "a crawl of one broken page gives no findings")
    weird = crawl_of({"https://ex.com/w/": {"url": "https://ex.com/w/", "status": 200, "final": "https://ex.com/w/",
                                            "ctype": "text/html",
                                            "doc": {"titles": "oops", "h1": None, "images": [None, {}, "x"],
                                                    "canonicals": [None, 5], "hreflang": [["en"], None, "x"],
                                                    "text": 5, "words": "many", "jsonld_types": ["Thing", None]}}})
    weird_findings, weird_passed = run(weird)
    chk(isinstance(weird_findings, list) and isinstance(weird_passed, list), "hostile document types do not raise")
    chk(all(f["urls"] == ["https://ex.com/w/"] for f in weird_findings), "hostile page still reported sanely")
    chk(run(crawl_of({"https://ex.com/n/": mkpage("https://ex.com/n/", doc_over={"text": None, "images": None,
                                                                                 "hreflang": None})}))[0] != [None],
        "None values in the document are tolerated")
    try:
        run("not a crawl")
        raised = False
    except ToolError:
        raised = True
    chk(raised, "a non-dict crawl raises ToolError")
    try:
        stats([1, 2])
        raised = False
    except ToolError:
        raised = True
    chk(raised, "stats of a non-dict raises ToolError")

    return n
