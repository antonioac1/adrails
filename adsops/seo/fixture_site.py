"""Loopback fixture website for the site-audit selftests: a tiny http.server on 127.0.0.1 with known SEO defects.

Test-only. Nothing here runs at import time; `serve()` binds 127.0.0.1 on an ephemeral port, serves the in-memory
PAGES dict (mutable, so a test can change the site between two audits) and is always shut down by the context
manager. The crawler reaches it only through `fetcher.fetch(..., allow_private=True)`, which no tool or CLI exposes.

Planted defects (each one is asserted by adsops/seo_tools.selftest):
  /services/          hub linked only from the home page            -> starved_pages (hub)
  /services/{a..e}/   every sibling hard-codes a link to /services/a/ -> hardcoded_sibling_skew; e lacks Service
  /guides/one|two/    95% identical, both self-canonical, no Article -> near_duplicate_self_canonical, schema_gaps
  /about/             no doctype, no lang                            -> doctype_missing, lang_missing
  /owners/            noindex, in the sitemap, ad landing page, linked with utm_ tags -> sitemap_non_indexable,
                      internal_utm_links, noindex_with_inlinks, ad_lp_noindex, ad_lp_equity_sink
  /old/ -> /old2/ -> /about/   two redirects                         -> redirect_chain, internal_links_to_redirects
  /missing/           404 linked from home                           -> internal_links_to_errors
  /orphan/            only in the sitemap                            -> sitemap_orphans
  /private/x          Disallowed in robots.txt                       -> never fetched
"""
import contextlib
import http.server
import threading

WORDS = ("Baytown style property management covers leasing tenant screening rent collection maintenance inspections "
         "owner statements eviction support and local market pricing for single family homes condos and townhomes "
         "across the bay area with clear monthly fees and no hidden charges for owners who want steady income ")


def _filler(seed, n):
    """n sentences of text unique to `seed` (so unrelated fixture pages are never near-duplicates)."""
    words = WORDS.split()
    out = []
    for i in range(n * 12):
        out.append(words[(i * 7 + len(seed) * 3) % len(words)] + str((i * 31 + sum(map(ord, seed))) % 997))
    return " ".join(out)


def _page(title, body, *, doctype=True, lang="en", head="", main_words=0, seed=None):
    filler = _filler(seed or title, main_words) if main_words else ""
    return (("<!DOCTYPE html>\n" if doctype else "") + ("<html lang=\"%s\">" % lang if lang else "<html>")
            + "<head><title>%s</title><meta name=\"description\" content=\"%s page\">%s</head>" % (title, title, head)
            + "<body><nav><a href=\"/\">Home</a><a href=\"/about/\">About</a></nav><main><h1>%s</h1>%s<p>%s</p></main>"
              "</body></html>" % (title, body, filler))


def _service(slug, with_schema=True):
    ld = ("<script type=\"application/ld+json\">{\"@context\":\"https://schema.org\",\"@type\":\"Service\","
          "\"name\":\"%s\"}</script>" % slug) if with_schema else ""
    links = "<a href=\"/services/a/\">Service A</a> <a href=\"/owners/?utm_source=site&utm_medium=%s\">Owners</a>" % slug
    return _page("Service " + slug.upper(), links, head=ld + "<link rel=\"canonical\" href=\"/services/%s/\">" % slug,
                 main_words=3 + ord(slug) % 5)


def default_pages():
    """-> {path: (status, headers, body)} for the fixture site (fresh copy every call)."""
    guide = _page("Rental market guide", "<p>Rents rose in the bay area this year.</p>", main_words=6, seed="guide",
                  head="<link rel=\"canonical\" href=\"/guides/%s/\">")
    home_links = ("<a href=\"/services/\">Services</a> " + " ".join(
        "<a href=\"/services/%s/\">%s</a>" % (s, s.upper()) for s in "abcde")
        + " <a href=\"/guides/one/\">Guide one</a> <a href=\"/guides/two/\">Guide two</a>"
          " <a href=\"/old/\">Old page</a> <a href=\"/missing/\">Missing</a> <a href=\"/private/x\">Private</a>"
          " <a href=\"/owners/?utm_source=home&utm_medium=hero\">Owners</a> <a href=\"/about/\"><img src=\"/logo.png\"></a>")
    pages = {
        "/robots.txt": (200, {"Content-Type": "text/plain"},
                        "User-agent: *\nDisallow: /private/\n\nSitemap: {base}/sitemap.xml\n"),
        "/sitemap.xml": (200, {"Content-Type": "application/xml"},
                         "<?xml version=\"1.0\"?><urlset>" + "".join(
                             "<url><loc>{base}%s</loc></url>" % p for p in (
                                 "/", "/services/", "/services/a/", "/services/b/", "/guides/one/", "/guides/two/",
                                 "/about/", "/owners/", "/orphan/")) + "</urlset>"),
        "/": (200, {"Content-Type": "text/html; charset=utf-8"},
              _page("Fixture home", home_links, main_words=8, head="<link rel=\"canonical\" href=\"/\">")),
        "/services/": (200, {"Content-Type": "text/html"},
                       _page("Services hub", "<p>All services.</p>", main_words=5,
                             head="<link rel=\"canonical\" href=\"/services/\">")),
        "/guides/one/": (200, {"Content-Type": "text/html"}, guide.replace("%s", "one")),
        "/guides/two/": (200, {"Content-Type": "text/html"}, guide.replace("%s", "two")),
        "/about/": (200, {"Content-Type": "text/html"},
                    _page("About us", "<p>Who we are.</p>", doctype=False, lang=None, main_words=4)),
        "/owners/": (200, {"Content-Type": "text/html"},
                     _page("Owners offer", "<p>Flat fee offer for owners.</p>", main_words=4,
                           head="<meta name=\"robots\" content=\"noindex, follow\">")),
        "/orphan/": (200, {"Content-Type": "text/html"}, _page("Orphan", "<p>Nobody links here.</p>", main_words=4)),
        "/old/": (301, {"Location": "/old2/"}, ""),
        "/old2/": (301, {"Location": "/about/"}, ""),
        "/private/x": (200, {"Content-Type": "text/html"}, _page("Private", "<p>secret</p>")),
    }
    for s in "abcde":
        pages["/services/%s/" % s] = (200, {"Content-Type": "text/html"}, _service(s, with_schema=(s != "e")))
    return pages


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "fixture"
    sys_version = ""

    def log_message(self, *a):                       # keep test output clean
        pass

    def do_GET(self):                                # noqa: N802 (http.server API)
        site = self.server.site
        path = self.path.split("?", 1)[0]
        site["hits"].append(self.path)
        status, headers, body = site["pages"].get(path, (404, {"Content-Type": "text/html"}, "<h1>Not found</h1>"))
        data = body.replace("{base}", site["base"]).encode("utf-8")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def serve(pages=None, host_name="fixture.test"):
    """-> site dict {base, port, pages, hits}. Base URL uses `host_name` (map it to 127.0.0.1 with resolver())."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    srv.site = {"base": "http://%s:%d" % (host_name, port), "port": port, "pages": pages or default_pages(), "hits": []}
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield srv.site
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def resolver(host_name="fixture.test"):
    """getaddrinfo stand-in: `host_name` -> 127.0.0.1, everything else unresolvable (OSError, as DNS would)."""
    import socket

    def res(host, port):
        if host == host_name:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        raise socket.gaierror("fixture: unknown host")
    return res
