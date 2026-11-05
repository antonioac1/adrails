import json
import unittest
from unittest import mock

from adsops import monitors


TOOL_ERROR = getattr(monitors, "ToolError", None)


def make_campaign(**overrides):
    campaign = {
        "key": "c1",
        "name": "Camp 1",
        "status": "ENABLED",
        "daily_budget": 100.0,
        "spend_today": 10.0,
        "spend_month": 1000.0,
        "month_budget": 5000.0,
        "clicks_today": 5,
        "conv_today": 1,
        "cpa_7d": 10.0,
        "cpa_30d": 10.0,
        "impressions_today": 100,
        "disapproved_ads": 0,
        "lost_budget_is": 0.0,
        "hour": 12,
        "days_since_conv": 0,
    }
    campaign.update(overrides)
    return campaign


def make_snapshot(campaigns=None, date="2024-06-15"):
    if campaigns is None:
        campaigns = [make_campaign()]
    return {"date": date, "campaigns": campaigns}


def make_monitor(**overrides):
    monitor = {
        "id": "m1",
        "type": "cpa_spike",
        "threshold": 0.5,
        "enabled": True,
        "severity": "warn",
    }
    monitor.update(overrides)
    return monitor


def make_alert(monitor="m1", campaign="c1", severity="warn",
               title="Alert title", detail="Alert detail"):
    return {
        "monitor": monitor,
        "campaign": campaign,
        "severity": severity,
        "title": title,
        "detail": detail,
        "value": 1,
        "threshold": 0.5,
    }


