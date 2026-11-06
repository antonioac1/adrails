"""Tests for adsops.local.plan — pure plan builder for local growth plans.

The module is documented as pure (no I/O), so these tests only exercise the
public API: PROFILE_FIELDS, validate_profile, economics, build, progress and
to_markdown.
"""

import json
import unittest

from adsops.local import plan as plan_mod


TOOLS = {
    "adsops_local_profile",
    "adsops_leads",
    "adsops_leads_due",
    "adsops_lsa_leads",
    "adsops_lsa_report",
    "adsops_lsa_dispute",
    "adsops_lsa_readiness",
    "adsops_local_schema",
    "adsops_local_nap",
    "adsops_local_area_page",
    "adsops_reviews",
    "aeo_visibility_record",
    "aeo_page_audit",
    "aeo_schema_generate",
    "aeo_site_audit",
    "aeo_llms_txt",
    "aeo_indexnow",
    "adsops_plan",
    "adsops_apply",
    "adsops_status",
    "adsops_guard",
    "adsops_conv_audit",
    "adsops_lp_diagnose",
    "adsops_negatives_suggest",
    "adsops_offline_conv_plan",
}

HUMAN_OR_AGENT = {"owner", "agent"}
VALID_TOOLS = TOOLS | HUMAN_OR_AGENT


def make_profile(**overrides):
    """A complete, internally consistent profile."""
    p = {
        "business_name": "Acme Plumbing",
        "trade": "plumber",
        "city": "Austin",
        "service_area": ["Austin", "Round Rock", "Cedar Park"],
        "services": ["Leak repair", "Drain cleaning", "Water heaters"],
        "phone": "5125550100",
        "website": "https://acme.example",
        "has_gbp": True,
        "gbp_verified": True,
        "review_count": 12,
        "rating": 4.7,
        "has_lsa": False,
        "lsa_eligible_category": True,
        "licensed_insured": True,
        "monthly_budget": 1500,
        "avg_job_value": 1000,
        "close_rate": 0.5,
        "hours": "Mon-Fri 8-5",
        "goals": "10 leads/month",
        "competitors": ["Bob's Plumbing"],
        "crm_today": "spreadsheet",
    }
    p.update(overrides)
    return p


class ProfileFieldsTests(unittest.TestCase):
    def test_profile_fields_shape(self):
        fields = plan_mod.PROFILE_FIELDS
        self.assertIsInstance(fields, list)
        self.assertGreaterEqual(len(fields), 20)
        for f in fields:
            self.assertIsInstance(f, dict)
            for key in ("key", "question", "required", "example"):
                self.assertIn(key, f)
            self.assertIsInstance(f["key"], str)
            self.assertIsInstance(f["question"], str)
            self.assertIsInstance(f["required"], bool)

    def test_profile_fields_cover_documented_keys(self):
        keys = {f["key"] for f in plan_mod.PROFILE_FIELDS}
        documented = {
            "business_name", "trade", "city", "service_area", "services",
            "phone", "website", "has_gbp", "gbp_verified", "review_count",
            "rating", "has_lsa", "lsa_eligible_category", "licensed_insured",
            "monthly_budget", "avg_job_value", "close_rate", "hours", "goals",
            "competitors", "crm_today",
        }
        self.assertEqual(documented - keys, set())


class ValidateProfileTests(unittest.TestCase):
    def test_empty_profile_is_not_ok(self):
        res = plan_mod.validate_profile({})
        self.assertIn("ok", res)
        self.assertIn("missing", res)
        self.assertIn("warnings", res)
        self.assertFalse(res["ok"])
        self.assertIsInstance(res["missing"], list)
        self.assertTrue(res["missing"])

    def test_valid_profile_passes(self):
        res = plan_mod.validate_profile(make_profile())
        self.assertTrue(res["ok"])
        self.assertEqual(res["missing"], [])

    def test_negative_budget_is_error(self):
        res = plan_mod.validate_profile(make_profile(monthly_budget=-1))
        self.assertFalse(res["ok"])

    def test_rating_out_of_range_flagged(self):
        for bad in (0.5, 6.0):
            res = plan_mod.validate_profile(make_profile(rating=bad))
            # Either a hard error or an explicit warning: never silently ok.
            self.assertFalse(res["ok"] and not res["warnings"])

    def test_close_rate_out_of_range_flagged(self):
        for bad in (-0.1, 1.5):
            res = plan_mod.validate_profile(make_profile(close_rate=bad))
            self.assertFalse(res["ok"] and not res["warnings"])


