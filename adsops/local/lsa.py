"""Google Local Services Ads (LSA) helpers for AdRails.

This module is pure data-in/data-out: it renders GAQL, normalises LSA leads and
conversations, derives lead-quality flags and safe ProvideLeadFeedback plans,
computes performance reports and scores verification readiness.

Safety properties: no I/O, no network, no clocks (callers inject dates), no
secrets or PII in exception text, and every write is represented as a plan for
the tool layer to execute through its gated client.

A tool should call the query renderer, pass raw REST rows to the normalisers,
then let the tool layer execute only the "submit" entries it has approved.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from adsops.errors import ToolError, scrub

# ---------------------------------------------------------------------------
# Small defensive helpers
# ---------------------------------------------------------------------------

_RESOURCE_RE = re.compile(r"^customers/\d+/localServicesLeads/\d+$")


def _g(row, *path, default=None):
    """Nested dict get that is safe for malformed/partial REST rows."""
    cur = row
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return default if cur is None else cur


def _unwrap(row, key):
    """Return a REST resource object, tolerating already-unwrapped rows."""
    if isinstance(row, dict):
        inner = row.get(key)
        if isinstance(inner, dict):
            return inner
        return row
    return {}


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _truthy(value):
    """Owner answers: y/yes/true -> True, n/no/false/'' -> False, any other text (e.g. hours "Mon-Fri 7-6") -> True (it is set)."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "n", "no", "false", "0", "none", "not yet"):
            return False
        return True
    return bool(value)


def _money(micros):
    """Google micros -> currency units rounded to 2 dp."""
    return round(_float(micros) / 1_000_000.0, 2)


def _parse_dt(value):
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _as_date(value):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = _parse_dt(str(value))
    return parsed.date() if parsed else date.today()


# ---------------------------------------------------------------------------
# GAQL
# ---------------------------------------------------------------------------

QUERIES = {
    "leads": (
        "SELECT local_services_lead.id, local_services_lead.category_id, "
        "local_services_lead.service_id, local_services_lead.contact_details.phone_number, "
        "local_services_lead.contact_details.email, local_services_lead.contact_details.consumer_name, "
        "local_services_lead.lead_type, local_services_lead.lead_status, "
        "local_services_lead.creation_date_time, local_services_lead.locale, "
        "local_services_lead.note.description, local_services_lead.lead_charged, "
        "local_services_lead.credit_details.credit_state, "
        "local_services_lead.credit_details.credit_state_last_update_date_time, "
        "local_services_lead.lead_feedback_submitted "
        "FROM local_services_lead "
        "WHERE local_services_lead.creation_date_time >= '{since}' "
        "ORDER BY local_services_lead.creation_date_time DESC LIMIT {limit}"
    ),
    "artifacts": (
        "SELECT local_services_verification_artifact.id, "
        "local_services_verification_artifact.creation_date_time, "
        "local_services_verification_artifact.status, "
        "local_services_verification_artifact.artifact_type, "
        "local_services_verification_artifact.insurance_verification_artifact.expiration_date_time, "
        "local_services_verification_artifact.license_verification_artifact.expiration_date_time, "
        "local_services_verification_artifact.license_verification_artifact.rejection_reason, "
        "local_services_verification_artifact.insurance_verification_artifact.rejection_reason "
        "FROM local_services_verification_artifact"
    ),
    "campaigns": (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type, "
        "campaign_budget.amount_micros, metrics.cost_micros, metrics.impressions, "
        "metrics.clicks, metrics.phone_calls, metrics.all_conversions, segments.date "
        "FROM campaign "
        "WHERE campaign.advertising_channel_type = 'LOCAL_SERVICES' "
        "AND segments.date BETWEEN '{since}' AND '{until}'"
    ),
}

_CONVERSATIONS_TEMPLATE = (
    "SELECT local_services_lead_conversation.id, local_services_lead_conversation.lead, "
    "local_services_lead_conversation.event_date_time, "
    "local_services_lead_conversation.conversation_channel, "
    "local_services_lead_conversation.participant_type, "
    "local_services_lead_conversation.phone_call_details.call_duration_millis, "
    "local_services_lead_conversation.phone_call_details.call_recording_url, "
    "local_services_lead_conversation.message_details.text "
    "FROM local_services_lead_conversation "
    "WHERE local_services_lead_conversation.lead IN ({names}) "
    "ORDER BY local_services_lead_conversation.event_date_time ASC"
)


def render(name, **kwargs):
    """Render a named canned GAQL query, refusing unknown names/missing args."""
    template = QUERIES.get(name)
    if template is None:
        raise ToolError(f"unknown LSA query: {name}")
    try:
        return template.format(**kwargs)
    except KeyError as exc:
        raise ToolError(f"missing parameter for {name}: {exc}") from None


