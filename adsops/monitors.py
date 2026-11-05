"""Monitors, alerts and scheduled briefs for AdRails (evaluation and formatting only).

Turns a point-in-time ad-account ``snapshot`` into compact, JSON-serialisable alerts
(CPA spikes, pacing, month forecast, zero impressions, disapprovals, budget-limited,
stale conversions), suppresses repeats via a caller-persisted state dict, and renders
a short markdown brief.

Safety: pure computation — no network, files or subprocess here. Delivery is delegated
to an injected ``notify(text)``; webhook URLs must be HTTPS; error text is ``scrub``-ed.

A tool should: ``validate`` configs on save; run ``evaluate`` + ``dedupe`` on a schedule
(persisting the returned state); then ``brief`` + ``deliver`` via the configured notifier.
"""

import calendar
import json
import math
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from adsops.errors import RailViolation, ToolError, scrub

# Default thresholds (ratios, except no_conversions_days which is a day count).
DEFAULTS = {
    "cpa_spike": 0.5,
    "pacing_over": 1.5,
    "pacing_under": 0.3,
    "month_forecast": 0.1,
    "zero_impressions": 0.0,
    "disapprovals": 0.0,
    "budget_limited": 0.2,
    "no_conversions_days": 7.0,
}
SEVERITIES = ("info", "warn", "crit")
_RANK = {"info": 1, "warn": 2, "crit": 3}
BRIEF_LIMIT = 3500

# Fallback for nested Google-Ads-style rows, e.g. {"metrics": {"costMicros": "2500000"}}.
_METRIC_FALLBACK = {
    "spend_today": ("costMicros", 1e6),
    "spend_month": ("costMicros", 1e6),
    "clicks_today": ("clicks", 1.0),
    "conv_today": ("conversions", 1.0),
    "impressions_today": ("impressions", 1.0),
}


def _g(row, *path, default=None):
    """Nested get: _g(row, "a", "b"); None values count as missing."""
    cur = row
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return default
    return default if cur is None else cur


def _num(value, default=0.0):
    """Defensive float coercion: strings ok, missing/NaN/inf -> default."""
    if value is None or value == "":
        return float(default)
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def _r2(value):
    return round(_num(value, 0.0), 2)


def _cnum(camp, key, default=0.0):
    """Campaign field as float, with nested-metrics fallback and micros conversion."""
    val = _g(camp, key)
    if val is None and key in _METRIC_FALLBACK:
        name, div = _METRIC_FALLBACK[key]
        raw = _g(camp, "metrics", name)
        if raw is not None:
            val = _num(raw) / div
    return _num(val, default)


def _enabled(camp):
    return str(_g(camp, "status", default="")).strip().upper() == "ENABLED"


def _hour(camp, snap):
    """Local hour from the campaign, else the snapshot, else -1 (unknown => never fires)."""
    raw = _g(camp, "hour")
    if raw is None:
        raw = _g(snap, "hour")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return -1


def _day_bounds(date_str):
    """(day_of_month, days_in_month) from an ISO date; (0, 0) when unparseable."""
    try:
        d = date.fromisoformat(str(date_str or "")[:10])
    except (TypeError, ValueError):
        return 0, 0
    return d.day, calendar.monthrange(d.year, d.month)[1]


def _matches(mon, camp):
    target = mon.get("campaign")
    if target is None or target == "":
        return True
    t = str(target).strip().lower()
    return t in (str(_g(camp, "key", default="")).strip().lower(),
                 str(_g(camp, "name", default="")).strip().lower())


def _alert(mid, camp, severity, title, detail, value, threshold):
    return {
        "monitor": mid,
        "campaign": str(_g(camp, "key") or _g(camp, "name") or ""),
        "severity": severity if severity in SEVERITIES else "warn",
        "title": str(title),
        "detail": str(detail),
        "value": _r2(value),
        "threshold": _r2(threshold),
    }


