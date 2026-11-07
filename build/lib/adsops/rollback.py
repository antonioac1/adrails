"""Git-backed history and guarded one-command rollback for AdRails state files.

This module never touches git or the filesystem directly: every effect is an
injected callable, so tools and tests can mock it.  It validates commit
references to prevent git argument injection, treats guards/commit envelopes as
non-revertible, and enforces an add-only rail for negative keywords.  A tool
should use plan_rollback (read-only), then write_and_commit, then the normal
plan/apply path.  Errors are ToolError with scrubbed text; no secrets are
returned or logged.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys

from adsops.errors import ToolError, RailViolation, scrub

_IGNORED_KEYS = {"guards", "commit_policy"}
_PROTECTED_KEYS = {"guards", "commit_policy", "customer_id", "campaign_id"}
_NEG_KEY = "negatives_add"
_COMMIT_RE = re.compile(r"^[0-9a-f]{4,40}$")
_HEAD_RE = re.compile(r"^HEAD~(\d{1,2})$")


def _g(row, *path, default=None):
    """Nested dict/list getter used for Google Ads REST-shaped rows."""
    cur = row
    for key in path:
        if isinstance(cur, dict):
            if key not in cur:
                return default
            cur = cur[key]
        elif isinstance(cur, (list, tuple)) and isinstance(key, int):
            if key < 0 or key >= len(cur):
                return default
            cur = cur[key]
        else:
            return default
    return cur


def _state_file_parts(state_file):
    if not isinstance(state_file, str) or not state_file.strip():
        raise ToolError("state_file must be a non-empty path")
    base = os.path.basename(state_file)
    if not base:
        raise ToolError("state_file must name a file")
    cwd = os.path.dirname(state_file) or "."
    return cwd, base


def _validate_commit(commit):
    """Only short/full lowercase SHA or HEAD~N (N<=50) may reach git."""
    if not isinstance(commit, str):
        raise ToolError("commit must be a string")
    if _COMMIT_RE.match(commit):
        return commit
    m = _HEAD_RE.match(commit)
    if m and int(m.group(1)) <= 50:
        return commit
    raise ToolError("invalid commit reference")


def history(state_file, runner, limit=20):
    """Return recent commits touching state_file, newest first."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ToolError("limit must be a positive integer")
    cwd, base = _state_file_parts(state_file)
    args = [
        "git", "log", "-n", str(limit),
        "--format=%h%x1f%aI%x1f%an%x1f%s", "--", base,
    ]
    rc, out, err = runner(args, cwd)
    if rc != 0:
        raise ToolError(scrub(err or out or "git log failed"))
    result = []
    for line in (out or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue  # malformed git output is ignored, not fatal
        result.append({
            "commit": parts[0],
            "at": parts[1],
            "author": parts[2],
            "subject": parts[3],
        })
    return result


def show(state_file, commit, runner):
    """Return parsed JSON object for state_file at a validated commit."""
    commit = _validate_commit(commit)
    cwd, base = _state_file_parts(state_file)
    args = ["git", "show", f"{commit}:./{base}"]
    rc, out, err = runner(args, cwd)
    if rc != 0:
        raise ToolError(scrub(err or "git show failed"))
    try:
        data = json.loads(out)
    except Exception:
        raise ToolError(f"state at {commit} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ToolError(f"state at {commit} is not a JSON object")
    return data


def _neg_key(item):
    if isinstance(item, dict):
        text = item.get("text")
        match_type = item.get("match_type", "EXACT")
    else:
        text = item
        match_type = "EXACT"
    return ("" if text is None else str(text),
            "" if match_type is None else str(match_type))


def _neg_text(item):
    return _neg_key(item)[0]


def _as_neg_list(value):
    return value if isinstance(value, list) else []


def _neg_union(current_list, target_list):
    result = []
    seen = set()
    for item in list(current_list) + list(target_list):
        key = _neg_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(copy.deepcopy(item))
    return result


def diff_states(old, new):
    """Diff top-level revertible fields plus negative-add/removal entries."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ToolError("states must be JSON objects")
    changes = []
    for key in sorted(set(old) | set(new)):
        if key.startswith("_") or key in _IGNORED_KEYS or key == _NEG_KEY:
            continue
        if (key in old) != (key in new) or old.get(key) != new.get(key):
            changes.append({
                "field": key,
                "from": copy.deepcopy(old.get(key)),
                "to": copy.deepcopy(new.get(key)),
            })

    old_neg = _as_neg_list(old.get(_NEG_KEY))
    new_neg = _as_neg_list(new.get(_NEG_KEY))
    old_map, new_map = {}, {}
    for item in old_neg:
        old_map.setdefault(_neg_key(item), item)
    for item in new_neg:
        new_map.setdefault(_neg_key(item), item)
    for key, item in old_map.items():
        if key not in new_map:
            changes.append({"field": _NEG_KEY, "from": _neg_text(item), "to": None})
    for key, item in new_map.items():
        if key not in old_map:
            changes.append({"field": _NEG_KEY, "from": None, "to": _neg_text(item)})
    return changes


def revert_state(current, target):
    """Build a state equal to target for revertible fields, with add-only negatives.

    guards/commit_policy/customer_id/campaign_id always come from current.
    Negatives present in current are never removed, even if target lacks them.
    """
    if not isinstance(current, dict) or not isinstance(target, dict):
        raise ToolError("states must be JSON objects")

    result = copy.deepcopy(current)
    result.pop("_rollback_note", None)
    result.pop("_rollback_note", None)

    # Apply target to current-only/target-only revertible ordinary fields.
    for key in list(result.keys()):
        if key in _PROTECTED_KEYS or key.startswith("_") or key == _NEG_KEY:
            continue
        if key in target:
            result[key] = copy.deepcopy(target[key])
        else:
            del result[key]
    for key, value in target.items():
        if key in _PROTECTED_KEYS or key.startswith("_") or key == _NEG_KEY:
            continue
        if key not in result:
            result[key] = copy.deepcopy(value)

    current_neg = _as_neg_list(current.get(_NEG_KEY))
    target_neg = _as_neg_list(target.get(_NEG_KEY))
    union = _neg_union(current_neg, target_neg)
    if union or _NEG_KEY in current or _NEG_KEY in target:
        result[_NEG_KEY] = union
    else:
        result.pop(_NEG_KEY, None)

    target_keys = {_neg_key(item) for item in target_neg}
    kept = []
    seen = set()
    for item in current_neg:
        key = _neg_key(item)
        if key not in target_keys and key not in seen:
            kept.append(_neg_text(item))
            seen.add(key)
    if kept:
        result["_rollback_note"] = "negatives kept (not removed by revert): " + ", ".join(kept)
    return result


def _default_read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise ToolError(scrub(str(exc))) from None


def _default_write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        raise ToolError(scrub(str(exc))) from None


def plan_rollback(state_file, to_commit, runner, read=None):
    """Read-only rollback plan: no file write and no git mutation."""
    reader = read or _default_read
    cwd, _base = _state_file_parts(state_file)
    try:
        raw = reader(state_file)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(scrub(str(exc))) from None
    try:
        current = json.loads(raw)
    except Exception:
        raise ToolError("current state is not valid JSON") from None
    if not isinstance(current, dict):
        raise ToolError("current state must be a JSON object")

    target = show(state_file, to_commit, runner)
    new_state = revert_state(current, target)
    changes = diff_states(current, new_state)
    note = "already at that state" if not changes else ""
    return {
        "to_commit": to_commit,
        "changes": changes,
        "new_state": new_state,
        "note": note,
    }


def _sha_from_commit_output(out):
    m = re.search(r"\[[^\]]*\s([0-9a-f]{4,40})\]", out or "")
    if m:
        return m.group(1)
    text = (out or "").strip()
    return text if re.fullmatch(r"[0-9a-f]{4,40}", text) else ""


def write_and_commit(state_file, new_state, message, runner, write=None):
    """Write JSON state, git add it, commit it, and return a short commit sha."""
    if not isinstance(new_state, dict):
        raise ToolError("new_state must be a JSON object")
    if not isinstance(message, str) or not message.strip():
        raise ToolError("commit message is required")
    cwd, base = _state_file_parts(state_file)

    payload = json.dumps(new_state, indent=2, sort_keys=False) + "\n"
    writer = write or _default_write
    try:
        writer(state_file, payload)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(scrub(str(exc))) from None

    rc, out, err = runner(["git", "add", "--", base], cwd)
    if rc != 0:
        raise ToolError(scrub(err or out or "git add failed"))

    rc, out, err = runner(
        ["git", "commit", "--only", "-m", message, "--", base], cwd
    )
    if rc != 0:
        raise ToolError(scrub(err or out or "git commit failed"))

    sha = _sha_from_commit_output(out)
    if sha:
        return sha

    # Some git versions/mocks do not print the sha; ask git directly.
    try:
        rc, rev, err = runner(["git", "rev-parse", "--short", "HEAD"], cwd)
    except Exception:
        return (out or "").strip()[:40]
    if rc == 0 and rev.strip():
        return rev.strip()
    return (out or "").strip()[:40]


def selftest() -> int:
    checks = 0

    def ok(cond, msg="assertion failed"):
        nonlocal checks
        if not cond:
            raise AssertionError(msg)
        checks += 1

    # _g helper
    row = {"a": {"b": {"c": "3"}}, "arr": [{"x": 1}]}
    ok(_g(row, "a", "b", "c") == "3")
    ok(_g(row, "a", "missing", default=0) == 0)
    ok(_g(row, "arr", 0, "x") == 1)

    calls = []

    def runner(args, cwd):
        calls.append((list(args), cwd))
        if args[0] != "git":
            return 1, "", "not git"
        sub = args[1]
        if sub == "log":
            return 0, (
                "abc123\x1f2024-01-01T00:00:00+00:00\x1fAlice\x1fAdd\n"
                "\n"
                "badline\n"
            ), ""
        if sub == "show":
            if args[2].startswith("deadbee:"):
                payload = {
                    "campaign_id": "c2",
                    "budget": 20,
                    "guards": {"x": 2},
                    "negatives_add": [{"text": "old", "match_type": "EXACT"}],
                }
            else:
                payload = {
                    "campaign_id": "c1",
                    "budget": 10,
                    "guards": {"x": 1},
                    "negatives_add": [{"text": "new", "match_type": "EXACT"}],
                }
            return 0, json.dumps(payload), ""
        if sub == "add":
            return 0, "", ""
        if sub == "commit":
            return 0, "[main cafe123] msg\n", ""
        if sub == "rev-parse":
            return 0, "cafe123\n", ""
        return 1, "", "unexpected"

    hist = history("/tmp/state.json", runner, limit=5)
    ok(len(hist) == 1)
    ok(hist[0]["commit"] == "abc123")
    ok(calls[0][1] == "/tmp")
    ok(calls[0][0][-2:] == ["--", "state.json"])

    ok(history("state.json", lambda a, c: (0, "", "")) == [])

    try:
        history("x", runner, limit=0)
    except ToolError:
        ok(True)
    else:
        ok(False, "expected ToolError for bad limit")

    target = show("/tmp/state.json", "deadbee", runner)
    ok(target["budget"] == 20)
    ok(calls[-1][0][2] == "deadbee:./state.json")

    try:
        show("/tmp/state.json", "-x", runner)
    except ToolError:
        ok(True)
    else:
        ok(False, "expected ToolError for injected commit")
    try:
        show("/tmp/state.json", "HEAD~51", runner)
    except ToolError:
        ok(True)
    else:
        ok(False, "expected ToolError for HEAD~51")

    try:
        show("/tmp/s.json", "deadbee", lambda a, c: (0, "not json", ""))
    except ToolError:
        ok(True)
    else:
        ok(False, "expected ToolError for malformed show JSON")

    ok(show("/tmp/s.json", "HEAD~50", lambda a, c: (0, "{}", "")) == {})

    old = {
        "budget": 10,
        "name": "a",
        "guards": {"g": 1},
        "_x": 1,
        "negatives_add": [{"text": "a", "match_type": "EXACT"}],
    }
    new = {
        "budget": 20,
        "name": "a",
        "guards": {"g": 2},
        "_x": 2,
        "negatives_add": [{"text": "b", "match_type": "EXACT"}],
    }
    d = diff_states(old, new)
    ok(len(d) == 3)
    ok(any(c["field"] == "budget" and c["from"] == 10 and c["to"] == 20 for c in d))
    ok(any(c["field"] == _NEG_KEY and c["from"] == "a" and c["to"] is None for c in d))
    ok(any(c["field"] == _NEG_KEY and c["from"] is None and c["to"] == "b" for c in d))

    d2 = diff_states(
        {"negatives_add": [{"text": "a", "match_type": "EXACT"}]},
        {"negatives_add": [{"text": "a", "match_type": "PHRASE"}]},
    )
    ok(len(d2) == 2)

    cur = {
        "campaign_id": "c1",
        "budget": 30,
        "name": "new",
        "guards": {"g": 2},
        "commit_policy": {"p": 2},
        "_meta": 1,
        "new_only": True,
        "negatives_add": [{"text": "keep", "match_type": "EXACT"}],
    }
    tgt = {
        "campaign_id": "c2",
        "budget": 10,
        "name": "old",
        "guards": {"g": 1},
        "commit_policy": {"p": 1},
        "negatives_add": [{"text": "old", "match_type": "EXACT"}],
    }
    r = revert_state(cur, tgt)
    ok(r["campaign_id"] == "c1")
    ok(r["guards"] == {"g": 2})
    ok(r["commit_policy"] == {"p": 2})
    ok(r["budget"] == 10)
    ok(r["name"] == "old")
    ok("new_only" not in r)
    ok(len(r["negatives_add"]) == 2)
    ok("_rollback_note" in r and "keep" in r["_rollback_note"])
    ok(r["_meta"] == 1)

    ok(revert_state({"a": 1, "b": 2}, {"a": 3}) == {"a": 3})

    same = {
        "campaign_id": "c1",
        "budget": 10,
        "negatives_add": [{"text": "a", "match_type": "EXACT"}],
    }
    p = plan_rollback(
        "/tmp/state.json", "deadbee",
        lambda a, c: (0, json.dumps(same), "") if a[1] == "show" else (0, "", ""),
        read=lambda path: json.dumps(same),
    )
    ok(p["changes"] == [])
    ok(p["note"] == "already at that state")
    ok(p["new_state"] == same)

    cur2 = {
        "budget": 20,
        "negatives_add": [{"text": "keep", "match_type": "EXACT"}],
    }
    tgt2 = {
        "budget": 10,
        "negatives_add": [{"text": "old", "match_type": "EXACT"}],
    }
    p2 = plan_rollback(
        "/tmp/state.json", "deadbee",
        lambda a, c: (0, json.dumps(tgt2), "") if a[1] == "show" else (0, "", ""),
        read=lambda path: json.dumps(cur2),
    )
    ok(any(c["field"] == "budget" for c in p2["changes"]))
    ok(len(p2["new_state"]["negatives_add"]) == 2)
    ok(p2["note"] == "")

    try:
        plan_rollback("/tmp/s.json", "deadbee", runner, read=lambda p: "{bad")
    except ToolError:
        ok(True)
    else:
        ok(False, "expected ToolError for malformed current JSON")

    wrote = {}

    def writer(path, text):
        wrote["path"] = path
        wrote["text"] = text

    def runner3(args, cwd):
        if args[1] == "add":
            return 0, "", ""
        if args[1] == "commit":
            return 0, "[main cafe123] msg\n", ""
        if args[1] == "rev-parse":
            return 0, "cafe123\n", ""
        return 1, "", "unexpected"

    sha = write_and_commit("/tmp/state.json", {"budget": 10}, "msg", runner3, write=writer)
    ok(sha == "cafe123")
    ok(wrote["path"] == "/tmp/state.json")
    ok(wrote["text"].endswith("\n"))
    ok(json.loads(wrote["text"]) == {"budget": 10})

    def runner_fail(args, cwd):
        if args[1] == "add":
            return 0, "", ""
        if args[1] == "commit":
            return 1, "", "fatal: nope"
        return 0, "", ""

    try:
        write_and_commit("/tmp/s.json", {}, "m", runner_fail, write=lambda p, t: None)
    except ToolError as exc:
        ok("fatal: nope" in str(exc))
    else:
        ok(False, "expected ToolError for failed commit")

    try:
        write_and_commit("/tmp/s.json", {}, "", runner_fail, write=lambda p, t: None)
    except ToolError:
        ok(True)
    else:
        ok(False, "expected ToolError for empty message")

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)
