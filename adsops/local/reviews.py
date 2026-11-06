"""Review-request links, message drafts and review health for local businesses.

This module powers AdRails' review workflow: it builds Google review links,
creates SMS/email *drafts* asking customers for a review (it never sends
anything), drafts calm replies to Google reviews, computes review velocity /
"how many more 5 stars to reach 4.5?" math, and normalises Business Profile
API v4 review rows.  Safety rails: no review gating, no incentives, no PII in
drafts, no invented details, no liability admissions.  Tools should treat every
returned draft as text a human reviews before anything is sent.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from fractions import Fraction

from adsops.errors import ToolError, RailViolation, scrub

# --------------------------------------------------------------------------- #
# constants / compiled patterns
# --------------------------------------------------------------------------- #
_PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_URL_RE = re.compile(r"^https?://\S+$")
_GOOGLE_RE = re.compile(r"(?:google\.|g\.page|goo\.gl)", re.I)
_WS_RE = re.compile(r"\s+")
_BRACE_RE = re.compile(r"[{}<>]")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_SAFETY_RE = re.compile(
    r"\b(?:injur(?:y|ed|ies)|lawsuit|attorney|police|fire|flood|insurance\s+claim)\b",
    re.I,
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

_SMS_LIMIT = 300
_REPLY_LIMIT = 600
_SMS_TAIL = " Reply STOP to opt out"

_STAR_WORDS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

# Words that are too generic to be the "detail" we thank a reviewer for.
_STOPWORDS = frozenset("""
about above after again against all almost also always amazing and any are around
because been before being best better between both but came can could did does doing
done down during each either even ever every excellent experience fantastic first for
from great had has have having her here hers him his how into just know like made make
many more most much must my never nice not now off once only other our out over own
place please really recommend right said same service she should since so some such
sure than thank thanks that the their them then there these they thing things this
those through time today too took town under until very was way we well were what when
where which while who why will with work would you your yours
""".split())

_RULES = [
    "Ask every customer for a review — never gate, filter, or ask only happy "
    "customers (violates Google policy and FTC rules).",
    "Never offer discounts, gifts or other incentives in exchange for a review.",
    "Send the request within 24 hours of job completion, while it is fresh.",
    "Send at most one reminder after 3 days; do not keep chasing.",
    "Never script or write the customer's review — send the link and let them speak.",
    "Keep customer PII out of request messages and out of public replies.",
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Nested getter for camelCase Google Ads / GBP rows: _g(r, "reviewer", "displayName")."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _int(value, default=0):
    """Defensive int coercion: values may arrive as str/float/None."""
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _num(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean(value, limit=80):
    """Strip emoji/braces, collapse whitespace, cap length (keeps drafts safe to format)."""
    if value is None:
        return ""
    text = _EMOJI_RE.sub("", str(value))
    text = _BRACE_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()[:limit]


def _parse_dt(value):
    """ISO string / date / datetime -> timezone-aware datetime (UTC), else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ref_date(today):
    if today is None:
        return datetime.now(timezone.utc).date()
    if isinstance(today, datetime):
        return today.astimezone(timezone.utc).date() if today.tzinfo else today.date()
    if isinstance(today, date):
        return today
    dt = _parse_dt(today)
    return dt.date() if dt else datetime.now(timezone.utc).date()


def _clip(text, limit=_REPLY_LIMIT):
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "..."


def _detail_word(text):
    """Longest non-stopword word (>4 letters) — the single concrete detail we may echo."""
    best = ""
    for match in _WORD_RE.finditer(text or ""):
        word = match.group(0).strip("'-")
        if len(word) <= 4 or word.lower() in _STOPWORDS:
            continue
        if _SAFETY_RE.search(word):  # never echo legal/safety words back publicly
            continue
        if len(word) > len(best):
            best = word
    return best.lower()


# --------------------------------------------------------------------------- #
# 1. review link
# --------------------------------------------------------------------------- #
def review_link(place_id):
    """Build the official Google "write a review" URL for a Place ID."""
    pid = place_id.strip() if isinstance(place_id, str) else ""
    if not _PLACE_ID_RE.match(pid):
        raise ToolError(
            scrub(f"invalid place_id {place_id!r}: need 10+ chars of A-Z a-z 0-9 _ -", 200)
        )
    return f"https://search.google.com/local/writereview?placeid={pid}"


