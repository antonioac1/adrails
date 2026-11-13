"""AdRails coach - Module A: onboarding, checklist, cadence.

Stdlib only. Pure functions. No network, disk writes, env reads, randomness,
or current-date calls (dates are passed in via `today`).

Tools: get_started(profile), setup_checklist(profile, done),
       weekly_cadence(profile, done, today)
Helpers: is_ymyl(industry), normalise_profile(profile)

The business profile lives in a `profile` dict the model keeps and passes back
(the "profile card"). Stateless: nothing is stored here.

Run `python3 coach_onboard.py --selftest` for inline asserts.
"""

from __future__ import annotations

import datetime
import json
import re

STR_CAP = 300
LIST_CAP = 30

# YMYL (regulated / "your money or your life") matchers. Word-ish boundaries stop
# silly hits like "lawn care" matching "law".
YMYL_PATTERNS = (
    r"\binsur", r"\bfinanc", r"\bfinance\b", r"\bmortgage", r"\blegal\b",
    r"\blaw\b", r"\blawy", r"\battorney", r"\bmedical", r"\bmedicin",
    r"\bdental", r"\bdentist", r"\bhealth", r"\btax", r"\baccount",
    r"\bcpa\b", r"\binvest", r"\bwealth\b", r"\breal estate\b", r"\brealt",
    r"\bbroker",
)

YMYL_COMPLIANCE = (
    "This is a YMYL topic (money, health, safety or legal). Use real licensed bylines: "
    "every page carries the author's name and license number, carriers are named only "
    "if the business is truly appointed with them, no guarantees and no 'best rates' or "
    "'cheapest' claims, add the state regulator disclaimer, and have a licensed person "
    "review the page before it is published."
)

COMPLIANCE_SHORT = (
    "YMYL topic: real bylines with license numbers, no guarantees or 'best rates' claims, "
    "state regulator disclaimer, licensed human review before publishing."
)

PROFILE_KEYS = (
    "business_name", "website", "industry", "services", "service_area", "state",
    "customers", "ideal_lead", "competitors", "differentiators", "phone", "gbp_url",
    "has_gbp", "reviews_count", "owner_hours_per_week", "publishing", "goals",
    "licenses", "done",
)

LIST_KEYS = ("services", "service_area", "competitors", "differentiators", "done")


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------

def _clean_str(value) -> str:
    """Trim, collapse whitespace, cap length. Anything non-text becomes ''."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)[:STR_CAP]
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:STR_CAP]


def _clean_list(value) -> list:
    """Clean, de-duplicate, cap count (30) and item length (300)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out, seen = [], set()
    for item in value:
        text = _clean_str(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= LIST_CAP:
            break
    return out


def _clean_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return None
    return None


def _clean_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "y", "1", "claimed", "verified"):
            return True
        if low in ("false", "no", "n", "0", "none"):
            return False
    return None


def is_ymyl(industry) -> bool:
    """True when the industry keyword-matches a regulated (YMYL) category."""
    text = _clean_str(industry).lower()
    if not text:
        return False
    flat = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if not flat:
        return False
    for pattern in YMYL_PATTERNS:
        if re.search(pattern, flat):
            return True
    return False


def normalise_profile(profile) -> dict:
    """Return a clean copy of the profile card.

    Unknown keys are dropped, every key is optional, strings are trimmed and
    capped at 300 chars, lists are capped at 30 items. Nothing is invented.
    """
    src = profile if isinstance(profile, dict) else {}
    out = {}
    for key in PROFILE_KEYS:
        if key not in src:
            out[key] = [] if key in LIST_KEYS else None
            continue
        raw = src.get(key)
        if key in LIST_KEYS:
            out[key] = _clean_list(raw)
        elif key == "has_gbp":
            out[key] = _clean_bool(raw)
        elif key in ("reviews_count", "owner_hours_per_week"):
            out[key] = _clean_int(raw)
        else:
            text = _clean_str(raw)
            out[key] = text if text else None
    return out


def _has(prof: dict, key: str) -> bool:
    val = prof.get(key)
    if key in LIST_KEYS:
        return bool(val)
    if key == "has_gbp":
        return val is not None
    return bool(val)


def _next_step(say: str, call=None, args_hint=None) -> dict:
    return {
        "say": say,
        "call": call,
        "args_hint": args_hint if isinstance(args_hint, dict) else {},
    }


# ---------------------------------------------------------------------------
# get_started
# ---------------------------------------------------------------------------

