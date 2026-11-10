"""Number normalisation and number tracing for the AdRails guarded content pipeline (0.12).

Everything handled here (page text, model output, facts files) is UNTRUSTED data. No regex is ever built from it, nothing
is eval'd or exec'd, and every string is capped at MAX_TEXT characters and every returned list at MAX_LIST items, so a
hostile 50 MB body cannot turn a check into a hang. Pure functions only: no network, no subprocess, no file writes, no
clock beyond "what year is it" for the default year window.

Public API (SPEC-D, numbers.py):

    norm(text)                       -> ASCII-ish, whitespace-collapsed, lowercased text
    sentences(text)                  -> list[str]
    numbers_in(text)                 -> list[str] of normalised number tokens ("$1,100" -> "1100", "12.0" -> "12")
    trace(text, allowed_text, ...)   -> [{"code": "UNTRACED_NUMBER", "msg": ...}] one entry per untraceable number
    word_statistics(text)            -> [{"code": "WORD_STATISTIC", "msg": ...}] for "forty percent", "one in five"...
    new_sentences(text, live_text)   -> helper: sentences of `text` that are not already in `live_text` (extend mode)

Tracing rule (short): a number is traced when its normalised value appears in the allowed text (facts file + verified
quotes + source labels). Two escapes exist for numbers that are *not* claims: bare integers <= small_max ("3 steps") and
4-digit years inside the year window. Both escapes are disabled for statistics ("3%", "1 in 5") and for dollar amounts
("$5" is still a claim), which must always be traced.
"""
import re
import unicodedata
from bisect import bisect_right
from datetime import date

from ..errors import scrub

MAX_TEXT = 200_000                  # cap for every input string (chars)
MAX_LIST = 500                      # cap for every returned list of ERRORS (items)
MAX_SCAN = 50_000                   # PM: tokens/sentences scanned; above this trace() fails closed (never silently skips)
MAX_SENTENCE = 160                  # cap for a sentence quoted back in a message
MAX_TOKEN = 40                      # absurd single number token guard
DEFAULT_SMALL_MAX = 12              # bare integers <= this need no trace

_ONCE_CHARS = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u2035": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u2036": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-", "\u2043": "-",
    "\u2026": "...", "\u203d": "?!", "\u00ab": '"', "\u00bb": '"',
    "\u00a0": " ", "\u1680": " ", "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
    "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ", "\u2008": " ", "\u2009": " ",
    "\u200a": " ", "\u202f": " ", "\u205f": " ", "\u3000": " ",
}
_ZERO = "\u200b\u200c\u200d\u2060\ufeff\u180e\u00ad"     # PM (GLM review #2): soft hyphen is dropped, not a dash
_TRANS = str.maketrans(_ONCE_CHARS)
_DROP = str.maketrans({c: None for c in _ZERO})

_WS_RE = re.compile(r"\s+")
_HSPACE_RE = re.compile(r"[^\S\n]+")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BOUND_RE = re.compile(r"[.!?](?=\s|$)")

# Number token: no letter/word char or dot immediately before (so "v2" and the tail of "3.4.5" are not tokens).
_NUM_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")
_BARE_INT_RE = re.compile(r"\A\d{1,9}\Z")
_YEAR_RE = re.compile(r"\A\d{4}\Z")

