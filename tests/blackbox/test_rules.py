import json
import os
import tempfile
import unittest

import adsops.rules as rules


def make_rule(rule_id="r1", when=None, then=None, scope="campaign",
              cooldown_hours=0, enabled=True):
    if when is None:
        when = {"metric": "cost", "op": ">", "value": 0}
    if then is None:
        then = {"pause": True}
    return {
        "id": rule_id,
        "when": when,
        "then": then,
        "scope": scope,
        "cooldown_hours": cooldown_hours,
        "enabled": enabled,
    }


def make_entity(entity_id="e1", scope="campaign", name="Entity", metrics=None):
    if metrics is None:
        metrics = {30: {"cost": 10, "clicks": 5, "impressions": 10, "conversions": 2}}
    return {
        "scope": scope,
        "id": entity_id,
        "name": name,
        "metrics": metrics,
    }


def comp(metric, op, value, window_days=None):
    c = {"metric": metric, "op": op, "value": value}
    if window_days is not None:
        c["window_days"] = window_days
    return c


class TestValidateRules(unittest.TestCase):
    def test_valid_minimal_rule(self):
        errors = rules.validate_rules([make_rule(cooldown_hours=24)])
        self.assertEqual(errors, [])

    def test_unknown_metric(self):
        r = make_rule(when={"metric": "bogus", "op": ">", "value": 1})
        errors = rules.validate_rules([r])
        self.assertTrue(errors)
        self.assertTrue(all(isinstance(e, str) for e in errors))

    def test_unknown_op(self):
        r = make_rule(when={"metric": "cost", "op": "~=", "value": 1})
        errors = rules.validate_rules([r])
        self.assertTrue(errors)

    def test_unknown_action(self):
        r = make_rule(then={"frobnicate": True})
        errors = rules.validate_rules([r])
        self.assertTrue(errors)

    def test_scope_action_mismatch(self):
        r1 = make_rule(
            rule_id="r1",
            scope="campaign",
            then={"add_negative": {"match_type": "PHRASE"}},
        )
        r2 = make_rule(rule_id="r2", scope="keyword", then={"budget_pct": 10})
        errors = rules.validate_rules([r1, r2])
        self.assertTrue(errors)

    def test_bad_window(self):
        r1 = make_rule(rule_id="r1", when=comp("cost", ">", 1, window_days=0))
        r2 = make_rule(rule_id="r2", when=comp("cost", ">", 1, window_days=91))
        errors = rules.validate_rules([r1, r2])
        self.assertTrue(errors)
        self.assertGreaterEqual(len(errors), 1)

    def test_duplicate_ids(self):
        r1 = make_rule(rule_id="dup")
        r2 = make_rule(rule_id="dup", then={"alert": "x"})
        errors = rules.validate_rules([r1, r2])
        self.assertTrue(errors)

    def test_depth_limit(self):
        cond = comp("cost", ">", 1)
        for _ in range(8):
            cond = {"all": [cond]}
        errors = rules.validate_rules([make_rule(when=cond)])
        self.assertTrue(errors)

    def test_rejects_enable_action(self):
        r1 = make_rule(rule_id="r1", then={"pause": False})
        r2 = make_rule(rule_id="r2", then={"enable": True})
        errors = rules.validate_rules([r1, r2])
        self.assertTrue(errors)


