"""OpenAI Ads policy file + cap math. The policy file is the ONLY source of caps; no tool writes it.

Shape (USD; amounts are major units, converted to micros only at the API edge):
{
  "platform": "openai_ads", "ad_account_id": "adacct_...", "campaign_key": "bt_chatgpt_owners",
  "currency": "USD",
  "caps": {"max_daily_budget_usd": 40, "max_lifetime_budget_usd": 500, "max_cumulative_spend_usd": 500,
           "max_bid_usd": 5, "max_end_date": "2026-11-30", "max_ops_per_run": 25,
           "overdelivery_factor": 2.0, "min_daily_budget_usd": 5},
  "guard": {"pause_headroom_usd": 72, "daily_anomaly_multiple": 1.5, "daily_spend_pause_usd": 72},
  "allowed_domains": ["acmepm.example"],
  "required_utm": {"utm_source": "chatgpt", "utm_medium": "paid", "utm_campaign": "{campaign_key}"},
  "allow_activate": false, "allow_archive": false, "approvers": ["Jane Approver"],
  "approval_max_age_days": 14, "require_plan_id": true, "plan_ttl_s": 900, "max_guard_age_min": 30
}

Cap math (one formula shared by activation and the guard, so they cannot ping-pong):
  worst_day   = daily_budget * overdelivery_factor        (lifetime budget: 0, the platform stops at it)
  pause_at    = max_cumulative_spend - max(pause_headroom, worst_day)
  activation  allowed only while spend_life < pause_at and spend_today < daily_pause_at
  guard       pauses when spend_life >= pause_at or spend_today >= daily_pause_at
  daily_pause_at = min(daily_budget * daily_anomaly_multiple, guard.daily_spend_pause_usd if set)
"""
import copy
import datetime as dt
import re

from . import strict
from .errors import RailViolation, ToolError

DEFAULTS_GUARD = {"pause_headroom_usd": 0.0, "daily_anomaly_multiple": 1.5}


def _num(d, key, where, minimum=0.0, required=True, positive=False):
    if key not in d:
        if required:
            raise ToolError(f"policy: {where}['{key}'] is required")
        return None
    strict.finite_number(d[key], f"policy: {where}['{key}']", minimum, allow_equal_min=not positive)
    return float(d[key])


