import json
import unittest

from adsops import kwplan

# The spec says validation failures raise ToolError.  Be tolerant about where
# the project defines it so this test file only depends on the public contract.
ToolError = getattr(kwplan, "ToolError", None)
if ToolError is None:
    try:
        from adsops.errors import ToolError as _ToolError
        ToolError = _ToolError
    except Exception:
        ToolError = Exception


def _idea(keyword, searches, competition="LOW", index=10, low=1.0, high=2.0):
    return {
        "keyword": keyword,
        "avg_monthly_searches": searches,
        "competition": competition,
        "competition_index": index,
        "low_bid": low,
        "high_bid": high,
    }


class BuildIdeasRequestTests(unittest.TestCase):
    def test_basic_defaults_and_keyword_seed(self):
        path, body = kwplan.build_ideas_request("1234567890", ["red shoes"])

        self.assertEqual(path, "customers/1234567890:generateKeywordIdeas")
        self.assertEqual(body["language"], "languageConstants/1000")
        self.assertEqual(body["geoTargetConstants"], ["geoTargetConstants/2840"])
        self.assertEqual(body["keywordPlanNetwork"], "GOOGLE_SEARCH")
        self.assertIs(body["includeAdultKeywords"], False)
        self.assertEqual(body["keywordSeed"]["keywords"], ["red shoes"])
        self.assertNotIn("keywordAndUrlSeed", body)

    def test_custom_geo_language_and_network(self):
        path, body = kwplan.build_ideas_request(
            "42",
            ["a", "b"],
            geo_ids=[2840, 2392],
            language_id=1033,
            network="GOOGLE_SEARCH_AND_PARTNERS",
        )

        self.assertEqual(path, "customers/42:generateKeywordIdeas")
        self.assertEqual(body["language"], "languageConstants/1033")
        self.assertEqual(
            body["geoTargetConstants"],
            ["geoTargetConstants/2840", "geoTargetConstants/2392"],
        )
        self.assertEqual(body["keywordPlanNetwork"], "GOOGLE_SEARCH_AND_PARTNERS")
        self.assertEqual(body["keywordSeed"]["keywords"], ["a", "b"])

    def test_url_uses_keyword_and_url_seed(self):
        url = "https://example.com/red-shoes"
        _, body = kwplan.build_ideas_request("1", ["red shoes"], url=url)

        self.assertIn("keywordAndUrlSeed", body)
        self.assertNotIn("keywordSeed", body)
        self.assertIn(url, json.dumps(body))
        self.assertIs(body["includeAdultKeywords"], False)

    def test_seed_count_and_length_boundaries_are_accepted(self):
        _, one = kwplan.build_ideas_request("1", ["x"])
        self.assertEqual(one["keywordSeed"]["keywords"], ["x"])

        twenty = ["seed%d" % i for i in range(20)]
        _, twenty_body = kwplan.build_ideas_request("1", twenty)
        self.assertEqual(twenty_body["keywordSeed"]["keywords"], twenty)

        eighty = "x" * 80
        _, eighty_body = kwplan.build_ideas_request("1", [eighty])
        self.assertEqual(eighty_body["keywordSeed"]["keywords"], [eighty])

    def test_invalid_seeds_raise_tool_error(self):
        invalid_seed_lists = [
            [],
            ["x"] * 21,
            [""],
            ["x" * 81],
            [123],
        ]
        for seeds in invalid_seed_lists:
            with self.subTest(seeds=seeds):
                with self.assertRaises(ToolError):
                    kwplan.build_ideas_request("1", seeds)


