"""Performance stage of the AdRails site audit (adsops/seo): one Lighthouse run per URL template, then findings.

Picks one page per template (`pick_pages`), runs Lighthouse on it — a local CLI via subprocess when one is configured
or on PATH, otherwise the PageSpeed Insights API with the user's own key, otherwise the stage is skipped with a note
(`choose_backend`, `run_cli`, `run_psi`) — and turns the Lighthouse result (LHR JSON) into the perf_opportunity /
lcp_slow / lcp_preload_mismatch findings of the issue catalogue (`analyze_lhr`, `findings`).

Read-only and pure: no network, disk or clock access at import time, no global mutable state. The process runner and
the http callable are injected, every URL goes through adsops.seo.fetcher.check_url before anything is spent, and
everything returned is JSON-serialisable (dicts/lists/str/int/float/None). Nothing here raises on hostile data —
a malformed LHR, missing audits, garbage page dicts or a crawl without pages simply produce empty results — and the
few ToolError messages carry no API key and no request URL.
"""
import json
import os
import re
import shutil
import subprocess
import urllib.parse

from adsops.errors import ToolError, scrub
from adsops.seo import fetcher
from adsops.seo.extract import normalize, template_of

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
NO_BACKEND_NOTE = ("Lighthouse skipped: no lighthouse CLI on PATH (npm i -g lighthouse) and no PageSpeed API key "
                   "(cfg seo.pagespeed_key or PAGESPEED_API_KEY)")
CLI_FLAGS = ("--output=json", "--output-path=stdout", "--quiet", "--only-categories=performance")
CHECKS = ("perf_opportunity", "lcp_slow", "lcp_preload_mismatch")
LCP_AUDITS = ("largest-contentful-paint-element", "lcp-discovery-insight", "lcp-breakdown-insight")
METRIC_AUDITS = (("lcp_ms", "largest-contentful-paint"), ("fcp_ms", "first-contentful-paint"),
                 ("tbt_ms", "total-blocking-time"), ("si_ms", "speed-index"))
