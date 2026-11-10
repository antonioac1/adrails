"""Optional JavaScript render parity for the AdRails site audit (adsops/seo).

Loads a few already-crawled pages in headless Chromium (Playwright, an OPTIONAL dependency) and compares what the raw
HTML response contains with what the browser builds after JavaScript runs: links that only exist after rendering
(js_only_links) and text that is missing from the HTML (js_content_gap), per SPEC-B. Read-only and pure towards the
site: every request the browser makes passes `request_allowed()`, which reuses the fetcher's SSRF checks, and the
only script executed is one fixed expression that reads links/words/title/h1 out of the rendered DOM.

Stdlib only at import time: Playwright is imported inside `available()` and `render_pages()` and never at module
import, so the rest of the audit works without it. No network, disk or clock access at import time; all I/O is
injected (the Playwright factory and the `allow` callable). Everything returned is JSON-serialisable
(dicts/lists/str/int/None, no datetime/sets) and nothing crashes on hostile input.
"""
import urllib.parse

from adsops.errors import ToolError
from adsops.seo import extract, fetcher

MAX_STR = 500
MAX_LINKS = 2000
MAX_URLS = 2000
MAX_LIST = 20
MAX_URL_LEN = 4096
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_PAGES = 10
MAX_TIMEOUT_MS = 60000
MAX_RENDER_PAGES = 20
_VIEWPORT = {"width": 412, "height": 823}
_ALLOW_CACHE = {}
NOT_AVAILABLE = ("JS render parity skipped: Playwright is not installed "
                 "(pip install playwright && playwright install chromium)")
# The only script this module ever runs: read links, visible words (main/body minus script/style/noscript/template),
# the document title and every h1 out of the rendered DOM.
_EVAL_JS = """() => {
  const root = document.querySelector('main') || document.body || document.documentElement;
  const links = Array.prototype.map.call(document.querySelectorAll('a[href]'), function (a) { return a.href; });
  const h1 = Array.prototype.map.call(document.querySelectorAll('h1'), function (h) { return h.innerText || ''; });
  if (!root) { return {links: links, words: 0, title: document.title || '', h1: h1}; }
  const clone = root.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,template').forEach(function (el) { el.remove(); });
  const text = clone.textContent || '';
  const words = text.split(/\\s+/).filter(function (w) { return w.length > 0; }).length;
  return {links: links, words: words, title: document.title || '', h1: h1};
}"""


# ---------------------------------------------------------------------------------------------------------------
# small total helpers (never raise)
# ---------------------------------------------------------------------------------------------------------------
def _text(value, limit=MAX_STR):
    """Whitespace-collapsed, length-capped string of `value` ('' for None or garbage)."""
    if value is None or isinstance(value, bool):
        return ""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    s = " ".join(s.split())
    return s[:limit] if limit else s


def _words(value):
    """Non-negative int word count from `value`, 0 for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 < n < 10 ** 9 else 0


def _host_of(url):
    """Lowercased host of an absolute URL ('' when unparsable)."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def _clean_links(raw):
    """Absolute http(s) links from `raw`, fragments dropped, de-duplicated, capped at MAX_LINKS/MAX_STR."""
    out, seen = [], set()
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, str) or len(out) >= MAX_LINKS:
            continue
        try:
            url = urllib.parse.urldefrag(item.strip())[0]
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if not url or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            continue
        url = url[:MAX_STR]
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _scope_set(scope_hosts):
    """Set of lowercased hosts from a list of host strings (empty set for anything else)."""
    out = set()
    if isinstance(scope_hosts, (list, tuple, set)):
        for host in scope_hosts:
            if isinstance(host, str) and host.strip():
                out.add(host.strip().lower().rstrip("."))
    return out


def _in_scope(link, scope, page_host):
    """True when `link` is an http(s) URL whose host is in `scope` (or equals `page_host` when no scope is known)."""
    try:
        parts = urllib.parse.urlsplit(link)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.rstrip(".").lower()
    return host in scope if scope else host == page_host


