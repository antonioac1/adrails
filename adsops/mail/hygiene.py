"""adsops.mail.hygiene — direct-mail list hygiene (normalise, suppress, label).

Guards a print program against the classic expensive failures: truncated ZIP+4
or unit lines, foreign addresses stamped domestically, institutional owners,
the advertiser's own buildings and current clients getting mailed, and stale
suppression lists. Everything here is pure: normalise a record, classify it,
render a label that never truncates, then ``clean()`` a whole list and get back
the kept records (tagged ``_key``/``_label``) plus a per-reason drop audit.

Callers persist the JSON-serialisable result; no I/O, no secrets, no stdout.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from adsops.errors import ToolError, RailViolation, scrub  # noqa: F401  (shared error contract)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "DC PR VI GU AS MP AA AE AP".split()
)

# States where a bare 4-digit ZIP lost its leading zero (USPS 0xxxx range).
LEADING_ZERO_STATES = frozenset("CT MA ME NH NJ RI VT PR VI".split())

# Countries treated as domestic after stripping punctuation/spaces.
DOMESTIC_COUNTRIES = frozenset({"", "US", "USA", "UNITEDSTATES"})

ABBREV = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "ROAD": "RD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
    "PLACE": "PL", "TERRACE": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT",
}

DEFAULT_DENY = (
    r"\bBANK\b",
    r"\bMORTGAGE\b",
    r"\bFEDERAL (HOME|NATIONAL)\b",
    r"\bHOUSING AUTHORITY\b",
    r"\bCOUNTY OF\b|\bCITY OF\b|\bSTATE OF\b",
    r"\bSCHOOL BOARD\b",
    r"\b(INVITATION|AMERICAN) HOMES\b",
    r"\bTRICON\b",
    r"\bPROGRESS RESIDENTIAL\b",
    r"\bHOME PARTNERS\b",
    r"\bFIRSTKEY\b",
    r"\bREIT\b",
    r"\bHOMEOWNERS? ASS(N|OCIATION)\b",
    r"\bCONDOMINIUM ASS(N|OCIATION)\b",
    r"\bCHURCH\b",
)

SUPPRESSION_LISTS = ("clients", "leads", "do_not_mail")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Nested dict getter used by callers/tests; missing or None -> default."""
    cur = row
    for step in path:
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(step)
        if cur is None:
            return default
    return default if cur is None else cur


