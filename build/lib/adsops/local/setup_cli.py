"""Agent-driven onboarding for a small business (the maintainers, 2026-11-06). The owner pastes ONE prompt into Claude; Claude runs these:

  adsops-mcp setup  [--dir ~/AdRails] [--business "Name"] [--install-desktop]
      -> workspace dir (chmod 700) + config.json with a `local` block and no ad accounts yet (lead book, plan, AEO and reviews work
         immediately), optionally registered in Claude Desktop's config (backup first, merge, never clobber other servers).
  adsops-mcp connect --config <cfg> --client-file <OAuth desktop client JSON> [--developer-token-file F]
      -> one Google sign-in in the browser (loopback + PKCE, 127.0.0.1 only, one request, then closed) -> refresh token saved chmod 600
         beside the config -> lists the Google Ads accounts that login can reach and adds them (read-only by default: no campaigns
         under rails until the owner asks for one).
Every command prints ONE JSON object (what happened + next steps written for the agent), never a secret. Pure helpers take injectable
paths/callables so the selftest runs with no network and no real Claude Desktop.
"""
import json
import os
import shutil
import sys
import time

from adsops.errors import ToolError

DESKTOP_CFG = {
    "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
    "win32": os.path.join(os.environ.get("APPDATA", "~"), "Claude", "claude_desktop_config.json"),
}
SCOPES = ["https://www.googleapis.com/auth/adwords"]


def _write_private(path, doc, overwrite=False):
    if os.path.exists(path) and not overwrite:
        raise ToolError("%s exists; not overwriting" % path)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def setup(dir="~/AdRails", business=None):
    """Create the workspace + config. Idempotent: an existing config is kept and reported."""
    base = os.path.abspath(os.path.expanduser(dir))
    os.makedirs(base, mode=0o700, exist_ok=True)
    cfg_path = os.path.join(base, "config.json")
    created = False
    if not os.path.exists(cfg_path):
        _write_private(cfg_path, {
            "tenant": "default",
            "accounts": [],
            "local": {"business": business or ""},
            "audit_log": os.path.join(base, "audit.jsonl"),
            "meter_log": os.path.join(base, "meter.jsonl"),
            "audit_chain": os.path.join(base, "audit-chain.jsonl"),
        })
        created = True
    return {"workspace": base, "config": cfg_path, "created": created}


def server_entry(cfg_path, which=shutil.which):
    """The mcpServers entry for Claude Desktop. Absolute uvx path: Desktop does not inherit the shell PATH."""
    uvx = which("uvx")
    if uvx:
        # pinned floor: a machine that cached an older adrails must not start it (0.7.0: playbooks + update check; 0.6.2 fixed the live LSA lead query)
        return {"command": uvx, "args": ["--from", "adrails>=0.7.0", "adsops-mcp", "--config", cfg_path]}
    exe = which("adsops-mcp")
    if exe:
        return {"command": exe, "args": ["--config", cfg_path]}
    return {"command": sys.executable, "args": ["-m", "adsops", "--config", cfg_path]}


def install_desktop(cfg_path, desktop_cfg=None, which=shutil.which, now=time.time):
    """Merge an `adrails` server into Claude Desktop's config. Backs up first; other servers untouched; never writes on bad JSON."""
    # ADSOPS_DESKTOP_CONFIG lets tests point at a sandbox (a smoke test once wrote the real Desktop config: restored from backup)
    path = os.path.expanduser(desktop_cfg or os.environ.get("ADSOPS_DESKTOP_CONFIG")
                              or DESKTOP_CFG.get(sys.platform, DESKTOP_CFG["darwin"]))
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh) or {}
        except ValueError:
            raise ToolError("Claude Desktop config is not valid JSON (%s); fix it first, nothing was changed" % path)
        shutil.copy2(path, "%s.bak-%d" % (path, int(now())))
    if not isinstance(doc, dict):
        raise ToolError("Claude Desktop config top level is not an object; nothing was changed")
    servers = doc.setdefault("mcpServers", {})
    entry = server_entry(cfg_path, which)
    replaced = "adrails" in servers
    servers["adrails"] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return {"desktop_config": path, "entry": entry, "replaced": replaced,
            "next": "Quit and reopen Claude Desktop (Cmd+Q), then AdRails' tools and the start/morning/weekly prompts appear."}


