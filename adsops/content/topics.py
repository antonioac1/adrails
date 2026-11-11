"""Evidence-picked topics for the AdRails guarded content pipeline (0.12).

`plan()` turns a 28-day Search Console aggregate (plus the last stored SEO audit and the learned LESSONS weights) into a
short list of articles the pipeline is allowed to draft: "extend this page" comes first, "write this new page" only when
several safe question queries share a topic and no page ranks for them. Everything here is UNTRUSTED data — website
queries, model output, audit dicts — so strings are capped (MAX_TEXT chars), lists are capped (MAX_ROWS/MAX_PATHS items)
and every regex is built from `re.escape` of a literal, never from the data itself. Pure functions: no network, no
subprocess, no file writes, no clock reads.

Public API (SPEC-D, topics.py):

    plan(gsc_rows, profile, audit_run=None, lessons=None, published=(), limit=5)
                                        -> {"candidates": [...], "refused": [...]}
    safe_query(q, profile)              -> (bool, reason)   is this query safe to chase at all?
    is_question(q)                      -> bool             does the query read as a question?
    doorway_reason(slug, queries, existing_paths, profile)
                                        -> str | None       why this candidate is a doorway page, else None

Refusing is the default: a competitor, own brand, excluded area, five-digit zip, non-ASCII or stopword-only query never
becomes a candidate, a page on another host is ignored, a slug that only swaps a place/zip/keyword out of a page that
already exists is refused, and published work is skipped so the same idea is not drafted twice.
"""
import hashlib
import re
import string
from collections import Counter
from functools import lru_cache
from urllib.parse import urlsplit

from ..errors import ToolError

MAX_TEXT = 200_000                  # cap for every input string (chars)
MAX_ROWS = 500                      # cap for every input list of rows / items
MAX_PATHS = 2_000                   # cap for the existing-page list compared against
MAX_QUERIES = 8                     # queries kept on one candidate
MAX_LIMIT = 50                      # plan(limit=...) must be 1..50
MAX_LIST = 500                      # cap for the returned "refused" list
MIN_QUERY_LEN = 3
MAX_QUERY_LEN = 80
MIN_IMPRESSIONS = 5                 # striking-distance rows with fewer impressions are noise
POS_MIN = 8.0                       # "striking distance" band
POS_MAX = 40.0
MIN_NEW_CLUSTER = 3                 # a brand new page needs at least this many question queries
MIN_SHARED_WORDS = 2
POS_WEIGHT_TOP = 1.0                # best position <= 20
POS_WEIGHT_MID = 0.6                # best position <= 40
POS_WEIGHT_LOW = 0.4                # outside the band (audit-driven pages only)
CLASS_WEIGHT = {"question": 1.2, "striking": 1.0, "thin_page": 1.0}
AUDIT_CODES = ("starved_pages", "thin_content")
PUBLISHED_MAX_AGE_DAYS = 30
MAX_ABS_NUM = 1_000_000_000

_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9']+")
_ZIP_RE = re.compile(r"(?<![0-9])[0-9]{5}(?![0-9])")
_ALLOWED = frozenset(string.ascii_letters + string.digits + " '-?")
_ALNUM_FOLD_RE = re.compile(r"[^a-z0-9]+")
_PATH_COLLAPSE_RE = re.compile(r"/+")
_PATH_SPLIT_RE = re.compile(r"[-_/.]+")

_STOPWORDS = frozenset("""
a about after all also am an and any are as at be because been before being both but by can could did do does doing
for from get got had has have having he her here him his how i if in into is it its just like me more most much must my
no not now of off on once only or other our out over own same she should so some such than that the their them then
there these they this those through to too under until up us very was we were what when where which while who whom why
will with would you your
""".split())

_QUESTION_STARTS = frozenset("""
how what when where which who whom whose why is are was were am can could do does did will would should shall must may
might have has had if whether
""".split())

_QUESTION_ANYWHERE = frozenset("how why what when where which who whom whose whether".split())

_DOORWAY_STOPWORDS = frozenset("a an the of for in to and vs or".split())


# ----------------------------------------------------------------------------------------------------------------
# small, hostile-input-safe helpers
# ----------------------------------------------------------------------------------------------------------------
def _cap_str(value, limit=MAX_TEXT):
    """Return `value` as a stripped string, capped at `limit` chars ("" for anything that is not text)."""
    if not isinstance(value, str):
        return ""
    s = value.strip()
    return s if len(s) <= limit else s[:limit]


def _squash(value):
    """Whitespace-collapsed, capped text (still original case)."""
    return _WS_RE.sub(" ", _cap_str(value))


def _norm(value):
    """Whitespace-collapsed, lowercased text — the form every comparison uses."""
    return _squash(value).lower()


def _fold(value):
    """Lowercase alphanumeric-folded text: every non-alphanumeric run becomes one space."""
    return _ALNUM_FOLD_RE.sub(" ", _norm(value)).strip()


def _as_list(value, cap=MAX_ROWS):
    """A bounded list copy of `value` (empty for anything that is not a list/tuple)."""
    if not isinstance(value, (list, tuple)):
        return []
    return list(value[:cap])


def _int(value):
    """Bounded int or None (never raises, rejects bools/NaN/inf/absurd values)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if abs(value) > MAX_ABS_NUM:
            return None
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or abs(value) > MAX_ABS_NUM:
            return None
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            f = float(s)
        except (TypeError, ValueError):
            return None
        if f != f or f in (float("inf"), float("-inf")) or abs(f) > MAX_ABS_NUM:
            return None
        return int(f)
    return None


def _float(value):
    """Bounded float or None (never raises, rejects bools/NaN/inf/absurd values)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            f = float(s)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")) or abs(f) > MAX_ABS_NUM:
        return None
    return f


def _norm_path(path):
    """A path we can compare: leading slash, collapsed slashes, lowercased, trailing slash on directory paths."""
    p = _WS_RE.sub("", _cap_str(path, 2048)) or "/"
    p = p.lower()
    if not p.startswith("/"):
        p = "/" + p
    p = _PATH_COLLAPSE_RE.sub("/", p)
    tail = p.rstrip("/").split("/")[-1] if p.strip("/") else ""
    if not p.endswith("/") and "." not in tail:
        p += "/"
    return p


def _split_url(url):
    """(scheme, host, path) for a url or a bare path; never raises."""
    s = _cap_str(url, 2048)
    try:
        parts = urlsplit(s)
    except ValueError:
        return "https", "", _norm_path(s)
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        scheme = "https"
    return scheme, (parts.hostname or "").lower(), _norm_path(parts.path)


