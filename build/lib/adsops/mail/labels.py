"""adsops.mail.labels — print-exact label sheets (HTML) and direct-mail vendor CSV.

Renders US-Letter label sheets for the Avery 5160/5163/5164/5167 families as
self-contained HTML that prints or PDFs pixel-exactly from any browser, and
builds CSV-injection-safe vendor export files for direct-mail houses.

Safety properties: every sheet carries a diagonal "SAMPLE - DO NOT MAIL"
watermark on the sheet *and* on each label unless the caller passes
sample=False explicitly; all user text is HTML-escaped; CSV cells beginning
with = + - @ are defused with a leading apostrophe; oversized batches are
refused with RailViolation instead of quietly generating huge files.

There is no network/file/clock I/O at import or call time. The single optional
external effect is the injected ``qr_svg`` callable. A tool should call
``counts`` first (to report sheets), then ``sheet_html(...sample=False)`` only
after an explicit operator confirmation, or ``vendor_csv`` for the vendor.
"""

from __future__ import annotations

import csv
import html
import io
import math
from typing import Any, Callable, Iterable, Mapping

from adsops.errors import ToolError, RailViolation, scrub

# --------------------------------------------------------------------------- #
# Templates (inches, US Letter 8.5 x 11)
# --------------------------------------------------------------------------- #

TEMPLATES: dict[str, dict[str, Any]] = {
    # 3 x 10 address labels (5160/5260/8160/8460/5630)
    "avery_5160": {"cols": 3, "rows": 10, "width": 2.625, "height": 1.0,
                   "top": 0.5, "left": 0.1875, "h_pitch": 2.75, "v_pitch": 1.0,
                   "per_sheet": 30},
    # 2 x 5 shipping labels (5163/5263/8163)
    "avery_5163": {"cols": 2, "rows": 5, "width": 4.0, "height": 2.0,
                   "top": 0.5, "left": 0.15625, "h_pitch": 4.1875, "v_pitch": 2.0,
                   "per_sheet": 10},
    # 2 x 3 shipping labels (5164/8164)
    "avery_5164": {"cols": 2, "rows": 3, "width": 4.0, "height": 3.333,
                   "top": 0.5, "left": 0.15625, "h_pitch": 4.1875, "v_pitch": 3.333,
                   "per_sheet": 6},
    # 4 x 20 return-address labels (5167/8167)
    "avery_5167": {"cols": 4, "rows": 20, "width": 1.75, "height": 0.5,
                   "top": 0.5, "left": 0.28125, "h_pitch": 2.0625, "v_pitch": 0.5,
                   "per_sheet": 80},
}

# Product numbers printed on the box that map onto the same die-cut geometry.
_ALIASES = {
    "5160": "avery_5160", "5260": "avery_5160", "8160": "avery_5160",
    "8460": "avery_5160", "5630": "avery_5160",
    "5163": "avery_5163", "5263": "avery_5163", "8163": "avery_5163",
    "5164": "avery_5164", "8164": "avery_5164",
    "5167": "avery_5167", "8167": "avery_5167",
}

DEFAULT_COLUMNS = ("name", "address_line_1", "address_line_2", "city", "state",
                   "zip", "country", "tracking_code", "tracking_url", "segment",
                   "variant", "batch")

MAX_HTML_PIECES = 1500      # a browser PDF of more labels is an operator mistake
MAX_CSV_PIECES = 20000      # vendor files above this must be split into batches

QR_IN = 0.8                 # nominal QR edge, inches

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _g(row: Any, *path: Any, default: Any = None) -> Any:
    """Nested get for Google-Ads-style camelCase rows (defensive, never raises)."""
    cur = row
    for key in path:
        if isinstance(cur, Mapping):
            cur = cur.get(key)
        elif isinstance(cur, (list, tuple)) and isinstance(key, int):
            cur = cur[key] if 0 <= key < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return default if cur is None else cur


