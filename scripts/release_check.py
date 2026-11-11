#!/usr/bin/env python3
"""Release checklist for AdRails: prints every place a version ships to, in order, and VERIFIES what it can. Read-only.

    python3 scripts/release_check.py            # offline: the repo agrees with itself (version, tool count, notes)
    python3 scripts/release_check.py --online   # + read-only GETs: PyPI, GitHub release, MCP Registry, adrails.dev, Glama

It never publishes, tags, pushes, deploys or logs in. Steps that need a human or a credential are printed as TODO with the
exact command; the release owner runs them. Single source of the version: adsops/__init__.py (pyproject reads it via
setuptools dynamic metadata); server.json, README, CLAIMS and the site must agree with it, and this script says where not.
Exit code 0 only when every offline check passes (and, with --online, every online check too).
"""
import json
import os
import re
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
results = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL", detail))


def todo(name, detail):
    results.append((name, "TODO", detail))


def tool_counts():
    """tools/list size without and with an openai_ads account (the README and site state the larger number)."""
    from adsops import tools
    out = []
    for with_oai in (False, True):
        tmp = tempfile.mkdtemp(prefix="adrails-release-")
        sp = os.path.join(tmp, "s.json")
        json.dump({"customer_id": "1", "campaign_id": 1}, open(sp, "w"))
        accts = [{"key": "g", "platform": "google_ads", "customer_id": "111-222-3333",
                  "campaigns": [{"key": "c", "state_file": sp}]}]
        if with_oai:
            accts.append({"key": "o", "platform": "openai_ads", "ad_account_id": "adacct_x", "campaigns": []})
        cfg = {"tenant": "t", "audit_log": os.path.join(tmp, "a.jsonl"),
               "credentials": {"google_ads": {"type": "static", "token": "x"}}, "accounts": accts}
        srv = tools.build_server(cfg, client_factory=lambda a: None, git_runner=lambda *a, **k: None)
        out.append(len(srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]))
    return out


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "adrails-release-check/1 (read-only)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(5_000_000).decode("utf-8", "replace")


def offline():
    from adsops import __version__ as v
    sj = json.load(open(os.path.join(ROOT, "server.json")))
    check("server.json version == adsops.__version__", sj.get("version") == v, "%s vs %s" % (sj.get("version"), v))
    pk = (sj.get("packages") or [{}])[0]
    check("server.json packages[0].version", pk.get("version") == v, str(pk.get("version")))
    pp = open(os.path.join(ROOT, "pyproject.toml")).read()
    check("pyproject reads the version from adsops.__version__", 'dynamic = ["version"]' in pp and "adsops.__version__" in pp)
    readme = open(os.path.join(ROOT, "README.md")).read()
    m = re.search(r"\*\*New in %s:\*\*.*?(\d+) tools\." % re.escape(v), readme)
    check("README has a 'New in %s' line" % v, bool(m))
    n_plain, n_oai = tool_counts()
    check("README tool count == tools/list (with an openai_ads account)", bool(m) and int(m.group(1)) == n_oai,
          "README %s, tools/list %d (%d without an openai_ads account)" % (m.group(1) if m else "?", n_oai, n_plain))
    check("README keeps the MCP Registry ownership marker", "mcp-name: dev.adrails/adrails" in readme)
    check("CLAIMS.md has a section for %s" % v, ("%s " % v) in open(os.path.join(ROOT, "CLAIMS.md")).read())
    # 0.12: the shipped example site profiles must load and pass the writer/reviewer independence rule
    import glob as _glob
    from adsops.content import writers as _w
    for ex in sorted(_glob.glob(os.path.join(ROOT, "adsops", "examples_seo", "*.example.json"))):
        if os.path.basename(ex).startswith("FACTS"):
            continue
        try:
            d = json.load(open(ex))
            _w.validate_chains(d.get("writers") or [], d.get("reviewers") or [], d.get("editor"))
            ok_ex, why = True, ""
        except Exception as e:                                   # noqa: BLE001 - report the reason
            ok_ex, why = False, str(e)[:160]
        check("example profile %s: chains valid, reviewer of another family" % os.path.basename(ex), ok_ex, why)
    from adsops.content import facts as _f
    try:
        _f.load_facts(os.path.join(ROOT, "adsops", "examples_seo", "FACTS.example.json"))
        ok_f, why_f = True, ""
    except Exception as e:                                       # noqa: BLE001
        ok_f, why_f = False, str(e)[:160]
    check("FACTS.example.json loads as an approved facts file", ok_f, why_f)
    from adsops import guides as _g
    check("playbook seo-content ships", any(p["name"] == "seo-content" for p in _g.available()))
    st = os.popen("cd %s && git status --porcelain 2>/dev/null" % ROOT).read().strip()
    check("working tree clean", not st, " ".join(st.split())[:200])
    return v, n_oai


