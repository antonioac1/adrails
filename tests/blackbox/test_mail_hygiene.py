import unittest

from adsops.mail.hygiene import (
    ToolError,
    classify_owner,
    clean,
    is_domestic,
    label_lines,
    normalize_address,
    suppression_stats,
)


class HygieneTests(unittest.TestCase):
    def _rec(
        self,
        name="Alice",
        line1="123 Main St",
        line2="",
        city="Tampa",
        state="FL",
        zip_code="33602",
        country=None,
        **extra
    ):
        rec = {
            "name": name,
            "address_line_1": line1,
            "address_line_2": line2,
            "city": city,
            "state": state,
            "zip": zip_code,
        }
        if country is not None:
            rec["country"] = country
        rec.update(extra)
        return rec

    def _stats_and_warning(self, result):
        if isinstance(result, tuple):
            self.assertEqual(len(result), 2)
            return result
        self.assertIsInstance(result, dict)
        stats = dict(result)
        warning = None
        for key in ("warning", "warnings"):
            if key in stats:
                warning = stats.pop(key)
                break
        return stats, warning

    def test_normalize_address_zip_and_key_abbreviations(self):
        rec = self._rec(
            name="Alice",
            line1="123 North Main St.",
            line2="Apartment 4",
            zip_code="33602-1234",
        )
        norm = normalize_address(rec)
        self.assertEqual(norm["line1"], "123 N MAIN ST")
        self.assertEqual(norm["line2"], "APT 4")
        self.assertEqual(norm["zip5"], "33602")
        self.assertEqual(norm["zip4"], "1234")
        self.assertEqual(norm["key"], "123 N MAIN ST|APT 4|33602")

    def test_normalize_address_leading_zero_for_eligible_state(self):
        rec = self._rec(state="MA", zip_code="2134")
        norm = normalize_address(rec)
        self.assertEqual(norm["zip5"], "02134")

    def test_normalize_address_hash_becomes_unit(self):
        rec = self._rec(line1="500 West Oak Road #7", line2="", zip_code="33602")
        norm = normalize_address(rec)
        self.assertEqual(norm["line1"], "500 W OAK RD UNIT 7")
        self.assertEqual(norm["key"], "500 W OAK RD UNIT 7||33602")

    def test_is_domestic_true_for_us_and_territories(self):
        cases = [
            ("FL", "33602", None),
            ("DC", "20001", "USA"),
            ("PR", "00901", "United States"),
            ("VI", "00801", "U.S.A."),
            ("GU", "96910", "US"),
            ("AA", "09001", None),
        ]
        for state, zip_code, country in cases:
            with self.subTest(state=state, country=country):
                rec = self._rec(state=state, zip_code=zip_code, country=country)
                self.assertTrue(is_domestic(rec))

    def test_is_domestic_false_for_foreign_invalid_state_or_zip(self):
        cases = [
            ("FL", "33602", "Canada"),
            ("ZZ", "33602", None),
            ("FL", "1234", "US"),
            ("CA", "2134", "US"),
            ("TX", "78701", "Mexico"),
        ]
        for state, zip_code, country in cases:
            with self.subTest(state=state, zip_code=zip_code, country=country):
                rec = self._rec(state=state, zip_code=zip_code, country=country)
                self.assertFalse(is_domestic(rec))

    def test_classify_owner_default_institutional_patterns(self):
        names = [
            "First National Bank",
            "Tricon Residential",
            "Progress Residential",
            "Homeowners Association",
            "County of Miami",
            "Springfield Church",
            "Invitation Homes",
            "American Homes",
            "FirstKey Homes",
            "Housing Authority",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(classify_owner(name))

    def test_classify_owner_does_not_deny_llc_or_trust(self):
        for name in ("Smith LLC", "Smith Family Trust", "John Smith"):
            with self.subTest(name=name):
                self.assertIsNone(classify_owner(name))

    def test_classify_owner_custom_patterns_and_case_insensitive(self):
        self.assertIsNotNone(classify_owner("tricon residential"))
        self.assertIsNotNone(classify_owner("Acme Properties", [r"\bACME\b"]))
        self.assertIsNone(classify_owner("Acme Properties"))
        self.assertIsNotNone(classify_owner("Smith Family Trust", [r"\bTRUST\b"]))

    def test_label_lines_basic_includes_zip4(self):
        rec = self._rec(
            name="Alice Smith",
            line1="123 Main Street",
            line2="Apt 4",
            zip_code="33602-1234",
        )
        lines = label_lines(rec, max_chars=40, max_lines=5)
        self.assertIsInstance(lines, list)
        self.assertTrue(any("ALICE SMITH" in line.upper() for line in lines))
        self.assertTrue(any("123 MAIN" in line.upper() for line in lines))
        self.assertTrue(any("APT 4" in line.upper() for line in lines))
        self.assertTrue(any("33602-1234" in line for line in lines))

    def test_label_lines_includes_country_for_foreign(self):
        rec = self._rec(
            name="Alice",
            line1="1 King St",
            city="Toronto",
            state="ON",
            zip_code="M5V 2T6",
            country="Canada",
        )
        lines = label_lines(rec)
        self.assertTrue(any("CANADA" in line.upper() for line in lines))

    def test_label_lines_wraps_without_truncating(self):
        rec = self._rec(
            name="Alice",
            line1="123 MAIN STREET WITH MANY WORDS IN IT",
        )
        lines = label_lines(rec, max_chars=12, max_lines=20)
        for line in lines:
            self.assertLessEqual(len(line), 12)
        self.assertIn("MANY", " ".join(lines).upper())

    def test_label_lines_raises_on_overflow(self):
        rec = self._rec(name="Alice", line1="123 MAIN STREET")
        with self.assertRaises(ToolError) as ctx:
            label_lines(rec, max_chars=10, max_lines=1)
        self.assertIn("label overflow", str(ctx.exception).lower())

    def test_clean_keeps_valid_and_adds_key_label(self):
        rec = self._rec()
        out = clean([rec])
        self.assertEqual(out["counts"]["input"], 1)
        self.assertEqual(out["counts"]["kept"], 1)
        self.assertEqual(len(out["kept"]), 1)
        self.assertEqual(out["dropped"], [])
        kept = out["kept"][0]
        self.assertIn("_key", kept)
        self.assertIn("_label", kept)
        self.assertEqual(kept["_key"], normalize_address(rec)["key"])

    def test_clean_drops_incomplete_address(self):
        rec = self._rec(city="")
        out = clean([rec])
        self.assertEqual(len(out["kept"]), 0)
        self.assertEqual(out["dropped"][0]["reason"], "incomplete_address")
        self.assertEqual(out["dropped"][0]["record_name"], rec["name"])
        self.assertEqual(out["counts"]["incomplete_address"], 1)

    def test_clean_drops_foreign_unless_allowed(self):
        rec = self._rec(country="Canada")
        out = clean([rec])
        self.assertEqual(out["dropped"][0]["reason"], "foreign")
        self.assertEqual(out["counts"]["foreign"], 1)

        out_allowed = clean([rec], allow_foreign=True)
        self.assertEqual(len(out_allowed["kept"]), 1)

    def test_clean_drops_invalid_zip(self):
        rec = self._rec(state="FL", zip_code="123", country="US")
        out = clean([rec])
        self.assertEqual(out["dropped"][0]["reason"], "invalid_zip")
        self.assertEqual(out["counts"]["invalid_zip"], 1)

    def test_clean_drops_institutional_owner(self):
        rec = self._rec(name="Tricon Residential")
        out = clean([rec])
        reason = out["dropped"][0]["reason"]
        self.assertTrue(reason.startswith("institutional_owner:"))
        self.assertTrue(
            any(
                key.startswith("institutional_owner:") and value == 1
                for key, value in out["counts"].items()
            )
        )

    def test_clean_suppresses_by_normalized_address(self):
        rec = self._rec(
            name="Alice",
            line1="123 Main Street",
            zip_code="33602-1234",
        )
        suppress_rec = self._rec(
            name="Someone Else",
            line1="123 MAIN ST",
            zip_code="33602",
        )
        out = clean([rec], suppress={"clients": [suppress_rec]})
        self.assertEqual(out["dropped"][0]["reason"], "suppressed:clients")
        self.assertEqual(out["counts"]["suppressed:clients"], 1)

    def test_clean_suppresses_by_normalized_name_and_zip(self):
        rec = self._rec(
            name="Bob Jones",
            line1="999 Other St",
            zip_code="33602-9999",
        )
        suppress_rec = self._rec(
            name="BOB JONES",
            line1="123 Main St",
            zip_code="33602",
        )
        out = clean([rec], suppress={"leads": [suppress_rec]})
        self.assertEqual(out["dropped"][0]["reason"], "suppressed:leads")

    def test_clean_drops_own_property(self):
        rec = self._rec()
        key = normalize_address(rec)["key"]
        out = clean([rec], own_addresses=(key,))
        self.assertEqual(out["dropped"][0]["reason"], "own_property")
        self.assertEqual(out["counts"]["own_property"], 1)

    def test_clean_dedupes_by_address(self):
        rec1 = self._rec(name="Alice", line1="123 Main St")
        rec2 = self._rec(name="Bob", line1="123 Main Street")
        out = clean([rec1, rec2], dedupe="address")
        self.assertEqual(len(out["kept"]), 1)
        self.assertEqual(out["dropped"][0]["reason"], "duplicate")
        self.assertEqual(out["counts"]["duplicate"], 1)

    def test_clean_dedupe_none_keeps_duplicates(self):
        rec1 = self._rec(name="Alice", line1="123 Main St")
        rec2 = self._rec(name="Bob", line1="123 Main Street")
        out = clean([rec1, rec2], dedupe=None)
        self.assertEqual(len(out["kept"]), 2)
        self.assertFalse(any(d["reason"] == "duplicate" for d in out["dropped"]))

    def test_clean_dedupes_by_name_and_address(self):
        rec1 = self._rec(name="Same", line1="123 Main St")
        rec2 = self._rec(name="Same", line1="123 Main Street")
        rec3 = self._rec(name="Different", line1="123 Main St")
        out = clean([rec1, rec2, rec3], dedupe="name+address")
        self.assertEqual(len(out["kept"]), 2)
        self.assertEqual(out["counts"]["duplicate"], 1)

    def test_clean_drops_label_overflow(self):
        rec = self._rec(line1=" ".join(["WORD"] * 100))
        out = clean([rec])
        self.assertEqual(out["dropped"][0]["reason"], "label_overflow")
        self.assertEqual(out["counts"]["label_overflow"], 1)

    def test_suppression_stats_counts_and_warns_on_empty_list(self):
        rec = self._rec()
        rec2 = self._rec(name="Bob", line1="456 Oak Ave", zip_code="33603")
        suppress = {
            "clients": [],
            "leads": [rec],
            "do_not_mail": [rec, rec2],
        }
        stats, warning = self._stats_and_warning(suppression_stats(suppress))
        self.assertEqual(stats["clients"], 0)
        self.assertEqual(stats["leads"], 1)
        self.assertEqual(stats["do_not_mail"], 2)
        self.assertIsInstance(warning, str)
        self.assertTrue(warning)

    def test_suppression_stats_no_warning_when_all_lists_nonempty(self):
        rec = self._rec()
        suppress = {
            "clients": [rec],
            "leads": [rec],
            "do_not_mail": [rec],
        }
        stats, warning = self._stats_and_warning(suppression_stats(suppress))
        self.assertEqual(stats["clients"], 1)
        self.assertEqual(stats["leads"], 1)
        self.assertEqual(stats["do_not_mail"], 1)
        self.assertFalse(warning)


if __name__ == "__main__":
    unittest.main()
