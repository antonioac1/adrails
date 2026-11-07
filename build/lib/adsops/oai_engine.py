"""OpenAI Ads desired state -> diff -> guarded plan -> validate -> execute. Pure except execute().

Desired-state file (one per campaign, committed to git; amounts in USD):
{
  "platform": "openai_ads", "ad_account_id": "adacct_...", "campaign_key": "bt_chatgpt_owners",
  "campaign": {"id": null, "name": "...", "status": "paused", "bidding_type": "clicks",
               "budget": {"daily_usd": 36}, "end_date": "2026-11-19",
               "targeting": {"countries": ["US"], "location_ids": ["3000xxx"], "platforms": null},
               "conversion_event_setting_ids": []},
  "ad_groups": [{"key": "owners", "id": null, "name": "...", "status": "paused",
                 "bidding": {"strategy": "fixed_bid", "max_bid_usd": 5}, "context_hints": ["..."],
                 "ads": [{"key": "flat149", "id": null, "name": "...", "status": "paused",
                          "title": "...", "body": "...", "target_url": "https://...", "file_id": "file_..."}]}],
  "approval": {"activate": false, "approved_by": null, "approved_on": null, "expires_on": null,
               "max_daily_budget_usd": null, "max_bid_usd": null, "max_total_spend_usd": null}
}
Keys starting with "_" are comments. Live resources the file does not name are never touched.

Ops are a flat list executed in this order: pauses, creates (always status=paused), updates,
archives (policy-gated), activations last (ad -> ad group -> campaign; the campaign is the spend gate).
"""
import datetime as dt
import hashlib
import json
import re
import urllib.parse

from . import oai_policy, strict, urls
from .errors import RailViolation, ToolError

STATUSES = ("active", "paused", "archived")
BIDDING_TYPES = ("clicks", "impressions", "conversions")
STRATEGIES = ("fixed_bid", "maximize_clicks", "maximize_conversions")
PLATFORMS = ("ios_app", "android_app", "web", "desktop_web", "ios_web", "android_web")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,40}$")
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
ORDER = {"pause": 0, "create": 1, "update": 2, "archive": 3, "activate": 4}
KIND_ORDER = {"campaign": 0, "ad_group": 1, "ad": 2}


def micros(usd):
    return int(round(float(usd) * 1_000_000))


def usd(m):
    return None if m is None else round(int(m) / 1_000_000, 6)


def _str(v, what, lo=1, hi=1000):
    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi or any(c in v for c in "\x00\r\n\t"):
        raise ToolError(f"state: {what} must be a string of {lo}..{hi} characters without control characters")
    return v.strip()


def _not_todo(v, what):
    if isinstance(v, str) and v.upper().startswith("TODO"):
        raise ToolError(f"state: {what} is still a placeholder ({v}); fill it in first")
    return v


def _opt_id(v, what):
    if v is None:
        return None
    _not_todo(v, what)
    if not isinstance(v, str) or not _ID.match(v):
        raise ToolError(f"state: {what} must be an id string or null")
    return v


def _status(v, what):
    if v not in STATUSES:
        raise ToolError(f"state: {what} must be one of {STATUSES}")
    return v


def _money(v, what, positive=True):
    strict.finite_number(v, f"state: {what}", 0, allow_equal_min=not positive)
    if v > 1_000_000:
        raise ToolError(f"state: {what} is absurdly large")
    return float(v)