# ---------------------------------------------------------------------------------------------------------------
# availability and SSRF-safe request policy
# ---------------------------------------------------------------------------------------------------------------
def available():
    """-> (bool, note): whether Playwright is importable here, plus a human-readable note either way."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False, NOT_AVAILABLE
    except Exception as exc:  # a broken optional install must never crash the audit
        return False, "JS render parity skipped: Playwright failed to import (%s)" % type(exc).__name__
    return True, "JS render parity available: Playwright is installed and will be used when asked."


def request_allowed(url, resolver=None):
    """True when the browser may load `url`: data:/blob:, or a public http(s) origin checked once per origin.

    Reuses fetcher.check_url (public addresses only, default ports only, no credentials); a ToolError means
    "no". Verdicts are cached per (scheme, host, port) so repeated resources do not re-resolve DNS.
    """
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url or len(url) > MAX_URL_LEN or any(c in url for c in "\r\n\t\x00 "):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False
    if scheme in ("data", "blob"):
        return True
    if scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return False
    key = (scheme, host, port)
    cached = _ALLOW_CACHE.get(key) if resolver is None else None
    if cached is not None:
        return cached
    try:
        if resolver is None:
            fetcher.check_url(url)
        else:
            fetcher.check_url(url, resolver=resolver)
        verdict = True
    except ToolError:
        verdict = False
    except Exception:  # anything unexpected is a refusal, never a crash
        verdict = False
    if resolver is None and len(_ALLOW_CACHE) < 5000:
        _ALLOW_CACHE[key] = verdict
    return verdict


# ---------------------------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------------------------
def _route_handler(allow):
    """-> handler(route) for context.route(): abort everything `allow` rejects, continue the rest."""
    def handler(route):
        try:
            permitted = getattr(route.request, "method", "GET") in ("GET", "HEAD") and bool(allow(route.request.url))
        except Exception:
            permitted = False
        try:
            if permitted:
                route.continue_()
            else:
                route.abort()
        except Exception:  # a route already handled/aborted must not abort the crawl
            pass
    return handler


def _render_one(context, url, allow, timeout_ms):
    """Render one URL -> {links, words, title, h1, error}; never raises."""
    rec = {"links": [], "words": 0, "title": "", "h1": [], "error": None}
    try:
        permitted = bool(allow(url))
    except Exception:
        permitted = False
    if not permitted:
        rec["error"] = "refused"
        return rec
    page = None
    try:
        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except Exception as exc:  # a navigation timeout is a result, not a crash
            rec["error"] = type(exc).__name__
        try:
            data = page.evaluate(_EVAL_JS)
        except Exception as exc:
            if rec["error"] is None:
                rec["error"] = type(exc).__name__
            data = None
        if isinstance(data, dict):
            rec["links"] = _clean_links(data.get("links"))
            rec["words"] = _words(data.get("words"))
            rec["title"] = _text(data.get("title"))
            h1s = data.get("h1")
            if isinstance(h1s, (list, tuple)):
                rec["h1"] = [_text(h) for h in h1s[:MAX_LIST] if isinstance(h, str)]
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    return rec


def render_pages(urls, user_agent=fetcher.GOOGLEBOT_UA, timeout_ms=DEFAULT_TIMEOUT_MS, max_pages=DEFAULT_MAX_PAGES,
                 playwright_factory=None, allow=None):
    """Render up to `max_pages` URLs in one headless Chromium -> {url: {links, words, title, h1, error}}.

    Playwright is imported here (never at import time); without it this raises ToolError(NOT_AVAILABLE). Every
    browser request is filtered through `allow` (default: request_allowed). `playwright_factory` exists for tests.
    """
    factory = playwright_factory
    if factory is None:
        ok, note = available()
        if not ok:
            raise ToolError(note)
        try:
            from playwright.sync_api import sync_playwright as factory
        except ImportError:
            raise ToolError(note) from None
    if not callable(factory):
        raise ToolError("seo.render.render_pages: playwright_factory must be callable")
    if not isinstance(urls, (list, tuple)):
        raise ToolError("seo.render.render_pages: urls must be a list of URLs")
    allow = allow if callable(allow) else request_allowed
    limit = max_pages if isinstance(max_pages, int) and not isinstance(max_pages, bool) and max_pages >= 0 \
        else DEFAULT_MAX_PAGES
    limit = min(limit, MAX_RENDER_PAGES)
    timeout = timeout_ms if isinstance(timeout_ms, int) and not isinstance(timeout_ms, bool) and timeout_ms > 0 \
        else DEFAULT_TIMEOUT_MS
    timeout = min(timeout, MAX_TIMEOUT_MS)
    _ALLOW_CACHE.clear()                       # verdicts never outlive one batch (DNS answers change)
    ua = user_agent if isinstance(user_agent, str) and user_agent else fetcher.GOOGLEBOT_UA
    todo, seen = [], set()
    for url in urls:
        if isinstance(url, str) and url and url not in seen and len(todo) < limit:
            seen.add(url)
            todo.append(url)
    result = {}
    if not todo:
        return result
    pins = []
    for url in todo:                           # pin each audited host to the address that was checked
        try:
            _s, host, _p, _path, ip = fetcher.check_url(url)
            pins.append("MAP %s %s" % (host, ip))
        except ToolError:
            continue
    args = ["--host-resolver-rules=" + ", ".join(sorted(set(pins)))] if pins and allow is request_allowed else []
    with factory() as pw:
        browser = pw.chromium.launch(headless=True, args=args) if args else pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=ua, viewport=dict(_VIEWPORT), is_mobile=True,
                                          java_script_enabled=True, service_workers="block",
                                          accept_downloads=False)
            try:
                context.route("**/*", _route_handler(allow))
                if hasattr(context, "route_web_socket"):   # route() does not see WebSockets: refuse them all
                    context.route_web_socket("**/*", lambda ws: ws.close())
                if hasattr(context, "on"):
                    context.on("page", lambda p: None)       # popups are closed when the page that opened them closes
                for url in todo:
                    result[url] = _render_one(context, url, allow, timeout)
            finally:
                try:
                    context.close()
                except Exception:                            # a crashed browser must not lose the results so far
                    pass
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return result


def render_isolated(urls, user_agent=fetcher.GOOGLEBOT_UA, timeout_ms=DEFAULT_TIMEOUT_MS, max_pages=DEFAULT_MAX_PAGES,
                    runner=None):
    """render_pages() in a child Python process with a hard wall-clock limit, so a page that freezes the browser's main
    thread (evaluate() has no timeout), a Chromium crash or a memory blow-up can only cost this step, never the audit."""
    import json
    import subprocess
    import sys
    pages = min(max(1, int(max_pages or 1)), MAX_RENDER_PAGES)
    tmo = min(max(1000, int(timeout_ms or DEFAULT_TIMEOUT_MS)), MAX_TIMEOUT_MS)
    req = json.dumps({"urls": [u for u in urls if isinstance(u, str)][:pages], "user_agent": user_agent,
                      "timeout_ms": tmo, "max_pages": pages})
    import os
    run = runner or subprocess.run
    env = dict(os.environ)
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the dir holding adsops/
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = run([sys.executable, "-m", "adsops.seo.render"], input=req, capture_output=True, text=True, env=env,
                   cwd=pkg_root, timeout=pages * (tmo / 1000.0 + 20) + 30)
    except subprocess.TimeoutExpired:
        raise ToolError("JS render step exceeded its time limit and was stopped") from None
    if getattr(proc, "returncode", 1) != 0:
        raise ToolError("JS render step failed (exit %s)" % getattr(proc, "returncode", "?"))
    try:
        out = json.loads(proc.stdout or "{}")
    except ValueError:
        raise ToolError("JS render step returned no result") from None
    if not isinstance(out, dict):
        raise ToolError("JS render step returned no result")
    if out.get("error"):
        raise ToolError(str(out["error"])[:300])
    return out.get("rendered") or {}


def _child_main():
    """python3 -m adsops.seo.render < {"urls": [...], ...}: one JSON object out (used by render_isolated)."""
    import json
    import sys
    try:
        req = json.loads(sys.stdin.read() or "{}")
        res = render_pages(req.get("urls") or [], user_agent=req.get("user_agent") or fetcher.GOOGLEBOT_UA,
                           timeout_ms=int(req.get("timeout_ms") or DEFAULT_TIMEOUT_MS),
                           max_pages=int(req.get("max_pages") or DEFAULT_MAX_PAGES))
        sys.stdout.write(json.dumps({"rendered": res}))
    except ToolError as e:
        sys.stdout.write(json.dumps({"error": str(e)}))
    except Exception as e:                                       # noqa: BLE001
        sys.stdout.write(json.dumps({"error": "render failed: " + type(e).__name__}))
    return 0


# ---------------------------------------------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------------------------------------------
def parity(crawl, rendered, scope_hosts=None):
    """Compare rendered pages with the crawl's raw PageDocuments -> (findings, passed) per SPEC-B.

    For every rendered URL without an error whose crawl page has a document: in-scope rendered links absent from
    doc["links"] are js_only_links (the page itself is ignored), and rendered words > raw words * 1.5 + 50 is
    js_content_gap. `passed` names the codes that had at least one comparable page and found nothing.
    """
    if not isinstance(crawl, dict):
        raise ToolError("seo.render.parity: crawl must be a dict")
    rendered = rendered if isinstance(rendered, dict) else {}
    pages = crawl.get("pages") if isinstance(crawl.get("pages"), dict) else {}
    settings = crawl.get("settings") if isinstance(crawl.get("settings"), dict) else {}
    scope = _scope_set(scope_hosts if scope_hosts is not None else settings.get("scope_hosts"))
    js_urls, js_examples, js_total = [], [], 0
    gap_urls, gap_examples = [], []
    comparable = 0
    for url in list(rendered)[:MAX_URLS]:
        rec = rendered.get(url)
        if not isinstance(url, str) or not isinstance(rec, dict) or rec.get("error"):
            continue
        page = pages.get(url)
        if not isinstance(page, dict):
            continue
        doc = page.get("doc")
        if not isinstance(doc, dict):
            continue
        comparable += 1
        page_host = _host_of(url)
        raw_links = set()
        doc_links = doc.get("links")
        if isinstance(doc_links, (list, tuple)):
            for entry in doc_links:
                href = entry.get("href") if isinstance(entry, dict) else entry
                if isinstance(href, str) and href:
                    raw_links.add(urllib.parse.urldefrag(href)[0])
        ren = set()
        links = rec.get("links")
        if isinstance(links, (list, tuple)):
            for link in links:
                if not isinstance(link, str) or not link or link == url:
                    continue
                if _in_scope(link, scope, page_host):
                    ren.add(urllib.parse.urldefrag(link)[0])
        js_only = sorted(ren - raw_links)
        if js_only:
            js_urls.append(url)
            js_total += len(js_only)
            if len(js_examples) < MAX_LIST:
                js_examples.append([url, len(js_only), js_only[:5]])
        raw_words = _words(doc.get("words"))
        ren_words = _words(rec.get("words"))
        if ren_words > raw_words * 1.5 + 50:
            gap_urls.append(url)
            if len(gap_examples) < MAX_LIST:
                gap_examples.append([url, raw_words, ren_words])
    findings, passed = [], []
    if js_urls:
        findings.append({
            "code": "js_only_links", "urls": js_urls[:MAX_URLS],
            "detail": "%d of %d comparable page(s) only get their in-scope links after JavaScript runs: %d link(s) "
                      "are missing from the raw HTML response." % (len(js_urls), comparable, js_total),
            "data": {"pages": len(js_urls), "js_only_total": js_total, "examples": js_examples}})
    elif comparable:
        passed.append("js_only_links")
    if gap_urls:
        findings.append({
            "code": "js_content_gap", "urls": gap_urls[:MAX_URLS],
            "detail": "%d of %d comparable page(s) carry far more text after rendering than in the raw HTML "
                      "(rendered words > 1.5x raw + 50)." % (len(gap_urls), comparable),
            "data": {"pages": len(gap_urls), "examples": gap_examples}})
    elif comparable:
        passed.append("js_content_gap")
    return findings, passed


# ── selftest ───────────────────────────────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "seo.render.selftest: " + msg
        n += 1

    # --- available() ------------------------------------------------------------------------------------------
    avail = available()
    ck(isinstance(avail, tuple) and len(avail) == 2 and isinstance(avail[0], bool) and isinstance(avail[1], str),
       "available() returns a (bool, note) tuple")
    ck(NOT_AVAILABLE.startswith("JS render parity skipped: Playwright is not installed"), "missing note is actionable")

    # --- request_allowed() ------------------------------------------------------------------------------------
    ck(request_allowed("https://127.0.0.1/") is False, "loopback refused")
    ck(request_allowed("http://10.0.0.1/x") is False, "private address refused")
    ck(request_allowed("file:///etc/passwd") is False, "file scheme refused")
    ck(request_allowed("data:image/png;base64,xx") is True, "data: URIs allowed")
    ck(request_allowed("blob:https://ex.com/1") is True, "blob: URIs allowed")
    ck(request_allowed("https://93.184.216.34/") is True, "public IP literal allowed without DNS")
    ck(request_allowed("https://[::1]/") is False, "IPv6 loopback refused")
    ck(request_allowed("http://10.0.0.1:8080/x") is False, "non-default port refused")
    ck(request_allowed("https://u:p@93.184.216.34/") is False, "credentials refused")
    ck(request_allowed("https://ex.com/a b") is False, "whitespace refused")
    ck(request_allowed(None) is False and request_allowed(123) is False and request_allowed("") is False,
       "non-strings and empty strings refused")
    ck(request_allowed("https://ex.com/") is request_allowed("https://ex.com/other"), "verdicts are cached per origin")

    # --- fake Playwright (no browser, no network) -------------------------------------------------------------
    def fake_site(payloads=None, fail_urls=()):
        """-> (factory, box, FakeRoute); box collects the browser/context once render_pages runs."""
        payloads = dict(payloads or {})
        fails = set(fail_urls)
        box = {}

        class FakeRequest:
            def __init__(self, url):
                self.url = url

        class FakeRoute:
            def __init__(self, url):
                self.request = FakeRequest(url)
                self.actions = []

            def abort(self):
                self.actions.append("abort")

            def continue_(self):
                self.actions.append("continue")

        class FakePage:
            def __init__(self, ctx):
                self.ctx = ctx
                self.gotos = []
                self.closed = False

            def goto(self, url, wait_until=None, timeout=None):
                self.gotos.append((url, wait_until, timeout))
                if url in fails:
                    raise RuntimeError("goto failed")

            def evaluate(self, js):
                url = self.gotos[-1][0] if self.gotos else ""
                return payloads.get(url, {"links": [], "words": 0, "title": "", "h1": []})

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.kwargs = None
                self.routes = []
                self.pages = []
                self.closed = False

            def route(self, pattern, handler):
                self.routes.append((pattern, handler))

            def new_page(self):
                page = FakePage(self)
                self.pages.append(page)
                return page

            def close(self):
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.launch_kw = None
                self.closed = False

            def new_context(self, **kw):
                ctx = FakeContext()
                ctx.kwargs = kw
                box["context"] = ctx
                return ctx

            def close(self):
                self.closed = True

        class FakeChromium:
            def __init__(self):
                self.launch_kw = None

            def launch(self, **kw):
                self.launch_kw = kw
                browser = FakeBrowser()
                browser.launch_kw = kw
                box["browser"] = browser
                return browser

        class FakePW:
            def __init__(self):
                self.chromium = FakeChromium()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return (lambda: FakePW()), box, FakeRoute

    url_a = "https://ex.com/a"
    payloads = {url_a: {"links": ["https://ex.com/a#top", "https://ex.com/b#x", "mailto:x@y.z",
                                  "javascript:void(0)", "/relative", "data:text/html,x", "https://ex.com/b"],
                        "words": 12, "title": "A  title", "h1": ["One"]}}
    factory, box, FakeRoute = fake_site(payloads)
    res = render_pages([url_a, url_a, 42, ""], playwright_factory=factory,
                       allow=lambda u: "10.0.0.1" not in u)
    ck(list(res) == [url_a], "render_pages keys are the requested URLs, de-duplicated, non-strings dropped")
    ck(res[url_a]["links"] == ["https://ex.com/a", "https://ex.com/b"],
       "fragments dropped, non-http(s) links dropped, duplicates removed")
    ck(res[url_a]["words"] == 12 and res[url_a]["title"] == "A title" and res[url_a]["h1"] == ["One"],
       "words/title/h1 captured, title collapsed")
    ck(res[url_a]["error"] is None, "clean render has error None")
    ctx = box["context"]
    ck(ctx.kwargs.get("viewport") == {"width": 412, "height": 823} and ctx.kwargs.get("is_mobile") is True,
       "mobile viewport configured")
    ck(ctx.kwargs.get("service_workers") == "block" and ctx.kwargs.get("accept_downloads") is False
       and ctx.kwargs.get("java_script_enabled") is True
       and ctx.kwargs.get("user_agent") == fetcher.GOOGLEBOT_UA, "context hardening options set")
    ck(box["browser"].launch_kw == {"headless": True}, "chromium launched headless")
    ck(ctx.routes and ctx.routes[0][0] == "**/*", "one catch-all route installed")
    handler = ctx.routes[0][1]
    private, public = FakeRoute("http://10.0.0.1/x"), FakeRoute("https://ex.com/img.png")
    handler(private)
    handler(public)
    ck(private.actions == ["abort"] and public.actions == ["continue"], "route handler aborts private, continues public")
    ck(ctx.closed and box["browser"].closed and all(page.closed for page in ctx.pages),
       "page, context and browser all closed")

    # --- refused start URL, goto failure, max_pages -----------------------------------------------------------
    bad = "https://ex.com/bad"
    factory2, box2, FakeRoute2 = fake_site({url_a: payloads[url_a], bad: {"words": 7, "links": []}},
                                           fail_urls={bad})
    res2 = render_pages(["http://10.0.0.1/hidden", url_a, bad], playwright_factory=factory2,
                        allow=lambda u: "10.0.0.1" not in u)
    ck(res2["http://10.0.0.1/hidden"]["error"] == "refused", "a refused start URL is recorded as refused")
    ck(all("10.0.0.1" not in goto[0] for page in box2["context"].pages for goto in page.gotos),
       "a refused start URL is never navigated to")
    ck(res2[bad]["error"] == "RuntimeError" and res2[bad]["words"] == 7,
       "goto failure records the exception type but still evaluates")
    ck(box2["browser"].closed, "browser closed even when goto raises")
    private2, public2 = FakeRoute2("http://10.0.0.1/x"), FakeRoute2("https://ex.com/style.css")
    box2["context"].routes[0][1](private2)
    box2["context"].routes[0][1](public2)
    ck(private2.actions == ["abort"] and public2.actions == ["continue"], "injected allow drives the route handler")

    many = ["https://ex.com/%d" % i for i in range(6)]
    factory3, box3, _ = fake_site({u: {"words": 1, "links": []} for u in many})
    ck(len(render_pages(many, playwright_factory=factory3, max_pages=2, allow=lambda u: True)) == 2,
       "max_pages limits how many URLs are rendered")
    ck(render_pages([], playwright_factory=factory3, allow=lambda u: True) == {}, "no URLs -> no browser work")
    ck(len(_clean_links(["https://ex.com/%d" % i for i in range(2005)])) == MAX_LINKS, "links capped at 2000")
    ck(_text("x" * 900) == "x" * 500 and _words("many") == 0, "strings capped at 500, junk word counts are 0")

    real_available = globals().get("available")
    try:
        globals()["available"] = lambda: (False, "JS render parity skipped: test-only")
        raised = False
        try:
            render_pages([], playwright_factory=None)
        except ToolError:
            raised = True
        ck(raised, "render_pages raises ToolError when Playwright is unavailable")
    finally:
        globals()["available"] = real_available

    # --- parity -----------------------------------------------------------------------------------------------
    page_url = "https://ex.com/page"
    crawl = {"settings": {"scope_hosts": ["ex.com", "www.ex.com"]},
             "pages": {page_url: {"url": page_url, "doc": {"links": [{"href": "https://ex.com/a"},
                                                                   {"href": "https://ex.com/b#frag"}],
                                                           "words": 100}}}}
    rendered = {page_url: {"links": ["https://ex.com/a", "https://ex.com/b", "https://ex.com/c", "https://ex.com/d",
                                     "https://ex.com/e", "https://other.com/x", "https://other.com/y", page_url],
                           "words": 100, "title": "T", "h1": [], "error": None}}
    findings, passed = parity(crawl, rendered)
    js = [f for f in findings if f["code"] == "js_only_links"]
    ck(len(js) == 1 and js[0]["urls"] == [page_url], "js_only_links fires on the source page")
    ck(js[0]["data"]["examples"] == [[page_url, 3, ["https://ex.com/c", "https://ex.com/d", "https://ex.com/e"]]],
       "js_only_links counts 3 JS-only in-scope links, off-scope and self ignored")
    ck(passed == ["js_content_gap"], "only js_content_gap passes when links are JS-only and words match")
    ck(js[0]["detail"].endswith("."), "finding detail is one full sentence")

    rendered_gap = {page_url: {"links": ["https://ex.com/a", "https://ex.com/b"], "words": 400, "error": None}}
    gap, passed_gap = parity(crawl, rendered_gap)
    ck([f["code"] for f in gap] == ["js_content_gap"], "words 100 -> 400 is a js_content_gap")
    ck(gap[0]["data"]["examples"] == [[page_url, 100, 400]], "gap example carries raw and rendered words")
    ck(passed_gap == ["js_only_links"], "js_only_links passes when every rendered link is in the HTML")

    none, passed_none = parity(crawl, {page_url: {"links": ["https://ex.com/a", "https://ex.com/b#frag"],
                                                   "words": 100, "error": None}})
    ck(none == [] and passed_none == ["js_only_links", "js_content_gap"], "identical rendering passes both checks")
    ck(parity(crawl, {}) == ([], []), "empty rendered input yields no findings and nothing passed")

    crawl_noscope = {"pages": {page_url: {"doc": {"links": [], "words": 10}}}}
    no_scope, _ = parity(crawl_noscope, {page_url: {"links": ["https://ex.com/z", "https://other.com/z"],
                                                    "words": 10}})
    ck([f["code"] for f in no_scope] == ["js_only_links"] and no_scope[0]["data"]["examples"][0][1] == 1,
       "without scope_hosts only same-host links count as in scope")

    skipped, skipped_passed = parity(crawl, {page_url: {"links": [], "words": 0, "error": "TimeoutError"},
                                             "https://ex.com/nodoc": {"links": [], "words": 0, "error": None},
                                             "https://ex.com/missing": {"links": "garbage", "error": None}})
    ck(skipped == [] and skipped_passed == [], "render errors, missing pages and missing docs are skipped")

    hostile = {"pages": {page_url: {"doc": {"links": [None, 5, {"href": 7}, {"nohref": 1}], "words": "many"}}}}
    hostile_f, _ = parity(hostile, {page_url: {"links": [7, None, "https://ex.com/q"], "words": "a lot"}})
    ck([f["code"] for f in hostile_f] == ["js_only_links"], "junk link rows are ignored, real JS-only link survives")

    raised = False
    try:
        parity([], {})
    except ToolError:
        raised = True
    ck(raised, "parity raises ToolError for a non-dict crawl")
    ck(parity(crawl, {"https://ex.com/deep": {"links": ["https://ex.com/deep#x"], "error": None}})[0] == [],
       "self links are ignored in parity")
    # isolation wrapper (fake runner: no browser, no network)
    import json as _json
    import subprocess as _sp

    class _P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out
    got = render_isolated(["https://93.184.216.34/"], runner=lambda *a, **k: _P(0, _json.dumps(
        {"rendered": {"https://93.184.216.34/": {"links": [], "words": 3, "title": "t", "h1": [], "error": None}}})))
    ck(got["https://93.184.216.34/"]["words"] == 3, "render_isolated returns the child's result")

    def _hang(*a, **k):
        raise _sp.TimeoutExpired("x", 1)
    try:
        render_isolated(["https://93.184.216.34/"], runner=_hang)
        ck(False, "a hung render must raise ToolError")
    except ToolError:
        ck(True, "a hung render child is stopped and reported")
    try:
        render_isolated(["https://93.184.216.34/"], runner=lambda *a, **k: _P(0, _json.dumps({"error": "nope"})))
        ck(False, "a child error must raise ToolError")
    except ToolError:
        ck(True, "a child error surfaces as ToolError")

    class _Route:
        def __init__(self, url, method):
            self.request = type("R", (), {"url": url, "method": method})()
            self.done = None

        def continue_(self):
            self.done = "continue"

        def abort(self):
            self.done = "abort"
    h = _route_handler(lambda u: True)
    r1, r2 = _Route("https://93.184.216.34/x", "POST"), _Route("https://93.184.216.34/x", "GET")
    h(r1)
    h(r2)
    ck(r1.done == "abort" and r2.done == "continue", "the browser may only GET/HEAD: page-script POSTs are aborted")
    return n


if __name__ == "__main__":
    raise SystemExit(_child_main())
