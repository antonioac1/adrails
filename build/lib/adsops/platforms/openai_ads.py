"""OpenAI (ChatGPT) Ads REST client: one API key per ad account, stdlib only.

Docs: https://developers.openai.com/ads (OpenAPI: https://developers.openai.com/ads/openapi.json).
Base https://api.ads.openai.com/v1, `Authorization: Bearer <key>`. Updates are POST (not PATCH).
State changes use /activate, /pause, /archive (archive is irreversible).

Rules kept here:
  * The API key never appears in a return value, an exception or a log line (`redact`).
  * Every create carries an Idempotency-Key, so a retried create cannot duplicate a resource.
  * 429 / 5xx / network errors are retried with backoff (Retry-After honoured, capped).
  * Path ids are validated, so a state file cannot inject a path segment.
  * No network at import time; `http=` and `sleep=` are injectable for tests.
"""
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from adsops.errors import ToolError, scrub

BASE = "https://api.ads.openai.com/v1"
CAPI_BASE = "https://bzr.openai.com/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 60
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_KEYLIKE = [re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[^\s\",}]+")]


# ── secrets ──────────────────────────────────────────────────────────────────────────────────
def redact(text, secrets=(), limit=1500):
    """Strip the given secret values and anything key-shaped, then the shared scrub()."""
    s = str(text)
    for sec in secrets:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "[REDACTED-KEY]")
    s = _KEYLIKE[0].sub("[REDACTED-KEY]", s)
    s = _KEYLIKE[1].sub(r"\1[REDACTED]", s)
    return scrub(s, limit)


def _check_key(value, where):
    v = (value or "").strip()
    if not v:
        raise ToolError(f"OpenAI Ads key from {where} is empty")
    if len(v) < 16 or len(v) > 1024 or re.search(r"\s", v):
        raise ToolError(f"OpenAI Ads key from {where} does not look like an API key (length/whitespace)")
    return v


def key_from_file(path):
    p = os.path.expanduser(str(path))

    def load():
        try:
            st = os.stat(p)
        except OSError:
            raise ToolError(f"OpenAI Ads key file not found: {p}") from None
        if st.st_mode & 0o044:
            raise ToolError(f"OpenAI Ads key file {p} is group- or world-readable; chmod 600 it") from None
        try:
            with open(p, encoding="utf-8") as fh:
                return _check_key(fh.read(), f"file {p}")
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"OpenAI Ads key file {p} is unreadable: {type(e).__name__}") from None
    load.kind = "file"
    return load


def key_from_env(name):
    def load():
        v = os.environ.get(str(name))
        if not v:
            raise ToolError(f"environment variable {name} is not set") from None
        return _check_key(v, f"env {name}")
    load.kind = "env"
    return load


def key_from_1password(ref, token_file, op_bin="/opt/homebrew/bin/op", runner=subprocess.run):
    """`op read <ref>` with OP_SERVICE_ACCOUNT_TOKEN from token_file. The value is a plain-text key."""
    ref_s, tpath = str(ref), os.path.expanduser(str(token_file))

    def load():
        try:
            with open(tpath, encoding="utf-8") as fh:
                token = fh.read().strip()
        except Exception as e:
            raise ToolError(f"1Password token file for {ref_s} is unreadable ({tpath}): {type(e).__name__}") from None
        if not token:
            raise ToolError(f"1Password token file for {ref_s} is empty ({tpath})") from None
        env = dict(os.environ, OP_SERVICE_ACCOUNT_TOKEN=token)
        try:
            r = runner([op_bin, "read", ref_s], capture_output=True, text=True, env=env, timeout=60)
        except Exception as e:
            raise ToolError(f"1Password read of {ref_s} could not run: {type(e).__name__}") from None
        if getattr(r, "returncode", 1) != 0:
            err = redact(getattr(r, "stderr", "") or "", (token,), 200)
            raise ToolError(f"1Password read of {ref_s} failed (rc={r.returncode}): {err} - "
                            "has the key been stored at that ref?") from None
        return _check_key(getattr(r, "stdout", ""), f"1Password {ref_s}")
    load.kind = "1password"
    return load


