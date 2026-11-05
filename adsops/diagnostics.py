"""Read-only PPC diagnostics for Google Ads (pure, side-effect free).

Turns raw Google Ads REST rows (nested camelCase dicts, money in micros) into
JSON-ready reports: wasted search-term spend, impression-share loss, quality
score breakdowns and day-over-day anomalies.  No network/file/clock I/O --
the caller supplies the rowsets, so every function is deterministic and easy
to mock.  A tool should render the GAQL in ``QUERIES`` (via ``query()``) and
call ``bundle(**rowsets)`` for one report plus a prioritised ``headline``.
Money leaves here as float currency units rounded to 2dp; impression-share
fractions stay 0-1 floats.  Nothing secret is ever logged or returned.
"""

from __future__ import annotations

import json
import statistics
import sys

from adsops.errors import RailViolation, ToolError, scrub  # noqa: F401  (re-exported)

MICROS = 1_000_000

__all__ = [
    "QUERIES", "query", "wasted_spend", "impression_share",
    "quality_scores", "anomalies", "bundle", "selftest",
]

# GAQL templates.  ``{since}``/``{until}`` are date strings (YYYY-MM-DD);
# campaign scoping (``{cid}``) is rendered by ``query()`` after int()-checking.
QUERIES = {
    "search_terms": (
        "SELECT search_term_view.search_term, campaign.id, metrics.cost_micros, "
        "metrics.clicks, metrics.impressions, metrics.conversions "
        "FROM search_term_view WHERE segments.date BETWEEN '{since}' AND '{until}'{cid}"
    ),
    "keywords": (
        "SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
        "ad_group_criterion.quality_info.quality_score, "
        "ad_group_criterion.quality_info.creative_quality_score, "
        "ad_group_criterion.quality_info.post_click_quality_score, "
        "ad_group_criterion.quality_info.search_predicted_ctr, "
        "metrics.cost_micros, metrics.clicks, metrics.conversions "
        "FROM keyword_view WHERE segments.date BETWEEN '{since}' AND '{until}'{cid}"
    ),
    "impression_share": (
        "SELECT campaign.id, campaign.name, metrics.search_impression_share, "
        "metrics.search_budget_lost_impression_share, "
        "metrics.search_rank_lost_impression_share, metrics.cost_micros "
        "FROM campaign WHERE segments.date BETWEEN '{since}' AND '{until}'{cid}"
    ),
    "daily": (
        "SELECT campaign.id, campaign.name, segments.date, metrics.cost_micros, "
        "metrics.clicks, metrics.impressions, metrics.conversions "
        "FROM campaign WHERE segments.date BETWEEN '{since}' AND '{until}'{cid}"
    ),
}


def query(name, cid=None, since="", until=""):
    """Render a GAQL query; ``cid`` adds an injection-safe campaign filter."""
    try:
        template = QUERIES[name]
    except KeyError:
        raise ToolError(f"unknown query {name!r}; expected {sorted(QUERIES)}") from None

    cid_clause = ""
    if cid not in (None, ""):
        try:
            safe_cid = int(cid)  # int() rejects "1 OR 1=1"-style injection
        except (TypeError, ValueError):
            raise ToolError("cid must be an integer id") from None
        cid_clause = f" AND campaign.id = {safe_cid}"

    return template.format(since=since, until=until, cid=cid_clause)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _alt_key(key):
    """camelCase -> snake_case (or the reverse) for the same logical field."""
    if "_" in key:
        head, *rest = key.split("_")
        return head + "".join(p[:1].upper() + p[1:] for p in rest)
    out = []
    for ch in key:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _g(row, *path, default=None):
    """Nested get tolerating camelCase vs snake_case keys; None -> default."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key in cur:
            cur = cur[key]
            continue
        alt = _alt_key(key)
        if alt in cur:
            cur = cur[alt]
            continue
        return default
    return default if cur is None else cur


def _num(value, default=0.0):
    """Defensive numeric coercion: strings, commas, junk, None, bools."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


