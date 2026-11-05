"""JSON-LD structured-data audit and generators for AdRails (AEO).

AI answer engines and Google read schema.org JSON-LD embedded in pages. This
module extracts those blocks from HTML, audits each object against a per-type
required/recommended profile (plus policy and freshness warnings), and scores
the result. It also builds ready-to-embed FAQPage / SoftwareApplication /
Organization / BreadcrumbList snippets that pass the audit's required checks.

Safety: no network, disk or clock access at import time; `today` is injectable,
all parsing is local, and exceptions carry scrubbed text only (no PII/secrets).
A tool should call `audit_html(html)` for read-only checks, or the `make_*`
generators plus `to_script_tag()` to produce markup for human review.
"""

import datetime
import html.parser
import json
import re

from adsops.errors import RailViolation, ToolError, scrub  # noqa: F401  (RailViolation kept for callers)

# ---------------------------------------------------------------------------
# Requirement profiles. "required" = must exist for the type to be valid,
# "recommended" = materially improves rich-result eligibility.
# ---------------------------------------------------------------------------
_LOCAL = {
    "required": ["name", "address", "telephone"],
    "recommended": ["url", "geo", "areaServed", "openingHoursSpecification",
                    "priceRange", "sameAs"],
}
_ARTICLE = {
    "required": ["headline", "datePublished"],
    "recommended": ["author", "dateModified", "image", "publisher"],
}

REQUIRED = {
    "Organization": {"required": ["name", "url"],
                     "recommended": ["logo", "sameAs", "description", "contactPoint"]},
    "LocalBusiness": _LOCAL,
    "RealEstateAgent": _LOCAL,
    "ProfessionalService": _LOCAL,
    "SoftwareApplication": {
        "required": ["name", "applicationCategory", "operatingSystem"],
        "recommended": ["offers", "aggregateRating", "description", "url", "softwareVersion"],
    },
    "Product": {"required": ["name"],
                "recommended": ["offers", "brand", "description", "image"]},
    "FAQPage": {"required": ["mainEntity"], "recommended": []},
    "Question": {"required": ["name", "acceptedAnswer"], "recommended": []},
    "Article": _ARTICLE,
    "BlogPosting": _ARTICLE,
    "NewsArticle": _ARTICLE,
    "BreadcrumbList": {"required": ["itemListElement"], "recommended": []},
    "WebSite": {"required": ["name", "url"], "recommended": ["potentialAction"]},
    "HowTo": {"required": ["name", "step"], "recommended": []},
    "Dataset": {"required": ["name", "description"],
                "recommended": ["license", "creator", "distribution", "temporalCoverage"]},
    "Event": {"required": ["name", "startDate", "location"], "recommended": ["offers"]},
}

_FAQ_MIN_WORDS = 10
_FAQ_MAX_WORDS = 300
_STALE_DAYS = 365
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|[T ]\d{2}:\d{2})")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _g(row, *path, default=None):
    """Nested get for camelCase dicts (Google Ads rows / JSON-LD objects).

    Walks dict keys (and int indices into lists) defensively; anything missing,
    None or of an unexpected shape yields `default`.
    """
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return default if cur is None else cur


def _s(value):
    """Coerce to string without ever raising (None -> '')."""
    return "" if value is None else (value if isinstance(value, str) else str(value))


def _num(value):
    """Coerce to float; returns None when not numerically representable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _types(obj):
    """Normalise @type into a list of strings (missing/invalid -> [])."""
    raw = obj.get("@type") if isinstance(obj, dict) else None
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, str) and x.strip()]
    return []


def _has(obj, field):
    """True when field exists and carries a non-empty value."""
    if not isinstance(obj, dict) or field not in obj:
        return False
    value = obj[field]
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


def _parse_date(value):
    """Parse an ISO-8601 date or datetime prefix; None when not valid ISO."""
    if not isinstance(value, str):
        return None
    match = _ISO_DATE.match(value.strip())
    if not match:
        return None
    try:
        return datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _today(value):
    """Resolve the injectable `today` argument into a date."""
    if value is None:
        return datetime.date.today()
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    parsed = _parse_date(str(value))
    if parsed is None:
        raise ToolError("today must be an ISO-8601 date, e.g. 2024-06-01")
    return parsed


def _dedupe(seq):
    seen, out = set(), []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _LDJsonParser(html.parser.HTMLParser):
    """Collect the text of <script type="application/ld+json"> blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        mime = attr.get("type", "").split(";")[0].strip().lower()
        if mime == "application/ld+json":
            self._buf = []

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._buf is not None:
            self.chunks.append("".join(self._buf))
            self._buf = None

    def close(self):
        super().close()
        if self._buf is not None:  # unterminated <script> at EOF
            self.chunks.append("".join(self._buf))
            self._buf = None


