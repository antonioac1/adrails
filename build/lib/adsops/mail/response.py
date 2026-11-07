"""Honest response-rate statistics for small direct-mail campaigns.

Provides Wilson intervals, grouped rate tables, two-proportion comparisons,
holdout sizing, incremental lift, and lead decay histograms. All calculations are
pure and JSON-serialisable; money/time-sensitive inputs are injected by callers.
Safety: no I/O or network at import time, no secrets in outputs, and invalid or
underpowered claims are refused or labelled rather than overstated.
Tools should pass mailing pieces and per-tracking-code outcomes, then surface the
verdict/confidence fields without converting small samples into false precision.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from adsops.errors import ToolError, RailViolation, scrub


# ---------------------------------------------------------------------------
# Small coercion / parsing helpers
# ---------------------------------------------------------------------------

def _g(row: Any, *path: str, default: Any = None) -> Any:
    """Nested dict getter used for Google-Ads-style camelCase rows."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _r(value: Any, nd: int = 5) -> Any:
    if value is None:
        return None
    out = round(float(value), nd)
    return 0.0 if out == 0 else out


def _truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return False


def _as_date(value: Any) -> date | None:
    """Best-effort ISO/date/datetime -> date. Malformed values become None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _tracking_code(piece: dict) -> Any:
    return piece.get("tracking_code", piece.get("code", piece.get("trackingCode")))


def _wilson_bounds(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    z2 = z * z
    den = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / den
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / den
    return max(0.0, center - half), min(1.0, center + half)


# ---------------------------------------------------------------------------
# Public statistics
# ---------------------------------------------------------------------------

def wilson(k: Any, n: Any, z: float = 1.96) -> list[float] | None:
    """Wilson score interval for a binomial rate, as fractions rounded to 5 dp."""
    n_int = int(_num(n, 0))
    k_float = _num(k, 0.0)
    if n_int <= 0:
        return None
    if k_float < 0 or k_float > n_int:
        raise ToolError("wilson: k must be between 0 and n")
    lo, hi = _wilson_bounds(k_float, n_int, z)
    return [_r(lo), _r(hi)]


def rate_table(
    pieces: list[dict],
    outcomes: dict,
    by: str = "batch",
    min_days: int = 42,
    today: Any = None,
) -> list[dict]:
    """Group mailed pieces and report rates with honest verdicts.

    `by` may be any piece key; missing values become "(none)". Outcomes are keyed
    by tracking code. Rates are fractions; CIs are Wilson 95% intervals.
    """
    if pieces is None:
        pieces = []
    if not isinstance(pieces, list):
        raise ToolError("rate_table: pieces must be a list")
    if outcomes is None:
        outcomes = {}
    if not isinstance(outcomes, dict):
        raise ToolError("rate_table: outcomes must be an object")
    if not isinstance(by, str) or not by:
        raise ToolError("rate_table: by must be a non-empty key")

    min_days_int = int(_num(min_days, 42))
    today_d = _as_date(today) or datetime.now(timezone.utc).date()
    groups: dict[Any, dict] = {}

    for piece in pieces:
        if not isinstance(piece, dict):
            continue
        gv = piece.get(by, "(none)")
        if gv is None:
            gv = "(none)"
        if not isinstance(gv, (str, int, float, bool)):
            gv = str(gv)
        g = groups.setdefault(
            gv,
            {"mailed": 0, "scans": 0, "leads": 0, "clients": 0, "days": None},
        )
        g["mailed"] += 1

        mailed_d = _as_date(piece.get("mailed_at"))
        if mailed_d is not None:
            days = (today_d - mailed_d).days
            g["days"] = days if g["days"] is None else min(g["days"], days)

        code = _tracking_code(piece)
        if code is None:
            continue
        try:
            outcome = outcomes.get(code)
            if outcome is None:
                outcome = outcomes.get(str(code))
        except TypeError:
            outcome = None
        if isinstance(outcome, dict):
            g["scans"] += 1 if _truth(outcome.get("scanned")) else 0
            g["leads"] += 1 if _truth(outcome.get("lead")) else 0
            g["clients"] += 1 if _truth(outcome.get("client")) else 0

    rows: list[dict] = []
    for group, g in groups.items():
        mailed = int(g["mailed"])
        leads = int(g["leads"])
        ci = wilson(leads, mailed)
        if ci is None:
            ci = [None, None]
        days_since = g["days"]

        if days_since is not None and days_since < min_days_int:
            verdict = f"too early (<{min_days_int} days)"
        elif leads == 0:
            upper = (ci[1] if ci[1] is not None else 0.0) * 100.0
            verdict = f"no signal yet: could still be up to {_r(upper, 1):.1f}%"
        elif mailed < 100:
            verdict = "n too small"
        else:
            verdict = "reading"

        rows.append(
            {
                "group": group,
                "mailed": mailed,
                "scans": int(g["scans"]),
                "leads": leads,
                "clients": int(g["clients"]),
                "scan_rate": _r(g["scans"] / mailed),
                "lead_rate": _r(leads / mailed),
                "lead_ci": ci,
                "client_rate": _r(g["clients"] / mailed),
                "days_since_mail": days_since,
                "verdict": verdict,
            }
        )
    return sorted(rows, key=lambda row: str(row["group"]))


def compare(k1: Any, n1: Any, k2: Any, n2: Any) -> dict:
    """Two-proportion z test plus Newcombe hybrid-score CI for the difference."""
    n1i = int(_num(n1, 0))
    n2i = int(_num(n2, 0))
    if n1i <= 0 or n2i <= 0:
        raise ToolError("compare: sample sizes must be positive")
    k1f = _num(k1, 0.0)
    k2f = _num(k2, 0.0)
    if k1f < 0 or k1f > n1i or k2f < 0 or k2f > n2i:
        raise ToolError("compare: counts must be within sample sizes")

    p1 = k1f / n1i
    p2 = k2f / n2i
    diff = p1 - p2

    l1, u1 = _wilson_bounds(k1f, n1i)
    l2, u2 = _wilson_bounds(k2f, n2i)
    # Newcombe (1998) method 10: hybrid score interval for p1-p2.
    lo = diff - math.hypot(p1 - l1, u2 - p2)
    hi = diff + math.hypot(u1 - p1, p2 - l2)
    lo = max(-1.0, min(1.0, lo))
    hi = max(-1.0, min(1.0, hi))

    pooled = (k1f + k2f) / (n1i + n2i)
    var = pooled * (1.0 - pooled) * (1.0 / n1i + 1.0 / n2i)
    if var <= 0:
        z = 0.0
        p_value = 1.0
    else:
        z = diff / math.sqrt(var)
        p_value = math.erfc(abs(z) / math.sqrt(2.0))
        p_value = max(0.0, min(1.0, p_value))

    return {
        "diff": _r(diff),
        "diff_ci": [_r(lo), _r(hi)],
        "z": _r(z, 4),
        "p_value": _r(p_value, 6),
        "significant": p_value < 0.05,
    }


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF via Acklam's rational approximation."""
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")

    a = (
        -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
        1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
        6.680131188771972e01, -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
        -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low, p_high = 0.02425, 1.0 - 0.02425

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
        ) / (
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def holdout_size(
    base_rate: Any,
    lift: Any,
    alpha: float = 0.05,
    power: float = 0.8,
    ratio: float = 1.0,
) -> dict:
    """Normal-approximation sample size for a two-proportion holdout test.

    p1 = base_rate * (1 + lift) is treatment, p2 = base_rate is holdout;
    ratio = treatment size / holdout size.
    """
    base = _num(base_rate, -1.0)
    lift_f = _num(lift, 0.0)
    if not (0.0 < base < 1.0):
        raise ToolError("holdout_size: base_rate must be between 0 and 1")
    if lift_f <= 0.0:
        raise ToolError("holdout_size: lift must be positive")
    if not (0.0 < alpha < 1.0):
        raise ToolError("holdout_size: alpha must be between 0 and 1")
    if not (0.0 < power < 1.0):
        raise ToolError("holdout_size: power must be between 0 and 1")
    ratio_f = _num(ratio, 0.0)
    if ratio_f <= 0.0:
        raise ToolError("holdout_size: ratio must be positive")

    p2 = base
    p1 = base * (1.0 + lift_f)
    if p1 >= 1.0:
        raise ToolError("holdout_size: treatment rate must be below 1")

    z_alpha = _norm_ppf(1.0 - alpha / 2.0)
    z_power = _norm_ppf(power)
    pooled = (ratio_f * p1 + p2) / (ratio_f + 1.0)
    n2 = (
        z_alpha * math.sqrt(pooled * (1.0 - pooled) * (ratio_f + 1.0) / ratio_f)
        + z_power * math.sqrt(p1 * (1.0 - p1) / ratio_f + p2 * (1.0 - p2))
    ) ** 2 / ((p1 - p2) ** 2)
    n1 = ratio_f * n2

    note = (
        "normal-approximation two-proportion size; "
        f"alpha={alpha}, power={power}, ratio(treatment/holdout)={_r(ratio_f, 3)}; "
        "validate assumptions before running"
    )
    return {
        "treatment": max(1, int(math.ceil(n1))),
        "holdout": max(1, int(math.ceil(n2))),
        "note": note,
    }


def incremental(treat_k: Any, treat_n: Any, hold_k: Any, hold_n: Any) -> dict:
    """Absolute/relative lift and incremental leads per 1,000 from a holdout."""
    tn = int(_num(treat_n, 0))
    hn = int(_num(hold_n, 0))
    cmp = compare(treat_k, tn, hold_k, hn)
    tr = _num(treat_k, 0.0) / tn
    hr = _num(hold_k, 0.0) / hn
    raw_abs = tr - hr
    return {
        "lift_abs": _r(raw_abs),
        "lift_rel": None if hr == 0 else _r(raw_abs / hr),
        "incremental_leads_per_1000": _r(raw_abs * 1000.0, 2),
        "ci": cmp["diff_ci"],
    }


def decay(outcomes: dict, pieces: list[dict], bucket_days: int = 7) -> list[dict]:
    """Histogram of days from mailed_at to lead_at for lead outcomes."""
    if outcomes is None:
        outcomes = {}
    if not isinstance(outcomes, dict):
        raise ToolError("decay: outcomes must be an object")
    if pieces is None:
        pieces = []
    if not isinstance(pieces, list):
        raise ToolError("decay: pieces must be a list")
    bd = int(_num(bucket_days, 0))
    if bd <= 0:
        raise ToolError("decay: bucket_days must be positive")

    mailed: dict[str, date] = {}
    for piece in pieces:
        if not isinstance(piece, dict):
            continue
        code = _tracking_code(piece)
        mailed_d = _as_date(piece.get("mailed_at"))
        if code is not None and mailed_d is not None:
            mailed[str(code)] = mailed_d

    counts: dict[int, int] = {}
    for code, outcome in outcomes.items():
        if not isinstance(outcome, dict) or not _truth(outcome.get("lead")):
            continue
        lead_d = _as_date(outcome.get("lead_at"))
        mailed_d = mailed.get(str(code))
        if lead_d is None or mailed_d is None:
            continue
        days = (lead_d - mailed_d).days
        if days < 0:  # ignore impossible pre-mailing "leads"
            continue
        idx = days // bd
        counts[idx] = counts.get(idx, 0) + 1

    if not counts:
        return []
    total = sum(counts.values())
    rows: list[dict] = []
    cumulative = 0
    for idx in range(0, max(counts) + 1):
        leads = counts.get(idx, 0)
        cumulative += leads
        rows.append(
            {
                "days_from": idx * bd,
                "days_to": idx * bd + bd - 1,
                "leads": leads,
                "cum_share": _r(cumulative / total),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Self-test: all effects mocked / deterministic
# ---------------------------------------------------------------------------

def selftest() -> int:
    checks = 0

    def ok(cond: Any, msg: str) -> None:
        nonlocal checks
        assert cond, msg
        checks += 1

    # Inverse normal and Wilson boundaries.
    ok(abs(_norm_ppf(0.975) - 1.959964) < 1e-4, "norm ppf")
    ok(wilson(0, 0) is None, "wilson empty")
    w0 = wilson(0, 10)
    ok(w0 is not None and w0[0] == 0.0 and 0.2 < w0[1] < 0.3, "wilson zero")
    w1 = wilson(10, 10)
    ok(w1 is not None and w1[1] == 1.0 and 0.7 < w1[0] < 0.8, "wilson all")
    try:
        wilson(11, 10)
        raise AssertionError("wilson validation")
    except ToolError:
        checks += 1

    # Rate table: grouping, malformed/missing keys, and verdicts.
    pieces = [
        {"tracking_code": "a", "mailed_at": "2024-01-01", "batch": "B1"},
        {"tracking_code": "b", "mailed_at": "2024-01-02", "batch": "B1"},
        {"tracking_code": "c", "mailed_at": "2024-01-03", "batch": "B2"},
        {"tracking_code": "d", "mailed_at": "2024-01-04"},
    ]
    outcomes = {
        "a": {"scanned": True, "lead": True, "client": False, "lead_at": "2024-01-09"},
        "b": {"scanned": "true", "lead": False},
        "c": {"lead": False},
    }
    early = rate_table(pieces, outcomes, by="batch", min_days=42, today=date(2024, 1, 15))
    by_group = {row["group"]: row for row in early}
    ok(by_group["B1"]["verdict"].startswith("too early"), "too early verdict")
    rows = rate_table(pieces, outcomes, by="batch", min_days=30, today=date(2024, 2, 15))
    by_group = {row["group"]: row for row in rows}
    ok(by_group["B1"]["mailed"] == 2, "mailed count")
    ok(by_group["B1"]["scans"] == 2, "scan count")
    ok(by_group["B1"]["leads"] == 1, "lead count")
    ok(by_group["B1"]["verdict"] == "n too small", "n too small verdict")
    ok(by_group["B2"]["verdict"].startswith("no signal yet"), "no signal verdict")
    ok(by_group["(none)"]["mailed"] == 1, "missing group")
    ok(len(by_group["B1"]["lead_ci"]) == 2, "lead ci shape")
    missing_by = rate_table(pieces, outcomes, by="segment", min_days=0, today=date(2024, 2, 15))
    ok(len(missing_by) == 1 and missing_by[0]["group"] == "(none)", "missing by")
    ok(missing_by[0]["mailed"] == 4, "missing by denominator")

    many = [
        {"tracking_code": str(i), "mailed_at": "2024-01-01", "batch": "B1"}
        for i in range(100)
    ]
    reading = rate_table(many, {"0": {"lead": True}}, by="batch", min_days=42, today=date(2024, 3, 1))
    ok(reading[0]["verdict"] == "reading", "reading verdict")
    ok(rate_table([], {}, by="batch", today=date(2024, 1, 1)) == [], "empty rate table")

    # Two-proportion comparison and Newcombe interval.
    c = compare(60, 100, 40, 100)
    ok(abs(c["diff"] - 0.2) < 1e-9, "compare diff")
    ok(c["diff_ci"][0] > 0, "compare ci lower")
    ok(c["significant"] is True, "compare significant")
    ok(0.0 <= c["p_value"] < 0.05, "compare p")
    c2 = compare(10, 100, 5, 100)
    ok(abs(c2["diff"] - 0.05) < 1e-9, "compare small diff")
    ok(c2["significant"] is False, "compare not significant")
    ok(compare(0, 1, 0, 1)["p_value"] == 1.0, "compare zero variance")
    try:
        compare(1, 0, 0, 1)
        raise AssertionError("compare validation")
    except ToolError:
        checks += 1

    # Holdout sizing and validation.
    h = holdout_size(0.01, 0.5)
    ok(h["treatment"] > 0 and h["holdout"] > 0, "holdout positive")
    ok("normal" in h["note"], "holdout note")
    h2 = holdout_size(0.01, 0.5, ratio=2.0)
    ok(h2["treatment"] > h2["holdout"], "holdout ratio")
    for args in ((0, 0.5), (0.5, 0), (0.9, 0.5)):
        try:
            holdout_size(*args)
            raise AssertionError("holdout validation")
        except ToolError:
            checks += 1

    # Incremental lift.
    inc = incremental(60, 1000, 40, 1000)
    ok(abs(inc["lift_abs"] - 0.02) < 1e-9, "incremental abs")
    ok(abs(inc["lift_rel"] - 0.5) < 1e-9, "incremental rel")
    ok(abs(inc["incremental_leads_per_1000"] - 20.0) < 1e-9, "incremental per 1000")
    ok(len(inc["ci"]) == 2, "incremental ci")
    inc0 = incremental(5, 100, 0, 100)
    ok(inc0["lift_rel"] is None, "incremental zero holdout")

    # Decay histogram.
    pieces_d = [
        {"tracking_code": "a", "mailed_at": "2024-01-01"},
        {"tracking_code": "b", "mailed_at": "2024-01-01"},
        {"tracking_code": "c", "mailed_at": "2024-01-01"},
    ]
    out_d = {
        "a": {"lead": True, "lead_at": "2024-01-02"},
        "b": {"lead": True, "lead_at": "2024-01-10"},
        "c": {"lead": False},
        "d": {"lead": True, "lead_at": "2024-01-02"},
    }
    d = decay(out_d, pieces_d, bucket_days=7)
    ok(len(d) == 2, "decay bucket count")
    ok(d[0]["days_from"] == 0 and d[0]["days_to"] == 6 and d[0]["leads"] == 1, "decay first")
    ok(d[1]["leads"] == 1 and abs(d[1]["cum_share"] - 1.0) < 1e-9, "decay cumulative")
    ok(decay({}, pieces_d) == [], "decay empty")
    try:
        decay(out_d, pieces_d, bucket_days=0)
        raise AssertionError("decay validation")
    except ToolError:
        checks += 1

    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
