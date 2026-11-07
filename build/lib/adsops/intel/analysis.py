"""Per-competitor analysis and the spend BAND.

Appearance share = (samples of keyword k in which domain d showed an ad) / (samples of k). Samples are the
denominator, so a keyword we sampled 40 times and a competitor appeared in 10 gives 25%, regardless of how
many ads were on each page.

Spend band (monthly USD), per domain, summed over keywords with volume data:
    low  = sum_k vol_low_k  * share_k * CTR_low(pos_k)  * CPC_low_k
    high = sum_k vol_high_k * share_k * CTR_high(pos_k) * CPC_high_k
where pos_k is the domain's mean ad rank on k and CTR depends on rank AND top-vs-bottom placement.
This is deliberately a band: inputs are ranges, and a point estimate would be falsely precise.

Volume/CPC source is pluggable (``VolumeSource``). v0 ships ``CsvVolumeSource`` (a CSV the user fills,
later Keyword Planner). ``write_placeholder_csv`` writes a CSV with every row marked PLACEHOLDER; any band
computed from a placeholder row is flagged ``placeholder_inputs=True`` and the report says so.
"""
import csv
import hashlib
import json
import os
import sys
from collections import defaultdict

# CTR by paid rank, as [low, high] fractions. ASSUMPTIONS, uncalibrated, to be replaced by our own account's
# observed CTR and the calibration loop. Sources:
#  - Shape (steep fall after rank 1): AccuraCast, analysis of ~2M AdWords clicks: rank 1 avg 7.11%, rank 9 0.55%;
#    a 20-rank table puts rank 1 at 7.94%, rank 2 at 5.00% (and in an older table 3.01%, rank 3 2.19%,
#    rank 4 2.15%). https://www.accuracast.com/articles/advertising/adwords-clickthrough/ (pre-2016 layout data)
#  - Level: LocaliQ search benchmarks, Property Management subcategory average CTR 6.66%, avg CPC $3.94
#    (2026 edition, quoted by secondary sources; not verified against the primary report).
#    https://localiq.com/blog/real-estate-search-advertising-benchmarks/
#  - Bottom-of-page ads: no good public source; set an order of magnitude below rank 1. Treat as a guess.
CTR_DEFAULT = {
    "source": "AccuraCast position curve (shape) + LocaliQ property-management avg CTR 6.66% (level); "
              "bottom-of-page is a guess. Uncalibrated assumption, see analysis.py header.",
    "top": {"1": [0.05, 0.10], "2": [0.03, 0.06], "3": [0.02, 0.04], "4": [0.015, 0.03]},
    "bottom": [0.005, 0.015],
}

PLACEHOLDER = "PLACEHOLDER"
CSV_FIELDS = ["keyword", "monthly_volume_low", "monthly_volume_high", "cpc_low_usd", "cpc_high_usd", "source"]
# Placeholder values, clearly fake: a tiny local volume range and a CPC range around LocaliQ's PM average.
PLACEHOLDER_ROW = {"monthly_volume_low": 10, "monthly_volume_high": 50, "cpc_low_usd": 2.50, "cpc_high_usd": 8.00}


def ctr_band(ctr, pos, top):
    if not top:
        return tuple(ctr["bottom"])
    r = max(1, int(round(pos or 1)))
    key = str(min(r, max(int(k) for k in ctr["top"])))
    return tuple(ctr["top"][key])


