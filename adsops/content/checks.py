"""Deterministic checks for the AdRails guarded content pipeline (adsops/content, 0.12).

`run(draft, ctx)` is the last line of defence between a model-written draft and a publisher. It reads nothing but
the draft and the CheckContext (profile, facts text, verified quotes, corpus, known paths) and returns the shared
CheckResult dict: `{"ok", "errors", "warnings", "stats"}` where `ok` is exactly `errors == []`.

Everything handled here - page text, model output, quotes, corpus pages - is UNTRUSTED data:

  * no eval/exec, no regex is ever built from input (profile rulings are re.escape'd), every string is capped at
    MAX_TEXT chars and every list at MAX_LIST items, so a hostile 50 MB body cannot hang a check;
  * the ten checks are deterministic and pure: no network, no subprocess, no file writes, no clock beyond the
    `today` string the caller passes in (used only to size the year window of the number trace);
  * `run()` never raises: a draft of the wrong shape becomes SCHEMA errors, and an unexpected exception is turned
    into a single SCHEMA error (fail closed - the caller sees "not ok", never a traceback).

Checks (SPEC-D, checks.py): SCHEMA, the number trace (numbers.trace + numbers.word_statistics on the visible text
plus facts/quotes/labels as the allowed set), BANNED_PHRASE / EXCLUDED_AREA / COMPETITOR / SUPERLATIVE /
URL_IN_TEXT / RAW_HTML, DUPLICATE (+ CORPUS_INCOMPLETE, fail closed), THIN, the extend-only RETENTION /
LIVE_CHANGES / UNDECLARED_CHANGE, JSONLD, AUTHOR, INTERNAL_LINK and SOURCE_DOMAIN. In extend mode the wording that
already lives on the page is not re-ruled: only sentences that are not in `ctx["live_text"]` are checked as text.

Public helpers: `visible_text(draft)`, `strip_markdown(md)`, `md_links(md)`.
"""
import html as html_mod
import json
import re
from urllib.parse import urlsplit

from ..errors import ToolError, scrub
from . import numbers

MAX_TEXT = 200_000                  # cap for any string we touch
MAX_LIST = 500                      # cap for any list we touch / return
MAX_BODY = 60_000                   # body_md hard cap (SPEC-D)
MAX_TITLE = 70
MAX_DESC = 170
MAX_FAQ = 8
MAX_SOURCES = 40
MAX_LINKS = 50
MAX_JSONLD = 20_000
MAX_CORPUS = 500                    # corpus pages compared per run
MAX_SNIPPET = 160                   # untrusted text quoted back in a message
SHINGLE_K = 5
MSG = 400                           # message cap
DEFAULT_SITE = ""
DEFAULT_SMALL_MAX = 12
DEFAULT_MIN_WORDS = {"new": 700, "extend": 150}
DEFAULT_EXTEND = {"min_retention": 0.85, "max_changed_sentences": 3}
DEFAULT_DUP = {"jaccard": 0.30, "containment": 0.50}

MODES = ("new", "extend")
CONTENT_TYPES = ("answer-extension", "guide", "faq", "comparison")

_SUPER_RE = re.compile(r"(?<![\w#])(?:#\s?1|no\.?\s?1|number[\s-]+one)(?![\w])", re.I)   # PM (GLM checks #15)
SUPERLATIVES = ("best", "#1", "number one", "leading", "top-rated", "top rated", "premier", "unmatched", "unbeatable",
                "guaranteed", "guarantee", "most trusted", "highest rated", "world-class")

# ── total helpers (never raise, never touch the outside world) ────────────────────────────────────────────────
_SLUG_RE = re.compile(r"\A/[a-z0-9/-]{1,120}/\Z")
_FILE_SLUG_RE = re.compile(r"\A/[a-z0-9/_-]{0,180}[a-z0-9_-]\.(?:html?|php|aspx?)\Z")   # QA minor: /blog/post.html
_FENCE_RE = re.compile(r"```.*?```", re.S)
_LINK_RE = re.compile(r"!?\[([^\]\n]{0,500})\]\(([^()\s]{0,2000})(?:\s+\"[^\"]{0,500}\")?\)")
_LINK_TITLE_RE = re.compile(r"\]\([^()\s]{0,2000}\s+\"([^\"]{0,500})\"\)")
_REFDEF_RE = re.compile(r"^[ \t]{0,3}\[[^\]\n]{1,200}\]:[ \t]*\S+", re.M)
_HEAD_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
_BULLET_RE = re.compile(r"^[ \t]{0,3}(?:[-+*]|\d{1,3}[.)])[ \t]+", re.M)
_RULE_RE = re.compile(r"^[ \t]{0,3}(?:-[ \t]*){3,}$|^[ \t]{0,3}(?:\*[ \t]*){3,}$", re.M)
_URL_RE = re.compile(r"(?:https?://|www\.)", re.I)
_TLDS = ("com|net|org|io|co|us|gov|edu|info|biz|dev|app|ai|xyz|site|online|shop|store|me|tv|ly|uk|ca|au|de|fr|es|it"
         "|nl|ru|cn|jp|br|in|se|no|fi|dk|ch|at|be|nz|za|ie|pl|pt|gr|tr|mx|ar|cl|kr|sg|hk|tw|il|ae|sa")
_BARE_DOMAIN_RE = re.compile(r"(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+(?:" + _TLDS + r")(?![\w\-])",
                             re.I)
_HTML_RE = re.compile(r"<(?![a-z][a-z0-9+.\-]*:)[a-z/!]", re.I)          # "<div>", "</p>", "<!--", but not "<https:"
_BY_RE = re.compile(r"(?<![\w])By[ \t]+([A-Z][a-zA-Z\u2019'\-]{1,30}(?:[ \t]+[A-Z][a-zA-Z\u2019'\-]{1,30}){0,2})")
_TODAY_RE = re.compile(r"\A(\d{4})-\d{2}-\d{2}\Z")


def _text(value, limit=MAX_TEXT):
    """Coerce UNTRUSTED input to a capped str; None / unusable objects become "" and never raise."""
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


def _mlist(value):
    """`value` when it is a list/tuple, else []."""
    return value if isinstance(value, (list, tuple)) else []


def _mdict(value):
    """`value` when it is a dict, else {}."""
    return value if isinstance(value, dict) else {}


def _num(value, default):
    """`value` as float, or `default` for anything that is not a real number (bools excluded)."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _int(value, default=0):
    """`value` as int, or `default` for anything that is not a real number (bools excluded)."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _strings(value, limit=200):
    """-> list of non-empty capped strings from a str, list or tuple (capped at MAX_LIST)."""
    if isinstance(value, str):
        out = [_text(value, limit)]
    elif isinstance(value, (list, tuple)):
        out = [_text(item, limit) for item in list(value)[:MAX_LIST]]
    else:
        out = []
    return [s for s in out if s]


def _clip(text, limit=MAX_SNIPPET):
    """Clip untrusted text for a message, on a word boundary where possible."""
    s = " ".join(_text(text, MAX_TEXT).split())
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut + "…"


def _snippet(haystack, start, end, pad=60):
    """A short window of `haystack` around [start, end), whitespace-collapsed and clipped."""
    lo = max(0, start - pad)
    hi = min(len(haystack), end + pad)
    return _clip(" ".join(haystack[lo:hi].split()), MAX_SNIPPET)


