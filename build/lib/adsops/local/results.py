"""Monthly results report for a small business using AdRails.

Pure functions over leads, events, spend, AI-visibility panels, reviews and
plan progress. No file/network I/O at import time; every external effect is
passed in. Outputs are JSON-safe: ISO strings, no sets, no datetime objects,
no customer names, phones or emails. Tools should call build() once, then
to_markdown() for the owner or to_case_study() for a shareable summary.
Money is rounded to 2 dp; rates to 3 dp. Missing data stays None and appears
in gaps rather than being invented as zero.
"""
from __future__ import annotations
import re

from datetime import datetime, date, timezone, timedelta
from statistics import median

from ..errors import ToolError

PRIVACY = "contains no customer names, phones or emails"


KNOWN_SOURCES = ("google_lsa", "google_ads", "website", "ai_assistant", "google_organic", "gbp", "referral", "phone",
                 "direct_mail", "chatgpt_ads", "email", "walk_in", "social", "other")


def _g(row, *path, default=None):
    """Nested dict get for camelCase/snake_case Google Ads style rows."""
    cur = row
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p, default)
        if cur is None:
            return default
    return cur


def _num(v, default=0.0):
    if v is None or v == "" or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(s):
    """Parse ISO strings (Z or offset), dates, or datetimes into UTC datetime."""
    if isinstance(s, datetime):
        dt = s
    elif isinstance(s, date):
        dt = datetime(s.year, s.month, s.day, tzinfo=timezone.utc)
    elif isinstance(s, str) and s.strip():
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in(dt_utc, s, e):
    """Window membership by the owner's LOCAL calendar date (review 11-06 #1: a 9:30pm lead in New York is 01:30Z next day;
    comparing UTC instants against date windows dropped it). s/e are UTC midnights of the window's dates."""
    return s.date() <= dt_utc.astimezone().date() < e.date()


def _window(start, end):
    s, e = _parse_dt(start), _parse_dt(end)
    if s is None or e is None:
        raise ToolError("invalid period window")
    return s, e


def _days(v, name):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer between 1 and 365")
    if n < 1 or n > 365:
        raise ToolError(f"{name} must be between 1 and 365")
    return n


def periods(today, baseline_days=30, current_days=30, started_using=None):
    """Return baseline and current windows as [start, end) ISO date strings."""
    td = _parse_dt(today)
    if td is None:
        raise ToolError("today is not a valid ISO date")
    bd, cd = _days(baseline_days, "baseline_days"), _days(current_days, "current_days")
    today_d = td.date()
    cur_end = today_d + timedelta(days=1)
    cur_start = cur_end - timedelta(days=cd)
    if started_using:
        su = _parse_dt(started_using)
        if su is None:
            raise ToolError("started_using is not a valid ISO date")
        base_end = su.date()
        base_start = base_end - timedelta(days=bd)
        cur_start = max(cur_start, base_end)      # review #2: windows never overlap, a lead is never both before AND after
    else:
        base_end = cur_start
        base_start = base_end - timedelta(days=bd)
    return {"baseline": [base_start.isoformat(), base_end.isoformat()],
            "current": [cur_start.isoformat(), cur_end.isoformat()]}


