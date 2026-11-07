"""Weekly markdown report joining the FREE sources first, then the (optional) paid DataForSEO sections.

  1. Who beats us      <- Auction Insights import (auction_insights.py)
  2. What they say     <- Google Ads Transparency Center collector (transparency.py)
  3. Our own trend     <- own Google Ads API snapshots (ownstats.py)
  4. Competitor join   <- one row per configured competitor across all sources
  5. DataForSEO sections (top advertisers, new advertisers, copy changes): "paid source on hold" when no data.
  6. Calibration and assumptions.

    python3 -m adsops.intel.report --config CFG [--write] [--now ISO]

Prints the report; ``--write`` saves it as ``<report_dir>/WEEKLY-INTEL-<date>.md``. Slack posting is not
available in v0 (``--slack`` is refused).
"""
import argparse
import calendar
import os
import sys
import time

from . import analysis as A
from . import auction_insights as AI
from . import calibrate as K
from . import ownstats as OS
from . import transparency as TR
from . import config as C
from .store import Store, iso

WEEK = 7 * 86400


def _pct(x):
    return f"{100 * x:.0f}%"


def _band(b):
    if not b or not b["keywords_used"]:
        return "n/a (no volume data)"
    s = f"${b['low']:,.0f} to ${b['high']:,.0f}"
    return s + (" (PLACEHOLDER inputs)" if b["placeholder_inputs"] else "")


ON_HOLD = "_Paid source on hold: no DataForSEO data (founder 11-01: free sources first)._"


def _p(v, raw=None):
    if v is None:
        return raw if raw and raw != "--" else "-"
    return f"{100 * v:.0f}%"


def _day(t):
    return t[:10] if t else "-"


def sec_beaters(cfg, db, now):
    L = ["## Who beats us (Google Ads Auction Insights)", ""]
    rng = AI.latest_range(db, cfg["tenant"]) if _has(db, "ai_rows") else None
    if not rng:
        return L + ["_No Auction Insights imported yet. Import the Ads UI export or the pasted table with "
                    "`python3 -m adsops.intel.auction_insights import`._", ""]
    age = (now - calendar.timegm(time.strptime(rng[1], "%Y-%m-%d"))) / 86400
    L.append(f"Range {rng[0]} to {rng[1]}" + (f" (**{age:.0f} days old: re-export**)" if age > 14 else "") +
             ". `above us` = overlap x position-above rate: the share of our impressions in which they also "
             "showed above us. `<10%` is Google hiding values under 10%.")
    tid = cfg["tenant"]
    for level, title in (("campaign", "Campaign"), ("ad_group", "Ad group")):
        for scope in AI.scopes(db, tid, rng, level):
            me = AI.self_row(db, tid, rng, level, scope)
            top = AI.beaters(db, tid, rng, level, scope)[: (8 if level == "campaign" else 4)]
            if not top:
                continue
            ours = (f"us: IS {_p(me['impression_share'], me['impression_share_raw'])}, abs-top "
                    f"{_p(me['abs_top_of_page_rate'], me['abs_top_of_page_rate_raw'])}") if me else "us: not in table"
            L += ["", f"**{title} `{scope}`** ({ours})", "",
                  "| Competitor | Impr. share | Overlap | Position above | Abs. top | Outranking (us) | Above us |",
                  "|---|---|---|---|---|---|---|"]
            for b in top:
                L.append(f"| {b['domain']} | {_p(b['impression_share'], b['impression_share_raw'])} | "
                         f"{_p(b['overlap_rate'])} | {_p(b['position_above_rate'])} | {_p(b['abs_top_of_page_rate'])} | "
                         f"{_p(b['outranking_share'])} | {_p(b['above_us'])} |")
    return L + [""]


