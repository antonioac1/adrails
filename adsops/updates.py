"""Update awareness: tell the owner's agent when a newer AdRails is out, and give it one command to upgrade (2026-11-06).

Why: AdRails runs on the owner's machine, so a fix we ship (0.6.2 fixed an LSA query the live API rejects) reaches nobody until
their agent upgrades. The agent cannot know unless the server tells it.

How it works, and what it sends:
  * At most ONE request per 24 h: GET https://pypi.org/pypi/adrails/json (the public package index). No account, config, usage or
    machine data is sent; the User-Agent is "adrails/<version>". The answer is cached in <config dir>/update-check.json.
  * Off switch: env ADRAILS_NO_UPDATE_CHECK=1, or "update_check": false in the config. Offline or slow (3 s timeout) = silently
    "unknown", never an error; a check can never block or break a tool.
  * Surfaces: the MCP `initialize` instructions (from the cache, so startup never waits on the network), the adsops_update_check
    tool, the start/morning/weekly prompts, and `adsops-mcp doctor`.
  * Upgrading is the owner's call: the agent tells them what changed and, with their OK, runs `adsops-mcp upgrade`, which raises the
    version floor in Claude Desktop's config (backup first; other servers untouched) so uvx fetches the new release on restart.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request

PYPI_URL = "https://pypi.org/pypi/adrails/json"
RELEASES = "https://github.com/antonioac1/adrails/releases/tag/v%s"
TTL_S = 24 * 3600
_FLOOR = re.compile(r"^adrails(>=|==)([0-9][0-9A-Za-z.\-+]*)$")


def vtuple(v):
    """'0.6.10' -> (0, 6, 10); pre-release/garbage parts sort as 0 so a weird string never claims to be newer."""
    out = []
    for part in str(v or "").split(".")[:4]:
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out + [0] * (4 - len(out)))


def enabled(cfg=None):
    if os.environ.get("ADRAILS_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return False
    return not (isinstance(cfg, dict) and cfg.get("update_check") is False)


def cache_path(cfg):
    """Beside the config's audit/meter logs (same rule as every other state file: never a fixed home path in tests)."""
    from .enterprise_tools import state_path
    return state_path(cfg if isinstance(cfg, dict) else {}, "update_cache", "update-check.json")


def _fetch_pypi(timeout=3.0):
    from . import __version__
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "adrails/%s" % __version__, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read(2_000_000))
    return str(doc["info"]["version"])


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass                                  # a read-only home must never break the server


def status(current, latest, checked_at=None, source="cache"):
    newer = bool(latest) and vtuple(latest) > vtuple(current)
    out = {"current": current, "latest": latest, "update_available": newer, "checked_at": checked_at, "source": source}
    if newer:
        out.update({
            "release_notes": RELEASES % latest,
            "upgrade_command": "adsops-mcp upgrade",
            "how": "Tell the owner AdRails %s is out (you run %s) and what changed (release_notes). With their OK, run "
                   "`uvx --from \"adrails>=%s\" adsops-mcp upgrade` (or `adsops-mcp upgrade`), then ask them to restart "
                   "Claude Desktop." % (latest, current, latest),
        })
    elif latest is None:
        out["note"] = "latest version unknown (offline, disabled, or not checked yet)"
    return out


def check(cfg=None, current=None, now=time.time, fetch=_fetch_pypi, force=False, cache_only=False):
    """-> status dict. Uses the cache when it is under 24 h old; otherwise one PyPI request (unless cache_only)."""
    from . import __version__
    current = current or __version__
    if not enabled(cfg):
        return {**status(current, None, source="disabled"), "note": "update check disabled (ADRAILS_NO_UPDATE_CHECK or update_check:false)"}
    path = cache_path(cfg)
    c = _read(path)
    fresh = c.get("latest") and (now() - float(c.get("checked_at_ts", 0))) < TTL_S
    if (fresh and not force) or cache_only:
        return status(current, c.get("latest"), c.get("checked_at"), "cache")
    try:
        latest = fetch()
    except Exception:                         # offline / DNS / timeout / bad JSON: never an error for the caller
        return status(current, c.get("latest"), c.get("checked_at"), "cache-stale")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))
    _write(path, {"latest": latest, "checked_at": stamp, "checked_at_ts": now()})
    return status(current, latest, stamp, "pypi")


