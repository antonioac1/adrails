"""Desired-state store: apply a change set to a state file and commit it to git.

Every agent-made change becomes a git commit, so the desired state always has an author, a reason
and a diff. Guards are never writable from here; only the operating fields are.
"""
import copy
import json
import os
import re
import subprocess
import time

from adsops import strict
from adsops.errors import RailViolation, ToolError

SETTABLE = ("daily_budget_usd", "status", "end_date", "negatives_add")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def apply_changes(state, changes):
    """Return (new_state, summary list). Pure. Refuses guard edits and malformed values."""
    if not isinstance(changes, dict):
        raise ToolError("changes must be an object")
    bad = sorted(set(changes) - set(SETTABLE))
    if bad:
        raise RailViolation(f"not settable: {', '.join(bad)} (only {', '.join(SETTABLE)})")
    new, summary = copy.deepcopy(state), []
    if "daily_budget_usd" in changes:
        b = strict.finite_number(changes["daily_budget_usd"], "daily_budget_usd", 0, allow_equal_min=False)
        b = round(float(b), 2)
        if b < 0.01:
            raise ToolError("daily_budget_usd must be at least 0.01")
        if b != state.get("daily_budget_usd"):
            summary.append(f"budget {state.get('daily_budget_usd')} -> {b}")
        new["daily_budget_usd"] = b
    if "status" in changes:
        s = changes["status"]
        if s not in ("ENABLED", "PAUSED"):
            raise ToolError("status must be ENABLED or PAUSED")
        if s != state.get("status"):
            summary.append(f"status {state.get('status')} -> {s}")
        new["status"] = s
    if "end_date" in changes:
        e = changes["end_date"]
        strict.iso_date(e, "end_date")
        if e != state.get("end_date"):
            summary.append(f"end {state.get('end_date')} -> {e}")
        new["end_date"] = e
    if "negatives_add" in changes:
        have = {(n["text"].strip().lower(), n.get("match_type", "PHRASE").upper())
                for n in state.get("negatives_add", [])}
        added = []
        for n in strict.negatives_list(changes["negatives_add"]):
            key = (n["text"], n["match_type"])
            if key not in have:
                have.add(key)
                added.append({"text": key[0], "match_type": key[1]})
        if added:
            new["negatives_add"] = list(state.get("negatives_add", [])) + added
            summary.append(f"+{len(added)} negatives")
    return new, summary


def _git(runner, cwd, *args):
    for attempt in range(6):
        r = runner(["git", *args], cwd=cwd, capture_output=True, text=True)
        if r.returncode == 0 or "index.lock" not in (r.stderr or ""):
            return r
        time.sleep(2)          # another writer (lane / janitor) mid-commit; never delete its lock
    return r


def write_and_commit(path, new_state, message, runner=subprocess.run):
    """Atomically write the state file, then `git commit --only` it. Returns the commit sha."""
    d = os.path.dirname(os.path.abspath(path))
    top = _git(runner, d, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ToolError("state file is not inside a git repository")
    root = top.stdout.strip()
    rel = os.path.relpath(os.path.abspath(path), root)
    tmp = path + ".adsops-tmp"
    with open(tmp, "w") as fh:
        json.dump(new_state, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    for args in (("add", "--", rel), ("commit", "--only", "-m", message, "--", rel)):
        r = _git(runner, root, *args)
        if r.returncode != 0:
            raise ToolError(f"git {args[0]} failed: {(r.stderr or r.stdout or '').strip()[:300]}")
    head = _git(runner, root, "rev-parse", "HEAD")
    return head.stdout.strip()


def selftest():
    import tempfile
    n = 0
    st = {"customer_id": "1", "campaign_id": 2, "campaign_name": "c", "status": "PAUSED", "daily_budget_usd": 8,
          "negatives_add": [{"text": "free", "match_type": "PHRASE"}], "guards": {"max_daily_budget_usd": 25}}
    new, s = apply_changes(st, {"daily_budget_usd": 12.345, "status": "ENABLED"})
    assert new["daily_budget_usd"] == 12.35 and new["status"] == "ENABLED" and len(s) == 2; n += 1
    assert st["status"] == "PAUSED"; n += 1                                     # input untouched
    for bad in ({"guards": {}}, {"allow_enable": True}, {"campaign_id": 9}):
        try:
            apply_changes(st, bad)
            raise AssertionError(bad)
        except RailViolation:
            n += 1
    for bad in ({"daily_budget_usd": True}, {"daily_budget_usd": -1}, {"status": "REMOVED"},
                {"end_date": "11/30/2026"}, {"negatives_add": [{"text": " "}]}):
        try:
            apply_changes(st, bad)
            raise AssertionError(bad)
        except ToolError:
            n += 1
    new, s = apply_changes(st, {"negatives_add": [{"text": "FREE "}, {"text": "jobs", "match_type": "exact"},
                                                  {"text": "jobs", "match_type": "EXACT"}]})
    assert new["negatives_add"][-1] == {"text": "jobs", "match_type": "EXACT"} and len(new["negatives_add"]) == 2; n += 1
    assert apply_changes(st, {"daily_budget_usd": 8})[1] == []; n += 1          # no-op is detectable

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "s.json")
    json.dump(st, open(p, "w"))
    seen = []

    class R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    locks = {"n": 1}

    def runner(cmd, cwd=None, **kw):
        seen.append(cmd[1:])
        if cmd[1] == "rev-parse" and cmd[2] == "--show-toplevel":
            return R(0, tmp + "\n")
        if cmd[1] == "commit" and locks["n"]:
            locks["n"] -= 1
            return R(128, "", "fatal: Unable to create '.git/index.lock': File exists.")
        if cmd[1] == "rev-parse":
            return R(0, "abc123\n")
        return R(0)
    global time
    real_sleep, time.sleep = time.sleep, (lambda s: None)
    try:
        sha = write_and_commit(p, new, "msg", runner=runner)
    finally:
        time.sleep = real_sleep
    assert sha == "abc123" and json.load(open(p))["negatives_add"][-1]["text"] == "jobs"; n += 1
    assert ["commit", "--only", "-m", "msg", "--", "s.json"] in seen and seen.count(["commit", "--only", "-m", "msg", "--", "s.json"]) == 2; n += 1
    assert not os.path.exists(p + ".adsops-tmp"); n += 1

    def failing(cmd, cwd=None, **kw):
        return R(1, "", "nope") if cmd[1] == "rev-parse" else R(0)
    try:
        write_and_commit(p, new, "m", runner=failing)
        raise AssertionError("not a repo")
    except ToolError:
        n += 1
    return n
