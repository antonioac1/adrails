import unittest

from adsops import diagnostics

MICROS = 1_000_000


def metrics(cost_dollars=0.0, clicks=0, impressions=0, conversions=0.0):
    """Build a raw REST-like metrics object."""
    return {
        "costMicros": str(int(round(cost_dollars * MICROS))),
        "clicks": str(clicks),
        "impressions": str(impressions),
        "conversions": conversions,
    }


def search_term_row(term, cost_dollars=0.0, clicks=0, impressions=0, conversions=0.0):
    return {
        "searchTermView": {"searchTerm": term},
        "metrics": metrics(cost_dollars, clicks, impressions, conversions),
    }


def keyword_row(
    text,
    qs=None,
    cost_dollars=0.0,
    creative="UNKNOWN",
    landing="UNKNOWN",
    ctr="UNKNOWN",
    clicks=0,
    conversions=0.0,
):
    qinfo = {
        "creativeQualityScore": creative,
        "postClickQualityScore": landing,
        "searchPredictedCtr": ctr,
    }
    if qs is not None:
        qinfo["qualityScore"] = qs
    return {
        "adGroupCriterion": {
            "keyword": {"text": text, "matchType": "EXACT"},
            "qualityInfo": qinfo,
        },
        "metrics": metrics(cost_dollars, clicks=clicks, conversions=conversions),
    }


def impression_share_row(name, is_=0.0, lost_budget=0.0, lost_rank=0.0, campaign_id="1"):
    return {
        "campaign": {"id": campaign_id, "name": name},
        "metrics": {
            "searchImpressionShare": is_,
            "searchBudgetLostImpressionShare": lost_budget,
            "searchRankLostImpressionShare": lost_rank,
            "costMicros": "0",
        },
    }


def daily_row(campaign_id, date, cost_dollars=0.0, clicks=10, impressions=100, conversions=1.0):
    return {
        "campaign": {"id": campaign_id},
        "segments": {"date": date},
        "metrics": metrics(cost_dollars, clicks, impressions, conversions),
    }


def daily_series(campaign_id, costs, conversions=None):
    rows = []
    for i, cost in enumerate(costs):
        date = "2024-01-%02d" % (i + 1)
        conv = conversions[i] if conversions is not None else 1.0
        rows.append(daily_row(campaign_id, date, cost, conversions=conv))
    return rows


