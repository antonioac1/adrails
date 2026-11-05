import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone

from adsops.auditchain import AuditChain, anchor_digest, redact


ZERO_HASH = "0" * 64
FIXED_TS = 1700000000
FIXED_ISO = datetime.fromtimestamp(FIXED_TS, timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)


def canonical(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha_canonical(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class AuditChainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "audit.log")
        self.chain = AuditChain(self.path, now=lambda: FIXED_TS)

    def _read_lines(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return f.readlines()

    def _write_lines(self, lines):
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    def _json_line(self, obj):
        return canonical(obj) + "\n"

    def test_append_returns_documented_entry_first_prev_and_hash(self):
        entry = self.chain.append("user.created", {"name": "Alice"})

        self.assertEqual(
            set(entry),
            {"seq", "at", "actor", "event", "data", "prev", "hash"},
        )
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["at"], FIXED_ISO)
        self.assertEqual(entry["actor"], "agent")
        self.assertEqual(entry["event"], "user.created")
        self.assertEqual(entry["data"], {"name": "Alice"})
        self.assertEqual(entry["prev"], ZERO_HASH)
        self.assertRegex(entry["hash"], r"^[0-9a-f]{64}$")

        body = {k: v for k, v in entry.items() if k != "hash"}
        self.assertEqual(entry["hash"], sha_canonical(body))

    def test_second_append_links_prev_and_hash(self):
        first = self.chain.append("one", {})
        second = self.chain.append("two", {})

        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["prev"], first["hash"])

        body = {k: v for k, v in second.items() if k != "hash"}
        self.assertEqual(second["hash"], sha_canonical(body))

    def test_append_redacts_data(self):
        original = {
            "user": "bob",
            "password": "hunter2",
            "nested": {"token": "abc"},
        }
        entry = self.chain.append("login", original)

        self.assertEqual(entry["data"]["user"], "bob")
        self.assertEqual(entry["data"]["password"], "[redacted]")
        self.assertEqual(entry["data"]["nested"]["token"], "[redacted]")
        self.assertEqual(original["password"], "hunter2")

        body = {k: v for k, v in entry.items() if k != "hash"}
        self.assertEqual(entry["hash"], sha_canonical(body))

        stored = json.loads(self._read_lines()[0])
        self.assertEqual(stored["data"]["password"], "[redacted]")

    def test_hash_uses_canonical_json_unicode_and_sorted_keys(self):
        data = {"z": 1, "a": [1, 2], "note": "café"}
        entry = self.chain.append("unicode", data)

        body = {
            "seq": 1,
            "at": FIXED_ISO,
            "actor": "agent",
            "event": "unicode",
            "data": data,
            "prev": ZERO_HASH,
        }
        self.assertEqual(entry["hash"], sha_canonical(body))

    def test_actor_default_and_overrides(self):
        default_entry = self.chain.append("e", {})
        explicit_entry = self.chain.append("e", {}, actor="alice")
        none_entry = self.chain.append("e", {}, actor=None)

        self.assertEqual(default_entry["actor"], "agent")
        self.assertEqual(explicit_entry["actor"], "alice")
        self.assertEqual(none_entry["actor"], "agent")

    def test_custom_actor_default(self):
        chain = AuditChain(
            os.path.join(self._tmp.name, "custom.log"),
            now=lambda: FIXED_TS,
            actor_default="robot",
        )
        entry = chain.append("e", {})
        self.assertEqual(entry["actor"], "robot")

    @unittest.skipUnless(os.name == "posix", "file mode is POSIX-specific")
    def test_file_created_with_0600(self):
        self.chain.append("e", {})
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_head_none_for_empty_chain(self):
        self.assertIsNone(self.chain.head())

    def test_head_returns_last_and_reads_fresh(self):
        self.chain.append("one", {})
        other = AuditChain(self.path, now=lambda: FIXED_TS + 1)
        second = other.append("two", {})

        head = self.chain.head()
        self.assertEqual(head["seq"], 2)
        self.assertEqual(head["hash"], second["hash"])

    def test_tail_newest_first_and_default_limit(self):
        for i in range(25):
            self.chain.append(f"event-{i}", {})

        got = self.chain.tail()
        self.assertEqual(len(got), 20)
        self.assertEqual([e["seq"] for e in got], list(range(25, 5, -1)))

    def test_tail_filters_event_and_actor(self):
        self.chain.append("login", {}, actor="alice")
        self.chain.append("update", {}, actor="bob")
        self.chain.append("login", {}, actor="bob")

        self.assertEqual(
            [e["seq"] for e in self.chain.tail(event="login")], [3, 1]
        )
        self.assertEqual(
            [e["seq"] for e in self.chain.tail(actor="bob")], [3, 2]
        )
        self.assertEqual(
            [e["seq"] for e in self.chain.tail(event="login", actor="bob")],
            [3],
        )
        self.assertEqual(self.chain.tail(event="missing"), [])

    def test_verify_valid_chain(self):
        self.chain.append("one", {})
        self.chain.append("two", {})

        result = self.chain.verify()
        self.assertEqual(
            set(result), {"ok", "entries", "first_bad_seq", "reason"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 2)
        self.assertIsNone(result["first_bad_seq"])
        self.assertIsNone(result["reason"])

    def test_verify_detects_edited_entry(self):
        self.chain.append("one", {"v": 1})
        self.chain.append("two", {"v": 2})

        lines = self._read_lines()
        obj = json.loads(lines[1])
        obj["data"]["v"] = 999
        lines[1] = self._json_line(obj)
        self._write_lines(lines)

        result = self.chain.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_bad_seq"], 2)
        self.assertIsInstance(result["reason"], str)

    def test_verify_detects_deleted_entry_seq_gap(self):
        for i in range(3):
            self.chain.append(f"e{i}", {})

        lines = self._read_lines()
        del lines[1]
        self._write_lines(lines)

        result = self.chain.verify()
        self.assertFalse(result["ok"])
        self.assertIsInstance(result["first_bad_seq"], int)
        self.assertIsInstance(result["reason"], str)

    def test_verify_detects_reordered_entries(self):
        for i in range(3):
            self.chain.append(f"e{i}", {})

        lines = self._read_lines()
        lines[0], lines[1] = lines[1], lines[0]
        self._write_lines(lines)

        result = self.chain.verify()
        self.assertFalse(result["ok"])
        self.assertIsInstance(result["reason"], str)

    def test_verify_detects_broken_prev_link_with_valid_hash(self):
        self.chain.append("one", {})
        self.chain.append("two", {})

        lines = self._read_lines()
        obj = json.loads(lines[1])
        obj["prev"] = ZERO_HASH
        body = {k: v for k, v in obj.items() if k != "hash"}
        obj["hash"] = sha_canonical(body)
        lines[1] = self._json_line(obj)
        self._write_lines(lines)

        result = self.chain.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_bad_seq"], 2)
        self.assertIsInstance(result["reason"], str)

    def test_verify_detects_unparsable_line(self):
        self.chain.append("one", {})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{not-json\n")

        result = self.chain.verify()
        self.assertFalse(result["ok"])
        self.assertIsInstance(result["reason"], str)

    def test_verify_not_cached_between_calls(self):
        self.chain.append("one", {})
        self.assertTrue(self.chain.verify()["ok"])

        lines = self._read_lines()
        obj = json.loads(lines[0])
        obj["event"] = "changed"
        lines[0] = self._json_line(obj)
        self._write_lines(lines)

        self.assertFalse(self.chain.verify()["ok"])

    def test_anchor_digest_matches_chain(self):
        for i in range(3):
            self.chain.append(f"e{i}", {})

        digest = anchor_digest(self.path)
        head = self.chain.head()

        self.assertEqual(set(digest), {"entries", "head_hash"})
        self.assertEqual(digest["entries"], 3)
        self.assertEqual(digest["head_hash"], head["hash"])

    def test_append_continues_across_instances(self):
        first = self.chain.append("one", {})

        other = AuditChain(self.path, now=lambda: FIXED_TS + 1)
        second = other.append("two", {})
        third = self.chain.append("three", {})

        self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])
        self.assertEqual(second["prev"], first["hash"])
        self.assertEqual(third["prev"], second["hash"])

    def test_redact_sensitive_keys_case_insensitive(self):
        payload = {
            "password": "p",
            "TOKEN": "t",
            "Authorization": "a",
            "cookie": "c",
            "email": "e",
            "phone": "ph",
            "api_key": "k",
            "secret": "s",
            "note": "ok",
        }
        out = redact(payload)

        for key in [
            "password",
            "TOKEN",
            "Authorization",
            "cookie",
            "email",
            "phone",
            "api_key",
            "secret",
        ]:
            self.assertEqual(out[key], "[redacted]")
        self.assertEqual(out["note"], "ok")

    def test_redact_recurses_lists_and_dicts(self):
        payload = {
            "outer": {"secret": "s", "safe": 1},
            "items": [{"token": "t"}, {"safe": 2}],
            "raw": [1, "text"],
        }
        out = redact(payload)

        self.assertEqual(out["outer"]["secret"], "[redacted]")
        self.assertEqual(out["outer"]["safe"], 1)
        self.assertEqual(out["items"][0]["token"], "[redacted]")
        self.assertEqual(out["items"][1]["safe"], 2)
        self.assertEqual(out["raw"], [1, "text"])

        self.assertEqual(
            redact([{"password": "p"}, {"safe": "v"}]),
            [{"password": "[redacted]"}, {"safe": "v"}],
        )

    def test_redact_returns_deep_copy_and_does_not_mutate(self):
        payload = {
            "nested": {"password": "p", "items": [1, 2]},
            "safe": {"v": 1},
        }
        out = redact(payload)

        self.assertIsNot(out, payload)
        self.assertIsNot(out["nested"], payload["nested"])
        out["nested"]["items"].append(3)
        out["safe"]["v"] = 99

        self.assertEqual(payload["nested"]["items"], [1, 2])
        self.assertEqual(payload["safe"]["v"], 1)

    def test_redact_preserves_non_sensitive_values(self):
        payload = {
            "name": "Alice",
            "count": 3,
            "active": True,
            "none": None,
            "values": [1, "x", {"a": 2}],
        }
        self.assertEqual(redact(payload), payload)

    def test_redact_redacts_non_string_sensitive_values(self):
        payload = {"password": 123, "token": None, "secret": ["x"]}
        out = redact(payload)

        self.assertEqual(out["password"], "[redacted]")
        self.assertEqual(out["token"], "[redacted]")
        self.assertEqual(out["secret"], "[redacted]")

    def test_redact_bearer_token_like_value_under_sensitive_key(self):
        payload = {"authorization": "Bearer " + ("A" * 32)}
        out = redact(payload)
        self.assertEqual(out["authorization"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
