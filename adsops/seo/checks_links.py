"""Internal-link checks for the AdRails site audit (adsops/seo): link graph and PageRank, redirect/error/http/utm
link targets, noindex link equity, anchor quality, nofollow internals, click depth and hardcoded sibling linking.

Read-only and pure: no network, disk or clock access, no global mutable state, no I/O at import time. Everything is
passed in (the crawl result dict described in SPEC-B) and everything returned is JSON-serialisable
(dicts/lists/str/int/float/bool/None). Nothing here raises on hostile data: malformed edge rows, missing keys,
non-dict pages and empty crawls all degrade to defaults. `resolve()` and `graph()` are the two pure helpers other
modules may import; `run()` returns (findings, passed) where `passed` names the checks that ran and found nothing.

Findings follow the shared contract: {"code", "urls", "detail", "data"} with a non-empty url list (the SOURCE pages
for link problems, targets in data), one plain-English sentence and a small JSON-safe data dict (lists capped at 20).
"""
import bisect
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from adsops.errors import ToolError
from adsops.seo.extract import strip_tracking, template_of
from adsops.seo.checks_content import is_indexable, html_pages

MAX_LIST = 20
MAX_URLS = 2000
MAX_STR = 500
DAMPING = 0.85
PR_ITERATIONS = 50
DEEP_DEPTH = 3
DEFAULT_STARVED = 3
DEFAULT_SKEW_SHARE = 0.5     # acceptance 2026-11-10: Wesley Chapel was hard-coded on 13 of 23 Baytown city pages (59%)
SKEW_MEDIAN_MAX = 0.2
MIN_TEMPLATE_PAGES = 4
HUB_MIN_CHILDREN = 2
REDIRECT_CODES = (301, 302, 303, 307, 308)
GENERIC_ANCHORS = frozenset({"click here", "here", "read more", "learn more", "more", "details", "view", "this",
                             "link", "go"})
_TRIM_RX = re.compile(r"^[\s»›→>…\.\!\?,:;\-–—]+|[\s»›→>…\.\!\?,:;\-–—]+$")


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


def _int(value, default=0):
    """`value` if it is a real int (bools excluded), else `default`."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _float(value, default):
    """`value` if it is a real int/float (bools excluded), else `default`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _list(value):
    """`value` if it is a list/tuple, else an empty list."""
    return value if isinstance(value, (list, tuple)) else []


def _pages(crawl):
    """-> {url: page} from the crawl result, {} when missing or of the wrong type."""
    pages = crawl.get("pages") if isinstance(crawl, dict) else None
    return pages if isinstance(pages, dict) else {}


def _page(pages, url):
    """Page dict stored under `url` (None when absent or not a dict)."""
    page = pages.get(url) if isinstance(url, str) else None
    return page if isinstance(page, dict) else None


def _url(page, fallback=""):
    """Requested URL of `page` (`fallback` when absent)."""
    value = page.get("url") if isinstance(page, dict) else None
    return value if isinstance(value, str) and value else fallback


def _path(url):
    """Path part of `url` ('/' when empty, '' when unparsable)."""
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return ""


def _host(url):
    """Lowercased hostname of `url` ('' when unparsable)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


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
    """First url for an 'e.g. ...' clause ('(none)' when empty)."""
    items = [u for u in urls if u]
    return items[0] if items else "(none)"


def _first_pair(rows):
    """First [src, dst] row rendered as 'src -> dst' ('(none)' when empty)."""
    return "%s -> %s" % (rows[0][0], rows[0][1]) if rows else "(none)"


def _first_triple(rows):
    """First [a, b, c] row rendered as 'a -> b (c)' ('(none)' when empty)."""
    return "%s -> %s (%s)" % (rows[0][0], rows[0][1], rows[0][2]) if rows else "(none)"


def _median(values):
    """Median of a list of numbers (0.0 when empty)."""
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def _rel_tokens(rel):
    """Lowercased rel tokens of an edge ('nofollow noopener' -> {'nofollow', 'noopener'})."""
    return set(_text(rel, 200).lower().split())


def _edge_rows(crawl):
    """-> [(src, dst, anchor, rel, in_nav)] of the crawl's edges, malformed rows skipped and fragments dropped."""
    rows = []
    raw = crawl.get("edges") if isinstance(crawl, dict) else None
    for row in _list(raw):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        src, dst = row[0], row[1]
        if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
            continue
        anchor = row[2] if len(row) > 2 and isinstance(row[2], str) else ""
        rel = row[3] if len(row) > 3 and isinstance(row[3], str) else ""
        in_nav = bool(row[4]) if len(row) > 4 else False
        rows.append((src.split("#")[0] or src, dst.split("#")[0] or dst, anchor, rel, in_nav))
    return rows


def _start_url(crawl):
    """settings.start_url of the crawl ('' when absent)."""
    settings = crawl.get("settings") if isinstance(crawl, dict) else None
    value = settings.get("start_url") if isinstance(settings, dict) else None
    return value if isinstance(value, str) else ""