def _host(url):
    """Hostname of a url ("" when there is none)."""
    return _split_url(url)[1]


def _path(url):
    """Path of a url (a bare path is returned as a path)."""
    if not isinstance(url, str):
        return ""
    return _norm_path(url) if not url.strip().lower().startswith("http") else _split_url(url)[2]


def _page_url(url):
    """Canonical page url: scheme://host/path for a url, path-only for a bare path."""
    if not isinstance(url, str) or not url.strip():
        return ""
    if not url.strip().lower().startswith("http"):
        return _norm_path(url)
    scheme, host, path = _split_url(url)
    return "%s://%s%s" % (scheme, host, path) if host else path


def _page_key(url):
    """Host+path key: two urls of the same page (http/https/www aside) compare equal."""
    u = _page_url(url)
    return u.split("://", 1)[-1] if "://" in u else u


def _host_set(host):
    """{host, its www twin} for the on-site comparison."""
    h = _norm(host)
    if not h:
        return set()
    return {h, h[4:] if h.startswith("www.") else "www." + h}


def _on_site(url, hosts):
    """True when the url's host is the site host or its www twin (a bare path counts as on-site)."""
    if not isinstance(url, str) or not url.strip():
        return False
    if not url.strip().lower().startswith("http"):
        return url.strip().startswith("/")
    return _host(url) in hosts


def _origin(site, host):
    """scheme://host for building audit urls out of paths."""
    scheme, h, _ = _split_url(site)
    return "%s://%s" % (scheme, h or host or "")


@lru_cache(maxsize=1024)
def _phrase_rx(phrase):
    """Word-bounded matcher for one folded literal phrase, with optional plural on the last word."""
    words = [w for w in phrase.split() if w]
    if not words:
        return None
    parts = [re.escape(w) for w in words]
    parts[-1] = parts[-1] + r"(?:s|es)?"
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])")


def _phrase_hit(text, phrases):
    """The first phrase in `phrases` found in folded `text`, else None."""
    folded = _fold(text)
    for phrase in _as_list(phrases, 200):
        p = _fold(phrase)
        if not p:
            continue
        rx = _phrase_rx(p)
        if rx is not None and rx.search(folded):
            return p
    return None


def _rulings(profile):
    """(banned, excluded, competitors, own_brand) lists from a profile, each bounded and lowercased."""
    rulings = profile.get("rulings") if isinstance(profile, dict) else None
    if not isinstance(rulings, dict):
        rulings = {}

    def pick(key):
        return [_norm(x) for x in _as_list(rulings.get(key), 200) if _norm(x)]

    return pick("banned_phrases"), pick("excluded_areas"), pick("competitors"), pick("own_brand")


def _content_words(query):
    """Lowercased, non-stopword, non-trivial words of a query (capped)."""
    words = _WORD_RE.findall(_norm(query))
    return [w for w in words if len(w) > 1 and w not in _STOPWORDS][:40]


# ----------------------------------------------------------------------------------------------------------------
# public: is_question / safe_query
# ----------------------------------------------------------------------------------------------------------------
def is_question(query):
    """True when the query reads as a question ("how much...", "what is...?", "x vs y")."""
    s = _norm(query)
    if not s:
        return False
    if "?" in s:
        return True
    words = s.split()[:12]
    if words and words[0] in _QUESTION_STARTS:
        return True
    if " vs " in " %s " % s or " versus " in " %s " % s:
        return True
    return any(w in _QUESTION_ANYWHERE for w in words)


def safe_query(query, profile):
    """(True, "") when the query may be chased, else (False, plain-English reason)."""
    if not isinstance(query, str):
        return False, "query is not text"
    s = _squash(query)
    if not s:
        return False, "query is empty"
    if len(s) < MIN_QUERY_LEN:
        return False, "query is too short (%d chars)" % len(s)
    if len(s) > MAX_QUERY_LEN:
        return False, "query is too long (%d chars)" % len(s)
    for ch in s:
        if ch in _ALLOWED:
            continue
        if not ch.isascii():
            return False, "query has non-ascii characters"
        return False, "query has an unsupported character %r" % ch
    low = s.lower()
    banned, areas, competitors, own_brand = _rulings(profile)
    hit = _phrase_hit(low, banned)
    if hit:
        return False, "query contains a banned phrase (%r)" % hit[:60]
    hit = _phrase_hit(low, competitors)
    if hit:
        return False, "query names a competitor (%r)" % hit[:60]
    hit = _phrase_hit(low, own_brand)
    if hit:
        return False, "query names our own brand (%r)" % hit[:60]
    hit = _phrase_hit(low, areas)
    if hit:
        return False, "query is about an excluded area (%r)" % hit[:60]
    if _ZIP_RE.search(low):
        return False, "query contains a 5-digit zip code"
    if not _content_words(low):
        return False, "query is only stopwords"
    return True, ""


# ----------------------------------------------------------------------------------------------------------------
# public: doorway_reason
# ----------------------------------------------------------------------------------------------------------------
def _doorway_path(raw):
    """Normalise a path or URL for doorway comparison; return (path, error)."""
    s = _cap_str(raw, 2048)
    if not s:
        return "", "path is missing"
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        try:
            p = urlsplit(s).path
        except ValueError:
            p = s
    else:
        p = s
    p = p.lower()
    p = _PATH_COLLAPSE_RE.sub("/", p)
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/")
    p = p + "/" if p else "/"
    segs = [seg for seg in p.strip("/").split("/") if seg]
    for seg in segs:
        if seg in (".", ".."):
            return p, "path contains a '%s' segment" % seg
    return p, None


def _path_tokens(path):
    """The folded token set of a path."""
    body = path.strip("/")
    words = [w for w in _PATH_SPLIT_RE.split(body) if w]
    out = set()
    for word in words:
        w = word.lower()
        if w in _DOORWAY_STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        if w:
            out.add(w)
    return frozenset(out)


def _path_seq(path):
    """The folded tokens of a path, in order (same folding as _path_tokens)."""
    out = []
    for word in _PATH_SPLIT_RE.split(path.strip("/")):
        w = word.lower()
        if not w or w in _DOORWAY_STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.append(w)
    return out


