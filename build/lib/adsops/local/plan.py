"""Growth plans for small local/service businesses.

Turns an owner's answers into a dated 90-day plan covering Google Business
Profile, Local Services Ads, Search ads, reviews, and AI-assistant (AEO)
visibility. Every task names the exact AdRails MCP tool that performs it, or
`owner` (a human must click) / `agent` (writing or thinking only).

Safety: pure functions - no I/O, network, files, or secrets; tasks never spend
money by themselves and always route through the guarded AdRails tools. A tool
should call validate_profile -> economics -> build -> progress, and render
to_markdown for the owner.
"""

import re
from datetime import date, timedelta

from adsops.errors import ToolError, RailViolation, scrub

TOOLS = (
    "adsops_local_profile", "adsops_leads", "adsops_leads_due", "adsops_lsa_leads",
    "adsops_lsa_report", "adsops_lsa_dispute", "adsops_lsa_readiness", "adsops_local_schema",
    "adsops_local_nap", "adsops_local_area_page", "adsops_reviews", "aeo_visibility_record",
    "aeo_page_audit", "aeo_schema_generate", "aeo_site_audit", "aeo_llms_txt", "aeo_indexnow",
    "adsops_plan", "adsops_apply", "adsops_status", "adsops_guard", "adsops_conv_audit",
    "adsops_lp_diagnose", "adsops_negatives_suggest", "adsops_offline_conv_plan",
)
_ALLOWED = frozenset(TOOLS) | {"owner", "agent"}  # internal only, never returned


def _yes(v):
    """Owner answers arrive as True/"y"/"yes"/"no"/"n"/""... ; bool("n") is True, so parse words (bug found 2026-11-06)."""
    if isinstance(v, str):
        return v.strip().lower() in ("y", "yes", "true", "1", "verified", "done")
    return bool(v)

PROFILE_FIELDS = [
    {"key": "business_name", "question": "What is the business called?", "required": True, "example": "Ridge Plumbing"},
    {"key": "trade", "question": "What trade or service do you do?", "required": True, "example": "plumber"},
    {"key": "city", "question": "Which city is the business based in?", "required": True, "example": "Boulder, CO"},
    {"key": "service_area", "question": "Which cities or zip codes do you serve?", "required": True, "example": ["Boulder", "Louisville"]},
    {"key": "services", "question": "What are your top 3-8 services?", "required": True, "example": ["drain cleaning", "water heaters"]},
    {"key": "phone", "question": "Which phone number should customers call?", "required": True, "example": "+1 303 555 0100"},
    {"key": "website", "question": "Do you have a website? Paste the URL or say none.", "required": False, "example": "https://example.com"},
    {"key": "has_gbp", "question": "Do you have a Google Business Profile?", "required": False, "example": "yes"},
    {"key": "gbp_verified", "question": "Is the Google Business Profile verified?", "required": False, "example": "yes"},
    {"key": "review_count", "question": "How many Google reviews do you have?", "required": False, "example": 4},
    {"key": "rating", "question": "What is your average star rating (1-5)?", "required": False, "example": 4.7},
    {"key": "has_lsa", "question": "Do you already run Local Services Ads?", "required": False, "example": "no"},
    {"key": "lsa_eligible_category", "question": "Is your trade an LSA-eligible category? (agent checks)", "required": False, "example": "yes"},
    {"key": "licensed_insured", "question": "Are you licensed and insured?", "required": False, "example": "yes"},
    {"key": "monthly_budget", "question": "What monthly ad budget can you commit, in USD?", "required": True, "example": 1500},
    {"key": "avg_job_value", "question": "What is an average job worth, in USD?", "required": False, "example": 500},
    {"key": "close_rate", "question": "Out of 10 leads, how many become jobs? (0-1 is fine)", "required": False, "example": 0.5},
    {"key": "hours", "question": "What are your opening hours?", "required": False, "example": "Mon-Sat 7am-6pm"},
    {"key": "goals", "question": "What result do you want, in leads per month?", "required": False, "example": "10 leads/month"},
    {"key": "competitors", "question": "Who are your main competitors?", "required": False, "example": ["A1 Plumbing"]},
    {"key": "crm_today", "question": "How do you track leads today? (none/spreadsheet/app)", "required": False, "example": "spreadsheet"},
]

_GOAL_RE = re.compile(r"(\d+)\s*lead", re.I)


