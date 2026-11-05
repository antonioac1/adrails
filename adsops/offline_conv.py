"""Offline / enhanced conversion payload builders (PII never leaves the host).

Turns operator rows (CSV/JSON dicts) into a Google Ads click-conversion
`uploadClickConversions` payload and an OpenAI CAPI events payload. Emails,
phones and names are normalised and SHA-256 hashed *locally*; raw PII never
appears in a payload, a return value, a log line or an exception message.
Google payloads are ALWAYS `validateOnly=True` -- the calling tool may clear
that flag only after a reviewed dry run. Bad rows never abort a batch: they are
reported in `skipped` as {"index", "reason"}. Everything here is pure and
deterministic (no I/O, no clock), so it is safe to unit test.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from datetime import datetime, timezone

from adsops.errors import ToolError, scrub

# OpenAI's exact CAPI field names are (as of writing) not fully verified by us.
# Every wire name we emit for that payload lives here so it can be fixed once.
FIELD_MAP = {
    "event_name": "event_name",
    "event_time": "event_time",
    "event_id": "event_id",
    "email": "email_sha256",
    "phone": "phone_sha256",
    "first_name": "first_name_sha256",
    "last_name": "last_name_sha256",
    "obref": "obref",
    "value": "value",
    "currency": "currency",
}

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
_PUNCT_NO_HYPHEN = {c for c in string.punctuation if c != "-"}
_CLICK_KEYS = ("gclid", "gbraid", "wbraid")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _g(obj, *path, default=None):
    """Nested get through camelCase dicts: _g(row, "metrics", "costMicros").

    Missing keys, None values and non-dict mid-paths all yield `default`.
    Falls back to a case-insensitive key match so "costmicros" still resolves.
    """
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key in cur:
            cur = cur[key]
            continue
        low = str(key).lower()
        match = next((k for k in cur if isinstance(k, str) and k.lower() == low), None)
        if match is None:
            return default
        cur = cur[match]
    return default if cur is None else cur


def _s(value) -> str:
    """Coerce to a stripped string; '' for None / bool / containers."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _num(value, default: float = 0.0) -> float:
    """Defensive float(): Google hands numbers back as strings, commas included."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return default


def _parse_time(value) -> datetime:
    """ISO-8601 (Z, +hh:mm, naive=UTC) or epoch seconds -> aware datetime."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ToolError("conversion_time is required (ISO-8601)")
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError(f"invalid conversion_time: {scrub(text)}") from None
    if moment.tzinfo is None:  # Google wants an explicit offset; assume UTC.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _google_time(moment: datetime) -> str:
    """Aware datetime -> 'yyyy-mm-dd hh:mm:ss+|-hh:mm' (Google Ads wire format)."""
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S%z")
    if len(stamp) >= 5 and stamp[-5] in "+-":
        stamp = f"{stamp[:-2]}:{stamp[-2:]}"
    return stamp


# --------------------------------------------------------------------------
# normalisers + hashing (the only place raw PII is ever touched)
# --------------------------------------------------------------------------
def normalize_email(s) -> str:
    """Trim/lowercase; gmail drops '+tag' and dots in the local part.

    Malformed values (no '@', empty local/domain) return '' so callers can
    treat them as 'no identifier' instead of hashing garbage.
    """
    value = s.strip().lower() if isinstance(s, str) else ""
    if not value:
        return ""
    local, sep, domain = value.partition("@")
    if not sep or not local or not domain:
        return ""
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}" if local else ""


def normalize_phone(s, default_country: str = "1") -> str:
    """To E.164 '+<digits>'; 10 digits get `default_country` prefixed.

    Raises ToolError when fewer than 8 or more than 15 digits remain.
    """
    raw = s.strip() if isinstance(s, str) else ""
    digits = re.sub(r"\D", "", raw)
    cc = re.sub(r"\D", "", _s(default_country)) or "1"
    if len(digits) == 10 and not raw.startswith("+"):
        digits = cc + digits
    if not digits or not (8 <= len(digits) <= 15):
        raise ToolError(f"invalid phone number (need 8-15 digits, got {len(digits)})")
    return "+" + digits


def normalize_name(s) -> str:
    """Trim/lowercase, drop punctuation except '-', collapse whitespace."""
    if not isinstance(s, str):
        return ""
    value = s.strip().lower()
    value = "".join(ch for ch in value if ch not in _PUNCT_NO_HYPHEN)
    return re.sub(r"\s+", " ", value).strip()