def key_loader(cred, runner=subprocess.run):
    """Build a loader from {"type": "api_key", "sources": [...]}; first source that works wins."""
    if not isinstance(cred, dict) or cred.get("type") != "api_key":
        raise ToolError("openai_ads credentials must be {\"type\": \"api_key\", \"sources\": [...]}")
    sources = cred.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ToolError("openai_ads credentials.sources must be a non-empty list")
    loaders = []
    for i, s in enumerate(sources):
        kind = (s or {}).get("kind") if isinstance(s, dict) else None
        if kind == "1password":
            if not s.get("ref") or not s.get("token_file"):
                raise ToolError(f"credentials.sources[{i}]: 1password needs ref and token_file")
            loaders.append(key_from_1password(s["ref"], s["token_file"], s.get("op_bin", "/opt/homebrew/bin/op"), runner))
        elif kind == "file":
            if not s.get("path"):
                raise ToolError(f"credentials.sources[{i}]: file needs path")
            loaders.append(key_from_file(s["path"]))
        elif kind == "env":
            if not s.get("name"):
                raise ToolError(f"credentials.sources[{i}]: env needs name")
            loaders.append(key_from_env(s["name"]))
        else:
            raise ToolError(f"credentials.sources[{i}].kind must be 1password|file|env")

    def load():
        errs = []
        for ld in loaders:
            try:
                return ld()
            except ToolError as e:
                errs.append(f"{ld.kind}: {e}")
        raise ToolError("no OpenAI Ads key source worked: " + " | ".join(errs)) from None
    return load


