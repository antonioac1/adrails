"""Coach tools (0.13.0): the small-business workflow as TOOLS, not only MCP prompts.

Why: some MCP clients (ChatGPT developer mode among them) never surface MCP prompts, so the `start` / `morning` / `weekly` /
`monthly` routines were invisible there and the assistant never interviewed the owner or proposed work. These four read-only
tools carry the same workflow, and every result ends with `next_step` ({say, call, args_hint}) so any model keeps moving:

  adsops_get_started      interview in rounds (<=4 questions), merged with the saved business profile; returns the routines
  adsops_setup_checklist  17-item SEO/AEO setup checklist (GBP, reviews, Bing Places, Apple Business Connect, Search Console,
                          Bing Webmaster, IndexNow, AI crawlers, llms.txt, schema, NAP, directories, pages) with a score
  adsops_page_plan        which landing pages to build, ranked by lead value, with a brief for the first one
  adsops_cadence          the weekly/monthly routine sized to the owner's hours, plus scheduled-task text

The logic lives in adsops/local/coach_onboard.py and coach_pages.py (pure, stdlib, shared verbatim with the hosted no-login
app at https://adrails.dev/chatgpt/mcp). This file only maps names: the saved local profile (trade, city, review_count) to the
coach vocabulary (industry, service_area, reviews_count), and the hosted tool names in next_step to the local ones.
Nothing here writes: saving answers stays with adsops_local_profile action=set (we hand back the exact fields to save).
"""
import contextlib
import io

from .errors import ToolError
from .local import coach_onboard as co
from .local import coach_pages as cp

# hosted name -> local name (None = no local twin; the step is then explained in words only)
NAMES = {"get_started": "adsops_get_started", "setup_checklist": "adsops_setup_checklist",
         "plan_landing_pages": "adsops_page_plan", "weekly_cadence": "adsops_cadence",
         "audit_site": "aeo_site_audit", "audit_page": "aeo_page_audit", "check_llms_txt": "aeo_llms_txt",
         "build_llms_txt": "aeo_llms_txt", "generate_schema": "aeo_schema_generate", "find_content_gaps": "aeo_page_audit"}
LOCAL_TO_COACH = {"trade": "industry", "review_count": "reviews_count"}
COACH_TO_LOCAL = {v: k for k, v in LOCAL_TO_COACH.items()}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


PROFILE = {"type": "object", "description": "Profile fields to use on top of the saved business profile (optional): business_name, "
           "website, industry, services, service_area, state, customers, ideal_lead, competitors, differentiators, has_gbp, "
           "reviews_count, publishing, owner_hours_per_week, goals, licenses"}
DONE = {"type": "array", "items": {"type": "string"}, "maxItems": 50, "description": "Checklist item ids already finished"}


def from_local(saved, extra=None):
    """Saved local profile + per-call fields -> coach profile."""
    out = {}
    for k, v in (saved or {}).items():
        out[LOCAL_TO_COACH.get(k, k)] = v
    for k in ("services", "service_area", "competitors", "differentiators"):     # local profiles often hold "a, b, c"
        if isinstance(out.get(k), str):
            out[k] = [x.strip() for x in out[k].split(",") if x.strip()]
    if not out.get("service_area") and out.get("city"):
        out["service_area"] = [out["city"]]
    if isinstance(out.get("has_gbp"), str):
        out["has_gbp"] = out["has_gbp"].strip().lower() in ("y", "yes", "true", "1")
    for k, v in (extra or {}).items() if isinstance(extra, dict) else ():
        if v not in (None, "", [], {}):
            out[k] = v
    return out


def _rename(step):
    """Hosted next_step -> local tool names (and argument shapes where they differ)."""
    if not isinstance(step, dict):
        return step
    call, hint = step.get("call"), step.get("args_hint") or {}
    if call == "find_content_gaps":                      # local twin checks one page against the questions
        pages = hint.get("pages") or []
        if not pages:
            return dict(step, call=None, args_hint={"questions": list(hint.get("queries") or [])[:20]})
        hint = {"url": pages[0], "questions": list(hint.get("queries") or [])[:20]}
    elif isinstance(hint, dict) and "profile" in hint:
        hint = {k: v for k, v in hint.items() if k != "profile"}   # local tools read the saved profile
    return dict(step, call=NAMES.get(call, call), args_hint=hint)


def _localise(res):
    if isinstance(res, dict):
        if "next_step" in res:
            res["next_step"] = _rename(res["next_step"])
        for key in ("items", "next_three", "weekly", "monthly", "this_week"):
            for it in res.get(key) or []:
                if isinstance(it, dict) and it.get("tool"):
                    it["tool"] = NAMES.get(it["tool"], it["tool"])
    return res


