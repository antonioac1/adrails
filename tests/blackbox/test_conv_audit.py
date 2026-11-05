import unittest

from adsops.conv_audit import QUERIES, audit


SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _mk_action(
    action_id,
    name="Action",
    status="ENABLED",
    primary=True,
    category="PURCHASE",
    counting_type="ONE_PER_CLICK",
    lookback=30,
    default_value=100,
    include=True,
):
    """Build a GAQL-ish action row with both snake_case and camelCase aliases."""
    ca = {
        "id": action_id,
        "name": name,
        "status": status,
        "type": "WEBPAGE",
        "category": category,
        "primary_for_goal": primary,
        "primaryForGoal": primary,
        "counting_type": counting_type,
        "countingType": counting_type,
        "click_through_lookback_window_days": lookback,
        "clickThroughLookbackWindowDays": lookback,
        "include_in_conversions_metric": include,
        "value_settings": {
            "default_value": default_value,
            "defaultValue": default_value,
        },
    }
    row = {
        "conversion_action": ca,
        "conversionAction": ca,
        "id": action_id,
        "name": name,
        "status": status,
        "category": category,
        "primary_for_goal": primary,
        "primaryForGoal": primary,
        "counting_type": counting_type,
        "countingType": counting_type,
        "click_through_lookback_window_days": lookback,
        "clickThroughLookbackWindowDays": lookback,
        "include_in_conversions_metric": include,
        "value_settings": ca["value_settings"],
        "default_value": default_value,
        "defaultValue": default_value,
        "conversion_action.id": action_id,
        "conversion_action.name": name,
        "conversion_action.status": status,
        "conversion_action.category": category,
        "conversion_action.primary_for_goal": primary,
        "conversion_action.counting_type": counting_type,
        "conversion_action.click_through_lookback_window_days": lookback,
        "conversion_action.value_settings.default_value": default_value,
        "conversion_action.value_settings.defaultValue": default_value,
    }
    return row


def _mk_action_stat(action_id, all_conversions, customer_id="123"):
    resource = f"customers/{customer_id}/conversionActions/{action_id}"
    return {
        "segments": {
            "conversionAction": resource,
            "conversion_action": resource,
        },
        "metrics": {
            "all_conversions": all_conversions,
            "allConversions": all_conversions,
            "conversions": all_conversions,
        },
        "segments.conversionAction": resource,
        "segments.conversion_action": resource,
        "metrics.all_conversions": all_conversions,
        "metrics.allConversions": all_conversions,
        "metrics.conversions": all_conversions,
    }


def _mk_campaign(
    campaign_id=1,
    name="Campaign",
    status="ENABLED",
    bidding="MANUAL_CPC",
    cost_units=10,
    conversions=20,
    clicks=100,
):
    cost_micros = int(round(cost_units * 1_000_000))
    cmp_obj = {
        "id": campaign_id,
        "name": name,
        "status": status,
        "bidding_strategy_type": bidding,
        "biddingStrategyType": bidding,
    }
    return {
        "campaign": cmp_obj,
        "campaign.id": campaign_id,
        "campaign.name": name,
        "campaign.status": status,
        "campaign.bidding_strategy_type": bidding,
        "campaign.biddingStrategyType": bidding,
        "metrics": {
            "cost_micros": cost_micros,
            "costMicros": cost_micros,
            "clicks": clicks,
            "conversions": conversions,
            "all_conversions": conversions,
            "allConversions": conversions,
        },
        "metrics.cost_micros": cost_micros,
        "metrics.costMicros": cost_micros,
        "metrics.clicks": clicks,
        "metrics.conversions": conversions,
        "metrics.all_conversions": conversions,
        "metrics.allConversions": conversions,
    }