# ── transport ────────────────────────────────────────────────────────────────────────────────
def urllib_http(method, url, headers, body, timeout=60):
    """-> (status, headers{lower: value}, body bytes). HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data


def multipart(fields, file_field=None, filename=None, content=None, content_type="application/octet-stream"):
    boundary = "adrails-" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    if file_field:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))[:100] or "upload"
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                      f"filename=\"{safe}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
                     + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _seg(value, what="id"):
    if not isinstance(value, str) or not _ID.match(value):
        raise ToolError(f"invalid {what}: {value!r}")
    return value


class OpenAIAdsClient:
    def __init__(self, key_provider, http=None, base=BASE, sleep=time.sleep, max_attempts=4,
                 rand=random.random, account_id=None):
        if not callable(key_provider):
            raise ToolError("OpenAIAdsClient needs a callable key provider")
        self._key_provider, self._key = key_provider, None
        self.http = http or urllib_http
        self.base, self.sleep, self.max_attempts, self.rand = base.rstrip("/"), sleep, max_attempts, rand
        self.account_id = account_id
        self.calls = 0

    def _api_key(self):
        if self._key is None:
            self._key = self._key_provider()
        return self._key

    def _secrets(self):
        return (self._key,) if self._key else ()

    def request(self, method, path, query=None, body=None, idempotency_key=None, raw=None,
                content_type=None, base=None, idempotent=True):
        """`idempotent=False` (a create the API offers no Idempotency-Key for): retried ONLY on 429,
        which means the request was not processed, so a retry cannot create a duplicate."""
        url = (base or self.base) + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": "Bearer " + self._api_key(), "Accept": "application/json",
                   "User-Agent": "adrails-adsops"}
        data = None
        if raw is not None:
            data, headers["Content-Type"] = raw, content_type
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        last = None
        for attempt in range(1, self.max_attempts + 1):
            self.calls += 1
            try:
                status, rh, rb = self.http(method, url, headers, data)
            except Exception as e:      # network error: retry (only when a repeat cannot duplicate)
                last = f"network error: {type(e).__name__}: {redact(e, self._secrets(), 200)}"
                if attempt < self.max_attempts and (idempotent or idempotency_key):
                    self.sleep(self._backoff(attempt, None))
                    continue
                raise ToolError(f"OpenAI Ads {method} {path} failed after {attempt} attempts: {last}") from None
            if 200 <= status < 300:
                if not rb:
                    return {}
                try:
                    return json.loads(rb)
                except ValueError:
                    raise ToolError(f"OpenAI Ads {method} {path}: response is not JSON") from None
            msg = self._error_text(rb)
            last = f"HTTP {status}: {msg}"
            retry_ok = status in RETRY_STATUS and (idempotent or idempotency_key or status == 429)
            if retry_ok and attempt < self.max_attempts:
                self.sleep(self._backoff(attempt, (rh or {}).get("retry-after")))
                continue
            err = ToolError(f"OpenAI Ads HTTP {status} on {method} {path}: {msg}")
            err.status = status
            raise err from None
        raise ToolError(f"OpenAI Ads {method} {path} failed: {last}")

    def _backoff(self, attempt, retry_after):
        try:
            ra = float(retry_after)
            if ra >= 0:
                return min(ra, MAX_RETRY_AFTER_S)
        except (TypeError, ValueError):
            pass
        return min(2 ** (attempt - 1) + self.rand(), MAX_RETRY_AFTER_S)

    def _error_text(self, rb):
        try:
            d = json.loads(rb or b"{}")
            e = d.get("error", d) if isinstance(d, dict) else d
            if isinstance(e, dict):
                parts = [str(e.get(k)) for k in ("code", "type", "message", "param") if e.get(k)]
                text = " | ".join(parts) or json.dumps(e)[:400]
            else:
                text = str(e)
        except Exception:
            text = (rb or b"")[:400].decode("utf-8", "replace")
        return redact(text, self._secrets(), 500)

    # ── reads ──
    def get(self, path, query=None):
        return self.request("GET", path, query=query)

    def list_all(self, path, query=None, max_pages=50):
        q, out = dict(query or {}), []
        q.setdefault("limit", 500)
        for _ in range(max_pages):
            page = self.get(path, q)
            data = page.get("data") or []
            out.extend(data)
            if not page.get("has_more") or not data:
                return out
            cur = page.get("last_id") or (data[-1].get("id") if isinstance(data[-1], dict) else None)
            if not cur or cur == q.get("after"):
                raise ToolError(f"OpenAI Ads {path}: has_more without a usable cursor")
            q["after"] = cur
        raise ToolError(f"OpenAI Ads {path}: more than {max_pages} pages")

    def ad_account(self):
        return self.get("/ad_account")

    def campaign(self, cid):
        return self.get("/campaigns/" + _seg(cid), {"include[]": ["serving_issues"]})

    def campaigns(self, name=None):
        return self.list_all("/campaigns", {"name": name} if name else None)

    def ad_groups(self, campaign_id):
        return self.list_all("/ad_groups", {"campaign_id": _seg(campaign_id), "include[]": ["serving_issues"]})

    def ads(self, ad_group_id):
        return self.list_all("/ads", {"ad_group_id": _seg(ad_group_id), "include[]": ["serving_issues"]})

    def insights(self, level_path, query):
        return self.list_all(level_path + "/insights", query, max_pages=20)

    def geo_lookup(self, q, limit=10):
        return self.get("/geo_lookup/search", {"q": q, "limit": int(limit)})

    # ── writes (callers must have passed the rails) ──
    def create(self, kind, body, idempotency_key):
        if not idempotency_key:
            raise ToolError("every create needs an Idempotency-Key")
        path = {"campaign": "/campaigns", "ad_group": "/ad_groups", "ad": "/ads"}[kind]
        return self.request("POST", path, body=body, idempotency_key=idempotency_key)

    def update(self, kind, rid, body):
        if "status" in body:
            raise ToolError("status changes go through activate/pause/archive, not update")
        path = {"campaign": "/campaigns/", "ad_group": "/ad_groups/", "ad": "/ads/"}[kind] + _seg(rid)
        return self.request("POST", path, body=body)

    def set_state(self, kind, rid, action):
        if action not in ("activate", "pause", "archive"):
            raise ToolError(f"unknown state action {action!r}")
        path = {"campaign": "/campaigns/", "ad_group": "/ad_groups/", "ad": "/ads/"}[kind] + _seg(rid)
        return self.request("POST", path + "/" + action)

    def bulk_validate(self, operations, idempotency_key):
        """Bulk API, validate_only=True ONLY. AdRails never writes through the Bulk API: real writes go
        through the per-resource endpoints, where every create is forced paused."""
        return self.request("POST", "/bulk_mutation_jobs", idempotency_key=idempotency_key,
                            body={"validate_only": True, "partial_failure": False, "operations": operations})

    def bulk_job(self, job_id):
        return self.get("/bulk_mutation_jobs/" + _seg(job_id))

    def bulk_results(self, job_id):
        return self.list_all("/bulk_mutation_jobs/" + _seg(job_id) + "/operations", {"limit": 100})

    def upload_image(self, filename, content, content_type, purpose=None):
        fields = {"purpose": purpose} if purpose else {}
        raw, ctype = multipart(fields, "file", filename, content, content_type)
        return self.request("POST", "/upload", raw=raw, content_type=ctype, idempotent=False)

    def update_brand(self, body):
        return self.request("POST", "/ad_account/brand", body=body)

    def pixels(self):
        return self.list_all("/conversions/pixels")

    def create_pixel(self, name):
        return self.request("POST", "/conversions/pixels", body={"name": name, "client_type": "web"}, idempotent=False)

    def event_settings(self):
        return self.list_all("/conversions/event_settings")

    def create_event_setting(self, name, event_type, source_id):
        return self.request("POST", "/conversions/event_settings",
                            body={"name": name, "event_type": event_type, "attribution_window_days": 30,
                                  "source_ids": [_seg(source_id, "source id")]}, idempotent=False)

    def create_capi_key(self, name):
        return self.request("POST", "/conversions/api_keys", body={"name": name}, idempotent=False)


def capi_validate_event(capi_key, pixel_id, event, http=None):
    """POST one event to the Conversions API with validate_only=true (never saved, never counted)."""
    c = OpenAIAdsClient(lambda: capi_key, http=http, base=CAPI_BASE, max_attempts=2)
    return c.request("POST", "/events", query={"pid": _seg(str(pixel_id), "pixel id")},
                     body={"validate_only": True, "integration_source": "adrails", "events": [event]})


def image_info(content):
    """(format, width, height) for PNG/JPEG/WebP from the header bytes; None if unrecognised."""
    b = content
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        return "png", int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1
                continue
            marker, seglen = b[i + 1], int.from_bytes(b[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return "jpeg", int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
            i += 2 + seglen
        return None
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP" and len(b) >= 30:
        if b[12:16] == b"VP8X":
            return "webp", 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if b[12:16] == b"VP8 ":
            return "webp", int.from_bytes(b[26:28], "little") & 0x3FFF, int.from_bytes(b[28:30], "little") & 0x3FFF
        if b[12:16] == b"VP8L":
            bits = int.from_bytes(b[21:25], "little")
            return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


# ── live reads used by the engine and the guard ──────────────────────────────────────────────
def local_midnight(now_ts, tzname):
    """Unix seconds of the most recent local midnight in the account time zone."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tzname or "UTC")
    except Exception:
        raise ToolError(f"unknown account timezone {tzname!r}") from None
    d = dt.datetime.fromtimestamp(now_ts, tz)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def local_date(now_ts, tzname):
    import datetime as dt
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tzname or "UTC")
    except Exception:
        raise ToolError(f"unknown account timezone {tzname!r}") from None
    return dt.datetime.fromtimestamp(now_ts, tz).date()


