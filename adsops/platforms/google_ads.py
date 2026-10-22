"""Google Ads REST client and live reads.

Reads (`live_state`, `search_terms`) are behaviour-identical to gads_ctl.py: same GAQL strings,
same output keys, same camelCase/snake_case tolerant `_g` access. Nothing here runs at import
time: the transport is always injectable (`http=`) and defaults to urllib only when called.
"""
import datetime as dt
import json
import urllib.error
import urllib.request

from ..errors import ToolError, scrub

BASE = "https://googleads.googleapis.com"
DEFAULT_TIMEOUT = 60


# ── field access (copied from gads_ctl.py, camelCase/snake_case tolerant) ─────────────────────
def _snake(k):
    return "".join("_" + c.lower() if c.isupper() else c for c in k)


def _g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, d.get(_snake(k)) if isinstance(k, str) else None)
    return default if d is None else d


def _end_key(end_field):
    """'end_date_time' -> 'endDateTime' (the REST field name for that GAQL field)."""
    return "".join(w.capitalize() if i else w for i, w in enumerate(str(end_field).split("_")))


def _path_of(url):
    """The path part of a URL, for error messages (never the query string or headers)."""
    s = str(url)
    i = s.find("://")
    if i >= 0:
        j = s.find("/", i + 3)
        return s[j:] if j >= 0 else "/"
    return s


