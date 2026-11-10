"""Stored site-audit runs and run-to-run diffing for the AdRails site audit (adsops/seo).

Read-only towards every audited site: this module never touches the network and never runs a request; it only
persists the audit result dicts it is handed as JSON files under a caller-supplied directory
(``base_dir/<safe_host>/<run_id>.json``) and compares two of them. Nothing happens at import time (no disk, no
clock); both are injected through ``RunStore``, and ``diff()`` is a pure function over two run dicts.
Hostile input (bad hostnames, forged run ids, corrupt files, runs that are not dicts) always ends in
``ToolError`` or an empty result, never a traceback. Every returned value is JSON-serialisable.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

from adsops.errors import ToolError

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$")
HOST_JUNK_RE = re.compile(r"[^a-z0-9.-]")
MAX_HOST = 253
MAX_RUN_BYTES = 50 * 1024 * 1024
MAX_DIFF = 500
DEFAULT_PRIORITY = 4
BROKEN_CANONICAL_CODE = "canonical_to_non_200"


def safe_host(host):
    """-> host lowercased, restricted to [a-z0-9.-], no leading dots, max 253 chars; ToolError if unusable."""
    if not isinstance(host, str):
        raise ToolError("host must be a string")
    clean = HOST_JUNK_RE.sub("", host.lower()).lstrip(".")[:MAX_HOST]
    if not clean or ".." in clean or clean in (".", ".."):
        raise ToolError("host %r is not a usable hostname" % (host[:80],))
    return clean


def _check_run_id(run_id):
    """-> the run id if it is a well-formed id, else ToolError (blocks path traversal)."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ToolError("invalid run id %r" % (str(run_id)[:80],))
    return run_id