QUESTIONS = {
    "business_name": {
        "ask": "What is the business called?",
        "why": "This exact name has to match everywhere online (Google, maps, directories).",
        "example": "Sunshine Coast Insurance",
    },
    "website": {
        "ask": "What is the website address?",
        "why": "So I can read the site myself instead of asking you about it.",
        "example": "https://sunshinecoastins.com",
    },
    "industry": {
        "ask": "In one line, what does the business do?",
        "why": "The industry decides the checklist, the compliance rules and the page ideas.",
        "example": "independent insurance agency",
    },
    "service_area": {
        "ask": "Which cities or counties do you serve?",
        "why": "Local pages only win when they name places you really serve.",
        "example": "Tampa, St Petersburg, Clearwater",
    },
    "state": {
        "ask": "Which state are you based or licensed in?",
        "why": "State rules and the regulator disclaimer depend on it.",
        "example": "Florida",
    },
    "services": {
        "ask": "List the services or products people actually buy from you.",
        "why": "Each real service becomes one strong page instead of a vague homepage.",
        "example": "home insurance, flood insurance, auto insurance",
    },
    "ideal_lead": {
        "ask": "Which single job or customer is worth the most to you?",
        "why": "The first page gets built for your most valuable customer.",
        "example": "homeowners who want home and flood cover together",
    },
    "customers": {
        "ask": "Who buys from you, in your own words?",
        "why": "Pages must use the customer's words, not industry jargon.",
        "example": "families near the coast who just got a mortgage",
    },
    "competitors": {
        "ask": "Name two or three competitors, or paste their websites.",
        "why": "Shows what buyers compare you against so we do not copy them.",
        "example": "the captive agency branch on Main Street, one big online insurer",
    },
    "differentiators": {
        "ask": "Why do customers pick you over the other guys?",
        "why": "True differences are the trust material for your pages.",
        "example": "we answer the phone, 20 years local, help with claims",
    },
    "licenses": {
        "ask": "What license numbers and credentials should appear on the site?",
        "why": "Licensed bylines are required for regulated topics and build trust.",
        "example": "FL agent license A123456",
    },
    "has_gbp": {
        "ask": "Do you have a Google Business Profile (the map listing)?",
        "why": "It is the fastest free visibility win for a local business.",
        "example": "yes, claimed but not verified",
    },
    "gbp_url": {
        "ask": "If you have the Google listing, paste its link.",
        "why": "Saves me hunting for the right listing.",
        "example": "https://g.page/yourbusiness",
    },
    "reviews_count": {
        "ask": "About how many Google reviews do you have?",
        "why": "Sets the review goal and shows whether the review habit needs work.",
        "example": "37",
    },
    "publishing": {
        "ask": "How do you edit the website: WordPress, Wix, Squarespace, Webflow, Shopify, custom, or none?",
        "why": "The publish steps must match the tool you actually have.",
        "example": "wordpress",
    },
    "owner_hours_per_week": {
        "ask": "How many hours a week can you realistically spend on this?",
        "why": "The weekly plan is scaled to your real time so you actually finish it.",
        "example": "3",
    },
    "goals": {
        "ask": "What would a win look like in the next 90 days?",
        "why": "Keeps the plan pointed at leads, not vanity traffic.",
        "example": "ten more quote requests a month",
    },
}

ROUNDS = (
    ("basics", ("business_name", "website", "industry", "service_area", "state")),
    ("money", ("services", "ideal_lead", "customers")),
    ("edge", ("competitors", "differentiators", "licenses")),
    ("ops", ("has_gbp", "gbp_url", "reviews_count", "publishing",
             "owner_hours_per_week", "goals")),
)

READY_KEYS = ("business_name", "industry", "service_area", "services")

LOOK_UP = (
    "Open the website and read the services menu, locations and about page",
    "Search Google Maps for the business name to find the Google Business Profile and review count",
    "Check whether the site has robots.txt, a sitemap.xml and a llms.txt file",
    "Search the business name plus the main city to see which directories and competitors appear",
    "Read the latest Google reviews to learn the words customers actually use",
)

INSTRUCTIONS = (
    "Interview the owner a few questions at a time, never a wall of questions. Ask only "
    "what you cannot look up yourself, and save every answer into profile_card. Pass "
    "profile_card to every AdRails tool call and keep a copy of it in the conversation or "
    "project memory so it survives between sessions. Never invent facts about the business: "
    "if a fact is missing, mark it unknown and ask later. Explain each step in plain English."
)


def _questions_for_keys(prof: dict, keys) -> list:
    ymyl = is_ymyl(prof.get("industry"))
    out = []
    for key in keys:
        if key == "licenses" and not ymyl:
            continue
        if _has(prof, key):
            continue
        if key == "gbp_url" and prof.get("has_gbp") is False:
            continue
        meta = QUESTIONS[key]
        out.append({
            "key": key,
            "ask": meta["ask"],
            "why": meta["why"],
            "example": meta["example"],
        })
        if len(out) >= 4:
            break
    return out


def _missing_keys(prof: dict) -> list:
    ymyl = is_ymyl(prof.get("industry"))
    missing = []
    for key in PROFILE_KEYS:
        if key == "licenses" and not ymyl:
            continue
        if key == "gbp_url" and prof.get("has_gbp") is False:
            continue
        if not _has(prof, key):
            missing.append(key)
    return missing


def get_started(profile=None) -> dict:
    """Interview in rounds of <=4 questions, or report ready for the checklist."""
    prof = normalise_profile(profile)
    ymyl = is_ymyl(prof.get("industry"))
    ready = all(_has(prof, key) for key in READY_KEYS)

    if ready:
        # Core facts are enough to start, but keep interviewing: the next unanswered round rides along so the
        # assistant asks it between tasks (ideal lead, competitors, GBP status and publishing shape every later step).
        later, later_round = [], None
        for idx, (_name, keys) in enumerate(ROUNDS, start=1):
            qs = _questions_for_keys(prof, keys)
            if qs:
                later, later_round = qs, idx
                break
        return {
            "stage": "ready",
            "round": later_round,
            "questions": later,
            "missing": _missing_keys(prof),
            "profile_card": prof,
            "look_up_yourself": list(LOOK_UP),
            "instructions_for_assistant": INSTRUCTIONS,
            "compliance": COMPLIANCE_SHORT if ymyl else None,
            "next_step": _next_step(
                ("I have the core facts, so the next step is your setup checklist, in priority order."
                 + (" I will also ask the %d remaining question%s below as we go." % (len(later), "" if len(later) == 1 else "s") if later else "")),
                "setup_checklist",
                {"profile": prof},
            ),
        }

    questions, round_no, round_name = [], 1, ROUNDS[0][0]
    for idx, (name, keys) in enumerate(ROUNDS, start=1):
        qs = _questions_for_keys(prof, keys)
        if qs:
            questions, round_no, round_name = qs, idx, name
            break
    if not questions:  # nothing left to ask but not "ready" (e.g. blank profile)
        questions = _questions_for_keys(prof, ROUNDS[0][1])[:4]

    return {
        "stage": "interview",
        "round": round_no,
        "round_name": round_name,
        "questions": questions,
        "missing": _missing_keys(prof),
        "profile_card": prof,
        "look_up_yourself": list(LOOK_UP),
        "instructions_for_assistant": INSTRUCTIONS,
        "compliance": COMPLIANCE_SHORT if ymyl else None,
        "next_step": _next_step(
            "I will ask %d quick questions, a few at a time. Answer in one line each and "
            "I will save them into your profile card." % len(questions),
            "get_started",
            {"profile": prof},
        ),
    }


