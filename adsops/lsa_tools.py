"""Google Local Services Ads (LSA) tools (the maintainers glue, 2026-11-06).

  adsops_lsa_leads      pull LSA leads + their calls/messages, flag junk, and sync them into the lead book (read on Google's side)
  adsops_lsa_report     spend, cost per charged lead, credits, booking/answer rate, missed calls, best hours, plain-English advice
  adsops_lsa_dispute    rate leads through ProvideLeadFeedback: junk -> dissatisfied + reason (Google may credit), booked -> satisfied.
                        Plan by default; commit=true submits (operator role, audited). The only LSA write the API offers.
  adsops_lsa_readiness  verification (license, insurance, background check, registration) + profile signals Google ranks on

Contract: Google Ads API v24 protos (specs/ent/local_lsa.md). All four read queries ran against the live API on 2026-11-06 (that run
caught contact_details sub-fields that v24 rejects; qa_local_suite pins every field to the live list). An account with no LSA campaign
returns zero rows and the report says so. Budgets, bids, hours and pausing for LSA are not in the API: the LSA dashboard owns them,
and these tools never pretend otherwise.
"""
import datetime as dt

from .errors import ToolError
from .local import leadbook, lsa
from .local_tools import _locked


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _category(c):
    """'xcat:service_area_business_plumber' -> 'plumber' (Google's LSA category ids are not for humans)."""
    c = str(c or "")
    return c.split(":", 1)[-1].replace("service_area_business_", "").replace("_", " ").strip()


def _local_iso(ts):
    """LSA times are 'YYYY-MM-DD HH:MM:SS' in the ad account's timezone; the owner's machine is almost always in that timezone,
    so read it as local time and emit an ISO string with the offset (the lead book stores UTC)."""
    try:
        return dt.datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").astimezone().isoformat()
    except (TypeError, ValueError):
        return None


_STAGE = {"BOOKED": "booked", "DECLINED": "lost", "CONSUMER_DECLINED": "lost", "DISABLED": "spam"}


