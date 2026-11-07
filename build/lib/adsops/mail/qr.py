"""QR Code encoder (ISO/IEC 18004) for AdRails mail-piece tracking URLs.

Pure stdlib, BYTE mode (UTF-8), versions 1-10, ECC levels L/M/Q/H.
The encoder is deterministic and side-effect free except through injected
arguments (none here).  It builds the complete module matrix, selects the
lowest-penalty mask, and renders SVG or PNG bytes without file I/O.

Tools should call `encode(data, ecc="M", version=None, mask=None)` and then
`to_svg` / `to_png` / `to_matrix`.  All output is JSON-serialisable except the
QR object itself and the PNG bytes (bytes are not JSON, return base64 if needed).
"""

import struct
import zlib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

from adsops.errors import ToolError


# ---------------------------------------------------------------------------
# Small nested-get helper (kept for consistency with other adsops modules).
# ---------------------------------------------------------------------------
def _g(row, *path, default=None):
    cur = row
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


# ---------------------------------------------------------------------------
# GF(256) arithmetic, primitive polynomial 0x11D
# ---------------------------------------------------------------------------
GF_EXP = [0] * 512
GF_LOG = [0] * 256


def _init_gf():
    x = 1
    for i in range(255):
        GF_EXP[i] = x
        GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        GF_EXP[i] = GF_EXP[i - 255]


_init_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def _rs_generator(n):
    """Return generator polynomial coefficients, highest degree first."""
    g = [1]
    for i in range(n):
        ai = GF_EXP[i]  # a^i with a = 2
        new = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            new[j] ^= c
            new[j + 1] ^= _gf_mul(c, ai)
        g = new
    return g


def _rs_remainder(data, n):
    """Remainder of data polynomial divided by generator for n EC codewords."""
    g = _rs_generator(n)
    rem = [0] * n
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(n):
            rem[i] ^= _gf_mul(g[i + 1], factor)
    return rem


# ---------------------------------------------------------------------------
# Standard tables (versions 1-10)
# ---------------------------------------------------------------------------
# (ec_per_block, num_blocks1, data_per_block1, num_blocks2, data_per_block2)
BLOCKS = {
    1: {"L": (7, 1, 19, 0, 0), "M": (10, 1, 16, 0, 0), "Q": (13, 1, 13, 0, 0), "H": (17, 1, 9, 0, 0)},
    2: {"L": (10, 1, 34, 0, 0), "M": (16, 1, 28, 0, 0), "Q": (22, 1, 22, 0, 0), "H": (28, 1, 16, 0, 0)},
    3: {"L": (15, 1, 55, 0, 0), "M": (26, 1, 44, 0, 0), "Q": (18, 2, 17, 0, 0), "H": (22, 2, 13, 0, 0)},
    4: {"L": (20, 1, 80, 0, 0), "M": (18, 2, 32, 0, 0), "Q": (26, 2, 24, 0, 0), "H": (16, 4, 9, 0, 0)},
    5: {"L": (26, 1, 108, 0, 0), "M": (24, 2, 43, 0, 0), "Q": (18, 2, 15, 2, 16), "H": (22, 2, 11, 2, 12)},
    6: {"L": (18, 2, 68, 0, 0), "M": (16, 4, 27, 0, 0), "Q": (24, 4, 19, 0, 0), "H": (28, 4, 15, 0, 0)},
    7: {"L": (20, 2, 78, 0, 0), "M": (18, 4, 31, 0, 0), "Q": (18, 2, 14, 4, 15), "H": (26, 4, 13, 1, 14)},
    8: {"L": (24, 2, 97, 0, 0), "M": (22, 2, 38, 2, 39), "Q": (22, 4, 18, 2, 19), "H": (26, 4, 14, 2, 15)},
    9: {"L": (30, 2, 116, 0, 0), "M": (22, 3, 36, 2, 37), "Q": (20, 4, 16, 4, 17), "H": (24, 4, 12, 4, 13)},
    10: {"L": (18, 2, 68, 2, 69), "M": (26, 4, 43, 1, 44), "Q": (24, 6, 19, 2, 20), "H": (28, 6, 15, 2, 16)},
}

REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

MASK_FUNCS = [
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
]


# ---------------------------------------------------------------------------
# Capacity / bit stream helpers
# ---------------------------------------------------------------------------
def _block_info(version, ecc):
    return BLOCKS[version][ecc]


def _data_codewords(version, ecc):
    ec, n1, d1, n2, d2 = _block_info(version, ecc)
    return n1 * d1 + n2 * d2


