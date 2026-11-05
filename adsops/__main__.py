"""python3 -m adsops [--config PATH] [--selftest] [meter-summary [--since ISO]]"""
import argparse
import json
import os
import sys


def _selftest():
    import importlib
    total = 0
    for name in ("errors", "protocol", "meter", "auth", "platforms.google_ads", "urls", "engine", "config", "statestore", "policy", "safety", "strict", "negatives", "lpdiag", "playbook_tools",
                 "conv_audit", "diagnostics", "rsa_validator", "rules", "monitors", "approvals", "rollback", "budget_realloc",
                 "fleet", "auditchain", "offline_conv", "kwplan", "change_history", "rbac", "sbom", "enterprise_tools",
                 "aeo.crawl_audit", "aeo.schema_audit", "aeo.page_audit", "aeo.llms_txt", "aeo.visibility", "aeo.indexnow",
                 "aeo.ai_traffic", "aeo.ads_to_aeo", "aeo_tools", "onboard",
                 "mail.codes", "mail.qr", "mail.hygiene", "mail.labels", "mail.scans", "mail.response", "mail.cac", "mail_tools", "tools"):
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
    ap.add_argument("cmd", nargs="?", choices=["serve", "meter-summary", "guard-all", "init", "doctor"], default="serve")
    ap.add_argument("--since")
    # init: scaffold one campaign (config, desired state, SAFE policy: caps == today's budget, commits off) + first git commit
    ap.add_argument("--dir"); ap.add_argument("--customer-id"); ap.add_argument("--campaign-id"); ap.add_argument("--sa")
    ap.add_argument("--login-customer-id"); ap.add_argument("--tenant", default="default")
    ap.add_argument("--offline", action="store_true", help="init without reading the live campaign (placeholder values)")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.cmd == "init":
        return _init(a, ap)
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
    if a.cmd == "doctor":
        from adsops import onboard
        rows = onboard.doctor(a.config)
        print(onboard.format_report(rows))
        return 0 if all(r["ok"] for r in rows) else 1
    tools.build_server(cfg).serve()
    return 0


def _init(a, ap):
    from adsops import auth, onboard
    from adsops.platforms import google_ads
    for need in ("dir", "customer_id", "campaign_id", "sa"):
        if not getattr(a, need):
            ap.error("init needs --dir --customer-id --campaign-id --sa")
    client = None
    if not a.offline:
        tokens = auth.from_config({"type": "service_account", "sources": [{"kind": "file", "path": a.sa}]})
        client = google_ads.GoogleAdsClient(str(a.customer_id).replace("-", ""), tokens, login_customer_id=a.login_customer_id,
                                            developer_token=os.environ.get("ADSOPS_GOOGLE_DEVELOPER_TOKEN"))
    out = onboard.init(a.dir, a.customer_id, a.campaign_id, a.sa, login_customer_id=a.login_customer_id, tenant=a.tenant, client=client)
    print(json.dumps(out, indent=1, default=str))
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
