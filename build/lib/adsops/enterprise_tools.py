"""Enterprise tools for AdRails: diagnostics, conversion audit, rules, monitors, approvals, rollback, fleet, audit chain.

Written by the maintainers 2026-11-05; the logic modules (conv_audit, diagnostics, rules, monitors, approvals, rollback, ...) were built
by DeepSeek from specs in specs/ent/ and reviewed. This file is only GLUE: it turns live reads into the plain rows those
pure modules take, and registers MCP tools.

Safety model (unchanged): nothing here can raise spend. Every tool is a READ, or writes only local files (approval queue,
audit chain, monitor state), or edits a desired-state file in git (rollback). Account changes still go through the one
door: committed state file -> adsops_plan -> adsops_apply.

harden(srv, ctx, cfg) runs LAST in build_server and wraps EVERY registered tool with:
  1. RBAC (cfg["rbac"], off unless enabled): refused calls have zero side effects.
  2. Approval gate (cfg["approvals"]["mode"] = "off" | "exposure" | "all"): a COMMIT through adsops_apply / adsops_set needs
     an approved request for that plan_id. "exposure" gates only adsops_set calls that raise budget, ENABLE or move the end date,
     plus every adsops_apply commit (apply can't tell its direction without a plan; the plan tool shows it).
  3. Tamper-evident audit chain (cfg["audit_chain"], default ~/.adsops/audit-chain.jsonl) for every mutating call,
     ok or refused, with arguments redacted.

Config keys used (all optional): audit_chain, rbac, approvals{mode,path,secret_env,ttl_s}, monitors[], monitor_state,
alerts{webhook_env}, fleet_caps{daily_total,month_total,per_account,overspend_factor}.
"""
import datetime as dt
import json
import os
import subprocess

from adsops import (approvals, auditchain, budget_realloc, change_history, conv_audit, diagnostics, fleet, kwplan,
                    monitors, offline_conv, rbac as rbac_mod, rollback, rsa_validator, rules, sbom)
from adsops.errors import RailViolation, ToolError, scrub
from adsops.platforms import google_ads

# Tools that change something outside this process (an ad account, a git file, the approval queue).
MUTATING = {"adsops_apply", "adsops_set", "adsops_guard", "adsops_rollback", "adsops_approval_decide",
            "adsops_approval_request", "adsops_auction_insights_import", "aeo_indexnow", "mail_labels",
            "adsops_openai_apply", "adsops_openai_guard", "adsops_openai_upload_image", "adsops_openai_brand_update",
            "adsops_openai_conversions_setup", "adsops_openai_send_conversions", "adsops_openai_audience",
            "adsops_local_profile", "adsops_local_plan", "adsops_leads", "adsops_lsa_leads", "adsops_lsa_dispute"}
GATED = {"adsops_apply", "adsops_set", "adsops_openai_apply"}


def _dates(now, days):
    until = dt.date.fromtimestamp(now())
    return (until - dt.timedelta(days=int(days))).isoformat(), until.isoformat()


def _days(args, default=30, hi=90):
    d = args.get("days", default)
    if not 1 <= d <= hi:
        raise ToolError(f"days must be 1..{hi}")
    return d


def state_path(cfg, key, filename):
    """Where a piece of local state lives: cfg[key] if set, else BESIDE the config's own audit_log/meter_log.
    Never a hard-coded home path: that let a temp-dir test config write into the production audit chain
    (qa_protocol caught it on 2026-11-06; the polluted chain was archived and rotated)."""
    if cfg.get(key):
        return os.path.expanduser(cfg[key])
    base = cfg.get("audit_log") or cfg.get("meter_log") or "~/.adsops/audit.jsonl"
    return os.path.join(os.path.dirname(os.path.expanduser(base)), filename)


