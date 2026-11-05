import unittest

from adsops.aeo import ads_to_aeo


def _term(text, clicks=1, impressions=10, cost=1.0, conversions=0):
    return {
        "term": text,
        "clicks": clicks,
        "impressions": impressions,
        "cost": cost,
        "conversions": conversions,
    }


def _question(text):
    return {"question": text}


class TestQuestionsFromTerms(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(ads_to_aeo.questions_from_terms([]), [])

    def test_filters_below_min_impr(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("best crm software", impressions=4)],
            min_impr=5,
        )
        self.assertEqual(out, [])

    def test_includes_at_min_impr(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("best crm software", impressions=5)],
            min_impr=5,
        )
        self.assertEqual(len(out), 1)

    def test_required_keys_and_counts(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("best crm software", clicks=3, impressions=20, cost=2.5, conversions=1)]
        )
        self.assertEqual(len(out), 1)
        q = out[0]
        for key in (
            "question",
            "source_terms",
            "impressions",
            "clicks",
            "cost",
            "conversions",
        ):
            self.assertIn(key, q)

        self.assertIsInstance(q["question"], str)
        self.assertIsInstance(q["source_terms"], list)
        self.assertTrue(q["source_terms"])
        self.assertTrue(
            any("best crm software" in str(s).lower() for s in q["source_terms"])
        )
        self.assertEqual(q["impressions"], 20)
        self.assertEqual(q["clicks"], 3)
        self.assertAlmostEqual(q["cost"], 2.5)
        self.assertEqual(q["conversions"], 1)

    def test_cost_template(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("property management cost", impressions=10)]
        )
        self.assertEqual(len(out), 1)
        q = out[0]["question"].lower()
        self.assertIn("how much does", q)
        self.assertIn("property management", q)
        self.assertTrue(q.endswith("?"))

    def test_best_template(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("best crm software", impressions=10)]
        )
        self.assertEqual(len(out), 1)
        q = out[0]["question"].lower()
        self.assertIn("what is the best", q)
        self.assertIn("crm software", q)
        self.assertTrue(q.endswith("?"))

    def test_vs_template(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("acme vs beta", impressions=10)]
        )
        self.assertEqual(len(out), 1)
        q = out[0]["question"].lower()
        self.assertIn("vs", q)
        self.assertIn("which is better", q)

    def test_dedupes_similar_terms_and_sums_counts(self):
        out = ads_to_aeo.questions_from_terms(
            [
                _term("best crm software", clicks=5, impressions=50, cost=10.0, conversions=2),
                _term("best crm software 2024", clicks=3, impressions=30, cost=5.0, conversions=1),
            ],
            min_impr=5,
        )
        self.assertEqual(len(out), 1)
        q = out[0]
        self.assertGreaterEqual(len(q["source_terms"]), 2)
        self.assertEqual(q["impressions"], 80)
        self.assertEqual(q["clicks"], 8)
        self.assertAlmostEqual(q["cost"], 15.0)
        self.assertEqual(q["conversions"], 3)

    def test_sorts_by_impressions_desc(self):
        out = ads_to_aeo.questions_from_terms(
            [
                _term("best crm software", impressions=100),
                _term("how to fix leak", impressions=10),
            ]
        )
        self.assertGreaterEqual(len(out), 2)
        self.assertGreaterEqual(out[0]["impressions"], out[1]["impressions"])

    def test_question_term_remains_question(self):
        out = ads_to_aeo.questions_from_terms(
            [_term("what is property management", impressions=10)]
        )
        self.assertEqual(len(out), 1)
        q = out[0]["question"]
        self.assertTrue(q.endswith("?"))
        self.assertIn("property management", q.lower())


class TestCoverage(unittest.TestCase):
    def test_returns_covered_and_gaps(self):
        out = ads_to_aeo.coverage(
            [_question("how much does property management cost")],
            [],
        )
        self.assertIn("covered", out)
        self.assertIn("gaps", out)
        self.assertIsInstance(out["covered"], list)
        self.assertIsInstance(out["gaps"], list)
        self.assertEqual(len(out["covered"]), 0)
        self.assertEqual(len(out["gaps"]), 1)
        self.assertIsNone(out["gaps"][0]["covered_by"])

    def test_heading_match_marks_covered(self):
        question_text = "how much does property management cost"
        pages = [
            {
                "url": "https://example.com/cost",
                "headings": [question_text],
                "text": "",
            }
        ]
        out = ads_to_aeo.coverage([_question(question_text)], pages)
        self.assertEqual(len(out["covered"]), 1)
        self.assertEqual(out["covered"][0]["covered_by"], "https://example.com/cost")
        self.assertEqual(out["gaps"], [])

    def test_text_match_marks_covered(self):
        question_text = "property management cost tampa"
        pages = [
            {
                "url": "https://example.com/tampa",
                "headings": [],
                "text": "We offer property management cost estimates in Tampa.",
            }
        ]
        out = ads_to_aeo.coverage([_question(question_text)], pages)
        self.assertEqual(len(out["covered"]), 1)
        self.assertEqual(out["covered"][0]["covered_by"], "https://example.com/tampa")

    def test_unmatched_question_is_gap(self):
        out = ads_to_aeo.coverage(
            [_question("how much does property management cost in tampa")],
            [
                {
                    "url": "https://example.com/other",
                    "headings": ["Unrelated heading"],
                    "text": "Nothing relevant here.",
                }
            ],
        )
        self.assertEqual(len(out["gaps"]), 1)
        self.assertIsNone(out["gaps"][0]["covered_by"])

    def test_uses_later_page_url(self):
        question_text = "how much does property management cost"
        pages = [
            {"url": "https://example.com/a", "headings": ["Unrelated"], "text": ""},
            {"url": "https://example.com/b", "headings": [question_text], "text": ""},
        ]
        out = ads_to_aeo.coverage([_question(question_text)], pages)
        self.assertEqual(len(out["covered"]), 1)
        self.assertEqual(out["covered"][0]["covered_by"], "https://example.com/b")


