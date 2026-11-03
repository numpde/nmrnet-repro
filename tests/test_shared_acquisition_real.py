"""Exercise the released publisher on cached target-compatible inputs offline."""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import wheelhouse, wheelhouse_acquire


class SharedAcquisitionIntegrationTests(unittest.TestCase):
    def test_released_streamer_emits_captured_source_and_selected_cached_wheel(self):
        pool = Path("/wheelhouse")
        pin = next(line for line in Path("/input/wheelhouse-tool-assets.in").read_text().splitlines()
                   if line.startswith("https://"))
        url, tool_hash = pin.rsplit("#sha256=", 1)
        tool = pool / url.rsplit("/", 1)[-1]
        self.assertEqual(tool_hash, hashlib.sha256(tool.read_bytes()).hexdigest())
        wheel_pin = next(line for line in Path("/input/provider-core-assets.in").read_text().splitlines()
                         if line.partition("#")[0].endswith(".whl"))
        wheel_url, wheel_hash = wheel_pin.rsplit("#sha256=", 1)
        wheel = Path(urllib.parse.urlsplit(wheel_url).path).name
        self.assertLessEqual((pool / wheel).stat().st_size, 1024 * 1024)
        source = Path("/workspace/tools/git_stream_sources.py")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="nmrnet-stream-proof-") as temporary:
            root = Path(temporary)
            allowlist = root / "sources.json"
            manifest = root / "manifest.json"
            report = root / "report.json"
            report_output = io.StringIO()
            with wheelhouse_acquire.pool_lock(pool, exclusive=False) as descriptor:
                wheelhouse.select_wheels(Path("/input/provider-core-native-install.lock"), pool,
                                         report_output=report_output, lock_fd=descriptor)
            report.write_text(report_output.getvalue())
            allowlist.write_text(json.dumps([{"path": str(source), "destination": "tools/git_stream_sources.py",
                                              "sha256": source_hash, "executable": bool(source.stat().st_mode & 0o111)}]))
            projected = subprocess.run([sys.executable, "-I", "-S", str(tool), "report", "--report", str(report),
                                        "--pool", str(pool), "--directory", "wheelhouse/runtime"],
                                       check=True, capture_output=True, text=True, timeout=30)
            manifest.write_text(projected.stdout)
            self.assertEqual([{"directory": "wheelhouse/runtime", "filename": wheel, "sha256": wheel_hash}],
                             json.loads(projected.stdout))
            identity = subprocess.run([sys.executable, "-I", "-S", str(tool), "identity", "--manifest", str(manifest)],
                                      check=True, capture_output=True, text=True, timeout=30).stdout.strip()
            self.assertRegex(identity, r"^[0-9a-f]{64}$")
            process = subprocess.run([sys.executable, "-I", "-S", str(tool), "stream", "--pool", str(pool),
                                      "--source-allowlist", str(allowlist), "--manifest", str(manifest)],
                                     capture_output=True, timeout=30, check=True)
            with tarfile.open(fileobj=io.BytesIO(process.stdout), mode="r:") as archive:
                observed = {entry.name: hashlib.sha256(archive.extractfile(entry).read()).hexdigest()
                            for entry in archive if entry.isfile()}
        self.assertEqual({"tools/git_stream_sources.py": source_hash, "wheelhouse/runtime/" + wheel: wheel_hash}, observed)

    def test_source_bound_projection_and_cached_publisher(self):
        pool = Path("/wheelhouse")
        real_select = wheelhouse_acquire.select_wheels
        calls = 0

        def select(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise subprocess.CalledProcessError(1, "simulate incomplete selection")
            return real_select(*args, **kwargs)

        with patch.object(wheelhouse_acquire, "select_wheels", side_effect=select):
            wheelhouse_acquire.acquire(
                [Path("/input/provider-core-native-install.lock")], pool,
                Path("/input/provider-core-assets.in"), Path("/input/wheelhouse-acquirer-assets.in"),
            )
        self.assertEqual(calls, 3)


if __name__ == "__main__":
    unittest.main()