# --------------------------------------------------------------------------- #
# 2. review-request drafts (never sent)
# --------------------------------------------------------------------------- #
_FB_WARM = (
    "Hi {first}, thanks for choosing {biz}. A quick review helps: {link}",
    "Hi {first}, a quick review of {biz} helps: {link}",
    "Hi {first}: {link}",
)
_FB_DIRECT = (
    "Hi {first}, thanks for your business. A quick review helps: {link}",
    "Hi {first}, a quick review would help us: {link}",
    "Hi {first}, please review us here: {link}",
)


def _fit_sms(template, fallbacks, first, biz, svc, link):
    """Render an SMS variant, degrading gracefully until it fits 300 chars."""
    for tmpl in (template,) + tuple(fallbacks):
        msg = tmpl.format(first=first, biz=biz, svc=svc, link=link) + _SMS_TAIL
        if len(msg) <= _SMS_LIMIT:
            return msg
    raise ToolError("cannot fit a review request into 300 SMS characters; use a shorter link")


def _email_parts(first, biz, svc, link, tone, variant):
    if variant == 1:
        subject = f"How did we do, {first}?"
        core = (
            f"Thank you for choosing {biz} for your {svc}. If you had a good "
            f"experience, a quick review would help other local customers find us."
        )
    else:
        subject = f"Would you review {biz}?"
        core = (
            f"Thanks for your recent {svc} with {biz}. Would you take 30 seconds "
            f"to share a quick review?"
        )
    if tone == "brief":
        subject = f"Quick review for {biz}?" if variant == 1 else f"Review {biz}?"
        core = (
            f"Thanks for choosing {biz}. A quick review of your {svc} helps:"
            if variant == 1
            else f"Thanks for your {svc}. A quick review helps:"
        )
    body = (
        f"Hi {first},\n\n{core}\n\n{link}\n\n"
        f"Thank you,\nThe {biz} team\n\n"
        f"Reply 'unsubscribe' to stop these emails."
    )
    return subject, body


def request_drafts(biz_name, customer_first_name, service, link, channel="sms", tone="friendly"):
    """Return two ready-to-review request drafts plus the policy rules to follow."""
    biz = _clean(biz_name, 60)
    first = _clean(customer_first_name, 24)
    svc = _clean(service, 40)
    if not biz or not first or not svc:
        raise ToolError("biz_name, customer_first_name and service are required")
    if channel not in ("sms", "email"):
        raise ToolError("channel must be 'sms' or 'email'")
    if tone not in ("friendly", "brief"):
        raise ToolError("tone must be 'friendly' or 'brief'")
    url = link.strip() if isinstance(link, str) else ""
    if not _URL_RE.match(url) or len(url) > 500:
        raise ToolError(scrub(f"link is not a valid http(s) URL: {link!r}", 200))
    if not _GOOGLE_RE.search(url):
        # Rail: sending customers to third-party review-gating pages is not allowed.
        raise RailViolation("only Google review links are allowed; third-party review links are refused")

    drafts = []
    if channel == "sms":
        if tone == "friendly":
            templates = (
                ("Hi {first}, it was a pleasure helping you with your {svc}! If you have 30 "
                 "seconds, a quick review of {biz} would mean a lot to our team: {link}", _FB_WARM),
                ("Hi {first}, thanks for choosing {biz} for your {svc}. Would you share a "
                 "quick review of how it went? {link}", _FB_DIRECT),
            )
        else:
            templates = (
                ("Hi {first}, thanks for choosing {biz}. A quick review of your {svc} "
                 "helps us a lot: {link}", _FB_WARM),
                ("Hi {first}, thanks for your {svc} with {biz}. Leave a quick review "
                 "here: {link}", _FB_DIRECT),
            )
        for tmpl, fallbacks in templates:
            msg = _fit_sms(tmpl, fallbacks, first, biz, svc, url)
            drafts.append({"channel": "sms", "text": msg, "chars": len(msg)})
    else:
        for variant in (1, 2):
            subject, body = _email_parts(first, biz, svc, url, tone, variant)
            drafts.append({"channel": "email", "subject": subject, "body": body, "chars": len(body)})

    return {"drafts": drafts, "rules": list(_RULES)}


