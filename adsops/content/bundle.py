"""Approved Markdown bundle for one drafted page (SPEC-D, AdRails 0.12.0).

Turns an approved draft + its review + its deterministic check result into a self-contained directory that an AI
coding agent can implement without judgement calls:

  page.md       YAML front matter (every value JSON-encoded) + body Markdown + FAQ section
  IMPLEMENT.md  plain imperative instructions, live changes, a "Do not" list and the acceptance checks
  review.json   the review object, checks.json the check result, sources.json a citation summary
  page.html     optional minimal standalone render (only when html=True)
  MANIFEST.json sha256 of every other file, written last

Everything handed to this module comes from websites and model outputs, so it is treated as UNTRUSTED data: no
eval/exec, no regexes built from it, every string capped, every HTML render escaped. No network, no subprocess.
Writes happen in a temp directory that is renamed into place, so a bundle is either complete or absent.
"""
import hashlib
import html
import json
import os
import re
import shutil
import tempfile

from ..errors import ToolError, scrub

MAX_BODY = 60000
MAX_STR = 200000
MAX_LIST = 500
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLUG_RX = re.compile(r"^/[a-z0-9/-]{1,120}/$")
ORIGIN_RX = re.compile(r"^(https?)://([A-Za-z0-9.\-]+(?::\d{1,5})?)/?$")
TAIL_RX = re.compile(r"[^a-z0-9]+")
INLINE_LINK_RX = re.compile(r"\[([^\[\]]{0,300})\]\(([^()\s]{0,2000})\)")
BOLD_RX = re.compile(r"\*\*(.+?)\*\*")
HEAD_RX = re.compile(r"^(#{1,3})[ \t]+(.*)$")
UL_RX = re.compile(r"^[-*][ \t]+(.*)$")
OL_RX = re.compile(r"^\d{1,3}\.[ \t]+(.*)$")


# --------------------------------------------------------------------------------------------------------------
# small helpers (all inputs capped; everything here is pure)
# --------------------------------------------------------------------------------------------------------------
def _cap(value, limit=MAX_STR):
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    return s if len(s) <= limit else s[:limit]


def _lst(value, limit=MAX_LIST):
    if not isinstance(value, (list, tuple)):
        return []
    return list(value)[:limit]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _origin(site):
    m = ORIGIN_RX.match(_cap(site, 300).strip())
    if not m:
        raise ToolError("bundle: profile 'site' is not an http(s) origin (got %r)" % scrub(site))
    return m.group(1).lower() + "://" + m.group(2).lower()


def _canonical(profile, slug):
    site = _cap(profile.get("site") if isinstance(profile, dict) else "", 300)
    return _origin(site) + slug


def _slug(draft):
    slug = _cap(draft.get("slug") if isinstance(draft, dict) else "", 200)
    if not SLUG_RX.match(slug) or ".." in slug or "//" in slug:
        raise ToolError("bundle: draft slug %r is not /lower/path/ form" % scrub(slug))
    return slug


def _body_block(draft):
    body = draft.get("body_md")
    if not isinstance(body, str) or not body.strip():
        raise ToolError("bundle: draft body_md is missing or empty")
    if len(body) > MAX_BODY:
        raise ToolError("bundle: draft body_md is longer than 60000 characters")
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return body


def _body_sha(draft, meta=None):
    if isinstance(meta, dict) and isinstance(meta.get("body_sha256"), str) and meta["body_sha256"]:
        return _cap(meta["body_sha256"], 64)
    return _sha(_body_block(draft))


def _slug_tail(slug):
    parts = [p for p in _cap(slug, 200).strip("/").split("/") if p]
    tail = TAIL_RX.sub("-", (parts[-1] if parts else "home").lower()).strip("-")
    return (tail or "home")[:48]


def _today(meta):
    today = _cap((meta or {}).get("today") if isinstance(meta, dict) else "", 20)
    if not DATE_RX.match(today):
        raise ToolError("bundle: meta.today must be YYYY-MM-DD (got %r)" % scrub(today))
    return today


def _author(profile):
    author = profile.get("author") if isinstance(profile, dict) else None
    author = author if isinstance(author, dict) else {}
    return {
        "name": _cap(author.get("name"), 120),
        "url": _cap(author.get("url"), 300),
        "id": _cap(author.get("id"), 300),
    }


def _faq(draft):
    out = []
    for item in _lst(draft.get("faq"), 8):
        if isinstance(item, dict):
            q, a = _cap(item.get("q"), 300).strip(), _cap(item.get("a"), 2000).strip()
            if q and a:
                out.append({"q": q, "a": a})
    return out