# ── desired state ────────────────────────────────────────────────────────────────────────────
def validate_state(raw):
    if not isinstance(raw, dict) or raw.get("platform") != "openai_ads":
        raise ToolError("state: must be an object with platform \"openai_ads\"")
    st = {"platform": "openai_ads"}
    for k in ("ad_account_id", "campaign_key"):
        st[k] = _str(raw.get(k), k, 1, 128)
    c = raw.get("campaign")
    if not isinstance(c, dict):
        raise ToolError("state: 'campaign' must be an object")
    camp = {"id": _opt_id(c.get("id"), "campaign.id"), "name": _str(c.get("name"), "campaign.name", 3, 1000),
            "status": _status(c.get("status"), "campaign.status")}
    if c.get("bidding_type") not in BIDDING_TYPES:
        raise ToolError(f"state: campaign.bidding_type must be one of {BIDDING_TYPES}")
    camp["bidding_type"] = c["bidding_type"]
    b = c.get("budget")
    if not isinstance(b, dict) or len([k for k in ("daily_usd", "lifetime_usd") if b.get(k) is not None]) != 1 \
            or set(b) - {"daily_usd", "lifetime_usd"}:
        raise ToolError("state: campaign.budget must have exactly one of daily_usd / lifetime_usd")
    camp["budget"] = {k: _money(v, f"campaign.budget.{k}") for k, v in b.items() if v is not None}
    for k in ("start_date", "end_date"):
        camp[k] = strict.iso_date(c[k], f"state: campaign.{k}") if c.get(k) is not None else None
    t = c.get("targeting") or {}
    if not isinstance(t, dict) or set(t) - {"countries", "location_ids", "excluded_location_ids", "platforms"}:
        raise ToolError("state: campaign.targeting allows countries, location_ids, excluded_location_ids, platforms")
    tg = {}
    for k in ("countries", "location_ids", "excluded_location_ids"):
        v = t.get(k) or []
        if not isinstance(v, list) or len(v) > 2500 or not all(isinstance(x, str) and _ID.match(x) for x in v):
            raise ToolError(f"state: campaign.targeting.{k} must be a list of ids/codes")
        for x in v:
            _not_todo(x, f"campaign.targeting.{k}")
        tg[k] = sorted(set(v))
    if not tg["countries"] and not tg["location_ids"]:
        raise ToolError("state: campaign.targeting needs countries or location_ids (no worldwide campaigns)")
    pl = t.get("platforms")
    if pl is not None and (not isinstance(pl, list) or not pl or not all(p in PLATFORMS for p in pl)):
        raise ToolError(f"state: campaign.targeting.platforms must be null or a non-empty list of {PLATFORMS}")
    tg["platforms"] = sorted(set(pl)) if pl else None
    camp["targeting"] = tg
    ces = c.get("conversion_event_setting_ids") or []
    if not isinstance(ces, list) or not all(isinstance(x, str) and _ID.match(x) for x in ces):
        raise ToolError("state: campaign.conversion_event_setting_ids must be a list of ids")
    camp["conversion_event_setting_ids"] = sorted(set(ces))
    st["campaign"] = camp
    ags, seen = raw.get("ad_groups") or [], set()
    if not isinstance(ags, list) or len(ags) > 50:
        raise ToolError("state: 'ad_groups' must be a list (max 50)")
    st["ad_groups"] = []
    for i, g in enumerate(ags):
        if not isinstance(g, dict) or not isinstance(g.get("key"), str) or not _KEY.match(g["key"]) or g["key"] in seen:
            raise ToolError(f"state: ad_groups[{i}].key must be a unique lowercase key")
        seen.add(g["key"])
        if any(x["name"] == g.get("name") for x in st["ad_groups"]):
            raise ToolError(f"state: ad group name {g.get('name')!r} is used twice")
        bid = g.get("bidding") or {}
        if bid.get("strategy") not in STRATEGIES:
            raise ToolError(f"state: ad group {g['key']!r} bidding.strategy must be one of {STRATEGIES}")
        mb = bid.get("max_bid_usd")
        if (bid["strategy"] == "fixed_bid") != (mb is not None):
            raise ToolError(f"state: ad group {g['key']!r}: max_bid_usd is required for fixed_bid and only for it")
        hints = g.get("context_hints") or []
        if not isinstance(hints, list) or len(hints) > 2000:
            raise ToolError(f"state: ad group {g['key']!r} context_hints must be a list (max 2000)")
        ag = {"key": g["key"], "id": _opt_id(g.get("id"), f"ad group {g['key']} id"),
              "name": _str(g.get("name"), f"ad group {g['key']} name", 3, 1000),
              "status": _status(g.get("status"), f"ad group {g['key']} status"),
              "bidding": {"strategy": bid["strategy"],
                          "max_bid_usd": None if mb is None else _money(mb, f"{g['key']}.max_bid_usd")},
              "context_hints": [_str(h, f"{g['key']} context hint", 1, 500) for h in hints], "ads": []}
        aseen = set()
        for j, a in enumerate(g.get("ads") or []):
            if not isinstance(a, dict) or not isinstance(a.get("key"), str) or not _KEY.match(a["key"]) or a["key"] in aseen:
                raise ToolError(f"state: ad_groups[{i}].ads[{j}].key must be a unique lowercase key")
            aseen.add(a["key"])
            if any(x["name"] == a.get("name") for x in ag["ads"]):
                raise ToolError(f"state: ad name {a.get('name')!r} is used twice in ad group {g['key']!r}")
            w = f"ad {g['key']}/{a['key']}"
            ag["ads"].append({"key": a["key"], "id": _opt_id(a.get("id"), w + " id"),
                              "name": _str(a.get("name"), w + " name", 3, 1000),
                              "status": _status(a.get("status"), w + " status"),
                              "title": _str(a.get("title"), w + " title", 3, 50),
                              "body": _str(a.get("body"), w + " body", 1, 100),
                              "target_url": _str(a.get("target_url"), w + " target_url", 8, 2048),
                              "file_id": _opt_id(a.get("file_id"), w + " file_id")})
        st["ad_groups"].append(ag)
    ap = raw.get("approval") or {}
    if not isinstance(ap, dict):
        raise ToolError("state: 'approval' must be an object")
    st["approval"] = ap
    return st