def sha256_hex(s) -> str:
    """Lowercase SHA-256 hex digest of the UTF-8 bytes (None -> hash of '')."""
    text = s if isinstance(s, str) else ("" if s is None else str(s))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hashed_identifiers(row: dict) -> tuple[list, str, str, str, str]:
    """Normalise one row's identifiers. Returns (googleIds, email, phone, first, last)."""
    email = normalize_email(row.get("email") or row.get("email_address"))
    try:
        phone = normalize_phone(row.get("phone") or row.get("phone_number"))
    except ToolError:
        phone = ""  # a bad phone must not kill a row that still has a gclid/email
    first = normalize_name(row.get("first_name") or row.get("firstName"))
    last = normalize_name(row.get("last_name") or row.get("lastName"))

    ids: list = []
    if email:
        ids.append({"hashedEmail": sha256_hex(email)})
    if phone:
        ids.append({"hashedPhoneNumber": sha256_hex(phone)})
    address = {}
    if first:
        address["hashedFirstName"] = sha256_hex(first)
    if last:
        address["hashedLastName"] = sha256_hex(last)
    if address:
        ids.append({"addressInfo": address})
    return ids, email, phone, first, last


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def build_google_click_conversions(rows, conversion_action_resource, currency="USD") -> dict:
    """Build an uploadClickConversions body (dry-run only: validateOnly=True)."""
    resource = _s(conversion_action_resource)
    if not resource:
        raise ToolError("conversion_action_resource is required (customers/<id>/conversionActions/<id>)")
    code = _s(currency).upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ToolError("currency must be a 3-letter ISO-4217 code, e.g. USD")
    if rows is None:
        rows = []
    if not isinstance(rows, (list, tuple)):
        raise ToolError("rows must be a list of objects")

    conversions: list = []
    skipped: list = []
    seen_orders: set = set()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped.append({"index": index, "reason": "row is not an object"})
            continue

        # Exactly one click id is emitted per conversion (Google's schema).
        click_key = next((k for k in _CLICK_KEYS if _s(row.get(k))), "")

        ids, email, phone, first, last = _hashed_identifiers(row)
        if not click_key and not ids:
            skipped.append({"index": index, "reason": "no click id and no user identifier"})
            continue

        order_id = _s(row.get("order_id") or row.get("orderId"))
        if order_id and order_id in seen_orders:
            skipped.append({"index": index, "reason": "duplicate order_id"})
            continue

        try:
            stamp = _google_time(
                _parse_time(row.get("conversion_time") or row.get("conversionTime") or row.get("timestamp"))
            )
        except ToolError as exc:
            skipped.append({"index": index, "reason": str(exc)})
            continue

        raw_value = row.get("value")
        if raw_value is None or raw_value == "":
            micros = row.get("value_micros", row.get("valueMicros"))
            value = round(_num(micros) / 1_000_000, 2) if micros not in (None, "") else 0.0
        else:
            value = round(_num(raw_value), 2)

        conv = {
            "conversionAction": resource,
            "conversionDateTime": stamp,
            "conversionValue": value,
            "currencyCode": code,
        }
        if order_id:
            conv["orderId"] = order_id
        if click_key:
            conv[click_key] = _s(row.get(click_key))
        if ids:
            conv["userIdentifiers"] = ids
        conversions.append(conv)
        if order_id:
            seen_orders.add(order_id)

    return {
        "conversions": conversions,
        "partialFailure": True,
        "validateOnly": True,  # never flipped here; the tool flips it post-review
        "skipped": skipped,
    }


