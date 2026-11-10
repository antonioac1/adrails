"""Measure published pieces and keep LESSONS.md for the AdRails guarded content pipeline (0.12).

Every input here (Search Console daily rows, piece metadata, an operator-edited LESSONS.md file) is UNTRUSTED data: it is
never eval'd or exec'd, no regex is ever built from it (all patterns are compiled once at import time and matched against
it), every string is capped at MAX_TEXT characters and every input list is capped before it is walked.

What lives here (SPEC-D, learn.py):

    measure(pieces, daily_rows, today, windows=(14, 28, 56), baseline_days=28) -> [row]
        Per piece, per window: impressions/day, clicks/day and impression-weighted average position before (the
        baseline_days days before published_day) and after (published_day+1 .. published_day+window), plus the relative
        change. status is "pending" while the window has not been reached yet and "no_data" when the baseline holds
        fewer than 7 days of data.

    scoreboard(rows, window=28) -> [{"group", "n", "median_impr_change", "improved_share", "ids"}]
        Groups measured rows by "<content_type>|<query_class>", keeps only rows that reached the window with a usable
        baseline, and reports the median relative change and the share of pieces that improved.

    update_lessons(path, board, today, min_n=3, max_lines=20) -> {"added", "kept", "dropped"}
        Rewrites only the "## Learned (AdRails updates this section)" section of LESSONS.md. "## Operator notes" is
        preserved byte-for-byte. A learned line the operator deleted is recorded in the sidecar
        LESSONS.suppressed.json and is never added back. The file is capped at 20 KB and the learned section at
        max_lines lines; the write is atomic (tempfile in the same directory + os.replace).

    read_lessons(path) -> {"text": str, "weights": {group: float}}
        The two sections (capped at 4000 chars) and planner weights: 1 + clamp(median change, -0.5, 0.5) for every
        learned group with n >= min_n.

The only side effects are the LESSONS.md file and its suppression sidecar, both written 0644 and atomically.
"""
import hashlib
import json
import os
import re
import statistics
import tempfile
from datetime import date, timedelta

from ..errors import ToolError

MAX_TEXT = 200_000                  # cap for every input string (chars)
MAX_PIECES = 500                    # cap for the pieces list (items)
MAX_ROWS = 200_000                  # cap for daily_rows (items) / scoreboard input rows
MAX_BOARD = 200                     # cap for scoreboard groups in a lessons update
MAX_WINDOWS = 8                     # cap for the windows tuple
MAX_IDS_PER_LINE = 6                # ids quoted as evidence on one learned line
MAX_LINES = 20                      # default cap for the learned section (lines)
MAX_BYTES = 20 * 1024               # hard cap for LESSONS.md (bytes)
READ_MAX = 4000                     # cap for read_lessons()["text"]
MIN_N = 3                           # minimum evidence (pieces) before a group becomes a lesson/weight
BASE_DAYS = 28                      # default baseline window (days before published_day)
MIN_BASELINE_DAYS = 7               # below this many days of data a window is "no_data"

OPERATOR_HEADER = "## Operator notes"
LEARNED_HEADER = "## Learned (AdRails updates this section)"

_TEMPLATE = (
    "# LESSONS\n"
    "\n"
    "Lines under \"Learned\" are evidence-backed observations from measured published pages: measurements, not\n"
    "opinions. The planner and the writer read this file. Delete a line to drop it for good (AdRails records the\n"
    "deletion and will not add it back). AdRails never edits Operator notes; write your own notes there, because\n"
    "AdRails rewrites the Learned section and keeps only its tagged lines.\n"
    "\n"
    + OPERATOR_HEADER + "\n"
    "\n"
    + LEARNED_HEADER + "\n"
)

_TAG_RX = re.compile(r"^- \[(L-[0-9a-f]{6})\]")
_LINE_RX = re.compile(
    r"^- \[(L-[0-9a-f]{6})\] (.{1,120}?): impressions/day ([+-]?\d+)% median at (\d+) days, "
    r"(\d+)/(\d+) improved"
)


# ---------------------------------------------------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------------------------------------------------
def _abs(path):
    """Absolute path for a LESSONS.md argument (untrusted string, capped)."""
    if not isinstance(path, (str, os.PathLike)):
        raise ToolError("LESSONS.md path must be a string")
    raw = str(path).strip()[:4096]
    if not raw:
        raise ToolError("LESSONS.md path is required")
    return os.path.abspath(os.path.expanduser(raw))