# ---------------------------------------------------------------------------
# setup_checklist
# ---------------------------------------------------------------------------

DIRECTORIES = {
    "insurance": [
        "Trusted Choice (independent agents)",
        "carrier agent-locator pages for appointed carriers",
        "Better Business Bureau (BBB)",
        "Yelp",
        "state agent association directory",
        "Insurance Journal agency directory",
    ],
    "legal": ["Avvo", "Justia", "FindLaw", "state bar directory"],
    "medical": ["Healthgrades", "Zocdoc", "WebMD"],
    "dental": ["Healthgrades", "Zocdoc", "WebMD"],
    "real estate": ["Zillow", "Realtor.com", "Homes.com"],
    "home services": ["Angi", "HomeAdvisor", "Thumbtack", "Houzz", "Nextdoor"],
    "generic": [
        "Yelp", "Better Business Bureau (BBB)", "Nextdoor", "Facebook page",
        "chamber of commerce",
    ],
}

DIRECTORY_MATCHERS = (
    ("insurance", ("insur", "agency", "agent")),
    ("legal", ("legal", "law", "attorney", "lawyer")),
    ("medical", ("medical", "medicine", "health", "clinic", "physician", "doctor")),
    ("dental", ("dental", "dentist", "orthodont")),
    ("real estate", ("real estate", "realtor", "realty")),
    ("home services", ("plumb", "hvac", "roof", "electric", "contractor",
                       "handyman", "landscap", "clean", "remodel", "pest",
                       "garage", "floor", "paint", "moving", "locksmith",
                       "tree", "fence", "pool")),
)


def industry_directories(industry) -> list:
    """Directory list for an industry (keyword matched), else the generic list."""
    text = _clean_str(industry).lower()
    if text:
        for key, needles in DIRECTORY_MATCHERS:
            for needle in needles:
                if needle in text:
                    return list(DIRECTORIES[key])
    return list(DIRECTORIES["generic"])


def _item(item_id, title, why, how, minutes, who, tool, priority):
    return {
        "id": item_id,
        "title": title,
        "why": why,
        "how": list(how),
        "minutes": minutes,
        "who": who,
        "tool": tool,
        "priority": priority,
    }


