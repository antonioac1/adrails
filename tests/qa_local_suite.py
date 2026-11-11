"""Integration QA for the local-business + Google LSA suite through the REAL server build (registration, prompts, the harden()
wrapper: RBAC / audit chain), with the REAL GoogleAdsClient over a fake HTTP layer so URLs, GAQL and request bodies are exercised.
Scenario: a new plumbing business with one LSA account. Run: PYTHONPATH=. python3 tests/qa_local_suite.py     the maintainers, 2026-11-06."""
import json, os, sys, tempfile

from adsops import auditchain, auth, config, tools
from adsops.platforms import google_ads

N = [0, 0]
NOW = 1793995200.0          # 2026-11-06 00:00 UTC-ish fixed clock


# Selectable fields from the live GoogleAdsFieldService (v24, 2026-11-06). See section 6 of main().
LIVE_V24_LSA_FIELDS = """
local_services_lead.category_id local_services_lead.contact_details local_services_lead.creation_date_time
local_services_lead.credit_details.credit_state local_services_lead.credit_details.credit_state_last_update_date_time
local_services_lead.id local_services_lead.lead_charged local_services_lead.lead_feedback_submitted local_services_lead.lead_status
local_services_lead.lead_type local_services_lead.locale local_services_lead.note.description local_services_lead.note.edit_date_time
local_services_lead.resource_name local_services_lead.service_id
local_services_lead_conversation.conversation_channel local_services_lead_conversation.event_date_time
local_services_lead_conversation.id local_services_lead_conversation.lead local_services_lead_conversation.message_details.attachment_urls
local_services_lead_conversation.message_details.text local_services_lead_conversation.participant_type
local_services_lead_conversation.phone_call_details.call_duration_millis local_services_lead_conversation.phone_call_details.call_recording_url
local_services_lead_conversation.resource_name
local_services_verification_artifact.artifact_type local_services_verification_artifact.creation_date_time
local_services_verification_artifact.id local_services_verification_artifact.status
local_services_verification_artifact.insurance_verification_artifact.expiration_date_time
local_services_verification_artifact.insurance_verification_artifact.rejection_reason
local_services_verification_artifact.license_verification_artifact.expiration_date_time
local_services_verification_artifact.license_verification_artifact.rejection_reason
local_services_verification_artifact.business_registration_check_verification_artifact.rejection_reason
local_services_verification_artifact.background_check_verification_artifact.final_adjudication_date_time
"""

def check(cond, what):
    N[0] += 1
    if not cond:
        N[1] += 1
        print("FAIL", what)


CID = "1112223333"
RN = lambda i: "customers/%s/localServicesLeads/%s" % (CID, i)


def lead(i, typ, status, created, phone, name, charged=True, fb=False):
    return {"localServicesLead": {"resourceName": RN(i), "id": str(i), "leadType": typ, "leadStatus": status,
                                  "creationDateTime": created, "leadCharged": charged, "leadFeedbackSubmitted": fb,
                                  "categoryId": "xcat:service_area_business_plumber",
                                  "contactDetails": {"phoneNumber": phone, "consumerName": name}}}


def conv(i, at, who, channel="MESSAGE", text=None, ms=None):
    c = {"lead": RN(i), "eventDateTime": at, "conversationChannel": channel, "participantType": who}
    if text:
        c["messageDetails"] = {"text": text}
    if ms is not None:
        c["phoneCallDetails"] = {"callDurationMillis": str(ms)}
    return {"localServicesLeadConversation": c}


LEADS = [lead(1, "PHONE_CALL", "ACTIVE", "2026-11-04 09:05:00", "+18135550101", "Maria Lopez"),
         lead(2, "MESSAGE", "NEW", "2026-11-04 13:30:00", "+18135550102", "Dan Price"),
         lead(3, "MESSAGE", "ACTIVE", "2026-11-05 10:00:00", "+18135550103", "SEO Pros"),
         lead(4, "PHONE_CALL", "BOOKED", "2026-11-05 16:45:00", "+18135550104", "Kim Ng"),
         lead(5, "PHONE_CALL", "ACTIVE", "2026-11-05 22:10:00", "+18135550105", "Unknown")]