def load_state(path):
    import os
    return validate_state(strict.load_json_file(os.path.expanduser(path), "state file"))


# ── rails on content ─────────────────────────────────────────────────────────────────────────
def check_url(url, pol, campaign_key):
    urls.check_url(url, pol["allowed_domains"])
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    for k, v in oai_policy.required_utm(pol, campaign_key).items():
        if q.get(k) != [v]:
            raise RailViolation(f"target_url {url!r} must carry exactly {k}={v}")
    return url


def check_content(st, pol):
    if st["ad_account_id"] != pol["ad_account_id"] or st["campaign_key"] != pol["campaign_key"]:
        raise RailViolation("state ad_account_id/campaign_key do not match the policy file")
    if st["campaign"]["status"] == "archived" or any(
            ag["status"] == "archived" or any(a["status"] == "archived" for a in ag["ads"]) for ag in st["ad_groups"]):
        if not pol["allow_archive"]:
            raise RailViolation("archiving is irreversible and the policy has allow_archive=false")
    oai_policy.check_caps(st, pol)
    for ag in st["ad_groups"]:
        for a in ag["ads"]:
            check_url(a["target_url"], pol, st["campaign_key"])
    return True


# ── live normalisation ───────────────────────────────────────────────────────────────────────
def norm_campaign(c):
    b = c.get("budget") or {}
    t = c.get("targeting") or {}
    loc, exc = t.get("locations") or {}, t.get("excluded_locations") or {}
    pl = (t.get("platforms") or {}).get("included") if isinstance(t.get("platforms"), dict) else None
    return {"id": c.get("id"), "name": c.get("name"), "status": c.get("status"), "bidding_type": c.get("bidding_type"),
            "budget": {"daily_usd": usd(b["daily_spend_limit_micros"])} if b.get("daily_spend_limit_micros") is not None
            else {"lifetime_usd": usd(b.get("lifetime_spend_limit_micros"))},
            "start_time": c.get("start_time"), "end_time": c.get("end_time"),
            "targeting": {"countries": sorted(loc.get("countries") or []),
                          "location_ids": sorted(str(x.get("id")) for x in loc.get("include") or []),
                          "excluded_location_ids": sorted(str(x.get("id")) for x in exc.get("include") or []),
                          "platforms": sorted(pl) if pl else None},
            "conversion_event_setting_ids": sorted(c.get("conversion_event_setting_ids") or [])}


def norm_ad_group(g):
    bc = g.get("bidding_config") or {}
    return {"id": g.get("id"), "name": g.get("name"), "status": g.get("status"),
            "bidding": {"strategy": bc.get("strategy"), "max_bid_usd": usd(bc.get("max_bid_micros"))},
            "context_hints": list(g.get("context_hints") or [])}


def norm_ad(a):
    cr = a.get("creative") or {}
    return {"id": a.get("id"), "name": a.get("name"), "status": a.get("status"), "title": cr.get("title"),
            "body": cr.get("body"), "target_url": cr.get("target_url"), "file_id": cr.get("file_id"),
            "review_status": a.get("review_status")}


def fingerprint(tree):
    """Hash of the live fields the plan depends on (not spend, timestamps or review chatter)."""
    c = tree.get("campaign")
    view = {"campaign": norm_campaign(c) if c else None,
            "ad_groups": [{**norm_ad_group(g), "ads": [{k: v for k, v in norm_ad(a).items() if k != "review_status"}
                                                       for a in g.get("ads", [])]} for g in tree.get("ad_groups", [])]}
    return hashlib.sha256(json.dumps(view, sort_keys=True, default=str).encode()).hexdigest()


