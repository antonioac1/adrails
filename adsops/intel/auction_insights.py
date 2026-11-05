"""Google Ads Auction Insights importer (free source: our own account's report, exported or pasted).

Auction Insights names the advertisers that compete with us in the same auctions. The Google Ads API cannot
read it, so a human (or the founder's Chrome session) downloads the CSV or pastes the table, and this module
imports it. Column semantics: https://support.google.com/google-ads/answer/2579754

    python3 -m adsops.intel.auction_insights import --config CFG --file report.csv|table.txt|- \\
        [--level campaign|ad_group|keyword] [--campaign NAME] [--range 2026-10-07..2026-10-31]
    python3 -m adsops.intel.auction_insights show --config CFG [--level L] [--scope NAME]

Inputs handled
  * Google CSV/TSV export (UTF-8 or UTF-16 with BOM, comma or tab). Two title lines (report name, date range)
    then a header row. Columns (aliases in COLS): Display URL domain, Impression share (Impr. share), Overlap
    rate, Position above rate, Top of page rate, Abs. Top of page rate, Outranking share, and optional Campaign,
    Ad group, Keyword.
  * Pasted code-block text, as posted in #fable-desk: section headings such as
    "CAMPAIGN acme_search | Oct 7-31, 2026 | 13 rows" or "TAMPA_CORE (16 rows)", then rows of whitespace
    separated columns ("You" is our own row).
  * Values: "12.3%" -> 0.123; "< 10%" (Google hides shares under 10%) -> NULL value, raw "<10%" kept (the true
    value is somewhere in [0, 0.10)); "--"/"-"/"" -> NULL, raw "--" (not applicable or no data).

Store: table ai_rows in the tenant SQLite, unique on (tenant, range_start, range_end, level, scope, domain),
re-import replaces. Fractions are stored 0..1; each metric has a ``<m>_raw`` text column.
"""
import argparse
import csv
import io
import re
import sys
import time
from datetime import date, datetime, timezone

from . import config as C
from .store import Store

METRICS = ("impression_share", "overlap_rate", "position_above_rate", "top_of_page_rate",
           "abs_top_of_page_rate", "outranking_share")

# normalised header (lowercase alphanumerics) -> field
COLS = {
    "displayurldomain": "domain", "displayurl": "domain", "domain": "domain", "advertiser": "domain",
    "imprshare": "impression_share", "impressionshare": "impression_share",
    "overlaprate": "overlap_rate",
    "positionaboverate": "position_above_rate",
    "topofpagerate": "top_of_page_rate",
    "abstopofpagerate": "abs_top_of_page_rate", "absolutetopofpagerate": "abs_top_of_page_rate",
    "abstoppagerate": "abs_top_of_page_rate", "absolutetopofthepagerate": "abs_top_of_page_rate",
    "outrankingshare": "outranking_share",
    "campaign": "campaign", "adgroup": "ad_group", "keyword": "keyword", "searchkeyword": "keyword",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_rows (
  tenant TEXT NOT NULL, range_start TEXT NOT NULL, range_end TEXT NOT NULL, level TEXT NOT NULL,
  scope TEXT NOT NULL, campaign TEXT, domain TEXT NOT NULL, is_self INTEGER NOT NULL DEFAULT 0,
  impression_share REAL, impression_share_raw TEXT, overlap_rate REAL, overlap_rate_raw TEXT,
  position_above_rate REAL, position_above_rate_raw TEXT, top_of_page_rate REAL, top_of_page_rate_raw TEXT,
  abs_top_of_page_rate REAL, abs_top_of_page_rate_raw TEXT, outranking_share REAL, outranking_share_raw TEXT,
  imported_at TEXT NOT NULL, source TEXT,
  PRIMARY KEY (tenant, range_start, range_end, level, scope, domain));
"""

_VAL = re.compile(r"^(?:<\s*10%|-{1,2}|\d+(?:\.\d+)?%)$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}


class ImportError_(ValueError):
    pass


def parse_value(s):
    """-> (fraction|None, raw). '< 10%' -> (None, '<10%'); '--' -> (None, '--')."""
    t = re.sub(r"\s+", "", str(s if s is not None else "")).replace("–", "-").replace("—", "--")
    if t == "":
        return None, "--"
    if re.fullmatch(r"<10(?:\.0+)?%", t):
        return None, "<10%"
    if re.fullmatch(r"-{1,2}", t):
        return None, "--"
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)%", t)
    if not m:
        raise ImportError_(f"unrecognised value {s!r}")
    return float(m.group(1).replace(",", ".")) / 100.0, t


def _mon(s):
    return _MONTHS[s[:3].lower()]


def parse_range(text, default_year=None):
    """Find a date range in free text: 'Oct 7 - 31, 2026', 'Oct 7, 2026 - Oct 31, 2026', '2026-10-07..2026-10-31'.
    -> (start_iso, end_iso) or None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|to|-|–)\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1), m.group(2)
    mon = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
    m = re.search(mon + r"\s+(\d{1,2}),?\s*(\d{4})?\s*[-–]\s*(?:" + mon + r"\s+)?(\d{1,2}),?\s*(\d{4})", text)
    if m:
        m1, d1, y1, m2, d2, y2 = m.groups()
        y2 = int(y2)
        y1 = int(y1) if y1 else y2
        m1n = _mon(m1)
        m2n = _mon(m2) if m2 else m1n
        if not y1 or (not m.group(3) and m2n < m1n):
            y1 = y2 - (1 if m2n < m1n else 0)
        return date(y1, m1n, int(d1)).isoformat(), date(y2, m2n, int(d2)).isoformat()
    return None