def build_openai_capi_events(rows, pixel_id, event_name="lead_created") -> dict:
    """Build OpenAI CAPI events. `pixel_id` is validated here, sent by the caller."""
    if not _s(pixel_id):
        raise ToolError("pixel_id is required")
    name = _s(event_name)
    if not name:
        raise ToolError("event_name is required")
    if rows is None:
        rows = []
    if not isinstance(rows, (list, tuple)):
        raise ToolError("rows must be a list of objects")

    events: list = []
    skipped: list = []
    seen_orders: set = set()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped.append({"index": index, "reason": "row is not an object"})
            continue

        _, email, phone, first, last = _hashed_identifiers(row)
        if not (email or phone or first or last):
            skipped.append({"index": index, "reason": "no user identifier"})
            continue

        order_id = _s(row.get("order_id") or row.get("orderId"))
        if order_id and order_id in seen_orders:
            skipped.append({"index": index, "reason": "duplicate order_id"})
            continue

        try:
            moment = _parse_time(
                row.get("conversion_time") or row.get("conversionTime") or row.get("timestamp")
            )
        except ToolError as exc:
            skipped.append({"index": index, "reason": str(exc)})
            continue

        user: dict = {}
        if email:
            user[FIELD_MAP["email"]] = [sha256_hex(email)]
        if phone:
            user[FIELD_MAP["phone"]] = [sha256_hex(phone)]
        if first:
            user[FIELD_MAP["first_name"]] = [sha256_hex(first)]
        if last:
            user[FIELD_MAP["last_name"]] = [sha256_hex(last)]
        obref = _s(row.get("obref"))
        if obref:
            user[FIELD_MAP["obref"]] = obref

        # Stable fallback id: identical rows dedupe on the OpenAI side.
        event_id = order_id or sha256_hex(json.dumps(row, sort_keys=True, default=str))[:32]

        events.append({
            FIELD_MAP["event_name"]: name,
            FIELD_MAP["event_time"]: int(moment.timestamp()),
            FIELD_MAP["event_id"]: event_id,
            "user": user,
            "custom_data": {
                FIELD_MAP["value"]: round(_num(row.get("value")), 2),
                FIELD_MAP["currency"]: (_s(row.get("currency")) or "USD").upper(),
            },
        })
        if order_id:
            seen_orders.add(order_id)

    return {"events": events, "skipped": skipped}


# --------------------------------------------------------------------------
# display safety
# --------------------------------------------------------------------------
def _is_hash_key(key) -> bool:
    low = key.lower() if isinstance(key, str) else ""
    return "hash" in low or "sha256" in low or "sha_256" in low


def _mask(value):
    if isinstance(value, str):
        return value[:8] + "\u2026" if len(value) > 8 else value
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    return value


