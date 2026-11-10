"""Read-only extraction for the AdRails site audit (adsops/seo): URL templates and "page documents" from HTML.

Pure module: it never touches the network, the disk or the clock, and it never executes anything it reads.
`template_of()` collapses a URL into the report template used everywhere in the audit, `normalize()` is the single
absolute-URL normaliser, and `parse()` turns raw HTML (str or bytes, however hostile) into the PageDocument dict
described in SPEC-B. Nothing here raises on bad HTML: unparsable input yields whatever was collected plus every
key of the contract. JSON-serialisable output only, no sets/tuples/datetimes, no print() outside selftest().
"""
import json
import re
import urllib.parse
from html.parser import HTMLParser

from adsops.errors import ToolError

MAX_STR = 500
MAX_TEXT = 60000
MAX_LINKS = 2000
MAX_IMAGES = 500
MAX_LIST = 20
MAX_HREFLANG = 200
MAX_ATTR = 8192
BUF_CAP = 200000

_ASSET_EXTS = frozenset(("pdf", "xml", "txt", "png", "jpg", "jpeg", "webp", "gif", "svg", "js", "css", "json",
                         "ico", "zip", "mp4"))
_DATE_SEG = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
_DIGITS_SEG = re.compile(r"^\d+$")

_VOID_TAGS = frozenset(("area", "base", "basefont", "br", "col", "embed", "frame", "hr", "img", "input", "isindex",
                        "keygen", "link", "meta", "param", "source", "track", "wbr"))
_SKIP_TAGS = frozenset(("script", "style", "noscript", "template", "svg"))
_NAV_TAGS = frozenset(("nav", "header", "footer"))
_SKIP_HREF_PREFIXES = ("mailto:", "tel:", "sms:", "javascript:", "data:", "vbscript:", "file:", "ftp:")


