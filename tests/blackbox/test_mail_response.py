import datetime
import math
import re
import unittest

from adsops.mail import response


def make_piece(code, mailed_at, batch="A", segment="S1", **kwargs):
    d = {
        "tracking_code": code,
        "mailed_at": mailed_at,
        "batch": batch,
        "segment": segment,
    }
    d.update(kwargs)
    return d


def make_outcome(scanned=False, lead=False, client=False, lead_at=None):
    return {
        "scanned": scanned,
        "lead": lead,
        "client": client,
        "lead_at": lead_at,
    }


class TestWilson(unittest.TestCase):
    def test_wilson_n_zero_or_negative(self):
        self.assertIsNone(response.wilson(0, 0))
        self.assertIsNone(response.wilson(5, 0))
        self.assertIsNone(response.wilson(0, -1))

    def test_wilson_bounds_and_rounding(self):
        lo, hi = response.wilson(0, 10)
        self.assertAlmostEqual(lo, 0.0, places=5)
        self.assertGreaterEqual(hi, 0.0)
        self.assertLessEqual(hi, 1.0)

        lo, hi = response.wilson(10, 10)
        self.assertAlmostEqual(hi, 1.0, places=5)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(lo, 1.0)

        for k, n in [(3, 10), (7, 20), (1, 5)]:
            lo, hi = response.wilson(k, n)
            self.assertEqual(lo, round(lo, 5))
            self.assertEqual(hi, round(hi, 5))

    def test_wilson_known_value(self):
        lo, hi = response.wilson(5, 10)
        # Wilson interval for 5/10 at 95% is roughly [0.2366, 0.7634]
        self.assertAlmostEqual(lo, 0.2366, delta=0.001)
        self.assertAlmostEqual(hi, 0.7634, delta=0.001)

    def test_wilson_custom_z(self):
        lo1, hi1 = response.wilson(5, 10, z=1.96)
        lo2, hi2 = response.wilson(5, 10, z=1.0)
        self.assertGreater(lo2, lo1)
        self.assertLess(hi2, hi1)