# ── body builders ────────────────────────────────────────────────────────────────────────────
def _zone(tz):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:
        raise ToolError(f"unknown account timezone {tz!r}") from None


def _end_time(d, tz):
    """end_date is inclusive: end at the following local midnight."""
    day = dt.date.fromisoformat(d) + dt.timedelta(days=1)
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=_zone(tz)).timestamp())


def _start_time(d, tz):
    day = dt.date.fromisoformat(d)
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=_zone(tz)).timestamp())


def _budget_body(b):
    if b.get("daily_usd") is not None:
        return {"daily_spend_limit_micros": micros(b["daily_usd"])}
    return {"lifetime_spend_limit_micros": micros(b["lifetime_usd"])}


def _targeting_body(t):
    loc = {}
    if t["countries"]:
        loc["countries"] = list(t["countries"])
    if t["location_ids"]:
        loc["include"] = [{"id": x} for x in t["location_ids"]]
    body = {"locations": loc}
    if t["excluded_location_ids"]:
        body["excluded_locations"] = {"include": [{"id": x} for x in t["excluded_location_ids"]]}
    body["platforms"] = {"included": list(t["platforms"])} if t["platforms"] else None
    return body


def _bidding_body(bid, bidding_type):
    billing = "impression" if bidding_type == "impressions" else "click"
    out = {"billing_event_type": billing, "strategy": bid["strategy"]}
    if bid["strategy"] == "fixed_bid":
        out["max_bid_micros"] = micros(bid["max_bid_usd"])
    return out


def _creative(a):
    cr = {"type": "chat_card", "title": a["title"], "body": a["body"], "target_url": a["target_url"]}
    if a["file_id"]:
        cr["file_id"] = a["file_id"]
    return cr


def idem_key(account_id, campaign_key, ref, body):
    h = hashlib.sha256(json.dumps([account_id, campaign_key, ref, body], sort_keys=True).encode()).hexdigest()
    return "adrails-" + h[:48]


# ── plan ─────────────────────────────────────────────────────────────────────────────────────
def _state_ops(kind, ref, rid, want, have, notes, label):
    """Status transition ops (not for newly created resources: those are handled by the caller)."""
    if want == have:
        return []
    if have == "archived":
        raise RailViolation(f"{label} is archived live; archived resources cannot be changed")
    action = {"active": "activate", "paused": "pause", "archived": "archive"}[want]
    notes.append(f"{label}: status {have} -> {want}")
    return [{"op": action, "kind": kind, "ref": ref, "id": rid}]


def _adopted(by_name, n_before, ops, label):
    """A resource found by NAME (no id in the state file) may only converge with zero ops: changing it
    needs its id recorded, so a same-named resource the file does not own is never edited."""
    if by_name and len(ops) > n_before:
        raise RailViolation(f"{label} was matched by name only; record its id in the state file before changing it")