def validate(monitors):
    """Return a list of human-readable config errors (empty list == valid)."""
    if not isinstance(monitors, list):
        return ["monitors must be a list"]
    errors, seen = [], set()
    for i, m in enumerate(monitors):
        tag = f"[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{tag} monitor must be an object")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            errors.append(f"{tag} id must be a non-empty string")
        elif mid in seen:
            errors.append(f"{tag} duplicate id {mid!r}")
        else:
            seen.add(mid)
        if m.get("type") not in DEFAULTS:
            errors.append(f"{tag} unknown type {m.get('type')!r}")
        thr = m.get("threshold")
        if thr is not None:
            try:
                val = float(thr)
            except (TypeError, ValueError):
                errors.append(f"{tag} threshold must be a number")
            else:
                if not math.isfinite(val) or val < 0:
                    errors.append(f"{tag} threshold must be a finite number >= 0")
        if m.get("severity") is not None and m.get("severity") not in SEVERITIES:
            errors.append(f"{tag} severity must be one of {SEVERITIES}")
        if "enabled" in m and not isinstance(m["enabled"], bool):
            errors.append(f"{tag} enabled must be a boolean")
        camp = m.get("campaign")
        if camp is not None and not isinstance(camp, (str, int)):
            errors.append(f"{tag} campaign must be a string")
    return errors


def _check(mtype, mid, sev, thr, camp, snap, dom, dim):
    """Evaluate one monitor type against one campaign; returns [] or [alert]."""
    name = str(_g(camp, "name") or _g(camp, "key") or "campaign")
    enabled = _enabled(camp)
    if mtype == "cpa_spike":
        c7, c30 = _cnum(camp, "cpa_7d"), _cnum(camp, "cpa_30d")
        if c30 > 0 and c7 > c30 * (1 + thr):
            return [_alert(mid, camp, sev, f"CPA spike: {name}",
                           f"7d CPA {c7:.2f} vs 30d {c30:.2f}", c7, c30 * (1 + thr))]
    elif mtype == "pacing_over":
        budget, spend = _cnum(camp, "daily_budget"), _cnum(camp, "spend_today")
        if budget > 0 and spend > budget * thr:
            ratio = spend / budget
            # Spec: >= 2x daily budget always escalates to crit, regardless of config severity.
            return [_alert(mid, camp, "crit" if ratio >= 2.0 else sev,
                           f"Overspending: {name}",
                           f"Spent {spend:.2f} of {budget:.2f} daily budget ({ratio:.2f}x)",
                           spend, budget * thr)]
    elif mtype == "pacing_under":
        budget, spend = _cnum(camp, "daily_budget"), _cnum(camp, "spend_today")
        hour = _hour(camp, snap)
        if enabled and hour >= 15 and budget > 0 and spend < budget * thr:
            return [_alert(mid, camp, sev, f"Underspending: {name}",
                           f"Spent {spend:.2f} of {budget:.2f} by hour {hour}",
                           spend, budget * thr)]
    elif mtype == "month_forecast":
        mbudget, smonth = _cnum(camp, "month_budget"), _cnum(camp, "spend_month")
        if mbudget > 0 and dom > 0 and dim > 0:
            forecast = smonth / dom * dim
            if forecast > mbudget * (1 + thr):
                return [_alert(mid, camp, sev, f"Budget forecast overrun: {name}",
                               f"Forecast {forecast:.2f} vs month budget {mbudget:.2f}",
                               forecast, mbudget * (1 + thr))]
    elif mtype == "zero_impressions":
        hour = _hour(camp, snap)
        if enabled and hour >= 12 and int(_cnum(camp, "impressions_today")) == 0:
            return [_alert(mid, camp, sev, f"Zero impressions: {name}",
                           f"No impressions by hour {hour}", 0.0, 1.0)]
    elif mtype == "disapprovals":
        count = int(_cnum(camp, "disapproved_ads"))
        if count > 0:
            return [_alert(mid, camp, sev, f"Disapproved ads: {name}",
                           f"{count} disapproved ad(s)", float(count), 0.0)]
    elif mtype == "budget_limited":
        lost = _cnum(camp, "lost_budget_is")
        if lost >= thr:
            return [_alert(mid, camp, sev, f"Budget limited: {name}",
                           f"Lost budget (IS) {lost:.2f}", lost, thr)]
    elif mtype == "no_conversions_days":
        raw = _g(camp, "days_since_conv")
        if raw is not None:
            days = _num(raw, -1.0)
            if 0 <= days and days >= thr:
                return [_alert(mid, camp, sev, f"No conversions: {name}",
                               f"{days:.0f} days since last conversion", float(int(days)), thr)]
    return []