def load_ctr(path=None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return CTR_DEFAULT


class VolumeSource:
    """Interface: get(keyword) -> dict(vol_low, vol_high, cpc_low, cpc_high, source, placeholder) or None."""

    def get(self, keyword):
        raise NotImplementedError


class CsvVolumeSource(VolumeSource):
    def __init__(self, path):
        self.rows = {}
        if path and os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        row = {"vol_low": float(r["monthly_volume_low"]), "vol_high": float(r["monthly_volume_high"]),
                               "cpc_low": float(r["cpc_low_usd"]), "cpc_high": float(r["cpc_high_usd"]),
                               "source": (r.get("source") or "").strip()}
                    except (KeyError, TypeError, ValueError):
                        continue        # blank or malformed row = no data for that keyword
                    if row["vol_low"] > row["vol_high"] or row["cpc_low"] > row["cpc_high"]:
                        continue
                    row["placeholder"] = row["source"].upper().startswith(PLACEHOLDER) or not row["source"]
                    self.rows[r["keyword"].strip().lower()] = row

    def get(self, keyword):
        return self.rows.get(keyword.lower())


def write_placeholder_csv(path, keywords):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for k in keywords:
            w.writerow({"keyword": k, **PLACEHOLDER_ROW,
                        "source": f"{PLACEHOLDER} - not real data; replace with Keyword Planner ranges"})


def copy_id(o):
    return hashlib.sha1(f"{o['headline']}\n{o['description']}".encode()).hexdigest()[:10]


def competitor_stats(samples, observations, our_domains=()):
    """-> {domain: stats}. Each sample counts at most once per domain."""
    n_by_kw = defaultdict(int)
    n_by_loc = defaultdict(int)
    for s in samples:
        n_by_kw[s["keyword"]] += 1
        n_by_loc[s["location"]] += 1
    total = len(samples)
    per = defaultdict(lambda: {"tasks": {}, "ranks": defaultdict(list), "top": 0, "ads": 0, "first": None,
                               "last": None, "copies": {}, "landing": defaultdict(int)})
    for o in observations:
        d = per[o["domain"]]
        d["ads"] += 1
        d["top"] += o["top_of_page"]
        d["ranks"][o["keyword"]].append((o["position"] or 0, o["top_of_page"]))
        d["tasks"][o["task_id"]] = (o["keyword"], o["location"])
        d["first"] = o["ts"] if d["first"] is None or o["ts"] < d["first"] else d["first"]
        d["last"] = o["ts"] if d["last"] is None or o["ts"] > d["last"] else d["last"]
        cid = copy_id(o)
        c = d["copies"].setdefault(cid, {"headline": o["headline"], "description": o["description"],
                                         "first": o["ts"], "last": o["ts"], "n": 0})
        c["n"] += 1
        c["first"], c["last"] = min(c["first"], o["ts"]), max(c["last"], o["ts"])
        d["landing"][o["landing_url"].split("?")[0]] += 1
    out = {}
    for dom, d in per.items():
        kw_hits, loc_hits = defaultdict(int), defaultdict(int)
        for kw, loc in d["tasks"].values():
            kw_hits[kw] += 1
            loc_hits[loc] += 1
        allranks = [r for rs in d["ranks"].values() for r, _ in rs]
        out[dom] = {
            "domain": dom,
            "ours": dom in our_domains,
            "samples_appeared": len(d["tasks"]),
            "share": len(d["tasks"]) / total if total else 0.0,
            "share_by_keyword": {k: kw_hits[k] / n_by_kw[k] for k in kw_hits if n_by_kw[k]},
            "share_by_location": {l: loc_hits[l] / n_by_loc[l] for l in loc_hits if n_by_loc[l]},
            "avg_position": sum(allranks) / len(allranks) if allranks else None,
            "pos_by_keyword": {k: (sum(r for r, _ in v) / len(v), sum(t for _, t in v) / len(v))
                               for k, v in d["ranks"].items()},
            "top_share": d["top"] / d["ads"] if d["ads"] else 0.0,
            "first_seen": d["first"], "last_seen": d["last"],
            "copies": d["copies"],
            "landing_urls": dict(sorted(d["landing"].items(), key=lambda x: -x[1])),
        }
    return out


def spend_band(stats, volumes, ctr=CTR_DEFAULT):
    """-> {low, high, keywords_used, keywords_missing, placeholder_inputs} in monthly USD."""
    low = high = 0.0
    used, missing, ph = 0, [], False
    for kw, share in stats["share_by_keyword"].items():
        v = volumes.get(kw)
        if not v:
            missing.append(kw)
            continue
        pos, top_frac = stats["pos_by_keyword"].get(kw, (1.0, 1.0))
        t_lo, t_hi = ctr_band(ctr, pos, True)
        b_lo, b_hi = ctr_band(ctr, pos, False)
        c_lo = top_frac * t_lo + (1 - top_frac) * b_lo
        c_hi = top_frac * t_hi + (1 - top_frac) * b_hi
        low += v["vol_low"] * share * c_lo * v["cpc_low"]
        high += v["vol_high"] * share * c_hi * v["cpc_high"]
        used += 1
        ph = ph or v["placeholder"]
    return {"low": round(low, 2), "high": round(high, 2), "keywords_used": used,
            "keywords_missing": sorted(missing), "placeholder_inputs": ph}


def selftest():
    import tempfile
    n = 0
    S = [{"task_id": f"s{i}", "keyword": "k1" if i < 4 else "k2", "location": "a" if i % 2 else "b"} for i in range(6)]

    def ob(tid, dom, kw, loc, pos, top, h="H", ts="2026-11-01T00:00:00Z"):
        return {"task_id": tid, "domain": dom, "keyword": kw, "location": loc, "position": pos, "top_of_page": top,
                "headline": h, "description": "D", "landing_url": f"https://{dom}/lp?gclid=1", "ts": ts}
    O = [ob("s0", "r.com", "k1", "b", 1, 1), ob("s1", "r.com", "k1", "a", 1, 1, ts="2026-11-03T00:00:00Z"),
         ob("s1", "r.com", "k1", "a", 3, 0, h="H2"),          # same sample twice: counts once for share
         ob("s4", "r.com", "k2", "b", 2, 1), ob("s2", "q.com", "k1", "b", 2, 1)]
    st = competitor_stats(S, O, our_domains=("q.com",))
    r = st["r.com"]
    assert r["samples_appeared"] == 3 and abs(r["share"] - 0.5) < 1e-9; n += 1
    assert r["share_by_keyword"] == {"k1": 0.5, "k2": 0.5}; n += 1
    assert abs(r["share_by_location"]["a"] - 1 / 3) < 1e-9 and abs(r["share_by_location"]["b"] - 2 / 3) < 1e-9; n += 1
    assert abs(r["avg_position"] - 7 / 4) < 1e-9 and abs(r["top_share"] - 0.75) < 1e-9; n += 1
    assert r["first_seen"] == "2026-11-01T00:00:00Z" and r["last_seen"] == "2026-11-03T00:00:00Z"; n += 1
    assert len(r["copies"]) == 2 and list(r["landing_urls"]) == ["https://r.com/lp"]; n += 1
    assert st["q.com"]["ours"] and not r["ours"]; n += 1
    assert ctr_band(CTR_DEFAULT, 1, True) == (0.05, 0.10) and ctr_band(CTR_DEFAULT, 7, True) == (0.015, 0.03); n += 1
    assert ctr_band(CTR_DEFAULT, 1, False) == (0.005, 0.015); n += 1
    d = tempfile.mkdtemp()
    p = os.path.join(d, "v.csv")
    with open(p, "w") as fh:
        fh.write("keyword,monthly_volume_low,monthly_volume_high,cpc_low_usd,cpc_high_usd,source\n"
                 "k1,100,200,2,6,keyword planner 2026-11\nk2,,,,,\nk3,5,1,1,1,bad range\n")
    vs = CsvVolumeSource(p)
    assert vs.get("K1")["vol_low"] == 100 and vs.get("k2") is None and vs.get("k3") is None; n += 1
    b = spend_band({"share_by_keyword": {"k1": 0.5}, "pos_by_keyword": {"k1": (1.0, 1.0)}}, vs)
    assert b["low"] == round(100 * 0.5 * 0.05 * 2, 2) and b["high"] == round(200 * 0.5 * 0.10 * 6, 2); n += 1
    assert b["placeholder_inputs"] is False and b["keywords_used"] == 1; n += 1
    b2 = spend_band(r, vs)
    assert b2["keywords_missing"] == ["k2"]; n += 1
    # mixed top/bottom placement blends the CTR bands
    b3 = spend_band({"share_by_keyword": {"k1": 1.0}, "pos_by_keyword": {"k1": (1.0, 0.5)}}, vs)
    assert b3["low"] == round(100 * (0.5 * 0.05 + 0.5 * 0.005) * 2, 2); n += 1
    pp = os.path.join(d, "ph.csv")
    write_placeholder_csv(pp, ["k1"])
    b4 = spend_band({"share_by_keyword": {"k1": 1.0}, "pos_by_keyword": {}}, CsvVolumeSource(pp))
    assert b4["placeholder_inputs"] is True and b4["high"] > b4["low"] > 0; n += 1
    return n


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(f"intel.analysis: {selftest()} checks")
        print("ALL PASS")
