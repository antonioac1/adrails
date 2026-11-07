"""adsops_results: before-and-after results for a business using AdRails (2026-11-06).

Three readers, one report: the owner (is this working?), an agency's client (client-ready, no customer data), and the AdRails case
study (action=case_study returns a privacy-safe JSON the owner may choose to email; nothing is sent from here). The math lives in
adsops/local/results.py (pure, DeepSeek-built from specs/ent/local_results.md, reviewed); this file only gathers the inputs:
lead book (leads + stage events), the AI-visibility history aeo_visibility_record writes, the saved profile and the plan.
Spend and review counts are NOT stored by AdRails, so the agent passes them in (from adsops_lsa_report / adsops_campaigns /
the Google Business Profile); missing inputs become named gaps, never invented zeros.
"""
import datetime as dt
import os

from .errors import ToolError
from .local import leadbook, results


def register(srv, ctx, cfg):
    from .enterprise_tools import state_path
    from .local_tools import _load, _paths
    P = _paths(cfg)

    def vis_path():
        a = cfg.get("aeo") or {}
        return os.path.expanduser(a["visibility_log"]) if a.get("visibility_log") else state_path(cfg, "_none", "aeo-visibility.jsonl")

    money = {"type": "object", "additionalProperties": {"type": "number"},
             "description": "dollars by lead source, e.g. {\"google_lsa\": 420, \"google_ads\": 300}"}

    def _money_map(v, name):
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ToolError("%s must be an object of source -> dollars" % name)
        for k, x in v.items():
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or x in (float("inf"), float("-inf")) or x < 0:
                raise ToolError("%s[%s] must be a non-negative number of dollars" % (name, k))
        return v

    def _reviews(v):
        if v is None:
            return None
        for k in ("baseline_count", "current_count"):
            x = v.get(k)
            if x is not None and (isinstance(x, bool) or not isinstance(x, int) or x < 0):
                raise ToolError("reviews.%s must be a whole number" % k)
        for k in ("baseline_rating", "current_rating"):
            x = v.get(k)
            if x is not None and (isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 5):
                raise ToolError("reviews.%s must be a number from 0 to 5" % k)
        return v

    @srv.tool("adsops_results", "Before-and-after results for the business: leads, jobs won and value, close rate, cost per lead by "
              "channel, first-response time, AI share of answer, reviews and plan progress, comparing the period before AdRails with "
              "the latest period. action=report (default) returns numbers, highlights, gaps and owner-readable markdown (client-ready "
              "for agencies). action=case_study returns a privacy-safe summary the OWNER may choose to email to the AdRails team (trade "
              "and city only unless permission='name'; never customer names, phones or emails). Pass spend/baseline_spend and reviews "
              "from adsops_lsa_report, adsops_campaigns and the Google Business Profile; missing data is listed as gaps, never guessed.",
              {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["report", "case_study"]},
                  "baseline_days": {"type": "integer"}, "current_days": {"type": "integer"},
                  "started_using": {"type": "string", "description": "YYYY-MM-DD the business started with AdRails (default: profile, "
                                                                   "else the plan start date)"},
                  "spend": money, "baseline_spend": money,
                  "reviews": {"type": "object", "properties": {"baseline_count": {"type": "integer"}, "current_count": {"type": "integer"},
                                                               "baseline_rating": {"type": "number"}, "current_rating": {"type": "number"}},
                              "additionalProperties": False},
                  "quote": {"type": "string", "description": "case_study: the owner's own words, verbatim (optional)"},
                  "permission": {"type": "string", "enum": ["anonymous", "name"],
                                 "description": "case_study: may the AdRails team name the business? ask the owner"}},
               "required": [], "additionalProperties": False})
    def results_tool(args):
        args = {**args, "spend": _money_map(args.get("spend"), "spend"),
                "baseline_spend": _money_map(args.get("baseline_spend"), "baseline_spend"), "reviews": _reviews(args.get("reviews"))}
        book = leadbook.LeadBook(P["book"], now=ctx.now)
        data = book._ensure()
        prof = _load(P["profile"], {}) or {}
        stored = _load(P["plan"], None) or {}
        plan = stored.get("plan")
        started = args.get("started_using") or prof.get("started_using") or (plan or {}).get("start_date")
        hist = []
        if os.path.isfile(vis_path()):
            from .aeo import visibility
            hist = visibility.load_history(vis_path())
        today = dt.date.fromtimestamp(ctx.now()).isoformat()
        rep = results.build(today, data.get("leads", []), data.get("events", []), visibility=hist,
                            spend=args.get("spend"), baseline_spend=args.get("baseline_spend"), reviews=args.get("reviews"),
                            plan=plan, done_ids=stored.get("done", []), profile={**prof, "started_using": started},
                            brand=prof.get("business_name"), baseline_days=int(args.get("baseline_days") or 30),
                            current_days=int(args.get("current_days") or 30))
        if (args.get("action") or "report") == "case_study":
            if args.get("permission") not in (None, "anonymous", "name"):
                raise ToolError("permission must be anonymous or name")
            cs = results.to_case_study(rep, quote=args.get("quote"), permission=args.get("permission"))
            return {"case_study": cs, "next": "show the owner this exact JSON; send it only if they say yes (they send it, or "
                                              "you draft the email for them to send)"}
        return {**rep, "markdown": results.to_markdown(rep)}


def register_gbp(srv, ctx, cfg):
    """adsops_gbp: Google Business Profile to 100% (pure checks in adsops/local/gbp.py; DeepSeek-built from specs/ent/local_gbp.md)."""
    import json
    import time
    from .enterprise_tools import state_path
    from .local import gbp
    hist = state_path(cfg, "gbp_history", "gbp-history.jsonl")

    @srv.tool("adsops_gbp", "Google Business Profile to 100%: action=audit scores a profile snapshot (fill it by reading the owner's "
              "GBP dashboard: categories, description, services, attributes, hours, photos, posts, Q&A, reviews, service area...) and "
              "returns the score, grade, ranked fixes and warnings (category stuffing, URL in description, address shown on a "
              "service-area business); each audit is kept so progress shows over time. action=description checks a description against "
              "Google's rules before the owner pastes it. action=posts returns a 2-a-week post plan rotating the services. Read the "
              "google-business-profile playbook first. Nothing here edits the profile: the owner (or you in their browser) does.",
              {"type": "object", "properties": {
                  "action": {"type": "string", "enum": ["audit", "description", "posts", "history"]},
                  "snapshot": {"type": "object"}, "text": {"type": "string"}, "business_name": {"type": "string"},
                  "services": {"type": "array", "items": {"type": "string"}}, "weeks": {"type": "integer"},
                  "per_week": {"type": "integer"}, "cta_url": {"type": "string"}},
               "required": ["action"], "additionalProperties": False})
    def gbp_tool(args):
        a = args["action"]
        if a == "audit":
            r = gbp.audit(args.get("snapshot") or {})
            try:                                   # score history only (no profile text): progress over time for adsops_results
                with open(hist, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now())), "score": r["score"],
                                         "grade": r["grade"]}) + "\n")
            except OSError:
                pass
            return r
        if a == "description":
            if not args.get("text"):
                raise ToolError("description needs text")
            return gbp.check_description(args["text"], args.get("business_name"))
        if a == "posts":
            return {"posts": gbp.post_plan(args.get("services") or [], weeks=int(args.get("weeks") or 4),
                                           per_week=int(args.get("per_week") or 2), cta_url=args.get("cta_url"))}
        rows = []
        if os.path.isfile(hist):
            for line in open(hist, encoding="utf-8"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return {"history": rows[-50:]}