def _checklist_items(prof: dict) -> list:
    ymyl = is_ymyl(prof.get("industry"))
    dirs = industry_directories(prof.get("industry"))
    dir_how = [
        "Search each of these: " + "; ".join(dirs),
        "Verify a listing exists and matches NAP (name, address, phone)",
        "Claim or create the free listing where it is missing",
        "Fix every wrong name, address, phone or stale website link",
    ]

    items = [
        _item("gbp_claim", "Claim and verify the Google Business Profile",
              "The map listing is the biggest free visibility win for a local business.",
              ["Go to business.google.com and search the business name",
               "Claim the listing (create it only if it truly does not exist)",
               "Finish verification by postcard, phone or video",
               "Confirm the address, phone and service area are correct"],
              20, "both", None, 1),
        _item("gbp_complete", "Fill every part of the Google profile",
              "Complete profiles show up for far more searches than half-filled ones.",
              ["Choose the closest categories, primary category first",
               "Add each real service with a short description",
               "Set hours, service area and phone",
               "Upload 5-10 real photos of people, office and finished work",
               "Write a 1-2 sentence description in plain English",
               "Answer 2-3 likely buyer questions in the Q&A section"],
              45, "both", None, 1),
        _item("gbp_posts", "Post once a week on the Google Business Profile",
              "Fresh posts keep the listing active and give buyers a reason to click.",
              ["Pick one useful topic: a tip, a service, a local note",
               "Write 2-3 plain sentences, no sales shouting",
               "Add one real photo and a clear call to action",
               "Publish and note the date so next week is easier"],
              15, "both", None, 2),
        _item("reviews_engine", "Build a simple review engine",
              "Reviews are the cheapest trust signal and they lift map rankings.",
              ["Copy the short Google review link from the profile",
               "Send it to every happy customer within two days of finishing",
               "Reply to every review within a week, good or bad",
               "Track the review count monthly"],
              25, "owner", None, 1),
        _item("bing_places", "Claim the Bing Places listing",
              "Feeds Bing and Copilot results and usually imports from Google in minutes.",
              ["Go to bingplaces.com and sign in",
               "Import the details from the Google Business Profile",
               "Verify the listing",
               "Check the hours, phone and website link"],
              15, "both", None, 3),
        _item("apple_business_connect", "Claim the Apple Business Connect listing",
              "Powers Apple Maps and Siri, which many phone buyers use.",
              ["Go to businessconnect.apple.com",
               "Search the business name and claim the place card",
               "Add hours, photos and a short description",
               "Verify the business"],
              15, "both", None, 3),
        _item("search_console", "Set up Google Search Console and submit the sitemap",
              "Shows which searches you appear for and gets new pages found fast.",
              ["Add the site at search.google.com/search-console",
               "Verify by DNS record or by uploading the HTML file",
               "Submit the sitemap.xml",
               "Bookmark the Performance report"],
              20, "both", None, 2),
        _item("bing_webmaster", "Set up Bing Webmaster Tools and import from Search Console",
              "Enables IndexNow and feeds ChatGPT and Copilot search with your pages.",
              ["Go to bing.com/webmasters and sign in",
               "Choose import from Google Search Console",
               "Confirm the sitemap came across",
               "Save the IndexNow key it gives you"],
              10, "both", None, 2),
        _item("indexnow", "Turn on IndexNow so new pages get crawled in hours",
              "Tells Bing and partners about a new page immediately, not weeks later.",
              ["Get the IndexNow key from Bing Webmaster Tools",
               "Publish the key file at the site root, or use the plugin",
               "Submit one test URL and confirm it was received"],
              15, "both", None, 3),
        _item("ai_crawlers", "Decide which AI crawlers may read the site",
              "If AI assistants cannot read the site, they cannot recommend it.",
              ["Open robots.txt and check the current rules",
               "Allow OAI-SearchBot and ChatGPT-User so ChatGPT search can cite the site",
               "Allow PerplexityBot and ClaudeBot",
               "Make a deliberate decision about GPTBot (training) and Google-Extended",
               "Save the file and confirm it still loads"],
              15, "assistant", "audit_site", 1),
        _item("llms_txt", "Publish an llms.txt summary of the site",
              "A short plain-text map that helps AI assistants understand the business.",
              ["List the business name, services and service area",
               "Link the main service pages",
               "Keep it short, factual and free of jargon",
               "Publish at /llms.txt and check that it loads"],
              15, "assistant", "build_llms_txt", 2),
        _item("schema", "Add LocalBusiness and FAQ structured data",
              "Structured data tells search engines exactly what the business is and where.",
              ["Pick the closest schema.org type, for example InsuranceAgency",
               "Add name, address, phone, area served and hours",
               "Add 3-5 FAQ questions with short plain answers",
               "Test it with the Rich Results test"],
              30, "assistant", "generate_schema", 2),
        _item("nap_consistency", "Make name, address and phone identical everywhere",
              "Mismatched details confuse search engines and cost local rankings.",
              ["Write the name, address and phone exactly as they should appear",
               "Check the website footer and contact page",
               "Check the Google, Bing and Apple listings",
               "Fix every difference, including abbreviations and suite numbers"],
              30, "both", None, 1),
        _item("directories", "Fix the industry directories",
              "Buyers and search engines check directories to confirm the business is real.",
              dir_how,
              25, "assistant", None, 3),
        _item("service_pages", "Plan and build one service page at a time",
              "One strong owner-reviewed page a week beats bulk auto-generated pages.",
              ["Run the page planner to get the ranked page list",
               "Pick the single top page and collect the real facts from the owner",
               "Draft it and have the owner check every number and claim",
               "Publish it, then submit the URL"],
              60, "both", "plan_landing_pages", 1),
        _item("answer_pages", "Build pages that answer real buyer questions",
              "Question pages match how people search and how AI assistants answer.",
              ["Run the content gap check with the buyer questions",
               "Confirm the question is not already answered on the site",
               "Draft one page per week, answering in the first two sentences",
               "Have the owner review it, then publish and submit the URL"],
              60, "both", "find_content_gaps", 2),
        _item("ai_visibility_baseline", "Record where AI assistants currently recommend the business",
              "You cannot improve AI visibility without a monthly baseline to compare against.",
              ["Write down 5 real buyer questions",
               "Ask each one in ChatGPT, Perplexity and Gemini",
               "Note whether the business is named, and who was named instead",
               "Repeat on the same day each month and compare"],
              30, "owner", None, 2),
    ]

    if prof.get("has_gbp") is True:
        for it in items:
            if it["id"] == "gbp_claim":
                it["title"] = "Confirm the Google Business Profile is claimed and verified"
                it["why"] = ("The listing already exists; verification is what unlocks posts, "
                             "reviews and insights.")
    if ymyl:
        for it in items:
            if it["id"] == "schema":
                it["how"].append(
                    "Add license numbers and the state regulator disclaimer when the owner supplies them")
            if it["id"] == "service_pages":
                it["how"].append(
                    "Add a licensed byline and disclaimer before publishing")
    return items


def _done_ids(prof: dict, done) -> set:
    ids = set(_clean_list(prof.get("done")))
    if done is not None:
        ids.update(_clean_list(done))
    return {i.lower() for i in ids}


def _weighted_score(items: list, done_ids: set) -> int:
    total = sum(it["priority"] for it in items)
    if total <= 0:
        return 0
    earned = sum(it["priority"] for it in items if it["id"].lower() in done_ids)
    return int(round(100.0 * earned / total))


def _todo_sorted(items: list, done_ids: set) -> list:
    order = {it["id"]: idx for idx, it in enumerate(items)}
    todo = [it for it in items if it["id"].lower() not in done_ids]
    todo.sort(key=lambda it: (it["priority"], order[it["id"]]))
    return todo