class TestRateTable(unittest.TestCase):
    def test_rate_table_empty(self):
        self.assertEqual(response.rate_table([], {}), [])

    def test_rate_table_grouping_and_counts(self):
        today = datetime.date(2024, 1, 10)
        pieces = [
            make_piece("1", "2024-01-01", batch="A"),
            make_piece("2", "2024-01-02", batch="A"),
            make_piece("3", "2024-01-03", batch="B"),
        ]
        outcomes = {
            "1": make_outcome(scanned=True, lead=True, client=False, lead_at="2024-01-05"),
            "2": make_outcome(scanned=False, lead=False, client=True, lead_at=None),
            "3": make_outcome(scanned=True, lead=False, client=False, lead_at=None),
        }
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=0, today=today)
        self.assertEqual(len(rows), 2)
        by_group = {r["group"]: r for r in rows}
        self.assertIn("A", by_group)
        self.assertIn("B", by_group)

        a = by_group["A"]
        self.assertEqual(a["mailed"], 2)
        self.assertEqual(a["scans"], 1)
        self.assertEqual(a["leads"], 1)
        self.assertEqual(a["clients"], 1)
        self.assertAlmostEqual(a["scan_rate"], 0.5, places=5)
        self.assertAlmostEqual(a["lead_rate"], 0.5, places=5)
        self.assertAlmostEqual(a["client_rate"], 0.5, places=5)

        b = by_group["B"]
        self.assertEqual(b["mailed"], 1)
        self.assertEqual(b["scans"], 1)
        self.assertEqual(b["leads"], 0)
        self.assertEqual(b["clients"], 0)
        self.assertAlmostEqual(b["scan_rate"], 1.0, places=5)
        self.assertAlmostEqual(b["lead_rate"], 0.0, places=5)
        self.assertAlmostEqual(b["client_rate"], 0.0, places=5)

    def test_rate_table_missing_by_key(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece("1", "2024-01-01")]
        pieces[0].pop("segment", None)
        outcomes = {"1": make_outcome(scanned=True)}
        rows = response.rate_table(pieces, outcomes, by="segment", min_days=0, today=today)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["group"], "(none)")

    def test_rate_table_days_since_mail_min(self):
        today = datetime.date(2024, 1, 10)
        pieces = [
            make_piece("1", "2024-01-01", batch="A"),
            make_piece("2", "2024-01-05", batch="A"),
        ]
        outcomes = {"1": make_outcome(), "2": make_outcome()}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=0, today=today)
        self.assertEqual(len(rows), 1)
        # Jan 1 -> 9 days, Jan 5 -> 5 days, min is 5
        self.assertEqual(rows[0]["days_since_mail"], 5)

    def test_rate_table_verdict_too_early(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece("1", "2024-01-01", batch="A")]
        outcomes = {"1": make_outcome(lead=True, lead_at="2024-01-05")}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=42, today=today)
        verdict = rows[0]["verdict"]
        self.assertIn("too early", verdict.lower())

    def test_rate_table_verdict_no_signal(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece("1", "2023-01-01", batch="A")]  # >42 days
        outcomes = {"1": make_outcome(lead=False)}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=42, today=today)
        verdict = rows[0]["verdict"]
        self.assertIn("no signal yet", verdict.lower())
        self.assertIn("%", verdict)
        upper = rows[0]["lead_ci"][1] * 100
        m = re.search(r"([\d.]+)\s*%", verdict)
        self.assertIsNotNone(m, msg=f"could not find percentage in {verdict!r}")
        x = float(m.group(1))
        self.assertAlmostEqual(x, upper, delta=0.2)

    def test_rate_table_verdict_n_too_small(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece(str(i), "2023-01-01", batch="A") for i in range(50)]
        outcomes = {str(i): make_outcome(lead=(i == 0)) for i in range(50)}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=42, today=today)
        self.assertEqual(rows[0]["verdict"], "n too small")

    def test_rate_table_verdict_reading(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece(str(i), "2023-01-01", batch="A") for i in range(100)]
        outcomes = {str(i): make_outcome(lead=(i == 0)) for i in range(100)}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=42, today=today)
        self.assertEqual(rows[0]["verdict"], "reading")

    def test_rate_table_verdict_precedence_too_early(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece(str(i), "2024-01-01", batch="A") for i in range(10)]
        # leads=0, mailed<100, days<min_days -> too early
        outcomes = {str(i): make_outcome(lead=False) for i in range(10)}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=42, today=today)
        self.assertIn("too early", rows[0]["verdict"].lower())
        # leads>0, mailed<100, days<min_days -> too early
        outcomes = {str(i): make_outcome(lead=(i == 0)) for i in range(10)}
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=42, today=today)
        self.assertIn("too early", rows[0]["verdict"].lower())

    def test_rate_table_rates(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece(str(i), "2023-01-01", batch="A") for i in range(4)]
        outcomes = {
            "0": make_outcome(scanned=True, lead=True, client=True),
            "1": make_outcome(scanned=True, lead=False, client=False),
            "2": make_outcome(scanned=False, lead=False, client=False),
            "3": make_outcome(scanned=False, lead=False, client=False),
        }
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=0, today=today)
        r = rows[0]
        self.assertAlmostEqual(r["scan_rate"], 2 / 4, places=5)
        self.assertAlmostEqual(r["lead_rate"], 1 / 4, places=5)
        self.assertAlmostEqual(r["client_rate"], 1 / 4, places=5)

    def test_rate_table_lead_ci_matches_wilson(self):
        today = datetime.date(2024, 1, 10)
        pieces = [make_piece(str(i), "2023-01-01", batch="A") for i in range(10)]
        outcomes = {str(i): make_outcome(lead=(i < 3)) for i in range(10)}  # 3 leads
        rows = response.rate_table(pieces, outcomes, by="batch", min_days=0, today=today)
        expected = response.wilson(3, 10)
        self.assertAlmostEqual(rows[0]["lead_ci"][0], expected[0], places=5)
        self.assertAlmostEqual(rows[0]["lead_ci"][1], expected[1], places=5)


class TestCompare(unittest.TestCase):
    def test_compare_guard_n_zero(self):
        with self.assertRaises(response.ToolError):
            response.compare(0, 0, 0, 0)

    def test_compare_diff_and_ci(self):
        res = response.compare(10, 100, 20, 100)
        self.assertAlmostEqual(res["diff"], -0.1, places=5)
        lo, hi = res["diff_ci"]
        self.assertLessEqual(lo, res["diff"])
        self.assertGreaterEqual(hi, res["diff"])
        self.assertLessEqual(lo, hi)

    def test_compare_z_and_p(self):
        res = response.compare(10, 100, 20, 100)
        p1 = 10 / 100
        p2 = 20 / 100
        p_pool = (10 + 20) / 200
        se = math.sqrt(p_pool * (1 - p_pool) * (1 / 100 + 1 / 100))
        expected_z = (p1 - p2) / se
        expected_p = 2 * (1 - 0.5 * (1 + math.erf(abs(expected_z) / math.sqrt(2))))
        self.assertAlmostEqual(res["z"], expected_z, places=3)
        self.assertAlmostEqual(res["p_value"], expected_p, places=3)
        self.assertIs(res["significant"], res["p_value"] < 0.05)

    def test_compare_identical(self):
        res = response.compare(50, 100, 50, 100)
        self.assertAlmostEqual(res["diff"], 0.0, places=5)
        self.assertAlmostEqual(res["z"], 0.0, places=5)
        self.assertAlmostEqual(res["p_value"], 1.0, places=5)
        self.assertFalse(res["significant"])

    def test_compare_not_significant(self):
        res = response.compare(48, 100, 52, 100)
        self.assertFalse(res["significant"])
        self.assertGreater(res["p_value"], 0.05)


