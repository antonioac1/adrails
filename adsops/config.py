"""Deployment config: load + validate, resolve campaigns, and git cleanliness.

The config is plain JSON so a deployment can be reviewed like any other file.
Every path in it is expanded with ``os.path.expanduser`` at load time; validation
failures raise ``ToolError`` naming the offending key.
"""
import json
import os
import subprocess

from . import strict
from .errors import ToolError, scrub

PLATFORMS = {"google_ads"}
DEFAULT_POLICY = {"require_clean_git": True, "allow_commit": False}
DEFAULT_METER_LOG = "~/.adsops/meter.jsonl"
DEFAULT_AUDIT_LOG = "~/.adsops/audit.jsonl"


def _expand(path):
    return os.path.expanduser(path) if isinstance(path, str) else path


def _need_str(obj, key, where=""):
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"config: {where}'{key}' is required and must be a non-empty string")
    return v


def _load_policy(cfg):
    pol = cfg.get("commit_policy")
    if pol is None:
        pol = dict(DEFAULT_POLICY)
    elif not isinstance(pol, dict):
        raise ToolError("config: 'commit_policy' must be an object")
    else:
        pol = {**DEFAULT_POLICY, **pol}
    for k in ("require_clean_git", "allow_commit", "allow_set"):
        if k in pol and not isinstance(pol[k], bool):
            raise ToolError(f"config: commit_policy['{k}'] must be a boolean")
    cfg["commit_policy"] = pol


def _load_credentials(cfg):
    cred = cfg.get("credentials")
    if cred is None:
        return
    if not isinstance(cred, dict):
        raise ToolError("config: 'credentials' must be an object")
    g = cred.get("google_ads")
    if g is None:
        return
    if not isinstance(g, dict):
        raise ToolError("config: credentials['google_ads'] must be an object")
    _need_str(g, "type", "credentials['google_ads']: ")
    sources = g.get("sources", [])
    if not isinstance(sources, list):
        raise ToolError("config: credentials['google_ads']['sources'] must be a list")
    for j, src in enumerate(sources):
        if not isinstance(src, dict):
            raise ToolError(f"config: credentials['google_ads']['sources'][{j}] must be an object")
        for k in ("path", "token_file"):
            if k in src:
                src[k] = _expand(_need_str(src, k, f"credentials['google_ads']['sources'][{j}]: "))


def load_config(path):
    """Read, validate and normalise a deployment config. Returns the dict."""
    if not isinstance(path, str):
        raise ToolError("config: path must be a string")
    p = _expand(path)
    cfg = strict.load_json_file(p, "config file")
    if not isinstance(cfg, dict):
        raise ToolError("config: top level must be a JSON object")

    _need_str(cfg, "tenant")
    _load_policy(cfg)
    cfg["meter_log"] = _expand(cfg.get("meter_log", DEFAULT_METER_LOG))
    cfg["audit_log"] = _expand(cfg.get("audit_log", DEFAULT_AUDIT_LOG))
    _load_credentials(cfg)

    accounts = cfg.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ToolError("config: 'accounts' must be a non-empty list")
    seen, seen_accounts = {}, set()
    for i, acct in enumerate(accounts):
        if not isinstance(acct, dict):
            raise ToolError(f"config: accounts[{i}] must be an object")
        akey = _need_str(acct, "key", f"accounts[{i}]: ")
        if akey in seen_accounts:
            raise ToolError(f"config: duplicate account key: '{akey}'")
        seen_accounts.add(akey)
        plat = _need_str(acct, "platform", f"account '{akey}': ")
        if plat not in PLATFORMS:
            raise ToolError(
                f"config: account '{akey}': unsupported platform {plat!r} "
                f"(supported: {', '.join(sorted(PLATFORMS))})"
            )
        _need_str(acct, "customer_id", f"account '{akey}': ")
        camps = acct.get("campaigns")
        if not isinstance(camps, list):
            raise ToolError(f"config: account '{akey}': 'campaigns' must be a list")
        for j, camp in enumerate(camps):
            if not isinstance(camp, dict):
                raise ToolError(f"config: account '{akey}': campaigns[{j}] must be an object")
            ckey = _need_str(camp, "key", f"account '{akey}' campaigns[{j}]: ")
            if ckey in seen:
                raise ToolError(f"config: duplicate campaign key: '{ckey}'")
            camp["state_file"] = _expand(
                _need_str(camp, "state_file", f"campaign '{ckey}': ")
            )
            seen[ckey] = akey
    return cfg


