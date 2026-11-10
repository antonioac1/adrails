"""Guarded content pipeline (AdRails 0.12, 2026-11-10): MCP tools + the `seo-run` one-shot for cron/systemd.

The selling point is the guardrails, not the writing: topics come from Search Console evidence, a model writes, code
checks every number and claim against the approved FACTS file and verbatim source quotes, a model from a DIFFERENT family
attacks and edits it (mandatory; same family, silent fallback or no verdict = hold), and only then is there an approved
Markdown bundle (page.md + IMPLEMENT.md for the customer's own AI agent). Publishing is a separate, gated step (human
approval by default; auto only with caps, a time window and a HOLD switch). Results are measured at 14/28/56 days and
summarised into a visible, operator-editable LESSONS.md.

Tools (sites come from config "seo_content": {"profiles": {"<name>": "/path/site-profile.json"}} - an agent can never
point the pipeline at an arbitrary file):
  seo_content_facts     propose a FACTS file from the site's own pages (never approved automatically)
  seo_content_plan      evidence-picked topics (Search Console 8-40 positions, questions, thin/starved audit pages)
  seo_content_draft     write + deterministic checks (+ one fix round); saves an unreviewed draft
  seo_content_review    mandatory independent adversarial review; approve -> bundle, else hold with reasons
  seo_content_bundle    list / read / verify approved bundles (IMPLEMENT.md, page.md)
  seo_content_publish   request approval, dry-run or publish one bundle through every gate
  seo_content_results   14/28/56-day Search Console results per published piece + scoreboard (optionally update lessons)
  seo_content_lessons   read LESSONS.md (what the planner and writer are told)
CLI: `adsops-mcp seo-run --profile site-profile.json [--stage run|plan|results|publish|facts] ...` (no ports, one shot).
"""
import datetime as dt
import json
import os
import sys
import time

from .errors import RailViolation, ToolError, scrub
from .content import bundle as bundle_mod
from .content import facts as facts_mod
from .content import learn as learn_mod
from .content import pipeline
from .content import publish as publish_mod

TOOLS = ("seo_content_facts", "seo_content_plan", "seo_content_draft", "seo_content_review", "seo_content_bundle",
         "seo_content_publish", "seo_content_results", "seo_content_lessons")
# One plain line each, only where an EXISTING paid extra genuinely helps (positioning rules: the free core stays complete).
PANEL_HINT = ("Optional paid extra: the AI visibility panel (adsops_paid, service \"panel\") asks ChatGPT, Perplexity, Gemini "
              "and Claude your questions, so you can see whether these pages start being cited. https://adrails.dev/pricing/")
STAMP_HINT = ("Optional paid extra: a Bitcoin timestamp of your audit-chain head (adsops_paid, service \"stamp\") proves later "
              "that this publish record existed unedited. https://adrails.dev/pricing/")


def _profiles(cfg):
    m = ((cfg or {}).get("seo_content") or {}).get("profiles") or {}
    if not isinstance(m, dict):
        raise ToolError("config seo_content.profiles must map a site name to a profile file")
    return {str(k): os.path.expanduser(str(v)) for k, v in m.items()}


def _profile_path(cfg, site):
    m = _profiles(cfg)
    if not m:
        raise ToolError("no content sites configured: add \"seo_content\": {\"profiles\": {\"mysite\": \"/path/site-profile.json\"}} "
                        "to the config (see examples/seo-server/)")
    if not site:
        if len(m) == 1:
            return next(iter(m.values()))
        raise ToolError("several sites configured; pass site (one of %s)" % ", ".join(sorted(m)))
    if site not in m:
        raise ToolError("unknown site %r (configured: %s)" % (scrub(site, 60), ", ".join(sorted(m))))
    return m[site]


def make_io(cfg, profile, gsc_client=None, fetch=None, http=None, run_cmd=None, now=time.time):
    client = gsc_client
    if client is None:
        try:
            from .seo_tools import gsc_client_for
            client = gsc_client_for(cfg or {})
        except ToolError:
            client = None
    prop = profile.get("gsc_site") or profile["site"]

    def gsc(site, start, end, dims):
        return client.query(prop, start, end, dims)
    return pipeline.IO(fetch=fetch, http=http, run_cmd=run_cmd, gsc=gsc if client is not None else None, now=now)


