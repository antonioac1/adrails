import unittest

from adsops.change_history import QUERY, normalize, classify, drift_report
from adsops.errors import ToolError


def _row(
    changeDateTime="2024-01-01 10:00:00",
    changeResourceType="CAMPAIGN",
    changeResourceName="customers/123/campaigns/456",
    clientType="GOOGLE_ADS_API",
    userEmail="alice@example.com",
    resourceChangeOperation="UPDATE",
    changedFields="status,budget.amount_micros",
    campaign="customers/123/campaigns/456",
):
    """Build a GAQL/API-shaped row, exposing both camelCase and snake_case
    spellings so the test focuses on normalization behaviour rather than on
    the exact wire shape of the incoming row."""
    camel = {
        "changeDateTime": changeDateTime,
        "changeResourceType": changeResourceType,
        "changeResourceName": changeResourceName,
        "clientType": clientType,
        "userEmail": userEmail,
        "resourceChangeOperation": resourceChangeOperation,
        "changedFields": changedFields,
        "campaign": campaign,
    }
    snake = {
        "change_date_time": changeDateTime,
        "change_resource_type": changeResourceType,
        "change_resource_name": changeResourceName,
        "client_type": clientType,
        "user_email": userEmail,
        "resource_change_operation": resourceChangeOperation,
        "changed_fields": changedFields,
        "campaign": campaign,
    }
    nested = dict(camel)
    nested.update(snake)
    row = {"changeEvent": nested, "change_event": nested}
    row.update(nested)
    return row


class TestQuery(unittest.TestCase):
    def test_query_selects_documented_fields(self):
        q = QUERY("2024-01-01 00:00:00")
        for field in (
            "change_event.change_date_time",
            "change_event.change_resource_type",
            "change_event.change_resource_name",
            "change_event.client_type",
            "change_event.user_email",
            "change_event.resource_change_operation",
            "change_event.changed_fields",
            "change_event.campaign",
        ):
            self.assertIn(field, q)
        self.assertIn("FROM change_event", q)

    def test_query_default_limit(self):
        q = QUERY("2024-01-01 00:00:00")
        self.assertIn("LIMIT 200", q)

    def test_query_custom_limit(self):
        q = QUERY("2024-01-01 00:00:00", limit=500)
        self.assertIn("LIMIT 500", q)

    def test_query_where_and_order(self):
        since = "2024-02-03 04:05:06"
        q = QUERY(since, limit=10)
        self.assertIn(
            "change_event.change_date_time >= '{}'".format(since), q
        )
        self.assertIn(
            "ORDER BY change_event.change_date_time DESC", q
        )
        self.assertIn("LIMIT 10", q)

    def test_query_accepts_limit_boundaries(self):
        self.assertIn("LIMIT 1", QUERY("2024-01-01 00:00:00", limit=1))
        self.assertIn(
            "LIMIT 10000", QUERY("2024-01-01 00:00:00", limit=10000)
        )

    def test_query_rejects_invalid_limits(self):
        for limit in (0, -1, 10001, 20000):
            with self.subTest(limit=limit):
                with self.assertRaises(ToolError):
                    QUERY("2024-01-01 00:00:00", limit=limit)

    def test_query_rejects_invalid_since_format(self):
        for since in (
            "2024-01-01",
            "2024-01-01T00:00:00",
            "01/01/2024 00:00:00",
            "not-a-date",
        ):
            with self.subTest(since=since):
                with self.assertRaises(ToolError):
                    QUERY(since)

    def test_query_accepts_valid_since_format(self):
        for since in ("2024-01-01 00:00:00", "2025-12-31 23:59:59"):
            with self.subTest(since=since):
                q = QUERY(since)
                self.assertIn(since, q)


class TestNormalize(unittest.TestCase):
    def test_normalize_basic_mapping(self):
        out = normalize([_row()])
        self.assertEqual(len(out), 1)
        item = out[0]
        for key in (
            "at",
            "resource_type",
            "resource",
            "client",
            "user",
            "operation",
            "fields",
            "campaign",
        ):
            self.assertIn(key, item)
        self.assertEqual(item["at"], "2024-01-01 10:00:00")
        self.assertEqual(item["resource_type"], "CAMPAIGN")
        self.assertEqual(item["resource"], "customers/123/campaigns/456")
        self.assertEqual(item["client"], "GOOGLE_ADS_API")
        self.assertEqual(item["operation"], "UPDATE")
        self.assertEqual(item["campaign"], "customers/123/campaigns/456")

    def test_normalize_redacts_user(self):
        item = normalize([_row(userEmail="alice@example.com")])[0]
        redacted = item["user"]
        self.assertNotEqual(redacted, "alice@example.com")
        self.assertTrue(redacted.startswith("a"))
        self.assertTrue(redacted.endswith("@example.com"))
        self.assertNotIn("alice", redacted)

    def test_normalize_redacts_user_with_multipart_domain(self):
        item = normalize([_row(userEmail="bob@corp.example.com")])[0]
        redacted = item["user"]
        self.assertNotEqual(redacted, "bob@corp.example.com")
        self.assertTrue(redacted.startswith("b"))
        self.assertTrue(redacted.endswith("@corp.example.com"))
        self.assertNotIn("bob", redacted)

    def test_normalize_fields_split(self):
        item = normalize([_row(changedFields="status,budget.amount_micros")])[0]
        self.assertIsInstance(item["fields"], list)
        self.assertIn("status", item["fields"])
        self.assertIn("budget.amount_micros", item["fields"])

    def test_normalize_empty_changed_fields(self):
        item = normalize([_row(changedFields="")])[0]
        self.assertEqual(item["fields"], [])

    def test_normalize_multiple_rows(self):
        out = normalize(
            [
                _row(userEmail="alice@example.com"),
                _row(
                    userEmail="bob@example.com",
                    changeDateTime="2024-01-02 11:00:00",
                    campaign="customers/123/campaigns/789",
                ),
            ]
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["at"], "2024-01-01 10:00:00")
        self.assertEqual(out[1]["at"], "2024-01-02 11:00:00")
        self.assertTrue(out[1]["user"].startswith("b"))


