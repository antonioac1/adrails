import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from adsops import sbom


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def flat_walker(root, filenames):
    return [(root, [], list(filenames))]


class TestFileManifest(unittest.TestCase):
    def test_includes_only_allowed_extensions_and_sorts(self):
        with tempfile.TemporaryDirectory() as root:
            files = ["b.py", "a.json", "c.md", "d.toml", "e.txt", "noext"]
            walker = lambda p: flat_walker(root, files)
            manifest = sbom.file_manifest(root, read=lambda p: b"x", walker=walker)

            self.assertEqual(
                [e["path"] for e in manifest],
                ["a.json", "b.py", "c.md", "d.toml"],
            )
            for entry in manifest:
                self.assertEqual(set(entry), {"path", "sha256", "bytes"})
                self.assertEqual(entry["sha256"], sha256_hex(b"x"))
                self.assertEqual(entry["bytes"], 1)

    def test_custom_include_ext(self):
        with tempfile.TemporaryDirectory() as root:
            walker = lambda p: flat_walker(root, ["a.py", "b.json"])
            manifest = sbom.file_manifest(
                root,
                include_ext=(".py",),
                read=lambda p: b"",
                walker=walker,
            )
            self.assertEqual([e["path"] for e in manifest], ["a.py"])

    def test_empty_walker_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = sbom.file_manifest(
                root,
                read=lambda p: b"",
                walker=lambda p: [],
            )
            self.assertEqual(manifest, [])

    def test_read_and_walker_are_injected(self):
        with tempfile.TemporaryDirectory() as root:
            walker_calls = []
            read_calls = []

            def walker(path):
                walker_calls.append(path)
                return flat_walker(root, ["a.py"])

            def read(path):
                read_calls.append(path)
                return b"hello"

            manifest = sbom.file_manifest(root, read=read, walker=walker)

            self.assertEqual(walker_calls, [root])
            self.assertEqual(len(read_calls), 1)
            self.assertEqual(os.path.basename(read_calls[0]), "a.py")
            self.assertEqual(manifest[0]["bytes"], 5)

    def test_paths_are_relative_posix_strings(self):
        with tempfile.TemporaryDirectory() as root:
            dirpath = os.path.join(root, "pkg", "sub")
            walker = lambda p: [(dirpath, [], ["m.py"])]
            manifest = sbom.file_manifest(root, read=lambda p: b"", walker=walker)
            self.assertEqual(manifest[0]["path"], "pkg/sub/m.py")

    def test_sha256_and_byte_count(self):
        with tempfile.TemporaryDirectory() as root:
            content = b"abc"
            walker = lambda p: flat_walker(root, ["a.py"])
            manifest = sbom.file_manifest(root, read=lambda p: content, walker=walker)

            self.assertEqual(manifest[0]["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(manifest[0]["bytes"], len(content))

    def test_default_exclude_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            rootp = Path(root)
            for rel in (
                "pkg/a.py",
                "__pycache__/b.py",
                ".git/c.py",
                "tests/fixtures/d.py",
                "keep/e.py",
            ):
                p = rootp / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"")

            manifest = sbom.file_manifest(root, read=lambda p: b"")
            self.assertEqual(
                [e["path"] for e in manifest],
                ["keep/e.py", "pkg/a.py"],
            )

    def test_custom_exclude_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            rootp = Path(root)
            for rel in ("skip/a.py", "keep/b.py"):
                p = rootp / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"")

            manifest = sbom.file_manifest(
                root,
                exclude_dirs=("skip",),
                read=lambda p: b"",
            )
            self.assertEqual([e["path"] for e in manifest], ["keep/b.py"])


class TestManifestDigest(unittest.TestCase):
    def test_canonical_json_sha256(self):
        manifest = [
            {"path": "b.py", "sha256": "2", "bytes": 2},
            {"path": "a.py", "sha256": "1", "bytes": 1},
        ]
        expected = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(sbom.manifest_digest(manifest), expected)
        self.assertEqual(len(expected), 64)

    def test_dict_key_order_does_not_matter(self):
        m1 = [{"path": "a.py", "sha256": "x", "bytes": 1}]
        m2 = [{"bytes": 1, "sha256": "x", "path": "a.py"}]
        self.assertEqual(sbom.manifest_digest(m1), sbom.manifest_digest(m2))

    def test_empty_manifest(self):
        expected = hashlib.sha256(b"[]").hexdigest()
        self.assertEqual(sbom.manifest_digest([]), expected)

    def test_different_manifests_differ(self):
        self.assertNotEqual(
            sbom.manifest_digest([]),
            sbom.manifest_digest([{"path": "a.py", "sha256": "x", "bytes": 1}]),
        )