def _flatten(value, depth=0):
    """Expand lists and @graph wrappers into a flat list of dicts."""
    if depth > 8:
        return [{"_error": "@graph nesting too deep", "_raw": _s(value)[:200]}]
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten(item, depth + 1))
        return out
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, (list, dict)):
            return _flatten(graph, depth + 1)
        return [value]
    return [{"_error": "JSON-LD value is not an object or list", "_raw": _s(value)[:200]}]


def extract_jsonld(html):
    """Return parsed JSON-LD objects from an HTML string.

    Handles @graph wrappers and top-level arrays; invalid JSON becomes
    {"_error": msg, "_raw": first 200 chars} so an audit can surface it.
    """
    if not isinstance(html, str):
        raise ToolError("html must be a string")
    parser = _LDJsonParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # HTMLParser is lenient, but never leak internals
        raise ToolError("failed to parse HTML: " + scrub(str(exc), 200))
    objects = []
    for raw in parser.chunks:
        text = raw.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except (ValueError, TypeError) as exc:
            objects.append({"_error": scrub(str(exc), 200), "_raw": text[:200]})
            continue
        objects.extend(_flatten(value))
    return objects


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def _answer_warnings(text):
    """FAQ answer length heuristics (AEO: too thin or too bloated)."""
    if text is None:
        return []
    if not isinstance(text, str) or not text.strip():
        return ["FAQ answer is empty"]
    words = len(text.split())
    if words < _FAQ_MIN_WORDS:
        return [f"FAQ answer too short ({words} words < {_FAQ_MIN_WORDS})"]
    if words > _FAQ_MAX_WORDS:
        return [f"FAQ answer too long ({words} words > {_FAQ_MAX_WORDS})"]
    return []


def _warnings(obj, today):
    """Per-object policy/freshness warnings (required fields handled separately)."""
    kinds = set(_types(obj))
    warns = []

    for key in ("datePublished", "dateModified"):
        if key in obj and obj[key] is not None:
            parsed = _parse_date(obj[key])
            if parsed is None:
                warns.append(f"{key} is not a valid ISO-8601 date")
            elif key == "dateModified":
                age = (today - parsed).days
                if age > _STALE_DAYS:
                    warns.append(f"dateModified is {age} days old (> {_STALE_DAYS})")

    if "sameAs" in obj and obj["sameAs"] is not None:
        values = obj["sameAs"] if isinstance(obj["sameAs"], list) else [obj["sameAs"]]
        if any(not (isinstance(u, str) and u.strip().lower().startswith("https://"))
               for u in values):
            warns.append("sameAs contains a non-https URL")

    rating = obj.get("aggregateRating")
    if isinstance(rating, dict):
        if not (_has(rating, "ratingCount") or _has(rating, "reviewCount")):
            warns.append("aggregateRating lacks ratingCount/reviewCount (Google policy risk)")

    offers = obj.get("offers")
    if offers is not None:
        values = offers if isinstance(offers, list) else [offers]
        for offer in values:
            if not isinstance(offer, dict):
                warns.append("offers must be an object or a list of objects")
                break
            if "price" in offer and offer["price"] is not None and _num(offer["price"]) is None:
                warns.append("offers.price is not numeric")
                break

    if "FAQPage" in kinds:
        entity = obj.get("mainEntity")
        for question in (entity if isinstance(entity, list) else [entity]):
            if isinstance(question, dict):
                warns.extend(_answer_warnings(_g(question, "acceptedAnswer", "text")))
    if "Question" in kinds:
        warns.extend(_answer_warnings(_g(obj, "acceptedAnswer", "text")))

    return _dedupe(warns)