def insights_end(ts, tzname):
    """Insights time_ranges.end must be a FULL HOUR in the ad account's timezone (live API, 2026-11-06:
    "minute and second must be 0"). Round UP, never down: rounding down would drop the current hour's spend from
    the guard's count. The API accepts a future full-hour end (verified live: same spend for floor and ceil)."""
    import datetime as dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        tz = ZoneInfo(tzname or "UTC")
    except (ZoneInfoNotFoundError, ValueError):          # only a genuinely unknown zone; never mask a code error
        raise ToolError(f"unknown account timezone {tzname!r}") from None
    d = dt.datetime.fromtimestamp(int(ts), tz)
    if d.minute == 0 and d.second == 0:
        return int(ts)
    return int((d.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).timestamp())


def campaign_spend(client, campaign_id, start_ts, end_ts):
    """Spend (account currency, major units) for one campaign between two unix times.

    Fails CLOSED: no rows, an unreadable spend, or delivery (impressions/clicks) without a spend field
    raises ToolError, so a renamed field or an empty report can never read as $0 to the guard.
    `includes[]=zero_impression_items` asks for a row even when the campaign did not deliver."""
    start_ts, end_ts = int(start_ts), int(end_ts)
    if end_ts <= start_ts:
        raise ToolError("spend window is empty or inverted (check campaign created_at units)")
    rows = client.insights("/campaigns/" + _seg(campaign_id), {
        "time_granularity": "none", "aggregation_level": "campaign", "includes[]": ["zero_impression_items"],
        "fields[]": ["campaign.id", "campaign.impressions", "campaign.clicks", "campaign.spend"],
        "time_ranges[]": [json.dumps({"type": "unix_range", "start": start_ts, "end": end_ts})]})
    if not rows:
        raise ToolError("insights returned no rows for the campaign; spend unknown (failing closed)")
    total = 0.0
    for r in rows:
        v = r.get("spend", r.get("campaign_spend"))
        if v is None:
            delivered = any(isinstance(r.get(k), (int, float)) and r.get(k) > 0
                            for k in ("impressions", "campaign_impressions", "clicks", "campaign_clicks"))
            if delivered or not any(k in r for k in ("impressions", "campaign_impressions")):
                raise ToolError("insights row has no spend field; spend unknown (failing closed)")
            continue                                    # explicit zero-delivery row
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ToolError("insights row has no readable spend field")
        try:
            f = float(v)
        except ValueError:
            raise ToolError("insights spend is not a number") from None
        if f != f or f < 0 or f > 1e9:
            raise ToolError("insights spend is out of range")
        total += f
    return round(total, 6)