def _phrase_re(phrase):
    """Word-boundary regex for an UNTRUSTED phrase; the phrase itself is re.escape'd (never a pattern)."""
    escaped = re.escape(phrase)
    if escaped[:1].isalnum() or escaped[:1] == "_":
        escaped = r"(?<!\w)" + escaped
    if escaped[-1:].isalnum() or escaped[-1:] == "_":
        escaped = escaped + r"(?!\w)"
    return re.compile(escaped, re.I)


def _year_window(today):
    """-> (year-1, year+1) from a YYYY-MM-DD string, or None (numbers.trace then uses the current year)."""
    m = _TODAY_RE.match(_text(today, 20).strip())
    if not m:
        return None
    year = int(m.group(1))
    return (year - 1, year + 1)


# ── markdown helpers (public per SPEC-D) ──────────────────────────────────────────────────────────────────────
def strip_markdown(md):
    """Visible text of a markdown body: fences, images, link TARGETS, heading/quote/list/rule syntax and inline
    emphasis markers are removed; link TEXT and all prose are kept. Capped at MAX_BODY + slack."""
    s = _text(md, MAX_BODY + 100_000)
    if not s:
        return ""
    s = _FENCE_RE.sub(lambda m: " " + m.group(0).strip("`") + " ", s)   # PM (GLM checks #1): fence content is visible
    s = html_mod.unescape(s)                    # PM (GLM #5): "Chea&#112;est" renders as "Cheapest"
    s = _LINK_RE.sub(lambda m: m.group(1), s)
    s = _HEAD_RE.sub("", s)
    s = _QUOTE_RE.sub("", s)
    s = _RULE_RE.sub(" ", s)
    s = _BULLET_RE.sub(" ", s)
    s = s.replace("`", " ").replace("*", " ").replace("~~", " ").replace("|", " ").replace("\\", " ")
    return s[:MAX_BODY + 100_000]


def md_links(md):
    """-> [{"text": str, "target": str}] for every inline markdown link/image in body (capped at MAX_LIST)."""
    out = []
    for m in _LINK_RE.finditer(_text(md, MAX_BODY + 100_000)):
        out.append({"text": _text(m.group(1), 500), "target": _text(m.group(2), 2000)})
        if len(out) >= MAX_LIST:
            break
    return out


def visible_text(draft):
    """Everything a reader sees: title, description, markdown body (syntax stripped), every FAQ q/a and the "after"
    side of every live change. Capped at MAX_TEXT characters."""
    if not isinstance(draft, dict):
        return ""
    parts = [_text(draft.get("title"), 500), _text(draft.get("description"), 2000),
             strip_markdown(draft.get("body_md"))]
    for item in _mlist(draft.get("faq"))[:MAX_FAQ]:
        if isinstance(item, dict):
            parts.append(_text(item.get("q"), 1000))
            parts.append(_text(item.get("a"), 4000))
    for item in _mlist(draft.get("live_changes"))[:MAX_LIST]:
        if isinstance(item, dict):
            parts.append(_text(item.get("after"), 4000))
    for item in _mlist(draft.get("internal_links"))[:MAX_LIST]:   # PM: anchors become visible text on other pages
        if isinstance(item, dict):
            parts.append(_text(item.get("anchor"), 300))
    parts.extend(_jsonld_values(draft.get("jsonld")))   # PM (GLM #4): JSON-LD prices/ratings/claims are traced too
    for m in _LINK_TITLE_RE.finditer(_text(draft.get("body_md"), MAX_BODY + 100_000)):   # link tooltips are visible too
        parts.append(m.group(1))
    return html_mod.unescape("\n".join(_text(p, 20000) for p in parts if p))


def _jsonld_values(obj, depth=0, out=None):
    """Every string and number inside a JSON-LD object (keys that are URLs/ids/types excluded), as text lines."""
    out = [] if out is None else out
    if depth > 8 or len(out) > 400:
        return out
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:200]:
            if k in ("@context", "@type", "@id", "url", "sameAs", "image", "logo", "mainEntityOfPage"):
                continue
            _jsonld_values(v, depth + 1, out)
    elif isinstance(obj, list):
        for v in obj[:200]:
            _jsonld_values(v, depth + 1, out)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out.append(str(obj))
    elif isinstance(obj, str):
        out.append(obj)
    return out


# ── shingle similarity ────────────────────────────────────────────────────────────────────────────────────────
def _shingles(text, k=SHINGLE_K):
    """-> set of k-word shingles over already-normalised text (shorter texts give one whole-text shingle)."""
    words = _text(text, MAX_TEXT).split()
    if not words:
        return set()
    kk = k if 1 <= k <= 20 else SHINGLE_K
    if len(words) <= kk:
        return {" ".join(words)}
    return {" ".join(words[i:i + kk]) for i in range(len(words) - kk + 1)}


def _jaccard(a, b):
    """Jaccard similarity of two shingle sets (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0     # PM: was inverted (always 0.0); found by the live Baytown run


def _containment(a, b):
    """Share of `a` that also appears in `b` (0.0 when `a` is empty)."""
    if not a:
        return 0.0
    return len(a & b) / len(a)


# ── url helpers ──────────────────────────────────────────────────────────────────────────────────────────────
def _url_key(url):
    """Lowercased URL without fragment/query and without a trailing slash (comparison key)."""
    s = _text(url, 2000).strip().lower().split("#")[0].split("?")[0]
    return s.rstrip("/")


def _norm_path(raw):
    """Normalise a site path for known_paths comparison: keep the path, add a trailing slash unless the last
    segment looks like a file ("/about" -> "/about/", "/x.html" stays "/x.html")."""
    s = _text(raw, 2000).strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(("http://", "https://")):
        s = urlsplit(s).path or "/"
    elif low.startswith("//"):
        s = urlsplit("https:" + s).path or "/"
    s = s.split("#")[0].split("?")[0]
    if not s.startswith("/"):
        s = "/" + s
    last = s.rstrip("/").rsplit("/", 1)[-1]
    if not s.endswith("/") and "." not in last:
        s = s + "/"
    return s


def _site_host(site):
    """Host of the profile site ('' when unusable)."""
    try:
        return (urlsplit(_text(site, 500)).hostname or "").lower()
    except Exception:
        return ""


def _internal_target(target, site):
    """-> the site path when `target` points inside the site, else None (external, anchor, mailto, ...)."""
    t = _text(target, 2000).strip()
    if not t or t.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    low = t.lower()
    if low.startswith(("http://", "https://", "//")):
        absolute = ("https:" + t) if low.startswith("//") else t
        try:
            host = (urlsplit(absolute).hostname or "").lower()
            path = urlsplit(absolute).path or "/"
        except Exception:
            return None
        mine = _site_host(site)
        if mine and host and (host == mine or host == "www." + mine or "www." + host == mine):
            return path
        return None
    return t


def _host_allowed(host, allowed):
    """True when `host` matches an allowed domain entry ("*.gov" = any subdomain of gov; else host or subdomain)."""
    h = _text(host, 300).strip().lower().rstrip(".")
    if not h:
        return False
    for raw in _strings(allowed, 300):
        entry = raw.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("*."):
            base = entry[2:]
            if base and (h == base or h.endswith("." + base)):
                return True
        elif h == entry or h.endswith("." + entry):
            return True
    return False


# ── profile / context normalisation (missing keys fall back to the spec defaults) ─────────────────────────────
def _cfg(ctx):
    """-> normalised settings from ctx["profile"], every optional key falling back to the SPEC-D default."""
    profile = _mdict(_mdict(ctx).get("profile"))
    rulings = _mdict(profile.get("rulings"))
    author = _mdict(profile.get("author"))
    site = _text(profile.get("site"), 500).strip() or DEFAULT_SITE
    host = _text(profile.get("host"), 300).strip().lower() or _site_host(site)
    if site and not site.endswith("/"):
        site = site + "/"
    min_words = _mdict(profile.get("min_words"))
    extend = _mdict(profile.get("extend"))
    dup = _mdict(profile.get("max_duplicate"))
    allowed = _strings(profile.get("allowed_source_domains"), 300)
    if not allowed and host:
        allowed = [host]
    return {
        "site": site,
        "host": host,
        "author": author,
        "banned_phrases": [numbers.norm(p) for p in _strings(rulings.get("banned_phrases"), 200)],
        "excluded_areas": _strings(rulings.get("excluded_areas"), 100),
        "competitors": _strings(rulings.get("competitors"), 200),
        "allowed_source_domains": allowed,
        "small_number_max": _int(profile.get("small_number_max"), DEFAULT_SMALL_MAX),
        "min_words_new": _int(min_words.get("new"), DEFAULT_MIN_WORDS["new"]),
        "min_words_extend": _int(min_words.get("extend"), DEFAULT_MIN_WORDS["extend"]),
        "min_retention": _num(extend.get("min_retention"), DEFAULT_EXTEND["min_retention"]),
        "max_changed": _int(extend.get("max_changed_sentences"), DEFAULT_EXTEND["max_changed_sentences"]),
        "max_jaccard": _num(dup.get("jaccard"), DEFAULT_DUP["jaccard"]),
        "max_containment": _num(dup.get("containment"), DEFAULT_DUP["containment"]),
    }


def _own_urls(site, slug):
    """URL keys of the draft's own page (its live version must not count as a duplicate of itself)."""
    origin = _text(site, 500).rstrip("/")
    path = " " + _text(slug, 300)
    base = origin + path.strip()
    return {_url_key(base)}                     # PM: the home page was always excluded from the duplicate check