def lead_metrics(leads, events, start, end, spend=None):
    """Lead volume, wins, response speed and by-source cost for one window."""
    s, e = _window(start, end)
    in_window = []
    for l in leads or []:
        if not isinstance(l, dict):
            continue
        created = _parse_dt(l.get("created_at"))
        if created is not None and _in(created, s, e):
            in_window.append(l)
    spam = sum(1 for l in in_window if str(l.get("stage") or "").lower() == "spam")
    real = [l for l in in_window if str(l.get("stage") or "").lower() != "spam"]

    contacts = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        lid = ev.get("lead_id")
        if lid is None or ev.get("from") != "new" or ev.get("to") in (None, "", "new"):
            continue
        at = _parse_dt(ev.get("at"))
        if at is not None:
            key = str(lid)
            if key not in contacts or at < contacts[key]:
                contacts[key] = at

    responses, never = [], 0
    for l in real:
        lid = str(l.get("id"))
        created = _parse_dt(l.get("created_at"))
        stage = str(l.get("stage") or "").lower()
        if lid in contacts and created is not None:
            responses.append((contacts[lid] - created).total_seconds() / 60.0)
        elif stage not in ("won", "lost"):
            never += 1

    by, spend_map = {}, spend if isinstance(spend, dict) else None
    for l in real:
        src = str(l.get("source") or "other")
        src = src if src in KNOWN_SOURCES else "other"      # review #6: free-text sources never become report labels
        d = by.setdefault(src, {"leads": 0, "won": 0, "won_value": 0.0})
        d["leads"] += 1
        if str(l.get("stage") or "").lower() == "won":
            d["won"] += 1
            d["won_value"] += _num(l.get("value"), 0.0)

    out_src = {}
    for src, d in by.items():
        # a source with no spend entry is UNKNOWN (None -> "n/a"), never $0: an organic channel stays n/a unless spend says 0
        sp = None if spend_map is None or spend_map.get(src) is None else round(_num(spend_map.get(src), 0.0), 2)
        cpl = round(sp / d["leads"], 2) if sp is not None and d["leads"] > 0 else None
        cpw = round(sp / d["won"], 2) if sp is not None and d["won"] > 0 else None
        out_src[src] = {"leads": d["leads"], "won": d["won"],
                        "won_value": round(d["won_value"], 2), "spend": sp,
                        "cost_per_lead": cpl, "cost_per_won": cpw}

    n = len(real)
    won = sum(d["won"] for d in by.values())
    won_value = round(sum(d["won_value"] for d in by.values()), 2)
    within = sum(1 for m in responses if m <= 5.0)
    return {
        "leads": n, "spam": spam, "by_source": out_src, "won": won,
        "won_value": won_value,
        "close_rate": round(won / n, 3) if n else None,
        "median_first_response_minutes": round(median(responses), 1) if responses else None,
        "answered_within_5_min_pct": round(within / len(responses), 3) if responses else None,
        "never_contacted": never,
    }


def visibility_metrics(visibility, start, end, brand):
    """Metrics from the LAST AI-visibility run in the window, unbranded rows only."""
    s, e = _window(start, end)
    runs = []
    for r in visibility or []:
        if not isinstance(r, dict):
            continue
        ts = _parse_dt(r.get("ts"))
        if ts is not None and _in(ts, s, e):
            runs.append((ts, r))
    if not runs:
        return None
    ts, run = sorted(runs, key=lambda x: x[0])[-1]
    brand_l = (brand or "").strip().lower()
    rows = []
    for res in run.get("results") or []:
        if not isinstance(res, dict):
            continue
        prompt = str(res.get("prompt") or "")
        if res.get("branded") is True or (brand_l and brand_l in prompt.lower()):
            continue
        rows.append(res)
    prompts = {str(r.get("prompt") or "") for r in rows}
    answers = len(rows)
    mentioned = sum(1 for r in rows if bool(r.get("mentioned")))
    cited = sum(1 for r in rows if bool(r.get("cited")))
    engines = sorted({str(r.get("engine")) for r in rows if r.get("engine") not in (None, "")})
    return {
        "run_id": run.get("run_id"),
        "ts": run.get("ts") if isinstance(run.get("ts"), str) else ts.isoformat(),
        "prompts": len(prompts), "answers": answers, "mentioned": mentioned,
        "share_of_answer": round(mentioned / answers, 3) if answers else None,
        "cited": cited, "engines": engines,
    }


def compare(baseline, current):
    """Numeric top-level before/after/change/pct for keys present in both."""
    baseline, current = baseline or {}, current or {}
    out = {}
    for k, b in baseline.items():
        if k not in current:
            continue
        a = current[k]
        if isinstance(b, bool) or isinstance(a, bool):
            continue
        if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
            continue
        change = a - b
        if isinstance(change, float):
            change = round(change, 6)
        out[k] = {"before": b, "after": a, "change": change,
                  "pct": round((a - b) / b, 3) if b not in (0, None) else None}
    return out


def _fmt_count(v):
    if v is None:
        return "n/a"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _fmt_money(v):
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v - round(v)) < 1e-9:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def _signed_money(v):
    if v is None:
        return "n/a"
    return ("+" if v >= 0 else "-") + _fmt_money(abs(v))


def _fmt_rate(v):
    if v is None:
        return "n/a"
    return f"{float(v) * 100:.0f}%"


def _fmt_minutes(v):
    if v is None:
        return "n/a"
    v = float(v)
    return f"{int(v)} min" if v.is_integer() else f"{v:.1f} min"


