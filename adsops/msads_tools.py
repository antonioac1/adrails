"""MCP tools for Microsoft Advertising (Bing Ads): adsops_msads_*. READ-ONLY by design.

The point: an agent (a monitor, a reporting bot, a second-opinion model) can watch Microsoft Ads delivery and stats in
detail and cannot change or spend anything. There is no write tool here, and the client underneath has no write method.

Registered only when the config has a `microsoft_ads` block:
    "microsoft_ads": {
      "developer_token": {"kind": "env", "name": "MSADS_DEVELOPER_TOKEN"},         # file | env | 1password
      "auth": {"provider": "google",                                              # or "microsoft"
               "client_id": "<OAuth client id>",
               "client_secret": {"kind": "env", "name": "MSADS_GOOGLE_CLIENT_SECRET"}, # google only
               "redirect_uri": "http://localhost:8765/oauth/callback",              # google only, as registered
               "refresh_token_file": "~/.adsops/msads-refresh-token"},
      "accounts": ["G123ABCD"]                                                     # optional allow-list (account numbers), or
                                                                                   # pinned [{"number","account_id","customer_id"}]
    }
Sign in once with `adrails msads-auth`.
"""
import datetime as dt

from .errors import ToolError
from .platforms import microsoft_ads as ms

ACCOUNT = {"account": {"type": "string", "description": "Microsoft Ads account number (e.g. G123ABCD) or id; default: the first one"}}
CAMPAIGN = {"campaign_id": {"type": "string", "description": "campaign id from adsops_msads_campaigns"}}
WINDOW = {"days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "trailing days including today (default 7)"},
          "day_from": {"type": "string", "description": "YYYY-MM-DD (overrides days)"},
          "day_to": {"type": "string", "description": "YYYY-MM-DD (default today)"}}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def build_client(cfg, http=None, sleep=None):
    m = cfg.get("microsoft_ads") or {}
    a = m.get("auth") or {}
    secret = ms.secret_loader(a["client_secret"], "auth.client_secret") if a.get("provider") == "google" else None
    kw = {"http": http} if http else {}
    tokens = ms.Tokens(a.get("provider"), a.get("client_id"), a.get("refresh_token_file") or "~/.adsops/msads-refresh-token",
                       client_secret=secret, **kw)
    ckw = {"http": http} if http else {}
    if sleep:
        ckw["sleep"] = sleep
    return ms.MicrosoftAdsClient(tokens, ms.secret_loader(m.get("developer_token"), "developer_token"), **ckw)


def resolve_accounts(cfg, client):
    """The accounts to read, shared by the tools and adsops_metrics_pull. Pinned objects -> used as-is (no discovery call);
    plain numbers/ids -> an allow-list over the accounts this sign-in can see; empty -> every visible account."""
    entries = (cfg.get("microsoft_ads") or {}).get("accounts") or []
    pinned = [x for x in entries if isinstance(x, dict)]
    if pinned:
        return [{"account_id": str(x["account_id"]), "number": x.get("number") or str(x["account_id"]), "name": x.get("name"),
                 "customer_id": str(x["customer_id"]), "status": None} for x in pinned]
    allow = [str(x).upper() for x in entries]
    return [a for a in client.accounts() if not allow or str(a["number"]).upper() in allow or a["account_id"] in allow]


def window(args, today):
    if args.get("day_from"):
        lo, hi = args["day_from"], args.get("day_to") or today.isoformat()
    else:
        days = int(args.get("days") or 7)
        lo, hi = (today - dt.timedelta(days=days - 1)).isoformat(), today.isoformat()
    try:
        if dt.date.fromisoformat(lo) > dt.date.fromisoformat(hi):
            raise ToolError("day_from is after day_to")
    except ValueError:
        raise ToolError("dates must be YYYY-MM-DD") from None
    return lo, hi


STOPPED = {"Suspended": "suspended by Microsoft (usually billing or policy): it cannot serve",
           "BudgetPaused": "paused because its budget ran out",
           "BudgetAndManualPaused": "paused (budget ran out and manually paused)"}