def _capacity_bytes(version, ecc):
    data_cw = _data_codewords(version, ecc)
    cc_bits = 8 if version <= 9 else 16
    return (data_cw * 8 - 4 - cc_bits) // 8


def _bits_from_value(val, n):
    return [(val >> i) & 1 for i in range(n - 1, -1, -1)]


# ---------------------------------------------------------------------------
# BCH / format / version information
# ---------------------------------------------------------------------------
def _format_bits(ecc, mask):
    ecc_val = {"L": 1, "M": 0, "Q": 3, "H": 2}[ecc]
    data = (ecc_val << 3) | mask
    rem = data << 10
    for i in range(4, -1, -1):
        if rem & (1 << (10 + i)):
            rem ^= 0x537 << i
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version):
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (12 + i)):
            rem ^= 0x1F25 << i
    return (version << 12) | rem


# ---------------------------------------------------------------------------
# Function pattern drawing
# ---------------------------------------------------------------------------
def _draw_finder(mods, func, r0, c0):
    r_start = r0 if r0 == 0 else r0 - 1
    c_start = c0 if c0 == 0 else c0 - 1
    for dr in range(8):
        for dc in range(8):
            mods[r_start + dr][c_start + dc] = False
            func[r_start + dr][c_start + dc] = True
    for dr in range(7):
        for dc in range(7):
            dark = (dr in (0, 6) or dc in (0, 6)) or (2 <= dr <= 4 and 2 <= dc <= 4)
            mods[r0 + dr][c0 + dc] = dark


def _draw_timing(mods, func, size):
    for c in range(8, size - 8):
        mods[6][c] = (c % 2 == 0)
        func[6][c] = True
    for r in range(8, size - 8):
        mods[r][6] = (r % 2 == 0)
        func[r][6] = True


def _draw_alignment(mods, func, version, size):
    positions = ALIGN.get(version, [])
    for r in positions:
        for c in positions:
            if (r == 6 and c == 6) or (r == 6 and c == size - 7) or (r == size - 7 and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    rr, cc = r + dr, c + dc
                    dark = (abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0))
                    mods[rr][cc] = dark
                    func[rr][cc] = True


def _reserve_format(func, size):
    for i in range(6):
        func[i][8] = True
    func[7][8] = True
    func[8][8] = True
    func[8][7] = True
    for i in range(9, 15):
        func[8][14 - i] = True
    for i in range(8):
        func[8][size - 1 - i] = True
    for i in range(8, 15):
        func[size - 15 + i][8] = True


def _draw_format_bits(mods, func, size, ecc, mask):
    bits = _format_bits(ecc, mask)

    def setf(r, c, val):
        mods[r][c] = bool(val)
        func[r][c] = True

    for i in range(6):
        setf(i, 8, (bits >> i) & 1)
    setf(7, 8, (bits >> 6) & 1)
    setf(8, 8, (bits >> 7) & 1)
    setf(8, 7, (bits >> 8) & 1)
    for i in range(9, 15):
        setf(8, 14 - i, (bits >> i) & 1)
    for i in range(8):
        setf(8, size - 1 - i, (bits >> i) & 1)
    for i in range(8, 15):
        setf(size - 15 + i, 8, (bits >> i) & 1)
    setf(size - 8, 8, 1)  # dark module


def _draw_version_bits(mods, func, version, size):
    if version < 7:
        return
    bits = _version_bits(version)
    for i in range(18):
        bit = (bits >> i) & 1
        a = size - 11 + i % 3
        b = i // 3
        mods[b][a] = bool(bit)
        func[b][a] = True
        mods[a][b] = bool(bit)
        func[a][b] = True


