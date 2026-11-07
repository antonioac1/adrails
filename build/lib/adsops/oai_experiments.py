"""A/B test reports for ChatGPT Ads / any ad set, with honest statistics.

Pure and side-effect free: no I/O, no globals mutated. Tools call ab_report()
with flat or Google-Ads-style nested rows to get a leaderboard, a leader among
qualified ads, and Bonferroni-corrected significance for every
leader-vs-challenger comparison. sample_needed() sizes a test up front. All math
has division-by-zero guards; secrets never reach outputs.
wilson/compare/holdout_size come from adsops.mail.response and are injectable
for tests.
"""

import json
import math
import sys

from adsops.errors import ToolError, RailViolation, scrub  # noqa: F401  rail helpers available
from adsops.mail.response import wilson, compare, holdout_size

__all__ = ["ab_report", "sample_needed", "selftest"]


# --------------------------------------------------------------------------- helpers

def _g(row, *path, default=None):
    """Nested get for Google-Ads-style dicts; returns default on any miss."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return default if cur is None else cur


def _num(v, default=0.0):
    """Coerce possibly-string numerics; missing/garbage -> default."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _r(x, nd=6):
    """Round to nd dp, preserving None."""
    return None if x is None else round(float(x), nd)


def _as_ci(v):
    """Normalise a CI (tuple/list/dict) to a rounded [lo, hi] list or None."""
    if v is None:
        return None
    if isinstance(v, dict):
        lo = v.get("lo", v.get("low", v.get("lower")))
        hi = v.get("hi", v.get("high", v.get("upper")))
    else:
        try:
            lo, hi = v[0], v[1]
        except (TypeError, IndexError, KeyError):
            return None
    try:
        return [round(float(lo), 6), round(float(hi), 6)]
    except (TypeError, ValueError):
        return None


def _wilson(k, n):
    """Wilson interval as [lo, hi], or None when there is no denominator."""
    if n is None or n <= 0:
        return None
    try:
        return _as_ci(wilson(int(k), int(n)))
    except Exception:
        return None


def _safe_compare(k1, n1, k2, n2):
    """compare() with defensive wrapping so a stats hiccup never crashes a report."""
    try:
        c = compare(int(k1), int(n1), int(k2), int(n2))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _counts(row, metric):
    """Return (k, n) feeding compare() for the chosen proportion metric."""
    if metric == "cvr":
        return int(round(row["conversions"])), int(row["clicks"])
    return int(row["clicks"]), int(row["impressions"])


def _is_json(x):
    try:
        json.dumps(x)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- report

