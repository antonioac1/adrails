"""DataForSEO credentials from 1Password via ``bin/op-sa`` (OpenClaw Ops vault). Never printed or logged.

Rules (house canon): unattended reads go through op-sa, never a bare ``op``; read the item by ID (a title
lookup has hung forever under cron), always with ``stdin=DEVNULL`` and a hard timeout. Listing items returns
titles and IDs only, no field values, so discovering the ID is safe to do once and cache in the config.

``get()`` returns ``(Creds | None, reason)``. ``None`` with reason "no credentials yet" is a normal state:
the collector exits 0 and does nothing.
"""
import json
import os
import subprocess
import sys

NO_CREDS = "no credentials yet"


class Creds:
    __slots__ = ("login", "password", "item_id")

    def __init__(self, login, password, item_id):
        self.login, self.password, self.item_id = login, password, item_id

    def __repr__(self):
        return f"Creds(login=<redacted>, password=<redacted>, item_id={self.item_id!r})"

    __str__ = __repr__


def _run(runner, argv, timeout):
    try:
        p = runner(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "op-sa timed out"
    except OSError as e:
        return None, f"op-sa could not start ({e.__class__.__name__})"
    if p.returncode != 0:
        # stderr from op names the failure class; it never carries field values. Keep it short anyway.
        first = (p.stderr or "").strip().splitlines()[:1]
        return None, f"op-sa rc={p.returncode}" + (f": {first[0][:160]}" if first else "")
    return p.stdout, None


def find_item_id(cred_cfg, runner=subprocess.run):
    """Look up the item ID by title from `item list` (titles + IDs only). Returns (id|None, reason)."""
    out, err = _run(runner, [cred_cfg["op_sa_path"], "item", "list", "--format", "json"], cred_cfg["timeout_s"])
    if err:
        return None, err
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, "op-sa item list returned non-JSON"
    want = cred_cfg["op_item_title"].strip().lower()
    hits = [i.get("id") for i in items if isinstance(i, dict) and str(i.get("title", "")).strip().lower() == want]
    if not hits:
        return None, NO_CREDS
    if len(hits) > 1:
        return None, f"{len(hits)} items titled '{cred_cfg['op_item_title']}'; set credentials.op_item_id"
    return hits[0], None


def get(cfg, runner=subprocess.run, cache=None):
    """Resolve credentials. ``cache(item_id)`` is called when an ID was discovered by title."""
    cc = cfg["credentials"]
    if not os.path.exists(cc["op_sa_path"]):
        return None, f"{NO_CREDS} (op-sa not found)"
    item_id = cc.get("op_item_id")
    if not item_id:
        item_id, why = find_item_id(cc, runner)
        if not item_id:
            return None, why
        if cache:
            cache(item_id)
    out, err = _run(runner, [cc["op_sa_path"], "item", "get", item_id, "--fields", "label=username,label=password",
                             "--reveal", "--format", "json"], cc["timeout_s"])
    if err:
        return None, err
    try:
        fields = json.loads(out)
    except json.JSONDecodeError:
        return None, "op-sa item get returned non-JSON"
    if isinstance(fields, dict):
        fields = [fields]
    vals = {str(f.get("label", "")).lower(): f.get("value") for f in fields if isinstance(f, dict)}
    login, pw = vals.get("username"), vals.get("password")
    if not login or not pw:
        return None, f"{NO_CREDS} (item {item_id} lacks username/password)"
    return Creds(login, pw, item_id), None


def selftest():
    import tempfile

    class P:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    calls = []

    def fake(items=None, fields=None, rc=0, timeout=False):
        def run(argv, **kw):
            calls.append((argv, kw))
            assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] > 0
            if timeout:
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
            if rc:
                return P(rc, "", "[ERROR] item not found\nmore")
            if argv[2] == "list":
                return P(0, json.dumps(items or []))
            return P(0, json.dumps(fields))
        return run

    n = 0
    sa = tempfile.NamedTemporaryFile(delete=False).name
    cfg = {"credentials": {"op_sa_path": sa, "op_item_title": "DataForSEO API", "op_item_id": None, "timeout_s": 5}}
    c, why = get(cfg, fake(items=[{"id": "x1", "title": "Other"}]))
    assert c is None and why == NO_CREDS; n += 1
    got = []
    c, why = get(cfg, fake(items=[{"id": "abc", "title": "DataForSEO API"}],
                           fields=[{"label": "username", "value": "me@x"}, {"label": "password", "value": "s3cret"}]),
                 cache=got.append)
    assert c and c.login == "me@x" and c.password == "s3cret" and got == ["abc"]; n += 1
    assert "s3cret" not in repr(c) and "me@x" not in str(c); n += 1
    get_argv = calls[-1][0]
    assert get_argv[1:4] == ["item", "get", "abc"] and "--reveal" in get_argv; n += 1
    cfg["credentials"]["op_item_id"] = "abc"
    c, why = get(cfg, fake(timeout=True))
    assert c is None and why == "op-sa timed out"; n += 1
    c, why = get(cfg, fake(rc=1))
    assert c is None and why.startswith("op-sa rc=1") and "more" not in why; n += 1
    c, why = get(cfg, fake(fields=[{"label": "username", "value": "u"}]))
    assert c is None and why.startswith(NO_CREDS); n += 1
    cfg["credentials"]["op_sa_path"] = sa + ".missing"
    c, why = get(cfg, fake())
    assert c is None and why.startswith(NO_CREDS); n += 1
    c2, why = find_item_id({**cfg["credentials"], "op_sa_path": sa},
                           fake(items=[{"id": "a", "title": "DataForSEO API"}, {"id": "b", "title": "dataforseo api"}]))
    assert c2 is None and "2 items" in why; n += 1
    os.unlink(sa)
    return n


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(f"intel.creds: {selftest()} checks")
        print("ALL PASS")
