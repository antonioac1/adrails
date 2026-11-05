"""Google Ads Transparency Center collector (free, public, polite).

Endpoints (public JSON RPC behind adstransparency.google.com, region US = 2840), both POST with a form field
``f.req`` holding a JSON object:
  SearchService/SearchSuggestions  {"1": text, "2": 10, "3": 10}
        -> {"1": [{"2": {"1": "<domain>"}} | {"1": {"1": name, "2": "AR...", "3": region}}], "3": token}
  SearchService/SearchCreatives    {"2": page_size, "3": {"12": {"1": "<domain|name>", "2": true}},
                                    "7": {"1": 1, "2": 0, "3": 2840}}
        -> {"1": [creative...], "4": total, "5": total}, creative =
           "1" advertiser id (AR...), "2" creative id (CR...), "4" format 1 text / 2 image / 3 video,
           "6"/"7" {"1": epoch seconds} first / last shown, "12" advertiser name, "14" advertiser domain,
           "3" content: {"1":{"4": preview URL}} (text ads) | {"3":{"2": "<img ...>"}} (image) ...
  Text-ad copy is inside the preview URL: query param ``overlay`` or ``assets``, "=" + url-safe base64 of a gzip
  of a tiny protobuf with string fields headline / description / visurl. Responsive ads usually carry only
  numeric asset ids ("assets" decodes to no strings), so they have no decoded text.
Recorded 2026-11-01 (fixtures/transparency_*.json); the endpoint is undocumented, so the parser is defensive.

Politeness and limits (it captcha'd us at about 150 requests on 10-29):
  * HARD cap 40 live requests per run (config transparency.max_requests, cannot exceed 40);
  * 3-8 s random jitter between live requests (config minimum 3 s);
  * every response cached on disk for 7 days (cache hits cost no request);
  * any HTTP 429, a redirect to /sorry, a captcha page or a non-JSON body stops the run cleanly, is recorded in
    tr_runs, and blocks new runs for ``cooldown_hours`` (default 6);
  * live calls only with --live. Without it the run uses the cache only.
Counts are per creative page (<= page_size) and US-wide (the Center cannot filter by city); ``total_reported``
is Google's own total when it exceeds what we fetched.

    python3 -m adsops.intel.transparency run    --config CFG [--live] [--max-requests N] [--only DOMAIN]
    python3 -m adsops.intel.transparency status --config CFG
    python3 -m adsops.intel.transparency parse  --file response.json
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import config as C
from .store import Store, iso

BASE = "https://adstransparency.google.com/anji/_/rpc/SearchService/"
FORMATS = {1: "text", 2: "image", 3: "video"}
HARD_CAP = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_advertisers (
  tenant TEXT NOT NULL, snap_date TEXT NOT NULL, advertiser_id TEXT NOT NULL, name TEXT, domain TEXT,
  query TEXT, creatives INTEGER, text_n INTEGER, image_n INTEGER, video_n INTEGER, other_n INTEGER,
  first_shown TEXT, last_shown TEXT, total_reported INTEGER, fetched_at TEXT NOT NULL,
  PRIMARY KEY (tenant, snap_date, advertiser_id));
CREATE TABLE IF NOT EXISTS tr_creatives (
  tenant TEXT NOT NULL, advertiser_id TEXT NOT NULL, creative_id TEXT NOT NULL, format TEXT, first_shown TEXT,
  last_shown TEXT, headline TEXT, description TEXT, visurl TEXT, first_seen_by_us TEXT NOT NULL,
  last_fetched TEXT NOT NULL, PRIMARY KEY (tenant, creative_id));
CREATE TABLE IF NOT EXISTS tr_runs (
  id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, ts TEXT NOT NULL, live INTEGER NOT NULL, requests INTEGER NOT NULL,
  cache_hits INTEGER NOT NULL, queries_done INTEGER NOT NULL, queries_total INTEGER NOT NULL,
  stopped_reason TEXT, note TEXT);
"""


class Blocked(Exception):
    """Captcha / rate limit / unexpected body: stop the run."""


