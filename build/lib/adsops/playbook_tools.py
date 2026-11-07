"""Playbook tools: adsops_negatives_suggest, adsops_competitors_report, adsops_auction_insights_import, adsops_lp_diagnose.

None of these can change a Google Ads account. negatives_suggest and lp_diagnose are live READS (GAQL search only);
competitors_report reads the local intel database; auction_insights_import writes only that local SQLite file.
The one way to change an account stays the committed desired-state file -> adsops_plan -> adsops_apply.

Optional config, all additive (nothing is required):
  top level   "intel_config": "/path/to/tenant.intel.json"          (AdRails Intel tenant file)
  per campaign "negatives": {"protect": [...], "out_of_area": [...], "competitor_brands": [...],
                             "home_state": "fl", "llm_command": ["/path/to/local-model-script"]}
  per campaign "landing": {"towns": ["Tampa", "New Port Richey"]}
"""
import datetime as dt
import os

from adsops import lpdiag, negatives
from adsops.errors import ToolError
from adsops.platforms import google_ads

FETCH = None            # tests replace this; None = lpdiag.http_fetch
MAX_IMPORT_CHARS = 2_000_000


def _intel(cfg):
    p = cfg.get("intel_config")
    if not p:
        raise ToolError("no AdRails Intel tenant configured: set 'intel_config' in the adsops config "
                        "to the path of your tenant .intel.json")
    from adsops.intel import config as ic
    return ic.load(os.path.expanduser(p))