def _item(kind, missing_required, missing_recommended, warnings):
    return {"type": kind, "missing_required": missing_required,
            "missing_recommended": missing_recommended, "warnings": warnings}


def audit_objects(objs, today=None):
    """Audit parsed JSON-LD objects; returns types/items/findings/score(0-100)."""
    if objs is None:
        objs = []
    if not isinstance(objs, (list, tuple)):
        raise ToolError("objs must be a list of JSON-LD objects")
    day = _today(today)

    items, findings, types_seen = [], [], []
    org_items, fatal = [], 0

    for obj in objs:
        if not isinstance(obj, dict):
            items.append(_item(None, [], [], ["entry is not a JSON object"]))
            findings.append("skipped a non-object JSON-LD entry")
            fatal += 1
            continue
        if "_error" in obj:
            msg = scrub(_s(obj.get("_error")), 200)
            items.append(_item(None, [], [], ["JSON-LD parse error: " + msg]))
            findings.append("JSON-LD parse error: " + msg)
            fatal += 1
            continue

        kinds = _types(obj)
        warns = _warnings(obj, day)
        if not kinds:
            items.append(_item(None, [], [], ["missing or invalid @type"] + warns))
            findings.append("JSON-LD object has no valid @type")
            continue

        for position, kind in enumerate(kinds):
            if kind not in types_seen:
                types_seen.append(kind)
            extra = warns if position == 0 else []
            profile = REQUIRED.get(kind)
            if profile is None:
                items.append(_item(kind, [], [],
                                   ["unknown @type; no requirement profile"] + extra))
                findings.append(f"{kind}: unknown type")
                continue
            missing_req = [f for f in profile["required"] if not _has(obj, f)]
            missing_rec = [f for f in profile["recommended"] if not _has(obj, f)]
            items.append(_item(kind, missing_req, missing_rec, list(extra)))
            for field in missing_req:
                findings.append(f"{kind}: missing required field '{field}'")
            for field in missing_rec:
                findings.append(f"{kind}: missing recommended field '{field}'")
            for warn in extra:
                findings.append(f"{kind}: {warn}")
            if kind == "Organization":
                org_items.append((len(items) - 1, _s(obj.get("name")).strip()))

    # Conflicting Organization blocks confuse engines about the entity identity.
    by_name = {}
    for index, name in org_items:
        if name:
            by_name.setdefault(name.lower(), []).append(index)
    if len(org_items) > 1 and len(by_name) > 1:
        sample = ", ".join(sorted(by_name)[:5])
        message = f"duplicate Organization blocks with different names: {sample}"
        for indices in by_name.values():
            for index in indices:
                items[index]["warnings"] = _dedupe(items[index]["warnings"] + [message])
        findings.append(message)

    if not objs:
        findings.append("no JSON-LD structured data found")

    missing_req = sum(len(i["missing_required"]) for i in items)
    missing_rec = sum(len(i["missing_recommended"]) for i in items)
    warned = sum(len(i["warnings"]) for i in items)
    score = 100 - 8 * missing_req - 2 * missing_rec - 4 * warned - 10 * fatal
    if not objs:
        score = 0
    score = max(0, min(100, int(round(score))))

    return {"types": types_seen, "items": items, "findings": findings, "score": score}


def audit_html(html, today=None):
    """Extract JSON-LD from raw HTML and audit it (read-only)."""
    return audit_objects(extract_jsonld(html), today=today)


# ---------------------------------------------------------------------------
# Generators (each must pass its own audit with no missing_required)
# ---------------------------------------------------------------------------
def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{label} must be a non-empty string")
    return value.strip()


def _require_url(value, label):
    text = _require_text(value, label)
    if not text.lower().startswith(("http://", "https://")):
        raise ToolError(f"{label} must be an absolute http(s) URL")
    return text