# ---------------------------------------------------------------- parsing
def _ts(v):
    try:
        return datetime.fromtimestamp(int(v["1"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _varint(b, i):
    n = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        n |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return n, i
    raise ValueError("truncated varint")


def _fields(b):
    """Yield (field_no, wire_type, value) of a protobuf message; raises ValueError on malformed input."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        no, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            if i + ln > len(b):
                raise ValueError("truncated field")
            v, i = b[i:i + ln], i + ln
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError("bad wire type")
        yield no, wt, v


def decode_text(preview_url):
    """Decode headline/description/visurl from a text-ad preview URL. -> dict (possibly empty)."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(preview_url).query)
    except ValueError:
        return {}
    out = {}
    for key in ("overlay", "assets"):
        for val in q.get(key, []):
            v = val.lstrip("=")
            v += "=" * (-len(v) % 4)
            try:
                raw = gzip.decompress(base64.urlsafe_b64decode(v))
            except (ValueError, OSError, EOFError):
                continue
            try:
                for _no, wt, item in _fields(raw):
                    if wt != 2:
                        continue
                    sub = list(_fields(item))
                    name = next((x[2] for x in sub if x[0] == 1 and x[1] == 2), b"").decode("utf-8", "replace")
                    if name in ("headline", "description", "visurl"):
                        body = next((x[2] for x in sub if x[0] == 2 and x[1] == 2), b"")
                        txt = next((x[2] for x in _fields(body) if x[0] == 1 and x[1] == 2), b"")
                        if txt:
                            out.setdefault(name, txt.decode("utf-8", "replace"))
            except ValueError:
                continue
    return out


def parse_creatives(resp):
    """SearchCreatives JSON -> (list of creative dicts, total_reported|None)."""
    if not isinstance(resp, dict):
        raise ValueError("response is not an object")
    out = []
    for c in resp.get("1") or []:
        if not isinstance(c, dict) or not c.get("1"):
            continue
        fmt = FORMATS.get(c.get("4"), "other")
        content = c.get("3") or {}
        url = ((content.get("1") or {}).get("4")) if isinstance(content.get("1"), dict) else None
        text = decode_text(url) if url else {}
        out.append({"advertiser_id": c["1"], "creative_id": c.get("2"), "format": fmt,
                    "first_shown": _ts(c.get("6")), "last_shown": _ts(c.get("7")),
                    "name": c.get("12"), "domain": (c.get("14") or "").lower() or None,
                    "headline": text.get("headline"), "description": text.get("description"),
                    "visurl": text.get("visurl")})
    tot = resp.get("4")
    return out, (int(tot) if str(tot).isdigit() else None)


def summarize(creatives, total):
    """Group by advertiser -> per-advertiser summary rows."""
    by = {}
    for c in creatives:
        a = by.setdefault(c["advertiser_id"], {"advertiser_id": c["advertiser_id"], "name": c["name"],
                                               "domain": c["domain"], "creatives": 0, "text_n": 0, "image_n": 0,
                                               "video_n": 0, "other_n": 0, "first_shown": None, "last_shown": None})
        a["creatives"] += 1
        a[c["format"] + "_n"] += 1
        if c["first_shown"] and (not a["first_shown"] or c["first_shown"] < a["first_shown"]):
            a["first_shown"] = c["first_shown"]
        if c["last_shown"] and (not a["last_shown"] or c["last_shown"] > a["last_shown"]):
            a["last_shown"] = c["last_shown"]
    for a in by.values():
        a["total_reported"] = total if total and total > len(creatives) and len(by) == 1 else None
    return by


# ---------------------------------------------------------------- transport + cache
def urllib_http(url, form, timeout=30):
    """-> (status, location|None, body_text). Never follows redirects (a 302 to /sorry is a captcha)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(url, data=urllib.parse.urlencode({"f.req": form}).encode(), method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Referer": "https://adstransparency.google.com/",
        "User-Agent": "Mozilla/5.0 (AdRails-Intel; low-volume research; +https://adrails.dev)"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as r:
            return r.status, None, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location"), e.read().decode("utf-8", "replace")[:2000]


def _cache_path(cache_dir, method, form):
    return os.path.join(cache_dir, hashlib.sha256((method + "\n" + form).encode()).hexdigest()[:40] + ".json")


def cache_get(cache_dir, method, form, days, now):
    p = _cache_path(cache_dir, method, form)
    try:
        with open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
        if 0 <= now - rec["t"] <= days * 86400:
            return rec["d"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def cache_put(cache_dir, method, form, data, now):
    os.makedirs(cache_dir, exist_ok=True)
    p = _cache_path(cache_dir, method, form)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"t": now, "d": data}, fh)
    os.replace(p + ".tmp", p)


def check_response(status, location, body):
    """Return parsed JSON or raise Blocked with the reason."""
    if status == 429:
        raise Blocked("http-429")
    if status in (301, 302, 303, 307, 308) or (location and "/sorry" in location):
        raise Blocked("captcha-redirect")
    if status != 200:
        raise Blocked(f"http-{status}")
    if re.search(r"(?i)captcha|unusual traffic|/sorry/", body[:3000]) and not body.lstrip().startswith("{"):
        raise Blocked("captcha-page")
    try:
        return json.loads(body)
    except ValueError:
        raise Blocked("non-json-body")


def creatives_form(query, page_size, region):
    return json.dumps({"2": page_size, "3": {"12": {"1": query, "2": True}}, "7": {"1": 1, "2": 0, "3": region}},
                      separators=(",", ":"))


# ---------------------------------------------------------------- run
def last_block(db, tenant, now, cooldown_h):
    r = db.execute("SELECT ts, stopped_reason FROM tr_runs WHERE tenant=? AND stopped_reason IS NOT NULL "
                   "AND live=1 ORDER BY id DESC LIMIT 1", (tenant,)).fetchone()
    if not r:
        return None
    t = datetime.strptime(r[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return (r[0], r[1]) if now - t < cooldown_h * 3600 and r[1] != "cap-reached" else None


def store_results(db, tenant, creatives, by, query, now):
    ts, day = iso(now), iso(now)[:10]
    for a in by.values():
        db.execute("INSERT OR REPLACE INTO tr_advertisers (tenant, snap_date, advertiser_id, name, domain, query, "
                   "creatives, text_n, image_n, video_n, other_n, first_shown, last_shown, total_reported, "
                   "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (tenant, day, a["advertiser_id"], a["name"], a["domain"], query, a["creatives"], a["text_n"],
                    a["image_n"], a["video_n"], a["other_n"], a["first_shown"], a["last_shown"],
                    a["total_reported"], ts))
    for c in creatives:
        if not c["creative_id"]:
            continue
        old = db.execute("SELECT first_seen_by_us FROM tr_creatives WHERE tenant=? AND creative_id=?",
                         (tenant, c["creative_id"])).fetchone()
        db.execute("INSERT OR REPLACE INTO tr_creatives VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (tenant, c["advertiser_id"], c["creative_id"], c["format"], c["first_shown"], c["last_shown"],
                    c["headline"], c["description"], c["visurl"], old[0] if old else ts, ts))
    db.commit()


def run(cfg, store, live=False, http=urllib_http, sleep=time.sleep, now=None, max_requests=None, only=None,
        rand=random.uniform, log=print):
    """Collect every configured query. -> dict(requests, cache_hits, done, total, stopped)."""
    t = cfg["transparency"]
    now = now or time.time()
    db = store.db
    db.executescript(SCHEMA)
    queries = [only] if only else (t.get("domains") or cfg["competitors"])
    cap = min(HARD_CAP, int(max_requests or t["max_requests"]))
    res = {"requests": 0, "cache_hits": 0, "done": 0, "total": len(queries), "stopped": None}
    blk = last_block(db, cfg["tenant"], now, t["cooldown_hours"]) if live else None
    if blk:
        res["stopped"] = f"cooldown after {blk[1]} at {blk[0]}"
        log("refusing to call live: " + res["stopped"])
        return res
    for q in queries:
        form = creatives_form(q, t["page_size"], t["region_code"])
        data = cache_get(t["cache_dir"], "SearchCreatives", form, t["cache_days"], now)
        if data is not None:
            res["cache_hits"] += 1
        elif not live:
            log(f"  {q}: not cached (no --live), skipped")
            continue
        else:
            if res["requests"] >= cap:
                res["stopped"] = "cap-reached"
                break
            if res["requests"]:
                sleep(rand(t["jitter_s"][0], t["jitter_s"][1]))
            res["requests"] += 1
            try:
                data = check_response(*http(BASE + "SearchCreatives?authuser=", form))
            except Blocked as e:
                res["stopped"] = str(e)
                log(f"  {q}: BLOCKED ({e}); stopping run")
                break
            except (urllib.error.URLError, OSError) as e:
                res["stopped"] = "network-error"
                log(f"  {q}: network error {type(e).__name__}; stopping run")
                break
            cache_put(t["cache_dir"], "SearchCreatives", form, data, now)
        try:
            creatives, total = parse_creatives(data)
        except ValueError as e:
            log(f"  {q}: unparseable ({e}), skipped")
            continue
        by = summarize(creatives, total)
        store_results(db, cfg["tenant"], creatives, by, q, now)
        res["done"] += 1
        log(f"  {q}: {len(creatives)} creatives, {len(by)} advertiser(s)" + ("" if by else " (none found)"))
    db.execute("INSERT INTO tr_runs (tenant, ts, live, requests, cache_hits, queries_done, queries_total, "
               "stopped_reason, note) VALUES (?,?,?,?,?,?,?,?,?)",
               (cfg["tenant"], iso(now), 1 if live else 0, res["requests"], res["cache_hits"], res["done"],
                res["total"], res["stopped"], None))
    db.commit()
    return res


def latest(db, tenant, domains=None):
    """Latest snapshot per advertiser (optionally filtered to domains) + text creatives. -> list of dicts."""
    if not db.execute("SELECT name FROM sqlite_master WHERE name='tr_advertisers'").fetchone():
        return []
    out = []
    for r in db.execute("SELECT * FROM tr_advertisers WHERE tenant=? AND snap_date=(SELECT MAX(snap_date) FROM "
                        "tr_advertisers t2 WHERE t2.tenant=tr_advertisers.tenant AND t2.advertiser_id="
                        "tr_advertisers.advertiser_id) ORDER BY creatives DESC", (tenant,)):
        if domains and (r["domain"] or "") not in domains and (r["query"] or "") not in domains:
            continue
        texts = [dict(x) for x in db.execute(
            "SELECT headline, description, last_shown FROM tr_creatives WHERE tenant=? AND advertiser_id=? "
            "AND headline IS NOT NULL ORDER BY last_shown DESC LIMIT 3", (tenant, r["advertiser_id"]))]
        out.append({**dict(r), "texts": texts})
    return out


def last_run(db, tenant):
    if not db.execute("SELECT name FROM sqlite_master WHERE name='tr_runs'").fetchone():
        return None
    r = db.execute("SELECT * FROM tr_runs WHERE tenant=? ORDER BY id DESC LIMIT 1", (tenant,)).fetchone()
    return dict(r) if r else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adsops.intel.transparency")
    ap.add_argument("cmd", nargs="?", choices=["run", "status", "parse"])
    ap.add_argument("--config")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--only")
    ap.add_argument("--file")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print(f"intel.transparency: {selftest()} checks")
        print("ALL PASS")
        return 0
    if a.cmd == "parse":
        cs, tot = parse_creatives(json.load(open(a.file)))
        print(json.dumps({"total": tot, "creatives": cs}, indent=1))
        return 0
    if not a.cmd or not a.config:
        ap.error("command and --config are required")
    if a.max_requests is not None and not 1 <= a.max_requests <= HARD_CAP:
        ap.error(f"--max-requests must be 1..{HARD_CAP}")
    cfg = C.load(a.config)
    st = Store(cfg["db_path"], cfg["tenant"])
    st.db.executescript(SCHEMA)
    if a.cmd == "status":
        print(json.dumps(last_run(st.db, cfg["tenant"]), indent=1))
        return 0
    r = run(cfg, st, live=a.live, max_requests=a.max_requests, only=a.only)
    print(json.dumps(r))
    return 0 if not r["stopped"] or r["stopped"] == "cap-reached" else 3


def selftest():
    import tempfile
    n = 0
    fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    rec = json.load(open(os.path.join(fx, "transparency_creatives_competitor_example.json")))
    cs, tot = parse_creatives(rec)
    assert len(cs) == 9 and tot == 9; n += 1
    fm = [c["format"] for c in cs]
    assert fm.count("image") == 3 and fm.count("text") == 6, fm; n += 1
    assert cs[0]["advertiser_id"].startswith("AR") and cs[0]["domain"] == "competitor_example.com"; n += 1
    assert cs[0]["first_shown"] == "2025-03-07T16:16:19Z" and cs[0]["last_shown"].startswith("2026-11-"), cs[0]; n += 1
    tx = [c for c in cs if c["headline"]]
    assert len(tx) == 1 and tx[0]["headline"].startswith("Property Managers Tampa") and \
        tx[0]["visurl"] == "competitor_example.com/Managers/Tampa" and "Premium Service" in tx[0]["description"]; n += 1
    by = summarize(cs, tot)
    assert len(by) == 1 and next(iter(by.values()))["creatives"] == 9 and next(iter(by.values()))["image_n"] == 3; n += 1
    assert decode_text("https://x/?assets=%3Dgarbage") == {} and decode_text("not a url") == {}; n += 1
    assert parse_creatives({"1": [{"1": "AR1", "4": 9}]})[0][0]["format"] == "other"; n += 1
    try:
        parse_creatives([])
    except ValueError:
        n += 1
    else:
        raise AssertionError

    # check_response classification
    for st_, loc, body, why in ((429, None, "", "http-429"), (302, "https://google.com/sorry/index", "", "captcha-redirect"),
                                (200, None, "<html>Our systems have detected unusual traffic</html>", "captcha-page"),
                                (200, None, "<html>hi</html>", "non-json-body"), (500, None, "", "http-500")):
        try:
            check_response(st_, loc, body)
        except Blocked as e:
            assert str(e) == why, (str(e), why); n += 1
        else:
            raise AssertionError(why)
    assert check_response(200, None, '{"1":[]}') == {"1": []}; n += 1

    # run: caching, cap, jitter, block handling (all HTTP mocked)
    d = tempfile.mkdtemp()
    doms = [f"d{i}.com" for i in range(50)]
    cfg = C.validate(C.sample(competitors=doms, transparency={"cache_dir": os.path.join(d, "c")}), d)
    calls, sleeps = [], []

    def http_ok(url, form):
        calls.append(form)
        return 200, None, json.dumps(rec)
    st = Store(":memory:", "t1")
    r = run(cfg, st, live=False, http=http_ok, sleep=sleeps.append, now=1e9, log=lambda *_: None)
    assert r["requests"] == 0 and r["done"] == 0 and not calls; n += 1  # no --live => no calls
    r = run(cfg, st, live=True, http=http_ok, sleep=sleeps.append, now=1e9, log=lambda *_: None, rand=lambda a, b: b)
    assert r["requests"] == 40 and r["stopped"] == "cap-reached" and len(calls) == 40, r; n += 1
    assert len(sleeps) == 39 and all(3 <= s <= 8 for s in sleeps); n += 1
    r = run(cfg, st, live=True, http=http_ok, sleep=sleeps.append, now=1e9 + 3600, log=lambda *_: None)
    assert r["cache_hits"] == 40 and r["requests"] == 10, r; n += 1  # first 40 from cache, 10 new
    r = run(cfg, st, live=True, http=http_ok, sleep=sleeps.append, now=1e9 + 8 * 86400, max_requests=5, log=lambda *_: None)
    assert r["cache_hits"] == 0 and r["requests"] == 5 and r["stopped"] == "cap-reached"; n += 1  # cache expired
    assert db_count(st, "tr_advertisers") >= 1 and db_count(st, "tr_creatives") == 9; n += 1
    # block => clean stop, recorded, cooldown honoured
    cfg2 = C.validate(C.sample(competitors=["a.com", "b.com", "c.com"],
                               transparency={"cache_dir": os.path.join(d, "c2")}), d)
    st2 = Store(":memory:", "t1")
    seq = [(200, None, json.dumps(rec)), (302, "https://www.google.com/sorry/index?x", "")]
    r = run(cfg2, st2, live=True, http=lambda u, f: seq.pop(0), sleep=lambda s: None, now=2e9, log=lambda *_: None)
    assert r["done"] == 1 and r["requests"] == 2 and r["stopped"] == "captcha-redirect"; n += 1
    lr = last_run(st2.db, "t1")
    assert lr["stopped_reason"] == "captcha-redirect" and lr["queries_done"] == 1; n += 1
    r = run(cfg2, st2, live=True, http=http_ok, sleep=lambda s: None, now=2e9 + 3600, log=lambda *_: None)
    assert r["requests"] == 0 and "cooldown" in r["stopped"]; n += 1
    r = run(cfg2, st2, live=True, http=http_ok, sleep=lambda s: None, now=2e9 + 7 * 3600, log=lambda *_: None)
    assert r["requests"] >= 1 and not r["stopped"]; n += 1
    # network error also stops cleanly
    def boom(u, f):
        raise urllib.error.URLError("down")
    st3 = Store(":memory:", "t1")
    cfg3 = C.validate(C.sample(competitors=["z.com", "y.com"], transparency={"cache_dir": os.path.join(d, "c3")}), d)
    r = run(cfg3, st3, live=True, http=boom, sleep=lambda s: None, now=3e9, log=lambda *_: None)
    assert r["stopped"] == "network-error" and r["requests"] == 1; n += 1
    lat = latest(st.db, "t1")
    assert lat and lat[0]["texts"] and lat[0]["texts"][0]["headline"].startswith("Property Managers"); n += 1
    assert latest(Store(":memory:", "t1").db, "t1") == []; n += 1
    return n


def db_count(store, table):
    return store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


if __name__ == "__main__":
    sys.exit(main())
