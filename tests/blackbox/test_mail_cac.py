"""Unit tests for adsops.mail.cac (attribution, channel report, breakeven, piece cost).

Only documented public behaviour is exercised; all data is synthetic and no I/O
is performed by the module under test.
"""

import unittest

from adsops.errors import ToolError
from adsops.mail.cac import attribute, breakeven, channel_report, piece_cost


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def lead_google(i, date, stage="lead"):
    return {"id": i, "created_at": date, "stage": stage,
            "utm_source": "google", "utm_medium": "cpc"}


def lead_meta(i, date, stage="lead"):
    return {"id": i, "created_at": date, "stage": stage,
            "utm_source": "facebook", "utm_medium": "paid"}


def lead_mail(i, date, stage="lead"):
    return {"id": i, "created_at": date, "stage": stage,
            "utm_source": "direct_mail", "utm_medium": "postcard"}


def lead_unknown(i, date, stage="lead"):
    return {"id": i, "created_at": date, "stage": stage}


def channel_row(report, name):
    """Find the per-channel row in a channel_report() result."""
    for row in report["channels"]:
        if row["channel"] == name:
            return row
    return None


def ranking_names(ranking):
    """Ranking entries may be channel names or channel dicts - normalise."""
    names = []
    for item in ranking:
        if isinstance(item, dict):
            names.append(item.get("channel"))
        else:
            names.append(item)
    return names


class TestAttribute(unittest.TestCase):

    def assertChannel(self, lead, expected, **kwargs):
        result = attribute(lead, **kwargs)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        channel, method = result
        self.assertEqual(channel, expected)
        self.assertIsInstance(method, str)
        return method

    def test_google_utm(self):
        method = self.assertChannel(lead_google(1, "2024-01-01"), "google_ads")
        self.assertNotEqual(method, "none")

    def test_chatgpt_paid(self):
        self.assertChannel({"utm_source": "chatgpt.com", "utm_medium": "cpc"},
                           "chatgpt_ads")

    def test_ai_organic(self):
        for source in ("chatgpt.com", "perplexity.ai", "claude.ai"):
            self.assertChannel({"utm_source": source, "utm_medium": "organic"},
                               "ai_organic")

    def test_meta_paid(self):
        for source in ("facebook", "instagram", "meta"):
            self.assertChannel({"utm_source": source, "utm_medium": "paid"},
                               "meta_ads")

    def test_mailed_code(self):
        lead = {"id": 1, "tracking_code": "MAIL-77"}
        method = self.assertChannel(lead, "direct_mail",
                                    mailed_codes=frozenset({"MAIL-77", "MAIL-01"}))
        self.assertNotEqual(method, "none")

    def test_mailed_code_not_in_set(self):
        # a tracking code is only meaningful when it is a known mailed code
        channel, method = attribute({"id": 1, "tracking_code": "MAIL-77"})
        self.assertEqual((channel, method), ("unattributed", "none"))

    def test_code_rule_precedes_utm(self):
        lead = {"id": 1, "tracking_code": "MAIL-77",
                "utm_source": "google", "utm_medium": "cpc"}
        self.assertChannel(lead, "direct_mail",
                           mailed_codes=frozenset({"MAIL-77"}))

    def test_self_reported_texts(self):
        self.assertChannel({"self_reported": "I got a postcard in the mail"},
                           "direct_mail")
        self.assertChannel({"self_reported": "found via a friend referral"},
                           "referral")
        self.assertChannel({"self_reported": "googled it"}, "google_unknown")
        self.assertChannel({"self_reported": "asked chatgpt"}, "ai_unknown")

    def test_rules_order(self):
        lead = {"utm_source": "google", "utm_medium": "cpc",
                "self_reported": "postcard"}
        self.assertChannel(lead, "google_ads")
        self.assertChannel(lead, "direct_mail",
                           rules=("self_reported", "utm"))

    def test_unknown(self):
        self.assertEqual(attribute({"id": 1}), ("unattributed", "none"))
        self.assertEqual(attribute({"id": 2, "notes": "no hints"}),
                         ("unattributed", "none"))