def validate(raw):
    if not isinstance(raw, dict):
        raise ToolError("policy: top level must be a JSON object")
    pol = copy.deepcopy(raw)
    if pol.get("platform") != "openai_ads":
        raise ToolError("policy: 'platform' must be \"openai_ads\"")
    for k in ("ad_account_id", "campaign_key"):
        if not isinstance(pol.get(k), str) or not pol[k].strip():
            raise ToolError(f"policy: '{k}' must be a non-empty string")
    if pol.get("currency", "USD") != "USD":
        raise ToolError("policy: only currency 'USD' is supported")
    pol["currency"] = "USD"
    caps = pol.get("caps")
    if not isinstance(caps, dict):
        raise ToolError("policy: 'caps' must be an object")
    _num(caps, "max_daily_budget_usd", "caps", positive=True)
    _num(caps, "max_cumulative_spend_usd", "caps", positive=True)
    _num(caps, "max_bid_usd", "caps", positive=True)
    caps.setdefault("max_lifetime_budget_usd", caps["max_cumulative_spend_usd"])
    _num(caps, "max_lifetime_budget_usd", "caps", positive=True)
    if caps["max_lifetime_budget_usd"] > caps["max_cumulative_spend_usd"]:
        raise ToolError("policy: caps.max_lifetime_budget_usd must not exceed caps.max_cumulative_spend_usd")
    caps.setdefault("overdelivery_factor", 2.0)
    _num(caps, "overdelivery_factor", "caps", minimum=1.0)
    if caps["overdelivery_factor"] > 10:
        raise ToolError("policy: caps.overdelivery_factor must be <= 10")
    if _num(caps, "min_daily_budget_usd", "caps", required=False) is not None \
            and caps["min_daily_budget_usd"] > caps["max_daily_budget_usd"]:
        raise ToolError("policy: caps.min_daily_budget_usd must not exceed caps.max_daily_budget_usd")
    strict.iso_date(caps.get("max_end_date"), "policy: caps['max_end_date']")
    if _num(caps, "max_account_spend_usd", "caps", required=False, positive=True) is not None:
        strict.iso_date(caps.get("account_spend_since"), "policy: caps['account_spend_since'] (needed with max_account_spend_usd)")
    ops = caps.get("max_ops_per_run")
    if isinstance(ops, bool) or not isinstance(ops, int) or ops < 0:
        raise ToolError("policy: caps['max_ops_per_run'] must be an integer >= 0")
    g = pol.get("guard", {})
    if not isinstance(g, dict):
        raise ToolError("policy: 'guard' must be an object")
    g = {**DEFAULTS_GUARD, **g}
    _num(g, "pause_headroom_usd", "guard")
    _num(g, "daily_anomaly_multiple", "guard", minimum=1.0)
    _num(g, "daily_spend_pause_usd", "guard", required=False, positive=True)
    if g["pause_headroom_usd"] >= caps["max_cumulative_spend_usd"]:
        raise ToolError("policy: guard.pause_headroom_usd must be below caps.max_cumulative_spend_usd")
    pol["guard"] = g
    doms = pol.get("allowed_domains")
    if not isinstance(doms, list) or not doms or not all(
            isinstance(d, str) and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d.strip().lower()) for d in doms):
        raise ToolError("policy: 'allowed_domains' must be a non-empty list of hostnames")
    pol["allowed_domains"] = [d.strip().lower() for d in doms]
    utm = pol.get("required_utm", {})
    if not isinstance(utm, dict) or not all(isinstance(k, str) and re.fullmatch(r"[a-z_]{1,40}", k)
                                            and isinstance(v, str) and 0 < len(v) <= 200 for k, v in utm.items()):
        raise ToolError("policy: 'required_utm' must map utm keys to non-empty strings")
    for k in ("allow_activate", "allow_archive", "require_plan_id"):
        default = k == "require_plan_id"
        v = pol.get(k, default)
        if not isinstance(v, bool):
            raise ToolError(f"policy: '{k}' must be a boolean")
        pol[k] = v
    appr = pol.get("approvers", [])
    if not isinstance(appr, list) or not all(isinstance(a, str) and a.strip() for a in appr):
        raise ToolError("policy: 'approvers' must be a list of names")
    pol["approvers"] = [a.strip() for a in appr]
    age = pol.get("max_guard_age_min")
    if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 1):
        raise ToolError("policy: 'max_guard_age_min' must be a positive integer or null")
    if pol["allow_activate"] and (not pol["approvers"] or age is None):
        raise ToolError("policy: allow_activate=true needs a non-empty 'approvers' list and max_guard_age_min")
    mad = pol.get("approval_max_age_days", 14)
    if isinstance(mad, bool) or not isinstance(mad, int) or not 1 <= mad <= 90:
        raise ToolError("policy: 'approval_max_age_days' must be an integer 1..90")
    pol["approval_max_age_days"] = mad
    ttl = pol.get("plan_ttl_s", 900)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 86400:
        raise ToolError("policy: 'plan_ttl_s' must be an integer in 60..86400")
    pol["plan_ttl_s"], pol["max_guard_age_min"] = ttl, age
    return pol


def load(path):
    if not isinstance(path, str):
        raise ToolError("policy: path must be a string")
    import os
    return validate(strict.load_json_file(os.path.expanduser(path), "policy file"))


def required_utm(pol, campaign_key):
    return {k: v.replace("{campaign_key}", campaign_key) for k, v in pol.get("required_utm", {}).items()}


# ── cap math ─────────────────────────────────────────────────────────────────────────────────
def worst_day(budget, pol):
    """Worst-case spend in one day for a budget dict {'daily_usd'|'lifetime_usd': x}."""
    if budget.get("daily_usd") is not None:
        return float(budget["daily_usd"]) * pol["caps"]["overdelivery_factor"]
    return 0.0


