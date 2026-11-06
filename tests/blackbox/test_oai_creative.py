import unittest
from urllib.parse import urlsplit, parse_qs

from adsops.errors import RailViolation
from adsops.oai_creative import validate_card, add_utm, check_set


def _codes(items):
    result = []
    for item in items or []:
        if isinstance(item, dict):
            code = item.get("code")
            if code is not None:
                result.append(code)
        elif isinstance(item, str):
            result.append(item)
    return result


def _messages(items):
    return [it.get("message", "") for it in items or [] if isinstance(it, dict)]


def _valid_ad(**overrides):
    ad = {
        "title": "Great Coffee",
        "body": "Try our fresh coffee today.",
        "target_url": "https://example.com/coffee",
        "file_id": "file_123",
    }
    ad.update(overrides)
    return ad


def _ad_without_file_id(**overrides):
    ad = _valid_ad(**overrides)
    ad.pop("file_id", None)
    return ad


def _has_pair(duplicates, a, b):
    for pair in duplicates or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            if (pair[0] == a and pair[1] == b) or (pair[0] == b and pair[1] == a):
                return True
    return False


class TestValidateCard(unittest.TestCase):
    def test_valid_card(self):
        res = validate_card(_valid_ad())
        self.assertTrue(res["ok"])
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["stats"]["title_len"], len("Great Coffee"))
        self.assertEqual(res["stats"]["body_len"], len("Try our fresh coffee today."))

    def test_title_length_errors(self):
        for title in ["Hi", "x" * 51]:
            res = validate_card(_valid_ad(title=title))
            self.assertFalse(res["ok"])
            self.assertIn("title_length", _codes(res["errors"]))

    def test_body_length_error(self):
        res = validate_card(_valid_ad(body="x" * 101))
        self.assertFalse(res["ok"])
        self.assertIn("body_length", _codes(res["errors"]))

    def test_empty_text_error(self):
        res = validate_card(_valid_ad(title="", body=""))
        self.assertFalse(res["ok"])
        self.assertIn("empty_text", _codes(res["errors"]))

    def test_url_scheme_error(self):
        res = validate_card(_valid_ad(target_url="http://example.com/coffee"))
        self.assertFalse(res["ok"])
        self.assertIn("url_scheme", _codes(res["errors"]))

    def test_url_length_error(self):
        res = validate_card(_valid_ad(target_url="https://example.com/" + "a" * 2050))
        self.assertFalse(res["ok"])
        self.assertIn("url_length", _codes(res["errors"]))

    def test_url_domain_policy(self):
        policy = {"allowed_domains": ["example.com"]}

        allowed = validate_card(_valid_ad(target_url="https://example.com/x"), policy=policy)
        self.assertNotIn("url_domain", _codes(allowed["errors"]))

        other = validate_card(_valid_ad(target_url="https://other.com/x"), policy=policy)
        self.assertIn("url_domain", _codes(other["errors"]))

        sub = validate_card(_valid_ad(target_url="https://sub.example.com/x"), policy=policy)
        self.assertIn("url_domain", _codes(sub["errors"]))

        sub_allowed = validate_card(
            _valid_ad(target_url="https://sub.example.com/x"),
            policy={"allowed_domains": ["sub.example.com"]},
        )
        self.assertNotIn("url_domain", _codes(sub_allowed["errors"]))

    def test_empty_allowed_domains_ignored(self):
        res = validate_card(
            _valid_ad(target_url="https://anything.example/x"),
            policy={"allowed_domains": []},
        )
        self.assertNotIn("url_domain", _codes(res["errors"]))

    def test_missing_utm(self):
        policy = {"required_utm": {"utm_source": "chatgpt"}}

        present = validate_card(
            _valid_ad(target_url="https://example.com/x?utm_source=chatgpt"),
            policy=policy,
        )
        self.assertNotIn("missing_utm", _codes(present["errors"]))

        missing = validate_card(_valid_ad(target_url="https://example.com/x"), policy=policy)
        self.assertIn("missing_utm", _codes(missing["errors"]))

        wrong = validate_card(
            _valid_ad(target_url="https://example.com/x?utm_source=other"),
            policy=policy,
        )
        self.assertIn("missing_utm", _codes(wrong["errors"]))

    def test_images_rules(self):
        no_files = validate_card(_ad_without_file_id())
        self.assertIn("images", _codes(no_files["errors"]))

        one_in_list = validate_card(_ad_without_file_id(file_ids=["file_1"]))
        self.assertIn("images", _codes(one_in_list["errors"]))

        four = validate_card(_ad_without_file_id(file_ids=[f"file_{i}" for i in range(4)]))
        self.assertNotIn("images", _codes(four["errors"]))
        self.assertTrue(four["ok"])

        seven = validate_card(_ad_without_file_id(file_ids=[f"file_{i}" for i in range(7)]))
        self.assertIn("images", _codes(seven["errors"]))

        one_singular = validate_card(_valid_ad(file_id="file_1"))
        self.assertNotIn("images", _codes(one_singular["errors"]))
        self.assertTrue(one_singular["ok"])

    def test_banned_phrase(self):
        res = validate_card(
            _valid_ad(body="This is a miracle coffee."),
            policy={"banned_phrases": ["miracle"]},
        )
        self.assertFalse(res["ok"])
        self.assertIn("banned_phrase", _codes(res["errors"]))


