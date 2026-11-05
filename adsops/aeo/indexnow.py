"""IndexNow / Bing URL-submission helpers for AdRails (adsops.aeo.indexnow).

Pure builders plus guarded submitters for search/answer-engine URL push APIs
(IndexNow: Bing, Yandex, Seznam, Naver; Bing Webmaster SubmitUrlBatch).
Safety: no I/O at import time — every external effect goes through an injected
callable (``http=``, ``fetch=``). IndexNow keys are validated (8-128 chars of
[A-Za-z0-9-]) and the Bing API key is NEVER placed in builder output (only a
``{PLACEHOLDER}`` the caller substitutes at send time). Submitted URLs must be
https on the exact host, so a tool cannot be tricked into vouching for foreign
domains. A tool should preview with ``build_*`` first, then submit, and surface
``meaning`` strings to the user verbatim.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from adsops.errors import ToolError, RailViolation, scrub

INDEXNOW_URL = "https://api.indexnow.org/indexnow"
BING_SUBMIT_URL = "https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlbatch"
MAX_URLS = 10000
_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
_KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MEANINGS = {
    400: "bad request",
    403: "key not valid",
    422: "URLs don't belong to host or key mismatch",
    429: "too many requests",
}


def _g(row, *path, default=None):
    """Nested getter for Google-Ads-style camelCase rows; missing/None -> default."""
    cur = row
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return default if cur is None else cur


def _num(value) -> float:
    """Coerce Google Ads numeric strings defensively; junk/missing -> 0.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def validate_key(key) -> str:
    """Return the IndexNow key if valid, else raise ToolError."""
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ToolError("IndexNow key must be 8-128 chars of [A-Za-z0-9-]")
    return key


def _norm_host(host) -> str:
    """Lower-case bare host from a host or URL string (no scheme, no trailing slash)."""
    if not isinstance(host, str):
        raise ToolError("host must be a string")
    h = host.strip().lower()
    for prefix in ("https://", "http://"):
        if h.startswith(prefix):
            h = h[len(prefix):]
    h = h.rstrip("/")
    if not h or "/" in h or " " in h:
        raise ToolError(f"invalid host: {scrub(str(host), 120)}")
    return h


def _clean_urls(host: str, urls) -> list:
    """Validate (https + exact host), dedupe preserving order. Raises on offenders."""
    if urls is None:
        urls = []
    if isinstance(urls, str):
        urls = [urls]
    cleaned, seen, bad = [], set(), []
    for raw in urls:
        if not isinstance(raw, str):
            bad.append(scrub(repr(raw), 80))
            continue
        url = raw.strip()
        parsed = None
        if url.lower().startswith("https://"):
            try:
                parsed = urlparse(url)
            except ValueError:
                parsed = None
        # Exact host only (no subdomains): netloc is compared case-insensitively.
        if parsed is None or not parsed.netloc or parsed.netloc.lower() != host:
            bad.append(scrub(url, 200))
            continue
        if url not in seen:
            seen.add(url)
            cleaned.append(url)
    if bad:
        shown = ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
        raise ToolError(f"URLs must be https on host {host}; offenders: {shown}")
    if not cleaned:
        raise ToolError(f"urls must contain at least 1 https URL on host {host}")
    return cleaned


def build_indexnow(host, key, urls, key_location=None):
    """Build an IndexNow request: (endpoint_url, headers, body_dict). Raises ToolError."""
    norm = _norm_host(host)
    validate_key(key)
    clean = _clean_urls(norm, urls)
    if len(clean) > MAX_URLS:
        raise ToolError(f"at most {MAX_URLS} urls per IndexNow request (got {len(clean)})")
    body = {"host": norm, "key": key, "urlList": clean}
    if key_location:
        loc = str(key_location).strip()
        if not loc.lower().startswith("https://"):
            raise ToolError("keyLocation must be an https URL")
        body["keyLocation"] = loc
    return INDEXNOW_URL, dict(_JSON_HEADERS), body


def _status_int(value, who: str) -> int:
    """Status codes arrive as ints; tolerate numeric strings, reject garbage."""
    if isinstance(value, bool):  # bool is an int subclass: never a valid status here
        raise ToolError(f"{who} returned a non-integer status")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{who} returned a non-integer status")


