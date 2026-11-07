"""ChatGPT Ads custom-audience helpers for guarded operations.

This module turns CRM rows into an upload-ready CSV containing only locally
computed SHA-256 identifiers, then plans and executes custom-audience
create/add/remove/replace calls. Raw emails and phone numbers never leave the
process. All network effects are deferred to an injected client and require an
explicit ``commit=True``. Validation and safety rails are pure and testable.
Tools should call ``plan()`` first, show the returned plan, and only then call
``execute(..., commit=True)`` with a shared idempotency key.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid

from adsops.errors import ToolError, RailViolation, scrub

_AUDIENCE_ID_RE = re.compile(r"^caud_[A-Za-z0-9_-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILE_BYTES = 500_000_000
_IDENTIFIER_HEADER = ["email_sha256", "phone_number_sha256"]


def _g(row, *path, default=None):
    """Nested dict get with a default; used for Google/OpenAI-style rows."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
        if cur is None:
            return default
    return cur if cur is not None else default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm_email(value) -> str | None:
    """Trim + lowercase, per the custom-audience docs. Do not strip dots/tags."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s.count("@") != 1:
        return None
    local, domain = s.split("@", 1)
    if not local or not domain or any(ch.isspace() for ch in s):
        return None
    return s


def _norm_phone(value) -> str | None:
    """Normalize common CRM formatting into E.164 with a leading '+'."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("00"):
        s = "+" + s[2:]
    # Remove visual separators; keep a possible leading plus.
    s = re.sub(r"[\s\-()./]", "", s)
    digits = s[1:] if s.startswith("+") else s
    if not digits.isdigit() or digits.startswith("0"):
        return None
    if not (8 <= len(digits) <= 15):
        return None
    return "+" + digits


def _prehashed(row, *names: str) -> str | None:
    for name in names:
        value = _g(row, name)
        if value is None:
            continue
        text = str(value).strip().lower()
        if _SHA_RE.fullmatch(text):
            return text
    return None


def _email_hash(row) -> str | None:
    raw = _g(row, "email") or _g(row, "email_address") or _g(row, "emailAddress")
    if raw is not None:
        normalized = _norm_email(raw)
        if normalized:
            return _sha256(normalized)
    return _prehashed(row, "email_sha256", "emailSha256")


def _phone_hash(row) -> str | None:
    raw = (
        _g(row, "phone")
        or _g(row, "phone_number")
        or _g(row, "phoneNumber")
        or _g(row, "telephone")
    )
    if raw is not None:
        normalized = _norm_phone(raw)
        if normalized:
            return _sha256(normalized)
    return _prehashed(row, "phone_number_sha256", "phoneNumberSha256")


def build_file(rows) -> tuple[str, dict]:
    """Build a CSV of SHA-256 identifiers. Never emits raw identifiers."""
    if rows is None:
        rows = []
    elif isinstance(rows, dict):
        rows = [rows]
    elif isinstance(rows, (str, bytes)):
        raise ToolError("rows must be an iterable of dicts")
    else:
        rows = list(rows)

    rows_in = len(rows)
    seen = set()
    output_rows = []
    emails = 0
    phones = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        email_hash = _email_hash(row)
        phone_hash = _phone_hash(row)
        if not email_hash and not phone_hash:
            continue
        key = (email_hash or "", phone_hash or "")
        if key in seen:
            continue
        seen.add(key)
        output_rows.append(key)
        if email_hash:
            emails += 1
        if phone_hash:
            phones += 1

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_IDENTIFIER_HEADER)
    for email_hash, phone_hash in output_rows:
        writer.writerow([email_hash, phone_hash])

    csv_text = buf.getvalue()
    stats = {
        "rows_in": rows_in,
        "rows_out": len(output_rows),
        "emails": emails,
        "phones": phones,
        "skipped": rows_in - len(output_rows),
    }
    return csv_text, stats