class TestClassify(unittest.TestCase):
    def _event(self, client="GOOGLE_ADS_API", user="alice@example.com"):
        return {"client": client, "user": user}

    def test_classify_default_adrails(self):
        events = [self._event()]
        result = classify(events)
        self.assertEqual(result[0]["source"], "adrails")

    def test_classify_non_api_clients_out_of_band(self):
        for client in ("GOOGLE_ADS_UI", "OTHER"):
            with self.subTest(client=client):
                result = classify([self._event(client=client)])
                self.assertEqual(result[0]["source"], "out_of_band")

    def test_classify_automated_and_recommendations_out_of_band(self):
        for client in (
            "GOOGLE_ADS_AUTOMATED_RULE",
            "GOOGLE_ADS_RECOMMENDATIONS",
        ):
            with self.subTest(client=client):
                result = classify([self._event(client=client)])
                self.assertEqual(result[0]["source"], "out_of_band")

    def test_classify_our_users_match(self):
        events = [self._event(user="alice@example.com")]
        result = classify(events, our_users=("alice@example.com",))
        self.assertEqual(result[0]["source"], "adrails")

    def test_classify_our_users_mismatch(self):
        events = [self._event(user="alice@example.com")]
        result = classify(events, our_users=("bob@example.com",))
        self.assertEqual(result[0]["source"], "out_of_band")

    def test_classify_custom_client_types(self):
        result = classify(
            [self._event(client="MY_TOOL")],
            our_client_types=("MY_TOOL",),
        )
        self.assertEqual(result[0]["source"], "adrails")

        result = classify(
            [self._event(client="GOOGLE_ADS_API")],
            our_client_types=("MY_TOOL",),
        )
        self.assertEqual(result[0]["source"], "out_of_band")

    def test_classify_empty(self):
        self.assertEqual(classify([]), [])


class TestDriftReport(unittest.TestCase):
    def test_out_of_band_count(self):
        report = drift_report(
            [
                {"source": "out_of_band", "client": "UI", "fields": []},
                {"source": "out_of_band", "client": "UI", "fields": []},
                {"source": "adrails", "client": "GOOGLE_ADS_API", "fields": []},
            ]
        )
        self.assertEqual(report["out_of_band"], 2)

    def test_by_client(self):
        report = drift_report(
            [
                {"source": "out_of_band", "client": "UI", "fields": []},
                {"source": "out_of_band", "client": "UI", "fields": []},
                {"source": "out_of_band", "client": "OTHER", "fields": []},
                {"source": "adrails", "client": "GOOGLE_ADS_API", "fields": []},
            ]
        )
        self.assertEqual(report["by_client"].get("UI"), 2)
        self.assertEqual(report["by_client"].get("OTHER"), 1)

    def test_risky_fields(self):
        risky_events = [
            {"source": "out_of_band", "client": "UI", "fields": ["status"]},
            {
                "source": "out_of_band",
                "client": "UI",
                "fields": ["budget.amount_micros"],
            },
            {
                "source": "out_of_band",
                "client": "UI",
                "fields": ["bidding_strategy"],
            },
            {"source": "out_of_band", "client": "UI", "fields": ["target_cpa"]},
            {"source": "out_of_band", "client": "UI", "fields": ["end_date"]},
        ]
        report = drift_report(risky_events)
        self.assertEqual(len(report["risky"]), len(risky_events))

    def test_not_risky_fields(self):
        report = drift_report(
            [
                {"source": "out_of_band", "client": "UI", "fields": ["name"]},
                {
                    "source": "out_of_band",
                    "client": "UI",
                    "fields": ["campaign.name"],
                },
            ]
        )
        self.assertEqual(report["risky"], [])

    def test_empty(self):
        report = drift_report([])
        self.assertEqual(report["out_of_band"], 0)
        self.assertEqual(report["by_client"], {})
        self.assertEqual(report["risky"], [])
        self.assertIsInstance(report["summary"], str)
        self.assertTrue(report["summary"].strip())


if __name__ == "__main__":
    unittest.main()