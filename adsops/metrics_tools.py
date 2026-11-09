"""The measurement loop (AdRails 0.10, 2026-11-09): keep a daily history of every number, and say whether each change worked.

Four tools over three DeepSeek-built, reviewed modules in adsops/metrics/:
  adsops_metrics_pull     fill the local store: Google Ads (campaign + search-term days), Search Console (site, query, page days),
                          AI-visibility panel runs and the lead book, plus a timeline of every committed change from the audit log.
                          Idempotent: re-running re-pulls only the missing days and the last few days platforms still revise.
  adsops_metrics          read the store: coverage, a daily series, top entities, the change timeline.
  adsops_search_console   organic search, live: sites, cheapest wins (pages ranking 4-20), weak titles (CTR gaps), queries you pay
                          for but already rank top-3 for, and period-over-period winners/losers.
  adsops_impact           "did that change work?": the counterfactual comparison (adsops/metrics/impact.py) for one change.

Where things live: the store is `metrics.sqlite` beside the config's audit log (cfg "metrics_db" overrides). Nothing leaves the
machine except the read-only API calls to Google. No customer names, phones or emails enter the store: leads are counted per
source and day only. Search Console sign-in: the same Google credential as Google Ads with the read-only Search Console scope
(cfg credentials.search_console overrides); a service account must be added as a user on the Search Console property.
Unattended: `python3 -m adsops.metrics_tools --config <config.json> pull` (cron) does exactly what adsops_metrics_pull does.
"""
import datetime as dt
import hashlib
import json
import os
import sys

from .errors import ToolError, scrub
from .metrics import gsc, impact, store as mstore

SCOPE_GSC = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_DIMS = (["date"], ["date", "query"], ["date", "page"])
# Ads metrics that are better when lower (impact verdict direction). Everything else: higher is better.
LOWER_IS_BETTER = {"cost"}
# One source stores several overlapping levels (a campaign's cost is also the sum of its search terms' cost), so a read
# that names no entity_type must pick ONE level, or totals double. Found live 2026-11-09: Acme cost read $95 for a $50 day.
DEFAULT_ETYPE = {"google_ads": "campaign", "search_console": "site", "aeo": "engine", "leads": "source"}
# adsops_rollback only rewrites the state file (an apply follows), so it is not a change to the account (review #3).
CHANGE_TOOLS = {"adsops_set", "adsops_apply", "adsops_openai_apply", "adsops_guard"}


def db_path(cfg):
    from .enterprise_tools import state_path
    return state_path(cfg, "metrics_db", "metrics.sqlite")


def _today(ctx):
    return dt.date.fromtimestamp(ctx.now()).isoformat()


def _gsc_tokens(cfg):
    """Search Console token provider: credentials.search_console if configured, else the Google Ads credential re-scoped."""
    from . import auth
    creds = cfg.get("credentials") or {}
    c = creds.get("search_console") or creds.get("google_ads")
    if not c:
        raise ToolError("no Google credential configured (credentials.google_ads or credentials.search_console)")
    tp = auth.from_config(c)
    if hasattr(tp, "scope"):                 # service account: mint a token for the Search Console scope
        tp.scope = SCOPE_GSC
    return tp                                # oauth_user: the scope was granted at `adsops-mcp connect` (0.10 asks for it)


def _gsc_http(method, url, headers, body):
    """(status, body bytes) without raising on HTTP errors: the gsc client decides what each status means."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        raise ToolError("Search Console unreachable: %s" % type(e).__name__)


def gsc_sites(cfg):
    """Sites to track: cfg search_console.sites, else the campaigns' url_domains matched against what the credential can see."""
    sc = cfg.get("search_console") or {}
    if sc.get("sites"):
        return list(sc["sites"])
    doms = set()
    for a in cfg.get("accounts") or []:
        for c in a.get("campaigns") or []:
            doms.update(d.lower() for d in c.get("url_domains") or [])
    for d in (cfg.get("aeo") or {}).get("domains") or []:
        doms.add(d.lower())
    return sorted(doms)


def _match_sites(wanted, visible):
    """Map bare domains (acmepm.example) to the property ids the credential can see (https://acmepm.example/ or sc-domain:...)."""
    out = []
    for w in wanted:
        if w.startswith(("https://", "http://", "sc-domain:")):
            out.append(w)
            continue
        hits = [v for v in visible if v in ("sc-domain:" + w, "https://" + w + "/", "https://www." + w + "/", "http://" + w + "/")]
        out.extend(hits[:1])
    return out