def setup_checklist(profile, done=None) -> dict:
    """Ordered setup checklist (~16 items) with a priority-weighted score."""
    prof = normalise_profile(profile)
    done_ids = _done_ids(prof, done)
    items = _checklist_items(prof)

    rows = []
    for it in items:
        row = dict(it)
        row["how"] = list(it["how"])
        row["status"] = "done" if it["id"].lower() in done_ids else "todo"
        rows.append(row)

    todo = _todo_sorted(rows, done_ids)
    next_three = [it["id"] for it in todo[:3]]
    ymyl = is_ymyl(prof.get("industry"))

    if todo:
        first = todo[0]
        if first.get("tool"):
            step = _next_step(
                "Next up is %s, about %d minutes; I will open the AdRails tool for it."
                % (first["title"], first["minutes"]),
                first["tool"],
                {"profile": prof},
            )
        else:
            step = _next_step(
                "Next up is %s, about %d minutes; follow the steps and tell me when it is done."
                % (first["title"], first["minutes"]),
                None,
                {"profile": prof, "done": sorted(done_ids) + [first["id"]]},
            )
    else:
        step = _next_step(
            "Every checklist item is done, so now keep the weekly habit going.",
            "weekly_cadence",
            {"profile": prof, "done": sorted(done_ids)},
        )

    return {
        "score": _weighted_score(rows, done_ids),
        "items": rows,
        "next_three": next_three,
        "compliance": YMYL_COMPLIANCE if ymyl else None,
        "next_step": step,
    }


# ---------------------------------------------------------------------------
# weekly_cadence
# ---------------------------------------------------------------------------

TASK_GBP_POST = {
    "day": "Monday",
    "task": "Publish one Google Business Profile post (2-3 sentences plus one real photo)",
    "minutes": 15, "tool": None}

TASK_GBP_POST_PLUS = {
    "day": "Monday",
    "task": "Publish one Google Business Profile post and reply to any new Google review",
    "minutes": 20, "tool": None}

TASK_REVIEWS = {
    "day": "Monday",
    "task": "Ask every happy customer from last week for a Google review (send the review link) "
            "and reply to every review, good or bad",
    "minutes": 25, "tool": None}

TASK_REVIEWS_PLUS = {
    "day": "Friday",
    "task": "Send the Google review link to last week's happy customers and reply to every review",
    "minutes": 20, "tool": None}

TASK_ONE_PAGE = {
    "day": "Wednesday",
    "task": "Draft or improve ONE page from the page plan, then have the owner check every fact",
    "minutes": 60, "tool": "plan_landing_pages"}

TASK_ONE_PAGE_PLUS = {
    "day": "Wednesday",
    "task": "Draft or improve ONE page from the page plan, get the owner's fact check, "
            "publish it, add it to the sitemap and submit the URL (IndexNow / Search Console)",
    "minutes": 70, "tool": "plan_landing_pages"}

TASK_SUBMIT = {
    "day": "Thursday",
    "task": "Publish the reviewed page, add it to the sitemap and submit the URL (IndexNow / Search Console)",
    "minutes": 20, "tool": None}

TASK_LEADS = {
    "day": "Friday",
    "task": "Check calls, forms and quote requests, and note what people asked for",
    "minutes": 15, "tool": None}

TASK_LEADS_PLUS = {
    "day": "Friday",
    "task": "Check calls, forms and quote requests and note which pages brought them in",
    "minutes": 15, "tool": None}

TASK_BUYER_QUESTION = {
    "day": "Friday",
    "task": "Answer one real buyer question on the site or in the Google Q&A section",
    "minutes": 20, "tool": "find_content_gaps"}

# Weekly plans scaled to owner hours. Every variant keeps the required work:
# GBP post, review asks plus replies, one new or improved page, URL submission,
# and a lead check.
WEEKLY_PLANS = {
    3: [TASK_GBP_POST_PLUS, TASK_ONE_PAGE_PLUS, TASK_REVIEWS_PLUS],
    5: [TASK_GBP_POST, TASK_REVIEWS, TASK_ONE_PAGE, TASK_SUBMIT, TASK_LEADS],
    7: [TASK_GBP_POST, TASK_REVIEWS, TASK_ONE_PAGE, TASK_SUBMIT, TASK_LEADS,
        TASK_BUYER_QUESTION],
}
# For 7 tasks we also add a checklist item, giving the extra weekly slot.

MONTHLY = [
    {"task": "Re-run the AI visibility check: 5 buyer questions in ChatGPT, Perplexity and Gemini; record who was named",
     "minutes": 30, "tool": None},
    {"task": "Re-run the site audit (robots.txt, sitemap, schema, AI crawler access)",
     "minutes": 20, "tool": "audit_site"},
    {"task": "Review the setup checklist score and finish the top unfinished item",
     "minutes": 30, "tool": "setup_checklist"},
    {"task": "Check the top three competitors: new pages, new offers, review counts",
     "minutes": 20, "tool": None},
    {"task": "Refresh one old page: update numbers, add a question, re-submit the URL",
     "minutes": 45, "tool": None},
]

def _hours(prof: dict) -> int:
    val = prof.get("owner_hours_per_week")
    if not isinstance(val, int) or val < 0:
        return 3
    return val


def _weekly_target(hours: int) -> int:
    if hours <= 2:
        return 3
    if hours <= 5:
        return 5
    return 7


