"""adsops.metrics.store — local daily metrics store for AdRails.

Keeps a long-format daily history (source, day, entity, metric) of every number
AdRails can read (Google Ads, Search Console, AI-visibility panels, leads) in one
SQLite file, plus a pull log and a change/annotation timeline so agents can see
trends and measure whether a change worked.

Safety: the only file I/O is the SQLite path handed to Store(path); no network, no
subprocess, nothing printed (stdout is JSON-RPC). Filled series use None for
missing days because unknown is not zero, and callers never pass PII.

Typical use: facts = ads_rows(rows) / gsc_rows(rows, site, dims) -> Store.upsert;
then series(), by_entity(), ratio_series(), coverage(), avg_position().
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import re
import sqlite3
import time

try:  # normal package import
    from ..errors import ToolError
except ImportError:  # pragma: no cover - lets the selftest run standalone
    class ToolError(Exception):
        """Fallback error type used only when adsops.errors is unavailable."""


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME_RE = re.compile(r"^[a-z0-9_.:-]+$")
_AD_LEVELS = ("campaign", "account", "search_term")
_ADS_METRICS = ("cost", "clicks", "impressions", "conversions", "conversions_value")
_AGGS = {"sum": "SUM", "avg": "AVG", "max": "MAX", "min": "MIN"}
_MAX_SPAN_DAYS = 1100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
    source TEXT NOT NULL, day TEXT NOT NULL, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, entity_name TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL, value REAL NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY(source, day, entity_type, entity_id, metric));
CREATE INDEX IF NOT EXISTS facts_metric_day ON facts(metric, day);
CREATE INDEX IF NOT EXISTS facts_entity ON facts(source, entity_type, entity_id, day);
CREATE TABLE IF NOT EXISTS pulls(
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '', day_from TEXT, day_to TEXT, at TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, error TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL DEFAULT '', entity_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '', ref TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


# --------------------------------------------------------------------------- #
# coercion helpers (Google Ads hands numbers back as strings)
# --------------------------------------------------------------------------- #
def _g(row, *path, default=None):
    """Nested dict get: _g(row, 'metrics', 'costMicros')."""
    cur = row
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return default if cur is None else cur


def _f(x, default=0.0):
    """Defensive float: strings, None, NaN/inf and bools never blow up."""
    if x is None or isinstance(x, bool):
        return default
    if isinstance(x, (int, float)):
        v = float(x)
    elif isinstance(x, str):
        try:
            v = float(x.strip())
        except ValueError:
            return default
    else:
        return default
    return v if math.isfinite(v) else default


def _day_str(day):
    """Validate a strict YYYY-MM-DD string (no '20240101', no Feb 30)."""
    if not isinstance(day, str) or not _DAY_RE.match(day):
        raise ToolError("day must be a YYYY-MM-DD string")
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        raise ToolError("day is not a real calendar date") from None
    return day


def _as_date(x, what="day"):
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    if isinstance(x, str):
        return _dt.date.fromisoformat(_day_str(x))
    raise ToolError(f"{what} must be YYYY-MM-DD")


def _money(v):
    """Money and metric values leave the store as tidy floats (no 1e-17 noise)."""
    return round(float(v), 6)


# --------------------------------------------------------------------------- #
# pure date / window helpers
# --------------------------------------------------------------------------- #
def days_between(day_from, day_to):
    """Inclusive list of ISO days; refuses reversed or >1100-day ranges."""
    a = _as_date(day_from, "day_from")
    b = _as_date(day_to, "day_to")
    span = (b - a).days
    if span < 0:
        raise ToolError("day_from is after day_to")
    if span > _MAX_SPAN_DAYS:
        raise ToolError(f"day range too wide (max {_MAX_SPAN_DAYS} days)")
    return [(a + _dt.timedelta(days=i)).isoformat() for i in range(span + 1)]


def pull_window(last_day, today, backfill_days=90, settle_days=3, max_days=None):
    """Which days to pull next. Recent days are re-pulled: platforms revise them."""
    t = _as_date(today, "today")
    day_to = t - _dt.timedelta(days=1)
    if last_day is None:
        day_from = t - _dt.timedelta(days=max(0, int(backfill_days)))
    else:
        ld = _as_date(last_day, "last_day")
        # never pull the future; always re-pull the last `settle_days`
        day_from = min(ld + _dt.timedelta(days=1),
                       t - _dt.timedelta(days=max(0, int(settle_days))))
    if max_days is not None and int(max_days) > 0:
        earliest = day_to - _dt.timedelta(days=int(max_days) - 1)
        if day_from < earliest:
            day_from = earliest
    if day_from > day_to:
        return None
    return (day_from.isoformat(), day_to.isoformat())


# --------------------------------------------------------------------------- #
# row -> facts converters
# --------------------------------------------------------------------------- #
def ads_rows(rows, level="campaign"):
    """Google Ads REST rows -> facts. Duplicate (day, entity) keys are summed."""
    if level not in _AD_LEVELS:
        raise ToolError("level must be 'campaign', 'account' or 'search_term'")
    acc = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = _g(row, "segments", "date")
        if not isinstance(day, str) or not _DAY_RE.match(day):
            continue  # a fact without a date cannot be placed on the timeline
        if level == "campaign":
            cid = _g(row, "campaign", "id")
            if cid is None or cid == "":
                continue
            eid, name = str(cid), (_g(row, "campaign", "name", default="") or "")
        elif level == "account":
            eid = ""
            name = _g(row, "customer", "descriptiveName", default="") or ""
        else:
            term = _g(row, "searchTermView", "searchTerm")
            if not isinstance(term, str) or not term.strip():
                continue
            eid = term.strip().lower()
            name = eid
        slot = acc.setdefault((day, eid), {"name": "", "m": dict.fromkeys(_ADS_METRICS, 0.0)})
        if isinstance(name, str) and name:
            slot["name"] = name
        met = _g(row, "metrics", default=None)
        if not isinstance(met, dict):
            met = {}
        slot["m"]["cost"] += _f(met.get("costMicros")) / 1e6   # sum raw, round ONCE below (review #8: per-row rounding lost sub-cent rows)
        slot["m"]["clicks"] += _f(met.get("clicks"))
        slot["m"]["impressions"] += _f(met.get("impressions"))
        slot["m"]["conversions"] += _f(met.get("conversions"))
        slot["m"]["conversions_value"] += _f(met.get("conversionsValue"))
    out = []
    for (day, eid), slot in sorted(acc.items()):
        for metric in _ADS_METRICS:
            val = slot["m"][metric]
            out.append({"source": "google_ads", "day": day, "entity_type": level,
                        "entity_id": eid, "entity_name": slot["name"], "metric": metric,
                        "value": round(val, 2) if metric == "cost" else val})
    return out


def gsc_rows(rows, site, dims):
    """Search Console searchAnalytics rows -> facts (position is never summed)."""
    if not isinstance(dims, (list, tuple)) or "date" not in dims:
        raise ToolError("dims must be a list that includes 'date'")
    dims = list(dims)
    others = [d for d in dims if d != "date"]
    if not others:
        mode, idx = "site", -1
    elif len(others) == 1 and others[0] in ("query", "page"):
        mode, idx = others[0], dims.index(others[0])
    else:
        raise ToolError("dims must be ['date'] or ['date','query'|'page']")
    date_idx = dims.index("date")
    site_id = "" if site is None else str(site)
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        keys = row.get("keys")
        if not isinstance(keys, (list, tuple)) or date_idx >= len(keys):
            continue
        day = keys[date_idx]
        if not isinstance(day, str) or not _DAY_RE.match(day):
            continue
        if mode == "site":
            eid = site_id
        else:
            if idx >= len(keys):
                continue
            key = keys[idx]
            if not isinstance(key, str) or not key.strip():
                continue
            eid = key.strip().lower() if mode == "query" else key.strip()
        impr, pos = _f(row.get("impressions")), _f(row.get("position"))
        # review #4: two tracked sites share queries ("plumber tampa"); without the site in the key the second overwrote the first
        name = eid
        if mode != "site":
            eid = site_id + "|" + eid
        base = {"source": "search_console", "day": day, "entity_type": mode,
                "entity_id": eid, "entity_name": name}
        out.append(dict(base, metric="clicks", value=_f(row.get("clicks"))))
        out.append(dict(base, metric="impressions", value=impr))
        # store position * impressions so averages stay impression-weighted
        out.append(dict(base, metric="position_x_impr", value=pos * impr))
        # review #5: no raw "position" fact: any sum of positions is meaningless. Average = position_x_impr / impressions.
    return out


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def _prepare_row(r, i, now_iso):
    """Validate one fact row, returning the SQL tuple. Raises ToolError(index)."""
    if not isinstance(r, dict):
        raise ToolError(f"row {i}: must be a dict")
    src, et, met = r.get("source"), r.get("entity_type"), r.get("metric")
    for label, v in (("source", src), ("entity_type", et), ("metric", met)):
        if not isinstance(v, str) or not (1 <= len(v) <= 64) or not _NAME_RE.match(v):
            raise ToolError(f"row {i}: invalid {label}")
    try:
        day = _day_str(r.get("day"))
    except ToolError:
        raise ToolError(f"row {i}: invalid day") from None
    eid = r.get("entity_id", "")
    if isinstance(eid, bool):
        raise ToolError(f"row {i}: invalid entity_id")
    if isinstance(eid, int):
        eid = str(eid)
    elif isinstance(eid, float) and eid.is_integer():
        eid = str(int(eid))
    if not isinstance(eid, str) or len(eid) > 512:
        raise ToolError(f"row {i}: invalid entity_id")
    name = r.get("entity_name", "") or ""
    name = (name if isinstance(name, str) else str(name))[:512]
    raw = r.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ToolError(f"row {i}: value must be a finite number")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ToolError(f"row {i}: value must be a finite number") from None
    if not math.isfinite(val):
        raise ToolError(f"row {i}: value must be a finite number")
    return (src, day, et, eid, name, met, val, now_iso)


def _sql_dict(row, ok_int=False):
    d = {k: row[k] for k in row.keys()}
    if ok_int:
        d["ok"] = bool(d.get("ok"))
    return d


class Store:
    """One SQLite file: daily facts, pull log and change/annotation timeline."""

    def __init__(self, path, now=time.time):
        p = path if isinstance(path, str) else os.fspath(path)
        self.path = p
        self._now = now if callable(now) else time.time
        self._mem = p == ":memory:"
        if not self._mem and not p.startswith("file:"):
            parent = os.path.dirname(os.path.abspath(p))
            if parent:  # the owner's machine: create the folder, never anywhere else
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(p)
        self._conn.row_factory = sqlite3.Row
        if not self._mem:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- internals ---------------------------------------------------------
    def _now_iso(self):
        return _dt.datetime.fromtimestamp(self._now(), _dt.timezone.utc).isoformat(timespec="seconds")

    def _today(self):
        return _dt.datetime.fromtimestamp(self._now(), _dt.timezone.utc).date()

    # -- writes ------------------------------------------------------------
    def upsert(self, rows):
        """All-or-nothing upsert of fact dicts; returns rows written."""
        rows = list(rows or ())
        if not rows:
            return 0
        now = self._now_iso()
        prepared = [_prepare_row(r, i, now) for i, r in enumerate(rows)]  # validate first
        with self._conn:
            self._conn.executemany(
                "INSERT INTO facts(source,day,entity_type,entity_id,entity_name,metric,value,updated) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,day,entity_type,entity_id,metric) "
                "DO UPDATE SET value=excluded.value, "
                "entity_name=CASE WHEN excluded.entity_name='' THEN facts.entity_name "
                "ELSE excluded.entity_name END, updated=excluded.updated", prepared)
        return len(prepared)

    def record_pull(self, source, scope, day_from, day_to, rows, ok=True, error=None):
        """Append to the pull log (error text is truncated: it may echo a URL)."""
        err = None if error is None else str(error)[:300]
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO pulls(source,scope,day_from,day_to,at,rows,ok,error) VALUES(?,?,?,?,?,?,?,?)",
                (str(source), "" if scope is None else str(scope),
                 None if day_from is None else str(day_from),
                 None if day_to is None else str(day_to),
                 self._now_iso(), int(_f(rows)), 1 if ok else 0, err))
        return int(cur.lastrowid)

    def last_pull(self, source, scope=None, ok_only=True):
        sql = "SELECT * FROM pulls WHERE source=?"
        args = [str(source)]
        if scope is not None:
            sql += " AND scope=?"
            args.append(str(scope))
        if ok_only:
            sql += " AND ok=1"
        sql += " ORDER BY id DESC LIMIT 1"
        row = self._conn.execute(sql, args).fetchone()
        return None if row is None else _sql_dict(row, ok_int=True)

    def add_event(self, day, kind, summary, source="", entity_type="", entity_id="", ref=None):
        """Insert a change annotation. Duplicate ref -> False, nothing written."""
        vals = (_day_str(day), self._now_iso(), str(kind)[:64], str(source)[:64],
                str(entity_type)[:64], str(entity_id)[:512], str(summary)[:500],
                None if ref is None else str(ref)[:256])
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO events(day,at,kind,source,entity_type,entity_id,summary,ref) "
                    "VALUES(?,?,?,?,?,?,?,?)", vals)
        except sqlite3.IntegrityError:  # UNIQUE ref: idempotent replay of a change
            return False
        return True

    # -- reads -------------------------------------------------------------
    def events(self, day_from=None, day_to=None, entity_id=None, kind=None, limit=200):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if day_from is not None:
            sql += " AND day>=?"
            args.append(_day_str(day_from))
        if day_to is not None:
            sql += " AND day<=?"
            args.append(_day_str(day_to))
        if entity_id is not None:
            sql += " AND entity_id=?"
            args.append(str(entity_id))
        if kind is not None:
            sql += " AND kind=?"
            args.append(str(kind))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [_sql_dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _fact_filters(self, sql, args, source, entity_type, entity_ids, day_from, day_to):
        """Shared WHERE fragments for series()/by_entity() (all optional)."""
        if source is not None:
            sql += " AND source=?"
            args.append(str(source))
        if entity_type is not None:
            sql += " AND entity_type=?"
            args.append(str(entity_type))
        if entity_ids is not None:
            ids = [str(i) for i in entity_ids]
            if not ids:
                sql += " AND 1=0"  # an explicit empty selection matches nothing
            else:
                sql += " AND entity_id IN (%s)" % ",".join("?" * len(ids))
                args.extend(ids)
        if day_from is not None:
            sql += " AND day>=?"
            args.append(_day_str(day_from))
        if day_to is not None:
            sql += " AND day<=?"
            args.append(_day_str(day_to))
        return sql

    def series(self, metric, source=None, entity_type=None, entity_ids=None,
               day_from=None, day_to=None, agg="sum", fill=True):
        """Per-day aggregate across matching entities, ascending by day."""
        if agg not in _AGGS:
            raise ToolError("agg must be one of sum, avg, max, min")
        sql = f"SELECT day, {_AGGS[agg]}(value) AS v FROM facts WHERE metric=?"
        args = [str(metric)]
        sql = self._fact_filters(sql, args, source, entity_type, entity_ids, day_from, day_to)
        sql += " GROUP BY day ORDER BY day"
        got = {r["day"]: r["v"] for r in self._conn.execute(sql, args).fetchall()}
        if fill and day_from is not None and day_to is not None:
            days = days_between(day_from, day_to)
        else:
            days = sorted(got)  # sparse: only days that actually have data
        out = []
        for d in days:
            v = got.get(d)
            out.append({"day": d, "value": None if v is None else _money(v)})
        return out

    def by_entity(self, metric, source, entity_type, day_from, day_to,
                  agg="sum", limit=50, order="desc"):
        """Ranked entities for a window; entity_name is the latest non-empty name."""
        if agg not in _AGGS:
            raise ToolError("agg must be one of sum, avg, max, min")
        if order not in ("asc", "desc"):
            raise ToolError("order must be 'asc' or 'desc'")
        sql = (f"SELECT entity_id, {_AGGS[agg]}(value) AS v, COUNT(DISTINCT day) AS days "
               "FROM facts WHERE metric=?")
        args = [str(metric)]
        sql = self._fact_filters(sql, args, source, entity_type, None, day_from, day_to)
        sql += " GROUP BY entity_id ORDER BY v %s LIMIT ?" % ("DESC" if order == "desc" else "ASC")
        args.append(int(limit))
        out = []
        for r in self._conn.execute(sql, args).fetchall():
            nm = self._conn.execute(
                "SELECT entity_name FROM facts WHERE source=? AND entity_type=? AND entity_id=? "
                "AND entity_name!='' ORDER BY day DESC, updated DESC LIMIT 1",
                (str(source), str(entity_type), r["entity_id"])).fetchone()
            out.append({"entity_id": r["entity_id"],
                        "entity_name": nm["entity_name"] if nm is not None else "",
                        "value": _money(r["v"]), "days": int(r["days"])})
        return out

    def ratio_series(self, num_metric, den_metric, source=None, entity_type=None,
                     entity_ids=None, day_from=None, day_to=None, agg="sum", fill=True):
        """Per-day num/den (CTR, CPC, ...); value is None when den is 0 or unknown."""
        kw = dict(source=source, entity_type=entity_type, entity_ids=entity_ids,
                  day_from=day_from, day_to=day_to, agg=agg, fill=fill)
        nums = self.series(num_metric, **kw)
        dens = {d["day"]: d["value"] for d in self.series(den_metric, **kw)}
        out = []
        for d in nums:
            nv, dv = d["value"], dens.get(d["day"])
            val = None
            if nv is not None and dv:  # dv None or 0 -> unknown, not zero
                val = _money(nv / dv)
            out.append({"day": d["day"], "num": nv, "den": dv, "value": val})
        return out

    def coverage(self):
        """One row per source: how much history the store actually holds."""
        sql = ("SELECT source, COUNT(DISTINCT metric) AS metric_count, MIN(day) AS first_day, "
               "MAX(day) AS last_day, COUNT(DISTINCT day) AS days, COUNT(*) AS rows "
               "FROM facts GROUP BY source ORDER BY source")
        out = []
        for r in self._conn.execute(sql).fetchall():
            out.append({"source": r["source"], "metric_count": int(r["metric_count"]),
                        "first_day": r["first_day"], "last_day": r["last_day"],
                        "days": int(r["days"]), "rows": int(r["rows"])})
        return out

    def day_range(self, source):
        row = self._conn.execute(
            "SELECT MIN(day) AS a, MAX(day) AS b FROM facts WHERE source=?",
            (str(source),)).fetchone()
        if row is None or row["a"] is None:
            return (None, None)
        return (row["a"], row["b"])

    def prune(self, keep_days):
        """Drop facts older than today-keep_days (never less than 30 days retained)."""
        try:
            keep = int(keep_days)
        except (TypeError, ValueError):
            raise ToolError("keep_days must be an integer >= 30") from None
        if keep < 30:
            raise ToolError("keep_days must be >= 30")
        cutoff = (self._today() - _dt.timedelta(days=keep)).isoformat()
        with self._conn:
            cur = self._conn.execute("DELETE FROM facts WHERE day < ?", (cutoff,))
        return int(cur.rowcount)

    def close(self):
        self._conn.close()


def avg_position(store, entity_type, entity_ids, day_from, day_to, site=None):
    """Impression-weighted average position for the window (None if no impressions)."""
    ids = list(entity_ids or [])
    if site is not None and not ids:
        ids = [str(site)]
    kw = dict(source="search_console", entity_type=entity_type, entity_ids=ids or None,
              day_from=day_from, day_to=day_to, agg="sum", fill=False)
    num = sum(d["value"] for d in store.series("position_x_impr", **kw) if d["value"] is not None)
    den = sum(d["value"] for d in store.series("impressions", **kw) if d["value"] is not None)
    if not den:
        return None
    return num / den


# --------------------------------------------------------------------------- #
# selftest: every external effect mocked (fixed clock, :memory:/tempfile DB)
# --------------------------------------------------------------------------- #
def selftest() -> int:
    import tempfile

    n = 0

    def ok(cond, msg="check failed"):
        nonlocal n
        if not cond:
            raise AssertionError(msg)
        n += 1

    def raises(fn, msg="expected ToolError"):
        nonlocal n
        try:
            fn()
        except ToolError:
            n += 1
            return
        raise AssertionError(msg)

    fixed = _dt.datetime(2024, 3, 15, 12, 0, tzinfo=_dt.timezone.utc).timestamp()
    today = "2024-03-15"

    # ---- pure date helpers ------------------------------------------------
    ok(days_between("2024-01-01", "2024-01-03") == ["2024-01-01", "2024-01-02", "2024-01-03"])
    raises(lambda: days_between("2024-01-03", "2024-01-01"))
    raises(lambda: days_between("2020-01-01", "2024-01-01"))  # 1461 days > 1100

    t = _dt.date(2024, 3, 15)
    ok(pull_window(None, today, backfill_days=90) ==
       ((t - _dt.timedelta(days=90)).isoformat(), "2024-03-14"))
    ok(pull_window("2024-03-13", today, settle_days=3) == ("2024-03-12", "2024-03-14"))
    ok(pull_window(None, today, backfill_days=90, max_days=10) == ("2024-03-05", "2024-03-14"))
    ok(pull_window("2024-03-15", today, settle_days=0) is None)  # already up to date

    # ---- upsert: insert, conflict update, name retention ------------------
    st = Store(":memory:", now=lambda: fixed)
    base = {"source": "google_ads", "day": "2024-03-01", "entity_type": "campaign",
            "entity_id": "1", "metric": "cost"}
    ok(st.upsert([]) == 0)
    ok(st.upsert([dict(base, value=1.0, entity_name="Campaign A")]) == 1)
    ok(st.upsert([dict(base, value=2.5, entity_name="")]) == 1)  # conflict -> replace value
    s = st.series("cost", source="google_ads")
    ok(len(s) == 1 and s[0]["day"] == "2024-03-01" and abs(s[0]["value"] - 2.5) < 1e-9)
    ok(st.by_entity("cost", "google_ads", "campaign", "2024-03-01", "2024-03-31")[0]["entity_name"]
       == "Campaign A")  # empty new name does not erase the old one
    ok(st.upsert([dict(base, day="2024-03-03", entity_id=5, value="1.5")]) == 1)  # coerced
    ok({r["entity_id"] for r in
        st.by_entity("cost", "google_ads", "campaign", "2024-03-01", "2024-03-31")} == {"1", "5"})

    # ---- upsert: all-or-nothing validation --------------------------------
    before = st.coverage()
    raises(lambda: st.upsert([dict(base, day="2024-03-02", value=5.0),
                              dict(base, day="2024-03-02", value=True)]))
    ok(st.coverage() == before)  # nothing was written
    raises(lambda: st.upsert([dict(base, day="2024-02-30", value=1.0)]))
    raises(lambda: st.upsert([dict(base, metric="Cost USD", value=1.0)]))
    raises(lambda: st.upsert([dict(base, value=float("nan"))]))
    raises(lambda: st.upsert([dict(base, value=True)]))

    # ---- series: fill gaps with None, aggregations ------------------------
    st2 = Store(":memory:", now=lambda: fixed)
    st2.upsert([
        {"source": "google_ads", "day": "2024-03-01", "entity_type": "campaign",
         "entity_id": "1", "metric": "clicks", "value": 3},
        {"source": "google_ads", "day": "2024-03-03", "entity_type": "campaign",
         "entity_id": "1", "metric": "clicks", "value": 5},
    ])
    s = st2.series("clicks", day_from="2024-03-01", day_to="2024-03-03")
    ok([d["value"] for d in s] == [3.0, None, 5.0])  # unknown is not zero
    st2.upsert([{"source": "google_ads", "day": "2024-03-01", "entity_type": "campaign",
                 "entity_id": "2", "metric": "clicks", "value": 7}])
    ok(st2.series("clicks", day_from="2024-03-01", day_to="2024-03-01", agg="avg")[0]["value"] == 5.0)
    ok(st2.series("clicks", day_from="2024-03-01", day_to="2024-03-01", agg="sum")[0]["value"] == 10.0)
    raises(lambda: st2.series("clicks", agg="median"))

    # ---- by_entity: ordering, day counts, latest name ---------------------
    be = st2.by_entity("clicks", "google_ads", "campaign", "2024-03-01", "2024-03-31", limit=10)
    ok([(r["entity_id"], r["value"], r["days"]) for r in be] == [("1", 8.0, 2), ("2", 7.0, 1)])
    st2.upsert([
        {"source": "google_ads", "day": "2024-03-02", "entity_type": "campaign",
         "entity_id": "2", "entity_name": "Old", "metric": "clicks", "value": 0},
        {"source": "google_ads", "day": "2024-03-03", "entity_type": "campaign",
         "entity_id": "2", "entity_name": "New", "metric": "clicks", "value": 0},
    ])
    names = {r["entity_id"]: r["entity_name"] for r in
             st2.by_entity("clicks", "google_ads", "campaign", "2024-03-01", "2024-03-31", limit=10)}
    ok(names == {"1": "", "2": "New"})
    ok(st2.by_entity("clicks", "google_ads", "campaign", "2024-03-01", "2024-03-31",
                     order="asc")[0]["entity_id"] == "2")

    # ---- ratio_series: zero denominator -> None ---------------------------
    st3 = Store(":memory:", now=lambda: fixed)
    st3.upsert([
        {"source": "google_ads", "day": "2024-03-01", "entity_type": "campaign",
         "entity_id": "1", "metric": "clicks", "value": 5},
        {"source": "google_ads", "day": "2024-03-01", "entity_type": "campaign",
         "entity_id": "1", "metric": "impressions", "value": 0},
        {"source": "google_ads", "day": "2024-03-02", "entity_type": "campaign",
         "entity_id": "1", "metric": "clicks", "value": 5},
        {"source": "google_ads", "day": "2024-03-02", "entity_type": "campaign",
         "entity_id": "1", "metric": "impressions", "value": 50},
    ])
    r = st3.ratio_series("clicks", "impressions", day_from="2024-03-01", day_to="2024-03-02")
    ok([x["value"] for x in r] == [None, 0.1])

    # ---- events: duplicate ref, filters, newest first ---------------------
    ok(st3.add_event("2024-03-01", "budget_change", "Raised budget", source="google_ads",
                     entity_type="campaign", entity_id="1", ref="h1") is True)
    ok(st3.add_event("2024-03-01", "budget_change", "replay", ref="h1") is False)
    st3.add_event("2024-03-02", "bid_change", "Lowered bid", entity_id="1")
    ok(len(st3.events(kind="bid_change")) == 1)
    ev = st3.events(entity_id="1", limit=10)
    ok(len(ev) == 2 and ev[0]["day"] == "2024-03-02")
    ok(st3.events(day_from="2024-03-02", day_to="2024-03-02")[0]["kind"] == "bid_change")

    # ---- pull log ---------------------------------------------------------
    pid = st3.record_pull("google_ads", "1234567890", "2024-03-01", "2024-03-03", 42)
    ok(isinstance(pid, int) and pid > 0)
    lp = st3.last_pull("google_ads")
    ok(lp["rows"] == 42 and lp["ok"] is True and lp["day_to"] == "2024-03-03")
    st3.record_pull("google_ads", "1234567890", "2024-03-04", "2024-03-05", 0,
                    ok=False, error="boom" * 500)
    ok(st3.last_pull("google_ads")["day_to"] == "2024-03-03")  # failures skipped by default
    ok(st3.last_pull("google_ads", ok_only=False)["error"] == ("boom" * 500)[:300])

    # ---- ads_rows: micros, merging, skipped rows --------------------------
    arows = [
        {"segments": {"date": "2024-03-01"}, "campaign": {"id": "1", "name": "C1"},
         "metrics": {"costMicros": "2500000", "clicks": "3", "conversions": 1.0}},
        {"segments": {"date": "2024-03-01"}, "searchTermView": {"searchTerm": " Running Shoes "},
         "metrics": {"costMicros": "1000000", "clicks": "1"}},
        {"segments": {"date": "2024-03-01"}, "searchTermView": {"searchTerm": "running shoes"},
         "metrics": {"costMicros": "2000000", "clicks": "2"}},
        {"campaign": {"id": "1"}, "metrics": {"costMicros": "9"}},          # no date
        {"segments": {"date": "2024-03-01"}, "metrics": {"costMicros": "9"}},  # no id
    ]
    facts = ads_rows(arows, "campaign")
    ok(len(facts) == 5)  # one campaign-day x five metrics; bad rows skipped
    d = {f["metric"]: f["value"] for f in facts if f["entity_id"] == "1"}
    ok(d["cost"] == 2.5 and d["clicks"] == 3.0 and d["conversions"] == 1.0)
    sterm = ads_rows(arows, "search_term")
    ok([f["value"] for f in sterm if f["metric"] == "cost"] == [3.0])  # 1.0 + 2.0 merged
    ok([f["value"] for f in sterm if f["metric"] == "clicks"] == [3.0])
    acc = ads_rows([{"segments": {"date": "2024-03-01"},
                     "customer": {"descriptiveName": "Acme"},
                     "metrics": {"clicks": "2"}}], "account")
    ok(len(acc) == 5 and acc[0]["entity_id"] == "" and acc[0]["entity_name"] == "Acme")

    # ---- gsc_rows: query / page / site, refusals --------------------------
    grows = [
        {"keys": ["2024-03-01", "Shoes"], "clicks": 2, "impressions": 100, "position": 4.0},
        {"keys": ["2024-03-01", "boots"], "clicks": 1, "impressions": 50, "position": 6.0},
    ]
    q = gsc_rows(grows, "https://x/", ["date", "query"])
    ok(len(q) == 6)  # 2 rows x 3 metrics, no raw "position" for queries
    ok(q[0]["entity_type"] == "query" and q[0]["entity_id"] == "https://x/|shoes" and q[0]["entity_name"] == "shoes")
    ok([f["value"] for f in q if f["entity_id"] == "https://x/|shoes" and f["metric"] == "position_x_impr"]
       == [400.0])
    pg = gsc_rows([{"keys": ["2024-03-01", "/a"], "clicks": 1, "impressions": 10, "position": 3.0}],
                  "https://x/", ["date", "page"])
    ok(pg[0]["entity_type"] == "page" and pg[0]["entity_id"] == "https://x/|/a")
    site_rows = gsc_rows([{"keys": ["2024-03-01"], "clicks": 3, "impressions": 10, "position": 2.0}],
                         "https://x/", ["date"])
    ok([(f["metric"], f["value"]) for f in site_rows] ==
       [("clicks", 3.0), ("impressions", 10.0), ("position_x_impr", 20.0)])
    ok(site_rows[0]["entity_id"] == "https://x/")
    two = Store(":memory:", now=lambda: fixed)                     # review #4: same query on two sites must not collide
    two.upsert(gsc_rows(grows, "https://a/", ["date", "query"]) + gsc_rows(grows, "https://b/", ["date", "query"]))
    ok(len(two.by_entity("clicks", "search_console", "query", "2024-03-01", "2024-03-01")) == 4)
    tiny = ads_rows([{"segments": {"date": "2024-03-01"}, "searchTermView": {"searchTerm": "x"}, "metrics": {"costMicros": "4000"}}] * 3, "search_term")
    ok([f["value"] for f in tiny if f["metric"] == "cost"] == [0.01])   # review #8: 3 x $0.004 = $0.012 -> 0.01, not 0.00
    raises(lambda: gsc_rows(grows, "x", ["query"]))               # no "date"
    raises(lambda: gsc_rows(grows, "x", ["date", "query", "page"]))

    # ---- avg_position: impression weighting across days -------------------
    st4 = Store(":memory:", now=lambda: fixed)
    st4.upsert([
        {"source": "search_console", "day": "2024-03-01", "entity_type": "query",
         "entity_id": "shoes", "metric": "position_x_impr", "value": 400.0},
        {"source": "search_console", "day": "2024-03-01", "entity_type": "query",
         "entity_id": "shoes", "metric": "impressions", "value": 100.0},
        {"source": "search_console", "day": "2024-03-02", "entity_type": "query",
         "entity_id": "shoes", "metric": "position_x_impr", "value": 600.0},
        {"source": "search_console", "day": "2024-03-02", "entity_type": "query",
         "entity_id": "shoes", "metric": "impressions", "value": 50.0},
    ])
    ok(abs(avg_position(st4, "query", ["shoes"], "2024-03-01", "2024-03-02") - 1000.0 / 150.0) < 1e-9)
    ok(avg_position(st4, "query", ["nope"], "2024-03-01", "2024-03-02") is None)

    # ---- prune, coverage, day_range ---------------------------------------
    raises(lambda: st4.prune(29))
    st4.upsert([
        {"source": "google_ads", "day": "2024-01-01", "entity_type": "campaign",
         "entity_id": "9", "metric": "clicks", "value": 1},
        {"source": "google_ads", "day": "2024-03-10", "entity_type": "campaign",
         "entity_id": "9", "metric": "clicks", "value": 1},
    ])
    ok(st4.prune(30) == 1)  # today-30 = 2024-02-14; only the January fact goes
    cov = {c["source"]: c for c in st4.coverage()}
    ok(cov["google_ads"]["rows"] == 1 and cov["google_ads"]["first_day"] == "2024-03-10")
    ok(cov["search_console"]["days"] == 2 and cov["search_console"]["metric_count"] == 2)
    ok(st4.day_range("search_console") == ("2024-03-01", "2024-03-02"))
    ok(st4.day_range("nope") == (None, None))

    # ---- on-disk store: parent dirs, WAL, persistence ---------------------
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "sub", "metrics.sqlite")
        stf = Store(path, now=lambda: fixed)
        stf.upsert([{"source": "leads", "day": "2024-03-01", "entity_type": "source",
                     "entity_id": "", "metric": "leads", "value": 2}])
        ok(os.path.exists(path))
        mode = stf._conn.execute("PRAGMA journal_mode").fetchone()[0]
        ok(str(mode).lower() == "wal")
        stf.close()
        stf2 = Store(path, now=lambda: fixed)
        ok(stf2.series("leads")[0]["value"] == 2.0)
        stf2.close()

    st.close()
    st2.close()
    st3.close()
    st4.close()
    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