class EconomicsTests(unittest.TestCase):
    def test_max_cpl_formula(self):
        econ = plan_mod.economics(make_profile(avg_job_value=1000, close_rate=0.5))
        self.assertIsNotNone(econ["max_cpl"])
        self.assertAlmostEqual(econ["max_cpl"], 150.0, places=2)

    def test_budget_supports_leads(self):
        econ = plan_mod.economics(
            make_profile(avg_job_value=1000, close_rate=0.5, monthly_budget=1500)
        )
        self.assertEqual(econ["budget_supports_leads"], 10)

    def test_missing_numbers_yield_none_and_note(self):
        econ = plan_mod.economics({})
        self.assertIn("max_cpl", econ)
        self.assertIsNone(econ["max_cpl"])
        self.assertIsNone(econ["budget_supports_leads"])
        self.assertTrue(econ.get("note"))


class BuildTests(unittest.TestCase):
    def _build(self, **overrides):
        return plan_mod.build(
            make_profile(**overrides),
            start_date="2030-01-01",
            horizon_days=90,
        )

    def test_top_level_keys(self):
        b = self._build()
        for key in ("summary", "stages", "tasks", "weekly_rhythm", "economics", "kpis"):
            self.assertIn(key, b)
        self.assertIsInstance(b["tasks"], list)
        self.assertTrue(b["tasks"])

    def test_task_fields_and_ranges(self):
        b = self._build()
        for t in b["tasks"]:
            for key in (
                "id", "week", "title", "why", "how", "tool", "done_when",
                "priority", "depends_on", "owner_minutes", "est_cost",
            ):
                self.assertIn(key, t)
            self.assertTrue(1 <= t["week"] <= 13)
            self.assertIsInstance(t["depends_on"], list)
            self.assertIsInstance(t["owner_minutes"], int)
            self.assertGreaterEqual(t["owner_minutes"], 0)
            self.assertIsInstance(t["est_cost"], str)

    def test_unique_ids(self):
        b = self._build()
        ids = [t["id"] for t in b["tasks"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_tools_are_valid(self):
        b = self._build()
        for t in b["tasks"]:
            self.assertIn(t["tool"], VALID_TOOLS, msg=f"{t['id']} -> {t['tool']}")

    def test_priority_values(self):
        b = self._build()
        for t in b["tasks"]:
            self.assertIn(t["priority"], {"P0", "P1", "P2"})

    def test_stages_shape(self):
        b = self._build()
        self.assertGreaterEqual(len(b["stages"]), 4)
        for s in b["stages"]:
            self.assertIn("name", s)
            self.assertIn("days", s)
            self.assertIn("why", s)

    def test_weekly_rhythm_references_documented_recurring_tools(self):
        b = self._build()
        self.assertTrue(b["weekly_rhythm"])
        blob = json.dumps(b["weekly_rhythm"], default=str)
        self.assertIn("adsops_leads_due", blob)
        self.assertIn("adsops_lsa_report", blob)
        self.assertIn("adsops_reviews", blob)

    def test_kpis_shape(self):
        b = self._build()
        self.assertTrue(b["kpis"])
        for k in b["kpis"]:
            self.assertIn("name", k)
            self.assertIn("target", k)
            self.assertIn("tool", k)
            self.assertIn(k["tool"], VALID_TOOLS)

    def test_dependency_ids_exist(self):
        b = self._build()
        ids = {t["id"] for t in b["tasks"]}
        for t in b["tasks"]:
            for dep in t["depends_on"]:
                self.assertIn(dep, ids, msg=f"{t['id']} depends on missing {dep}")

    def test_dependency_ordering(self):
        b = self._build()
        by_id = {t["id"]: t for t in b["tasks"]}
        for t in b["tasks"]:
            for dep in t["depends_on"]:
                self.assertLessEqual(
                    by_id[dep]["week"], t["week"],
                    msg=f"{t['id']} (week {t['week']}) precedes dependency {dep}",
                )

    def test_budget_zero_is_organic_only(self):
        b = self._build(monthly_budget=0)
        for t in b["tasks"]:
            self.assertNotIn(t["tool"], {"adsops_plan", "adsops_apply"},
                              msg=f"{t['id']} is an ads task under zero budget")
        self.assertTrue(b["summary"])

    def test_lsa_skipped_when_not_eligible(self):
        b = self._build(lsa_eligible_category=False, licensed_insured=False)
        for t in b["tasks"]:
            self.assertNotEqual(t["tool"], "adsops_lsa_readiness")

    def test_lsa_adds_work_when_eligible_and_licensed(self):
        eligible = self._build(lsa_eligible_category=True, licensed_insured=True)
        not_eligible = self._build(lsa_eligible_category=False, licensed_insured=False)
        self.assertGreater(len(eligible["tasks"]), len(not_eligible["tasks"]))

    def test_gbp_claim_skipped_when_verified(self):
        unverified = self._build(has_gbp=False, gbp_verified=False)
        verified = self._build(has_gbp=True, gbp_verified=True)
        self.assertGreater(len(unverified["tasks"]), len(verified["tasks"]))


class ProgressTests(unittest.TestCase):
    def _plan(self):
        return plan_mod.build(make_profile(), start_date="2030-01-01", horizon_days=90)

    def test_counts_and_pct(self):
        p = self._plan()
        ids = [t["id"] for t in p["tasks"]]
        done = ids[:3]
        res = plan_mod.progress(p, done, "2030-01-02")
        self.assertEqual(res["total"], len(ids))
        self.assertEqual(res["done"], 3)
        self.assertAlmostEqual(
            float(res["pct"]), 3 * 100.0 / len(ids), delta=1.0
        )
        self.assertIn("overdue", res)
        self.assertIn("next", res)

    def test_all_done(self):
        p = self._plan()
        ids = [t["id"] for t in p["tasks"]]
        res = plan_mod.progress(p, ids, "2030-06-01")
        self.assertEqual(res["done"], len(ids))
        self.assertEqual(res["overdue"], [])
        self.assertEqual(res["next"], [])
        self.assertAlmostEqual(float(res["pct"]), 100.0, places=0)

    def test_overdue_when_weeks_have_passed(self):
        # Every week of a 90-day plan starting in 2020 has long ended.
        p = plan_mod.build(make_profile(), start_date="2020-01-01", horizon_days=90)
        res = plan_mod.progress(p, [], "2030-01-01")
        self.assertEqual(len(res["overdue"]), len(p["tasks"]))

    def test_next_constraints(self):
        p = plan_mod.build(make_profile(), start_date="2020-01-01", horizon_days=90)
        done = [p["tasks"][0]["id"]]
        res = plan_mod.progress(p, done, "2030-01-01")
        self.assertLessEqual(len(res["next"]), 5)
        done_set = set(done)
        for t in res["next"]:
            self.assertNotIn(t["id"], done_set)
            for dep in t["depends_on"]:
                self.assertIn(dep, done_set)


class MarkdownTests(unittest.TestCase):
    def test_markdown_contains_every_id(self):
        p = plan_mod.build(make_profile(), start_date="2030-01-01", horizon_days=90)
        md = plan_mod.to_markdown(p)
        self.assertIsInstance(md, str)
        for t in p["tasks"]:
            self.assertIn(t["id"], md)

    def test_markdown_is_a_checkbox_list(self):
        p = plan_mod.build(make_profile(), start_date="2030-01-01", horizon_days=90)
        md = plan_mod.to_markdown(p)
        self.assertIn("- [ ]", md)
        self.assertIn("T01", md)


if __name__ == "__main__":
    unittest.main()
