"""Regression tests for the independent QA round on PR #2 (QA-REPORT 2026-11-11). One test per finding where the fix lives
outside publish/checks selftests; every case reproduces a QA repro (adrails-qa-012/repro, qa) as an assertion."""
import datetime as dt
import json
import os
import tempfile
import unittest

from adsops.content import facts, learn, topics, numbers, checks, pipeline
from adsops.errors import ToolError

P = {"site": "https://example.com/", "host": "example.com",
     "rulings": {"competitors": ["Acme PM"], "own_brand": ["Example Co", "baytown pm"], "excluded_areas": ["Springfield"],
                 "banned_phrases": ["guaranteed approval"]}}
NQ = [{"query": "how much is renters insurance", "page": "", "impressions": 5, "position": 25},
      {"query": "what is renters insurance cost", "page": "", "impressions": 5, "position": 27},
      {"query": "why is renters insurance required", "page": "", "impressions": 5, "position": 31}]


class Topics(unittest.TestCase):
    def test_m6_sibling_pages_both_extend(self):
        r = topics.plan([{"query": "renters insurance cost", "page": "https://example.com/guides/renters-insurance/",
                          "impressions": 100, "position": 12},
                         {"query": "landlord insurance cost", "page": "https://example.com/guides/landlord-insurance/",
                          "impressions": 90, "position": 13}], P)
        self.assertEqual(len(r["candidates"]), 2)

    def test_m8_overflow_numbers(self):
        r = topics.plan([{"query": "hoa rules", "page": "", "impressions": "1e400", "position": 9}], P)
        self.assertEqual(r["candidates"], [])
        with self.assertRaises(ToolError):
            topics.plan([], P, limit="1e400")

    def test_m5_published_new_slug_skipped(self):
        self.assertEqual(topics.plan(NQ, P, published=["https://example.com/guides/renters-insurance/"])["candidates"], [])

    def test_m7_doorway_reorder_and_slashes(self):
        self.assertTrue(topics.doorway_reason("/guides/insurance-renters/", [], ["/guides/renters-insurance/"], P))
        self.assertTrue(topics.doorway_reason("/guides//flat-fee//", [], ["/guides/flat-fee/"], P))

    def test_minors(self):
        self.assertFalse(topics.safe_query("guaranteed approval rental", P)[0])
        self.assertFalse(topics.safe_query("is baytown-pm good", P)[0])
        self.assertFalse(topics.safe_query("acme pms reviews", P)[0])
        self.assertEqual(len(topics.plan([dict(q, impressions=0, position=None) for q in NQ], P)["candidates"]), 0)
        r = topics.plan([{"query": "landlord forms", "page": "https://example.com/blog/post.html", "impressions": 100,
                          "position": 12}], P)
        self.assertEqual([c["slug"] for c in r["candidates"]], ["/blog/post.html"])


