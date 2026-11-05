import unittest

from adsops.fleet import rollup, aggregate_check, enforce, RailViolation


def _row(account="A", platform="google", campaign="c", status="enabled",
         spend_today=0, spend_month=0, daily_budget=0, conv_month=0,
         currency="USD"):
    return {
        "account": account,
        "platform": platform,
        "campaign": campaign,
        "status": status,
        "spend_today": spend_today,
        "spend_month": spend_month,
        "daily_budget": daily_budget,
        "conv_month": conv_month,
        "currency": currency,
    }


class RollupTests(unittest.TestCase):
    def test_returns_required_top_level_keys(self):
        r = rollup([_row()])
        for k in ("by_account", "by_platform", "total", "currencies", "warnings"):
            self.assertIn(k, r)

    def test_by_account_aggregation(self):
        rows = [
            _row(account="A", campaign="c1", spend_today=10, spend_month=100,
                 daily_budget=50, conv_month=2),
            _row(account="A", campaign="c2", spend_today=5, spend_month=50,
                 daily_budget=20, conv_month=1),
        ]
        a = rollup(rows)["by_account"]["A"]
        for k in ("spend_today", "spend_month", "daily_budget_enabled",
                  "conv_month", "cpa_month", "campaigns_enabled",
                  "campaigns_total"):
            self.assertIn(k, a)
        self.assertAlmostEqual(a["spend_today"], 15)
        self.assertAlmostEqual(a["spend_month"], 150)
        self.assertAlmostEqual(a["daily_budget_enabled"], 70)
        self.assertAlmostEqual(a["conv_month"], 3)
        self.assertEqual(a["campaigns_enabled"], 2)
        self.assertEqual(a["campaigns_total"], 2)
        self.assertAlmostEqual(a["cpa_month"], 50.0)

    def test_campaign_count_excludes_non_enabled(self):
        rows = [
            _row(account="A", campaign="c1", status="enabled", daily_budget=10),
            _row(account="A", campaign="c2", status="paused", daily_budget=999),
        ]
        a = rollup(rows)["by_account"]["A"]
        self.assertEqual(a["campaigns_enabled"], 1)
        self.assertEqual(a["campaigns_total"], 2)
        # Disabled budget must not be counted in daily_budget_enabled.
        self.assertAlmostEqual(a["daily_budget_enabled"], 10)

    def test_by_platform_breakdown(self):
        rows = [
            _row(account="A", platform="google", campaign="g1", spend_today=10,
                 spend_month=100, daily_budget=5, conv_month=1),
            _row(account="B", platform="google", campaign="g2", spend_today=20,
                 spend_month=200, daily_budget=7, conv_month=2),
            _row(account="B", platform="meta", campaign="m1", spend_today=1,
                 spend_month=10, daily_budget=3, conv_month=0),
        ]
        r = rollup(rows)
        g = r["by_platform"]["google"]
        for k in ("spend_today", "spend_month", "daily_budget_enabled",
                  "conv_month", "cpa_month", "campaigns_enabled",
                  "campaigns_total"):
            self.assertIn(k, g)
        self.assertAlmostEqual(g["spend_today"], 30)
        self.assertAlmostEqual(g["spend_month"], 300)
        self.assertAlmostEqual(g["daily_budget_enabled"], 12)
        self.assertAlmostEqual(g["conv_month"], 3)
        self.assertEqual(g["campaigns_enabled"], 2)
        self.assertIn("meta", r["by_platform"])

    def test_single_currency_total_is_flat(self):
        rows = [
            _row(account="A", campaign="c1", spend_today=10, spend_month=100,
                 daily_budget=5),
            _row(account="B", campaign="c2", spend_today=20, spend_month=200,
                 daily_budget=7),
        ]
        r = rollup(rows)
        # Single currency: total is a flat aggregate, not keyed by currency.
        self.assertIn("spend_today", r["total"])
        self.assertAlmostEqual(r["total"]["spend_today"], 30)
        self.assertAlmostEqual(r["total"]["spend_month"], 300)
        self.assertAlmostEqual(r["total"]["daily_budget_enabled"], 12)

    def test_currencies_list(self):
        rows = [
            _row(account="A", currency="USD"),
            _row(account="B", currency="EUR"),
        ]
        currencies = rollup(rows)["currencies"]
        self.assertIn("USD", currencies)
        self.assertIn("EUR", currencies)
        self.assertEqual(len(currencies), 2)

    def test_multi_currency_total_keyed_by_currency(self):
        rows = [
            _row(account="A", campaign="c1", currency="USD", spend_today=10,
                 spend_month=100, daily_budget=5),
            _row(account="B", campaign="c2", currency="EUR", spend_today=20,
                 spend_month=200, daily_budget=7),
        ]
        total = rollup(rows)["total"]
        self.assertIn("USD", total)
        self.assertIn("EUR", total)
        self.assertAlmostEqual(total["USD"]["spend_today"], 10)
        self.assertAlmostEqual(total["EUR"]["spend_today"], 20)
        self.assertAlmostEqual(total["USD"]["spend_month"], 100)
        self.assertAlmostEqual(total["EUR"]["spend_month"], 200)

    def test_never_sums_across_currencies(self):
        rows = [
            _row(account="A", campaign="c1", currency="USD", spend_today=10),
            _row(account="B", campaign="c2", currency="USD", spend_today=5),
            _row(account="C", campaign="c3", currency="EUR", spend_today=7),
        ]
        total = rollup(rows)["total"]
        self.assertAlmostEqual(total["USD"]["spend_today"], 15)
        self.assertAlmostEqual(total["EUR"]["spend_today"], 7)

    def test_mixed_currency_account_emits_warning(self):
        rows = [
            _row(account="A", campaign="c1", currency="USD"),
            _row(account="A", campaign="c2", currency="EUR"),
        ]
        warnings = rollup(rows)["warnings"]
        self.assertIsInstance(warnings, list)
        self.assertGreaterEqual(len(warnings), 1)

    def test_clean_data_has_no_warnings(self):
        rows = [
            _row(account="A", campaign="c1", currency="USD", daily_budget=5),
            _row(account="B", campaign="c2", currency="USD", daily_budget=5),
        ]
        self.assertEqual(rollup(rows)["warnings"], [])

    def test_empty_rows(self):
        r = rollup([])
        self.assertEqual(r["by_account"], {})
        self.assertEqual(r["by_platform"], {})
        self.assertIsInstance(r["warnings"], list)


