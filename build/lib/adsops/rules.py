"""Declarative automation rules for AdRails (adsops.rules).

Rules live in the immutable policy file and only ever *propose* desired-state
patches; this module never touches the network, the filesystem or the ad account.
It validates the rule grammar (validate_rules), evaluates conditions against
already-fetched, windowed entity metrics (evaluate) and merges the resulting
patches (to_state_patch).  Rules can only pause, add negatives, or move budgets
*within* policy caps -- anything that would enable/resume exposure is refused.
`now` and `last_fired` are injectable so cooldown behaviour stays deterministic;
a tool should evaluate, show the proposals/alerts, and only then merge the patch.
"""

from __future__ import annotations

from datetime import datetime, timezone

from adsops.errors import ToolError, RailViolation, scrub

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_SCOPES = ("keyword", "campaign", "search_term")
_OPS = (">", ">=", "<", "<=", "==", "!=")
_BASE_METRICS = (
    "cost", "clicks", "impressions", "conversions", "quality_score",
    "impression_share", "lost_budget", "spend_today", "days_since_conv",
)
_DERIVED_METRICS = ("ctr", "cpc", "cpa", "conv_rate")
_METRICS = frozenset(_BASE_METRICS + _DERIVED_METRICS)
_ACTIONS = ("pause", "add_negative", "budget_pct", "alert")
_MONEY_METRICS = frozenset({"cost", "cpc", "cpa", "spend_today", "lost_budget"})
_ENABLE_KEYS = frozenset({"enable", "enabled", "resume", "unpause", "activate", "start"})
_MAX_DEPTH = 5


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Nested getter tolerant of Google-Ads camelCase rows and str/int keys."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        if key in cur:
            cur = cur[key]
        elif str(key) in cur:
            cur = cur[str(key)]
        else:
            return default
    return default if cur is None else cur


def _num(value):
    """Coerce Google-Ads style (string) numbers to float; None when unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fmt(value, metric=None):
    """Human formatting for reason strings ("cost 42.10", "conversions 0")."""
    if value is None:
        return "None"
    if metric in _MONEY_METRICS:
        return f"{value:.2f}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _as_dt(value):
    """Accept datetime/ISO string; always return tz-aware UTC datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _utcnow():
    return datetime.now(timezone.utc)


def _is_enabling(obj):
    """True if a dict would turn exposure ON (pause:false, enable:true, ...)."""
    if not isinstance(obj, dict):
        return False
    for key, val in obj.items():
        low = str(key).lower()
        if low in _ENABLE_KEYS and val:
            return True
        if low == "pause" and val is False:
            return True
        if low == "status" and str(val).upper() in ("ENABLED", "ACTIVE", "ON"):
            return True
    return False


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _check_cond(cond, depth, rid, errs, path="when"):
    if depth > _MAX_DEPTH:
        errs.append(f"rule '{rid}': {path} nested deeper than {_MAX_DEPTH}")
        return
    if not isinstance(cond, dict):
        errs.append(f"rule '{rid}': {path} must be an object")
        return
    if "all" in cond or "any" in cond:
        if "all" in cond and "any" in cond:
            errs.append(f"rule '{rid}': {path} cannot mix 'all' and 'any'")
            return
        key = "all" if "all" in cond else "any"
        extra = set(cond) - {key}
        if extra:
            errs.append(f"rule '{rid}': {path} has unexpected keys {sorted(map(str, extra))}")
        kids = cond.get(key)
        if not isinstance(kids, list) or not kids:
            errs.append(f"rule '{rid}': {path}.{key} must be a non-empty list")
            return
        for i, kid in enumerate(kids):
            _check_cond(kid, depth + 1, rid, errs, f"{path}.{key}[{i}]")
        return
    if "not" in cond:
        extra = set(cond) - {"not"}
        if extra:
            errs.append(f"rule '{rid}': {path} has unexpected keys {sorted(map(str, extra))}")
        _check_cond(cond.get("not"), depth + 1, rid, errs, f"{path}.not")
        return
    extra = set(cond) - {"metric", "op", "value", "window_days"}
    if extra:
        errs.append(f"rule '{rid}': {path} has unexpected keys {sorted(map(str, extra))}")
    metric = cond.get("metric")
    if not isinstance(metric, str) or metric not in _METRICS:
        errs.append(f"rule '{rid}': {path} unknown metric {metric!r}")
    op = cond.get("op")
    if not isinstance(op, str) or op not in _OPS:
        errs.append(f"rule '{rid}': {path} unknown op {op!r}")
    if "value" not in cond or _num(cond.get("value")) is None:
        errs.append(f"rule '{rid}': {path} value must be numeric")
    if "window_days" in cond:
        win = cond["window_days"]
        if isinstance(win, bool) or not isinstance(win, int) or not (1 <= win <= 90):
            errs.append(f"rule '{rid}': {path} window_days must be an int in 1..90")


