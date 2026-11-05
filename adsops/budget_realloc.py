"""Budget reallocation *proposals* for AdRails.

Reads 30-day campaign performance plus an explicit policy and recommends moving
daily budget from inefficient "donors" to efficient, budget-limited
"receivers". Nothing is executed here: the module is pure, so a tool can wrap
`recommend()` behind the usual guards and persist the result with
`as_state_patches()` through the normal state-patch rail.

Safety properties: total daily spend is preserved (moves cancel exactly, in
integer cents), per-campaign min/max and the account `total_daily_cap` are
enforced, campaigns with lost_rank_is > lost_budget_is never get a raise, and
an account already over its cap gets no moves at all.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from adsops.errors import ToolError, RailViolation, scrub

_CENT = 100


def _g(row: Any, *path: str, default: Any = None) -> Any:
    """Nested camelCase getter for Google-Ads-shaped dicts; never raises."""
    cur = row
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return default if cur is None else cur


def _num(v: Any, default: float = 0.0) -> float:
    """Coerce Google's string-typed numerics; None/''/NaN/inf/bad -> default."""
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _cents(v: Any) -> int:
    """Money -> integer cents (all arithmetic stays integral to avoid drift)."""
    return int(round(_num(v) * _CENT))


def _median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _allocate(caps: List[int], total: int) -> List[int]:
    """Split `total` cents across capacities, proportional (Hamilton), ints only.

    Guarantees sum(result) == min(total, sum(caps)) and result[i] <= caps[i].
    """
    n = len(caps)
    if n == 0 or total <= 0:
        return [0] * n
    s = sum(caps)
    if s <= 0:
        return [0] * n
    if total >= s:
        return list(caps)
    out = [(total * c) // s for c in caps]
    rem = total - sum(out)  # always < n, distribute by largest fractional part
    for i in sorted(range(n), key=lambda j: (total * caps[j]) % s, reverse=True):
        if rem <= 0:
            break
        if out[i] < caps[i]:
            out[i] += 1
            rem -= 1
    return out


def _pack(moves: List[dict], before_c: int, after_c: int, notes: List[str], extra: float) -> dict:
    return {
        "moves": moves,
        "total_before": round(before_c / 100.0, 2),
        "total_after": round(after_c / 100.0, 2),
        "expected_extra_conv": round(max(extra, 0.0), 2),
        "notes": notes,
    }


def recommend(campaigns: Any, policy: Any, objective: str = "conversions",
              max_step_pct: Any = 20) -> dict:
    """Propose donor->receiver daily-budget moves inside policy caps.

    Pure: returns a JSON-serialisable dict, never mutates the inputs, never
    raises for merely pessimistic data (empty list, over-cap account, no
    eligible pair) - it returns an empty plan plus a note instead.
    """
    if not isinstance(campaigns, (list, tuple)):
        raise ToolError("campaigns must be a list of campaign dicts")
    policy = policy if isinstance(policy, dict) else {}
    notes: List[str] = []

    if objective not in ("conversions", "conv"):
        notes.append("objective '%s' unsupported; scoring on conversions" % scrub(str(objective), 40))

    step = _num(max_step_pct, 0.0)
    pol_step = _g(policy, "max_budget_step_pct")
    if pol_step is not None:
        step = min(step, _num(pol_step, step))
    if step <= 0:
        raise ToolError("max_step_pct must be a positive number of percent")
    if step > 100:
        notes.append("max_step_pct clamped to 100")
        step = 100.0

    pol_camps = _g(policy, "campaigns", default={}) or {}
    if not isinstance(pol_camps, dict):
        pol_camps = {}
        notes.append("policy['campaigns'] is not a dict; ignored")
    cap_raw = _g(policy, "total_daily_cap")
    cap_c = None if cap_raw is None else _cents(cap_raw)

    rows: List[dict] = []
    for c in campaigns:
        if not isinstance(c, dict):
            notes.append("skipped non-dict campaign entry")
            continue
        key = _g(c, "key")
        if not key:
            notes.append("skipped campaign without a key")
            continue
        if str(_g(c, "status", default="") or "").upper() != "ENABLED":
            continue
        cost = max(_num(_g(c, "cost_30d")), 0.0)
        conv = max(_num(_g(c, "conv_30d")), 0.0)
        rows.append({
            "key": str(key),
            "budget_c": max(_cents(_g(c, "daily_budget")), 0),
            "cost": cost,
            "conv": conv,
            "lost_budget": _num(_g(c, "lost_budget_is")),
            "lost_rank": _num(_g(c, "lost_rank_is")),
            "score": (conv / cost) if cost > 0 else 0.0,
        })

    total_before_c = sum(r["budget_c"] for r in rows)

    if len(rows) < 2:
        notes.append("need at least two ENABLED campaigns; no moves proposed")
        return _pack([], total_before_c, total_before_c, notes, 0.0)

    if cap_c is not None and total_before_c > cap_c:
        # Moves cancel out, so an already-over-cap account would stay over cap.
        notes.append("enabled total %.2f exceeds total_daily_cap %.2f; no moves proposed"
                     % (total_before_c / 100.0, cap_c / 100.0))
        return _pack([], total_before_c, total_before_c, notes, 0.0)

    med = _median([r["score"] for r in rows]) or 0.0
    med_cpa = _median([r["cost"] / r["conv"] for r in rows if r["conv"] > 0 and r["cost"] > 0])
    donor_thresh = med * 0.6

    donors: List[tuple] = []
    receivers: List[tuple] = []
    for r in rows:
        pc = pol_camps.get(r["key"]) or {}
        min_c = max(_cents(_g(pc, "min_daily_budget", default=0)), 0)
        max_raw = _g(pc, "max_daily_budget")
        max_c = None if max_raw is None else _cents(max_raw)
        r["step_c"] = max(int(round(r["budget_c"] * step / 100.0)), 0)

        # Zero-conversion campaigns burning >= 3x the median CPA are donors too.
        starved = (r["conv"] <= 0 and r["cost"] > 0 and med_cpa is not None
                   and r["cost"] >= 3.0 * med_cpa)
        if r["score"] < donor_thresh or starved:
            cap = min(r["step_c"], max(r["budget_c"] - min_c, 0))
            if cap > 0:
                donors.append((r, cap, "zero_conv" if (starved and r["conv"] <= 0) else "low_score"))

        if r["lost_budget"] >= 0.1 and r["score"] > med:
            if r["lost_rank"] > r["lost_budget"]:
                notes.append("not raising %s: lost_rank_is > lost_budget_is" % scrub(r["key"], 40))
                continue
            room = None if max_c is None else max_c - r["budget_c"]
            if room is not None and room <= 0:
                continue
            cap = r["step_c"] if room is None else min(r["step_c"], room)
            if cap > 0:
                receivers.append((r, cap))

    receivers.sort(key=lambda t: (-t[0]["score"], t[0]["key"]))

    if not donors or not receivers:
        notes.append("no donors or no eligible receivers inside policy caps; no moves proposed")
        return _pack([], total_before_c, total_before_c, notes, 0.0)

    give_caps = [c for _, c, _ in donors]
    take_caps = [c for _, c in receivers]
    transfer = min(sum(give_caps), sum(take_caps))
    if transfer <= 0:
        notes.append("no feasible transfer inside min/max and step caps")
        return _pack([], total_before_c, total_before_c, notes, 0.0)

    gives = _allocate(give_caps, transfer)
    takes = _allocate(take_caps, transfer)

    moves: List[dict] = []
    extra = 0.0
    for (r, _cap, why), g in zip(donors, gives):
        if g <= 0:
            continue
        if why == "zero_conv" and med_cpa:
            reason = "zero conversions at %.1fx median CPA" % (r["cost"] / med_cpa)
        else:
            reason = "marginal efficiency %.4f below %.4f threshold" % (r["score"], donor_thresh)
        moves.append({"key": r["key"], "from": round(r["budget_c"] / 100.0, 2),
                      "to": round((r["budget_c"] - g) / 100.0, 2),
                      "delta": round(-g / 100.0, 2), "reason": reason,
                      "_budget_c": r["budget_c"] - g, "_delta_c": -g})
        extra -= (g / 100.0) * r["score"]
    for (r, _cap), t in zip(receivers, takes):
        if t <= 0:
            continue
        moves.append({"key": r["key"], "from": round(r["budget_c"] / 100.0, 2),
                      "to": round((r["budget_c"] + t) / 100.0, 2),
                      "delta": round(t / 100.0, 2),
                      "reason": "lost budget share %.2f with above-median efficiency %.4f"
                                % (r["lost_budget"], r["score"]),
                      "_budget_c": r["budget_c"] + t, "_delta_c": t})
        extra += (t / 100.0) * r["score"]

    # Integer-cent allocation is drift-free by construction; the guard below is
    # a defensive net that pins any residue on the largest receiver.
    drift = sum(m["_delta_c"] for m in moves)
    if drift:
        ups = [m for m in moves if m["_delta_c"] > 0]
        if ups:
            big = max(ups, key=lambda m: m["_delta_c"])
            big["_delta_c"] -= drift
            big["_budget_c"] -= drift
            big["to"] = round(big["_budget_c"] / 100.0, 2)
            big["delta"] = round(big["_delta_c"] / 100.0, 2)

    after_c = total_before_c + sum(m["_delta_c"] for m in moves)
    if cap_c is not None and after_c > cap_c:
        raise RailViolation("refusing to propose budgets above total_daily_cap")

    out_moves = [{"key": m["key"], "from": m["from"], "to": m["to"],
                  "delta": m["delta"], "reason": m["reason"]} for m in moves]
    out_moves.sort(key=lambda m: (-m["delta"], m["key"]))
    if not out_moves:
        notes.append("reallocation rounded to zero; no moves proposed")
    return _pack(out_moves, total_before_c, after_c, notes, extra)


def as_state_patches(result: Any) -> Dict[str, dict]:
    """Turn a proposal into {campaign_key: {"daily_budget_usd": new_budget}}."""
    if not isinstance(result, dict):
        raise ToolError("result must be the dict returned by recommend()")
    patches: Dict[str, dict] = {}
    for mv in result.get("moves") or []:
        if not isinstance(mv, dict):
            continue
        key = mv.get("key")
        if not key:
            continue
        to = _num(mv.get("to"), -1.0)
        if to < 0:
            raise RailViolation("refusing to patch negative budget for %s" % scrub(str(key), 40))
        patches[str(key)] = {"daily_budget_usd": round(to, 2)}
    return patches


def selftest() -> int:
    """Exercise every branch with mocked numbers; returns the check count."""
    n = 0

    def ck(cond: Any, msg: str) -> None:
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    def raises(fn, exc, msg: str) -> None:
        nonlocal n
        n += 1
        try:
            fn()
        except exc:
            return
        raise AssertionError(msg)

    def C(key, budget, cost, conv, lb=0.0, lr=0.0, status="ENABLED"):
        return {"key": key, "daily_budget": budget, "cost_30d": cost, "conv_30d": conv,
                "lost_budget_is": lb, "lost_rank_is": lr, "status": status}

    base = [C("donor", 100, 300, 3), C("recv", 50, 100, 20, lb=0.5)]
    pol = {"campaigns": {"recv": {"max_daily_budget": 100}}}

    # 1-2: empty / single input
    r = recommend([], {})
    ck(r["moves"] == [] and bool(r["notes"]), "empty list -> no moves but a note")
    ck(r["total_before"] == 0.0 and r["total_after"] == 0.0, "empty totals are zero")
    r = recommend([C("a", 100, 50, 1)], {})
    ck(r["moves"] == [] and any("two" in s for s in r["notes"]), "single campaign -> note, no moves")

    # 3: happy path donor -> receiver
    r = recommend(base, pol)
    d = {m["key"]: m for m in r["moves"]}
    ck(len(r["moves"]) == 2, "one donor + one receiver -> two moves")
    ck(d["donor"]["to"] == 90.0 and d["donor"]["delta"] == -10.0, "donor cut by 20% step")
    ck(d["recv"]["to"] == 60.0 and d["recv"]["delta"] == 10.0, "receiver gains the same amount")
    ck(r["total_before"] == r["total_after"] == 150.0, "total daily spend preserved")
    ck(r["expected_extra_conv"] > 0, "positive expected conversion lift")

    # 4-5: policy floors and ceilings
    r = recommend(base, {"campaigns": {"donor": {"min_daily_budget": 95},
                                       "recv": {"max_daily_budget": 100}}})
    d = {m["key"]: m for m in r["moves"]}
    ck(d["donor"]["to"] == 95.0, "donor never drops below min_daily_budget")
    ck(r["total_after"] == 150.0, "total preserved with min floor")
    r = recommend(base, {"campaigns": {"recv": {"max_daily_budget": 55}}})
    d = {m["key"]: m for m in r["moves"]}
    ck(d["recv"]["to"] == 55.0, "receiver never exceeds max_daily_budget")

    # 6-7: rail and status filters
    r = recommend([C("donor", 100, 300, 3), C("recv", 50, 100, 20, lb=0.5, lr=0.9)], pol)
    ck(r["moves"] == [], "lost_rank_is > lost_budget_is blocks the raise")
    ck(any("lost_rank_is" in s for s in r["notes"]), "note explains the exclusion")
    r = recommend([C("donor", 100, 300, 3), C("recv", 50, 100, 20, lb=0.5, status="PAUSED")], pol)
    ck(r["moves"] == [], "PAUSED campaign cannot receive budget")

    # 8: policy step cap
    r = recommend([C("donor", 100, 300, 3), C("recv", 200, 100, 20, lb=0.5)],
                  {"max_budget_step_pct": 5})
    d = {m["key"]: m for m in r["moves"]}
    ck(d["donor"]["delta"] == -5.0 and d["recv"]["delta"] == 5.0,
       "policy max_budget_step_pct caps the move")

    # 9: bad input refusals
    raises(lambda: recommend(base, {}, max_step_pct=0), ToolError, "zero step refused")
    raises(lambda: recommend(base, {}, max_step_pct="nope"), ToolError, "non-numeric step refused")
    raises(lambda: recommend("nope", {}), ToolError, "campaigns must be a list")

    # 10-11: malformed rows and string numerics
    messy = base + ["junk", {"name": "no-key"}, None, 42]
    r = recommend(messy, pol)
    ck(any("non-dict" in s for s in r["notes"]), "non-dict rows skipped with a note")
    ck(any("without a key" in s for s in r["notes"]), "keyless rows skipped with a note")
    ck(r["total_after"] == r["total_before"] == 150.0, "skipped rows leave totals intact")
    strrows = [{"key": "donor", "daily_budget": "100", "cost_30d": "300", "conv_30d": "3",
                "lost_budget_is": "0", "lost_rank_is": "0", "status": "enabled"},
               {"key": "recv", "daily_budget": "50", "cost_30d": "100", "conv_30d": "20",
                "lost_budget_is": "0.5", "lost_rank_is": "0", "status": "ENABLED"}]
    ck(len(recommend(strrows, pol)["moves"]) == 2, "string-typed numerics are coerced")

    # 12: zero-conversion high-spend donor
    z = [C("a", 100, 100, 10), C("b", 100, 100, 10), C("z", 100, 500, 0),
         C("recv", 100, 100, 20, lb=0.6)]
    r = recommend(z, {})
    d = {m["key"]: m for m in r["moves"]}
    ck("z" in d and d["z"]["delta"] < 0, "zero-conversion high-spend campaign donates")
    ck(r["total_after"] == r["total_before"], "totals preserved across several campaigns")

    # 13: no signal at all
    ck(recommend([C("a", 100, 100, 0), C("b", 100, 100, 0)], {})["moves"] == [],
       "all-zero conversions -> no moves")

    # 14-15: account cap
    r = recommend(base, {"total_daily_cap": 100})
    ck(r["moves"] == [] and any("total_daily_cap" in s for s in r["notes"]),
       "account already over cap gets no moves")
    r = recommend(base, {"total_daily_cap": 200, "campaigns": {"recv": {"max_daily_budget": 100}}})
    ck(bool(r["moves"]) and r["total_after"] <= 200.0, "moves stay under total_daily_cap")

    # 16: cent-level rounding
    r = recommend([C("donor", 33.33, 300, 3), C("recv", 66.67, 100, 20, lb=0.5)], {})
    for m in r["moves"]:
        ck(round(m["to"], 2) == m["to"], "budgets rounded to cents")
    ck(r["total_after"] == 100.0, "cent-level totals still balance exactly")
    ck(sum(_allocate([10, 10, 10], 7)) == 7 and max(_allocate([10, 10, 10], 7)) <= 10,
       "_allocate never overshoots capacities")

    # 17: determinism
    ck(recommend(base, pol) == recommend(base, pol), "recommend is deterministic")
    ck(base[0]["daily_budget"] == 100, "input campaigns are not mutated")

    # 18-19: state patches
    p = as_state_patches(recommend(base, pol))
    ck(p == {"donor": {"daily_budget_usd": 90.0}, "recv": {"daily_budget_usd": 60.0}},
       "state patches mirror the proposal")
    ck(as_state_patches({"moves": []}) == {}, "empty plan -> empty patches")
    ck(as_state_patches({}) == {}, "missing moves key -> empty patches")
    raises(lambda: as_state_patches({"moves": [{"key": "x", "to": -1}]}), RailViolation,
           "negative budget refused")
    raises(lambda: as_state_patches("nope"), ToolError, "non-dict result refused")
    ck(as_state_patches({"moves": [{"key": "k", "from": 1, "to": 2.006}]})
       == {"k": {"daily_budget_usd": 2.01}}, "patch budgets rounded to 2dp")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
