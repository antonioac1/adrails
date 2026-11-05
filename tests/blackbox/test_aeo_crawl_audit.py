"""Tests for adsops.aeo.crawl_audit — written strictly against the documented spec.

All network I/O is mocked; only the public documented API and behaviours
(including safety refusals and edge cases) are exercised.
"""

import unittest
from urllib.parse import urlsplit

import adsops.aeo.crawl_audit as ca


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS = {"critical": 25, "high": 12, "medium": 6, "low": 2, "info": 0}
SEVERITIES = tuple(SEVERITY_WEIGHTS)


def sev(finding):
    """Best-effort extraction of a finding's severity (dict or object)."""
    if isinstance(finding, str):
        low = finding.strip().lower()
        for level in SEVERITIES:
            if low.startswith(level):
                return level
    if isinstance(finding, dict):
        for key in ("severity", "level", "sev"):
            if key in finding:
                return str(finding[key]).strip().lower()
    for key in ("severity", "level", "sev"):
        if hasattr(finding, key):
            return str(getattr(finding, key)).strip().lower()
    raise AssertionError("finding has no severity: %r" % (finding,))


def text_of(finding):
    if isinstance(finding, dict):
        return " ".join(str(v) for v in finding.values()).lower()
    return str(finding).lower()


def severities(findings):
    return [sev(f) for f in findings]


def path_of(url):
    return urlsplit(url).path or "/"


def make_fetch(pages):
    """pages: dict url-path -> (status, headers, body). Returns (fetch, calls)."""
    calls = []

    def fetch(url):
        calls.append(url)
        return pages.get(path_of(url), (404, {}, ""))

    return fetch, calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

URL = "https://example.com/page"
TITLE = "<title>Example page</title>"
DESC = '<meta name="description" content="A short description of the page.">'
CANON = '<link rel="canonical" href="https://example.com/page">'
BODY_WORDS = " ".join(["alpha"] * 300)


def page(head="", body=None, lang=True):
    if body is None:
        body = "<p>%s</p>" % BODY_WORDS
    lang_attr = ' lang="en"' if lang else ""
    return "<html%s><head>%s</head><body>%s</body></html>" % (lang_attr, head, body)


CLEAN_HEAD = TITLE + DESC + CANON
CLEAN_PAGE = page(head=CLEAN_HEAD)


# ---------------------------------------------------------------------------
# AI_BOTS table
# ---------------------------------------------------------------------------

class TestAiBots(unittest.TestCase):
    def test_bot_table_contents(self):
        expected = {
            "GPTBot": ("OpenAI", "training"),
            "OAI-SearchBot": ("OpenAI", "search"),
            "ChatGPT-User": ("OpenAI", "user-fetch"),
            "ClaudeBot": ("Anthropic", "training"),
            "Claude-SearchBot": ("Anthropic", "search"),
            "Claude-User": ("Anthropic", "user-fetch"),
            "PerplexityBot": ("Perplexity", "search"),
            "Perplexity-User": ("Perplexity", "user-fetch"),
            "Google-Extended": ("Google", "training"),
            "Googlebot": ("Google", "search"),
            "Bingbot": ("Microsoft", "search"),
            "Applebot": ("Apple", "search"),
            "Applebot-Extended": ("Apple", "training"),
            "DuckAssistBot": ("DuckDuckGo", "user-fetch"),
            "Meta-ExternalAgent": ("Meta", "training"),
            "Amazonbot": ("Amazon", "search"),
            "CCBot": ("CommonCrawl", "training"),
            "Bytespider": ("ByteDance", "training"),
        }
        self.assertEqual(set(ca.AI_BOTS.keys()), set(expected.keys()))
        for name, (owner, purpose) in expected.items():
            info = ca.AI_BOTS[name]
            self.assertEqual(info["owner"], owner, name)
            self.assertEqual(info["purpose"], purpose, name)

    def test_bot_ordering_and_purpose_domain(self):
        names = list(ca.AI_BOTS.keys())
        self.assertEqual(names[0], "GPTBot")
        self.assertEqual(names[-1], "Bytespider")
        for name, info in ca.AI_BOTS.items():
            self.assertIn(info["purpose"], ("training", "search", "user-fetch"))
            self.assertTrue(info["owner"])


# ---------------------------------------------------------------------------
# parse_robots
# ---------------------------------------------------------------------------