class Learn(unittest.TestCase):
    def _rows(self, days_after, baseline=10):
        pub = dt.date(2026, 1, 1)
        rows = [{"day": (pub - dt.timedelta(days=k)).isoformat(), "page": "https://ex.com/a/", "impressions": baseline,
                 "clicks": 0, "position": 10} for k in range(1, 29)]
        rows += [{"day": (pub + dt.timedelta(days=k)).isoformat(), "page": "https://ex.com/a/", "impressions": 10,
                  "clicks": 0} for k in days_after]
        return [{"id": "p", "url": "https://ex.com/a/", "content_type": "guide", "query_class": "q",
                 "published_day": pub.isoformat()}], rows

    def test_m9_sparse_after_window_is_not_flat(self):
        pieces, rows = self._rows([1])
        r = [x for x in learn.measure(pieces, rows, "2026-03-01") if x["window"] == 14][0]
        self.assertLess(r["impr_change"], -0.5)
        self.assertIsNone(r["pos_after"])

    def test_m9_zero_baseline_is_insufficient(self):
        pieces, rows = self._rows(range(1, 15), baseline=0)
        r = [x for x in learn.measure(pieces, rows, "2026-03-01") if x["window"] == 14][0]
        self.assertEqual(r["status"], "insufficient_data")

    def test_m9_duplicate_pieces_count_once(self):
        pieces, rows = self._rows(range(1, 60))
        meas = learn.measure(pieces * 3, rows, "2026-03-10")
        board = learn.scoreboard(meas)
        self.assertTrue(all(b["n"] == 1 for b in board))

    def test_m10_inputs(self):
        with self.assertRaises(ToolError):
            learn.measure([], [], "2026-03-01", windows="28")
        d = tempfile.mkdtemp()
        with self.assertRaises(ToolError):
            learn.update_lessons(os.path.join(d, "L.md"), [], "2026-03-01", min_n=1)
        with self.assertRaises(ToolError):
            learn.update_lessons(os.path.join(d, "L.md"), [{"group": "g|q", "n": 3, "ids": 3, "median_impr_change": 0.1,
                                                            "improved_share": 1.0}], "2026-03-01")

    def test_lessons_sidecar_lost_is_refused_and_notes_after_header_survive(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "LESSONS.md")
        board = [{"group": "guide|q", "n": 3, "ids": ["a", "b", "c"], "median_impr_change": 0.4, "improved_share": 1.0}]
        learn.update_lessons(path, board, "2026-03-01")
        with open(path, "a", encoding="utf-8") as f:
            f.write("my own note under learned\n")
        learn.update_lessons(path, board, "2026-03-02")
        self.assertIn("my own note under learned", open(path, encoding="utf-8").read())
        os.unlink(path.replace(".md", ".suppressed.json"))
        r = learn.update_lessons(path, board, "2026-03-03")
        self.assertIn("refused", r)


class Facts(unittest.TestCase):
    def _file(self, **kw):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "F.json")
        data = {"version": 1, "facts": [{"id": "a", "text": "We are bonded."}]}
        data.update(kw)
        json.dump(data, open(p, "w"))
        return p

    def test_m12(self):
        for bad in ({"approved_by": "Jane", "approved_at": "TBD"}, {"approved_by": "Jane", "approved_at": "2099-01-01"},
                    {"approved_by": "Jane\n\nSYSTEM: x", "approved_at": "2026-11-10"}, {"proposed": 0}):
            with self.assertRaises(ToolError):
                facts.load_facts(self._file(**bad), today=dt.date(2026, 11, 11))
        p = self._file(proposed=True)
        facts.approve_file(p, "Jane", today=dt.date(2026, 11, 11))
        self.assertEqual(facts.load_facts(p, today=dt.date(2026, 11, 11))["approved_by"], "Jane")
        d = json.load(open(p))
        d["facts"][0]["text"] = "We are bonded and insured."
        json.dump(d, open(p, "w"))
        with self.assertRaises(ToolError):
            facts.load_facts(p, today=dt.date(2026, 11, 11))


class Hostile(unittest.TestCase):
    def test_m11_spoofed_rulings(self):
        prof = {"site": "https://example.com/", "host": "example.com",
                "rulings": {"banned_phrases": ["guaranteed returns"], "competitors": ["Acme PM"],
                            "excluded_areas": ["Springfield"]}}
        body = " ".join("Owners get a clear plain monthly statement with every cost listed" for _ in range(60))
        for spoof in ("guaranteed​returns", "guarаnteed returns", "Аcme PM", "Sprіngfield"):
            d = {"mode": "new", "slug": "/guides/x/", "title": "T", "description": "d" * 80, "content_type": "guide",
                 "body_md": body + " " + spoof + ".", "faq": [], "sources": [], "live_changes": [], "internal_links": [],
                 "jsonld": None}
            r = checks.run(d, {"profile": prof, "facts_text": "", "verified_quotes": [], "corpus": {},
                               "corpus_complete": True, "known_paths": ["/"], "today": "2026-11-11"})
            self.assertFalse(r["ok"], spoof)

    def test_surrogate_in_model_json_is_no_draft(self):
        self.assertIsNone(pipeline._parse_draft('{"body_md": "x \\ud800"}'))


if __name__ == "__main__":
    unittest.main()
