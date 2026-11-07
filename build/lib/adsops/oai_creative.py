"""ChatGPT Ads chat_card creative checker for AdRails (pure, offline).

Validates a chat_card creative (title / body / destination URL / images) against
OpenAI's documented limits and operator policy (allowed domains, required UTM
parameters, banned phrases, brand mention), appends UTM tracking parameters, and
screens whole A/B creative sets for duplicates.

Safety properties: no network, subprocess or file I/O at import time or inside
any function; no secrets/PII are echoed back; every function returns plain
JSON-serialisable data (no sets, datetimes or handles). Callers must inspect
`ok` / `errors` before pushing a creative to the ads API — this module never
mutates the ad account. Use `add_utm` to build trackable destinations and
`check_set` to vet a batch before upload.
"""

import json
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from adsops.errors import ToolError, RailViolation, scrub

# Documented chat_card limits (OpenAI Ads docs).
TITLE_MIN, TITLE_MAX = 3, 50
BODY_MAX = 100
URL_MAX = 2048
IMAGE_COUNTS = (1, 4, 5, 6)  # 1 hero image, or a 4-6 image carousel
CAROUSEL_IMAGE_COUNTS = (4, 5, 6)

# Best-practice warnings are ours, not OpenAI policy; say so in every message.
_BP_NOTE = " (best-practice advice, not an OpenAI policy limit)"

_SUPERLATIVE = re.compile(r"(?<![\w#])(best|#1|number one|cheapest|guaranteed)(?!\w)")
_CTA = re.compile(r"\b(get|see|book|call|start|compare|find|request|try)\b", re.I)
_CAPS_WORD = re.compile(r"\b[A-Z]{4,}\b")
_PHONE = re.compile(r"\+?\(?\d[\d\s().-]{5,}\d")

POLICY_KEYS = ("allowed_domains", "required_utm", "banned_phrases", "brand")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Tolerant nested getter: _g(row, "campaign", "id") -> value or default."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _text(v):
    """Coerce an ad field to str defensively (missing/None -> '')."""
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _norm(s):
    """Case- and whitespace-insensitive form used for duplicate detection."""
    return " ".join(_text(s).lower().split())


def _err(errors, code, field, message):
    errors.append({"code": code, "field": field, "message": message})


def _warn(code, field, message):
    return {"code": code, "field": field, "message": message + _BP_NOTE}