def _as_date(value, field):
    """Parse a 'YYYY-MM-DD' string (or a date) or fail with a plain-English ToolError."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ToolError(f"{field} must be a YYYY-MM-DD string")
    raw = value.strip()[:32]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ToolError(f"{field} is not a valid YYYY-MM-DD date: {raw!r}") from None


def _num(value, default=0.0):
    """Float from untrusted input; NaN/inf/bools/garbage fall back to `default`."""
    if value is None or isinstance(value, bool):
        return default
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return x


def _change(before, after):
    """Relative change (after-before)/before, or None when it cannot be computed."""
    if before is None or after is None or before <= 0:
        return None
    return (after - before) / before


def _r6(x):
    return None if x is None else round(x, 6)


def _one_line(text, limit=120):
    """Collapse a label to a single short line (labels come from untrusted JSON)."""
    s = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
    return s[:limit]


# ---------------------------------------------------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------------------------------------------------
def _index_daily(daily_rows):
    """{page: {day: {"i": impressions, "c": clicks, "p": position*impressions, "n": rows}}}."""
    if daily_rows is None:
        return {}
    if not isinstance(daily_rows, (list, tuple)):
        raise ToolError("daily_rows must be a list")
    idx = {}
    for row in list(daily_rows)[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        page = row.get("page")
        day = row.get("day")
        if not isinstance(page, str) or not isinstance(day, str):
            continue
        page = page.strip()[:400]
        if not page:
            continue
        try:
            d = date.fromisoformat(day.strip()[:10])
        except ValueError:
            continue
        slot = idx.setdefault(page, {}).setdefault(d, {"i": 0.0, "c": 0.0, "p": 0.0, "n": 0})
        imp = _num(row.get("impressions"))
        pos = _num(row.get("position"))
        slot["i"] += imp
        slot["c"] += _num(row.get("clicks"))
        slot["p"] += pos * imp
        slot["n"] += 1
    return idx


def _lookup(idx, url):
    """Find the day map for a piece URL, tolerating a missing/extra trailing slash."""
    if not url:
        return {}
    for key in (url, url.rstrip("/") + "/", url.rstrip("/")):
        if key in idx:
            return idx[key]
    return {}


def _avg_window(daymap, lo, hi):
    """Per-day means over the observed days inside [lo, hi]; position is impression-weighted."""
    days = [d for d in daymap if lo <= d <= hi]
    if not days:
        return {"days": 0, "impr": None, "clicks": None, "position": None}
    impr = sum(daymap[d]["i"] for d in days)
    clicks = sum(daymap[d]["c"] for d in days)
    posw = sum(daymap[d]["p"] for d in days)
    return {
        "days": len(days),
        "impr": impr / len(days),
        "clicks": clicks / len(days),
        "position": (posw / impr) if impr > 0 else None,
    }


def _windows(windows):
    out = []
    for w in list(windows or (14, 28, 56))[:MAX_WINDOWS]:
        try:
            wi = int(_num(w, 0))
        except (TypeError, ValueError):
            continue
        if wi > 0 and wi not in out:
            out.append(wi)
    return out or [14, 28, 56]


def measure(pieces, daily_rows, today, windows=(14, 28, 56), baseline_days=BASE_DAYS):
    """Measure every published piece over every reached window (see module docstring)."""
    t = _as_date(today, "today")
    if pieces is not None and not isinstance(pieces, (list, tuple)):
        raise ToolError("pieces must be a list")
    base_days = int(_num(baseline_days, BASE_DAYS)) or BASE_DAYS
    if base_days < 1:
        base_days = BASE_DAYS
    wins = _windows(windows)
    idx = _index_daily(daily_rows)
    out = []
    for piece in list(pieces or [])[:MAX_PIECES]:
        if not isinstance(piece, dict):
            continue
        pid = _one_line(piece.get("id") or "", 120)
        url = _one_line(piece.get("url") or "", 400)
        content_type = _one_line(piece.get("content_type") or "unknown", 60)
        query_class = _one_line(piece.get("query_class") or "unknown", 60)
        try:
            pub = _as_date(piece.get("published_day"), f"published_day for {pid or url or 'piece'}")
        except ToolError:
            continue
        daymap = _lookup(idx, url)
        before = _avg_window(daymap, pub - timedelta(days=base_days), pub - timedelta(days=1))
        for w in wins:
            after_end = pub + timedelta(days=w)
            after = _avg_window(daymap, pub + timedelta(days=1), after_end)
            row = {
                "id": pid,
                "url": url,
                "content_type": content_type,
                "query_class": query_class,
                "window": w,
                "published_day": pub.isoformat(),
                "baseline_days": before["days"],
                "days": after["days"],
                "impr_before": None,
                "impr_after": None,
                "impr_change": None,
                "clicks_before": None,
                "clicks_after": None,
                "clicks_change": None,
                "pos_before": None,
                "pos_after": None,
                "pos_change": None,
            }
            if t < after_end:
                row["status"] = "pending"
            elif before["days"] < MIN_BASELINE_DAYS or before["impr"] is None:
                row["status"] = "no_data"
            else:
                row["status"] = "ok"
                row["impr_before"] = _r6(before["impr"])
                row["impr_after"] = _r6(after["impr"])
                row["impr_change"] = _r6(_change(before["impr"], after["impr"]))
                row["clicks_before"] = _r6(before["clicks"])
                row["clicks_after"] = _r6(after["clicks"])
                row["clicks_change"] = _r6(_change(before["clicks"], after["clicks"]))
                row["pos_before"] = _r6(before["position"])
                row["pos_after"] = _r6(after["position"])
                row["pos_change"] = _r6(_change(before["position"], after["position"]))
            out.append(row)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# scoreboard
# ---------------------------------------------------------------------------------------------------------------------
def scoreboard(rows, window=28):
    """Group measured rows by "<content_type>|<query_class>" and report medians for one window."""
    want = int(_num(window, 28)) or 28
    groups = {}
    if rows is None:
        rows = []
    if not isinstance(rows, (list, tuple)):
        raise ToolError("rows must be a list of measure() rows")
    for row in list(rows)[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "ok":
            continue
        change = row.get("impr_change")
        if isinstance(change, bool) or not isinstance(change, (int, float)):
            continue
        if _num(change, None) is None:
            continue
        try:
            row_window = int(_num(row.get("window"), 0))
        except (TypeError, ValueError):
            continue
        if row_window != want:
            continue
        group = row.get("group")
        if not isinstance(group, str) or not group.strip():
            group = f"{_one_line(row.get('content_type') or 'unknown', 60)}|" \
                    f"{_one_line(row.get('query_class') or 'unknown', 60)}"
        groups.setdefault(_one_line(group, 120), []).append((_one_line(row.get("id") or "", 80), float(change)))
    out = []
    for group, items in list(groups.items())[:MAX_BOARD]:
        changes = sorted(c for _, c in items)
        ids = [pid for pid, _ in items if pid][:50]
        out.append({
            "group": group,
            "n": len(items),
            "median_impr_change": round(statistics.median(changes), 6),
            "improved_share": round(sum(1 for c in changes if c > 0) / len(changes), 6),
            "ids": ids,
        })
    out.sort(key=lambda r: (-r["n"], r["group"]))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# LESSONS.md reading / writing
# ---------------------------------------------------------------------------------------------------------------------
def _sidecar_path(path):
    base, _ext = os.path.splitext(path)
    return base + ".suppressed.json"


def _split_learned(text):
    """(head, body): head is everything up to and including the Learned header line (kept byte-for-byte)."""
    idx = text.find(LEARNED_HEADER)
    if idx < 0:
        head = text if text.endswith("\n") else text + "\n"
        if not head.endswith("\n\n"):
            head = head + "\n"
        return head + LEARNED_HEADER + "\n", ""
    end = text.find("\n", idx)
    if end < 0:
        return text + "\n", ""
    return text[:end + 1], text[end + 1:]


def _sections(raw):
    """The two sections of LESSONS.md (operator notes first), as text."""
    start = raw.find(OPERATOR_HEADER)
    if start < 0:
        start = raw.find(LEARNED_HEADER)
    if start < 0:
        return ""
    return raw[start:]


def _learned_lines(body):
    """[(tag, line)] for every '- [L-xxxxxx] ...' line in the learned section."""
    out = []
    for line in body[:MAX_TEXT].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _TAG_RX.match(stripped)
        if not match:
            continue
        out.append((match.group(1), stripped[:400]))
        if len(out) >= 500:
            break
    return out


def _parse_line(line):
    """Parse one learned line; None when it does not follow the documented format."""
    match = _LINE_RX.match(line.strip()[:400])
    if not match:
        return None
    return {
        "tag": match.group(1),
        "group": match.group(2).strip(),
        "change": int(match.group(3)) / 100.0,
        "window": int(match.group(4)),
        "improved": int(match.group(5)),
        "n": int(match.group(6)),
    }


def _tag_for(group):
    return "L-" + hashlib.sha1(group.encode("utf-8", "replace")).hexdigest()[:6]


def _lesson_line(group, row, today_iso):
    """The exact learned-line format from SPEC-D."""
    pct = int(round(row["change"] * 100))
    evidence = ", ".join(row["ids"][:MAX_IDS_PER_LINE]) if row["ids"] else "none"
    return (
        f"- [{row['tag']}] {group}: impressions/day {pct:+d}% median at {row['window']} days, "
        f"{row['improved']}/{row['n']} improved (evidence: {evidence}) — {today_iso}"
    )


def _board_rows(board, today_iso):
    """Normalise scoreboard rows into the values a learned line needs."""
    if board is None:
        return []
    if not isinstance(board, (list, tuple)):
        raise ToolError("board must be a list of scoreboard rows")
    out = []
    for row in list(board)[:MAX_BOARD]:
        if not isinstance(row, dict):
            continue
        group = _one_line(row.get("group") or "", 120)
        if not group or "|" not in group:
            continue
        n = int(_num(row.get("n"), 0))
        share = _num(row.get("improved_share"), 0.0)
        share = 0.0 if share < 0 else (1.0 if share > 1 else share)
        ids = [_one_line(i, 80) for i in (row.get("ids") or []) if isinstance(i, str)]
        out.append({
            "group": group,
            "tag": _tag_for(group),
            "n": n,
            "change": _num(row.get("median_impr_change"), 0.0),
            "improved": int(round(share * n)),
            "window": int(_num(row.get("window"), 28)) or 28,
            "ids": [i for i in ids if i],
        })
    return out


def _read_state(path):
    state = {"suppressed": [], "known": []}
    try:
        with open(_sidecar_path(path), "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(MAX_TEXT)
    except FileNotFoundError:
        return state
    except OSError:
        return state
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return state
    if not isinstance(parsed, dict):
        return state
    for key in ("suppressed", "known"):
        values = parsed.get(key)
        if isinstance(values, list):
            state[key] = [v for v in values if isinstance(v, str) and len(v) <= 40][:500]
    return state


def _atomic_write(path, text):
    """Write text to path atomically: tempfile in the same directory, then os.replace."""
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create the directory for {os.path.basename(path)}: {exc}") from None
    fd, tmp = tempfile.mkstemp(prefix=".lessons-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ToolError(f"cannot write {os.path.basename(path)}: {exc}") from None
    return path


def update_lessons(path, board, today, min_n=MIN_N, max_lines=MAX_LINES):
    """Rewrite the Learned section of LESSONS.md from a scoreboard (operator notes untouched)."""
    p = _abs(path)
    t = _as_date(today, "today")
    today_iso = t.isoformat()
    n_min = max(1, int(_num(min_n, MIN_N)))
    n_max = max(1, min(int(_num(max_lines, MAX_LINES)), 200))

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            existing_text = fh.read(MAX_TEXT)
    except FileNotFoundError:
        existing_text = _TEMPLATE
    except OSError as exc:
        raise ToolError(f"cannot read {os.path.basename(p)}: {exc}") from None

    head, body = _split_learned(existing_text)
    existing = _learned_lines(body)
    existing_tags = {tag for tag, _line in existing}

    state = _read_state(p)
    suppressed = list(dict.fromkeys(state["suppressed"]))
    known = state["known"]
    for tag in known:
        if tag not in existing_tags and tag not in suppressed:
            suppressed.append(tag)
    sup_set = set(suppressed)

    rows = _board_rows(board, today_iso)
    entries = []            # [(tag, line)] in final file order
    added, dropped = [], []
    for row in rows:
        if row["tag"] in sup_set:
            dropped.append(row["tag"])
            continue
        if row["n"] < n_min:
            dropped.append(row["tag"])
            continue
        if row["tag"] in {tag for tag, _ in entries}:
            continue
        entries.append((row["tag"], _lesson_line(row["group"], row, today_iso)))
        added.append(row["tag"])
    added_set = set(added)
    kept = []
    for tag, line in existing:
        if tag in sup_set:
            if tag not in dropped:
                dropped.append(tag)
            continue
        if tag in added_set:
            continue
        entries.append((tag, line))
        kept.append(tag)

    while len(entries) > n_max:
        tag, _line = entries.pop()
        dropped.append(tag)
        if tag in kept:
            kept.remove(tag)
        if tag in added_set:
            added.remove(tag)
            added_set.discard(tag)

    text = head + "".join(line + "\n" for _tag, line in entries)
    while entries and len(text.encode("utf-8")) > MAX_BYTES:
        tag, _line = entries.pop()
        text = head + "".join(l + "\n" for _t, l in entries)
        dropped.append(tag)
        if tag in kept:
            kept.remove(tag)
        if tag in added_set:
            added.remove(tag)
            added_set.discard(tag)

    _atomic_write(p, text)
    state_out = {
        "version": 1,
        "suppressed": sorted(sup_set),
        "known": [tag for tag, _line in entries],
    }
    _atomic_write(_sidecar_path(p), json.dumps(state_out, indent=1, sort_keys=True) + "\n")
    return {"added": added, "kept": kept, "dropped": list(dict.fromkeys(dropped))}


def read_lessons(path):
    """The two LESSONS.md sections (<=4000 chars) and the planner weights they imply."""
    out = {"text": "", "weights": {}}
    p = _abs(path)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(MAX_TEXT)
    except FileNotFoundError:
        return out
    except OSError as exc:
        raise ToolError(f"cannot read {os.path.basename(p)}: {exc}") from None
    out["text"] = _sections(raw)[:READ_MAX]
    _head, body = _split_learned(raw)
    weights = {}
    for _tag, line in _learned_lines(body):
        info = _parse_line(line)
        if not info or info["n"] < MIN_N:
            continue
        weights[info["group"]] = round(1.0 + max(-0.5, min(0.5, info["change"])), 4)
        if len(weights) >= MAX_BOARD:
            break
    out["weights"] = weights
    return out


# ---------------------------------------------------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------------------------------------------------
def _ck(n, cond, msg):
    if not cond:
        raise AssertionError(f"learn.selftest: {msg}")
    return n + 1


def _row(rows, pid, window):
    for r in rows:
        if r["id"] == pid and r["window"] == window:
            return r
    raise AssertionError(f"learn.selftest: missing measure row {pid}/{window}")


def _close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def selftest() -> int:
    """In-memory + tempfile checks; returns the number of checks performed (>= 30)."""
    n = 0
    url_a, url_b, url_c = "https://example.com/a/", "https://example.com/b/", "https://example.com/c/"
    today = "2026-03-10"
    pub_a = date(2026, 1, 1)

    rows = []
    for i in range(28):
        d = pub_a - timedelta(days=28 - i)
        if d == date(2025, 12, 20):
            rows.append({"day": d.isoformat(), "page": url_a, "impressions": 30, "clicks": 1, "position": 6})
        else:
            rows.append({"day": d.isoformat(), "page": url_a, "impressions": 10, "clicks": 1, "position": 8})
    for i in range(1, 15):
        rows.append({"day": (pub_a + timedelta(days=i)).isoformat(), "page": url_a,
                     "impressions": 15, "clicks": 2, "position": 5})
    for i in range(15, 29):
        rows.append({"day": (pub_a + timedelta(days=i)).isoformat(), "page": url_a,
                     "impressions": 5, "clicks": 1, "position": 9})
    rows.append({"day": "2026-02-25", "page": url_b, "impressions": 7, "clicks": 1, "position": 20})

    pieces = [
        {"id": "p1", "url": url_a, "content_type": "guide", "query_class": "question", "published_day": "2026-01-01"},
        {"id": "p2", "url": url_b, "content_type": "guide", "query_class": "question", "published_day": "2026-03-01"},
        {"id": "p3", "url": url_c, "content_type": "faq", "query_class": "striking", "published_day": "2026-02-20"},
        {"id": "p4", "url": "https://example.com/d/", "content_type": "faq", "query_class": "striking",
         "published_day": "2023-01-01"},
        {"id": "bad", "url": "https://example.com/e/", "published_day": "not-a-day"},
        "not-a-dict",
    ]
    out = measure(pieces, rows, today)
    n = _ck(n, len(out) == 12, "measure emits 3 windows per valid piece (4 pieces -> 12 rows)")
    r14 = _row(out, "p1", 14)
    r28 = _row(out, "p1", 28)
    r56 = _row(out, "p1", 56)
    n = _ck(n, r14["status"] == "ok", "p1 window 14 should be measured")
    n = _ck(n, r28["status"] == "ok", "p1 window 28 should be measured")
    n = _ck(n, r56["status"] == "ok", "p1 window 56 should be measured")
    n = _ck(n, _close(r14["impr_before"], 300 / 28), "baseline impressions/day should be 300/28")
    n = _ck(n, _close(r14["pos_before"], 7.8), "baseline position must be impression-weighted (7.8)")
    n = _ck(n, _close(r14["impr_after"], 15.0), "14-day window impressions/day should be 15")
    n = _ck(n, _close(r14["impr_change"], 0.4), "14-day change should be +0.4")
    n = _ck(n, _close(r28["impr_after"], 10.0), "28-day window impressions/day should be 10")
    n = _ck(n, abs(r28["impr_change"] + 1 / 15) < 1e-5, "28-day change should be about -6.7%")
    n = _ck(n, _close(r14["clicks_after"], 2.0), "clicks/day after should be 2")
    n = _ck(n, _close(r14["clicks_change"], 1.0), "clicks change should be +100%")
    n = _ck(n, _close(r14["pos_after"], 5.0), "position after should be 5")
    n = _ck(n, r14["baseline_days"] == 28, "baseline should hold 28 observed days")
    n = _ck(n, r14["days"] == 14, "window 14 should hold 14 observed days")
    p2 = _row(out, "p2", 14)
    n = _ck(n, p2["status"] == "pending", "p2 window 14 must be pending")
    n = _ck(n, p2["impr_before"] is None, "pending rows carry no numbers")
    n = _ck(n, _row(out, "p2", 28)["status"] == "pending", "p2 window 28 must be pending")
    p3 = _row(out, "p3", 14)
    n = _ck(n, p3["status"] == "no_data", "p3 must be no_data (no baseline rows)")
    n = _ck(n, p3["baseline_days"] == 0, "p3 baseline day count should be 0")
    n = _ck(n, _row(out, "p4", 56)["status"] == "no_data", "p4 (old piece, no rows) must be no_data")
    n = _ck(n, all(r["id"] != "bad" for r in out), "an invalid published_day is skipped, not guessed")
    n = _ck(n, len(measure([], [], today)) == 0, "empty input gives no rows")
    try:
        measure([], [], "10/03/2026")
        n = _ck(n, False, "a bad today should raise ToolError")
    except ToolError:
        n = _ck(n, True, "a bad today raises ToolError")

    ok_rows = [r for r in out if r["status"] == "ok"]
    board = scoreboard(ok_rows, window=14)
    n = _ck(n, len(board) == 1, "one group at window 14")
    n = _ck(n, board[0]["group"] == "guide|question", "group key is content_type|query_class")
    n = _ck(n, board[0]["n"] == 1, "group n counts measured pieces")
    n = _ck(n, _close(board[0]["median_impr_change"], 0.4), "median change for the single guide")
    n = _ck(n, _close(board[0]["improved_share"], 1.0), "improved share 1/1")
    n = _ck(n, board[0]["ids"] == ["p1"], "ids carry the piece id")
    n = _ck(n, scoreboard(ok_rows, window=99) == [], "no rows for an unmeasured window")
    synth = [
        {"id": "x1", "content_type": "guide", "query_class": "question", "window": 28, "status": "ok",
         "impr_change": 0.10},
        {"id": "x2", "content_type": "guide", "query_class": "question", "window": 28, "status": "ok",
         "impr_change": 0.30},
        {"id": "x3", "content_type": "guide", "query_class": "question", "window": 28, "status": "ok",
         "impr_change": -0.10},
        {"id": "x4", "content_type": "faq", "query_class": "thin_page", "window": 28, "status": "pending",
         "impr_change": 5.0},
        {"id": "x5", "content_type": "faq", "query_class": "thin_page", "window": 28, "status": "ok",
         "impr_change": None},
    ]
    sb = scoreboard(synth, 28)
    n = _ck(n, len(sb) == 1, "pending/None rows are ignored by the scoreboard")
    n = _ck(n, _close(sb[0]["median_impr_change"], 0.10), "median of (0.10,0.30,-0.10) is 0.10")
    n = _ck(n, _close(sb[0]["improved_share"], round(2 / 3, 6)), "improved share should be 2/3")
    n = _ck(n, sb[0]["ids"] == ["x1", "x2", "x3"], "scoreboard keeps every measured id")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "LESSONS.md")
        res = update_lessons(path, sb, today, min_n=3, max_lines=20)
        n = _ck(n, os.path.exists(path), "LESSONS.md is created when missing")
        n = _ck(n, res["added"] == [sb[0]["group"] and _tag_for("guide|question")], "the group is added")
        n = _ck(n, res["dropped"] == [], "nothing dropped on the first run")
        raw = open(path, encoding="utf-8").read()
        head, body = _split_learned(raw)
        n = _ck(n, OPERATOR_HEADER in head and LEARNED_HEADER in head, "both section headers exist")
        n = _ck(n, "Delete a line to drop it for good" in head, "the explanation sits at the top")
        n = _ck(n, "never edits Operator notes" in head, "the explanation mentions operator notes")
        lines = _learned_lines(body)
        n = _ck(n, len(lines) == 1, "exactly one learned line")
        info = _parse_line(lines[0][1])
        n = _ck(n, info is not None, "the learned line parses back")
        n = _ck(n, info["group"] == "guide|question", "line carries the group")
        n = _ck(n, info["change"] == 0.10, "line carries the +10% median")
        n = _ck(n, (info["improved"], info["n"]) == (2, 3), "line carries 2/3 improved")
        n = _ck(n, today in lines[0][1] and "at 28 days" in lines[0][1], "line carries today and the window")
        n = _ck(n, "(evidence: x1, x2, x3)" in lines[0][1], "line lists the evidence ids")
        n = _ck(n, lines[0][1].startswith(f"- [{_tag_for('guide|question')}] guide|question:"), "tag format")
        n = _ck(n, not [f for f in os.listdir(tmp) if f.endswith(".tmp")], "atomic write leaves no temp file")
        n = _ck(n, oct(os.stat(path).st_mode & 0o777) == "0o644", "LESSONS.md is 0644")

        # operator notes are never modified
        op_text = "## Operator notes\n\nKeep this hand-written note.\n   - odd indentation\n\ttab line\n"
        keep_lines = [ln for ln in raw.splitlines() if ln.startswith("- [")]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# LESSONS\n\n" + op_text + "\n" + LEARNED_HEADER + "\n"
                     + "".join(ln + "\n" for ln in keep_lines))
        update_lessons(path, sb, today, min_n=3, max_lines=20)
        raw2 = open(path, encoding="utf-8").read()
        n = _ck(n, op_text in raw2, "operator notes preserved byte-for-byte")
        n = _ck(n, len(_learned_lines(_split_learned(raw2)[1])) == 1, "the learned line is rewritten")

        # min_n respected
        small = [{"group": "faq|striking", "n": 2, "median_impr_change": 0.4, "improved_share": 1.0,
                  "ids": ["y1", "y2"]}]
        res = update_lessons(path, small, today, min_n=3, max_lines=20)
        n = _ck(n, "faq|striking" not in open(path, encoding="utf-8").read(), "n < min_n never becomes a lesson")
        n = _ck(n, _tag_for("faq|striking") in res["dropped"], "below-min_n groups are reported as dropped")
        res = update_lessons(path, small, today, min_n=2, max_lines=20)
        n = _ck(n, "faq|striking" in open(path, encoding="utf-8").read(), "n >= min_n becomes a lesson")

        # a deleted learned line is suppressed and never re-added
        tag = _tag_for("guide|question")
        update_lessons(path, sb, today, min_n=3, max_lines=20)
        n = _ck(n, tag in open(path, encoding="utf-8").read(), "the guide lesson is present again")
        keep = [ln for ln in open(path, encoding="utf-8").read().splitlines() if not ln.startswith(f"- [{tag}]")]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + "\n")
        res = update_lessons(path, sb, today, min_n=3, max_lines=20)
        n = _ck(n, res["added"] == [], "a deleted lesson is not re-added")
        n = _ck(n, tag not in open(path, encoding="utf-8").read(), "the deleted lesson stays out of the file")
        n = _ck(n, tag in (res["dropped"] or []), "the suppressed tag is reported as dropped")
        side = json.loads(open(_sidecar_path(path), encoding="utf-8").read())
        n = _ck(n, tag in side["suppressed"], "the suppression is recorded in the sidecar")
        update_lessons(path, sb, today, min_n=3, max_lines=20)
        n = _ck(n, tag not in open(path, encoding="utf-8").read(), "still suppressed on later runs")

        # as long as it is not deleted, a lesson is kept across runs
        path2 = os.path.join(tmp, "KEPT.md")
        update_lessons(path2, sb, today, min_n=3, max_lines=20)
        res = update_lessons(path2, sb, today, min_n=3, max_lines=20)
        n = _ck(n, res["added"] == [tag], "a kept lesson is refreshed, not duplicated")
        n = _ck(n, len(_learned_lines(_split_learned(open(path2, encoding="utf-8").read())[1])) == 1,
                "refreshing never doubles the line")

        # max_lines cap
        many = [{"group": f"guide|g{i}", "n": 4, "median_impr_change": 0.1 * i, "improved_share": 0.5,
                 "ids": [f"z{i}"]} for i in range(6)]
        path3 = os.path.join(tmp, "CAP.md")
        res = update_lessons(path3, many, today, min_n=3, max_lines=2)
        n = _ck(n, len(_learned_lines(_split_learned(open(path3, encoding="utf-8").read())[1])) == 2,
                "max_lines caps the learned section")
        n = _ck(n, len(res["dropped"]) == 4, "capped groups are reported as dropped")

        # file size cap
        path4 = os.path.join(tmp, "BIG.md")
        with open(path4, "w", encoding="utf-8") as fh:
            fh.write("# LESSONS\n\n## Operator notes\n\n" + ("n" * 20100) + "\n\n" + LEARNED_HEADER + "\n")
        update_lessons(path4, many, today, min_n=3, max_lines=20)
        size = os.path.getsize(path4)
        n = _ck(n, size <= MAX_BYTES, "LESSONS.md never exceeds 20 KB")
        n = _ck(n, "n" * 20100 in open(path4, encoding="utf-8").read(), "operator notes survive the size cap")

        # read_lessons
        path5 = os.path.join(tmp, "READ.md")
        with open(path5, "w", encoding="utf-8") as fh:
            fh.write(
                "# LESSONS\n\n## Operator notes\n\nnote\n\n" + LEARNED_HEADER + "\n"
                f"- [L-aaaaaa] guide|question: impressions/day +90% median at 28 days, 3/3 improved (evidence: a, b, c)"
                f" — {today}\n"
                f"- [L-bbbbbb] faq|striking: impressions/day -90% median at 28 days, 0/3 improved (evidence: d)"
                f" — {today}\n"
                f"- [L-cccccc] guide|thin_page: impressions/day +50% median at 28 days, 2/2 improved (evidence: e)"
                f" — {today}\n"
            )
        read = read_lessons(path5)
        n = _ck(n, read["weights"].get("guide|question") == 1.5, "weight clamps at 1.5")
        n = _ck(n, read["weights"].get("faq|striking") == 0.5, "weight clamps at 0.5")
        n = _ck(n, "guide|thin_page" not in read["weights"], "groups with n < 3 get no weight")
        n = _ck(n, OPERATOR_HEADER in read["text"] and LEARNED_HEADER in read["text"], "text carries both sections")
        n = _ck(n, len(read["text"]) <= READ_MAX, "text is capped at 4000 chars")
        n = _ck(n, read_lessons(os.path.join(tmp, "MISSING.md")) == {"text": "", "weights": {}},
                "a missing file reads as empty")
        w = read_lessons(path2)["weights"]
        n = _ck(n, w.get("guide|question") == 1.1, "weight = 1 + median change (clamped)")
        n = _ck(n, all(0.5 <= v <= 1.5 for v in w.values()), "every weight stays inside 0.5..1.5")
        try:
            update_lessons("", sb, today)
            n = _ck(n, False, "an empty path should raise ToolError")
        except ToolError:
            n = _ck(n, True, "an empty path raises ToolError")

    return n