def _check_action(action, scope, rid, errs):
    if not isinstance(action, dict) or not action:
        errs.append(f"rule '{rid}': 'then' must be a non-empty object")
        return
    if _is_enabling(action):
        errs.append(f"rule '{rid}': action would enable exposure; rules may only pause or reduce")
        return
    if len(action) != 1:
        errs.append(f"rule '{rid}': action must have exactly one key")
        return
    name = next(iter(action))
    val = action[name]
    if name not in _ACTIONS:
        errs.append(f"rule '{rid}': unknown action '{name}'")
        return
    if name == "pause":
        if val is not True:
            errs.append(f"rule '{rid}': pause action must be true (false would enable exposure)")
    elif name == "add_negative":
        if scope != "search_term":
            errs.append(f"rule '{rid}': add_negative is only valid for scope 'search_term'")
        if not isinstance(val, dict):
            errs.append(f"rule '{rid}': add_negative needs an object with match_type")
        elif str(val.get("match_type", "")).upper() not in ("PHRASE", "EXACT"):
            errs.append(f"rule '{rid}': add_negative match_type must be PHRASE or EXACT")
    elif name == "budget_pct":
        if scope != "campaign":
            errs.append(f"rule '{rid}': budget_pct is only valid for scope 'campaign'")
        if _num(val) is None:
            errs.append(f"rule '{rid}': budget_pct must be numeric")
    elif name == "alert":
        if not isinstance(val, str) or not val.strip():
            errs.append(f"rule '{rid}': alert needs a non-empty message")


def validate_rules(rules):
    """Return a list of human-readable problems (empty list == valid)."""
    if not isinstance(rules, list):
        return ["rules must be a list"]
    errs, seen = [], set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errs.append(f"rules[{i}]: must be an object")
            continue
        rid = rule.get("id")
        if not isinstance(rid, str) or not rid.strip():
            errs.append(f"rules[{i}]: 'id' must be a non-empty string")
            rid = f"#{i}"
        elif rid in seen:
            errs.append(f"rule '{rid}': duplicate id")
        else:
            seen.add(rid)
        scope = rule.get("scope")
        if not isinstance(scope, str) or scope not in _SCOPES:
            errs.append(f"rule '{rid}': scope must be one of {list(_SCOPES)}")
        if "when" not in rule:
            errs.append(f"rule '{rid}': missing 'when'")
        else:
            _check_cond(rule["when"], 1, rid, errs)
        if "then" not in rule:
            errs.append(f"rule '{rid}': missing 'then'")
        else:
            _check_action(rule["then"], scope, rid, errs)
        if "cooldown_hours" in rule:
            cd = _num(rule["cooldown_hours"])
            if cd is None or cd < 0:
                errs.append(f"rule '{rid}': cooldown_hours must be a number >= 0")
        if "enabled" in rule and not isinstance(rule["enabled"], bool):
            errs.append(f"rule '{rid}': enabled must be a boolean")
    return errs


# --------------------------------------------------------------------------- #
# condition evaluation
# --------------------------------------------------------------------------- #
def _window_metrics(entity, days):
    """Fetch the metric dict for a window; str and int keys both accepted."""
    metrics = _g(entity, "metrics", default=None)
    if not isinstance(metrics, dict):
        return {}
    for key in (days, str(days)):
        val = metrics.get(key)
        if isinstance(val, dict):
            return val
    return {}  # no data for that window -> all zeros (safe: never over-fires)


def _metric_value(entity, metric, days):
    window = _window_metrics(entity, days)

    def base(name):
        val = _num(window.get(name))
        return 0.0 if val is None else val

    if metric in _BASE_METRICS:
        return base(metric)
    if metric == "ctr":
        imp = base("impressions")
        return base("clicks") / imp if imp else None
    if metric == "cpc":
        clicks = base("clicks")
        return base("cost") / clicks if clicks else None
    if metric == "cpa":
        conv = base("conversions")
        return base("cost") / conv if conv else None
    if metric == "conv_rate":
        clicks = base("clicks")
        return base("conversions") / clicks if clicks else None
    return None


