"""Tenant config for AdRails Intel: load, default, validate. One JSON file per tenant.

Relative paths in the file resolve against the config file's own directory, so a tenant
config can be reviewed and moved as a unit. ``~`` is expanded. Validation errors raise
``ToolError`` naming the key.
"""
import json
import os
import sys

from ..errors import ToolError

DEVICES = {"mobile", "desktop"}
MODES = {"standard", "priority", "live"}
CADENCES = {"daily", "weekly"}

# USD per SERP page (<=10 organic results). Source: https://dataforseo.com/pricing/serp/google-organic-serp-api
# (fetched 2026-11-01: "$0.0006 in the Standard queue, $0.0012 in the Priority queue, and $0.002 in Live mode";
# "The location, language, and device you request do not affect the price.")
PRICE_PER_SERP_USD = {"standard": 0.0006, "priority": 0.0012, "live": 0.002}

DEFAULTS = {
    "language_code": "en",
    "devices": ["mobile", "desktop"],
    "depth": 10,
    "mode": "standard",
    "cadence": "daily",
    "monthly_cap_usd": 10.0,
    "collector_enabled": False,
    "price_per_serp_usd": PRICE_PER_SERP_USD,
    "db_path": "~/.adsops/intel/{tenant}.sqlite",
    "slack": {"enabled": False},
    "competitors": [],
    "auction_insights": {"campaign": None, "level": "ad_group"},
    "transparency": {"domains": None, "region_code": 2840, "max_requests": 40, "jitter_s": [3, 8],
                     "cache_days": 7, "cache_dir": "~/.adsops/intel/{tenant}-transparency-cache",
                     "page_size": 40, "cooldown_hours": 6},
    "ownstats": {"adsops_config": None, "account": None, "campaigns": [], "lookback_days": 7},
    "credentials": {
        "op_sa_path": "~/.openclaw/workspace/bin/op-sa",
        "op_item_title": "DataForSEO API",
        "op_item_id": None,
        "timeout_s": 30,
    },
}


def _need(cond, msg):
    if not cond:
        raise ToolError("intel config: " + msg)