def _has_phone(text):
    """True when the text holds a phone-like run with >= 7 digits."""
    for m in _PHONE.finditer(text):
        if sum(ch.isdigit() for ch in m.group(0)) >= 7:
            return True
    return False


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def validate_card(ad, policy=None):
    """Validate one chat_card. Returns {"ok", "errors", "warnings", "stats"}."""
    ad = ad if isinstance(ad, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    errors, warnings = [], []

    title = _text(_g(ad, "title")).strip()
    body = _text(_g(ad, "body")).strip()
    url = _text(_g(ad, "target_url")).strip()

    # --- required text + documented length limits (measured on stripped text)
    if not title:
        _err(errors, "empty_text", "title", "title is required")
    elif not (TITLE_MIN <= len(title) <= TITLE_MAX):
        _err(errors, "title_length", "title",
             f"title must be {TITLE_MIN}-{TITLE_MAX} characters (got {len(title)})")
    if not body:
        _err(errors, "empty_text", "body", "body is required")
    elif len(body) > BODY_MAX:
        _err(errors, "body_length", "body",
             f"body must be <= {BODY_MAX} characters (got {len(body)})")

    # --- destination URL
    parts = None
    if not url:
        _err(errors, "url_scheme", "target_url",
             "destination URL is required and must use https")
    else:
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
        except ValueError:
            parts, scheme = None, ""
        if scheme != "https":
            _err(errors, "url_scheme", "target_url", "destination URL must use https")
        if len(url) > URL_MAX:
            _err(errors, "url_length", "target_url",
                 f"destination URL must be <= {URL_MAX} characters (got {len(url)})")
        allowed = [str(d).strip().lower().rstrip(".")
                   for d in (policy.get("allowed_domains") or []) if str(d).strip()]
        if allowed and parts is not None:
            try:
                host = (parts.hostname or "").lower().rstrip(".")
            except ValueError:
                host = ""
            # Exact host match only: subdomains must be listed explicitly.
            if host not in allowed:
                _err(errors, "url_domain", "target_url",
                     f"destination host {host or '(none)'!r} is not in the allowed domain list")

    # --- required UTM parameters (exact key/value match)
    required = policy.get("required_utm") or {}
    if required and url and parts is not None:
        got = parse_qs(parts.query, keep_blank_values=True)
        for key, want in required.items():
            vals = got.get(str(key)) or []
            if str(want) not in vals:
                _err(errors, "missing_utm", "target_url",
                     f"required UTM parameter {str(key)!r} must equal {str(want)!r}")

    # --- images: either exactly one file_id, or 4-6 file_ids
    solo = _g(ad, "file_id")
    raw = _g(ad, "file_ids")
    has_solo = bool(solo)
    list_ids = []
    list_error = False
    if raw is None:
        list_ids = []
    elif isinstance(raw, (list, tuple)):
        list_ids = [x for x in raw if x]
    else:
        _err(errors, "images", "file_ids", "file_ids must be a list of image file ids")
        list_error = True
    if not list_error:
        if has_solo:
            if list_ids:
                _err(errors, "images", "file_ids",
                     "a chat_card needs either exactly one file_id or 4-6 file_ids; "
                     "do not provide both")
        else:
            if len(list_ids) not in CAROUSEL_IMAGE_COUNTS:
                _err(errors, "images", "file_ids",
                     "a chat_card needs either exactly one file_id or 4-6 file_ids"
                     f" (got {len(list_ids)})")

    # --- banned phrases (policy)
    haystack = f"{title}\n{body}".lower()
    for phrase in policy.get("banned_phrases") or []:
        p = str(phrase).strip().lower()
        if p and p in haystack:
            _err(errors, "banned_phrase", "title/body",
                 f"contains banned phrase {str(phrase)!r}")

    # --- best-practice warnings (never block a push by themselves)
    text_all = f"{title} {body}"
    caps = sorted(set(_CAPS_WORD.findall(text_all)))
    if caps:
        warnings.append(_warn("all_caps_word", "title/body",
                              f"all-caps words read as shouting: {', '.join(caps)}"))
    if text_all.count("!") > 1:
        warnings.append(_warn("exclamation", "title/body",
                              "more than one exclamation mark"))
    sup = sorted({m.group(0) for m in _SUPERLATIVE.finditer(text_all.lower())})
    if sup:
        warnings.append(_warn("superlative", "title/body",
                              f"superlative claims need substantiation: {', '.join(sup)}"))
    if _has_phone(text_all):
        warnings.append(_warn("phone_in_text", "title/body",
                              "phone numbers in creative text are hard to track"))
    brand = _text(policy.get("brand")).strip()
    if brand and brand.lower() not in f"{title} {body}".lower():
        warnings.append(_warn("no_brand", "title/body",
                              f"brand {brand!r} is not mentioned in title or body"))
    if title and body and _norm(title) == _norm(body):
        warnings.append(_warn("title_equals_body", "title/body",
                              "title and body are identical"))
    if title.endswith("?") and not _CTA.search(body):
        warnings.append(_warn("question_title_without_answer", "body",
                              "title asks a question but the body has no call to action"))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {"title_len": len(title), "body_len": len(body)},
    }


def add_utm(url, utm):
    """Merge UTM query parameters into an https URL, keeping other params + fragment."""
    if not isinstance(url, str) or not url.strip():
        raise ToolError("add_utm requires a non-empty destination URL")
    url = url.strip()
    if not isinstance(utm, dict):
        raise ToolError("utm must be a dict of query parameters")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ToolError(scrub(f"invalid destination URL: {url}", 300)) from None
    if parts.scheme.lower() != "https":
        # Safety rail: never attach tracking to a plaintext destination.
        raise RailViolation("refusing to add tracking parameters to a non-https URL")
    extra = {}
    for k, v in utm.items():
        if not isinstance(k, str) or not k.strip():
            raise ToolError("utm keys must be non-empty strings")
        extra[k.strip()] = "" if v is None else str(v)
    if not extra:
        return url
    merged, seen = [], set()
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k in extra:
            if k in seen:  # collapse duplicate occurrences to one value
                continue
            seen.add(k)
            merged.append((k, extra[k]))
        else:
            merged.append((k, v))
    for k, v in extra.items():
        if k not in seen:
            merged.append((k, v))
    query = urlencode(merged)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def check_set(ads, policy=None):
    """Screen an A/B creative set: per-ad results, duplicate pairs, title diversity."""
    ads = list(ads or [])
    per_ad = []
    for i, ad in enumerate(ads):
        res = validate_card(ad, policy)
        per_ad.append({"index": i, **res})

    titles = [_norm(_g(ad if isinstance(ad, dict) else {}, "title")) for ad in ads]
    bodies = [_norm(_g(ad if isinstance(ad, dict) else {}, "body")) for ad in ads]

    duplicates = []
    seen_pairs = set()
    for i in range(len(ads)):
        for j in range(i + 1, len(ads)):
            is_duplicate = False
            if titles[i] and titles[i] == titles[j]:
                is_duplicate = True
            if bodies[i] and bodies[i] == bodies[j]:
                is_duplicate = True
            if is_duplicate:
                pair = (i, j)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    duplicates.append([i, j])

    first_words = {t.split(" ")[0] for t in titles if t}
    return {
        "ok": all(r["ok"] for r in per_ad) and not duplicates,
        "per_ad": per_ad,
        "duplicates": duplicates,
        "diversity": len(first_words),
    }


