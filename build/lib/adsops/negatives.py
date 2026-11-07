"""adsops_negatives_suggest core: classify search terms and propose PHRASE negatives. Read-only, no network here.

Rules are deterministic (word lists below plus the tenant's own lists). An optional local hook, `llm_command` (an argv
list you control, e.g. a script that calls a local model), may classify the leftovers. Nothing paid is called by
default, and anything the hook proposes is marked `needs_review` and still goes through the same protect and
coverage filters.

Negative-keyword matching mirrors how the live list works (close variants are NOT applied to negatives, so
'apartments' does not cover 'apartment'): PHRASE blocks a query containing those words contiguously and in order,
BROAD blocks a query containing all the words in any order, EXACT blocks only the identical query.
"""
import datetime as dt
import json
import re
import subprocess

from adsops.errors import ToolError

_TOK = re.compile(r"[a-z0-9']+")


def toks(text):
    return _TOK.findall(str(text or "").lower().replace("'", ""))


def parse_negative(text, match_type="PHRASE"):
    """'[foo bar]' -> EXACT, '"foo bar"' -> PHRASE, else the given type. -> (tokens, match_type)"""
    t = str(text or "").strip()
    if t.startswith("[") and t.endswith("]"):
        return toks(t), "EXACT"
    if t.startswith('"') and t.endswith('"'):
        return toks(t), "PHRASE"
    return toks(t), (match_type or "PHRASE").upper()


def _contig(needle, hay):
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def blocks(neg, query_tokens):
    """Does negative `neg` = (tokens, match_type) stop an ad for this query?"""
    nt, mt = neg
    if not nt:
        return False
    if mt == "EXACT":
        return nt == query_tokens
    if mt == "BROAD":
        return all(t in query_tokens for t in nt)
    return _contig(nt, query_tokens)


def coverage(negs, query_tokens):
    """First existing negative that blocks the query, as 'text (TYPE)', else None."""
    for nt, mt in negs:
        if blocks((nt, mt), query_tokens):
            return " ".join(nt) + f" ({mt})"
    return None


# ── deterministic lexicons: class -> (whole-word/phrase triggers, stems matched inside a token) ─────────────────
LEX = {
    "renter": (["renter", "renters", "apartment", "apartments", "for rent", "rentals", "rental agency", "rental agencies",
                "rental agents", "tenant portal", "pay rent", "portal", "locator", "locators", "roommate", "roommates",
                "section 8", "low income"], []),
    "job": (["job", "jobs", "career", "careers", "salary", "salaries", "hiring", "employment", "internship",
             "how to become", "course", "courses", "certification", "license exam", "training"], []),
    "hoa_condo": (["hoa", "hoa management", "condo association", "homeowners association", "community association",
                   "association management", "condo", "condos", "condominium"], ["condominium"]),
    "vacation": (["vacation", "airbnb", "vrbo", "short term", "short-term", "str management", "turnkey"], []),
    "diy_software": (["software", "app", "template", "templates", "spreadsheet", "excel", "lease agreement",
                      "how to", "diy", "tutorial", "pdf"], []),
}
CLASS_LABEL = {"renter": "renter intent", "job": "job / training intent", "hoa_condo": "HOA / condo-association",
               "vacation": "vacation / short-term rental", "diy_software": "DIY / software",
               "out_of_area": "outside the service area", "competitor": "competitor brand", "llm": "local-model call"}
US_STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
             "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
             "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
             "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
             "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee",
             "texas", "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming"]


def _find_phrase(phrase_tokens, q):
    return _contig(phrase_tokens, q)


def _lexicon_hits(q, qtext):
    """-> [(class, candidate phrase)] for one query. Whole-word matches first; stems match inside a token."""
    hits = []
    for cls, (words, stems) in LEX.items():
        for w in words:
            wt = toks(w)
            if _find_phrase(wt, q):
                hits.append((cls, " ".join(wt)))
        for s in stems:
            if any(s in t for t in q) and not any(h == (cls, s) or h[0] == cls and s in h[1] for h in hits):
                hits.append((cls, s))
    return hits


