"""OpenAI Ads Conversions API sender with local hashing and safety rails.

Builds CAPI events from CRM lead rows, normalizing and SHA-256 hashing all
personally identifying identifiers locally. Raw emails, phones, names, and
external IDs never appear in return values. Sending is batched to <=1000
events, defaults to validate-only, and refuses event payloads that still
contain obvious raw identifiers. All network I/O is injected, making this
module deterministic for tests and safe to import.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import string
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote

from adsops.errors import ToolError, RailViolation, scrub

CAPI_URL = "https://bzr.openai.com/v1/events"
MAX_BATCH = 1000
FUTURE_SLACK_MS = 5 * 60 * 1000  # API allows up to 10 minutes; we are stricter.

_ACTION_SOURCES = {
    "web", "mobile_app", "offline", "physical_store",
    "phone_call", "email", "other",
}

_EVENT_TYPE_ALIASES = {
    "lead": "lead_created",
    "lead_created": "lead_created",
    "purchase": "order_created",
    "order": "order_created",
    "order_created": "order_created",
    "checkout": "checkout_started",
    "checkout_started": "checkout_started",
    "signup": "registration_completed",
    "registration": "registration_completed",
    "registration_completed": "registration_completed",
    "appointment": "appointment_scheduled",
    "appointment_scheduled": "appointment_scheduled",
    "trial": "trial_started",
    "trial_started": "trial_started",
    "subscription": "subscription_created",
    "subscription_created": "subscription_created",
    "contents_viewed": "contents_viewed",
    "items_added": "items_added",
    "page_viewed": "page_viewed",
}

# data.type must match the event shape expected by the API.  These mappings
# avoid emitting an invalid shape for the supported conversion types.
_DATA_TYPE_BY_EVENT = {
    "lead_created": "customer_action",
    "registration_completed": "customer_action",
    "appointment_scheduled": "customer_action",
    "checkout_started": "contents",
    "order_created": "contents",
    "subscription_created": "plan_enrollment",
    "trial_started": "plan_enrollment",
    "contents_viewed": "contents",
    "items_added": "contents",
    "page_viewed": "contents",
}

_ZERO_DECIMAL = frozenset(
    "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split()
)
_THREE_DECIMAL = frozenset("BHD IQD JOD KWD LYD OMR TND".split())
_RAW_PII_KEYS = {
    "email", "phone", "phone_number", "first_name", "last_name",
    "firstname", "lastname",
}


def normalize_email(s):
    """Trim, lowercase, and return an email, or None when empty."""
    if not isinstance(s, str):
        return None
    t = s.strip().lower()
    return t or None


def normalize_phone(s):
    """Remove formatting/leading + and zeroes; require 8-15 digits."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    t = "".join(ch for ch in t if not ch.isspace() and ch not in "().-")
    if t.startswith("+"):
        t = t[1:]
    if not t or not t.isdigit():
        return None
    t = t.lstrip("0")
    return t if 8 <= len(t) <= 15 else None


def normalize_name(s):
    """Lowercase and remove whitespace plus ASCII punctuation."""
    if not isinstance(s, str):
        return None
    t = "".join(
        ch for ch in s.lower()
        if not ch.isspace() and ch not in string.punctuation
    )
    return t or None


def sha256_hex(s):
    """Lowercase SHA-256 hex digest of a UTF-8 string."""
    if not isinstance(s, str):
        raise ValueError("sha256_hex requires a string")
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _g(row, *path, default=None):
    """Nested dict getter; missing or None values return default."""
    cur = row
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
        if cur is None:
            return default
    return cur