def _clean(value, limit=MAX_STR):
    """Whitespace-collapsed, truncated string ('' for None)."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit]


def _maybe_clean(value):
    """Same as _clean but keeps None (for optional attributes like alt/width)."""
    return None if value is None else _clean(value)


def template_of(url):
    """-> report template of `url` ('home', 'asset', '/faq/', '/a/*/*/', ...). Query strings are ignored."""
    if not isinstance(url, str):
        raise ToolError("template_of: url must be a string")
    try:
        path = urllib.parse.urlsplit(url).path or ""
    except ValueError:
        path = url                                    # unparsable: best effort on the raw string
    if path in ("", "/"):
        return "home"
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "home"
    last = segs[-1]
    if "." in last and last.rsplit(".", 1)[1].lower() in _ASSET_EXTS:
        return "asset"
    mapped = ["{n}" if (_DIGITS_SEG.match(s) or _DATE_SEG.match(s)) else s for s in segs]
    if len(mapped) == 1:
        return "/" + mapped[0] + "/"
    if len(mapped) == 2:
        return "/" + mapped[0] + "/*/"
    return "/" + mapped[0] + "/*/*/"


def normalize(url, base=None):
    """-> absolute normalised http(s) URL (fragment dropped, scheme/host lowercased, default port removed).

    Relative `url` is joined against `base` first. Returns '' when the result is not http(s)."""
    if not isinstance(url, str):
        raise ToolError("normalize: url must be a string")
    if base is not None and not isinstance(base, str):
        base = None
    try:
        full = urllib.parse.urljoin(base, url) if base else url
        u = urllib.parse.urlsplit(full)
        scheme = (u.scheme or "").lower()
        host = (u.hostname or "").lower()
        port = u.port
    except (ValueError, TypeError):
        return ""
    if scheme not in ("http", "https") or not host:
        return ""
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = ("[" + host + "]") if ":" in host else host
    if port is not None:
        netloc = netloc + ":" + str(port)
    return urllib.parse.urlunsplit((scheme, netloc, u.path or "/", u.query, ""))


def _resolve(raw, base):
    """Resolve a raw href/attribute value against `base`; '' when it is not an absolute http(s) URL."""
    if not raw or not isinstance(raw, str):
        return ""
    try:
        return normalize(raw.strip(), base or None)
    except Exception:
        return ""


def _attrs(pairs):
    """-> {lowercased name: value}; first duplicate wins, values capped, None -> ''."""
    out = {}
    for item in pairs or ():
        try:
            key, val = item[0], item[1]
        except Exception:
            continue
        if not isinstance(key, str):
            continue
        key = key.lower()
        if key in out:
            continue
        if val is None:
            val = ""
        elif not isinstance(val, str):
            val = str(val)
        if len(val) > MAX_ATTR:
            val = val[:MAX_ATTR]
        out[key] = val
    return out


class _Buf:
    """Bounded text buffer: keeps at most `cap` characters, joins parts with `sep`."""

    def __init__(self, cap=BUF_CAP, sep=" "):
        self.cap = cap
        self.sep = sep
        self.parts = []
        self.n = 0

    def add(self, s):
        if not s or self.n >= self.cap:
            return
        take = s[: self.cap - self.n]
        self.parts.append(take)
        self.n += len(take)

    def text(self):
        return self.sep.join(self.parts)


class _PageParser(HTMLParser):
    """One tolerant pass over a page: every accounting is a counter, so void/self-closing tags cannot unbalance it."""

    def __init__(self, base_url):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.base_url = base_url
        self.base_href = None
        self.doctype = False
        self.seen_start = False
        self.stack = []
        self.skip_depth = 0
        self.svg_depth = 0
        self.head_depth = 0
        self.body_depth = 0
        self.main_depth = 0
        self.nav_depth = 0
        self.has_body = False
        self.has_main = False
        self.lang = None
        self.titles = []
        self.descriptions = []
        self.robots = []
        self.canonicals = []
        self.hreflangs = []
        self.links = []
        self.images = []
        self.preloads = []
        self.posters = []
        self.h1 = []
        self.h2_count = 0
        self.jsonld_types = []
        self.jsonld_errors = 0
        self.viewport = False
        self.og_image = False
        self.meta_refresh = None
        self._title = None
        self._h1 = None
        self._ld = None
        self._a_stack = []
        self._seen_types = set()
        self.main_buf = _Buf()
        self.body_buf = _Buf()

    # -- counters -------------------------------------------------------------------------------------------
    def _bump(self, tag, d):
        if tag in _SKIP_TAGS:
            self.skip_depth += d
        if tag == "svg":
            self.svg_depth += d
        if tag == "head":
            self.head_depth += d
        if tag == "body":
            self.body_depth += d
        if tag == "main":
            self.main_depth += d
        if tag in _NAV_TAGS:
            self.nav_depth += d

    def _push(self, tag):
        self.stack.append(tag)
        self._bump(tag, 1)
        if tag == "body":
            self.has_body = True
        elif tag == "main":
            self.has_main = True

    def _unwind(self, tag):
        idx = -1
        for i in range(len(self.stack) - 1, max(-1, len(self.stack) - 65), -1):   # bounded: no O(n^2) on hostile nesting
            if self.stack[i] == tag:
                idx = i
                break
        if idx < 0:
            return False
        while len(self.stack) > idx:
            self._bump(self.stack.pop(), -1)
        return True

    # -- tags -----------------------------------------------------------------------------------------------
    def handle_decl(self, decl):
        if not self.seen_start and isinstance(decl, str) and decl.strip().lower().startswith("doctype"):
            self.doctype = True

    def unknown_decl(self, data):
        return None

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, True)

    def _open(self, tag, attrs, closed):
        tag = (tag or "").lower()
        self.seen_start = True
        a = _attrs(attrs)
        if tag == "html":
            if self.lang is None:
                self.lang = _clean(a.get("lang") or "") or None
        elif tag == "base":
            if self.base_href is None and a.get("href"):
                self.base_href = a["href"]
        elif tag == "meta":
            self._meta(a)
        elif tag == "link":
            self._link(a)
        elif tag == "title":
            if self.svg_depth == 0 and not self.has_body:
                self._title = _Buf(4000)
        elif tag == "h1":
            self._h1 = _Buf(4000)
        elif tag == "h2":
            self.h2_count += 1
        elif tag == "a":
            self._a_stack.append(self._anchor(a))
        elif tag == "img":
            self._img(a)
        elif tag == "video":
            if a.get("poster"):
                self.posters.append(a["poster"])
        elif tag == "script":
            if (a.get("type") or "").strip().lower() == "application/ld+json":
                self._ld = _Buf(BUF_CAP, sep="")
        if tag in _VOID_TAGS or closed:
            self._close_leaf(tag)
        else:
            self._push(tag)

    def _close_leaf(self, tag):
        """Finish the per-tag capture of a void or self-closing tag (nothing was pushed)."""
        if tag == "a" and self._a_stack:
            self._close_anchor()
        elif tag == "title" and self._title is not None:
            self.titles.append(_clean(self._title.text()))
            self._title = None
        elif tag == "h1" and self._h1 is not None:
            self.h1.append(_clean(self._h1.text()))
            self._h1 = None
        elif tag == "script" and self._ld is not None:
            self._finish_ld()

    def _meta(self, a):
        name = (a.get("name") or "").strip().lower()
        prop = (a.get("property") or "").strip().lower()
        equiv = (a.get("http-equiv") or "").strip().lower()
        content = a.get("content")
        if name == "description" and content is not None:
            self.descriptions.append(_clean(content))
        if name in ("robots", "googlebot") and content is not None:
            self.robots.append(_clean(content).lower())
        if equiv == "refresh" and content is not None:
            self.meta_refresh = _clean(content)
        if name == "viewport" or prop == "viewport":
            self.viewport = True
        if name in ("og:image", "og:image:url") or prop in ("og:image", "og:image:url", "og:image:secure_url"):
            self.og_image = True

    def _link(self, a):
        rel = (a.get("rel") or "").strip().lower()
        rels = rel.split()
        if not rels:
            return
        href = a.get("href")
        if "canonical" in rels:
            self.canonicals.append(href if href is not None else "")
        if "alternate" in rels and a.get("hreflang"):
            self.hreflangs.append([_clean(a["hreflang"]).lower(), href or ""])
        if "preload" in rels and (a.get("as") or "").strip().lower() == "image":
            src = (href or "").strip()
            if not src:
                srcset = (a.get("imagesrcset") or "").strip()
                src = srcset.split(",")[0].strip().split(" ")[0] if srcset else ""
            if src:
                self.preloads.append(src)

    def _anchor(self, a):
        raw = (a.get("href") or "").strip()
        low = raw.lower()
        rel = (a.get("rel") or "").strip().lower()
        if not raw or raw.startswith("#") or low.startswith(_SKIP_HREF_PREFIXES):
            raw = ""                                    # not an internal-eligible link: keep the slot, drop the URL
        return {"href": raw, "rel": rel, "nofollow": "nofollow" in rel.split(), "aria": a.get("aria-label"),
                "title": a.get("title"), "in_nav": self.nav_depth > 0, "text": _Buf(4000), "alt": _Buf(1000)}

    def _close_anchor(self):
        if self._a_stack:
            self.links.append(self._a_stack.pop())

    def _img(self, a):
        alt = a.get("alt")
        if alt:
            if self._h1 is not None:
                self._h1.add(" " + alt + " ")
            if self._a_stack:
                self._a_stack[-1]["alt"].add(" " + alt + " ")
        self.images.append({"src": a.get("src") or "", "alt": alt, "width": a.get("width"),
                            "height": a.get("height"), "loading": a.get("loading")})

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag == "a":
            self._close_anchor()
        elif tag == "title" and self._title is not None:
            self.titles.append(_clean(self._title.text()))
            self._title = None
        elif tag == "h1" and self._h1 is not None:
            self.h1.append(_clean(self._h1.text()))
            self._h1 = None
        elif tag == "script" and self._ld is not None:
            self._finish_ld()
        self._unwind(tag)

    # -- text -----------------------------------------------------------------------------------------------
    def handle_data(self, data):
        if not data or not isinstance(data, str):
            return
        if self._ld is not None:
            self._ld.add(data)
            return
        if self._title is not None:
            self._title.add(data)
            return
        if self._h1 is not None:
            self._h1.add(data)
        if self._a_stack and not self.skip_depth:
            self._a_stack[-1]["text"].add(data)
        if self.skip_depth or self.head_depth:
            return
        if self.main_depth:
            self.main_buf.add(data)
        if self.body_depth or not self.has_body:
            self.body_buf.add(data)

    # -- ld+json --------------------------------------------------------------------------------------------
    def _finish_ld(self):
        buf = self._ld
        self._ld = None
        raw = buf.text().strip() if buf is not None else ""
        if not raw:
            self.jsonld_errors += 1
            return
        try:
            data = json.loads(raw, strict=False)
        except Exception:
            self.jsonld_errors += 1
            return
        try:
            self._walk_types(data, 0)
        except Exception:
            return

    def _add_type(self, value):
        t = _clean(value)
        if t and t not in self._seen_types and len(self.jsonld_types) < MAX_IMAGES:
            self._seen_types.add(t)
            self.jsonld_types.append(t)

    def _walk_types(self, obj, depth):
        if depth > 20:
            return
        if isinstance(obj, dict):
            t = obj.get("@type")
            if isinstance(t, str):
                self._add_type(t)
            elif isinstance(t, list):
                for item in t:
                    if isinstance(item, str):
                        self._add_type(item)
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    self._walk_types(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    self._walk_types(item, depth + 1)

    # -- result ---------------------------------------------------------------------------------------------
    def finish(self):
        """Flush every capture still open when the document ended (unclosed tags are normal on the web)."""
        if self._ld is not None:
            self._finish_ld()
        if self._title is not None:
            self.titles.append(_clean(self._title.text()))
            self._title = None
        if self._h1 is not None:
            self.h1.append(_clean(self._h1.text()))
            self._h1 = None
        while self._a_stack:
            self._close_anchor()

    def _eff_base(self):
        if not self.base_href:
            return self.base_url
        try:
            return urllib.parse.urljoin(self.base_url, self.base_href)
        except Exception:
            return self.base_url

    def document(self):
        base = self._eff_base()
        main_text = _clean(self.main_buf.text(), MAX_TEXT)
        body_text = _clean(self.body_buf.text(), MAX_TEXT)
        text = main_text if (self.has_main and main_text) else body_text
        canonicals = []
        for raw in self.canonicals[:MAX_LIST]:
            url = _resolve(raw, base)
            if url:
                canonicals.append(url)
        hreflang = []
        for pair in self.hreflangs[:MAX_HREFLANG]:
            try:
                lang, raw = pair[0], pair[1]
            except Exception:
                continue
            url = _resolve(raw, base)
            if url:
                hreflang.append([_clean(lang).lower(), url])
        links = []
        for rec in self.links:
            href = _resolve(rec.get("href"), base)
            if not href or len(href) > 2048:          # absurd hrefs are dropped, never queued or stored
                continue
            anchor = (_clean(rec["text"].text()) or _clean(rec["alt"].text()) or _clean(rec.get("aria"))
                      or _clean(rec.get("title")))
            links.append({"href": href, "anchor": anchor, "rel": rec.get("rel") or "",
                          "nofollow": bool(rec.get("nofollow")), "in_nav": bool(rec.get("in_nav"))})
            if len(links) >= MAX_LINKS:
                break
        images = []
        for im in self.images[:MAX_IMAGES]:
            images.append({"src": _resolve(im.get("src"), base), "alt": _maybe_clean(im.get("alt")),
                           "width": _maybe_clean(im.get("width")), "height": _maybe_clean(im.get("height")),
                           "loading": _maybe_clean(im.get("loading"))})
        preloads = []
        for raw in self.preloads[:MAX_IMAGES]:
            url = _resolve(raw, base)
            if url:
                preloads.append(url)
        posters = []
        for raw in self.posters[:MAX_LIST * 5]:
            url = _resolve(raw, base)
            if url:
                posters.append(url)
        return {
            "doctype": bool(self.doctype),
            "lang": self.lang or None,
            "titles": self.titles[:MAX_LIST],
            "descriptions": self.descriptions[:MAX_LIST],
            "robots": self.robots[:MAX_LIST],
            "canonicals": canonicals,
            "h1": self.h1[:MAX_LIST],
            "h2_count": int(self.h2_count),
            "hreflang": hreflang,
            "links": links,
            "jsonld_types": self.jsonld_types,
            "jsonld_errors": int(self.jsonld_errors),
            "text": text,
            "words": len(text.split()),
            "preload_images": preloads,
            "images": images,
            "video_posters": posters,
            "viewport": bool(self.viewport),
            "og_image": bool(self.og_image),
            "meta_refresh": self.meta_refresh,
        }


def _empty_document():
    """Skeleton of the PageDocument contract: every key present, nothing claimed."""
    return {"doctype": False, "lang": None, "titles": [], "descriptions": [], "robots": [], "canonicals": [],
            "h1": [], "h2_count": 0, "hreflang": [], "links": [], "jsonld_types": [], "jsonld_errors": 0,
            "text": "", "words": 0, "preload_images": [], "images": [], "video_posters": [], "viewport": False,
            "og_image": False, "meta_refresh": None}


def parse(html, base_url):
    """-> PageDocument dict for `html` (str, bytes or anything else) resolved against `base_url`. Never raises."""
    if isinstance(html, (bytes, bytearray, memoryview)):
        try:
            html = bytes(html).decode("utf-8", "replace")
        except Exception:
            html = ""
    elif not isinstance(html, str):
        try:
            html = "" if html is None else str(html)
        except Exception:
            html = ""
    base = base_url if isinstance(base_url, str) else ""
    parser = _PageParser(base)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    try:
        parser.finish()
    except Exception:
        pass
    try:
        return parser.document()
    except Exception:
        return _empty_document()


def selftest():
    """Offline assertions for this module; returns the number of checks run."""
    n = 0

    def chk(cond, msg):
        nonlocal n
        n += 1
        assert cond, "seo.extract.selftest: " + msg

    chk(template_of("https://x.com/") == "home", "root URL is the home template")
    chk(template_of("https://x.com") == "home", "empty path is the home template")
    chk(template_of("https://x.com/faq/") == "/faq/", "one segment template")
    chk(template_of("https://x.com/faq") == "/faq/", "one segment without trailing slash")
    chk(template_of("https://x.com/property-management/tampa/") == "/property-management/*/", "two segment template")
    chk(template_of("https://x.com/a/b/c/") == "/a/*/*/", "three+ segments collapse")
    chk(template_of("https://x.com/blog/2026-09/") == "/blog/*/", "date segment becomes {n}")
    chk(template_of("https://x.com/blog/42/post/") == "/blog/*/*/", "numeric segment becomes {n}")
    chk(template_of("https://x.com/sitemap.xml") == "asset", "asset extension")
    chk(template_of("https://x.com/x.PDF") == "asset", "asset extension is case-insensitive")
    chk(template_of("https://x.com/faq/?utm_source=x") == "/faq/", "query is ignored for the template")
    chk(template_of("https://x.com/img/logo.png") == "asset", "nested asset extension")

    chk(normalize("HTTPS://Example.COM:443/A?b=1#frag") == "https://example.com/A?b=1",
        "scheme/host lowercased, default port and fragment dropped, path case kept")
    chk(normalize("http://Example.com:80") == "http://example.com/", "empty path becomes /")
    chk(normalize("/a", "https://x.com/dir/page") == "https://x.com/a", "relative URL joined to base")
    chk(normalize("https://x.com:8443/a") == "https://x.com:8443/a", "non-default port kept")
    chk(normalize("mailto:a@b.c") == "", "non-http scheme gives ''")
    chk(normalize("javascript:void(0)") == "", "javascript gives ''")
    try:
        template_of(None)
        chk(False, "template_of(None) must raise ToolError")
    except ToolError:
        chk(True, "template_of(None) raises ToolError")

    html = (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<base href=\"https://cdn.example.com/sub/\">\n"
        "<title>  AdRails   Site  </title>\n"
        "<meta name=\"description\" content=\"We do ads.\">\n"
        "<meta name=\"robots\" content=\"INDEX, follow\">\n"
        "<meta name=\"viewport\" content=\"width=device-width\">\n"
        "<meta property=\"og:image\" content=\"/i.png\">\n"
        "<meta http-equiv=\"refresh\" content=\"0; url=/x\">\n"
        "<link rel=\"canonical\" href=\"/canon#frag\">\n"
        "<link rel=\"alternate\" hreflang=\"ES\" href=\"/es/\">\n"
        "<link rel=\"preload\" as=\"image\" href=\"/hero.png\">\n"
        "<link rel=\"preload\" as=\"image\" imagesrcset=\"/a.png 1x, /b.png 2x\">\n"
        "<style>body{color:red}</style>\n"
        "<script type=\"application/ld+json\">"
        "{\"@context\":\"https://schema.org\",\"@graph\":[{\"@type\":\"Organization\"},"
        "{\"@type\":\"WebSite\",\"author\":{\"@type\":\"Person\"}}]}</script>\n"
        "<script type=\"application/ld+json\">{broken</script>\n"
        "</head>\n<body>\n<nav><a href=\"/nav-link\">Nav</a></nav>\n"
        "<header><a href=\"/head-link\" rel=\"NOFOLLOW\">Head</a></header>\n<main>\n"
        "<h1>Big <img src=\"/logo.png\" alt=\"Logo\"> Head</h1>\n<h2>Sub</h2>\n"
        "<p>Hello world <a href=\"mailto:x@y.z\">mail</a><a href=\"#top\">top</a><a href=\"rel/page\">Rel</a>\n"
        "<a href=\"/x?utm_source=nl\">Utm</a><a>no href</a></p>\n"
        "<video poster=\"/poster.jpg\"></video>\n<script>var secret = 1;</script>\n</main>\n"
        "<footer><a href=\"tel:+123\">call</a></footer>\n<img src=\"/noalt.png\">\n</body>\n</html>"
    )
    doc = parse(html, "https://x.com/start/")
    chk(doc["doctype"] is True, "doctype detected")
    chk(doc["lang"] == "en", "html lang extracted")
    chk(doc["titles"] == ["AdRails Site"], "title collapsed")
    chk(doc["descriptions"] == ["We do ads."], "description extracted")
    chk(doc["robots"] == ["index, follow"], "robots lowercased")
    chk(doc["canonicals"] == ["https://cdn.example.com/canon"], "canonical resolved against <base href>, fragment dropped")
    chk(doc["h1"] == ["Big Logo Head"], "h1 includes img alt")
    chk(doc["h2_count"] == 1, "h2 counted")
    chk(doc["viewport"] is True and doc["og_image"] is True, "viewport and og:image flags")
    chk(doc["meta_refresh"] == "0; url=/x", "meta refresh content")
    chk(doc["hreflang"] == [["es", "https://cdn.example.com/es/"]], "hreflang pair")
    chk(doc["preload_images"] == ["https://cdn.example.com/hero.png", "https://cdn.example.com/a.png"],
        "preload href and imagesrcset first URL")
    chk(doc["video_posters"] == ["https://cdn.example.com/poster.jpg"], "video poster resolved")
    chk(doc["jsonld_errors"] == 1, "broken ld+json counted as one error")
    chk(doc["jsonld_types"] == ["Organization", "WebSite", "Person"], "ld+json @graph types in order")
    hrefs = [l["href"] for l in doc["links"]]
    chk(hrefs == ["https://cdn.example.com/nav-link", "https://cdn.example.com/head-link",
                  "https://cdn.example.com/sub/rel/page", "https://cdn.example.com/x?utm_source=nl"],
        "mailto/#top/no-href skipped, relatives resolved")
    chk(doc["links"][0]["in_nav"] is True and doc["links"][1]["in_nav"] is True, "nav/header links flagged in_nav")
    chk(doc["links"][1]["nofollow"] is True and doc["links"][1]["rel"] == "nofollow", "rel nofollow normalised")
    chk(doc["links"][2]["anchor"] == "Rel", "anchor text")
    chk(doc["images"][-1]["alt"] is None and doc["images"][-1]["src"] == "https://cdn.example.com/noalt.png",
        "img without alt keeps alt=None")
    chk("Hello world" in doc["text"] and "Nav" not in doc["text"], "text is main only")
    chk("var secret" not in doc["text"] and "body{color:red}" not in doc["text"], "script/style never in text")
    chk(doc["words"] == len(doc["text"].split()) and doc["words"] > 0, "word count")
    chk(isinstance(json.dumps(doc), str), "document is JSON-serialisable")

    doc2 = parse("<html><body><p>hi</p></body></html>", "https://x.com/")
    chk(doc2["doctype"] is False and doc2["lang"] is None, "missing doctype and lang")
    chk(doc2["titles"] == [] and doc2["links"] == [] and doc2["h1"] == [], "empty lists always present")
    chk(doc2["text"] == "hi", "body text when there is no main")

    doc3 = parse("<html><head><title>Real</title></head><body><svg><title>Icon</title></svg><p>x</p></body></html>",
                 "https://x.com/")
    chk(doc3["titles"] == ["Real"], "title inside svg is not a page title")

    doc4 = parse("<html><body>outer<main>inner</main>after</body></html>", "https://x.com/")
    chk(doc4["text"] == "inner", "main text preferred over body text")

    doc5 = parse("<html><body><script>SECRET</script><p>visible</p></body></html>", "https://x.com/")
    chk("SECRET" not in doc5["text"] and "visible" in doc5["text"], "script text is not page text")

    hostile = ["<" * 100000,
               "<a href=\"" + "x" * 1000000 + "\">x</a>",
               "<p>nul\x00byte\x00</p>",
               "<div>" * 5000 + "deep" + "</div>" * 5000,
               "<p>&#xZZ;&amp;&bogus;&#0;</p><a href=''>e</a><img src>",
               "<html lang=\"\"><title></title><meta name=robots><link rel=canonical>",
               b"<p>\xff\xfe non utf8</p>",
               "", None, 12345, "<base href=\":\"><a href=\"/x\">x</a>"]
    for bad in hostile:
        try:
            out = parse(bad, "https://x.com/")
            ok = isinstance(out, dict) and set(out) == set(_empty_document()) and isinstance(json.dumps(out), str)
        except Exception:
            ok = False
        chk(ok, "hostile input still yields a full document")
    return n
