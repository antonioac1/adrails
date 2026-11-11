"""Additive page extensions (AdRails 0.12, QA fix B2). PM-written.

An EXTEND bundle adds new sections to a page that is already live. It must never change anything else: not the <title>,
the meta description, the H1, the canonical, the og:* tags, nor the page's existing structured data. This module:

  * reads the live page's head and JSON-LD blocks (live_page),
  * MERGES the draft's FAQ into the existing JSON-LD (append Question nodes to an existing FAQPage, or add one FAQPage
    node) and bumps existing dateModified values, returning the exact resulting blocks (never a replacement),
  * writes extend.json + jsonld.before.json + jsonld.after.json + verify_extend.py for the implementing agent, and the
    extend section of IMPLEMENT.md,
  * ships a verifier the agent must run: everything outside the one inserted region, the declared live changes and the
    JSON-LD blocks must be unchanged, and the JSON-LD must equal jsonld.after.json exactly.
"""
import copy
import hashlib
import html as html_mod
import json
import re
from html.parser import HTMLParser

from ..errors import ToolError

MAX_HTML = 3_000_000
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class _Page(HTMLParser):
    """Linear, tolerant parse of a page: head facts and raw ld+json blocks (QA N11: no backtracking regexes)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.description, self.canonical, self.og, self.h1 = None, None, None, {}, []
        self.ld, self._ld, self._title, self._h1 = [], None, None, None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
            self._ld = []
        elif tag == "title" and self.title is None:
            self._title = []
        elif tag == "h1":
            self._h1 = []
        elif tag == "meta":
            name, prop = a.get("name", "").lower(), a.get("property", "").lower()
            if name == "description":
                self.description = a.get("content")
            key = prop if prop.startswith(("og:", "twitter:")) else name if name.startswith("twitter:") else None
            if key:
                self.og[key] = a.get("content")
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = a.get("href")

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ld.append("".join(self._ld))
            self._ld = None
        elif tag == "title" and self._title is not None:
            self.title, self._title = " ".join("".join(self._title).split()), None
        elif tag == "h1" and self._h1 is not None:
            self.h1.append(" ".join("".join(self._h1).split()))
            self._h1 = None

    def handle_data(self, data):
        for buf in (self._ld, self._title, self._h1):
            if buf is not None:
                buf.append(data)


def _parse_page(html):
    if not isinstance(html, str):
        raise ToolError("the live page is not text")
    if len(html) > MAX_HTML:
        raise ToolError("the live page is larger than %d characters; refusing to model it" % MAX_HTML)
    p = _Page()
    p.feed(html)
    p.close()
    return p


def head_facts(html):
    """The head values an extension must never change: title, meta description, canonical, og/twitter tags, H1s."""
    p = _parse_page(html)
    return {"title": p.title, "description": p.description, "canonical": p.canonical, "og": p.og, "h1": p.h1}


def _nodup_pairs(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def jsonld_blocks(html):
    """-> list of (parsed_or_None, raw_text) for every ld+json block, in page order."""
    out = []
    for raw in _parse_page(html).ld:
        try:
            out.append((json.loads(raw), raw))
        except (ValueError, RecursionError):
            out.append((None, raw))
    return out


def live_page(html, url):
    """Everything the bundle needs about the live page. Refuses pages it cannot model safely."""
    if not isinstance(html, str) or not html.strip():
        raise ToolError("the live page to extend could not be read")
    p = _parse_page(html)
    blocks = []
    for raw in p.ld:
        try:
            blocks.append(json.loads(raw, object_pairs_hook=_nodup_pairs))   # duplicate keys = unparseable (refused)
        except (ValueError, RecursionError):
            blocks.append(None)
    return {"url": url, "sha256": hashlib.sha256(html.encode("utf-8", "surrogatepass")).hexdigest(),
            "head": {"title": p.title, "description": p.description, "canonical": p.canonical, "og": p.og,
                     "h1": p.h1}, "jsonld": blocks, "jsonld_unparseable": sum(1 for b in blocks if b is None)}


def _nodes(block):
    """Top-level nodes of one JSON-LD block (the block itself, its @graph list or object, or its list items)."""
    if isinstance(block, list):
        return [n for n in block if isinstance(n, dict)]
    if isinstance(block, dict):
        g = block.get("@graph")
        if isinstance(g, dict):
            return [g]
        return [n for n in g if isinstance(n, dict)] if isinstance(g, list) else [block]
    return []


def _types(node):
    t = node.get("@type")
    return {str(x) for x in t} if isinstance(t, list) else {str(t)}


def _one(s, n=200):
    return " ".join(str(s).replace("`", "'").split())[:n]


def merge_jsonld(blocks, faq, today):
    """-> (after_blocks, ops). Only ADDS: Question nodes (to the one existing FAQPage, or one new FAQPage node) and sets
    existing plain-string dateModified values to today. Never removes or rewrites anything else."""
    if any(b is None for b in blocks):
        raise ToolError("the live page has a JSON-LD block that does not parse; an extension cannot merge into it safely")
    after = copy.deepcopy(blocks)
    ops = []
    questions, seen = [], set()
    for q in faq or []:
        if not isinstance(q, dict) or not isinstance(q.get("q"), str) or not isinstance(q.get("a"), str) \
                or not q["q"].strip() or not q["a"].strip():
            raise ToolError("every FAQ item needs a non-empty text question and answer")
        if any(ch in q["q"] + q["a"] for ch in "<>"):             # never markup inside an embedded ld+json script
            raise ToolError("FAQ text may not contain < or >")
        key = " ".join(q["q"].lower().split())
        if key in seen:                                            # QA N11: duplicate draft questions
            continue
        seen.add(key)
        questions.append({"@type": "Question", "name": q["q"].strip(),
                          "acceptedAnswer": {"@type": "Answer", "text": q["a"].strip()}})
    if questions:
        targets = [(bi, n) for bi, b in enumerate(after) for n in _nodes(b) if "FAQPage" in _types(n)]
        if len(targets) > 1:
            raise ToolError("the live page has more than one FAQPage node; an extension cannot merge safely")
        if targets:
            bi, node = targets[0]
            ents = node.get("mainEntity", [])
            if isinstance(ents, dict):
                ents = [ents]
            if not isinstance(ents, list):
                raise ToolError("the live FAQPage has a mainEntity that is not a list or object; refusing to merge")
            have = {" ".join(str(e.get("name", "")).lower().split()) for e in ents if isinstance(e, dict)}
            add = [q for q in questions if " ".join(q["name"].lower().split()) not in have]
            node["mainEntity"] = ents + add
            ops.append("modify node: append %d Question node(s) to the mainEntity of the FAQPage node%s in JSON-LD block %d"
                       % (len(add), (" with @id %s" % _one(node["@id"], 120)) if node.get("@id") else "", bi + 1))
        else:
            node = {"@type": "FAQPage", "mainEntity": questions}
            graph_block = next((b for b in after if isinstance(b, dict) and isinstance(b.get("@graph"), list)), None)
            if graph_block is not None:
                graph_block["@graph"].append(node)
                ops.append("add node: append one FAQPage node at the end of the @graph of JSON-LD block %d"
                           % (after.index(graph_block) + 1))
            else:
                after.append(dict({"@context": "https://schema.org"}, **node))
                ops.append("add block: add one new <script type=\"application/ld+json\"> block (a FAQPage) after the "
                           "existing ones (after the last existing block in page order)")
    for bi, b in enumerate(after):
        for n in _nodes(b):
            if isinstance(n.get("dateModified"), str) and n["dateModified"] != today:   # typed values left alone
                n["dateModified"] = today
                ops.append("modify node: set dateModified to %s on the %s node in JSON-LD block %d" % (
                    today, _one("/".join(sorted(_types(n))), 80), bi + 1))
    return after, ops


VERIFIER = r'''#!/usr/bin/env python3
"""verify_extend.py ORIGINAL.html NEW.html  (shipped in an AdRails extend bundle; stdlib only). Prints OK or FAIL lines.
NEW must parse to exactly ORIGINAL's structure plus ONE well-formed inserted block placed right after the anchor section,
with only the declared live changes, and with JSON-LD equal to jsonld.after.json (blocks in the same page order; a new block
goes after the existing ones)."""
import hashlib, json, os, re, sys
from html.parser import HTMLParser
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "extend.json"), encoding="utf-8"))
after = json.load(open(os.path.join(here, "jsonld.after.json"), encoding="utf-8"))
old, new = (open(p, encoding="utf-8").read() for p in sys.argv[1:3])
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
FORBID = set("script iframe object embed style form link meta template noscript details dialog svg math base frame "
             "frameset input button select textarea".split())
# round 2 review: the insert may only use elements whose text a browser renders as page content (allowlist)
ALLOW = set("p h1 h2 h3 h4 h5 h6 ul ol li dl dt dd a strong em b i u s small mark sub sup abbr cite q code pre kbd "
            "blockquote br hr table thead tbody tfoot tr th td caption div section article aside span figure "
            "figcaption".split())
HIDE_CLASS = re.compile(r"(^|\s)(hidden|d-none|sr-only|visually-hidden|invisible|collapse)(\s|$)", re.I)
# round 2 review: any class token that reads as hiding, at any breakpoint/framework spelling
HIDE_TOKEN = re.compile(r"(^|[-_:])(none|hidden|hide|invisible|collapsed?|offscreen|off-screen|clip|clipped)($|[-_])|"
                        r"sr-only|screen-?reader|visually-?hidden|opacity-0|(^|:)(max-)?[hw]-0$|"
                        r"text-transparent|indent", re.I)
# attributes an inserted element may carry (no src/srcset/ping/style/on*/data-*: nothing loads, nothing hides)
ALLOW_ATTR = set("href class id title rel target colspan rowspan scope start reversed type lang dir headers abbr "
                 "cite datetime".split())
HIDE_ANCESTOR = set("template noscript details dialog head title datalist noembed noframes select textarea "
                    "script style object canvas audio video svg math".split())
fails = []
if max(len(old), len(new)) > 5000000:
    print("FAIL page too large to verify"); sys.exit(1)
if hashlib.sha256(old.encode("utf-8", "surrogatepass")).hexdigest() != spec["original_sha256"]:
    fails.append("ORIGINAL.html is not the page this bundle was reviewed against (sha256 differs): stop and report")
class P(HTMLParser):
    def __init__(s):
        super().__init__(convert_charrefs=True); s.t, s.ld, s._ld, s.ldattrs = [], [], None, []
    def handle_starttag(s, tag, attrs):
        if tag == "script" and any(k == "type" and (v or "").strip().lower() == "application/ld+json" for k, v in attrs):
            s.ldattrs.append(tuple(sorted((k, v or "") for k, v in attrs))); s._ld = []; return
        s.t.append(("S", tag, tuple(sorted((k, v or "") for k, v in attrs))))
    def handle_startendtag(s, tag, attrs):
        # "<div/>" leaves a div OPEN in HTML: only void elements close themselves (round 2 review)
        s.handle_starttag(tag, attrs)
    def handle_endtag(s, tag):
        if tag == "script" and s._ld is not None:
            s.ld.append("".join(s._ld)); s._ld = None; return
        s.t.append(("E", tag))
    def handle_data(s, d):
        if s._ld is not None:
            s._ld.append(d); return
        x = " ".join(d.split())
        if x:
            if s.t and s.t[-1][0] == "T":
                s.t[-1] = ("T", s.t[-1][1] + " " + x)
            else:
                s.t.append(("T", x))
    def handle_comment(s, d): s.t.append(("C", " ".join(d.split())))
    def handle_decl(s, d): s.t.append(("D", d.strip().lower()))
    def unknown_decl(s, d): s.t.append(("D", d.strip().lower()))
    def handle_pi(s, d): s.t.append(("D", d.strip().lower()))
def parse(h):
    p = P(); p.feed(h); p.close()
    p.t = [(t[0], t[1].lower(), t[2]) if t[0] == "S" else t for t in p.t]
    if p._ld is not None:
        p.t.append(("X", "unclosed script"))
    return p.t, p.ld, p.ldattrs
O, _, lda_o = parse(old)
N, ldn, lda_n = parse(new)
# JSON-LD script tags are compared separately; their attributes may not change (a new block: type only)
_ok_ld = set(lda_o) | {(("type", "application/ld+json"),)}
if any(a not in _ok_ld for a in lda_n):
    fails.append("a JSON-LD <script> tag has attributes the original page does not have")
def _nodup(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("duplicate JSON key")
    return dict(pairs)
try:
    got = [json.loads(b, object_pairs_hook=_nodup) for b in ldn]
except (ValueError, RecursionError):
    got = None
if got != after:
    fails.append("the JSON-LD blocks are not exactly jsonld.after.json (same blocks and nodes, same order)")
_prot, _open = set(), []
for i, tok in enumerate(O):          # text in <head>, <title> and <h1> is protected: declared changes never apply there
    if tok[0] == "S" and tok[1] in ("head", "title", "h1"): _open.append(tok[1])
    elif tok[0] == "E" and tok[1] in _open: _open.remove(tok[1])
    elif tok[0] == "T" and _open: _prot.add(i)
for c in spec["live_changes"]:
    for i, tok in enumerate(O):
        if tok[0] == "T" and i not in _prot and c["before"] and c["before"] in tok[1]:
            O[i] = ("T", " ".join(tok[1].replace(c["before"], c["after"], 1).split())); break
    else:
        fails.append("declared change not found on the original page: %r" % c["before"][:80])
def norm(s): return " ".join(s.lower().split())
HEAD = re.compile(r"^h([1-6])$")
p = None
if spec.get("insert_after"):
    want = norm(spec["insert_after"])
    for i, tok in enumerate(O):
        if tok[0] == "S" and HEAD.match(tok[1]):
            j, txt = i + 1, []
            while j < len(O) and O[j] != ("E", tok[1]):
                if O[j][0] == "T": txt.append(O[j][1])
                j += 1
            if norm(" ".join(txt)) == want:
                level, depth, k = int(tok[1][1]), 0, j + 1
                while k < len(O):
                    t = O[k]
                    if t[0] == "S" and HEAD.match(t[1]) and int(t[1][1]) <= level and depth == 0:
                        break
                    if t[0] == "S" and t[1] not in VOID: depth += 1
                    elif t[0] == "E":
                        depth -= 1
                        if depth < 0: break
                    k += 1
                p = k; break
    if p is None:
        fails.append("the anchor heading %r is not on the original page" % spec["insert_after"])
else:
    p = next((i for i, t in enumerate(O) if t == ("E", "main")), None)
    p = p if p is not None else next((i for i, t in enumerate(O) if t == ("E", "body")), len(O))
def _hiding(tag, attrs):
    for a, v in attrs:
        if a in ("hidden", "aria-hidden", "inert"): return a
        if a == "style" and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?![.\d]*[1-9])|"
                                      r"(height|width|max-height)\s*:\s*0(?![.\d]*[1-9])|clip|text-indent\s*:\s*-", v or "", re.I):
            return "style"
        if a == "class" and any(HIDE_TOKEN.search(c) for c in (v or "").split()): return "class"
    if tag in HIDE_ANCESTOR and not (tag == "details" and any(a == "open" for a, _ in attrs)): return tag
    return None
if p is not None:                                # round 2 review: the insert point must not be inside a hidden region
    _stack = []
    for t in O[:p]:
        if t[0] == "S" and t[1] not in VOID: _stack.append(t)
        elif t[0] == "E":
            for j in range(len(_stack) - 1, -1, -1):
                if _stack[j][1] == t[1]:
                    del _stack[j:]; break
    for t in _stack:
        why = _hiding(t[1], t[2])
        if why:
            fails.append("the insert point is inside a hidden or non-rendered <%s> (%s) of the original page" % (t[1], why))
            break
_page_classes = {c for t in O if t[0] == "S" for a, v in t[2] if a == "class" for c in (v or "").split()}
if p is not None:
    k = len(N) - len(O)
    if k <= 0 or N[:p] != O[:p] or N[p + k:] != O[p:]:
        fails.append("outside ONE block inserted right after the anchor section, the page does not have the original "
                     "structure (moved, removed, re-ordered, re-tagged or changed content)")
    else:
        ins, stack, heads = N[p:p + k], [], []
        for t in ins:
            if t[0] in "CDX":
                fails.append("the inserted block contains a comment, declaration or unclosed element"); break
            if t[0] == "S":
                if t[1] in FORBID or t[1] not in ALLOW:
                    fails.append("the inserted block contains a <%s> element" % t[1])
                for a, v in t[2]:
                    if a.startswith("on") or a in ("hidden", "aria-hidden", "style", "srcdoc", "inert") or a not in ALLOW_ATTR:
                        fails.append("the inserted block uses the %r attribute" % a)
                    if a == "class":
                        for c in (v or "").split():
                            if HIDE_CLASS.search(c) or HIDE_TOKEN.search(c):
                                fails.append("the inserted block uses a hiding class (%r)" % c)
                            elif c not in _page_classes:
                                fails.append("the inserted block uses the class %r, which the original page does not use" % c)
                    if a == "href" and v not in spec.get("allowed_hrefs", []):
                        fails.append("the inserted block links to %r, which page.md does not" % v)
                if HEAD.match(t[1]): heads.append(int(t[1][1]))
                if t[1] not in VOID: stack.append(t[1])
            elif t[0] == "E":
                if not stack or stack[-1] != t[1]:
                    fails.append("the inserted block is not well-formed (</%s> does not close the open element)" % t[1]); break
                stack.pop()
        if stack: fails.append("the inserted block leaves <%s> unclosed" % stack[-1])
        if heads != spec.get("insert_headings", heads):
            fails.append("the inserted headings are h%s, expected h%s" % (heads, spec.get("insert_headings")))
        words = re.findall(r"\w+", " ".join(t[1] for t in ins if t[0] == "T").casefold())
        if words != spec.get("insert_words"):
            fails.append("the visible text of the inserted block is not exactly the body of page.md")
print("\n".join("FAIL " + f for f in dict.fromkeys(fails)) if fails else "OK")
sys.exit(1 if fails else 0)
'''


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)", re.M | re.S)


def _no_code(body):
    """Markdown without fenced code blocks (a '# x' line inside a fence is not a heading)."""
    return _FENCE_RE.sub("", body)


def bundle_files(draft, live, today):
    """-> (files {name: text}, ops, after_blocks) for an extend bundle."""
    after, ops = merge_jsonld(live["jsonld"], draft.get("faq"), today)
    from . import checks as checks_mod
    body = draft.get("body_md") or ""
    words = re.findall(r"\w+", " ".join(html_mod.unescape(checks_mod.strip_markdown(body)).split()).casefold())
    for c in draft.get("live_changes") or []:
        if isinstance(c, dict) and not str(c.get("before") or "").strip():
            raise ToolError("a declared live change has an empty 'before' text")
    hrefs = sorted({l["target"] for l in checks_mod.md_links(body)})
    spec = {"page": live["url"], "original_sha256": live["sha256"], "insert_after": draft.get("insert_after"),
            "insert_words": words, "allowed_hrefs": hrefs,
            "insert_headings": [len(h) for h in re.findall(r"^ {0,3}(#{1,6})[ \t]", _no_code(body), re.M)],
            "live_changes": [{"before": c.get("before", ""), "after": c.get("after", "")}
                             for c in draft.get("live_changes") or [] if isinstance(c, dict)],
            "head_unchanged": live["head"], "jsonld_ops": ops}
    files = {"extend.json": json.dumps(spec, indent=1, ensure_ascii=False) + "\n",
             "jsonld.before.json": json.dumps(live["jsonld"], indent=1, ensure_ascii=False) + "\n",
             "jsonld.after.json": json.dumps(after, indent=1, ensure_ascii=False) + "\n",
             "verify_extend.py": VERIFIER}
    return files, ops, after


def implement_section(draft, live, ops, one_line):
    """IMPLEMENT.md text for an extension (replaces the head/JSON-LD/where sections of a new-page bundle)."""
    h = live["head"]
    lines = ["## What an extend is",
             "",
             "This page is ALREADY LIVE at %s. An extend ADDS new sections to it. It does not change the page's title," % live["url"],
             "meta description, H1, canonical link, og/twitter tags, URL, template or any existing text, except the",
             "declared live changes listed below. Its structured data is MERGED, never replaced.",
             "",
             "Start from the live page source whose sha256 is `%s` (verify_extend.py checks this). If your copy" % live["sha256"],
             "differs, stop and report: the page changed after this bundle was reviewed.",
             "",
             "## 1. Insert the new sections",
             ""]
    ia = one_line(draft.get("insert_after"), 200)
    if ia:
        lines.append("Insert the body Markdown of `page.md` (the new sections), rendered in the page's own markup and styles,")
        lines.append("as ONE contiguous block directly after the section whose heading is \"%s\"." % ia)
    else:
        lines.append("Insert the body Markdown of `page.md` (the new sections), rendered in the page's own markup and styles,")
        lines.append("as ONE contiguous block at the end of the main content, before any closing call-to-action or footer.")
    lines += ["", "## 2. Declared live changes", ""]
    changes = [c for c in draft.get("live_changes") or [] if isinstance(c, dict)]
    if not changes:
        lines.append("None. Every existing sentence stays exactly as it is.")
    for i, c in enumerate(changes, 1):
        lines += ['%d. before: "%s"' % (i, one_line(c.get("before"), 400)), '   after:  "%s"' % one_line(c.get("after"), 400), ""]
    lines += ["", "## 3. Do NOT change the head", "",
              "Keep these exactly as they are on the live page:",
              "- `<title>` = %s" % json.dumps(h.get("title")),
              "- meta description = %s" % json.dumps(h.get("description")),
              "- canonical = %s" % json.dumps(h.get("canonical")),
              "- H1 = %s" % json.dumps(h.get("h1")),
              "- og/twitter tags: unchanged (%d tags)" % len(h.get("og") or {}),
              "", "## 4. Merge the structured data (never replace it)", ""]
    if ops:
        lines.append("The page has %d JSON-LD block(s) (`jsonld.before.json`). Apply exactly these changes:" % len(live["jsonld"]))
        lines += ["- " + one_line(o, 300) for o in ops]
    else:
        lines.append("No structured-data changes. Leave every JSON-LD block exactly as it is.")
    lines += ["", "The result must equal `jsonld.after.json`: the same blocks in the same order, with only the changes above.",
              "Do not delete, merge, split or reorder blocks or nodes.", ""]
    return "\n".join(lines)


def selftest() -> int:
    n = 0
    _LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)   # test helper only

    def ck(cond, msg):
        nonlocal n
        assert cond, "extend.selftest: " + msg
        n += 1

    import os
    import subprocess
    import sys
    import tempfile
    page = ("<html><head><title>Rent or Sell?</title><meta name=\"description\" content=\"Old desc\">"
            "<link rel=\"canonical\" href=\"https://ex.com/a/\"><meta property=\"og:title\" content=\"Rent or Sell?\">\n"
            "<script type=\"application/ld+json\">{\"@context\":\"https://schema.org\",\"@graph\":[{\"@type\":\"BreadcrumbList\","
            "\"itemListElement\":[]},{\"@type\":\"FAQPage\",\"@id\":\"#faq\",\"mainEntity\":[{\"@type\":\"Question\",\"name\":\"Old q\","
            "\"acceptedAnswer\":{\"@type\":\"Answer\",\"text\":\"Old a\"}}]},{\"@type\":\"Article\",\"dateModified\":\"2026-01-01\"}]}"
            "</script>\n</head><body>\n<h1>Rent or Sell?</h1>\n<h2>The fee, plainly</h2>\n<p>We charge $149.</p>\n"
            "<h2>Next step</h2>\n<p>Call us.</p>\n</body></html>\n")
    live = live_page(page, "https://ex.com/a/")
    ck(live["head"]["title"] == "Rent or Sell?" and live["head"]["h1"] == ["Rent or Sell?"], "head facts read")
    ck(live["head"]["canonical"] == "https://ex.com/a/" and live["head"]["og"]["og:title"] == "Rent or Sell?", "canonical/og")
    draft = {"faq": [{"q": "New q", "a": "New a"}, {"q": "Old q", "a": "dup"}], "insert_after": "The fee, plainly",
             "body_md": "## New\n\nNew text.",
             "live_changes": [{"before": "Call us.", "after": "Call us today."}]}
    after, ops = merge_jsonld(live["jsonld"], draft["faq"], "2026-11-11")
    g = after[0]["@graph"]
    ck([n2["@type"] for n2 in g] == ["BreadcrumbList", "FAQPage", "Article"], "no node removed or reordered")
    ck([q["name"] for q in g[1]["mainEntity"]] == ["Old q", "New q"], "questions appended, duplicates skipped")
    ck(g[2]["dateModified"] == "2026-11-11" and len(ops) == 2, "dateModified bumped, ops listed")
    ck(live["jsonld"][0]["@graph"][2]["dateModified"] == "2026-01-01", "live blocks not mutated")
    a2, o2 = merge_jsonld([{"@context": "https://schema.org", "@type": "Article"}], draft["faq"], "2026-11-11")
    ck(len(a2) == 2 and a2[1]["@type"] == "FAQPage" and a2[0] == {"@context": "https://schema.org", "@type": "Article"},
       "FAQPage added as a new block when there is no @graph")
    try:
        merge_jsonld([None], [], "2026-11-11")
        ck(False, "unparseable live JSON-LD must refuse")
    except ToolError:
        ck(True, "unparseable live JSON-LD refuses the merge")
    files, ops, after = bundle_files(draft, live, "2026-11-11")
    tmp = tempfile.mkdtemp(prefix="adrails-ext-")
    for name, text in files.items():
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(text)
    orig = os.path.join(tmp, "orig.html")
    with open(orig, "w", encoding="utf-8") as f:
        f.write(page)

    def verify(new_html):
        p = os.path.join(tmp, "new.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_html)
        r = subprocess.run([sys.executable, os.path.join(tmp, "verify_extend.py"), orig, p], capture_output=True, text=True)
        return r.returncode, r.stdout
    ld_new = "<script type=\"application/ld+json\">%s</script>" % json.dumps(after[0])
    good = re.sub(_LD_RE, lambda m: ld_new, page).replace("<p>We charge $149.</p>\n",
                                                            "<p>We charge $149.</p>\n<h2>New</h2>\n<p>New text.</p>\n")
    good = good.replace("<p>Call us.</p>", "<p>Call us today.</p>")
    rc, out = verify(good)
    ck(rc == 0 and out.strip() == "OK", "a correct implementation verifies: %s" % out)
    rc, out = verify(good.replace("<title>Rent or Sell?</title>", "<title>New title</title>"))
    ck(rc == 1, "retitling fails")
    rc, out = verify(re.sub(_LD_RE, lambda m: "<script type=\"application/ld+json\">{\"@type\":\"Article\"}</script>", good))
    ck(rc == 1 and "JSON-LD" in out, "replacing the @graph fails (QA B2)")
    rc, out = verify(good.replace("<h1>Rent or Sell?</h1>", "<h1>Asking Rent</h1>"))
    ck(rc == 1, "rewriting the H1 fails")
    rc, out = verify(good.replace("<h2>Next step</h2>\n", ""))
    ck(rc == 1, "deleting live content fails, even next to a declared change")
    rc, out = verify(good.replace("<p>Call us today.</p>", "<p>Call now.</p>"))
    ck(rc == 1, "an undeclared change fails")
    rc, out = verify(good.replace("<h1>", "<p>x</p>\n<h1>", 1))
    ck(rc == 1, "a second insertion before the anchor fails")
    rc, out = verify(good.replace("<p>New text.</p>", "<p>New text.</p><script src=//evil></script>"))
    ck(rc == 1 and "script" in out, "a script in the inserted block fails (review fr#2)")
    rc, out = verify(good.replace("<p>New text.</p>", "<p>New text and more.</p>"))
    ck(rc == 1 and "visible text" in out, "inserted text must equal page.md")
    rc, out = verify(good.replace("<p>Call us today.</p>", "<p onclick=\"x()\">Call us today.</p>"))
    ck(rc == 1, "markup changed inside a declared live change fails (review fr#3)")
    dup = good.replace('"@context": "https://schema.org"', '"@type": "Evil", "@context": "https://schema.org"', 1)
    rc, out = verify(dup)
    ck(rc == 1, "duplicate JSON keys / altered JSON-LD fail (review fr#19)")
    for bad_faq in ([{"q": 1, "a": "x"}], [{"q": "q</script>", "a": "x"}]):
        try:
            merge_jsonld(live["jsonld"], bad_faq, "2026-11-11")
            ck(False, "bad FAQ must refuse")
        except ToolError:
            ck(True, "non-text or markup FAQ refused (review fr#12/#20)")
    try:
        merge_jsonld([{"@type": "FAQPage", "mainEntity": "legacy"}], [{"q": "a", "a": "b"}], "2026-11-11")
        ck(False, "odd mainEntity must refuse")
    except ToolError:
        ck(True, "odd live mainEntity refuses the merge (review fr#11)")
    base_ins = "<h2>New</h2>\n<p>New text.</p>\n"
    damage = {
        "block moved to the end of the file": good.replace(base_ins, "").replace("</html>", base_ins + "</html>"),
        "block moved before </main>-like end of body": good.replace(base_ins, "").replace("</body>", base_ins + "</body>"),
        "unclosed comment": good.replace("<p>New text.</p>", "<p>New text.</p><!--"),
        "unclosed hidden div": good.replace("<h2>New</h2>", "<div hidden><h2>New</h2>"),
        "display:none wrapper": good.replace(base_ins, "<div style=\"display:none\">" + base_ins + "</div>\n"),
        "aria-hidden wrapper": good.replace(base_ins, "<div aria-hidden=\"true\">" + base_ins + "</div>\n"),
        "white-on-white": good.replace("<p>New text.</p>", "<p style=\"color:#fff\">New text.</p>"),
        "h2 -> h6": good.replace("<h2>New</h2>", "<h6>New</h6>"),
        "unclosed <p>": good.replace("<p>New text.</p>", "<p>New text.</p><p>"),
        "hiding class": good.replace("<p>New text.</p>", "<p class=\"sr-only\">New text.</p>"),
        # round 2 adversarial reviews
        "ld+json tag gains attributes": good.replace("<script type=\"application/ld+json\">",
                                                     "<script type=\"application/ld+json\" onload=\"x()\" src=\"https://e/x\">"),
        "text inside <title> in the insert": good.replace("<p>New text.</p>", "<title>New text.</title>"),
        "text inside <canvas> in the insert": good.replace("<p>New text.</p>", "<canvas>New text.</canvas>"),
        "non-void self-closing <div/>": good.replace("<p>New text.</p>", "<div id=\"x\"/><p>New text.</p>"),
        "class the page does not use": good.replace("<p>New text.</p>", "<p class=\"screen-reader-text\">New text.</p>"),
        "remote image beacon": good.replace("<p>New text.</p>", "<p>New text.</p><img src=\"https://e.example/p.gif\">"),
        "different non-Latin words": good.replace("<p>New text.</p>", "<p>New text. \u0442\u0435\u043a\u0441\u0442</p>"),
    }
    for label, bad in damage.items():
        rc, out = verify(bad)
        ck(rc == 1, "QA N3 damage refused: %s (%s)" % (label, out.strip()[:80]))
    # round 2 review: a declared change is never applied to <title>/<h1> text (even when the same text is there)
    tmp_h = tempfile.mkdtemp(prefix="adrails-ext-h1-")
    page_h = page.replace("<h1>Rent or Sell?</h1>", "<h1>Call us.</h1>")
    files_h, _, after_h = bundle_files(draft, live_page(page_h, "https://ex.com/a/"), "2026-11-11")
    for name, text in files_h.items():
        with open(os.path.join(tmp_h, name), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(tmp_h, "orig.html"), "w", encoding="utf-8") as f:
        f.write(page_h)
    good_h = re.sub(_LD_RE, lambda m: "<script type=\"application/ld+json\">%s</script>" % json.dumps(after_h[0]), page_h)
    good_h = good_h.replace("<p>We charge $149.</p>\n", "<p>We charge $149.</p>\n<h2>New</h2>\n<p>New text.</p>\n")

    def verify_h(new_html):
        with open(os.path.join(tmp_h, "new.html"), "w", encoding="utf-8") as f:
            f.write(new_html)
        r = subprocess.run([sys.executable, os.path.join(tmp_h, "verify_extend.py"), os.path.join(tmp_h, "orig.html"),
                            os.path.join(tmp_h, "new.html")], capture_output=True, text=True)
        return r.returncode
    ck(verify_h(good_h.replace("<p>Call us.</p>", "<p>Call us today.</p>")) == 0, "declared body change verifies")
    ck(verify_h(good_h.replace("<h1>Call us.</h1>", "<h1>Call us today.</h1>")) == 1,
       "a declared change applied to the H1 instead of the body fails")
    ck(bundle_files(dict(draft, body_md="## New\n\n```\n# not a heading\n```\n\nNew text."), live,
                    "2026-11-11")[0]["extend.json"].count('"insert_headings"') == 1 and
       json.loads(bundle_files(dict(draft, body_md="## New\n\n```\n# not a heading\n```\n\nNew text."), live,
                               "2026-11-11")[0]["extend.json"])["insert_headings"] == [2],
       "a '#' line inside a code fence is not an expected heading")
    # round 2 review (GLM): an anchor inside a closed <details>/hidden region of the ORIGINAL is refused
    for wrap_open, wrap_close in (("<details><summary>More</summary>", "</details>"), ("<div hidden>", "</div>")):
        tmp_d = tempfile.mkdtemp(prefix="adrails-ext-d-")
        page_d = page.replace("<h2>The fee, plainly</h2>\n<p>We charge $149.</p>\n",
                              wrap_open + "<h2>The fee, plainly</h2>\n<p>We charge $149.</p>\n" + wrap_close + "\n")
        files_d, _, after_d = bundle_files(dict(draft, live_changes=[]), live_page(page_d, "https://ex.com/a/"), "2026-11-11")
        for name, text in files_d.items():
            with open(os.path.join(tmp_d, name), "w", encoding="utf-8") as f:
                f.write(text)
        with open(os.path.join(tmp_d, "orig.html"), "w", encoding="utf-8") as f:
            f.write(page_d)
        impl_d = re.sub(_LD_RE, lambda m: "<script type=\"application/ld+json\">%s</script>" % json.dumps(after_d[0]), page_d)
        impl_d = impl_d.replace("<p>We charge $149.</p>\n", "<p>We charge $149.</p>\n<h2>New</h2>\n<p>New text.</p>\n")
        with open(os.path.join(tmp_d, "new.html"), "w", encoding="utf-8") as f:
            f.write(impl_d)
        r = subprocess.run([sys.executable, os.path.join(tmp_d, "verify_extend.py"), os.path.join(tmp_d, "orig.html"),
                            os.path.join(tmp_d, "new.html")], capture_output=True, text=True)
        ck(r.returncode == 1 and "insert point" in r.stdout, "insert point inside %s is refused" % wrap_open)
    dup_live = live_page(page.replace('{"@context"', '{"@context":"x","@context"', 1), "https://ex.com/a/")
    ck(dup_live["jsonld_unparseable"] == 1, "duplicate JSON keys in the live JSON-LD make it unparseable (merge refuses)")
    try:
        bundle_files(dict(draft, live_changes=[{"before": " ", "after": "x"}]), live, "2026-11-11")
        ck(False, "an empty live-change 'before' must be refused")
    except ToolError:
        ck(True, "an empty live-change 'before' is refused at bundle time")
    # QA N4: a merge that ADDS a new JSON-LD block verifies when implemented correctly
    page2 = page.replace(re.search(_LD_RE, page).group(0), "")
    live2 = live_page(page2, "https://ex.com/a/")
    files2, ops2, after2 = bundle_files(dict(draft, live_changes=[]), live2, "2026-11-11")
    ck(any(o.startswith("add block") for o in ops2), "add-block op listed")
    for name, text in files2.items():
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(text)
    with open(orig, "w", encoding="utf-8") as f:
        f.write(page2)
    good2 = page2.replace("<p>We charge $149.</p>\n", "<p>We charge $149.</p>\n" + base_ins).replace(
        "</head>", "<script type=\"application/ld+json\">%s</script></head>" % json.dumps(after2[0]))
    rc, out = verify(good2)
    ck(rc == 0, "a correct implementation that adds a new JSON-LD block verifies (QA N4): %s" % out)
    # QA N11
    try:
        merge_jsonld([{"@graph": [{"@type": "FAQPage"}, {"@type": ["FAQPage"]}]}], [{"q": "a", "a": "b"}], "2026-11-11")
        ck(False, "two FAQPage nodes must refuse")
    except ToolError:
        ck(True, "two FAQPage nodes refuse the merge")
    a3, _ = merge_jsonld([{"@type": [{"x": 1}], "dateModified": {"@value": "2020"}}], [], "2026-11-11")
    ck(a3[0]["dateModified"] == {"@value": "2020"}, "unhashable @type tolerated, typed dateModified left alone")
    a4, _ = merge_jsonld([{"@graph": {"@type": "FAQPage", "mainEntity": []}}], [{"q": "Q?", "a": "A"}, {"q": "q?", "a": "B"}],
                         "2026-11-11")
    ck(len(a4[0]["@graph"]["mainEntity"]) == 1, "dict @graph handled, duplicate draft questions merged once")
    big = "<html><head><title>t</title></head><body>" + "<p a='>'>x</p>" * 40000 + "</body></html>"
    import time as _t
    t0 = _t.time()
    hf = head_facts(big)
    ck(_t.time() - t0 < 5 and hf["title"] == "t", "linear parse of a 600 KB page")
    try:
        live_page("x" * (MAX_HTML + 1), "u")
        ck(False, "oversize page must refuse")
    except ToolError:
        ck(True, "oversize page refused with a clear message")
    sec = implement_section(draft, live, ops, lambda v, n: " ".join(str(v or "").split())[:n])
    ck("ALREADY LIVE" in sec and "never replaced" in sec and "Rent or Sell?" in sec, "IMPLEMENT extend section")
    return n