def _text(piece: Mapping, *keys: str) -> str:
    """First non-empty stripped string value among ``keys``."""
    for key in keys:
        val = _g(piece, key)
        if val is None or isinstance(val, (dict, list, tuple)):
            continue
        s = str(val).strip()
        if s:
            return s
    return ""


def _in(value: float) -> str:
    """Format inches with 5 decimals (enough for every template offset), trimmed."""
    s = f"{float(value):.5f}".rstrip("0").rstrip(".")
    return (s or "0") + "in"


def _pt(value: float) -> str:
    return f"{float(value):g}"


def _template(name: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ToolError("template must be a non-empty string")
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    key = _ALIASES.get(key, key)
    tpl = TEMPLATES.get(key)
    if tpl is None:
        raise ToolError(f"unknown label template {name!r}; known: {', '.join(sorted(TEMPLATES))}")
    return dict(tpl, key=key)


def _normalise_pieces(pieces: Any) -> list[Mapping]:
    if pieces is None:
        return []
    if isinstance(pieces, (Mapping, str, bytes)):
        raise ToolError("pieces must be a sequence of objects, not a single value")
    try:
        items = list(pieces)
    except TypeError:
        raise ToolError("pieces must be a sequence of objects") from None
    for idx, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ToolError(f"piece #{idx} is not an object: {scrub(repr(item), 120)}")
    return items


def _label_lines(piece: Mapping) -> list[str]:
    """Explicit ``_label`` wins; otherwise compose from name/address fields."""
    raw = piece.get("_label")
    if raw is not None:
        if isinstance(raw, str):
            lines = raw.splitlines()
        elif isinstance(raw, (list, tuple)):
            lines = [str(x) for x in raw]
        else:
            raise ToolError("piece['_label'] must be a string or a list of strings")
        return [ln.strip() for ln in lines if ln.strip()]

    name = _text(piece, "name", "full_name", "recipient")
    a1 = _text(piece, "address_line_1", "address1", "street", "address")
    a2 = _text(piece, "address_line_2", "address2", "unit")
    city = _text(piece, "city", "town")
    state = _text(piece, "state", "region", "province")
    postcode = _text(piece, "zip", "postal_code", "zipcode", "postcode")
    country = _text(piece, "country")

    tail = " ".join(part for part in (state, postcode) if part)
    city_line = ", ".join(part for part in (city, tail) if part)
    lines = [name, a1, a2, city_line]
    # The spec says to build label lines from the piece's name/address fields.
    # Include country whenever provided rather than silently dropping it.
    if country:
        lines.append(country)
    return [ln for ln in lines if ln]


def _qr_allowed(tpl: Mapping) -> bool:
    """QR fits if the label is at least 1.75in tall or at least 2.625in wide."""
    return tpl["height"] >= 1.75 or tpl["width"] >= 2.625


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

_CSS = """
@page { size: letter; margin: 0; }
html, body { margin: 0; padding: 0; background: #fff; }
.sheet { position: relative; width: 8.5in; height: 11in; overflow: hidden;
         background: #fff; page-break-after: always; break-after: page; }
.sheet:last-of-type { page-break-after: auto; break-after: auto; }
.label { position: absolute; overflow: visible; white-space: normal;
         word-wrap: break-word; overflow-wrap: anywhere; line-height: 1.15;
         font-family: Helvetica, Arial, sans-serif; color: #000; }
.label .txt { position: absolute; left: 0.05in; top: 0.04in; bottom: 0.04in;
              right: 0.05in; overflow: visible; white-space: normal;
              overflow-wrap: anywhere; }
.label .code { font-size: 0.8em; margin-top: 0.04in; letter-spacing: 0.02em; }
.label .qr { position: absolute; top: 50%; transform: translateY(-50%); }
.label .qr svg { width: 100%; height: 100%; display: block; }
.note { position: absolute; left: 0.5in; top: 0.5in;
        font: 12pt Helvetica, Arial, sans-serif; color: #666; }
.wm-sheet { position: absolute; left: 0; top: 42%; width: 8.5in; text-align: center;
            transform: rotate(-30deg); z-index: 50; pointer-events: none;
            font: bold 40pt/1.2 Helvetica, Arial, sans-serif; letter-spacing: 0.08em;
            color: rgba(190, 0, 0, 0.16); }
.wm-label { position: absolute; left: 0; top: 50%; width: 100%; text-align: center;
            transform: rotate(-30deg) translateY(-50%); z-index: 40;
            pointer-events: none; color: rgba(190, 0, 0, 0.35);
            font: bold 8pt/1 Helvetica, Arial, sans-serif; }
@media print { .sheet { page-break-after: always; } .sheet:last-of-type { page-break-after: auto; } }
"""


def _label_html(piece: Mapping, tpl: Mapping, x: float, y: float, font_pt: float,
                show_code: bool, qr_svg: Callable[[str], str] | None,
                sample: bool) -> str:
    lines = _label_lines(piece)
    code = _text(piece, "tracking_code")
    emit_code = bool(show_code and code)

    # Shrink one step (never below 6pt) when the address block is cramped.
    size = font_pt
    if len(lines) > 4 or any(len(ln) > 34 for ln in lines):
        size = max(6.0, font_pt - 1.0)

    # Optional QR: only when the die-cut can host it and the piece has a URL.
    svg, qr_in = "", 0.0
    if callable(qr_svg) and _qr_allowed(tpl):
        url = _text(piece, "tracking_url", "qr_url")
        if url:
            try:
                candidate = qr_svg(url)
            except Exception:  # a broken QR must never sink the whole sheet
                candidate = None
            if isinstance(candidate, str) and candidate.strip():
                svg = candidate
                qr_in = min(QR_IN, tpl["height"] - 0.12, tpl["width"] * 0.45)
                if qr_in < 0.3:      # too small to scan reliably -> drop it
                    svg, qr_in = "", 0.0

    parts = [f'<div class="label" style="left:{_in(x)};top:{_in(y)};'
             f'width:{_in(tpl["width"])};height:{_in(tpl["height"])};">']
    txt_style = f"font-size:{_pt(size)}pt;"
    if qr_in:
        txt_style += f"right:{_in(qr_in + 0.12)};"
    parts.append(f'<div class="txt" style="{txt_style}">')
    for line in lines:
        parts.append(f'<div class="ln">{html.escape(line)}</div>')
    if emit_code:
        parts.append(f'<div class="code">Code {html.escape(code)}</div>')
    parts.append("</div>")
    if qr_in:
        parts.append(f'<div class="qr" style="width:{_in(qr_in)};height:{_in(qr_in)};'
                     f'right:0.06in;">{svg}</div>')
    if sample:
        parts.append('<div class="wm-label">SAMPLE</div>')
    parts.append("</div>")
    return "".join(parts)


def _render_sheet(items: list[Mapping], tpl: Mapping, sample: bool, show_code: bool,
                  qr_svg: Callable[[str], str] | None, font_pt: float) -> str:
    parts = ['<div class="sheet">']
    if not items:
        parts.append('<div class="note">No labels &#8212; nothing to print.</div>')
    for idx, piece in enumerate(items[: tpl["per_sheet"]]):
        col = idx % tpl["cols"]
        row = idx // tpl["cols"]
        x = tpl["left"] + col * tpl["h_pitch"]
        y = tpl["top"] + row * tpl["v_pitch"]
        parts.append(_label_html(piece, tpl, x, y, font_pt, show_code, qr_svg, sample))
    if sample:
        parts.append('<div class="wm-sheet">SAMPLE - DO NOT MAIL</div>')
    parts.append("</div>")
    return "".join(parts)


def sheet_html(pieces: Iterable[Mapping] | None, template: str = "avery_5160",
               sample: bool = True, title: str = "Labels", show_code: bool = True,
               qr_svg: Callable[[str], str] | None = None,
               font_pt: float = 9) -> str:
    """Render label sheets as a printable HTML document (one div per sheet)."""
    tpl = _template(template)
    if not isinstance(sample, bool):
        raise ToolError("sample must be a boolean (True = watermark, do not mail)")
    if qr_svg is not None and not callable(qr_svg):
        raise ToolError("qr_svg must be callable(url) -> svg string")
    try:
        font_pt = float(font_pt)
    except (TypeError, ValueError):
        raise ToolError("font_pt must be a number") from None
    if math.isnan(font_pt) or not 6.0 <= font_pt <= 24.0:
        raise ToolError("font_pt must be between 6 and 24")

    items = _normalise_pieces(pieces)
    if len(items) > MAX_HTML_PIECES:
        raise RailViolation(
            f"refusing to render {len(items)} labels in one sheet set "
            f"(limit {MAX_HTML_PIECES}); split the campaign into batches")

    per = tpl["per_sheet"]
    sheets: list[str] = []
    if not items:
        sheets.append(_render_sheet([], tpl, sample, bool(show_code), qr_svg, font_pt))
    else:
        for start in range(0, len(items), per):
            sheets.append(_render_sheet(items[start:start + per], tpl, sample,
                                        bool(show_code), qr_svg, font_pt))

    doc = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{html.escape(str(title or 'Labels'))}</title>",
        f"<style>{_CSS}</style></head><body>",
        *sheets,
        "</body></html>",
    ]
    return "\n".join(doc)


