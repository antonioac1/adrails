"""Change-history and drift detection helpers for AdRails.

This module turns Google Ads `change_event` rows into a small, JSON-safe
history, classifies each change as AdRails-originated or out-of-band, and
summarises risky edits such as budget, status, and bidding changes.

It performs no I/O itself: callers inject the HTTP/GAQL execution layer.
Safety properties: all user e-mail addresses are redacted, errors carry no
secrets, outputs are plain dicts/lists with JSON-safe values, and invalid
inputs raise ToolError before any external call is made.
"""

import re
from datetime import datetime

from adsops.errors import ToolError, RailViolation, scrub

# Google Ads change_event date filter must be 'YYYY-MM-DD HH:MM:SS'.
_SINCE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_RISKY_FIELD_NEEDLES = ("amount_micros", "status", "bidding", "target_cpa", "end_date")


def _g(row, *path, default=None):
    """Nested dict getter for Google Ads REST camelCase rows."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def redact_user(value) -> str:
    """Return a privacy-preserving form such as 'a…@example.com'."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "@" in text:
        local, domain = text.split("@", 1)
        first = local[:1] if local else ""
        return f"{first}…@{domain}"
    return f"{text[:1]}…"


def _safe_event(event):
    """Copy an event and strip/redact any user e-mail field."""
    ev = dict(event)
    raw_user = ev.get("user")
    if raw_user is None:
        raw_user = ev.get("user_email")
    if raw_user is None:
        raw_user = ev.get("userEmail")
    if raw_user is not None or "user" in ev:
        ev["user"] = redact_user(raw_user)
    # Avoid leaking raw e-mail keys even if callers passed unnormalised rows.
    ev.pop("user_email", None)
    ev.pop("userEmail", None)
    return ev


