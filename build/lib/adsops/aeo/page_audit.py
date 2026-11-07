"""Answer-readiness (AEO) auditing for single HTML pages.

`analyze()` scores a page 0-100 on nine answer-engine signals (answer-first
paragraph, question headings, FAQ block, structured data, concrete specifics,
freshness, outbound citations, entity clarity, scannability) and returns
concrete fix `findings` plus extractable `answer_blocks` and
`suggested_questions`. `compare()` ranks many pages against the best one.

Safety: pure functions only. No network, no clock, no file I/O at import or
call time — the clock is injected via `today=`. Nothing is printed; tools
fetch HTML with their own injectable fetcher and pass it here.
"""

import json
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from adsops.errors import RailViolation, ToolError, scrub  # noqa: F401

# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm(text):
    """Collapse all whitespace runs to single spaces."""
    return _WS.sub(" ", text or "").strip()


def _g(row, *path, default=None):
    """Nested getter for camelCase Google Ads REST rows (defensive)."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


_QWORDS = frozenset(
    "how what why when where who which can does is should do are will could would".split()
)
_LEAD_STOP = frozenset({"this", "it", "they", "these", "here"})


def _is_question(text):
    """A heading counts as a question if it ends with '?' or opens with a Q word."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    first = re.split(r"[\s:,]+", t.lower(), maxsplit=1)[0]
    return first in _QWORDS


_STOP = frozenset("is are was be do does did can could should would will how what why when where who which the an and or "
                  "of to in on for with my your our it its this that you we i me much many".split())


def _tokens(text):
    """Content tokens (len>1, stopwords dropped) for question-overlap matching.

    Stopwords are dropped so "Is A secure?" is NOT counted as covered by "What is A?" (the maintainers review fix 2026-11-05:
    the shared word "is" alone gave 50% overlap)."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1 and w not in _STOP}


_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_MONTH_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.I,
)


def _parse_date(value):
    """Best-effort ISO / human date parse. Returns a date or None (never raises)."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for cand in (s, s[:10]):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def _dates_in_text(text):
    """Find displayable dates: 2024-01-05, 1/5/2024, January 5, 2024."""
    out = []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text or ""):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text or ""):
        try:
            out.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    for m in _MONTH_RE.finditer(text or ""):
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                out.append(date(int(m.group(3)), mon, int(m.group(2))))
            except ValueError:
                pass
    return out


