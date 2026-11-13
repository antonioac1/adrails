"""python3 -m adsops [--config PATH] [--selftest] [meter-summary [--since ISO]] | site-audit --url URL [...] | seo-run --profile P [...]"""
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
                 "mail.codes", "mail.qr", "mail.hygiene", "mail.labels", "mail.scans", "mail.response", "mail.cac", "platforms.openai_ads", "oai_policy",
                 "oai_creative", "oai_conversions", "oai_audiences", "oai_experiments", "oai_builder",
                 "local.leadbook", "local.presence", "local.reviews", "local.oauth_connect", "local.plan", "local.lsa", "local.setup_cli", "local.results", "local.gbp", "updates", "guides", "paid_tools", "metrics.store", "metrics.gsc", "metrics.impact", "metrics_tools", "devices",
                 "seo.fetcher", "seo.extract", "seo.crawl", "seo.checks_site", "seo.checks_content", "seo.checks_links",
                 "seo.adguard", "seo.issues", "seo.runs", "seo.perf", "seo.render", "seo.gsc_join", "seo_tools",
                 "content.numbers", "content.checks", "content.sources", "content.facts", "content.topics", "content.bundle",
                 "content.learn", "content.extend", "content.writers", "content.review", "content.publish", "content.pipeline", "content_tools", "coach_tools",
                 "mail_tools", "tools"):
        mod = importlib.import_module("adsops." + name)
        n = mod.selftest()
        print(f"  {name:22s} {n} checks", file=sys.stderr)
        total += n
    print(f"ALL PASS ({total} checks)")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "seo-run" in argv[:3]:                     # guarded content pipeline, one shot (cron/systemd): seo-run --help
        from adsops import content_tools
        rest = [x for x in argv if x != "seo-run"]
        if "--config" not in rest and os.environ.get("ADSOPS_CONFIG"):
            rest += ["--config", os.environ["ADSOPS_CONFIG"]]
        return content_tools.main(rest)
    if "site-audit" in argv[:3]:                  # technical SEO site audit: its own flags (python3 -m adsops site-audit --help)
        from adsops import seo_tools
        rest = [x for x in argv if x != "site-audit"]
        cfg = None
        if "--config" not in rest and os.environ.get("ADSOPS_CONFIG"):
            rest += ["--config", os.environ["ADSOPS_CONFIG"]]
        return seo_tools.main(rest, cfg)
    ap = argparse.ArgumentParser(prog="adsops")
    ap.add_argument("--config", default=os.environ.get("ADSOPS_CONFIG"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("cmd", nargs="?", choices=["serve", "meter-summary", "guard-all", "init", "doctor", "setup", "connect", "upgrade"], default="serve")
    ap.add_argument("--since")
    # init: scaffold one campaign (config, desired state, SAFE policy: caps == today's budget, commits off) + first git commit
    ap.add_argument("--dir"); ap.add_argument("--customer-id"); ap.add_argument("--campaign-id"); ap.add_argument("--sa")
    ap.add_argument("--login-customer-id"); ap.add_argument("--tenant", default="default")
    ap.add_argument("--offline", action="store_true", help="init without reading the live campaign (placeholder values)")
    ap.add_argument("--only-platform", choices=["google_ads", "openai_ads"],
                    help="guard-all: check only this platform's campaigns")
    # setup / connect: agent-driven small-business onboarding (adsops/local/setup_cli.py); both print one JSON object
    ap.add_argument("--business"); ap.add_argument("--install-desktop", action="store_true")
    ap.add_argument("--client-file"); ap.add_argument("--developer-token-file")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.cmd == "init":
        return _init(a, ap)
    if a.cmd in ("setup", "connect"):
        from adsops.local import setup_cli
        return setup_cli.main(a.cmd, a)
    if a.cmd == "upgrade":       # agent-run, owner-approved: fetch the latest release and raise Claude Desktop's version floor
        from adsops import updates, config as _c
        cfg = _c.load_config(a.config) if a.config else None
        r = updates.main_upgrade(cfg)
        print(json.dumps(r, indent=1))
        return 0 if r.get("ok") else 1
    if not a.config:
        ap.error("--config (or ADSOPS_CONFIG) is required")
    from adsops import config, meter, tools
    cfg = config.load_config(a.config)
    if a.cmd == "meter-summary":
        m = meter.Meter(os.path.expanduser(cfg.get("meter_log", "~/.adsops/meter.jsonl")), cfg["tenant"])
        print(json.dumps(m.summary(a.since), indent=1))
        return 0
    if a.cmd == "guard-all":
        return guard_all(cfg, only_platform=a.only_platform)
    if a.cmd == "doctor":
        from adsops import onboard
        rows = onboard.doctor(a.config)
        print(onboard.format_report(rows))
        return 0 if all(r["ok"] for r in rows) else 1
    from adsops import updates
    updates.refresh_in_background(cfg)   # at most one PyPI request a day, cached; off with ADRAILS_NO_UPDATE_CHECK=1
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


def guard_all(cfg, server=None, write_heartbeat=None, only_platform=None):
    """Unattended spend guard: PAUSE-only pass over every campaign. A heartbeat is written ONLY when every
    campaign it covers was checked cleanly, so a failing guard goes stale and blocks exposure increases
    (fail closed). Google campaigns share cfg.guard_heartbeat; each openai_ads account has its own, so a
    missing OpenAI key can never stale the Google heartbeat (and vice versa)."""
    import datetime as dt
    from adsops import config, safety, tools
    srv = server or tools.build_server(cfg)
    accounts = {a["key"]: a for a in cfg.get("accounts", [])}
    ok_by = {}
    for c in config.list_campaigns(cfg):
        if only_platform and c["platform"] != only_platform:
            continue
        acct = accounts.get(c["account"], {})
        is_oai = c["platform"] == "openai_ads"
        hb_key = ("openai", c["account"]) if is_oai else ("google", None)
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "adsops_openai_guard" if is_oai else "adsops_guard",
                                   "arguments": {"campaign": c["key"], "commit": True}}})
        res = r.get("result", {})
        line = res.get("structuredContent") or {"error": "".join(x.get("text", "") for x in res.get("content", []))[:300]}
        good = not res.get("isError") and "error" not in r
        if is_oai and acct.get("mode", "disabled") == "disabled":
            hb_key = None            # a disabled account proves nothing: never write its heartbeat
        if hb_key is not None:
            ok_by[hb_key] = ok_by.get(hb_key, True) and good
        print(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                          "campaign": c["key"], **line}))
    wh = write_heartbeat or safety.write_heartbeat
    for (plat, akey), good in ok_by.items():
        if not good:
            continue
        if plat == "google":
            wh(os.path.expanduser(cfg.get("guard_heartbeat", "~/.adsops/guard.heartbeat")))
        else:
            a = accounts[akey]
            wh(os.path.expanduser(a.get("guard_heartbeat") or f"~/.adsops/openai-{akey}.heartbeat"))
    return 0 if all(ok_by.values()) else 1

if __name__ == "__main__":
    sys.exit(main())