def submit_indexnow(host, key, urls, http, key_location=None):
    """POST to IndexNow in batches of 10000.

    ``http(method, url, headers, body_bytes) -> (status:int, body:str)``.
    Single batch -> {"ok", "status"[, "meaning"]}; multiple -> {"ok", "batches": [...]}.
    """
    if not callable(http):
        raise ToolError("http callable is required")
    norm = _norm_host(host)
    validate_key(key)
    clean = _clean_urls(norm, urls)
    batches = [clean[i:i + MAX_URLS] for i in range(0, len(clean), MAX_URLS)]
    if len(batches) > 10:  # safety rail: never mass-submit >100k URLs in one call
        raise RailViolation("refusing to submit more than 100000 URLs in one call")
    results = []
    for batch in batches:
        url, headers, body = build_indexnow(norm, key, batch, key_location)
        payload = json.dumps(body).encode("utf-8")
        try:
            status, _resp = http("POST", url, headers, payload)  # body may echo secrets: dropped
        except Exception as exc:  # noqa: BLE001 - surface as user-visible failure
            raise ToolError("IndexNow request failed: " + scrub(str(exc)))
        status = _status_int(status, "http")
        if status in (200, 202):
            results.append({"ok": True, "status": status})
        else:
            results.append({"ok": False, "status": status,
                            "meaning": _MEANINGS.get(status, "unexpected status")})
    if len(results) == 1:
        return results[0]
    return {"ok": all(r["ok"] for r in results), "batches": results}


def check_key_file(host, key, fetch):
    """Verify https://host/<key>.txt serves exactly the key. ``fetch(url)->(status, body)``."""
    norm = _norm_host(host)
    validate_key(key)
    if not callable(fetch):
        raise ToolError("fetch callable is required")
    url = f"https://{norm}/{key}.txt"
    try:
        status, body = fetch(url)
    except Exception as exc:  # noqa: BLE001
        raise ToolError("key file fetch failed: " + scrub(str(exc)))
    status = _status_int(status, "fetch")
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace")
    elif body is None:
        text = ""
    else:
        text = body if isinstance(body, str) else str(body)
    return {"ok": status == 200 and text.strip() == key, "status": status, "url": url}


def build_bing_submit(site_url, urls, api_key_name="BING_API_KEY"):
    """Describe the Bing SubmitUrlBatch request with a key placeholder (no key value)."""
    if not isinstance(site_url, str) or not site_url.strip().lower().startswith("https://"):
        raise ToolError("site_url must be an https URL")
    site = site_url.strip()
    try:
        site_host = (urlparse(site).netloc or "").lower()
    except ValueError:
        site_host = ""
    if not site_host:
        raise ToolError("site_url must be an https URL with a host")
    if not isinstance(api_key_name, str) or not _KEY_NAME_RE.match(api_key_name):
        raise ToolError("api_key_name must be a simple env-var style identifier")
    if urls is None:
        urls = []
    if isinstance(urls, str):
        urls = [urls]
    clean, seen, bad = [], set(), []
    for raw in urls:
        url = raw.strip() if isinstance(raw, str) else ""
        try:
            host = (urlparse(url).netloc or "").lower() if url.lower().startswith("https://") else ""
        except ValueError:
            host = ""
        # Bing accepts the site root plus its subdomains.
        if not host or not (host == site_host or host.endswith("." + site_host)):
            bad.append(scrub(url or repr(raw), 200))
            continue
        if url not in seen:
            seen.add(url)
            clean.append(url)
    if bad:
        shown = ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
        raise ToolError(f"urls must be https under {site_host}; offenders: {shown}")
    if not clean:
        raise ToolError("urls must contain at least 1 https URL")
    if len(clean) > MAX_URLS:
        raise ToolError(f"at most {MAX_URLS} urls per Bing batch (got {len(clean)})")
    return {
        "method": "POST",
        "url": f"{BING_SUBMIT_URL}?apikey={{{api_key_name}}}",
        "headers": dict(_JSON_HEADERS),
        "body": {"siteUrl": site, "urlList": clean},
        "key_ref": api_key_name,  # name only; the caller injects the value at send time
        "note": "caller substitutes the API key at send time; it is never stored here",
    }


def _local(tag) -> str:
    """Namespace-stripped, lower-cased XML tag name."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _parse_iso(value):
    """ISO-8601 (with/without Z, date-only) -> aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fresh(lastmod, since) -> bool:
    """True if lastmod >= since; missing/unparsable lastmod is not fresh."""
    if not lastmod:
        return False
    dt = _parse_iso(lastmod)
    return dt is not None and dt >= since