class TestEvaluate(unittest.TestCase):
    def test_all_returns_keys(self):
        r = make_rule(when={"all": [comp("cost", ">", 5)]}, then={"pause": True})
        e = make_entity(metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})

        self.assertIsInstance(out, dict)
        for key in ("proposals", "alerts", "skipped_cooldown"):
            self.assertIn(key, out)
            self.assertIsInstance(out[key], list)

        self.assertEqual(len(out["proposals"]), 1)
        p = out["proposals"][0]
        for key in ("rule", "entity", "action", "reason", "patch"):
            self.assertIn(key, p)
        self.assertEqual(p["rule"], "r1")
        self.assertIn("e1", str(p["entity"]))
        self.assertIsInstance(p["reason"], str)
        self.assertTrue(p["reason"])
        self.assertIsInstance(p["patch"], dict)

    def test_any_condition(self):
        r = make_rule(when={"any": [comp("cost", ">", 100), comp("cost", ">", 5)]})
        e = make_entity(metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 1)

        e2 = make_entity(metrics={30: {"cost": 1}})
        out2 = rules.evaluate([r], [e2], {})
        self.assertEqual(len(out2["proposals"]), 0)

    def test_not_condition(self):
        r = make_rule(when={"not": comp("cost", ">", 100)})
        e = make_entity(metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 1)

        e2 = make_entity(metrics={30: {"cost": 200}})
        out2 = rules.evaluate([r], [e2], {})
        self.assertEqual(len(out2["proposals"]), 0)

    def test_derived_metrics(self):
        cond = {
            "all": [
                comp("ctr", ">", 0.5),
                comp("cpc", "<", 3),
                comp("cpa", "==", 4),
                comp("conv_rate", ">=", 0.4),
            ]
        }
        r = make_rule(when=cond)
        e = make_entity(
            metrics={
                30: {
                    "cost": 12,
                    "clicks": 6,
                    "impressions": 10,
                    "conversions": 3,
                }
            }
        )
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 1, out)

    def test_none_comparison_is_false(self):
        r = make_rule(when=comp("cpa", "<", 100))
        e = make_entity(metrics={30: {"cost": 10, "clicks": 5, "conversions": 0}})
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 0)

        r2 = make_rule(rule_id="r2", when=comp("cpa", "!=", 0))
        out2 = rules.evaluate([r2], [e], {})
        self.assertEqual(len(out2["proposals"]), 0)

    def test_window_default_30_and_string_keys(self):
        r = make_rule(when=comp("cost", ">", 5))
        e_int = make_entity(entity_id="e_int", metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e_int], {})
        self.assertEqual(len(out["proposals"]), 1)

        e_str = make_entity(entity_id="e_str", metrics={"30": {"cost": 10}})
        out2 = rules.evaluate([r], [e_str], {})
        self.assertEqual(len(out2["proposals"]), 1)

    def test_scope_matching(self):
        r = make_rule(scope="campaign")
        e_kw = make_entity(entity_id="kw1", scope="keyword", metrics={30: {"cost": 10}})
        e_cmp = make_entity(entity_id="cmp1", scope="campaign", metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e_kw, e_cmp], {})
        self.assertEqual(len(out["proposals"]), 1)
        self.assertIn("cmp1", str(out["proposals"][0]["entity"]))

    def test_alert_action(self):
        r = make_rule(then={"alert": "notify-me"})
        e = make_entity(metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 0)
        self.assertEqual(len(out["alerts"]), 1)
        self.assertIn("notify-me", str(out["alerts"]))

    def test_pause_patch(self):
        r = make_rule(then={"pause": True})
        e = make_entity(metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 1)
        self.assertIs(out["proposals"][0]["patch"].get("pause"), True)

    def test_add_negative_patch(self):
        r = make_rule(scope="search_term", then={"add_negative": {"match_type": "EXACT"}})
        e = make_entity(
            entity_id="cheap shoes",
            scope="search_term",
            name="cheap shoes",
            metrics={30: {"cost": 10}},
        )
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 1)
        patch = out["proposals"][0]["patch"]
        self.assertIn("add_negative", patch)
        self.assertEqual(patch["add_negative"]["match_type"], "EXACT")
        self.assertIn("cheap shoes", str(patch))

    def test_cooldown(self):
        r = make_rule(cooldown_hours=24)
        e = make_entity(metrics={30: {"cost": 10}})
        last_fired = {"r1|e1": "2024-01-01T00:00:00"}

        out = rules.evaluate(
            [r], [e], {}, last_fired=last_fired, now="2024-01-01T12:00:00"
        )
        self.assertEqual(len(out["proposals"]), 0)
        self.assertEqual(len(out["skipped_cooldown"]), 1)

        out2 = rules.evaluate(
            [r], [e], {}, last_fired=last_fired, now="2024-01-03T00:00:00"
        )
        self.assertEqual(len(out2["proposals"]), 1)

        r0 = make_rule(rule_id="r0", cooldown_hours=0)
        out3 = rules.evaluate(
            [r0],
            [e],
            {},
            last_fired={"r0|e1": "2024-01-01T00:00:00"},
            now="2024-01-01T12:00:00",
        )
        self.assertEqual(len(out3["proposals"]), 1)

    def test_budget_clamp_and_state_key(self):
        r = make_rule(then={"budget_pct": 50})
        e = make_entity(metrics={30: {"cost": 10}})
        e["daily_budget"] = 100.0
        policy = {
            "max_budget_step_pct": 10,
            "min_daily_budget": 1,
            "max_daily_budget": 1000,
        }

        out = rules.evaluate([r], [e], policy)
        self.assertEqual(len(out["proposals"]), 1)
        self.assertEqual(out["proposals"][0]["patch"]["budget_pct"], 10)

        r_neg = make_rule(rule_id="r_neg", then={"budget_pct": -50})
        out_neg = rules.evaluate([r_neg], [e], policy)
        self.assertEqual(out_neg["proposals"][0]["patch"]["budget_pct"], -10)

        r_def = make_rule(rule_id="r_def", then={"budget_pct": 50})
        out_def = rules.evaluate([r_def], [e], {})
        self.assertEqual(out_def["proposals"][0]["patch"]["budget_pct"], 20)

        state = rules.to_state_patch(out["proposals"])
        self.assertIn("daily_budget_usd", state)
        self.assertIsInstance(state["daily_budget_usd"], (int, float))

    def test_ignores_disabled_rule(self):
        r = make_rule(enabled=False)
        e = make_entity(metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})
        self.assertEqual(len(out["proposals"]), 0)