def _opt_num(value):
    """Like ``_num`` but returns None when the value is missing/unparseable."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clamp01(value):
    return max(0.0, min(1.0, float(value)))


def _frac(value):
    """Parse impression-share values into 0..1 floats.

    Google returns a float, or a bucket string such as "< 0.1", "> 0.9" or
    "< 10%".  Buckets are mapped to the near side (0.09 / 0.91) so downstream
    maths stays numeric.
    """
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return _clamp01(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return 0.0
    offset = 0.0
    if text[0] in "<>":
        offset = -0.01 if text[0] == "<" else 0.01
        text = text[1:]
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return 0.0
    if percent:
        number /= 100.0
    return _clamp01(round(number + offset, 4))


def _cost(row):
    """metrics.costMicros -> float currency units (unrounded)."""
    return _num(_g(row, "metrics", "costMicros")) / MICROS


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def wasted_spend(search_term_rows, min_cost=5.0, top=25):
    """Zero-conversion search terms costing at least ``min_cost`` currency units."""
    rows = search_term_rows or []
    floor = _num(min_cost, 5.0)
    total = 0.0
    wasted = []
    for row in rows:
        if not isinstance(row, dict):
            continue  # tolerate malformed rows
        cost = _cost(row)
        total += cost
        clicks = int(_num(_g(row, "metrics", "clicks")))
        impressions = int(_num(_g(row, "metrics", "impressions")))
        conversions = _num(_g(row, "metrics", "conversions"))
        # ``cost > 0`` keeps free impressions out of the wasted list.
        if cost >= floor and cost > 0 and conversions == 0:
            term = _g(row, "searchTermView", "searchTerm")
            wasted.append({
                "term": str(term).strip() if term else "(not provided)",
                "cost": round(cost, 2),
                "clicks": clicks,
                "impressions": impressions,
            })
    wasted.sort(key=lambda t: (-t["cost"], t["term"]))
    wasted_total = round(float(sum(t["cost"] for t in wasted)), 2)
    limit = int(_num(top, 25))
    terms = wasted if limit <= 0 else wasted[:limit]  # top<=0 means "no limit"
    return {
        "total_cost": round(total, 2),
        "wasted_cost": wasted_total,
        "wasted_pct": round(wasted_total / total * 100.0, 2) if total > 0 else 0.0,
        "terms": terms,
    }


def impression_share(rows):
    """Per-campaign impression share, budget/rank loss and a plain diagnosis."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cid = _g(row, "campaign", "id")
        name = _g(row, "campaign", "name")
        if name:
            label = str(name)
        elif cid not in (None, ""):
            label = f"campaign {cid}"
        else:
            label = "unknown campaign"
        shown = _frac(_g(row, "metrics", "searchImpressionShare"))
        lost_budget = _frac(_g(row, "metrics", "searchBudgetLostImpressionShare"))
        lost_rank = _frac(_g(row, "metrics", "searchRankLostImpressionShare"))
        if lost_budget >= 0.2:
            diagnosis = "budget-limited"
        elif lost_rank >= 0.3:
            diagnosis = "rank-limited"
        else:
            diagnosis = "healthy"
        out.append({
            "campaign": label,
            "is": round(shown, 4),
            "lost_budget": round(lost_budget, 4),
            "lost_rank": round(lost_rank, 4),
            "diagnosis": diagnosis,
        })
    return out


_COMPONENTS = (
    ("creative", "creativeQualityScore"),
    ("landing", "postClickQualityScore"),
    ("ctr", "searchPredictedCtr"),
)


def quality_scores(keyword_rows):
    """Cost-weighted QS, worst keywords (QS<=4) and quality-component tallies."""
    counts = {"creative": {}, "landing": {}, "ctr": {}}
    qs_weighted = 0.0
    cost_weight = 0.0
    qs_plain = 0.0
    qs_n = 0
    low = []
    for row in keyword_rows or []:
        if not isinstance(row, dict):
            continue
        text = _g(row, "adGroupCriterion", "keyword", "text")
        match = _g(row, "adGroupCriterion", "keyword", "matchType")
        info = _g(row, "adGroupCriterion", "qualityInfo") or {}
        if not isinstance(info, dict):
            info = {}
        cost = _cost(row)
        qs = _opt_num(_g(info, "qualityScore"))
        if qs is not None:
            qs_weighted += qs * cost
            cost_weight += cost
            qs_plain += qs
            qs_n += 1
            if qs <= 4:
                low.append({
                    "keyword": str(text) if text else "(not provided)",
                    "match_type": str(match) if match else "",
                    "quality_score": int(qs),
                    "cost": round(cost, 2),
                    "clicks": int(_num(_g(row, "metrics", "clicks"))),
                })
        for bucket, field in _COMPONENTS:
            raw = _g(info, field)
            if isinstance(raw, str) and raw.strip():
                label = raw.strip().upper()
                counts[bucket][label] = counts[bucket].get(label, 0) + 1
    if cost_weight > 0:
        weighted = qs_weighted / cost_weight
    elif qs_n:
        weighted = qs_plain / qs_n  # all-zero spend: fall back to a plain mean
    else:
        weighted = 0.0
    low.sort(key=lambda k: (-k["cost"], k["keyword"]))
    return {"weighted_qs": weighted, "low_qs": low, "component_counts": counts}