def resolve_campaign(cfg, key):
    """Return ``(account, campaign)`` for a campaign key; unknown -> ToolError."""
    for acct in cfg.get("accounts", []) or []:
        for camp in acct.get("campaigns", []) or []:
            if camp.get("key") == key:
                return acct, camp
    raise ToolError(f"unknown campaign key: {key}")


def list_campaigns(cfg):
    """Flat, safe listing of every campaign in the config."""
    out = []
    for acct in cfg.get("accounts", []) or []:
        for camp in acct.get("campaigns", []) or []:
            out.append({
                "key": camp.get("key"),
                "account": acct.get("key"),
                "platform": acct.get("platform"),
                "customer_id": acct.get("customer_id"),
                "state_file": camp.get("state_file"),
            })
    return out


def git_clean(path, runner=subprocess.run):
    """True only if ``path`` is tracked by git and has no pending changes.

    Both commands run with ``cwd`` set to the file's directory. Never raises:
    a missing git binary or a non-repo directory yields ``(False, reason)``.
    """
    f = os.path.abspath(_expand(path))
    cwd = os.path.dirname(f) or "."

    def _run(args):
        return runner(args, cwd=cwd, capture_output=True, text=True)

    try:
        r = _run(["git", "ls-files", "--error-unmatch", "--", f])
    except Exception as e:  # git missing, not a repo, bad runner — all non-fatal
        return False, f"git unavailable: {e.__class__.__name__}"
    if getattr(r, "returncode", 1) != 0:
        return False, "not tracked by git"

    try:
        r = _run(["git", "status", "--porcelain", "--", f])
    except Exception as e:
        return False, f"git unavailable: {e.__class__.__name__}"
    if getattr(r, "returncode", 1) != 0:
        err = (getattr(r, "stderr", "") or "").strip()
        return False, f"git status failed: {scrub(err, 200)}"
    if (getattr(r, "stdout", "") or "").strip():
        return False, "uncommitted changes"
    return True, "tracked and clean"


EXAMPLE = {
    "tenant": "example-agency",
    "meter_log": "~/.adsops/meter.jsonl",
    "audit_log": "~/.adsops/audit.jsonl",
    "credentials": {"google_ads": {"type": "service_account",
                                   "sources": [{"kind": "file", "path": "~/.adsops/sa.json"}]}},
    "google_ads": {"api_version": "v24", "end_field": "end_date_time", "login_customer_id": None},
    "commit_policy": {"require_clean_git": True, "allow_commit": False},
    "accounts": [{"key": "acme", "platform": "google_ads", "customer_id": "000-000-0000",
                  "campaigns": [{"key": "acme_search", "state_file": "~/ads/acme_search.json"}]}],
}