class TestToStatePatch(unittest.TestCase):
    def test_negatives_and_pause(self):
        r_neg = make_rule(
            rule_id="neg",
            scope="search_term",
            then={"add_negative": {"match_type": "PHRASE"}},
        )
        r_pause = make_rule(rule_id="pause", scope="campaign", then={"pause": True})
        e_neg = make_entity(
            entity_id="bad term",
            scope="search_term",
            name="bad term",
            metrics={30: {"cost": 10}},
        )
        e_cmp = make_entity(entity_id="cmp1", scope="campaign", metrics={30: {"cost": 10}})

        out = rules.evaluate([r_neg, r_pause], [e_neg, e_cmp], {})
        self.assertEqual(len(out["proposals"]), 2)

        state = rules.to_state_patch(out["proposals"])
        self.assertIn("negatives_add", state)
        self.assertIsInstance(state["negatives_add"], list)
        self.assertEqual(len(state["negatives_add"]), 1)

        item = state["negatives_add"][0]
        self.assertIn("text", item)
        self.assertIn("match_type", item)
        self.assertEqual(item["match_type"], "PHRASE")
        self.assertEqual(item["text"], "bad term")
        self.assertEqual(state.get("status"), "PAUSED")

    def test_keyword_pauses(self):
        r = make_rule(rule_id="kp", scope="keyword", then={"pause": True})
        e = make_entity(entity_id="kw1", scope="keyword", metrics={30: {"cost": 10}})
        out = rules.evaluate([r], [e], {})

        state = rules.to_state_patch(out["proposals"])
        self.assertIn("keyword_pauses", state)
        self.assertIn("kw1", state["keyword_pauses"])
        self.assertNotEqual(state.get("status"), "PAUSED")


class TestTempfile(unittest.TestCase):
    def test_validate_rules_from_tempfile(self):
        rule_data = [make_rule(rule_id="from_file", cooldown_hours=24)]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"rules": rule_data}, f)
            path = f.name

        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            errors = rules.validate_rules(loaded["rules"])
            self.assertEqual(errors, [])
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()