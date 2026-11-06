"""Onboarding + diagnostics for AdRails (``adsops/onboard.py``).

``init`` scaffolds config/state/policy for one Google Ads campaign and makes the
first git commit; ``doctor`` reports whether everything needed to run is in
place; ``format_report`` renders that. Ground rules: stdlib only (py3.9+),
``init`` never overwrites an existing file, every generated policy starts safe
(caps == today's budget, commits off, enable forbidden), and ``doctor`` never
echoes environment values (tokens/keys) into its output.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from adsops import config

DEFAULT_HEARTBEAT = "~/.adsops/guard.heartbeat"


def slug(name, cid):
    """Lowercase, non-[a-z0-9] runs -> '_', trim '_', cap 40; '' -> 'c<cid>'."""
    out = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:40]
    return out or "c" + str(cid)


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def init(dir, customer_id, campaign_id, sa_path, login_customer_id=None,
         tenant="default", client=None, runner=subprocess.run, today=None):
    """Scaffold a directory for one campaign and make the first git commit."""
    base = os.path.abspath(os.path.expanduser(dir))
    os.makedirs(base, exist_ok=True)

    if client is not None:  # live: pull the facts from Google Ads
        rows = client.search(
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.end_date_time, campaign_budget.amount_micros FROM campaign "
            "WHERE campaign.id = %d" % int(campaign_id)) or []
        if not rows:
            raise ValueError("campaign %s not found in account %s, or no access"
                             % (campaign_id, customer_id))
        row = rows[0] or {}
        camp = row.get("campaign") or {}
        micros = (row.get("campaignBudget") or {}).get("amountMicros") or 0
        name = camp.get("name") or ("c%s" % campaign_id)
        raw = str(camp.get("status") or "").upper()
        status = raw if raw in ("ENABLED", "PAUSED") else "PAUSED"  # normalise
        end = (camp.get("endDateTime") or "")[:10] or None
        budget = round(int(micros) / 1e6, 2)
    else:  # offline scaffold
        name, status, end, budget = "c%s" % campaign_id, "PAUSED", None, 10.0

    key = slug(name, campaign_id)
    cfg_path = os.path.join(base, "config.json")
    state_path = os.path.join(base, key + ".json")
    policy_path = os.path.join(base, key + ".policy.json")
    for path in (cfg_path, state_path, policy_path):  # refuse before writing
        if os.path.exists(path):
            raise ValueError("refusing to overwrite " + path)

    state = {"customer_id": customer_id, "campaign_id": int(campaign_id),
             "campaign_name": name, "status": status,
             "daily_budget_usd": budget, "negatives_add": []}
    if end:
        state["end_date"] = end

    # caps start at the discovered budget, so nothing can be raised yet
    max_end = end or ((today or datetime.date.today())
                      + datetime.timedelta(days=30)).isoformat()
    policy = {
        "customer_id": customer_id, "campaign_id": int(campaign_id),
        "guards": {"max_daily_budget_usd": budget, "min_daily_budget_usd": 1,
                   "max_end_date": max_end,
                   "lifetime_spend_pause_usd": round(budget * 30, 2),
                   "daily_spend_pause_usd": round(budget * 1.5, 2),
                   "max_ops_per_run": 12},
        "allow_enable": False, "max_guard_age_min": 30,
        "require_plan_id": True, "plan_ttl_s": 900}

    cfg = {
        "tenant": tenant, "meter_log": "~/.adsops/meter.jsonl",
        "audit_log": "~/.adsops/audit.jsonl",
        "plan_ledger": "~/.adsops/plans.jsonl", "lock_dir": "~/.adsops/locks",
        "guard_heartbeat": "~/.adsops/guard.heartbeat",
        "credentials": {"google_ads": {"type": "service_account", "sources": [
            {"kind": "file", "path": sa_path}]}},
        "google_ads": {"api_version": "v24", "end_field": "end_date_time",
                       "login_customer_id": login_customer_id},
        "commit_policy": {"require_clean_git": True, "allow_commit": False,
                          "allow_set": False},
        "accounts": [{"key": "main", "platform": "google_ads",
                      "customer_id": customer_id,
                      "campaigns": [{"key": key, "state_file": state_path,
                                     "policy_file": policy_path}]}]}

    _write_json(cfg_path, cfg)
    _write_json(state_path, state)
    _write_json(policy_path, policy)

    # first commit satisfies the server's clean-git guard
    if not os.path.isdir(os.path.join(base, ".git")):
        runner(["git", "init", "-q"], cwd=base, check=True)
    runner(["git", "add", "config.json", key + ".json", key + ".policy.json"],
           cwd=base, check=True)
    runner(["git", "commit", "-q", "-m", "adrails init: " + key],
           cwd=base, check=True)

    return {
        "dir": base, "config": cfg_path, "state_file": state_path,
        "policy_file": policy_path, "campaign_key": key, "live": bool(client),
        "next_steps": [
            "Commits are OFF. Review %s (caps start at today's budget: no raises "
            "until you edit max_daily_budget_usd), then set "
            "commit_policy.allow_commit and allow_set to true in config.json and "
            "commit." % policy_path,
            "Schedule the spend guard every 15 min: adsops-mcp --config %s "
            "guard-all (templates in deploy/)." % cfg_path,
            "Register the server: claude mcp add adrails -- adsops-mcp --config %s"
            % cfg_path,
            "Run: adsops-mcp --config %s doctor" % cfg_path]}


def doctor(config_path, client_factory=None, env=None, now=None,
           git=subprocess.run):
    """Return diagnostic rows: {check, ok, detail, fix}."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    rows = []

    def add(name, fn, fix):
        try:  # a broken check must never abort the whole report
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, type(exc).__name__ + ": " + str(exc)[:200]
        rows.append({"check": name, "ok": bool(ok), "detail": str(detail),
                     "fix": fix})

    # 1) config -- nothing else is possible without it
    try:
        cfg = config.load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        return [{"check": "config", "ok": False,
                 "detail": type(exc).__name__ + ": " + str(exc)[:200],
                 "fix": "Fix %s so `adsops-mcp --config %s doctor` loads it."
                        % (config_path, config_path)}]
    add("config", lambda: (True, "loaded " + config_path), "")

    try:
        sa_path = os.path.expanduser(
            cfg["credentials"]["google_ads"]["sources"][0]["path"])
    except Exception:  # noqa: BLE001
        sa_path = "<credentials.google_ads.sources[0].path>"
    sa_email = ""  # best effort: only used in "fix" hints
    try:
        with open(sa_path, "r", encoding="utf-8") as fh:
            sa_email = str(json.load(fh).get("client_email") or "")
    except Exception:  # noqa: BLE001
        pass

    gcred = (cfg.get("credentials") or {}).get("google_ads") or {}
    oauth_user = gcred.get("type") == "oauth_user"
    signin_dev = False
    if oauth_user:
        # small-business sign-in (adsops-mcp connect): check the sign-in file, not a service account (review #10)
        sp = os.path.expanduser(str(gcred.get("file") or ""))

        def _signin():
            if not sp or not os.path.isfile(sp):
                return False, "missing sign-in file: " + (sp or "<credentials.google_ads.file>")
            if os.name != "nt" and os.stat(sp).st_mode & 0o077:
                return False, "sign-in file is readable by others: chmod 600 " + sp
            with open(sp, encoding="utf-8") as fh:
                d = json.load(fh) or {}
            return bool(d.get("refresh_token")), "signed in" if d.get("refresh_token") else "no refresh token"
        add("google_signin", _signin, "Run: adsops-mcp connect --config %s --client-file <oauth client json>" % config_path)
        try:
            with open(sp, encoding="utf-8") as fh:
                signin_dev = bool((json.load(fh) or {}).get("developer_token"))
        except Exception:  # noqa: BLE001
            pass

    # 2) service-account key file
    def _sa():
        if not os.path.isfile(sa_path):
            return False, "missing file: " + sa_path
        with open(sa_path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        email = str(data.get("client_email") or "")
        if not email:
            return False, "no client_email in " + sa_path
        return True, "service account: " + email

    if gcred and not oauth_user:
        add("service_account_file", _sa, "Create a service-account key in your "
            "Google Cloud project (Google Ads API enabled) and save it at " + sa_path)

    # 3) developer token -- presence only, value never echoed
    has_token = bool(env.get("ADSOPS_GOOGLE_DEVELOPER_TOKEN")) or signin_dev
    if not gcred:
        pass        # no Google credentials yet (local business before `connect`): nothing to check
    elif oauth_user and not has_token:
        add("developer_token", lambda: (True, "not set (only needed if Google issued one for your Cloud project)"), "")
    else:
        add("developer_token", lambda: (has_token, "set" if has_token else "missing"),
            "Google Ads > Tools > API Center: copy your developer token, then export "
            "ADSOPS_GOOGLE_DEVELOPER_TOKEN=... in the environment that launches "
            "adsops-mcp")

    # 4-6) per account / per campaign
    for acct in cfg.get("accounts") or []:
        def _access(acct=acct):
            if client_factory is None:
                return True, "skipped (offline)"
            client_factory(acct).search("SELECT customer.id FROM customer LIMIT 1")
            return True, "query ok"

        add("access:" + str(acct.get("key") or "?"), _access,
            "In Google Ads > Admin > Access and security, add %s with Standard "
            "access to account %s (or to your manager account, and set "
            "google_ads.login_customer_id)"
            % (sa_email or "<client_email>", acct.get("customer_id")))

        for camp in acct.get("campaigns") or []:
            ckey = str(camp.get("key") or "?")

            def _files(camp=camp):
                missing = [p for p in (camp.get("state_file"),
                                       camp.get("policy_file"))
                           if p and not os.path.isfile(os.path.expanduser(p))]
                return not missing, ("missing: " + ", ".join(missing) if missing
                                     else "state + policy present")

            add("files:" + ckey, _files, "run adsops-mcp init again in a new "
                "directory, or restore the file from git")

            def _git(camp=camp):
                work = os.path.dirname(os.path.abspath(os.path.expanduser(
                    camp.get("state_file") or ".")))
                proc = git(["git", "-C", work, "status", "--porcelain"],
                           capture_output=True, text=True)
                if proc.returncode != 0:
                    return False, "git status failed (rc=%s)" % proc.returncode
                dirty = (getattr(proc, "stdout", "") or "").strip()
                return not dirty, ("uncommitted changes: " + dirty[:200]
                                   if dirty else "clean")

            add("git:" + ckey, _git, "commit your edits: the server refuses "
                "uncommitted state files")

    # 7) spend-guard heartbeat (30 min freshness)
    hb_path = os.path.expanduser(cfg.get("guard_heartbeat") or DEFAULT_HEARTBEAT)

    def _heartbeat():
        if not os.path.exists(hb_path):
            return False, "never ran"
        age = now - os.path.getmtime(hb_path)
        msg = "last run %.0f min ago" % (age / 60.0)
        return age < 1800, (msg if age < 1800 else "stale: " + msg)

    add("guard_heartbeat", _heartbeat, "Schedule: adsops-mcp --config %s "
        "guard-all every 15 minutes (deploy/GUARD.md). Budget raises and "
        "re-enables are refused while it is stale." % config_path)

    return rows


def format_report(results):
    """One block per check ('PASS/FAIL  check  detail' + 'fix:'), then a total."""
    lines = []
    for row in results:
        ok = bool(row.get("ok"))
        lines.append("%s  %s  %s" % ("PASS" if ok else "FAIL", row.get("check"),
                                     row.get("detail")))
        if not ok:
            lines.append("      fix: " + str(row.get("fix") or ""))
    passed = sum(1 for row in results if row.get("ok"))
    lines.append("%d of %d checks passed" % (passed, len(results)))
    return "\n".join(lines)


def selftest():
    """Offline exercise of slug/init/doctor. Returns 0 on success."""
    count = 0

    def ck(cond, label):
        nonlocal count
        if not cond:
            raise AssertionError(label)
        count += 1

    class Done(object):  # stand-in for a completed subprocess
        returncode, stdout, stderr = 0, "", ""

    class FakeClient(object):
        def __init__(self, rows, error=None):
            self.rows, self.error, self.queries = rows, error, []

        def search(self, query):
            self.queries.append(query)
            if self.error is not None:
                raise self.error
            return self.rows

    git_calls = []

    def runner(argv, **kw):
        git_calls.append(list(argv))
        return Done()

    def fake_git(argv, **kw):
        return Done()

    tmp = tempfile.mkdtemp(prefix="adrails-selftest-")
    try:
        # --- slug ----------------------------------------------------------
        ck(slug("My Campaign! 2024", 1) == "my_campaign_2024", "slug")
        ck(slug("___", 7) == "c7", "slug empty -> c<id>")
        ck(slug("A" * 60, 1) == "a" * 40, "slug 40 cap")

        # --- init with client ---------------------------------------------
        rows = [{"campaign": {"id": "1", "name": "My Campaign! 2024",
                              "status": "ENABLED",
                              "endDateTime": "2026-12-31 23:59:59"},
                 "campaignBudget": {"amountMicros": "20000000"}}]
        out = init(os.path.join(tmp, "live"), "123", 1, "/tmp/sa-key.json",
                   client=FakeClient(rows), runner=runner)
        jload = lambda p: json.load(open(p, encoding="utf-8"))
        state, policy, cfg = jload(out["state_file"]), \
            jload(out["policy_file"]), jload(out["config"])
        ck(out["campaign_key"] == "my_campaign_2024", "init key")
        ck(out["live"] is True, "live flag")
        ck(state["campaign_name"] == "My Campaign! 2024", "state name")
        ck(state["status"] == "ENABLED", "state status")
        ck(state["daily_budget_usd"] == 20.0, "state budget")
        ck(state["end_date"] == "2026-12-31", "state end")
        ck(policy["guards"]["max_daily_budget_usd"] == 20.0, "policy max budget")
        ck(policy["guards"]["daily_spend_pause_usd"] == 30.0, "policy daily pause")
        ck(policy["guards"]["lifetime_spend_pause_usd"] == 600.0, "policy life")
        ck(policy["allow_enable"] is False, "policy enable off")
        ck(cfg["commit_policy"] == {"require_clean_git": True,
                                    "allow_commit": False, "allow_set": False},
           "commits off")
        ck(["git", "init", "-q"] in git_calls, "git init called")
        ck(["git", "add", "config.json", "my_campaign_2024.json",
            "my_campaign_2024.policy.json"] in git_calls, "git add called")
        ck(["git", "commit", "-q", "-m", "adrails init: my_campaign_2024"]
           in git_calls, "git commit called")

        # --- init offline ---------------------------------------------------
        off = init(os.path.join(tmp, "off"), "999", 555, "/tmp/sa.json",
                   runner=runner, today=datetime.date(2026, 1, 1))
        ck(off["campaign_key"] == "c555" and off["live"] is False, "offline key")
        ck(jload(off["state_file"])["daily_budget_usd"] == 10.0, "offline budget")
        ck(jload(off["policy_file"])["guards"]["max_end_date"] == "2026-01-31",
           "offline max_end = today+30")

        # --- refuse overwrite / not found -----------------------------------
        before = open(out["state_file"], encoding="utf-8").read()
        try:
            init(out["dir"], "123", 1, "/tmp/sa-key.json",
                 client=FakeClient(rows), runner=runner)
            ck(False, "overwrite refused")
        except ValueError:
            ck(True, "overwrite refused")
        ck(open(out["state_file"], encoding="utf-8").read() == before,
           "state untouched after refusal")
        try:
            init(os.path.join(tmp, "nope"), "123", 42, "/tmp/sa.json",
                 client=FakeClient([]), runner=runner)
            ck(False, "unknown campaign raises")
        except ValueError:
            ck(True, "unknown campaign raises")

        # --- doctor ---------------------------------------------------------
        sa_file = os.path.join(tmp, "sa.json")
        _write_json(sa_file, {"type": "service_account",
                              "client_email": "bot@proj.iam.gserviceaccount.com"})
        heartbeat = os.path.join(tmp, "guard.heartbeat")
        open(heartbeat, "w").close()
        patched = jload(out["config"])
        patched["credentials"]["google_ads"]["sources"][0]["path"] = sa_file
        patched["guard_heartbeat"] = heartbeat
        _write_json(out["config"], patched)

        env = {"ADSOPS_GOOGLE_DEVELOPER_TOKEN": "SECRET-TOKEN-123"}
        ok_rows = doctor(out["config"], lambda a: FakeClient([{}]), env,
                         now=time.time() + 1, git=fake_git)
        report = format_report(ok_rows)
        ck(all(r["ok"] for r in ok_rows), "doctor all green:\n" + report)
        ck("SECRET-TOKEN-123" not in report, "no token leak")
        ck("bot@proj.iam.gserviceaccount.com" in report, "sa email reported")

        no_tok = doctor(out["config"], lambda a: FakeClient([{}]), {},
                        now=time.time() + 1, git=fake_git)
        tok = [r for r in no_tok if r["check"] == "developer_token"][0]
        ck(not tok["ok"] and tok["detail"] == "missing", "token missing fails")

        def denied(acct):
            return FakeClient([], error=PermissionError("PERMISSION_DENIED"))

        bad = doctor(out["config"], denied, env, now=time.time() + 1,
                     git=fake_git)
        acc = [r for r in bad if r["check"].startswith("access:")][0]
        ck(not acc["ok"], "access failure detected")
        ck("bot@proj.iam.gserviceaccount.com" in acc["fix"], "fix names email")

        stale = doctor(out["config"], lambda a: FakeClient([{}]), env,
                       now=os.path.getmtime(heartbeat) + 4000, git=fake_git)
        hb = [r for r in stale if r["check"] == "guard_heartbeat"][0]
        ck(not hb["ok"], "stale heartbeat fails")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return count   # harness contract: return the number of checks (stdout stays clean for JSON-RPC)


if __name__ == "__main__":
    import sys
    print("ALL PASS (%d checks)" % selftest(), file=sys.stderr)