class TestCycloneDX(unittest.TestCase):
    def setUp(self):
        self.manifest = [{"path": "a.py", "sha256": "abc", "bytes": 3}]
        self.digest = sbom.manifest_digest(self.manifest)

    def test_basic_structure(self):
        bom = sbom.cyclonedx("adsops", "1.2.3", self.manifest)

        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["specVersion"], "1.5")
        self.assertEqual(bom["version"], 1)

        serial = bom["serialNumber"]
        self.assertTrue(serial.startswith("urn:uuid:"))
        uuid.UUID(serial[len("urn:uuid:"):])

        meta = bom["metadata"]
        comp = meta["component"]
        self.assertEqual(comp["type"], "application")
        self.assertEqual(comp["name"], "adsops")
        self.assertEqual(comp["version"], "1.2.3")
        self.assertEqual(
            comp["hashes"],
            [{"alg": "SHA-256", "content": self.digest}],
        )
        self.assertEqual(meta["tools"], [{"name": "adsops.sbom"}])
        self.assertEqual(bom["components"], [])
        self.assertEqual(
            bom["properties"],
            [{"name": "adsops:stdlib_only", "value": "true"}],
        )

    def test_no_timestamp_without_now(self):
        bom = sbom.cyclonedx("adsops", "1.2.3", self.manifest)
        self.assertNotIn("timestamp", bom)
        self.assertNotIn("timestamp", bom["metadata"])

    def test_deterministic(self):
        a = sbom.cyclonedx("adsops", "1.2.3", self.manifest)
        b = sbom.cyclonedx("adsops", "1.2.3", self.manifest)
        self.assertEqual(a, b)
        self.assertEqual(a["serialNumber"], b["serialNumber"])

    def test_serial_number_depends_on_inputs(self):
        base = sbom.cyclonedx("adsops", "1.2.3", self.manifest)["serialNumber"]
        other_name = sbom.cyclonedx("other", "1.2.3", self.manifest)["serialNumber"]
        other_version = sbom.cyclonedx("adsops", "9.9.9", self.manifest)["serialNumber"]
        other_manifest = sbom.cyclonedx(
            "adsops",
            "1.2.3",
            [{"path": "b.py", "sha256": "def", "bytes": 3}],
        )["serialNumber"]

        self.assertNotEqual(base, other_name)
        self.assertNotEqual(base, other_version)
        self.assertNotEqual(base, other_manifest)


class TestVerify(unittest.TestCase):
    def test_ok(self):
        with tempfile.TemporaryDirectory() as root:
            content = b"same"
            walker = lambda p: flat_walker(root, ["a.py"])
            manifest = [{
                "path": "a.py",
                "sha256": sha256_hex(content),
                "bytes": len(content),
            }]

            result = sbom.verify(
                root,
                manifest,
                read=lambda p: content,
                walker=walker,
            )
            self.assertEqual(
                result,
                {"ok": True, "changed": [], "missing": [], "extra": []},
            )

    def test_changed(self):
        with tempfile.TemporaryDirectory() as root:
            walker = lambda p: flat_walker(root, ["a.py"])
            manifest = [{
                "path": "a.py",
                "sha256": sha256_hex(b"old"),
                "bytes": 3,
            }]

            result = sbom.verify(
                root,
                manifest,
                read=lambda p: b"new",
                walker=walker,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["changed"], ["a.py"])
            self.assertEqual(result["missing"], [])
            self.assertEqual(result["extra"], [])

    def test_missing(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = [{
                "path": "a.py",
                "sha256": sha256_hex(b"x"),
                "bytes": 1,
            }]

            result = sbom.verify(
                root,
                manifest,
                read=lambda p: b"x",
                walker=lambda p: [],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["missing"], ["a.py"])
            self.assertEqual(result["changed"], [])
            self.assertEqual(result["extra"], [])

    def test_extra(self):
        with tempfile.TemporaryDirectory() as root:
            walker = lambda p: flat_walker(root, ["a.py"])
            result = sbom.verify(
                root,
                [],
                read=lambda p: b"x",
                walker=walker,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["extra"], ["a.py"])
            self.assertEqual(result["changed"], [])
            self.assertEqual(result["missing"], [])

    def test_combined_changed_missing_extra(self):
        with tempfile.TemporaryDirectory() as root:
            walker = lambda p: flat_walker(root, ["a.py", "b.py"])

            def read(path):
                base = os.path.basename(path)
                if base == "a.py":
                    return b"new"
                if base == "b.py":
                    return b"extra"
                raise KeyError(path)

            manifest = [
                {"path": "a.py", "sha256": sha256_hex(b"old"), "bytes": 3},
                {"path": "c.py", "sha256": sha256_hex(b"c"), "bytes": 1},
            ]

            result = sbom.verify(root, manifest, read=read, walker=walker)
            self.assertFalse(result["ok"])
            self.assertCountEqual(result["changed"], ["a.py"])
            self.assertCountEqual(result["missing"], ["c.py"])
            self.assertCountEqual(result["extra"], ["b.py"])

    def test_walker_and_read_injected(self):
        with tempfile.TemporaryDirectory() as root:
            content = b"ok"
            calls = {"walk": [], "read": []}

            def walker(path):
                calls["walk"].append(path)
                return flat_walker(root, ["a.py"])

            def read(path):
                calls["read"].append(path)
                return content

            manifest = [{
                "path": "a.py",
                "sha256": sha256_hex(content),
                "bytes": len(content),
            }]

            result = sbom.verify(root, manifest, read=read, walker=walker)

            self.assertTrue(result["ok"])
            self.assertEqual(calls["walk"], [root])
            self.assertEqual(len(calls["read"]), 1)
            self.assertEqual(os.path.basename(calls["read"][0]), "a.py")


if __name__ == '__main__':
    unittest.main()