def created_ts(camp, now_ts):
    """Campaign creation time in unix seconds; ms tolerated; missing / future / >5y old -> ToolError."""
    v = camp.get("created_at")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ToolError("campaign has no created_at; lifetime spend unknown (failing closed)")
    v = v / 1000 if v > 1e11 else v
    if v > now_ts + 300:
        raise ToolError("campaign created_at is in the future; lifetime spend unknown (failing closed)")
    if v < now_ts - 5 * 365 * 86400 + 86400:
        raise ToolError("campaign is older than the 5-year insights window; lifetime spend cannot be read")
    return int(v) // 3600 * 3600


def tag(campaign_key):
    """Written into the description of every campaign AdRails creates, so the guard can find all of a
    key's campaigns (renames / rotations included) without trusting the desired-state file."""
    return "adrails:" + campaign_key


def account_spend(client, start_ts, end_ts):
    start_ts, end_ts = int(start_ts), int(end_ts)
    if end_ts <= start_ts:
        return 0.0
    rows = client.insights("/ad_account", {
        "time_granularity": "none", "aggregation_level": "ad_account",
        "fields[]": ["ad_account.impressions", "ad_account.spend"],
        "time_ranges[]": [json.dumps({"type": "unix_range", "start": start_ts, "end": end_ts})]})
    total = 0.0
    for r in rows:
        v = r.get("spend", r.get("ad_account_spend"))
        if v is None:
            if any(isinstance(r.get(k), (int, float)) and r.get(k) > 0 for k in ("impressions", "ad_account_impressions")):
                raise ToolError("account insights row has no spend field; spend unknown (failing closed)")
            continue
        f = float(v)
        if f != f or f < 0 or f > 1e9:
            raise ToolError("account insights spend is out of range")
        total += f
    return round(total, 6)


