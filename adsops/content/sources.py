"""Cited-source handling for the AdRails guarded content pipeline (adsops/content, 0.12).

This module fetches the pages a draft cites, proves every quote is verbatim in them, and builds the
site corpus the duplicate check reads. Everything it touches is UNTRUSTED (remote HTML, sitemap XML,
model-written quotes):

  * no eval/exec, no regex built from input, every list/string is capped before use;
  * redirects are followed MANUALLY (301/302/303/307/308), each hop goes back through `fetch` (which
    re-validates the URL and pins the DNS answer), a hop that leaves https is refused, and both loops
    and over-long chains are errors: a fetched record never comes from an unvalidated hop;
  * anything that is not 200 + complete + text/html|text/plain is an error with empty text, never a
    silently empty page ("fail closed": build_corpus runs only when every page was fetched);
  * errors are short strings/codes carrying no response text (messages are scrubbed).

Pure helpers (`_norm`, `known_paths_from_sitemap`, `verify_quotes`) never touch the network, the disk
or the clock; `fetch_text`, `fetch_sources` and `build_corpus` only call the injected `fetch` callable
(`lambda u: fetcher.fetch(u, user_agent=UA)` by default). No print() outside selftest().
"""
import hashlib
import re
import time
import urllib.parse

from ..errors import ToolError, scrub
from ..seo import extract

UA = "Mozilla/5.0 (compatible; AdRails-content/0.12; +https://adrails.dev/)"

MAX_URL = 4096
MAX_TEXT = 400_000                       # cap on any text we keep; LONGER text is an error, never cut (PM)
MAX_BODY = 3_000_000                     # = fetcher.MAX_BYTES; PM: 200k cut real .gov pages mid-text
MAX_XML = 3_000_000                      # PM: 200k silently dropped sitemap URLs (corpus looked complete)
MAX_ITEMS = 500                          # cap on any list we iterate
MAX_SOURCES = 40                         # a draft may cite at most 40 sources
MAX_QUOTE = 600
MIN_QUOTE = 20
MAX_SITEMAP_PATHS = 2000
MAX_CORPUS_PAGES = 400
REDIRECTS = (301, 302, 303, 307, 308)

_LOC = re.compile(r"<loc\b[^>]*>(.*?)</loc\s*>", re.I | re.S)

_TRANS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ", "\u200b": "", "\u200c": "", "\u200d": "",
    "\u2060": "", "\ufeff": "", "\u2026": "...", "\t": " ", "\r": " ", "\n": " ", "\f": " ", "\v": " ",
})


