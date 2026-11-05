import csv
import io
import re
import unittest
from unittest.mock import Mock

from adsops.mail import labels


DEFAULT_COLUMNS = [
    "name",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "zip",
    "country",
    "tracking_code",
    "tracking_url",
    "segment",
    "variant",
    "batch",
]

WATERMARK = "SAMPLE - DO NOT MAIL"


def make_piece(**overrides):
    piece = {
        "name": "Alice Smith",
        "address_line_1": "123 Main St",
        "address_line_2": "Apt 4",
        "city": "Springfield",
        "state": "IL",
        "zip": "62704",
        "country": "USA",
        "tracking_code": "ABC1234",
        "tracking_url": "https://example.com/t/ABC1234",
        "segment": "segment-a",
        "variant": "variant-a",
        "batch": "batch-a",
    }
    piece.update(overrides)
    return piece


def _num_re(value):
    """Regex fragment matching a numeric inch value with optional 0 and trailing zeros."""
    v = float(value)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v))) + r"(?:\.0+)?"

    s = f"{v:.6f}".rstrip("0").rstrip(".")
    if s.startswith("0."):
        s = s[1:]
        return r"(?:0)?" + re.escape(s) + r"0*"
    return re.escape(s) + r"0*"


def _assert_css_inches(testcase, html, prop, value):
    pattern = (
        r"(?<![-\w])"
        + re.escape(prop)
        + r"\s*:\s*"
        + _num_re(value)
        + r"in\b"
    )
    testcase.assertRegex(html, pattern)


def _visible_text(html):
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def _style_near_text(html, text):
    """Return the last style attribute value before the given text, for font checks."""
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    idx = html.find(text)
    if idx < 0:
        return ""
    window = html[max(0, idx - 1500):idx]
    styles = re.findall(r'style\s*=\s*["\']([^"\']*)["\']', window, flags=re.I)
    return styles[-1] if styles else ""


class TestTemplates(unittest.TestCase):
    def test_templates_dict_has_per_sheet_counts(self):
        expected = {
            "avery_5160": 30,
            "avery_5163": 10,
            "avery_5164": 6,
            "avery_5167": 80,
        }

        for name, per_sheet in expected.items():
            with self.subTest(template=name):
                self.assertIn(name, labels.TEMPLATES)
                self.assertEqual(labels.TEMPLATES[name]["per_sheet"], per_sheet)

    def test_sheet_html_geometry_for_each_template(self):
        expected = [
            ("avery_5160", 0.1875, 0.5, 2.625, 1.0),
            ("avery_5163", 0.15625, 0.5, 4.0, 2.0),
            ("avery_5164", 0.15625, 0.5, 4.0, 3.333),
            ("avery_5167", 0.28125, 0.5, 1.75, 0.5),
        ]

        for template, left, top, width, height in expected:
            with self.subTest(template=template):
                html = labels.sheet_html(
                    [make_piece(_label=["Only"])],
                    template=template,
                    sample=False,
                )
                _assert_css_inches(self, html, "left", left)
                _assert_css_inches(self, html, "top", top)
                _assert_css_inches(self, html, "width", width)
                _assert_css_inches(self, html, "height", height)

    def test_sheet_html_uses_horizontal_pitch_for_second_label(self):
        cases = [
            ("avery_5160", 0.1875 + 2.75),
            ("avery_5163", 0.15625 + 4.1875),
            ("avery_5164", 0.15625 + 4.1875),
            ("avery_5167", 0.28125 + 2.0625),
        ]

        for template, second_left in cases:
            with self.subTest(template=template):
                html = labels.sheet_html(
                    [make_piece(_label=["A"]), make_piece(_label=["B"])],
                    template=template,
                    sample=False,
                )
                _assert_css_inches(self, html, "left", second_left)

    def test_sheet_html_uses_vertical_pitch(self):
        pieces = [make_piece(_label=[f"P{i}"]) for i in range(4)]
        html = labels.sheet_html(pieces, template="avery_5160", sample=False)
        _assert_css_inches(self, html, "top", 1.5)
        _assert_css_inches(self, html, "left", 0.1875)


