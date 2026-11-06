import json
import re
import unittest

from adsops.local.presence import (
    ToolError,
    area_page_brief,
    citations_checklist,
    nap_check,
    schema,
)


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def _all_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _all_keys(value)


def _digits(value):
    return re.sub(r"\D", "", value or "")


def _base_biz(**updates):
    biz = {
        "name": "Acme Plumbing",
        "type": "Plumber",
        "url": "https://acme.example",
        "phone": "(512) 555-1234",
        "address": {
            "street": "123 Main Street",
            "city": "Austin",
            "region": "TX",
            "postal": "78701",
        },
    }
    biz.update(updates)
    return biz


class SchemaTests(unittest.TestCase):
    def test_schema_minimal_has_core_keys_and_business_id(self):
        result = schema(_base_biz())
        self.assertIsInstance(result, dict)
        self.assertTrue({"jsonld", "script", "warnings"}.issubset(result))
        self.assertIsInstance(result["jsonld"], dict)
        self.assertIsInstance(result["script"], str)
        self.assertIsInstance(result["warnings"], list)

        jsonld = result["jsonld"]
        self.assertEqual(jsonld.get("@type"), "Plumber")
        self.assertEqual(jsonld.get("name"), "Acme Plumbing")
        self.assertEqual(jsonld.get("@id"), "https://acme.example#business")
        self.assertIn("application/ld+json", result["script"])

    def test_schema_non_https_url_produces_warning(self):
        result = schema(_base_biz(url="http://acme.example"))
        self.assertTrue(result["warnings"])
        self.assertTrue(any("https" in str(w).lower() for w in result["warnings"]))

    def test_schema_unknown_type_falls_back_to_localbusiness_and_warns(self):
        result = schema(_base_biz(type="DefinitelyNotARealSchemaType"))
        self.assertEqual(result["jsonld"].get("@type"), "LocalBusiness")
        self.assertTrue(result["warnings"])

    def test_schema_service_area_business_without_street_uses_areaserved(self):
        biz = {
            "name": "Acme Plumbing",
            "type": "Plumber",
            "url": "https://acme.example",
            "areas": ["Austin"],
        }
        result = schema(biz)
        jsonld = result["jsonld"]
        self.assertIn("areaServed", jsonld)
        self.assertNotIn("streetAddress", set(_all_keys(jsonld)))

    def test_schema_warns_if_no_address_and_no_areas(self):
        result = schema({
            "name": "Acme Plumbing",
            "type": "Plumber",
            "url": "https://acme.example",
        })
        self.assertTrue(result["warnings"])
        warnings = " ".join(str(w).lower() for w in result["warnings"])
        self.assertTrue("address" in warnings or "area" in warnings)

    def test_schema_area_served_city_and_postal_types(self):
        biz = {
            "name": "Acme Plumbing",
            "type": "Plumber",
            "url": "https://acme.example",
            "areas": ["Austin", "78701"],
        }
        jsonld = schema(biz)["jsonld"]
        served = jsonld.get("areaServed")
        if isinstance(served, dict):
            served = [served]
        self.assertIsInstance(served, list)

        types = {item.get("@type") for item in served if isinstance(item, dict)}
        names = {item.get("name") for item in served if isinstance(item, dict)}
        self.assertIn("City", types)
        self.assertIn("PostalCode", types)
        self.assertIn("Austin", names)
        self.assertIn("78701", names)

    def test_schema_hours_range_and_day_names(self):
        biz = _base_biz(hours={"mo-fr": "08:00-18:00", "sa": "09:00-13:00"})
        jsonld = schema(biz)["jsonld"]
        specs = jsonld.get("openingHoursSpecification")
        if isinstance(specs, dict):
            specs = [specs]
        self.assertIsInstance(specs, list)
        self.assertTrue(specs)

        days = []
        for item in specs:
            if not isinstance(item, dict):
                continue
            day = item.get("dayOfWeek")
            if isinstance(day, str):
                days.append(day)
            elif isinstance(day, list):
                days.extend(day)
        day_text = " ".join(days).lower()
        self.assertIn("monday", day_text)
        self.assertIn("friday", day_text)
        self.assertIn("saturday", day_text)

        pairs = {
            (item.get("opens"), item.get("closes"))
            for item in specs
            if isinstance(item, dict)
        }
        self.assertIn(("08:00", "18:00"), pairs)
        self.assertIn(("09:00", "13:00"), pairs)

    def test_schema_24_7_hours_cover_all_days(self):
        jsonld = schema(_base_biz(hours={"mo-su": "24/7"}))["jsonld"]
        specs = jsonld.get("openingHoursSpecification")
        if isinstance(specs, dict):
            specs = [specs]
        self.assertIsInstance(specs, list)

        days = []
        for item in specs:
            if not isinstance(item, dict):
                continue
            day = item.get("dayOfWeek")
            if isinstance(day, str):
                days.append(day)
            elif isinstance(day, list):
                days.extend(day)
        day_text = " ".join(days).lower()
        for day in [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ]:
            self.assertIn(day, day_text)

        pairs = {
            (item.get("opens"), item.get("closes"))
            for item in specs
            if isinstance(item, dict)
        }
        self.assertIn(("00:00", "23:59"), pairs)

    def test_schema_bad_hours_raise_toolerror(self):
        with self.assertRaises(ToolError):
            schema(_base_biz(hours={"mo": "not-a-time"}))

    def test_schema_omits_self_reported_rating_and_warns(self):
        result = schema(_base_biz(rating=4.9))
        self.assertNotIn("aggregateRating", set(_all_keys(result["jsonld"])))
        warnings = " ".join(str(w).lower() for w in result["warnings"])
        self.assertIn("aggregaterating omitted", warnings)
        self.assertIn("google", warnings)

    def test_schema_services_build_offer_catalog(self):
        services = [
            "Drain Cleaning",
            {
                "name": "Leak Repair",
                "description": "Emergency leak repair",
                "price_range": "$100-$200",
            },
        ]
        jsonld = schema(_base_biz(services=services))["jsonld"]
        catalog = jsonld.get("hasOfferCatalog")
        self.assertIsInstance(catalog, dict)
        self.assertEqual(catalog.get("@type"), "OfferCatalog")
        self.assertTrue(catalog.get("itemListElement"))

        types = {item.get("@type") for item in _walk_dicts(catalog)}
        self.assertIn("Offer", types)
        self.assertIn("Service", types)

    def test_schema_license_and_same_as_are_included(self):
        jsonld = schema(_base_biz(
            license="TX-123456",
            same_as=[
                "https://www.yelp.com/biz/acme-plumbing-austin",
                "https://www.facebook.com/acmeplumbing",
            ],
        ))["jsonld"]
        self.assertIn("hasCredential", set(_all_keys(jsonld)))
        dumped = json.dumps(jsonld)
        self.assertIn("TX-123456", dumped)
        self.assertIn("https://www.yelp.com/biz/acme-plumbing-austin", dumped)
        self.assertIn("https://www.facebook.com/acmeplumbing", dumped)

    def test_schema_script_escapes_embedded_closing_script_tag(self):
        result = schema(_base_biz(name='Bad </script><script>alert(1)</script>'))
        self.assertEqual(result["script"].count("</script>"), 1)