def _resolve(base, p):
    if not isinstance(p, str) or not p:
        return p
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def validate(cfg, base="."):
    """Apply defaults and validate in place; returns cfg."""
    for k, v in DEFAULTS.items():
        if k not in cfg:
            cfg[k] = json.loads(json.dumps(v))
    cfg["credentials"] = {**DEFAULTS["credentials"], **(cfg.get("credentials") or {})}
    cfg["price_per_serp_usd"] = {**PRICE_PER_SERP_USD, **(cfg.get("price_per_serp_usd") or {})}
    for sec in ("auction_insights", "transparency", "ownstats"):
        _need(isinstance(cfg.get(sec), dict), f"'{sec}' must be an object")
        cfg[sec] = {**DEFAULTS[sec], **cfg[sec]}
    _need(isinstance(cfg.get("tenant"), str) and cfg["tenant"].strip(), "'tenant' is required")
    _need(cfg["tenant"].replace("-", "").replace("_", "").isalnum(), "'tenant' must be [A-Za-z0-9_-]")
    doms = cfg.get("our_domains")
    _need(isinstance(doms, list) and doms and all(isinstance(d, str) and "." in d for d in doms),
          "'our_domains' must be a non-empty list of domains")
    cfg["our_domains"] = [d.lower().removeprefix("www.") for d in doms]
    kws = cfg.get("keywords")
    _need(isinstance(kws, list) and kws and all(isinstance(k, str) and k.strip() for k in kws),
          "'keywords' must be a non-empty list of strings")
    _need(len(set(kws)) == len(kws), "'keywords' has duplicates")
    locs = cfg.get("locations")
    _need(isinstance(locs, list) and locs, "'locations' must be a non-empty list")
    keys = set()
    for i, loc in enumerate(locs):
        _need(isinstance(loc, dict), f"locations[{i}] must be an object")
        _need(isinstance(loc.get("key"), str) and loc["key"], f"locations[{i}].key is required")
        _need(loc["key"] not in keys, f"locations[{i}].key duplicates '{loc['key']}'")
        keys.add(loc["key"])
        code = loc.get("location_code")
        _need(code is None or (isinstance(code, int) and not isinstance(code, bool) and code > 0),
              f"locations[{i}].location_code must be a positive integer or null")
        _need(code is not None or (isinstance(loc.get("location_name"), str) and loc["location_name"]),
              f"locations[{i}] needs location_code or location_name")
    _need(isinstance(cfg["devices"], list) and cfg["devices"] and set(cfg["devices"]) <= DEVICES,
          f"'devices' must be a non-empty subset of {sorted(DEVICES)}")
    _need(cfg["mode"] in MODES, f"'mode' must be one of {sorted(MODES)}")
    _need(cfg["cadence"] in CADENCES, f"'cadence' must be one of {sorted(CADENCES)}")
    _need(isinstance(cfg["depth"], int) and 1 <= cfg["depth"] <= 700, "'depth' must be 1..700")
    cap = cfg["monthly_cap_usd"]
    _need(isinstance(cap, (int, float)) and not isinstance(cap, bool) and 0 < cap <= 500,
          "'monthly_cap_usd' must be a number in (0, 500]")
    _need(isinstance(cfg["collector_enabled"], bool), "'collector_enabled' must be a boolean")
    for m, p in cfg["price_per_serp_usd"].items():
        _need(isinstance(p, (int, float)) and p > 0, f"price_per_serp_usd['{m}'] must be > 0")
    _need(not cfg["slack"].get("enabled"), "slack posting is disabled in v0")
    cred = cfg["credentials"]
    _need(cred["op_item_id"] is None or (isinstance(cred["op_item_id"], str) and cred["op_item_id"].isalnum()),
          "credentials.op_item_id must be null or an alphanumeric 1Password item id")
    comp = cfg["competitors"]
    _need(isinstance(comp, list) and all(isinstance(d, str) and "." in d for d in comp),
          "'competitors' must be a list of domains")
    cfg["competitors"] = [d.lower().removeprefix("www.") for d in comp]
    _need(len(set(cfg["competitors"])) == len(cfg["competitors"]), "'competitors' has duplicates")
    _need(cfg["auction_insights"]["level"] in ("campaign", "ad_group", "keyword"),
          "auction_insights.level must be campaign|ad_group|keyword")
    t = cfg["transparency"]
    _need(isinstance(t["max_requests"], int) and not isinstance(t["max_requests"], bool) and 1 <= t["max_requests"] <= 40,
          "transparency.max_requests must be 1..40 (hard cap)")
    j = t["jitter_s"]
    _need(isinstance(j, list) and len(j) == 2 and all(isinstance(x, (int, float)) for x in j) and 3 <= j[0] <= j[1],
          "transparency.jitter_s must be [min, max] with min >= 3 seconds")
    _need(isinstance(t["cache_days"], (int, float)) and t["cache_days"] >= 1, "transparency.cache_days must be >= 1")
    _need(isinstance(t["page_size"], int) and 1 <= t["page_size"] <= 40, "transparency.page_size must be 1..40")
    _need(t["domains"] is None or (isinstance(t["domains"], list) and all(isinstance(d, str) and d for d in t["domains"])),
          "transparency.domains must be null (use competitors) or a list of strings")
    t["cache_dir"] = _resolve(base, t["cache_dir"].replace("{tenant}", cfg["tenant"]))
    o = cfg["ownstats"]
    _need(isinstance(o["lookback_days"], int) and 1 <= o["lookback_days"] <= 90, "ownstats.lookback_days must be 1..90")
    _need(isinstance(o["campaigns"], list), "ownstats.campaigns must be a list of campaign names")
    if o.get("adsops_config"):
        o["adsops_config"] = _resolve(base, o["adsops_config"])
    cfg["db_path"] = _resolve(base, cfg["db_path"].replace("{tenant}", cfg["tenant"]))
    for k in ("report_dir", "volume_csv", "spyfu_csv", "ctr_table"):
        if cfg.get(k):
            cfg[k] = _resolve(base, cfg[k])
    cred["op_sa_path"] = _resolve(base, cred["op_sa_path"])
    kb = cfg.get("known_budget_usd_month")
    _need(kb is None or (isinstance(kb, (int, float)) and kb > 0), "'known_budget_usd_month' must be > 0 or null")
    return cfg


