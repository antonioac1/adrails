"""SQLite store: SERP samples (the share denominator), ad observations, pending tasks, and the spend ledger.

Tables
  samples       one row per fetched SERP: task_id, ts, tenant, keyword, location, device, ads_count
  observations  one row per ad seen: ts, keyword, location, device, position (paid rank_group), rank_absolute,
                top_of_page, domain, display_url, headline, description, landing_url (+ task_id, tenant)
  pending       tasks posted to the standard queue and not yet collected
  ledger        append-only spend: ts, month, kind, n_tasks, est_usd, actual_usd, note
  meta          small key/value state (rotation cursor)
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  task_id TEXT PRIMARY KEY, ts TEXT NOT NULL, tenant TEXT NOT NULL, keyword TEXT NOT NULL,
  location TEXT NOT NULL, device TEXT NOT NULL, ads_count INTEGER NOT NULL, check_url TEXT);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, tenant TEXT NOT NULL, ts TEXT NOT NULL, keyword TEXT NOT NULL,
  location TEXT NOT NULL, device TEXT NOT NULL, position INTEGER, rank_absolute INTEGER,
  top_of_page INTEGER NOT NULL, domain TEXT NOT NULL, display_url TEXT, headline TEXT, description TEXT,
  landing_url TEXT);
CREATE INDEX IF NOT EXISTS obs_domain ON observations(tenant, domain, ts);
CREATE INDEX IF NOT EXISTS obs_kw ON observations(tenant, keyword, location);
CREATE TABLE IF NOT EXISTS pending (
  task_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, posted_at TEXT NOT NULL, keyword TEXT NOT NULL,
  location TEXT NOT NULL, device TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, tenant TEXT NOT NULL, month TEXT NOT NULL, kind TEXT NOT NULL,
  n_tasks INTEGER NOT NULL, est_usd REAL NOT NULL, actual_usd REAL, note TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def iso(t=None):
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ts(dfs_datetime, fallback):
    """DataForSEO 'YYYY-MM-DD HH:MM:SS +00:00' -> ISO Z; else fallback."""
    try:
        d = datetime.strptime(dfs_datetime, "%Y-%m-%d %H:%M:%S %z")
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path, tenant):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.tenant = tenant

    def close(self):
        self.db.close()

    # --- spend ledger ---
    def ledger_add(self, kind, n_tasks, est_usd, actual_usd=None, note="", ts=None):
        ts = ts or iso()
        self.db.execute("INSERT INTO ledger(ts,tenant,month,kind,n_tasks,est_usd,actual_usd,note) VALUES(?,?,?,?,?,?,?,?)",
                        (ts, self.tenant, ts[:7], kind, n_tasks, est_usd, actual_usd, note))
        self.db.commit()

    def month_spend(self, month):
        """Conservative: per row, the larger of estimate and actual."""
        r = self.db.execute("SELECT COALESCE(SUM(MAX(est_usd, COALESCE(actual_usd, 0))), 0) FROM ledger "
                            "WHERE tenant=? AND month=?", (self.tenant, month)).fetchone()
        return float(r[0])

    # --- queue ---
    def add_pending(self, task_id, keyword, location, device, ts=None):
        self.db.execute("INSERT OR IGNORE INTO pending(task_id,tenant,posted_at,keyword,location,device) VALUES(?,?,?,?,?,?)",
                        (task_id, self.tenant, ts or iso(), keyword, location, device))

    def pending(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM pending WHERE tenant=? ORDER BY posted_at", (self.tenant,))]

    def bump_pending(self, task_id):
        self.db.execute("UPDATE pending SET tries=tries+1 WHERE task_id=?", (task_id,))

    def drop_pending(self, task_id):
        self.db.execute("DELETE FROM pending WHERE task_id=?", (task_id,))

    # --- samples + observations ---
    def add_sample(self, task_id, ts, keyword, location, device, ads, check_url=None):
        """Record one SERP and its ads atomically; a re-collected task is ignored."""
        cur = self.db.execute("INSERT OR IGNORE INTO samples(task_id,ts,tenant,keyword,location,device,ads_count,check_url) "
                              "VALUES(?,?,?,?,?,?,?,?)", (task_id, ts, self.tenant, keyword, location, device, len(ads), check_url))
        if cur.rowcount:
            self.db.executemany(
                "INSERT INTO observations(task_id,tenant,ts,keyword,location,device,position,rank_absolute,top_of_page,"
                "domain,display_url,headline,description,landing_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(task_id, self.tenant, ts, keyword, location, device, a["ad_rank"], a["rank_absolute"], a["top_of_page"],
                  a["domain"], a["display_url"], a["headline"], a["description"], a["landing_url"]) for a in ads])
        self.db.commit()
        return bool(cur.rowcount)

    def commit(self):
        self.db.commit()

    def samples(self, since=None, until=None):
        q, args = "SELECT * FROM samples WHERE tenant=?", [self.tenant]
        if since:
            q, args = q + " AND ts>=?", args + [since]
        if until:
            q, args = q + " AND ts<?", args + [until]
        return [dict(r) for r in self.db.execute(q, args)]

    def observations(self, since=None, until=None):
        q, args = "SELECT * FROM observations WHERE tenant=?", [self.tenant]
        if since:
            q, args = q + " AND ts>=?", args + [since]
        if until:
            q, args = q + " AND ts<?", args + [until]
        return [dict(r) for r in self.db.execute(q + " ORDER BY ts", args)]

    # --- meta ---
    def get_meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (f"{self.tenant}:{k}",)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (f"{self.tenant}:{k}", str(v)))
        self.db.commit()


def selftest():
    n = 0
    s = Store(":memory:", "t1")
    s.ledger_add("task_post", 10, 0.006, 0.005, ts="2026-11-01T00:00:00Z")
    s.ledger_add("task_post", 10, 0.006, 0.009, ts="2026-11-02T00:00:00Z")
    s.ledger_add("task_post", 10, 0.006, None, ts="2026-10-31T00:00:00Z")
    assert abs(s.month_spend("2026-11") - 0.015) < 1e-9 and abs(s.month_spend("2026-10") - 0.006) < 1e-9; n += 1
    s.add_pending("a", "kw", "tampa", "mobile")
    s.commit()
    assert [p["task_id"] for p in s.pending()] == ["a"]; n += 1
    ad = {"ad_rank": 1, "rank_absolute": 1, "top_of_page": 1, "domain": "r.com", "display_url": "r.com",
          "headline": "H", "description": "D", "landing_url": "https://r.com"}
    assert s.add_sample("a", "2026-11-01T20:00:00Z", "kw", "tampa", "mobile", [ad, {**ad, "domain": "q.com"}]); n += 1
    assert not s.add_sample("a", "2026-11-01T20:00:00Z", "kw", "tampa", "mobile", [ad]); n += 1
    assert len(s.observations()) == 2 and len(s.samples()) == 1; n += 1
    assert s.observations(since="2026-11-02") == []; n += 1
    s.drop_pending("a"); s.commit()
    assert s.pending() == []; n += 1
    s.set_meta("cursor", 7)
    assert s.get_meta("cursor") == "7" and Store(":memory:", "t2").get_meta("cursor") is None; n += 1
    assert norm_ts("2026-11-01 20:00:00 +00:00", "x") == "2026-11-01T20:00:00Z" and norm_ts(None, "x") == "x"; n += 1
    return n


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(f"intel.store: {selftest()} checks")
        print("ALL PASS")
