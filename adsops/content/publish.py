"""Guarded publishers for approved content bundles (AdRails 0.12). PM-written security core (Order D).

Nothing reaches a site except through `publish()`, and `publish()` refuses unless ALL of these hold:
  1. no kill-switch file (STOP) and no HOLD file (a failed post-publish check writes HOLD; only a human removes it);
  2. the bundle's MANIFEST verifies byte for byte, its review verdict is "approve" and its checks passed;
  3. mode "human" (default): a human approved this exact bundle (plan id = hash of its MANIFEST) through the existing
     signed approvals queue (ADRAILS_APPROVAL_SECRET); mode "auto": the profile enables it AND daily/weekly caps and a
     local time window are set and respected;
  4. the publish lock is free (one publish at a time).
Publishers: "static" (write page.md, optional index.html, into a directory under the slug; never outside it, never
through a symlink, never over an existing page unless the bundle is an "extend" of that page, with a backup) and "git"
(the same write inside a clean git working tree, on a NEW branch, one commit; it never pushes - opening the pull
request stays with the operator). Every attempt, allowed or refused, is appended to the audit chain.
After writing, the file is re-read and its hash compared; optionally the live URL is fetched and must return 200 with
the page title. Any failure writes HOLD and stops all further publishing.
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time

from ..errors import RailViolation, ToolError, scrub
from . import bundle as bundle_mod

TARGETS = ("none", "static", "git")
_BRANCH_OK = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,80}$")
_WINDOW = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


def settings(profile):
    """-> validated publish settings (ToolError on anything unsafe or incomplete)."""
    p = dict((profile or {}).get("publish") or {})
    out = {"target": p.get("target", "none"), "mode": p.get("mode", "human"), "html": bool(p.get("html", False)),
           "verify_url": bool(p.get("verify_url", False)), "branch_prefix": p.get("branch_prefix", "adrails/content-")}
    if out["target"] not in TARGETS:
        raise ToolError("publish.target must be one of %s" % ", ".join(TARGETS))
    if out["mode"] not in ("human", "auto"):
        raise ToolError("publish.mode must be human or auto")
    if out["target"] != "none":
        d = p.get("dir")
        if not isinstance(d, str) or not d:
            raise ToolError("publish.dir is required for target %s" % out["target"])
        base = (profile or {}).get("_dir") or os.getcwd()
        out["dir"] = os.path.realpath(os.path.join(base, os.path.expanduser(d)))
        if not os.path.isdir(out["dir"]):
            raise ToolError("publish.dir does not exist: %s" % out["dir"])
    if not _BRANCH_OK.match(out["branch_prefix"]):
        raise ToolError("publish.branch_prefix must match [a-z0-9/_-]")
    if out["mode"] == "auto":
        a = p.get("auto") or {}
        try:
            out["max_per_day"] = int(a["max_per_day"])
            out["max_per_week"] = int(a["max_per_week"])
            m = _WINDOW.match(str(a["window"]))
            out["tz"] = str(a.get("tz") or "UTC")
        except (KeyError, TypeError, ValueError):
            raise ToolError("publish.mode auto needs auto.max_per_day, auto.max_per_week and auto.window "
                            "(\"HH:MM-HH:MM\"), optional auto.tz") from None
        if not m or not 1 <= out["max_per_day"] <= 10 or not 1 <= out["max_per_week"] <= 50:
            raise ToolError("auto caps: max_per_day 1..10, max_per_week 1..50, window HH:MM-HH:MM")
        out["window"] = ((int(m.group(1)), int(m.group(2))), (int(m.group(3)), int(m.group(4))))
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(out["tz"])
        except Exception:
            raise ToolError("auto.tz is not a known time zone: %s" % scrub(out["tz"], 60)) from None
    return out


def state_files(state_dir):
    return {"hold": os.path.join(state_dir, "content-HOLD"), "stop": os.path.join(state_dir, "content-STOP"),
            "lock": os.path.join(state_dir, "content-publish.lock"), "log": os.path.join(state_dir, "content-published.jsonl")}


def plan_id_for(bundle_dir):
    with open(os.path.join(bundle_dir, "MANIFEST.json"), "rb") as f:
        return "seo-content:" + hashlib.sha256(f.read()).hexdigest()[:32]


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_bundle(bundle_dir):
    """-> (manifest, review) for an approved, intact bundle, else RailViolation."""
    ok, errs = bundle_mod.verify(bundle_dir)
    if not ok:
        raise RailViolation("bundle failed its integrity check: " + "; ".join(str(e) for e in errs)[:400])
    man = _read_json(os.path.join(bundle_dir, "MANIFEST.json"))
    rv = _read_json(os.path.join(bundle_dir, "review.json"))
    ch = _read_json(os.path.join(bundle_dir, "checks.json"))
    if man.get("verdict") != "approve" or rv.get("verdict") != "approve":
        raise RailViolation("bundle is not approved by the adversarial review")
    if ch.get("ok") is not True:
        raise RailViolation("bundle did not pass the deterministic checks")
    if not (rv.get("reviewer") or {}).get("model_answered"):
        raise RailViolation("bundle has no verified reviewer model on record")
    return man, rv


def published(log_path):
    rows = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("ok"):
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def _auto_gate(st, rows, now):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(st["tz"])
    local = dt.datetime.fromtimestamp(now, tz)
    (h1, m1), (h2, m2) = st["window"]
    cur = local.hour * 60 + local.minute
    if not (h1 * 60 + m1) <= cur < (h2 * 60 + m2):
        raise RailViolation("auto publish only inside %02d:%02d-%02d:%02d %s (now %s)" % (h1, m1, h2, m2, st["tz"],
                                                                                         local.strftime("%H:%M")))
    day = local.date()
    week = day.isocalendar()[:2]
    days = [dt.datetime.fromtimestamp(r["at"], tz).date() for r in rows if isinstance(r.get("at"), (int, float))]
    if sum(1 for d in days if d == day) >= st["max_per_day"]:
        raise RailViolation("auto publish cap reached: %d today" % st["max_per_day"])
    if sum(1 for d in days if d.isocalendar()[:2] == week) >= st["max_per_week"]:
        raise RailViolation("auto publish cap reached: %d this week" % st["max_per_week"])


class _Lock:
    def __init__(self, path, stale_s=3600):
        self.path, self.stale_s, self.fd = path, stale_s, None

    def __enter__(self):
        try:
            if time.time() - os.path.getmtime(self.path) > self.stale_s:
                os.unlink(self.path)
        except OSError:
            pass
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise RailViolation("another publish is running (lock %s)" % self.path) from None
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _target_path(base, slug, name):
    """Path for `name` under base/<slug>/, refusing traversal and symlinks anywhere on the way."""
    if not isinstance(slug, str) or not re.match(r"^/[a-z0-9/-]{1,120}/$", slug) or ".." in slug or "//" in slug:
        raise RailViolation("unsafe slug %r" % scrub(str(slug), 120))
    cur = base
    for seg in slug.strip("/").split("/"):
        cur = os.path.join(cur, seg)
        if os.path.islink(cur):
            raise RailViolation("refusing to write through a symlink: %s" % cur)
    path = os.path.join(cur, name)
    if os.path.islink(path):
        raise RailViolation("refusing to overwrite a symlink: %s" % path)
    real = os.path.realpath(path)
    if os.path.commonpath([real, base]) != base:
        raise RailViolation("target escapes publish.dir")
    return path


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".adrails-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _git(argv, cwd, run=None):
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_KEY", "_TOKEN", "_SECRET"))}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if run:
        return run(["git"] + argv, cwd)
    p = subprocess.run(["git"] + argv, cwd=cwd, capture_output=True, text=True, timeout=120, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def publish(bundle_dir, profile, *, state_dir, chain, queue=None, now=time.time, actor="agent", fetch=None, git_run=None,
            dry_run=False):
    """Publish one approved bundle through every gate. -> result dict. Raises RailViolation when a gate refuses."""
    st = settings(profile)
    files = state_files(state_dir)
    os.makedirs(state_dir, exist_ok=True)
    bundle_dir = os.path.realpath(bundle_dir)
    record = {"bundle": bundle_dir, "target": st["target"], "mode": st["mode"], "dry_run": bool(dry_run)}

    def audit(ok, **extra):
        data = dict(record, ok=ok, **extra)
        chain.append("content_publish", data, actor=actor)
        return data

    try:
        if os.path.exists(files["stop"]):
            raise RailViolation("kill switch is on (%s): nothing is published" % files["stop"])
        if os.path.exists(files["hold"]):
            with open(files["hold"], encoding="utf-8", errors="replace") as f:
                why = f.read(400)
            raise RailViolation("publishing is on HOLD after a failed post-publish check (%s). A human must read it and "
                                "delete %s." % (scrub(why.strip(), 300), files["hold"]))
        if st["target"] == "none":
            raise RailViolation("publish.target is \"none\": this site takes Markdown bundles only (give IMPLEMENT.md "
                                "to your own agent)")
        man, rv = check_bundle(bundle_dir)
        pid = plan_id_for(bundle_dir)
        record.update(plan_id=pid, bundle_id=man.get("bundle_id"), reviewer=(rv.get("reviewer") or {}).get("model_answered"))
        rows = published(files["log"])
        if any(r.get("plan_id") == pid for r in rows):
            raise RailViolation("this bundle was already published")
        if st["mode"] == "human":
            if queue is None:
                raise RailViolation("human approval is required: configure approvals (ADRAILS_APPROVAL_SECRET) and "
                                    "request approval for plan_id %s" % pid)
            queue.require_approved(pid)
        else:
            _auto_gate(st, rows, now())
        with open(os.path.join(bundle_dir, "page.md"), "rb") as f:
            page = f.read()
        fm = bundle_mod.read_front_matter(page.decode("utf-8"))
        slug, mode = fm.get("slug"), fm.get("mode")
        if not slug:
            raise RailViolation("page.md front matter has no slug")
        record.update(slug=slug)
        targets = [(_target_path(st["dir"], slug, "index.md"), page)]
        if st["html"] and os.path.exists(os.path.join(bundle_dir, "page.html")):
            with open(os.path.join(bundle_dir, "page.html"), "rb") as f:
                targets.append((_target_path(st["dir"], slug, "index.html"), f.read()))
        for path, _ in targets:
            if os.path.exists(path) and mode != "extend":
                raise RailViolation("a page already exists at %s and this bundle is not an extension of it" % path)
        if dry_run:
            return audit(True, would_write=[p for p, _ in targets], note="dry run: nothing written")
        with _Lock(files["lock"]):
            branch = None
            if st["target"] == "git":
                rc, out = _git(["status", "--porcelain"], st["dir"], git_run)
                if rc != 0 or out.strip():
                    raise RailViolation("publish.dir is not a clean git working tree; refusing to mix changes")
                branch = st["branch_prefix"] + re.sub(r"[^a-z0-9-]", "-", slug.strip("/"))[:50] + "-" + pid[-8:]
                rc, out = _git(["checkout", "-b", branch], st["dir"], git_run)
                if rc != 0:
                    raise RailViolation("could not create branch %s: %s" % (branch, scrub(out, 200)))
            backups = []
            for path, data in targets:
                if os.path.exists(path):
                    bak = path + ".bak-%d" % int(now())
                    os.replace(path, bak)
                    backups.append(bak)
                _write(path, data)
            commit = None
            if st["target"] == "git":
                rels = [os.path.relpath(p, st["dir"]) for p, _ in targets]
                rc, out = _git(["add", "--"] + rels, st["dir"], git_run)
                if rc == 0:
                    msg = "AdRails content: %s (bundle %s, reviewed by %s)" % (slug, man.get("bundle_id"), record["reviewer"])
                    rc, out = _git(["commit", "-m", msg, "--"] + rels, st["dir"], git_run)
                if rc != 0:
                    raise RailViolation("git commit failed: %s" % scrub(out, 200))
                rc, out = _git(["rev-parse", "HEAD"], st["dir"], git_run)
                commit = out.strip()[:40] if rc == 0 else None
            problems = []
            for path, data in targets:
                with open(path, "rb") as f:
                    if hashlib.sha256(f.read()).digest() != hashlib.sha256(data).digest():
                        problems.append("written file differs from the bundle: %s" % path)
            url = None
            if st["verify_url"] and not problems:
                url = (profile.get("site") or "").rstrip("/") + slug
                r = (fetch or _default_fetch)(url)
                title = str(fm.get("title") or "")
                if (r or {}).get("status") != 200 or (title and title not in str((r or {}).get("body") or "")):
                    problems.append("live check failed for %s (status %s)" % (url, (r or {}).get("status")))
            row = dict(record, ok=not problems, at=now(), files=[p for p, _ in targets], backups=backups, branch=branch,
                       commit=commit, url=url, problems=problems)
            with open(files["log"], "a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            if problems:
                with open(files["hold"], "w", encoding="utf-8") as f:
                    f.write("%s HOLD: %s\n" % (dt.datetime.fromtimestamp(now(), dt.timezone.utc).isoformat(),
                                               "; ".join(problems)))
                audit(False, error="post-publish check failed; HOLD written", problems=problems)
                raise RailViolation("post-publish check failed (%s); HOLD written to %s" % ("; ".join(problems)[:300],
                                                                                           files["hold"]))
            return audit(True, files=[p for p, _ in targets], branch=branch, commit=commit, url=url,
                         note=("committed on branch %s; push it and open the pull request yourself" % branch
                               if branch else "written to %s" % st["dir"]))
    except RailViolation as e:
        if "HOLD written" not in str(e):
            audit(False, error=scrub(str(e), 300))
        raise


def _default_fetch(url):
    from ..seo import fetcher
    return fetcher.fetch(url, user_agent="Mozilla/5.0 (compatible; AdRails-content/0.12; +https://adrails.dev/)")


# ── selftest ────────────────────────────────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    import shutil
    import tempfile
    from .. import approvals, auditchain
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "publish.selftest: " + msg
        n += 1

    def refused(fn, needle, msg):
        try:
            fn()
        except RailViolation as e:
            return ck(needle in str(e), msg + " (got: %s)" % e)
        ck(False, msg + " (not refused)")

    tmp = tempfile.mkdtemp(prefix="adrails-pub-")
    try:
        state = os.path.join(tmp, "state")
        site = os.path.join(tmp, "site")
        os.makedirs(site)
        chain = auditchain.AuditChain(os.path.join(tmp, "chain.jsonl"))
        q = approvals.ApprovalQueue(os.path.join(tmp, "appr.jsonl"), b"x" * 32)
        prof = {"site": "https://example.com/", "host": "example.com", "_dir": tmp,
                "author": {"name": "Jane Doe", "url": "/team/jane/", "id": "https://example.com/team/jane/#person"},
                "publish": {"target": "static", "dir": "site"}}
        draft = {"mode": "new", "slug": "/guides/flat-fee/", "title": "Flat fee guide", "description": "d" * 80,
                 "content_type": "guide", "body_md": "Body text here.", "faq": [], "sources": [], "live_changes": [],
                 "internal_links": [], "jsonld": None}
        rv = {"verdict": "approve", "reviewer": {"model_answered": "openai/gpt-5", "family": "openai"}, "findings": []}
        meta = {"bundle_id": "b1", "today": "2026-11-10", "writer_model": "glm-5.3", "reviewer_model": "openai/gpt-5",
                "run_id": "r1"}
        out = bundle_mod.write(os.path.join(tmp, "bundles"), draft, rv, {"ok": True, "errors": [], "warnings": []}, {},
                               prof, meta)
        bdir = out["path"]
        pid = plan_id_for(bdir)
        refused(lambda: publish(bdir, prof, state_dir=state, chain=chain, queue=q), "no request on file",
                "human mode needs an approval")
        req = q.request(pid, "example.com", "publish /guides/flat-fee/")
        refused(lambda: publish(bdir, prof, state_dir=state, chain=chain, queue=q), "pending", "pending is not approved")
        q.decide(req["approve_token"])
        r = publish(bdir, prof, state_dir=state, chain=chain, queue=q, dry_run=True)
        ck(r["ok"] and r["dry_run"] and not os.path.exists(os.path.join(site, "guides/flat-fee/index.md")), "dry run")
        r = publish(bdir, prof, state_dir=state, chain=chain, queue=q)
        target = os.path.join(site, "guides", "flat-fee", "index.md")
        ck(r["ok"] and os.path.exists(target), "published after approval")
        with open(target, "rb") as f, open(os.path.join(bdir, "page.md"), "rb") as g:
            ck(f.read() == g.read(), "published bytes == bundle bytes")
        refused(lambda: publish(bdir, prof, state_dir=state, chain=chain, queue=q), "already published", "no double publish")
        ck(chain.verify().get("ok", True) is not False and any(e["event"] == "content_publish" for e in chain.tail(20)),
           "audit chain records publishes")
        # integrity: tamper with an approved bundle
        out2 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/second/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b2"))
        with open(os.path.join(out2["path"], "page.md"), "a", encoding="utf-8") as f:
            f.write("\nInjected: 68% of owners save $5,000.\n")
        refused(lambda: publish(out2["path"], prof, state_dir=state, chain=chain, queue=q), "integrity", "tamper refused")
        # existing page + new mode refused; symlink refused
        out3 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/flat-fee/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b3"))
        p3 = plan_id_for(out3["path"])
        q.decide(q.request(p3, "example.com", "x")["approve_token"])
        refused(lambda: publish(out3["path"], prof, state_dir=state, chain=chain, queue=q), "already exists",
                "new bundle may not overwrite a page")
        os.makedirs(os.path.join(tmp, "elsewhere"))
        os.symlink(os.path.join(tmp, "elsewhere"), os.path.join(site, "evil"))
        out4 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/evil/x/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b4"))
        q.decide(q.request(plan_id_for(out4["path"]), "example.com", "x")["approve_token"])
        refused(lambda: publish(out4["path"], prof, state_dir=state, chain=chain, queue=q), "symlink", "symlink refused")
        # kill switch and HOLD
        open(state_files(state)["stop"], "w").close()
        refused(lambda: publish(out4["path"], prof, state_dir=state, chain=chain, queue=q), "kill switch", "STOP")
        os.unlink(state_files(state)["stop"])
        # auto mode: caps + window + HOLD tripped by a failed live check
        auto = dict(prof, publish={"target": "static", "dir": "site", "mode": "auto", "verify_url": True,
                                   "auto": {"max_per_day": 1, "max_per_week": 3, "window": "07:00-12:00",
                                            "tz": "America/New_York"}})
        nine_am = dt.datetime(2026, 11, 11, 9, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")).timestamp()
        out5 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/auto-one/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b5"))
        refused(lambda: publish(out5["path"], auto, state_dir=state, chain=chain, now=lambda: nine_am - 3 * 3600),
                "only inside", "outside the window refused")
        r = publish(out5["path"], auto, state_dir=state, chain=chain, now=lambda: nine_am,
                    fetch=lambda u: {"status": 200, "body": "<h1>Flat fee guide</h1>"})
        ck(r["ok"], "auto publish inside window with a passing live check")
        out6 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/auto-two/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b6"))
        refused(lambda: publish(out6["path"], auto, state_dir=state, chain=chain, now=lambda: nine_am + 60),
                "cap reached", "daily cap")
        refused(lambda: publish(out6["path"], auto, state_dir=state, chain=chain, now=lambda: nine_am + 86400,
                                fetch=lambda u: {"status": 500, "body": ""}), "HOLD written", "failed live check trips HOLD")
        ck(os.path.exists(state_files(state)["hold"]), "HOLD file exists")
        out7 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/auto-three/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b7"))
        refused(lambda: publish(out7["path"], auto, state_dir=state, chain=chain, now=lambda: nine_am + 2 * 86400),
                "HOLD", "HOLD stops later publishes")
        try:
            settings(dict(prof, publish={"target": "static", "dir": "site", "mode": "auto"}))
            ck(False, "auto without caps must be refused")
        except ToolError:
            ck(True, "auto without caps refused")
        # not-approved bundle cannot even be written
        try:
            bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/held/"), dict(rv, verdict="hold"),
                             {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b8"))
            ck(False, "held review must not produce a bundle")
        except ToolError:
            ck(True, "held review produces no bundle")
        # git publisher: new branch + one commit, never a push
        calls = []

        def git_run(argv, cwd):
            calls.append(argv)
            if argv[1:3] == ["status", "--porcelain"]:
                return 0, ""
            if argv[1] == "rev-parse":
                return 0, "abc123\n"
            return 0, ""
        gsite = os.path.join(tmp, "gsite")
        os.makedirs(gsite)
        gprof = dict(prof, publish={"target": "git", "dir": "gsite"})
        out9 = bundle_mod.write(os.path.join(tmp, "bundles"), dict(draft, slug="/guides/git-one/"), rv,
                                {"ok": True, "errors": [], "warnings": []}, {}, prof, dict(meta, bundle_id="b9"))
        q.decide(q.request(plan_id_for(out9["path"]), "example.com", "x")["approve_token"])
        os.unlink(state_files(state)["hold"])
        r = publish(out9["path"], gprof, state_dir=state, chain=chain, queue=q, git_run=git_run)
        ck(r["ok"] and r["branch"].startswith("adrails/content-") and r["commit"] == "abc123", "git branch + commit")
        ck(not any("push" in a for a in calls) and any(a[1] == "checkout" for a in calls), "git never pushes")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return n
