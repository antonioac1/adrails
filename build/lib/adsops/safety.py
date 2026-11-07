"""adsops.safety — fail-closed guards around spend exposure, writer locking and reconciliation.

Everything here is about the moments where an agent could do harm:

* ``exposure_increases``  — did this plan make the campaign spend more money?
* ``require_fresh_guard`` — refuse increases while the unattended guard is not running.
* ``AccountLock``         — one writer per account, enforced with ``fcntl.flock``.
* ``reconcile``           — after an ambiguous commit failure, work out what landed.

Pure except for explicit filesystem helpers; every clock, sleep and file operation is injectable so
the selftest never touches the network and only uses temporary directories. Nothing is printed.
"""
import datetime as dt
import fcntl
import os
import re
import tempfile
import time

from .errors import RailViolation, ToolError

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _end_key(end_field):
    """campaign.end_date_time -> endDateTime (same camelisation as engine.plan)."""
    return "".join(w.capitalize() if i else w for i, w in enumerate(str(end_field).split("_")))


def _now(now):
    """Support now= as either a clock callable or a fixed number."""
    return float(now()) if callable(now) else float(now)


def _num(value):
    """Best-effort float; None when the value is not a number (bools are not numbers)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _micros(value):
    """int(amountMicros) as Google returns it (a string), or None if unparseable."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _updates(ops, service):
    """Yield the ``update`` payloads for one service, tolerating malformed entries."""
    entries = ops.get(service) if isinstance(ops, dict) else None
    if not isinstance(entries, (list, tuple)):
        return
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("update"), dict):
            yield entry["update"]


# ── exposure ──────────────────────────────────────────────────────────────────────────────────
def exposure_increases(ops, live, end_field="end_date_time"):
    """Return human reasons why ``ops`` would increase spend exposure ([] when it would not).

    Budget raises, re-enables and end-date extensions count. Pauses, budget cuts and negative
    keywords never do: ``campaignCriteria`` is ignored entirely.
    """
    if not isinstance(ops, dict):
        return []
    live = live if isinstance(live, dict) else {}
    end_key = _end_key(end_field)
    live_end = str(live.get("end") or "")
    live_budget = _num(live.get("budget_usd"))
    if live_budget is None:
        live_budget = 0.0  # unknown baseline: any budget is an increase (fail closed)
    reasons = []

    for update in _updates(ops, "campaignBudgets"):
        micros = _micros(update.get("amountMicros"))
        if micros is None:
            reasons.append("budget change with unreadable amount")   # fail closed
            continue
        new = micros / 1e6
        if new > live_budget + 1e-9:
            reasons.append(f"budget up {live_budget:.2f} -> {new:.2f}")

    for update in _updates(ops, "campaigns"):
        if update.get("status") == "ENABLED" and live.get("status") != "ENABLED":
            reasons.append("enable")
        if end_key in update:
            new_end = str(update[end_key])
            if new_end[:10] > live_end:
                reasons.append(f"end extended {live_end} -> {new_end}")

    return reasons


# ── heartbeat ─────────────────────────────────────────────────────────────────────────────────
def heartbeat_age_min(path, now=time.time):
    """Minutes since the heartbeat file was last written; None if it does not exist."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    return (_now(now) - mtime) / 60.0


def write_heartbeat(path, now=time.time):
    """Atomically write an ISO-8601 UTC timestamp line, creating parent directories."""
    when = dt.datetime.fromtimestamp(_now(now), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".adsops-hb-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(when + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def require_fresh_guard(ops, live, heartbeat_path, max_age_min, now=time.time,
                        end_field="end_date_time"):
    """Refuse exposure-increasing plans unless a recent heartbeat proves the guard is running."""
    reasons = exposure_increases(ops, live, end_field=end_field)
    if max_age_min is None or not reasons:
        return None
    summary = "; ".join(reasons)
    age = heartbeat_age_min(heartbeat_path, now=now)
    if age is None:
        raise RailViolation(
            f"refusing to increase spend exposure ({summary}): guard heartbeat missing at "
            f"{heartbeat_path}; the unattended guard must run at least every {max_age_min} min")
    if age < -5:   # a heartbeat dated in the future proves nothing (clock skew or a forged file)
        raise RailViolation(
            f"refusing to increase spend exposure ({summary}): guard heartbeat is dated in the future")
    if age > max_age_min:
        raise RailViolation(
            f"refusing to increase spend exposure ({summary}): guard heartbeat is {age:.1f} min "
            f"old (max {max_age_min} min) — is the guard running?")
    return None


# ── one writer per account ────────────────────────────────────────────────────────────────────
def require_enable_allowed(ops, live, guards):
    """Refuse a re-ENABLE while today's spend is at or over the daily pause threshold (guard/agent ping-pong)."""
    for u in _updates(ops, "campaigns"):
        if u.get("status") == "ENABLED" and live.get("status") != "ENABLED":
            cap = _num((guards or {}).get("daily_spend_pause_usd"))
            spent = _num(live.get("spend_today"))
            if cap is not None and (spent is None or spent >= cap):
                raise RailViolation(f"refusing to ENABLE: today's spend {spent} is at/over the daily pause "
                                    f"threshold {cap}; wait for tomorrow")
    return None