def plan(st, pol, tree):
    """-> (ops, notes, info). Raises RailViolation for anything the rails refuse."""
    check_content(st, pol)
    acct = tree["account"]
    if acct.get("id") and acct["id"] != st["ad_account_id"]:
        raise RailViolation(f"API key belongs to {acct.get('id')!r}, not {st['ad_account_id']!r}")
    if (acct.get("currency_code") or "USD") != pol["currency"]:
        raise RailViolation(f"account currency {acct.get('currency_code')!r} is not {pol['currency']}")
    tz = acct.get("timezone") or "UTC"
    key, aid, ops, notes = st["campaign_key"], st["ad_account_id"], [], []
    c = st["campaign"]
    live_c = tree.get("campaign")
    if c["id"] and not live_c:
        raise RailViolation(f"campaign id {c['id']} from the state file was not found live (archived or deleted?)")
    cbody = {"name": c["name"], "bidding_type": c["bidding_type"], "budget": _budget_body(c["budget"]),
             "targeting": _targeting_body(c["targeting"]), "end_time": _end_time(c["end_date"], tz)}
    if c["start_date"]:
        cbody["start_time"] = _start_time(c["start_date"], tz)
    if c["conversion_event_setting_ids"]:
        cbody["conversion_event_setting_ids"] = list(c["conversion_event_setting_ids"])
    if not live_c:
        if c["status"] == "archived":
            raise RailViolation("refusing to create a campaign in status archived")
        others = [x.get("id") for x in tree.get("siblings", []) if x.get("status") != "archived"]
        if others:
            raise RailViolation(f"campaign key {key!r} already has live campaign(s) {others}: put the id in the state "
                                "file instead of creating another (one campaign per key, so the cap cannot be multiplied)")
        body = {**cbody, "status": "paused", "description": "adrails:" + key}
        ops.append({"op": "create", "kind": "campaign", "ref": "campaign", "body": body,
                    "idempotency_key": idem_key(aid, key, "campaign", body)})
        notes.append(f"campaign: create {c['name']!r} PAUSED")
        live_ags = {}
    else:
        L = norm_campaign(live_c)
        if L["bidding_type"] != c["bidding_type"]:
            raise RailViolation(f"bidding_type is immutable (live {L['bidding_type']!r}, wanted {c['bidding_type']!r}): "
                                "create a new campaign")
        upd = {}
        if L["name"] != c["name"]:
            upd["name"] = c["name"]
        if L["budget"] != c["budget"]:
            if "lifetime_usd" in c["budget"] and "daily_usd" in L["budget"]:
                raise RailViolation("a daily budget cannot be switched back to lifetime: create a new campaign")
            upd["budget"] = cbody["budget"]
            notes.append(f"campaign: budget {L['budget']} -> {c['budget']}")
        if L["targeting"] != c["targeting"]:
            upd["targeting"] = cbody["targeting"]
            notes.append(f"campaign: targeting {L['targeting']} -> {c['targeting']}")
        if L["end_time"] != cbody["end_time"]:
            upd["end_time"] = cbody["end_time"]
            notes.append(f"campaign: end {L['end_time']} -> {cbody['end_time']} ({c['end_date']} inclusive)")
        if "start_time" in cbody and L["start_time"] != cbody["start_time"]:
            upd["start_time"] = cbody["start_time"]
        if L["conversion_event_setting_ids"] != c["conversion_event_setting_ids"]:
            if L["bidding_type"] == "conversions":
                raise RailViolation("conversion events of a conversions campaign cannot be changed")
            upd["conversion_event_setting_ids"] = list(c["conversion_event_setting_ids"])
        n0 = len(ops)
        if upd:
            ops.append({"op": "update", "kind": "campaign", "ref": "campaign", "id": L["id"], "body": upd,
                        "prev": {k: (L.get(k) if k != "budget" else L["budget"]) for k in upd}})
        ops += _state_ops("campaign", "campaign", L["id"], c["status"], L["status"], notes, "campaign")
        _adopted(not c["id"], n0, ops, f"campaign {c['name']!r}")
        live_ags = {g["id"]: g for g in tree.get("ad_groups", [])}
    by_name, name_matched_ags = {}, []
    for g in live_ags.values():
        by_name.setdefault(g.get("name"), []).append(g)
    for ag in st["ad_groups"]:
        ref = "ag:" + ag["key"]
        lg = live_ags.get(ag["id"]) if ag["id"] else None
        if ag["id"] and not lg:
            raise RailViolation(f"ad group id {ag['id']} ({ag['key']}) was not found live in the campaign (archived or deleted?)")
        ag_by_name = False
        if not lg and not ag["id"]:
            hits = by_name.get(ag["name"], [])
            ag_by_name = bool(hits)
            if ag_by_name:
                name_matched_ags.append(ref)
            if len(hits) > 1:
                raise RailViolation(f"{len(hits)} live ad groups are named {ag['name']!r}; set the id in the state file")
            lg = hits[0] if hits else None
        bbody = _bidding_body(ag["bidding"], c["bidding_type"])
        if not lg:
            if ag["status"] == "archived":
                raise RailViolation(f"refusing to create ad group {ag['key']} archived")
            body = {"name": ag["name"], "status": "paused", "bidding_config": bbody,
                    "context_hints": ag["context_hints"]}
            ops.append({"op": "create", "kind": "ad_group", "ref": ref, "parent": "campaign",
                        "parent_id": live_c["id"] if live_c else None, "body": body,
                        "idempotency_key": idem_key(aid, key, ref, body)})
            notes.append(f"ad group {ag['key']}: create PAUSED")
            live_ads = {}
        else:
            G = norm_ad_group(lg)
            n0 = len(ops)
            upd = {}
            if G["name"] != ag["name"]:
                upd["name"] = ag["name"]
            if G["bidding"] != ag["bidding"]:
                upd["bidding_config"] = bbody
                notes.append(f"ad group {ag['key']}: bidding {G['bidding']} -> {ag['bidding']}")
            if G["context_hints"] != ag["context_hints"]:
                upd["context_hints"] = ag["context_hints"]
                notes.append(f"ad group {ag['key']}: context hints changed")
            if upd:
                ops.append({"op": "update", "kind": "ad_group", "ref": ref, "id": G["id"], "body": upd,
                            "prev": {"bidding": G["bidding"]}})
            ops += _state_ops("ad_group", ref, G["id"], ag["status"], G["status"], notes, f"ad group {ag['key']}")
            _adopted(ag_by_name, n0, ops, f"ad group {ag['key']!r}")
            live_ads = {a["id"]: a for a in lg.get("ads", [])}
        ad_names = {}
        for a in live_ads.values():
            ad_names.setdefault(a.get("name"), []).append(a)
        for ad in ag["ads"]:
            aref = f"ad:{ag['key']}/{ad['key']}"
            la = live_ads.get(ad["id"]) if ad["id"] else None
            if ad["id"] and not la:
                raise RailViolation(f"ad id {ad['id']} ({ad['key']}) was not found live in ad group {ag['key']} (archived or deleted?)")
            ad_by_name = False
            if not la and not ad["id"]:
                hits = ad_names.get(ad["name"], [])
                ad_by_name = bool(hits)
                if len(hits) > 1:
                    raise RailViolation(f"{len(hits)} live ads are named {ad['name']!r}; set the id in the state file")
                la = hits[0] if hits else None
            if not la:
                if ad["status"] == "archived":
                    raise RailViolation(f"refusing to create ad {ad['key']} archived")
                if not ad["file_id"]:
                    raise RailViolation(f"ad {ad['key']}: creative.file_id is required to create a chat_card; upload a "
                                        ">=640x640 image with adsops_openai_upload_image and put its file_id in the state")
                body = {"name": ad["name"], "status": "paused", "creative": _creative(ad)}
                ops.append({"op": "create", "kind": "ad", "ref": aref, "parent": ref,
                            "parent_id": lg["id"] if lg else None, "body": body,
                            "idempotency_key": idem_key(aid, key, aref, body)})
                notes.append(f"ad {ag['key']}/{ad['key']}: create PAUSED (goes to OpenAI review)")
            else:
                A = norm_ad(la)
                n0 = len(ops)
                upd = {}
                if A["name"] != ad["name"]:
                    upd["name"] = ad["name"]
                if any(A[k] != ad[k] for k in ("title", "body", "target_url", "file_id")):
                    upd["creative"] = _creative(ad)
                    notes.append(f"ad {ag['key']}/{ad['key']}: creative changed (re-enters OpenAI review)")
                if upd:
                    ops.append({"op": "update", "kind": "ad", "ref": aref, "id": A["id"], "body": upd})
                ops += _state_ops("ad", aref, A["id"], ad["status"], A["status"], notes, f"ad {ag['key']}/{ad['key']}")
                _adopted(ad_by_name, n0, ops, f"ad {ag['key']}/{ad['key']}")
            if not la and ad["status"] == "active":
                ops.append({"op": "activate", "kind": "ad", "ref": aref, "id": None})
        if not lg and ag["status"] == "active":
            ops.append({"op": "activate", "kind": "ad_group", "ref": ref, "id": None})
    if not live_c and c["status"] == "active":
        ops.append({"op": "activate", "kind": "campaign", "ref": "campaign", "id": None})
    if live_c and not c["id"] and ops:
        raise RailViolation(f"campaign {c['name']!r} was matched by name only; record its id in the state file "
                            "before AdRails changes anything in it")
    for ref in name_matched_ags:
        if any(o.get("parent") == ref for o in ops):
            raise RailViolation(f"ad group {ref} was matched by name only; record its id before adding ads to it")
    ops.sort(key=lambda o: (ORDER[o["op"]], KIND_ORDER[o["kind"]] if o["op"] != "activate" else -KIND_ORDER[o["kind"]]))
    if len(ops) > pol["caps"]["max_ops_per_run"]:
        raise RailViolation(f"{len(ops)} ops exceed policy max_ops_per_run {pol['caps']['max_ops_per_run']}")
    info = exposure(st, ops, tree)
    return ops, notes, info