def _sources(draft):
    out = []
    for item in _lst(draft.get("sources"), 40):
        if isinstance(item, dict):
            out.append({"url": _cap(item.get("url"), 500), "quote": _cap(item.get("quote"), 600)})
    return out


def _one_line(value, limit):
    """PM: text that lands in IMPLEMENT.md (read by the customer's AI agent) is one plain line: no newlines, backticks
    or markdown that could start a new instruction block."""
    return " ".join(_cap(value, limit * 2).replace("`", "'").split())[:limit]


def _links(draft):
    out = []
    for item in _lst(draft.get("internal_links"), 50):
        if isinstance(item, dict):
            out.append({"from": _one_line(item.get("from"), 300), "anchor": _one_line(item.get("anchor"), 200)})
    return out


# --------------------------------------------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------------------------------------------
def article_jsonld(draft, profile, today):
    """Article JSON-LD for this page (FAQPage is added on top of it by front_matter/write when the draft has a FAQ)."""
    slug = _slug(draft)
    author = _author(profile)
    canonical = _canonical(profile, slug)
    node = {"@type": "Article"}
    node["headline"] = _cap(draft.get("title"), 70)
    node["description"] = _cap(draft.get("description"), 170)
    node["author"] = {"@id": author["id"] or canonical, "name": author["name"], "url": author["url"]}
    if _cap(draft.get("mode"), 20) == "new":
        node["datePublished"] = today
    node["dateModified"] = today
    node["mainEntityOfPage"] = {"@type": "WebPage", "@id": canonical}
    return {"@context": "https://schema.org", **node}


def faq_jsonld(draft):
    """FAQPage JSON-LD for the draft FAQ, or None when there is no FAQ."""
    items = _faq(draft)
    if not items:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": it["q"], "acceptedAnswer": {"@type": "Answer", "text": it["a"]}}
            for it in items
        ],
    }


def bundle_jsonld(draft, profile, today):
    """The 'jsonld' front-matter value: the Article, plus the FAQPage when the draft has a FAQ."""
    article = article_jsonld(draft, profile, today)
    faq = faq_jsonld(draft)
    return [article, faq] if faq else article


# --------------------------------------------------------------------------------------------------------------
# front matter
# --------------------------------------------------------------------------------------------------------------
def front_matter(draft, profile, today, meta=None):
    """YAML front matter between '---' lines; every value is JSON-encoded (JSON is valid YAML)."""
    if not isinstance(draft, dict):
        raise ToolError("bundle.front_matter: draft must be a dict")
    today = _today({"today": today})
    slug = _slug(draft)
    author = _author(profile)
    mode = _cap(draft.get("mode"), 20)
    rows = [("title", _cap(draft.get("title"), 70)), ("description", _cap(draft.get("description"), 170)),
            ("slug", slug), ("canonical", _canonical(profile, slug)),
            ("author", {"name": author["name"], "url": author["url"]})]
    if mode == "new":
        rows.append(("date_published", today))
    rows.append(("date_modified", today))
    rows.append(("content_type", _cap(draft.get("content_type"), 40)))
    rows.append(("mode", mode))
    if mode == "extend":
        rows.append(("insert_after", _one_line(draft.get("insert_after"), 200) or None))
        rows.append(("live_changes", [{"before": _one_line(c.get("before"), 400), "after": _one_line(c.get("after"), 400)}
                                      for c in _lst(draft.get("live_changes"), 50) if isinstance(c, dict)]))
    rows.append(("jsonld", bundle_jsonld(draft, profile, today)))
    rows.append(("sources", _sources(draft)))
    rows.append(("internal_links_to_add", [dict(l, to=slug) for l in _links(draft)]))
    lines = ["---"]
    for key, value in rows:
        lines.append("%s: %s" % (key, json.dumps(value, ensure_ascii=True, sort_keys=False)))
    lines.append("---")
    return "\n".join(lines) + "\n"