# --------------------------------------------------------------------------- #
# 3. reply drafts
# --------------------------------------------------------------------------- #
def reply_draft(review, biz_name, owner_name=None):
    """Draft a public owner reply. Never uses reviewer PII, never admits fault."""
    if not isinstance(review, dict):
        raise ToolError("review must be an object with rating/text/author_first_name")
    rating = _int(review.get("rating"), -1)
    if not 1 <= rating <= 5:
        raise ToolError("review.rating must be an integer between 1 and 5")
    raw_text = review.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    biz = _clean(biz_name, 60) or "our business"
    owner = _clean(owner_name, 40) if owner_name else ""

    flags = []
    if rating == 1 and not text:
        flags.append("possible_fake")  # empty one-star reviews are often drive-by/competitor
    if _SAFETY_RE.search(text):
        flags.append("mentions_safety_or_legal")
        flags.append("owner_review_personally")

    if rating >= 4:
        tone = "grateful"
        word = _detail_word(text)  # only one concrete detail, only if the reviewer wrote it
        if word:
            draft = (
                f"Thank you for taking the time to review {biz} — we're glad the {word} "
                f"stood out. It means a lot to our team to hear you had a good experience. "
                f"We look forward to seeing you again."
            )
        else:
            draft = (
                f"Thank you for taking the time to review {biz}. It means a lot to our team "
                f"to hear you had a good experience, and we're glad you chose us. "
                f"We look forward to seeing you again."
            )
    elif rating == 3:
        tone = "helpful"
        draft = (
            f"Thank you for your honest feedback about {biz}. We'd like your next visit to "
            f"be a better one — please contact us directly so we can help. "
            f"We appreciate your business."
        )
    else:
        tone = "calm"
        # No liability admission, no job details, no PII — just a calm offline invitation.
        draft = (
            f"Thank you for sharing your feedback about {biz}. We would like to learn more "
            f"about what happened so we can improve. Please contact us directly so we can "
            f"discuss this privately — we would welcome the chance to listen."
        )

    if owner:
        draft += f"\n\n— {owner}, {biz}"
    return {"draft": _clip(draft), "tone": tone, "flags": flags}


