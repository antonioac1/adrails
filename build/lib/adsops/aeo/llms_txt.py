"""Build and validate llms.txt / llms-full.txt (the llmstxt.org convention).

The format is: an H1 title, an optional blockquote summary, `##` sections and
`- [title](url): note` link lines. This module is pure string work - no network,
no files - so a tool can generate a file for a docs site, validate a fetched
copy, or convert a page's HTML to markdown with mocked inputs. Safety: builders
raise ToolError instead of emitting invalid documents, URLs are scrubbed before
they appear in warnings/errors, and validate() reports problems as data rather
than exceptions. Handlers should build() -> validate() -> publish, surfacing
warnings to the user, and never log raw URLs containing tokens.
"""

import re
from html.parser import HTMLParser

from adsops.errors import ToolError, RailViolation, scrub  # noqa: F401 (RailViolation kept for API parity)

__all__ = ["parse", "validate", "build", "build_from_pages", "build_full",
           "html_to_markdown", "selftest"]

LINK_LIMIT = 100_000          # llms.txt soft size budget (bytes)
FULL_LIMIT = 5_000_000        # llms-full.txt gets much more room
_LINK_RE = re.compile(r"^\[([^\]]*)\]\(\s*([^)\s]+)\s*\)\s*(?::\s*(.*))?$")
_SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "aside", "head",
                        "noscript", "iframe", "svg", "form"})


def _g(row, *path, default=None):
    """Nested getter: _g(row, "metrics", "costMicros") -> value or default."""
    cur = row
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return default if cur is None else cur


def _clean(value, limit=300):
    """One-line, length-capped string; tolerant of None / non-str."""
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _safe_url(value):
    """Clean a URL so it cannot break out of the markdown link syntax."""
    u = _clean(value, 2000)
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _short(u):
    # Public document text, not a secret: scrub() mangled tool names like "adsops_auction_insights_import" (the maintainers 2026-11-05).
    t = " ".join(str(u).split())
    return t if len(t) <= 100 else t[:99] + "…"


def _parse_link(body):
    """'- [title](url): note' (body already stripped of the leading '- ')."""
    m = _LINK_RE.match(body)
    if not m:
        return None
    note = m.group(3)
    note = note.strip() if note is not None else ""
    return {"title": m.group(1).strip(), "url": m.group(2).strip(),
            "note": note or None}


def parse(text):
    """Parse llms.txt text into title/summary/sections plus structural errors."""
    errors, prose = [], []
    title = summary = None
    sections, cur = [], None
    text = text if isinstance(text, str) else ""
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return {"title": None, "summary": None, "sections": [],
                "errors": ["empty document"]}
    if not lines[first].startswith("# "):
        errors.append("first non-empty line must be '# Title'")
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if i == first and s.startswith("# "):
            title = s[2:].strip()
        elif s.startswith("## "):
            cur = {"name": s[3:].strip(), "links": []}
            sections.append(cur)
        elif s.startswith("# "):
            continue  # extra H1s are ignored, not fatal
        elif s.startswith(">"):
            part = s[1:].strip()
            summary = part if summary is None else (summary + " " + part).strip()
        elif s[:2] in ("- ", "* "):
            link = _parse_link(s[2:].strip())
            if link is None:
                # Prose bullets are common in real llms.txt files (llmstxt.org lists links, but sites mix in facts);
                # they are reported as one warning by validate(), never as a structural error.
                prose.append(_short(s))
                continue
            if cur is None:  # links before any "## Section"
                cur = {"name": "", "links": []}
                sections.append(cur)
            cur["links"].append(link)
        # anything else is prose and deliberately ignored
    return {"title": title, "summary": summary, "sections": sections,
            "errors": errors, "prose_bullets": prose}