CONVS = [conv(1, "2026-11-04 09:05:00", "CONSUMER", "PHONE_CALL", ms=245000),
         conv(2, "2026-11-04 13:30:00", "CONSUMER", text="Water heater leaking in garage, can someone come today?"),
         conv(3, "2026-11-05 10:00:00", "CONSUMER", text="We offer SEO and marketing services to rank your Google listing #1"),
         conv(4, "2026-11-05 16:45:00", "CONSUMER", "PHONE_CALL", ms=410000),
         conv(5, "2026-11-05 22:10:00", "CONSUMER", "PHONE_CALL", ms=4000)]
ARTS = [{"localServicesVerificationArtifact": {"id": "9", "artifactType": "LICENSE", "status": "PASSED",
                                               "licenseVerificationArtifact": {"expirationDateTime": "2026-11-20 00:00:00"}}},
        {"localServicesVerificationArtifact": {"id": "8", "artifactType": "INSURANCE", "status": "PENDING"}}]
CAMP = [{"campaign": {"id": "77", "name": "LSA", "status": "ENABLED", "biddingStrategyType": "MAXIMIZE_CONVERSIONS"},
         "campaignBudget": {"amountMicros": "50000000"}, "metrics": {"costMicros": "210000000", "impressions": "900",
                                                                   "phoneCalls": "3"}, "segments": {"date": "2026-11-05"}}]


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        if url.endswith("googleAds:search"):
            q = body["query"]
            if "FROM local_services_lead_conversation" in q:
                return {"results": [c for c in CONVS if "'%s'" % c["localServicesLeadConversation"]["lead"] in q]}
            if "FROM local_services_lead " in q or q.rstrip().endswith("FROM local_services_lead"):
                return {"results": LEADS}
            if "FROM local_services_verification_artifact" in q:
                return {"results": ARTS}
            if "FROM campaign" in q:
                return {"results": CAMP}
            return {"results": []}
        if url.endswith(":provideLeadFeedback"):
            return {"creditIssuanceDecision": "SUCCESS_NOT_REACHED_THRESHOLD"}
        raise AssertionError("unexpected call " + url)


def build(td, http, rbac=None):
    raw = {"tenant": "qa", "meter_log": os.path.join(td, "meter.jsonl"), "audit_log": os.path.join(td, "audit.jsonl"),
           "audit_chain": os.path.join(td, "audit-chain.jsonl"), "local": {"business": "Example Plumbing"},
           "accounts": [{"key": "lsa", "platform": "google_ads", "customer_id": CID, "campaigns": []}]}
    if rbac:
        raw["rbac"] = rbac
    p = os.path.join(td, "config.json")
    json.dump(raw, open(p, "w"))
    cfg = config.load_config(p)
    cf = lambda acct: google_ads.GoogleAdsClient(CID, auth.StaticToken("tok"), http=http)
    return tools.build_server(cfg, client_factory=cf, now=lambda: NOW)


def call(srv, tool, **args):
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})["result"]
    return (not r.get("isError")), r.get("structuredContent"), "".join(c.get("text", "") for c in r.get("content", []))