class MonitorsTests(unittest.TestCase):
    def assert_tool_error(self, fn):
        if TOOL_ERROR is not None:
            with self.assertRaises(TOOL_ERROR):
                fn()
        else:
            with self.assertRaises(Exception):
                fn()

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------
    def test_validate_valid_monitors_returns_no_errors(self):
        errors = monitors.validate([make_monitor()])
        self.assertIsInstance(errors, list)
        self.assertEqual(errors, [])

    def test_validate_missing_required_fields_reports_errors(self):
        errors = monitors.validate([{"id": "m1"}])
        self.assertIsInstance(errors, list)
        self.assertTrue(errors)

    def test_validate_invalid_type_or_severity_reports_errors(self):
        errors = monitors.validate([{
            "id": "m1",
            "type": "not_a_real_monitor_type",
            "threshold": 0.5,
            "enabled": True,
            "severity": "not_a_severity",
        }])
        self.assertIsInstance(errors, list)
        self.assertTrue(errors)

    # ------------------------------------------------------------------
    # evaluate: cpa_spike
    # ------------------------------------------------------------------
    def test_evaluate_cpa_spike_triggers_and_alert_shape(self):
        monitor = make_monitor(type="cpa_spike", threshold=0.5, severity="warn")
        snapshot = make_snapshot([
            make_campaign(cpa_7d=20.0, cpa_30d=10.0),
        ])

        alerts = monitors.evaluate([monitor], snapshot)

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        for key in ("monitor", "campaign", "severity", "title", "detail",
                    "value", "threshold"):
            self.assertIn(key, alert)

        self.assertEqual(alert["monitor"], "m1")
        self.assertIn(alert["campaign"], ("c1", "Camp 1"))
        self.assertEqual(alert["severity"], "warn")
        self.assertIsInstance(alert["title"], str)
        self.assertIsInstance(alert["detail"], str)

    def test_evaluate_cpa_spike_default_threshold_and_zero_base(self):
        monitor = {
            "id": "m2",
            "type": "cpa_spike",
            "enabled": True,
            "severity": "warn",
        }

        # Default threshold is 0.5: 16 > 10 * 1.5
        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(cpa_7d=16.0, cpa_30d=10.0)]),
        )
        self.assertEqual(len(alerts), 1)

        # cpa_30d == 0 must not trigger cpa_spike.
        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(cpa_7d=100.0, cpa_30d=0.0)]),
        )
        self.assertEqual(alerts, [])

    # ------------------------------------------------------------------
    # evaluate: pacing_over / pacing_under
    # ------------------------------------------------------------------
    def test_evaluate_pacing_over_default_threshold_and_crit_escalation(self):
        monitor = {
            "id": "po",
            "type": "pacing_over",
            "enabled": True,
            "severity": "warn",
        }

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(daily_budget=100.0, spend_today=160.0)]),
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "warn")

        # Ratio >= 2.0 must be crit even if monitor severity is warn.
        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(daily_budget=100.0, spend_today=210.0)]),
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "crit")

    def test_evaluate_pacing_under_requires_hour_and_enabled(self):
        monitor = {
            "id": "pu",
            "type": "pacing_under",
            "threshold": 0.3,
            "enabled": True,
            "severity": "warn",
        }

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(
                daily_budget=100.0, spend_today=10.0,
                hour=15, status="ENABLED",
            )]),
        )
        self.assertEqual(len(alerts), 1)

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(
                daily_budget=100.0, spend_today=10.0,
                hour=14, status="ENABLED",
            )]),
        )
        self.assertEqual(alerts, [])

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(
                daily_budget=100.0, spend_today=10.0,
                hour=15, status="PAUSED",
            )]),
        )
        self.assertEqual(alerts, [])

    # ------------------------------------------------------------------
    # evaluate: month_forecast
    # ------------------------------------------------------------------
    def test_evaluate_month_forecast_default_threshold(self):
        monitor = {
            "id": "mf",
            "type": "month_forecast",
            "enabled": True,
            "severity": "warn",
        }

        # 2024-06-15: forecast = 3000 / 15 * 30 = 6000
        # default threshold 0.1: 6000 > 5000 * 1.1
        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(spend_month=3000.0, month_budget=5000.0)]),
        )
        self.assertEqual(len(alerts), 1)

    def test_evaluate_month_forecast_without_month_budget_no_alert(self):
        monitor = {
            "id": "mf",
            "type": "month_forecast",
            "enabled": True,
            "severity": "warn",
        }
        campaign = make_campaign(spend_month=3000.0)
        del campaign["month_budget"]

        alerts = monitors.evaluate([monitor], make_snapshot([campaign]))
        self.assertEqual(alerts, [])

    # ------------------------------------------------------------------
    # evaluate: zero_impressions / disapprovals / budget_limited /
    # no_conversions_days
    # ------------------------------------------------------------------
    def test_evaluate_zero_impressions_requires_enabled_and_hour(self):
        monitor = {
            "id": "zi",
            "type": "zero_impressions",
            "enabled": True,
            "severity": "warn",
        }

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(
                impressions_today=0, hour=12, status="ENABLED",
            )]),
        )
        self.assertEqual(len(alerts), 1)

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(
                impressions_today=0, hour=11, status="ENABLED",
            )]),
        )
        self.assertEqual(alerts, [])

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(
                impressions_today=0, hour=12, status="PAUSED",
            )]),
        )
        self.assertEqual(alerts, [])

    def test_evaluate_disapprovals(self):
        monitor = {
            "id": "da",
            "type": "disapprovals",
            "enabled": True,
            "severity": "warn",
        }

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(disapproved_ads=1)]),
        )
        self.assertEqual(len(alerts), 1)

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(disapproved_ads=0)]),
        )
        self.assertEqual(alerts, [])

    def test_evaluate_budget_limited_threshold_inclusive(self):
        monitor = {
            "id": "bl",
            "type": "budget_limited",
            "enabled": True,
            "severity": "warn",
        }

        # Default threshold is 0.2 and comparison is >=.
        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(lost_budget_is=0.2)]),
        )
        self.assertEqual(len(alerts), 1)

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(lost_budget_is=0.19)]),
        )
        self.assertEqual(alerts, [])

    def test_evaluate_no_conversions_days_threshold_inclusive(self):
        monitor = {
            "id": "nc",
            "type": "no_conversions_days",
            "enabled": True,
            "severity": "warn",
        }

        # Default threshold is 7 and comparison is >=.
        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(days_since_conv=7)]),
        )
        self.assertEqual(len(alerts), 1)

        alerts = monitors.evaluate(
            [monitor],
            make_snapshot([make_campaign(days_since_conv=6)]),
        )
        self.assertEqual(alerts, [])

    # ------------------------------------------------------------------
    # evaluate: campaign targeting
    # ------------------------------------------------------------------
    def test_evaluate_respects_optional_campaign_targeting(self):
        monitor = make_monitor(
            id="targeted",
            type="cpa_spike",
            threshold=0.5,
            campaign="c1",
        )
        c1 = make_campaign(key="c1", name="Camp 1", cpa_7d=20.0, cpa_30d=10.0)
        c2 = make_campaign(key="c2", name="Camp 2", cpa_7d=20.0, cpa_30d=10.0)

        alerts = monitors.evaluate([monitor], make_snapshot([c1, c2]))

        self.assertEqual(len(alerts), 1)
        self.assertIn(alerts[0]["campaign"], ("c1", "Camp 1"))

    # ------------------------------------------------------------------
    # dedupe
    # ------------------------------------------------------------------
    def test_dedupe_first_call_then_suppresses_within_window(self):
        alert = make_alert()
        now = "2024-06-15T12:00:00"

        first, state = monitors.dedupe([alert], {}, now)
        self.assertEqual(len(first), 1)
        self.assertIsInstance(state, dict)

        second, state2 = monitors.dedupe(
            [alert], state, "2024-06-15T13:00:00"
        )
        self.assertEqual(second, [])
        self.assertIsInstance(state2, dict)

    def test_dedupe_expires_after_repeat_hours(self):
        alert = make_alert()
        first, state = monitors.dedupe(
            [alert], {}, "2024-06-15T12:00:00", repeat_hours=24
        )
        self.assertEqual(len(first), 1)

        again, _ = monitors.dedupe(
            [alert], state, "2024-06-16T13:00:00", repeat_hours=24
        )
        self.assertEqual(len(again), 1)

    def test_dedupe_escalation_warn_to_crit_always_fires(self):
        warn_alert = make_alert(severity="warn")
        crit_alert = make_alert(severity="crit")

        first, state = monitors.dedupe(
            [warn_alert], {}, "2024-06-15T12:00:00"
        )
        self.assertEqual(len(first), 1)

        second, _ = monitors.dedupe(
            [crit_alert], state, "2024-06-15T13:00:00"
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["severity"], "crit")

    # ------------------------------------------------------------------
    # brief
    # ------------------------------------------------------------------
    def test_brief_returns_bullet_markdown_with_totals(self):
        snapshot = make_snapshot([
            make_campaign(spend_today=200.0, conv_today=8),
        ])
        alert = make_alert(
            title="Rare issue marker",
            detail="Rare detail marker",
        )

        text = monitors.brief(snapshot, [alert], title="My Brief")

        self.assertIsInstance(text, str)
        self.assertIn("My Brief", text)
        self.assertLessEqual(len(text), 3500)

        # Totals should be present: spend 200, conversions 8, blended CPA 25.
        self.assertIn("200", text)
        self.assertIn("8", text)
        self.assertIn("25", text)

        # A top issue should be represented by title or detail.
        self.assertTrue(
            "Rare issue marker" in text or "Rare detail marker" in text
        )

        # No markdown tables: bullets, not pipe tables.
        for line in text.splitlines():
            self.assertFalse(line.strip().startswith("|"))
        self.assertTrue(
            any(line.lstrip().startswith(("-", "*")) for line in text.splitlines())
        )

    def test_brief_truncates_long_content_to_limit(self):
        alerts = [
            make_alert(
                monitor="m{}".format(i),
                title="X" * 200,
                detail="Y" * 200,
            )
            for i in range(50)
        ]

        text = monitors.brief(make_snapshot(), alerts)
        self.assertIsInstance(text, str)
        self.assertLessEqual(len(text), 3500)

    # ------------------------------------------------------------------
    # deliver
    # ------------------------------------------------------------------
    def test_deliver_success_calls_notify(self):
        notify = mock.Mock()

        result = monitors.deliver("hello", notify)

        notify.assert_called_once_with("hello")
        self.assertIsInstance(result, dict)
        self.assertTrue(result["delivered"])
        self.assertIn("error", result)
        self.assertIsNone(result["error"])

    def test_deliver_notify_exception_returns_error(self):
        notify = mock.Mock(side_effect=RuntimeError("boom api_key=SECRET"))

        result = monitors.deliver("hello", notify)

        self.assertIsInstance(result, dict)
        self.assertFalse(result["delivered"])
        self.assertIn("error", result)
        self.assertIsInstance(result["error"], str)

    # ------------------------------------------------------------------
    # webhook_notifier
    # ------------------------------------------------------------------
    def test_webhook_notifier_requires_https(self):
        http = mock.Mock(return_value=(200, "ok"))

        try:
            notify = monitors.webhook_notifier("http://example.com/hook", http)
        except Exception:
            return

        self.assertTrue(callable(notify))
        with self.assertRaises(Exception):
            notify("hello")
        self.assertFalse(http.called)

    def test_webhook_notifier_posts_json_on_2xx_success(self):
        http = mock.Mock(return_value=(201, "created"))
        notify = monitors.webhook_notifier("https://example.com/hook", http)

        notify("hello")

        self.assertTrue(http.called)
        call = http.call_args
        if call.args:
            method = call.args[0]
            url = call.args[1]
            body = call.args[3]
        else:
            method = call.kwargs.get("method")
            url = call.kwargs.get("url")
            body = call.kwargs.get("body")

        self.assertEqual(str(method).upper(), "POST")
        self.assertEqual(url, "https://example.com/hook")

        if isinstance(body, (str, bytes, bytearray)):
            payload = json.loads(body)
        else:
            payload = body
        self.assertEqual(payload, {"text": "hello"})

    def test_webhook_notifier_non_2xx_raises_tool_error(self):
        http = mock.Mock(return_value=(500, "server error"))
        notify = monitors.webhook_notifier("https://example.com/hook", http)

        self.assert_tool_error(lambda: notify("hello"))


if __name__ == '__main__':
    unittest.main()