# --------------------------------------------------------------------------- #
# Vendor CSV
# --------------------------------------------------------------------------- #


def _csv_safe(value: Any) -> str:
    """Flatten control chars and defuse spreadsheet formula injection."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if text[:1] in ("=", "+", "-", "@", "\t"):
        text = "'" + text
    return text


def vendor_csv(pieces: Iterable[Mapping] | None, columns: Iterable[str] | None = None) -> str:
    """Header + rows of CSV text, safe to hand to a direct-mail vendor."""
    items = _normalise_pieces(pieces)
    if len(items) > MAX_CSV_PIECES:
        raise RailViolation(
            f"refusing to export {len(items)} rows (limit {MAX_CSV_PIECES}); "
            "split the order into batches")
    if columns is None:
        cols = list(DEFAULT_COLUMNS)
    else:
        if isinstance(columns, (str, bytes)):
            raise ToolError("columns must be a list of column names, not a string")
        supplied = list(columns)
        cols = [c.strip() for c in supplied if isinstance(c, str) and c.strip()]
        if len(cols) != len(supplied):
            raise ToolError("each column name must be a non-empty string")
        if not cols:
            raise ToolError("columns must not be empty")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for piece in items:
        writer.writerow([_csv_safe(_g(piece, col)) for col in cols])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def counts(pieces: Iterable[Mapping] | None, template: str = "avery_5160") -> dict[str, int]:
    """Sheet math for a run: total pieces, sheets to print, labels on the last sheet."""
    tpl = _template(template)
    n = len(_normalise_pieces(pieces))
    per = tpl["per_sheet"]
    sheets = max(1, math.ceil(n / per)) if n else 1
    last = n % per
    if last == 0 and n:
        last = per
    return {"pieces": n, "sheets": sheets, "labels_last_sheet": last}


# --------------------------------------------------------------------------- #
# Self-test (all I/O mocked; no files, no network, no clock)
# --------------------------------------------------------------------------- #


def selftest() -> int:
    checks = 0

    def ok(cond: bool, msg: str) -> None:
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(msg)

    def raises(exc: type, fn: Callable, *args: Any, **kwargs: Any) -> None:
        nonlocal checks
        checks += 1
        try:
            fn(*args, **kwargs)
        except exc:
            return
        except Exception as err:  # noqa: BLE001 - report the wrong failure clearly
            raise AssertionError(f"expected {exc.__name__}, got {type(err).__name__}: {err}")
        raise AssertionError(f"expected {exc.__name__}, nothing raised")

    piece = {
        "name": "Ada Lovelace",
        "address_line_1": "12 Analytical Way",
        "address_line_2": "Apt 3",
        "city": "Springfield",
        "state": "IL",
        "zip": "62704",
        "country": "US",
        "tracking_code": "ABC123",
        "tracking_url": "https://adrails.example/t/ABC123",
    }

    # --- templates --------------------------------------------------------
    ok(set(TEMPLATES) == {"avery_5160", "avery_5163", "avery_5164", "avery_5167"},
       "template keys")
    for key, tpl in TEMPLATES.items():
        ok(tpl["per_sheet"] == tpl["cols"] * tpl["rows"], f"{key} per_sheet == cols*rows")

    # --- default render is watermarked ------------------------------------
    doc = sheet_html([piece])
    ok(doc.startswith("<!DOCTYPE html>"), "doctype")
    ok("@page" in doc and "size: letter" in doc, "letter page css")
    ok("SAMPLE - DO NOT MAIL" in doc and 'class="wm-label"' in doc, "default watermark")
    ok("Code ABC123" in doc, "tracking code line")
    ok("https://adrails.example" not in doc, "tracking url not printed")

    # --- explicit sample=False is required for real print ------------------
    # NB: the CSS still defines .wm-label/.wm-sheet as class *selectors*, so we
    # assert the watermark *elements* are gone, not the raw substrings.
    real = sheet_html([piece], sample=False)
    ok("SAMPLE" not in real
       and 'class="wm-label"' not in real
       and 'class="wm-sheet"' not in real, "sample=False drops watermark")
    ok("Code ABC123" in real, "code kept when sample=False")

    # --- exact geometry ----------------------------------------------------
    grid = [dict(piece, name=f"P{i}") for i in range(4)]
    laid = sheet_html(grid, sample=False)
    ok("left:2.9375in" in laid, "5160 second column x")
    ok("top:1.5in" in laid, "5160 second row y")
    laid3 = sheet_html([piece, piece, piece], template="avery_5163", sample=False)
    ok("left:4.34375in" in laid3 and "top:2.5in" in laid3, "5163 offsets")

    # --- escaping ----------------------------------------------------------
    evil = {"_label": '<b>"x" & y</b>'}
    escaped = sheet_html([evil], sample=False)
    ok("&lt;b&gt;" in escaped and "<b>" not in escaped, "html escaping")

    # --- font shrink rules -------------------------------------------------
    long_line = {"_label": "x" * 40}
    ok("font-size:8pt" in sheet_html([long_line], sample=False, font_pt=9),
       "shrink on >34 char line")
    tall = {"_label": "a\nb\nc\nd\ne"}
    ok("font-size:8pt" in sheet_html([tall], sample=False, font_pt=9),
       "shrink on >4 lines")
    ok("font-size:9pt" in sheet_html([{"_label": "short"}], sample=False, font_pt=9),
       "no shrink when it fits")
    ok("font-size:6pt" in sheet_html([long_line], sample=False, font_pt=6),
       "shrink floor is 6pt")

    # --- address composition ----------------------------------------------
    composed = sheet_html([piece], sample=False)
    ok("Springfield, IL 62704" in composed, "city/state/zip line")
    ok("Apt 3" in composed, "second address line")
    ok("US" in composed, "domestic country included")
    ok("Germany" in sheet_html([dict(piece, country="Germany")], sample=False),
       "international country shown")

    # --- empty run ---------------------------------------------------------
    blank = sheet_html([], sample=False)
    ok(blank.count('class="sheet"') == 1 and "No labels" in blank, "empty -> one blank sheet")
    ok(counts([], "avery_5160") == {"pieces": 0, "sheets": 1, "labels_last_sheet": 0},
       "empty counts")

    # --- counts ------------------------------------------------------------
    ok(counts([piece] * 30, "avery_5160") ==
       {"pieces": 30, "sheets": 1, "labels_last_sheet": 30}, "one full 5160 sheet")
    ok(counts([piece] * 31, "avery_5160")["sheets"] == 2, "31 pieces -> 2 sheets")
    ok(counts([piece] * 31, "avery_5160")["labels_last_sheet"] == 1, "last sheet has 1")
    ok(counts([piece] * 7, "avery_5164") ==
       {"pieces": 7, "sheets": 2, "labels_last_sheet": 1}, "5164 7 pieces")
    ok(counts([], "Avery_5163")["sheets"] == 1, "case-insensitive template name")
    ok(sheet_html([piece], template="8160", sample=False).count('class="sheet"') == 1,
       "product-number alias 8160")

    # --- QR ----------------------------------------------------------------
    seen: list[str] = []

    def good_qr(url: str) -> str:
        seen.append(url)
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'

    with_qr = sheet_html([piece], sample=False, qr_svg=good_qr)
    ok(seen == [piece["tracking_url"]], "qr callable received tracking url")
    ok("<svg" in with_qr and "right:0.92in" in with_qr, "qr embedded, text narrowed")

    def bad_qr(url: str) -> str:
        raise RuntimeError("boom sk_live_secret")

    ok("<svg" not in sheet_html([piece], sample=False, qr_svg=bad_qr), "broken qr skipped")
    no_url = {k: v for k, v in piece.items() if k != "tracking_url"}
    seen.clear()
    ok("<svg" not in sheet_html([no_url], sample=False, qr_svg=good_qr) and seen == [],
       "no url -> no qr call")

    seen.clear()
    ok("<svg" not in sheet_html([piece], template="avery_5167", sample=False, qr_svg=good_qr)
       and seen == [], "too-small template skips qr")

    # --- vendor csv --------------------------------------------------------
    rows = vendor_csv([piece]).splitlines()
    ok(rows[0] == ",".join(DEFAULT_COLUMNS), "csv header")
    ok(rows[1].startswith("Ada Lovelace,12 Analytical Way,Apt 3,Springfield,IL,62704,US,"),
       "csv row prefix")
    ok(rows[1].endswith("ABC123,,,"), "csv trailing empty columns")
    injected = vendor_csv([{"name": "=HYPERLINK(1)", "city": "+1", "state": "@x",
                            "zip": "-3"}], columns=["name", "city", "state", "zip"])
    ok(injected.splitlines()[1] == "'=HYPERLINK(1),'+1,'@x,'-3", "csv injection guard")
    ok(vendor_csv([{"name": "a\nb"}], columns=["name"]).splitlines()[1] == "a b",
       "newline flattened")
    ok(vendor_csv([]).strip() == ",".join(DEFAULT_COLUMNS), "empty csv -> header only")
    ok(vendor_csv([piece], columns=["zip", "name"]).splitlines()[0] == "zip,name",
       "custom column order")
    ok(vendor_csv([piece], columns=("name",)).splitlines()[1] == "Ada Lovelace",
       "tuple columns accepted")

    # --- refusals ----------------------------------------------------------
    raises(ToolError, sheet_html, [piece], template="nope")
    raises(ToolError, sheet_html, [piece], font_pt=99)
    raises(ToolError, sheet_html, [piece], font_pt="abc")
    raises(ToolError, sheet_html, [piece], sample="yes")
    raises(ToolError, sheet_html, [piece], qr_svg="not-callable")
    raises(ToolError, sheet_html, ["not a dict"])
    raises(ToolError, sheet_html, "not-a-list")
    raises(ToolError, sheet_html, [{"_label": 5}])
    raises(ToolError, vendor_csv, [piece], columns="name")
    raises(ToolError, vendor_csv, [piece], columns=[])
    raises(ToolError, vendor_csv, [{"name": "x"}], columns=[3])
    raises(ToolError, counts, [], "nope")
    raises(RailViolation, sheet_html, [piece] * (MAX_HTML_PIECES + 1))
    raises(RailViolation, vendor_csv, [piece] * (MAX_CSV_PIECES + 1))

    return checks


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)