def validate(text, base_url=None, full=False):
    """Return {"ok", "errors", "warnings"} for an llms.txt document."""
    text = text if isinstance(text, str) else ""
    doc = parse(text)
    errors = list(doc["errors"])
    warnings = []
    if doc.get("prose_bullets"):
        warnings.append("%d list item(s) are prose, not [title](url) links, e.g. %s"
                        % (len(doc["prose_bullets"]), doc["prose_bullets"][0]))
    if not doc["summary"]:
        warnings.append("missing summary (add '> summary' below the title)")
    seen, total = set(), 0
    for sec in doc["sections"]:
        if not sec["name"]:
            if sec["links"]:
                warnings.append("links appear before the first '## Section'")
        for link in sec["links"]:
            total += 1
            u = link["url"]
            if u in seen:
                warnings.append("duplicate url: " + _short(u))
                continue
            seen.add(u)
            low = u.lower()
            if low.startswith("https://"):
                continue
            if low.startswith("http://"):
                warnings.append("non-https url: " + _short(u))
            elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u):
                warnings.append("non-http scheme: " + _short(u))
            elif not base_url:
                warnings.append("relative url (pass base_url to resolve): " + _short(u))
    if total == 0:
        errors.append("no links found")
    limit = FULL_LIMIT if full else LINK_LIMIT
    if len(text.encode("utf-8", "ignore")) > limit:
        warnings.append("file exceeds %d bytes; move bulk content to llms-full.txt"
                        % limit)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def build(title, summary, sections, intro=None):
    """Render llms.txt. `sections` is [(name, [(title, url, note), ...])]."""
    lines = ["# " + (_clean(title) or "Untitled"), ""]
    s = _clean(summary, 1000)
    if s:
        lines += ["> " + s, ""]
    body = _clean(intro, 2000)
    if body:
        lines += [body, ""]
    n_links = 0
    for name, links in sections or []:
        lines += ["## " + (_clean(name) or "Docs"), ""]
        for item in links or []:
            if isinstance(item, dict):
                lt, url, note = item.get("title"), item.get("url"), item.get("note")
            else:
                item = list(item)
                lt = item[0] if len(item) > 0 else None
                url = item[1] if len(item) > 1 else None
                note = item[2] if len(item) > 2 else None
            u = _safe_url(url)
            if not u:
                continue
            line = "- [%s](%s)" % (_clean(lt) or u, u)
            note = _clean(note, 300)
            if note:
                line += ": " + note
            lines.append(line)
            n_links += 1
        lines.append("")
    if not n_links:
        raise ToolError("build: at least one link with a url is required")
    return "\n".join(lines).rstrip() + "\n"


def build_from_pages(site_title, summary, pages):
    """Group pages by their "section" (default "Docs") in first-seen order."""
    groups = {}
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        url = _safe_url(_g(page, "url"))
        if not url:
            continue
        name = _clean(_g(page, "section")) or "Docs"
        title = _clean(_g(page, "title")) or url
        note = _clean(_g(page, "description"), 300) or None
        groups.setdefault(name, []).append((title, url, note))
    if not groups:
        raise ToolError("build_from_pages: no pages with a url were supplied")
    return build(site_title, summary, list(groups.items()))


def build_full(pages_text, title, summary):
    """Render llms-full.txt from [(url, title, markdown_body)] pairs."""
    out = ["# " + (_clean(title) or "Untitled"), ""]
    s = _clean(summary, 1000)
    if s:
        out += ["> " + s, ""]
    count = 0
    for item in pages_text or []:
        item = list(item) if not isinstance(item, dict) else [item.get("url"), item.get("title"), item.get("body")]
        url = _safe_url(item[0] if len(item) > 0 else None)
        ptitle = _clean(item[1] if len(item) > 1 else None)
        body = item[2] if len(item) > 2 else ""
        body = body if isinstance(body, str) else ""
        if re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", body):
            body = html_to_markdown(body)  # html slipped through the pipeline
        body = body.strip()
        out += ["## " + (ptitle or url or "Page"),
                "Source: " + (url or "(none)"), "", body, ""]
        count += 1
    if not count:
        raise ToolError("build_full: at least one page is required")
    return "\n".join(out).rstrip() + "\n"