def ab_report(ads, metric="ctr", min_impressions=1000, min_clicks=30, alpha=0.05):
    """Build an honest A/B leaderboard with Bonferroni-corrected significance.

    metric is 'ctr' (clicks/impressions) or 'cvr' (conversions/clicks).
    Leader is the highest point estimate among ads meeting the minimums.
    When fewer than 2 ads meet the minimums, the verdict is 'too early' and
    comparisons are empty; if no ads meet the minimums, leader is None.
    """
    metric = (metric or "ctr").strip().lower()
    if metric not in ("ctr", "cvr"):
        raise ToolError("metric must be 'ctr' or 'cvr'")
    try:
        a_alpha = float(alpha)
    except (TypeError, ValueError):
        raise ToolError("alpha must be a number in (0, 1)")
    if not (0.0 < a_alpha < 1.0):
        raise ToolError("alpha must be in (0, 1)")
    min_impressions = max(0, int(min_impressions))
    min_clicks = max(0, int(min_clicks))

    rows = []
    qualified = []
    for ad in ads or []:
        if not isinstance(ad, dict):
            continue
        aid = _g(ad, "id")
        if aid is None:
            continue  # rows without an id cannot be compared or paused safely
        imp = _num(_g(ad, "impressions"))
        clk = _num(_g(ad, "clicks"))
        conv = _num(_g(ad, "conversions"))
        spend = _num(_g(ad, "spend"))
        iimp, iclk = int(imp), int(clk)
        ctr = iclk / imp if imp > 0 else None
        cvr = conv / clk if clk > 0 else None
        cpc = spend / clk if clk > 0 else None
        cpa = spend / conv if conv > 0 else None
        row = {
            "id": aid,
            "name": _g(ad, "name", default="") or "",
            "impressions": iimp,
            "clicks": iclk,
            "conversions": round(conv, 4),
            "spend": round(spend, 2),
            "ctr": _r(ctr),
            "ctr_ci": _wilson(iclk, iimp) if imp > 0 else None,
            "cvr": _r(cvr),
            "cvr_ci": _wilson(int(round(conv)), iclk) if clk > 0 else None,
            "cpc": _r(cpc, 2),
            "cpa": _r(cpa, 2),
        }
        rows.append(row)
        val = row[metric]
        if val is not None and iimp >= min_impressions and iclk >= min_clicks:
            qualified.append(row)

    # Deterministic ordering: best estimate first, ties broken by id.
    qualified.sort(key=lambda r: (-(r[metric] if r[metric] is not None else 0.0), str(r["id"])))

    comparisons = []
    recommend_pause = []
    if not qualified:
        leader = None
        verdict = ("too early: no ads meet the minimums "
                   f"(min_impressions={min_impressions}, min_clicks={min_clicks})")
    else:
        leader_row = qualified[0]
        leader = leader_row["id"]
        if len(qualified) < 2:
            verdict = ("too early: fewer than 2 ads meet the minimums "
                       f"(min_impressions={min_impressions}, min_clicks={min_clicks})")
        else:
            others = qualified[1:]
            bonf = a_alpha / len(others)  # Bonferroni across leader-vs-challenger tests
            for other in others:
                c = _safe_compare(*_counts(leader_row, metric), *_counts(other, metric))
                p = _num(c.get("p_value"), default=None)
                diff = _num(c.get("diff"), default=None)
                sig = bool(p is not None and p < bonf)
                comparisons.append({
                    "id": other["id"],
                    "vs": leader,
                    "diff": _r(diff),
                    "diff_ci": _as_ci(c.get("diff_ci")),
                    "p_value": _r(p),
                    "significant": sig,
                })
                # diff = leader_rate - challenger_rate; >0 and significant -> worse.
                if sig and diff is not None and diff > 0:
                    recommend_pause.append(other["id"])
            label = leader_row["name"] or str(leader)
            if recommend_pause:
                verdict = (f"{label} leads on {metric}; {len(recommend_pause)} challenger(s) "
                           f"significantly worse at Bonferroni alpha={bonf:.4g} -> recommend pausing "
                           + ", ".join(str(x) for x in recommend_pause))
            else:
                verdict = (f"{label} leads on {metric}; no challenger is significantly worse "
                           f"at Bonferroni alpha={bonf:.4g} (needs a bigger sample)")

    return {
        "metric": metric,
        "rows": rows,
        "leader": leader,
        "comparisons": comparisons,
        "verdict": verdict,
        "recommend_pause": recommend_pause,
    }


def sample_needed(base_ctr, lift, alpha=0.05, power=0.8):
    """Impressions per ad needed to detect a relative `lift` over `base_ctr`."""
    base = _num(base_ctr, default=None)
    lift = _num(lift, default=None)
    if base is None or not (0.0 < base < 1.0):
        raise ToolError("base_ctr must be a proportion in (0, 1)")
    if lift is None or lift <= 0:
        raise ToolError("lift must be a positive relative change (e.g. 0.1 = +10%)")
    try:
        a_alpha, a_power = float(alpha), float(power)
    except (TypeError, ValueError):
        raise ToolError("alpha and power must be numbers in (0, 1)")
    if not (0.0 < a_alpha < 1.0):
        raise ToolError("alpha must be in (0, 1)")
    if not (0.0 < a_power < 1.0):
        raise ToolError("power must be in (0, 1)")
    try:
        raw = holdout_size(float(base), float(lift), a_alpha, a_power)
    except Exception as exc:  # surface a clean, secret-free message
        raise ToolError("sample size calculation failed: " + scrub(str(exc))) from None
    n = _num(raw, default=None)
    if n is None:
        raise ToolError("sample size calculation returned no value")
    return int(math.ceil(n))


