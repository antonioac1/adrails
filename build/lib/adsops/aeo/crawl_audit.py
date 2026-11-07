"""AI-crawler accessibility audit for a website (robots.txt, sitemap, page directives).

Parses RFC 9309 robots.txt rules and checks whether common AI crawlers may fetch key paths.
Audits HTML pages for robots meta/X-Robots-Tag blocks, canonical/title/description/lang issues,
thin visible body text, noscript-only content and JS-dependent rendering. A site-level audit
fetches robots.txt, llms.txt, sitemap.xml and the home page through an injected `fetch` callable,
merges findings, and computes a 0-100 score. All external effects are injectable so tools/tests
can mock networking. Return values are JSON-serialisable and never include secrets.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from adsops.errors import ToolError


AI_BOTS = {
    "GPTBot": {"owner": "OpenAI", "purpose": "training"},
    "OAI-SearchBot": {"owner": "OpenAI", "purpose": "search"},
    "ChatGPT-User": {"owner": "OpenAI", "purpose": "user-fetch"},
    "ClaudeBot": {"owner": "Anthropic", "purpose": "training"},
    "Claude-SearchBot": {"owner": "Anthropic", "purpose": "search"},
    "Claude-User": {"owner": "Anthropic", "purpose": "user-fetch"},
    "PerplexityBot": {"owner": "Perplexity", "purpose": "search"},
    "Perplexity-User": {"owner": "Perplexity", "purpose": "user-fetch"},
    "Google-Extended": {"owner": "Google", "purpose": "training"},
    "Googlebot": {"owner": "Google", "purpose": "search"},
    "Bingbot": {"owner": "Microsoft", "purpose": "search"},
    "Applebot": {"owner": "Apple", "purpose": "search"},
    "Applebot-Extended": {"owner": "Apple", "purpose": "training"},
    "DuckAssistBot": {"owner": "DuckDuckGo", "purpose": "user-fetch"},
    "Meta-ExternalAgent": {"owner": "Meta", "purpose": "training"},
    "Amazonbot": {"owner": "Amazon", "purpose": "search"},
    "CCBot": {"owner": "CommonCrawl", "purpose": "training"},
    "Bytespider": {"owner": "ByteDance", "purpose": "training"},
}

SEVERITY_WEIGHTS = {"critical": 25, "high": 12, "medium": 6, "low": 2, "info": 0}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _finding(severity, code, message):
    """Create a consistent JSON-safe finding dict."""
    return {"severity": severity, "code": code, "message": message}


def _sorted_findings(findings):
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER.get(f.get("severity"), 99),
            f.get("code", ""),
            f.get("message", ""),
        ),
    )


def _text(body):
    """Accept str/bytes/None and return text; bytes are decoded defensively."""
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body or ""


def _g(obj, *path, default=None):
    """Nested get for camelCase Google Ads-ish dicts; missing/non-dict -> default."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
        if cur is default:
            return default
    return cur


def parse_robots(text):
    """Parse robots.txt into groups/sitemaps per RFC 9309 (case-insensitive keys)."""
    groups, sitemaps = [], []
    cur, rules_started = None, False

    for raw in _text(text).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            cur, rules_started = None, False
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()

        if key == "user-agent":
            # A user-agent line after rules starts a new group; contiguous UA
            # lines before rules all belong to the same group.
            if cur is None or rules_started:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
                rules_started = False
            cur["agents"].append(val)
        elif key in ("allow", "disallow"):
            if cur is None:
                cur = {"agents": ["*"], "rules": []}
                groups.append(cur)
            if not val:
                # Empty directive is effectively unrestricted; skip it.
                rules_started = True
                continue
            cur["rules"].append((key, val))
            rules_started = True
        elif key == "sitemap" and val:
            sitemaps.append(val)
        # Unknown directives (crawl-delay, host, ...) are ignored.

    return {"groups": groups, "sitemaps": sitemaps}


