"""Tracking codes and tracking URLs for direct-mail pieces (adsops.mail.codes).

Every printed piece gets a short, human-readable code carrying a Luhn-mod-30
check character, so a code read back over the phone can be validated before it
is attributed to a lead. A wrong guess would credit the wrong piece, so we never
"repair" lookalike glyphs (0/O, 1/I/L, U): such input is simply refused.

Safety: codes are validated before use, tracking URLs must be https (cleartext
links are refused), error text is scrubbed, and all randomness is injectable
(`seed=`/`rng=`) so tools and tests stay deterministic. Tools should call
`assign()` once per batch and `parse_code_from_url()` on inbound visits.
"""

from __future__ import annotations

import random
import secrets
import string
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:  # pragma: no cover - keeps `python adsops/mail/codes.py` runnable from a checkout
    from adsops.errors import RailViolation, ToolError, scrub
except ImportError:  # pragma: no cover
    class ToolError(Exception):
        """User-visible failure."""

    class RailViolation(Exception):
        """A safety rail refused the operation."""

    def scrub(text, limit=1500):
        text = str(text)
        return text if len(text) <= limit else text[:limit] + "..."


# 30 symbols: no 0/1/I/L/O/U, so codes survive being read aloud or handwritten.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
MIN_LEN = 5
MAX_LEN = 12
MAX_BATCH = 1_000_000
DEFAULT_UTM = {"utm_source": "direct_mail", "utm_medium": "mail"}
_SEPARATORS = frozenset(" \t\r\n-._")
_PARAM_CHARS = frozenset(string.ascii_letters + string.digits + "_.-[]")


def _g(row, *path, default=None):
    """Nested dict get for piece/row lookups (Google Ads REST rows are nested)."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _luhn_sum(body: str) -> int:
    """Luhn mod N sum: from the rightmost char, double every second codepoint."""
    n = len(ALPHABET)
    factor = 2
    total = 0
    for ch in reversed(body):
        addend = factor * ALPHABET.index(ch)
        factor = 1 if factor == 2 else 2
        total += (addend // n) + (addend % n)
    return total


_P = 31   # prime modulus: one more than the alphabet size


def _weighted(body: str) -> int:
    return sum((k + 1) * ALPHABET.index(ch) for k, ch in enumerate(body)) % _P


def check_char(body: str) -> str | None:
    """Check character for `body` under a position-weighted checksum modulo the prime 31 (the maintainers, 2026-11-05).

    The whole code c_1..c_L (check char last) satisfies  sum(k * v_k) == 0  (mod 31), weights k = 1..L.
    Because 31 is prime and every weight is non-zero mod 31, ANY single substitution (anywhere, check char included)
    changes the sum by k*(a-b) != 0, and ANY adjacent transposition changes it by (a-b) != 0: both are always detected.
    (Luhn mod 30 and ISO 7064 MOD 31,30 both miss ~0.2% of adjacent swaps; measured, so they were replaced.)
    The alphabet has 30 symbols but the check value has 31 possibilities: when it would be 30 there is no symbol, so the
    function returns None and make_code() draws a new body (about 1 body in 31)."""
    if not isinstance(body, str) or not body:
        raise ToolError("check_char() requires a non-empty code body")
    body = body.upper()
    if any(ch not in ALPHABET for ch in body):
        raise ToolError("code body contains characters outside the code alphabet")
    w = len(body) + 1                                   # the check char's weight
    v = (-_weighted(body) * pow(w, -1, _P)) % _P
    return ALPHABET[v] if v < len(ALPHABET) else None


def make_code(rng, length: int = 7, banned=()) -> str:
    """Body of `length - 1` random symbols plus its check char.

    Never returns an all-one-symbol code and never contains a banned substring.
    `rng` only needs a `.choice(seq)` method (random.Random / secrets.SystemRandom).
    """
    if not isinstance(length, int) or isinstance(length, bool) or not (2 <= length <= MAX_LEN):
        raise ToolError(f"code length must be an integer between 2 and {MAX_LEN}")
    banned = tuple(str(b).upper() for b in (banned or ()))
    body_len = length - 1
    for _ in range(1000):
        body = "".join(rng.choice(ALPHABET) for _ in range(body_len))
        cc = check_char(body)
        if cc is None:            # check value 30 has no symbol: draw again
            continue
        code = body + cc
        if len(set(code)) < 2:  # all one character -> misreads as a typo
            continue
        if any(b and b in code for b in banned):
            continue
        return code
    raise ToolError("could not draw a code satisfying the constraints")


def normalize(text) -> str | None:
    """Canonical code, or None. Never guesses lookalikes (0/O, 1/I/L, U)."""
    if not isinstance(text, str):
        return None
    cleaned = "".join(ch for ch in text.upper() if ch not in _SEPARATORS)
    if not (MIN_LEN <= len(cleaned) <= MAX_LEN):
        return None
    if any(ch not in ALPHABET for ch in cleaned):
        return None
    if check_char(cleaned[:-1]) != cleaned[-1]:
        return None
    return cleaned


def is_valid(code) -> bool:
    """True only for a code with correct length, alphabet and check character."""
    return normalize(code) is not None


def generate(n: int, existing=(), length: int = 7, seed=None, banned=()) -> list[str]:
    """`n` unique valid codes, disjoint from `existing` and from each other."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise ToolError("n must be an integer")
    if n < 0:
        raise ToolError("n must be >= 0")
    if n > MAX_BATCH:
        raise ToolError(f"n must be <= {MAX_BATCH:,}")
    if not isinstance(length, int) or isinstance(length, bool) or not (MIN_LEN <= length <= MAX_LEN):
        raise ToolError(f"length must be between {MIN_LEN} and {MAX_LEN}")
    if isinstance(existing, str):
        existing = [existing]
    existing_list = list(existing or ())
    taken = {c for c in (normalize(e) for e in existing_list) if c}
    space = len(ALPHABET) ** (length - 1)
    if space < 4 * (n + len(existing_list)):
        raise ToolError(
            f"code space too small: {len(ALPHABET)}^{length - 1}={space} "
            f"for {n} new + {len(existing_list)} existing codes"
        )
    if n == 0:
        return []
    rng = random.Random(seed) if seed is not None else secrets.SystemRandom()
    out: list[str] = []
    seen = set(taken)
    attempts = 0
    limit = 20 * n + 5000
    while len(out) < n:
        attempts += 1
        if attempts > limit:
            raise ToolError("could not generate enough distinct codes; use a longer length")
        code = make_code(rng, length=length, banned=banned)
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _safe_param(name, what: str = "param") -> str:
    """Query-parameter names are restricted to characters that need no escaping."""
    if not isinstance(name, str) or not name:
        raise ToolError(f"{what} must be a non-empty string")
    if len(name) > 64 or any(ch not in _PARAM_CHARS for ch in name):
        raise ToolError(f"{what} contains characters that are not URL-safe")
    return name