def changed_urls(sitemap_xml, since_iso):
    """Filter a sitemap by lastmod.

    A <urlset> returns a list of matching <loc> URLs.
    A <sitemapindex> returns {"indexes": [child sitemap URLs]}.
    """
    since = _parse_iso(since_iso)
    if since is None:
        raise ToolError("since_iso must be an ISO-8601 timestamp")
    if not sitemap_xml or not str(sitemap_xml).strip():
        return []
    try:
        root = ET.fromstring(sitemap_xml)
    except ET.ParseError as exc:
        raise ToolError("invalid sitemap XML: " + scrub(str(exc), 300))
    kind = _local(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        raise ToolError(f"unsupported sitemap root element: {kind or '?'}")
    child_tag = "sitemap" if kind == "sitemapindex" else "url"
    found = []
    for node in root:
        if _local(node.tag) != child_tag:
            continue
        loc, lastmod = "", ""
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip()
        if loc and _fresh(lastmod, since):
            found.append(loc)
    if kind == "urlset":
        return found
    return {"indexes": found}


def selftest() -> int:
    """Mocked end-to-end checks. Returns the number of assertions."""
    n = 0

    def check(cond, msg):
        nonlocal n
        if not cond:
            raise AssertionError(msg)
        n += 1

    def raises(exc_type, fn):
        try:
            fn()
        except exc_type:
            return True
        except Exception:  # noqa: BLE001 - wrong error type means the check fails
            return False
        return False

    # --- _g / _num helpers handle malformed Google Ads rows
    row = {"campaign": {"id": "1", "name": "x"}, "metrics": {"costMicros": "2500000", "clicks": "3"}}
    check(_g(row, "campaign", "name") == "x", "_g nested")
    check(_g(row, "metrics", "missing", default=0) == 0, "_g default")
    check(_num("2500000") == 2500000.0 and _num(None) == 0.0 and _num("junk") == 0.0, "_num coercion")

    # --- validate_key
    check(validate_key("abcd1234") == "abcd1234", "valid key")
    check(raises(ToolError, lambda: validate_key("short")), "short key refused")
    check(raises(ToolError, lambda: validate_key("abcd 12345")), "space in key refused")
    check(raises(ToolError, lambda: validate_key(None)), "non-str key refused")

    # --- build_indexnow
    url, headers, body = build_indexnow("https://Example.com/", "abcd1234",
                                        ["https://example.com/a", "https://example.com/a",
                                         "https://example.com/b"])
    check(url == INDEXNOW_URL and headers["Content-Type"].startswith("application/json"), "indexnow endpoint")
    check(body["host"] == "example.com" and body["urlList"] == ["https://example.com/a",
                                                                "https://example.com/b"], "dedupe + host")
    check("keyLocation" not in body, "no keyLocation by default")
    check(raises(ToolError, lambda: build_indexnow("example.com", "abcd1234",
                                                   ["https://example.com/ok", "http://evil.com/x"])),
          "http/foreign url refused")
    check(raises(ToolError, lambda: build_indexnow("example.com", "abcd1234", [])), "empty urls refused")
    check(raises(ToolError, lambda: build_indexnow("example.com", "abcd1234",
                                                   [f"https://example.com/{i}" for i in range(MAX_URLS + 1)])),
          "too many urls refused")
    check(raises(ToolError, lambda: build_indexnow("example.com", "abcd1234",
                                                   ["https://example.com/a"], key_location="http://x/k.txt")),
          "non-https keyLocation refused")
    _u, _h, kl_body = build_indexnow("example.com", "abcd1234", ["https://example.com/a"],
                                     key_location="https://example.com/abcd1234.txt")
    check(kl_body["keyLocation"] == "https://example.com/abcd1234.txt", "keyLocation kept")

    # --- submit_indexnow happy path (mocked http)
    calls = []

    def http_ok(method, req_url, req_headers, payload):
        calls.append((method, req_url, req_headers, payload))
        return 202, "ignored"

    res = submit_indexnow("example.com", "abcd1234",
                          ["https://example.com/a", "https://example.com/a"], http_ok)
    check(res == {"ok": True, "status": 202}, "submit 202 -> ok")
    sent = json.loads(calls[0][3].decode("utf-8"))
    check(calls[0][0] == "POST" and calls[0][1] == INDEXNOW_URL, "submit request shape")
    check(sent["urlList"] == ["https://example.com/a"], "submit payload deduped")

    def http_status(code):
        return lambda method, req_url, hdrs, payload: (code, "")

    res = submit_indexnow("example.com", "abcd1234", ["https://example.com/a"], http_status(403))
    check(res["ok"] is False and res["meaning"] == "key not valid", "403 meaning")
    res = submit_indexnow("example.com", "abcd1234", ["https://example.com/a"], http_status(429))
    check(res["meaning"] == "too many requests", "429 meaning")
    res = submit_indexnow("example.com", "abcd1234", ["https://example.com/a"], http_status(418))
    check(res["meaning"] == "unexpected status", "unknown status meaning")
    # Status codes arrive as ints, but numeric strings are tolerated defensively.
    res = submit_indexnow("example.com", "abcd1234", ["https://example.com/a"], http_status("200"))
    check(res == {"ok": True, "status": 200}, "numeric-string status coerced")

    many = [f"https://example.com/{i}" for i in range(MAX_URLS + 1)]
    res = submit_indexnow("example.com", "abcd1234", many, http_ok)
    check(res["ok"] is True and len(res["batches"]) == 2, "batching of 10001 urls")

    def http_boom(method, req_url, hdrs, payload):
        raise OSError("connection reset token=SEKRET")

    check(raises(ToolError, lambda: submit_indexnow("example.com", "abcd1234",
                                                    ["https://example.com/a"], http_boom)),
          "transport error -> ToolError")
    check(raises(ToolError, lambda: submit_indexnow("example.com", "abcd1234",
                                                    ["https://example.com/a"], http_status("not-a-status"))),
          "non-integer status refused")
    check(raises(RailViolation, lambda: submit_indexnow(
        "example.com", "abcd1234",
        [f"https://example.com/{i}" for i in range(10 * MAX_URLS + 1)], http_ok)),
        "mass submit refused")

    # --- check_key_file
    seen_urls = []

    def fetch_ok(u):
        seen_urls.append(u)
        return 200, " abcd1234\n"

    res = check_key_file("example.com", "abcd1234", fetch_ok)
    check(res["ok"] is True and seen_urls[0] == "https://example.com/abcd1234.txt", "key file ok")
    check(check_key_file("example.com", "abcd1234", lambda u: (200, "wrong"))["ok"] is False,
          "key file mismatch")
    check(check_key_file("example.com", "abcd1234", lambda u: (404, ""))["ok"] is False,
          "key file 404")

    # --- build_bing_submit
    bing = build_bing_submit("https://example.com", ["https://example.com/a",
                                                     "https://www.example.com/b"])
    check(bing["url"].endswith("?apikey={BING_API_KEY}") and "key" not in bing["body"], "bing placeholder only")
    check(bing["method"] == "POST" and bing["body"]["siteUrl"] == "https://example.com", "bing body")
    check("abcd1234" not in json.dumps(bing), "no key value leaked")
    bing2 = build_bing_submit("https://example.com", ["https://example.com/a"], api_key_name="MY_KEY")
    check("{MY_KEY}" in bing2["url"], "custom key name placeholder")
    check(raises(ToolError, lambda: build_bing_submit("https://example.com", ["https://evil.com/x"])),
          "bing foreign url refused")
    check(raises(ToolError, lambda: build_bing_submit("http://example.com", ["https://example.com/a"])),
          "bing non-https site refused")

    # --- changed_urls
    urlset = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/new</loc><lastmod>2024-06-01</lastmod></url>
      <url><loc>https://example.com/old</loc><lastmod>2024-01-01</lastmod></url>
      <url><loc>https://example.com/unknown</loc></url>
    </urlset>"""
    res = changed_urls(urlset, "2024-05-01T00:00:00Z")
    check(res == ["https://example.com/new"], "urlset filtering excludes missing lastmod")

    idx = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sm-new.xml</loc><lastmod>2024-07-01T10:00:00Z</lastmod></sitemap>
      <sitemap><loc>https://example.com/sm-old.xml</loc><lastmod>2023-01-01</lastmod></sitemap>
    </sitemapindex>"""
    res = changed_urls(idx, "2024-05-01")
    check(res == {"indexes": ["https://example.com/sm-new.xml"]}, "sitemap index")
    check(changed_urls("", "2024-01-01") == [], "empty sitemap")
    check(raises(ToolError, lambda: changed_urls("<not-xml", "2024-01-01")), "bad xml refused")
    check(raises(ToolError, lambda: changed_urls(urlset, "not-a-date")), "bad since refused")
    check(raises(ToolError, lambda: changed_urls("<rss/>", "2024-01-01")), "unsupported root refused")

    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)