class ParseIdeasTests(unittest.TestCase):
    def test_parses_documented_fields_and_types(self):
        response = {
            "results": [
                {
                    "text": "red shoes",
                    "keywordIdeaMetrics": {
                        "avgMonthlySearches": "1500",
                        "competition": "MEDIUM",
                        "competitionIndex": "42",
                        "lowTopOfPageBidMicros": "1500000",
                        "highTopOfPageBidMicros": "2500000",
                    },
                }
            ]
        }

        ideas = kwplan.parse_ideas(response)
        self.assertEqual(len(ideas), 1)
        item = ideas[0]

        self.assertEqual(
            set(item.keys()),
            {
                "keyword",
                "avg_monthly_searches",
                "competition",
                "competition_index",
                "low_bid",
                "high_bid",
            },
        )
        self.assertEqual(item["keyword"], "red shoes")
        self.assertEqual(item["avg_monthly_searches"], 1500)
        self.assertIsInstance(item["avg_monthly_searches"], int)
        self.assertEqual(item["competition"], "MEDIUM")
        self.assertEqual(item["competition_index"], 42)
        self.assertIsInstance(item["competition_index"], int)
        self.assertAlmostEqual(item["low_bid"], 1.5)
        self.assertAlmostEqual(item["high_bid"], 2.5)
        self.assertIsInstance(item["low_bid"], float)
        self.assertIsInstance(item["high_bid"], float)

    def test_sorted_by_average_searches_descending(self):
        response = {
            "results": [
                {
                    "text": "small",
                    "keywordIdeaMetrics": {"avgMonthlySearches": "10"},
                },
                {
                    "text": "large",
                    "keywordIdeaMetrics": {"avgMonthlySearches": "100"},
                },
                {
                    "text": "medium",
                    "keywordIdeaMetrics": {"avgMonthlySearches": "50"},
                },
            ]
        }

        ideas = kwplan.parse_ideas(response)
        self.assertEqual([i["keyword"] for i in ideas], ["large", "medium", "small"])
        self.assertEqual([i["avg_monthly_searches"] for i in ideas], [100, 50, 10])

    def test_competition_values_are_preserved(self):
        response = {
            "results": [
                {
                    "text": "a",
                    "keywordIdeaMetrics": {
                        "avgMonthlySearches": "4",
                        "competition": "LOW",
                        "competitionIndex": "1",
                    },
                },
                {
                    "text": "b",
                    "keywordIdeaMetrics": {
                        "avgMonthlySearches": "3",
                        "competition": "HIGH",
                        "competitionIndex": "2",
                    },
                },
                {
                    "text": "c",
                    "keywordIdeaMetrics": {
                        "avgMonthlySearches": "2",
                        "competition": "UNSPECIFIED",
                        "competitionIndex": "3",
                    },
                },
            ]
        }

        ideas = kwplan.parse_ideas(response)
        by_keyword = {i["keyword"]: i for i in ideas}
        self.assertEqual(by_keyword["a"]["competition"], "LOW")
        self.assertEqual(by_keyword["b"]["competition"], "HIGH")
        self.assertEqual(by_keyword["c"]["competition"], "UNSPECIFIED")

    def test_empty_results_return_empty_list(self):
        self.assertEqual(kwplan.parse_ideas({"results": []}), [])


class ClusterTests(unittest.TestCase):
    def test_groups_by_informative_token_and_sums_searches(self):
        ideas = [
            _idea("widget a", 10),
            _idea("widget b", 20),
            _idea("widget c", 30),
            _idea("widget d", 40),
            _idea("widget e", 50),
            _idea("widget f", 60),
            _idea("gadget a", 70),
            _idea("gadget b", 80),
            _idea("gadget c", 90),
            _idea("gadget d", 100),
        ]

        clusters = kwplan.cluster(ideas, min_shared=4)
        self.assertEqual(len(clusters), 2)

        by_theme = {c["theme"].lower(): c for c in clusters}
        self.assertIn("widget", by_theme)
        self.assertIn("gadget", by_theme)

        widget = by_theme["widget"]
        self.assertEqual(
            set(widget["keywords"]),
            {"widget a", "widget b", "widget c", "widget d", "widget e", "widget f"},
        )
        self.assertEqual(widget["searches"], 210)

        gadget = by_theme["gadget"]
        self.assertEqual(
            set(gadget["keywords"]),
            {"gadget a", "gadget b", "gadget c", "gadget d"},
        )
        self.assertEqual(gadget["searches"], 340)

    def test_drops_tokens_in_more_than_60_percent_of_ideas(self):
        ideas = [
            _idea("common theme a", 10),
            _idea("common theme b", 20),
            _idea("common other c", 30),
            _idea("common other d", 40),
            _idea("theme unique e", 50),
        ]

        clusters = kwplan.cluster(ideas, min_shared=2)
        self.assertEqual(len(clusters), 2)

        by_theme = {c["theme"].lower(): c for c in clusters}
        self.assertIn("theme", by_theme)
        self.assertIn("other", by_theme)
        self.assertNotIn("common", by_theme)

        theme = by_theme["theme"]
        self.assertEqual(
            set(theme["keywords"]),
            {"common theme a", "common theme b", "theme unique e"},
        )
        self.assertEqual(theme["searches"], 80)

        other = by_theme["other"]
        self.assertEqual(
            set(other["keywords"]),
            {"common other c", "common other d"},
        )
        self.assertEqual(other["searches"], 70)