class TestHoldoutSize(unittest.TestCase):
    def test_holdout_size_validation(self):
        with self.assertRaises(response.ToolError):
            response.holdout_size(0, 0.2)
        with self.assertRaises(response.ToolError):
            response.holdout_size(1, 0.2)
        with self.assertRaises(response.ToolError):
            response.holdout_size(-0.1, 0.2)
        with self.assertRaises(response.ToolError):
            response.holdout_size(0.1, 0)
        with self.assertRaises(response.ToolError):
            response.holdout_size(0.1, -0.1)

    def test_holdout_size_positive(self):
        res = response.holdout_size(0.05, 0.2, ratio=1.0)
        self.assertIn("treatment", res)
        self.assertIn("holdout", res)
        self.assertIn("note", res)
        self.assertGreater(res["treatment"], 0)
        self.assertGreater(res["holdout"], 0)
        self.assertIsInstance(res["note"], str)
        self.assertAlmostEqual(res["treatment"] / res["holdout"], 1.0, delta=0.1)

    def test_holdout_size_ratio(self):
        res = response.holdout_size(0.05, 0.2, ratio=2.0)
        self.assertAlmostEqual(res["treatment"] / res["holdout"], 2.0, delta=0.2)

    def test_holdout_size_monotonic(self):
        res1 = response.holdout_size(0.1, 0.1)
        res2 = response.holdout_size(0.1, 0.2)
        self.assertGreater(res1["treatment"], res2["treatment"])


class TestIncremental(unittest.TestCase):
    def test_incremental(self):
        res = response.incremental(20, 1000, 10, 1000)
        self.assertAlmostEqual(res["lift_abs"], 0.01, places=5)
        self.assertAlmostEqual(res["lift_rel"], 1.0, places=5)
        self.assertAlmostEqual(res["incremental_leads_per_1000"], 10.0, places=5)
        self.assertEqual(len(res["ci"]), 2)
        self.assertLessEqual(res["ci"][0], res["ci"][1])


class TestDecay(unittest.TestCase):
    def test_decay_empty(self):
        self.assertEqual(response.decay({}, []), [])

    def test_decay_buckets(self):
        pieces = [
            make_piece("1", "2024-01-01"),
            make_piece("2", "2024-01-01"),
            make_piece("3", "2024-01-01"),
            make_piece("4", "2024-01-01"),
        ]
        outcomes = {
            "1": make_outcome(lead=True, lead_at="2024-01-03"),  # 2 days
            "2": make_outcome(lead=True, lead_at="2024-01-06"),  # 5 days
            "3": make_outcome(lead=True, lead_at="2024-01-09"),  # 8 days
            "4": make_outcome(lead=True, lead_at="2024-01-11"),  # 10 days
        }
        rows = response.decay(outcomes, pieces, bucket_days=7)
        self.assertEqual(len(rows), 2)
        r0, r1 = rows
        self.assertEqual(r0["days_from"], 0)
        self.assertEqual(r0["days_to"], 6)
        self.assertEqual(r0["leads"], 2)
        self.assertAlmostEqual(r0["cum_share"], 0.5, places=5)
        self.assertEqual(r1["days_from"], 7)
        self.assertEqual(r1["days_to"], 13)
        self.assertEqual(r1["leads"], 2)
        self.assertAlmostEqual(r1["cum_share"], 1.0, places=5)

    def test_decay_ignores_non_leads(self):
        pieces = [
            make_piece("1", "2024-01-01"),
            make_piece("2", "2024-01-01"),
            make_piece("3", "2024-01-01"),
        ]
        outcomes = {
            "1": make_outcome(lead=True, lead_at="2024-01-03"),  # 2 days
            "2": make_outcome(lead=False, lead_at="2024-01-04"),  # not a lead
            "3": make_outcome(lead=True, lead_at=None),  # lead but no date
        }
        rows = response.decay(outcomes, pieces, bucket_days=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["leads"], 1)

    def test_decay_custom_bucket(self):
        pieces = [
            make_piece("1", "2024-01-01"),
            make_piece("2", "2024-01-01"),
        ]
        outcomes = {
            "1": make_outcome(lead=True, lead_at="2024-01-06"),  # 5 days
            "2": make_outcome(lead=True, lead_at="2024-02-10"),  # 40 days
        }
        rows = response.decay(outcomes, pieces, bucket_days=30)
        self.assertEqual(len(rows), 2)
        r0, r1 = rows
        self.assertEqual(r0["days_from"], 0)
        self.assertEqual(r0["days_to"], 29)
        self.assertEqual(r0["leads"], 1)
        self.assertAlmostEqual(r0["cum_share"], 0.5, places=5)
        self.assertEqual(r1["days_from"], 30)
        self.assertEqual(r1["days_to"], 59)
        self.assertEqual(r1["leads"], 1)
        self.assertAlmostEqual(r1["cum_share"], 1.0, places=5)


if __name__ == "__main__":
    unittest.main()