# Folder-ish words that say nothing about the topic; they do not count as a shared template.
_GENERIC_TOKENS = frozenset("guide blog article resource page post news learn help".split())
# Audience words: "renters-insurance" vs "landlord-insurance" are different topics, not a doorway swap.
_AUDIENCE_TOKENS = frozenset("landlord tenant renter owner homeowner investor buyer seller manager agent "
                             "realtor association hoa board".split())
# Single-word US state names: "eviction-process-florida" is the same page with a place bolted on.
_STATE_TOKENS = frozenset("alabama alaska arizona arkansas california colorado connecticut delaware florida "
                          "georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland "
                          "massachusetts michigan minnesota mississippi missouri montana nebraska nevada ohio "
                          "oklahoma oregon pennsylvania tennessee texas utah vermont virginia washington "
                          "wisconsin wyoming".split())


def _slot_swap(a_seq, b_seq):
    """(a_slot, b_slot, shared) when the two token lists share a template and differ in ONE contiguous slot."""
    n = 0
    while n < len(a_seq) and n < len(b_seq) and a_seq[n] == b_seq[n]:
        n += 1
    m = 0
    while (m < len(a_seq) - n and m < len(b_seq) - n
           and a_seq[len(a_seq) - 1 - m] == b_seq[len(b_seq) - 1 - m]):
        m += 1
    a_slot = a_seq[n:len(a_seq) - m]
    b_slot = b_seq[n:len(b_seq) - m]
    shared = [t for t in a_seq[:n] + a_seq[len(a_seq) - m:] if t not in _GENERIC_TOKENS]
    return a_slot, b_slot, shared


def _place_tokens(profile):
    """Folded words of profile["places"] and profile["rulings"]["excluded_areas"]."""
    tokens = set()

    def add(value):
        if not isinstance(value, str):
            return
        for w in re.findall(r"[a-z0-9]+", value.lower()):
            if w in _DOORWAY_STOPWORDS:
                continue
            if len(w) > 3 and w.endswith("s"):
                w = w[:-1]
            if w:
                tokens.add(w)

    if not isinstance(profile, dict):
        return tokens
    places = profile.get("places")
    if isinstance(places, str):
        add(places)
    elif isinstance(places, (list, tuple)):
        for item in _as_list(places, 200):
            add(item)
    rulings = profile.get("rulings")
    if isinstance(rulings, dict):
        for area in _as_list(rulings.get("excluded_areas"), 200):
            add(area)
    return tokens


def _place_like(token, place_tokens):
    """True for a known place word, a state name, or any all-digit token (zip, year, number)."""
    return token in place_tokens or token in _STATE_TOKENS or token.isdigit()


def _template_doorway(path, other, place_tokens, topic_words):
    """Why `path` is `other` with one slot swapped or a place bolted on, else None (QA round 2 N2)."""
    a_slot, b_slot, shared = _slot_swap(_path_seq(path), _path_seq(other))
    if not shared or len(a_slot) > 3 or len(b_slot) > 3 or not (a_slot or b_slot):
        return None
    slot = a_slot + b_slot
    if all(_place_like(t, place_tokens) for t in a_slot) and all(_place_like(t, place_tokens) for t in b_slot) \
            and any(_place_like(t, place_tokens) for t in slot):
        what = "a place name, zip or number swapped" if a_slot and b_slot else "a place name or number"
        return "%s differs from %s only by %s" % (path[:80], other[:80], what)
    if not a_slot or not b_slot:
        return None                       # a topic word added ("eviction-process" -> "eviction-process-timeline")
    if any(t in _AUDIENCE_TOKENS or t in topic_words for t in slot):
        return None                       # a different audience or an owner-approved sibling topic
    if len(a_slot) == 1 and len(b_slot) == 1 and len(shared) >= 2:
        return ("%s is %s with one keyword swapped (%r for %r) - same template, likely a doorway; add the word to "
                "rulings.topic_words if these are genuinely different topics" % (path[:80], other[:80], a_slot[0],
                                                                                 b_slot[0]))
    if len(shared) >= 2 and not any(t in _AUDIENCE_TOKENS for t in shared):
        # a 1-3 word slot swapped inside a 2+ word template whose other instance is a live/candidate page
        return ("%s is %s with %r swapped for %r - same template, likely a place or keyword doorway; add the words "
                "to rulings.topic_words if these are genuinely different topics"
                % (path[:80], other[:80], "-".join(a_slot), "-".join(b_slot)))
    return None


def _place_swap(a_tokens, b_tokens, place_tokens):
    """True when the token sets differ by exactly one place-like token on each side."""
    left = a_tokens - b_tokens
    right = b_tokens - a_tokens
    if len(left) != 1 or len(right) != 1:
        return False
    a = next(iter(left))
    b = next(iter(right))
    return _place_like(a, place_tokens) and _place_like(b, place_tokens)


def doorway_reason(slug, queries, existing_paths, profile):
    """Plain-English reason this NEW slug is a doorway page, or None when it is a real new topic."""
    if not isinstance(slug, str) or not slug.strip():
        return "slug is missing"
    path, err = _doorway_path(slug)
    if err:
        return "slug %s" % err
    if path == "/":
        return "slug is empty"
    for seg in [s for s in path.strip("/").split("/") if s]:
        if seg.isdigit():
            return "slug has a digits-only segment (%r) - that is a zip/year page, not a topic" % path[:80]
    my_tokens = _path_tokens(path)
    place_tokens = _place_tokens(profile)
    rulings = profile.get("rulings") if isinstance(profile, dict) else None
    topic_words = set()
    if isinstance(rulings, dict):
        for w in _as_list(rulings.get("topic_words"), 200):
            if isinstance(w, str):
                topic_words.update(_path_seq(w.lower()))
    for raw in _as_list(existing_paths, MAX_PATHS):
        if not isinstance(raw, str) or not raw.strip():
            continue
        other, other_err = _doorway_path(raw)
        if other_err or not other:
            continue
        if other == path:
            return "an existing page already lives at %s" % other[:100]
        other_tokens = _path_tokens(other)
        if my_tokens == other_tokens:
            return "%s has the same topic tokens as existing page %s" % (path[:80], other[:80])
        if _place_swap(my_tokens, other_tokens, place_tokens):
            return "%s is %s with only a place name or zip swapped" % (path[:80], other[:80])
        why = _template_doorway(path, other, place_tokens, topic_words)
        if why:
            return why
        my_places = {t for t in my_tokens if _place_like(t, place_tokens)}
        other_places = {t for t in other_tokens if _place_like(t, place_tokens)}
        my_rest = my_tokens - my_places - _GENERIC_TOKENS
        if my_places and other_places and my_places != other_places and my_rest \
                and my_rest <= other_tokens - other_places:
            return ("%s is a place page (%s) on the same topic as place page %s"
                    % (path[:80], "-".join(sorted(my_places))[:40], other[:80]))
        if len(my_tokens - _GENERIC_TOKENS) >= 3 and my_tokens <= other_tokens:
            return "%s covers a subset of the topic of existing page %s" % (path[:80], other[:80])
    return None