def read_front_matter(text):
    """Inverse of front_matter(): -> dict of the JSON-encoded values between the first two '---' lines ({} if none).
    PM addition (publish.py reads slug/mode/title from the approved page.md itself, not from any side file)."""
    if not isinstance(text, str) or not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].split("\n"):
        key, sep, raw = line.partition(": ")
        if not sep or not re.match(r"^[a-z_]{1,40}$", key):
            continue
        try:
            out[key] = json.loads(raw)
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------------------------------------------
# IMPLEMENT.md
# --------------------------------------------------------------------------------------------------------------
def _reviewer_label(review, meta):
    reviewer = review.get("reviewer") if isinstance(review, dict) else None
    reviewer = reviewer if isinstance(reviewer, dict) else {}
    label = _cap(reviewer.get("model_answered"), 80) or _cap((meta or {}).get("reviewer_model"), 80) or "unknown"
    family = _cap(reviewer.get("family"), 40)
    return label + ((" (" + family + " family)") if family else "")


def implement_md(draft, profile, review, meta):
    """Plain, exact instructions for an AI coding agent. Imperative, no options, no judgement calls."""
    if not isinstance(draft, dict):
        raise ToolError("bundle.implement_md: draft must be a dict")
    meta = meta if isinstance(meta, dict) else {}
    review = review if isinstance(review, dict) else {}
    today = _today(meta)
    slug = _slug(draft)
    mode = _cap(draft.get("mode"), 20)
    title = _cap(draft.get("title"), 70)
    canonical = _canonical(profile, slug)
    body_sha = _body_sha(draft, meta)
    links = _links(draft)
    changes = [c for c in _lst(draft.get("live_changes"), 50) if isinstance(c, dict)]
    writer = _cap(meta.get("writer_model"), 80) or "unknown"
    out = []

    def add(line=""):
        out.append(line)

    add("# Implementation instructions — " + (title or slug))
    add()
    add("You are an AI coding agent. Implement this page exactly as described below. Do not make choices of your own.")
    add("This bundle was written by %s, passed the deterministic checks and was adversarially reviewed by %s, which"
        % (writer, _reviewer_label(review, meta)))
    add("approved it. The files in this directory are the whole truth: `page.md` is the source, this file is the plan.")
    add("Bundle %s, generated %s, run %s." % (_cap(meta.get("bundle_id"), 64) or "n/a", today,
                                              _cap(meta.get("run_id"), 80) or "n/a"))
    add()
    add("## 1. Where the page goes")
    add()
    if mode == "new":
        add("Create a new page at the path `%s` (full URL %s). Use the body Markdown from `page.md` as its content."
            % (slug, canonical))
    else:
        add("This is an EXTEND of the page that already exists at `%s` (full URL %s)." % (slug, canonical))
        ia = _one_line(draft.get("insert_after"), 200)
        if ia:
            add("Insert the body Markdown from `page.md` (the new sections) directly after the section whose heading is")
            add("\"%s\" on the existing page, in the page's own markup and styles." % ia)
        else:
            add("Insert the body Markdown from `page.md` (the new sections) at the end of the existing page's main")
            add("content, before any closing call-to-action or footer, in the page's own markup and styles.")
        add("Keep the existing page's URL, its text and its template. Apply only the declared live changes below, and")
        add("**change nothing else**.")
        add()
        if not changes:
            add("There are no declared live changes: existing sentences stay untouched.")
        else:
            add("Declared live changes (%d). Each one is a single live sentence, before -> after:" % len(changes))
            add()
            for i, ch in enumerate(changes, 1):
                add('%d. before: "%s"' % (i, _one_line(ch.get("before"), 400)))
                add('   after:  "%s"' % _one_line(ch.get("after"), 400))
                add('   reason: "%s"' % _one_line(ch.get("reason"), 200))
                add()
            add("Change nothing else. Every change you make must appear in the list above, word for word.")
    add()
    add("## 2. Put the head exactly as in page.md")
    add()
    add("Take `title`, `description`, `canonical` and `jsonld` from the front matter of `page.md` and use them")
    add("verbatim in the document head:")
    add()
    add("- `<title>` = %s" % json.dumps(_cap(draft.get("title"), 70), ensure_ascii=True))
    add("- `<meta name=\"description\">` = %s" % json.dumps(_cap(draft.get("description"), 170), ensure_ascii=True))
    add("- `<link rel=\"canonical\">` = %s" % json.dumps(canonical, ensure_ascii=True))
    add("- one `<script type=\"application/ld+json\">` block containing the `jsonld` value, unmodified")
    add()
    add("Do not rewrite, shorten or extend any of these values. Do not add other structured data.")
    add()
    add("## 3. Add the internal links")
    add()
    if not links:
        add("No internal links are queued for this page. Add none.")
    else:
        add("Add exactly these links, one per line, with these anchors and this target `%s`:" % slug)
        add()
        for i, link in enumerate(links, 1):
            add('%d. on page `%s` link the text "%s" to `%s`' % (i, link["from"], link["anchor"], slug))
        add()
        add("Anchor text must match exactly. If the anchor text does not occur on that page, do not rewrite the page")
        add("sentence: report the link as not added instead.")
    add()
    add("## 4. Do not")
    add()
    add("- Add facts, numbers, claims, statistics, images, headings or links that are not in `page.md`.")
    add("- Change the author: keep the byline and the JSON-LD `author` exactly as given in `page.md`.")
    add("- Change the wording of the body block, even to fix a typo; report it instead.")
    add("- Touch any page other than `%s` and the pages named in step 3." % slug)
    add("- Add analytics, scripts, embeds, redirects, hreflang, meta robots or tracking parameters.")
    add("- Edit the files in this bundle directory.")
    add()
    add("## 5. Acceptance checks you must run")
    add()
    add("Run all of them and record the real result of each; do not assume any of them pass.")
    add()
    add("1. `%s` returns HTTP 200." % canonical)
    add("2. The rendered page has exactly one `<h1>` and its text equals the title %s."
        % json.dumps(_cap(draft.get("title"), 70), ensure_ascii=True))
    add("3. The canonical link equals %s." % json.dumps(canonical, ensure_ascii=True))
    add("4. Every `<script type=\"application/ld+json\">` block on the page parses as JSON and matches `page.md`.")
    add("5. Each internal link from step 3 is present with the given anchor and target.")
    add("6. The website sitemap lists %s." % canonical)
    if links:
        add("7. The pages you edited in step 3 still return 200 and no other content changed.")
    else:
        add("7. No other page changed: the diff contains only this page.")
    add("8. The body text block in `page.md` (everything between the closing `---` line and the `## FAQ` heading,")
    add("   leading and trailing blank lines ignored) has sha256 `%s` and that is unchanged after your edit." % body_sha)
    add()
    add("## 6. How to report back")
    add()
    add("Reply with: (a) the final URL, (b) the result of each of the acceptance checks above, pass or fail with the")
    add("observed value, (c) the diff summary: files changed with lines added and removed, (d) each declared live")
    add("change and whether it was applied, (e) every internal link from step 3 and whether it was added, and")
    add("(f) anything you could not do. Do not claim a check passed unless you ran it.")
    add()
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------------------
# minimal standalone HTML render
# --------------------------------------------------------------------------------------------------------------
def _strong(raw):
    return BOLD_RX.sub(lambda m: "<strong>" + m.group(1) + "</strong>", html.escape(raw, quote=False))