def weekly_cadence(profile, done=None, today=None) -> dict:
    """Weekly and monthly routine scaled to owner hours, plus paste-ready schedule."""
    prof = normalise_profile(profile)
    done_ids = _done_ids(prof, done)
    items = _checklist_items(prof)
    todo = _todo_sorted(items, done_ids)

    hours = _hours(prof)
    target = _weekly_target(hours)

    weekly = [dict(t, minutes=t["minutes"]) for t in WEEKLY_PLANS[target]]
    if len(weekly) < target:
        for it in todo:
            if len(weekly) >= target:
                break
            weekly.append({
                "day": "Saturday",
                "task": "Finish checklist item: %s (%d minutes)" % (it["title"], it["minutes"]),
                "minutes": it["minutes"],
                "tool": it.get("tool"),
            })
    weekly = weekly[:target]

    this_week = []
    for it in todo[:2]:
        this_week.append({
            "id": it["id"],
            "task": it["title"],
            "minutes": it["minutes"],
            "tool": it.get("tool"),
            "who": it.get("who"),
        })
    this_week.append({
        "id": "content_page",
        "task": TASK_ONE_PAGE["task"],
        "minutes": 60,
        "tool": "plan_landing_pages",
        "who": "both",
    })
    this_week = this_week[:3]

    name = prof.get("business_name") or "my business"
    area = ", ".join(prof.get("service_area") or []) or prof.get("state") or "my area"
    chatgpt_task = (
        "Every Monday at 8am: run my AdRails weekly routine for %s (%s). "
        "Use the saved profile card from this project, call weekly_cadence first, then "
        "setup_checklist for the next unfinished item, then plan_landing_pages for the next "
        "page. Give me: 1) this week's three tasks with minutes, 2) one Google Business "
        "Profile post I can copy, 3) the outline for one page plus exactly which facts you "
        "need from me, 4) the URLs to submit after publishing. Keep every fact true, add "
        "compliance notes for regulated topics, one page a week maximum, and remind me that "
        "I review every page before it goes live." % (name, area)
    )
    when = " (%s)" % today if today else ""
    calendar = ("%s - 1 hour AdRails block: GBP post, review asks, and one page draft for "
                "my review%s" % (name, when))

    rules = [
        "One page a week, reviewed by you, maximum.",
        "Never bulk-publish or auto-generate pages; Google's scaled content abuse policy punishes it.",
        "Every number, license and carrier name on a page must come from you, the owner.",
        "Add each new page to the sitemap and submit it via IndexNow or Search Console.",
        "Link each new page from the home or services page.",
        "Reply to every review within a week.",
    ]
    ymyl = is_ymyl(prof.get("industry"))
    if ymyl:
        rules.append(
            "YMYL: licensed byline with number, no guarantees or 'best rates' claims, "
            "state regulator disclaimer, licensed human review before publishing.")

    if todo:
        first = todo[0]
        if first.get("tool"):
            step = _next_step(
                "Start this week with %s; I will open the tool that helps with it."
                % first["title"],
                first["tool"],
                {"profile": prof},
            )
        else:
            step = _next_step(
                "Start this week with %s (%d minutes); tell me when it is done and I will "
                "update the checklist." % (first["title"], first["minutes"]),
                "setup_checklist",
                {"profile": prof, "done": sorted(done_ids) + [first["id"]]},
            )
    else:
        step = _next_step(
            "The checklist is clear, so open the page planner and pick next week's page.",
            "plan_landing_pages",
            {"profile": prof},
        )

    return {
        "weekly": weekly,
        "monthly": [dict(m) for m in MONTHLY],
        "this_week": this_week,
        "schedule_text": {"chatgpt_task": chatgpt_task, "calendar": calendar},
        "rules": rules,
        "owner_hours_per_week": hours,
        "checklist_score": _weighted_score(items, done_ids),
        "today": today,
        "compliance": COMPLIANCE_SHORT if ymyl else None,
        "next_step": step,
    }


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

INSURANCE_PROFILE = {
    "business_name": "  Sunshine   Coast Insurance ",
    "website": "https://sunshinecoastins.com",
    "industry": "Independent Insurance Agency",
    "services": ["home insurance", "flood insurance", "auto insurance"],
    "service_area": ["Tampa", "St Petersburg", "Clearwater"],
    "state": "Florida",
    "customers": "families who just bought a house near the coast",
    "ideal_lead": "home and flood bundle",
    "competitors": ["captive agency branch", "big online insurer"],
    "differentiators": ["answers the phone", "20 years local"],
    "phone": "813-555-0100",
    "has_gbp": True,
    "reviews_count": 37,
    "owner_hours_per_week": 4,
    "publishing": "wordpress",
    "goals": "ten more quote requests a month",
    "licenses": "FL agent license A123456",
    "made_up_key": "should be dropped",
}

PLUMBER_PROFILE = {
    "business_name": "Drain Guys Plumbing",
    "website": "drainguys.com",
    "industry": "plumber",
    "services": ["emergency drain cleaning", "water heater repair"],
    "service_area": ["Austin", "Round Rock"],
    "state": "Texas",
    "owner_hours_per_week": 1,
}


