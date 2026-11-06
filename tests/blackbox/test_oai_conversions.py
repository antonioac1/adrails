import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from adsops import oai_conversions as oc


NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def iso(dt):
    return dt.isoformat()


def base_row(**overrides):
    row = {
        "id": "lead-1",
        "created_at": iso(NOW - timedelta(days=1)),
        "email": "Alice@Example.COM",
        "phone": "+1 (415) 555-2671",
        "first_name": "Alice",
        "last_name": "Smith",
        "value": 42.0,
        "oppref": "oppref_abc",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94107",
        "country": "US",
    }
    row.update(overrides)
    return row


def _call_args(call):
    if call.args:
        return call.args
    return (
        call.kwargs["method"],
        call.kwargs["url"],
        call.kwargs["headers"],
        call.kwargs["body_bytes"],
    )


def _header(headers, name):
    if hasattr(headers, "items"):
        items = headers.items()
    else:
        items = headers
    for key, value in items:
        if key.lower() == name.lower():
            return value
    return None


def _payload(call):
    body = _call_args(call)[3]
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


class TestOAIConversions(unittest.TestCase):
    def test_normalize_email(self):
        self.assertEqual(
            oc.normalize_email("  Foo@Example.COM  "),
            "foo@example.com",
        )

    def test_normalize_phone_valid(self):
        self.assertEqual(
            oc.normalize_phone("+1 (415) 555-2671"),
            "14155552671",
        )
        self.assertEqual(oc.normalize_phone("00012345678"), "12345678")
        self.assertEqual(oc.normalize_phone("12345678"), "12345678")
        self.assertEqual(
            oc.normalize_phone("123456789012345"),
            "123456789012345",
        )

    def test_normalize_phone_invalid(self):
        self.assertIsNone(oc.normalize_phone("1234567"))
        self.assertIsNone(oc.normalize_phone("1234567890123456"))
        self.assertIsNone(oc.normalize_phone("abc"))

    def test_normalize_name(self):
        self.assertEqual(oc.normalize_name(" Mary Jane "), "maryjane")
        self.assertEqual(oc.normalize_name("O'Connor"), "oconnor")
        self.assertEqual(oc.normalize_name("José"), "josé")
        self.assertEqual(oc.normalize_name("Anne-Marie"), "annemarie")

    def test_sha256_hex(self):
        self.assertEqual(
            oc.sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(len(oc.sha256_hex("")), 64)

    def test_build_events_basic_and_hashed_user_data(self):
        row = base_row()
        res = oc.build_events([row], now_ms=NOW_MS)

        self.assertIn("events", res)
        self.assertIn("skipped", res)
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(res["skipped"], [])

        ev = res["events"][0]
        self.assertEqual(
            ev["timestamp_ms"],
            int((NOW - timedelta(days=1)).timestamp() * 1000),
        )
        self.assertEqual(ev["action_source"], "offline")
        self.assertEqual(ev["oppref"], "oppref_abc")

        user = ev["user"]
        self.assertEqual(
            user["emails_sha256"],
            [oc.sha256_hex(oc.normalize_email(row["email"]))],
        )
        self.assertEqual(
            user["phone_numbers_sha256"],
            [oc.sha256_hex(oc.normalize_phone(row["phone"]))],
        )
        self.assertEqual(
            user["first_names_sha256"],
            [oc.sha256_hex(oc.normalize_name(row["first_name"]))],
        )
        self.assertEqual(
            user["last_names_sha256"],
            [oc.sha256_hex(oc.normalize_name(row["last_name"]))],
        )
        self.assertEqual(user["cities"], [row["city"]])
        self.assertEqual(user["regions"], [row["state"]])
        self.assertEqual(user["postal_codes"], [row["zip"]])
        self.assertEqual(user["countries"], [row["country"]])

        self.assertEqual(ev["data"]["amount"], 4200)
        self.assertIsInstance(ev["data"]["amount"], int)
        self.assertEqual(ev["data"]["currency"], "USD")

    def test_build_events_deterministic_event_id(self):
        row = base_row(id="lead-deterministic")
        res1 = oc.build_events([row], now_ms=NOW_MS)
        res2 = oc.build_events([row], now_ms=NOW_MS)

        event_id = res1["events"][0]["id"]
        self.assertIsInstance(event_id, str)
        self.assertTrue(event_id)
        self.assertEqual(event_id, res2["events"][0]["id"])

        other = base_row(id="lead-other")
        res3 = oc.build_events([other], now_ms=NOW_MS)
        self.assertNotEqual(event_id, res3["events"][0]["id"])

    def test_build_events_skips_too_old(self):
        row = base_row(
            id="old",
            created_at=iso(NOW - timedelta(days=8)),
        )
        res = oc.build_events([row], now_ms=NOW_MS)
        self.assertEqual(res["events"], [])
        self.assertEqual(len(res["skipped"]), 1)
        self.assertEqual(res["skipped"][0]["index"], 0)
        self.assertIn("reason", res["skipped"][0])

    def test_build_events_skips_future_beyond_five_minutes(self):
        row = base_row(
            id="future",
            created_at=iso(NOW + timedelta(minutes=10)),
        )
        res = oc.build_events([row], now_ms=NOW_MS)
        self.assertEqual(res["events"], [])
        self.assertEqual(len(res["skipped"]), 1)

    def test_build_events_allows_near_future(self):
        row = base_row(
            id="near-future",
            created_at=iso(NOW + timedelta(minutes=2)),
        )
        res = oc.build_events([row], now_ms=NOW_MS)
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(res["skipped"], [])

    def test_build_events_max_age_days(self):
        row = base_row(
            id="three-days-old",
            created_at=iso(NOW - timedelta(days=3)),
        )
        res1 = oc.build_events([row], now_ms=NOW_MS, max_age_days=1)
        self.assertEqual(len(res1["events"]), 0)

        res2 = oc.build_events([row], now_ms=NOW_MS, max_age_days=7)
        self.assertEqual(len(res2["events"]), 1)

    def test_build_events_skips_no_identifier(self):
        row = {
            "id": "no-identifier",
            "created_at": iso(NOW - timedelta(days=1)),
            "value": 10.0,
        }
        res = oc.build_events([row], now_ms=NOW_MS)
        self.assertEqual(res["events"], [])
        self.assertEqual(len(res["skipped"]), 1)

    def test_build_events_skips_duplicate_ids(self):
        r1 = base_row(id="duplicate", email="a@example.com")
        r2 = base_row(id="duplicate", email="b@example.com")
        res = oc.build_events([r1, r2], now_ms=NOW_MS)

        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(len(res["skipped"]), 1)
        self.assertIn(res["skipped"][0]["index"], (0, 1))

    def test_build_events_action_source_and_event_type(self):
        row = base_row(id="custom-source")
        res = oc.build_events(
            [row],
            now_ms=NOW_MS,
            event_type="order_created",
            action_source="physical_store",
        )
        ev = res["events"][0]
        self.assertEqual(ev["type"], "order_created")
        self.assertEqual(ev["action_source"], "physical_store")

    def test_build_events_currency_and_amount(self):
        row = base_row(id="eur-event", value=100.0)
        res = oc.build_events([row], now_ms=NOW_MS, currency="EUR")
        ev = res["events"][0]
        self.assertEqual(ev["data"]["currency"], "EUR")
        self.assertEqual(ev["data"]["amount"], 10000)
        self.assertIsInstance(ev["data"]["amount"], int)

    def test_build_events_never_emits_raw_identifiers(self):
        row = base_row(id="raw-check")
        res = oc.build_events([row], now_ms=NOW_MS)
        blob = json.dumps(res)

        for raw in (
            row["email"],
            row["phone"],
            row["first_name"],
            row["last_name"],
        ):
            self.assertNotIn(raw, blob)

        ev = res["events"][0]
        for forbidden in ("email", "phone", "first_name", "last_name"):
            self.assertNotIn(forbidden, ev)
            self.assertNotIn(forbidden, ev["user"])

    def test_build_events_plural_lists_within_api_limit(self):
        row = base_row(id="plural-limit")
        res = oc.build_events([row], now_ms=NOW_MS)
        user = res["events"][0]["user"]

        for key in (
            "emails_sha256",
            "phone_numbers_sha256",
            "first_names_sha256",
            "last_names_sha256",
            "cities",
            "regions",
            "postal_codes",
            "countries",
        ):
            self.assertLessEqual(len(user.get(key, [])), 3)

    def test_send_default_validate_only_and_auth(self):
        events = [
            {
                "id": "e1",
                "type": "order_created",
                "timestamp_ms": NOW_MS,
                "action_source": "offline",
                "data": {"type": "customer_action"},
            }
        ]
        http = mock.Mock(return_value=(200, '{"ok": true}'))
        res = oc.send(events, "PX123", "SECRETKEY", http)

        self.assertEqual(http.call_count, 1)
        method, url, headers, _body = _call_args(http.call_args)
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://bzr.openai.com/v1/events?pid=PX123")
        self.assertEqual(_header(headers, "Authorization"), "Bearer SECRETKEY")
        self.assertTrue(
            _header(headers, "Content-Type").lower().startswith("application/json")
        )

        payload = _payload(http.call_args)
        self.assertEqual(payload["events"], events)
        self.assertIs(payload["validate_only"], True)
        self.assertEqual(payload["integration_source"], "adrails")

        self.assertEqual(res["batches"], 1)
        self.assertEqual(res["sent"], len(events))
        self.assertIs(res["validate_only"], True)
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["status"], 200)
        self.assertIs(res["results"][0]["ok"], True)
        self.assertNotIn("SECRETKEY", json.dumps(res))

    def test_send_validate_only_false(self):
        events = [{"id": "e1"}]
        http = mock.Mock(return_value=(200, "{}"))
        res = oc.send(events, "PX", "KEY", http, validate_only=False)

        payload = _payload(http.call_args)
        self.assertIs(payload["validate_only"], False)
        self.assertIs(res["validate_only"], False)

    def test_send_batches(self):
        events = [{"id": f"e{i}"} for i in range(5)]
        http = mock.Mock(return_value=(200, "{}"))
        res = oc.send(events, "PX", "KEY", http, validate_only=False, batch_size=2)

        self.assertEqual(http.call_count, 3)
        sizes = []
        for call in http.call_args_list:
            sizes.append(len(_payload(call)["events"]))

        self.assertEqual(sum(sizes), 5)
        self.assertTrue(all(size <= 2 for size in sizes))
        self.assertEqual(res["batches"], 3)
        self.assertEqual(res["sent"], 5)

    def test_send_default_batch_limit(self):
        events = [{"id": f"e{i}"} for i in range(1500)]
        http = mock.Mock(return_value=(200, "{}"))
        res = oc.send(events, "PX", "KEY", http)

        self.assertEqual(http.call_count, 2)
        sizes = []
        for call in http.call_args_list:
            sizes.append(len(_payload(call)["events"]))

        self.assertEqual(sum(sizes), 1500)
        self.assertTrue(all(size <= 1000 for size in sizes))
        self.assertEqual(res["batches"], 2)
        self.assertEqual(res["sent"], 1500)

    def test_send_error_scrubs_key(self):
        events = [{"id": "e1"}]
        http = mock.Mock(return_value=(400, '{"error": "bad SECRETKEY"}'))
        res = oc.send(events, "PX", "SECRETKEY", http)

        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["status"], 400)
        self.assertFalse(res["results"][0]["ok"])
        self.assertNotIn("SECRETKEY", json.dumps(res))

    def test_send_integration_source(self):
        events = [{"id": "e1"}]
        http = mock.Mock(return_value=(200, "{}"))
        oc.send(
            events,
            "PX",
            "KEY",
            http,
            integration_source="acme_measurement",
        )

        payload = _payload(http.call_args)
        self.assertEqual(payload["integration_source"], "acme_measurement")

    def test_redact_preview_basic(self):
        h1 = "a" * 64
        h2 = "b" * 64
        events = [
            {
                "id": "e1",
                "user": {
                    "emails_sha256": [h1],
                    "phone_numbers_sha256": [h2],
                    "cities": ["SF"],
                },
            },
            {"id": "e2", "user": {"first_names_sha256": [h1]}},
            {"id": "e3", "user": {"last_names_sha256": [h2]}},
            {"id": "e4", "user": {"emails_sha256": [h1]}},
        ]

        preview = oc.redact_preview(events, n=3)

        self.assertEqual(len(preview), 3)
        self.assertEqual(preview[0]["id"], "e1")
        self.assertEqual(
            preview[0]["user"]["emails_sha256"][0],
            "a" * 8 + "…",
        )
        self.assertEqual(
            preview[0]["user"]["phone_numbers_sha256"][0],
            "b" * 8 + "…",
        )
        self.assertEqual(
            preview[1]["user"]["first_names_sha256"][0],
            "a" * 8 + "…",
        )
        self.assertEqual(
            preview[2]["user"]["last_names_sha256"][0],
            "b" * 8 + "…",
        )
        self.assertEqual(preview[0]["user"]["cities"], ["SF"])

    def test_redact_preview_default_n(self):
        events = [
            {"id": f"e{i}", "user": {"emails_sha256": ["c" * 64]}}
            for i in range(5)
        ]

        preview = oc.redact_preview(events)

        self.assertEqual(len(preview), 3)
        self.assertEqual([event["id"] for event in preview], ["e0", "e1", "e2"])
        self.assertEqual(
            preview[2]["user"]["emails_sha256"][0],
            "c" * 8 + "…",
        )

    def test_redact_preview_n_larger_than_list(self):
        events = [{"id": "e1", "user": {"emails_sha256": ["d" * 64]}}]
        preview = oc.redact_preview(events, n=10)

        self.assertEqual(len(preview), 1)
        self.assertEqual(
            preview[0]["user"]["emails_sha256"][0],
            "d" * 8 + "…",
        )


if __name__ == '__main__':
    unittest.main()
