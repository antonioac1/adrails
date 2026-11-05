"""adsops_lp_diagnose core: quality-score components per ad group plus a lightweight landing-page check. Read-only.

Google side: two GAQL reads through the existing client (keyword quality_info, ENABLED ad final URLs).
Page side: a plain HTTP GET per distinct final URL (status, response time, bytes, <title>, first <h1>); no browser,
no JavaScript, so a page whose H1 is injected by script is reported as "no H1 in the HTML" and said so.
`fetch` is injectable so the selftest never touches the network.
"""
import ipaddress
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from adsops.errors import ToolError, scrub

BAD = {"BELOW_AVERAGE": 2, "AVERAGE": 1, "ABOVE_AVERAGE": 0}
STOP = {"near", "me", "in", "the", "of", "for", "a", "an", "and", "to", "best", "top", "cheap", "my", "company",
        "companies", "services", "service", "fl", "florida"}
MAX_BYTES = 400_000


def _toks(s):
    return re.findall(r"[a-z0-9]+", str(s or "").lower())


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.h1, self._in = "", "", None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title" and not self.title:
            self._in = "title"
        elif tag == "h1" and not self.h1:
            self._in = "h1"

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("title", "h1"):
            self._in = None if self._in == tag else self._in

    def handle_data(self, data):
        if self._skip:
            return
        if self._in == "title":
            self.title += data
        elif self._in == "h1":
            self.h1 += data


def parse_html(html):
    p = _Page()
    try:
        p.feed(html or "")
    except Exception:  # noqa: BLE001 — broken markup must not kill the report
        pass
    return {"title": " ".join(p.title.split()), "h1": " ".join(p.h1.split())}


def _safe_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ToolError(f"not an http(s) URL: {scrub(url, 80)}")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ToolError("refusing to fetch a local address")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ToolError("refusing to fetch a private address")
    except ValueError:
        pass
    return url


def http_fetch(url, timeout=10):
    """-> {status, ms, bytes, html, final_url} or {error}. Plain GET, honest UA, body capped."""
    try:
        _safe_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "AdRails-lp-check/0.1 (+read-only landing page check)"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(MAX_BYTES + 1)
                status, final = r.status, r.geturl()
        except urllib.error.HTTPError as e:
            body, status, final = e.read(MAX_BYTES + 1), e.code, url
        ms = int((time.perf_counter() - t0) * 1000)
        return {"status": status, "ms": ms, "bytes": len(body), "truncated": len(body) > MAX_BYTES,
                "html": body[:MAX_BYTES].decode("utf-8", "replace"), "final_url": final}
    except ToolError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — a dead host is a finding, not a crash
        return {"error": f"{type(e).__name__}"}


# ── Google reads ───────────────────────────────────────────────────────────────────────────────────────────────
def _g(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k, d.get(re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k)))
    return d


