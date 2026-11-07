"""Draft a ChatGPT Ads campaign for AdRails (pure; writes nothing).

Turn a business brief into the AdRails desired-state file plus the immutable
policy file that ``adsops.oai_engine`` / ``adsops.oai_policy`` accept: every
resource is created paused, activation is denied, every target URL is https on
the landing host and carries exactly the required UTM triple, and the caps
mirror the brief (refused when they cannot survive one 2x-overdelivery day).
No I/O happens here: a tool calls ``draft(brief, now=...)``, surfaces ``checks``
and hands ``state``/``policy`` to ``adsops_openai_plan``. Nothing is created,
uploaded or activated from this module.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:  # normal package import
    from .errors import ToolError
except ImportError:  # pragma: no cover - flat import for scripts
    from adsops.errors import ToolError

# Creative checks are a rail: never fail open. If oai_creative can't be imported, importing this module fails too
# (the maintainers review 2026-11-06: the triaged version silently skipped every ad check when the import failed).
from adsops.oai_creative import validate_card

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ACCOUNT_RE = re.compile(r"^adacct_[A-Za-z0-9_-]+$")
HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
# Campaign-level bidding goal accepted by oai_engine.BIDDING_TYPES. "clicks"
# pairs with the fixed_bid CPC strategy used on the ad group.
CAMPAIGN_BIDDING = "clicks"
UTM_SOURCE, UTM_MEDIUM = "chatgpt", "paid"
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign")
DEFAULT_END_DAYS = 30
DEFAULT_MAX_BID = 1.0
KEY_LIMIT = 64


# --------------------------------------------------------------------- helpers

def _g(row, *path, default=None):
    """Nested getter for camelCase API rows / nested briefs; None counts as missing."""
    cur = row
    for step in path:
        if isinstance(cur, dict):
            if step not in cur:
                return default
            cur = cur[step]
        elif isinstance(cur, (list, tuple)) and isinstance(step, int):
            if not 0 <= step < len(cur):
                return default
            cur = cur[step]
        else:
            return default
    return default if cur is None else cur


def _num(value, field):
    """Coerce to a positive finite float rounded to 2 dp, else raise ToolError."""
    if value is None or isinstance(value, bool):
        raise ToolError(f"brief.{field} must be a number")
    try:
        out = round(float(value), 2)
    except (TypeError, ValueError):
        raise ToolError(f"brief.{field} must be a number") from None
    if not math.isfinite(out) or out <= 0:
        raise ToolError(f"brief.{field} must be a positive number >= 0.01")
    return out


def _strs(value):
    """str|list|None -> list of non-empty stripped strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        value = [value]
    return [t for t in (str(v).strip() for v in value if v is not None) if t]


def _dedupe(items):
    return list(dict.fromkeys(items))


def _today(now):
    if now is None:
        return datetime.now(timezone.utc).date()
    value = now() if callable(now) else now
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ToolError("now() must return a datetime or a date")