def _walk(node):
    if isinstance(node, dict):
        return {k: (_mask(v) if _is_hash_key(k) else _walk(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    return node


def redact_report(payload):
    """Copy of `payload` with hashed identifiers truncated to 8 chars + '…'."""
    if not isinstance(payload, (dict, list)):
        raise ToolError("payload must be a dict or list")
    return _walk(payload)


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def selftest() -> int:
    """Self-check. No I/O happens in this module, so there is nothing to mock."""
    n = 0

    def check(cond, label):
        nonlocal n
        if not cond:
            raise AssertionError(f"selftest failed: {label}")
        n += 1

    # -- normalisers ------------------------------------------------------
    check(normalize_email("  Foo.Bar+tag@Gmail.com ") == "foobar@gmail.com", "gmail dot/plus")
    check(normalize_email("John@GoogleMail.COM") == "john@googlemail.com", "googlemail lower")
    check(normalize_email(" A@B.com") == "a@b.com", "plain email")
    check(normalize_email("") == "" and normalize_email(None) == "", "empty email")
    check(normalize_email("nope") == "", "malformed email dropped")

    check(normalize_phone("(415) 555-0100") == "+14155550100", "us phone")
    check(normalize_phone("+44 20 7946 0958") == "+442079460958", "intl phone")
    check(normalize_phone("4155550100", default_country="+1") == "+14155550100", "default country")
    for bad in ("123", "1" * 16, ""):
        try:
            normalize_phone(bad)
        except ToolError:
            n += 1
        else:
            raise AssertionError(f"selftest failed: phone {bad!r} accepted")

    check(normalize_name("  O'Brien   Smith ") == "obrien smith", "name punctuation")
    check(normalize_name("Anne-Marie  D.") == "anne-marie d", "hyphen kept")
    check(sha256_hex("a") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb", "sha256")

    # -- google click conversions ----------------------------------------
    rows = [
        {"gclid": "Cj0KCQ==", "email": "Jane.Doe@gmail.com", "phone": "415-555-0100",
         "first_name": "Jane", "last_name": "Doe", "order_id": "A-1",
         "conversion_time": "2026-11-05T10:00:00Z", "value": 12.5},
        {"gbraid": "gb-1", "email": "bob@example.com", "order_id": "A-2",
         "conversion_time": "2026-11-05T10:00:00-05:00"},
        {"order_id": "A-3", "conversion_time": "2026-11-05T10:00:00Z"},          # no ids
        {"gclid": "g-4", "email": "bob@example.com", "order_id": "A-2",
         "conversion_time": "2026-11-05T10:00:00Z"},                             # duplicate
        {"gclid": "g-5", "phone": "123", "conversion_time": "2026-11-05T10:00:00Z"},
        {"gclid": "g-6", "email": "x@y.com"},                                    # no time
        "not-a-row",
    ]
    out = build_google_click_conversions(rows, "customers/1/conversionActions/2")
    check(out["validateOnly"] is True and out["partialFailure"] is True, "always validateOnly")
    convs = out["conversions"]
    check(len(convs) == 3, "three conversions kept")
    c0 = convs[0]
    check(c0["conversionDateTime"] == "2026-11-05 10:00:00+00:00", "utc format")
    check(c0["conversionValue"] == 12.5, "value kept")
    check(c0["currencyCode"] == "USD" and c0["conversionAction"].endswith("/2"), "currency+action")
    check(c0["orderId"] == "A-1" and c0["gclid"] == "Cj0KCQ==", "ids kept")
    check({"hashedEmail": sha256_hex("janedoe@gmail.com")} in c0["userIdentifiers"], "gmail hash")
    check({"addressInfo": {"hashedFirstName": sha256_hex("jane"),
                           "hashedLastName": sha256_hex("doe")}} in c0["userIdentifiers"], "name hash")
    check(convs[1]["conversionDateTime"] == "2026-11-05 10:00:00-05:00", "offset preserved")
    check("gbraid" in convs[1], "gbraid key")
    check("userIdentifiers" not in convs[2], "bad phone dropped, row kept")
    check(len(out["skipped"]) == 4, "four skipped")
    reasons = [s["reason"] for s in out["skipped"]]
    check(any("duplicate" in r for r in reasons), "duplicate reason")
    check(any("identifier" in r for r in reasons), "no identifier reason")
    check(any("conversion_time" in r for r in reasons), "bad time reason")
    check(any("not an object" in r for r in reasons), "non-dict row reason")

    micros = build_google_click_conversions(
        [{"gclid": "g", "conversion_time": "2026-11-05T10:00:00Z", "value_micros": "2500000"}],
        "customers/1/conversionActions/2")
    check(micros["conversions"][0]["conversionValue"] == 2.5, "micros -> units")

    empty = build_google_click_conversions([], "customers/1/conversionActions/2")
    check(empty["conversions"] == [] and empty["skipped"] == [], "empty rows")
    try:
        build_google_click_conversions([], "")
    except ToolError:
        n += 1
    else:
        raise AssertionError("selftest failed: empty resource accepted")

    # -- openai capi ------------------------------------------------------
    ev = build_openai_capi_events([
        {"email": "Jane.Doe@gmail.com", "phone": "4155550100", "order_id": "A-1",
         "conversion_time": "2026-11-05T10:00:00Z", "value": 30, "currency": "EUR"},
        {"email": "bob@example.com", "conversion_time": "2026-11-05T10:00:00Z"},
        {"order_id": "A-9", "conversion_time": "2026-11-05T10:00:00Z"},  # no identifiers
    ], "pix_123")
    check(len(ev["events"]) == 2 and len(ev["skipped"]) == 1, "capi skip")
    e0 = ev["events"][0]
    check(e0["event_name"] == "lead_created", "default event name")
    check(e0["event_time"] == int(datetime(2026, 11, 5, 10, tzinfo=timezone.utc).timestamp()), "unix time")
    check(e0["event_id"] == "A-1", "order id as event id")
    check(e0["user"]["email_sha256"] == [sha256_hex("janedoe@gmail.com")], "capi email list")
    check(e0["user"]["phone_sha256"] == [sha256_hex("+14155550100")], "capi phone list")
    check(e0["custom_data"] == {"value": 30.0, "currency": "EUR"}, "capi custom data")
    e1 = ev["events"][1]
    check(re.fullmatch(r"[0-9a-f]{32}", e1["event_id"]) is not None, "derived event id")
    try:
        build_openai_capi_events([], "")
    except ToolError:
        n += 1
    else:
        raise AssertionError("selftest failed: empty pixel_id accepted")

    # -- redaction --------------------------------------------------------
    payload = {"conversions": [{"gclid": "g", "userIdentifiers": [{"hashedEmail": "a" * 64}]}],
               "events": [{"user": {"email_sha256": ["b" * 64]}}]}
    red = redact_report(payload)
    check(red["conversions"][0]["userIdentifiers"][0]["hashedEmail"] == "a" * 8 + "\u2026", "redact str")
    check(red["events"][0]["user"]["email_sha256"] == ["b" * 8 + "\u2026"], "redact list")
    check(red["conversions"][0]["gclid"] == "g", "non-hash untouched")
    check(payload["conversions"][0]["userIdentifiers"][0]["hashedEmail"] == "a" * 64, "no mutation")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
