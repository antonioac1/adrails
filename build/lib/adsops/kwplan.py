"""Keyword Planner research helpers for AdRails (``adsops.kwplan``).

Pure core of keyword research: builds Google Ads REST
``customers/{id}:generateKeywordIdeas`` requests, parses and sorts idea rows,
clusters keywords into themes, tags search intent, and plans shared negative
keyword lists. Negatives are add-only, so ``remove`` is always empty.

Safety: nothing here touches the network, clock or filesystem -- a tool performs
the HTTP call itself with an injected callable and passes decoded JSON back in.
Bad input raises ToolError before any request exists; money leaves this module
in currency units (micros / 1e6) and every output is JSON-serialisable.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from adsops.errors import ToolError, scrub

_ALLOWED_NETWORKS = ("GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS")
_MATCH_TYPES = ("BROAD", "PHRASE", "EXACT")
_COMPETITIONS = ("LOW", "MEDIUM", "HIGH", "UNSPECIFIED")
_MAX_SEEDS = 20
_MAX_SEED_LEN = 80
_MAX_KEYWORD_LEN = 80


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Nested-safe getter for camelCase Google Ads REST rows."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _num(value, cast=int, default=0):
    """Defensive numeric coercion: Google Ads happily sends numbers as strings."""
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        pass
    try:  # e.g. "500.0" -> int
        return cast(float(value))
    except (TypeError, ValueError):
        return default


def _money(micros):
    """Micros -> currency units, rounded to 2 dp, never negative."""
    return round(max(0.0, _num(micros, int) / 1_000_000.0), 2)


def _norm_keyword(text, where):
    if not isinstance(text, str) or not text.strip():
        raise ToolError(f"{where}: keyword text must be a non-empty string")
    text = " ".join(text.split())
    if len(text) > _MAX_KEYWORD_LEN:
        raise ToolError(f"{where}: keyword text longer than {_MAX_KEYWORD_LEN} characters")
    return text


def _norm_entry(entry, where):
    """Validate/normalise one {'text':..., 'match_type':...} record."""
    if not isinstance(entry, dict):
        raise ToolError(f"{where}: expected an object with 'text' and 'match_type'")
    text = _norm_keyword(entry.get("text"), where)
    mt = entry.get("match_type", entry.get("matchType"))
    if not isinstance(mt, str) or mt.strip().upper() not in _MATCH_TYPES:
        raise ToolError(scrub(
            f"{where}: match_type must be one of {', '.join(_MATCH_TYPES)} (got {mt!r})"))
    return {"text": text, "match_type": mt.strip().upper()}


# --------------------------------------------------------------------------- #
# request building
# --------------------------------------------------------------------------- #
def build_ideas_request(customer_id, seeds, url=None, geo_ids=None,
                        language_id=1000, network="GOOGLE_SEARCH"):
    """Build (path, body) for POST customers/{cid}:generateKeywordIdeas."""
    cid = str(customer_id or "").strip().replace("-", "")
    if not cid.isdigit():
        raise ToolError("customer_id must be a numeric Google Ads customer id")

    if not isinstance(seeds, (list, tuple)):
        raise ToolError(f"seeds must be a list of 1..{_MAX_SEEDS} keyword strings")
    if not 1 <= len(seeds) <= _MAX_SEEDS:
        raise ToolError(f"seeds must contain 1..{_MAX_SEEDS} keywords "
                        f"(got {len(seeds)})")

    cleaned, seen = [], set()
    for seed in seeds:
        if not isinstance(seed, str):
            raise ToolError("each seed must be a string")
        text = " ".join(seed.split())
        if not text:
            raise ToolError("seed keywords must be non-empty strings")
        if len(text) > _MAX_SEED_LEN:
            raise ToolError(f"seed keyword longer than {_MAX_SEED_LEN} characters")
        if text.lower() not in seen:  # case-insensitive dedupe, keep first spelling
            seen.add(text.lower())
            cleaned.append(text)
    if not 1 <= len(cleaned) <= _MAX_SEEDS:
        raise ToolError(f"seeds must contain 1..{_MAX_SEEDS} distinct keywords "
                        f"(got {len(cleaned)})")

    geos = [2840] if geo_ids is None else list(geo_ids)
    if not geos:
        raise ToolError("geo_ids must contain at least one geo target constant id")
    geo_paths = []
    for g in geos:
        gid = str(g).strip()
        if not gid.isdigit():
            raise ToolError("geo_ids must be numeric geo target constant ids")
        geo_paths.append(f"geoTargetConstants/{int(gid)}")

    lang = str(language_id).strip()
    if not lang.isdigit():
        raise ToolError("language_id must be a numeric language constant id")

    net = str(network or "").strip().upper()
    if net not in _ALLOWED_NETWORKS:
        raise ToolError("network must be GOOGLE_SEARCH or GOOGLE_SEARCH_AND_PARTNERS")

    body = {
        "language": f"languageConstants/{int(lang)}",
        "geoTargetConstants": geo_paths,
        "keywordPlanNetwork": net,
        "includeAdultKeywords": False,
    }
    if url is not None:
        u = str(url).strip()
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https") or "." not in (parsed.netloc or ""):
            raise ToolError("url must be an absolute http(s) URL")
        body["keywordAndUrlSeed"] = {"keywords": cleaned, "url": u}
    else:
        body["keywordSeed"] = {"keywords": cleaned}
    return f"customers/{cid}:generateKeywordIdeas", body


# --------------------------------------------------------------------------- #
# response parsing
# --------------------------------------------------------------------------- #
def parse_ideas(response):
    """Flatten results[] into sorted idea dicts (searches desc, then keyword)."""
    results = _g(response, "results", default=[])
    if not isinstance(results, list):
        return []
    out = []
    for row in results:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():  # malformed row: skip
            continue
        metrics = _g(row, "keywordIdeaMetrics", default={})
        if not isinstance(metrics, dict):
            metrics = {}
        comp = str(metrics.get("competition") or "UNSPECIFIED").strip().upper()
        if comp not in _COMPETITIONS:
            comp = "UNSPECIFIED"
        out.append({
            "keyword": text.strip(),
            "avg_monthly_searches": _num(metrics.get("avgMonthlySearches"), int),
            "competition": comp,
            "competition_index": _num(metrics.get("competitionIndex"), int),
            "low_bid": _money(metrics.get("lowTopOfPageBidMicros")),
            "high_bid": _money(metrics.get("highTopOfPageBidMicros")),
        })
    out.sort(key=lambda r: (-r["avg_monthly_searches"], r["keyword"].lower()))
    return out


# --------------------------------------------------------------------------- #
# clustering
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset(
    "a an and are as at be by for from how i if in into is it of on or the to "
    "vs with what when where which who why you your my our us this that these "
    "those do does can will near me".split()
)


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(t) > 1 and t not in _STOPWORDS]


def cluster(ideas, min_shared=1):
    """Group keywords by their most informative shared (non-generic) token."""
    if not isinstance(ideas, list) or not ideas:
        return []
    min_shared = max(1, _num(min_shared, int, 1))
    rows = []
    for idea in ideas:
        if not isinstance(idea, dict):
            continue
        kw = idea.get("keyword")
        if not isinstance(kw, str) or not kw.strip():
            continue
        rows.append((kw.strip(), _num(idea.get("avg_monthly_searches"), int), _tokens(kw)))
    if not rows:
        return []

    n = len(rows)
    df = {}
    for _, _, toks in rows:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    # Tokens appearing in >60% of ideas are too generic to be a theme -- but a
    # lone idea (or two) must not lose every token, hence the df >= 2 guard.
    usable = {t: c for t, c in df.items() if not (c > 0.6 * n and c >= 2)}

    groups = {}
    for kw, searches, toks in rows:
        cands = [t for t in set(toks) if t in usable and df[t] >= min_shared]
        if cands:
            # Widest coverage first (broadest coherent theme), then the longer
            # / more specific token, then alphabetical -- fully deterministic.
            theme = sorted(cands, key=lambda t: (-df[t], -len(t), t))[0]
        else:
            theme = "(other)"
        groups.setdefault(theme, []).append((kw, searches))

    out = []
    for theme, items in groups.items():
        items.sort(key=lambda x: (-x[1], x[0].lower()))
        out.append({"theme": theme,
                    "keywords": [k for k, _ in items],
                    "searches": int(sum(s for _, s in items))})
    out.sort(key=lambda g: (-g["searches"], g["theme"]))
    return out


# --------------------------------------------------------------------------- #
# intent tagging
# --------------------------------------------------------------------------- #
_INTENT_CUES = (
    ("transactional", (
        "near me", "nearby", "buy", "hire", "cheap", "cheapest", "deal", "deals",
        "discount", "book", "booking", "order", "shop", "quote", "purchase",
        "rent", "rental", "installation", "for sale", "delivery", "appointment",
        "coupon", "subscribe", "sign up", "open now",
    )),
    ("navigational", (
        "login", "log in", "sign in", "signin", "account", "official site",
        "official website", "website", "app", "download", "customer service",
        "contact", "phone number", "hours",
    )),
    ("commercial", (
        "best", "top", "review", "reviews", "vs", "versus", "compare",
        "comparison", "alternative", "alternatives", "ranked", "which",
        "cost", "costs", "price", "prices", "pricing",
    )),
    ("informational", (
        "how", "what", "why", "when", "where", "who", "guide", "tutorial",
        "meaning", "definition", "example", "examples", "tips", "ideas",
        "learn", "diy", "recipe", "history", "difference",
    )),
)


def intent(keyword):
    """Return transactional|navigational|commercial|informational for a query."""
    text = " ".join(re.findall(r"[a-z0-9]+", str(keyword or "").lower()))
    if not text:
        return "informational"  # unmatched/empty queries default to research-ish
    padded = f" {text} "
    for name, cues in _INTENT_CUES:
        for cue in cues:
            if f" {cue} " in padded:  # whole-token match, so "cost" != "costume"
                return name
    return "informational"


# --------------------------------------------------------------------------- #
# shared negative keyword list planning
# --------------------------------------------------------------------------- #
def plan_shared_list(current, desired):
    """Diff desired vs current negatives. Negatives are add-only: no removes."""
    cur = [_norm_entry(e, f"current[{i}]") for i, e in enumerate(current or [])]
    des = [_norm_entry(e, f"desired[{i}]") for i, e in enumerate(desired or [])]

    have = {(e["text"].lower(), e["match_type"]) for e in cur}
    seen, reported = set(), set()
    add, present = [], []
    for entry in des:
        key = (entry["text"].lower(), entry["match_type"])
        if key in have:
            if key not in reported:  # report each already-present item once
                reported.add(key)
                present.append(entry["text"])
            continue
        if key in seen:  # duplicate inside `desired`
            continue
        seen.add(key)
        add.append(entry)

    if not des:
        note = "no desired keywords supplied; nothing to add"
    elif not add:
        note = (f"no changes: all {len(present)} desired keywords already present "
                f"(negatives are add-only)")
    elif present:
        shown = ", ".join(present[:5]) + (" ..." if len(present) > 5 else "")
        note = (f"{len(add)} to add, {len(present)} already present "
                f"(case-insensitive): {shown}; negatives are add-only")
    else:
        note = f"{len(add)} to add, none already present; negatives are add-only"
    return {"add": add, "remove": [], "note": note}


def shared_list_ops(shared_set_resource, adds):
    """Turn planned adds into sharedCriteria create operations."""
    if not isinstance(shared_set_resource, str) or not shared_set_resource.strip():
        raise ToolError("shared_set_resource must be a non-empty resource name")
    if not isinstance(adds, list):
        raise ToolError("adds must be a list of {'text', 'match_type'} objects")
    res = shared_set_resource.strip()
    ops = []
    for i, entry in enumerate(adds):
        item = _norm_entry(entry, f"adds[{i}]")
        ops.append({"create": {"sharedSet": res,
                               "keyword": {"text": item["text"],
                                           "matchType": item["match_type"]}}})
    return ops


# --------------------------------------------------------------------------- #
# selftest -- all I/O mocked (this module has none), pure and fast
# --------------------------------------------------------------------------- #
def selftest() -> int:
    checks = 0

    def ck(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(f"selftest failed: {msg}")

    def raises(fn, exc, msg):
        nonlocal checks
        checks += 1
        try:
            fn()
        except exc:
            return
        except Exception as e:  # noqa: BLE001 - report wrong exception type
            raise AssertionError(f"selftest: {msg}: wrong exception "
                                 f"{type(e).__name__}: {e}") from e
        raise AssertionError(f"selftest: {msg}: no exception raised")

    # -- build_ideas_request -------------------------------------------------
    path, body = build_ideas_request("1234567890", [" running shoes ", "trail shoes"])
    ck(path == "customers/1234567890:generateKeywordIdeas", "request path")
    ck(body["language"] == "languageConstants/1000", "default language")
    ck(body["geoTargetConstants"] == ["geoTargetConstants/2840"], "default geo")
    ck(body["keywordPlanNetwork"] == "GOOGLE_SEARCH", "default network")
    ck(body["includeAdultKeywords"] is False, "adult keywords excluded")
    ck(body["keywordSeed"] == {"keywords": ["running shoes", "trail shoes"]},
       "seed keywords trimmed")
    ck("keywordAndUrlSeed" not in body, "no url seed when url omitted")
    ck(build_ideas_request("1", ["Shoes", "shoes"])[1]["keywordSeed"]["keywords"]
       == ["Shoes"], "case-insensitive seed dedupe keeps first spelling")
    ck(build_ideas_request("1", ["x" * 80])[1]["keywordSeed"]["keywords"] == ["x" * 80],
       "seed at 80 chars accepted")

    _, body2 = build_ideas_request("1-2", ["shoes"], url="https://example.com/shoes",
                                   geo_ids=[2840, "2841"], language_id=1010,
                                   network="google_search_and_partners")
    ck(body2["keywordAndUrlSeed"] == {"keywords": ["shoes"],
                                      "url": "https://example.com/shoes"}, "url seed")
    ck(body2["geoTargetConstants"] == ["geoTargetConstants/2840",
                                       "geoTargetConstants/2841"], "geo override")
    ck(body2["language"] == "languageConstants/1010", "language override")
    ck(body2["keywordPlanNetwork"] == "GOOGLE_SEARCH_AND_PARTNERS", "network override")

    raises(lambda: build_ideas_request("12x", ["a"]), ToolError, "bad customer id")
    raises(lambda: build_ideas_request("1", []), ToolError, "no seeds")
    raises(lambda: build_ideas_request("1", [f"k{i}" for i in range(21)]),
           ToolError, "too many seeds")
    raises(lambda: build_ideas_request("1", ["   "]), ToolError, "blank seed")
    raises(lambda: build_ideas_request("1", ["x" * 81]), ToolError, "seed too long")
    raises(lambda: build_ideas_request("1", ["a"], network="DISPLAY"),
           ToolError, "bad network")
    raises(lambda: build_ideas_request("1", ["a"], url="example.com"),
           ToolError, "relative url")
    raises(lambda: build_ideas_request("1", ["a"], geo_ids=[]), ToolError, "no geos")

    # -- parse_ideas ---------------------------------------------------------
    resp = {"results": [
        {"text": "shoes", "keywordIdeaMetrics": {
            "avgMonthlySearches": "500", "competition": "low",
            "competitionIndex": "20", "lowTopOfPageBidMicros": "1500000",
            "highTopOfPageBidMicros": 2500000}},
        {"text": "running shoes", "keywordIdeaMetrics": {
            "avgMonthlySearches": "2000", "competition": "HIGH",
            "competitionIndex": 80, "lowTopOfPageBidMicros": 500000,
            "highTopOfPageBidMicros": "900000"}},
        {"keywordIdeaMetrics": {"avgMonthlySearches": 10}},  # no text -> skipped
        "not-a-dict",
        {"text": "boots"},  # no metrics -> zeros
    ]}
    ideas = parse_ideas(resp)
    ck([i["keyword"] for i in ideas] == ["running shoes", "shoes", "boots"],
       "sorted by searches desc")
    ck(ideas[0]["avg_monthly_searches"] == 2000, "string -> int coercion")
    ck(ideas[1]["competition"] == "LOW" and ideas[1]["competition_index"] == 20,
       "competition normalised")
    ck(ideas[1]["low_bid"] == 1.5 and ideas[1]["high_bid"] == 2.5, "micros -> units")
    ck(ideas[2]["competition"] == "UNSPECIFIED" and ideas[2]["low_bid"] == 0.0,
       "missing metrics default to zero")
    ck(parse_ideas({}) == [] and parse_ideas(None) == [] and parse_ideas([]) == [],
       "empty/malformed response")
    ck(parse_ideas({"results": [{"text": "x", "keywordIdeaMetrics":
                                 {"competition": "WEIRD"}}]})[0]["competition"]
       == "UNSPECIFIED", "unknown competition -> UNSPECIFIED")

    # -- cluster -------------------------------------------------------------
    ideas_c = [
        {"keyword": "running shoes", "avg_monthly_searches": 100},
        {"keyword": "trail shoes", "avg_monthly_searches": 50},
        {"keyword": "running socks", "avg_monthly_searches": 10},
        {"keyword": "wool socks", "avg_monthly_searches": 5},
    ]
    cl = cluster(ideas_c)
    ck([g["theme"] for g in cl] == ["running", "shoes", "socks"], "cluster themes")
    ck(cl[0]["keywords"] == ["running shoes", "running socks"] and cl[0]["searches"] == 110,
       "cluster members + search sum")
    ck(cl[1]["keywords"] == ["trail shoes"] and cl[2]["searches"] == 5, "single-member cluster")
    ck(cluster([]) == [] and cluster([{"keyword": "  "}]) == [], "cluster empty input")
    one = cluster([{"keyword": "shoes", "avg_monthly_searches": "7"}])
    ck(len(one) == 1 and one[0]["searches"] == 7 and isinstance(one[0]["searches"], int),
       "single idea, string searches -> int sum")
    other = cluster([{"keyword": "zzz shoes", "avg_monthly_searches": 1},
                     {"keyword": "qqq shoes", "avg_monthly_searches": 2}], min_shared=2)
    ck(other == [{"theme": "(other)", "keywords": ["qqq shoes", "zzz shoes"],
                  "searches": 3}], "min_shared fallback to (other)")

    # -- intent --------------------------------------------------------------
    ck(intent("pizza near me") == "transactional", "near me -> transactional")
    ck(intent("Buy Running Shoes Online") == "transactional", "buy (case-insensitive)")
    ck(intent("best running shoes") == "commercial", "best -> commercial")
    ck(intent("nike vs adidas") == "commercial", "vs -> commercial")
    ck(intent("how to bake bread") == "informational", "how -> informational")
    ck(intent("netflix login") == "navigational", "login -> navigational")
    ck(intent("") == "informational", "empty keyword default")

    # -- plan_shared_list ----------------------------------------------------
    cur = [{"text": "Free", "match_type": "BROAD"},
           {"text": "cheap", "match_type": "PHRASE"}]
    des = [{"text": "free", "match_type": "broad"},
           {"text": "CHEAP", "match_type": "PHRASE"},
           {"text": "scam", "match_type": "EXACT"},
           {"text": "scam", "match_type": "exact"},
           {"text": " free ", "match_type": "EXACT"}]
    plan = plan_shared_list(cur, des)
    ck(plan["remove"] == [], "remove always empty")
    ck(plan["add"] == [{"text": "scam", "match_type": "EXACT"},
                       {"text": "free", "match_type": "EXACT"}],
       "adds deduped case-insensitively")
    ck("2 to add" in plan["note"] and "2 already present" in plan["note"],
       "note reports adds and existing")
    ck(plan_shared_list([], [])["add"] == [] and plan_shared_list(cur, cur)["add"] == [],
       "no-op plans")
    ck(plan_shared_list(cur, cur)["note"].startswith("no changes"), "all-present note")
    raises(lambda: plan_shared_list([{"text": "x", "match_type": "FUZZY"}], []),
           ToolError, "bad match type in current")
    raises(lambda: plan_shared_list([], [{"text": "", "match_type": "BROAD"}]),
           ToolError, "empty keyword text")

    # -- shared_list_ops -----------------------------------------------------
    ops = shared_list_ops("customers/1/sharedSets/9", plan["add"])
    ck(len(ops) == 2, "one op per add")
    ck(ops[0] == {"create": {"sharedSet": "customers/1/sharedSets/9",
                             "keyword": {"text": "scam", "matchType": "EXACT"}}},
       "operation shape")
    ck(shared_list_ops("customers/1/sharedSets/9", []) == [], "no adds -> no ops")
    raises(lambda: shared_list_ops("", []), ToolError, "empty shared set resource")
    raises(lambda: shared_list_ops("res", [{"text": "x", "match_type": "WIDE"}]),
           ToolError, "bad match type in adds")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)