def _mk_customer(auto_tagging=True, tracking_status="CONVERSION_TRACKED", enhanced=True):
    setting = {
        "conversion_tracking_status": tracking_status,
        "conversionTrackingStatus": tracking_status,
        "enhanced_conversions_for_leads_enabled": enhanced,
        "enhancedConversionsForLeadsEnabled": enhanced,
    }
    cust = {
        "id": 123,
        "auto_tagging_enabled": auto_tagging,
        "autoTaggingEnabled": auto_tagging,
        "conversion_tracking_setting": setting,
        "conversionTrackingSetting": setting,
        "conversion_tracking_status": tracking_status,
        "conversionTrackingStatus": tracking_status,
        "enhanced_conversions_for_leads_enabled": enhanced,
        "enhancedConversionsForLeadsEnabled": enhanced,
    }
    return {
        "customer": cust,
        "customer.id": 123,
        "customer.auto_tagging_enabled": auto_tagging,
        "customer.autoTaggingEnabled": auto_tagging,
        "customer.conversion_tracking_status": tracking_status,
        "customer.conversionTrackingStatus": tracking_status,
        "customer.conversion_tracking_setting": setting,
        "customer.conversionTrackingSetting": setting,
        "auto_tagging_enabled": auto_tagging,
        "autoTaggingEnabled": auto_tagging,
        "conversion_tracking_status": tracking_status,
        "conversionTrackingStatus": tracking_status,
        "enhanced_conversions_for_leads_enabled": enhanced,
        "enhancedConversionsForLeadsEnabled": enhanced,
    }


def _drop_tracking_status(customer):
    """Remove all tracking-status fields so the API value is effectively missing."""
    for key in list(customer.keys()):
        lk = key.lower()
        if "conversion_tracking_status" in lk or "conversiontrackingstatus" in lk:
            del customer[key]

    cust = customer.get("customer", {})
    for key in list(cust.keys()):
        lk = key.lower()
        if key in ("conversion_tracking_setting", "conversionTrackingSetting"):
            setting = cust[key]
            for sk in list(setting.keys()):
                if "status" in sk.lower():
                    del setting[sk]
        elif "conversion_tracking_status" in lk or "conversiontrackingstatus" in lk:
            del cust[key]


def _drop_default_value(action):
    """Remove all default-value fields so the action has no configured value."""
    for key in list(action.keys()):
        lk = key.lower()
        if ("default" in lk and "value" in lk) or key in ("value_settings", "valueSettings"):
            if key in ("value_settings", "valueSettings"):
                vs = action[key]
                for vk in list(vs.keys()):
                    if "default" in vk.lower():
                        del vs[vk]
            else:
                del action[key]

    ca = action.get("conversion_action", {})
    for key in list(ca.keys()):
        if key in ("value_settings", "valueSettings"):
            vs = ca[key]
            for vk in list(vs.keys()):
                if "default" in vk.lower():
                    del vs[vk]
        elif "default" in key.lower():
            del ca[key]


def _by_id(res):
    return {f["id"]: f for f in res["findings"]}


def _ids(res):
    return {f["id"] for f in res["findings"]}


def _contains_value(obj, target):
    if isinstance(obj, dict):
        return any(_contains_value(v, target) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_value(v, target) for v in obj)
    return obj == target


