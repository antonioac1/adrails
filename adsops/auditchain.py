"""adsops.auditchain — tamper-evident, hash-chained audit log.

Every guarded operation (budget change, pause, allowlist edit) appends exactly
one JSON line to an append-only file.  Each line stores the SHA-256 of the
previous line's canonical JSON, so editing, deleting or reordering a line
breaks the chain and ``verify()`` reports the first bad sequence number.

Safety properties: payloads always pass through ``redact()`` (no tokens, keys,
emails or phone numbers are ever written), the file is created mode 0o600, and
every append is flushed + fsync'ed.  Nothing is cached in memory because
another process may append between reads.

Tool usage: ``chain = AuditChain(path)`` -> ``chain.append(event, data)`` after
a successful side effect, ``chain.verify()`` before/after a batch, and
``anchor_digest(path)`` to publish a tiny daily digest (so truncation of the
tail becomes detectable later).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from adsops.errors import RailViolation, ToolError, scrub

ZERO_HASH = "0" * 64
_MAX_DEPTH = 20

# Key names whose *values* are never written to disk.
_SENSITIVE = re.compile(r"(token|secret|password|key|authorization|cookie|email|phone)", re.I)
# Explicit bearer credentials anywhere.
_BEARER = re.compile(r"^Bearer\s+\S+$", re.I)
# Long opaque token-ish blob: needs both letters and a digit so ordinary
# snake_case identifiers (e.g. "update_campaign_budget_settings") survive.
_TOKENISH = re.compile(
    r"^(?=[A-Za-z0-9_\-.]*[A-Za-z])(?=[A-Za-z0-9_\-.]*\d)[A-Za-z0-9_\-.]{24,}$"
)


def _canonical(obj) -> str:
    """Canonical JSON: the single byte representation we hash and store."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_entry(entry: dict) -> str:
    """SHA-256 over the canonical entry with the ``hash`` field removed."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _iso_utc(ts) -> str:
    """Epoch seconds -> 'YYYY-MM-DDTHH:MM:SSZ' (no datetime objects in output)."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        raise ToolError("audit clock returned a non-numeric timestamp")
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _redact_scalar(value):
    if isinstance(value, str) and (_BEARER.match(value) or _TOKENISH.match(value)):
        return "[redacted]"
    return value


def redact(obj, _depth: int = 0):
    """Deep copy of ``obj`` with sensitive values replaced by '[redacted]'.

    Values under keys matching /(token|secret|password|key|authorization|
    cookie|email|phone)/i are replaced wholesale (whole subtree), and any
    bearer/long-opaque string is redacted regardless of its key.  Recursion is
    capped at depth 20 so circular structures cannot blow the stack.
    """
    if _depth > _MAX_DEPTH:
        return "[redacted]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if _SENSITIVE.search(ks):
                out[ks] = "[redacted]"
            elif isinstance(v, (dict, list, tuple)):
                out[ks] = redact(v, _depth + 1)
            else:
                out[ks] = _redact_scalar(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj]
    return obj