def _generic_key(anchor):
    """Lowercased anchor with leading/trailing arrows and punctuation trimmed ('Read more »' -> 'read more')."""
    return _TRIM_RX.sub("", _text(anchor, 200).lower()).strip()


def _is_noindex(page):
    """True when the page document robots or the X-Robots-Tag header says noindex."""
    if not isinstance(page, dict):
        return False
    doc = page.get("doc") if isinstance(page.get("doc"), dict) else {}
    for value in _list(doc.get("robots")):
        if "noindex" in _text(value, 200).lower():
            return True
    headers = page.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if _text(key, 100).lower() == "x-robots-tag" and "noindex" in _text(value, 200).lower():
                return True
    return False


def _first_status(page, url):
    """Status of the first hop of `page` (falls back to the page status)."""
    chain = _list(page.get("chain"))
    if chain and isinstance(chain[0], (list, tuple)) and len(chain[0]) > 1:
        status = _int(chain[0][1], -1)
        if status >= 0:
            return status
    return _int(page.get("status"), 0)


def _bare(url):
    """`url` without query string or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("?")[0].split("#")[0]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _has_utm(url):
    """True when the query string of `url` carries any utm_* parameter."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return False
    for key, _value in parse_qsl(query, keep_blank_values=True):
        if key.lower().startswith("utm_"):
            return True
    return False


# ---------------------------------------------------------------------------------------------------------------
# public helpers: redirect resolution and the link graph
# ---------------------------------------------------------------------------------------------------------------
def resolve(crawl, url):
    """-> the final URL of `url`: the crawled redirect target when `url` is a crawled page that redirects, else `url`.

    Redirect chains are followed to their end (with cycle protection); targets that were never crawled are not used.
    """
    if not isinstance(url, str):
        raise ToolError("checks_links.resolve: url must be a string")
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("checks_links.resolve: crawl must be a dict")
    pages = _pages(crawl)
    current = url.split("#")[0] or url
    if current not in pages:                       # the crawler queues URLs without tracking parameters
        stripped = strip_tracking(current)
        if stripped in pages:
            current = stripped
    seen = {current}
    while True:
        page = _page(pages, current)
        if page is None:
            return current
        final = page.get("final")
        if not isinstance(final, str) or not final or final == current or final in seen or final not in pages:
            return current
        seen.add(current)
        current = final


def _pagerank(nodes, out, damping=DAMPING, iterations=PR_ITERATIONS):
    """-> {url: score} with scores summing to ~1; dangling nodes spread their weight uniformly."""
    count = len(nodes)
    if count == 0:
        return {}
    rank = {url: 1.0 / count for url in nodes}
    for _ in range(iterations):
        dangling = sum(rank[url] for url in nodes if not out.get(url))
        base = (1.0 - damping) / count + damping * dangling / count
        new = {url: base for url in nodes}
        for url in nodes:
            targets = out.get(url) or []
            if not targets:
                continue
            share = damping * rank[url] / len(targets)
            for target in targets:
                new[target] = new.get(target, base) + share
        rank = new
    total = sum(rank.values())
    if total <= 0:
        return {url: round(1.0 / count, 6) for url in nodes}
    return {url: round(rank[url] / total, 6) for url in nodes}


def graph(crawl):
    """-> {'nodes', 'inlinks', 'pagerank', 'out'} for the internal link graph of one crawl result.

    Only HTML pages are nodes. Edges are (src, resolve(dst)) with both ends inside the node set, src != dst, no
    nofollow token and de-duplicated. `inlinks` counts distinct source pages (never the page itself); `out` lists
    each node's resolved targets sorted for determinism; PageRank uses damping 0.85 over 50 iterations.
    """
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("checks_links.graph: crawl must be a dict")
    crawl = crawl if isinstance(crawl, dict) else {}
    nodes, node_set = [], set()
    for page in html_pages(crawl):
        url = _url(page)
        if url and url not in node_set:
            nodes.append(url)
            node_set.add(url)
    pairs = []
    seen = set()
    for src, dst, _anchor, rel, _in_nav in _edge_rows(crawl):
        if src not in node_set:
            continue
        target = resolve(crawl, dst)
        if target not in node_set or target == src:
            continue
        if "nofollow" in _rel_tokens(rel):
            continue
        if (src, target) in seen:
            continue
        seen.add((src, target))
        pairs.append((src, target))
    out = {url: [] for url in nodes}
    inlinks = {url: 0 for url in nodes}
    for src, target in pairs:
        out[src].append(target)
        inlinks[target] += 1
    for url in nodes:
        out[url] = sorted(out[url])
    return {"nodes": nodes, "inlinks": inlinks, "pagerank": _pagerank(nodes, out), "out": out}