# Context markers used for statistics (numbers) and spelled-out statistics (word_statistics).
_PCT_AFTER = re.compile(r"^\s*(?:%|percent\b|per\s?cent\b|pct\b)")
_OUT_AFTER = re.compile(r"^\s*out\s+of\b")
_RANGE_AFTER = re.compile(r"^\s*(?:-|to)\s*\$?\d[\d,.]*\s*(?:%|percent\b|per\s?cent\b)")   # PM: "5-10%", "5 to 10 percent"
_OF_EVERY_AFTER = re.compile(r"^\s+of\s+every\b")
_IN_NUM_AFTER = re.compile(r"^\s+(?:in|of|for|to)\s+(?:{nw}|\d)".format(
    nw=r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
       r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)"))

_ONES = r"one|two|three|four|five|six|seven|eight|nine"
_TEENS = r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_TENS = r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_SCALES = r"hundred|hundreds|thousand|thousands|million|millions|billion|billions|trillion|trillions"
_FRACS = r"half|halves|third|thirds|quarter|quarters|fifth|fifths|tenth|tenths"
_DOZEN = r"dozen|dozens|couple"
_TOK = "|".join((_ONES, _TEENS, _TENS, _SCALES, _FRACS, _DOZEN))
_NUMWORD_RE = re.compile(r"(?<![\w])(?:" + _TOK + r")(?:[-\s]+(?:" + _TOK + r")){0,4}\b", re.I)

_SCALE_WORDS = frozenset("hundred hundreds thousand thousands million millions billion billions trillion trillions".split())
_FRAC_WORDS = frozenset(("half", "halves", "halfs", "third", "thirds", "quarter", "quarters", "fifth", "fifths",
                         "tenth", "tenths"))
_VAGUE_WORDS = _SCALE_WORDS | _FRAC_WORDS | {"dozen", "dozens", "couple"}

_NOUNS = (r"properties|property|homes|home|houses|house|units|unit|doors|owners|owner|landlords|landlord|clients|client"
          r"|customers|customer|tenants|tenant|people|person|residents|resident|investors|investor|listings|listing"
          r"|deals|deal|transactions|transaction|sales|sale|calls|call|leads|lead|steps|step|days|day|weeks|week|months"
          r"|month|years|year|hours|hour|minutes|minute|miles|mile|times|visits|visit|views|pageviews|sessions|session"
          r"|keywords|agents|agent|managers|manager|employees|employee|staff|reviews|review|offices|office|markets"
          r"|market|cities|city|counties|county|stars|bedrooms|bathrooms|fees|fee|minutes|seconds|gigs|jobs")
_MONEY_AFTER = re.compile(r"^\s*(?:dollars?|bucks?|cents?|grand|usd|euros?|pounds?)")
_COUNT_AFTER = re.compile(r"^\s+(?:the\s+|a\s+|an\s+)?(?:" + _NOUNS + r")\b", re.I)
_OF_AFTER = re.compile(r"^\s+of\b")


# ------------------------------------------------------------------------------------------------------------- helpers
def _text(value, limit=MAX_TEXT):
    """Coerce UNTRUSTED input to a capped str; None and unusable objects become "" and never raise."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    return value[:limit] if len(value) > limit else value


def _prepare(text):
    """ASCII-map, de-zero-width and collapse ALL whitespace (newlines included) to single spaces. Keeps case."""
    s = _text(text)
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = unicodedata.normalize("NFKC", s.translate(_DROP))       # PM (GLM review #2): fullwidth digits, ligatures, ...
    s = s.translate(_TRANS)
    return _WS_RE.sub(" ", s).strip()


def _value(token):
    """Normalise a number token: drop $ % , and trailing decimal zeros ("$1,100"->"1100", "12.0"->"12")."""
    s = _text(token, MAX_TOKEN).replace("$", "").replace("%", "").replace(",", "").strip()
    whole, _sep, frac = s.partition(".")
    whole = whole.lstrip("0") or "0"
    frac = frac.rstrip("0")
    return whole + ("." + frac if frac else "")


def _small(small_max):
    """Sanitise small_max; anything absurd falls back to the default."""
    try:
        value = int(small_max)
    except Exception:
        return DEFAULT_SMALL_MAX
    return value if 0 <= value <= MAX_TEXT else DEFAULT_SMALL_MAX


def _years(years):
    """Sanitise the year window; None means (current year - 1, current year + 1)."""
    try:
        if years is not None:
            lo, hi = int(years[0]), int(years[1])
            return (min(lo, hi), max(lo, hi))
    except Exception:
        pass
    this_year = date.today().year
    return (this_year - 1, this_year + 1)


def _spans(prepared):
    """-> list of (start, end) sentence spans over prepared text (cut after .!? followed by space/end)."""
    spans = []
    start = 0
    for m in _BOUND_RE.finditer(prepared):
        end = m.end()
        if end > start:
            spans.append((start, end))
        start = end
    if start < len(prepared):
        spans.append((start, len(prepared)))
    if not spans and prepared:
        spans.append((0, len(prepared)))
    return spans


def _sentence_for(prepared, spans, starts, pos):
    """Return the sentence of `prepared` that contains character offset `pos`."""
    if not spans:
        return ""
    idx = bisect_right(starts, pos) - 1
    if idx < 0:
        idx = 0
    elif idx >= len(spans):
        idx = len(spans) - 1
    return prepared[spans[idx][0]:spans[idx][1]]


def _clip(text, limit=MAX_SENTENCE):
    """Clip untrusted text for a message, on a word boundary where possible."""
    s = " ".join(_text(text).split())
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut + "…"


def _scan(prepared):
    """-> list of number-token dicts found in prepared text (capped at MAX_LIST)."""
    items = []
    if not prepared:
        return items
    for m in _NUM_RE.finditer(prepared):
        token = m.group(0)
        if len(token) > MAX_TOKEN:
            continue
        value = _value(token)
        if not value:
            continue
        nxt = prepared[m.end():m.end() + 2]
        items.append({"tok": token, "val": value, "start": m.start(), "end": m.end(),
                      "money": token.startswith("$"), "pct": token.endswith("%"),
                      "scaled": bool(re.match(r"[kKmMbB](?![a-zA-Z])", nxt))})   # PM: "5k owners" is not a small number
        if len(items) >= MAX_SCAN:
            break
    return items


def _is_statistic(prepared, end):
    """True when the number ending at `end` reads as a statistic ("3%", "5 percent", "1 in 5", "2 out of 3")."""
    after = prepared[end:end + 48].lower()
    if not after:
        return False
    return bool(_PCT_AFTER.match(after) or _OUT_AFTER.match(after) or _OF_EVERY_AFTER.match(after)
                or _IN_NUM_AFTER.match(after) or _RANGE_AFTER.match(after))


def _allowed_numbers(allowed_text):
    """-> set of normalised numbers that count as traced. Accepts str, list/tuple/set of str, or None."""
    if allowed_text is None:
        return set()
    if isinstance(allowed_text, (list, tuple, set, frozenset)):
        joined = " ".join(_text(x, MAX_TEXT // 8) for x in list(allowed_text)[:MAX_SCAN])
    else:
        joined = _text(allowed_text)
    return set(numbers_in(joined))


# ----------------------------------------------------------------------------------------------------------- public
def norm(text):
    """Map curly quotes / dashes / nbsp / zero-width to ASCII, collapse whitespace, lowercase. None -> ""."""
    return _prepare(text).lower()


def sentences(text):
    """Split text into sentences on [.!?] followed by space/end, and on newlines; strip, drop empties."""
    raw = _text(text).replace("\r\n", "\n").replace("\r", "\n").translate(_DROP).translate(_TRANS)
    out = []
    for line in raw.split("\n"):
        line = _HSPACE_RE.sub(" ", line).strip()
        if not line:
            continue
        for part in _SENT_SPLIT_RE.split(line):
            part = " ".join(part.split())
            if part:
                out.append(part)
                if len(out) >= MAX_SCAN:
                    return out
    return out


def numbers_in(text):
    """-> list of distinct normalised number tokens in text, in order (strip $ % , and trailing decimal zeros)."""
    seen = set()
    out = []
    for item in _scan(_prepare(text)):
        value = item["val"]
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= MAX_SCAN:
            break
    return out


def trace(text, allowed_text="", small_max=DEFAULT_SMALL_MAX, years=None):
    """Flag every number in `text` that is not traceable to `allowed_text` (facts + quotes + labels).

    A number passes when its normalised value is in numbers_in(allowed_text). Otherwise bare integers <= small_max and
    4-digit years inside `years` (default: previous, current, next year) pass - UNLESS the number is a statistic
    ("68%", "5 percent", "1 in 5", "2 out of 3") or a dollar amount, which must always be traced. Each error quotes the
    offending sentence, clipped to 160 characters. Returns at most MAX_LIST errors.
    """
    raw = text if isinstance(text, str) else _text(text)
    if len(raw) > MAX_TEXT:     # PM: fail closed - text past the cap would otherwise go untraced
        return [{"code": "UNTRACED_NUMBER", "msg": "text is longer than %d characters; refused untraced" % MAX_TEXT}]
    allowed = _allowed_numbers(allowed_text)
    small = _small(small_max)
    window = _years(years)
    prepared = _prepare(text)
    items = _scan(prepared)
    if not items:
        return []
    if len(items) >= MAX_SCAN:
        return [{"code": "UNTRACED_NUMBER", "msg": "more than %d numbers; refused untraced" % MAX_SCAN}]
    spans = _spans(prepared)
    starts = [span[0] for span in spans]
    seen = set()
    errors = []
    for item in items:
        value = item["val"]
        if value in allowed:
            continue
        statistic = item["pct"] or _is_statistic(prepared, item["end"])
        if not item["money"] and not statistic and not item["scaled"]:
            if _BARE_INT_RE.match(value) and int(value) <= small:
                continue
            if _YEAR_RE.match(value) and window[0] <= int(value) <= window[1]:
                continue
        sentence = _sentence_for(prepared, spans, starts, item["start"])
        key = (value, sentence)
        if key in seen:
            continue
        seen.add(key)
        if item["money"]:
            kind = "dollar amount"
        elif statistic:
            kind = "statistic"
        else:
            kind = "number"
        msg = ('"{}" - the {} {} is not traced to FACTS.json or a cited source; cite a source or remove it.'
               .format(_clip(sentence), kind, _clip(item["tok"], 40)))
        errors.append({"code": "UNTRACED_NUMBER", "msg": scrub(msg, 400)})
        if len(errors) >= MAX_LIST:
            break
    return errors


def _marker(phrase, after):
    """-> a reason string when a spelled-out number phrase is used as a stat/money/count, else None."""
    if _PCT_AFTER.match(after):
        return "a statistic"
    if _OUT_AFTER.match(after) or _IN_NUM_AFTER.match(after) or _OF_EVERY_AFTER.match(after):
        return "a statistic"
    if _MONEY_AFTER.match(after):
        return "a money amount"
    if _COUNT_AFTER.match(after):
        return "a count"
    words = phrase.replace("-", " ").split()
    if words and words[-1] in _VAGUE_WORDS and _OF_AFTER.match(after):
        return "a vague quantity"
    return None


def word_statistics(text):
    """Flag spelled-out numbers used as statistics, money or counts ("forty percent", "one in five owners",
    "nine out of ten", "two hundred dollars", "a dozen properties", "thousands of clients", "half of all landlords").

    Covers one..ninety-nine, hundred/thousand/million, dozen(s), half and a third/quarter/fifth/tenth.
    """
    prepared = _prepare(text)
    if not prepared:
        return []
    low = prepared.lower()
    errors = []
    covered_to = 0
    for m in _NUMWORD_RE.finditer(low):
        start, end = m.start(), m.end()
        if start < covered_to:
            continue
        phrase = low[start:end]
        after = low[end:end + 60]
        reason = _marker(phrase, after)
        if not reason:
            continue
        snippet = prepared[start:min(len(prepared), end + 40)]
        msg = ('"{}" spells out {} ({}); write the exact figure with a source, or drop the number.'
               .format(_clip(snippet), reason, _clip(phrase, 60)))
        errors.append({"code": "WORD_STATISTIC", "msg": scrub(msg, 400)})
        covered_to = end
        if len(errors) >= MAX_LIST:
            break
    return errors


def _match_key(sentence):
    """Normalised sentence with outer punctuation stripped, so "Old line." matches "the old line stays here"."""
    return norm(sentence).strip(' .!?,;:"()[]\'-\u2019')


def new_sentences(text, live_text):
    """Helper for extend mode: sentences of `text` that are not already present (normalised) in `live_text`."""
    live = norm(live_text)
    out = []
    for sentence in sentences(text):
        key = _match_key(sentence)
        if key and key in live:
            continue
        out.append(sentence)
    return out


# ---------------------------------------------------------------------------------------------------------- selftest
def selftest() -> int:
    n = 0

    def chk(cond, msg):
        nonlocal n
        assert cond, "numbers.selftest: " + msg
        n += 1

    # norm ------------------------------------------------------------------------------------------------
    chk(norm("“Flat-fee”\u00a0management — $149") == '"flat-fee" management - $149', "norm maps quotes/dash/nbsp")
    chk(norm("a\u200bb   c") == "ab c", "norm drops zero-width and collapses")
    chk(norm(None) == "", "norm(None) is empty")
    chk(norm(12) == "12", "norm coerces non-str")
    chk(norm("MiXeD Case\tText") == "mixed case text", "norm lowercases and collapses tabs")
    chk(norm(norm("—We’re  the  “best”")) == norm("—We’re  the  “best”"), "norm is idempotent")
    chk(len(norm("a" * (MAX_TEXT + 5000))) == MAX_TEXT, "norm caps huge input at MAX_TEXT")

    # sentences -------------------------------------------------------------------------------------------
    chk(sentences("One. Two! Three?") == ["One.", "Two!", "Three?"], "sentences split on .!?")
    chk(sentences("Line one\nLine two.") == ["Line one", "Line two."], "sentences split on newlines")
    chk(sentences("") == [] and sentences(None) == [] and sentences(" \n\n ") == [], "sentences of empty input")
    chk(sentences("Flat fee works. It does.")[0] == "Flat fee works.", "sentences keep original case")
    chk(sentences("No stop here") == ["No stop here"], "sentences of a single unpunctuated line")
    chk(len(sentences("A short one. " * 2000)) == 2000, "sentences not capped below MAX_SCAN (PM: no silent skips)")
    chk(sentences("Fee is $1,100. Fees vary.") == ["Fee is $1,100.", "Fees vary."], "sentences keep money tokens")

    # numbers_in ------------------------------------------------------------------------------------------
    chk(numbers_in("Management is $1,100 per month.") == ["1100"], "numbers_in strips $ and ,")
    chk(numbers_in("68% of owners") == ["68"], "numbers_in strips %")
    chk(numbers_in("4.5% fees, 1,788 homes, 12.0 units") == ["4.5", "1788", "12"], "numbers_in normalises decimals")
    chk(numbers_in(None) == [] and numbers_in("") == [], "numbers_in of empty input")
    chk(all(isinstance(x, str) and "$" not in x for x in numbers_in("$5 and $9")), "numbers_in returns plain strings")
    chk(numbers_in("v2 and 3.4.5") == ["3.4"], "numbers_in ignores letter-bound and dotted tails")
    chk(numbers_in("$1,100 then $1,100") == ["1100"], "numbers_in dedupes")

    # trace: reference lessons ----------------------------------------------------------------------------
    r = trace("About 68% of owners renew each year.", "")
    chk(len(r) == 1 and r[0]["code"] == "UNTRACED_NUMBER", "\"68% of owners\" is untraced")
    chk("68% of owners" in r[0]["msg"], "trace message quotes the sentence")
    chk(len(trace("The flat fee is $1,100 per year.", "")) == 1, "\"$1,100\" is untraced")
    chk(trace("The fee is $149 per month per home.", "Management is $149 per month per home.") == [],
        "\"$149\" is traced when allowed")
    chk(trace("Set up in 3 steps.", "") == [], "\"3 steps\" passes as a small bare integer")
    chk(len(trace("Renewals run at 3%.", "")) == 1, "\"3%\" is untraced (percent escapes nothing)")
    chk(trace("Pricing rose in 2025.", "", years=(2024, 2026)) == [], "\"2025\" passes when in the year window")
    chk(trace("Pricing rose in {}".format(date.today().year), "") == [], "default year window covers this year")
    chk(len(trace("This started in 1998.", "", years=(2024, 2026))) == 1, "\"1998\" is untraced outside the window")
    chk(len(word_statistics("It costs one hundred forty-nine dollars.")) == 1,
        "\"one hundred forty-nine dollars\" is a word statistic")

    # trace: money and statistics always traced -----------------------------------------------------------
    chk(len(trace("Saves you $5 a month.", "")) == 1, "dollar amounts are always traced ($5 fails)")
    chk(trace("Saves you $5 a month.", "Rebate $5 per month") == [], "$5 passes when allowed")
    chk(len(trace("1 in 5 owners renew.", "")) == 1, "\"1 in 5\" statistic is always traced")
    chk(len(trace("Only 3 percent renew.", "")) == 1, "\"3 percent\" statistic is always traced")
    chk(len(trace("2 out of 3 owners renew.", "")) >= 1, "\"2 out of 3\" statistic is always traced")
    chk(len(trace("150 landlords signed up.", "")) == 1, "a 3-digit bare integer is traced")
    chk(trace("150 landlords signed up.", "We serve 150 landlords") == [], "traced when the value is allowed")
    chk(trace("Set up in 15 steps.", "", small_max=20) == [], "small_max raises the exception ceiling")
    chk(len(trace("Set up in 3 steps.", "", small_max=2)) == 1, "small_max lowers the exception ceiling")
    chk(trace("A 4.5% fee is normal.", "Our fee is 4.5% of rent", ) == [], "decimal traced when allowed")
    chk(len(trace("A 4.5% fee is normal.", "")) == 1, "decimal untraced when not allowed")
    chk(trace("We manage 1,788 homes.", "We manage 1,788 homes in Ohio.") == [], "\"1,788\" traced when allowed")
    chk(len(trace("We manage 1,788 homes.", "")) == 1, "\"1,788\" untraced when not allowed")

    # trace: shapes, caps, hostile input ----------------------------------------------------------------
    chk(trace(None) == [] and trace("", "") == [] and trace("no digits here", None) == [], "trace of empty input")
    chk(trace("nine out of ten owners", "") == [], "spelled-out numbers are not number tokens")
    chk(len(trace("We serve 150 landlords.", None)) == 1, "trace accepts allowed_text=None")
    chk(len(trace("150 landlords " * 20000)) <= MAX_LIST, "trace caps the error list")
    chk(len(trace("\u200b" * 200000 + "fine")) == 1, "oversize text is refused, not silently truncated (PM)")
    chk(len(trace("we manage 5k homes")) == 1, "5k is not a small number (PM)")
    chk(len(trace(" ".join(str(i + 100) for i in range(800)))) == MAX_LIST, "numbers past 500 are still traced (PM)")
    chk(len(trace('He said "68% of owners" twice.', "")) == 1, "trace reads numbers inside quotes")
    chk(len(trace("68% of owners and 68% of owners.", "")) == 1, "trace dedupes equal number+sentence")
    chk(all(set(x) == {"code", "msg"} for x in trace("68% and $9 and 150", "")), "trace error shape is code+msg")

    # word_statistics -----------------------------------------------------------------------------------
    chk(len(word_statistics("Forty percent of owners renew.")) == 1, "\"forty percent\" is flagged")
    chk(word_statistics("Forty percent of owners renew.")[0]["code"] == "WORD_STATISTIC", "word stat code")
    chk(len(word_statistics("One in five owners renews.")) >= 1, "\"one in five owners\" is flagged")
    chk(len(word_statistics("Nine out of ten owners renew.")) >= 1, "\"nine out of ten\" is flagged")
    chk(len(word_statistics("It costs two hundred dollars.")) >= 1, "\"two hundred dollars\" is flagged")
    chk(len(word_statistics("We manage a dozen properties.")) >= 1, "\"a dozen properties\" is flagged")
    chk(len(word_statistics("We help thousands of clients.")) >= 1, "\"thousands of clients\" is flagged")
    chk(len(word_statistics("Half of all landlords renew.")) >= 1, "\"half of all landlords\" is flagged")
    chk(word_statistics("We manage homes in Springfield.") == [], "plain prose is not flagged")
    chk(word_statistics("The one of the best teams.") == [], "\"one of the best\" is not a count")
    chk(word_statistics(None) == [] and word_statistics("") == [], "word_statistics of empty input")
    chk(len(word_statistics("forty percent or " * 2000)) == MAX_LIST, "word_statistics caps the list")
    chk(all("WORD_STATISTIC" == x["code"] for x in word_statistics("a dozen homes and half of all owners")),
        "word_statistics codes are stable")

    # new_sentences helper -------------------------------------------------------------------------------
    chk(new_sentences("Old line. New line.", "The old line stays here.") == ["New line."],
        "new_sentences keeps only new sentences")
    chk(new_sentences(None, None) == [] and new_sentences("", "x") == [], "new_sentences of empty input")
    chk(len(trace("save \uff16\uff18\uff05 now")) == 1, "fullwidth 68% is normalised and traced (GLM #2)")
    chk(numbers_in("\uff16\uff18") == ["68"], "fullwidth digits read as ASCII")
    chk(len(trace("we cut costs 5-10% for owners")) == 2, "both ends of a percent range are statistics")
    chk(norm("com\u00adpetitor") == "competitor", "soft hyphen dropped, not turned into a dash")
    return n
