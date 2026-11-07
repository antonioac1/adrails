"""Per-request usage metering: append-only JSON lines, never arguments or results."""
import json
import os
import tempfile
import time
from datetime import datetime, timezone

from .errors import scrub


def _iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _int(value, default=0):
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


def _error_class(error):
    """A short, secret-free class/message label for a failed call."""
    if error is None:
        return None
    label = scrub(error, 200).strip()
    if not label:
        return "Error"
    head = label.split(":", 1)[0].strip()
    return (head or "Error")[:120]


class Meter:
    """Writes one JSON line per tool call and aggregates them on demand."""

    def __init__(self, path, tenant, now=time.time):
        self.path = path
        self.tenant = tenant
        self.now = now

    def _file(self):
        return os.path.expanduser(self.path)

    def record(self, tool, ok, ms, units=1, ops=0, error_class=None):
        entry = {
            "at": _iso(self.now()),
            "tenant": self.tenant,
            "tool": tool,
            "ok": bool(ok),
            "ms": round(float(ms), 1),
            "units": _int(units, 1),
            "ops": _int(ops, 0),
            "error_class": error_class,
        }
        path = self._file()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def summary(self, since_iso=None):
        by_tool = {}
        path = self._file()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # skip malformed lines
                    if not isinstance(entry, dict):
                        continue
                    tool = entry.get("tool")
                    if not isinstance(tool, str) or not tool:
                        continue
                    if entry.get("tenant", self.tenant) != self.tenant:
                        continue
                    at = entry.get("at")
                    if since_iso is not None:
                        if not isinstance(at, str) or at < since_iso:
                            continue
                    ms = entry.get("ms", 0.0)
                    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
                        ms = 0.0
                    stat = by_tool.setdefault(tool, {
                        "calls": 0, "ok": 0, "errors": 0, "units": 0, "ops": 0, "_ms": []})
                    stat["calls"] += 1
                    if entry.get("ok"):
                        stat["ok"] += 1
                    else:
                        stat["errors"] += 1
                    stat["units"] += _int(entry.get("units"), 0)
                    stat["ops"] += _int(entry.get("ops"), 0)
                    stat["_ms"].append(float(ms))
        out = {}
        for tool, stat in by_tool.items():
            samples = stat.pop("_ms")
            stat["ms_p50"] = round(_median(samples), 1)
            out[tool] = stat
        return {self.tenant: out}


def hook(meter):
    """Build an ``on_call`` for :class:`adsops.protocol.Server` that meters every call."""

    def on_call(tool, ok, ms, result=None, error=None):
        ops = 0
        if isinstance(result, dict):
            ops = _int(result.get("ops_count", 0), 0)
        return meter.record(tool, ok, ms, units=1, ops=ops,
                            error_class=None if ok else _error_class(error))

    return on_call


def selftest():
    total = 0

    def ok(cond, detail="assertion failed"):
        nonlocal total
        assert cond, detail
        total += 1

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "deeper", "meter.jsonl")
        meter = Meter(path, "t1", now=lambda: 1735787045.0)  # 2025-01-02T03:04:05Z

        # record shape + parent dir creation
        entry = meter.record("tool.a", True, 12.345, units=2, ops=7)
        ok(os.path.isfile(path), "parent dirs not created")
        ok(entry["at"] == "2025-01-02T03:04:05Z", entry["at"])
        ok(entry["tenant"] == "t1" and entry["tool"] == "tool.a" and entry["ok"] is True)
        ok(entry["ms"] == 12.3 and isinstance(entry["ms"], float))
        ok(entry["units"] == 2 and entry["ops"] == 7 and entry["error_class"] is None)
        ok(set(entry) == {"at", "tenant", "tool", "ok", "ms", "units", "ops", "error_class"})
        ok(len(entry) == 8)

        # nothing about arguments/results is ever written
        raw = open(path, encoding="utf-8").read().strip()
        ok(len(raw.splitlines()) == 1)
        ok("args" not in raw and "arguments" not in raw and "result" not in raw)

        # more calls, including failures
        meter.record("tool.a", True, 30.0, ops=1)
        meter.record("tool.a", False, 10.0, error_class="ToolError")
        meter.record("tool.b", True, 4.0, ops=3)
        summ = meter.summary()
        ok(list(summ) == ["t1"])
        a = summ["t1"]["tool.a"]
        ok(a["calls"] == 3 and a["ok"] == 2 and a["errors"] == 1)
        ok(a["units"] == 4 and a["ops"] == 8, a)
        ok(a["ms_p50"] == 12.3, a["ms_p50"])
        ok(summ["t1"]["tool.b"]["calls"] == 1 and summ["t1"]["tool.b"]["ms_p50"] == 4.0)
        ok(set(a) == {"calls", "ok", "errors", "units", "ops", "ms_p50"})

        # malformed lines are skipped
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
            fh.write("[1, 2, 3]\n")
            fh.write("\n")
        summ = meter.summary()
        ok(summ["t1"]["tool.a"]["calls"] == 3)

        # since filter (ISO-8601 UTC seconds, lexicographic)
        old = Meter(path, "t1", now=lambda: 1600000000.0)  # 2020-09-13T12:26:40Z
        old.record("tool.a", True, 99.0)
        old.record("tool.c", True, 5.0)
        ok(meter.summary()["t1"]["tool.a"]["calls"] == 4)
        filtered = meter.summary(since_iso="2025-01-01T00:00:00Z")
        ok(filtered["t1"]["tool.a"]["calls"] == 3)
        ok("tool.c" not in filtered["t1"])
        ok(meter.summary(since_iso="2020-09-13T12:26:40Z")["t1"]["tool.c"]["calls"] == 1)

        # hook(): ops from ops_count, error_class on failure
        hpath = os.path.join(tmp, "hook", "meter.jsonl")
        hmeter = Meter(hpath, "t2", now=lambda: 1735787045.0)
        on_call = hook(hmeter)
        on_call("tool.x", True, 3.5, {"ops_count": 4}, None)
        on_call("tool.y", True, 1.0, "not-a-dict", None)
        on_call("tool.z", False, 2.0, None, "ToolError: nope")
        rows = [json.loads(line) for line in open(hpath, encoding="utf-8")]
        ok(rows[0]["ops"] == 4 and rows[0]["units"] == 1 and rows[0]["ok"] is True)
        ok(rows[1]["ops"] == 0)
        ok(rows[2]["error_class"] == "ToolError" and rows[2]["ok"] is False)
        hsumm = hmeter.summary()
        ok(hsumm["t2"]["tool.x"]["ops"] == 4 and hsumm["t2"]["tool.z"]["errors"] == 1)

        # a secret in the error text never reaches the log
        on_call("tool.s", False, 1.0, None, "boom ya29.abcDEF123-_x")
        last = open(hpath, encoding="utf-8").read().splitlines()[-1]
        ok("ya29" not in last and "REDACTED" in last)

        # missing file -> empty summary
        empty = Meter(os.path.join(tmp, "nothing.jsonl"), "t3").summary()
        ok(empty == {"t3": {}})

    return total
