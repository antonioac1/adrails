"""adsops.aeo.visibility — AI "share of answer" tracking for guarded ad ops.

Answers whether assistants mention / cite the brand for the prompts that matter.
No network of its own: every model call goes through an injected
``ask(engine, prompt) -> {"text": str, "citations": [urls]}`` callable, so the
module is fully mockable and import-time effect free. Ask failures never abort a
panel; they are recorded as redacted errors. All functions are pure and return
JSON-serialisable dicts/lists (no sets, no datetimes). Ranks are 1-based from
first-mention offsets; snippets are 200-char windows. A tool calls ``run_panel``
then ``score`` (optionally ``diff`` vs ``load_history`` and ``store``).
"""

import json
import re
import urllib.parse

from adsops.errors import ToolError, RailViolation, scrub

# Lexicons for a cheap, deterministic sentiment read of the mention snippet.
_POSITIVE = ("recommend", "best", "great", "excellent", "leading", "reliable",
             "powerful", "love", "strong", "good", "top")
_NEGATIVE = ("avoid", "expensive", "limited", "doesn't", "doesnt", "does not",
             "poor", "weak", "bad", "lacking", "complicated", "slow", "concern")

# Belt-and-braces redaction: adsops.errors.scrub may only catch known formats,
# so we also strip obvious token shapes before it ever leaves the process.
_SECRET_PAT = re.compile(
    r"(sk-[a-z0-9_\-]{6,}"
    r"|gh[pousr]_[a-z0-9]{10,}"
    r"|xox[abprs]-[a-z0-9\-]{8,}"
    r"|akia[0-9a-z]{12,}"
    r"|aiza[0-9a-z_\-]{20,}"
    r"|bearer\s+[a-z0-9._\-]{8,}"
    r"|(?:api|access|secret|auth|session)[_-]?(?:key|token)\s*[=:]\s*\S{6,}"
    r"|[a-f0-9]{32,})",
    re.IGNORECASE,
)

_MAX_REPEATS = 20        # rail: repeats per (engine, prompt)
_MAX_CALLS = 600         # rail: total model calls in one panel


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Nested get: _g(resp, "a", "b") -> resp["a"]["b"] or default if absent."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def _redact(text):
    """Strip token-shaped substrings; never raises, always returns a str."""
    try:
        s = str(text)
    except Exception:
        return "error"
    return _SECRET_PAT.sub("[REDACTED]", s)


def _safe_err(text, limit=300):
    """Redact + scrubs (defensively) an error string for safe surfacing."""
    s = _redact(text)
    try:
        s = scrub(s, limit=limit)
    except Exception:
        s = s[:limit]
    return s if isinstance(s, str) else str(s)[:limit]


def _pattern(terms):
    """Compile a case-insensitive, whole-token alternation of `terms` (or None)."""
    parts = []
    for t in terms or []:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if len(t) < 2:          # single chars would match everywhere
            continue
        parts.append(re.escape(t))
    if not parts:
        return None
    parts.sort(key=len, reverse=True)  # longest alternative first
    body = "|".join(parts)
    # Lookarounds instead of \b so dotted domains ("adrails.dev") match cleanly.
    return re.compile(r"(?<![0-9a-z])(?:" + body + r")(?![0-9a-z])", re.IGNORECASE)


def _first_index(pat, text):
    if pat is None or not text:
        return None
    m = pat.search(text)
    return m.start() if m else None


def _host(url):
    """Best-effort hostname of a citation URL (handles scheme-less values)."""
    try:
        s = str(url).strip()
        if "//" not in s:
            s = "//" + s
        return (urllib.parse.urlsplit(s).hostname or "").lower()
    except Exception:
        return ""


def _is_brand_host(host, domains):
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _brand_terms(brand):
    terms = []
    v = brand.get("name")
    if isinstance(v, str) and v.strip():
        terms.append(v.strip())
    for key in ("aliases", "domains"):
        for item in brand.get(key) or []:
            if isinstance(item, str) and item.strip():
                terms.append(item.strip())
    return terms


