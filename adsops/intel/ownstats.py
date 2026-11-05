"""Our own competitive metrics, read from the Google Ads API (READ-ONLY: only GAQL ``search`` is ever called).

Per snapshot (window = ``lookback_days`` ending yesterday, keyed by the window end date) it stores:
  * ad group level: impressions, search_impression_share, search_top_impression_share,
    search_absolute_top_impression_share, search_rank_lost_impression_share;
  * keyword level (keyword_view): the same four shares plus quality score and its three components
    (creative_quality_score = ad relevance, post_click_quality_score = landing page experience,
    search_predicted_ctr = expected CTR). Quality info has no date dimension, so it is a second query joined by
    ad group + criterion id;
  * campaign level: search_budget_lost_impression_share (+ impression share, rank lost, cost).
Google reports impression shares under 10% as 0.0999; ``share_label`` renders that as "<10%".
Quality score 0 means "not enough data" and is stored as NULL.

Client: the existing adsops one (``adsops.tools.Context`` + ``auth.from_config`` + ``GoogleAdsClient``) built from
the adsops deployment config named in ``ownstats.adsops_config``. No credentials are added here.

    python3 -m adsops.intel.ownstats snapshot --config CFG [--dry-run]
    python3 -m adsops.intel.ownstats show --config CFG
"""
import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import config as C
from .store import Store, iso

FLOOR = 0.0999

SCHEMA = """
CREATE TABLE IF NOT EXISTS os_campaign (
  tenant TEXT NOT NULL, snap_date TEXT NOT NULL, window_start TEXT NOT NULL, campaign TEXT NOT NULL,
  impressions INTEGER, impression_share REAL, rank_lost_is REAL, budget_lost_is REAL, cost_usd REAL,
  fetched_at TEXT NOT NULL, PRIMARY KEY (tenant, snap_date, campaign));
CREATE TABLE IF NOT EXISTS os_adgroup (
  tenant TEXT NOT NULL, snap_date TEXT NOT NULL, window_start TEXT NOT NULL, campaign TEXT NOT NULL,
  ad_group TEXT NOT NULL, impressions INTEGER, impression_share REAL, top_is REAL, abs_top_is REAL,
  rank_lost_is REAL, fetched_at TEXT NOT NULL, PRIMARY KEY (tenant, snap_date, campaign, ad_group));
CREATE TABLE IF NOT EXISTS os_keyword (
  tenant TEXT NOT NULL, snap_date TEXT NOT NULL, window_start TEXT NOT NULL, campaign TEXT NOT NULL,
  ad_group TEXT NOT NULL, criterion_id TEXT NOT NULL, keyword TEXT, match_type TEXT, impressions INTEGER,
  impression_share REAL, top_is REAL, abs_top_is REAL, rank_lost_is REAL, quality_score INTEGER,
  creative_qs TEXT, landing_qs TEXT, predicted_ctr TEXT, fetched_at TEXT NOT NULL,
  PRIMARY KEY (tenant, snap_date, campaign, ad_group, criterion_id));
"""

_SAFE = re.compile(r"^[A-Za-z0-9_ .\-]+$")


def _camel(k):
    p = k.split("_")
    return p[0] + "".join(x.title() for x in p[1:])