def conversations_for(lead_resource_names):
    """Return conversation GAQL queries, chunked to at most 50 leads each.

    Every name must be a valid local services lead resource name; otherwise a
    ToolError is raised before any query is built.
    """
    if isinstance(lead_resource_names, str):
        raise ToolError("lead_resource_names must be a list, not a string")
    names = list(lead_resource_names or [])
    queries = []
    for start in range(0, len(names), 50):
        chunk = names[start : start + 50]
        for name in chunk:
            if not isinstance(name, str) or not _RESOURCE_RE.match(name):
                raise ToolError(scrub(f"invalid lead resource name: {name!r}"))
        quoted = ", ".join("'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'" for name in chunk)
        queries.append(_CONVERSATIONS_TEMPLATE.format(names=quoted))
    return queries


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_leads(lead_rows, conv_rows=()):
    """Normalise LSA lead + conversation REST rows into compact dicts.

    Bad rows are skipped rather than crashing the batch. Conversations are
    matched by full lead resource name (falling back to the numeric lead id).
    """
    convs_by_lead = {}
    for row in conv_rows or ():
        conv = _unwrap(row, "localServicesLeadConversation")
        if not isinstance(conv, dict):
            continue
        lead_ref = conv.get("lead")
        if isinstance(lead_ref, dict):
            lead_ref = lead_ref.get("resourceName") or lead_ref.get("id")
        if not lead_ref:
            continue
        at = conv.get("eventDateTime") or ""
        channel = conv.get("conversationChannel") or "UNKNOWN"
        participant = str(conv.get("participantType") or "").lower()
        if participant not in ("advertiser", "consumer"):
            participant = ""
        millis = _g(conv, "phoneCallDetails", "callDurationMillis", default=0)
        duration_s = round(_float(millis) / 1000.0, 1)
        text = _g(conv, "messageDetails", "text", default="") or ""
        if not isinstance(text, str):
            text = str(text)
        convs_by_lead.setdefault(str(lead_ref), []).append({
            "at": str(at),
            "channel": str(channel),
            "from": participant,
            "duration_s": duration_s,
            "text": text[:500],
        })

    has_conv_data = bool(conv_rows)
    out = []
    for row in lead_rows or ():
        if not isinstance(row, dict):
            continue
        lead = _unwrap(row, "localServicesLead")
        lead_id = lead.get("id")
        if lead_id is None or str(lead_id).strip() == "":
            continue
        lead_id = str(lead_id)
        resource_name = lead.get("resourceName") or (row.get("resourceName") if isinstance(row, dict) else None)
        conversations = []
        if resource_name and resource_name in convs_by_lead:
            conversations = convs_by_lead[resource_name]
        elif not resource_name:
            for name, items in convs_by_lead.items():
                if name.rsplit("/", 1)[-1] == lead_id:
                    resource_name = name
                    conversations = items
                    break
        conversations = sorted(conversations, key=lambda item: item.get("at") or "")

        contact_raw = lead.get("contactDetails")
        contact = contact_raw if isinstance(contact_raw, dict) else {}
        status = str(lead.get("leadStatus") or "UNKNOWN")
        wiped = (not isinstance(contact_raw, dict)) or status == "WIPED_OUT"
        credit_state = _g(lead, "creditDetails", "creditState", default=None)
        lead_type = str(lead.get("leadType") or "UNKNOWN")
        created_at = str(lead.get("creationDateTime") or "")

        call_seconds = round(sum(max(0.0, _float(c.get("duration_s"))) for c in conversations), 1)
        consumer_call = any(c.get("from") == "consumer" and _float(c.get("duration_s")) > 0 for c in conversations)
        advertiser_events = [c for c in conversations if c.get("from") == "advertiser"]

        first_response = None
        if lead_type == "PHONE_CALL" and consumer_call:
            first_response = 0.0  # live-answered call
        elif advertiser_events and created_at:
            created_dt = _parse_dt(created_at)
            replied_dt = _parse_dt(advertiser_events[0].get("at"))
            if created_dt and replied_dt:
                first_response = round(max(0.0, (replied_dt - created_dt).total_seconds() / 60.0), 1)

        if lead_type == "PHONE_CALL":
            if consumer_call or advertiser_events:
                answered = True
            elif has_conv_data:
                answered = False
            else:
                answered = None
        else:
            answered = True if advertiser_events else None

        out.append({
            "id": lead_id,
            "resource_name": resource_name,
            "type": lead_type,
            "status": status,
            "category": lead.get("categoryId"),
            "service": lead.get("serviceId"),
            "created_at": created_at,
            "charged": _bool(lead.get("leadCharged")),
            "credit_state": credit_state,
            "feedback_submitted": _bool(lead.get("leadFeedbackSubmitted")),
            "name": contact.get("consumerName") or None,
            "phone": contact.get("phoneNumber") or None,
            "email": contact.get("email") or None,
            "note": _g(lead, "note", "description", default=None),
            "wiped": wiped,
            "conversations": conversations,
            "first_response_minutes": first_response,
            "answered": answered,
            "call_seconds": call_seconds,
        })
    return out


# ---------------------------------------------------------------------------
# Lead quality
# ---------------------------------------------------------------------------

_SOLICIT = re.compile(
    r"\b(seo|marketing services|google (my )?listing|ranking|website design|loan|"
    r"financing offer|are you the owner)\b",
    re.I,
)
_NOT_READY = re.compile(r"(just a quote|next year|just pricing|not ready)", re.I)
_ZIP = re.compile(r"\b\d{5}\b")