def _compare(value, op, target):
    """None (undefined metric) never satisfies a comparison."""
    if value is None or target is None:
        return False
    if op == ">":
        return value > target
    if op == ">=":
        return value >= target
    if op == "<":
        return value < target
    if op == "<=":
        return value <= target
    if op == "==":
        return value == target
    if op == "!=":
        return value != target
    return False


def _eval_cond(cond, entity, depth=1):
    """Return (matched, human_text, {windows_used})."""
    if depth > _MAX_DEPTH + 3 or not isinstance(cond, dict):
        return False, "invalid condition", set()
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        kids = cond.get(key)
        if not isinstance(kids, list) or not kids:
            return False, f"{key}[]", set()
        results = [_eval_cond(kid, entity, depth + 1) for kid in kids]
        if key == "all":
            matched = all(r[0] for r in results)
        else:
            matched = any(r[0] for r in results)
        joiner = " and " if key == "all" else " or "
        windows = set()
        for res in results:
            windows |= res[2]
        return matched, joiner.join(r[1] for r in results), windows
    if "not" in cond:
        inner = cond.get("not")
        if not isinstance(inner, dict):
            return False, "invalid condition", set()
        ok, text, windows = _eval_cond(inner, entity, depth + 1)
        return (not ok), f"not ({text})", windows
    metric = cond.get("metric")
    op = cond.get("op")
    target = _num(cond.get("value"))
    days = cond.get("window_days", 30)
    if isinstance(days, bool) or not isinstance(days, int) or not (1 <= days <= 90):
        days = 30
    value = _metric_value(entity, metric, days)
    text = f"{metric} {_fmt(value, metric)} {op} {_fmt(target)}"
    return _compare(value, op, target), text, {days}


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def _current_budget(entity):
    direct = _num(entity.get("daily_budget"))
    if direct is not None:
        return direct
    return _num(_g(entity, "metrics", "daily_budget", default=None))


def evaluate(rules, entities, policy, last_fired=None, now=None):
    """Evaluate rules against fetched entities; returns proposals/alerts/skips."""
    if not isinstance(rules, list):
        raise ToolError("rules must be a list")
    errs = validate_rules(rules)
    if errs:
        msg = "invalid rules: " + "; ".join(errs[:5])
        if any(("enable" in e.lower()) or ("resume" in e.lower()) for e in errs):
            raise RailViolation(scrub(msg))
        raise ToolError(scrub(msg))
    if not isinstance(entities, list):
        raise ToolError("entities must be a list")

    pol = policy if isinstance(policy, dict) else {}
    min_budget = _num(pol.get("min_daily_budget"))
    max_budget = _num(pol.get("max_daily_budget"))
    if min_budget is not None and max_budget is not None and min_budget > max_budget:
        raise RailViolation("policy caps are inverted (min_daily_budget > max_daily_budget)")
    step_pct = _num(pol.get("max_budget_step_pct"))
    step_pct = 20.0 if step_pct is None else abs(step_pct)

    now_dt = _as_dt(now) or _utcnow()
    fired = last_fired if isinstance(last_fired, dict) else {}
    proposals, alerts, skipped = [], [], []

    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled", True) is False:
            continue
        rid = str(rule.get("id"))
        scope = rule.get("scope")
        cond = rule.get("when")
        action = rule.get("then")
        cooldown = _num(rule.get("cooldown_hours")) or 0.0
        act_name = next(iter(action)) if isinstance(action, dict) and action else ""
        act_val = action.get(act_name) if act_name else None

        for entity in entities:
            if not isinstance(entity, dict) or entity.get("scope") != scope:
                continue
            eid = str(entity.get("id", ""))
            key = f"{rid}|{eid}"

            if cooldown > 0:
                prev = _as_dt(fired.get(key))
                if prev is not None:
                    elapsed = (now_dt - prev).total_seconds() / 3600.0
                    if elapsed < cooldown:
                        skipped.append({
                            "rule": rid, "entity": eid, "scope": scope,
                            "last_fired": fired.get(key), "cooldown_hours": cooldown,
                            "hours_remaining": round(cooldown - elapsed, 2),
                        })
                        continue

            matched, text, windows = _eval_cond(cond, entity)
            if not matched:
                continue
            if len(windows) == 1:
                reason = f"{text} over {next(iter(windows))}d"
            elif len(windows) > 1:
                reason = f"{text} over " + ",".join(f"{w}d" for w in sorted(windows))
            else:
                reason = text

            if act_name == "alert":
                alerts.append({"rule": rid, "entity": eid, "scope": scope,
                               "message": str(act_val), "reason": reason})
                continue

            if act_name == "pause":
                patch = {"pause": True}
            elif act_name == "add_negative":
                term = entity.get("name") or entity.get("id") or ""
                if not str(term).strip():
                    continue
                patch = {"add_negative": {"text": str(term),
                                          "match_type": str(_g(act_val, "match_type",
                                                               default="PHRASE")).upper()}}
            elif act_name == "budget_pct":
                current = _current_budget(entity)
                pct = _num(act_val)
                if current is None or current <= 0 or pct is None:
                    continue  # never guess a budget: absent data -> no proposal
                new_budget = current * (1.0 + pct / 100.0)
                band = current * step_pct / 100.0
                new_budget = min(max(new_budget, current - band), current + band)
                if min_budget is not None:
                    new_budget = max(new_budget, min_budget)
                if max_budget is not None:
                    new_budget = min(new_budget, max_budget)
                new_budget = round(new_budget, 2)
                effective_pct = round((new_budget / current - 1.0) * 100.0, 6)
                patch = {"daily_budget": new_budget, "budget_pct": effective_pct,
                         "current_daily_budget": round(current, 2)}
            else:
                continue

            proposals.append({"rule": rid, "entity": eid, "scope": scope,
                              "action": act_name, "reason": reason, "patch": patch})

    return {"proposals": proposals, "alerts": alerts, "skipped_cooldown": skipped}