def _norm_header(h):
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def parse_csv(text):
    """Google export -> (rows, range|None). rows: dict(domain, campaign?, ad_group?, keyword?, metric fields...)"""
    lines = text.splitlines()
    hi = None
    for i, ln in enumerate(lines):
        cells = re.split(r"\t|,", ln)
        if sum(1 for c in cells if _norm_header(c.strip('" ')) in COLS) >= 3:
            hi = i
            break
    if hi is None:
        raise ImportError_("no header row with Display URL domain / metric columns found")
    rng = parse_range("\n".join(lines[:hi])) or None
    delim = "\t" if lines[hi].count("\t") >= 2 else ","
    rd = csv.reader(io.StringIO("\n".join(lines[hi:])), delimiter=delim)
    head = [COLS.get(_norm_header(c)) for c in next(rd)]
    if "domain" not in head:
        raise ImportError_("header has no domain column")
    out = []
    for r in rd:
        if not r or all(not c.strip() for c in r):
            continue
        rec = {h: c.strip() for h, c in zip(head, r) if h}
        d = rec.get("domain", "")
        if not d or d.lower().startswith(("total", "--")) and "." not in d:
            continue
        out.append(rec)
    return out, rng


def _is_data_row(tokens):
    return len(tokens) >= 7 and all(_VAL.match(t) for t in tokens[-6:])


def parse_text(text, default_level="ad_group", default_campaign=None):
    """Pasted tables -> (rows, range|None). Each row has level/scope/campaign set from the nearest heading."""
    text = re.sub(r"<\s+10%", "<10%", text.replace("```", "\n"))
    rows, rng = [], None
    level, scope, campaign = default_level, None, default_campaign
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        tok = ln.split()
        if _is_data_row(tok):
            if scope is None:
                raise ImportError_(f"data row before any section heading: {ln[:60]!r}")
            rec = {"domain": tok[0], "level": level, "scope": scope, "campaign": campaign}
            rec.update(dict(zip(METRICS, tok[-6:])))
            rows.append(rec)
            continue
        if _norm_header(tok[0]) in COLS and sum(1 for t in tok if _norm_header(t) in COLS) >= 2 or \
                ln.lower().startswith("domain"):
            continue  # a header row
        # heading
        r = parse_range(ln)
        if r:
            rng = rng or r
        head = ln.split("|")[0].strip()
        head = re.sub(r"\((?:[^)]*\brows?\b[^)]*)\)", "", head).strip()
        head = re.sub(r"\(\s*paused\s*\)", "", head, flags=re.I).strip()
        m = re.match(r"(?i)^(campaign|ad ?group|keyword)s?\s*[:\-]?\s+(.+)$", head)
        if m:
            kind = m.group(1).lower().replace(" ", "")
            level = {"campaign": "campaign", "adgroup": "ad_group", "keyword": "keyword"}[kind]
            scope = m.group(2).strip()
            if level == "campaign":
                campaign = scope
        else:
            level, scope = default_level, head
        if level != "keyword":
            scope = scope.lower() if level == "ad_group" else scope
    return rows, rng