def online(v, n):
    def safe(name, fn):
        try:
            fn()
        except Exception as e:                                   # noqa: BLE001 - report, never crash the checklist
            check(name, False, "%s: %s" % (type(e).__name__, str(e)[:160]))

    safe("PyPI latest == %s" % v, lambda: check("PyPI latest == %s" % v,
         json.loads(get("https://pypi.org/pypi/adrails/json"))["info"]["version"] == v))
    safe("GitHub latest release == v%s" % v, lambda: check("GitHub latest release == v%s" % v,
         json.loads(get("https://api.github.com/repos/antonioac1/adrails/releases/latest"))["tag_name"] == "v" + v))

    def reg():
        d = json.loads(get("https://registry.modelcontextprotocol.io/v0/servers?search=dev.adrails/adrails"))
        latest = [s["server"]["version"] for s in d.get("servers", [])
                  if s["server"].get("name") == "dev.adrails/adrails"
                  and s.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {}).get("isLatest")]
        check("MCP Registry dev.adrails/adrails latest == %s" % v, latest == [v], str(latest))
    safe("MCP Registry", reg)

    def site():
        home = get("https://adrails.dev/")
        sv = re.search(r'"softwareVersion":\s*"([^"]+)"', home)
        check("adrails.dev JSON-LD softwareVersion == %s" % v, bool(sv) and sv.group(1) == v, sv.group(1) if sv else "none")
        mj = json.loads(get("https://adrails.dev/.well-known/mcp.json"))
        check("adrails.dev/.well-known/mcp.json version", mj.get("version") == v, str(mj.get("version")))
        check("adrails.dev/.well-known/mcp.json lists %d tools" % n, len(mj.get("tools") or []) == n, str(len(mj.get("tools") or [])))
        llms = get("https://adrails.dev/llms.txt")
        check("adrails.dev/llms.txt states %d tools" % n, ("%d tools" % n) in llms or ("%d MCP tools" % n) in llms)
        tools_page = get("https://adrails.dev/docs/tools/")
        check("adrails.dev/docs/tools/ says %d" % n, ("%d MCP tools" % n) in tools_page or ("%d tools" % n) in tools_page)
    safe("adrails.dev", site)

    def glama():
        page = get("https://glama.ai/mcp/servers/antonioac1/adrails")
        check("Glama page mentions %s (re-indexes from GitHub on its own schedule)" % v, v in page)
    safe("Glama", glama)


def steps(v):
    todo("1 tag + GitHub release (owner)", "git tag v%s && git push origin v%s; gh release create v%s --notes-file <notes>  "
         "-> publish.yml uploads to PyPI by Trusted Publishing (automatic on the tag); docker.yml smoke-tests the image" % (v, v, v))
    todo("2 MCP Registry (manual, needs the adrails.dev registry key)",
         "mcp-publisher login http --domain adrails.dev --private-key <hex of ~/.openclaw/secrets/adrails-registry-ed25519.pem>; "
         "mcp-publisher publish  (reads server.json; PyPI must already have %s)" % v)
    todo("3 adrails.dev (manual deploy)", "projects/adrails-site: bump build/gen.py VERSION, regenerate build/tools.json from tools/list, "
         "python3 build/gen.py && python3 build/check.py, deploy/deploy.sh <host>; then IndexNow for changed URLs (aeo_indexnow)")
    todo("4 directories", "Glama and PulseMCP re-ingest from the Registry/GitHub (verify, claim if needed); awesome-mcp-servers PR "
         "#17293 text if it names a version; docker/mcp-registry PRs (remote servers only)")
    todo("5 GitHub repo About/topics", "gh repo edit antonioac1/adrails --add-topic technical-seo --add-topic seo-audit (optional)")


def main():
    v, n = offline()
    if "--online" in sys.argv:
        online(v, n)
    steps(v)
    w = max(len(r[0]) for r in results)
    for name, status, detail in results:
        print("%-5s %-*s %s" % (status, w, name, detail))
    return 1 if any(s == "FAIL" for _, s, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
