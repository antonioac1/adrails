"""Microsoft Advertising (Bing Ads) READ-ONLY client over the v13 JSON REST API, stdlib only.

Why read-only: this adapter exists so an agent can watch delivery and stats on Microsoft Ads in detail without being able
to spend a cent. There is deliberately no create/update/delete method here; a monitoring agent can be handed this
safely. (Changes stay a human or reviewed-desired-state job.)

Endpoints (verified live 2026-11-16, Bearer token + DeveloperToken header):
  CustomerManagement  https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13/  User/Query, Accounts/Search
  CampaignManagement  https://campaign.api.bingads.microsoft.com/CampaignManagement/v13/      Campaigns/QueryByAccountId,
                      AdGroups/QueryByCampaignId, Keywords/QueryByAdGroupId, Ads/QueryByAdGroupId, NegativeKeywords/QueryByEntityIds
  Reporting           https://reporting.api.bingads.microsoft.com/Reporting/v13/             GenerateReport/Submit, GenerateReport/Poll
  REST returns 64-bit ids as STRINGS; this client keeps them as strings.

Sign-in (one time, then unattended): an OAuth refresh token for a user who can see the account.
  provider "microsoft": Microsoft identity platform, scope msads.manage. Refresh tokens ROTATE on every use, so the new
                        one is written back to refresh_token_file each time (any use within 90 days keeps it alive).
  provider "google":    for Microsoft Ads logins that use "Sign in with Google". Google OAuth, scopes openid email profile
                        (Microsoft's own SDK does the same). Google refresh tokens do not rotate; publish the Google OAuth
                        app ("In production") or Google expires them after 7 days.
Secrets (developer token, client secret, refresh token) never appear in a return value, exception or log line.
No network at import time; `http=` and `sleep=` are injectable for tests.
"""
import csv
import io
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from adsops.errors import ToolError, scrub

CUSTOMER = "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13/"
CAMPAIGN = "https://campaign.api.bingads.microsoft.com/CampaignManagement/v13/"
REPORTING = "https://reporting.api.bingads.microsoft.com/Reporting/v13/"
MS_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_DEVICE = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
MS_SCOPE = "https://ads.microsoft.com/msads.manage offline_access"
G_TOKEN = "https://oauth2.googleapis.com/token"
G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
G_SCOPE = "openid email profile"
RETRY_STATUS = {429, 500, 502, 503, 504}
_ID = re.compile(r"^\d{1,20}$")
PROVIDERS = ("microsoft", "google")

REPORT_COLUMNS = {
    "campaign": ("CampaignPerformanceReportRequest", ["TimePeriod", "AccountName", "CampaignId", "CampaignName", "CampaignStatus",
                                                       "Impressions", "Clicks", "Spend", "Conversions", "Revenue"]),
    "ad_group": ("AdGroupPerformanceReportRequest", ["TimePeriod", "CampaignName", "AdGroupId", "AdGroupName", "Status",
                                                      "Impressions", "Clicks", "Spend", "Conversions"]),
    "keyword": ("KeywordPerformanceReportRequest", ["TimePeriod", "CampaignName", "AdGroupName", "KeywordId", "Keyword",
                                                     "BidMatchType", "Impressions", "Clicks", "Spend", "Conversions"]),
    "search_term": ("SearchQueryPerformanceReportRequest", ["TimePeriod", "CampaignName", "AdGroupName", "SearchQuery", "Keyword",
                                                            "Impressions", "Clicks", "Spend", "Conversions"]),
}


# ── secrets ──────────────────────────────────────────────────────────────────────────────────
def redact(text, secrets=(), limit=1500):
    s = str(text)
    for sec in secrets:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "[REDACTED]")
    s = re.sub(r"(?i)(\"?(access_token|refresh_token|client_secret|developertoken|authorization)\"?\s*[:=]\s*\"?)[^\s\",}]+",
               r"\1[REDACTED]", s)
    return scrub(s, limit)


