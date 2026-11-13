#!/usr/bin/env python3
"""
AdRails coach - Module B: page planning.

One pure function `plan_landing_pages(profile, existing_pages=None, limit=12)` that turns a
business profile ("profile card") into a prioritised list of pages the owner should build,
plus a first-page brief and a set of publishing rules.

Constraints honoured:
- Python 3.10+ stdlib only.
- Pure functions: no network, no disk, no env, no randomness, no current-date calls.
- Stateless: everything comes in through `profile` / `existing_pages`.
- Never recommend bulk/AI publishing; never invent facts about the business.

Run `python3 coach_pages.py --selftest` for the inline tests.
"""

from __future__ import annotations

import json
import re
import unicodedata

__all__ = ["plan_landing_pages", "is_ymyl", "slugify"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_STRLEN = 300
MAX_LIST = 30
MAX_SLUG = 60
MAX_SERVICES_PAGES = 6
MAX_SERVICE_CITY_SERVICES = 2  # "top services" x top 3 cities
MAX_CITIES = 3

YMYL_KEYWORDS = (
    "insurance",
    "insur",
    "finance",
    "financial",
    "mortgage",
    "legal",
    "law",
    "attorney",
    "lawyer",
    "medical",
    "medicine",
    "health",
    "dental",
    "dentist",
    "tax",
    "accounting",
    "accountant",
    "cpa",
    "investment",
    "investing",
    "wealth",
    "real estate",
    "realtor",
    "broker",
    "loan",
    "banking",
)

# Sensible alternatives for "<service> vs <alternative>" comparison pages.
COMPARISON_MAP = {
    "insurance": [
        "independent agent vs captive agent",
        "umbrella policy vs higher liability limits",
        "HO-3 vs HO-5 homeowners policy",
        "actual cash value vs replacement cost",
    ],
    "home services": [
        "repair vs replace",
        "fix now vs wait and see",
        "licensed contractor vs handyman",
    ],
    "legal": [
        "settle vs trial",
        "hourly fee vs flat fee",
    ],
    "medical": [
        "in person visit vs telehealth visit",
    ],
    "real estate": [
        "listing agent vs buyer's agent",
    ],
}

INSURANCE_SERVICE_HINTS = [
    "home insurance",
    "auto insurance",
    "flood insurance",
    "wind / hurricane insurance",
    "umbrella insurance",
    "business / commercial insurance",
    "life insurance",
]

COMPLIANCE_NOTE = (
    "This is a regulated financial product line (YMYL: your-money-or-your-life topics). "
    "Put a licensed-agent byline and real license number on every page, name a carrier only "
    "if you truly represent them, never promise savings or say 'best rates' or 'guaranteed', "
    "add the state regulator disclaimer, and have a licensed person read the page before "
    "it goes live."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clean_str(value, limit: int = MAX_STRLEN) -> str:
    """Trim, collapse whitespace and cap a string. Non-strings become ''."""
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, (int, float, bool)):
            value = str(value)
        else:
            return ""
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[:limit].rstrip()
    return value


def _clean_list(value, limit: int = MAX_LIST, item_len: int = MAX_STRLEN) -> list:
    """Normalise a list-ish value into a clean, capped list of unique strings."""
    if value is None:
        return []
    if isinstance(value, str):
        raw = [p for p in re.split(r"[,\n;|]", value)]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out, seen = [], set()
    for item in list(raw)[: limit * 3]:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or item.get("url") or ""
        s = _clean_str(item, item_len)
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "has", "have"}
    return False


def _as_int(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+", value.replace(",", ""))
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                return default
    return default


def is_ymyl(industry) -> bool:
    """True when the industry is 'your money or your life' (regulated) territory."""
    text = _clean_str(industry).lower()
    if not text:
        return False
    return any(k in text for k in YMYL_KEYWORDS)


def _industry_family(industry: str) -> str:
    """Coarse bucket used to pick comparison alternatives and page flavour."""
    t = (industry or "").lower()
    if any(k in t for k in ("insurance", "insur")):
        return "insurance"
    if any(k in t for k in ("legal", "law", "attorney", "lawyer")):
        return "legal"
    if any(k in t for k in ("medical", "dental", "dentist", "health", "clinic", "chiropract")):
        return "medical"
    if any(k in t for k in ("real estate", "realtor", "property manage")):
        return "real estate"
    if any(k in t for k in ("plumb", "hvac", "roof", "electric", "landscap", "clean", "pest",
                            "handyman", "contractor", "remodel", "paint", "floor", "movers",
                            "garage", "fenc", "pool", "tree", "junk")):
        return "home services"
    return "generic"


_SLUG_STOP = {"the", "and", "for", "with", "our", "your", "a", "an", "of", "in", "on", "at", "to"}


def slugify(text: str, max_len: int = MAX_SLUG) -> str:
    """Lowercase ascii-hyphen slug, <= max_len chars, never empty."""
    text = _clean_str(text, 400)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "page"


def _title_case(text: str) -> str:
    small = {"and", "or", "for", "the", "a", "an", "in", "of", "to", "vs", "with", "on", "at"}
    words = _clean_str(text, 200).split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in small:
            out.append(lw)
        elif w.isupper() and len(w) <= 5:
            out.append(w)  # keep acronyms: HO-3, FAQ
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out) or _clean_str(text)