class TestParseRobots(unittest.TestCase):
    def test_basic_group(self):
        parsed = ca.parse_robots("User-agent: GPTBot\nDisallow: /private\n")
        self.assertEqual(len(parsed["groups"]), 1)
        group = parsed["groups"][0]
        self.assertEqual([a.lower() for a in group["agents"]], ["gptbot"])
        rules = [(str(r[0]).lower(), r[1]) for r in group["rules"]]
        self.assertEqual(rules, [("disallow", "/private")])
        self.assertEqual(parsed["sitemaps"], [])

    def test_case_insensitive_directives(self):
        parsed = ca.parse_robots("USER-AGENT: gptbot\nDISALLOW: /secret\n")
        group = parsed["groups"][0]
        self.assertEqual(len(group["agents"]), 1)
        self.assertEqual(group["agents"][0].lower(), "gptbot")
        rules = [(str(r[0]).lower(), r[1]) for r in group["rules"]]
        self.assertEqual(rules, [("disallow", "/secret")])

    def test_comments_stripped(self):
        text = "# a comment\nUser-agent: GPTBot  # inline\nDisallow: /a # trailing\n"
        parsed = ca.parse_robots(text)
        group = parsed["groups"][0]
        self.assertEqual(group["agents"][0].lower(), "gptbot")
        rules = [(str(r[0]).lower(), r[1]) for r in group["rules"]]
        self.assertEqual(rules, [("disallow", "/a")])

    def test_blank_lines_and_multiple_agents(self):
        text = (
            "User-agent: Alpha\n"
            "User-agent: Beta\n"
            "Disallow: /x\n"
            "\n"
            "User-agent: Gamma\n"
            "Disallow: /y\n"
        )
        parsed = ca.parse_robots(text)
        self.assertEqual(len(parsed["groups"]), 2)
        first, second = parsed["groups"]
        self.assertEqual([a.lower() for a in first["agents"]], ["alpha", "beta"])
        self.assertEqual(len(first["rules"]), 1)
        self.assertEqual([a.lower() for a in second["agents"]], ["gamma"])

    def test_sitemaps_collected(self):
        text = (
            "User-agent: *\n"
            "Disallow:\n"
            "Sitemap: https://example.com/s1.xml\n"
            "Sitemap: https://example.com/s2.xml\n"
        )
        parsed = ca.parse_robots(text)
        self.assertEqual(
            list(parsed["sitemaps"]),
            ["https://example.com/s1.xml", "https://example.com/s2.xml"],
        )


# ---------------------------------------------------------------------------
# can_fetch
# ---------------------------------------------------------------------------

class TestCanFetch(unittest.TestCase):
    def test_star_fallback(self):
        parsed = ca.parse_robots("User-agent: *\nDisallow: /secret\n")
        self.assertFalse(ca.can_fetch(parsed, "GPTBot", "/secret"))
        self.assertTrue(ca.can_fetch(parsed, "GPTBot", "/public"))

    def test_specific_group_beats_star(self):
        text = "User-agent: *\nDisallow: /\n\nUser-agent: GPTBot\nAllow: /\n"
        parsed = ca.parse_robots(text)
        self.assertTrue(ca.can_fetch(parsed, "GPTBot", "/"))
        self.assertFalse(ca.can_fetch(parsed, "Bingbot", "/"))

    def test_longest_match_wins(self):
        text = "User-agent: *\nDisallow: /a\nAllow: /a/b\n"
        parsed = ca.parse_robots(text)
        self.assertFalse(ca.can_fetch(parsed, "GPTBot", "/a/c"))
        self.assertTrue(ca.can_fetch(parsed, "GPTBot", "/a/b/c"))

    def test_allow_wins_ties(self):
        text = "User-agent: *\nAllow: /tie\nDisallow: /tie\n"
        parsed = ca.parse_robots(text)
        self.assertTrue(ca.can_fetch(parsed, "GPTBot", "/tie"))

    def test_wildcards(self):
        text = "User-agent: *\nDisallow: /*.pdf$\n"
        parsed = ca.parse_robots(text)
        self.assertFalse(ca.can_fetch(parsed, "GPTBot", "/docs/file.pdf"))
        self.assertTrue(ca.can_fetch(parsed, "GPTBot", "/docs/file.pdf.html"))

    def test_empty_disallow_allows_everything(self):
        parsed = ca.parse_robots("User-agent: *\nDisallow:\n")
        self.assertTrue(ca.can_fetch(parsed, "GPTBot", "/anything"))

    def test_no_rules_defaults_to_allow(self):
        self.assertTrue(ca.can_fetch(ca.parse_robots(""), "GPTBot", "/"))