class TestBrief(unittest.TestCase):
    def test_returns_markdown_string(self):
        result = ads_to_aeo.brief(
            "How much does X cost?",
            [_term("x cost")],
            site_facts=None,
        )
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_includes_target_question(self):
        question = "How much does property management cost?"
        result = ads_to_aeo.brief(
            question,
            [_term("property management cost")],
            site_facts=None,
        )
        self.assertIn(question.lower(), result.lower())

    def test_includes_provided_site_facts(self):
        result = ads_to_aeo.brief(
            "How much does X cost?",
            [_term("x cost")],
            site_facts={"average_price": "$250", "founded": "2010"},
        )
        self.assertIn("$250", result)
        self.assertIn("2010", result)

    def test_does_not_include_unprovided_fact(self):
        with_fact = ads_to_aeo.brief(
            "How much does X cost?",
            [_term("x cost")],
            site_facts={"unique_fact": "ABC123"},
        )
        self.assertIn("ABC123", with_fact)

        without_fact = ads_to_aeo.brief(
            "How much does X cost?",
            [_term("x cost")],
            site_facts=None,
        )
        self.assertNotIn("ABC123", without_fact)

    def test_has_faq_jsonld_skeleton(self):
        result = ads_to_aeo.brief(
            "How much does X cost?",
            [_term("x cost")],
            site_facts=None,
        )
        lowered = result.lower()
        self.assertIn("faq", lowered)
        self.assertIn("json", lowered)

    def test_has_internal_link_placeholder(self):
        result = ads_to_aeo.brief(
            "How much does X cost?",
            [_term("x cost")],
            site_facts=None,
        )
        self.assertIn("link", result.lower())

    def test_suggests_multiple_h2_questions(self):
        result = ads_to_aeo.brief(
            "How much does property management cost?",
            [
                _term("best crm software"),
                _term("property management cost"),
                _term("acme vs beta"),
                _term("plumber near me"),
                _term("what is seo"),
            ],
            site_facts=None,
        )
        self.assertGreaterEqual(result.count("?"), 3)


class TestRoadmap(unittest.TestCase):
    def test_returns_expected_keys(self):
        out = ads_to_aeo.roadmap(
            [_term("best crm software", impressions=100)],
            [],
            top=10,
        )
        self.assertIn("gaps", out)
        self.assertIn("covered_count", out)
        self.assertIn("gap_count", out)
        self.assertIsInstance(out["gaps"], list)
        self.assertIsInstance(out["covered_count"], int)
        self.assertIsInstance(out["gap_count"], int)

    def test_top_limits_gaps(self):
        out = ads_to_aeo.roadmap(
            [
                _term("best crm software", impressions=100),
                _term("property management cost", impressions=90),
                _term("acme vs beta", impressions=80),
            ],
            [],
            top=2,
        )
        self.assertEqual(len(out["gaps"]), 2)
        self.assertGreaterEqual(out["gap_count"], 3)

    def test_counts_and_briefs(self):
        out = ads_to_aeo.roadmap(
            [
                _term("best crm software", impressions=100),
                _term("property management cost", impressions=90),
            ],
            [],
            top=10,
        )
        self.assertEqual(out["covered_count"], 0)
        self.assertEqual(len(out["gaps"]), out["gap_count"])
        for gap in out["gaps"]:
            self.assertIn("brief", gap)
            self.assertIsInstance(gap["brief"], str)

    def test_passes_site_facts_to_brief(self):
        out = ads_to_aeo.roadmap(
            [_term("best crm software", impressions=100)],
            [],
            top=10,
            site_facts={"price": "$999"},
        )
        self.assertTrue(out["gaps"])
        self.assertTrue(any("$999" in gap["brief"] for gap in out["gaps"]))

    def test_covered_count_with_matching_page(self):
        terms = [
            _term("best crm software", impressions=100),
            _term("property management cost", impressions=90),
        ]
        questions = ads_to_aeo.questions_from_terms(terms)
        covered_question = questions[0]["question"]
        pages = [
            {
                "url": "https://example.com/covered",
                "headings": [covered_question],
                "text": "",
            }
        ]
        out = ads_to_aeo.roadmap(terms, pages, top=10)
        self.assertGreaterEqual(out["covered_count"], 1)
        self.assertGreaterEqual(out["gap_count"], 1)
        self.assertLessEqual(len(out["gaps"]), out["gap_count"])


class TestEmptyInputs(unittest.TestCase):
    def test_empty_coverage_and_roadmap(self):
        coverage = ads_to_aeo.coverage([], [])
        self.assertEqual(coverage, {"covered": [], "gaps": []})

        roadmap = ads_to_aeo.roadmap([], [])
        self.assertEqual(roadmap["gaps"], [])
        self.assertEqual(roadmap["covered_count"], 0)
        self.assertEqual(roadmap["gap_count"], 0)


if __name__ == '__main__':
    unittest.main()
