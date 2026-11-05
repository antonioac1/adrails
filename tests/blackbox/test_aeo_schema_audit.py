import datetime
import json
import unittest

from adsops.aeo import schema_audit as sa


def _ld_script(payload: str) -> str:
    return f'<html><head><script type="application/ld+json">{payload}</script></head></html>'


def _item_warnings(result) -> list:
    warnings = []
    for item in result.get("items", []):
        warnings.extend(item.get("warnings", []))
    return warnings


def _item_by_type(result, type_name: str):
    for item in result.get("items", []):
        if item.get("type") == type_name:
            return item
    return None


class TestRequiredSchema(unittest.TestCase):
    def test_required_map_organization(self):
        spec = sa.REQUIRED["Organization"]
        self.assertIn("required", spec)
        self.assertIn("recommended", spec)
        self.assertTrue({"name", "url"}.issubset(set(spec["required"])))
        self.assertTrue(
            {"logo", "sameAs", "description", "contactPoint"}
            & set(spec["recommended"])
        )

    def test_required_map_software_application(self):
        spec = sa.REQUIRED["SoftwareApplication"]
        self.assertTrue(
            {"name", "applicationCategory", "operatingSystem"}.issubset(
                set(spec["required"])
            )
        )

    def test_required_map_faq_and_question(self):
        faq = sa.REQUIRED["FAQPage"]
        question = sa.REQUIRED["Question"]
        self.assertIn("mainEntity", faq["required"])
        self.assertTrue({"name", "acceptedAnswer"}.issubset(set(question["required"])))

    def test_required_map_breadcrumb_list(self):
        spec = sa.REQUIRED["BreadcrumbList"]
        self.assertIn("itemListElement", spec["required"])


class TestExtractJsonLd(unittest.TestCase):
    def test_extracts_basic_object(self):
        html = _ld_script(
            json.dumps(
                {
                    "@context": "https://schema.org",
                    "@type": "Organization",
                    "name": "Acme",
                    "url": "https://acme.example",
                }
            )
        )
        objs = sa.extract_jsonld(html)
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["name"], "Acme")

    def test_ignores_non_jsonld_scripts(self):
        html = (
            "<html><body>"
            "<script>var x = 1;</script>"
            '<script type="text/javascript">{"@type":"Person"}</script>'
            '<script type="application/ld+json">'
            '{"@type":"WebSite","name":"Site","url":"https://example.com"}'
            "</script>"
            "</body></html>"
        )
        objs = sa.extract_jsonld(html)
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0]["@type"], "WebSite")

    def test_flattens_graph_array(self):
        html = _ld_script(
            json.dumps(
                {
                    "@context": "https://schema.org",
                    "@graph": [
                        {
                            "@type": "Organization",
                            "name": "Org",
                            "url": "https://org.example",
                        },
                        {
                            "@type": "WebSite",
                            "name": "Site",
                            "url": "https://site.example",
                        },
                    ],
                }
            )
        )
        objs = sa.extract_jsonld(html)
        self.assertEqual(len(objs), 2)
        types = {obj.get("@type") for obj in objs}
        self.assertIn("Organization", types)
        self.assertIn("WebSite", types)

    def test_flattens_top_level_list(self):
        html = _ld_script(
            json.dumps(
                [
                    {"@type": "Organization", "name": "Org", "url": "https://org.example"},
                    {"@type": "Product", "name": "Widget"},
                ]
            )
        )
        objs = sa.extract_jsonld(html)
        self.assertEqual(len(objs), 2)
        self.assertEqual(objs[1]["name"], "Widget")

    def test_invalid_json_records_error(self):
        html = _ld_script('{"@type": "Organization", "name": ')
        objs = sa.extract_jsonld(html)
        self.assertEqual(len(objs), 1)
        self.assertIn("_error", objs[0])
        self.assertIn("_raw", objs[0])
        self.assertIsInstance(objs[0]["_error"], str)

    def test_invalid_json_raw_is_truncated(self):
        bad_payload = '{"@type": "Organization", "name": "' + ("x" * 300)
        html = _ld_script(bad_payload)
        objs = sa.extract_jsonld(html)
        self.assertEqual(len(objs), 1)
        self.assertIn("_raw", objs[0])
        self.assertLessEqual(len(objs[0]["_raw"]), 200)
        self.assertTrue(objs[0]["_raw"])