def register(srv, ctx, cfg):
    from .local_tools import _load, _paths
    P = _paths(cfg)

    def g_accounts():
        return [a for a in cfg.get("accounts", []) if a.get("platform") == "google_ads"]

    def account(key):
        accts = g_accounts()
        if key:
            for a in accts:
                if a.get("key") == key:
                    return a
            raise ToolError("unknown google_ads account key: %s" % key)
        if len(accts) == 1:
            return accts[0]
        raise ToolError("pass account (one of: %s)" % ", ".join(a["key"] for a in accts) if accts else "no google_ads account in config")

    def fetch(acct, days, limit=500):
        client = ctx.client_factory(acct)
        today = dt.date.fromtimestamp(ctx.now())
        since = (today - dt.timedelta(days=days)).isoformat()
        rows = client.search(lsa.render("leads", since=since + " 00:00:00", limit=int(limit)))
        names = [r.get("localServicesLead", {}).get("resourceName") for r in rows]
        names = [n for n in names if n]
        convs = []
        for q in lsa.conversations_for(names) if names else []:
            convs += client.search(q)
        return client, lsa.normalize_leads(rows, convs), since, today.isoformat()

    ACCT = {"account": {"type": "string", "description": "google_ads account key (optional when there is only one)"}}
    DAYS = {"days": {"type": "integer", "description": "look-back window, 1-90 (default 30)"}}

    def days_of(args, default=30):
        d = int(args.get("days") or default)
        if not 1 <= d <= 90:
            raise ToolError("days must be 1..90")
        return d

    @srv.tool("adsops_lsa_leads", "Google Local Services Ads: pull leads (calls, messages, bookings) with their conversation history, "
              "who answered and how fast, charge and credit status, and junk flags (spam, solicitation, duplicate, wrong area, wrong "
              "job). sync=true (default) adds them to the lead book so every lead lives in one place. Read-only on Google's side.",
              _schema({**ACCT, **DAYS, "sync": {"type": "boolean"}, "include_contact": {"type": "boolean"}}))
    def lsa_leads(args):
        acct = account(args.get("account"))
        _, leads, since, until = fetch(acct, days_of(args))
        prof = _load(P["profile"], {})
        flags = {f["lead_id"]: f["flags"] for f in lsa.flags_for_all(leads, prof)}
        synced = {"created": 0, "updated": 0}
        if args.get("sync", True):
            with _locked(P["book"]):      # one writer at a time (review #13)
                book = leadbook.LeadBook(P["book"], now=ctx.now)
                for l in leads:
                    if l.get("wiped"):
                        continue
                    r = book.upsert({"source": "google_lsa", "source_id": l["id"], "name": l.get("name") or "", "phone": l.get("phone") or "",
                                     "email": l.get("email") or "", "service": _category(l.get("category")),
                                     "created_at": _local_iso(l.get("created_at")),   # when the lead arrived, not when we synced
                                     "tags": ["lsa", (l.get("type") or "").lower()]})
                    synced["created" if r.get("created") else "updated"] += 1
                    stage = _STAGE.get(l.get("status"))
                    cur = r["lead"]
                    if stage and cur.get("source") == "google_lsa" and cur.get("stage") == "new":   # untouched LSA leads only: the owner's stage wins
                        try:
                            book.move(cur["id"], stage, value=0 if stage == "won" else None,
                                      lost_reason="declined on LSA" if stage == "lost" else None)
                        except ToolError:
                            pass
        out = []
        for l in leads:
            item = {k: l.get(k) for k in ("id", "type", "status", "created_at", "charged", "credit_state", "feedback_submitted",
                                          "first_response_minutes", "answered", "call_seconds")}
            item["service"] = _category(l.get("category"))
            if args.get("include_contact", True):
                item.update({k: l.get(k) for k in ("name", "phone", "email")})
            item["flags"] = flags.get(l["id"], [])
            item["last_message"] = next((c.get("text") for c in reversed(l.get("conversations") or []) if c.get("text")), None)
            out.append(item)
        return {"account": acct["key"], "since": since, "until": until, "count": len(out), "leads": out, "synced": synced,
                "waiting": [i["id"] for i in out if i["status"] == "NEW"],
                "note": "LSA leads are read through the Google Ads API; replying happens in the LSA app/phone (the API has no "
                        "consumer messaging)."}

    @srv.tool("adsops_lsa_report", "Google Local Services Ads report: spend, cost per charged lead vs the max the business can afford "
              "(from the profile), credits pending/issued, lead mix, booking rate, answer rate and missed calls (missed calls hurt LSA "
              "ranking), first-response time, leads by hour and weekday, and plain-English advice. Read-only.",
              _schema({**ACCT, **DAYS}))
    def lsa_report(args):
        acct = account(args.get("account"))
        client, leads, since, until = fetch(acct, days_of(args))
        try:
            camps = client.search(lsa.render("campaigns", since=since, until=until))
        except ToolError as e:                       # metrics on LSA campaigns are UNVERIFIED live: degrade, don't die
            camps, note = [], "campaign metrics unavailable from the API (%s); lead-only report" % str(e)[:160]
        else:
            note = None if camps else "no LOCAL_SERVICES campaign metrics returned; lead-only report"
        prof = _load(P["profile"], {})
        from .local import plan as lplan
        max_cpl = (lplan.economics(prof) or {}).get("max_cpl") if prof else None
        r = lsa.report(leads, camps, since, until, max_cpl=max_cpl)
        return {**r, "account": acct["key"], "max_cpl": max_cpl, **({"note": note} if note else {})}

    @srv.tool("adsops_lsa_dispute", "Google Local Services Ads lead feedback (how you get credit for junk leads and teach Google which "
              "leads you want): with no decisions it SUGGESTS ratings from the junk flags (spam, solicitation, duplicate, wrong area, "
              "wrong job -> dissatisfied; booked -> very satisfied). Pass decisions [{lead_id, answer, reason, comment}] to plan them; "
              "commit=true submits via ProvideLeadFeedback (once per lead, inside the window). Returns Google's credit decision.",
              _schema({**ACCT, **DAYS, "decisions": {"type": "array", "items": {"type": "object"}}, "commit": {"type": "boolean"}}))
    def lsa_dispute(args):
        acct = account(args.get("account"))
        client, leads, _, today = fetch(acct, days_of(args))
        if args.get("commit") and not args.get("decisions"):
            # feedback is once-per-lead and irreversible: commit only what a human saw (review #2)
            raise ToolError("commit=true needs the explicit decisions the owner approved (run without commit first, show the owner, "
                            "then pass those decisions back)")
        decisions = args.get("decisions") or lsa.suggest(leads, _load(P["profile"], {}))
        plan = lsa.feedback_plan(leads, decisions, today=today)
        if not args.get("commit"):
            return {"mode": "plan", "submit": plan["submit"], "refused": plan["refused"],
                    "next": "show the owner, then call again with these decisions and commit=true" if plan["submit"] else None}
        cid = str(acct["customer_id"]).replace("-", "")
        results = []
        for item in plan["submit"]:
            path = "customers/%s/localServicesLeads/%s:provideLeadFeedback" % (cid, item["lead_id"])
            try:
                r = client._post(path, item["body"])
                results.append({"lead_id": item["lead_id"], "ok": True, "credit_decision": r.get("creditIssuanceDecision")})
            except ToolError as e:                      # one rejected lead must not hide the others' outcomes
                results.append({"lead_id": item["lead_id"], "ok": False, "error": str(e)[:300]})
        return {"mode": "commit", "results": results, "refused": plan["refused"]}

    @srv.tool("adsops_lsa_readiness", "Google Local Services Ads readiness: verification status for license, insurance, background "
              "check and business registration (with rejection reasons and expiry warnings) plus the profile signals Google ranks on "
              "(reviews, rating, response time, photos, hours, messaging, bio). Read-only; the fixes are listed in plain English.",
              _schema({**ACCT}))
    def lsa_readiness(args):
        acct = account(args.get("account"))
        client = ctx.client_factory(acct)
        arts = client.search(lsa.render("artifacts"))
        prof = dict(_load(P["profile"], {}))
        try:
            _, leads, _, _ = fetch(acct, 30)
            mins = sorted(l["first_response_minutes"] for l in leads if l.get("first_response_minutes") is not None)
            if mins:
                prof["median_first_response_minutes"] = mins[len(mins) // 2]
        except ToolError:
            pass
        return lsa.readiness(arts, profile=prof, today=dt.date.fromtimestamp(ctx.now()).isoformat())
