"""QA for the OpenAI (ChatGPT) Ads adapter. Stdlib only, no network: a fake Ads API answers every call.

    cd <repo root> && python3 tests/qa_openai_ads.py

Covers every write path (mocked HTTP), policy refusals, cap math incl. 2x overdelivery, the spend gate,
plan ids, commit gates, the pause-only guard (incl. a randomized never-activates property run),
guard-all heartbeat separation, retries/idempotency, and that the API key never leaks.
"""
import copy
import datetime as dt
import json
import os
import random
import subprocess
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from adsops import __main__ as main_mod, config, oai_engine, oai_policy, tools  # noqa: E402
from adsops.errors import RailViolation, ToolError  # noqa: E402
from adsops.platforms import openai_ads  # noqa: E402

KEY = "sk-adrails-CANARYKEY-9f8e7d6c5b4a39281706"
CAPI_KEY = "capi-CANARY-0011223344556677889900"
AID = "adacct_EXAMPLE0000000000000000000000"
NOW = dt.datetime(2026, 11, 6, 15, 0, tzinfo=dt.timezone.utc).timestamp()
CHECKS, FAILS = 0, []


def check(cond, what):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(what)
        print("FAIL:", what)


def raises(fn, needle="", exc=ToolError):
    try:
        fn()
    except exc as e:
        return needle.lower() in str(e).lower()
    return False


# ── fake OpenAI Ads API ──────────────────────────────────────────────────────────────────────
class FakeAPI:
    def __init__(self, tz="America/New_York"):
        self.acct = {"id": AID, "account_name": "Acme PM", "currency_code": "USD",
                     "timezone": tz, "status": "active", "review": {"status": "approved"}}
        self.store = {"campaign": {}, "ad_group": {}, "ad": {}}
        self.idem, self.requests, self.n = {}, [], 0
        self.spend_today, self.spend_life, self.account_spend = 0.0, 0.0, 0.0
        self.fail_next = []          # list of (status, body) returned before normal handling
        self.bulk_mode = "404"       # 404 | ok | fail
        self.pixels, self.events = [], []
        self.page_size = 500

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n:04d}"

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        path, q = u.path.replace("/v1", "", 1), urllib.parse.parse_qs(u.query)
        self.requests.append({"method": method, "path": path, "q": q, "headers": dict(headers),
                              "body": body if isinstance(body, bytes) and not body.startswith(b"{") else
                              (json.loads(body) if body else None)})
        if headers.get("Authorization") != "Bearer " + (CAPI_KEY if u.netloc == "bzr.openai.com" else KEY):
            return 401, {}, b'{"error": {"message": "bad key"}}'
        # Live-API rule (verified 2026-11-06): insights time_ranges.end must be a full hour in the account timezone.
        if path.endswith("/insights"):
            for tr in q.get("time_ranges[]", []):
                end = json.loads(tr).get("end")
                if end is not None and int(end) % 3600 != 0:      # tests run America/New_York (whole-hour offset)
                    return 400, {}, json.dumps({"error": {"message": "time_ranges.end must be a full-hour unix timestamp"}}).encode()
        if self.fail_next:
            st, b = self.fail_next.pop(0)
            if isinstance(st, Exception):
                raise st
            return st, {"retry-after": "0"}, b
        js = json.loads(body) if body and body[:1] == b"{" else None
        return self.route(method, path, q, js, headers)

    def ok(self, obj, status=200):
        return status, {}, json.dumps(obj).encode()

    def listing(self, items, q):
        lim = min(int(q.get("limit", ["500"])[0]), self.page_size)
        after = q.get("after", [None])[0]
        ids = [i["id"] for i in items]
        start = ids.index(after) + 1 if after in ids else 0
        page = items[start:start + lim]
        return self.ok({"object": "list", "data": page, "has_more": start + lim < len(items),
                        "first_id": page[0]["id"] if page else None, "last_id": page[-1]["id"] if page else None})

    def route(self, m, path, q, js, headers):
        parts = path.strip("/").split("/")
        kinds = {"campaigns": "campaign", "ad_groups": "ad_group", "ads": "ad"}
        if path == "/ad_account":
            return self.ok(self.acct)
        if path == "/ad_account/insights":
            return self.ok({"object": "list", "data": [{"spend": self.account_spend}], "has_more": False})
        if path == "/ad_account/brand":
            self.acct.update({k: v for k, v in js.items() if k != "favicon_file_id"})
            self.acct["review"] = {"status": "in_review"}
            return self.ok(self.acct)
        if path == "/geo_lookup/search":
            return self.ok({"count": 1, "results": [{"id": "3000077", "type": "market", "name": "Tampa - St. Petersburg"}]})
        if path == "/upload":
            return self.ok({"file_id": self._id("file")})
        if path == "/conversions/pixels":
            if m == "GET":
                return self.listing(self.pixels, q)
            p = {"id": self._id("clidsrc"), "name": js["name"], "client_type": "web", "pixel_id": "1345"}
            self.pixels.append(p)
            return self.ok(p)
        if path == "/conversions/event_settings":
            if m == "GET":
                return self.listing(self.events, q)
            e = {"id": self._id("ces"), **js, "archived": False}
            self.events.append(e)
            return self.ok(e)
        if path == "/conversions/api_keys":
            return self.ok({"name": js["name"], "api_key": CAPI_KEY})
        if path == "/events":
            assert js["validate_only"] is True
            return self.ok({"validated": len(js["events"])})
        if parts[0] == "bulk_mutation_jobs":
            if self.bulk_mode == "404":
                return 404, {}, b'{"error": {"message": "Not found"}}'
            if m == "POST":
                self.bulk = js
                return self.ok({"id": "blkmtnjob_1", "status": "completed" if self.bulk_mode == "ok" else "failed"}, 202)
            if len(parts) == 2:
                return self.ok({"id": "blkmtnjob_1", "status": "completed"})
            st = "validated" if self.bulk_mode == "ok" else "failed"
            return self.ok({"object": "list", "has_more": False, "complete": True,
                            "data": [{"operation_id": o["operation_id"], "status": st, "error_code": None if st == "validated"
                                      else "invalid", "error": None} for o in self.bulk["operations"]]})
        kind = kinds.get(parts[0])
        if not kind:
            return 404, {}, b'{"error": {"message": "Not found"}}'
        store = self.store[kind]
        if len(parts) == 1 and m == "GET":
            items = list(store.values())
            if "name" in q:
                items = [i for i in items if i["name"] == q["name"][0]]
            if "campaign_id" in q:
                items = [i for i in items if i.get("campaign_id") == q["campaign_id"][0]]
            if "ad_group_id" in q:
                items = [i for i in items if i.get("ad_group_id") == q["ad_group_id"][0]]
            return self.listing(items, q)
        if len(parts) == 1 and m == "POST":
            ik = headers.get("Idempotency-Key")
            if ik in self.idem:
                return self.ok(store[self.idem[ik]])
            obj = {"id": self._id({"campaign": "cmpn", "ad_group": "adgrp", "ad": "ad"}[kind]),
                   "created_at": int(NOW) - 86400 * 3, **copy.deepcopy(js)}
            if kind == "ad":
                obj["review_status"] = "in_review"
            store[obj["id"]] = obj
            self.idem[ik] = obj["id"]
            return self.ok(obj)
        rid = parts[1]
        if rid not in store:
            return 404, {}, b'{"error": {"message": "Not found"}}'
        obj = store[rid]
        if len(parts) == 2 and m == "GET":
            return self.ok(obj)
        if len(parts) == 2 and m == "POST":
            obj.update(copy.deepcopy(js))
            return self.ok(obj)
        if parts[2] in ("activate", "pause", "archive"):
            obj["status"] = {"activate": "active", "pause": "paused", "archive": "archived"}[parts[2]]
            return self.ok(obj)
        if parts[2] == "insights":
            rng = json.loads(q["time_ranges[]"][0])
            midnight = openai_ads.local_midnight(int(NOW), self.acct["timezone"])
            spend = self.spend_today if rng["start"] >= midnight else self.spend_life
            return self.ok({"object": "list", "data": [{"campaign_id": rid, "spend": spend}], "has_more": False})
        return 404, {}, b'{"error": {"message": "Not found"}}'

    def writes(self):
        return [r for r in self.requests if r["method"] == "POST" and r["path"] not in ("/events",)]


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────
def policy(**over):
    p = {"platform": "openai_ads", "ad_account_id": AID, "campaign_key": "bt_chatgpt_owners", "currency": "USD",
         "caps": {"max_daily_budget_usd": 40, "max_lifetime_budget_usd": 500, "max_cumulative_spend_usd": 500,
                  "max_bid_usd": 5, "max_end_date": "2026-11-30", "max_ops_per_run": 25, "overdelivery_factor": 2.0,
                  "min_daily_budget_usd": 5},
         "guard": {"pause_headroom_usd": 50, "daily_anomaly_multiple": 1.5},
         "allowed_domains": ["acmepm.example"],
         "required_utm": {"utm_source": "chatgpt", "utm_medium": "paid", "utm_campaign": "{campaign_key}"},
         "allow_activate": True, "allow_archive": False, "approvers": ["Jane Approver"],
         "approval_max_age_days": 14, "require_plan_id": True, "plan_ttl_s": 900, "max_guard_age_min": 30}
    for k, v in over.items():
        if k in ("caps", "guard"):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