def quality_flags(lead, profile=None):
    """Heuristic flags for one lead; never flags rated/credited/disabled leads."""
    profile = profile or {}
    if not isinstance(lead, dict):
        return []
    if lead.get("feedback_submitted") or lead.get("credit_state"):
        return []
    if lead.get("status") in ("DISABLED", "WIPED_OUT", "BOOKED"):
        return []

    conversations = [c for c in (lead.get("conversations") or []) if isinstance(c, dict)]
    texts = [c.get("text") or "" for c in conversations]
    blob = " ".join(texts).lower()
    call_seconds = _float(lead.get("call_seconds"))
    has_messages = any((c.get("text") or "").strip() for c in conversations)
    flags = []

    # Short phone calls with no follow-up text are low-confidence spam/misdial.
    if lead.get("type") == "PHONE_CALL" and call_seconds <= 10 and not has_messages:
        flags.append({
            "flag": "possible_spam_or_misdial",
            "reason_enum": "SPAM",
            "confidence": "low",
            "why": f"call lasted {call_seconds:.0f}s with no messages",
        })

    match = _SOLICIT.search(blob)
    if match:
        flags.append({
            "flag": "solicitation",
            "reason_enum": "SOLICITATION",
            "confidence": "high",
            "why": f"message matched solicitation phrase: {match.group(0)}",
        })

    outside = [str(place).lower() for place in (profile.get("known_outside") or []) if str(place).strip()]
    service_area = " ".join(str(area).lower() for area in (profile.get("service_area") or []))
    named_outside = [place for place in outside if place in blob]
    area_zips = set(_ZIP.findall(service_area))
    bad_zips = []
    if area_zips:   # a city-only service area can't judge a ZIP (review #5: "33605 in Tampa" was flagged)
        for m in _ZIP.finditer(blob):
            after = blob[m.end():m.end() + 14]
            if re.match(r"\s+[a-z]+\s+(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|ct|court|way|pl|place|cir|ter|hwy)\b", after):
                continue                                              # house number, not a ZIP
            if m.group(0) not in area_zips:
                bad_zips.append(m.group(0))
    if named_outside or bad_zips:
        why_parts = []
        if named_outside:
            why_parts.append("outside-area place: " + ", ".join(named_outside))
        if bad_zips:
            why_parts.append("outside-area ZIP: " + ", ".join(bad_zips))
        flags.append({
            "flag": "geo_mismatch",
            "reason_enum": "GEO_MISMATCH",
            "confidence": "medium",
            "why": "; ".join(why_parts),
        })

    for service in profile.get("services_not_offered") or []:
        needle = str(service).strip().lower()
        if needle and needle in blob:
            flags.append({
                "flag": "job_type_mismatch",
                "reason_enum": "JOB_TYPE_MISMATCH",
                "confidence": "medium",
                "why": f"message mentions unoffered service: {service}",
            })
            break

    if _NOT_READY.search(blob):
        flags.append({
            "flag": "not_ready_to_book",
            "reason_enum": "NOT_READY_TO_BOOK",
            "confidence": "low",
            "why": "message suggests the consumer is still shopping or pricing",
        })

    return flags


def flags_for_all(leads, profile=None):
    """Run quality_flags plus cross-lead duplicate-phone detection."""
    leads = [lead for lead in (leads or []) if isinstance(lead, dict)]
    seen = {}
    out = []
    order = sorted(range(len(leads)), key=lambda i: (_parse_dt(leads[i].get("created_at")) or __import__("datetime").datetime.max))
    for index in order:      # oldest first (API returns newest first): the repeat gets flagged, not the original (review #4)
        lead = leads[index]
        flags = quality_flags(lead, profile)
        phone = str(lead.get("phone") or "").strip()
        created = _parse_dt(lead.get("created_at"))
        rated = lead.get("feedback_submitted") or lead.get("credit_state")
        if phone and created and not rated and lead.get("status") != "BOOKED":   # a booked job is never junk
            for prev_dt, prev_index in seen.get(phone, []):
                if abs((created - prev_dt).total_seconds()) <= 7 * 86400:
                    flags.append({
                        "flag": "duplicate_lead",
                        "reason_enum": "DUPLICATE",
                        "confidence": "high",
                        "why": f"same phone as lead {leads[prev_index].get('id')} within 7 days",
                    })
                    break
        if phone and created:
            seen.setdefault(phone, []).append((created, index))
        if flags:
            out.append({"lead_id": str(lead.get("id")), "flags": flags})
    return out


# ---------------------------------------------------------------------------
# Feedback planning
# ---------------------------------------------------------------------------

_ANSWERS = {"VERY_SATISFIED", "SATISFIED", "NEUTRAL", "DISSATISFIED", "VERY_DISSATISFIED"}
_SAT_REASONS = {"OTHER_SATISFIED_REASON", "BOOKED_CUSTOMER", "LIKELY_BOOKED_CUSTOMER", "SERVICE_RELATED", "HIGH_VALUE_SERVICE"}
_DIS_REASONS = {"OTHER_DISSATISFIED_REASON", "GEO_MISMATCH", "JOB_TYPE_MISMATCH", "NOT_READY_TO_BOOK", "SPAM", "DUPLICATE", "SOLICITATION"}


