"""Unit tests for adsops.oai_builder.draft().

The builder is pure (it must not write anything), so every test feeds a brief
dict and inspects the returned desired-state / policy pair. The creative
validator is always mocked - no real network or disk access happens here.
"""

import unittest
from unittest import mock
from urllib.parse import urlparse, parse_qs

import adsops.oai_builder as oai_builder


class _Validation(list):
    """Stand-in for whatever validate_card returns (list of errors, or
    a dict-like with an "errors" key). Behaves as a list of error strings."""

    def get(self, key, default=None):
        if key in ("errors", "error"):
            return list(self)
        return default

    def __getitem__(self, item):
        if isinstance(item, str):
            return self.get(item)
        return list.__getitem__(self, item)

    @property
    def errors(self):
        return list(self)


def make_brief(**overrides):
    brief = {
        "key": "acme_chatgpt",
        "ad_account_id": "adacct_12345",
        "approvers": ["Dana Ops"],
        "business": "Acme makes industrial widgets",
        "landing_url": "https://www.example.com/signup",
        "offer": "Start a free 14-day trial",
        "daily_budget": 40,
        "lifetime_cap": 500,
        "max_bid": 5,
        "countries": ["US"],
        "context_hints": ["best industrial widgets", "widget supplier for teams"],
        "ads": [
            {"title": "Acme Widgets", "body": "Industrial widgets that last.", "file_id": "file_abc"},
        ],
        "conversion_event": "lead_created",
        "utm_campaign": "acme_chatgpt",
    }
    brief.update(overrides)
    return brief


