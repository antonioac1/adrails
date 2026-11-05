import json
import unittest

from adsops.aeo import indexnow


def _is_ok(result):
    """Accept either a bool or a documented dict-shaped result."""
    if isinstance(result, dict) and "ok" in result:
        return bool(result["ok"])
    return bool(result)


class ToolErrorAssertions(unittest.TestCase):
    def assertToolError(self, func, *args, **kwargs):
        with self.assertRaises(Exception) as cm:
            func(*args, **kwargs)
        self.assertEqual(type(cm.exception).__name__, "ToolError")


class ValidateKeyTests(ToolErrorAssertions):
    def test_validate_key_accepts_allowed_characters_and_boundaries(self):
        for key in ["abcdefgh", "A1-b2-C3", "a" * 128, "-" * 8]:
            indexnow.validate_key(key)

    def test_validate_key_rejects_bad_lengths(self):
        for key in ["", "a" * 7, "a" * 129]:
            self.assertToolError(indexnow.validate_key, key)

    def test_validate_key_rejects_bad_characters(self):
        for key in ["abcdefg_", "abcdefg ", "abcdefg.", "abcdefg$", "ábcdefgh"]:
            self.assertToolError(indexnow.validate_key, key)


class BuildIndexNowTests(ToolErrorAssertions):
    def test_build_indexnow_returns_endpoint_headers_and_body(self):
        host = "example.com"
        key = "abcdefgh"
        urls = ["https://example.com/a", "https://example.com/b"]

        url, headers, body = indexnow.build_indexnow(host, key, urls)

        self.assertEqual(url, "https://api.indexnow.org/indexnow")
        self.assertEqual(
            headers,
            {"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(body["host"], host)
        self.assertEqual(body["key"], key)
        self.assertEqual(body["urlList"], urls)

    def test_build_indexnow_dedupes_preserving_order(self):
        host = "example.com"
        key = "abcdefgh"
        urls = [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/a",
            "https://example.com/c",
            "https://example.com/b",
        ]

        _, _, body = indexnow.build_indexnow(host, key, urls)
        self.assertEqual(
            body["urlList"],
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
        )

    def test_build_indexnow_includes_key_location_when_supplied(self):
        host = "example.com"
        key = "abcdefgh"
        key_location = "https://example.com/abcdefgh.txt"

        _, _, body = indexnow.build_indexnow(
            host,
            key,
            ["https://example.com/a"],
            key_location=key_location,
        )
        self.assertEqual(body["keyLocation"], key_location)

    def test_build_indexnow_rejects_empty_and_too_many_urls(self):
        self.assertToolError(
            indexnow.build_indexnow,
            "example.com",
            "abcdefgh",
            [],
        )

        too_many = [f"https://example.com/{i}" for i in range(10001)]
        self.assertToolError(
            indexnow.build_indexnow,
            "example.com",
            "abcdefgh",
            too_many,
        )

    def test_build_indexnow_rejects_non_https_and_non_exact_hosts(self):
        bad_urls = [
            "http://example.com/a",
            "https://evil.com/a",
            "https://sub.example.com/a",
            "https://example.com.evil.com/a",
        ]
        for bad_url in bad_urls:
            with self.subTest(bad_url=bad_url):
                self.assertToolError(
                    indexnow.build_indexnow,
                    "example.com",
                    "abcdefgh",
                    [bad_url],
                )

    def test_build_indexnow_accepts_exact_host_and_10000_urls(self):
        host = "example.com"
        key = "abcdefgh"
        urls = [f"https://example.com/{i}" for i in range(10000)]

        _, _, body = indexnow.build_indexnow(host, key, urls)
        self.assertEqual(len(body["urlList"]), 10000)
        self.assertEqual(body["urlList"][0], "https://example.com/0")
        self.assertEqual(body["urlList"][-1], "https://example.com/9999")


class SubmitIndexNowTests(ToolErrorAssertions):
    def test_submit_indexnow_success_statuses_use_http_contract(self):
        host = "example.com"
        key = "abcdefgh"
        urls = ["https://example.com/a", "https://example.com/b"]

        for status in (200, 202):
            with self.subTest(status=status):
                calls = []

                def http(method, url, headers, body_bytes):
                    calls.append((method, url, headers, body_bytes))
                    return status, "OK"

                result = indexnow.submit_indexnow(host, key, urls, http)

                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertEqual(len(calls), 1)

                method, url, headers, body_bytes = calls[0]
                self.assertEqual(method, "POST")
                self.assertEqual(url, "https://api.indexnow.org/indexnow")
                self.assertEqual(
                    headers,
                    {"Content-Type": "application/json; charset=utf-8"},
                )
                self.assertIsInstance(body_bytes, (bytes, bytearray))

                body = json.loads(body_bytes.decode("utf-8"))
                self.assertEqual(body["host"], host)
                self.assertEqual(body["key"], key)
                self.assertEqual(body["urlList"], urls)

    def test_submit_indexnow_documented_error_statuses(self):
        host = "example.com"
        key = "abcdefgh"
        urls = ["https://example.com/a"]

        for status in (400, 403, 422, 429):
            with self.subTest(status=status):

                def http(method, url, headers, body_bytes):
                    return status, "error"

                result = indexnow.submit_indexnow(host, key, urls, http)

                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertIn("meaning", result)
                self.assertIsInstance(result["meaning"], str)
                self.assertTrue(result["meaning"].strip())

                meaning = result["meaning"].lower()
                if status == 400:
                    self.assertTrue(
                        any(token in meaning for token in ("bad", "invalid", "request"))
                    )
                elif status == 403:
                    self.assertIn("key", meaning)
                elif status == 422:
                    self.assertTrue(
                        any(token in meaning for token in ("url", "host", "key"))
                    )
                elif status == 429:
                    self.assertTrue(
                        any(
                            token in meaning
                            for token in ("too many", "rate", "limit")
                        )
                    )

    def test_submit_indexnow_batches_10001_urls(self):
        host = "example.com"
        key = "abcdefgh"
        urls = [f"https://example.com/{i}" for i in range(10001)]

        batches = []

        def http(method, url, headers, body_bytes):
            body = json.loads(body_bytes.decode("utf-8"))
            batches.append(body["urlList"])
            return 200, "OK"

        result = indexnow.submit_indexnow(host, key, urls, http)

        self.assertTrue(result["ok"])
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 10000)
        self.assertEqual(len(batches[1]), 1)
        self.assertEqual(batches[0][0], urls[0])
        self.assertEqual(batches[0][-1], urls[9999])
        self.assertEqual(batches[1][0], urls[10000])

    def test_submit_indexnow_dedupes_before_sending(self):
        host = "example.com"
        key = "abcdefgh"
        urls = [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/a",
        ]

        sent = []

        def http(method, url, headers, body_bytes):
            sent.append(json.loads(body_bytes.decode("utf-8")))
            return 200, "OK"

        result = indexnow.submit_indexnow(host, key, urls, http)

        self.assertTrue(result["ok"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sent[0]["urlList"],
            ["https://example.com/a", "https://example.com/b"],
        )

    def test_submit_indexnow_passes_key_location(self):
        host = "example.com"
        key = "abcdefgh"
        key_location = "https://example.com/abcdefgh.txt"

        sent = []

        def http(method, url, headers, body_bytes):
            sent.append(json.loads(body_bytes.decode("utf-8")))
            return 200, "OK"

        result = indexnow.submit_indexnow(
            host,
            key,
            ["https://example.com/a"],
            http,
            key_location=key_location,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(sent[0]["keyLocation"], key_location)

    def test_submit_indexnow_does_not_call_http_on_invalid_input(self):
        called = False

        def http(method, url, headers, body_bytes):
            nonlocal called
            called = True
            return 200, "OK"

        self.assertToolError(
            indexnow.submit_indexnow,
            "example.com",
            "abcdefgh",
            ["http://example.com/a"],
            http,
        )
        self.assertFalse(called)


class CheckKeyFileTests(ToolErrorAssertions):
    def test_check_key_file_success_strips_body_and_calls_expected_url(self):
        host = "example.com"
        key = "abcdefgh"
        calls = []

        def fetch(url):
            calls.append(url)
            return 200, f"  {key}\n"

        result = indexnow.check_key_file(host, key, fetch)

        self.assertTrue(_is_ok(result))
        self.assertEqual(calls, [f"https://{host}/{key}.txt"])

    def test_check_key_file_200_but_wrong_body_is_false(self):
        host = "example.com"
        key = "abcdefgh"

        def fetch(url):
            return 200, "wrong-key"

        result = indexnow.check_key_file(host, key, fetch)
        self.assertFalse(_is_ok(result))

    def test_check_key_file_non_200_is_false(self):
        host = "example.com"
        key = "abcdefgh"

        def fetch(url):
            return 404, key

        result = indexnow.check_key_file(host, key, fetch)
        self.assertFalse(_is_ok(result))


class BuildBingSubmitTests(ToolErrorAssertions):
    def test_build_bing_submit_default_structure_and_placeholder(self):
        site_url = "https://example.com"
        urls = ["https://example.com/a", "https://example.com/b"]

        out = indexnow.build_bing_submit(site_url, urls)

        self.assertEqual(out["method"], "POST")
        self.assertEqual(
            out["url"],
            "https://ssl.bing.com/webmaster/api.svc/json/"
            "SubmitUrlbatch?apikey={BING_API_KEY}",
        )
        self.assertEqual(out["body"]["siteUrl"], site_url)
        self.assertEqual(out["body"]["urlList"], urls)

    def test_build_bing_submit_custom_key_name_placeholder(self):
        site_url = "https://example.com"
        urls = ["https://example.com/a"]

        out = indexnow.build_bing_submit(
            site_url,
            urls,
            api_key_name="MY_KEY_NAME",
        )

        self.assertEqual(
            out["url"],
            "https://ssl.bing.com/webmaster/api.svc/json/"
            "SubmitUrlbatch?apikey={MY_KEY_NAME}",
        )
        self.assertIn("{MY_KEY_NAME}", out["url"])

    def test_build_bing_submit_preserves_site_and_urls(self):
        site_url = "https://example.com"
        urls = [
            "https://example.com/one",
            "https://example.com/two",
            "https://example.com/three",
        ]

        out = indexnow.build_bing_submit(site_url, urls)

        self.assertEqual(out["body"]["siteUrl"], site_url)
        self.assertEqual(out["body"]["urlList"], urls)


class ChangedUrlsTests(ToolErrorAssertions):
    def test_changed_urls_returns_locs_with_lastmod_gte_since(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://example.com/new</loc>
            <lastmod>2024-02-01</lastmod>
          </url>
          <url>
            <loc>https://example.com/equal</loc>
            <lastmod>2024-01-01</lastmod>
          </url>
          <url>
            <loc>https://example.com/old</loc>
            <lastmod>2023-12-31</lastmod>
          </url>
        </urlset>"""

        result = indexnow.changed_urls(sitemap_xml, "2024-01-01")

        self.assertCountEqual(
            result,
            [
                "https://example.com/new",
                "https://example.com/equal",
            ],
        )

    def test_changed_urls_excludes_older_and_missing_lastmod(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://example.com/old</loc>
            <lastmod>2023-12-31</lastmod>
          </url>
          <url>
            <loc>https://example.com/missing</loc>
          </url>
        </urlset>"""

        result = indexnow.changed_urls(sitemap_xml, "2024-01-01")
        self.assertCountEqual(result, [])

    def test_changed_urls_handles_xml_namespaces(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://example.com/ns-url</loc>
            <lastmod>2024-05-01</lastmod>
          </url>
        </urlset>"""

        result = indexnow.changed_urls(sitemap_xml, "2024-01-01")
        self.assertEqual(list(result), ["https://example.com/ns-url"])

    def test_changed_urls_handles_sitemap_index(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap>
            <loc>https://example.com/sitemap-a.xml</loc>
            <lastmod>2024-02-01</lastmod>
          </sitemap>
          <sitemap>
            <loc>https://example.com/sitemap-b.xml</loc>
            <lastmod>2024-03-01</lastmod>
          </sitemap>
        </sitemapindex>"""

        result = indexnow.changed_urls(sitemap_xml, "2024-01-01")
        expected = [
            "https://example.com/sitemap-a.xml",
            "https://example.com/sitemap-b.xml",
        ]

        if isinstance(result, dict):
            self.assertIn("indexes", result)
            self.assertCountEqual(result["indexes"], expected)
        else:
            self.assertCountEqual(result, expected)

    def test_changed_urls_empty_sitemap_returns_empty_list(self):
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        </urlset>"""

        result = indexnow.changed_urls(sitemap_xml, "2024-01-01")
        self.assertEqual(list(result), [])


if __name__ == "__main__":
    unittest.main()