def secret_loader(src, what, runner=subprocess.run):
    """{"kind": "file", "path"} | {"kind": "env", "name"} | {"kind": "1password", "ref", "token_file"} | {"kind": "value", "value"}
    ("value" is only for non-secrets such as a public OAuth client id)."""
    if not isinstance(src, dict):
        raise ToolError(f"microsoft_ads: {what} must be a source object {{\"kind\": ...}}")
    kind = src.get("kind")

    cache = {}

    def load():
        if "v" in cache:                       # read once per process (1Password lookups are slow subprocesses)
            return cache["v"]
        cache["v"] = _load()
        return cache["v"]

    def _load():
        if kind == "file":
            p = os.path.expanduser(str(src.get("path") or ""))
            try:
                v = open(p).read().strip()
            except OSError as e:
                raise ToolError(f"microsoft_ads: {what} file unreadable ({type(e).__name__})") from None
        elif kind == "env":
            v = (os.environ.get(str(src.get("name") or "")) or "").strip()
        elif kind == "1password":
            env = dict(os.environ)
            r, last = None, None
            for attempt in range(2):             # a cold first `op` in a freshly spawned MCP server can stall; one retry
                try:
                    env["OP_SERVICE_ACCOUNT_TOKEN"] = open(os.path.expanduser(src["token_file"])).read().strip()
                    # stdin=DEVNULL: an MCP server's stdin is the JSON-RPC pipe; a child must never read from it
                    r = runner([src.get("op_bin", "op"), "read", src["ref"]], capture_output=True, text=True, timeout=25, env=env,
                               stdin=subprocess.DEVNULL)
                    break
                except subprocess.TimeoutExpired as e:
                    last = e
                except (OSError, KeyError, subprocess.SubprocessError) as e:
                    raise ToolError(f"microsoft_ads: {what} 1password read failed ({type(e).__name__})") from None
            if r is None:
                raise ToolError(f"microsoft_ads: {what} 1password read timed out twice ({type(last).__name__})")
            if r.returncode != 0:
                raise ToolError(f"microsoft_ads: {what} 1password read failed (rc={r.returncode})")
            v = r.stdout.strip()
        elif kind == "value":
            v = str(src.get("value") or "").strip()
        else:
            raise ToolError(f"microsoft_ads: {what}.kind must be file|env|1password|value")
        if not v:
            raise ToolError(f"microsoft_ads: {what} is empty")
        return v
    return load


# ── transport ────────────────────────────────────────────────────────────────────────────────
def urllib_http(method, url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _net_reason(e, url):
    reason = str(getattr(e, "reason", e))
    host = urllib.parse.urlparse(url).hostname
    if "nodename" in reason or "Name or service" in reason:
        return f"DNS lookup failed for {host}"
    return f"{type(e).__name__} talking to {host}: {scrub(reason, 120)}"


def form(http, url, data):
    try:
        st, raw = http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(data).encode())
    except (OSError, urllib.error.URLError) as e:            # timeouts are OSError too: never escape as a non-ToolError
        raise ToolError("microsoft_ads: sign-in server unreachable: " + _net_reason(e, url)) from None
    try:
        return st, json.loads(raw or b"{}")
    except ValueError:
        return st, {"error": "non_json", "error_description": raw[:200].decode("utf-8", "replace")}


class Tokens:
    """Access tokens from a stored refresh token. Microsoft rotates refresh tokens: write the new one back atomically."""

    def __init__(self, provider, client_id, refresh_token_file, client_secret=None, http=None, now=time.time):
        if provider not in PROVIDERS:
            raise ToolError("microsoft_ads: auth.provider must be microsoft or google")
        if provider == "google" and client_secret is None:
            raise ToolError("microsoft_ads: auth.provider google needs auth.client_secret")
        self.provider, self.client_id, self.client_secret = provider, client_id, client_secret
        self.rt_file = os.path.expanduser(refresh_token_file)
        self.http, self.now = http or urllib_http, now
        self._tok, self._exp = None, 0

    def secrets(self):
        out = []
        for f in (self.client_secret,):
            try:
                out.append(f() if callable(f) else f)
            except ToolError:
                pass
        try:
            out.append(open(self.rt_file).read().strip())
        except OSError:
            pass
        return [x for x in out + [self._tok] if x]

    def save(self, rt):
        import tempfile
        d = os.path.dirname(self.rt_file) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".msads-rt-", dir=d)   # unique per writer: a cron pull and the server can't interleave
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(rt)
        os.replace(tmp, self.rt_file)

    def access_token(self):
        if self._tok and self.now() < self._exp - 120:
            return self._tok
        try:
            rt = open(self.rt_file).read().strip()
        except OSError:
            raise ToolError("microsoft_ads: no refresh token yet. Run `adrails msads-auth` once (sign in as a user "
                            "who can see the Microsoft Ads account).") from None
        if self.provider == "google":
            data = {"client_id": self.client_id, "client_secret": self.client_secret(), "grant_type": "refresh_token",
                    "refresh_token": rt}
            st, r = form(self.http, G_TOKEN, data)
        else:
            st, r = form(self.http, MS_TOKEN, {"client_id": self.client_id, "grant_type": "refresh_token",
                                                "refresh_token": rt, "scope": MS_SCOPE})
        if "access_token" not in r:
            hint = (" (Google: if this started ~7 days after sign-in, the Google OAuth app is still in Testing; publish it)"
                    if self.provider == "google" else "")
            raise ToolError(redact(f"microsoft_ads: token refresh failed: {r.get('error')} "
                                   f"{(r.get('error_description') or '')[:200]}{hint}", self.secrets()))
        if r.get("refresh_token") and r["refresh_token"] != rt:
            self.save(r["refresh_token"])
        self._tok, self._exp = r["access_token"], self.now() + int(r.get("expires_in") or 3000)
        return self._tok