def register(srv, ctx, cfg):
    from .local_tools import _load, _paths, START_PROMPT, MORNING_PROMPT, WEEKLY_PROMPT, MONTHLY_PROMPT
    P = _paths(cfg)

    def prof(args):
        return from_local(_load(P["profile"], {}), args.get("profile"))

    def today():
        import datetime as dt
        return dt.datetime.fromtimestamp(ctx.now()).strftime("%Y-%m-%d")

    @srv.tool("adsops_get_started", "START HERE for any owner who wants more leads, SEO, AEO or local visibility. Interviews the "
              "owner a few questions at a time (merged with the saved business profile), lists what to look up yourself instead of "
              "asking, returns the exact fields to save with adsops_local_profile action=set, and the daily/weekly/monthly "
              "routines as text (for clients that do not show MCP prompts). Read-only; next_step says what to call next.",
              _schema({"profile": PROFILE}))
    def get_started(args):
        res = _localise(co.get_started(prof(args)))
        card = res.get("profile_card") or {}
        res["save_with"] = {"tool": "adsops_local_profile", "action": "set",
                            "fields": {COACH_TO_LOCAL.get(k, k): v for k, v in card.items() if v not in (None, "", [], {}) and k != "done"}}
        res["routines"] = {"start": START_PROMPT, "morning": MORNING_PROMPT, "weekly": WEEKLY_PROMPT, "monthly": MONTHLY_PROMPT}
        return res

    @srv.tool("adsops_setup_checklist", "The SEO/AEO setup checklist for a local business, in priority order: Google Business "
              "Profile, reviews, Bing Places, Apple Business Connect, Search Console, Bing Webmaster Tools, IndexNow, AI crawler "
              "access, llms.txt, schema, consistent name/address/phone, industry directories, service and answer pages. Each item: "
              "why, how, minutes, who, and the AdRails tool that helps. Pass done ids for a score and the next three. Read-only.",
              _schema({"profile": PROFILE, "done": DONE}))
    def checklist(args):
        p = prof(args)
        if not p.get("business_name") and not p.get("industry"):
            raise ToolError("no business profile yet: call adsops_get_started first")
        return _localise(co.setup_checklist(p, args.get("done")))

    @srv.tool("adsops_page_plan", "Which landing pages to build for leads: one per service, service-plus-city, a cost/coverage "
              "guide, honest comparisons, an FAQ hub and an about/credentials page, ranked by lead value, each with the buyer "
              "questions it must answer, schema types and the facts the owner must supply, plus a brief for the first page. Pass "
              "existing_pages (URLs from the sitemap) to skip what exists. One owner-reviewed page a week; never bulk-publish.",
              _schema({"profile": PROFILE, "existing_pages": {"type": "array", "maxItems": 300, "items": {"type": ["string", "object"]},
                                                              "description": "URLs (or {url,title}) already on the site"},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 25}}))
    def page_plan(args):
        p = prof(args)
        if not p.get("services") and not p.get("industry"):
            raise ToolError("no services in the profile yet: call adsops_get_started first")
        lim = args.get("limit")
        lim = max(1, min(int(lim), 25)) if isinstance(lim, int) and not isinstance(lim, bool) else 12
        return _localise(cp.plan_landing_pages(p, list(args.get("existing_pages") or [])[:300], lim))

    @srv.tool("adsops_cadence", "The owner's recurring SEO/AEO routine sized to their hours: weekly tasks (Google post, reviews, "
              "one page, submit new URLs, check leads), monthly checks (AI visibility, site audit, checklist score, competitors, "
              "refresh a page), this week's three tasks, and ready-to-paste text for a scheduled task and a calendar reminder. "
              "Pair with the morning/weekly/monthly routines from adsops_get_started. Read-only.",
              _schema({"profile": PROFILE, "done": DONE}))
    def cadence(args):
        return _localise(co.weekly_cadence(prof(args), args.get("done"), today()))

    return srv


def selftest():
    """Module selftests of the shared coach logic + the local mapping layer."""
    n = 0
    for mod in (co, cp):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                mod._selftest()
            except SystemExit as e:                     # some selftests sys.exit(0) on success
                assert not e.code, e.code
        assert buf.getvalue().strip().splitlines()[-1] == "ALL PASS", mod.__name__
        n += 1
    p = from_local({"business_name": "Acme", "trade": "plumber", "city": "Tampa", "review_count": 12, "has_gbp": "y"},
                   {"services": ["drain cleaning"], "state": "FL", "website": ""})
    assert p["industry"] == "plumber" and p["service_area"] == ["Tampa"] and p["reviews_count"] == 12 and p["has_gbp"] is True; n += 1
    assert p.get("website") is None; n += 1                                  # empty per-call values never clobber
    r = _localise(co.get_started(p))
    assert r["next_step"]["call"] in ("adsops_setup_checklist", "adsops_get_started"); n += 1
    assert "profile" not in (r["next_step"].get("args_hint") or {}); n += 1
    c = _localise(co.setup_checklist(p, ["gbp_claim"]))
    assert all(i["tool"] is None or i["tool"].startswith(("adsops_", "aeo_")) for i in c["items"]); n += 1
    pl = _localise(cp.plan_landing_pages(dict(p, website="https://example.com/"), [], 5))
    assert pl["next_step"]["call"] == "aeo_page_audit" and pl["next_step"]["args_hint"]["url"] == "https://example.com/"; n += 1
    assert pl["next_step"]["args_hint"]["questions"]; n += 1
    w = _localise(co.weekly_cadence(p, None, "2026-11-16"))
    assert all((t.get("tool") is None) or t["tool"].startswith(("adsops_", "aeo_")) for t in w["weekly"] + w["monthly"]); n += 1
    q = from_local({"business_name": "B", "trade": "roofer", "service_area": "Tampa, Largo", "services": "repair, replacement"})
    assert q["service_area"] == ["Tampa", "Largo"] and q["services"] == ["repair", "replacement"]; n += 1
    nw = _localise(cp.plan_landing_pages(q, [], 3))                          # no website: no invalid empty-url call
    assert nw["next_step"]["call"] is None and nw["next_step"]["args_hint"].get("queries"); n += 1
    assert _rename({"call": "find_content_gaps", "args_hint": {"queries": ["q"], "pages": []}})["call"] is None; n += 1
    assert _rename(None) is None and _rename({"call": "x", "args_hint": None})["call"] == "x"; n += 1
    return n