def register(srv, ctx, cfg):
    cprop = {"campaign": {"type": "string", "description": "campaign key from adsops_campaigns"}}

    def schema(props, req=("campaign",)):
        return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}

    @srv.tool("adsops_negatives_suggest",
              "Read-only. Classify the campaign's recent search terms (renter, job, HOA/condo, vacation, DIY/software, "
              "out-of-area, competitor brand) with deterministic rules plus an optional local hook, and propose PHRASE "
              "negatives with cost/click evidence. Skips terms and phrases already covered by existing negatives, never "
              "suggests protected brand/core-service words. Returns a negatives_add patch and a basis note to paste into "
              "the desired-state file; it changes nothing.",
              schema({**cprop, "days": {"type": "integer", "description": "1-90, default 30"},
                      "min_cost": {"type": "number", "description": "ignore suggestions whose total cost is below this (default 0)"}}))
    def negatives_suggest(args):
        days = args.get("days", 30)
        if not 1 <= days <= 90:
            raise ToolError("days must be 1..90")
        _, camp, raw, eff, _, client = ctx.open(args["campaign"])
        settings = dict(camp.get("negatives") or {})
        rows = google_ads.search_terms(client, eff["campaign_id"], days)
        existing = [(n["text"], n.get("match_type", "PHRASE")) for n in (eff.get("negatives_add") or [])]
        note = None
        try:
            live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
            existing += [(t, m) for t, m in live["negatives"]]
        except ToolError as e:
            note = f"live negatives could not be read ({e}); coverage was checked against the desired-state file only"
        llm = negatives.run_llm_command(settings["llm_command"]) if settings.get("llm_command") else None
        res = negatives.suggest(rows, existing, settings, days, llm)
        floor = float(args.get("min_cost", 0))
        if floor:
            res["suggestions"] = [s for s in res["suggestions"] if s["cost"] >= floor]
            res["negatives_add_patch"] = negatives.patch_for(res)
        if note:
            res["warning"] = note
        if not settings.get("protect"):
            res["warning"] = (res.get("warning", "") + " No protect list is configured for this campaign "
                              "(negatives.protect): add your brand and core service words.").strip()
        return {"campaign": args["campaign"], "days": days, "terms_seen": len(rows),
                "existing_negatives": len(existing), **res}

    @srv.tool("adsops_competitors_report",
              "Read-only. The AdRails Intel weekly report from the FREE sources: Auction Insights already imported "
              "into the local database, the cached Transparency Center data, and our own Google Ads stats snapshots. "
              "Returns markdown. Makes no network call and spends nothing.",
              schema({}, ()))
    def competitors_report(args):
        from adsops.intel import report
        from adsops.intel.store import Store
        icfg = _intel(cfg)
        md = report.build(icfg, Store(icfg["db_path"], icfg["tenant"]))
        return {"tenant": icfg["tenant"], "markdown": md, "sources": ["auction_insights (local db)",
                "transparency (cache)", "own stats (local snapshots)"]}

    @srv.tool("adsops_auction_insights_import",
              "Import a Google Ads Auction Insights export (CSV/TSV text) or a pasted table into the local intel "
              "database, replacing rows for the same date range. Writes only the local SQLite file.",
              schema({"text": {"type": "string", "description": "the CSV/TSV contents or the pasted table"},
                      "level": {"type": "string", "enum": ["campaign", "ad_group", "keyword"]},
                      "campaign": {"type": "string"},
                      "range": {"type": "string", "description": "e.g. 2026-10-07..2026-10-31, when the text has no dates"}},
                     ("text",)))
    def ai_import(args):
        from adsops.intel import auction_insights as ai
        from adsops.intel.store import Store
        text = args["text"]
        if len(text) > MAX_IMPORT_CHARS:
            raise ToolError("text is larger than 2,000,000 characters")
        icfg = _intel(cfg)
        st = Store(icfg["db_path"], icfg["tenant"])
        st.db.executescript(ai.SCHEMA)
        try:
            rng = ai.parse_range(args["range"]) if args.get("range") else None
            if args.get("range") and not rng:
                raise ToolError("could not read 'range'; use 2026-10-07..2026-10-31")
            n = ai.import_text(st, icfg, text, "adsops_auction_insights_import", args.get("level"),
                               args.get("campaign"), rng)
        except ai.ImportError_ as e:
            raise ToolError(f"import failed: {e}") from None
        return {"tenant": icfg["tenant"], "rows_stored": n, "range": list(ai.latest_range(st.db, icfg["tenant"]) or [])}

    @srv.tool("adsops_lp_diagnose",
              "Read-only. Per ad group: Google's quality-score components (expected CTR, ad relevance, landing page "
              "experience) plus a plain HTTP check of each final URL (status, response time, bytes, whether the title/H1 "
              "echo the ad group's keywords and town). Returns plain-English findings, worst first. No browser, "
              "no JavaScript; changes nothing.",
              schema({**cprop, "towns": {"type": "array", "items": {"type": "string"},
                                         "description": "target towns to look for in the title/H1 (default: campaign config 'landing.towns')"},
                      "max_urls": {"type": "integer", "description": "1-50, default 25"}}))
    def lp_diagnose(args):
        _, camp, _, eff, _, client = ctx.open(args["campaign"])
        mx = args.get("max_urls", 25)
        if not 1 <= mx <= 50:
            raise ToolError("max_urls must be 1..50")
        towns = args.get("towns") or (camp.get("landing") or {}).get("towns") or []
        groups = lpdiag.read_account(client, eff["campaign_id"])
        res = lpdiag.diagnose(groups, FETCH or lpdiag.http_fetch, towns, mx)
        return {"campaign": args["campaign"], "towns_checked": towns, **res}

    return ["adsops_negatives_suggest", "adsops_competitors_report", "adsops_auction_insights_import", "adsops_lp_diagnose"]


