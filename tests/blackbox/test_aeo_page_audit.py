"""Tests for adsops.aeo.page_audit — answer-readiness scoring.

Only documented public behaviour is asserted: analyze()/compare() return
shapes, the nine documented signal keys, threshold behaviours, safety /
edge cases (empty documents, invalid JSON-LD, pronoun-led leads, stale
dates) and the derived answer_blocks / suggested_questions / findings.
All I/O is avoided; only in-memory HTML strings are used.
"""

import datetime
import unittest

from adsops.aeo import page_audit


SIGNAL_KEYS = (
    "answer_first",
    "question_headings",
    "faq_block",
    "structured_data",
    "specifics",
    "freshness",
    "citations",
    "entity_clarity",
    "scannability",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def fill(n, start="Widgets"):
    """Return a string of exactly ``n`` words (first word is ``start``)."""
    if n <= 0:
        return ""
    return " ".join([start] + ["content"] * (n - 1))


def doc(head="", body=""):
    return "<html><head>%s</head><body>%s</body></html>" % (head, body)


def h1(text):
    return "<h1>%s</h1>" % text


def h2(text):
    return "<h2>%s</h2>" % text


def p(text):
    return "<p>%s</p>" % text


def jsonld(payload):
    return '<script type="application/ld+json">%s</script>' % payload


def rich_doc(url="https://acme.com/guide"):
    """A page that should trip most of the documented signals."""
    today = datetime.date.today().isoformat()
    body = "".join([
        h1("Acme Widgets"),
        p(fill(50)),                                    # answer_first lead
        h2("How do I reset a widget?"),
        p(fill(50, "Reset")),
        h2("What does a widget cost?"),
        p(fill(50, "Prices")),
        h2("FAQ"),
        p(fill(30, "Answers")),
        "<ul><li>%s</li><li>%s</li></ul>" % (fill(8), fill(8)),
        p("Updated %s. Plans cost $49 for 3 units, $99 for 6 units, "
          "$199 for 12 units and $499 for 30 units." % today),
        '<a href="https://source-one.org/report">Source one</a>',
        '<a href="https://source-two.net/data">Source two</a>',
    ])
    head = jsonld('{"@context": "https://schema.org", "@type": "FAQPage", '
                  '"mainEntity": []}')
    return doc(head=head, body=body)


def field(entry, name):
    """Read a named value from a compare() entry (dict or object)."""
    if isinstance(entry, dict):
        return entry[name]
    return getattr(entry, name)


# --------------------------------------------------------------------------
# shape / contract
# --------------------------------------------------------------------------

class AnalyzeContractTests(unittest.TestCase):

    def test_returns_documented_top_level_keys(self):
        result = page_audit.analyze(
            doc(body=h1("Acme") + p(fill(40)))
        )
        for key in ("score", "grade", "signals", "findings",
                    "answer_blocks", "suggested_questions"):
            self.assertIn(key, result)
        self.assertIsInstance(result["signals"], dict)
        self.assertIsInstance(result["findings"], list)
        self.assertIsInstance(result["answer_blocks"], list)
        self.assertIsInstance(result["suggested_questions"], list)

    def test_signals_expose_all_documented_keys(self):
        result = page_audit.analyze(doc(body=h1("Acme")))
        for key in SIGNAL_KEYS:
            self.assertIn(key, result["signals"])

    def test_score_in_range_and_grade_is_a_string(self):
        pages = [doc(), rich_doc(), doc(body=h1("Acme") + p(fill(30)))]
        for html in pages:
            result = page_audit.analyze(html)
            self.assertGreaterEqual(result["score"], 0)
            self.assertLessEqual(result["score"], 100)
            self.assertIsInstance(result["grade"], str)
            self.assertTrue(result["grade"].strip())

    def test_empty_document_scores_low_and_reports_findings(self):
        result = page_audit.analyze("<html><body></body></html>")
        self.assertLess(result["score"], 30)
        self.assertTrue(result["findings"])

    def test_better_page_outranks_poor_page(self):
        poor = page_audit.analyze("<html><body><p>hello</p></body></html>")
        rich = page_audit.analyze(rich_doc(), url="https://acme.com/guide")
        self.assertGreater(rich["score"], poor["score"])
        self.assertGreaterEqual(rich["score"], 40)
        self.assertLessEqual(poor["score"], 40)

    def test_findings_are_one_per_missed_signal(self):
        result = page_audit.analyze("<html><body><p>Hello there.</p></body></html>")
        missed = [k for k, v in result["signals"].items() if not v["ok"]]
        self.assertEqual(len(result["findings"]), len(missed))
        self.assertTrue(result["findings"])


# --------------------------------------------------------------------------
# answer_first
# --------------------------------------------------------------------------

class AnswerFirstTests(unittest.TestCase):

    def test_self_contained_lead_scores_signal(self):
        html = doc(body=h1("Reset Guide") + p(fill(50)))
        self.assertTrue(page_audit.analyze(html)["signals"]["answer_first"]["ok"])

    def test_pronoun_led_lead_is_rejected(self):
        html = doc(body=h1("Reset Guide") + p(fill(50, "This")))
        self.assertFalse(page_audit.analyze(html)["signals"]["answer_first"]["ok"])

    def test_too_short_lead_is_rejected(self):
        html = doc(body=h1("Reset Guide") + p(fill(12)))
        self.assertFalse(page_audit.analyze(html)["signals"]["answer_first"]["ok"])

    def test_too_long_lead_is_rejected(self):
        html = doc(body=h1("Reset Guide") + p(fill(120)))
        self.assertFalse(page_audit.analyze(html)["signals"]["answer_first"]["ok"])


# --------------------------------------------------------------------------
# question_headings
# --------------------------------------------------------------------------

class QuestionHeadingTests(unittest.TestCase):

    def test_share_at_or_above_thirty_percent_passes(self):
        body = (h1("Acme")
                + h2("How do widgets work?")
                + h2("Pricing tiers")
                + h2("Contact sales"))
        self.assertTrue(
            page_audit.analyze(doc(body=body))["signals"]["question_headings"]["ok"]
        )

    def test_no_question_headings_fails(self):
        body = (h1("Acme")
                + h2("Pricing tiers")
                + h2("Contact sales")
                + h2("About the team"))
        self.assertFalse(
            page_audit.analyze(doc(body=body))["signals"]["question_headings"]["ok"]
        )


# --------------------------------------------------------------------------
# faq_block / structured_data
# --------------------------------------------------------------------------

class FaqAndStructuredDataTests(unittest.TestCase):

    def test_faq_section_heading_detected(self):
        body = h1("Acme") + h2("Frequently Asked Questions") + p(fill(30))
        self.assertTrue(
            page_audit.analyze(doc(body=body))["signals"]["faq_block"]["ok"]
        )

    def test_faqpage_jsonld_detected(self):
        head = jsonld('{"@context": "https://schema.org", "@type": "FAQPage"}')
        result = page_audit.analyze(doc(head=head, body=h1("Acme") + p(fill(30))))
        self.assertTrue(result["signals"]["faq_block"]["ok"])
        self.assertTrue(result["signals"]["structured_data"]["ok"])

    def test_no_faq_material_is_not_detected(self):
        body = h1("Acme") + h2("Pricing") + p(fill(30))
        self.assertFalse(
            page_audit.analyze(doc(body=body))["signals"]["faq_block"]["ok"]
        )

    def test_valid_jsonld_sets_structured_data(self):
        head = jsonld('{"@context": "https://schema.org", "@type": "Article"}')
        result = page_audit.analyze(doc(head=head, body=h1("Acme") + p(fill(30))))
        self.assertTrue(result["signals"]["structured_data"]["ok"])

    def test_invalid_jsonld_does_not_set_structured_data(self):
        head = jsonld('{this is not valid json')
        result = page_audit.analyze(doc(head=head, body=h1("Acme") + p(fill(30))))
        self.assertFalse(result["signals"]["structured_data"]["ok"])


# --------------------------------------------------------------------------
# specifics
# --------------------------------------------------------------------------

class SpecificsTests(unittest.TestCase):

    def test_numeric_density_meets_threshold(self):
        body = (h1("Pricing")
                + p("Plan A costs $10, Plan B costs $20, Plan C costs $30 "
                    "and Plan D costs $40 in 2024."))
        self.assertTrue(page_audit.analyze(doc(body=body))["signals"]["specifics"]["ok"])

    def test_text_without_numbers_or_prices_fails(self):
        body = h1("Acme") + p(fill(120))
        self.assertFalse(page_audit.analyze(doc(body=body))["signals"]["specifics"]["ok"])


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------

class FreshnessTests(unittest.TestCase):

    def test_recent_visible_date_passes(self):
        today = datetime.date.today().isoformat()
        body = h1("Acme") + p("Last updated %s." % today) + p(fill(40))
        self.assertTrue(page_audit.analyze(doc(body=body))["signals"]["freshness"]["ok"])

    def test_recent_time_element_passes(self):
        today = datetime.date.today().isoformat()
        body = (h1("Acme")
                + '<time datetime="%s">%s</time>' % (today, today)
                + p(fill(40)))
        self.assertTrue(page_audit.analyze(doc(body=body))["signals"]["freshness"]["ok"])

    def test_stale_date_fails(self):
        body = h1("Acme") + p("Last updated 1999-01-01.") + p(fill(40))
        self.assertFalse(page_audit.analyze(doc(body=body))["signals"]["freshness"]["ok"])

    def test_missing_date_fails(self):
        body = h1("Acme") + p(fill(40))
        self.assertFalse(page_audit.analyze(doc(body=body))["signals"]["freshness"]["ok"])


# --------------------------------------------------------------------------
# citations
# --------------------------------------------------------------------------

class CitationTests(unittest.TestCase):

    def test_two_outbound_sources_pass(self):
        body = (h1("Acme")
                + p(fill(40))
                + '<a href="https://source-one.org/report">one</a>'
                + '<a href="https://source-two.net/data">two</a>')
        result = page_audit.analyze(doc(body=body), url="https://acme.com/guide")
        self.assertTrue(result["signals"]["citations"]["ok"])

    def test_same_domain_links_do_not_count(self):
        body = (h1("Acme")
                + p(fill(40))
                + '<a href="https://acme.com/one">one</a>'
                + '<a href="https://acme.com/two">two</a>')
        result = page_audit.analyze(doc(body=body), url="https://acme.com/guide")
        self.assertFalse(result["signals"]["citations"]["ok"])

    def test_single_outbound_source_is_not_enough(self):
        body = (h1("Acme")
                + p(fill(40))
                + '<a href="https://source-one.org/report">one</a>')
        result = page_audit.analyze(doc(body=body), url="https://acme.com/guide")
        self.assertFalse(result["signals"]["citations"]["ok"])


# --------------------------------------------------------------------------
# entity_clarity / scannability
# --------------------------------------------------------------------------

class EntityAndScannabilityTests(unittest.TestCase):

    def test_h1_alone_satisfies_entity_clarity(self):
        result = page_audit.analyze(doc(body=h1("Acme Widgets")))
        self.assertTrue(result["signals"]["entity_clarity"]["ok"])

    def test_brand_in_first_paragraph_satisfies_entity_clarity(self):
        html = doc(body=p("Acme makes widgets for distributed teams."))
        result = page_audit.analyze(html, brand="Acme")
        self.assertTrue(result["signals"]["entity_clarity"]["ok"])

    def test_no_h1_and_no_brand_fails_entity_clarity(self):
        html = doc(body=p("Widgets are made for distributed teams."))
        result = page_audit.analyze(html)
        self.assertFalse(result["signals"]["entity_clarity"]["ok"])

    def test_list_with_short_paragraphs_is_scannable(self):
        body = (h1("Acme")
                + p(fill(30))
                + p(fill(30))
                + "<ul><li>%s</li><li>%s</li></ul>" % (fill(6), fill(6)))
        self.assertTrue(page_audit.analyze(doc(body=body))["signals"]["scannability"]["ok"])

    def test_no_list_or_table_fails_scannability(self):
        body = h1("Acme") + p(fill(20)) + p(fill(20))
        self.assertFalse(page_audit.analyze(doc(body=body))["signals"]["scannability"]["ok"])

    def test_table_also_satisfies_scannability(self):
        body = (h1("Acme")
                + p(fill(30))
                + "<table><tr><td>a</td><td>b</td></tr></table>")
        self.assertTrue(page_audit.analyze(doc(body=body))["signals"]["scannability"]["ok"])


# --------------------------------------------------------------------------
# answer_blocks / suggested_questions
# --------------------------------------------------------------------------

class AnswerBlockTests(unittest.TestCase):

    def test_passage_after_question_heading_becomes_block(self):
        body = (h1("Acme")
                + h2("How do I reset my widget?")
                + p(fill(60, "Reset")))
        blocks = page_audit.analyze(doc(body=body))["answer_blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertIn("reset", blocks[0]["question"].lower())
        self.assertIn("content", blocks[0]["answer"])
        self.assertIn("Reset", blocks[0]["answer"])

    def test_paragraph_outside_40_90_words_is_not_a_block(self):
        body = (h1("Acme")
                + h2("How do I reset my widget?")
                + p(fill(15)))
        blocks = page_audit.analyze(doc(body=body))["answer_blocks"]
        self.assertEqual(blocks, [])

    def test_paragraph_not_after_question_heading_is_not_a_block(self):
        body = (h1("Acme")
                + h2("Pricing tiers")
                + p(fill(60)))
        blocks = page_audit.analyze(doc(body=body))["answer_blocks"]
        self.assertEqual(blocks, [])


class SuggestedQuestionTests(unittest.TestCase):

    def test_uncovered_target_question_is_suggested(self):
        target = "How much does widget shipping cost to Europe?"
        body = h1("Acme") + h2("Pricing tiers") + p(fill(40))
        result = page_audit.analyze(doc(body=body), target_questions=(target,))
        self.assertIn(target, result["suggested_questions"])

    def test_covered_target_question_is_not_suggested(self):
        target = "How much does widget shipping cost to Europe?"
        body = h1("Acme") + h2(target) + p(fill(40))
        result = page_audit.analyze(doc(body=body), target_questions=(target,))
        self.assertNotIn(target, result["suggested_questions"])

    def test_no_target_questions_yields_empty_list(self):
        result = page_audit.analyze(doc(body=h1("Acme") + p(fill(40))))
        self.assertEqual(result["suggested_questions"], [])


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

class CompareTests(unittest.TestCase):

    def test_ranks_pages_by_score_descending(self):
        pages = [
            ("https://poor.example/a", "<html><body><p>hi</p></body></html>"),
            ("https://rich.example/b", rich_doc(url="https://rich.example/b")),
            ("https://mid.example/c",
             doc(body=h1("Acme Widgets") + p(fill(50)))),
        ]
        ranked = page_audit.compare(pages)
        self.assertEqual(len(ranked), 3)
        scores = [field(entry, "score") for entry in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(field(ranked[0], "delta_vs_best"), 0)
        self.assertIn("rich.example", str(ranked[0]))

    def test_empty_page_list_returns_empty(self):
        self.assertEqual(page_audit.compare([]), [])

    def test_single_page_has_zero_delta(self):
        ranked = page_audit.compare(
            [("https://acme.com/guide", rich_doc())]
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(field(ranked[0], "delta_vs_best"), 0)

    def test_compare_accepts_analyze_keywords(self):
        pages = [
            ("https://a.example/1", doc(body=h1("Acme") + p(fill(40)))),
            ("https://b.example/2", doc(body=p(fill(40)))),
        ]
        ranked = page_audit.compare(
            pages,
            brand="Acme",
            target_questions=("How do widgets work?",),
        )
        self.assertEqual(len(ranked), 2)


if __name__ == '__main__':
    unittest.main()