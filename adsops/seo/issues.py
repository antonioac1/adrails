"""Issue catalogue and prioritised issue table for the AdRails site audit (adsops/seo).

Turns finding dicts (as produced by the checks_* modules) into one row per finding, sorts them by priority,
blast radius and effort, and renders the table as markdown or CSV. Read-only and pure: no network, disk or clock
access here, no global state, every input is passed in, and every output is JSON-serialisable.
"""
import csv
import hashlib
import io

from adsops.errors import ToolError
from adsops.seo.extract import template_of


def _c(priority, hours, issue, recommendation):
    """Build one catalogue entry (keeps the CATALOG literal readable)."""
    return {"priority": priority, "hours": hours, "issue": issue, "recommendation": recommendation}


CATALOG = {
    "robots_blocks_googlebot": _c(
        1, 0.5, "robots.txt blocks Googlebot from pages you want indexed",
        "Edit robots.txt so the Googlebot user-agent group does not Disallow the affected paths, and re-check it "
        "in the robots.txt report in Search Console before deploying"),
    "robots_unreachable": _c(
        2, 0.5, "robots.txt cannot be fetched",
        "Serve /robots.txt with status 200 and content type text/plain; a 5xx response makes Google stop crawling "
        "the whole site"),
    "robots_no_sitemap_line": _c(
        3, 0.25, "robots.txt has no Sitemap line",
        "Add a line 'Sitemap: https://<host>/sitemap.xml' at the end of robots.txt so crawlers find the sitemap "
        "without guessing its URL"),
    "sitemap_missing": _c(
        2, 1, "No usable XML sitemap found",
        "Publish /sitemap.xml listing every canonical, indexable URL, keep it updated on publish, and reference it "
        "from robots.txt"),
    "sitemap_non_indexable": _c(
        1, 1, "Pages in the sitemap that Google cannot index",
        "Remove the listed URLs from the sitemap or make them indexable (status 200, HTML, no noindex, "
        "self-canonical)"),
    "sitemap_missing_indexable": _c(
        2, 0.5, "Indexable pages missing from the sitemap",
        "Add the listed live pages to sitemap.xml and regenerate the sitemap whenever a page is published or "
        "unpublished"),
    "sitemap_orphans": _c(
        2, 1, "Sitemap URLs with no internal links",
        "Link to the listed URLs from navigation or related content, or drop them from the sitemap when they are "
        "not meant for search"),
    "http_not_redirected": _c(
        1, 0.5, "http:// does not redirect to https://",
        "Return 301 from every http:// URL to the same path on https:// at the server or CDN level, and keep it "
        "also for deeper paths, not only the home page"),
    "redirect_chain": _c(
        3, 0.5, "Redirect chains and loops",
        "Point each old URL straight at the final destination with a single 301 so crawlers and users make one hop"),
    "www_not_consolidated": _c(
        2, 0.5, "www and non-www both serve pages",
        "Pick one host, 301 the other to it, and use the chosen host in canonicals, sitemap and internal links"),
    "soft_404": _c(
        2, 1, "Missing pages return 200",
        "Return status 404 (or 410) for unknown URLs instead of rendering an error page with a 200 status"),
    "server_errors": _c(
        1, 2, "Pages return 5xx errors",
        "Fix the server error for the listed URLs and check the application log at the same timestamp for the "
        "stack trace"),
    "low_indexable_share": _c(
        2, 2, "Few crawled pages are indexable",
        "Review the pages that are not indexable, fix the cause it reports (status, noindex, canonical), and stop "
        "linking to pages you do not want indexed"),
    "internal_links_to_redirects": _c(
        3, 1, "Internal links point at redirecting URLs",
        "Change the href of the listed links to the final destination URL so each click and crawl is a single "
        "request"),
    "internal_links_to_errors": _c(
        1, 1, "Internal links point at broken URLs",
        "Update or remove the links that return 4xx or 5xx and point them at a live replacement page"),
    "internal_links_to_http": _c(
        2, 0.5, "Internal links use http://",
        "Rewrite internal hrefs to https:// (or use root-relative links) so no click pays for an extra redirect"),
    "internal_utm_links": _c(
        2, 0.5, "Internal links carry utm_ parameters",
        "Remove utm_ parameters from internal links; tag only links on other sites and ads, because internal UTMs "
        "start a new session and overwrite the real ad/organic source in analytics"),
    "noindex_with_inlinks": _c(
        2, 1, "Noindex pages still receive internal links",
        "Decide per page: remove the noindex tag when the page should rank, otherwise remove the internal links "
        "that spend crawl budget on it"),
    "canonical_to_non_200": _c(
        1, 1, "Canonical URL does not return 200",
        "Point the canonical at the live, indexable version of the page (status 200, same content) or remove the "
        "tag"),
    "canonical_multiple": _c(
        2, 0.5, "Pages declare more than one canonical",
        "Keep exactly one <link rel=\"canonical\"> per page and remove the others, including any added by a plugin "
        "or CMS module"),
    "canonical_cross_host": _c(
        2, 0.5, "Canonical points at a different host",
        "Use a canonical on the same host as the page, or make the target host the only host serving the content "
        "and redirect the others to it"),
    "canonical_non_self": _c(
        3, 0.5, "Canonical points at another page",
        "Check each case: make the page self-canonical when it has its own search value, and keep the cross "
        "canonical only where the page is a true duplicate"),
    "near_duplicate_self_canonical": _c(
        1, 2, "Near-duplicate pages each declare themselves canonical",
        "Group the near-duplicate URLs, choose one version, and make the others canonicalise to it or 301 to it"),
    "near_duplicate": _c(
        3, 2, "Near-duplicate page content",
        "Merge the duplicate pages into one URL, or rewrite each one so it answers a distinct query, and link "
        "internally to the chosen version"),
    "thin_content": _c(
        3, 2, "Pages with very little text",
        "Expand the page with what a visitor needs (prices, areas served, process, answers to common questions) or "
        "merge it into a page that already covers the topic"),
    "title_missing": _c(
        2, 0.5, "Pages without a title tag",
        "Add a unique <title> of about 50-60 characters that names the service and the location"),
    "title_duplicate": _c(
        3, 1, "Duplicate title tags",
        "Give each page a distinct title that matches its own content and target query"),
    "title_too_long": _c(
        4, 0.5, "Titles longer than 60 characters",
        "Shorten titles to about 60 characters, main phrase first, so results do not cut them off"),
    "title_multiple": _c(
        3, 0.25, "Pages with more than one title tag",
        "Keep a single <title> inside <head> and remove the extra one, usually injected by a theme or plugin"),
    "description_missing": _c(
        4, 0.5, "Pages without a meta description",
        "Write a 120-155 character description for each indexable page that summarises what it offers"),
    "description_duplicate": _c(
        4, 1, "Duplicate meta descriptions",
        "Write a description per page that reflects its own content instead of reusing the site-wide one"),
    "description_too_long": _c(
        4, 0.5, "Meta descriptions longer than 160 characters",
        "Trim descriptions to about 155 characters and put the offer first so the full text shows in results"),
    "h1_missing": _c(
        3, 0.5, "Pages without an H1",
        "Add one <h1> per page that states what the page is about, using the same main phrase as the title"),
    "h1_multiple": _c(
        4, 0.5, "More than one H1 per page",
        "Keep one <h1> and turn the other headings into <h2> or styled paragraphs"),
    "h1_duplicate": _c(
        4, 1, "Duplicate H1 text across pages",
        "Rewrite the H1 of each page so it names that page's own topic"),
    "doctype_missing": _c(
        2, 0.25, "Pages missing the doctype",
        "Start every HTML document with <!DOCTYPE html> so browsers render in standards mode"),
    "lang_missing": _c(
        3, 0.25, "<html> without a lang attribute",
        "Add lang=\"en\" (or the correct language code) to the <html> element on every page"),
    "hreflang_errors": _c(
        2, 1, "hreflang annotations are incomplete or wrong",
        "For each language version add self-referencing and return hreflang links plus x-default, using absolute "
        "URLs with valid language-region codes"),
    "deep_pages": _c(
        3, 1, "Important pages sit deep in the click path",
        "Link the listed deep URLs from the home page or main navigation so they are at most three clicks from the "
        "start"),
    "starved_pages": _c(
        2, 2, "Strategic pages with too few internal links",
        "Link the listed pages from the home page, their hub page and two or three related pages, so each has several "
        "internal entry points instead of the few it has now"),
    "hardcoded_sibling_skew": _c(
        2, 2, "A template hard-codes the same sibling links on every page",
        "Replace the fixed sibling links in the template with links to each page's nearest or related siblings, so link "
        "equity is spread instead of piling onto the same few pages"),
    "empty_anchors": _c(
        4, 0.5, "Links with no anchor text",
        "Give every link visible text or an image alt that describes the destination"),
    "generic_anchors": _c(
        4, 0.5, "Links with generic anchor text",
        "Replace 'click here' and 'read more' anchors with text that names the destination page"),
    "nofollow_internal": _c(
        3, 0.5, "Internal links marked nofollow",
        "Remove rel=nofollow from internal links; use robots.txt or noindex when a page must stay out of the index"),
    "schema_gaps": _c(
        3, 2, "Structured data missing for the page type",
        "Add JSON-LD for the page type that applies (LocalBusiness, Service, FAQPage, BreadcrumbList) with the "
        "fields Google requires"),
    "jsonld_invalid": _c(
        2, 0.5, "JSON-LD blocks that do not parse",
        "Fix the JSON syntax of the listed blocks and validate them in the Rich Results Test before deploying"),
    "hsts_missing": _c(
        3, 0.25, "No HSTS header on https responses",
        "Send Strict-Transport-Security: max-age=31536000; includeSubDomains on https responses once every "
        "subdomain supports https"),
    "security_headers_missing": _c(
        4, 0.5, "Missing security headers",
        "Add X-Content-Type-Options: nosniff, Referrer-Policy and a Content-Security-Policy at the server or CDN"),
    "images_missing_alt": _c(
        4, 1, "Images without alt text",
        "Add an alt attribute to each listed image that describes its content, and use an empty alt only for "
        "decorative images"),
    "ad_lp_noindex": _c(
        4, 0.25, "Ad landing pages are noindex (fine for ads; confirm it is deliberate)",
        "No change is needed for the ads themselves (Google Ads does not need the page indexed); confirm the noindex is "
        "deliberate, keep the page out of the sitemap, and do not link to it from organic pages"),
    "ad_lp_non_200": _c(
        1, 0.5, "Ad landing pages do not return 200",
        "Point each ad's final URL at a page that returns 200 directly instead of relying on a redirect from the "
        "ad URL"),
    "ad_lp_canonical_elsewhere": _c(
        2, 0.5, "Ad landing pages canonicalise to another URL",
        "Make each ad landing page self-canonical, or send the ads to the canonical URL so the landing page keeps "
        "its own signals"),
    "ad_lp_duplicates_organic": _c(
        2, 2, "Ad landing pages duplicate organic pages",
        "Use the organic page as the ad destination, or rewrite the landing page with ad-specific content so the "
        "two do not compete"),
    "ad_lp_equity_sink": _c(
        3, 1, "Ad-only pages collect internal links they cannot pass on",
        "Keep ad-only pages out of the navigation and sitemap and link internally to the indexable page instead"),
    "js_only_links": _c(
        2, 2, "Links only exist after JavaScript runs",
        "Render navigation and content links in the HTML response (server-side or pre-rendered) so crawlers see "
        "them without executing JavaScript"),
    "js_content_gap": _c(
        2, 2, "Main content is missing from the HTML",
        "Server-render or pre-render the text that matters so the HTML response carries the content, not only a "
        "script tag"),
    "perf_opportunity": _c(
        3, 2, "Page weight and request count can be reduced",
        "Compress images to WebP or AVIF, self-host and subset fonts, and defer scripts that are not needed for the "
        "first screen"),
    "lcp_slow": _c(
        2, 4, "Largest Contentful Paint is slow",
        "Find the LCP element, preload it, serve it compressed from the same origin, and remove render-blocking "
        "CSS and JS from the critical path"),
    "lcp_preload_mismatch": _c(
        2, 0.5, "Preloaded image is not the LCP image",
        "Preload the image that actually renders as LCP (correct href, imagesrcset and type) and remove preloads "
        "for files that are unused"),
    "gsc_not_indexed": _c(
        1, 1, "Indexable URLs Search Console reports as not indexed",
        "Read the coverage reason Search Console gives for each URL, fix that cause (crawl, canonical, noindex, "
        "soft 404), then request indexing again"),
    "gsc_canonical_mismatch": _c(
        2, 1, "Google picks a different canonical than the page declares",
        "Align the signals: make the declared canonical the URL Google picks, or remove the duplicate versions "
        "that split them"),
}

