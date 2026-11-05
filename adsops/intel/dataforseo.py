"""DataForSEO SERP API client (Google organic, advanced): the licensed source of paid results.

Endpoints (docs fetched 2026-11-01; see README.md for the field list and source URLs):
  POST /v3/serp/google/organic/task_post               standard queue, <=100 tasks per call, charged on post
  GET  /v3/serp/google/organic/tasks_ready             free, <=20 calls/min, completed tasks from the last 3 days
  GET  /v3/serp/google/organic/task_get/advanced/{id}  free for 30 days after posting
  POST /v3/serp/google/organic/live/advanced           one task per call, charged per call (behind --live)
  GET  /v3/serp/google/locations/{country}             free location list
Auth: HTTP Basic (login:password). The transport is injectable; the selftest never touches the network.
"""
import base64
import json
import sys
import urllib.error
import urllib.request

from ..errors import ToolError, scrub

BASE = "https://api.dataforseo.com/v3"
MAX_TASKS_PER_POST = 100
OK_DONE = 20000
OK_CREATED = 20100
# 404xx/406xx with these codes mean "not ready yet" rather than "failed".
PENDING_CODES = {40601, 40602, 40400}


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Client:
    def __init__(self, login, password, transport=urllib_transport, timeout=60, base=BASE):
        tok = base64.b64encode(f"{login}:{password}".encode()).decode()
        self._headers = {"Authorization": "Basic " + tok, "Content-Type": "application/json"}
        self.transport, self.timeout, self.base = transport, timeout, base

    def __repr__(self):
        return "Client(<credentials redacted>)"

    def _call(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.transport(method, self.base + path, dict(self._headers), body, self.timeout)
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            raise ToolError(f"dataforseo {path}: HTTP {status}, non-JSON body")
        if status != 200 or data.get("status_code") != OK_DONE:
            raise ToolError(scrub(f"dataforseo {path}: HTTP {status} {data.get('status_code')} "
                                  f"{data.get('status_message')}", 300))
        return data

    def task_post(self, tasks):
        if not 1 <= len(tasks) <= MAX_TASKS_PER_POST:
            raise ToolError(f"task_post takes 1..{MAX_TASKS_PER_POST} tasks, got {len(tasks)}")
        return self._call("POST", "/serp/google/organic/task_post", tasks)

    def tasks_ready(self):
        return self._call("GET", "/serp/google/organic/tasks_ready")

    def task_get_advanced(self, task_id):
        if not all(c.isalnum() or c == "-" for c in task_id):
            raise ToolError("bad task id")
        return self._call("GET", f"/serp/google/organic/task_get/advanced/{task_id}")

    def live_advanced(self, task):
        return self._call("POST", "/serp/google/organic/live/advanced", [task])

    def locations(self, country="us"):
        return self._call("GET", f"/serp/google/locations/{country}")


def build_task(cfg, keyword, loc, device, tag):
    t = {"keyword": keyword, "language_code": cfg["language_code"], "device": device,
         "depth": cfg["depth"], "tag": tag[:255]}
    if loc.get("location_code"):
        t["location_code"] = loc["location_code"]
    else:
        t["location_name"] = loc["location_name"]
    if cfg["mode"] == "priority":
        t["priority"] = 2
    return t


def _domain(d):
    d = (d or "").lower().strip()
    return d[4:] if d.startswith("www.") else d


def parse_result(res):
    """One ``tasks[].result[]`` object -> (meta, [ad rows]).

    ``ad_rank`` = paid ``rank_group`` (1 = first ad on the page). ``top_of_page`` is INFERRED: a paid item whose
    ``rank_absolute`` precedes the first organic item. DataForSEO documents no explicit top/bottom field.
    """
    items = [i for i in (res.get("items") or []) if isinstance(i, dict)]
    organic = [i.get("rank_absolute") for i in items if i.get("type") == "organic" and i.get("rank_absolute")]
    first_org = min(organic) if organic else None
    ads = []
    for i in items:
        if i.get("type") != "paid":
            continue
        ra = i.get("rank_absolute")
        ads.append({
            "ad_rank": i.get("rank_group"),
            "rank_absolute": ra,
            "top_of_page": 1 if (first_org is None or (ra is not None and ra < first_org)) else 0,
            "domain": _domain(i.get("domain")),
            "display_url": i.get("breadcrumb") or "",
            "headline": i.get("title") or "",
            "description": i.get("description") or "",
            "landing_url": i.get("url") or "",
        })
    meta = {"keyword": res.get("keyword"), "location_code": res.get("location_code"),
            "datetime": res.get("datetime"), "check_url": res.get("check_url"),
            "items_count": res.get("items_count"), "ads_count": len(ads)}
    return meta, ads


def _raises(fn):
    try:
        fn()
    except ToolError as e:
        return e
    return None


def selftest():
    n = 0
    sent = []

    def fake(method, url, headers, body, timeout):
        sent.append((method, url, headers, body))
        if url.endswith("/task_post"):
            tasks = json.loads(body)
            return 200, json.dumps({"status_code": 20000, "cost": 0.0006 * len(tasks),
                                    "tasks": [{"id": f"id-{k}", "status_code": OK_CREATED, "cost": 0.0006,
                                               "data": t} for k, t in enumerate(tasks)]}).encode()
        if url.endswith("/tasks_ready"):
            return 200, json.dumps({"status_code": 20000, "tasks": [{"result": [{"id": "id-0"}]}]}).encode()
        if "/task_get/advanced/" in url:
            return 200, json.dumps({"status_code": 20000, "tasks": [{"id": "id-0", "status_code": 20000,
                                                                     "result": [SAMPLE_RESULT]}]}).encode()
        return 401, json.dumps({"status_code": 40100, "status_message": "auth failed"}).encode()

    c = Client("me@x", "pw", transport=fake)
    assert "pw" not in repr(c); n += 1
    cfg = {"language_code": "en", "depth": 10, "mode": "standard"}
    t = build_task(cfg, "property management tampa", {"key": "tampa", "location_name": "Tampa,Florida,United States"},
                   "mobile", "bt|1")
    assert t["location_name"].startswith("Tampa") and "location_code" not in t and "priority" not in t; n += 1
    t2 = build_task({**cfg, "mode": "priority"}, "k", {"key": "x", "location_code": 1015116}, "desktop", "x")
    assert t2["location_code"] == 1015116 and t2["priority"] == 2; n += 1
    r = c.task_post([t])
    assert r["tasks"][0]["id"] == "id-0" and sent[-1][2]["Authorization"].startswith("Basic "); n += 1
    assert _raises(lambda: c.task_post([t] * 101)); n += 1
    assert c.tasks_ready()["tasks"][0]["result"][0]["id"] == "id-0"; n += 1
    g = c.task_get_advanced("id-0")
    meta, ads = parse_result(g["tasks"][0]["result"][0])
    assert meta["ads_count"] == 3 and [a["domain"] for a in ads] == ["rivalpm.com", "acmepm.example", "lowpm.com"]; n += 1
    assert [a["top_of_page"] for a in ads] == [1, 1, 0] and ads[0]["ad_rank"] == 1; n += 1
    assert ads[0]["headline"] == "Rival PM Tampa" and ads[0]["landing_url"].startswith("https://rivalpm.com"); n += 1
    e = _raises(c.locations)
    assert e and "40100" in str(e) and "pw" not in str(e); n += 1
    assert _raises(lambda: c.task_get_advanced("../etc")); n += 1
    assert parse_result({"items": None})[1] == []; n += 1
    return n


SAMPLE_RESULT = {
    "keyword": "property management tampa", "location_code": 1015116, "datetime": "2026-11-01 20:00:00 +00:00",
    "items_count": 6,
    "items": [
        {"type": "paid", "rank_group": 1, "rank_absolute": 1, "domain": "www.rivalpm.com", "title": "Rival PM Tampa",
         "description": "8% of rent.", "url": "https://rivalpm.com/lp?gclid=x", "breadcrumb": "rivalpm.com/tampa"},
        {"type": "paid", "rank_group": 2, "rank_absolute": 2, "domain": "acmepm.example",
         "title": "Tampa Property Management", "description": "$149/mo flat.", "url": "https://acmepm.example/owners/",
         "breadcrumb": "acmepm.example/owners/free-quote"},
        {"type": "organic", "rank_group": 1, "rank_absolute": 3, "domain": "x.com"},
        {"type": "local_pack", "rank_group": 1, "rank_absolute": 4},
        {"type": "organic", "rank_group": 2, "rank_absolute": 5, "domain": "y.com"},
        {"type": "paid", "rank_group": 3, "rank_absolute": 6, "domain": "lowpm.com", "title": "Low PM",
         "description": "Cheap.", "url": "https://lowpm.com/", "breadcrumb": "lowpm.com"},
    ],
}


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(f"intel.dataforseo: {selftest()} checks")
        print("ALL PASS")
