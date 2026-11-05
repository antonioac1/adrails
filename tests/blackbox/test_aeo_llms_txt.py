import unittest

from adsops.aeo import llms_txt


VALID = """# My Docs

> Everything you need to know.

## Getting Started

- [Install](https://example.com/install): How to install it
- [Usage](https://example.com/usage)

## Advanced

- [Config](https://example.com/config): Configuration reference
"""


class TestParse(unittest.TestCase):
    def test_parse_valid(self):
        p = llms_txt.parse(VALID)
        self.assertIn("Everything you need to know.", p["summary"])
        self.assertEqual(p["title"].strip(), "My Docs")
        self.assertTrue({"title", "summary", "sections", "errors"} <= set(p))
        self.assertEqual(p["errors"], [])
        self.assertEqual(
            [s["name"] for s in p["sections"]], ["Getting Started", "Advanced"]
        )
        links = p["sections"][0]["links"]
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["title"], "Install")
        self.assertEqual(links[0]["url"], "https://example.com/install")
        self.assertIn("How to install it", links[0]["note"])
        self.assertEqual(links[1]["title"], "Usage")
        self.assertEqual(links[1]["url"], "https://example.com/usage")
        self.assertFalse(links[1]["note"])  # optional note absent

    def test_parse_missing_h1(self):
        p = llms_txt.parse("## Only Section\n\n- [A](https://example.com/a)\n")
        self.assertTrue(p["errors"])
        self.assertFalse(p["title"])

    def test_parse_no_summary(self):
        p = llms_txt.parse("# T\n\n## S\n\n- [A](https://example.com/a)\n")
        self.assertFalse(p["summary"])
        self.assertEqual(p["sections"][0]["name"], "S")

    def test_parse_leading_blank_lines(self):
        text = "\n\n\n# Title\n\n> Sum\n\n## Sec\n\n- [A](https://example.com/a)\n"
        p = llms_txt.parse(text)
        self.assertEqual(p["title"].strip(), "Title")
        self.assertEqual(p["sections"][0]["name"], "Sec")
        self.assertEqual(p["errors"], [])

    def test_parse_rejects_non_h1_first_line(self):
        text = "Introduction\n\n# Title\n\n> S\n\n## S\n\n- [A](https://example.com/a)\n"
        p = llms_txt.parse(text)
        self.assertTrue(p["errors"])


class TestValidate(unittest.TestCase):
    def test_validate_valid(self):
        v = llms_txt.validate(VALID)
        self.assertTrue({"ok", "errors", "warnings"} <= set(v))
        self.assertTrue(v["ok"])
        self.assertEqual(v["errors"], [])

    def test_validate_missing_h1(self):
        v = llms_txt.validate(
            "> Summary\n\n## S\n\n- [A](https://example.com/a)\n"
        )
        self.assertFalse(v["ok"])
        self.assertTrue(v["errors"])

    def test_validate_no_summary_is_warning(self):
        v = llms_txt.validate("# T\n\n## S\n\n- [A](https://example.com/a)\n")
        self.assertEqual(v["errors"], [])
        self.assertTrue(v["warnings"])
        self.assertTrue(v["ok"])

    def test_validate_no_links_is_error(self):
        v = llms_txt.validate("# T\n\n> Summary\n\n## S\n")
        self.assertFalse(v["ok"])
        self.assertTrue(v["errors"])

    def test_validate_relative_url_without_base(self):
        v = llms_txt.validate("# T\n\n> S\n\n## S\n\n- [P](/page)\n")
        self.assertTrue(v["warnings"])
        self.assertTrue(v["ok"])

    def test_validate_relative_url_with_base(self):
        v = llms_txt.validate(
            "# T\n\n> S\n\n## S\n\n- [P](/page)\n",
            base_url="https://example.com",
        )
        self.assertEqual(v["warnings"], [])

    def test_validate_non_https_warning(self):
        v = llms_txt.validate(
            "# T\n\n> S\n\n## S\n\n- [X](http://example.com/x)\n"
        )
        self.assertTrue(v["warnings"])
        self.assertTrue(v["ok"])

    def test_validate_duplicate_urls_warning(self):
        text = (
            "# T\n\n> S\n\n## S\n\n"
            "- [A](https://example.com/same)\n"
            "- [B](https://example.com/same)\n"
        )
        v = llms_txt.validate(text)
        self.assertTrue(v["warnings"])
        self.assertTrue(v["ok"])

    def test_validate_size_warning_and_full(self):
        lines = ["# Big", "", "> Summary", "", "## Many", ""]
        for i in range(2000):
            lines.append(
                "- [Link %d](https://example.com/%d): %s" % (i, i, "x" * 80)
            )
        text = "\n".join(lines) + "\n"
        self.assertGreater(len(text), 100 * 1024)
        normal = llms_txt.validate(text)
        self.assertTrue(normal["warnings"])
        full = llms_txt.validate(text, full=True)
        self.assertLess(len(full["warnings"]), len(normal["warnings"]))

    def test_validate_optional_section_allowed(self):
        text = (
            "# T\n\n> S\n\n## Optional\n\n"
            "- [Extra](https://example.com/extra): Optional page\n"
        )
        v = llms_txt.validate(text)
        self.assertTrue(v["ok"])
        self.assertEqual(v["errors"], [])