def live_tree(client, campaign_id=None, campaign_name=None, now=time.time, with_spend=True,
              campaign_key=None, account_since=None):
    """Raw live view: account; the managed campaign (by id, else exact non-archived name) with its ad
    groups and ads; every OTHER campaign tagged for this key ("siblings"); every campaign in the
    account; spend summed over the managed campaign and its siblings; optional account-wide spend."""
    acct = client.ad_account()
    all_c = client.campaigns()
    if campaign_id:
        camp = client.campaign(campaign_id)
    else:
        hits = [c for c in all_c if c.get("name") == campaign_name and c.get("status") != "archived"]
        if len(hits) > 1:
            raise ToolError(f"{len(hits)} live campaigns are named {campaign_name!r}; put the right id in the state file")
        camp = client.campaign(hits[0]["id"]) if hits else None
    t_ = tag(campaign_key) if campaign_key else None
    sib = [c for c in all_c if t_ and c.get("description") == t_ and (not camp or c.get("id") != camp.get("id"))]
    out = {"account": acct, "campaign": camp, "ad_groups": [], "siblings": sib, "all_campaigns": all_c,
           "spend_today": 0.0, "spend_life": 0.0, "spend_by_campaign": {}, "account_spend": None}
    if camp:
        for ag in client.ad_groups(camp["id"]):
            if ag.get("status") == "archived":
                continue
            ag = dict(ag)
            ag["ads"] = [a for a in client.ads(ag["id"]) if a.get("status") != "archived"]
            out["ad_groups"].append(ag)
    if with_spend:
        t = int(now())
        tz = acct.get("timezone") or "UTC"
        midnight = local_midnight(t, tz)
        for c in ([camp] if camp else []) + sib:
            start_life = created_ts(c, t)
            t_end = insights_end(t, tz)
            life = campaign_spend(client, c["id"], start_life, t_end) if t > start_life else 0.0
            today = campaign_spend(client, c["id"], max(midnight, start_life), t_end) \
                if c.get("status") != "archived" and t > max(midnight, start_life) else 0.0
            out["spend_by_campaign"][c["id"]] = {"today": today, "life": life}
            out["spend_life"] = round(out["spend_life"] + life, 6)
            out["spend_today"] = round(out["spend_today"] + today, 6)
        if account_since:
            import datetime as dt
            from zoneinfo import ZoneInfo
            d = dt.date.fromisoformat(account_since)
            start = int(dt.datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz)).timestamp())
            out["account_spend"] = account_spend(client, start, insights_end(t, tz))
    return out


# ── selftest (all I/O mocked) ────────────────────────────────────────────────────────────────
def selftest():
    n = 0
    KEY = "sk-adrails-TESTKEY-0123456789abcdef"
    calls, slept = [], []

    def http_seq(responses):
        it = iter(responses)

        def http(method, url, headers, body):
            calls.append((method, url, dict(headers), body))
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        return http

    ok = (200, {}, b'{"id": "adacct_1", "currency_code": "USD"}')
    c = OpenAIAdsClient(lambda: KEY, http=http_seq([ok]), sleep=slept.append)
    assert c.ad_account()["id"] == "adacct_1"
    assert calls[-1][2]["Authorization"] == "Bearer " + KEY and calls[-1][1] == BASE + "/ad_account"
    n += 1
    # 429 with Retry-After then success; backoff honoured
    calls.clear()
    c = OpenAIAdsClient(lambda: KEY, http=http_seq([(429, {"retry-after": "3"}, b"{}"), ok]), sleep=slept.append)
    c.ad_account()
    assert slept[-1] == 3 and len(calls) == 2
    n += 1
    # 400 error: not retried, key never in message
    c = OpenAIAdsClient(lambda: KEY, http=http_seq([(400, {}, json.dumps({"error": {"message": "bad " + KEY}}).encode())]))
    try:
        c.ad_account()
        raise AssertionError("expected ToolError")
    except ToolError as e:
        assert KEY not in str(e) and getattr(e, "status", None) == 400
    n += 1
    # create requires an idempotency key and sends it
    calls.clear()
    c = OpenAIAdsClient(lambda: KEY, http=http_seq([ok]))
    c.create("campaign", {"name": "x"}, "idem-1")
    assert calls[-1][2]["Idempotency-Key"] == "idem-1"
    try:
        c.create("campaign", {}, None)
        raise AssertionError
    except ToolError:
        n += 1
    # update refuses status; bad id refused
    for fn in (lambda: c.update("campaign", "cmpn_1", {"status": "active"}),
               lambda: c.set_state("campaign", "../x", "pause"),
               lambda: c.set_state("campaign", "cmpn_1", "delete")):
        try:
            fn()
            raise AssertionError
        except ToolError:
            n += 1
    assert redact("Authorization: Bearer " + KEY, (KEY,)).count("TESTKEY") == 0
    n += 1
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (700).to_bytes(4, "big")
    assert image_info(png) == ("png", 640, 700)
    n += 1
    return n