def _brand_domains(brand):
    doms = []
    for item in brand.get("domains") or []:
        if isinstance(item, str) and item.strip():
            doms.append(item.strip().lower())
    for item in brand.get("aliases") or []:      # alias that looks like a domain
        if isinstance(item, str) and "." in item and " " not in item.strip():
            doms.append(item.strip().lower())
    out = []
    for d in doms:
        d = d.lstrip(".").strip()
        if d and d not in out:
            out.append(d)
    return out


def _sentiment(snippet):
    low = snippet.lower()
    pos = sum(low.count(w) for w in _POSITIVE)
    neg = sum(low.count(w) for w in _NEGATIVE)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _num(x):
    """float for real numbers, else None (bools are not numbers here)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _analyse(text, citations, bpat, domains, comp_pats):
    """Turn one raw answer into the per-run result fields."""
    bidx = _first_index(bpat, text)
    hits = []
    for name, pat in comp_pats:
        i = _first_index(pat, text)
        if i is not None:
            hits.append((i, name))
    hits.sort()

    if bidx is None:
        position = None
        snippet = text[:200].strip()      # fallback window for gap analysis
    else:
        # Rank of our first mention among all brand+competitor mentions.
        position = 1 + sum(1 for i, _ in hits if i < bidx)
        start = max(0, bidx - 80)
        snippet = text[start:start + 200].strip()

    cited = any(_is_brand_host(_host(u), domains) for u in citations)
    return {
        "mentioned": bidx is not None,
        "position": position,
        "cited": cited,
        "competitors_mentioned": [name for _, name in hits],
        "sentiment": _sentiment(snippet),
        "snippet": snippet,
    }


def _strings(value, field):
    if not isinstance(value, (list, tuple)):
        raise ToolError(f"{field} must be a list of non-empty strings")
    out = []
    for v in value:
        if not isinstance(v, str) or not v.strip():
            raise ToolError(f"{field} must contain non-empty strings")
        out.append(v.strip())
    if not out:
        raise ToolError(f"{field} must not be empty")
    return out


# --------------------------------------------------------------------------- #
# panel
# --------------------------------------------------------------------------- #
def run_panel(prompts, engines, ask, brand, competitors, repeats=1, sleep=None):
    """Ask every engine `repeats` times per prompt and analyse each answer.

    `ask(engine, prompt)` is the only external effect; exceptions it raises are
    captured into `errors` (redacted) and the panel continues. `sleep()` is
    called between calls when supplied (caller-side rate limiting).
    """
    prompts = _strings(prompts, "prompts")
    engines = _strings(engines, "engines")
    if not callable(ask):
        raise ToolError("ask must be a callable (engine, prompt) -> dict")
    try:
        reps = int(repeats)
    except (TypeError, ValueError):
        raise ToolError("repeats must be an integer")
    if isinstance(repeats, bool) or reps < 1:
        raise ToolError("repeats must be >= 1")
    if reps > _MAX_REPEATS:
        raise RailViolation(f"repeats above rail limit ({_MAX_REPEATS})")
    calls = len(prompts) * len(engines) * reps
    if calls > _MAX_CALLS:
        raise RailViolation(f"panel of {calls} model calls exceeds rail limit ({_MAX_CALLS})")

    if not isinstance(brand, dict):
        raise ToolError("brand must be a dict")
    bpat = _pattern(_brand_terms(brand))
    if bpat is None:
        raise ToolError("brand must define name, aliases or domains")
    domains = _brand_domains(brand)

    comp_pats = []
    if competitors:
        if not isinstance(competitors, dict):
            raise ToolError("competitors must be a dict name -> [terms]")
        for name, terms in competitors.items():
            if isinstance(terms, str):
                terms = [terms]
            elif not isinstance(terms, (list, tuple)):
                terms = []
            # Match the competitor's display name plus any aliases/domains.
            name_s = str(name).strip() if name is not None else ""
            tl = [name_s] if name_s else []
            for t in terms:
                if isinstance(t, str) and t.strip():
                    tl.append(t.strip())
            pat = _pattern(tl)
            if pat is not None:
                comp_pats.append((name_s or str(name), pat))

    results, errors, first = [], [], True
    for engine in engines:
        for prompt in prompts:
            for run in range(1, reps + 1):
                if not first and callable(sleep):
                    try:
                        sleep()
                    except Exception:
                        pass
                first = False
                try:
                    resp = ask(engine, prompt)
                except Exception as exc:      # noqa: BLE001 - panel must survive
                    msg = str(exc) or exc.__class__.__name__
                    errors.append({"engine": engine, "prompt": prompt, "run": run,
                                   "error": _safe_err(msg)})
                    continue

                text = _g(resp, "text", default="")
                if not isinstance(text, str):
                    text = "" if text is None else str(text)
                cits = _g(resp, "citations", default=[])
                if isinstance(cits, str):
                    cits = [cits]
                elif not isinstance(cits, (list, tuple)):
                    cits = []
                cits = [c for c in cits if isinstance(c, str)]

                row = {"engine": engine, "prompt": prompt, "run": run}
                row.update(_analyse(text, cits, bpat, domains, comp_pats))
                results.append(row)
    return {"results": results, "errors": errors}


# --------------------------------------------------------------------------- #
# scoring / diffing
# --------------------------------------------------------------------------- #
def _summarise(sub):
    n = len(sub)
    if not n:
        return {"runs": 0, "share_of_answer": 0.0, "citation_rate": 0.0, "avg_position": None}
    mentioned = sum(1 for r in sub if r.get("mentioned"))
    cited = sum(1 for r in sub if r.get("cited"))
    pos = [_num(r.get("position")) for r in sub]
    pos = [p for p in pos if p is not None]
    return {
        "runs": n,
        "share_of_answer": mentioned / n,
        "citation_rate": cited / n,
        "avg_position": round(sum(pos) / len(pos), 2) if pos else None,
    }


def score(results):
    """Aggregate panel rows into rates, ranks, per-engine/prompt splits and gaps."""
    rows = [r for r in (results or []) if isinstance(r, dict) and "mentioned" in r]
    out = _summarise(rows)
    out["total_runs"] = len(rows)

    by_engine, by_prompt = {}, {}
    for r in rows:
        by_engine.setdefault(str(r.get("engine") or ""), []).append(r)
        by_prompt.setdefault(str(r.get("prompt") or ""), []).append(r)
    out["by_engine"] = {k: _summarise(v) for k, v in by_engine.items()}
    out["by_prompt"] = {k: _summarise(v) for k, v in by_prompt.items()}

    total = len(rows)
    comp_share = {}
    for r in rows:
        for c in r.get("competitors_mentioned") or []:
            if isinstance(c, str) and c:
                comp_share[c] = comp_share.get(c, 0) + 1
    out["competitor_share"] = {
        k: v / total for k, v in sorted(comp_share.items())
    } if total else {}

    # Gap prompts: competitors appear for a prompt but we never appear for it.
    we_won, gap = {}, {}
    for r in rows:
        p = str(r.get("prompt") or "")
        if r.get("mentioned"):
            we_won[p] = True
        else:
            we_won.setdefault(p, False)
        bucket = gap.setdefault(p, set())
        for c in r.get("competitors_mentioned") or []:
            if isinstance(c, str) and c:
                bucket.add(c)
    gaps = [(p, len(cs)) for p, cs in gap.items() if cs and not we_won.get(p)]
    gaps.sort(key=lambda kv: (-kv[1], kv[0]))
    out["top_gap_prompts"] = [p for p, _ in gaps]
    return out


def diff(prev_score, cur_score):
    """Human-readable movement lines between two `score()` outputs."""
    prev = prev_score if isinstance(prev_score, dict) else {}
    cur = cur_score if isinstance(cur_score, dict) else {}
    if not prev:
        return ["no previous run to compare"]

    def pts(a, b):
        # Convert a fraction delta to rounded percentage points.
        return int(round(((_num(b) or 0.0) - (_num(a) or 0.0)) * 100))

    out = []
    d = pts(prev.get("share_of_answer"), cur.get("share_of_answer"))
    if d:
        out.append(f"share of answer {'up' if d > 0 else 'down'} {abs(d)} pts")

    pe = prev.get("by_engine") or {}
    ce = cur.get("by_engine") or {}
    for eng in sorted(ce):
        sub = ce[eng] if isinstance(ce[eng], dict) else {}
        psub = pe.get(eng) if isinstance(pe.get(eng), dict) else {}
        d = pts(psub.get("share_of_answer"), sub.get("share_of_answer"))
        if d:
            out.append(f"share of answer {'up' if d > 0 else 'down'} {abs(d)} pts on {eng}")

    d = pts(prev.get("citation_rate"), cur.get("citation_rate"))
    if d:
        out.append(f"citation rate {'up' if d > 0 else 'down'} {abs(d)} pts")

    pa, ca = _num(prev.get("avg_position")), _num(cur.get("avg_position"))
    if pa is not None and ca is not None:
        delta = round(ca - pa, 2)
        if delta < 0:
            out.append(f"avg position improved by {abs(delta)} ({pa} -> {ca})")
        elif delta > 0:
            out.append(f"avg position worsened by {delta} ({pa} -> {ca})")

    pc = prev.get("competitor_share") or {}
    cc = cur.get("competitor_share") or {}
    for name in sorted(set(pc) | set(cc)):
        d = pts(pc.get(name), cc.get(name))
        if d:
            out.append(f"{name} share of answer {'up' if d > 0 else 'down'} {abs(d)} pts")
    return out


# --------------------------------------------------------------------------- #
# persistence (JSON lines: one record per panel run)
# --------------------------------------------------------------------------- #
def store(path, run_id, results, now_iso):
    """Append one panel run to a JSON-lines history file."""
    rows = list(results or [])
    try:
        line = json.dumps({"run_id": str(run_id), "ts": str(now_iso), "results": rows},
                          ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"results are not JSON-serialisable: {_safe_err(exc)}")
    try:
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        raise ToolError(_safe_err(f"cannot write history: {exc}"))
    return {"stored": len(rows), "run_id": str(run_id)}


def load_history(path):
    """Read a JSON-lines history file; missing file -> []; bad lines skipped."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except (OSError, TypeError) as exc:
        raise ToolError(_safe_err(f"cannot read history: {exc}"))
    out = []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# selftest — every effect mocked, tempfile only