def _fmt_val(v, kind):
    if v is None:
        return "n/a"
    if kind == "money":
        return _fmt_money(v)
    if kind == "rate":
        return _fmt_rate(v)
    if kind == "minutes":
        return _fmt_minutes(v)
    if kind == "rating":
        return f"{float(v):.1f}"
    return _fmt_count(v)


def _change_text(before, after, pct, kind):
    if before is None or after is None:
        return "n/a"
    if kind == "rate":
        return f"{(after - before) * 100:+.0f} pts"
    if kind == "rating":
        return f"{after - before:+.1f}"
    if kind == "minutes":
        return f"{after - before:+.0f} min"
    if pct is not None:
        return f"{pct * 100:+.0f}%"
    diff = after - before
    if kind == "money":
        return _signed_money(diff)
    return f"{diff:+.0f}" if isinstance(diff, (int, float)) else str(diff)


def _table_rows(report):
    rows = []
    bl = (report.get("baseline") or {}).get("leads") or {}
    cl = (report.get("current") or {}).get("leads") or {}
    changes = report.get("changes") or {}

    def add(metric, before, after, kind, key=None):
        pct = (changes.get(key) or {}).get("pct") if key else None
        rows.append({"metric": metric, "before": _fmt_val(before, kind),
                     "after": _fmt_val(after, kind),
                     "change": _change_text(before, after, pct, kind)})

    add("Leads", bl.get("leads"), cl.get("leads"), "count", "leads")
    add("Jobs won", bl.get("won"), cl.get("won"), "count", "won")
    add("Won value", bl.get("won_value"), cl.get("won_value"), "money", "won_value")
    add("Close rate", bl.get("close_rate"), cl.get("close_rate"), "rate", "close_rate")
    add("Median first response", bl.get("median_first_response_minutes"),
        cl.get("median_first_response_minutes"), "minutes", "median_first_response_minutes")
    bv = (report.get("baseline") or {}).get("visibility") or {}
    cv = (report.get("current") or {}).get("visibility") or {}
    add("AI share of answer", bv.get("share_of_answer"), cv.get("share_of_answer"),
        "rate", "share_of_answer")
    rev = report.get("reviews") or {}
    add("Reviews", rev.get("baseline_count"), rev.get("current_count"), "count")
    add("Rating", rev.get("baseline_rating"), rev.get("current_rating"), "rating")
    bsrc, csrc = bl.get("by_source") or {}, cl.get("by_source") or {}
    for src in sorted(set(bsrc) | set(csrc)):
        add(f"Cost per lead ({src})", (bsrc.get(src) or {}).get("cost_per_lead"),
            (csrc.get(src) or {}).get("cost_per_lead"), "money")
    return rows


def _highlights(b, c, bv, cv, rev):
    items = []

    def pos(key, label, fmt=_fmt_count):
        before, after = b.get(key), c.get(key)
        if before is not None and after is not None and after > before:
            pct = (after - before) / before if before else None
            items.append((pct if pct is not None else after - before,
                          f"{label} went from {fmt(before)} to {fmt(after)}"
                          f"{f' ({pct * 100:+.0f}%).' if pct is not None else '.'}"))

    pos("leads", "Leads")
    pos("won", "Jobs won")
    pos("won_value", "Won value", _fmt_money)
    bm, cm = b.get("median_first_response_minutes"), c.get("median_first_response_minutes")
    if bm is not None and cm is not None and cm < bm:
        drop = (bm - cm) / bm if bm else None
        items.append((drop if drop is not None else bm - cm,
                      f"Median first response fell from {_fmt_minutes(bm)} to {_fmt_minutes(cm)}."))
    br, cr = b.get("close_rate"), c.get("close_rate")
    if br is not None and cr is not None and cr > br:
        items.append((cr - br, f"Close rate went from {_fmt_rate(br)} to {_fmt_rate(cr)}."))
    bsrc, csrc = b.get("by_source") or {}, c.get("by_source") or {}
    for src in csrc:
        bcpl = (bsrc.get(src) or {}).get("cost_per_lead")
        ccpl = (csrc.get(src) or {}).get("cost_per_lead")
        if bcpl is not None and ccpl is not None and ccpl < bcpl:
            drop = (bcpl - ccpl) / bcpl if bcpl else None
            items.append((drop if drop is not None else bcpl - ccpl,
                          f"Cost per lead on {src} fell from {_fmt_money(bcpl)} to {_fmt_money(ccpl)}."))
    if cv and cv.get("answers"):
        ment, ans = cv.get("mentioned") or 0, cv.get("answers") or 0
        bment = bv.get("mentioned") if bv else None
        if bment is not None and ment > bment:
            items.append((ment - bment, f"Named in {ment} of {ans} AI answers, up from {bment}."))
        elif bv is None:
            items.append((0, f"Named in {ment} of {ans} AI answers."))
    if rev and rev.get("new_reviews") is not None and rev["new_reviews"] > 0:
        items.append((rev["new_reviews"], f"Added {rev['new_reviews']} new reviews."))
    items.sort(key=lambda x: x[0] if x[0] is not None else -1e9, reverse=True)
    return [s for _, s in items[:5]]