def _jsonld_dates(obj, out):
    """Recursively collect datePublished / dateModified from parsed JSON-LD."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in ("datePublished", "dateModified") and isinstance(val, str):
                d = _parse_date(val)
                if d:
                    out.append(d)
            else:
                _jsonld_dates(val, out)
    elif isinstance(obj, list):
        for val in obj:
            _jsonld_dates(val, out)


def _has_type(objs, wanted):
    """True when any @type in the JSON-LD tree equals `wanted` (case-insensitive)."""
    wanted = wanted.lower()
    stack = list(objs)
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            t = cur.get("@type")
            if isinstance(t, str) and t.lower() == wanted:
                return True
            if isinstance(t, list) and any(
                isinstance(x, str) and x.lower() == wanted for x in t
            ):
                return True
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return False


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        d = _parse_date(value)
        if d:
            return d
    raise ToolError("today must be a date, datetime or ISO date string")


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------


class _Doc(HTMLParser):
    """Collects ordered blocks (h1-h3 / p) plus lists, tables, links, <time>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []  # [("h1"|"h2"|"h3"|"p", text)] in document order
        self.list_count = 0
        self.table_count = 0
        self.links = []
        self.jsonld_raw = []
        self.times = []  # datetime attrs and text of <time>
        self.text_parts = []
        self._cap = None  # tag currently being captured
        self._buf = []
        self._tbuf = None
        self._script = None
        self._sbuf = []

    def _flush(self):
        tag, self._cap = self._cap, None
        txt = _norm("".join(self._buf))
        self._buf = []
        if tag and txt:
            self.blocks.append((tag, txt))

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            if tag == "script" and "ld+json" in a.get("type", "").lower():
                self._script, self._sbuf = "jsonld", []
            else:
                self._script = "skip"
            return
        if tag == "br":
            if self._cap:
                self._buf.append(" ")
            return
        if tag in ("h1", "h2", "h3", "p"):
            self._flush()  # implicit close of a dangling block
            self._cap, self._buf = tag, []
            return
        if tag in ("ul", "ol"):
            self.list_count += 1
        elif tag == "table":
            self.table_count += 1
        elif tag == "a":
            href = a.get("href", "").strip()
            if href:
                self.links.append(href)
        elif tag == "time":
            self._tbuf = []
            if a.get("datetime"):
                self.times.append(a["datetime"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._script == "jsonld":
                self.jsonld_raw.append("".join(self._sbuf))
            self._script, self._sbuf = None, []
            return
        if tag == "time":
            if self._tbuf is not None:
                txt = _norm("".join(self._tbuf))
                if txt:
                    self.times.append(txt)
                self._tbuf = None
            return
        if tag == self._cap:
            self._flush()

    def handle_data(self, data):
        if self._script:
            if self._script == "jsonld":
                self._sbuf.append(data)
            return
        self.text_parts.append(data)
        if self._cap:
            self._buf.append(data)
        if self._tbuf is not None:
            self._tbuf.append(data)

    def finish(self):
        self.close()
        self._flush()


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _grade(score):
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def analyze(html, url="", target_questions=(), brand=None, today=None):
    """Score one page for answer-engine extractability (0-100)."""
    if not isinstance(html, str):
        raise ToolError("html must be a string")
    if isinstance(target_questions, str):
        target_questions = [target_questions]

    doc = _Doc()
    try:
        doc.feed(html)
        doc.finish()
    except Exception as exc:  # a broken page must never crash a tool call
        raise ToolError(scrub(f"could not parse html: {exc}")) from None

    blocks = doc.blocks
    headings = [(int(t[1]), txt) for t, txt in blocks if t in ("h1", "h2", "h3")]
    h1 = next((txt for lvl, txt in headings if lvl == 1), "")
    paras = [txt for t, txt in blocks if t == "p"]
    text = _norm(" ".join(doc.text_parts))
    words = text.split()
    wc = len(words)

    # first paragraph that follows the H1 (document order)
    first_p = ""
    for i, (tag, _txt) in enumerate(blocks):
        if tag == "h1":
            for tag2, txt2 in blocks[i + 1:]:
                if tag2 == "p":
                    first_p = txt2
                    break
            break

    # first paragraph anywhere (for entity clarity)
    first_p_overall = next((txt for t, txt in blocks if t == "p"), "")

    ld = []
    for raw in doc.jsonld_raw:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, (dict, list)):
            ld.append(obj)

    today_d = _as_date(today) or date.today()
    score = 0
    signals = {}
    findings = []

    def add(name, ok, pts, fix, **extra):
        nonlocal score
        ok = bool(ok)
        signals[name] = dict(extra, ok=ok, points=pts if ok else 0)
        if ok:
            score += pts
        else:
            findings.append(fix)

    # 1. answer_first (20) -------------------------------------------------
    fp_words = first_p.split()
    lead = fp_words[0].lower().strip(".,;:!?\"'()") if fp_words else ""
    af_ok = bool(first_p) and 25 <= len(fp_words) <= 80 and lead not in _LEAD_STOP
    add(
        "answer_first",
        af_ok,
        20,
        "Open with a self-contained 25-80 word paragraph straight under the H1 "
        "instead of starting with a pronoun like 'This' or 'It'.",
        words=len(fp_words),
    )

    # 2. question_headings (15) --------------------------------------------
    subs = [txt for lvl, txt in headings if lvl in (2, 3)]
    q_count = sum(1 for t in subs if _is_question(t))
    share = (q_count / len(subs)) if subs else 0.0
    add(
        "question_headings",
        bool(subs) and share >= 0.3,
        15,
        "Rewrite at least 30% of your H2/H3 headings as real questions "
        "(How/What/Why/... or ending in '?') so engines can match user queries.",
        questions=q_count,
        subheadings=len(subs),
        share=round(share, 2),
    )

    # 3. faq_block (10) -----------------------------------------------------
    faq_heading = any(("faq" in t.lower()) or ("frequently asked" in t.lower()) for _, t in headings)
    faq_ld = _has_type(ld, "FAQPage")
    add(
        "faq_block",
        faq_heading or faq_ld,
        10,
        "Add a visible FAQ section with question-style headings and mirror it in "
        "FAQPage JSON-LD.",
        heading=faq_heading,
        jsonld=faq_ld,
    )

    # 4. structured_data (10) ----------------------------------------------
    add(
        "structured_data",
        bool(ld),
        10,
        "Emit at least one valid JSON-LD block (Organization, FAQPage, Product...) "
        "so machines can read your entities without guessing.",
        blocks=len(ld),
    )

    # 5. specifics (15) -----------------------------------------------------
    spec_tokens = [w for w in words if any(c.isdigit() for c in w) or "$" in w]
    dens = (len(spec_tokens) * 100.0 / wc) if wc else 0.0
    add(
        "specifics",
        dens >= 2.0,
        15,
        "Add concrete numbers, prices and dates; aim for at least 2 numeric or "
        "price tokens per 100 words so answers are quotable.",
        per_100_words=round(dens, 2),
        tokens=len(spec_tokens),
    )

    # 6. freshness (10) -----------------------------------------------------
    dates = []
    _jsonld_dates(ld, dates)
    for raw in doc.times:
        d = _parse_date(raw)
        dates.extend([d] if d else _dates_in_text(raw))
    dates.extend(_dates_in_text(text))
    fresh = [d for d in dates if abs((today_d - d).days) <= 180]
    add(
        "freshness",
        bool(fresh),
        10,
        "Show a visible publish/update date (and dateModified in JSON-LD) no older "
        "than 180 days; stale pages get demoted as answers.",
        dated=len(dates),
    )

    # 7. citations (10) -----------------------------------------------------
    own = urlparse(url).netloc.lower() if url else ""
    hosts = set()
    for href in doc.links:
        p = urlparse(href)
        if p.scheme not in ("http", "https"):
            continue
        host = (p.netloc or "").lower()
        if not host:
            continue
        if own and (host == own or host.endswith("." + own)):
            continue
        hosts.add(host)
    add(
        "citations",
        len(hosts) >= 2,
        10,
        "Link out to at least two authoritative external sources so claims stay "
        "verifiable instead of self-referential.",
        domains=sorted(hosts)[:10],
        count=len(hosts),
    )

    # 8. entity_clarity (5) -------------------------------------------------
    brand_hit = False
    if brand:
        b = str(brand).strip().lower()
        brand_hit = bool(b) and (b in h1.lower() or b in first_p_overall.lower())
    add(
        "entity_clarity",
        brand_hit or bool(h1),
        5,
        "Put exactly one H1 on the page and name your brand in it or in the first "
        "paragraph so the entity is unambiguous.",
        brand_in_h1=bool(brand) and str(brand).strip().lower() in h1.lower(),
        h1=h1,
    )

    # 9. scannability (5) ---------------------------------------------------
    avg_para = (sum(len(p.split()) for p in paras) / len(paras)) if paras else 0.0
    add(
        "scannability",
        (doc.list_count + doc.table_count) >= 1 and avg_para < 90,
        5,
        "Break copy into bullet lists or a table and keep paragraphs under 90 words "
        "so passages are chunkable.",
        lists=doc.list_count,
        tables=doc.table_count,
        avg_paragraph_words=round(avg_para, 1),
    )

    # extractable passages: a 40-90 word paragraph directly under a question heading
    answer_blocks = []
    for i, (tag, txt) in enumerate(blocks):
        if tag in ("h1", "h2", "h3") and _is_question(txt) and i + 1 < len(blocks):
            nxt_tag, nxt_txt = blocks[i + 1]
            if nxt_tag == "p" and 40 <= len(nxt_txt.split()) <= 90:
                answer_blocks.append({"question": txt, "answer": nxt_txt})

    # questions the page does not already answer with a heading
    heading_tokens = [_tokens(t) for _, t in headings]
    suggested = []
    for q in target_questions or ():
        q = str(q).strip()
        qt = _tokens(q)
        if not q or not qt:
            continue
        covered = any(len(qt & ht) / len(qt) >= 0.5 for ht in heading_tokens)
        if not covered:
            suggested.append(q)

    score = max(0, min(100, round(score)))
    return {
        "url": url,
        "score": score,
        "grade": _grade(score),
        "signals": signals,
        "findings": findings,
        "answer_blocks": answer_blocks,
        "suggested_questions": suggested,
    }


def compare(pages, **kw):
    """Rank [(url, html), ...] by score; `delta_vs_best` is <= 0 (0 = best)."""
    if not isinstance(pages, (list, tuple)):
        raise ToolError("pages must be a list of (url, html) pairs")
    kw.pop("url", None)  # url always comes from the pair, never from kwargs
    rows = []
    for item in pages:
        try:
            url, html = item
        except (TypeError, ValueError):
            raise ToolError("each page must be a (url, html) pair") from None
        res = analyze(html, url=url, **kw)
        rows.append(
            {
                "url": url,
                "score": res["score"],
                "grade": res["grade"],
                "findings": len(res["findings"]),
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["url"]))
    best = rows[0]["score"] if rows else 0
    for r in rows:
        r["delta_vs_best"] = round(r["score"] - best, 2)
    return rows


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


def selftest() -> int:
    """>=15 assertions, everything injected/mocked, no network, no real clock."""
    n = 0

    def check(cond, msg="assertion failed"):
        nonlocal n
        if not cond:
            raise AssertionError(msg)
        n += 1

    def raises(fn, exc=ToolError):
        try:
            fn()
        except exc:
            return True
        return False

    def words(k, stem="w"):
        return " ".join(f"{stem}{i}" for i in range(k))

    # --- _g nested camelCase helper --------------------------------------
    row = {"campaign": {"id": "1", "name": "x"}, "metrics": {"costMicros": "2500000"}}
    check(_g(row, "campaign", "name") == "x", "_g happy path")
    check(_g(row, "campaign", "missing", default=0) == 0, "_g default")
    check(_g(row, "nope", "deep") is None, "_g missing branch")
    check(int(_g(row, "metrics", "costMicros", default="0")) == 2500000, "_g micros coercion")
    check(raises(lambda: analyze(123)), "non-str html must raise ToolError")

    today = date(2024, 6, 15)
    ld_faq = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"FAQPage","dateModified":"2024-06-01"}'
        "</script>"
    )
    good = (
        "<html><head>" + ld_faq + "</head><body>"
        "<h1>AdRails Pricing</h1>"
        "<p>AdRails costs 149 dollars per month for the Starter plan and 399 dollars "
        "per month for Growth, billed monthly since 2024-05-01 with no setup fee at all.</p>"
        "<h2>What is included in the Starter plan?</h2><p>" + words(45, "plan") + "</p>"
        "<h2>How does billing work?</h2><p>" + words(45, "billing") + "</p>"
        "<h2>Can I cancel anytime?</h2><p>" + words(45, "cancel") + "</p>"
        "<h2>Frequently asked questions</h2><ul><li>Yes</li></ul>"
        "<table><tr><td>a</td></tr></table>"
        '<p>Source: <a href="https://example.org/a">A</a> and '
        '<a href="https://other.net/b">B</a></p>'
        "</body></html>"
    )
    bad = "<html><body><p>This is a page.</p></body></html>"

    r = analyze(
        good,
        url="https://adrails.example/pricing",
        brand="AdRails",
        today=today,
        target_questions=["What is included in the Starter plan?", "How much does it cost?"],
    )
    check(r["score"] == 100, f"good page should score 100, got {r['score']}")
    check(r["grade"] == "A", "good page grade A")
    check(all(s["ok"] for s in r["signals"].values()), "all nine signals earned")
    check(len(r["signals"]) == 9, "nine signals reported")
    check(r["findings"] == [], "no findings for a perfect page")
    check(len(r["answer_blocks"]) == 3, "three extractable answer blocks")
    check(r["answer_blocks"][0]["question"].startswith("What is included"), "block question kept")
    check(r["answer_blocks"][0]["answer"] == words(45, "plan"), "block answer kept")
    check(r["suggested_questions"] == ["How much does it cost?"], "covered question suppressed")

    b = analyze(bad, today=today)
    check(b["score"] == 0, f"bad page should score 0, got {b['score']}")
    check(b["grade"] == "F", "bad page grade F")
    check(len(b["findings"]) == 9, f"nine fixes for bad page, got {len(b['findings'])}")
    check(analyze("", today=today)["score"] == 0, "empty html scores 0 without crashing")

    broken = '<h1>X</h1><script type="application/ld+json">{not json}</script>'
    check(
        analyze(broken, today=today)["signals"]["structured_data"]["ok"] is False,
        "invalid JSON-LD must not count as structured data",
    )

    # answer_first boundaries: pronoun opener, 24 vs 25 words
    body = " ".join(["value"] * 29) + " ends."
    check(analyze(f"<h1>X</h1><p>Widget {body}</p>", today=today)["signals"]["answer_first"]["ok"])
    check(
        analyze(f"<h1>X</h1><p>This {body}</p>", today=today)["signals"]["answer_first"]["ok"]
        is False,
        "pronoun opener must fail answer_first",
    )
    check(
        analyze(f"<h1>X</h1><p>{' '.join(['w'] * 24)}</p>", today=today)["signals"][
            "answer_first"
        ]["ok"]
        is False,
        "24 words is below the 25-word floor",
    )
    check(
        analyze(f"<h1>X</h1><p>{' '.join(['w'] * 25)}</p>", today=today)["signals"][
            "answer_first"
        ]["ok"],
        "25 words is the inclusive floor",
    )

    # freshness goes stale when the clock moves on
    stale = analyze(good, url="https://adrails.example/pricing", brand="AdRails", today=date(2025, 6, 15))
    check(stale["score"] == 90, f"stale page loses 10 points, got {stale['score']}")
    check(stale["signals"]["freshness"]["ok"] is False, "stale dates fail freshness")
    check(len(stale["findings"]) == 1, "exactly one stale finding")
    check(analyze(good, today="2024-06-15")["score"] == 100, "ISO string `today` accepted")
    check(raises(lambda: analyze(good, today=object())), "bad `today` raises ToolError")

    # compare / ranking
    ranked = compare(
        [("https://b.example/", bad), ("https://a.example/", good)],
        brand="AdRails",
        today=today,
    )
    check(len(ranked) == 2, "compare returns every page")
    check(ranked[0]["url"] == "https://a.example/", "best page ranks first")
    check(ranked[0]["delta_vs_best"] == 0, "best page delta is 0")
    check(ranked[1]["delta_vs_best"] == -100, "worst page delta is -100")
    check(compare([]) == [], "compare of nothing is an empty list")
    check(raises(lambda: compare("nope")), "compare rejects non-list input")
    check(raises(lambda: compare([("only-url",)])), "compare rejects non-pairs")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)