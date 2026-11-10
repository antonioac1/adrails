"""Black-box tests for the adsops.seo site-audit package, written from SPEC-B only.

No network, no sleeps, deterministic. Only the documented public contract is used:
extract.template_of/parse/normalize/strip_tracking, issues.CATALOG/build_table/to_markdown/to_csv,
checks_content/checks_site/checks_links.run + is_indexable, crawl.crawl, runs.RunStore/diff.
"""

import csv
import io
import json
import os
import tempfile
import unittest

from adsops.errors import ToolError
from adsops.seo import checks_content, checks_links, checks_site, crawl, extract, issues, runs

BASE = "https://example.com"
HOST = "example.com"
HTML_CT = "text/html; charset=utf-8"

DOC_KEYS = [
    "doctype", "lang", "titles", "descriptions", "robots", "canonicals", "h1", "h2_count", "hreflang", "links",
    "jsonld_types", "jsonld_errors", "text", "words", "preload_images", "images", "video_posters", "viewport",
    "og_image", "meta_refresh",
]

CRAWL_KEYS = ["settings", "robots", "sitemap", "pages", "edges", "external_links", "probes", "skipped", "notes"]

TABLE_ROW_KEYS = {
    "id", "priority", "hours", "code", "issue", "recommendation", "detail", "example_url", "pages_affected",
    "templates", "template", "urls", "data", "status",
}

LOREM = " ".join("Sentence number %d talks about property management services in Texas." % i for i in range(30))
PLAIN_HTML = '<!doctype html><html lang="en"><head><title>Plain</title></head><body><main><p>%s</p></main></body></html>' % LOREM


def parse_doc(html, url):
    return extract.parse(html, url)


def make_page(url, html=None, status=200, ctype=HTML_CT, headers=None, final=None, sitemap=False, depth=0):
    """Build a Page dict as documented in SPEC 'Crawl result'."""
    if html is None:
        html = PLAIN_HTML
    doc = extract.parse(html, url) if (ctype or "").startswith("text/html") else None
    hdrs = {"content-type": ctype}
    if headers:
        hdrs.update(headers)
    return {
        "url": url,
        "chain": [[url, status, None]],
        "status": status,
        "final": final if final is not None else url,
        "ctype": ctype,
        "bytes": max(1, len(html.encode("utf-8", "replace"))),
        "truncated": False,
        "ms": 5,
        "headers": hdrs,
        "error": None,
        "depth": depth,
        "in_sitemap": sitemap,
        "discovered": "start",
        "doc": doc,
    }


def make_crawl(pages=None, robots_text="", edges=None, probes=None, sitemap_urls=None, notes=None):
    """Build a Crawl result dict as documented in SPEC 'Crawl result'."""
    return {
        "settings": {
            "start_url": BASE + "/",
            "user_agent": "AdRailsAudit/1.0",
            "max_pages": 50,
            "rate_limit_s": 0.0,
            "scope_hosts": [HOST, "www." + HOST],
            "respect_robots": True,
            "timeout_s": 10,
            "max_bytes": 5000000,
            "sitemap_seeding": True,
            "started_at": "2024-01-01T00:00:00Z",
            "finished_at": "2024-01-01T00:01:00Z",
        },
        "robots": {"url": BASE + "/robots.txt", "status": 200 if robots_text else 404, "text": robots_text, "sitemaps": []},
        "sitemap": {
            "urls": list(sitemap_urls or []),
            "lastmod": {},
            "sources": [],
            "errors": [],
            "truncated": False,
        },
        "pages": dict(pages or {}),
        "edges": list(edges or []),
        "external_links": {},
        "probes": probes if probes is not None else {
            "http_home": [[BASE.replace("https", "http") + "/", 200, None]],
            "www_home": [],
            "not_found": {"url": BASE + "/adrails-404-check-deadbeef/", "status": 404},
        },
        "skipped": {"robots_disallowed": [], "off_scope": 0, "cap_reached": False, "non_html": 0},
        "notes": list(notes or []),
    }


def codes_of(findings):
    return [f.get("code") for f in findings]


def resp(url, status=200, body="", ctype=HTML_CT, headers=None, location=None):
    hdrs = {"content-type": ctype}
    if headers:
        hdrs.update(headers)
    if location is not None:
        hdrs["location"] = location
    return {
        "url": url,
        "status": status,
        "headers": hdrs,
        "body": body,
        "bytes": len(body.encode("utf-8", "replace")),
        "truncated": False,
        "ms": 1,
        "ctype": ctype,
        "error": None,
    }