def feedback_plan(leads, decisions, today=None, window_days=30):
    """Build safe ProvideLeadFeedback request bodies; never performs the POST."""
    today_date = _as_date(today)
    by_key = {}
    for lead in leads or []:
        if not isinstance(lead, dict):
            continue
        lead_id = lead.get("id")
        if lead_id is not None and str(lead_id) != "":
            by_key[str(lead_id)] = lead
        resource_name = lead.get("resource_name")
        if resource_name:
            by_key[str(resource_name)] = lead

    submit = []
    refused = []
    for decision in decisions or []:
        if not isinstance(decision, dict):
            refused.append({"lead_id": None, "why": "decision must be an object"})
            continue
        key = str(decision.get("lead_id") or "")
        lead = by_key.get(key)
        if not lead:
            refused.append({"lead_id": key or None, "why": "unknown lead id"})
            continue
        if lead.get("feedback_submitted"):
            refused.append({"lead_id": key, "why": "feedback already submitted"})
            continue
        resource_name = lead.get("resource_name")
        # resource_name may be absent in hand-built rows; the tool builds the URL from customer id + lead id

        created = _parse_dt(lead.get("created_at"))
        if not created:
            refused.append({"lead_id": key, "why": "unparseable creation date"})
            continue
        if (today_date - created.date()).days > window_days:
            refused.append({"lead_id": key, "why": f"outside {window_days}-day feedback window"})
            continue

        answer = str(decision.get("answer") or "").strip().upper()
        reason = str(decision.get("reason") or "").strip().upper()
        comment = decision.get("comment") or ""
        if not isinstance(comment, str):
            comment = str(comment)
        if len(comment) > 1000:
            refused.append({"lead_id": key, "why": "comment exceeds 1000 characters"})
            continue
        if answer not in _ANSWERS:
            refused.append({"lead_id": key, "why": f"invalid survey answer: {answer or '(empty)'}"})
            continue

        if answer in ("VERY_SATISFIED", "SATISFIED"):
            if reason not in _SAT_REASONS:
                refused.append({"lead_id": key, "why": "satisfied answer needs a satisfied reason"})
                continue
            detail = {"surveySatisfiedReason": reason}
            if reason == "OTHER_SATISFIED_REASON":
                if not comment:
                    refused.append({"lead_id": key, "why": "OTHER_SATISFIED_REASON requires a comment"})
                    continue
                detail["otherReasonComment"] = comment
            body = {"surveyAnswer": answer, "surveySatisfied": detail}
        elif answer in ("DISSATISFIED", "VERY_DISSATISFIED"):
            if reason not in _DIS_REASONS:
                refused.append({"lead_id": key, "why": "dissatisfied answer needs a dissatisfied reason"})
                continue
            detail = {"surveyDissatisfiedReason": reason}
            if reason == "OTHER_DISSATISFIED_REASON":
                if not comment:
                    refused.append({"lead_id": key, "why": "OTHER_DISSATISFIED_REASON requires a comment"})
                    continue
                detail["otherReasonComment"] = comment
            body = {"surveyAnswer": answer, "surveyDissatisfied": detail}
        else:  # NEUTRAL
            if reason:
                refused.append({"lead_id": key, "why": "NEUTRAL must not include a reason"})
                continue
            body = {"surveyAnswer": "NEUTRAL"}

        submit.append({
            "lead_id": key,
            "resource_name": resource_name,
            "body": body,
            "why": f"{answer}" + (f" / {reason}" if reason else ""),
        })
    return {"submit": submit, "refused": refused}


def suggest(leads, profile=None):
    """Suggest feedback decisions: bad leads get negative, booked leads positive."""
    leads = [lead for lead in (leads or []) if isinstance(lead, dict)]
    flagged = {item["lead_id"]: item["flags"] for item in flags_for_all(leads, profile)}
    severity = {"high": 3, "medium": 2, "low": 1}
    out = []
    for lead in leads:
        lead_id = str(lead.get("id"))
        if not lead_id:
            continue
        if lead.get("feedback_submitted") or lead.get("credit_state"):
            continue
        flags = flagged.get(lead_id) or []
        if flags:
            best = None
            for flag in flags:
                if best is None or severity.get(flag.get("confidence"), 0) > severity.get(best.get("confidence"), 0):
                    best = flag
            if best and best.get("confidence") == "low":
                best = None     # spec: suggest only high/medium flags; a 4-second call may be a real customer (review #3)
            if best:
                answer = "VERY_DISSATISFIED" if best.get("confidence") == "high" and best.get("reason_enum") in ("SPAM", "SOLICITATION") else "DISSATISFIED"
                out.append({
                    "lead_id": lead_id,
                    "answer": answer,
                    "reason": best.get("reason_enum"),
                    "comment": "",  # no heuristic currently maps to an OTHER_* reason
                })
                continue
        if lead.get("status") == "BOOKED":
            out.append({
                "lead_id": lead_id,
                "answer": "VERY_SATISFIED",
                "reason": "BOOKED_CUSTOMER",
                "comment": "",
            })
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 1)