def _list_hits(q, items, cls):
    out = []
    for it in items or []:
        it_t = toks(it)
        if it_t and _find_phrase(it_t, q):
            out.append((cls, " ".join(it_t)))
    return out


def _protected(candidate_tokens, protect):
    """A candidate is refused if it is, or sits inside, a protected phrase (brand or core service word): such a
    negative would block the protected term itself. A longer phrase that merely contains one ('acme texas') is fine."""
    for p in protect or []:
        pt = toks(p)
        if pt and _contig(candidate_tokens, pt):
            return p
    return None


def _prefer(hits):
    """Longest, most specific phrase per class first; drop a hit whose tokens sit inside a longer hit."""
    hits = sorted(set(hits), key=lambda h: (-len(h[1]), h[1]))
    kept = []
    for cls, ph in hits:
        pt = toks(ph)
        if any(_contig(pt, toks(k)) and pt != toks(k) for _, k in kept):
            continue
        kept.append((cls, ph))
    return kept


def classify(rows, existing, settings, llm=None):
    """rows: [{term, matched, clicks, cost}]; existing: [(text, match_type)]; settings: dict (see README).
    -> dict with suggestions / already_covered / refused_protected / relevant / unclear."""
    settings = settings or {}
    negs = [parse_negative(t, m) for t, m in existing]
    protect = list(settings.get("protect") or [])
    out_of_area = list(settings.get("out_of_area") or [])
    home = (settings.get("home_state") or "").lower()
    state_names = [s for s in US_STATES if s != {"fl": "florida"}.get(home, home)]
    brands = list(settings.get("competitor_brands") or [])
    agg, covered, refused, relevant, unclear = {}, [], [], [], []

    def ev(r):
        return {"term": r["term"], "clicks": int(r.get("clicks", 0)), "cost": round(float(r.get("cost", 0.0)), 2)}

    for r in rows:
        q = toks(r["term"])
        if not q:
            continue
        cov = coverage(negs, q)
        if cov:
            covered.append({**ev(r), "covered_by": cov})
            continue
        hits = _lexicon_hits(q, r["term"]) + _list_hits(q, out_of_area, "out_of_area") \
            + _list_hits(q, state_names, "out_of_area") + _list_hits(q, brands, "competitor")
        hits = _prefer(hits)
        used = False
        for cls, ph in hits:
            pt = toks(ph)
            hit_p = _protected(pt, protect)
            if hit_p:
                refused.append({"phrase": ph, "class": cls, "protected_by": hit_p, **ev(r)})
                continue
            cc = coverage(negs, pt)
            if cc:      # the phrase is already negated; this term only slipped past because the stem is glued into a token
                covered.append({**ev(r), "covered_by": cc, "note": "stem already negated; this term is one glued token"})
                used = True
                continue
            a = agg.setdefault(ph, {"phrase": ph, "match_type": "PHRASE", "class": cls, "source": "rule",
                                    "clicks": 0, "cost": 0.0, "terms": [], "literal_hit": False})
            a["clicks"] += ev(r)["clicks"]
            a["cost"] = round(a["cost"] + ev(r)["cost"], 2)
            a["terms"].append(ev(r))
            a["literal_hit"] = a["literal_hit"] or _contig(pt, q)
            used = True
        if used:
            continue
        if any(_find_phrase(toks(p), q) for p in protect):
            relevant.append(ev(r))
        else:
            unclear.append(ev(r))
    if llm and unclear:
        keep = []
        by_term = {u["term"]: u for u in unclear}
        for sug in llm([u for u in unclear if u["clicks"] > 0 or u["cost"] > 0]):
            u = by_term.get(sug.get("term"))
            ph = " ".join(toks(sug.get("phrase")))
            if not u or not ph or not _contig(toks(ph), toks(u["term"])):
                continue            # the hook may only propose a phrase that literally appears in the term
            if coverage(negs, toks(ph)) or _protected(toks(ph), protect):
                continue
            a = agg.setdefault(ph, {"phrase": ph, "match_type": "PHRASE", "class": "llm", "source": "llm",
                                    "clicks": 0, "cost": 0.0, "terms": [], "literal_hit": True, "needs_review": True})
            a["clicks"] += u["clicks"]
            a["cost"] = round(a["cost"] + u["cost"], 2)
            a["terms"].append(u)
            keep.append(u["term"])
        unclear = [u for u in unclear if u["term"] not in keep]
    sugg = sorted(agg.values(), key=lambda s: (-s["cost"], -s["clicks"], s["phrase"]))
    for s in sugg:
        s["class_label"] = CLASS_LABEL[s["class"]]
        s["blocks_evidence_terms"] = s.pop("literal_hit")
    return {"suggestions": sugg, "already_covered": covered, "refused_protected": refused,
            "relevant": relevant, "unclear": sorted(unclear, key=lambda u: -u["cost"])}