def plan(op: str, name: str, rows, audience_id: str | None = None,
         allow_empty: bool = False) -> dict:
    """Validate an operation and return a commit-gated plan (no I/O)."""
    if not isinstance(op, str):
        raise ToolError("op must be one of create, add, remove, replace")
    op = op.strip().lower()
    if op not in {"create", "add", "remove", "replace"}:
        raise ToolError("op must be one of create, add, remove, replace")

    name_text = "" if name is None else str(name).strip()
    if op == "create":
        if len(name_text) < 3:
            raise ToolError("audience name must contain at least 3 characters")
        if audience_id is not None:
            raise ToolError("audience_id is only valid for add, remove, or replace")
    else:
        if not isinstance(audience_id, str) or not _AUDIENCE_ID_RE.fullmatch(audience_id.strip()):
            raise ToolError("audience_id must match ^caud_[A-Za-z0-9_-]+$")
        audience_id = audience_id.strip()

    csv_text, stats = build_file(rows)
    file_bytes = len(csv_text.encode("utf-8"))
    if file_bytes >= _MAX_FILE_BYTES:
        raise RailViolation("audience file must be smaller than 500 MB")

    notes = [
        "Identifiers are normalized and SHA-256 hashed locally; raw identifiers are never sent.",
        "execute(..., commit=True) is required before any network call is made.",
    ]

    if stats["rows_out"] < 1:
        if op == "replace" and allow_empty:
            notes.append("Empty replacement allowed for exclusion-only audiences.")
        else:
            raise ToolError("no valid identifiers to send")
    elif stats["skipped"]:
        notes.append(f"{stats['skipped']} input row(s) were skipped as invalid or duplicate.")

    if op == "replace" and stats["rows_out"] == 0:
        notes.append("The replace upload contains only the CSV header.")

    return {
        "op": op,
        "name": name_text,
        "audience_id": audience_id,
        "file_bytes": file_bytes,
        "stats": stats,
        "requires_commit": True,
        "notes": notes,
    }


def _membership_path(op: str, audience_id: str) -> str:
    # The API exposes membership changes as action-style POST endpoints.
    return f"/custom_audiences/{audience_id}/members/{op}"


def execute(client, plan: dict, csv_text: str, commit: bool = False,
            idempotency_key: str | None = None) -> dict:
    """Execute a plan only when commit=True; otherwise return the plan."""
    if not commit:
        return plan

    if not isinstance(plan, dict):
        raise ToolError("plan must be a dict")
    op = plan.get("op")
    if op not in {"create", "add", "remove", "replace"}:
        raise ToolError("invalid plan op")
    if not isinstance(csv_text, str):
        raise ToolError("csv_text must be a string")

    csv_bytes = csv_text.encode("utf-8")
    if len(csv_bytes) >= _MAX_FILE_BYTES:
        raise RailViolation("audience file must be smaller than 500 MB")

    expected = plan.get("file_bytes")
    if expected is not None:
        try:
            expected_int = int(expected)
        except (TypeError, ValueError) as exc:
            raise ToolError("plan file_bytes is invalid") from exc
        if expected_int != len(csv_bytes):
            raise ToolError("csv_text size does not match plan")

    stats = plan.get("stats")
    if not isinstance(stats, dict):
        stats = {}
    if op != "replace" and int(stats.get("rows_out", 0) or 0) < 1:
        raise RailViolation("refusing to upload an empty audience file")

    filename = "audience.csv"
    upload_key = str(idempotency_key) if idempotency_key else uuid.uuid4().hex
    membership_key = str(idempotency_key) if idempotency_key else uuid.uuid4().hex

    upload = client.request(
        "POST",
        "/uploads",
        raw=({"purpose": "custom_audience"}, filename, csv_bytes),
        idempotency_key=upload_key,
    )
    file_id = _g(upload, "file_id") or _g(upload, "id") or _g(upload, "file", "id")
    if not file_id:
        raise ToolError("upload response did not include a file_id")
    file_id = str(file_id)

    if op == "create":
        name = str(plan.get("name") or "").strip()
        if len(name) < 3:
            raise ToolError("audience name must contain at least 3 characters")
        body = {
            "name": name,
            "file_id": file_id,
            "identifier_resolution": "auto",
            "filename": filename,
            "mimetype": "text/csv",
            "file_size": len(csv_bytes),
        }
        resp = client.request(
            "POST",
            "/custom_audiences",
            body=body,
            idempotency_key=membership_key,
        )
        audience_id = (
            _g(resp, "id")
            or _g(resp, "audience_id")
            or _g(resp, "custom_audience_id")
            or _g(resp, "custom_audience", "id")
        )
        if not audience_id:
            raise ToolError("create response did not include an audience id")
        status = _g(resp, "status", default="processing")
        return {"audience_id": str(audience_id), "status": str(status)}

    audience_id = plan.get("audience_id")
    if not audience_id:
        raise ToolError("plan audience_id is required")
    body = {"file_id": file_id, "identifier_resolution": "auto"}
    resp = client.request(
        "POST",
        _membership_path(op, str(audience_id)),
        body=body,
        idempotency_key=membership_key,
    )
    status = _g(resp, "status", default="submitted")
    return {"audience_id": str(audience_id), "status": str(status)}