def health_findings(campaigns, tree, perf_by_campaign, today_spend, account=None):
    """Pure: everything that would stop a campaign from serving, or waste its money. Each finding has a severity.
    Structure checks run for SEARCH campaigns only (Performance Max has no ad groups; Shopping/Audience ad groups have
    product/image ads, not text ads): checking them the same way would raise a false critical every day."""
    out = []
    if account and account.get("status") not in (None, "Active"):
        out.append({"severity": "critical", "campaign": None,
                    "issue": f"account is {account.get('status')}" + (f" ({account.get('pause_reason')})" if account.get("pause_reason") else "")
                    + ": nothing in it can serve (check billing / payment method)"})
    for c in campaigns:
        cid, name = c["campaign_id"], c["name"]
        if c["status"] in STOPPED:
            out.append({"severity": "critical", "campaign": name, "issue": "campaign is " + STOPPED[c["status"]]})
            continue
        if c["status"] != "Active" or (c.get("type") or "Search") != "Search":
            continue
        t = tree.get(cid) or {"ad_groups": []}
        live_groups = [g for g in t["ad_groups"] if g["status"] == "Active"]
        if not live_groups:
            out.append({"severity": "critical", "campaign": name, "issue": "campaign is Active but has no Active ad group: nothing can serve"})
        for g in live_groups:
            kws = g.get("keywords") or []
            ads = g.get("ads") or []
            ok_kw = [k for k in kws if k["status"] == "Active" and k.get("editorial_status") not in ("Disapproved", "Inactive")]
            ok_ad = [a for a in ads if a["status"] == "Active" and a.get("editorial_status") not in ("Disapproved", "Inactive")]
            if kws and not ok_kw:
                out.append({"severity": "critical", "campaign": name, "ad_group": g["name"], "issue": "no serving keyword (all paused or disapproved)"})
            if not ok_ad:
                out.append({"severity": "critical", "campaign": name, "ad_group": g["name"], "issue": "no serving ad (all paused or disapproved)"})
            bad = [k["text"] for k in kws if k.get("editorial_status") == "Disapproved"]
            if bad:
                out.append({"severity": "warning", "campaign": name, "ad_group": g["name"], "issue": f"{len(bad)} keyword(s) disapproved", "examples": bad[:5]})
            badads = [a["ad_id"] for a in ads if a.get("editorial_status") == "Disapproved"]
            if badads:
                out.append({"severity": "warning", "campaign": name, "ad_group": g["name"], "issue": f"{len(badads)} ad(s) disapproved", "ad_ids": badads})
            if g.get("network") == "OwnedAndOperatedOnly":
                out.append({"severity": "info", "campaign": name, "ad_group": g["name"], "issue": "Bing-owned sites only (syndicated search partners off)"})
        negs = t.get("negatives") or []
        pos = {k["text"].lower() for g in live_groups for k in (g.get("keywords") or []) if k["status"] == "Active"}
        clash = []
        for n in negs:
            nt, nm = (n.get("text") or "").lower(), n.get("match_type")
            for p in pos:
                if (nm == "Exact" and p == nt) or (nm != "Exact" and f" {nt} " in f" {p} "):
                    clash.append((n.get("text"), p))
        if clash:
            out.append({"severity": "warning", "campaign": name, "issue": f"{len(clash)} negative keyword(s) block your own keywords",
                        "examples": [f"-{a} blocks '{b}'" for a, b in clash[:5]]})
        p = perf_by_campaign.get(name)
        if p is not None and p["Impressions"] == 0:
            out.append({"severity": "warning", "campaign": name,
                        "issue": "Active but 0 impressions in the window (new campaigns can take hours; check bids, budget, approvals, payment)"})
        spend = today_spend.get(name)
        if spend is not None and c.get("daily_budget"):
            pct = spend / float(c["daily_budget"])
            if pct >= 1.0:
                out.append({"severity": "info", "campaign": name, "issue": f"today's spend ${spend:.2f} reached the ${c['daily_budget']}/day budget "
                            "(Microsoft can deliver up to 2x on a single day, averaged over the month)"})
        if not c.get("final_url_suffix"):
            out.append({"severity": "info", "campaign": name, "issue": "no final URL suffix: leads will not carry utm/campaign ids into your CRM"})
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["severity"]])


