import json
import re
import unittest
from datetime import datetime, timedelta
from ipaddress import ip_network

from adsops.aeo import ai_traffic


class TestAITraffic(unittest.TestCase):
    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _entry(ip="1.1.1.1", path="/", referer="", ua="Mozilla/5.0",
               ts="2024-01-01T00:00:00+00:00", status=200, method="GET"):
        return {
            "ip": ip,
            "ts": ts,
            "method": method,
            "path": path,
            "status": status,
            "referer": referer,
            "ua": ua,
        }

    @staticmethod
    def _combined(ip, dt, path="/", status=200, referer="-", ua="Mozilla/5.0"):
        stamp = dt.strftime("%d/%b/%Y:%H:%M:%S +0000")
        return (
            f'{ip} - - [{stamp}] "GET {path} HTTP/1.1" {status} 42 '
            f'"{referer}" "{ua}"'
        )

    @staticmethod
    def _nets(ranges, bot):
        value = ranges[bot]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    @staticmethod
    def _regex_search(pattern, text):
        if hasattr(pattern, "search"):
            return bool(pattern.search(text))
        return bool(re.search(pattern, text, re.I))

    # --------------------------------------------------------------- REFERRERS
    def test_referrers_contains_documented_host_suffixes(self):
        expected_hosts = [
            "chatgpt.com",
            "chat.openai.com",
            "perplexity.ai",
            "claude.ai",
            "gemini.google.com",
            "copilot.microsoft.com",
            "bing.com/chat",
            "you.com",
            "phind.com",
            "meta.ai",
        ]
        for host in expected_hosts:
            self.assertIn(host, ai_traffic.REFERRERS)

    def test_referrers_maps_to_documented_engines(self):
        expected_engines = {
            "chatgpt",
            "perplexity",
            "claude",
            "gemini",
            "copilot",
            "you",
            "phind",
            "meta",
        }
        self.assertTrue(expected_engines.issubset(set(ai_traffic.REFERRERS.values())))

    # ------------------------------------------------------------------ BOT_UA
    def test_bot_ua_values_are_bot_owner_purpose_tuples(self):
        for value in ai_traffic.BOT_UA.values():
            self.assertIsInstance(value, tuple)
            self.assertEqual(len(value), 3)

    def test_bot_ua_recognises_documented_bot_user_agents(self):
        samples = [
            "Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
            "Mozilla/5.0 (compatible; ClaudeBot/1.0; +https://claude.ai)",
            "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai)",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
            "CCBot/2.0 (https://commoncrawl.org/faq/)",
        ]
        for ua in samples:
            self.assertTrue(
                any(self._regex_search(pattern, ua) for pattern in ai_traffic.BOT_UA),
                msg=f"no BOT_UA pattern matched {ua!r}",
            )

    # -------------------------------------------------------------- parse_line
    def test_parse_line_combined(self):
        line = (
            '1.2.3.4 - - [10/Oct/2000:13:55:36 -0700] '
            '"GET /foo?x=1 HTTP/1.1" 200 123 '
            '"https://chatgpt.com/" "Mozilla/5.0"'
        )
        entry = ai_traffic.parse_line(line)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["ip"], "1.2.3.4")
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["path"], "/foo?x=1")
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["referer"], "https://chatgpt.com/")
        self.assertEqual(entry["ua"], "Mozilla/5.0")
        self.assertTrue(entry["ts"])

    def test_parse_line_json_with_request(self):
        raw = {
            "ip": "5.6.7.8",
            "time": "2024-01-01T00:00:00Z",
            "request": "POST /api HTTP/1.1",
            "status": 201,
            "referer": "https://claude.ai/",
            "ua": "Mozilla/5.0",
        }
        entry = ai_traffic.parse_line(json.dumps(raw), fmt="json")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["ip"], "5.6.7.8")
        self.assertEqual(entry["method"], "POST")
        self.assertEqual(entry["path"], "/api")
        self.assertEqual(entry["status"], 201)
        self.assertEqual(entry["referer"], "https://claude.ai/")
        self.assertEqual(entry["ua"], "Mozilla/5.0")

    def test_parse_line_json_alternate_keys_and_uri(self):
        raw = {
            "remote_addr": "9.9.9.9",
            "ts": "2024-01-01T00:00:00Z",
            "uri": "/alt",
            "status": 200,
            "http_referer": "https://www.perplexity.ai/",
            "http_user_agent": "UnitTestAgent/1.0",
        }
        entry = ai_traffic.parse_line(json.dumps(raw), fmt="json")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["ip"], "9.9.9.9")
        self.assertEqual(entry["path"], "/alt")
        self.assertEqual(entry["referer"], "https://www.perplexity.ai/")
        self.assertEqual(entry["ua"], "UnitTestAgent/1.0")

    def test_parse_line_returns_none_for_invalid_input(self):
        self.assertIsNone(ai_traffic.parse_line(""))
        self.assertIsNone(ai_traffic.parse_line("garbage"))
        self.assertIsNone(ai_traffic.parse_line("{not json", fmt="json"))

    # ------------------------------------------------------------- load_ranges
    def test_load_ranges_compiles_cidrs(self):
        ranges = ai_traffic.load_ranges({"GPTBot": ["203.0.113.0/24"]})
        self.assertIn("GPTBot", ranges)
        nets = self._nets(ranges, "GPTBot")
        self.assertTrue(any(net == ip_network("203.0.113.0/24") for net in nets))

    def test_ip_in(self):
        ranges = ai_traffic.load_ranges({"GPTBot": ["203.0.113.0/24"]})
        nets = self._nets(ranges, "GPTBot")
        self.assertTrue(ai_traffic.ip_in("203.0.113.7", nets))
        self.assertFalse(ai_traffic.ip_in("198.51.100.7", nets))

    # ---------------------------------------------------------------- classify
    def test_classify_ai_referral_host_suffix(self):
        entry = self._entry(referer="https://www.perplexity.ai/search?q=x")
        result = ai_traffic.classify(entry)
        self.assertEqual(result["kind"], "ai_referral")
        self.assertEqual(result["engine"], "perplexity")

    def test_classify_ai_referral_openai_alias(self):
        entry = self._entry(referer="https://chat.openai.com/c/123")
        result = ai_traffic.classify(entry)
        self.assertEqual(result["kind"], "ai_referral")
        self.assertEqual(result["engine"], "chatgpt")

    def test_classify_ai_referral_from_utm_source(self):
        entry = self._entry(
            path="/landing?utm_source=chatgpt.com",
            referer="https://example.com/?utm_source=chatgpt.com",
        )
        result = ai_traffic.classify(entry)
        self.assertEqual(result["kind"], "ai_referral")
        self.assertEqual(result["engine"], "chatgpt")

    def test_classify_verified_ai_bot(self):
        ranges = ai_traffic.load_ranges({"GPTBot": ["203.0.113.0/24"]})
        entry = self._entry(
            ip="203.0.113.10",
            ua="Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
            path="/allowed",
        )
        result = ai_traffic.classify(entry, ranges=ranges)
        self.assertEqual(result["kind"], "ai_bot_verified")
        self.assertEqual(result["bot"], "GPTBot")

    def test_classify_spoofed_ai_bot_outside_ranges(self):
        ranges = ai_traffic.load_ranges({"GPTBot": ["203.0.113.0/24"]})
        entry = self._entry(
            ip="198.51.100.5",
            ua="Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
            path="/allowed",
        )
        result = ai_traffic.classify(entry, ranges=ranges)
        self.assertEqual(result["kind"], "ai_bot_spoofed")
        self.assertEqual(result["bot"], "GPTBot")

    def test_classify_unverified_ai_bot_without_ranges(self):
        entry = self._entry(
            ip="198.51.100.5",
            ua="Mozilla/5.0 (compatible; ClaudeBot/1.0; +https://claude.ai)",
            path="/allowed",
        )
        result = ai_traffic.classify(entry)
        self.assertEqual(result["kind"], "ai_bot_unverified")
        self.assertEqual(result["bot"], "ClaudeBot")

    def test_classify_scanner_path_is_spoofed_even_with_ranges(self):
        ranges = ai_traffic.load_ranges({"GPTBot": ["203.0.113.0/24"]})
        entry = self._entry(
            ip="203.0.113.10",
            ua="Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
            path="/.env",
        )
        result = ai_traffic.classify(entry, ranges=ranges)
        self.assertEqual(result["kind"], "ai_bot_spoofed")

    def test_classify_human_and_other_bot(self):
        human = ai_traffic.classify(
            self._entry(
                referer="https://www.google.com/",
                ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            )
        )
        self.assertEqual(human["kind"], "human")

        other = ai_traffic.classify(
            self._entry(ua="Mozilla/5.0 (compatible; Googlebot/2.1)")
        )
        self.assertEqual(other["kind"], "other_bot")

    # ---------------------------------------------------------------- summarize
    def test_summarize_counts_ai_referrals_and_humans(self):
        entries = [
            self._entry(ip="1.1.1.1", referer="https://chatgpt.com/", path="/a"),
            self._entry(ip="1.1.1.2", referer="https://chatgpt.com/c/1", path="/a"),
            self._entry(ip="1.1.1.3", referer="https://www.perplexity.ai/", path="/b"),
            self._entry(ip="1.1.1.4", referer="https://www.google.com/", path="/human"),
        ]
        summary = ai_traffic.summarize(entries, site="example.com")

        self.assertEqual(summary["site"], "example.com")
        self.assertEqual(summary["ai_referrals"]["chatgpt"], 2)
        self.assertEqual(summary["ai_referrals"]["perplexity"], 1)
        self.assertEqual(summary["landing_pages"]["/a"], 2)
        self.assertEqual(summary["landing_pages"]["/b"], 1)
        self.assertNotIn("/human", summary["landing_pages"])
        self.assertEqual(summary["humans"], 1)

    def test_summarize_bot_fetches_and_spoofed_total(self):
        ranges = ai_traffic.load_ranges({"GPTBot": ["203.0.113.0/24"]})
        entries = [
            self._entry(
                ip="203.0.113.5",
                ua="Mozilla/5.0 (compatible; GPTBot/1.0)",
                path="/x",
            ),
            self._entry(
                ip="198.51.100.9",
                ua="Mozilla/5.0 (compatible; GPTBot/1.0)",
                path="/y",
            ),
            self._entry(
                ip="198.51.100.10",
                ua="Mozilla/5.0 (compatible; ClaudeBot/1.0)",
                path="/z",
            ),
        ]
        summary = ai_traffic.summarize(entries, ranges=ranges)
        bots = summary["bot_fetches"]

        self.assertEqual(bots["GPTBot"]["verified"], 1)
        self.assertEqual(bots["GPTBot"]["spoofed"], 1)
        self.assertEqual(bots["ClaudeBot"]["unverified"], 1)
        self.assertEqual(summary["spoofed_total"], 1)
        self.assertIn("/x", summary["top_bot_paths"]["GPTBot"])

    def test_summarize_flags_evenly_spaced_high_volume_ip(self):
        base = datetime(2024, 1, 1, 0, 0, 0)
        lines = [
            self._combined(
                "198.51.100.77",
                base + timedelta(seconds=60 * i),
                path=f"/p{i}",
            )
            for i in range(20)
        ]
        entries = [ai_traffic.parse_line(line) for line in lines]
        entries = [entry for entry in entries if entry]
        self.assertEqual(len(entries), 20)

        summary = ai_traffic.summarize(entries)
        self.assertIn("198.51.100.77", summary["automated_suspects"])

    def test_summarize_does_not_flag_irregular_high_volume_ip(self):
        base = datetime(2024, 1, 1, 0, 0, 0)
        lines = []
        elapsed = 0
        for i in range(20):
            lines.append(
                self._combined(
                    "198.51.100.88",
                    base + timedelta(seconds=elapsed),
                    path=f"/q{i}",
                )
            )
            elapsed += 10 if i % 2 == 0 else 1000

        entries = [ai_traffic.parse_line(line) for line in lines]
        entries = [entry for entry in entries if entry]
        self.assertEqual(len(entries), 20)

        summary = ai_traffic.summarize(entries)
        self.assertNotIn("198.51.100.88", summary["automated_suspects"])

    # -------------------------------------------------------------- weekly_line
    def test_weekly_line_returns_single_plain_text_line(self):
        entries = [
            self._entry(referer="https://chatgpt.com/", path="/a"),
            self._entry(referer="https://www.perplexity.ai/", path="/b"),
        ]
        summary = ai_traffic.summarize(entries, site="example.com")
        line = ai_traffic.weekly_line(summary)

        self.assertIsInstance(line, str)
        self.assertTrue(line.strip())
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)


if __name__ == '__main__':
    unittest.main()