# ----------------------------------------------------------------------------------------------------------------
# plan(): rows -> extend / new candidates
# ----------------------------------------------------------------------------------------------------------------
def _row(raw):
    """(parsed bounded GSC row, "") or (None, refusal reason for bad numeric fields)."""
    if not isinstance(raw, dict):
        return None, ""
    query = _squash(raw.get("query"))
    if not query:
        return None, ""
    page = _squash(raw.get("page"))

    imp_raw = raw.get("impressions")
    if imp_raw is None or (isinstance(imp_raw, str) and not imp_raw.strip()):
        impressions = 0
    else:
        impressions = _int(imp_raw)
        if impressions is None:
            return None, "row ignored: impressions is not a finite number"
        impressions = max(0, impressions)

    clicks_raw = raw.get("clicks")
    if clicks_raw is None or (isinstance(clicks_raw, str) and not clicks_raw.strip()):
        clicks = 0
    else:
        clicks = _int(clicks_raw)
        if clicks is None:
            return None, "row ignored: clicks is not a finite number"
        clicks = max(0, clicks)

    pos_raw = raw.get("position")
    if pos_raw is None or (isinstance(pos_raw, str) and not pos_raw.strip()):
        position = None
    else:
        position = _float(pos_raw)
        if position is None:
            return None, "row ignored: position is not a finite number"

    return {"query": query[:200], "page": page[:2048], "impressions": impressions,
            "clicks": clicks, "position": position}, ""


def _bad_row_entry(raw, reason):
    """Refused-list entry for a row rejected on numeric validation."""
    q = ""
    page = None
    if isinstance(raw, dict):
        q = _squash(raw.get("query"))[:120]
        p = _squash(raw.get("page"))
        page = p[:2048] or None
    return {"mode": "new", "page": page, "slug": None, "queries": [q] if q else [], "reason": reason}


def _refuse(bucket, seen, entry):
    """Record one refusal, deduplicated by (reason, queries)."""
    key = (entry.get("reason"), tuple(entry.get("queries") or ()))
    if key in seen:
        return
    seen.add(key)
    bucket.append(entry)


def _audit_urls(audit_run, origin):
    """Full urls of the pages a stored SEO audit flagged as starved or thin."""
    if not isinstance(audit_run, dict):
        return []
    rows = audit_run.get("rows")
    if not isinstance(rows, list):
        rows = audit_run.get("findings")
    out = []
    for row in _as_list(rows, MAX_ROWS):
        if not isinstance(row, dict) or row.get("code") not in AUDIT_CODES:
            continue
        for url in _as_list(row.get("urls"), MAX_ROWS):
            if not isinstance(url, str) or not url.strip():
                continue
            s = url.strip()
            if s.lower().startswith("http"):
                out.append(_page_url(s))
            elif s.startswith("/"):
                out.append(_page_url(origin + s))
    return out[:MAX_PATHS]


def _class_of(rows):
    """question when at least half of a page's target queries are questions, else striking."""
    if not rows:
        return "striking"
    if 2 * sum(1 for r in rows if is_question(r["query"])) >= len(rows):
        return "question"
    return "striking"


def _pos_weight(position):
    """Position weight of the score: 1.0 up to 20, 0.6 up to 40, 0.4 outside (audit-driven pages)."""
    if position is None:
        return POS_WEIGHT_MID
    if position <= 20.0:
        return POS_WEIGHT_TOP
    if position <= POS_MAX:
        return POS_WEIGHT_MID
    return POS_WEIGHT_LOW


def _lesson_weight(query_class, lessons):
    """Multiplier from the LESSONS file for "<content_type>|<query_class>" keys, clamped to 0.5..1.5."""
    if not isinstance(lessons, dict):
        return 1.0
    weights = lessons.get("weights")
    if not isinstance(weights, dict):
        return 1.0
    for key in sorted(k for k in weights if isinstance(k, str))[:200]:
        group = key.split("|")[-1].strip().lower()
        if group != query_class:
            continue
        value = _float(weights.get(key))
        if value is None:
            continue
        return max(0.5, min(1.5, value))
    return 1.0


def _id(mode, page, top_query):
    """Stable candidate id: sha1 of mode+page+top query, 10 hex chars."""
    raw = "%s|%s|%s" % (mode, page or "", top_query or "")
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]


def _reason(candidate):
    """One plain-English sentence saying why this candidate is on the list."""
    where = candidate["page"] or candidate["slug"]
    nq = len(candidate["queries"])
    verb = "extend" if candidate["mode"] == "extend" else "write"
    if candidate["query_class"] == "thin_page":
        head = "the stored audit flags %s as thin or starved content and it has %d safe queries" % (where, nq)
    elif candidate["mode"] == "new":
        head = "%d question queries with no ranking page share the topic %s" % (nq, candidate["slug"])
    else:
        head = "ranks on page 1-4 (best %.1f) for %d quer%s" % (candidate["best_position"], nq,
                                                                "y" if nq == 1 else "ies")
    return "%s; %d impressions in 28 days -> %s %s" % (head, candidate["impressions"], verb, where)


def _make(mode, page, slug, rows, query_class):
    """Build one candidate dict from the rows that support it."""
    picks = sorted(rows, key=lambda r: (-r["impressions"], r["query"]))
    names = []
    for row in picks:
        if row["query"] not in names:
            names.append(row["query"])
        if len(names) >= MAX_QUERIES:
            break
    positions = [r["position"] for r in rows if r["position"] is not None]
    best = round(min(positions), 2) if positions else 0.0
    candidate = {"id": _id(mode, page, names[0] if names else ""), "mode": mode, "page": page, "slug": slug,
                 "queries": names, "query_class": query_class,
                 "impressions": sum(r["impressions"] for r in rows), "best_position": float(best),
                 "score": 0.0, "reason": ""}
    candidate["reason"] = _reason(candidate)
    return candidate


