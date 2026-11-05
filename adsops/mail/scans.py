"""Count real QR/URL scans of mail tracking codes from local web access logs.

This module parses nginx/apache combined logs or JSON-lines logs, extracts a
tracking-code query parameter, and drops bot/link-preview traffic, own-office
IPs/CIDRs, test hits made before the mail date, non-GET/HEAD requests, bad
statuses, asset requests, unknown codes, and unparsable lines. Visitor IDs are
salted hashes of IP + user-agent; raw IPs never leave this module. Use
`count()` on fetched log lines, `merge()` to combine incremental batches, and
`summarize()` to roll results up by batch/segment. All external input is passed
in explicitly so tools can test without network/file side effects.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from adsops.errors import ToolError, RailViolation, scrub

BOT_UA = (
    r"bot|crawl|spider|slurp|curl|wget|python|httpx|requests|go-http|headless|"
    r"lighthouse|scan|monitor|preview|facebookexternalhit|whatsapp|slack|discord|"
    r"telegram|skype|okhttp|java/|libwww|axios|node-fetch"
)

_DROP_KEYS = ("bot", "own_ip", "unknown_code", "before_mailing", "not_get", "bad_status", "unparsed")
_ASSET_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".woff2")
_GOOD_STATUS = (200, 301, 302, 304)
_MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

_APACHE_TS_RE = re.compile(
    r"^(?P<d>\d{1,2})/(?P<mon>[A-Za-z]{3})/(?P<y>\d{4}):"
    r"(?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2}) (?P<off>[+-]\d{4})$"
)
_COMBINED_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+'
    r'"(?P<method>[A-Za-z]+)\s+(?P<target>\S+)(?:\s+HTTP/[\d.]+)?"\s+'
    r'(?P<status>\d{3})\s+\S+'
    r'(?:\s+"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)")?'
)
_REQ_RE = re.compile(r"^(?P<method>[A-Za-z]+)\s+(?P<target>\S+)(?:\s+HTTP/[\d.]+)?$")


def _g(row, *path, default=None):
    """Nested dict/list getter used by JSON log parsing."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def _int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default


def _float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iter(value):
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return value