def exposure(st, ops, tree):
    """Which ops add spend exposure, and whether the campaign ends up able to deliver."""
    inc = []
    for o in ops:
        if o["op"] == "activate":
            inc.append(f"activate {o['kind']} {o['ref']}")
        elif o["op"] == "update":
            b, prev = o["body"], o.get("prev") or {}
            if "budget" in b:
                inc.append("campaign budget change")
            if "targeting" in b or "end_time" in b or "start_time" in b:
                inc.append("campaign targeting/schedule change")
            if "bidding_config" in b:
                inc.append(f"bid change in {o['ref']}")
    live_c = tree.get("campaign")
    ends_active = st["campaign"]["status"] == "active"
    return {"exposure_increases": inc, "campaign_ends_active": ends_active,
            "spend_gate": bool(inc) and ends_active,
            "live_campaign_status": live_c.get("status") if live_c else None}


# ── validation ───────────────────────────────────────────────────────────────────────────────
def validate_ops(ops):
    """Local schema checks mirroring the published OpenAPI limits. -> list of problems ([] = ok)."""
    bad = []
    for o in ops:
        b = o.get("body") or {}
        if o["op"] in ("create", "update"):
            if "status" in b and (o["op"] == "update" or b["status"] != "paused"):
                bad.append(f"{o['ref']}: status may only be 'paused' on create")
            if "name" in b and not 3 <= len(b["name"]) <= 1000:
                bad.append(f"{o['ref']}: name length")
            bud = b.get("budget")
            if bud is not None and (len(bud) != 1 or any(not isinstance(v, int) or v < 1_000_000 for v in bud.values())):
                bad.append(f"{o['ref']}: budget must be one integer >= 1,000,000 micros")
            bc = b.get("bidding_config")
            if bc is not None and bc.get("strategy") == "fixed_bid" and not (
                    isinstance(bc.get("max_bid_micros"), int) and 1 <= bc["max_bid_micros"] <= 30_400_000_000_000):
                bad.append(f"{o['ref']}: max_bid_micros out of range")
            cr = b.get("creative")
            if cr is not None:
                if not 3 <= len(cr.get("title", "")) <= 50 or len(cr.get("body", "")) > 100 \
                        or len(cr.get("target_url", "")) > 2048:
                    bad.append(f"{o['ref']}: creative title 3-50, body <=100, url <=2048")
            if len(b.get("context_hints") or []) > 2000:
                bad.append(f"{o['ref']}: too many context hints")
        if o["op"] == "create" and not o.get("idempotency_key"):
            bad.append(f"{o['ref']}: create without idempotency key")
    return bad