# --------------------------------------------------------------------------- #
# self-test (all I/O mocked: this module performs none)
# --------------------------------------------------------------------------- #
def selftest() -> int:
    n = [0]

    def chk(cond, msg="assertion failed"):
        assert cond, msg
        n[0] += 1

    def codes(res, key="errors"):
        return [e["code"] for e in res[key]]

    url_ok = "https://shop.example.com/shoes?utm_source=chatgpt&utm_medium=cpc"
    policy = {
        "allowed_domains": ["shop.example.com"],
        "required_utm": {"utm_source": "chatgpt", "utm_medium": "cpc"},
        "banned_phrases": ["free money", "cure"],
        "brand": "Acme",
    }

    def ad(**kw):
        base = {"title": "Acme Shoes", "body": "Get yours today.",
                "target_url": url_ok, "file_id": "file-1"}
        base.update(kw)
        return base

    # happy path + stats
    res = validate_card(ad(), policy)
    chk(res["ok"] is True, f"good card should pass: {res}")
    chk(res["errors"] == [] and res["warnings"] == [], f"unexpected findings: {res}")
    chk(res["stats"] == {"title_len": 10, "body_len": 16}, res["stats"])
    chk(validate_card(ad(), None)["ok"] is True, "no policy -> no domain/utm checks")
    chk(isinstance(json.dumps(validate_card(ad(), policy)), str), "result is JSON-safe")

    # title / body boundaries
    chk("title_length" in codes(validate_card(ad(title="ab"), policy)), "2-char title fails")
    chk("title_length" not in codes(validate_card(ad(title="abc"), policy)), "3-char title ok")
    chk("title_length" not in codes(validate_card(ad(title="x" * 50), policy)), "50-char title ok")
    chk("title_length" in codes(validate_card(ad(title="x" * 51), policy)), "51-char title fails")
    chk("body_length" not in codes(validate_card(ad(body="y" * 100), policy)), "100-char body ok")
    chk("body_length" in codes(validate_card(ad(body="y" * 101), policy)), "101-char body fails")

    # empty text (no double-reported length error)
    res = validate_card({"title": "", "body": "  ", "target_url": url_ok,
                         "file_id": "f"}, policy)
    chk(codes(res).count("empty_text") == 2, res["errors"])
    chk("title_length" not in codes(res), "empty title must not also report title_length")

    # URL checks
    http_url = "http://shop.example.com/s?utm_source=chatgpt&utm_medium=cpc"
    chk(codes(validate_card(ad(target_url=http_url), policy)) == ["url_scheme"],
        "http destination rejected")
    long_url = "https://shop.example.com/" + "a" * 2100 + "?utm_source=chatgpt&utm_medium=cpc"
    chk(codes(validate_card(ad(target_url=long_url), policy)) == ["url_length"],
        "over-long URL rejected")
    chk(codes(validate_card({"title": "Acme", "body": "Hi.", "file_id": "f"}, policy))
        == ["url_scheme"], "missing URL rejected")
    chk(codes(validate_card(ad(target_url="https://evil.example.org/x?utm_source=chatgpt"
                                         "&utm_medium=cpc"), policy)) == ["url_domain"],
        "foreign domain rejected")
    chk(codes(validate_card(ad(target_url="https://www.shop.example.com/x?utm_source=chatgpt"
                                         "&utm_medium=cpc"), policy)) == ["url_domain"],
        "unlisted subdomain rejected")
    open_policy = dict(policy, allowed_domains=[])
    chk(validate_card(ad(target_url="https://evil.example.org/x?utm_source=chatgpt"
                                    "&utm_medium=cpc"), open_policy)["ok"] is True,
        "empty allow-list disables the domain check")

    # UTM requirements
    chk("missing_utm" in codes(validate_card(
        ad(target_url="https://shop.example.com/x?utm_source=chatgpt"), policy)),
        "missing utm_medium flagged")
    chk("missing_utm" in codes(validate_card(
        ad(target_url="https://shop.example.com/x?utm_source=google&utm_medium=cpc"),
        policy)), "wrong utm value flagged")

    # image rules
    chk("images" in codes(validate_card(ad(file_id=None), policy)), "zero images flagged")
    chk("images" in codes(validate_card(ad(file_id=None, file_ids=["a", "b"]), policy)),
        "two images flagged")
    chk("images" in codes(validate_card(ad(file_id=None, file_ids=["a"]), policy)),
        "one plural file_id flagged")
    chk("images" not in codes(validate_card(ad(file_id=None, file_ids=list("abcd")), policy)),
        "four images ok")
    chk("images" in codes(validate_card(ad(file_id="f", file_ids=list("abc")), policy)),
        "both singular and list ids flagged")
    chk("images" in codes(validate_card(ad(file_id=None, file_ids=list("abcdefg")), policy)),
        "seven images flagged")

    # banned phrases / malformed input tolerance
    chk("banned_phrase" in codes(validate_card(ad(body="Get free money today."), policy)),
        "banned phrase flagged")
    chk("banned_phrase" not in codes(validate_card(ad(), policy)), "clean copy is clean")
    res = validate_card({"title": 5, "body": None, "target_url": None,
                         "file_ids": "nope"}, policy)
    chk("empty_text" in codes(res), "non-string / missing fields are tolerated")

    # best-practice warnings
    chk("all_caps_word" in codes(validate_card(ad(body="SAVE big with Acme."), policy),
                                 "warnings"), "all-caps warning")
    chk("superlative" in codes(validate_card(ad(body="Acme is the best."), policy),
                               "warnings"), "superlative warning")
    chk("exclamation" in codes(validate_card(ad(body="Wow! Amazing! Get Acme."), policy),
                               "warnings"), "double exclamation warning")
    chk("phone_in_text" in codes(validate_card(ad(body="Call 555-123-4567 for Acme."),
                                               policy), "warnings"), "phone warning")
    chk("no_brand" in codes(validate_card(ad(title="Running Shoes"), policy),
                            "warnings"), "missing brand warning")
    chk("title_equals_body" in codes(validate_card(
        ad(title="Acme Shoes", body="acme   shoes"), policy), "warnings"),
        "identical title/body warning")
    chk("question_title_without_answer" in codes(validate_card(
        ad(title="Need new shoes?", body="Acme shoes for all."), policy), "warnings"),
        "question without CTA warned")
    chk("question_title_without_answer" not in codes(validate_card(
        ad(title="Need new shoes?", body="Get Acme shoes now."), policy), "warnings"),
        "question with CTA not warned")
    res = validate_card(ad(body="Acme is the best."), policy)
    chk("best-practice" in res["warnings"][0]["message"], "warnings disclose they are advice")

    # add_utm
    chk(add_utm("https://shop.example.com/p?a=1#frag",
                {"utm_source": "chatgpt", "utm_medium": "cpc"})
        == "https://shop.example.com/p?a=1&utm_source=chatgpt&utm_medium=cpc#frag",
        "merge keeps params and fragment")
    chk(add_utm("https://shop.example.com/p?utm_source=old", {"utm_source": "chatgpt"})
        == "https://shop.example.com/p?utm_source=chatgpt", "utm values are overridden")
    chk(add_utm("https://shop.example.com/p", {}) == "https://shop.example.com/p",
        "empty utm dict is a no-op")
    try:
        add_utm("http://shop.example.com/p", {"utm_source": "chatgpt"})
        raise AssertionError("http destination should be refused")
    except RailViolation:
        n[0] += 1
    try:
        add_utm("", {"utm_source": "chatgpt"})
        raise AssertionError("empty url should raise ToolError")
    except ToolError:
        n[0] += 1

    # check_set
    ads = [ad(), ad(title="acme   shoes"), ad(title="Trail Runners",
                                              body="Run further with Acme.")]
    st = check_set(ads, policy)
    chk([p["index"] for p in st["per_ad"]] == [0, 1, 2], "per_ad keeps indexes")
    chk(st["ok"] is False, "duplicate titles make the set not ok")
    chk([0, 1] in st["duplicates"], st["duplicates"])
    chk(st["diversity"] == 2, st["diversity"])
    chk(check_set([], policy) == {"ok": True, "per_ad": [], "duplicates": [],
                                  "diversity": 0}, "empty set is ok")
    chk(isinstance(json.dumps(check_set(ads, policy)), str), "check_set is JSON-safe")
    return n[0]


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)