def load(path):
    path = os.path.expanduser(path)
    with open(path, "r", encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as e:
            raise ToolError(f"intel config: {path} is not valid JSON ({e.msg} line {e.lineno})")
    cfg["_path"] = os.path.abspath(path)
    return validate(cfg, os.path.dirname(os.path.abspath(path)))


def cache_item_id(path, item_id):
    """Write a discovered 1Password item ID (not a secret) back into the tenant config."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    raw.setdefault("credentials", {})["op_item_id"] = item_id
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def price(cfg):
    return float(cfg["price_per_serp_usd"][cfg["mode"]]) * max(1, -(-cfg["depth"] // 10))


def sample(**over):
    cfg = {"tenant": "t1", "our_domains": ["www.Example.com"], "keywords": ["a", "b"],
           "locations": [{"key": "tampa", "location_name": "Tampa,Florida,United States"}]}
    cfg.update(over)
    return cfg


def selftest():
    import tempfile
    n = 0
    c = validate(sample(), "/base")
    assert c["our_domains"] == ["example.com"] and c["monthly_cap_usd"] == 10.0; n += 1
    assert c["db_path"].endswith("/.adsops/intel/t1.sqlite") and c["collector_enabled"] is False; n += 1
    assert abs(price(c) - 0.0006) < 1e-12; n += 1
    assert abs(price(validate(sample(depth=20, mode="live"))) - 0.004) < 1e-12; n += 1
    for bad in (dict(tenant=""), dict(keywords=[]), dict(keywords=["a", "a"]), dict(devices=["tablet"]),
                dict(monthly_cap_usd=0), dict(monthly_cap_usd=True), dict(mode="x"),
                dict(locations=[{"key": "x"}]), dict(slack={"enabled": True}),
                dict(credentials={"op_item_id": "a;rm"}), dict(our_domains=["nodot"])):
        try:
            validate(sample(**bad))
        except ToolError:
            n += 1
        else:
            raise AssertionError(f"accepted bad config {bad}")
    for bad in (dict(transparency={"max_requests": 41}), dict(transparency={"jitter_s": [1, 2]}),
                dict(transparency={"cache_days": 0}), dict(competitors=["nodot"]),
                dict(competitors=["a.com", "www.a.com"]), dict(auction_insights={"level": "x"}),
                dict(ownstats={"lookback_days": 0})):
        try:
            validate(sample(**bad))
        except ToolError:
            n += 1
        else:
            raise AssertionError(f"accepted bad config {bad}")
    c = validate(sample(competitors=["WWW.A.com"]), "/b")
    assert c["competitors"] == ["a.com"] and c["transparency"]["max_requests"] == 40; n += 1
    assert c["transparency"]["cache_dir"].endswith("t1-transparency-cache"); n += 1
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.json")
    with open(p, "w") as fh:
        json.dump(sample(report_dir="out"), fh)
    c = load(p)
    assert c["report_dir"] == os.path.join(d, "out"); n += 1
    cache_item_id(p, "abc123XYZ")
    assert load(p)["credentials"]["op_item_id"] == "abc123XYZ"; n += 1
    return n


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(f"intel.config: {selftest()} checks")
        print("ALL PASS")