class TestAuditObjects(unittest.TestCase):
    def test_return_shape_and_score_range(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "Organization",
                    "name": "Acme",
                    "url": "https://acme.example",
                    "logo": "https://acme.example/logo.png",
                    "sameAs": ["https://twitter.com/acme"],
                    "description": "Acme",
                    "contactPoint": {"@type": "ContactPoint", "contactType": "sales"},
                }
            ]
        )
        for key in ("types", "items", "findings", "score"):
            self.assertIn(key, result)
        self.assertIn("Organization", result["types"])
        self.assertIsInstance(result["items"], list)
        self.assertTrue(result["items"])
        item = result["items"][0]
        for key in ("type", "missing_required", "missing_recommended", "warnings"):
            self.assertIn(key, item)
        self.assertIsInstance(item["missing_required"], list)
        self.assertIsInstance(item["missing_recommended"], list)
        self.assertIsInstance(item["warnings"], list)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_detects_missing_required_and_recommended(self):
        result = sa.audit_objects([{"@type": "Organization", "name": "Acme"}])
        item = _item_by_type(result, "Organization")
        self.assertIsNotNone(item)
        self.assertIn("url", item["missing_required"])
        self.assertTrue(
            {"logo", "sameAs", "description", "contactPoint"}
            & set(item["missing_recommended"])
        )

    def test_valid_organization_has_no_missing_required(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "Organization",
                    "name": "Acme",
                    "url": "https://acme.example",
                    "logo": "https://acme.example/logo.png",
                    "sameAs": ["https://twitter.com/acme"],
                    "description": "Acme",
                    "contactPoint": {"@type": "ContactPoint", "contactType": "sales"},
                }
            ]
        )
        item = _item_by_type(result, "Organization")
        self.assertIsNotNone(item)
        self.assertEqual(item["missing_required"], [])

    def test_warns_on_non_numeric_offer_price(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "Product",
                    "name": "Widget",
                    "offers": {
                        "@type": "Offer",
                        "price": "free",
                        "priceCurrency": "USD",
                    },
                }
            ]
        )
        self.assertTrue(_item_warnings(result))

    def test_warns_on_non_https_same_as(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "Organization",
                    "name": "Acme",
                    "url": "https://acme.example",
                    "sameAs": ["http://twitter.com/acme"],
                }
            ]
        )
        self.assertTrue(_item_warnings(result))

    def test_warns_on_aggregate_rating_without_counts(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "SoftwareApplication",
                    "name": "App",
                    "applicationCategory": "BusinessApplication",
                    "operatingSystem": "Any",
                    "aggregateRating": {
                        "@type": "AggregateRating",
                        "ratingValue": "4.5",
                    },
                }
            ]
        )
        self.assertTrue(_item_warnings(result))

    def test_warns_on_short_faq_answer(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "FAQPage",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": "Question?",
                            "acceptedAnswer": {
                                "@type": "Answer",
                                "text": "Too short",
                            },
                        }
                    ],
                }
            ]
        )
        self.assertTrue(_item_warnings(result))

    def test_warns_on_long_faq_answer(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "FAQPage",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": "Question?",
                            "acceptedAnswer": {
                                "@type": "Answer",
                                "text": "word " * 301,
                            },
                        }
                    ],
                }
            ]
        )
        self.assertTrue(_item_warnings(result))

    def test_warns_on_duplicate_organization_blocks_with_different_names(self):
        result = sa.audit_objects(
            [
                {
                    "@type": "Organization",
                    "name": "Acme One",
                    "url": "https://one.example",
                    "logo": "https://one.example/logo.png",
                    "sameAs": ["https://x.example/one"],
                    "description": "One",
                    "contactPoint": {"@type": "ContactPoint", "contactType": "sales"},
                },
                {
                    "@type": "Organization",
                    "name": "Acme Two",
                    "url": "https://two.example",
                    "logo": "https://two.example/logo.png",
                    "sameAs": ["https://x.example/two"],
                    "description": "Two",
                    "contactPoint": {"@type": "ContactPoint", "contactType": "sales"},
                },
            ]
        )
        self.assertTrue(_item_warnings(result) or result.get("findings"))