_METRICS = ("cost", "clicks", "conversions", "cpa")


def _daily_value(row, metric):
    """One metric for one daily row, or None when it cannot be computed."""
    metrics = _g(row, "metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    cost = _num(_g(metrics, "costMicros")) / MICROS
    conversions = _num(_g(metrics, "conversions"))
    if metric == "cost":
        return cost
    if metric == "clicks":
        return _num(_g(metrics, "clicks"))
    if metric == "conversions":
        return conversions
    if metric == "cpa":
        if conversions <= 0:
            return None  # undefined CPA days are skipped entirely
        return cost / conversions
    return None


def anomalies(daily_rows, z=2.5, min_days=7):
    """Compare each campaign's last day to the mean/stdev of prior days."""
    threshold = _num(z, 2.5)
    need = max(1, int(_num(min_days, 7)))
    by_campaign = {}
    for row in daily_rows or []:
        if not isinstance(row, dict):
            continue
        cid = _g(row, "campaign", "id")
        date = _g(row, "segments", "date")
        if cid in (None, "") or not date:
            continue
        by_campaign.setdefault(str(cid), []).append(row)
    findings = []
    for cid in sorted(by_campaign):
        rows = sorted(by_campaign[cid], key=lambda r: str(_g(r, "segments", "date")))
        if len(rows) < need + 1:
            continue  # not enough history to judge the final day
        last = rows[-1]
        date = str(_g(last, "segments", "date"))
        history = rows[:-1]
        for metric in _METRICS:
            hist = [v for v in (_daily_value(r, metric) for r in history) if v is not None]
            if len(hist) < need:
                continue
            value = _daily_value(last, metric)
            if value is None:
                continue
            mean = statistics.fmean(hist)
            sd = statistics.pstdev(hist)
            note = ""
            if sd == 0:
                # Flat baseline: only a >50% move is interesting (z undefined).
                if mean == 0:
                    if value == 0:
                        continue
                    zscore = 0.0
                    note = "baseline was flat at zero"
                else:
                    change = (value - mean) / abs(mean)
                    if abs(change) <= 0.5:
                        continue
                    zscore = 0.0
                    note = "flat baseline, last day moved more than 50%"
            else:
                zscore = (value - mean) / sd
                if abs(zscore) < threshold:
                    continue
            direction = "up" if value > mean else "down"
            if metric in ("cost", "cpa"):
                detail = f"{metric} moved {direction} to ${value:,.2f} from a ${mean:,.2f} baseline"
            else:
                detail = f"{metric} moved {direction} to {value:,.2f} from a {mean:,.2f} baseline"
            detail += f" (z={zscore:.1f} over {len(hist)} days)" if sd > 0 else f" (flat {len(hist)}-day baseline)"
            if note:
                detail += f"; {note}"
            findings.append({
                "campaign_id": cid,
                "metric": metric,
                "date": date,
                "value": round(value, 2),
                "baseline_mean": round(mean, 2),
                "z": round(zscore, 2),
                "direction": direction,
                "explanation": detail,
            })
    # Most surprising first; stable tie-break keeps output deterministic.
    findings.sort(key=lambda f: (-abs(f["z"]), f["campaign_id"], f["metric"]))
    return findings


def bundle(**rowsets):
    """Combine all four diagnostics into one report plus a ranked headline."""
    waste = wasted_spend(rowsets.get("search_terms") or [])
    shares = impression_share(rowsets.get("impression_share") or [])
    quality = quality_scores(rowsets.get("keywords") or [])
    anoms = anomalies(rowsets.get("daily") or [])

    scored = []
    if waste["wasted_cost"] > 0:
        scored.append((1000.0 + waste["wasted_cost"],
                       f"${waste['wasted_cost']:.2f} wasted ({waste['wasted_pct']:.1f}% of tracked spend) "
                       f"across {len(waste['terms'])} zero-conversion search terms"))
    for row in shares:
        if row["diagnosis"] == "budget-limited":
            scored.append((600.0 + row["lost_budget"] * 100.0,
                           f"{row['campaign']}: losing {row['lost_budget'] * 100:.0f}% of impression share to budget"))
        elif row["diagnosis"] == "rank-limited":
            scored.append((500.0 + row["lost_rank"] * 100.0,
                           f"{row['campaign']}: losing {row['lost_rank'] * 100:.0f}% of impression share to ad rank"))
    if quality["low_qs"]:
        worst = quality["low_qs"][0]
        scored.append((400.0 + len(quality["low_qs"]),
                       f"{len(quality['low_qs'])} keyword(s) with quality score <= 4; "
                       f"worst: '{worst['keyword']}' (QS {worst['quality_score']}, ${worst['cost']:.2f})"))
    for a in anoms[:5]:
        scored.append((300.0 + abs(a["z"]) + (a["value"] or 0) / 1000.0, a["explanation"]))
    scored.sort(key=lambda s: (-s[0], s[1]))
    headline = [text for _, text in scored[:5]] or ["No material issues detected in the supplied rowsets"]
    return {
        "wasted_spend": waste,
        "impression_share": shares,
        "quality_scores": quality,
        "anomalies": anoms,
        "headline": headline,
    }


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def selftest() -> int:
    """Exercise every public function on in-memory rows (no I/O at all)."""
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        assert cond, f"check failed: {msg}"
        checks += 1

    # helpers ------------------------------------------------------------
    ok(_g({"a": {"b": 1}}, "a", "b") == 1, "_g nested camel")
    ok(_g({}, "a", "b", default=7) == 7, "_g default")
    ok(_g({"search_term_view": {"search_term": "x"}}, "searchTermView", "searchTerm") == "x", "_g snake")
    ok(_g({"metrics": {"costMicros": None}}, "metrics", "costMicros", default=0) == 0, "_g None")
    ok(_num("2,500,000") == 2500000.0 and _num(None) == 0.0 and _num("bad", 1.5) == 1.5, "_num coercion")
    ok(abs(_frac("< 0.1") - 0.09) < 1e-9, "_frac '<' bucket")
    ok(abs(_frac("> 0.9") - 0.91) < 1e-9, "_frac '>' bucket")
    ok(abs(_frac("< 10%") - 0.09) < 1e-9 and abs(_frac("35%") - 0.35) < 1e-9, "_frac percent")
    ok(_frac(0.5) == 0.5 and _frac(None) == 0.0 and _frac("junk") == 0.0, "_frac plain")

    # queries ------------------------------------------------------------
    ok("campaign.id = 123" in query("daily", cid=123, since="a", until="b"), "query cid")
    try:
        query("daily", cid="1 OR 1=1")
    except ToolError:
        ok(True, "query rejects injection")
    else:
        ok(False, "query rejects injection")
    try:
        query("nope")
    except ToolError:
        ok(True, "query rejects unknown name")
    else:
        ok(False, "query rejects unknown name")

    # wasted_spend -------------------------------------------------------
    st_rows = [
        {"searchTermView": {"searchTerm": "cheap flights"}, "campaign": {"id": "1"},
         "metrics": {"costMicros": "10000000", "clicks": "5", "impressions": "900", "conversions": "0"}},
        {"searchTermView": {"searchTerm": "free stuff"},
         "metrics": {"costMicros": 2500000, "clicks": 3, "impressions": 40, "conversions": 0}},
        {"searchTermView": {"searchTerm": "good term"},
         "metrics": {"costMicros": "4000000", "clicks": 2, "impressions": 10, "conversions": "2"}},
        {"searchTermView": {"searchTerm": "exactly five"},
         "metrics": {"costMicros": "5000000", "conversions": "0.0"}},
        "not a dict",
        {"metrics": {}},
    ]
    waste = wasted_spend(st_rows)
    ok(waste["total_cost"] == 21.5, "wasted total_cost")
    ok(waste["wasted_cost"] == 15.0, "wasted wasted_cost")
    ok(waste["wasted_pct"] == 69.77, "wasted pct")
    ok([t["term"] for t in waste["terms"]] == ["cheap flights", "exactly five"], "wasted ordering/boundary")
    ok(waste["terms"][0]["clicks"] == 5 and waste["terms"][0]["impressions"] == 900, "wasted term fields")
    ok(len(wasted_spend(st_rows, top=1)["terms"]) == 1, "wasted top limit")
    ok(wasted_spend(st_rows, min_cost=100)["wasted_cost"] == 0.0, "wasted high floor")
    ok(wasted_spend([]) == {"total_cost": 0.0, "wasted_cost": 0.0, "wasted_pct": 0.0, "terms": []}, "wasted empty")

    # impression_share ---------------------------------------------------
    is_rows = [
        {"campaign": {"id": "1", "name": "Brand"},
         "metrics": {"searchImpressionShare": "0.45", "searchBudgetLostImpressionShare": "0.25",
                     "searchRankLostImpressionShare": "0.1"}},
        {"campaign": {"id": "2", "name": "Generic"},
         "metrics": {"searchImpressionShare": 0.8, "searchBudgetLostImpressionShare": "< 0.1",
                     "searchRankLostImpressionShare": "> 0.9"}},
        {"campaign": {"id": "3", "name": "Healthy"},
         "metrics": {"searchImpressionShare": 0.9, "searchBudgetLostImpressionShare": 0.0,
                     "searchRankLostImpressionShare": 0.0}},
        {"campaign": {"id": "4", "name": "Edge"},
         "metrics": {"searchBudgetLostImpressionShare": 0.2, "searchRankLostImpressionShare": 0.3}},
        {"campaign": {"id": "6", "name": "RankEdge"},
         "metrics": {"searchBudgetLostImpressionShare": "0.1",
                     "searchRankLostImpressionShare": "0.3"}},
        {"campaign": {"id": "5"}},
    ]
    shares = impression_share(is_rows)
    ok(len(shares) == 6, "share row count")
    ok(shares[0]["diagnosis"] == "budget-limited" and shares[0]["lost_budget"] == 0.25, "share budget-limited")
    ok(shares[1]["diagnosis"] == "rank-limited" and shares[1]["lost_rank"] == 0.91, "share rank bucket")
    ok(shares[1]["lost_budget"] == 0.09, "share budget bucket")
    ok(shares[2]["diagnosis"] == "healthy", "share healthy")
    ok(shares[3]["diagnosis"] == "budget-limited", "share budget boundary")
    ok(shares[4]["diagnosis"] == "rank-limited", "share rank boundary")
    ok(shares[5]["is"] == 0.0 and shares[5]["campaign"] == "campaign 5", "share missing metrics")
    ok(impression_share([]) == [], "share empty")

    # quality_scores -----------------------------------------------------
    kw_rows = [
        {"adGroupCriterion": {"keyword": {"text": "shoes", "matchType": "EXACT"},
                              "qualityInfo": {"qualityScore": 7, "creativeQualityScore": "ABOVE_AVERAGE",
                                              "postClickQualityScore": "AVERAGE",
                                              "searchPredictedCtr": "BELOW_AVERAGE"}},
         "metrics": {"costMicros": "10000000", "clicks": "10"}},
        {"adGroupCriterion": {"keyword": {"text": "cheap shoes", "matchType": "PHRASE"},
                              "qualityInfo": {"qualityScore": "3", "creativeQualityScore": "BELOW_AVERAGE",
                                              "postClickQualityScore": "BELOW_AVERAGE",
                                              "searchPredictedCtr": "AVERAGE"}},
         "metrics": {"costMicros": "4000000", "clicks": "4"}},
        {"adGroupCriterion": {"keyword": {"text": "no score", "matchType": "BROAD"},
                              "qualityInfo": {}},
         "metrics": {"costMicros": "2000000", "clicks": "1"}},
        {"adGroupCriterion": {"keyword": {"text": "zero cost", "matchType": "BROAD"},
                              "qualityInfo": {"qualityScore": 4}}},
        {"adGroupCriterion": {}},
    ]
    quality = quality_scores(kw_rows)
    ok(abs(quality["weighted_qs"] - (82 / 14)) < 1e-9, "qs weighted (cost-weighted)")
    ok([k["keyword"] for k in quality["low_qs"]] == ["cheap shoes", "zero cost"], "qs low list")
    ok(quality["low_qs"][0]["quality_score"] == 3 and quality["low_qs"][0]["cost"] == 4.0, "qs low fields")
    ok(quality["component_counts"]["creative"] == {"ABOVE_AVERAGE": 1, "BELOW_AVERAGE": 1}, "qs creative counts")
    ok(quality["component_counts"]["landing"] == {"AVERAGE": 1, "BELOW_AVERAGE": 1}, "qs landing counts")
    ok(quality["component_counts"]["ctr"] == {"BELOW_AVERAGE": 1, "AVERAGE": 1}, "qs ctr counts")
    ok(quality_scores([])["weighted_qs"] == 0.0 and quality_scores([])["low_qs"] == [], "qs empty")
    flat = quality_scores([{"adGroupCriterion": {"qualityInfo": {"qualityScore": 8}}},
                           {"adGroupCriterion": {"qualityInfo": {"qualityScore": 2}}}])
    ok(flat["weighted_qs"] == 5.0, "qs zero-cost fallback")

    # anomalies ----------------------------------------------------------
    def daily_rows(cid, costs, clicks=None, convs=None):
        out = []
        for i, c in enumerate(costs):
            out.append({
                "campaign": {"id": cid},
                "segments": {"date": f"2024-01-{i + 1:02d}"},
                "metrics": {"costMicros": str(int(c * MICROS)),
                            "clicks": str(clicks[i] if clicks else 0),
                            "impressions": "100",
                            "conversions": str(convs[i] if convs else 0)},
            })
        return out

    daily = (daily_rows("111", [10] * 7 + [20])
             + daily_rows("222", [10, 12, 11, 9, 10, 11, 10, 30],
                          clicks=[5, 6, 5, 4, 5, 6, 5, 5], convs=[1] * 8))
    anoms = anomalies(daily)
    ok(isinstance(anoms, list) and len(anoms) >= 3, "anomalies found")
    flat_cost = [a for a in anoms if a["campaign_id"] == "111" and a["metric"] == "cost"]
    ok(len(flat_cost) == 1 and flat_cost[0]["direction"] == "up" and flat_cost[0]["z"] == 0.0, "anomaly flat baseline")
    ok(flat_cost[0]["date"] == "2024-01-08" and flat_cost[0]["value"] == 20.0, "anomaly last date")
    b_cost = [a for a in anoms if a["campaign_id"] == "222" and a["metric"] == "cost"]
    ok(len(b_cost) == 1 and abs(b_cost[0]["z"]) > 2.5 and b_cost[0]["value"] == 30.0, "anomaly z spike")
    ok(bool([a for a in anoms if a["campaign_id"] == "222" and a["metric"] == "cpa"]), "anomaly cpa")
    ok(not [a for a in anoms if a["campaign_id"] == "111" and a["metric"] in ("clicks", "conversions", "cpa")],
       "anomaly zero metrics skipped")
    ok(anomalies([]) == [], "anomalies empty")
    ok(anomalies(daily_rows("333", [10] * 6 + [50])) == [], "anomalies need history")
    ok(anomalies(daily, min_days=20) == [], "anomalies min_days")
    high = anomalies(daily, z=100)
    ok(len(high) == 1 and high[0]["campaign_id"] == "111", "anomalies z threshold")
    ok(anomalies([{"campaign": {}, "segments": {"date": "2024-01-01"}}, "junk"]) == [], "anomalies malformed")

    # bundle -------------------------------------------------------------
    rep = bundle(search_terms=st_rows, keywords=kw_rows, impression_share=is_rows, daily=daily)
    ok(set(rep) == {"wasted_spend", "impression_share", "quality_scores", "anomalies", "headline"}, "bundle keys")
    ok(isinstance(rep["headline"], list) and 0 < len(rep["headline"]) <= 5, "bundle headline size")
    ok(all(isinstance(h, str) for h in rep["headline"]), "bundle headline strings")
    ok("wasted" in rep["headline"][0], "bundle headline priority")
    ok(any("Brand" in h for h in rep["headline"]), "bundle headline campaign")
    ok(json.dumps(rep) != "", "bundle json-serialisable")
    empty = bundle()
    ok(empty["headline"] == ["No material issues detected in the supplied rowsets"], "bundle empty headline")
    ok(empty["wasted_spend"]["terms"] == [] and empty["anomalies"] == [], "bundle empty rowsets")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)