class TestSheetHTML(unittest.TestCase):
    def test_returns_string_with_page_and_sheet_setup(self):
        html = labels.sheet_html([make_piece()], sample=False)
        self.assertIsInstance(html, str)
        self.assertRegex(html, r"@page\s*\{[^}]*\bsize\s*:\s*letter\b")
        self.assertRegex(html, r"@page\s*\{[^}]*\bmargin\s*:\s*0\b")
        self.assertRegex(html, r"width\s*:\s*8\.5in")
        self.assertRegex(html, r"height\s*:\s*11in")
        self.assertIn("page-break-after", html)

    def test_label_text_built_from_name_and_address_fields(self):
        piece = make_piece(
            name="Alice Smith",
            address_line_1="123 Main St",
            address_line_2="Apt 4",
            city="Springfield",
            state="IL",
            zip="62704",
            country="USA",
        )
        html = labels.sheet_html([piece], sample=False)

        for text in [
            "Alice Smith",
            "123 Main St",
            "Apt 4",
            "Springfield",
            "IL",
            "62704",
            "USA",
        ]:
            self.assertIn(text, html)

    def test_label_override_replaces_built_lines(self):
        piece = make_piece(
            name="Ignored Name",
            _label=["Custom line one", "Custom line two"],
        )
        html = labels.sheet_html([piece], sample=False)

        self.assertIn("Custom line one", html)
        self.assertIn("Custom line two", html)
        self.assertNotIn("Ignored Name", html)

    def test_all_text_is_html_escaped(self):
        piece = make_piece(
            name="A&B <C>",
            address_line_1="1 <Street>",
            tracking_code="<CODE>",
        )
        html = labels.sheet_html([piece], sample=False, show_code=True)

        self.assertIn("A&amp;B &lt;C&gt;", html)
        self.assertIn("1 &lt;Street&gt;", html)
        self.assertIn("Code &lt;CODE&gt;", html)
        self.assertNotIn("A&B <C>", html)

    def test_title_is_html_escaped(self):
        html = labels.sheet_html(
            [make_piece()],
            title='<Title & "stuff">',
            sample=False,
        )
        self.assertIn("&lt;Title &amp;", html)
        self.assertNotIn('<Title & "stuff">', html)

    def test_show_code_toggle(self):
        piece = make_piece(tracking_code="TRK999")

        with_code = labels.sheet_html([piece], show_code=True, sample=False)
        without_code = labels.sheet_html([piece], show_code=False, sample=False)

        self.assertIn("Code TRK999", with_code)
        self.assertNotIn("Code TRK999", without_code)

    def test_font_shrinks_for_line_longer_than_34_chars(self):
        short = "x" * 34
        long = "x" * 35

        html_short = labels.sheet_html(
            [make_piece(_label=[short])],
            font_pt=9,
            sample=False,
        )
        html_long = labels.sheet_html(
            [make_piece(_label=[long])],
            font_pt=9,
            sample=False,
        )

        self.assertRegex(
            _style_near_text(html_short, short),
            r"font-size\s*:\s*9(?:\.0)?pt",
        )
        self.assertRegex(
            _style_near_text(html_long, long),
            r"font-size\s*:\s*8(?:\.0)?pt",
        )

    def test_font_shrinks_for_more_than_four_lines(self):
        four_lines = ["one", "two", "three", "four"]
        five_lines = ["one", "two", "three", "four", "five"]

        html_four = labels.sheet_html(
            [make_piece(_label=four_lines)],
            font_pt=9,
            sample=False,
        )
        html_five = labels.sheet_html(
            [make_piece(_label=five_lines)],
            font_pt=9,
            sample=False,
        )

        self.assertRegex(
            _style_near_text(html_four, "one"),
            r"font-size\s*:\s*9(?:\.0)?pt",
        )
        self.assertRegex(
            _style_near_text(html_five, "five"),
            r"font-size\s*:\s*8(?:\.0)?pt",
        )

    def test_font_never_shrinks_below_six(self):
        long = "x" * 40
        html = labels.sheet_html(
            [make_piece(_label=[long])],
            font_pt=6,
            sample=False,
        )

        self.assertRegex(
            _style_near_text(html, long),
            r"font-size\s*:\s*6(?:\.0)?pt",
        )
        self.assertNotRegex(html, r"font-size\s*:\s*5(?:\.0)?pt")

    def test_label_css_allows_wrapping_and_overflow(self):
        html = labels.sheet_html(
            [make_piece(_label=["A"])],
            sample=False,
        )
        self.assertRegex(html, r"white-space\s*:\s*normal")
        self.assertRegex(html, r"overflow\s*:\s*visible")

    def test_qr_embedded_for_tall_template(self):
        qr = Mock(return_value='<svg id="qr-test">X</svg>')
        piece = make_piece(tracking_url="https://example.com/qr")

        html = labels.sheet_html(
            [piece],
            template="avery_5163",
            qr_svg=qr,
            sample=False,
        )

        self.assertIn('<svg id="qr-test">X</svg>', html)
        qr.assert_called()

    def test_qr_embedded_for_wide_5160_with_0_8in_size(self):
        qr = Mock(return_value='<svg id="qr-test-wide">X</svg>')
        piece = make_piece(tracking_url="https://example.com/qr")

        html = labels.sheet_html(
            [piece],
            template="avery_5160",
            qr_svg=qr,
            sample=False,
        )

        self.assertIn('<svg id="qr-test-wide">X</svg>', html)
        self.assertRegex(html, _num_re(0.8) + r"in")
        qr.assert_called()

    def test_qr_not_embedded_for_too_small_template(self):
        qr = Mock(return_value='<svg id="qr-test-small">X</svg>')
        piece = make_piece(tracking_url="https://example.com/qr")

        html = labels.sheet_html(
            [piece],
            template="avery_5167",
            qr_svg=qr,
            sample=False,
        )

        self.assertNotIn('<svg id="qr-test-small">X</svg>', html)

    def test_sample_watermark_is_default(self):
        pieces = [make_piece(_label=["A"]), make_piece(_label=["B"])]
        html = labels.sheet_html(pieces)

        self.assertIn(WATERMARK, html)

    def test_sample_false_removes_watermark(self):
        pieces = [make_piece(_label=["A"]), make_piece(_label=["B"])]
        html = labels.sheet_html(pieces, sample=False)

        self.assertNotIn(WATERMARK, html)

    def test_empty_pieces_render_one_blank_sheet_with_note(self):
        html = labels.sheet_html([], sample=False)

        self.assertRegex(html, r"width\s*:\s*8\.5in")
        self.assertRegex(html, r"height\s*:\s*11in")
        self.assertIn("page-break-after", html)

        text = _visible_text(html)
        self.assertTrue(text.strip())
        self.assertRegex(
            text,
            r"(?i)(empty|no\s+labels|no\s+pieces|nothing|blank)",
        )

    def test_many_pieces_include_all_labels_and_page_break(self):
        pieces = [make_piece(_label=[f"Label {i}"]) for i in range(31)]
        html = labels.sheet_html(pieces, sample=False)

        self.assertIn("Label 0", html)
        self.assertIn("Label 30", html)
        self.assertIn("page-break-after", html)


