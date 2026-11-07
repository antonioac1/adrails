"""Local lead book ("tiny CRM") for a small service business, one JSON file.

Every inbound lead from every channel (Google LSA, Google Ads, website, AI
assistants, GBP, referrals, direct mail, phone, walk-ins) lands here, gets a
stage, a follow-up date, and feeds reports plus offline-conversion uploads.

Safety: writes are atomic (tempfile + fsync + os.replace) and the previous
file is copied to ``<path>.bak``; the book is chmod 600 because it holds
customer PII; a corrupt file is refused rather than silently overwritten.
All time comes from the injectable ``now`` callable, so tests freeze the clock.

A tool does: ``lb = LeadBook(path); lb.upsert(row); lb.move(id, "contacted")``
— every mutating call persists the book before returning.
"""

import base64
import copy
import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import statistics
import tempfile
import time

from adsops.errors import ToolError, RailViolation, scrub  # noqa: F401  (API contract)

SOURCES = ("google_lsa", "google_ads", "website", "ai_assistant", "google_organic", "gbp",
           "referral", "direct_mail", "phone", "walk_in", "social", "other")
STAGES = ("new", "contacted", "quoted", "booked", "won", "lost", "spam")
OPEN_STAGES = ("new", "contacted", "quoted", "booked")
CLOSED_STAGES = ("won", "lost", "spam")
FOLLOW_UP_DAYS = {"contacted": 2, "quoted": 3}
MAX_LEADS = 50_000
MAX_NOTES = 500
DEFAULT_CSV_FIELDS = ("id", "created_at", "name", "phone", "email", "source", "service",
                      "area", "stage", "value", "won_value", "won_at", "next_follow_up")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REL_RE = re.compile(r"^\+(\d{1,3})\s*(d|days?)?$", re.I)   # "+3d", "+3", "+3 days"


