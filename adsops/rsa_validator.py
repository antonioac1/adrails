"""Responsive Search Ad (RSA) draft validator for AdRails.

Validates a Google Ads RSA draft against Google's hard limits *and* AdRails' ad
policy before any mutate call is made, so an unsafe draft can never reach a live
account. Character counts follow Google's rule (CJK/full-width glyphs count as 2),
all comparisons are case- and whitespace-insensitive, and every finding is a plain
JSON record `{"code", "field", "message"}` - never an exception, never raw PII.

A tool should call `validate_rsa(ad, policy)`, refuse to write when
`result["ok"]` is False, and use `suggest_fixes(result)` to tell the operator how
to repair the draft. The module performs no I/O and has no import-time effects.
"""

from __future__ import annotations

import re
import unicodedata
from itertools import combinations
from urllib.parse import urlsplit

from adsops.errors import RailViolation, ToolError, scrub  # noqa: F401  (RailViolation re-exported for tools)

HEADLINE_SLOTS = ("HEADLINE_1", "HEADLINE_2", "HEADLINE_3")
DESCRIPTION_SLOTS = ("DESCRIPTION_1", "DESCRIPTION_2")
MAX_HEADLINE = 30
MAX_DESCRIPTION = 90
MAX_PATH = 15
MAX_PINS_PER_SLOT = 2
MIN_HEADLINES, MAX_HEADLINES = 3, 15
MIN_DESCRIPTIONS, MAX_DESCRIPTIONS = 2, 4

CTA_VERBS = ("call", "get", "book", "start", "request", "schedule",
             "compare", "see", "try", "contact")
SUPERLATIVES = ("best", "#1", "number one", "cheapest", "guaranteed")

REPEAT_RE = re.compile(r"([A-Za-z])\1{2,}")          # "Freeee", "sooo"
CAPS_RE = re.compile(r"\b[A-Z]{4,}\b")                # ALL-CAPS words, 4+ letters
PHONE_RE = re.compile(r"\+?\d[\d\-.\s()]{6,}\d")      # candidate digit runs
WORD_RE = re.compile(r"[a-z0-9']+")
SAFE_SYMBOLS = {"\u2122", "\u00a9", "\u00ae", "\u00b0"}  # (tm) (c) (r) degree