class FakeSite:
    """Deterministic in-memory site; records every URL the crawler asks for."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return resp(url, 404, '<!doctype html><html lang="en"><head><title>404</title></head>'
                                   '<body><p>not found</p></body></html>')
        return route(url) if callable(route) else dict(route)

    def fetched(self, url):
        return any(c == url for c in self.calls)


def no_sleep(_seconds):
    return None


def fixed_now():
    return 1735689600.0


# ---------------------------------------------------------------- template_of


class TestTemplateOf(unittest.TestCase):
    def test_home_variants(self):
        self.assertEqual(extract.template_of(BASE), "home")
        self.assertEqual(extract.template_of(BASE + "/"), "home")
        self.assertEqual(extract.template_of(BASE + "/?utm_source=x"), "home")

    def test_one_segment(self):
        self.assertEqual(extract.template_of(BASE + "/faq/"), "/faq/")
        self.assertEqual(extract.template_of(BASE + "/owners"), "/owners/")

    def test_two_segments(self):
        self.assertEqual(extract.template_of(BASE + "/property-management/houston"), "/property-management/*/")

    def test_three_or_more_segments_collapse(self):
        self.assertEqual(extract.template_of(BASE + "/a/b/c"), "/a/*/*/")
        self.assertEqual(extract.template_of(BASE + "/a/b/c/d/e"), "/a/*/*/")

    def test_assets(self):
        for ext in ("pdf", "xml", "txt", "png", "jpg", "webp", "gif", "svg", "js", "css", "json", "ico", "zip", "mp4"):
            self.assertEqual(extract.template_of(BASE + "/files/thing." + ext), "asset", "ext %s" % ext)
        self.assertEqual(extract.template_of(BASE + "/a/b.pdf?x=1"), "asset")

    def test_numeric_and_date_segments(self):
        self.assertEqual(extract.template_of(BASE + "/12345/"), "/{n}/")
        self.assertEqual(extract.template_of(BASE + "/2024/"), "/{n}/")
        self.assertEqual(extract.template_of(BASE + "/2024-05/faq"), "/{n}/*/")
        self.assertEqual(extract.template_of(BASE + "/blog/2024/03/hello"), "/blog/*/*/")

    def test_query_ignored(self):
        self.assertEqual(extract.template_of(BASE + "/faq/?a=1&b=2"), "/faq/")
        self.assertEqual(extract.template_of(BASE + "/owners?ref=nav"), "/owners/")

    def test_collapses_trailing_slashes(self):
        self.assertEqual(extract.template_of(BASE + "/faq//"), "/faq/")
        self.assertEqual(extract.template_of(BASE + "/faq///"), "/faq/")


class TestNormalizeAndTracking(unittest.TestCase):
    def test_normalize_relative_against_base(self):
        self.assertEqual(extract.normalize("/a/b", BASE + "/x/y"), BASE + "/a/b")
        self.assertEqual(extract.normalize("c", BASE + "/a/b"), BASE + "/a/c")

    def test_normalize_non_http_is_empty(self):
        for bad in ("mailto:a@b.com", "tel:+15551212", "javascript:void(0)", "data:text/html,x", "sms:123"):
            self.assertEqual(extract.normalize(bad), "", bad)

    def test_strip_tracking_removes_only_tracking_params(self):
        url = BASE + "/a?utm_source=x&utm_medium=y&utm_campaign=z&gclid=1&fbclid=2&msclkid=3&keep=1"
        out = extract.strip_tracking(url)
        for gone in ("utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid", "msclkid"):
            self.assertNotIn(gone, out, gone)
        self.assertIn("keep=1", out)


# --------------------------------------------------------------------- parse


class TestParseContract(unittest.TestCase):
    def test_every_documented_key_is_present_and_serialisable(self):
        doc = parse_doc(PLAIN_HTML, BASE + "/")
        self.assertIsInstance(doc, dict)
        for key in DOC_KEYS:
            self.assertIn(key, doc, "missing PageDocument key %s" % key)
        json.dumps(doc)

    def test_doctype_and_lang(self):
        self.assertTrue(parse_doc('<!DOCTYPE html><html lang=" en-GB "></html>', BASE + "/")["doctype"])
        self.assertIsNone(parse_doc("<html></html>", BASE + "/")["lang"])
        self.assertIsNone(parse_doc('<html lang=""></html>', BASE + "/")["lang"])
        self.assertEqual(parse_doc('<html lang=" en-GB "></html>', BASE + "/")["lang"], "en-GB")

    def test_titles_collapsed_multi_and_svg_ignored(self):
        html = ('<html><head><title>  A\n\n  b </title><title>Second</title></head>'
                '<body><svg><title>icon</title></svg></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertEqual(doc["titles"][0], "A b")
        self.assertIn("Second", doc["titles"])
        self.assertNotIn("icon", doc["titles"])

    def test_descriptions_and_robots_lowercased(self):
        html = ('<html><head><meta name="description" content="First desc">'
                '<meta name="description" content="Second desc">'
                '<meta name="robots" content="NOINDEX, Follow">'
                '<meta name="googlebot" content="NoSnipPet"></head><body></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertIn("First desc", doc["descriptions"])
        self.assertIn("Second desc", doc["descriptions"])
        joined = " ".join(doc["robots"])
        self.assertEqual(joined, joined.lower())
        self.assertIn("noindex", joined)
        self.assertIn("nosnippet", joined)

    def test_canonical_resolution_base_href_and_fragment(self):
        html = ('<html><head><base href="/sub/"><link rel="canonical" href="page.html#frag"></head>'
                '<body></body></html>')
        doc = parse_doc(html, BASE + "/a/b")
        self.assertEqual(doc["canonicals"], [BASE + "/sub/page.html"])
        doc2 = parse_doc('<html><head><link rel="canonical" href="/plain#x"></head></html>', BASE + "/a/b")
        self.assertEqual(doc2["canonicals"], [BASE + "/plain"])

    def test_headings_and_h1_img_alt(self):
        html = ('<html><body><h1>Hello <img alt="World"> there</h1><h2>One</h2><h2>Two</h2>'
                '<h2>Three</h2></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertEqual(doc["h2_count"], 3)
        self.assertEqual(len(doc["h1"]), 1)
        self.assertIn("Hello", doc["h1"][0])
        self.assertIn("World", doc["h1"][0])

    def test_hreflang_pairs(self):
        html = ('<html><head><link rel="alternate" hreflang="fr" href="/fr/">'
                '<link rel="alternate" hreflang="es-MX" href="https://example.com/es/#x"></head></html>')
        doc = parse_doc(html, BASE + "/")
        pairs = [list(p) for p in doc["hreflang"]]
        self.assertIn(["fr", BASE + "/fr/"], pairs)
        self.assertIn(["es-mx", BASE + "/es/"], [[l.lower(), h] for l, h in pairs])   # PM fix: case is not specified

    def test_links_filtering_anchors_and_nav(self):
        html = ('<html><body>'
                '<nav><a href="/nav/">Nav</a></nav>'
                '<a href="mailto:a@b.com">mail</a>'
                '<a href="tel:123">tel</a>'
                '<a href="sms:123">sms</a>'
                '<a href="javascript:void(0)">js</a>'
                '<a href="data:text/html,x">data</a>'
                '<a href="#top">top</a>'
                '<a href="/img"><img alt="Alt text"></a>'
                '<a href="/aria" aria-label="Aria label">   </a>'
                '<a rel="NoFollow External" href="https://other.example.org/x#f">Out</a>'
                '</body></html>')
        doc = parse_doc(html, BASE + "/")
        links = doc["links"]
        hrefs = [l["href"] for l in links]
        self.assertEqual(len(links), 4, hrefs)
        self.assertEqual(links[0]["href"], BASE + "/nav/")
        self.assertTrue(links[0]["in_nav"])
        self.assertEqual(links[1]["anchor"], "Alt text")
        self.assertEqual(links[2]["anchor"], "Aria label")
        self.assertEqual(links[3]["href"], "https://other.example.org/x")
        self.assertTrue(links[3]["nofollow"])
        self.assertEqual(links[3]["rel"], "nofollow external")
        for l in links:
            self.assertIn("in_nav", l)
            self.assertIn("anchor", l)

    def test_jsonld_types_recursive_and_error_count(self):
        html = ('<html><head>'
                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",'
                '"sub":{"@type":"WebSite"},"@graph":[{"@type":"BreadcrumbList"}]}</script>'
                '<script type="application/ld+json">{not json at all</script>'
                '</head><body></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertEqual(doc["jsonld_types"][0], "Organization")
        for expected in ("Organization", "WebSite", "BreadcrumbList"):
            self.assertIn(expected, doc["jsonld_types"])
        self.assertEqual(len(doc["jsonld_types"]), len(set(doc["jsonld_types"])))
        self.assertEqual(doc["jsonld_errors"], 1)

    def test_text_and_word_count_excludes_script_style(self):
        html = ('<!doctype html><html lang="en"><body><script>bad()</script>'
                '<style>.x{color:red}</style><noscript>noscript junk</noscript>'
                '<main><p>Hello   world</p></main></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertIn("Hello world", doc["text"])
        self.assertNotIn("bad()", doc["text"])
        self.assertNotIn("color:red", doc["text"])
        self.assertNotIn("noscript junk", doc["text"])
        self.assertEqual(doc["words"], len(doc["text"].split()))

    def test_preload_images_and_imagesrcset(self):
        html = ('<html><head><link rel="preload" as="image" href="/a.png">'
                '<link rel="preload" as="image" imagesrcset="/b.png 1x, /c.png 2x">'
                '<link rel="preload" as="script" href="/s.js"></head><body></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertIn(BASE + "/a.png", doc["preload_images"])
        self.assertIn(BASE + "/b.png", doc["preload_images"])
        self.assertNotIn(BASE + "/s.js", doc["preload_images"])

    def test_images_and_video_posters(self):
        html = ('<html><body><img src="/i1.png" width="10" height="20" loading="lazy">'
                '<img src="/i2.png" alt="Two"><video poster="/p.jpg"></video></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertEqual(doc["images"][0]["src"], BASE + "/i1.png")
        self.assertIsNone(doc["images"][0]["alt"])
        self.assertEqual(doc["images"][0]["width"], "10")
        self.assertEqual(doc["images"][0]["height"], "20")
        self.assertEqual(doc["images"][0]["loading"], "lazy")
        self.assertEqual(doc["images"][1]["alt"], "Two")
        self.assertEqual(doc["video_posters"], [BASE + "/p.jpg"])

    def test_viewport_og_image_meta_refresh(self):
        html = ('<html><head><meta name="viewport" content="width=device-width">'
                '<meta property="og:image" content="/og.png">'
                '<meta http-equiv="refresh" content="0;url=/x"></head><body></body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertTrue(doc["viewport"])
        self.assertTrue(doc["og_image"])
        self.assertEqual(doc["meta_refresh"], "0;url=/x")
        empty = parse_doc("<html><head></head><body></body></html>", BASE + "/")
        self.assertFalse(empty["viewport"])
        self.assertFalse(empty["og_image"])
        self.assertIsNone(empty["meta_refresh"])

    def test_documented_caps(self):
        imgs = "".join('<img src="/i%d.png" alt="a">' % i for i in range(600))
        anchors = "".join('<a href="/p%d">p%d</a>' % (i, i) for i in range(2100))
        titles = "".join("<title>t%d</title>" % i for i in range(25))
        html = ('<!doctype html><html><head>' + titles + '</head><body>' + imgs + anchors + '</body></html>')
        doc = parse_doc(html, BASE + "/")
        self.assertLessEqual(len(doc["images"]), 500)
        self.assertLessEqual(len(doc["links"]), 2000)
        self.assertLessEqual(len(doc["titles"]), 20)
        long_html = "<html><body><main><p>" + ("word " * 20000) + "</p></main></body></html>"
        self.assertLessEqual(len(parse_doc(long_html, BASE + "/")["text"]), 60000)
        long_title = "<html><head><title>" + ("x" * 5000) + "</title></head></html>"
        self.assertLessEqual(len(parse_doc(long_title, BASE + "/")["titles"][0]), 500)

    def test_hostile_input_never_raises_and_keeps_shape(self):
        hostile = [
            "",
            "\x00\x00<html\x00><body>x\x00</body>",
            "<div>" * 150 + "deep" + "</div>" * 150,
            '<a href="/x" title="' + ("A" * 200000) + '">y</a>',
            '<html lang="' + ("z" * 10000) + '">',
            '<script>var x = "<title>no</title>";</script>',
            '<meta name="description" content="' + ("q" * 10000) + '">',
            "<<<>>>&&&<!-- unterminated",
            "<title>" + ("t" * 5000) + "</title>",
            '<p>&amp; &#x1F600; \\ "quotes" \'single\'</p>',
            '<html><body><a href="' + ("/x" * 5000) + '">huge</a></body></html>',
        ]
        for html in hostile:
            doc = extract.parse(html, BASE + "/")
            self.assertIsInstance(doc, dict)
            for key in DOC_KEYS:
                self.assertIn(key, doc, "hostile input lost key %s" % key)
            json.dumps(doc)
            for link in doc["links"]:
                self.assertLessEqual(len(link.get("anchor") or ""), 500)


# ----------------------------------------------------------- is_indexable


class TestIsIndexable(unittest.TestCase):
    def test_clean_page_is_indexable(self):
        self.assertTrue(checks_content.is_indexable(make_page(BASE + "/a/")))

    def test_noindex_meta_makes_non_indexable(self):
        page = make_page(BASE + "/a/", html='<html lang="en"><head><meta name="robots" content="noindex"></head>'
                                           '<body><p>hi</p></body></html>')
        self.assertFalse(checks_content.is_indexable(page))

    def test_x_robots_tag_noindex_makes_non_indexable(self):
        page = make_page(BASE + "/a/", headers={"x-robots-tag": "noindex, nofollow"})
        self.assertFalse(checks_content.is_indexable(page))

    def test_canonical_to_other_url_makes_non_indexable(self):
        html = ('<html lang="en"><head><link rel="canonical" href="/b/"></head><body><p>hi</p></body></html>')
        self.assertFalse(checks_content.is_indexable(make_page(BASE + "/a/", html=html)))
        same = ('<html lang="en"><head><link rel="canonical" href="/a/"></head><body><p>hi</p></body></html>')
        self.assertTrue(checks_content.is_indexable(make_page(BASE + "/a/", html=same)))

    def test_slash_variants_are_different_urls(self):
        html = ('<html lang="en"><head><link rel="canonical" href="/a/"></head><body><p>hi</p></body></html>')
        page = make_page(BASE + "/a", html=html)
        self.assertFalse(checks_content.is_indexable(page))

    def test_status_ctype_doc_and_final_requirements(self):
        self.assertFalse(checks_content.is_indexable(make_page(BASE + "/a/", status=500)))
        self.assertFalse(checks_content.is_indexable(make_page(BASE + "/a/", ctype="application/json")))
        self.assertFalse(checks_content.is_indexable(make_page(BASE + "/a/", final=BASE + "/b/")))
        broken = make_page(BASE + "/a/")
        broken["doc"] = None
        self.assertFalse(checks_content.is_indexable(broken))


# --------------------------------------------------------- checks_content


class TestChecksContent(unittest.TestCase):
    def test_flags_missing_doctype(self):
        html = '<html lang="en"><head><title>No doctype</title></head><body><main><p>%s</p></main></body></html>' % LOREM
        result = make_crawl(pages={BASE + "/": make_page(BASE + "/", html=html)})
        findings, passed = checks_content.run(result)
        self.assertIn("doctype_missing", codes_of(findings))
        self.assertIsInstance(passed, list)

    def test_flags_missing_lang(self):
        html = '<!doctype html><html><head><title>No lang</title></head><body><main><p>%s</p></main></body></html>' % LOREM
        result = make_crawl(pages={BASE + "/": make_page(BASE + "/", html=html)})
        findings, _passed = checks_content.run(result)
        self.assertIn("lang_missing", codes_of(findings))

    def test_flags_near_duplicate_pair(self):
        text_a = " ".join("Page alpha line %d covers rental pricing detail." % i for i in range(40))
        text_b = text_a + " One extra closing sentence."      # PM fix: the generated pair was not 80% similar
        html_a = '<!doctype html><html lang="en"><head><title>Alpha</title></head><body><main><p>%s</p></main></body></html>' % text_a
        html_b = '<!doctype html><html lang="en"><head><title>Beta</title></head><body><main><p>%s</p></main></body></html>' % text_b
        pages = {BASE + "/a/": make_page(BASE + "/a/", html=html_a), BASE + "/b/": make_page(BASE + "/b/", html=html_b)}
        findings, _passed = checks_content.run(make_crawl(pages=pages))
        dup_codes = {"near_duplicate", "near_duplicate_self_canonical"}
        hits = [f for f in findings if f.get("code") in dup_codes]
        self.assertTrue(hits, "expected a near-duplicate finding, got %r" % codes_of(findings))
        blob = json.dumps(hits)
        self.assertIn(BASE + "/a/", blob)
        self.assertIn(BASE + "/b/", blob)

    def test_findings_shape_and_passed_names(self):
        result = make_crawl(pages={BASE + "/": make_page(BASE + "/")})
        findings, passed = checks_content.run(result)
        for f in findings:
            self.assertIn("code", f)
            self.assertIn("urls", f)
            self.assertIn("detail", f)
            self.assertIn("data", f)
            self.assertTrue(f["urls"], "finding %r has empty urls" % f.get("code"))
            self.assertIn(f["code"], issues.CATALOG, "unknown code emitted: %r" % f["code"])
            json.dumps(f)
        self.assertTrue(passed)
        for name in passed:
            self.assertIsInstance(name, str)

    def test_pages_with_title_multiple_and_thin_content(self):
        html = ('<!doctype html><html lang="en"><head><title>One</title><title>Two</title></head>'
                '<body><main><p>tiny</p></main></body></html>')
        findings, _passed = checks_content.run(make_crawl(pages={BASE + "/t/": make_page(BASE + "/t/", html=html)}))
        got = codes_of(findings)
        self.assertIn("title_multiple", got)
        self.assertIn("thin_content", got)


# ------------------------------------------------------------ checks_site


class TestChecksSite(unittest.TestCase):
    def test_flags_soft_404_probe(self):
        probes = {
            "http_home": [],
            "www_home": [],
            "not_found": {"url": BASE + "/adrails-404-check-deadbeef/", "status": 200},
        }
        findings, _passed = checks_site.run(make_crawl(pages={BASE + "/": make_page(BASE + "/")}, probes=probes))
        self.assertIn("soft_404", codes_of(findings))

    def test_clean_404_probe_is_not_flagged(self):
        probes = {
            "http_home": [],
            "www_home": [],
            "not_found": {"url": BASE + "/adrails-404-check-deadbeef/", "status": 404},
        }
        findings, _passed = checks_site.run(make_crawl(pages={BASE + "/": make_page(BASE + "/")}, probes=probes))
        self.assertNotIn("soft_404", codes_of(findings))

    def test_flags_robots_disallowing_googlebot(self):
        robots = "User-agent: *\nDisallow:\n\nUser-agent: Googlebot\nDisallow: /\n"
        findings, _passed = checks_site.run(make_crawl(pages={BASE + "/": make_page(BASE + "/")}, robots_text=robots))
        got = codes_of(findings)
        self.assertIn("robots_blocks_googlebot", got)
        for f in findings:
            if f["code"] == "robots_blocks_googlebot":
                self.assertTrue(f["urls"])

    def test_friendly_robots_is_not_flagged(self):
        robots = "User-agent: *\nDisallow: /admin/\nSitemap: https://example.com/sitemap.xml\n"
        findings, _passed = checks_site.run(make_crawl(pages={BASE + "/": make_page(BASE + "/")}, robots_text=robots))
        self.assertNotIn("robots_blocks_googlebot", codes_of(findings))

    def test_sitemap_non_indexable_url_is_flagged(self):
        url = BASE + "/noindexed/"
        html = '<!doctype html><html lang="en"><head><meta name="robots" content="noindex"><title>x</title></head>' \
               '<body><main><p>%s</p></main></body></html>' % LOREM
        pages = {url: make_page(url, html=html, sitemap=True)}
        findings, _passed = checks_site.run(make_crawl(pages=pages, sitemap_urls=[url]))
        self.assertIn("sitemap_non_indexable", codes_of(findings))


# ----------------------------------------------------------- checks_links


class TestChecksLinks(unittest.TestCase):
    def test_flags_internal_links_with_utm(self):
        src = BASE + "/a/"
        dst = BASE + "/b/?utm_source=newsletter"
        html = ('<!doctype html><html lang="en"><head><title>A</title></head><body><main><p>%s</p>'
                '<a href="/b/?utm_source=newsletter">b</a></main></body></html>' % LOREM)
        pages = {src: make_page(src, html=html)}
        result = make_crawl(pages=pages, edges=[[src, dst, "b", "", False]])
        findings, _passed = checks_links.run(result)
        self.assertIn("internal_utm_links", codes_of(findings))

    def test_clean_internal_links_are_not_flagged(self):
        src = BASE + "/a/"
        dst = BASE + "/b/"
        html = ('<!doctype html><html lang="en"><head><title>A</title></head><body><main><p>%s</p>'
                '<a href="/b/">b</a></main></body></html>' % LOREM)
        result = make_crawl(pages={src: make_page(src, html=html)}, edges=[[src, dst, "b", "", False]])
        findings, _passed = checks_links.run(result)
        self.assertNotIn("internal_utm_links", codes_of(findings))
        for f in findings:
            self.assertTrue(f["urls"])


# ---------------------------------------------------------------- issues


class TestIssues(unittest.TestCase):
    def findings(self):
        return [
            {"code": "title_missing", "urls": [BASE + "/a/"], "detail": "1 page has no title.",
             "data": {"count": 1}},
            {"code": "server_errors", "urls": [BASE + "/b/"], "detail": "1 page returned a 500.",
             "data": {"status": 500}},
            {"code": "totally_unknown_code", "urls": [BASE + "/c/"], "detail": "Unknown a|b =cmd(x)",
             "data": {}},
        ]

    def test_catalog_shape(self):
        self.assertIsInstance(issues.CATALOG, dict)
        self.assertIn("server_errors", issues.CATALOG)
        self.assertIn("title_missing", issues.CATALOG)
        for code, meta in issues.CATALOG.items():
            self.assertIn("priority", meta, code)
            self.assertIn("hours", meta, code)
            self.assertIn("issue", meta, code)
            self.assertIn("recommendation", meta, code)
            self.assertIn(meta["priority"], (1, 2, 3, 4), code)
        self.assertEqual(issues.CATALOG["server_errors"]["priority"], 1)
        self.assertEqual(issues.CATALOG["title_too_long"]["priority"], 4)

    def test_build_table_keys_and_priority_order(self):
        rows = issues.build_table(self.findings())
        self.assertTrue(rows)
        priorities = [r["priority"] for r in rows]
        self.assertEqual(priorities, sorted(priorities))
        self.assertEqual(rows[0]["priority"], 1)
        self.assertEqual(rows[0]["code"], "server_errors")
        for row in rows:
            self.assertTrue(TABLE_ROW_KEYS <= set(row), "missing row keys: %r" % (TABLE_ROW_KEYS - set(row)))
        json.dumps([dict(r) for r in rows], default=str)

    def test_unknown_code_defaults_to_priority_three(self):
        rows = issues.build_table(self.findings())
        row = [r for r in rows if r["code"] == "totally_unknown_code"][0]
        self.assertEqual(row["priority"], 3)

    def test_csv_header_exact(self):
        out = issues.to_csv(issues.build_table(self.findings()))
        self.assertIsInstance(out, str)
        header = out.splitlines()[0].strip()
        self.assertEqual(
            header,
            "priority,hours,issue,recommendation,example_url,pages_affected,template,status,code,detail",
        )

    def test_csv_neutralises_formula_cell(self):
        out = issues.to_csv(issues.build_table(self.findings()))
        self.assertIn("=cmd", out)
        for row in csv.reader(io.StringIO(out)):
            for cell in row:
                self.assertFalse(cell.startswith("="), "unneutralised CSV cell: %r" % cell)

    def test_markdown_escapes_pipe(self):
        md = issues.to_markdown(issues.build_table(self.findings()))
        self.assertIsInstance(md, str)
        self.assertIn("\\|", md)
        self.assertIn("a\\|b", md)


# ----------------------------------------------------------------- crawl


def _site():
    home = ('<!doctype html><html lang="en"><head><title>Home</title></head><body>'
            '<a href="/old/">old</a> <a href="/private/secret/">secret</a> '
            '<a href="https://other.example.org/x">external</a></body></html>')
    new = '<!doctype html><html lang="en"><head><title>New</title></head><body><p>%s</p></body></html>' % LOREM
    secret = '<!doctype html><html lang="en"><head><title>Secret</title></head><body><p>secret</p></body></html>'
    return FakeSite({
        BASE + "/robots.txt": resp(BASE + "/robots.txt", 200, "User-agent: *\nDisallow: /private/\n",
                                   ctype="text/plain"),
        BASE + "/": resp(BASE + "/", 200, home),
        BASE + "/old/": resp(BASE + "/old/", 301, "", location=BASE + "/new/"),
        BASE + "/new/": resp(BASE + "/new/", 200, new),
        BASE + "/private/secret/": resp(BASE + "/private/secret/", 200, secret),
    })


class TestCrawl(unittest.TestCase):
    def crawl(self):
        site = _site()
        result = crawl.crawl(BASE + "/", fetch=site, sleep=no_sleep, now=fixed_now, rate_limit_s=0.0)
        return site, result

    def test_result_is_json_serialisable_and_complete(self):
        _site_obj, result = self.crawl()
        for key in CRAWL_KEYS:
            self.assertIn(key, result, "missing crawl key %s" % key)
        blob = json.dumps(result)
        self.assertIsInstance(blob, str)
        self.assertIn(BASE + "/", result["pages"])

    def test_robots_disallow_is_respected(self):
        site, result = self.crawl()
        self.assertFalse(site.fetched(BASE + "/private/secret/"), "crawler fetched a robots-disallowed URL")
        self.assertNotIn(BASE + "/private/secret/", result["pages"])
        self.assertIsInstance(result["skipped"]["robots_disallowed"], list)

    def test_stays_on_host_and_records_external_links(self):
        site, result = self.crawl()
        self.assertFalse(site.fetched("https://other.example.org/x"), "crawler left the host")
        for url in site.calls:
            self.assertTrue(url.startswith(BASE + "/") or url.startswith("http://" + HOST)
                            or url.startswith("https://www." + HOST),     # PM fix: the www twin is in scope (SPEC probes)
                            "unexpected fetch outside scope: %r" % url)
        self.assertIn("https://other.example.org/x", result["external_links"])

    def test_records_redirect_chain(self):
        _site_obj, result = self.crawl()
        page = result["pages"].get(BASE + "/old/")
        self.assertIsNotNone(page, "redirecting URL missing from pages: %r" % list(result["pages"]))
        chain = page["chain"]
        self.assertGreaterEqual(len(chain), 2, "redirect chain not recorded: %r" % (chain,))
        self.assertEqual(page["status"], 200)
        self.assertEqual(page["final"], BASE + "/new/")

    def test_edges_are_five_element_lists(self):
        _site_obj, result = self.crawl()
        self.assertTrue(result["edges"])
        for edge in result["edges"]:
            self.assertIsInstance(edge, list)
            self.assertEqual(len(edge), 5, edge)
        srcs = [e[0] for e in result["edges"]]
        self.assertIn(BASE + "/", srcs)

    def test_pages_have_documented_fields(self):
        _site_obj, result = self.crawl()
        for url, page in result["pages"].items():
            for key in ("url", "chain", "status", "final", "ctype", "bytes", "truncated", "ms",
                        "headers", "error", "depth", "in_sitemap", "discovered", "doc"):
                self.assertIn(key, page, "page %s missing %s" % (url, key))
            self.assertIn(page["discovered"], ("start", "link", "sitemap"), url)

    def test_non_http_start_url_raises_tool_error(self):
        site = FakeSite({})
        with self.assertRaises(ToolError):
            crawl.crawl("ftp://example.com/", fetch=site, sleep=no_sleep, now=fixed_now, rate_limit_s=0.0)
        with self.assertRaises(ToolError):
            crawl.crawl("not a url", fetch=site, sleep=no_sleep, now=fixed_now, rate_limit_s=0.0)


# ------------------------------------------------------------------ runs


class TestRuns(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="adrails-runs-")
        self.store = runs.RunStore(self.dir, keep=30, now=fixed_now)

    def run_dict(self, n=1, extra_pages=None):
        pages = {BASE + "/": {"status": 200, "indexable": True, "noindex": False}}
        if extra_pages:
            pages.update(extra_pages)
        return {
            "host": HOST,
            "started_at": "2024-01-01T00:00:00Z",
            "finished_at": "2024-01-01T00:10:00Z",
            "pages": pages,
            "sequence": n,
        }

    def test_round_trip(self):
        rid = self.store.save(self.run_dict())
        self.assertIsInstance(rid, str)
        self.assertTrue(rid)
        loaded = self.store.load(HOST, rid)
        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded.get("host"), HOST)
        self.assertIn(BASE + "/", json.dumps(loaded))

    def test_malformed_run_id_is_refused(self):
        self.store.save(self.run_dict())
        for bad in ("../x", "..", "a/b", ""):
            with self.assertRaises(ToolError):
                self.store.load(HOST, bad)

    def test_latest_returns_saved_run(self):
        first = self.store.save(self.run_dict(1))
        second = self.store.save(self.run_dict(2, {BASE + "/b/": {"status": 200, "indexable": True, "noindex": False}}))
        self.assertNotEqual(first, second)
        latest = self.store.latest(HOST)
        self.assertTrue(latest == second or (isinstance(latest, str) and second in latest)
                        or isinstance(latest, dict), "unexpected latest(): %r" % (latest,))

    def test_diff_flags_noindex_regression(self):
        prev = self.run_dict(1)
        cur = self.run_dict(2, {})
        cur["pages"] = {BASE + "/": {"status": 200, "indexable": False, "noindex": True}}
        d = runs.diff(prev, cur)
        self.assertIsInstance(d, dict)
        for key in ("new", "fixed", "regressions", "summary"):
            self.assertIn(key, d, key)
        self.assertTrue(d["regressions"], "noindex flip not reported as regression: %r" % (d,))
        self.assertIn(BASE + "/", json.dumps(d["regressions"]))
        json.dumps(d, default=str)

    def test_diff_reports_new_page(self):
        prev = self.run_dict(1)
        cur = self.run_dict(2, {BASE + "/new-page/": {"status": 200, "indexable": True, "noindex": False}})
        d = runs.diff(prev, cur)
        # PM fix: "new" lists new (code, url) ISSUES, not new pages; a healthy new page is neither new issue nor regression
        self.assertNotIn(BASE + "/new-page/", json.dumps(d.get("new")))
        self.assertFalse(d.get("regressions"), "unexpected regressions: %r" % (d.get("regressions"),))

    def test_store_does_not_write_outside_base_dir(self):
        rid = self.store.save(self.run_dict())
        self.assertIsInstance(rid, str)
        self.assertNotIn("..", rid)
        self.assertTrue(os.path.isdir(self.dir))


if __name__ == "__main__":
    unittest.main()