# ---------------------------------------------------------------------------------------------------------------
# public API: the checks
# ---------------------------------------------------------------------------------------------------------------
def run(crawl, starved_threshold=DEFAULT_STARVED, skew_share=DEFAULT_SKEW_SHARE, max_examples=MAX_LIST):
    """-> (findings, passed) for every internal-link check in SPEC-B, over one crawl result."""
    if crawl is not None and not isinstance(crawl, dict):
        raise ToolError("checks_links.run: crawl must be a dict")
    crawl = crawl if isinstance(crawl, dict) else {}
    cap = _int(max_examples, MAX_LIST)
    cap = MAX_LIST if cap <= 0 else min(cap, MAX_LIST)
    thr = _int(starved_threshold, DEFAULT_STARVED)
    share_limit = _float(skew_share, DEFAULT_SKEW_SHARE)
    pages = _pages(crawl)
    edges = _edge_rows(crawl)
    h_pages = html_pages(crawl)
    idx_pages = [page for page in h_pages if is_indexable(page)]
    start = _start_url(crawl)
    findings, passed = [], []

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

    def settle(code, fired, ran=True):
        """Record `code` in `passed` when it ran and stayed silent."""
        if ran and not fired:
            passed.append(code)

    # shared graph data
    graph_data = graph(crawl)
    inlinks = graph_data.get("inlinks", {})
    resolved_out = {}
    for src, dst, _anchor, _rel, _in_nav in edges:
        resolved_out.setdefault(src, set()).add(resolve(crawl, dst))

    # --- deep_pages -----------------------------------------------------------------------------------------
    deep = []
    for page in idx_pages:
        url = _url(page)
        depth = page.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int):
            continue
        if depth > DEEP_DEPTH:
            deep.append([url, depth])
    settle("deep_pages", emit(
        "deep_pages", [row[0] for row in deep],
        "%d indexable pages sit deeper than %d clicks from the home page (e.g. %s is %d clicks deep)"
        % (len(deep), DEEP_DEPTH, deep[0][0] if deep else "(none)", deep[0][1] if deep else 0),
        {"count": len(deep), "examples": deep[:cap], "limit": DEEP_DEPTH}),
        ran=any(isinstance(p.get("depth"), int) for p in idx_pages))

    # --- starved_pages --------------------------------------------------------------------------------------
    crawled_urls = _uniq([key for key in pages.keys() if isinstance(key, str)])
    sorted_paths = sorted(set(_path(url) for url in crawled_urls))

    below = {}                                    # one pass: every crawled path credits each of its parent folders
    for path in sorted_paths:
        cut = path.rstrip("/").rfind("/")
        while cut > 0:
            below[path[:cut + 1]] = below.get(path[:cut + 1], 0) + 1
            cut = path[:cut].rfind("/")

    def children_below(url):
        """Number of distinct crawled paths strictly under the path of `url`."""
        base = _path(url)
        return below.get(base if base.endswith("/") else base + "/", 0) if base else 0

    rows = []
    for page in idx_pages:
        url = _url(page)
        if url == start:
            continue
        count = _int(inlinks.get(url), 0)
        if count <= thr:
            rows.append([url, count, children_below(url) >= HUB_MIN_CHILDREN])
    rows.sort(key=lambda row: (0 if row[2] else 1, row[1]))
    starved_urls = [row[0] for row in rows]
    hubs = [row for row in rows if row[2]]
    others = [row for row in rows if not row[2]]
    if hubs:
        detail = ("%d hub pages have %d or fewer internal links (%s has %d) and %d other indexable pages are "
                  "equally starved (e.g. %s)") % (len(hubs), thr, hubs[0][0], hubs[0][1], len(others),
                                                  _ex([row[0] for row in others]))
    else:
        detail = "%d indexable pages have %d or fewer internal links (e.g. %s)" % (len(rows), thr, _ex(starved_urls))
    settle("starved_pages", emit(
        "starved_pages", starved_urls, detail,
        {"count": len(rows), "hubs": len(hubs), "threshold": thr,
         "pages": [[row[0], row[1], row[2]] for row in rows[:cap]]}), ran=bool(idx_pages))

    # --- hardcoded_sibling_skew -----------------------------------------------------------------------------
    templates = {}
    for page in idx_pages:
        url = _url(page)
        try:
            name = template_of(url)
        except ToolError:
            continue
        if name in ("home", "asset"):
            continue
        templates.setdefault(name, []).append(url)
    skew_templates = []
    for name in sorted(templates):
        members = _uniq(templates[name])
        total = len(members)
        if total < MIN_TEMPLATE_PAGES:
            continue
        member_set = set(members)
        linked_from = {target: 0 for target in members}
        for src in members:                       # single pass over each member's own links
            for target in resolved_out.get(src, ()):
                if target in member_set and target != src:
                    linked_from[target] += 1
        shares = [linked_from[target] / float(total - 1) for target in members]
        top = [target for target in members
               if linked_from[target] / float(total - 1) >= share_limit and linked_from[target] >= 3]
        median_share = _median(shares)
        if not top or median_share > SKEW_MEDIAN_MAX:
            continue
        sources = []
        for src in members:
            targets = resolved_out.get(src, ())
            if any(target in targets for target in top):
                sources.append(src)
        skew_templates.append({"template": name, "pages": total, "median_share": round(median_share, 4),
                               "max_share": round(max(shares), 4),
                               "targets": [[target, linked_from[target], total] for target in top],
                               "sources": sources})
    skew_templates.sort(key=lambda row: (-row["max_share"], -row["pages"], row["template"]))
    skew_urls = [url for row in skew_templates for url in row["sources"]]
    if skew_templates:
        first = skew_templates[0]
        top_target = first["targets"][0]
        detail = ("%d of %d %s pages link to %s while the median sibling is linked from %d%% of them"
                  % (top_target[1], first["pages"], first["template"], top_target[0], int(round(first["median_share"] * 100))))
        if len(skew_templates) > 1:
            detail = "%d templates hard-code the same sibling links: %s" % (len(skew_templates), "; ".join(
                "%d of %d %s pages link to %s" % (row["targets"][0][1], row["pages"], row["template"], row["targets"][0][0])
                for row in skew_templates[:4]))
    else:
        detail = ""
    settle("hardcoded_sibling_skew", emit(
        "hardcoded_sibling_skew", skew_urls, detail,
        {"template": skew_templates[0]["template"] if skew_templates else "",
         "targets": skew_templates[0]["targets"][:cap] if skew_templates else [],
         "median_share": skew_templates[0]["median_share"] if skew_templates else 0.0,
         "templates": [{k: v for k, v in row.items() if k != "sources"} for row in skew_templates[:cap]]}),
        ran=bool(skew_templates) or any(len(_uniq(v)) >= MIN_TEMPLATE_PAGES for v in templates.values()))

    # --- internal_links_to_redirects --------------------------------------------------------------------------
    settings = crawl.get("settings") if isinstance(crawl.get("settings"), dict) else {}
    scope_hosts = {h.lower() for h in _list(settings.get("scope_hosts")) if isinstance(h, str)}
    redirect_rows = []
    for src, dst, _anchor, _rel, _in_nav in edges:
        page = _page(pages, dst)
        if page is None:
            continue
        final = page.get("final") if isinstance(page.get("final"), str) else ""
        if not final or final == dst:
            continue
        if scope_hosts and _host(final) not in scope_hosts:
            continue                                   # a deliberate hop to another site (portal, app): not a crawl-waste issue
        if _first_status(page, dst) in REDIRECT_CODES:
            redirect_rows.append([src, dst, final])
    settle("internal_links_to_redirects", emit(
        "internal_links_to_redirects", [row[0] for row in redirect_rows],
        "%d internal links on %d pages point at URLs that redirect (e.g. %s)"
        % (len(redirect_rows), len(_uniq([row[0] for row in redirect_rows])), _first_triple(redirect_rows)),
        {"count": len(redirect_rows), "examples": redirect_rows[:cap],
         "targets": _uniq([row[1] for row in redirect_rows], cap)}), ran=bool(edges))

    # --- internal_links_to_errors -----------------------------------------------------------------------------
    error_rows = []
    for src, dst, _anchor, _rel, _in_nav in edges:
        page = _page(pages, dst)
        if page is None:
            continue
        status = _int(page.get("status"), 0)
        error = _text(page.get("error"), 200).lower()
        if status in (404, 410) or status >= 500 or (status == 0 and error and not error.startswith(
                ("refused", "request budget", "crawl deadline", "robots.txt"))):     # 401/403/429 are not "broken"
            error_rows.append([src, dst, status])
    settle("internal_links_to_errors", emit(
        "internal_links_to_errors", [row[0] for row in error_rows],
        "%d internal links on %d pages point at URLs that fail (e.g. %s)"
        % (len(error_rows), len(_uniq([row[0] for row in error_rows])), _first_triple(error_rows)),
        {"count": len(error_rows), "examples": error_rows[:cap],
         "targets": _uniq([row[1] for row in error_rows], cap)}), ran=bool(edges))

    # --- internal_links_to_http -------------------------------------------------------------------------------
    https_home = start.lower().startswith("https://")
    http_rows = [[src, dst] for src, dst, _anchor, _rel, _in_nav in edges if https_home and dst.lower().startswith("http://")]
    settle("internal_links_to_http", emit(
        "internal_links_to_http", [row[0] for row in http_rows],
        "%d internal links on %d pages use plain http:// instead of https:// (e.g. %s)"
        % (len(http_rows), len(_uniq([row[0] for row in http_rows])), _first_pair(http_rows)),
        {"count": len(http_rows), "examples": http_rows[:cap]}), ran=bool(edges) and https_home)

    # --- internal_utm_links -----------------------------------------------------------------------------------
    utm_rows = []
    for src, dst, _anchor, _rel, _in_nav in edges:
        if not _has_utm(dst) and strip_tracking(dst) == dst:     # utm_* or ad click ids (gclid, fbclid, ...)
            continue
        utm_rows.append([src, dst, _bare(dst)])
    target_links = {}
    target_noindex = {}

    def _target_is_noindex(target_dst, bare):
        """True when the crawled page behind this utm link exists and is not indexable."""
        for candidate in (resolve(crawl, target_dst), target_dst, bare, resolve(crawl, bare)):
            page = _page(pages, candidate)
            if page is not None:
                return not is_indexable(page)
        return False

    for _src, dst, bare in utm_rows:
        target_links[bare] = target_links.get(bare, 0) + 1
        if bare not in target_noindex:
            target_noindex[bare] = _target_is_noindex(dst, bare)
    utm_targets = [[bare, count, bool(target_noindex.get(bare))] for bare, count in target_links.items()]
    utm_targets.sort(key=lambda row: (-row[1], row[0]))
    utm_sources = _uniq([row[0] for row in utm_rows])
    if utm_rows:
        lead = utm_targets[0]
        detail = ("%d internal links on %d pages carry utm_ parameters, mostly to %s%s"
                  % (len(utm_rows), len(utm_sources), lead[0], " (noindex)" if lead[2] else ""))
    else:
        detail = ""
    settle("internal_utm_links", emit(
        "internal_utm_links", utm_sources, detail,
        {"links": len(utm_rows), "pages": len(utm_sources), "targets": utm_targets[:cap]}), ran=bool(edges))

    # --- noindex_with_inlinks ---------------------------------------------------------------------------------
    idx_set = set(_url(page) for page in idx_pages)
    indexable_inlinks = {}
    followed = {}
    for src, dst, _anchor, rel, _in_nav in edges:            # nofollow links pass no equity: not counted here
        if src in idx_set and "nofollow" not in _rel_tokens(rel):
            followed.setdefault(src, set()).add(resolve(crawl, dst))
    for src in idx_set:
        for target in followed.get(src, ()):
            if target != src:
                indexable_inlinks[target] = indexable_inlinks.get(target, 0) + 1
    noindex_rows = []
    for page in h_pages:
        url = _url(page)
        if not _is_noindex(page):
            continue
        count = _int(indexable_inlinks.get(url), 0)
        if count >= 1:
            noindex_rows.append([url, count])
    noindex_rows.sort(key=lambda row: (-row[1], row[0]))
    settle("noindex_with_inlinks", emit(
        "noindex_with_inlinks", [row[0] for row in noindex_rows],
        "%d noindex pages still receive links from indexable pages, wasting link equity (e.g. %s from %d pages)"
        % (len(noindex_rows), noindex_rows[0][0] if noindex_rows else "(none)", noindex_rows[0][1] if noindex_rows else 0),
        {"count": len(noindex_rows), "examples": noindex_rows[:cap]}), ran=bool(h_pages))

    # --- empty_anchors / generic_anchors / nofollow_internal --------------------------------------------------
    empty_rows = [[src, dst] for src, dst, anchor, _rel, _in_nav in edges if not _text(anchor, 200)]
    settle("empty_anchors", emit(
        "empty_anchors", [row[0] for row in empty_rows],
        "%d internal links have no anchor text (e.g. %s)" % (len(empty_rows), _first_pair(empty_rows)),
        {"links": len(empty_rows), "count": len(empty_rows),
         "pages": len(_uniq([row[0] for row in empty_rows])), "examples": empty_rows[:cap]}), ran=bool(edges))

    generic_rows = [[src, dst, _text(anchor, 80)] for src, dst, anchor, _rel, _in_nav in edges
                    if _generic_key(anchor) in GENERIC_ANCHORS]
    settle("generic_anchors", emit(
        "generic_anchors", [row[0] for row in generic_rows],
        "%d internal links use a generic anchor such as 'click here' or 'read more' (e.g. %s -> \"%s\")"
        % (len(generic_rows), generic_rows[0][0] if generic_rows else "(none)",
           generic_rows[0][2] if generic_rows else ""),
        {"links": len(generic_rows), "count": len(generic_rows),
         "pages": len(_uniq([row[0] for row in generic_rows])), "examples": generic_rows[:cap]}), ran=bool(edges))

    nofollow_rows = [[src, dst, dst] for src, dst, _anchor, rel, _in_nav in edges
                     if "nofollow" in _rel_tokens(rel)]
    settle("nofollow_internal", emit(
        "nofollow_internal", [row[0] for row in nofollow_rows],
        "%d internal links are marked nofollow, so they pass no equity (e.g. %s)"
        % (len(nofollow_rows), _first_pair(nofollow_rows)),
        {"count": len(nofollow_rows), "targets": _uniq([row[1] for row in nofollow_rows], cap),
         "examples": [row[:2] for row in nofollow_rows[:cap]]}), ran=bool(edges))

    return findings, passed