def _scope_of(rec, level):
    return rec.get(level) or rec.get("scope") or "ALL"


def import_rows(store, tenant, rows, rng, level, our_domains, source, campaign=None, now=None):
    """Upsert rows. ``rng`` (start, end) is required. -> number of rows stored."""
    if not rng:
        raise ImportError_("no date range: put it in the file or pass --range START..END")
    store.db.executescript(SCHEMA)
    ts = datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for rec in rows:
        lv = rec.get("level", level)
        scope = _scope_of(rec, lv)
        dom = rec["domain"].strip().lower()
        is_self = 1 if dom in ("you", "your", "you (your)") or dom.removeprefix("www.") in our_domains else 0
        if dom == "you":
            dom = our_domains[0] if our_domains else "you"
        vals = {}
        for m in METRICS:
            v, raw = parse_value(rec.get(m, ""))
            vals[m], vals[m + "_raw"] = v, raw
        cols = ["tenant", "range_start", "range_end", "level", "scope", "campaign", "domain", "is_self",
                "imported_at", "source"] + list(vals)
        args = [tenant, rng[0], rng[1], lv, scope, rec.get("campaign") or campaign, dom, is_self, ts, source] \
            + list(vals.values())
        store.db.execute(f"INSERT OR REPLACE INTO ai_rows ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", args)
        n += 1
    store.db.commit()
    return n


def import_text(store, cfg, data, source, level=None, campaign=None, rng=None, now=None):
    """Auto-detects CSV/TSV export vs pasted text. -> rows stored."""
    level = level or cfg["auction_insights"]["level"]
    campaign = campaign or cfg["auction_insights"].get("campaign")
    if data and not isinstance(data, str):
        data = _decode(data)
    sniff = data.lstrip("﻿").lstrip()
    is_csv = bool(re.search(r"(?i)display url domain|^domain\b", sniff[:2000], re.M)) and \
        ("\t" in sniff.split("\n", 5)[min(2, len(sniff.split("\n")) - 1)] or "," in sniff[:2000]) and \
        not _text_like(sniff)
    if is_csv:
        rows, found = parse_csv(data)
    else:
        rows, found = parse_text(data, level, campaign)
    if not rows:
        raise ImportError_("no auction-insight rows found")
    return import_rows(store, cfg["tenant"], rows, rng or found, level, cfg["our_domains"], source, campaign, now)


def _text_like(s):
    """Pasted tables have whitespace-aligned data rows (no commas/tabs separating metrics)."""
    for ln in s.splitlines():
        if _is_data_row(ln.replace("<  ", "<").split()) and "," not in ln and "\t" not in ln:
            return True
    return False


def latest_range(db, tenant):
    r = db.execute("SELECT range_start, range_end FROM ai_rows WHERE tenant=? ORDER BY range_end DESC, "
                   "range_start DESC LIMIT 1", (tenant,)).fetchone()
    return (r[0], r[1]) if r else None


def beaters(db, tenant, rng=None, level="campaign", scope=None, min_overlap=0.0):
    """Competitors ranked by how often they sat above us: overlap_rate x position_above_rate (the share of OUR
    impressions in which they also showed above us). Unknown values sort last and are flagged."""
    rng = rng or latest_range(db, tenant)
    if not rng:
        return []
    q = "SELECT * FROM ai_rows WHERE tenant=? AND range_start=? AND range_end=? AND level=? AND is_self=0"
    a = [tenant, rng[0], rng[1], level]
    if scope:
        q += " AND scope=?"
        a.append(scope)
    out = []
    for r in db.execute(q, a):
        ov, pa = r["overlap_rate"], r["position_above_rate"]
        out.append({"domain": r["domain"], "scope": r["scope"], "impression_share": r["impression_share"],
                    "impression_share_raw": r["impression_share_raw"], "overlap_rate": ov,
                    "position_above_rate": pa, "top_of_page_rate": r["top_of_page_rate"],
                    "abs_top_of_page_rate": r["abs_top_of_page_rate"], "outranking_share": r["outranking_share"],
                    "above_us": (ov * pa) if ov is not None and pa is not None else None})
    out = [o for o in out if (o["overlap_rate"] or 0) >= min_overlap]
    out.sort(key=lambda o: (o["above_us"] is None, -(o["above_us"] or 0), -(o["impression_share"] or 0), o["domain"]))
    return out