class _FakeClient:
    """Tiny in-memory client used only by selftest."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, query=None, body=None,
                idempotency_key=None, raw=None):
        self.calls.append({
            "method": method,
            "path": path,
            "query": query,
            "body": body,
            "idempotency_key": idempotency_key,
            "raw": raw,
        })
        if not self.responses:
            raise AssertionError("unexpected client.request call")
        resp = self.responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def get(self, path, query=None):
        return self.request("GET", path, query=query)

    def list_all(self, path, query=None):
        return []


def selftest() -> int:
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        if not cond:
            raise AssertionError(msg)
        checks += 1

    def raises(exc_type, fn, msg):
        nonlocal checks
        try:
            fn()
        except exc_type:
            checks += 1
            return
        except Exception as exc:
            raise AssertionError(f"{msg}: wrong exception {type(exc).__name__}: {exc}") from exc
        raise AssertionError(f"{msg}: no exception")

    # build_file: empty input still emits a header and zeroed stats.
    csv_text, stats = build_file([])
    ok(csv_text == "email_sha256,phone_number_sha256\n", "empty CSV header")
    ok(stats == {"rows_in": 0, "rows_out": 0, "emails": 0, "phones": 0, "skipped": 0},
       "empty stats")

    # Email normalization and hashing: no raw email in output.
    email = " A@Example.COM "
    csv_text, stats = build_file([{"email": email}])
    expected_email_hash = hashlib.sha256(b"a@example.com").hexdigest()
    ok(expected_email_hash in csv_text, "email hash present")
    ok("A@Example.COM" not in csv_text, "raw email not present")
    ok(stats["emails"] == 1, "email stat counted")

    # Phone formatting, E.164 plus prefix, and local hashing.
    phone = "+1 (202) 555-0123"
    csv_text, stats = build_file([{"phone": phone}])
    expected_phone_hash = hashlib.sha256(b"+12025550123").hexdigest()
    ok(expected_phone_hash in csv_text, "phone hash present")
    ok(phone not in csv_text, "raw phone not present")
    ok(stats["phones"] == 1, "phone stat counted")

    # Both identifiers produce one output row with both cells.
    csv_text, stats = build_file([{"email": "a@x.com", "phone": "+12025550123"}])
    ok(len(csv_text.splitlines()) == 2, "one data row for both identifiers")
    ok(stats["emails"] == 1 and stats["phones"] == 1, "both stats counted")

    # Deduplication after normalization.
    csv_text, stats = build_file([{"email": "a@x.com"}, {"email": " A@X.com "}])
    ok(stats["rows_in"] == 2 and stats["rows_out"] == 1, "dedupe rows")
    ok(stats["skipped"] == 1, "dedupe skipped count")

    # Invalid rows and non-dict rows are skipped.
    csv_text, stats = build_file([{}, {"email": "bad"}, {"phone": "123"}, "notdict"])
    ok(stats["rows_in"] == 4 and stats["rows_out"] == 0, "invalid rows skipped")
    ok(stats["skipped"] == 4, "invalid skipped count")

    # plan validation and happy paths.
    raises(ToolError, lambda: plan("create", "ab", [{"email": "a@x.com"}]),
           "short create name")
    p = plan("create", "My Audience", [{"email": "a@x.com"}])
    ok(p["op"] == "create", "plan create op")
    ok(p["requires_commit"] is True, "plan requires commit")
    ok(p["file_bytes"] > 0, "plan file bytes")
    ok(p["stats"]["rows_out"] == 1, "plan rows out")

    raises(ToolError, lambda: plan("add", "Name", [{"email": "a@x.com"}]),
           "missing audience id")
    raises(ToolError, lambda: plan("add", "Name", [{"email": "a@x.com"}],
                                   audience_id="bad"),
           "bad audience id")
    raises(ToolError, lambda: plan("replace", "Name", [],
                                   audience_id="caud_1"),
           "empty replace refused")
    p_empty = plan("replace", "Name", [], audience_id="caud_1", allow_empty=True)
    ok(p_empty["stats"]["rows_out"] == 0, "empty replace allowed")
    ok(p_empty["file_bytes"] > 0, "empty replace still has header bytes")

    # execute: commit=False must not touch the client.
    csv_text, _ = build_file([{"email": "a@x.com"}])
    p = plan("create", "My Audience", [{"email": "a@x.com"}])
    fc = _FakeClient([])
    out = execute(fc, p, csv_text, commit=False)
    ok(out == p, "commit false returns plan")
    ok(len(fc.calls) == 0, "commit false no calls")

    # execute create: upload + create body and idempotency propagation.
    fc = _FakeClient([{"file_id": "file_1"},
                      {"id": "caud_123", "status": "processing"}])
    out = execute(fc, p, csv_text, commit=True, idempotency_key="idem-1")
    ok(out == {"audience_id": "caud_123", "status": "processing"}, "create result")
    ok(fc.calls[0]["path"] == "/uploads", "upload path")
    ok(fc.calls[0]["raw"][0] == {"purpose": "custom_audience"}, "upload purpose")
    ok(fc.calls[0]["raw"][1] == "audience.csv", "upload filename")
    ok(fc.calls[0]["idempotency_key"] == "idem-1", "upload idempotency key")
    ok(fc.calls[1]["path"] == "/custom_audiences", "create path")
    ok(fc.calls[1]["body"]["file_id"] == "file_1", "create file id")
    ok(fc.calls[1]["body"]["identifier_resolution"] == "auto", "create resolution")
    ok(fc.calls[1]["body"]["file_size"] == p["file_bytes"], "create file size")
    ok(fc.calls[1]["idempotency_key"] == "idem-1", "create idempotency key")

    # Missing audience id in create response is a user-visible failure.
    fc = _FakeClient([{"file_id": "file_1"}, {}])
    raises(ToolError, lambda: execute(fc, p, csv_text, commit=True),
           "missing create id")

    # add/remove/replace paths, bodies, and returned audience id.
    p_add = plan("add", "Name", [{"email": "a@x.com"}], audience_id="caud_abc")
    fc = _FakeClient([{"file_id": "file_2"}, {"status": "accepted"}])
    out = execute(fc, p_add, csv_text, commit=True, idempotency_key="k2")
    ok(out == {"audience_id": "caud_abc", "status": "accepted"}, "add result")
    ok(fc.calls[1]["path"] == "/custom_audiences/caud_abc/members/add", "add path")
    ok(fc.calls[1]["body"] == {"file_id": "file_2", "identifier_resolution": "auto"},
       "add body")

    p_remove = plan("remove", "Name", [{"email": "a@x.com"}], audience_id="caud_abc")
    fc = _FakeClient([{"file_id": "file_3"}, {"status": "accepted"}])
    execute(fc, p_remove, csv_text, commit=True)
    ok(fc.calls[1]["path"] == "/custom_audiences/caud_abc/members/remove",
       "remove path")

    p_replace = plan("replace", "Name", [{"email": "a@x.com"}], audience_id="caud_abc")
    fc = _FakeClient([{"file_id": "file_4"}, {"status": "accepted"}])
    execute(fc, p_replace, csv_text, commit=True)
    ok(fc.calls[1]["path"] == "/custom_audiences/caud_abc/members/replace",
       "replace path")

    # Empty replace can be committed: header-only CSV is still non-zero bytes.
    empty_csv, _ = build_file([])
    fc = _FakeClient([{"file_id": "file_5"}, {"status": "accepted"}])
    out = execute(fc, p_empty, empty_csv, commit=True)
    ok(out["audience_id"] == "caud_1", "empty replace execute id")

    # _g nested helper edge cases.
    ok(_g({"a": {"b": 1}}, "a", "b") == 1, "_g nested")
    ok(_g({}, "a", default="x") == "x", "_g default")

    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