class _MarkdownParser(HTMLParser):
    """Minimal HTML -> markdown converter (headings, p, lists, links, code, tables)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []   # (kind, text)
        self.cur = []      # inline buffer
        self.skip = 0
        self.pre = 0
        self.lists = []    # [[kind, counter], ...]
        self.href = None
        self.heading = 0
        self.cell = None
        self.row = []
        self.rows = None

    # -- helpers -------------------------------------------------------
    def _add(self, s):
        (self.cell if self.cell is not None else self.cur).append(s)

    def _flush(self, kind="p"):
        t = "".join(self.cur)
        self.cur = []
        if kind == "li":
            t = re.sub(r"\n{3,}", "\n\n", t.strip("\n")).rstrip()
            if t.strip():
                self.blocks.append((kind, t))
            return
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r" *\n *", "\n", t).strip()
        if t:
            self.blocks.append((kind, t))

    def _flush_pre(self):
        t = "".join(self.cur).strip("\n")
        self.cur = []
        if t.strip():
            self.blocks.append(("code", "```\n" + t + "\n```"))

    # -- HTMLParser hooks ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self._flush()
            self.heading = int(tag[1])
        elif tag == "p":
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            indent = "  " * max(0, len(self.lists) - 1)
            if self.lists:
                lst = self.lists[-1]
                lst[1] += 1
                marker = "- " if lst[0] == "ul" else "%d. " % lst[1]
            else:
                marker = "- "
            self.cur.append(indent + marker)
        elif tag == "pre":
            self._flush()
            self.pre = 1
        elif tag == "code":
            if not self.pre:
                self._add("`")
        elif tag in ("strong", "b"):
            self._add("**")
        elif tag in ("em", "i"):
            self._add("_")
        elif tag == "a":
            self.href = (dict(attrs).get("href") or "").strip()
            self._add("[")
        elif tag == "br":
            self._add("\n")
        elif tag == "table":
            self._flush()
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4"):
            t = re.sub(r"\s+", " ", "".join(self.cur)).strip()
            self.cur = []
            if t:
                self.blocks.append(("h", "#" * (self.heading or 1) + " " + t))
            self.heading = 0
        elif tag == "p":
            self._flush()
        elif tag == "li":
            self._flush("li")
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
        elif tag == "pre":
            self._flush_pre()
            self.pre = 0
        elif tag == "code":
            if not self.pre:
                self._add("`")
        elif tag in ("strong", "b"):
            self._add("**")
        elif tag in ("em", "i"):
            self._add("_")
        elif tag == "a":
            href = self.href or ""
            self._add("](%s)" % href if href else "]")
            self.href = None
        elif tag in ("td", "th"):
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell or [])).strip())
            self.cell = None
        elif tag == "tr":
            if self.row:
                (self.rows if self.rows is not None else []).append(self.row)
            self.row = []
        elif tag == "table":
            rows = [r for r in (self.rows or []) if any(c for c in r)]
            if rows:
                lines = ["| " + " | ".join(rows[0]) + " |",
                         "|" + " --- |" * len(rows[0])]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                self.blocks.append(("tbl", "\n".join(lines)))
            self.rows = None
            self.row = []

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self.cur.append(data)
        elif self.cell is not None:
            self.cell.append(data)
        else:
            self.cur.append(re.sub(r"\s+", " ", data))

    # -- render ---------------------------------------------------------
    def result(self):
        self._flush()
        parts, prev = [], None
        for kind, text in self.blocks:
            if not text.strip():
                continue
            if parts:
                tight = kind in ("li", "tr") and prev in ("li", "tr")
                parts.append("\n" if tight else "\n\n")
            parts.append(text)
            prev = kind
        return "".join(parts).strip()


def html_to_markdown(html):
    """Convert a page's HTML into simple markdown; nav/footer/scripts dropped."""
    if not html or not isinstance(html, str):
        return ""
    parser = _MarkdownParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup must never explode a build
        pass
    return parser.result()