def self_row(db, tenant, rng, level, scope):
    return db.execute("SELECT * FROM ai_rows WHERE tenant=? AND range_start=? AND range_end=? AND level=? "
                      "AND scope=? AND is_self=1", (tenant, rng[0], rng[1], level, scope)).fetchone()


def scopes(db, tenant, rng, level):
    return [r[0] for r in db.execute("SELECT DISTINCT scope FROM ai_rows WHERE tenant=? AND range_start=? AND "
                                     "range_end=? AND level=? ORDER BY scope", (tenant, rng[0], rng[1], level))]


def history(db, tenant, level, scope, domain):
    """Per-range impression share of one domain in one scope, oldest first (for trend lines)."""
    return [(r[0], r[1], r[2]) for r in db.execute(
        "SELECT range_end, impression_share, impression_share_raw FROM ai_rows WHERE tenant=? AND level=? AND "
        "scope=? AND domain=? ORDER BY range_end", (tenant, level, scope, domain))]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adsops.intel.auction_insights")
    ap.add_argument("cmd", nargs="?", choices=["import", "show"])
    ap.add_argument("--config")
    ap.add_argument("--file")
    ap.add_argument("--level", choices=["campaign", "ad_group", "keyword"])
    ap.add_argument("--campaign")
    ap.add_argument("--scope")
    ap.add_argument("--range")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print(f"intel.auction_insights: {selftest()} checks")
        print("ALL PASS")
        return 0
    if not a.cmd or not a.config:
        ap.error("command and --config are required")
    cfg = C.load(a.config)
    st = Store(cfg["db_path"], cfg["tenant"])
    st.db.executescript(SCHEMA)
    if a.cmd == "import":
        if not a.file:
            ap.error("--file is required (use - for stdin)")
        raw = sys.stdin.buffer.read() if a.file == "-" else open(a.file, "rb").read()
        rng = parse_range(a.range) if a.range else None
        n = import_text(st, cfg, _decode(raw), a.file, a.level, a.campaign, rng)
        print(f"imported {n} rows into {cfg['db_path']}")
        return 0
    rng = latest_range(st.db, cfg["tenant"])
    if not rng:
        print("no auction insights imported yet")
        return 0
    lv = a.level or "campaign"
    print(f"range {rng[0]}..{rng[1]} level={lv}")
    for b in beaters(st.db, cfg["tenant"], rng, lv, a.scope):
        print(f"  {b['domain']:40s} above-us={b['above_us']} IS={b['impression_share_raw']}")
    return 0


FIXTURE_DIR = None