def _parse_apache_ts(value):
    if not isinstance(value, str):
        return None
    m = _APACHE_TS_RE.match(value.strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon").title())
    if mon is None:
        return None
    off = m.group("off")
    sign = 1 if off[0] == "+" else -1
    try:
        tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
        dt = datetime(
            int(m.group("y")), mon, int(m.group("d")),
            int(m.group("H")), int(m.group("M")), int(m.group("S")), tzinfo=tz,
        )
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _dt_from_iso(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts_any(value):
    if isinstance(value, (int, float)):
        return _dt_from_iso(value)
    if not isinstance(value, str):
        return None
    dt = _parse_apache_ts(value)
    return dt if dt is not None else _dt_from_iso(value)


def _iso_out(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _split_target(target):
    target = str(target or "")
    if not target:
        return "", ""
    try:
        u = urlsplit(target)
        path = u.path or ("/" if target.startswith("/") else target)
        return path, u.query
    except ValueError:
        pass
    if "?" in target:
        p, q = target.split("?", 1)
        return p, q
    return target, ""


def _parse_json_line(text):
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    ip = _g(obj, "remote_addr") or _g(obj, "ip")
    ts_raw = _g(obj, "time_iso8601") or _g(obj, "time") or _g(obj, "ts")
    if not ip or ts_raw is None:
        return None
    ts = _parse_ts_any(ts_raw)
    if ts is None:
        return None

    method = "GET"
    target = ""
    req = _g(obj, "request")
    if isinstance(req, str) and req.strip():
        m = _REQ_RE.match(req.strip())
        if m:
            method = m.group("method").upper()
            target = m.group("target")
    if not target:
        uri = _g(obj, "uri") or _g(obj, "path") or "/"
        args = _g(obj, "args")
        if args is None:
            args = _g(obj, "query") or ""
        if isinstance(args, dict):
            args = "&".join(f"{k}={v}" for k, v in args.items())
        uri, args = str(uri), str(args)
        target = f"{uri}?{args}" if args else uri
        method = str(_g(obj, "method") or method).upper()

    path, query = _split_target(target)
    ua = _g(obj, "http_user_agent") or _g(obj, "ua") or ""
    ref = _g(obj, "http_referer") or _g(obj, "referer") or ""
    return {
        "ip": str(ip), "ts": _iso_out(ts), "method": method,
        "path": path, "query": query, "status": _int(_g(obj, "status")),
        "ua": str(ua), "referer": str(ref),
    }


def parse(line, fmt="combined"):
    """Parse one access-log line. Returns a normalised dict or None."""
    if line is None:
        return None
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    text = str(line).strip()
    if not text:
        return None

    if fmt == "combined":
        m = _COMBINED_RE.match(text)
        if not m:
            return None
        ts = _parse_apache_ts(m.group("ts"))
        if ts is None:
            return None
        path, query = _split_target(m.group("target"))
        return {
            "ip": m.group("ip"), "ts": _iso_out(ts),
            "method": m.group("method").upper(), "path": path, "query": query,
            "status": _int(m.group("status")),
            "ua": m.group("ua") or "", "referer": m.group("referer") or "",
        }
    if fmt == "json":
        return _parse_json_line(text)
    raise ToolError(f"unknown log format: {fmt}")


def extract_code(entry, param="c", path_prefixes=("/",), normalizer=None):
    """Extract and normalise a tracking code from a parsed entry, or None."""
    if not isinstance(entry, dict) or not param:
        return None
    path = str(entry.get("path") or "")
    prefixes = (path_prefixes,) if isinstance(path_prefixes, str) else (path_prefixes or ())
    if not any(path.startswith(str(p)) for p in prefixes):
        return None
    vals = parse_qs(str(entry.get("query") or ""), keep_blank_values=True).get(param)
    if not vals:
        return None
    code = vals[0]
    code = str(code).strip().upper() if normalizer is None else normalizer(code)
    if code is None:
        return None
    code = str(code).strip()
    return code or None


def _compile_bot(pattern):
    if not pattern:
        return None
    if hasattr(pattern, "search"):
        return pattern
    try:
        return re.compile(str(pattern), re.I)
    except re.error as exc:
        raise ToolError(f"invalid bot_ua regex: {scrub(str(exc), 200)}") from None


def _is_asset(path):
    p = str(path or "").lower()
    return any(p.endswith(ext) for ext in _ASSET_EXT)


def _own_sets(own_ips, own_cidrs):
    addrs, nets = set(), []
    for item in _iter(own_ips):
        try:
            addrs.add(ipaddress.ip_address(str(item)))
        except ValueError:
            pass
    for item in _iter(own_cidrs):
        try:
            nets.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError:
            pass
    return addrs, nets


def _is_own(ip, addrs, nets):
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    if addr in addrs:
        return True
    return any(addr in net for net in nets)


def _visitor_id(ip, ua, salt):
    raw = f"{salt}{ip}|{ua}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def count(lines, mailed: dict, fmt="combined", param="c", path_prefixes=("/",),
          own_ips=(), own_cidrs=(), bot_ua=BOT_UA, normalizer=None, salt=""):
    """Count scans from log lines, returning per-code stats and drop reasons."""
    if isinstance(lines, bytes):
        lines = lines.decode("utf-8", "replace")
    if isinstance(lines, str):
        lines = lines.splitlines()
    if not isinstance(mailed, dict):
        raise ToolError("mailed must be a mapping of code -> ISO timestamp")

    mailed_dt = {str(code): _parse_ts_any(when) for code, when in mailed.items()}
    bot_re = _compile_bot(bot_ua)
    own_addrs, own_nets = _own_sets(own_ips, own_cidrs)
    dropped = {k: 0 for k in _DROP_KEYS}
    acc = {}

    for raw in lines or ():
        entry = parse(raw, fmt)
        if entry is None:
            dropped["unparsed"] += 1
            continue
        if entry["method"] not in ("GET", "HEAD"):
            dropped["not_get"] += 1
            continue
        if entry["status"] not in _GOOD_STATUS:
            dropped["bad_status"] += 1
            continue
        if _is_own(entry["ip"], own_addrs, own_nets):
            dropped["own_ip"] += 1
            continue
        if bot_re is not None and bot_re.search(entry["ua"] or ""):
            dropped["bot"] += 1
            continue
        if _is_asset(entry["path"]):
            continue

        code = extract_code(entry, param, path_prefixes, normalizer)
        if not code or code not in mailed_dt:
            dropped["unknown_code"] += 1
            continue
        hit_dt = _parse_ts_any(entry["ts"])
        mailed_at = mailed_dt.get(code)
        if mailed_at is not None and hit_dt is not None and hit_dt < mailed_at:
            dropped["before_mailing"] += 1
            continue

        vid = _visitor_id(entry["ip"], entry["ua"], salt)
        a = acc.get(code)
        if a is None:
            a = {"visitors": set(), "hits": 0, "first": hit_dt}
            acc[code] = a
        a["hits"] += 1
        a["visitors"].add(vid)
        if hit_dt is not None and (a["first"] is None or hit_dt < a["first"]):
            a["first"] = hit_dt

    scans, visitor_ids = {}, {}
    for code in sorted(acc):
        a = acc[code]
        ids = sorted(a["visitors"])
        first = a["first"]
        if first is not None and mailed_dt.get(code) is not None:
            days = (first - mailed_dt[code]).total_seconds() / 86400.0
        else:
            days = 0.0
        scans[code] = {
            "visitors": len(ids), "hits": a["hits"],
            "first_scan": _iso_out(first) or "", "days_to_first_scan": days,
        }
        visitor_ids[code] = ids

    return {"scans": scans, "dropped": dropped, "visitor_ids": visitor_ids}


def merge(prev, cur):
    """Merge two results, unioning visitor IDs. Idempotent for identical input."""
    prev = {} if prev is None else prev
    cur = {} if cur is None else cur
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        raise ToolError("merge expects scan result dicts")

    same_result = (prev == cur)

    p_scans, c_scans = prev.get("scans") or {}, cur.get("scans") or {}
    p_vids, c_vids = prev.get("visitor_ids") or {}, cur.get("visitor_ids") or {}
    p_drop, c_drop = prev.get("dropped") or {}, cur.get("dropped") or {}
    codes = set(p_scans) | set(c_scans) | set(p_vids) | set(c_vids)
    out = {"scans": {}, "dropped": {}, "visitor_ids": {}}

    for key in _DROP_KEYS:
        if same_result:
            out["dropped"][key] = max(_int(p_drop.get(key)), _int(c_drop.get(key)))
        else:
            out["dropped"][key] = _int(p_drop.get(key)) + _int(c_drop.get(key))

    for code in sorted(codes):
        p, c = p_scans.get(code) or {}, c_scans.get(code) or {}
        ids = sorted({str(x) for x in (p_vids.get(code) or [])} |
                     {str(x) for x in (c_vids.get(code) or [])})
        if same_result:
            hits = max(_int(p.get("hits")), _int(c.get("hits")), len(ids))
        else:
            hits = _int(p.get("hits")) + _int(c.get("hits"))
            if hits < len(ids):
                hits = len(ids)
        p_first, c_first = p.get("first_scan"), c.get("first_scan")
        if p_first and c_first:
            if p_first <= c_first:
                first, days = p_first, p.get("days_to_first_scan")
            else:
                first, days = c_first, c.get("days_to_first_scan")
        elif p_first:
            first, days = p_first, p.get("days_to_first_scan")
        elif c_first:
            first, days = c_first, c.get("days_to_first_scan")
        else:
            first, days = "", 0.0
        out["scans"][code] = {
            "visitors": len(ids), "hits": hits,
            "first_scan": first or "", "days_to_first_scan": _float(days),
        }
        out["visitor_ids"][code] = ids
    return out


def summarize(result, pieces):
    """Roll scan results up over tracking-code pieces by batch and segment."""
    scans = (result or {}).get("scans") or {}
    scanned = {str(c) for c, d in scans.items() if _int((d or {}).get("visitors")) > 0}
    groups = {"batch": {}, "segment": {}}
    total_codes = set()

    for piece in pieces or ():
        if not isinstance(piece, dict):
            continue
        code = piece.get("tracking_code") or piece.get("code")
        if not code:
            continue
        code = str(code)
        total_codes.add(code)
        for kind in ("batch", "segment"):
            name = str(piece.get(kind) or "unknown")
            g = groups[kind].setdefault(name, {"mailed": set(), "scanned": set()})
            g["mailed"].add(code)
            if code in scanned:
                g["scanned"].add(code)

    out = {"by_batch": {}, "by_segment": {}, "total": {}}
    for kind in ("batch", "segment"):
        for name in sorted(groups[kind]):
            g = groups[kind][name]
            mailed, hits = len(g["mailed"]), len(g["scanned"])
            out[f"by_{kind}"][name] = {
                "mailed": mailed, "scanned_codes": hits,
                "scan_rate": (hits / mailed) if mailed else 0.0,
            }
    mailed = len(total_codes)
    hits = len(total_codes & scanned)
    out["total"] = {
        "mailed": mailed, "scanned_codes": hits,
        "scan_rate": (hits / mailed) if mailed else 0.0,
    }
    return out


def selftest() -> int:
    n = 0

    def ok(cond, msg="assertion failed"):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    basic = '1.1.1.1 - - [05/Nov/2026:10:00:01 -0500] "GET /p?c=abc HTTP/1.1" 200 1 "-" "Mozilla/5.0"'
    e = parse(basic)
    ok(e is not None, "combined parse")
    ok(e["ts"] == "2026-11-05T15:00:01Z", "combined timezone conversion")
    ok(e["path"] == "/p" and e["query"] == "c=abc", "combined path/query")
    ok(e["status"] == 200 and e["method"] == "GET", "combined method/status")

    head = '1.2.3.4 - - [05/Nov/2026:10:00:01 +0000] "HEAD / HTTP/1.1" 304 -'
    eh = parse(head)
    ok(eh is not None and eh["method"] == "HEAD" and eh["ua"] == "", "combined no ua")

    js = ('{"remote_addr":"9.9.9.9","time_iso8601":"2026-11-05T10:00:00Z",'
          '"request":"GET /q?c=xyz HTTP/1.1","status":200,'
          '"http_user_agent":"Mozilla","http_referer":"-"}')
    ej = parse(js, fmt="json")
    ok(ej is not None, "json parse")
    ok(ej["path"] == "/q" and ej["query"] == "c=xyz", "json request path/query")
    ok(ej["status"] == 200 and ej["ua"] == "Mozilla", "json status/ua")

    ju = ('{"ip":"1.1.1.1","time":"2026-11-05T10:00:00+00:00",'
          '"uri":"/r","args":"c=qq","status":"200"}')
    eju = parse(ju, fmt="json")
    ok(eju is not None and eju["method"] == "GET" and eju["query"] == "c=qq", "json uri+args")
    ok(parse("{bad json", fmt="json") is None, "malformed json")
    ok(parse("not a log line") is None, "malformed combined")
    ok(parse("") is None, "empty line")

    ok(extract_code({"path": "/p", "query": "c=ab"}) == "AB", "extract default normalizer")
    ok(extract_code({"path": "/p", "query": "c=ab"}, path_prefixes=("/x",)) is None, "prefix mismatch")
    ok(extract_code({"path": "/p", "query": "x=ab"}) is None, "missing param")
    ok(extract_code({"path": "/p", "query": "c=ab"},
                    normalizer=lambda s: s + "!") == "ab!", "custom normalizer")

    mailed = {"ABC": "2026-11-05T15:00:00Z"}
    other_ip = basic.replace("1.1.1.1", "9.9.9.9")
    lines = [
        basic,
        basic.replace("10:00:01", "10:00:02"),
        basic.replace("1.1.1.1", "2.2.2.2").replace("Mozilla/5.0", "Googlebot/2.1"),
        basic.replace("1.1.1.1", "10.0.0.1"),
        basic.replace("1.1.1.1", "4.4.4.4").replace("c=abc", "c=zzz"),
        basic.replace("1.1.1.1", "3.3.3.3").replace("10:00:01", "09:59:00"),
        basic.replace("1.1.1.1", "5.5.5.5").replace("GET", "POST"),
        basic.replace("1.1.1.1", "6.6.6.6").replace("200 1", "500 1"),
        basic.replace("1.1.1.1", "7.7.7.7").replace("/p?", "/style.css?"),
        "not a log line",
    ]
    r = count(lines, mailed, own_ips=("10.0.0.1",), salt="s")
    ok(r["scans"]["ABC"]["visitors"] == 1, "visitor dedupe")
    ok(r["scans"]["ABC"]["hits"] == 2, "hit counter")
    ok(r["scans"]["ABC"]["first_scan"] == "2026-11-05T15:00:01Z", "first scan")
    ok(r["dropped"]["bot"] == 1, "bot drop")
    ok(r["dropped"]["own_ip"] == 1, "own ip drop")
    ok(r["dropped"]["unknown_code"] == 1, "unknown code drop")
    ok(r["dropped"]["before_mailing"] == 1, "pre-mail drop")
    ok(r["dropped"]["not_get"] == 1, "method drop")
    ok(r["dropped"]["bad_status"] == 1, "status drop")
    ok(r["dropped"]["unparsed"] == 1, "unparsed drop")
    ok(sum(r["dropped"].values()) == 7, "asset silently skipped")

    vid = r["visitor_ids"]["ABC"][0]
    ok(len(vid) == 16, "visitor hash length")
    ok("1.1.1.1" not in vid, "raw ip not in visitor id")
    ok(count([basic], mailed, salt="other")["visitor_ids"]["ABC"][0] != vid, "salt changes id")

    cidr_line = basic.replace("1.1.1.1", "192.168.1.5")
    ok(count([cidr_line], mailed, own_cidrs=("192.168.0.0/16",))["dropped"]["own_ip"] == 1,
       "own cidr drop")
    fetch_line = basic.replace("Mozilla/5.0", "MyFetcher/1.0")
    ok(count([fetch_line], mailed, bot_ua="fetcher")["dropped"]["bot"] == 1, "custom bot regex")

    head_ok = basic.replace('"GET /p?c=abc HTTP/1.1" 200', '"HEAD /p?c=abc HTTP/1.1" 304')
    ok(count([head_ok], mailed)["scans"]["ABC"]["hits"] == 1, "HEAD 304 accepted")

    empty = count([], mailed)
    ok(empty["scans"] == {}, "empty input scans")
    ok(sum(empty["dropped"].values()) == 0, "empty input drops")

    m = merge(r, r)
    ok(m["scans"]["ABC"]["hits"] == r["scans"]["ABC"]["hits"], "merge hits idempotent")
    ok(m["visitor_ids"]["ABC"] == r["visitor_ids"]["ABC"], "merge visitors idempotent")
    ok(m["dropped"] == r["dropped"], "merge drops idempotent")
    m2 = merge(r, count([other_ip], mailed, salt="s"))
    ok(m2["scans"]["ABC"]["visitors"] == 2, "merge unions visitor ids")

    pieces = [
        {"tracking_code": "ABC", "batch": "B1", "segment": "S1"},
        {"tracking_code": "ZZZ", "batch": "B1", "segment": "S2"},
    ]
    s = summarize(r, pieces)
    ok(s["by_batch"]["B1"]["mailed"] == 2, "batch mailed")
    ok(s["by_batch"]["B1"]["scanned_codes"] == 1, "batch scanned")
    ok(s["by_batch"]["B1"]["scan_rate"] == 0.5, "batch rate")
    ok(s["by_segment"]["S2"]["scanned_codes"] == 0, "segment scanned")
    ok(s["total"]["mailed"] == 2 and s["total"]["scanned_codes"] == 1, "total counts")
    ok(summarize({}, [])["total"]["mailed"] == 0, "empty summarize")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)