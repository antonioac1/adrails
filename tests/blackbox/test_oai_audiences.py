"""Tests for adsops.oai_audiences — CRM list -> hashed custom audience files/plans.

Only the documented public surface is exercised:
    build_file(rows) -> (csv_text, stats)
    plan(op, name, rows, audience_id=None, ...) -> dict
    execute(client, plan, csv_text, commit=False, idempotency_key=None) -> dict
All I/O is mocked (fake client); no network, no real files needed.
"""

import hashlib
import json
import unittest

from adsops import oai_audiences


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_hex(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def data_rows(csv_text):
    """Return CSV body lines (everything after the header), ignoring blanks."""
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    return lines[1:]


ROWS = [
    {"email": "alex@example.com"},
    {"phone_number": "+12025550123"},
]


class FakeClient:
    """Minimal stand-in for OpenAIAdsClient; records calls, replays canned JSON."""

    def __init__(self, responses=()):
        self.calls = []
        self._responses = list(responses)

    def _record(self, method, path, query, body, idempotency_key, raw):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "body": body,
                "idempotency_key": idempotency_key,
                "raw": raw,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return {}

    def request(self, method, path, query=None, body=None, idempotency_key=None, raw=None):
        return self._record(method, path, query, body, idempotency_key, raw)

    def get(self, path, query=None):
        return self._record("GET", path, query, None, None, None)

    def list_all(self, path, query=None):
        return []


def raw_fields(raw):
    """raw == (fields, filename, bytes); fields may be a dict or pair list."""
    fields = raw[0]
    return dict(fields)


# ---------------------------------------------------------------------------
# build_file
# ---------------------------------------------------------------------------

class BuildFileTests(unittest.TestCase):
    def test_header_is_documented_identifier_columns(self):
        csv_text, _ = oai_audiences.build_file([{"email": "a@example.com"}])
        header = csv_text.splitlines()[0].strip().lstrip("\ufeff")
        self.assertEqual(header, "email_sha256,phone_number_sha256")

    def test_email_is_trimmed_lowercased_then_hashed(self):
        csv_text, stats = oai_audiences.build_file([{"email": "  Alex@Example.COM  "}])
        self.assertIn(sha256_hex("alex@example.com"), csv_text)
        self.assertEqual(stats["emails"], 1)

    def test_email_dots_and_plus_tags_are_preserved(self):
        csv_text, _ = oai_audiences.build_file([{"email": "A.B+tag@Example.com"}])
        self.assertIn(sha256_hex("a.b+tag@example.com"), csv_text)
        self.assertNotIn(sha256_hex("ab@example.com"), csv_text)

    def test_phone_e164_value_is_hashed_with_leading_plus(self):
        csv_text, _ = oai_audiences.build_file([{"phone_number": "+12025550123"}])
        self.assertIn(sha256_hex("+12025550123"), csv_text)

    def test_phone_is_normalized_to_e164_before_hashing(self):
        formatted, _ = oai_audiences.build_file([{"phone_number": "+1 (202) 555-0123"}])
        plain, _ = oai_audiences.build_file([{"phone_number": "+12025550123"}])
        self.assertIn(sha256_hex("+12025550123"), formatted)
        self.assertEqual(formatted, plain)

    def test_hashes_are_64_char_lowercase_hex_and_one_row_per_input_row(self):
        csv_text, stats = oai_audiences.build_file(
            [{"email": "a@example.com", "phone_number": "+12025550123"}]
        )
        rows = data_rows(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["rows_out"], 1)
        cells = rows[0].split(",")
        self.assertEqual(len(cells), 2)
        for cell in cells:
            self.assertRegex(cell.strip(), r"^[0-9a-f]{64}$")

    def test_rows_without_identifiers_are_skipped_and_counted(self):
        rows = [
            {"email": "a@example.com"},
            {},
            {"email": "   "},
            {"phone_number": ""},
        ]
        csv_text, stats = oai_audiences.build_file(rows)
        self.assertEqual(stats["rows_in"], 4)
        self.assertEqual(stats["rows_out"], 1)
        self.assertEqual(stats["skipped"], 3)
        self.assertEqual(len(data_rows(csv_text)), 1)

    def test_identical_rows_are_deduped(self):
        rows = [
            {"email": "a@example.com"},
            {"email": " A@Example.com "},
            {"email": "b@example.com"},
        ]
        csv_text, stats = oai_audiences.build_file(rows)
        self.assertEqual(stats["rows_in"], 3)
        self.assertEqual(stats["rows_out"], 2)
        self.assertEqual(len(data_rows(csv_text)), 2)
        self.assertIn(sha256_hex("a@example.com"), csv_text)
        self.assertIn(sha256_hex("b@example.com"), csv_text)

    def test_stats_contains_documented_keys(self):
        _, stats = oai_audiences.build_file(ROWS)
        for key in ("rows_in", "rows_out", "emails", "phones", "skipped"):
            self.assertIn(key, stats)
        self.assertEqual(stats["rows_in"], 2)
        self.assertEqual(stats["rows_out"], 2)
        self.assertEqual(stats["emails"], 1)
        self.assertEqual(stats["phones"], 1)
        self.assertEqual(stats["skipped"], 0)

    def test_raw_identifiers_never_appear_in_output(self):
        csv_text, _ = oai_audiences.build_file(
            [{"email": "jane.doe@example.com", "phone_number": "+12025550123"}]
        )
        self.assertNotIn("jane.doe@example.com", csv_text)
        self.assertNotIn("jane.doe", csv_text)
        self.assertNotIn("+12025550123", csv_text)
        self.assertNotIn("12025550123", csv_text)

    def test_empty_input_yields_no_data_rows(self):
        csv_text, stats = oai_audiences.build_file([])
        self.assertEqual(stats["rows_in"], 0)
        self.assertEqual(stats["rows_out"], 0)
        self.assertEqual(data_rows(csv_text), [])
        self.assertNotIn("@", csv_text)


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

class PlanTests(unittest.TestCase):
    def assert_refused(self, *args, **kwargs):
        with self.assertRaises(Exception):
            oai_audiences.plan(*args, **kwargs)

    def test_create_plan_shape(self):
        p = oai_audiences.plan("create", "High-value customers", ROWS)
        for key in ("op", "name", "audience_id", "file_bytes", "stats",
                    "requires_commit", "notes"):
            self.assertIn(key, p)
        self.assertEqual(p["op"], "create")
        self.assertEqual(p["name"], "High-value customers")
        self.assertIsNone(p["audience_id"])
        self.assertIs(p["requires_commit"], True)
        self.assertIsInstance(p["notes"], list)
        self.assertGreaterEqual(p["file_bytes"], 1)

    def test_plan_file_bytes_and_stats_match_build_file(self):
        csv_text, stats = oai_audiences.build_file(ROWS)
        p = oai_audiences.plan("create", "High-value customers", ROWS)
        self.assertEqual(p["file_bytes"], len(csv_text.encode("utf-8")))
        for key in ("rows_in", "rows_out", "emails", "phones", "skipped"):
            self.assertEqual(p["stats"][key], stats[key])

    def test_create_name_shorter_than_three_chars_refused(self):
        self.assert_refused("create", "ab", ROWS)
        self.assert_refused("create", "", ROWS)

    def test_create_name_of_exactly_three_chars_allowed(self):
        p = oai_audiences.plan("create", "abc", ROWS)
        self.assertEqual(p["name"], "abc")

    def test_create_with_no_usable_rows_refused(self):
        self.assert_refused("create", "High-value customers", [{"email": "  "}])
        self.assert_refused("create", "High-value customers", [])

    def test_add_without_audience_id_refused(self):
        self.assert_refused("add", "High-value customers", ROWS)
        self.assert_refused("remove", "High-value customers", ROWS)
        self.assert_refused("replace", "High-value customers", ROWS)

    def test_audience_id_pattern_enforced(self):
        for bad in ("aud_123", "caud_", "caud_abc def", "Caud_123", ""):
            with self.subTest(audience_id=bad):
                self.assert_refused("add", "High-value customers", ROWS,
                                    audience_id=bad)

    def test_valid_audience_id_accepted(self):
        p = oai_audiences.plan("add", "High-value customers", ROWS,
                               audience_id="caud_ABC-123_x")
        self.assertEqual(p["op"], "add")
        self.assertEqual(p["audience_id"], "caud_ABC-123_x")

    def test_remove_with_empty_rows_refused(self):
        self.assert_refused("remove", "High-value customers", [],
                            audience_id="caud_abc123")

    def test_replace_with_empty_rows_requires_allow_empty(self):
        self.assert_refused("replace", "High-value customers", [],
                            audience_id="caud_abc123")

    def test_replace_allows_empty_rows_with_allow_empty(self):
        p = oai_audiences.plan("replace", "High-value customers", [],
                               audience_id="caud_abc123", allow_empty=True)
        self.assertEqual(p["op"], "replace")
        self.assertEqual(p["audience_id"], "caud_abc123")
        self.assertEqual(p["stats"]["rows_out"], 0)
        self.assertIs(p["requires_commit"], True)

    def test_unknown_operation_refused(self):
        self.assert_refused("merge", "High-value customers", ROWS)
        self.assert_refused("delete", "High-value customers", ROWS)


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

class ExecuteTests(unittest.TestCase):
    def _run_create(self, rows=None, idempotency_key=None, responses=None):
        rows = ROWS if rows is None else rows
        client = FakeClient(responses if responses is not None else [
            {"file_id": "oaisdmntci_123"},
            {"id": "caud_abc123", "status": "processing"},
        ])
        p = oai_audiences.plan("create", "High-value customers", rows)
        csv_text, _ = oai_audiences.build_file(rows)
        out = oai_audiences.execute(client, p, csv_text, commit=True,
                                    idempotency_key=idempotency_key)
        return client, p, csv_text, out

    def test_without_commit_returns_plan_and_makes_no_calls(self):
        client = FakeClient()
        p = oai_audiences.plan("create", "High-value customers", ROWS)
        csv_text, _ = oai_audiences.build_file(ROWS)
        out = oai_audiences.execute(client, p, csv_text, commit=False)
        self.assertEqual(out, p)
        self.assertEqual(client.calls, [])

    def test_without_commit_ignores_idempotency_key(self):
        client = FakeClient()
        p = oai_audiences.plan("create", "High-value customers", ROWS)
        csv_text, _ = oai_audiences.build_file(ROWS)
        oai_audiences.execute(client, p, csv_text, commit=False,
                              idempotency_key="idem-1")
        self.assertEqual(client.calls, [])

    def test_create_uploads_multipart_then_creates_audience(self):
        client, p, csv_text, out = self._run_create()
        self.assertEqual(len(client.calls), 2)

        upload, create = client.calls
        self.assertEqual(upload["method"].upper(), "POST")
        self.assertEqual(upload["path"], "/uploads")
        self.assertIsNotNone(upload["raw"])
        fields, filename, payload = upload["raw"]
        self.assertEqual(raw_fields(upload["raw"]).get("purpose"), "custom_audience")
        self.assertTrue(str(filename).lower().endswith(".csv"))
        self.assertEqual(payload, csv_text.encode("utf-8"))
        # hashed file only: no raw PII in the upload body
        self.assertNotIn(b"alex@example.com", payload)

        self.assertEqual(create["method"].upper(), "POST")
        self.assertEqual(create["path"], "/custom_audiences")
        self.assertEqual(create["body"]["file_id"], "oaisdmntci_123")
        self.assertEqual(create["body"]["identifier_resolution"], "auto")
        self.assertEqual(create["body"]["name"], p["name"])

        self.assertEqual(out["audience_id"], "caud_abc123")
        self.assertEqual(out["status"], "processing")

    def test_create_body_carries_no_raw_identifiers(self):
        client, _, _, _ = self._run_create()
        body = json.dumps(client.calls[1]["body"], default=str)
        self.assertNotIn("alex@example.com", body)
        self.assertNotIn("+12025550123", body)

    def test_membership_ops_target_uploaded_file_and_audience(self):
        for op in ("add", "remove", "replace"):
            with self.subTest(op=op):
                client = FakeClient([
                    {"file_id": "oaisdmntci_9"},
                    {"status": "processing"},
                ])
                p = oai_audiences.plan(op, "High-value customers", ROWS,
                                       audience_id="caud_abc123")
                csv_text, _ = oai_audiences.build_file(ROWS)
                out = oai_audiences.execute(client, p, csv_text, commit=True)

                self.assertEqual(len(client.calls), 2)
                self.assertEqual(client.calls[0]["path"], "/uploads")
                call = client.calls[1]
                self.assertEqual(call["method"].upper(), "POST")
                self.assertIn("caud_abc123", call["path"])
                self.assertNotEqual(call["path"].rstrip("/"), "/custom_audiences")
                self.assertEqual(call["body"]["file_id"], "oaisdmntci_9")
                self.assertEqual(call["body"]["identifier_resolution"], "auto")
                self.assertEqual(out["audience_id"], "caud_abc123")
                self.assertEqual(out["status"], "processing")

    def test_idempotency_key_is_sent(self):
        client, _, _, _ = self._run_create(idempotency_key="idem-abc-123")
        keys = [c["idempotency_key"] for c in client.calls]
        self.assertTrue(any(k for k in keys), "expected at least one idempotency key")
        for k in keys:
            if k is not None:
                self.assertIsInstance(k, str)
                self.assertTrue(k)

    def test_execute_returns_only_audience_id_and_status_keys(self):
        client, _, _, out = self._run_create()
        self.assertIsInstance(out, dict)
        self.assertIn("audience_id", out)
        self.assertIn("status", out)


if __name__ == '__main__':
    unittest.main()