def _int(value):
    """-> int(value) for ints/floats (None for anything else, incl. NaN)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return int(value)
    return None


def _stamp(seconds):
    """-> UTC 'YYYYMMDDTHHMMSSZ' from epoch seconds."""
    try:
        moment = datetime.fromtimestamp(float(seconds), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ToolError("now() must return epoch seconds")
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _iso(seconds):
    """-> UTC ISO-8601 timestamp from epoch seconds."""
    try:
        return datetime.fromtimestamp(float(seconds), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        raise ToolError("now() must return epoch seconds")


def _ensure_dir(path):
    """Create `path` (and parents) with mode 0o700."""
    if os.path.isdir(path):
        return
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ToolError("could not create run directory: %s" % (exc,))


class RunStore:
    """JSON-file store for audit runs: base_dir/<safe_host>/<run_id>.json, atomic writes, newest `keep` kept."""

    def __init__(self, base_dir, keep=30, now=time.time):
        try:
            base = os.fspath(base_dir)
        except TypeError:
            raise ToolError("base_dir must be a path")
        if not isinstance(base, str) or not base.strip():
            raise ToolError("base_dir must be a non-empty path string")
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise ToolError("keep must be a positive integer")
        if not callable(now):
            raise ToolError("now must be a callable returning epoch seconds")
        self.base_dir = os.path.abspath(base)
        self.keep = keep
        self.now = now
        self._seq = 0

    # -- paths and ids -------------------------------------------------------------------------------------
    def _host_dir(self, host):
        return os.path.join(self.base_dir, safe_host(host))

    def _new_run_id(self, stamp, host_dir):
        """-> a run id not yet on disk (counter bumped until it is free)."""
        for _ in range(0x10000):
            run_id = "%s-%04x" % (stamp, self._seq & 0xffff)
            self._seq += 1
            if not os.path.exists(os.path.join(host_dir, run_id + ".json")):
                return run_id
        raise ToolError("could not allocate a unique run id")

    @staticmethod
    def _ids(host_dir):
        """-> run ids in that directory, newest first (missing directory -> [])."""
        try:
            names = os.listdir(host_dir)
        except OSError:
            return []
        out = [name[:-5] for name in names if name.endswith(".json") and RUN_ID_RE.match(name[:-5])]
        out.sort(reverse=True)
        return out

    # -- writing -------------------------------------------------------------------------------------------
    @staticmethod
    def _write(path, payload):
        """Atomically write `payload` to `path` via a 0o600 temp file in the same directory."""
        folder = os.path.dirname(path)
        tmp = os.path.join(folder, ".tmp-" + os.path.basename(path))
        fd = -1
        try:
            try:
                os.remove(tmp)
            except OSError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ToolError("could not write run file: %s" % (exc,))
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def save(self, run):
        """Persist `run` (setting its run_id/at) and return the new run id."""
        if not isinstance(run, dict):
            raise ToolError("run must be a dict")
        host = safe_host(run.get("host"))
        host_dir = self._host_dir(host)
        _ensure_dir(host_dir)
        run_id = self._new_run_id(_stamp(self.now()), host_dir)
        run["run_id"] = run_id
        run["host"] = host
        if not isinstance(run.get("at"), str) or not run.get("at"):
            run["at"] = _iso(self.now())
        try:
            payload = json.dumps(run, ensure_ascii=False, sort_keys=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolError("run is not JSON-serialisable: %s" % (exc,))
        self._write(os.path.join(host_dir, run_id + ".json"), payload)
        self._prune(host)
        return run_id

    def _prune(self, host):
        """Delete every run of `host` older than the newest `keep`."""
        host_dir = self._host_dir(host)
        for run_id in self._ids(host_dir)[self.keep:]:
            try:
                os.remove(os.path.join(host_dir, run_id + ".json"))
            except OSError:
                pass

    # -- reading -------------------------------------------------------------------------------------------
    def list(self, host):
        """-> run ids for `host`, newest first."""
        return self._ids(self._host_dir(safe_host(host)))

    def load(self, host, run_id):
        """-> the stored run dict; ToolError on a bad id, a missing file, an oversized file or corrupt JSON."""
        host_dir = self._host_dir(safe_host(host))
        rid = _check_run_id(run_id)
        path = os.path.join(host_dir, rid + ".json")
        try:
            size = os.path.getsize(path)
        except OSError:
            raise ToolError("stored run %s not found" % (rid,))
        if size > MAX_RUN_BYTES:
            raise ToolError("stored run %s is larger than 50 MB" % (rid,))
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_RUN_BYTES + 1)
            if len(raw) > MAX_RUN_BYTES:
                raise ToolError("stored run %s is larger than 50 MB" % (rid,))
            data = json.loads(raw.decode("utf-8", "replace"))
        except ToolError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise ToolError("stored run %s is unreadable" % (rid,))
        if not isinstance(data, dict):
            raise ToolError("stored run %s is unreadable" % (rid,))
        data.setdefault("run_id", rid)
        return data

    def latest(self, host, before=None):
        """-> the newest run dict for `host`, or the newest one whose run_id is < before; None if none."""
        host = safe_host(host)
        if before is not None:
            before = _check_run_id(before)
        for run_id in self.list(host):
            if before is None or run_id < before:
                return self.load(host, run_id)
        return None


# -- diffing -----------------------------------------------------------------------------------------------
def pairs(run):
    """-> set of (code, url) pairs for every row of a run and each of its urls (hostile input tolerated)."""
    out = set()
    rows = run.get("rows") if isinstance(run, dict) else None
    if not isinstance(rows, (list, tuple)):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        urls = row.get("urls")
        if not isinstance(code, str) or not code or not isinstance(urls, (list, tuple)):
            continue
        for url in urls:
            if isinstance(url, str) and url:
                out.add((code, url))
    return out


def _priorities(run):
    """-> {code: lowest priority seen} for the rows of a run."""
    out = {}
    rows = run.get("rows") if isinstance(run, dict) else None
    if not isinstance(rows, (list, tuple)):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        prio = _int(row.get("priority"))
        if isinstance(code, str) and code and prio is not None:
            if code not in out or prio < out[code]:
                out[code] = prio
    return out


def _pages(run):
    """-> {url: page dict} for a run (non-dict pages and non-string urls dropped)."""
    pages = run.get("pages") if isinstance(run, dict) else None
    if not isinstance(pages, dict):
        return {}
    return {url: page for url, page in pages.items() if isinstance(url, str) and isinstance(page, dict)}


def _run_id_of(run):
    run_id = run.get("run_id") if isinstance(run, dict) else None
    return run_id if isinstance(run_id, str) else None


def _row_count(run):
    rows = run.get("rows") if isinstance(run, dict) else None
    return len(rows) if isinstance(rows, (list, tuple)) else 0


def _regressions(prev, cur, lcp_jump_ms, lcp_jump_ratio, inlink_drop_ratio):
    """-> regression dicts for pages seen in both runs, plus page_gone for vanished indexable pages."""
    out = []
    if prev is None:
        return out
    prev_pages = _pages(prev)
    cur_pages = _pages(cur)
    for url in sorted(prev_pages):
        before = prev_pages[url]
        after = cur_pages.get(url)
        if after is None:
            if _int(before.get("status")) == 200 and before.get("indexable") is True:
                out.append({"kind": "page_gone", "url": url, "before": 200, "after": None})
            continue
        new_noindex = (not bool(before.get("noindex"))) and bool(after.get("noindex"))
        if new_noindex:
            out.append({"kind": "new_noindex", "url": url, "before": False, "after": True})
        if before.get("indexable") is True and after.get("indexable") is False and not new_noindex:
            out.append({"kind": "became_non_indexable", "url": url, "before": True, "after": False})
        status_before = _int(before.get("status"))
        status_after = _int(after.get("status"))
        if status_before == 200 and status_after is not None and (status_after >= 400 or status_after == 0):
            out.append({"kind": "status_error", "url": url, "before": status_before, "after": status_after})
        canon_before = before.get("canonical")
        canon_after = after.get("canonical")
        if isinstance(canon_before, str) and canon_before and canon_before != canon_after:
            out.append({"kind": "canonical_changed", "url": url, "before": canon_before, "after": canon_after})
        inlinks_before = _int(before.get("inlinks"))
        inlinks_after = _int(after.get("inlinks"))
        if (inlinks_before is not None and inlinks_after is not None and inlinks_before >= 2
                and inlinks_after <= inlinks_before * (1 - inlink_drop_ratio)):
            out.append({"kind": "lost_inlinks", "url": url, "before": inlinks_before, "after": inlinks_after})
        lcp_before = _int(before.get("lcp_ms"))
        lcp_after = _int(after.get("lcp_ms"))
        if (lcp_before is not None and lcp_after is not None and lcp_after - lcp_before >= lcp_jump_ms
                and lcp_after >= lcp_before * (1 + lcp_jump_ratio)):
            out.append({"kind": "lcp_jump", "url": url, "before": lcp_before, "after": lcp_after})
        depth_before = _int(before.get("depth"))
        depth_after = _int(after.get("depth"))
        if depth_before is not None and depth_after is not None and depth_after >= depth_before + 2:
            out.append({"kind": "depth_increase", "url": url, "before": depth_before, "after": depth_after})
    return out


def diff(prev, cur, lcp_jump_ms=500, lcp_jump_ratio=0.2, inlink_drop_ratio=0.5):
    """-> new/fixed issue pairs plus regressions between two stored runs (prev may be None)."""
    if not isinstance(cur, dict):
        raise ToolError("current run must be a dict")
    if prev is not None and not isinstance(prev, dict):
        raise ToolError("previous run must be a dict or None")
    prev_pairs = pairs(prev) if prev is not None else set()
    cur_pairs = pairs(cur)
    prios = _priorities(cur)
    new_items = [{"code": code, "url": url, "priority": prios.get(code, DEFAULT_PRIORITY)}
                 for code, url in cur_pairs - prev_pairs]
    new_items.sort(key=lambda item: (item["priority"], item["code"], item["url"]))
    fixed_items = [{"code": code, "url": url} for code, url in prev_pairs - cur_pairs]
    fixed_items.sort(key=lambda item: (item["code"], item["url"]))
    regressions = _regressions(prev, cur, lcp_jump_ms, lcp_jump_ratio, inlink_drop_ratio)
    for item in new_items:
        if item["code"] == BROKEN_CANONICAL_CODE:
            regressions.append({"kind": "broken_canonical", "url": item["url"], "before": None,
                                "after": item["code"]})
    regressions.sort(key=lambda reg: (reg["kind"], reg["url"]))
    return {
        "previous": _run_id_of(prev),
        "current": _run_id_of(cur),
        "new": new_items[:MAX_DIFF],
        "fixed": fixed_items[:MAX_DIFF],
        "regressions": regressions[:MAX_DIFF],
        "summary": {"new": len(new_items), "fixed": len(fixed_items), "regressions": len(regressions),
                    "issues_before": _row_count(prev), "issues_after": _row_count(cur)},
    }


def selftest() -> int:
    """Offline checks for the run store and the diff (tempfile.mkdtemp only)."""
    import tempfile

    n = 0
    base = tempfile.mkdtemp(prefix="adsops-seo-runs-")
    clock = {"t": 1735689600.0}  # 2025-01-01T00:00:00Z
    store = RunStore(base, keep=3, now=lambda: clock["t"])

    def chk(cond, msg):
        nonlocal n
        assert cond, "seo.runs.selftest: " + msg
        n += 1

    # --- safe_host -----------------------------------------------------------------------------------------
    chk(safe_host("  WWW.Example.COM  ") == "www.example.com", "host is lowercased, junk removed, dots stripped")
    chk(safe_host("...ex.test") == "ex.test", "leading dots are removed")
    chk(len(safe_host("a" * 400)) == 253, "hosts are capped at 253 chars")
    for bad in ("", "   ", "...", "..", "ex..test", None, 5):
        try:
            safe_host(bad)
            chk(False, "safe_host(%r) must raise" % (bad,))
        except ToolError:
            chk(True, "safe_host rejects %r" % (bad,))

    # --- save / list / load / latest round trip ------------------------------------------------------------
    run1 = {"host": "Example.COM", "settings": {"max_pages": 10}, "summary": {}, "rows": [], "pages": {}}
    rid1 = store.save(run1)
    chk(bool(RUN_ID_RE.match(rid1)), "save returns a well-formed run id")
    chk(run1["run_id"] == rid1 and isinstance(run1["at"], str) and "T" in run1["at"],
        "save stamps run_id and at on the run")
    chk(run1["host"] == "example.com" and store.list("example.com") == [rid1], "the run lands in the safe host dir")
    chk(store.load("example.com", rid1)["settings"] == {"max_pages": 10}, "load returns what save wrote")
    path1 = os.path.join(base, "example.com", rid1 + ".json")
    chk(os.stat(path1).st_mode & 0o777 == 0o600, "run files are written with mode 0o600")
    chk(os.path.isdir(os.path.join(base, "example.com")), "the host directory exists")

    # --- same-second saves get distinct ids ---------------------------------------------------------------
    rid2 = store.save({"host": "example.com"})
    rid3 = store.save({"host": "example.com"})
    chk(len({rid1, rid2, rid3}) == 3, "three saves in the same second give three distinct ids")
    chk(store.list("example.com") == [rid3, rid2, rid1], "list returns run ids newest first")
    chk(store.latest("example.com")["run_id"] == rid3, "latest returns the newest run")
    chk(store.latest("example.com", before=rid3)["run_id"] == rid2, "latest(before=) skips newer runs")
    chk(store.latest("example.com", before=rid1) is None, "latest(before=) returns None when nothing is older")
    chk(store.list("nothing.test") == [] and store.latest("nothing.test") is None,
        "an unknown host lists nothing and has no latest")

    # --- pruning ------------------------------------------------------------------------------------------
    clock["t"] += 60
    more = [store.save({"host": "example.com"}) for _ in range(3)]
    chk(len(store.list("example.com")) == 3, "pruning keeps exactly `keep` runs")
    chk(store.list("example.com")[0] == more[-1] and rid1 not in store.list("example.com"),
        "pruning drops the oldest runs first")
    chk(not os.path.exists(path1), "the pruned file is gone from disk")

    # --- hostile input ------------------------------------------------------------------------------------
    for bad_rid in ("../x", "a/b", "", "20250101T000000Z-zzzz", "20250101T000000Z-00000", None, 7):
        try:
            store.load("example.com", bad_rid)
            chk(False, "load(%r) must raise" % (bad_rid,))
        except ToolError:
            chk(True, "load rejects run id %r" % (bad_rid,))
    try:
        store.load("..", rid2)
        chk(False, "load with a traversal host must raise")
    except ToolError:
        chk(True, "load with a traversal host raises ToolError")
    try:
        store.save({"host": "ex..test"})
        chk(False, "save with a dotted-dot host must raise")
    except ToolError:
        chk(True, "save with a dotted-dot host raises ToolError")
    try:
        store.save(["not", "a", "dict"])
        chk(False, "save of a non-dict must raise")
    except ToolError:
        chk(True, "save of a non-dict raises ToolError")

    corrupt = os.path.join(base, "example.com", "20250101T000000Z-0abc.json")
    with open(corrupt, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    try:
        store.load("example.com", "20250101T000000Z-0abc")
        chk(False, "corrupt JSON must raise")
    except ToolError as exc:
        chk("is unreadable" in str(exc), "corrupt JSON raises the unreadable message")
    fresh = store.save({"host": "example.com"})
    try:
        chk(store.load("example.com", fresh)["run_id"] == fresh, "a valid id still loads next to the corrupt file")
    except ToolError:
        chk(False, "a valid id still loads next to the corrupt file")
    big = os.path.join(base, "example.com", "20250101T000000Z-0abd.json")
    with open(big, "wb") as handle:
        handle.write(b"{}")
    os.truncate(big, MAX_RUN_BYTES + 1)
    try:
        store.load("example.com", "20250101T000000Z-0abd")
        chk(False, "a file over 50 MB must raise")
    except ToolError as exc:
        chk("50 MB" in str(exc), "a file over 50 MB raises the size error")

    # --- diff: no previous run ----------------------------------------------------------------------------
    cur = {"run_id": "20250102T000000Z-0001", "host": "example.com", "at": "2025-01-02T00:00:00Z",
           "settings": {}, "summary": {},
           "rows": [{"id": "r1", "code": "title_missing", "priority": 2, "urls": ["https://ex.test/a/"],
                     "pages_affected": 1, "issue": "titles"},
                    {"id": "r2", "code": "h1_missing", "priority": 3, "urls": ["https://ex.test/b/", 5],
                     "pages_affected": 1, "issue": "h1"}],
           "pages": {}}
    done = diff(None, cur)
    chk(done["previous"] is None and done["current"] == cur["run_id"], "the diff names both runs")
    chk(len(done["new"]) == 2 and done["fixed"] == [] and done["regressions"] == [],
        "with no previous run every pair is new and nothing is fixed")
    chk(done["new"][0]["code"] == "title_missing" and done["new"][0]["priority"] == 2,
        "new pairs are sorted by priority and carry their priority")
    chk(done["summary"] == {"new": 2, "fixed": 0, "regressions": 0, "issues_before": 0, "issues_after": 2},
        "the summary counts new/fixed/regressions/rows")
    chk(len(pairs(cur)) == 2, "pairs() skips non-string urls")

    # --- diff: new and fixed pairs ------------------------------------------------------------------------
    prev = {"run_id": "20250101T000000Z-0001", "host": "example.com", "at": "2025-01-01T00:00:00Z",
            "settings": {}, "summary": {},
            "rows": [{"id": "r0", "code": "title_missing", "priority": 2, "urls": ["https://ex.test/a/"],
                      "pages_affected": 1, "issue": "titles"},
                     {"id": "r9", "code": "description_missing", "priority": 4,
                      "urls": ["https://ex.test/z/"], "pages_affected": 1, "issue": "desc"}],
            "pages": {}}
    done = diff(prev, cur)
    chk([(i["code"], i["url"]) for i in done["new"]] == [("h1_missing", "https://ex.test/b/")],
        "pairs only in the current run are new")
    chk([(i["code"], i["url"]) for i in done["fixed"]] == [("description_missing", "https://ex.test/z/")],
        "pairs only in the previous run are fixed")
    chk(done["summary"]["issues_before"] == 2 and done["summary"]["issues_after"] == 2, "row counts come from both runs")

    # --- diff: every regression kind ----------------------------------------------------------------------
    def page(status=200, indexable=True, noindex=False, canonical=None, inlinks=5, depth=1, lcp_ms=1000):
        return {"status": status, "indexable": indexable, "noindex": noindex, "canonical": canonical,
                "inlinks": inlinks, "depth": depth, "lcp_ms": lcp_ms}

    kinds = {
        "https://ex.test/noindex/": (page(), page(noindex=True)),
        "https://ex.test/nonindexable/": (page(), page(indexable=False)),
        "https://ex.test/broken/": (page(), page(status=500)),
        "https://ex.test/canon/": (page(canonical="https://ex.test/canon/"), page(canonical="https://ex.test/x/")),
        "https://ex.test/inlinks/": (page(inlinks=6), page(inlinks=1)),
        "https://ex.test/lcp/": (page(lcp_ms=1000), page(lcp_ms=2000)),
        "https://ex.test/depth/": (page(depth=1), page(depth=3)),
        "https://ex.test/gone/": (page(), None),
        "https://ex.test/fine/": (page(), page()),
        "https://ex.test/ok404/": (page(status=404, indexable=False), None),
    }
    prev_x = {"run_id": "20250101T000000Z-0001", "rows": [], "pages": {}}
    cur_x = {"run_id": "20250102T000000Z-0001", "rows": [], "pages": {}}
    for url, (before_page, after_page) in kinds.items():
        prev_x["pages"][url] = before_page
        if after_page is not None:
            cur_x["pages"][url] = after_page
    cur_x["rows"] = [{"id": "c1", "code": "canonical_to_non_200", "priority": 1,
                      "urls": ["https://ex.test/broken/"], "pages_affected": 1, "issue": "canonical"}]
    done = diff(prev_x, cur_x)
    seen = {(reg["kind"], reg["url"]) for reg in done["regressions"]}
    for kind, url in (("new_noindex", "https://ex.test/noindex/"),
                      ("became_non_indexable", "https://ex.test/nonindexable/"),
                      ("status_error", "https://ex.test/broken/"),
                      ("canonical_changed", "https://ex.test/canon/"),
                      ("lost_inlinks", "https://ex.test/inlinks/"),
                      ("lcp_jump", "https://ex.test/lcp/"),
                      ("depth_increase", "https://ex.test/depth/"),
                      ("page_gone", "https://ex.test/gone/"),
                      ("broken_canonical", "https://ex.test/broken/")):
        chk((kind, url) in seen, "diff reports %s for %s" % (kind, url))
    chk(not any(reg["url"] == "https://ex.test/fine/" for reg in done["regressions"]), "an unchanged page is no regression")
    chk(not any(reg["url"] == "https://ex.test/ok404/" for reg in done["regressions"]),
        "a vanished 404 page is not page_gone")
    chk(len(done["regressions"]) == 9, "one regression per changed page and kind")
    json.dumps(done)
    chk(True, "the diff is JSON-serialisable")

    # --- diff: thresholds and hostile input ---------------------------------------------------------------
    small = {"run_id": "20250102T000000Z-0002", "rows": [],
             "pages": {"u": page(lcp_ms=1100, depth=2, inlinks=5)}}
    small_prev = {"run_id": "20250101T000000Z-0002", "rows": [],
                  "pages": {"u": page(lcp_ms=1000, depth=1, inlinks=4)}}
    chk(diff(small_prev, small)["regressions"] == [], "small LCP/depth/inlink moves are not regressions")
    strong = {"run_id": "20250102T000000Z-0003", "rows": [],
              "pages": {"u": page(lcp_ms=1700, depth=3, inlinks=2)}}
    chk(len(diff(small_prev, strong)["regressions"]) == 3, "crossing every threshold gives three regressions")
    chk(diff(None, {})["summary"] == {"new": 0, "fixed": 0, "regressions": 0, "issues_before": 0,
                                      "issues_after": 0}, "an empty run diffs to an empty result")
    hostile = {"run_id": 5, "rows": "nope", "pages": {"https://ex.test/a/": None, "https://ex.test/b/": {"status": "500"},
                                                     7: {"status": 200}}}
    hostile_prev = {"run_id": None, "rows": [None, 5, {"code": None, "urls": "x"}],
                    "pages": {"https://ex.test/a/": {"status": 200, "indexable": True, "noindex": None}}}
    out = diff(hostile_prev, hostile)
    chk(out["previous"] is None and out["current"] is None, "non-string run ids become None")
    chk(isinstance(out["regressions"], list) and json.dumps(out), "a hostile diff still returns JSON")
    for bad in ([], "run", 7):
        try:
            diff(bad, cur)
            chk(False, "diff with a bad previous run must raise")
        except ToolError:
            chk(True, "diff rejects a non-dict previous run")
    try:
        diff(None, None)
        chk(False, "diff with a None current run must raise")
    except ToolError:
        chk(True, "diff rejects a None current run")

    return n
