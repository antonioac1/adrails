"""Ads -> answers bridge: turn paid-search data into AEO content gaps.

Consumes normalised search-term rows (flat dicts, or Google Ads REST
``searchTermView``/``metrics`` rows -- micros are converted to currency units
rounded to 2 dp) and returns question clusters, a page-coverage map, markdown
content briefs and a prioritised roadmap.  Every function is pure: no network,
no clock, no file I/O, so callers inject their own data.  Safety: briefs never
invent facts or figures (they cite only caller-supplied ``site_facts``), echoed
text is length-capped, and oversized inputs are refused with RailViolation.
Tools: call roadmap() for a report, brief() for a single question.
"""

from __future__ import annotations

import json
import re
from math import isfinite

try:  # real error types; fallback keeps the module importable in isolation
    from adsops.errors import RailViolation, ToolError, scrub
except Exception:  # pragma: no cover - only hit outside the adsops package
    class ToolError(Exception):
        """Bad input / user-visible failure."""

    class RailViolation(Exception):
        """A safety rail refused the operation."""

    def scrub(text, limit=1500):
        return str(text)[:limit]


MAX_TERMS = 5000
MAX_TOP = 50
MAX_QUESTION_CHARS = 500
MAX_TERM_CHARS = 200
MAX_H2 = 6

_QUESTION_PREFIX = ("how", "what", "why", "is", "are", "can", "does", "do",
                    "should", "will", "when", "where", "which", "who")

# Any of these tokens makes a term "intent rich" enough to be worth a question.
_TRIGGERS = frozenset(
    "how what why is are can does do should will when where which who best cost "
    "costs price prices pricing vs versus near".split()
)

STOP = frozenset(
    "a about an and are as at be been being but by can could did do does for from "
    "had has have how i if in into is it its me much my near need needs of on or our "
    "should so than that the their them then there these this to too was we were what "
    "when where which who why will with you your".split()
)

_WORD_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- primitives
def _g(row, *path, default=None):
    """Nested get for camelCase API rows: _g(row, "metrics", "clicks")."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int):
            cur = cur[key] if -len(cur) <= key < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def _num(value):
    """Defensive numeric coercion: '2,500' -> 2500.0, junk/None/bool -> 0.0."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value.strip().replace(",", ""))
        except ValueError:
            return 0.0
    else:
        return 0.0
    return out if isfinite(out) else 0.0


def _money(value):
    """Currency units rounded to 2 dp (normalises -0.0 -> 0.0)."""
    out = round(_num(value), 2)
    return out if out else 0.0


def _tidy(value):
    """Ints stay ints, fractional counts keep 4 dp (JSON-friendly)."""
    num = _num(value)
    return int(num) if num.is_integer() else round(num, 4)


def _norm_space(text):
    return _WS_RE.sub(" ", text).strip() if isinstance(text, str) else ""


def _tokens(text, drop_stop=True):
    """Lowercase word tokens with light singularisation ('costs' -> 'cost')."""
    if not isinstance(text, str):
        return []
    low = _WORD_RE.sub(" ", text.lower().replace("'", "").replace("\u2019", ""))
    out = []
    for tok in low.split():
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        if drop_stop and tok in STOP:
            continue
        out.append(tok)
    return out


def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _is_intent_rich(term):
    low = term.lower()
    if "near me" in low:
        return True
    return bool(set(re.findall(r"[a-z0-9]+", low)) & _TRIGGERS)


def _to_question(term, city=None):
    """Template table that rewrites a search term into a natural question."""
    body = _norm_space(term).strip(" ?!.,;:")
    if not body:
        return ""
    words = body.lower().split()
    if words and words[0] in _QUESTION_PREFIX:  # already question-shaped
        return body[0].upper() + body[1:] + "?"
    m = re.match(r"^(.*?)\s+vs\.?\s+(.+)$", body, re.I)
    if m:
        return "%s vs %s: which is better?" % (m.group(1), m.group(2))
    m = re.match(r"^best\s+(.+)$", body, re.I)
    if m:
        return "What is the best %s?" % m.group(1)
    m = re.match(r"^(.*?)\s+near\s+me$", body, re.I)
    if m:
        if city:
            return "Who offers %s near %s?" % (m.group(1), city)
        return "Who offers %s near me?" % m.group(1)
    m = re.match(r"^(?:cost|costs|price|prices|pricing)\s+(?:of|for)\s+(.+)$", body, re.I)
    if m:
        return "How much does %s cost?" % m.group(1)
    m = re.match(r"^(.+?)\s+(?:cost|costs|price|prices|pricing)$", body, re.I)
    if m:
        return "How much does %s cost?" % m.group(1)
    return "What should I know about %s?" % body