class NapCheckTests(unittest.TestCase):
    def _canonical(self):
        return {
            "name": "Acme Plumbing",
            "phone": "(512) 555-1234",
            "address": "123 Main Street",
            "city": "Austin",
            "postal": "78701",
        }

    def test_nap_check_normalizes_formatted_us_phone_variants(self):
        pages = [{
            "url": "https://example.com/page",
            "text": (
                "Acme Plumbing\n"
                "Call (512) 555-1234\n"
                "123 Main Street, Austin, TX 78701"
            ),
        }]
        result = nap_check(self._canonical(), pages)
        item = result["results"][0]
        self.assertTrue(item["phone_found"])
        seen_digits = [_digits(phone) for phone in item["phones_seen"]]
        self.assertTrue(any(phone.endswith("5125551234") for phone in seen_digits))

    def test_nap_check_flags_different_phone_number_listed(self):
        pages = [{
            "url": "https://example.com/page",
            "text": (
                "Acme Plumbing\n"
                "(512) 555-9999\n"
                "123 Main Street, Austin, TX 78701"
            ),
        }]
        item = nap_check(self._canonical(), pages)["results"][0]
        self.assertFalse(item["phone_found"])
        issues = " ".join(str(issue).lower() for issue in item["issues"])
        self.assertIn("different phone number listed", issues)

    def test_nap_check_name_matching_ignores_case_punctuation_and_llc(self):
        canonical = self._canonical()
        canonical["name"] = "Acme Heating and Cooling"
        pages = [{
            "url": "https://example.com/page",
            "text": (
                "ACME HEATING & COOLING LLC\n"
                "(512) 555-1234\n"
                "123 Main Street, Austin, TX 78701"
            ),
        }]
        item = nap_check(canonical, pages)["results"][0]
        self.assertTrue(item["name_found"])

    def test_nap_check_address_matching_handles_abbreviations(self):
        pages = [{
            "url": "https://example.com/page",
            "text": (
                "Acme Plumbing\n"
                "(512) 555-1234\n"
                "123 Main St, Austin, TX 78701"
            ),
        }]
        item = nap_check(self._canonical(), pages)["results"][0]
        self.assertTrue(item["address_found"])

    def test_nap_check_score_is_share_of_fully_consistent_pages(self):
        good_text = (
            "Acme Plumbing\n"
            "(512) 555-1234\n"
            "123 Main Street, Austin, TX 78701"
        )
        bad_text = (
            "Acme Plumbing\n"
            "(512) 555-0000\n"
            "123 Main Street, Austin, TX 78701"
        )
        result = nap_check(self._canonical(), [
            {"url": "https://example.com/good", "text": good_text},
            {"url": "https://example.com/bad", "text": bad_text},
        ])
        self.assertEqual(len(result["results"]), 2)
        self.assertAlmostEqual(float(result["score"]), 50.0, places=0)

    def test_nap_check_result_shape_and_summary(self):
        pages = [{
            "url": "https://example.com/page",
            "text": (
                "Acme Plumbing\n"
                "(512) 555-1234\n"
                "123 Main Street, Austin, TX 78701"
            ),
        }]
        result = nap_check(self._canonical(), pages)
        self.assertTrue({"results", "score", "summary"}.issubset(result))
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertIsInstance(result["summary"], str)
        self.assertTrue(result["summary"])

        item = result["results"][0]
        self.assertTrue({
            "url", "name_found", "phone_found", "address_found",
            "phones_seen", "issues",
        }.issubset(item))
        self.assertEqual(item["url"], "https://example.com/page")
        self.assertIsInstance(item["phones_seen"], list)
        self.assertIsInstance(item["issues"], list)