def _audit_run(cfg, host):
    try:
        from .seo import runs
        from .seo_tools import store_dir
        return runs.RunStore(store_dir(cfg or {})).latest(runs.safe_host(host))
    except (ToolError, OSError, ValueError):
        return None


def _queue(cfg, ctx):
    try:
        from .enterprise_tools import _approval_queue
        return _approval_queue(cfg or {}, ctx)
    except ToolError:
        return None


def _chain(cfg):
    from .enterprise_tools import _chain as ch
    return ch(cfg or {})


def _bundle_path(profile, bundle_id):
    p = pipeline.paths(profile)
    if not isinstance(bundle_id, str) or not bundle_id or "/" in bundle_id or bundle_id.startswith(".") or len(bundle_id) > 120:
        raise ToolError("bundle_id must be a bundle directory name from seo_content_bundle")
    path = os.path.join(p["bundles"], bundle_id)
    if not os.path.isfile(os.path.join(path, "MANIFEST.json")):
        raise ToolError("no bundle %s" % bundle_id)
    return path


def list_bundles(profile):
    p = pipeline.paths(profile)
    out = []
    try:
        names = sorted(os.listdir(p["bundles"]), reverse=True)
    except FileNotFoundError:
        return out
    for name in names[:100]:
        mf = os.path.join(p["bundles"], name, "MANIFEST.json")
        if os.path.isfile(mf):
            try:
                with open(mf, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            out.append({"bundle_id": name, "created_at": m.get("created_at"), "verdict": m.get("verdict"),
                        "writer_model": m.get("writer_model"), "reviewer_model": m.get("reviewer_model")})
    return out


def register(srv, ctx, cfg, gsc_client=None, fetch=None, http=None, run_cmd=None):
    now = (ctx.now if ctx else time.time)

    def setup(args, need_chains=True):
        path = _profile_path(cfg, args.get("site"))
        if need_chains:
            profile, facts, chains = pipeline.load(path)
        else:
            profile, facts, chains = facts_mod.load_profile(path), None, None
        io = make_io(cfg, profile, gsc_client, fetch, http, run_cmd, now)
        pipeline._guard(pipeline.paths(profile))
        return profile, facts, chains, io

    site = {"site": {"type": "string", "description": "site name from config seo_content.profiles (optional if only one)"}}

    @srv.tool("seo_content_facts", "Onboarding for the content pipeline: read the site's own pages and PROPOSE a facts file "
              "(sentences with numbers or business claims, each with its source URL) for a human to review. Writes "
              "FACTS.proposed.json beside the site profile; it is never used until a person edits it, sets approved_by "
              "and approved_at, and saves it as the profile's facts file. The pipeline refuses to draft without one.",
              {"type": "object", "properties": dict(site, max_facts={"type": "integer"}), "required": [],
               "additionalProperties": False})
    def facts_tool(args):
        profile, _, _, io = setup(args, need_chains=False)
        known, corpus, complete, errs = pipeline.site_pages(io, profile, time.monotonic() + 600)
        prop = facts_mod.propose(corpus, max_facts=max(1, min(int(args.get("max_facts") or 60), 200)))
        out = os.path.join(profile["_dir"], "FACTS.proposed.json")
        facts_mod.write_proposal(out, prop)
        return {"written": out, "facts_proposed": len(prop["facts"]), "pages_read": len(corpus), "site_complete": complete,
                "next": "A human reviews every line, deletes anything untrue, sets approved_by/approved_at, removes "
                        "\"proposed\" and saves it as " + profile["facts_file"]}

    @srv.tool("seo_content_plan", "Pick content topics from evidence, read-only: Search Console queries ranking 8-40 "
              "(28 days), question queries, and thin/starved pages from the last stored seo_audit_run. Prefers extending "
              "an existing page over a new URL; refuses brand/competitor/unsafe queries and doorway patterns (place or "
              "keyword swaps). Uses LESSONS.md weights.",
              {"type": "object", "properties": dict(site, limit={"type": "integer"}), "required": [],
               "additionalProperties": False})
    def plan_tool(args):
        profile, facts, chains, io = setup(args)
        return pipeline.plan(profile, io, audit_run=_audit_run(cfg, profile["host"]),
                             limit=max(1, min(int(args.get("limit") or 5), 20)))

    @srv.tool("seo_content_draft", "Write one page for a planned topic with the configured writer chain, then run every "
              "deterministic check (numbers traced to FACTS or verbatim source quotes, banned phrases, competitors, "
              "duplicates vs the live site, retention for page extensions, links, JSON-LD, author) with one fix round. "
              "Saves an UNREVIEWED draft; nothing is publishable until seo_content_review approves it.",
              {"type": "object", "properties": dict(site, candidate_id={"type": "string"}), "required": ["candidate_id"],
               "additionalProperties": False})
    def draft_tool(args):
        profile, facts, chains, io = setup(args)
        pl = pipeline.plan(profile, io, audit_run=_audit_run(cfg, profile["host"]), limit=20)
        cands = [c for c in pl["candidates"] if c["id"] == args["candidate_id"]]
        if not cands:
            raise ToolError("candidate %s is not in the current plan (run seo_content_plan)" % scrub(args["candidate_id"], 40))
        with pipeline._RunLock(pipeline.paths(profile)["lock"]):
            r = pipeline.draft(profile, facts, chains, io, candidate=cands[0])
        pipeline._log(pipeline.paths(profile), dict(pipeline._slim(r), at=now()))
        return r

    @srv.tool("seo_content_review", "MANDATORY adversarial review of a saved draft by a model from a different family than "
              "the writer (refused if same family; the answering model is verified, silent fallbacks are refused; no "
              "verdict = hold). Cited sources are re-fetched and every quote re-found; reviewer edits re-run every check. "
              "Approve -> an approved Markdown bundle (page.md, IMPLEMENT.md for your AI agent, review.json, hashes).",
              {"type": "object", "properties": dict(site, draft_id={"type": "string"}, html={"type": "boolean"}),
               "required": ["draft_id"], "additionalProperties": False})
    def review_tool(args):
        profile, facts, chains, io = setup(args)
        with pipeline._RunLock(pipeline.paths(profile)["lock"]):
            r = pipeline.review_draft(profile, facts, chains, io, draft_id=args["draft_id"], html=bool(args.get("html")))
        pipeline._log(pipeline.paths(profile), dict(pipeline._slim(r), at=now()))
        return r

    @srv.tool("seo_content_bundle", "List approved content bundles, or read one (IMPLEMENT.md, page.md, review summary) and "
              "verify its hashes. Read-only.",
              {"type": "object", "properties": dict(site, bundle_id={"type": "string"}), "required": [],
               "additionalProperties": False})
    def bundle_tool(args):
        path = _profile_path(cfg, args.get("site"))
        profile = facts_mod.load_profile(path)
        if not args.get("bundle_id"):
            return {"bundles": list_bundles(profile)}
        b = _bundle_path(profile, args["bundle_id"])
        ok, errs = bundle_mod.verify(b)
        out = {"bundle_id": args["bundle_id"], "path": b, "verified": ok, "errors": errs[:20]}
        for name, key, cap in (("IMPLEMENT.md", "implement_md", 30000), ("page.md", "page_md", 80000)):
            with open(os.path.join(b, name), encoding="utf-8") as f:
                out[key] = f.read(cap)
        with open(os.path.join(b, "review.json"), encoding="utf-8") as f:
            rv = json.load(f)
        out["review"] = {k: rv.get(k) for k in ("verdict", "reviewer", "editor", "findings", "edited")}
        return out

    @srv.tool("seo_content_publish", "Publish one approved bundle through the guarded publisher (static directory or a git "
              "commit on a new branch; never pushes). action=request asks a human to approve it (signed approvals queue), "
              "dry_run shows what would be written, publish writes it. Refuses on: kill switch, HOLD, failed integrity, "
              "no approval (human mode), caps/window (auto mode), an existing page for a new-page bundle. Every attempt "
              "goes to the audit chain.",
              {"type": "object", "properties": dict(site, bundle_id={"type": "string"},
                                                    action={"type": "string", "enum": ["request", "dry_run", "publish"]}),
               "required": ["bundle_id", "action"], "additionalProperties": False})
    def publish_tool(args):
        path = _profile_path(cfg, args.get("site"))
        profile = facts_mod.load_profile(path)
        b = _bundle_path(profile, args["bundle_id"])
        state = pipeline.paths(profile)["state"]
        q = _queue(cfg, ctx)
        if args["action"] == "request":
            publish_mod.check_bundle(b)
            if q is None:
                raise ToolError("approvals are not configured: set ADRAILS_APPROVAL_SECRET (32+ chars) so a human can "
                                "approve publishes")
            r = q.request(publish_mod.plan_id_for(b), profile["host"], "publish content bundle %s" % args["bundle_id"],
                          risk="medium")
            return {"plan_id": r["plan_id"], "request_id": r["request_id"], "status": r["status"],
                    "note": "a human approves with adsops_approval_decide and the approve token; then call action=publish"}
        out = publish_mod.publish(b, profile, state_dir=state, chain=_chain(cfg), queue=q, now=now,
                                  actor=os.environ.get("ADRAILS_PRINCIPAL") or "agent", fetch=fetch,
                                  dry_run=args["action"] == "dry_run")
        if out.get("ok") and not out.get("dry_run"):
            out["optional"] = STAMP_HINT
        return out

    @srv.tool("seo_content_results", "Did the content work? For every piece published through AdRails: Search Console "
              "impressions, clicks and position per day before vs 14/28/56 days after, and a scoreboard by content type "
              "and query class. update_lessons=true also refreshes the Learned section of LESSONS.md (visible lines with "
              "evidence; deleted lines never come back; Operator notes never touched).",
              {"type": "object", "properties": dict(site, update_lessons={"type": "boolean"}), "required": [],
               "additionalProperties": False})
    def results_tool(args):
        path = _profile_path(cfg, args.get("site"))
        profile = facts_mod.load_profile(path)
        io = make_io(cfg, profile, gsc_client, fetch, http, run_cmd, now)
        out = pipeline.results(profile, io, update_lessons=bool(args.get("update_lessons")))
        if out.get("pieces"):
            out["optional"] = PANEL_HINT
        return out

    @srv.tool("seo_content_lessons", "Read the site's LESSONS.md: the evidence-backed lines the planner and writer use "
              "(and the operator's own notes). Read-only; edit the file by hand to change it.",
              {"type": "object", "properties": dict(site), "required": [], "additionalProperties": False})
    def lessons_tool(args):
        profile = facts_mod.load_profile(_profile_path(cfg, args.get("site")))
        if not os.path.exists(profile["_lessons_path"]):
            return {"path": profile["_lessons_path"], "exists": False, "text": "", "weights": {}}
        r = learn_mod.read_lessons(profile["_lessons_path"])
        return {"path": profile["_lessons_path"], "exists": True, "text": r["text"], "weights": r["weights"]}


# ── CLI: one shot for cron / systemd timers ─────────────────────────────────────────────────────────────────────
def main(argv=None, cfg=None):
    """adsops-mcp seo-run --profile PATH [--stage run|plan|results|publish|facts] [--bundle ID] [--candidate ID]."""
    import argparse
    ap = argparse.ArgumentParser(prog="adsops-mcp seo-run")
    ap.add_argument("--profile", required=True, help="site profile JSON (see examples/seo-server/)")
    ap.add_argument("--config", help="AdRails config (Search Console credential, approvals, audit chain)")
    ap.add_argument("--stage", choices=["run", "plan", "results", "publish", "facts"], default="run")
    ap.add_argument("--candidate")
    ap.add_argument("--bundle", help="publish: bundle id (default: every approved, unpublished bundle, oldest first)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--html", action="store_true", help="also render page.html into the bundle")
    ap.add_argument("--update-lessons", action="store_true")
    a = ap.parse_args(argv)
    if cfg is None and a.config:
        from . import config as config_mod
        cfg = config_mod.load_config(a.config)
    cfg = cfg or {"audit_log": os.path.join(os.path.dirname(os.path.abspath(a.profile)), "state", "audit.jsonl")}
    try:
        if a.stage == "run":
            profile = facts_mod.load_profile(a.profile)
            out = pipeline.run_once(a.profile, make_io(cfg, profile), candidate_id=a.candidate, html=a.html,
                                    audit_run=_audit_run(cfg, profile["host"]))
        elif a.stage == "plan":
            profile, facts, chains = pipeline.load(a.profile)
            out = pipeline.plan(profile, make_io(cfg, profile), audit_run=_audit_run(cfg, profile["host"]))
        elif a.stage == "results":
            profile = facts_mod.load_profile(a.profile)
            out = pipeline.results(profile, make_io(cfg, profile), update_lessons=a.update_lessons)
        elif a.stage == "facts":
            profile = facts_mod.load_profile(a.profile)
            io = make_io(cfg, profile)
            known, corpus, complete, errs = pipeline.site_pages(io, profile, time.monotonic() + 900)
            dest = os.path.join(profile["_dir"], "FACTS.proposed.json")
            facts_mod.write_proposal(dest, facts_mod.propose(corpus))
            out = {"written": dest, "pages_read": len(corpus), "complete": complete}
        else:
            profile = facts_mod.load_profile(a.profile)
            p = pipeline.paths(profile)
            pipeline._guard(p)
            done = {r.get("plan_id") for r in publish_mod.published(publish_mod.state_files(p["state"])["log"])}
            ids = [a.bundle] if a.bundle else [b["bundle_id"] for b in reversed(list_bundles(profile))]
            out = {"published": [], "refused": []}
            q = _queue(cfg, None) if (profile.get("publish") or {}).get("mode", "human") == "human" else None
            for bid in ids:
                b = _bundle_path(profile, bid)
                if publish_mod.plan_id_for(b) in done:
                    continue
                try:
                    out["published"].append(publish_mod.publish(b, profile, state_dir=p["state"], chain=_chain(cfg),
                                                                queue=q, dry_run=a.dry_run))
                except RailViolation as e:
                    out["refused"].append({"bundle_id": bid, "reason": scrub(str(e), 300)})
                    if "HOLD" in str(e) or "kill switch" in str(e) or "cap reached" in str(e):
                        break
    except RailViolation as e:
        print(json.dumps({"ok": False, "refused": scrub(str(e), 500)}))
        return 3
    except ToolError as e:
        print(json.dumps({"ok": False, "error": scrub(str(e), 500)}))
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


def selftest() -> int:
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "content_tools.selftest: " + msg
        n += 1

    try:
        _profile_path({}, None)
        ck(False, "no profiles must raise")
    except ToolError as e:
        ck("no content sites configured" in str(e), "no profiles configured")
    cfg = {"seo_content": {"profiles": {"a": "/tmp/a.json", "b": "/tmp/b.json"}}}
    ck(_profile_path(cfg, "a") == "/tmp/a.json", "named profile")
    for bad in (None, "c"):
        try:
            _profile_path(cfg, bad)
            ck(False, "ambiguous/unknown must raise")
        except ToolError:
            ck(True, "ambiguous or unknown site refused")
    ck(_profile_path({"seo_content": {"profiles": {"only": "/x.json"}}}, None) == "/x.json", "single default")
    for bad in ("../x", "/etc", ".hidden", ""):
        try:
            _bundle_path({"_dir": "/tmp", "site": "https://e.com/"}, bad)
            ck(False, "bad bundle id must raise")
        except ToolError:
            ck(True, "bundle id %r refused" % bad)
    ck(len(TOOLS) == 8 and "seo_content_review" in TOOLS, "tool list")
    return n
