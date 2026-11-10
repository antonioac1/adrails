"""One polite, SSRF-safe HTTP GET for the site audit crawler (adsops/seo). Read-only; never follows a redirect itself.

Safety (reuses adsops.aeo_tools.public_addresses, the same rule aeo_site_audit uses):
  * http/https only, default ports only (80/443), no userinfo in the URL;
  * the host is resolved ONCE, every address must be public, and the TCP connection goes to that exact address
    (TLS still verifies the certificate for the hostname via SNI), so a DNS answer that changes between the check and
    the connect (rebinding) cannot reach a private address;
  * redirects are returned to the caller (status + Location), which re-checks every hop through this function;
  * the body is capped (default 3 MB, compressed AND decompressed), the connect/read timeout is bounded;
  * nothing fetched is executed: bodies are returned as text for parsing only.

`fetch()` never raises on network trouble: it returns status 0 and a short `error` (exception type only, no
response text), so one dead URL is a finding, not a crash. Unsafe URLs come back as status 0 with error
"refused: ...". Tests inject `resolver` and `connector`; `allow_private` exists ONLY for loopback fixture tests
and is never reachable from an MCP tool or the CLI.
"""
import http.client
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
import zlib

from adsops.errors import ToolError

GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
MAX_BYTES = 3_000_000
KEEP_HEADERS = ("content-type", "location", "x-robots-tag", "strict-transport-security", "content-security-policy",
                "x-content-type-options", "x-frame-options", "referrer-policy", "cache-control", "content-encoding",
                "content-length", "last-modified", "link", "permissions-policy")
_CHARSET = re.compile(r"charset=[\"']?([A-Za-z0-9_\-]+)", re.I)


def check_url(url, allow_private=False, resolver=socket.getaddrinfo):
    """-> (scheme, host, port, path_with_query, pinned_ip). ToolError when the URL may not be fetched."""
    from adsops.aeo_tools import public_addresses
    if not isinstance(url, str) or len(url) > 4096 or any(c in url for c in "\r\n\t\x00 "):
        raise ToolError("URL is empty, too long or contains whitespace/control characters")
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ToolError("only http(s) URLs can be fetched")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise ToolError("URLs carrying credentials are refused")
    host = (u.hostname or "").rstrip(".").lower()
    if not host:
        raise ToolError("URL has no host")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise ToolError("URL has an invalid port") from None
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    if allow_private:                                    # loopback fixture tests only
        try:
            infos = resolver(host, None)
        except (OSError, UnicodeError):
            raise ToolError(f"cannot resolve {host}") from None
        return u.scheme, host, port, path, infos[0][4][0]
    if port not in (80, 443):
        raise ToolError("only the default ports (80/443) are crawled")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global or ip.is_multicast:
            raise ToolError("refusing to fetch a non-public address")
        return u.scheme, host, port, path, str(ip)
    return u.scheme, host, port, path, public_addresses(host, resolver)[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


def default_connector(scheme, host, ip, port, timeout):
    if scheme == "https":
        return _PinnedHTTPS(host, ip, port, timeout, ssl.create_default_context())
    return _PinnedHTTP(host, ip, port, timeout)


def _decode_body(raw, encoding, max_bytes):
    """Undo gzip/deflate with a hard output cap (decompression bombs stop at max_bytes). -> (bytes, truncated)."""
    enc = (encoding or "").strip().lower()
    if enc in ("gzip", "x-gzip", "deflate"):
        wbits = 16 + zlib.MAX_WBITS if "gzip" in enc else zlib.MAX_WBITS
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(raw, max_bytes + 1)
        except zlib.error:
            try:                                         # raw deflate without zlib header
                d = zlib.decompressobj(-zlib.MAX_WBITS)
                out = d.decompress(raw, max_bytes + 1)
            except zlib.error:
                return b"", True
        return out[:max_bytes], len(out) > max_bytes or bool(d.unconsumed_tail)
    return raw[:max_bytes], len(raw) > max_bytes


def fetch(url, user_agent=GOOGLEBOT_UA, timeout=15, max_bytes=MAX_BYTES, resolver=socket.getaddrinfo,
          connector=None, allow_private=False, clock=time.monotonic, method="GET"):
    """One request, no redirect following. -> {url, status, headers{lower: str}, body (str), bytes, truncated, ms,
    ctype, error}. status 0 = not fetched (see error)."""
    out = {"url": url, "status": 0, "headers": {}, "body": "", "bytes": 0, "truncated": False, "ms": 0, "ctype": "",
           "error": None}
    try:
        scheme, host, port, path, ip = check_url(url, allow_private, resolver)
    except ToolError as e:
        out["error"] = "refused: " + str(e)[:120]
        return out
    conn = None
    t0 = clock()
    try:
        conn = (connector or default_connector)(scheme, host, ip, port, timeout)
        default_port = 443 if scheme == "https" else 80
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if port == default_port else "%s:%d" % (host, port))
        conn.putheader("User-Agent", user_agent)
        conn.putheader("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5")
        conn.putheader("Accept-Encoding", "gzip, deflate")
        conn.putheader("Connection", "close")
        conn.endheaders()
        resp = conn.getresponse()
        raw = resp.read(max_bytes + 1) if method != "HEAD" else b""
        hdrs = {}
        for k, v in resp.getheaders():
            k = k.lower()
            if k in KEEP_HEADERS:
                hdrs[k] = (hdrs[k] + ", " + v) if k in hdrs else v
        body, trunc = _decode_body(raw, hdrs.get("content-encoding"), max_bytes)
        ctype = hdrs.get("content-type", "")
        m = _CHARSET.search(ctype)
        charset = m.group(1) if m else "utf-8"
        try:
            text = body.decode(charset, "replace")
        except LookupError:
            text = body.decode("utf-8", "replace")
        out.update(status=int(resp.status), headers=hdrs, body=text, bytes=len(body),
                   truncated=trunc or len(raw) > max_bytes, ctype=ctype.split(";")[0].strip().lower())
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as e:
        out["error"] = type(e).__name__
    finally:
        out["ms"] = int((clock() - t0) * 1000)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return out


