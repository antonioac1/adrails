import unittest
import datetime
from datetime import date, datetime, timedelta

import adsops.local.lsa as lsa


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def lead_row(
    id="1",
    type="MESSAGE",
    status="NEW",
    phone="+15550001",
    email="alice@example.com",
    name="Alice",
    created="2026-11-01 09:10:00",
    charged=True,
    credit_state=None,
    feedback=False,
    note=None,
    category=1,
    service=2,
    wiped=False,
):
    """Build one REST-shaped local_services_lead row."""
    contact = None
    if not wiped:
        contact = {}
        if phone is not None:
            contact["phoneNumber"] = phone
        if email is not None:
            contact["email"] = email
        if name is not None:
            contact["consumerName"] = name

    row = {
        "localServicesLead": {
            "id": str(id),
            "leadType": type,
            "leadStatus": status,
            "creationDateTime": created,
            "leadCharged": charged,
            "leadFeedbackSubmitted": feedback,
            "categoryId": str(category),
            "serviceId": str(service),
            "contactDetails": contact,
        }
    }
    if credit_state is not None:
        row["localServicesLead"]["creditDetails"] = {"creditState": credit_state}
    if note is not None:
        row["localServicesLead"]["note"] = {"description": note}
    return row


def conv_row(
    lead_resource,
    at,
    channel="MESSAGE",
    participant="CONSUMER",
    text=None,
    duration_ms=None,
):
    """Build one REST-shaped local_services_lead_conversation row."""
    data = {
        "lead": lead_resource,
        "eventDateTime": at,
        "conversationChannel": channel,
        "participantType": participant,
    }
    if text is not None:
        data["messageDetails"] = {"text": text}
    if duration_ms is not None:
        data["phoneCallDetails"] = {"callDurationMillis": duration_ms}
    return {"localServicesLeadConversation": data}


def campaign_row(
    id="1",
    name="LSA Campaign",
    status="ENABLED",
    bidding="MANUAL_CPA",
    budget_micros="50000000",
    cost_micros="12345678",
    impressions="100",
    clicks="10",
    phone_calls="5",
    conversions="2",
    day="2026-11-01",
):
    return {
        "campaign": {
            "id": str(id),
            "name": name,
            "status": status,
            "advertisingChannelType": "LOCAL_SERVICES",
            "biddingStrategyType": bidding,
        },
        "campaignBudget": {"amountMicros": str(budget_micros)},
        "metrics": {
            "costMicros": str(cost_micros),
            "impressions": str(impressions),
            "clicks": str(clicks),
            "phoneCalls": str(phone_calls),
            "allConversions": str(conversions),
        },
        "segments": {"date": day},
    }


def artifact_row(
    id="1",
    artifact_type="LICENSE",
    status="PASSED",
    created="2026-11-01 09:00:00",
    expiration=None,
    rejection_reason=None,
):
    data = {
        "id": str(id),
        "artifactType": artifact_type,
        "status": status,
        "creationDateTime": created,
    }
    if artifact_type == "LICENSE":
        data["licenseVerificationArtifact"] = {
            "expirationDateTime": expiration,
            "rejectionReason": rejection_reason,
        }
    elif artifact_type == "INSURANCE":
        data["insuranceVerificationArtifact"] = {
            "expirationDateTime": expiration,
            "rejectionReason": rejection_reason,
        }
    return {"localServicesVerificationArtifact": data}


def _find_dicts(obj, predicate):
    if isinstance(obj, dict):
        if predicate(obj):
            yield obj
        for value in obj.values():
            yield from _find_dicts(value, predicate)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _find_dicts(value, predicate)



def _feedback_plan(*args, **kwargs):
    """feedback_plan pinned to a day inside the 30-day window of the 2026-11-01 fixtures, so the tests do not
    depend on the wall clock (they failed from 2026-12-02 on). A test may still pass its own today=."""
    kwargs.setdefault("today", "2026-11-05")
    return lsa.feedback_plan(*args, **kwargs)

class LsaTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # query construction
    # ------------------------------------------------------------------

    def test_queries_keys_and_render_leads(self):
        for key in ("leads", "conversations_for", "artifacts", "campaigns"):
            self.assertIn(key, lsa.QUERIES)

        query = lsa.render("leads", since="2026-11-01", limit=25)
        self.assertIn("local_services_lead.creation_date_time", query)
        self.assertIn("2026-11-01", query)
        self.assertIn("LIMIT 25", query)

    def _call_conversations_for(self, names):
        if hasattr(lsa, "conversations_for"):
            return lsa.conversations_for(names)

        value = lsa.QUERIES.get("conversations_for")
        if callable(value):
            return value(names)

        last_error = None
        for kw in ("names", "lead_resource_names", "lead_names", "resources"):
            try:
                return lsa.render("conversations_for", **{kw: names})
            except TypeError as exc:
                last_error = exc
        raise AssertionError(f"could not call conversations_for: {last_error}")

    def assertToolError(self, func, *args, **kwargs):
        tool_error = getattr(lsa, "ToolError", None)
        if tool_error is not None:
            with self.assertRaises(tool_error):
                func(*args, **kwargs)
        else:
            with self.assertRaises(Exception):
                func(*args, **kwargs)

    def test_conversations_for_rejects_invalid_resource_name(self):
        valid = "customers/123/localServicesLeads/1"
        bad = "customers/123/localServicesLeads/abc"
        self.assertToolError(self._call_conversations_for, [valid, bad])

        # single quotes are not part of the valid regex, so they must not be
        # silently embedded into GAQL.
        self.assertToolError(
            self._call_conversations_for,
            ["customers/123/localServicesLeads/1' OR 1=1 --"],
        )

    def test_conversations_for_chunks_more_than_50(self):
        names = [f"customers/123/localServicesLeads/{i}" for i in range(1, 52)]
        result = self._call_conversations_for(names)

        if isinstance(result, (list, tuple)):
            self.assertGreaterEqual(len(result), 2)
            for query in result:
                text = str(query)
                self.assertLessEqual(text.count("localServicesLeads/"), 50)
            total = sum(str(q).count("localServicesLeads/") for q in result)
            self.assertEqual(total, 51)
        else:
            text = str(result)
            self.assertEqual(text.count("localServicesLeads/"), 51)
            self.assertGreaterEqual(text.count(" IN "), 2)

    # ------------------------------------------------------------------
    # lead normalisation
    # ------------------------------------------------------------------

    def test_normalize_leads_parses_camel_case_nested_row(self):
        rows = [
            lead_row(
                id="7",
                type="MESSAGE",
                status="NEW",
                phone="+15551234",
                email="bob@example.com",
                name="Bob",
                created="2026-11-01 09:10:00",
                charged=True,
                credit_state=None,
                feedback=False,
                note="please call",
                category="10",
                service="20",
            )
        ]
        leads = lsa.normalize_leads(rows)
        self.assertEqual(len(leads), 1)
        lead = leads[0]

        self.assertEqual(lead["id"], "7")
        self.assertEqual(lead["type"].upper(), "MESSAGE")
        self.assertEqual(lead["status"].upper(), "NEW")
        self.assertEqual(lead["phone"], "+15551234")
        self.assertEqual(lead["email"], "bob@example.com")
        self.assertEqual(lead["name"], "Bob")
        self.assertEqual(lead["created_at"], "2026-11-01 09:10:00")
        self.assertTrue(lead["charged"])
        self.assertIsNone(lead["credit_state"])
        self.assertFalse(lead["feedback_submitted"])
        self.assertFalse(lead["wiped"])
        self.assertEqual(lead["note"], "please call")
        self.assertIsInstance(lead["resource_name"], str)
        self.assertIn("conversations", lead)

    def test_normalize_leads_wiped_when_contact_details_missing(self):
        rows = [lead_row(id="8", status="WIPED_OUT", wiped=True)]
        lead = lsa.normalize_leads(rows)[0]
        self.assertTrue(lead["wiped"])
        self.assertIn(lead.get("phone"), (None, ""))
        self.assertIn(lead.get("email"), (None, ""))
        self.assertIn(lead.get("name"), (None, ""))

    def test_normalize_leads_first_response_minutes_from_advertiser_event(self):
        resource = "customers/123/localServicesLeads/1"
        rows = [lead_row(id="1", created="2026-11-01 09:10:00")]
        convs = [
            conv_row(resource, "2026-11-01 09:11:00", participant="CONSUMER", text="hi"),
            conv_row(resource, "2026-11-01 09:15:00", participant="ADVERTISER", text="hello"),
        ]
        lead = lsa.normalize_leads(rows, convs)[0]
        self.assertAlmostEqual(lead["first_response_minutes"], 5.0)
        self.assertEqual(lead["answered"], True)

    def test_normalize_leads_phone_call_answered_live(self):
        resource = "customers/123/localServicesLeads/1"
        rows = [lead_row(id="1", type="PHONE_CALL", created="2026-11-01 09:10:00")]
        convs = [
            conv_row(
                resource,
                "2026-11-01 09:10:00",
                channel="PHONE_CALL",
                participant="CONSUMER",
                duration_ms=5000,
            )
        ]
        lead = lsa.normalize_leads(rows, convs)[0]
        self.assertEqual(lead["first_response_minutes"], 0)
        self.assertIs(lead["answered"], True)
        self.assertEqual(lead["call_seconds"], 5.0)

    def test_normalize_leads_conversations_sorted_and_text_truncated(self):
        resource = "customers/123/localServicesLeads/1"
        rows = [lead_row(id="1")]
        convs = [
            conv_row(resource, "2026-11-01 09:12:00", participant="CONSUMER", text="second"),
            conv_row(resource, "2026-11-01 09:11:00", participant="ADVERTISER", text="x" * 600),
        ]
        lead = lsa.normalize_leads(rows, convs)[0]
        times = [c["at"] for c in lead["conversations"]]
        self.assertEqual(times, sorted(times))
        self.assertLessEqual(len(lead["conversations"][1]["text"]), 500)
        self.assertIn(lead["conversations"][1]["from"], ("advertiser", "consumer"))

    def test_normalize_leads_skips_bad_row_but_keeps_valid(self):
        rows = [{}, lead_row(id="9")]
        leads = lsa.normalize_leads(rows)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["id"], "9")

    # ------------------------------------------------------------------
    # quality flags
    # ------------------------------------------------------------------

    def _normalized(self, **kwargs):
        return lsa.normalize_leads([lead_row(**kwargs)])[0]

    def _flag(self, flags, reason_enum):
        for item in flags:
            if isinstance(item, dict) and item.get("reason_enum") == reason_enum:
                return item
        self.fail(f"missing flag {reason_enum!r} in {flags!r}")

    def test_quality_flags_short_phone_call_is_possible_spam(self):
        resource = "customers/123/localServicesLeads/1"
        lead = lsa.normalize_leads(
            [lead_row(id="1", type="PHONE_CALL")],
            [
                conv_row(
                    resource,
                    "2026-11-01 09:10:00",
                    channel="PHONE_CALL",
                    participant="CONSUMER",
                    duration_ms=5000,
                )
            ],
        )[0]
        flags = lsa.quality_flags(lead, profile={})
        item = self._flag(flags, "SPAM")
        self.assertEqual(item["flag"], "possible_spam_or_misdial")
        self.assertEqual(item["confidence"], "low")

    def test_quality_flags_message_solicitation(self):
        resource = "customers/123/localServicesLeads/1"
        lead = lsa.normalize_leads(
            [lead_row(id="1", type="MESSAGE")],
            [
                conv_row(
                    resource,
                    "2026-11-01 09:10:00",
                    participant="CONSUMER",
                    text="We offer SEO marketing services. Are you the owner?",
                )
            ],
        )[0]
        flags = lsa.quality_flags(lead, profile={})
        item = self._flag(flags, "SOLICITATION")
        self.assertEqual(item["confidence"], "high")

    def test_quality_flags_never_flags_already_rated_leads(self):
        already_feedback = self._normalized(id="1", feedback=True)
        already_credit = self._normalized(id="2", credit_state="PENDING")

        self.assertEqual(lsa.quality_flags(already_feedback, profile={}), [])
        self.assertEqual(lsa.quality_flags(already_credit, profile={}), [])

    def test_quality_flags_disabled_lead(self):
        lead = self._normalized(id="1", status="DISABLED")
        self.assertEqual(lsa.quality_flags(lead, profile={}), [])

    def test_quality_flags_geo_mismatch(self):
        resource = "customers/123/localServicesLeads/1"
        lead = lsa.normalize_leads(
            [lead_row(id="1", type="MESSAGE")],
            [
                conv_row(
                    resource,
                    "2026-11-01 09:10:00",
                    participant="CONSUMER",
                    text="I live in 90210, can you come out?",
                )
            ],
        )[0]
        flags = lsa.quality_flags(
            lead,
            profile={"service_area": ["10001"], "known_outside": ["90210"]},
        )
        item = self._flag(flags, "GEO_MISMATCH")
        self.assertEqual(item["confidence"], "medium")

    def test_quality_flags_job_type_mismatch(self):
        resource = "customers/123/localServicesLeads/1"
        lead = lsa.normalize_leads(
            [lead_row(id="1", type="MESSAGE")],
            [
                conv_row(
                    resource,
                    "2026-11-01 09:10:00",
                    participant="CONSUMER",
                    text="I need roofing work on my house.",
                )
            ],
        )[0]
        flags = lsa.quality_flags(
            lead,
            profile={"services_not_offered": ["roofing"]},
        )
        item = self._flag(flags, "JOB_TYPE_MISMATCH")
        self.assertEqual(item["confidence"], "medium")

    def test_quality_flags_not_ready_to_book(self):
        resource = "customers/123/localServicesLeads/1"
        lead = lsa.normalize_leads(
            [lead_row(id="1", type="MESSAGE")],
            [
                conv_row(
                    resource,
                    "2026-11-01 09:10:00",
                    participant="CONSUMER",
                    text="Can I just get a quote for next year?",
                )
            ],
        )[0]
        flags = lsa.quality_flags(lead, profile={})
        item = self._flag(flags, "NOT_READY_TO_BOOK")
        self.assertEqual(item["confidence"], "low")

    def test_flags_for_all_marks_duplicate_phone_within_seven_days(self):
        rows = [
            lead_row(id="1", phone="+15550001", created="2026-11-01 09:00:00"),
            lead_row(id="2", phone="+15550001", created="2026-11-03 09:00:00"),
        ]
        leads = lsa.normalize_leads(rows)
        result = lsa.flags_for_all(leads, profile={})
        duplicates = list(
            _find_dicts(
                result,
                lambda d: d.get("reason_enum") == "DUPLICATE"
                and d.get("confidence") == "high",
            )
        )
        self.assertTrue(duplicates, f"expected DUPLICATE high flag in {result!r}")

    # ------------------------------------------------------------------
    # feedback planning
    # ------------------------------------------------------------------

    def _feedback_lead(self, **kwargs):
        return self._normalized(**kwargs)

    def test_feedback_plan_satisfied_body_shape(self):
        lead = self._feedback_lead(id="1")
        plan = _feedback_plan(
            [lead],
            [
                {
                    "lead_id": "1",
                    "answer": "VERY_SATISFIED",
                    "reason": "BOOKED_CUSTOMER",
                }
            ],
        )
        self.assertEqual(len(plan["submit"]), 1)
        item = plan["submit"][0]
        self.assertEqual(item["lead_id"], "1")
        self.assertIn("resource_name", item)
        body = item["body"]
        self.assertEqual(body["surveyAnswer"], "VERY_SATISFIED")
        self.assertEqual(
            body["surveySatisfied"]["surveySatisfiedReason"], "BOOKED_CUSTOMER"
        )
        self.assertNotIn("surveyDissatisfied", body)

    def test_feedback_plan_dissatisfied_body_shape(self):
        lead = self._feedback_lead(id="1")
        plan = _feedback_plan(
            [lead],
            [
                {
                    "lead_id": "1",
                    "answer": "DISSATISFIED",
                    "reason": "SPAM",
                }
            ],
        )
        self.assertEqual(len(plan["submit"]), 1)
        body = plan["submit"][0]["body"]
        self.assertEqual(body["surveyAnswer"], "DISSATISFIED")
        self.assertEqual(
            body["surveyDissatisfied"]["surveyDissatisfiedReason"], "SPAM"
        )
        self.assertNotIn("surveySatisfied", body)

    def test_feedback_plan_neutral_body_has_no_reason_block(self):
        lead = self._feedback_lead(id="1")
        plan = _feedback_plan(
            [lead], [{"lead_id": "1", "answer": "NEUTRAL"}]
        )
        self.assertEqual(len(plan["submit"]), 1)
        body = plan["submit"][0]["body"]
        self.assertEqual(body["surveyAnswer"], "NEUTRAL")
        self.assertNotIn("surveySatisfied", body)
        self.assertNotIn("surveyDissatisfied", body)

    def test_feedback_plan_other_reason_requires_comment(self):
        lead = self._feedback_lead(id="1")

        refused = _feedback_plan(
            [lead],
            [
                {
                    "lead_id": "1",
                    "answer": "SATISFIED",
                    "reason": "OTHER_SATISFIED_REASON",
                }
            ],
        )
        self.assertEqual(refused["submit"], [])
        self.assertTrue(any(r["lead_id"] == "1" for r in refused["refused"]))

        submitted = _feedback_plan(
            [lead],
            [
                {
                    "lead_id": "1",
                    "answer": "SATISFIED",
                    "reason": "OTHER_SATISFIED_REASON",
                    "comment": "great customer",
                }
            ],
        )
        self.assertEqual(len(submitted["submit"]), 1)
        body = submitted["submit"][0]["body"]
        self.assertEqual(
            body["surveySatisfied"]["otherReasonComment"], "great customer"
        )

    def test_feedback_plan_refusals(self):
        lead = self._feedback_lead(id="1", created="2026-10-01 09:00:00")
        today = date(2026, 12, 1)

        # unknown lead
        plan = _feedback_plan(
            [lead],
            [{"lead_id": "999", "answer": "SATISFIED", "reason": "BOOKED_CUSTOMER"}],
            today=today,
        )
        self.assertTrue(any(r["lead_id"] == "999" for r in plan["refused"]))

        # already submitted
        rated = self._feedback_lead(id="2", feedback=True)
        plan = _feedback_plan(
            [rated],
            [{"lead_id": "2", "answer": "SATISFIED", "reason": "BOOKED_CUSTOMER"}],
            today=today,
        )
        self.assertTrue(any(r["lead_id"] == "2" for r in plan["refused"]))

        # older than window
        plan = _feedback_plan(
            [lead],
            [{"lead_id": "1", "answer": "SATISFIED", "reason": "BOOKED_CUSTOMER"}],
            today=today,
        )
        self.assertTrue(any(r["lead_id"] == "1" for r in plan["refused"]))

        # invalid enum
        fresh = self._feedback_lead(id="3", created="2026-11-01 09:00:00")
        plan = _feedback_plan(
            [fresh],
            [{"lead_id": "3", "answer": "MAYBE", "reason": "BOOKED_CUSTOMER"}],
            today=today,
        )
        self.assertTrue(any(r["lead_id"] == "3" for r in plan["refused"]))

        # satisfied answer with dissatisfied reason
        plan = _feedback_plan(
            [fresh],
            [{"lead_id": "3", "answer": "VERY_SATISFIED", "reason": "SPAM"}],
            today=today,
        )
        self.assertTrue(any(r["lead_id"] == "3" for r in plan["refused"]))

        # over-long comment
        plan = _feedback_plan(
            [fresh],
            [
                {
                    "lead_id": "3",
                    "answer": "SATISFIED",
                    "reason": "BOOKED_CUSTOMER",
                    "comment": "x" * 1001,
                }
            ],
            today=today,
        )
        self.assertTrue(any(r["lead_id"] == "3" for r in plan["refused"]))

    def test_suggest_flags_bad_leads_and_rates_booked_leads(self):
        solicitation_resource = "customers/123/localServicesLeads/1"
        solicitation = lsa.normalize_leads(
            [lead_row(id="1", type="MESSAGE", status="NEW")],
            [
                conv_row(
                    solicitation_resource,
                    "2026-11-01 09:10:00",
                    participant="CONSUMER",
                    text="We offer SEO marketing services.",
                )
            ],
        )[0]
        booked = self._feedback_lead(id="2", status="BOOKED", feedback=False)

        decisions = lsa.suggest([solicitation, booked], profile={})

        by_id = {d["lead_id"]: d for d in decisions}
        self.assertIn("1", by_id)
        self.assertEqual(by_id["1"]["answer"], "VERY_DISSATISFIED")
        self.assertEqual(by_id["1"]["reason"], "SOLICITATION")

        self.assertIn("2", by_id)
        self.assertEqual(by_id["2"]["answer"], "VERY_SATISFIED")
        self.assertEqual(by_id["2"]["reason"], "BOOKED_CUSTOMER")

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------

    def test_report_zero_division_and_micros_conversion(self):
        lead = self._normalized(id="1", charged=False)
        report = lsa.report(
            [lead],
            [campaign_row(budget_micros="50000000", cost_micros="12345678")],
            since="2026-11-01",
            until="2026-11-30",
        )
        self.assertIsNone(report["cost_per_lead"])
        self.assertEqual(report["budget"], 50.0)
        self.assertAlmostEqual(report["spend"], 12.35, places=2)

    def test_report_by_hour_and_by_weekday(self):
        created = "2026-11-01 09:12:00"
        lead = self._normalized(id="1", created=created)
        report = lsa.report([lead], [], since="2026-11-01", until="2026-11-30")

        self.assertEqual(report["by_hour"].get(9), 1)
        expected_day = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").strftime("%a")
        self.assertEqual(report["by_weekday"].get(expected_day), 1)
        self.assertEqual(sum(report["by_weekday"].values()), 1)

    def test_report_missed_calls_and_advice(self):
        lead = self._normalized(id="1", type="PHONE_CALL")
        lead["answered"] = False

        report = lsa.report([lead], [], since="2026-11-01", until="2026-11-30")

        self.assertEqual(report["missed_calls"], 1)
        self.assertTrue(report["advice"])
        self.assertTrue(any("missed" in str(a).lower() for a in report["advice"]))

    def test_report_rates_and_vs_max_cpl(self):
        booked = self._normalized(id="1", status="BOOKED")
        new = self._normalized(id="2", status="NEW")

        report = lsa.report([booked, new], [], since="2026-11-01", until="2026-11-30")
        self.assertEqual(report["booking_rate"], 0.5)

        answered = self._normalized(id="3", type="PHONE_CALL")
        answered["answered"] = True
        missed = self._normalized(id="4", type="PHONE_CALL")
        missed["answered"] = False

        report = lsa.report(
            [answered, missed], [], since="2026-11-01", until="2026-11-30"
        )
        self.assertEqual(report["answer_rate"], 0.5)
        self.assertEqual(report["missed_calls"], 1)

        charged = self._normalized(id="5", charged=True)
        report = lsa.report(
            [charged],
            [campaign_row(cost_micros="12345678")],
            since="2026-11-01",
            until="2026-11-30",
            max_cpl=10.0,
        )
        self.assertEqual(report["vs_max_cpl"], "over")

    # ------------------------------------------------------------------
    # readiness
    # ------------------------------------------------------------------

    def _readiness(self, artifacts, profile=None, today=date(2026, 11, 1)):
        try:
            return lsa.readiness(artifacts, profile=profile, today=today)
        except TypeError:
            return lsa.readiness(
                artifacts, profile=profile, today=today.isoformat()
            )

    def _check(self, checks, keyword):
        for check in checks:
            if keyword in check["check"].lower():
                return check
        self.fail(f"no readiness check containing {keyword!r}: {checks!r}")

    def test_readiness_expiring_license_warns(self):
        art = artifact_row(
            id="1",
            artifact_type="LICENSE",
            status="PASSED",
            expiration="2026-11-15 00:00:00",
        )
        result = self._readiness([art])
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertIn("Google Verified", result["badge"])

        check = self._check(result["checks"], "license")
        self.assertEqual(check["status"], "warn")

    def test_readiness_expired_license_fails(self):
        art = artifact_row(
            id="1",
            artifact_type="LICENSE",
            status="PASSED",
            expiration="2026-10-01 00:00:00",
        )
        result = self._readiness([art])
        check = self._check(result["checks"], "license")
        self.assertEqual(check["status"], "fail")

    def test_readiness_artifact_statuses_and_profile_signals(self):
        artifacts = [
            artifact_row(
                id="1",
                artifact_type="BACKGROUND_CHECK",
                status="PASSED",
            ),
            artifact_row(
                id="2",
                artifact_type="INSURANCE",
                status="PENDING",
                expiration="2027-01-01 00:00:00",
            ),
            artifact_row(
                id="3",
                artifact_type="BUSINESS_REGISTRATION_CHECK",
                status="FAILED",
                rejection_reason="document unclear",
            ),
        ]
        profile = {
            "review_count": 0,
            "rating": 3.5,
            "has_photos": False,
            "median_first_response_minutes": 10,
        }
        result = self._readiness(artifacts, profile=profile)

        self.assertEqual(self._check(result["checks"], "background")["status"], "ok")
        self.assertEqual(self._check(result["checks"], "insurance")["status"], "warn")
        self.assertEqual(self._check(result["checks"], "business")["status"], "fail")
        self.assertEqual(self._check(result["checks"], "review")["status"], "fail")
        self.assertEqual(self._check(result["checks"], "rating")["status"], "warn")
        self.assertEqual(self._check(result["checks"], "respond")["status"], "ok")

        for check in result["checks"]:
            self.assertIn("check", check)
            self.assertIn("status", check)
            self.assertIn("fix", check)
            self.assertIn(check["status"], ("ok", "warn", "fail", "unknown"))


if __name__ == "__main__":
    unittest.main()