def bulk_operations(ops):
    """Translate the ops the Bulk API can express (campaign/ad group/ad updates) for validate_only.
    -> (operations, skipped_refs). Creates are skipped: the bulk create schema cannot carry file ids,
    bidding_type or bid strategy, so it would validate a different request than the one we send."""
    out, skipped = [], []
    for i, o in enumerate(ops):
        b = o.get("body") or {}
        if o["op"] == "update" and o["kind"] == "campaign" and set(b) <= {"name", "budget", "end_time", "start_time"}:
            inp = {k: b[k] for k in ("name", "end_time", "start_time") if k in b}
            if "budget" in b:
                (bt, m), = b["budget"].items()
                inp.update(max_budget_micros=m, budget_type="daily" if bt.startswith("daily") else "lifetime")
            out.append({"operation_id": f"op{i}", "type": "campaign.update", "target_resource_id": o["id"], "input": inp})
        elif o["op"] == "update" and o["kind"] == "ad_group" and set(b) <= {"name", "context_hints", "bidding_config"} \
                and (b.get("bidding_config") or {}).get("strategy", "fixed_bid") == "fixed_bid":
            inp = {k: b[k] for k in ("name", "context_hints") if k in b}
            if "bidding_config" in b:
                inp["max_bid_micros"] = b["bidding_config"]["max_bid_micros"]
            out.append({"operation_id": f"op{i}", "type": "ad_group.update", "target_resource_id": o["id"], "input": inp})
        elif o["op"] in ("pause", "activate", "archive") and o.get("id"):
            st = {"pause": "paused", "activate": "active", "archive": "archived"}[o["op"]]
            out.append({"operation_id": f"op{i}", "type": o["kind"] + ".update", "target_resource_id": o["id"],
                        "input": {"status": st}})
        else:
            skipped.append(o["ref"] + ":" + o["op"])
    return out, skipped