def _known_paths(ctx):
    """-> set of normalised site paths that exist (ctx["known_paths"], plus the site root)."""
    known = set()
    for raw in _mlist(_mdict(ctx).get("known_paths"))[:5000]:   # PM: was capped at 500 (big sites failed links)
        path = _norm_path(raw)
        if path:
            known.add(path)
    known.add("/")
    return known


# ── the checks ───────────────────────────────────────────────────────────────────────────────────────────────
def _schema(draft, cfg, err):
    """Check 1 (SCHEMA) and structural sanitation. -> (mode, slug, body_md, faq, sources, changes, links, jsonld)."""
    mode = _text(draft.get("mode"), 20).strip().lower()
    if mode not in MODES:
        err("SCHEMA", 'draft.mode must be "new" or "extend", not "{}"'.format(_clip(mode or draft.get("mode"), 40)))
        mode = "new"
    slug = _text(draft.get("slug"), 300).strip()
    if not slug:
        err("SCHEMA", "draft.slug is required (for example /guides/flat-fee/)")
    elif ".." in slug or "//" in slug or not (_SLUG_RE.match(slug) or (mode == "extend" and _FILE_SLUG_RE.match(slug))):
        err("SCHEMA", 'draft.slug "{}" must look like /lower/case/path/ (no dots, max 120 chars)'
            .format(_clip(slug, 80)))
    ctype = _text(draft.get("content_type"), 40).strip().lower()
    if ctype not in CONTENT_TYPES:
        err("SCHEMA", 'draft.content_type must be one of {}, not "{}"'.format(", ".join(CONTENT_TYPES),
                                                                           _clip(ctype, 40)))
    title = _text(draft.get("title"), 1000)
    if not title.strip():
        err("SCHEMA", "draft.title is required")
    elif len(title) > MAX_TITLE:
        err("SCHEMA", "draft.title is {} characters; the limit is {}".format(len(title), MAX_TITLE))
    description = _text(draft.get("description"), 2000)
    if draft.get("description") is not None and not isinstance(draft.get("description"), str):
        err("SCHEMA", "draft.description must be a string")
    elif len(description) > MAX_DESC:
        err("SCHEMA", "draft.description is {} characters; the limit is {}".format(len(description), MAX_DESC))
    raw_body = draft.get("body_md")
    if not isinstance(raw_body, str):
        err("SCHEMA", "draft.body_md must be a string of markdown")
    body = _text(raw_body, MAX_BODY + 2000)
    if len(body) > MAX_BODY:
        err("SCHEMA", "draft.body_md is {} characters; the limit is {}".format(len(body), MAX_BODY))
    faq = []
    for item in _mlist(draft.get("faq"))[:MAX_FAQ + 1]:
        if not isinstance(item, dict):
            err("SCHEMA", "every draft.faq entry must be an object with q and a")
            continue
        if not isinstance(item.get("q"), str) or not isinstance(item.get("a"), str):
            err("SCHEMA", "every draft.faq entry needs a string q and a string a")
            continue
        faq.append({"q": _text(item.get("q"), 1000), "a": _text(item.get("a"), 4000)})
    if len(_mlist(draft.get("faq"))) > MAX_FAQ:
        err("SCHEMA", "draft.faq has {} entries; the limit is {}".format(len(_mlist(draft.get("faq"))), MAX_FAQ))
    sources = []
    for item in _mlist(draft.get("sources"))[:MAX_SOURCES + 1]:
        if not isinstance(item, dict):
            err("SCHEMA", "every draft.sources entry must be an object with url and quote")
            continue
        url = _text(item.get("url"), 2000)
        quote = _text(item.get("quote"), 2000)
        if not url:
            err("SCHEMA", "every draft.sources entry needs a url")
            continue
        sources.append({"url": url, "quote": quote})
    if len(_mlist(draft.get("sources"))) > MAX_SOURCES:
        err("SCHEMA", "draft.sources has {} entries; the limit is {}"
            .format(len(_mlist(draft.get("sources"))), MAX_SOURCES))
    changes = []
    for item in _mlist(draft.get("live_changes"))[:MAX_LIST]:
        if not isinstance(item, dict):
            err("SCHEMA", "every draft.live_changes entry must be an object with before, after and reason")
            continue
        if not isinstance(item.get("before"), str) or not isinstance(item.get("after"), str):
            err("SCHEMA", "every draft.live_changes entry needs string before and after")
            continue
        changes.append({"before": _text(item.get("before"), 2000), "after": _text(item.get("after"), 2000),
                        "reason": _text(item.get("reason"), 500)})
    links = []
    for item in _mlist(draft.get("internal_links"))[:MAX_LIST]:
        if not isinstance(item, dict):
            err("SCHEMA", "every draft.internal_links entry must be an object with from and anchor")
            continue
        if not isinstance(item.get("from"), str) or not isinstance(item.get("anchor"), str):
            err("SCHEMA", "every draft.internal_links entry needs a string from and anchor")
            continue
        links.append({"from": _text(item.get("from"), 500), "anchor": _text(item.get("anchor"), 300)})
    jsonld = draft.get("jsonld")
    if jsonld is not None and not isinstance(jsonld, dict):
        err("SCHEMA", "draft.jsonld must be an object or null")
        jsonld = None
    return {"mode": mode, "slug": slug, "body": body, "faq": faq, "sources": sources, "changes": changes,
            "links": links, "jsonld": jsonld, "title": title, "description": description, "ctype": ctype}


