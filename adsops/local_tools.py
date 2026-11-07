"""Local-business growth suite (the maintainers glue, 2026-11-06): everything a small service business needs to get leads from Google
(Local Services Ads, Search, Maps) and from AI assistants, run from the owner's own Claude.

  adsops_local_profile   the business profile the plan is built from (ask the owner, save once)
  adsops_local_plan      generic 90-day growth plan, customised from the profile; every task names the tool that does it
  adsops_leads           the lead book: one local CRM for every lead from every channel (add/move/note/find/import/export/pipeline)
  adsops_leads_due       speed-to-lead: who is waiting, who is overdue for a follow-up, today
  adsops_local_schema    LocalBusiness JSON-LD for the website (service areas, services, hours, license) with warnings
  adsops_local_nap       name/address/phone consistency across listing pages + the citation checklist
  adsops_local_area_page a brief for one service x area page that answers what people ask AI assistants
  adsops_reviews         review link, request drafts (never sent), reply drafts, review velocity vs competitors

Logic lives in adsops/local/* (DeepSeek-built from specs/ent/local_*.md, black-box tested). State lives BESIDE the config
(state_path), never in ~/.adsops: lead book, profile and plan are plain JSON files the owner owns. Nothing here sends a message,
spends money or touches an ad account; the LSA tools (lsa_tools.py) do the account reads and gated writes.
"""
import json
import os

from .errors import ToolError
from .local import leadbook, plan as lplan, presence, reviews

SOURCES_ENUM = ["google_lsa", "google_ads", "website", "ai_assistant", "google_organic", "gbp", "referral", "direct_mail", "phone",
                "walk_in", "social", "other"]


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _paths(cfg):
    from .enterprise_tools import state_path
    return {"book": state_path(cfg, "lead_book", "leads.json"), "profile": state_path(cfg, "local_profile", "business-profile.json"),
            "plan": state_path(cfg, "local_plan", "growth-plan.json")}


class _locked:
    """Cross-process lock around load-modify-save of a JSON state file (two MCP clients can run two servers; review #13)."""
    def __init__(self, path):
        self.path = path + ".lock"

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except ImportError:                                  # Windows
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc):
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        except ImportError:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        self.fh.close()