# ---------------------------------------------------------------------------
# audit_robots
# ---------------------------------------------------------------------------

ROBOTS_WITH_SITEMAP = (
    "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
)


class TestAuditRobots(unittest.TestCase):
    def test_structure(self):
        result = ca.audit_robots(ROBOTS_WITH_SITEMAP)
        self.assertIn("bots", result)
        self.assertIn("sitemaps", result)
        self.assertIn("findings", result)

        bots = result["bots"]
        self.assertEqual({b["bot"] for b in bots}, set(ca.AI_BOTS.keys()))
        expected_paths = {"/", "/llms.txt", "/sitemap.xml"}
        for entry in bots:
            info = ca.AI_BOTS[entry["bot"]]
            self.assertEqual(entry["owner"], info["owner"])
            self.assertEqual(entry["purpose"], info["purpose"])
            self.assertEqual(set(entry["allowed"].keys()), expected_paths)
            for value in entry["allowed"].values():
                self.assertIsInstance(value, bool)

    def test_custom_paths(self):
        result = ca.audit_robots(ROBOTS_WITH_SITEMAP, paths=("/a", "/b"))
        for entry in result["bots"]:
            self.assertEqual(set(entry["allowed"].keys()), {"/a", "/b"})

    def test_blocked_search_bot_is_critical(self):
        result = ca.audit_robots("User-agent: Googlebot\nDisallow: /\n")
        googlebot = next(b for b in result["bots"] if b["bot"] == "Googlebot")
        self.assertFalse(googlebot["allowed"]["/"])
        crit = [f for f in result["findings"] if sev(f) == "critical"]
        self.assertTrue(crit, "blocking a search bot on / should be critical")
        self.assertTrue(
            any("invisible to google answers" in text_of(f) for f in crit),
            "critical finding should mention invisibility to the owner",
        )

    def test_blocked_training_bot_is_not_critical(self):
        result = ca.audit_robots("User-agent: GPTBot\nDisallow: /\n")
        gptbot = next(b for b in result["bots"] if b["bot"] == "GPTBot")
        self.assertFalse(gptbot["allowed"]["/"])
        self.assertNotIn(
            "critical",
            severities(result["findings"]),
            "training bots allowed/blocked is a policy choice, not critical",
        )

    def test_missing_sitemap_is_medium(self):
        result = ca.audit_robots("User-agent: *\nAllow: /\n")
        self.assertEqual(list(result["sitemaps"]), [])
        self.assertIn("medium", severities(result["findings"]))

    def test_present_sitemap_listed(self):
        result = ca.audit_robots(ROBOTS_WITH_SITEMAP)
        self.assertEqual(
            list(result["sitemaps"]), ["https://example.com/sitemap.xml"]
        )


# ---------------------------------------------------------------------------
# audit_page
# ---------------------------------------------------------------------------