def main():
    td = tempfile.mkdtemp(prefix="qa-local-")
    http = FakeHTTP()
    srv = build(td, http)
    names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    for t in ("adsops_local_profile", "adsops_local_plan", "adsops_leads", "adsops_leads_due", "adsops_local_schema", "adsops_local_nap",
              "adsops_local_area_page", "adsops_reviews", "adsops_lsa_leads", "adsops_lsa_report", "adsops_lsa_dispute",
              "adsops_lsa_readiness"):
        check(t in names, "registered " + t)
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
    check("prompts" in init["capabilities"], "prompts capability advertised")
    pl = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})["result"]["prompts"]
    check({p["name"] for p in pl} == {"start", "morning", "weekly", "monthly"}, "four prompts")
    g = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {"name": "start"}})["result"]
    check("adsops_local_profile" in g["messages"][0]["content"]["text"], "start prompt drives the profile tool")

    # 1. profile -> plan
    ok, r, t = call(srv, "adsops_local_profile", action="questions")
    check(ok and len(r["questions"]) >= 15, "profile questions")
    prof = {"business_name": "Example Plumbing", "trade": "plumber", "city": "Tampa", "service_area": "Tampa, Brandon, 33602",
            "services": "water heaters, drain cleaning, leak repair", "phone": "(813) 555-0100", "website": "https://example-plumbing.example",
            "has_gbp": "y", "gbp_verified": "n", "review_count": 3, "rating": 5.0, "has_lsa": "y", "lsa_eligible_category": "y",
            "licensed_insured": "y", "monthly_budget": 1500, "avg_job_value": 650, "close_rate": 0.4, "hours": "Mon-Fri 7-6",
            "goals": "20 leads/month", "competitors": "Roto-Rooter, Mr. Rooter", "crm_today": "none", "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4"}
    ok, r, t = call(srv, "adsops_local_profile", action="set", fields=prof)
    check(ok and r["validation"]["ok"] and r["economics"]["max_cpl"] == 78.0, "profile saved + max_cpl 650*0.4*0.3=78: " + t[:200])
    check(os.stat(r["file"]).st_mode & 0o777 == 0o600, "profile file private")
    ok, plan, t = call(srv, "adsops_local_plan", action="build", start_date="2026-11-06")
    check(ok and len(plan["tasks"]) >= 12, "plan built: " + t[:200])
    tool_names = set(names) | {"owner", "agent"}
    check(ok and all(x["tool"] in tool_names for x in plan["tasks"]), "every plan task names a REAL registered tool (or owner/agent): "
          + str(sorted({x["tool"] for x in plan.get("tasks", []) if x["tool"] not in tool_names})))
    check(ok and all(x["tool"] in tool_names for x in plan["weekly_rhythm"]), "weekly rhythm tools are real")
    ok, _, t = call(srv, "adsops_local_plan", action="build")
    check(not ok and "rebuild=true" in t, "plan never silently replaced")
    first = plan["tasks"][0]["id"]
    ok, pr, _ = call(srv, "adsops_local_plan", action="progress", done=[first])
    check(ok and pr["done"] == 1 and pr["next"], "progress marks + next")
    ok, _, t = call(srv, "adsops_local_plan", action="progress", done=["T999"])
    check(not ok and "unknown task" in t, "bad task id refused")

    check(plan["tasks"][0]["title"].startswith("Claim and verify"), "gbp_verified='n' (owner words) -> claim task first")
    from adsops.local import plan as P
    t2 = P.build(dict(prof, monthly_budget=0, lsa_eligible_category="no"), start_date="2026-11-06")["tasks"]
    check(not any(x["tool"] in ("adsops_plan", "adsops_lsa_readiness") for x in t2), "'no' LSA + $0 budget -> organic-only plan")
    # 2. LSA: pull + sync, flags, report, dispute plan/commit, readiness
    ok, r, t = call(srv, "adsops_lsa_leads", days=7)
    check(ok and r["count"] == 5 and r["synced"]["created"] == 5, "LSA leads pulled + synced: " + t[:300])
    by = {x["id"]: x for x in (r or {}).get("leads", [])}
    check(any(f["reason_enum"] == "SOLICITATION" for f in by.get("3", {}).get("flags", [])), "SEO pitch flagged SOLICITATION")
    check(not by.get("4", {}).get("flags"), "booked lead never flagged")
    check(by.get("1", {}).get("answered") is True and by.get("1", {}).get("first_response_minutes") == 0, "live-answered call")
    q = [c for c in http.calls if c[1].endswith("googleAds:search") and "local_services_lead_conversation" in c[2]["query"]]
    check(q and all(RN(i) in q[0][2]["query"] for i in range(1, 6)), "conversations fetched by lead filter")
    ok, r2, _ = call(srv, "adsops_lsa_leads", days=7)
    check(ok and r2["synced"]["created"] == 0, "re-sync dedupes by LSA lead id")
    ok, bk, _ = call(srv, "adsops_leads", action="find", source="google_lsa")
    check(ok and bk["total"] == 5, "lead book holds 5 LSA leads")
    stages = {l["name"]: l["stage"] for l in bk["leads"]}
    check(stages.get("Kim Ng") == "booked", "BOOKED on LSA -> booked in the book")
    maria = [l for l in bk["leads"] if l["name"] == "Maria Lopez"]
    check(maria and maria[0]["created_at"].startswith("2026-11-04") and maria[0]["service"] == "plumber",
          "synced lead keeps LSA arrival date + readable service: %r" % ((maria[0]["created_at"], maria[0]["service"]),) if maria else "no maria")

    ok, rep, t = call(srv, "adsops_lsa_report", days=7)
    check(ok and rep["spend"] == 210.0 and rep["max_cpl"] == 78.0 and rep["cost_per_lead"] == 42.0, "report spend/cpl: " + t[:300])
    check(ok and isinstance(rep.get("advice"), list), "report advice")

    ok, plan_d, t = call(srv, "adsops_lsa_dispute", days=7)
    sub = {s["lead_id"]: s for s in (plan_d or {}).get("submit", [])}
    check(ok and plan_d["mode"] == "plan" and "3" in sub and "4" in sub, "dispute suggestions: " + t[:300])
    check(sub.get("3", {}).get("body", {}).get("surveyDissatisfied", {}).get("surveyDissatisfiedReason") == "SOLICITATION", "SEO -> SOLICITATION body")
    check(sub.get("4", {}).get("body", {}).get("surveySatisfied", {}).get("surveySatisfiedReason") == "BOOKED_CUSTOMER", "booked -> satisfied")
    check(not [c for c in http.calls if "provideLeadFeedback" in c[1]], "plan mode posts nothing")
    decisions = [{"lead_id": "3", "answer": "VERY_DISSATISFIED", "reason": "SOLICITATION"}]
    ok, res, t = call(srv, "adsops_lsa_dispute", days=7, decisions=decisions, commit=True)
    posts = [c for c in http.calls if "provideLeadFeedback" in c[1]]
    check(ok and len(posts) == 1 and posts[0][1].endswith("/v24/customers/%s/localServicesLeads/3:provideLeadFeedback" % CID),
          "commit posts exactly one feedback to the documented URL: " + t[:200])
    check(posts and posts[0][2] == {"surveyAnswer": "VERY_DISSATISFIED", "surveyDissatisfied": {"surveyDissatisfiedReason": "SOLICITATION"}},
          "feedback body matches the API contract: %r" % (posts[0][2] if posts else None))
    check(ok and res["results"][0]["credit_decision"] == "SUCCESS_NOT_REACHED_THRESHOLD", "credit decision surfaced")
    ok, mm, t = call(srv, "adsops_lsa_dispute", days=7, decisions=[{"lead_id": "2", "answer": "DISSATISFIED", "reason": "BOOKED_CUSTOMER"}])
    check(ok and mm["submit"] == [] and mm["refused"] and mm["refused"][0]["lead_id"] == "2", "mismatched answer/reason refused in plan")

    ok, rd, t = call(srv, "adsops_lsa_readiness")
    lic = [c for c in (rd or {}).get("checks", []) if c["check"] == "license"]
    check(ok and lic and lic[0]["status"] == "warn", "license expiring in 14 days warns: " + t[:300])
    check(ok and "Google Verified" in rd["badge"], "badge text current")

    # 3. lead book: speed-to-lead, stages, conversions, export
    ok, a, _ = call(srv, "adsops_leads", action="add", lead={"name": "Walk In", "phone": "813.555.0199", "source": "website",
                                                              "gclid": "Cj0KCQ-test", "service": "drain"})
    lid = a["lead"]["id"]
    ok, d, _ = call(srv, "adsops_leads_due")
    check(ok and any(x["id"] == lid for x in d["today"] + d["new_uncontacted"] + d["overdue"]), "new web lead shows as due")
    ok, _, t = call(srv, "adsops_leads", action="move", id=lid, stage="won")
    check(not ok and "value" in t, "won without value refused")
    ok, w, _ = call(srv, "adsops_leads", action="move", id=lid, stage="won", value=780)
    check(ok and w["stage"] == "won", "won with value")
    ok, cv, _ = call(srv, "adsops_leads", action="conversions", since="2026-11-01", until="2026-11-30")
    check(ok and cv["conversions"] and cv["conversions"][0]["gclid"] == "Cj0KCQ-test", "won job -> offline conversion row")
    ok, ex, _ = call(srv, "adsops_leads", action="export")
    check(ok and "Walk In" in ex["csv"], "CSV export")
    ok, pp, _ = call(srv, "adsops_leads", action="pipeline")
    check(ok and pp["by_source"]["website"]["won"] == 1, "pipeline by source")

    # 4. presence + reviews
    ok, sc, t = call(srv, "adsops_local_schema")
    check(ok and sc["jsonld"]["@type"] == "Plumber" and "areaServed" in sc["jsonld"], "schema from profile: " + t[:200])
    ok, nap, t = call(srv, "adsops_local_nap", pages=[{"url": "https://yelp.example/lee", "text": "Example Plumbing (813) 555-0100 Tampa"},
                                                     {"url": "https://bbb.example/lee", "text": "Example Plumbing LLC 813-555-0999 Tampa"}])
    check(ok and nap["results"][1]["issues"], "NAP: wrong phone on BBB flagged: " + t[:200])
    ok, cit, _ = call(srv, "adsops_local_nap", action="citations")
    check(ok and any("Apple" in c["site"] for c in cit["citations"]), "citations include Apple Business Connect")
    ok, ap, _ = call(srv, "adsops_local_area_page", service="water heater repair", area="Brandon")
    check(ok and len(ap["title"]) <= 60 and len(ap["faq"]) >= 6, "area page brief")
    ok, lk, _ = call(srv, "adsops_reviews", action="link")
    check(ok and lk["link"].endswith("placeid=ChIJN1t_tDeuEmsRUsoyG83frY4"), "review link from profile place_id")
    ok, rq, t = call(srv, "adsops_reviews", action="request", customer_first_name="Kim", service="water heater", channel="sms")
    check(ok and rq["drafts"] and any("gating" in x.lower() or "every customer" in x.lower() for x in rq["rules"]), "request drafts + no-gating rule")
    ok, rp, _ = call(srv, "adsops_reviews", action="reply", review={"rating": 1, "text": "They flooded my kitchen, calling my attorney",
                                                                   "author_first_name": "Bob"})
    check(ok and "mentions_safety_or_legal" in rp["flags"], "legal review flagged for the owner")
    ok, vel, _ = call(srv, "adsops_reviews", action="velocity", reviews=[{"rating": 5, "created_at": "2026-10-01T00:00:00Z"}] * 3,
                      competitors=[{"name": "Roto-Rooter", "count": 210, "rating": 4.4}])
    check(ok and vel["count"] == 3 and vel["vs_competitors"], "review velocity vs competitors")

    # 4b. independent-review regressions (2026-11-06)
    ok, _, t = call(srv, "adsops_lsa_dispute", days=7, commit=True)
    check(not ok and "decisions" in t, "#2 commit without explicit decisions refused")
    ok, sg, _ = call(srv, "adsops_lsa_dispute", days=7)
    check(ok and "5" not in {x["lead_id"] for x in sg["submit"]}, "#3 low-confidence (4-second call) not suggested for dispute")
    from adsops.local import lsa as L
    dup = L.normalize_leads([lead(21, "MESSAGE", "NEW", "2026-11-05 10:00:00", "+18135550777", "New"),
                             lead(20, "MESSAGE", "NEW", "2026-11-02 10:00:00", "+18135550777", "Old")])
    fl = {f["lead_id"]: f["flags"] for f in L.flags_for_all(dup)}
    check("21" in fl and "20" not in fl, "#4 the repeat (newer) lead is flagged, not the original: %r" % sorted(fl))
    ok, man, _ = call(srv, "adsops_leads", action="add", lead={"name": "Danny P (my notes)", "phone": "813-555-0102", "source": "phone"})
    for _ in range(3):
        call(srv, "adsops_lsa_leads", days=7)
    ok, g2, _ = call(srv, "adsops_leads", action="find", text="0102")
    dans = [l for l in g2["leads"]]
    notes = [n["text"] for l in dans for n in l.get("notes", [])]
    check(len(dans) == 1 or all(n.count("also came in via google_lsa") <= 1 for n in [" ".join(notes)]), "#7 re-sync adds at most one cross-channel note")
    check(any(l["name"] == "Dan Price" or l["name"] == "Danny P (my notes)" for l in dans), "#7 lead present")
    ok, ex2, _ = call(srv, "adsops_leads", action="export")
    check(ok and "'+1" not in ex2["csv"] and "+18135550101" in ex2["csv"], "#11 phones exported as data, not escaped")
    ok, nap2, _ = call(srv, "adsops_local_nap", pages=[{"url": "https://dead.example", "text": ""},
                                                       {"url": "https://ok.example", "text": "Example Plumbing (813) 555-0100 Tampa"}])
    check(ok and nap2["unreadable"] and nap2["unreadable"][0]["url"] == "https://dead.example" and len(nap2["results"]) == 1,
          "#12 unreadable page reported, not scored as 'name not found'")
    ok, _, t = call(srv, "adsops_leads", action="conversions", since="2026-13-01", until="2026-13-31")
    check(not ok and "real dates" in t, "bad date -> clear error, not internal error")
    # #8: rebuild to a smaller plan keeps progress usable
    call(srv, "adsops_local_plan", action="progress", done=[plan["tasks"][-1]["id"]])
    call(srv, "adsops_local_profile", action="set", fields={"monthly_budget": 0, "lsa_eligible_category": "n", "has_lsa": "n"})
    ok, _, t = call(srv, "adsops_local_plan", action="build", rebuild=True)
    ok2, pr2, t2 = call(srv, "adsops_local_plan", action="progress", done=[])
    check(ok and ok2, "#8 progress works after a shrinking rebuild: " + t2[:150])
    call(srv, "adsops_local_profile", action="set", fields={"monthly_budget": 1500, "lsa_eligible_category": "y", "has_lsa": "y"})
    # #13: two server processes (two LeadBook writers) never lose writes
    import threading
    srvA, srvB = build(td, http), build(td, http)
    def add_many(s_, tag):
        for i in range(25):
            call(s_, "adsops_leads", action="add", lead={"name": "%s%d" % (tag, i), "phone": "727555%04d" % (i + (0 if tag == "A" else 100)), "source": "website"})
    ths = [threading.Thread(target=add_many, args=(srvA, "A")), threading.Thread(target=add_many, args=(srvB, "B"))]
    [x.start() for x in ths]; [x.join() for x in ths]
    ok, allw, _ = call(srv, "adsops_leads", action="find", source="website", limit=500)
    names_w = {l["name"] for l in allw["leads"]}
    check(all(("A%d" % i) in names_w and ("B%d" % i) in names_w for i in range(25)), "#13 concurrent writers: no lost leads (%d)" % len(names_w))

    # #1 PII never in the chain, even in free-text notes
    call(srv, "adsops_leads", action="note", id=lid, note="Husband Bob at 5pm, gate code 4411")
    call(srv, "adsops_leads", action="add", lead={"name": "Jane Customer", "service": "leak at 123 Oak St", "source": "phone"})

    # 5. audit chain + privacy + RBAC
    chain = os.path.join(td, "audit-chain.jsonl")
    body = open(chain).read() if os.path.isfile(chain) else ""
    check(os.path.isfile(chain) and auditchain.AuditChain(chain).verify()["ok"], "audit chain valid beside config")
    check("adsops_lsa_dispute" in body and "adsops_leads" in body, "mutations audited")
    body_text = re.sub(r"[0-9a-f]{16,}", "", body)     # hashes/hmacs are random hex and can contain "4411"
    check(not any(p in body_text for p in ("Maria Lopez", "8135550101", "Kim Ng", "gate code", "4411", "Jane Customer", "123 Oak", "Example Plumbing")),
          "#1 no customer PII or free text in the audit chain")
    check(not os.path.exists(os.path.expanduser("~/.adsops/leads.json")) or os.path.getmtime(os.path.expanduser("~/.adsops/leads.json")) < NOW,
          "lead book never written to ~/.adsops")
    td2 = tempfile.mkdtemp(prefix="qa-local-rbac-")
    os.environ["ADRAILS_PRINCIPAL"] = "v"
    srv2 = build(td2, FakeHTTP(), rbac={"enabled": True, "principals": {"v": ["viewer"]}})
    ok, _, t = call(srv2, "adsops_lsa_dispute", decisions=decisions, commit=True)
    check(not ok, "viewer cannot submit LSA feedback: " + t[:120])
    ok, _, _ = call(srv2, "adsops_lsa_report")
    check(ok, "viewer can read the LSA report")
    os.environ.pop("ADRAILS_PRINCIPAL", None)
    # 7. playbooks + update awareness (0.7.0) through the real server; update check disabled so tests never touch PyPI
    os.environ["ADRAILS_NO_UPDATE_CHECK"] = "1"
    try:
        init = srv.handle({"jsonrpc": "2.0", "id": 90, "method": "initialize", "params": {}})["result"]
        check("adsops_playbook" in init.get("instructions", "") and "never spend money" in init.get("instructions", ""),
              "initialize carries agent instructions (playbook pointer + rails)")
        ok, r, t = call(srv, "adsops_playbook")
        check(ok and {p["name"] for p in r["playbooks"]} >= {"aeo", "local-leads"}, "playbook list has aeo + local-leads")
        ok, r, t = call(srv, "adsops_playbook", name="aeo", section="Fast path")
        check(ok and r["text"].startswith("## Fast path") and "aeo_visibility_record" in r["text"], "playbook section read")
        ok, _, t = call(srv, "adsops_playbook", name="../../etc/passwd")
        check(not ok, "playbook name traversal refused")
        ok, r, t = call(srv, "adsops_update_check")
        check(ok and r["source"] == "disabled" and r["update_available"] is False, "update check honours ADRAILS_NO_UPDATE_CHECK")
        os.environ["ADRAILS_PRINCIPAL"] = "v"
        ok1, _, _ = call(srv2, "adsops_playbook", name="local-leads")
        ok2, _, _ = call(srv2, "adsops_update_check")
        check(ok1 and ok2, "viewer can read playbooks and the update check")
        os.environ.pop("ADRAILS_PRINCIPAL", None)
    finally:
        os.environ.pop("ADRAILS_NO_UPDATE_CHECK", None)
    # 8. results report, case study privacy, GBP checker, plan->playbook links (0.8.0), through the real server
    ok, r, t = call(srv, "adsops_results")
    check(ok and "markdown" in r and "| Metric |" in r["markdown"], "results report renders a before/after table: " + t[:120])
    ok, r, t = call(srv, "adsops_results", action="case_study", quote="It answers leads before I do", permission="anonymous")
    blob = json.dumps(r)
    check(ok and r["case_study"]["permission"] == "anonymous" and not any(x in blob for x in ("Maria Lopez", "8135550101", "Kim Ng",
          "Jane Customer", "Example Plumbing")), "case study carries no customer PII and hides the business name when anonymous")
    ok, _, t = call(srv, "adsops_results", action="case_study", permission="everyone")
    check(not ok, "case study refuses an unknown permission")
    ok, r, t = call(srv, "adsops_gbp", action="audit", snapshot={"verified": True, "primary_category": "Plumber"})
    check(ok and 0 < r["score"] < 50 and r["fixes"], "gbp audit scores a thin profile low with ranked fixes")
    ok, r, t = call(srv, "adsops_gbp", action="history")
    check(ok and r["history"] and "score" in r["history"][-1] and "Plumber" not in json.dumps(r), "gbp history keeps scores only")
    ok, r, t = call(srv, "adsops_gbp", action="description", text="Best plumber! www.x.com")
    check(ok and not r["ok"], "gbp description rules catch URL + superlative")
    ok, r, t = call(srv, "adsops_local_plan", action="markdown")
    check(ok and "playbook: " in r["markdown"], "plan markdown links tasks to playbook sections")
    chain_body = open(os.path.join(td, "audit-chain.jsonl")).read()
    check("It answers leads before I do" not in chain_body, "owner quote never lands in the audit chain")
    # 6. GAQL contract: every LSA field we SELECT must be selectable in Google Ads API v24. Snapshot taken from the live
    #    GoogleAdsFieldService on 2026-11-06; FakeHTTP accepts any field, which is how contact_details.phone_number shipped in
    #    0.6.0/0.6.1 and failed on the first live call (UNRECOGNIZED_FIELD). Refresh the snapshot when the API version moves.
    import re as _re
    from adsops.local import lsa as _lsa
    live = set(LIVE_V24_LSA_FIELDS.split())
    qs = [_lsa.render("leads", since="2026-01-01 00:00:00", limit=1), _lsa.render("artifacts"),
          list(_lsa.conversations_for(["customers/1/localServicesLeads/2"]))[0]]
    used = set()
    for q in qs:
        sel = q.split("FROM")[0].replace("SELECT", "")
        used |= {f.strip() for f in sel.split(",") if f.strip()}
    bad = sorted(f for f in used if f.startswith("local_services") and f not in live)
    check(not bad, "GAQL: every LSA field is selectable in live v24 (bad: %s)" % bad)
    print("QA: %d checks, %d failures" % (N[0], N[1]))
    return 1 if N[1] else 0


if __name__ == "__main__":
    sys.exit(main())
