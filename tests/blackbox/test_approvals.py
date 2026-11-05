"""Unit tests for adsops.approvals (public API only).

Only documented behaviour / return keys are asserted. All file I/O goes to a
temporary directory; the clock is injected where determinism matters.
"""

import base64
import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import adsops.approvals as approvals


def _resolve_exception(name):
    """Locate RailViolation / ToolError wherever the package defines them."""
    for modname in (
        "adsops.approvals",
        "adsops.errors",
        "adsops.error",
        "adsops.common",
        "adsops.rails",
        "adsops",
    ):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        cls = getattr(mod, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return cls
    return Exception  # permissive fallback so tests still exercise paths


RailViolation = _resolve_exception("RailViolation")
ToolError = _resolve_exception("ToolError")


def _b64url_decode(token):
    pad = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + pad).decode("utf-8")


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _expires_epoch(value):
    """Accept either numeric epochs or ISO-8601 UTC strings for expires_at."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        else:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise TypeError(f"unsupported expires_at value: {value!r}")


def tamper_with_signature(token):
    """Re-encode a token with a corrupted signature, keeping it well formed."""
    try:
        raw = _b64url_decode(token)
    except Exception:
        raw = ""
    parts = raw.split(".")
    if len(parts) >= 3:
        sig = parts[-1]
        parts[-1] = sig[:-1] + ("A" if sig[-1:] != "A" else "B")
        return _b64url_encode(".".join(parts))
    # Fallback: flip a character near the end (the signature area).
    idx = min(len(token) - 1, int(len(token) * 0.75))
    ch = "A" if token[idx] != "A" else "B"
    return token[:idx] + ch + token[idx + 1:]


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class ApprovalQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.secret = b"unit-test-secret-value"
        self.clock = Clock()

    # -- helpers -----------------------------------------------------------
    def path(self, name="approvals.jsonl"):
        return os.path.join(self.dir, name)

    def make_queue(self, name="approvals.jsonl", **kwargs):
        kwargs.setdefault("secret", self.secret)
        kwargs.setdefault("now", self.clock)
        return approvals.ApprovalQueue(self.path(name), **kwargs)

    def read_lines(self, name="approvals.jsonl"):
        with open(self.path(name), "r", encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]

    # -- request -----------------------------------------------------------
    def test_request_returns_documented_keys(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "increase budget", risk="high")
        for key in (
            "request_id",
            "plan_id",
            "status",
            "expires_at",
            "approve_token",
            "reject_token",
        ):
            self.assertIn(key, req)
        self.assertEqual(req["plan_id"], "plan-1")
        self.assertEqual(req["status"], "pending")
        self.assertGreater(_expires_epoch(req["expires_at"]), self.clock.t)

    def test_tokens_are_urlsafe_and_distinct(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        approve, reject = req["approve_token"], req["reject_token"]
        self.assertIsInstance(approve, str)
        self.assertIsInstance(reject, str)
        self.assertNotEqual(approve, reject)
        for tok in (approve, reject):
            self.assertNotIn("+", tok)
            self.assertNotIn("/", tok)

    def test_second_request_for_pending_plan_returns_existing(self):
        q = self.make_queue()
        first = q.request("plan-1", "camp-1", "summary one")
        second = q.request("plan-1", "camp-1", "summary two")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["approve_token"], second["approve_token"])
        self.assertEqual(len(q.list()), 1)

    def test_different_plans_get_different_requests(self):
        q = self.make_queue()
        a = q.request("plan-a", "camp", "s")
        b = q.request("plan-b", "camp", "s")
        self.assertNotEqual(a["request_id"], b["request_id"])
        self.assertNotEqual(a["approve_token"], b["approve_token"])

    def test_status_returns_pending_request(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        current = q.status(req["request_id"])
        self.assertIsInstance(current, dict)
        self.assertEqual(current["request_id"], req["request_id"])
        self.assertEqual(current["status"], "pending")

    # -- decide ------------------------------------------------------------
    def test_approve_flow(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        out = q.decide(req["approve_token"], "alice@example.com")
        self.assertIsInstance(out, dict)
        self.assertEqual(out["plan_id"], "plan-1")
        self.assertEqual(out["status"], "approved")
        self.assertEqual(q.status(req["request_id"])["status"], "approved")

    def test_reject_flow(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        out = q.decide(req["reject_token"], "bob@example.com")
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(q.status(req["request_id"])["status"], "rejected")

    def test_already_decided_is_refused(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        q.decide(req["approve_token"], "alice")
        with self.assertRaises(RailViolation):
            q.decide(req["approve_token"], "alice")

    def test_tampered_token_is_refused(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        bad = tamper_with_signature(req["approve_token"])
        self.assertNotEqual(bad, req["approve_token"])
        with self.assertRaises(RailViolation):
            q.decide(bad, "alice")
        # The genuine token must still work afterwards.
        self.assertEqual(q.status(req["request_id"])["status"], "pending")

    def test_expired_token_is_refused(self):
        q = self.make_queue(ttl_s=60)
        req = q.request("plan-1", "camp-1", "summary")
        self.assertAlmostEqual(_expires_epoch(req["expires_at"]), self.clock.t + 60, delta=1)
        self.clock.advance(3600)
        with self.assertRaises(RailViolation):
            q.decide(req["approve_token"], "alice")

    def test_unknown_request_id_raises_tool_error(self):
        qa = self.make_queue("a.jsonl")
        qa.request("plan-a", "camp", "summary")
        qb = self.make_queue("b.jsonl")
        foreign = qb.request("plan-b", "camp", "summary")
        with self.assertRaises(ToolError):
            qa.decide(foreign["approve_token"], "alice")

    # -- require_approved --------------------------------------------------
    def test_require_approved_raises_while_pending(self):
        q = self.make_queue()
        req = q.request("plan-x", "camp-1", "summary")
        with self.assertRaises(RailViolation) as ctx:
            q.require_approved("plan-x")
        message = str(ctx.exception)
        self.assertIn("plan-x", message)
        self.assertIn(req["request_id"], message)

    def test_require_approved_raises_after_reject(self):
        q = self.make_queue()
        req = q.request("plan-x", "camp-1", "summary")
        q.decide(req["reject_token"], "bob")
        with self.assertRaises(RailViolation):
            q.require_approved("plan-x")

    def test_require_approved_returns_request_once_approved(self):
        q = self.make_queue()
        req = q.request("plan-x", "camp-1", "summary")
        q.decide(req["approve_token"], "alice")
        approved = q.require_approved("plan-x")
        self.assertIsInstance(approved, dict)
        self.assertEqual(approved["request_id"], req["request_id"])
        self.assertEqual(approved["plan_id"], "plan-x")
        self.assertEqual(approved["status"], "approved")

    def test_require_approved_refuses_expired_approval(self):
        q = self.make_queue(ttl_s=30)
        req = q.request("plan-x", "camp-1", "summary")
        q.decide(req["approve_token"], "alice")
        self.clock.advance(7200)
        with self.assertRaises(RailViolation):
            q.require_approved("plan-x")

    # -- storage -----------------------------------------------------------
    def test_state_persists_across_instances(self):
        q1 = self.make_queue()
        req = q1.request("plan-1", "camp-1", "summary")

        q2 = self.make_queue()
        self.assertEqual(q2.status(req["request_id"])["status"], "pending")
        q2.decide(req["approve_token"], "bob")

        q3 = self.make_queue()
        self.assertEqual(q3.status(req["request_id"])["status"], "approved")

    def test_file_is_append_only_json_lines(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        q.decide(req["approve_token"], "alice")
        lines = self.read_lines()
        self.assertGreaterEqual(len(lines), 2)
        events = []
        for line in lines:
            record = json.loads(line)  # every line must be valid JSON
            self.assertIsInstance(record, dict)
            events.append(record.get("event"))
        self.assertIn("request", events)
        self.assertIn("decision", events)

    def test_secret_is_never_written_to_file(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        q.decide(req["approve_token"], "alice")
        with open(self.path(), "rb") as fh:
            blob = fh.read()
        self.assertNotIn(self.secret, blob)
        self.assertNotIn(self.secret.hex().encode("ascii"), blob)

    @unittest.skipIf(sys.platform.startswith("win"), "POSIX permissions only")
    def test_file_permissions_are_owner_only(self):
        q = self.make_queue()
        q.request("plan-1", "camp-1", "summary")
        mode = stat.S_IMODE(os.stat(self.path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_lines_are_skipped(self):
        q = self.make_queue()
        req = q.request("plan-1", "camp-1", "summary")
        with open(self.path(), "a", encoding="utf-8") as fh:
            fh.write("this is not json at all\n")
            fh.write('{"event": "request", "truncated"\n')

        q2 = self.make_queue()
        items = q2.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["request_id"], req["request_id"])

        # The queue must still accept writes after seeing corrupt data.
        new_req = q2.request("plan-2", "camp-1", "summary")
        self.assertEqual(new_req["status"], "pending")
        self.assertEqual(len(self.make_queue().list()), 2)

    # -- list --------------------------------------------------------------
    def test_list_returns_newest_first(self):
        q = self.make_queue()
        for plan_id in ("plan-1", "plan-2", "plan-3"):
            q.request(plan_id, "camp", "summary")
            self.clock.advance(1)
        items = q.list()
        self.assertEqual(
            [item["plan_id"] for item in items], ["plan-3", "plan-2", "plan-1"]
        )

    def test_list_filters_by_status(self):
        q = self.make_queue()
        q.request("plan-1", "camp", "summary")
        mid = q.request("plan-2", "camp", "summary")
        q.request("plan-3", "camp", "summary")
        q.decide(mid["approve_token"], "alice")

        pending = q.list(status="pending")
        self.assertEqual([i["plan_id"] for i in pending], ["plan-3", "plan-1"])
        approved = q.list(status="approved")
        self.assertEqual([i["plan_id"] for i in approved], ["plan-2"])

    def test_list_filters_by_campaign(self):
        q = self.make_queue()
        q.request("plan-1", "alpha", "summary")
        q.request("plan-2", "beta", "summary")
        q.request("plan-3", "alpha", "summary")

        alpha = q.list(campaign="alpha")
        self.assertEqual(len(alpha), 2)
        self.assertEqual({i["plan_id"] for i in alpha}, {"plan-1", "plan-3"})
        self.assertEqual(len(q.list()), 3)


BASE_APPROVAL_POLICY = {
    "budget_increase_pct": 20,
    "daily_budget_over": 100,
    "enable": True,
    "ops_over": 25,
    "always": False,
}


def make_summary(**overrides):
    summary = {
        "budget_delta_pct": None,
        "new_daily_budget": None,
        "enable": False,
        "end_date_extend_days": 0,
        "ops": 0,
        "platform": "google_ads",
    }
    summary.update(overrides)
    return summary


def make_policy(**overrides):
    approval = dict(BASE_APPROVAL_POLICY)
    approval.update(overrides)
    return {"approval": approval}


class NeedsApprovalTests(unittest.TestCase):
    def check(self, summary, policy):
        result = approvals.needs_approval(summary, policy)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        needed, reasons = result
        self.assertIsInstance(needed, bool)
        if needed:
            self.assertTrue(reasons)
        else:
            self.assertFalse(reasons)
        return needed

    def test_missing_policy_section_never_requires_approval(self):
        summary = make_summary(budget_delta_pct=500.0, enable=True, ops=500)
        self.assertFalse(self.check(summary, {}))
        self.assertFalse(self.check(summary, {"other": {"x": 1}}))

    def test_budget_increase_above_threshold(self):
        self.assertTrue(self.check(make_summary(budget_delta_pct=50.0), make_policy()))

    def test_budget_increase_below_threshold(self):
        self.assertFalse(self.check(make_summary(budget_delta_pct=5.0), make_policy()))

    def test_daily_budget_over_threshold(self):
        self.assertTrue(
            self.check(make_summary(new_daily_budget=250.0), make_policy())
        )

    def test_daily_budget_under_threshold(self):
        self.assertFalse(
            self.check(make_summary(new_daily_budget=40.0), make_policy())
        )

    def test_enable_requires_approval_when_policy_enables_rule(self):
        self.assertTrue(self.check(make_summary(enable=True), make_policy()))
        self.assertFalse(self.check(make_summary(enable=False), make_policy()))

    def test_enable_rule_disabled_in_policy(self):
        self.assertFalse(
            self.check(make_summary(enable=True), make_policy(enable=False))
        )

    def test_ops_over_threshold(self):
        self.assertTrue(self.check(make_summary(ops=40), make_policy()))
        self.assertFalse(self.check(make_summary(ops=10), make_policy()))

    def test_always_flag_forces_approval(self):
        self.assertTrue(self.check(make_summary(), make_policy(always=True)))

    def test_quiet_plan_needs_no_approval(self):
        summary = make_summary(
            budget_delta_pct=1.0,
            new_daily_budget=15.0,
            enable=False,
            ops=2,
            platform="meta",
        )
        self.assertFalse(self.check(summary, make_policy()))

    def test_reasons_are_reported_as_list(self):
        needed, reasons = approvals.needs_approval(
            make_summary(budget_delta_pct=99.0, ops=99), make_policy()
        )
        self.assertTrue(needed)
        self.assertIsInstance(reasons, list)
        self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()