# ── ingest helpers (pure-ish; each returns facts for Store.upsert) ───────────────────────────────────────────────
def leads_facts(leads, day_from, day_to):
    """Counts per source per day from the lead book: leads (spam excluded), won, won_value. Never names, phones or emails."""
    agg = {}
    for ld in leads or []:
        day = str(ld.get("created_at") or "")[:10]
        if not (day_from <= day <= day_to) or ld.get("stage") == "spam":
            continue
        src = str(ld.get("source") or "other").lower()[:64]
        a = agg.setdefault((day, src), {"leads": 0, "won": 0, "won_value": 0.0})
        a["leads"] += 1
        if ld.get("stage") == "won":
            a["won"] += 1
            try:
                a["won_value"] += float(ld.get("value") or 0)
            except (TypeError, ValueError):
                pass
    # review #2: a day with no leads is 0 leads, not unknown: fill every day from each source's first lead in the window
    first = {}
    for (d, s_) in agg:
        first[s_] = min(first.get(s_, d), d)
    for s_, d0 in first.items():
        for d in mstore.days_between(d0, day_to):
            agg.setdefault((d, s_), {"leads": 0, "won": 0, "won_value": 0.0})
    return [{"source": "leads", "day": d, "entity_type": "source", "entity_id": s, "entity_name": s, "metric": m, "value": v}
            for (d, s), a in agg.items() for m, v in a.items()]


def aeo_facts(runs, day_from, day_to, brand=None):
    """AI-visibility panel runs -> per engine per day: answers, mentioned, cited (branded prompts excluded: they always pass).
    The last run of a day wins, so a re-run the same day replaces rather than double-counts."""
    b = (brand or "").strip().lower()
    last = {}
    for r in runs or []:
        day = str(r.get("ts") or "")[:10]
        if day_from <= day <= day_to:
            last[day] = r
    out = []
    for day, r in last.items():
        agg = {}
        for x in r.get("results") or []:
            if x.get("branded") or (b and b in str(x.get("prompt") or "").lower()):
                continue
            e = str(x.get("engine") or "unknown").lower()[:64]
            a = agg.setdefault(e, {"answers": 0, "mentioned": 0, "cited": 0})
            a["answers"] += 1
            a["mentioned"] += 1 if x.get("mentioned") else 0
            a["cited"] += 1 if x.get("cited") else 0
        for e, a in agg.items():
            out += [{"source": "aeo", "day": day, "entity_type": "engine", "entity_id": e, "entity_name": e, "metric": m, "value": v}
                    for m, v in a.items()]
    return out


def change_events(lines, resolve=None):
    """Audit-log lines -> change events (only changes that reached the account). ref = hash of the line, so re-ingest is a no-op."""
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        tool = e.get("tool")
        if tool not in CHANGE_TOOLS:
            continue
        if tool == "adsops_guard":
            if not e.get("paused"):          # review #3: guard entries carry `paused`, not `ops`; a pause is THE change to measure
                continue
        elif tool in ("adsops_apply", "adsops_openai_apply") and not (e.get("commit") and any((e.get("ops") or {}).values())):
            continue                         # validate-only runs and no-op commits changed nothing
        if tool == "adsops_set" and (e.get("dry_run") or not e.get("applied")):
            continue                         # review #3: applied=false only edited the state file; the later apply is the change
        summ = e.get("summary") or e.get("notes") or e.get("why") or tool
        summary = "; ".join(map(str, summ)) if isinstance(summ, list) else str(summ)
        camp = str(e.get("campaign") or "")
        cid = (resolve(camp) if resolve else None) or ""
        out.append({"day": _local_day(e.get("at")), "kind": tool.replace("adsops_", ""), "summary": (camp + ": " if camp else "") + summary,
                    "source": "google_ads", "entity_type": "campaign" if cid else "", "entity_id": cid,
                    "ref": hashlib.sha256(line.strip().encode()).hexdigest()[:32]})
    return [x for x in out if len(x["day"]) == 10]


def _local_day(at):
    """Audit stamps are UTC; Ads and Search Console days are local. A 9 pm change in Florida is 01:00Z the NEXT day, so convert
    to this machine's local zone (the owner's) before calling it the change day (review #7)."""
    try:
        return dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return str(at or "")[:10]


def zero_fill(st, source, entity_type, metrics, day_from, day_to):
    """Review #2: a pulled day with no rows is a ZERO day (no clicks, no leads), not unknown. Platforms return no row for a
    zero day, so after each pull write explicit zeros for every entity already known to the store, from its first day on,
    inside the pulled window only (outside the window stays null = never pulled)."""
    rows = st._conn.execute("SELECT entity_id, MIN(day) AS first, MAX(entity_name) AS name FROM facts WHERE source=? AND entity_type=? "
                         "GROUP BY entity_id", (source, entity_type)).fetchall()
    have = {(r[0], r[1], r[2]) for r in st._conn.execute(
        "SELECT entity_id, day, metric FROM facts WHERE source=? AND entity_type=? AND day BETWEEN ? AND ?",
        (source, entity_type, day_from, day_to))}
    out = []
    for r in rows:
        for d in mstore.days_between(max(day_from, r[1]), day_to) if max(day_from, r[1]) <= day_to else []:
            out += [{"source": source, "day": d, "entity_type": entity_type, "entity_id": r[0], "entity_name": r[2] or "",
                     "metric": m, "value": 0} for m in metrics if (r[0], d, m) not in have]
    return st.upsert(out) if out else 0


