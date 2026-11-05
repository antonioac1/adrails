"""Calibration hooks. CALIBRATION ONLY: nothing here feeds estimates back into the store.

(a) Known-budget check: our own domain's band must contain the real monthly budget (Acme: $X/mo)
    once real samples exist. A miss tells us which way the assumptions are off.
(b) SpyFu benchmark: a hand-made CSV ``domain,est_spend`` typed from one month of SpyFu Basic for ~10
    competitors. Compared row by row and never ingested elsewhere (SpyFu's ToS forbid bulk extraction).
"""
import csv
import os
import sys


def check_known_budget(band, known_usd):
    """-> {verdict: inside|below|above|no-data, ratio_mid}."""
    if not band or band.get("keywords_used", 0) == 0 or band["high"] <= 0:
        return {"verdict": "no-data", "known_usd": known_usd, "band": band}
    verdict = "inside" if band["low"] <= known_usd <= band["high"] else ("below" if known_usd < band["low"] else "above")
    mid = (band["low"] + band["high"]) / 2
    return {"verdict": verdict, "known_usd": known_usd, "low": band["low"], "high": band["high"],
            "ratio_mid": round(mid / known_usd, 2) if known_usd else None,
            "placeholder_inputs": band.get("placeholder_inputs", False)}


def load_spyfu_csv(path):
    rows = {}
    if path and os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows[r["domain"].strip().lower().removeprefix("www.")] = float(r["est_spend"])
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
    return rows


def compare_spyfu(bands, spyfu):
    """bands: {domain: band}. -> rows sorted by domain: inside/below/above our band, ratio spyfu/mid."""
    out = []
    for dom in sorted(spyfu):
        b = bands.get(dom)
        est = spyfu[dom]
        if not b or b.get("keywords_used", 0) == 0:
            out.append({"domain": dom, "spyfu": est, "verdict": "not-observed"})
            continue
        mid = (b["low"] + b["high"]) / 2
        v = "inside" if b["low"] <= est <= b["high"] else ("below" if est < b["low"] else "above")
        out.append({"domain": dom, "spyfu": est, "low": b["low"], "high": b["high"], "verdict": v,
                    "spyfu_over_mid": round(est / mid, 2) if mid else None})
    return out


def selftest():
    import tempfile
    n = 0
    assert check_known_budget({"low": 500, "high": 1200, "keywords_used": 3}, 750)["verdict"] == "inside"; n += 1
    r = check_known_budget({"low": 100, "high": 300, "keywords_used": 3}, 750)
    assert r["verdict"] == "above" and r["ratio_mid"] == round(200 / 750, 2); n += 1
    assert check_known_budget({"low": 900, "high": 2000, "keywords_used": 1}, 750)["verdict"] == "below"; n += 1
    assert check_known_budget({"low": 0, "high": 0, "keywords_used": 0}, 750)["verdict"] == "no-data"; n += 1
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.csv")
    with open(p, "w") as fh:
        fh.write("domain,est_spend\nwww.Rival.com,1000\nq.com,50\nz.com,abc\n")
    sp = load_spyfu_csv(p)
    assert sp == {"rival.com": 1000.0, "q.com": 50.0}; n += 1
    rows = compare_spyfu({"rival.com": {"low": 400, "high": 1600, "keywords_used": 2}}, sp)
    assert rows[0]["verdict"] == "not-observed" and rows[1]["verdict"] == "inside" and rows[1]["spyfu_over_mid"] == 1.0; n += 1
    assert load_spyfu_csv(os.path.join(d, "missing.csv")) == {}; n += 1
    return n


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(f"intel.calibrate: {selftest()} checks")
        print("ALL PASS")
