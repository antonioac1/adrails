"""Local-business presence helpers for AdRails.

Builds schema.org LocalBusiness JSON-LD, checks NAP consistency across fetched
pages, and drafts service-area page briefs.  All functions are pure: callers
fetch HTML/text and pass it in; nothing here touches the network or disk.
Safety properties: no secrets in outputs, stable JSON-friendly returns, money
never invented, and no self-serving aggregateRating markup is emitted.
A tool should call these helpers, wrap failures in ToolError/RailViolation,
and never print to stdout.
"""

import json
import re
import unicodedata

from adsops.errors import ToolError, RailViolation, scrub

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_ALIAS = {
    "mo": 0, "mon": 0, "monday": 0,
    "tu": 1, "tue": 1, "tuesday": 1,
    "we": 2, "wed": 2, "wednesday": 2,
    "th": 3, "thu": 3, "thursday": 3,
    "fr": 4, "fri": 4, "friday": 4,
    "sa": 5, "sat": 5, "saturday": 5,
    "su": 6, "sun": 6, "sunday": 6,
}
_TYPES = {
    "Plumber": "Plumber",
    "Electrician": "Electrician",
    "HVACBusiness": "HVACBusiness",
    "RoofingContractor": "RoofingContractor",
    "GeneralContractor": "GeneralContractor",
    "HousePainter": "HousePainter",
    "Locksmith": "Locksmith",
    "MovingCompany": "MovingCompany",
    "PestControl": ("HomeAndConstructionBusiness", "https://schema.org/PestControl"),
    "CleaningService": ("HomeAndConstructionBusiness", "https://schema.org/CleaningService"),
    "AutoRepair": "AutoRepair",
    "LegalService": "LegalService",
    "Dentist": "Dentist",
    "RealEstateAgent": "RealEstateAgent",
    "ProfessionalService": "ProfessionalService",
    "LocalBusiness": "LocalBusiness",
}
_ADDR_ABBR = {
    "st": "street", "ave": "avenue", "rd": "road", "dr": "drive",
    "blvd": "boulevard", "ln": "lane", "ct": "court", "ste": "suite",
    "suite": "suite", "n": "north", "s": "south", "e": "east", "w": "west",
    "pl": "place", "pkwy": "parkway", "hwy": "highway", "sq": "square",
}
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)


def _g(row, *path, default=None):
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
        if cur is None:
            return default
    return cur