# ---------------------------------------------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    """Offline assertions over fake crawl dicts; returns the number of checks performed."""
    n = 0

    def chk(cond, msg):
        nonlocal n
        n += 1
        assert cond, "seo.checks_links.selftest: " + msg

    def mkpage(url, depth=1, status=200, final=None, doc_over=None, chain=None, error=None, ctype="text/html"):
        doc = {"doctype": True, "lang": "en", "titles": ["T " + url], "descriptions": ["D " + url], "robots": [],
               "canonicals": [], "h1": ["H " + url], "h2_count": 1, "hreflang": [], "links": [], "jsonld_types": [],
               "jsonld_errors": 0, "text": "some words right here", "words": 4, "preload_images": [], "images": [],
               "video_posters": [], "viewport": True, "og_image": True, "meta_refresh": None}
        if isinstance(doc_over, dict):
            doc.update(doc_over)
        return {"url": url, "chain": list(chain or []), "status": status, "final": url if final is None else final,
                "ctype": ctype, "bytes": 1000, "truncated": False, "ms": 5, "headers": {}, "error": error,
                "depth": depth, "in_sitemap": True, "discovered": "link", "doc": doc}

    def crawl_of(pages, edges=None, start="https://ex.com/"):
        return {"settings": {"start_url": start}, "pages": pages, "edges": list(edges or []), "robots": {},
                "sitemap": {}, "external_links": {}, "probes": {}, "skipped": {}, "notes": []}

    def codes(result):
        return [f["code"] for f in result]

    def by_code(result):
        return {f["code"]: f for f in result}

    home = "https://ex.com/"

    # --- city template with hardcoded sibling skew ------------------------------------------------------------
    cities = ["austin", "dallas", "houston", "tampa", "miami", "wesley"]
    city_pages = {home: mkpage(home)}
    for city in cities:
        city_pages["https://ex.com/city/%s/" % city] = mkpage("https://ex.com/city/%s/" % city)
    skew_edges = [["https://ex.com/city/%s/" % city, "https://ex.com/city/wesley/", "Wesley Chapel", "", False]
                  for city in cities if city != "wesley"]
    out = crawl_of(city_pages, skew_edges)
    findings, passed = run(out)
    found = by_code(findings)
    chk("hardcoded_sibling_skew" in found, "5 of 6 city pages linking to one sibling is skew")
    skew = found.get("hardcoded_sibling_skew", {})
    chk([t[0] for t in skew.get("data", {}).get("targets", [])] == ["https://ex.com/city/wesley/"],
        "skew target is the wesley page")
    chk(skew.get("data", {}).get("targets", [[None, None, None]])[0][1:] == [5, 6], "skew target linked from 5 of 6")
    chk(len(skew.get("urls", [])) == 5, "the five linking pages are the finding URLs")
    chk(skew.get("data", {}).get("median_share") == 0.0, "median sibling share is 0%")
    chk("hardcoded_sibling_skew" not in passed, "skew is not reported as passing")
    chk(all(f["urls"] for f in findings), "no finding has an empty url list")

    # --- hub page starved of links ----------------------------------------------------------------------------
    hub_pages = {home: mkpage(home), "https://ex.com/about/": mkpage("https://ex.com/about/"),
                 "https://ex.com/city/": mkpage("https://ex.com/city/"),
                 "https://ex.com/city/austin/": mkpage("https://ex.com/city/austin/"),
                 "https://ex.com/city/dallas/": mkpage("https://ex.com/city/dallas/")}
    hub_edges = [[home, "https://ex.com/city/", "Cities", "", True],
                 ["https://ex.com/about/", "https://ex.com/city/", "Cities", "", False]]
    findings, passed = run(crawl_of(hub_pages, hub_edges))
    found = by_code(findings)
    starved = found.get("starved_pages", {})
    chk("starved_pages" in found, "pages with 2 inlinks are starved at the default threshold of 3")
    hub_row = [row for row in starved.get("data", {}).get("pages", []) if row[0] == "https://ex.com/city/"]
    chk(hub_row and hub_row[0][1] == 2 and hub_row[0][2] is True, "the /city/ hub is starved and flagged as a hub")
    chk(starved.get("data", {}).get("hubs") == 1, "exactly one starved hub")
    chk(starved.get("urls", [""])[0] == "https://ex.com/city/", "hubs are listed first in urls")
    chk(home not in starved.get("urls", []), "the home page is never reported as starved")
    chk("starved pages" in starved.get("detail", "") or "internal links" in starved.get("detail", ""),
        "starved detail is a sentence")
    findings2, _ = run(crawl_of(hub_pages, hub_edges), starved_threshold=0)
    chk("https://ex.com/city/" not in by_code(findings2).get("starved_pages", {}).get("urls", []),
        "a lower threshold clears the hub")

    # --- utm links to a noindex page --------------------------------------------------------------------------
    utm_pages = {home: mkpage(home),
                 "https://ex.com/owners/": mkpage("https://ex.com/owners/", doc_over={"robots": ["noindex, follow"]})}
    utm_edges = [[home, "https://ex.com/owners/?utm_source=google&utm_medium=cpc", "Owners", "", False],
                 [home, "https://ex.com/owners/?utm_campaign=spring", "", "", False],
                 [home, "https://ex.com/owners/", "Owners", "", True]]
    findings, passed = run(crawl_of(utm_pages, utm_edges))
    found = by_code(findings)
    utm = found.get("internal_utm_links", {})
    chk("internal_utm_links" in found, "utm_ parameters on internal links are flagged")
    chk(utm.get("data", {}).get("links") == 2, "both utm links are counted")
    chk(utm.get("urls") == [home], "the linking page is the finding URL")
    chk(utm.get("data", {}).get("targets", [[None, None, None]])[0] == ["https://ex.com/owners/", 2, True],
        "utm target is stripped of its query and marked noindex")
    chk("(noindex)" in utm.get("detail", ""), "detail mentions the noindex target")
    noin = found.get("noindex_with_inlinks", {})
    chk("noindex_with_inlinks" in found and noin.get("urls") == ["https://ex.com/owners/"],
        "the noindex page linked from an indexable page is flagged")
    chk(noin.get("data", {}).get("examples", [[None, None]])[0] == ["https://ex.com/owners/", 1],
        "one indexable page links to the noindex page (the utm links count once)")

    # --- pagerank --------------------------------------------------------------------------------------------
    pr_pages = {home: mkpage(home), "https://ex.com/a/": mkpage("https://ex.com/a/"),
                "https://ex.com/b/": mkpage("https://ex.com/b/"), "https://ex.com/hub/": mkpage("https://ex.com/hub/")}
    pr_edges = [[home, "https://ex.com/hub/", "Hub", "", False], ["https://ex.com/a/", "https://ex.com/hub/", "H", "", False],
                ["https://ex.com/b/", "https://ex.com/hub/", "H", "", False], ["https://ex.com/hub/", home, "Home", "", False]]
    g = graph(crawl_of(pr_pages, pr_edges))
    chk(len(g["nodes"]) == 4, "every HTML page is a node")
    chk(g["inlinks"]["https://ex.com/hub/"] == 3, "the hub has three distinct source pages")
    chk(g["out"][home] == ["https://ex.com/hub/"], "out lists the resolved targets of a page")
    chk(abs(sum(g["pagerank"].values()) - 1.0) < 1e-3, "pagerank sums to 1")
    chk(max(g["pagerank"], key=lambda u: g["pagerank"][u]) == "https://ex.com/hub/", "the most-linked page ranks highest")
    dangling = graph(crawl_of({home: mkpage(home), "https://ex.com/x/": mkpage("https://ex.com/x/")},
                              [[home, "https://ex.com/x/", "", "", False]]))
    chk(abs(sum(dangling["pagerank"].values()) - 1.0) < 1e-3, "pagerank still sums to 1 with a dangling node")
    chk(dangling["inlinks"]["https://ex.com/x/"] == 1 and dangling["inlinks"][home] == 0,
        "inlinks are counted per target and default to zero")

    # --- redirects, nofollow, duplicate edges ----------------------------------------------------------------
    red_pages = {home: mkpage(home),
                 "https://ex.com/old/": mkpage("https://ex.com/old/", final="https://ex.com/new/",
                                               chain=[["https://ex.com/old/", 301, "https://ex.com/new/"],
                                                      ["https://ex.com/new/", 200, ""]]),
                 "https://ex.com/new/": mkpage("https://ex.com/new/")}
    red_edges = [[home, "https://ex.com/old/", "Old", "", False], [home, "https://ex.com/old/#frag", "Old", "", False],
                 ["https://ex.com/new/", home, "Home", "nofollow", False], ["https://ex.com/new/", home, "Home", "", False]]
    red = crawl_of(red_pages, red_edges)
    chk(resolve(red, "https://ex.com/old/") == "https://ex.com/new/", "resolve follows a redirect")
    chk(resolve(red, "https://ex.com/new/") == "https://ex.com/new/", "resolve keeps a non-redirect URL")
    chk(resolve(red, "https://ex.com/never/") == "https://ex.com/never/", "resolve keeps an uncrawled URL")
    g = graph(red)
    chk("https://ex.com/old/" not in g["nodes"], "redirecting pages are not nodes")
    chk(g["inlinks"]["https://ex.com/new/"] == 1, "inlinks resolve through redirects and de-duplicate edges")
    chk(g["out"]["https://ex.com/new/"] == [home], "a nofollow internal edge is dropped from the graph")
    findings, _ = run(red)
    found = by_code(findings)
    redir = found.get("internal_links_to_redirects", {})
    chk(redir.get("urls") == [home], "links to redirecting URLs flag the source page")
    chk(redir.get("data", {}).get("examples", [[None, None, None]])[0]
        == [home, "https://ex.com/old/", "https://ex.com/new/"], "redirect example lists src, dst and final")
    chk(found.get("nofollow_internal", {}).get("urls") == ["https://ex.com/new/"], "nofollow internals flag the source")

    # --- errors ----------------------------------------------------------------------------------------------
    err_pages = {home: mkpage(home), "https://ex.com/broken/": mkpage("https://ex.com/broken/", status=500),
                 "https://ex.com/refused/": mkpage("https://ex.com/refused/", status=0, error="refused: non-public address"),
                 "https://ex.com/timeout/": mkpage("https://ex.com/timeout/", status=0, error="timed out")}
    err_edges = [[home, "https://ex.com/broken/", "Broken", "", False],
                 [home, "https://ex.com/refused/", "Refused", "", False],
                 [home, "https://ex.com/timeout/", "Timeout", "", False]]
    findings, passed = run(crawl_of(err_pages, err_edges))
    found = by_code(findings)
    errs = found.get("internal_links_to_errors", {})
    chk(errs.get("urls") == [home], "links to failing URLs flag the source page")
    chk(sorted(row[2] for row in errs.get("data", {}).get("examples", [])) == [0, 500],
        "500s and non-refused failures count, refused connections do not")
    chk("https://ex.com/refused/" not in [row[1] for row in errs.get("data", {}).get("examples", [])],
        "a refused connection is not an error link")

    # --- http links, depth, anchors, hostile input ------------------------------------------------------------
    http_pages = {home: mkpage(home), "https://ex.com/deep/": mkpage("https://ex.com/deep/", depth=5),
                  "https://ex.com/far/": mkpage("https://ex.com/far/", depth=None),
                  "https://ex.com/edge/": mkpage("https://ex.com/edge/", depth=3)}
    http_edges = [[home, "http://ex.com/x/", "", "", False], [home, "https://ex.com/edge/", "Click here »", "", False],
                  [home, "https://ex.com/deep/", "Read more", "noopener", False],
                  [home, "https://ex.com/far/", "Learn more.", "", True]]
    findings, passed = run(crawl_of(http_pages, http_edges))
    found = by_code(findings)
    chk(found.get("internal_links_to_http", {}).get("urls") == [home], "plain http internal links are flagged")
    chk(found.get("deep_pages", {}).get("data", {}).get("examples") == [["https://ex.com/deep/", 5]],
        "only pages deeper than 3 clicks are deep (depth None is ignored)")
    chk(sorted(found.get("generic_anchors", {}).get("data", {}).get("examples", [])) ==
        [[home, "https://ex.com/deep/", "Read more"], [home, "https://ex.com/edge/", "Click here »"],
         [home, "https://ex.com/far/", "Learn more."]], "generic anchors are matched after trimming punctuation")
    chk("internal_links_to_http" not in passed, "the http check fired and is not in passed")
    plain = run(crawl_of(http_pages, http_edges, start="http://ex.com/"))
    chk("internal_links_to_http" not in codes(plain[0]) and "internal_links_to_http" not in plain[1],
        "the http check does not run when the site itself is http")

    empty = run(crawl_of({}, []))
    chk(empty[0] == [] and empty[1] == [], "an empty crawl gives no findings and nothing passed")
    chk(graph({}) == {"nodes": [], "inlinks": {}, "pagerank": {}, "out": {}}, "an empty graph is empty")
    hostile = {"settings": "not a dict", "pages": {"https://ex.com/": "not a page", home: mkpage(home)},
               "edges": [None, [1, 2], "x", [home], [home, None], [home, "https://ex.com/4/", None, None],
                         [home, "https://ex.com/4/?utm_x=1", "", "nofollow", None]]}
    h_findings, h_passed = run(hostile)
    chk(isinstance(h_findings, list) and isinstance(h_passed, list), "a hostile crawl does not crash run()")
    chk(all(isinstance(f["urls"], list) and f["urls"] for f in h_findings), "hostile findings still have urls")
    chk(run(None) == ([], []), "a None crawl is treated as empty")
    try:
        resolve({}, None)
        chk(False, "resolve must reject a non-string URL")
    except ToolError:
        chk(True, "resolve raises ToolError on a non-string URL")

    import json
    findings, _ = run(crawl_of(utm_pages, utm_edges))
    chk(isinstance(json.dumps(findings), str), "findings are JSON-serialisable")
    capped, _ = run(crawl_of(city_pages, skew_edges), max_examples=1)
    chk(all(len(v) <= 1 for f in capped for v in f["data"].values() if isinstance(v, list)),
        "data lists honour max_examples")

    return n