class TestVendorCSV(unittest.TestCase):
    def test_vendor_csv_default_columns_and_values(self):
        piece = make_piece(
            name="Alice",
            city="Springfield",
            zip="62704",
        )
        text = labels.vendor_csv([piece])
        reader = csv.DictReader(io.StringIO(text))

        self.assertEqual(reader.fieldnames, DEFAULT_COLUMNS)
        rows = list(reader)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Alice")
        self.assertEqual(row["city"], "Springfield")
        self.assertEqual(row["zip"], "62704")
        self.assertEqual(row["tracking_code"], "ABC1234")

    def test_vendor_csv_custom_columns(self):
        piece = make_piece(name="Alice", zip="62704")
        text = labels.vendor_csv([piece], columns=["zip", "name"])
        reader = csv.DictReader(io.StringIO(text))

        self.assertEqual(reader.fieldnames, ["zip", "name"])
        row = next(reader)
        self.assertEqual(row["zip"], "62704")
        self.assertEqual(row["name"], "Alice")

    def test_vendor_csv_guards_formula_injection(self):
        for value in ["=SUM(A1)", "+cmd", "-2+3", "@foo"]:
            with self.subTest(value=value):
                piece = make_piece(name=value)
                text = labels.vendor_csv([piece], columns=["name"])
                row = next(csv.DictReader(io.StringIO(text)))
                self.assertEqual(row["name"], "'" + value)

    def test_vendor_csv_injection_guard_applies_to_other_columns(self):
        piece = make_piece(city="=HYPERLINK(...)")
        text = labels.vendor_csv([piece], columns=["city"])
        row = next(csv.DictReader(io.StringIO(text)))
        self.assertEqual(row["city"], "'=HYPERLINK(...)")

    def test_vendor_csv_does_not_prefix_safe_values(self):
        piece = make_piece(name="Alice")
        text = labels.vendor_csv([piece], columns=["name"])
        row = next(csv.DictReader(io.StringIO(text)))
        self.assertEqual(row["name"], "Alice")


class TestCounts(unittest.TestCase):
    def test_counts_empty_pieces_does_not_crash(self):
        result = labels.counts([], "avery_5160")

        self.assertEqual(
            set(result),
            {"pieces", "sheets", "labels_last_sheet"},
        )
        self.assertEqual(result["pieces"], 0)
        self.assertIsInstance(result["sheets"], int)
        self.assertIsInstance(result["labels_last_sheet"], int)

    def test_counts_partial_full_and_overflow_sheets(self):
        cases = [
            (1, 1, 1),
            (29, 1, 29),
            (30, 1, 30),
            (31, 2, 1),
            (60, 2, 30),
            (61, 3, 1),
        ]

        for n, sheets, last in cases:
            with self.subTest(n=n):
                pieces = [make_piece() for _ in range(n)]
                result = labels.counts(pieces, "avery_5160")
                self.assertEqual(
                    result,
                    {
                        "pieces": n,
                        "sheets": sheets,
                        "labels_last_sheet": last,
                    },
                )

    def test_counts_with_other_template(self):
        pieces = [make_piece() for _ in range(11)]
        result = labels.counts(pieces, "avery_5163")

        self.assertEqual(
            result,
            {
                "pieces": 11,
                "sheets": 2,
                "labels_last_sheet": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()