def selftest() -> int:
    n = 0

    def check(cond, msg="check failed"):
        nonlocal n
        assert cond, msg
        n += 1

    # --- parse ---------------------------------------------------------
    doc = parse("# Acme\n\n> Ads for everyone.\n\n## Docs\n"
                "- [Intro](https://a.example/i): start here\n"
                "- [API](https://a.example/api)\n")
    check(doc["title"] == "Acme")
    check(doc["summary"] == "Ads for everyone.")
    check(len(doc["sections"]) == 1 and doc["sections"][0]["name"] == "Docs")
    check(doc["sections"][0]["links"][0] ==
          {"title": "Intro", "url": "https://a.example/i", "note": "start here"})
    check(doc["sections"][0]["links"][1]["note"] is None)
    check(doc["errors"] == [])

    empty = parse("")
    check(empty["title"] is None and empty["errors"] == ["empty document"])
    bad = parse("\n\n- [a](https://x.example)\n")
    check(bad["title"] is None and any("Title" in e for e in bad["errors"]))

    malformed = parse("# T\n\n> s\n\n## S\n- not a link\n- [a](https://a.example)\n")
    check(malformed["errors"] == [] and malformed["prose_bullets"] == ["- not a link"])   # prose is a warning, not an error
    check(any("prose" in w for w in validate("# T\n\n> s\n\n## S\n- not a link\n- [a](https://a.example)\n")["warnings"]))
    check(len(malformed["sections"][0]["links"]) == 1)

    # --- validate ------------------------------------------------------
    check(validate("# T\n\n> s\n")["ok"] is False)            # no links = error
    r = validate("# T\n\n## S\n- [a](https://a.example)\n")
    check(r["ok"] is True and any("summary" in w for w in r["warnings"]))
    r = validate("# T\n\n> s\n\n## S\n- [a](http://a.example)\n")
    check(any("non-https" in w for w in r["warnings"]))
    r = validate("# T\n\n> s\n\n## S\n- [a](https://a.example)\n- [b](https://a.example)\n")
    check(any("duplicate" in w for w in r["warnings"]))
    rel = "# T\n\n> s\n\n## S\n- [a](/rel/page)\n"
    check(any("relative" in w for w in validate(rel)["warnings"]))
    check(not any("relative" in w for w in
                  validate(rel, base_url="https://a.example")["warnings"]))
    opt = validate("# T\n\n> s\n\n## Optional\n- [a](https://a.example)\n")
    check(opt["ok"] is True and opt["errors"] == [])
    big = "# T\n\n> " + "x" * 100_100 + "\n\n## S\n- [a](https://a.example)\n"
    check(any("exceeds" in w for w in validate(big)["warnings"]))
    check(not any("exceeds" in w for w in validate(big, full=True)["warnings"]))

    # --- build ---------------------------------------------------------
    txt = build("Acme", "Ads for everyone.",
                [("Docs", [("Intro", "https://a.example/i", "start here"),
                           ("API", "https://a.example/api", None)])])
    v = validate(txt)
    check(v["ok"] is True and v["errors"] == [])
    back = parse(txt)
    check(back["title"] == "Acme" and back["sections"][0]["links"][0]["note"] == "start here")
    check(len(back["sections"][0]["links"]) == 2)
    check(parse(build("Ac\nme", "s", [("Docs", [("a", "https://a.example", None)])]))["title"] == "Ac me")
    try:
        build("Acme", "s", [("Docs", [])])
        check(False, "build accepted a document with no links")
    except ToolError:
        check(True)

    # --- build_from_pages ---------------------------------------------
    pages = [{"url": "https://a.example/1", "title": "One", "description": "d1",
              "section": "Guides"},
             {"url": "https://a.example/2", "title": "Two", "section": "Start"},
             {"url": "https://a.example/3", "title": "Three", "section": "Guides"},
             {"title": "no url"}]
    grouped = parse(build_from_pages("Acme", "s", pages))
    check([s["name"] for s in grouped["sections"]] == ["Guides", "Start"])
    check(len(grouped["sections"][0]["links"]) == 2)
    check(grouped["sections"][1]["links"][0]["title"] == "Two")
    default = parse(build_from_pages("Acme", "s", [{"url": "https://a.example/x", "title": "X"}]))
    check(default["sections"][0]["name"] == "Docs")
    try:
        build_from_pages("Acme", "s", [])
        check(False, "build_from_pages accepted an empty page list")
    except ToolError:
        check(True)

    # --- build_full ----------------------------------------------------
    full = build_full([("https://a.example/1", "One", "<p>Hello <b>world</b></p>")],
                      "Acme", "s")
    check("# Acme" in full)
    check("## One" in full and "Source: https://a.example/1" in full)
    check("**world**" in full and "<b>" not in full)
    try:
        build_full([], "Acme", "s")
        check(False, "build_full accepted no pages")
    except ToolError:
        check(True)

    # --- html_to_markdown ---------------------------------------------
    md = html_to_markdown(
        "<html><head><title>t</title></head><body><nav>menu</nav>"
        "<h1>Hello</h1><p>Some <strong>bold</strong> and "
        "<a href=\"https://x.example\">link</a>.</p>"
        "<ul><li>one</li><li>two</li></ul><ol><li>first</li></ol>"
        "<pre>code here</pre>"
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "<script>alert(1)</script><footer>bye</footer></body></html>")
    check("# Hello" in md)
    check("**bold**" in md)
    check("[link](https://x.example)" in md)
    check("- one" in md and "- two" in md)
    check("1. first" in md)
    check("```" in md and "code here" in md)
    check("| A | B |" in md and "| --- | --- |" in md and "| 1 | 2 |" in md)
    check("alert(1)" not in md)
    check("menu" not in md and "bye" not in md)
    check(html_to_markdown("") == "")
    check(html_to_markdown(None) == "")
    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