def _selftest() -> int:
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        assert cond, "FAILED: " + msg
        checks += 1

    # --- empty profile -----------------------------------------------------
    r = get_started(None)
    ok(r["stage"] == "interview", "empty profile interviews")
    ok(1 <= len(r["questions"]) <= 4, "at most 4 questions per round")
    ok(r["questions"][0]["key"] == "business_name", "round 1 starts with business name")
    ok(r["compliance"] is None, "blank industry is not YMYL")
    ok(r["next_step"]["call"] == "get_started", "interview next_step calls get_started")
    ok(r["profile_card"]["services"] == [], "missing lists default to []")
    ok(isinstance(r["instructions_for_assistant"], str) and "profile_card" in r["instructions_for_assistant"],
       "instructions mention the profile card")
    ok(len(r["look_up_yourself"]) >= 3, "look-up list present")
    json.dumps(r)
    for q in r["questions"]:
        ok(all(isinstance(q.get(k), str) and q[k] for k in ("key", "ask", "why", "example")),
           "question has key/ask/why/example")

    # --- partial profile ---------------------------------------------------
    rp = get_started({"business_name": "Acme", "industry": "insurance"})
    ok(rp["stage"] == "interview", "partial profile keeps interviewing")
    ok(rp["round"] == 1, "partial profile stays in round 1")
    ok("website" in [q["key"] for q in rp["questions"]], "asks for the website")
    ok("licenses" in rp["missing"], "YMYL partial profile tracks licenses")
    ok(rp["compliance"] is not None, "partial insurance profile gets compliance note")
    json.dumps(rp)

    # --- full insurance profile -> ready -----------------------------------
    r2 = get_started(INSURANCE_PROFILE)
    ok(r2["stage"] == "ready", "full profile is ready")
    ok(all(q["key"] in r2["missing"] for q in r2["questions"]) and len(r2["questions"]) <= 4, "ready carries only still-missing questions (<=4)")
    ok(r2["next_step"]["call"] == "setup_checklist", "ready points at setup_checklist")
    ok(r2["profile_card"]["business_name"] == "Sunshine Coast Insurance", "name trimmed and normalised")
    ok("made_up_key" not in r2["profile_card"], "unknown keys dropped")
    ok("licenses" not in r2["missing"], "licenses recorded")
    json.dumps(r2)

    # --- is_ymyl -----------------------------------------------------------
    ok(is_ymyl("Insurance agency") is True, "insurance is YMYL")
    ok(is_ymyl("MORTGAGE broker") is True, "mortgage is YMYL")
    ok(is_ymyl("dental office") is True, "dental is YMYL")
    ok(is_ymyl("plumber") is False, "plumber is not YMYL")
    ok(is_ymyl("lawn care") is False, "lawn care is not YMYL (law boundary)")
    ok(is_ymyl("") is False, "empty industry is not YMYL")
    ok(is_ymyl(None) is False, "None industry is not YMYL")

    # --- normalise caps ----------------------------------------------------
    big = {"services": ["svc %d" % i for i in range(1000)],
           "goals": "x" * 10000, "differentiators": "one thing"}
    npb = normalise_profile(big)
    ok(len(npb["services"]) <= LIST_CAP, "list capped at 30")
    ok(len(npb["goals"]) <= STR_CAP, "string capped at 300")
    ok(npb["differentiators"] == ["one thing"], "bare string becomes a list")
    ok(normalise_profile("not a dict")["business_name"] is None, "non-dict tolerated")
    json.dumps(npb)

    # --- checklist ---------------------------------------------------------
    cl = setup_checklist(INSURANCE_PROFILE)
    ids = [it["id"] for it in cl["items"]]
    ok(len(cl["items"]) >= 16, "checklist has at least 16 items")
    ok(len(ids) == len(set(ids)), "checklist ids unique")
    for required in ("gbp_claim", "gbp_complete", "gbp_posts", "reviews_engine", "bing_places",
                     "apple_business_connect", "search_console", "bing_webmaster", "indexnow",
                     "ai_crawlers", "llms_txt", "schema", "nap_consistency", "directories",
                     "service_pages", "answer_pages", "ai_visibility_baseline"):
        ok(required in ids, "required id present: " + required)
    ok(0 <= cl["score"] <= 100, "score in 0-100")
    ok(cl["score"] == 0, "nothing done = 0 percent")
    ok(len(cl["next_three"]) == 3, "next_three has 3 ids")
    ok(cl["compliance"] is not None and "license" in cl["compliance"].lower(),
       "insurance checklist has compliance note")
    ok(all(it["status"] in ("done", "todo") for it in cl["items"]), "status values valid")
    ok(all(it["who"] in ("owner", "assistant", "both") for it in cl["items"]), "who values valid")
    ok(all(1 <= it["priority"] <= 3 for it in cl["items"]), "priority 1-3")
    ok(all(3 <= len(it["how"]) <= 6 for it in cl["items"]), "how has 3-6 steps")
    ok(cl["items"][0]["priority"] == 1, "priority 1 items come first")
    json.dumps(cl)

    # --- YMYL differs for plumber -----------------------------------------
    clp = setup_checklist(PLUMBER_PROFILE)
    ok(clp["compliance"] is None, "plumber checklist has no compliance note")
    plumb_dirs = [it for it in clp["items"] if it["id"] == "directories"][0]["how"][0]
    ok("Angi" in plumb_dirs and "Trusted Choice" not in plumb_dirs,
       "home services directories for plumber")
    ins_dirs = [it for it in cl["items"] if it["id"] == "directories"][0]["how"][0]
    ok("Trusted Choice" in ins_dirs and "Angi" not in ins_dirs,
       "insurance directories for agency")

    # --- done list affects score ------------------------------------------
    all_ids = [it["id"] for it in cl["items"]]
    cl_all = setup_checklist(INSURANCE_PROFILE, done=all_ids)
    ok(cl_all["score"] == 100, "all done = 100 percent")
    ok(cl_all["next_three"] == [], "no next three when all done")
    ok(cl_all["next_step"]["call"] == "weekly_cadence", "done checklist points at weekly_cadence")
    ok(all(it["status"] == "done" for it in cl_all["items"]), "all items marked done")
    cl_some = setup_checklist(INSURANCE_PROFILE, done=all_ids[:6])
    ok(0 < cl_some["score"] < 100, "partial done gives partial score")
    ok(cl_some["score"] > cl["score"], "doing items raises the score")
    ok(setup_checklist(INSURANCE_PROFILE, done=["gbp_claim"])["score"] ==
       _weighted_score(cl["items"], {"gbp_claim"}), "score is priority weighted")
    pd = dict(INSURANCE_PROFILE, done=["gbp_claim", "reviews_engine"])
    ok(setup_checklist(pd)["score"] > 0, "profile.done also counted")

    # --- weekly cadence scaling -------------------------------------------
    w_low = weekly_cadence(PLUMBER_PROFILE)          # 1 hour  -> 3 tasks
    w_mid = weekly_cadence(INSURANCE_PROFILE)        # 4 hours -> 5 tasks
    w_high = weekly_cadence(dict(INSURANCE_PROFILE, owner_hours_per_week=10))  # -> 7 tasks
    ok(len(w_low["weekly"]) == 3, "<=2h gives 3 weekly tasks")
    ok(len(w_mid["weekly"]) == 5, "<=5h gives 5 weekly tasks")
    ok(len(w_high["weekly"]) == 7, ">5h gives 7 weekly tasks")
    ok(4 <= len(w_mid["monthly"]) <= 5, "monthly has 4-5 tasks")
    ok(len(w_mid["this_week"]) == 3, "this_week has 3 tasks")
    ok(all({"day", "minutes", "tool"} <= set(t) for t in w_mid["weekly"]), "weekly task fields")
    joined = " ".join(t["task"] for t in w_mid["weekly"]).lower()
    ok("google business profile post" in joined, "weekly includes a GBP post")
    ok("review" in joined, "weekly includes review asks/replies")
    ok("one page" in joined, "weekly includes one page of work")
    ok("submit the url" in joined, "weekly includes URL submission")
    ok("calls" in joined, "weekly includes checking leads/calls")
    monthly_joined = " ".join(m["task"] for m in w_mid["monthly"]).lower()
    for needle in ("ai visibility", "audit", "checklist", "competitor", "refresh"):
        ok(needle in monthly_joined, "monthly includes " + needle)
    ok("Every Monday" in w_mid["schedule_text"]["chatgpt_task"], "chatgpt task text starts weekly")
    ok("profile card" in w_mid["schedule_text"]["chatgpt_task"].lower(),
       "chatgpt task mentions the profile card")
    ok(isinstance(w_mid["schedule_text"]["calendar"], str)
       and w_mid["schedule_text"]["calendar"], "calendar line present")
    ok(any("bulk" in rule.lower() for rule in w_mid["rules"]), "no bulk publishing rule")
    ok(any("one page a week" in rule.lower() for rule in w_mid["rules"]), "one page a week rule")
    ok(w_mid["compliance"] is not None, "insurance cadence carries compliance")
    ok(w_low["compliance"] is None, "plumber cadence has no compliance")
    ok(any("YMYL" in rule for rule in w_mid["rules"]), "YMYL rule added for insurance")
    ok(not any("YMYL" in rule for rule in w_low["rules"]), "no YMYL rule for plumber")
    json.dumps(w_mid)
    json.dumps(w_low)

    # --- today arg ---------------------------------------------------------
    w_today = weekly_cadence(INSURANCE_PROFILE, today="2026-11-13")
    ok(w_today["today"] == "2026-11-13", "today echoed back")
    ok("2026-11-13" in w_today["schedule_text"]["calendar"], "today appears in calendar")
    datetime.date.fromisoformat(w_today["today"])
    ok(weekly_cadence(INSURANCE_PROFILE)["today"] is None, "no clock read without today")

    # --- huge input does not explode --------------------------------------
    huge = {
        "business_name": "Y" * 10000,
        "industry": "insurance",
        "services": ["service number %d" % i for i in range(1000)],
        "service_area": ["city %d" % i for i in range(1000)],
        "differentiators": ["diff %d" % i for i in range(1000)],
        "competitors": ["comp %d" % i for i in range(1000)],
        "done": ["gbp_claim"] * 1000 + ["nope"] * 500,
        "goals": "goal " * 5000,
        "customers": ["not", "a", "string"],
        "owner_hours_per_week": "5",
        "has_gbp": "yes",
        "reviews_count": "12",
        "website": 1234,
    }
    r_huge = get_started(huge)
    cl_huge = setup_checklist(huge)
    w_huge = weekly_cadence(huge)
    for res in (r_huge, cl_huge, w_huge):
        json.dumps(res)
    ok(len(r_huge["profile_card"]["business_name"]) <= STR_CAP, "huge name capped")
    ok(len(r_huge["profile_card"]["services"]) <= LIST_CAP, "huge service list capped")
    ok(r_huge["profile_card"]["has_gbp"] is True, "string bool coerced")
    ok(r_huge["profile_card"]["owner_hours_per_week"] == 5, "string int coerced")
    ok(r_huge["profile_card"]["reviews_count"] == 12, "reviews count coerced")
    ok(r_huge["profile_card"]["customers"] is None, "list where string expected -> None")
    ok(r_huge["stage"] == "ready", "huge profile still resolves to ready")
    ok(len(w_huge["weekly"]) == 5, "huge profile weekly scaled to 5")
    ok(len(cl_huge["next_three"]) == 3, "huge profile still gives 3 next items")
    # a service_area with no state still works
    ok(get_started({"business_name": "B", "industry": "roofing",
                    "service_area": ["Miami"], "services": ["roof repair"]})["stage"] == "ready",
       "service_area satisfies readiness without explicit state")

    # --- next_step shape everywhere ---------------------------------------
    for res in (r, rp, r2, cl, clp, cl_all, cl_some, w_low, w_mid, w_high, w_today, w_huge, cl_huge):
        ns = res["next_step"]
        ok(isinstance(ns.get("say"), str) and ns["say"].strip(), "next_step.say is one sentence")
        ok(ns.get("call") is None or isinstance(ns.get("call"), str), "next_step.call type")
        ok(isinstance(ns.get("args_hint"), dict), "next_step.args_hint is a dict")

    # --- purity: no banned constructs in this source ----------------------
    with open(__file__, "r", encoding="utf-8") as fh:
        src = fh.read()
    banned = ["import " + "requests", "import " + "urllib", "import " + "socket",
              "datetime.date." + "today", "datetime.datetime." + "now",
              "random" + ".", "os" + ".environ", "import " + "os", "sub" + "process"]
    for token in banned:
        ok(token not in src, "banned construct absent: " + token)

    print("selftest checks passed: %d" % checks)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("coach_onboard.py - Module A: get_started, setup_checklist, weekly_cadence")
    print("Run: python3 coach_onboard.py --selftest")
