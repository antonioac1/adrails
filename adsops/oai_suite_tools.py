"""ChatGPT Ads growth suite (the maintainers glue, 2026-11-06): the tools a business needs to win on ChatGPT Ads, beyond launch.

  adsops_openai_ad_check          check chat_card copy/URLs/images before review rejects them (pure)
  adsops_openai_draft_campaign    business brief -> desired-state + policy drafts the engine accepts (paused, capped, unapproved)
  adsops_openai_ab_report         which ad wins, with intervals and Bonferroni-corrected significance (read-only)
  adsops_openai_send_conversions  CRM leads -> Conversions API events, hashed locally; validate-only unless commit=true
  adsops_openai_audience          hashed customer lists -> custom audiences (create/add/remove/replace); plan unless commit=true

Logic lives in oai_creative / oai_builder / oai_experiments / oai_conversions / oai_audiences (DeepSeek-built from specs/ent/oai_*.md
with the OpenAI doc excerpts embedded; black-box suites + Lane-A acceptance: drafts pass the REAL oai_engine/oai_policy validators).
Rails reused from oai_tools.OAI: account mode (disabled/read_only/live) and key-belongs-to-account verification. Writes need mode=live.
Raw emails/phones/names never leave this process: they are normalised and SHA-256 hashed before anything is built or sent.
"""
import csv
import io
import json
import os

from . import oai_audiences, oai_builder, oai_conversions, oai_creative, oai_experiments, oai_policy
from .errors import ToolError
from .platforms import openai_ads

MAX_ROWS = 200_000


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _rows(args):
    if args.get("rows"):
        return list(args["rows"])[:MAX_ROWS]
    p = os.path.expanduser(str(args.get("csv_path") or ""))
    if not p or not os.path.isfile(p):
        raise ToolError("pass rows or an existing csv_path")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)][:MAX_ROWS]