def _as_fields(value):
    """Normalise changed_fields, which may be repeated/list/string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    text = str(value).strip()
    return [text] if text else []


def _campaign_from_change(change):
    """Campaign may be a resource name string or a nested campaign object."""
    candidate = _g(change, "campaign", default="")
    if isinstance(candidate, dict):
        return str(
            candidate.get("name")
            or candidate.get("id")
            or candidate.get("resourceName")
            or ""
        )
    return "" if candidate is None else str(candidate)


def QUERY(since_iso_datetime, limit=200, until_iso_datetime=None) -> str:
    """Build a validated GAQL query for change_event history.

    Google rejects change_event queries without a FINITE date range (live-verified 2026-11-05:
    "missing filters ... or filtering with an infinite range"), so an upper bound is always sent:
    `until_iso_datetime` (same format) or, by default, 2099-12-31 23:59:59 (finite, accepted)."""
    until = (until_iso_datetime or "2099-12-31 23:59:59").strip()
    if not _SINCE_RE.fullmatch(until):
        raise ToolError("until must be in 'YYYY-MM-DD HH:MM:SS' format")
    if not isinstance(since_iso_datetime, str):
        raise ToolError("since must be a string in 'YYYY-MM-DD HH:MM:SS' format")
    since = since_iso_datetime.strip()
    if not _SINCE_RE.fullmatch(since):
        raise ToolError("since must be in 'YYYY-MM-DD HH:MM:SS' format")
    try:
        datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ToolError(f"invalid since datetime: {scrub(str(exc), 200)}") from None

    if isinstance(limit, bool):
        raise ToolError("limit must be an integer between 1 and 10000")
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        raise ToolError("limit must be an integer between 1 and 10000") from None
    if isinstance(limit, float) and not limit.is_integer():
        raise ToolError("limit must be an integer between 1 and 10000")
    if not 1 <= limit_int <= 10000:
        raise ToolError("limit must be between 1 and 10000")

    return (
        "SELECT change_event.change_date_time, change_event.change_resource_type, "
        "change_event.change_resource_name, change_event.client_type, "
        "change_event.user_email, change_event.resource_change_operation, "
        "change_event.changed_fields, change_event.campaign "
        "FROM change_event "
        f"WHERE change_event.change_date_time >= '{since}' "
        f"AND change_event.change_date_time <= '{until}' "
        "ORDER BY change_event.change_date_time DESC "
        f"LIMIT {limit_int}"
    )


def normalize(rows):
    """Normalise Google Ads change_event rows into a JSON-safe history list."""
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    try:
        iterator = iter(rows)
    except TypeError:
        raise ToolError("rows must be an iterable of change_event dicts") from None

    out = []
    for row in iterator:
        if not isinstance(row, dict):
            continue

        change = row.get("changeEvent")
        if not isinstance(change, dict):
            # Tolerate already-flat rows, but ignore clearly unrelated dicts.
            flat_keys = (
                "changeDateTime",
                "changeResourceType",
                "changeResourceName",
                "clientType",
                "userEmail",
                "resourceChangeOperation",
                "changedFields",
                "campaign",
            )
            change = row if any(key in row for key in flat_keys) else {}

        out.append(
            {
                "at": str(_g(change, "changeDateTime", default="") or ""),
                "resource_type": str(_g(change, "changeResourceType", default="") or ""),
                "resource": str(_g(change, "changeResourceName", default="") or ""),
                "client": str(_g(change, "clientType", default="") or ""),
                "user": redact_user(_g(change, "userEmail", default="")),
                "operation": str(_g(change, "resourceChangeOperation", default="") or ""),
                "fields": _as_fields(_g(change, "changedFields", default=None)),
                "campaign": _campaign_from_change(change),
            }
        )
    return out


def _user_matches(user, our_users) -> bool:
    """Match raw or already-redacted users against the configured allow-list."""
    if not our_users:
        return True
    if user in our_users:
        return True
    redacted = redact_user(user)
    for allowed in our_users:
        if redacted == allowed or redact_user(allowed) == redacted:
            return True
    return False


def classify(events, our_client_types=("GOOGLE_ADS_API",), our_users=()):
    """Tag each event as 'adrails' or 'out_of_band' without leaking e-mail."""
    if events is None:
        return []
    if isinstance(events, dict):
        events = [events]

    if isinstance(our_client_types, str):
        client_types = (our_client_types,)
    elif our_client_types is None:
        client_types = ()
    else:
        client_types = tuple(our_client_types)

    if isinstance(our_users, str):
        users = (our_users,)
    elif our_users is None:
        users = ()
    else:
        users = tuple(our_users)

    out = []
    for event in events:
        if not isinstance(event, dict):
            continue

        raw_user = event.get("user")
        if raw_user is None:
            raw_user = event.get("user_email")
        if raw_user is None:
            raw_user = event.get("userEmail")
        user_text = "" if raw_user is None else str(raw_user)

        client = str(
            event.get("client")
            or event.get("client_type")
            or event.get("clientType")
            or ""
        )
        source = (
            "adrails"
            if client in client_types and _user_matches(user_text, users)
            else "out_of_band"
        )

        safe = _safe_event(event)
        safe["source"] = source
        out.append(safe)
    return out


def drift_report(events):
    """Summarise out-of-band changes and return risky events safely."""
    if events is None:
        events = []
    if isinstance(events, dict):
        events = [events]

    out_of_band = 0
    by_client = {}
    risky = []

    for event in events:
        if not isinstance(event, dict):
            continue

        source = event.get("source")
        if source is None:
            # Conservative fallback for callers that pass normalised but
            # unclassified events.
            source = classify([event])[0].get("source", "out_of_band")
        if source != "out_of_band":
            continue

        out_of_band += 1
        client = str(
            event.get("client")
            or event.get("client_type")
            or event.get("clientType")
            or "UNKNOWN"
        )
        by_client[client] = by_client.get(client, 0) + 1

        fields = event.get("fields")
        if isinstance(fields, (list, tuple)):
            field_text = " ".join(str(item) for item in fields).lower()
        else:
            field_text = str(fields or "").lower()
        if any(needle in field_text for needle in _RISKY_FIELD_NEEDLES):
            risky.append(_safe_event(event))

    if out_of_band == 0:
        summary = "No out-of-band changes detected."
    else:
        summary = f"{out_of_band} out-of-band change(s) detected; {len(risky)} risky."

    return {
        "out_of_band": out_of_band,
        "by_client": by_client,
        "risky": risky,
        "summary": summary,
    }


def selftest() -> int:
    """Run deterministic, I/O-free checks. Returns the number of assertions."""
    n = 0

    def check(condition, message):
        nonlocal n
        assert condition, message
        n += 1

    def expect_tool_error(fn, message):
        nonlocal n
        try:
            fn()
        except ToolError:
            n += 1
            return
        raise AssertionError(message)

    # QUERY validation.
    query = QUERY("2024-01-01 00:00:00", 200)
    check(query.startswith("SELECT change_event.change_date_time"), "query select")
    check(
        "change_event.change_date_time >= '2024-01-01 00:00:00'" in query,
        "query since predicate",
    )
    check(query.endswith("LIMIT 200"), "query limit")
    check("LIMIT 10000" in QUERY("2024-01-01 00:00:00", 10000), "max limit")
    check("LIMIT 200" in QUERY("2024-01-01 00:00:00", "200"), "string limit")
    expect_tool_error(
        lambda: QUERY("2024-01-01 00:00:00", 0), "limit 0 should fail"
    )
    expect_tool_error(
        lambda: QUERY("2024-01-01 00:00:00", 10001), "limit 10001 should fail"
    )
    expect_tool_error(
        lambda: QUERY("2024-01-01", 10), "short since should fail"
    )
    expect_tool_error(
        lambda: QUERY("2024-13-01 00:00:00", 10), "invalid month should fail"
    )
    expect_tool_error(
        lambda: QUERY(None, 10), "non-string since should fail"
    )

    # Helper behaviour.
    check(_g({"a": {"b": 3}}, "a", "b") == 3, "nested getter")
    check(_g({"a": {}}, "a", "b", default=7) == 7, "nested getter default")
    check(_g({"a": {"b": 0}}, "a", "b") == 0, "nested getter zero")
    check(redact_user("alice@example.com") == "a…@example.com", "redact email")
    check(redact_user("bob") == "b…", "redact non-email")
    check(redact_user("") == "", "redact empty")

    # Normalisation.
    rows = [
        {
            "changeEvent": {
                "changeDateTime": "2024-01-02T03:04:05Z",
                "changeResourceType": "CAMPAIGN",
                "changeResourceName": "customers/1/campaigns/2",
                "clientType": "GOOGLE_ADS_API",
                "userEmail": "alice@example.com",
                "resourceChangeOperation": "UPDATE",
                "changedFields": "status, amount_micros",
                "campaign": {"id": "2", "name": "Spring"},
            }
        },
        {
            "changeEvent": {
                "changeDateTime": "2024-01-03T00:00:00Z",
                "changeResourceType": "AD_GROUP",
                "changeResourceName": "customers/1/adGroups/3",
                "clientType": "GOOGLE_ADS_UI",
                "userEmail": "bob@example.com",
                "resourceChangeOperation": "CREATE",
                "changedFields": ["status"],
                "campaign": "customers/1/campaigns/2",
            }
        },
        "malformed",
    ]
    norm = normalize(rows)
    check(len(norm) == 2, "normalize length")
    check(norm[0]["at"] == "2024-01-02T03:04:05Z", "normalize at")
    check(norm[0]["user"] == "a…@example.com", "normalize redacts user")
    check(norm[0]["fields"] == ["status", "amount_micros"], "normalize fields")
    check(norm[0]["campaign"] == "Spring", "normalize campaign object")
    check(norm[1]["fields"] == ["status"], "normalize list fields")
    check(normalize([]) == [], "normalize empty")
    check(normalize(None) == [], "normalize None")
    check(normalize([{"changeEvent": {}}])[0]["fields"] == [], "normalize missing fields")

    # Classification.
    classified = classify(norm)
    check(classified[0]["source"] == "adrails", "API classified adrails")
    check(classified[1]["source"] == "out_of_band", "UI classified out-of-band")
    check(classify([]) == [], "classify empty")
    allowed = classify(
        [{"client": "GOOGLE_ADS_API", "user": "bob@example.com"}],
        our_users=("bob@example.com",),
    )[0]
    check(allowed["source"] == "adrails", "allowed user")
    check(allowed["user"] == "b…@example.com", "classify redacts user")
    check("userEmail" not in allowed, "classify drops raw userEmail")
    denied = classify(
        [{"client": "GOOGLE_ADS_API", "user": "alice@example.com"}],
        our_users=("bob@example.com",),
    )[0]
    check(denied["source"] == "out_of_band", "disallowed user")
    check(
        classify([{"client_type": "GOOGLE_ADS_AUTOMATED_RULE"}])[0]["source"]
        == "out_of_band",
        "automated rule is out-of-band",
    )
    check(
        classify([{"client": "CUSTOM"}], our_client_types="CUSTOM")[0]["source"]
        == "adrails",
        "string client types accepted",
    )

    # Drift reporting.
    events = [
        {"source": "out_of_band", "client": "UI", "fields": ["status"]},
        {"source": "out_of_band", "client": "UI", "fields": ["name"]},
        {
            "source": "out_of_band",
            "client": "OTHER",
            "fields": ["campaign_budget.amount_micros"],
        },
        {"source": "adrails", "client": "API", "fields": ["status"]},
    ]
    report = drift_report(events)
    check(report["out_of_band"] == 3, "drift out-of-band count")
    check(report["by_client"] == {"UI": 2, "OTHER": 1}, "drift by client")
    check(len(report["risky"]) == 2, "drift risky count")
    check(
        report["summary"].endswith(".") and "3" in report["summary"],
        "drift summary",
    )
    empty = drift_report([])
    check(empty["out_of_band"] == 0, "empty drift count")
    check(empty["by_client"] == {}, "empty drift by client")
    check(empty["risky"] == [], "empty drift risky")
    check(
        empty["summary"] == "No out-of-band changes detected.",
        "empty drift summary",
    )
    check(drift_report(None)["out_of_band"] == 0, "None drift")
    fallback = drift_report([{"client": "UI", "fields": ["status"]}])
    check(fallback["out_of_band"] == 1 and len(fallback["risky"]) == 1, "fallback classify")

    return n


if __name__ == "__main__":
    import sys

    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