def register(srv, ctx, cfg, gsc_client=None):
    from . import config as config_mod, engine
    path = db_path(cfg)

    def open_store():
        return mstore.Store(path, now=ctx.now)

    def gclient():
        return gsc_client or gsc.Client(_gsc_tokens(cfg), _gsc_http)

    def resolve(camp_key):
        try:
            _, camp = config_mod.resolve_campaign(cfg, camp_key)
            return str(engine.load_state(camp["state_file"]).get("campaign_id") or "")
        except Exception:                    # an old campaign key no longer in config: keep the event, unlinked
            return ""

    # ── pull ──
    def pull(args):
        today = _today(ctx)
        only = set(args.get("sources") or ["google_ads", "search_console", "aeo", "leads", "changes"])
        backfill = int(args.get("backfill_days") or 90)
        if not 1 <= backfill <= 480:
            raise ToolError("backfill_days must be 1..480 (Search Console keeps 16 months)")
        st, report = open_store(), {}
        try:
            if "google_ads" in only:
                for a in cfg.get("accounts") or []:
                    if a.get("platform") != "google_ads":
                        continue
                    scope = "account:" + a["key"]
                    last = (st.last_pull("google_ads", scope) or {}).get("day_to")
                    win = mstore.pull_window(last, today, backfill_days=backfill, settle_days=7)
                    if not win:
                        report[scope] = "up to date"
                        continue
                    try:
                        cl = ctx.client_factory(a)
                        q = ("SELECT segments.date, campaign.id, campaign.name, metrics.cost_micros, metrics.clicks, metrics.impressions, "
                             "metrics.conversions, metrics.conversions_value FROM campaign WHERE segments.date BETWEEN '%s' AND '%s'" % win)
                        n = st.upsert(mstore.ads_rows(cl.search(q), level="campaign"))
                        # search terms over the whole window (review note: a 30-day cap left permanent gaps after an outage)
                        tf = win[0]
                        q2 = ("SELECT segments.date, search_term_view.search_term, metrics.cost_micros, metrics.clicks, metrics.impressions, "
                              "metrics.conversions, metrics.conversions_value FROM search_term_view WHERE segments.date BETWEEN '%s' AND '%s' "
                              "AND metrics.impressions > 0" % (tf, win[1]))
                        n += st.upsert(mstore.ads_rows(cl.search(q2), level="search_term"))
                        zero_fill(st, "google_ads", "campaign", ["cost", "clicks", "impressions", "conversions", "conversions_value"], *win)
                        st.record_pull("google_ads", scope, win[0], win[1], n)
                        report[scope] = {"days": list(win), "rows": n}
                    except ToolError as e:
                        st.record_pull("google_ads", scope, win[0], win[1], 0, ok=False, error=str(e))
                        report[scope] = {"error": scrub(str(e), 300)}
            if "search_console" in only:
                wanted = gsc_sites(cfg)
                if wanted:
                    try:
                        c = gclient()
                        sites = _match_sites(wanted, [s["site"] for s in c.sites()])
                        for site in sites:
                            scope = "site:" + site
                            last = (st.last_pull("search_console", scope) or {}).get("day_to")
                            win = mstore.pull_window(last, today, backfill_days=backfill, settle_days=4)
                            if not win:
                                report[scope] = "up to date"
                                continue
                            n, cut = 0, []
                            for dims in GSC_DIMS:
                                c.truncated = False
                                n += st.upsert(mstore.gsc_rows(c.query(site, win[0], win[1], list(dims)), site, list(dims)))
                                if getattr(c, "truncated", False):
                                    cut.append("+".join(dims))
                            zero_fill(st, "search_console", "site", ["clicks", "impressions", "position_x_impr"], *win)
                            if cut:                  # review #6: never advance past a partial pull; next run retries the window
                                err = "row cap hit for %s: pull again with a smaller backfill_days" % ", ".join(cut)
                                st.record_pull("search_console", scope, win[0], win[1], n, ok=False, error=err)
                                report[scope] = {"days": list(win), "rows": n, "error": err}
                            else:
                                st.record_pull("search_console", scope, win[0], win[1], n)
                                report[scope] = {"days": list(win), "rows": n}
                        missing = [w for w in wanted if not _match_sites([w], sites)]
                        if missing:
                            report["search_console_not_visible"] = missing
                    except ToolError as e:
                        report["search_console"] = {"error": scrub(str(e), 300)}
            lo = (dt.date.fromisoformat(today) - dt.timedelta(days=backfill)).isoformat()
            if "aeo" in only:
                vp = (cfg.get("aeo") or {}).get("visibility_log")
                from .enterprise_tools import state_path
                vp = os.path.expanduser(vp) if vp else state_path(cfg, "_none", "aeo-visibility.jsonl")
                if os.path.isfile(vp):
                    from .aeo import visibility
                    report["aeo"] = {"rows": st.upsert(aeo_facts(visibility.load_history(vp), lo, today, brand=_brand(cfg)))}
            if "leads" in only:
                try:
                    from .local import leadbook
                    from .local_tools import _paths
                    book = _paths(cfg)["book"]
                    if os.path.isfile(book):
                        data = leadbook.LeadBook(book, now=ctx.now)._ensure()
                        report["leads"] = {"rows": st.upsert(leads_facts(data.get("leads"), lo, today))}
                except (ToolError, OSError, KeyError) as e:
                    report["leads"] = {"error": scrub(str(e), 200)}
            if "changes" in only:
                ap = os.path.expanduser(cfg.get("audit_log", "~/.adsops/audit.jsonl"))
                if os.path.isfile(ap):
                    with open(ap, encoding="utf-8") as fh:
                        evs = change_events(fh, resolve)
                    report["changes"] = {"new": sum(1 for e in evs if st.add_event(**e)), "seen": len(evs)}
            report["coverage"] = st.coverage()
        finally:
            st.close()
        return {"store": path, "pulled": report,
                "next": "adsops_impact action=changes lists what changed; adsops_search_console action=opportunities for organic wins"}

    @srv.tool("adsops_metrics_pull", "Fill the LOCAL daily metrics history (one SQLite file on this machine): Google Ads campaign and "
              "search-term days, Google Search Console site/query/page days, AI-visibility panel runs, lead counts by source, and a "
              "timeline of every committed ad change from the audit log. Safe to run every day (cron or the `morning` prompt): it "
              "fetches only missing days plus the last few that Google still revises. First run backfills backfill_days (default 90). "
              "Read-only towards Google; nothing leaves this machine.",
              {"type": "object", "properties": {
                  "sources": {"type": "array", "items": {"type": "string", "enum": ["google_ads", "search_console", "aeo", "leads", "changes"]}},
                  "backfill_days": {"type": "integer"}}, "required": [], "additionalProperties": False})
    def metrics_pull(args):
        return pull(args)

    # ── read the store ──
    @srv.tool("adsops_metrics", "Read the local daily metrics history (fill it with adsops_metrics_pull). action=coverage: what is "
              "stored, per source, first/last day. action=series: one metric per day (e.g. source=google_ads metric=conversions, "
              "entity_type=campaign, entity_ids=[...]; missing days are null, never zero); ratio=[num,den] gives e.g. cost per "
              "conversion per day. action=top: entities ranked by a metric over a window (top search terms by cost, top queries by "
              "clicks). action=events: the change timeline. Sources: google_ads, search_console, aeo, leads.",
              {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["coverage", "series", "top", "events"]},
                  "source": {"type": "string"}, "metric": {"type": "string"}, "entity_type": {"type": "string"},
                  "entity_ids": {"type": "array", "items": {"type": "string"}},
                  "ratio": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
                  "day_from": {"type": "string"}, "day_to": {"type": "string"}, "days": {"type": "integer"},
                  "agg": {"type": "string", "enum": ["sum", "avg", "max", "min"]}, "limit": {"type": "integer"},
                  "order": {"type": "string", "enum": ["asc", "desc"]}},
               "required": ["action"], "additionalProperties": False})
    def metrics_read(args):
        a = args["action"]
        today = _today(ctx)
        d_to = args.get("day_to") or (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
        d_from = args.get("day_from") or (dt.date.fromisoformat(d_to) - dt.timedelta(days=int(args.get("days") or 28) - 1)).isoformat()
        st = open_store()
        try:
            if a == "coverage":
                return {"store": path, "coverage": st.coverage()}
            if a == "events":
                return {"events": st.events(day_from=args.get("day_from"), day_to=args.get("day_to"), limit=int(args.get("limit") or 100))}
            if a == "series":
                src = args.get("source")
                if not src:
                    raise ToolError("series needs source (google_ads, search_console, aeo or leads)")
                f = {"source": src, "entity_type": args.get("entity_type") or DEFAULT_ETYPE.get(src), "entity_ids": args.get("entity_ids"),
                     "day_from": d_from, "day_to": d_to}
                if args.get("metric") == "position" and not args.get("ratio"):
                    # review #5: positions are never summed; daily average = position_x_impr / impressions
                    rs = st.ratio_series("position_x_impr", "impressions", **f)
                    return {"series": [{"day": r["day"], "value": None if r["value"] is None else round(r["value"], 2)} for r in rs],
                            "metric": "position", "note": "impression-weighted average position (1 = top)"}
                if args.get("ratio"):
                    num, den = args["ratio"]
                    return {"series": st.ratio_series(num, den, **f), "ratio": [num, den]}
                if not args.get("metric"):
                    raise ToolError("series needs metric (or ratio)")
                return {"series": st.series(args["metric"], agg=args.get("agg") or "sum", **f), "metric": args["metric"]}
            if a == "top":
                for k in ("metric", "source", "entity_type"):
                    if not args.get(k):
                        raise ToolError("top needs %s" % k)
                if args["metric"] == "position":
                    raise ToolError("positions cannot be summed: rank by clicks or impressions, or read action=series metric=position")
                return {"top": st.by_entity(args["metric"], args["source"], args["entity_type"], d_from, d_to,
                                            agg=args.get("agg") or "sum", limit=int(args.get("limit") or 25),
                                            order=args.get("order") or "desc"), "day_from": d_from, "day_to": d_to}
            raise ToolError("action must be coverage, series, top or events")
        finally:
            st.close()

    # ── Search Console, live ──
    @srv.tool("adsops_search_console", "Google Search Console (organic search), read-only and live. action=sites: properties this login "
              "can read. action=opportunities: one ranked list of the cheapest organic wins in plain English (pages already ranking "
              "4-20, titles that under-earn clicks for their position, and search terms you PAY for while already ranking top-3 "
              "organically, using the Google Ads terms in the local store). action=striking | ctr_gaps | overlap: each list on its own. "
              "action=trends: queries (or pages, by=page) that gained or lost clicks vs the previous period of the same length. "
              "Default window: the last 28 days (Search Console data lags ~2-3 days).",
              {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["sites", "opportunities", "striking", "ctr_gaps", "overlap", "trends"]},
                  "site": {"type": "string", "description": "property, e.g. https://example.com/ or sc-domain:example.com (default: the first tracked site)"},
                  "days": {"type": "integer"}, "by": {"type": "string", "enum": ["query", "page"]}, "limit": {"type": "integer"}},
               "required": ["action"], "additionalProperties": False})
    def search_console(args):
        c = gclient()
        a = args["action"]
        if a == "sites":
            return {"sites": c.sites(), "tracked": gsc_sites(cfg)}
        site = args.get("site")
        if not site:
            vis = [s["site"] for s in c.sites()]
            m = _match_sites(gsc_sites(cfg), vis)
            if not m:
                raise ToolError("no Search Console property matches this config: pass site= (action=sites lists them)")
            site = m[0]
        days = int(args.get("days") or 28)
        if not 7 <= days <= 480:
            raise ToolError("days must be 7..480")
        lim = int(args.get("limit") or 25)
        end = dt.date.fromisoformat(_today(ctx)) - dt.timedelta(days=3)
        start = end - dt.timedelta(days=days - 1)
        by = args.get("by") or "query"

        def rows(s, e, key):
            return gsc.aggregate(c.query(site, s.isoformat(), e.isoformat(), [key]), [key], key)

        if a == "trends":
            p_end = start - dt.timedelta(days=1)
            p_start = p_end - dt.timedelta(days=days - 1)
            return {"site": site, "by": by, "period": [start.isoformat(), end.isoformat()],
                    "previous": [p_start.isoformat(), p_end.isoformat()],
                    **gsc.trends(rows(p_start, p_end, by), rows(start, end, by), key=by, limit=lim)}
        qrows = rows(start, end, by if a in ("striking", "ctr_gaps") else "query")
        terms = None
        if a in ("overlap", "opportunities"):
            st = open_store()
            try:
                top = st.by_entity("cost", "google_ads", "search_term", start.isoformat(), end.isoformat(), limit=500)
                clicks = {r["entity_id"]: r["value"] for r in st.by_entity("clicks", "google_ads", "search_term", start.isoformat(), end.isoformat(), limit=2000)}
                conv = {r["entity_id"]: r["value"] for r in st.by_entity("conversions", "google_ads", "search_term", start.isoformat(), end.isoformat(), limit=2000)}
            finally:
                st.close()
            terms = [{"term": r["entity_id"], "cost": r["value"], "clicks": clicks.get(r["entity_id"], 0),
                      "conversions": conv.get(r["entity_id"], 0)} for r in top]
        base = {"site": site, "period": [start.isoformat(), end.isoformat()]}
        if a == "striking":
            return {**base, "striking": gsc.striking_distance(qrows, limit=lim)}
        if a == "ctr_gaps":
            return {**base, "ctr_gaps": gsc.ctr_gaps(qrows, limit=lim)}
        if a == "overlap":
            out = gsc.paid_organic_overlap(qrows, terms or [])
            return {**base, "overlap": out[:lim], "note": None if terms else "no Google Ads search terms in the local store: run adsops_metrics_pull"}
        return {**base, "opportunities": gsc.opportunities(qrows, ads_terms=terms, limit=min(lim, 25)),
                "ads_terms_used": bool(terms)}

    # ── impact ──
    @srv.tool("adsops_impact", "Did that change work? action=changes lists committed changes (budget moves, negatives, bid changes, guard pauses) from "
              "the local history. action=measure compares what happened after one change with a counterfactual of what would likely "
              "have happened without it (other untouched campaigns as the control when they exist, else the weekly pattern before the "
              "change), with a 90% range and a placebo test of how often a jump that size happens by chance. metric: conversions, "
              "clicks, cost, impressions, conversions_value, cpa (cost per conversion), or with source=search_console clicks/"
              "impressions, source=leads leads/won. Needs >= 14 days before and >= 7 days after the change; earlier it says how long "
              "to wait. Run adsops_metrics_pull first.",
              {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["changes", "measure"]},
                  "ref": {"type": "string", "description": "a change ref from action=changes (or pass day)"},
                  "day": {"type": "string", "description": "YYYY-MM-DD the change took effect"},
                  "metric": {"type": "string"}, "source": {"type": "string", "enum": ["google_ads", "search_console", "leads", "aeo"]},
                  "campaign_id": {"type": "string", "description": "measure one campaign (default: the change's campaign, else the account)"},
                  "pre_days": {"type": "integer"}, "post_days": {"type": "integer"}},
               "required": ["action"], "additionalProperties": False})
    def impact_tool(args):
        st = open_store()
        try:
            if args["action"] == "changes":
                return {"changes": st.events(limit=100)}
            ev = None
            if args.get("ref"):
                ev = next((e for e in st.events(limit=5000) if e.get("ref") == args["ref"]), None)
                if not ev:
                    raise ToolError("no change with that ref: action=changes lists them")
            day = args.get("day") or (ev or {}).get("day")
            if not day:
                raise ToolError("measure needs ref or day")
            source = args.get("source") or "google_ads"
            metric = args.get("metric") or ("conversions" if source == "google_ads" else "clicks" if source == "search_console" else "leads")
            pre, post = int(args.get("pre_days") or 28), args.get("post_days")
            d0 = dt.date.fromisoformat(day)
            lo = (d0 - dt.timedelta(days=pre + 1)).isoformat()
            hi = (d0 + dt.timedelta(days=int(post) if post else 365)).isoformat()
            # review #7: recent days are immature (Search Console's last days are partial; conversions arrive for weeks), and
            # immature post days fake a "hurt". Cap the post window before them.
            lag = 4 if source == "search_console" or metric in ("conversions", "conversions_value", "cpa", "won", "won_value") else 1
            hi = min(hi, (dt.date.fromisoformat(_today(ctx)) - dt.timedelta(days=lag)).isoformat())
            cid = args.get("campaign_id") or (ev or {}).get("entity_id") or None
            etype = DEFAULT_ETYPE.get(source)          # account-wide = the sum of campaigns, never campaigns + search terms
            kw = {"pre_days": pre, "post_days": int(post) if post else None}

            def ser(m, ids=None, et=None):
                # the change day itself is half before, half after: blank it so it counts on neither side (review #7)
                return [{"day": p["day"], "value": None if p["day"] == day else p["value"]}
                        for p in st.series(m, source=source, entity_type=et, entity_ids=ids, day_from=lo, day_to=hi)]

            def controls_for(m):
                """Other campaigns, not changed in the window, best-correlated with the treated one before the change."""
                if source != "google_ads" or not cid:
                    return []
                changed = {e["entity_id"] for e in st.events(day_from=lo, day_to=hi, limit=5000) if e.get("entity_id")}
                cands = {r["entity_id"]: ser(m, [r["entity_id"]], "campaign")
                         for r in st.by_entity(m, "google_ads", "campaign", lo, hi, limit=40) if r["entity_id"] != cid}
                pre_t = [p for p in ser(m, [cid], "campaign") if p["day"] < day]
                ids = impact.pick_controls(cands, cid, exclude_ids=changed, pre_series=pre_t)
                return [cands[i] for i in ids]

            ids = [cid] if (cid and source == "google_ads") else None
            if metric == "cpa":
                if source != "google_ads":
                    raise ToolError("cpa is a Google Ads metric")
                r = impact.measure_ratio(ser("cost", ids, etype), ser("conversions", ids, etype), day,
                                         num_controls=controls_for("cost") or None, den_controls=controls_for("conversions") or None, **kw)
            else:
                r = impact.measure(ser(metric, ids, etype), day, controls=controls_for(metric) or None, metric=metric,
                                   higher_is_better=metric not in LOWER_IS_BETTER, **kw)
            return {"change": ev, "scope": {"source": source, "campaign_id": cid or "all"}, **r}
        finally:
            st.close()