def read_account(client, campaign_id):
    cid = int(campaign_id)
    kws = client.search(
        "SELECT ad_group.id, ad_group.name, ad_group_criterion.keyword.text, ad_group_criterion.quality_info.quality_score, "
        "ad_group_criterion.quality_info.creative_quality_score, ad_group_criterion.quality_info.post_click_quality_score, "
        "ad_group_criterion.quality_info.search_predicted_ctr FROM keyword_view WHERE campaign.id = %d "
        "AND ad_group_criterion.status = 'ENABLED' AND ad_group_criterion.negative = FALSE "
        "AND ad_group.status = 'ENABLED'" % cid)
    ads = client.search(
        "SELECT ad_group.id, ad_group.name, ad_group_ad.ad.final_urls FROM ad_group_ad WHERE campaign.id = %d "
        "AND ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED'" % cid)
    groups = {}
    for r in kws:
        gid = str(_g(r, "ad_group", "id"))
        g = groups.setdefault(gid, {"id": gid, "name": _g(r, "ad_group", "name"), "keywords": [], "urls": [],
                                    "qs": [], "ad_relevance": [], "landing": [], "ctr": []})
        q = _g(r, "ad_group_criterion", "quality_info") or {}
        g["keywords"].append(str(_g(r, "ad_group_criterion", "keyword", "text") or ""))
        if q.get("qualityScore", q.get("quality_score")):
            g["qs"].append(int(q.get("qualityScore", q.get("quality_score"))))
        for key, camel, snake in (("ad_relevance", "creativeQualityScore", "creative_quality_score"),
                                  ("landing", "postClickQualityScore", "post_click_quality_score"),
                                  ("ctr", "searchPredictedCtr", "search_predicted_ctr")):
            v = q.get(camel, q.get(snake))
            if v and v not in ("UNSPECIFIED", "UNKNOWN"):
                g[key].append(v)
    for r in ads:
        gid = str(_g(r, "ad_group", "id"))
        g = groups.setdefault(gid, {"id": gid, "name": _g(r, "ad_group", "name"), "keywords": [], "urls": [],
                                    "qs": [], "ad_relevance": [], "landing": [], "ctr": []})
        for u in (_g(r, "ad_group_ad", "ad", "final_urls") or []):
            if u not in g["urls"]:
                g["urls"].append(u)
    return list(groups.values())


def _worst(vals):
    return max(vals, key=lambda v: BAD.get(v, 1)) if vals else None


def _status(vals):
    """Plain summary: the worst component seen across the ad group's keywords, plus how many keywords share it."""
    w = _worst(vals)
    return None if w is None else {"worst": w, "keywords_at_worst": sum(1 for v in vals if v == w), "keywords_rated": len(vals)}


def message_match(g, page, towns):
    text = _toks(page.get("title", "") + " " + page.get("h1", ""))
    best, best_kw = 0.0, None
    for kw in g["keywords"]:
        sig = [t for t in _toks(kw) if t not in STOP]
        if sig:
            sc = sum(1 for t in sig if t in text) / len(sig)
            if sc > best:
                best, best_kw = sc, kw
    town_hit = None
    if towns:
        town_hit = any(set(_toks(t)) <= set(text) for t in towns)
    return {"keyword_score": round(best, 2), "best_keyword": best_kw, "town_in_headline": town_hit}


