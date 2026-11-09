"""Google Search Console (read-only) connector and organic-search analysis for AdRails.

Wraps the Search Console v3 REST API behind an injected
``http(method, url, headers, body) -> (status, body_bytes)`` callable and a token
provider (``.access_token()``, optional ``.invalidate()``): no import-time I/O, no
network in tests, and the access token never leaks into return values, logs or errors.

Every call is read-only (site listing + searchAnalytics query) and all inputs are
validated before any HTTP happens. Analysis helpers are pure and surface cheap
organic wins (striking distance, CTR gaps, paid/organic overlap, period trends) as
JSON-serialisable dicts. Tools should catch ToolError and show ``scrub(str(exc))``.
"""

from __future__ import annotations

import json
import time
from datetime import date
from urllib.parse import quote, unquote

from ..errors import ToolError, scrub

BASE = "https://www.googleapis.com/webmasters/v3"
_DIMS = ("date", "query", "page", "country", "device", "searchAppearance")
_OPS = ("equals", "contains", "notEquals", "notContains", "includingRegex", "excludingRegex")


# --------------------------------------------------------------------------- helpers
def _g(row, *path, default=None):
    """Nested dict getter tolerant of missing/None intermediates."""
    cur = row
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _f(value, default=0.0):
    """Coerce API numbers (often strings) to float; blank/missing/malformed -> default."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value):
    return "" if value is None else str(value)


def _norm(value):
    """Lowercase + whitespace-collapse; used to match paid terms to organic queries."""
    return " ".join(_text(value).lower().split())


def _is_date(value):
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _body_text(raw):
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return "" if raw is None else str(raw)


def _site_from_url(url):
    """Recover the property from a request URL (site is percent-encoded after /sites/)."""
    marker = "/sites/"
    if marker in url:
        return unquote(url.split(marker, 1)[1].split("/", 1)[0])
    return url


# --------------------------------------------------------------------------- client
class Client:
    """Thin read-only Search Console REST client."""

    def __init__(self, token_provider, http, base=BASE, max_rows=100000, sleep=None):
        self.token_provider = token_provider
        self.http = http
        self.base = base.rstrip("/")
        self.max_rows = int(max_rows)
        self.sleep = sleep or time.sleep  # injected in tests so nothing really waits
        self.truncated = False

    def _call(self, method, url, body=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        transient = 0
        auth_retried = False
        while True:
            # Re-read the token each attempt so a 401 refresh actually takes effect.
            token = self.token_provider.access_token()
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            status, raw = self.http(method, url, headers, payload)
            text = _body_text(raw)
            if status == 401 and not auth_retried:
                auth_retried = True
                invalidate = getattr(self.token_provider, "invalidate", None)
                if callable(invalidate):
                    invalidate()
                continue
            if status in (429, 500, 502, 503) and transient < 2:
                self.sleep(1 if transient == 0 else 3)
                transient += 1
                continue
            if 200 <= status < 300:
                try:
                    return json.loads(text or "{}")
                except ValueError:
                    raise ToolError("Search Console returned invalid JSON") from None
            if status == 403 and any(w in text.lower() for w in ("ratelimit", "rate limit", "quota", "usagelimits")):
                raise ToolError("Search Console quota or rate limit reached: try again later (" + scrub(text, 200) + ")")
            if status == 403:
                # Never echo the token; name the property so access can be granted.
                raise ToolError(
                    "Search Console 403 for %s: add the service account or Google login "
                    "as a user on that Search Console property" % _site_from_url(url)
                )
            raise ToolError("Search Console HTTP %d: %s" % (status, scrub(text, 300)))

    def sites(self):
        data = self._call("GET", self.base + "/sites")
        entries = data.get("siteEntry") if isinstance(data, dict) else []
        out = []
        for entry in entries or []:
            if isinstance(entry, dict):
                out.append({
                    "site": _text(_g(entry, "siteUrl", default="")),
                    "permission": _text(_g(entry, "permissionLevel", default="")),
                })
        out.sort(key=lambda item: item["site"])
        return out

    def query(self, site, start, end, dimensions, row_limit=25000, data_state="all", filters=None):
        # --- validation happens before any HTTP -------------------------------
        if not isinstance(site, str) or not (
            site.startswith("https://") or site.startswith("http://") or site.startswith("sc-domain:")
        ):
            raise ToolError("site must start with https://, http:// or sc-domain:")
        if not _is_date(start) or not _is_date(end) or start > end:
            raise ToolError("dates must be YYYY-MM-DD and start <= end")
        if not isinstance(dimensions, (list, tuple)) or not dimensions:
            raise ToolError("dimensions must be a non-empty list")
        dimensions = list(dimensions)
        if any(d not in _DIMS for d in dimensions) or len(set(dimensions)) != len(dimensions):
            raise ToolError("invalid or duplicate dimensions")
        try:
            rl = int(row_limit)
        except (TypeError, ValueError):
            raise ToolError("row_limit must be 1..25000") from None
        if not 1 <= rl <= 25000:
            raise ToolError("row_limit must be 1..25000")
        if data_state not in ("all", "final"):
            raise ToolError("data_state must be 'all' or 'final'")
        if filters is not None:
            if not isinstance(filters, list):
                raise ToolError("filters must be a list")
            for item in filters:
                if (not isinstance(item, dict) or item.get("operator") not in _OPS
                        or not item.get("dimension") or "expression" not in item):
                    raise ToolError("invalid dimension filter")

        self.truncated = False
        rows = []
        start_row = 0
        url = self.base + "/sites/" + quote(site, safe="") + "/searchAnalytics/query"
        while start_row < self.max_rows:
            body = {
                "startDate": start,
                "endDate": end,
                "dimensions": dimensions,
                "rowLimit": rl,
                "startRow": start_row,
                "dataState": data_state,
                "type": "web",
            }
            if filters is not None:
                body["dimensionFilterGroups"] = [{"filters": filters}]
            data = self._call("POST", url, body)
            page = data.get("rows") if isinstance(data, dict) else []
            if not isinstance(page, list):
                page = []
            left = self.max_rows - len(rows)
            if len(page) > left:
                rows.extend(page[:left])
                self.truncated = True
                break
            rows.extend(page)
            if len(page) < rl:  # short page => done
                break
            start_row += rl
            if start_row >= self.max_rows:
                self.truncated = True
                break
        return rows


# --------------------------------------------------------------------------- analysis
def aggregate(rows, dims, by):
    """Collapse API rows (keys aligned to ``dims``) to one row per ``by`` value."""
    dims = list(dims or [])
    if by not in dims:
        raise ToolError("by must be one of dimensions")
    index = dims.index(by)
    acc = {}
    for row in rows or []:
        r = row if isinstance(row, dict) else {}
        keys = r.get("keys") if isinstance(r.get("keys"), list) else None
        value = r.get(by)
        if value is None and keys is not None and index < len(keys):
            value = keys[index]
        key = "" if value is None else str(value)
        if by == "query":
            key = key.lower()
        clicks, impressions, position = _f(r.get("clicks")), _f(r.get("impressions")), _f(r.get("position"))
        a = acc.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0])  # clicks, imps, imp*pos, pos sum, count
        a[0] += clicks
        a[1] += impressions
        a[2] += position * impressions
        a[3] += position
        a[4] += 1
    out = []
    for key, (clicks, impressions, weighted, pos_sum, count) in acc.items():
        position = weighted / impressions if impressions else (pos_sum / count if count else 0.0)
        out.append({
            by: key,
            "clicks": clicks,
            "impressions": impressions,
            "ctr": clicks / impressions if impressions else 0.0,
            "position": position,
        })
    out.sort(key=lambda item: str(item[by]))
    return out


_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.018}


def _ctr_anchor(pos):
    if pos <= 10:
        return _CTR[max(1, int(pos))]
    if pos <= 20:
        return 0.012 - (pos - 11) * (0.007 / 9.0)  # 0.012 at 11 -> 0.005 at 20
    return 0.003


def expected_ctr(position):
    """Conservative organic CTR curve; fractional positions interpolate linearly."""
    pos = _f(position)
    if pos != pos or pos < 1:  # NaN or below the first slot
        pos = 1.0
    if pos > 20:
        return 0.003
    if pos == int(pos):
        return _ctr_anchor(int(pos))
    lo = int(pos)
    return _ctr_anchor(lo) + (pos - lo) * (_ctr_anchor(lo + 1) - _ctr_anchor(lo))


def _base_item(row):
    r = row if isinstance(row, dict) else {}
    clicks, impressions = _f(r.get("clicks")), _f(r.get("impressions"))
    item = {}
    for name in ("query", "page"):
        if name in r:
            item[name] = _text(r[name])
    item["clicks"] = clicks
    item["impressions"] = impressions
    item["ctr"] = _f(r.get("ctr"), clicks / impressions if impressions else 0.0)
    item["position"] = _f(r.get("position"))
    return item


def _item_key(item):
    return _text(item.get("query") if "query" in item else item.get("page")).lower()


def striking_distance(rows, min_impressions=50, lo=4.0, hi=20.0, limit=25):
    """Rows already on page 1-2: the cheapest SEO wins."""
    out = []
    for row in rows or []:
        r = row if isinstance(row, dict) else {}
        position, impressions = _f(r.get("position")), _f(r.get("impressions"))
        if lo <= position <= hi and impressions >= min_impressions:
            item = _base_item(r)
            item["potential_clicks"] = max(0, int(round(impressions * expected_ctr(3) - item["clicks"])))
            out.append(item)
    out.sort(key=lambda item: (-item["potential_clicks"], _item_key(item)))
    return out[:max(0, int(limit))]


def ctr_gaps(rows, min_impressions=100, limit=25):
    """Rows on page 1 whose CTR is far below the curve (weak title/description)."""
    out = []
    for row in rows or []:
        item = _base_item(row)
        if item["impressions"] >= min_impressions and item["position"] <= 10:
            expected = expected_ctr(item["position"])
            if item["ctr"] < 0.5 * expected:
                item["missing_clicks"] = int(round(item["impressions"] * expected - item["clicks"]))
                out.append(item)
    out.sort(key=lambda item: (-item["missing_clicks"], _item_key(item)))
    return out[:max(0, int(limit))]


def _median(values):
    values = sorted(values)
    if not values:
        return 0.0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def paid_organic_overlap(gsc_rows, ads_terms, max_position=3.0, min_cost=1.0):
    """Paid search terms where the site already ranks top-3 organically."""
    gmap = {}
    for row in gsc_rows or []:
        r = row if isinstance(row, dict) else {}
        query = _norm(r.get("query"))
        if query and query not in gmap:
            gmap[query] = r
    items = []
    for term in ads_terms or []:
        t = term if isinstance(term, dict) else {}
        text, cost, conversions = _text(t.get("term")), _f(t.get("cost")), _f(t.get("conversions"))
        if cost < min_cost:
            continue
        gsc = gmap.get(_norm(text))
        if not gsc:
            continue
        position = _f(gsc.get("position"))
        if position > max_position:
            continue
        items.append({
            "term": text,
            "cost": cost,
            "paid_clicks": _f(t.get("clicks")),
            "conversions": conversions,
            "organic_position": position,
            "organic_clicks": _f(gsc.get("clicks")),
            "organic_impressions": _f(gsc.get("impressions")),
        })
    median_cpa = _median([i["cost"] / i["conversions"] for i in items if i["conversions"] > 0])
    for item in items:
        if item["conversions"] == 0 or item["cost"] / item["conversions"] > median_cpa:
            rk = int(round(max(1.0, item["organic_position"])))
            item["advice"] = "test pausing or lowering bids: you already rank #%d organically" % rk
        else:
            item["advice"] = "keep: paid converts here; watch for cannibalisation"
        item["cost"] = round(item["cost"], 2)
    items.sort(key=lambda item: (-item["cost"], _text(item["term"]).lower()))
    return items


def _period(rows, key):
    """Aggregate one period into {name: (clicks, impressions, position)}."""
    acc = {}
    for row in rows or []:
        r = row if isinstance(row, dict) else {}
        name = _text(r.get(key))
        if key == "query":
            name = name.lower()
        if not name:
            continue
        clicks, impressions, position = _f(r.get("clicks")), _f(r.get("impressions")), _f(r.get("position"))
        a = acc.setdefault(name, [0.0, 0.0, 0.0, 0.0, 0])
        a[0] += clicks
        a[1] += impressions
        a[2] += position * impressions
        a[3] += position
        a[4] += 1
    return {
        k: (a[0], a[1], a[2] / a[1] if a[1] else (a[3] / a[4] if a[4] else 0.0))
        for k, a in acc.items()
    }


def trends(before_rows, after_rows, key="query", min_impressions=20, limit=20):
    """Compare two periods aggregated by ``key``; deterministic ordering, ties by key."""
    before, after = _period(before_rows, key), _period(after_rows, key)

    def item_for(name, cb, ca, pb, pa):
        return {
            "key": name,
            "clicks_before": cb,
            "clicks_after": ca,
            "clicks_change": ca - cb,
            "position_before": pb,
            "position_after": pa,
        }

    winners, losers, new, lost = [], [], [], []
    for name in set(before) & set(after):
        cb, _ib, pb = before[name]
        ca, _ia, pa = after[name]
        if ca > cb:
            winners.append(item_for(name, cb, ca, pb, pa))
        elif ca < cb:
            losers.append(item_for(name, cb, ca, pb, pa))
    for name in set(after) - set(before):
        ca, ia, pa = after[name]
        if ia >= min_impressions:
            new.append(item_for(name, 0.0, ca, None, pa))
    for name in set(before) - set(after):
        cb, ib, pb = before[name]
        if ib >= min_impressions:
            lost.append(item_for(name, cb, 0.0, pb, None))
    winners.sort(key=lambda i: (-i["clicks_change"], i["key"]))
    losers.sort(key=lambda i: (i["clicks_change"], i["key"]))
    new.sort(key=lambda i: (-i["clicks_after"], i["key"]))
    lost.sort(key=lambda i: (-i["clicks_before"], i["key"]))
    cut = max(0, int(limit))
    return {"winners": winners[:cut], "losers": losers[:cut], "new": new[:cut], "lost": lost[:cut]}


def opportunities(rows, ads_terms=None, limit=10):
    """One ranked list of cheap wins (clicks-equivalent), owner-friendly 'why' text."""
    items = []
    for item in striking_distance(rows, limit=100000):
        if item["potential_clicks"] <= 0:
            continue
        rk = int(round(max(1.0, item["position"])))
        items.append({
            "kind": "striking_distance",
            "key": _text(item.get("query") or item.get("page")),
            "value_clicks": item["potential_clicks"],
            "why": "rank #%d with %d impressions; about %d extra clicks available"
                   % (rk, int(item["impressions"]), item["potential_clicks"]),
            "_rank": float(item["potential_clicks"]),
        })
    for item in ctr_gaps(rows, limit=100000):
        if item["missing_clicks"] <= 0:
            continue
        rk = int(round(max(1.0, item["position"])))
        items.append({
            "kind": "ctr_gap",
            "key": _text(item.get("query") or item.get("page")),
            "value_clicks": item["missing_clicks"],
            "why": "ranks #%d but CTR %.1f%% vs %.1f%% expected; about %d missed clicks"
                   % (rk, item["ctr"] * 100, expected_ctr(item["position"]) * 100, item["missing_clicks"]),
            "_rank": float(item["missing_clicks"]),
        })
    if ads_terms:
        cpcs = []
        for term in ads_terms:
            t = term if isinstance(term, dict) else {}
            cost, clicks = _f(t.get("cost")), _f(t.get("clicks"))
            if cost > 0 and clicks > 0:
                cpcs.append(cost / clicks)
        denom = max(1.0, _median(cpcs))
        for item in paid_organic_overlap(rows, ads_terms):
            rk = int(round(max(1.0, item["organic_position"])))
            items.append({
                "kind": "paid_organic_overlap",
                "key": item["term"],
                "value_cost": item["cost"],
                "why": "you already rank #%d organically; paid spend %.2f on %d clicks"
                       % (rk, item["cost"], int(round(item["paid_clicks"]))),
                "_rank": item["cost"] / denom,
            })
    items.sort(key=lambda i: (-i["_rank"], i["kind"], _text(i["key"]).lower()))
    for item in items:
        item.pop("_rank", None)
    return items[:max(0, int(limit))]


# --------------------------------------------------------------------------- selftest
def selftest() -> int:
    n = 0

    def ck(cond, msg="check"):
        nonlocal n
        assert cond, msg
        n += 1

    def refused(fn):
        try:
            fn()
        except ToolError:
            return True
        return False

    class TP:
        def __init__(self):
            self.token = "TOKEN_SECRET_123"
            self.invalidated = 0

        def access_token(self):
            return self.token

        def invalidate(self):
            self.invalidated += 1
            self.token = "TOKEN_SECRET_456"

    # --- sites() ---------------------------------------------------------
    seen = []

    def http_sites(method, url, headers, body):
        seen.append((method, url, headers))
        return 200, json.dumps({"siteEntry": [
            {"siteUrl": "https://b.com/", "permissionLevel": "owner"},
            {"siteUrl": "https://a.com/", "permissionLevel": "siteFullUser"},
        ]}).encode()

    sites = Client(TP(), http_sites).sites()
    ck(sites == [{"site": "https://a.com/", "permission": "siteFullUser"},
                 {"site": "https://b.com/", "permission": "owner"}], "sites sorted")
    ck(seen[0][0] == "GET" and seen[0][2]["Content-Type"] == "application/json", "headers")
    ck(seen[0][2]["Authorization"].startswith("Bearer "), "bearer header")

    # --- paging across three pages ---------------------------------------
    starts = []

    def http_pages(method, url, headers, body):
        data = json.loads(body)
        starts.append(data["startRow"])
        count = 25000 if data["startRow"] in (0, 25000) else 1
        return 200, json.dumps({"rows": [{} for _ in range(count)]}).encode()

    c = Client(TP(), http_pages)
    rows = c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query"])
    ck(starts == [0, 25000, 50000], "paged startRows")
    ck(len(rows) == 50001, "all rows collected")
    ck(c.truncated is False, "not truncated")

    # --- max_rows truncation ---------------------------------------------
    c = Client(TP(), lambda *a: (200, b'{"rows":[{"x":1},{"x":2}]}'), max_rows=3)
    rows = c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query"], row_limit=2)
    ck(len(rows) == 3 and c.truncated is True, "max_rows truncation")

    # --- 401 -> invalidate + one retry -----------------------------------
    tp = TP()
    calls = [0]

    def http_401(method, url, headers, body):
        calls[0] += 1
        return (401, b'{"error":"expired"}') if calls[0] == 1 else (200, b'{"siteEntry":[]}')

    c = Client(tp, http_401)
    ck(c.sites() == [] and tp.invalidated == 1 and calls[0] == 2, "401 retry")

    # --- 429 backoff with a fake sleep -----------------------------------
    waits, calls = [], [0]

    def http_429(method, url, headers, body):
        calls[0] += 1
        return (429, b"{}") if calls[0] < 3 else (200, b'{"siteEntry":[]}')

    Client(TP(), http_429, sleep=waits.append).sites()
    ck(waits == [1, 3], "backoff 1s then 3s")
    ck(calls[0] == 3, "three attempts")

    # --- 403 names the site but never the token --------------------------
    c = Client(TP(), lambda *a: (403, b'{"error":"forbidden"}'))
    msg = ""
    try:
        c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query"], row_limit=10)
    except ToolError as exc:
        msg = str(exc)
    ck("https://a.com/" in msg, "403 names site")
    ck("TOKEN_SECRET" not in msg, "403 hides token")

    # --- invalid JSON ----------------------------------------------------
    ck(refused(lambda: Client(TP(), lambda *a: (200, b"<html>")).sites()), "bad JSON refused")

    # --- input validation (no HTTP allowed) ------------------------------
    def nohttp(*args):
        raise AssertionError("HTTP must not be called")

    c = Client(TP(), nohttp)
    ck(refused(lambda: c.query("a.com", "2024-01-01", "2024-01-31", ["query"])), "bad site")
    ck(refused(lambda: c.query("https://a.com/", "2024-02-01", "2024-01-01", ["query"])), "bad dates")
    ck(refused(lambda: c.query("https://a.com/", "2024-01-01", "2024-01-31", ["bogus"])), "bad dim")
    ck(refused(lambda: c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query", "query"])), "dup dims")
    ck(refused(lambda: c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query"], row_limit=0)), "row_limit")
    ck(refused(lambda: c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query"],
                               filters=[{"dimension": "query", "operator": "nope", "expression": "x"}])), "bad filter")

    # --- URL encoding + request body -------------------------------------
    bodies, urls = [], []

    def http_url(method, url, headers, body):
        urls.append(url)
        bodies.append(json.loads(body))
        return 200, b'{"rows":[]}'

    c = Client(TP(), http_url)
    c.query("sc-domain:x.com", "2024-01-01", "2024-01-31", ["query"], row_limit=1)
    c.query("https://a.com/", "2024-01-01", "2024-01-31", ["query"], row_limit=1,
            filters=[{"dimension": "query", "operator": "contains", "expression": "shoe"}])
    ck("sc-domain%3Ax.com" in urls[0], "sc-domain encoded")
    ck("https%3A%2F%2Fa.com%2F" in urls[1], "https url encoded")
    ck(bodies[0]["dataState"] == "all" and bodies[0]["type"] == "web", "default body")
    ck(bodies[1]["dimensionFilterGroups"] == [{"filters": [
        {"dimension": "query", "operator": "contains", "expression": "shoe"}]}], "filters forwarded")

    # --- aggregation -----------------------------------------------------
    agg = aggregate([
        {"query": "A", "clicks": "5", "impressions": "100", "position": "2"},
        {"query": "a", "clicks": 5, "impressions": 300, "position": 4},
    ], ["query"], "query")
    ck(len(agg) == 1 and agg[0]["query"] == "a", "lowercased aggregation")
    ck(agg[0]["clicks"] == 10.0 and agg[0]["impressions"] == 400.0, "summed")
    ck(abs(agg[0]["position"] - 3.5) < 1e-9, "weighted position")
    ck(abs(agg[0]["ctr"] - 0.025) < 1e-9, "recomputed ctr")
    ck(aggregate([], ["query"], "query") == [], "empty aggregate")
    ck(refused(lambda: aggregate([], ["query"], "page")), "aggregate by not in dims")

    # --- expected CTR curve ----------------------------------------------
    ck(abs(expected_ctr(1.5) - 0.215) < 1e-9, "interp 1.5")
    ck(abs(expected_ctr(15) - (0.012 - 4 * (0.007 / 9))) < 1e-9, "interp 15")
    seq = [expected_ctr(x) for x in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 15, 20, 21, 50)]
    ck(all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1)), "monotonic")
    ck(expected_ctr(0) == 0.28 and expected_ctr(50) == 0.003, "clamped ends")

    # --- striking distance -----------------------------------------------
    sd = striking_distance([
        {"query": "q1", "position": 5, "impressions": 100, "clicks": 2},
        {"query": "q2", "position": 30, "impressions": 1000, "clicks": 100},
        {"query": "q3", "position": 6, "impressions": 10, "clicks": 0},
        {"query": "q4", "position": 7, "impressions": 200, "clicks": 1},
    ])
    ck([x["query"] for x in sd] == ["q4", "q1"], "striking sorted")
    ck(sd[0]["potential_clicks"] == 19, "potential clicks")
    ck(striking_distance([{"query": "x"}]) == [], "missing keys tolerated")

    # --- CTR gaps --------------------------------------------------------
    cg = ctr_gaps([{"query": "g", "position": 1, "impressions": 1000, "clicks": 10},
                   {"query": "ok", "position": 1, "impressions": 1000, "clicks": 200}])
    ck(len(cg) == 1 and cg[0]["missing_clicks"] == 270, "ctr gap")

    # --- paid/organic overlap --------------------------------------------
    ov = paid_organic_overlap(
        [{"query": "  Buy  Shoes ", "position": 2, "clicks": 50, "impressions": 500},
         {"query": "CRM   Tool", "position": 1, "clicks": 40, "impressions": 1000}],
        [{"term": "buy shoes", "cost": 20.0, "clicks": 10, "conversions": 0},
         {"term": "crm tool", "cost": 30.0, "clicks": 5, "conversions": 2}])
    ck(len(ov) == 2 and ov[0]["term"] == "crm tool", "overlap sorted by cost")
    ck(ov[0]["advice"].startswith("keep"), "keep advice")
    ck("already rank #2" in ov[1]["advice"], "pause advice")
    ck(paid_organic_overlap([{"query": "x", "position": 1}], [{"term": "x", "cost": 0.5}]) == [], "min_cost filter")
    ck(paid_organic_overlap([{"query": "y", "position": 9}], [{"term": "y", "cost": 5.0}]) == [], "max_position filter")

    # --- trends ----------------------------------------------------------
    before = [
        {"query": "win", "clicks": 10, "impressions": 100, "position": 5},
        {"query": "lose", "clicks": 20, "impressions": 100, "position": 4},
        {"query": "lost", "clicks": 1, "impressions": 30, "position": 6},
        {"query": "stay", "clicks": 5, "impressions": 50, "position": 7},
    ]
    after = [
        {"query": "win", "clicks": 30, "impressions": 120, "position": 3},
        {"query": "lose", "clicks": 2, "impressions": 100, "position": 4},
        {"query": "stay", "clicks": 5, "impressions": 50, "position": 6},  # unchanged: no winner/loser
        {"query": "new", "clicks": 4, "impressions": 25, "position": 2},
        {"query": "tiny", "clicks": 9, "impressions": 5, "position": 1},
    ]
    tr = trends(before, after)
    ck([x["key"] for x in tr["winners"]] == ["win"] and tr["winners"][0]["clicks_change"] == 20.0, "winner")
    ck([x["key"] for x in tr["losers"]] == ["lose"], "loser")
    ck([x["key"] for x in tr["new"]] == ["new"], "new filtered by impressions")
    ck([x["key"] for x in tr["lost"]] == ["lost"] and tr["lost"][0]["position_after"] is None, "lost")
    ck(trends([], []) == {"winners": [], "losers": [], "new": [], "lost": []}, "empty trends")

    # --- opportunities ---------------------------------------------------
    rows = [
        {"query": "q1", "position": 5, "impressions": 1000, "clicks": 2, "ctr": 0.002},
        {"query": "q2", "position": 2, "impressions": 5000, "clicks": 40, "ctr": 0.008},
    ]
    opp = opportunities(rows, ads_terms=[{"term": "q1", "cost": 40.0, "clicks": 8, "conversions": 0}])
    ck(opp and opp[0]["kind"] == "ctr_gap" and opp[0]["key"] == "q2", "top opportunity")
    ck(opp[0]["value_clicks"] == 710, "click value")
    ck(opportunities(rows) == opportunities(rows), "deterministic")
    ck(opportunities(rows, limit=1) == [opp[0]], "limit honoured")
    ck(all("_rank" not in o for o in opp), "no internal keys")

    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