class TestChannelReport(unittest.TestCase):

    def test_basic_metrics(self):
        costs = [{"channel": "google_ads", "date": "2024-03-01", "amount": 100.0},
                 {"channel": "google_ads", "date": "2024-03-05", "amount": 50.0}]
        leads = [lead_google(1, "2024-03-01", "client"),
                 lead_google(2, "2024-03-02"),
                 lead_google(3, "2024-03-03")]
        report = channel_report(costs, leads, client_value=500.0, min_n=1)

        self.assertEqual(len(report["channels"]), 1)
        row = channel_row(report, "google_ads")
        self.assertIsNotNone(row)
        self.assertEqual(row["spend"], 150.0)
        self.assertEqual(row["leads"], 3)
        self.assertEqual(row["clients"], 1)
        self.assertAlmostEqual(row["cpl"], 50.0)
        self.assertAlmostEqual(row["cac"], 150.0)
        self.assertAlmostEqual(row["lead_to_client"], 1.0 / 3.0, places=4)
        self.assertAlmostEqual(row["roas"], 500.0 / 150.0, places=4)

    def test_period_filters_costs_and_leads(self):
        costs = [{"channel": "google_ads", "date": "2024-01-10", "amount": 40.0},
                 {"channel": "google_ads", "date": "2024-03-10", "amount": 999.0}]
        leads = [lead_google(1, "2024-01-15"), lead_google(2, "2024-03-15")]
        report = channel_report(costs, leads,
                                period=("2024-01-01", "2024-01-31"), min_n=0)
        row = channel_row(report, "google_ads")
        self.assertEqual(row["spend"], 40.0)
        self.assertEqual(row["leads"], 1)

    def test_lead_only_channel_has_no_cost_data(self):
        report = channel_report([], [lead_meta(1, "2024-08-01")], min_n=0)
        row = channel_row(report, "meta_ads")
        self.assertIsNotNone(row)
        self.assertEqual(row["spend"], 0.0)
        self.assertIn("no cost data", row.get("note") or "")

    def test_spend_without_leads(self):
        costs = [{"channel": "meta_ads", "date": "2024-09-01", "amount": 75.0}]
        report = channel_report(costs, [], min_n=0)
        row = channel_row(report, "meta_ads")
        self.assertEqual(row["leads"], 0)
        self.assertIsNone(row["cpl"])
        self.assertIsNone(row["cac"])
        self.assertIsNone(row["cpql"])
        self.assertIn("spend without leads", row.get("note") or "")

    def test_too_few_note(self):
        costs = [{"channel": "google_ads", "date": "2024-10-01", "amount": 100.0}]
        leads = [lead_google(1, "2024-10-02"), lead_google(2, "2024-10-03")]

        report = channel_report(costs, leads)  # default min_n=5
        self.assertIn("too few to judge",
                      channel_row(report, "google_ads").get("note") or "")

        report_ok = channel_report(costs, leads, min_n=2)
        self.assertNotIn("too few to judge",
                         channel_row(report_ok, "google_ads").get("note") or "")

    def test_spend_rounded_to_two_dp(self):
        costs = [{"channel": "meta_ads", "date": "2024-11-01", "amount": 0.1},
                 {"channel": "meta_ads", "date": "2024-11-02", "amount": 0.2}]
        report = channel_report(costs, [], min_n=0)
        self.assertEqual(channel_row(report, "meta_ads")["spend"], 0.3)
        self.assertEqual(report["total"]["spend"], 0.3)

    def test_unattributed_and_methods(self):
        leads = [lead_google(1, "2024-06-01"),
                 lead_google(2, "2024-06-02"),
                 lead_unknown(3, "2024-06-03")]
        report = channel_report([], leads, min_n=0)

        self.assertEqual(report["unattributed"], 1)
        methods = report["attribution_methods"]
        self.assertIsInstance(methods, dict)
        expected_method = attribute(lead_google(99, "2024-06-01"))[1]
        self.assertEqual(methods.get(expected_method), 2)

    def test_total(self):
        costs = [{"channel": "google_ads", "date": "2024-02-01", "amount": 100.0},
                 {"channel": "meta_ads", "date": "2024-02-01", "amount": 100.0}]
        leads = [lead_google(1, "2024-02-02", "client"),
                 lead_google(2, "2024-02-03"),
                 lead_meta(3, "2024-02-04")]
        report = channel_report(costs, leads, min_n=0)

        total = report["total"]
        self.assertEqual(total["spend"], 200.0)
        self.assertEqual(total["leads"], 3)
        self.assertEqual(total["clients"], 1)

    def test_ranking_by_cac_none_last(self):
        costs = [{"channel": "google_ads", "date": "2024-05-01", "amount": 100.0},
                 {"channel": "meta_ads", "date": "2024-05-01", "amount": 300.0},
                 {"channel": "direct_mail", "date": "2024-05-01", "amount": 50.0}]
        leads = [lead_google(1, "2024-05-02", "client"),
                 lead_meta(2, "2024-05-02", "client"),
                 lead_mail(3, "2024-05-02")]
        report = channel_report(costs, leads, min_n=0)

        names = ranking_names(report["ranking"])
        self.assertEqual(len(names), 3)
        self.assertEqual(names[0], "google_ads")    # cac 100
        self.assertEqual(names[1], "meta_ads")      # cac 300
        self.assertEqual(names[2], "direct_mail")   # no clients -> cac None

    def test_roas_requires_client_value(self):
        costs = [{"channel": "google_ads", "date": "2024-12-01", "amount": 100.0}]
        leads = [lead_google(1, "2024-12-02", "client")]

        without = channel_report(costs, leads, min_n=0)
        self.assertIsNone(channel_row(without, "google_ads").get("roas"))

        with_value = channel_report(costs, leads, client_value=250.0, min_n=0)
        self.assertAlmostEqual(channel_row(with_value, "google_ads")["roas"], 2.5)

    def test_no_clients_denominators(self):
        costs = [{"channel": "google_ads", "date": "2024-04-01", "amount": 100.0}]
        leads = [lead_google(1, "2024-04-02", "qualified"),
                 lead_google(2, "2024-04-03")]
        report = channel_report(costs, leads, min_n=0)
        row = channel_row(report, "google_ads")

        self.assertEqual(row["clients"], 0)
        self.assertEqual(row["qualified"], 1)
        self.assertIsNone(row["cac"])
        self.assertAlmostEqual(row["cpl"], 50.0)
        self.assertAlmostEqual(row["cpql"], 100.0)
        self.assertIn(row["lead_to_client"], (0.0, None))

    def test_mailed_codes_passed_through(self):
        costs = [{"channel": "direct_mail", "date": "2024-07-01", "amount": 60.0}]
        leads = [{"id": 1, "created_at": "2024-07-02", "stage": "lead",
                  "tracking_code": "MAIL9"}]

        with_codes = channel_report(costs, leads,
                                    mailed_codes=frozenset({"MAIL9"}), min_n=0)
        row = channel_row(with_codes, "direct_mail")
        self.assertEqual(row["spend"], 60.0)
        self.assertEqual(row["leads"], 1)
        self.assertEqual(with_codes["unattributed"], 0)

        without_codes = channel_report(costs, leads, min_n=0)
        self.assertEqual(without_codes["unattributed"], 1)
        self.assertEqual(channel_row(without_codes, "direct_mail")["leads"], 0)