def _git_runner(args, cwd):
    """rollback.py's runner contract: (args, cwd) -> (rc, stdout, stderr)."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def _camp_keys(cfg):
    return [c["key"] for a in cfg.get("accounts", []) for c in a.get("campaigns", [])]


def snapshot(ctx, keys):
    """Live per-campaign snapshot in the shape monitors/fleet/budget_realloc take. One bad campaign never hides the rest."""
    now = ctx.now()
    today = dt.date.fromtimestamp(now)
    since30, until = _dates(ctx.now, 30)
    since7 = (today - dt.timedelta(days=7)).isoformat()
    out, errors = [], []
    for key in keys:
        try:
            account, camp, raw, eff, pol, client = ctx.open(key)
            live = google_ads.live_state(client, eff["campaign_id"], ctx.end_field())
            cid = int(eff["campaign_id"])

            def agg(since, extra="metrics.conversions"):
                rows = client.search(f"SELECT metrics.cost_micros, metrics.clicks, metrics.impressions, {extra} FROM campaign "
                                     f"WHERE campaign.id = {cid} AND segments.date BETWEEN '{since}' AND '{until}'")
                m = lambda k: sum(float((r.get("metrics") or {}).get(k) or 0) for r in rows)
                return {"cost": m("costMicros") / 1e6, "clicks": m("clicks"), "impr": m("impressions"), "conv": m("conversions")}

            t, w7, w30 = agg(until), agg(since7), agg(since30)
            mtd = agg(today.replace(day=1).isoformat())
            isr = client.search("SELECT metrics.search_budget_lost_impression_share, metrics.search_rank_lost_impression_share "
                                f"FROM campaign WHERE campaign.id = {cid} AND segments.date BETWEEN '{since7}' AND '{until}'")
            lb = diagnostics._frac((isr[0].get("metrics") or {}).get("searchBudgetLostImpressionShare")) if isr else 0.0
            lr = diagnostics._frac((isr[0].get("metrics") or {}).get("searchRankLostImpressionShare")) if isr else 0.0
            dis = client.search("SELECT ad_group_ad.ad.id FROM ad_group_ad WHERE campaign.id = %d AND "
                                "ad_group_ad.policy_summary.approval_status = 'DISAPPROVED' AND ad_group_ad.status = 'ENABLED'" % cid)
            out.append({"key": key, "account": account["key"], "platform": "google", "name": live["name"],
                        "status": live["status"], "daily_budget": live["budget_usd"], "spend_today": round(t["cost"], 2),
                        "spend_month": round(mtd["cost"], 2), "month_budget": (pol or {}).get("month_budget_usd"),
                        "clicks_today": t["clicks"], "conv_today": t["conv"], "impressions_today": t["impr"],
                        "cpa_7d": round(w7["cost"] / w7["conv"], 2) if w7["conv"] else 0,
                        "cpa_30d": round(w30["cost"] / w30["conv"], 2) if w30["conv"] else 0,
                        "cost_30d": round(w30["cost"], 2), "conv_30d": w30["conv"], "conv_month": mtd["conv"],
                        "lost_budget_is": lb, "lost_rank_is": lr, "disapproved_ads": len(dis),
                        "hour": dt.datetime.fromtimestamp(now).hour, "currency": "USD"})
        except ToolError as e:
            errors.append({"campaign": key, "error": str(e)[:300]})
    return {"date": today.isoformat(), "campaigns": out, "errors": errors}


def _approval_queue(cfg, ctx):
    a = cfg.get("approvals") or {}
    secret = os.environ.get(a.get("secret_env", "ADRAILS_APPROVAL_SECRET"), "")
    if len(secret) < 32:
        raise ToolError("approvals need a secret of 32+ chars in env " + a.get("secret_env", "ADRAILS_APPROVAL_SECRET"))
    path = os.path.expanduser(a["path"]) if a.get("path") else state_path(cfg, "_none", "approvals.jsonl")
    return approvals.ApprovalQueue(path, secret.encode(), now=ctx.now, ttl_s=int(a.get("ttl_s", 86400)))


def _chain(cfg):
    return auditchain.AuditChain(state_path(cfg, "audit_chain", "audit-chain.jsonl"))


def register(srv, ctx, cfg):
    camp = {"campaign": {"type": "string", "description": "campaign key from adsops_campaigns"}}
    days = {"days": {"type": "integer", "description": "lookback window, default 30"}}

    def schema(props, req=("campaign",)):
        return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}

    @srv.tool("adsops_conv_audit", "Read-only. Audit conversion tracking for the campaign's account: primary goals, dead tags, "
              "double counting, auto-tagging, lead counting, smart bidding without enough conversions. Returns a 0-100 score, grade, "
              "and ranked findings with fixes. Run this before trusting any CPA or optimising toward conversions.",
              schema({**camp, **days}))
    def conv_audit_tool(args):
        _, _, _, _, _, client = ctx.open(args["campaign"])
        since, until = _dates(ctx.now, _days(args))
        r = {k: client.search(q.format(since=since, until=until)) for k, q in conv_audit.QUERIES.items()}
        return conv_audit.audit(r["actions"], r["action_stats"], r["campaigns"], r["customer"], args.get("days", 30),
                                goals=r.get("goals"))

    @srv.tool("adsops_diagnostics", "Read-only PPC diagnostics bundle for one campaign: wasted spend (zero-conversion search terms), "
              "impression share lost to budget vs rank, cost-weighted quality score with weak components, and day-over-baseline "
              "anomalies (z-score). Returns a ranked headline list plus details.", schema({**camp, **days}))
    def diagnostics_tool(args):
        _, _, _, eff, _, client = ctx.open(args["campaign"])
        since, until = _dates(ctx.now, _days(args))
        rows = {k: client.search(diagnostics.query(k, eff["campaign_id"], since, until)) for k in diagnostics.QUERIES}
        return diagnostics.bundle(**rows)

    @srv.tool("adsops_rsa_validate", "Pure check, no account access. Validate a Responsive Search Ad draft against Google's limits "
              "and common disapproval causes (lengths incl. double-width chars, pins, duplicates, caps, punctuation, phone numbers, "
              "unsubstantiated superlatives, banned phrases) before any write. Returns errors, warnings and fixes.",
              {"type": "object", "properties": {
                  "ad": {"type": "object", "description": "{headlines:[str|{text,pin}], descriptions:[...], final_url, path1, path2}"},
                  "policy": {"type": "object", "description": "optional {keywords:[], banned_phrases:[], allow_caps:[]}"}},
               "required": ["ad"], "additionalProperties": False})
    def rsa_tool(args):
        res = rsa_validator.validate_rsa(args["ad"], args.get("policy"))
        res["fixes"] = rsa_validator.suggest_fixes(res)
        return res

    @srv.tool("adsops_rules_check", "Read-only. Evaluate the automation rules in the campaign's POLICY file (founder-owned, agents "
              "can't edit it) against live metrics. Returns PROPOSALS and a desired-state patch; nothing is applied. Rules can only "
              "pause, add negatives, alert, or move budget inside the policy caps.", schema({**camp, **days}))
    def rules_tool(args):
        _, _, raw, eff, pol, client = ctx.open(args["campaign"])
        rs = (pol or {}).get("rules") or []
        if not rs:
            return {"proposals": [], "alerts": [], "note": "no rules in the policy file ('rules': [...])"}
        errs = rules.validate_rules(rs)
        if errs:
            raise ToolError("policy rules are invalid: " + "; ".join(errs[:5]))
        win = _days(args)
        since, until = _dates(ctx.now, win)
        snap = snapshot(ctx, [args["campaign"]])["campaigns"]
        ents = []
        if snap:
            c = snap[0]
            ents.append({"scope": "campaign", "id": args["campaign"], "name": c["name"], "daily_budget": c["daily_budget"],
                         "metrics": {win: {"cost": c["cost_30d"], "conversions": c["conv_30d"], "spend_today": c["spend_today"],
                                           "lost_budget": c["lost_budget_is"]}}})
        for r in client.search(diagnostics.query("search_terms", eff["campaign_id"], since, until)):
            m = r.get("metrics") or {}
            term = (r.get("searchTermView") or {}).get("searchTerm")
            if term:
                ents.append({"scope": "search_term", "id": term, "name": term, "metrics": {win: {
                    "cost": float(m.get("costMicros") or 0) / 1e6, "clicks": float(m.get("clicks") or 0),
                    "impressions": float(m.get("impressions") or 0), "conversions": float(m.get("conversions") or 0)}}})
        limits = {"min_daily_budget": (pol or {}).get("min_daily_budget_usd"), "max_daily_budget": (pol or {}).get("max_daily_budget_usd"),
                  "max_budget_step_pct": (pol or {}).get("max_budget_step_pct", 20)}
        res = rules.evaluate(rs, ents, limits)
        res["state_patch"] = rules.to_state_patch(res["proposals"])
        res["next"] = "copy state_patch into the desired-state file, commit, then adsops_plan"
        return res

    @srv.tool("adsops_monitor_check", "Read-only. Run monitors (CPA spike, pacing over/under, month forecast, zero impressions, "
              "disapprovals, budget-limited) over live snapshots of every configured campaign (or one). New alerts are de-duplicated "
              "for 24 h; with deliver=true they are posted to the webhook named in config alerts.webhook_env.",
              schema({"campaign": camp["campaign"], "deliver": {"type": "boolean"}}, req=()))
    def monitor_tool(args):
        snap = snapshot(ctx, [args["campaign"]] if args.get("campaign") else _camp_keys(cfg))
        mons = cfg.get("monitors") or [{"id": t, "type": t} for t in ("cpa_spike", "pacing_over", "pacing_under", "month_forecast",
                                                                       "zero_impressions", "disapprovals", "budget_limited")]
        errs = monitors.validate(mons)
        if errs:
            raise ToolError("monitor config invalid: " + "; ".join(errs[:5]))
        alerts = monitors.evaluate(mons, snap)
        sp = state_path(cfg, "monitor_state", "monitor-state.json")
        state = json.load(open(sp)) if os.path.isfile(sp) else {}
        new, state = monitors.dedupe(alerts, state, dt.datetime.fromtimestamp(ctx.now(), dt.timezone.utc).isoformat())
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        json.dump(state, open(sp, "w"))
        out = {"alerts": alerts, "new_alerts": new, "errors": snap["errors"]}
        if args.get("deliver") and new:
            env = (cfg.get("alerts") or {}).get("webhook_env", "ADRAILS_ALERT_WEBHOOK")
            url = os.environ.get(env)
            if not url:
                raise ToolError(f"deliver=true but env {env} is empty")
            out["delivery"] = monitors.deliver(monitors.brief(snap, new, "AdRails alerts"),
                                               monitors.webhook_notifier(url, __import__('adsops.aeo_tools', fromlist=['raw_http']).raw_http))
        return out

    @srv.tool("adsops_brief", "Read-only. A short markdown brief across every configured campaign: spend today, conversions, "
              "blended CPA and the top issues from the monitors. Built for a daily Slack/email post.",
              schema({}, req=()))
    def brief_tool(args):
        snap = snapshot(ctx, _camp_keys(cfg))
        mons = cfg.get("monitors") or [{"id": t, "type": t} for t in ("cpa_spike", "pacing_over", "zero_impressions",
                                                                       "disapprovals", "budget_limited")]
        return {"markdown": monitors.brief(snap, monitors.evaluate(mons, snap)), "errors": snap["errors"]}

    @srv.tool("adsops_fleet", "Read-only. Fleet view across every configured account and campaign: spend, budget, conversions and "
              "CPA by account and platform, plus the AGGREGATE cap check (config fleet_caps), which assumes platforms may "
              "deliver up to 2x the daily budget.", schema({}, req=()))
    def fleet_tool(args):
        snap = snapshot(ctx, _camp_keys(cfg))
        rows = [{**c, "campaign": c["key"]} for c in snap["campaigns"]]
        out = fleet.rollup(rows)
        if cfg.get("fleet_caps"):
            out["aggregate"] = fleet.aggregate_check(rows, cfg["fleet_caps"])
        out["errors"] = snap["errors"]
        return out

    @srv.tool("adsops_realloc_suggest", "Read-only. Recommend moving daily budget from weak campaigns to budget-limited strong ones "
              "inside each policy's caps. The total never rises. Returns moves plus per-campaign desired-state patches to review.",
              schema({}, req=()))
    def realloc_tool(args):
        snap = snapshot(ctx, _camp_keys(cfg))
        caps = {}
        for c in snap["campaigns"]:
            pol = ctx.open(c["key"])[4] or {}
            caps[c["key"]] = {"min_daily_budget": pol.get("min_daily_budget_usd", 0),
                              "max_daily_budget": pol.get("max_daily_budget_usd", c["daily_budget"])}
        res = budget_realloc.recommend(snap["campaigns"], {"campaigns": caps, "total_daily_cap": (cfg.get("fleet_caps") or {})
                                                           .get("daily_total")})
        res["state_patches"] = budget_realloc.as_state_patches(res)
        return res

    @srv.tool("adsops_drift", "Read-only. Who changed the account outside AdRails? Reads Google's change history for the last N "
              "days (max 30) and flags out-of-band edits (UI, scripts, auto-applied recommendations), highlighting budget, status "
              "and bidding changes.", schema({**camp, "days": {"type": "integer", "description": "1-30, default 7"}}))
    def drift_tool(args):
        _, _, _, _, _, client = ctx.open(args["campaign"])
        d = args.get("days", 7)
        if not 1 <= d <= 30:
            raise ToolError("days must be 1..30 (Google keeps 30 days of change history)")
        since = dt.datetime.fromtimestamp(ctx.now() - d * 86400).strftime("%Y-%m-%d %H:%M:%S")
        until = dt.datetime.fromtimestamp(ctx.now() + 60).strftime("%Y-%m-%d %H:%M:%S")
        ev = change_history.classify(change_history.normalize(client.search(change_history.QUERY(since, 1000, until))))
        rep = change_history.drift_report(ev)
        rep["events"] = ev[:50]
        return rep

    @srv.tool("adsops_keyword_ideas", "Read-only. Keyword Planner ideas (searches, competition, top-of-page bid range) for seed "
              "keywords and/or a URL, clustered by theme with an intent label. Needs a Basic-access developer token.",
              schema({**camp, "seeds": {"type": "array", "items": {"type": "string"}}, "url": {"type": "string"}},
                     req=("campaign", "seeds")))
    def kw_tool(args):
        account, _, _, _, _, client = ctx.open(args["campaign"])
        path, body = kwplan.build_ideas_request(str(account["customer_id"]).replace("-", ""), args["seeds"], url=args.get("url"))
        ideas = kwplan.parse_ideas(client._post(path, body))
        for i in ideas:
            i["intent"] = kwplan.intent(i["keyword"])
        return {"ideas": ideas[:200], "clusters": kwplan.cluster(ideas)}

    @srv.tool("adsops_offline_conv_plan", "Build (never send) an offline / enhanced conversion upload from CRM rows. Emails, phones "
              "and names are normalised and SHA-256 hashed LOCALLY, so raw PII never leaves this machine. Returns a redacted preview "
              "and counts; validate_only stays true.",
              {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "object"}},
                                                "conversion_action": {"type": "string", "description": "customers/X/conversionActions/Y"},
                                                "currency": {"type": "string"}},
               "required": ["rows", "conversion_action"], "additionalProperties": False})
    def offline_tool(args):
        p = offline_conv.build_google_click_conversions(args["rows"], args["conversion_action"], args.get("currency", "USD"))
        return {"preview": offline_conv.redact_report(p), "count": len(p["conversions"]), "skipped": p["skipped"],
                "validate_only": True}

    @srv.tool("adsops_history", "Read-only. Git history of the campaign's desired-state file: every change agents or lanes made, "
              "newest first.", schema({**camp, "limit": {"type": "integer"}}))
    def history_tool(args):
        _, c, _, _, _, _ = ctx.open(args["campaign"])
        return {"history": rollback.history(os.path.expanduser(c["state_file"]), _git_runner, min(args.get("limit", 20), 100))}

    @srv.tool("adsops_rollback", "Undo: build the desired state as of an earlier commit (guards, account and campaign ids are never "
              "reverted, and negatives are never removed). With commit=true it writes and git-commits the state file; then run "
              "adsops_plan and adsops_apply as usual. Nothing reaches the ad account from this tool.",
              schema({**camp, "to_commit": {"type": "string", "description": "short sha or HEAD~N"},
                      "commit": {"type": "boolean"}}, req=("campaign", "to_commit")))
    def rollback_tool(args):
        _, c, _, _, _, _ = ctx.open(args["campaign"])
        sf = os.path.expanduser(c["state_file"])
        plan = rollback.plan_rollback(sf, args["to_commit"], _git_runner)
        if args.get("commit") and plan["changes"]:
            plan["committed"] = rollback.write_and_commit(
                sf, plan["new_state"], f"adsops_rollback {args['campaign']}: to {args['to_commit']} [via adsops]", _git_runner)
            plan["next"] = "adsops_plan, then adsops_apply"
        return plan

    @srv.tool("adsops_approval_request", "Ask a human to approve a plan before it can be committed (when config approvals.mode is on). "
              "Returns approve/reject tokens to send to the approver; the tokens are HMAC-signed and expire.",
              schema({"plan_id": {"type": "string"}, **camp, "summary": {"type": "string"}, "risk": {"type": "string"}},
                     req=("plan_id", "campaign", "summary")))
    def appr_req(args):
        return _approval_queue(cfg, ctx).request(args["plan_id"], args["campaign"], args["summary"], args.get("risk", "high"))

    @srv.tool("adsops_approval_decide", "Record a human decision using a signed approve/reject token. Forged, expired or reused tokens "
              "are refused.", schema({"token": {"type": "string"}, "decided_by": {"type": "string"}}, req=("token",)))
    def appr_dec(args):
        return _approval_queue(cfg, ctx).decide(args["token"], args.get("decided_by", "human"))

    @srv.tool("adsops_approvals", "List approval requests (newest first), optionally by status.",
              schema({"status": {"type": "string"}}, req=()))
    def appr_list(args):
        return {"requests": [{k: v for k, v in r.items() if not k.endswith("_token")}
                             for r in _approval_queue(cfg, ctx).list(status=args.get("status"))]}

    @srv.tool("adsops_audit_verify", "Verify the tamper-evident audit chain of every mutating call (hash-linked entries). Returns ok, "
              "entry count, the first bad entry if any, and a head digest to publish so later truncation is detectable.",
              schema({}, req=()))
    def audit_verify(args):
        ch = _chain(cfg)
        return {**ch.verify(), "anchor": auditchain.anchor_digest(ch._path) if os.path.isfile(ch._path) else None}

    @srv.tool("adsops_audit_tail", "The last N audit-chain entries (redacted), newest first.",
              schema({"n": {"type": "integer"}, "event": {"type": "string"}}, req=()))
    def audit_tail(args):
        return {"entries": _chain(cfg).tail(min(args.get("n", 20), 200), event=args.get("event"))}

    @srv.tool("adsops_sbom", "Supply-chain manifest of this AdRails install: a CycloneDX 1.5 SBOM and a SHA-256 manifest of every "
              "package file, so you can verify the code you run is the code that was released.",
              schema({"verify_against": {"type": "string", "description": "optional manifest digest to compare"}}, req=()))
    def sbom_tool(args):
        from adsops import __version__
        root = os.path.dirname(os.path.abspath(__file__))
        man = sbom.file_manifest(root)
        digest = sbom.manifest_digest(man)
        out = {"digest": digest, "files": len(man), "sbom": sbom.cyclonedx("adrails", __version__, man)}
        if args.get("verify_against"):
            out["matches"] = args["verify_against"] == digest
        return out

    @srv.tool("adsops_whoami", "This server's identity and controls: version, principal and roles (RBAC), approval mode, audit chain "
              "path, and whether a guard heartbeat is configured.", schema({}, req=()))
    def whoami(args):
        from adsops import __version__
        rb = rbac_mod.Rbac.from_config(cfg, env=dict(os.environ))
        return {"version": __version__, "rbac": rb.describe(), "approvals": (cfg.get("approvals") or {}).get("mode", "off"),
                "audit_chain": state_path(cfg, "audit_chain", "audit-chain.jsonl"),
                "guard_heartbeat": bool(cfg.get("guard_heartbeat"))}


def _exposure_raising(tool, args):
    if tool in ("adsops_apply", "adsops_openai_apply"):
        return True                                     # direction unknown without a plan: gate it
    return any(k in args for k in ("daily_budget_usd", "end_date")) or str(args.get("status", "")).upper() == "ENABLED"


def harden(srv, ctx, cfg):
    """Wrap every registered tool with RBAC -> approval gate -> audit chain. Call LAST in build_server."""
    rb = rbac_mod.Rbac.from_config(cfg, env=dict(os.environ))
    mode = (cfg.get("approvals") or {}).get("mode", "off")
    if mode not in ("off", "exposure", "all"):
        raise ToolError("approvals.mode must be off | exposure | all")
    for name, entry in srv._tools.items():
        fn = entry["fn"]

        def guarded(args, _fn=fn, _name=name):
            committing = (_name in ("adsops_apply", "adsops_openai_apply") and args.get("commit")) or (_name == "adsops_set" and args.get("apply"))
            try:
                rb.check(_name)
                if _name in GATED and committing and mode != "off" and (mode == "all" or _exposure_raising(_name, args)):
                    if not args.get("plan_id"):
                        raise RailViolation("approvals are on: pass the approved plan_id")
                    _approval_queue(cfg, ctx).require_approved(args["plan_id"])
                result = _fn(args)
            except ToolError as e:
                if _name in MUTATING:
                    _record(cfg, _name, args, False, str(e))
                raise
            if _name in MUTATING:
                _record(cfg, _name, args, True, None, result)
            return result

        entry["fn"] = guarded


# Local-business tools carry customer PII in free text (names, notes, gate codes, addresses). The chain is append-only and hash-linked,
# so PII written there can never be removed: record ONLY structural args for these tools (review finding #1, 2026-11-06).
_PII_TOOL_PREFIXES = ("adsops_local_", "adsops_leads", "adsops_lsa_", "adsops_reviews")
_SAFE_ARGS = ("action", "id", "stage", "source", "days", "commit", "account", "rebuild", "op", "channel", "start_date", "since",
              "until", "limit", "write")


def _audit_args(tool, args):
    if not tool.startswith(_PII_TOOL_PREFIXES):
        return {k: v for k, v in args.items() if k not in ("rows",)}
    out = {k: args[k] for k in _SAFE_ARGS if k in args and isinstance(args[k], (str, int, float, bool))}
    if isinstance(args.get("decisions"), list):
        out["decisions"] = [{k: d.get(k) for k in ("lead_id", "answer", "reason")} for d in args["decisions"] if isinstance(d, dict)][:200]
    if isinstance(args.get("done"), list):
        out["done"] = [str(x)[:8] for x in args["done"]][:100]
    if isinstance(args.get("fields"), dict):
        out["fields_set"] = sorted(args["fields"])[:50]          # which profile keys changed, never their values
    for k in ("rows", "lead", "note", "text", "lost_reason", "review", "reviews", "urls", "pages"):
        if k in args:
            out["has_" + k] = len(args[k]) if isinstance(args[k], (list, dict, str)) else True
    return out


def _record(cfg, tool, args, ok, error, result=None):
    """Append to the audit chain. Failure to audit a successful mutation is surfaced, never swallowed."""
    data = {"tool": tool, "ok": ok, "args": _audit_args(tool, args)}
    if error:
        # scrub BEFORE persisting: upstream errors can echo tokens/keys (qa_secrets canary leak, 2026-11-06)
        data["error"] = scrub(error, 300)
    if isinstance(result, dict):
        data["result_keys"] = sorted(result)[:20]
        for k in ("plan_id", "committed", "request_id", "status"):
            if k in result:
                data[k] = result[k]
    try:
        _chain(cfg).append("tool_call", data, actor=os.environ.get("ADRAILS_PRINCIPAL") or "agent")
    except OSError as e:
        if ok:
            raise ToolError(f"mutation succeeded but the audit chain could not be written: {e}") from None


# ── selftest: glue only (the logic modules test themselves) ──────────────────────────────────
def selftest():
    import tempfile
    from adsops import protocol
    n = 0
    tmp = tempfile.mkdtemp()

    class Client:
        def __init__(self):
            self.queries = []

        def search(self, q):
            self.queries.append(q)
            if "FROM change_event" in q:
                assert "<=" in q, "change_event needs a finite range"
                return [{"changeEvent": {"changeDateTime": "2026-11-05 09:00:00", "changeResourceType": "CAMPAIGN_BUDGET",
                                         "clientType": "GOOGLE_ADS_WEB_CLIENT", "userEmail": "x@y.com",
                                         "resourceChangeOperation": "UPDATE", "changedFields": "amount_micros"}}]
            if "FROM search_term_view" in q:
                return [{"searchTermView": {"searchTerm": "free rent"}, "metrics": {"costMicros": "60000000", "clicks": "9",
                                                                                     "impressions": "40", "conversions": 0}}]
            if "campaign_budget.amount_micros" in q:
                return [{"campaign": {"id": "42", "name": "c42", "status": "ENABLED"}, "campaignBudget": {"amountMicros": "30000000"},
                         "metrics": {"costMicros": "5000000"}}]
            return []

    cl = Client()

    class Ctx:
        now = staticmethod(lambda: 1793880000.0)

        def open(self, key):
            if key != "c42":
                raise ToolError("unknown campaign")
            return ({"key": "acct", "customer_id": "111-222-3333"}, {"key": "c42", "state_file": os.path.join(tmp, "s.json")},
                    {}, {"campaign_id": 42}, {"rules": [{"id": "neg", "scope": "search_term", "then": {"add_negative": {"match_type": "PHRASE"}},
                                                         "when": {"all": [{"metric": "cost", "op": ">=", "value": 50},
                                                                          {"metric": "conversions", "op": "==", "value": 0}]}}],
                                              "max_daily_budget_usd": 50}, cl)

        def end_field(self):
            return "end_date_time"

    cfg = {"accounts": [{"key": "acct", "campaigns": [{"key": "c42"}]}], "audit_chain": os.path.join(tmp, "chain.jsonl"),
           "monitor_state": os.path.join(tmp, "mon.json"), "approvals": {"mode": "all", "path": os.path.join(tmp, "q.jsonl")}}
    srv = protocol.Server("t", "0")
    calls = []

    @srv.tool("adsops_apply", "x", {"type": "object", "properties": {"commit": {"type": "boolean"}, "plan_id": {"type": "string"}},
                                    "additionalProperties": False})
    def apply(args):
        calls.append(args)
        return {"status": "applied"}

    register(srv, Ctx(), cfg)
    os.environ["ADRAILS_APPROVAL_SECRET"] = "x" * 40
    harden(srv, Ctx(), cfg)

    def call(name, args):
        return srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]

    names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert len(names) >= 20 and "adsops_conv_audit" in names; n += 1
    r = call("adsops_apply", {"commit": True})
    assert r["isError"] and "plan_id" in r["content"][0]["text"] and not calls; n += 1          # gate: no plan_id
    r = call("adsops_apply", {"commit": True, "plan_id": "p1"})
    assert r["isError"] and "approval" in r["content"][0]["text"] and not calls; n += 1          # gate: not approved
    q = _approval_queue(cfg, Ctx())
    tok = call("adsops_approval_request", {"plan_id": "p1", "campaign": "c42", "summary": "budget 30->36"})["structuredContent"]
    call("adsops_approval_decide", {"token": tok["approve_token"]})
    r = call("adsops_apply", {"commit": True, "plan_id": "p1"})
    assert not r["isError"] and len(calls) == 1; n += 1                                          # approved -> runs
    assert not call("adsops_apply", {})["isError"]; n += 1                                       # validate-only never gated
    v = call("adsops_audit_verify", {})["structuredContent"]
    assert v["ok"] and v["entries"] >= 4, v; n += 1                                              # refusals are audited too
    tail = call("adsops_audit_tail", {"n": 10})["structuredContent"]["entries"]
    assert not any("approve_token" in json.dumps(e) and tok["approve_token"] in json.dumps(e) for e in tail); n += 1
    d = call("adsops_drift", {"campaign": "c42", "days": 7})["structuredContent"]
    assert d["out_of_band"] == 1 and d["risky"]; n += 1
    assert call("adsops_drift", {"campaign": "c42", "days": 31})["isError"]; n += 1
    rr = call("adsops_rules_check", {"campaign": "c42"})["structuredContent"]
    assert any(p["action"] == "add_negative" for p in rr["proposals"]), rr; n += 1
    assert rr["state_patch"]["negatives_add"][0]["text"] == "free rent"; n += 1
    v = call("adsops_rsa_validate", {"ad": {"headlines": ["a", "b"], "descriptions": ["d"], "final_url": "http://x"}})
    assert not v["structuredContent"]["ok"]; n += 1
    m = call("adsops_monitor_check", {})["structuredContent"]
    assert "alerts" in m and os.path.isfile(cfg["monitor_state"]); n += 1
    # RBAC: a viewer may read but never apply, and the refusal has no side effect
    cfg2 = dict(cfg, rbac={"enabled": True, "principals": {"g": ["viewer"]}}, approvals={"mode": "off"})
    os.environ["ADRAILS_PRINCIPAL"] = "g"
    srv2 = protocol.Server("t", "0")
    srv2.tool("adsops_apply", "x", {"type": "object", "properties": {"commit": {"type": "boolean"}}})(apply)
    register(srv2, Ctx(), cfg2)
    harden(srv2, Ctx(), cfg2)
    before = len(calls)
    r = srv2.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "adsops_apply", "arguments": {"commit": True}}})
    assert r["result"]["isError"] and len(calls) == before; n += 1
    r = srv2.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "adsops_drift", "arguments": {"campaign": "c42"}}})
    assert not r["result"]["isError"]; n += 1
    del os.environ["ADRAILS_PRINCIPAL"]
    try:
        harden(protocol.Server("t", "0"), Ctx(), dict(cfg, approvals={"mode": "maybe"}))
        raise AssertionError("bad mode accepted")
    except ToolError:
        n += 1
    return n