class TestAuditHtml(unittest.TestCase):
    def test_empty_html_has_no_types_or_items(self):
        result = sa.audit_html("<html><body>No structured data</body></html>")
        self.assertEqual(result["types"], [])
        self.assertEqual(result["items"], [])
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_warns_when_date_modified_is_old(self):
        article = {
            "@type": "Article",
            "headline": "Old article",
            "datePublished": "2024-01-01",
            "dateModified": "2020-01-01",
            "author": {"@type": "Person", "name": "Author"},
        }
        html = _ld_script(json.dumps(article))
        today = datetime.date(2024, 6, 1)
        try:
            result = sa.audit_html(html, today=today)
        except TypeError:
            result = sa.audit_html(html, today=today.isoformat())
        self.assertTrue(_item_warnings(result))

    def test_warns_when_date_published_is_not_iso(self):
        article = {
            "@type": "Article",
            "headline": "Non-ISO article",
            "datePublished": "January 1, 2024",
            "dateModified": "2024-01-01",
            "author": {"@type": "Person", "name": "Author"},
        }
        result = sa.audit_html(_ld_script(json.dumps(article)))
        self.assertTrue(_item_warnings(result))


class TestGenerators(unittest.TestCase):
    def test_make_org_passes_audit(self):
        obj = sa.make_org(
            "Acme",
            "https://acme.example",
            logo="https://acme.example/logo.png",
            same_as=("https://twitter.com/acme",),
        )
        self.assertEqual(obj.get("@type"), "Organization")
        self.assertEqual(obj.get("name"), "Acme")
        result = sa.audit_objects([obj])
        item = _item_by_type(result, "Organization")
        self.assertIsNotNone(item)
        self.assertEqual(item["missing_required"], [])

    def test_make_software_passes_audit(self):
        obj = sa.make_software(
            "App",
            "https://app.example",
            "An application",
            version="1.2.3",
        )
        self.assertEqual(obj.get("@type"), "SoftwareApplication")
        result = sa.audit_objects([obj])
        item = _item_by_type(result, "SoftwareApplication")
        self.assertIsNotNone(item)
        self.assertEqual(item["missing_required"], [])

    def test_make_faq_passes_audit(self):
        obj = sa.make_faq([("Question one?", "Answer one."), ("Question two?", "Answer two.")])
        self.assertEqual(obj.get("@type"), "FAQPage")
        self.assertIn("mainEntity", obj)
        result = sa.audit_objects([obj])
        item = _item_by_type(result, "FAQPage")
        self.assertIsNotNone(item)
        self.assertEqual(item["missing_required"], [])

    def test_make_breadcrumbs_passes_audit(self):
        obj = sa.make_breadcrumbs(
            [("Home", "https://example.com/"), ("Products", "https://example.com/products")]
        )
        self.assertEqual(obj.get("@type"), "BreadcrumbList")
        result = sa.audit_objects([obj])
        item = _item_by_type(result, "BreadcrumbList")
        self.assertIsNotNone(item)
        self.assertEqual(item["missing_required"], [])


class TestScriptTag(unittest.TestCase):
    def test_roundtrip_json(self):
        obj = {
            "@type": "Organization",
            "name": "Acme",
            "url": "https://acme.example",
        }
        tag = sa.to_script_tag(obj)
        opening = '<script type="application/ld+json">'
        closing = "</script>"
        self.assertTrue(tag.startswith(opening))
        self.assertTrue(tag.endswith(closing))
        inner = tag[len(opening) : -len(closing)]
        self.assertEqual(json.loads(inner), obj)

    def test_escapes_closing_script_inside_json(self):
        obj = {
            "@type": "Article",
            "headline": "Test",
            "description": "before</script>after",
        }
        tag = sa.to_script_tag(obj)
        self.assertEqual(tag.count("</script>"), 1)
        opening = '<script type="application/ld+json">'
        closing = "</script>"
        inner = tag[len(opening) : -len(closing)]
        parsed = json.loads(inner)
        self.assertEqual(parsed["description"], "before</script>after")


if __name__ == "__main__":
    unittest.main()
