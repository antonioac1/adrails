"""Collector: plan a budget-capped sample, post it to DataForSEO, collect results into SQLite.

    python3 -m adsops.intel.collector --config CFG estimate          # cost math, no network
    python3 -m adsops.intel.collector --config CFG plan              # what the next run would post, no network
    python3 -m adsops.intel.collector --config CFG run [--live]      # SPENDS (capped): posts tasks
    python3 -m adsops.intel.collector --config CFG collect           # free: fetch finished tasks
    python3 -m adsops.intel.collector --config CFG resolve-locations [--write]   # free: names -> location codes
    python3 -m adsops.intel.collector --config CFG status

Gates, in order, for every network command: credentials present (else "no credentials yet", exit 0); for
``run`` also ``collector_enabled: true`` in the config. The HARD monthly cap is checked before every post
(each <=100-task chunk, each live call) against the ledger, counting max(estimate, billed) per row.
Sampling rotates through keyword x location x device so a cap below full coverage still covers everything
over a cycle instead of the same first N combos every day.
"""
import argparse
import calendar
import json
import math
import sys
import time

from ..errors import ToolError, scrub
from . import config as C
from . import creds as K
from . import dataforseo as D
from .store import Store, iso, norm_ts

RUNS_PER_MONTH = {"daily": 30.4, "weekly": 4.35}
RESULT_TTL_S = 30 * 86400          # task_get is free for 30 days after posting
STALE_PROBE_S = 2 * 3600           # probe pending tasks not listed in tasks_ready after this long


def combos(cfg):
    return [(kw, loc["key"], dev) for kw in cfg["keywords"] for loc in cfg["locations"] for dev in cfg["devices"]]


def estimate(cfg):
    p = C.price(cfg)
    full = len(combos(cfg))
    runs = RUNS_PER_MONTH[cfg["cadence"]]
    full_month = full * p * runs
    per_run_tasks = min(full, int(cfg["monthly_cap_usd"] / runs / p))
    return {"price_per_task_usd": p, "combos": full, "runs_per_month": runs,
            "full_coverage_usd_month": round(full_month, 2), "cap_usd_month": cfg["monthly_cap_usd"],
            "tasks_per_run_under_cap": per_run_tasks,
            "coverage_per_run": round(per_run_tasks / full, 3) if full else 0,
            "days_per_full_cycle": (math.ceil(full / per_run_tasks) if per_run_tasks else None)
            if cfg["cadence"] == "daily" else None,
            "capped_usd_month": round(per_run_tasks * p * runs, 2)}