def _load(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # profile can hold phone/email: owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def register(srv, ctx, cfg):
    P = _paths(cfg)

    def book():
        return leadbook.LeadBook(P["book"], now=ctx.now)

    def today():
        import datetime as dt
        return dt.datetime.fromtimestamp(ctx.now()).strftime("%Y-%m-%d")

    @srv.tool("adsops_local_profile", "Local business: read or save the business profile everything else is built from (trade, city, "
              "service area, services, phone, website, Google Business Profile status, reviews, LSA eligibility, budget, average job "
              "value, close rate, goals, competitors). action=questions lists what to ask the owner; action=set merges fields and "
              "returns what is still missing plus the lead economics (max cost per lead the business can afford).",
              _schema({"action": {"type": "string", "enum": ["questions", "get", "set"]}, "fields": {"type": "object"}}, ["action"]))
    def profile(args):
        if args["action"] == "questions":
            return {"questions": lplan.PROFILE_FIELDS}
        prof = _load(P["profile"], {})
        if args["action"] == "set":
            f = args.get("fields") or {}
            if not isinstance(f, dict) or not f:
                raise ToolError("pass fields={...}")
            prof.update(f)
            _save(P["profile"], prof)
        return {"profile": prof, "validation": lplan.validate_profile(prof), "economics": lplan.economics(prof), "file": P["profile"]}

    @srv.tool("adsops_local_plan", "Local business: the 90-day growth plan for getting leads from Google (LSA, Search, Maps) and AI "
              "assistants. action=build makes it from the saved profile (generic plan, customised: skips what is done, organic-only "
              "with no budget, LSA only when eligible); action=progress marks tasks done and returns what is next and overdue; "
              "action=markdown returns the owner-readable checklist. Every task names the AdRails tool that does it.",
              _schema({"action": {"type": "string", "enum": ["build", "progress", "markdown", "get"]},
                       "start_date": {"type": "string"}, "done": {"type": "array", "items": {"type": "string"}},
                       "rebuild": {"type": "boolean"}}, ["action"]))
    def growth_plan(args):
        stored = _load(P["plan"], None)
        if args["action"] == "build":
            if stored and not args.get("rebuild"):
                raise ToolError("a plan already exists (%s); pass rebuild=true to replace it (done marks are kept)" % P["plan"])
            prof = _load(P["profile"], {})
            v = lplan.validate_profile(prof)
            if not v["ok"]:
                raise ToolError("profile incomplete, ask the owner for: " + ", ".join(v["missing"]) + " (adsops_local_profile)")
            pl = lplan.build(prof, start_date=args.get("start_date") or today())
            ids = {t["id"] for t in pl["tasks"]}
            doc = {"plan": pl, "done": [d for d in (stored or {}).get("done", []) if d in ids]}   # review #8
            _save(P["plan"], doc)
            return {**pl, "file": P["plan"]}
        if not stored:
            raise ToolError("no plan yet: adsops_local_plan action=build")
        if args["action"] == "progress":
            ids = {t["id"] for t in stored["plan"]["tasks"]}
            bad = [d for d in (args.get("done") or []) if d not in ids]
            done = sorted((set(stored.get("done", [])) & ids) | set(args.get("done") or []))
            if bad:
                raise ToolError("unknown task ids: " + ", ".join(bad))
            stored["done"] = done
            _save(P["plan"], stored)
            return lplan.progress(stored["plan"], done, today())
        if args["action"] == "markdown":
            return {"markdown": lplan.to_markdown(stored["plan"]), "done": stored.get("done", [])}
        return stored

    @srv.tool("adsops_leads", "Lead book (local CRM in a JSON file beside the config): action=add (any channel; dedupes by source id, "
              "phone or email), move (stage: new, contacted, quoted, booked, won, lost, spam; won needs value, lost needs reason), "
              "note (with optional follow_up date or +Nd), get, find (stage/source/text/since/until), import (rows + source + column "
              "mapping), export (CSV), pipeline (counts, win rate and value by source, median first-contact minutes), "
              "conversions (won jobs with a gclid, ready for adsops_offline_conv_plan).",
              _schema({"action": {"type": "string", "enum": ["add", "move", "note", "get", "find", "import", "export", "pipeline",
                                                              "conversions"]},
                       "lead": {"type": "object", "description": "for add: name, phone, email, source, source_id, service, area, "
                                                                 "value, gclid, tags"},
                       "id": {"type": "string"}, "stage": {"type": "string"}, "value": {"type": "number"},
                       "lost_reason": {"type": "string"}, "note": {"type": "string"}, "follow_up": {"type": "string"},
                       "source": {"type": "string", "enum": SOURCES_ENUM}, "text": {"type": "string"}, "since": {"type": "string"},
                       "until": {"type": "string"}, "limit": {"type": "integer"}, "rows": {"type": "array", "items": {"type": "object"}},
                       "mapping": {"type": "object"}}, ["action"]))
    def leads(args):
        with _locked(P["book"]):
            return _leads(args)

    def _leads(args):
        b, a = book(), args["action"]
        if a == "add":
            return b.upsert(args.get("lead") or {})
        if a == "move":
            return b.move(args.get("id") or "", args.get("stage") or "", note=args.get("note"), value=args.get("value"),
                          lost_reason=args.get("lost_reason"))
        if a == "note":
            return b.note(args.get("id") or "", args.get("note") or "", follow_up=args.get("follow_up"))
        if a == "get":
            return b.get(args.get("id") or "")
        if a == "find":
            return b.find(stage=args.get("stage"), source=args.get("source"), text=args.get("text"), since=args.get("since"),
                          until=args.get("until"), limit=min(int(args.get("limit") or 50), 500))
        if a == "import":
            if not args.get("rows") or not args.get("source"):
                raise ToolError("import needs rows and source")
            return b.import_rows(args["rows"], args["source"], mapping=args.get("mapping"))
        if a == "export":
            return {"csv": b.export_csv()}
        if a == "pipeline":
            return b.pipeline(since=args.get("since"), until=args.get("until"))
        if not (args.get("since") and args.get("until")):
            raise ToolError("conversions needs since and until (YYYY-MM-DD)")
        try:
            return {"conversions": b.conversions(args["since"], args["until"])}
        except ValueError:
            raise ToolError("since/until must be real dates, YYYY-MM-DD")

    @srv.tool("adsops_leads_due", "Speed-to-lead check (run it every morning and after lunch): new leads still waiting for a first "
              "contact (oldest first, minutes waiting), follow-ups overdue, and follow-ups due today. Leads answered within 5 "
              "minutes book far more often, and Google ranks responsive LSA advertisers higher.",
              _schema({}))
    def leads_due(args):
        return book().due()

    @srv.tool("adsops_local_schema", "Local business: generate LocalBusiness JSON-LD (the right schema.org type for the trade, service "
              "areas, services, hours, license, profile links) ready to paste into the website head, with warnings. Uses the saved "
              "profile unless business is passed. Never emits self-serving review stars.",
              _schema({"business": {"type": "object"}}))
    def local_schema(args):
        biz = args.get("business") or _biz_from_profile(_load(P["profile"], {}))
        return presence.schema(biz)

    @srv.tool("adsops_local_nap", "Local business: check that name, address and phone match exactly across listing pages (Google, Bing, "
              "Apple, Yelp, BBB, Facebook, directories) — inconsistent NAP costs Maps and AI-assistant visibility. Pass urls to fetch "
              "(or pages with text). action=citations returns the prioritised listing-site checklist instead.",
              _schema({"action": {"type": "string", "enum": ["check", "citations"]}, "urls": {"type": "array", "items": {"type": "string"}},
                       "pages": {"type": "array", "items": {"type": "object"}}, "canonical": {"type": "object"}}))
    def local_nap(args):
        prof = _load(P["profile"], {})
        if args.get("action") == "citations":
            return {"citations": presence.citations_checklist(prof.get("trade") or "")}
        pages = list(args.get("pages") or [])
        if args.get("urls"):
            from .aeo_tools import safe_fetch
            for u in args["urls"][:25]:
                try:
                    status, _, html = safe_fetch(u)
                    if int(status) >= 400:
                        pages.append({"url": u, "text": "", "error": "HTTP %s" % status})
                    else:
                        pages.append({"url": u, "text": _text(html)})
                except Exception as e:                  # one dead listing must not sink the check
                    pages.append({"url": u, "text": "", "error": str(e)[:200]})
        if not pages:
            raise ToolError("pass urls or pages")
        canon = args.get("canonical") or {"name": prof.get("business_name"), "phone": prof.get("phone"),
                                          "street": (prof.get("address") or {}).get("street") if isinstance(prof.get("address"), dict) else prof.get("address"),
                                          "city": prof.get("city"), "postal": prof.get("postal")}
        good = [p for p in pages if not p.get("error") and p.get("text")]
        failed = [{"url": p["url"], "issues": ["could not read this page: " + (p.get("error") or "empty response")]}
                  for p in pages if p.get("error") or not p.get("text")]
        out = presence.nap_check(canon, good) if good else {"results": [], "score": None, "summary": "no page could be read"}
        out["unreadable"] = failed          # never scored as "business name not found" (review #12)
        return out

    @srv.tool("adsops_local_area_page", "Local business: brief for ONE service x area page (title, meta, H1, outline, the questions "
              "people ask ChatGPT/Gemini about that service there, what to include and what to avoid). The agent then writes the "
              "page from it; publish, then ping aeo_indexnow.",
              _schema({"service": {"type": "string"}, "area": {"type": "string"},
                       "competitors": {"type": "array", "items": {"type": "string"}}}, ["service", "area"]))
    def area_page(args):
        return presence.area_page_brief(_biz_from_profile(_load(P["profile"], {})), args["area"], args["service"],
                                        competitors=args.get("competitors"))

    @srv.tool("adsops_reviews", "Reviews engine (reviews drive LSA and Maps ranking): action=link (Google write-a-review link from the "
              "Place ID), request (SMS/email request DRAFTS for a customer; AdRails never sends them), reply (reply draft for a "
              "review, flags ones the owner must handle personally), velocity (pace, average, reviews needed to hit 4.5/4.8, gap vs "
              "competitors). Policy built in: ask every customer, no gating, no incentives.",
              _schema({"action": {"type": "string", "enum": ["link", "request", "reply", "velocity"]}, "place_id": {"type": "string"},
                       "customer_first_name": {"type": "string"}, "service": {"type": "string"},
                       "channel": {"type": "string", "enum": ["sms", "email"]}, "tone": {"type": "string", "enum": ["friendly", "brief"]},
                       "review": {"type": "object"}, "reviews": {"type": "array", "items": {"type": "object"}},
                       "gbp_reviews": {"type": "array", "items": {"type": "object"}},
                       "competitors": {"type": "array", "items": {"type": "object"},
                                       "description": "[{name, count, rating}] (count = their number of Google reviews)"}}, ["action"]))
    def reviews_tool(args):
        prof = _load(P["profile"], {})
        name = prof.get("business_name") or "our business"
        pid = args.get("place_id") or prof.get("place_id")
        a = args["action"]
        if a == "link":
            if not pid:
                raise ToolError("no place_id: find it with Google's Place ID finder and save it in the profile (place_id)")
            return {"link": reviews.review_link(pid)}
        if a == "request":
            if not pid:
                raise ToolError("no place_id in the profile yet")
            return reviews.request_drafts(name, args.get("customer_first_name") or "there", args.get("service") or "",
                                          reviews.review_link(pid), args.get("channel", "sms"), args.get("tone", "friendly"))
        if a == "reply":
            if not args.get("review"):
                raise ToolError("pass review={rating, text, author_first_name}")
            return reviews.reply_draft(args["review"], name, owner_name=prof.get("owner_name"))
        rows = list(args.get("reviews") or []) + reviews.parse_gbp_reviews(args.get("gbp_reviews") or [])
        return reviews.velocity(rows, competitors=args.get("competitors"))

    # MCP prompts: the owner (or his Claude) picks one and the whole job runs. Plain words, no setup knowledge needed.
    srv.prompt("start", "Set up my business: interview me, build my 90-day lead plan, and start on it",
               render=lambda a: START_PROMPT)
    srv.prompt("morning", "Morning check: new leads waiting, follow-ups due, LSA leads to sync and dispute, today's plan tasks",
               render=lambda a: MORNING_PROMPT)
    srv.prompt("weekly", "Weekly review: leads and cost per lead by channel, LSA disputes, reviews, AI visibility, next week's plan",
               render=lambda a: WEEKLY_PROMPT)


START_PROMPT = """You are my marketing operator, using the AdRails tools. Goal: get my business real leads from Google (Local Services Ads,
Search, Maps) and from AI assistants, and keep every lead organised.
1. Call adsops_local_profile action=questions, then interview me in plain English, a few questions at a time. Save answers with
   action=set. Look up what you can yourself (my website, my Google Business Profile, my reviews, competitors) instead of asking.
2. Read the playbooks: adsops_playbook name=local-leads, then name=aeo (its Fast path section first if the business is new).
   They hold the tested strategy; follow them while you work the plan.
3. Call adsops_local_plan action=build and show me the plan as a short checklist (action=markdown). Tell me only the steps that
   need ME (signups, verification, photos), with how long each takes.
4. Start the first tasks that need no one but you, using the tool each task names. Mark finished tasks with action=progress.
Never spend money, enable a campaign or send a message to a customer without asking me first."""

MORNING_PROMPT = """Morning check with the AdRails tools: 1) adsops_lsa_leads (sync new Local Services leads into the lead book) if I
run LSA; 2) adsops_leads_due: list who is waiting and for how long, with a one-line suggested reply for each; 3) flag LSA leads that look
like spam or wrong job for a credit dispute (adsops_lsa_dispute, plan only); 4) adsops_local_plan action=progress: today's tasks; 5) adsops_update_check: if a newer
AdRails is out, one line saying so and offering to upgrade. Keep it under 15 lines; lead with anything urgent."""

WEEKLY_PROMPT = """Weekly review with the AdRails tools: adsops_leads action=pipeline (leads, wins and cost per lead by channel),
adsops_lsa_report (cost per lead vs my max cost per lead, booking rate, best hours), adsops_lsa_dispute plan for the week's bad leads
(ask me before submitting), adsops_reviews action=velocity, aeo_visibility_record if a month has passed since the last AI-visibility
check, then adsops_local_plan action=progress. Finish with 3 recommendations, each with the tool call that does it."""


def _biz_from_profile(p):
    """Profile (owner words) -> presence.schema biz dict."""
    if not p.get("business_name"):
        raise ToolError("no business profile yet: run adsops_local_profile first")
    areas = p.get("service_area") or []
    if isinstance(areas, str):
        areas = [a.strip() for a in areas.split(",") if a.strip()]
    services = p.get("services") or []
    if isinstance(services, str):
        services = [s.strip() for s in services.split(",") if s.strip()]
    return {k: v for k, v in {
        "name": p.get("business_name"), "type": p.get("schema_type") or _TYPE.get((p.get("trade") or "").lower().strip(), "LocalBusiness"),
        "url": p.get("website") or None, "phone": p.get("phone"), "email": p.get("email"),
        "address": p.get("address") if isinstance(p.get("address"), dict) else None,
        "areas": areas, "services": services, "hours": p.get("hours") if isinstance(p.get("hours"), dict) else None,
        "price_range": p.get("price_range"), "logo": p.get("logo"), "same_as": p.get("profiles") or [], "license": p.get("license"),
    }.items() if v}


_TYPE = {"plumber": "Plumber", "plumbing": "Plumber", "electrician": "Electrician", "electrical": "Electrician", "hvac": "HVACBusiness",
         "roofing": "RoofingContractor", "roofer": "RoofingContractor", "painter": "HousePainter", "painting": "HousePainter",
         "locksmith": "Locksmith", "moving": "MovingCompany", "movers": "MovingCompany", "general contractor": "GeneralContractor",
         "auto repair": "AutoRepair", "lawyer": "LegalService", "attorney": "LegalService", "dentist": "Dentist"}


def _text(html):
    import re
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:200_000]