def read_client_file(path):
    """Google's downloaded OAuth client JSON ({"installed": {...}}) -> (client_id, client_secret)."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        d = json.load(fh)
    inner = d.get("installed") or d.get("web") or d
    cid, sec = inner.get("client_id"), inner.get("client_secret")
    if not cid or not sec:
        raise ToolError("that file is not a Google OAuth client (need client_id and client_secret; create a 'Desktop app' client)")
    if "web" in d and "installed" not in d:
        raise ToolError("that is a 'Web application' client; create an OAuth client of type 'Desktop app' instead")
    return cid, sec


def accessible_customers(token_provider, http, developer_token=None, api_version="v24"):
    """GET customers:listAccessibleCustomers -> ['1234567890', ...]."""
    h = {"Authorization": "Bearer " + token_provider.access_token()}
    if developer_token:
        h["developer-token"] = developer_token
    status, body = http("GET", "https://googleads.googleapis.com/%s/customers:listAccessibleCustomers" % api_version, h, None)
    if int(status) != 200:
        raise ToolError("Google Ads API refused listAccessibleCustomers (HTTP %s): %s" % (status, str(body)[:300]))
    names = (json.loads(body) if isinstance(body, str) else body).get("resourceNames") or []
    return [n.split("/")[-1] for n in names]


def connect(cfg_path, client_file, developer_token=None, run_loopback=None, http=None, open_browser=None, list_accounts=True):
    """Sign in once, save the refresh token beside the config, wire credentials, add reachable Google Ads accounts."""
    from adsops.local import oauth_connect
    if http is None:
        from adsops.aeo_tools import raw_http as http
    cfg_path = os.path.abspath(os.path.expanduser(cfg_path))
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cid, sec = read_client_file(client_file)
    kw = {"http": http}
    if open_browser is not None:
        kw["open_browser"] = open_browser
    got = (run_loopback or oauth_connect.run_loopback)(cid, sec, SCOPES, **kw)
    signin = os.path.join(os.path.dirname(cfg_path), "google-signin.json")
    doc = {"type": "oauth_user", "client_id": cid, "client_secret": sec, "refresh_token": got["refresh_token"]}
    if developer_token:
        doc["developer_token"] = developer_token
    _write_private(signin, doc, overwrite=True)
    cfg.setdefault("credentials", {})["google_ads"] = {"type": "oauth_user", "file": signin}
    added, ids, warn = [], [], None
    if list_accounts:
        try:
            tp = oauth_connect.UserTokenProvider(doc, http=http)
            ids = accessible_customers(tp, http, developer_token)
        except ToolError as e:
            warn = str(e)
        have = {str(a.get("customer_id", "")).replace("-", "") for a in cfg.get("accounts", [])}
        for i in ids:
            if i not in have:
                cfg.setdefault("accounts", []).append({"key": "google_%s" % i, "platform": "google_ads", "customer_id": i, "campaigns": []})
                added.append(i)
    _write_private(cfg_path, cfg, overwrite=True)
    out = {"signed_in": True, "signin_file": signin, "accounts_found": ids, "accounts_added": added,
           "next": "Restart Claude Desktop so AdRails reloads, then run the 'start' prompt."}
    if warn:
        out["warning"] = warn + " (sign-in saved; accounts can be added later once API access is approved)"
    return out


def main(cmd, a):
    """CLI entry from adsops.__main__; prints one JSON object. Returns the exit code."""
    try:
        if cmd == "setup":
            out = setup(a.dir or "~/AdRails", a.business)
            if a.install_desktop:
                out["desktop"] = install_desktop(out["config"])
            out["next_steps"] = [
                "Restart Claude Desktop (Cmd+Q, reopen) if --install-desktop ran.",
                "In Claude Desktop, pick the AdRails 'start' prompt (or say: use AdRails to set up my business).",
                "To connect Google Ads/LSA: create a Google Cloud OAuth client of type 'Desktop app' with the Google Ads API enabled, "
                "download its JSON, then run: adsops-mcp connect --config %s --client-file <that.json>" % out["config"],
            ]
        elif cmd == "connect":
            if not (a.config and a.client_file):
                raise ToolError("connect needs --config and --client-file")
            dev = None
            if a.developer_token_file:
                with open(os.path.expanduser(a.developer_token_file), encoding="utf-8") as fh:
                    dev = fh.read().strip()
            out = connect(a.config, a.client_file, developer_token=dev)
        else:
            raise ToolError("unknown command %s" % cmd)
    except (ToolError, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)[:500]}))
        return 1
    print(json.dumps({"ok": True, **out}, indent=1))
    return 0


def selftest():
    import tempfile
    n = 0

    def ok(c, m):
        nonlocal n
        n += 1
        if not c:
            raise AssertionError(m)

    with tempfile.TemporaryDirectory() as td:
        r = setup(os.path.join(td, "ws"), "Example Plumbing")
        ok(r["created"] and (os.name == "nt" or os.stat(r["config"]).st_mode & 0o777 == 0o600), "config private")
        ok(not setup(os.path.join(td, "ws"))["created"], "setup idempotent")
        from adsops import config as cmod
        c = cmod.load_config(r["config"])
        ok(c["accounts"] == [] and c["local"]["business"] == "Example Plumbing", "account-less local config loads")
        dpath = os.path.join(td, "claude", "claude_desktop_config.json")
        os.makedirs(os.path.dirname(dpath))
        json.dump({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}, open(dpath, "w"))
        d = install_desktop(r["config"], dpath, which=lambda n: "/opt/bin/uvx" if n == "uvx" else None, now=lambda: 1)
        doc = json.load(open(dpath))
        ok(doc["mcpServers"]["other"] == {"command": "x"} and doc["theme"] == "dark", "other servers untouched")
        ok(doc["mcpServers"]["adrails"]["command"] == "/opt/bin/uvx" and r["config"] in doc["mcpServers"]["adrails"]["args"], "entry")
        ok(os.path.exists(dpath + ".bak-1") and not d["replaced"], "backup written")
        open(dpath, "w").write("{bad")
        try:
            install_desktop(r["config"], dpath)
            ok(False, "bad json must refuse")
        except ToolError:
            ok(open(dpath).read() == "{bad", "bad desktop json untouched")
        e = server_entry("/c.json", which=lambda n: None)
        ok(e["command"] == sys.executable and e["args"][:2] == ["-m", "adsops"], "fallback entry")
        cf = os.path.join(td, "client.json")
        json.dump({"installed": {"client_id": "cid.apps", "client_secret": "S3CRET"}}, open(cf, "w"))
        ok(read_client_file(cf) == ("cid.apps", "S3CRET"), "client file")
        wf = os.path.join(td, "web.json")
        json.dump({"web": {"client_id": "a", "client_secret": "b"}}, open(wf, "w"))
        try:
            read_client_file(wf)
            ok(False, "web client refused")
        except ToolError as ex:
            ok("Desktop app" in str(ex), "web client refused with hint")

        def http(method, url, headers, body):
            if "oauth2" in url:
                return 200, json.dumps({"access_token": "AT", "expires_in": 3600})
            ok(headers["Authorization"] == "Bearer AT", "bearer used")
            return 200, json.dumps({"resourceNames": ["customers/1112223333", "customers/4445556666"]})
        out = connect(r["config"], cf, run_loopback=lambda *a, **k: {"refresh_token": "RT"}, http=http)
        sf = out["signin_file"]
        ok((os.name == "nt" or os.stat(sf).st_mode & 0o777 == 0o600) and json.load(open(sf))["refresh_token"] == "RT", "signin private")
        c2 = json.load(open(r["config"]))
        ok(c2["credentials"]["google_ads"] == {"type": "oauth_user", "file": sf} and len(c2["accounts"]) == 2, "wired + accounts")
        ok("S3CRET" not in json.dumps(out) and '"RT"' not in json.dumps(out), "no secrets in output")
        out2 = connect(r["config"], cf, run_loopback=lambda *a, **k: {"refresh_token": "RT2"}, http=http)
        ok(out2["accounts_added"] == [] and len(json.load(open(r["config"]))["accounts"]) == 2, "no duplicate accounts")
        from adsops import auth
        tp = auth.from_config({"type": "oauth_user", "file": sf})
        ok(hasattr(tp, "access_token"), "auth oauth_user from file")
        os.chmod(sf, 0o644)
        if os.name == "nt":
            return n
        try:
            auth.from_config({"type": "oauth_user", "file": sf})
            ok(False, "world-readable refused")
        except ToolError:
            ok(True, "world-readable signin refused")
    return n


if __name__ == "__main__":
    print("ALL PASS (%d checks)" % selftest(), file=sys.stderr)