def report(leads, campaign_rows, since, until, max_cpl=None):
    """Build spend/lead/response report; all money is currency units, 2 dp."""
    leads = [lead for lead in (leads or []) if isinstance(lead, dict)]
    spend = 0.0
    budgets = {}
    for row in campaign_rows or ():
        if not isinstance(row, dict):
            continue
        day = _g(row, "segments", "date", default=None)
        if day and since and until and not (str(since) <= str(day) <= str(until)):
            continue
        spend += _float(_g(row, "metrics", "costMicros", default=0)) / 1_000_000.0
        campaign_id = str(_g(row, "campaign", "id", default="") or _g(row, "campaign", "resourceName", default=""))
        amount = _float(_g(row, "campaignBudget", "amountMicros", default=0)) / 1_000_000.0
        if amount:
            budgets[campaign_id] = amount
    spend = round(spend, 2)
    budget = round(sum(budgets.values()), 2)

    total = len(leads)
    charged = sum(1 for lead in leads if lead.get("charged"))
    credited = sum(1 for lead in leads if lead.get("credit_state") == "CREDITED")
    pending = sum(1 for lead in leads if lead.get("credit_state") == "PENDING")
    by_type = {}
    by_status = {}
    for lead in leads:
        lead_type = str(lead.get("type") or "UNKNOWN")
        status = str(lead.get("status") or "UNKNOWN")
        by_type[lead_type] = by_type.get(lead_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1

    booked = sum(1 for lead in leads if lead.get("status") == "BOOKED")
    booking_rate = round(booked / total, 4) if total else 0.0
    cost_per_lead = round(spend / charged, 2) if charged else None

    call_leads = [lead for lead in leads if lead.get("type") == "PHONE_CALL"]
    answered = [lead for lead in call_leads if lead.get("answered") is True]
    answer_rate = round(len(answered) / len(call_leads), 4) if call_leads else None
    missed_calls = sum(1 for lead in call_leads if lead.get("answered") is False)

    first_responses = [lead.get("first_response_minutes") for lead in leads if lead.get("first_response_minutes") is not None]
    median_first_response = _median([_float(value) for value in first_responses])

    by_hour = {hour: 0 for hour in range(24)}
    by_weekday = {day: 0 for day in _WEEKDAYS}
    for lead in leads:
        created = _parse_dt(lead.get("created_at"))
        if created:
            by_hour[created.hour] += 1
            by_weekday[_WEEKDAYS[created.weekday()]] += 1

    vs_max_cpl = None
    if max_cpl is not None and cost_per_lead is not None:
        vs_max_cpl = "under" if cost_per_lead <= _float(max_cpl) else "over"

    advice = []
    if not total:
        advice.append("No LSA leads in this period; check budget, service areas, and campaign status.")
    if missed_calls:
        advice.append(f"You missed {missed_calls} call(s); missed calls hurt ranking. Answer every call or call back within minutes.")
    if call_leads and median_first_response is None:
        advice.append("No advertiser replies were recorded; reply to every lead within minutes.")
    elif median_first_response is not None and median_first_response > 15:
        advice.append(f"Median first response is {median_first_response:.0f} minutes; aim for under 15.")
    off_hours = sum(by_hour[hour] for hour in list(range(0, 8)) + list(range(19, 24)))
    if off_hours:
        advice.append("Some leads arrive outside business hours; turn on messages and booking so they are not lost.")
    if vs_max_cpl == "over":
        advice.append(f"Cost per lead {cost_per_lead:.2f} is above your {float(max_cpl):.2f} target; lower category bids or tighten job types and service areas.")

    return {
        "spend": spend,
        "leads": total,
        "charged_leads": charged,
        "credited": credited,
        "pending_credit": pending,
        "cost_per_lead": cost_per_lead,
        "by_type": by_type,
        "by_status": by_status,
        "booking_rate": booking_rate,
        "answer_rate": answer_rate,
        "median_first_response_minutes": median_first_response,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "missed_calls": missed_calls,
        "budget": budget,
        "vs_max_cpl": vs_max_cpl,
        "advice": advice,
    }


# ---------------------------------------------------------------------------
# Verification readiness
# ---------------------------------------------------------------------------

_ARTIFACT_TYPES = ("BACKGROUND_CHECK", "INSURANCE", "LICENSE", "BUSINESS_REGISTRATION_CHECK")
_ARTIFACT_FIX = {
    "BACKGROUND_CHECK": "Complete the background check for your business.",
    "INSURANCE": "Upload a current insurance certificate.",
    "LICENSE": "Upload a valid trade license.",
    "BUSINESS_REGISTRATION_CHECK": "Verify your business registration details.",
}
_EXPIRY_PATHS = {
    "INSURANCE": ("insuranceVerificationArtifact", "expirationDateTime"),
    "LICENSE": ("licenseVerificationArtifact", "expirationDateTime"),
}
_REJECTION_PATHS = {
    "INSURANCE": ("insuranceVerificationArtifact", "rejectionReason"),
    "LICENSE": ("licenseVerificationArtifact", "rejectionReason"),
}
_BADGE = (
    "Google Verified badge requires the checks for your category/location to pass "
    "(Guarantee/Screened were replaced by Google Verified on 2025-10-20)"
)
_SCORE_POINTS = {"ok": 1.0, "warn": 0.5, "unknown": 0.0, "fail": 0.0}


def readiness(artifacts, profile=None, today=None):
    """Score verification readiness and return actionable checks + badge text."""
    today_date = _as_date(today)
    latest = {}
    for row in artifacts or ():
        artifact = _unwrap(row, "localServicesVerificationArtifact")
        artifact_type = artifact.get("artifactType")
        if artifact_type not in _ARTIFACT_TYPES:
            continue
        created = str(artifact.get("creationDateTime") or "")
        if artifact_type not in latest or created > str(latest[artifact_type].get("creationDateTime") or ""):
            latest[artifact_type] = artifact

    checks = []
    for artifact_type in _ARTIFACT_TYPES:
        artifact = latest.get(artifact_type)
        check_name = artifact_type.lower()
        if not artifact:
            checks.append({"check": check_name, "status": "unknown", "fix": _ARTIFACT_FIX[artifact_type]})
            continue
        status = str(artifact.get("status") or "").upper()
        rejection = _g(artifact, *_REJECTION_PATHS[artifact_type], default=None) if artifact_type in _REJECTION_PATHS else None
        if status == "PASSED":
            check_status, fix = "ok", ""
        elif status == "PENDING":
            check_status, fix = "warn", "Verification is pending review."
        elif status in ("FAILED", "NO_SUBMISSION", "CANCELLED"):
            check_status = "fail"
            fix = _ARTIFACT_FIX[artifact_type]
        else:
            check_status, fix = "unknown", _ARTIFACT_FIX[artifact_type]
        if rejection:
            fix = (fix + " " if fix else "") + f"Rejection reason: {rejection}"
        checks.append({"check": check_name, "status": check_status, "fix": fix})

    for artifact_type, check_name in (("INSURANCE", "insurance_expiry"), ("LICENSE", "license_expiry")):
        artifact = latest.get(artifact_type)
        if not artifact:
            continue
        expiration = _g(artifact, *_EXPIRY_PATHS[artifact_type], default=None)
        parsed = _parse_dt(expiration)
        if not parsed:
            continue
        days_left = (parsed.date() - today_date).days
        own = next((c for c in checks if c["check"] == artifact_type.lower()), None)
        if own and own["status"] == "ok" and days_left <= 30:
            own["status"] = "fail" if days_left < 0 else "warn"
            own["fix"] = "Expired; upload a renewed document." if days_left < 0 else f"Expires in {days_left} day(s); renew now."
        if days_left < 0:
            checks.append({"check": check_name, "status": "fail", "fix": f"Expired on {str(expiration)[:10]}; upload a new document."})
        elif days_left <= 30:
            checks.append({"check": check_name, "status": "warn", "fix": f"Expires in {days_left} day(s); renew now."})
        else:
            checks.append({"check": check_name, "status": "ok", "fix": ""})

    if profile is not None:
        if "review_count" in profile:
            reviews = _int(profile.get("review_count"))
            status = "ok" if reviews >= 5 else ("fail" if reviews == 0 else "warn")
            checks.append({"check": "reviews", "status": status, "fix": "" if reviews >= 5 else "Ask happy customers for reviews to build trust."})
        if "rating" in profile:
            rating = _float(profile.get("rating"))
            checks.append({"check": "rating", "status": "warn" if rating < 4.0 else "ok", "fix": "Rating below 4.0; fix service issues and request reviews." if rating < 4.0 else ""})
        for key, label in (("has_photos", "photos"), ("hours", "hours"), ("messages_enabled", "messages"), ("bio_written", "bio"), ("gbp_verified", "gbp_verified")):
            if key in profile:
                good = _truthy(profile.get(key))
                checks.append({"check": label, "status": "ok" if good else "warn", "fix": "" if good else f"Complete {label} in your Google Business Profile."})
        if "median_first_response_minutes" in profile:
            value = profile.get("median_first_response_minutes")
            fast = value is not None and _float(value) <= 15
            checks.append({"check": "responds_fast", "status": "ok" if fast else "warn", "fix": "" if fast else "Reply to leads within 15 minutes."})

    points = sum(_SCORE_POINTS.get(check["status"], 0.0) for check in checks)
    score = round(100.0 * points / len(checks)) if checks else 0
    score = max(0, min(100, score))
    return {"score": score, "checks": checks, "badge": _BADGE}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def selftest() -> int:
    checks = 0

    def ok(condition, message):
        nonlocal checks
        if not condition:
            raise AssertionError(message)
        checks += 1

    ok(_g({"a": {"b": {"c": "5"}}}, "a", "b", "c") == "5", "_g nested")
    ok(_g({}, "a", default=7) == 7, "_g default")
    ok(_int("12") == 12 and _float("2.5") == 2.5, "coercion")
    query = render("leads", since="2026-01-01", limit=10)
    ok("LIMIT 10" in query and "2026-01-01" in query, "render leads")

    message_lead = {"localServicesLead": {
        "id": "1",
        "resourceName": "customers/123/localServicesLeads/1",
        "leadType": "MESSAGE",
        "leadStatus": "NEW",
        "creationDateTime": "2026-11-01 09:12:00",
        "leadCharged": True,
        "contactDetails": {"phoneNumber": "+15550001", "email": "a@example.com", "consumerName": "A"},
        "creditDetails": {"creditState": "PENDING"},
        "note": {"description": "need roof"},
    }}
    conversation_rows = [
        {"localServicesLeadConversation": {"id": "c1", "lead": "customers/123/localServicesLeads/1", "eventDateTime": "2026-11-01 09:20:00", "conversationChannel": "MESSAGE", "participantType": "ADVERTISER", "messageDetails": {"text": "hi"}}},
        {"localServicesLeadConversation": {"id": "c2", "lead": "customers/123/localServicesLeads/1", "eventDateTime": "2026-11-01 09:13:00", "conversationChannel": "MESSAGE", "participantType": "CONSUMER", "messageDetails": {"text": "x" * 600}}},
    ]
    leads = normalize_leads([message_lead], conversation_rows)
    ok(len(leads) == 1, "normalize one")
    first = leads[0]
    ok(first["phone"] == "+15550001" and first["email"] == "a@example.com" and first["charged"] is True and first["credit_state"] == "PENDING", "lead fields")
    ok([conv["at"] for conv in first["conversations"]] == ["2026-11-01 09:13:00", "2026-11-01 09:20:00"], "conversation sort")
    ok(len(first["conversations"][0]["text"]) == 500, "text truncation")
    ok(first["first_response_minutes"] == 8.0 and first["answered"] is True, "first response minutes + replied = answered")

    wiped = normalize_leads([{"localServicesLead": {"id": "2", "leadStatus": "WIPED_OUT", "creationDateTime": "2026-11-02 10:00:00", "leadCharged": True}}])
    ok(len(wiped) == 1 and wiped[0]["wiped"] is True and wiped[0]["phone"] is None and wiped[0]["name"] is None, "wiped lead")

    call_lead = {"localServicesLead": {"id": "3", "resourceName": "customers/123/localServicesLeads/3", "leadType": "PHONE_CALL", "leadStatus": "BOOKED", "creationDateTime": "2026-11-03 08:00:00", "contactDetails": {"phoneNumber": "+15550002"}}}
    call_conv = [{"localServicesLeadConversation": {"lead": "customers/123/localServicesLeads/3", "eventDateTime": "2026-11-03 08:00:00", "conversationChannel": "PHONE_CALL", "participantType": "CONSUMER", "phoneCallDetails": {"callDurationMillis": "45000"}}}]
    call = normalize_leads([call_lead], call_conv)[0]
    ok(call["first_response_minutes"] == 0.0 and call["answered"] is True and call["call_seconds"] == 45.0, "live call")

    ok(normalize_leads([None, {}, {"localServicesLead": {"id": ""}}]) == [], "skip malformed")

    queries = conversations_for(["customers/1/localServicesLeads/2"])
    ok(len(queries) == 1 and "'customers/1/localServicesLeads/2'" in queries[0], "conversation query")
    try:
        conversations_for(["customers/x/localServicesLeads/2"])
        ok(False, "bad resource must be rejected")
    except ToolError:
        ok(True, "bad resource rejected")
    ok(len(conversations_for([f"customers/1/localServicesLeads/{i}" for i in range(51)])) == 2, "conversation chunking")

    spam_flags = quality_flags({"id": "10", "type": "PHONE_CALL", "status": "NEW", "call_seconds": 5, "conversations": []})
    ok(any(flag["reason_enum"] == "SPAM" for flag in spam_flags), "short-call spam")
    solicitation = {"id": "11", "type": "MESSAGE", "status": "NEW", "conversations": [{"text": "We offer SEO and Google listing ranking services"}], "charged": True}
    ok(any(flag["reason_enum"] == "SOLICITATION" and flag["confidence"] == "high" for flag in quality_flags(solicitation)), "solicitation")
    ok(quality_flags(dict(solicitation, feedback_submitted=True)) == [], "rated lead not flagged")

    geo_lead = {"id": "12", "type": "MESSAGE", "status": "NEW", "conversations": [{"text": "my zip is 90210"}]}
    ok(any(flag["reason_enum"] == "GEO_MISMATCH" for flag in quality_flags(geo_lead, {"service_area": ["Los Angeles 90001"], "known_outside": []})), "geo mismatch")
    ok(not quality_flags({"id": "12b", "type": "MESSAGE", "status": "NEW", "conversations": [{"text": "I'm at 33605 in Tampa, 12345 Oak Lane"}]},
                         {"service_area": ["Tampa", "Brandon"]}), "city-only area never ZIP-flags; street numbers ignored")
    job_lead = {"id": "13", "type": "MESSAGE", "status": "NEW", "conversations": [{"text": "I need roof repair"}]}
    ok(any(flag["reason_enum"] == "JOB_TYPE_MISMATCH" for flag in quality_flags(job_lead, {"services_not_offered": ["roof repair"]})), "job mismatch")
    not_ready = {"id": "14", "type": "MESSAGE", "status": "NEW", "conversations": [{"text": "just pricing for now"}]}
    ok(any(flag["reason_enum"] == "NOT_READY_TO_BOOK" and flag["confidence"] == "low" for flag in quality_flags(not_ready)), "not ready to book")

    base = {"type": "MESSAGE", "status": "NEW", "conversations": [], "phone": "+15550009", "created_at": "2026-11-01 10:00:00"}
    all_flags = flags_for_all([dict(base, id="20"), dict(base, id="21", created_at="2026-11-03 10:00:00")])
    ok(any(item["lead_id"] == "21" and any(flag["reason_enum"] == "DUPLICATE" for flag in item["flags"]) for item in all_flags), "duplicate lead")

    fb_lead = {"id": "30", "resource_name": "customers/1/localServicesLeads/30", "type": "MESSAGE", "status": "BOOKED", "created_at": "2026-11-01 10:00:00", "feedback_submitted": False, "credit_state": None}
    plan = feedback_plan([fb_lead], [{"lead_id": "30", "answer": "VERY_SATISFIED", "reason": "BOOKED_CUSTOMER"}], today="2026-11-10")
    ok(len(plan["submit"]) == 1 and plan["submit"][0]["body"] == {"surveyAnswer": "VERY_SATISFIED", "surveySatisfied": {"surveySatisfiedReason": "BOOKED_CUSTOMER"}}, "satisfied feedback body")
    plan = feedback_plan([fb_lead], [{"lead_id": "30", "answer": "DISSATISFIED", "reason": "OTHER_DISSATISFIED_REASON"}], today="2026-11-10")
    ok(not plan["submit"] and plan["refused"], "OTHER requires comment")
    plan = feedback_plan([fb_lead], [{"lead_id": "30", "answer": "DISSATISFIED", "reason": "OTHER_DISSATISFIED_REASON", "comment": "bad experience"}], today="2026-11-10")
    ok(plan["submit"][0]["body"]["surveyDissatisfied"]["otherReasonComment"] == "bad experience", "OTHER comment body")
    plan = feedback_plan([fb_lead], [{"lead_id": "30", "answer": "NEUTRAL"}], today="2026-11-10")
    ok(plan["submit"][0]["body"] == {"surveyAnswer": "NEUTRAL"}, "neutral body")

    old_lead = dict(fb_lead, id="31", resource_name="customers/1/localServicesLeads/31", created_at="2026-01-01 10:00:00")
    plan = feedback_plan([old_lead], [{"lead_id": "31", "answer": "SATISFIED", "reason": "SERVICE_RELATED"}], today="2026-11-10")
    ok(not plan["submit"] and "window" in plan["refused"][0]["why"], "window refusal")
    plan = feedback_plan([fb_lead], [{"lead_id": "999", "answer": "SATISFIED", "reason": "SERVICE_RELATED"}], today="2026-11-10")
    ok(not plan["submit"], "unknown lead refused")
    plan = feedback_plan([fb_lead], [{"lead_id": "30", "answer": "MAYBE"}], today="2026-11-10")
    ok(not plan["submit"], "invalid answer refused")
    plan = feedback_plan([fb_lead], [{"lead_id": "30", "answer": "SATISFIED", "reason": "SPAM"}], today="2026-11-10")
    ok(not plan["submit"], "reason mismatch refused")

    suggestions = suggest([
        dict(solicitation, id="40", resource_name="customers/1/localServicesLeads/40"),
        dict(fb_lead, id="41", resource_name="customers/1/localServicesLeads/41"),
    ])
    ok(any(item["answer"] == "VERY_DISSATISFIED" and item["reason"] == "SOLICITATION" for item in suggestions), "suggest bad lead")
    ok(any(item["answer"] == "VERY_SATISFIED" and item["reason"] == "BOOKED_CUSTOMER" for item in suggestions), "suggest booked lead")

    campaign_rows = [{"campaign": {"id": "c1"}, "campaignBudget": {"amountMicros": "50000000"}, "metrics": {"costMicros": "2500000"}, "segments": {"date": "2026-11-01"}}]
    empty_report = report([], campaign_rows, "2026-11-01", "2026-11-30")
    ok(empty_report["spend"] == 2.5 and empty_report["cost_per_lead"] is None and empty_report["budget"] == 50.0, "report zero-division/micros")
    ok(len(empty_report["by_hour"]) == 24 and len(empty_report["by_weekday"]) == 7, "report bucket shapes")
    full_report = report([
        dict(fb_lead, id="50", type="PHONE_CALL", charged=True, credit_state="CREDITED", answered=True, first_response_minutes=5.0, created_at="2026-11-02 14:00:00"),
        dict(fb_lead, id="51", type="PHONE_CALL", charged=False, credit_state="PENDING", answered=False, first_response_minutes=None, created_at="2026-11-02 03:00:00"),
    ], campaign_rows, "2026-11-01", "2026-11-30", max_cpl=1.0)
    ok(full_report["cost_per_lead"] == 2.5 and full_report["vs_max_cpl"] == "over", "report cpl/vs max")
    ok(full_report["missed_calls"] == 1 and full_report["answer_rate"] == 0.5, "report calls")
    ok(full_report["by_hour"][14] == 1 and full_report["by_hour"][3] == 1 and full_report["median_first_response_minutes"] == 5.0, "report hour/median")
    ok(full_report["credited"] == 1 and full_report["pending_credit"] == 1, "report credits")

    artifacts = [
        {"localServicesVerificationArtifact": {"artifactType": "LICENSE", "status": "PASSED", "creationDateTime": "2026-01-01 10:00:00", "licenseVerificationArtifact": {"expirationDateTime": "2026-12-01 00:00:00"}}},
        {"localServicesVerificationArtifact": {"artifactType": "BACKGROUND_CHECK", "status": "PASSED", "creationDateTime": "2026-01-01 10:00:00"}},
        {"localServicesVerificationArtifact": {"artifactType": "INSURANCE", "status": "FAILED", "creationDateTime": "2026-01-01 10:00:00", "insuranceVerificationArtifact": {"rejectionReason": "EXPIRED_DOCUMENT"}}},
    ]
    ready = readiness(artifacts, today="2026-11-10")
    ok(0 <= ready["score"] <= 100 and "Google Verified" in ready["badge"], "readiness score/badge")
    license_check = [check for check in ready["checks"] if check["check"] == "license_expiry"][0]
    ok(license_check["status"] == "warn", "expiring license warns")
    insurance_check = [check for check in ready["checks"] if check["check"] == "insurance"][0]
    ok(insurance_check["status"] == "fail" and "EXPIRED_DOCUMENT" in insurance_check["fix"], "rejection reason surfaced")

    expired = readiness([{"localServicesVerificationArtifact": {"artifactType": "LICENSE", "status": "PASSED", "licenseVerificationArtifact": {"expirationDateTime": "2026-10-01 00:00:00"}}}], today="2026-11-10")
    ok([check for check in expired["checks"] if check["check"] == "license_expiry"][0]["status"] == "fail", "expired license fails")

    profile_ready = readiness([], profile={"review_count": 0, "rating": 3.9, "has_photos": False, "gbp_verified": True}, today="2026-11-10")
    ok(any(check["check"] == "reviews" and check["status"] == "fail" for check in profile_ready["checks"]), "reviews fail")
    ok(any(check["check"] == "rating" and check["status"] == "warn" for check in profile_ready["checks"]), "rating warn")

    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