MAX_STDOUT_BYTES = 40 * 1024 * 1024
MAX_STR = 500
MAX_LIST = 20
MAX_URLS = 500
MAX_NODE_DEPTH = 6
LCP_BUDGET_MS = 2500
OPP_MIN_MS = 100
OPP_MIN_BYTES = 20480
_SRC_RX = re.compile(r"""(?<![\w:-])src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", re.I)
_POSTER_RX = re.compile(r"""(?<![\w:-])poster\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", re.I)
_SRCSET_RX = re.compile(r"""(?<![\w:-])srcset\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_CSS_URL_RX = re.compile(r"""url\(\s*["']?([^"')]+?)["']?\s*\)""", re.I)


# ---------------------------------------------------------------------------------------------------------------
# small, total helpers (never raise, never touch the outside world)
# ---------------------------------------------------------------------------------------------------------------
def _text(value, limit=MAX_STR):
    """Whitespace-collapsed string of `value` ('' for None/garbage), truncated to `limit` characters."""
    if value is None:
        return ""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:                                            # noqa: BLE001 - hostile __str__
        return ""
    s = " ".join(s.split())
    return s[:limit] if limit else s


def _list(value):
    """`value` if it is a list/tuple, else an empty list."""
    return value if isinstance(value, (list, tuple)) else []


def _num(value):
    """float(value) when `value` is a real number (never a bool, NaN or inf), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _int(value):
    """Rounded int of a real number, else None."""
    v = _num(value)
    return None if v is None else int(round(v))


def _decode(value):
    """Text of a subprocess stream (bytes/str/anything), never raising."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:                                        # noqa: BLE001 - hostile bytes
            return ""
    if isinstance(value, str):
        return value
    return "" if value is None else _text(value, MAX_STDOUT_BYTES)


def _basename(url):
    """Last path segment of `url` ('hero.webp'), or the whole string when it has no path."""
    try:
        path = urllib.parse.urlsplit(_text(url, 2000)).path or ""
    except ValueError:
        return _text(url, 200)
    segs = [s for s in path.split("/") if s]
    return segs[-1] if segs else (_text(url, 200) or "an image")


def _strip_query(url):
    """`url` without fragment and query string."""
    return _text(url, 2000).split("#", 1)[0].split("?", 1)[0]


def _page_word(url):
    """How a finding talks about a page: 'home' for the home page, the template otherwise ('/faq/')."""
    try:
        tpl = template_of(_text(url, 2000))
    except ToolError:
        tpl = "home"
    return "home" if tpl == "home" else (tpl or "home")


# ---------------------------------------------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------------------------------------------
def choose_backend(cfg=None, which=shutil.which, env=os.environ):
    """-> {'kind': 'cli', 'cmd': [...]} | {'kind': 'psi', 'key': ...} | {'kind': 'none', 'note': ...}.

    The configured command wins over PATH, and a PageSpeed key is only used when no CLI exists. The key itself is
    never echoed into a note.
    """
    seo = (cfg if isinstance(cfg, dict) else {}).get("seo")
    seo = seo if isinstance(seo, dict) else {}
    cmd = seo.get("lighthouse_cmd")
    if isinstance(cmd, (list, tuple)) and len(cmd) > 0 and all(isinstance(c, str) and c for c in cmd):
        return {"kind": "cli", "cmd": list(cmd)}
    try:
        found = which("lighthouse")
    except Exception:                                            # noqa: BLE001 - hostile which()
        found = None
    if isinstance(found, str) and found.strip():
        return {"kind": "cli", "cmd": [found.strip()]}
    env_key = ""
    if env is not None and hasattr(env, "get"):
        try:
            env_key = env.get("PAGESPEED_API_KEY") or ""
        except Exception:                                        # noqa: BLE001 - hostile mapping
            env_key = ""
    key = seo.get("pagespeed_key") or env_key
    if isinstance(key, str) and key.strip():
        return {"kind": "psi", "key": key.strip()}
    return {"kind": "none", "note": NO_BACKEND_NOTE}


# ---------------------------------------------------------------------------------------------------------------
# page selection
# ---------------------------------------------------------------------------------------------------------------
def _depth_key(page):
    """Sort key putting the shallowest page first and unreachable (None depth) pages last."""
    d = _int(page.get("depth"))
    if d is None or d < 0:
        return (1, 0)
    return (0, d)


def pick_pages(crawl, per_template=1, max_pages=10):
    """-> list of URLs to measure: one page per template, shallowest first, home template first, capped."""
    crawl = crawl if isinstance(crawl, dict) else {}
    pages = crawl.get("pages")
    pages = pages if isinstance(pages, dict) else {}
    per = _int(per_template)
    per = 1 if per is None or per < 0 else per
    cap = _int(max_pages)
    cap = 10 if cap is None else max(0, cap)
    if cap == 0 or per == 0:
        return []
    by_template = {}
    for url, page in pages.items():
        if not isinstance(url, str) or not url or not isinstance(page, dict):
            continue
        if _int(page.get("status")) != 200:
            continue
        final = _text(page.get("final"), 2000) or url
        if final != url:
            continue
        if not isinstance(page.get("doc"), dict):
            continue
        ctype = _text(page.get("ctype"), 100).lower()
        if ctype and "html" not in ctype:
            continue
        try:
            tpl = template_of(url)
        except ToolError:
            continue
        if not tpl or tpl == "asset":
            continue
        by_template.setdefault(tpl, []).append((_depth_key(page), url))
    ordered = []
    for tpl in sorted(by_template, key=lambda t: (t != "home", t)):
        for _, url in sorted(by_template[tpl])[:per]:
            ordered.append(url)
        if len(ordered) >= cap:
            break
    return ordered[:cap]


# ---------------------------------------------------------------------------------------------------------------
# running Lighthouse
# ---------------------------------------------------------------------------------------------------------------
def run_cli(url, cmd, runner=subprocess.run, timeout=240, chrome_path=None):
    """-> LHR dict from a local Lighthouse CLI run (never shell=True, output captured, no secrets in messages)."""
    _scheme, host, _port, _path, ip = fetcher.check_url(url)    # ToolError for non-public/unsafe URLs, before any work
    base = [c for c in _list(cmd) if isinstance(c, str) and c]
    if not base:
        raise ToolError("Lighthouse command is empty: set cfg seo.lighthouse_cmd")
    # Chrome resolves the audited host to the address we checked (no DNS rebinding for the page itself).
    # Residual (documented): redirects and subresources to OTHER hosts are resolved by Chrome; the result is
    # dropped below if the final URL is not public.
    argv = list(base) + [url] + list(CLI_FLAGS) + ["--chrome-flags=--headless=new --host-resolver-rules=MAP %s %s"
                                                   % (host, ip)]
    env = dict(os.environ)
    if chrome_path:
        env["CHROME_PATH"] = str(chrome_path)
    try:
        proc = runner(argv, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ToolError("Lighthouse timed out on " + url) from None
    except (OSError, ValueError) as exc:
        raise ToolError("Lighthouse could not start on %s: %s" % (url, scrub(type(exc).__name__))) from None
    if proc is None:
        raise ToolError("Lighthouse returned no report")
    returncode = _int(getattr(proc, "returncode", 1))
    if returncode is None or returncode != 0:
        tail = scrub(_decode(getattr(proc, "stderr", ""))[-300:])
        raise ToolError("Lighthouse failed on %s: %s" % (url, tail or "exit code %s" % returncode))
    raw = getattr(proc, "stdout", "")
    text = _decode(raw)
    size = len(raw) if isinstance(raw, bytes) else len(text.encode("utf-8", "ignore"))
    if size > MAX_STDOUT_BYTES:
        raise ToolError("Lighthouse report is too large (%d bytes > %d)" % (size, MAX_STDOUT_BYTES))
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ToolError("Lighthouse returned no report") from None
    if not isinstance(data, dict) or not isinstance(data.get("audits"), dict):
        raise ToolError("Lighthouse returned no report")
    _final_public(data)
    return data


def _final_public(lhr):
    """Drop a report whose page ended up on a non-public address (a redirect into a private network)."""
    final = lhr.get("finalDisplayedUrl") or lhr.get("finalUrl") or ""
    if final and isinstance(final, str):
        fetcher.check_url(final)          # raises ToolError for private/loopback/odd-port final URLs


def run_psi(url, key, http, strategy="mobile"):
    """-> LHR dict from the PageSpeed Insights API; `http(method, url, headers, body) -> (status, bytes)`."""
    fetcher.check_url(url)
    if not isinstance(key, str) or not key.strip():
        raise ToolError("PageSpeed API key is missing")
    strat = _text(strategy, 20).lower() or "mobile"
    if strat not in ("mobile", "desktop"):
        raise ToolError("PageSpeed strategy must be mobile or desktop")
    target = PSI_ENDPOINT + "?url=" + urllib.parse.quote(url, safe="") + "&strategy=" + strat + "&category=performance"
    headers = {"Accept": "application/json", "X-Goog-Api-Key": key.strip()}     # key in a header, never in the URL
    try:
        status, body = http("GET", target, headers, None)
    except ToolError:
        raise ToolError("PageSpeed API request failed") from None   # the transport's text could carry the request
    except Exception as exc:                                     # noqa: BLE001 - transport failures never echo the URL
        raise ToolError("PageSpeed API unreachable: " + type(exc).__name__) from None
    code = _int(status)
    if code != 200:
        raise ToolError("PageSpeed API HTTP %s" % (code if code is not None else "error"))
    try:
        data = json.loads(_decode(body))
    except (ValueError, TypeError):
        raise ToolError("PageSpeed API returned no report") from None
    if not isinstance(data, dict) or not isinstance(data.get("lighthouseResult"), dict):
        raise ToolError("PageSpeed API returned no lighthouseResult")
    return data["lighthouseResult"]


# ---------------------------------------------------------------------------------------------------------------
# reading the LHR
# ---------------------------------------------------------------------------------------------------------------
def _attr(rx, text):
    """First capture group of `rx` in `text` ('' when absent)."""
    if not isinstance(text, str) or not text:
        return ""
    m = rx.search(text)
    if not m:
        return ""
    for group in m.groups():
        if group:
            return group.strip()
    return ""


def _src_from_snippet(snippet):
    """-> (src, attribute) of the first usable reference in a node snippet; ('', '') when there is none."""
    snippet = _text(snippet, MAX_STR)
    if not snippet:
        return "", ""
    for rx, name in ((_SRC_RX, "src"), (_POSTER_RX, "poster")):
        value = _attr(rx, snippet)
        if value:
            return value, name
    srcset = _attr(_SRCSET_RX, snippet)
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0].strip()
        if first:
            return first, "srcset"
    css = _attr(_CSS_URL_RX, snippet)
    if css:
        return css, "url()"
    return "", ""


def _find_node(obj, depth=0):
    """First dict with type == 'node' and a snippet/selector, walking dicts/lists up to MAX_NODE_DEPTH."""
    if depth > MAX_NODE_DEPTH:
        return None
    if isinstance(obj, dict):
        if obj.get("type") == "node" and (obj.get("snippet") or obj.get("selector")):
            return obj
        for value in obj.values():
            found = _find_node(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found = _find_node(value, depth + 1)
            if found:
                return found
    return None


def _lcp_node(audits):
    """The LCP node dict from the LCP audits, or None."""
    for audit_id in LCP_AUDITS:
        audit = audits.get(audit_id)
        if isinstance(audit, dict):
            node = _find_node(audit.get("details"))
            if node:
                return node
    return None


def analyze_lhr(lhr, doc=None):
    """-> compact, JSON-safe performance summary of one Lighthouse result (never raises, missing data is None)."""
    report = lhr if isinstance(lhr, dict) else {}
    page_doc = doc if isinstance(doc, dict) else {}
    url = _text(report.get("finalDisplayedUrl") or report.get("finalUrl") or report.get("requestedUrl"), 2000)
    categories = report.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    perf = categories.get("performance")
    perf = perf if isinstance(perf, dict) else {}
    score = _num(perf.get("score"))
    score = None if score is None else max(0, min(100, int(round(score * 100))))
    audits = report.get("audits")
    audits = audits if isinstance(audits, dict) else {}
    metrics = {}
    for key, audit_id in METRIC_AUDITS:
        audit = audits.get(audit_id)
        metrics[key] = _int(audit.get("numericValue")) if isinstance(audit, dict) else None
    cls_audit = audits.get("cumulative-layout-shift")
    cls = _num(cls_audit.get("numericValue")) if isinstance(cls_audit, dict) else None
    metrics["cls"] = None if cls is None else round(cls, 3)
    opportunities = []
    for audit_id, audit in audits.items():
        if not isinstance(audit, dict):
            continue
        details = audit.get("details")
        if not isinstance(details, dict) or details.get("type") != "opportunity":
            continue
        savings_ms = _int(details.get("overallSavingsMs"))
        if savings_ms is None:
            metric_savings = audit.get("metricSavings")
            if isinstance(metric_savings, dict):
                savings_ms = _int(metric_savings.get("LCP"))
        savings_ms = 0 if savings_ms is None else max(0, savings_ms)
        savings_bytes = _int(details.get("overallSavingsBytes"))
        savings_bytes = 0 if savings_bytes is None else max(0, savings_bytes)
        if savings_ms < OPP_MIN_MS and savings_bytes < OPP_MIN_BYTES:
            continue
        opportunities.append({"id": _text(audit_id, 80),
                              "title": _text(audit.get("title"), 200) or _text(audit_id, 80),
                              "savings_ms": savings_ms, "savings_bytes": savings_bytes})
    opportunities.sort(key=lambda o: (-o["savings_ms"], -o["savings_bytes"], o["id"]))
    opportunities = opportunities[:MAX_LIST]
    node = _lcp_node(audits)
    lcp_element = None
    lcp_src = ""
    if node:
        snippet = _text(node.get("snippet"), MAX_STR)
        raw_src, _ = _src_from_snippet(snippet)
        lcp_src = (normalize(raw_src, url) or raw_src) if raw_src else ""
        lcp_element = {"selector": _text(node.get("selector"), MAX_STR), "snippet": snippet, "src": lcp_src}
    preloads = []
    for item in _list(page_doc.get("preload_images"))[:MAX_LIST * 5]:
        value = _text(item, 2000)
        if value:
            preloads.append(normalize(value, url) or value)
    preload_mismatch = bool(preloads) and bool(lcp_src)
    if preload_mismatch:
        exact = {p for p in preloads}
        loose = {_strip_query(p) for p in preloads}
        preload_mismatch = lcp_src not in exact and _strip_query(lcp_src) not in loose
    return {"url": url,
            "lighthouse_version": _text(report.get("lighthouseVersion"), 40),
            "score": score,
            "metrics": metrics,
            "opportunities": opportunities,
            "lcp_element": lcp_element,
            "preload_images": preloads,
            "preload_mismatch": preload_mismatch}


# ---------------------------------------------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------------------------------------------
def _plural(count):
    return "" if count == 1 else "s"


def _opportunity_finding(rows):
    """One aggregated perf_opportunity finding (or None when no page has an opportunity)."""
    totals = {}
    pages_with = []
    for row in rows:
        url = _text(row.get("url"), 2000)
        opps = [o for o in _list(row.get("opportunities")) if isinstance(o, dict)]
        if opps and url:
            pages_with.append(url)
        for opp in opps:
            oid = _text(opp.get("id"), 80) or "unnamed"
            slot = totals.setdefault(oid, {"title": _text(opp.get("title"), 200) or oid, "pages": 0, "savings": 0})
            slot["pages"] += 1
            savings = _int(opp.get("savings_ms"))
            slot["savings"] += max(0, savings or 0)
            slot["bytes"] = slot.get("bytes", 0) + max(0, _int(opp.get("savings_bytes")) or 0)
    if not totals or not pages_with:
        return None
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1]["savings"], -kv[1].get("bytes", 0), -kv[1]["pages"], kv[0]))
    top = ranked[0][1]
    gain = ("an estimated %d ms" % top["savings"]) if top["savings"] else ("about %d KB" % (top.get("bytes", 0) // 1024))
    detail = ("Biggest win: '%s' saves %s across %d page%s."
              % (top["title"], gain, top["pages"], _plural(top["pages"])))
    data = {"opportunities": [[oid, slot["title"], slot["pages"], slot["savings"]] for oid, slot in ranked[:MAX_LIST]],
            "pages_with_opportunities": len(pages_with)}
    return {"code": "perf_opportunity", "urls": pages_with[:MAX_URLS], "detail": detail, "data": data}


def _lcp_slow_finding(rows, budget):
    """One lcp_slow finding for every measured page above the budget (or None)."""
    slow = []
    for row in rows:
        url = _text(row.get("url"), 2000)
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        lcp = _int(metrics.get("lcp_ms"))
        if not url or lcp is None or lcp <= budget:
            continue
        element = row.get("lcp_element") if isinstance(row.get("lcp_element"), dict) else {}
        slow.append([url, lcp, _text(element.get("selector"), MAX_STR)])
    if not slow:
        return None
    slow.sort(key=lambda item: (-item[1], item[0]))
    measured = len([r for r in rows if isinstance(r.get("metrics"), dict)])
    detail = ("%d of %d measured page%s have an LCP above %d ms, worst %d ms on %s."
              % (len(slow), measured, _plural(measured), budget, slow[0][1], slow[0][0]))
    data = {"examples": slow[:MAX_LIST], "count": len(slow), "budget_ms": budget}
    return {"code": "lcp_slow", "urls": [item[0] for item in slow][:MAX_URLS], "detail": detail, "data": data}


def _preload_finding(rows):
    """One lcp_preload_mismatch finding for every page preloading something else than its LCP (or None)."""
    mismatched = []
    for row in rows:
        if not row.get("preload_mismatch"):
            continue
        url = _text(row.get("url"), 2000)
        if not url:
            continue
        element = row.get("lcp_element") if isinstance(row.get("lcp_element"), dict) else {}
        src = _text(element.get("src"), 2000)
        selector = _text(element.get("selector"), MAX_STR)
        _, attribute = _src_from_snippet(_text(element.get("snippet"), MAX_STR))
        preloads = [_text(p, 2000) for p in _list(row.get("preload_images")) if _text(p, 2000)]
        mismatched.append((url, src, preloads, selector, attribute))
    if not mismatched:
        return None
    url0, src0, pre0, sel0, attr0 = mismatched[0]
    preloaded = _basename(pre0[0]) if pre0 else "an image"
    loaded = ("(%s%s)" % ((attr0 + " ") if attr0 and attr0 != "src" else "", _basename(src0))) if src0 else ""
    detail = ("the %s page preloads %s but its LCP element is %s%s"
              % (_page_word(url0), preloaded, sel0 or "an image", (" " + loaded) if loaded else ""))
    if len(mismatched) > 1:
        detail += "; %d more page%s do the same" % (len(mismatched) - 1, _plural(len(mismatched) - 1))
    data = {"examples": [[u, s, p] for u, s, p, _sel, _attr in mismatched[:MAX_LIST]],
            "count": len(mismatched)}
    return {"code": "lcp_preload_mismatch", "urls": [m[0] for m in mismatched][:MAX_URLS], "detail": detail,
            "data": data}


def findings(results, lcp_budget_ms=LCP_BUDGET_MS):
    """-> (findings, passed) for a list of analyze_lhr outputs; one finding per code, `passed` names quiet checks."""
    rows = [r for r in _list(results) if isinstance(r, dict)]
    budget = _int(lcp_budget_ms)
    budget = LCP_BUDGET_MS if budget is None else budget
    out = []
    for found in (_opportunity_finding(rows), _lcp_slow_finding(rows, budget), _preload_finding(rows)):
        if found is not None:
            out.append(found)
    fired = {f["code"] for f in out}
    passed = [code for code in CHECKS if code not in fired] if rows else []
    return out, passed


# ---------------------------------------------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------------------------------------------
def selftest() -> int:
    """Offline assertions over a synthetic Lighthouse 13 report, fake runners and fake http callables."""
    n = 0

    def chk(cond, msg):
        nonlocal n
        assert cond, "seo.perf.selftest: " + msg
        n += 1

    class Proc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def runner_of(proc=None, exc=None, calls=None):
        def run(argv, capture_output=True, timeout=None, env=None):
            if calls is not None:
                calls.append({"argv": list(argv), "timeout": timeout, "env": env, "capture": capture_output})
            if exc is not None:
                raise exc
            return proc
        return run

    public = "http://93.184.216.34/"                             # a literal public IP: check_url never touches DNS
    lhr = {"lighthouseVersion": "13.6.0", "finalDisplayedUrl": "https://ex.com/",
           "categories": {"performance": {"score": 0.52}},
           "audits": {
               "largest-contentful-paint": {"numericValue": 5600.4},
               "first-contentful-paint": {"numericValue": 1200},
               "total-blocking-time": {"numericValue": 30},
               "speed-index": {"numericValue": 3000},
               "cumulative-layout-shift": {"numericValue": 0.0},
               "unused-javascript": {"title": "Reduce unused JavaScript",
                                     "details": {"type": "opportunity", "overallSavingsMs": 450,
                                                 "overallSavingsBytes": 90000}},
               "redirects": {"title": "Avoid redirects", "details": {"type": "opportunity", "overallSavingsMs": 0}},
               "lcp-discovery-insight": {"details": {"type": "list", "items": [
                   {"type": "checklist"},
                   {"type": "node", "selector": "main > section.hero > div.hero-bg > video#broll",
                    "snippet": '<video id="broll" preload="none" poster="/assets/hero-broll-poster.webp">'}]}}}}
    doc = {"preload_images": ["https://ex.com/assets/hero-antonio.webp"]}

    # choose_backend -------------------------------------------------------------------------------------------
    backend = choose_backend({}, which=lambda name: None, env={})
    chk(backend["kind"] == "none" and "PAGESPEED_API_KEY" in backend.get("note", ""), "no CLI and no key -> skipped")
    backend = choose_backend(None, which=lambda name: "/usr/local/bin/lighthouse", env={})
    chk(backend == {"kind": "cli", "cmd": ["/usr/local/bin/lighthouse"]}, "a CLI on PATH is used")
    backend = choose_backend({"seo": {"lighthouse_cmd": ["npx", "lighthouse"]}}, which=lambda n: "/x/lighthouse", env={})
    chk(backend.get("cmd") == ["npx", "lighthouse"], "cfg seo.lighthouse_cmd wins over PATH")
    backend = choose_backend({"seo": {"lighthouse_cmd": []}}, which=lambda n: None, env={"PAGESPEED_API_KEY": "k-123"})
    chk(backend["kind"] == "psi" and backend["key"] == "k-123", "an empty cmd list falls through to the env key")
    backend = choose_backend({"seo": {"pagespeed_key": "cfg-key-9"}}, which=lambda n: None, env={})
    chk(backend["kind"] == "psi" and backend["key"] == "cfg-key-9", "cfg seo.pagespeed_key selects PSI")
    backend = choose_backend({"seo": {"pagespeed_key": ""}}, which=lambda n: None,
                             env={"PAGESPEED_API_KEY": "secret-env-key"})
    chk(backend["kind"] == "psi" and "secret-env-key" not in json.dumps(backend.get("note") or ""),
        "the env key is used and never appears in a note")
    chk(choose_backend({"seo": {"lighthouse_cmd": "lighthouse"}}, which=lambda n: None, env={})["kind"] == "none",
        "a non-list lighthouse_cmd is ignored")
    chk(choose_backend({"seo": "garbage"}, which=lambda n: None, env={})["kind"] == "none",
        "a non-dict seo block does not crash")
    chk(choose_backend({"seo": {"pagespeed_key": "  "}}, which=lambda n: None, env={})["kind"] == "none",
        "a blank key does not count as a backend")

    # pick_pages -----------------------------------------------------------------------------------------------
    def page(status=200, final=None, has_doc=True, ctype="text/html", depth=0):
        return {"status": status, "final": final, "ctype": ctype, "depth": depth,
                "doc": {"preload_images": []} if has_doc else None}

    crawl = {"pages": {
        "https://ex.com/": page(depth=0),
        "https://ex.com/faq/": page(depth=1),
        "https://ex.com/locations/a/": page(depth=3),
        "https://ex.com/locations/b/": page(depth=1),
        "https://ex.com/locations/c/": page(depth=1),
        "https://ex.com/old/": page(final="https://ex.com/new/"),
        "https://ex.com/gone/": page(status=404),
        "https://ex.com/nodoc/": page(has_doc=False),
        "https://ex.com/plain/": page(ctype="text/plain"),
        "https://ex.com/file.pdf": page(depth=1)}}
    picked = pick_pages(crawl)
    chk(picked[0] == "https://ex.com/", "the home template is measured first")
    chk("https://ex.com/locations/b/" in picked and "https://ex.com/locations/c/" not in picked,
        "per template the shallowest page wins, ties broken by url")
    chk("https://ex.com/old/" not in picked and "https://ex.com/gone/" not in picked, "redirects and errors are skipped")
    chk("https://ex.com/nodoc/" not in picked and "https://ex.com/plain/" not in picked, "non-HTML pages are skipped")
    chk("https://ex.com/file.pdf" not in picked, "asset templates are skipped")
    chk(len(pick_pages(crawl, max_pages=2)) == 2, "max_pages caps the list")
    chk(len(pick_pages(crawl, per_template=2)) > len(picked), "per_template=2 takes more pages")
    chk(pick_pages({}) == [] and pick_pages(None) == [], "an empty or missing pages map yields nothing")
    chk(pick_pages({"pages": {"https://ex.com/x/": None, "https://ex.com/y/": 7}}) == [],
        "garbage page entries are ignored")

    # run_cli --------------------------------------------------------------------------------------------------
    calls = []
    got = run_cli(public, ["lighthouse"], runner=runner_of(Proc(0, json.dumps(lhr), ""), calls=calls),
                  timeout=7, chrome_path="/bin/chrome")
    chk(got.get("lighthouseVersion") == "13.6.0", "run_cli returns the parsed LHR")
    chk(calls and calls[0]["argv"][:2] == ["lighthouse", public], "run_cli passes the cmd then the url")
    chk("--only-categories=performance" in calls[0]["argv"] and any(a.startswith("--chrome-flags=--headless=new --host-resolver-rules=MAP ") for a in calls[0]["argv"]),
        "run_cli adds the performance flags")
    chk(calls[0]["timeout"] == 7 and calls[0]["env"].get("CHROME_PATH") == "/bin/chrome",
        "run_cli forwards the timeout and CHROME_PATH")
    chk(calls[0]["capture"] is True, "run_cli captures output (never shells out)")
    try:
        run_cli(public, ["lighthouse"], runner=runner_of(Proc(2, "", b"Fatal error: Chrome crashed")))
        chk(False, "a non-zero exit should raise")
    except ToolError as exc:
        chk("failed" in str(exc) and "Chrome crashed" in str(exc), "a non-zero exit names the stderr tail")
    try:
        run_cli(public, ["lighthouse"], runner=runner_of(exc=subprocess.TimeoutExpired("lighthouse", 240)))
        chk(False, "a timeout should raise")
    except ToolError as exc:
        chk("timed out" in str(exc) and public in str(exc), "a timeout is reported with the url")
    private_calls = []
    try:
        run_cli("http://127.0.0.1/", ["lighthouse"], runner=runner_of(Proc(0, json.dumps(lhr)), calls=private_calls))
        chk(False, "a private url should be refused")
    except ToolError:
        chk(private_calls == [], "a non-public url is refused before the CLI runs")
    for payload, label in (("not json", "unparsable stdout"), ('{"foo": 1}', "an LHR without audits")):
        try:
            run_cli(public, ["lighthouse"], runner=runner_of(Proc(0, payload, "")))
            chk(False, "%s should raise" % label)
        except ToolError as exc:
            chk("no report" in str(exc), "%s is reported as no report" % label)
    try:
        run_cli(public, ["lighthouse"], runner=runner_of(Proc(0, "x" * (MAX_STDOUT_BYTES + 1), "")))
        chk(False, "oversized stdout should raise")
    except ToolError as exc:
        chk("too large" in str(exc).lower(), "oversized stdout is rejected")

    # run_psi --------------------------------------------------------------------------------------------------
    seen = []

    def http_of(status, payload):
        def call(method, url, headers, body):
            seen.append({"method": method, "url": url, "headers": headers, "body": body})
            return status, payload
        return call

    got = run_psi(public, "KEY-abc-123", http_of(200, json.dumps({"lighthouseResult": lhr}).encode()))
    chk(got.get("lighthouseVersion") == "13.6.0", "run_psi returns the lighthouseResult")
    chk(seen[0]["method"] == "GET" and "runPagespeed" in seen[0]["url"] and "strategy=mobile" in seen[0]["url"],
        "run_psi calls the v5 endpoint with the mobile strategy")
    chk("KEY-abc-123" not in seen[0]["url"] and "93.184.216.34" in seen[0]["url"]
        and seen[0]["headers"].get("X-Goog-Api-Key") == "KEY-abc-123", "the key travels in a header, never in the URL")
    try:
        run_psi(public, "KEY-abc-123", http_of(403, b'{"error": {"code": 403}}'))
        chk(False, "a 403 should raise")
    except ToolError as exc:
        msg = str(exc)
        chk("403" in msg and "KEY-abc-123" not in msg and "googleapis" not in msg,
            "a PSI failure leaks neither the key nor the request URL")
    try:
        run_psi(public, "KEY-abc-123", http_of(200, b'{"error": "nope"}'))
        chk(False, "a body without lighthouseResult should raise")
    except ToolError as exc:
        chk("lighthouseResult" in str(exc), "a PSI body without a report is rejected")
    try:
        run_psi("http://10.0.0.1/", "KEY-abc-123", http_of(200, b"{}"))
        chk(False, "a private url should be refused")
    except ToolError:
        chk(True, "run_psi refuses a non-public url before calling the API")

    # analyze_lhr ----------------------------------------------------------------------------------------------
    result = analyze_lhr(lhr, doc)
    chk(result["url"] == "https://ex.com/", "analyze uses finalDisplayedUrl")
    chk(result["score"] == 52 and result["lighthouse_version"] == "13.6.0", "score is a percentage and the version is kept")
    chk(result["metrics"]["lcp_ms"] == 5600 and result["metrics"]["fcp_ms"] == 1200, "LCP and FCP are rounded ints")
    chk(result["metrics"]["tbt_ms"] == 30 and result["metrics"]["si_ms"] == 3000, "TBT and SI are read")
    chk(result["metrics"]["cls"] == 0.0 and isinstance(result["metrics"]["cls"], float), "CLS keeps three decimals")
    chk(len(result["opportunities"]) == 1 and result["opportunities"][0]["id"] == "unused-javascript",
        "only opportunities over the thresholds are kept")
    chk(result["opportunities"][0]["savings_ms"] == 450 and result["opportunities"][0]["savings_bytes"] == 90000,
        "opportunity savings are read from details")
    chk(result["lcp_element"]["selector"] == "main > section.hero > div.hero-bg > video#broll",
        "the LCP node selector is found through nested details")
    chk(result["lcp_element"]["src"] == "https://ex.com/assets/hero-broll-poster.webp", "the poster is resolved absolute")
    chk(result["preload_mismatch"] is True, "a preloaded non-LCP image is a mismatch")
    chk(result["preload_images"] == ["https://ex.com/assets/hero-antonio.webp"], "preloads come from the page document")
    ok = analyze_lhr(lhr, {"preload_images": ["https://ex.com/assets/hero-broll-poster.webp?cb=1"]})
    chk(ok["preload_mismatch"] is False, "query strings are ignored when matching preloads")
    chk(analyze_lhr(lhr, None)["preload_mismatch"] is False, "no document means no preload mismatch")
    alt = {"finalUrl": "https://ex.com/a/", "categories": {}, "audits": {"render-blocking": {
        "title": "Eliminate render-blocking requests", "metricSavings": {"LCP": 350},
        "details": {"type": "opportunity"}}}}
    alt_result = analyze_lhr(alt)
    chk(alt_result["opportunities"] and alt_result["opportunities"][0]["savings_ms"] == 350,
        "metricSavings.LCP is used when overallSavingsMs is missing")
    chk(alt_result["score"] is None and alt_result["lcp_element"] is None, "missing categories and audits give None")
    chk(analyze_lhr({})["metrics"]["lcp_ms"] is None and analyze_lhr(None)["url"] == "", "empty or non-dict LHRs do not raise")
    chk(analyze_lhr({"audits": [], "categories": {"performance": {"score": "x"}}})["metrics"]["lcp_ms"] is None,
        "hostile audit containers are tolerated")
    chk(analyze_lhr({"audits": {"largest-contentful-paint": {"numericValue": "loads"}}})["metrics"]["lcp_ms"] is None,
        "a non-numeric metric is None")
    deep = {"type": "list"}
    cursor = deep
    for _ in range(10):
        cursor["items"] = [{"nested": {}}]
        cursor = cursor["items"][0]["nested"]
    cursor["type"] = "node"
    cursor["snippet"] = '<img src="/x.webp">'
    chk(analyze_lhr({"finalUrl": "https://ex.com/", "audits": {"largest-contentful-paint-element": {
        "details": deep}}})["lcp_element"] is None, "nodes deeper than the walk limit are ignored")
    css_node = analyze_lhr({"finalUrl": "https://ex.com/", "audits": {"lcp-breakdown-insight": {"details": {
        "items": [{"type": "node", "selector": "div.hero", "snippet": "background: url('/img/hero.webp')"}]}}}})
    chk(css_node["lcp_element"]["src"] == "https://ex.com/img/hero.webp", "a CSS url() snippet is resolved")

    # findings -------------------------------------------------------------------------------------------------
    found, passed = findings([result])
    chk(sorted(f["code"] for f in found) == ["lcp_preload_mismatch", "lcp_slow", "perf_opportunity"],
        "the synthetic report yields lcp_slow, lcp_preload_mismatch and perf_opportunity")
    chk(passed == [], "nothing passes when all three checks fire")
    chk(all(f["urls"] and f["detail"] and isinstance(f["data"], dict) for f in found),
        "every finding carries urls, a detail and a data dict")
    opp = [f for f in found if f["code"] == "perf_opportunity"][0]
    chk([row[0] for row in opp["data"]["opportunities"]] == ["unused-javascript"], "the opportunity list is ranked")
    chk("Reduce unused JavaScript" in opp["detail"] and "450" in opp["detail"], "the detail names the top win")
    pre = [f for f in found if f["code"] == "lcp_preload_mismatch"][0]
    chk(pre["detail"].startswith("the home page preloads hero-antonio.webp"), "the preload detail says what is preloaded")
    chk("video#broll" in pre["detail"] and "poster hero-broll-poster.webp" in pre["detail"],
        "the preload detail names the real LCP element")
    chk(pre["data"]["examples"][0][1] == "https://ex.com/assets/hero-broll-poster.webp",
        "the mismatch example carries the LCP src")
    slow = [f for f in found if f["code"] == "lcp_slow"][0]
    chk(slow["data"]["examples"][0][:2] == ["https://ex.com/", 5600], "the slow-LCP example carries the measurement")
    chk(json.dumps(found) and json.dumps(result), "findings and analyses are JSON-serialisable")
    quiet, quiet_passed = findings([{"url": "https://ex.com/a/", "metrics": {"lcp_ms": 900}, "opportunities": []}])
    chk(quiet == [] and quiet_passed == ["perf_opportunity", "lcp_slow", "lcp_preload_mismatch"],
        "a fast page with no opportunities passes every check")
    chk(findings([]) == ([], []), "no results means nothing ran and nothing was reported")
    chk(findings([None, {}, "x"]) == ([], ["perf_opportunity", "lcp_slow", "lcp_preload_mismatch"]),
        "garbage results never crash the findings step")
    chk(findings([result], lcp_budget_ms=9000)[1] == ["lcp_slow"], "the LCP budget is configurable")
    many = [{"url": "https://ex.com/%d/" % i, "metrics": {"lcp_ms": 4000},
             "opportunities": [{"id": "unused-css", "title": "Reduce unused CSS", "savings_ms": 200}]} for i in range(40)]
    many_found, _ = findings(many)
    chk(all(len(f["data"]["examples"][0]) if "examples" in f["data"] else True for f in many_found),
        "aggregated findings keep a uniform example shape")
    chk(len([f for f in many_found if f["code"] == "lcp_slow"][0]["data"]["examples"]) == MAX_LIST,
        "example lists are capped at 20")
    chk([f for f in many_found if f["code"] == "perf_opportunity"][0]["data"]["opportunities"][0][2] == 40,
        "opportunity aggregation counts every page")
    return n
