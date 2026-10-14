"""Check the supported launcher's actual argv without engine access."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    def command(self, lane, archive=None):
        environment = dict(os.environ)
        environment.pop("ARCHIVE", None)
        environment.pop("NMRNET_ARCHIVE_INPUT", None)
        environment["NMRNET_WLAN_INTERFACE_INPUT"] = "lo"
        if archive is not None:
            environment["NMRNET_ARCHIVE_INPUT"] = archive
        return subprocess.run(["bash", "tools/container.sh", "--print", lane],
                              env=environment, text=True, capture_output=True)

    def test_test_lane_is_offline_and_reads_only_tools_and_tests(self):
        result = self.command("test")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = shlex.split(result.stdout)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("--read-only", arguments)
        self.assertIn("no-new-privileges:true", arguments)
        self.assertIn("NMRNET_IN_CONTAINER=1", arguments)
        mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertNotIn("/var/run/docker.sock", " ".join(mounts))

    def test_download_requires_explicit_archive_and_only_writes_downloads(self):
        self.assertNotEqual(self.command("download").returncode, 0)
        self.assertNotEqual(self.command("download", "../weight.zip").returncode, 0)
        result = self.command("download", "weight.zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = shlex.split(result.stdout.splitlines()[-2])
        self.assertEqual(arguments[arguments.index("--network") + 1], "host")
        mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
        writable = [mount for mount in mounts if not mount.endswith(",readonly")]
        self.assertEqual(writable, [f"type=bind,src={Path.cwd()}/.work/downloads,dst=/downloads"])

    def test_ambient_archive_does_not_select_a_download(self):
        environment = {**os.environ, "ARCHIVE": "weight.zip", "NMRNET_WLAN_INTERFACE_INPUT": "lo"}
        environment.pop("NMRNET_ARCHIVE_INPUT", None)
        result = subprocess.run(["bash", "tools/container.sh", "--print", "download"],
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("make command line", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_check_lane_reads_downloads_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            (checkout / "tools").mkdir()
            (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
            (checkout / "tools/docker.sh").write_bytes(Path("tools/docker.sh").read_bytes())
            (checkout / ".work/downloads").mkdir(parents=True)
            result = subprocess.run(["bash", str(checkout / "tools/container.sh"), "--print", "check"],
                                    env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip"}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertEqual(arguments[arguments.index("--network") + 1], "none")
            mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
            self.assertEqual(len(mounts), 4)
            self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
            self.assertIn("NMRNET_IN_CONTAINER=1", arguments)
            self.assertEqual(arguments[arguments.index("-m") + 1], "tools.artifacts")
            self.assertIn(f"type=bind,src={checkout}/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly", mounts)
            self.assertIn(f"type=bind,src={checkout}/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly", mounts)

    def test_symlink_staging_is_rejected_before_engine_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            (checkout / "tools").mkdir()
            (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
            (checkout / "tools/docker.sh").write_bytes(Path("tools/docker.sh").read_bytes())
            (checkout / ".work").symlink_to(checkout / "elsewhere", target_is_directory=True)
            result = subprocess.run(["bash", str(checkout / "tools/container.sh"), "--print", "download"],
                                    env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip", "NMRNET_WLAN_INTERFACE_INPUT": "lo"}, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("without a symlink", result.stderr)

    def test_extract_reads_selected_inputs_offline_and_only_writes_prepared(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            (checkout / "tools").mkdir()
            (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
            (checkout / ".work/downloads").mkdir(parents=True)
            (checkout / "artifacts").mkdir()
            (checkout / "artifacts/weight.selection.json").write_text("{}")
            result = subprocess.run(["bash", str(checkout / "tools/container.sh"), "--print", "extract"],
                                    env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip"}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertEqual(arguments[arguments.index("--network") + 1], "none")
            mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
            self.assertEqual(len(mounts), 6)
            writable = [mount for mount in mounts if not mount.endswith(",readonly")]
            self.assertEqual(writable, [f"type=bind,src={checkout}/.work/prepared,dst=/prepared"])
            self.assertIn(f"type=bind,src={checkout}/.work/downloads,dst=/downloads,readonly", mounts)
            self.assertIn(f"type=bind,src={checkout}/artifacts/weight.selection.json,dst=/inputs/selection.json,readonly", mounts)
            self.assertEqual(arguments[arguments.index("--output") + 1], "/prepared/weight")
            (checkout / ".work/prepared").symlink_to(checkout / "elsewhere", target_is_directory=True)
            denied = subprocess.run(["bash", str(checkout / "tools/container.sh"), "--print", "extract"],
                                    env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip"}, text=True, capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("without a symlink", denied.stderr)
