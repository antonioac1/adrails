"""Supply-chain integrity helpers for the adsops release pipeline.

Builds a deterministic sha256 manifest of the package tree, digests that
manifest, and emits a CycloneDX 1.5 SBOM whose serial number is a UUIDv5
derived from (name, version, manifest digest) so identical inputs always
produce identical output.  ``verify`` re-hashes the tree to report drifted,
missing and unexpected files.

Safety: read-only, stdlib only, no network and no clock access unless a
``now`` callable is injected; every filesystem access goes through injectable
``read``/``walker`` callables so tools and tests can mock it.  Failures raise
ToolError with scrubbed messages and never leak file contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import uuid

from adsops.errors import ToolError, scrub

__all__ = [
    "file_manifest",
    "manifest_digest",
    "cyclonedx",
    "verify",
    "selftest",
]

DEFAULT_EXT = (".py", ".json", ".md", ".toml")
DEFAULT_EXCL = ("__pycache__", ".git", "tests/fixtures")


def _read_bytes(path: str) -> bytes:
    """Default reader: raw bytes, no decoding (hashing must be byte-exact)."""
    with open(path, "rb") as fh:
        return fh.read()


def _join(root, rel: str) -> str:
    """Join a posix relative path onto root using the local separator."""
    return os.path.join(os.fspath(root), *rel.split("/"))


def _as_bytes(data) -> bytes:
    """Normalise whatever an injected reader returned into bytes."""
    if data is None:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise ToolError("reader returned unsupported data type")


def file_manifest(root, include_ext=DEFAULT_EXT, exclude_dirs=DEFAULT_EXCL,
                  read=None, walker=None):
    """Hash every matching file under ``root`` into a sorted manifest list.

    Entries look like ``{"path": "adsops/x.py", "sha256": "<hex>", "bytes": 123}``
    with posix relative paths, sorted by path for byte-stable output.  Directory
    entries in ``exclude_dirs`` may be bare names ("__pycache__") or relative
    paths ("tests/fixtures").
    """
    walker = walker or os.walk
    read = read or _read_bytes
    root = os.fspath(root)
    if not os.path.isdir(root):
        raise ToolError(f"file_manifest: not a directory: {scrub(str(root), 200)}")
    exts = tuple(include_ext or ())
    excluded = set(exclude_dirs or ())
    out = []
    for dirpath, dirnames, filenames in walker(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_posix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        # Prune excluded directories in place: os.walk honours dirnames[:].
        kept = []
        for d in dirnames:
            child = posixpath.join(rel_posix, d) if rel_posix else d
            if d in excluded or child in excluded:
                continue
            kept.append(d)
        dirnames[:] = sorted(kept)
        for fn in sorted(filenames):
            if not exts or not fn.endswith(exts):
                continue
            data = _as_bytes(read(os.path.join(dirpath, fn)))
            rel = posixpath.join(rel_posix, fn) if rel_posix else fn
            out.append({
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            })
    out.sort(key=lambda entry: entry["path"])
    return out


def manifest_digest(manifest) -> str:
    """sha256 over canonical (sorted-key, compact) JSON of the manifest list."""
    if not isinstance(manifest, (list, tuple)):
        raise ToolError("manifest_digest: manifest must be a list")
    canon = json.dumps(list(manifest), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _parse_dep(dep):
    """Accept "name==1.2", "name@1.2", "name" or ("name", "1.2")."""
    if isinstance(dep, (tuple, list)):
        if not dep:
            raise ToolError("dependency entry is empty")
        name = str(dep[0]).strip()
        ver = str(dep[1]).strip() if len(dep) > 1 else ""
    else:
        text = str(dep).strip()
        if not text:
            raise ToolError("dependency entry is empty")
        name, ver = text, ""
        for sep in ("==", "@"):
            if sep in text:
                head, _, tail = text.partition(sep)
                name, ver = head.strip(), tail.strip()
                break
    if not name:
        raise ToolError("dependency name is required")
    return name, ver


def cyclonedx(name, version, manifest, deps=(), now=None):
    """Render a CycloneDX 1.5 BOM dict for the package + optional deps.

    Deterministic: the serial number is UUIDv5(name@version#digest) and no
    timestamp is emitted unless a ``now`` callable is supplied.
    """
    name = str(name or "").strip()
    version = str(version or "").strip()
    if not name:
        raise ToolError("cyclonedx: component name is required")
    if not isinstance(manifest, (list, tuple)):
        raise ToolError("cyclonedx: manifest must be a list")
    digest = manifest_digest(list(manifest))
    seed = f"adsops:{name}@{version}:{digest}"
    serial = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
    metadata = {
        "component": {
            "type": "application",
            "name": name,
            "version": version,
            "hashes": [{"alg": "SHA-256", "content": digest}],
        },
        "tools": [{"name": "adsops.sbom"}],
    }
    if now is not None:
        stamp = now()
        metadata["timestamp"] = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
    seen = set()
    components = []
    for dep in deps:
        dname, dver = _parse_dep(dep)
        key = (dname.lower(), dver)
        if key in seen:  # dedupe so repeated pins cannot perturb output order
            continue
        seen.add(key)
        purl = (f"pkg:pypi/{dname.lower()}@{dver}" if dver
                else f"pkg:pypi/{dname.lower()}")
        components.append({"type": "library", "name": dname,
                           "version": dver, "purl": purl})
    components.sort(key=lambda c: (c["name"].lower(), c["version"]))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": serial,
        "version": 1,
        "metadata": metadata,
        "components": components,
        "properties": [{"name": "adsops:stdlib_only", "value": "true"}],
    }


def verify(root, manifest, read=None, walker=None):
    """Re-hash the tree and diff it against ``manifest``.

    Returns ``{"ok": bool, "changed": [...], "missing": [...], "extra": [...]}``.
    Files listed in the manifest but not returned by the current walk are
    reported as missing.
    """
    if not isinstance(manifest, (list, tuple)):
        raise ToolError("verify: manifest must be a list")
    read = read or _read_bytes
    old = {}
    for entry in manifest:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise ToolError("verify: every manifest entry needs a 'path'")
        old[str(entry["path"])] = entry
    current = {e["path"]: e for e in file_manifest(root, read=read, walker=walker)}
    changed = sorted(p for p in (set(old) & set(current))
                     if old[p].get("sha256") != current[p]["sha256"])
    missing = sorted(set(old) - set(current))
    extra = sorted(set(current) - set(old))
    return {"ok": not (changed or missing or extra),
            "changed": changed, "missing": missing, "extra": extra}


def selftest() -> int:
    """Exercise every public function with mocked I/O; returns check count."""
    import tempfile
    from datetime import datetime, timezone

    n = 0

    def ok(cond, msg):
        nonlocal n
        if not cond:
            raise AssertionError(msg)
        n += 1

    def write(root, rel, data):
        full = os.path.join(root, *rel.split("/"))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    with tempfile.TemporaryDirectory() as td:
        payload = b"print('hi')\n"
        write(td, "a.py", payload)
        write(td, "b.json", b'{"k":1}')
        write(td, "c.md", b"# t\n")
        write(td, "d.txt", b"ignore me")
        write(td, "sub/e.py", b"x = 1\n")
        write(td, "__pycache__/junk.py", b"compiled")
        write(td, "tests/fixtures/f.py", b"fixture")
        write(td, "sub/__pycache__/z.py", b"zz")

        man = file_manifest(td)
        paths = [e["path"] for e in man]
        ok(paths == ["a.py", "b.json", "c.md", "sub/e.py"], f"unexpected paths {paths}")
        ok(set(man[0]) == {"path", "sha256", "bytes"}, "entry keys")
        ok("d.txt" not in paths, ".txt should be filtered")
        ok("tests/fixtures/f.py" not in paths, "fixtures dir should be pruned")
        ok(not any("__pycache__" in p for p in paths), "pycache should be pruned")
        ok(man[0]["sha256"] == hashlib.sha256(payload).hexdigest(), "sha256 mismatch")
        ok(man[0]["bytes"] == len(payload), "byte count mismatch")

        only_txt = file_manifest(td, include_ext=(".txt",))
        ok([e["path"] for e in only_txt] == ["d.txt"], "include_ext override")
        no_sub = file_manifest(td, exclude_dirs=("sub", "__pycache__", "tests/fixtures"))
        ok([e["path"] for e in no_sub] == ["a.py", "b.json", "c.md"], "exclude_dirs override")

        digest = manifest_digest(man)
        ok(len(digest) == 64 and digest == manifest_digest(list(man)), "digest not stable")
        ok(manifest_digest([]) == hashlib.sha256(b"[]").hexdigest(), "empty digest")

        bom = cyclonedx("adsops", "0.1.0", man,
                        deps=("requests==2.31.0", ("idna", "3.6"), "requests==2.31.0"))
        ok(bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5", "bom header")
        ok(bom["serialNumber"].startswith("urn:uuid:"), "serial prefix")
        uuid.UUID(bom["serialNumber"][len("urn:uuid:"):])  # raises if malformed
        ok(True, "serial not a uuid")
        ok(bom["metadata"]["component"]["hashes"][0]["content"] == digest, "component hash")
        ok(bom["metadata"]["component"]["name"] == "adsops", "component name")
        ok("timestamp" not in bom["metadata"], "timestamp must be optional")
        ok(bom == cyclonedx("adsops", "0.1.0", man,
                            deps=("requests==2.31.0", ("idna", "3.6"))), "not deterministic")
        ok(len(bom["components"]) == 2, "deps not deduped")
        ok(bom["components"][0]["purl"] == "pkg:pypi/idna@3.6", "purl/sort order")
        ok(bom["properties"] == [{"name": "adsops:stdlib_only", "value": "true"}], "props")

        stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bom_ts = cyclonedx("adsops", "0.1.0", man, now=lambda: stamp)
        ok(bom_ts["metadata"]["timestamp"] == stamp.isoformat(), "injected timestamp")

        try:
            cyclonedx("", "1.0", man)
            ok(False, "empty name should raise")
        except ToolError:
            ok(True, "empty name accepted")

        res = verify(td, man)
        ok(res == {"ok": True, "changed": [], "missing": [], "extra": []}, f"clean verify {res}")

        write(td, "a.py", b"tampered\n")
        res = verify(td, man)
        ok(res["ok"] is False and res["changed"] == ["a.py"], f"changed {res}")
        ok(res["missing"] == [] and res["extra"] == [], "changed only")

        os.remove(os.path.join(td, "b.json"))
        write(td, "new.py", b"brand new\n")
        res = verify(td, man)
        ok(res["missing"] == ["b.json"], f"missing {res}")
        ok(res["extra"] == ["new.py"], f"extra {res}")
        ok("a.py" in res["changed"], "changed still reported")

        empty_dir = os.path.join(td, "empty")
        os.makedirs(empty_dir)
        ok(file_manifest(empty_dir) == [], "empty dir should yield []")

        # Fully mocked reader + walker: no real file contents touched.
        fake = file_manifest(td, read=lambda p: b"DATA",
                             walker=lambda r: [(td, [], ["m.py", "n.txt"])])
        ok(len(fake) == 1 and fake[0]["path"] == "m.py", f"mocked walk {fake}")
        ok(fake[0]["sha256"] == hashlib.sha256(b"DATA").hexdigest(), "mocked read hash")

        try:
            file_manifest(os.path.join(td, "does-not-exist"))
            ok(False, "missing root should raise")
        except ToolError:
            ok(True, "missing root accepted")

    return n


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)