def _sid(v, what="id"):
    s = str(v).strip()
    if not _ID.match(s):
        raise ToolError(f"microsoft_ads: {what} must be a numeric id")
    return s


class MicrosoftAdsClient:
    """Read-only. Every method is a query; there is no write path in this class."""

    def __init__(self, tokens, developer_token, http=None, sleep=time.sleep, max_attempts=4):
        self.tokens, self.dev = tokens, developer_token
        self.http, self.sleep, self.max_attempts = http or urllib_http, sleep, max_attempts

    def _secrets(self):
        try:
            d = self.dev() if callable(self.dev) else self.dev
        except ToolError:
            d = None
        return [x for x in [d] + self.tokens.secrets() if x]

    def post(self, base, path, body, account_id=None, customer_id=None):
        hdr = {"Authorization": "Bearer " + self.tokens.access_token(),
               "DeveloperToken": self.dev() if callable(self.dev) else self.dev, "Content-Type": "application/json"}
        if account_id:
            hdr["CustomerAccountId"] = _sid(account_id, "account_id")
        if customer_id:
            hdr["CustomerId"] = _sid(customer_id, "customer_id")
        data = json.dumps(body).encode()
        last = None
        for attempt in range(self.max_attempts):
            try:
                st, raw = self.http("POST", base + path, hdr, data)
            except (OSError, urllib.error.URLError) as e:
                st, raw, last = None, b"", _net_reason(e, base)
            if st == 200:
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    raise ToolError(f"microsoft_ads: {path} returned non-JSON") from None
            if st is not None and st not in RETRY_STATUS:
                raise ToolError(redact(f"microsoft_ads: {path} HTTP {st}: {self._error_text(raw)}", self._secrets()))
            last = last or f"HTTP {st}"
            if attempt + 1 < self.max_attempts:
                self.sleep(min(30, 2 ** attempt))
        raise ToolError(f"microsoft_ads: {path} failed after {self.max_attempts} attempts ({last})")

    @staticmethod
    def _error_text(raw):
        try:
            j = json.loads(raw or b"{}")
            errs = j.get("OperationErrors") or j.get("BatchErrors") or []
            if errs:
                return "; ".join(f"{e.get('ErrorCode')}: {e.get('Message')}" for e in errs[:3])
            return str(j.get("Message") or j)[:300]
        except ValueError:
            return (raw or b"")[:300].decode("utf-8", "replace")

    # ── account discovery ──
    def user(self):
        return self.post(CUSTOMER, "User/Query", {"UserId": None}).get("User") or {}

    def accounts(self):
        uid = self.user().get("Id")
        r = self.post(CUSTOMER, "Accounts/Search", {"Predicates": [{"Field": "UserId", "Operator": "Equals", "Value": str(uid)}],
                                                    "Ordering": None, "PageInfo": {"Index": 0, "Size": 1000}})
        return [{"account_id": str(a.get("Id")), "number": a.get("Number"), "name": a.get("Name"),
                 "customer_id": str(a.get("ParentCustomerId")), "status": a.get("AccountLifeCycleStatus"),
                 "currency": a.get("CurrencyCode"), "pause_reason": a.get("PauseReason")} for a in r.get("Accounts") or []]

    # ── structure ──
    def campaigns(self, account_id, customer_id):
        out = []
        for ctype in ("Search", "Shopping", "Audience", "PerformanceMax"):
            try:
                r = self.post(CAMPAIGN, "Campaigns/QueryByAccountId", {"AccountId": _sid(account_id), "CampaignType": ctype},
                              account_id, customer_id)
            except ToolError:
                if ctype == "Search":
                    raise
                continue                      # an account without that campaign type is not an error
            for c in r.get("Campaigns") or []:
                b = c.get("BiddingScheme") or {}
                out.append({"campaign_id": str(c.get("Id")), "name": c.get("Name"), "type": c.get("CampaignType") or ctype,
                            "status": c.get("Status"), "daily_budget": c.get("DailyBudget"), "budget_type": c.get("BudgetType"),
                            "bid_strategy": b.get("Type"), "max_cpc": (b.get("MaxCpc") or {}).get("Amount"),
                            "final_url_suffix": c.get("FinalUrlSuffix"), "time_zone": c.get("TimeZone")})
        return out

    def ad_groups(self, campaign_id, account_id, customer_id):
        r = self.post(CAMPAIGN, "AdGroups/QueryByCampaignId", {"CampaignId": _sid(campaign_id)}, account_id, customer_id)
        # "type" = v13 AdGroup.AdGroupType (SearchStandard | SearchDynamic | ...): health needs it to tell a dynamic search
        # ad group (no keywords by design) from a keyword-targeted one with an empty keyword list (cannot serve).
        return [{"ad_group_id": str(g.get("Id")), "name": g.get("Name"), "status": g.get("Status"), "network": g.get("Network"),
                 "type": g.get("AdGroupType"), "cpc_bid": (g.get("CpcBid") or {}).get("Amount")} for g in r.get("AdGroups") or []]

    def keywords(self, ad_group_id, account_id, customer_id):
        r = self.post(CAMPAIGN, "Keywords/QueryByAdGroupId", {"AdGroupId": _sid(ad_group_id)}, account_id, customer_id)
        return [{"keyword_id": str(k.get("Id")), "text": k.get("Text"), "match_type": k.get("MatchType"), "status": k.get("Status"),
                 "editorial_status": k.get("EditorialStatus")} for k in r.get("Keywords") or []]

    def ads(self, ad_group_id, account_id, customer_id):
        # DynamicSearch included so a dynamic search ad group's ads are seen; without it health reported "no serving ad".
        r = self.post(CAMPAIGN, "Ads/QueryByAdGroupId", {"AdGroupId": _sid(ad_group_id),
                                                         "AdTypes": ["ResponsiveSearch", "ExpandedText", "DynamicSearch"]}, account_id, customer_id)
        out = []
        for a in r.get("Ads") or []:
            heads = [h.get("Asset", {}).get("Text") for h in ((a.get("Headlines") or []) if isinstance(a.get("Headlines"), list) else [])]
            out.append({"ad_id": str(a.get("Id")), "type": a.get("Type"), "status": a.get("Status"),
                        "editorial_status": a.get("EditorialStatus"), "final_urls": a.get("FinalUrls") or [],
                        "headlines": [h for h in heads if h]})
        return out

    def campaign_negatives(self, campaign_id, account_id, customer_id):
        r = self.post(CAMPAIGN, "NegativeKeywords/QueryByEntityIds", {"EntityIds": [_sid(campaign_id)], "EntityType": "Campaign",
                                                                      "ParentEntityId": _sid(account_id)}, account_id, customer_id)
        ents = r.get("EntityNegativeKeywords") or []
        return [{"text": n.get("Text"), "match_type": n.get("MatchType")} for e in ents for n in (e.get("NegativeKeywords") or [])]

    # ── reporting ──
    def report(self, level, account_id, customer_id, day_from, day_to, campaign_ids=None, poll_s=3, max_polls=60, time_zone=None):
        """Rows as dicts (CSV header names). Microsoft returns NO file when there is no data: that is [] (zero), not an error."""
        if level not in REPORT_COLUMNS:
            raise ToolError(f"microsoft_ads: level must be one of {', '.join(REPORT_COLUMNS)}")
        rtype, cols = REPORT_COLUMNS[level]

        def d(s):
            y, m, dd = (int(x) for x in str(s).split("-"))
            return {"Year": y, "Month": m, "Day": dd}
        scope = {"AccountIds": [int(_sid(account_id))]}
        if campaign_ids:
            scope = {"Campaigns": [{"AccountId": int(_sid(account_id)), "CampaignId": int(_sid(c))} for c in campaign_ids]}
        req = {"ReportRequest": {"Type": rtype, "ExcludeColumnHeaders": False, "ExcludeReportFooter": True,
                                 "ExcludeReportHeader": True, "Format": "Csv", "ReturnOnlyCompleteData": False,
                                 "ReportName": "adrails-" + level, "Aggregation": "Daily", "Columns": cols, "Scope": scope,
                                 "Time": {"CustomDateRangeStart": d(day_from), "CustomDateRangeEnd": d(day_to)}}}
        if time_zone:                          # the account's own day boundaries (campaign TimeZone), like Google's account-local days
            req["ReportRequest"]["Time"]["ReportTimeZone"] = time_zone
        rid = self.post(REPORTING, "GenerateReport/Submit", req, account_id, customer_id).get("ReportRequestId")
        if not rid:
            raise ToolError("microsoft_ads: report submit returned no id")
        for _ in range(max_polls):
            st = (self.post(REPORTING, "GenerateReport/Poll", {"ReportRequestId": rid}, account_id, customer_id)
                  .get("ReportRequestStatus") or {})
            if st.get("Status") == "Success":
                url = st.get("ReportDownloadUrl")
                if not url:
                    return []
                try:
                    code, raw = self.http("GET", url, {}, None)
                except (OSError, urllib.error.URLError) as e:
                    raise ToolError("microsoft_ads: report download failed: " + _net_reason(e, url)) from None
                if code != 200:
                    raise ToolError(f"microsoft_ads: report download HTTP {code}")
                return parse_report(raw)
            if st.get("Status") == "Error":
                raise ToolError("microsoft_ads: report generation failed")
            self.sleep(poll_s)
        raise ToolError("microsoft_ads: report still pending; try again in a minute")