def refresh_in_background(cfg):
    """Fire-and-forget daily refresh at server start, so the NEXT start's instructions know. Daemon thread: never delays exit."""
    if enabled(cfg):
        threading.Thread(target=lambda: check(cfg), name="adrails-update-check", daemon=True).start()


def instructions_note(cfg):
    """One sentence for the MCP initialize `instructions`, from the cache only (startup never waits on the network)."""
    s = check(cfg, cache_only=True)
    if s.get("update_available"):
        return ("AdRails %s is available (this is %s). At a natural moment tell the owner and offer to upgrade: call "
                "adsops_update_check for the release notes and the command." % (s["latest"], s["current"]))
    return None


def upgrade_desktop(latest, desktop_cfg=None, now=time.time):
    """Raise the `adrails>=X` floor in every Claude Desktop entry that runs AdRails through uvx. Backup first; refuses bad JSON."""
    from .errors import ToolError
    from .local import setup_cli
    path = os.path.expanduser(desktop_cfg or os.environ.get("ADSOPS_DESKTOP_CONFIG")
                              or setup_cli.DESKTOP_CFG.get(sys.platform, setup_cli.DESKTOP_CFG["darwin"]))
    if not os.path.exists(path):
        return {"changed": [], "path": path, "note": "no Claude Desktop config found; run `adsops-mcp setup --install-desktop` first"}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError:
        raise ToolError("Claude Desktop config is not valid JSON (%s); nothing was changed" % path)
    changed = []
    for name, ent in (doc.get("mcpServers") or {}).items():
        args = ent.get("args") if isinstance(ent, dict) else None
        if not isinstance(args, list):
            continue
        for i, a in enumerate(args):
            m = _FLOOR.match(str(a))
            if m and vtuple(m.group(2)) < vtuple(latest):
                args[i] = "adrails>=%s" % latest
                changed.append({"server": name, "from": a, "to": args[i]})
    if changed:
        shutil.copy2(path, "%s.bak-%d" % (path, int(now())))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, path)
    return {"changed": changed, "path": path,
            "next": "restart Claude Desktop (quit fully, reopen) so it starts AdRails %s" % latest if changed else None}


def main_upgrade(cfg=None, desktop_cfg=None, fetch=_fetch_pypi, run=None):
    """`adsops-mcp upgrade`: find the latest release, pre-fetch it with uvx (so the restart is instant), raise Desktop's floor."""
    import subprocess
    s = check(cfg, force=True, fetch=fetch)
    latest = s.get("latest")
    if not latest:
        return {"ok": False, "error": "could not reach PyPI to find the latest version; try again when online"}
    out = {"ok": True, "current": s["current"], "latest": latest}
    uvx = shutil.which("uvx")
    if uvx:
        run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=300))
        r = run([uvx, "--refresh", "--from", "adrails>=%s" % latest, "adsops-mcp", "--selftest"])
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        out["prefetch"] = {"ok": r.returncode == 0, "selftest": tail[0][:120]}
    out["desktop"] = upgrade_desktop(latest, desktop_cfg)
    if not out["desktop"]["changed"] and vtuple(latest) <= vtuple(s["current"]) and not uvx:
        out["note"] = "already on the latest version"
    return out