def _clean(v):
    return str(v).strip() if v is not None else ""


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_text(s):
    s = str(s or "").lower()
    s = re.sub(r"'s\b", "", s)  # Bob's -> bob
    s = s.replace("&", " and ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


_SUFFIX = {"llc", "inc", "co", "corp", "ltd", "limited", "company", "incorporated"}


def _norm_name(s):
    return " ".join(t for t in _norm_text(s).split() if t not in _SUFFIX)


def _norm_phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def _norm_addr(s):
    return " ".join(_ADDR_ABBR.get(t, t) for t in _norm_text(s).split())


def _truncate(s, limit):
    s = " ".join(str(s or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut[:limit].rstrip(" -|,.;") or s[:limit]


def _slugify(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "local"


def _resolve_type(t):
    if t is None or (isinstance(t, str) and not t.strip()):
        return "LocalBusiness", None, None
    if not isinstance(t, str):
        return "LocalBusiness", None, "Unknown business type; defaulting to LocalBusiness"
    raw = t.strip()
    hit = _TYPES.get(raw)
    if hit is None:
        low = raw.lower()
        for k, v in _TYPES.items():
            if k.lower() == low:
                hit = v
                break
    if hit is None:
        return "LocalBusiness", None, f"Unknown business type '{raw}'; defaulting to LocalBusiness"
    if isinstance(hit, tuple):
        return hit[0], hit[1], None
    return hit, None, None


def _time_range(v):
    if not isinstance(v, str):
        raise ToolError("hours values must be strings")
    s = v.strip().lower()
    if s in ("24/7", "24-7", "24x7", "24/7/365"):
        return "00:00", "23:59"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", s)
    if not m:
        raise ToolError(f"invalid hours value {v!r}; use HH:MM-HH:MM or 24/7")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ToolError(f"invalid hours value {v!r}")
    if h1 * 60 + m1 >= h2 * 60 + m2:
        raise ToolError(f"invalid hours value {v!r}: opening must be before closing")
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"


def _hours_specs(hours):
    if not isinstance(hours, dict):
        raise ToolError("hours must be an object")
    day_times = {}
    for key, val in hours.items():
        if not isinstance(key, str):
            raise ToolError("hours keys must be strings")
        k = key.strip().lower()
        if k in ("24/7", "24-7", "24x7"):
            days = list(range(7))
            open_, close_ = _time_range("24/7")
        else:
            days = []
            for grp in k.split(","):
                grp = grp.strip()
                if not grp:
                    continue
                if "-" in grp:
                    a, b = grp.split("-", 1)
                    ia, ib = _DAY_ALIAS.get(a.strip()), _DAY_ALIAS.get(b.strip())
                    if ia is None or ib is None:
                        raise ToolError(f"invalid hours key {key!r}")
                    days.extend(range(ia, ib + 1) if ia <= ib else list(range(ia, 7)) + list(range(0, ib + 1)))
                else:
                    i = _DAY_ALIAS.get(grp)
                    if i is None:
                        raise ToolError(f"invalid hours key {key!r}")
                    days.append(i)
            if not days:
                raise ToolError(f"invalid hours key {key!r}")
            open_, close_ = _time_range(val)
        for i in days:
            day_times[i] = (open_, close_)
    groups = {}
    for i in range(7):
        if i in day_times:
            groups.setdefault(day_times[i], []).append(_DAYS[i])
    return [
        {"@type": "OpeningHoursSpecification", "dayOfWeek": days, "opens": t[0], "closes": t[1]}
        for t, days in groups.items()
    ]


def schema(biz):
    """Return LocalBusiness JSON-LD, a script tag, and warnings."""
    if not isinstance(biz, dict):
        raise ToolError("biz must be an object")
    name = biz.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolError("biz.name is required")
    name = name.strip()
    warnings = []
    stype, additional, tw = _resolve_type(biz.get("type"))
    if tw:
        warnings.append(tw)

    data = {"@context": "https://schema.org", "@type": stype, "name": name}
    if additional:
        data["additionalType"] = additional

    url = biz.get("url")
    if isinstance(url, str) and url.strip():
        url = url.strip()
        data["url"] = url
        data["@id"] = url + "#business"
        if not url.startswith("https://"):
            warnings.append("url should use https")
    else:
        warnings.append("url missing; add an https URL for @id")

    phone = biz.get("phone")
    if phone not in (None, ""):
        data["telephone"] = str(phone).strip()
    email = biz.get("email")
    if email not in (None, ""):
        data["email"] = str(email).strip()

    addr = biz.get("address")
    if addr is not None and not isinstance(addr, dict):
        raise ToolError("biz.address must be an object")
    addr = addr or {}
    street = _clean(addr.get("street") or addr.get("streetAddress"))
    city = _clean(addr.get("city") or addr.get("addressLocality"))
    region = _clean(addr.get("region") or addr.get("addressRegion"))
    postal = _clean(addr.get("postal") or addr.get("postalCode"))
    country = _clean(addr.get("country") or addr.get("addressCountry")) or "US"
    if street:
        pa = {"@type": "PostalAddress", "streetAddress": street}
        if city:
            pa["addressLocality"] = city
        if region:
            pa["addressRegion"] = region
        if postal:
            pa["postalCode"] = postal
        pa["addressCountry"] = country
        data["address"] = pa

    areas = biz.get("areas") or []
    if isinstance(areas, str):
        areas = [areas]
    if not isinstance(areas, list):
        raise ToolError("biz.areas must be a list")
    served = []
    for a in areas:
        if not isinstance(a, str) or not a.strip():
            continue
        a = a.strip()
        if re.fullmatch(r"\d{5}(?:-\d{4})?", a):
            served.append({"@type": "PostalCode", "name": a, "postalCode": a})
        else:
            served.append({"@type": "City", "name": a})
    if served:
        data["areaServed"] = served
    if not street and not served:
        warnings.append("No street address or areas served provided; add at least one for local SEO")

    services = biz.get("services") or []
    if isinstance(services, (str, dict)):
        services = [services]
    if not isinstance(services, list):
        raise ToolError("biz.services must be a list")
    items = []
    for s in services:
        if isinstance(s, str):
            nm, desc, pr = s.strip(), "", None
        elif isinstance(s, dict):
            nm = _clean(s.get("name"))
            desc = _clean(s.get("description"))
            pr = s.get("price_range")
        else:
            continue
        if not nm:
            continue
        svc = {"@type": "Service", "name": nm}
        if desc:
            svc["description"] = desc
        offer = {"@type": "Offer", "itemOffered": svc}
        if pr not in (None, ""):
            offer["description"] = f"Price range: {pr}"
        items.append(offer)
    if items:
        data["hasOfferCatalog"] = {"@type": "OfferCatalog", "name": "Services", "itemListElement": items}

    hours = biz.get("hours")
    if hours not in (None, {}):
        specs = _hours_specs(hours)
        if specs:
            data["openingHoursSpecification"] = specs

    pr = biz.get("price_range") or biz.get("priceRange")
    if pr not in (None, ""):
        data["priceRange"] = str(pr)
    for key in ("logo", "image"):
        val = biz.get(key)
        if isinstance(val, str) and val.strip():
            data[key] = val.strip()

    sa = biz.get("same_as") or biz.get("sameAs")
    if isinstance(sa, str):
        sa = [sa]
    if isinstance(sa, list):
        vals = [str(x).strip() for x in sa if isinstance(x, str) and x.strip()]
        if vals:
            data["sameAs"] = vals

    geo = biz.get("geo")
    if geo not in (None, {}):
        if not isinstance(geo, dict):
            warnings.append("geo must be an object")
        else:
            lat = geo.get("lat", geo.get("latitude"))
            lng = geo.get("lng", geo.get("longitude"))
            try:
                latf, lngf = float(lat), float(lng)
            except (TypeError, ValueError):
                warnings.append("geo must include numeric lat and lng")
            else:
                data["geo"] = {"@type": "GeoCoordinates", "latitude": latf, "longitude": lngf}

    founded = biz.get("founded")
    if founded not in (None, ""):
        data["foundingDate"] = str(founded)

    lic = biz.get("license")
    if lic:
        ln = _clean(lic.get("name") or lic.get("id")) if isinstance(lic, dict) else _clean(lic)
        if ln:
            data["hasCredential"] = {"@type": "EducationalOccupationalCredential", "name": ln}

    if biz.get("rating") not in (None, ""):
        warnings.append(
            "aggregateRating omitted: Google ignores/penalises self-serving review markup on your own site"
        )

    body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    script = '<script type="application/ld+json">' + body + "</script>"
    return {"jsonld": data, "script": script, "warnings": warnings}


def _canon_parts(canonical):
    addr = canonical.get("address")
    street = city = postal = ""
    if isinstance(addr, dict):
        street = _clean(addr.get("street") or addr.get("streetAddress"))
        city = _clean(addr.get("city") or addr.get("addressLocality"))
        postal = _clean(addr.get("postal") or addr.get("postalCode"))
    elif isinstance(addr, str):
        street = addr
    street = street or _clean(canonical.get("street"))
    city = city or _clean(canonical.get("city"))
    postal = postal or _clean(canonical.get("postal"))
    return street, city, postal


def _address_match(street, city, postal, page_norm):
    required = []
    if street:
        toks = _norm_addr(street).split()
        if toks:
            if toks[0].isdigit():
                required.append(toks[0])
                toks = toks[1:]
            if toks:
                required.append(toks[0])
        if postal:
            digits = re.sub(r"\D", "", postal)[:5]
            if digits:
                required.append(digits)
    else:
        if city:
            required.append(_norm_text(city))
        if postal:
            digits = re.sub(r"\D", "", postal)[:5]
            if digits:
                required.append(digits)
    return all(p in page_norm for p in required) if required else True


def _phones_in(text):
    out = []
    for m in _PHONE_RE.finditer(text):
        p = _norm_phone(m.group(0))
        if p and p not in out:
            out.append(p)
    return out


def nap_check(canonical, pages):
    """Compare canonical NAP against fetched page text; return per-page issues and score."""
    if not isinstance(canonical, dict):
        raise ToolError("canonical must be an object")
    cname_raw = canonical.get("name")
    if not isinstance(cname_raw, str) or not cname_raw.strip():
        raise ToolError("canonical.name is required")
    if not isinstance(pages, list):
        raise ToolError("pages must be a list")
    if len(pages) > 200:
        raise RailViolation("refusing to check more than 200 pages at once")

    cname = _norm_name(cname_raw)
    cphone = _norm_phone(canonical.get("phone"))
    street, city, postal = _canon_parts(canonical)
    results = []
    consistent = 0

    for p in pages:
        if not isinstance(p, dict):
            p = {"url": "", "text": str(p)}
        url = str(p.get("url") or "")
        text = p.get("text") or ""
        if not isinstance(text, str):
            text = str(text)
        page_norm = _norm_addr(text)
        name_found = bool(cname and cname in _norm_name(text))
        phones = _phones_in(text)
        phone_found = True if not cphone else cphone in phones
        different = bool(cphone and any(ph != cphone for ph in phones))
        address_found = _address_match(street, city, postal, page_norm)
        issues = []
        if not name_found:
            issues.append("business name not found")
        if cphone and not phone_found:
            issues.append("canonical phone not found")
        if different:
            issues.append("different phone number listed")
        if not address_found:
            issues.append("address not found")
        if not issues:
            consistent += 1
        results.append({
            "url": url,
            "name_found": name_found,
            "phone_found": phone_found,
            "address_found": address_found,
            "phones_seen": phones,
            "issues": issues,
        })

    score = int(round(100 * consistent / len(pages))) if pages else 0
    summary = "No pages checked" if not pages else (
        f"{score}/100: {consistent}/{len(pages)} pages fully consistent with canonical NAP"
    )
    return {"results": results, "score": score, "summary": summary}


def area_page_brief(biz, area, service, competitors=None):
    """Draft a unique service-area page brief with FAQ and schema hints."""
    if not isinstance(biz, dict):
        raise ToolError("biz must be an object")
    area = _clean(area)
    service = _clean(service)
    if not area:
        raise ToolError("area is required")
    if not service:
        raise ToolError("service is required")
    name = _clean(biz.get("name")) or "Your business"
    phone = _clean(biz.get("phone"))

    slug = _slugify(service) + "-" + _slugify(area)
    title = _truncate(f"{service.title()} in {area} | {name}", 60)
    meta = f"{name} provides {service} in {area}. Licensed, insured, local."
    if phone:
        meta += f" Call {phone}."
    meta += " See services, service area, pricing, and FAQs."
    meta = _truncate(meta, 155)
    h1 = f"{service.title()} in {area}"

    outline = [
        f"{service.title()} services in {area}",
        f"Service area and response times around {area}",
        f"Pricing and estimates for {service}",
        f"Licensing, insurance, and permits in {area}",
        f"Why choose {name}",
        f"Recent local jobs and customer reviews",
        f"Frequently asked questions about {service} in {area}",
    ]
    if competitors:
        outline.insert(6, f"How {name} compares to other {service} providers in {area}")

    faq = [
        {"q": f"How much does {service} cost in {area}?",
         "a_hint": "Give a real range or estimate policy, not a fake exact price."},
        {"q": f"Who is the best {service} in {area}?",
         "a_hint": "Answer with proof: license, reviews, local jobs, response time."},
        {"q": f"Do I need a permit for {service} in {area}?",
         "a_hint": "Name the local authority and when permits are typically required."},
        {"q": f"How fast can a {service} come out in {area}?",
         "a_hint": "State real response windows and any emergency availability."},
        {"q": f"What areas near {area} do you serve?",
         "a_hint": "List actual cities/ZIPs you serve; avoid fake city lists."},
        {"q": f"Are you licensed and insured in {area}?",
         "a_hint": "Include license number/state and insurance confirmation."},
    ]

    return {
        "slug": slug,
        "title": title,
        "meta_description": meta,
        "h1": h1,
        "outline": outline,
        "faq": faq,
        "schema_hint": (
            "Use presence.schema LocalBusiness JSON-LD with areaServed, plus FAQPage JSON-LD "
            "for these questions. Do not add aggregateRating from self-reported reviews."
        ),
        "must_include": [
            "Consistent NAP (name, address, phone)",
            "License number and state",
            "Real photos of local jobs",
            "Link to reviews (Google Business Profile)",
            "Price ranges or estimate policy",
            "Service-area map",
        ],
        "avoid": [
            "Doorway pages: don't clone this page per city with only the name swapped",
            "Fake reviews or self-serving aggregateRating markup",
            "Keyword-stuffed city lists",
            "Duplicate content across service-area pages",
        ],
    }


def citations_checklist(biz_type):
    """Ordered US citation/listing checklist for local service businesses."""
    bt = _clean(biz_type).lower()
    home = any(k in bt for k in (
        "plumb", "electric", "hvac", "roof", "contractor", "paint", "lock",
        "moving", "pest", "clean", "handyman", "auto repair", "remodel",
    ))
    rows = [
        ("Google Business Profile", "https://business.google.com/", "high",
         "Primary source for Maps, local pack, and AI local answers."),
        ("Bing Places", "https://www.bingplaces.com/", "high",
         "Read by Bing/Copilot and helps validate business identity."),
        ("Apple Business Connect", "https://businessconnect.apple.com/", "high",
         "Powers Apple Maps/Siri; increasingly read by AI assistants."),
        ("Yelp", "https://biz.yelp.com/", "high",
         "Strong local discovery and review corpus for service queries."),
        ("Facebook", "https://www.facebook.com/business/", "medium",
         "Profile and local page reinforce NAP and social proof."),
        ("BBB", "https://www.bbb.org/", "medium",
         "Trust signal and business identity citation."),
        ("Nextdoor", "https://business.nextdoor.com/", "medium",
         "Neighborhood recommendations and local service discovery."),
        ("Angi", "https://www.angi.com/", "high",
         "High-intent home-service marketplace citation."),
        ("Thumbtack", "https://www.thumbtack.com/pro", "medium",
         "Lead marketplace with local service categories."),
        ("Houzz", "https://www.houzz.com/pro", "medium",
         "Home trades only: project photos and professional profile."),
        ("Yellow Pages", "https://www.yellowpages.com/", "low",
         "Legacy citation; still used by some data aggregators."),
        ("Local chamber of commerce", "https://www.uschamber.com/", "medium",
         "Local trust and community citation; find the city chamber."),
        ("State license lookup", "https://www.usa.gov/state-licenses", "high",
         "Authoritative license verification for contractor trades."),
    ]
    out = []
    for site, url, priority, why in rows:
        if site == "Houzz" and not home:
            continue
        out.append({"site": site, "url": url, "priority": priority, "why": why})
    return out


def selftest() -> int:
    n = 0

    def ck(cond, msg=""):
        nonlocal n
        if not cond:
            raise AssertionError(msg)
        n += 1

    b = {
        "name": "Acme Plumbing",
        "type": "Plumber",
        "url": "https://acme.example",
        "phone": "(555) 123-4567",
        "areas": ["Springfield", "12345"],
        "hours": {"mo-fr": "08:00-18:00", "sa": "09:00-13:00"},
        "services": ["Drain cleaning", {"name": "Water heater", "description": "Tank and tankless", "price_range": "$150-$1200"}],
        "rating": 4.9,
    }
    r = schema(b)
    j = r["jsonld"]
    ck(j["@type"] == "Plumber")
    ck("address" not in j)
    ck(j["areaServed"][0] == {"@type": "City", "name": "Springfield"})
    ck(j["areaServed"][1]["@type"] == "PostalCode")
    ck(j["@id"] == "https://acme.example#business")
    ck(any("aggregateRating" in w for w in r["warnings"]))
    ck("aggregateRating" not in j)
    ck(len(j["openingHoursSpecification"]) == 2)
    ck(j["openingHoursSpecification"][0]["dayOfWeek"] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    ck(j["hasOfferCatalog"]["itemListElement"][1]["itemOffered"]["name"] == "Water heater")

    r24 = schema({"name": "X", "type": "LocalBusiness", "hours": {"24/7": "24/7"}})
    spec = r24["jsonld"]["openingHoursSpecification"][0]
    ck(spec["opens"] == "00:00" and spec["closes"] == "23:59")
    ck(len(spec["dayOfWeek"]) == 7)

    try:
        schema({"name": "X", "hours": {"mo": "8am-5pm"}})
        ck(False, "bad hours should raise")
    except ToolError:
        ck(True)

    ru = schema({"name": "X", "type": "Wizardry"})
    ck(ru["jsonld"]["@type"] == "LocalBusiness")
    ck(any("Unknown business type" in w for w in ru["warnings"]))

    rs = schema({"name": "A </script><x>", "type": "Plumber", "url": "https://x.example"})
    ck(rs["script"].count("</script>") == 1)

    try:
        schema({"type": "Plumber"})
        ck(False, "missing name should raise")
    except ToolError:
        ck(True)

    rh = schema({"name": "X", "url": "http://x.example"})
    ck(any("https" in w for w in rh["warnings"]))

    canon = {
        "name": "Acme Plumbing LLC",
        "phone": "555-123-4567",
        "street": "123 N Main St",
        "city": "Springfield",
        "postal": "12345",
    }
    pages = [
        {"url": "a", "text": "Acme Plumbing - call (555) 123-4567. 123 North Main Street, Springfield 12345"},
        {"url": "b", "text": "Acme Plumbing 555.999.0000 at 123 N Main St 12345"},
    ]
    res = nap_check(canon, pages)
    ck(res["results"][0]["name_found"])
    ck(res["results"][0]["phone_found"])
    ck(res["results"][0]["address_found"])
    ck(res["results"][0]["issues"] == [])
    ck(res["results"][1]["phone_found"] is False)
    ck(any("different phone" in i for i in res["results"][1]["issues"]))
    ck(res["score"] == 50)

    ck(nap_check(canon, [])["score"] == 0)
    ck(nap_check(canon, [])["summary"] == "No pages checked")
    ck(_norm_phone("+1 (555) 123-4567") == "5551234567")
    ck(_norm_phone("555.123.4567") == "5551234567")
    ck(nap_check(canon, [{"url": "x"}])["results"][0]["name_found"] is False)

    try:
        nap_check(canon, [{"url": "", "text": ""}] * 201)
        ck(False, "page rail should fire")
    except RailViolation:
        ck(True)

    br = area_page_brief({"name": "Acme Plumbing", "phone": "555-123-4567"}, "St. Louis", "water heater repair", competitors=["Other"])
    ck(len(br["title"]) <= 60)
    ck(len(br["meta_description"]) <= 155)
    ck(br["slug"] == "water-heater-repair-st-louis")
    ck(len(br["faq"]) == 6)
    ck(any("How much" in f["q"] for f in br["faq"]))
    ck(any("doorway" in a.lower() for a in br["avoid"]))
    try:
        area_page_brief({"name": "X"}, "", "repair")
        ck(False, "empty area should raise")
    except ToolError:
        ck(True)

    cl = citations_checklist("Plumber")
    ck(cl[0]["site"] == "Google Business Profile")
    ck(any(x["site"] == "Houzz" for x in cl))
    ck(any(x["site"] == "Apple Business Connect" and x["priority"] == "high" for x in cl))
    ck(all(set(x) == {"site", "url", "priority", "why"} for x in cl))
    ck(not any(x["site"] == "Houzz" for x in citations_checklist("Dentist")))
    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
