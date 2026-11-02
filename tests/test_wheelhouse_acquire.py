"""Exercise the shared publisher handoff without downloading artifacts."""

import fcntl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import wheelhouse_acquire


class SharedAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.lock = self.root / "inference.lock"
        self.lock.write_text("torch==2.13.0+cpu \\\n    --hash=sha256:" + "a" * 64 + "\n")
        self.assets = self.root / "assets.in"
        self.assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/provider_core-1.0-py3-none-any.whl#sha256=" + "b" * 64 + "\n")
        self.tool_assets = self.root / "tool.in"
        self.tool_assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/wheelhouse-tool-1.0.0.pyz#sha256=" + "c" * 64 + "\n")
        self.declaration = self.root / "inference.in"
        self.url = "https://download.pytorch.org/whl/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl#sha256=" + "a" * 64
        self.declaration.write_text("torch @ " + self.url + "\n")

    def test_warm_pool_needs_no_tool_or_metadata_request(self):
        with (patch.object(wheelhouse_acquire, "prepare_core_assets") as assets,
              patch.object(wheelhouse_acquire, "select_wheels", return_value={}),
              patch.object(wheelhouse_acquire.subprocess, "run") as run):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       declarations=[self.declaration])
        assets.assert_not_called()
        run.assert_not_called()

    def test_warm_pool_still_ensures_the_stream_tool_needed_for_builds(self):
        captured = []

        def prepare(path, pool, *, download):
            captured.append(path.read_text())
            self.assertEqual(pool, self.pool)
            self.assertTrue(download)

        with (patch.object(wheelhouse_acquire, "prepare_core_assets", side_effect=prepare),
              patch.object(wheelhouse_acquire, "select_wheels", return_value={}),
              patch.object(wheelhouse_acquire.subprocess, "run") as run):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       stream_assets=self.tool_assets)
        self.assertEqual(captured, [self.tool_assets.read_text()])
        run.assert_not_called()

    def test_missing_wheel_uses_issuer_source_and_shared_publisher(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "locate" in command:
                kwargs["stdout"].write(b"projected lock")
            else:
                # The shared publisher must be able to take its own exclusive lease.
                with wheelhouse_acquire.pool_lock(self.pool, exclusive=True) as descriptor:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return subprocess.CompletedProcess(command, 0)

        with (patch.object(wheelhouse_acquire, "prepare_core_assets"),
              patch.object(wheelhouse_acquire, "select_wheels", side_effect=[subprocess.CalledProcessError(1, "pip"), {}]),
              patch.object(wheelhouse_acquire.subprocess, "run", side_effect=run)):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       declarations=[self.declaration])
        self.assertEqual(len(calls), 2)
        self.assertIn("locate", calls[0])
        self.assertEqual(calls[0][calls[0].index("--pool") + 1], str(self.pool))
        self.assertIn(self.url, calls[0])
        self.assertIn("acquire", calls[1])
        self.assertIn("--source-lock", calls[1])
        self.assertEqual(calls[1][-2:], ["--pool", str(self.pool)])

    def test_invalid_direct_source_does_not_fall_back_to_index(self):
        self.declaration.write_text("torch @ https://example.invalid/torch.whl\n")
        with self.assertRaisesRegex(ValueError, "direct sources require"):
            wheelhouse_acquire.direct_wheels([self.declaration], {"torch"})
        self.declaration.write_text("torch @ https://example.invalid/torch.whl#sha256=" + "a" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "invalid wheel filename"):
            wheelhouse_acquire.direct_wheels([self.declaration], {"torch"})

    def test_support_selects_only_the_pinned_source_archive(self):
        source_url = "https://github.com/numpde/nmr-provider-tools/releases/download/v1/provider_core-1.0.tar.gz#sha256=" + "d" * 64
        self.assets.write_text(self.assets.read_text() + source_url + "\n")
        captured = []

        def prepare(path, pool, *, download):
            captured.append(path.read_text())
            self.assertEqual(pool, self.pool)
            self.assertTrue(download)

        with patch.object(wheelhouse_acquire, "prepare_core_assets", side_effect=prepare):
            wheelhouse_acquire.test_support(self.assets, self.pool, download=True)
        self.assertEqual(captured, [source_url + "\n"])


if __name__ == "__main__":
    unittest.main()
