import os
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib

from adsops.mail import qr


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _tool_error_type():
    """Best-effort lookup of the documented ToolError type."""
    exc = getattr(qr, "ToolError", None)
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc
    try:
        from adsops.errors import ToolError as TE
        return TE
    except Exception:
        return Exception


def _png_chunks(data):
    assert data.startswith(PNG_SIG)
    pos = 8
    chunks = []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        cdata = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        chunks.append((ctype, cdata, crc))
        pos += 12 + length
    assert pos == len(data)
    return chunks


class TestQrAgainstSpec(unittest.TestCase):
    def test_public_api_present(self):
        self.assertTrue(callable(qr.encode))
        self.assertTrue(callable(qr.to_svg))
        self.assertTrue(callable(qr.to_png))
        self.assertTrue(callable(qr.to_matrix))

    def test_encode_returns_documented_attributes(self):
        q = qr.encode("HELLO", ecc="M", version=1, mask=0)
        self.assertEqual(q.version, 1)
        self.assertEqual(q.ecc, "M")
        self.assertEqual(q.mask, 0)
        self.assertEqual(q.size, 21)
        self.assertIsInstance(q.modules, list)
        self.assertEqual(len(q.modules), 21)
        for row in q.modules:
            self.assertEqual(len(row), 21)

    def test_default_ecc_is_m(self):
        q = qr.encode("x")
        self.assertEqual(q.ecc, "M")

    def test_auto_version_chooses_smallest_that_fits(self):
        q14 = qr.encode("a" * 14, ecc="M", mask=0)
        q15 = qr.encode("a" * 15, ecc="M", mask=0)
        self.assertEqual(q14.version, 1)
        self.assertEqual(q15.version, 2)

    def test_explicit_version_is_used(self):
        q = qr.encode("x", ecc="M", version=3, mask=0)
        self.assertEqual(q.version, 3)
        self.assertEqual(q.size, 29)

    def test_size_formula_for_versions_1_2_7_10(self):
        for version in (1, 2, 7, 10):
            q = qr.encode("x", ecc="M", version=version, mask=0)
            self.assertEqual(q.size, 17 + 4 * version)
            self.assertEqual(len(q.modules), q.size)

    def test_all_ecc_levels_accepted(self):
        for ecc in ("L", "M", "Q", "H"):
            q = qr.encode("x", ecc=ecc, version=1, mask=0)
            self.assertEqual(q.ecc, ecc)

    def test_v1_m_byte_capacity_boundary(self):
        q = qr.encode("a" * 14, ecc="M", version=1, mask=0)
        self.assertEqual(q.version, 1)
        with self.assertRaises(_tool_error_type()):
            qr.encode("a" * 15, ecc="M", version=1, mask=0)

    def test_v10_m_byte_capacity_boundary(self):
        q = qr.encode("a" * 213, ecc="M", version=10, mask=0)
        self.assertEqual(q.version, 10)
        self.assertEqual(q.size, 57)
        with self.assertRaises(_tool_error_type()):
            qr.encode("a" * 214, ecc="M", version=10, mask=0)

    def test_auto_version_too_long_for_v10_raises(self):
        with self.assertRaises(_tool_error_type()):
            qr.encode("a" * 214, ecc="M", mask=0)

    def test_utf8_byte_mode_capacity(self):
        # "é" is 2 bytes in UTF-8; v1-M holds 14 bytes.
        q = qr.encode("é" * 7, ecc="M", version=1, mask=0)
        self.assertEqual(q.version, 1)
        with self.assertRaises(_tool_error_type()):
            qr.encode("é" * 8, ecc="M", version=1, mask=0)

    def test_explicit_masks_0_through_7(self):
        for mask in range(8):
            q = qr.encode("mask", ecc="M", version=1, mask=mask)
            self.assertEqual(q.mask, mask)

    def test_auto_mask_is_in_range(self):
        q = qr.encode("mask", ecc="M", version=1, mask=None)
        self.assertIn(q.mask, range(8))

    def test_modules_are_booleans(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        for row in q.modules:
            for value in row:
                self.assertIsInstance(value, bool)

    def test_dark_module_position(self):
        for version in (1, 7, 10):
            q = qr.encode("x", ecc="M", version=version, mask=0)
            self.assertTrue(q.modules[4 * version + 9][8])

    def test_top_left_finder_pattern(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        pattern = [
            [1, 1, 1, 1, 1, 1, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 1, 1, 1, 0, 1],
            [1, 0, 0, 0, 0, 0, 1],
            [1, 1, 1, 1, 1, 1, 1],
        ]
        for r in range(7):
            for c in range(7):
                self.assertEqual(q.modules[r][c], bool(pattern[r][c]))

    def test_top_left_separators_are_light(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        for i in range(8):
            self.assertFalse(q.modules[7][i])
            self.assertFalse(q.modules[i][7])

    def test_timing_patterns_alternate(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        for c in range(8, q.size - 8):
            self.assertEqual(q.modules[6][c], ((c - 8) % 2 == 0))
        for r in range(8, q.size - 8):
            self.assertEqual(q.modules[r][6], ((r - 8) % 2 == 0))

    def test_format_info_for_m_mask_0(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        bits = 0x5412  # 101010000010010, quoted in the spec

        def bit(i):
            return bool((bits >> i) & 1)

        # Copy 1 around the top-left finder.
        coords = [
            (0, 8), (1, 8), (2, 8), (3, 8), (4, 8), (5, 8),
            (7, 8), (8, 8), (8, 7), (8, 5), (8, 4), (8, 3),
            (8, 2), (8, 1), (8, 0),
        ]
        for i, (r, c) in enumerate(coords):
            self.assertEqual(q.modules[r][c], bit(i))

        # Copy 2 split across bottom-left and top-right.
        for i in range(8):
            self.assertEqual(q.modules[8][q.size - 1 - i], bit(i))
        for i in range(8, 15):
            self.assertEqual(q.modules[q.size - 15 + i][8], bit(i))

    def test_version_info_for_v7(self):
        q = qr.encode("x", ecc="M", version=7, mask=0)
        bits = 0x07C94  # version 7 version-information bits

        for i in range(18):
            expected = bool((bits >> i) & 1)
            top_r = i // 3
            top_c = q.size - 11 + (i % 3)
            self.assertEqual(q.modules[top_r][top_c], expected)

            bot_r = q.size - 11 + (i % 3)
            bot_c = i // 3
            self.assertEqual(q.modules[bot_r][bot_c], expected)

    def test_to_matrix_adds_quiet_zone_and_preserves_modules(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        border = 4
        m = qr.to_matrix(q, border=border)
        n = q.size + 2 * border
        self.assertEqual(len(m), n)
        for row in m:
            self.assertEqual(len(row), n)
            for value in row:
                self.assertIn(value, (0, 1))

        for i in range(n):
            self.assertEqual(m[0][i], 0)
            self.assertEqual(m[-1][i], 0)
            self.assertEqual(m[i][0], 0)
            self.assertEqual(m[i][-1], 0)

        for r in range(q.size):
            for c in range(q.size):
                expected = 1 if q.modules[r][c] else 0
                self.assertEqual(m[r + border][c + border], expected)

    def test_to_matrix_zero_border(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        m = qr.to_matrix(q, border=0)
        self.assertEqual(len(m), q.size)
        for r in range(q.size):
            self.assertEqual(len(m[r]), q.size)
            for c in range(q.size):
                expected = 1 if q.modules[r][c] else 0
                self.assertEqual(m[r][c], expected)

    def test_to_svg_is_well_formed_and_has_single_path(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        svg = qr.to_svg(q, scale=3, border=2)
        self.assertIsInstance(svg, str)
        self.assertEqual(svg.count("<path"), 1)
        self.assertIn("crispEdges", svg)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".svg", delete=False, encoding="utf-8"
        ) as f:
            f.write(svg)
            name = f.name
        try:
            root = ET.parse(name).getroot()
            self.assertTrue(root.tag.endswith("svg"))
        finally:
            os.unlink(name)

    def test_to_png_header_and_ihdr(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        png = qr.to_png(q, scale=2, border=1)
        self.assertIsInstance(png, bytes)
        self.assertTrue(png.startswith(PNG_SIG))

        chunks = _png_chunks(png)
        self.assertEqual(chunks[0][0], b"IHDR")
        self.assertEqual(chunks[-1][0], b"IEND")

        width, height, bit_depth, color_type, comp, filt, interlace = (
            struct.unpack(">IIBBBBB", chunks[0][1])
        )
        self.assertEqual(width, height)
        self.assertGreater(width, 0)
        self.assertEqual(bit_depth, 8)
        self.assertEqual(color_type, 0)  # 8-bit grayscale
        self.assertEqual(comp, 0)
        self.assertEqual(filt, 0)
        self.assertEqual(interlace, 0)

    def test_to_png_chunk_crcs_are_valid(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        png = qr.to_png(q)
        for ctype, cdata, crc in _png_chunks(png):
            expected = zlib.crc32(ctype + cdata) & 0xFFFFFFFF
            self.assertEqual(crc, expected)

    def test_to_png_idat_decompresses_to_scanline_buffer(self):
        q = qr.encode("x", ecc="M", version=1, mask=0)
        png = qr.to_png(q, scale=2, border=1)
        chunks = _png_chunks(png)

        width, height, _, _, _, _, _ = struct.unpack(">IIBBBBB", chunks[0][1])
        idat = b"".join(cdata for ctype, cdata, _ in chunks if ctype == b"IDAT")
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), height * (width + 1))

    def test_empty_string_uses_v1(self):
        q = qr.encode("", ecc="M", mask=0)
        self.assertEqual(q.version, 1)
        self.assertEqual(q.size, 21)

    def test_selftest_runs(self):
        selftest = getattr(qr, "selftest", None)
        if selftest is None:
            self.skipTest("selftest not exposed")
        result = selftest()
        if isinstance(result, bool):
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