# ── default transport (only touched when called) ──────────────────────────────────────────────
def urllib_http(method, url, headers, body):
    """POST/GET JSON via urllib with a 60s timeout. HTTPError -> ToolError with a scrubbed body."""
    data = None
    if str(method).upper() != "GET":
        data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(str(url), method=str(method).upper(), data=data,
                                 headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        raise ToolError("HTTP %s on %s: %s" % (getattr(e, "code", "?"), _path_of(url),
                                               scrub(text)))
    except urllib.error.URLError as e:
        why = getattr(e, "reason", None) or "connection failed"
        raise ToolError("network error on %s: %s" % (_path_of(url), scrub(why)))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError("request to %s failed: %s" % (_path_of(url), scrub(e)))
    try:
        return json.loads(raw or b"{}")
    except Exception:
        raise ToolError("invalid JSON response from %s" % _path_of(url))


# ── client ────────────────────────────────────────────────────────────────────────────────────
class GoogleAdsClient:
    """Minimal Google Ads REST client: search (paginated) + mutate (validateOnly aware)."""

    def __init__(self, customer_id, token_provider, http=None, api_version="v24",
                 login_customer_id=None, developer_token=None):
        cid = str(customer_id if customer_id is not None else "").replace("-", "").strip()
        if not cid or not cid.isdigit():
            raise ToolError("invalid customer_id: %r" % (customer_id,))
        if token_provider is None or not hasattr(token_provider, "access_token"):
            raise ToolError("GoogleAdsClient needs a token provider with access_token()")
        self.customer_id = cid
        self.token_provider = token_provider
        self.http = http or urllib_http
        self.api_version = str(api_version or "v24")
        # Google expects the login customer id without dashes; the developer token is never logged.
        self.login_customer_id = (str(login_customer_id).replace("-", "").strip()
                                  if login_customer_id else None)
        self.developer_token = developer_token or None

    def __repr__(self):
        return "GoogleAdsClient(customer_id=%r, api_version=%r)" % (self.customer_id,
                                                                   self.api_version)

    # -- internals
    def _url(self, path):
        return "%s/%s/%s" % (BASE, self.api_version, path)

    def _headers(self):
        h = {"Authorization": "Bearer " + str(self.token_provider.access_token()),
             "Content-Type": "application/json"}
        if self.developer_token:
            h["developer-token"] = str(self.developer_token)
        if self.login_customer_id:
            h["login-customer-id"] = self.login_customer_id
        return h

    def _post(self, path, body):
        r = self.http("POST", self._url(path), self._headers(), body)
        if not isinstance(r, dict):
            raise ToolError("unexpected response from %s (not a JSON object)" % path)
        return r

    # -- API
    def search(self, gaql):
        """Run GAQL, following nextPageToken, and return the flattened results list."""
        out, token = [], None
        path = "customers/%s/googleAds:search" % self.customer_id
        while True:
            body = {"query": str(gaql)}
            if token:
                body["pageToken"] = token
            r = self._post(path, body)
            results = r.get("results") or []
            if not isinstance(results, list):
                raise ToolError("unexpected search response: 'results' is not a list")
            out += results
            token = r.get("nextPageToken")
            if not token:
                return out

    def mutate(self, service, operations, validate_only, partial=False):
        """POST customers/{cid}/{service}:mutate."""
        service = str(service).strip().strip("/")
        if not service:
            raise ToolError("mutate needs a service name (e.g. campaigns)")
        ops = list(operations or [])
        body = {"operations": ops, "validateOnly": bool(validate_only)}
        if partial:
            body["partialFailure"] = True
        return self._post("customers/%s/%s:mutate" % (self.customer_id, service), body)


# ── reads (behaviour-identical to gads_ctl.py) ───────────────────────────────────────────────
def live_state(client, campaign_id, end_field="end_date_time"):
    """Live status/end/budget/spend/negatives for exactly one campaign."""
    try:
        cid = int(campaign_id)
    except (TypeError, ValueError):
        raise ToolError("invalid campaign_id: %r" % (campaign_id,))
    base = ("SELECT campaign.id, campaign.name, campaign.status, campaign." + str(end_field) + ", "
            "campaign.campaign_budget, campaign_budget.amount_micros, "
            "campaign_budget.explicitly_shared, metrics.cost_micros FROM campaign "
            "WHERE campaign.id = " + str(cid))
    life = client.search(base)
    if not life:
        raise ToolError("campaign %s not visible to this identity" % campaign_id)
    today = client.search(base + " AND segments.date DURING TODAY")
    row = life[0]
    endkey = _end_key(end_field)
    negs = client.search("SELECT campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
                         "FROM campaign_criterion WHERE campaign.id = " + str(cid) +
                         " AND campaign_criterion.negative = TRUE AND campaign_criterion.type = KEYWORD")
    return {
        "id": str(_g(row, "campaign", "id")),
        "name": _g(row, "campaign", "name"),
        "status": _g(row, "campaign", "status"),
        "end": (_g(row, "campaign", endkey) or _g(row, "campaign", end_field) or "")[:10] or None,
        "budget_rn": _g(row, "campaign", "campaignBudget"),
        "budget_usd": int(_g(row, "campaignBudget", "amountMicros", default=0)) / 1e6,
        "budget_shared": bool(_g(row, "campaignBudget", "explicitlyShared", default=False)),
        "spend_life": int(_g(row, "metrics", "costMicros", default=0)) / 1e6,
        "spend_today": sum(int(_g(r, "metrics", "costMicros", default=0)) for r in today) / 1e6,
        "negatives": sorted({(_g(n, "campaignCriterion", "keyword", "text", default="").lower(),
                              _g(n, "campaignCriterion", "keyword", "matchType", default=""))
                             for n in negs}),
    }


def search_terms(client, campaign_id, days, today=None):
    """Clicked search terms over the last `days` days (inclusive of `today`)."""
    try:
        cid = int(campaign_id)
    except (TypeError, ValueError):
        raise ToolError("invalid campaign_id: %r" % (campaign_id,))
    if today is None:
        day = dt.date.today()
    elif isinstance(today, dt.datetime):
        day = today.date()
    elif isinstance(today, dt.date):
        day = today
    else:
        try:
            day = dt.date.fromisoformat(str(today))
        except ValueError:
            raise ToolError("invalid today: %r" % (today,))
    try:
        days_n = int(days)
    except (TypeError, ValueError):
        raise ToolError("invalid days: %r" % (days,))
    since = (day - dt.timedelta(days=days_n)).isoformat()
    rows = client.search("SELECT search_term_view.search_term, segments.keyword.info.text, metrics.clicks, "
                         "metrics.impressions, metrics.cost_micros FROM search_term_view WHERE campaign.id = "
                         "%d AND segments.date BETWEEN '%s' AND '%s' "
                         "ORDER BY metrics.clicks DESC" % (cid, since, day.isoformat()))
    return [{"term": _g(r, "searchTermView", "searchTerm"),
             "matched": _g(r, "segments", "keyword", "info", "text"),
             "clicks": int(_g(r, "metrics", "clicks", default=0)),
             "cost": int(_g(r, "metrics", "costMicros", default=0)) / 1e6} for r in rows]


# ── selftest (no network, no credentials) ────────────────────────────────────────────────────
def selftest():
    import io

    n = 0

    def check(cond, label):
        nonlocal n
        assert cond, "google_ads.selftest: " + label
        n += 1

    def raises(fn, needle=None):
        try:
            fn()
        except ToolError as e:
            if needle is not None:
                assert needle in str(e), "expected %r in %r" % (needle, str(e))
            return str(e)
        raise AssertionError("expected a ToolError")

    class Tok:
        def __init__(self, token="tok-A"):
            self.token, self.calls = token, 0

        def access_token(self):
            self.calls += 1
            return self.token

    class Fake:
        def __init__(self, responder=None):
            self.calls = []
            self.responder = responder or (lambda m, u, h, b: {})

        def __call__(self, method, url, headers, body):
            self.calls.append({"method": method, "url": url, "headers": dict(headers),
                               "body": json.loads(json.dumps(body))})
            return self.responder(method, url, headers, body)

    # 1. normalisation + headers
    f = Fake()
    tok = Tok("tok-A")
    c = GoogleAdsClient("123-456-7890", tok, http=f)
    check(c.customer_id == "1234567890", "customer_id dashes stripped")
    c.search("SELECT campaign.id FROM campaign")
    call = f.calls[-1]
    check(call["url"] == BASE + "/v24/customers/1234567890/googleAds:search", "search url")
    check(call["headers"]["Authorization"] == "Bearer tok-A", "bearer header")
    check(call["headers"]["Content-Type"] == "application/json", "content-type header")
    check("developer-token" not in call["headers"], "no developer-token when unset")
    check("login-customer-id" not in call["headers"], "no login-customer-id when unset")
    check(tok.calls >= 1, "token provider consulted for the header")
    check("tok-A" not in repr(c) and "tok-A" not in str(c.developer_token), "repr hides secrets")
    raises(lambda: GoogleAdsClient("not-a-cid", Tok(), http=f), needle="invalid customer_id")
    raises(lambda: GoogleAdsClient("111", None, http=f), needle="token provider")

    # 2. optional headers + mutate body
    f = Fake(lambda m, u, h, b: {"results": [{}] * len(b.get("operations", []))})
    c2 = GoogleAdsClient("999", Tok("tok-B"), http=f, api_version="v21",
                         developer_token="dev-123", login_customer_id="111-222-3333")
    c2.mutate("campaigns", [{"update": {"resourceName": "x"}}], validate_only=True)
    call = f.calls[-1]
    check(call["headers"]["developer-token"] == "dev-123", "developer-token header")
    check(call["headers"]["login-customer-id"] == "1112223333", "login-customer-id header")
    check(call["url"] == BASE + "/v21/customers/999/campaigns:mutate", "mutate url + api version")
    check(call["body"] == {"operations": [{"update": {"resourceName": "x"}}],
                          "validateOnly": True}, "mutate body, no partialFailure by default")
    c2.mutate("campaignCriteria", [], validate_only=False, partial=True)
    body = f.calls[-1]["body"]
    check(body["partialFailure"] is True and body["validateOnly"] is False,
          "mutate body with partialFailure")
    raises(lambda: c2.mutate("", [], False), needle="service name")

    # 3. search pagination over two pages
    def paged(method, url, headers, body):
        if body.get("pageToken") == "p2":
            return {"results": [{"campaign": {"id": "2"}}]}
        return {"results": [{"campaign": {"id": "1"}}], "nextPageToken": "p2"}

    f = Fake(paged)
    rows = GoogleAdsClient("1", Tok(), http=f).search("SELECT campaign.id FROM campaign")
    check([r["campaign"]["id"] for r in rows] == ["1", "2"], "pagination follows nextPageToken")
    check(len(f.calls) == 2 and "pageToken" not in f.calls[0]["body"], "first page has no pageToken")
    check(f.calls[1]["body"]["pageToken"] == "p2", "second page carries pageToken")

    # 4. transport failures propagate as ToolError
    def boom(method, url, headers, body):
        raise ToolError("HTTP 400 on /v24/customers/1/googleAds:search: bad")

    raises(lambda: GoogleAdsClient("1", Tok(), http=Fake(boom)).search("x"), needle="HTTP 400")
    raises(lambda: GoogleAdsClient("1", Tok(), http=Fake(lambda *a: ["not", "a", "dict"]))
           .search("x"), needle="not a JSON object")

    # 5. default urllib transport: HTTP error body is scrubbed
    real = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        body = b'{"error": {"message": "bad token ya29.SECRET-TOKEN-VALUE"}}'
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    urllib.request.urlopen = fake_urlopen
    try:
        msg = raises(lambda: urllib_http("POST", BASE + "/v24/customers/1/googleAds:search",
                                         {"Authorization": "Bearer tok-A"}, {}), needle="HTTP 403")
        check("ya29.SECRET-TOKEN-VALUE" not in msg, "HTTP error body scrubbed")
        check("/v24/customers/1/googleAds:search" in msg, "HTTP error names the path")
        check("Bearer" not in msg, "HTTP error leaks no authorization header")
    finally:
        urllib.request.urlopen = real
    # 6. live_state on camelCase fixture rows
    row = {"campaign": {"id": "9876543210", "name": "acme_search", "status": "ENABLED",
                        "endDateTime": "2026-10-14 23:59:59",
                        "campaignBudget": "customers/1234567890/campaignBudgets/1"},
           "campaignBudget": {"amountMicros": "25000000", "explicitlyShared": False},
           "metrics": {"costMicros": "40000000"}}

    def live_responder(method, url, headers, body):
        q = body["query"]
        if "FROM campaign_criterion" in q:
            return {"results": [
                {"campaignCriterion": {"keyword": {"text": "Tenant", "matchType": "BROAD"}}},
                {"campaignCriterion": {"keyword": {"text": "jobs", "matchType": "PHRASE"}}}]}
        if "DURING TODAY" in q:
            return {"results": [{"metrics": {"costMicros": "5000000"}}]}
        return {"results": [row]}

    f = Fake(live_responder)
    live = live_state(GoogleAdsClient("123-456-7890", Tok(), http=f), 9876543210)
    check(sorted(live) == ["budget_rn", "budget_shared", "budget_usd", "end", "id", "name",
                           "negatives", "spend_life", "spend_today", "status"],
          "live_state keys")
    check(live["id"] == "9876543210" and live["name"] == "acme_search", "live_state identity")
    check(live["status"] == "ENABLED", "live_state status")
    check(live["end"] == "2026-10-14", "live_state end date truncated to 10 chars")
    check(live["budget_rn"] == "customers/1234567890/campaignBudgets/1", "live_state budget_rn")
    check(abs(live["budget_usd"] - 25.0) < 1e-9, "live_state budget_usd")
    check(live["budget_shared"] is False, "live_state budget_shared")
    check(abs(live["spend_life"] - 40.0) < 1e-9, "live_state spend_life")
    check(abs(live["spend_today"] - 5.0) < 1e-9, "live_state spend_today")
    check(live["negatives"] == [("jobs", "PHRASE"), ("tenant", "BROAD")],
          "live_state negatives lowercased, sorted, deduped")
    q0 = f.calls[0]["body"]["query"]
    check("campaign.end_date_time" in q0 and "campaign_budget.explicitly_shared" in q0
          and q0.endswith("WHERE campaign.id = 9876543210"), "live_state GAQL string")

    # snake_case tolerance (same _g semantics as gads_ctl)
    srow = {"campaign": {"id": 9876543210, "name": "x", "status": "PAUSED",
                         "end_date_time": "2026-12-01 00:00:00", "campaign_budget": "rn/1"},
            "campaign_budget": {"amount_micros": "1000000", "explicitly_shared": True},
            "metrics": {"cost_micros": "0"}}
    f = Fake(lambda m, u, h, b: {"results": []} if "campaign_criterion" in b["query"] or
             "DURING TODAY" in b["query"] else {"results": [srow]})
    live2 = live_state(GoogleAdsClient("1234567890", Tok(), http=f), 9876543210)
    check(live2["end"] == "2026-12-01" and live2["budget_rn"] == "rn/1", "snake_case fields read")
    check(live2["budget_usd"] == 1.0 and live2["budget_shared"] is True, "snake_case budget")
    check(live2["negatives"] == [] and live2["spend_today"] == 0.0, "empty today/negatives")
    raises(lambda: live_state(GoogleAdsClient("1", Tok(), http=Fake()), 42),
           needle="not visible")
    raises(lambda: live_state(GoogleAdsClient("1", Tok(), http=Fake()), "abc"),
           needle="invalid campaign_id")

    # 7. search_terms with an injected today
    f = Fake(lambda m, u, h, b: {"results": [
        {"searchTermView": {"searchTerm": "property manager tampa"},
         "segments": {"keyword": {"info": {"text": "property manager tampa"}}},
         "metrics": {"clicks": "3", "costMicros": "9000000"}}]})
    terms = search_terms(GoogleAdsClient("1234567890", Tok(), http=f), 9876543210, 7,
                         today=dt.date(2026, 10, 12))
    check(terms == [{"term": "property manager tampa", "matched": "property manager tampa",
                     "clicks": 3, "cost": 9.0}], "search_terms parsed")
    q = f.calls[0]["body"]["query"]
    check("segments.date BETWEEN '2026-10-05' AND '2026-10-12'" in q, "search_terms BETWEEN clause")
    check("FROM search_term_view WHERE campaign.id = 9876543210" in q, "search_terms GAQL clause")
    check(q.endswith("ORDER BY metrics.clicks DESC"), "search_terms ordering")
    check("segments.keyword.info.text" in q and "metrics.impressions" in q, "search_terms fields")
    return n