def register(srv, ctx, cfg, oai):
    ACCT = {"account": {"type": "string", "description": "openai_ads account key from the config"}}
    CAMP = {"campaign": {"type": "string", "description": "openai_ads campaign key"}}

    @srv.tool("adsops_openai_ad_check", "ChatGPT Ads: check chat_card ads before submitting them: OpenAI's limits (title 3-50, body <=100, "
              "https URL <=2048, 1 or 4-6 images), allowed domains and required UTM tags from the campaign policy, duplicates across "
              "an A/B set, and best-practice warnings (caps, superlatives, phone numbers). Pure check, no account access.",
              _schema({"ads": {"type": "array", "items": {"type": "object"}}, **CAMP,
                       "policy": {"type": "object", "description": "optional {allowed_domains, required_utm, banned_phrases, brand}"}}, ["ads"]))
    def ad_check(args):
        pol = dict(args.get("policy") or {})
        if args.get("campaign"):
            from . import config
            _, camp = config.resolve_campaign(cfg, args["campaign"])
            p = oai_policy.load(camp["policy_file"])
            pol.setdefault("allowed_domains", p.get("allowed_domains") or [])
            pol.setdefault("required_utm", oai_policy.required_utm(p, args["campaign"]) or {})
        return oai_creative.check_set(args["ads"], pol)

    @srv.tool("adsops_openai_draft_campaign", "ChatGPT Ads: turn a business brief (account, landing page, offer, budget, lifetime cap, "
              "context hints people ask ChatGPT, agent-written ads) into a desired-state file and a policy file that the AdRails engine "
              "accepts: everything paused, conservative caps, UTM-tagged URLs, activation off until a human approves. Returns both "
              "documents and next steps; with write=true saves them as drafts beside the config (never commits, never touches the account).",
              _schema({"brief": {"type": "object"}, "write": {"type": "boolean"}}, ["brief"]))
    def draft(args):
        d = oai_builder.draft(args["brief"])
        if args.get("write"):
            from .enterprise_tools import state_path
            base = state_path(cfg, "_none", "openai-drafts")
            os.makedirs(base, exist_ok=True)
            key = args["brief"]["key"]
            sp, pp = os.path.join(base, f"desired_state.{key}.openai.json"), os.path.join(base, f"policy.{key}.openai.json")
            for path, doc in ((sp, d["state"]), (pp, d["policy"])):
                if os.path.exists(path):
                    raise ToolError(f"{path} exists; drafts never overwrite")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2)
                    fh.write("\n")
            d["written"] = {"state_file": sp, "policy_file": pp}
        return d

    @srv.tool("adsops_openai_ab_report", "ChatGPT Ads: which ad is winning? Per-ad CTR (and conversion rate when available) with 95% "
              "intervals, a leader, Bonferroni-corrected comparisons and a pause recommendation only for ads significantly worse. "
              "Reads the campaign's ad-level insights, or takes rows you pass. Read-only.",
              _schema({**CAMP, "days": {"type": "integer"}, "rows": {"type": "array", "items": {"type": "object"}},
                       "metric": {"type": "string", "enum": ["ctr", "cvr"]}, "min_impressions": {"type": "integer"},
                       "min_clicks": {"type": "integer"}}))
    def ab_report(args):
        rows = args.get("rows")
        if not rows:
            if not args.get("campaign"):
                raise ToolError("pass campaign or rows")
            days = args.get("days", 14)
            if not 1 <= days <= 90:
                raise ToolError("days must be 1..90")
            _, _, st, pol, client = oai.open(args["campaign"])
            tree = oai.tree(client, st, pol, with_spend=False)
            if not tree["campaign"]:
                return {"note": "campaign not created yet", "rows": []}
            import time
            t = int(ctx.now())
            start = openai_ads.local_midnight(t, tree["account"].get("timezone") or "UTC") - (days - 1) * 86400
            raw = client.insights("/campaigns/" + tree["campaign"]["id"], {
                "time_granularity": "all", "aggregation_level": "ad", "limit": 2000,
                "fields[]": ["ad.id", "ad.impressions", "ad.clicks", "ad.spend", "ad.conversions"],
                "time_ranges[]": [json.dumps({"type": "unix_range", "start": start,
                                              "end": openai_ads.insights_end(t, tree["account"].get("timezone") or "UTC")})]})
            names = {a.get("id"): a.get("name") for g in tree.get("ad_groups", []) for a in g.get("ads", [])}
            agg = {}
            for r in raw or []:
                a = r.get("ad") or r
                i = a.get("id")
                if not i:
                    continue
                x = agg.setdefault(i, {"id": i, "name": names.get(i) or i, "impressions": 0, "clicks": 0, "conversions": 0, "spend": 0.0})
                for k in ("impressions", "clicks", "conversions"):
                    x[k] += int(float(a.get(k) or 0))
                x["spend"] += float(a.get("spend") or 0) / (1e6 if float(a.get("spend") or 0) > 1e5 else 1)
            rows = list(agg.values())
        return oai_experiments.ab_report(rows, metric=args.get("metric", "ctr"), min_impressions=args.get("min_impressions", 1000),
                                         min_clicks=args.get("min_clicks", 30))

    @srv.tool("adsops_openai_send_conversions", "ChatGPT Ads: send real conversions (CRM leads, qualified leads, sales) to the OpenAI "
              "Conversions API so ChatGPT Ads can optimise toward them. Emails, phones and names are normalised and SHA-256 hashed locally; "
              "events older than 7 days are skipped; event ids are deterministic so re-sends dedupe. Validate-only unless commit=true.",
              _schema({**ACCT, "rows": {"type": "array", "items": {"type": "object"}}, "csv_path": {"type": "string"},
                       "event_type": {"type": "string"}, "pixel_id": {"type": "string"}, "commit": {"type": "boolean"}}, ["account"]))
    def send_conversions(args):
        account = oai.account(args["account"])
        if args.get("commit"):
            oai.client(account, need="write")                 # mode must be live and the key must belong to this account
        pixel = args.get("pixel_id") or account.get("pixel_id")
        if not pixel:
            raise ToolError("no pixel_id: pass it or set accounts[].pixel_id (adsops_openai_conversions_setup creates the pixel)")
        kf = os.path.expanduser(account.get("capi_key_file") or "")
        if not kf or not os.path.isfile(kf):
            raise ToolError("no Conversions API key file (accounts[].capi_key_file); adsops_openai_conversions_setup can create one")
        if os.stat(kf).st_mode & 0o077:
            raise ToolError("the Conversions API key file must not be group/world readable (chmod 600)")
        built = oai_conversions.build_events(_rows(args), event_type=args.get("event_type", "lead"),
                                             now_ms=int(ctx.now() * 1000))
        if not built["events"]:
            return {"sent": 0, "skipped": built["skipped"][:50], "note": "no eligible events"}
        key = open(kf, encoding="utf-8").read().strip()
        res = oai_conversions.send(built["events"], pixel, key, _raw_http,
                                   validate_only=not args.get("commit"))
        return {**{k: v for k, v in res.items() if k != "results"}, "results": res.get("results", [])[:20],
                "skipped": built["skipped"][:50], "preview": oai_conversions.redact_preview(built["events"])}

    @srv.tool("adsops_openai_audience", "ChatGPT Ads custom audiences from a customer list (CSV or rows): emails/phones are hashed "
              "locally into the documented email_sha256/phone_number_sha256 file. op=create|add|remove|replace. Returns the plan and counts; "
              "commit=true uploads the hashed file and creates/updates the audience (mode=live accounts only). Use it to exclude current "
              "clients or target look-alike customer lists.",
              _schema({**ACCT, "op": {"type": "string", "enum": ["create", "add", "remove", "replace"]}, "name": {"type": "string"},
                       "audience_id": {"type": "string"}, "rows": {"type": "array", "items": {"type": "object"}},
                       "csv_path": {"type": "string"}, "commit": {"type": "boolean"}}, ["account", "op"]))
    def audience(args):
        account = oai.account(args["account"])
        csv_text, stats = oai_audiences.build_file(_rows(args))
        plan = oai_audiences.plan(args["op"], args.get("name") or "", _rows(args), audience_id=args.get("audience_id"))
        out = {"plan": {k: v for k, v in plan.items() if k != "file_bytes"}, "stats": stats}
        if args.get("commit"):
            client = oai.client(account, need="write")
            out["result"] = oai_audiences.execute(client, plan, csv_text, commit=True)
        return out


def _raw_http(method, url, headers, body):
    from .aeo_tools import raw_http
    return raw_http(method, url, headers, body)