# ---------------------------------------------------------------------------
# Penalty scoring
# ---------------------------------------------------------------------------
def _penalty(mods):
    size = len(mods)
    score = 0

    # N1: runs of 5+ same colour
    for r in range(size):
        run = 1
        for c in range(1, size):
            if mods[r][c] == mods[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(size):
        run = 1
        for r in range(1, size):
            if mods[r][c] == mods[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # N2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            if mods[r][c] == mods[r][c + 1] == mods[r + 1][c] == mods[r + 1][c + 1]:
                score += 3

    # N3: finder-like pattern with 4 light modules on either side
    for r in range(size):
        row = "".join("1" if mods[r][c] else "0" for c in range(size))
        score += 40 * (row.count("10111010000") + row.count("00001011101"))
    for c in range(size):
        col = "".join("1" if mods[r][c] else "0" for r in range(size))
        score += 40 * (col.count("10111010000") + col.count("00001011101"))

    # N4: dark/light balance
    dark = sum(sum(row) for row in mods)
    total = size * size
    k = abs(dark * 100 - 50 * total) // (5 * total)
    score += k * 10
    return score


# ---------------------------------------------------------------------------
# Public QR object
# ---------------------------------------------------------------------------
class QR:
    def __init__(self, version, ecc, mask, modules):
        self.version = version
        self.ecc = ecc
        self.mask = mask
        self.size = len(modules)
        self.modules = modules


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------
def encode(data: str, ecc="M", version=None, mask=None) -> QR:
    if not isinstance(data, str):
        raise ToolError("data must be a str")
    if not isinstance(ecc, str):
        raise ToolError("ecc must be one of L, M, Q, H")
    ecc = ecc.upper()
    if ecc not in "LMQH":
        raise ToolError("ecc must be one of L, M, Q, H")
    if version is not None:
        if not isinstance(version, int) or not (1 <= version <= 10):
            raise ToolError("version must be an integer 1..10")
    if mask is not None:
        if not isinstance(mask, int) or not (0 <= mask <= 7):
            raise ToolError("mask must be an integer 0..7")

    data_bytes = data.encode("utf-8")

    if version is None:
        chosen = None
        for v in range(1, 11):
            if len(data_bytes) <= _capacity_bytes(v, ecc):
                chosen = v
                break
        if chosen is None:
            max_v10 = _capacity_bytes(10, ecc)
            raise ToolError(
                f"data too long for version 10 at ECC {ecc}: {len(data_bytes)} bytes, max {max_v10}"
            )
        version = chosen
    else:
        cap = _capacity_bytes(version, ecc)
        if len(data_bytes) > cap:
            raise ToolError(
                f"data too long for version {version} at ECC {ecc}: {len(data_bytes)} bytes, max {cap}"
            )

    ec_per_block, n1, d1, n2, d2 = _block_info(version, ecc)
    data_cw = n1 * d1 + n2 * d2
    capacity_bits = data_cw * 8

    # ---- bit stream ------------------------------------------------------
    bits = []
    bits.extend(_bits_from_value(0b0100, 4))  # byte mode
    cc_bits = 8 if version <= 9 else 16
    bits.extend(_bits_from_value(len(data_bytes), cc_bits))
    for b in data_bytes:
        bits.extend(_bits_from_value(b, 8))

    term = min(4, capacity_bits - len(bits))
    bits.extend([0] * term)
    while len(bits) % 8 != 0:
        bits.append(0)

    pad = [0xEC, 0x11]
    pi = 0
    while len(bits) < capacity_bits:
        bits.extend(_bits_from_value(pad[pi % 2], 8))
        pi += 1

    # ---- data codewords --------------------------------------------------
    data_codewords = []
    for i in range(0, capacity_bits, 8):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i + j]
        data_codewords.append(v)

    # ---- split into blocks, compute EC, interleave -----------------------
    blocks = []
    idx = 0
    for _ in range(n1):
        blocks.append(data_codewords[idx:idx + d1])
        idx += d1
    for _ in range(n2):
        blocks.append(data_codewords[idx:idx + d2])
        idx += d2

    ec_blocks = [_rs_remainder(b, ec_per_block) for b in blocks]

    interleaved = []
    max_d = max((len(b) for b in blocks), default=0)
    for i in range(max_d):
        for b in blocks:
            if i < len(b):
                interleaved.append(b[i])
    for i in range(ec_per_block):
        for b in ec_blocks:
            interleaved.append(b[i])

    final_bits = []
    for cw in interleaved:
        final_bits.extend(_bits_from_value(cw, 8))
    final_bits.extend([0] * REMAINDER[version])

    # ---- matrix ----------------------------------------------------------
    size = 17 + 4 * version
    mods = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    _draw_finder(mods, func, 0, 0)
    _draw_finder(mods, func, 0, size - 7)
    _draw_finder(mods, func, size - 7, 0)
    _draw_timing(mods, func, size)
    _draw_alignment(mods, func, version, size)
    mods[size - 8][8] = True
    func[size - 8][8] = True
    _reserve_format(func, size)
    _draw_version_bits(mods, func, version, size)

    # ---- place data ------------------------------------------------------
    bit_idx = 0
    right = size - 1
    upward = True
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                c = right - j
                r = (size - 1 - vert) if upward else vert
                if not func[r][c]:
                    if bit_idx < len(final_bits):
                        mods[r][c] = bool(final_bits[bit_idx])
                        bit_idx += 1
        right -= 2
        upward = not upward

    # ---- choose mask -----------------------------------------------------
    masks = [mask] if mask is not None else list(range(8))
    best_mods = None
    best_score = None
    best_mask = None
    for m in masks:
        cand = [row[:] for row in mods]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and MASK_FUNCS[m](r, c):
                    cand[r][c] = not cand[r][c]
        _draw_format_bits(cand, func, size, ecc, m)
        score = _penalty(cand)
        if best_score is None or score < best_score:
            best_score = score
            best_mods = cand
            best_mask = m

    return QR(version, ecc, best_mask, best_mods)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def to_matrix(qr, border=4):
    if not isinstance(border, int) or border < 0:
        raise ToolError("border must be a non-negative integer")
    n = qr.size + 2 * border
    mat = [[0] * n for _ in range(n)]
    for r in range(qr.size):
        for c in range(qr.size):
            if qr.modules[r][c]:
                mat[r + border][c + border] = 1
    return mat


def to_svg(qr, scale=4, border=4, dark="#000", light="#fff"):
    if not isinstance(scale, int) or scale < 1:
        raise ToolError("scale must be >= 1")
    if not isinstance(border, int) or border < 0:
        raise ToolError("border must be >= 0")
    total = qr.size + 2 * border
    w = total * scale
    parts = []
    for r in range(qr.size):
        c = 0
        while c < qr.size:
            if qr.modules[r][c]:
                start = c
                while c < qr.size and qr.modules[r][c]:
                    c += 1
                parts.append(
                    f"M{start + border} {r + border}h{c - start}v1h{-(c - start)}z"
                )
            else:
                c += 1
    path = " ".join(parts)
    dark_attr = _xml_escape(dark, {'"': "&quot;"})
    light_attr = _xml_escape(light, {'"': "&quot;"})
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{w}" '
        f'viewBox="0 0 {total} {total}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="{light_attr}"/>'
        f'<path fill="{dark_attr}" d="{path}"/>'
        f"</svg>"
    )