# ---------------------------------------------------------------- helpers
def _g(row, *path, default=None):
    """Nested get for camelCase Google Ads rows; tolerant of missing keys."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def _iso(ts):
    """Epoch seconds -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC, no microseconds)."""
    dt = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value):
    """Parse ISO string / epoch number -> epoch float, or None when unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if _DATE_RE.match(text):
            dt = datetime.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_bound(value, end=False):
    """Bound for range filters: date-only -> start (or end) of that UTC day."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if _DATE_RE.match(text):
        dt = datetime.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp() + (86399.999 if end else 0.0)
    return _parse_ts(text)


def _today(ts):
    return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).date().isoformat()


def _add_days(ts, days):
    d = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).date()
    return (d + datetime.timedelta(days=days)).isoformat()


def _num(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def _phone(raw):
    """Normalise to E.164-ish: US 10 digits -> +1XXXXXXXXXX, else '+' + digits."""
    if raw is None:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return "+" + digits
    if len(digits) >= 8:
        return "+" + digits
    return ""


def _email(raw):
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    return text if "@" in text and len(text) <= 254 else ""


def _tags(raw):
    out = []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        for item in raw:
            tag = str(item).strip()[:64]
            if tag and tag not in out:
                out.append(tag)
    return out[:50]


def _norm_notes(raw, at):
    out = []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            stamp = str(item.get("at") or at)
        else:
            text = str(item or "").strip()
            stamp = at
        if text:
            out.append({"at": stamp, "text": text[:2000]})
    return out


def _lead_id(source, source_id, phone, email, created_at):
    """Stable id: 'L' + 8 base32 chars of sha256 over channel key or contact+time."""
    key = f"{source}|{source_id}" if source_id else f"{phone or email}|{created_at}"
    return "L" + base64.b32encode(hashlib.sha256(key.encode("utf-8")).digest()).decode("ascii")[:8]


def _absorb(target, incoming, now_iso, cross_source=None, fill_only=False):
    """Merge incoming fields into an existing lead; never touches the stage. fill_only: a cross-channel merge only fills blanks, so a
    sync never overwrites what the owner typed (review #7)."""
    for key in ("name", "phone", "email", "service", "area", "gclid"):
        val = incoming.get(key)
        if isinstance(val, str):
            val = val.strip()
        if val and not (fill_only and target.get(key)):
            target[key] = val
    if incoming.get("value"):
        target["value"] = round(_num(incoming["value"]), 2)
    tags = target.setdefault("tags", [])
    for tag in incoming.get("tags") or []:
        if tag not in tags:
            tags.append(tag)
    notes = target.setdefault("notes", [])
    notes.extend(incoming.get("notes") or [])
    if cross_source and not any(n.get("text") == f"also came in via {cross_source}" for n in notes):
        notes.append({"at": now_iso, "text": f"also came in via {cross_source}"})
        if "repeat" not in tags:
            tags.append("repeat")
    if len(notes) > MAX_NOTES:
        del notes[:-MAX_NOTES]
    return target


# ---------------------------------------------------------------- the book
class LeadBook:
    """One JSON file holding leads + events. Mutating methods persist eagerly."""

    def __init__(self, path, now=time.time):
        self.path = path if isinstance(path, str) else os.fspath(path)
        self.now = now
        self.data = None

    # -- storage ---------------------------------------------------
    def _ensure(self):
        if self.data is None:
            self.load()
        return self.data

    def load(self):
        if not os.path.exists(self.path):
            self.data = {"version": 1, "leads": [], "events": []}
            return self.data
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("root is not an object")
            leads = raw.get("leads")
            events = raw.get("events")
            leads = [] if leads is None else leads
            events = [] if events is None else events
            if not isinstance(leads, list) or not isinstance(events, list):
                raise ValueError("bad collections")
            for lead in leads:
                if not isinstance(lead, dict) or not lead.get("id"):
                    raise ValueError("bad lead row")
        except (OSError, ValueError):  # JSONDecodeError is a ValueError
            # Never leak parser text (may echo PII) and never overwrite the file.
            raise ToolError(
                f"lead book is corrupt: {self.path}; a backup is at {self.path}.bak"
            ) from None
        self.data = {"version": 1, "leads": leads, "events": events}
        return self.data

    def save(self):
        data = self._ensure()
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".leadbook-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            if os.path.exists(self.path):
                shutil.copy2(self.path, self.path + ".bak")
                try:
                    os.chmod(self.path + ".bak", 0o600)
                except OSError:
                    pass
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path

    # -- internals -------------------------------------------------
    def _ref(self, lead_id):
        target = str(lead_id or "").strip()
        for lead in self._ensure()["leads"]:
            if lead.get("id") == target:
                return lead
        raise ToolError(f"unknown lead: {scrub(target, 40)}")

    def _resolve_follow_up(self, spec, now_ts):
        text = str(spec or "").strip()
        rel = _REL_RE.match(text)
        if rel:
            return _add_days(now_ts, int(rel.group(1)))
        if _DATE_RE.match(text):
            return text
        raise ToolError("follow_up must be YYYY-MM-DD or +Nd")

    # -- write API -------------------------------------------------
    def upsert(self, lead, _save=True):
        if not isinstance(lead, dict):
            raise ToolError("lead must be an object")
        data = self._ensure()
        now_ts = float(self.now())
        now_iso = _iso(now_ts)

        raw_source = str(lead.get("source") or "").strip().lower()
        source = raw_source if raw_source in SOURCES else "other"
        source_id = str(lead.get("source_id") or "").strip()[:128]
        incoming = {
            "name": str(lead.get("name") or "").strip()[:200],
            "phone": _phone(lead.get("phone")),
            "email": _email(lead.get("email")),
            "service": str(lead.get("service") or "").strip()[:200],
            "area": str(lead.get("area") or "").strip()[:120],
            "gclid": str(lead.get("gclid") or "").strip()[:512],
            "tags": _tags(lead.get("tags")),
            "notes": _norm_notes(lead.get("notes"), now_iso),
            "value": _num(lead.get("value")),
        }
        born = _parse_ts(lead.get("created_at"))
        created_at = _iso(born) if born else now_iso

        # 1) exact channel identity wins (even for closed leads)
        if source_id:
            for existing in data["leads"]:
                if (existing.get("source") == source and (existing.get("source_id") or "") == source_id) or \
                        f"{source}:{source_id}" in (existing.get("alt_ids") or []):   # merged earlier from another channel
                    _absorb(existing, incoming, now_iso)
                    existing["updated_at"] = now_iso
                    if _save:
                        self.save()
                    return {"lead": copy.deepcopy(existing), "created": False}

        # 2) same phone/email on an OPEN lead -> merge, keeping the original source
        for existing in data["leads"]:
            if existing.get("stage") in CLOSED_STAGES:
                continue
            hit = (incoming["phone"] and existing.get("phone") == incoming["phone"]) or \
                  (incoming["email"] and existing.get("email") == incoming["email"])
            if hit:
                if source_id:   # remember the channel id so re-syncs match step 1 (no repeated notes; review #7)
                    alt = existing.setdefault("alt_ids", [])
                    if f"{source}:{source_id}" not in alt:
                        alt.append(f"{source}:{source_id}")
                _absorb(existing, incoming, now_iso, cross_source=source, fill_only=True)
                existing["updated_at"] = now_iso
                if _save:
                    self.save()
                return {"lead": copy.deepcopy(existing), "created": False}

        # 3) brand new lead
        if len(data["leads"]) >= MAX_LEADS:
            raise ToolError(f"lead book is full ({MAX_LEADS} leads); archive old leads first")
        stage = lead.get("stage")
        stage = stage if stage in STAGES else "new"
        follow = lead.get("next_follow_up")
        follow = follow.strip() if isinstance(follow, str) and _DATE_RE.match(follow.strip()) else _today(now_ts)
        lead_id = _lead_id(source, source_id, incoming["phone"], incoming["email"], created_at)
        taken = {row.get("id") for row in data["leads"]}
        salt = 0
        while lead_id in taken:
            salt += 1
            lead_id = _lead_id(source, source_id, incoming["phone"], incoming["email"],
                               f"{created_at}#{salt}")
        obj = {
            "id": lead_id,
            "name": incoming["name"],
            "phone": incoming["phone"],
            "email": incoming["email"],
            "source": source,
            "source_id": source_id,
            "service": incoming["service"],
            "area": incoming["area"],
            "value": round(incoming["value"], 2),
            "stage": stage,
            "lost_reason": "",
            "notes": incoming["notes"],
            "created_at": created_at,
            "updated_at": now_iso,
            "next_follow_up": follow,
            "tags": incoming["tags"],
            "gclid": incoming["gclid"],
            "won_value": 0.0,
            "won_at": "",
        }
        data["leads"].append(obj)
        if _save:
            self.save()
        return {"lead": copy.deepcopy(obj), "created": True}

    def move(self, lead_id, stage, note=None, value=None, lost_reason=None):
        if stage not in STAGES:
            raise ToolError(f"unknown stage: {scrub(str(stage), 40)}")
        lead = self._ref(lead_id)
        now_ts = float(self.now())
        now_iso = _iso(now_ts)
        frm = lead.get("stage") or "new"
        text = str(note or "").strip()[:2000]

        # Validation happens before any mutation so a refusal leaves the book intact.
        if frm in CLOSED_STAGES and stage != frm and not text:
            raise ToolError("reopening a closed lead requires a note")
        won_value = 0.0
        if stage == "won":
            if value is None:                      # spec: won REQUIRES the job value (it feeds ROI + offline conversions)
                raise ToolError("won requires value (the job's actual value, 0 allowed)")
            else:
                won_value = _num(value, None)
                if won_value is None:
                    raise ToolError("won requires a numeric value")
            if won_value < 0:
                raise ToolError("won requires value >= 0")
        reason = str(lost_reason or "").strip()[:500]
        if stage == "lost" and not reason:
            raise ToolError("lost requires a lost_reason")

        lead["stage"] = stage
        lead["updated_at"] = now_iso
        if text:
            lead.setdefault("notes", []).append({"at": now_iso, "text": text})
        if stage == "won":
            lead["won_value"] = round(won_value, 2)
            lead["won_at"] = now_iso
            lead["lost_reason"] = ""
        else:
            lead["won_value"] = 0.0
            lead["won_at"] = ""
        lead["lost_reason"] = reason if stage == "lost" else ""
        if stage in FOLLOW_UP_DAYS:
            lead["next_follow_up"] = _add_days(now_ts, FOLLOW_UP_DAYS[stage])
        elif stage in ("booked", "won", "lost", "spam"):
            lead["next_follow_up"] = ""
        self._ensure()["events"].append(
            {"at": now_iso, "lead_id": lead["id"], "from": frm, "to": stage}
        )
        self.save()
        return copy.deepcopy(lead)

    def note(self, lead_id, text, follow_up=None):
        lead = self._ref(lead_id)
        body = str(text or "").strip()[:2000]
        if not body:
            raise ToolError("note text is required")
        now_ts = float(self.now())
        now_iso = _iso(now_ts)
        lead.setdefault("notes", []).append({"at": now_iso, "text": body})
        if follow_up not in (None, ""):
            lead["next_follow_up"] = self._resolve_follow_up(follow_up, now_ts)
        lead["updated_at"] = now_iso
        self.save()
        return copy.deepcopy(lead)

    def import_rows(self, rows, source, mapping=None):
        if not isinstance(rows, (list, tuple)):
            raise ToolError("rows must be a list")
        src = source if source in SOURCES else "other"
        table = mapping if isinstance(mapping, dict) else {}
        created = updated = 0
        skipped = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                skipped.append({"row": index, "why": "row is not an object"})
                continue
            row = {table.get(key, key): val for key, val in raw.items()}
            if not (_phone(row.get("phone")) or _email(row.get("email"))
                    or str(row.get("name") or "").strip()):
                skipped.append({"row": index, "why": "no phone, email or name"})
                continue
            row["source"] = src
            try:
                result = self.upsert(row, _save=False)
            except ToolError as exc:
                skipped.append({"row": index, "why": scrub(str(exc), 200)})
                continue
            if result["created"]:
                created += 1
            else:
                updated += 1
        self.save()
        return {"created": created, "updated": updated, "skipped": skipped}

    # -- read API --------------------------------------------------
    def get(self, lead_id):
        return copy.deepcopy(self._ref(lead_id))

    def find(self, stage=None, source=None, text=None, since=None, until=None, limit=50):
        lo = _parse_bound(since, end=False) if since else None
        hi = _parse_bound(until, end=True) if until else None
        query = str(text or "").strip().lower()
        hits = []
        for lead in self._ensure()["leads"]:
            if stage and lead.get("stage") != stage:
                continue
            if source and lead.get("source") != source:
                continue
            born = _parse_ts(lead.get("created_at"))
            if lo is not None and (born is None or born < lo):
                continue
            if hi is not None and (born is None or born > hi):
                continue
            if query:
                notes = lead.get("notes") or []
                hay = " ".join([
                    str(lead.get("name") or ""), str(lead.get("phone") or ""),
                    str(lead.get("email") or ""), str(lead.get("service") or ""),
                    " ".join(str(n.get("text") if isinstance(n, dict) else n) for n in notes),
                ]).lower()
                if query not in hay:
                    continue
            hits.append(lead)
        hits.sort(key=lambda l: (l.get("created_at") or "", l.get("id") or ""), reverse=True)
        cap = 50 if limit is None else max(1, min(int(limit), 1000))
        return {"leads": [copy.deepcopy(l) for l in hits[:cap]], "total": len(hits)}

    def due(self, today=None):
        now_ts = float(self.now())
        if today is None:
            day = _today(now_ts)
        elif isinstance(today, (int, float)):
            day = _today(float(today))
        else:
            day = str(today).strip()[:10]
            if not _DATE_RE.match(day):
                raise ToolError("today must be YYYY-MM-DD")
        overdue, due_today, waiting = [], [], []
        for lead in self._ensure()["leads"]:
            if lead.get("stage") in CLOSED_STAGES:
                continue
            follow = lead.get("next_follow_up") or ""
            if not _DATE_RE.match(follow):
                continue
            if follow < day:
                overdue.append(lead)
            elif follow == day:
                due_today.append(lead)
        overdue.sort(key=lambda l: (l.get("next_follow_up") or "", l.get("created_at") or ""))
        due_today.sort(key=lambda l: l.get("created_at") or "")
        for lead in self._ensure()["leads"]:
            if lead.get("stage") != "new":
                continue
            born = _parse_ts(lead.get("created_at"))
            if born is None:
                continue
            minutes = int((now_ts - born) // 60)
            if minutes >= 15:  # speed-to-lead: only leads waiting a while
                item = copy.deepcopy(lead)
                item["waiting_minutes"] = minutes
                waiting.append((born, item))
        waiting.sort(key=lambda pair: pair[0])
        return {
            "overdue": [copy.deepcopy(l) for l in overdue],
            "today": [copy.deepcopy(l) for l in due_today],
            "new_uncontacted": [pair[1] for pair in waiting],
        }

    def pipeline(self, since=None, until=None):
        data = self._ensure()
        lo = _parse_bound(since, end=False) if since else None
        hi = _parse_bound(until, end=True) if until else None
        first_contact = {}
        for event in data["events"]:
            if event.get("from") == "new" and event.get("to") not in (None, "", "new"):
                at = _parse_ts(event.get("at"))
                lid = event.get("lead_id")
                if lid and at is not None and (lid not in first_contact or at < first_contact[lid]):
                    first_contact[lid] = at
        by_stage = {stage: 0 for stage in STAGES}
        by_source = {}
        open_value = won_value = 0.0
        deltas = []
        for lead in data["leads"]:
            born = _parse_ts(lead.get("created_at"))
            if lo is not None and (born is None or born < lo):
                continue
            if hi is not None and (born is None or born > hi):
                continue
            stage = lead.get("stage") if lead.get("stage") in STAGES else "new"
            source = lead.get("source") if lead.get("source") in SOURCES else "other"
            by_stage[stage] += 1
            bucket = by_source.setdefault(
                source, {"leads": 0, "won": 0, "won_value": 0.0, "spam": 0})
            bucket["leads"] += 1
            if stage == "spam":
                bucket["spam"] += 1
            if stage == "won":
                bucket["won"] += 1
                bucket["won_value"] += _num(lead.get("won_value"))
                won_value += _num(lead.get("won_value"))
            if stage in OPEN_STAGES:
                open_value += _num(lead.get("value"))
            if stage != "new" and lead.get("id") in first_contact and born is not None:
                deltas.append((first_contact[lead["id"]] - born) / 60.0)
        for bucket in by_source.values():
            denom = bucket["leads"] - bucket["spam"]
            bucket["win_rate"] = round(bucket["won"] / denom, 4) if denom > 0 else None
            bucket["won_value"] = round(bucket["won_value"], 2)
            del bucket["spam"]
        return {
            "by_stage": by_stage,
            "by_source": by_source,
            "open_value": round(open_value, 2),
            "won_value": round(won_value, 2),
            "median_first_contact_minutes": round(statistics.median(deltas), 2) if deltas else None,
        }

    def export_csv(self, fields=None):
        columns = list(fields) if fields else list(DEFAULT_CSV_FIELDS)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        rows = sorted(self._ensure()["leads"], key=lambda l: (l.get("created_at") or "", l.get("id") or ""))
        for lead in rows:
            cells = []
            for field in columns:
                value = lead.get(field, "")
                if isinstance(value, (list, tuple)):
                    value = "; ".join(str(v) for v in value)
                elif value is None:
                    value = ""
                text = str(value)
                # CSV injection guard; plain numbers/phones ("+18135550100", "-40.5") are data, not formulas (review #11)
                if text[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
                    text = "'" + text
                cells.append(text)
            writer.writerow(cells)
        return buf.getvalue()

    def conversions(self, since, until):
        lo = _parse_bound(since, end=False)
        hi = _parse_bound(until, end=True)
        if lo is None or hi is None:
            raise ToolError("since and until are required (YYYY-MM-DD)")
        out = []
        for lead in self._ensure()["leads"]:
            if lead.get("stage") != "won":
                continue
            gclid = str(lead.get("gclid") or "").strip()
            if not gclid:
                continue
            at = _parse_ts(lead.get("won_at"))
            if at is None or not (lo <= at <= hi):
                continue
            stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
            out.append({
                "gclid": gclid,
                "conversion_date_time": stamp.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                "value": round(_num(lead.get("won_value")), 2),
            })
        out.sort(key=lambda c: c["conversion_date_time"])
        return out


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    global MAX_LEADS
    checks = 0

    def ok(cond, msg=""):
        nonlocal checks
        checks += 1
        assert cond, msg or f"check {checks} failed"

    t = [1_700_000_000.0]  # 2023-11-14T22:13:20Z

    def now():
        return t[0]

    tmpdir = tempfile.mkdtemp(prefix="leadbook-selftest-")
    path = os.path.join(tmpdir, "leads.json")
    lb = LeadBook(path, now=now)

    # storage basics
    ok(lb.load() == {"version": 1, "leads": [], "events": []}, "missing file -> empty book")
    ok(_today(t[0]) == "2023-11-14", "fixed clock date")

    # creation + normalisation
    res = lb.upsert({"name": "Ann", "phone": "(813) 555-0100", "email": " Ann@Example.COM ",
                     "source": "google_lsa", "source_id": "LSA-1", "service": "AC repair",
                     "value": "250.5"})
    lead = res["lead"]
    ok(res["created"] and lead["id"].startswith("L") and len(lead["id"]) == 9, "new id shape")
    ok(lead["phone"] == "+18135550100" and lead["email"] == "ann@example.com", "contact norm")
    ok(lead["stage"] == "new" and lead["next_follow_up"] == "2023-11-14", "new defaults")
    ok(lead["value"] == 250.5 and lead["source"] == "google_lsa", "value/source")
    ok(os.path.exists(path) and (os.stat(path).st_mode & 0o777) == 0o600, "0600 on create")

    # dedupe by (source, source_id)
    res = lb.upsert({"name": "Ann B", "source": "google_lsa", "source_id": "LSA-1", "value": 300})
    ok(not res["created"] and res["lead"]["id"] == lead["id"] and len(lb.data["leads"]) == 1,
       "dedupe by source_id")
    ok(res["lead"]["value"] == 300.0 and res["lead"]["name"] == "Ann B", "update fields")

    # dedupe by phone across formats -> merge, keep original source, tag repeat
    res = lb.upsert({"name": "Caller", "phone": "813.555.0100", "source": "phone"})
    ok(not res["created"] and res["lead"]["id"] == lead["id"] and "repeat" in res["lead"]["tags"],
       "phone merge + repeat tag")
    ok(res["lead"]["source"] == "google_lsa", "original source kept")
    ok(any(n["text"] == "also came in via phone" for n in res["lead"]["notes"]), "merge note")
    res = lb.upsert({"name": "Third", "phone": "+18135550100", "source": "website"})
    ok(not res["created"] and len(lb.data["leads"]) == 1, "third phone format merges")

    # helpers
    ok(_phone("(813) 555-0100") == _phone("+1 813 555 0100") == "+18135550100", "phone eq")
    ok(_phone("5550100") == "" and _phone("12 34 56 78") == "+12345678", "phone short/long")
    ok(_phone("+44 20 7946 0958") == "+442079460958", "phone intl")
    ok(_email(" Foo@Bar.COM ") == "foo@bar.com" and _email("nope") == "", "email norm")

    # stage rules
    try:
        lb.move(lead["id"], "won", value=-5)
        ok(False, "negative won value must refuse")
    except ToolError:
        ok(True)
    try:
        lb.move(lead["id"], "lost")
        ok(False, "lost without reason must refuse")
    except ToolError:
        ok(True)
    try:
        lb.move(lead["id"], "teleported")
        ok(False, "unknown stage must refuse")
    except ToolError:
        ok(True)
    moved = lb.move(lead["id"], "contacted")
    ok(moved["next_follow_up"] == "2023-11-16", "contacted -> +2d")
    ok(lb.move(lead["id"], "quoted")["next_follow_up"] == "2023-11-17", "quoted -> +3d")
    ok(lb.move(lead["id"], "booked")["next_follow_up"] == "", "booked clears follow-up")
    moved = lb.move(lead["id"], "won", value=1200)
    ok(moved["won_value"] == 1200.0 and moved["won_at"] != "" and moved["next_follow_up"] == "",
       "won sets value/time")

    # no merge into a won lead
    res = lb.upsert({"name": "New Ann", "phone": "(813) 555-0100", "source": "website"})
    ok(res["created"] and len(lb.data["leads"]) == 2, "won leads never merge")

    # reopen needs a note and clears won fields
    try:
        lb.move(lead["id"], "contacted")
        ok(False, "reopen without note must refuse")
    except ToolError:
        ok(True)
    moved = lb.move(lead["id"], "contacted", note="customer called back")
    ok(moved["stage"] == "contacted" and moved["won_value"] == 0.0 and moved["won_at"] == "",
       "reopen with note clears won")
    ok(lb.note(lead["id"], "left voicemail", follow_up="+3d")["next_follow_up"] == "2023-11-17",
       "relative follow-up")

    # due() lists + speed-to-lead
    tb = LeadBook(os.path.join(tmpdir, "due.json"), now=now)
    old = tb.upsert({"name": "Scheduled", "phone": "5550001111", "source": "website"})["lead"]
    tb.move(old["id"], "contacted")  # follow-up 2023-11-16
    today_lead = tb.upsert({"name": "Today", "phone": "5550002222", "value": 500,
                            "source": "website"})["lead"]
    fresh = tb.upsert({"name": "Fresh", "phone": "5550003333", "source": "website"})["lead"]
    t[0] -= 20 * 60
    stale = tb.upsert({"name": "Stale", "phone": "5550004444", "source": "website"})["lead"]
    t[0] += 20 * 60
    due = tb.due(today="2023-11-16")
    ok([x["id"] for x in due["today"]] == [old["id"]], "due today list")
    ok({x["id"] for x in due["overdue"]} == {today_lead["id"], fresh["id"], stale["id"]},
       "overdue list")
    ok([x["id"] for x in due["new_uncontacted"]] == [stale["id"]]
       and due["new_uncontacted"][0]["waiting_minutes"] == 20, "speed-to-lead 15min gate")

    # pipeline math incl. zero division
    spam = tb.upsert({"name": "Spam", "phone": "5550009999", "source": "gbp"})["lead"]
    tb.move(spam["id"], "spam")
    pipe = tb.pipeline()
    ok(pipe["by_stage"]["new"] == 3 and pipe["by_stage"]["contacted"] == 1
       and pipe["by_stage"]["spam"] == 1, "by_stage counts")
    ok(pipe["by_source"]["website"]["leads"] == 4 and pipe["by_source"]["website"]["win_rate"] == 0.0,
       "win rate")
    ok(pipe["by_source"]["gbp"]["win_rate"] is None, "zero division -> None")
    ok(pipe["open_value"] == 500.0 and pipe["won_value"] == 0.0, "open/won value")
    ok(pipe["median_first_contact_minutes"] == 0.0, "median first contact")

    # CSV injection + header
    evil = tb.upsert({"name": "=cmd|' /C calc'!A0", "phone": "5550007777", "source": "other"})
    ok(evil["created"], "evil lead created")
    text = tb.export_csv()
    ok(text.startswith("id,created_at,name,"), "csv header")
    ok("'=cmd|" in text, "csv '=' escaped")
    tb.upsert({"name": "@SUM(A1)", "phone": "5550008888", "source": "other"})
    ok("'@SUM(A1)" in tb.export_csv(), "csv '@' escaped")

    # atomic save + backup
    ok(os.path.exists(path + ".bak") and (os.stat(path + ".bak").st_mode & 0o777) == 0o600,
       ".bak written 0600")
    ok(json.load(open(path, encoding="utf-8"))["version"] == 1, "saved json valid")

    # corrupt file refusal
    bad = os.path.join(tmpdir, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    try:
        LeadBook(bad, now=now).load()
        ok(False, "corrupt file must refuse")
    except ToolError as exc:
        ok("corrupt" in str(exc) and bad in str(exc), "corrupt message")
    ok(open(bad, encoding="utf-8").read() == "{not json", "corrupt file untouched")

    # conversions filtering
    cb = LeadBook(os.path.join(tmpdir, "conv.json"), now=now)
    w1 = cb.upsert({"name": "W1", "phone": "5551110000", "source": "google_ads",
                    "source_id": "g-1", "gclid": "ABC123"})["lead"]
    cb.move(w1["id"], "won", value=99.5)
    w2 = cb.upsert({"name": "W2", "phone": "5551110001", "source": "google_ads",
                    "gclid": "DEF456"})["lead"]
    cb.move(w2["id"], "won", value=10)
    keep = t[0]
    t[0] += 86400 * 30
    w3 = cb.upsert({"name": "W3", "phone": "5551110002", "source": "google_ads",
                    "gclid": "GHI789"})["lead"]
    cb.move(w3["id"], "won", value=5)
    t[0] = keep
    conv = cb.conversions("2023-11-14", "2023-11-15")
    ok([c["gclid"] for c in conv] == ["ABC123", "DEF456"], "conversions range filter")
    ok(conv[0]["conversion_date_time"] == "2023-11-14 22:13:20+00:00", "conversion timestamp")
    ok(conv[0]["value"] == 99.5, "conversion value")

    # import_rows: mapping, skip, update
    ib = LeadBook(os.path.join(tmpdir, "import.json"), now=now)
    rows = [
        {"Full Name": "Ivy", "Mobile": "(941) 555-0101", "Email": "IVY@x.com"},
        {"Full Name": "", "Mobile": "", "Email": ""},
        {"Full Name": "Jack", "Mobile": "9415550102"},
    ]
    out = ib.import_rows(rows, "direct_mail",
                         mapping={"Full Name": "name", "Mobile": "phone", "Email": "email"})
    ok(out["created"] == 2 and len(out["skipped"]) == 1 and out["skipped"][0]["row"] == 1,
       "import skip + create")
    ok(len(ib.data["leads"]) == 2 and os.path.exists(ib.path), "import persisted")
    out = ib.import_rows([{"Full Name": "Ivy2", "Mobile": "941.555.0101"}], "direct_mail",
                         mapping={"Full Name": "name", "Mobile": "phone"})
    ok(out["created"] == 0 and out["updated"] == 1, "import update via phone")

    # find
    ok(ib.find(text="ivy")["total"] == 1, "find text")
    ok(ib.find(text="zzz")["total"] == 0, "find miss")
    ok(len(ib.find(limit=1)["leads"]) == 1 and ib.find(limit=1)["total"] == 2, "find limit/total")

    # size boundary
    saved_cap = MAX_LEADS
    MAX_LEADS = 3
    try:
        mb = LeadBook(os.path.join(tmpdir, "max.json"), now=now)
        for i in range(3):
            mb.upsert({"name": f"P{i}", "phone": f"55522200{i:02d}", "source": "other"})
        try:
            mb.upsert({"name": "Overflow", "phone": "5552229999", "source": "other"})
            ok(False, "over capacity must refuse")
        except ToolError:
            ok(True)
    finally:
        MAX_LEADS = saved_cap

    ok(_g({"a": {"b": {"c": "7"}}}, "a", "b", "c") == "7" and _g({"a": 1}, "x", default=0) == 0,
       "nested get helper")
    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
