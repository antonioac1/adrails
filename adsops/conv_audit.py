"""Read-only Google Ads conversion-tracking audit for AdRails.

Takes raw GAQL rows (conversion actions, action stats, campaigns, customer) and returns a
graded report: 0-100 score, letter grade, severity-sorted findings with fixes, and
normalised action rows carrying in-window conversions. Pure data in / data out: no
network, files or account mutation. Tools should render QUERIES with {since}/{until},
fetch rows through their injectable HTTP callable, then call audit(). Refusals raise
RailViolation, bad input raises ToolError, and numeric fields are coerced defensively.
"""

from __future__ import annotations

try:  # sibling package module; fallback keeps this file runnable standalone
    from adsops.errors import ToolError, RailViolation, scrub  # noqa: F401
except Exception:  # pragma: no cover
    class ToolError(Exception):
        """User-visible tool failure."""

    class RailViolation(Exception):
        """A safety rail refused the operation."""

    def scrub(text, limit=1500):  # type: ignore[misc]
        return str(text)[:limit]


QUERIES = {
    "actions": (
        "SELECT conversion_action.id, conversion_action.name, conversion_action.status, "
        "conversion_action.type, conversion_action.category, conversion_action.primary_for_goal, "
        "conversion_action.counting_type, conversion_action.click_through_lookback_window_days, "
        "conversion_action.include_in_conversions_metric, conversion_action.value_settings.default_value, "
        "conversion_action.tag_snippets "
        "FROM conversion_action WHERE conversion_action.status != 'REMOVED'"
    ),
    # Per-action stats. FROM customer (live-verified 2026-11-05; FROM conversion_action with
    # segments.conversion_action is rejected by the API with INVALID_ARGUMENT).
    "action_stats": (
        "SELECT segments.conversion_action, metrics.all_conversions, metrics.conversions "
        "FROM customer WHERE segments.date BETWEEN '{since}' AND '{until}'"
    ),
    "campaigns": (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type, "
        "metrics.cost_micros, metrics.clicks, metrics.conversions "
        "FROM campaign WHERE segments.date BETWEEN '{since}' AND '{until}' AND campaign.status = 'ENABLED'"
    ),
    "customer": (
        "SELECT customer.id, customer.auto_tagging_enabled, "
        "customer.conversion_tracking_setting.conversion_tracking_status, "
        "customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled FROM customer"
    ),
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PENALTY = {"critical": 30, "high": 15, "medium": 7, "low": 3}

# Lead-ish categories where every-click counting inflates conversion counts.
LEAD_CATEGORIES = frozenset({
    "LEAD", "SUBMIT_LEAD_FORM", "CONTACT", "PHONE_CALL_LEAD",
    "BOOK_APPOINTMENT", "SIGNUP", "QUALIFIED_LEAD",
})
SMART_BIDDING_TYPES = frozenset({
    "MAXIMIZE_CONVERSIONS", "TARGET_CPA", "MAXIMIZE_CONVERSION_VALUE", "TARGET_ROAS",
})
BROKEN_TRACKING = frozenset({"", "NOT_CONVERSION_TRACKED", "UNSPECIFIED", "UNKNOWN"})

MIN_LOOKBACK_DAYS = 30
MIN_WASTE_COST = 50.0        # currency units burned with zero conversions
MIN_SMART_BIDDING_CONV = 15.0
MAX_WINDOW_DAYS = 730


# --------------------------------------------------------------------------- helpers
def _camel_to_snake(name):
    """Convert lowerCamelCase to snake_case, preserving already-snake keys."""
    out = []
    for i, ch in enumerate(name):
        if ch.isupper():
            if i > 0 and (name[i - 1].islower() or (i + 1 < len(name) and name[i + 1].islower())):
                out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _snake_to_camel(name):
    """Convert snake_case to lowerCamelCase, preserving already-camel keys."""
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _g(row, *path, default=None):
    """Nested get tolerant of camelCase/snake_case GAQL row field aliases."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            if key in cur:
                cur = cur[key]
            else:
                alt = None
                if isinstance(key, str):
                    alt = _snake_to_camel(key) if "_" in key else _camel_to_snake(key)
                if alt is not None and alt != key and alt in cur:
                    cur = cur[alt]
                else:
                    return default
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
    return default if cur is None else cur


def _num(value, cast=float, default=None):
    """Coerce GAQL numerics that arrive as strings; missing/garbage -> default (0)."""
    if default is None:
        default = cast(0)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _bool(value):
    """Tri-state boolean: True/False/None (None = not present, so don't flag it)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _money(micros):
    """Google Ads micros -> exact currency units (threshold comparisons must not pre-round)."""
    return _num(micros, float, 0.0) / 1_000_000.0


def _id_from_resource(value):
    """'customers/1/conversionActions/101' -> '101' (also accepts a resourceName dict)."""
    if isinstance(value, dict):
        value = _g(value, "resourceName", default="")
    text = str(value or "").strip().rstrip("/")
    return text.split("/")[-1] if text else ""


def _rows(value):
    """Accept a single row, a list of rows, or nothing."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return [r for r in value]
    raise ToolError("expected a Google Ads row or a list of rows")


def _norm_name(name):
    """Lowercase, strip punctuation and collapse whitespace (for duplicate detection)."""
    text = str(name or "").strip().lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def _finding(fid, severity, title, detail, fix):
    return {"id": fid, "severity": severity, "title": title, "detail": detail, "fix": fix}


def _validate_days(days):
    if isinstance(days, bool):
        raise ToolError("days must be an integer between 1 and 730")
    try:
        window = int(days)
    except (TypeError, ValueError):
        raise ToolError("days must be an integer between 1 and 730") from None
    if window < 1:
        raise ToolError("days must be >= 1")
    if window > MAX_WINDOW_DAYS:
        raise RailViolation(f"refusing to audit more than {MAX_WINDOW_DAYS} days of data")
    return window


def _stats_by_action(rows):
    """Sum all_conversions (fallback conversions) per conversion action id."""
    totals = {}
    for row in _rows(rows):
        res = _g(row, "segments", "conversionAction")
        if res is None:
            res = _g(row, "conversionAction", "resourceName")
        aid = _id_from_resource(res)
        if not aid:
            continue
        raw = _g(row, "metrics", "allConversions")
        if raw is None:
            raw = _g(row, "metrics", "conversions", default=0)
        totals[aid] = totals.get(aid, 0.0) + _num(raw, float, 0.0)
    return totals


def _normalise_actions(rows, stats):
    out = []
    for row in _rows(rows):
        ca = _g(row, "conversionAction")
        if not isinstance(ca, dict):
            ca = row if isinstance(row, dict) else {}
        aid = str(_g(ca, "id", default="") or "")
        include = _bool(_g(ca, "includeInConversionsMetric"))
        out.append({
            "id": aid,
            "name": str(_g(ca, "name", default="") or ""),
            "status": str(_g(ca, "status", default="") or "").upper(),
            "type": str(_g(ca, "type", default="") or "").upper(),
            "category": str(_g(ca, "category", default="") or "").upper(),
            "primary_for_goal": _bool(_g(ca, "primaryForGoal")) is True,
            "counting_type": str(_g(ca, "countingType", default="") or "").upper(),
            "lookback_days": _num(_g(ca, "clickThroughLookbackWindowDays"), int, 0),
            "include_in_conversions": True if include is None else include,
            "default_value": round(_num(_g(ca, "valueSettings", "defaultValue"), float, 0.0), 2),
            "all_conversions": round(stats.get(aid, 0.0), 2),
        })
    return out


def _normalise_campaigns(rows):
    out = []
    for row in _rows(rows):
        camp = _g(row, "campaign", default=row)
        if not isinstance(camp, dict):
            camp = {}
        metrics = _g(row, "metrics", default={}) or {}
        out.append({
            "id": str(_g(camp, "id", default="") or ""),
            "name": str(_g(camp, "name", default="") or ""),
            "status": str(_g(camp, "status", default="") or "").upper(),
            "bidding_strategy_type": str(_g(camp, "biddingStrategyType", default="") or "").upper(),
            "cost": _money(_g(metrics, "costMicros", default=0)),
            "clicks": _num(_g(metrics, "clicks"), int, 0),
            "conversions": round(_num(_g(metrics, "conversions"), float, 0.0), 2),
        })
    return out


def _customer_row(customer):
    rows = _rows(customer)
    if not rows or not isinstance(rows[0], dict):
        return {}
    inner = rows[0].get("customer")
    return inner if isinstance(inner, dict) else rows[0]


def _grade(score):
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


# ----------------------------------------------------------------------------- audit
def audit(actions, action_stats, campaigns, customer, days=30) -> dict:
    """Audit conversion tracking from raw GAQL rows and return a graded report."""
    window = _validate_days(days)

    stats = _stats_by_action(action_stats)
    norm_actions = _normalise_actions(actions, stats)
    norm_campaigns = _normalise_campaigns(campaigns)
    cust = _customer_row(customer)

    findings = []
    enabled_actions = [a for a in norm_actions if a["status"] == "ENABLED"]
    primary = [a for a in enabled_actions if a["primary_for_goal"]]
    # The campaigns query already filters ENABLED; treat a missing status as enabled.
    enabled_campaigns = [c for c in norm_campaigns if c["status"] in ("", "ENABLED")]

    if not primary:
        findings.append(_finding(
            "no_primary_action", "critical", "No primary conversion action",
            f"{len(enabled_actions)} enabled conversion action(s) found, none flagged primary_for_goal.",
            "In Google Ads > Goals > Conversions mark exactly one meaningful action as Primary for goal."))

    if _bool(_g(cust, "autoTaggingEnabled")) is False:
        findings.append(_finding(
            "auto_tagging_off", "high", "Auto-tagging disabled",
            "customer.autoTaggingEnabled is false, so GCLID-based attribution is incomplete.",
            "Enable auto-tagging in Google Ads > Admin > Account settings."))

    tracking_status = str(_g(cust, "conversionTrackingSetting", "conversionTrackingStatus",
                             default="") or "").strip().upper()
    if tracking_status in BROKEN_TRACKING:
        findings.append(_finding(
            "tracking_not_set", "critical", "Conversion tracking not active",
            f"conversion_tracking_status is {tracking_status or 'missing'}.",
            "Verify the Google tag / conversion linker fires site-wide and that test conversions record."))

    dead = [a for a in primary if a["all_conversions"] <= 0]
    if dead:
        names = ", ".join(a["name"] or a["id"] for a in dead[:5])
        findings.append(_finding(
            "dead_action", "high", "Primary action recorded no conversions",
            f"No conversions in the last {window} days for primary action(s): {names}.",
            "Confirm the tag fires, then demote or replace the dead action as primary."))

    # Duplicate primaries: same normalised name, or two primaries sharing a category.
    name_map, cat_map, pairs, seen = {}, {}, [], set()
    for action in primary:
        key = _norm_name(action["name"])
        if key:
            other = name_map.setdefault(key, action)
            if other is not action and (other["id"], action["id"]) not in seen:
                seen.add((other["id"], action["id"]))
                pairs.append(f"'{other['name']}' and '{action['name']}' share a normalised name")
        cat = action["category"]
        if cat:
            other = cat_map.setdefault(cat, action)
            if other is not action and (other["id"], action["id"]) not in seen:
                seen.add((other["id"], action["id"]))
                pairs.append(f"'{other['name']}' and '{action['name']}' both count category {cat}")
    if pairs:
        findings.append(_finding(
            "duplicate_names", "medium", "Duplicate primary conversion actions",
            "; ".join(pairs) + ".",
            "Keep one primary action per goal so conversions are not double counted."))

    many = [a for a in primary
            if a["category"] in LEAD_CATEGORIES and a["counting_type"] == "MANY_PER_CLICK"]
    if many:
        findings.append(_finding(
            "counting_many_per_click_for_leads", "medium", "Lead actions count every click",
            "MANY_PER_CLICK counting on lead action(s): "
            + ", ".join(a["name"] or a["id"] for a in many[:5]) + ".",
            "Switch lead actions to ONE_PER_CLICK unless a single lead can legitimately repeat."))

    short = [a for a in primary
             if a["category"] in LEAD_CATEGORIES and a["lookback_days"] < MIN_LOOKBACK_DAYS]
    if short:
        findings.append(_finding(
            "short_lookback", "low", "Click lookback window too short for leads",
            "Lead action(s) with lookback < 30 days: "
            + ", ".join(f"{a['name'] or a['id']} ({a['lookback_days']}d)" for a in short[:5]) + ".",
            "Extend the click-through lookback to 30+ days so delayed leads are attributed."))

    no_value = [a for a in primary if a["default_value"] <= 0]
    if no_value:
        findings.append(_finding(
            "no_value", "low", "Primary actions have no conversion value",
            "No default value set on: " + ", ".join(a["name"] or a["id"] for a in no_value[:5]) + ".",
            "Assign a defensible default value so value-based bidding has a signal."))

    waste = [c for c in enabled_campaigns
             if c["cost"] >= MIN_WASTE_COST and c["conversions"] <= 0]
    if waste:
        findings.append(_finding(
            "spend_no_conversions", "high", "Spend with zero conversions",
            f"Enabled campaign(s) with cost >= {MIN_WASTE_COST:.0f} and no conversions in the last "
            f"{window} days: " + ", ".join(f"{c['name'] or c['id']} ({c['cost']:.2f})" for c in waste[:5]) + ".",
            "Pause or fix tracking/pacing on these campaigns before continuing to spend."))

    smart = [c for c in enabled_campaigns if c["bidding_strategy_type"] in SMART_BIDDING_TYPES]
    total_conv = round(sum(c["conversions"] for c in enabled_campaigns), 2)
    if smart and total_conv < MIN_SMART_BIDDING_CONV:
        findings.append(_finding(
            "smart_bidding_without_conversions", "critical", "Smart bidding starved of conversions",
            f"Smart bidding used by {len(smart)} campaign(s) but only {total_conv} conversion(s) "
            f"in the last {window} days (need >= {MIN_SMART_BIDDING_CONV:.0f}).",
            "Move to manual/max-clicks bidding until conversion volume is sufficient, or widen the window."))

    if _bool(_g(cust, "conversionTrackingSetting", "enhancedConversionsForLeadsEnabled")) is False:
        findings.append(_finding(
            "enhanced_conversions_leads_off", "low", "Enhanced conversions for leads disabled",
            "conversion_tracking_setting.enhanced_conversions_for_leads_enabled is false.",
            "Enable enhanced conversions for leads to recover more click-to-lead matches."))

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["id"]))
    score = max(0, 100 - sum(PENALTY[f["severity"]] for f in findings))
    grade = _grade(score)

    if findings:
        counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITY_ORDER}
        parts = [f"{counts[s]} {s}" for s in ("critical", "high", "medium", "low") if counts[s]]
        summary = (f"{len(findings)} issue(s) found ({', '.join(parts)}) across "
                   f"{len(norm_actions)} conversion action(s) and {len(enabled_campaigns)} enabled "
                   f"campaign(s) over the last {window} days.")
    else:
        summary = (f"Conversion tracking looks healthy over the last {window} days: "
                   f"{len(norm_actions)} action(s), {len(enabled_campaigns)} enabled campaign(s), "
                   "no issues found.")

    return {"score": score, "grade": grade, "findings": findings,
            "actions": norm_actions, "summary": summary}


# -------------------------------------------------------------------------- selftest
def selftest() -> int:
    """Mocked, no-I/O checks for the audit. Returns the number of assertions run."""
    import json

    n = 0

    def check(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(f"check {n} failed: {msg}")

    def make():
        actions = [
            {"conversionAction": {
                "id": "101", "name": "Purchase", "status": "ENABLED", "type": "WEBPAGE",
                "category": "PURCHASE", "primaryForGoal": True, "countingType": "ONE_PER_CLICK",
                "clickThroughLookbackWindowDays": "30", "includeInConversionsMetric": True,
                "valueSettings": {"defaultValue": 25.0}}},
            {"conversionAction": {
                "id": "202", "name": "Lead Form", "status": "ENABLED", "type": "WEBPAGE",
                "category": "SUBMIT_LEAD_FORM", "primaryForGoal": False,
                "countingType": "ONE_PER_CLICK", "clickThroughLookbackWindowDays": 30,
                "includeInConversionsMetric": True, "valueSettings": {"defaultValue": 5}}},
            {"conversionAction": {"id": "303", "name": "Old", "status": "REMOVED",
                                  "category": "PURCHASE", "primaryForGoal": False}},
        ]
        stats = [
            {"segments": {"conversionAction": "customers/1/conversionActions/101"},
             "metrics": {"allConversions": "12"}},
            {"segments": {"conversionAction": "customers/1/conversionActions/101"},
             "metrics": {"allConversions": 3}},
            {"segments": {"conversionAction": "customers/1/conversionActions/202"},
             "metrics": {"conversions": 2}},
        ]
        campaigns = [
            {"campaign": {"id": "1", "name": "Brand", "status": "ENABLED",
                          "biddingStrategyType": "TARGET_CPA"},
             "metrics": {"costMicros": "120000000", "clicks": "40", "conversions": 20.0}},
            {"campaign": {"id": "2", "name": "Search", "status": "ENABLED",
                          "biddingStrategyType": "MAXIMIZE_CONVERSIONS"},
             "metrics": {"costMicros": "30000000", "clicks": "10", "conversions": "4"}},
        ]
        customer = {"customer": {
            "id": "1", "autoTaggingEnabled": True,
            "conversionTrackingSetting": {
                "conversionTrackingStatus": "CONVERSION_TRACKING_MANAGED_BY_THIS_ACCOUNT",
                "enhancedConversionsForLeadsEnabled": True}}}
        return actions, stats, campaigns, customer

    def ids(res):
        return [f["id"] for f in res["findings"]]

    # --- primitive helpers
    check(_g({"a": {"b": "7"}}, "a", "b") == "7", "nested get")
    check(_g({"a": 1}, "a", "missing", default="d") == "d", "missing default")
    check(_num("42") == 42.0, "numeric string")
    check(_num(None) == 0 and _num("oops") == 0, "missing/garbage -> 0")
    check(_money("2500000") == 2.5, "micros -> units")
    check(_id_from_resource("customers/1/conversionActions/101") == "101", "resource id")
    check(_bool("FALSE") is False and _bool("true") is True and _bool(None) is None, "bool tri-state")

    # --- healthy account
    a, s, c, cu = make()
    res = audit(a, s, c, cu)
    check(res["score"] == 100 and res["grade"] == "A", f"healthy score {res['score']} {ids(res)}")
    check(res["findings"] == [], "healthy has no findings")
    check(len(res["actions"]) == 3, "all rows normalised")
    check([x for x in res["actions"] if x["id"] == "101"][0]["all_conversions"] == 15.0, "stats summed")
    check(isinstance(json.dumps(res), str), "output JSON-serialisable")
    check(res["summary"].endswith(".") and "healthy" in res["summary"], "summary sentence")

    # --- empty input
    empty = audit([], [], [], {})
    check(empty["score"] == 40 and empty["grade"] == "D", f"empty score {empty['score']}")
    check(set(ids(empty)) == {"no_primary_action", "tracking_not_set"}, f"empty ids {ids(empty)}")
    check(empty["actions"] == [], "empty actions")

    # --- dead primary action
    a, s, c, cu = make()
    s.clear()
    res = audit(a, s, c, cu)
    check(ids(res) == ["dead_action"] and res["score"] == 85, f"dead {ids(res)} {res['score']}")

    # --- no primary action
    a, s, c, cu = make()
    for row in a:
        row["conversionAction"]["primaryForGoal"] = False
    res = audit(a, s, c, cu)
    check(ids(res) == ["no_primary_action"] and res["score"] == 70, "no-primary flagged")

    # --- tracking not set / auto-tagging off
    a, s, c, cu = make()
    cu["customer"]["conversionTrackingSetting"]["conversionTrackingStatus"] = "NOT_CONVERSION_TRACKED"
    check(ids(audit(a, s, c, cu)) == ["tracking_not_set"], "tracking not set")
    a, s, c, cu = make()
    cu["customer"]["autoTaggingEnabled"] = "false"
    res = audit(a, s, c, cu)
    check(ids(res) == ["auto_tagging_off"] and res["score"] == 85, "auto tagging off")

    # --- duplicate primaries (same name, case/space-insensitive)
    a, s, c, cu = make()
    a.append({"conversionAction": {"id": "404", "name": " purchase ", "status": "ENABLED",
                                   "category": "PURCHASE", "primaryForGoal": True,
                                   "countingType": "ONE_PER_CLICK",
                                   "clickThroughLookbackWindowDays": 30,
                                   "valueSettings": {"defaultValue": 10}}})
    s.append({"segments": {"conversionAction": "customers/1/conversionActions/404"},
              "metrics": {"allConversions": 2}})
    res = audit(a, s, c, cu)
    check(ids(res) == ["duplicate_names"] and res["score"] == 93, f"dupes {ids(res)}")
    check(len([f for f in res["findings"] if f["id"] == "duplicate_names"]) == 1, "one dupe finding")

    # --- lead category pitfalls
    a, s, c, cu = make()
    a[0]["conversionAction"]["category"] = "LEAD"
    a[0]["conversionAction"]["countingType"] = "MANY_PER_CLICK"
    res = audit(a, s, c, cu)
    check(ids(res) == ["counting_many_per_click_for_leads"] and res["score"] == 93, "many-per-click")
    a[0]["conversionAction"]["countingType"] = "ONE_PER_CLICK"
    a[0]["conversionAction"]["clickThroughLookbackWindowDays"] = "10"
    res = audit(a, s, c, cu)
    check(ids(res) == ["short_lookback"] and res["score"] == 97, "short lookback")

    # --- missing / zero value
    a, s, c, cu = make()
    a[0]["conversionAction"]["valueSettings"]["defaultValue"] = "0"
    check(ids(audit(a, s, c, cu)) == ["no_value"], "zero value")
    del a[0]["conversionAction"]["valueSettings"]
    res = audit(a, s, c, cu)
    check(ids(res) == ["no_value"] and res["score"] == 97, "missing value")

    # --- spend without conversions
    a, s, c, cu = make()
    c[1]["metrics"]["conversions"] = "0"
    c[1]["metrics"]["costMicros"] = "80000000"
    res = audit(a, s, c, cu)
    check(ids(res) == ["spend_no_conversions"] and res["score"] == 85, "wasted spend")

    # --- smart bidding starved of conversions
    a, s, c, cu = make()
    c[0]["metrics"]["conversions"] = 3
    c[1]["metrics"]["conversions"] = "1"
    res = audit(a, s, c, cu)
    check(ids(res) == ["smart_bidding_without_conversions"] and res["score"] == 70, "smart bidding")

    # --- enhanced conversions for leads off
    a, s, c, cu = make()
    cu["customer"]["conversionTrackingSetting"]["enhancedConversionsForLeadsEnabled"] = False
    check(ids(audit(a, s, c, cu)) == ["enhanced_conversions_leads_off"], "EC for leads off")

    # --- malformed rows must not crash and must not invent findings
    a, s, c, cu = make()
    c.append({"campaign": {"id": "9", "name": "Junk"},
              "metrics": {"costMicros": "abc", "clicks": None, "conversions": "x"}})
    c.append({"metrics": {"costMicros": "1000000"}})
    s.append({"segments": {}, "metrics": {}})
    res = audit(a, s, c, cu)
    check(res["score"] == 100, f"malformed tolerated {ids(res)}")

    # --- severity ordering and combined penalties
    a, s, c, cu = make()
    s.clear()
    cu["customer"]["autoTaggingEnabled"] = False
    res = audit(a, s, c, cu)
    order = [SEVERITY_ORDER[f["severity"]] for f in res["findings"]]
    check(order == sorted(order) and len(order) == 2 and res["score"] == 70, "sorted + combined")

    # --- input validation / rails
    a, s, c, cu = make()
    raised = None
    try:
        audit(a, s, c, cu, days=0)
    except ToolError:
        raised = "tool"
    check(raised == "tool", "days=0 rejected")
    raised = None
    try:
        audit(a, s, c, cu, days="abc")
    except ToolError:
        raised = "tool"
    check(raised == "tool", "non-numeric days rejected")
    raised = None
    try:
        audit(a, s, c, cu, days=5000)
    except RailViolation:
        raised = "rail"
    except ToolError:
        raised = "tool"
    check(raised == "rail", "oversized window refused by rail")
    check(audit(a, s, c, cu, days="90")["score"] == 100, "string days accepted")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)