class TestWarnings(unittest.TestCase):
    def _assert_warning(self, ad, code, policy=None):
        res = validate_card(ad, policy=policy)
        self.assertIn(code, _codes(res["warnings"]))
        return res

    def test_warning_all_caps_word(self):
        self._assert_warning(_valid_ad(body="FREE coffee"), "all_caps_word")

    def test_warning_exclamation(self):
        self._assert_warning(_valid_ad(body="Great! Amazing! Wow!"), "exclamation")

    def test_warning_superlative(self):
        res = self._assert_warning(_valid_ad(body="The best coffee"), "superlative")
        self.assertTrue(any("substantiation" in m for m in _messages(res["warnings"])))

    def test_warning_phone_in_text(self):
        self._assert_warning(_valid_ad(body="Call 555-123-4567"), "phone_in_text")

    def test_warning_no_brand(self):
        self._assert_warning(
            _valid_ad(body="Try our coffee"),
            "no_brand",
            policy={"brand": "Acme"},
        )

        with_brand = validate_card(
            _valid_ad(body="Try Acme coffee"),
            policy={"brand": "Acme"},
        )
        self.assertNotIn("no_brand", _codes(with_brand["warnings"]))

    def test_warning_title_equals_body(self):
        self._assert_warning(
            _valid_ad(title="Great Coffee", body="Great Coffee"),
            "title_equals_body",
        )

    def test_warning_question_title_without_answer(self):
        self._assert_warning(
            _valid_ad(title="Need coffee?", body="Fresh beans."),
            "question_title_without_answer",
        )

        with_cta = validate_card(
            _valid_ad(title="Need coffee?", body="Get fresh beans."),
        )
        self.assertNotIn("question_title_without_answer", _codes(with_cta["warnings"]))


class TestAddUtm(unittest.TestCase):
    def test_add_utm_merges_and_preserves(self):
        url = "https://example.com/path?ref=abc#frag"
        new_url = add_utm(url, {"utm_source": "chatgpt", "utm_medium": "cpc"})

        parts = urlsplit(new_url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.fragment, "frag")

        query = parse_qs(parts.query)
        self.assertEqual(query["ref"], ["abc"])
        self.assertEqual(query["utm_source"], ["chatgpt"])
        self.assertEqual(query["utm_medium"], ["cpc"])

    def test_add_utm_overrides_existing_utm(self):
        url = "https://example.com/path?utm_source=old&utm_medium=old"
        new_url = add_utm(url, {"utm_source": "chatgpt"})

        query = parse_qs(urlsplit(new_url).query)
        self.assertEqual(query["utm_source"], ["chatgpt"])
        self.assertEqual(query["utm_medium"], ["old"])

    def test_add_utm_requires_https(self):
        with self.assertRaises(RailViolation):
            add_utm("http://example.com/path", {"utm_source": "chatgpt"})


class TestCheckSet(unittest.TestCase):
    def _ad(self, i, title=None, body=None):
        return {
            "title": title if title is not None else f"Title {i}",
            "body": body if body is not None else f"Body {i}",
            "target_url": f"https://example.com/{i}",
            "file_id": f"file_{i}",
        }

    def test_all_valid(self):
        ads = [
            self._ad(0, "Alpha one"),
            self._ad(1, "Beta two"),
            self._ad(2, "Gamma three"),
        ]
        res = check_set(ads)

        self.assertTrue(res["ok"])
        self.assertEqual(len(res["per_ad"]), 3)
        for i, item in enumerate(res["per_ad"]):
            self.assertEqual(item.get("index"), i)
            self.assertTrue(item["ok"])
        self.assertEqual(res["diversity"], 3)

    def test_ok_false_if_any_invalid(self):
        ads = [self._ad(0), self._ad(1, title="Hi")]
        res = check_set(ads)

        self.assertFalse(res["ok"])
        self.assertTrue(res["per_ad"][0]["ok"])
        self.assertFalse(res["per_ad"][1]["ok"])

    def test_duplicates_title_and_body(self):
        title_dup = [self._ad(0, "Great Coffee"), self._ad(1, "  great   coffee  ")]
        res_title = check_set(title_dup)
        self.assertTrue(_has_pair(res_title["duplicates"], 0, 1))

        body_dup = [self._ad(0, "Alpha", "Fresh beans"), self._ad(1, "Beta", "fresh   BEANS")]
        res_body = check_set(body_dup)
        self.assertTrue(_has_pair(res_body["duplicates"], 0, 1))

    def test_diversity_distinct_first_words(self):
        ads = [
            self._ad(0, "Alpha one"),
            self._ad(1, "Alpha two"),
            self._ad(2, "Beta three"),
        ]
        res = check_set(ads)
        self.assertEqual(res["diversity"], 2)


if __name__ == '__main__':
    unittest.main()