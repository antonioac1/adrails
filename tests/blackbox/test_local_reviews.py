import datetime
import unittest

from adsops.local import reviews


# The module is expected to expose ToolError.  Fall back to Exception so the
# validation tests still exercise the refusal behaviour if it is re-exported
# differently.
ToolError = getattr(reviews, "ToolError", Exception)


def _rules_text(result):
    return " ".join(str(rule) for rule in result.get("rules", [])).lower()


def _has_emoji(value):
    emoji_ranges = (
        (0x1F300, 0x1FAFF),
        (0x2600, 0x27BF),
        (0x1F1E6, 0x1F1FF),
    )
    return any(
        start <= ord(char) <= end
        for char in value
        for start, end in emoji_ranges
    )


class ReviewsTestCase(unittest.TestCase):
    PLACE_ID = "ChIJN1t_tDeuEmsRUsoyG83frY4"
    LINK = "https://search.google.com/local/writereview?placeid=" + PLACE_ID

    def _sms_drafts(self, tone="friendly"):
        return reviews.request_drafts(
            biz_name="Acme Plumbing",
            customer_first_name="Dana",
            service="drain cleaning",
            link=self.LINK,
            channel="sms",
            tone=tone,
        )

    def test_review_link_builds_expected_url(self):
        self.assertEqual(
            reviews.review_link(self.PLACE_ID),
            "https://search.google.com/local/writereview?placeid=" + self.PLACE_ID,
        )

    def test_review_link_rejects_short_place_id(self):
        with self.assertRaises(ToolError):
            reviews.review_link("short")

    def test_review_link_rejects_invalid_place_id_characters(self):
        for bad_place_id in ("abc defghijk", "abc!defghijk", "abc/defghijk"):
            with self.subTest(place_id=bad_place_id):
                with self.assertRaises(ToolError):
                    reviews.review_link(bad_place_id)

    def test_request_drafts_returns_two_drafts_and_rules(self):
        result = self._sms_drafts()
        self.assertIn("drafts", result)
        self.assertIn("rules", result)
        self.assertIsInstance(result["drafts"], list)
        self.assertEqual(len(result["drafts"]), 2)
        self.assertTrue(all(isinstance(draft, str) for draft in result["drafts"]))
        self.assertIsInstance(result["rules"], list)
        self.assertGreater(len(result["rules"]), 0)

    def test_sms_drafts_obey_sms_constraints(self):
        result = self._sms_drafts()
        for draft in result["drafts"]:
            lower = draft.lower()
            self.assertLessEqual(len(draft), 300)
            self.assertEqual(draft.count(self.LINK), 1)
            self.assertIn("dana", lower)
            self.assertIn("reply stop to opt out", lower)

    def test_sms_drafts_have_no_emojis_by_default(self):
        result = self._sms_drafts()
        for draft in result["drafts"]:
            self.assertFalse(_has_emoji(draft), msg="SMS draft contains an emoji")

    def test_request_drafts_rules_forbid_review_gating(self):
        text = _rules_text(self._sms_drafts())
        self.assertTrue(
            "every customer" in text or "all customers" in text,
            msg="rules should require asking every customer",
        )
        self.assertTrue(
            any(
                phrase in text
                for phrase in (
                    "gating",
                    "sentiment",
                    "only happy",
                    "happy customers",
                )
            ),
            msg="rules should explicitly forbid review gating / sentiment filtering",
        )

    def test_request_drafts_rules_cover_incentives_timing_and_reminders(self):
        text = _rules_text(self._sms_drafts())
        self.assertIn("incentive", text)
        self.assertIn("24", text)
        self.assertIn("reminder", text)
        self.assertTrue(
            "3 day" in text or "3-day" in text or "three day" in text,
            msg="rules should mention one reminder after 3 days at most",
        )

    def test_request_drafts_supports_brief_email_variants(self):
        result = reviews.request_drafts(
            biz_name="Acme Plumbing",
            customer_first_name="Dana",
            service="drain cleaning",
            link=self.LINK,
            channel="email",
            tone="brief",
        )
        self.assertEqual(len(result["drafts"]), 2)
        self.assertIsInstance(result["rules"], list)
        self.assertTrue(all(isinstance(draft, str) for draft in result["drafts"]))

    def test_reply_draft_returns_documented_shape(self):
        result = reviews.reply_draft(
            {
                "rating": 5,
                "text": "The installation was excellent and the team was professional.",
                "author_first_name": "Dana",
            },
            "Acme Plumbing",
            owner_name="Pat",
        )
        self.assertIn("draft", result)
        self.assertIn("tone", result)
        self.assertIn("flags", result)
        self.assertIsInstance(result["draft"], str)
        self.assertIsInstance(result["tone"], str)
        self.assertIsInstance(result["flags"], list)
        self.assertLessEqual(len(result["draft"]), 600)

    def test_five_star_reply_references_a_detail_word_from_text(self):
        review = {
            "rating": 5,
            "text": "The installation was excellent and the team was professional.",
            "author_first_name": "Dana",
        }
        draft = reviews.reply_draft(review, "Acme Plumbing")["draft"].lower()
        candidates = [
            word.strip(".,!?;:").lower()
            for word in review["text"].split()
            if len(word.strip(".,!?;:")) > 4
        ]
        self.assertTrue(
            any(word in draft for word in candidates),
            msg="5-star reply should reference a detail word from the review text",
        )

    def test_reply_draft_does_not_leak_reviewer_pii(self):
        review_text = (
            "Great work. My name is John Smith. Call 555-123-4567 or email "
            "jane.doe@example.com. I live at 123 Main Street."
        )
        draft = reviews.reply_draft(
            {
                "rating": 5,
                "text": review_text,
                "author_first_name": "John",
            },
            "Acme Plumbing",
        )["draft"]
        lower = draft.lower()
        for leaked in (
            "john smith",
            "555-123-4567",
            "5551234567",
            "jane.doe@example.com",
            "123 main street",
        ):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, lower)
        self.assertLessEqual(len(draft), 600)

    def test_one_star_empty_text_flags_possible_fake(self):
        result = reviews.reply_draft(
            {"rating": 1, "text": "", "author_first_name": "Sam"},
            "Acme Plumbing",
        )
        self.assertIn("possible_fake", result["flags"])

    def test_safety_or_legal_mentions_are_flagged(self):
        result = reviews.reply_draft(
            {
                "rating": 1,
                "text": (
                    "I will call my attorney about the injury and the "
                    "insurance claim."
                ),
                "author_first_name": "Lee",
            },
            "Acme Plumbing",
        )
        self.assertIn("mentions_safety_or_legal", result["flags"])

    def test_three_star_reply_thanks_and_invites_contact(self):
        draft = reviews.reply_draft(
            {"rating": 3, "text": "It was okay.", "author_first_name": "Kim"},
            "Acme Plumbing",
        )["draft"].lower()
        self.assertIn("thank", draft)
        self.assertTrue(
            any(
                word in draft
                for word in ("contact", "call", "email", "message", "reach")
            ),
            msg="3-star reply should invite the customer to contact the business",
        )

    def test_low_star_reply_offers_offline_contact_and_is_short(self):
        result = reviews.reply_draft(
            {
                "rating": 2,
                "text": "The timing was not what I expected.",
                "author_first_name": "Kim",
            },
            "Acme Plumbing",
        )
        draft = result["draft"].lower()
        self.assertLessEqual(len(result["draft"]), 600)
        self.assertTrue(
            any(
                word in draft
                for word in ("contact", "call", "email", "message", "reach", "offline")
            ),
            msg="1-2 star reply should offer offline contact",
        )

    def _velocity_reviews(self):
        return [
            {"rating": 5, "created_at": "2024-05-30"},
            {"rating": 4, "created_at": "2024-05-20"},
            {"rating": 5, "created_at": "2024-04-15"},
            {"rating": 3, "created_at": "2024-01-01"},
        ]

    def test_velocity_basic_metrics(self):
        data = reviews.velocity(
            self._velocity_reviews(),
            today=datetime.date(2024, 6, 1),
        )
        self.assertEqual(data["count"], 4)
        self.assertAlmostEqual(data["rating"], 4.25, places=2)
        self.assertEqual(data["last_30d"], 2)
        self.assertEqual(data["last_90d"], 3)
        self.assertAlmostEqual(data["per_month_90d"], 1.0, places=2)
        self.assertEqual(data["days_since_last"], 2)
        self.assertNotEqual(data.get("recency_flag"), "stale")
        self.assertIsInstance(data["advice"], list)

    def test_velocity_needed_to_reach_math(self):
        data = reviews.velocity(
            self._velocity_reviews(),
            today=datetime.date(2024, 6, 1),
        )
        needed = data["needed_to_reach"]
        self.assertEqual(needed["4.5"], 2)
        self.assertEqual(needed["4.8"], 11)

    def test_velocity_impossible_five_star_target_is_none(self):
        data = reviews.velocity(
            [
                {"rating": 5, "created_at": "2024-05-30"},
                {"rating": 4, "created_at": "2024-05-29"},
            ],
            today=datetime.date(2024, 6, 1),
        )
        # 5.0 is only reachable if every existing review is already 5-star.
        self.assertIsNone(data["needed_to_reach"].get("5.0"))

    def test_velocity_marks_stale_when_last_review_over_30_days_old(self):
        data = reviews.velocity(
            [{"rating": 5, "created_at": "2024-04-01"}],
            today=datetime.date(2024, 6, 1),
        )
        self.assertEqual(data["days_since_last"], 61)
        self.assertEqual(data["recency_flag"], "stale")

    def test_velocity_handles_empty_reviews(self):
        data = reviews.velocity([], today=datetime.date(2024, 6, 1))
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["last_30d"], 0)
        self.assertEqual(data["last_90d"], 0)
        self.assertEqual(data["per_month_90d"], 0)
        self.assertIn(data["rating"], (0, 0.0, None))
        self.assertIsNone(data["days_since_last"])
        self.assertIsInstance(data["advice"], list)

    def test_velocity_compares_to_competitors(self):
        data = reviews.velocity(
            [
                {"rating": 5, "created_at": "2024-05-30"},
                {"rating": 5, "created_at": "2024-05-29"},
            ],
            today=datetime.date(2024, 6, 1),
            competitors=[{"name": "Acme", "count": 10, "rating": 4.8}],
        )
        self.assertEqual(len(data["vs_competitors"]), 1)
        item = data["vs_competitors"][0]
        self.assertEqual(item["name"], "Acme")
        self.assertIn("gap_count", item)
        self.assertIn("gap_rating", item)
        self.assertIsInstance(item["gap_count"], (int, float))
        self.assertIsInstance(item["gap_rating"], (int, float))

    def test_parse_gbp_reviews_empty_input(self):
        self.assertEqual(reviews.parse_gbp_reviews([]), [])

    def test_parse_gbp_reviews_maps_star_rating_words(self):
        words = ["ONE", "TWO", "THREE", "FOUR", "FIVE"]
        rows = [
            {
                "reviewId": "r{0}".format(index),
                "starRating": word,
                "comment": "Review text {0}".format(index),
                "createTime": "2024-01-01T00:00:00Z",
                "reviewer": {"displayName": "Ann Lee"},
            }
            for index, word in enumerate(words, start=1)
        ]
        parsed = reviews.parse_gbp_reviews(rows)
        self.assertEqual([row["rating"] for row in parsed], [1, 2, 3, 4, 5])
        self.assertEqual(
            [row["id"] for row in parsed],
            ["r1", "r2", "r3", "r4", "r5"],
        )
        for row in parsed:
            self.assertEqual(row["author_first_name"], "Ann")

    def test_parse_gbp_reviews_normalizes_author_and_replied(self):
        rows = [
            {
                "reviewId": "r1",
                "starRating": "FIVE",
                "comment": "Great service",
                "createTime": "2024-01-01T00:00:00Z",
                "reviewer": {"displayName": "Mary Jane Watson"},
                "reviewReply": {"comment": "Thank you"},
            }
        ]
        parsed = reviews.parse_gbp_reviews(rows)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["text"], "Great service")
        self.assertEqual(parsed[0]["author_first_name"], "Mary")
        self.assertTrue(parsed[0]["replied"])

    def test_parse_gbp_reviews_defaults_missing_comment_and_reply(self):
        rows = [
            {
                "reviewId": "r1",
                "starRating": "FOUR",
                "createTime": "2024-01-01T00:00:00Z",
                "reviewer": {"displayName": "Solo"},
            }
        ]
        parsed = reviews.parse_gbp_reviews(rows)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["text"], "")
        self.assertEqual(parsed[0]["author_first_name"], "Solo")
        self.assertFalse(parsed[0]["replied"])


if __name__ == "__main__":
    unittest.main()