def _match_len(pattern, path):
    """Return length of RFC-style prefix match, or -1. Supports '*' and '$'."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    if anchored:
        regex += "$"
    m = re.match(regex, path)
    if not m:
        return -1
    return len(path) if anchored else m.end()


def can_fetch(parsed, agent, path):
    """RFC 9309 longest-match check; allow wins ties; '*' group is fallback."""
    if not isinstance(parsed, dict):
        return True
    path = path or "/"
    agent_l = (agent or "").strip().lower()
    specific = [
        g for g in parsed.get("groups", [])
        if any(str(a).strip().lower() == agent_l for a in g.get("agents", []))
    ]
    selected = specific if specific else [
        g for g in parsed.get("groups", [])
        if any(str(a).strip() == "*" for a in g.get("agents", []))
    ]

    best_len, best_type = -1, None
    for group in selected:
        for rule_type, pattern in group.get("rules", []):
            n = _match_len(pattern, path)
            if n < 0:
                continue
            if n > best_len or (n == best_len and rule_type == "allow"):
                best_len, best_type = n, rule_type
    if best_type is None:
        return True
    return best_type == "allow"


def audit_robots(text, paths=("/", "/llms.txt", "/sitemap.xml")):
    """Audit robots.txt for AI-crawler accessibility and report findings."""
    parsed = parse_robots(text)
    bots, findings = [], []

    for bot, info in AI_BOTS.items():
        allowed = {p: can_fetch(parsed, bot, p) for p in paths}
        bots.append({
            "bot": bot,
            "owner": info["owner"],
            "purpose": info["purpose"],
            "allowed": allowed,
        })
        root_ok = allowed.get("/", True)
        if info["purpose"] in ("search", "user-fetch") and not root_ok:
            findings.append(_finding(
                "critical", "bot_blocked_root",
                f"{bot} ({info['purpose']}) is blocked on / — invisible to {info['owner']} answers",
            ))
        elif info["purpose"] == "training":
            # Training access is a policy choice; record both outcomes as INFO.
            state = "allowed" if root_ok else "blocked"
            findings.append(_finding(
                "info", f"training_{state}",
                f"{bot} training crawler {state} on / (policy choice)",
            ))

    if not parsed["sitemaps"]:
        findings.append(_finding("medium", "no_sitemap", "robots.txt has no Sitemap: line"))

    return {"bots": bots, "sitemaps": parsed["sitemaps"], "findings": _sorted_findings(findings)}


def _header(headers, name):
    if not headers:
        return ""
    lower = name.lower()
    for key, val in headers.items():
        if str(key).lower() == lower:
            if isinstance(val, (list, tuple)):
                return ",".join(str(x) for x in val)
            return str(val)
    return ""


def _has_noindex(value):
    return re.search(r"(?:^|[\s,;:])noindex(?:$|[\s,;:])", value.lower()) is not None


def _has_nosnippet(value):
    return re.search(r"(?:^|[\s,;:])nosnippet(?:$|[\s,;:])", value.lower()) is not None


def _has_max_snippet_zero(value):
    return re.search(r"max-snippet\s*:\s*0(?!\d)", value.lower()) is not None


def _word_count(text):
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


class _PageParser(HTMLParser):
    """Extract title, meta, canonical, lang, body text and scripts from HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.title_parts = []
        self.meta = {}
        self.canonical = None
        self.lang = None
        self.script_count = 0
        self._skip = 0
        self._in_body = False
        self._in_head = False
        self._in_title = False
        self._in_noscript = 0
        self._text_parts = []
        self._noscript_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = attrs.get("lang") or self.lang
        elif tag == "head":
            self._in_head = True
        elif tag == "body":
            self._in_body = True
        elif tag == "title":
            self._in_title = True
            self.title_parts = []
        elif tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
            if name:
                self.meta[name] = attrs.get("content", "")
        elif tag == "link":
            rels = attrs.get("rel", "").lower().split()
            if "canonical" in rels:
                self.canonical = attrs.get("href") or None
        elif tag == "script":
            self.script_count += 1
            self._skip += 1
        elif tag in ("style", "nav"):
            self._skip += 1
        elif tag == "noscript":
            self._in_noscript += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in ("script", "style", "nav", "noscript", "title"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "head":
            self._in_head = False
        elif tag == "body":
            self._in_body = False
        elif tag == "title":
            self._in_title = False
            self.title = "".join(self.title_parts)
        elif tag == "script":
            self._skip = max(0, self._skip - 1)
        elif tag in ("style", "nav"):
            self._skip = max(0, self._skip - 1)
        elif tag == "noscript":
            self._in_noscript = max(0, self._in_noscript - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_noscript:
            self._noscript_parts.append(data)
        elif self._skip == 0 and (self._in_body or not self._in_head):
            self._text_parts.append(data)


def audit_page(url, html, headers):
    """Audit one rendered HTML page for AI-crawler and classic SEO blockers."""
    parser = _PageParser()
    parser.feed(_text(html))
    parser.close()

    title = (parser.title if parser.title is not None else "".join(parser.title_parts)).strip()
    desc = (parser.meta.get("description") or "").strip()
    body_text = " ".join(parser._text_parts)
    noscript_text = " ".join(parser._noscript_parts)
    body_words = _word_count(body_text)
    noscript_words = _word_count(noscript_text)
    findings = []

    # Directives may arrive in X-Robots-Tag and/or a meta robots tag.
    directives = []
    x_robots = _header(headers, "x-robots-tag")
    if x_robots:
        directives.append(("X-Robots-Tag", x_robots))
    meta_robots = " ".join(
        v for k, v in parser.meta.items()
        if "robots" in k or k in ("googlebot", "bingbot", "gptbot")
    )
    if meta_robots.strip():
        directives.append(("meta robots", meta_robots))

    for source, value in directives:
        if _has_noindex(value) or _has_nosnippet(value) or _has_max_snippet_zero(value):
            findings.append(_finding(
                "critical", "robots_directive",
                f"{source} blocks indexing/snippets: {value[:80]}",
            ))
            break  # One critical finding is enough to describe the block.

    canonical = (parser.canonical or "").strip()
    if not canonical:
        findings.append(_finding("medium", "canonical_missing", "missing canonical link"))
    else:
        resolved = urljoin(url, canonical)
        if urlparse(resolved).netloc.lower() != urlparse(url).netloc.lower():
            findings.append(_finding("medium", "canonical_foreign", "canonical points to another host"))
        if urlparse(resolved).scheme.lower() != "https":
            findings.append(_finding("low", "canonical_not_https", "canonical is not HTTPS"))

    if not title:
        findings.append(_finding("medium", "title_missing", "missing <title>"))
    if not desc:
        findings.append(_finding("medium", "description_missing", "missing meta description"))
    elif len(desc) > 160:
        findings.append(_finding("low", "description_long", "meta description is over 160 characters"))

    if not parser.lang or not parser.lang.strip():
        findings.append(_finding("low", "lang_missing", "html lang attribute is missing"))

    if body_words < 200:
        findings.append(_finding(
            "high", "thin_body",
            f"visible body text is only {body_words} words; AI fetchers read body text only",
        ))
    if noscript_words >= 200 and body_words < 200:
        findings.append(_finding(
            "high", "noscript_only", "substantive content appears only inside <noscript>",
        ))
    if body_words < 50 and parser.script_count >= 3:
        findings.append(_finding(
            "high", "requires_js", "page appears to require JavaScript to render body content",
        ))

    return {
        "findings": _sorted_findings(findings),
        "meta": {
            "title": title,
            "description": desc,
            "canonical": canonical,
            "lang": parser.lang or "",
            "body_words": body_words,
            "script_count": parser.script_count,
            "noscript_words": noscript_words,
            "x_robots_tag": x_robots,
        },
    }


def _safe_fetch(fetch, url):
    """Call injected fetch and never leak exception text into results."""
    try:
        status, headers, body = fetch(url)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        return status, headers or {}, body or ""
    except Exception:
        return 0, {}, ""


def _score(findings):
    score = 100
    for finding in findings:
        score -= SEVERITY_WEIGHTS.get(finding.get("severity"), 0)
    return max(0, min(100, score))


def audit_site(base_url, fetch):
    """Fetch robots/llms/sitemap/home via `fetch` and return a merged audit."""
    parsed_base = urlparse(base_url or "")
    if parsed_base.scheme not in ("http", "https") or not parsed_base.netloc:
        raise ToolError("base_url must be an absolute http(s) URL")

    root = f"{parsed_base.scheme}://{parsed_base.netloc}"
    robots_url = root + "/robots.txt"
    llms_url = root + "/llms.txt"
    sitemap_url = root + "/sitemap.xml"

    r_status, _, r_body = _safe_fetch(fetch, robots_url)
    l_status, _, l_body = _safe_fetch(fetch, llms_url)
    s_status, _, s_body = _safe_fetch(fetch, sitemap_url)
    h_status, h_headers, h_body = _safe_fetch(fetch, base_url)

    robots = audit_robots(_text(r_body))
    robots["status"] = r_status

    llms_text = _text(l_body)
    llms = {"status": l_status, "bytes": len(llms_text.encode("utf-8"))}

    locs = re.findall(r"<loc\b[^>]*>.*?</loc>", _text(s_body), flags=re.I | re.S)
    sitemap = {"status": s_status, "urls": len(locs)}

    home = audit_page(base_url, _text(h_body), h_headers)

    findings = list(robots["findings"]) + list(home["findings"])
    if r_status != 200:
        findings.append(_finding("medium", "robots_unreachable", f"robots.txt returned HTTP {r_status}"))
    if l_status != 200:
        findings.append(_finding("low", "llms_txt_missing", f"llms.txt returned HTTP {l_status}"))
    if s_status != 200:
        findings.append(_finding("medium", "sitemap_unreachable", f"sitemap.xml returned HTTP {s_status}"))
    elif sitemap["urls"] == 0:
        findings.append(_finding("medium", "sitemap_empty", "sitemap.xml contains no <loc> URLs"))
    if h_status == 0:
        findings.append(_finding("high", "home_unreachable", "home page could not be fetched"))
    elif h_status >= 400:
        findings.append(_finding("high", "home_error", f"home page returned HTTP {h_status}"))

    findings = _sorted_findings(findings)
    return {
        "robots": robots,
        "llms_txt": llms,
        "sitemap": sitemap,
        "home": home,
        "score": _score(findings),
        "findings": findings,
    }


def selftest() -> int:
    """Run mocked assertions covering happy paths and edge cases."""
    n = 0

    def check(cond, msg="assertion failed"):
        nonlocal n
        assert cond, msg
        n += 1

    robots_txt = """# comment
User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /private
Allow: /private/public

User-agent: *
Disallow: /
Sitemap: https://example.com/sitemap.xml
"""
    parsed = parse_robots(robots_txt)
    check(len(parsed["groups"]) == 2, "expected two robots groups")
    check(parsed["groups"][0]["agents"] == ["GPTBot", "ClaudeBot"], "multiple UA lines grouped")
    check(("disallow", "/private") in parsed["groups"][0]["rules"], "disallow parsed")
    check(parsed["sitemaps"] == ["https://example.com/sitemap.xml"], "sitemap parsed")

    check(can_fetch(parsed, "GPTBot", "/private/x") is False, "specific disallow")
    check(can_fetch(parsed, "GPTBot", "/private/public/x") is True, "longest allow wins")
    check(can_fetch(parsed, "OtherBot", "/anything") is False, "wildcard fallback disallow")
    check(can_fetch(parsed, "GPTBot", "/other") is True, "unmatched specific group allows")

    wild = parse_robots("User-agent: *\nDisallow: /*.pdf$\nAllow: /public$\n")
    check(can_fetch(wild, "X", "/a.pdf") is False, "wildcard and end anchor")
    check(can_fetch(wild, "X", "/a.pdf?x=1") is True, "end anchor rejects query suffix")
    check(can_fetch(wild, "X", "/public") is True, "anchored allow")

    ra = audit_robots("User-agent: *\nDisallow: /\n", paths=("/",))
    crit = [f for f in ra["findings"] if f["severity"] == "critical" and f["code"] == "bot_blocked_root"]
    check(len(crit) >= 5, "search/user-fetch bots blocked on root")
    check(any(f["code"] == "no_sitemap" and f["severity"] == "medium" for f in ra["findings"]),
          "missing sitemap finding")
    ra2 = audit_robots("User-agent: *\nAllow: /\nSitemap: x\n", paths=("/",))
    check(not any(f["severity"] == "critical" for f in ra2["findings"]), "allowed robots has no critical")

    good_html = (
        "<html lang='en'><head><title>Hi</title>"
        "<meta name='description' content='short'>"
        "<link rel='canonical' href='https://example.com/x'></head>"
        "<body><nav>menu</nav><p>" + ("word " * 250) + "</p></body></html>"
    )
    page = audit_page("https://example.com/x", good_html, {})
    check(page["meta"]["title"] == "Hi", "title extracted")
    check(page["meta"]["body_words"] >= 250, "body words counted after nav stripped")
    check(not any(f["code"] == "thin_body" for f in page["findings"]), "no thin body on good page")

    blocked = audit_page(
        "https://example.com/", "<html><body>" + ("word " * 210) + "</body></html>",
        {"X-Robots-Tag": "noindex"},
    )
    check(any(f["severity"] == "critical" and f["code"] == "robots_directive" for f in blocked["findings"]),
          "X-Robots-Tag noindex critical")

    noscript_html = (
        "<html><head><title>T</title></head><body><script>x</script><noscript>"
        + ("word " * 210) + "</noscript></body></html>"
    )
    noscript_page = audit_page("http://example.com/a", noscript_html, {})
    check(any(f["code"] == "noscript_only" and f["severity"] == "high" for f in noscript_page["findings"]),
          "noscript-only content detected")
    check(any(f["code"] == "canonical_missing" for f in noscript_page["findings"]),
          "missing canonical reported")

    foreign = audit_page(
        "https://example.com/",
        "<html><head><title>T</title><link rel='canonical' href='https://other.com/x'></head>"
        "<body>" + ("word " * 210) + "</body></html>", {},
    )
    check(any(f["code"] == "canonical_foreign" for f in foreign["findings"]), "foreign canonical reported")

    long_desc = "a" * 161
    long_page = audit_page(
        "https://example.com/",
        "<html lang='en'><head><title>T</title><meta name='description' content='"
        + long_desc + "'></head><body>" + ("word " * 210) + "</body></html>", {},
    )
    check(any(f["code"] == "description_long" and f["severity"] == "low" for f in long_page["findings"]),
          "long description low finding")

    pages = {
        "https://ex.test/robots.txt": (
            200, {}, "User-agent: *\nAllow: /\nSitemap: https://ex.test/sitemap.xml"
        ),
        "https://ex.test/llms.txt": (200, {}, "hello"),
        "https://ex.test/sitemap.xml": (
            200, {}, "<urlset><url><loc>https://ex.test/a</loc></url>"
                    "<url><loc>https://ex.test/b</loc></url></urlset>"
        ),
        "https://ex.test/": (
            200, {},
            "<html lang='en'><head><title>Home</title>"
            "<meta name='description' content='ok'>"
            "<link rel='canonical' href='https://ex.test/'></head><body>"
            + ("word " * 250) + "</body></html>"
        ),
    }
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url not in pages:
            raise RuntimeError("missing " + url)  # text must not leak into results
        return pages[url]

    site = audit_site("https://ex.test/", fake_fetch)
    check(site["robots"]["status"] == 200, "robots status captured")
    check(site["llms_txt"]["bytes"] == 5, "llms bytes counted")
    check(site["sitemap"]["urls"] == 2, "sitemap locs counted")
    check(site["score"] == 100, "clean mocked site scores 100")
    check(len(calls) == 4, "all four site URLs fetched")

    failed = audit_site("https://ex.test", lambda u: (0, {}, ""))
    check(failed["score"] < 100, "failed site loses points")
    check(isinstance(failed["findings"], list), "failed site findings remain a list")

    try:
        audit_site("not a url", lambda u: (200, {}, ""))
    except ToolError:
        check(True, "bad base_url raises ToolError")
    else:
        check(False, "expected ToolError for bad base_url")

    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