class AggregateCheckTests(unittest.TestCase):
    def test_within_caps_ok(self):
        rows = [_row(daily_budget=10, spend_today=5)]
        res = aggregate_check(rows, {"daily_total": 1000})
        self.assertTrue(res["ok"])
        self.assertEqual(res["violations"], [])
        self.assertIn("headroom_daily", res)

    def test_daily_cap_exceeded(self):
        # Enabled daily budget 100; worst case exposure = 100 * 2 = 200 > 150.
        rows = [_row(daily_budget=100)]
        res = aggregate_check(rows, {"daily_total": 150})
        self.assertFalse(res["ok"])
        self.assertGreaterEqual(len(res["violations"]), 1)
        for v in res["violations"]:
            for k in ("scope", "limit", "exposure", "detail"):
                self.assertIn(k, v)

    def test_default_overspend_factor_is_two(self):
        rows = [_row(daily_budget=100)]  # exposure = 200
        self.assertFalse(aggregate_check(rows, {"daily_total": 199})["ok"])
        self.assertTrue(aggregate_check(rows, {"daily_total": 200})["ok"])

    def test_custom_overspend_factor(self):
        rows = [_row(daily_budget=100)]  # 100 * 1.5 = 150
        caps = {"daily_total": 160, "overspend_factor": 1.5}
        self.assertTrue(aggregate_check(rows, caps)["ok"])
        caps_bad = {"daily_total": 140, "overspend_factor": 1.5}
        self.assertFalse(aggregate_check(rows, caps_bad)["ok"])

    def test_headroom_daily_value(self):
        rows = [_row(daily_budget=10)]  # exposure = 20
        res = aggregate_check(rows, {"daily_total": 100})
        self.assertAlmostEqual(res["headroom_daily"], 80)

    def test_per_account_cap(self):
        rows = [_row(account="A", daily_budget=100)]  # exposure 200
        caps = {"daily_total": 100000, "per_account": {"A": {"daily": 150}}}
        res = aggregate_check(rows, caps)
        self.assertFalse(res["ok"])
        self.assertGreaterEqual(len(res["violations"]), 1)

    def test_per_account_cap_satisfied(self):
        rows = [_row(account="A", daily_budget=10)]  # exposure 20
        caps = {"daily_total": 100000, "per_account": {"A": {"daily": 1000}}}
        self.assertTrue(aggregate_check(rows, caps)["ok"])

    def test_proposal_raising_exposure_over_cap_fails(self):
        rows = [_row(account="A", campaign="c1", daily_budget=10)]  # exposure 20
        caps = {"daily_total": 25}
        self.assertTrue(aggregate_check(rows, caps)["ok"])
        proposed = [{"account": "A", "campaign": "c1", "daily_budget": 50,
                     "status": "enabled"}]
        res = aggregate_check(rows, caps, proposed=proposed)
        self.assertFalse(res["ok"])

    def test_proposal_adding_campaign_counts(self):
        rows = [_row(account="A", campaign="c1", daily_budget=10)]
        caps = {"daily_total": 100}
        proposed = [{"account": "A", "campaign": "new", "daily_budget": 1000,
                     "status": "enabled"}]
        self.assertFalse(aggregate_check(rows, caps, proposed=proposed)["ok"])

    def test_proposal_lowering_exposure_is_ok(self):
        rows = [_row(account="A", campaign="c1", daily_budget=100)]  # exposure 200
        caps = {"daily_total": 100}
        self.assertFalse(aggregate_check(rows, caps)["ok"])
        proposed = [{"account": "A", "campaign": "c1", "daily_budget": 40,
                     "status": "enabled"}]  # exposure 80 <= 100
        self.assertTrue(aggregate_check(rows, caps, proposed=proposed)["ok"])

    def test_disabled_campaign_not_in_exposure(self):
        rows = [
            _row(campaign="on", status="enabled", daily_budget=10),
            _row(campaign="off", status="paused", daily_budget=1000),
        ]
        # Only the enabled 10 counts -> exposure 20.
        self.assertTrue(aggregate_check(rows, {"daily_total": 100})["ok"])


class EnforceTests(unittest.TestCase):
    def test_no_raise_when_within_caps(self):
        rows = [_row(daily_budget=1)]
        enforce(rows, {"daily_total": 1000}, [])

    def test_raises_when_over_and_raises_exposure(self):
        rows = [_row(account="A", campaign="c1", daily_budget=10)]
        caps = {"daily_total": 25}
        proposed = [{"account": "A", "campaign": "c1", "daily_budget": 100,
                     "status": "enabled"}]
        with self.assertRaises(RailViolation):
            enforce(rows, caps, proposed)

    def test_no_raise_when_proposal_lowers_exposure(self):
        rows = [_row(account="A", campaign="c1", daily_budget=100)]
        caps = {"daily_total": 100}
        proposed = [{"account": "A", "campaign": "c1", "daily_budget": 10,
                     "status": "enabled"}]
        enforce(rows, caps, proposed)


if __name__ == '__main__':
    unittest.main()