def _gaps(visibility, bv, cv, spend, baseline_spend, reviews, plan, profile):
    gaps = []
    if visibility is None:
        gaps.append("no visibility data: record a panel with aeo_visibility_record")
    else:
        if bv is None:
            gaps.append("no visibility panel in the baseline period: record one with aeo_visibility_record")
        if cv is None:
            gaps.append("no visibility panel in the current period: record one with aeo_visibility_record")
    if spend is None and baseline_spend is None:
        gaps.append("no spend data: pass spend from adsops_lsa_report / adsops_campaigns")
    else:
        if spend is None:
            gaps.append("no current spend data: pass spend from adsops_lsa_report / adsops_campaigns")
        if baseline_spend is None:
            gaps.append("no baseline spend data: pass baseline_spend from adsops_lsa_report / adsops_campaigns")
    if not isinstance(reviews, dict) or reviews.get("baseline_count") is None or reviews.get("current_count") is None:
        gaps.append("no review counts")
    elif reviews.get("baseline_rating") is None or reviews.get("current_rating") is None:
        gaps.append("no review ratings")
    if not isinstance(plan, dict):
        gaps.append("no plan progress: pass plan and done_ids")
    if profile.get("avg_job_value") is None:
        gaps.append("no average job value: pass profile.avg_job_value to value wins")
    return gaps


def build(today, leads, events, visibility=None, spend=None, baseline_spend=None,
          reviews=None, plan=None, done_ids=None, profile=None, brand=None,
          baseline_days=30, current_days=30):
    """Assemble the full before/after report. Missing data stays None."""
    profile = profile if isinstance(profile, dict) else {}
    p = periods(today, baseline_days, current_days, profile.get("started_using"))
    b_start, b_end = p["baseline"]
    c_start, c_end = p["current"]
    b_leads = lead_metrics(leads, events, b_start, b_end, baseline_spend)
    c_leads = lead_metrics(leads, events, c_start, c_end, spend)
    no_baseline = (b_leads.get("leads") or 0) + (b_leads.get("spam") or 0) == 0
    if no_baseline:                    # review #3: nothing recorded before = unknown, never "grew from 0"
        b_leads = {k: (None if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in b_leads.items()}
    use_brand = brand or profile.get("business_name")
    b_vis = visibility_metrics(visibility, b_start, b_end, use_brand)
    c_vis = visibility_metrics(visibility, c_start, c_end, use_brand)
    b_all, c_all = dict(b_leads), dict(c_leads)
    if b_vis:
        b_all.update(b_vis)
    if c_vis:
        c_all.update(c_vis)
    changes = compare(b_all, c_all)

    rev = None
    if isinstance(reviews, dict):
        rev = {k: reviews.get(k) for k in
               ("baseline_count", "current_count", "baseline_rating", "current_rating")}
        bc, cc = reviews.get("baseline_count"), reviews.get("current_count")
        rev["new_reviews"] = _int(cc) - _int(bc) if bc is not None and cc is not None else None

    pl = None
    if isinstance(plan, dict):
        tasks = plan.get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        done_set = {str(x) for x in (done_ids or []) if x is not None}
        done_tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("id")) in done_set]
        pl = {"tasks": len(tasks), "done": len(done_tasks),
              "done_pct": round(len(done_tasks) / len(tasks), 3) if tasks else None,
              "owner_minutes_done": int(sum(_num(t.get("owner_minutes"), 0) for t in done_tasks))}

    business = {"name": profile.get("business_name"), "trade": profile.get("trade"),
                "city": profile.get("city"), "started_using": profile.get("started_using")}
    return {
        "business": business, "periods": p,
        "baseline": {"leads": b_leads, "visibility": b_vis},
        "current": {"leads": c_leads, "visibility": c_vis},
        "changes": changes, "reviews": rev, "plan": pl,
        "highlights": _highlights(b_leads, c_leads, b_vis, c_vis, rev),
        "gaps": (["no leads recorded in the baseline period: import the old enquiries (adsops_leads action=import) or ask the "
                  "owner for an estimate, otherwise before/after lead numbers are unknown"] if no_baseline else [])
                + _gaps(visibility, b_vis, c_vis, spend, baseline_spend, reviews, plan, profile),
        "privacy": PRIVACY,
    }