def sec_transparency(cfg, db, now):
    L = ["## What they say (Google Ads Transparency Center)", ""]
    advs = TR.latest(db, cfg["tenant"], cfg["competitors"] or None) if _has(db, "tr_advertisers") else []
    lr = TR.last_run(db, cfg["tenant"]) if _has(db, "tr_runs") else None
    if lr:
        L.append(f"Last run {lr['ts']}: {lr['queries_done']}/{lr['queries_total']} queries, {lr['requests']} live "
                 f"requests, {lr['cache_hits']} cache hits" +
                 (f", **stopped: {lr['stopped_reason']}**" if lr["stopped_reason"] else "") + ". "
                 "Counts are creatives in the first page (US-wide, not Tampa-only); not spend or impressions.")
    if not advs:
        return L + ["_No Transparency Center data yet._", ""]
    L += ["", "| Advertiser | Creatives (text / image / video) | First - last shown | Latest decoded ad text |",
          "|---|---|---|---|"]
    for a in advs:
        tx = "; ".join(f"\"{t['headline']}\"" + (f" / \"{t['description'][:90]}\"" if t["description"] else "")
                       for t in a["texts"][:1]) or "not decodable (responsive/image ad)"
        more = f" (of {a['total_reported']} reported)" if a["total_reported"] else ""
        L.append(f"| {a['name'] or a['advertiser_id']} ({a['domain'] or a['query']}) | {a['creatives']}{more} "
                 f"({a['text_n']} / {a['image_n']} / {a['video_n']}) | {_day(a['first_shown'])} - "
                 f"{_day(a['last_shown'])} | {tx} |")
    return L + [""]


def sec_own(cfg, db, now):
    L = ["## Our own trend (Google Ads API, weekly snapshots)", ""]
    dates = OS.snaps(db, cfg["tenant"], "os_adgroup", 6)
    if not dates:
        return L + ["_No own-stats snapshot yet (`python3 -m adsops.intel.ownstats snapshot`)._", ""]
    L.append(f"Snapshots (window ends): {', '.join(reversed(dates))}. Impression share per ad group, oldest to newest.")
    for title, col in (("Search impression share", "impression_share"), ("Lost to rank", "rank_lost_is"),
                       ("Abs. top impression share", "abs_top_is")):
        tr = OS.trend(db, cfg["tenant"], "os_adgroup", ["campaign", "ad_group"], col)
        L += ["", f"**{title}**", ""] + [f"- {k[1]} ({k[0]}): " + " > ".join(OS.share_label(v) for _, v in vals)
                                         for k, vals in sorted(tr.items())]
    tr = OS.trend(db, cfg["tenant"], "os_campaign", ["campaign"], "budget_lost_is")
    L += ["", "**Campaign lost to budget**", ""] + [f"- {k[0]}: " + " > ".join(OS.share_label(v) for _, v in vals)
                                                     for k, vals in sorted(tr.items())]
    last = dates[0]
    kw = db.execute("SELECT ad_group, keyword, quality_score, creative_qs, landing_qs, predicted_ctr, impressions, "
                    "impression_share, rank_lost_is FROM os_keyword WHERE tenant=? AND snap_date=? AND "
                    "quality_score IS NOT NULL ORDER BY quality_score, impressions DESC LIMIT 8", (cfg["tenant"], last)).fetchall()
    if kw:
        L += ["", f"**Lowest quality scores ({last})**", "",
              "| Ad group | Keyword | QS | Ad relevance | Landing page | Expected CTR | Impr. | IS | Lost to rank |",
              "|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {k[0]} | {k[1]} | {k[2]} | {k[3] or '-'} | {k[4] or '-'} | {k[5] or '-'} | {k[6]} | "
              f"{OS.share_label(k[7])} | {OS.share_label(k[8])} |" for k in kw]
    return L + [""]