def _normalise_term_row(row):
    """Accept flat term dicts or Google-Ads searchTermView rows; else None."""
    if isinstance(row, str):
        term = _norm_space(row)
        if not term or len(term) > MAX_TERM_CHARS:
            return None
        return {"term": term, "clicks": 0.0, "impressions": 0.0,
                "cost": 0.0, "conversions": 0.0}
    if not isinstance(row, dict):
        return None
    term = row.get("term")
    if term is None:
        term = _g(row, "searchTermView", "searchTerm")
    if term is None:
        term = row.get("search_term")
    if not isinstance(term, str) or not term.strip():
        return None
    term = _norm_space(term)
    if len(term) > MAX_TERM_CHARS:
        return None
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}

    def pick(*names):
        for name in names:  # flat keys win, then the metrics sub-object
            val = row.get(name)
            if val is not None:
                return _num(val)
        for name in names:
            val = metrics.get(name)
            if val is not None:
                return _num(val)
        return 0.0

    cost = pick("cost")
    if cost == 0.0:  # Google Ads money arrives in micros
        micros = row.get("costMicros")
        if micros is None:
            micros = metrics.get("costMicros")
        if micros is not None:
            cost = _num(micros) / 1_000_000.0
    return {"term": term, "clicks": pick("clicks"),
            "impressions": pick("impressions"),
            "conversions": pick("conversions"), "cost": _money(cost)}


# -------------------------------------------------------------------- public
def questions_from_terms(search_terms, min_impr=5, city=None):
    """Cluster terms into question targets; dedupe by token Jaccard >= 0.7."""
    if not isinstance(search_terms, (list, tuple)):
        raise ToolError("search_terms must be a list of term dicts")
    if len(search_terms) > MAX_TERMS:
        raise RailViolation("refusing %d terms (cap %d)" % (len(search_terms), MAX_TERMS))
    try:
        min_impr = int(min_impr)
    except (TypeError, ValueError):
        raise ToolError("min_impr must be an integer") from None
    if min_impr < 0:
        raise ToolError("min_impr must be >= 0")
    city = _norm_space(city) if isinstance(city, str) else ""
    city = city or None

    clusters = []
    for row in search_terms:
        rec = _normalise_term_row(row)
        if rec is None or rec["impressions"] < min_impr:
            continue
        if not _is_intent_rich(rec["term"]):
            continue
        question = _to_question(rec["term"], city=city)
        if not question:
            continue
        clusters.append({"question": question, "source_terms": [rec["term"]],
                         "impressions": rec["impressions"], "clicks": rec["clicks"],
                         "cost": rec["cost"], "conversions": rec["conversions"]})

    # Highest impressions first so the strongest phrasing becomes canonical.
    clusters.sort(key=lambda r: (-r["impressions"], r["question"]))
    merged, keys = [], []
    for rec in clusters:
        toks = set(_tokens(rec["question"]))
        for i, kept in enumerate(keys):
            if _jaccard(toks, kept) >= 0.7:  # near-duplicate -> merge counts
                tgt = merged[i]
                tgt["impressions"] += rec["impressions"]
                tgt["clicks"] += rec["clicks"]
                tgt["cost"] += rec["cost"]
                tgt["conversions"] += rec["conversions"]
                for st in rec["source_terms"]:
                    if st not in tgt["source_terms"]:
                        tgt["source_terms"].append(st)
                break
        else:
            merged.append(rec)
            keys.append(toks)

    for rec in merged:
        rec["impressions"] = _tidy(rec["impressions"])
        rec["clicks"] = _tidy(rec["clicks"])
        rec["conversions"] = _tidy(rec["conversions"])
        rec["cost"] = _money(rec["cost"])
    merged.sort(key=lambda r: (-r["impressions"], r["question"]))
    return merged


def _page_index(pages):
    """Pre-tokenise pages once so coverage() stays linear in practice."""
    idx = []
    if not isinstance(pages, (list, tuple)):
        return idx
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = page.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        headings = page.get("headings") or []
        if isinstance(headings, str):
            headings = [headings]
        hsets = [set(_tokens(h)) for h in headings if isinstance(h, str)]
        idx.append({"url": url.strip(), "headings": [h for h in hsets if h],
                    "text": set(_tokens(page.get("text") or ""))})
    return idx


