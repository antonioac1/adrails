"""Measure AI-driven traffic from web server access logs.

Parses nginx ``combined`` or one-JSON-object-per-line access logs into normalised
entries, then classifies each request as an AI referral (ChatGPT, Perplexity,
Claude, Gemini, Copilot, ...), a verified AI crawler (source IP inside the bot's
published CIDR ranges), an unverified crawler, a spoofed crawler, a human, or
another bot.

Safety: pure and read-only. Log lines, CIDR maps and timestamps are injected by
the caller, so there is no network/file/clock effect and every test can mock.
Results are JSON-serialisable dicts with no headers, cookies or tokens. Tools
should call ``parse_line`` per line, ``summarize`` over the batch, then
``weekly_line`` for a short Slack digest; bad input raises ``ToolError``.
"""

import ipaddress
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

try:  # pragma: no cover - the package supplies these in production
    from adsops.errors import ToolError, scrub
except ImportError:  # pragma: no cover - lets selftest run standalone
    class ToolError(Exception):
        """Bad input or a refused operation (standalone fallback)."""

    def scrub(text, limit=1500):
        return str(text)[:limit]


# Referrer host suffix (optionally host/path-prefix) -> engine name.
REFERRERS = {
    "chatgpt.com": "chatgpt",
    "chat.openai.com": "chatgpt",
    "perplexity.ai": "perplexity",
    "claude.ai": "claude",
    "gemini.google.com": "gemini",
    "copilot.microsoft.com": "copilot",
    "bing.com/chat": "copilot",           # only bing's chat surface counts
    "you.com": "you",
    "phind.com": "phind",
    "meta.ai": "meta",
}

# Campaign utm_source values that identify an AI referral.
UTM_SOURCES = {
    "chatgpt.com": "chatgpt", "chatgpt": "chatgpt", "openai": "chatgpt",
    "perplexity": "perplexity", "perplexity.ai": "perplexity",
    "claude.ai": "claude", "claude": "claude",
    "gemini": "gemini", "gemini.google.com": "gemini",
    "copilot": "copilot", "bing.com/chat": "copilot",
    "you.com": "you", "phind.com": "phind", "meta.ai": "meta",
}

# UA token regex -> (bot, owner, purpose). Google-Extended is a robots.txt token,
# not a user agent, so it is deliberately absent here.
BOT_UA = {
    r"GPTBot": ("GPTBot", "OpenAI", "training"),
    r"OAI-SearchBot": ("OAI-SearchBot", "OpenAI", "search-index"),
    r"ChatGPT-User": ("ChatGPT-User", "OpenAI", "user-fetch"),
    r"ClaudeBot": ("ClaudeBot", "Anthropic", "training"),
    r"Claude-User": ("Claude-User", "Anthropic", "user-fetch"),
    r"Claude-SearchBot": ("Claude-SearchBot", "Anthropic", "search-index"),
    r"PerplexityBot": ("PerplexityBot", "Perplexity", "search-index"),
    r"Perplexity-User": ("Perplexity-User", "Perplexity", "user-fetch"),
    r"Applebot": ("Applebot", "Apple", "search-index"),
    r"DuckAssistBot": ("DuckAssistBot", "DuckDuckGo", "user-fetch"),
    r"Meta-ExternalAgent": ("Meta-ExternalAgent", "Meta", "training"),
    r"Amazonbot": ("Amazonbot", "Amazon", "search-index"),
    r"CCBot": ("CCBot", "Common Crawl", "training"),
    r"Googlebot": ("Googlebot", "Google", "search-index"),
    r"bingbot": ("bingbot", "Microsoft", "search-index"),
}

# Crawlers whose fetches we treat as AI traffic; the rest are "other_bot".
AI_BOTS = {
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User",
    "Claude-SearchBot", "PerplexityBot", "Perplexity-User", "DuckAssistBot",
    "Meta-ExternalAgent", "Amazonbot", "CCBot",
}