# ── execute ──────────────────────────────────────────────────────────────────────────────────
def execute(client, ops, allowed, before_activate=None):
    """Run ops in order. Stops at the first failure and says exactly what landed. Never retries a
    whole plan; the client retries a single request only on 429/5xx/network (creates are idempotent).

    `allowed` is the set of op kinds this caller may run (the guard passes {"pause"}); anything else is
    refused here too, so the rails do not depend on every caller building ops correctly.
    `before_activate()` runs once, right before the first activation, to re-check spend."""
    ids, done, checked = {}, [], False
    for o in ops:
        if o.get("op") not in allowed:
            raise RailViolation(f"op {o.get('op')!r} on {o.get('ref')} is not allowed on this path "
                                f"(allowed: {sorted(allowed)}). Landed before it: {done or 'nothing'}")
        if o["op"] == "activate" and before_activate and not checked:
            before_activate()
            checked = True
        try:
            if o["op"] == "create":
                body = dict(o["body"])
                if body.get("status") != "paused":          # explicit, not assert: must survive python -O
                    raise ToolError("creates must be paused")
                if o["kind"] == "ad_group":
                    body["campaign_id"] = o.get("parent_id") or ids["campaign"]
                elif o["kind"] == "ad":
                    body["ad_group_id"] = o.get("parent_id") or ids[o["parent"]]
                r = client.create(o["kind"], body, o["idempotency_key"])
                if not r.get("id"):
                    raise ToolError(f"create {o['ref']} returned no id")
                if r.get("status") not in (None, "paused"):
                    raise ToolError(f"create {o['ref']} came back {r.get('status')!r}, not paused")
                ids[o["ref"]] = r["id"]
            elif o["op"] == "update":
                client.update(o["kind"], o["id"], o["body"])
            else:
                rid = o.get("id") or ids[o["ref"]]
                client.set_state(o["kind"], rid, o["op"])
            done.append(f"{o['op']} {o['ref']}")
        except (ToolError, KeyError) as e:
            raise ToolError(f"stopped at {o['op']} {o['ref']}: {e}. Landed before it: {done or 'nothing'}. "
                            f"Created ids: {ids or 'none'}") from None
    return {"done": done, "created_ids": ids}


def guard_plan(pol, tree):
    """Pause-only, and independent of the desired-state file. -> (ops, reasons).

    Uses the LIVE budgets/bids so an outside edit cannot dodge it; spend is summed over every campaign
    tagged for this key; an active tagged campaign the state does not manage (an orphan) is paused;
    with an account cap, every active campaign in the account is paused once the account is over it."""
    caps, ops, why = pol["caps"], [], []
    c = tree.get("campaign")
    L = norm_campaign(c) if c else None
    budget = L["budget"] if L else {"daily_usd": 0.0}
    targets = {}
    if c and c.get("status") == "active":
        pause, w = oai_policy.guard_decision(budget, pol, tree["spend_today"], tree["spend_life"])
        if budget.get("daily_usd") is not None and budget["daily_usd"] > caps["max_daily_budget_usd"]:
            pause, w = True, w + [f"live daily budget ${budget['daily_usd']} is over the policy cap"]
        if budget.get("lifetime_usd") is not None and budget["lifetime_usd"] > caps["max_lifetime_budget_usd"]:
            pause, w = True, w + ["live lifetime budget is over the policy cap"]
        for g in tree.get("ad_groups", []):
            G = norm_ad_group(g)
            if G["status"] == "active" and (G["bidding"]["max_bid_usd"] is None or G["bidding"]["max_bid_usd"] > caps["max_bid_usd"]):
                pause, w = True, w + [f"live ad group {G['name']!r} bid is over the policy cap or uncapped"]
        if pause:
            targets[c["id"]] = w
    for s in tree.get("siblings", []):
        if s.get("status") == "active":
            targets[s["id"]] = [f"campaign {s['id']} is tagged for this key but not the one the state manages (orphan)"]
    apa = oai_policy.account_pause_at(budget, pol)
    if apa is not None and tree.get("account_spend") is not None and tree["account_spend"] >= apa:
        for x in tree.get("all_campaigns", []):
            if x.get("status") == "active":
                targets.setdefault(x["id"], []).append(
                    f"account spend ${tree['account_spend']:.2f} >= account pause point ${apa:.2f}")
    for cid, w in targets.items():
        ops.append({"op": "pause", "kind": "campaign", "ref": "campaign:" + cid, "id": cid})
        why += [f"{cid}: {x}" for x in w]
    if not ops:
        return [], ["within caps" if c and c.get("status") == "active" else
                    "campaign not active (or not created): nothing to guard"]
    return ops, why
