"""Unit tests for adsops.offline_conv.

These tests only rely on the documented public behaviour of the module:
normalisation helpers, local SHA-256 hashing, the Google click-conversion
payload builder, the OpenAI CAPI payload builder and the redaction helper.
"""

import hashlib
import json
import re
import unittest
from datetime import datetime

from adsops import offline_conv as oc

GOOGLE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
ELLIPSIS = "\u2026"

UTC_TIME = "2026-11-05T10:00:00+00:00"


def sha(text):
    """SHA-256 hex digest the module is expected to produce."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identifier_map(conversion):
    """Flatten a Google userIdentifiers list into a plain dict."""
    found = {}
    for item in conversion.get("userIdentifiers") or []:
        for key, value in item.items():
            if key == "addressInfo" and isinstance(value, dict):
                found.update(value)
            else:
                found[key] = value
    return found


class NormalizationTests(unittest.TestCase):
    def test_normalize_email_basic(self):
        self.assertEqual(oc.normalize_email("  John.Doe@Example.COM  "), "john.doe@example.com")

    def test_normalize_email_gmail_strips_dots_and_plus(self):
        self.assertEqual(oc.normalize_email("John.Doe+promo@Gmail.com"), "johndoe@gmail.com")

    def test_normalize_email_googlemail(self):
        self.assertEqual(oc.normalize_email("J.Doe+news@googlemail.com"), "jdoe@googlemail.com")

    def test_normalize_email_non_gmail_is_untouched(self):
        self.assertEqual(oc.normalize_email(" A.B+tag@Example.com "), "a.b+tag@example.com")

    def test_normalize_phone_ten_digits_uses_default_country(self):
        self.assertEqual(oc.normalize_phone("(555) 123-4567"), "+15551234567")
        self.assertEqual(oc.normalize_phone("0123456789", default_country="44"), "+440123456789")

    def test_normalize_phone_keeps_international_digits(self):
        self.assertEqual(oc.normalize_phone("+44 20 7946 0958"), "+442079460958")
        self.assertEqual(oc.normalize_phone("123456789012345"), "+123456789012345")

    def test_normalize_phone_rejects_length_outside_range(self):
        for bad in ("1234567", "1234567890123456", ""):
            with self.assertRaises(Exception):
                oc.normalize_phone(bad)

    def test_normalize_name_punctuation_and_spaces(self):
        self.assertEqual(oc.normalize_name("  Smith,   John.  "), "smith john")

    def test_normalize_name_keeps_hyphen(self):
        self.assertEqual(oc.normalize_name("Mary-Jane  Smith"), "mary-jane smith")

    def test_normalize_name_removes_apostrophe(self):
        # Punctuation is removed (not replaced by a space).
        self.assertEqual(oc.normalize_name("John  O'Brien "), "john obrien")

    def test_sha256_hex_matches_hashlib_and_is_lowercase(self):
        digest = oc.sha256_hex("a@b.com")
        self.assertEqual(digest, sha("a@b.com"))
        self.assertRegex(digest, HEX64_RE)
        self.assertNotEqual(oc.sha256_hex("a@b.com"), oc.sha256_hex("A@B.com"))


class GoogleClickConversionTests(unittest.TestCase):
    def test_basic_structure_and_local_hashing(self):
        rows = [{
            "gclid": "GC123",
            "email": "John.Doe+promo@gmail.com",
            "phone": "(555) 123-4567",
            "first_name": " John ",
            "last_name": "Smith-Jones",
            "conversion_time": UTC_TIME,
            "value": 42.5,
            "order_id": "ORD-1",
        }]
        payload = oc.build_google_click_conversions(rows, "customers/1/conversionActions/2")

        self.assertIsInstance(payload["conversions"], list)
        self.assertEqual(len(payload["conversions"]), 1)
        conv = payload["conversions"][0]

        self.assertEqual(conv["conversionAction"], "customers/1/conversionActions/2")
        self.assertRegex(conv["conversionDateTime"], GOOGLE_TIME_RE)
        self.assertEqual(conv["conversionValue"], 42.5)
        self.assertEqual(conv["currencyCode"], "USD")
        self.assertEqual(conv["orderId"], "ORD-1")
        self.assertEqual(conv["gclid"], "GC123")

        ids = identifier_map(conv)
        self.assertEqual(ids["hashedEmail"], sha("johndoe@gmail.com"))
        self.assertEqual(ids["hashedPhoneNumber"], sha("+15551234567"))
        self.assertEqual(ids["hashedFirstName"], sha("john"))
        self.assertEqual(ids["hashedLastName"], sha("smith-jones"))

    def test_time_conversion_iso_and_z(self):
        rows = [
            {"gclid": "G-Z", "conversion_time": "2026-11-05T10:00:00Z"},
            {"gclid": "G-OFF", "conversion_time": "2026-11-05T10:00:00-05:00"},
        ]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertEqual(len(payload["conversions"]), 2)
        first, second = payload["conversions"]

        self.assertEqual(first["conversionDateTime"], "2026-11-05 10:00:00+00:00")
        self.assertEqual(second["conversionDateTime"], "2026-11-05 10:00:00-05:00")

    def test_time_conversion_space_format(self):
        rows = [{"gclid": "G1", "conversion_time": "2026-11-05 10:00:00+02:00"}]
        payload = oc.build_google_click_conversions(rows, "act/1")
        time_value = payload["conversions"][0]["conversionDateTime"]
        self.assertRegex(time_value, GOOGLE_TIME_RE)
        self.assertEqual(time_value, "2026-11-05 10:00:00+02:00")

    def test_validate_only_and_partial_failure_always_set(self):
        rows = [
            {"gclid": "G1", "conversion_time": UTC_TIME},
            {"value": 1, "conversion_time": UTC_TIME},  # will be skipped
        ]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertIs(payload["validateOnly"], True)
        self.assertIs(payload["partialFailure"], True)
        self.assertEqual(len(payload["skipped"]), 1)

    def test_row_without_click_id_or_identifier_is_skipped(self):
        rows = [
            {"conversion_time": UTC_TIME, "value": 5, "order_id": "NO-ID"},
            {"gclid": "G1", "conversion_time": UTC_TIME},
        ]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertEqual(len(payload["conversions"]), 1)
        self.assertEqual(len(payload["skipped"]), 1)
        skipped = payload["skipped"][0]
        self.assertEqual(skipped["index"], 0)
        self.assertIn("reason", skipped)
        self.assertIsInstance(skipped["reason"], str)

    def test_skipped_index_refers_to_original_row(self):
        rows = [
            {"gclid": "G1", "conversion_time": UTC_TIME},
            {"conversion_time": UTC_TIME},  # no identifier -> skipped
            {"gclid": "G2", "conversion_time": UTC_TIME},
        ]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertEqual(len(payload["conversions"]), 2)
        self.assertEqual([s["index"] for s in payload["skipped"]], [1])

    def test_click_id_only_row_is_not_skipped(self):
        rows = [{"wbraid": "WB1", "conversion_time": UTC_TIME}]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertEqual(len(payload["conversions"]), 1)
        conv = payload["conversions"][0]
        self.assertEqual(conv.get("wbraid"), "WB1")
        self.assertFalse(conv.get("gclid"))

    def test_identifier_only_row_is_not_skipped(self):
        rows = [{"email": "a@example.com", "conversion_time": UTC_TIME}]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertEqual(len(payload["conversions"]), 1)
        conv = payload["conversions"][0]
        self.assertFalse(conv.get("gclid"))
        self.assertEqual(identifier_map(conv)["hashedEmail"], sha("a@example.com"))

    def test_duplicate_order_id_skips_later_rows(self):
        rows = [
            {"gclid": "G-FIRST", "conversion_time": UTC_TIME, "order_id": "DUP-1"},
            {"gclid": "G-SECOND", "conversion_time": UTC_TIME, "order_id": "DUP-1"},
        ]
        payload = oc.build_google_click_conversions(rows, "act/1")
        self.assertEqual(len(payload["conversions"]), 1)
        self.assertEqual(payload["conversions"][0]["gclid"], "G-FIRST")
        self.assertEqual([s["index"] for s in payload["skipped"]], [1])

    def test_custom_currency(self):
        rows = [{"gclid": "G1", "conversion_time": UTC_TIME, "value": 3}]
        payload = oc.build_google_click_conversions(rows, "act/1", currency="EUR")
        self.assertEqual(payload["conversions"][0]["currencyCode"], "EUR")

    def test_payload_never_contains_raw_pii(self):
        rows = [{
            "gclid": "G1",
            "email": "jane.secret@example.com",
            "phone": "(555) 987-6543",
            "first_name": "Jane",
            "last_name": "Secretson",
            "conversion_time": UTC_TIME,
            "value": 9,
            "order_id": "ORD-PII",
        }]
        dumped = json.dumps(oc.build_google_click_conversions(rows, "act/1"))
        self.assertNotIn("jane.secret@example.com", dumped)
        self.assertNotIn("jane.secret", dumped)
        self.assertNotIn("5559876543", dumped)
        self.assertNotIn("Secretson", dumped)


class OpenAICapiEventTests(unittest.TestCase):
    def test_basic_structure(self):
        rows = [{
            "email": "john.doe+promo@gmail.com",
            "phone": "(555) 123-4567",
            "conversion_time": UTC_TIME,
            "value": 12.5,
            "currency": "EUR",
            "order_id": "O-1",
            "obref": "ref-9",
        }]
        payload = oc.build_openai_capi_events(rows, "PX-123")
        self.assertIsInstance(payload["events"], list)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["skipped"], [])

        event = payload["events"][0]
        self.assertEqual(event["event_name"], "lead_created")
        self.assertEqual(event["event_id"], "O-1")
        self.assertIsInstance(event["event_time"], int)

        user = event["user"]
        self.assertEqual(user["email_sha256"], [sha("johndoe@gmail.com")])
        self.assertEqual(user["phone_sha256"], [sha("+15551234567")])
        self.assertEqual(user["obref"], "ref-9")

        custom = event["custom_data"]
        self.assertEqual(custom["value"], 12.5)
        self.assertEqual(custom["currency"], "EUR")

    def test_event_time_is_unix_seconds(self):
        rows = [{
            "email": "a@example.com",
            "conversion_time": UTC_TIME,
            "order_id": "O-1",
        }]
        payload = oc.build_openai_capi_events(rows, "PX-123")
        expected = int(datetime.fromisoformat(UTC_TIME).timestamp())
        self.assertEqual(payload["events"][0]["event_time"], expected)

    def test_event_id_falls_back_to_hash_of_row(self):
        rows = [{"email": "a@example.com", "conversion_time": UTC_TIME}]
        payload = oc.build_openai_capi_events(rows, "PX-123")
        event_id = payload["events"][0]["event_id"]
        self.assertRegex(event_id, HEX32_RE)
        self.assertNotEqual(event_id, sha("a@example.com"))

    def test_custom_event_name_and_multiple_rows(self):
        rows = [
            {"email": "a@example.com", "conversion_time": UTC_TIME, "order_id": "O-1"},
            {"email": "b@example.com", "conversion_time": UTC_TIME, "order_id": "O-2"},
        ]
        payload = oc.build_openai_capi_events(rows, "PX-123", event_name="purchase")
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual([e["event_name"] for e in payload["events"]],
                         ["purchase", "purchase"])
        self.assertEqual([e["event_id"] for e in payload["events"]], ["O-1", "O-2"])
        self.assertEqual(payload["events"][0]["user"]["email_sha256"],
                         [sha("a@example.com")])

    def test_missing_obref_is_none(self):
        rows = [{"email": "a@example.com", "conversion_time": UTC_TIME, "order_id": "O-1"}]
        payload = oc.build_openai_capi_events(rows, "PX-123")
        self.assertIsNone(payload["events"][0]["user"].get("obref"))

    def test_payload_never_contains_raw_pii(self):
        rows = [{
            "email": "jane.secret@example.com",
            "phone": "(555) 987-6543",
            "conversion_time": UTC_TIME,
            "value": 9,
            "order_id": "O-PII",
        }]
        dumped = json.dumps(oc.build_openai_capi_events(rows, "PX-123"))
        self.assertNotIn("jane.secret@example.com", dumped)
        self.assertNotIn("jane.secret", dumped)
        self.assertNotIn("5559876543", dumped)


class RedactionTests(unittest.TestCase):
    def test_redact_google_payload_truncates_hashes_only(self):
        rows = [{
            "gclid": "G1",
            "email": "a@example.com",
            "phone": "+15551234567",
            "first_name": "Ann",
            "last_name": "Lee",
            "conversion_time": UTC_TIME,
            "value": 7,
            "order_id": "ORD-77",
        }]
        payload = oc.build_google_click_conversions(rows, "act/1")
        original_ids = identifier_map(payload["conversions"][0])

        redacted = oc.redact_report(payload)
        conv = redacted["conversions"][0]
        redacted_ids = identifier_map(conv)

        for key in ("hashedEmail", "hashedPhoneNumber",
                    "hashedFirstName", "hashedLastName"):
            self.assertIn(key, redacted_ids)
            self.assertEqual(redacted_ids[key], original_ids[key][:8] + ELLIPSIS)

        # Non-hashed fields must survive untouched.
        self.assertEqual(conv["gclid"], "G1")
        self.assertEqual(conv["orderId"], "ORD-77")
        self.assertEqual(conv["conversionValue"], 7)
        self.assertEqual(conv["currencyCode"], "USD")
        self.assertEqual(conv["conversionAction"], "act/1")
        self.assertIs(redacted["validateOnly"], True)

    def test_redact_openai_payload(self):
        rows = [{
            "email": "a@example.com",
            "phone": "+15551234567",
            "conversion_time": UTC_TIME,
            "value": 4,
            "currency": "USD",
            "order_id": "O-1",
        }]
        payload = oc.build_openai_capi_events(rows, "PX-123")
        redacted = oc.redact_report(payload)
        user = redacted["events"][0]["user"]

        self.assertEqual(user["email_sha256"], [sha("a@example.com")[:8] + ELLIPSIS])
        self.assertEqual(user["phone_sha256"], [sha("+15551234567")[:8] + ELLIPSIS])
        self.assertEqual(redacted["events"][0]["event_id"], "O-1")
        self.assertEqual(redacted["events"][0]["custom_data"]["value"], 4)


class ModuleContractTests(unittest.TestCase):
    def test_field_map_present_for_openai_mapping(self):
        self.assertIsInstance(oc.FIELD_MAP, dict)


if __name__ == '__main__':
    unittest.main()