class DiagnosticsTests(unittest.TestCase):
    def test_queries_exported(self):
        self.assertIsInstance(diagnostics.QUERIES, dict)
        for key in ("search_terms", "keywords", "impression_share", "daily"):
            self.assertIn(key, diagnostics.QUERIES)
            query = diagnostics.QUERIES[key]
            self.assertIsInstance(query, str)
            for placeholder in ("{cid}", "{since}", "{until}"):
                self.assertIn(placeholder, query)

        self.assertIn("search_term_view", diagnostics.QUERIES["search_terms"])
        self.assertIn("keyword_view", diagnostics.QUERIES["keywords"])
        self.assertIn("metrics.search_impression_share", diagnostics.QUERIES["impression_share"])
        self.assertIn("segments.date", diagnostics.QUERIES["daily"])

    def test_wasted_spend_empty(self):
        result = diagnostics.wasted_spend([])
        self.assertEqual(result["total_cost"], 0.0)
        self.assertEqual(result["wasted_cost"], 0.0)
        self.assertEqual(result["wasted_pct"], 0.0)
        self.assertEqual(result["terms"], [])

    def test_wasted_spend_filters_and_sorts(self):
        rows = [
            search_term_row("A", 10.0, clicks=1, impressions=10, conversions=0.0),
            search_term_row("B", 6.0, clicks=2, impressions=20, conversions=2.0),
            search_term_row("C", 8.0, clicks=3, impressions=30, conversions=0.0),
        ]
        result = diagnostics.wasted_spend(rows, min_cost=5.0)

        self.assertAlmostEqual(result["total_cost"], 24.0)
        self.assertAlmostEqual(result["wasted_cost"], 18.0)
        self.assertEqual(len(result["terms"]), 2)
        self.assertEqual(result["terms"][0]["term"], "A")
        self.assertAlmostEqual(result["terms"][0]["cost"], 10.0)
        self.assertEqual(result["terms"][1]["term"], "C")
        self.assertAlmostEqual(result["terms"][1]["cost"], 8.0)
        self.assertGreater(result["wasted_pct"], 0)
        self.assertTrue(
            {"term", "cost", "clicks", "impressions"}.issubset(result["terms"][0])
        )

    def test_wasted_spend_min_cost_boundary(self):
        rows = [
            search_term_row("at", 5.0, conversions=0.0),
            search_term_row("below", 4.99, conversions=0.0),
        ]
        result = diagnostics.wasted_spend(rows, min_cost=5.0)

        self.assertEqual(len(result["terms"]), 1)
        self.assertEqual(result["terms"][0]["term"], "at")
        self.assertAlmostEqual(result["wasted_cost"], 5.0)

    def test_wasted_spend_top_limit(self):
        rows = [
            search_term_row("t0", 10.0, conversions=0.0),
            search_term_row("t1", 8.0, conversions=0.0),
            search_term_row("t2", 6.0, conversions=0.0),
            search_term_row("t3", 5.0, conversions=1.0),
        ]
        result = diagnostics.wasted_spend(rows, min_cost=5.0, top=2)

        self.assertEqual(len(result["terms"]), 2)
        self.assertEqual([t["term"] for t in result["terms"]], ["t0", "t1"])
        self.assertAlmostEqual(result["total_cost"], 29.0)
        self.assertAlmostEqual(result["wasted_cost"], 24.0)

    def test_wasted_spend_no_wasted(self):
        rows = [
            search_term_row("conv", 10.0, conversions=1.0),
            search_term_row("conv2", 6.0, conversions=1.0),
        ]
        result = diagnostics.wasted_spend(rows, min_cost=5.0)

        self.assertEqual(result["terms"], [])
        self.assertAlmostEqual(result["wasted_cost"], 0.0)
        self.assertAlmostEqual(result["wasted_pct"], 0.0)
        self.assertAlmostEqual(result["total_cost"], 16.0)

    def test_wasted_spend_does_not_mutate_input(self):
        rows = [search_term_row("A", 10.0, conversions=0.0)]
        before = repr(rows)
        diagnostics.wasted_spend(rows)
        self.assertEqual(repr(rows), before)

    def test_impression_share_diagnosis(self):
        rows = [
            impression_share_row("Budget", is_=0.5, lost_budget=0.2, lost_rank=0.0),
            impression_share_row("Rank", is_=0.5, lost_budget=0.0, lost_rank=0.3),
            impression_share_row("Healthy", is_=0.5, lost_budget=0.19, lost_rank=0.29),
        ]
        result = diagnostics.impression_share(rows)

        self.assertEqual(len(result), 3)
        self.assertEqual(
            {r["diagnosis"] for r in result},
            {"budget-limited", "rank-limited", "healthy"},
        )
        for row in result:
            self.assertIn("campaign", row)
            for key in ("is", "lost_budget", "lost_rank"):
                self.assertIsInstance(row[key], float)
                self.assertGreaterEqual(row[key], 0.0)
                self.assertLessEqual(row[key], 1.0)

    def test_impression_share_parses_bounds(self):
        rows = [
            impression_share_row(
                "Bounded", is_="< 0.1", lost_budget="> 0.9", lost_rank="0.15"
            )
        ]
        result = diagnostics.impression_share(rows)[0]

        self.assertAlmostEqual(result["is"], 0.09)
        self.assertAlmostEqual(result["lost_budget"], 0.91)
        self.assertAlmostEqual(result["lost_rank"], 0.15)
        self.assertEqual(result["diagnosis"], "budget-limited")

    def test_quality_scores_weighted_low_and_components(self):
        rows = [
            keyword_row(
                "k1",
                qs=8,
                cost_dollars=10.0,
                creative="ABOVE_AVERAGE",
                landing="AVERAGE",
                ctr="BELOW_AVERAGE",
            ),
            keyword_row(
                "k2",
                qs=2,
                cost_dollars=30.0,
                creative="BELOW_AVERAGE",
                landing="BELOW_AVERAGE",
                ctr="BELOW_AVERAGE",
            ),
            keyword_row(
                "k3",
                qs=4,
                cost_dollars=20.0,
                creative="AVERAGE",
                landing="ABOVE_AVERAGE",
                ctr="ABOVE_AVERAGE",
            ),
            keyword_row(
                "k4",
                qs=None,
                cost_dollars=50.0,
                creative="UNKNOWN",
                landing="UNKNOWN",
                ctr="UNKNOWN",
            ),
        ]
        result = diagnostics.quality_scores(rows)

        expected = (8 * 10.0 + 2 * 30.0 + 4 * 20.0) / (10.0 + 30.0 + 20.0)
        self.assertAlmostEqual(result["weighted_qs"], expected, places=6)
        self.assertEqual(len(result["low_qs"]), 2)

        counts = result["component_counts"]
        for part in ("creative", "landing", "ctr"):
            self.assertIn(part, counts)

        self.assertEqual(counts["creative"].get("BELOW_AVERAGE", 0), 1)
        self.assertEqual(counts["creative"].get("ABOVE_AVERAGE", 0), 1)
        self.assertEqual(counts["creative"].get("AVERAGE", 0), 1)

        self.assertEqual(counts["landing"].get("BELOW_AVERAGE", 0), 1)
        self.assertEqual(counts["landing"].get("AVERAGE", 0), 1)
        self.assertEqual(counts["landing"].get("ABOVE_AVERAGE", 0), 1)

        self.assertEqual(counts["ctr"].get("BELOW_AVERAGE", 0), 2)
        self.assertEqual(counts["ctr"].get("ABOVE_AVERAGE", 0), 1)

    def test_quality_scores_ignores_missing_qs(self):
        rows = [
            keyword_row("has", qs=5, cost_dollars=10.0),
            keyword_row("missing", qs=None, cost_dollars=100.0),
        ]
        result = diagnostics.quality_scores(rows)

        self.assertAlmostEqual(result["weighted_qs"], 5.0)
        self.assertEqual(result["low_qs"], [])

    def test_quality_scores_empty(self):
        result = diagnostics.quality_scores([])

        self.assertIn("weighted_qs", result)
        self.assertEqual(result["low_qs"], [])
        self.assertIn("component_counts", result)
        for part in ("creative", "landing", "ctr"):
            self.assertIn(part, result["component_counts"])

    def test_anomalies_empty(self):
        self.assertEqual(diagnostics.anomalies([]), [])

    def test_anomalies_requires_min_days_history(self):
        rows = daily_series("1", [10.0] * 6 + [100.0])
        self.assertEqual(diagnostics.anomalies(rows, min_days=7), [])

    def test_anomalies_detects_cost_spike(self):
        rows = daily_series(
            "123", [10.0, 10.5, 9.5, 10.0, 10.2, 9.8, 10.0, 100.0]
        )
        result = diagnostics.anomalies(rows)

        self.assertTrue(result)
        cost_items = [r for r in result if r["metric"] == "cost"]
        self.assertTrue(cost_items)

        item = cost_items[0]
        self.assertEqual(item["campaign_id"], "123")
        self.assertEqual(item["direction"], "up")
        self.assertEqual(item["date"], "2024-01-08")
        self.assertGreater(item["z"], 2.5)
        self.assertGreater(item["value"], item["baseline_mean"])
        for key in (
            "campaign_id",
            "metric",
            "date",
            "value",
            "baseline_mean",
            "z",
            "direction",
            "explanation",
        ):
            self.assertIn(key, item)

    def test_anomalies_zero_stdev_identical_last_day(self):
        rows = daily_series("1", [10.0] * 8)
        self.assertEqual(diagnostics.anomalies(rows), [])

    def test_anomalies_zero_stdev_large_change(self):
        rows = daily_series("1", [10.0] * 7 + [20.0])
        result = diagnostics.anomalies(rows)

        self.assertTrue(
            any(r["metric"] == "cost" and r["direction"] == "up" for r in result)
        )

    def test_anomalies_below_z_threshold(self):
        rows = daily_series(
            "1", [10.0, 10.5, 9.5, 10.0, 10.2, 9.8, 10.0, 10.1]
        )
        result = diagnostics.anomalies(rows, z=2.5)

        self.assertFalse(any(r["metric"] == "cost" for r in result))

    def test_anomalies_direction_down(self):
        rows = daily_series(
            "1", [10.0, 10.5, 9.5, 10.0, 10.2, 9.8, 10.0, 1.0]
        )
        result = diagnostics.anomalies(rows)

        cost_items = [r for r in result if r["metric"] == "cost"]
        self.assertTrue(cost_items)
        self.assertEqual(cost_items[0]["direction"], "down")

    def test_anomalies_cpa_skips_zero_conversion_last_day(self):
        rows = daily_series(
            "1", [10.0] * 7 + [100.0], conversions=[1.0] * 7 + [0.0]
        )
        result = diagnostics.anomalies(rows)

        self.assertFalse(any(r["metric"] == "cpa" for r in result))
        self.assertTrue(any(r["metric"] == "cost" for r in result))

    def test_bundle_combines_all_four(self):
        rowsets = dict(
            search_terms=[search_term_row("waste", 100.0, conversions=0.0)],
            keywords=[keyword_row("kw", qs=1, cost_dollars=100.0)],
            impression_share=[impression_share_row("Camp", lost_budget=0.5)],
            daily=daily_series("1", [10.0] * 7 + [100.0]),
        )
        result = diagnostics.bundle(**rowsets)

        for key in (
            "wasted_spend",
            "impression_share",
            "quality_scores",
            "anomalies",
            "headline",
        ):
            self.assertIn(key, result)

        self.assertIsInstance(result["headline"], list)
        self.assertLessEqual(len(result["headline"]), 5)
        self.assertTrue(all(isinstance(h, str) for h in result["headline"]))
        self.assertGreater(result["wasted_spend"]["wasted_cost"], 0)
        self.assertEqual(len(result["quality_scores"]["low_qs"]), 1)
        self.assertEqual(result["impression_share"][0]["diagnosis"], "budget-limited")
        self.assertTrue(any(r["metric"] == "cost" for r in result["anomalies"]))

    def test_bundle_empty_rowsets(self):
        result = diagnostics.bundle()

        for key in (
            "wasted_spend",
            "impression_share",
            "quality_scores",
            "anomalies",
            "headline",
        ):
            self.assertIn(key, result)
        self.assertIsInstance(result["headline"], list)

    def test_bundle_headline_limited_to_five(self):
        search_terms = [
            search_term_row("waste%d" % i, 100.0 + i, conversions=0.0)
            for i in range(10)
        ]
        keywords = [
            keyword_row("kw%d" % i, qs=1, cost_dollars=100.0 + i)
            for i in range(10)
        ]
        impression_share = [
            impression_share_row("Camp%d" % i, lost_budget=0.5, campaign_id=str(i))
            for i in range(10)
        ]
        daily = daily_series("1", [10.0] * 7 + [100.0])

        result = diagnostics.bundle(
            search_terms=search_terms,
            keywords=keywords,
            impression_share=impression_share,
            daily=daily,
        )

        self.assertIsInstance(result["headline"], list)
        self.assertLessEqual(len(result["headline"]), 5)
        self.assertTrue(all(isinstance(h, str) for h in result["headline"]))
        self.assertGreater(len(result["headline"]), 0)


if __name__ == "__main__":
    unittest.main()
