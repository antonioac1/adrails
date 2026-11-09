"""Measure the causal impact of one ad-account change (budget, bid, page, pause).

Compares what actually happened after a change with a counterfactual built from
untouched control series (OLS on the pre-period) or the pre-period day-of-week
pattern.  A residual bootstrap gives a 90% interval; a placebo test replays fake
changes inside the pre-period to estimate how often an effect this large appears
by chance.  Everything is pure, deterministic given ``seed``, and stdlib-only.
Tools should call ``measure`` or ``measure_ratio`` and surface the returned dict;
bad input raises ``ToolError`` and no secrets ever reach the output.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from ..errors import ToolError, scrub

# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
PLACEBO_MAX = 0.05


def _g(row, *path, default=None):
    """Nested dict getter for Google Ads REST rows."""
    cur = row
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def _coerce_float(v, what="value"):
    if v is None:
        return None
    if isinstance(v, bool):
        raise ToolError(scrub(f"bad {what}: {v!r}"))
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ToolError(scrub(f"bad {what}: {v!r}"))


def _day(s):
    if isinstance(s, date):
        return s
    if not isinstance(s, str):
        raise ToolError(scrub(f"bad day: {s!r}"))
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        raise ToolError(scrub(f"bad day: {s!r}"))


def _int_in(name, v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
        raise ToolError(scrub(f"{name} must be an integer in {lo}..{hi}"))
    return v


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def _pct(sorted_vals, q):
    """Linear-interpolated percentile, q in 0..100."""
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * q / 100.0
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _series_map(series, what="series"):
    out = {}
    for row in series or []:
        if not isinstance(row, dict):
            raise ToolError(scrub(f"bad {what} row: {row!r}"))
        d = row.get("day")
        if d is None:
            continue
        v = row.get("value")
        if v is None:
            continue
        out[_day(d).isoformat()] = _coerce_float(v, "value")
    return out


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _solve(A, b):
    """Gaussian elimination with partial pivoting."""
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            continue
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] / pv
            if f == 0:
                continue
            for c in range(col, n + 1):
                M[r][c] -= f * M[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        if abs(M[i][i]) < 1e-14:
            x[i] = 0.0
            continue
        x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return x


# --------------------------------------------------------------------------- #
# alignment, model fit, prediction
# --------------------------------------------------------------------------- #
def align(treated, controls=None):
    """Return (days, y, X) on days where treated and every control are present."""
    tmap = _series_map(treated, "treated")
    cmaps = [_series_map(c, "control") for c in (controls or [])]
    days = sorted(d for d in tmap if all(d in cm for cm in cmaps))
    y = [tmap[d] for d in days]
    X = [[cm[d] for cm in cmaps] for d in days]
    return days, y, X


def fit(y_pre, X_pre, days_pre=None):
    """Fit OLS (controls) or a weekday-mean model (no controls)."""
    y = [_coerce_float(v, "value") for v in (y_pre or [])]
    raw = [r for r in (X_pre or []) if r is not None]
    k = len(raw[0]) if raw else 0

    if k > 0 and len(raw) == len(y) and len(y) >= k + 2:
        rows = []
        for r in raw:
            if not isinstance(r, (list, tuple)):
                raise ToolError("control rows must be lists")
            rr = [_coerce_float(v, "control") for v in list(r)[:k]]
            rr += [0.0] * (k - len(rr))
            rows.append(rr)
        p = k + 1
        XtX = [[0.0] * p for _ in range(p)]
        Xty = [0.0] * p
        for r, yi in zip(rows, y):
            rv = [1.0] + r
            for i in range(p):
                Xty[i] += rv[i] * yi
                for j in range(p):
                    XtX[i][j] += rv[i] * rv[j]
        trace = sum(XtX[i][i] for i in range(1, p))
        ridge = max(1e-9, 1e-6 * trace / k)
        for i in range(1, p):
            XtX[i][i] += ridge
        coef = _solve(XtX, Xty)
        preds = [sum(c * v for c, v in zip(coef, [1.0] + r)) for r in rows]
        resid = [a - b for a, b in zip(y, preds)]
        n = len(y)
        sd = math.sqrt(sum(r * r for r in resid) / (n - 1)) if n > 1 else 0.0
        sd = max(sd, 1e-9)
        my = sum(y) / n
        ss_res = sum(r * r for r in resid)
        ss_tot = sum((v - my) ** 2 for v in y)
        r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else (1.0 if ss_res < 1e-12 else 0.0)
        return {"method": "controls", "coef": coef, "k": k, "resid": resid,
                "resid_sd": sd, "r2": r2}

    # seasonal fallback: weekday means, small weekdays fall back to the overall level
    if days_pre and len(days_pre) == len(y):
        dlist = [_day(d) for d in days_pre]
    else:
        dlist = [date(1970, 1, 1) + timedelta(days=i) for i in range(len(y))]
    overall = sum(y) / len(y) if y else 0.0
    groups = {}
    for d, v in zip(dlist, y):
        groups.setdefault(d.weekday(), []).append(v)
    wm = {wd: (sum(vs) / len(vs) if len(vs) >= 2 else overall) for wd, vs in groups.items()}
    preds = [wm.get(d.weekday(), overall) for d in dlist]
    resid = [a - b for a, b in zip(y, preds)]
    sd = math.sqrt(sum(r * r for r in resid) / (len(y) - 1)) if len(y) > 1 else 0.0
    return {"method": "seasonal", "weekday_means": wm, "overall": overall,
            "resid": resid, "resid_sd": max(sd, 1e-9), "r2": None}


def predict(model, days, X=None):
    """Predict values for ``days`` using a fitted model."""
    if model.get("method") == "controls":
        coef, rows = model["coef"], X or []
        out = []
        for i in range(len(days)):
            row = rows[i] if i < len(rows) and rows[i] is not None else []
            s = coef[0] if coef else 0.0
            for j, b in enumerate(coef[1:]):
                v = _coerce_float(row[j], "control") if j < len(row) and row[j] is not None else 0.0
                s += b * (v or 0.0)
            out.append(s)
        return out
    means, overall = model.get("weekday_means", {}), model.get("overall", 0.0)
    return [means.get(_day(d).weekday(), overall) for d in days]


# --------------------------------------------------------------------------- #
# public measurement
# --------------------------------------------------------------------------- #
def measure(treated, change_day, controls=None, pre_days=28, post_days=None, metric="metric",
            higher_is_better=True, sims=1000, seed=7, min_pre=14, min_post=7, _internals=False):
    """Measure the effect of a change at ``change_day`` on a daily metric."""
    cd = _day(change_day)
    pre_days = _int_in("pre_days", pre_days, 1, 365)
    if post_days is not None:
        post_days = _int_in("post_days", post_days, 1, 365)
    sims = _int_in("sims", sims, 100, 20000)
    min_pre = _int_in("min_pre", min_pre, 1, 365)
    min_post = _int_in("min_post", min_post, 1, 365)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ToolError("seed must be an integer")

    days, y, X = align(treated, controls)
    idx = {d: i for i, d in enumerate(days)}
    lo = (cd - timedelta(days=pre_days)).isoformat()
    hi = (cd + timedelta(days=post_days)).isoformat() if post_days is not None else None
    pre = [d for d in days if lo <= d < cd.isoformat()]
    post = [d for d in days if d >= cd.isoformat() and (hi is None or d < hi)]
    need = {"pre_days": min_pre, "post_days": min_post}

    if len(pre) < min_pre:
        gap = min_pre - len(pre)
        return {"status": "not_enough_history", "pre_days": len(pre), "post_days": len(post),
                "need": need, "message": f"Need {gap} more day(s) of history before "
                f"{cd.isoformat()}: at least {min_pre} pre-change days are required."}
    if len(post) < min_post:
        gap = min_post - len(post)
        return {"status": "too_early", "pre_days": len(pre), "post_days": len(post),
                "need": need, "message": f"Wait {gap} more day(s): at least {min_post} "
                f"days after the change are needed."}

    y_pre = [y[idx[d]] for d in pre]
    X_pre = [X[idx[d]] for d in pre]
    y_post = [y[idx[d]] for d in post]
    X_post = [X[idx[d]] for d in post]
    model = fit(y_pre, X_pre, pre)
    pred = predict(model, post, X_post)

    actual_total = sum(y_post)
    expected_total = sum(pred)
    effect = actual_total - expected_total
    rel = effect / expected_total if abs(expected_total) > 1e-12 else None

    # residual bootstrap interval for the total effect
    resid = list(model.get("resid") or [])
    rng = random.Random(seed)
    sims_eff, exp_sims = [], []
    # Moving-block bootstrap (blocks of up to 7 days, review 2026-11-09): daily ad metrics run in streaks, and resampling single
    # days pretended each day was independent, which made the range too narrow (19% false alarms on AR(0.6) noise vs ~10%).
    blk = max(1, min(7, len(resid) // 3)) if resid else 1
    for _ in range(sims):
        exp_sim = 0.0
        t = 0
        while t < len(pred):
            st0 = rng.randrange(len(resid) - blk + 1) if resid else 0
            for k in range(blk):
                if t >= len(pred):
                    break
                exp_sim += pred[t] + (resid[st0 + k] if resid else 0.0)
                t += 1
        exp_sims.append(exp_sim)                   # kept in draw order: measure_ratio pairs numerator/denominator draws
        sims_eff.append(actual_total - exp_sim)
    sims_eff.sort()
    effect_lo, effect_hi = _pct(sims_eff, 5), _pct(sims_eff, 95)

    # placebo: replay fake changes inside the pre-period
    placebo, placebo_tot = [], []
    if rel is not None and len(pre) >= min_pre + min_post:
        min_train = max(4, min_pre // 2)
        cand = [i for i in range(min_train, len(pre) - min_post + 1)]
        if len(cand) > 60:
            step = len(cand) / 60.0
            cand = sorted({cand[int(j * step)] for j in range(60)})
        k_ctrl = len(X_pre[0]) if X_pre and X_pre[0] is not None else 0
        for i in cand:
            tr, te = pre[:i], pre[i:i + len(post)]
            if len(tr) < max(min_train, k_ctrl + 2) or len(te) < min_post:
                continue
            m2 = fit([y[idx[d]] for d in tr], [X[idx[d]] for d in tr], tr)
            p2 = predict(m2, te, [X[idx[d]] for d in te])
            denom = sum(p2)
            if abs(denom) < 1e-12:
                continue
            act_p = sum(y[idx[d]] for d in te)
            placebo.append((act_p - denom) / denom)
            placebo_tot.append({"start": te[0], "actual": act_p, "expected": denom})
    if placebo and rel is not None:
        cnt = sum(1 for p in placebo if abs(p) >= abs(rel))
        placebo_p = cnt / len(placebo)
    else:
        placebo_p = None

    if placebo_p is not None:
        confidence = round(1 - placebo_p, 2)
    else:
        confidence = 0.9 if (effect_lo > 0 or effect_hi < 0) else 0.5

    good = (effect_lo > 0) if higher_is_better else (effect_hi < 0)
    bad = (effect_hi < 0) if higher_is_better else (effect_lo > 0)
    # placebo gate 0.05 (review 2026-11-09, measured on 120 null series x 2 noise types): 0.1 gave 11-19% false "helped/hurt";
    # 0.05 gives 7-12% while still catching a real +30% step 95-100% of the time.
    sig = placebo_p is None or placebo_p <= PLACEBO_MAX
    verdict = "helped" if good and sig else ("hurt" if bad and sig else "no_clear_effect")

    summary = _summary(metric, rel, effect_lo, effect_hi, expected_total, len(post), placebo_p, verdict)
    return {"status": "ok", "metric": metric, "change_day": cd.isoformat(),
            "method": model.get("method"), "pre_days": len(pre), "post_days": len(post),
            "actual_total": _r(actual_total), "expected_total": _r(expected_total),
            "effect": _r(effect), "effect_lo": _r(effect_lo), "effect_hi": _r(effect_hi),
            "relative_effect": _r(rel, 3), "placebo_p": _r(placebo_p, 3), "placebo_windows": len(placebo),
            "confidence": confidence, "verdict": verdict,
            "r2": _r(model.get("r2"), 3) if model.get("r2") is not None else None,
            "daily": [{"day": d, "actual": _r(y[idx[d]]), "expected": _r(p)}
                      for d, p in zip(post, pred)],
            "summary": summary,
            **({"_exp_sims": exp_sims, "_placebo": placebo_tot} if _internals else {})}


def _subject(metric):
    if not metric or metric == "metric":
        return "The metric was"
    m = str(metric)
    m = m[0].upper() + m[1:]
    return f"{m} were" if m.lower().endswith("s") else f"{m} was"


def _summary(metric, rel, lo, hi, exp, n_post, placebo_p, verdict):
    subj = _subject(metric)
    if rel is None:
        base = f"{subj} not comparable: the expected total was zero"
    else:
        pct = rel * 100.0
        word = "higher" if pct >= 0 else "lower"
        base = f"{subj} {abs(pct):.0f}% {word} than expected in the {n_post} days after the change"
        if abs(exp) > 1e-12:
            base += f" (90% range {lo / exp * 100:+.0f}% to {hi / exp * 100:+.0f}%)"
    tail = {"helped": "it likely helped", "hurt": "it likely hurt",
            "no_clear_effect": "no clear effect was detectable"}.get(verdict, "the result is inconclusive")
    if placebo_p is not None:
        if placebo_p <= 0:
            clause = "no quiet stretch before the change moved this much"
        else:
            clause = f"a change this big appears by chance in about 1 in {max(1, int(round(1 / placebo_p)))} quiet periods"
        return f"{base}; {clause}: {tail}."
    return f"{base}: {tail}."


def measure_ratio(num, den, change_day, num_controls=None, den_controls=None, lower_is_better=True, **kw):
    """Measure a cost-per-result ratio (e.g. CPL = cost / leads; lower_is_better=False for ROAS-style ratios).

    Review 2026-11-09 #1: the first version judged the ratio from its parts' significance, so a budget doubling at a constant CPL
    (cost and leads both up, significantly) got "helped"/"hurt" by sign noise. Now the RATIO itself is tested: a bootstrap interval
    of actual/expected ratio from paired numerator/denominator draws, and a placebo test on the ratio over the quiet period."""
    kw = dict(kw)
    label = str(kw.pop("metric", "cost per result"))
    m_num = measure(num, change_day, controls=num_controls, metric=label + " numerator", _internals=True, **kw)
    m_den = measure(den, change_day, controls=den_controls, metric=label + " denominator", _internals=True, **kw)
    bad = m_num if m_num.get("status") != "ok" else (m_den if m_den.get("status") != "ok" else None)
    if bad is not None:
        return {"status": bad.get("status"), "message": bad.get("message"),
                "pre_days": bad.get("pre_days"), "post_days": bad.get("post_days"),
                "need": bad.get("need")}
    sims_n, sims_d = m_num.pop("_exp_sims"), m_den.pop("_exp_sims")
    pl_n, pl_d = m_num.pop("_placebo"), m_den.pop("_placebo")

    an, en = m_num["actual_total"], m_num["expected_total"]
    ad, ed = m_den["actual_total"], m_den["expected_total"]
    ra = an / ad if abs(ad) > 1e-12 else None
    re_ = en / ed if abs(ed) > 1e-12 else None
    rc = (ra - re_) / re_ if (ra is not None and re_ is not None and abs(re_) > 1e-12) else None

    lo = hi = placebo_p = None
    n_pl = 0
    if rc is not None:
        draws = sorted(ra / (sn / sd) - 1 for sn, sd in zip(sims_n, sims_d) if abs(sd) > 1e-12 and abs(sn) > 1e-12)
        if len(draws) >= 50:
            lo, hi = _pct(draws, 5), _pct(draws, 95)
        dmap = {p["start"]: p for p in pl_d}
        rels = []
        for p in pl_n:
            q = dmap.get(p["start"])
            if q and abs(q["actual"]) > 1e-12 and abs(q["expected"]) > 1e-12 and abs(p["expected"]) > 1e-12:
                rels.append((p["actual"] / q["actual"]) / (p["expected"] / q["expected"]) - 1)
        n_pl = len(rels)
        if rels:
            placebo_p = sum(1 for r in rels if abs(r) >= abs(rc)) / len(rels)

    sig = placebo_p is None or placebo_p <= PLACEBO_MAX
    verdict = "no_clear_effect"
    if lo is not None and sig:
        good_down = hi < 0          # ratio clearly below expected
        good_up = lo > 0
        if (good_down and lower_is_better) or (good_up and not lower_is_better):
            verdict = "helped"
        elif (good_up and lower_is_better) or (good_down and not lower_is_better):
            verdict = "hurt"

    if ra is not None and re_ is not None:
        s = f"{_subject(label)} {ra:.2f} after the change vs {re_:.2f} expected"
        if rc is not None:
            s += f" ({rc * 100:+.0f}%"
            s += f", 90% range {lo * 100:+.0f}% to {hi * 100:+.0f}%)" if lo is not None else ")"
    else:
        s = f"{label} could not be computed (zero denominator)"
    tail = {"helped": "it likely helped", "hurt": "it likely hurt",
            "no_clear_effect": "no clear effect was detectable"}[verdict]
    return {"status": "ok", "ratio_actual": _r(ra, 3), "ratio_expected": _r(re_, 3),
            "ratio_change": _r(rc, 3), "ratio_change_lo": _r(lo, 3), "ratio_change_hi": _r(hi, 3),
            "placebo_p": _r(placebo_p, 3), "placebo_windows": n_pl, "verdict": verdict, "num": m_num, "den": m_den,
            "summary": f"{s}: {tail}."}


def pick_controls(candidates, treated_id, exclude_ids=(), max_controls=5, pre_series=None):
    """Pick up to ``max_controls`` candidate series most correlated with the treated pre-period."""
    if not isinstance(candidates, dict):
        raise ToolError("candidates must be a dict of id -> series")
    if max_controls is None or max_controls <= 0:
        return []
    if isinstance(exclude_ids, str):
        exclude_ids = (exclude_ids,)
    excl = {str(x) for x in (exclude_ids or ())}
    treated = pre_series if pre_series is not None else candidates.get(treated_id)
    if treated is None:
        return []
    tmap = _series_map(treated, "treated")
    scored = []
    for cid, series in candidates.items():
        sid = str(cid)
        if sid == str(treated_id) or sid in excl:
            continue
        try:
            cmap = _series_map(series, "candidate")
        except ToolError:
            continue
        ov = sorted(set(tmap) & set(cmap))
        if len(ov) < 14:
            continue
        r = _pearson([tmap[d] for d in ov], [cmap[d] for d in ov])
        if r is None or r < 0.3:
            continue
        scored.append((-r, sid, cid))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [cid for _, _, cid in scored[:max_controls]]


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
def selftest() -> int:
    import json

    checks = 0

    def ok(cond, msg):
        nonlocal checks
        if not cond:
            raise AssertionError(msg)
        checks += 1

    def mk(start, vals):
        d0 = date.fromisoformat(start)
        return [{"day": (d0 + timedelta(days=i)).isoformat(), "value": v} for i, v in enumerate(vals)]

    # helpers / alignment
    ok(_g({"a": {"b": {"c": "5"}}}, "a", "b", "c") == "5", "_g nested")
    ok(_g({}, "a", "b", default=9) == 9, "_g default")
    ok(align([], None) == ([], [], []), "empty align")
    d, y, X = align([{"day": "2024-01-01", "value": 1}, {"day": "2024-01-02", "value": None},
                     {"day": "2024-01-03", "value": 3}], None)
    ok(d == ["2024-01-01", "2024-01-03"] and y == [1.0, 3.0] and X == [[], []], "drop None gaps")
    d, y, X = align([{"day": "2024-01-01", "value": "5"}], None)
    ok(y == [5.0] and X == [[]], "coerce string values")

    # OLS recovery and ridge
    m = fit([2 + 3 * i for i in range(20)], [[float(i)] for i in range(20)])
    ok(abs(m["coef"][0] - 2) < 1e-3, "OLS intercept")
    ok(abs(m["coef"][1] - 3) < 1e-3, "OLS slope")
    mr = fit([2 + 3 * i for i in range(10)], [[float(i), float(i)] for i in range(10)])
    ok(len(mr["coef"]) == 3 and all(math.isfinite(c) for c in mr["coef"]), "ridge solvable")
    ok(abs(sum(mr["coef"][1:]) - 3) < 1e-2, "ridge splits slopes")
    ok(predict({"method": "controls", "coef": [1.0, 2.0]}, ["2024-01-01"], [[3.0]]) == [7.0], "predict controls")
    ms = fit([10.0, 10.0], [], ["2024-01-01", "2024-01-02"])
    ok(predict(ms, ["2024-01-07"]) == [10.0], "seasonal fallback")

    # synthetic controlled scenario: true +30% step
    rnd = random.Random(1)
    n, ci = 60, 40
    days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    ctrl = [100 + 5 * (i % 7) + rnd.uniform(-3, 3) for i in range(n)]
    base = [10 + 0.8 * ctrl[i] + rnd.uniform(-2, 2) for i in range(n)]
    stepped = [v * 1.3 if i >= ci else v for i, v in enumerate(base)]
    res = measure(mk("2024-01-01", stepped), days[ci], controls=[mk("2024-01-01", ctrl)],
                  metric="leads", sims=1000, seed=7)
    ok(res["status"] == "ok", "controlled status")
    ok(res["method"] == "controls", "controlled method")
    ok(res["verdict"] == "helped", "controlled verdict")
    ok(res["effect_lo"] > 0, "controlled interval excludes zero")
    ok(abs(res["relative_effect"] - 0.3) < 0.1, "controlled relative effect")
    ok(res["placebo_p"] is not None and res["placebo_p"] <= 0.1, "controlled placebo")
    ok(len(res["daily"]) == 20, "daily length")
    ok(res["effect_lo"] <= res["effect_hi"], "interval order")

    # no change
    flat = measure(mk("2024-01-01", base), days[ci], controls=[mk("2024-01-01", ctrl)],
                   metric="leads", sims=500, seed=7)
    ok(flat["status"] == "ok" and flat["verdict"] == "no_clear_effect", "no change verdict")

    # a cost drop: lower is better
    dropped = measure(mk("2024-01-01", [v * 0.7 if i >= ci else v for i, v in enumerate(base)]),
                      days[ci], controls=[mk("2024-01-01", ctrl)], metric="cost",
                      higher_is_better=False, sims=500, seed=7)
    ok(dropped["verdict"] == "helped" and dropped["effect_hi"] < 0, "lower-is-better helped")

    # seasonal weekday pattern recovers +20%
    weekly = [130, 90, 100, 110, 95, 105, 120]
    svals = []
    for i in range(n):
        v = weekly[(date(2024, 1, 1) + timedelta(days=i)).weekday()] + rnd.uniform(-2, 2)
        svals.append(v * 1.2 if i >= ci else v)
    sres = measure(mk("2024-01-01", svals), days[ci], sims=500, seed=3, metric="leads")
    ok(sres["method"] == "seasonal", "seasonal method")
    ok(abs(sres["relative_effect"] - 0.2) < 0.15, "seasonal effect")
    ok(sres["effect_lo"] > 0, "seasonal interval")

    # refusals / early returns
    early = measure(mk("2024-01-01", base[:50]), days[48], sims=200, seed=1)
    ok(early["status"] == "too_early" and "Wait" in early["message"], "too_early")
    hist = measure(mk("2024-01-01", base[:20]), days[10], sims=200, seed=1)
    ok(hist["status"] == "not_enough_history" and "history" in hist["message"], "not_enough_history")
    ok(measure([], "2024-01-10")["status"] == "not_enough_history", "empty treated")
    for bad in (lambda: measure(base, "nope"), lambda: measure(base, days[ci], pre_days=0),
                lambda: measure(base, days[ci], sims=50)):
        try:
            bad()
        except ToolError:
            ok(True, "bad input refused")
        else:
            ok(False, "bad input refused")

    # ratio: flat cost + leads +40% => CPL helped
    cost = [50 + rnd.uniform(-2, 2) for _ in range(n)]
    leads = []
    for i in range(n):
        v = 20 + 3 * (i % 7) + rnd.uniform(-1, 1)
        leads.append(v * 1.4 if i >= ci else v)
    rr = measure_ratio(mk("2024-01-01", cost), mk("2024-01-01", leads), days[ci],
                       metric="CPL", sims=400, seed=11)
    ok(rr["status"] == "ok" and rr["verdict"] == "helped", "ratio helped")
    ok(rr["ratio_actual"] is not None and rr["ratio_expected"] is not None
       and rr["ratio_actual"] < rr["ratio_expected"], "ratio direction")
    z = measure_ratio(mk("2024-01-01", cost), mk("2024-01-01", [0.0] * n), days[ci],
                      metric="CPL", sims=300, seed=11)
    ok(z["status"] == "ok" and z["ratio_actual"] is None, "zero denominator")
    wrong = 0
    for sd in range(1, 11):        # review #1: budget doubled at the SAME cost per lead is not a CPL change (was 5/5 wrong)
        rq = random.Random(sd)
        cs = [max(0.0, (50 + rq.gauss(0, 5)) * (2 if i >= 28 else 1)) for i in range(56)]
        ls = [max(0.0, (5 + rq.gauss(0, 1)) * (2 if i >= 28 else 1)) for i in range(56)]
        rv = measure_ratio(mk("2026-09-01", cs), mk("2026-09-01", ls), "2026-09-29", sims=300)
        wrong += rv["verdict"] != "no_clear_effect"
    ok(wrong <= 2, "constant CPL under a budget change misjudged %d/10 (false-alarm rate must stay near 10%%)" % wrong)

    # control picking and determinism
    tvals = [100 + rnd.uniform(-10, 10) for _ in range(40)]
    c1 = [v + rnd.uniform(-1, 1) for v in tvals]
    c2 = [float(i % 2) for i in range(40)]
    picked = pick_controls({"c1": mk("2024-01-01", c1), "c2": mk("2024-01-01", c2),
                            "c3": mk("2024-01-01", tvals), "short": mk("2024-01-01", tvals[:5])},
                           "treated", exclude_ids=("c3",), pre_series=mk("2024-01-01", tvals))
    ok("c1" in picked and "c2" not in picked and "c3" not in picked, "pick_controls")
    ok(pick_controls({}, "treated") == [], "pick empty")
    res2 = measure(mk("2024-01-01", stepped), days[ci], controls=[mk("2024-01-01", ctrl)],
                   metric="leads", sims=1000, seed=7)
    ok(json.dumps(res, sort_keys=True) == json.dumps(res2, sort_keys=True), "deterministic")

    try:
        align([{"day": "2024-01-01", "value": "oops"}], None)
    except ToolError:
        ok(True, "malformed value refused")
    else:
        ok(False, "malformed value refused")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