def selftest():
    import os
    import tempfile
    n = 0
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    assert parse_value("12.34%") == (0.1234, "12.34%"); n += 1
    assert parse_value("< 10%") == (None, "<10%") and parse_value("<10%") == (None, "<10%"); n += 1
    assert parse_value(" -- ") == (None, "--") and parse_value("-") == (None, "--") and parse_value("")[0] is None; n += 1
    assert parse_value("100.00%") == (1.0, "100.00%"); n += 1
    try:
        parse_value("n/a")
    except ImportError_:
        n += 1
    else:
        raise AssertionError("accepted garbage value")
    assert parse_range("Oct 7 – 31, 2026") == ("2026-10-07", "2026-10-31"); n += 1
    assert parse_range("Oct 7, 2026 - Nov 3, 2026") == ("2026-10-07", "2026-11-03"); n += 1
    assert parse_range("2026-10-07..2026-10-31") == ("2026-10-07", "2026-10-31"); n += 1
    assert parse_range("Dec 20 - Jan 3, 2027") == ("2026-12-20", "2027-01-03"); n += 1
    assert parse_range("no dates here") is None; n += 1

    cfg = C.validate(C.sample(tenant="t1", our_domains=["acmepm.example"]), ".")
    cfg["auction_insights"]["campaign"] = "acme_search"

    # Google CSV export fixture (UTF-8, two title lines, header row)
    st = Store(":memory:", "t1")
    data = open(os.path.join(fx, "auction_insights_sample.csv"), "rb").read()
    k = import_text(st, cfg, _decode(data), "sample.csv", level="campaign")
    assert k == 5; n += 1
    r = st.db.execute("SELECT * FROM ai_rows WHERE domain='rivalone.com'").fetchone()
    assert r["range_start"] == "2026-10-07" and r["level"] == "campaign" and abs(r["overlap_rate"] - 0.2751) < 1e-9; n += 1
    r = st.db.execute("SELECT * FROM ai_rows WHERE domain='smallrival.com'").fetchone()
    assert r["impression_share"] is None and r["impression_share_raw"] == "<10%" and r["position_above_rate"] is None \
        and r["position_above_rate_raw"] == "--"; n += 1
    me = st.db.execute("SELECT * FROM ai_rows WHERE is_self=1").fetchone()
    assert me["domain"] == "acmepm.example" and abs(me["impression_share"] - 0.2246) < 1e-9; n += 1
    # re-import replaces, does not duplicate
    import_text(st, cfg, _decode(data), "sample.csv", level="campaign")
    assert st.db.execute("SELECT COUNT(*) FROM ai_rows").fetchone()[0] == 5; n += 1

    # UTF-16 TSV (what the Ads UI actually downloads), keyword level with extra columns
    tsv = open(os.path.join(fx, "auction_insights_keyword_sample.tsv"), "rb").read()
    assert tsv[:2] == b"\xff\xfe"; n += 1
    st2 = Store(":memory:", "t1")
    k = import_text(st2, cfg, tsv, "kw.tsv", level="keyword")
    scopes_ = scopes(st2.db, "t1", latest_range(st2.db, "t1"), "keyword")
    assert k == 4 and scopes_ == ["property management tampa", "tampa property management"], (k, scopes_); n += 1

    # pasted code-block text, same shape as the #fable-desk thread
    txt = open(os.path.join(fx, "auction_insights_pasted.txt"), encoding="utf-8").read()
    st3 = Store(":memory:", "t1")
    k = import_text(st3, cfg, txt, "slack", level="ad_group")
    assert k == 15, k; n += 1
    assert latest_range(st3.db, "t1") == ("2026-10-07", "2026-10-31"); n += 1
    lv = {r[0]: r[1] for r in st3.db.execute("SELECT scope, level FROM ai_rows GROUP BY scope")}
    assert lv["acme_search"] == "campaign" and lv["tampa_core"] == "ad_group" and lv["owners_all"] == "ad_group"; n += 1
    row = st3.db.execute("SELECT * FROM ai_rows WHERE scope='pasco' AND domain='bianchirealtyandpropertymanagement.com'").fetchone()
    assert row["impression_share_raw"] == "<10%" and row["campaign"] == "acme_search"; n += 1
    row = st3.db.execute("SELECT * FROM ai_rows WHERE scope='hillsborough_towns' AND domain='waldenlakepark.com'").fetchone()
    assert row["position_above_rate"] is None and row["position_above_rate_raw"] == "--" and row["top_of_page_rate"] == 0.0; n += 1
    b = beaters(st3.db, "t1", None, "campaign")
    assert b[0]["domain"] == "collabregtampa.com" and abs(b[0]["above_us"] - 0.2751 * 0.7203) < 1e-9; n += 1
    assert b[-1]["above_us"] is None or b[-1]["above_us"] <= b[0]["above_us"]; n += 1
    assert self_row(st3.db, "t1", latest_range(st3.db, "t1"), "campaign", "acme_search")["is_self"] == 1; n += 1
    assert history(st3.db, "t1", "campaign", "acme_search", "collabregtampa.com")[0][1] == 0.1424; n += 1
    # errors are loud
    for bad in ("hello world", "rivalone.com 1% 2% 3% 4% 5% 6%"):
        try:
            import_text(Store(":memory:", "t1"), cfg, bad, "x")
        except ImportError_:
            n += 1
        else:
            raise AssertionError("accepted junk: " + bad)
    try:
        import_text(Store(":memory:", "t1"), cfg, "CAMPAIGN c\nrivalone.com 1% 2% 3% 4% 5% 6%", "x")
    except ImportError_ as e:
        assert "date range" in str(e); n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