def pause_at(budget, pol):
    cap = pol["caps"]["max_cumulative_spend_usd"]
    return round(cap - max(pol["guard"]["pause_headroom_usd"], worst_day(budget, pol)), 6)


def daily_pause_at(budget, pol):
    g = pol["guard"]
    vals = []
    if budget.get("daily_usd") is not None:
        vals.append(float(budget["daily_usd"]) * g["daily_anomaly_multiple"])
    if g.get("daily_spend_pause_usd") is not None:
        vals.append(g["daily_spend_pause_usd"])
    return round(min(vals), 6) if vals else None


def account_pause_at(budget, pol):
    cap = pol["caps"].get("max_account_spend_usd")
    if cap is None:
        return None
    return round(cap - max(pol["guard"]["pause_headroom_usd"], worst_day(budget, pol)), 6)


def guard_decision(budget, pol, spend_today, spend_life):
    """-> (should_pause: bool, reasons[list]). Pure; pausing is the only possible action."""
    why = []
    pa = pause_at(budget, pol)
    if spend_life >= pa:
        why.append(f"lifetime spend ${spend_life:.2f} >= pause point ${pa:.2f} "
                   f"(cap ${pol['caps']['max_cumulative_spend_usd']:.2f} minus headroom)")
    if spend_life >= pol["caps"]["max_cumulative_spend_usd"]:
        why.append("lifetime spend is at or over the hard cap")
    dpa = daily_pause_at(budget, pol)
    if dpa is not None and spend_today >= dpa:
        why.append(f"today's spend ${spend_today:.2f} >= anomaly threshold ${dpa:.2f}")
    return bool(why), why


def check_caps(desired, pol):
    """Static caps on the desired campaign tree. Raises RailViolation naming the cap."""
    caps = pol["caps"]
    b = desired["campaign"]["budget"]
    if b.get("daily_usd") is not None:
        if b["daily_usd"] > caps["max_daily_budget_usd"]:
            raise RailViolation(f"daily budget ${b['daily_usd']} exceeds policy max ${caps['max_daily_budget_usd']}")
        if caps.get("min_daily_budget_usd") is not None and b["daily_usd"] < caps["min_daily_budget_usd"]:
            raise RailViolation(f"daily budget ${b['daily_usd']} is below policy min ${caps['min_daily_budget_usd']}")
    if b.get("lifetime_usd") is not None and b["lifetime_usd"] > caps["max_lifetime_budget_usd"]:
        raise RailViolation(f"lifetime budget ${b['lifetime_usd']} exceeds policy max ${caps['max_lifetime_budget_usd']}")
    end = desired["campaign"].get("end_date")
    if end is None or end > caps["max_end_date"]:
        raise RailViolation(f"campaign end_date {end!r} must be set and <= policy max_end_date {caps['max_end_date']}")
    for ag in desired["ad_groups"]:
        bid = ag["bidding"].get("max_bid_usd")
        if bid is not None and bid > caps["max_bid_usd"]:
            raise RailViolation(f"ad group {ag['key']!r} bid ${bid} exceeds policy max_bid ${caps['max_bid_usd']}")
        if ag["bidding"]["strategy"] != "fixed_bid" and caps["max_bid_usd"] is not None:
            raise RailViolation(f"ad group {ag['key']!r}: strategy {ag['bidding']['strategy']!r} has no bid ceiling; "
                                "policy max_bid_usd needs fixed_bid")