def _get(d, *path):
    """Tolerant REST accessor: tries camelCase then snake_case at each level."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d[_camel(k)] if _camel(k) in d else d.get(k)
    return d


def _num(v, cast=float):
    try:
        return None if v is None or v == "" else cast(v)
    except (TypeError, ValueError):
        return None


def share_label(v):
    if v is None:
        return "-"
    return "<10%" if abs(v - FLOOR) < 1e-6 else f"{100 * v:.0f}%"


def window(now, days):
    end = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def _where(since, until, names, extra=""):
    w = f"segments.date BETWEEN '{since}' AND '{until}'"
    if names:
        for n in names:
            if not _SAFE.match(n):
                raise ValueError(f"unsafe campaign name {n!r}")
        w += " AND campaign.name IN (" + ",".join(f"'{n}'" for n in names) + ")"
    return w + extra


def queries(since, until, names):
    sh = ("metrics.search_impression_share, metrics.search_top_impression_share, "
          "metrics.search_absolute_top_impression_share, metrics.search_rank_lost_impression_share")
    return {
        "campaign": ("SELECT campaign.name, metrics.impressions, metrics.search_impression_share, "
                     "metrics.search_rank_lost_impression_share, metrics.search_budget_lost_impression_share, "
                     f"metrics.cost_micros FROM campaign WHERE {_where(since, until, names)}"),
        "ad_group": (f"SELECT campaign.name, ad_group.name, metrics.impressions, {sh} FROM ad_group "
                     f"WHERE {_where(since, until, names)}"),
        "keyword": (f"SELECT campaign.name, ad_group.id, ad_group.name, ad_group_criterion.criterion_id, "
                    f"ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
                    f"metrics.impressions, {sh} FROM keyword_view WHERE {_where(since, until, names)}"),
        "quality": ("SELECT campaign.name, ad_group.id, ad_group_criterion.criterion_id, "
                    "ad_group_criterion.quality_info.quality_score, "
                    "ad_group_criterion.quality_info.creative_quality_score, "
                    "ad_group_criterion.quality_info.post_click_quality_score, "
                    "ad_group_criterion.quality_info.search_predicted_ctr FROM keyword_view WHERE "
                    "ad_group_criterion.status != 'REMOVED'" + (
                        " AND campaign.name IN (" + ",".join(f"'{n}'" for n in names) + ")" if names else "")),
    }


def build_client(cfg):
    """Existing adsops client from the adsops deployment config (no new credentials)."""
    from .. import config as acfg
    from .. import tools
    o = cfg["ownstats"]
    if not o.get("adsops_config") or not o.get("account"):
        raise ValueError("ownstats.adsops_config and ownstats.account must be set")
    ac = acfg.load_config(o["adsops_config"])
    account = next((a for a in ac["accounts"] if a["key"] == o["account"]), None)
    if not account:
        raise ValueError(f"account {o['account']!r} not in {o['adsops_config']}")
    return tools.Context(ac).client_factory(account)


def snapshot(cfg, store, client, now=None):
    """Run the four read-only queries and store the snapshot. -> counts dict."""
    now = now or time.time()
    o = cfg["ownstats"]
    names = o["campaigns"] or None
    since, until = window(now, o["lookback_days"])
    q = queries(since, until, names)
    store.db.executescript(SCHEMA)
    db, t, ts = store.db, cfg["tenant"], iso(now)
    n = {"campaign": 0, "ad_group": 0, "keyword": 0}
    for r in client.search(q["campaign"]):
        db.execute("INSERT OR REPLACE INTO os_campaign VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (t, until, since, _get(r, "campaign", "name"), _num(_get(r, "metrics", "impressions"), int),
                    _num(_get(r, "metrics", "search_impression_share")),
                    _num(_get(r, "metrics", "search_rank_lost_impression_share")),
                    _num(_get(r, "metrics", "search_budget_lost_impression_share")),
                    (_num(_get(r, "metrics", "cost_micros"), int) or 0) / 1e6, ts))
        n["campaign"] += 1
    for r in client.search(q["ad_group"]):
        db.execute("INSERT OR REPLACE INTO os_adgroup VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (t, until, since, _get(r, "campaign", "name"), _get(r, "ad_group", "name"),
                    _num(_get(r, "metrics", "impressions"), int), _num(_get(r, "metrics", "search_impression_share")),
                    _num(_get(r, "metrics", "search_top_impression_share")),
                    _num(_get(r, "metrics", "search_absolute_top_impression_share")),
                    _num(_get(r, "metrics", "search_rank_lost_impression_share")), ts))
        n["ad_group"] += 1
    qual = {}
    for r in client.search(q["quality"]):
        key = (str(_get(r, "ad_group", "id")), str(_get(r, "ad_group_criterion", "criterion_id")))
        qi = _get(r, "ad_group_criterion", "quality_info") or {}
        qs = _num(qi.get("qualityScore", qi.get("quality_score")), int)
        norm = lambda v: None if v in (None, "UNSPECIFIED", "UNKNOWN") else v  # noqa: E731
        qual[key] = (qs or None, norm(qi.get("creativeQualityScore", qi.get("creative_quality_score"))),
                     norm(qi.get("postClickQualityScore", qi.get("post_click_quality_score"))),
                     norm(qi.get("searchPredictedCtr", qi.get("search_predicted_ctr"))))
    for r in client.search(q["keyword"]):
        cid = str(_get(r, "ad_group_criterion", "criterion_id"))
        qv = qual.get((str(_get(r, "ad_group", "id")), cid), (None, None, None, None))
        db.execute("INSERT OR REPLACE INTO os_keyword VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (t, until, since, _get(r, "campaign", "name"), _get(r, "ad_group", "name"), cid,
                    _get(r, "ad_group_criterion", "keyword", "text"),
                    _get(r, "ad_group_criterion", "keyword", "match_type"),
                    _num(_get(r, "metrics", "impressions"), int), _num(_get(r, "metrics", "search_impression_share")),
                    _num(_get(r, "metrics", "search_top_impression_share")),
                    _num(_get(r, "metrics", "search_absolute_top_impression_share")),
                    _num(_get(r, "metrics", "search_rank_lost_impression_share")), *qv, ts))
        n["keyword"] += 1
    db.commit()
    n["snap_date"], n["window_start"] = until, since
    return n


def snaps(db, tenant, table="os_adgroup", limit=8):
    if not db.execute("SELECT name FROM sqlite_master WHERE name=?", (table,)).fetchone():
        return []
    return [r[0] for r in db.execute(f"SELECT DISTINCT snap_date FROM {table} WHERE tenant=? ORDER BY snap_date DESC "
                                     "LIMIT ?", (tenant, limit))]


def trend(db, tenant, table, key_cols, value_col, limit=6):
    """{key tuple: [(snap_date, value) oldest->newest]} over the last ``limit`` snapshots."""
    dates = snaps(db, tenant, table, limit)
    out = {}
    if not dates:
        return out
    for r in db.execute(f"SELECT snap_date, {','.join(key_cols)}, {value_col} FROM {table} WHERE tenant=? AND "
                        f"snap_date IN ({','.join('?' * len(dates))}) ORDER BY snap_date", [tenant] + dates):
        out.setdefault(tuple(r[1:1 + len(key_cols)]), []).append((r[0], r[-1]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adsops.intel.ownstats")
    ap.add_argument("cmd", nargs="?", choices=["snapshot", "show"])
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true", help="print the GAQL, call nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print(f"intel.ownstats: {selftest()} checks")
        print("ALL PASS")
        return 0
    if not a.cmd or not a.config:
        ap.error("command and --config are required")
    cfg = C.load(a.config)
    st = Store(cfg["db_path"], cfg["tenant"])
    if a.cmd == "snapshot":
        if a.dry_run:
            since, until = window(time.time(), cfg["ownstats"]["lookback_days"])
            for k, v in queries(since, until, cfg["ownstats"]["campaigns"] or None).items():
                print(f"[{k}] {v}")
            return 0
        print(snapshot(cfg, st, build_client(cfg)))
        return 0
    st.db.executescript(SCHEMA)
    for g, vals in trend(st.db, cfg["tenant"], "os_adgroup", ["ad_group"], "impression_share").items():
        print(g[0], " ".join(f"{d[5:]}:{share_label(v)}" for d, v in vals))
    return 0


class _FakeClient:
    """Read-only fake: has search() only; any mutate attempt is an AttributeError."""

    def __init__(self, data):
        self.data, self.seen = data, []

    def search(self, gaql):
        self.seen.append(gaql)
        for k in ("FROM campaign", "FROM ad_group", "quality_info", "FROM keyword_view"):
            if k in gaql and not (k == "FROM keyword_view" and "quality_info" in gaql):
                return self.data[k]
        raise AssertionError("unexpected GAQL " + gaql)


def selftest():
    n = 0
    assert share_label(0.0999) == "<10%" and share_label(0.336) == "34%" and share_label(None) == "-"; n += 1
    assert window(datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp(), 7) == ("2026-10-26", "2026-11-01"); n += 1
    q = queries("2026-10-26", "2026-11-01", ["acme_search"])
    assert "campaign.name IN ('acme_search')" in q["ad_group"] and "search_rank_lost_impression_share" in q["ad_group"] \
        and "search_budget_lost_impression_share" in q["campaign"] and "post_click_quality_score" in q["quality"]; n += 1
    for bad in ("x'; DROP", "a\nb"):
        try:
            queries("a", "b", [bad])
        except ValueError:
            n += 1
        else:
            raise AssertionError("unsafe name accepted")
    assert not any(w in " ".join(q.values()) for w in ("mutate", "UPDATE", "CREATE")); n += 1

    def row(camp, ag=None, **m):
        r = {"campaign": {"name": camp}, "metrics": m}
        if ag:
            r["adGroup"] = {"name": ag, "id": "77"}
        return r
    data = {
        "FROM campaign": [row("acme_search", impressions="1136", searchImpressionShare=0.173,
                              searchRankLostImpressionShare=0.4, searchBudgetLostImpressionShare=0.317,
                              costMicros="152995119")],
        "FROM ad_group": [row("acme_search", "owner_intent", impressions="422", searchImpressionShare=0.135,
                              searchTopImpressionShare=0.1027, searchAbsoluteTopImpressionShare=0.0999,
                              searchRankLostImpressionShare=0.592)],
        "quality_info": [{"campaign": {"name": "acme_search"}, "adGroup": {"id": "77"},
                          "adGroupCriterion": {"criterionId": "5", "qualityInfo": {
                              "qualityScore": 5, "creativeQualityScore": "ABOVE_AVERAGE",
                              "postClickQualityScore": "BELOW_AVERAGE", "searchPredictedCtr": "AVERAGE"}}},
                         {"campaign": {"name": "acme_search"}, "adGroup": {"id": "77"},
                          "adGroupCriterion": {"criterionId": "6", "qualityInfo": {
                              "qualityScore": 0, "creativeQualityScore": "UNSPECIFIED"}}}],
        "FROM keyword_view": [{"campaign": {"name": "acme_search"}, "adGroup": {"name": "owner_intent", "id": "77"},
                               "adGroupCriterion": {"criterionId": "5", "keyword": {"text": "tampa property management",
                                                                                     "matchType": "PHRASE"}},
                               "metrics": {"impressions": "10", "searchImpressionShare": 0.333,
                                           "searchRankLostImpressionShare": 0.42}},
                              {"campaign": {"name": "acme_search"}, "adGroup": {"name": "owner_intent", "id": "77"},
                               "adGroupCriterion": {"criterionId": "6", "keyword": {"text": "x y", "matchType": "EXACT"}},
                               "metrics": {"impressions": "3", "searchImpressionShare": 0.0999}}]}
    cfg = C.validate(C.sample(ownstats={"campaigns": ["acme_search"], "lookback_days": 7}), ".")
    st = Store(":memory:", "t1")
    cl = _FakeClient(data)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp()
    r = snapshot(cfg, st, cl, now)
    assert r["campaign"] == 1 and r["ad_group"] == 1 and r["keyword"] == 2 and r["snap_date"] == "2026-11-01"; n += 1
    assert len(cl.seen) == 4 and all(s.lstrip().startswith("SELECT") for s in cl.seen); n += 1
    c = st.db.execute("SELECT * FROM os_campaign").fetchone()
    assert abs(c["budget_lost_is"] - 0.317) < 1e-9 and abs(c["cost_usd"] - 152.995119) < 1e-6; n += 1
    k = {r["keyword"]: r for r in st.db.execute("SELECT * FROM os_keyword")}
    assert k["tampa property management"]["quality_score"] == 5 and k["tampa property management"]["landing_qs"] == "BELOW_AVERAGE" \
        and k["tampa property management"]["predicted_ctr"] == "AVERAGE" and k["tampa property management"]["creative_qs"] == "ABOVE_AVERAGE"; n += 1
    assert k["x y"]["quality_score"] is None and k["x y"]["creative_qs"] is None and k["x y"]["impression_share"] == 0.0999; n += 1
    ag = st.db.execute("SELECT * FROM os_adgroup").fetchone()
    assert ag["abs_top_is"] == 0.0999 and ag["rank_lost_is"] == 0.592 and ag["impressions"] == 422; n += 1
    snapshot(cfg, st, _FakeClient(data), now)  # idempotent within a day
    assert st.db.execute("SELECT COUNT(*) FROM os_keyword").fetchone()[0] == 2; n += 1
    data["FROM ad_group"][0]["metrics"]["searchImpressionShare"] = 0.2
    snapshot(cfg, st, _FakeClient(data), now + 7 * 86400)
    tr = trend(st.db, "t1", "os_adgroup", ["ad_group"], "impression_share")
    assert tr[("owner_intent",)] == [("2026-11-01", 0.135), ("2026-11-08", 0.2)]; n += 1
    assert trend(Store(":memory:", "t1").db, "t1", "os_adgroup", ["ad_group"], "impression_share") == {}; n += 1
    try:
        build_client(C.validate(C.sample(), "."))
    except ValueError:
        n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
