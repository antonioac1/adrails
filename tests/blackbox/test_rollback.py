"""Tests for adsops.rollback against the documented spec only.

All git interaction is injected/mocked; filesystem I/O uses tempfile.
"""

import copy
import json
import os
import tempfile
import unittest

from adsops.rollback import (
    ToolError,
    diff_states,
    history,
    plan_rollback,
    revert_state,
    show,
    write_and_commit,
)


def _git_line(sha, at, author, subject):
    """One line in the format requested by history()."""
    return "\x1f".join([sha, at, author, subject])


class RecordingRunner:
    """Injected git runner that records calls and replays canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        if not self.responses:
            raise AssertionError("unexpected git call: %r (cwd=%r)" % (args, cwd))
        return self.responses.pop(0)


def _neg(text, match_type="literal"):
    return {"text": text, "match_type": match_type}


def _neg_keys(state):
    return {(n["text"], n["match_type"]) for n in state.get("negatives_add", [])}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.state_file = os.path.join(tempfile.gettempdir(), "repo", "desired.json")
        self.dirname = os.path.dirname(self.state_file)

    def test_history_command_and_parsing(self):
        out = "\n".join(
            [
                _git_line("abc1234", "2024-01-02T03:04:05+00:00", "Ann", "Init"),
                _git_line("def5678", "2024-01-02T03:05:06+00:00", "Bob", "Tune"),
            ]
        ) + "\n"
        runner = RecordingRunner((0, out, ""))
        result = history(self.state_file, runner)

        args, cwd = runner.calls[0]
        self.assertEqual(
            args,
            ["git", "log", "-n", "20", "--format=%h%x1f%aI%x1f%an%x1f%s", "--", "desired.json"],
        )
        self.assertEqual(cwd, self.dirname)
        self.assertEqual(
            result[0],
            {
                "commit": "abc1234",
                "at": "2024-01-02T03:04:05+00:00",
                "author": "Ann",
                "subject": "Init",
            },
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["commit"], "def5678")

    def test_history_limit_is_forwarded(self):
        runner = RecordingRunner((0, _git_line("abc1234", "2024-01-01T00:00:00Z", "A", "s"), ""))
        history(self.state_file, runner, limit=5)
        args, _ = runner.calls[0]
        self.assertIn("-n", args)
        self.assertEqual(args[args.index("-n") + 1], "5")

    def test_history_keeps_spaces_and_unicode_in_subject(self):
        out = _git_line("aaaa111", "2024-02-02T00:00:00Z", "Zoë", "raise budget — Q3")
        runner = RecordingRunner((0, out, ""))
        result = history(self.state_file, runner)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["subject"], "raise budget — Q3")
        self.assertEqual(result[0]["author"], "Zoë")


class ShowTests(unittest.TestCase):
    def setUp(self):
        self.state_file = os.path.join(tempfile.gettempdir(), "repo", "desired.json")
        self.dirname = os.path.dirname(self.state_file)
        self.payload = {"budget": 100, "guards": {"x": 1}}

    def test_show_command(self):
        runner = RecordingRunner((0, json.dumps(self.payload), ""))
        show(self.state_file, "abc1234", runner)
        args, cwd = runner.calls[0]
        self.assertEqual(args, ["git", "show", "abc1234:./desired.json"])
        self.assertEqual(cwd, self.dirname)

    def test_show_returns_parsed_json(self):
        runner = RecordingRunner((0, json.dumps(self.payload), ""))
        self.assertEqual(show(self.state_file, "abc1234", runner), self.payload)

    def test_show_accepts_head_tilde(self):
        for commit in ("HEAD~1", "HEAD~50"):
            with self.subTest(commit=commit):
                runner = RecordingRunner((0, json.dumps(self.payload), ""))
                show(self.state_file, commit, runner)
                args, _ = runner.calls[0]
                self.assertEqual(args, ["git", "show", commit + ":./desired.json"])

    def test_show_rejects_malformed_commit(self):
        bad = ["abc", "ABCD", "a" * 41, "zzzz", "HEAD~51", "HEAD~x", "dead beef"]
        for commit in bad:
            with self.subTest(commit=commit):
                runner = RecordingRunner()
                with self.assertRaises(ToolError):
                    show(self.state_file, commit, runner)
                self.assertEqual(runner.calls, [])

    def test_show_rejects_dash_prefixed_commit(self):
        for commit in ("-n", "--upload-pack=evil", "-"):
            with self.subTest(commit=commit):
                runner = RecordingRunner()
                with self.assertRaises(ToolError):
                    show(self.state_file, commit, runner)
                self.assertEqual(runner.calls, [])


class DiffStatesTests(unittest.TestCase):
    def test_reports_changed_top_level_field(self):
        d = diff_states({"budget": 100, "geo": "US"}, {"budget": 150, "geo": "US"})
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["field"], "budget")
        self.assertEqual(d[0]["from"], 100)
        self.assertEqual(d[0]["to"], 150)

    def test_reports_added_and_removed_keys(self):
        added = diff_states({"a": 1}, {"a": 1, "b": 2})
        self.assertEqual([i["field"] for i in added], ["b"])
        self.assertEqual(added[0]["to"], 2)

        removed = diff_states({"a": 1, "b": 2}, {"a": 1})
        self.assertEqual([i["field"] for i in removed], ["b"])
        self.assertEqual(removed[0]["from"], 2)

    def test_ignores_envelope_and_underscore_keys(self):
        old = {"guards": {"x": 1}, "commit_policy": {"y": 2}, "_rollback_note": "a", "budget": 10}
        new = {"guards": {"x": 9}, "commit_policy": {"y": 8}, "_rollback_note": "b", "budget": 10}
        self.assertEqual(diff_states(old, new), [])

    def test_identical_states_produce_no_changes(self):
        state = {"budget": 10, "negatives_add": [_neg("spam")]}
        self.assertEqual(diff_states(state, copy.deepcopy(state)), [])

    def test_detects_added_negative(self):
        old = {"negatives_add": [_neg("spam")]}
        new = {"negatives_add": [_neg("spam"), _neg("eggs", "regex")]}
        d = diff_states(old, new)
        self.assertTrue(d)
        self.assertTrue(any("negatives_add" in str(item.get("field", "")) for item in d))

    def test_detects_removed_negative(self):
        old = {"negatives_add": [_neg("spam"), _neg("eggs", "regex")]}
        new = {"negatives_add": [_neg("spam")]}
        d = diff_states(old, new)
        self.assertTrue(d)
        self.assertTrue(any("negatives_add" in str(item.get("field", "")) for item in d))


class RevertStateTests(unittest.TestCase):
    def test_revertible_fields_take_target_value(self):
        current = {"budget": 100, "targeting": {"geo": "US"}}
        target = {"budget": 50, "targeting": {"geo": "EU"}}
        out = revert_state(current, target)
        self.assertEqual(out["budget"], 50)
        self.assertEqual(out["targeting"], {"geo": "EU"})

    def test_envelope_fields_stay_from_current(self):
        current = {
            "customer_id": "c1",
            "campaign_id": "p1",
            "guards": {"g": 1},
            "commit_policy": {"x": 1},
            "budget": 100,
        }
        target = {
            "customer_id": "c2",
            "campaign_id": "p2",
            "guards": {"g": 9},
            "commit_policy": {"x": 9},
            "budget": 50,
        }
        out = revert_state(current, target)
        self.assertEqual(out["customer_id"], "c1")
        self.assertEqual(out["campaign_id"], "p1")
        self.assertEqual(out["guards"], {"g": 1})
        self.assertEqual(out["commit_policy"], {"x": 1})
        self.assertEqual(out["budget"], 50)

    def test_negatives_are_add_only_union(self):
        current = {"negatives_add": [_neg("a"), _neg("b")]}
        target = {"negatives_add": [_neg("a"), _neg("c")]}
        out = revert_state(current, target)
        # b was removed in target but must never be removed by a rollback.
        self.assertEqual(_neg_keys(out), {("a", "literal"), ("b", "literal"), ("c", "literal")})
        self.assertIn("_rollback_note", out)

    def test_negatives_distinguished_by_match_type(self):
        current = {"negatives_add": [_neg("x", "literal")]}
        target = {"negatives_add": [_neg("x", "regex")]}
        out = revert_state(current, target)
        self.assertEqual(_neg_keys(out), {("x", "literal"), ("x", "regex")})

    def test_inputs_are_not_mutated(self):
        current = {"budget": 100, "guards": {"g": 1}, "negatives_add": [_neg("a")]}
        target = {"budget": 50, "guards": {"g": 9}, "negatives_add": [_neg("b")]}
        current_snapshot = copy.deepcopy(current)
        target_snapshot = copy.deepcopy(target)

        out = revert_state(current, target)

        self.assertEqual(current, current_snapshot)
        self.assertEqual(target, target_snapshot)
        self.assertIsNot(out, current)


class PlanRollbackTests(unittest.TestCase):
    def setUp(self):
        self.state_file = os.path.join(tempfile.gettempdir(), "repo", "desired.json")
        self.current = {
            "customer_id": "c1",
            "guards": {"g": 1},
            "budget": 100,
            "negatives_add": [_neg("spam")],
        }
        self.target = {
            "customer_id": "c2",
            "guards": {"g": 9},
            "budget": 50,
            "negatives_add": [_neg("spam"), _neg("eggs")],
        }

    def test_returns_documented_keys_and_uses_injected_read(self):
        reads = []

        def fake_read(path):
            reads.append(path)
            return json.dumps(self.current)

        runner = RecordingRunner((0, json.dumps(self.target), ""))
        result = plan_rollback(self.state_file, "abc1234", runner, read=fake_read)

        self.assertEqual(reads, [self.state_file])
        self.assertEqual(result["to_commit"], "abc1234")
        expected_new_state = revert_state(self.current, self.target)
        self.assertEqual(result["new_state"], expected_new_state)
        self.assertEqual(result["changes"], diff_states(self.current, expected_new_state))
        self.assertIn("note", result)

    def test_no_revertible_changes_reports_already_at_state(self):
        current = {"budget": 100, "guards": {"g": 1}, "commit_policy": {"p": 1}}
        target = {"budget": 100, "guards": {"g": 9}, "commit_policy": {"p": 8}}
        runner = RecordingRunner((0, json.dumps(target), ""))
        result = plan_rollback(self.state_file, "deadbeef", runner, read=lambda p: json.dumps(current))
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["note"], "already at that state")
        self.assertEqual(result["new_state"]["guards"], {"g": 1})

    def test_invalid_commit_refused_without_touching_git(self):
        runner = RecordingRunner()
        with self.assertRaises(ToolError):
            plan_rollback(self.state_file, "-rf", runner, read=lambda p: json.dumps(self.current))
        self.assertEqual(runner.calls, [])

    def test_default_read_reads_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "desired.json")
            with open(state_file, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.current))

            runner = RecordingRunner((0, json.dumps(self.target), ""))
            result = plan_rollback(state_file, "abc1234", runner)

        self.assertEqual(result["to_commit"], "abc1234")
        self.assertEqual(result["new_state"], revert_state(self.current, self.target))
        self.assertTrue(result["changes"])

    def test_planning_issues_no_write_commands(self):
        runner = RecordingRunner((0, json.dumps(self.target), ""))
        plan_rollback(self.state_file, "abc1234", runner, read=lambda p: json.dumps(self.current))

        self.assertTrue(runner.calls)
        for args, _cwd in runner.calls:
            self.assertEqual(args[0], "git")
            self.assertEqual(args[1], "show")
            self.assertNotIn("add", args)
            self.assertNotIn("commit", args)


class WriteAndCommitTests(unittest.TestCase):
    SHA = "1234567890abcdef1234567890abcdef12345678"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dirname = self._tmp.name
        self.state_file = os.path.join(self.dirname, "desired.json")
        self.new_state = {"budget": 50, "targeting": {"geo": "EU"}}

    def test_writes_json_indent_two_with_trailing_newline(self):
        runner = RecordingRunner((0, "", ""), (0, self.SHA, ""))
        returned = write_and_commit(self.state_file, self.new_state, "update budget", runner)

        with open(self.state_file, "r", encoding="utf-8") as fh:
            text = fh.read()

        self.assertEqual(json.loads(text), self.new_state)
        self.assertTrue(text.endswith("\n"))
        self.assertIn('\n  "', text)  # two-space indentation
        self.assertEqual(returned, self.SHA)

    def test_runs_add_then_commit(self):
        runner = RecordingRunner((0, "", ""), (0, self.SHA, ""))
        write_and_commit(self.state_file, self.new_state, "update budget", runner)

        self.assertEqual(len(runner.calls), 2)
        add_args, add_cwd = runner.calls[0]
        commit_args, commit_cwd = runner.calls[1]

        self.assertEqual(add_args, ["git", "add", "--", "desired.json"])
        self.assertEqual(add_cwd, self.dirname)
        self.assertEqual(
            commit_args,
            ["git", "commit", "--only", "-m", "update budget", "--", "desired.json"],
        )
        self.assertEqual(commit_cwd, self.dirname)

    def test_uses_injected_write(self):
        captured = {}

        def fake_write(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        runner = RecordingRunner((0, "", ""), (0, self.SHA, ""))
        write_and_commit(self.state_file, self.new_state, "msg", runner, write=fake_write)

        args = captured["args"]
        self.assertIn(self.state_file, args)
        texts = [a for a in args if isinstance(a, str) and a.lstrip().startswith("{")]
        self.assertEqual(len(texts), 1, "expected the JSON document to be passed to write()")
        self.assertEqual(json.loads(texts[0]), self.new_state)
        self.assertTrue(texts[0].endswith("\n"))
        # Nothing must have been written to disk behind the injected writer's back.
        self.assertFalse(os.path.exists(self.state_file))

    def test_git_failure_raises_tool_error(self):
        scenarios = {
            "add fails": [(1, "", "add exploded"), (0, self.SHA, "")],
            "commit fails": [(0, "", ""), (1, "", "commit exploded")],
        }
        for name, responses in scenarios.items():
            with self.subTest(scenario=name):
                runner = RecordingRunner(*responses)
                with self.assertRaises(ToolError):
                    write_and_commit(self.state_file, self.new_state, "msg", runner)


if __name__ == "__main__":
    unittest.main()