def evaluate(monitors, snapshot):
    """Run all enabled monitors against a snapshot; returns a list of alert dicts."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    camps = [c for c in (_g(snap, "campaigns", default=[]) or []) if isinstance(c, dict)]
    dom, dim = _day_bounds(_g(snap, "date", default=""))
    alerts = []
    for mon in monitors or []:
        if not isinstance(mon, dict):
            continue
        mtype = mon.get("type")
        if mtype not in DEFAULTS or mon.get("enabled", True) is False:
            continue
        mid = str(mon.get("id") or mtype)
        sev = mon.get("severity") if mon.get("severity") in SEVERITIES else "warn"
        thr = _num(mon.get("threshold"), DEFAULTS[mtype])
        for camp in camps:
            if _matches(mon, camp):
                alerts.extend(_check(mtype, mid, sev, thr, camp, snap, dom, dim))
    return alerts


def _parse_iso(value):
    """ISO-8601 -> aware datetime (naive assumed UTC); None when invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dedupe(alerts, state, now_iso, repeat_hours=24):
    """Suppress repeats of (monitor, campaign, severity) within repeat_hours; escalations fire.

    Returns (new_alerts, new_state); state is a plain JSON-serialisable dict the caller persists.
    """
    now = _parse_iso(now_iso)
    if now is None:
        raise ToolError("now_iso must be an ISO-8601 timestamp")
    try:
        window = float(repeat_hours)
    except (TypeError, ValueError):
        raise ToolError("repeat_hours must be a number")
    if not math.isfinite(window) or window < 0:
        raise ToolError("repeat_hours must be a finite number >= 0")
    src = state if isinstance(state, dict) else {}
    work = {str(k): (dict(v) if isinstance(v, dict) else v) for k, v in src.items()}
    out = []
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        key = f"{a.get('monitor')}::{a.get('campaign')}"
        sev = str(a.get("severity") or "warn")
        prev = work.get(key)
        if isinstance(prev, dict):
            prev_at = _parse_iso(prev.get("last_fired"))
            # Same-or-lower severity inside the quiet window is a repeat; escalation always fires.
            if (prev_at is not None and now - prev_at < timedelta(hours=window)
                    and _RANK.get(sev, 0) <= _RANK.get(str(prev.get("severity") or ""), 0)):
                continue
        out.append(dict(a))
        work[key] = {"last_fired": _iso(now), "severity": sev}
    return out, work


def brief(snapshot, alerts, title="AdRails daily brief"):
    """Render a compact markdown brief (<= 3500 chars, bullet lines only, no tables)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    camps = [c for c in (_g(snap, "campaigns", default=[]) or []) if isinstance(c, dict)]
    day = str(_g(snap, "date", default="") or "unknown")
    spend = sum(_cnum(c, "spend_today") for c in camps)
    conv = sum(_cnum(c, "conv_today") for c in camps)
    clicks = sum(_cnum(c, "clicks_today") for c in camps)
    enabled = sum(1 for c in camps if _enabled(c))
    ranked = sorted([a for a in (alerts or []) if isinstance(a, dict)],
                    key=lambda a: -_RANK.get(str(a.get("severity") or ""), 0))
    cpa_line = (f"- Blended CPA: {spend / conv:.2f}" if conv > 0
                else "- Blended CPA: n/a (no conversions)")
    lines = [f"**{str(title)}**", f"Date: {day}", "",
             f"- Spend today: {spend:.2f}",
             f"- Conversions: {conv:g}",
             f"- Clicks: {clicks:g}",
             cpa_line,
             f"- Campaigns: {len(camps)} ({enabled} enabled)", "",
             f"**Top issues ({len(ranked)})**"]
    if not ranked:
        lines.append("- No active alerts")
    for a in ranked[:10]:
        t = scrub(str(a.get("title") or "alert"), 200)
        d = scrub(str(a.get("detail") or ""), 300)
        camp = str(a.get("campaign") or "")
        suffix = (f" ({camp})" if camp else "") + (f" — {d}" if d else "")
        lines.append(f"- [{str(a.get('severity') or 'warn').upper()}] {t}{suffix}")
    if len(ranked) > 10:
        lines.append(f"- ... and {len(ranked) - 10} more")
    text = "\n".join(lines)
    if len(text) > BRIEF_LIMIT:
        tail = "\n- ... (truncated)"
        text = text[: BRIEF_LIMIT - len(tail)] + tail
    return text


def deliver(text, notify):
    """Call notify(text); never raises for delivery failures — returns a scrubbed status dict."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError("text must be a non-empty string")
    if not callable(notify):
        raise ToolError("notify must be callable")
    try:
        notify(text)
        return {"delivered": True, "error": None}
    except Exception as exc:  # delivery problems must not abort the scheduled run
        return {"delivered": False, "error": scrub(f"{type(exc).__name__}: {exc}", 500)}