def _dedupe_slug(base: str, used: set) -> str:
    slug = slugify(base)
    if slug not in used:
        used.add(slug)
        return slug
    n = 2
    while n <= 999:
        suffix = "-%d" % n
        candidate = slug[: MAX_SLUG - len(suffix)].rstrip("-") + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1
    used.add(slug)
    return slug


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", _clean_str(text, 400).lower())
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _tokens(text: str) -> set:
    stop = _SLUG_STOP | {"services", "service", "page", "home", "index", "html", "php", "www", "com"}
    return {t for t in _norm_text(text).split() if len(t) > 2 and t not in stop}


def _parse_existing(existing_pages) -> list:
    """Accept list of {'url','title'} dicts, URL strings, or one whitespace-separated string."""
    if not existing_pages:
        return []
    if isinstance(existing_pages, str):
        raw = [p for p in re.split(r"[\s,]+", existing_pages) if p]
    elif isinstance(existing_pages, (list, tuple, set)):
        raw = list(existing_pages)
    else:
        return []
    out = []
    for item in list(raw)[:200]:
        url, title = "", ""
        if isinstance(item, dict):
            url = _clean_str(item.get("url") or item.get("link") or item.get("path"), 400)
            title = _clean_str(item.get("title") or item.get("name"), 200)
        else:
            url = _clean_str(item, 400)
        if not url and not title:
            continue
        out.append({"url": url, "title": title})
    return out


def _existing_keys(existing: list) -> tuple:
    """Build (slug set, title set, token-bag list) for fuzzy matching."""
    slugs, titles, bags = set(), set(), []
    for page in existing:
        url = page.get("url", "")
        title = page.get("title", "")
        tail = ""
        if url:
            path = re.sub(r"^[a-zA-Z]+://[^/]+", "", url)
            path = path.split("?")[0].split("#")[0]
            tail = path.strip("/").split("/")[-1]
            tail = re.sub(r"\.(html?|php|aspx?|jsp)$", "", tail)
            slugs.add(slugify(tail) if tail else "")
            slugs.add(slugify(url))
        if title:
            titles.add(_norm_text(title))
            slugs.add(slugify(title))
        bag = _tokens(tail.replace("-", " ") + " " + title + " " + url.replace("-", " "))
        if bag:
            bags.append(bag)
    slugs.discard("")
    return slugs, titles, bags


def _exists(candidate: dict, existing: list, keys: tuple) -> bool:
    slugs, titles, bags = keys
    slug = candidate["slug"]
    norm_title = _norm_text(candidate["title"])
    if slug in slugs or norm_title in titles:
        return True
    cand_bag = _tokens(slug.replace("-", " ") + " " + candidate["title"])
    if not cand_bag:
        return False
    need = max(2, min(3, len(cand_bag)))
    for bag in bags:
        if not bag:
            continue
        overlap = cand_bag & bag
        if len(overlap) >= need and len(overlap) / len(cand_bag) >= 0.6:
            return True
    return False