def make_faq(qas):
    """Build a FAQPage from [(question, answer), ...]."""
    if not isinstance(qas, (list, tuple)) or not qas:
        raise ToolError("qas must be a non-empty list of (question, answer) pairs")
    entities = []
    for pair in qas:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ToolError("each QA entry must be a (question, answer) pair")
        question = _require_text(pair[0], "question")
        answer = _require_text(pair[1], "answer")
        entities.append({"@type": "Question", "name": question,
                         "acceptedAnswer": {"@type": "Answer", "text": answer}})
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": entities}


def make_software(name, url, description, category="BusinessApplication", os="Any",
                  price="0", currency="USD", version=None):
    """Build a SoftwareApplication (with an Offer) that passes required checks."""
    name = _require_text(name, "name")
    url = _require_url(url, "url")
    description = _require_text(description, "description")
    category = _require_text(category, "category")
    os = _require_text(os, "os")

    amount = _num(price)
    if amount is None or amount < 0:
        raise ToolError("price must be a non-negative number")
    price_text = price.strip() if isinstance(price, str) else f"{amount:.2f}"
    if _num(price_text) is None:
        raise ToolError("price must be a non-negative number")

    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise ToolError("currency must be a 3-letter ISO-4217 code")

    obj = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": name,
        "url": url,
        "description": description,
        "applicationCategory": category,
        "operatingSystem": os,
        "offers": {"@type": "Offer", "price": price_text,
                   "priceCurrency": currency.strip().upper()},
    }
    if version is not None:
        obj["softwareVersion"] = _require_text(version, "version")
    return obj


def make_org(name, url, logo=None, same_as=()):
    """Build an Organization block (sameAs entries must be https)."""
    obj = {"@context": "https://schema.org", "@type": "Organization",
           "name": _require_text(name, "name"), "url": _require_url(url, "url")}
    if logo is not None:
        obj["logo"] = _require_url(logo, "logo")
    if same_as:
        if isinstance(same_as, str):
            raise ToolError("same_as must be a sequence of URLs, not a string")
        links = []
        for link in same_as:
            link = _require_text(link, "same_as entry")
            if not link.lower().startswith("https://"):
                raise ToolError("same_as entries must be https URLs")
            links.append(link)
        if links:
            obj["sameAs"] = links
    return obj


def make_breadcrumbs(trail):
    """Build a BreadcrumbList from [(name, url), ...] (positions start at 1)."""
    if not isinstance(trail, (list, tuple)) or not trail:
        raise ToolError("trail must be a non-empty list of (name, url) pairs")
    elements = []
    for index, pair in enumerate(trail, start=1):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ToolError("each breadcrumb must be a (name, url) pair")
        elements.append({"@type": "ListItem", "position": index,
                         "name": _require_text(pair[0], "breadcrumb name"),
                         "item": _require_url(pair[1], "breadcrumb url")})
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": elements}


def to_script_tag(obj):
    """Serialise an object into an embeddable JSON-LD script tag (</ escaped)."""
    try:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ToolError("object is not JSON-serialisable: " + scrub(str(exc), 200))
    # "</" would let a value close the script element; "<\/" is still valid JSON.
    return '<script type="application/ld+json">' + payload.replace("</", "<\\/") + "</script>"