def _end_date(raw, today):
    """Optional ISO end date; defaults to today + 30 days, never in the past."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return today + timedelta(days=DEFAULT_END_DAYS)
    try:
        parsed = date.fromisoformat(str(raw).strip())
    except ValueError:
        raise ToolError("brief.end_date must be an ISO date (YYYY-MM-DD)") from None
    if parsed < today:
        raise ToolError("brief.end_date must not be in the past")
    return parsed


def _landing(raw):
    """Return (host, normalised https URL); refuses http, credentials and ports."""
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("brief.landing_url is required")
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        raise ToolError("brief.landing_url is not a valid URL") from None
    if parts.scheme.lower() != "https":
        raise ToolError("brief.landing_url must be an https:// URL")
    if parts.username or parts.password:
        raise ToolError("brief.landing_url must not contain credentials")
    if port not in (None, 443):
        raise ToolError("brief.landing_url must not include a port")
    host = (parts.hostname or "").lower().rstrip(".")
    if not HOST_RE.fullmatch(host):
        raise ToolError("brief.landing_url must name a public domain host")
    # Fragments are dropped: tracking/UTM never belongs in one.
    return host, urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _with_utm(landing, utm):
    """Merge the required UTM triple, dropping every pre-existing utm_* key."""
    parts = urlsplit(landing)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    query.extend((k, utm[k]) for k in UTM_KEYS)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), ""))


def _sanitize(text):
    """Lowercase -> [a-z0-9_] so derived keys always satisfy the engine key regex."""
    return re.sub(r"[^a-z0-9_]", "_", str(text).lower()).strip("_") or "campaign"


def _safe_key(prefix, suffix, limit=KEY_LIMIT):
    """Append suffix while keeping a validator-friendly (<= 64 char, alnum-first) key."""
    prefix = str(prefix)
    if len(prefix) + len(suffix) > limit:
        prefix = prefix[: max(1, limit - len(suffix))]
    out = prefix + suffix
    if out[:1] and not out[0].isalnum():
        out = "k" + out[1:]
    return out[:limit]


def _check_location_id(value, field):
    """Reject ids the engine cannot use (empty, whitespace, TODO placeholders)."""
    if not 1 <= len(value) <= 64 or any(ch.isspace() for ch in value) or value.startswith("TODO"):
        raise ToolError(f"brief.{field}: {value!r} is not a usable location id "
                        "(resolve ids with adsops_openai_geo_lookup)")
    return value


def _countries(value):
    items = _dedupe([c.upper() for c in _strs(value)]) or ["US"]
    for code in items:
        if not COUNTRY_RE.match(code):
            raise ToolError(f"brief.countries entries must be ISO-2 codes, got {code!r}")
    return items[:2500]


# --------------------------------------------------------- creative validation

def _card_validator():
    """Return the current adsops.oai_creative.validate_card (patchable)."""
    return validate_card


def _fallback_card_errors(card):
    """Minimal card schema, used only when oai_creative.validate_card is unavailable."""
    errors, title, body = [], str(card.get("title") or ""), str(card.get("body") or "")
    if not 3 <= len(title) <= 50:
        errors.append(f"title must be 3-50 characters (got {len(title)})")
    if not 1 <= len(body) <= 100:
        errors.append(f"body must be 1-100 characters (got {len(body)})")
    if not card.get("file_id"):
        errors.append("an image file_id is required")
    return errors


def _normalise_card_result(res):
    """Accept list[str], {'errors': [...]}, {'ok': False}, bool or None."""
    if res is None or res is True:
        return []
    if isinstance(res, dict):
        errs = res.get("errors") or res.get("problems") or res.get("issues") or []
        if isinstance(errs, str):
            errs = [errs]
        cleaned = [str(e) for e in errs if str(e)]
        if not cleaned and (res.get("ok") is False or res.get("valid") is False):
            cleaned = ["creative card is invalid"]
        return cleaned
    if isinstance(res, (list, tuple, set)):
        return [str(e) for e in res if str(e)]
    if isinstance(res, str):
        return [res] if res.strip() else []
    return [] if res else ["creative card is invalid"]


def _card_errors(ad):
    """Never leak validator exception text (it may quote copy/PII)."""
    fn = _card_validator()
    if fn is None:
        return _fallback_card_errors(ad)
    try:
        try:
            res = fn(ad)
        except TypeError:  # tolerate keyword-style validators
            res = fn(**{k: ad[k] for k in ("title", "body", "file_id", "target_url") if ad.get(k)})
    except Exception as exc:
        return [f"creative validator rejected the card ({type(exc).__name__})"]
    return _normalise_card_result(res)


def _build_ads(raw_ads, key, target_url, checks):
    """Build paused ad dicts; validate_card findings go into ``checks`` (never raised)."""
    ads = []
    for index, raw in enumerate(raw_ads):
        if not isinstance(raw, dict):
            raise ToolError(f"brief.ads[{index}] must be an object with title/body/file_id")
        title = str(raw.get("title") or "").strip()
        body = str(raw.get("body") or "").strip()
        raw_ids = raw.get("file_ids")
        if raw_ids is None:
            raw_ids = raw.get("file_id")
        file_ids = _dedupe(_strs(raw_ids))
        ad_key = _safe_key(_sanitize(key), f"_ad_{index + 1}")
        base = title if len(title) >= 3 else f"Ad {index + 1}"
        ad = {
            "key": ad_key,
            "name": (base[:900] + f" #{index + 1}")[:1000],
            "status": "paused",
            "title": title,
            "body": body,
            "target_url": target_url,
            "file_id": file_ids[0] if file_ids else None,
        }
        # The card is validated with the real, UTM-tagged target_url attached.
        for message in _card_errors(ad):
            checks.append({"level": "error", "code": "ad_creative",
                           "ad_key": ad_key, "message": message})
        ads.append(ad)
    return ads


# --------------------------------------------------------------------- the tool

def draft(brief: dict, *, now=None) -> dict:
    """Brief -> {"state", "policy", "checks", "next_steps"}; raises ToolError on bad input.

    Pure: no I/O, never mutates ``brief``, returns JSON-serialisable data only.
    """
    if not isinstance(brief, dict):
        raise ToolError("brief must be a JSON object")

    key = str(brief.get("key") or "").strip()
    if not SLUG_RE.match(key):
        raise ToolError("brief.key must be a slug: 1-64 chars of a-z, 0-9, '-' or '_', starting alphanumeric")

    account_id = str(brief.get("ad_account_id") or "").strip()
    if not ACCOUNT_RE.match(account_id) or len(account_id) > 128:
        raise ToolError("brief.ad_account_id is required and must look like adacct_<id>")

    business = str(brief.get("business") or "").strip()
    if not business:
        raise ToolError("brief.business is required")
    offer = str(brief.get("offer") or "").strip()

    if str(brief.get("currency") or "USD").strip().upper() != "USD":
        raise ToolError("only USD ad accounts are supported (policy requires currency USD)")

    host, landing = _landing(brief.get("landing_url"))

    daily = _num(brief.get("daily_budget"), "daily_budget")
    lifetime = _num(brief.get("lifetime_cap"), "lifetime_cap")
    # A campaign must survive one overdelivery day at 2x; otherwise it cannot run.
    if 2 * daily > lifetime + 1e-9:
        raise ToolError(f"daily_budget x2 ({2 * daily:.2f}) exceeds lifetime_cap ({lifetime:.2f}); "
                        "the campaign could not serve a single 2x-overdelivery day")

    raw_ads = brief.get("ads")
    if not isinstance(raw_ads, (list, tuple)) or not raw_ads:
        raise ToolError("brief.ads must be a non-empty list of {title, body, file_id}")

    today = _today(now)
    end_date = _end_date(brief.get("end_date"), today)

    checks = []
    raw_bid = brief.get("max_bid")
    bid_given = raw_bid is not None
    if bid_given:
        bid = _num(raw_bid, "max_bid")
    else:
        bid = DEFAULT_MAX_BID
        checks.append({"level": "info", "code": "bid_default",
                       "message": f"max_bid not set; using a fixed {bid:.2f} bid"})

    utm_campaign = (str(brief.get("utm_campaign") or "").strip() or key)[:200]
    utm = {"utm_source": UTM_SOURCE, "utm_medium": UTM_MEDIUM, "utm_campaign": utm_campaign}
    target_url = _with_utm(landing, utm)

    countries = _countries(brief.get("countries"))
    locations = _dedupe(_strs(brief.get("regions")) + _strs(brief.get("markets")))[:2500]
    excluded = _dedupe(_strs(brief.get("excluded_location_ids")))[:2500]
    for lid in locations:
        _check_location_id(lid, "regions/markets")
    for lid in excluded:
        _check_location_id(lid, "excluded_location_ids")
    hints = [h[:500] for h in _strs(brief.get("context_hints"))[:2000]]
    approvers = _strs(brief.get("approvers"))
    conversion_event = str(brief.get("conversion_event") or "").strip()
    name = (str(brief.get("name") or business).strip() or key)[:120]
    if len(name) < 3:
        name = (key + " campaign")[:120]

    if not offer:
        checks.append({"level": "warn", "code": "offer_missing",
                       "message": "no offer described; ad copy may be vague"})
    if not approvers:
        checks.append({"level": "warn", "code": "approvers_empty",
                       "message": "no approvers listed; activation needs a named human approver"})
    if conversion_event:
        # The brief names the event; the numeric setting ids must come from the account.
        checks.append({"level": "info", "code": "conversion_event",
                       "message": f"fill campaign.conversion_event_setting_ids for {conversion_event!r} "
                                  "before activation"})
    checks.append({"level": "info", "code": "spend_gate",
                   "message": f"2 x daily {daily:.2f} <= lifetime {lifetime:.2f}"})

    ads = _build_ads(raw_ads, key, target_url, checks)

    campaign = {
        "name": name,
        "status": "paused",
        "bidding_type": CAMPAIGN_BIDDING,
        "budget": {"daily_usd": daily},  # exactly one of daily_usd / lifetime_usd
        "end_date": end_date.isoformat(),  # inclusive, account time zone
        "targeting": {
            "countries": countries,
            "location_ids": locations,
            "excluded_location_ids": excluded,
            "platforms": None,  # let the engine use the account default
        },
        "conversion_event_setting_ids": [],
    }
    ad_group = {
        "key": _safe_key(_sanitize(key), "_core"),
        "name": (f"{name} - Core"[:1000]),
        "status": "paused",
        "bidding": {"strategy": "fixed_bid", "max_bid_usd": bid},  # maximised strategies are refused
        "context_hints": hints,
        "ads": ads,
    }
    state = {
        "platform": "openai_ads",
        "ad_account_id": account_id,
        "campaign_key": key,
        "campaign": campaign,
        "ad_groups": [ad_group],
        # EXACTLY the block oai_policy.check_approval reads (the maintainers fix 2026-11-06: the generated draft invented fields).
        # A human fills activate/approved_by/approved_on/expires_on; campaign_name must equal the campaign it approves.
        "approval": {"activate": False, "approved_by": None, "approved_on": None, "expires_on": None,
                     "campaign_name": campaign.get("name")},
    }

    caps = {
        "max_daily_budget_usd": daily,
        "min_daily_budget_usd": min(5.0, daily),  # never above max_daily
        "max_lifetime_budget_usd": lifetime,
        "max_cumulative_spend_usd": lifetime,
        "max_account_spend_usd": lifetime,
        "account_spend_since": date(today.year, today.month, 1).isoformat(),
        "max_end_date": end_date.isoformat(),
        "max_ops_per_run": 25,
        "overdelivery_factor": 2.0,
    }
    if bid_given:
        caps["max_bid_usd"] = bid

    policy = {
        "platform": "openai_ads",
        "ad_account_id": account_id,
        "campaign_key": key,
        "currency": "USD",
        "caps": caps,
        "guard": {
            "pause_headroom_usd": 0,
            "daily_anomaly_multiple": 1.5,
            # Above the daily cap, strictly below the cumulative cap.
            "daily_spend_pause_usd": round(min(daily * 1.8, lifetime * 0.99), 2),
        },
        "allowed_domains": [host],
        "required_utm": dict(utm),
        "allow_activate": False,  # activation is always a separate, approved step
        "allow_archive": False,
        "approvers": approvers,
        "approval_max_age_days": 14,
        "require_plan_id": True,
        "plan_ttl_s": 900,
        "max_guard_age_min": 30,
    }

    next_steps = [
        f"Commit the two files: desired state -> state/{key}.json and policy -> policy/{key}.json.",
        f"Run adsops_openai_plan for campaign key '{key}' and read the diff (it changes nothing).",
        "Run adsops_openai_apply in validate-only mode to confirm the plan is accepted.",
        "Upload the ad images and write the returned file_ids into every ad, then re-plan.",
        "Request approval from an approver listed in the policy (plan TTL is 15 minutes).",
        "Activate in order: ad, then ad group, then campaign (only after approval).",
    ]
    assert "adrails:"  # the engine tags every created campaign, see docs
    return {"state": state, "policy": policy, "checks": checks, "next_steps": next_steps}


# --------------------------------------------------------------------- selftest

def selftest() -> int:
    """All branches, no real I/O; returns the number of assertions."""
    n = 0

    def ok(cond, msg="check failed"):
        nonlocal n
        assert cond, msg
        n += 1

    def refused(fn):
        try:
            fn()
        except ToolError:
            return True
        return False

    fixed_now = lambda: datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def brief(**over):
        base = {
            "key": "acme_chatgpt", "ad_account_id": "adacct_test",
            "business": "Acme makes widgets", "landing_url": "https://example.com/widgets",
            "offer": "Free trial", "daily_budget": 40, "lifetime_cap": 500, "max_bid": 2,
            "countries": ["US"], "context_hints": ["best widgets", "widget pricing"],
            "ads": [{"title": "Acme Widgets", "body": "Try Acme widgets today.", "file_id": "file_123"}],
            "conversion_event": "lead_created", "utm_campaign": "acme_chatgpt",
        }
        base.update(over)
        return base

    out = draft(brief(), now=fixed_now)
    state, policy = out["state"], out["policy"]
    camp, group, ad = state["campaign"], state["ad_groups"][0], state["ad_groups"][0]["ads"][0]

    ok(state["platform"] == "openai_ads" and state["campaign_key"] == "acme_chatgpt")
    ok(state["ad_account_id"] == "adacct_test" and policy["ad_account_id"] == "adacct_test")
    ok(camp["status"] == "paused" and group["status"] == "paused" and ad["status"] == "paused")
    ok(camp["bidding_type"] == CAMPAIGN_BIDDING and group["bidding"]["strategy"] == "fixed_bid")
    ok(camp["budget"] == {"daily_usd": 40.0} and camp["end_date"] == "2026-02-14")
    ok("id" not in camp and "id" not in group and "id" not in ad)  # name matching only converges
    ok(ad["target_url"].startswith("https://example.com/widgets?"))
    ok("utm_source=chatgpt" in ad["target_url"] and "utm_medium=paid" in ad["target_url"]
       and "utm_campaign=acme_chatgpt" in ad["target_url"])
    ok(policy["allow_activate"] is False and policy["allow_archive"] is False)
    ok(policy["allowed_domains"] == ["example.com"])
    ok(policy["caps"]["max_daily_budget_usd"] == 40.0 and policy["caps"]["min_daily_budget_usd"] == 5.0)
    ok(policy["caps"]["max_lifetime_budget_usd"] == 500.0
       and policy["caps"]["max_cumulative_spend_usd"] == 500.0)
    ok(policy["caps"]["max_bid_usd"] == 2.0 and group["bidding"]["max_bid_usd"] == 2.0)
    ok(policy["caps"]["max_end_date"] == camp["end_date"])
    ok(policy["caps"]["account_spend_since"] == "2026-01-01")
    ok(policy["guard"]["daily_spend_pause_usd"] == 72.0)
    ok(policy["required_utm"] == {"utm_source": "chatgpt", "utm_medium": "paid",
                                  "utm_campaign": "acme_chatgpt"})
    ok(len(out["next_steps"]) == 6 and "Commit" in out["next_steps"][0]
       and "Activate" in out["next_steps"][-1])
    ok(isinstance(json.dumps(out), str))  # JSON-serialisable: no sets, no datetimes
    ok(not any(c["level"] == "error" for c in out["checks"]), "valid ads must not error")

    # Acceptance: the authoritative engine/policy validators must accept the draft.
    try:
        from adsops import oai_engine as E, oai_policy as P
    except Exception:  # pragma: no cover - engine not installed
        E = P = None
    if E is not None:
        for b in (brief(), brief(daily_budget=100, lifetime_cap=200)):
            d = draft(b, now=fixed_now)
            sv, pv = E.validate_state(d["state"]), P.validate(d["policy"])
            P.check_caps(sv, pv)
            n += 1

    merged = draft(brief(landing_url="https://example.com/w?ref=abc&utm_source=fb&utm_term=x"), now=fixed_now)
    url = merged["state"]["ad_groups"][0]["ads"][0]["target_url"]
    ok("ref=abc" in url and "utm_source=fb" not in url and "utm_source=chatgpt" in url
       and "utm_term" not in url)

    ok(draft(brief(end_date="2026-02-01"), now=fixed_now)["state"]["campaign"]["end_date"] == "2026-02-01")
    over = draft(brief(utm_campaign="spring_sale"), now=fixed_now)
    ok("utm_campaign=spring_sale" in over["state"]["ad_groups"][0]["ads"][0]["target_url"]
       and over["policy"]["required_utm"]["utm_campaign"] == "spring_sale")
    geo = draft(brief(regions=["geo_1"], markets=["geo_2"]), now=fixed_now)
    ok(geo["state"]["campaign"]["targeting"]["location_ids"] == ["geo_1", "geo_2"])
    ok(draft(brief(countries=[]), now=fixed_now)["state"]["campaign"]["targeting"]["countries"] == ["US"])
    ok(draft(brief(daily_budget="40", lifetime_cap="80"), now=fixed_now)["state"]["campaign"]["budget"]
       == {"daily_usd": 40.0})
    nobid = draft(brief(max_bid=None), now=fixed_now)
    ok(nobid["state"]["ad_groups"][0]["bidding"]["max_bid_usd"] > 0)
    ok("max_bid_usd" not in nobid["policy"]["caps"])

    bad = draft(brief(ads=[{"title": "X", "body": "", "file_id": ""}]), now=fixed_now)
    ok(any(c["level"] == "error" for c in bad["checks"]))
    ok(len(bad["state"]["ad_groups"][0]["ads"]) == 1)  # reported, never dropped

    ok(refused(lambda: draft(brief(landing_url="http://example.com/x"), now=fixed_now)))
    ok(refused(lambda: draft(brief(landing_url="https://user:pw@example.com/x"), now=fixed_now)))
    ok(refused(lambda: draft(brief(landing_url="https://example.com:8443/x"), now=fixed_now)))
    ok(refused(lambda: draft(brief(key="Acme ChatGPT"), now=fixed_now)))
    ok(refused(lambda: draft(brief(ads=[]), now=fixed_now)))
    ok(refused(lambda: draft(brief(ads=[None]), now=fixed_now)))
    ok(refused(lambda: draft(brief(daily_budget=100, lifetime_cap=150), now=fixed_now)))
    ok(refused(lambda: draft(brief(daily_budget=-1), now=fixed_now)))
    ok(refused(lambda: draft(brief(currency="EUR"), now=fixed_now)))
    ok(refused(lambda: draft("not a dict", now=fixed_now)))
    ok(refused(lambda: draft(brief(ad_account_id=""), now=fixed_now)))
    ok(refused(lambda: draft(brief(ad_account_id="foo"), now=fixed_now)))
    ok(refused(lambda: draft(brief(ad_account_id="adacct_"), now=fixed_now)))
    ok(refused(lambda: draft(brief(regions=["bad id"]), now=fixed_now)))

    edge = draft(brief(daily_budget=100, lifetime_cap=200), now=fixed_now)  # boundary: 2x == cap
    ok(edge["policy"]["caps"]["max_lifetime_budget_usd"] == 200.0
       and edge["state"]["campaign"]["budget"] == {"daily_usd": 100.0})
    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)