def tz_of(campaigns):
    """The account's report time zone = its campaigns' TimeZone (same enum as ReportTimeZone); None -> Microsoft's default."""
    zones = {c.get("time_zone") for c in campaigns if c.get("time_zone")}
    return zones.pop() if len(zones) == 1 else None


def register(srv, ctx, cfg, client_factory=None):
    if not cfg.get("microsoft_ads"):
        return None
    allow = [str(x).upper() for x in (cfg["microsoft_ads"].get("accounts") or []) if not isinstance(x, dict)]
    state = {}

    def client():
        if "c" not in state:
            state["c"] = (client_factory or build_client)(cfg)
        return state["c"]

    def today():
        return dt.date.fromtimestamp(ctx.now())

    def accounts():
        if "accts" not in state:
            state["accts"] = resolve_accounts(cfg, client())
        return state["accts"]

    def account(key):
        acc = accounts()
        if not acc:
            raise ToolError("microsoft_ads: no account visible to this sign-in" + (" (check microsoft_ads.accounts)" if allow else ""))
        if not key:
            return acc[0]
        k = str(key).upper()
        for a in acc:
            if str(a["number"]).upper() == k or a["account_id"] == str(key):
                return a
        raise ToolError(f"microsoft_ads: account {key!r} not found (have: {', '.join(str(a['number']) for a in acc)})")

    def tz(a):
        k = "tz:" + a["account_id"]
        if k not in state:
            state[k] = tz_of(client().campaigns(a["account_id"], a["customer_id"]))
        return state[k]

    def tree(a, cid):
        ags = client().ad_groups(cid, a["account_id"], a["customer_id"])
        for g in ags:
            g["keywords"] = client().keywords(g["ad_group_id"], a["account_id"], a["customer_id"])
            g["ads"] = client().ads(g["ad_group_id"], a["account_id"], a["customer_id"])
        return {"ad_groups": ags, "negatives": client().campaign_negatives(cid, a["account_id"], a["customer_id"])}

    note = "Read-only: this tool cannot change or spend anything."

    @srv.tool("adsops_msads_accounts", "Microsoft Ads: who this sign-in is and every ad account it can see (number, id, "
              "customer id, status, currency). " + note, _schema({}))
    def t_accounts(args):
        u = client().user()
        return {"user": u.get("UserName"), "accounts": accounts(), "read_only": True}

    @srv.tool("adsops_msads_campaigns", "Microsoft Ads: campaigns with status, daily budget, bid strategy and max CPC cap, "
              "time zone and final URL suffix. " + note, _schema(ACCOUNT))
    def t_campaigns(args):
        a = account(args.get("account"))
        return {"account": a["number"], "campaigns": client().campaigns(a["account_id"], a["customer_id"])}

    @srv.tool("adsops_msads_structure", "Microsoft Ads: one campaign's full build: ad groups (status, network), every keyword "
              "(match type, status, editorial approval), every ad (status, approval, final URLs, headlines) and campaign negative "
              "keywords. Use it to answer 'is this set up right / can it serve?'. " + note, _schema({**ACCOUNT, **CAMPAIGN}, ["campaign_id"]))
    def t_structure(args):
        a = account(args.get("account"))
        t = tree(a, args["campaign_id"])
        summary = {"ad_groups": len(t["ad_groups"]), "active_ad_groups": sum(g["status"] == "Active" for g in t["ad_groups"]),
                   "keywords": sum(len(g["keywords"]) for g in t["ad_groups"]),
                   "ads": sum(len(g["ads"]) for g in t["ad_groups"]), "negatives": len(t["negatives"])}
        return {"account": a["number"], "campaign_id": args["campaign_id"], "summary": summary, **t}

    @srv.tool("adsops_msads_performance", "Microsoft Ads stats from the Reporting API: impressions, clicks, spend, conversions "
              "per day at level campaign | ad_group | keyword, plus totals with CTR, average CPC and cost per conversion. No rows "
              "= no traffic in the window (zero, not missing). " + note,
              _schema({**ACCOUNT, **WINDOW, "level": {"type": "string", "enum": ["campaign", "ad_group", "keyword"]},
                       "campaign_id": {"type": "string", "description": "optional: only this campaign"}}))
    def t_perf(args):
        a = account(args.get("account"))
        lo, hi = window(args, today())
        level = args.get("level") or "campaign"
        rows = client().report(level, a["account_id"], a["customer_id"], lo, hi,
                               campaign_ids=[args["campaign_id"]] if args.get("campaign_id") else None, time_zone=tz(a))
        return {"account": a["number"], "level": level, "day_from": lo, "day_to": hi, "totals": ms.totals(rows),
                "rows": rows[:500], "rows_total": len(rows)}

    @srv.tool("adsops_msads_search_terms", "Microsoft Ads: what people actually typed (search query report), summed over the "
              "window and sorted by spend, with the keyword that matched. Use it to find wasted spend and new negatives. " + note,
              _schema({**ACCOUNT, **WINDOW, "campaign_id": {"type": "string"},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500}}))
    def t_terms(args):
        a = account(args.get("account"))
        lo, hi = window({"days": 30, **args}, today())
        rows = client().report("search_term", a["account_id"], a["customer_id"], lo, hi,
                               campaign_ids=[args["campaign_id"]] if args.get("campaign_id") else None, time_zone=tz(a))
        agg = {}
        for r in rows:
            k = (r.get("SearchQuery"), r.get("Keyword"), r.get("AdGroupName"))
            s = agg.setdefault(k, {"search_query": k[0], "keyword": k[1], "ad_group": k[2], "impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0.0})
            s["impressions"] += r.get("Impressions", 0); s["clicks"] += r.get("Clicks", 0)
            s["spend"] = round(s["spend"] + r.get("Spend", 0), 2); s["conversions"] += r.get("Conversions", 0)
        terms = sorted(agg.values(), key=lambda x: (-x["spend"], -x["clicks"]))
        return {"account": a["number"], "day_from": lo, "day_to": hi, "terms": terms[:int(args.get("limit") or 100)],
                "terms_total": len(terms), "wasted": [t for t in terms if t["spend"] > 0 and not t["conversions"]][:25]}

    @srv.tool("adsops_msads_health", "Microsoft Ads monitor: everything that stops an Active campaign from serving or wastes its "
              "money, as findings with severity (critical / warning / info): no active ad group, all keywords or ads paused or "
              "disapproved, negatives that block your own keywords, 0 impressions, today's spend vs budget, missing tracking "
              "suffix. Built for a monitoring agent to run daily. " + note, _schema({**ACCOUNT, "days": {"type": "integer", "minimum": 1, "maximum": 30}}))
    def t_health(args):
        a = account(args.get("account"))
        cs = client().campaigns(a["account_id"], a["customer_id"])
        active = [c for c in cs if c["status"] == "Active"]
        search = [c for c in active if (c.get("type") or "Search") == "Search"]
        tr = {c["campaign_id"]: tree(a, c["campaign_id"]) for c in search}
        perf, spend_today = {}, {}
        if active:
            lo, hi = window({"days": int(args.get("days") or 2)}, today())
            rows = client().report("campaign", a["account_id"], a["customer_id"], lo, hi, time_zone=tz_of(cs))
            for c in active:
                mine = [r for r in rows if r.get("CampaignName") == c["name"]]
                perf[c["name"]] = ms.totals(mine)
                spend_today[c["name"]] = sum(r.get("Spend", 0) for r in mine if _is_day(r.get("TimePeriod"), hi))
        f = health_findings(cs, tr, perf, spend_today, account=a)
        return {"account": a["number"], "checked_campaigns": [c["name"] for c in search],
                "not_structure_checked": [f"{c['name']} ({c.get('type')})" for c in active if c not in search], "window_stats": perf,
                "ok": not any(x["severity"] == "critical" for x in f), "findings": f}

    return state


def facts(rows, level):
    """Reporting rows -> metrics-store facts (source microsoft_ads). Same metric names as google_ads (cost, clicks,
    impressions, conversions) so the two platforms sit side by side. Duplicate (day, entity) keys are summed."""
    acc = {}
    for r in rows or []:
        day = norm_day(r.get("TimePeriod"))
        if len(day) != 10:
            continue
        if level == "campaign":
            eid, name = str(r.get("CampaignId") or r.get("CampaignName") or ""), r.get("CampaignName") or ""
        else:
            eid = str(r.get("SearchQuery") or "").strip().lower(); name = eid
        if not eid:
            continue
        s = acc.setdefault((day, eid), {"name": name, "m": {"cost": 0.0, "clicks": 0.0, "impressions": 0.0, "conversions": 0.0}})
        for src, dst in (("Spend", "cost"), ("Clicks", "clicks"), ("Impressions", "impressions"), ("Conversions", "conversions")):
            s["m"][dst] += float(r.get(src) or 0)
    return [{"source": "microsoft_ads", "day": d, "entity_type": level, "entity_id": e, "entity_name": v["name"], "metric": k,
             "value": round(val, 4)} for (d, e), v in acc.items() for k, val in v["m"].items()]


def _is_day(period, iso):
    """Report TimePeriod for daily rows arrives as YYYY-MM-DD or M/D/YYYY depending on locale; compare as a date."""
    return norm_day(period) == iso


def norm_day(period):
    s = str(period or "").strip()
    if len(s) == 10 and s[4] == "-":
        return s
    try:
        m, d, y = (int(x) for x in s.split("/"))
        return dt.date(y, m, d).isoformat()
    except (ValueError, TypeError):
        return s


def selftest():
    from .protocol import Server
    n = 0
    camps = [{"campaign_id": "1", "name": "Owners", "status": "Active", "daily_budget": 20, "final_url_suffix": None}]
    tree_ok = {"1": {"ad_groups": [{"name": "g", "status": "Active", "network": "OwnedAndOperatedAndSyndicatedSearch",
                                    "keywords": [{"text": "plumber denver", "status": "Active", "editorial_status": "Active"}],
                                    "ads": [{"ad_id": "9", "status": "Active", "editorial_status": "Active"}]}],
                     "negatives": [{"text": "jobs", "match_type": "Phrase"}]}}
    f = health_findings(camps, tree_ok, {"Owners": {"Impressions": 10}}, {"Owners": 3.0})
    assert [x["severity"] for x in f] == ["info"] and "suffix" in f[0]["issue"]; n += 1
    paused = {"1": {"ad_groups": [{"name": "g", "status": "Paused", "keywords": [], "ads": []}], "negatives": []}}
    assert health_findings(camps, paused, {}, {})[0]["severity"] == "critical"; n += 1          # built in drafts, only the campaign enabled
    clash = {"1": {**tree_ok["1"], "negatives": [{"text": "denver", "match_type": "Phrase"}]}}
    assert any("block your own" in x["issue"] for x in health_findings(camps, clash, {}, {})); n += 1
    dis = {"1": {"ad_groups": [{**tree_ok["1"]["ad_groups"][0], "ads": [{"ad_id": "9", "status": "Active", "editorial_status": "Disapproved"}]}], "negatives": []}}
    sev = {x["issue"]: x["severity"] for x in health_findings(camps, dis, {}, {})}
    assert sev.get("no serving ad (all paused or disapproved)") == "critical"; n += 1
    assert any("0 impressions" in x["issue"] for x in health_findings(camps, tree_ok, {"Owners": {"Impressions": 0}}, {})); n += 1
    assert any("reached" in x["issue"] for x in health_findings(camps, tree_ok, {}, {"Owners": 21.0})); n += 1
    assert health_findings([{**camps[0], "status": "Paused"}], paused, {}, {}) == []; n += 1   # paused campaigns are not alarms
    assert health_findings([{**camps[0], "status": "Suspended"}], {}, {}, {})[0]["severity"] == "critical"; n += 1
    assert health_findings([{**camps[0], "type": "PerformanceMax"}], {}, {}, {}) == [] or all(
        x["severity"] != "critical" for x in health_findings([{**camps[0], "type": "PerformanceMax"}], {}, {}, {})); n += 1
    assert health_findings(camps, tree_ok, {}, {}, account={"status": "Paused", "pause_reason": 2})[0]["severity"] == "critical"; n += 1
    assert tz_of([{"time_zone": "MountainTimeUSCanada"}, {"time_zone": "MountainTimeUSCanada"}]) == "MountainTimeUSCanada" and tz_of([]) is None; n += 1
    assert norm_day("11/6/2026") == "2026-11-06" and norm_day("2026-11-06") == "2026-11-06"; n += 1
    today = dt.date(2026, 11, 16)
    assert window({"days": 7}, today) == ("2026-11-10", "2026-11-16"); n += 1
    try:
        window({"day_from": "2026-11-17", "day_to": "2026-11-01"}, today); raise AssertionError
    except ToolError:
        n += 1

    class FakeClient:
        def user(self):
            return {"UserName": "u@x"}

        def accounts(self):
            return [{"account_id": "187", "number": "G1", "name": "Acme", "customer_id": "255", "status": "Active"},
                    {"account_id": "188", "number": "G2", "name": "Other", "customer_id": "255", "status": "Active"}]

        def campaigns(self, a, c):
            return camps

        def ad_groups(self, cid, a, c):
            return [{"ad_group_id": "5", "name": "g", "status": "Active", "network": "OwnedAndOperatedAndSyndicatedSearch"}]

        def keywords(self, g, a, c):
            return [{"text": "plumber denver", "match_type": "Exact", "status": "Active", "editorial_status": "Active"}]

        def ads(self, g, a, c):
            return [{"ad_id": "9", "status": "Active", "editorial_status": "Active", "final_urls": ["https://x/"]}]

        def campaign_negatives(self, cid, a, c):
            return []

        def report(self, level, a, c, lo, hi, campaign_ids=None, time_zone=None):
            if level == "search_term":
                return [{"SearchQuery": "pm jobs", "Keyword": "pm", "AdGroupName": "g", "Impressions": 5, "Clicks": 2, "Spend": 9.5, "Conversions": 0},
                        {"SearchQuery": "pm jobs", "Keyword": "pm", "AdGroupName": "g", "Impressions": 1, "Clicks": 1, "Spend": 2.0, "Conversions": 0},
                        {"SearchQuery": "tampa pm", "Keyword": "pm", "AdGroupName": "g", "Impressions": 9, "Clicks": 3, "Spend": 6.0, "Conversions": 1}]
            return [{"TimePeriod": hi, "CampaignName": "Owners", "Impressions": 100, "Clicks": 4, "Spend": 12.0, "Conversions": 1}]

    class Ctx:
        def now(self):
            return dt.datetime(2026, 11, 16, 12).timestamp()
    srv = Server("t", "0")
    assert register(srv, Ctx(), {"accounts": []}) is None; n += 1                               # no block -> no tools
    cfg = {"microsoft_ads": {"accounts": ["g2"]}}
    register(srv, Ctx(), cfg, client_factory=lambda c: FakeClient())
    names = [t for t in srv._order if t.startswith("adsops_msads_")]
    assert names == ["adsops_msads_accounts", "adsops_msads_campaigns", "adsops_msads_structure", "adsops_msads_performance",
                     "adsops_msads_search_terms", "adsops_msads_health"]; n += 1
    assert not any(w in t for t in names for w in ("set", "apply", "pause", "enable", "update", "create", "delete")); n += 1
    call = lambda name, args: srv._tools[name]["fn"](args)
    assert [a["number"] for a in call("adsops_msads_accounts", {})["accounts"]] == ["G2"]; n += 1   # allow-list honoured
    try:
        call("adsops_msads_campaigns", {"account": "G1"}); raise AssertionError
    except ToolError:
        n += 1
    st = call("adsops_msads_structure", {"campaign_id": "1"})
    assert st["summary"] == {"ad_groups": 1, "active_ad_groups": 1, "keywords": 1, "ads": 1, "negatives": 0}; n += 1
    p = call("adsops_msads_performance", {"days": 7})
    assert p["totals"]["Spend"] == 12.0 and p["totals"]["CostPerConversion"] == 12.0 and p["day_from"] == "2026-11-10"; n += 1
    t = call("adsops_msads_search_terms", {})
    assert t["terms"][0] == {"search_query": "pm jobs", "keyword": "pm", "ad_group": "g", "impressions": 6, "clicks": 3, "spend": 11.5, "conversions": 0}
    assert [w["search_query"] for w in t["wasted"]] == ["pm jobs"]; n += 1
    # config validation (config._load_microsoft_ads)
    from . import config as config_mod
    good = {"microsoft_ads": {"developer_token": {"kind": "env", "name": "X"},
                              "auth": {"provider": "google", "client_id": "id", "client_secret": {"kind": "env", "name": "Y"},
                                       "redirect_uri": "http://localhost:8765/cb"}}}
    config_mod._load_microsoft_ads(good)
    assert good["microsoft_ads"]["auth"]["refresh_token_file"].endswith("msads-refresh-token"); n += 1
    for bad in ({"developer_token": {"kind": "env", "name": "X"}, "auth": {"provider": "msn", "client_id": "i"}},
                {"developer_token": {"kind": "plain"}, "auth": {"provider": "microsoft", "client_id": "i"}},
                {"developer_token": {"kind": "env", "name": "X"}, "auth": {"provider": "google", "client_id": "i"}},
                {"developer_token": {"kind": "env", "name": "X"}, "auth": {"provider": "microsoft", "client_id": "i"}, "accounts": [True]},
                {"developer_token": {"kind": "env", "name": "X"}, "auth": {"provider": "microsoft", "client_id": "i"},
                 "accounts": ["G1", {"account_id": 1, "customer_id": 2}]}):
        try:
            config_mod._load_microsoft_ads({"microsoft_ads": bad}); raise AssertionError(bad)
        except ToolError:
            n += 1
    fx = facts([{"TimePeriod": "11/15/2026", "CampaignId": "1", "CampaignName": "Owners", "Spend": 5.0, "Clicks": 2, "Impressions": 40, "Conversions": 0},
                {"TimePeriod": "2026-11-15", "CampaignId": "1", "CampaignName": "Owners", "Spend": 1.0, "Clicks": 1, "Impressions": 4, "Conversions": 1}],
               "campaign")
    v = {(f["metric"]): f["value"] for f in fx}
    assert len(fx) == 4 and v == {"cost": 6.0, "clicks": 3.0, "impressions": 44.0, "conversions": 1.0} and fx[0]["day"] == "2026-11-15"; n += 1
    srv2 = Server("t2", "0")
    class NoDiscovery(FakeClient):
        def accounts(self):
            raise AssertionError("pinned accounts must not call Accounts/Search")
    register(srv2, Ctx(), {"microsoft_ads": {"accounts": [{"number": "G7", "account_id": 187, "customer_id": 255}]}},
             client_factory=lambda c: NoDiscovery())
    assert srv2._tools["adsops_msads_campaigns"]["fn"]({})["account"] == "G7"; n += 1
    h = call("adsops_msads_health", {})
    assert h["ok"] and h["checked_campaigns"] == ["Owners"] and h["window_stats"]["Owners"]["Impressions"] == 100; n += 1
    return n