# Paths only scanners ask for: a "bot" hitting these is hostile regardless of IP.
_SCANNER_RE = re.compile(r"(\.env|\.git|wp-|\.php|/admin|/phpmyadmin)", re.I)
_COMBINED_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]*)\]\s+"(?P<request>[^"]*)"'
    r'\s+(?P<status>\d{3})\s+(?P<size>\S+)'
    r'(?:\s+"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)")?'
)
_REQUEST_RE = re.compile(r"^(\S+)\s+(\S+)")
_BOT_RES = [(re.compile(pat, re.I), info) for pat, info in BOT_UA.items()]


def _g(row, *path, default=None):
    """Nested dict get: _g(row, "httpRequest", "userAgent")."""
    cur = row
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _iso(ts):
    """Normalise log timestamps to ISO-8601 strings (never datetime objects)."""
    if isinstance(ts, bool):
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(ts or "").strip()
    if not text:
        return ""
    if text.isdigit() and len(text) >= 10:  # epoch seconds as a string
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%d/%b/%Y:%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return text


def _epoch(ts):
    """ISO timestamp -> POSIX seconds, or None when unusable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _split_request(request):
    match = _REQUEST_RE.match(str(request or "").strip())
    return (match.group(1).upper(), match.group(2)) if match else ("", str(request or ""))


def _entry(ip, ts, method, path, status, referer, ua):
    return {
        "ip": str(ip or "").strip(),
        "ts": _iso(ts),
        "method": str(method or "").upper(),
        "path": str(path or ""),
        "status": _int(status),
        "referer": str(referer or ""),
        "ua": str(ua or ""),
    }


def parse_line(line, fmt="combined"):
    """Parse one access-log line into a normalised entry dict, or None."""
    if fmt not in ("combined", "json"):
        raise ToolError(f"unknown log format: {fmt!r}")
    if not isinstance(line, str) or not line.strip():
        return None
    line = line.strip()
    if fmt == "json":
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        ip = obj.get("ip") or obj.get("remote_addr") or _g(obj, "httpRequest", "remoteIp")
        if not ip:
            return None
        # "request" may be a full request line; "uri" is a bare path.
        raw = obj.get("request") or obj.get("uri") or _g(obj, "httpRequest", "requestUrl") or ""
        method, path = _split_request(raw)
        return _entry(
            ip,
            obj.get("time") or obj.get("ts") or obj.get("timestamp") or "",
            obj.get("method") or method,
            path,
            obj.get("status"),
            obj.get("referer") or obj.get("http_referer") or obj.get("referrer") or "",
            obj.get("ua") or obj.get("http_user_agent")
            or _g(obj, "httpRequest", "userAgent") or "",
        )
    match = _COMBINED_RE.match(line)
    if not match:
        return None
    method, path = _split_request(match.group("request"))
    return _entry(match.group("ip"), match.group("ts"), method, path,
                  match.group("status"), match.group("referer") or "",
                  match.group("ua") or "")


def load_ranges(mapping):
    """Compile {bot: [CIDR, ...]} into {bot: [ip_network, ...]}; raise on bad CIDR."""
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise ToolError("ranges must be a dict of bot -> list of CIDR strings")
    out = {}
    for bot, cidrs in mapping.items():
        if isinstance(cidrs, str):
            cidrs = [c for c in re.split(r"[,\s]+", cidrs) if c]
        if not isinstance(cidrs, (list, tuple, set)):
            raise ToolError(f"ranges for {bot!r} must be a list of CIDR strings")
        nets = []
        for cidr in cidrs:
            try:
                nets.append(ipaddress.ip_network(str(cidr).strip(), strict=False))
            except ValueError as exc:
                raise ToolError(f"invalid CIDR for {bot!r}: {scrub(str(exc))}") from None
        if nets:
            out[str(bot)] = nets
    return out


def _clean_ip(value):
    text = str(value or "").strip().strip("[]")
    if text.count(":") == 1 and "." in text:  # "1.2.3.4:5678"
        text = text.split(":", 1)[0]
    return text


def ip_in(ip, nets):
    """True when ip belongs to any of the compiled networks (never raises)."""
    if not ip or not nets:
        return False
    try:
        addr = ipaddress.ip_address(_clean_ip(ip))
    except ValueError:
        return False
    if isinstance(nets, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        nets = [nets]
    try:
        return any(addr in net for net in nets)
    except TypeError:
        return False


def _nets_for(bot, ranges):
    """Published ranges for a bot, or None when none are known (=> unverified)."""
    if not ranges:
        return None
    if bot in ranges:
        return ranges[bot]
    low = bot.lower()
    for key, nets in ranges.items():
        if str(key).lower() == low:
            return nets
    return None


def _match_bot(ua):
    text = str(ua or "")
    if text:
        for rx, info in _BOT_RES:
            if rx.search(text):
                return info
    return ("", "", "")


def _referrer_engine(referer, path=""):
    ref = str(referer or "").strip()
    if ref and ref != "-":
        try:
            parts = urlsplit(ref if "//" in ref else "//" + ref)
            host = (parts.hostname or "").lower()
            ref_path = parts.path or ""
        except ValueError:
            host, ref_path = "", ""
        if host.startswith("www."):
            host = host[4:]
        for key, engine in REFERRERS.items():
            key_host, _, key_path = key.partition("/")
            if ((host == key_host or host.endswith("." + key_host))
                    and (not key_path or ref_path.startswith("/" + key_path))):
                return engine
    query = parse_qs(urlsplit(str(path or "")).query)
    for value in query.get("utm_source", []):
        engine = UTM_SOURCES.get(value.strip().lower())
        if engine:
            return engine
    return None


def classify(entry, ranges=None):
    """Classify one parsed entry into a JSON-serialisable verdict dict."""
    if not isinstance(entry, dict):
        raise ToolError("entry must be a dict produced by parse_line()")
    ua = str(entry.get("ua") or "")
    path = str(entry.get("path") or "")
    bot, owner, purpose = _match_bot(ua)
    if bot:
        if bot not in AI_BOTS:
            return {"kind": "other_bot", "bot": bot}
        nets = _nets_for(bot, ranges)
        # Scanner paths are hostile even from a "verified" IP.
        if _SCANNER_RE.search(path) or (nets and not ip_in(entry.get("ip"), nets)):
            kind = "ai_bot_spoofed"
        elif nets:
            kind = "ai_bot_verified"
        else:
            kind = "ai_bot_unverified"
        return {"kind": kind, "bot": bot, "owner": owner, "purpose": purpose}
    engine = _referrer_engine(entry.get("referer"), path)
    if engine:
        return {"kind": "ai_referral", "engine": engine}
    return {"kind": "human"}


def _path_only(path):
    return str(path or "").split("#", 1)[0].split("?", 1)[0] or "/"


def _top(counts, limit):
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {key: value for key, value in ranked[:limit]}


def _suspects(hits, min_hits=20, spread=0.15):
    """IPs with >= min_hits fetches whose inter-arrival gaps are near-uniform."""
    out = []
    for ip, times in hits.items():
        if len(times) < min_hits:
            continue
        times = sorted(times)
        gaps = [b - a for a, b in zip(times, times[1:])]
        mean = sum(gaps) / len(gaps) if gaps else 0.0
        if mean <= 0:  # all hits inside the same second: no usable signal
            continue
        var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        if math.sqrt(var) < spread * mean:
            out.append(ip)
    return sorted(out)


def summarize(entries, ranges=None, site=""):
    """Aggregate parsed entries into referral/bot counters plus automation hints."""
    if entries is None:
        entries = []
    if not isinstance(entries, (list, tuple)):
        raise ToolError("entries must be a list of parsed log entries")
    referrals, landing, bots, bot_paths, hits = {}, {}, {}, {}, {}
    humans = spoofed_total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        verdict = classify(entry, ranges)
        kind = verdict["kind"]
        if kind == "ai_referral":
            engine = verdict["engine"]
            referrals[engine] = referrals.get(engine, 0) + 1
            page = _path_only(entry.get("path"))
            landing[page] = landing.get(page, 0) + 1
        elif kind.startswith("ai_bot_"):
            bot, bucket = verdict["bot"], kind[len("ai_bot_"):]
            stats = bots.setdefault(bot, {"verified": 0, "unverified": 0, "spoofed": 0})
            stats[bucket] += 1
            page = _path_only(entry.get("path"))
            paths = bot_paths.setdefault(bot, {})
            paths[page] = paths.get(page, 0) + 1
            if bucket == "spoofed":
                spoofed_total += 1
        elif kind == "human":
            humans += 1
        moment = _epoch(entry.get("ts"))
        ip = str(entry.get("ip") or "")
        if moment is not None and ip:
            hits.setdefault(ip, []).append(moment)
    return {
        "site": str(site or ""),
        "ai_referrals": _top(referrals, len(referrals)),
        "landing_pages": _top(landing, 20),
        "bot_fetches": {bot: bots[bot] for bot in sorted(bots)},
        "top_bot_paths": {bot: _top(bot_paths[bot], 10) for bot in sorted(bot_paths)},
        "humans": humans,
        "spoofed_total": spoofed_total,
        "automated_suspects": _suspects(hits),
    }


def weekly_line(summary):
    """Render a summary as one plain-text Slack line (no newlines)."""
    if not isinstance(summary, dict):
        raise ToolError("summary must be a dict produced by summarize()")
    site = str(summary.get("site") or "").strip()
    refs = summary.get("ai_referrals") or {}
    total_refs = sum(_int(v) for v in refs.values())
    ref_txt = ", ".join(f"{k} {_int(v)}" for k, v in list(refs.items())[:4]) or "none"
    bots = summary.get("bot_fetches") or {}
    verified = sum(_int(b.get("verified")) for b in bots.values() if isinstance(b, dict))
    unverified = sum(_int(b.get("unverified")) for b in bots.values() if isinstance(b, dict))
    spoofed = _int(summary.get("spoofed_total"))
    humans = _int(summary.get("humans"))
    prefix = f"[{site}] " if site else ""
    return (f"{prefix}AI traffic: {total_refs} AI referrals ({ref_txt}); "
            f"AI bots verified {verified}, unverified {unverified}, spoofed {spoofed}; "
            f"humans {humans}")


def selftest() -> int:
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        checks += 1
        assert cond, msg

    def ent(ip, ua="", path="/", ref="", ts="2025-05-01T10:00:00+00:00"):
        return {"ip": ip, "ts": ts, "method": "GET", "path": path,
                "status": 200, "referer": ref, "ua": ua}

    ranges = load_ranges({
        "GPTBot": ["198.51.100.0/24"],
        "ClaudeBot": "203.0.113.0/24, 2001:db8::/32",
    })

    combined = ('203.0.113.9 - - [01/May/2025:10:00:00 +0000] "GET /pricing?x=1 HTTP/1.1" '
                '200 512 "https://chatgpt.com/" "Mozilla/5.0 Chrome/124"')
    e = parse_line(combined)
    ok(e is not None, "combined line parses")
    ok(e["ip"] == "203.0.113.9" and e["method"] == "GET"
       and e["path"] == "/pricing?x=1", "combined fields")
    ok(e["status"] == 200 and e["ts"].startswith("2025-05-01T10:00:00"), "combined status/ts")
    ok(parse_line("") is None and parse_line("nonsense") is None
       and parse_line(None) is None, "bad combined lines")

    j = json.dumps({"remote_addr": "198.51.100.20", "time": "2025-05-01T10:05:00Z",
                    "request": "GET /a?utm_source=x HTTP/1.1", "status": "200",
                    "http_referer": "", "http_user_agent": "GPTBot/1.2"})
    je = parse_line(j, fmt="json")
    ok(je is not None and je["status"] == 200 and je["ip"] == "198.51.100.20", "json parses")
    ok(parse_line('{"uri": "/b", "remote_addr": "192.0.2.1"}', fmt="json")["path"] == "/b",
       "json uri fallback")
    ok(parse_line("[1,2]", fmt="json") is None and parse_line("{bad", fmt="json") is None,
       "malformed json")

    ok(ip_in("198.51.100.7", ranges["GPTBot"])
       and not ip_in("203.0.113.7", ranges["GPTBot"]), "ip_in membership")
    ok(not ip_in("not-an-ip", ranges["GPTBot"]) and not ip_in("198.51.100.7", None),
       "ip_in edge cases")
    ok(len(ranges["ClaudeBot"]) == 2, "comma-separated CIDR string")
    try:
        load_ranges({"X": ["10.0.0.0/33"]})
        ok(False, "invalid CIDR must raise")
    except ToolError:
        ok(True, "invalid CIDR raises ToolError")

    gpt = "Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)"
    ok(classify(ent("198.51.100.5", gpt, "/docs"), ranges)["kind"] == "ai_bot_verified",
       "verified bot")
    ok(classify(ent("198.51.100.5", gpt), None)["kind"] == "ai_bot_unverified",
       "unverified bot")
    ok(classify(ent("203.0.113.5", gpt), ranges)["kind"] == "ai_bot_spoofed", "spoofed ip")
    ok(classify(ent("198.51.100.5", gpt, "/.env"), ranges)["kind"] == "ai_bot_spoofed",
       "scanner path spoofed")
    ok(classify(ent("192.0.2.1", "", "/", "https://www.perplexity.ai/search?q=x"))["engine"]
       == "perplexity", "perplexity referer")
    ok(classify(ent("192.0.2.2", "", "/?utm_source=claude.ai"))["engine"] == "claude",
       "utm referral")
    ok(classify(ent("192.0.2.3", "", "/", "https://www.bing.com/chat?q=hi"))["engine"]
       == "copilot", "bing chat referer")
    ok(classify(ent("192.0.2.4", "", "/", "https://www.google.com/"))["kind"] == "human",
       "human")
    ok(classify(ent("192.0.2.5", "Mozilla/5.0 (compatible; Googlebot/2.1)"))["kind"]
       == "other_bot", "other bot")
    try:
        classify(None)
        ok(False, "classify(None) must raise")
    except ToolError:
        ok(True, "classify(None) raises")

    entries = [
        ent("192.0.2.1", "", "/pricing", "https://chatgpt.com/"),
        ent("192.0.2.2", "", "/?utm_source=perplexity"),
        ent("198.51.100.5", gpt, "/docs"),
        ent("203.0.113.5", gpt, "/docs"),
        ent("198.51.100.6", gpt, "/.env"),
        ent("192.0.2.9", "Mozilla/5.0 Chrome", "/"),
        ent("192.0.2.10", "Mozilla/5.0 (compatible; Googlebot/2.1)", "/"),
    ]
    s = summarize(entries, ranges, site="example.com")
    ok(s["site"] == "example.com"
       and s["ai_referrals"] == {"chatgpt": 1, "perplexity": 1}, "referral counts")
    ok(s["landing_pages"] == {"/": 1, "/pricing": 1}, "landing pages")
    ok(s["bot_fetches"].get("GPTBot") == {"verified": 1, "unverified": 0, "spoofed": 2},
       "bot fetch buckets")
    ok(s["humans"] == 1 and s["spoofed_total"] == 2, "human/spoof totals")
    ok(s["top_bot_paths"]["GPTBot"].get("/.env") == 1, "top bot paths")
    s2 = summarize(entries, None)
    ok(s2["bot_fetches"]["GPTBot"]["unverified"] == 2
       and s2["bot_fetches"]["GPTBot"]["spoofed"] == 1, "unverified without ranges")
    ok(json.dumps(s) == json.dumps(summarize(entries, ranges, site="example.com")),
       "deterministic and json-serialisable")
    empty = summarize([], None, site="x")
    ok(empty["ai_referrals"] == {} and empty["humans"] == 0
       and empty["automated_suspects"] == [], "empty summary")

    base = datetime(2025, 5, 1, tzinfo=timezone.utc)
    ts_at = lambda sec: (base + timedelta(seconds=sec)).isoformat()
    steady = [ent("198.18.0.1", ts=ts_at(i * 60)) for i in range(20)]
    jittery = [ent("198.18.0.2", ts=ts_at(i * 60 if i % 2 else i * 300)) for i in range(20)]
    ok(summarize(steady)["automated_suspects"] == ["198.18.0.1"], "steady automation flagged")
    ok(summarize(jittery)["automated_suspects"] == [], "jittery traffic not flagged")
    ok(summarize(steady[:19])["automated_suspects"] == [], "19 hits below threshold")

    line = weekly_line(s)
    ok(isinstance(line, str) and "\n" not in line and "example.com" in line, "weekly line")
    ok("chatgpt" in line and "spoofed 2" in line, "weekly line content")
    ok("0 AI referrals" in weekly_line({}), "weekly line empty summary")
    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