class TestBreakeven(unittest.TestCase):

    def test_values(self):
        result = breakeven(cost_per_piece=0.5, lead_to_client=0.1,
                           client_value=1000.0)
        self.assertAlmostEqual(result["breakeven_lead_rate"],
                               0.5 / (0.1 * 1000.0))
        self.assertAlmostEqual(result["max_cpl"], 1000.0 * 0.1)
        self.assertAlmostEqual(
            result["pieces_per_lead_at_breakeven"] * result["breakeven_lead_rate"],
            1.0)

    def test_target_roas_scales(self):
        base = breakeven(0.5, 0.1, 1000.0, target_roas=1.0)
        hard = breakeven(0.5, 0.1, 1000.0, target_roas=2.0)
        self.assertAlmostEqual(hard["max_cpl"], base["max_cpl"] / 2.0)
        self.assertAlmostEqual(hard["breakeven_lead_rate"],
                               base["breakeven_lead_rate"] * 2.0)

    def test_validation(self):
        good = {"cost_per_piece": 1.0, "lead_to_client": 0.1, "client_value": 100.0}
        bad = [{"cost_per_piece": 0.0},
               {"cost_per_piece": -1.0},
               {"lead_to_client": 0.0},
               {"client_value": -5.0},
               {"target_roas": 0.0}]
        for override in bad:
            kwargs = dict(good)
            kwargs.update(override)
            with self.assertRaises((ValueError, ToolError)):
                breakeven(**kwargs)


class TestPieceCost(unittest.TestCase):

    def test_basic(self):
        result = piece_cost(1.0, 0.5, other=0.25, quantity=4)
        self.assertEqual(result["total"], 1.75)
        self.assertEqual(result["per_piece"], 0.4375)

    def test_rounding(self):
        result = piece_cost(1.0, 0.333, quantity=7)
        self.assertEqual(result["total"], 1.33)          # 2 dp
        self.assertEqual(result["per_piece"], 0.1904)    # 4 dp

    def test_defaults(self):
        result = piece_cost(0.75, 0.25)
        self.assertEqual(result["total"], 1.0)
        self.assertEqual(result["per_piece"], 1.0)


if __name__ == '__main__':
    unittest.main()