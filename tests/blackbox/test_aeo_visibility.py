"""Unit tests for adsops.aeo.visibility (AI share-of-answer tracker).

Only documented public API is exercised: run_panel, score, diff, store,
load_history. Every model call is mocked with in-memory fakes; file I/O uses
tempfile. Assertions avoid exact message wording unless the spec quotes it.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from adsops.aeo import visibility


BRAND = {"name": "AdRails", "aliases": ["adrails.dev"], "domains": ["adrails.dev"]}
COMPETITORS = {"Adspirer": ["adspirer.com"], "Ahrefs": ["ahrefs.com"]}


def make_result(engine="chatgpt", prompt="p", run=0, mentioned=False, position=None,
                cited=False, competitors=(), sentiment="neutral", snippet=""):
    """Build a result dict shaped exactly as documented for run_panel."""
    return {
        "engine": engine,
        "prompt": prompt,
        "run": run,
        "mentioned": mentioned,
        "position": position,
        "cited": cited,
        "competitors_mentioned": list(competitors),
        "sentiment": sentiment,
        "snippet": snippet,
    }


def fake_ask(mapping, calls=None):
    """ask(engine, prompt) driven by a prompt -> response map.

    A value that is an Exception instance is raised; a callable is invoked.
    """
    def ask(engine, prompt):
        if calls is not None:
            calls.append((engine, prompt))
        value = mapping.get(prompt)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return {"text": "", "citations": []}
        if callable(value):
            return value()
        return value
    return ask


class RunPanelTests(unittest.TestCase):
    def test_result_shape_and_counts(self):
        ask = fake_ask({"q1": {"text": "AdRails is great", "citations": []}})
        out = visibility.run_panel(["q1"], ["chatgpt"], ask, BRAND, {})
        self.assertIsInstance(out, dict)
        self.assertIn("results", out)
        self.assertIn("errors", out)
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(out["results"]), 1)

        r = out["results"][0]
        for key in ("engine", "prompt", "run", "mentioned", "position", "cited",
                    "competitors_mentioned", "sentiment", "snippet"):
            self.assertIn(key, r)
        self.assertEqual(r["engine"], "chatgpt")
        self.assertEqual(r["prompt"], "q1")
        self.assertIsInstance(r["run"], int)
        self.assertTrue(r["mentioned"])
        self.assertIsInstance(r["snippet"], str)

    def test_ask_receives_engine_and_prompt(self):
        calls = []
        ask = fake_ask({"q1": {"text": "nothing", "citations": []}}, calls)
        visibility.run_panel(["q1"], ["claude"], ask, BRAND, {})
        self.assertEqual(calls, [("claude", "q1")])

    def test_counts_cover_prompts_engines_and_repeats(self):
        ask = fake_ask({}, None)  # every prompt returns empty text
        out = visibility.run_panel(["a", "b"], ["chatgpt", "claude"], ask, BRAND, {},
                                   repeats=2)
        self.assertEqual(len(out["results"]), 2 * 2 * 2)
        engines = {r["engine"] for r in out["results"]}
        prompts = {r["prompt"] for r in out["results"]}
        self.assertEqual(engines, {"chatgpt", "claude"})
        self.assertEqual(prompts, {"a", "b"})

    def test_mention_detection_is_case_insensitive(self):
        ask = fake_ask({"q": {"text": "you should try ADRAILS today", "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        self.assertTrue(out["results"][0]["mentioned"])

    def test_mention_detection_is_whole_word(self):
        ask = fake_ask({"q": {"text": "adrailscorp and xadrails are unrelated firms",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        self.assertFalse(out["results"][0]["mentioned"])
        self.assertIsNone(out["results"][0]["position"])

    def test_brand_domain_in_text_counts_as_mention(self):
        brand = {"name": "Zeta Widget", "aliases": ["zeta-widget"],
                 "domains": ["zetawidget.com"]}
        ask = fake_ask({"q": {"text": "More details at zetawidget.com today.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, brand, {})
        self.assertTrue(out["results"][0]["mentioned"])

    def test_cited_when_citation_host_ends_with_brand_domain(self):
        ask = fake_ask({"q": {"text": "no brand words here",
                              "citations": ["https://www.adrails.dev/blog/post"]}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        r = out["results"][0]
        self.assertTrue(r["cited"])
        self.assertFalse(r["mentioned"])

    def test_not_cited_for_third_party_citations(self):
        ask = fake_ask({"q": {"text": "AdRails appears",
                              "citations": ["https://example.com/a",
                                            "https://adspirer.com/b"]}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, COMPETITORS)
        self.assertFalse(out["results"][0]["cited"])

    def test_position_counts_competitor_mentions_before_brand(self):
        ask = fake_ask({"q": {"text": "Adspirer is the leader. AdRails is a runner-up.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, COMPETITORS)
        r = out["results"][0]
        self.assertTrue(r["mentioned"])
        self.assertEqual(r["position"], 2)
        self.assertIn("Adspirer", r["competitors_mentioned"])

    def test_position_is_one_when_brand_comes_first(self):
        ask = fake_ask({"q": {"text": "AdRails then Adspirer.", "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, COMPETITORS)
        self.assertEqual(out["results"][0]["position"], 1)

    def test_competitors_mentioned_lists_all_rivals(self):
        ask = fake_ask({"q": {"text": "Adspirer and Ahrefs both do this, AdRails too.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, COMPETITORS)
        r = out["results"][0]
        self.assertEqual(set(r["competitors_mentioned"]), {"Adspirer", "Ahrefs"})
        self.assertEqual(r["position"], 3)

    def test_competitors_mentioned_without_brand(self):
        ask = fake_ask({"q": {"text": "You want Adspirer for this job.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, COMPETITORS)
        r = out["results"][0]
        self.assertFalse(r["mentioned"])
        self.assertIsNone(r["position"])
        self.assertEqual(r["competitors_mentioned"], ["Adspirer"])

    def test_sentiment_positive(self):
        ask = fake_ask({"q": {"text": "I would recommend AdRails; it is the best tool.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        self.assertEqual(out["results"][0]["sentiment"], "positive")

    def test_sentiment_negative(self):
        ask = fake_ask({"q": {"text": "AdRails is expensive and most people avoid it.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        self.assertEqual(out["results"][0]["sentiment"], "negative")

    def test_sentiment_neutral(self):
        ask = fake_ask({"q": {"text": "AdRails is one of several tools in the list.",
                              "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        self.assertEqual(out["results"][0]["sentiment"], "neutral")

    def test_snippet_is_at_most_200_chars_around_mention(self):
        text = ("x" * 500) + " AdRails " + ("y" * 500)
        ask = fake_ask({"q": {"text": text, "citations": []}})
        out = visibility.run_panel(["q"], ["chatgpt"], ask, BRAND, {})
        snippet = out["results"][0]["snippet"]
        self.assertLessEqual(len(snippet), 200)
        self.assertIn("adrails", snippet.lower())

    def test_ask_exception_is_recorded_and_panel_continues(self):
        calls = []
        ask = fake_ask({
            "bad": RuntimeError("upstream exploded"),
            "good": {"text": "AdRails wins", "citations": []},
        }, calls)
        out = visibility.run_panel(["bad", "good"], ["chatgpt"], ask, BRAND, {})
        self.assertTrue(out["errors"])
        for err in out["errors"]:
            # SPEC only requires the scrubbed error to be recorded; it does not
            # mandate whether the entry is a string or a structured record.
            if isinstance(err, dict):
                self.assertIsInstance(err.get("error"), str)
            else:
                self.assertIsInstance(err, str)
        # both prompts were attempted
        self.assertEqual([c[1] for c in calls], ["bad", "good"])
        good = [r for r in out["results"] if r["prompt"] == "good"]
        self.assertEqual(len(good), 1)
        self.assertTrue(good[0]["mentioned"])

    def test_sleep_is_invoked_between_runs(self):
        sleep = mock.Mock()
        ask = fake_ask({}, None)
        out = visibility.run_panel(["a", "b"], ["chatgpt"], ask, BRAND, {},
                                   repeats=2, sleep=sleep)
        self.assertEqual(len(out["results"]), 4)
        self.assertGreaterEqual(sleep.call_count, 1)


class ScoreTests(unittest.TestCase):
    def test_share_of_answer(self):
        results = [
            make_result(mentioned=True, position=1),
            make_result(mentioned=False),
            make_result(mentioned=False),
        ]
        s = visibility.score(results)
        self.assertAlmostEqual(s["share_of_answer"], 1.0 / 3.0, places=6)

    def test_citation_rate(self):
        results = [
            make_result(cited=True),
            make_result(cited=True),
            make_result(cited=False),
            make_result(cited=False),
        ]
        s = visibility.score(results)
        self.assertAlmostEqual(s["citation_rate"], 0.5, places=6)

    def test_avg_position(self):
        results = [
            make_result(mentioned=True, position=1),
            make_result(mentioned=True, position=3),
            make_result(mentioned=False),
        ]
        s = visibility.score(results)
        self.assertAlmostEqual(s["avg_position"], 2.0, places=6)

    def test_by_engine_and_by_prompt_grouping(self):
        results = [
            make_result(engine="chatgpt", prompt="p1", mentioned=True, position=1),
            make_result(engine="chatgpt", prompt="p2", mentioned=False),
            make_result(engine="claude", prompt="p1", mentioned=False),
            make_result(engine="claude", prompt="p2", mentioned=True, position=2),
        ]
        s = visibility.score(results)
        self.assertEqual(set(s["by_engine"].keys()), {"chatgpt", "claude"})
        self.assertEqual(set(s["by_prompt"].keys()), {"p1", "p2"})
        self.assertIsInstance(s["by_engine"]["chatgpt"], dict)
        self.assertIsInstance(s["by_prompt"]["p1"], dict)

    def test_competitor_share(self):
        results = [
            make_result(competitors=["Adspirer"]),
            make_result(competitors=["Adspirer"]),
            make_result(competitors=[]),
            make_result(competitors=[]),
        ]
        s = visibility.score(results)
        self.assertAlmostEqual(s["competitor_share"]["Adspirer"], 0.5, places=6)
        self.assertAlmostEqual(s["competitor_share"].get("Ahrefs", 0.0), 0.0, places=6)

    def test_top_gap_prompts_sorted_by_number_of_competitors(self):
        results = [
            make_result(prompt="gap_one", mentioned=False, competitors=["Adspirer"]),
            make_result(prompt="gap_two", mentioned=False,
                        competitors=["Adspirer", "Ahrefs"]),
            make_result(prompt="we_win", mentioned=True, position=1,
                        competitors=["Adspirer"]),
        ]
        s = visibility.score(results)
        self.assertEqual(s["top_gap_prompts"], ["gap_two", "gap_one"])


class DiffTests(unittest.TestCase):
    def _scores(self):
        prev_results = [
            make_result(prompt="p1", mentioned=True, position=1),
            make_result(prompt="p2", mentioned=False),
        ]
        cur_results = [
            make_result(prompt="p1", mentioned=True, position=1),
            make_result(prompt="p2", mentioned=True, position=2),
        ]
        return visibility.score(prev_results), visibility.score(cur_results)

    def test_diff_reports_share_increase_in_points(self):
        prev, cur = self._scores()
        self.assertAlmostEqual(cur["share_of_answer"] - prev["share_of_answer"],
                               0.5, places=6)
        d = visibility.diff(prev, cur)
        self.assertIsInstance(d, list)
        self.assertTrue(d)
        self.assertTrue(all(isinstance(s, str) for s in d))
        self.assertIn("50", " ".join(d))

    def test_diff_of_identical_scores_is_a_list_of_strings(self):
        prev, _ = self._scores()
        d = visibility.diff(prev, prev)
        self.assertIsInstance(d, list)
        for s in d:
            self.assertIsInstance(s, str)


class StorageTests(unittest.TestCase):
    def test_store_appends_json_lines_and_load_history_roundtrip(self):
        results = [
            make_result(prompt="p1", mentioned=True, position=1),
            make_result(prompt="p2", mentioned=False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.jsonl")
            visibility.store(path, "run-1", results, "2024-01-01T00:00:00Z")
            visibility.store(path, "run-2", results, "2024-01-02T00:00:00Z")

            self.assertTrue(os.path.exists(path))
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            for line in lines:
                json.loads(line)  # each line is valid JSON
            self.assertIn("run-1", raw)
            self.assertIn("run-2", raw)

            hist = visibility.load_history(path)
            self.assertEqual(len(hist), 2)


if __name__ == '__main__':
    unittest.main()