class TestAuditPage(unittest.TestCase):
    def test_clean_page_has_no_critical_or_high(self):
        result = ca.audit_page(URL, CLEAN_PAGE, {})
        self.assertIn("findings", result)
        self.assertIn("meta", result)
        self.assertIsInstance(result["meta"], dict)
        levels = severities(result["findings"])
        self.assertNotIn("critical", levels)
        self.assertNotIn("high", levels)

    def test_meta_robots_noindex_is_critical(self):
        html = page(head=CLEAN_HEAD + '<meta name="robots" content="noindex">')
        result = ca.audit_page(URL, html, {})
        self.assertIn("critical", severities(result["findings"]))

    def test_x_robots_tag_nosnippet_is_critical(self):
        result = ca.audit_page(URL, CLEAN_PAGE, {"X-Robots-Tag": "nosnippet"})
        self.assertIn("critical", severities(result["findings"]))

    def test_missing_title_and_description(self):
        html = page(head=CANON)
        result = ca.audit_page(URL, html, {})
        self.assertIn("medium", severities(result["findings"]))

    def test_overlong_meta_description_is_low(self):
        long_desc = '<meta name="description" content="%s">' % ("d" * 200)
        html = page(head=TITLE + long_desc + CANON)
        result = ca.audit_page(URL, html, {})
        self.assertIn("low", severities(result["findings"]))

    def test_missing_lang_is_low(self):
        html = page(head=CLEAN_HEAD, lang=False)
        result = ca.audit_page(URL, html, {})
        self.assertIn("low", severities(result["findings"]))

    def test_canonical_problems(self):
        # missing canonical -> medium
        result = ca.audit_page(URL, page(head=TITLE + DESC), {})
        self.assertIn("medium", severities(result["findings"]))

        # foreign canonical -> medium
        foreign = '<link rel="canonical" href="https://other.example/x">'
        result = ca.audit_page(URL, page(head=TITLE + DESC + foreign), {})
        self.assertIn("medium", severities(result["findings"]))

        # non-https canonical -> low
        insecure = '<link rel="canonical" href="http://example.com/page">'
        result = ca.audit_page(URL, page(head=TITLE + DESC + insecure), {})
        self.assertIn("low", severities(result["findings"]))

    def test_thin_body_is_high(self):
        html = page(head=CLEAN_HEAD, body="<p>only a few words here</p>")
        result = ca.audit_page(URL, html, {})
        self.assertIn("high", severities(result["findings"]))

    def test_content_only_in_noscript_is_high(self):
        noscript_only = page(
            head=CLEAN_HEAD,
            body="<noscript><p>%s</p></noscript><p>hi</p>" % BODY_WORDS,
        )
        result = ca.audit_page(URL, noscript_only, {})
        self.assertIn("high", severities(result["findings"]))

    def test_js_required_page_is_high(self):
        scripts = "".join("<script src='app%d.js'></script>" % i for i in range(10))
        html = page(head=CLEAN_HEAD, body="<p>tiny stub</p>" + scripts)
        result = ca.audit_page(URL, html, {})
        self.assertIn("high", severities(result["findings"]))


# ---------------------------------------------------------------------------
# audit_site
# ---------------------------------------------------------------------------

GOOD_ROBOTS = (
    "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
)
LLMS_TXT = "# llms.txt\nExample site summary.\n"
SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<urlset>"
    "<url><loc>https://example.com/</loc></url>"
    "<url><loc>https://example.com/about</loc></url>"
    "</urlset>"
)


class TestAuditSite(unittest.TestCase):
    def _good_pages(self):
        return {
            "/robots.txt": (200, {}, GOOD_ROBOTS),
            "/llms.txt": (200, {}, LLMS_TXT),
            "/sitemap.xml": (200, {}, SITEMAP_XML),
            "/": (200, {"Content-Type": "text/html"}, CLEAN_PAGE),
        }

    def test_result_shape(self):
        fetch, calls = make_fetch(self._good_pages())
        result = ca.audit_site("https://example.com", fetch)

        for key in ("robots", "llms_txt", "sitemap", "home", "score", "findings"):
            self.assertIn(key, result)

        self.assertIn("/robots.txt", {path_of(u) for u in calls})
        self.assertIn("/llms.txt", {path_of(u) for u in calls})
        self.assertIn("/sitemap.xml", {path_of(u) for u in calls})
        self.assertIn("/", {path_of(u) for u in calls})

        self.assertEqual(result["llms_txt"]["status"], 200)
        self.assertEqual(result["llms_txt"]["bytes"], len(LLMS_TXT))

        self.assertEqual(result["sitemap"]["status"], 200)
        self.assertEqual(result["sitemap"]["urls"], 2)

        self.assertIsInstance(result["home"], dict)
        self.assertIsInstance(result["findings"], list)

        score = result["score"]
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

    def test_findings_sorted_by_severity(self):
        fetch, _ = make_fetch(self._good_pages())
        result = ca.audit_site("https://example.com", fetch)
        weights = [SEVERITY_WEIGHTS[sev(f)] for f in result["findings"]]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_missing_assets(self):
        pages = {
            "/robots.txt": (404, {}, ""),
            "/llms.txt": (404, {}, ""),
            "/sitemap.xml": (404, {}, ""),
            "/": (200, {}, CLEAN_PAGE),
        }
        fetch, _ = make_fetch(pages)
        result = ca.audit_site("https://example.com", fetch)

        self.assertEqual(result["llms_txt"]["status"], 404)
        self.assertEqual(result["llms_txt"]["bytes"], 0)
        self.assertEqual(result["sitemap"]["status"], 404)
        self.assertEqual(result["sitemap"]["urls"], 0)
        self.assertLess(result["score"], 100)


if __name__ == '__main__':
    unittest.main()