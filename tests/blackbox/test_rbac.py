import unittest
from unittest import mock

from adsops.rbac import Rbac, wrap


def rbac_cfg(principal, roles, enabled=True, roles_override=None, principals=None):
    if principals is None:
        principals = {principal: roles}
    cfg = {
        "enabled": enabled,
        "principal": principal,
        "principals": principals,
    }
    if roles_override is not None:
        cfg["roles"] = roles_override
    return cfg


class RbacTests(unittest.TestCase):
    def assertRaisesNamed(self, name, func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - assert exception type name
            self.assertEqual(type(exc).__name__, name)
            return exc
        else:
            self.fail(f"{name} was not raised")

    # --- disabled / missing config -------------------------------------

    def test_disabled_config_allows_everything(self):
        cfg = rbac_cfg("viewer-user", ["viewer"], enabled=False)
        rbac = Rbac(cfg, "someone-else")
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertTrue(rbac.allowed("adsops_apply"))
        self.assertTrue(rbac.allowed("totally_unknown_tool"))

    def test_missing_config_none_allows_everything(self):
        rbac = Rbac(None, "anyone")
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertTrue(rbac.allowed("adsops_apply"))
        self.assertTrue(rbac.allowed("totally_unknown_tool"))

    def test_empty_config_allows_everything(self):
        rbac = Rbac({}, "anyone")
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertTrue(rbac.allowed("adsops_apply"))
        self.assertTrue(rbac.allowed("totally_unknown_tool"))

    # --- enabled / unknown principal -----------------------------------

    def test_enabled_unknown_principal_denies_everything(self):
        cfg = rbac_cfg("known", ["admin"])
        rbac = Rbac(cfg, "stranger")
        self.assertFalse(rbac.allowed("adsops_status"))
        self.assertFalse(rbac.allowed("adsops_apply"))
        self.assertFalse(rbac.allowed("anything_at_all"))
        self.assertRaisesNamed("RailViolation", rbac.check, "adsops_status")

    # --- viewer role ---------------------------------------------------

    def test_viewer_exact_tools_allowed(self):
        rbac = Rbac(rbac_cfg("v", ["viewer"]), "v")
        for tool in [
            "adsops_campaigns",
            "adsops_status",
            "adsops_terms",
            "adsops_usage",
            "adsops_history",
            "adsops_competitors_report",
            "adsops_lp_diagnose",
            "adsops_negatives_suggest",
            "adsops_monitor_check",
            "adsops_brief",
            "adsops_fleet",
            "adsops_rsa_validate",
            "adsops_keyword_ideas",
            "adsops_rules_check",
            "adsops_realloc_suggest",
            "adsops_drift",
            "adsops_conv_audit",
        ]:
            with self.subTest(tool=tool):
                self.assertTrue(rbac.allowed(tool))

    def test_viewer_glob_patterns_allowed(self):
        rbac = Rbac(rbac_cfg("v", ["viewer"]), "v")
        for tool in [
            "aeo_site_audit",
            "adsops_weekly_report",
            "adsops_foo_diagnos_bar",
            "adsops_audit_log",
        ]:
            with self.subTest(tool=tool):
                self.assertTrue(rbac.allowed(tool))

    def test_viewer_denied_operator_and_admin_tools(self):
        rbac = Rbac(rbac_cfg("v", ["viewer"]), "v")
        for tool in [
            "adsops_plan",
            "adsops_apply",
            "adsops_set",
            "adsops_rollback",
            "adsops_approval_request",
            "adsops_offline_conv_plan",
            "adsops_approval_decide",
            "adsops_guard",
            "totally_unknown_tool",
        ]:
            with self.subTest(tool=tool):
                self.assertFalse(rbac.allowed(tool))

    # --- operator role -------------------------------------------------

    def test_operator_role_inherits_viewer_and_allows_operator_tools(self):
        rbac = Rbac(rbac_cfg("op", ["operator"]), "op")
        self.assertTrue(rbac.allowed("adsops_status"))
        for tool in [
            "adsops_plan",
            "adsops_apply",
            "adsops_set",
            "adsops_rollback",
            "adsops_approval_request",
            "adsops_offline_conv_plan",
        ]:
            with self.subTest(tool=tool):
                self.assertTrue(rbac.allowed(tool))

    def test_operator_cannot_decide_approvals(self):
        rbac = Rbac(rbac_cfg("op", ["operator"]), "op")
        self.assertFalse(rbac.allowed("adsops_approval_decide"))

    # --- approver role -------------------------------------------------

    def test_approver_role_inherits_viewer_and_can_decide(self):
        rbac = Rbac(rbac_cfg("apr", ["approver"]), "apr")
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertTrue(rbac.allowed("adsops_campaigns"))
        self.assertTrue(rbac.allowed("adsops_approval_decide"))

    def test_approver_cannot_apply(self):
        rbac = Rbac(rbac_cfg("apr", ["approver"]), "apr")
        self.assertFalse(rbac.allowed("adsops_apply"))

    # --- guard role ----------------------------------------------------

    def test_guard_role(self):
        rbac = Rbac(rbac_cfg("guard", ["guard"]), "guard")
        self.assertTrue(rbac.allowed("adsops_guard"))
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertFalse(rbac.allowed("adsops_apply"))
        self.assertFalse(rbac.allowed("adsops_approval_decide"))

    # --- admin role ----------------------------------------------------

    def test_admin_allows_any_tool(self):
        rbac = Rbac(rbac_cfg("boss", ["admin"]), "boss")
        self.assertTrue(rbac.allowed("adsops_apply"))
        self.assertTrue(rbac.allowed("adsops_not_a_real_tool_xyz"))
        self.assertTrue(rbac.allowed("aeo_anything"))

    # --- allowed / check -----------------------------------------------

    def test_allowed_returns_bool_and_check_allows_without_raising(self):
        rbac = Rbac(rbac_cfg("v", ["viewer"]), "v")
        self.assertIs(rbac.allowed("adsops_status"), True)
        self.assertIs(rbac.allowed("adsops_apply"), False)
        rbac.check("adsops_status")  # should not raise

    def test_check_raises_rail_violation_with_context(self):
        rbac = Rbac(rbac_cfg("grokbot", ["viewer"]), "grokbot")
        exc = self.assertRaisesNamed("RailViolation", rbac.check, "adsops_apply")
        msg = str(exc)
        self.assertIn("principal 'grokbot'", msg)
        self.assertIn("roles: viewer", msg)
        self.assertIn("may not call adsops_apply", msg)

    # --- describe ------------------------------------------------------

    def test_describe_viewer(self):
        d = Rbac(rbac_cfg("v", ["viewer"]), "v").describe()
        self.assertEqual(
            set(d.keys()),
            {"enabled", "principal", "roles", "allowed_patterns"},
        )
        self.assertTrue(d["enabled"])
        self.assertEqual(d["principal"], "v")
        self.assertEqual(set(d["roles"]), {"viewer"})
        self.assertIn("adsops_status", d["allowed_patterns"])

    def test_describe_admin_includes_wildcard(self):
        d = Rbac(rbac_cfg("boss", ["admin"]), "boss").describe()
        self.assertTrue(d["enabled"])
        self.assertIn("*", d["allowed_patterns"])

    def test_describe_disabled(self):
        d = Rbac(rbac_cfg("v", ["viewer"], enabled=False), "v").describe()
        self.assertEqual(
            set(d.keys()),
            {"enabled", "principal", "roles", "allowed_patterns"},
        )
        self.assertFalse(d["enabled"])

    # --- validation / overrides ----------------------------------------

    def test_unknown_role_raises_tool_error_at_construction(self):
        cfg = rbac_cfg("u", ["no_such_role"])
        self.assertRaisesNamed("ToolError", Rbac, cfg, "u")

    def test_custom_role_from_roles_override(self):
        cfg = rbac_cfg(
            "deployer",
            ["deployer"],
            roles_override={"deployer": ["deploy_*"]},
        )
        rbac = Rbac(cfg, "deployer")
        self.assertTrue(rbac.allowed("deploy_now"))
        self.assertFalse(rbac.allowed("adsops_status"))

    def test_principal_argument_overrides_config_principal(self):
        cfg = rbac_cfg(
            "lane-a",
            ["admin"],
            principals={"lane-a": ["admin"], "grokbot": ["viewer"]},
        )
        rbac = Rbac(cfg, "grokbot")
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertFalse(rbac.allowed("adsops_not_a_real_tool_xyz"))

    def test_principal_from_config_used_when_argument_none(self):
        cfg = rbac_cfg("grokbot", ["viewer"])
        rbac = Rbac(cfg, None)
        self.assertTrue(rbac.allowed("adsops_status"))
        self.assertFalse(rbac.allowed("adsops_apply"))

    # --- wrap ----------------------------------------------------------

    def test_wrap_allowed_calls_function_and_returns(self):
        rbac = Rbac(rbac_cfg("v", ["viewer"]), "v")
        fn = mock.Mock(return_value="ok")
        wrapped = wrap(fn, "adsops_status", rbac)
        self.assertEqual(wrapped({"a": 1}), "ok")
        fn.assert_called_once_with({"a": 1})

    def test_wrap_denied_does_not_call_function(self):
        rbac = Rbac(rbac_cfg("v", ["viewer"]), "v")
        fn = mock.Mock()
        wrapped = wrap(fn, "adsops_apply", rbac)
        self.assertRaisesNamed("RailViolation", wrapped, {"a": 1})
        fn.assert_not_called()

    def test_wrap_calls_check_before_function_with_mock(self):
        fn = mock.Mock(return_value=123)
        rbac = mock.Mock()
        wrapped = wrap(fn, "tool_x", rbac)
        self.assertEqual(wrapped("args"), 123)
        rbac.check.assert_called_once_with("tool_x")
        fn.assert_called_once_with("args")


if __name__ == '__main__':
    unittest.main()