def selftest():
    import json
    import tempfile

    from adsops import protocol
    from adsops.intel import config as ic
    n = 0
    d = tempfile.mkdtemp()
    fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "acme_negatives_2026_11_01.json")))
    calls = []

    class Client:
        def search(self, q):
            calls.append(q)
            if "search_term_view" in q:
                return [{"searchTermView": {"searchTerm": t["term"]}, "segments": {"keyword": {"info": {"text": t["matched"]}}},
                         "metrics": {"clicks": str(t["clicks"]), "costMicros": str(int(t["cost"] * 1e6))}} for t in fx["terms"]]
            if "campaign_criterion" in q:     # live negatives: baseline minus two, to prove the state file is also consulted
                return [{"campaignCriterion": {"keyword": {"text": t, "matchType": m}}} for t, m in fx["existing"][:-2]]
            if "keyword_view" in q:
                return [{"adGroup": {"id": "1", "name": "Tampa"}, "adGroupCriterion": {"keyword": {"text": "property management tampa"},
                         "qualityInfo": {"qualityScore": 7, "creativeQualityScore": "AVERAGE",
                                         "postClickQualityScore": "BELOW_AVERAGE", "searchPredictedCtr": "AVERAGE"}}}]
            if "ad_group_ad" in q:
                return [{"adGroup": {"id": "1", "name": "Tampa"}, "adGroupAd": {"ad": {"finalUrls": ["https://ok.example/"]}}}]
            return [{"campaign": {"id": "42", "name": "c", "status": "ENABLED", "campaignBudget": "x"},
                     "campaignBudget": {"amountMicros": "1000000"}, "metrics": {"costMicros": "0"}}]

    class Ctx:
        def open(self, key):
            if key != "c42":
                raise ToolError("unknown campaign key: " + key)
            camp = {"key": "c42", "negatives": fx["settings"], "landing": {"towns": ["Tampa"]}}
            eff = {"campaign_id": "42", "negatives_add": [{"text": t, "match_type": m} for t, m in fx["existing"]]}
            return {}, camp, eff, eff, None, Client()

        def end_field(self):
            return "end_date_time"

    ic_path = os.path.join(d, "t.intel.json")
    json.dump(ic.sample(tenant="t1", our_domains=["acmepm.example"], db_path=os.path.join(d, "intel.sqlite"), report_dir=d), open(ic_path, "w"))
    cfg = {"tenant": "t1", "intel_config": ic_path}
    srv = protocol.Server("t", "0")
    names = register(srv, Ctx(), cfg)

    def call(name, args=None, c=None):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
        return r["result"]

    listed = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert listed == names; n += 1
    # negatives_suggest: state file + live list both consulted; the Acme baseline yields the six; read-only GAQL only
    r = call("adsops_negatives_suggest", {"campaign": "c42", "days": 30})["structuredContent"]
    assert sorted(s["phrase"] for s in r["suggestions"]) == sorted(fx["expected_new"]) and r["terms_seen"] == len(fx["terms"]); n += 1
    assert all(q.lstrip().upper().startswith("SELECT") for q in calls) and "warning" not in r; n += 1
    assert r["negatives_add_patch"]["negatives_add"] and list(r["basis_note"])[0].startswith("_negatives_basis_"); n += 1
    assert call("adsops_negatives_suggest", {"campaign": "c42", "days": 500})["isError"]; n += 1
    assert call("adsops_negatives_suggest", {"campaign": "nope"})["isError"]; n += 1
    r = call("adsops_negatives_suggest", {"campaign": "c42", "min_cost": 5})["structuredContent"]
    assert [s["phrase"] for s in r["suggestions"]] == ["condominium"]; n += 1
    # lp_diagnose with the fetch mocked
    global FETCH
    FETCH = lambda u: {"status": 200, "ms": 300, "bytes": 90, "html": "<title>Tampa Property Management</title><h1>Property management in Tampa</h1>", "final_url": u}
    try:
        r = call("adsops_lp_diagnose", {"campaign": "c42"})["structuredContent"]
        assert r["towns_checked"] == ["Tampa"] and r["ad_groups"][0]["components"]["landing_page_experience"]["worst"] == "BELOW_AVERAGE"; n += 1
        assert r["findings"][0]["rank"] == 1 and "landing page experience" in r["findings"][0]["issue"]; n += 1
        assert call("adsops_lp_diagnose", {"campaign": "c42", "max_urls": 99})["isError"]; n += 1
    finally:
        FETCH = None
    # intel tools: import a pasted Auction Insights table, then the report reads it back
    pasted = open(os.path.join(os.path.dirname(__file__), "intel", "fixtures", "auction_insights_pasted.txt")).read()
    r = call("adsops_auction_insights_import", {"text": pasted, "range": "2026-10-07..2026-10-31"})["structuredContent"]
    assert r["rows_stored"] > 5 and r["range"] == ["2026-10-07", "2026-10-31"]; n += 1
    assert call("adsops_auction_insights_import", {"text": "nothing useful here"})["isError"]; n += 1
    assert call("adsops_auction_insights_import", {"text": pasted, "range": "soon"})["isError"]; n += 1
    r = call("adsops_competitors_report")["structuredContent"]
    assert r["markdown"].startswith("# AdRails Intel weekly: t1") and "collabregtampa.com" in r["markdown"]; n += 1
    # without an intel tenant the intel tools say what to configure, not a traceback
    srv2 = protocol.Server("t", "0")
    register(srv2, Ctx(), {"tenant": "t1"})
    rr = srv2.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "adsops_competitors_report", "arguments": {}}})["result"]
    assert rr["isError"] and "intel_config" in rr["content"][0]["text"]; n += 1
    return n


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        print(f"playbook_tools: {selftest()} checks")
        print("ALL PASS")