# --------------------------------------------------------------------------- #
# 4. velocity / health math
# --------------------------------------------------------------------------- #
def velocity(reviews, today=None, competitors=None, targets=(4.5, 4.8)):
    """Review count, average, recency, gap-to-target math and competitor gaps."""
    rows = [r for r in (reviews or []) if isinstance(r, dict)]
    ref = _ref_date(today)

    rated, dates = [], []
    for row in rows:
        rating = _int(row.get("rating"), 0)
        if 1 <= rating <= 5:
            rated.append(rating)
        dt = _parse_dt(row.get("created_at"))
        if dt is not None:
            dates.append(dt)

    count = len(rows)
    n = len(rated)
    total = sum(rated)
    avg = round(total / n, 2) if n else 0.0
    last_30d = sum(1 for d in dates if (ref - d.date()).days < 30)
    last_90d = sum(1 for d in dates if (ref - d.date()).days < 90)
    per_month_90d = round(last_90d / 3.0, 2)

    if dates:
        days_since_last = max(0, (ref - max(dates).date()).days)
    else:
        days_since_last = None
    if days_since_last is None:
        recency_flag = "no_reviews"
    elif days_since_last > 30:
        recency_flag = "stale"
    else:
        recency_flag = "ok"

    # needed_to_reach: exact integer math.  k 5-star reviews must satisfy
    # (S + 5k) / (n + k) >= p/q  ->  k >= (p*n - q*S) / (5q - p).
    needed = {}
    for target in targets or ():
        try:
            frac = Fraction(float(target)).limit_denominator(1000)
        except (TypeError, ValueError):
            continue
        key = f"{float(target):g}"
        if n == 0:
            needed[key] = 0
            continue
        if frac > 5:
            needed[key] = None
        elif frac == 5:
            needed[key] = 0 if total == 5 * n else None
        else:
            num = frac.numerator * n - frac.denominator * total
            den = 5 * frac.denominator - frac.numerator
            needed[key] = max(0, -((-num) // den)) if den > 0 else None

    vs = []
    for comp in competitors or []:
        if not isinstance(comp, dict):
            continue
        vs.append({
            "name": _clean(comp.get("name"), 60),
            "gap_count": _int(comp.get("count"), 0) - count,   # >0 => competitor has more
            "gap_rating": round(_num(comp.get("rating"), 0.0) - avg, 2),
        })

    advice = []
    if count == 0:
        advice.append("No reviews yet: ask every customer, starting with your last 20 jobs.")
    if days_since_last is not None and days_since_last > 30:
        advice.append(
            f"Last review was {days_since_last} days ago — request one within 24h of each job."
        )
    if n and avg < 4.5:
        advice.append(f"Average rating {avg:.2f} is below 4.5; steady 5-star asks will lift it.")
    if vs:
        worst = max(vs, key=lambda v: v["gap_count"])
        if worst["gap_count"] > 0 and worst["name"]:
            advice.append(
                f"{worst['name']} has {worst['gap_count']} more reviews; weekly requests close the gap."
            )
    if count and days_since_last is not None and days_since_last <= 30:
        advice.append("Momentum looks healthy — keep asking every customer and reply to every review.")
    if not advice:
        advice.append("Ask every customer for a review (no gating) and reply to each one.")

    return {
        "count": count,
        "rating": avg,
        "last_30d": last_30d,
        "last_90d": last_90d,
        "per_month_90d": per_month_90d,
        "days_since_last": days_since_last,
        "recency_flag": recency_flag,
        "needed_to_reach": needed,
        "vs_competitors": vs,
        "advice": advice,
    }


# --------------------------------------------------------------------------- #
# 5. Business Profile API v4 normaliser
# --------------------------------------------------------------------------- #
def parse_gbp_reviews(api_rows):
    """Normalise GBP v4 review rows to flat JSON-safe dicts."""
    out = []
    for row in api_rows or []:
        if not isinstance(row, dict):
            continue  # malformed entries are dropped, never crash a tool call
        raw_star = row.get("starRating")
        if isinstance(raw_star, str) and raw_star.strip().upper() in _STAR_WORDS:
            rating = _STAR_WORDS[raw_star.strip().upper()]
        else:
            rating = _int(raw_star, 0)
        name = _g(row, "reviewer", "displayName", default="") or ""
        tokens = str(name).split()
        reply = _g(row, "reviewReply", "comment", default=None)
        out.append({
            "id": str(row.get("reviewId") or ""),
            "rating": rating,
            "text": str(row.get("comment") or "").strip(),
            "created_at": str(row.get("createTime") or ""),
            "author_first_name": tokens[0] if tokens else "",
            "replied": bool(reply and str(reply).strip()),
        })
    return out


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def selftest() -> int:
    """Run every code path with no I/O. Returns the number of checks."""
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(msg)

    today = "2024-06-01"

    # --- review_link ------------------------------------------------------ #
    link = review_link("ChIJN1t_tDeuEmsRUsoyG83frY4")
    ok(link == "https://search.google.com/local/writereview?placeid=ChIJN1t_tDeuEmsRUsoyG83frY4",
       "review_link builds the exact URL")
    for bad in ("short", "has space here", "", "abc!defghijkl", None):
        try:
            review_link(bad)
            ok(False, f"bad place id accepted: {bad!r}")
        except ToolError:
            ok(True, "bad place id rejected")

    # --- request_drafts: sms --------------------------------------------- #
    res = request_drafts("Bright Smile Dental", "Ana", "cleaning", link, channel="sms", tone="friendly")
    ok(len(res["drafts"]) == 2, "two sms variants returned")
    for draft in res["drafts"]:
        ok(len(draft["text"]) <= 300, f"sms length {len(draft['text'])}")
        ok(draft["text"].count(link) == 1, "link appears exactly once")
        ok("Ana" in draft["text"], "first name present")
        ok("Reply STOP to opt out" in draft["text"], "sms opt-out present")
        ok(not _EMOJI_RE.search(draft["text"]), "no emojis in sms")
    ok(len({d["text"] for d in res["drafts"]}) == 2, "sms variants differ")
    rules = " ".join(res["rules"]).lower()
    ok("every customer" in rules, "rule: ask every customer")
    ok("gate" in rules or "filter" in rules, "rule: no review gating")
    ok("incentive" in rules, "rule: no incentives")
    ok("24 hours" in rules, "rule: send within 24h")
    ok("3 days" in rules, "rule: one reminder after 3 days")

    # --- request_drafts: email ------------------------------------------- #
    email = request_drafts("Bright Smile Dental", "Ana", "cleaning", link, channel="email", tone="brief")
    ok(len(email["drafts"]) == 2, "two email variants")
    ok(all("subject" in d and "body" in d for d in email["drafts"]), "email has subject+body")
    ok(email["drafts"][0]["body"].count(link) == 1, "email link once")

    # --- request_drafts: validation / rails ------------------------------ #
    for kwargs in ({"channel": "push"}, {"tone": "rude"}):
        try:
            request_drafts("B", "Ana", "svc", link, **kwargs)
            ok(False, f"bad option accepted: {kwargs}")
        except ToolError:
            ok(True, "bad channel/tone rejected")
    try:
        request_drafts("B", "Ana", "svc", "https://example.com/review", channel="sms")
        ok(False, "third-party review link accepted")
    except RailViolation:
        ok(True, "third-party review link refused")

    long_link = review_link("A" * 60)
    long_sms = request_drafts("B", "Ana", "cleaning", long_link, channel="sms", tone="friendly")
    ok(all(len(d["text"]) <= 300 for d in long_sms["drafts"]), "long link still fits sms")

    # --- reply_draft ------------------------------------------------------ #
    r5 = reply_draft(
        {"rating": 5, "text": "The fireplace crew was fantastic and fast", "author_first_name": "Jane Doe"},
        "Bright Smile", owner_name="Sam",
    )
    ok(r5["tone"] == "grateful", "5-star -> grateful tone")
    ok("fireplace" in r5["draft"].lower(), "5-star thanks the concrete detail")
    ok("Jane" not in r5["draft"] and "Doe" not in r5["draft"], "no reviewer name in reply")
    ok(len(r5["draft"]) <= 600, "reply <= 600 chars")
    ok("Sam" in r5["draft"], "owner signature present")

    r5b = reply_draft({"rating": 5, "text": "", "author_first_name": "Ana"}, "Bright Smile")
    ok("stood out" not in r5b["draft"].lower(), "no invented detail without text")

    r1 = reply_draft({"rating": 1, "text": "", "author_first_name": "Anon"}, "Bright Smile")
    ok("possible_fake" in r1["flags"], "empty 1-star flagged possible_fake")
    ok(r1["tone"] == "calm", "1-star -> calm tone")

    r2 = reply_draft(
        {"rating": 2,
         "text": "I slipped, have an injury and my attorney called. Ring 555-123-4567.",
         "author_first_name": "Bob Smith"},
        "Bright Smile",
    )
    ok("mentions_safety_or_legal" in r2["flags"], "safety/legal flag raised")
    ok("555" not in r2["draft"] and "attorney" not in r2["draft"].lower(), "no pii/job detail echoed")
    ok("Bob" not in r2["draft"] and "Smith" not in r2["draft"], "no reviewer name in low reply")
    ok("fault" not in r2["draft"].lower() and "negligent" not in r2["draft"].lower(),
       "no admission of liability")

    r3 = reply_draft({"rating": 3, "text": "It was okay, a bit slow.", "author_first_name": "Cy"},
                     "Bright Smile")
    ok("contact us directly" in r3["draft"].lower(), "3-star invites offline contact")
    ok(r3["tone"] == "helpful", "3-star -> helpful tone")

    try:
        reply_draft({"rating": 0, "text": "x"}, "B")
        ok(False, "invalid rating accepted")
    except ToolError:
        ok(True, "invalid rating rejected")

    # --- velocity --------------------------------------------------------- #
    reviews = [{"rating": 4, "created_at": "2024-05-20T10:00:00Z"} for _ in range(8)]
    reviews += [{"rating": 5, "created_at": "2024-04-01T10:00:00Z"} for _ in range(2)]
    v = velocity(reviews, today=today)
    ok(v["count"] == 10, "velocity count")
    ok(v["rating"] == 4.2, "velocity average 4.2")
    ok(v["last_30d"] == 8, "last_30d window")
    ok(v["last_90d"] == 10, "last_90d window")
    ok(v["per_month_90d"] == 3.33, "per_month_90d")
    ok(v["days_since_last"] == 12, "days_since_last")
    ok(v["recency_flag"] == "ok", "fresh reviews are not stale")
    ok(v["needed_to_reach"]["4.5"] == 6, f"10 @ 4.2 needs 6 for 4.5, got {v['needed_to_reach']}")
    ok(v["needed_to_reach"]["4.8"] == 30, "10 @ 4.2 needs 30 for 4.8")

    v_stale = velocity([{"rating": 5, "created_at": "2024-01-01T00:00:00Z"}], today=today)
    ok(v_stale["recency_flag"] == "stale", "old review -> stale")

    v0 = velocity([], today=today)
    ok(v0["count"] == 0 and v0["rating"] == 0.0 and v0["days_since_last"] is None,
       "empty reviews handled")
    ok(v0["needed_to_reach"]["4.5"] == 0 and v0["vs_competitors"] == [],
       "empty reviews -> 0 needed, no competitors")

    v5 = velocity(reviews, today=today, targets=(5.0,))
    ok(v5["needed_to_reach"]["5"] is None, "5.0 impossible with a 4-star present")
    v5b = velocity([{"rating": 5, "created_at": "2024-05-01"}] * 3, today=today, targets=(5.0,))
    ok(v5b["needed_to_reach"]["5"] == 0, "5.0 already reached by all 5-stars")

    vc = velocity(reviews, today=today,
                  competitors=[{"name": "Rival Dental", "count": 25, "rating": 4.6}, "junk"])
    ok(len(vc["vs_competitors"]) == 1, "malformed competitor skipped")
    ok(vc["vs_competitors"][0]["gap_count"] == 15, "gap_count 25-10")
    ok(vc["vs_competitors"][0]["gap_rating"] == 0.4, "gap_rating 4.6-4.2")

    vbad = velocity([{"rating": "x", "created_at": "nope"}, {"rating": 5, "created_at": "2024-05-30"}],
                    today=today)
    ok(vbad["count"] == 2 and vbad["rating"] == 5.0, "malformed rating/date tolerated")

    # --- parse_gbp_reviews ------------------------------------------------ #
    rows = [
        {"reviewId": "r1", "starRating": "FIVE", "comment": "Great pizza",
         "createTime": "2024-05-01T00:00:00Z",
         "reviewer": {"displayName": "Jane Doe"}, "reviewReply": {"comment": "Thanks!"}},
        {"reviewId": "r2", "starRating": "ONE", "comment": "",
         "createTime": "2024-04-01T00:00:00Z", "reviewer": {"displayName": "Bob"}},
        {"starRating": "4", "reviewer": {"displayName": "Cy"}},
        "garbage",
        None,
    ]
    parsed = parse_gbp_reviews(rows)
    ok(len(parsed) == 3, "malformed rows skipped")
    ok(parsed[0]["rating"] == 5 and parsed[0]["replied"] is True, "FIVE -> 5 and replied")
    ok(parsed[0]["author_first_name"] == "Jane", "full display name trimmed to first token")
    ok(parsed[1]["rating"] == 1 and parsed[1]["replied"] is False, "ONE -> 1, not replied")
    ok(parsed[2]["rating"] == 4 and parsed[2]["author_first_name"] == "Cy", "numeric star coerced")
    ok(parse_gbp_reviews([]) == [] and parse_gbp_reviews(None) == [], "empty/None -> []")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