def _brand(cfg):
    try:
        from .local_tools import _load, _paths
        return (_load(_paths(cfg)["profile"], {}) or {}).get("business_name")
    except Exception:
        return None


def main(argv=None):
    """Unattended pull for cron: python3 -m adsops.metrics_tools --config <path> pull [--backfill N]. Prints one JSON line."""
    import argparse
    import time
    from . import config as config_mod
    from .tools import Context
    ap = argparse.ArgumentParser(prog="adsops.metrics_tools")
    ap.add_argument("--config", required=True)
    ap.add_argument("cmd", choices=["pull"])
    ap.add_argument("--backfill", type=int, default=90)
    a = ap.parse_args(argv)
    cfg = config_mod.load_config(a.config)
    tools = {}

    class Srv:
        def tool(self, name, desc, schema):
            def deco(fn):
                tools[name] = fn
                return fn
            return deco
    register(Srv(), Context(cfg, now=time.time), cfg)
    out = tools["adsops_metrics_pull"]({"backfill_days": a.backfill})
    print(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **out}, default=str))
    errs = [k for k, v in out["pulled"].items() if isinstance(v, dict) and v.get("error")]
    return 1 if errs else 0


def selftest():
    import tempfile
    n = 0
    td = tempfile.mkdtemp(prefix="adrails-metrics-")
    audit = os.path.join(td, "audit.jsonl")
    lines = [json.dumps({"at": "2026-10-01T10:00:00+00:00", "tool": "adsops_set", "campaign": "c1", "summary": ["budget 50 -> 25"], "applied": True}),
             json.dumps({"at": "2026-10-01T11:00:00+00:00", "tool": "adsops_set", "campaign": "c1", "summary": ["+3 negatives"], "applied": False}),
             json.dumps({"at": "2026-10-01T12:00:00+00:00", "tool": "adsops_rollback", "campaign": "c1"}),
             json.dumps({"at": "2026-10-05T12:00:00+00:00", "tool": "adsops_guard", "campaign": "c1", "commit": True, "paused": False, "why": "ok"}),
             json.dumps({"at": "2026-10-05T13:00:00+00:00", "tool": "adsops_guard", "campaign": "c1", "commit": True, "paused": True, "why": "daily spend cap"}),
             json.dumps({"at": "2026-10-02T10:00:00+00:00", "tool": "adsops_apply", "campaign": "c1", "commit": False, "ops": {"x": 3}}),
             json.dumps({"at": "2026-10-03T10:00:00+00:00", "tool": "adsops_apply", "campaign": "c1", "commit": True, "ops": {"campaignCriteria": 2}, "notes": ["2 new negatives"]}),
             json.dumps({"at": "2026-10-04T10:00:00+00:00", "tool": "adsops_status", "campaign": "c1"}), "not json"]
    open(audit, "w").write("\n".join(lines) + "\n")
    evs = change_events(open(audit), resolve=lambda k: "42")
    assert [e["kind"] for e in evs] == ["set", "guard", "apply"] and "daily spend cap" in evs[1]["summary"] and evs[0]["entity_id"] == "42" and "budget 50 -> 25" in evs[0]["summary"], evs; n += 1
    assert evs[0]["ref"] != evs[1]["ref"] and len(evs[0]["ref"]) == 32; n += 1
    lf = leads_facts([{"created_at": "2026-10-01T09:00:00Z", "source": "google_ads", "stage": "won", "value": 900, "name": "Jane", "phone": "555"},
                      {"created_at": "2026-10-01T10:00:00Z", "source": "google_ads", "stage": "new"},
                      {"created_at": "2026-10-01T11:00:00Z", "source": "google_ads", "stage": "spam"},
                      {"created_at": "2025-01-01T11:00:00Z", "source": "gbp", "stage": "new"}], "2026-09-01", "2026-10-31")
    got = {f["metric"]: f["value"] for f in lf if f["day"] == "2026-10-01"}
    zero = [f for f in lf if f["day"] == "2026-10-02" and f["metric"] == "leads"]
    assert zero and zero[0]["value"] == 0 and max(f["day"] for f in lf) == "2026-10-31"; n += 1   # review #2: no-lead days are 0
    assert got == {"leads": 2, "won": 1, "won_value": 900.0} and "Jane" not in json.dumps(lf) and "555" not in json.dumps(lf); n += 1
    af = aeo_facts([{"ts": "2026-10-01T01:00:00Z", "results": [{"engine": "chatgpt", "prompt": "best x", "mentioned": True}]},
                    {"ts": "2026-10-01T05:00:00Z", "results": [{"engine": "chatgpt", "prompt": "best x", "mentioned": False, "cited": True},
                                                               {"engine": "chatgpt", "prompt": "Acme reviews", "mentioned": True}]}],
                   "2026-09-01", "2026-10-31", brand="Acme")
    got = {f["metric"]: f["value"] for f in af}
    assert got == {"answers": 1, "mentioned": 0, "cited": 1}, got; n += 1      # last run of the day wins; branded prompt dropped
    assert _match_sites(["a.com", "b.com", "sc-domain:c.com"], ["https://a.com/", "sc-domain:b.com"]) == ["https://a.com/", "sc-domain:b.com", "sc-domain:c.com"]; n += 1

    # tools end to end with fake Ads + fake GSC
    tools = {}

    class Srv:
        def tool(self, name, desc, schema):
            def deco(fn):
                tools[name] = fn
                return fn
            return deco

    import random
    rnd = random.Random(3)
    base = dt.date(2026, 9, 1)

    class FakeAds:
        def search(self, q):
            out = []
            for i in range(60):
                d = (base + dt.timedelta(days=i)).isoformat()
                if not (q.split("BETWEEN '")[1][:10] <= d <= q.split("AND '")[1][:10]):
                    continue
                common = 4 * ((i * 7919) % 11) / 10          # a shared daily swing both campaigns feel (season, weather)
                for cid, lift in (("42", 1.5 if i >= 40 else 1.0), ("43", 1.0)):
                    conv = (10 + common + rnd.random()) * lift
                    if "search_term_view" in q:
                        out.append({"segments": {"date": d}, "searchTermView": {"searchTerm": "Plumber Tampa"},
                                    "metrics": {"costMicros": "5000000", "clicks": "2", "impressions": "10", "conversions": 0}})
                    else:
                        out.append({"segments": {"date": d}, "campaign": {"id": cid, "name": "c" + cid},
                                    "metrics": {"costMicros": str(int(50e6)), "clicks": "20", "impressions": "200", "conversions": conv}})
            return out

    class FakeGsc:
        def sites(self):
            return [{"site": "https://a.com/", "permission": "siteOwner"}]

        def query(self, site, s, e, dims, **k):
            if dims == ["query"]:
                return [{"keys": ["plumber tampa"], "clicks": 30, "impressions": 300, "ctr": 0.1, "position": 2.0},
                        {"keys": ["water heater repair"], "clicks": 1, "impressions": 400, "ctr": 0.0025, "position": 8.0}]
            return [{"keys": [s] + ["x"] * (len(dims) - 1), "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 5.0}]

    state = os.path.join(td, "c1.json")
    json.dump({"customer_id": "111-222-3333", "campaign_id": 42}, open(state, "w"))
    cfg = {"tenant": "t", "audit_log": audit, "accounts": [{"key": "acct", "platform": "google_ads", "customer_id": "111-222-3333",
            "campaigns": [{"key": "c1", "state_file": state, "url_domains": ["a.com"]}]}]}

    class Ctx:
        now = staticmethod(lambda: dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc).timestamp())
        client_factory = staticmethod(lambda a: FakeAds())

    register(Srv(), Ctx(), cfg, gsc_client=FakeGsc())
    assert db_path(cfg) == os.path.join(td, "metrics.sqlite"); n += 1          # beside the audit log, never ~/.adsops in a test
    r = tools["adsops_metrics_pull"]({"backfill_days": 60})
    assert r["pulled"]["account:acct"]["rows"] > 0 and r["pulled"]["site:https://a.com/"]["rows"] > 0, r; n += 1
    assert r["pulled"]["changes"]["new"] == 3, r["pulled"]["changes"]; n += 1
    r2 = tools["adsops_metrics_pull"]({"backfill_days": 60})
    assert r2["pulled"]["changes"]["new"] == 0, "re-pull adds no duplicate events"; n += 1
    cov = {c["source"]: c for c in tools["adsops_metrics"]({"action": "coverage"})["coverage"]}
    assert set(cov) >= {"google_ads", "search_console"}; n += 1
    s = tools["adsops_metrics"]({"action": "series", "source": "google_ads", "metric": "conversions", "entity_type": "campaign",
                                 "entity_ids": ["42"], "day_from": "2026-09-01", "day_to": "2026-09-05"})["series"]
    assert len(s) == 5 and all(p["value"] for p in s); n += 1
    acct = tools["adsops_metrics"]({"action": "series", "source": "google_ads", "metric": "cost", "day_from": "2026-09-01", "day_to": "2026-09-01"})["series"]
    assert acct[0]["value"] == 100.0, acct; n += 1      # two campaigns x $50; the $5 search-term row must NOT be added on top
    top = tools["adsops_metrics"]({"action": "top", "source": "google_ads", "metric": "cost", "entity_type": "search_term", "days": 30})["top"]
    assert top and top[0]["entity_id"] == "plumber tampa"; n += 1
    ov = tools["adsops_search_console"]({"action": "overlap", "site": "https://a.com/"})
    assert ov["overlap"] and ov["overlap"][0]["term"] == "plumber tampa", ov; n += 1
    op = tools["adsops_search_console"]({"action": "opportunities"})
    assert op["site"] == "https://a.com/" and op["opportunities"] and op["ads_terms_used"]; n += 1
    ch = tools["adsops_impact"]({"action": "changes"})["changes"]
    assert len(ch) == 3; n += 1
    m = tools["adsops_impact"]({"action": "measure", "day": "2026-10-11", "campaign_id": "42", "metric": "conversions"})
    assert m["status"] == "ok" and m["verdict"] == "helped" and m["method"] == "controls", {k: m.get(k) for k in ("status", "verdict", "method", "summary")}; n += 1
    early = tools["adsops_impact"]({"action": "measure", "day": "2026-10-28", "campaign_id": "42"})
    assert early["status"] == "too_early", early.get("status"); n += 1
    try:
        tools["adsops_impact"]({"action": "measure", "ref": "nope"})
        raise AssertionError("unknown ref must be refused")
    except ToolError:
        n += 1
    return n


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        print("ALL PASS (%d checks)" % selftest(), file=sys.stderr)
    else:
        sys.exit(main())