class AreaPageBriefTests(unittest.TestCase):
    def _biz(self):
        return _base_biz(
            license="TX-123456",
            areas=["Austin", "Round Rock", "78701"],
        )

    def test_area_page_brief_core_keys_and_length_limits(self):
        result = area_page_brief(
            self._biz(), "Austin, TX", "water heater repair"
        )
        self.assertTrue({
            "slug", "title", "meta_description", "h1", "outline",
            "faq", "schema_hint", "must_include", "avoid",
        }.issubset(result))
        self.assertLessEqual(len(result["title"]), 60)
        self.assertLessEqual(len(result["meta_description"]), 155)
        self.assertIsInstance(result["h1"], str)
        self.assertTrue(result["h1"])
        self.assertIsInstance(result["outline"], list)
        self.assertTrue(result["outline"])

    def test_area_page_brief_slug_is_lowercase_ascii_hyphenated(self):
        result = area_page_brief(self._biz(), "Austin", "Plumbing")
        self.assertEqual(result["slug"], "plumbing-austin")

        accented = area_page_brief(self._biz(), "Montréal", "Plomberie")
        slug = accented["slug"]
        self.assertTrue(slug.isascii())
        self.assertEqual(slug, slug.lower())
        self.assertNotIn(" ", slug)
        self.assertNotIn("_", slug)

    def test_area_page_brief_has_six_ai_assistant_faq_questions(self):
        result = area_page_brief(
            self._biz(), "Austin", "water heater repair"
        )
        faq = result["faq"]
        self.assertEqual(len(faq), 6)
        for item in faq:
            self.assertTrue({"q", "a_hint"}.issubset(item))
            self.assertTrue(item["q"])

        questions = " ".join(item["q"].lower() for item in faq)
        self.assertIn("cost", questions)
        self.assertIn("permit", questions)
        self.assertIn("best", questions)

    def test_area_page_brief_must_include_and_avoid_sections(self):
        result = area_page_brief(
            self._biz(), "Austin", "water heater repair"
        )
        must_include = " ".join(
            str(item).lower() for item in result["must_include"]
        )
        for token in ["nap", "license", "photo", "review", "price", "map"]:
            self.assertIn(token, must_include)

        avoid = " ".join(str(item).lower() for item in result["avoid"])
        self.assertIn("doorway", avoid)
        self.assertIn("clone", avoid)

    def test_area_page_brief_outline_contains_h2_headings(self):
        result = area_page_brief(
            self._biz(), "Austin", "water heater repair"
        )
        outline = result["outline"]
        self.assertGreaterEqual(len(outline), 3)
        self.assertTrue(all(isinstance(item, str) for item in outline))