def protected_negatives(ops, protected_terms):
    """Negatives that would block a protected term (the negative's words all appear in the term). [] = fine."""
    hits = []
    terms = [str(t).lower().split() for t in (protected_terms or [])]
    for op in (ops or {}).get("campaignCriteria", []) or []:
        kw = ((op.get("create") or {}).get("keyword") or {})
        words = str(kw.get("text", "")).lower().split()
        if words and any(all(w in t for w in words) for t in terms):
            hits.append(kw.get("text"))
    return hits


class AccountLock:
    """Exclusive per-account lock (``fcntl.flock`` on a lock file), with bounded waiting."""

    poll_s = 0.2

    def __init__(self, lock_dir, account_key, timeout_s=30, sleep=time.sleep,
                 now=time.monotonic):
        self.lock_dir = str(lock_dir)
        self.account_key = str(account_key)
        safe = _UNSAFE.sub("_", self.account_key).strip("_") or "account"
        self.path = os.path.join(self.lock_dir, safe + ".lock")
        self.timeout_s = float(timeout_s)
        self.sleep = sleep
        self.now = now
        self._fh = None
        self._locked = False

    def __enter__(self):
        os.makedirs(self.lock_dir, exist_ok=True)
        fh = open(self.path, "a+")
        started = self.now()
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if self.now() - started >= self.timeout_s:
                    fh.close()
                    raise ToolError(
                        f"account {self.account_key} is busy (another adsops writer)") from None
                self.sleep(self.poll_s)
                continue
            self._fh, self._locked = fh, True
            return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            try:
                if self._locked:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh, self._locked = None, False
        return False


# ── reconciliation ────────────────────────────────────────────────────────────────────────────
def reconcile(attempted, remaining):
    """Classify each attempted service after a failed commit by re-planning on fresh live state.

    ``attempted`` is the ops dict that was being committed; ``remaining`` is ``engine.plan()``
    re-run on fresh live state after the failure. A service with no remaining ops landed, one with
    the same number of ops did not, anything in between only partly landed.
    """
    out = {"landed": [], "not_landed": [], "partial": []}
    if not isinstance(attempted, dict):
        return out
    remaining = remaining if isinstance(remaining, dict) else {}
    for svc, ops in attempted.items():
        want = len(ops) if isinstance(ops, (list, tuple)) else 0
        left_ops = remaining.get(svc)
        left = len(left_ops) if isinstance(left_ops, (list, tuple)) else 0
        if left == 0:
            out["landed"].append(svc)
        elif left == want:
            out["not_landed"].append(svc)
        else:
            out["partial"].append(svc)
    return out