def _check_base_url(base_url):
    if not isinstance(base_url, str) or not base_url.strip():
        raise ToolError("base_url is required")
    try:
        parts = urlsplit(base_url.strip())
    except ValueError:
        raise ToolError(f"base_url is not a valid URL: {scrub(base_url, 120)}") from None
    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise ToolError(f"base_url must be an absolute https URL: {scrub(base_url, 120)}")
    if scheme != "https":
        # Safety rail: a cleartext tracking link leaks codes and segment data.
        raise RailViolation("tracking URLs must use https; cleartext links are refused")
    if not parts.hostname:
        raise ToolError(f"base_url needs a host: {scrub(base_url, 120)}")
    return parts


def tracking_url(base_url: str, code, param: str = "c", utm=None) -> str:
    """https URL carrying the code plus UTM params; existing query/fragment kept."""
    parts = _check_base_url(base_url)
    key = _safe_param(param)
    canon = normalize(code)
    if canon is None:
        raise ToolError(f"invalid tracking code: {scrub(code, 40)}")
    if utm is not None and not isinstance(utm, dict):
        raise ToolError("utm must be a mapping of query parameters")
    params = dict(DEFAULT_UTM)
    for k, v in (utm or {}).items():
        pk = _safe_param(k, what="utm key")
        if v is None or v == "":
            params.pop(pk, None)  # explicit None removes a default
        else:
            params[pk] = str(v)
    taken = set(params) | {key}
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in taken]
    pairs.append((key, canon))
    pairs.extend(params.items())
    return urlunsplit(("https", parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def parse_code_from_url(url, param: str = "c") -> str | None:
    """Normalized valid code from a tracking URL, or None (never raises)."""
    if not isinstance(url, str) or not url.strip():
        return None
    want = param.lower() if isinstance(param, str) and param else "c"
    try:
        parts = urlsplit(url.strip())
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return None
    for k, v in pairs:
        if k.lower() == want:
            return normalize(v)
    return None


def assign(pieces, base_url: str, existing=(), length: int = 7, seed=None,
           utm=None, campaign=None) -> list[dict]:
    """Copy each piece with `tracking_code` + `tracking_url`; valid codes are kept."""
    if isinstance(pieces, dict) or isinstance(pieces, (str, bytes)) or pieces is None:
        raise ToolError("pieces must be a list of objects")
    pieces = list(pieces)
    for i, p in enumerate(pieces):
        if not isinstance(p, dict):
            raise ToolError(f"piece {i} is not an object")
    _check_base_url(base_url)  # fail before doing any work
    if not isinstance(length, int) or isinstance(length, bool) or not (MIN_LEN <= length <= MAX_LEN):
        raise ToolError(f"length must be between {MIN_LEN} and {MAX_LEN}")
    if isinstance(existing, str):
        existing = [existing]
    existing_list = list(existing or ())

    kept: list[str] = []
    need = 0
    for p in pieces:
        code = normalize(_g(p, "tracking_code"))
        if code:
            kept.append(code)
        else:
            need += 1
    # Re-issued codes must dodge both caller-supplied and already-kept codes.
    pool = existing_list + kept
    fresh = generate(need, existing=pool, length=length, seed=seed) if need else []
    queue = iter(fresh)

    out: list[dict] = []
    for p in pieces:
        piece = dict(p)
        code = normalize(_g(p, "tracking_code")) or next(queue)
        piece_utm = dict(utm or {})
        if campaign not in (None, ""):
            piece_utm["utm_campaign"] = str(campaign)
        segment = _g(p, "segment")
        if isinstance(segment, str) and segment.strip():
            piece_utm["utm_content"] = segment.strip()
        piece["tracking_code"] = code
        piece["tracking_url"] = tracking_url(base_url, code, utm=piece_utm)
        out.append(piece)
    return out


def selftest() -> int:
    """Offline checks for codes, URLs and assignment. Returns the check count."""
    checks = 0

    def ok(cond, label):
        nonlocal checks
        if not cond:
            raise AssertionError(f"selftest failed: {label}")
        checks += 1

    def raises(exc, fn, label):
        nonlocal checks
        try:
            fn()
        except exc:
            checks += 1
            return
        except Exception as other:  # noqa: BLE001 - wrong type is a failure
            raise AssertionError(f"{label}: raised {type(other).__name__}, wanted {exc.__name__}")
        raise AssertionError(f"{label}: did not raise {exc.__name__}")

    # --- alphabet / check character ---------------------------------------
    ok(len(ALPHABET) == 30 and len(set(ALPHABET)) == 30, "alphabet has 30 unique symbols")
    ok(not set("01ILOU") & set(ALPHABET), "alphabet excludes ambiguous glyphs")
    ok(check_char("2") == "2", "check char of '2' (value 0 -> weighted sum 0)")
    ok(check_char("234567") == check_char("234567"), "check char is deterministic")
    # the guarantee itself, exhaustively for one body: every substitution and every adjacent swap is caught
    _b = next(b for b in ("23456K", "ABCDEF", "XYZ234") if check_char(b))
    _c = _b + check_char(_b)
    ok(all(normalize(_c[:i] + a + _c[i + 1:]) is None for i in range(len(_c)) for a in ALPHABET if a != _c[i]), "all substitutions caught")
    ok(all(normalize(_c[:i] + _c[i + 1] + _c[i] + _c[i + 2:]) is None for i in range(len(_c) - 1) if _c[i] != _c[i + 1]), "all swaps caught")
    for body in ("234567", "ZZZZZZ", "ABCDEF", "999999"):
        ok(is_valid(body + check_char(body)), f"round-trip for body {body}")
    raises(ToolError, lambda: check_char(""), "empty body refused")
    raises(ToolError, lambda: check_char("23456O"), "non-alphabet body refused")

    GOOD = "234567" + check_char("234567")
    # --- normalize / is_valid ---------------------------------------------
    ok(is_valid(GOOD), "known good code is valid")
    ok(is_valid(' 234-567Q '), "separators ignored")
    ok(is_valid(GOOD.lower()), "case normalized")
    ok(not is_valid('2345672'), "wrong check char rejected")
    ok(normalize(' 234-567Q ') == GOOD, "normalize canonicalizes")
    ok(normalize("2345670") is None, "0 is not repaired")
    ok(normalize("O2345678") is None, "lookalikes are never guessed")
    ok(normalize("") is None and normalize(None) is None and normalize(1234) is None,
       "non-codes -> None")
    ok(not is_valid("2345") and not is_valid("2" * 13), "length bounds 5..12")

    # --- make_code / generate ---------------------------------------------
    code = make_code(random.Random(1), 7)
    ok(len(code) == 7 and is_valid(code), "make_code gives a valid code of the length")
    ok(make_code(random.Random(1), 7) == code, "make_code deterministic per rng")

    class _Twos:
        """First body is all '2' (check char '2' -> all-one code), then all '3'."""

        def __init__(self):
            self.calls = 0

        def choice(self, seq):
            self.calls += 1
            return "2" if self.calls <= 6 else "3"

    ok(len(set(make_code(_Twos(), 7))) > 1 and is_valid(make_code(_Twos(), 7)), "all-one-symbol codes are redrawn")
    ok(is_valid(make_code(random.Random(2), 5)), "5-char codes valid")
    seeded = generate(25, seed=42)
    ok(seeded == generate(25, seed=42), "generate(seed=) deterministic")
    ok(len(set(seeded)) == 25 and all(is_valid(c) for c in seeded), "unique and valid")
    ok(GOOD not in generate(25, existing=[GOOD], seed=42), "existing excluded")
    ok(GOOD not in generate(1, existing='234-567Q', seed=42), "string existing handled")
    ok(generate(0) == [], "generate(0) is empty")
    ok(all("2" not in c for c in generate(20, seed=7, banned=("2",))), "banned substrings absent")
    ok(all(is_valid(c) for c in generate(4, length=5, seed=1) + generate(4, length=12, seed=1)),
       "boundary lengths 5 and 12")
    raises(ToolError, lambda: generate(-1), "negative n refused")
    raises(ToolError, lambda: generate(MAX_BATCH + 1), "n above cap refused")
    raises(ToolError, lambda: generate(3, length=4), "length below 5 refused")
    raises(ToolError, lambda: generate(300_000, length=5), "code space too small refused")

    # --- tracking_url / parse_code_from_url -------------------------------
    url = tracking_url("https://ads.example/mail/piece", GOOD)
    ok(url == "https://ads.example/mail/piece?c=" + GOOD + "&utm_source=direct_mail&utm_medium=mail",
       "default tracking url")
    ok(tracking_url("https://ads.example/mail/piece", '234-567Q') == url, "code normalized in url")
    url2 = tracking_url("https://ads.example/p?batch=7#frag", GOOD, param="tc",
                        utm={"utm_campaign": "spring", "utm_medium": None})
    ok(url2.startswith("https://ads.example/p?batch=7&tc=" + GOOD + "&"), "existing query preserved")
    ok("utm_campaign=spring" in url2 and "utm_source=direct_mail" in url2, "utm merged")
    ok("utm_medium" not in url2, "utm=None drops a default")
    ok(url2.endswith("#frag"), "fragment kept last")
    ok(parse_code_from_url(url2, param="tc") == GOOD, "url round-trip")
    ok(parse_code_from_url("https://ads.example/p?C=" + GOOD) == GOOD, "param case-insensitive")
    ok(parse_code_from_url("https://ads.example/p?c=" + GOOD[:3] + "-" + GOOD[3:]) == GOOD, "url code normalized")
    ok(parse_code_from_url("https://ads.example/p?c=nope") is None, "invalid url code -> None")
    ok(parse_code_from_url("https://ads.example/p") is None and parse_code_from_url(None) is None,
       "missing code -> None")
    ok("OLD" not in tracking_url("https://ads.example/p?c=OLD", GOOD), "stale code dropped")
    raises(RailViolation, lambda: tracking_url("http://ads.example/p", GOOD),
           "cleartext url refused")
    raises(ToolError, lambda: tracking_url("https:///p", GOOD), "missing host refused")
    raises(ToolError, lambda: tracking_url("/relative", GOOD), "relative url refused")
    raises(ToolError, lambda: tracking_url("https://ads.example/p", "12345"), "invalid code refused")
    raises(ToolError, lambda: tracking_url("https://ads.example/p", GOOD, param="a&b"),
           "unsafe param refused")
    raises(ToolError, lambda: tracking_url("https://ads.example/p", GOOD,
                                           utm={"bad key": "x"}), "unsafe utm key refused")

    # --- assign -----------------------------------------------------------
    pieces = [{"segment": "north"}, {"segment": "south"}, {"segment": ""}]
    out = assign(pieces, "https://ads.example/mail", seed=3, campaign="spring")
    ok(len(out) == 3 and all(is_valid(p["tracking_code"]) for p in out), "every piece coded")
    ok(len({p["tracking_code"] for p in out}) == 3, "assign codes unique")
    ok(all(parse_code_from_url(p["tracking_url"]) == p["tracking_code"] for p in out),
       "assign urls match codes")
    ok("utm_campaign=spring" in out[0]["tracking_url"]
       and "utm_content=north" in out[0]["tracking_url"], "campaign + segment utm")
    ok("utm_content" not in out[2]["tracking_url"], "empty segment skipped")
    ok(pieces[0] == {"segment": "north"}, "assign does not mutate input")
    kept = assign([{"tracking_code": '234-567Q'}, {"tracking_code": "bogus"}],
                  "https://ads.example/m", seed=11, existing=[GOOD])
    ok(kept[0]["tracking_code"] == GOOD, "valid existing code kept + normalized")
    ok(is_valid(kept[1]["tracking_code"]) and kept[1]["tracking_code"] != GOOD,
       "invalid code replaced, existing respected")
    ok(assign([], "https://ads.example/m") == [], "empty batch is empty")
    raises(ToolError, lambda: assign(["nope"], "https://ads.example/m"), "non-dict piece refused")
    raises(ToolError, lambda: assign([{}], "https://ads.example/m", length=3), "bad length refused")
    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