def _safe_href(href):
    h = _cap(href, 2000).strip()
    if not h or any(ord(c) < 32 for c in h) or "\\" in h:
        return False
    if h.startswith("//"):
        return False
    if h.startswith("/"):
        return True
    return h.lower().startswith("https://")


def _inline(raw):
    raw = _cap(raw, MAX_STR)
    out, pos = [], 0
    for m in INLINE_LINK_RX.finditer(raw):
        out.append(_strong(raw[pos:m.start()]))
        label = _strong(m.group(1))
        href = m.group(2)
        if _safe_href(href):
            out.append('<a href="%s">%s</a>' % (html.escape(_cap(href, 2000).strip(), quote=True), label))
        else:
            out.append(label)
        pos = m.end()
    out.append(_strong(raw[pos:]))
    return "".join(out)


def render_html(draft, profile, jsonld=None):
    """Minimal standalone page: everything escaped, markdown subset only, links limited to '/...' and https."""
    if not isinstance(draft, dict):
        raise ToolError("bundle.render_html: draft must be a dict")
    today = _cap(draft.get("date_modified") or "2026-01-01", 20)
    today = today if DATE_RX.match(today) else "2026-01-01"
    slug = _slug(draft)
    canonical = _canonical(profile, slug)
    data = jsonld if isinstance(jsonld, (dict, list)) else bundle_jsonld(draft, profile, today)
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
    out = ['<!DOCTYPE html>', '<html lang="en">', '<head>', '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>%s</title>' % html.escape(_cap(draft.get("title"), 70)),
           '<meta name="description" content="%s">' % html.escape(_cap(draft.get("description"), 170), quote=True),
           '<link rel="canonical" href="%s">' % html.escape(canonical, quote=True),
           '<script type="application/ld+json">%s</script>' % payload,
           '</head>', '<body>', '<main>']
    para, ul, ol = [], [], []

    def flush():
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)))
            para.clear()
        if ul:
            out.append("<ul>")
            out.extend("<li>%s</li>" % _inline(x) for x in ul)
            out.append("</ul>")
            ul.clear()
        if ol:
            out.append("<ol>")
            out.extend("<li>%s</li>" % _inline(x) for x in ol)
            out.append("</ol>")
            ol.clear()

    for line in _body_block(draft).split("\n")[:4000]:
        line = line.rstrip()
        if not line.strip():
            flush()
            continue
        m = HEAD_RX.match(line)
        if m:
            flush()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(m.group(2)), level))
            continue
        m = UL_RX.match(line)
        if m:
            if ol:
                flush()
            ul.append(m.group(1))
            continue
        m = OL_RX.match(line)
        if m:
            if ul:
                flush()
            ol.append(m.group(1))
            continue
        if ul or ol:
            if ul:
                ul.append(line.strip())
            else:
                ol.append(line.strip())
            continue
        para.append(line.strip())
    flush()
    faq = _faq(draft)
    if faq:
        out.append("<h2>FAQ</h2>")
        for item in faq:
            out.append("<h3>%s</h3>" % html.escape(item["q"], quote=False))
            out.append("<p>%s</p>" % html.escape(item["a"], quote=False))
    out.extend(['</main>', '</body>', '</html>', ''])
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------------------
# write / verify
# --------------------------------------------------------------------------------------------------------------
def _sources_summary(sources):
    recs = sources
    if isinstance(sources, dict) and isinstance(sources.get("sources"), dict):
        recs = sources["sources"]
    if not isinstance(recs, dict):
        raise ToolError("bundle.write: sources must be a {url: record} mapping")
    out = {}
    for url, rec in list(recs.items())[:MAX_LIST]:
        rec = rec if isinstance(rec, dict) else {}
        out[_cap(url, 500)] = {"final": _cap(rec.get("final") or url, 500),
                               "sha256": _cap(rec.get("sha256"), 64),
                               "status": rec.get("status") if isinstance(rec.get("status"), int) else None}
    return out