def sec_join(cfg, db, now, week):
    comp = cfg["competitors"]
    if not comp:
        return []
    rng = AI.latest_range(db, cfg["tenant"]) if _has(db, "ai_rows") else None
    ai = {}
    if rng:
        for lv in ("campaign",):
            for scope in AI.scopes(db, cfg["tenant"], rng, lv):
                for b in AI.beaters(db, cfg["tenant"], rng, lv, scope):
                    ai.setdefault(b["domain"], b)
    advs = TR.latest(db, cfg["tenant"], comp) if _has(db, "tr_advertisers") else []
    L = ["## Competitor join (all sources)", "",
         "| Competitor | Auction Insights IS | Above us | Transparency creatives | Last shown | DataForSEO share |",
         "|---|---|---|---|---|---|"]
    for d in comp:
        b = ai.get(d)
        tr = [a for a in advs if (a["domain"] or "").endswith(d) or a["query"] == d]
        cr = sum(a["creatives"] for a in tr)
        ls = max((a["last_shown"] or "" for a in tr), default="")
        w = week.get(d)
        L.append(f"| {d} | {_p(b['impression_share'], b['impression_share_raw']) if b else 'not in table'} | "
                 f"{_p(b['above_us']) if b else '-'} | {cr if tr else 'none found'} | {_day(ls)} | "
                 f"{_pct(w['share']) if w else 'paid source on hold'} |")
    return L + [""]