def selftest():
    import tempfile
    n = 0
    assert vtuple("0.6.10") > vtuple("0.6.9") > vtuple("0.6") and vtuple("1.0.0rc1") == vtuple("1.0.0"); n += 1
    assert vtuple("garbage") == (0, 0, 0, 0); n += 1
    td = tempfile.mkdtemp(prefix="adrails-upd-")
    cfg = {"audit_log": os.path.join(td, "audit.jsonl")}
    calls = []

    def fetch():
        calls.append(1)
        return "9.9.9"
    clock = [1_000_000.0]
    s = check(cfg, current="0.6.2", now=lambda: clock[0], fetch=fetch)
    assert s["update_available"] and s["latest"] == "9.9.9" and s["source"] == "pypi" and "upgrade" in s["how"]; n += 1
    s = check(cfg, current="0.6.2", now=lambda: clock[0] + 3600, fetch=fetch)
    assert s["source"] == "cache" and len(calls) == 1, "second call inside 24 h must not hit the network"; n += 1
    clock[0] += TTL_S + 1
    check(cfg, current="0.6.2", now=lambda: clock[0], fetch=fetch)
    assert len(calls) == 2, "after 24 h it refreshes"; n += 1

    def boom():
        raise OSError("offline")
    s = check(cfg, current="0.6.2", now=lambda: clock[0] + 2 * TTL_S, fetch=boom)
    assert s["latest"] == "9.9.9" and s["source"] == "cache-stale", "offline falls back to the last known answer"; n += 1
    s = check({"audit_log": os.path.join(td, "x", "a.jsonl")}, current="0.6.2", fetch=boom)
    assert s["latest"] is None and not s["update_available"] and "note" in s; n += 1
    s = check(cfg, current="9.9.9", now=lambda: clock[0], fetch=fetch)
    assert not s["update_available"]; n += 1
    assert check({**cfg, "update_check": False}, fetch=fetch)["source"] == "disabled"; n += 1
    os.environ["ADRAILS_NO_UPDATE_CHECK"] = "1"
    try:
        assert check(cfg, fetch=fetch)["source"] == "disabled" and instructions_note(cfg) is None; n += 1
    finally:
        os.environ.pop("ADRAILS_NO_UPDATE_CHECK", None)
    from . import __version__
    assert ("AdRails 9.9.9 is available" in (instructions_note(cfg) or "")) == (vtuple(__version__) < vtuple("9.9.9")); n += 1
    # Desktop floor upgrade: only adrails floors that are lower; other servers untouched; backup made; bad JSON refused
    dpath = os.path.join(td, "claude_desktop_config.json")
    doc = {"mcpServers": {"adrails": {"command": "/x/uvx", "args": ["--from", "adrails>=0.6.1", "adsops-mcp", "--config", "c"]},
                          "other": {"command": "npx", "args": ["foo>=1.0"]}, "pinned": {"command": "uvx", "args": ["adrails==9.9.9"]}}}
    json.dump(doc, open(dpath, "w"))
    r = upgrade_desktop("0.7.0", dpath, now=lambda: 5)
    d2 = json.load(open(dpath))
    assert d2["mcpServers"]["adrails"]["args"][1] == "adrails>=0.7.0" and d2["mcpServers"]["other"] == doc["mcpServers"]["other"]; n += 1
    assert d2["mcpServers"]["pinned"]["args"] == ["adrails==9.9.9"] and os.path.exists(dpath + ".bak-5") and len(r["changed"]) == 1; n += 1
    assert upgrade_desktop("0.7.0", dpath)["changed"] == [], "idempotent"; n += 1
    open(dpath, "w").write("{bad")
    try:
        upgrade_desktop("0.7.0", dpath)
        raise AssertionError("bad JSON must be refused")
    except Exception as e:
        assert "not valid JSON" in str(e); n += 1
    assert open(dpath).read() == "{bad"; n += 1
    assert upgrade_desktop("0.7.0", os.path.join(td, "none.json"))["changed"] == []; n += 1
    # main_upgrade with a fake uvx run and fake fetch: never touches the real Desktop config (sandbox path passed explicitly)
    json.dump(doc, open(dpath, "w"))
    old_which = shutil.which
    shutil.which = lambda name: "/fake/uvx" if name == "uvx" else None
    try:
        r = main_upgrade(cfg, dpath, fetch=lambda: "0.7.0",
                         run=lambda cmd: type("R", (), {"returncode": 0, "stdout": "ALL PASS (1 checks)\n"})())
    finally:
        shutil.which = old_which
    assert r["ok"] and r["latest"] == "0.7.0" and r["prefetch"]["ok"] and r["desktop"]["changed"]; n += 1
    assert main_upgrade(cfg, dpath, fetch=boom)["ok"] in (True, False); n += 1    # offline: falls back to cache, never raises
    shutil.rmtree(td, ignore_errors=True)
    return n