def _run(draft, ctx, err, warn, stats, errors_so_far=lambda: []):
    """The ten checks. Reports through err()/warn() and fills `stats`."""
    cfg = _cfg(ctx)
    if not isinstance(draft, dict):
        err("SCHEMA", "the draft must be a JSON object")
        return
    d = _schema(draft, cfg, err)
    mode, slug, body = d["mode"], d["slug"], d["body"]

    visible = visible_text(draft)
    if len(visible) > MAX_TEXT:                  # PM (GLM checks #8): never check a silently truncated text
        err("SCHEMA", "the draft's visible text is longer than {} characters".format(MAX_TEXT))
        return
    raw_live = _mdict(ctx).get("live_text")
    if isinstance(raw_live, str) and len(raw_live) > MAX_TEXT:
        err("SCHEMA", "the live page text is longer than {} characters; it cannot be compared safely".format(MAX_TEXT))
        return
    if len(_mlist(draft.get("live_changes"))) > MAX_LIST or len(_mlist(draft.get("internal_links"))) > MAX_LIST:
        err("SCHEMA", "too many live_changes or internal_links")
        return
    if re.search(r"\]\(\s*<?\s*(?:javascript|data|vbscript|file):", _text(draft.get("body_md"), MAX_BODY + 100_000), re.I):
        err("INTERNAL_LINK", "a link uses a javascript:, data:, vbscript: or file: target")
    if _REFDEF_RE.search(_text(draft.get("body_md"), MAX_BODY + 100_000)):
        err("INTERNAL_LINK", "reference-style link definitions ([x]: url) are not allowed; use inline links")
    live_text = _text(raw_live, MAX_TEXT)
    live_norm = numbers.norm(live_text)

    # 2/3. text-under-rule: in extend mode the legacy wording of the live page is not re-ruled.
    if mode == "extend":
        if not live_norm:
            err("SCHEMA", 'mode "extend" needs ctx.live_text (the current page text) to measure retention')
        under = "\n".join(numbers.new_sentences(visible, live_text))
    else:
        under = visible

    # 2. number trace + spelled-out statistics ------------------------------------------------------------
    allowed = "\n".join([_text(_mdict(ctx).get("facts_text"), MAX_TEXT),
                         "\n".join(_strings(_mdict(ctx).get("verified_quotes"), MAX_TEXT // 4)),
                         "\n".join(_strings(_mdict(ctx).get("source_labels"), 300))])
    for e in numbers.trace(under, allowed, small_max=cfg["small_number_max"],
                           years=_year_window(_mdict(ctx).get("today"))):
        err(e["code"], e["msg"])
    for e in numbers.word_statistics(under):
        err(e["code"], e["msg"])

    # 3. ruling checks on the visible text ---------------------------------------------------------------
    low_text = numbers.norm(under)
    low = low_text
    # PM (lesson from REVIEW-C F1): verified source quotes are published in the bundle too, so the rulings that are
    # about WHAT the page says (banned phrases, excluded areas, competitors) also run on them.
    low = low + "\n" + numbers.norm("\n".join(_strings(_mdict(ctx).get("verified_quotes"), MAX_TEXT // 4)))
    for phrase in cfg["banned_phrases"]:
        phrase = numbers.norm(phrase)            # PM: config phrases were compared un-normalised
        if phrase and phrase in low:
            err("BANNED_PHRASE", 'the banned phrase "{}" appears in the draft'.format(_clip(phrase, 120)))
    for area in cfg["excluded_areas"]:
        if not numbers.norm(area):
            continue
        m = _phrase_re(numbers.norm(area)).search(low)
        if m:
            err("EXCLUDED_AREA", 'the excluded area "{}" is mentioned: "{}"'
                .format(_clip(area, 80), _snippet(low, m.start(), m.end())))
    for name in cfg["competitors"]:
        if not numbers.norm(name):
            continue
        m = _phrase_re(numbers.norm(name)).search(low)
        if m:
            err("COMPETITOR", 'the competitor "{}" is named: "{}"'
                .format(_clip(name, 80), _snippet(low, m.start(), m.end())))
    folded = re.sub(r"[^a-z0-9]+", " ", low)    # PM (GLM checks #14): "Acme-PM", "guaranteed-returns"
    for code, items in (("BANNED_PHRASE", cfg["banned_phrases"]), ("COMPETITOR", cfg["competitors"]),
                        ("EXCLUDED_AREA", cfg["excluded_areas"])):
        for item in items:
            f_item = re.sub(r"[^a-z0-9]+", " ", numbers.norm(item)).strip()
            already = any(e["code"] == code and _clip(numbers.norm(item), 120) in e["msg"].lower() for e in errors_so_far())
            if f_item and not already and re.search(r"(?<![a-z0-9])" + re.escape(f_item) + r"(?![a-z0-9])", folded):
                err(code, '"{}" appears in the draft (punctuation ignored)'.format(_clip(item, 120)))
    m = _SUPER_RE.search(low_text)
    if m:
        err("SUPERLATIVE", 'the superlative "{}" is not allowed'.format(_clip(m.group(0), 40)))
    for word in SUPERLATIVES:                   # our own words only: an official quote may say "best practice"
        m = _phrase_re(word).search(low_text)
        if m:
            err("SUPERLATIVE", 'the superlative "{}" is not allowed: "{}"'
                .format(_clip(word, 40), _snippet(low_text, m.start(), m.end())))
    m = _URL_RE.search(under)
    if m:
        err("URL_IN_TEXT", 'a URL is written in the text: "{}"'.format(_snippet(under, m.start(), m.end())))
    else:
        m = _BARE_DOMAIN_RE.search(numbers.norm(under))
        if m:
            err("URL_IN_TEXT", 'a domain is written in the text: "{}"'.format(_clip(m.group(0), 120)))
    m = _HTML_RE.search(under)
    if m:
        err("RAW_HTML", 'raw HTML is not allowed in the body: "{}"'.format(_snippet(under, m.start(), m.end())))

    # 4. duplicate + corpus completeness -----------------------------------------------------------------
    draft_sh = _shingles(numbers.norm(strip_markdown(body)))           # the page body
    vis_sh = _shingles(numbers.norm(visible))   # PM (GLM checks #17): FAQ/title/description count for duplicates too
    corpus = _mdict(_mdict(ctx).get("corpus"))
    if _mdict(ctx).get("corpus_complete") is not True:
        err("CORPUS_INCOMPLETE", "the site corpus was not fetched completely, so duplicates cannot be ruled out")
    if len(corpus) > MAX_CORPUS:                 # PM: never compare a silently truncated corpus
        err("CORPUS_INCOMPLETE", "the site corpus has {} pages; at most {} are compared".format(len(corpus), MAX_CORPUS))
    own = _own_urls(cfg["site"], slug)
    worst = 0.0
    top = []
    for url, page_text in list(corpus.items())[:MAX_CORPUS]:
        page_url = _text(url, 2000)
        if not page_url or _url_key(page_url) in own:
            continue
        if isinstance(page_text, str) and len(page_text) > MAX_TEXT:      # PM (GLM checks #18): fail closed
            err("CORPUS_INCOMPLETE", '"{}" is longer than {} characters and cannot be compared'.format(
                _clip(page_url, 200), MAX_TEXT))
            continue
        page_sh = _shingles(numbers.norm(_text(page_text, MAX_TEXT)))
        if not page_sh or not draft_sh:
            continue
        jac = max(_jaccard(draft_sh, page_sh), _jaccard(vis_sh, page_sh))
        con = max(_containment(draft_sh, page_sh), _containment(vis_sh, page_sh))
        worst = max(worst, jac)
        top.append({"url": page_url, "jaccard": round(jac, 4), "containment": round(con, 4)})
        if jac > cfg["max_jaccard"] or con > cfg["max_containment"]:
            err("DUPLICATE", 'this draft repeats "{}" (jaccard {:.2f}, containment {:.2f})'
                .format(_clip(page_url, 200), jac, con))
    top.sort(key=lambda row: (-row["jaccard"], -row["containment"]))
    stats["max_jaccard"] = round(worst, 4)
    stats["dup_top"] = top[:5]

    # 5. thin content ------------------------------------------------------------------------------------
    words = len(visible.split())
    stats["words"] = words
    if mode == "extend":
        added = numbers.new_sentences(strip_markdown(body), live_text) if live_norm else []
        words_added = sum(len(s.split()) for s in added)
        stats["words_added"] = words_added
        if live_norm and words_added < cfg["min_words_extend"]:
            err("THIN", "this extension adds {} words; at least {} new words are required"
                .format(words_added, cfg["min_words_extend"]))
    elif words < cfg["min_words_new"]:
        err("THIN", "the draft has {} words; at least {} are required".format(words, cfg["min_words_new"]))

    # 6. extend: retention, changed sentences, declared changes -------------------------------------------
    if mode == "extend" and live_norm:
        live_sh = _shingles(numbers.norm(strip_markdown(live_text)))
        retention = _containment(live_sh, draft_sh)
        stats["retention"] = round(retention, 4)
        if live_sh and retention < cfg["min_retention"]:
            err("RETENTION", "only {:.0%} of the current page text survives; at least {:.0%} must be kept"
                .format(retention, cfg["min_retention"]))
        # PM (GLM checks #16): a live sentence moved into the description or an anchor is still removed from the page
        new_norm = numbers.norm("\n".join([strip_markdown(body)] + [_text(f.get("q"), 1000) + "\n" + _text(f.get("a"), 4000)
                                                                    for f in _mlist(draft.get("faq")) if isinstance(f, dict)]))
        changed = []
        for sentence in numbers.sentences(strip_markdown(live_text)):
            key = numbers.norm(sentence).strip(' .!?,;:"()[]\'-\u2019')
            if key and key not in new_norm:
                changed.append(sentence)
        stats["changed_sentences"] = len(changed)
        if len(changed) > cfg["max_changed"]:
            err("LIVE_CHANGES", "{} live sentences were changed; at most {} may change"
                .format(len(changed), cfg["max_changed"]))
        declared = [numbers.norm(c["before"]).strip(' .!?,;:"()[]\'-\u2019') for c in d["changes"]]
        for sentence in changed:
            key = numbers.norm(sentence).strip(' .!?,;:"()[]\'-\u2019')
            ok = False
            for before in declared:
                if not before:
                    continue
                if before == key or before in key or key in before:
                    ok = True
                    break
            if not ok:
                err("UNDECLARED_CHANGE", 'this live sentence was removed but not declared in live_changes: "{}"'
                    .format(_clip(sentence)))

    # 7. JSON-LD -----------------------------------------------------------------------------------------
    jsonld = d["jsonld"]
    if jsonld is not None:
        if "@context" not in jsonld or "@type" not in jsonld:
            err("JSONLD", 'draft.jsonld needs both "@context" and "@type"')
        try:
            blob = json.dumps(jsonld, ensure_ascii=False)
        except (TypeError, ValueError):
            err("JSONLD", "draft.jsonld cannot be serialised to JSON")
            blob = ""
        if len(blob) > MAX_JSONLD:
            err("JSONLD", "draft.jsonld is {} characters; the limit is {}".format(len(blob), MAX_JSONLD))

    # 8. author ------------------------------------------------------------------------------------------
    author = cfg["author"]
    author_id = _text(author.get("@id") or author.get("id"), 500).strip()
    author_name = _text(author.get("name"), 300).strip()
    if isinstance(jsonld, dict):
        for obj in _author_objects(jsonld.get("author")):
            aid = _text(obj.get("@id"), 500).strip()
            name = _text(obj.get("name"), 300).strip()
            if aid and author_id and aid != author_id:
                err("AUTHOR", 'draft.jsonld author "@id" is "{}"; it must be "{}"'
                    .format(_clip(aid, 200), _clip(author_id, 200)))
            if name and author_name and numbers.norm(name) != numbers.norm(author_name):
                err("AUTHOR", 'draft.jsonld author "{}" is not the approved author "{}"'
                    .format(_clip(name, 120), _clip(author_name, 120)))
    if author_name:
        for m in _BY_RE.finditer(_text(visible, MAX_TEXT)):
            found = numbers.norm(m.group(1))
            if found and found != numbers.norm(author_name):
                err("AUTHOR", 'the body credits "{}" as the author but the approved author is "{}"'
                    .format(_clip(m.group(1), 120), _clip(author_name, 120)))

    # 9. internal links ----------------------------------------------------------------------------------
    known = _known_paths(ctx)
    links = md_links(body)
    if len(links) > MAX_LINKS:
        err("INTERNAL_LINK", "the body has {} links; the limit is {}".format(len(links), MAX_LINKS))
    for link in links[:MAX_LINKS]:
        tgt = link["target"].strip()
        low_t = tgt.lower()
        scheme = low_t.split(":", 1)[0] if ":" in low_t.split("/", 1)[0] else ""
        bad = (scheme not in ("", "http", "https")) or (low_t.startswith("//") and _internal_target(tgt, cfg["site"]) is None)
        if bad:                                  # PM (GLM checks #3/#4): javascript:, data:, //other-host
            err("INTERNAL_LINK", '"{}" uses a link target that is not allowed: "{}"'.format(_clip(link["text"], 80),
                                                                                         _clip(tgt, 120)))
            continue
        path = _internal_target(link["target"], cfg["site"])
        if path is None:
            host = (urlsplit(tgt).hostname or "").lower() if low_t.startswith(("http://", "https://")) else ""
            if host and not _host_allowed(host, cfg["allowed_source_domains"]):   # PM (GLM checks #3/#4)
                err("INTERNAL_LINK", '"{}" links to {}, which is not the site or an allowed source domain'.format(
                    _clip(link["text"], 80), _clip(host, 120)))
            continue
        norm = _norm_path(path)
        if norm and norm not in known:
            err("INTERNAL_LINK", '"{}" links to "{}", which is not a known page on this site'
                .format(_clip(link["text"], 80), _clip(link["target"], 200)))
    for link in d["links"]:
        norm = _norm_path(link["from"])
        if norm and norm not in known:
            err("INTERNAL_LINK", 'internal_links asks "{}" to link here, but that page does not exist'
                .format(_clip(link["from"], 200)))

    # 10. sources ----------------------------------------------------------------------------------------
    for source in d["sources"]:
        try:
            parts = urlsplit(source["url"])
            scheme = (parts.scheme or "").lower()
            host = (parts.hostname or "").lower()
        except Exception:
            err("SOURCE_DOMAIN", 'the source "{}" is not a usable URL'.format(_clip(source["url"], 200)))
            continue
        if scheme != "https":
            err("SOURCE_DOMAIN", 'the source "{}" must be https'.format(_clip(source["url"], 200)))
        elif not _host_allowed(host, cfg["allowed_source_domains"]):
            err("SOURCE_DOMAIN", 'the source host "{}" is not an allowed source domain'
                .format(_clip(host or source["url"], 200)))
    vq = [numbers.norm(q) for q in _strings(_mdict(ctx).get("verified_quotes"), MAX_TEXT // 4)]
    for source in d["sources"]:                  # PM (GLM checks #19): every cited quote must be one that was verified
        qn = numbers.norm(source.get("quote"))
        if vq and qn and not any(qn == v or qn in v for v in vq):
            err("QUOTE_NOT_FOUND", 'the quote cited for "{}" was not verified against the fetched source'.format(
                _clip(source["url"], 200)))
    if d["sources"] and not vq:
        err("SOURCES_INCOMPLETE", "the draft cites {} sources but none of them was verified against the fetched page"
            .format(len(d["sources"])))

    # warnings (never fatal) -----------------------------------------------------------------------------
    if d["ctype"] == "guide" and not d["faq"]:
        warn("FAQ_MISSING", "this guide has no FAQ; a short FAQ usually earns the answer box")
    if len(d["links"]) < 2:
        warn("FEW_INTERNAL_LINKS", "only {} internal link(s) planned; at least 2 are recommended"
             .format(len(d["links"])))
    if len(d["description"]) < 70:
        warn("SHORT_DESCRIPTION", "the description is {} characters; 70-170 works best in search results"
             .format(len(d["description"])))


def _author_objects(value):
    """-> list of author objects from a JSON-LD "author" value (dict, list or plain string)."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        return [{"name": value}]
    out = []
    for item in _mlist(value)[:20]:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({"name": item})
    return out


def run(draft, ctx):
    """Run every deterministic check on `draft` against `ctx` -> the shared CheckResult dict.

    `ok` is True only when `errors` is empty. `stats` carries words, words_added, retention, changed_sentences,
    max_jaccard and dup_top. Hostile input never raises: it becomes SCHEMA errors (fail closed).
    """
    ctx = _mdict(ctx)
    errors = []
    warnings = []
    stats = {"words": 0, "words_added": None, "retention": None, "changed_sentences": None,
             "max_jaccard": 0.0, "dup_top": []}

    def err(code, msg):
        if len(errors) < MAX_LIST:
            errors.append({"code": code, "msg": scrub(msg, MSG)})

    def warn(code, msg):
        if len(warnings) < MAX_LIST:
            warnings.append({"code": code, "msg": scrub(msg, MSG)})

    try:
        _run(draft, ctx, err, warn, stats, lambda: errors)
    except Exception as exc:                      # a check must never crash a pipeline
        err("SCHEMA", "the draft could not be checked ({}); it is not approved".format(type(exc).__name__))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


# ── selftest ─────────────────────────────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    n = 0

    def chk(cond, msg):
        nonlocal n
        assert cond, "checks.selftest: " + msg
        n += 1

    fill = ("Flat fee management keeps the owner in control of the budget and the scope of work stays plain. ")
    body = " ".join(fill for _ in range(45))                      # ~810 words, no digits, no rulings
    quote = "Rental income is reported on Schedule E for the tax year."
    live_words = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
                  "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango")

    def line(word):
        return "The {} client update covers the budget and the scope of work.".format(word)

    live_long = " ".join(line(w) for w in live_words)             # 20 sentences with no digits
    live_nums = ("We have served 1,788 owners since 1998. " + line("alpha") + " " + line("bravo") + " "
                 + line("charlie") + " " + line("delta"))

    profile = {
        "site": "https://example.com/", "host": "example.com",
        "author": {"name": "Jane Doe", "url": "/team/jane/", "id": "https://example.com/team/jane/#person"},
        "rulings": {"banned_phrases": ["guaranteed returns"], "excluded_areas": ["Springfield"],
                    "competitors": ["Acme PM"], "own_brand": ["Example Co"]},
        "allowed_source_domains": ["irs.gov", "*.gov", "example.com"],
        "facts_file": "FACTS.json", "lessons_file": "LESSONS.md",
    }

    def ctx(**over):
        base = {"profile": profile, "facts_text": "Management is $149 per month per home.",
                "verified_quotes": [quote], "source_labels": ["www.irs.gov"],
                "corpus": {"https://example.com/something-else/": "A page about parking permits and street sweeping."},
                "corpus_complete": True, "live_text": None,
                "known_paths": ["/", "/about/", "/guides/flat-fee/", "/pricing/"], "today": "2026-11-10"}
        base.update(over)
        return base

    def draft(**over):
        base = {"mode": "new", "slug": "/guides/flat-fee/", "title": "Flat-Fee Property Management Guide",
                "description": "A plain guide to flat fee property management: what the fee covers and how owners "
                               "keep control of the budget and the scope.",
                "content_type": "guide", "body_md": body,
                "faq": [{"q": "What does the fee cover?", "a": "The agreed scope of work, listed in writing."}],
                "sources": [{"url": "https://www.irs.gov/x", "quote": quote}],
                "live_changes": [],
                "internal_links": [{"from": "/", "anchor": "flat fee guide"},
                                   {"from": "/about/", "anchor": "our guide"}],
                "jsonld": {"@context": "https://schema.org", "@type": "Article",
                           "author": {"@id": "https://example.com/team/jane/#person", "name": "Jane Doe"}}}
        base.update(over)
        return base

    def codes(result):
        return {e["code"] for e in result["errors"]}

    # helpers --------------------------------------------------------------------------------------------
    chk(strip_markdown("See [our pricing](/pricing/) now.") .strip().startswith("See our pricing now"),
        "strip_markdown drops link targets and keeps link text")
    chk("http" not in strip_markdown("![alt](/img/x.png)"), "strip_markdown drops image targets")
    chk(strip_markdown("## Title\n\n- a\n- b").startswith("Title"), "strip_markdown drops heading and list syntax")
    chk(md_links("a [one](/x/) and [two](https://y.example/z)") ==
        [{"text": "one", "target": "/x/"}, {"text": "two", "target": "https://y.example/z"}], "md_links finds targets")
    chk(md_links(None) == [] and strip_markdown(None) == "", "markdown helpers survive None")
    chk("What does" in visible_text(draft()), "visible_text includes the FAQ")
    chk("Schedule E" not in visible_text(draft()), "visible_text excludes source quotes")
    chk(visible_text(None) == "" and visible_text({}) == "", "visible_text survives junk")

    # clean draft ----------------------------------------------------------------------------------------
    res = run(draft(), ctx())
    chk(set(res) == {"ok", "errors", "warnings", "stats"}, "CheckResult has exactly ok/errors/warnings/stats")
    chk(res["errors"] == [], "a clean draft has no errors: {}".format([e["code"] for e in res["errors"]]))
    chk(res["ok"] is True, "a clean draft passes")
    chk(res["ok"] == (res["errors"] == []), "ok is exactly errors == []")
    chk(res["warnings"] == [], "a clean draft raises no warnings")
    chk(set(res["stats"]) == {"words", "words_added", "retention", "changed_sentences", "max_jaccard", "dup_top"},
        "stats carries the documented keys")
    chk(res["stats"]["words"] >= 700, "stats.words counts the visible words")
    chk(res["stats"]["words_added"] is None and res["stats"]["retention"] is None, "new mode has no extend stats")
    chk(res["stats"]["max_jaccard"] < 0.05, "an unrelated corpus gives a low max_jaccard")
    chk(run(draft(mode="new", live_changes=[]), ctx())["stats"]["dup_top"][0]["url"].endswith("something-else/"),
        "dup_top names the closest corpus page")
    chk(run(draft(sources=[], jsonld=None), ctx(facts_text="", verified_quotes=[]))["ok"],
        "a draft with no sources needs no quotes")

    # SCHEMA ---------------------------------------------------------------------------------------------
    chk("SCHEMA" in codes(run("not a draft", ctx())), "a non-object draft is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(title=""), ctx())), "an empty title is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(title="T" * 71), ctx())), "a title over 70 chars is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(description="D" * 171), ctx())), "a description over 170 chars is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(content_type="blog"), ctx())), "an unknown content_type is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(mode="rewrite"), ctx())), "an unknown mode is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(slug="/Guides/Flat Fee/"), ctx())), "a malformed slug is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(slug="/guides/../etc/"), ctx())), "a slug with .. is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(body_md="x" * 61_000), ctx())), "a body over 60k chars is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(body_md=None), ctx())), "a missing body is a SCHEMA error")
    chk("SCHEMA" in codes(run(draft(faq=[{"q": "a", "a": "b"}] * 9), ctx())), "more than 8 FAQ entries is SCHEMA")
    chk("SCHEMA" in codes(run(draft(sources=[{"url": "https://www.irs.gov/x", "quote": quote}] * 41), ctx())),
        "more than 40 sources is SCHEMA")
    chk("SCHEMA" in codes(run(draft(mode="extend", body_md=body), ctx(live_text=None))),
        "extend without live_text errors")

    # number trace ---------------------------------------------------------------------------------------
    r = run(draft(body_md=body + " About 68% of owners renew each year."), ctx())
    chk("UNTRACED_NUMBER" in codes(r), '"68% of owners" is flagged as untraced')
    chk(any("68% of owners" in e["msg"] for e in r["errors"]), "the untraced number quotes its sentence")
    chk("UNTRACED_NUMBER" in codes(run(draft(body_md=body + " The flat fee is $1,100 per year."), ctx())),
        '"$1,100" is flagged as untraced')
    chk("UNTRACED_NUMBER" not in codes(run(draft(body_md=body + " The fee is $149 per month per home."), ctx())),
        '"$149" passes when FACTS.json carries it')
    chk("UNTRACED_NUMBER" in codes(run(draft(body_md=body + " Set up takes 3 percent of the time."), ctx())),
        '"3 percent" is always a statistic')
    chk("UNTRACED_NUMBER" not in codes(run(draft(body_md=body + " It takes 3 steps to start."), ctx())),
        '"3 steps" passes as a small bare integer')
    chk("WORD_STATISTIC" in codes(run(draft(body_md=body + " About forty percent of owners renew."), ctx())),
        '"forty percent" is a spelled-out statistic')
    chk("WORD_STATISTIC" not in codes(run(draft(body_md=body + " A dozen notes are in the log."), ctx())),
        "plain prose is not a word statistic when it is not a count of a noun we track")

    # rulings --------------------------------------------------------------------------------------------
    chk("BANNED_PHRASE" in codes(run(draft(body_md=body + " We offer guaranteed returns to every owner."), ctx())),
        "the banned phrase is caught")
    chk("SUPERLATIVE" in codes(run(draft(body_md=body + " Our team is simply the best in the market."), ctx())),
        '"best" is a superlative')
    chk("SUPERLATIVE" in codes(run(draft(title="The #1 flat fee manager"), ctx())), '"#1" is a superlative')
    chk("COMPETITOR" in codes(run(draft(body_md=body + " Acme PM charges more for the same work."), ctx())),
        "the competitor name is caught")
    chk("EXCLUDED_AREA" in codes(run(draft(body_md=body + " We also manage homes around Springfield."), ctx())),
        "the excluded area is caught")
    chk("COMPETITOR" not in codes(run(draft(body_md=body + " Acme Property Managers is not us."), ctx())),
        "a longer word is not a competitor match")
    chk("URL_IN_TEXT" in codes(run(draft(body_md=body + " Read more at www.x.com today."), ctx())),
        "a www URL in the text is caught")
    chk("URL_IN_TEXT" in codes(run(draft(body_md=body + " Our notes live at example.com/pricing."), ctx())),
        "a bare domain in the text is caught")
    chk("URL_IN_TEXT" not in codes(run(draft(body_md=body + " See [pricing](/pricing/) for the fee."), ctx())),
        "a markdown link target is not visible text")
    chk("RAW_HTML" in codes(run(draft(body_md=body + '\n<div class="card">Block</div>'), ctx())),
        "raw HTML in the body is caught")
    chk("RAW_HTML" not in codes(run(draft(body_md=body + " The notes compare a < b without trouble."), ctx())),
        "a lone < is not raw HTML")
    chk("RAW_HTML" not in codes(run(draft(body_md=body + " See <https://example.com/x> for more."), ctx())),
        "a markdown autolink is not raw HTML")

    # duplicate / corpus ---------------------------------------------------------------------------------
    chk("DUPLICATE" in codes(run(draft(), ctx(corpus={"https://example.com/copy/": body}))),
        "a corpus page with the same body is a duplicate")
    chk("DUPLICATE" not in codes(run(draft(), ctx(corpus={"https://example.com/guides/flat-fee/": body}))),
        "the draft's own page is not a duplicate of itself")
    chk("CORPUS_INCOMPLETE" in codes(run(draft(), ctx(corpus_complete=False))),
        "an incomplete corpus fails closed")
    chk("CORPUS_INCOMPLETE" in codes(run(draft(), {"profile": profile})), "a missing corpus fails closed")

    # thin -----------------------------------------------------------------------------------------------
    chk("THIN" in codes(run(draft(body_md="Too short to rank anywhere."), ctx())), "a short new draft is thin")
    chk("THIN" not in codes(run(draft(), ctx())), "a full-length new draft is not thin")

    # extend ---------------------------------------------------------------------------------------------
    ext = run(draft(mode="extend", body_md=live_nums + " " + fill * 9), ctx(live_text=live_nums))
    chk(ext["ok"] is True, "a clean extend passes: {}".format([e["msg"] for e in ext["errors"]]))
    chk("UNTRACED_NUMBER" not in codes(ext), "extend does not re-rule legacy live numbers (1,788 / 1998)")
    chk(ext["stats"]["retention"] and ext["stats"]["retention"] > 0.95, "extend reports retention")
    chk(ext["stats"]["changed_sentences"] == 0, "extend reports zero changed live sentences")
    chk(ext["stats"]["words_added"] >= 150, "extend reports the words added")

    low_ret = run(draft(mode="extend", body_md=fill * 20), ctx(live_text=live_long))
    chk("RETENTION" in codes(low_ret), "an extension that rewrites the page fails retention")
    chk(low_ret["stats"]["retention"] < 0.85, "the low retention is reported in stats")

    removed = [w for w in live_words if w != "mike"]
    one_changed = run(draft(mode="extend", body_md=" ".join(line(w) for w in removed) + " " + fill * 9),
                      ctx(live_text=live_long))
    chk("UNDECLARED_CHANGE" in codes(one_changed), "a removed live sentence must be declared")
    chk("LIVE_CHANGES" not in codes(one_changed), "one change is inside the allowed count")
    declared = run(draft(mode="extend", body_md=" ".join(line(w) for w in removed) + " " + fill * 9,
                         live_changes=[{"before": line("mike"),
                                        "after": "The mike update now also names the budget line.",
                                        "reason": "tightened"}]), ctx(live_text=live_long))
    chk("UNDECLARED_CHANGE" not in codes(declared), "a declared change is accepted")
    many = run(draft(mode="extend", body_md=" ".join(line(w) for w in live_words[:16]) + " " + fill * 9),
               ctx(live_text=live_long))
    chk("LIVE_CHANGES" in codes(many), "more than 3 changed live sentences is LIVE_CHANGES")

    # jsonld / author ------------------------------------------------------------------------------------
    chk("JSONLD" in codes(run(draft(jsonld={"@type": "Article"}), ctx())), 'JSON-LD without "@context" fails')
    chk("JSONLD" in codes(run(draft(jsonld={"@context": "https://schema.org", "@type": "Article",
                                            "x": {1, 2}}), ctx())), "JSON-LD that cannot be dumped fails")
    chk("JSONLD" in codes(run(draft(jsonld={"@context": "https://schema.org", "@type": "Article",
                                            "description": "x" * 20_500}), ctx())), "JSON-LD over 20k chars fails")
    chk("JSONLD" not in codes(run(draft(jsonld=None), ctx())), "a null jsonld is fine")
    chk("AUTHOR" in codes(run(draft(jsonld={"@context": "https://schema.org", "@type": "Article",
                                            "author": {"name": "John Smith"}}), ctx())),
        "a wrong JSON-LD author fails")
    chk("AUTHOR" in codes(run(draft(body_md=body + "\n\nBy Jane Roe"), ctx())), 'a "By X" credit fails')
    chk("AUTHOR" not in codes(run(draft(body_md=body + "\n\nBy Jane Doe"), ctx())), "the approved By line passes")

    # internal links -------------------------------------------------------------------------------------
    chk("INTERNAL_LINK" in codes(run(draft(body_md=body + " See [our fees](/guides/missing/)."), ctx())),
        "a link to an unknown path fails")
    chk("INTERNAL_LINK" not in codes(run(draft(body_md=body + " See [our fees](/pricing/)."), ctx())),
        "a link to a known path passes")
    chk("INTERNAL_LINK" in codes(run(draft(internal_links=[{"from": "/nowhere/", "anchor": "x"}]), ctx())),
        "an internal_links.from that does not exist fails")
    chk("INTERNAL_LINK" in codes(run(draft(body_md=body + " See [them](https://other.example/x)."), ctx())),
        "an external link to a non-allowed domain fails (GLM checks #4)")
    chk("INTERNAL_LINK" not in codes(run(draft(body_md=body + " See [the IRS](https://www.irs.gov/x)."), ctx())),
        "an external link to an allowed source domain is fine")
    for bad_t in ("javascript:alert(1)", "data:text/html;base64,PHM+", "//acme-pm.com/fees"):
        chk("INTERNAL_LINK" in codes(run(draft(body_md=body + " [x](%s)" % bad_t), ctx())), "link target %s refused" % bad_t)
    chk("INTERNAL_LINK" in codes(run(draft(body_md=body + "\n\n[f]: https://acme.example/\n"), ctx())),
        "reference-style link definitions refused")
    chk("SUPERLATIVE" in codes(run(draft(body_md=body + " We are the No. 1 team."), ctx())), "No. 1 is a superlative")
    chk("COMPETITOR" in codes(run(draft(body_md=body + " Unlike Acme-PM we answer."), ctx())), "hyphenated competitor")
    chk("UNTRACED_NUMBER" in codes(run(draft(body_md=body + "\n\n```\n68% of owners renew\n```\n"), ctx())),
        "numbers inside fenced code are checked (GLM checks #1)")
    chk("QUOTE_NOT_FOUND" in codes(run(draft(sources=[{"url": "https://www.irs.gov/x", "quote": quote},
                                                      {"url": "https://www.irs.gov/x", "quote": "68% of owners renew"}]),
                                       ctx())), "a cited quote that was not verified fails (GLM checks #19)")

    # sources --------------------------------------------------------------------------------------------
    chk("SOURCE_DOMAIN" in codes(run(draft(sources=[{"url": "https://spam.example.net/x", "quote": quote}]), ctx())),
        "a source outside the allowed domains fails")
    chk("SOURCE_DOMAIN" in codes(run(draft(sources=[{"url": "http://www.irs.gov/x", "quote": quote}]), ctx())),
        "an http source fails (https only)")
    chk("SOURCE_DOMAIN" not in codes(run(draft(sources=[{"url": "https://tax.irs.gov/x", "quote": quote}]), ctx())),
        "a subdomain of an allowed domain passes")
    chk("SOURCE_DOMAIN" not in codes(run(draft(sources=[{"url": "https://agency.state.gov/x", "quote": quote}]), ctx())),
        '"*.gov" allows any subdomain of gov')
    chk("SOURCES_INCOMPLETE" in codes(run(draft(), ctx(verified_quotes=[]))),
        "citing sources that were never verified fails closed")

    # warnings -------------------------------------------------------------------------------------------
    warn_faq = run(draft(faq=[]), ctx())
    chk(warn_faq["ok"] and "FAQ_MISSING" in {w["code"] for w in warn_faq["warnings"]}, "a guide without FAQ warns")
    warn_links = run(draft(internal_links=[]), ctx())
    chk("FEW_INTERNAL_LINKS" in {w["code"] for w in warn_links["warnings"]}, "fewer than 2 internal links warns")
    warn_desc = run(draft(description="Short description."), ctx())
    chk(warn_desc["ok"] and "SHORT_DESCRIPTION" in {w["code"] for w in warn_desc["warnings"]},
        "a short description warns but does not fail")

    # defaults / hostile input ---------------------------------------------------------------------------
    bare = run(draft(), {"profile": {}})
    chk(isinstance(bare, dict) and bare["ok"] is False, "an empty profile falls back to the defaults (and fails open data)")
    chk(len(run(draft(body_md=body + " 150 landlords " * 400), ctx())["errors"]) <= MAX_LIST, "errors are capped")
    chk(run(draft(body_md=None, faq=None, sources=None, internal_links=None, jsonld="x"), ctx())["ok"] is False,
        "garbage fields become errors, not exceptions")
    chk(run({"mode": "new", "slug": 5, "title": object(), "description": [], "content_type": None,
             "body_md": {}}, ctx())["ok"] is False, "wrong types never raise")
    ck_a, ck_b = {"a b c d e", "b c d e f", "c d e f g"}, {"a b c d e", "b c d e f", "x y z w v"}
    chk(abs(_jaccard(ck_a, ck_b) - 0.5) < 1e-9, "jaccard value (2 shared of 4) - regression for the inverted return")
    long_dup = " ".join("word%d" % (i % 37) for i in range(400))
    r_dup = run(draft(body_md=long_dup), ctx(corpus={"https://example.com/other/": long_dup + " tail"}))
    chk(r_dup["stats"]["max_jaccard"] > 0.9 and "DUPLICATE" in {e["code"] for e in r_dup["errors"]},
        "jaccard is measured and drives a DUPLICATE error on a near-copy")
    r_ld = run(draft(jsonld={"@context": "https://schema.org", "@type": "Product",
                             "offers": {"price": 399.99, "priceCurrency": "USD"}}), ctx())
    chk("UNTRACED_NUMBER" in {e["code"] for e in r_ld["errors"]}, "a JSON-LD price is traced like body text (GLM #4)")
    r_ent = run(draft(body_md=body + " Acme&#32;PM is slower."), ctx())
    chk("COMPETITOR" in {e["code"] for e in r_ent["errors"]}, "HTML entities are decoded before the rulings (GLM #5)")
    return n
