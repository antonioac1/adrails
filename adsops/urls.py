"""adsops.urls — desired-state URL fields: policy checks and the pure diff against live.

Managed fields (all optional; absent = unmanaged, never touched):

  state["campaign_final_url_suffix"]                    campaign.final_url_suffix
  state["ad_groups"][<name>]["url_custom_parameters"]   ad_group.url_custom_parameters (exact {key: value})
  state["ad_groups"][<name>]["final_url_suffix"]        ad_group.final_url_suffix ("" = none)
  state["ad_groups"][<name>]["ad_final_urls"]           ad.final_urls of EVERY enabled ad in that group

Rails: every final URL must be https on a domain in the campaign's `url_domains` allowlist (from the
deployment config, injected by tools.Context; a value in the state file itself is ignored). A URL
change adds no spend exposure; it still shows up as ops + notes, so it lands in the plan diff and in
the audit log. Pure: no I/O.
"""
import re
import urllib.parse

from .errors import RailViolation, ToolError

MANAGED_KEYS = ("campaign_final_url_suffix", "ad_groups")
_AG_KEYS = {"url_custom_parameters", "final_url_suffix", "ad_final_urls"}
_PARAM_KEY = re.compile(r"^[A-Za-z0-9]{1,16}$")
_MAX_PARAMS, _MAX_VALUE, _MAX_URL, _MAX_SUFFIX = 8, 250, 2048, 2048
_BAD_CHARS = re.compile(r"[\s\\<>\"'`\x00-\x1f\x7f]")


def manages_urls(state):
    """True when the state file manages any URL field (so live URL fields must be read)."""
    return isinstance(state, dict) and any(k in state for k in MANAGED_KEYS)


def _domains(state):
    doms = state.get("url_domains") or []
    if not isinstance(doms, (list, tuple)):
        raise ToolError("url_domains must be a list of hostnames")
    return [str(d).strip().lower().lstrip(".") for d in doms if str(d).strip()]