def diagnose(groups, fetch, towns=None, max_urls=25):
    towns = towns or []
    urls = []
    for g in groups:
        for u in g["urls"]:
            if u not in urls:
                urls.append(u)
    skipped = urls[max_urls:]
    pages = {}
    for u in urls[:max_urls]:
        res = fetch(u)
        info = parse_html(res.get("html", "")) if "html" in res else {"title": "", "h1": ""}
        pages[u] = {k: res.get(k) for k in ("status", "ms", "bytes", "error", "final_url", "truncated") if k in res} | info
    findings = []

    def add(score, g, url, issue, why, fix):
        findings.append({"score": score, "ad_group": g["name"], "url": url, "issue": issue, "why": why, "fix": fix})

    out_groups = []
    for g in groups:
        comp = {"landing_page_experience": _status(g["landing"]), "ad_relevance": _status(g["ad_relevance"]),
                "expected_ctr": _status(g["ctr"])}
        out_groups.append({"ad_group": g["name"], "keywords": len(g["keywords"]),
                           "quality_score_avg": round(sum(g["qs"]) / len(g["qs"]), 1) if g["qs"] else None,
                           "components": comp, "urls": g["urls"]})
        for label, key, score, fix in (
                ("landing page experience", "landing_page_experience", 80,
                 "Make the page open faster and say, near the top, what the searcher typed."),
                ("expected click-through rate", "expected_ctr", 70, "Rewrite headlines to lead with the searcher's own words."),
                ("ad relevance", "ad_relevance", 65, "Put the main keyword in a headline or split the ad group by intent.")):
            c = comp[key]
            if c and c["worst"] == "BELOW_AVERAGE":
                add(score, g, None, f"Google rates {label} below average",
                    f"{c['keywords_at_worst']} of {c['keywords_rated']} rated keywords are below average, which raises what you pay per click.", fix)
        if not g["urls"]:
            add(60, g, None, "No enabled ad with a final URL", "There is no page to check for this ad group.", "Check that the ad group has an enabled ad.")
        for u in g["urls"]:
            p = pages.get(u)
            if p is None:
                continue
            if p.get("error"):
                add(100, g, u, "The landing page could not be fetched", f"Fetch failed ({p['error']}). Visitors may see the same.", "Open it in a browser; fix DNS, TLS or the server.")
                continue
            if p["status"] >= 400:
                add(100, g, u, f"The landing page returns HTTP {p['status']}", "Clicks are paying for an error page.", "Fix the URL or pause the ads until it loads.")
                continue
            if p["ms"] >= 3000:
                add(60, g, u, f"The page took {p['ms'] / 1000:.1f}s to respond", "Slow pages lose visitors before they read anything.", "Check hosting, caching and image size.")
            elif p["ms"] >= 1500:
                add(30, g, u, f"The page took {p['ms'] / 1000:.1f}s to respond", "Slower than ideal for mobile visitors.", "Check caching and image size.")
            if p["bytes"] >= 1_500_000:
                add(35, g, u, f"The HTML alone is {p['bytes'] / 1e6:.1f} MB", "Heavy pages load slowly on phones.", "Trim inline scripts and data.")
            if not p["title"]:
                add(40, g, u, "The page has no <title>", "Search engines and visitors get no label for the page.", "Add a title naming the service and town.")
            if not p["h1"]:
                add(40, g, u, "No H1 headline in the page HTML", "A script-built headline is invisible to this check and to many crawlers.", "Put the main headline in the HTML as an H1.")
            mm = message_match(g, p, towns)
            p.setdefault("match", {})[g["name"]] = mm
            if (p["title"] or p["h1"]) and g["keywords"]:
                if mm["keyword_score"] < 0.34:
                    add(75, g, u, "The headline and title don't echo what this ad group bids on",
                        f"Best keyword '{mm['best_keyword']}' appears in only {int(mm['keyword_score'] * 100)}% of its words.",
                        "Make the H1 repeat the searcher's words, or point this ad group at a page that does.")
                elif mm["keyword_score"] < 0.67:
                    add(45, g, u, "The headline only partly matches this ad group's keywords",
                        f"Best keyword '{mm['best_keyword']}' appears in {int(mm['keyword_score'] * 100)}% of its words.",
                        "Work the missing words into the H1.")
            if towns and mm["town_in_headline"] is False and (p["title"] or p["h1"]):
                add(50, g, u, "No target town in the title or H1", f"Searchers are local; none of {', '.join(towns)} appears.", "Name the town in the H1.")
    for u in skipped:
        findings.append({"score": 1, "ad_group": None, "url": u, "issue": "Not checked (limit reached)", "why": f"max_urls={max_urls}", "fix": "Raise max_urls."})
    findings.sort(key=lambda f: (-f["score"], f["ad_group"] or "", f["issue"]))
    for i, f in enumerate(findings, 1):
        f["rank"] = i
        f["severity"] = "high" if f["score"] >= 70 else "medium" if f["score"] >= 40 else "low"
        del f["score"]
    return {"ad_groups": out_groups, "pages": pages, "findings": findings,
            "note": "Plain HTTP fetch, no JavaScript: a headline built by script shows as missing."}