def to_markdown(report):
    """Owner-readable GitHub markdown report."""
    business = report.get("business") or {}
    name = business.get("name") or "your business"
    p = report.get("periods") or {}
    lines = [f"# Results: {name}", ""]

    def period_line(label, pair):
        if not pair or len(pair) < 2:
            return f"- {label}: n/a"
        return f"- {label}: {pair[0]} to {pair[1]}"

    lines.append(period_line("Baseline", p.get("baseline")))
    lines.append(period_line("Current", p.get("current")))
    lines += ["", "| Metric | Before | After | Change |", "|---|---:|---:|---:|"]
    for row in _table_rows(report):
        lines.append(f"| {row['metric']} | {row['before']} | {row['after']} | {row['change']} |")
    hl = report.get("highlights") or []
    if hl:
        lines += ["", "## Highlights"]
        lines += [f"- {h}" for h in hl]
    plan = report.get("plan")
    lines += ["", "## Plan progress"]
    if plan:
        lines.append(f"- Tasks done: {plan.get('done', 0)} of {plan.get('tasks', 0)}")
        if plan.get("done_pct") is not None:
            lines.append(f"- Progress: {plan['done_pct'] * 100:.0f}%")
        lines.append(f"- Owner minutes invested: {plan.get('owner_minutes_done', 0)}")
    else:
        lines.append("- n/a")
    gaps = report.get("gaps") or []
    lines += ["", "## Gaps"]
    lines += [f"- {g}" for g in gaps] if gaps else ["- none"]
    return "\n".join(lines).rstrip() + "\n"


_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def _clean_quote(quote, business_name, permission):
    """Review #6: the quote is the one free-text field in a shareable payload. Phones and emails are refused outright (the owner
    rewrites it); the business name is masked unless the owner said the name may be used."""
    if not quote:
        return None
    q = str(quote)[:600]
    if _PHONE.search(q) or _EMAIL.search(q):
        raise ToolError("the quote contains a phone number or email address; ask the owner for a version without contact details")
    if business_name and permission != "name":
        q = re.sub(re.escape(str(business_name)), "[the business]", q, flags=re.I)
    return q


def to_case_study(report, quote=None, permission=None):
    """Shareable case-study payload; business name hidden unless permission == 'name'."""
    if permission not in (None, "anonymous", "name"):
        raise ToolError("permission must be one of: anonymous, name")
    business = report.get("business") or {}
    biz = {"trade": business.get("trade"), "city": business.get("city")}
    if permission == "name":
        biz["name"] = business.get("name")
    return {
        "version": 1, "business": biz, "periods": report.get("periods"),
        "before_after": _table_rows(report), "highlights": report.get("highlights") or [],
        "gaps": report.get("gaps") or [], "owner_quote": _clean_quote(quote, business.get("name"), permission),
        "permission": permission, "privacy": report.get("privacy") or PRIVACY,
    }