def _has(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def build(cfg, store, now=None, top_n=15):
    now = now or time.time()
    since = iso(now - WEEK)
    until = iso(now + 1)
    month = iso(now)[:7]
    s_week, o_week = store.samples(since, until), store.observations(since, until)
    s_all, o_all = store.samples(None, until), store.observations(None, until)
    week = A.competitor_stats(s_week, o_week, cfg["our_domains"])
    alltime = A.competitor_stats(s_all, o_all, cfg["our_domains"])
    vols = A.CsvVolumeSource(cfg.get("volume_csv"))
    ctr = A.load_ctr(cfg.get("ctr_table"))
    bands = {d: A.spend_band(st, vols, ctr) for d, st in week.items()}
    L = [f"# AdRails Intel weekly: {cfg['tenant']} ({iso(now)[:10]})", "",
         "Free sources: Google Ads Auction Insights, Google Ads Transparency Center, our own Google Ads API stats. "
         "Paid source (DataForSEO SERP sampling): " + ("**on hold**" if not s_all else "active") + ".", ""]
    db = store.db
    L += sec_beaters(cfg, db, now) + sec_transparency(cfg, db, now) + sec_own(cfg, db, now) + sec_join(cfg, db, now, week)
    L += ["## DataForSEO coverage and spend", ""]
    if not s_all:
        L += [ON_HOLD, ""]
    else:
        L += [f"Window: {since} to {iso(now)}. {len(cfg['keywords'])} keywords x {len(cfg['locations'])} locations "
              f"x {len(cfg['devices'])} devices.", "",
              f"- SERP samples this week: {len(s_week)} ({len({s['keyword'] for s in s_week})} distinct keywords); "
              f"all time: {len(s_all)}",
              f"- Data spend {month}: ${store.month_spend(month):.2f} of the ${cfg['monthly_cap_usd']:.2f} hard cap", ""]
        if not s_week:
            L += ["**No samples this week.** Nothing below is evidence of anything.", ""]
    L += ["## Top advertisers this week (DataForSEO)", ""]
    ranked = sorted(week.values(), key=lambda s: (-s["share"], s["domain"]))[:top_n]
    if not ranked:
        L += [ON_HOLD, ""]
    else:
        L += ["| Domain | Appearance share | Top of page | Avg ad rank | Keywords seen | Est. monthly spend band |",
              "|---|---|---|---|---|---|"]
        for s in ranked:
            name = s["domain"] + (" (us)" if s["ours"] else "")
            ap = f"{s['avg_position']:.1f}" if s["avg_position"] else "-"
            L.append(f"| {name} | {_pct(s['share'])} | {_pct(s['top_share'])} | {ap} | {len(s['share_by_keyword'])} "
                     f"| {_band(bands[s['domain']])} |")
    new = sorted(d for d, s in alltime.items() if s["first_seen"] and s["first_seen"] >= since)
    L += ["", "## New advertisers since last week (DataForSEO)", ""]
    L += ([ON_HOLD] if not s_all else []) + [f"- **{d}**: first seen {alltime[d]['first_seen']}, share this week {_pct(week[d]['share'])}, "
          f"landing {next(iter(alltime[d]['landing_urls']), '-')}" for d in new] or ([] if not s_all else ["- none"])
    L += ["", "## Ad copy changes (DataForSEO; new copy from advertisers seen before this week)", ""]
    changes = []
    for d, s in sorted(alltime.items()):
        if s["first_seen"] >= since:
            continue
        for c in s["copies"].values():
            if c["first"] >= since:
                changes.append(f"- **{d}** ({c['first'][:10]}): \"{c['headline']}\" / \"{c['description']}\"")
    L += changes or ([ON_HOLD] if not s_all else ["- none"])
    L += ["", "## Calibration", ""]
    ours = [bands[d] for d in cfg["our_domains"] if d in bands]
    if cfg.get("known_budget_usd_month"):
        b = ours[0] if ours else None
        r = K.check_known_budget(b, cfg["known_budget_usd_month"])
        L.append(f"- Own-domain blind test ({', '.join(cfg['our_domains'])}): known budget "
                 f"${cfg['known_budget_usd_month']:,.0f}/mo, our band {_band(b)}: **{r['verdict']}**")
    sp = K.load_spyfu_csv(cfg.get("spyfu_csv"))
    if sp:
        for r in K.compare_spyfu(bands, sp):
            L.append(f"- SpyFu benchmark {r['domain']}: SpyFu ${r['spyfu']:,.0f}, ours "
                     f"{_band(bands.get(r['domain']))}: {r['verdict']}")
    else:
        L.append("- SpyFu benchmark: no CSV yet (calibration only, hand-typed, never ingested)")
    L += ["", "## Assumptions (read before quoting a number)", "",
          f"- CTR by ad rank: {ctr.get('source', 'custom table')}",
          "- Spend band = sum over keywords of volume x appearance share x CTR(rank, top/bottom) x CPC, "
          "low inputs to high inputs. It is a range by design; never quote the midpoint as a fact.",
          f"- Volume/CPC: {'CSV ' + os.path.basename(cfg['volume_csv']) if cfg.get('volume_csv') else 'none configured'}"
          f"{' (contains PLACEHOLDER rows: bands are not real estimates)' if any(b['placeholder_inputs'] for b in bands.values()) else ''}.",
          "- Auction Insights values under 10% are hidden by Google (`<10%`); competitor rows are as of the "
          "imported export, and the Ads UI shows only advertisers above Google's activity threshold.",
          "- Transparency Center counts are creatives (first page, US-wide), not spend or impressions; responsive "
          "ads usually have no decodable text.",
          "- Top of page is inferred (paid item ranked above the first organic result); DataForSEO has no explicit field.",
          "- Share is measured at the hours we sample. Advertisers with an ad schedule (dayparting) are "
          "under-counted outside their hours, and the band inherits that bias.", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adsops.intel.report")
    ap.add_argument("--config")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--now")
    ap.add_argument("--slack", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print(f"intel.report: {selftest()} checks")
        print("ALL PASS")
        return 0
    if a.slack:
        print("slack posting is not available in v0", file=sys.stderr)
        return 2
    if not a.config:
        ap.error("--config is required")
    cfg = C.load(a.config)
    now = calendar.timegm(time.strptime(a.now, "%Y-%m-%dT%H:%M:%SZ")) if a.now else time.time()
    md = build(cfg, Store(cfg["db_path"], cfg["tenant"]), now)
    if a.write:
        os.makedirs(cfg["report_dir"], exist_ok=True)
        p = os.path.join(cfg["report_dir"], f"WEEKLY-INTEL-{iso(now)[:10]}.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(p)
    else:
        print(md)
    return 0


def selftest():
    import tempfile
    n = 0
    d = tempfile.mkdtemp()
    vol = os.path.join(d, "v.csv")
    A.write_placeholder_csv(vol, ["k1", "k2"])
    cfg = C.validate(C.sample(keywords=["k1", "k2"], our_domains=["acmepm.example"], volume_csv=vol,
                              known_budget_usd_month=750, report_dir=d), d)
    s = Store(":memory:", cfg["tenant"])
    now = calendar.timegm(time.strptime("2026-11-09T12:00:00", "%Y-%m-%dT%H:%M:%S"))
    old, new = iso(now - 10 * 86400), iso(now - 86400)

    def ad(dom, rank=1, top=1, h="H"):
        return {"ad_rank": rank, "rank_absolute": rank, "top_of_page": top, "domain": dom, "display_url": dom,
                "headline": h, "description": "D", "landing_url": f"https://{dom}/"}
    s.add_sample("o1", old, "k1", "tampa", "mobile", [ad("rival.com")])
    s.add_sample("w1", new, "k1", "tampa", "mobile", [ad("rival.com", h="New headline"), ad("acmepm.example", 2)])
    s.add_sample("w2", new, "k2", "tampa", "desktop", [ad("newcomer.com")])
    md = build(cfg, s, now)
    assert "| rival.com | 50% |" in md and "| acmepm.example (us) | 50% |" in md; n += 1
    assert "**newcomer.com**" in md and "**rival.com**" not in md.split("## New advertisers")[1].split("##")[0]; n += 1
    assert '"New headline"' in md and md.count("(2026-11-08)") == 1; n += 1
    assert "PLACEHOLDER inputs" in md and "Own-domain blind test" in md; n += 1
    assert "SpyFu benchmark: no CSV yet" in md; n += 1
    empty = build(cfg, Store(":memory:", cfg["tenant"]), now)
    assert empty.count("Paid source on hold") >= 4 and "| (none) |" not in empty; n += 1
    assert "No Auction Insights imported yet" in empty and "No Transparency Center data yet" in empty \
        and "No own-stats snapshot yet" in empty; n += 1
    assert "DataForSEO coverage" in md and "Paid source (DataForSEO SERP sampling): active" in md; n += 1

    # free sources joined (fixtures + mocked clients, no network)
    import json
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    cfg2 = C.validate(C.sample(keywords=["k1"], our_domains=["acmepm.example"], report_dir=d,
                               competitors=["collabregtampa.com", "competitor_example.com", "nodata.com"],
                               ownstats={"campaigns": ["acme_search"]},
                               transparency={"cache_dir": os.path.join(d, "tc")}), d)
    fs = Store(":memory:", cfg2["tenant"])
    AI.import_text(fs, cfg2, open(os.path.join(fx, "auction_insights_pasted.txt"), encoding="utf-8").read(), "fx")
    rec = json.load(open(os.path.join(fx, "transparency_creatives_competitor_example.json")))
    TR.run(cfg2, fs, live=True, http=lambda u, f: (200, None, json.dumps(rec)), sleep=lambda x: None, now=now,
           only="competitor_example.com", log=lambda *_: None)
    OS.snapshot(cfg2, fs, OS._FakeClient({
        "FROM campaign": [{"campaign": {"name": "acme_search"}, "metrics": {"searchBudgetLostImpressionShare": 0.3,
                                                                           "searchImpressionShare": 0.17}}],
        "FROM ad_group": [{"campaign": {"name": "acme_search"}, "adGroup": {"name": "owner_intent"},
                           "metrics": {"searchImpressionShare": 0.135, "searchRankLostImpressionShare": 0.59}}],
        "quality_info": [], "FROM keyword_view": []}), now)
    fr = build(cfg2, fs, now)
    if os.environ.get("INTEL_SHOW_REPORT"):
        print(fr)
    assert "## Who beats us" in fr and "| collabregtampa.com | 14% | 28% | 72% |" in fr, fr[:1500]; n += 1
    assert "**Ad group `tampa_core`**" in fr and "<10%" in fr and "hidden" in fr; n += 1
    assert "Real Property Management Tampa St. Pete" in fr and "Property Managers Tampa" in fr \
        and "(6 / 3 / 0)" in fr; n += 1
    assert "owner_intent (acme_search): 14%" in fr and "acme_search: 30%" in fr; n += 1
    jn = fr.split("## Competitor join")[1].split("##")[0]
    assert "| collabregtampa.com | 14% | 20% | none found |" in jn, jn; n += 1
    assert "| competitor_example.com | not in table | - | 9 |" in jn and "| nodata.com | not in table | - | none found | - | paid source on hold |" in jn, jn; n += 1
    assert main(["--slack"]) == 2; n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
