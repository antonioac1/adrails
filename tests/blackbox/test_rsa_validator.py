import unittest
from adsops.rsa_validator import validate_rsa, suggest_fixes


def base_ad():
    return {
        "headlines": [
            {"text": "call us today", "pin": None},
            {"text": "Headline 2", "pin": None},
            {"text": "Headline 3", "pin": None},
            {"text": "Headline 4", "pin": None},
            {"text": "Headline 5", "pin": None},
            {"text": "Headline 6", "pin": None},
            {"text": "Headline 7", "pin": None},
            {"text": "Headline 8", "pin": None},
        ],
        "descriptions": [
            {"text": "Description 1", "pin": None},
            {"text": "Description 2", "pin": None},
            {"text": "Description 3", "pin": None},
        ],
        "final_url": "https://example.com",
        "path1": "path1",
        "path2": "path2",
    }


class TestRSAValidator(unittest.TestCase):
    def test_returns_documented_keys(self):
        result = validate_rsa(base_ad())
        self.assertIsInstance(result, dict)
        for key in ("ok", "errors", "warnings", "stats"):
            self.assertIn(key, result)
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["errors"], list)
        self.assertIsInstance(result["warnings"], list)
        self.assertIsInstance(result["stats"], dict)

    def test_valid_ad_is_ok(self):
        result = validate_rsa(base_ad())
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])

    def test_headline_and_description_counts(self):
        cases = [
            ("too few headlines", lambda ad: ad.__setitem__("headlines", ad["headlines"][:2])),
            (
                "too many headlines",
                lambda ad: ad.__setitem__(
                    "headlines",
                    [{"text": f"Headline {i}", "pin": None} for i in range(1, 17)],
                ),
            ),
            ("too few descriptions", lambda ad: ad.__setitem__("descriptions", ad["descriptions"][:1])),
            (
                "too many descriptions",
                lambda ad: ad.__setitem__(
                    "descriptions",
                    [{"text": f"Description {i}", "pin": None} for i in range(1, 6)],
                ),
            ),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                ad = base_ad()
                mutate(ad)
                result = validate_rsa(ad)
                self.assertFalse(result["ok"])
                self.assertTrue(result["errors"])

    def test_text_length_limits(self):
        long_headline = "The quick brown fox jumps over a"
        long_description = (
            "This description is intentionally made longer than ninety characters "
            "by adding many words to exceed the limit."
        )
        cases = [
            (
                "headline too long",
                lambda ad: ad["headlines"].__setitem__(0, {"text": long_headline, "pin": None}),
            ),
            (
                "description too long",
                lambda ad: ad["descriptions"].__setitem__(0, {"text": long_description, "pin": None}),
            ),
            ("path1 too long", lambda ad: ad.__setitem__("path1", "abcdefghijklmnop")),
            ("path2 too long", lambda ad: ad.__setitem__("path2", "abcdefghijklmnop")),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                ad = base_ad()
                mutate(ad)
                result = validate_rsa(ad)
                self.assertFalse(result["ok"])
                self.assertTrue(result["errors"])

    def test_empty_text(self):
        for field, index in (("headlines", 0), ("descriptions", 0)):
            with self.subTest(field=field):
                ad = base_ad()
                ad[field][index] = {"text": "", "pin": None}
                result = validate_rsa(ad)
                self.assertFalse(result["ok"])
                self.assertTrue(result["errors"])

    def test_duplicate_text_case_insensitive_whitespace_normalised(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "Buy Now", "pin": None}
        ad["headlines"][1] = {"text": "  buy   now  ", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

        ad = base_ad()
        ad["descriptions"][0] = {"text": "Great Service", "pin": None}
        ad["descriptions"][1] = {"text": "  great   service  ", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_final_url_validation(self):
        for url in ("http://example.com", "https://"):
            with self.subTest(url=url):
                ad = base_ad()
                ad["final_url"] = url
                result = validate_rsa(ad)
                self.assertFalse(result["ok"])
                self.assertTrue(result["errors"])

    def test_path2_without_path1(self):
        ad = base_ad()
        ad["path1"] = ""
        ad["path2"] = "abc"
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_pin_rules(self):
        ad = base_ad()
        for i in range(3):
            ad["headlines"][i]["pin"] = "HEADLINE_1"
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

        ad = base_ad()
        ad["headlines"] = [
            {"text": "One", "pin": "HEADLINE_1"},
            {"text": "Two", "pin": "HEADLINE_2"},
            {"text": "Three", "pin": "HEADLINE_3"},
        ]
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_excessive_punctuation(self):
        for text in ("Buy now!", "Great!!", "Why??"):
            with self.subTest(text=text):
                ad = base_ad()
                ad["headlines"][0] = {"text": text, "pin": None}
                result = validate_rsa(ad)
                self.assertFalse(result["ok"])
                self.assertTrue(result["errors"])

    def test_all_caps_words_and_allowlist(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "FREE QUOTE", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

        ad = base_ad()
        ad["headlines"][0] = {"text": "HVAC repair", "pin": None}
        result = validate_rsa(ad, {"allow_caps": ["HVAC"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])

    def test_phone_number(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "Call 555-123-4567", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_repeated_characters(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "Freeee", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_emoji_symbols(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "Great ★ deal", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_click_here(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "Click here", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_warning_few_headlines(self):
        ad = base_ad()
        ad["headlines"] = ad["headlines"][:7]
        result = validate_rsa(ad)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])

    def test_warning_few_descriptions(self):
        ad = base_ad()
        ad["descriptions"] = ad["descriptions"][:2]
        result = validate_rsa(ad)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])

    def test_warning_near_duplicate(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "quality price hvac repair", "pin": None}
        ad["headlines"][1] = {"text": "quality price hvac repair now", "pin": None}
        ad["headlines"][2] = {"text": "call now", "pin": None}
        result = validate_rsa(ad)
        self.assertTrue(result["ok"])
        self.assertTrue(
            any(
                isinstance(w, dict) and w.get("code") == "near_duplicate"
                for w in result["warnings"]
            )
        )

    def test_warning_no_keywords(self):
        policy = {"keywords": ["acme", "widget"]}
        result = validate_rsa(base_ad(), policy)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])

    def test_warning_no_call_to_action(self):
        ad = base_ad()
        ad["headlines"] = [{"text": f"Headline {i}", "pin": None} for i in range(1, 9)]
        result = validate_rsa(ad)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])

    def test_warning_superlative_needs_substantiation(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "Best service", "pin": None}
        result = validate_rsa(ad)
        self.assertTrue(result["ok"])
        self.assertTrue(
            any(
                isinstance(w, dict)
                and "substantiation" in w.get("message", "").lower()
                for w in result["warnings"]
            )
        )

    def test_banned_phrase_error(self):
        policy = {"banned_phrases": ["guaranteed"]}
        ad = base_ad()
        ad["headlines"][0] = {"text": "Guaranteed results", "pin": None}
        result = validate_rsa(ad, policy)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_cjk_width(self):
        ad = base_ad()
        ad["headlines"][0] = {"text": "漢字測試內容範例資料樣本語言文字", "pin": None}
        result = validate_rsa(ad)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

        ad = base_ad()
        ad["headlines"][0] = {"text": "漢字測試內容範例資料樣本語言文", "pin": None}
        result = validate_rsa(ad)
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])

    def test_stats_is_dict(self):
        result = validate_rsa(base_ad())
        self.assertIsInstance(result["stats"], dict)
        self.assertTrue(result["stats"])

    def test_suggest_fixes_returns_list_of_strings(self):
        ad = base_ad()
        ad["headlines"] = ad["headlines"][:2]
        result = validate_rsa(ad)
        fixes = suggest_fixes(result)
        self.assertIsInstance(fixes, list)
        self.assertTrue(all(isinstance(f, str) for f in fixes))


if __name__ == '__main__':
    unittest.main()