# --------------------------------------------------------------------------- #
def selftest() -> int:
    import os
    import tempfile

    n = 0

    def check(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    brand = {"name": "AdRails", "aliases": ["adrails.dev"], "domains": ["adrails.dev"]}
    comps = {"Adspirer": ["adspirer.com"], "BidBot": ["bidbot.io"]}

    def make_ask(responses):
        calls = []

        def ask(engine, prompt):
            calls.append((engine, prompt))
            r = responses.get((engine, prompt), {"text": "", "citations": []})
            if isinstance(r, Exception):
                raise r
            return r

        ask.calls = calls
        return ask

    # 1..7 happy path
    ask1 = make_ask({("chatgpt", "best crm"): {
        "text": "I recommend AdRails as the best tool, ahead of Adspirer.",
        "citations": ["https://www.adrails.dev/blog/x"]}})
    out1 = run_panel(["best crm"], ["chatgpt"], ask1, brand, comps)
    r1 = out1["results"][0]
    check(len(out1["results"]) == 1 and out1["errors"] == [], "happy path: one result, no errors")
    check(r1["mentioned"] is True, "brand mentioned")
    check(r1["position"] == 1, f"brand first, got position {r1['position']}")
    check(r1["cited"] is True, "cited via subdomain")
    check(r1["competitors_mentioned"] == ["Adspirer"], "competitor captured")
    check(r1["sentiment"] == "positive", f"positive sentiment, got {r1['sentiment']}")
    check("AdRails" in r1["snippet"], "snippet contains mention")

    # 8 competitor first -> rank 2
    ask2 = make_ask({("chatgpt", "best crm"): {
        "text": "Adspirer is great; AdRails is limited.", "citations": []}})
    r2 = run_panel(["best crm"], ["chatgpt"], ask2, brand, comps)["results"][0]
    check(r2["position"] == 2 and r2["cited"] is False, "rank 2 when competitor speaks first")

    # 9 no mention -> fallback snippet
    ask3 = make_ask({("claude", "x"): {"text": "No idea.", "citations": ["https://example.com"]}})
    r3 = run_panel(["x"], ["claude"], ask3, brand, comps)["results"][0]
    check(r3["mentioned"] is False and r3["position"] is None and r3["snippet"] == "No idea.",
          "absent brand: no position, text head snippet")

    # 10 errors captured and redacted, panel continues
    ask4 = make_ask({("chatgpt", "q"): RuntimeError("token=sk-SECRET123 boom")})
    out4 = run_panel(["q"], ["chatgpt"], ask4, brand, comps)
    check(out4["results"] == [] and len(out4["errors"]) == 1, "ask exception -> error row only")
    check(isinstance(out4["errors"][0]["error"], str) and out4["errors"][0]["error"],
          "error text present")
    check("sk-SECRET123" not in out4["errors"][0]["error"], "secret scrubbed from error")

    # 11 repeats numbering
    ask5 = make_ask({("chatgpt", "q"): {"text": "AdRails", "citations": []}})
    out5 = run_panel(["q"], ["chatgpt"], ask5, brand, comps, repeats=2)
    check([r["run"] for r in out5["results"]] == [1, 2], "repeats run numbering")

    # 12..15 rails and validation
    for bad, exc_type, label in (
        (dict(repeats=0), ToolError, "repeats=0 -> ToolError"),
        (dict(repeats=99), RailViolation, "repeats=99 -> RailViolation"),
        (dict(prompts=[]), ToolError, "empty prompts -> ToolError"),
    ):
        kwargs = dict(prompts=["q"], engines=["chatgpt"], ask=ask5, brand=brand, competitors=comps)
        kwargs.update(bad)
        try:
            run_panel(**kwargs)
            check(False, label)
        except exc_type:
            check(True, label)
    try:
        run_panel([f"p{i}" for i in range(500)], ["a", "b"], ask5, brand, comps)
        check(False, "oversized panel should rail")
    except RailViolation:
        check(True, "oversized panel -> RailViolation")

    # 16 sleep called between calls only
    slept = []
    run_panel(["q", "q2"], ["chatgpt"], ask5, brand, comps, sleep=lambda: slept.append(1))
    check(len(slept) == 1, f"sleep between 2 calls, got {len(slept)}")

    # 17 non-dict answer degrades gracefully
    ask_bad = make_ask({("chatgpt", "q"): "plain string"})
    r_bad = run_panel(["q"], ["chatgpt"], ask_bad, brand, comps)["results"][0]
    check(r_bad["mentioned"] is False and r_bad["cited"] is False, "non-dict answer tolerated")

    # 18 string citations are normalised
    ask_str = make_ask({("chatgpt", "q"): {"text": "AdRails", "citations": "https://adrails.dev/a"}})
    check(run_panel(["q"], ["chatgpt"], ask_str, brand, comps)["results"][0]["cited"] is True,
          "string citation accepted")

    # 19 negative sentiment
    ask_neg = make_ask({("chatgpt", "q"): {"text": "AdRails is expensive and limited", "citations": []}})
    check(run_panel(["q"], ["chatgpt"], ask_neg, brand, comps)["results"][0]["sentiment"] == "negative",
          "negative sentiment")

    # 20 whole-word matching and domain-in-text matching
    ask_part = make_ask({("chatgpt", "q"): {"text": "adrailsonly is unrelated", "citations": []}})
    check(run_panel(["q"], ["chatgpt"], ask_part, brand, comps)["results"][0]["mentioned"] is False,
          "no partial-word match")
    ask_dom = make_ask({("chatgpt", "q"): {"text": "see adrails.dev for details", "citations": []}})
    check(run_panel(["q"], ["chatgpt"], ask_dom, brand, comps)["results"][0]["mentioned"] is True,
          "brand domain in text counts as mention")

    # 21 lookalike citation hosts are not ours
    ask_look = make_ask({("chatgpt", "q"): {
        "text": "AdRails", "citations": ["notadrails.dev/x", "https://adrails.dev.evil.com/"]}})
    check(run_panel(["q"], ["chatgpt"], ask_look, brand, comps)["results"][0]["cited"] is False,
          "lookalike hosts rejected")

    # 22..27 scoring
    rows = [
        {"engine": "chatgpt", "prompt": "p1", "run": 1, "mentioned": True, "position": 1,
         "cited": True, "competitors_mentioned": ["Adspirer"], "sentiment": "positive", "snippet": ""},
        {"engine": "chatgpt", "prompt": "p2", "run": 1, "mentioned": False, "position": None,
         "cited": False, "competitors_mentioned": ["Adspirer", "BidBot"], "sentiment": "neutral", "snippet": ""},
        {"engine": "claude", "prompt": "p1", "run": 1, "mentioned": False, "position": None,
         "cited": False, "competitors_mentioned": [], "sentiment": "neutral", "snippet": ""},
        {"engine": "claude", "prompt": "p2", "run": 1, "mentioned": False, "position": None,
         "cited": False, "competitors_mentioned": ["BidBot"], "sentiment": "neutral", "snippet": ""},
        {"engine": "claude", "prompt": "p3", "run": 1, "mentioned": False, "position": None,
         "cited": False, "competitors_mentioned": ["Adspirer"], "sentiment": "neutral", "snippet": ""},
    ]
    s = score(rows)
    check(s["total_runs"] == 5 and s["share_of_answer"] == 0.2, "share of answer 1/5")
    check(s["citation_rate"] == 0.2 and s["avg_position"] == 1.0, "citation rate and avg position")
    check(s["by_engine"]["chatgpt"]["share_of_answer"] == 0.5
          and s["by_engine"]["claude"]["share_of_answer"] == 0.0, "by_engine split")
    check(s["competitor_share"] == {"Adspirer": 0.6, "BidBot": 0.4}, "competitor share")
    check(s["top_gap_prompts"] == ["p2", "p3"], f"gap prompts sorted, got {s['top_gap_prompts']}")
    check(json.dumps(s, sort_keys=True) is not None, "score is JSON-serialisable")

    # 28 empty + malformed rows
    s0 = score([])
    check(s0["share_of_answer"] == 0.0 and s0["avg_position"] is None
          and s0["top_gap_prompts"] == [] and s0["competitor_share"] == {}, "empty score")
    s_bad = score([{"engine": "x"}, "nope", None,
                   {"engine": "x", "prompt": "p", "mentioned": True, "position": None}])
    check(s_bad["total_runs"] == 1 and s_bad["share_of_answer"] == 1.0, "malformed rows skipped")

    # 29..31 diff
    prev = {"share_of_answer": 0.2, "citation_rate": 0.1, "avg_position": 2.0,
            "by_engine": {"chatgpt": {"share_of_answer": 0.2}},
            "competitor_share": {"Adspirer": 0.5}}
    cur = {"share_of_answer": 0.32, "citation_rate": 0.1, "avg_position": 1.0,
           "by_engine": {"chatgpt": {"share_of_answer": 0.32}},
           "competitor_share": {"Adspirer": 0.3}}
    d = diff(prev, cur)
    check("share of answer up 12 pts" in d, f"overall diff line, got {d}")
    check("share of answer up 12 pts on chatgpt" in d, "per-engine diff line")
    check("avg position improved by 1.0 (2.0 -> 1.0)" in d
          and "Adspirer share of answer down 20 pts" in d, "position and competitor lines")
    check(diff({}, cur) == ["no previous run to compare"], "no baseline message")

    # 32..35 persistence
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "history.jsonl")
        check(load_history(path) == [], "missing history file -> []")
        info = store(path, "run-1", out1["results"], "2024-01-01T00:00:00Z")
        check(info["stored"] == 1, "store reports row count")
        store(path, "run-2", rows, "2024-01-02T00:00:00Z")
        hist = load_history(path)
        check(len(hist) == 2 and hist[1]["run_id"] == "run-2", "history roundtrip")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        check(len(load_history(path)) == 2, "malformed history line skipped")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)