# ── selftest ───────────────────────────────────────────────────────────────────────────────────────────────────
def selftest():
    import gzip
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "seo.fetcher.selftest: " + msg
        n += 1

    pub = lambda h, p: [(2, 1, 6, "", ("93.184.216.34", 0))]
    priv = lambda h, p: [(2, 1, 6, "", ("10.0.0.5", 0))]
    mixed = lambda h, p: [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("127.0.0.1", 0))]
    for bad, res in (("file:///etc/passwd", pub), ("ftp://a.example/", pub), ("http://a.example:8080/", pub),
                     ("https://u:p@a.example/", pub), ("https://a.example/\r\nX: y", pub), ("http://10.0.0.1/", pub),
                     ("http://127.0.0.1/", pub), ("http://[::1]/", pub), ("http://169.254.169.254/latest/", pub),
                     ("https://inner.example/", priv), ("https://rebind.example/", mixed), ("http://localhost/", pub),
                     ("http://printer.local/", pub), ("http://[::ffff:127.0.0.1]/", pub)):
        r = fetch(bad, resolver=res, connector=lambda *a: (_ for _ in ()).throw(AssertionError("connected")))
        ck(r["status"] == 0 and r["error"].startswith("refused"), f"refused {bad}")

    seen = {}

    class Resp:
        def __init__(self, status, headers, body):
            self.status, self._h, self._b = status, headers, body

        def getheaders(self):
            return self._h

        def read(self, n):
            return self._b[:n]

    class Conn:
        def __init__(self, scheme, host, ip, port, timeout, resp):
            seen.update(scheme=scheme, host=host, ip=ip, port=port, headers=[])
            self.resp = resp

        def putrequest(self, m, p, **k):
            seen["req"] = (m, p)

        def putheader(self, k, v):
            seen["headers"].append((k, v))

        def endheaders(self):
            pass

        def getresponse(self):
            return self.resp

        def close(self):
            seen["closed"] = True

    def conn_for(resp):
        return lambda *a: Conn(*a, resp=resp)

    r = fetch("https://a.example/x?y=1#frag", resolver=pub,
              connector=conn_for(Resp(200, [("Content-Type", "text/html; charset=utf-8"), ("Set-Cookie", "s=1"),
                                            ("Strict-Transport-Security", "max-age=1")], "<p>héllo</p>".encode())))
    ck(r["status"] == 200 and r["body"] == "<p>héllo</p>" and r["ctype"] == "text/html", "200 body decoded")
    ck(seen["ip"] == "93.184.216.34" and seen["host"] == "a.example" and seen["req"] == ("GET", "/x?y=1"),
       "connects to the pinned address, fragment dropped")
    ck(("User-Agent", GOOGLEBOT_UA) in seen["headers"] and seen.get("closed"), "Googlebot UA by default, closed")
    ck("set-cookie" not in r["headers"] and "strict-transport-security" in r["headers"], "only audit headers kept")
    r = fetch("https://a.example/", resolver=pub, user_agent="X-Bot/1",
              connector=conn_for(Resp(301, [("Location", "/new")], b"")))
    ck(r["status"] == 301 and r["headers"]["location"] == "/new" and ("User-Agent", "X-Bot/1") in seen["headers"],
       "redirect returned, not followed; UA configurable")
    big = b"a" * 5000
    r = fetch("https://a.example/", resolver=pub, max_bytes=1000, connector=conn_for(Resp(200, [], big)))
    ck(r["truncated"] and r["bytes"] == 1000, "body capped")
    bomb = gzip.compress(b"0" * 2_000_000)
    r = fetch("https://a.example/", resolver=pub, max_bytes=10_000,
              connector=conn_for(Resp(200, [("Content-Encoding", "gzip")], bomb)))
    ck(r["truncated"] and r["bytes"] == 10_000, "gzip bomb stops at the cap")
    r = fetch("https://a.example/", resolver=pub,
              connector=conn_for(Resp(200, [("Content-Encoding", "gzip"), ("Content-Type", "text/html")],
                                      gzip.compress(b"<html>ok</html>"))))
    ck(r["body"] == "<html>ok</html>" and not r["truncated"], "gzip decoded")

    def boom(*a):
        raise ConnectionRefusedError("secret detail")
    r = fetch("https://a.example/", resolver=pub, connector=boom)
    ck(r["status"] == 0 and r["error"] == "ConnectionRefusedError", "network error -> status 0, type only")
    r = fetch("https://a.example/", resolver=pub,
              connector=conn_for(Resp(200, [("Content-Type", "text/html; charset=bogus-cs")], b"ok")))
    ck(r["body"] == "ok", "unknown charset falls back to utf-8")
    ck(check_url("http://127.0.0.1:8123/a", allow_private=True,
                 resolver=lambda h, p: [(2, 1, 6, "", ("127.0.0.1", 0))])[3] == "/a", "fixture mode allows loopback")
    return n