URL = "https://acmepm.example/owners/?utm_source=chatgpt&utm_medium=paid&utm_campaign=bt_chatgpt_owners"


def state(**over):
    s = {"platform": "openai_ads", "ad_account_id": AID, "campaign_key": "bt_chatgpt_owners",
         "campaign": {"id": None, "name": "Acme owners - Tampa Bay", "status": "paused", "bidding_type": "clicks",
                      "budget": {"daily_usd": 36}, "end_date": "2026-11-19",
                      "targeting": {"countries": [], "location_ids": ["3000077"], "platforms": None}},
         "ad_groups": [{"key": "owners", "id": None, "name": "Owners - flat fee", "status": "paused",
                        "bidding": {"strategy": "fixed_bid", "max_bid_usd": 5},
                        "context_hints": ["Tampa property management cost"],
                        "ads": [{"key": "flat149", "id": None, "name": "Flat 149", "status": "paused",
                                 "title": "Tampa property management, $149/mo", "body": "Never a % of rent.",
                                 "target_url": URL, "file_id": "file_img1"}]}],
         "approval": {"activate": False}}
    for k, v in over.items():
        s[k] = v
    return s


APPROVAL = {"activate": True, "campaign_name": "Acme owners - Tampa Bay", "approved_by": "Jane Approver", "approved_on": "2026-11-05", "expires_on": "2026-11-19",
            "max_daily_budget_usd": 36, "max_bid_usd": 5, "max_total_spend_usd": 500}


class Env:
    def __init__(self, st=None, pol=None, mode="live", allow_commit=True, bulk=False, heartbeat=True):
        self.td = tempfile.mkdtemp(prefix="qa-oai-")
        self.api = FakeAPI()
        self.sf, self.pf = os.path.join(self.td, "state.json"), os.path.join(self.td, "policy.json")
        self.hb = os.path.join(self.td, "oai.heartbeat")
        self.ghb = os.path.join(self.td, "google.heartbeat")
        subprocess.run(["git", "init", "-q", self.td], check=True)
        for a in (["config", "user.email", "qa@x"], ["config", "user.name", "qa"]):
            subprocess.run(["git", "-C", self.td] + a, check=True)
        self.write(st or state(), pol or policy())
        if heartbeat:
            open(self.hb, "w").write("x\n")
            os.utime(self.hb, (NOW - 60, NOW - 60))
        self.cfg_raw = {
            "tenant": "qa", "meter_log": os.path.join(self.td, "meter.jsonl"),
            "audit_log": os.path.join(self.td, "audit.jsonl"), "plan_ledger": os.path.join(self.td, "plans.jsonl"),
            "lock_dir": os.path.join(self.td, "locks"), "guard_heartbeat": self.ghb,
            "commit_policy": {"require_clean_git": True, "allow_commit": allow_commit},
            "accounts": [{"key": "acme_openai", "platform": "openai_ads", "ad_account_id": AID, "mode": mode,
                          "bulk_validate": bulk, "guard_heartbeat": self.hb,
                          "capi_key_file": os.path.join(self.td, "capi.key"),
                          "credentials": {"type": "api_key", "sources": [{"kind": "env", "name": "QA_OAI_KEY"}]},
                          "campaigns": [{"key": "bt_chatgpt_owners", "state_file": self.sf, "policy_file": self.pf}]}]}
        self.reload()

    def reload(self):
        p = os.path.join(self.td, "config.json")
        json.dump(self.cfg_raw, open(p, "w"))
        self.cfg = config.load_config(p)
        self.client = openai_ads.OpenAIAdsClient(lambda: KEY, http=self.api, sleep=lambda s: None)
        self.srv = tools.build_server(self.cfg, now=lambda: NOW, oai_client_factory=lambda a: self.client)
        self.oai_capi = None

    def write(self, st=None, pol=None, commit=True):
        if st is not None:
            json.dump(st, open(self.sf, "w"), indent=1)
        if pol is not None:
            json.dump(pol, open(self.pf, "w"), indent=1)
        if commit:
            subprocess.run(["git", "-C", self.td, "add", "-A"], check=True)
            subprocess.run(["git", "-C", self.td, "commit", "-qm", "x", "--allow-empty"], check=True)

    def call(self, name, **args):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        res = r["result"]
        text = "".join(c.get("text", "") for c in res.get("content", []))
        return (not res.get("isError")), res.get("structuredContent"), text

    def plan_apply(self):
        ok, p, t = self.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
        assert ok, t
        if not p["ops_count"]:
            return p, None
        return p, self.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])

    def adopt_ids(self):
        """Record created ids in the state file (what an operator does after the first commit)."""
        st = json.load(open(self.sf))
        c = next(iter(self.api.store["campaign"].values()))
        st["campaign"]["id"] = c["id"]
        g = next(iter(self.api.store["ad_group"].values()))
        st["ad_groups"][0]["id"] = g["id"]
        a = next(iter(self.api.store["ad"].values()))
        st["ad_groups"][0]["ads"][0]["id"] = a["id"]
        self.write(st)
        return st