# --------------------------------------------------------------------------- helpers
def _g(row, *path, default=None):
    """Nested get for camelCase Google Ads REST rows; never raises."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _as_text(value) -> str:
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _snip(text, limit: int = 40) -> str:
    """Short echo of the advertiser's own ad text for messages.

    Not run through scrub(): ad copy is the caller's own public text, and scrub() turned long copy into
    "[REDACTED-BLOB]", hiding exactly the line the advertiser must fix (the maintainers review 2026-11-05)."""
    t = " ".join(_as_text(text).split())
    return t if len(t) <= limit else t[: max(0, limit - 1)] + "…"


def _norm(text: str) -> str:
    """Case-folded, whitespace-normalised form used for every comparison."""
    return " ".join(text.split()).lower()


def _width(text: str) -> int:
    """Google character count: East-Asian Wide/Fullwidth glyphs cost 2."""
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _jaccard(norm_a: str, norm_b: str) -> float:
    """Word-set Jaccard overlap of two already-normalised texts."""
    wa, wb = set(norm_a.split()), set(norm_b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _bad_symbols(text: str) -> list[str]:
    """Emoji / pictographs / exotic modifier symbols (ASCII and a few marks exempt)."""
    bad = set()
    for ch in text:
        if ch in SAFE_SYMBOLS or ord(ch) < 128:
            continue
        cp = ord(ch)
        if (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                or 0x2B00 <= cp <= 0x2BFF or cp == 0xFE0F):
            bad.add(ch)
        elif unicodedata.category(ch) in ("So", "Sk"):
            bad.add(ch)
    return sorted(bad)


def _has_phone(text: str) -> bool:
    """Phone numbers: digit runs of 10+ digits, separators allowed."""
    for match in PHONE_RE.finditer(text):
        if sum(c.isdigit() for c in match.group(0)) >= 10:
            return True
    return False


def _add(bucket: list, code: str, field: str, message: str) -> None:
    bucket.append({"code": code, "field": field, "message": message})


def _policy(policy) -> dict:
    """Normalise the optional policy dict; unknown/missing keys mean 'not configured'."""
    pol = {"allow_caps": (), "keywords": (), "banned_phrases": ()}
    if isinstance(policy, dict):
        for key in pol:
            value = _g(policy, key, default=None)
            if isinstance(value, (list, tuple)):
                pol[key] = tuple(v for v in value if isinstance(v, str) and v.strip())
    return pol


def _entries(items) -> list[dict]:
    """Coerce a headline/description list into [{i, text, pin}] defensively."""
    out = []
    if not isinstance(items, (list, tuple)):
        return out
    for i, item in enumerate(items):
        if isinstance(item, dict):
            raw, pin = _g(item, "text", default=""), item.get("pin")
        else:
            raw, pin = item, None
        out.append({"i": i, "text": _as_text(raw), "pin": pin if isinstance(pin, str) else None})
    return out


def _check_assets(entries, field, max_len, slots, errors, pol) -> None:
    """Per-asset rules shared by headlines and descriptions."""
    is_headline = field == "headlines"
    allow_caps = {w.lower() for w in pol["allow_caps"]}
    banned = [n for n in (_norm(p) for p in pol["banned_phrases"]) if n]
    seen: dict[str, list[int]] = {}

    for entry in entries:
        text, where = entry["text"], f"{field}[{entry['i']}]"
        width = _width(text)
        if width > max_len:
            _add(errors, f"{field[:-1]}_too_long", where,
                 f"{width} chars exceeds {max_len}: {_snip(text)}")
        if not _norm(text):
            _add(errors, "empty_text", where, "asset text is empty")
            continue
        if entry["pin"] is not None and entry["pin"] not in slots:
            _add(errors, "invalid_pin", where, f"unsupported pin {_snip(entry['pin'], 24)}")
        seen.setdefault(_norm(text), []).append(entry["i"])

        low = _norm(text)
        if "click here" in low:
            _add(errors, "click_here", where, "'click here' is not allowed")
        if is_headline and "!" in text:
            _add(errors, "punctuation", where, "no exclamation marks in headlines")
        if "!!" in text or "??" in text:
            _add(errors, "punctuation", where, "excessive punctuation")
        if REPEAT_RE.search(text):
            _add(errors, "repeated_chars", where, f"repeated characters: {_snip(text)}")
        bad = _bad_symbols(text)
        if bad:
            _add(errors, "emoji_symbols", where, "remove symbols/emoji: " + " ".join(bad))
        if _has_phone(text):
            _add(errors, "phone_number", where, "phone numbers are not allowed in text")
        caps = sorted({m.group(0) for m in CAPS_RE.finditer(text)
                       if m.group(0).lower() not in allow_caps})
        if caps:
            _add(errors, "all_caps", where, "ALL-CAPS words need approval: " + ", ".join(caps))
        for phrase in banned:
            if phrase in low:
                _add(errors, "banned_phrase", where, f"banned phrase: {_snip(phrase)}")

    for key, idxs in seen.items():
        if len(idxs) > 1:
            _add(errors, f"duplicate_{field[:-1]}", f"{field}{idxs}",
                 f"duplicate text: {_snip(key)}")


def _near_dups(entries, field, warnings) -> None:
    for a, b in combinations(entries, 2):
        na, nb = _norm(a["text"]), _norm(b["text"])
        if not na or not nb or na == nb:
            continue
        if _jaccard(na, nb) >= 0.8:
            _add(warnings, "near_duplicate", f"{field}[{a['i']},{b['i']}]",
                 "near-duplicate wording; vary the phrasing")


# --------------------------------------------------------------------------- main API
def validate_rsa(ad: dict, policy: dict | None = None) -> dict:
    """Validate one RSA draft. Returns {"ok", "errors", "warnings", "stats"}."""
    if not isinstance(ad, dict):
        raise ToolError("ad must be an object with headlines/descriptions")
    pol = _policy(policy)
    errors: list[dict] = []
    warnings: list[dict] = []

    headlines = _entries(ad.get("headlines"))
    descriptions = _entries(ad.get("descriptions"))
    n_h, n_d = len(headlines), len(descriptions)

    if not MIN_HEADLINES <= n_h <= MAX_HEADLINES:
        _add(errors, "headline_count", "headlines",
             f"need {MIN_HEADLINES}-{MAX_HEADLINES} headlines, got {n_h}")
    if not MIN_DESCRIPTIONS <= n_d <= MAX_DESCRIPTIONS:
        _add(errors, "description_count", "descriptions",
             f"need {MIN_DESCRIPTIONS}-{MAX_DESCRIPTIONS} descriptions, got {n_d}")

    _check_assets(headlines, "headlines", MAX_HEADLINE, HEADLINE_SLOTS, errors, pol)
    _check_assets(descriptions, "descriptions", MAX_DESCRIPTION, DESCRIPTION_SLOTS, errors, pol)

    # Display paths: optional, but path2 is meaningless without path1.
    p1, p2 = _as_text(ad.get("path1")), _as_text(ad.get("path2"))
    for name, value in (("path1", p1), ("path2", p2)):
        if _width(value) > MAX_PATH:
            _add(errors, "path_too_long", name,
                 f"{_width(value)} chars exceeds {MAX_PATH}: {_snip(value)}")
    if p2.strip() and not p1.strip():
        _add(errors, "path2_without_path1", "path2", "path2 requires path1")

    # Final URL must be https:// with a host.
    url = _as_text(ad.get("final_url")).strip()
    host = ""
    if not url:
        _add(errors, "final_url", "final_url", "final_url is required")
    else:
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        if parts is None:
            _add(errors, "final_url", "final_url", "final_url cannot be parsed")
        else:
            host = parts.hostname or ""
            if parts.scheme.lower() != "https":
                _add(errors, "final_url", "final_url", "final_url must use https://")
            if not host:
                _add(errors, "final_url", "final_url", "final_url must include a host")

    # Pinning: Google needs at least one free headline and <= 2 pins per slot.
    slot_counts: dict[str, int] = {}
    for entry in headlines:
        if entry["pin"] in HEADLINE_SLOTS:
            slot_counts[entry["pin"]] = slot_counts.get(entry["pin"], 0) + 1
    for slot in HEADLINE_SLOTS:
        count = slot_counts.get(slot, 0)
        if count > MAX_PINS_PER_SLOT:
            _add(errors, "pin_overflow", slot,
                 f"{count} headlines pinned to {slot} (max {MAX_PINS_PER_SLOT})")
    if n_h and all(entry["pin"] in HEADLINE_SLOTS for entry in headlines):
        _add(errors, "all_pinned", "headlines",
             "at least one headline must stay unpinned for rotation")

    # ---- warnings (non-blocking quality advice)
    if n_h < 8:
        _add(warnings, "few_headlines", "headlines", f"only {n_h} headlines; 8+ rotates better")
    if n_d < 3:
        _add(warnings, "few_descriptions", "descriptions",
             f"only {n_d} descriptions; 3+ rotates better")
    _near_dups(headlines, "headlines", warnings)
    _near_dups(descriptions, "descriptions", warnings)

    headline_hay = " ".join(_norm(e["text"]) for e in headlines)
    full_hay = " ".join(_norm(e["text"]) for e in headlines + descriptions)

    if pol["keywords"] and not any(_norm(k) in headline_hay for k in pol["keywords"]):
        _add(warnings, "no_keyword", "headlines",
             "no policy keyword appears in any headline")
    if not any(verb in set(WORD_RE.findall(full_hay)) for verb in CTA_VERBS):
        _add(warnings, "no_cta", "ad", "no call-to-action verb found")
    superlatives = [s for s in SUPERLATIVES
                    if (s in full_hay if s.startswith("#")
                        else re.search(rf"\b{re.escape(s)}\b", full_hay))]
    if superlatives:
        _add(warnings, "superlative", "ad",
             "needs substantiation: " + ", ".join(superlatives))

    stats = {
        "headline_count": n_h,
        "description_count": n_d,
        "pinned_headline_slots": [s for s in HEADLINE_SLOTS if slot_counts.get(s)],
        "pinned_description_slots": sorted({e["pin"] for e in descriptions
                                            if e["pin"] in DESCRIPTION_SLOTS}),
        "pinned_headline_count": sum(1 for e in headlines if e["pin"] in HEADLINE_SLOTS),
        "avg_headline_length": round(sum(_width(e["text"]) for e in headlines) / n_h, 1) if n_h else 0.0,
        "max_headline_length": max((_width(e["text"]) for e in headlines), default=0),
        "max_description_length": max((_width(e["text"]) for e in descriptions), default=0),
        "final_url_host": host,
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


_FIX_TEXT = {
    "headline_count": "Provide 3-15 headlines (8+ recommended).",
    "description_count": "Provide 2-4 descriptions (3+ recommended).",
    "headline_too_long": "Shorten headlines to <= 30 characters (CJK counts double).",
    "description_too_long": "Shorten descriptions to <= 90 characters.",
    "path_too_long": "Shorten display paths to <= 15 characters.",
    "path2_without_path1": "Set path1 before using path2.",
    "empty_text": "Remove empty assets or fill in their text.",
    "duplicate_headline": "Rewrite duplicate headlines so each is unique.",
    "duplicate_description": "Rewrite duplicate descriptions so each is unique.",
    "final_url": "Use a valid https:// final URL that includes a hostname.",
    "invalid_pin": "Pin only to HEADLINE_1..3 or DESCRIPTION_1..2.",
    "pin_overflow": "Pin at most 2 assets to any single slot.",
    "all_pinned": "Leave at least one headline unpinned so Google can rotate.",
    "punctuation": "Remove exclamation marks and repeated !!/?? punctuation.",
    "all_caps": "Use sentence case, or allowlist approved acronyms in policy.",
    "phone_number": "Remove phone numbers; use a call extension instead.",
    "repeated_chars": "Fix stretched words such as 'Freeee'.",
    "emoji_symbols": "Remove emoji and decorative symbols from ad text.",
    "click_here": "Replace 'click here' with a concrete call to action.",
    "banned_phrase": "Remove policy-banned phrasing.",
    "few_headlines": "Add more headlines (8+) for better rotation.",
    "few_descriptions": "Add a third description for better rotation.",
    "near_duplicate": "Vary near-duplicate assets so they test different angles.",
    "no_keyword": "Work at least one policy keyword into a headline.",
    "no_cta": "Add a call-to-action verb (call, book, get, schedule...).",
    "superlative": "Back superlatives with proof or reword them.",
}


def suggest_fixes(result) -> list[str]:
    """Short, human-readable repair hints derived from a validate_rsa() result."""
    if not isinstance(result, dict):
        raise ToolError("suggest_fixes expects the dict returned by validate_rsa")
    out: list[str] = []

    def add(msg: str) -> None:
        if msg not in out:
            out.append(msg)

    errors = result.get("errors") or []
    warnings = result.get("warnings") or []
    for item in errors:
        code = item.get("code") if isinstance(item, dict) else str(item)
        add(_FIX_TEXT.get(code, "Review the reported error and adjust the draft."))
    if not errors:
        for item in warnings:
            code = item.get("code") if isinstance(item, dict) else str(item)
            hint = _FIX_TEXT.get(code)
            if hint:
                add("Optional: " + hint)
        add("Draft passes validation; it is safe to submit for review.")
    return out


# --------------------------------------------------------------------------- selftest
def selftest() -> int:
    """Offline self-check: >= 15 assertions, no network/files needed."""
    import json

    checks = 0

    def ok(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(msg)

    def codes(res):
        return {e["code"] for e in res["errors"]}

    def wcodes(res):
        return {w["code"] for w in res["warnings"]}

    base_headlines = ["Call Now", "Book a Visit", "Get a Free Quote",
                      "Same Day Service", "Local Experts", "Compare Prices",
                      "Schedule Today", "Try Us Free"]
    base_descriptions = ["Call our team today to book a same day visit.",
                         "Get a free quote from local experts in minutes.",
                         "Schedule service online and compare our prices."]

    def ad(**over):
        draft = {"headlines": list(base_headlines),
                 "descriptions": list(base_descriptions),
                 "final_url": "https://example.com/quote",
                 "path1": "quote", "path2": "today"}
        draft.update(over)
        return draft

    good = validate_rsa(ad())
    ok(good["ok"] is True, "clean draft should pass")
    ok(good["stats"]["headline_count"] == 8, "headline count stat")
    ok(good["stats"]["avg_headline_length"] == 13.0, "avg headline length stat")
    ok(json.loads(json.dumps(good)) == good, "result is JSON-serialisable")

    empty = validate_rsa({})
    ok(empty["ok"] is False and {"headline_count", "description_count", "final_url"} <= codes(empty),
       "empty ad flags counts + url")

    ok("headline_count" in codes(validate_rsa(ad(headlines=base_headlines[:2]))), "too few headlines")
    ok("headline_count" in codes(validate_rsa(ad(headlines=base_headlines + base_headlines))),
       "16 headlines rejected")

    long_h = list(base_headlines)
    long_h[0] = "x" * 31
    ok("headline_too_long" in codes(validate_rsa(ad(headlines=long_h))), "31-char headline rejected")
    long_h[0] = "x" * 30
    ok("headline_too_long" not in codes(validate_rsa(ad(headlines=long_h))), "30-char headline allowed")

    long_d = list(base_descriptions)
    long_d[0] = "y" * 91
    ok("description_too_long" in codes(validate_rsa(ad(descriptions=long_d))), "91-char description rejected")
    long_d[0] = "y" * 90
    ok("description_too_long" not in codes(validate_rsa(ad(descriptions=long_d))), "90-char description allowed")

    ok("path_too_long" in codes(validate_rsa(ad(path1="z" * 16))), "path1 > 15 rejected")
    ok("path2_without_path1" in codes(validate_rsa(ad(path1="", path2="deal"))), "path2 needs path1")

    dup = list(base_headlines)
    dup[1] = "  call   NOW "
    ok("duplicate_headline" in codes(validate_rsa(ad(headlines=dup))), "case/space duplicate headline")

    ok("final_url" in codes(validate_rsa(ad(final_url="http://example.com"))), "http url rejected")
    ok("final_url" in codes(validate_rsa(ad(final_url="https:///nohost"))), "url without host rejected")
    ok(validate_rsa(ad(final_url="https://example.com/x"))["ok"] is True, "https url accepted")

    pinned_all = [{"text": "Call Now", "pin": "HEADLINE_1"},
                  {"text": "Book a Visit", "pin": "HEADLINE_2"},
                  {"text": "Get a Quote", "pin": "HEADLINE_3"}]
    res = validate_rsa(ad(headlines=pinned_all))
    ok("all_pinned" in codes(res) and "pin_overflow" not in codes(res), "all headlines pinned")

    overflow = [{"text": "Call Now", "pin": "HEADLINE_1"},
                {"text": "Book a Visit", "pin": "HEADLINE_1"},
                {"text": "Get a Quote", "pin": "HEADLINE_1"},
                {"text": "Try Us Free", "pin": None}]
    res = validate_rsa(ad(headlines=overflow))
    ok("pin_overflow" in codes(res) and "all_pinned" not in codes(res), "3 pins to one slot")

    two_pins = [{"text": "Call Now", "pin": "HEADLINE_1"},
                {"text": "Book a Visit", "pin": "HEADLINE_1"},
                {"text": "Get a Quote", "pin": None},
                {"text": "Try Us Free", "pin": None}]
    ok("pin_overflow" not in codes(validate_rsa(ad(headlines=two_pins))), "2 pins to one slot allowed")

    bang = list(base_headlines)
    bang[0] = "Call Now!"
    ok("punctuation" in codes(validate_rsa(ad(headlines=bang))), "! in headline rejected")
    dbl = list(base_descriptions)
    dbl[0] = "Call our team today!! Book now."
    ok("punctuation" in codes(validate_rsa(ad(descriptions=dbl))), "!! rejected")

    caps = list(base_headlines)
    caps[0] = "FREE QUOTE"
    ok("all_caps" in codes(validate_rsa(ad(headlines=caps))), "ALL-CAPS word rejected")
    ok("all_caps" not in codes(validate_rsa(ad(headlines=caps), {"allow_caps": ["FREE", "QUOTE"]})),
       "allowlisted ALL-CAPS accepted")

    phone = list(base_headlines)
    phone[0] = "Call 555-123-4567"
    ok("phone_number" in codes(validate_rsa(ad(headlines=phone))), "phone number rejected")
    short_digits = list(base_headlines)
    short_digits[0] = "Call 555-1234"
    ok("phone_number" not in codes(validate_rsa(ad(headlines=short_digits))), "short digit run allowed")

    repeat = list(base_headlines)
    repeat[0] = "Freeee Quote"
    ok("repeated_chars" in codes(validate_rsa(ad(headlines=repeat))), "stretched word rejected")

    star = list(base_headlines)
    star[0] = "\u2605 Top Rated"
    ok("emoji_symbols" in codes(validate_rsa(ad(headlines=star))), "star symbol rejected")
    tm = list(base_headlines)
    tm[0] = "Top Rated\u2122"
    ok("emoji_symbols" not in codes(validate_rsa(ad(headlines=tm))), "trademark sign allowed")

    click = list(base_headlines)
    click[0] = "Click here now"
    ok("click_here" in codes(validate_rsa(ad(headlines=click))), "'click here' rejected")

    ok("banned_phrase" in codes(validate_rsa(ad(), {"banned_phrases": ["free quote"]})),
       "banned phrase rejected")

    few = validate_rsa(ad(headlines=base_headlines[:3], descriptions=base_descriptions[:2]))
    ok({"few_headlines", "few_descriptions"} <= wcodes(few), "low-asset warnings")

    near = validate_rsa(ad(headlines=["Book a free visit", "Book a free visit online", "Call Now"]))
    ok("near_duplicate" in wcodes(near), "near-duplicate warning")

    sup = list(base_headlines)
    sup[0] = "Best Prices"
    ok("superlative" in wcodes(validate_rsa(ad(headlines=sup))), "superlative warning")

    no_cta = validate_rsa(ad(headlines=["Shiny Floors", "Local Experts", "Fast Service"],
                             descriptions=["Floor cleaning done right.", "Trusted local crew."]))
    ok("no_cta" in wcodes(no_cta), "missing CTA warning")
    ok("no_keyword" in wcodes(validate_rsa(ad(), {"keywords": ["plumber"]})), "missing keyword warning")

    cjk_long = list(base_headlines)
    cjk_long[0] = "\u65e5" * 16          # width 32 > 30
    ok("headline_too_long" in codes(validate_rsa(ad(headlines=cjk_long))), "CJK width counted as 2")
    cjk_long[0] = "\u65e5" * 15          # width 30, allowed
    ok("headline_too_long" not in codes(validate_rsa(ad(headlines=cjk_long))), "15 CJK chars fit")

    broken = validate_rsa(ad(headlines=["Call Now", None, 5, {"text": None}]))
    ok("empty_text" in codes(broken), "malformed rows degrade to empty-text errors")

    fixes = suggest_fixes(validate_rsa({}))
    ok(isinstance(fixes, list) and fixes and all(isinstance(f, str) for f in fixes),
       "suggest_fixes returns strings for a failing draft")
    ok(any("safe to submit" in f for f in suggest_fixes(good)), "suggest_fixes confirms a passing draft")

    try:
        validate_rsa(["not-a-dict"])
    except ToolError:
        checks += 1
    else:
        raise AssertionError("non-dict ad must raise ToolError")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