def webhook_notifier(url, http):
    """Build notify(text) that POSTs {"text": text} as JSON via http(method,url,headers,body)->(status,body)."""
    if not isinstance(url, str) or not url.strip():
        raise ToolError("webhook url is required")
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.netloc:
        raise RailViolation("webhook url must be https:// with a host")
    if not callable(http):
        raise ToolError("http must be callable")

    def notify(text):
        body = json.dumps({"text": str(text)})
        try:
            status, resp = http("POST", url.strip(), {"Content-Type": "application/json"}, body)
        except Exception as exc:  # transport errors surface as ToolError with scrubbed text
            raise ToolError("webhook request failed: " + scrub(str(exc), 300))
        try:
            code = int(status)
        except (TypeError, ValueError):
            raise ToolError("webhook returned a non-numeric status")
        if not 200 <= code < 300:
            raise ToolError(f"webhook failed: HTTP {code}: " + scrub(str(resp), 300))
        return {"status": code, "delivered": True}

    return notify


def selftest() -> int:
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    def snap(**over):
        camp = {"key": "111", "name": "Alpha", "status": "ENABLED", "daily_budget": 100.0,
                "spend_today": 50.0, "spend_month": 1000.0, "month_budget": 3000.0,
                "clicks_today": 10, "conv_today": 2.0, "cpa_7d": 10.0, "cpa_30d": 10.0,
                "impressions_today": 500, "disapproved_ads": 0, "lost_budget_is": 0.0,
                "hour": 16, "days_since_conv": 3}
        camp.update(over)
        return {"date": "2024-03-10", "campaigns": [camp]}

    def one(mtype, **kw):
        m = {"id": "m-" + mtype, "type": mtype, "severity": "warn"}
        m.update(kw)
        return [m]

    # --- validate ---
    ok(validate([]) == [], "empty config is valid")
    ok(validate(one("pacing_over")) == [], "valid config passes")
    errs = validate([{"id": "a", "type": "nope"},
                     {"id": "a", "type": "pacing_over", "threshold": -1,
                      "severity": "urgent", "enabled": "yes"}, "x"])
    ok(len(errs) == 6, f"expected 6 config errors, got {len(errs)}: {errs}")
    ok(any("unknown type" in e for e in errs), "unknown type reported")
    ok(any("duplicate" in e for e in errs), "duplicate id reported")
    ok(validate("nope") == ["monitors must be a list"], "non-list config rejected")

    # --- cpa_spike ---
    ok(evaluate(one("cpa_spike"), snap()) == [], "flat CPA does not alert")
    a = evaluate(one("cpa_spike"), snap(cpa_7d=20.0, cpa_30d=10.0))
    ok(len(a) == 1 and a[0]["severity"] == "warn" and a[0]["value"] == 20.0, "CPA spike fires")
    ok(a[0]["threshold"] == 15.0, "CPA spike threshold = 30d CPA * (1+t)")
    ok(evaluate(one("cpa_spike", threshold=1.0), snap(cpa_7d=20.0, cpa_30d=10.0)) == [],
       "CPA spike boundary is strict >")
    ok(evaluate(one("cpa_spike"), snap(cpa_7d=99.0, cpa_30d=0.0)) == [], "zero 30d CPA guarded")

    # --- pacing_over / escalation ---
    ok(evaluate(one("pacing_over"), snap(spend_today=150.0)) == [], "pacing_over boundary is strict >")
    ok(evaluate(one("pacing_over"), snap(spend_today=160.0))[0]["severity"] == "warn", "1.6x is warn")
    ok(evaluate(one("pacing_over"), snap(spend_today=250.0))[0]["severity"] == "crit", "2.5x escalates to crit")

    # --- pacing_under ---
    ok(evaluate(one("pacing_under"), snap(spend_today=10.0, hour=16))[0]["title"].startswith("Underspending"),
       "pacing_under fires late in the day")
    ok(evaluate(one("pacing_under"), snap(spend_today=10.0, hour=14)) == [], "pacing_under silent before hour 15")
    ok(evaluate(one("pacing_under"), snap(spend_today=10.0, status="PAUSED")) == [],
       "pacing_under ignores paused campaigns")

    # --- month_forecast ---
    ok(evaluate(one("month_forecast"), snap()) == [], "forecast within budget is quiet")
    a = evaluate(one("month_forecast"), snap(spend_month=2000.0))
    ok(len(a) == 1 and a[0]["value"] == 6200.0 and a[0]["threshold"] == 3300.0, "month forecast computed")
    ok(evaluate(one("month_forecast"), snap(month_budget=None, spend_month=99999.0)) == [],
       "month_forecast needs month_budget")
    ok(evaluate(one("month_forecast"), {"date": "", "campaigns": [{"spend_month": 9e9, "month_budget": 1}]}) == [],
       "month_forecast needs a parseable date")

    # --- zero_impressions / disapprovals / budget_limited / no_conversions_days ---
    ok(evaluate(one("zero_impressions"), snap(impressions_today=0, hour=11)) == [], "zero imp before noon ignored")
    ok(len(evaluate(one("zero_impressions"), snap(impressions_today=0, hour=12))) == 1, "zero imp at noon fires")
    ok(evaluate(one("zero_impressions"), snap(impressions_today=0, status="PAUSED")) == [], "zero imp paused ignored")
    ok(evaluate(one("disapprovals"), snap()) == [], "no disapprovals -> quiet")
    ok(evaluate(one("disapprovals"), snap(disapproved_ads=2))[0]["value"] == 2.0, "disapprovals fires")
    ok(evaluate(one("budget_limited"), snap(lost_budget_is=0.1)) == [], "budget_limited below threshold")
    ok(len(evaluate(one("budget_limited"), snap(lost_budget_is=0.25))) == 1, "budget_limited fires")
    ok(evaluate(one("no_conversions_days"), snap(days_since_conv=None)) == [], "missing days_since_conv skipped")
    ok(len(evaluate(one("no_conversions_days"), snap(days_since_conv=7))) == 1, "7 days without conv fires")
    ok(evaluate(one("no_conversions_days"), snap(days_since_conv=6)) == [], "6 days without conv quiet")

    # --- filters and malformed rows ---
    ok(len(evaluate(one("pacing_over", campaign="alpha", threshold=1.0), snap(spend_today=150.0))) == 1,
       "campaign filter matches case-insensitively")
    ok(evaluate(one("pacing_over", campaign="Beta"), snap(spend_today=250.0)) == [], "campaign filter excludes")
    ok(evaluate(None, None) == [], "null monitors/snapshot -> no alerts")
    ok(evaluate(one("pacing_over"), {"campaigns": "nope"}) == [], "malformed campaigns -> no alerts")
    a = evaluate(one("pacing_over", threshold=1.0),
                 {"date": "2024-03-10", "campaigns": [{"key": "9", "daily_budget": "1",
                                                       "metrics": {"costMicros": "2500000"}}]})
    ok(len(a) == 1 and a[0]["value"] == 2.5, "micros fallback + string numbers coerced")
    a = evaluate(one("cpa_spike"), {"campaigns": [{"key": "1", "cpa_7d": "20", "cpa_30d": "10"}]})
    ok(len(a) == 1, "string CPA values coerced")
    ok(evaluate(one("pacing_over"), {"campaigns": [{"key": "1", "daily_budget": "abc"}]}) == [],
       "unparseable numbers fall back to 0")

    # --- dedupe ---
    alert = {"monitor": "m1", "campaign": "c1", "severity": "warn", "title": "t",
             "detail": "d", "value": 1.0, "threshold": 1.0}
    new, st = dedupe([alert], {}, "2024-03-10T12:00:00Z")
    ok(len(new) == 1 and "m1::c1" in st and st["m1::c1"]["severity"] == "warn", "first alert fires")
    ok(dedupe([alert], st, "2024-03-10T13:00:00Z")[0] == [], "repeat suppressed within window")
    esc = [dict(alert, severity="crit")]
    new, st2 = dedupe(esc, st, "2024-03-10T13:30:00Z")
    ok(len(new) == 1 and st2["m1::c1"]["severity"] == "crit", "escalation always fires")
    ok(dedupe([alert], st2, "2024-03-10T14:00:00Z")[0] == [], "de-escalation suppressed in window")
    ok(len(dedupe([alert], st2, "2024-03-11T13:31:00Z")[0]) == 1, "repeat after window fires")
    ok(len(dedupe([alert], st, "2024-03-10T12:00:01Z", repeat_hours=0)[0]) == 1, "repeat_hours=0 never suppresses")
    try:
        dedupe([alert], {}, "not-a-date")
        ok(False, "bad now_iso must raise")
    except ToolError:
        ok(True, "bad now_iso raises ToolError")
    try:
        dedupe([alert], {}, "2024-03-10T12:00:00Z", repeat_hours="soon")
        ok(False, "bad repeat_hours must raise")
    except ToolError:
        ok(True, "bad repeat_hours raises ToolError")

    # --- brief ---
    b = brief({"date": "2024-03-10", "campaigns": [
        {"daily_budget": 100, "spend_today": 50, "conv_today": 2, "clicks_today": 10, "status": "ENABLED"}]}, [])
    ok("AdRails daily brief" in b and "Spend today: 50.00" in b and "25.00" in b, "brief totals rendered")
    ok("No active alerts" in b and "|" not in b, "brief has no alerts and no markdown tables")
    ok(brief(None, None).startswith("**AdRails daily brief**"), "brief tolerates empty inputs")
    # Prose-like titles/details (spaces break token-ish patterns) so scrubbing can't shrink
    # the rendering below the cap; the brief must then be truncated with the marker.
    many = [dict(alert, campaign=f"c{i}",
                 title="Manual review needed for this campaign " * 6,
                 detail="Spend is tracking above the planned daily pace " * 10)
            for i in range(30)]
    long_brief = brief(snap(), many)
    ok(len(long_brief) <= BRIEF_LIMIT, "brief capped at 3500 chars")
    ok(long_brief.endswith("(truncated)"), "truncated brief ends with marker")

    # --- deliver ---
    sent = []
    ok(deliver("hello", sent.append) == {"delivered": True, "error": None} and sent == ["hello"],
       "deliver calls notify")

    def boom(_text):
        raise RuntimeError("token=SECRET123 rejected")

    res = deliver("hello", boom)
    ok(res["delivered"] is False and isinstance(res["error"], str) and res["error"], "deliver reports errors")
    try:
        deliver("", sent.append)
        ok(False, "empty text must raise")
    except ToolError:
        ok(True, "empty text raises ToolError")

    # --- webhook_notifier ---
    try:
        webhook_notifier("http://hooks.example.com/x", lambda *a: (200, "ok"))
        ok(False, "http:// must be refused")
    except RailViolation:
        ok(True, "non-https webhook refused")
    try:
        webhook_notifier("", lambda *a: (200, "ok"))
        ok(False, "empty url must raise")
    except ToolError:
        ok(True, "empty webhook url raises ToolError")

    seen = {}

    def fake_http(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return (200, "ok")

    wh = webhook_notifier("https://hooks.example.com/x", fake_http)
    ok(wh("hello")["delivered"] is True, "webhook notify returns delivered")
    ok(seen["method"] == "POST" and json.loads(seen["body"]) == {"text": "hello"}, "webhook posts JSON text")
    ok(any(k.lower() == "content-type" for k in seen["headers"]), "webhook sets content-type")

    try:
        webhook_notifier("https://hooks.example.com/x", lambda *a: (500, "boom"))("hi")
        ok(False, "non-2xx must raise")
    except ToolError:
        ok(True, "non-2xx webhook raises ToolError")
    try:
        webhook_notifier("https://hooks.example.com/x", lambda *a: (302, "moved"))("hi")
        ok(False, "3xx must raise")
    except ToolError:
        ok(True, "3xx webhook raises ToolError")

    def bad_http(*_a):
        raise OSError("connection reset")

    try:
        webhook_notifier("https://hooks.example.com/x", bad_http)("hi")
        ok(False, "transport error must raise")
    except ToolError:
        ok(True, "transport error raises ToolError")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