class OaiBuilderTests(unittest.TestCase):
    def setUp(self):
        # Patch the validator wherever the builder imported it from.
        if hasattr(oai_builder, "validate_card"):
            self._patch = mock.patch.object(oai_builder, "validate_card")
        else:
            self._patch = mock.patch("adsops.oai_creative.validate_card")
        self.validate_mock = self._patch.start()
        self.addCleanup(self._patch.stop)
        self.validate_mock.side_effect = lambda ad: _Validation()

    # ------------------------------------------------------------------ helpers
    def _campaign(self, state):
        return state.get("campaign", state)

    def _ad_groups(self, state):
        return state.get("ad_groups") or state.get("adGroups") or []

    def _ads(self, state):
        ads = []
        for group in self._ad_groups(state):
            ads.extend(group.get("ads") or [])
        return ads

    def _approval(self, state):
        if "approval" in state:
            return state["approval"]
        campaign = self._campaign(state)
        return campaign.get("approval")

    def _assert_refused(self, brief):
        try:
            oai_builder.draft(brief)
        except Exception as exc:  # noqa: BLE001 - we assert on the type name
            self.assertIn(
                "ToolError",
                type(exc).__name__,
                msg="expected a ToolError refusal, got %r" % (exc,),
            )
        else:
            self.fail("draft() should have refused this brief")

    # ------------------------------------------------------------------- shape
    def test_returns_documented_top_level_keys(self):
        result = oai_builder.draft(make_brief())
        self.assertIsInstance(result, dict)
        for key in ("state", "policy", "checks", "next_steps"):
            self.assertIn(key, result)
        self.assertIsInstance(result["state"], dict)
        self.assertIsInstance(result["policy"], dict)
        self.assertIsInstance(result["checks"], list)
        self.assertIsInstance(result["next_steps"], list)

    def test_state_platform_is_openai_ads(self):
        state = oai_builder.draft(make_brief())["state"]
        platform = state.get("platform") or self._campaign(state).get("platform")
        self.assertEqual(platform, "openai_ads")

    def test_every_resource_is_created_paused(self):
        state = oai_builder.draft(make_brief())["state"]
        self.assertEqual(self._campaign(state)["status"], "paused")
        groups = self._ad_groups(state)
        self.assertTrue(groups)
        for group in groups:
            self.assertEqual(group["status"], "paused")
            for ad in group.get("ads") or []:
                self.assertEqual(ad["status"], "paused")

    def test_state_contains_no_resource_ids(self):
        state = oai_builder.draft(make_brief())["state"]
        forbidden = {"id", "campaign_id", "ad_group_id", "ad_id", "resource_id"}

        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield k
                    yield from walk(v)
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    yield from walk(v)

        present = set(walk(state)) & forbidden
        self.assertEqual(present, set(), "new resources must not carry ids: %s" % present)

    def test_approval_block_is_exact_and_inactive(self):
        state = oai_builder.draft(make_brief())["state"]
        approval = self._approval(state)
        self.assertIsNotNone(approval)
        self.assertEqual(
            set(approval),
            {"activate", "approved_by", "approved_on", "expires_on", "campaign_name"},
        )
        self.assertIs(approval["activate"], False)
        self.assertIsNone(approval["approved_by"])
        self.assertIsNone(approval["approved_on"])
        self.assertIsNone(approval["expires_on"])
        self.assertEqual(approval["campaign_name"], self._campaign(state)["name"])

    # ------------------------------------------------------------------ budget
    def test_campaign_budget_matches_brief(self):
        result = oai_builder.draft(make_brief(daily_budget=40, lifetime_cap=500))
        budget = self._campaign(result["state"])["budget"]
        self.assertAlmostEqual(float(budget["daily_usd"]), 40.0)
        self.assertNotIn("lifetime_usd", budget)
        self.assertAlmostEqual(float(result["policy"]["caps"]["max_lifetime_budget_usd"]), 500.0)

    def test_targeting_countries_are_carried_over(self):
        state = oai_builder.draft(make_brief(countries=["US"]))["state"]
        targeting = self._campaign(state)["targeting"]
        self.assertEqual(list(targeting["countries"]), ["US"])

    # -------------------------------------------------------------------- utm
    def test_utm_parameters_on_every_ad_target_url(self):
        brief = make_brief(utm_campaign="acme_chatgpt")
        state = oai_builder.draft(brief)["state"]
        ads = self._ads(state)
        self.assertTrue(ads)
        for ad in ads:
            url = ad["target_url"]
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query.get("utm_source"), ["chatgpt"])
            self.assertEqual(query.get("utm_medium"), ["paid"])
            self.assertEqual(query.get("utm_campaign"), ["acme_chatgpt"])
            self.assertEqual(urlparse(url).scheme, "https")
            self.assertEqual(urlparse(url).hostname, "www.example.com")

    def test_utm_campaign_defaults_to_key(self):
        brief = make_brief()
        del brief["utm_campaign"]
        state = oai_builder.draft(brief)["state"]
        for ad in self._ads(state):
            query = parse_qs(urlparse(ad["target_url"]).query)
            self.assertEqual(query.get("utm_campaign"), ["acme_chatgpt"])

    # ----------------------------------------------------------------- policy
    def test_policy_identity_fields(self):
        policy = oai_builder.draft(make_brief())["policy"]
        self.assertEqual(policy.get("platform"), "openai_ads")
        self.assertEqual(policy.get("ad_account_id"), "adacct_12345")
        self.assertEqual(policy.get("campaign_key"), "acme_chatgpt")
        self.assertEqual(policy.get("currency"), "USD")

    def test_policy_allows_no_activation_by_default(self):
        policy = oai_builder.draft(make_brief())["policy"]
        self.assertIs(policy["allow_activate"], False)

    def test_policy_caps_mirror_brief_budgets(self):
        policy = oai_builder.draft(make_brief(daily_budget=40, lifetime_cap=500))["policy"]
        caps = policy["caps"]
        self.assertAlmostEqual(float(caps["max_daily_budget_usd"]), 40.0)
        self.assertAlmostEqual(float(caps["max_lifetime_budget_usd"]), 500.0)

    def test_policy_caps_include_max_bid_when_given(self):
        policy = oai_builder.draft(make_brief(max_bid=7.5))["policy"]
        self.assertAlmostEqual(float(policy["caps"]["max_bid_usd"]), 7.5)

    def test_policy_caps_have_no_bid_when_not_given(self):
        policy = oai_builder.draft(make_brief(max_bid=None))["policy"]
        self.assertIsNone(policy["caps"].get("max_bid_usd"))

    def test_policy_allowed_domains_is_landing_host(self):
        policy = oai_builder.draft(make_brief())["policy"]
        self.assertEqual(policy["allowed_domains"], ["www.example.com"])

    def test_policy_required_utm(self):
        policy = oai_builder.draft(make_brief())["policy"]
        required = policy["required_utm"]
        self.assertEqual(required["utm_source"], "chatgpt")
        self.assertEqual(required["utm_medium"], "paid")
        # utm_campaign may be echoed literally or as the "{campaign_key}" token.
        self.assertIn(required["utm_campaign"], ("acme_chatgpt", "{campaign_key}"))

    # ---------------------------------------------------------------- creative
    def test_each_ad_is_validated(self):
        ads = [
            {"title": "Acme Widgets", "body": "Widgets for teams.", "file_id": "file_1"},
            {"title": "Acme Widgets Pro", "body": "Heavy duty widgets.", "file_ids": ["file_2", "file_3"]},
        ]
        oai_builder.draft(make_brief(ads=ads))
        self.assertEqual(self.validate_mock.call_count, len(ads))

    def test_validator_errors_end_up_in_checks(self):
        self.validate_mock.side_effect = lambda ad: _Validation(["title must be 3-50 characters"])
        result = oai_builder.draft(make_brief())
        checks = result["checks"]
        self.assertTrue(
            any("3-50" in str(c) for c in checks),
            "validator errors must surface in checks, got %r" % (checks,),
        )

    def test_checks_is_a_list_when_all_ads_are_valid(self):
        result = oai_builder.draft(make_brief())
        self.assertIsInstance(result["checks"], list)

    # ------------------------------------------------------------- next steps
    def test_next_steps_are_ordered_strings_ending_in_activation(self):
        steps = oai_builder.draft(make_brief())["next_steps"]
        self.assertTrue(steps)
        for step in steps:
            self.assertIsInstance(step, str)
            self.assertTrue(step.strip())
        self.assertIn("activat", steps[-1].lower())

    # --------------------------------------------------------------- refusals
    def test_refuses_when_two_x_overdelivery_exceeds_lifetime_cap(self):
        self._assert_refused(make_brief(daily_budget=40, lifetime_cap=79))

    def test_accepts_exact_two_x_overdelivery_boundary(self):
        result = oai_builder.draft(make_brief(daily_budget=40, lifetime_cap=80))
        self.assertIn("state", result)

    def test_refuses_non_https_landing_url(self):
        self._assert_refused(make_brief(landing_url="http://www.example.com/signup"))

    def test_refuses_missing_ads(self):
        self._assert_refused(make_brief(ads=[]))
        brief = make_brief()
        del brief["ads"]
        self._assert_refused(brief)

    def test_refuses_key_that_is_not_a_slug(self):
        for bad_key in ("Acme ChatGPT", "acme/chatgpt", "", "acme.chatgpt"):
            with self.subTest(key=bad_key):
                self._assert_refused(make_brief(key=bad_key))

    def test_refuses_invalid_ad_account_id(self):
        for bad_account in ("acct_123", "", "adacct_", "adacct_123 45", None):
            with self.subTest(ad_account_id=bad_account):
                self._assert_refused(make_brief(ad_account_id=bad_account))

    def test_accepts_ad_account_id_with_allowed_separators(self):
        result = oai_builder.draft(make_brief(ad_account_id="adacct_Acme-1_2"))
        self.assertEqual(result["policy"]["ad_account_id"], "adacct_Acme-1_2")


if __name__ == "__main__":
    unittest.main()