def remaining_tasks(cfg, store, now):
    p = C.price(cfg)
    left = cfg["monthly_cap_usd"] - store.month_spend(iso(now)[:7])
    return max(0, int((left + 1e-12) // p)), left


def plan(cfg, store, now):
    allc = combos(cfg)
    want = estimate(cfg)["tasks_per_run_under_cap"]
    can, _ = remaining_tasks(cfg, store, now)
    k = min(want, can, len(allc))
    cur = int(store.get_meta("cursor", 0)) % len(allc)
    return [allc[(cur + i) % len(allc)] for i in range(k)]


def _loc(cfg, key):
    return next(l for l in cfg["locations"] if l["key"] == key)


def run(cfg, client, store, now=None, live=False, log=print):
    now = now or time.time()
    if live:
        cfg = {**cfg, "mode": "live"}
    p = C.price(cfg)
    batch = plan(cfg, store, now)
    posted = 0
    if not batch:
        log(json.dumps({"event": "skip", "reason": "monthly cap reached or nothing to sample",
                        "month_spend_usd": round(store.month_spend(iso(now)[:7]), 4), "cap_usd": cfg["monthly_cap_usd"]}))
        return 0
    chunk = 1 if live else D.MAX_TASKS_PER_POST
    for i in range(0, len(batch), chunk):
        part = batch[i:i + chunk]
        can, left = remaining_tasks(cfg, store, now)          # HARD cap, re-checked before every call
        if can < len(part):
            part = part[:can]
        if not part:
            log(json.dumps({"event": "cap_stop", "left_usd": round(left, 4)}))
            break
        tasks = [D.build_task(cfg, kw, _loc(cfg, lk), dev, f"{cfg['tenant']}|{lk}|{dev}") for kw, lk, dev in part]
        est = len(part) * p
        try:
            resp = client.live_advanced(tasks[0]) if live else client.task_post(tasks)
        except ToolError as e:
            # Charged-or-not is unknown on a transport failure: book the estimate so the cap stays conservative.
            store.ledger_add("live" if live else "task_post", len(part), est, None, note="error: " + scrub(e, 160))
            log(json.dumps({"event": "post_error", "error": scrub(e, 200)}))
            break
        billed = resp.get("cost")
        store.ledger_add("live" if live else "task_post", len(part), est,
                         float(billed) if isinstance(billed, (int, float)) else None)
        for (kw, lk, dev), t in zip(part, resp.get("tasks") or []):
            if live and t.get("status_code") == D.OK_DONE and t.get("result"):
                meta, ads = D.parse_result(t["result"][0])
                store.add_sample(t["id"], norm_ts(meta["datetime"], iso(now)), kw, lk, dev, ads, meta["check_url"])
            elif not live and t.get("status_code") == D.OK_CREATED:
                store.add_pending(t["id"], kw, lk, dev, iso(now))
        store.commit()
        posted += len(part)
    store.set_meta("cursor", (int(store.get_meta("cursor", 0)) + posted) % len(combos(cfg)))
    log(json.dumps({"event": "posted", "tasks": posted, "mode": cfg["mode"],
                    "month_spend_usd": round(store.month_spend(iso(now)[:7]), 4), "cap_usd": cfg["monthly_cap_usd"]}))
    return posted


def collect(cfg, client, store, now=None, log=print):
    now = now or time.time()
    pend = store.pending()
    if not pend:
        log(json.dumps({"event": "collect", "pending": 0}))
        return 0
    ready = set()
    try:
        for t in client.tasks_ready().get("tasks") or []:
            ready.update(r.get("id") for r in (t.get("result") or []))
    except ToolError as e:
        log(json.dumps({"event": "tasks_ready_error", "error": scrub(e, 200)}))
    got = failed = waiting = 0
    for pt in pend:
        age = now - calendar.timegm(time.strptime(pt["posted_at"], "%Y-%m-%dT%H:%M:%SZ"))
        if age > RESULT_TTL_S:
            store.drop_pending(pt["task_id"]); failed += 1
            continue
        if pt["task_id"] not in ready and age < STALE_PROBE_S:
            waiting += 1
            continue
        try:
            r = client.task_get_advanced(pt["task_id"])
        except ToolError as e:
            store.bump_pending(pt["task_id"]); waiting += 1
            log(json.dumps({"event": "task_get_error", "error": scrub(e, 200)}))
            continue
        t = (r.get("tasks") or [{}])[0]
        code = t.get("status_code")
        if code == D.OK_DONE and t.get("result"):
            meta, ads = D.parse_result(t["result"][0])
            store.add_sample(pt["task_id"], norm_ts(meta["datetime"], iso(now)), pt["keyword"], pt["location"],
                             pt["device"], ads, meta["check_url"])
            store.drop_pending(pt["task_id"]); got += 1
        elif code in D.PENDING_CODES:
            store.bump_pending(pt["task_id"]); waiting += 1
        else:
            store.drop_pending(pt["task_id"]); failed += 1
    store.commit()
    log(json.dumps({"event": "collect", "collected": got, "waiting": waiting, "failed": failed}))
    return got


def resolve_locations(cfg, client):
    """Match configured location_name values against the free locations list. Returns {key: code|None}."""
    rows = client.locations("us").get("tasks", [{}])[0].get("result") or []
    by_name = {r.get("location_name"): r.get("location_code") for r in rows}
    codes = set(by_name.values())
    out = {}
    for l in cfg["locations"]:
        c = l.get("location_code")
        out[l["key"]] = c if c in codes else by_name.get(l.get("location_name"))
    return out


def _loc_fingerprint(cfg):
    return json.dumps(sorted((l["key"], l.get("location_code")) for l in cfg["locations"]))


def _client(cfg, runner=None):
    kw = {"runner": runner} if runner else {}
    c, why = K.get(cfg, cache=lambda i: C.cache_item_id(cfg["_path"], i) if cfg.get("_path") else None, **kw)
    if not c:
        return None, why
    return D.Client(c.login, c.password), None


def main(argv=None, client_factory=None, log=print):
    ap = argparse.ArgumentParser(prog="adsops.intel.collector")
    ap.add_argument("cmd", nargs="?", choices=["estimate", "plan", "run", "collect", "resolve-locations", "status"])
    ap.add_argument("--config")
    ap.add_argument("--live", action="store_true", help="use live/advanced (one task per call, ~3.3x the price)")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print(f"intel.collector: {selftest()} checks")
        print("ALL PASS")
        return 0
    if not a.cmd or not a.config:
        ap.error("--config and a command are required")
    cfg = C.load(a.config)
    if a.cmd == "estimate":
        log(json.dumps(estimate(cfg), indent=1))
        return 0
    store = Store(cfg["db_path"], cfg["tenant"])
    now = time.time()
    if a.cmd == "plan":
        b = plan(cfg, store, now)
        log(json.dumps({"tasks": len(b), "first": b[:5], "estimate_usd": round(len(b) * C.price(cfg), 4)}))
        return 0
    if a.cmd == "status":
        log(json.dumps({"month_spend_usd": round(store.month_spend(iso(now)[:7]), 4), "cap_usd": cfg["monthly_cap_usd"],
                        "pending": len(store.pending()), "samples": len(store.samples()),
                        "collector_enabled": cfg["collector_enabled"]}))
        return 0
    client, why = (client_factory or _client)(cfg)
    if not client:
        log(f"{why}: nothing done")
        return 0
    if a.cmd == "run":
        if not cfg["collector_enabled"]:
            log("collector disabled in config (collector_enabled=false): nothing done")
            return 0
        if store.get_meta("locations_verified") != _loc_fingerprint(cfg):
            log("locations not verified against DataForSEO: run resolve-locations first (free): nothing done")
            return 0
        run(cfg, client, store, now, live=a.live, log=log)
    elif a.cmd == "collect":
        collect(cfg, client, store, now, log=log)
    elif a.cmd == "resolve-locations":
        res = resolve_locations(cfg, client)
        log(json.dumps(res))
        missing = [k for k, v in res.items() if not v]
        if missing:
            log(f"not found in DataForSEO locations: {missing}: fix location_name/location_code")
            return 1
        if a.write:
            with open(cfg["_path"], encoding="utf-8") as fh:
                raw = json.load(fh)
            for l in raw["locations"]:
                l["location_code"] = res[l["key"]]
            with open(cfg["_path"], "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2)
                fh.write("\n")
            for l in cfg["locations"]:
                l["location_code"] = res[l["key"]]
        if all(res[l["key"]] == l.get("location_code") for l in cfg["locations"]):
            store.set_meta("locations_verified", _loc_fingerprint(cfg))
            log("locations verified")
        else:
            log("codes differ from config: re-run with --write to cache them, then they count as verified")
    return 0


class FakeDFS:
    """In-memory DataForSEO stand-in for selftests: records calls, bills $0.0006/task."""

    def __init__(self, ads_for=None, fail_post=False):
        self.calls, self.queue, self.fail_post = [], {}, fail_post
        self.ads_for = ads_for or (lambda kw, dev: [])
        self.n = 0

    def _result(self, task):
        items = []
        for k, (dom, top) in enumerate(self.ads_for(task["keyword"], task["device"])):
            items.append({"type": "paid", "rank_group": k + 1, "rank_absolute": k + 1 if top else 50 + k,
                          "domain": dom, "title": f"{dom} ad", "description": "d", "url": f"https://{dom}/",
                          "breadcrumb": dom})
        items.append({"type": "organic", "rank_group": 1, "rank_absolute": 10, "domain": "org.com"})
        return {"keyword": task["keyword"], "datetime": "2026-11-01 20:00:00 +00:00", "items": items}

    def task_post(self, tasks):
        self.calls.append(("task_post", len(tasks)))
        if self.fail_post:
            raise ToolError("dataforseo /task_post: HTTP 500")
        out = []
        for t in tasks:
            self.n += 1
            tid = f"t{self.n}"
            self.queue[tid] = t
            out.append({"id": tid, "status_code": D.OK_CREATED, "cost": 0.0006})
        return {"status_code": 20000, "cost": round(0.0006 * len(tasks), 6), "tasks": out}

    def live_advanced(self, task):
        self.calls.append(("live", 1))
        self.n += 1
        return {"status_code": 20000, "cost": 0.002,
                "tasks": [{"id": f"L{self.n}", "status_code": D.OK_DONE, "result": [self._result(task)]}]}

    def tasks_ready(self):
        self.calls.append(("tasks_ready", 0))
        return {"tasks": [{"result": [{"id": i} for i in self.queue]}]}

    def task_get_advanced(self, tid):
        self.calls.append(("task_get", 0))
        t = self.queue.pop(tid, None)
        if t is None:
            return {"tasks": [{"id": tid, "status_code": 40400}]}
        return {"tasks": [{"id": tid, "status_code": D.OK_DONE, "result": [self._result(t)]}]}

    def locations(self, country="us"):
        return {"tasks": [{"result": [{"location_name": "Tampa,Florida,United States", "location_code": 1015116}]}]}


def selftest():
    n = 0
    base = C.validate(C.sample(keywords=[f"k{i}" for i in range(50)],
                               locations=[{"key": "a", "location_name": "Tampa,Florida,United States"},
                                          {"key": "b", "location_code": 99}]))
    base["db_path"] = ":memory:"
    e = estimate(base)          # 50 x 2 x 2 = 200 combos; $10/30.4 = $0.329/run -> 548 tasks > 200
    assert e["combos"] == 200 and e["tasks_per_run_under_cap"] == 200 and e["coverage_per_run"] == 1.0; n += 1
    assert abs(e["full_coverage_usd_month"] - 200 * 0.0006 * 30.4) < 0.01; n += 1
    tight = {**base, "monthly_cap_usd": 1.0}  # $1/30.4/0.0006 = 54 tasks/run
    assert estimate(tight)["tasks_per_run_under_cap"] == 54 and estimate(tight)["days_per_full_cycle"] == 4; n += 1

    s = Store(":memory:", "t1")
    now = calendar.timegm(time.strptime("2026-11-01T12:00:00", "%Y-%m-%dT%H:%M:%S"))
    f = FakeDFS(ads_for=lambda kw, dev: [("rival.com", True), ("example.com", kw.endswith("0"))])
    logs = []
    assert run(tight, f, s, now, log=logs.append) == 54; n += 1
    assert [c for c in f.calls if c[0] == "task_post"] == [("task_post", 54)]; n += 1
    assert len(s.pending()) == 54 and abs(s.month_spend("2026-11") - 54 * 0.0006) < 1e-9; n += 1
    assert plan(tight, s, now)[0] == combos(tight)[54]; n += 1           # rotation cursor advanced
    assert collect(tight, f, s, now + 60, log=logs.append) == 54 and s.pending() == []; n += 1
    obs = s.observations()
    assert len(s.samples()) == 54 and {o["domain"] for o in obs} == {"rival.com", "example.com"}; n += 1
    assert all(o["top_of_page"] == 1 for o in obs if o["domain"] == "rival.com"); n += 1

    # HARD cap: ledger already at the cap -> nothing posted, no network call
    s2 = Store(":memory:", "t1")
    s2.ledger_add("task_post", 1, 0.9999, 1.0, ts=iso(now))
    f2 = FakeDFS()
    assert run(tight, f2, s2, now, log=logs.append) == 0 and f2.calls == []; n += 1
    # partial room: cap leaves 10 tasks -> exactly 10 posted
    s3 = Store(":memory:", "t1")
    s3.ledger_add("task_post", 1, 1.0 - 10 * 0.0006, None, ts=iso(now))
    assert run(tight, FakeDFS(), s3, now, log=logs.append) == 10; n += 1
    # big batch is chunked at 100 per post and still capped
    big = {**base, "monthly_cap_usd": 500.0}
    f4 = FakeDFS()
    assert run(big, f4, Store(":memory:", "t1"), now, log=logs.append) == 200; n += 1
    assert [c[1] for c in f4.calls] == [100, 100]; n += 1
    # transport error books the estimate (conservative) and stops
    s5 = Store(":memory:", "t1")
    assert run(tight, FakeDFS(fail_post=True), s5, now, log=logs.append) == 0; n += 1
    assert abs(s5.month_spend("2026-11") - 54 * 0.0006) < 1e-9; n += 1
    # live mode: one task per call at the live price, results stored immediately
    s6 = Store(":memory:", "t1")
    f6 = FakeDFS(ads_for=lambda kw, dev: [("rival.com", True)])
    k = run({**base, "monthly_cap_usd": 0.3045}, f6, s6, now, live=True, log=logs.append)
    assert k == 5 and len(s6.samples()) == 5 and all(c == ("live", 1) for c in f6.calls); n += 1
    assert abs(s6.month_spend("2026-11") - 5 * 0.002) < 1e-9; n += 1
    assert resolve_locations(base, FakeDFS()) == {"a": 1015116, "b": None}; n += 1   # 99 unknown to DFS

    # CLI gates: no credentials -> exit 0, nothing done; disabled -> nothing done even with a client
    import os
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.json")
    with open(p, "w") as fh:
        json.dump({**C.sample(), "db_path": os.path.join(d, "x.sqlite")}, fh)
    out = []
    assert main(["--config", p, "run"], client_factory=lambda c: (None, K.NO_CREDS), log=out.append) == 0
    assert out == ["no credentials yet: nothing done"]; n += 1
    f7 = FakeDFS()
    out = []
    assert main(["--config", p, "run"], client_factory=lambda c: (f7, None), log=out.append) == 0
    assert f7.calls == [] and "disabled" in out[0]; n += 1
    with open(p) as fh:
        raw = json.load(fh)
    raw["collector_enabled"] = True
    with open(p, "w") as fh:
        json.dump(raw, fh)
    out = []
    main(["--config", p, "run"], client_factory=lambda c: (f7, None), log=out.append)
    assert f7.calls == [] and "not verified" in out[0]; n += 1
    out = []
    assert main(["--config", p, "resolve-locations"], client_factory=lambda c: (f7, None), log=out.append) == 0
    assert out[-1].startswith("codes differ"); n += 1     # name matched, code not cached yet
    out = []
    main(["--config", p, "resolve-locations", "--write"], client_factory=lambda c: (f7, None), log=out.append)
    assert out[-1] == "locations verified" and C.load(p)["locations"][0]["location_code"] == 1015116; n += 1
    out = []
    main(["--config", p, "run"], client_factory=lambda c: (f7, None), log=out.append)
    assert json.loads(out[-1])["event"] == "posted" and f7.calls[0][0] == "task_post"; n += 1
    out = []
    main(["--config", p, "estimate"], log=out.append)
    assert json.loads(out[0])["combos"] == 2 * 1 * 2; n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
