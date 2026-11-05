"""Unit tests for adsops.budget_realloc (spec-driven, black box).

Only documented public API is exercised:
  - recommend(campaigns, policy, objective="conversions", max_step_pct=20)
  - as_state_patches(result)
"""

import unittest

from adsops.budget_realloc import recommend, as_state_patches

EPS = 0.011  # one cent + a little float slack


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _campaign(key, budget, cost, conv, lost_budget=0.0, lost_rank=0.0,
              status="enabled"):
    return {
        "key": key,
        "daily_budget": budget,
        "cost_30d": cost,
        "conv_30d": conv,
        "lost_budget_is": lost_budget,
        "lost_rank_is": lost_rank,
        "status": status,
    }


def _policy(campaigns, caps=None, total_cap=None, step=20):
    caps = caps or {}
    pol = {
        "campaigns": {},
        "total_daily_cap": total_cap,
        "max_budget_step_pct": step,
    }
    for c in campaigns:
        k = c["key"]
        pol["campaigns"][k] = caps.get(
            k, {"min_daily_budget": 1.0, "max_daily_budget": 100000.0}
        )
    return pol


def _run(campaigns, caps=None, total_cap=None, step=20, **kwargs):
    return recommend(campaigns, _policy(campaigns, caps, total_cap, step),
                     **kwargs)


def _moves_for(result, key):
    return [m for m in result["moves"] if m["key"] == key]


def _base_pair():
    """One clear receiver (high score, lost budget) and one clear donor."""
    receiver = _campaign("recv", 100.0, 500.0, 50.0, lost_budget=1.0)  # score .1
    donor = _campaign("donor", 100.0, 500.0, 5.0)                      # score .01
    return [receiver, donor]


# --------------------------------------------------------------------------- #
# contract / shape
# --------------------------------------------------------------------------- #
class TestReturnContract(unittest.TestCase):

    def test_result_has_documented_keys(self):
        result = _run(_base_pair())
        for key in ("moves", "total_before", "total_after",
                    "expected_extra_conv", "notes"):
            self.assertIn(key, result)
        self.assertIsInstance(result["moves"], list)
        self.assertIsInstance(result["notes"], list)
        self.assertIsInstance(result["total_before"], (int, float))
        self.assertIsInstance(result["total_after"], (int, float))
        self.assertIsInstance(result["expected_extra_conv"], (int, float))

    def test_move_entries_have_documented_keys(self):
        result = _run(_base_pair())
        self.assertTrue(result["moves"], "expected at least one move")
        for move in result["moves"]:
            for key in ("key", "from", "to", "delta", "reason"):
                self.assertIn(key, move)
            self.assertIsInstance(move["key"], str)

    def test_empty_campaigns_no_moves_with_note(self):
        result = recommend([], {"campaigns": {}, "total_daily_cap": None,
                                "max_budget_step_pct": 20})
        self.assertEqual(result["moves"], [])
        self.assertTrue(result["notes"], "spec requires a note when nothing moves")

    def test_single_campaign_no_moves_with_note(self):
        camps = [_campaign("only", 100.0, 500.0, 50.0, lost_budget=1.0)]
        result = _run(camps)
        self.assertEqual(result["moves"], [])
        self.assertTrue(result["notes"])

    def test_expected_extra_conv_zero_when_no_moves(self):
        camps = [_campaign("only", 100.0, 500.0, 50.0, lost_budget=1.0)]
        result = _run(camps)
        self.assertAlmostEqual(float(result["expected_extra_conv"]), 0.0,
                               places=6)


# --------------------------------------------------------------------------- #
# core transfer behaviour
# --------------------------------------------------------------------------- #
class TestTransfers(unittest.TestCase):

    def test_donor_down_receiver_up(self):
        result = _run(_base_pair())
        donor_moves = _moves_for(result, "donor")
        recv_moves = _moves_for(result, "recv")
        self.assertTrue(donor_moves)
        self.assertTrue(recv_moves)
        self.assertLess(sum(m["delta"] for m in donor_moves), 0)
        self.assertGreater(sum(m["delta"] for m in recv_moves), 0)

    def test_total_budget_stays_equal(self):
        result = _run(_base_pair())
        self.assertAlmostEqual(result["total_before"], result["total_after"],
                               places=2)

    def test_move_delta_consistent_with_from_to(self):
        result = _run(_base_pair())
        for move in result["moves"]:
            self.assertAlmostEqual(move["to"] - move["from"], move["delta"],
                                   places=2)

    def test_deltas_net_to_zero(self):
        result = _run(_base_pair())
        self.assertAlmostEqual(sum(m["delta"] for m in result["moves"]), 0.0,
                               places=2)

    def test_rounding_to_cents(self):
        result = _run(_base_pair())
        for move in result["moves"]:
            for field in ("from", "to", "delta"):
                value = move[field]
                self.assertAlmostEqual(value, round(value, 2), places=6,
                                       msg="%s not rounded to cents" % field)

    def test_rounding_drift_fixed(self):
        # fractional budgets force a rounding drift that must be corrected
        camps = [
            _campaign("r1", 10.01, 100.0, 10.0, lost_budget=1.0),
            _campaign("r2", 10.01, 100.0, 10.0, lost_budget=1.0),
            _campaign("d1", 20.02, 100.0, 1.0),
            _campaign("d2", 20.02, 100.0, 1.0),
        ]
        result = _run(camps)
        self.assertAlmostEqual(sum(m["delta"] for m in result["moves"]), 0.0,
                               places=2)
        self.assertAlmostEqual(result["total_before"], result["total_after"],
                               places=2)

    def test_total_daily_cap_respected(self):
        result = _run(_base_pair(), total_cap=200.0)
        self.assertLessEqual(result["total_after"], 200.0 + EPS)

    def test_expected_extra_conv_non_negative(self):
        for camps, caps in (
            (_base_pair(), None),
            ([_campaign("a", 50.0, 500.0, 50.0, lost_budget=1.0),
              _campaign("b", 50.0, 2000.0, 2.0)], None),
        ):
            result = _run(camps, caps=caps)
            self.assertGreaterEqual(float(result["expected_extra_conv"]), 0.0)


