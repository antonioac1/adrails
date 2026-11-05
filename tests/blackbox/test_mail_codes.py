"""Tests for adsops.mail.codes, written against the documented specification.

Only public behaviour is asserted: alphabet, check characters, validity,
normalisation, generation, tracking URLs, assignment and URL parsing.
"""

import random
import unittest
import urllib.parse

from adsops.mail import codes

ALPHABET = codes.ALPHABET
VALUE = {ch: i for i, ch in enumerate(ALPHABET)}


def checksum_value(body):
    """Value (0..30) the check char must take for `body`, per the spec.

    sum(k * v_k) == 0 (mod 31) over the whole code, with the check char last.
    """
    length = len(body) + 1
    total = sum((i + 1) * VALUE[ch] for i, ch in enumerate(body))
    for value in range(31):
        if (total + length * value) % 31 == 0:
            return value
    raise AssertionError("no value satisfies the documented checksum")


def query(url):
    """Decoded query parameters of a URL."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def one_code(seed=0, length=7):
    return codes.make_code(random.Random(seed), length=length)


class TestAlphabet(unittest.TestCase):
    def test_alphabet_definition(self):
        self.assertEqual(ALPHABET, "23456789ABCDEFGHJKMNPQRSTVWXYZ")
        self.assertEqual(len(ALPHABET), 30)
        self.assertEqual(len(set(ALPHABET)), 30)
        for banned in "01ILOU":
            self.assertNotIn(banned, ALPHABET)


class TestCheckChar(unittest.TestCase):
    def test_matches_weighted_checksum(self):
        bodies = [
            "2222",
            "2345",
            "ABCDEF",
            "KMNQRS",
            "23456789",
            "ABCDEFGHJK",
            "ZYXWVTSRQ",
        ]
        for body in bodies:
            with self.subTest(body=body):
                expected = checksum_value(body)
                got = codes.check_char(body)
                if expected == 30:
                    self.assertIsNone(got)
                else:
                    self.assertEqual(got, ALPHABET[expected])
                    full = body + got
                    total = sum(
                        (i + 1) * VALUE[ch] for i, ch in enumerate(full)
                    )
                    self.assertEqual(total % 31, 0)

    def test_returns_none_for_value_30(self):
        body = "9" + "2" * 5
        self.assertEqual(checksum_value(body), 30)
        self.assertIsNone(codes.check_char(body))


class TestMakeCode(unittest.TestCase):
    def test_lengths_and_validity(self):
        for length in (5, 7, 12):
            with self.subTest(length=length):
                code = codes.make_code(random.Random(1), length=length)
                self.assertEqual(len(code), length)
                self.assertTrue(codes.is_valid(code))
                self.assertGreater(len(set(code)), 1)

    def test_seeded_determinism(self):
        self.assertEqual(
            codes.make_code(random.Random(42)),
            codes.make_code(random.Random(42)),
        )

    def test_never_all_one_character(self):
        rng = random.Random(2024)
        for _ in range(300):
            code = codes.make_code(rng)
            self.assertGreater(len(set(code)), 1)
            self.assertTrue(codes.is_valid(code))
            self.assertEqual(codes.check_char(code[:-1]), code[-1])


class TestIsValid(unittest.TestCase):
    def setUp(self):
        self.code = one_code(seed=5)

    def test_generated_code_valid(self):
        self.assertTrue(codes.is_valid(self.code))

    def test_normalizes_case_and_separators(self):
        raw = self.code
        self.assertTrue(codes.is_valid(raw.lower()))
        self.assertTrue(codes.is_valid(raw[:3] + "-" + raw[3:]))
        self.assertTrue(
            codes.is_valid(
                " " + raw.lower()[:2] + "." + raw.lower()[2:] + " "
            )
        )

    def test_length_bounds(self):
        # "2"*L is checksum-valid (every value is 0), so only length decides.
        self.assertFalse(codes.is_valid("2" * 4))
        self.assertFalse(codes.is_valid("2" * 13))
        self.assertTrue(codes.is_valid(one_code(seed=6, length=5)))

    def test_rejects_banned_characters(self):
        for banned in "01ILOU":
            with self.subTest(char=banned):
                self.assertFalse(codes.is_valid(banned + self.code[1:]))

    def test_rejects_wrong_check_char(self):
        other = ALPHABET[(VALUE[self.code[-1]] + 1) % 30]
        self.assertNotEqual(other, self.code[-1])
        self.assertFalse(codes.is_valid(self.code[:-1] + other))

    def test_detects_every_single_substitution(self):
        code = self.code
        for pos in range(len(code)):
            for value in range(30):
                char = ALPHABET[value]
                if char == code[pos]:
                    continue
                candidate = code[:pos] + char + code[pos + 1:]
                self.assertFalse(
                    codes.is_valid(candidate),
                    "substitution at %d with %s" % (pos, char),
                )

    def test_detects_adjacent_transpositions(self):
        code = self.code
        tested = 0
        for i in range(len(code) - 1):
            if code[i] == code[i + 1]:
                continue
            swapped = code[:i] + code[i + 1] + code[i] + code[i + 2:]
            self.assertFalse(codes.is_valid(swapped))
            tested += 1
        self.assertGreater(tested, 0)


class TestNormalize(unittest.TestCase):
    def test_returns_canonical_code(self):
        code = one_code(seed=9)
        self.assertEqual(codes.normalize(code), code)
        self.assertEqual(codes.normalize(code.lower()), code)
        self.assertEqual(
            codes.normalize(" " + code.lower()[:3] + "-" + code.lower()[3:] + " "),
            code,
        )
        self.assertEqual(codes.normalize(code[:2] + "." + code[2:]), code)

    def test_returns_none_without_guessing(self):
        code = one_code(seed=10)
        # '0' is not in the alphabet and must not be guessed into a code.
        self.assertIsNone(codes.normalize("0" + code[1:]))
        self.assertIsNone(codes.normalize(""))
        self.assertIsNone(codes.normalize("2" * 4))
        other = ALPHABET[(VALUE[code[-1]] + 1) % 30]
        self.assertIsNone(codes.normalize(code[:-1] + other))


class TestGenerate(unittest.TestCase):
    def test_unique_valid_codes(self):
        out = codes.generate(20, seed=7)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 20)
        self.assertEqual(len(set(out)), 20)
        for code in out:
            self.assertTrue(codes.is_valid(code))

    def test_seed_determinism(self):
        self.assertEqual(
            codes.generate(10, seed=99), codes.generate(10, seed=99)
        )
        self.assertNotEqual(
            codes.generate(10, seed=1), codes.generate(10, seed=2)
        )

    def test_excludes_existing_and_zero(self):
        existing = set(codes.generate(10, seed=1))
        out = codes.generate(30, seed=2, existing=existing)
        self.assertEqual(len(out), 30)
        self.assertEqual(len(set(out)), 30)
        self.assertTrue(existing.isdisjoint(out))
        self.assertEqual(codes.generate(0, seed=3), [])

    def test_raises_when_space_too_small(self):
        # 30 ** 4 == 810000 < 4 * 300000, so generation must refuse.
        with self.assertRaises(getattr(codes, "ToolError")):
            codes.generate(300000, length=5)


class TestTrackingUrl(unittest.TestCase):
    def setUp(self):
        self.code = one_code(seed=12)
        self.base = "https://example.com/p"

    def test_basic_url_and_default_utm(self):
        url = codes.tracking_url(self.base, self.code)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "example.com")
        self.assertEqual(parts.path, "/p")
        params = query(url)
        self.assertEqual(params.get("c"), [self.code])
        self.assertEqual(params.get("utm_source"), ["direct_mail"])
        self.assertEqual(params.get("utm_medium"), ["mail"])

    def test_custom_param_and_utm(self):
        url = codes.tracking_url(
            self.base,
            self.code,
            param="ref",
            utm={
                "utm_source": "direct_mail",
                "utm_medium": "mail",
                "utm_campaign": "batch7",
            },
        )
        params = query(url)
        self.assertEqual(params.get("ref"), [self.code])
        self.assertNotIn("c", params)
        self.assertEqual(params.get("utm_campaign"), ["batch7"])
        self.assertEqual(params.get("utm_source"), ["direct_mail"])
        self.assertEqual(params.get("utm_medium"), ["mail"])

    def test_preserves_query_and_fragment(self):
        url = codes.tracking_url(
            "https://example.com/p?x=1&y=2#frag", self.code
        )
        params = query(url)
        self.assertEqual(params.get("x"), ["1"])
        self.assertEqual(params.get("y"), ["2"])
        self.assertEqual(params.get("c"), [self.code])
        self.assertEqual(urllib.parse.urlsplit(url).fragment, "frag")
        self.assertTrue(url.endswith("#frag"))

    def test_encodes_utm_values(self):
        url = codes.tracking_url(
            self.base,
            self.code,
            utm={
                "utm_source": "direct_mail",
                "utm_medium": "mail",
                "utm_campaign": "spring sale & more",
            },
        )
        self.assertEqual(
            query(url).get("utm_campaign"), ["spring sale & more"]
        )
        self.assertNotIn(" ", url)
        self.assertNotIn("&more", url)

    def test_rejects_insecure_or_hostless_base(self):
        for bad in (
            "http://example.com/p",
            "example.com/p",
            "https:///p",
            "",
            "ftp://example.com",
        ):
            with self.subTest(base=bad):
                with self.assertRaises(Exception):
                    codes.tracking_url(bad, self.code)


class TestAssign(unittest.TestCase):
    def setUp(self):
        self.base = "https://example.com/p"

    def test_adds_tracking_fields(self):
        pieces = [{"piece_id": 1}, {"piece_id": 2}]
        out = codes.assign(pieces, self.base, seed=11)
        self.assertIsInstance(out, list)
        self.assertIsNot(out, pieces)
        self.assertEqual(len(out), 2)
        assigned = []
        for piece in out:
            self.assertIn("tracking_code", piece)
            self.assertIn("tracking_url", piece)
            self.assertTrue(codes.is_valid(piece["tracking_code"]))
            self.assertEqual(
                codes.parse_code_from_url(piece["tracking_url"]),
                piece["tracking_code"],
            )
            assigned.append(piece["tracking_code"])
        self.assertEqual(len(set(assigned)), 2)

    def test_campaign_and_segment_utm(self):
        pieces = [{"segment": "vip"}, {}]
        out = codes.assign(
            pieces, self.base, seed=12, campaign="spring2024"
        )
        vip = query(out[0]["tracking_url"])
        plain = query(out[1]["tracking_url"])
        self.assertEqual(vip.get("utm_campaign"), ["spring2024"])
        self.assertEqual(vip.get("utm_content"), ["vip"])
        self.assertEqual(plain.get("utm_campaign"), ["spring2024"])
        self.assertNotIn("utm_content", plain)

    def test_keeps_valid_code_and_avoids_existing(self):
        pre = one_code(seed=21)
        existing = set(codes.generate(5, seed=31)) | {pre}
        pieces = [{"tracking_code": pre}] + [{} for _ in range(10)]
        out = codes.assign(
            pieces, self.base, existing=existing, seed=32
        )
        self.assertEqual(out[0]["tracking_code"], pre)
        generated = {piece["tracking_code"] for piece in out[1:]}
        self.assertEqual(len(generated), 10)
        self.assertTrue(existing.isdisjoint(generated))
        self.assertNotIn(pre, generated)


class TestParseCodeFromUrl(unittest.TestCase):
    def setUp(self):
        self.code = one_code(seed=41)
        self.base = "https://example.com/p"

    def test_roundtrip_and_custom_param(self):
        url = codes.tracking_url(self.base, self.code)
        self.assertEqual(codes.parse_code_from_url(url), self.code)
        # Param values are case-insensitive.
        self.assertEqual(codes.parse_code_from_url(url.lower()), self.code)
        ref_url = codes.tracking_url(self.base, self.code, param="ref")
        self.assertEqual(
            codes.parse_code_from_url(ref_url, param="ref"), self.code
        )
        self.assertIsNone(codes.parse_code_from_url(ref_url))

    def test_normalizes_separators_in_value(self):
        raw = (
            self.base
            + "?c="
            + self.code[:3].lower()
            + "-"
            + self.code[3:].lower()
        )
        self.assertEqual(codes.parse_code_from_url(raw), self.code)

    def test_invalid_urls_return_none(self):
        self.assertIsNone(codes.parse_code_from_url(self.base))
        self.assertIsNone(codes.parse_code_from_url(self.base + "?c="))
        self.assertIsNone(codes.parse_code_from_url(self.base + "?c=0O1ILU"))
        # Checksum-valid but below the minimum code length.
        self.assertIsNone(codes.parse_code_from_url(self.base + "?c=2222"))


if __name__ == '__main__':
    unittest.main()