def _page_covers(q_tokens, page, heading_min=0.6):
    """Heading overlap >= 0.6 of the question, or body holds every token."""
    qs = set(q_tokens)
    if not qs:
        return False
    need = min(2, len(qs))  # a single shared word never "covers" a longer question
    for head in page["headings"]:
        shared = qs & head
        if len(shared) >= need and len(shared) / float(len(qs)) >= heading_min:
            return True
    return qs.issubset(page["text"])


def coverage(questions, pages):
    """Attach ``covered_by`` (url or None) and split into covered/gaps."""
    if not isinstance(questions, (list, tuple)):
        raise ToolError("questions must be a list of strings or dicts")
    idx = _page_index(pages)
    covered, gaps = [], []
    for item in questions:
        if isinstance(item, str):
            rec = {"question": item}
        elif isinstance(item, dict):
            rec = dict(item)
        else:
            continue
        text = rec.get("question")
        if not isinstance(text, str) or not text.strip():
            continue
        rec["question"] = _norm_space(text)
        toks = _tokens(rec["question"])
        rec["covered_by"] = next((p["url"] for p in idx if _page_covers(toks, p)), None)
        (covered if rec["covered_by"] else gaps).append(rec)
    return {"covered": covered, "gaps": gaps}


def _brief_payload(question, terms=None, site_facts=None):
    """Build the structured AEO brief payload. Never invents numbers."""
    if not isinstance(question, str) or not question.strip():
        raise ToolError("question must be a non-empty string")
    clean = _norm_space(question)
    if len(clean) > MAX_QUESTION_CHARS:
        raise ToolError(scrub("question too long: " + clean[:120]))
    target = clean.strip(" ?")
    if not target:
        raise ToolError("question must contain words, not just punctuation")
    target += "?"

    # Suggested H2s come only from the related terms we were handed.
    h2s, seen = [], [set(_tokens(target))]
    for entry in terms if isinstance(terms, (list, tuple)) else []:
        term = entry.get("term") if isinstance(entry, dict) else entry
        if not isinstance(term, str) or not term.strip():
            continue
        cand = _to_question(term)
        if not cand:
            continue
        toks = set(_tokens(cand))
        if any(_jaccard(toks, s) >= 0.7 for s in seen):
            continue
        seen.append(toks)
        h2s.append(cand)
        if len(h2s) >= MAX_H2:
            break

    # Facts: only what the caller supplied, flattened to short strings.
    facts = []
    if isinstance(site_facts, dict):
        for key in sorted(site_facts):
            value = site_facts[key]
            if value is None or isinstance(value, dict):
                continue
            if isinstance(value, (list, tuple)):
                text = ", ".join(str(v) for v in value if v is not None)
            elif isinstance(value, bool):
                text = "yes" if value else "no"
            else:
                text = str(value)
            text = _norm_space(text)
            if text:
                facts.append({"key": _norm_space(str(key))[:80], "value": text[:300]})

    faq = {"@context": "https://schema.org", "@type": "FAQPage",
           "mainEntity": [{"@type": "Question", "name": q,
                           "acceptedAnswer": {"@type": "Answer", "text": ""}}
                          for q in [target] + h2s]}

    lines = ["# Content brief: " + target, "", "## Target question", target, "",
             "## Answer slot (40-60 words)",
             "> Write a direct, factual answer of 40-60 words. Lead with the answer,",
             "> cite only the facts below, never invent figures, prices or dates.",
             "", "## Suggested H2 questions"]
    if h2s:
        lines.extend("%d. %s" % (i + 1, q) for i, q in enumerate(h2s))
    else:
        lines.append("- (no related terms supplied; derive H2s from the SERP)")
    lines.extend(["", "## Facts to cite"])
    if facts:
        lines.extend("- **%s**: %s" % (f["key"], f["value"]) for f in facts)
    else:
        lines.append("- (no site facts supplied; do not invent numbers)")
    lines.extend(["", "## FAQ JSON-LD skeleton", ""])
    lines.extend("    " + ln for ln in json.dumps(faq, indent=2).splitlines())
    lines.extend(["", "## Internal links",
                  "- TODO: link to the 2-3 closest money pages and the parent hub."])

    return {"question": target, "answer_slot": "40-60 words", "h2s": h2s,
            "facts": facts, "faq_jsonld": faq, "internal_links": [],
            "markdown": "\n".join(lines)}


def brief(question, terms=None, site_facts=None):
    """Return a markdown AEO content brief. Never invents numbers."""
    return _brief_payload(question, terms=terms, site_facts=site_facts)["markdown"]