class CitationsChecklistTests(unittest.TestCase):
    def _sites_text(self, checklist):
        return " ".join(str(item["site"]).lower() for item in checklist)

    def test_citations_checklist_shape(self):
        result = citations_checklist("Plumber")
        self.assertIsInstance(result, list)
        self.assertTrue(result)
        for item in result:
            self.assertTrue({"site", "url", "priority", "why"}.issubset(item))
            self.assertIsInstance(item["site"], str)
            self.assertTrue(item["site"])
            self.assertIsInstance(item["url"], str)
            self.assertTrue(item["url"].startswith("http"))
            self.assertIsInstance(item["why"], str)
            self.assertTrue(item["why"])

    def test_citations_checklist_contains_core_us_local_sites(self):
        result = citations_checklist("Plumber")
        sites = self._sites_text(result)
        for token in [
            "google", "bing", "apple", "yelp", "facebook", "bbb",
            "nextdoor", "angi", "thumbtack", "yellow", "chamber",
            "license",
        ]:
            self.assertIn(token, sites)

    def test_citations_checklist_houzz_only_for_home_trades(self):
        self.assertIn("houzz", self._sites_text(citations_checklist("Plumber")))
        self.assertNotIn("houzz", self._sites_text(citations_checklist("Dentist")))

    def test_citations_checklist_apple_and_bing_are_high_priority(self):
        result = citations_checklist("Plumber")
        apple = [i for i in result if "apple" in i["site"].lower()]
        bing = [i for i in result if "bing" in i["site"].lower()]
        self.assertTrue(apple)
        self.assertTrue(bing)
        self.assertEqual(str(apple[0]["priority"]).lower(), "high")
        self.assertEqual(str(bing[0]["priority"]).lower(), "high")

    def test_citations_checklist_includes_state_license_lookup(self):
        result = citations_checklist("Plumber")
        self.assertTrue(any("license" in item["site"].lower() for item in result))


if __name__ == "__main__":
    unittest.main()