def _clusters(rows):
    """Group question rows that share >= MIN_SHARED_WORDS content words (union-find, capped input)."""
    items, seen = [], set()
    for row in rows:
        if row["query"] in seen:
            continue
        seen.add(row["query"])
        items.append(row)
    items[:] = items[:MAX_ROWS]
    words = [_content_words(r["query"]) for r in items]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if len(set(words[i]) & set(words[j])) >= MIN_SHARED_WORDS:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    groups = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(items[i])
    return [g for g in groups.values() if len(g) >= MIN_NEW_CLUSTER]


def _cluster_words(rows):
    """The content words shared by at least two queries of a cluster, most common first (max 3)."""
    counts = Counter()
    for row in rows:
        counts.update(dict.fromkeys(_content_words(row["query"]), 1))   # ordered, not a set (hash-seed flake)
    shared = [(w, c) for w, c in counts.items() if c >= MIN_SHARED_WORDS]
    shared.sort(key=lambda pair: -pair[1])      # stable: ties keep first-appearance order
    return [w for w, _ in shared[:3]]


def _slug_from_words(words):
    """A clean /guides/<words>/ slug from cluster words."""
    parts = []
    for word in _as_list(words, 3):
        cleaned = re.sub(r"[^a-z0-9]+", "-", _norm(word)).strip("-")
        if cleaned and not cleaned.isdigit():
            parts.append(cleaned)
    if not parts:
        parts = ["topic"]
    return ("/guides/" + "-".join(parts) + "/")[:120]


