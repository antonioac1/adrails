import datetime as dt
import hashlib
import json
import re
import unittest

from adsops.mail import scans


_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _to_utc(ts):
    """Parse an ISO-8601 timestamp and return a UTC datetime."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts).astimezone(dt.timezone.utc)


def _combined(ip, when, path, ua="Mozilla/5.0", method="GET", status=200, referer="-"):
    """Build a realistic nginx/apache combined access-log line."""
    assert when.tzinfo is not None
    stamp = (
        f"{when.day:02d}/{_MONTHS[when.month - 1]}/{when.year:04d}:"
        f"{when.hour:02d}:{when.minute:02d}:{when.second:02d} "
        f"{when.strftime('%z')}"
    )
    return (
        f'{ip} - - [{stamp}] "{method} {path} HTTP/1.1" '
        f'{status} 123 "{referer}" "{ua}"'
    )


class TestParse(unittest.TestCase):
    def test_combined_parse_fields_and_utc(self):
        when = dt.datetime(
            2026, 11, 5, 10, 0, 0,
            tzinfo=dt.timezone(dt.timedelta(hours=-5)),
        )
        line = _combined(
            "1.2.3.4",
            when,
            "/x?c=AB&d=1",
            ua="Mozilla/5.0",
            referer="https://ref.example/",
        )

        entry = scans.parse(line)
        self.assertIsNotNone(entry)
        for key in (
            "ip", "ts", "method", "path", "query",
            "status", "ua", "referer",
        ):
            self.assertIn(key, entry)

        self.assertEqual(entry["ip"], "1.2.3.4")
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["path"], "/x")
        self.assertIn("AB", str(entry["query"]))
        self.assertEqual(str(entry["status"]), "200")
        self.assertEqual(entry["ua"], "Mozilla/5.0")
        self.assertEqual(entry["referer"], "https://ref.example/")

        # 10:00 -0500 is 15:00 UTC.
        self.assertEqual(
            _to_utc(entry["ts"]),
            dt.datetime(2026, 11, 5, 15, 0, tzinfo=dt.timezone.utc),
        )

    def test_combined_parse_invalid_returns_none(self):
        self.assertIsNone(scans.parse("not a log line"))
        self.assertIsNone(scans.parse(""))

    def test_json_parse_request_variant(self):
        obj = {
            "remote_addr": "1.2.3.4",
            "time_iso8601": "2026-11-05T10:00:00-05:00",
            "request": "GET /x?c=AB HTTP/1.1",
            "status": 200,
            "http_user_agent": "UA",
            "http_referer": "ref",
        }
        entry = scans.parse(json.dumps(obj), fmt="json")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["ip"], "1.2.3.4")
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["path"], "/x")
        self.assertEqual(str(entry["status"]), "200")
        self.assertEqual(entry["ua"], "UA")
        self.assertEqual(entry["referer"], "ref")
        self.assertEqual(scans.extract_code(entry), "AB")
        self.assertEqual(
            _to_utc(entry["ts"]),
            dt.datetime(2026, 11, 5, 15, 0, tzinfo=dt.timezone.utc),
        )

    def test_json_parse_alt_keys(self):
        obj = {
            "ip": "5.6.7.8",
            "time": "2026-11-05T10:00:00Z",
            "uri": "/y",
            "args": "c=CD",
            "status": "304",
            "ua": "UA2",
            "referer": "-",
        }
        entry = scans.parse(json.dumps(obj), fmt="json")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["ip"], "5.6.7.8")
        self.assertEqual(entry["path"], "/y")
        self.assertEqual(str(entry["status"]), "304")
        self.assertEqual(entry["ua"], "UA2")
        self.assertEqual(scans.extract_code(entry), "CD")

    def test_json_parse_invalid_returns_none(self):
        self.assertIsNone(scans.parse("{not json}", fmt="json"))


class TestBotRegex(unittest.TestCase):
    def test_bot_ua_matches_bots(self):
        for ua in (
            "Googlebot/2.1",
            "facebookexternalhit/1.1",
            "WhatsApp/2.0",
            "Slackbot-LinkExpanding 1.0",
            "curl/7.68.0",
            "python-requests/2.25.1",
            "HeadlessChrome/120.0",
        ):
            with self.subTest(ua=ua):
                self.assertIsNotNone(re.search(scans.BOT_UA, ua, re.IGNORECASE))

    def test_bot_ua_does_not_match_normal_browser(self):
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.assertIsNone(re.search(scans.BOT_UA, ua, re.IGNORECASE))


class TestExtractCode(unittest.TestCase):
    def test_extract_code_default(self):
        entry = {"path": "/p", "query": "c=ab&x=1"}
        self.assertEqual(scans.extract_code(entry), "AB")

        self.assertIsNone(scans.extract_code({"path": "/p", "query": "d=ab"}))
        self.assertIsNone(scans.extract_code({"path": "/p"}))

    def test_extract_code_path_prefixes_and_missing(self):
        entry = {"path": "/q/p", "query": "c=ab"}
        self.assertEqual(
            scans.extract_code(entry, path_prefixes=("/q/",)),
            "AB",
        )
        self.assertIsNone(
            scans.extract_code(
                {"path": "/other", "query": "c=ab"},
                path_prefixes=("/q/",),
            )
        )

    def test_extract_code_custom_normalizer(self):
        entry = {"path": "/p", "query": "c=AbC"}
        self.assertEqual(
            scans.extract_code(entry, normalizer=lambda s: s.lower()),
            "abc",
        )


class TestCount(unittest.TestCase):
    def setUp(self):
        self.when = dt.datetime(
            2026, 11, 5, 10, 0, 0, tzinfo=dt.timezone.utc
        )
        self.mailed = {"AB": "2026-11-01T00:00:00+00:00"}

    def test_count_basic_and_visitor_id(self):
        line = _combined(
            "1.2.3.4",
            self.when,
            "/x?c=AB",
            ua="Mozilla/5.0",
        )
        result = scans.count([line], self.mailed, salt="s")

        self.assertIn("AB", result["scans"])
        scan = result["scans"]["AB"]
        self.assertEqual(scan["visitors"], 1)
        self.assertEqual(scan["hits"], 1)
        self.assertIn("first_scan", scan)

        expected_id = hashlib.sha256(
            ("s" + "1.2.3.4" + "|" + "Mozilla/5.0").encode("utf-8")
        ).hexdigest()[:16]
        self.assertEqual(result["visitor_ids"]["AB"], [expected_id])

        mailed_dt = _to_utc(self.mailed["AB"])
        first_dt = _to_utc(scan["first_scan"])
        self.assertAlmostEqual(
            (first_dt - mailed_dt).total_seconds() / 86400.0,
            4 + 10 / 24.0,
            places=3,
        )

        for key in (
            "bot", "own_ip", "unknown_code", "before_mailing",
            "not_get", "bad_status", "unparsed",
        ):
            self.assertIn(key, result["dropped"])

        # Raw IPs must never leak into the output.
        self.assertNotIn("1.2.3.4", json.dumps(result))

    def test_count_same_visitor_multiple_hits(self):
        t1 = dt.datetime(2026, 11, 5, 10, 0, tzinfo=dt.timezone.utc)
        t2 = dt.datetime(2026, 11, 5, 11, 0, tzinfo=dt.timezone.utc)
        lines = [
            _combined("1.2.3.4", t1, "/x?c=AB", ua="UA"),
            _combined("1.2.3.4", t2, "/x?c=AB", ua="UA"),
        ]
        result = scans.count(lines, self.mailed)

        self.assertEqual(result["scans"]["AB"]["visitors"], 1)
        self.assertEqual(result["scans"]["AB"]["hits"], 2)
        self.assertEqual(len(result["visitor_ids"]["AB"]), 1)

    def test_count_different_visitors(self):
        t1 = dt.datetime(2026, 11, 5, 10, 0, tzinfo=dt.timezone.utc)
        t2 = dt.datetime(2026, 11, 5, 11, 0, tzinfo=dt.timezone.utc)
        lines = [
            _combined("1.2.3.4", t1, "/x?c=AB", ua="UA1"),
            _combined("1.2.3.4", t2, "/x?c=AB", ua="UA2"),
        ]
        result = scans.count(lines, self.mailed)

        self.assertEqual(result["scans"]["AB"]["visitors"], 2)
        self.assertEqual(result["scans"]["AB"]["hits"], 2)
        self.assertEqual(len(result["visitor_ids"]["AB"]), 2)

    def test_count_drops_bots(self):
        line = _combined(
            "1.2.3.4",
            self.when,
            "/x?c=AB",
            ua="Googlebot/2.1",
        )
        result = scans.count([line], self.mailed)

        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["bot"], 1)

    def test_count_drops_own_ip_and_cidr(self):
        line = _combined("1.2.3.4", self.when, "/x?c=AB")
        result = scans.count([line], self.mailed, own_ips=("1.2.3.4",))
        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["own_ip"], 1)

        line2 = _combined("10.1.2.3", self.when, "/x?c=AB")
        result2 = scans.count(
            [line2], self.mailed, own_cidrs=("10.0.0.0/8",)
        )
        self.assertEqual(result2["scans"], {})
        self.assertEqual(result2["dropped"]["own_ip"], 1)

    def test_count_drops_own_ip_v6_cidr(self):
        line = _combined("2001:db8::1", self.when, "/x?c=AB")
        result = scans.count(
            [line], self.mailed, own_cidrs=("2001:db8::/32",)
        )
        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["own_ip"], 1)

    def test_count_drops_unknown_code(self):
        line = _combined("1.2.3.4", self.when, "/x?c=ZZ")
        result = scans.count([line], self.mailed)

        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["unknown_code"], 1)

    def test_count_drops_before_mailing(self):
        mailed = {"AB": "2026-11-10T00:00:00+00:00"}
        line = _combined("1.2.3.4", self.when, "/x?c=AB")
        result = scans.count([line], mailed)

        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["before_mailing"], 1)

    def test_count_drops_not_get_and_allows_head(self):
        post = _combined(
            "1.2.3.4", self.when, "/x?c=AB", method="POST"
        )
        result = scans.count([post], self.mailed)
        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["not_get"], 1)

        head = _combined(
            "1.2.3.4", self.when, "/x?c=AB", method="HEAD"
        )
        result2 = scans.count([head], self.mailed)
        self.assertEqual(result2["scans"]["AB"]["visitors"], 1)

    def test_count_allows_redirects_and_304_drops_bad_status(self):
        for status in (200, 301, 302, 304):
            with self.subTest(status=status):
                line = _combined(
                    "1.2.3.4", self.when, "/x?c=AB", status=status
                )
                result = scans.count([line], self.mailed)
                self.assertIn("AB", result["scans"])

        bad = _combined("1.2.3.4", self.when, "/x?c=AB", status=404)
        result = scans.count([bad], self.mailed)
        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["bad_status"], 1)

    def test_count_skips_asset_paths(self):
        for path in (
            "/static/app.js?c=AB",
            "/img/logo.png?c=AB",
            "/css/site.css?c=AB",
            "/favicon.ico?c=AB",
            "/font.woff2?c=AB",
        ):
            with self.subTest(path=path):
                line = _combined("1.2.3.4", self.when, path)
                result = scans.count([line], self.mailed)
                self.assertEqual(result["scans"], {})

    def test_count_unparsed(self):
        result = scans.count(["garbage"], self.mailed)
        self.assertEqual(result["scans"], {})
        self.assertEqual(result["dropped"]["unparsed"], 1)

    def test_count_custom_normalizer_and_param(self):
        mailed = {"ab": "2026-11-01T00:00:00+00:00"}
        line = _combined("1.2.3.4", self.when, "/t/x?code=AB")
        result = scans.count(
            [line],
            mailed,
            param="code",
            path_prefixes=("/t/",),
            normalizer=lambda s: s.lower(),
        )
        self.assertIn("ab", result["scans"])
        self.assertEqual(result["scans"]["ab"]["visitors"], 1)

        other = _combined("1.2.3.4", self.when, "/other?code=AB")
        result2 = scans.count(
            [other],
            mailed,
            param="code",
            path_prefixes=("/t/",),
            normalizer=lambda s: s.lower(),
        )
        self.assertEqual(result2["scans"], {})


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.mailed = {"AB": "2026-11-01T00:00:00+00:00"}

    def test_merge_unions_visitors_and_sums_hits(self):
        t1 = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)
        t2 = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
        t3 = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.timezone.utc)

        prev = scans.count(
            [_combined("1.1.1.1", t1, "/x?c=AB", ua="UA1")],
            self.mailed,
        )
        cur = scans.count(
            [
                _combined("2.2.2.2", t2, "/x?c=AB", ua="UA2"),
                _combined("1.1.1.1", t3, "/x?c=AB", ua="UA1"),
            ],
            self.mailed,
        )
        merged = scans.merge(prev, cur)

        self.assertEqual(merged["scans"]["AB"]["visitors"], 2)
        self.assertEqual(merged["scans"]["AB"]["hits"], 3)
        self.assertEqual(len(merged["visitor_ids"]["AB"]), 2)
        self.assertEqual(
            _to_utc(merged["scans"]["AB"]["first_scan"]),
            t1,
        )

    def test_merge_idempotent_visitors(self):
        t1 = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)
        line = _combined("1.1.1.1", t1, "/x?c=AB", ua="UA1")

        prev = scans.count([line], self.mailed)
        again = scans.count([line], self.mailed)
        merged = scans.merge(prev, again)

        self.assertEqual(
            merged["scans"]["AB"]["visitors"],
            prev["scans"]["AB"]["visitors"],
        )
        self.assertEqual(
            set(merged["visitor_ids"]["AB"]),
            set(prev["visitor_ids"]["AB"]),
        )


class TestSummarize(unittest.TestCase):
    def _result(self, scans_by_code):
        return {
            "scans": scans_by_code,
            "dropped": {
                "bot": 0,
                "own_ip": 0,
                "unknown_code": 0,
                "before_mailing": 0,
                "not_get": 0,
                "bad_status": 0,
                "unparsed": 0,
            },
            "visitor_ids": {},
        }

    def test_summarize_by_batch_segment_total(self):
        result = self._result(
            {
                "AB": {
                    "visitors": 2,
                    "hits": 3,
                    "first_scan": "2026-11-05T00:00:00+00:00",
                    "days_to_first_scan": 1.0,
                },
                "CD": {
                    "visitors": 1,
                    "hits": 1,
                    "first_scan": "2026-11-06T00:00:00+00:00",
                    "days_to_first_scan": 2.0,
                },
            }
        )
        pieces = [
            {"tracking_code": "AB", "batch": "B1", "segment": "S1"},
            {"tracking_code": "CD", "batch": "B1", "segment": "S2"},
            {"tracking_code": "EF", "batch": "B2", "segment": "S2"},
        ]

        summary = scans.summarize(result, pieces)

        self.assertEqual(summary["by_batch"]["B1"]["mailed"], 2)
        self.assertEqual(summary["by_batch"]["B1"]["scanned_codes"], 2)
        self.assertAlmostEqual(
            summary["by_batch"]["B1"]["scan_rate"], 1.0
        )

        self.assertEqual(summary["by_batch"]["B2"]["mailed"], 1)
        self.assertEqual(summary["by_batch"]["B2"]["scanned_codes"], 0)
        self.assertAlmostEqual(
            summary["by_batch"]["B2"]["scan_rate"], 0.0
        )

        self.assertEqual(summary["by_segment"]["S1"]["mailed"], 1)
        self.assertEqual(summary["by_segment"]["S1"]["scanned_codes"], 1)
        self.assertEqual(summary["by_segment"]["S2"]["mailed"], 2)
        self.assertEqual(summary["by_segment"]["S2"]["scanned_codes"], 1)

        self.assertEqual(summary["total"]["mailed"], 3)
        self.assertEqual(summary["total"]["scanned_codes"], 2)
        self.assertAlmostEqual(
            summary["total"]["scan_rate"], 2 / 3.0
        )

    def test_summarize_zero_visitor_not_scanned(self):
        result = self._result(
            {
                "AB": {
                    "visitors": 0,
                    "hits": 0,
                    "first_scan": None,
                    "days_to_first_scan": None,
                },
            }
        )
        pieces = [
            {"tracking_code": "AB", "batch": "B1", "segment": "S1"},
        ]

        summary = scans.summarize(result, pieces)

        self.assertEqual(summary["by_batch"]["B1"]["mailed"], 1)
        self.assertEqual(
            summary["by_batch"]["B1"]["scanned_codes"], 0
        )
        self.assertAlmostEqual(
            summary["by_batch"]["B1"]["scan_rate"], 0.0
        )
        self.assertEqual(summary["total"]["scanned_codes"], 0)
        self.assertAlmostEqual(summary["total"]["scan_rate"], 0.0)


if __name__ == '__main__':
    unittest.main()