def selftest():
    import copy
    import tempfile
    from types import SimpleNamespace

    n = 0

    def fails(fn, *needles):
        try:
            fn()
        except ToolError as e:
            msg = str(e)
            for needle in needles:
                assert needle in msg, (needle, msg)
            return
        raise AssertionError("expected ToolError")

    with tempfile.TemporaryDirectory() as td:
        def dump(obj, name="config.json"):
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
            return p

        def variant(**over):
            c = copy.deepcopy(EXAMPLE)
            c.update(over)
            return c

        # 1. the shipped example config loads and normalises
        cfg = load_config(dump(EXAMPLE))
        assert cfg["tenant"] == "example-agency"
        assert cfg["accounts"][0]["campaigns"][0]["key"] == "acme_search"
        n += 1

        # 2. missing config file
        fails(lambda: load_config(os.path.join(td, "nope.json")), "not found")
        n += 1

        # 3. missing tenant names tenant
        fails(lambda: load_config(dump(variant(tenant=""))), "tenant")
        n += 1

        # 4. bad platform names platform and the account key
        bad_plat = variant(accounts=[{"key": "acme", "platform": "tiktok",
                                      "customer_id": "1", "campaigns": []}])
        fails(lambda: load_config(dump(bad_plat)), "platform", "acme")
        n += 1

        # 5. missing customer_id names customer_id
        no_cid = variant(accounts=[{"key": "acme", "platform": "google_ads",
                                    "campaigns": []}])
        fails(lambda: load_config(dump(no_cid)), "customer_id")
        n += 1

        # 6. duplicate campaign key names the duplicated key
        dup = variant(accounts=[
            {"key": "a1", "platform": "google_ads", "customer_id": "1",
             "campaigns": [{"key": "dup_search", "state_file": "~/s1.json"}]},
            {"key": "a2", "platform": "google_ads", "customer_id": "2",
             "campaigns": [{"key": "dup_search", "state_file": "~/s2.json"}]},
        ])
        fails(lambda: load_config(dump(dup)), "duplicate", "dup_search")
        n += 1

        # 7. commit is OFF by default
        no_pol = copy.deepcopy(EXAMPLE)
        del no_pol["commit_policy"]
        polcfg = load_config(dump(no_pol))
        assert polcfg["commit_policy"] == {"require_clean_git": True, "allow_commit": False}
        n += 1

        # 8. bad policy type names commit_policy
        fails(lambda: load_config(dump(variant(commit_policy={"allow_commit": "yes"}))),
              "commit_policy", "allow_commit")
        n += 1

        # 9. paths are ~-expanded (meter/audit, credential source, state file)
        exp = load_config(dump(EXAMPLE))
        assert exp["meter_log"] == os.path.expanduser("~/.adsops/meter.jsonl")
        assert exp["audit_log"] == os.path.expanduser("~/.adsops/audit.jsonl")
        assert exp["credentials"]["google_ads"]["sources"][0]["path"] == \
            os.path.expanduser("~/.adsops/sa.json")
        assert exp["accounts"][0]["campaigns"][0]["state_file"] == \
            os.path.expanduser("~/ads/acme_search.json")
        assert "~" not in exp["accounts"][0]["campaigns"][0]["state_file"]
        n += 1

        # 10. resolve_campaign returns (account, campaign)
        acct, camp = resolve_campaign(exp, "acme_search")
        assert acct["key"] == "acme" and camp["state_file"].endswith("acme_search.json")
        n += 1

        # 11. unknown campaign key -> ToolError
        fails(lambda: resolve_campaign(exp, "ghost"), "unknown campaign key", "ghost")
        n += 1

        # 12. list_campaigns shape
        rows = list_campaigns(exp)
        assert len(rows) == 1
        assert set(rows[0]) == {"key", "account", "platform", "customer_id", "state_file"}
        assert rows[0]["key"] == "acme_search" and rows[0]["account"] == "acme"
        assert rows[0]["platform"] == "google_ads" and rows[0]["customer_id"] == "000-000-0000"
        n += 1

        # --- git_clean via a fake runner (no real git, no real repo) -------------
        state_path = os.path.join(td, "state.json")
        open(state_path, "w").close()

        def fake(rc_ls=0, stdout="", rc_status=0, boom=False, calls=None):
            def runner(args, cwd=None, capture_output=True, text=True, **kw):
                if boom:
                    raise FileNotFoundError("git")
                if calls is not None:
                    calls.append((list(args), cwd))
                if args[1] == "ls-files":
                    return SimpleNamespace(returncode=rc_ls, stdout="",
                                           stderr="fatal: not a git repository")
                return SimpleNamespace(returncode=rc_status, stdout=stdout,
                                       stderr="fatal: bad revision")
            return runner

        # 13. tracked + clean -> (True, reason); cwd is the file's directory
        calls = []
        ok, reason = git_clean(state_path, runner=fake(calls=calls))
        assert ok is True, reason
        assert calls and calls[0][1] == os.path.dirname(state_path)
        n += 1

        # 14. untracked -> (False, reason)
        ok, reason = git_clean(state_path, runner=fake(rc_ls=1))
        assert ok is False and "tracked" in reason
        n += 1

        # 15. modified -> (False, reason)
        ok, reason = git_clean(state_path, runner=fake(stdout=" M state.json\n"))
        assert ok is False and "uncommitted" in reason
        n += 1

        # 16. git failure (binary missing) never raises
        ok, reason = git_clean(state_path, runner=fake(boom=True))
        assert ok is False and "git unavailable" in reason
        n += 1

        # 17. git status failure never raises
        ok, reason = git_clean(state_path, runner=fake(rc_status=128))
        assert ok is False and "git status failed" in reason
        n += 1

        # 18. untracked/other porcelain output never reads as clean
        ok, reason = git_clean(state_path, runner=fake(stdout="?? extra.json\n"))
        assert ok is False and "uncommitted" in reason
        n += 1

    return n
