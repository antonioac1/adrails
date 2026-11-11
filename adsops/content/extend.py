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

from ..errors import ToolError

_LD_RE = re.compile(r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_RE = re.compile(r"<meta\s[^>]*>", re.I)
_LINK_RE = re.compile(r"<link\s[^>]*>", re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_ATTR_RE = re.compile(r"([a-zA-Z:-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
_TAG_RE = re.compile(r"<[^>]+>")
MAX_HTML = 3_000_000


def _attrs(tag):
    return {k.lower(): v[1:-1] for k, v in _ATTR_RE.findall(tag)}


def _text(fragment):
    return " ".join(html_mod.unescape(_TAG_RE.sub(" ", fragment or "")).split())


def head_facts(html):
    """The head values an extension must never change: title, meta description, canonical, og:* and the H1 texts."""
    out = {"title": None, "description": None, "canonical": None, "og": {}, "h1": []}
    m = _TITLE_RE.search(html)
    if m:
        out["title"] = _text(m.group(1))
    for tag in _META_RE.findall(html):
        a = _attrs(tag)
        if a.get("name", "").lower() == "description":
            out["description"] = a.get("content")
        prop = a.get("property", "").lower()
        if prop.startswith("og:") or prop.startswith("twitter:") or a.get("name", "").lower().startswith("twitter:"):
            out["og"][prop or a.get("name", "").lower()] = a.get("content")
    for tag in _LINK_RE.findall(html):
        a = _attrs(tag)
        if "canonical" in a.get("rel", "").lower().split():
            out["canonical"] = a.get("href")
    out["h1"] = [_text(x) for x in _H1_RE.findall(html)]
    return out


def jsonld_blocks(html):
    """-> list of (parsed_or_None, raw_text) for every ld+json block, in page order."""
    out = []
    for raw in _LD_RE.findall(html):
        try:
            out.append((json.loads(raw), raw))
        except ValueError:
            out.append((None, raw))
    return out


def live_page(html, url):
    """Everything the bundle needs about the live page. Refuses pages it cannot model safely."""
    if not isinstance(html, str) or not html.strip():
        raise ToolError("the live page to extend could not be read")
    if len(html) > MAX_HTML:
        raise ToolError("the live page is too large to extend safely")
    blocks = jsonld_blocks(html)
    return {"url": url, "sha256": hashlib.sha256(html.encode("utf-8", "surrogatepass")).hexdigest(),
            "head": head_facts(html), "jsonld": [b for b, _ in blocks],
            "jsonld_unparseable": sum(1 for b, _ in blocks if b is None)}


def _nodes(block):
    """Top-level nodes of one JSON-LD block (the block itself, its @graph, or its list items)."""
    if isinstance(block, list):
        return [n for n in block if isinstance(n, dict)]
    if isinstance(block, dict):
        g = block.get("@graph")
        return [n for n in g if isinstance(n, dict)] if isinstance(g, list) else [block]
    return []


def _types(node):
    t = node.get("@type")
    return set(t) if isinstance(t, list) else {t}


def merge_jsonld(blocks, faq, today):
    """-> (after_blocks, ops). Only ADDS: Question nodes (to the existing FAQPage, or one new FAQPage node) and sets
    existing dateModified values to today. Never removes or rewrites anything else."""
    if any(b is None for b in blocks):
        raise ToolError("the live page has a JSON-LD block that does not parse; an extension cannot merge into it safely")
    after = copy.deepcopy(blocks)
    ops = []
    questions = [{"@type": "Question", "name": q["q"], "acceptedAnswer": {"@type": "Answer", "text": q["a"]}}
                 for q in faq or [] if isinstance(q, dict) and q.get("q") and q.get("a")]
    if questions:
        target = None
        for bi, b in enumerate(after):
            for n in _nodes(b):
                if "FAQPage" in _types(n):
                    target = (bi, n)
                    break
            if target:
                break
        if target:
            ents = target[1].get("mainEntity")
            if isinstance(ents, dict):
                ents = [ents]
            if not isinstance(ents, list):
                ents = []
            have = {str(e.get("name", "")).strip().lower() for e in ents if isinstance(e, dict)}
            add = [q for q in questions if q["name"].strip().lower() not in have]
            target[1]["mainEntity"] = ents + add
            ops.append("append %d Question node(s) to the existing FAQPage node%s in JSON-LD block %d" % (
                len(add), (" (@id %s)" % target[1]["@id"]) if target[1].get("@id") else "", target[0] + 1))
        else:
            node = {"@type": "FAQPage", "mainEntity": questions}
            graph_block = next((b for b in after if isinstance(b, dict) and isinstance(b.get("@graph"), list)), None)
            if graph_block is not None:
                graph_block["@graph"].append(node)
                ops.append("add one FAQPage node at the end of the @graph of JSON-LD block %d" % (after.index(graph_block) + 1))
            else:
                after.append(dict({"@context": "https://schema.org"}, **node))
                ops.append("add one new JSON-LD block (a FAQPage) after the existing blocks")
    for bi, b in enumerate(after):
        for n in _nodes(b):
            if "dateModified" in n and n["dateModified"] != today:
                n["dateModified"] = today
                ops.append("set dateModified to %s on the %s node in JSON-LD block %d" % (
                    today, "/".join(sorted(str(t) for t in _types(n))), bi + 1))
    return after, ops


VERIFIER = r'''#!/usr/bin/env python3
"""verify_extend.py ORIGINAL.html NEW.html  (shipped in an AdRails extend bundle; stdlib only)
Checks that NEW is ORIGINAL plus ONE inserted region, the declared live changes, and the JSON-LD in jsonld.after.json,
and that the title, meta description, canonical, og/twitter tags and H1 are unchanged. Prints OK or FAIL lines."""
import difflib, hashlib, html, json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "extend.json"), encoding="utf-8"))
after = json.load(open(os.path.join(here, "jsonld.after.json"), encoding="utf-8"))
old, new = (open(p, encoding="utf-8").read() for p in sys.argv[1:3])
fails = []
if hashlib.sha256(old.encode("utf-8", "surrogatepass")).hexdigest() != spec["original_sha256"]:
    fails.append("ORIGINAL.html is not the page this bundle was reviewed against (sha256 differs): stop and report")
LD = re.compile(r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
def head(h):
    t = re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S)
    metas = sorted(m for m in re.findall(r"<meta\s[^>]*>", h, re.I) if re.search(r"(name|property)\s*=\s*[\"'](description|og:|twitter:)", m, re.I))
    links = sorted(l for l in re.findall(r"<link\s[^>]*>", h, re.I) if re.search(r"canonical", l, re.I))
    h1 = re.findall(r"<h1[^>]*>(.*?)</h1>", h, re.I | re.S)
    return [t.group(1).strip() if t else None, metas, links, [" ".join(x.split()) for x in h1]]
if head(old) != head(new):
    fails.append("title, meta description, canonical, og/twitter tags or H1 changed: an extension must not change them")
try:
    got = [json.loads(b) for b in LD.findall(new)]
except ValueError:
    got = None
if got != after:
    fails.append("the JSON-LD blocks are not exactly jsonld.after.json (same blocks, same order)")
def lines(h):
    return [l.rstrip() for l in LD.sub("<ld-json/>", h).splitlines()]
def norm(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split()).lower()
a, b = lines(old), lines(new)
inserts = 0
for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
    if op == "equal":
        continue
    if op == "insert":
        inserts += 1
        if spec.get("insert_after"):
            anchor = next((k for k, l in enumerate(a) if norm(spec["insert_after"]) in norm(l)), None)
            if anchor is None or i1 <= anchor:
                fails.append("new content is not inserted after the heading %r" % spec["insert_after"])
        continue
    oldt, newt = norm(" ".join(a[i1:i2])), norm(" ".join(b[j1:j2]))
    ok = False
    if op == "replace":
        for c in spec["live_changes"]:
            bf, af = norm(c["before"]), norm(c["after"])
            if bf and bf in oldt and af in newt:
                oldt, newt = oldt.replace(bf, " ", 1), newt.replace(af, " ", 1)
                ok = True
        ok = ok and " ".join(oldt.split()) == " ".join(newt.split())   # nothing else changed inside the hunk
    if not ok:
        fails.append("lines %d-%d of the original were %s outside the declared changes" % (i1 + 1, i2, "removed" if op == "delete" else "changed"))
if inserts > 1:
    fails.append("the new sections must be ONE contiguous insertion (found %d)" % inserts)
print("\n".join("FAIL " + f for f in fails) if fails else "OK")
sys.exit(1 if fails else 0)
'''


def bundle_files(draft, live, today):
    """-> (files {name: text}, ops, after_blocks) for an extend bundle."""
    after, ops = merge_jsonld(live["jsonld"], draft.get("faq"), today)
    spec = {"page": live["url"], "original_sha256": live["sha256"], "insert_after": draft.get("insert_after"),
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
        lines += ["- " + o for o in ops]
    else:
        lines.append("No structured-data changes. Leave every JSON-LD block exactly as it is.")
    lines += ["", "The result must equal `jsonld.after.json`: the same blocks in the same order, with only the changes above.",
              "Do not delete, merge, split or reorder blocks or nodes.", ""]
    return "\n".join(lines)


def selftest() -> int:
    n = 0

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
    ck(rc == 1 and "title" in out, "retitling fails")
    rc, out = verify(re.sub(_LD_RE, lambda m: "<script type=\"application/ld+json\">{\"@type\":\"Article\"}</script>", good))
    ck(rc == 1 and "JSON-LD" in out, "replacing the @graph fails (QA B2)")
    rc, out = verify(good.replace("<h1>Rent or Sell?</h1>", "<h1>Asking Rent</h1>"))
    ck(rc == 1, "rewriting the H1 fails")
    rc, out = verify(good.replace("<h2>Next step</h2>\n", ""))
    ck(rc == 1 and ("removed" in out or "changed" in out), "deleting live content fails, even next to a declared change")
    rc, out = verify(good.replace("<p>Call us today.</p>", "<p>Call now.</p>"))
    ck(rc == 1, "an undeclared change fails")
    rc, out = verify(good.replace("<h1>", "<p>x</p>\n<h1>", 1))
    ck(rc == 1, "a second insertion before the anchor fails")
    sec = implement_section(draft, live, ops, lambda v, n: " ".join(str(v or "").split())[:n])
    ck("ALREADY LIVE" in sec and "never replaced" in sec and "Rent or Sell?" in sec, "IMPLEMENT extend section")
    return n
