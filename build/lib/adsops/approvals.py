"""Out-of-band human approval queue for risky ad-account changes.

An agent *proposes* a change (plan_id + summary + risk); a human approves or
rejects it out-of-band (Slack button, CLI, email) with an HMAC-signed
capability token.  Any tool that mutates an ad account must gate on
``ApprovalQueue.require_approved(plan_id)``; nothing here talks to Google.

Safety properties: the HMAC secret is caller-supplied and never persisted;
state is an append-only JSON-lines file (mode 0o600) replayed on every read,
so a corrupt or truncated line cannot flip a decision; tokens are compared in
constant time and never appear in ``status``/``list`` output; free text is
scrubbed before it is stored.  ``now`` is injectable so tests never sleep.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time

from adsops.errors import RailViolation, ToolError, scrub

# action name used inside a signed token -> resulting request status
_ACTIONS = {"approve": "approved", "reject": "rejected", "expire": "expired"}
# every status a request can have (used to validate list() filters)
_STATUSES = frozenset({"pending", *_ACTIONS.values()})


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _num(value, default=None):
    """Coerce JSON/strings to float defensively; never raise."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(ts):
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return None


def _sign(secret: bytes, request_id: str, action: str, plan_id: str) -> str:
    """HMAC over request_id|action|plan_id - plan binding stops token reuse."""
    msg = f"{request_id}|{action}|{plan_id}".encode("utf-8")
    return _b64e(hmac.new(secret, msg, hashlib.sha256).digest())


def _make_token(secret: bytes, request_id: str, action: str, plan_id: str) -> str:
    body = f"{request_id}.{action}.{_sign(secret, request_id, action, plan_id)}"
    return _b64e(body.encode("utf-8"))


def _parse_token(token):
    """Return (request_id, action, sig) or raise RailViolation (never echoes token)."""
    if not isinstance(token, str) or not token.strip():
        raise RailViolation("missing approval token")
    try:
        body = _b64d(token.strip()).decode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise RailViolation("tampered approval token")
    parts = body.split(".")
    if len(parts) != 3:
        raise RailViolation("tampered approval token")
    rid, action, sig = parts
    if not rid or action not in ("approve", "reject") or not sig or not sig.isascii():
        raise RailViolation("tampered approval token")
    return rid, action, sig