POLISH = {
    "images_missing_alt", "title_too_long", "description_too_long", "description_missing", "description_duplicate",
    "h1_multiple", "h1_duplicate", "empty_anchors", "generic_anchors", "security_headers_missing",
}

MAX_URLS_PER_ROW = 200
MAX_CELL = 300
CSV_COLUMNS = ("priority", "hours", "issue", "recommendation", "example_url", "pages_affected", "template",
               "status", "code", "detail")


def _unique_urls(raw):
    """-> sorted list of unique non-empty string URLs (hostile input tolerated)."""
    out = []
    seen = set()
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, str):
            continue
        url = item.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    out.sort()
    return out


def _template_of(url):
    """template_of with a safe fallback so one hostile URL cannot break the whole table."""
    try:
        templ = template_of(url)
    except Exception:
        return "unknown"
    return templ if isinstance(templ, str) and templ else "unknown"


def _row_id(code, urls):
    try:
        digest = hashlib.sha1("\n".join(urls).encode("utf-8", "replace")).hexdigest()
    except Exception:
        digest = hashlib.sha1(b"").hexdigest()
    return code + ":" + digest[:8]


def build_table(findings, status="open"):
    """-> list of issue rows, one per finding, sorted by (priority, polish, blast radius, effort, code)."""
    if isinstance(findings, (str, bytes)):
        raise ToolError("findings must be a list of finding dicts")
    rows = []
    if not isinstance(findings, (list, tuple)):
        return rows
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        code = finding.get("code")
        if not isinstance(code, str) or not code:
            code = "unknown"
        data = finding.get("data")
        if not isinstance(data, dict):
            data = {}
        urls = _unique_urls(finding.get("urls"))
        if not urls and data.get("site_level") is not True:
            continue
        detail = finding.get("detail", "")
        if not isinstance(detail, str):
            detail = "" if detail is None else str(detail)
        meta = CATALOG.get(code)
        if isinstance(meta, dict):
            priority = meta.get("priority", 3)
            hours = meta.get("hours", 1)
            issue = meta.get("issue", code)
            recommendation = meta.get("recommendation", "")
        else:
            priority, hours, issue, recommendation = 3, 1, code, detail
        if not isinstance(priority, int) or isinstance(priority, bool):
            priority = 3
        if not isinstance(hours, (int, float)) or isinstance(hours, bool):
            hours = 1
        templ_counts = {}
        for url in urls:
            templ = _template_of(url)
            templ_counts[templ] = templ_counts.get(templ, 0) + 1
        top_template = ""
        if templ_counts:
            top_template = sorted(templ_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rows.append({
            "id": _row_id(code, urls),
            "priority": priority,
            "hours": hours,
            "code": code,
            "issue": issue if isinstance(issue, str) else str(issue),
            "recommendation": recommendation if isinstance(recommendation, str) else str(recommendation),
            "detail": detail,
            "example_url": urls[0] if urls else "",
            "pages_affected": len(urls),
            "templates": templ_counts,
            "template": top_template,
            "urls": urls[:MAX_URLS_PER_ROW],
            "data": data,
            "status": status,
        })
    rows.sort(key=lambda r: (r["priority"], r["code"] in POLISH, -r["pages_affected"], -r["hours"], r["code"]))
    return rows


def summarize(rows):
    """-> totals for the issue table (counts per priority, effort, distinct pages)."""
    out = {"issues": 0, "by_priority": {"1": 0, "2": 0, "3": 0, "4": 0}, "hours_total": 0.0, "pages_with_issues": 0}
    if not isinstance(rows, (list, tuple)):
        return out
    hours = 0.0
    urls = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        out["issues"] += 1
        priority = str(row.get("priority", 3))
        if priority in out["by_priority"]:
            out["by_priority"][priority] += 1
        try:
            hours += float(row.get("hours", 0) or 0)
        except (TypeError, ValueError):
            pass
        for url in row.get("urls", []) or []:
            if isinstance(url, str):
                urls.add(url)
    out["hours_total"] = round(hours, 1)
    out["pages_with_issues"] = len(urls)
    return out


def _cell(value, limit=MAX_CELL):
    """Markdown-safe cell: no pipes, no newlines, capped length."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace("|", "\\|")
    return text[:limit]


def to_markdown(rows, title="Site audit", summary=None):
    """-> markdown document with an optional summary block and the issue table."""
    lines = ["# " + (str(title) if title else "Site audit"), ""]
    if isinstance(summary, dict):
        by_priority = summary.get("by_priority")
        if not isinstance(by_priority, dict):
            by_priority = {}
        lines.append("- Issues: %s (P1: %s, P2: %s, P3: %s, P4: %s)" % (
            _cell(summary.get("issues", 0), 20), _cell(by_priority.get("1", 0), 20),
            _cell(by_priority.get("2", 0), 20), _cell(by_priority.get("3", 0), 20),
            _cell(by_priority.get("4", 0), 20)))
        lines.append("- Estimated fix effort: %s hours" % _cell(summary.get("hours_total", 0), 20))
        lines.append("- Pages with issues: %s" % _cell(summary.get("pages_with_issues", 0), 20))
        lines.append("")
    lines.append("| # | Priority | Hours | Issue | Recommendation | Example URL | Pages | Template | Status |")
    lines.append("|---|----------|-------|-------|----------------|-------------|-------|----------|--------|")
    index = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        index += 1
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            index, _cell(row.get("priority", "")), _cell(row.get("hours", "")),
            _cell(str(row.get("issue", "")) + (": " + str(row["detail"]) if row.get("detail") else ""), 600),
            _cell(row.get("recommendation", "")), _cell(row.get("example_url", "")),
            _cell(row.get("pages_affected", "")), _cell(row.get("template", "")), _cell(row.get("status", ""))))
    lines.append("")
    return "\n".join(lines)


def _csv_cell(value):
    """Neutralise spreadsheet formulas before the CSV writer sees the value."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


def to_csv(rows):
    """-> CSV text with a fixed header; every cell is formula-injection safe."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(list(CSV_COLUMNS))
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        writer.writerow([_csv_cell(row.get(name)) for name in CSV_COLUMNS])
    return buffer.getvalue()


def selftest() -> int:
    """Offline assertions over the catalogue, the table builder and both renderers."""
    n = 0
    codes = [
        "robots_blocks_googlebot", "robots_unreachable", "robots_no_sitemap_line", "sitemap_missing",
        "sitemap_non_indexable", "sitemap_missing_indexable", "sitemap_orphans", "http_not_redirected",
        "redirect_chain", "www_not_consolidated", "soft_404", "server_errors", "low_indexable_share",
        "internal_links_to_redirects", "internal_links_to_errors", "internal_links_to_http", "internal_utm_links",
        "noindex_with_inlinks", "canonical_to_non_200", "canonical_multiple", "canonical_cross_host",
        "canonical_non_self", "near_duplicate_self_canonical", "near_duplicate", "thin_content", "title_missing",
        "title_duplicate", "title_too_long", "title_multiple", "description_missing", "description_duplicate",
        "description_too_long", "h1_missing", "h1_multiple", "h1_duplicate", "doctype_missing", "lang_missing",
        "hreflang_errors", "deep_pages", "starved_pages", "hardcoded_sibling_skew", "empty_anchors",
        "generic_anchors", "nofollow_internal", "schema_gaps", "jsonld_invalid", "hsts_missing",
        "security_headers_missing", "images_missing_alt", "ad_lp_noindex", "ad_lp_non_200",
        "ad_lp_canonical_elsewhere", "ad_lp_duplicates_organic", "ad_lp_equity_sink", "js_only_links",
        "js_content_gap", "perf_opportunity", "lcp_slow", "lcp_preload_mismatch", "gsc_not_indexed",
        "gsc_canonical_mismatch",
    ]
    expected_hours = {"sitemap_orphans": 1, "title_too_long": 0.5, "hsts_missing": 0.25, "lcp_slow": 4,
                      "low_indexable_share": 2, "title_multiple": 0.25, "gsc_not_indexed": 1}
    expected_priority = {"robots_blocks_googlebot": 1, "title_too_long": 4, "description_duplicate": 4,
                         "hsts_missing": 3, "sitemap_orphans": 2, "gsc_canonical_mismatch": 2}

    missing = [c for c in codes if c not in CATALOG]
    assert not missing, "seo.issues.selftest: catalogue misses %r" % (missing,)
    n += 1
    assert len(CATALOG) == len(codes), "seo.issues.selftest: catalogue has extra codes"
    n += 1
    assert all(CATALOG[c]["priority"] in (1, 2, 3, 4) for c in codes), "seo.issues.selftest: priority outside 1..4"
    n += 1
    assert all(expected_priority[c] == CATALOG[c]["priority"] for c in expected_priority), \
        "seo.issues.selftest: priority does not match the spec table"
    n += 1
    assert all(isinstance(CATALOG[c]["hours"], (int, float)) and CATALOG[c]["hours"] > 0 for c in codes), \
        "seo.issues.selftest: hours must be positive numbers"
    n += 1
    assert all(CATALOG[c]["hours"] == expected_hours[c] for c in expected_hours), \
        "seo.issues.selftest: hours do not match the spec table"
    n += 1
    assert all(isinstance(CATALOG[c]["issue"], str) and CATALOG[c]["issue"] for c in codes), \
        "seo.issues.selftest: empty issue title"
    n += 1
    assert all(isinstance(CATALOG[c]["recommendation"], str) and len(CATALOG[c]["recommendation"]) > 20
               for c in codes), "seo.issues.selftest: recommendation too short"
    n += 1
    assert all("hard cap" not in (CATALOG[c]["issue"] + " " + CATALOG[c]["recommendation"]).lower() for c in codes), \
        "seo.issues.selftest: forbidden phrase in catalogue"
    n += 1
    assert POLISH <= set(CATALOG), "seo.issues.selftest: POLISH holds unknown codes"
    n += 1

    def finding(code, urls, detail="detail", data=None):
        return {"code": code, "urls": list(urls), "detail": detail, "data": data or {}}

    assert build_table([]) == [], "seo.issues.selftest: empty findings must give an empty table"
    n += 1
    empty_summary = summarize([])
    assert empty_summary == {"issues": 0, "by_priority": {"1": 0, "2": 0, "3": 0, "4": 0}, "hours_total": 0.0,
                             "pages_with_issues": 0}, "seo.issues.selftest: zero summary"
    n += 1

    rows = build_table([
        finding("title_missing", ["https://x.com/a/"]),
        finding("server_errors", ["https://x.com/b/"]),
        finding("title_too_long", ["https://x.com/c/", "https://x.com/d/", "https://x.com/e/"]),
        finding("thin_content", ["https://x.com/f/"]),
    ])
    assert rows[0]["code"] == "server_errors", "seo.issues.selftest: priority 1 must sort first"
    n += 1
    assert [r["code"] for r in rows].index("server_errors") < [r["code"] for r in rows].index("title_missing"), \
        "seo.issues.selftest: priority 1 before priority 2"
    n += 1
    assert rows[-1]["code"] == "title_too_long", "seo.issues.selftest: polish rows must sort last"
    n += 1

    many = build_table([finding("title_missing", ["https://x.com/a/"]),
                        finding("hreflang_errors", ["https://x.com/b/", "https://x.com/c/", "https://x.com/d/"])])
    assert many[0]["code"] == "hreflang_errors", "seo.issues.selftest: more pages affected must sort first"
    n += 1

    one = build_table([finding("thin_content", ["https://x.com/a/", "https://x.com/b/"]), ])[0]
    two = build_table([finding("thin_content", ["https://x.com/b/", "https://x.com/a/"])])[0]
    assert one["id"] == two["id"], "seo.issues.selftest: id must not depend on url order"
    n += 1
    assert one["id"].startswith("thin_content:") and len(one["id"].split(":")[1]) == 8, \
        "seo.issues.selftest: id must be code plus 8 hex chars"
    n += 1
    assert int(one["id"].split(":")[1], 16) >= 0, "seo.issues.selftest: id suffix must be hex"
    n += 1
    assert one["urls"] == ["https://x.com/a/", "https://x.com/b/"], "seo.issues.selftest: urls must be sorted"
    n += 1

    templ = build_table([finding("thin_content", ["https://x.com/", "https://x.com/a/b/", "https://x.com/a/c/"])])[0]
    assert templ["templates"] == {"home": 1, "/a/*/": 2}, "seo.issues.selftest: templates counted wrong"
    n += 1
    assert templ["template"] == "/a/*/", "seo.issues.selftest: most common template must win"
    n += 1
    tie = build_table([finding("thin_content", ["https://x.com/", "https://x.com/faq/"])])[0]
    assert tie["template"] == "/faq/", "seo.issues.selftest: template ties must break alphabetically"
    n += 1
    assert templ["pages_affected"] == 3, "seo.issues.selftest: pages_affected counts unique urls"
    n += 1

    dup = build_table([finding("thin_content", ["https://x.com/a/", "https://x.com/a/", "https://x.com/a/"])])[0]
    assert dup["pages_affected"] == 1 and dup["urls"] == ["https://x.com/a/"], \
        "seo.issues.selftest: duplicate urls must be collapsed"
    n += 1

    big = build_table([finding("thin_content", ["https://x.com/p%d/" % i for i in range(300)])])[0]
    assert len(big["urls"]) == 200 and big["pages_affected"] == 300, \
        "seo.issues.selftest: row urls capped at 200 but pages_affected must stay complete"
    n += 1
    assert big["example_url"] == "https://x.com/p0/" or big["example_url"] == "https://x.com/p1/", \
        "seo.issues.selftest: example_url must come from the row urls"
    n += 1

    unknown = build_table([finding("some_new_code", ["https://x.com/a/"], detail="the detail text")])[0]
    assert (unknown["priority"], unknown["hours"], unknown["issue"], unknown["recommendation"]) == \
        (3, 1, "some_new_code", "the detail text"), "seo.issues.selftest: unknown code default is wrong"
    n += 1

    assert build_table([finding("hsts_missing", [])]) == [], \
        "seo.issues.selftest: findings without urls must be skipped"
    n += 1
    site_level = build_table([finding("hsts_missing", [], data={"site_level": True})])
    assert len(site_level) == 1 and site_level[0]["example_url"] == "", \
        "seo.issues.selftest: site_level findings must be kept"
    n += 1

    hostile = build_table([
        None, "not-a-dict", 42, {}, {"code": None, "urls": "https://x.com/"},
        {"code": "thin_content", "urls": None, "data": None, "detail": None},
        {"code": "thin_content", "urls": [None, "", "  ", 5, "https://x.com/a/"]},
    ])
    assert len(hostile) == 1 and hostile[0]["urls"] == ["https://x.com/a/"], \
        "seo.issues.selftest: hostile input must not crash or leak junk urls"
    n += 1
    assert build_table(None) == [] and build_table(()) == [], \
        "seo.issues.selftest: non-list findings must give an empty table"
    n += 1
    try:
        build_table("not a list")
        raise AssertionError("seo.issues.selftest: build_table must reject a bare string")
    except ToolError:
        pass
    n += 1

    summary = summarize(rows)
    assert summary["issues"] == 4 and summary["by_priority"]["1"] == 1 and summary["by_priority"]["4"] == 1, \
        "seo.issues.selftest: summary counts are wrong"
    n += 1
    assert summary["pages_with_issues"] == 6, "seo.issues.selftest: summary must count unique pages"
    n += 1
    assert summary["hours_total"] == 5.0, "seo.issues.selftest: hours_total must sum the row hours"
    n += 1
    assert round(sum(0.1 for _ in range(3)), 1) == 0.3 and summarize([{"priority": "1", "hours": 0.1}])[
        "hours_total"] == 0.1, "seo.issues.selftest: hours_total must be rounded to one decimal"
    n += 1
    mixed = summarize([{"priority": 1, "hours": 0.25, "urls": ["https://x.com/a/"]},
                       {"priority": 2, "hours": 0.25, "urls": ["https://x.com/a/", "https://x.com/b/"]}])
    assert mixed["hours_total"] == 0.5 and mixed["pages_with_issues"] == 2, \
        "seo.issues.selftest: summary must sum hours and dedupe pages"
    n += 1

    pipe = build_table([finding("thin_content", ["https://x.com/a/"], detail="a | b")])
    md = to_markdown(pipe, title="Audit", summary=summarize(pipe))
    assert md.startswith("# Audit"), "seo.issues.selftest: markdown title missing"
    n += 1
    assert "| # | Priority | Hours | Issue | Recommendation | Example URL | Pages | Template | Status |" in md, \
        "seo.issues.selftest: markdown header must match the spec"
    n += 1
    table_lines = [ln for ln in md.split("\n") if ln.startswith("|")]
    widths = {ln.replace("\\|", "").count("|") for ln in table_lines}
    assert widths == {10}, "seo.issues.selftest: every table line must keep nine columns"
    n += 1
    assert to_markdown([{"priority": 1, "hours": 0.5, "issue": "x|y", "recommendation": "r|s",
                                 "example_url": "u", "pages_affected": 1, "template": "home",
                                 "status": "open"}]).count("x\\|y") == 1, \
        "seo.issues.selftest: markdown must escape pipes in cells"
    n += 1
    long_row = {"priority": 1, "hours": 1, "issue": "x", "recommendation": "i" * 400, "example_url": "u",
                "pages_affected": 1, "template": "home", "status": "open"}
    long_md = to_markdown([long_row])
    assert "i" * 301 not in long_md and "i" * 300 in long_md, "seo.issues.selftest: markdown cells capped at 300"
    n += 1
    newline_row = {"priority": 1, "hours": 1, "issue": "line\nbreak", "recommendation": "a\r\nb", "example_url": "u",
                   "pages_affected": 1, "template": "home", "status": "open"}
    newline_md = to_markdown([newline_row])
    assert "line break" in newline_md and "a b" in newline_md and "line\nbreak" not in newline_md, \
        "seo.issues.selftest: newlines must become spaces"
    n += 1
    assert to_markdown([]).rstrip().endswith("|--------|"), "seo.issues.selftest: empty table must still render"
    n += 1

    csv_text = to_csv(build_table([finding("thin_content", ["https://x.com/a/"], detail="=cmd()")]))
    header = csv_text.split("\n")[0]
    assert header == "priority,hours,issue,recommendation,example_url,pages_affected,template,status,code,detail", \
        "seo.issues.selftest: csv header must match the spec"
    n += 1
    assert "'=cmd()" in csv_text and '"=cmd()"' not in csv_text, \
        "seo.issues.selftest: csv formula injection must be neutralised"
    n += 1
    inject = to_csv([{"priority": 1, "hours": 1, "issue": "+SUM(A1)", "recommendation": "@x", "example_url": "-1",
                      "pages_affected": 1, "template": "home", "status": "\tbad", "code": "thin_content",
                      "detail": "ok"}])
    assert "'+SUM(A1)" in inject and "'@x" in inject and "'-1" in inject and "'\tbad" in inject, \
        "seo.issues.selftest: every formula prefix must be neutralised"
    n += 1
    quoted = to_csv([{"priority": 1, "hours": 1, "issue": "a,b", "recommendation": "r", "example_url": "u",
                      "pages_affected": 1, "template": "home", "status": "open", "code": "thin_content",
                      "detail": "d"}])
    assert '"a,b"' in quoted, "seo.issues.selftest: commas must be quoted"
    n += 1
    assert to_csv([]).strip() == header, "seo.issues.selftest: empty csv must still carry the header"
    n += 1
    assert "None" not in to_csv([{"priority": None}]), "seo.issues.selftest: None cells must render empty"
    n += 1
    return n