def _canon(text) -> str:
    """Uppercase, expand '#', drop punctuation, collapse spaces, USPS-abbreviate."""
    s = str(text or "").upper().replace("#", " UNIT ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    return " ".join(ABBREV.get(tok, tok) for tok in s.split(" "))


def _state(value) -> str:
    return str(value or "").strip().upper()


def _country(value) -> str:
    """'U.S.A.' / 'united states' / 'US' -> 'USA' / 'UNITEDSTATES' / 'US'."""
    return re.sub(r"[^A-Z]", "", str(value or "").upper())


def _parse_zip(raw, state="") -> tuple[str, str]:
    """Return (zip5, zip4); ('', '') when the ZIP cannot be trusted."""
    st = _state(state)
    s = str(raw or "").strip()
    if not s:
        return "", ""
    if "-" in s:
        head, _, tail = s.partition("-")
        a = re.sub(r"\D", "", head)
        b = re.sub(r"\D", "", tail)
        if len(a) == 4 and st in LEADING_ZERO_STATES:
            a = "0" + a
        zip5 = a[:5]
        zip4 = b[:4] if len(b) >= 4 else ""
    else:
        d = re.sub(r"\D", "", s)
        if len(d) == 9:
            zip5, zip4 = d[:5], d[5:]
        elif len(d) == 5:
            zip5, zip4 = d, ""
        elif len(d) == 4 and st in LEADING_ZERO_STATES:
            # "2134" in MA is really "02134"; in FL it is a data error.
            zip5, zip4 = "0" + d, ""
        else:
            return "", ""
    if len(zip5) != 5:
        return "", ""
    return zip5, zip4


# --------------------------------------------------------------------------- #
# normalisation / classification
# --------------------------------------------------------------------------- #
def normalize_address(rec) -> dict:
    """Canonical view of a mailing record plus the dedupe/suppression key."""
    rec = rec or {}
    line1 = _canon(_g(rec, "address_line_1") or _g(rec, "line1"))
    line2 = _canon(_g(rec, "address_line_2") or _g(rec, "line2"))
    city = _canon(rec.get("city"))
    state = _state(rec.get("state"))
    zip5, zip4 = _parse_zip(rec.get("zip") or rec.get("zip5"), state)
    return {
        "name": _canon(rec.get("name")),
        "line1": line1,
        "line2": line2,
        "city": city,
        "state": state,
        "zip5": zip5,
        "zip4": zip4,
        "country": _country(rec.get("country")),
        "key": f"{line1}|{line2}|{zip5}",
    }


def is_domestic(rec) -> bool:
    """True only for a US/territory country, valid state code and 5-digit ZIP."""
    n = normalize_address(rec)
    return (
        n["country"] in DOMESTIC_COUNTRIES
        and n["state"] in US_STATES
        and len(n["zip5"]) == 5
    )


def classify_owner(name, deny_patterns=DEFAULT_DENY):
    """Return 'institutional_owner: <pattern>' or None. Plain LLC/TRUST pass."""
    text = re.sub(r"\s+", " ", str(name or "").upper())
    if not text:
        return None
    for pat in (deny_patterns or ()):
        try:
            hit = re.search(pat, text, re.IGNORECASE)
        except re.error as exc:  # a caller-supplied bad regex is a user error
            raise ToolError("bad deny pattern: " + scrub(str(exc), 120))
        if hit:
            # human-readable: report the matched words, not the regex (the maintainers 2026-11-05)
            return f"institutional_owner: {hit.group(0).upper()}"
    return None


# --------------------------------------------------------------------------- #
# label rendering
# --------------------------------------------------------------------------- #
def _wrap(text: str, width: int) -> list:
    """Greedy word wrap; an over-long single token keeps its own full line."""
    words = str(text or "").split()
    out, cur = [], ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def label_lines(rec, max_chars=40, max_lines=5) -> list:
    """Render label lines, wrapping (never truncating). Raises on overflow."""
    rec = rec or {}
    width = max(1, int(max_chars))
    limit = max(1, int(max_lines))
    name = str(rec.get("name") or "").strip()
    l1 = str(_g(rec, "address_line_1") or _g(rec, "line1") or "").strip()
    l2 = str(_g(rec, "address_line_2") or _g(rec, "line2") or "").strip()
    city = str(rec.get("city") or "").strip()
    st = _state(rec.get("state"))
    zip5, zip4 = _parse_zip(rec.get("zip") or rec.get("zip5"), st)
    zc = (zip5 + ("-" + zip4 if zip4 else "")) or str(rec.get("zip") or "").strip()
    cityline = " ".join(x for x in (city, st, zc) if x)
    country = str(rec.get("country") or "").strip()

    base = [p for p in (name, l1, l2, cityline) if p]
    if country and _country(country) not in DOMESTIC_COUNTRIES:
        base.append(country)

    lines = []
    for part in base:
        lines.extend(_wrap(part, width))
    if len(lines) > limit:
        raise ToolError("label overflow")
    return lines


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def clean(records, suppress=None, deny_patterns=DEFAULT_DENY, allow_foreign=False,
          dedupe="address", own_addresses=()) -> dict:
    """Filter a mailing list; every drop carries a machine-readable reason."""
    records = list(records or [])
    supp = suppress or {}

    # Suppression index: canonical address key and canonical name+zip5, both
    # normalised exactly like inbound records (see normalize_address).
    supp_addr, supp_name = {}, {}
    for list_name in SUPPRESSION_LISTS:
        for srec in (supp.get(list_name) or []):
            sn = normalize_address(srec)
            if sn["line1"]:
                supp_addr.setdefault(sn["key"], list_name)
            if sn["name"] and sn["zip5"]:
                supp_name.setdefault(f"{sn['name']}|{sn['zip5']}", list_name)

    # Own properties: dicts normalise to a full key. Bare strings are treated
    # as canonical address keys per spec, and a raw line-1 value is also
    # accepted for convenience.
    own_keys, own_lines = set(), set()
    for entry in (own_addresses or ()):
        if isinstance(entry, Mapping):
            on = normalize_address(entry)
            if on["line1"]:
                own_keys.add(on["key"])
        else:
            text = str(entry or "").strip()
            if not text:
                continue
            if "|" in text:
                own_keys.add(text)
                own_keys.add(text.upper())
            else:
                canon = _canon(text)
                if canon:
                    own_lines.add(canon)

    kept, dropped, counts = [], [], {}

    def drop(index, reason, record_name):
        dropped.append({"index": index, "reason": reason,
                        "record_name": str(record_name or "")})
        counts[reason] = counts.get(reason, 0) + 1

    seen = set()
    for index, rec in enumerate(records):
        rec = rec or {}
        name = str(rec.get("name") or "").strip()
        raw = {
            "line1": str(_g(rec, "address_line_1") or _g(rec, "line1") or "").strip(),
            "city": str(rec.get("city") or "").strip(),
            "state": str(rec.get("state") or "").strip(),
            "zip": str(rec.get("zip") or rec.get("zip5") or "").strip(),
        }
        if not all(raw.values()):
            drop(index, "incomplete_address", name)
            continue

        n = normalize_address(rec)
        domestic_country = n["country"] in DOMESTIC_COUNTRIES
        if not domestic_country:
            if not allow_foreign:
                drop(index, "foreign", name)
                continue
        elif n["state"] not in US_STATES or len(n["zip5"]) != 5:
            drop(index, "invalid_zip", name)
            continue

        reason = classify_owner(name, deny_patterns)
        if reason:
            drop(index, reason, name)
            continue

        hit = supp_addr.get(n["key"])
        if not hit and n["name"] and n["zip5"]:
            hit = supp_name.get(f"{n['name']}|{n['zip5']}")
        if hit:
            drop(index, f"suppressed:{hit}", name)
            continue

        if n["key"] in own_keys or (n["line1"] and n["line1"] in own_lines):
            drop(index, "own_property", name)
            continue

        dkey = None
        if dedupe:
            dkey = (f"{n['name']}|{n['key']}" if dedupe == "name+address"
                    else n["key"])
            if dkey in seen:
                drop(index, "duplicate", name)
                continue

        try:
            label = label_lines(rec)
        except ToolError:
            drop(index, "label_overflow", name)
            continue

        if dkey is not None:
            seen.add(dkey)
        out = dict(rec)  # extra keys are the caller's business: keep untouched
        out["_key"] = n["key"]
        out["_label"] = label
        kept.append(out)

    counts["kept"] = len(kept)
    counts["input"] = len(records)
    return {"kept": kept, "dropped": dropped, "counts": counts}


def suppression_stats(suppress) -> dict:
    """Per-list counts plus a loud warning for any empty (stale) list."""
    supp = suppress or {}
    out, warnings = {}, []
    for list_name in SUPPRESSION_LISTS:
        count = len(supp.get(list_name) or [])
        out[list_name] = count
        if count == 0:
            warnings.append(
                f"{list_name} suppression list is EMPTY — mailing current "
                f"{list_name.replace('_', ' ')} is the #1 embarrassment risk"
            )
    out["warning"] = "; ".join(warnings)
    return out


# --------------------------------------------------------------------------- #
# selftest — every external effect mocked, no I/O, no network
# --------------------------------------------------------------------------- #
def selftest() -> int:
    checks = 0

    def ok(cond, msg="assertion failed"):
        nonlocal checks
        if not cond:
            raise AssertionError(msg)
        checks += 1

    # normalize_address -----------------------------------------------------
    a = normalize_address({"address_line_1": "123 Main Street", "address_line_2": "Apt #4",
                           "city": "Tampa", "state": "fl", "zip": "33602-1234"})
    ok(a["zip5"] == "33602" and a["zip4"] == "1234", "zip+4 split")
    ok(a["line1"] == "123 MAIN ST", "street abbreviation")
    ok(a["line2"] == "APT UNIT 4", "'#' -> UNIT, APARTMENT -> APT")
    ok(a["state"] == "FL", "state normalised upper")
    ok(a["key"] == "123 MAIN ST|APT UNIT 4|33602", "canonical key shape")

    b = normalize_address({"address_line_1": "Northeast Boulevard", "city": "X",
                           "state": "MA", "zip": "2134"})
    ok(b["line1"] == "NE BLVD", "composite directional + boulevard")
    ok(b["zip5"] == "02134", "lost leading zero restored in MA")
    ok(normalize_address({"address_line_1": "1 A St", "city": "X", "state": "FL",
                          "zip": "2134"})["zip5"] == "", "4-digit ZIP invalid outside NE")
    ok(normalize_address({})["key"] == "||", "empty record -> empty key")
    ok(normalize_address({"state": "FL", "zip": 33602})["zip5"] == "33602",
       "numeric zip coerced")

    # is_domestic -----------------------------------------------------------
    ok(is_domestic({"country": "", "state": "FL", "zip": "33602"}), "plain US domestic")
    ok(is_domestic({"country": "U.S.A.", "state": "PR", "zip": "00901"}),
       "territory domestic, punctuated country")
    ok(not is_domestic({"country": "Canada", "state": "ON", "zip": "M5V"}), "foreign")
    ok(not is_domestic({"country": "US", "state": "FL", "zip": "3360"}), "short zip")
    ok(not is_domestic({"country": "US", "state": "ZZ", "zip": "33602"}), "bad state")

    # classify_owner --------------------------------------------------------
    ok(classify_owner("WELLS FARGO BANK NA") is not None, "bank denied")
    ok(classify_owner("Smith Family Trust") is None, "plain TRUST allowed")
    ok(classify_owner("Sunrise Holdings LLC") is None, "plain LLC allowed")
    ok(classify_owner("Tricon Residential") is not None, "known institutional")
    ok(classify_owner("") is None, "empty name ok")
    ok(classify_owner("ACME", deny_patterns=(r"\bACME\b",)) is not None,
       "custom deny pattern honoured")

    # label_lines -----------------------------------------------------------
    long_rec = {"name": "Jonathan Q Public",
                "address_line_1": "1234 Very Long Street Name Boulevard",
                "address_line_2": "Apartment 55 Building C",
                "city": "Rancho Santa Margarita", "state": "CA", "zip": "92688-1234"}
    lab = label_lines(long_rec, max_chars=20, max_lines=12)
    ok(all(len(x) <= 20 for x in lab), "every label line within max_chars")
    ok("Jonathan Q Public" in " ".join(lab), "names never truncated")
    ok(any("92688-1234" in x for x in lab), "city/state/zip line present")

    overflowed = False
    try:
        label_lines(long_rec, max_chars=20, max_lines=3)
    except ToolError:
        overflowed = True
    ok(overflowed, "label overflow raises ToolError")
    ok(label_lines({"name": "X" * 50}, max_chars=10, max_lines=10)[0] == "X" * 50,
       "single long token kept whole")
    ok(label_lines({"name": "A B", "address_line_1": "1 Rue X", "city": "Paris",
                    "state": "IDF", "zip": "75001", "country": "France"})[-1]
       == "France", "country line appended for foreign")

    # clean -----------------------------------------------------------------
    recs = [
        {"name": "Jane Doe", "address_line_1": "123 Main Street", "city": "Tampa",
         "state": "FL", "zip": "33602-1234", "ref": "A1"},
        {"name": "Other Person", "address_line_1": "123 Main St", "city": "Tampa",
         "state": "FL", "zip": "33602"},
        {"name": "Wells Fargo Bank", "address_line_1": "9 Bank St", "city": "Tampa",
         "state": "FL", "zip": "33602"},
        {"name": "Pierre Dupont", "address_line_1": "1 Rue de Rivoli", "city": "Paris",
         "state": "IDF", "zip": "75001", "country": "France"},
        {"name": "No Zip", "address_line_1": "5 Nowhere Rd", "city": "Tampa",
         "state": "FL"},
    ]
    out = clean(recs)
    ok(out["counts"]["input"] == 5, "input counted")
    ok(out["counts"]["kept"] == 1, "exactly one survives")
    ok(out["kept"][0]["ref"] == "A1", "extra keys untouched")
    ok(out["kept"][0]["_key"] == "123 MAIN ST||33602", "kept record tagged _key")
    ok(isinstance(out["kept"][0]["_label"], list) and out["kept"][0]["_label"],
       "kept record tagged _label")
    ok(out["counts"].get("duplicate") == 1, "duplicate detected")
    ok(out["counts"].get("foreign") == 1, "foreign dropped")
    ok(out["counts"].get("incomplete_address") == 1, "incomplete dropped")
    ok(any(k.startswith("institutional_owner") for k in out["counts"]),
       "institutional owner dropped")
    ok([d["index"] for d in out["dropped"]] == [1, 2, 3, 4], "drop indexes in order")

    out_zip = clean([{"name": "Bad Zip", "address_line_1": "1 A St", "city": "Tampa",
                      "state": "FL", "zip": "2134"}])
    ok(out_zip["counts"].get("invalid_zip") == 1, "invalid ZIP dropped")

    out_far = clean([{"name": "Pierre", "address_line_1": "1 Rue de Rivoli",
                      "city": "Paris", "state": "IDF", "zip": "75001",
                      "country": "France"}], allow_foreign=True)
    ok(out_far["counts"]["kept"] == 1, "allow_foreign keeps overseas mail")

    supp = {
        "clients": [{"name": "Acme Corp", "address_line_1": "500 Client Avenue",
                     "city": "Tampa", "state": "FL", "zip": "33602"}],
        "leads": [{"name": "Lead Guy", "address_line_1": "77 Lead Ln",
                   "city": "Ocala", "state": "FL", "zip": "34470"}],
        "do_not_mail": [],
    }
    out_sup = clean([
        {"name": "Different Name", "address_line_1": "500 Client Ave",
         "city": "Tampa", "state": "FL", "zip": "33602"},
        {"name": "Acme Corp", "address_line_1": "999 Other Rd", "city": "Tampa",
         "state": "FL", "zip": "33602"},
    ], suppress=supp)
    ok(out_sup["counts"].get("suppressed:clients") == 2,
       "suppressed by address key and by name+zip5")

    out_own = clean([{"name": "Me", "address_line_1": "1 Owned St", "city": "Tampa",
                      "state": "FL", "zip": "33602"}],
                    own_addresses=[{"address_line_1": "1 Owned Street",
                                    "city": "Tampa", "state": "FL", "zip": "33602"}])
    ok(out_own["counts"].get("own_property") == 1, "own property blocked")

    own_key = normalize_address({"address_line_1": "1 Keyed St", "city": "Tampa",
                                 "state": "FL", "zip": "33602"})["key"]
    out_own_key = clean([{"name": "Me", "address_line_1": "1 Keyed St",
                          "city": "Tampa", "state": "FL", "zip": "33602"}],
                        own_addresses=(own_key,))
    ok(out_own_key["counts"].get("own_property") == 1, "own property by key blocked")

    duo = [{"name": "A", "address_line_1": "1 Same St", "city": "Tampa",
            "state": "FL", "zip": "33602"}] * 2
    ok(clean(duo, dedupe=None)["counts"]["kept"] == 2, "dedupe=None keeps both")
    ok(clean(duo)["counts"]["kept"] == 1, "dedupe=address collapses")
    duo2 = [dict(duo[0]), dict(duo[1], name="B")]
    ok(clean(duo2, dedupe="name+address")["counts"]["kept"] == 2,
       "name+address keeps distinct names")

    big = {"name": "WORD " * 60, "address_line_1": "1 A St", "city": "Tampa",
           "state": "FL", "zip": "33602"}
    ok(clean([big])["counts"].get("label_overflow") == 1,
       "label overflow surfaced through clean()")

    empty = clean([])
    ok(empty["counts"] == {"kept": 0, "input": 0}, "empty input handled")
    ok(clean([None, {}, {"name": None, "address_line_1": None}])["counts"]["kept"] == 0,
       "malformed rows do not crash")

    stats = suppression_stats({"clients": [], "leads": [{"name": "x"}],
                               "do_not_mail": []})
    ok(stats["clients"] == 0 and stats["leads"] == 1, "suppression counts")
    ok("clients" in stats["warning"], "empty clients list warns loudly")
    ok(suppression_stats({"clients": [{"a": 1}], "leads": [{"a": 1}],
                          "do_not_mail": [{"a": 1}]})["warning"] == "",
       "no warning when all lists populated")

    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)