class AuditChain:
    """Append-only, hash-linked audit log.  Every read re-reads the file."""

    def __init__(self, path, now=time.time, actor_default="agent"):
        if not path or not isinstance(path, (str, os.PathLike)):
            raise ToolError("audit path must be a non-empty filesystem path")
        self._path = os.fspath(path)
        self._now = now
        self._actor = str(actor_default or "agent")

    # ---- internals -------------------------------------------------------
    def _read_lines(self) -> list:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ToolError(scrub(f"cannot read audit log: {exc}"))

    def _entries(self, strict: bool = True) -> list:
        """Parsed entries in file order.  strict=True refuses corrupt logs."""
        out = []
        for line in self._read_lines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except (ValueError, TypeError):
                if strict:
                    raise ToolError("audit log has an unparsable line; refusing to append")
                continue
            if not isinstance(obj, dict) or not isinstance(obj.get("hash"), str):
                if strict:
                    raise ToolError("audit log has a malformed entry; refusing to append")
                continue
            out.append(obj)
        return out

    def _append_line(self, line: str) -> None:
        parent = os.path.dirname(os.path.abspath(self._path))
        try:
            os.makedirs(parent, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise ToolError(scrub(f"cannot open audit log for append: {exc}"))
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fd = -1  # ownership handed to fdopen; avoid double close below
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        try:  # tighten perms if the file pre-existed with a looser mode
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # ---- public API ------------------------------------------------------
    def append(self, event: str, data: dict, actor=None) -> dict:
        if not isinstance(event, str) or not event.strip():
            raise ToolError("event must be a non-empty string")
        payload = redact({} if data is None else data)
        if not isinstance(payload, dict):
            raise ToolError("data must be a JSON object (dict)")
        try:
            json.dumps(payload)  # fail before touching the file
        except (TypeError, ValueError):
            raise ToolError("data is not JSON-serialisable")

        entries = self._entries(strict=True)  # never chain onto a corrupt log
        entry = {
            "seq": len(entries) + 1,
            "at": _iso_utc(self._now()),
            "actor": str(actor or self._actor),
            "event": event,
            "data": payload,
            "prev": entries[-1]["hash"] if entries else ZERO_HASH,
        }
        entry["hash"] = _hash_entry(entry)
        self._append_line(_canonical(entry) + "\n")
        return entry

    @staticmethod
    def _bad(count: int, seq: int, reason: str) -> dict:
        return {"ok": False, "entries": count, "first_bad_seq": seq,
                "reason": scrub(reason, 200)}

    def verify(self) -> dict:
        """Re-walk the whole file; report the first broken position, if any."""
        prev = ZERO_HASH
        count = 0
        for idx, line in enumerate(self._read_lines(), start=1):
            s = line.strip()
            if not s:
                continue
            expected = count + 1
            try:
                obj = json.loads(s)
            except (ValueError, TypeError):
                return self._bad(count, expected, f"unparsable line {idx}")
            if not isinstance(obj, dict):
                return self._bad(count, expected, f"unparsable line {idx}")
            # Sequence numbers are dense and 1-based: a gap means deletion/reorder.
            if obj.get("seq") != expected:
                return self._bad(count, expected,
                                 f"missing or reordered entry (expected seq {expected})")
            if obj.get("prev") != prev:
                return self._bad(count, expected, f"broken prev link at seq {expected}")
            if not isinstance(obj.get("hash"), str) or _hash_entry(obj) != obj["hash"]:
                return self._bad(count, expected, f"hash mismatch at seq {expected}")
            prev = obj["hash"]
            count += 1
        return {"ok": True, "entries": count, "first_bad_seq": None, "reason": None}

    def tail(self, n: int = 20, event=None, actor=None) -> list:
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ToolError("n must be a non-negative integer")
        if n == 0:
            return []
        entries = [
            e for e in self._entries(strict=False)
            if (event is None or e.get("event") == event)
            and (actor is None or e.get("actor") == actor)
        ]
        return list(reversed(entries[-n:]))

    def head(self):
        entries = self._entries(strict=False)
        return entries[-1] if entries else None


def anchor_digest(path) -> dict:
    """Tiny digest (count + head hash) safe to post to Slack/git daily."""
    chain = AuditChain(path)
    entries = chain._entries(strict=False)
    return {
        "entries": len(entries),
        "head_hash": entries[-1]["hash"] if entries else ZERO_HASH,
    }


def selftest() -> int:
    import tempfile

    n = 0

    def ok(cond, msg="check failed"):
        nonlocal n
        assert cond, msg
        n += 1

    def read_lines(p):
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    def write_lines(p, lines):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # ---- redact (pure) ----
    ok(redact({"password": "hunter2"})["password"] == "[redacted]")
    ok(redact({"api_key": "sk-1"})["api_key"] == "[redacted]")
    ok(redact({"campaign_id": "123"})["campaign_id"] == "123")
    nested = redact({"items": [{"email": "a@b.c"}, {"n": 1}]})
    ok(nested["items"][0]["email"] == "[redacted]" and nested["items"][1]["n"] == 1)
    ok(redact({"note": "Bearer abcdef0123456789abcdef012345"})["note"] == "[redacted]")
    ok(redact({"op": "update_campaign_budget_settings"})["op"] == "update_campaign_budget_settings")
    deep = "leaf"
    for _ in range(25):
        deep = {"k": deep}
    r = redact(deep)
    for _ in range(21):
        r = r["k"]
    ok(r == "[redacted]", "depth limit not enforced")

    with tempfile.TemporaryDirectory() as td:
        # ---- happy path: two appends, verify, head/tail, anchor ----
        p = os.path.join(td, "nested", "audit.jsonl")
        ch = AuditChain(p, now=lambda: 1700000000.0)
        e1 = ch.append("campaign.update", {"campaign": {"id": "1"}, "token": "abc"})
        ok(e1["seq"] == 1 and e1["prev"] == ZERO_HASH)
        ok(e1["at"] == "2023-11-14T22:13:20Z")
        ok(e1["actor"] == "agent" and e1["data"]["token"] == "[redacted]")
        ok(re.fullmatch(r"[0-9a-f]{64}", e1["hash"]) is not None)
        ok(os.path.exists(p), "log file not created")
        if os.name != "nt":
            ok((os.stat(p).st_mode & 0o777) == 0o600, "audit log mode is not 0600")
        e2 = ch.append("budget.change", {"budget": 12.5}, actor="alice")
        ok(e2["seq"] == 2 and e2["prev"] == e1["hash"] and e2["actor"] == "alice")
        ok(ch.verify() == {"ok": True, "entries": 2, "first_bad_seq": None, "reason": None})
        ok(ch.head()["hash"] == e2["hash"])
        ok([x["seq"] for x in ch.tail(10)] == [2, 1])
        ok([x["seq"] for x in ch.tail(10, event="campaign.update")] == [1])
        ok(ch.tail(0) == [] and ch.tail(5, actor="nobody") == [])
        ok(anchor_digest(p) == {"entries": 2, "head_hash": e2["hash"]})
        try:
            ch.append("bad", {"obj": object()})
            ok(False, "non-serialisable data accepted")
        except ToolError:
            ok(True)

        # ---- edited entry ----
        pb = os.path.join(td, "b.jsonl")
        cb = AuditChain(pb)
        for i in range(3):
            cb.append(f"e{i}", {"i": i})
        lines = read_lines(pb)
        rec = json.loads(lines[1])
        rec["data"]["i"] = 99
        lines[1] = _canonical(rec)
        write_lines(pb, lines)
        vb = cb.verify()
        ok(vb["ok"] is False and vb["first_bad_seq"] == 2 and vb["entries"] == 1)

        # ---- deleted middle entry (seq gap) ----
        pc = os.path.join(td, "c.jsonl")
        cc = AuditChain(pc)
        for i in range(3):
            cc.append(f"e{i}", {})
        lines = read_lines(pc)
        del lines[1]
        write_lines(pc, lines)
        vc = cc.verify()
        ok(vc["ok"] is False and vc["first_bad_seq"] == 2)

        # ---- reordered entries ----
        pd = os.path.join(td, "d.jsonl")
        cd = AuditChain(pd)
        for i in range(3):
            cd.append(f"e{i}", {})
        lines = read_lines(pd)
        lines[1], lines[2] = lines[2], lines[1]
        write_lines(pd, lines)
        vd = cd.verify()
        ok(vd["ok"] is False and vd["first_bad_seq"] == 2)

        # ---- unparsable line blocks further appends ----
        pe = os.path.join(td, "e.jsonl")
        ce = AuditChain(pe)
        ce.append("ok", {})
        with open(pe, "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        ve = ce.verify()
        ok(ve["ok"] is False and ve["first_bad_seq"] == 2 and "unparsable" in ve["reason"])
        try:
            ce.append("again", {})
            ok(False, "append accepted a corrupt log")
        except ToolError:
            ok(True)

        # ---- missing file ----
        pm = os.path.join(td, "missing.jsonl")
        cm = AuditChain(pm, now=lambda: 0)
        ok(cm.head() is None and cm.tail() == [])
        ok(cm.verify() == {"ok": True, "entries": 0, "first_bad_seq": None, "reason": None})
        ok(anchor_digest(pm) == {"entries": 0, "head_hash": ZERO_HASH})

    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
