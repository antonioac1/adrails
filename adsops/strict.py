"""Strict parsing + value checks shared by every loader. Untrusted files never reach the rails unvalidated."""
import datetime as dt
import json
import math

from adsops.errors import ToolError

MAX_FILE_BYTES = 5_000_000
MATCH_TYPES = ("BROAD", "PHRASE", "EXACT")


def _no_constants(name):
    raise ValueError(f"non-finite number {name} is not allowed")


def load_json_file(path, what):
    """Read a JSON file: size-capped, UTF-8, no NaN/Infinity, bounded nesting. Errors -> ToolError."""
    if not isinstance(path, (str, bytes)) and not hasattr(path, "__fspath__"):
        raise ToolError(f"{what}: path must be a string")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise ToolError(f"{what} not found: {path}") from None
    except OSError as e:
        raise ToolError(f"cannot read {what} {path}: {type(e).__name__}") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ToolError(f"{what} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
        return json.loads(text, parse_constant=_no_constants)
    except UnicodeDecodeError:
        raise ToolError(f"{what} is not valid UTF-8") from None
    except RecursionError:
        raise ToolError(f"{what} is nested too deeply") from None
    except ValueError as e:
        raise ToolError(f"{what}: invalid JSON: {str(e)[:120]}") from None


def finite_number(value, name, minimum=0.0, allow_equal_min=True):
    """A real, finite number (not bool, not NaN/inf, not overflowed) >= minimum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{name} must be a number")
    if not math.isfinite(float(value)):
        raise ToolError(f"{name} must be finite")
    if value < minimum or (not allow_equal_min and value == minimum):
        raise ToolError(f"{name} must be {'>=' if allow_equal_min else '>'} {minimum}")
    return value


def iso_date(value, name):
    """A real calendar date written exactly YYYY-MM-DD."""
    if not isinstance(value, str) or len(value) != 10:
        raise ToolError(f"{name} must be a YYYY-MM-DD date")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} is not a real date: {value!r}") from None
    return value


def match_type(value, name="match_type"):
    if value is None:
        return "PHRASE"
    if not isinstance(value, str) or value.strip().upper() not in MATCH_TYPES:
        raise ToolError(f"{name} must be one of {', '.join(MATCH_TYPES)}")
    return value.strip().upper()


def negatives_list(value, name="negatives_add"):
    if not isinstance(value, list):
        raise ToolError(f"{name} must be a list")
    if len(value) > 1000:
        raise ToolError(f"{name} has more than 1000 entries")
    out = []
    for i, n in enumerate(value):
        if not isinstance(n, dict) or not isinstance(n.get("text"), str) or not n["text"].strip():
            raise ToolError(f"{name}[{i}] needs a non-empty text")
        if len(n["text"]) > 80 or any(c in n["text"] for c in "\r\n\t\x00"):
            raise ToolError(f"{name}[{i}] text is too long or has control characters")
        out.append({"text": n["text"].strip().lower(), "match_type": match_type(n.get("match_type"), f"{name}[{i}].match_type")})
    return out


def selftest():
    import os, tempfile
    n = 0
    d = tempfile.mkdtemp()
    def wf(name, data):
        p = os.path.join(d, name); open(p, "wb").write(data); return p
    for bad in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b'\xff\xfe{}', b'[' * 200000, b'{"a":'):
        try:
            load_json_file(wf("x.json", bad), "f"); raise AssertionError(bad[:20])
        except ToolError:
            n += 1
    assert load_json_file(wf("ok.json", b'\xef\xbb\xbf{"a": 1e3}'), "f") == {"a": 1000.0}; n += 1
    try:
        load_json_file(None, "f"); raise AssertionError("None path")
    except ToolError:
        n += 1
    for bad in (float("nan"), float("inf"), True, "10", None, [1], -1):
        try:
            finite_number(bad, "x"); raise AssertionError(bad)
        except ToolError:
            n += 1
    assert finite_number(1e308 * 10 if False else 5, "x") == 5; n += 1
    for bad in ("2026-02-30", "2026-13-01", " 2026-11-30", "2026-1-1", 20261130):
        try:
            iso_date(bad, "d"); raise AssertionError(bad)
        except ToolError:
            n += 1
    assert iso_date("2028-02-29", "d"); n += 1
    for bad in ("FUZZY", 3, ""):
        try:
            match_type(bad); raise AssertionError(bad)
        except ToolError:
            n += 1
    assert match_type("exact ") == "EXACT" and match_type(None) == "PHRASE"; n += 1
    for bad in (5, None, [{"text": " "}], [{"text": "a\nb"}], [{"text": "x" * 81}], [3]):
        try:
            negatives_list(bad); raise AssertionError(bad)
        except ToolError:
            n += 1
    return n