def _real(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_approval(desired, pol, today):
    """The spend gate. `today` is a date in the account time zone. Raises RailViolation."""
    if not pol["allow_activate"]:
        raise RailViolation("activation refused: policy allow_activate is false")
    a = desired.get("approval") or {}
    if a.get("activate") is not True:
        raise RailViolation("activation refused: the committed state file has no approval.activate=true")
    if a.get("campaign_name") != desired["campaign"]["name"]:
        raise RailViolation("activation refused: approval.campaign_name must equal the campaign name it approves")
    who = a.get("approved_by")
    if who not in pol["approvers"]:
        raise RailViolation(f"activation refused: approval.approved_by {who!r} is not in the policy approvers list")
    try:
        on = dt.date.fromisoformat(a.get("approved_on") or "")
        exp = dt.date.fromisoformat(a.get("expires_on") or "")
    except ValueError:
        raise RailViolation("activation refused: approval needs approved_on and expires_on (YYYY-MM-DD)") from None
    if on > today:
        raise RailViolation("activation refused: approval is dated in the future")
    if today > exp:
        raise RailViolation(f"activation refused: approval expired on {exp}")
    if (exp - on).days > pol["approval_max_age_days"]:
        raise RailViolation(f"activation refused: approval window is longer than {pol['approval_max_age_days']} days")
    b = desired["campaign"]["budget"]
    for field, val in (("max_daily_budget_usd", b.get("daily_usd")), ("max_lifetime_budget_usd", b.get("lifetime_usd"))):
        if val is not None and not (_real(a.get(field)) and val <= a[field]):
            raise RailViolation(f"activation refused: budget ${val} is above approval.{field} {a.get(field)!r}")
    for ag in desired["ad_groups"]:
        bid = ag["bidding"].get("max_bid_usd")
        if bid is not None and not (_real(a.get("max_bid_usd")) and bid <= a["max_bid_usd"]):
            raise RailViolation(f"activation refused: bid ${bid} in {ag['key']!r} is above approval.max_bid_usd")
    tot = a.get("max_total_spend_usd")
    if not _real(tot) or tot > pol["caps"]["max_cumulative_spend_usd"]:
        raise RailViolation("activation refused: approval.max_total_spend_usd must be set and <= the policy cap")
    return True


def check_activation_room(budget, pol, spend_today, spend_life, account_spend=None):
    apa = account_pause_at(budget, pol)
    if apa is not None:
        if account_spend is None:
            raise RailViolation("activation refused: the policy has an account cap but account spend was not read")
        if account_spend >= apa:
            raise RailViolation(f"activation refused: account spend ${account_spend:.2f} since "
                                f"{pol['caps']['account_spend_since']} is at/over the account pause point ${apa:.2f}")
    pa, dpa = pause_at(budget, pol), daily_pause_at(budget, pol)
    if spend_life >= pa:
        raise RailViolation(f"activation refused: lifetime spend ${spend_life:.2f} leaves no room under the pause point "
                            f"${pa:.2f} (cap minus max(headroom, daily budget x {pol['caps']['overdelivery_factor']}))")
    if dpa is not None and spend_today >= dpa:
        raise RailViolation(f"activation refused: today's spend ${spend_today:.2f} is at/over ${dpa:.2f}; wait for tomorrow")
    return pa


def selftest():
    n = 0
    base = {"platform": "openai_ads", "ad_account_id": "adacct_1", "campaign_key": "k",
            "caps": {"max_daily_budget_usd": 40, "max_cumulative_spend_usd": 500, "max_bid_usd": 5,
                     "max_end_date": "2026-11-30", "max_ops_per_run": 25},
            "guard": {"pause_headroom_usd": 50}, "allowed_domains": ["acmepm.example"]}
    p = validate(base)
    assert p["caps"]["overdelivery_factor"] == 2.0 and p["allow_activate"] is False and p["require_plan_id"] is True
    n += 1
    assert pause_at({"daily_usd": 36}, p) == 428.0          # 500 - max(50, 72)
    assert pause_at({"lifetime_usd": 500}, p) == 450.0      # 500 - max(50, 0)
    n += 1
    assert guard_decision({"daily_usd": 36}, p, 10, 428.0)[0] is True
    assert guard_decision({"daily_usd": 36}, p, 10, 427.99)[0] is False
    assert guard_decision({"daily_usd": 36}, p, 54, 0)[0] is True        # 1.5 x 36
    n += 1
    for bad in ({"platform": "google_ads"}, {"caps": {**base["caps"], "max_bid_usd": -1}},
                {"allowed_domains": []}, {"allow_activate": True}):
        try:
            validate({**base, **bad})
            raise AssertionError(bad)
        except ToolError:
            n += 1
    return n