def check_url(url, domains):
    """Refuse anything but https://<allowlisted host or subdomain>/... (no userinfo, no port)."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL or _BAD_CHARS.search(url):
        raise RailViolation(f"final URL {url!r} is empty, too long or has forbidden characters")
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https":
        raise RailViolation(f"final URL {url!r} must be https")
    host = (p.hostname or "").lower()
    if not host or p.username is not None or p.password is not None or "@" in p.netloc:
        raise RailViolation(f"final URL {url!r} has no host or carries credentials")
    if p.port is not None:
        raise RailViolation(f"final URL {url!r} may not name a port")
    if not domains:
        raise RailViolation("no url_domains allowlist is configured for this campaign; URL changes are refused")
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise RailViolation(f"final URL host {host!r} is not in the allowlist {sorted(domains)}")
    return url


def check_suffix(sfx, what):
    if not isinstance(sfx, str) or len(sfx) > _MAX_SUFFIX or "://" in sfx or _BAD_CHARS.search(sfx) \
            or sfx.startswith(("?", "&", "#")):
        raise RailViolation(f"{what} {sfx!r} must be a query string like 'a=1&b={{_x}}' (no URL, no spaces)")
    return sfx


def check_params(params, what):
    if not isinstance(params, dict):
        raise ToolError(f"{what} must be an object of {{key: value}}")
    if len(params) > _MAX_PARAMS:
        raise RailViolation(f"{what}: at most {_MAX_PARAMS} custom parameters")
    for k, v in params.items():
        if not _PARAM_KEY.match(str(k)):
            raise RailViolation(f"{what}: key {k!r} must be 1-16 letters/digits")
        if not isinstance(v, str) or not v or len(v) > _MAX_VALUE or _BAD_CHARS.search(v):
            raise RailViolation(f"{what}: value for {k!r} must be a non-empty string without spaces")
    return {str(k): v for k, v in params.items()}


def validate_state(state):
    """Check every desired URL value against the rails (even when nothing would change)."""
    domains = _domains(state)
    if "campaign_final_url_suffix" in state:
        check_suffix(state["campaign_final_url_suffix"], "campaign_final_url_suffix")
    groups = state.get("ad_groups", {})
    if not isinstance(groups, dict):
        raise ToolError("state.ad_groups must be an object keyed by ad group name")
    for name, g in groups.items():
        if not isinstance(g, dict):
            raise ToolError(f"state.ad_groups[{name!r}] must be an object")
        extra = sorted(set(g) - _AG_KEYS)
        if extra:
            raise RailViolation(f"state.ad_groups[{name!r}]: unsupported field(s) {extra} "
                                f"(URL fields only: {sorted(_AG_KEYS)}; bids/keywords are not managed here)")
        if "url_custom_parameters" in g:
            check_params(g["url_custom_parameters"], f"ad group {name!r} url_custom_parameters")
        if "final_url_suffix" in g:
            check_suffix(g["final_url_suffix"], f"ad group {name!r} final_url_suffix")
        if "ad_final_urls" in g:
            fu = g["ad_final_urls"]
            if not isinstance(fu, list) or not fu:
                raise ToolError(f"ad group {name!r} ad_final_urls must be a non-empty list")
            for u in fu:
                check_url(u, domains)


def _plist(params):
    return [{"key": k, "value": params[k]} for k in sorted(params)]


def plan_urls(state, live):
    """-> (campaign_update_fields, ops_by_service, notes). Pure."""
    if not manages_urls(state):
        return {}, {}, []
    validate_state(state)
    lu = live.get("urls") if isinstance(live, dict) else None
    if not isinstance(lu, dict):
        raise ToolError("live state has no URL read (live['urls']); refusing to diff URL fields blind")
    camp, ops, notes = {}, {}, []
    if "campaign_final_url_suffix" in state:
        want, have = state["campaign_final_url_suffix"], lu.get("campaign_final_url_suffix") or ""
        if want != have:
            camp["finalUrlSuffix"] = want
            notes.append(f"url: campaign final_url_suffix {have!r} -> {want!r}")
    live_groups = lu.get("ad_groups") or {}
    for name in sorted(state.get("ad_groups", {})):
        g = state["ad_groups"][name]
        lg = live_groups.get(name)
        if not isinstance(lg, dict):
            raise RailViolation(f"ad group {name!r} is not in this campaign (or is removed)")
        if lg.get("duplicate"):
            raise RailViolation(f"ad group name {name!r} is ambiguous (more than one live group)")
        upd, mask = {"resourceName": lg["resource_name"]}, []
        if "final_url_suffix" in g and g["final_url_suffix"] != (lg.get("final_url_suffix") or ""):
            upd["finalUrlSuffix"] = g["final_url_suffix"]
            mask.append("finalUrlSuffix")
            notes.append(f"url: ad group {name} final_url_suffix {lg.get('final_url_suffix') or ''!r} "
                         f"-> {g['final_url_suffix']!r}")
        if "url_custom_parameters" in g:
            want = check_params(g["url_custom_parameters"], name)
            have = dict(lg.get("url_custom_parameters") or {})
            if want != have:
                upd["urlCustomParameters"] = _plist(want)
                mask.append("urlCustomParameters")
                notes.append(f"url: ad group {name} url_custom_parameters {have} -> {want}")
        if mask:
            ops.setdefault("adGroups", []).append({"update": upd, "updateMask": ",".join(mask)})
        if "ad_final_urls" in g:
            ads = lg.get("ads") or []
            if not ads:
                raise RailViolation(f"ad group {name!r} has no enabled ads to point at {g['ad_final_urls']}")
            for ad in ads:
                if list(ad.get("final_urls") or []) != list(g["ad_final_urls"]):
                    ops.setdefault("ads", []).append({"update": {"resourceName": ad["resource_name"],
                                                                 "finalUrls": list(g["ad_final_urls"])},
                                                      "updateMask": "finalUrls"})
                    notes.append(f"url: ad {ad['resource_name'].rsplit('/', 1)[-1]} ({name}) final_urls "
                                 f"{ad.get('final_urls') or []} -> {g['ad_final_urls']}")
    return camp, ops, notes


# ── selftest ──────────────────────────────────────────────────────────────────────────────────
def _live_urls():
    return {"campaign_final_url_suffix": "utm_content={_ag}",
            "ad_groups": {
                "pasco": {"resource_name": "customers/1/adGroups/11", "status": "ENABLED",
                          "url_custom_parameters": {"ag": "pasco"}, "final_url_suffix": "",
                          "ads": [{"resource_name": "customers/1/ads/101",
                                   "final_urls": ["https://example.com/owners/pasco/"]}]},
                "tampa": {"resource_name": "customers/1/adGroups/12", "status": "ENABLED",
                          "url_custom_parameters": {}, "final_url_suffix": "",
                          "ads": [{"resource_name": "customers/1/ads/201", "final_urls": ["https://example.com/"]},
                                  {"resource_name": "customers/1/ads/202", "final_urls": ["https://example.com/"]}]}}}


def selftest():
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "urls.selftest: " + msg
        n += 1

    def refused(fn, exc=RailViolation, needle=""):
        try:
            fn()
        except exc as e:
            assert needle in str(e), (needle, str(e))
            return True
        return False

    doms = ["example.com"]
    ck(check_url("https://example.com/owners/", doms) and check_url("https://www.example.com/x?y=1", doms),
       "https on allowlisted host / subdomain passes")
    for bad in ("http://example.com/", "https://evil.com/", "https://example.com.evil.com/",
                "https://notexample.com/", "https://user@example.com/", "https://example.com:8443/",
                "javascript:alert(1)", "https://example.com/ x", "//example.com/", "", None):
        ck(refused(lambda b=bad: check_url(b, doms)), f"refused URL {bad!r}")
    ck(refused(lambda: check_url("https://example.com/", []), needle="allowlist"), "no allowlist -> refuse")
    ck(check_suffix("utm_content={_ag}&x=1", "s") and check_suffix("", "s") == "", "suffix ok / empty ok")
    for bad in ("https://evil.com/?a=1", "?a=1", "a b", 5):
        ck(refused(lambda b=bad: check_suffix(b, "s")), f"refused suffix {bad!r}")
    ck(check_params({"ag": "pasco"}, "p") == {"ag": "pasco"}, "params ok")
    for bad in ({"a-b": "x"}, {"ag": ""}, {"ag": "a b"}, {"k%d" % i: "v" for i in range(9)}, {"x" * 17: "v"}):
        ck(refused(lambda b=bad: check_params(b, "p")), f"refused params {bad!r}")

    live = {"urls": _live_urls()}
    base = {"url_domains": doms, "campaign_final_url_suffix": "utm_content={_ag}",
            "ad_groups": {"pasco": {"url_custom_parameters": {"ag": "pasco"},
                                    "ad_final_urls": ["https://example.com/owners/pasco/"]}}}
    ck(plan_urls(base, live) == ({}, {}, []), "state matching live -> 0 ops")
    ck(plan_urls({"url_domains": doms}, {}) == ({}, {}, []) and not manages_urls({"x": 1}),
       "unmanaged state -> nothing (and no live read needed)")
    st = {**base, "campaign_final_url_suffix": "utm_content={_ag}&v=2",
          "ad_groups": {"tampa": {"url_custom_parameters": {"ag": "tampa"}, "final_url_suffix": "",
                                  "ad_final_urls": ["https://example.com/owners/tampa/"]}}}
    camp, ops, notes = plan_urls(st, live)
    ck(camp == {"finalUrlSuffix": "utm_content={_ag}&v=2"}, "campaign suffix diff")
    ck(ops["adGroups"] == [{"update": {"resourceName": "customers/1/adGroups/12",
                                       "urlCustomParameters": [{"key": "ag", "value": "tampa"}]},
                            "updateMask": "urlCustomParameters"}], "ad group custom-parameter op")
    ck([o["update"]["resourceName"] for o in ops["ads"]] == ["customers/1/ads/201", "customers/1/ads/202"]
       and all(o["updateMask"] == "finalUrls" and o["update"]["finalUrls"] == ["https://example.com/owners/tampa/"]
               for o in ops["ads"]), "every enabled ad in the group gets the final URL")
    ck(len(notes) == 4 and all(x.startswith("url: ") for x in notes), "each URL change is a note (-> audit)")
    _, ops2, _ = plan_urls({**base, "ad_groups": {"pasco": {"final_url_suffix": "a=1"}}}, live)
    ck(ops2["adGroups"][0]["updateMask"] == "finalUrlSuffix", "ad group suffix op")
    ck(refused(lambda: plan_urls({**base, "ad_groups": {"pasco": {"ad_final_urls": ["https://evil.com/"]}}}, live),
               needle="allowlist"), "off-allowlist final URL refused")
    ck(refused(lambda: plan_urls({**base, "url_domains": []}, live), needle="allowlist"),
       "URL state with no allowlist refused even at 0 diff")
    ck(refused(lambda: plan_urls({**base, "ad_groups": {"pasco": {"cpc_bid_micros": 9}}}, live),
               needle="bids/keywords"), "bid field in ad_groups refused")
    ck(refused(lambda: plan_urls({**base, "ad_groups": {"ghost": {"ad_final_urls": ["https://example.com/"]}}},
                                 live), needle="not in this campaign"), "unknown ad group refused")
    lv = {"urls": _live_urls()}
    lv["urls"]["ad_groups"]["pasco"]["duplicate"] = True
    ck(refused(lambda: plan_urls(base, lv), needle="ambiguous"), "duplicate ad group name refused")
    lv = {"urls": _live_urls()}
    lv["urls"]["ad_groups"]["pasco"]["ads"] = []
    ck(refused(lambda: plan_urls(base, lv), needle="no enabled ads"), "group without enabled ads refused")
    ck(refused(lambda: plan_urls(base, {}), exc=ToolError, needle="no URL read"), "missing live URL read refused")
    ck(refused(lambda: plan_urls({**base, "ad_groups": {"pasco": {"ad_final_urls": []}}}, live), exc=ToolError),
       "empty final URL list refused")
    return n