def _parse_iso_ms(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _minor_units(value, currency):
    """Convert major-unit money to integer minor units, half-up."""
    c = (currency or "USD").upper()
    factor = 1 if c in _ZERO_DECIMAL else 1000 if c in _THREE_DECIMAL else 100
    d = Decimal(str(value))
    if not d.is_finite() or d < 0:
        raise ValueError("bad amount")
    return int((d * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _clean_geo(value, limit):
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s[:limit] if s else None


def _clean_country(value):
    s = _clean_geo(value, 2)
    return s.upper() if s and len(s) == 2 and s.isalpha() else None


def build_events(
    rows,
    event_type="lead",
    now_ms=None,
    action_source="offline",
    currency="USD",
    max_age_days=7,
):
    """Build hashed CAPI events and report skipped rows by index/reason."""
    if not isinstance(rows, list):
        raise ToolError("rows must be a list")
    mapped = _EVENT_TYPE_ALIASES.get(str(event_type).strip().lower())
    if not mapped:
        raise ToolError("unsupported event_type")
    if action_source not in _ACTION_SOURCES:
        raise ToolError("unsupported action_source")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ToolError("currency must be a three-letter ISO code")
    currency = currency.upper()
    if (
        not isinstance(max_age_days, (int, float))
        or isinstance(max_age_days, bool)
        or max_age_days < 0
    ):
        raise ToolError("max_age_days must be >= 0")
    max_age_ms = int(max_age_days * 86_400_000)
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)

    events, skipped, seen = [], [], set()

    def skip(index, reason):
        skipped.append({"index": index, "reason": reason})

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            skip(i, "row is not an object")
            continue

        lead_id_raw = row.get("id")
        if lead_id_raw is None or (isinstance(lead_id_raw, str) and not lead_id_raw.strip()):
            skip(i, "missing id")
            continue
        lead_id = str(lead_id_raw).strip()
        if not lead_id:
            skip(i, "missing id")
            continue

        ts = _parse_iso_ms(row.get("created_at"))
        if ts is None:
            skip(i, "invalid or missing created_at")
            continue
        if ts < now_ms - max_age_ms:
            skip(i, "older than max_age_days")
            continue
        if ts > now_ms + FUTURE_SLACK_MS:
            skip(i, "timestamp in the future")
            continue

        email = normalize_email(row.get("email"))
        phone = normalize_phone(row.get("phone"))
        first = normalize_name(row.get("first_name"))
        last = normalize_name(row.get("last_name"))
        if not any((email, phone, first, last)):
            skip(i, "no identifier")
            continue

        amount = None
        value_raw = row.get("value")
        if value_raw is not None and value_raw != "":
            try:
                amount = _minor_units(value_raw, currency)
            except (InvalidOperation, ValueError, TypeError, OverflowError):
                skip(i, "invalid value")
                continue

        if lead_id in seen:
            skip(i, "duplicate id")
            continue
        seen.add(lead_id)

        user = {}
        for key, normalized in (
            ("emails_sha256", email),
            ("phone_numbers_sha256", phone),
            ("first_names_sha256", first),
            ("last_names_sha256", last),
        ):
            if normalized:
                user[key] = [sha256_hex(normalized)]
        # The stable lead id is a pseudonymous external id; hash it too.
        user["external_ids_sha256"] = [sha256_hex(lead_id)]

        for key, field, limit in (
            ("cities", "city", 128),
            ("regions", "state", 128),
            ("postal_codes", "zip", 32),
        ):
            v = _clean_geo(row.get(field), limit)
            if v:
                user[key] = [v]
        country = _clean_country(row.get("country"))
        if country:
            user["countries"] = [country]

        data = {"type": _DATA_TYPE_BY_EVENT.get(mapped, "customer_action")}
        if amount is not None:
            data["amount"] = amount
            data["currency"] = currency

        ev = {
            "id": "adrails_" + sha256_hex(f"{mapped}:{lead_id}")[:40],
            "type": mapped,
            "timestamp_ms": ts,
            "action_source": action_source,
            "data": data,
        }
        oppref = row.get("oppref")
        if isinstance(oppref, str) and oppref.strip():
            ev["oppref"] = oppref  # pass original, unmodified
        if action_source == "web":
            src = row.get("source_url")
            if not isinstance(src, str) or not src.strip():
                skip(i, "web events require source_url")
                continue
            ev["source_url"] = src.strip()
        ev["user"] = user
        events.append(ev)

    return {"events": events, "skipped": skipped}


def _contains_raw_pii(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _RAW_PII_KEYS:
                if isinstance(v, (str, int, float)) and str(v).strip():
                    return True
                if isinstance(v, (list, tuple)) and any(str(x).strip() for x in v):
                    return True
            if _contains_raw_pii(v):
                return True
    elif isinstance(obj, (list, tuple)):
        return any(_contains_raw_pii(x) for x in obj)
    return False


def _norm_integration_source(src):
    if not isinstance(src, str):
        raise ToolError("integration_source must be a string")
    s = src.strip().lower()
    if not (1 <= len(s) <= 64) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s):
        raise ToolError("invalid integration_source")
    return s


def _redact_error(exc, secret):
    text = str(exc)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return scrub(text)


def send(
    events,
    pixel_id,
    capi_key,
    http,
    validate_only=True,
    integration_source="adrails",
    batch_size=1000,
):
    """POST hashed events to the Conversions API in <=1000-event batches."""
    if not isinstance(events, list):
        raise ToolError("events must be a list")
    if not isinstance(pixel_id, str) or not pixel_id.strip():
        raise ToolError("pixel_id is required")
    if not isinstance(capi_key, str) or not capi_key.strip():
        raise ToolError("capi_key is required")
    if not callable(http):
        raise ToolError("http must be callable")
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= MAX_BATCH
    ):
        raise ToolError("batch_size must be between 1 and 1000")
    source = _norm_integration_source(integration_source)
    if _contains_raw_pii(events):
        raise RailViolation("refusing to send raw identifiers; hash with build_events first")

    if not events:
        return {
            "batches": 0,
            "sent": 0,
            "validate_only": bool(validate_only),
            "results": [],
        }

    url = CAPI_URL + "?pid=" + quote(pixel_id.strip(), safe="")
    headers = {
        "Authorization": f"Bearer {capi_key}",
        "Content-Type": "application/json",
    }
    results, sent = [], 0

    for start in range(0, len(events), batch_size):
        batch = events[start:start + batch_size]
        sent += len(batch)
        body = {
            "events": batch,
            "validate_only": bool(validate_only),
            "integration_source": source,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        try:
            resp = http("POST", url, headers, body_bytes)
            status = resp[0] if isinstance(resp, (tuple, list)) and len(resp) >= 2 else 0
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = 0
            results.append({"status": status, "ok": 200 <= status < 300})
        except Exception as exc:  # noqa: BLE001 - caller sees a scrubbed reason
            results.append({
                "status": 0,
                "ok": False,
                "error": _redact_error(exc, capi_key),
            })

    return {
        "batches": len(results),
        "sent": sent,
        "validate_only": bool(validate_only),
        "results": results,
    }


def redact_preview(events, n=3):
    """Return a deep copy of the first n events with hashes shortened."""
    if not isinstance(events, list):
        raise ToolError("events must be a list")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ToolError("n must be an integer >= 0")
    out = copy.deepcopy(events[:n])

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if isinstance(k, str) and k.lower().endswith("_sha256"):
                    if isinstance(v, list):
                        obj[k] = [
                            (x[:8] + "…") if isinstance(x, str) else x for x in v
                        ]
                    elif isinstance(v, str):
                        obj[k] = v[:8] + "…"
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(out)
    return out


def selftest() -> int:
    """Run mocked checks for normalization, build, send, and redaction."""
    checks = 0

    def check(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(msg)

    check(normalize_email("  Foo@Bar.COM ") == "foo@bar.com", "email normalize")
    check(normalize_email("   ") is None, "email empty")
    check(normalize_phone("+1 (415) 555-2671") == "14155552671", "phone normalize")
    check(normalize_phone("0044 20 7946 0958") == "442079460958", "phone zeros")
    check(normalize_phone("12345") is None, "phone short")
    check(normalize_phone("1" * 16) is None, "phone long")
    check(normalize_name("O'Connor") == "oconnor", "name punctuation")
    check(normalize_name("José") == "josé", "name non-ascii")
    check(normalize_name(" Mary Jane ") == "maryjane", "name spaces")
    check(normalize_name("!!!") is None, "name empty")
    check(len(sha256_hex("maryjane")) == 64, "sha256 length")
    check(
        sha256_hex("abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "sha256 known",
    )
    check(_g({"a": {"b": {"c": "5"}}}, "a", "b", "c") == "5", "_g nested")
    check(_g({}, "x", default=0) == 0, "_g default")
    check(_g({"a": None}, "a", default=7) == 7, "_g none")

    now = 1_700_000_000_000

    def iso(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    base = {
        "id": "lead-1",
        "created_at": iso(now - 1000),
        "email": " Mary@Example.COM ",
        "phone": "+1 (415) 555-2671",
        "first_name": "Mary Jane",
        "last_name": "O'Connor",
        "value": 42.0,
        "oppref": "oppref_abc",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94107",
        "country": "us",
    }
    res = build_events([base], now_ms=now)
    check(len(res["events"]) == 1 and res["skipped"] == [], "build happy")
    ev = res["events"][0]
    check(ev["type"] == "lead_created" and ev["timestamp_ms"] == now - 1000, "event meta")
    check(ev["id"].startswith("adrails_"), "event id")
    check(ev["data"]["amount"] == 4200 and ev["data"]["currency"] == "USD", "amount")
    check(ev["user"]["emails_sha256"] == [sha256_hex("mary@example.com")], "email hash")
    check(ev["user"]["phone_numbers_sha256"] == [sha256_hex("14155552671")], "phone hash")
    check(ev["user"]["first_names_sha256"] == [sha256_hex("maryjane")], "first hash")
    check(ev["user"]["last_names_sha256"] == [sha256_hex("oconnor")], "last hash")
    check(ev["user"]["external_ids_sha256"] == [sha256_hex("lead-1")], "external hash")
    check(ev["user"]["regions"] == ["CA"] and ev["user"]["countries"] == ["US"], "geo")
    dump = json.dumps(res)
    check(
        "mary@example.com" not in dump
        and "14155552671" not in dump
        and "O'Connor" not in dump,
        "no raw pii",
    )
    check(ev["oppref"] == "oppref_abc", "oppref")

    old = dict(base, id="old", created_at=iso(now - 8 * 86_400_000))
    future = dict(base, id="future", created_at=iso(now + 6 * 60 * 1000))
    age = build_events([old, future], now_ms=now)
    check(len(age["events"]) == 0 and len(age["skipped"]) == 2, "age skips")
    check(any("older" in s["reason"] for s in age["skipped"]), "old reason")
    check(any("future" in s["reason"] for s in age["skipped"]), "future reason")
    boundary = dict(base, id="boundary", created_at=iso(now + 5 * 60 * 1000))
    check(len(build_events([boundary], now_ms=now)["events"]) == 1, "future boundary")
    noid = {"created_at": iso(now), "email": "x@y.com"}
    check(build_events([noid], now_ms=now)["skipped"][0]["reason"] == "missing id", "missing id")
    noident = {"id": "n1", "created_at": iso(now), "city": "X"}
    check("no identifier" in build_events([noident], now_ms=now)["skipped"][0]["reason"], "no identifier")
    dup = build_events([base, dict(base)], now_ms=now)
    check(len(dup["events"]) == 1 and dup["skipped"][0]["reason"] == "duplicate id", "duplicate")
    bad_ts = dict(base, id="badts", created_at="not-a-date")
    check("created_at" in build_events([bad_ts], now_ms=now)["skipped"][0]["reason"], "bad ts")
    neg = dict(base, id="neg", value=-1)
    check("invalid value" in build_events([neg], now_ms=now)["skipped"][0]["reason"], "neg value")
    jpy = dict(base, id="jpy", value=1000.5)
    check(build_events([jpy], now_ms=now, currency="JPY")["events"][0]["data"]["amount"] == 1001, "jpy minor")
    try:
        build_events([], max_age_days=-1)
        check(False, "negative age should raise")
    except ToolError:
        check(True, "negative age")
    try:
        build_events([], event_type="nope")
        check(False, "bad event type should raise")
    except ToolError:
        check(True, "bad event type")

    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url, dict(headers), body))
        return 200, '{"ok":true}'

    live = [
        {"id": f"e{i}", "type": "lead_created", "timestamp_ms": now, "data": {"type": "customer_action"}}
        for i in range(3)
    ]
    out = send(live, "px_1", "sekret", fake_http, batch_size=2)
    check(out["batches"] == 2 and out["sent"] == 3 and out["validate_only"] is True, "send summary")
    check(len(calls) == 2 and all(c[0] == "POST" for c in calls), "send calls")
    check(calls[0][1] == "https://bzr.openai.com/v1/events?pid=px_1", "send url")
    check(calls[0][2]["Authorization"] == "Bearer sekret", "auth header")
    body0 = json.loads(calls[0][3].decode("utf-8"))
    check(body0["validate_only"] is True and body0["integration_source"] == "adrails", "body flags")
    check(len(body0["events"]) == 2, "batch size")
    check("sekret" not in json.dumps(out), "no secret in result")
    calls.clear()
    empty = send([], "px_1", "sekret", fake_http)
    check(empty["batches"] == 0 and empty["sent"] == 0 and calls == [], "empty send")
    calls.clear()
    send(live, "px_1", "sekret", fake_http, validate_only=False, batch_size=1000)
    check(json.loads(calls[0][3].decode())["validate_only"] is False, "validate false")

    try:
        send(live, "", "sekret", fake_http)
        check(False, "blank pid should raise")
    except ToolError:
        check(True, "blank pid")
    try:
        send(live, "px", "sekret", fake_http, integration_source="bad source")
        check(False, "bad source should raise")
    except ToolError:
        check(True, "bad source")
    try:
        send(live, "px", "sekret", fake_http, batch_size=0)
        check(False, "bad batch should raise")
    except ToolError:
        check(True, "bad batch")

    def bad_http(method, url, headers, body):
        raise RuntimeError("boom sekret")

    failed = send(live[:1], "px", "sekret", bad_http)
    check(failed["results"][0]["ok"] is False and failed["results"][0]["status"] == 0, "http error")
    check("sekret" not in json.dumps(failed) and "boom" in json.dumps(failed), "error scrubbed")
    try:
        send([{"id": "x", "email": "a@b.com"}], "px", "sekret", fake_http)
        check(False, "raw pii should refuse")
    except RailViolation:
        check(True, "raw pii refusal")

    h = "a" * 64
    original = [{"id": "e", "user": {"emails_sha256": [h], "cities": ["X"]}}]
    preview = redact_preview(original, 3)
    check(preview[0]["user"]["emails_sha256"][0] == "a" * 8 + "…", "redact hash")
    check(original[0]["user"]["emails_sha256"][0] == h, "redact no mutate")
    check(redact_preview(original, 0) == [], "redact zero")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