def _write_file(path, text, mode=0o644):
    data = text.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)
    return hashlib.sha256(data).hexdigest()


def write(out_dir, draft, review, checks, sources, profile, meta, html=False):
    """Write the approved bundle atomically. Refuses unless the review verdict is 'approve' and checks ok."""
    if not isinstance(draft, dict):
        raise ToolError("bundle.write: draft must be a dict")
    review = review if isinstance(review, dict) else {}
    checks = checks if isinstance(checks, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    verdict = _cap(review.get("verdict"), 40)
    if verdict != "approve":
        raise ToolError("bundle.write: refused — review verdict is %r, only 'approve' can be written."
                        % scrub(verdict or "missing"))
    if checks.get("ok") is not True:
        raise ToolError("bundle.write: refused — the deterministic checks did not pass (checks['ok'] is not True).")
    today = _today(meta)
    slug = _slug(draft)
    bundle_id = _cap(meta.get("bundle_id"), 64).strip() or _sha(slug)[:12]
    id6 = TAIL_RX.sub("", bundle_id.lower())[:6] or "000000"
    name = "%s-%s-%s" % (today.replace("-", ""), _slug_tail(slug), id6)
    target = _cap(out_dir, 1024).strip()
    if not target:
        raise ToolError("bundle.write: out_dir is required")
    os.makedirs(target, exist_ok=True)
    final = os.path.join(target, name)
    if os.path.exists(final):
        raise ToolError("bundle.write: refused — %s already exists, refusing to overwrite." % name)

    fm = front_matter(draft, profile, today, meta)
    body = _body_block(draft)
    page = fm + "\n" + body + "\n"
    faq = _faq(draft)
    if faq:
        page += "\n## FAQ\n\n"
        for item in faq:
            page += "### %s\n\n%s\n\n" % (item["q"], item["a"])
    impl = implement_md(draft, profile, review, meta)
    summary = _sources_summary(sources)
    reviewer = review.get("reviewer") if isinstance(review.get("reviewer"), dict) else {}

    tmp = tempfile.mkdtemp(prefix=".bundle-", dir=target)
    try:
        os.chmod(tmp, 0o755)
        hashes = {}
        hashes["page.md"] = _write_file(os.path.join(tmp, "page.md"), page)
        hashes["IMPLEMENT.md"] = _write_file(os.path.join(tmp, "IMPLEMENT.md"), impl)
        hashes["review.json"] = _write_file(os.path.join(tmp, "review.json"),
                                            json.dumps(review, indent=1, ensure_ascii=True) + "\n")
        hashes["checks.json"] = _write_file(os.path.join(tmp, "checks.json"),
                                            json.dumps(checks, indent=1, ensure_ascii=True) + "\n")
        hashes["sources.json"] = _write_file(os.path.join(tmp, "sources.json"),
                                             json.dumps(summary, indent=1, ensure_ascii=True) + "\n")
        if html:
            hashes["page.html"] = _write_file(os.path.join(tmp, "page.html"), render_html(draft, profile))
        manifest = {
            "files": hashes,
            "bundle_id": bundle_id,
            "created_at": _cap(meta.get("created_at"), 40) or (today + "T00:00:00Z"),
            "verdict": "approve",
            "reviewer_model": _cap(reviewer.get("model_answered"), 80) or _cap(meta.get("reviewer_model"), 80),
            "writer_model": _cap(meta.get("writer_model"), 80),
        }
        _write_file(os.path.join(tmp, "MANIFEST.json"), json.dumps(manifest, indent=1, ensure_ascii=True) + "\n")
        os.rename(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"path": final, "manifest": manifest}


def verify(path):
    """Re-hash every file listed in MANIFEST.json. Missing, extra or modified files are errors."""
    path = _cap(path, 1024).strip()
    errors = []
    if not path or not os.path.isdir(path):
        return False, ["bundle.verify: %r is not a directory" % scrub(path)]
    manifest_path = os.path.join(path, "MANIFEST.json")
    if not os.path.isfile(manifest_path):
        return False, ["bundle.verify: MANIFEST.json is missing"]
    try:
        with open(manifest_path, "rb") as fh:
            manifest = json.loads(fh.read(262144).decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        return False, ["bundle.verify: MANIFEST.json is not readable JSON (%s)" % scrub(exc)]
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        return False, ["bundle.verify: MANIFEST.json has no files map"]
    listed = set()
    for name, digest in list(files.items())[:MAX_LIST]:
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith(".") \
                or name == "MANIFEST.json" or not isinstance(digest, str):
            errors.append("bundle.verify: bad manifest entry %r" % scrub(name))
            continue
        listed.add(name)
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            errors.append("bundle.verify: file %s is missing" % name)
            continue
        try:
            with open(full, "rb") as fh:
                data = fh.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:          # PM: was hashing only the first 4 MB (a tail could be appended)
                errors.append("bundle.verify: %s is larger than 8 MB" % name)
                continue
            actual = hashlib.sha256(data).hexdigest()
        except OSError as exc:
            errors.append("bundle.verify: %s is unreadable (%s)" % (name, scrub(exc)))
            continue
        if actual != digest.lower():
            errors.append("bundle.verify: %s does not match its manifest hash" % name)
    for required in ("page.md", "IMPLEMENT.md", "review.json", "checks.json"):     # PM: a manifest must cover these
        if required not in listed:
            errors.append("bundle.verify: MANIFEST.json does not list %s" % required)
    if manifest.get("verdict") != "approve":
        errors.append("bundle.verify: MANIFEST.json verdict is not approve")
    for entry in sorted(os.listdir(path)):
        if entry == "MANIFEST.json" or entry in listed or entry.startswith(".bundle-"):
            continue
        errors.append("bundle.verify: unexpected file %s in the bundle" % scrub(entry)[:60])
    return (not errors), errors


# --------------------------------------------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    n = 0

    def chk(cond, msg):
        nonlocal n
        assert cond, "bundle.selftest: " + msg
        n += 1

    profile = {"site": "https://example.com/", "host": "example.com",
               "author": {"name": "Jane Doe", "url": "/team/jane/",
                          "id": "https://example.com/team/jane/#person"}}
    base = {
        "mode": "new", "slug": "/guides/flat-fee/",
        "title": "Flat-fee property management: what the fee covers",
        "description": "What a flat monthly fee covers, what it does not cover, and how to compare two quotes line by line.",
        "content_type": "guide",
        "body_md": ("# Flat fee management\n\nA flat fee of $149 per month per home.\n\n"
                    "- one clear line\n- two clear lines\n\n"
                    "See [pricing](/pricing/) and [bad](javascript:alert) and <script>alert(1)</script>.\n"),
        "faq": [{"q": "Is the fee monthly?", "a": "Yes, charged every month per home."}],
        "sources": [{"url": "https://example.com/pricing/", "quote": "Management is $149 per month per home."}],
        "live_changes": [], "internal_links": [{"from": "/pricing/", "anchor": "flat fee guide"}], "jsonld": None,
    }
    review = {"verdict": "approve", "reviewer": {"model_answered": "glm-4.6", "family": "glm"}, "findings": []}
    checks = {"ok": True, "errors": [], "warnings": [], "stats": {"words": 812}}
    sources = {"https://example.com/pricing/": {"final": "https://example.com/pricing/", "sha256": "a" * 64,
                                                "status": 200, "text": "do not leak the full text"}}
    meta = {"bundle_id": "ab12cd34ef567890", "today": "2026-11-10", "writer_model": "deepseek-chat",
            "reviewer_model": "glm-4.6", "run_id": "run-1"}

    root = tempfile.mkdtemp(prefix="bundle-selftest-")
    try:
        # refusals first: a bundle can never be written without approval and green checks
        try:
            write(root, base, dict(review, verdict="hold"), checks, sources, profile, meta)
            raise AssertionError("bundle.selftest: hold verdict was written")
        except ToolError as exc:
            chk("refused" in str(exc), "hold verdict must be refused")
        try:
            write(root, base, review, {"ok": False, "errors": [{"code": "THIN"}]}, sources, profile, meta)
            raise AssertionError("bundle.selftest: failed checks were written")
        except ToolError as exc:
            chk("refused" in str(exc), "failed checks must be refused")
        chk(os.listdir(root) == [] or all(x.startswith(".") for x in os.listdir(root)), "no dir on refusal")

        res = write(root, base, review, checks, sources, profile, meta, html=True)
        path = res["path"]
        chk(os.path.isdir(path), "bundle dir exists")
        chk(os.path.basename(path) == "20261110-flat-fee-ab12cd", "dir name shape")
        for name in ("page.md", "IMPLEMENT.md", "review.json", "checks.json", "sources.json", "page.html",
                     "MANIFEST.json"):
            chk(os.path.isfile(os.path.join(path, name)), name + " written")
        chk(oct(os.stat(path).st_mode & 0o777) == "0o755", "dir mode 0755")
        chk(oct(os.stat(os.path.join(path, "page.md")).st_mode & 0o777) == "0o644", "file mode 0644")

        ok, errors = verify(path)
        chk(ok is True, "fresh bundle verifies: %r" % (errors,))
        chk(errors == [], "no verify errors on fresh bundle")

        page = open(os.path.join(path, "page.md"), encoding="utf-8").read()
        chk(page.startswith("---\n"), "page.md opens with front matter")
        body_start = page.index("\n---\n", 4) + 5
        fm_txt = page[:body_start]
        chk('"https://example.com/guides/flat-fee/"' in fm_txt, "canonical in front matter")
        chk("date_published" in fm_txt, "new draft has date_published")
        chk('"@type": "FAQPage"' in fm_txt, "FAQPage present when faq")
        fm_lines = [ln for ln in fm_txt.split("\n")[1:] if ln and ln != "---"]
        chk(len(fm_lines) >= 10, "front matter has the expected keys")
        for ln in fm_lines:
            key, _, rest = ln.partition(": ")
            chk(bool(key) and not key.startswith(" "), "front matter key %r is bare" % key[:20])
            json.loads(rest)
        body_block = page[body_start:].split("\n## FAQ\n")[0].strip("\n")
        chk(body_block == base["body_md"].strip("\n"), "body block is written verbatim")
        chk("## FAQ" in page and "### Is the fee monthly?" in page, "FAQ section rendered in page.md")

        impl = open(os.path.join(path, "IMPLEMENT.md"), encoding="utf-8").read()
        chk("an AI coding agent" in impl, "IMPLEMENT addresses an AI coding agent")
        chk(hashlib.sha256(body_block.encode()).hexdigest() in impl, "IMPLEMENT quotes the body sha256")
        chk("glm-4.6" in impl, "IMPLEMENT names the reviewer model")
        chk("Create a new page at the path `/guides/flat-fee/`" in impl, "new draft gets the create instruction")
        chk("/guides/flat-fee/" in impl, "IMPLEMENT names the slug path")
        chk("Do not" in impl, "Do not list present")
        chk("sitemap" in impl and "exactly one `<h1>`" in impl, "acceptance checks present")
        chk("/pricing/" in impl and "flat fee guide" in impl, "internal link step lists from/anchor")

        review_json = json.loads(open(os.path.join(path, "review.json"), encoding="utf-8").read())
        chk(review_json["verdict"] == "approve", "review.json holds the review")
        sources_json = json.loads(open(os.path.join(path, "sources.json"), encoding="utf-8").read())
        chk(set(sources_json["https://example.com/pricing/"]) == {"final", "sha256", "status"},
            "sources.json is a summary only")
        chk("text" not in sources_json["https://example.com/pricing/"], "sources.json drops full texts")
        manifest = json.loads(open(os.path.join(path, "MANIFEST.json"), encoding="utf-8").read())
        chk(manifest["verdict"] == "approve", "manifest verdict")
        chk(manifest["reviewer_model"] == "glm-4.6" and manifest["writer_model"] == "deepseek-chat",
            "manifest model names")
        chk("MANIFEST.json" not in manifest["files"], "manifest does not hash itself")
        for name, digest in manifest["files"].items():
            actual = hashlib.sha256(open(os.path.join(path, name), "rb").read()).hexdigest()
            chk(actual == digest, "manifest hash matches " + name)

        page_html = open(os.path.join(path, "page.html"), encoding="utf-8").read()
        chk("&lt;script&gt;alert(1)&lt;/script&gt;" in page_html, "html escapes <script>")
        chk("<script>alert(1)</script>" not in page_html, "no raw script tag from the body")
        chk('href="javascript' not in page_html, "javascript: link not rendered as a link")
        chk("<strong>" not in page_html or "**" not in page_html, "bold handled")
        chk('<a href="/pricing/">pricing</a>' in page_html, "relative link kept as a link")
        chk("<ul>" in page_html and "<li>one clear line</li>" in page_html, "list rendered")
        chk("<h1>Flat fee management</h1>" in page_html, "heading rendered")

        # extending: every live change is listed and the page must not change otherwise
        ext = dict(base, mode="extend", faq=[],
                   live_changes=[{"before": "We charge a management fee.",
                                  "after": "We charge a flat fee of $149.",
                                  "reason": "name the product"}, ])
        ext_impl = implement_md(ext, profile, review, meta)
        chk("We charge a management fee." in ext_impl and "We charge a flat fee of $149." in ext_impl,
            "extend lists the live change before/after")
        chk("name the product" in ext_impl, "extend lists the reason")
        chk("change nothing else" in ext_impl, "extend says change nothing else")
        chk("EXTEND" in ext_impl, "extend is labelled as an extend")
        chk("date_published" not in front_matter(ext, profile, "2026-11-10", meta),
            "extend front matter has no date_published")
        res2 = write(os.path.join(root, "ext"), ext, review, checks, sources, profile, meta)
        chk(verify(res2["path"])[0] is True, "extend bundle verifies")

        # tampering with a single byte must fail verification
        tampered = os.path.join(root, "tampered")
        shutil.copytree(path, tampered)
        target = os.path.join(tampered, "page.md")
        with open(target, "r+b") as fh:
            fh.seek(5)
            first = fh.read(1)
            fh.seek(5)
            fh.write(b"X" if first != b"X" else b"Y")
        ok_t, errs_t = verify(tampered)
        chk(ok_t is False, "tampered bundle fails verify")
        chk(any("page.md" in e for e in errs_t), "tamper error names page.md")

        extra = os.path.join(root, "extra")
        shutil.copytree(path, extra)
        with open(os.path.join(extra, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("smuggled\n")
        ok_e, errs_e = verify(extra)
        chk(ok_e is False and any("notes.txt" in e for e in errs_e), "extra file fails verify")

        missing = os.path.join(root, "missing")
        shutil.copytree(path, missing)
        os.remove(os.path.join(missing, "sources.json"))
        ok_m, errs_m = verify(missing)
        chk(ok_m is False and any("sources.json" in e for e in errs_m), "missing file fails verify")

        try:
            write(root, base, review, checks, sources, profile, meta)
            raise AssertionError("bundle.selftest: existing dir was overwritten")
        except ToolError as exc:
            chk("already exists" in str(exc), "existing bundle dir is refused")

        ok_n, _ = verify(os.path.join(root, "nope"))
        chk(ok_n is False, "verify on a missing dir fails")
        chk(article_jsonld(base, profile, "2026-11-10")["datePublished"] == "2026-11-10", "article published date")
        chk(article_jsonld(ext, profile, "2026-11-10").get("dateModified") == "2026-11-10", "article modified date")
        chk(json.dumps(article_jsonld(base, profile, "2026-11-10"))[:20].startswith('{"@context"'),
            "article jsonld is serialisable")
        chk(front_matter(base, profile, "2026-11-10", meta).count("---") >= 2, "front matter delimited")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return n
