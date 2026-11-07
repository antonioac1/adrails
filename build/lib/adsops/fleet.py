"""Fleet-wide rollups and aggregate spend guardrails for AdRails.

Aggregates per-campaign rows from many ad accounts/platforms (google, openai,
meta, microsoft) into per-account, per-platform and total views, then enforces
an aggregate spend cap using a worst-case exposure model (platforms may
overspend a daily budget by `overspend_factor`, default 2x).

Safety properties: money is never summed across currencies (mixed-currency
accounts get split keys plus a warning), caps are evaluated on the worst-case
post-proposal exposure, and any proposal that *lowers* exposure is never
blocked. A tool should call `rollup(rows)` for reporting, `aggregate_check`
for a dry run, and `enforce` immediately before applying a budget change.
"""

import math

from adsops.errors import RailViolation, ToolError, scrub

_ENABLED_WORDS = {"enabled", "on", "active", "true", "1", "yes"}


# --------------------------------------------------------------------------- #
# small coercion helpers (rows are untyped JSON, numbers may arrive as strings)
# --------------------------------------------------------------------------- #
def _s(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _g(row, *path, default=None):
    """Nested get for camelCase REST rows: _g(r, 'metrics', 'costMicros')."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
        if cur is None:
            return default
    return cur


def _num(value, default=0.0) -> float:
    """Defensive float coercion; '$1,000.50', None, garbage -> 0.0 (or default)."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            out = float(value)
        except (TypeError, ValueError):
            return default
        return out if math.isfinite(out) else default
    try:
        out = float(str(value).strip().replace(",", "").replace("$", "").replace(" ", ""))
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _money(value) -> float:
    return round(_num(value), 2)


def _micros_to_units(value) -> float:
    return _num(value) / 1_000_000.0


def _is_enabled(status) -> bool:
    return _s(status).lower() in _ENABLED_WORDS


def _norm_row(row):
    """Coerce a flat AdRails row (or nested Google Ads REST row) to canonical keys."""
    if not isinstance(row, dict):
        return None
    camp_raw = row.get("campaign")
    if isinstance(camp_raw, dict):
        camp = _s(camp_raw.get("name")) or _s(camp_raw.get("id")) or _s(camp_raw.get("resourceName"))
    else:
        camp = _s(camp_raw) or _s(_g(row, "adGroup", "name"))
    account = (_s(row.get("account")) or _s(_g(row, "customer", "descriptiveName"))
               or _s(_g(row, "customer", "id")) or "unknown")
    platform = _s(row.get("platform")).lower()
    if not platform:
        # nested Google Ads rows carry no platform field; their shape tells us
        platform = "google" if isinstance(camp_raw, dict) or "metrics" in row else "unknown"
    currency = (_s(row.get("currency")) or _s(_g(row, "customer", "currencyCode")) or "USD").upper()
    status = _s(row.get("status")) or _s(_g(row, "campaign", "status"))
    cost_micros = _g(row, "metrics", "costMicros")
    spend_today = _num(row.get("spend_today")) if "spend_today" in row else _micros_to_units(cost_micros)
    if "spend_month" in row:
        spend_month = _num(row.get("spend_month"))
    else:
        spend_month = _micros_to_units(cost_micros) if cost_micros is not None else 0.0
    budget = row.get("daily_budget")
    if budget is None:
        budget = _micros_to_units(_g(row, "campaign", "campaignBudget", "amountMicros"))
    conv = row.get("conv_month")
    if conv is None:
        conv = _g(row, "metrics", "conversions")
    return {
        "account": account, "platform": platform, "campaign": camp, "status": status,
        "spend_today": _num(spend_today), "spend_month": _num(spend_month),
        "daily_budget": _num(budget), "conv_month": _num(conv), "currency": currency,
    }


# --------------------------------------------------------------------------- #
# rollup
# --------------------------------------------------------------------------- #
def _blank_bucket(currency):
    return {"currency": currency, "spend_today": 0.0, "spend_month": 0.0,
            "daily_budget_enabled": 0.0, "conv_month": 0.0,
            "campaigns_enabled": 0, "campaigns_total": 0}


def _bump(bucket, row):
    bucket["spend_today"] += row["spend_today"]
    bucket["spend_month"] += row["spend_month"]
    bucket["conv_month"] += row["conv_month"]
    bucket["campaigns_total"] += 1
    if _is_enabled(row["status"]):
        bucket["campaigns_enabled"] += 1
        bucket["daily_budget_enabled"] += row["daily_budget"]


def _finish(bucket):
    out = {
        "currency": bucket["currency"],
        "spend_today": _money(bucket["spend_today"]),
        "spend_month": _money(bucket["spend_month"]),
        "daily_budget_enabled": _money(bucket["daily_budget_enabled"]),
        "conv_month": round(_num(bucket["conv_month"]), 4),
        "cpa_month": None,
        "campaigns_enabled": bucket["campaigns_enabled"],
        "campaigns_total": bucket["campaigns_total"],
    }
    if bucket["conv_month"] > 0:
        out["cpa_month"] = _money(bucket["spend_month"] / bucket["conv_month"])
    return out


def rollup(rows):
    """Aggregate campaign rows into by_account / by_platform / total (+warnings)."""
    if rows is None:
        rows = []
    if isinstance(rows, (str, bytes, dict)):
        raise ToolError("rows must be a list of row dicts")

    warnings, norm, bad = [], [], 0
    for raw in rows:
        row = _norm_row(raw)
        if row is None:
            bad += 1
            continue
        norm.append(row)
    if bad:
        warnings.append(f"skipped {bad} malformed row(s)")

    acct, plat, tot = {}, {}, {}
    acct_ccys, plat_ccys = {}, {}
    for row in norm:
        akey = (row["account"], row["currency"])
        pkey = (row["platform"], row["currency"])
        acct.setdefault(akey, _blank_bucket(row["currency"]))
        plat.setdefault(pkey, _blank_bucket(row["currency"]))
        tot.setdefault(row["currency"], _blank_bucket(row["currency"]))
        _bump(acct[akey], row)
        _bump(plat[pkey], row)
        _bump(tot[row["currency"]], row)
        acct_ccys.setdefault(row["account"], set()).add(row["currency"])
        plat_ccys.setdefault(row["platform"], set()).add(row["currency"])
        if _is_enabled(row["status"]) and row["daily_budget"] <= 0:
            warnings.append(f"{row['account']}/{row['campaign']}: enabled with no daily budget "
                            "(unbounded worst-case exposure)")

    for name in sorted(acct_ccys):
        ccys = acct_ccys[name]
        if len(ccys) > 1:
            warnings.append(f"account '{name}' mixes currencies {sorted(ccys)}; "
                            "kept separate, never summed")
    for name in sorted(plat_ccys):
        ccys = plat_ccys[name]
        if len(ccys) > 1:
            warnings.append(f"platform '{name}' mixes currencies {sorted(ccys)}; kept separate")

    # Mixed-currency entity -> suffix every key with the currency so nothing is
    # ever silently summed across currencies (the plain key no longer exists).
    by_account = {}
    for (name, ccy) in sorted(acct):
        key = name if len(acct_ccys[name]) == 1 else f"{name} [{ccy}]"
        by_account[key] = _finish(acct[(name, ccy)])
    by_platform = {}
    for (name, ccy) in sorted(plat):
        key = name if len(plat_ccys[name]) == 1 else f"{name} [{ccy}]"
        by_platform[key] = _finish(plat[(name, ccy)])

    if len(tot) <= 1:
        total = _finish(next(iter(tot.values()))) if tot else _finish(_blank_bucket("USD"))
    else:
        total = {ccy: _finish(bucket) for ccy, bucket in sorted(tot.items())}

    return {"by_account": by_account, "by_platform": by_platform, "total": total,
            "currencies": sorted(tot), "warnings": warnings}


# --------------------------------------------------------------------------- #
# aggregate cap
# --------------------------------------------------------------------------- #
def _daily_exposure(rows, factor):
    return sum(r["daily_budget"] for r in rows if _is_enabled(r["status"])) * factor


def _account_exposure(rows, factor):
    per = {}
    for r in rows:
        if _is_enabled(r["status"]):
            per[r["account"]] = per.get(r["account"], 0.0) + r["daily_budget"]
    return {k: v * factor for k, v in per.items()}


def _apply_proposed(rows, proposed, default_currency="USD"):
    """Return a copy of rows with proposals applied (unknown campaigns are added)."""
    out = [dict(r) for r in rows]
    by_pair, by_camp = {}, {}
    for i, r in enumerate(out):
        by_pair.setdefault((r["account"], r["campaign"]), []).append(i)
        by_camp.setdefault(r["campaign"], []).append(i)
    for prop in proposed or []:
        if not isinstance(prop, dict):
            continue
        acct, camp = _s(prop.get("account")), _s(prop.get("campaign"))
        status, budget = prop.get("status"), prop.get("daily_budget")
        targets = by_pair.get((acct, camp)) or by_camp.get(camp)  # account-less match -> campaign
        if targets:
            for i in targets:
                if status is not None:
                    out[i]["status"] = _s(status)
                if budget is not None:
                    out[i]["daily_budget"] = _num(budget)
        else:
            new = {"account": acct or "unknown",
                   "platform": _s(prop.get("platform")).lower() or "unknown",
                   "campaign": camp,
                   "status": _s(status) if status is not None else "enabled",
                   "daily_budget": _num(budget), "spend_today": 0.0, "spend_month": 0.0,
                   "conv_month": 0.0,
                   "currency": (_s(prop.get("currency")) or default_currency).upper()}
            out.append(new)
            by_pair.setdefault((new["account"], camp), []).append(len(out) - 1)
            by_camp.setdefault(camp, []).append(len(out) - 1)
    return out


def aggregate_check(rows, caps, proposed=None):
    """Worst-case aggregate spend check. Never blocks a proposal that lowers exposure."""
    if caps is None:
        caps = {}
    if not isinstance(caps, dict):
        raise ToolError("caps must be a mapping")

    # worst-case multiplier: platforms may deliver up to Nx the daily budget
    factor = _num(caps.get("overspend_factor"), 2.0)
    if factor <= 0:
        factor = 2.0

    norm = [n for n in (_norm_row(r) for r in (rows or [])) if n is not None]
    currencies = sorted({r["currency"] for r in norm})
    default_ccy = currencies[0] if len(currencies) == 1 else "USD"

    base_daily = _daily_exposure(norm, factor)
    post = _apply_proposed(norm, proposed, default_ccy)
    post_daily = _daily_exposure(post, factor)
    month_spend = sum(r["spend_month"] for r in post)
    acc_exp = _account_exposure(post, factor)

    violations = []
    if len(currencies) > 1:
        violations.append({"scope": "currency", "limit": None, "exposure": None,
                           "detail": f"mixed currencies {currencies}; aggregate caps "
                                     "cannot be summed safely"})
    if caps.get("daily_total") is not None:
        limit = _num(caps["daily_total"])
        if post_daily > limit + 1e-9:
            violations.append({
                "scope": "daily_total", "limit": _money(limit), "exposure": _money(post_daily),
                "detail": f"worst-case daily exposure {post_daily:.2f} exceeds cap "
                          f"{limit:.2f} ({factor:g}x enabled budgets)"})
    if caps.get("month_total") is not None:
        limit = _num(caps["month_total"])
        if month_spend > limit + 1e-9:
            violations.append({
                "scope": "month_total", "limit": _money(limit), "exposure": _money(month_spend),
                "detail": f"month-to-date spend {month_spend:.2f} exceeds cap {limit:.2f}"})

    per = caps.get("per_account") or {}
    if not isinstance(per, dict):
        raise ToolError("caps['per_account'] must be a mapping")
    for acct in sorted(per):
        spec = per[acct]
        limit = spec.get("daily") if isinstance(spec, dict) else spec
        if limit is None:
            continue
        limit = _num(limit)
        exposure = acc_exp.get(acct, 0.0)
        if exposure > limit + 1e-9:
            violations.append({
                "scope": f"per_account:{acct}", "limit": _money(limit),
                "exposure": _money(exposure),
                "detail": f"account '{acct}' worst-case daily exposure {exposure:.2f} "
                          f"exceeds cap {limit:.2f}"})

    # tightest remaining room across every configured cap; negative => already over
    candidates = []
    if caps.get("daily_total") is not None:
        candidates.append(_num(caps["daily_total"]) - post_daily)
    for acct in per:
        spec = per[acct]
        limit = spec.get("daily") if isinstance(spec, dict) else spec
        if limit is not None:
            candidates.append(_num(limit) - acc_exp.get(acct, 0.0))
    headroom = round(min(candidates), 2) if candidates else None

    lowering = post_daily < base_daily - 1e-9
    ok = (not violations) or (bool(proposed) and lowering)
    return {"ok": ok, "violations": violations, "headroom_daily": headroom}


def enforce(rows, caps, proposed):
    """Raise RailViolation on the first blocking violation; else return the check."""
    result = aggregate_check(rows, caps, proposed)
    if not result["ok"]:
        first = result["violations"][0] if result["violations"] else {"detail": "aggregate cap violated"}
        raise RailViolation(scrub(f"aggregate spend cap refused: {first['detail']}", 500))
    return result


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def selftest() -> int:
    checks = 0

    def eq(actual, expected, msg=""):
        nonlocal checks
        checks += 1
        assert actual == expected, f"{msg}: {actual!r} != {expected!r}"

    def okk(cond, msg=""):
        nonlocal checks
        checks += 1
        assert cond, msg

    rows = [
        {"account": "A1", "platform": "google", "campaign": "c1", "status": "ENABLED",
         "spend_today": 10.0, "spend_month": 100.0, "daily_budget": 50.0, "conv_month": 4.0,
         "currency": "usd"},
        {"account": "A1", "platform": "google", "campaign": "c2", "status": "PAUSED",
         "spend_today": 2.5, "spend_month": 20.0, "daily_budget": 30.0, "conv_month": "2",
         "currency": "USD"},
        {"account": "A2", "platform": "meta", "campaign": "m1", "status": "enabled",
         "spend_today": "5.5", "spend_month": "40", "daily_budget": "10", "conv_month": 0,
         "currency": "USD"},
    ]

    # 1. empty fleet
    r0 = rollup([])
    eq(r0["by_account"], {}, "empty by_account")
    eq(r0["currencies"], [], "empty currencies")
    eq(r0["total"]["spend_month"], 0.0, "empty total")

    # 2. nested Google Ads REST row -> canonical shape
    nested = {"campaign": {"id": "1", "name": "g1", "status": "ENABLED",
                           "campaignBudget": {"amountMicros": "5000000"}},
              "customer": {"descriptiveName": "G-Acct", "currencyCode": "USD"},
              "metrics": {"costMicros": "2500000", "conversions": 1.0}}
    n = _norm_row(nested)
    eq(n["account"], "G-Acct", "nested account")
    eq(n["campaign"], "g1", "nested campaign")
    eq(n["daily_budget"], 5.0, "micros budget")
    eq(n["spend_month"], 2.5, "micros spend")
    eq(n["platform"], "google", "nested platform")
    eq(n["status"], "ENABLED", "nested status")

    # 3. numeric coercion edges
    eq(_num("2500000"), 2500000.0, "numeric string")
    eq(_num("$1,000.50"), 1000.5, "currency string")
    eq(_num(None), 0.0, "none numeric")
    eq(_num("abc", -1.0), -1.0, "garbage numeric")

    # 4. rollup happy path
    r1 = rollup(rows)
    eq(r1["by_account"]["A1"]["spend_today"], 12.5, "A1 spend_today")
    eq(r1["by_account"]["A1"]["daily_budget_enabled"], 50.0, "A1 enabled budget")
    eq(r1["by_account"]["A1"]["cpa_month"], 20.0, "A1 cpa")
    eq(r1["by_account"]["A2"]["cpa_month"], None, "A2 cpa none")
    eq(r1["by_platform"]["google"]["campaigns_total"], 2, "google campaigns")
    eq(r1["total"]["spend_month"], 160.0, "total spend_month")
    eq(r1["currencies"], ["USD"], "single currency")

    # 5. mixed currency is split, never summed (plain account key disappears)
    mixed = rows + [{"account": "A1", "platform": "google", "campaign": "c3",
                     "status": "ENABLED", "spend_today": 1.0, "spend_month": 3.0,
                     "daily_budget": 7.0, "conv_month": 1.0, "currency": "EUR"}]
    rm = rollup(mixed)
    okk("A1 [EUR]" in rm["by_account"] and "A1 [USD]" in rm["by_account"], "split currency keys")
    okk("A1" not in rm["by_account"], "no cross-currency merged account key")
    eq(rm["by_account"]["A1 [USD]"]["spend_today"], 12.5, "A1 USD split spend")
    okk(any("mixes currencies" in w for w in rm["warnings"]), "mixed currency warning")
    eq(rm["total"]["EUR"]["spend_month"], 3.0, "EUR total")
    eq(rm["total"]["USD"]["spend_month"], 160.0, "USD total untouched")

    # 6. malformed rows skipped with a warning
    rbad = rollup([None, "x", {"account": "A9"}])
    okk(any("skipped 2" in w for w in rbad["warnings"]), "malformed warning")
    eq(rbad["by_account"]["A9"]["campaigns_total"], 1, "sparse row kept")
    bad_input_raised = False
    try:
        rollup({"account": "A1"})
    except ToolError:
        bad_input_raised = True
    okk(bad_input_raised, "dict input raises ToolError")

    # 7. aggregate_check happy path (enabled budgets 60 * 2.0 = 120)
    caps = {"daily_total": 200.0}
    chk = aggregate_check(rows, caps)
    eq(chk["ok"], True, "under cap ok")
    eq(chk["headroom_daily"], 80.0, "headroom")
    eq(chk["violations"], [], "no violations")

    # 8. proposal that raises exposure over the cap is refused
    raise_p = [{"account": "A1", "campaign": "c1", "daily_budget": 100.0}]
    chk2 = aggregate_check(rows, caps, raise_p)
    eq(chk2["ok"], False, "raising proposal refused")
    eq(chk2["violations"][0]["scope"], "daily_total", "violation scope")
    eq(chk2["headroom_daily"], -20.0, "negative headroom")

    # 9. already over cap, but a lowering proposal is always ok
    tight = {"daily_total": 50.0}
    eq(aggregate_check(rows, tight)["ok"], False, "no proposal over cap")
    chk4 = aggregate_check(rows, tight, [{"account": "A1", "campaign": "c1", "daily_budget": 40.0}])
    eq(chk4["ok"], True, "lowering proposal always ok")
    eq(len(chk4["violations"]), 1, "violations still reported")

    # 10. per-account and month caps
    chk5 = aggregate_check(rows, {"per_account": {"A1": {"daily": 99.0}}})
    eq(chk5["violations"][0]["scope"], "per_account:A1", "per-account scope")
    chk6 = aggregate_check(rows, {"month_total": 100.0})
    eq(chk6["violations"][0]["scope"], "month_total", "month scope")

    # 11. overspend factor is configurable
    chk7 = aggregate_check(rows, {"daily_total": 200.0, "overspend_factor": 1.0})
    eq(chk7["headroom_daily"], 140.0, "factor 1.0 headroom")

    # 12. unknown campaign in a proposal becomes new exposure
    chk8 = aggregate_check(rows, caps, [{"account": "A3", "campaign": "new",
                                         "daily_budget": 100.0, "status": "ENABLED"}])
    eq(chk8["ok"], False, "new campaign exposure")

    # 13. mixed currencies block aggregate caps
    mix = rows + [{"account": "B1", "platform": "meta", "campaign": "b1", "status": "ENABLED",
                   "spend_today": 0, "spend_month": 0, "daily_budget": 5, "conv_month": 0,
                   "currency": "EUR"}]
    chk9 = aggregate_check(mix, {"daily_total": 1000.0})
    okk(any(v["scope"] == "currency" for v in chk9["violations"]), "currency violation")

    # 14. no caps -> ok, headroom unknown
    chk10 = aggregate_check(rows, {})
    eq(chk10["ok"], True, "no caps ok")
    eq(chk10["headroom_daily"], None, "no caps headroom")

    # 15. enforce
    raised = False
    try:
        enforce(rows, caps, raise_p)
    except RailViolation as exc:
        raised = "refused" in str(exc)
    eq(raised, True, "enforce raises RailViolation")
    res = enforce(rows, caps, [{"account": "A1", "campaign": "c1", "daily_budget": 20.0}])
    eq(res["ok"], True, "enforce allows reductions")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
