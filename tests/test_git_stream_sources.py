"""Stream source entries must describe the committed captured blobs."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from host.git_stream_sources import source_allowlist


# silent-defaults: allow-contract -- Inventory entries model nonexecutable files unless the scenario selects an executable file.
def record(name, content, executable=False):
    digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content, usedforsecurity=False).hexdigest()
    # silent-defaults: allow-contract -- Git records executable files as 100755 and ordinary files as 100644.
    mode = "100755" if executable else "100644"
    return f"{mode} blob {digest}\t{name}\0".encode()


# silent-defaults: allow-contract -- Parent inventory uses the context root; submodule and invalid-prefix scenarios supply their own prefix.
def admitted(path, prefix="."):
    return path, prefix, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class GitStreamSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = self.root / "context"
        self.context.mkdir()
        self.inventory = self.root / "inventory"

    def test_combines_parent_and_submodule_sources(self):
        (self.context / "a.py").write_bytes(b"parent")
        upstream = self.context / "NMRNetplusplus"
        upstream.mkdir()
        (upstream / "run.py").write_bytes(b"child")
        (upstream / "run.py").chmod(0o755)
        self.inventory.write_bytes(record("a.py", b"parent"))
        second = self.root / "upstream"
        second.write_bytes(record("run.py", b"child", executable=True))
        result = source_allowlist(self.context, [admitted(self.inventory), admitted(second, "NMRNetplusplus")])
        self.assertEqual([entry["destination"] for entry in result], ["NMRNetplusplus/run.py", "a.py"])
        self.assertEqual(result[0]["path"], str(upstream / "run.py"))
        self.assertTrue(result[0]["executable"])
        self.assertEqual(result[1]["sha256"], hashlib.sha256(b"parent").hexdigest())

    def test_changed_bytes_and_executable_mode_are_rejected(self):
        path = self.context / "input"
        path.write_bytes(b"changed")
        self.inventory.write_bytes(record("input", b"committed"))
        with self.assertRaisesRegex(ValueError, "committed blob"):
            source_allowlist(self.context, [admitted(self.inventory)])
        path.write_bytes(b"committed")
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "type or mode"):
            source_allowlist(self.context, [admitted(self.inventory)])

    def test_rejects_malformed_inventory_and_duplicate_destinations(self):
        (self.context / "input").write_bytes(b"x")
        self.inventory.write_bytes(record("input", b"x")[:-1])
        with self.assertRaisesRegex(ValueError, "NUL-delimited"):
            source_allowlist(self.context, [admitted(self.inventory)])
        self.inventory.write_bytes(record("input", b"x"))
        with self.assertRaisesRegex(ValueError, "duplicated"):
            source_allowlist(self.context, [admitted(self.inventory), admitted(self.inventory)])
        self.inventory.write_bytes(record(".", b"x"))
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            source_allowlist(self.context, [admitted(self.inventory)])
        self.inventory.write_bytes(record("input", b"x"))
        with self.assertRaisesRegex(ValueError, "prefix"):
            source_allowlist(self.context, [admitted(self.inventory, "a//b")])

    def test_rejects_inventory_change_after_image_identity_is_captured(self):
        (self.context / "input").write_bytes(b"x")
        self.inventory.write_bytes(record("input", b"x"))
        original = admitted(self.inventory)
        self.inventory.write_bytes(record("input", b"y"))
        with self.assertRaisesRegex(ValueError, "captured image identity"):
            source_allowlist(self.context, [original])

    def test_rejects_symlinked_ancestor(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "input").write_bytes(b"x")
        (self.context / "linked").symlink_to(outside, target_is_directory=True)
        self.inventory.write_bytes(record("linked/input", b"x"))
        with self.assertRaises(OSError):
            source_allowlist(self.context, [admitted(self.inventory)])

    def test_rejects_symlinked_file(self):
        outside = self.root / "outside"
        outside.write_bytes(b"x")
        (self.context / "input").symlink_to(outside)
        self.inventory.write_bytes(record("input", b"x"))
        with self.assertRaises(OSError):
            source_allowlist(self.context, [admitted(self.inventory)])

    def test_rejects_symlinked_context_ancestor(self):
        (self.context / "input").write_bytes(b"x")
        self.inventory.write_bytes(record("input", b"x"))
        link = self.root / "linked-parent"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            source_allowlist(link / "context", [admitted(self.inventory)])


if __name__ == "__main__":
    unittest.main()