def _published_set(published):
    """(ids, page keys/paths) of work already done, so the same idea is not drafted twice."""
    ids, pages = set(), set()

    def add_page(value):
        if not isinstance(value, str) or not value.strip():
            return
        s = value.strip()
        pages.add(_page_key(s))
        pages.add(_path(s))

    for item in _as_list(published, MAX_ROWS):
        if isinstance(item, str) and item.strip():
            s = item.strip()
            if s.lower().startswith("http") or s.startswith("/") or "/" in s:
                add_page(s)
            else:
                ids.add(s[:200])
        elif isinstance(item, dict):
            age = _float(item.get("days_ago"))
            if age is not None and age > PUBLISHED_MAX_AGE_DAYS:
                continue
            if isinstance(item.get("id"), str):
                ids.add(item["id"][:200])
            for key in ("page", "url", "slug"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    add_page(val)
    return ids, pages


def _is_published(candidate, done_ids, done_pages):
    """True when the candidate id, page path, or slug path is already published."""
    if candidate["id"] in done_ids:
        return True
    for value in (candidate.get("page"), candidate.get("slug")):
        if not isinstance(value, str) or not value.strip():
            continue
        if value in done_pages:
            return True
        key = _page_key(value)
        if key in done_pages:
            return True
        path = _path(value)
        if path and path in done_pages:
            return True
    return False


def plan(gsc_rows, profile, audit_run=None, lessons=None, published=(), limit=5):
    """Pick <= limit topics from the 28-day GSC aggregate; returns {"candidates": [...], "refused": [...]}."""
    if isinstance(gsc_rows, str):
        raise ToolError("gsc_rows must be a list of Search Console rows, not a single string")
    prof = profile if isinstance(profile, dict) else {}
    host = _norm(prof.get("host") or "") or _host(prof.get("site") or "")
    hosts = _host_set(host)
    origin = _origin(prof.get("site") or "", host)

    wanted = _int(limit)
    if wanted is None or wanted < 1 or wanted > MAX_LIMIT:
        raise ToolError("limit must be an integer between 1 and 50")

    refused, seen_refusals = [], set()
    rows = []
    for raw in _as_list(gsc_rows, MAX_ROWS):
        row, err = _row(raw)
        if row:
            rows.append(row)
        elif err:
            _refuse(refused, seen_refusals, _bad_row_entry(raw, err))

    safe_rows = []
    for row in rows:
        ok, why = safe_query(row["query"], prof)
        if ok:
            safe_rows.append(row)
        else:
            _refuse(refused, seen_refusals,
                    {"mode": "new", "page": row["page"] or None, "slug": None, "queries": [row["query"][:120]],
                     "reason": "query refused: %s" % why})

    on_site, no_page = [], []
    for row in safe_rows:
        if not row["page"]:
            no_page.append(row)
        elif _on_site(row["page"], hosts):
            row["page"] = _page_url(row["page"])
            on_site.append(row)

    by_page = {}
    for row in on_site:
        by_page.setdefault(_page_key(row["page"]), []).append(row)

    audit_urls = [u for u in _audit_urls(audit_run, origin) if u and _on_site(u, hosts)]
    existing = sorted({_path(row["page"]) for row in on_site} | {_path(u) for u in audit_urls})

    raw_candidates = []
    for key in sorted(by_page):
        group = by_page[key]
        striking = [r for r in group if r["position"] is not None and POS_MIN <= r["position"] <= POS_MAX
                    and r["impressions"] >= MIN_IMPRESSIONS]
        if not striking:
            continue
        page = striking[0]["page"]
        raw_candidates.append(_make("extend", page, _path(page), striking, _class_of(striking)))

    have = {_page_key(c["page"]) for c in raw_candidates if c["page"]}
    for url in audit_urls:
        key = _page_key(url)
        if key in have:
            continue
        group = by_page.get(key) or []
        if not group:
            continue
        have.add(key)
        candidate = _make("extend", url, _path(url), group, "thin_page")
        if candidate["impressions"] > 0:
            raw_candidates.append(candidate)

    pool = [r for r in no_page if r["position"] is None or r["position"] <= POS_MAX]
    for cluster in _clusters([r for r in pool if is_question(r["query"])]):
        if sum(r["impressions"] for r in cluster) < MIN_IMPRESSIONS:
            continue
        words = _cluster_words(cluster)
        if len(words) < MIN_SHARED_WORDS:
            continue
        raw_candidates.append(_make("new", None, _slug_from_words(words), cluster, "question"))

    for candidate in raw_candidates:
        pos = candidate["best_position"] if candidate["best_position"] else None
        candidate["score"] = round(candidate["impressions"] * _pos_weight(pos)
                                   * CLASS_WEIGHT.get(candidate["query_class"], 1.0)
                                   * _lesson_weight(candidate["query_class"], lessons), 4)
    raw_candidates.sort(key=lambda c: (-c["score"], c["id"]))

    done_ids, done_pages = _published_set(published)
    kept = []
    for candidate in raw_candidates:
        if candidate["impressions"] <= 0:
            continue
        if _is_published(candidate, done_ids, done_pages):
            continue
        if candidate["mode"] == "new":
            others = list(existing) + sorted(p for p in done_pages if isinstance(p, str) and p.startswith("/"))
            for kept_candidate in kept:
                if kept_candidate["slug"] and kept_candidate["slug"] != candidate["slug"]:
                    others.append(kept_candidate["slug"])
            why = doorway_reason(candidate["slug"], candidate["queries"], others, prof)
            if why:
                _refuse(refused, seen_refusals, dict(candidate, reason=why))
                continue
        kept.append(candidate)

    return {"candidates": kept[:wanted], "refused": refused[:MAX_LIST]}


# ----------------------------------------------------------------------------------------------------------------
# selftest
# ----------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    """Assert the contract above with in-memory data only; returns the number of checks."""
    n = 0

    def chk(cond, msg):
        nonlocal n
        if not cond:
            raise AssertionError("topics.selftest: " + msg)
        n += 1

    def raises_tool_error(fn):
        try:
            fn()
        except ToolError:
            return True
        return False

    P = {"site": "https://example.com/", "host": "example.com",
         "places": ["Shelbyville", "Springfield", "Ogdenville"],
         "rulings": {"banned_phrases": ["guaranteed approval"], "excluded_areas": ["Springfield"],
                     "competitors": ["Acme PM"], "own_brand": ["Example Co", "baytown pm"]}}

    # safe_query -------------------------------------------------------------------------------------------
    chk(safe_query("how much is landlord insurance", P) == (True, ""), "a plain question is a safe query")
    chk(safe_query("cost per door?", P)[0] is True, "'?' is allowed")
    chk(safe_query("what's an hoa fee", P)[0] is True, "an apostrophe is allowed")
    chk(safe_query("acme pm reviews", P)[0] is False, "a competitor query is refused")
    chk(safe_query("acme pms reviews", P)[0] is False, "a plural competitor variant is refused")
    chk("competitor" in safe_query("acme pm reviews", P)[1], "the competitor reason says why")
    chk(safe_query("acme landlord forum", P)[0] is True, "a partial competitor word is not a competitor hit")
    chk(safe_query("example co pricing", P)[0] is False, "the own brand is refused")
    chk(safe_query("is baytown-pm good", P)[0] is False, "a hyphenated own-brand variant is refused")
    chk(safe_query("guaranteed approval rental", P)[0] is False, "a banned phrase is refused")
    chk("banned" in safe_query("guaranteed approval rental", P)[1], "the banned reason says why")
    chk(safe_query("landlord help springfield", P)[0] is False, "an excluded area is refused")
    chk(safe_query("rental rates 90210", P)[0] is False, "a 5-digit zip is refused")
    chk(safe_query("caf\u00e9 landlord", P)[0] is False, "a non-ascii query is refused")
    chk(safe_query("ab", P)[0] is False, "a 2-char query is refused")
    chk(safe_query("a" * 90, P)[0] is False, "a 90-char query is refused")
    chk(safe_query("how to", P)[0] is False, "a stopword-only query is refused")
    chk(safe_query("", P)[0] is False and safe_query(None, P)[0] is False, "empty/None queries are refused")
    chk(safe_query("landlord <script>x", P)[0] is False, "markup characters are refused")
    chk(safe_query("hoa rules", None)[0] is True, "a missing profile still allows a plain query")

    # is_question ------------------------------------------------------------------------------------------
    chk(is_question("how much is landlord insurance") is True, "'how ...' is a question")
    chk(is_question("what is a fair rent?") is True, "'?' marks a question")
    chk(is_question("landlord insurance cost") is False, "a noun phrase is not a question")
    chk(is_question("renters vs landlords insurance") is True, "'vs' marks a question")
    chk(is_question(None) is False and is_question("") is False, "is_question of empty input is False")

    # extend preferred over new ----------------------------------------------------------------------------
    ext = {"query": "landlord insurance cost", "page": "https://example.com/guides/landlord-insurance-cost-guide/",
           "impressions": 100, "clicks": 3, "position": 12}
    new_qs = [{"query": "how much is renters insurance", "page": "", "impressions": 5, "clicks": 0, "position": 25},
              {"query": "what is renters insurance cost", "page": "", "impressions": 5, "clicks": 0, "position": 27},
              {"query": "why is renters insurance required", "page": "", "impressions": 5, "clicks": 0,
               "position": 31}]
    out = plan([ext] + new_qs, P)
    chk(len(out["candidates"]) == 2, "one extend and one new candidate")
    chk(out["candidates"][0]["mode"] == "extend", "extend is preferred over new")
    chk(out["candidates"][0]["score"] == 100.0, "score = impressions * position weight * class weight")
    chk(out["candidates"][1]["mode"] == "new" and out["candidates"][1]["page"] is None, "the new candidate has no page")
    chk(out["candidates"][1]["slug"] == "/guides/renters-insurance/", "the new slug comes from the shared words")
    chk(out["candidates"][1]["query_class"] == "question", "question class detected for new clusters")

    # sibling live pages must not refuse each other (M6) ---------------------------------------------------
    sibling_rows = [{"query": "renters insurance cost", "page": "https://example.com/guides/renters-insurance/",
                     "impressions": 100, "clicks": 0, "position": 12},
                    {"query": "landlord insurance cost", "page": "https://example.com/guides/landlord-insurance/",
                     "impressions": 90, "clicks": 0, "position": 13}]
    out = plan(sibling_rows, P)
    chk(len(out["candidates"]) == 2, "sibling extend pages are both allowed")
    chk([c["mode"] for c in out["candidates"]] == ["extend", "extend"], "both sibling pages are extends")

    # file-name pages can be extended (M7d) ----------------------------------------------------------------
    file_out = plan([{"query": "landlord forms", "page": "https://example.com/blog/post.html",
                      "impressions": 100, "clicks": 0, "position": 12}], P)
    chk(len(file_out["candidates"]) == 1, "file-name page can be extended")
    chk(file_out["candidates"][0]["slug"] == "/blog/post.html", "file-name slug keeps the path as-is")
    chk(file_out["candidates"][0]["page"] == "https://example.com/blog/post.html", "file-name page keeps the path as-is")

    # band + impression filters ----------------------------------------------------------------------------
    far = [{"query": "flat fee management", "page": "https://example.com/guides/fees/", "impressions": 500,
            "clicks": 1, "position": 55}]
    chk(plan(far, P)["candidates"] == [], "queries outside 8..40 are ignored")
    chk(plan([dict(far[0], position=3)], P)["candidates"] == [], "queries already ranking above 8 are ignored")
    chk(plan([dict(far[0], position=12, impressions=4)], P)["candidates"] == [],
        "queries with < 5 impressions are ignored")
    chk(plan([dict(far[0], position=12, impressions=100, page="https://other.com/guides/fees/")], P)["candidates"]
        == [], "pages on another host are ignored")
    chk(len(plan([dict(far[0], position=12, impressions=100, page="https://www.example.com/guides/fees/")], P)
            ["candidates"]) == 1, "the www twin of the site host counts as on-site")
    chk(plan([{"query": "flat fee management", "page": "https://example.com/guides/fees/", "impressions": 20,
               "clicks": 0, "position": 12}], P)["candidates"][0]["query_class"] == "striking",
        "a non-question extend is classed striking")

    # thin pages from the stored audit ---------------------------------------------------------------------
    thin_run = {"rows": [{"code": "thin_content", "urls": ["https://example.com/guides/thin-page/"]},
                         {"code": "other", "urls": ["https://example.com/guides/ignored/"]}]}
    thin_rows = [{"query": "hoa rules", "page": "https://example.com/guides/thin-page/", "impressions": 1,
                  "clicks": 0, "position": 60}]
    out = plan(thin_rows, P, audit_run=thin_run)
    chk(len(out["candidates"]) == 1, "a page the audit flagged becomes a candidate")
    chk(out["candidates"][0]["query_class"] == "thin_page", "audit pages are classed thin_page")
    chk(out["candidates"][0]["mode"] == "extend", "audit pages are extended, not rewritten")
    chk(out["candidates"][0]["slug"] == "/guides/thin-page/", "audit candidates keep the live slug")
    chk(plan(thin_rows, P, audit_run={"rows": [{"code": "thin_content", "urls": ["/guides/thin-page/"]}]})
        ["candidates"][0]["query_class"] == "thin_page", "path-only audit urls work too")
    chk(plan(thin_rows, P)["candidates"] == [], "without the audit the starved page is not picked")

    zero_thin_rows = [{"query": "hoa rules", "page": "https://example.com/guides/thin-page/", "impressions": 0,
                       "clicks": 0, "position": 60}]
    chk(plan(zero_thin_rows, P, audit_run=thin_run)["candidates"] == [],
        "a zero-impression audit candidate is skipped")

    # doorway refusals (M7) --------------------------------------------------------------------------------
    chk(doorway_reason("/service-areas/shelbyville/", [], ["/service-areas/springfield/"], P) is not None,
        "a place-swapped slug is refused")
    chk(doorway_reason("/areas/90210/", [], [], P) is not None, "a zip slug is refused")
    chk(doorway_reason("/guides/flat-fee/", [], ["/guides/rental-guide/"], P) is None, "a distinct slug is allowed")
    chk(doorway_reason("/guides/flat-fee/", [], ["https://example.com/guides/flat-fee/"], P) is not None,
        "an existing slug is refused")
    chk(doorway_reason("/guides/flat-fee/", [], [], P) is None, "no existing pages -> not a doorway")
    chk(doorway_reason("", [], [], P) is not None, "a missing slug is refused")
    chk(doorway_reason("/guides/insurance-renters/", [], ["/guides/renters-insurance/"], P) is not None,
        "reordered tokens are refused")
    chk(doorway_reason("/guides//flat-fee//", [], ["/guides/flat-fee/"], P) is not None,
        "repeated slashes are normalised and refused")
    chk(doorway_reason("/guides/renters-insurance/", [], ["/guides/landlord-insurance/"], P) is None,
        "one-word non-place differences are distinct topics")
    chk(doorway_reason("/guides/the-renters-insurance/", [], ["/guides/renters-insurance/"], P) is not None,
        "stopwords and plural folding are applied")
    chk(doorway_reason("/guides/../flat-fee/", [], [], P) is not None, "a '..' segment is refused")
    chk(doorway_reason("/guides/./flat-fee/", [], [], P) is not None, "a '.' segment is refused")
    why = doorway_reason("/areas/shelbyville/", [], ["/areas/springfield/"], P)
    chk(why is not None and "place" in why, "the place-swap refusal says why")

    swap_rows = [{"query": "ogdenville rental laws", "page": "https://example.com/areas/ogdenville/",
                  "impressions": 50, "clicks": 1, "position": 10},
                 {"query": "shelbyville rental rules", "page": "https://example.com/areas/shelbyville/",
                  "impressions": 40, "clicks": 1, "position": 11}]
    out = plan(swap_rows, P)
    chk([c["mode"] for c in out["candidates"]] == ["extend", "extend"],
        "extend pages are never doorway-refused")

    # lessons weights --------------------------------------------------------------------------------------
    pair = [{"query": "renters insurance pricing guide", "page": "https://example.com/guides/renters-insurance/",
             "impressions": 100, "clicks": 5, "position": 12},
            {"query": "how much do renters insurance costs", "page": "https://example.com/answers/renters-cost/",
             "impressions": 80, "clicks": 2, "position": 14}]
    base = plan(pair, P)
    chk([c["mode"] for c in base["candidates"]] == ["extend", "extend"], "both rows become extend candidates")
    chk(base["candidates"][0]["queries"][0] == "renters insurance pricing guide", "the base order is by score")
    boosted = plan(pair, P, lessons={"weights": {"guide|question": 1.5}})
    chk(boosted["candidates"][0]["queries"][0].startswith("how much"), "a lesson weight flips the order")
    chk(boosted["candidates"][0]["score"] > boosted["candidates"][1]["score"], "candidates are sorted by score desc")
    chk(plan(pair, P, lessons={"weights": {"guide|question": 9}})["candidates"][0]["score"] <= 80 * 1.2 * 1.5,
        "lesson weights are clamped to 1.5")

    # published --------------------------------------------------------------------------------------------
    first_id = base["candidates"][0]["id"]
    out = plan(pair, P, published=[first_id])
    chk(len(out["candidates"]) == 1 and out["candidates"][0]["id"] != first_id, "published ids are skipped")
    out = plan(pair, P, published=["https://example.com/guides/renters-insurance/"])
    chk(len(out["candidates"]) == 1, "published pages are skipped")
    chk(len(plan(pair, P, published=[{"id": first_id, "days_ago": 45}])["candidates"]) == 2,
        "work published more than 30 days ago is not skipped")

    chk(plan(new_qs, P, published=["https://example.com/guides/renters-insurance/"])["candidates"] == [],
        "published page paths skip new candidates")
    chk(plan(new_qs, P, published=["/guides/renters-insurance/"])["candidates"] == [],
        "published site paths skip new candidates")
    new_id = plan(new_qs, P)["candidates"][0]["id"]
    chk(plan(new_qs, P, published=[new_id])["candidates"] == [], "published ids skip new candidates")

    # stable ids + limit -----------------------------------------------------------------------------------
    again = plan(pair, P)
    chk([c["id"] for c in again["candidates"]] == [c["id"] for c in base["candidates"]], "ids are stable")
    chk(re.match(r"^[0-9a-f]{10}$", first_id) is not None, "ids are sha1 hex[:10]")
    chk(len(plan(pair, P, limit=1)["candidates"]) == 1, "limit caps the list")
    chk(raises_tool_error(lambda: plan(pair, P, limit=0)), "limit below 1 is a ToolError")
    chk(raises_tool_error(lambda: plan(pair, P, limit=10 ** 6)), "limit above 50 is a ToolError")
    chk(raises_tool_error(lambda: plan([], P, limit="1e400")), "non-finite limit is a ToolError")
    chk(len(plan(pair, P, limit=50)["candidates"]) == 2, "limit up to 50 is accepted")

    # zero-impression candidates (M7c) ---------------------------------------------------------------------
    zero_new = [dict(q, impressions=0) for q in new_qs]
    chk(plan(zero_new, P)["candidates"] == [], "zero-impression new clusters are skipped")
    low_new = [dict(q, impressions=1) for q in new_qs]
    chk(plan(low_new, P)["candidates"] == [], "a new cluster needs at least 5 impressions in total")

    # hostile input ----------------------------------------------------------------------------------------
    chk(plan(None, None) == {"candidates": [], "refused": []}, "plan(None, None) is empty, not an exception")
    chk(plan([{"query": None}, "junk", 5, {"impressions": 9}], P)["candidates"] == [], "malformed rows are ignored")
    refused = plan([{"query": "acme pm", "page": "", "impressions": 9, "position": 9}], P)["refused"]
    chk(len(refused) == 1 and "refused" in refused[0]["reason"], "an unsafe query is recorded as refused")
    raised = False
    try:
        plan("not a list", P)
    except ToolError:
        raised = True
    chk(raised, "a string gsc_rows is a caller error (ToolError)")
    chk(len(plan([dict(ext) for _ in range(600)], P)["candidates"]) == 1,
        "600 rows are capped and collapse into one candidate")
    chk(isinstance(plan([{"query": "x" * 300, "page": "", "impressions": 5, "position": 9}], P), dict),
        "an over-long query does not raise")
    chk(plan([{"query": "flat fee management", "page": "", "impressions": 5, "position": 9}], P)["candidates"] == [],
        "a single query with no page is not enough for a new page")

    # non-finite / absurd row numbers (M8) -----------------------------------------------------------------
    over = plan([{"query": "hoa rules", "page": "", "impressions": "1e400", "position": 9}], P)
    chk(over["candidates"] == [], "overflow impressions do not raise")
    chk(any("finite" in r["reason"] for r in over["refused"]), "overflow impressions are refused with a reason")
    nan_out = plan([{"query": "hoa rules", "page": "", "impressions": float("nan"), "position": 9}], P)
    chk(nan_out["candidates"] == [], "nan impressions do not raise")
    inf_out = plan([{"query": "hoa rules", "page": "", "impressions": float("inf"), "position": 9}], P)
    chk(inf_out["candidates"] == [], "inf impressions do not raise")

    # QA round 2 N2: same template + place/zip/keyword swap, vs live slugs and vs other candidates -----------
    for new_slug, live, refused_ in [
            ("/property-management/clearwater/", "/property-management/tampa/", True),
            ("/tampa-property-management/", "/orlando-property-management/", True),
            ("/tampa/property-management/", "/orlando/property-management/", True),
            ("/landlord-insurance-tampa/", "/landlord-insurance-orlando/", True),
            ("/property-management/st-petersburg/", "/property-management/tampa/", True),
            ("/rent-prices-33613/", "/rent-prices-33612/", True),
            ("/guides/eviction-process-florida/", "/guides/eviction-process/", True),
            ("/guides/flat-fee-pricing/", "/guides/flat-fee-contracts/", True),
            ("/guides/security-deposit-rules/", "/guides/lease-renewal-rules/", False),
            ("/guides/hoa-rules-for-landlords/", "/guides/hoa-rules-for-tenants/", False),
            ("/guides/how-much-rent-can-i-charge/", "/guides/how-to-set-rent/", False),
            ("/guides/eviction-process-timeline/", "/guides/eviction-process/", False)]:
        chk((doorway_reason(new_slug, [], [live], P) is not None) == refused_,
            "N2 %s vs %s refused=%s" % (new_slug, live, refused_))
    PW = dict(P, rulings=dict(P.get("rulings") or {}, topic_words=["pricing"]))
    chk(doorway_reason("/guides/flat-fee-pricing/", [], ["/guides/flat-fee-contracts/"], PW) is None,
        "rulings.topic_words allows an owner-approved sibling topic")
    PP = dict(P, places=["Tampa", "Clearwater", "St Petersburg"])
    chk(doorway_reason("/guides/property-st-petersburg/", [], ["/property-management/tampa/"], PP) is not None,
        "a question-cluster slug naming a known place is refused against a place page on the same topic")
    city_rows = lambda city: [{"query": q % city, "page": "", "impressions": 10, "position": 22}
                              for q in ("how much does landlord insurance cost in %s",
                                        "what does landlord insurance cover in %s",
                                        "is landlord insurance required in %s")]
    live_city = [{"query": "property management tampa cost", "impressions": 80, "position": 14,
                  "page": "https://example.com/property-management/tampa/"}]
    out_n2 = plan(live_city, P)
    chk([c["mode"] for c in out_n2["candidates"]] == ["extend"], "extends are never doorway-blocked")
    out_pub = plan(city_rows("tampa"), P,
                   published=["https://example.com/guides/how-much-does-landlord-insurance-cost-in-tampa/"])
    chk(all(c["mode"] != "new" or "tampa" not in c["slug"] for c in out_pub["candidates"]),
        "a near-slug of a published page is refused")
    return n