def selftest():
    n = 0
    html_ok = "<html><head><title>Tampa Property Management | Acme</title></head><body><h1>Flat fee property management in Tampa</h1><script>x</script></body></html>"
    html_bad = "<html><body><h1>Welcome</h1></body></html>"

    class C:
        def __init__(self):
            self.q = []

        def search(self, gaql):
            self.q.append(gaql)
            if "keyword_view" in gaql:
                def kw(gid, name, text, qs, cr, lp, ctr):
                    return {"adGroup": {"id": gid, "name": name}, "adGroupCriterion": {"keyword": {"text": text},
                            "qualityInfo": {"qualityScore": qs, "creativeQualityScore": cr, "postClickQualityScore": lp, "searchPredictedCtr": ctr}}}
                return [kw("1", "Tampa core", "property management tampa", 7, "AVERAGE", "ABOVE_AVERAGE", "AVERAGE"),
                        kw("1", "Tampa core", "tampa property manager", 6, "AVERAGE", "BELOW_AVERAGE", "AVERAGE"),
                        kw("2", "New Port Richey", "property management new port richey", 4, "BELOW_AVERAGE", "BELOW_AVERAGE", "BELOW_AVERAGE"),
                        kw("3", "Dead page", "rental management", 5, "AVERAGE", "AVERAGE", "AVERAGE")]
            return [{"adGroup": {"id": "1", "name": "Tampa core"}, "adGroupAd": {"ad": {"finalUrls": ["https://ok.example/tampa"]}}},
                    {"adGroup": {"id": "2", "name": "New Port Richey"}, "adGroupAd": {"ad": {"finalUrls": ["https://ok.example/home"]}}},
                    {"adGroup": {"id": "3", "name": "Dead page"}, "adGroupAd": {"ad": {"finalUrls": ["https://gone.example/x"]}}}]

    fetched = []

    def fetch(u):
        fetched.append(u)
        if u.endswith("/tampa"):
            return {"status": 200, "ms": 400, "bytes": len(html_ok), "html": html_ok, "final_url": u}
        if u.endswith("/home"):
            return {"status": 200, "ms": 3400, "bytes": 2_000_000, "html": html_bad, "final_url": u}
        return {"status": 404, "ms": 200, "bytes": 10, "html": "nope", "final_url": u}

    c = C()
    groups = read_account(c, 42)
    assert all("campaign.id = 42" in q for q in c.q) and len(groups) == 3 and groups[0]["keywords"][1] == "tampa property manager"; n += 1
    r = diagnose(groups, fetch, towns=["Tampa", "New Port Richey"])
    assert sorted(fetched) == sorted({g["urls"][0] for g in groups}) and len(fetched) == 3; n += 1     # one fetch per URL
    top = r["findings"][0]
    assert top["rank"] == 1 and top["severity"] == "high" and "HTTP 404" in top["issue"] and top["ad_group"] == "Dead page"; n += 1
    issues = [f["issue"] for f in r["findings"]]
    assert any("took 3.4s" in i for i in issues) and any("don't echo" in i for i in issues) and any("No <title>" in i or "no <title>" in i for i in issues); n += 1
    assert any("landing page experience below average" in i for i in issues) and any("No target town" in i for i in issues); n += 1
    ok = [f for f in r["findings"] if f["ad_group"] == "Tampa core" and f["url"]]
    assert ok == [], ok; n += 1                                    # the healthy page earns no page findings
    g1 = r["ad_groups"][0]
    assert g1["quality_score_avg"] == 6.5 and g1["components"]["landing_page_experience"] == {"worst": "BELOW_AVERAGE", "keywords_at_worst": 1, "keywords_rated": 2}; n += 1
    assert r["pages"]["https://ok.example/tampa"]["h1"].startswith("Flat fee") and "x" not in r["pages"]["https://ok.example/tampa"]["h1"]; n += 1
    assert [f["rank"] for f in r["findings"]] == list(range(1, len(r["findings"]) + 1)); n += 1
    r2 = diagnose(groups, fetch, max_urls=1)
    assert any(f["issue"].startswith("Not checked") for f in r2["findings"]); n += 1
    for bad in ("file:///etc/passwd", "http://127.0.0.1/", "http://localhost/x", "http://192.168.1.5/", "ftp://x.example/"):
        assert "error" in http_fetch(bad), bad
    n += 1
    return n


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        print(f"lpdiag: {selftest()} checks")
        print("ALL PASS")
