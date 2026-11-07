"""Google Business Profile (GBP) completeness audit for a small business.

Scores a plain-dict snapshot of the owner's GBP listing against a weighted
"100% complete" checklist, ranks the fixes worth the most points, and checks
the free-text fields against Google's limits (750-char description, no links,
phones or superlatives). Pure and offline: nothing here calls Google, the
network or the filesystem - the calling agent reads the owner's dashboard (or
the owner pastes a snapshot in) and hands the dict to `audit()`. Every return
value is JSON-serialisable and carries no secrets; bad input raises ToolError.
"""

import re

from ..errors import ToolError

# --------------------------------------------------------------- constants --
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_POST_DAYS = ("Tue", "Fri", "Wed")          # slot 1, 2, 3 inside each week
_POST_TYPES = ("offer", "update", "event")
_POST_CTAS = ("Learn more", "Get offer", "Call now", "Book")

_URL_RE = re.compile(r"(?:https?://|www\.|[\w-]+\.(?:com|net|org|biz|io|co|us)\b)", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_SUPERLATIVE_RE = re.compile(r"\b(?:best|cheapest|lowest[- ]price|top[- ]rated|number one)\b|#1", re.I)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")


# ----------------------------------------------------------------- helpers --
def _g(row, *path, default=None):
    """Nested safe get: _g(row, "a", "b") -> row["a"]["b"], else `default`."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return default if cur is None else cur


def _num(v, default=0.0):
    """Coerce Google's string-or-number fields to float, defensively; NaN/inf -> default (review 11-06 #11)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


def _int(v, default=0):
    f = _num(v, None)
    return int(f) if f is not None else default


def _lst(v):
    return list(v) if isinstance(v, (list, tuple)) else []


def _clamp01(x):
    x = _num(x, 0.0)
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _share(v):
    """A 0..1 share that may arrive as a percent ("85" or 85 -> 0.85; review 11-06 #11). ONLY for share fields, never for
    have/target ratios (a ratio above 1 is real over-delivery and must clamp to full credit)."""
    x = _num(v, 0.0)
    return _clamp01(x / 100.0 if x > 1.0 else x)


def _yes(v):
    """Flags arrive as bools or words; "false"/"no"/"0" are False (review #11: bool("false") is True)."""
    if isinstance(v, str):
        return v.strip().lower() in ("y", "yes", "true", "1", "on", "verified")
    return bool(v) and v == v


def _norm(text):
    """Lowercase + collapse punctuation so name repetition compares cleanly."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _has_phone(text):
    """True when the text holds a run with 7+ digits (a phone number)."""
    for m in _PHONE_RE.finditer(text):
        if len(re.sub(r"\D", "", m.group())) >= 10:
            return True
    return False


def _caps_run(text):
    """Longest run of consecutive ALL-CAPS words (shouting detection)."""
    best = run = 0
    for m in _WORD_RE.finditer(text):
        w = m.group()
        if len(w) >= 2 and w.isupper():
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _day_set(hours, day):
    """Is `day` (3-letter prefix) present in the hours dict with a value?"""
    if not isinstance(hours, dict):
        return False
    for k, v in hours.items():
        if isinstance(k, str) and k.strip().lower()[:3] == day:
            return v is not None and bool(str(v).strip())
    return False


# ------------------------------------------------------------- text checker --
def check_description(text, business_name=None):
    """Validate a GBP description against Google's limits and spam rules."""
    t = text if isinstance(text, str) else ("" if text is None else str(text))
    n = len(t)
    problems = []
    if not t.strip():
        problems.append("description is empty")
    else:
        if n > 750:
            problems.append(f"description is {n} chars; Google allows at most 750")
        elif n < 250:
            problems.append(f"description is only {n} chars; aim for 250-750 for full credit")
        if _URL_RE.search(t):
            problems.append("description contains a URL; Google strips links and may reject it")
        if _has_phone(t):
            problems.append("description contains a phone number; Google may reject it")
        if _caps_run(t) > 3:
            problems.append("description has a run of more than 3 ALL-CAPS words (shouting)")
        if _SUPERLATIVE_RE.search(t):
            problems.append("description uses a superlative (best/#1/cheapest); Google may reject it")
        if isinstance(business_name, str) and business_name.strip():
            nb, nt = _norm(business_name), _norm(t)
            if nb and (nt.startswith(nb + " " + nb) or nt.startswith(nb + nb)):
                problems.append("description starts by repeating the business name twice")
    return {"ok": not problems, "chars": n, "problems": problems}


# ---------------------------------------------------------- check evaluators --
# Each evaluator returns (fraction 0..1 of the weight earned, [warnings]).
def _f_verified(s):
    return (1.0 if _yes(s.get("verified")) else 0.0), []


def _f_primary(s):
    pc = s.get("primary_category")
    return (1.0 if isinstance(pc, str) and pc.strip() else 0.0), []


def _f_secondary(s):
    n = len([c for c in _lst(s.get("secondary_categories")) if str(c).strip()])
    warns = []
    if n > 4:
        warns.append(f"{n} secondary categories looks like category stuffing; keep 1-4")
    return (1.0 if 1 <= n <= 4 else 0.5 if 5 <= n <= 9 else 0.0), warns


def _f_description(s):
    d = _g(s, "description")
    if not isinstance(d, str) or not d.strip():
        return 0.0, []
    res = check_description(d, _g(s, "name"))
    warns = []
    if any("URL" in p for p in res["problems"]):
        warns.append("description contains a URL; Google strips links and may reject it")
    return (1.0 if res["ok"] else 0.5), warns


def _f_phone(s):
    p = s.get("phone")
    digits = re.sub(r"\D", "", p) if isinstance(p, str) else ""
    return (1.0 if len(digits) >= 7 else 0.0), []


def _f_website(s):
    w = s.get("website")
    if not isinstance(w, str) or not w.strip():
        return 0.0, []
    return (1.0 if w.strip().lower().startswith("https://") else 0.5), []


def _f_appointment(s):
    u = s.get("appointment_url")
    return (1.0 if isinstance(u, str) and u.strip().lower().startswith("http") else 0.0), []


def _f_hours(s):
    hours = _g(s, "hours")
    return sum(1 for d in _DOW if _day_set(hours, d)) / 7.0, []


def _f_special_hours(s):
    return (1.0 if _yes(s.get("special_hours_set")) else 0.0), []


def _f_services(s):
    """A service counts when it has a name and a >= 50 char description."""
    good = 0
    for item in _lst(s.get("services")):
        name = item if isinstance(item, str) else _g(item, "name")
        desc = _g(item, "description") if isinstance(item, dict) else ""
        if isinstance(name, str) and name.strip() and isinstance(desc, str) and len(desc.strip()) >= 50:
            good += 1
    return min(1.0, good / 5.0), []


def _f_attributes(s):
    total, answered = _int(_g(s, "attributes_total")), _int(_g(s, "attributes_answered"))
    if total <= 0:
        return 0.0, []
    return min(1.0, _clamp01(answered / total) / 0.9), []


def _f_photos(s):
    return _clamp01(_int(s.get("photos")) / 10.0), []


def _f_logo(s):
    return (1.0 if _yes(s.get("has_logo")) else 0.0), []


def _f_cover(s):
    return (1.0 if _yes(s.get("has_cover")) else 0.0), []


def _f_video(s):
    return _clamp01(_int(s.get("videos")) / 1.0), []


def _f_posts(s):
    return _clamp01(_int(s.get("posts_last_30d")) / 4.0), []


def _f_qa(s):
    return _clamp01(_int(s.get("qa_pairs")) / 5.0), []


def _f_reviews(s):
    return _clamp01(_int(s.get("reviews")) / 10.0), []


def _f_rating(s):
    r = _num(s.get("rating"))
    warns = []
    if 0 < r < 4.0:
        warns.append(f"rating {r:.1f} is below 4.0; ask every customer for a review (never only the happy ones) and reply to every review")
    return _clamp01(r / 4.5), warns


def _f_replied(s):
    return _clamp01(_share(s.get("reviews_replied_pct")) / 0.9), []


def _f_social(s):
    return _clamp01(len(_lst(s.get("social_links"))) / 1.0), []


def _f_messaging(s):
    return (1.0 if _yes(s.get("messaging_on")) else 0.0), []


def _f_service_area(s):
    areas = _lst(s.get("service_area"))
    warns = []
    if len(areas) > 20:
        warns.append(f"{len(areas)} service areas is over the limit of 20; trim the list")
    if not _yes(s.get("is_service_area_business")):
        return 1.0, warns          # not applicable -> nothing to fix
    frac = 0.0
    if _yes(s.get("address_hidden")):
        frac += 0.5
    else:
        warns.append("street address is shown on a service-area business; hide it")
    if areas and len(areas) <= 20:
        frac += 0.5
    return frac, warns


# -------------------------------------------------------------- checklist --
CHECKS = [
    {"id": "verified", "label": "Profile verified", "weight": 12,
     "fix": "Finish Google verification (video or postcard) for the profile",
     "why": "Unverified profiles lose edits, can be suspended and rank below verified competitors."},
    {"id": "primary_category", "label": "Primary category set", "weight": 10,
     "fix": "Choose the single most specific primary category for your main revenue",
     "why": "The primary category decides which searches and features (booking, menu) you can appear in."},
    {"id": "secondary_categories", "label": "Secondary categories (1-4)", "weight": 4,
     "fix": "Add 1-4 secondary categories for services you genuinely sell",
     "why": "Extra categories widen relevant queries; more than 4 looks like stuffing."},
    {"id": "description", "label": "Description (250-750 chars)", "weight": 8,
     "fix": "Write a 250-750 char description with no URL, phone number or superlatives",
     "why": "Google strips links and phones and may reject promos; a full description feeds the panel."},
    {"id": "phone", "label": "Local phone number", "weight": 4,
     "fix": "Add the local phone number customers actually call",
     "why": "Phone is a call-tracking signal and a top reason people tap the profile."},
    {"id": "website", "label": "HTTPS website", "weight": 4,
     "fix": "Point the website field at your https:// landing page",
     "why": "Plain http is flagged as unsafe and loses the click-through."},
    {"id": "appointment_url", "label": "Appointment / booking link", "weight": 2,
     "fix": "Add your booking or appointment URL so searchers can book directly",
     "why": "A booking link adds a call-to-action button and converts high-intent traffic."},
    {"id": "hours", "label": "Hours for all 7 days", "weight": 6,
     "fix": "Fill in opening hours for every day of the week (use 'closed' where true)",
     "why": "'Open now' filtering and map ranking depend on complete hours."},
    {"id": "special_hours", "label": "Special (holiday) hours", "weight": 2,
     "fix": "Set special hours for upcoming holidays and closures",
     "why": "Missing holiday hours causes bad visits and negative reviews."},
    {"id": "services", "label": "5+ services with descriptions", "weight": 8,
     "fix": "List at least 5 services, each with a 50+ character description",
     "why": "Service names and descriptions are matched against service-intent searches."},
    {"id": "attributes", "label": "Attributes mostly answered", "weight": 2,
     "fix": "Answer the profile attributes (at least 90% of the list)",
     "why": "Attributes power filters like 'wheelchair accessible' or 'free estimates'."},
    {"id": "photos", "label": "10+ photos", "weight": 8,
     "fix": "Upload at least 10 real photos (exterior, interior, team, work)",
     "why": "Photo count and freshness correlate with profile views and direction requests."},
    {"id": "logo", "label": "Logo uploaded", "weight": 2,
     "fix": "Upload a square logo image",
     "why": "The logo appears in search results and builds instant recognition."},
    {"id": "cover", "label": "Cover photo set", "weight": 2,
     "fix": "Choose a strong cover photo of the business",
     "why": "The cover photo is the first visual in the knowledge panel."},
    {"id": "video", "label": "At least one video", "weight": 2,
     "fix": "Upload one short video (15-30 s) of the team or work",
     "why": "Video sections keep people on the profile longer."},
    {"id": "posts", "label": "4+ posts in 30 days", "weight": 3,
     "fix": "Publish 4 posts this month (offers, updates, events)",
     "why": "Regular posts keep the profile fresh and add a call-to-action button."},
    {"id": "qa", "label": "5+ Q&A pairs", "weight": 2,
     "fix": "Seed 5 owner-answered questions from real customer calls",
     "why": "Owner-seeded Q&A blocks inaccurate answers from strangers."},
    {"id": "reviews", "label": "10+ reviews", "weight": 10,
     "fix": "Ask every customer for a review (no gating, no incentives) until you pass 10",
     "why": "Review count and recency are the strongest local ranking and trust signals."},
    {"id": "rating", "label": "Rating 4.5+", "weight": 2,
     "fix": "Lift the average rating toward 4.5 by fixing repeat complaints",
     "why": "Ratings below 4.0 visibly lose clicks to nearby competitors."},
    {"id": "reviews_replied_pct", "label": "Reply to 90%+ of reviews", "weight": 3,
     "fix": "Reply to at least 90% of reviews, including the bad ones",
     "why": "Owner replies show engagement and soften the impact of negative reviews."},
    {"id": "social_links", "label": "Social profiles linked", "weight": 1,
     "fix": "Link at least one social profile (Facebook, Instagram)",
     "why": "Social links add corroborating signals and another click path."},
    {"id": "messaging", "label": "Messaging enabled", "weight": 1,
     "fix": "Turn on messaging so searchers can text you",
     "why": "Messaging captures leads who will not phone during business hours."},
    {"id": "service_area", "label": "Service-area hygiene", "weight": 2,
     "fix": "Hide the street address and list up to 20 service areas",
     "why": "Service-area businesses that show an address break Google's rules and can be suspended."},
]

_FRAC = {
    "verified": _f_verified, "primary_category": _f_primary, "secondary_categories": _f_secondary,
    "description": _f_description, "phone": _f_phone, "website": _f_website,
    "appointment_url": _f_appointment, "hours": _f_hours, "special_hours": _f_special_hours,
    "services": _f_services, "attributes": _f_attributes, "photos": _f_photos,
    "logo": _f_logo, "cover": _f_cover, "video": _f_video, "posts": _f_posts, "qa": _f_qa,
    "reviews": _f_reviews, "rating": _f_rating, "reviews_replied_pct": _f_replied,
    "social_links": _f_social, "messaging": _f_messaging, "service_area": _f_service_area,
}


# ------------------------------------------------------------------- audit --
def audit(snapshot):
    """Score a GBP snapshot; return score/grade/passing/fixes/warnings/next_three."""
    if not isinstance(snapshot, dict):
        raise ToolError("snapshot must be a dict of Google Business Profile fields")

    total, passing, fixes, warnings = 0.0, [], [], []
    for ch in CHECKS:
        frac, warns = _FRAC[ch["id"]](snapshot)
        frac = _clamp01(frac)
        total += ch["weight"] * frac
        for w in warns:
            if w not in warnings:
                warnings.append(w)
        if frac >= 0.999:
            passing.append(ch["id"])
        else:
            fixes.append({"id": ch["id"], "label": ch["label"], "fix": ch["fix"],
                          "why": ch["why"], "points": round(ch["weight"] * (1.0 - frac), 2)})

    score = int(round(_clamp01(total / 100.0) * 100))
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    fixes.sort(key=lambda f: -f["points"])      # stable: ties keep CHECKS order
    return {"score": score, "grade": grade, "passing": passing, "fixes": fixes,
            "warnings": warnings, "next_three": [f["fix"] for f in fixes[:3]]}


# --------------------------------------------------------------- post plan --
def _service_names(services):
    """Accept ["Drain cleaning", ...] or [{"name": ..., "description": ...}, ...]."""
    if not isinstance(services, (list, tuple)):
        raise ToolError("services must be a list of names or {name, description} dicts")
    names = []
    for item in services:
        if isinstance(item, str):
            nm = item.strip()
        elif isinstance(item, dict):
            nm = str(item.get("name") or "").strip()
        else:
            nm = ""
        if nm:
            names.append(nm)
    return names


def post_plan(services, weeks=4, per_week=2, cta_url=None):
    """Build a GBP posting calendar rotating through `services`."""
    names = _service_names(services)
    if not names:
        raise ToolError("services must contain at least one named service")
    try:
        weeks, per_week = int(weeks), int(per_week)
    except (TypeError, ValueError):
        raise ToolError("weeks and per_week must be integers")
    if not 1 <= weeks <= 12:
        raise ToolError("weeks must be between 1 and 12")
    if not 1 <= per_week <= 3:
        raise ToolError("per_week must be between 1 and 3")
    if cta_url is not None and not isinstance(cta_url, str):
        raise ToolError("cta_url must be a string or None")

    plan, i = [], 0
    for week in range(1, weeks + 1):
        for slot in range(per_week):
            plan.append({
                "week": week,
                "day": _POST_DAYS[slot],                 # Tue, Fri, Wed
                "topic": names[i % len(names)],
                "type": _POST_TYPES[i % len(_POST_TYPES)],
                "cta": _POST_CTAS[i % len(_POST_CTAS)],
                "url": cta_url,
            })
            i += 1
    return plan


# ---------------------------------------------------------------- selftest --
def selftest() -> int:
    import json

    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    # --- checklist integrity -------------------------------------------------
    ok(sum(c["weight"] for c in CHECKS) == 100, "CHECKS weights must sum to 100")
    ok(len({c["id"] for c in CHECKS}) == len(CHECKS), "CHECKS ids must be unique")
    ok(all(set(c) == {"id", "label", "weight", "fix", "why"} for c in CHECKS), "CHECKS shape")
    ok({"verified", "primary_category", "description", "reviews", "photos", "services", "hours"}
       <= {c["id"] for c in CHECKS}, "required checks present")

    # --- empty / malformed snapshot -----------------------------------------
    empty = audit({})
    ok(0 <= empty["score"] <= 10, "empty snapshot scores 0-10")
    ok(empty["grade"] == "D", "empty snapshot is grade D")
    ok(len(empty["fixes"]) >= 15, "empty snapshot lists many fixes")
    pts = [f["points"] for f in empty["fixes"]]
    ok(pts == sorted(pts, reverse=True), "fixes sorted by points desc")
    ok(empty["next_three"] == [f["fix"] for f in empty["fixes"][:3]], "next_three mirrors top fixes")
    ok(audit({"photos": None, "services": None, "hours": None,
              "secondary_categories": None})["score"] <= 10, "None fields tolerated")

    # --- complete snapshot ---------------------------------------------------
    desc = ("Acme Plumbing keeps homes and small businesses in the neighbourhood running with "
            "same-day repairs, honest quotes and tidy workmanship. Our licensed plumbers handle "
            "leaks, blocked drains, water heater replacement and bathroom refits. We arrive on time, "
            "explain the options in plain language and clean up before we leave. Callouts cover the "
            "whole metro area seven days a week, and every job is backed by a written workmanship "
            "guarantee. Ask about our maintenance plan for landlords and cafes.")
    full = {
        "name": "Acme Plumbing", "primary_category": "Plumber",
        "secondary_categories": ["Drainage service", "Water heater repair"],
        "description": desc,
        "phone": "+1 555 123 4567", "website": "https://acmeplumbing.example",
        "appointment_url": "https://acmeplumbing.example/book",
        "address_hidden": False, "service_area": [], "is_service_area_business": False,
        "hours": {d: "9:00-17:00" for d in _DOW}, "special_hours_set": True,
        "services": [{"name": f"Service {i}",
                      "description": "Same-day service with tidy workmanship and a written guarantee."}
                     for i in range(5)],
        "products": [], "attributes_answered": 9, "attributes_total": 10,
        "photos": 12, "has_logo": True, "has_cover": True, "videos": 1,
        "posts_last_30d": 4, "qa_pairs": 6, "reviews": 25, "rating": 4.8,
        "reviews_replied_pct": 0.95, "social_links": ["https://facebook.com/acme"],
        "messaging_on": True, "verified": True, "opening_date": "2015-03-01",
        "booking_link": "https://acmeplumbing.example/book",
    }
    a = audit(full)
    ok(a["score"] >= 95 and a["grade"] == "A", "complete snapshot scores grade A")
    ok(a["fixes"] == [], "complete snapshot has no fixes")
    ok(a["warnings"] == [], "complete snapshot has no warnings")
    ok(isinstance(json.dumps(a), str), "audit output is JSON serialisable")

    # --- partial credit ------------------------------------------------------
    half = audit(dict(full, photos=5))
    ok(a["score"] - half["score"] == 4, "5 of 10 photos earns half of the 8 photo points")
    ok("photos" not in half["passing"], "partial photos is still an open fix")
    ok([f for f in half["fixes"] if f["id"] == "photos"][0]["points"] == 4.0, "photo fix worth 4 points")

    # --- service-area hygiene ------------------------------------------------
    sa = audit(dict(full, is_service_area_business=True, address_hidden=False,
                    service_area=["Springfield"]))
    ok(any("address" in w for w in sa["warnings"]), "shown address on service-area biz warns")
    ok("service_area" not in sa["passing"], "service-area check fails when address shown")
    ok(sa["score"] < a["score"], "service-area failure costs points")
    sa2 = audit(dict(full, is_service_area_business=True, address_hidden=True,
                     service_area=["Springfield"]))
    ok(sa2["score"] == a["score"], "hidden address + service area passes")
    many = audit(dict(full, is_service_area_business=True, address_hidden=True,
                      service_area=[f"Area {i}" for i in range(25)]))
    ok(any("20" in w for w in many["warnings"]), "too many service areas warns")

    # --- category stuffing ---------------------------------------------------
    stuff = audit(dict(full, secondary_categories=["A", "B", "C", "D", "E", "F"]))
    ok(any("stuffing" in w for w in stuff["warnings"]), "6 secondary categories -> stuffing warning")
    ok("secondary_categories" not in stuff["passing"], "6 categories earns only half credit (1-4 is the rule; review 11-06)")

    # --- description rules ---------------------------------------------------
    bad = check_description("Visit https://acme.example or call 555-123-4567 for the best price")
    ok(not bad["ok"], "url/phone/superlative description is not ok")
    ok(any("URL" in p for p in bad["problems"]), "url problem reported")
    ok(any("phone" in p for p in bad["problems"]), "phone problem reported")
    ok(any("superlative" in p for p in bad["problems"]), "superlative flagged")
    ok(any("750" in p for p in check_description("word " * 160)["problems"]), "800 chars -> too long")
    ok(check_description("")["chars"] == 0 and not check_description(None)["ok"],
       "empty and None descriptions fail")
    ok(any("250" in p for p in check_description("Short and sweet plumbing.")["problems"]),
       "short description flagged")
    ok(any("ALL-CAPS" in p for p in check_description("HUGE SALE NOW TODAY " + "x" * 250)["problems"]),
       "all-caps run flagged")
    ok(any("repeating" in p for p in check_description(
        "Acme Plumbing Acme Plumbing keeps drains clear " + "y" * 250,
        business_name="Acme Plumbing")["problems"]), "repeated business name flagged")
    ok(check_description(desc, "Acme Plumbing")["ok"], "the good description passes")

    # --- post plan -----------------------------------------------------------
    plan = post_plan([{"name": "Drain cleaning"}, "Water heaters", "Leak repair"],
                     weeks=2, per_week=2)
    ok(len(plan) == 4, "2 weeks x 2 posts = 4 rows")
    ok([p["day"] for p in plan] == ["Tue", "Fri", "Tue", "Fri"], "day order Tue/Fri")
    ok([p["topic"] for p in plan] == ["Drain cleaning", "Water heaters", "Leak repair",
                                      "Drain cleaning"], "topics rotate through services")
    ok({p["type"] for p in plan} <= {"offer", "update", "event"}, "post types valid")
    ok({p["cta"] for p in plan} <= {"Learn more", "Get offer", "Call now", "Book"}, "ctas valid")
    ok(all(p["url"] is None for p in plan), "url defaults to None")
    ok([p["day"] for p in post_plan(["A"], weeks=1, per_week=3)] == ["Tue", "Fri", "Wed"],
       "third slot is Wed")
    ok(post_plan(["A"], weeks=1, per_week=1, cta_url="https://x.example")[0]["url"]
       == "https://x.example", "cta url carried through")
    ok(isinstance(json.dumps(plan), str), "plan is JSON serialisable")

    # --- refusals ------------------------------------------------------------
    for label, fn in (
        ("non-dict snapshot", lambda: audit("nope")),
        ("no services", lambda: post_plan([])),
        ("services not a list", lambda: post_plan("plumbing")),
        ("unnamed services", lambda: post_plan([{"description": "x"}])),
        ("weeks 0", lambda: post_plan(["A"], weeks=0)),
        ("weeks 13", lambda: post_plan(["A"], weeks=13)),
        ("per_week 0", lambda: post_plan(["A"], per_week=0)),
        ("per_week 4", lambda: post_plan(["A"], per_week=4)),
    ):
        try:
            fn()
        except ToolError:
            ok(True, f"ToolError for {label}")
        else:
            ok(False, f"expected ToolError for {label}")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