class TestBuild(unittest.TestCase):
    SECTIONS = [
        ("Getting Started", [
            ("Install", "https://example.com/install", "Install guide"),
            ("Usage", "https://example.com/usage", ""),
        ]),
        ("Advanced", [
            ("Config", "https://example.com/config", "Config guide"),
        ]),
    ]

    def test_build_roundtrip_valid(self):
        text = llms_txt.build("My Docs", "A summary.", self.SECTIONS)
        v = llms_txt.validate(text)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["errors"], [])
        p = llms_txt.parse(text)
        self.assertEqual(p["title"].strip(), "My Docs")
        self.assertIn("A summary.", p["summary"])
        self.assertEqual(
            [s["name"] for s in p["sections"]], ["Getting Started", "Advanced"]
        )
        links = p["sections"][0]["links"]
        self.assertEqual([l["title"] for l in links], ["Install", "Usage"])
        self.assertEqual(links[0]["url"], "https://example.com/install")
        self.assertIn("Install guide", links[0]["note"])
        self.assertFalse(links[1]["note"])

    def test_build_with_intro(self):
        text = llms_txt.build(
            "T", "S", [("Sec", [("A", "https://e.com/a", "")])],
            intro="Welcome to the docs.",
        )
        self.assertIn("Welcome to the docs.", text)
        self.assertEqual(llms_txt.validate(text)["errors"], [])

    def test_build_from_pages_grouping_and_order(self):
        pages = [
            {"url": "https://example.com/a", "title": "Page A",
             "description": "About A", "section": "Guides"},
            {"url": "https://example.com/b", "title": "Page B",
             "description": "About B", "section": "API"},
            {"url": "https://example.com/c", "title": "Page C",
             "description": "About C"},
        ]
        text = llms_txt.build_from_pages("Site", "Summary", pages)
        self.assertEqual(llms_txt.validate(text)["errors"], [])
        p = llms_txt.parse(text)
        self.assertEqual([s["name"] for s in p["sections"]], ["Guides", "API", "Docs"])
        by_name = {s["name"]: [l["title"] for l in s["links"]] for s in p["sections"]}
        self.assertEqual(by_name["Guides"], ["Page A"])
        self.assertEqual(by_name["API"], ["Page B"])
        self.assertEqual(by_name["Docs"], ["Page C"])

    def test_build_full_structure(self):
        text = llms_txt.build_full(
            [("https://example.com/a", "Page A", "Body A text"),
             ("https://example.com/b", "Page B", "Body B text")],
            "Full Docs", "Full summary",
        )
        self.assertTrue(text.lstrip().startswith("# Full Docs"))
        self.assertIn("> Full summary", text)
        self.assertIn("## Page A", text)
        self.assertIn("Source: https://example.com/a", text)
        self.assertIn("Body A text", text)
        self.assertIn("## Page B", text)
        self.assertIn("Source: https://example.com/b", text)
        self.assertIn("Body B text", text)
        self.assertLess(text.index("Page A"), text.index("Page B"))

    def test_build_full_strips_html(self):
        text = llms_txt.build_full(
            [("https://example.com/x", "X", "<p>Hello <b>world</b></p>")],
            "T", "S",
        )
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("<b>", text)
        self.assertNotIn("</b>", text)


class TestHtmlToMarkdown(unittest.TestCase):
    def test_headings_and_paragraph(self):
        md = llms_txt.html_to_markdown(
            "<h1>One</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4><p>Para</p>"
        )
        for word in ("One", "Two", "Three", "Four"):
            self.assertIn(word, md)
            self.assertRegex(md, r"#+\s*" + word)
        self.assertIn("Para", md)

    def test_links_and_emphasis(self):
        md = llms_txt.html_to_markdown(
            '<p>See <a href="https://example.com">Example</a> and '
            "<strong>bold</strong> <em>it</em>.</p>"
        )
        self.assertIn("Example", md)
        self.assertIn("https://example.com", md)
        self.assertIn("bold", md)
        self.assertIn("it", md)

    def test_lists(self):
        md = llms_txt.html_to_markdown(
            "<ul><li>Alpha</li><li>Beta</li></ul><ol><li>First</li></ol>"
        )
        for word in ("Alpha", "Beta", "First"):
            self.assertIn(word, md)
        self.assertTrue(
            any(line.strip()[:1] in "-*" for line in md.splitlines() if line.strip())
        )

    def test_table_rows_as_pipes(self):
        md = llms_txt.html_to_markdown(
            "<table><tr><td>H1</td><td>H2</td></tr>"
            "<tr><td>c1</td><td>c2</td></tr></table>"
        )
        self.assertIn("|", md)
        for word in ("H1", "H2", "c1", "c2"):
            self.assertIn(word, md)

    def test_drops_ignored_blocks(self):
        md = llms_txt.html_to_markdown(
            "<nav>NAVTEXT</nav><footer>FOOTERTEXT</footer>"
            "<script>SCRIPTTEXT</script><style>STYLETEXT</style>"
            "<aside>ASIDETEXT</aside><p>keepme</p>"
        )
        self.assertIn("keepme", md)
        for bad in ("NAVTEXT", "FOOTERTEXT", "SCRIPTTEXT", "STYLETEXT", "ASIDETEXT"):
            self.assertNotIn(bad, md)

    def test_code_and_pre(self):
        md = llms_txt.html_to_markdown(
            "<p>Run <code>pip install x</code></p>"
            "<pre><code>line1\nline2</code></pre>"
        )
        self.assertIn("pip install x", md)
        self.assertIn("line1", md)


if __name__ == '__main__':
    unittest.main()