# ── selftest ──────────────────────────────────────────────────────────────────────────────────
def selftest():
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, msg
        n += 1

    def attempt(fn):
        try:
            return "ok", fn()
        except Exception as e:  # noqa: BLE001 — assertions inspect the class
            return "err", e

    live = {"id": "1", "name": "c", "status": "ENABLED", "end": "2026-11-05",
            "budget_rn": "customers/1/campaignBudgets/2", "budget_usd": 25.0,
            "budget_shared": False, "spend_life": 10.0, "spend_today": 1.0, "negatives": []}
    paused = dict(live, status="PAUSED")

    # ── exposure: every reason ──────────────────────────────────────────────────────────────
    up = {"campaignBudgets": [{"update": {"amountMicros": "30000000"},
                               "updateMask": "amountMicros"}]}
    ck(exposure_increases(up, live) == ["budget up 25.00 -> 30.00"], "budget increase reason")
    ck(exposure_increases({"campaignBudgets": [{"update": {"amountMicros": "25000000"}}]},
                          live) == [], "equal budget is not an increase")
    ck(exposure_increases({"campaigns": [{"update": {"status": "ENABLED"},
                                          "updateMask": "status"}]}, paused) == ["enable"],
       "enable reason")
    ck(exposure_increases({"campaigns": [{"update": {"status": "ENABLED"}}]}, live) == [],
       "enable when already enabled is not an increase")
    ext = {"campaigns": [{"update": {"endDateTime": "2026-12-01 23:59:59"},
                          "updateMask": "endDateTime"}]}
    ck(exposure_increases(ext, live) == ["end extended 2026-11-05 -> 2026-12-01 23:59:59"],
       "end extension reason")
    ck(exposure_increases({"campaigns": [{"update": {"endDateTime": "2026-11-01 23:59:59"}}]},
                          live) == [], "end shortening is not an increase")
    ck(exposure_increases({"campaigns": [{"update": {"endDate": "2026-12-01"}}]}, live,
                          end_field="end_date") == ["end extended 2026-11-05 -> 2026-12-01"],
       "end_field honoured (endDate)")
    ck(exposure_increases({"campaigns": [{"update": {"endDate": "2026-12-01"}}]}, live) == [],
       "default end_field ignores endDate")
    ck(exposure_increases({"campaignBudgets": [{"update": {"amountMicros": "10000000"}}],
                           "campaigns": [{"update": {"status": "PAUSED"}, "updateMask": "status"}],
                           "campaignCriteria": [{"create": {"keyword": {"text": "x"}}}]},
                          live) == [], "decrease + pause + negatives -> []")
    ck(exposure_increases({"campaignCriteria": [{"create": {}}]}, live) == [],
       "negatives-only plan -> []")
    ck(exposure_increases({}, live) == [] and exposure_increases(None, live) == [],
       "empty/None ops -> []")
    ck(exposure_increases({"campaignBudgets": [{"update": {"amountMicros": "bad"}}]}, live)
       == ["budget change with unreadable amount"], "unparseable amountMicros fails closed")
    many = exposure_increases({"campaignBudgets": [{"update": {"amountMicros": "40000000"}}],
                               "campaigns": [{"update": {"status": "ENABLED",
                                                         "endDateTime": "2026-12-31"}}]}, paused)
    ck(many == ["budget up 25.00 -> 40.00", "enable",
                "end extended 2026-11-05 -> 2026-12-31"], f"multiple reasons collected: {many}")

    # ── heartbeat (real temp file, injected clock) ───────────────────────────────────────────
    td = tempfile.mkdtemp()
    hb = os.path.join(td, "nested", "guard.heartbeat")
    ck(heartbeat_age_min(os.path.join(td, "absent"), now=1000.0) is None,
       "missing heartbeat -> None")
    write_heartbeat(hb, now=1_700_000_000)
    ck(os.path.exists(hb), "write_heartbeat creates parents + file")
    with open(hb) as fh:
        line = fh.read()
    ck(line.strip() == "2023-11-14T22:13:20Z", f"heartbeat is ISO-8601 UTC: {line!r}")
    ck(not [f for f in os.listdir(os.path.dirname(hb)) if f.endswith(".tmp")],
       "no temp file left behind")
    os.utime(hb, (1_000_000.0, 1_000_000.0))
    ck(abs(heartbeat_age_min(hb, now=1_000_600.0) - 10.0) < 1e-6, "age uses mtime + injected now")
    ck(abs(heartbeat_age_min(hb, now=1_003_600.0) - 60.0) < 1e-6, "stale age computed")

    # ── require_fresh_guard ─────────────────────────────────────────────────────────────────
    ck(require_fresh_guard(up, live, os.path.join(td, "absent"), None) is None,
       "max_age_min=None is a no-op")
    ck(require_fresh_guard({}, live, os.path.join(td, "absent"), 20) is None,
       "no exposure increase is a no-op even with a missing heartbeat")
    ck(require_fresh_guard(up, live, hb, 20, now=1_000_600.0) is None, "fresh heartbeat passes")
    r = attempt(lambda: require_fresh_guard(up, live, hb, 20, now=1_003_600.0))
    ck(r[0] == "err" and isinstance(r[1], RailViolation), "stale heartbeat -> RailViolation")
    ck("60.0 min" in str(r[1]) and "max 20 min" in str(r[1]), f"stale msg has the age: {r[1]}")
    ck("budget up 25.00 -> 30.00" in str(r[1]), "stale msg lists the reasons")
    r = attempt(lambda: require_fresh_guard(ext, live, os.path.join(td, "absent"), 20, now=1.0))
    ck(r[0] == "err" and isinstance(r[1], RailViolation) and "missing" in str(r[1]),
       "missing heartbeat -> RailViolation naming the file")
    ck("end extended" in str(r[1]), "missing msg lists the reasons")

    # ── AccountLock ─────────────────────────────────────────────────────────────────────────
    ld = os.path.join(td, "locks")
    with AccountLock(ld, "acme") as lock:
        ck(lock._locked and os.path.exists(os.path.join(ld, "acme.lock")), "lock file created")
        tick = {"t": 0.0}

        def fake_now():
            return tick["t"]

        def fake_sleep(s):
            tick["t"] += s

        r = attempt(lambda: AccountLock(ld, "acme", timeout_s=1, sleep=fake_sleep,
                                        now=fake_now).__enter__())
        ck(r[0] == "err" and isinstance(r[1], ToolError), "second lock on same key -> ToolError")
        ck("account acme is busy" in str(r[1]) and "another adsops writer" in str(r[1]),
           f"busy message: {r[1]}")
        ck(tick["t"] >= 1.0, "busy wait respected the injected timeout")
        with AccountLock(ld, "other", timeout_s=1, sleep=fake_sleep, now=fake_now) as second:
            ck(second._locked, "different key does not block")
    with AccountLock(ld, "acme", timeout_s=0.5) as again:
        ck(again._locked, "lock is released on exit")
    with AccountLock(ld, "../weird key!", timeout_s=0.5) as sanitized:
        base = os.path.basename(sanitized.path)
        ck(sanitized.path.startswith(ld + os.sep) and base == "weird_key.lock",
           f"account_key sanitized: {sanitized.path}")
        stem = base[: -len(".lock")]
        ck(all(ch not in stem for ch in "/. !") and stem == "weird_key",
           "path traversal characters never reach the filename")

    # ── reconcile ───────────────────────────────────────────────────────────────────────────
    attempted = {"campaignBudgets": [{"update": {}}],
                 "campaigns": [{"update": {}}, {"update": {}}],
                 "campaignCriteria": [{"create": {}}, {"create": {}}, {"create": {}}]}
    remaining = {"campaigns": [{"update": {}}, {"update": {}}],
                 "campaignCriteria": [{"create": {}}]}
    r = reconcile(attempted, remaining)
    ck(r == {"landed": ["campaignBudgets"], "not_landed": ["campaigns"],
             "partial": ["campaignCriteria"]}, f"reconcile all three cases: {r}")
    ck(reconcile(attempted, {})["landed"] == list(attempted), "nothing left -> all landed")
    ck(reconcile(attempted, attempted)["not_landed"] == list(attempted),
       "everything left -> all not_landed")
    ck(reconcile({}, {"campaigns": [1]}) == {"landed": [], "not_landed": [], "partial": []},
       "empty attempted -> empty")
    ck(reconcile(None, None)["landed"] == [], "reconcile tolerates None")
    ck(reconcile({"campaigns": [1]}, {"campaigns": []})["landed"] == ["campaigns"],
       "empty remaining list counts as landed")

    return n
