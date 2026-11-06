import unittest
from unittest.mock import patch, Mock
import contextlib

import adsops.oai_experiments as oai
import adsops.mail.response as response


def patch_stats(wilson=None, compare=None, holdout=None):
    """Patch stats functions in both the response module and the experiments module."""
    patchers = []
    if wilson is not None:
        patchers.append(patch.object(response, 'wilson', wilson))
        patchers.append(patch.object(oai, 'wilson', wilson, create=True))
    if compare is not None:
        patchers.append(patch.object(response, 'compare', compare))
        patchers.append(patch.object(oai, 'compare', compare, create=True))
    if holdout is not None:
        patchers.append(patch.object(response, 'holdout_size', holdout))
        patchers.append(patch.object(oai, 'holdout_size', holdout, create=True))
    return patchers


class TestOAIExperiments(unittest.TestCase):
    def test_ab_report_returns_expected_keys(self):
        ads = [{"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
                "conversions": 10, "spend": 50.0}]
        m_wilson = Mock(return_value=(0.1, 0.2))
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0.05, 0.15),
                                       "z": 2.0, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=m_wilson, compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertSetEqual(set(rep.keys()),
                            {"metric", "rows", "leader", "comparisons",
                             "verdict", "recommend_pause"})

    def test_rows_contain_all_ads_and_metrics_computed(self):
        ads = [
            {"id": "A", "name": "Ad A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50.0},
            {"id": "B", "name": "Ad B", "impressions": 2000, "clicks": 200,
             "conversions": 20, "spend": 100.0},
        ]
        m_wilson = Mock(return_value=(0.1, 0.2))
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0.05, 0.15),
                                       "z": 2.0, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=m_wilson, compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertEqual(len(rep["rows"]), 2)
        row_a = next(r for r in rep["rows"] if r["id"] == "A")
        self.assertEqual(row_a["impressions"], 1000)
        self.assertEqual(row_a["clicks"], 100)
        self.assertEqual(row_a["conversions"], 10)
        self.assertEqual(row_a["spend"], 50.0)
        self.assertAlmostEqual(row_a["ctr"], 100 / 1000)
        self.assertAlmostEqual(row_a["cvr"], 10 / 100)
        self.assertAlmostEqual(row_a["cpc"], 50.0 / 100)
        self.assertAlmostEqual(row_a["cpa"], 50.0 / 10)
        self.assertEqual(row_a["ctr_ci"], [0.1, 0.2])
        self.assertEqual(row_a["cvr_ci"], [0.1, 0.2])

    def test_division_by_zero_returns_none(self):
        ads = [
            {"id": "A", "name": "Zero Imp", "impressions": 0, "clicks": 0,
             "conversions": 0, "spend": 0.0},
            {"id": "B", "name": "Zero Clicks", "impressions": 1000, "clicks": 0,
             "conversions": 0, "spend": 10.0},
            {"id": "C", "name": "Zero Conversions", "impressions": 1000, "clicks": 100,
             "conversions": 0, "spend": 50.0},
        ]
        m_wilson = Mock(return_value=(0.0, 0.0))
        m_compare = Mock(return_value={"diff": 0.0, "diff_ci": (0, 0),
                                       "z": 0, "p_value": 1.0, "significant": False})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=m_wilson, compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, min_impressions=0, min_clicks=0)
        rows = {r["id"]: r for r in rep["rows"]}
        # A: impressions=0 -> all ratios None
        self.assertIsNone(rows["A"]["ctr"])
        self.assertIsNone(rows["A"]["cvr"])
        self.assertIsNone(rows["A"]["cpc"])
        self.assertIsNone(rows["A"]["cpa"])
        # B: clicks=0 -> cvr/cpc/cpa None, ctr=0
        self.assertEqual(rows["B"]["ctr"], 0.0)
        self.assertIsNone(rows["B"]["cvr"])
        self.assertIsNone(rows["B"]["cpc"])
        self.assertIsNone(rows["B"]["cpa"])
        # C: conversions=0 -> cpa None, others computed
        self.assertAlmostEqual(rows["C"]["ctr"], 0.1)
        self.assertEqual(rows["C"]["cvr"], 0.0)
        self.assertAlmostEqual(rows["C"]["cpc"], 0.5)
        self.assertIsNone(rows["C"]["cpa"])

    def test_leader_is_highest_ctr_among_qualified(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
            {"id": "C", "name": "C", "impressions": 500, "clicks": 100,
             "conversions": 10, "spend": 50},  # unqualified
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertEqual(rep["leader"], "B")

    def test_leader_none_when_no_qualified(self):
        ads = [{"id": "A", "name": "A", "impressions": 100, "clicks": 10,
                "conversions": 1, "spend": 10}]
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(), compare=Mock()):
                stack.enter_context(p)
            rep = oai.ab_report(ads, min_impressions=1000, min_clicks=30)
        self.assertIsNone(rep["leader"])
        self.assertEqual(rep["comparisons"], [])
        self.assertEqual(rep["recommend_pause"], [])
        self.assertIn("too early", rep["verdict"].lower())

    def test_comparisons_compare_every_other_qualified_to_leader(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
            {"id": "C", "name": "C", "impressions": 1000, "clicks": 50,
             "conversions": 5, "spend": 50},
            {"id": "D", "name": "D", "impressions": 500, "clicks": 100,
             "conversions": 10, "spend": 50},  # unqualified
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertEqual(rep["leader"], "B")
        comp_ids = {c["id"] for c in rep["comparisons"]}
        self.assertEqual(comp_ids, {"A", "C"})
        for c in rep["comparisons"]:
            self.assertEqual(c["vs"], "B")

    def test_comparisons_include_diff_and_p_value_from_compare(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0.05, 0.15),
                                       "z": 2.0, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertEqual(len(rep["comparisons"]), 1)
        comp = rep["comparisons"][0]
        self.assertEqual(comp["diff"], 0.1)
        self.assertEqual(comp["diff_ci"], [0.05, 0.15])
        self.assertEqual(comp["p_value"], 0.01)
        self.assertTrue(comp["significant"])

    def test_bonferroni_correction_applied_to_significance(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},  # leader ctr 0.2
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},  # ctr 0.1
            {"id": "C", "name": "C", "impressions": 1000, "clicks": 50,
             "conversions": 5, "spend": 50},   # ctr 0.05
        ]
        def compare_side_effect(*args, **kwargs):
            # Identify B by its clicks=100
            if 100 in args:
                return {"diff": 0.1, "diff_ci": (0, 0), "z": 2.0,
                        "p_value": 0.01, "significant": True}
            else:
                return {"diff": 0.15, "diff_ci": (0, 0), "z": 3.0,
                        "p_value": 0.04, "significant": True}
        m_compare = Mock(side_effect=compare_side_effect)
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, alpha=0.05)
        comps = {c["id"]: c for c in rep["comparisons"]}
        # Bonferroni: 0.05 / 2 = 0.025
        self.assertTrue(comps["B"]["significant"])   # 0.01 < 0.025
        self.assertFalse(comps["C"]["significant"])  # 0.04 > 0.025

    def test_recommend_pause_includes_significantly_worse_ads(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},  # leader ctr 0.2
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},  # ctr 0.1
            {"id": "C", "name": "C", "impressions": 1000, "clicks": 150,
             "conversions": 15, "spend": 50},  # ctr 0.15
        ]
        def compare_side_effect(*args):
            if 100 in args:  # B
                return {"diff": 0.1, "diff_ci": (0.05, 0.15), "z": 2.0,
                        "p_value": 0.01, "significant": True}
            else:  # C
                return {"diff": 0.05, "diff_ci": (0, 0.1), "z": 1.0,
                        "p_value": 0.2, "significant": False}
        m_compare = Mock(side_effect=compare_side_effect)
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, alpha=0.05)
        self.assertEqual(set(rep["recommend_pause"]), {"B"})
        self.assertNotIn("A", rep["recommend_pause"])

    def test_recommend_pause_excludes_not_significantly_worse(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 1.0, "p_value": 0.2, "significant": False})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, alpha=0.05)
        self.assertEqual(rep["recommend_pause"], [])

    def test_verdict_too_early_when_fewer_than_two_qualified(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},  # qualified
            {"id": "B", "name": "B", "impressions": 100, "clicks": 10,
             "conversions": 1, "spend": 10},   # unqualified
        ]
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(), compare=Mock()):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertIn("too early", rep["verdict"].lower())
        self.assertEqual(rep["leader"], "A")
        self.assertEqual(rep["comparisons"], [])

    def test_verdict_not_too_early_when_at_least_two_qualified(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertNotIn("too early", rep["verdict"].lower())

    def test_unqualified_ads_not_in_comparisons(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},  # qualified
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},  # qualified leader
            {"id": "C", "name": "C", "impressions": 1000, "clicks": 10,
             "conversions": 1, "spend": 10},   # unqualified (clicks < 30)
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, min_clicks=30)
        comp_ids = {c["id"] for c in rep["comparisons"]}
        self.assertEqual(comp_ids, {"A"})
        self.assertEqual(rep["leader"], "B")

    def test_metric_cvr_leader_uses_cvr(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},  # cvr 0.1
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 100,
             "conversions": 20, "spend": 50},  # cvr 0.2
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, metric="cvr")
        self.assertEqual(rep["leader"], "B")
        self.assertEqual(rep["metric"], "cvr")

    def test_metric_cvr_comparisons_use_conversions(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 100,
             "conversions": 20, "spend": 50},
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            oai.ab_report(ads, metric="cvr")
        args, _ = m_compare.call_args
        # conversions 20 (leader) and 10 (other) must be part of the call
        self.assertIn(20, args)
        self.assertIn(10, args)

    def test_ctr_ci_and_cvr_ci_use_wilson(self):
        ads = [{"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
                "conversions": 10, "spend": 50}]
        m_wilson = Mock(return_value=(0.1, 0.2))
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=m_wilson, compare=Mock()):
                stack.enter_context(p)
            oai.ab_report(ads)
        m_wilson.assert_any_call(100, 1000)  # ctr: clicks, impressions
        m_wilson.assert_any_call(10, 100)    # cvr: conversions, clicks

    def test_sample_needed_calls_holdout_size(self):
        m_holdout = Mock(return_value=1234)
        with contextlib.ExitStack() as stack:
            for p in patch_stats(holdout=m_holdout):
                stack.enter_context(p)
            result = oai.sample_needed(0.1, 0.05, alpha=0.02, power=0.9)
        self.assertEqual(result, 1234)
        m_holdout.assert_called_once_with(0.1, 0.05, 0.02, 0.9)

    def test_sample_needed_default_args(self):
        m_holdout = Mock(return_value=1000)
        with contextlib.ExitStack() as stack:
            for p in patch_stats(holdout=m_holdout):
                stack.enter_context(p)
            oai.sample_needed(0.2, 0.1)
        m_holdout.assert_called_once_with(0.2, 0.1, 0.05, 0.8)

    def test_sample_needed_positional_args(self):
        m_holdout = Mock(return_value=42)
        with contextlib.ExitStack() as stack:
            for p in patch_stats(holdout=m_holdout):
                stack.enter_context(p)
            result = oai.sample_needed(0.1, 0.05, 0.01, 0.9)
        self.assertEqual(result, 42)
        m_holdout.assert_called_once_with(0.1, 0.05, 0.01, 0.9)

    def test_ab_report_with_empty_ads(self):
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(), compare=Mock()):
                stack.enter_context(p)
            rep = oai.ab_report([])
        self.assertEqual(rep["rows"], [])
        self.assertIsNone(rep["leader"])
        self.assertEqual(rep["comparisons"], [])
        self.assertEqual(rep["recommend_pause"], [])
        self.assertIn("too early", rep["verdict"].lower())

    def test_ab_report_with_one_ad(self):
        ads = [{"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
                "conversions": 10, "spend": 50}]
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=Mock()):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertEqual(rep["leader"], "A")
        self.assertEqual(rep["comparisons"], [])
        self.assertIn("too early", rep["verdict"].lower())

    def test_min_impressions_and_min_clicks_filtering(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 30,
             "conversions": 3, "spend": 10},  # meets both exactly
            {"id": "B", "name": "B", "impressions": 999, "clicks": 100,
             "conversions": 10, "spend": 10},  # impressions below
            {"id": "C", "name": "C", "impressions": 1000, "clicks": 29,
             "conversions": 3, "spend": 10},   # clicks below
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads, min_impressions=1000, min_clicks=30)
        self.assertEqual(rep["leader"], "A")
        self.assertEqual(rep["comparisons"], [])
        self.assertIn("too early", rep["verdict"].lower())

    def test_ab_report_metric_default_is_ctr(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2, "p_value": 0.01, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep = oai.ab_report(ads)
        self.assertEqual(rep["metric"], "ctr")

    def test_alpha_affects_significance(self):
        ads = [
            {"id": "A", "name": "A", "impressions": 1000, "clicks": 200,
             "conversions": 20, "spend": 50},
            {"id": "B", "name": "B", "impressions": 1000, "clicks": 100,
             "conversions": 10, "spend": 50},
        ]
        m_compare = Mock(return_value={"diff": 0.1, "diff_ci": (0, 0),
                                       "z": 2.0, "p_value": 0.03, "significant": True})
        with contextlib.ExitStack() as stack:
            for p in patch_stats(wilson=Mock(return_value=(0, 0)),
                                 compare=m_compare):
                stack.enter_context(p)
            rep1 = oai.ab_report(ads, alpha=0.05)
            rep2 = oai.ab_report(ads, alpha=0.02)
        self.assertTrue(rep1["comparisons"][0]["significant"])
        self.assertFalse(rep2["comparisons"][0]["significant"])


if __name__ == '__main__':
    unittest.main()