# ── small helpers ────────────────────────────────────────────────────────────────────────────────────────────
def _norm(text, limit=MAX_TEXT):
    """Map curly quotes/dashes/nbsp/zero-width to ASCII, collapse whitespace, lowercase ('' for None)."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if len(s) > limit:
        s = s[:limit]
    return " ".join(s.translate(_TRANS).split()).lower()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _host(text):
    """-> bare lowercase host of a URL ('' when there is none). Never raises."""
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    try:
        if "//" in s:
            s = urllib.parse.urlsplit(s).netloc or ""
        s = s.split("/")[0].split("@")[-1]
        if s.startswith("["):
            s = s.split("]")[0] + "]"
        elif ":" in s:
            s = s.split(":")[0]
    except (ValueError, UnicodeError):
        return ""
    return s.strip().rstrip(".").lower()


def _scheme(url):
    try:
        return (urllib.parse.urlsplit(url).scheme or "").lower()
    except (ValueError, UnicodeError):
        return ""


def _is_https(url):
    return _scheme(url) == "https"


def _is_http(url):
    return _scheme(url) in ("http", "https")


def _origin(site):
    """-> 'scheme://host/' for an http(s) site URL ('' when it is not one)."""
    if not isinstance(site, str) or not site.strip():
        return ""
    try:
        parts = urllib.parse.urlsplit(site.strip())
        host = _host(parts.netloc)
    except (ValueError, UnicodeError):
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    return "%s://%s/" % (parts.scheme.lower(), host)


def _err(code, msg):
    return {"code": code, "msg": msg}


def _default_fetch(url):
    """Production fetch: one polite GET through adsops.seo.fetcher (SSRF-safe, never raises)."""
    from ..seo import fetcher
    return fetcher.fetch(url, user_agent=UA)


def _try_fetch(fetch, url):
    """-> the fetch record (dict) or None when the callable raised / returned something else."""
    try:
        got = fetch(url)
    except Exception:                     # noqa: BLE001 - a dead fetcher is a finding, not a crash
        return None
    return got if isinstance(got, dict) else None


def _status(rec):
    try:
        return int(rec.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def _header(rec, name):
    hdrs = rec.get("headers")
    if not isinstance(hdrs, dict):
        return ""
    for k, v in list(hdrs.items())[:100]:
        if isinstance(k, str) and k.lower() == name and isinstance(v, str):
            return v
    return ""


def _location(rec):
    loc = _header(rec, "location").strip()
    return loc[:MAX_URL]


def _ctype(rec):
    ct = rec.get("ctype")
    if not isinstance(ct, str) or not ct.strip():
        ct = _header(rec, "content-type")
    return (ct or "").split(";")[0].strip().lower()


def _kind(ctype):
    """-> 'html' | 'text' | '' (anything we may turn into text)."""
    if not ctype:
        return "html"                       # no content-type at all: best effort, the extractor is harmless
    if ctype in ("text/html", "application/xhtml+xml") or ctype.endswith("+html") or "html" in ctype:
        return "html"
    if ctype == "text/plain" or ctype.startswith("text/plain"):
        return "text"
    return ""


def _norm_pattern(pattern):
    """-> lowercase domain pattern (scheme, port, path and userinfo removed)."""
    if not isinstance(pattern, str):
        return ""
    s = pattern.strip().lower()
    if not s:
        return ""
    wildcard = s.startswith("*.")
    if wildcard:
        s = s[2:]
    if "//" in s:
        try:
            s = urllib.parse.urlsplit(s).netloc or ""
        except (ValueError, UnicodeError):
            return ""
    s = s.split("/")[0].split("@")[-1]
    if ":" in s:
        s = s.split(":")[0]
    s = s.strip().rstrip(".")
    if not s:
        return ""
    return ("*." + s) if wildcard else s


def _domain_allowed(host, allowed_domains):
    """True when `host` is an allowed source host: '*.gov' = any subdomain of gov, else exact or subdomain."""
    if not isinstance(host, str) or not host:
        return False
    patterns = allowed_domains if isinstance(allowed_domains, (list, tuple)) else []
    for raw in list(patterns)[:MAX_ITEMS]:
        pat = _norm_pattern(raw)
        if not pat:
            continue
        if pat.startswith("*."):
            base = pat[2:]
            if base and host.endswith("." + base):
                return True
        elif host == pat or host.endswith("." + pat):
            return True
    return False


# ── fetch one URL, following redirects manually ──────────────────────────────────────────────────────────────
def fetch_text(url, fetch=None, max_hops=3, allowed_domains=None):
    """Fetch `url` through `fetch`, following redirects by hand. -> {url, final, status, text, sha256, error}.

    Each hop is re-checked by `fetch` itself; a hop that is not https, a loop, a chain longer than
    `max_hops`, a non-200 status, a truncated body or any fetch error gives text "" and a short error.
    text/html is reduced to its visible text by extract.parse(); text/plain is kept as is. sha256 is
    the hash of the kept text (evidence integrity for sources.json).
    """
    if fetch is not None and not callable(fetch):
        raise ToolError("fetch_text: fetch must be a callable")
    fetch = _default_fetch if fetch is None else fetch
    raw_url = url if isinstance(url, str) else ""
    out = {"url": raw_url, "final": raw_url, "status": 0, "text": "", "sha256": "", "error": None}
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL:
        out["error"] = "empty or invalid url"
        return out
    try:
        hops = int(max_hops)
    except (TypeError, ValueError):
        hops = 3
    hops = max(0, min(hops, 10))
    cur = url.strip()
    seen = {cur}
    taken = 0
    while True:
        got = _try_fetch(fetch, cur)
        if got is None:
            out["final"] = cur
            out["error"] = "fetch raised an exception"
            return out
        status = _status(got)
        if status in REDIRECTS:
            loc = _location(got)
            out["final"] = cur
            out["status"] = status
            if not loc:
                out["error"] = "redirect without location"
                return out
            try:
                nxt = urllib.parse.urljoin(cur, loc)
            except (ValueError, UnicodeError):
                out["error"] = "redirect target cannot be parsed"
                return out
            if not _is_https(nxt):
                out["error"] = "redirect to non-https refused"
                return out
            # PM: the allowed-domain rule applies to EVERY hop, or irs.gov -> evil.example would be "verified"
            if allowed_domains is not None and not _domain_allowed(_host(nxt), allowed_domains):
                out["error"] = "redirect to a domain that is not allowed: " + scrub(_host(nxt) or "?", 120)
                return out
            taken += 1
            if taken > hops:
                out["error"] = "too many redirects"
                return out
            if nxt in seen:
                out["error"] = "redirect loop"
                return out
            seen.add(nxt)
            cur = nxt
            continue
        out["final"] = cur
        out["status"] = status
        err = got.get("error")
        if isinstance(err, str) and err:
            out["error"] = "fetch error: " + scrub(err, 120)
            return out
        if status != 200:
            out["error"] = "status %d" % status
            return out
        if got.get("truncated"):                      # PM (GLM #7): any truthy value means truncated
            out["error"] = "truncated"
            return out
        body = got.get("body")
        body = body if isinstance(body, str) else ("" if body is None else str(body))
        kind = _kind(_ctype(got))
        if len(body) > MAX_BODY:
            out["error"] = "too large"
            return out
        if kind == "html":
            try:
                parsed = extract.parse(body, cur)
            except Exception:             # noqa: BLE001 - the extractor never raises, but never trust that
                parsed = {}
            text = parsed.get("text") if isinstance(parsed, dict) else ""
            text = text if isinstance(text, str) else ""
        elif kind == "text":
            text = body
        else:
            out["error"] = "unsupported content type: " + scrub(_ctype(got) or "unknown", 40)
            return out
        if len(text) > MAX_TEXT:
            out["error"] = "text too long (%d chars)" % len(text)
            return out
        out["text"] = text
        out["sha256"] = _sha(out["text"])
        return out


# ── fetch the cited sources ──────────────────────────────────────────────────────────────────────────────────
def fetch_sources(urls, allowed_domains, fetch=None, deadline=None, max_sources=12):
    """Fetch every cited URL that is https and on an allowed domain.

    -> {"ok": bool, "sources": {url: fetch_text record}, "errors": [{"code","msg"}]}. Non-https URLs and
    domains outside `allowed_domains` are refused WITHOUT being fetched, an expired `deadline` stops the
    run, and `ok` is False when there is any error at all (fail closed).
    """
    fetch = _default_fetch if fetch is None else fetch
    out = {"ok": False, "sources": {}, "errors": []}
    errs = out["errors"]
    if not isinstance(urls, (list, tuple)):
        errs.append(_err("SOURCES_INCOMPLETE", "urls must be a list of strings"))
        return out
    try:
        cap = int(max_sources)
    except (TypeError, ValueError):
        cap = 12
    cap = max(1, min(cap, 100))
    ordered, seen = [], set()
    for item in list(urls)[:MAX_ITEMS]:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s and len(s) <= MAX_URL and s not in seen:
            seen.add(s)
            ordered.append(s)
    if len(ordered) > cap:
        errs.append(_err("TOO_MANY", "too many sources (%d > %d)" % (len(ordered), cap)))
        ordered = ordered[:cap]
    if not ordered:
        errs.append(_err("SOURCES_INCOMPLETE", "no source URLs were given"))
    for u in ordered:
        if deadline is not None and time.monotonic() > deadline:
            errs.append(_err("DEADLINE", "deadline"))
            break
        if not _is_https(u):
            errs.append(_err("SOURCE_SCHEME", "only https sources are fetched: " + scrub(u, 200)))
            continue
        host = _host(u)
        if not _domain_allowed(host, allowed_domains):
            errs.append(_err("SOURCE_DOMAIN", "domain is not allowed: " + scrub(host or u, 200)))
            continue
        rec = fetch_text(u, fetch, allowed_domains=allowed_domains)
        out["sources"][u] = rec
        if rec.get("error"):
            errs.append(_err("FETCH", "%s: %s" % (scrub(u, 200), scrub(rec["error"], 120))))
    out["ok"] = not errs
    return out


# ── prove the quotes ────────────────────────────────────────────────────────────────────────────────────────
def verify_quotes(draft_sources, fetched):
    """Check every cited quote against the fetched text of its URL.

    -> (verified_quotes, labels, errors). A quote matches when its normalised form is a substring of the
    normalised fetched text (curly quotes, dashes, nbsp, zero-width and whitespace differences do not
    matter), and it must be 20..600 characters long. Errors use QUOTE_NOT_FOUND (quote missing/too short/
    too long) and SOURCES_INCOMPLETE (source not fetched, fetch errored, no sources at all).
    """
    quotes, labels, errors = [], [], []
    fetched = fetched if isinstance(fetched, dict) else {}
    srcs = draft_sources if isinstance(draft_sources, (list, tuple)) else []
    srcs = list(srcs)[:MAX_SOURCES]
    if not srcs:
        errors.append(_err("SOURCES_INCOMPLETE", "the draft cites no sources"))
    if not fetched:
        errors.append(_err("SOURCES_INCOMPLETE", "no sources were fetched"))
    for i, src in enumerate(srcs):
        if not isinstance(src, dict):
            errors.append(_err("SOURCES_INCOMPLETE", "source %d is not an object" % i))
            continue
        url = src.get("url")
        quote = src.get("quote")
        if not isinstance(url, str) or not url.strip():
            errors.append(_err("SOURCES_INCOMPLETE", "source %d has no url" % i))
            continue
        url = url.strip()
        rec = fetched.get(url)
        if not isinstance(rec, dict):
            errors.append(_err("SOURCES_INCOMPLETE", "source %d was not fetched: %s" % (i, scrub(url, 200))))
            continue
        if isinstance(rec.get("error"), str) and rec["error"]:
            errors.append(_err("SOURCES_INCOMPLETE", "source %d could not be fetched: %s" % (i, scrub(url, 200))))
            continue
        text = rec.get("text")
        text_n = _norm(text if isinstance(text, str) else "")
        if not isinstance(quote, str) or len(_norm(quote)) < MIN_QUOTE:     # PM (GLM #16): measured after normalising
            errors.append(_err("QUOTE_NOT_FOUND", "quote %d is shorter than %d characters" % (i, MIN_QUOTE)))
            continue
        if len(quote) > MAX_QUOTE:
            errors.append(_err("QUOTE_NOT_FOUND", "quote %d is longer than %d characters" % (i, MAX_QUOTE)))
            continue
        if not text_n:
            errors.append(_err("SOURCES_INCOMPLETE", "source %d has no text: %s" % (i, scrub(url, 200))))
            continue
        quote_n = _norm(quote)
        if not quote_n or quote_n not in text_n:
            host = _host(url)
            errors.append(_err("QUOTE_NOT_FOUND", "quote %d is not in %s" % (i, scrub(host or url, 80))))
            continue
        quotes.append(quote)
        host = _host(url)
        if host and host not in labels:
            labels.append(host)
    return quotes, labels, errors


# ── the duplicate-check corpus ──────────────────────────────────────────────────────────────────────────────
def build_corpus(site, fetch=None, known_paths=None, deadline=None, max_pages=400):
    """Fetch the site origin plus every known path. -> {"corpus": {url: text}, "complete": bool, "errors"}.

    `complete` is False (fail closed: the duplicate check then refuses the draft) on any fetch error, any
    truncated page, an expired deadline, or more URLs than `max_pages` / MAX_CORPUS_PAGES.
    """
    fetch = _default_fetch if fetch is None else fetch
    out = {"corpus": {}, "complete": True, "errors": []}
    errs = out["errors"]
    try:
        cap = int(max_pages)
    except (TypeError, ValueError):
        cap = MAX_CORPUS_PAGES
    cap = max(1, min(cap, MAX_CORPUS_PAGES))
    origin = _origin(site)
    if not origin:
        out["complete"] = False
        errs.append(_err("CORPUS_INCOMPLETE", "site must be an http(s) URL"))
        return out
    urls, seen = [origin], {origin}
    paths = known_paths if isinstance(known_paths, (list, tuple)) else []
    for path in list(paths)[:MAX_ITEMS]:
        if not isinstance(path, str):
            continue
        s = path.strip()
        if not s or len(s) > MAX_URL:
            continue
        try:
            u = urllib.parse.urljoin(origin, s)
        except (ValueError, UnicodeError):
            continue
        if not _is_http(u) or u in seen:
            continue
        seen.add(u)
        urls.append(u)
    if len(urls) > cap:
        out["complete"] = False
        errs.append(_err("CORPUS_INCOMPLETE", "too many pages to crawl (%d > %d)" % (len(urls), cap)))
        urls = urls[:cap]
    for u in urls:
        if deadline is not None and time.monotonic() > deadline:
            out["complete"] = False
            errs.append(_err("DEADLINE", "deadline"))
            break
        site_host = _host(origin)
        twin = site_host[4:] if site_host.startswith("www.") else "www." + site_host
        rec = fetch_text(u, fetch, allowed_domains=[site_host, twin])     # PM: corpus redirects stay on the site
        if rec.get("error"):
            out["complete"] = False
            errs.append(_err("CORPUS_INCOMPLETE", "%s: %s" % (scrub(u, 200), scrub(rec["error"], 120))))
            continue
        out["corpus"][u] = rec["text"]
    return out


def known_paths_from_sitemap(xml_text, host):
    """-> same-host <loc> paths from a sitemap (max 2000, deduped, in document order).

    A plain regex over <loc>…</loc>: no XML parser, so no entity expansion and no DTD tricks. Other
    hosts, non-http(s) locs, fragments and query strings are dropped; 'www.' is ignored when comparing.
    """
    if not isinstance(xml_text, str) or not xml_text:
        return []
    base = _host(host)
    if base.startswith("www."):
        base = base[4:]
    if not base:
        return []
    paths, seen = [], set()
    try:
        matches = _LOC.finditer(xml_text[:MAX_XML])
    except Exception:                     # noqa: BLE001 - never raise on hostile input
        return []
    for m in matches:
        if len(paths) >= MAX_SITEMAP_PATHS:
            break
        loc = " ".join(m.group(1).split())[:MAX_URL]
        if not loc or not loc.lower().startswith(("https://", "http://")):
            continue
        try:
            parts = urllib.parse.urlsplit(loc)
            loc_host = _host(parts.netloc)
            path = parts.path or "/"
        except (ValueError, UnicodeError):
            continue
        if loc_host.startswith("www."):
            loc_host = loc_host[4:]
        if loc_host != base:
            continue
        if not path.startswith("/"):
            path = "/" + path
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


# ── selftest ────────────────────────────────────────────────────────────────────────────────────────────────
def selftest():
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "content.sources.selftest: " + msg
        n += 1

    calls = []
    pages = {}

    def mk(status=200, body="", ctype="text/html", headers=None, truncated=False, error=None):
        return {"url": "", "status": status, "headers": dict(headers or {}), "body": body,
                "bytes": len(body or ""), "truncated": truncated, "ms": 1, "ctype": ctype, "error": error}

    def fake(url):
        calls.append(url)
        rec = pages.get(url)
        out = dict(rec) if rec is not None else mk(404, "", "text/html")
        out["url"] = url
        return out

    soon = lambda: time.monotonic() + 60
    past = lambda: time.monotonic() - 1

    # norm
    ck(_norm("\u201cHi\u201d \u2014 there\u00a0zero\u200bwidth") == '"hi" - there zerowidth',
       "norm maps curly quotes, dashes, nbsp and zero-width")
    ck(_norm(None) == "" and _norm(123) == "123", "norm tolerates None and non-strings")
    ck(_origin("https://Site.Example/x") == "https://site.example/" and _origin("ftp://a/") == "",
       "origin is lowercased with a trailing slash, non-http refused")

    # fetch_text: the happy paths
    pages["https://a.example/guide/"] = mk(body="<html><body><h1>Flat fees</h1><p>Management is $149.</p></body></html>")
    r = fetch_text("https://a.example/guide/", fake)
    ck(r["status"] == 200 and r["error"] is None and r["final"] == "https://a.example/guide/", "200 html is fetched")
    ck("Flat fees" in r["text"] and "$149" in r["text"] and "<" not in r["text"], "html reduced to visible text")
    ck(r["sha256"] == _sha(r["text"]) and len(r["sha256"]) == 64, "sha256 of the kept text is recorded")
    pages["https://a.example/robots.txt"] = mk(body="User-agent: *\nDisallow:", ctype="text/plain")
    r = fetch_text("https://a.example/robots.txt", fake)
    ck(r["text"] == "User-agent: *\nDisallow:" and r["error"] is None, "text/plain is kept verbatim")
    pages["https://a.example/nc/"] = mk(body="<p>No type</p>", ctype="")
    r = fetch_text("https://a.example/nc/", fake)
    ck(r["error"] is None and "No type" in r["text"], "a missing content-type is treated as html")
    pages["https://a.example/hdr/"] = mk(body="<p>Header type</p>", ctype="")
    pages["https://a.example/hdr/"]["headers"] = {"content-type": "text/html; charset=utf-8"}
    r = fetch_text("https://a.example/hdr/", fake)
    ck(r["error"] is None and "Header type" in r["text"], "content-type read from the headers when ctype is empty")

    # fetch_text: failures are errors with empty text
    r = fetch_text("https://a.example/missing/", fake)
    ck(r["status"] == 404 and r["error"] == "status 404" and r["text"] == "", "404 is an error with empty text")
    pages["https://a.example/big/"] = mk(body="<p>x</p>", truncated=True)
    r = fetch_text("https://a.example/big/", fake)
    ck(r["error"] == "truncated" and r["text"] == "", "a truncated body is refused")
    pages["https://a.example/dead/"] = mk(status=0, error="ConnectionRefusedError")
    r = fetch_text("https://a.example/dead/", fake)
    ck(r["text"] == "" and "ConnectionRefusedError" in (r["error"] or ""), "a fetch error is carried over")
    pages["https://a.example/file.pdf"] = mk(body="%PDF-1.4", ctype="application/pdf")
    r = fetch_text("https://a.example/file.pdf", fake)
    ck((r["error"] or "").startswith("unsupported content type") and r["text"] == "",
       "a non html/text content type is refused")

    def boom(url):
        raise RuntimeError("secret detail")
    r = fetch_text("https://a.example/", boom)
    ck(r["error"] == "fetch raised an exception" and "secret" not in r["error"], "a raising fetch is an error")
    ck(fetch_text("", fake)["error"] == "empty or invalid url", "an empty url is refused")
    ck(fetch_text(None, fake)["error"] == "empty or invalid url", "a non-string url is refused")
    try:
        fetch_text("https://a.example/", fetch="nope")
        raise AssertionError("content.sources.selftest: a non-callable fetch must raise")
    except ToolError:
        n += 1
    ck(fetch_text("https://a.example/" + "x" * 5000, fake)["error"] == "empty or invalid url", "an over-long url is refused")

    # fetch_text: redirects
    calls.clear()
    pages["https://a.example/old/"] = mk(301, headers={"location": "/mid/"})
    pages["https://a.example/mid/"] = mk(302, headers={"Location": "https://b.example/final/"})
    pages["https://b.example/final/"] = mk(body="<html><body><p>Done</p></body></html>")
    r = fetch_text("https://a.example/old/", fake)
    ck(r["final"] == "https://b.example/final/" and r["status"] == 200 and "Done" in r["text"],
       "relative and absolute redirects are followed")
    ck(r["url"] == "https://a.example/old/" and calls[-1] == "https://b.example/final/",
       "the original url is kept and the last hop fetched")
    ck(calls == ["https://a.example/old/", "https://a.example/mid/", "https://b.example/final/"],
       "every hop goes back through fetch")
    pages["https://a.example/l1/"] = mk(301, headers={"location": "https://a.example/l2/"})
    pages["https://a.example/l2/"] = mk(301, headers={"location": "https://a.example/l1/"})
    r = fetch_text("https://a.example/l1/", fake)
    ck(r["error"] == "redirect loop" and r["text"] == "", "a redirect loop is refused")
    for i in range(5):
        pages["https://a.example/h%d/" % i] = mk(301, headers={"location": "/h%d/" % (i + 1)})
    pages["https://a.example/h5/"] = mk(body="<p>end</p>")
    ck(fetch_text("https://a.example/h0/", fake)["error"] == "too many redirects", "a long redirect chain is refused")
    r = fetch_text("https://a.example/h2/", fake, max_hops=3)
    ck(r["error"] is None and r["final"] == "https://a.example/h5/" and "end" in r["text"],
       "three hops are allowed with max_hops=3")
    pages["https://a.example/down/"] = mk(302, headers={"location": "http://a.example/insecure/"})
    ck(fetch_text("https://a.example/down/", fake)["error"] == "redirect to non-https refused",
       "a redirect to http is refused")
    pages["https://a.example/noloc/"] = mk(301)
    ck(fetch_text("https://a.example/noloc/", fake)["error"] == "redirect without location",
       "a redirect without Location is refused")

    # allowed domains
    for host, allowed in (("irs.gov", True), ("www.irs.gov", True), ("sub.irs.gov", True),
                          ("notgov.example", False), ("gov", False), ("irs.gov.evil.net", False)):
        ck(_domain_allowed(host, ["*.gov"]) is allowed, "wildcard *.gov decides " + host)
    ck(_domain_allowed("example.com", ["example.com"]) and _domain_allowed("blog.example.com", ["example.com"]),
       "an exact host and its subdomains are allowed")
    ck(not _domain_allowed("example.com.evil.net", ["example.com"]), "the suffix trick is not allowed")
    ck(_domain_allowed("example.com", ["HTTPS://Example.COM:443/path"]) and not _domain_allowed("x.test", []),
       "patterns are normalised and an empty list allows nothing")

    # fetch_sources
    pages["https://a.example/p/"] = mk(body="<p>ok</p>")
    calls.clear()
    r = fetch_sources(["https://a.example/p/", "https://a.example/p/"], ["a.example"], fake, deadline=soon())
    ck(r["ok"] is True and list(r["sources"]) == ["https://a.example/p/"] and r["errors"] == [],
       "an allowed source is fetched once")
    ck(calls.count("https://a.example/p/") == 1, "a duplicate url is fetched only once")
    calls.clear()
    r = fetch_sources(["https://evil.example/x/"], ["a.example"], fake, deadline=soon())
    ck(r["ok"] is False and r["sources"] == {} and calls == [], "a non-allowed domain is refused without fetching")
    ck(r["errors"] and r["errors"][0]["code"] == "SOURCE_DOMAIN", "the refusal uses SOURCE_DOMAIN")
    calls.clear()
    r = fetch_sources(["http://a.example/x/"], ["a.example"], fake, deadline=soon())
    ck(r["ok"] is False and calls == [] and r["errors"][0]["code"] == "SOURCE_SCHEME", "a non-https source is refused")
    calls.clear()
    pages["https://www.irs.gov/pub/"] = mk(body="<p>IRS page</p>")
    r = fetch_sources(["https://www.irs.gov/pub/"], ["*.gov"], fake, deadline=soon())
    ck(r["ok"] is True and "https://www.irs.gov/pub/" in r["sources"], "a *.gov source is fetched")
    calls.clear()
    r = fetch_sources(["https://a.example/p/"], ["a.example"], fake, deadline=past())
    ck(r["ok"] is False and calls == [] and r["errors"][0]["msg"] == "deadline",
       "an expired deadline stops before any fetch")
    many = ["https://a.example/p%d/" % i for i in range(15)]
    for u in many:
        pages[u] = mk(body="<p>ok</p>")
    r = fetch_sources(many, ["a.example"], fake, deadline=soon(), max_sources=3)
    ck(r["ok"] is False and len(r["sources"]) == 3 and any(e["code"] == "TOO_MANY" for e in r["errors"]),
       "max_sources caps the fetch list and is reported")
    pages["https://a.example/gone/"] = mk(404)
    r = fetch_sources(["https://a.example/p/", "https://a.example/gone/"], ["a.example"], fake, deadline=soon())
    ck(r["ok"] is False and "https://a.example/p/" in r["sources"] and r["sources"]["https://a.example/gone/"]["error"],
       "one bad source makes the whole set not ok")
    ck(any(e["code"] == "FETCH" for e in r["errors"]), "a failed source fetch is reported as FETCH")
    ck(fetch_sources([], ["a.example"], fake, deadline=soon())["ok"] is False, "an empty source list is not ok")

    # verify_quotes
    fetched = {"https://www.a.example/q/": {"url": "https://www.a.example/q/", "final": "https://www.a.example/q/",
                                           "status": 200, "error": None, "sha256": "x",
                                           "text": "We charge $149 per month per home. It\u2019s a flat fee, no "
                                                   "surprises at all."}}
    good = "It's a flat fee,   no\u00a0surprises at all."
    quotes, labels, errs = verify_quotes([{"url": "https://www.a.example/q/", "quote": good}], fetched)
    ck(errs == [] and quotes == [good], "a quote matches across curly quotes, nbsp and extra spaces")
    ck(labels == ["www.a.example"], "labels are the source hosts")
    quotes, labels, errs = verify_quotes([{"url": "https://www.a.example/q/", "quote": "We charge $149 per month per home."},
                                          {"url": "https://www.a.example/q/", "quote": good}], fetched)
    ck(len(quotes) == 2 and labels == ["www.a.example"], "labels are deduplicated per host")
    quotes, _, errs = verify_quotes([{"url": "https://www.a.example/q/", "quote": "We charge $99 per month per home."}],
                                    fetched)
    ck(quotes == [] and errs and errs[0]["code"] == "QUOTE_NOT_FOUND", "a quote that is not in the source is an error")
    quotes, _, errs = verify_quotes([{"url": "https://www.a.example/q/", "quote": "flat fee"}], fetched)
    ck(quotes == [] and errs[0]["code"] == "QUOTE_NOT_FOUND" and "shorter" in errs[0]["msg"],
       "a quote shorter than 20 characters is an error")
    quotes, _, errs = verify_quotes([{"url": "https://www.a.example/q/", "quote": "x" * 601}], fetched)
    ck(quotes == [] and errs[0]["code"] == "QUOTE_NOT_FOUND" and "longer" in errs[0]["msg"],
       "a quote longer than 600 characters is an error")
    _, _, errs = verify_quotes([{"url": "https://never.example/q/", "quote": good}], fetched)
    ck(errs and errs[0]["code"] == "SOURCES_INCOMPLETE", "an unfetched source is SOURCES_INCOMPLETE")
    _, _, errs = verify_quotes([{"url": "https://www.a.example/q/", "quote": good}],
                               {"https://www.a.example/q/": {"error": "status 404", "text": ""}})
    ck(errs and errs[0]["code"] == "SOURCES_INCOMPLETE", "a source whose fetch failed is SOURCES_INCOMPLETE")
    _, _, errs = verify_quotes([], fetched)
    ck(errs and errs[0]["code"] == "SOURCES_INCOMPLETE", "a draft without sources is SOURCES_INCOMPLETE")
    _, _, errs = verify_quotes([{"quote": good}], fetched)
    ck(errs and errs[0]["code"] == "SOURCES_INCOMPLETE", "a source without a url is SOURCES_INCOMPLETE")

    # build_corpus
    pages["https://site.example/"] = mk(body="<html><body><p>Home page</p></body></html>")
    pages["https://site.example/about/"] = mk(body="<html><body><p>About us</p></body></html>")
    r = build_corpus("https://site.example", fake, ["/about/"], deadline=soon())
    ck(r["complete"] is True and r["errors"] == [] and len(r["corpus"]) == 2, "a good corpus is complete")
    ck("https://site.example/" in r["corpus"] and "Home page" in r["corpus"]["https://site.example/"],
       "the site origin is crawled")
    ck("https://site.example/about/" in r["corpus"] and "About us" in r["corpus"]["https://site.example/about/"],
       "known paths are joined against the origin")
    r = build_corpus("https://site.example", fake, ["/missing/"], deadline=soon())
    ck(r["complete"] is False and r["errors"] and len(r["corpus"]) == 1, "a missing path makes the corpus incomplete")
    pages["https://site.example/big/"] = mk(body="<p>x</p>", truncated=True)
    ck(build_corpus("https://site.example", fake, ["/big/"], deadline=soon())["complete"] is False,
       "a truncated page makes the corpus incomplete")
    r = build_corpus("https://site.example", fake, ["/about/"], deadline=past())
    ck(r["complete"] is False and r["corpus"] == {}, "an expired deadline makes the corpus incomplete")
    r = build_corpus("https://site.example", fake, ["/about/"], deadline=soon(), max_pages=1)
    ck(r["complete"] is False and len(r["corpus"]) == 1 and r["errors"], "more URLs than max_pages is incomplete")
    ck(build_corpus("ftp://site.example", fake)["complete"] is False, "a non-http site is refused")

    # known_paths_from_sitemap
    xml = ('<?xml version="1.0"?><urlset><url><loc>https://site.example/a/</loc></url>'
           '<url><loc> https://site.example/b/ </loc></url><url><loc>https://other.example/c/</loc></url>'
           '<url><loc>ftp://site.example/d/</loc></url><url><loc>https://site.example/a/</loc></url></urlset>')
    ck(known_paths_from_sitemap(xml, "site.example") == ["/a/", "/b/"],
       "same-host paths are taken, other hosts, schemes and duplicates are dropped")
    ck(known_paths_from_sitemap("<urlset></urlset>", "site.example") == [], "a sitemap without <loc> gives no paths")
    ck(known_paths_from_sitemap(xml, "www.site.example") == ["/a/", "/b/"], "www is ignored when comparing hosts")
    ck(known_paths_from_sitemap(xml, "") == [] and known_paths_from_sitemap(None, "site.example") == [],
       "hostile input gives no paths")
    big = "".join("<loc>https://site.example/p%d/</loc>" % i for i in range(2100))
    ck(len(known_paths_from_sitemap(big, "site.example")) == 2000, "sitemap paths are capped at 2000")
    ck(known_paths_from_sitemap("<loc>https://site.example/x?a=1&amp;b=2</loc>", "site.example") == ["/x"],
       "query strings are not part of a corpus path and entities are not expanded")
    # PM: allowed domains are re-checked on every redirect hop
    def hop_fetch(u):
        if u == "https://www.irs.gov/a":
            return {"url": u, "status": 301, "headers": {"location": "https://evil.example/a"}, "body": "", "error": None}
        return {"url": u, "status": 200, "headers": {"content-type": "text/plain"}, "ctype": "text/plain",
                "body": "the quoted text is right here for sure", "error": None}
    r = fetch_sources(["https://www.irs.gov/a"], ["*.gov"], fetch=hop_fetch)
    ck(not r["ok"] and "not allowed" in r["sources"]["https://www.irs.gov/a"]["error"], "cross-domain redirect refused")
    big = fetch_text("https://x.gov/b", lambda u: {"url": u, "status": 200, "headers": {"content-type": "text/plain"},
                                                  "ctype": "text/plain", "body": "a" * (MAX_TEXT + 1), "error": None})
    ck(big["error"] and not big["text"], "over-long text is an error, never silently cut")
    return n