# --------------------------------------------------------------------------- #
# patch merging
# --------------------------------------------------------------------------- #
def to_state_patch(proposals):
    """Merge proposals into a desired-state fragment (never enables anything)."""
    if not isinstance(proposals, list):
        raise ToolError("proposals must be a list")
    negatives, keyword_pauses = [], []
    seen_neg, seen_kw = set(), set()
    budget, paused_campaign = None, False

    for prop in proposals:
        if not isinstance(prop, dict):
            continue
        patch = prop.get("patch")
        if not isinstance(patch, dict):
            continue
        if _is_enabling(patch):
            raise RailViolation("refusing to build a patch that would enable exposure")
        scope = prop.get("scope")

        if patch.get("pause") is True:
            if scope == "campaign":
                paused_campaign = True
            elif scope == "keyword":
                kid = str(prop.get("entity", ""))
                if kid and kid not in seen_kw:
                    seen_kw.add(kid)
                    keyword_pauses.append(kid)

        neg = patch.get("add_negative")
        if isinstance(neg, dict):
            text = str(neg.get("text") or "").strip()
            match_type = str(neg.get("match_type") or "PHRASE").upper()
            if text and (text, match_type) not in seen_neg:
                seen_neg.add((text, match_type))
                negatives.append({"text": text, "match_type": match_type})

        value = _num(patch.get("daily_budget"))
        if value is not None:
            budget = round(value, 2)  # last campaign budget proposal wins

    out = {"negatives_add": negatives, "keyword_pauses": keyword_pauses}
    if budget is not None:
        out["daily_budget_usd"] = budget
    if paused_campaign:
        out["status"] = "PAUSED"
    return out


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def selftest() -> int:
    n = 0

    def chk(cond, msg="check failed"):
        nonlocal n
        assert cond, msg
        n += 1

    def deep(k):
        node = {"metric": "clicks", "op": ">", "value": 0}
        for _ in range(k):
            node = {"not": node}
        return node

    good = [{
        "id": "pause-wasters", "scope": "keyword",
        "when": {"all": [{"metric": "cost", "op": ">=", "value": 40, "window_days": 30},
                         {"metric": "conversions", "op": "==", "value": 0, "window_days": 30}]},
        "then": {"pause": True}, "cooldown_hours": 24, "enabled": True,
    }]
    chk(validate_rules(good) == [], validate_rules(good))
    chk(validate_rules("nope") == ["rules must be a list"])
    chk(any("unknown metric" in e for e in validate_rules(
        [{"id": "a", "scope": "keyword", "when": {"metric": "bogus", "op": ">", "value": 1},
          "then": {"pause": True}}])))
    chk(any("unknown op" in e for e in validate_rules(
        [{"id": "a", "scope": "keyword", "when": {"metric": "cost", "op": "~", "value": 1},
          "then": {"pause": True}}])))
    chk(any("window_days" in e for e in validate_rules(
        [{"id": "a", "scope": "keyword",
          "when": {"metric": "cost", "op": ">", "value": 1, "window_days": 0},
          "then": {"pause": True}}])))
    chk(any("window_days" in e for e in validate_rules(
        [{"id": "a", "scope": "keyword",
          "when": {"metric": "cost", "op": ">", "value": 1, "window_days": 91},
          "then": {"pause": True}}])))
    chk(any("duplicate" in e for e in validate_rules(
        [dict(good[0]), dict(good[0])])))
    chk(any("nested deeper" in e for e in validate_rules(
        [{"id": "d", "scope": "keyword", "when": deep(5), "then": {"pause": True}}])))
    chk(validate_rules([{"id": "d", "scope": "keyword", "when": deep(4),
                         "then": {"pause": True}}]) == [])
    chk(any("search_term" in e for e in validate_rules(
        [{"id": "n", "scope": "keyword", "when": {"metric": "clicks", "op": ">", "value": 1},
          "then": {"add_negative": {"match_type": "EXACT"}}}])))
    chk(any("campaign" in e for e in validate_rules(
        [{"id": "b", "scope": "keyword", "when": {"metric": "cost", "op": ">", "value": 1},
          "then": {"budget_pct": -10}}])))
    chk(any("enable" in e for e in validate_rules(
        [{"id": "e", "scope": "keyword", "when": {"metric": "cost", "op": ">", "value": 1},
          "then": {"pause": False}}])))
    chk(any("enable" in e for e in validate_rules(
        [{"id": "e", "scope": "keyword", "when": {"metric": "cost", "op": ">", "value": 1},
          "then": {"enable": True}}])))
    chk(any("unknown action" in e for e in validate_rules(
        [{"id": "e", "scope": "keyword", "when": {"metric": "cost", "op": ">", "value": 1},
          "then": {"nuke": 1}}])))
    chk(any("non-empty list" in e for e in validate_rules(
        [{"id": "e", "scope": "keyword", "when": {"all": []}, "then": {"pause": True}}])))

    kw = {"scope": "keyword", "id": "k1", "name": "wasters",
          "metrics": {"30": {"cost": "42.1", "clicks": "10", "impressions": "500",
                             "conversions": 0}}}
    res = evaluate(good, [kw], {}, now="2024-01-01T00:00:00Z")
    chk(len(res["proposals"]) == 1 and res["proposals"][0]["patch"] == {"pause": True})
    chk(res["proposals"][0]["reason"] == "cost 42.10 >= 40 and conversions 0 == 0 over 30d",
        res["proposals"][0]["reason"])
    chk(evaluate(good, [], {}, now="2024-01-01T00:00:00Z")["proposals"] == [])
    chk(evaluate(good, [None, "x", {"scope": "keyword"}], {},
                 now="2024-01-01T00:00:00Z")["proposals"] == [])

    cool = evaluate(good, [kw], {}, last_fired={"pause-wasters|k1": "2023-12-31T12:00:00Z"},
                    now="2024-01-01T00:00:00Z")
    chk(len(cool["proposals"]) == 0 and len(cool["skipped_cooldown"]) == 1)
    chk(evaluate(good, [kw], {}, last_fired={"pause-wasters|k1": "2023-12-30T00:00:00Z"},
                 now="2024-01-01T00:00:00Z")["proposals"][0]["rule"] == "pause-wasters")

    cpa_rule = [{"id": "cpa0", "scope": "keyword",
                 "when": {"metric": "cpa", "op": ">", "value": 0}, "then": {"pause": True}}]
    chk(evaluate(cpa_rule, [kw], {}, now="2024-01-01T00:00:00Z")["proposals"] == [])

    not_rule = [{"id": "not-big", "scope": "keyword",
                 "when": {"not": {"metric": "impressions", "op": ">", "value": 100}},
                 "then": {"pause": True}}]
    small = {"scope": "keyword", "id": "k9", "metrics": {"30": {"impressions": 50}}}
    chk(len(evaluate(not_rule, [small], {}, now="2024-01-01T00:00:00Z")["proposals"]) == 1)

    alert_rule = [{"id": "cpa-high", "scope": "keyword",
                   "when": {"any": [{"metric": "cpa", "op": ">", "value": 5},
                                    {"metric": "clicks", "op": ">", "value": 999}]},
                   "then": {"alert": "cpa too high"}}]
    k2 = {"scope": "keyword", "id": "k2",
          "metrics": {30: {"cost": 100, "clicks": 20, "conversions": 2, "impressions": 1000}}}
    ra = evaluate(alert_rule, [k2], {}, now="2024-01-01T00:00:00Z")
    chk(len(ra["alerts"]) == 1 and ra["alerts"][0]["message"] == "cpa too high"
        and ra["proposals"] == [])

    camp = {"scope": "campaign", "id": "c1", "name": "Brand", "daily_budget": 100.0,
            "metrics": {"30": {"cost": 50, "conversions": 10, "clicks": 100}}}
    budget_rule = [{"id": "raise", "scope": "campaign",
                    "when": {"metric": "cpa", "op": "<", "value": 10},
                    "then": {"budget_pct": 50}}]
    rb = evaluate(budget_rule, [camp], {"min_daily_budget": 10.0, "max_daily_budget": 110.0},
                  now="2024-01-01T00:00:00Z")
    chk(len(rb["proposals"]) == 1 and rb["proposals"][0]["patch"]["daily_budget"] == 110.0,
        rb["proposals"])
    chk(evaluate(budget_rule, [dict(camp, daily_budget=None)], {},
                 now="2024-01-01T00:00:00Z")["proposals"] == [])

    term = {"scope": "search_term", "id": "cheap shoes",
            "metrics": {"30": {"clicks": 5, "cost": 3}}}
    neg_rule = [{"id": "neg", "scope": "search_term",
                 "when": {"metric": "clicks", "op": ">=", "value": 3},
                 "then": {"add_negative": {"match_type": "exact"}}}]
    rn = evaluate(neg_rule, [term], {}, now="2024-01-01T00:00:00Z")
    chk(len(rn["proposals"]) == 1)
    patch = to_state_patch(rn["proposals"])
    chk(patch["negatives_add"] == [{"text": "cheap shoes", "match_type": "EXACT"}], patch)
    chk(patch.get("status") is None and "daily_budget_usd" not in patch)

    merged = to_state_patch(
        evaluate(good, [kw], {}, now="2024-01-01T00:00:00Z")["proposals"]
        + rb["proposals"]
        + [{"rule": "x", "entity": "n1", "scope": "search_term", "action": "add_negative",
            "reason": "r", "patch": {"add_negative": {"text": "cheap shoes",
                                                      "match_type": "PHRASE"}}},
           {"rule": "x", "entity": "n2", "scope": "search_term", "action": "add_negative",
            "reason": "r", "patch": {"add_negative": {"text": "cheap shoes",
                                                      "match_type": "PHRASE"}}}])
    chk(merged["daily_budget_usd"] == 110.0)
    chk(merged["keyword_pauses"] == ["k1"])
    chk(len(merged["negatives_add"]) == 1)
    chk(to_state_patch([]) == {"negatives_add": [], "keyword_pauses": []})

    try:
        to_state_patch([{"rule": "x", "entity": "e", "scope": "campaign",
                         "action": "pause", "reason": "r", "patch": {"pause": False}}])
        chk(False, "expected RailViolation")
    except RailViolation:
        n += 1

    try:
        evaluate([{"id": "e", "scope": "keyword",
                   "when": {"metric": "cost", "op": ">", "value": 1},
                   "then": {"pause": False}}], [], {}, now="2024-01-01T00:00:00Z")
        chk(False, "expected RailViolation")
    except RailViolation:
        n += 1

    try:
        evaluate("bad", [], {})
        chk(False, "expected ToolError")
    except ToolError:
        n += 1

    chk(_g({"a": {"b": {"c": "7"}}}, "a", "b", "c") == "7")
    chk(_g({"a": 1}, "a", "b", default="d") == "d")
    chk(_num("2500000") == 2500000.0 and _num(None) is None and _num(True) is None)

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)