# ---------------------------------------------------------------------------
# Self-test (all I/O mocked; no network/disk/clock dependency)
# ---------------------------------------------------------------------------
def selftest() -> int:
    checks = 0

    def check(condition, message):
        nonlocal checks
        if not condition:
            raise AssertionError(message)
        checks += 1

    today = "2024-06-01"

    # --- extraction -------------------------------------------------------
    page = ('<html><head>'
            '<script type="application/ld+json">{"@type":"Organization","name":"A","url":"https://a.example"}</script>'
            '<script type="text/javascript">var x = 1;</script>'
            '</head></html>')
    objs = extract_jsonld(page)
    check(len(objs) == 1 and objs[0]["name"] == "A", "simple extraction")
    check(extract_jsonld("") == [], "empty html yields no objects")

    graph = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
             '{"@type":"WebSite","name":"S","url":"https://s.example","potentialAction":{}},'
             '{"@type":"Organization","name":"O","url":"https://o.example","logo":"https://o.example/l.png"}]}</script>')
    expanded = extract_jsonld(graph)
    check(len(expanded) == 2 and expanded[1]["@type"] == "Organization", "@graph expanded")

    listed = extract_jsonld('<script type="application/ld+json">[{"@type":"HowTo","name":"h","step":[]},'
                            '{"@type":"Dataset","name":"d","description":"x"}]</script>')
    check(len(listed) == 2, "top-level list expanded")

    broken = extract_jsonld('<script type="application/ld+json">{not json</script>')
    check(len(broken) == 1 and "_error" in broken[0] and len(broken[0]["_raw"]) <= 200,
          "invalid JSON recorded with truncated raw")

    try:
        extract_jsonld(None)  # type: ignore[arg-type]
        check(False, "extract_jsonld(None) should raise")
    except ToolError:
        check(True, "extract_jsonld(None) raises ToolError")

    # --- audit happy path / required fields -------------------------------
    full_org = {"@context": "https://schema.org", "@type": "Organization", "name": "Acme",
                "url": "https://acme.example", "logo": "https://acme.example/l.png",
                "sameAs": ["https://x.example/acme"], "description": "Acme does things.",
                "contactPoint": {"@type": "ContactPoint", "telephone": "+10000000"}}
    audited = audit_objects([full_org], today=today)
    check(audited["items"][0]["missing_required"] == [], "full org has no missing required")
    check(audited["items"][0]["missing_recommended"] == [], "full org has no missing recommended")
    check(audited["score"] == 100, "clean org scores 100")

    article = audit_objects([{"@type": "Article"}], today=today)
    check(article["items"][0]["missing_required"] == ["headline", "datePublished"],
          "article missing required fields listed")
    check(0 <= article["score"] < 100, "article is penalised")

    empty = audit_objects([], today=today)
    check(empty["score"] == 0 and empty["types"] == [] and empty["items"] == [], "empty audit")

    # --- warnings ---------------------------------------------------------
    short = audit_objects([make_faq([("What is AEO?", "Short.")])], today=today)
    check(any("too short" in w for w in short["items"][0]["warnings"]), "short FAQ answer warned")

    long_text = " ".join(["word"] * 320)
    long_faq = audit_objects([make_faq([("Q?", long_text)])], today=today)
    check(any("too long" in w for w in long_faq["items"][0]["warnings"]), "long FAQ answer warned")

    stale = {"@type": "BlogPosting", "headline": "H",
             "datePublished": "2020-01-01", "dateModified": "2023-01-01"}
    check(any("days old" in w for w in audit_objects([stale], today=today)["items"][0]["warnings"]),
          "stale dateModified warned")

    undated = {"@type": "BlogPosting", "headline": "H", "datePublished": "Jan 5 2024"}
    check(any("ISO-8601" in w for w in audit_objects([undated], today=today)["items"][0]["warnings"]),
          "non-ISO date warned")

    bad_price = {"@type": "Product", "name": "P", "offers": {"@type": "Offer", "price": "free"}}
    check(any("not numeric" in w for w in audit_objects([bad_price], today=today)["items"][0]["warnings"]),
          "non-numeric price warned")
    good_price = {"@type": "Product", "name": "P", "offers": {"@type": "Offer", "price": "9.99"}}
    check(not any("not numeric" in w for w in audit_objects([good_price], today=today)["items"][0]["warnings"]),
          "numeric price accepted")

    insecure = {"@type": "Organization", "name": "A", "url": "https://a.example",
                "sameAs": ["http://a.example"]}
    check(any("non-https" in w for w in audit_objects([insecure], today=today)["items"][0]["warnings"]),
          "http sameAs warned")

    rated = {"@type": "SoftwareApplication", "name": "S", "applicationCategory": "BusinessApplication",
             "operatingSystem": "Any",
             "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5"}}
    check(any("ratingCount" in w for w in audit_objects([rated], today=today)["items"][0]["warnings"]),
          "aggregateRating without counts warned")

    duplicated = audit_objects([{"@type": "Organization", "name": "A", "url": "https://a.example"},
                                {"@type": "Organization", "name": "B", "url": "https://b.example"}],
                               today=today)
    check(any("duplicate Organization" in w for w in duplicated["items"][0]["warnings"]),
          "conflicting Organization blocks warned")

    junk = audit_objects([{"@type": "Mystery"}, "not-an-object", {"_error": "boom"}], today=today)
    check(0 <= junk["score"] <= 100, "garbage input keeps score bounded")

    # --- generators -------------------------------------------------------
    org = make_org("Acme", "https://acme.example", logo="https://acme.example/l.png",
                   same_as=["https://x.example/acme"])
    check(audit_objects([org], today=today)["items"][0]["missing_required"] == [],
          "make_org passes required")

    sw = make_software("AcmeApp", "https://acme.example/app", "Does a thing.", version="1.2.3")
    check(audit_objects([sw], today=today)["items"][0]["missing_required"] == [],
          "make_software passes required")
    check(sw["offers"]["price"] == "0" and sw["offers"]["priceCurrency"] == "USD",
          "make_software defaults")

    faq = make_faq([("Q1?", "Answer one that is long enough to clear the ten word minimum easily.")])
    check(audit_objects([faq], today=today)["items"][0]["missing_required"] == [],
          "make_faq passes required")

    crumbs = make_breadcrumbs([("Home", "https://acme.example/"),
                               ("Blog", "https://acme.example/blog")])
    check(audit_objects([crumbs], today=today)["items"][0]["missing_required"] == [],
          "make_breadcrumbs passes required")
    check(crumbs["itemListElement"][1]["position"] == 2, "breadcrumb positions are 1-based")

    end_to_end = audit_html('<script type="application/ld+json">'
                            '{"@type":"WebSite","name":"S","url":"https://s.example"}</script>', today=today)
    check(end_to_end["types"] == ["WebSite"] and end_to_end["items"][0]["missing_required"] == [],
          "audit_html end-to-end")

    # --- generator refusals ----------------------------------------------
    try:
        make_software("N", "https://n.example", "d", price="free")
        check(False, "bad price should raise")
    except ToolError:
        check(True, "non-numeric price raises")

    try:
        make_software("N", "https://n.example", "d", currency="US")
        check(False, "bad currency should raise")
    except ToolError:
        check(True, "bad currency raises")

    try:
        make_faq([])
        check(False, "empty faq should raise")
    except ToolError:
        check(True, "empty faq raises")

    try:
        make_faq([("only-a-question",)])
        check(False, "malformed pair should raise")
    except ToolError:
        check(True, "malformed QA pair raises")

    try:
        make_org("A", "https://a.example", same_as=["ftp://a.example"])
        check(False, "non-https same_as should raise")
    except ToolError:
        check(True, "non-https same_as raises")

    try:
        make_breadcrumbs([("Home", "nope")])
        check(False, "bad breadcrumb url should raise")
    except ToolError:
        check(True, "bad breadcrumb url raises")

    try:
        audit_objects("nope")
        check(False, "non-list objs should raise")
    except ToolError:
        check(True, "non-list objs raises")

    # --- script tag -------------------------------------------------------
    tag = to_script_tag({"@type": "Thing", "name": "</script><b>x</b>"})
    check(tag.startswith('<script type="application/ld+json">') and tag.endswith("</script>"),
          "script tag shape")
    check("</script><b>" not in tag, "closing script escaped inside payload")
    inner = tag[len('<script type="application/ld+json">'):-len("</script>")]
    check(json.loads(inner)["name"] == "</script><b>x</b>", "script payload round-trips")

    try:
        to_script_tag({"x": object()})
        check(False, "non-serialisable object should raise")
    except ToolError:
        check(True, "non-serialisable object raises")

    # --- _g helper --------------------------------------------------------
    check(_g({"a": {"b": [1, 2]}}, "a", "b", 1) == 2, "_g walks nested lists")
    check(_g({"a": {}}, "a", "missing", default=0) == 0, "_g returns default")
    check(_g(None, "a") is None, "_g tolerates non-dicts")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