def _dedupe_pages(pages: list) -> list:
    """Drop duplicate concepts (same type + same normalised title), keeping the best score."""
    seen, out = set(), []
    for page in sorted(pages, key=lambda p: -p["priority_score"]):
        key = (page["type"], _norm_text(page["title"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(page)
    return out


# ---------------------------------------------------------------------------
# Candidate builders
# ---------------------------------------------------------------------------

def _matches_ideal_lead(service: str, ideal_lead: str) -> bool:
    """True when the service clearly overlaps the customer the owner wants most."""
    if not ideal_lead:
        return False
    lead = _norm_text(ideal_lead)
    if not lead:
        return False
    if service.strip().lower() in ideal_lead.lower():
        return True
    generic = {"insurance", "insurances", "service", "services", "policy", "policies", "coverage"}
    words = [w for w in _norm_text(service).split() if len(w) >= 4 and w not in generic]
    return any(w in lead for w in words)


def _service_pages(p, fam, ymyl, services, ideal_lead, state, short_area):
    pages = []
    for svc in services[:MAX_SERVICES_PAGES]:
        low = svc.lower()
        svc_title = _title_case(svc)
        score = 70
        if _matches_ideal_lead(svc, ideal_lead):
            score += 15
        if ymyl:
            qs = [
                "How much does %s cost in %s?" % (low, state or "my area"),
                "What does %s actually cover?" % low,
                "Do I need %s where I live?" % low,
                "How do I choose a good %s agent?" % (low if "insurance" in low else low + " insurance"),
                "What is not covered by %s?" % low,
            ]
        else:
            qs = [
                "How much does %s cost in %s?" % (low, state or "my area"),
                "How long does %s take?" % low,
                "Do you serve %s?" % (short_area or "my area"),
                "How do I pick a good %s company?" % low,
                "What is included in %s?" % low,
            ]
        must = [
            "The exact services you offer for %s and what is included." % low,
            "Your real price range or how you quote (owner supplies the numbers - never guess).",
            "Your service area and response/turnaround time.",
        ]
        if ymyl:
            must.append("Your license number(s) and the carriers you are actually appointed with.")
            must.append("The disclaimer text your state regulator requires.")
        pages.append({
            "slug": svc,
            # Service pages keep the bare service name so they never read like a city page
            # (the city pages below carry "<Service> in <City>" titles).
            "title": svc_title,
            "type": "service",
            "target_questions": qs[:5],
            "primary_query": ("%s %s" % (low, state.strip())).strip() if state else low,
            "schema": (["Service", "InsuranceAgency", "FAQPage"] if fam == "insurance"
                       else ["Service", "LocalBusiness", "FAQPage"]),
            "must_include": must,
            "why": ("One page per service is the fastest way to rank for a buyer who already knows "
                    "what they want and is comparing providers."),
            "priority_score": score,
        })
    return pages


def _service_city_pages(fam, ymyl, services, cities, area_phrase):
    pages = []
    if not cities:
        return pages
    top_services = services[:MAX_SERVICE_CITY_SERVICES]
    for city in cities[:MAX_CITIES]:
        for idx, svc in enumerate(top_services):
            low = svc.lower()
            score = 60 + (10 if idx == 0 else 0)
            if ymyl:
                qs = [
                    "Who is the best %s agent in %s?" % (low if "insurance" in low else low + " insurance", city),
                    "How much is %s in %s?" % (low, city),
                    "Do I need flood coverage in %s?" % city if "flood" in low
                    else "Is %s required in %s?" % (low, city),
                    "Can you write %s for a home in %s?" % (low, city),
                ]
            else:
                qs = [
                    "Who does %s in %s?" % (low, city),
                    "How much does %s cost in %s?" % (low, city),
                    "Do you cover the %s area?" % city,
                    "How fast can you get to %s?" % city,
                ]
            pages.append({
                "slug": "%s-%s" % (low, city),
                "title": "%s in %s" % (_title_case(low), city),
                "type": "service_city",
                "target_questions": qs[:4],
                "primary_query": "%s %s" % (low, city.lower()),
                "schema": (["Service", "InsuranceAgency", "FAQPage"] if fam == "insurance"
                           else ["Service", "LocalBusiness"]),
                "must_include": [
                    "Proof you really serve %s: local jobs, local reviews, or a local office." % city,
                    "Travel/response time to %s and any local rules (flood zones, permits)." % city,
                    "A real local example or review (with the customer's permission).",
                ],
                "why": ("City pages win 'near me' and 'in <city>' searches, but only when they are "
                        "genuinely different from the main service page."),
                "priority_score": score,
            })
    return pages


def _pillar_pages(fam, ymyl, services, state, area_phrase):
    pages = []
    if not services:
        return pages
    focus = services[0].lower()
    where = state or "my area"
    if ymyl:
        cqs = [
            "How much does %s cost in %s?" % (focus, where),
            "What does %s cover and what does it not?" % focus,
            "Is %s required in %s?" % (focus, where),
            "How much coverage do I actually need?",
        ]
        if fam == "insurance":
            title = "How %s Works in %s: Coverage, Cost and Exclusions" % (_title_case(focus), where)
        else:
            title = "How Much Does %s Cost in %s?" % (_title_case(focus), where)
    else:
        cqs = [
            "How much does %s cost in %s?" % (focus, where),
            "How long does %s take, start to finish?" % focus,
            "What can go wrong and how do you prevent it?",
            "DIY vs hiring a pro: what is worth it?",
        ]
        title = "How Much Does %s Cost in %s?" % (_title_case(focus), where)
    pages.append({
        "slug": "%s-cost-and-guide" % focus,
        "title": title,
        "type": "pillar_guide",
        "target_questions": cqs,
        "primary_query": "%s cost %s" % (focus, (state or "").lower()),
        "schema": ["Article", "FAQPage"] + (["InsuranceAgency"] if fam == "insurance" else ["LocalBusiness"]),
        "must_include": [
            "Your real price ranges or the factors that move the price (owner supplies the numbers).",
            "Local rules, seasonality or risks specific to %s." % (state or "your service area"),
            "Your own experience: jobs done, claims handled, mistakes people make.",
        ],
        "why": ("Cost and explainer guides answer the question buyers ask before they are ready to "
                "call, and they earn links and AI citations."),
        "priority_score": 55,
    })
    return pages


def _comparison_pages(fam, area_phrase):
    pages = []
    for pair in COMPARISON_MAP.get(fam, [])[:3]:
        if " vs " in pair:
            left, right = pair.split(" vs ", 1)
            slug_base = "%s-vs-%s" % (left.strip(), right.strip())
        else:
            slug_base = pair
        pages.append({
            "slug": slug_base,
            "title": _title_case(pair),
            "type": "comparison",
            "target_questions": [
                "Which one is right for me?",
                "What is the real difference in %s?" % ("cost" if fam != "legal" else "outcome"),
                "Can I switch later if I choose wrong?",
            ],
            "primary_query": pair.lower(),
            "schema": ["Article", "FAQPage"],
            "must_include": [
                "An honest side-by-side table (no invented prices).",
                "A clear 'this is usually better when...' rule from your own practice.",
                "The compliance disclaimer" + (" and your license details." if fam == "insurance" else "."),
            ],
            "why": ("Comparison pages catch buyers at the decision moment - high intent, and low "
                    "competition when they are written from real experience."),
            "priority_score": 45,
        })
    return pages


def _faq_hub(services, ymyl, fam, area_phrase):
    qs = ["How much does %s cost?" % (services[0].lower() if services else "this service"),
          "Do you serve %s?" % (area_phrase or "my area"),
          "What do I need to get started?",
          "How do I get a quote?",
          "Are you licensed and insured?"]
    if ymyl:
        qs.append("What coverage do I need where I live?")
    return [{
        "slug": "faq",
        "title": "Frequently Asked Questions",
        "type": "faq_hub",
        "target_questions": qs[:5],
        "primary_query": "%s questions %s" % (fam if fam != "generic" else "local service",
                                              (area_phrase or "").lower()),
        "schema": ["FAQPage", "LocalBusiness"],
        "must_include": [
            "Short direct answers (2 sentences), then the detail.",
            "Your real phone number and opening hours.",
            "Only facts you can back up - no invented statistics.",
        ],
        "why": ("A single FAQ hub with FAQPage schema can win featured snippets and gets quoted by "
                "AI answer engines looking for short, clear answers."),
        "priority_score": 50,
    }]


def _about_trust(p, ymyl, fam, services, area_phrase):
    score = 50 + (20 if ymyl else 0)
    name = p.get("business_name") or "our business"
    diffs = p.get("differentiators") or []
    must = [
        "Owner/founder name, years in business, and a real photo.",
        "Licenses, certifications and association memberships that actually exist.",
        "Real review quotes with names (with permission).",
        "What makes %s different: %s" % (name, "; ".join(diffs[:3]) if diffs else "owner supplies this"),
    ]
    if ymyl:
        must.append("License numbers, appointed carriers, and the state regulator disclaimer.")
    return [{
        "slug": "about",
        "title": ("About %s" % name) if name != "our business" else "About Us",
        "type": "about_trust",
        "target_questions": [
            "Who am I actually talking to?",
            "Are they licensed, and how long have they done this?",
            "What do other customers say?",
            "Do they serve %s?" % (area_phrase or "my area"),
        ],
        "primary_query": "%s reviews and credentials" % name.lower(),
        "schema": (["AboutPage", "InsuranceAgency", "Person"] if fam == "insurance"
                   else ["AboutPage", "LocalBusiness", "Person"]),
        "must_include": must,
        "why": ("Trust pages close the deal for regulated and high-ticket services, and they are the "
                "page Google and AI systems check to confirm you are a real business."),
        "priority_score": score,
    }]


_STATE_NAMES = dict(zip(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
    ["Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho",
     "Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan","Minnesota",
     "Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire","New Jersey","New Mexico","New York","North Carolina",
     "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island","South Carolina","South Dakota","Tennessee","Texas",
     "Utah","Vermont","Virginia","Washington","West Virginia","Wisconsin","Wyoming","District of Columbia"]))


def _suggested_services(fam, services):
    """Insurance-only gentle suggestions. Never converted into pages here."""
    if fam != "insurance":
        return []
    have = " ".join(s.lower() for s in services)
    out = []
    for hint in INSURANCE_SERVICE_HINTS:
        heads = [w.strip().split(" ")[0] for w in hint.split("/")]
        if heads and not any(h and h in have for h in heads):
            out.append(hint)
    return out[:8]


def _avoid_list(fam):
    avoid = [
        {
            "what": "Competitor 'vs' pages (you vs <rival name>)",
            "reason": ("Thin, legally risky and easy to get wrong. Use your own service and "
                       "comparison pages to explain differences without naming rivals."),
        },
        {
            "what": "Bulk or AI-generated page batches published automatically",
            "reason": ("Google's 'scaled content abuse' policy punishes mass-produced pages. One "
                       "owner-reviewed page a week ranks better and keeps you safe."),
        },
        {
            "what": "Doorway pages (one page per suburb with the same text)",
            "reason": "Duplicate city pages get filtered out and can drag the whole site down.",
        },
    ]
    if fam == "insurance":
        avoid.append({
            "what": "Rate-table pages built from generic quote data",
            "reason": ("Insurance rates depend on the person and the property. Made-up numbers are a "
                       "compliance and trust problem - quote properly instead."),
        })
    return avoid


def _page_brief(page, ymyl):
    first_q = page["target_questions"][0] if page["target_questions"] else "this service"
    outline = [q if q.endswith("?") else q + "?" for q in page["target_questions"][:4]]
    if not outline:
        outline = ["What does this cost?", "What is included?", "Who is this for?"]
    faq = list(outline[:4])
    faq.append("How do I get started?")
    return {
        "title": page["title"],
        "h1": page["title"],
        "outline": outline[:5],
        "answer_first": ("Open with a 2-sentence direct answer to '%s' in plain English, before any "
                         "sales copy. A busy owner should get the answer without scrolling." % first_q),
        "faq": faq[:5],
        "cta": ("Call us at <your real phone number>, or send the form and we will reply within one "
                "business day."),
        "schema": page["schema"],
        "must_include": page["must_include"],
        "compliance": COMPLIANCE_NOTE if ymyl else None,
    }


# ---------------------------------------------------------------------------
# Profile normalisation
# ---------------------------------------------------------------------------

def _normalise_profile(profile) -> dict:
    if not isinstance(profile, dict):
        profile = {}
    return {
        "business_name": _clean_str(profile.get("business_name")),
        "website": _clean_str(profile.get("website"), 400),
        "industry": _clean_str(profile.get("industry")),
        "services": _clean_list(profile.get("services")),
        "service_area": _clean_list(profile.get("service_area")),
        "state": _clean_str(profile.get("state"), 60),
        "customers": _clean_str(profile.get("customers")),
        "ideal_lead": _clean_str(profile.get("ideal_lead")),
        "competitors": _clean_list(profile.get("competitors")),
        "differentiators": _clean_list(profile.get("differentiators")),
        "phone": _clean_str(profile.get("phone"), 60),
        "gbp_url": _clean_str(profile.get("gbp_url"), 400),
        "has_gbp": _as_bool(profile.get("has_gbp")),
        "reviews_count": _as_int(profile.get("reviews_count")),
        "owner_hours_per_week": _as_int(profile.get("owner_hours_per_week")),
        "publishing": _clean_str(profile.get("publishing"), 60),
        "goals": _clean_str(profile.get("goals")),
        "licenses": _clean_str(profile.get("licenses")),
        "done": _clean_list(profile.get("done")),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_landing_pages(profile, existing_pages=None, limit=12) -> dict:
    """Plan the next pages to build for this business. Pure, deterministic, JSON-safe."""
    p = _normalise_profile(profile)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(limit, 50))

    services = p["services"]
    ymyl = is_ymyl(p["industry"])
    fam = _industry_family(p["industry"])
    state = _STATE_NAMES.get(p["state"].strip().upper(), p["state"]) if p["state"] else p["state"]

    # Cities = service_area entries that are short enough to be a place name.
    cities = [e for e in p["service_area"] if len(e) <= 40]
    if cities:
        area_phrase = ", ".join(cities[:3]) + (", " + state if state else "")
    else:
        area_phrase = state

    if cities:
        short_area = cities[0] + (", " + state if state else "")
    else:
        short_area = state

    existing = _parse_existing(existing_pages)
    keys = _existing_keys(existing)

    candidates = []
    candidates += _service_pages(p, fam, ymyl, services, p["ideal_lead"], state, short_area)
    candidates += _service_city_pages(fam, ymyl, services, cities, area_phrase)
    candidates += _pillar_pages(fam, ymyl, services, state, area_phrase)
    candidates += _comparison_pages(fam, area_phrase)
    candidates += _faq_hub(services, ymyl, fam, area_phrase)
    candidates += _about_trust(p, ymyl, fam, services, area_phrase)

    # Unique slugs, in creation order, then fuzzy "already on the site" detection.
    used = set()
    for page in candidates:
        page["slug"] = _dedupe_slug(page["slug"], used)
        page["exists"] = _exists(page, existing, keys)

    candidates = _dedupe_pages(candidates)
    order = {pg["slug"]: i for i, pg in enumerate(candidates)}

    already_have = [pg for pg in candidates if pg["exists"]]
    fresh = [pg for pg in candidates if not pg["exists"]]
    sort_key = lambda pg: (-pg["priority_score"], order.get(pg["slug"], 0))
    already_have.sort(key=sort_key)
    fresh.sort(key=sort_key)

    short_have = [{
        "slug": pg["slug"],
        "title": pg["title"],
        "type": pg["type"],
        "priority_score": pg["priority_score"],
        "note": "Already on your site - improve it instead of making a new one.",
    } for pg in already_have]

    pages = fresh[:limit]
    suggested = _suggested_services(fam, services)

    publishing_rules = [
        "One page a week, maximum. Quality beats volume every time.",
        "You review every fact before it goes live - no numbers the owner did not confirm.",
        "Never bulk-publish or auto-publish AI drafts; Google's scaled content abuse policy punishes it.",
        "Add each new page to the sitemap and submit it via IndexNow and Google Search Console.",
        "Link the new page from your home page or services page so people (and crawlers) find it.",
        "Use the customer's words: the questions in target_questions are what people actually type.",
    ]
    if ymyl:
        publishing_rules.append(COMPLIANCE_NOTE)

    first_page_brief = _page_brief(pages[0], ymyl) if pages else None

    gap_pages = list(dict.fromkeys(([p["website"]] if p.get("website") else [])
                                   + [e["url"] for e in existing if e.get("url")]))[:10]
    if pages and not gap_pages:
        # No URL to check yet: a hint with an empty page list would be an invalid call, so say it in words instead.
        next_step = {
            "say": ("Here is your prioritised page list. The first page to write is '%s'. Tell me the website address "
                    "(or that there is none yet) and I will check what is already covered before drafting it."
                    % pages[0]["title"]),
            "call": None,
            "args_hint": {"queries": list(pages[0]["target_questions"])},
        }
    elif pages:
        top = pages[0]
        next_step = {
            "say": ("Here is your prioritised page list. The first page to write is '%s'; before we "
                    "draft it, let's confirm nobody already answers those questions on your site."
                    % top["title"]),
            "call": "find_content_gaps",
            "args_hint": {
                "queries": list(top["target_questions"]),
                "pages": gap_pages,
                "city": (cities[0] if cities else ""),
            },
        }
    else:
        next_step = {
            "say": ("You already cover the pages I would have suggested, so the next win is improving "
                    "the existing ones - starting with your weakest service page."),
            "call": "audit_page",
            "args_hint": {"url": p.get("website") or ""},
        }

    return {
        "pages": pages,
        "already_have": short_have,
        "suggested_services": suggested,
        "avoid": _avoid_list(fam),
        "publishing_rules": publishing_rules,
        "first_page_brief": first_page_brief,
        "counts": {
            "generated": len(pages),
            "already_have": len(short_have),
            "total_candidates": len(candidates),
        },
        "profile_card": p,
        "next_step": next_step,
    }


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

def _selftest() -> None:
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        assert cond, "FAIL: " + name

    # 1-4 empty profile
    empty = plan_landing_pages({})
    check("empty profile returns dict", isinstance(empty, dict))
    check("empty profile has valid next_step", "say" in empty["next_step"] and "call" in empty["next_step"]
          and "args_hint" in empty["next_step"])
    check("empty profile pages is a list", isinstance(empty["pages"], list))
    check("empty profile still offers an FAQ hub", any(pg["type"] == "faq_hub" for pg in empty["pages"]))
    json.dumps(empty)

    # 5-7 partial profile, YMYL by industry only
    partial = plan_landing_pages({"business_name": "Acme", "industry": "accounting"})
    check("partial: accounting is ymyl", is_ymyl("Accounting") is True)
    check("partial: compliance rule present", any("regulator" in r for r in partial["publishing_rules"]))
    check("partial: no city pages without service_area",
          not any(pg["type"] == "service_city" for pg in partial["pages"]))
    json.dumps(partial)

    # 8+ full insurance profile
    insurance_profile = {
        "business_name": "Sunshine Risk Advisors",
        "website": "https://sunshinerisk.example",
        "industry": "insurance agency",
        "services": ["home insurance", "flood insurance", "auto insurance"],
        "service_area": ["Tampa", "St Petersburg", "Clearwater"],
        "state": "FL",
        "customers": "homeowners and small business owners",
        "ideal_lead": "homeowners bundling home and flood",
        "competitors": ["bigboxagency.example"],
        "differentiators": ["answers the phone", "40 carrier options"],
        "phone": "813-555-0100",
        "has_gbp": True,
        "reviews_count": 42,
        "owner_hours_per_week": 3,
        "publishing": "wordpress",
        "goals": "more flood insurance leads",
        "licenses": "FL agent license A123456",
        "done": [],
    }
    ins = plan_landing_pages(insurance_profile)
    ins_all = plan_landing_pages(insurance_profile, limit=50)
    check("insurance: is_ymyl", is_ymyl("insurance agency") is True)
    check("insurance: has service pages", any(pg["type"] == "service" for pg in ins["pages"]))
    check("insurance: has city pages", any(pg["type"] == "service_city" for pg in ins["pages"]))
    check("insurance: Tampa city page exists",
          any("tampa" in pg["slug"] for pg in ins["pages"] if pg["type"] == "service_city"))
    check("insurance: city pages capped",
          len([pg for pg in ins["pages"] if pg["type"] == "service_city"]) <= MAX_SERVICE_CITY_SERVICES * MAX_CITIES)
    check("insurance: has comparison pages", any(pg["type"] == "comparison" for pg in ins_all["pages"]))
    service_scores = {pg["slug"]: pg["priority_score"] for pg in ins["pages"] if pg["type"] == "service"}
    check("insurance: ideal_lead boosts the home service page to 85",
          service_scores.get("home-insurance") == 85)
    check("insurance: ideal_lead does not boost unrelated auto page",
          service_scores.get("auto-insurance") == 70)
    check("insurance: suggested_services non-empty", len(ins["suggested_services"]) > 0)
    check("insurance: suggested_services exclude owned services",
          "home insurance" not in ins["suggested_services"])
    check("insurance: avoid list warns about competitor pages",
          any("Competitor" in a["what"] for a in ins["avoid"]))
    check("insurance: no vs_competitor pages", not any(pg["type"] == "vs_competitor" for pg in ins_all["pages"]))
    check("insurance: first_page_brief present", isinstance(ins["first_page_brief"], dict))
    check("insurance: brief has answer_first", "answer_first" in (ins["first_page_brief"] or {}))
    check("insurance: brief faq length 3-5",
          3 <= len((ins["first_page_brief"] or {}).get("faq", [])) <= 5)
    check("insurance: brief compliance note present", bool((ins["first_page_brief"] or {}).get("compliance")))
    check("insurance: next_step calls find_content_gaps", ins["next_step"]["call"] == "find_content_gaps")
    check("insurance: sorted by priority desc",
          all(ins["pages"][i]["priority_score"] >= ins["pages"][i + 1]["priority_score"]
              for i in range(len(ins["pages"]) - 1)))
    check("insurance: service page title is the bare service name",
          any(pg["type"] == "service" and pg["slug"] == "home-insurance" and pg["title"] == "Home Insurance"
              for pg in ins["pages"]))
    check("insurance: city page title carries the city",
          any(pg["type"] == "service_city" and pg["title"] == "Home Insurance in Tampa" for pg in ins["pages"]))
    check("insurance: service and city titles never collide",
          len({pg["title"] for pg in ins_all["pages"]}) == len(ins_all["pages"]))
    check("insurance: slugs unique", len({pg["slug"] for pg in ins["pages"]}) == len(ins["pages"]))
    check("insurance: slugs ascii lowercase hyphen",
          all(re.fullmatch(r"[a-z0-9-]+", pg["slug"]) for pg in ins["pages"]))
    check("insurance: slugs <= 60 chars", all(len(pg["slug"]) <= 60 for pg in ins["pages"]))
    check("insurance: 3-5 target questions per page",
          all(3 <= len(pg["target_questions"]) <= 5 for pg in ins["pages"]))
    check("insurance: every page has must_include and why",
          all(pg["must_include"] and pg["why"] for pg in ins["pages"]))
    check("insurance: no page marked exists by default", all(pg["exists"] is False for pg in ins["pages"]))
    json.dumps(ins)

    # plumber: not YMYL
    plumber_profile = {
        "business_name": "Rapid Pipe Co",
        "industry": "plumbing",
        "services": ["emergency leak repair", "water heater install"],
        "service_area": ["Tampa", "Brandon"],
        "state": "FL",
        "owner_hours_per_week": 2,
    }
    plumb = plan_landing_pages(plumber_profile)
    check("plumber: not ymyl", is_ymyl("plumbing") is False)
    check("plumber: no compliance rule", not any("regulator" in r for r in plumb["publishing_rules"]))
    check("plumber: home-services comparison page",
          any("replace" in pg["slug"] or "repair" in pg["slug"] for pg in plumb["pages"]))
    check("plumber: no suggested_services", plumb["suggested_services"] == [])
    check("plumber: brief compliance is None", plumb["first_page_brief"]["compliance"] is None)
    json.dumps(plumb)

    # exists detection
    existing = [
        {"url": "https://sunshinerisk.example/home-insurance/", "title": "Home Insurance"},
        "https://sunshinerisk.example/flood-insurance",
        {"url": "https://sunshinerisk.example/tampa-home-insurance", "title": "Home Insurance Tampa"},
    ]
    ins_ex = plan_landing_pages(insurance_profile, existing_pages=existing)
    check("exists: pages moved to already_have", len(ins_ex["already_have"]) >= 2)
    check("exists: no slug in both lists",
          not (set(pg["slug"] for pg in ins_ex["already_have"]) & set(pg["slug"] for pg in ins_ex["pages"])))
    check("exists: home insurance detected",
          any("home-insurance" in pg["slug"] for pg in ins_ex["already_have"]))
    check("exists: nothing in pages flagged exists",
          all(pg["exists"] is False for pg in ins_ex["pages"]))
    json.dumps(ins_ex)

    # limit behaviour
    limited = plan_landing_pages(insurance_profile, limit=2)
    check("limit: respected", len(limited["pages"]) == 2)
    check("limit: keeps the best pages",
          limited["pages"][0]["priority_score"] >= limited["pages"][1]["priority_score"])
    check("limit: junk value still yields pages", len(plan_landing_pages(insurance_profile, limit="abc")["pages"]) >= 1)

    # huge inputs must not explode
    big_profile = {
        "business_name": "B" * 20000,
        "industry": "insurance agency",
        "services": ["service number %d" % i for i in range(1000)],
        "service_area": ["city %d" % i for i in range(1000)],
        "state": "F" * 10000,
        "differentiators": ["d"] * 1000,
        "competitors": ["c"] * 1000,
        "ideal_lead": "x" * 10000,
        "customers": "y" * 10000,
        "done": ["gbp_claim"] * 1000,
    }
    big = plan_landing_pages(big_profile, existing_pages=["u"] * 1000, limit=1000)
    check("huge: services capped", len(big["profile_card"]["services"]) <= MAX_LIST)
    check("huge: long strings capped", len(big["profile_card"]["ideal_lead"]) <= MAX_STRLEN)
    check("huge: page count bounded", len(big["pages"]) <= 50)
    check("huge: slugs <= 60", all(len(pg["slug"]) <= 60 for pg in big["pages"]))
    check("huge: slugs unique", len({pg["slug"] for pg in big["pages"]}) == len(big["pages"]))
    json.dumps(big)

    # slug helper
    check("slugify: accents transliterated", slugify("Café Flambé & Co.") == "cafe-flambe-co")
    check("slugify: caps length", len(slugify("a" * 200)) <= 60)
    check("slugify: never empty", slugify("!!!") == "page")

    # determinism + tolerance
    check("deterministic output",
          json.dumps(plan_landing_pages(insurance_profile)) == json.dumps(plan_landing_pages(insurance_profile)))
    check("non-dict profile tolerated", isinstance(plan_landing_pages(None), dict))

    passed = sum(1 for _, ok in checks if ok)
    print("checks run: %d, passed: %d" % (len(checks), passed))
    print("ALL PASS")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(json.dumps(plan_landing_pages({"business_name": "Demo", "industry": "plumbing",
                                             "services": ["leak repair"]}), indent=2))