def _png_chunk(tag, data):
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def to_png(qr, scale=4, border=4):
    if not isinstance(scale, int) or scale < 1:
        raise ToolError("scale must be >= 1")
    if not isinstance(border, int) or border < 0:
        raise ToolError("border must be >= 0")
    mat = to_matrix(qr, border)
    h = len(mat)
    w = len(mat[0])
    raw = bytearray()
    for r in range(h):
        row_bytes = bytearray()
        for c in range(w):
            v = 0 if mat[r][c] else 255
            row_bytes.extend([v] * scale)
        for _ in range(scale):
            raw.append(0)  # filter type 0
            raw.extend(row_bytes)
    ihdr = struct.pack(">IIBBBBB", w * scale, h * scale, 8, 0, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", ihdr)
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _png_chunk(b"IEND", b"")
    return png


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------
def selftest() -> int:
    checks = 0

    def chk(cond, msg=""):
        nonlocal checks
        if not cond:
            raise AssertionError(msg or f"check {checks + 1} failed")
        checks += 1

    # 1. basic v1-M
    qr = encode("HELLO WORLD", ecc="M", version=1)
    chk(qr.version == 1, "version")
    chk(qr.size == 21, "size")
    chk(qr.ecc == "M", "ecc")
    chk(len(qr.modules) == 21 and len(qr.modules[0]) == 21, "matrix shape")

    # 2. finder pattern
    chk(qr.modules[0][0] is True, "finder corner")
    chk(qr.modules[0][6] is True, "finder corner 6")
    chk(qr.modules[1][1] is False, "finder light ring")
    chk(qr.modules[2][2] is True, "finder inner dark")
    chk(qr.modules[6][6] is True, "finder inner dark corner")
    chk(qr.modules[7][0] is False, "separator")

    # 3. timing patterns
    for c in range(8, 13):
        chk(qr.modules[6][c] == (c % 2 == 0), f"timing col {c}")
    for r in range(8, 13):
        chk(qr.modules[r][6] == (r % 2 == 0), f"timing row {r}")

    # 4. dark module
    chk(qr.modules[4 * 1 + 9][8] is True, "dark module")

    # 5. format information for M, mask 0
    fb = _format_bits("M", 0)
    chk(fb == 0b101010000010010, f"format bits {fb:015b}")
    qr0 = encode("A", ecc="M", version=1, mask=0)
    read = 0
    for i in range(6):
        if qr0.modules[i][8]:
            read |= 1 << i
    if qr0.modules[7][8]:
        read |= 1 << 6
    if qr0.modules[8][8]:
        read |= 1 << 7
    if qr0.modules[8][7]:
        read |= 1 << 8
    for i in range(9, 15):
        if qr0.modules[8][14 - i]:
            read |= 1 << i
    chk(read == fb, f"placed format {read:015b} != {fb:015b}")

    # 6. Reed-Solomon remainder
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17]
    rem = _rs_remainder(data, 10)
    chk(len(rem) == 10, "rs length")
    chk(_rs_remainder(data + rem, 10) == [0] * 10, "rs zero remainder")

    # 7. capacities
    chk(_capacity_bytes(1, "M") == 14, "v1-M capacity")
    chk(_capacity_bytes(10, "M") == 213, "v10-M capacity")

    # 8. too-long refusal
    try:
        encode("x" * 15, ecc="M", version=1)
        chk(False, "expected ToolError")
    except ToolError as exc:
        chk("data too long" in str(exc), "too long message")

    # 9. max capacity boundary
    qr_max = encode("x" * 213, ecc="M", version=10)
    chk(qr_max.version == 10, "v10 max version")
    chk(qr_max.size == 57, "v10 max size")
    try:
        encode("x" * 214, ecc="M", version=10)
        chk(False, "expected ToolError")
    except ToolError:
        chk(True, "v10 overflow")

    # 10. empty input
    qr_empty = encode("", ecc="H")
    chk(qr_empty.version == 1, "empty version")
    chk(qr_empty.modules[0][0] is True, "empty finder")

    # 11. all ECC levels
    for level in "LMQH":
        q = encode("test", ecc=level)
        chk(q.version >= 1, f"ecc {level}")

    # 12. alignment pattern v2
    qr2 = encode("hi", ecc="L", version=2)
    chk(qr2.size == 25, "v2 size")
    chk(qr2.modules[18][18] is True, "alignment centre")
    chk(qr2.modules[16][16] is True, "alignment border")
    chk(qr2.modules[17][17] is False, "alignment inner light")

    # 13. version information v7
    qr7 = encode("x" * 100, ecc="L", version=7)
    chk(qr7.size == 45, "v7 size")
    vb = _version_bits(7)
    read_v = 0
    for i in range(18):
        r = i // 3
        c = 45 - 11 + i % 3
        if qr7.modules[r][c]:
            read_v |= 1 << i
    chk(read_v == vb, f"version bits {read_v} != {vb}")
    read_v2 = 0
    for i in range(18):
        r = 45 - 11 + i % 3
        c = i // 3
        if qr7.modules[r][c]:
            read_v2 |= 1 << i
    chk(read_v2 == vb, "version bits bottom-left")

    # 14. explicit and automatic mask
    qm = encode("mask test", ecc="Q", mask=3)
    chk(qm.mask == 3, "explicit mask")
    qa = encode("mask test", ecc="Q")
    chk(0 <= qa.mask <= 7, "auto mask range")

    # 15. matrix / quiet zone
    mat = to_matrix(qr, border=4)
    chk(len(mat) == qr.size + 8, "matrix border size")
    chk(mat[0][0] == 0, "quiet zone")
    chk(mat[4][4] == (1 if qr.modules[0][0] else 0), "matrix offset")

    # 16. SVG well-formed
    svg = to_svg(qr, scale=3, border=2)
    root = ET.fromstring(svg)
    chk(root.tag.endswith("svg"), "svg root")
    chk("shape-rendering" in svg, "svg crispEdges")

    # 17. PNG header / CRC
    png = to_png(qr, scale=2, border=1)
    chk(png[:8] == b"\x89PNG\r\n\x1a\n", "png signature")
    chunk_len = struct.unpack(">I", png[8:12])[0]
    chk(chunk_len == 13, "png IHDR length")
    tag = png[12:16]
    chk(tag == b"IHDR", "png IHDR tag")
    ihdr_data = png[16:16 + 13]
    crc = struct.unpack(">I", png[16 + 13:16 + 17])[0]
    chk(crc == (zlib.crc32(tag + ihdr_data) & 0xFFFFFFFF), "png IHDR crc")
    w, h = struct.unpack(">II", ihdr_data[:8])
    total = qr.size + 2
    chk(w == total * 2 and h == total * 2, "png dimensions")

    # 18. invalid inputs
    for bad in ["Z"]:
        try:
            encode("x", ecc=bad)
            chk(False, "bad ecc")
        except ToolError:
            chk(True, "bad ecc refused")
    try:
        encode("x", version=11)
        chk(False, "bad version")
    except ToolError:
        chk(True, "bad version refused")
    try:
        encode("x", mask=8)
        chk(False, "bad mask")
    except ToolError:
        chk(True, "bad mask refused")

    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