class ApprovalQueue:
    """File-backed, HMAC-signed approval queue (append-only event log)."""

    def __init__(self, path, secret: bytes, now=time.time, ttl_s=86400):
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 8:
            raise ToolError("approval secret must be at least 8 bytes")
        if not path:
            raise ToolError("approval store path is required")
        ttl = _num(ttl_s)
        if ttl is None or ttl <= 0:
            raise ToolError("ttl_s must be a positive number")
        self.path = os.fspath(path)
        self.secret = bytes(secret)
        self._now = now
        self.ttl_s = float(ttl)
        self._ensure_file()

    # -- storage ---------------------------------------------------------
    def _ensure_file(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
        os.chmod(self.path, 0o600)  # tighten even pre-existing files

    def _append(self, event: dict) -> None:
        data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            while data:  # os.write may be partial
                data = data[os.write(fd, data):]
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    def _load(self) -> "dict[str, dict]":
        """Replay the log.  Corrupt/unknown lines are skipped, never fatal."""
        records: "dict[str, dict]" = {}
        try:
            fh = open(self.path, "r", encoding="utf-8")
        except FileNotFoundError:
            return records
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                rid = ev.get("request_id")
                if not isinstance(rid, str) or not rid:
                    continue
                kind = ev.get("event")
                if kind == "request":
                    if rid in records:
                        continue  # ids are unique; first write wins
                    plan_id = ev.get("plan_id")
                    if not isinstance(plan_id, str) or not plan_id:
                        continue
                    at = _num(ev.get("at"), 0.0) or 0.0
                    ttl = _num(ev.get("ttl_s"), self.ttl_s) or self.ttl_s
                    records[rid] = {
                        "request_id": rid,
                        "plan_id": plan_id,
                        "campaign": str(ev.get("campaign") or ""),
                        "summary": str(ev.get("summary") or ""),
                        "risk": str(ev.get("risk") or "high"),
                        "requested_by": str(ev.get("requested_by") or "agent"),
                        "status": "pending",
                        "created_at": at,
                        "expires_at": at + ttl,
                        "decided_at": None,
                        "decided_by": None,
                    }
                elif kind == "decision":
                    rec = records.get(rid)
                    if rec is None or rec["status"] != "pending":
                        continue  # decisions on unknown/decided requests are ignored
                    status = _ACTIONS.get(ev.get("action"))
                    if status is None:
                        continue
                    rec["status"] = status
                    rec["decided_at"] = _num(ev.get("at"), 0.0) or 0.0
                    rec["decided_by"] = str(ev.get("decided_by") or "human")
        return records

    @staticmethod
    def _view(rec: dict) -> dict:
        """Public JSON view - deliberately excludes tokens."""
        return {
            "request_id": rec["request_id"],
            "plan_id": rec["plan_id"],
            "campaign": rec["campaign"],
            "summary": rec["summary"],
            "risk": rec["risk"],
            "requested_by": rec["requested_by"],
            "status": rec["status"],
            "created_at": _iso(rec["created_at"]),
            "expires_at": _iso(rec["expires_at"]),
            "decided_at": _iso(rec["decided_at"]),
            "decided_by": rec["decided_by"],
        }

    # -- public API ------------------------------------------------------
    def request(self, plan_id, campaign, summary, risk="high", requested_by="agent") -> dict:
        if not isinstance(plan_id, str) or not plan_id.strip() or len(plan_id) > 200:
            raise ToolError("plan_id is required (<=200 chars)")
        if "\n" in plan_id or "\r" in plan_id:
            raise ToolError("plan_id must be a single line")
        if not isinstance(campaign, str) or not campaign.strip():
            raise ToolError("campaign is required")
        now = float(self._now())
        records = self._load()

        # Only one pending request per plan: reuse it while it is still live.
        existing = None
        for rec in records.values():
            if rec["plan_id"] == plan_id and rec["status"] == "pending":
                existing = rec
        if existing is not None:
            if now <= existing["expires_at"]:
                out = self._view(existing)
                out["approve_token"] = _make_token(self.secret, existing["request_id"], "approve", plan_id)
                out["reject_token"] = _make_token(self.secret, existing["request_id"], "reject", plan_id)
                return out
            # Stale pending request: retire it so the plan can be re-proposed.
            self._append({"event": "decision", "request_id": existing["request_id"],
                          "action": "expire", "at": now, "decided_by": "system"})

        rid = "req_" + secrets.token_hex(6)
        rec = {
            "request_id": rid,
            "plan_id": plan_id,
            "campaign": scrub(str(campaign), 200),
            "summary": scrub(str(summary or ""), 500),
            "risk": str(risk or "high")[:20],
            "requested_by": scrub(str(requested_by or "agent"), 120),
            "status": "pending",
            "created_at": now,
            "expires_at": now + self.ttl_s,
            "decided_at": None,
            "decided_by": None,
        }
        self._append({"event": "request", "request_id": rid, "plan_id": plan_id,
                      "campaign": rec["campaign"], "summary": rec["summary"],
                      "risk": rec["risk"], "requested_by": rec["requested_by"],
                      "at": now, "ttl_s": self.ttl_s})
        out = self._view(rec)
        out["approve_token"] = _make_token(self.secret, rid, "approve", plan_id)
        out["reject_token"] = _make_token(self.secret, rid, "reject", plan_id)
        return out

    def decide(self, token, decided_by="human") -> dict:
        rid, action, sig = _parse_token(token)
        rec = self._load().get(rid)
        if rec is None:
            raise ToolError(f"unknown approval request {rid}")
        expected = _sign(self.secret, rid, action, rec["plan_id"])
        try:
            match = hmac.compare_digest(expected, sig)
        except TypeError:
            match = False
        if not match:
            raise RailViolation(f"tampered approval token for request {rid}")
        if rec["status"] != "pending":
            raise RailViolation(f"request {rid} is already {rec['status']}")
        now = float(self._now())
        if now > rec["expires_at"]:  # expires_at itself is still valid
            raise RailViolation(f"request {rid} expired at {_iso(rec['expires_at'])}")
        who = scrub(str(decided_by or "human"), 120)
        self._append({"event": "decision", "request_id": rid, "action": action,
                      "at": now, "decided_by": who})
        rec = dict(rec, status=_ACTIONS[action], decided_at=now, decided_by=who)
        return self._view(rec)

    def status(self, request_id) -> dict:
        if not isinstance(request_id, str) or not request_id:
            raise ToolError("request_id is required")
        rec = self._load().get(request_id)
        if rec is None:
            raise ToolError(f"unknown approval request {request_id}")
        return self._view(rec)

    def list(self, status=None, campaign=None) -> list:
        # NB: "pending" is a valid filter too - check against all statuses.
        if status is not None and status not in _STATUSES:
            raise ToolError("status must be one of pending/approved/rejected/expired")
        out = [self._view(rec) for rec in self._load().values()
               if (status is None or rec["status"] == status)
               and (campaign is None or rec["campaign"] == campaign)]
        out.reverse()  # newest first
        return out

    def require_approved(self, plan_id) -> dict:
        if not isinstance(plan_id, str) or not plan_id:
            raise RailViolation("plan needs human approval: no plan_id given")
        mine = [rec for rec in self._load().values() if rec["plan_id"] == plan_id]
        if not mine:
            raise RailViolation(f"plan {plan_id} needs human approval: no request on file")
        rec = mine[-1]  # replay order == chronological order
        if rec["status"] == "approved":
            if float(self._now()) > rec["expires_at"]:
                raise RailViolation(
                    f"plan {plan_id} needs human approval: request {rec['request_id']} is expired")
            return self._view(rec)
        raise RailViolation(
            f"plan {plan_id} needs human approval: request {rec['request_id']} is {rec['status']}")


def needs_approval(plan_summary: dict, policy: dict):
    """Return (required, reasons) for a proposed plan under policy['approval']."""
    summary = plan_summary if isinstance(plan_summary, dict) else {}
    cfg = policy.get("approval") if isinstance(policy, dict) else None
    if not isinstance(cfg, dict):
        return False, []  # no policy section -> nothing is gated
    reasons = []

    if cfg.get("always") is True:
        reasons.append("policy: approval always required")
    delta = _num(summary.get("budget_delta_pct"))
    limit = _num(cfg.get("budget_increase_pct"))
    if delta is not None and limit is not None and delta > limit:
        reasons.append(f"budget increase {delta:g}% > {limit:g}%")
    daily = _num(summary.get("new_daily_budget"))
    cap = _num(cfg.get("daily_budget_over"))
    if daily is not None and cap is not None and daily > cap:
        reasons.append(f"daily budget {daily:g} > {cap:g}")
    if summary.get("enable") is True and cfg.get("enable") is True:
        reasons.append("enabling a campaign requires approval")
    ops = _num(summary.get("ops"))
    ops_over = _num(cfg.get("ops_over"))
    if ops is not None and ops_over is not None and ops > ops_over:
        reasons.append(f"{ops:g} operations > {ops_over:g}")
    # Optional extension threshold; the default policy does not define it.
    extend = _num(summary.get("end_date_extend_days"))
    extend_over = _num(cfg.get("end_date_extend_days"))
    if extend is not None and extend_over is not None and extend > extend_over:
        reasons.append(f"end date extension {extend:g}d > {extend_over:g}d")
    return bool(reasons), reasons


def selftest() -> int:
    checks = 0

    def ok(cond, msg=""):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(msg or f"selftest check {checks} failed")

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "approvals.jsonl")
        clock = [1_700_000_000.0]
        secret = b"s3cret-key-0123456789"
        q = ApprovalQueue(path, secret, now=lambda: clock[0], ttl_s=100)

        ok(os.path.exists(path), "store file created")
        ok((os.stat(path).st_mode & 0o777) == 0o600, "store file mode is 0600")

        a = q.request("plan-1", "camp-1", "raise budget to 500", risk="high")
        ok(a["status"] == "pending" and a["plan_id"] == "plan-1")
        ok(a["request_id"].startswith("req_"))
        ok(isinstance(a["expires_at"], str) and a["expires_at"].endswith("Z"))
        ok(bool(a["approve_token"]) and a["approve_token"] != a["reject_token"])

        again = q.request("plan-1", "camp-1", "different summary")
        ok(again["request_id"] == a["request_id"], "duplicate pending plan returns existing")
        ok(q.status(a["request_id"])["summary"] == "raise budget to 500")

        b = q.request("plan-2", "camp-2", "enable campaign", risk="medium")
        ok(b["request_id"] != a["request_id"])
        ok([r["request_id"] for r in q.list()] == [b["request_id"], a["request_id"]], "newest first")
        ok(len(q.list(status="pending")) == 2, "pending is a valid status filter")
        ok(len(q.list(campaign="camp-2")) == 1 and q.list(campaign="camp-2")[0]["plan_id"] == "plan-2")
        ok("approve_token" not in q.list()[0], "list never leaks tokens")
        ok("approve_token" not in q.status(a["request_id"]), "status never leaks tokens")

        approved = q.decide(a["approve_token"], "alice")
        ok(approved["status"] == "approved" and approved["decided_by"] == "alice")
        ok(q.status(a["request_id"])["status"] == "approved" and approved["decided_at"] is not None)

        try:
            q.decide(a["approve_token"], "alice")
            ok(False, "double decide must refuse")
        except RailViolation:
            ok(True)

        wrong = _make_token(secret, b["request_id"], "approve", "plan-other")
        try:
            q.decide(wrong, "mallory")
            ok(False, "wrong-message signature must refuse")
        except RailViolation:
            ok(True)
        try:
            q.decide("!!!", "mallory")
            ok(False, "garbage token must refuse")
        except RailViolation:
            ok(True)
        try:
            q.decide("", "mallory")
            ok(False, "empty token must refuse")
        except RailViolation:
            ok(True)
        try:
            q.decide(_make_token(secret, "req_ghost", "approve", "plan-1"), "mallory")
            ok(False, "unknown request must raise ToolError")
        except ToolError:
            ok(True)

        ok(q.decide(b["reject_token"], "bob")["status"] == "rejected")
        ok(q.require_approved("plan-1")["status"] == "approved")
        try:
            q.require_approved("plan-2")
            ok(False, "rejected plan must refuse")
        except RailViolation as exc:
            ok("needs human approval" in str(exc) and "rejected" in str(exc))
        try:
            q.require_approved("plan-missing")
            ok(False, "unknown plan must refuse")
        except RailViolation:
            ok(True)

        # Replay from disk in a fresh instance (state is the log, not memory).
        q2 = ApprovalQueue(path, secret, now=lambda: clock[0], ttl_s=100)
        ok(q2.status(a["request_id"])["status"] == "approved")
        ok(len(q2.list()) == 2)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not json\n{\"event\":\"decision\"}\n{\"event\":\"bogus\",\"request_id\":\"x\"}\n")
        ok(len(q2.list()) == 2, "corrupt lines skipped")

        # Expiry: refuse decisions, then let the plan be re-proposed.
        c = q.request("plan-3", "camp-3", "extend end date", risk="low")
        clock[0] += 101
        try:
            q.decide(c["approve_token"], "alice")
            ok(False, "expired request must refuse")
        except RailViolation as exc:
            ok("expired" in str(exc))
        try:
            q.require_approved("plan-3")
            ok(False, "expired approval must refuse")
        except RailViolation:
            ok(True)
        d = q.request("plan-3", "camp-3", "extend end date")
        ok(d["request_id"] != c["request_id"], "expired pending request is superseded")
        ok(q.status(c["request_id"])["status"] == "expired")
        ok(q.status(d["request_id"])["status"] == "pending")

        try:
            q.request("", "camp-1", "x")
            ok(False, "blank plan_id must raise")
        except ToolError:
            ok(True)
        try:
            q.request("plan-9", "", "x")
            ok(False, "blank campaign must raise")
        except ToolError:
            ok(True)
        try:
            q.status("req_nope")
            ok(False, "unknown request_id must raise")
        except ToolError:
            ok(True)
        try:
            ApprovalQueue(path, b"short")
            ok(False, "weak secret must raise")
        except ToolError:
            ok(True)
        try:
            q.list(status="bogus")
            ok(False, "bad status filter must raise")
        except ToolError:
            ok(True)

    # Policy evaluation (pure, no I/O).
    pol = {"approval": {"budget_increase_pct": 20, "daily_budget_over": 100,
                        "enable": True, "ops_over": 25, "always": False}}
    res = needs_approval({"budget_delta_pct": 5}, pol)
    ok(res == (False, []) and isinstance(res[1], list))
    hit, why = needs_approval({"budget_delta_pct": 25.0}, pol)
    ok(hit and any("budget increase" in r for r in why))
    ok(needs_approval({"budget_delta_pct": 20}, pol)[0] is False, "boundary is not exceeded")
    ok(needs_approval({"new_daily_budget": "250.5"}, pol)[0] is True, "string numbers coerced")
    ok(needs_approval({"new_daily_budget": 100}, pol)[0] is False)
    ok(needs_approval({"enable": True}, pol)[0] is True)
    ok(needs_approval({"enable": False}, pol)[0] is False)
    ok(needs_approval({"ops": 26}, pol)[0] is True)
    ok(needs_approval({"ops": 25}, pol)[0] is False)
    ok(needs_approval({}, {}) == (False, []), "missing policy section")
    ok(needs_approval({"budget_delta_pct": 99}, {"approval": {"always": True}})[0] is True)
    ok(needs_approval({"budget_delta_pct": None}, pol)[0] is False)
    ok(needs_approval({"budget_delta_pct": "n/a"}, pol)[0] is False, "malformed number ignored")
    ok(needs_approval({"enable": True, "ops": 30}, {"approval": {"enable": True, "ops_over": 25}})[1]
       == ["enabling a campaign requires approval", "30 operations > 25"])
    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