# --------------------------------------------------------------------------- #
# policy caps
# --------------------------------------------------------------------------- #
class TestCaps(unittest.TestCase):

    def test_donor_never_below_min(self):
        camps = [
            _campaign("recv", 100.0, 500.0, 50.0, lost_budget=1.0),
            _campaign("donor", 10.0, 500.0, 5.0),
        ]
        caps = {"donor": {"min_daily_budget": 10.0,
                          "max_daily_budget": 1000.0}}
        result = _run(camps, caps=caps)
        for move in _moves_for(result, "donor"):
            self.assertGreaterEqual(move["to"], 10.0 - EPS)

    def test_receiver_never_above_max(self):
        camps = [
            _campaign("recv", 100.0, 500.0, 50.0, lost_budget=1.0),
            _campaign("donor", 100.0, 500.0, 5.0),
        ]
        caps = {"recv": {"min_daily_budget": 1.0, "max_daily_budget": 103.0}}
        result = _run(camps, caps=caps)
        for move in _moves_for(result, "recv"):
            self.assertLessEqual(move["to"], 103.0 + EPS)

    def test_step_pct_limits_donor_decrease(self):
        result = _run(_base_pair(), max_step_pct=20)
        for move in _moves_for(result, "donor"):
            self.assertGreaterEqual(move["delta"], -100.0 * 0.20 - EPS)

    def test_step_pct_limits_receiver_increase(self):
        result = _run(_base_pair(), max_step_pct=20)
        for move in _moves_for(result, "recv"):
            self.assertLessEqual(move["delta"], 100.0 * 0.20 + EPS)

    def test_policy_step_pct_is_honoured(self):
        result = _run(_base_pair(), step=5, max_step_pct=20)
        for move in _moves_for(result, "donor"):
            self.assertGreaterEqual(move["delta"], -100.0 * 0.05 - EPS)
        for move in _moves_for(result, "recv"):
            self.assertLessEqual(move["delta"], 100.0 * 0.05 + EPS)


# --------------------------------------------------------------------------- #
# eligibility rules
# --------------------------------------------------------------------------- #
class TestEligibility(unittest.TestCase):

    def test_disabled_campaign_not_in_moves(self):
        camps = _base_pair() + [
            _campaign("paused", 100.0, 500.0, 50.0, lost_budget=1.0,
                      status="paused"),
        ]
        result = _run(camps)
        self.assertEqual(_moves_for(result, "paused"), [])

    def test_rank_above_budget_never_increased(self):
        camps = [
            _campaign("hot", 100.0, 500.0, 50.0, lost_budget=0.5,
                      lost_rank=1.0),
            _campaign("donor", 100.0, 500.0, 5.0),
        ]
        result = _run(camps)
        for move in _moves_for(result, "hot"):
            self.assertLessEqual(move["delta"], 1e-9)

    def test_no_lost_budget_signal_means_no_increase(self):
        camps = [
            _campaign("h", 100.0, 500.0, 50.0, lost_budget=0.0),
            _campaign("donor", 100.0, 500.0, 5.0),
        ]
        result = _run(camps)
        for move in _moves_for(result, "h"):
            self.assertLessEqual(move["delta"], 1e-9)

    def test_zero_conv_high_cost_campaign_is_donor(self):
        camps = [
            _campaign("k1", 100.0, 100.0, 10.0, lost_budget=1.0),  # score .1
            _campaign("k2", 100.0, 200.0, 10.0),                   # score .05
            _campaign("k0", 100.0, 900.0, 0.0),                    # zero conv
        ]
        result = _run(camps)
        k0_moves = _moves_for(result, "k0")
        self.assertTrue(k0_moves, "zero-conv, very expensive campaign should donate")
        self.assertLess(sum(m["delta"] for m in k0_moves), 0)

    def test_total_preserved_with_mixed_campaigns(self):
        camps = [
            _campaign("k1", 100.0, 100.0, 10.0, lost_budget=1.0),
            _campaign("k2", 100.0, 200.0, 10.0),
            _campaign("k0", 100.0, 900.0, 0.0),
            _campaign("off", 100.0, 900.0, 0.0, status="paused"),
        ]
        result = _run(camps)
        self.assertAlmostEqual(result["total_before"], result["total_after"],
                               places=2)


# --------------------------------------------------------------------------- #
# as_state_patches
# --------------------------------------------------------------------------- #
class TestAsStatePatches(unittest.TestCase):

    def test_patches_keys_match_moves(self):
        result = _run(_base_pair())
        patches = as_state_patches(result)
        self.assertEqual(set(patches), {m["key"] for m in result["moves"]})

    def test_patches_value_is_target_budget(self):
        result = _run(_base_pair())
        patches = as_state_patches(result)
        for move in result["moves"]:
            patch = patches[move["key"]]
            self.assertIn("daily_budget_usd", patch)
            self.assertAlmostEqual(patch["daily_budget_usd"], move["to"],
                                   places=2)

    def test_patches_empty_when_no_moves(self):
        result = recommend([], {"campaigns": {}, "total_daily_cap": None,
                                "max_budget_step_pct": 20})
        self.assertEqual(as_state_patches(result), {})


if __name__ == '__main__':
    unittest.main()