class TestConversionAudit(unittest.TestCase):
    def _healthy(
        self,
        actions=None,
        action_stats=None,
        campaigns=None,
        customer=None,
    ):
        if actions is None:
            actions = [_mk_action(1)]
        if action_stats is None:
            action_stats = [_mk_action_stat(1, 10)]
        if campaigns is None:
            campaigns = [_mk_campaign(1, "Healthy Campaign", "ENABLED", "MANUAL_CPC", 10, 20)]
        if customer is None:
            customer = _mk_customer()
        return audit(actions, action_stats, campaigns, customer)

    def test_queries_contains_documented_queries(self):
        for name in ("actions", "action_stats", "campaigns", "customer"):
            with self.subTest(name=name):
                self.assertIn(name, QUERIES)
                self.assertIsInstance(QUERIES[name], str)
                self.assertIn("select", QUERIES[name].lower())

    def test_queries_use_placeholders_and_format(self):
        for name in ("action_stats", "campaigns"):
            with self.subTest(name=name):
                self.assertIn("{since}", QUERIES[name])
                self.assertIn("{until}", QUERIES[name])
                formatted = QUERIES[name].format(since="2024-01-01", until="2024-01-31")
                self.assertIn("2024-01-01", formatted)
                self.assertIn("2024-01-31", formatted)

    def test_queries_actions_excludes_removed(self):
        q = QUERIES["actions"].lower()
        self.assertIn("conversion_action", q)
        self.assertIn("removed", q)

    def test_queries_customer_contains_tracking_setting(self):
        q = QUERIES["customer"].lower()
        self.assertIn("auto_tagging_enabled", q)
        self.assertIn("conversion_tracking_setting", q)

    def test_return_shape_and_types(self):
        res = self._healthy()
        for key in ("score", "grade", "findings", "actions", "summary"):
            self.assertIn(key, res)
        self.assertIsInstance(res["score"], int)
        self.assertGreaterEqual(res["score"], 0)
        self.assertLessEqual(res["score"], 100)
        self.assertIn(res["grade"], {"A", "B", "C", "D", "F"})
        self.assertIsInstance(res["findings"], list)
        self.assertIsInstance(res["actions"], list)
        self.assertIsInstance(res["summary"], str)
        self.assertTrue(res["summary"])

    def test_healthy_baseline_scores_100_grade_a_and_no_findings(self):
        res = self._healthy()
        self.assertEqual(res["score"], 100)
        self.assertEqual(res["grade"], "A")
        self.assertEqual(res["findings"], [])

    def test_no_primary_action_when_none_enabled_primary(self):
        cases = [
            ("paused_primary", _mk_action(1, status="PAUSED", primary=True)),
            ("enabled_not_primary", _mk_action(1, status="ENABLED", primary=False)),
        ]
        for label, action in cases:
            with self.subTest(label=label):
                res = self._healthy(actions=[action], action_stats=[_mk_action_stat(1, 10)])
                finding = _by_id(res).get("no_primary_action")
                self.assertIsNotNone(finding)
                self.assertEqual(finding["severity"], "critical")
                self.assertEqual(res["score"], 70)
                self.assertEqual(res["grade"], "C")

    def test_auto_tagging_off(self):
        res = self._healthy(customer=_mk_customer(auto_tagging=False))
        finding = _by_id(res).get("auto_tagging_off")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(res["score"], 85)
        self.assertEqual(res["grade"], "B")

    def test_tracking_not_set_for_status_and_missing(self):
        status_customer = _mk_customer(tracking_status="NOT_CONVERSION_TRACKED")
        missing_customer = _mk_customer()
        _drop_tracking_status(missing_customer)

        for label, customer in (
            ("status", status_customer),
            ("missing", missing_customer),
        ):
            with self.subTest(label=label):
                res = self._healthy(customer=customer)
                finding = _by_id(res).get("tracking_not_set")
                self.assertIsNotNone(finding)
                self.assertEqual(finding["severity"], "critical")
                self.assertEqual(res["score"], 70)
                self.assertEqual(res["grade"], "C")

    def test_dead_action_respects_resource_name_mapping(self):
        action = _mk_action(123)

        cases = [
            ("matching_nonzero", _mk_action_stat(123, 5), False),
            ("mismatched_nonzero", _mk_action_stat(999, 5), True),
            ("matching_zero", _mk_action_stat(123, 0), True),
        ]
        for label, stat, expect_dead in cases:
            with self.subTest(label=label):
                res = self._healthy(actions=[action], action_stats=[stat])
                if expect_dead:
                    finding = _by_id(res).get("dead_action")
                    self.assertIsNotNone(finding)
                    self.assertEqual(finding["severity"], "high")
                    self.assertEqual(res["score"], 85)
                else:
                    self.assertNotIn("dead_action", _ids(res))
                    self.assertEqual(res["score"], 100)

    def test_duplicate_names_same_category_both_primary(self):
        actions = [
            _mk_action(1, name="Lead A", category="LEAD", primary=True, default_value=100),
            _mk_action(2, name="Lead B", category="LEAD", primary=True, default_value=100),
        ]
        stats = [_mk_action_stat(1, 5), _mk_action_stat(2, 5)]
        res = self._healthy(actions=actions, action_stats=stats)

        finding = _by_id(res).get("duplicate_names")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(res["score"], 93)
        self.assertEqual(res["grade"], "A")

    def test_counting_many_per_click_for_lead_categories(self):
        action = _mk_action(
            1,
            category="LEAD",
            counting_type="MANY_PER_CLICK",
            lookback=30,
            default_value=100,
        )
        res = self._healthy(actions=[action], action_stats=[_mk_action_stat(1, 5)])

        finding = _by_id(res).get("counting_many_per_click_for_leads")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(res["score"], 93)

    def test_short_lookback_for_lead_categories(self):
        cases = [(29, True), (30, False)]
        for lookback, expect_finding in cases:
            with self.subTest(lookback=lookback):
                action = _mk_action(
                    1,
                    category="LEAD",
                    counting_type="ONE_PER_CLICK",
                    lookback=lookback,
                    default_value=100,
                )
                res = self._healthy(actions=[action], action_stats=[_mk_action_stat(1, 5)])
                if expect_finding:
                    finding = _by_id(res).get("short_lookback")
                    self.assertIsNotNone(finding)
                    self.assertEqual(finding["severity"], "low")
                    self.assertEqual(res["score"], 97)
                else:
                    self.assertNotIn("short_lookback", _ids(res))
                    self.assertEqual(res["score"], 100)

    def test_no_value_for_zero_and_missing_default(self):
        cases = [("zero", 0), ("missing", 100)]
        for label, default in cases:
            with self.subTest(label=label):
                action = _mk_action(1, category="PURCHASE", default_value=default)
                if label == "missing":
                    _drop_default_value(action)

                res = self._healthy(actions=[action], action_stats=[_mk_action_stat(1, 5)])
                finding = _by_id(res).get("no_value")
                self.assertIsNotNone(finding)
                self.assertEqual(finding["severity"], "low")
                self.assertEqual(res["score"], 97)

    def test_spend_no_conversions_threshold_and_lists_campaign_name(self):
        campaign = _mk_campaign(
            1,
            name="Wasteful Campaign",
            status="ENABLED",
            bidding="MANUAL_CPC",
            cost_units=50,
            conversions=0,
        )
        res = self._healthy(campaigns=[campaign])
        finding = _by_id(res).get("spend_no_conversions")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "high")
        self.assertIn("Wasteful Campaign", finding["detail"])
        self.assertEqual(res["score"], 85)

        below = _mk_campaign(
            1,
            name="Just Below Threshold",
            status="ENABLED",
            bidding="MANUAL_CPC",
            cost_units=49.999999,
            conversions=0,
        )
        res_below = self._healthy(campaigns=[below])
        self.assertNotIn("spend_no_conversions", _ids(res_below))
        self.assertEqual(res_below["score"], 100)

    def test_smart_bidding_without_conversions_threshold(self):
        cases = [(14, True), (15, False)]
        for conversions, expect_finding in cases:
            with self.subTest(conversions=conversions):
                campaign = _mk_campaign(
                    1,
                    name="Smart Campaign",
                    status="ENABLED",
                    bidding="TARGET_CPA",
                    cost_units=1,
                    conversions=conversions,
                )
                res = self._healthy(campaigns=[campaign])
                if expect_finding:
                    finding = _by_id(res).get("smart_bidding_without_conversions")
                    self.assertIsNotNone(finding)
                    self.assertEqual(finding["severity"], "critical")
                    self.assertEqual(res["score"], 70)
                    self.assertEqual(res["grade"], "C")
                else:
                    self.assertNotIn("smart_bidding_without_conversions", _ids(res))
                    self.assertEqual(res["score"], 100)

    def test_enhanced_conversions_leads_off(self):
        action = _mk_action(
            1,
            category="LEAD",
            counting_type="ONE_PER_CLICK",
            lookback=30,
            default_value=100,
        )
        res = self._healthy(
            actions=[action],
            action_stats=[_mk_action_stat(1, 5)],
            customer=_mk_customer(enhanced=False),
        )
        finding = _by_id(res).get("enhanced_conversions_leads_off")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "low")
        self.assertEqual(res["score"], 97)

    def test_score_penalty_aggregation(self):
        res = self._healthy(
            actions=[_mk_action(1, status="PAUSED")],
            customer=_mk_customer(auto_tagging=False),
        )
        ids = _ids(res)
        self.assertIn("no_primary_action", ids)
        self.assertIn("auto_tagging_off", ids)
        self.assertEqual(res["score"], 55)  # 100 - 30 - 15
        self.assertEqual(res["grade"], "D")

    def test_score_floor_zero(self):
        action = _mk_action(1, status="PAUSED")
        customer = _mk_customer(
            auto_tagging=False,
            tracking_status="NOT_CONVERSION_TRACKED",
        )
        campaign = _mk_campaign(
            1,
            name="Bad Campaign",
            status="ENABLED",
            bidding="TARGET_CPA",
            cost_units=50,
            conversions=0,
        )
        res = self._healthy(
            actions=[action],
            action_stats=[_mk_action_stat(1, 0)],
            campaigns=[campaign],
            customer=customer,
        )
        ids = _ids(res)
        for finding_id in (
            "no_primary_action",
            "auto_tagging_off",
            "tracking_not_set",
            "spend_no_conversions",
            "smart_bidding_without_conversions",
        ):
            self.assertIn(finding_id, ids)
        self.assertEqual(res["score"], 0)
        self.assertEqual(res["grade"], "F")

    def test_findings_sorted_by_severity(self):
        actions = [
            _mk_action(
                1,
                name="Lead One",
                category="LEAD",
                lookback=7,
                counting_type="ONE_PER_CLICK",
                default_value=100,
            ),
            _mk_action(
                2,
                name="Lead Two",
                category="LEAD",
                lookback=7,
                counting_type="ONE_PER_CLICK",
                default_value=100,
            ),
        ]
        stats = [_mk_action_stat(1, 5), _mk_action_stat(2, 5)]
        customer = _mk_customer(
            auto_tagging=False,
            tracking_status="NOT_CONVERSION_TRACKED",
        )
        res = self._healthy(actions=actions, action_stats=stats, customer=customer)

        ids = _ids(res)
        for finding_id in (
            "tracking_not_set",
            "auto_tagging_off",
            "duplicate_names",
            "short_lookback",
        ):
            self.assertIn(finding_id, ids)

        ranks = [SEVERITY_RANK[f["severity"]] for f in res["findings"]]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(res["score"], 45)  # 100 - 30 - 15 - 7 - 3
        self.assertEqual(res["grade"], "D")

    def test_finding_entries_have_documented_keys(self):
        res = self._healthy(customer=_mk_customer(auto_tagging=False))
        self.assertTrue(res["findings"])
        for finding in res["findings"]:
            self.assertIsInstance(finding, dict)
            for key in ("id", "severity", "title", "detail", "fix"):
                self.assertIn(key, finding)
            self.assertIn(finding["severity"], SEVERITY_RANK)
            for key in ("id", "title", "detail", "fix"):
                self.assertIsInstance(finding[key], str)

    def test_actions_output_includes_recent_all_conversions(self):
        action = _mk_action(123)
        res = self._healthy(
            actions=[action],
            action_stats=[_mk_action_stat(123, 42)],
        )
        self.assertEqual(len(res["actions"]), 1)
        self.assertTrue(_contains_value(res["actions"][0], 42))


if __name__ == '__main__':
    unittest.main()