def _num(v, default=None):
    """Coerce Google-Ads-style numeric strings to float; blank/garbage -> default."""
    if v is None or v == "":
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f  # NaN guard


def _g(row, *path, default=None):
    """Nested get for camelCase REST rows: _g(row, 'metrics', 'costMicros')."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, dict)):
        return len(v) == 0
    return False


def _first(v) -> str:
    """First meaningful value of a list/CSV field, as a plain string."""
    if isinstance(v, (list, tuple)):
        for x in v:
            if not _blank(x):
                return str(x).strip()
        return ""
    if _blank(v):
        return ""
    return str(v).split(",")[0].strip()


def _parse_date(s) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        raise ToolError(scrub("date must be YYYY-MM-DD, got %r" % (s,)))


def _money(v) -> str:
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return "$0"


def _goal_leads(goals):
    if _blank(goals):
        return None
    m = _GOAL_RE.search(str(goals)) or re.search(r"(\d+)", str(goals))
    return int(m.group(1)) if m else None


def validate_profile(p) -> dict:
    """Check an owner profile. Returns ok/missing/warnings (+errors) - never raises."""
    prof = dict(p or {})
    missing = [f["key"] for f in PROFILE_FIELDS if f["required"] and _blank(prof.get(f["key"]))]
    warnings, errors = [], []
    budget = _num(prof.get("monthly_budget"))
    if budget is not None and budget < 0:
        errors.append("monthly_budget cannot be negative")
    rating = _num(prof.get("rating"))
    if rating is not None and not 1 <= rating <= 5:
        errors.append("rating must be between 1 and 5")
    cr = _num(prof.get("close_rate"))
    if cr is not None and not 0 <= cr <= 1:
        errors.append("close_rate must be between 0 and 1")
    if _blank(prof.get("website")):
        warnings.append("no website: the plan starts with a one-page site")
    if not _yes(prof.get("gbp_verified")):
        warnings.append("Google Business Profile not verified yet: it is the first task")
    if (_num(prof.get("review_count"), 0) or 0) < 10:
        warnings.append("fewer than 10 reviews: reviews are a top task, not an afterthought")
    if _blank(prof.get("goals")):
        warnings.append("no goal set: defaulting to 10 leads/month")
    if (budget or 0) <= 0:
        warnings.append("no ad budget: the plan will be organic-only")
    if str(prof.get("crm_today") or "none").strip().lower() in ("none", ""):
        warnings.append("no lead book today: old enquiries are being lost")
    return {"ok": not missing and not errors, "missing": missing,
            "warnings": warnings, "errors": errors}


def economics(p) -> dict:
    """Max cost per lead, goal and budget sanity. max_cpl = job * close_rate * 0.3."""
    prof = dict(p or {})
    ajv, cr, budget = _num(prof.get("avg_job_value")), _num(prof.get("close_rate")), _num(prof.get("monthly_budget"))
    max_cpl = round(ajv * cr * 0.3, 2) if (ajv and cr and ajv > 0 and cr > 0) else None
    goal = _goal_leads(prof.get("goals"))
    supports = int(budget // max_cpl) if (max_cpl and budget and budget > 0) else None
    notes = []
    if max_cpl is None:
        notes.append("Need avg_job_value and close_rate to set a max cost per lead.")
    if budget is not None and budget <= 0:
        notes.append("No ad budget: organic leads only until one channel proves itself.")
    if goal is not None and supports is not None and supports < goal:
        notes.append("Budget supports %d leads at max CPL but the goal is %d: narrow the area or lift close_rate." % (supports, goal))
    if max_cpl:
        notes.append("Never pay more than %.2f per lead; above that the ad loses money." % max_cpl)
    return {"max_cpl": max_cpl, "leads_needed_for_goal": goal,
            "budget_supports_leads": supports,
            "note": " ".join(notes) or "Fill in avg_job_value, close_rate and monthly_budget for exact numbers."}


def _by_id(tasks, tid):
    for t in tasks:
        if t["id"] == tid:
            return t
    return None


def _brief(t) -> dict:
    return {"id": t.get("id"), "title": t.get("title"), "week": t.get("week"),
            "priority": t.get("priority"), "tool": t.get("tool"),
            "depends_on": list(t.get("depends_on") or [])}


def build(p, start_date="2025-01-06", horizon_days=90) -> dict:
    """Create the full plan. Tasks are ordered; a task never comes before its dependency."""
    prof = dict(p or {})
    start = _parse_date(start_date)
    hd = int(_num(horizon_days, 0) or 0)
    if hd < 7:
        raise ToolError("horizon_days must be at least 7")
    if hd > 365:
        raise RailViolation("refusing a plan longer than 365 days: it would be stale on arrival")
    max_week = max(1, min(13, (hd + 6) // 7))

    name = _first(prof.get("business_name")) or "your business"
    city = _first(prof.get("city")) or "your area"
    area = _first(prof.get("service_area")) or city
    svc = _first(prof.get("services")) or "your main service"
    has_site = not _blank(prof.get("website"))
    gbp_ok = _yes(prof.get("has_gbp")) and _yes(prof.get("gbp_verified"))
    review_count = int(_num(prof.get("review_count"), 0) or 0)
    budget = _num(prof.get("monthly_budget"), 0.0) or 0.0
    running_ads = budget > 0
    lsa_ok = _yes(prof.get("lsa_eligible_category")) and _yes(prof.get("licensed_insured"))
    has_lsa = _yes(prof.get("has_lsa")) or lsa_ok

    tasks, skipped, ids = [], [], {}

    def _week(w):
        return max(1, min(max_week, int(w)))

    def add(title, tool, why, how, done_when, priority="P1", week=1, deps=(),
            owner_minutes=0, est_cost="$0", stage=1):
        assert tool in _ALLOWED, tool
        w = _week(week)
        dep_ids = []
        for d in deps:  # dependencies always exist and were created earlier
            dep = _by_id(tasks, d)
            if dep is not None:
                w = max(w, dep["week"])
                dep_ids.append(dep["id"])
        tid = "T%02d" % (len(tasks) + 1)
        tasks.append({"id": tid, "week": w, "title": title, "why": why, "how": how,
                      "tool": tool, "done_when": done_when, "priority": priority,
                      "depends_on": dep_ids, "owner_minutes": int(owner_minutes),
                      "est_cost": est_cost, "stage": stage})
        return tid

    # ---- Stage 1: Foundations (week 1-2)
    if not gbp_ok:
        ids["gbp"] = add(
            "Claim and verify your Google Business Profile", "owner",
            "An unverified profile is nearly invisible in Maps and cannot collect reviews.",
            "Go to business.google.com, sign in with the Google account you will keep forever, "
            "pick the business and finish the verification Google offers.",
            "Google shows Verified on the profile and you can publish an update.",
            "P0", 1, (), 20, "$0", 1)
    ids["nap"] = add(
        "Make name, address and phone identical on every listing", "adsops_local_nap",
        "Google and AI assistants cross-check listings; mismatches split your ranking signal.",
        "Run the tool and fix every listing it flags until the report is clean.",
        "adsops_local_nap reports zero mismatches on the main directories.",
        "P0", 1, (), 0, "$0", 1)
    if not has_site:
        ids["site"] = add(
            "Put up a one-page website", "agent",
            "With no website, ads, reviews and AI answers have nowhere to send people.",
            "The agent writes one page (service, city, phone, hours, area, a big call button); "
            "you publish it on any free host and send the URL back.",
            "A public URL loads and shows the phone number in large type.",
            "P0", 1, (), 15, "$0-15/mo hosting", 1)
    ids["schema"] = add(
        "Add LocalBusiness schema to the site", "adsops_local_schema",
        "Schema is the machine-readable version of the business that AI assistants quote.",
        "The agent generates the JSON-LD from your profile; you paste it into the site head.",
        "adsops_local_schema finds LocalBusiness markup matching name, phone and city.",
        "P1", 2 if not has_site else 1, ((ids["site"],) if not has_site else ()), 0, "$0", 1)
    ids["leads"] = add(
        "Set up the lead book and import old enquiries", "adsops_leads",
        "Leads you never wrote down are leads you never followed up.",
        "Run adsops_leads to create the book, then paste in every enquiry from the last 12 months "
        "(texts, voicemails, missed calls).",
        "adsops_leads lists at least 10 historical leads with dates.",
        "P0", 1, (), 10, "$0", 1)
    if running_ads:
        ids["conv"] = add(
            "Fix conversion tracking before spending a dollar", "adsops_conv_audit",
            "Without conversion tracking the ads optimise toward clicks, not jobs.",
            "Run the audit, then apply the fixes it lists.",
            "adsops_conv_audit reports a firing conversion for calls and form fills.",
            "P0", 2, (), 0, "$0", 1)
    ids["crawl"] = add(
        "Let AI crawlers read the site", "aeo_site_audit",
        "If GPTBot, PerplexityBot or Google-Extended are blocked you can never be cited.",
        "Run the crawl audit and remove the robots.txt block or login wall it reports.",
        "aeo_site_audit reports the AI bots as allowed on the homepage and service pages.",
        "P1", 2, ((ids["site"],) if not has_site else ()), 0, "$0", 1)
    ids["aeo0"] = add(
        "Measure the AI-answer baseline", "aeo_visibility_record",
        "You cannot prove progress without a day-zero number.",
        "Run the tool for the top 3 services in the service area and save the report.",
        "aeo_visibility_record returns a stored baseline with a date and score.",
        "P1", 2, (), 0, "$0", 1)

    # ---- Stage 2: Get leads now (week 2-4)
    if lsa_ok:
        ids["lsa"] = add(
            "Sign up for Local Services Ads and get verified", "adsops_lsa_readiness",
            "LSA is the cheapest pay-per-lead source for most trades and shows above the ads.",
            "Run the readiness check, then sign up at ads.google.com/localservices with licence and "
            "insurance documents. You will do a background check and a short phone call.",
            "The LSA profile is live and shows the Google Guaranteed or Screened badge.",
            "P0", 2, ((ids["gbp"],) if "gbp" in ids else ()), 45, "$0 to join, pay per lead", 2)
    else:
        skipped.append({"task": "Local Services Ads",
                        "reason": "category not LSA-eligible, or licence/insurance not confirmed"})
    if budget >= 300:
        ids["search"] = add(
            "Turn on a Search campaign with hard caps", "adsops_plan",
            "Search catches people who are ready to buy right now.",
            "Run adsops_plan with the caps from the economics section (%s/month maximum), review it, "
            "then adsops_apply. Do not raise the budget for 30 days." % _money(budget),
            "adsops_status shows a live campaign spending at or under the daily cap.",
            "P0", 3, ((ids["conv"],) if "conv" in ids else ()), 0, "%s/mo" % _money(budget), 2)
    else:
        skipped.append({"task": "Search ads", "reason": "monthly_budget is below $300"})
    target_reviews = 25 if review_count >= 10 else 10
    ids["reviews"] = add(
        "Ask every past customer for a review (target %d)" % target_reviews, "adsops_reviews",
        "Reviews are the strongest Maps ranking factor and the proof AI assistants quote.",
        "Create the review link, text it today to every customer from the last two years, then to "
        "every customer after each job.",
        "The profile shows at least %d Google reviews." % target_reviews,
        "P0" if review_count < 10 else "P1", 2, (), 5, "$0", 2)
    ids["speed"] = add(
        "Answer every lead within 5 minutes", "adsops_leads_due",
        "Response time beats price: the first business to reply usually wins the job.",
        "Check adsops_leads_due every morning and after every job; call back, do not text, then mark "
        "the lead answered in the book.",
        "adsops_leads_due shows zero leads older than 5 minutes unanswered for 7 days.",
        "P0", 2, (ids["leads"],), 0, "$0", 2)

    # ---- Stage 3: Get found by AI (week 3-8)
    ids["area"] = add(
        "Write a page for the top service in each of the top 3 areas", "adsops_local_area_page",
        "Service+area pages are what Maps, organic results and AI assistants quote for 'X in Y'.",
        "The agent starts with %s in %s and writes one page per service/area pair; you read each "
        "aloud and correct anything that sounds wrong." % (svc, area),
        "Three published URLs, each naming a service and a city in the title.",
        "P1", 3, (ids["schema"],), 10, "$0", 3)
    ids["faq"] = add(
        "Answer the questions people ask AI assistants", "agent",
        "Assistants quote FAQ text almost verbatim; unanswered questions go to a competitor.",
        "The agent writes 8 question/answer pairs in the words customers use; you confirm prices, "
        "hours and coverage before publishing.",
        "The FAQ is published on the site and linked from the homepage.",
        "P1", 3, (ids["schema"],), 10, "$0", 3)
    ids["llms"] = add(
        "Publish an llms.txt file", "aeo_llms_txt",
        "llms.txt tells AI crawlers what the site contains and which pages matter.",
        "Run the tool to generate the file, then upload it to the site root.",
        "https://your-domain/llms.txt returns 200 with the service list.",
        "P2", 4, (ids["schema"],), 0, "$0", 3)
    ids["cites"] = add(
        "Get listed on Bing Places, Apple Business Connect, Yelp, BBB and Nextdoor", "adsops_local_nap",
        "Every consistent listing is another source assistants trust when naming a business.",
        "Claim each listing, paste the exact name/address/phone from the profile, re-run the tool.",
        "adsops_local_nap reports the five extra citations as matching.",
        "P1", 4, (ids["nap"],), 20, "$0", 3)
    ids["indexnow"] = add(
        "Ping search engines after every publish", "aeo_indexnow",
        "New pages get found in days instead of months.",
        "Run the tool after each new page or edit; the agent does it as the last publish step.",
        "aeo_indexnow returns a 200/202 for the submitted URLs.",
        "P2", 4, (ids["area"],), 0, "$0", 3)
    ids["pgaudit"] = add(
        "Audit the pages AI assistants would read", "aeo_page_audit",
        "Thin or slow pages are skipped by Google and by assistants.",
        "Run the audit on the homepage and the three area pages, then fix the top three issues.",
        "aeo_page_audit scores every audited page above its pass mark.",
        "P2", 5, (ids["area"],), 0, "$0", 3)
    ids["schemaaudit"] = add(
        "Check the schema actually parses", "aeo_schema_generate",
        "Broken JSON-LD is worse than none: assistants drop the whole entity.",
        "Run the tool, paste any corrected block it prints, then run it again.",
        "aeo_schema_generate reports zero errors on the homepage.",
        "P2", 5, (ids["schema"],), 0, "$0", 3)

    # ---- Stage 4: Optimise (week 6-13)
    if has_lsa:
        ids["dispute"] = add(
            "Dispute bad LSA leads every week", "adsops_lsa_dispute",
            "Spam and wrong-area leads are refundable, but only inside the dispute window.",
            "Run the weekly report, open every wrong number, spam or out-of-area lead and file the "
            "dispute the same day.",
            "Every disputed lead shows a status of Submitted in adsops_lsa_report.",
            "P0", 6, ((ids["lsa"],) if "lsa" in ids else ()), 10, "$0", 4)
        add("Compare LSA cost per lead with the max CPL", "adsops_lsa_report",
            "You need to know whether a lead is worth what you paid before scaling.",
            "Run the report weekly; if cost per lead is above max CPL, pause the worst service.",
            "adsops_lsa_report shows cost per lead at or under max CPL for 2 weeks running.",
            "P1", 6, (ids["dispute"],), 5, "$0", 4)
    else:
        skipped.append({"task": "LSA reporting", "reason": "no Local Services Ads account"})
    if budget >= 300:
        add("Add negative keywords weekly", "adsops_negatives_suggest",
            "Search terms that are not your service eat the budget silently.",
            "Run the suggestion tool, approve the obvious junk (jobs, free, DIY, other cities) and apply.",
            "adsops_status shows under 10% of spend on non-service search terms.",
            "P1", 6, ((ids["search"],) if "search" in ids else ()), 0, "$0", 4)
    if running_ads:
        add("Fix the landing page the ads point at", "adsops_lp_diagnose",
            "A page that does not answer 'can you come today, how much' wastes every click.",
            "Run the diagnosis and make the top three fixes (phone above the fold, price range, area).",
            "adsops_lp_diagnose reports the top three issues resolved.",
            "P1", 7, (), 15, "$0", 4)
        add("Upload won jobs as offline conversions", "adsops_offline_conv_plan",
            "Without the closed-won signal the ads optimise toward cheap junk leads.",
            "Export last month's won jobs from the lead book and upload them weekly.",
            "adsops_offline_conversions confirms the upload and the conversion action counts it.",
            "P1", 8, (ids["leads"],), 10, "$0", 4)
    add("Re-measure AI visibility", "aeo_visibility_record",
        "This is the only honest check that the AEO work is working.",
        "Run the same queries as the baseline on the same day each month and compare.",
        "aeo_visibility_record shows a score above the stored baseline.",
        "P1", 8, (ids["aeo0"],), 0, "$0", 4)
    add("Move the budget toward the cheapest won job", "agent",
        "Every channel has a different cost per won job; the money should follow the winner.",
        "Add up jobs won per channel from the lead book, compare with cost per lead from adsops_status "
        "and adsops_lsa_report, then shift at most 30% of the budget.",
        "A written note states cost per won job per channel and the new split.",
        "P2", 12, (ids["leads"],), 15, "$0", 4)

    rhythm = [{"cadence": "daily", "title": "Call back everyone waiting on a quote",
               "tool": "adsops_leads_due", "why": "speed to lead wins the job"}]
    if has_lsa:
        rhythm.append({"cadence": "daily", "title": "Pull new LSA leads into the book",
                       "tool": "adsops_lsa_leads", "why": "unpulled leads go stale in hours"})
        rhythm.append({"cadence": "weekly", "title": "LSA report: cost per lead, missed calls, best hours",
                       "tool": "adsops_lsa_report", "why": "know what a lead costs against what you can afford"})
        rhythm.append({"cadence": "weekly", "title": "Rate LSA leads: dispute junk, confirm booked jobs",
                       "tool": "adsops_lsa_dispute", "why": "feedback only counts inside the window, and it trains Google"})
    rhythm.append({"cadence": "weekly", "title": "Ask every finished customer for a review",
                   "tool": "adsops_reviews", "why": "reviews compound like interest"})
    if running_ads:
        rhythm.append({"cadence": "weekly", "title": "Check spend and cost per lead",
                       "tool": "adsops_status", "why": "catch a runaway campaign early"})
    rhythm.append({"cadence": "monthly", "title": "Re-measure AI visibility",
                   "tool": "aeo_visibility_record", "why": "proves the AEO work moved the needle"})

    kpis = [
        {"name": "Leads answered within 5 minutes", "target": "100%", "tool": "adsops_leads_due"},
        {"name": "Google reviews", "target": str(target_reviews), "tool": "adsops_reviews"},
        {"name": "Leads per month", "target": str(_goal_leads(prof.get("goals")) or 10), "tool": "adsops_leads"},
    ]
    econ = economics(prof)
    if econ["max_cpl"]:
        kpis.append({"name": "Cost per lead", "target": "under %.2f" % econ["max_cpl"], "tool": "adsops_status"})
    if has_lsa:
        kpis.append({"name": "LSA disputes filed", "target": "every week", "tool": "adsops_lsa_dispute"})
    kpis.append({"name": "AI answers mentioning the business", "target": "above baseline", "tool": "aeo_visibility_record"})

    if budget <= 0:
        summary = ("Organic-only 90-day plan for %s in %s: profile, reviews, area pages and AI "
                   "visibility. No ad budget, so there are no ads tasks." % (name, city))
    else:
        summary = ("90-day plan for %s in %s: fix the profile and tracking, get leads from Google and "
                   "AI assistants, then move budget to the cheapest won job." % (name, city))
    return {
        "summary": summary, "start_date": start.isoformat(), "horizon_days": hd,
        "stages": [
            {"name": "Foundations", "days": "week 1-2",
             "why": "A verified profile, clean listings and tracking are the floor everything stands on."},
            {"name": "Get leads now", "days": "week 2-4",
             "why": "LSA, Search and reviews produce calls while the slower work compounds."},
            {"name": "Get found by AI", "days": "week 3-8",
             "why": "Area pages, FAQ, llms.txt and citations are what assistants quote."},
            {"name": "Optimise", "days": "week 6-13",
             "why": "Dispute junk leads, fix the landing page, feed back won jobs, follow the money."},
        ],
        "tasks": tasks, "weekly_rhythm": rhythm, "economics": econ, "kpis": kpis,
        "skipped": skipped,
    }


def progress(plan, done_ids, today) -> dict:
    """Completion, overdue and the next 5 actionable tasks (deps satisfied)."""
    plan = plan or {}
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    done = {str(x) for x in (done_ids or [])}
    today_d = _parse_date(today)
    start = _parse_date(plan.get("start_date")) if plan.get("start_date") else today_d
    total = len(tasks)
    done_count = sum(1 for t in tasks if t.get("id") in done)
    pct = round(100.0 * done_count / total, 1) if total else 0.0
    overdue, ready = [], []
    for t in tasks:
        if t.get("id") in done:
            continue
        end = start + timedelta(days=7 * int(t.get("week", 1) or 1) - 1)
        if end < today_d:
            overdue.append(t)
        if all(d in done for d in (t.get("depends_on") or [])):
            ready.append(t)
    overdue.sort(key=lambda t: (int(t.get("week", 1) or 1), str(t.get("id"))))
    ready.sort(key=lambda t: (int(t.get("week", 1) or 1), str(t.get("priority", "P9")), str(t.get("id"))))
    return {"done": done_count, "total": total, "pct": pct,
            "overdue": [_brief(t) for t in overdue], "next": [_brief(t) for t in ready[:5]]}


def to_markdown(plan) -> str:
    """Owner-readable checklist; every task id appears exactly once."""
    plan = plan or {}
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    stages = plan.get("stages") or []
    lines = ["# Growth plan", "", str(plan.get("summary") or ""), ""]
    for i, st in enumerate(stages, start=1):
        group = [t for t in tasks if int(t.get("stage", 1) or 1) == i]
        if not group:
            continue
        lines.append("## %s (%s)" % (st.get("name", "Stage"), st.get("days", "")))
        if st.get("why"):
            lines.append("_%s_" % st["why"])
        for t in group:
            lines.append("- [ ] %s (week %s) %s — %s" % (t.get("id"), t.get("week"), t.get("title"), t.get("tool")))
        lines.append("")
    known = {t.get("id") for st in stages for t in tasks if int(t.get("stage", 1) or 1) <= len(stages)}
    rest = [t for t in tasks if t.get("id") not in known]
    if rest:
        lines.append("## Other")
        lines += ["- [ ] %s (week %s) %s — %s" % (t.get("id"), t.get("week"), t.get("title"), t.get("tool")) for t in rest]
        lines.append("")
    if plan.get("weekly_rhythm"):
        lines.append("## Weekly rhythm")
        lines += ["- %s: %s (%s)" % (r.get("cadence"), r.get("title"), r.get("tool")) for r in plan["weekly_rhythm"]]
        lines.append("")
    if plan.get("kpis"):
        lines.append("## KPIs")
        lines += ["- %s: target %s via %s" % (k.get("name"), k.get("target"), k.get("tool")) for k in plan["kpis"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _demo_profile(**over) -> dict:
    p = {"business_name": "Ridge Plumbing", "trade": "plumber", "city": "Boulder",
         "service_area": ["Boulder", "Louisville"], "services": ["drain cleaning", "water heaters", "leak repair"],
         "phone": "+13035550100", "website": "https://ridgeplumbing.example",
         "has_gbp": True, "gbp_verified": True, "review_count": 4, "rating": 4.7,
         "has_lsa": False, "lsa_eligible_category": True, "licensed_insured": True,
         "monthly_budget": 1500, "avg_job_value": 500, "close_rate": 0.5,
         "hours": "Mon-Sat 7-6", "goals": "10 leads/month", "competitors": ["A1 Plumbing"],
         "crm_today": "spreadsheet"}
    p.update(over)
    return p


def selftest() -> int:
    """All I/O mocked or absent. Returns the number of checks."""
    n = 0

    def check(cond, msg):
        nonlocal n
        assert cond, msg
        n += 1

    keys = [f["key"] for f in PROFILE_FIELDS]
    check(len(keys) == len(set(keys)) and len(keys) >= 20, "profile fields unique")
    check(all(k in keys for k in ("business_name", "trade", "city", "phone", "monthly_budget")), "required fields defined")
    check(all({"key", "question", "required", "example"} <= set(f) for f in PROFILE_FIELDS), "field shape")

    check(validate_profile(_demo_profile())["ok"] is True, "good profile ok")
    miss = validate_profile({})
    check(miss["ok"] is False and "business_name" in miss["missing"] and "phone" in miss["missing"], "empty missing")
    check("monthly_budget" in validate_profile(_demo_profile(monthly_budget=-5))["errors"][0], "negative budget error")
    bad = validate_profile(_demo_profile(rating=9, close_rate=1.5))
    check(len(bad["errors"]) == 2 and bad["ok"] is False, "rating/close_rate errors")
    w = validate_profile(_demo_profile(website="", review_count=1, gbp_verified=False))
    check(len(w["warnings"]) >= 2 and w["ok"] is True, "warnings not fatal")

    e = economics(_demo_profile())
    check(e["max_cpl"] == 75.0, "max_cpl math")
    check(e["budget_supports_leads"] == 20, "budget supports leads")
    check(e["leads_needed_for_goal"] == 10, "goal parse")
    e2 = economics({"business_name": "x"})
    check(e2["max_cpl"] is None and e2["budget_supports_leads"] is None and "Need avg_job_value" in e2["note"], "economics None")
    check("No ad budget" in economics(_demo_profile(monthly_budget=0))["note"], "zero budget note")

    plan = build(_demo_profile(), start_date="2025-01-06")
    tids = [t["id"] for t in plan["tasks"]]
    check(len(tids) > 5 and len(tids) == len(set(tids)) and tids[0] == "T01", "unique ids")
    check(all(t["tool"] in _ALLOWED for t in plan["tasks"]), "tools valid")
    check(all({"id", "week", "title", "why", "how", "tool", "done_when", "priority",
               "depends_on", "owner_minutes", "est_cost"} <= set(t) for t in plan["tasks"]), "task keys")
    ok_deps = True
    for t in plan["tasks"]:
        for d in t["depends_on"]:
            dep = _by_id(plan["tasks"], d)
            ok_deps = ok_deps and dep is not None and dep["week"] <= t["week"] and tids.index(d) < tids.index(t["id"])
    check(ok_deps, "dependency ordering")
    check(all(1 <= t["week"] <= 13 for t in plan["tasks"]), "weeks in range")

    organic = build(_demo_profile(monthly_budget=0))
    otools = [t["tool"] for t in organic["tasks"]]
    check("adsops_plan" not in otools and "adsops_conv_audit" not in otools and "organic" in organic["summary"].lower(),
          "organic only")
    check(any("budget" in s["reason"] for s in organic["skipped"]), "skip reason recorded")
    no_lsa = build(_demo_profile(lsa_eligible_category=False))
    check("adsops_lsa_readiness" not in [t["tool"] for t in no_lsa["tasks"]], "LSA skipped")
    check(any(s["task"] == "Local Services Ads" for s in no_lsa["skipped"]), "LSA skip reason")
    check("adsops_lsa_readiness" in [t["tool"] for t in plan["tasks"]], "LSA included when eligible")
    check(not any(t["title"].startswith("Claim and verify") for t in plan["tasks"]), "skip verified GBP claim")
    unver = [t for t in build(_demo_profile(gbp_verified=False))["tasks"] if t["title"].startswith("Claim and verify")]
    check(len(unver) == 1 and unver[0]["tool"] == "owner" and unver[0]["owner_minutes"] >= 15, "GBP claim owner task")
    rv = [t for t in build(_demo_profile(review_count=12))["tasks"] if t["tool"] == "adsops_reviews"][0]
    check("25" in rv["title"], "lighter target when reviews exist")

    try:
        build(_demo_profile(), horizon_days=3)
        check(False, "short horizon should raise")
    except ToolError:
        check(True, "short horizon raises ToolError")
    try:
        build(_demo_profile(), start_date="not-a-date")
        check(False, "bad date should raise")
    except ToolError:
        check(True, "bad date raises ToolError")
    try:
        build(_demo_profile(), horizon_days=400)
        check(False, "long horizon should refuse")
    except RailViolation:
        check(True, "long horizon refused")

    pr = progress(plan, [], "2025-01-05")
    check(pr["pct"] == 0.0 and pr["done"] == 0 and 1 <= len(pr["next"]) <= 5, "progress empty")
    check(all(not t["depends_on"] for t in pr["next"]), "next has deps satisfied")
    first = pr["next"][0]["id"]
    pr2 = progress(plan, [first], "2025-01-05")
    check(all(set(t["depends_on"]) <= {first} for t in pr2["next"]), "next after one done")
    check(len(progress(plan, [], "2025-06-01")["overdue"]) > 0, "overdue detected")
    allids = [t["id"] for t in plan["tasks"]]
    check(progress(plan, allids, "2025-06-01")["pct"] == 100.0 and progress(plan, allids, "2025-06-01")["next"] == [], "all done")

    md = to_markdown(plan)
    check(all(i in md for i in allids), "markdown has every id")
    check("## Foundations" in md and "## Optimise" in md, "markdown stage headings")
    check(_g({"a": {"b": {"c": "7"}}}, "a", "b", "c") == "7" and _g({"a": {}}, "a", "b", default=0) == 0, "nested get")
    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
