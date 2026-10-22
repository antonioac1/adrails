"""python3 -m adsops [--config PATH] [--selftest] [meter-summary [--since ISO]]"""
import argparse
import json
import os
import sys


def _selftest():
    import importlib
    total = 0
    for name in ("errors", "protocol", "meter", "auth", "platforms.google_ads", "engine", "config", "statestore", "policy", "safety", "strict", "tools"):
        mod = importlib.import_module("adsops." + name)
        n = mod.selftest()
        print(f"  {name:22s} {n} checks", file=sys.stderr)
        total += n
    print(f"ALL PASS ({total} checks)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adsops")
    ap.add_argument("--config", default=os.environ.get("ADSOPS_CONFIG"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("cmd", nargs="?", choices=["serve", "meter-summary", "guard-all"], default="serve")
    ap.add_argument("--since")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not a.config:
        ap.error("--config (or ADSOPS_CONFIG) is required")
    from adsops import config, meter, tools
    cfg = config.load_config(a.config)
    if a.cmd == "meter-summary":
        m = meter.Meter(os.path.expanduser(cfg.get("meter_log", "~/.adsops/meter.jsonl")), cfg["tenant"])
        print(json.dumps(m.summary(a.since), indent=1))
        return 0
    if a.cmd == "guard-all":
        return guard_all(cfg)
    tools.build_server(cfg).serve()
    return 0


def guard_all(cfg, server=None, write_heartbeat=None):
    """Unattended spend guard: PAUSE-only pass over every campaign. The heartbeat is written ONLY when every
    campaign was checked cleanly, so a failing guard goes stale and blocks exposure increases (fail closed)."""
    import datetime as dt
    from adsops import config, safety, tools
    srv = server or tools.build_server(cfg)
    ok = True
    for c in config.list_campaigns(cfg):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "adsops_guard", "arguments": {"campaign": c["key"], "commit": True}}})
        res = r.get("result", {})
        line = res.get("structuredContent") or {"error": "".join(x.get("text", "") for x in res.get("content", []))[:300]}
        ok = ok and not res.get("isError")
        print(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                          "campaign": c["key"], **line}))
    if ok:
        (write_heartbeat or safety.write_heartbeat)(os.path.expanduser(cfg.get("guard_heartbeat", "~/.adsops/guard.heartbeat")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