def _num(v):
    """A report cell -> float, or None when it is not a number. Microsoft writes "--" (and sometimes "N/A" or an empty
    cell) where a value is not available; reading those as 0.0 fabricated zero spend / zero conversions, so a non-numeric
    placeholder is None = UNKNOWN and every consumer must treat it that way (see totals, msads_tools)."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    try:
        f = float(s)
    except ValueError:                                  # "", "--", "N/A", garbage -> unknown, never zero
        return None
    return f if math.isfinite(f) else None              # "nan"/"inf" parse as floats but are not values either


def add_known(a, b):
    """None-sticky addition: unknown + anything = unknown. Used wherever report values are summed, so one unknown cell
    makes the total unknown instead of a silent partial sum that reads as the real number."""
    return None if a is None or b is None else a + b


def parse_report(raw):
    """Zip (or bare) CSV -> list of dicts with numeric metrics converted."""
    data = raw
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        data = z.read(z.namelist()[0])
    text = data.decode("utf-8-sig", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if not r or not any(r.values()):
            continue
        for k in ("Impressions", "Clicks", "Spend", "Conversions", "Revenue"):
            if k in r:
                r[k] = _num(r[k])
        rows.append(r)
    return rows


def totals(rows):
    """Window totals + ratios. A metric with ANY unknown (None) cell totals to None and is listed under "incomplete";
    a ratio with an unknown input is None. (An absent column still counts as 0: that level simply has no such column.)
    No rows at all = no traffic = zeros, as documented on adsops_msads_performance."""
    t, unknown = {}, []
    for k in ("Impressions", "Clicks", "Spend", "Conversions"):
        tot = 0.0
        for r in rows:
            tot = add_known(tot, r.get(k, 0))
        t[k] = None if tot is None else round(tot, 2)
        if tot is None:
            unknown.append(k)
    t["CTR"] = round(t["Clicks"] / t["Impressions"], 4) if t["Impressions"] and t["Clicks"] is not None else None
    t["AvgCPC"] = round(t["Spend"] / t["Clicks"], 2) if t["Clicks"] and t["Spend"] is not None else None
    t["CostPerConversion"] = round(t["Spend"] / t["Conversions"], 2) if t["Conversions"] and t["Spend"] is not None else None
    if unknown:
        t["incomplete"] = unknown
    return t


# ── one-time sign-in (CLI: adrails msads-auth) ───────────────────────────────────────────────
def device_code_signin(tokens, out=print, http=None, sleep=time.sleep, now=time.time):
    """provider microsoft: device code. The user opens the printed URL on any device and enters the code."""
    http = http or tokens.http
    st, d = form(http, MS_DEVICE, {"client_id": tokens.client_id, "scope": MS_SCOPE})
    if "device_code" not in d:
        raise ToolError(f"microsoft_ads: device code refused: {d.get('error')} {(d.get('error_description') or '')[:200]}")
    out(f"Open {d['verification_uri']} and enter the code {d['user_code']} (expires in {int(d['expires_in']) // 60} min).")
    end, wait = now() + int(d["expires_in"]), int(d.get("interval") or 5)
    while now() < end:
        sleep(wait)
        st, t = form(http, MS_TOKEN, {"client_id": tokens.client_id, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                      "device_code": d["device_code"]})
        if t.get("refresh_token"):
            tokens.save(t["refresh_token"])
            return True
        if t.get("error") == "slow_down":
            wait += 5
        elif t.get("error") != "authorization_pending":
            raise ToolError(f"microsoft_ads: sign-in failed: {t.get('error')}")
    raise ToolError("microsoft_ads: the code expired before sign-in")


def google_auth_url(client_id, redirect_uri, state):
    return G_AUTH + "?" + urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
                                                  "scope": G_SCOPE, "access_type": "offline", "prompt": "consent",
                                                  "state": state})


def google_signin(tokens, redirect_uri, out=print, wait_for_code=None, http=None):
    """provider google: browser sign-in with a one-shot listener on the loopback redirect (127.0.0.1 only)."""
    import secrets as _s
    state = _s.token_urlsafe(16)
    u = urllib.parse.urlparse(redirect_uri)
    if u.hostname not in ("localhost", "127.0.0.1") or not u.port:
        raise ToolError("microsoft_ads: google redirect_uri must be http://localhost:<port>/<path> (registered on your Google client)")
    out("Open this link in a browser signed in to the Google account you use for Microsoft Ads, then click Allow:")
    out(google_auth_url(tokens.client_id, redirect_uri, state))
    code = (wait_for_code or _loopback_code)(u.port, u.path or "/", state)
    st, t = form(http or tokens.http, G_TOKEN, {"client_id": tokens.client_id, "client_secret": tokens.client_secret(), "code": code,
                                                "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    if not t.get("refresh_token"):
        raise ToolError(f"microsoft_ads: Google returned no refresh token: {t.get('error')}")
    tokens.save(t["refresh_token"])
    return True


def _loopback_code(port, path, state, timeout=900):
    import http.server
    import socketserver
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        timeout = 10                           # a browser preconnect that never sends cannot block the listener

        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(p.query)
            if p.path == path and q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items() if k in ("code", "error")})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab." if got.get("code") else b"Waiting for sign-in.")

        def log_message(self, *a):
            pass
    socketserver.TCPServer.allow_reuse_address = True
    try:
        srv = socketserver.TCPServer(("127.0.0.1", port), H)
    except OSError as e:
        raise ToolError(f"microsoft_ads: cannot listen on 127.0.0.1:{port} ({e.strerror or e}); free the port or change "
                        "redirect_uri (and the Google client) to another localhost port") from None
    with srv:
        srv.timeout = 5
        end = time.time() + timeout
        while time.time() < end and not got:
            srv.handle_request()
    if got.get("error") or not got.get("code"):
        raise ToolError(f"microsoft_ads: Google sign-in not completed ({got.get('error') or 'timeout'})")
    return got["code"]


# ── selftest ─────────────────────────────────────────────────────────────────────────────────
def selftest():
    import tempfile
    n = 0
    tmp = tempfile.mkdtemp()
    rt = os.path.join(tmp, "rt")

    # rotating Microsoft refresh token is written back 0600; secrets never leak into errors
    open(rt, "w").write("RT1")
    calls = []

    def ms_http(method, url, headers, body):
        calls.append((url, body))
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "AT1", "refresh_token": "RT2", "expires_in": 3600}).encode()
        return 500, b""
    tk = Tokens("microsoft", "cid", rt, http=ms_http)
    assert tk.access_token() == "AT1"; n += 1
    assert open(rt).read() == "RT2" and oct(os.stat(rt).st_mode & 0o777) == "0o600"; n += 1
    assert tk.access_token() == "AT1" and len(calls) == 1; n += 1                       # cached until near expiry

    # google: client secret required, refresh token kept, testing-mode hint on invalid_grant
    try:
        Tokens("google", "gid", rt); raise AssertionError
    except ToolError:
        n += 1
    open(rt, "w").write("GRT")

    def g_bad(method, url, headers, body):
        return 400, json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}).encode()
    try:
        Tokens("google", "gid", rt, client_secret=lambda: "GSECRET-123456", http=g_bad).access_token(); raise AssertionError
    except ToolError as e:
        assert "Testing" in str(e) and "GSECRET" not in str(e) and "GRT" not in str(e); n += 1

    # client: REST shape (verified live), string ids, headers, retry, non-retry error is redacted
    seen = []

    def api(method, url, headers, body):
        seen.append((url, headers, body))
        if url.endswith("oauth2/v2.0/token"):
            return 200, json.dumps({"access_token": "AT9", "expires_in": 3600}).encode()
        if url.endswith("User/Query"):
            return 200, json.dumps({"User": {"Id": "42", "UserName": "u@x"}}).encode()
        if url.endswith("Accounts/Search"):
            return 200, json.dumps({"Accounts": [{"Id": "187", "Number": "G1", "Name": "Acme", "ParentCustomerId": "255",
                                                  "AccountLifeCycleStatus": "Active", "CurrencyCode": "USD"}]}).encode()
        if url.endswith("Campaigns/QueryByAccountId"):
            if json.loads(body)["CampaignType"] != "Search":
                return 400, json.dumps({"OperationErrors": [{"ErrorCode": "X", "Message": "no such type"}]}).encode()
            return 200, json.dumps({"Campaigns": [{"Id": "525", "Name": "C", "Status": "Active", "DailyBudget": 20,
                                                   "BiddingScheme": {"Type": "MaxClicks", "MaxCpc": {"Amount": 8}}}]}).encode()
        if url.endswith("AdGroups/QueryByCampaignId"):
            return 200, json.dumps({"AdGroups": [{"Id": "1", "Name": "g", "Status": "Paused", "Network": "OwnedAndOperatedOnly"}]}).encode()
        if url.endswith("NegativeKeywords/QueryByEntityIds"):
            return 200, json.dumps({"EntityNegativeKeywords": [{"NegativeKeywords": [{"Text": "jobs", "MatchType": "Phrase"}]}]}).encode()
        if url.endswith("Keywords/QueryByAdGroupId"):
            return 503, b""
        return 404, b"{}"
    open(rt, "w").write("RTX")
    sleeps = []
    cl = MicrosoftAdsClient(Tokens("microsoft", "cid", rt, http=api), lambda: "DEVTOKEN-SECRET", http=api, sleep=sleeps.append)
    acc = cl.accounts()
    assert acc == [{"account_id": "187", "number": "G1", "name": "Acme", "customer_id": "255", "status": "Active",
                    "currency": "USD", "pause_reason": None}]; n += 1
    h = [x for x in seen if x[0].endswith("Accounts/Search")][0][1]
    assert h["Authorization"] == "Bearer AT9" and h["DeveloperToken"] == "DEVTOKEN-SECRET"; n += 1
    cs = cl.campaigns("187", "255")
    assert cs[0]["campaign_id"] == "525" and cs[0]["bid_strategy"] == "MaxClicks" and cs[0]["max_cpc"] == 8; n += 1
    h = [x for x in seen if x[0].endswith("Campaigns/QueryByAccountId")][0][1]
    assert h["CustomerAccountId"] == "187" and h["CustomerId"] == "255"; n += 1
    assert cl.campaign_negatives("525", "187", "255") == [{"text": "jobs", "match_type": "Phrase"}]; n += 1
    try:
        cl.keywords("1", "187", "255"); raise AssertionError
    except ToolError as e:
        assert "after 4 attempts" in str(e) and sleeps == [1, 2, 4]; n += 1
    try:
        cl.ad_groups("1; DROP", "187", "255"); raise AssertionError
    except ToolError:
        n += 1                                                                            # ids validated, no path injection
    assert not any(hasattr(MicrosoftAdsClient, w) for w in ("create", "update", "delete", "set_status", "pause", "enable")); n += 1

    def err_http(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "AT9", "expires_in": 3600}).encode()
        return 401, b'{"Message": "bad DeveloperToken=DEVTOKEN-SECRET"}'
    try:
        MicrosoftAdsClient(Tokens("microsoft", "cid", rt, http=err_http), "DEVTOKEN-SECRET", http=err_http).user(); raise AssertionError
    except ToolError as e:
        assert "DEVTOKEN-SECRET" not in str(e) and "HTTP 401" in str(e); n += 1

    # reports: submit -> poll -> zip CSV; no-data success -> []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("r.csv", "﻿TimePeriod,CampaignName,Impressions,Clicks,Spend,Conversions\n2026-11-16,C,\"1,200\",30,95.50,2\n")
    polls = iter(["Pending", "Success"])

    def rep(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "AT9", "expires_in": 3600}).encode()
        if url.endswith("Submit"):
            b = json.loads(body)["ReportRequest"]
            assert b["Type"] == "CampaignPerformanceReportRequest" and b["Time"]["CustomDateRangeStart"] == {"Year": 2026, "Month": 11, "Day": 1}
            assert b["Time"].get("ReportTimeZone") == "MountainTimeUSCanada"
            return 200, b'{"ReportRequestId": "r1"}'
        if url.endswith("Poll"):
            s = next(polls)
            return 200, json.dumps({"ReportRequestStatus": {"Status": s, "ReportDownloadUrl": "https://dl/x.zip" if s == "Success" else None}}).encode()
        if url == "https://dl/x.zip":
            return 200, buf.getvalue()
        return 404, b""
    rows = MicrosoftAdsClient(Tokens("microsoft", "cid", rt, http=rep), "D" * 16, http=rep, sleep=lambda s: None).report(
        "campaign", "187", "255", "2026-11-01", "2026-11-16", time_zone="MountainTimeUSCanada")
    assert rows[0]["Impressions"] == 1200 and rows[0]["Spend"] == 95.5; n += 1
    t = totals(rows)
    assert t["CTR"] == 0.025 and t["AvgCPC"] == 3.18 and t["CostPerConversion"] == 47.75; n += 1

    # 0.14.4 regression: non-numeric placeholders are UNKNOWN (None), never a fabricated 0
    assert [_num(x) for x in ("--", "N/A", "", " ", "garbage", None, "nan", "inf")] == [None] * 8; n += 1
    assert _num("1,234.5") == 1234.5 and _num("12.3%") == 12.3 and _num("0") == 0.0 and _num(7) == 7.0 and _num("-2") == -2.0; n += 1
    dash = parse_report("TimePeriod,CampaignName,Impressions,Clicks,Spend,Conversions\n2026-11-15,C,--,3,N/A,\n2026-11-16,C,10,2,4.00,1\n".encode())
    assert dash[0]["Impressions"] is None and dash[0]["Spend"] is None and dash[0]["Conversions"] is None and dash[0]["Clicks"] == 3.0; n += 1
    t = totals(dash)                                       # one unknown cell -> unknown total, unknown ratios; known ones still summed
    assert t["Impressions"] is None and t["Spend"] is None and t["Conversions"] is None and t["Clicks"] == 5.0 and t["CTR"] is None \
        and t["AvgCPC"] is None and t["CostPerConversion"] is None and t["incomplete"] == ["Impressions", "Spend", "Conversions"], t; n += 1
    assert "incomplete" not in totals(rows) and totals([]) == {"Impressions": 0.0, "Clicks": 0.0, "Spend": 0.0, "Conversions": 0.0,
                                                                "CTR": None, "AvgCPC": None, "CostPerConversion": None}; n += 1

    # 0.14.4 regression: ad group type is read (health exempts dynamic search groups), and DSA ads are requested
    asked = []

    def types(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "AT9", "expires_in": 3600}).encode()
        asked.append((url, json.loads(body)))
        if url.endswith("AdGroups/QueryByCampaignId"):
            return 200, json.dumps({"AdGroups": [{"Id": "1", "Name": "g", "Status": "Active", "AdGroupType": "SearchDynamic"}]}).encode()
        return 200, json.dumps({"Ads": []}).encode()
    tc = MicrosoftAdsClient(Tokens("microsoft", "cid", rt, http=types), "D" * 16, http=types, sleep=lambda s: None)
    assert tc.ad_groups("525", "187", "255")[0]["type"] == "SearchDynamic"; n += 1
    tc.ads("1", "187", "255")
    assert "DynamicSearch" in asked[-1][1]["AdTypes"]; n += 1

    def nodata(method, url, headers, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "AT9", "expires_in": 3600}).encode()
        if url.endswith("Submit"):
            return 200, b'{"ReportRequestId": "r2"}'
        return 200, json.dumps({"ReportRequestStatus": {"Status": "Success", "ReportDownloadUrl": None}}).encode()
    assert MicrosoftAdsClient(Tokens("microsoft", "cid", rt, http=nodata), "D" * 16, http=nodata, sleep=lambda s: None).report(
        "search_term", "187", "255", "2026-11-01", "2026-11-02") == []; n += 1

    # network failures always surface as ToolError (never escape and abort a whole metrics pull)
    def down(method, url, headers, body):
        raise urllib.error.URLError(OSError(8, "nodename nor servname provided, or not known"))
    open(rt, "w").write("RTZ")
    try:
        Tokens("microsoft", "cid", rt, http=down).access_token(); raise AssertionError
    except ToolError as e:
        assert "DNS lookup failed for login.microsoftonline.com" in str(e); n += 1
    calls_n = []
    ld = secret_loader({"kind": "file", "path": rt}, "x")
    orig_open = open
    assert ld() == ld() == "RTZ"; n += 1                                               # cached

    # 1password: stdin never inherited, one retry after a stall
    tok = os.path.join(tmp, "optok"); open(tok, "w").write("sa")
    seen_kw, tries = [], []

    def flaky(argv, **kw):
        seen_kw.append(kw); tries.append(1)
        if len(tries) == 1:
            raise subprocess.TimeoutExpired(argv, 25)
        return subprocess.CompletedProcess(argv, 0, "SECRETVALUE\n", "")
    assert secret_loader({"kind": "1password", "ref": "op://v/i/f", "token_file": tok}, "x", runner=flaky)() == "SECRETVALUE"
    assert len(tries) == 2 and all(k.get("stdin") is subprocess.DEVNULL for k in seen_kw); n += 1

    def always(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 25)
    try:
        secret_loader({"kind": "1password", "ref": "op://v/i/f", "token_file": tok}, "x", runner=always)(); raise AssertionError
    except ToolError as e:
        assert "timed out twice" in str(e); n += 1

    # sign-in helpers
    open(rt, "w").write("")
    seq = iter([{"device_code": "DC", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
                 "expires_in": 900, "interval": 1}, {"error": "authorization_pending"}, {"refresh_token": "NEW"}])

    def dc_http(method, url, headers, body):
        return 200, json.dumps(next(seq)).encode()
    lines = []
    assert device_code_signin(Tokens("microsoft", "cid", rt, http=dc_http), out=lines.append, sleep=lambda s: None)
    assert open(rt).read() == "NEW" and "ABCD-EFGH" in lines[0] and not any("NEW" in l for l in lines); n += 1
    u = urllib.parse.parse_qs(urllib.parse.urlparse(google_auth_url("gid", "http://localhost:8765/cb", "S")).query)
    assert u["access_type"] == ["offline"] and u["prompt"] == ["consent"] and u["scope"] == [G_SCOPE]; n += 1

    def g_ok(method, url, headers, body):
        return 200, json.dumps({"access_token": "a", "refresh_token": "GNEW"}).encode()
    assert google_signin(Tokens("google", "gid", rt, client_secret=lambda: "s" * 20, http=g_ok), "http://localhost:8765/cb",
                         out=lambda *a: None, wait_for_code=lambda p, path, s: "CODE")
    assert open(rt).read() == "GNEW"; n += 1
    try:
        google_signin(Tokens("google", "gid", rt, client_secret=lambda: "s" * 20), "https://evil.example/cb", out=lambda *a: None)
        raise AssertionError
    except ToolError:
        n += 1                                                                           # redirect must be loopback
    return n