def run_llm_command(argv, timeout=60):
    """Optional hook: argv gets [{term,clicks,cost}] as JSON on stdin and prints [{term,phrase}] JSON. Local only;
    the command is yours (config `negatives.llm_command`), nothing is called when it is unset."""
    if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ToolError("negatives.llm_command must be a list of strings")

    def hook(items):
        try:
            p = subprocess.run(argv, input=json.dumps(items), capture_output=True, text=True, timeout=timeout)
            data = json.loads(p.stdout) if p.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    return hook


def basis_note(result, days, today=None):
    today = today or dt.date.today().isoformat()
    s = result["suggestions"]
    if not s:
        return f"Search terms, last {days} days (adsops_negatives_suggest {today}): no new negatives suggested."
    bits = []
    for x in s:
        top = ", ".join(f"'{t['term']}' ${t['cost']:.2f}/{t['clicks']} clicks" for t in x["terms"][:2])
        bits.append(f"{x['class_label']}: '{x['phrase']}' ({top})")
    return (f"Search terms, last {days} days (adsops_negatives_suggest {today}): " + "; ".join(bits) +
            f". {len(result['already_covered'])} terms were already blocked by existing negatives and "
            f"{len(result['refused_protected'])} candidates were refused as protected words.")


def patch_for(result):
    return {"negatives_add": [{"text": s["phrase"], "match_type": "PHRASE"} for s in result["suggestions"]]}


def suggest(rows, existing, settings, days, llm=None, today=None):
    res = classify(rows, existing, settings, llm)
    res["negatives_add_patch"] = patch_for(res)
    key = "_negatives_basis_" + (today or dt.date.today().isoformat()).replace("-", "_")
    res["basis_note"] = {key: basis_note(res, days, today)}
    res["next_step"] = ("Review, paste negatives_add_patch entries into negatives_add of the desired-state file and the "
                        "basis note beside it, commit the file, then adsops_plan -> adsops_apply. This tool changes nothing.")
    return res