# --------------------------------------------------------------------------- selftest

def _fake_wilson(k, n):
    if n <= 0:
        return (0.0, 0.0)
    rate = k / n
    half = 1.0 / math.sqrt(n)
    return (max(0.0, rate - half), min(1.0, rate + half))


def _fake_compare(k1, n1, k2, n2):
    """Real two-proportion z-test mock so significance assertions are meaningful."""
    p1 = k1 / n1 if n1 else 0.0
    p2 = k2 / n2 if n2 else 0.0
    diff = p1 - p2
    pbar = (k1 + k2) / (n1 + n2) if (n1 + n2) else 0.0
    se = math.sqrt(pbar * (1 - pbar) * (1 / n1 + 1 / n2)) if n1 and n2 and 0 < pbar < 1 else 0.0
    z = diff / se if se else 0.0
    pval = math.erfc(abs(z) / math.sqrt(2)) if se else 1.0
    return {"diff": diff, "diff_ci": (diff - 0.01, diff + 0.01),
            "z": z, "p_value": pval, "significant": pval < 0.05}


def selftest() -> int:
    me = sys.modules[__name__]
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        assert cond, msg
        checks += 1

    calls = {}
    saved = (me.wilson, me.compare, me.holdout_size)
    me.wilson = _fake_wilson
    me.compare = _fake_compare
    me.holdout_size = lambda b, l, a, p: (calls.update(base=b, lift=l, alpha=a, power=p), 1234.4)[1]
    try:
        # 1. Empty input is well-formed and honest.
        rep = ab_report([])
        ok(rep["leader"] is None and rep["rows"] == [] and rep["comparisons"] == [], "empty report")
        ok("too early" in rep["verdict"], "empty verdict too early")
        ok(_is_json(rep), "empty report json")

        # 2. A single qualifying ad -> too early, but it is the leader among qualified.
        rep = ab_report([{"id": "a", "name": "A", "impressions": 5000,
                          "clicks": 500, "conversions": 50, "spend": 100}])
        ok(rep["leader"] == "a" and rep["rows"][0]["id"] == "a", "single ad leader")
        ok("too early" in rep["verdict"], "single ad too early")
        ok(rep["comparisons"] == [], "single ad no comparisons")

        # 3. Division-by-zero guards.
        rep = ab_report([{"id": "z", "name": "Z", "impressions": 0, "clicks": 0,
                          "conversions": 0, "spend": 0}])
        r0 = rep["rows"][0]
        ok(r0["ctr"] is None and r0["ctr_ci"] is None, "zero impressions -> None ctr")
        ok(r0["cvr"] is None and r0["cpc"] is None and r0["cpa"] is None, "zero clicks/conv -> None")

        # 4. Two ads, clear winner -> leader, one comparison, pause loser.
        ads = [
            {"id": "win", "name": "Winner", "impressions": 20000, "clicks": 2000,
             "conversions": 200, "spend": 400.0},
            {"id": "lose", "name": "Loser", "impressions": 20000, "clicks": 1000,
             "conversions": 80, "spend": 200.0},
        ]
        rep = ab_report(ads)
        ok(rep["leader"] == "win", "clear winner leader")
        ok(len(rep["comparisons"]) == 1 and rep["comparisons"][0]["vs"] == "win", "one comparison vs leader")
        ok(rep["comparisons"][0]["diff"] > 0, "diff positive = leader better")
        ok(rep["recommend_pause"] == ["lose"], "pause clearly worse challenger")
        ok(rep["rows"][0]["spend"] == 400.0 and rep["rows"][0]["cpc"] == 0.2, "money rounded 2dp")
        ok(_is_json(rep), "report json")

        # 5. String numerics + _g nested/default behaviour.
        nested = {"campaign": {"id": "n1", "name": "N"}}
        ok(_g(nested, "campaign", "id") == "n1", "_g nested")
        ok(_g(nested, "campaign", "missing", default=7) == 7, "_g default")
        rep = ab_report([{"id": "s", "name": "S", "impressions": "1000",
                          "clicks": "100", "conversions": "10", "spend": "5.678"}])
        ok(rep["rows"][0]["impressions"] == 1000 and rep["rows"][0]["spend"] == 5.68,
           "string coerced + spend rounded")

        # 6. Bonferroni: a borderline challenger flips with alpha.
        tri = [
            {"id": "L", "name": "L", "impressions": 20000, "clicks": 2000, "conversions": 200, "spend": 100},
            {"id": "B1", "name": "B1", "impressions": 20000, "clicks": 1874, "conversions": 180, "spend": 90},
            {"id": "B2", "name": "B2", "impressions": 20000, "clicks": 1000, "conversions": 90, "spend": 50},
        ]
        rep = ab_report(tri, alpha=0.05)  # bonferroni = 0.025
        b1 = next(c for c in rep["comparisons"] if c["id"] == "B1")
        b2 = next(c for c in rep["comparisons"] if c["id"] == "B2")
        ok(b1["significant"] is False, "borderline not significant under Bonferroni 0.025")
        ok(b2["significant"] is True, "far-worse challenger significant")
        ok(rep["recommend_pause"] == ["B2"], "only far-worse paused")
        rep2 = ab_report(tri, alpha=0.10)  # bonferroni = 0.05 -> B1 now significant
        b1b = next(c for c in rep2["comparisons"] if c["id"] == "B1")
        ok(b1b["significant"] is True, "borderline significant at looser Bonferroni")
        ok("B1" in rep2["recommend_pause"] and "B2" in rep2["recommend_pause"], "both paused at alpha .10")

        # 7. metric=cvr tracks conversions/clicks.
        rep = ab_report([
            {"id": "c1", "name": "C1", "impressions": 5000, "clicks": 200, "conversions": 60, "spend": 10},
            {"id": "c2", "name": "C2", "impressions": 5000, "clicks": 200, "conversions": 20, "spend": 10},
        ], metric="cvr", min_impressions=1000, min_clicks=30)
        ok(rep["leader"] == "c1", "cvr leader")
        ok(rep["rows"][0]["cvr"] is not None, "cvr computed")

        # 8. Input validation -> ToolError (never a raw exception).
        bads = [
            ("metric", lambda: ab_report([], metric="roi")),
            ("alpha", lambda: ab_report([], alpha=1.5)),
            ("base_ctr", lambda: sample_needed(0.0, 0.1)),
            ("lift", lambda: sample_needed(0.1, 0.0)),
            ("power", lambda: sample_needed(0.1, 0.1, power=2.0)),
        ]
        for name, fn in bads:
            try:
                fn()
            except ToolError:
                ok(True, "ToolError: " + name)
            else:
                ok(False, "expected ToolError: " + name)

        # 9. Malformed rows are skipped / coerced, never crash.
        rep = ab_report([None, {"name": "no-id"}, {"id": "x", "impressions": "oops"}])
        ok([r["id"] for r in rep["rows"]] == ["x"], "malformed rows skipped")
        ok(rep["rows"][0]["impressions"] == 0, "garbage numeric -> 0")

        # 10. sample_needed forwards args and ceils.
        n = sample_needed(0.1, 0.1)
        ok(n == 1235, "sample_needed ceil")
        ok(abs(calls["base"] - 0.1) < 1e-12 and abs(calls["lift"] - 0.1) < 1e-12
           and abs(calls["alpha"] - 0.05) < 1e-12 and abs(calls["power"] - 0.8) < 1e-12,
           "sample_needed forwards args")

        # 11. Deterministic tie-break by id.
        rep = ab_report([
            {"id": "b", "name": "B", "impressions": 2000, "clicks": 200, "conversions": 20, "spend": 1},
            {"id": "a", "name": "A", "impressions": 2000, "clicks": 200, "conversions": 20, "spend": 1},
        ])
        ok(rep["leader"] == "a", "deterministic tie-break by id")
    finally:
        me.wilson, me.compare, me.holdout_size = saved
    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)