def roadmap(search_terms, pages, top=10, site_facts=None):
    """Question clusters -> coverage -> top-N gap briefs."""
    try:
        top = int(top)
    except (TypeError, ValueError):
        raise ToolError("top must be an integer") from None
    if top < 1:
        raise ToolError("top must be >= 1")
    if top > MAX_TOP:
        raise RailViolation("refusing top=%d (cap %d)" % (top, MAX_TOP))

    questions = questions_from_terms(search_terms)
    cov = coverage(questions, pages)
    gaps = []
    for gap in cov["gaps"][:top]:
        b = _brief_payload(gap["question"], terms=gap.get("source_terms"),
                           site_facts=site_facts)
        rec = dict(gap)
        rec.pop("covered_by", None)
        rec["h2s"] = b["h2s"]
        rec["faq_jsonld"] = b["faq_jsonld"]
        rec["brief"] = b["markdown"]
        gaps.append(rec)
    return {"gaps": gaps, "covered_count": len(cov["covered"]),
            "gap_count": len(cov["gaps"])}


# ------------------------------------------------------------------ selftest
def selftest() -> int:
    checks = 0

    def ok(cond, msg="check failed"):
        nonlocal checks
        if not cond:
            raise AssertionError(msg)
        checks += 1

    # -- primitives
    ok(_g({"a": {"b": 1}}, "a", "b") == 1, "_g nested")
    ok(_g({}, "a", default=7) == 7, "_g default")
    ok(_g({"a": [1, 2]}, "a", 5, default="x") == "x", "_g out of range")
    ok(_num("2,500") == 2500.0 and _num("junk") == 0.0 and _num(None) == 0.0, "_num")
    ok(_num(True) == 0.0 and _num(float("inf")) == 0.0, "_num guards")
    ok(_money("2.5") == 2.5 and _money(-0.0) == 0.0, "_money")
    ok(_tokens("Property Management Costs!") == ["property", "management", "cost"], "_tokens")
    ok(_tokens(None) == [] and _jaccard([], [1]) == 0.0, "_tokens/jaccard empty")

    # -- question templating
    ok(_to_question("best property management") == "What is the best property management?", "best")
    ok(_to_question("x vs y") == "x vs y: which is better?", "vs")
    ok(_to_question("plumber near me", city="Tampa") == "Who offers plumber near Tampa?", "near+city")
    ok(_to_question("plumber near me") == "Who offers plumber near me?", "near")
    ok(_to_question("how do i pick a plumber") == "How do i pick a plumber?", "already-question")
    ok(_to_question("price of widgets") == "How much does widgets cost?", "price-of")
    ok(_to_question("") == "", "empty term")

    # -- row normalisation (Google Ads micros)
    rec = _normalise_term_row({
        "searchTermView": {"searchTerm": "how much does property management cost"},
        "metrics": {"impressions": "120", "clicks": "7", "costMicros": "2500000",
                    "conversions": "1.5"}})
    ok(rec["cost"] == 2.5, "micros -> units")
    ok(rec["clicks"] == 7.0 and rec["conversions"] == 1.5, "string metrics coerced")
    ok(_normalise_term_row(42) is None and _normalise_term_row({"term": ""}) is None, "bad rows")

    # -- clustering
    terms = [
        {"term": "property management cost", "impressions": 100, "clicks": 10,
         "cost": 12.5, "conversions": 2},
        {"term": "how much does property management cost", "impressions": 40, "clicks": 4,
         "cost": 3.0, "conversions": 1},
        {"term": "best property management tampa", "impressions": 60, "clicks": 6,
         "cost": 5.0, "conversions": 0},
        {"term": "plumber near me", "impressions": 30, "clicks": 3,
         "cost": 1.0, "conversions": 0},
        {"term": "cheap widgets", "impressions": 500, "clicks": 50, "cost": 9.0},  # no trigger
        {"term": "price of widgets", "impressions": 2, "clicks": 0, "cost": 0.0},  # below min
    ]
    qs = questions_from_terms(terms, min_impr=5)
    ok(len(qs) == 3, "3 clusters, got %d" % len(qs))
    ok(qs[0]["question"] == "How much does property management cost?", "top cluster")
    ok(qs[0]["impressions"] == 140, "dedupe merged impressions")
    ok(qs[0]["cost"] == 15.5 and qs[0]["clicks"] == 14, "dedupe merged money/clicks")
    ok(len(qs[0]["source_terms"]) == 2, "merged source terms")
    ok(all("widget" not in r["question"] for r in qs), "non-intent term dropped")
    ok(qs == sorted(qs, key=lambda r: (-r["impressions"], r["question"])), "sorted by impressions")
    ok(len(questions_from_terms(terms, min_impr=0)) == 4, "min_impr=0 widens")
    ok(questions_from_terms([]) == [], "empty terms")
    ok(questions_from_terms([{"term": "cost near me", "impressions": 9}], city="Tampa")[0]
       ["question"] == "Who offers cost near Tampa?", "city passed through")

    try:
        questions_from_terms("nope")
        raise AssertionError("expected ToolError for non-list search_terms")
    except ToolError:
        checks += 1
    try:
        questions_from_terms([], -1)
        raise AssertionError("expected ToolError for negative min_impr")
    except ToolError:
        checks += 1
    try:
        questions_from_terms([{"term": "x"}] * (MAX_TERMS + 1))
        raise AssertionError("expected RailViolation")
    except RailViolation:
        checks += 1

    # -- coverage
    pages = [
        {"url": " https://ex.com/pm-cost ",
         "headings": ["How much does property management cost?"], "text": ""},
        {"url": "https://ex.com/blog", "headings": ["Company news"],
         "text": "we sell widgets and gadgets"},
    ]
    cov = coverage(qs, pages)
    ok(cov["covered"][0]["covered_by"] == "https://ex.com/pm-cost", "covered by heading")
    ok(len(cov["covered"]) == 1 and len(cov["gaps"]) == 2, "1 covered / 2 gaps")
    # 2 of 4 question tokens under a heading = 0.5 overlap -> below the 0.6 floor.
    weak = coverage(["How much does property management cost in Tampa?"],
                    [{"url": "u", "headings": ["Property management"], "text": ""}])
    ok(weak["gaps"] and not weak["covered"], "0.6 heading threshold enforced")
    body = coverage(["How much does property management cost?"],
                    [{"url": "u2", "headings": [], "text": "how much does property "
                                                            "management cost in tampa"}])
    ok(body["covered"][0]["covered_by"] == "u2", "text superset covers")
    ok(len(coverage([123, {"question": "  "}, "ok question here"], None)["gaps"]) == 1,
       "malformed entries skipped, bad pages tolerated")

    # -- brief
    qtext = "How much does property management cost in Tampa?"
    brief_terms = ["property management fees", "best property management tampa",
                   "property management cost"]
    brief_facts = {"avg_price": 120, "licensed": True,
                   "extra": {"nested": 1}, "empty": None}
    b = _brief_payload(qtext, terms=brief_terms, site_facts=brief_facts)
    md = brief(qtext, terms=brief_terms, site_facts=brief_facts)
    ok(b["question"] == qtext, "brief target")
    ok("40-60" in md, "answer slot present")
    ok(len(b["h2s"]) == 2 and b["h2s"][0].startswith("What should I know"), "h2 dedupe vs target")
    ok([f["key"] for f in b["facts"]] == ["avg_price", "licensed"], "facts only supplied")
    ok("nested" not in md and "+120" not in md, "no invented facts")
    ok("**licensed**: yes" in md, "bool fact flattened")
    ok(len(b["faq_jsonld"]["mainEntity"]) == 3, "faq has target + h2s")
    ok(json.loads(json.dumps(b))["faq_jsonld"]["@type"] == "FAQPage", "brief JSON-safe")
    ok("no site facts supplied" in brief("who offers plumber near me?"),
       "empty-facts warning")
    try:
        brief("   ")
        raise AssertionError("expected ToolError")
    except ToolError:
        checks += 1
    try:
        brief("x" * (MAX_QUESTION_CHARS + 1))
        raise AssertionError("expected ToolError")
    except ToolError:
        checks += 1

    # -- roadmap
    rm = roadmap(terms, pages, top=2, site_facts={"avg_price": 120})
    ok(rm["covered_count"] == 1 and rm["gap_count"] == 2, "roadmap counts")
    ok(len(rm["gaps"]) == 2 and "brief" in rm["gaps"][0], "roadmap briefs attached")
    ok("**avg_price**: 120" in rm["gaps"][0]["brief"], "site facts flow into roadmap")
    ok(len(roadmap(terms, pages, top=1)["gaps"]) == 1, "top limit honoured")
    ok(roadmap([], []) == {"gaps": [], "covered_count": 0, "gap_count": 0}, "empty roadmap")
    ok(json.loads(json.dumps(rm))["gap_count"] == 2, "roadmap JSON-safe")
    try:
        roadmap(terms, pages, top=0)
        raise AssertionError("expected ToolError")
    except ToolError:
        checks += 1
    try:
        roadmap(terms, pages, top=MAX_TOP + 1)
        raise AssertionError("expected RailViolation")
    except RailViolation:
        checks += 1

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)