class IntentTests(unittest.TestCase):
    def assert_intents(self, keywords, expected):
        for keyword in keywords:
            with self.subTest(keyword=keyword):
                self.assertEqual(kwplan.intent(keyword), expected)

    def test_transactional_keywords(self):
        self.assert_intents(
            ["plumber near me", "hire plumber"],
            "transactional",
        )

    def test_commercial_keywords(self):
        self.assert_intents(
            ["plumber cost", "plumber price", "best plumber", "plumber vs electrician"],
            "commercial",
        )

    def test_informational_keywords(self):
        self.assert_intents(
            ["how to fix a sink", "what is a valve", "why is my pipe leaking"],
            "informational",
        )

    def test_navigational_keywords(self):
        self.assert_intents(
            ["login to account", "facebook login"],
            "navigational",
        )

    def test_intent_returns_allowed_category(self):
        allowed = {
            "transactional",
            "commercial",
            "informational",
            "navigational",
        }
        self.assertIn(kwplan.intent("blue widgets"), allowed)


class PlanSharedListTests(unittest.TestCase):
    def _add_texts(self, add):
        texts = []
        for item in add:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    texts.append(item["text"])
                elif (
                    isinstance(item.get("keyword"), dict)
                    and isinstance(item["keyword"].get("text"), str)
                ):
                    texts.append(item["keyword"]["text"])
        return [t.lower() for t in texts]

    def test_adds_new_items_and_remove_is_always_empty(self):
        current = [{"text": "Cats", "match_type": "BROAD"}]
        desired = [
            {"text": "cats", "match_type": "BROAD"},
            {"text": "Dogs", "match_type": "PHRASE"},
        ]

        result = kwplan.plan_shared_list(current, desired)

        self.assertEqual(result["remove"], [])
        self.assertEqual(len(result["add"]), 1)
        self.assertIn("dogs", self._add_texts(result["add"]))
        self.assertIsInstance(result["note"], str)
        self.assertTrue(result["note"])

    def test_case_insensitive_dedupe_within_desired(self):
        desired = [
            {"text": "cats", "match_type": "BROAD"},
            {"text": "CATS", "match_type": "BROAD"},
            {"text": "dogs", "match_type": "BROAD"},
        ]

        result = kwplan.plan_shared_list([], desired)

        self.assertEqual(result["remove"], [])
        self.assertEqual(len(result["add"]), 2)
        self.assertEqual(
            sorted(self._add_texts(result["add"])),
            ["cats", "dogs"],
        )

    def test_no_adds_when_all_desired_already_present(self):
        current = [
            {"text": "cats", "match_type": "BROAD"},
            {"text": "dogs", "match_type": "PHRASE"},
        ]
        desired = [{"text": "CATS", "match_type": "BROAD"}]

        result = kwplan.plan_shared_list(current, desired)

        self.assertEqual(result["add"], [])
        self.assertEqual(result["remove"], [])
        self.assertIsInstance(result["note"], str)
        self.assertTrue(result["note"])  # reports the already-present desired item

    def test_current_extras_are_not_removed(self):
        current = [
            {"text": "cats", "match_type": "BROAD"},
            {"text": "dogs", "match_type": "PHRASE"},
        ]
        desired = [{"text": "cats", "match_type": "BROAD"}]

        result = kwplan.plan_shared_list(current, desired)

        self.assertEqual(result["remove"], [])
        self.assertEqual(result["add"], [])

    def test_match_type_survives_in_add_plan(self):
        desired = [{"text": "free shipping", "match_type": "PHRASE"}]

        result = kwplan.plan_shared_list([], desired)

        self.assertEqual(len(result["add"]), 1)
        serialized = json.dumps(result["add"]).lower()
        self.assertIn("free shipping", serialized)
        self.assertIn("phrase", serialized)


class SharedListOpsTests(unittest.TestCase):
    def test_builds_create_operations(self):
        resource = "customers/1/sharedSets/abc"
        adds = [
            {"text": "free", "match_type": "BROAD"},
            {"text": "cheap", "match_type": "PHRASE"},
        ]

        ops = kwplan.shared_list_ops(resource, adds)

        self.assertEqual(
            ops,
            [
                {
                    "create": {
                        "sharedSet": resource,
                        "keyword": {"text": "free", "matchType": "BROAD"},
                    }
                },
                {
                    "create": {
                        "sharedSet": resource,
                        "keyword": {"text": "cheap", "matchType": "PHRASE"},
                    }
                },
            ],
        )

    def test_empty_adds_returns_empty_list(self):
        self.assertEqual(kwplan.shared_list_ops("customers/1/sharedSets/x", []), [])


if __name__ == '__main__':
    unittest.main()