def selftest() -> int:
    n = 0

    def ok(cond, msg):
        nonlocal n
        assert cond, msg
        n += 1

    p = periods("2024-04-30", baseline_days=5, current_days=3)
    ok(p["current"] == ["2024-04-28", "2024-05-01"], "current period")
    ok(p["baseline"] == ["2024-04-23", "2024-04-28"], "baseline period")
    p2 = periods("2024-04-30", 5, 3, "2024-04-10")
    ok(p2["baseline"] == ["2024-04-05", "2024-04-10"], "started_using baseline")
    try:
        periods("2024-04-30", baseline_days=0)
        ok(False, "bad days should raise")
    except ToolError:
        ok(True, "bad days raised")
    try:
        periods("not-a-date")
        ok(False, "bad today should raise")
    except ToolError:
        ok(True, "bad today raised")

    leads = [
        {"id": "b1", "created_at": "2024-04-23T10:00:00Z", "source": "google_lsa",
         "stage": "won", "value": 500, "name": "Jane Roe", "phone": "8135550100",
         "email": "jane@example.com"},
        {"id": "b2", "created_at": "2024-04-24T10:00:00+00:00", "source": "google_lsa",
         "stage": "new", "value": None},
        {"id": "b3", "created_at": "2024-04-24T11:00:00+00:00", "source": "google_ads",
         "stage": "spam", "name": "Spam"},
        {"id": "c1", "created_at": "2024-04-28T09:00:00Z", "source": "google_lsa",
         "stage": "won", "value": 800, "name": "Jane Roe", "phone": "8135550100",
         "email": "jane@example.com"},
        {"id": "c2", "created_at": "2024-04-28T09:10:00+00:00", "source": "google_lsa",
         "stage": "contacted"},
        {"id": "c3", "created_at": "2024-04-29T09:00:00Z", "source": "google_ads",
         "stage": "new"},
        {"id": "c4", "created_at": "2024-04-29T10:00:00Z", "source": "google_ads",
         "stage": "spam", "name": "Spam"},
    ]
    events = [
        {"lead_id": "b2", "from": "new", "to": "contacted", "at": "2024-04-24T11:35:00Z"},
        {"lead_id": "c2", "from": "new", "to": "contacted", "at": "2024-04-28T09:16:00Z"},
        {"lead_id": "c1", "from": "new", "to": "contacted", "at": "2024-04-28T09:03:00Z"},
    ]
    cur = lead_metrics(leads, events, "2024-04-28", "2024-05-01",
                       spend={"google_lsa": 300.0, "google_ads": 100.0})
    ok(cur["leads"] == 3, "spam excluded from leads")
    ok(cur["spam"] == 1, "spam counted")
    ok(cur["won"] == 1 and cur["won_value"] == 800.0, "wins/value")
    ok(abs(cur["close_rate"] - 0.333) < 1e-9, "close rate")
    ok(cur["median_first_response_minutes"] == 4.5, "median response")
    ok(abs(cur["answered_within_5_min_pct"] - 0.5) < 1e-9, "5-min pct")
    ok(cur["never_contacted"] == 1, "never contacted open lead")
    lsa = cur["by_source"]["google_lsa"]
    ok(lsa["cost_per_lead"] == 150.0 and lsa["cost_per_won"] == 300.0, "lsa CPL/CPW")
    ads = cur["by_source"]["google_ads"]
    ok(ads["cost_per_lead"] == 100.0 and ads["cost_per_won"] is None, "ads CPL")
    ok("Jane Roe" not in str(cur) and "8135550100" not in str(cur), "no PII in lead metrics")
    base = lead_metrics(leads, events, "2024-04-23", "2024-04-28",
                        spend={"google_lsa": 100.0})
    ok(base["leads"] == 2 and base["spam"] == 1, "baseline spam/leads")
    ok(base["median_first_response_minutes"] == 95.0, "baseline median")
    empty_lm = lead_metrics([], [], "2024-01-01", "2024-02-01")
    ok(empty_lm["leads"] == 0 and empty_lm["close_rate"] is None
       and empty_lm["median_first_response_minutes"] is None, "empty lead metrics")
    mal = lead_metrics([{"id": "x", "created_at": "bad", "source": "x"}], [],
                       "2024-01-01", "2024-02-01")
    ok(mal["leads"] == 0, "malformed created_at skipped")

    vis = [
        {"run_id": "r1", "ts": "2024-04-25T00:00:00Z", "results": [
            {"engine": "chatgpt", "prompt": "plumber near me", "mentioned": False, "cited": False},
            {"engine": "perplexity", "prompt": "best plumber in Tampa", "mentioned": True, "cited": True},
            {"engine": "chatgpt", "prompt": "Jane Roe plumbing reviews", "mentioned": True,
             "cited": True, "branded": True}]},
        {"run_id": "r2", "ts": "2024-04-29T00:00:00Z", "results": [
            {"engine": "chatgpt", "prompt": "plumber near me", "mentioned": True, "cited": False},
            {"engine": "perplexity", "prompt": "best plumber in Tampa", "mentioned": True, "cited": True},
            {"engine": "gemini", "prompt": "emergency plumber", "mentioned": False, "cited": False},
            {"engine": "chatgpt", "prompt": "Jane Roe plumbing", "mentioned": True, "cited": True}]},
    ]
    v = visibility_metrics(vis, "2024-04-28", "2024-05-01", "Jane Roe Plumbing")
    ok(v is not None and v["run_id"] == "r2", "last visibility run")
    ok(v["answers"] == 3 and v["prompts"] == 3, "branded rows excluded")
    ok(v["mentioned"] == 2 and abs(v["share_of_answer"] - 0.667) < 1e-9, "visibility share")
    ok(v["cited"] == 1, "visibility cited")
    ok(v["engines"] == ["chatgpt", "gemini", "perplexity"], "visibility engines")
    vb = visibility_metrics(vis, "2024-04-23", "2024-04-28", "Jane Roe Plumbing")
    ok(vb["answers"] == 2 and vb["mentioned"] == 1, "baseline visibility")
    ok(visibility_metrics(vis, "2023-01-01", "2023-02-01", "X") is None, "no run -> None")

    cmp = compare({"a": 0, "b": 4, "c": None}, {"a": 5, "b": 11, "c": 3})
    ok(cmp["a"]["pct"] is None and cmp["a"]["change"] == 5, "compare zero pct None")
    ok(abs(cmp["b"]["pct"] - 1.75) < 1e-9, "compare pct")
    ok("c" not in cmp, "compare skips None")

    profile = {"business_name": "Jane Roe Plumbing", "trade": "Plumbing", "city": "Tampa",
               "started_using": "2024-04-28", "avg_job_value": 500}
    reviews = {"baseline_count": 10, "current_count": 10,
               "baseline_rating": 4.5, "current_rating": 4.6}
    plan = {"tasks": [{"id": "t1", "stage": "done", "owner_minutes": 30},
                      {"id": "t2", "stage": "todo", "owner_minutes": 60}]}
    report = build("2024-04-30", leads, events, visibility=vis,
                   spend={"google_lsa": 300.0, "google_ads": 100.0},
                   baseline_spend={"google_lsa": 100.0}, reviews=reviews,
                   plan=plan, done_ids=["t1"], profile=profile,
                   brand="Jane Roe Plumbing", baseline_days=5, current_days=3)
    ok(report["business"]["name"] == "Jane Roe Plumbing", "build business")
    ok(report["periods"]["baseline"] == ["2024-04-23", "2024-04-28"], "build periods")
    ok(report["current"]["leads"]["leads"] == 3, "build current leads")
    ok(report["plan"]["done"] == 1 and report["plan"]["owner_minutes_done"] == 30
       and report["plan"]["done_pct"] == 0.5, "build plan")
    ok(report["reviews"]["new_reviews"] == 0, "build new reviews")
    ok(len(report["highlights"]) <= 5
       and report["highlights"][0].startswith("Named in 2 of 3 AI answers"),
       "highlights order")
    ok(any("Median first response fell" in h for h in report["highlights"]),
       "response highlight")
    ok("no visibility data" not in " ".join(report["gaps"]), "no false visibility gap")

    empty = build("2024-04-30", leads, events, profile={"business_name": "X"},
                  brand="X", baseline_days=5, current_days=3)
    gaps = " ".join(empty["gaps"])
    ok("no visibility data" in gaps, "gap visibility")
    ok("no spend data" in gaps, "gap spend")
    ok("no review counts" in gaps, "gap reviews")
    ok("no plan progress" in gaps, "gap plan")

    md = to_markdown(empty)
    ok("| Metric | Before | After | Change |" in md, "markdown table header")
    ok("n/a" in md, "markdown n/a for missing")
    ok("# Results: X" in md, "markdown title")

    cs = to_case_study(report, quote="Great", permission=None)
    s = str(cs)
    ok("Jane Roe" not in s, "case study hides business name")
    ok("8135550100" not in s and "jane@example.com" not in s, "case study no PII")
    ok(cs["permission"] is None and cs["before_after"][0]["metric"] == "Leads",
       "case study shape")
    cs2 = to_case_study(report, permission="name")
    ok(cs2["business"]["name"] == "Jane Roe Plumbing", "case study name permission")
    try:
        to_case_study(report, permission="public")
        ok(False, "bad permission should raise")
    except ToolError:
        ok(True, "bad permission raised")
    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