os.environ["QA_OAI_KEY"] = KEY


def run():
    # 1. config validation ------------------------------------------------------------------
    e = Env()
    check(e.cfg["accounts"][0]["mode"] == "live", "config loads an openai_ads account")
    bad = copy.deepcopy(e.cfg_raw)
    bad["accounts"][0]["ad_account_id"] = "123"
    p = os.path.join(e.td, "bad.json")
    json.dump(bad, open(p, "w"))
    check(raises(lambda: config.load_config(p), "adacct_"), "config refuses a malformed ad_account_id")
    bad = copy.deepcopy(e.cfg_raw)
    bad["accounts"][0]["mode"] = "yolo"
    json.dump(bad, open(p, "w"))
    check(raises(lambda: config.load_config(p), "mode"), "config refuses an unknown mode")
    bad = copy.deepcopy(e.cfg_raw)
    del bad["accounts"][0]["credentials"]
    json.dump(bad, open(p, "w"))
    check(raises(lambda: config.load_config(p), "credentials"), "live openai account without credentials refused")
    bad["accounts"][0]["mode"] = "disabled"
    json.dump(bad, open(p, "w"))
    check(config.load_config(p)["accounts"][0]["mode"] == "disabled", "disabled account may omit credentials")
    bad = copy.deepcopy(e.cfg_raw)
    del bad["accounts"][0]["campaigns"][0]["policy_file"]
    json.dump(bad, open(p, "w"))
    check(raises(lambda: config.load_config(p), "policy_file"), "openai campaign without policy_file refused")
    ok, _, t = e.call("adsops_status", campaign="bt_chatgpt_owners")
    check(not ok and "adsops_openai" in t, "google tools refuse an openai campaign key with a pointer")

    # 2. key loading --------------------------------------------------------------------------
    kf = os.path.join(e.td, "k.key")
    open(kf, "w").write(KEY + "\n")
    os.chmod(kf, 0o644)
    check(raises(lambda: openai_ads.key_from_file(kf)(), "world-readable"), "world-readable key file refused")
    os.chmod(kf, 0o600)
    check(openai_ads.key_from_file(kf)() == KEY, "key file read")

    class R:
        returncode, stdout, stderr = 1, "", "[ERROR] item not found; token=" + KEY

    def fake_op(args, **kw):
        assert kw["env"]["OP_SERVICE_ACCOUNT_TOKEN"] == "optoken"
        return R()
    tf = os.path.join(e.td, "op.token")
    open(tf, "w").write("optoken")
    ld = openai_ads.key_loader({"type": "api_key", "sources": [
        {"kind": "1password", "ref": "op://Vault/OpenAI Ads - Acme/credential", "token_file": tf},
        {"kind": "file", "path": kf}]}, runner=fake_op)
    check(ld() == KEY, "1Password failure falls back to the file source")
    R.returncode = 0
    R.stdout = KEY + "\n"
    check(openai_ads.key_from_1password("op://x/y/z", tf, runner=fake_op)() == KEY, "1Password ref read")
    R.returncode, R.stdout = 1, ""
    try:
        openai_ads.key_from_1password("op://x/y/z", tf, runner=fake_op)()
    except ToolError as ex:
        check(KEY not in str(ex), "1Password stderr containing the key is redacted")

    # 3. first plan + commit: creates, all paused, idempotency keys ---------------------------
    e = Env()
    ok, p, t = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(ok and p["ops_count"] == 3 and [o["op"] for o in p["ops"]] == ["create"] * 3, "plan: 3 creates " + t[:300])
    check(all(o["body"]["status"] == "paused" for o in p["ops"]), "every create is paused")
    check(p["spend_gate"] is False and p["exposure_increases"] == [], "paused creates add no exposure")
    ok, a, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners")
    check(ok and a["validated"] and not a["committed"] and not e.api.writes(), "validate-only apply writes nothing")
    check("not enabled" in a["bulk"], "bulk validate off unless configured")
    ok, a, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True)
    check(not ok and "plan_id required" in t, "commit without plan_id refused")
    ok, a, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id="deadbeef.00")
    check(not ok and "does not match" in t, "commit with a wrong plan_id refused")
    ok, a, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(ok and a["committed"] and len(a["res"]["created_ids"]) == 3, "commit creates campaign/ad group/ad " + t[:300])
    creates = [r for r in e.api.writes() if r["path"] in ("/campaigns", "/ad_groups", "/ads")]
    check(len(creates) == 3 and all(r["headers"].get("Idempotency-Key", "").startswith("adrails-") for r in creates),
          "every create carries an Idempotency-Key")
    check(all(r["body"]["status"] == "paused" for r in creates), "API saw status=paused on every create")
    check(creates[1]["body"]["campaign_id"] == a["res"]["created_ids"]["campaign"]
          and creates[2]["body"]["ad_group_id"] == a["res"]["created_ids"]["ag:owners"], "children wired to new parents")
    cb = creates[0]["body"]
    check(cb["budget"] == {"daily_spend_limit_micros": 36_000_000} and cb["bidding_type"] == "clicks", "micros budget")
    check(creates[1]["body"]["bidding_config"] == {"billing_event_type": "click", "strategy": "fixed_bid",
                                                   "max_bid_micros": 5_000_000}, "fixed bid in micros")
    end = dt.datetime.fromtimestamp(cb["end_time"], dt.timezone.utc)
    check(end == dt.datetime(2026, 11, 20, 5, 0, tzinfo=dt.timezone.utc), "end_date inclusive at NY midnight")
    ok, a2, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(ok and a2["ops_count"] == 0 and not a2["committed"], "re-apply after create is a no-op (name matching)")
    ok, p2, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(ok and p2["ops_count"] == 0 and "plan_id" not in p2, "plan converges to 0 ops")
    # replaying the same plan id is refused (ledger)
    e2 = Env()
    ok, p, _ = e2.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    e2.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    for o in list(e2.api.store["campaign"].values()):
        o["name"] = "renamed elsewhere"
    ok, _, t = e2.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(not ok, "a plan_id cannot be replayed")
    e2.api.idem.clear()

    # 4. updates: budget, bid, creative, targeting; then the spend gate -----------------------
    e.adopt_ids()
    st = json.load(open(e.sf))
    st["campaign"]["budget"] = {"daily_usd": 30}
    st["ad_groups"][0]["bidding"]["max_bid_usd"] = 4.5
    st["ad_groups"][0]["ads"][0]["body"] = "Never a percentage of rent."
    st["campaign"]["targeting"]["location_ids"] = ["3000077", "2000010"]
    e.write(st)
    p, (ok, a, t) = e.plan_apply()
    kinds = sorted((o["op"], o["kind"]) for o in p["ops"])
    check(ok and kinds == [("update", "ad"), ("update", "ad_group"), ("update", "campaign")], "three updates " + t[:200])
    ups = {r["path"]: r["body"] for r in e.api.writes()[-3:]}
    cid = st["campaign"]["id"]
    check(ups["/campaigns/" + cid]["budget"] == {"daily_spend_limit_micros": 30_000_000}, "budget update sent in micros")
    check("status" not in json.dumps(list(ups.values())), "updates never carry status")
    check(sorted(x["id"] for x in ups["/campaigns/" + cid]["targeting"]["locations"]["include"]) == ["2000010", "3000077"],
          "targeting update sends full location list")
    # activation without approval
    st["campaign"]["status"] = st["ad_groups"][0]["status"] = st["ad_groups"][0]["ads"][0]["status"] = "active"
    e.write(st)
    ok, p, t = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(ok and p["spend_gate"] and any("approval.activate" in b for b in p["spend_gate_blockers"]),
          "plan shows the missing-approval blocker")
    ok, _, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(not ok and "spend gate" in t and not any(r["path"].endswith("/activate") for r in e.api.requests),
          "activation refused without approval; nothing activated")
    for bad_ap, needle in ((dict(APPROVAL, approved_by="Some Agent"), "approvers"),
                           (dict(APPROVAL, expires_on="2026-11-05"), "expired"),
                           (dict(APPROVAL, approved_on="2026-11-09"), "future"),
                           (dict(APPROVAL, max_daily_budget_usd=20), "above approval"),
                           (dict(APPROVAL, max_bid_usd=4), "above approval.max_bid"),
                           (dict(APPROVAL, max_total_spend_usd=900), "max_total_spend"),
                           (dict(APPROVAL, expires_on="2026-12-31"), "window")):
        st["approval"] = bad_ap
        e.write(st)
        ok, p, t = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
        check(any(needle in b for b in p["spend_gate_blockers"]), f"approval refused: {needle}")
    st["approval"] = APPROVAL
    e.write(st)
    e.write(pol=policy(allow_activate=False))
    ok, p, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(any("allow_activate" in b for b in p["spend_gate_blockers"]), "policy allow_activate=false blocks")
    e.write(pol=policy())
    os.utime(e.hb, (NOW - 3600, NOW - 3600))
    ok, p, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(any("heartbeat" in b for b in p["spend_gate_blockers"]), "stale guard heartbeat blocks activation")
    os.utime(e.hb, (NOW - 60, NOW - 60))
    e.api.spend_life = 440.0          # desired daily $30: 500 - max(50, 30*2=60) = 440
    ok, p, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(any("pause point" in b for b in p["spend_gate_blockers"]), "2x-overdelivery room check blocks at $440")
    e.api.spend_life = 439.0
    e.api.spend_today = 45.0          # 1.25x daily: below 1.5x anomaly threshold of 45? 30*1.5 = 45 -> blocked
    ok, p, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(any("today's spend" in b for b in p["spend_gate_blockers"]), "today's anomalous spend blocks activation")
    e.api.spend_today = 10.0
    ok, p, t = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(ok and p["spend_gate_blockers"] == [], "approved, fresh guard, room under cap: no blockers " + t[:300])
    ok, a, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    acts = [r["path"] for r in e.api.requests if r["path"].endswith("/activate")]
    check(ok and len(acts) == 3 and acts[0].startswith("/ads/") and acts[1].startswith("/ad_groups/")
          and acts[2].startswith("/campaigns/"), "activation order ad -> ad group -> campaign " + t[:200])
    # uncommitted state / policy edits block commit
    st["campaign"]["budget"] = {"daily_usd": 25}
    e.write(st, commit=False)
    ok, p, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    ok, _, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(not ok and "committed" in t, "uncommitted state edit blocks commit")
    e.write(st)
    with open(e.pf, "a") as fh:
        fh.write("\n")
    ok, _, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(not ok and "committed" in t, "uncommitted policy edit blocks commit")
    e.write()

    # 5. guard: pause-only ------------------------------------------------------------------
    e.api.spend_life, e.api.spend_today = 300.0, 10.0
    ok, g, t = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok and g["action"] == "none", "guard: within caps -> none " + t[:200])
    e.api.spend_life = 440.0          # live budget 30 -> pause_at = 500 - max(50, 60) = 440
    ok, g, t = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners")
    check(ok and g["action"] == "PAUSE" and not g["committed"], "guard dry-run reports PAUSE without writing")
    ok, g, t = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    camp = e.api.store["campaign"][st["campaign"]["id"]]
    check(ok and g["committed"] and camp["status"] == "paused", "guard pauses at the 2x-aware pause point")
    camp["status"] = "active"
    e.api.spend_life, e.api.spend_today = 100.0, 45.5       # live 30 * 1.5 = 45
    ok, g, _ = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(g["action"] == "PAUSE" and any("anomaly" in w for w in g["why"]), "guard pauses on anomalous daily spend")
    camp["status"] = "active"
    e.api.spend_today = 0
    camp["budget"] = {"daily_spend_limit_micros": 90_000_000}  # raised outside AdRails
    ok, g, _ = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(g["action"] == "PAUSE" and any("over the policy cap" in w for w in g["why"]), "guard pauses an outside budget raise")
    camp["budget"] = {"daily_spend_limit_micros": 25_000_000}
    camp["status"] = "active"
    agid = st["ad_groups"][0]["id"]
    e.api.store["ad_group"][agid]["bidding_config"] = {"billing_event_type": "click", "strategy": "maximize_clicks"}
    ok, g, _ = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(g["action"] == "PAUSE", "guard pauses an uncapped (maximize) bid set outside AdRails")
    # randomized property: the guard never activates / raises / archives
    rng = random.Random(7)
    for i in range(150):
        camp["status"] = rng.choice(["active", "paused"])
        camp["budget"] = {"daily_spend_limit_micros": rng.randint(1, 80) * 1_000_000}
        e.api.spend_life, e.api.spend_today = rng.uniform(0, 600), rng.uniform(0, 120)
        before = len(e.api.requests)
        e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
        posts = [r for r in e.api.requests[before:] if r["method"] == "POST"]
        if any(not r["path"].endswith("/pause") for r in posts):
            check(False, f"guard issued a non-pause write on run {i}: {posts}")
            break
    check(True, "150 random guard runs: only /pause writes")
    # read_only mode: guard may pause, apply may not commit
    e.cfg_raw["accounts"][0]["mode"] = "read_only"
    e.reload()
    camp["status"], e.api.spend_life = "active", 499.0
    ok, g, _ = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok and camp["status"] == "paused", "guard pauses in read_only mode")
    ok, p, _ = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    ok, _, t = e.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p.get("plan_id", "x"))
    check(not ok and "mode=read_only" in t, "read_only mode refuses commit")
    e.cfg_raw["accounts"][0]["mode"] = "disabled"
    e.reload()
    ok, g, _ = e.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok and g["action"] == "none" and "disabled" in g["why"][0], "disabled account: guard is a clean no-op")
    ok, _, t = e.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok and "disabled" in t, "disabled account: plan refused with how-to")

    # 6. policy refusals --------------------------------------------------------------------
    def plan_err(st_over=None, pol_over=None, mut=None):
        x = Env(state(**(st_over or {})), policy(**(pol_over or {})))
        if mut:
            mut(x)
        ok, _, t = x.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
        return ok, t

    def ad_mut(**kw):
        s = state()
        s["ad_groups"][0]["ads"][0].update(kw)
        return {"ad_groups": s["ad_groups"]}

    def camp_mut(**kw):
        s = state()
        s["campaign"].update(kw)
        return {"campaign": s["campaign"]}

    cases = [
        (camp_mut(budget={"daily_usd": 41}), None, "exceeds policy max"),
        (camp_mut(budget={"daily_usd": 2}), None, "below policy min"),
        (camp_mut(budget={"lifetime_usd": 600}), None, "lifetime budget"),
        (camp_mut(end_date="2026-12-31"), None, "max_end_date"),
        (camp_mut(end_date=None), None, "must be set"),
        (ad_mut(target_url="https://evil.example/owners/?utm_source=chatgpt&utm_medium=paid&utm_campaign=bt_chatgpt_owners"),
         None, "allowlist"),
        (ad_mut(target_url="http://acmepm.example/owners/?utm_source=chatgpt&utm_medium=paid&utm_campaign=bt_chatgpt_owners"),
         None, "https"),
        (ad_mut(target_url="https://acmepm.example/owners/?utm_source=chatgpt&utm_medium=paid"), None, "utm_campaign"),
        (ad_mut(target_url="https://acmepm.example/owners/?utm_source=google&utm_medium=paid&utm_campaign=bt_chatgpt_owners"),
         None, "utm_source"),
        (ad_mut(file_id=None), None, "file_id is required"),
        (ad_mut(status="archived"), None, "allow_archive"),
        (None, {"caps": {"max_ops_per_run": 2}}, "max_ops_per_run"),
        (None, {"currency": "EUR"}, "currency"),
    ]
    s = state()
    s["ad_groups"][0]["bidding"] = {"strategy": "fixed_bid", "max_bid_usd": 6}
    cases.append(({"ad_groups": s["ad_groups"]}, None, "max_bid"))
    s = state()
    s["ad_groups"][0]["bidding"] = {"strategy": "maximize_clicks"}
    cases.append(({"ad_groups": s["ad_groups"]}, None, "no bid ceiling"))
    for so, po, needle in cases:
        ok, t = plan_err(so, po)
        check(not ok and needle.lower() in t.lower(), f"policy refusal: {needle} -> {t[:160]}")
    ok, t = plan_err(mut=lambda x: x.api.acct.update(id="adacct_other00"))
    check(not ok and "belongs to" in t, "key for a different ad account refused")
    ok, t = plan_err(mut=lambda x: x.api.acct.update(currency_code="EUR"))
    check(not ok and "currency" in t, "non-USD account refused")
    # bidding type immutable / archive endpoint gated
    x = Env()
    x.plan_apply()
    x.adopt_ids()
    s = json.load(open(x.sf))
    s["campaign"]["bidding_type"] = "impressions"
    x.write(s)
    ok, _, t = x.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok and "immutable" in t, "bidding_type change refused")
    s["campaign"]["bidding_type"] = "clicks"
    s["ad_groups"][0]["ads"][0]["status"] = "archived"
    x.write(s, policy(allow_archive=True))
    p, (ok, a, t) = x.plan_apply()
    check(ok and any(r["path"].endswith("/archive") for r in x.api.requests), "archive only when policy allows it")
    s["ad_groups"][0]["ads"][0]["status"] = "paused"
    x.write(s)
    ok, _, t = x.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok and "archived" in t, "an archived resource cannot be revived")
    for bad in ({"guard": {"pause_headroom_usd": 600}}, {"caps": {"max_lifetime_budget_usd": 900}},
                {"caps": {"overdelivery_factor": 0.5}}, {"allow_activate": True, "approvers": []},
                {"required_utm": {"utm_source": ""}}, {"allowed_domains": ["*"]}):
        check(raises(lambda: oai_policy.validate(policy(**bad))), f"policy validation refuses {bad}")

    # 7. cap math ------------------------------------------------------------------------------
    pol = oai_policy.validate(policy(guard={"pause_headroom_usd": 0}))
    check(oai_policy.pause_at({"daily_usd": 36}, pol) == 428.0, "pause_at = cap - 2 x daily")
    check(oai_policy.pause_at({"lifetime_usd": 500}, pol) == 500.0, "lifetime budget: pause at cap minus headroom")
    pol3 = oai_policy.validate(policy(caps={"overdelivery_factor": 3.0}))
    check(oai_policy.pause_at({"daily_usd": 36}, pol3) == 392.0, "overdelivery factor is configurable (3x)")
    check(oai_policy.daily_pause_at({"daily_usd": 36}, pol) == 54.0, "daily anomaly = 1.5 x budget")
    pol_d = oai_policy.validate(policy(guard={"daily_spend_pause_usd": 40}))
    check(oai_policy.daily_pause_at({"daily_usd": 36}, pol_d) == 40.0, "explicit daily pause wins when lower")
    check(raises(lambda: oai_policy.check_activation_room({"daily_usd": 36}, pol, 0, 428.0), "pause point", RailViolation),
          "activation room uses the same pause point as the guard")
    check(oai_policy.check_activation_room({"daily_usd": 36}, pol, 0, 427.99) == 428.0, "just under the pause point is ok")

    # 8. retries, rate limits, idempotency, pagination ---------------------------------------
    x = Env()
    orig_route, flaky = x.api.route, [(429, b'{"error": {"message": "slow down"}}'), (503, b"{}")]

    def flaky_create(m, path, q, js, h):
        if path == "/campaigns" and m == "POST" and flaky:
            st_, b_ = flaky.pop(0)
            return st_, {"retry-after": "0"}, b_
        return orig_route(m, path, q, js, h)
    x.api.route = flaky_create
    x.api.fail_next = [(OSError("reset"), None)]
    p, (ok, a, t) = x.plan_apply()
    check(ok and a["committed"], "429/503/network errors are retried " + t[:200])
    idems = [r["headers"].get("Idempotency-Key") for r in x.api.requests if r["path"] == "/campaigns" and r["method"] == "POST"]
    check(len(idems) >= 2 and len(set(idems)) == 1, "retried create reuses the same Idempotency-Key")
    check(len(x.api.store["campaign"]) == 1, "retries never duplicated the campaign")
    x = Env()
    x.api.fail_next = [(400, json.dumps({"error": {"message": "invalid budget for " + KEY}}).encode())]
    ok, p, _ = x.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok, "4xx is not retried and surfaces as an error")
    x = Env()
    x.api.page_size = 2
    for i in range(5):
        x.api.store["campaign"][f"cmpn_x{i}"] = {"id": f"cmpn_x{i}", "name": f"other {i}", "status": "paused"}
    check(len(x.client.campaigns()) == 5, "cursor pagination follows has_more/last_id")
    # partial failure stops and reports what landed
    x = Env()
    orig = x.api.route

    def fail_ads(m, path, q, js, h):
        if path == "/ads" and m == "POST":
            return 422, {}, b'{"error": {"message": "image too small"}}'
        return orig(m, path, q, js, h)
    x.api.route = fail_ads
    p, (ok, a, t) = x.plan_apply()
    check(not ok and "stopped at create ad:" in t and "cmpn_" in t, "partial failure reports landed ops and ids")
    check(all(c["status"] == "paused" for c in x.api.store["campaign"].values()), "partial failure leaves things paused")

    # 9. bulk validate_only --------------------------------------------------------------------
    x = Env(bulk=True)
    x.plan_apply()
    x.adopt_ids()
    s = json.load(open(x.sf))
    s["campaign"]["budget"] = {"daily_usd": 20}
    x.write(s)
    ok, a, _ = x.call("adsops_openai_apply", campaign="bt_chatgpt_owners")
    check(ok and "unavailable" in a["bulk"], "bulk 404 -> local validation only")
    x.api.bulk_mode = "ok"
    ok, a, _ = x.call("adsops_openai_apply", campaign="bt_chatgpt_owners")
    check(ok and a["bulk"] == "validated" and x.api.bulk["validate_only"] is True, "bulk validate_only used when enabled")
    check(x.api.bulk["operations"][0]["input"]["max_budget_micros"] == 20_000_000, "bulk translation of a budget update")
    x.api.bulk_mode = "fail"
    ok, p, _ = x.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    ok, _, t = x.call("adsops_openai_apply", campaign="bt_chatgpt_owners", commit=True, plan_id=p["plan_id"])
    check(not ok and "rejected" in t, "a failed bulk validate_only blocks the commit")

    # 10. assets + measurement -----------------------------------------------------------------
    x = Env()
    img = os.path.join(x.td, "img.png")

    def png(w, h):
        import struct
        import zlib
        raw = b"".join(b"\x00" + b"\x80" * (w * 3) for _ in range(h))
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    open(img, "wb").write(png(512, 512))
    ok, _, t = x.call("adsops_openai_upload_image", account="acme_openai", path=img, purpose="ad_image")
    check(not ok and "640" in t, "512px image refused for ads")
    ok, r, _ = x.call("adsops_openai_upload_image", account="acme_openai", path=img, purpose="account_favicon")
    check(ok and not r["committed"] and not x.api.writes(), "favicon check without commit writes nothing")
    ok, r, _ = x.call("adsops_openai_upload_image", account="acme_openai", path=img, purpose="account_favicon", commit=True)
    up = [q for q in x.api.requests if q["path"] == "/upload"]
    check(ok and r["file_id"] and b"account_favicon" in up[-1]["body"], "favicon upload sends purpose=account_favicon")
    open(img, "wb").write(b"#!/bin/sh\necho not an image\n" * 10)
    ok, _, t = x.call("adsops_openai_upload_image", account="acme_openai", path=img, purpose="account_favicon")
    check(not ok and "not a png" in t.lower(), "non-image files are refused (no file exfiltration)")
    ok, r, _ = x.call("adsops_openai_brand_update", account="acme_openai", favicon_file_id="file_0001", commit=True)
    check(not ok, "brand update refused without acknowledge_delivery_pause")
    ok, r, _ = x.call("adsops_openai_brand_update", account="acme_openai", url="https://evil.example/")
    check(not ok, "brand url must be on an allowed domain")
    ok, r, _ = x.call("adsops_openai_brand_update", account="acme_openai", favicon_file_id="file_0001",
                      url="https://acmepm.example/", commit=True, acknowledge_delivery_pause=True)
    check(ok and r["committed"] and r["review"]["status"] == "in_review", "brand update with acknowledgement")
    ok, r, _ = x.call("adsops_openai_conversions_setup", account="acme_openai")
    check(ok and len(r["todo"]) == 2 and not x.api.pixels, "conversions setup preview writes nothing")
    ok, r, _ = x.call("adsops_openai_conversions_setup", account="acme_openai", commit=True, create_capi_key=True)
    check(ok and r["event_setting"]["event_type"] == "lead_created" and x.api.events[0]["source_ids"] == [x.api.pixels[0]["id"]],
          "pixel + lead_created event setting created and linked")
    kf = x.cfg["accounts"][0]["capi_key_file"]
    check(CAPI_KEY not in json.dumps(r) and oct(os.stat(kf).st_mode & 0o777) == "0o600", "CAPI key written 0600, not returned")
    ok, r, _ = x.call("adsops_openai_conversions_setup", account="acme_openai", commit=True)
    check(ok and r["todo"] == [] and len(x.api.pixels) == 1, "conversions setup is idempotent by name")
    ok, r, t = x.call("adsops_openai_conversions_setup", account="acme_openai", commit=True, create_capi_key=True)
    check(not ok and "overwrite" in t, "existing CAPI key file is never overwritten")
    import adsops.platforms.openai_ads as oa
    real = oa.OpenAIAdsClient.__init__

    def capi_init(self, kp, http=None, **kw):
        real(self, kp, http=x.api, **kw)
    oa.OpenAIAdsClient.__init__ = capi_init
    try:
        ok, r, t = x.call("adsops_openai_test_event", account="acme_openai", pixel_id="1345",
                          source_url="https://acmepm.example/owners/thanks")
    finally:
        oa.OpenAIAdsClient.__init__ = real
    ev = [q for q in x.api.requests if q["path"] == "/events"]
    check(ok and ev and ev[-1]["body"]["validate_only"] is True and ev[-1]["body"]["events"][0]["type"] == "lead_created",
          "test event is lead_created with validate_only=true " + t[:200])
    ok, r, _ = x.call("adsops_openai_geo_lookup", account="acme_openai", q="Tampa")
    check(ok and r["results"][0]["type"] == "market", "geo lookup")

    # 11. guard-all heartbeat separation --------------------------------------------------------
    x = Env(mode="disabled")
    written = []
    rc = main_mod.guard_all(x.cfg, server=x.srv, write_heartbeat=written.append)
    check(rc == 0 and x.hb not in written, "disabled openai account never writes its heartbeat")
    x = Env()
    x.plan_apply()
    written = []
    rc = main_mod.guard_all(x.cfg, server=x.srv, write_heartbeat=written.append)
    check(rc == 0 and written == [x.hb], "guard-all writes the openai account heartbeat after a clean pass")
    x.api.fail_next = [(500, b"{}")] * 20
    written = []
    rc = main_mod.guard_all(x.cfg, server=x.srv, write_heartbeat=written.append)
    check(rc == 1 and written == [], "a failing openai guard writes no heartbeat (fail closed)")

    # guard-all --only-platform openai_ads never touches google campaigns
    y = Env()
    y.cfg["accounts"].append({"key": "g", "platform": "google_ads", "customer_id": "1",
                              "campaigns": [{"key": "gcamp", "state_file": "/nonexistent.json"}]})
    seen = []
    real_handle = y.srv.handle

    def spy(msg):
        seen.append(msg["params"]["name"])
        return real_handle(msg)
    y.srv.handle = spy
    main_mod.guard_all(y.cfg, server=y.srv, write_heartbeat=lambda p: None, only_platform="openai_ads")
    check(seen == ["adsops_openai_guard"], "guard-all --only-platform openai_ads skips google campaigns")

    # 11b. review fixes: fail-closed spend, op allow-lists, name adoption, key perms -----------
    x = Env()
    x.plan_apply()
    x.adopt_ids()
    c0 = next(iter(x.api.store["campaign"].values()))
    c0["status"] = "active"
    orig = x.api.route

    def empty_insights(m, path, q, js, h):
        if path.endswith("/insights"):
            return 200, {}, b'{"object": "list", "data": [], "has_more": false}'
        return orig(m, path, q, js, h)
    x.api.route = empty_insights
    ok, _, t = x.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(not ok and "failing closed" in t, "empty insights -> guard errors (fails closed), never reads $0")
    written = []
    main_mod.guard_all(x.cfg, server=x.srv, write_heartbeat=written.append)
    check(written == [], "unknown spend -> no heartbeat -> activation blocked")

    def renamed_spend(m, path, q, js, h):
        if path.endswith("/insights"):
            return 200, {}, b'{"object": "list", "data": [{"campaign_id": "x", "impressions": 900, "cost": 12.5}]}'
        return orig(m, path, q, js, h)
    x.api.route = renamed_spend
    ok, _, t = x.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(not ok and "no spend field" in t, "delivery without a spend field fails closed")

    def zero_row(m, path, q, js, h):
        if path.endswith("/insights"):
            check("zero_impression_items" in q.get("includes[]", []), "spend query asks for zero-impression rows")
            return 200, {}, b'{"object": "list", "data": [{"campaign_id": "x", "impressions": 0}]}'
        return orig(m, path, q, js, h)
    x.api.route = zero_row
    ok, g, t = x.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok and g["spend_life"] == 0.0, "explicit zero-delivery row reads as $0 " + t[:200])
    x.api.route = orig
    c0["created_at"] = None
    ok, _, t = x.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(not ok and "created_at" in t, "missing created_at fails closed")
    c0["created_at"] = int(NOW - 86400) * 1000
    ok, g, t = x.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok, "created_at in milliseconds is understood " + t[:200])
    pause_only = [{"op": "archive", "kind": "campaign", "ref": "campaign", "id": c0["id"]}]
    check(raises(lambda: oai_engine.execute(x.client, pause_only, {"pause"}), "not allowed", RailViolation)
          and c0["status"] != "archived", "execute refuses ops outside its allow-list (guard path = pause only)")
    act = [{"op": "activate", "kind": "campaign", "ref": "campaign", "id": c0["id"]}]
    c0["status"] = "paused"

    def boom():
        raise RailViolation("spend moved")
    check(raises(lambda: oai_engine.execute(x.client, act, {"activate"}, before_activate=boom), "spend moved", RailViolation)
          and c0["status"] == "paused", "spend is re-read right before the first activation")
    check(not hasattr(x.client, "bulk_submit") and x.client.bulk_validate.__doc__ and "ONLY" in x.client.bulk_validate.__doc__,
          "the client has no Bulk write path, only validate_only")
    # a same-named resource the state does not own (no id) is never edited
    y = Env()
    y.api.store["campaign"]["cmpn_foreign"] = {"id": "cmpn_foreign", "name": "Acme owners - Tampa Bay",
                                               "status": "paused", "bidding_type": "clicks", "created_at": int(NOW) - 99,
                                               "budget": {"daily_spend_limit_micros": 10_000_000}}
    ok, _, t = y.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok and "matched by name only" in t, "name-matched campaign with differences is refused until its id is recorded")
    kf2 = os.path.join(y.td, "g.key")
    open(kf2, "w").write(KEY)
    os.chmod(kf2, 0o640)
    check(raises(lambda: openai_ads.key_from_file(kf2)(), "group"), "group-readable key file refused")
    # child activation under a campaign that stays paused is allowed; campaign activation never is without the gate
    z = Env()
    z.plan_apply()
    st_z = z.adopt_ids()
    st_z["ad_groups"][0]["ads"][0]["status"] = "active"
    z.write(st_z)
    p, (ok, a, t) = z.plan_apply()
    check(ok and not p["spend_gate"] and next(iter(z.api.store["ad"].values()))["status"] == "active"
          and next(iter(z.api.store["campaign"].values()))["status"] == "paused",
          "activating an ad under a paused campaign needs no spend gate and the campaign stays paused")

    # 11c. GLM review fixes ---------------------------------------------------------------------
    w = Env()
    w.plan_apply()
    st_w = w.adopt_ids()
    c1 = next(iter(w.api.store["campaign"].values()))
    check(c1.get("description") == "adrails:bt_chatgpt_owners", "created campaigns carry the adrails key tag")
    rot = copy.deepcopy(st_w)
    rot["campaign"]["id"], rot["campaign"]["name"] = None, "Acme owners - rotated"
    w.write(rot)
    ok, _, t = w.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok and "already has live campaign" in t, "campaign rotation (rename + drop id) is refused")
    # orphan: a second tagged campaign that is active gets paused by the guard
    w.write(st_w)
    w.api.store["campaign"]["cmpn_orphan"] = {"id": "cmpn_orphan", "name": "old one", "status": "active",
                                              "description": "adrails:bt_chatgpt_owners", "bidding_type": "clicks",
                                              "created_at": int(NOW) - 86400, "budget": {"daily_spend_limit_micros": 40_000_000}}
    w.api.spend_life = 100.0
    ok, g, t = w.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok and w.api.store["campaign"]["cmpn_orphan"]["status"] == "paused" and any("orphan" in x for x in g["why"]),
          "guard pauses an active orphan campaign tagged for the key " + t[:200])
    check(g["spend_life"] == 200.0, "lifetime spend is summed over the managed campaign and its siblings")
    # guard does not depend on the state file
    c1["status"] = "active"
    w.api.spend_life = 300.0      # x2 campaigns = 600 >= pause point
    open(w.sf, "w").write("{ not json")
    w.write()
    ok, g, t = w.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(not ok and "unreadable" in t and c1["status"] == "paused",
          "broken state file: guard still pauses the tagged campaign, then fails (no heartbeat)")
    w.write(st_w)
    # account-wide cap pauses every active campaign in the account
    v = Env(pol=policy(caps={"max_account_spend_usd": 500, "account_spend_since": "2026-11-01"}))
    v.plan_apply()
    v.adopt_ids()
    cv = next(iter(v.api.store["campaign"].values()))
    cv["status"] = "active"
    v.api.store["campaign"]["cmpn_manual"] = {"id": "cmpn_manual", "name": "made in Ads Manager", "status": "active",
                                              "created_at": int(NOW) - 86400}
    orig_v = v.api.route

    def acct_spend(m, path, q, js, h):
        if path == "/ad_account/insights":
            return 200, {}, b'{"object": "list", "data": [{"spend": 431.0, "impressions": 5}], "has_more": false}'
        return orig_v(m, path, q, js, h)
    v.api.route = acct_spend
    ok, g, t = v.call("adsops_openai_guard", campaign="bt_chatgpt_owners", commit=True)
    check(ok and cv["status"] == "paused" and v.api.store["campaign"]["cmpn_manual"]["status"] == "paused",
          "account cap: guard pauses every active campaign in the account " + t[:200])
    pv = oai_policy.validate(policy(caps={"max_account_spend_usd": 500, "account_spend_since": "2026-11-01"}))
    check(raises(lambda: oai_policy.check_activation_room({"daily_usd": 36}, pv, 0, 0, 428.0), "account", RailViolation),
          "activation refused when account spend is past the account pause point")
    check(raises(lambda: oai_policy.check_activation_room({"daily_usd": 36}, pv, 0, 0, None), "not read", RailViolation),
          "account cap with unknown account spend fails closed")
    # approval is single use
    u = Env()
    u.plan_apply()
    st_u = u.adopt_ids()
    st_u["campaign"]["status"] = "active"
    st_u["approval"] = APPROVAL
    u.write(st_u)
    p, (ok, a, t) = u.plan_apply()
    cu = next(iter(u.api.store["campaign"].values()))
    check(ok and cu["status"] == "active", "approved activation works " + t[:200])
    cu["status"] = "paused"                       # e.g. the guard paused it
    ok, p, _ = u.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(any("already used" in b for b in p["spend_gate_blockers"]), "re-activation needs a NEW approval")
    # non-idempotent creates are not retried on 5xx
    q_ = Env()
    q_.api.fail_next = [(503, b"{}")]
    try:
        q_.client.create_pixel("AdRails website pixel")
        check(False, "pixel create should not be retried on 503")
    except ToolError:
        check(len([r for r in q_.api.requests if r["path"] == "/conversions/pixels"]) == 1,
              "a create without Idempotency-Key support is not retried on 5xx")
    q_.api.fail_next = [(429, b"{}")]
    check(q_.client.create_pixel("AdRails website pixel")["id"], "...but is retried on 429 (not processed)")
    # conversions: an event setting on ANOTHER pixel is never reused
    r_ = Env()
    r_.api.events.append({"id": "ces_other", "event_type": "lead_created", "source_ids": ["clidsrc_other"], "archived": False})
    ok, r, _ = r_.call("adsops_openai_conversions_setup", account="acme_openai", commit=True)
    check(ok and r["event_setting"]["id"] != "ces_other", "lead_created setting bound to another pixel is not reused")
    orig_r = r_.api.route

    def empty_key(m, path, q, js, h):
        if path == "/conversions/api_keys":
            return 200, {}, b'{"name": "x"}'
        return orig_r(m, path, q, js, h)
    r_.api.route = empty_key
    ok, _, t = r_.call("adsops_openai_conversions_setup", account="acme_openai", commit=True, create_capi_key=True)
    check(not ok and not os.path.exists(r_.cfg["accounts"][0]["capi_key_file"]), "empty CAPI key response writes no file")
    # asset tools verify the key belongs to the configured account
    s_ = Env()
    s_.api.acct["id"] = "adacct_someoneelse1"
    ok, _, t = s_.call("adsops_openai_conversions_setup", account="acme_openai")
    check(not ok and "belongs to" in t, "asset tools refuse a key for a different ad account")
    # duplicate sibling names refused
    dup = state()
    dup["ad_groups"][0]["ads"].append(dict(dup["ad_groups"][0]["ads"][0], key="dup2"))
    check(raises(lambda: oai_engine.validate_state(dup), "used twice"), "duplicate ad names in one ad group refused")

    # 11d. GLM second pass
    nm = Env()
    nm.api.store["campaign"]["cmpn_team"] = {"id": "cmpn_team", "name": "Acme owners - Tampa Bay", "status": "paused",
                                             "bidding_type": "clicks", "created_at": int(NOW) - 99, "end_time": None}
    cb_ = oai_engine._budget_body({"daily_usd": 36})
    nm.api.store["campaign"]["cmpn_team"]["budget"] = cb_
    ok, _, t = nm.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    check(not ok and "matched by name only" in t and not nm.api.store["ad_group"],
          "no children are created inside a campaign matched by name only")
    pb = oai_policy.validate(policy())
    sb = oai_engine.validate_state(state(campaign=dict(state()["campaign"], status="active"),
                                         approval=dict(APPROVAL, max_total_spend_usd=True)))
    check(raises(lambda: oai_policy.check_approval(sb, pb, dt.date(2026, 11, 6)), "max_total_spend", RailViolation),
          "a boolean is not a number in the approval block")
    check(raises(lambda: oai_engine._end_time("2026-11-18", "Mars/Olympus"), "timezone"), "bad account timezone -> ToolError")
    body, _ct = openai_ads.multipart({}, "file", 'x"; name="purpose\r\nevil.png', b"data")
    check(b'name="purpose\r\nevil' not in body and b"evil.png" in body, "multipart filename is sanitised")

    # 12. secrets never logged -----------------------------------------------------------------
    x = Env()
    x.api.fail_next = [(500, json.dumps({"error": {"message": "echo Authorization: Bearer " + KEY}}).encode())] * 4
    ok, _, t = x.call("adsops_openai_plan", campaign="bt_chatgpt_owners")
    x.api.fail_next = [(OSError("conn reset while sending " + KEY), None)] * 4
    ok2, _, t2 = x.call("adsops_openai_status", campaign="bt_chatgpt_owners")
    x.api.fail_next = [(403, ("{\"api_key\": \"" + KEY + "\"}").encode())]
    ok3, _, t3 = x.call("adsops_openai_account", account="acme_openai")
    x.plan_apply()
    blobs = [t, t2, t3, open(x.cfg["audit_log"]).read(), open(x.cfg["meter_log"]).read()]
    check(not ok and not ok2 and not ok3, "error paths surfaced as tool errors")
    check(all(KEY not in b and "CANARYKEY" not in b for b in blobs), "the API key never appears in outputs, audit or meter")
    check(openai_ads.redact("x " + KEY) .count("CANARY") == 0, "redact strips sk- shaped keys")


if __name__ == "__main__":
    run()
    print(f"\nQA: {CHECKS} checks, {len(FAILS)} failures")
    sys.exit(1 if FAILS else 0)