def selftest():
    import os
    n = 0
    fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "acme_negatives_2026_11_01.json")))
    existing = [(t, m) for t, m in fx["existing"]]
    res = suggest(fx["terms"], existing, fx["settings"], 30, today="2026-11-01")
    got = [s["phrase"] for s in res["suggestions"]]
    assert sorted(got) == sorted(fx["expected_new"]) and len(got) == 6, got; n += 1
    assert res["negatives_add_patch"]["negatives_add"][0] == {"text": "condominium", "match_type": "PHRASE"}; n += 1
    assert got[0] == "condominium" and res["suggestions"][0]["cost"] == 11.74 and res["suggestions"][0]["clicks"] == 2; n += 1
    assert res["suggestions"][0]["blocks_evidence_terms"] is False   # 'condominiumassociates' is one token: say so
    n += 1
    cov = {c["term"]: c["covered_by"] for c in res["already_covered"]}
    assert "locators (PHRASE)" == cov["tampa locators"] and "airbnb (PHRASE)" == cov["airbnb property management tampa"]; n += 1
    assert "property management jobs tampa" in cov and "yale property management tampa" in cov; n += 1
    assert {r["term"] for r in res["relevant"]} >= {"property management tampa", "flat fee property management tampa",
                                                    "acme property management"}; n += 1
    assert [u["term"] for u in res["unclear"]] == ["how much does a lawn guy cost"]; n += 1
    # re-running with today's six added suggests nothing (idempotent)
    after = existing + [(p, "PHRASE") for p in fx["expected_new"]]
    again = suggest(fx["terms"], after, fx["settings"], 30, today="2026-11-01")
    assert again["suggestions"] == [] and "no new negatives" in again["basis_note"]["_negatives_basis_2026_11_01"]; n += 1
    # matching semantics: close variants are not applied; BROAD is order-free; EXACT is exact only
    assert blocks((["apartment"], "PHRASE"), toks("tampa apartments")) is False
    assert blocks((["rent", "tampa"], "BROAD"), toks("tampa for rent")) and not blocks((["tampa"], "EXACT"), toks("tampa fl"))
    assert parse_negative("[free quote]") == (["free", "quote"], "EXACT"); n += 1
    # protect list: never suggest brand or core service words, even if a rule fires on them
    r2 = classify([{"term": "acme tx property management", "clicks": 1, "cost": 2.0}], [],
                  {"protect": ["acme", "property management"], "out_of_area": ["acme tx", "acme"]})
    assert [s["phrase"] for s in r2["suggestions"]] == ["acme tx"] and r2["refused_protected"] == []; n += 1
    r3 = classify([{"term": "property management tampa jobs", "clicks": 1, "cost": 2.0}], [], {"protect": ["tampa", "jobs"]})
    assert r3["suggestions"] == [] and r3["refused_protected"][0]["protected_by"] == "jobs"; n += 1
    r4 = classify([{"term": "tampa property management", "clicks": 1, "cost": 2.0}], [],
                  {"protect": ["property management"], "competitor_brands": ["management"]})
    assert r4["suggestions"] == [] and r4["refused_protected"]; n += 1
    # out-of-state: other states flagged, the home state is not
    r5 = classify([{"term": "property management ohio", "clicks": 1, "cost": 1.0},
                   {"term": "property management florida", "clicks": 1, "cost": 1.0}], [], {"home_state": "fl"})
    assert [s["phrase"] for s in r5["suggestions"]] == ["ohio"]; n += 1
    # LLM hook: only phrases that literally appear in an unclassified term; protect + coverage still apply
    calls = []

    def llm(items):
        calls.append(items)
        return [{"term": "lawn guy cost", "phrase": "lawn"}, {"term": "how much does a lawn guy cost", "phrase": "lawn"},
                {"term": "how much does a lawn guy cost", "phrase": "tampa"}, {"term": "ghost", "phrase": "ghost"}]
    r6 = classify(fx["terms"], existing, fx["settings"], llm)
    lg = [s for s in r6["suggestions"] if s["source"] == "llm"]
    assert [s["phrase"] for s in lg] == ["lawn"] and lg[0]["needs_review"] and r6["unclear"] == []; n += 1
    assert len(calls) == 1 and all(c["clicks"] > 0 or c["cost"] > 0 for c in calls[0]); n += 1
    assert run_llm_command(["/definitely/not/a/command"])([{"term": "x"}]) == []; n += 1   # a dead hook degrades to nothing
    try:
        run_llm_command("echo hi")
        raise AssertionError("string argv accepted")
    except ToolError:
        n += 1
    return n


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        print(f"negatives: {selftest()} checks")
        print("ALL PASS")
