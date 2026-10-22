"""Changed metadata, broadened grants and capture shortfalls publish no plan."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.integration.nmrnet.config_ports import ConfigFiles

from nmrnet_provider.config import ConfigurationRejected, read_startup_file
from nmrnet_provider.recovery_capture import capture_recovery, recovery_metadata


class RecoveryCaptureRefusalTests(unittest.TestCase):
    def configured_files(self, directory):
        source = Path(directory) / "source"
        source.mkdir(mode=0o700)
        files = ConfigFiles(source)
        files.raw = files.raw.replace(str(files.state).encode(), str(Path(directory) / "state").encode())
        files.write(files.raw)
        return files

    def test_changed_toml_is_refused_before_any_credential_read(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self.configured_files(directory)
            fields = recovery_metadata(files.path, mode="run", source_root=files.root, maximum_bytes=131072)
            files.write(files.raw + b"\n# changed after metadata\n")
            destination = Path(directory) / "frozen"
            with (
                patch("nmrnet_provider.recovery_capture.read_startup_file", wraps=read_startup_file) as reads,
                self.assertRaisesRegex(ConfigurationRejected, "changed"),
            ):
                capture_recovery(
                    files.path,
                    mode="run",
                    source_root=files.root,
                    destination=destination,
                    expected_config_sha256=fields[2],
                    maximum_bytes=131072,
                )
            self.assertEqual([Path(call.args[0]) for call in reads.call_args_list], [files.path])
            self.assertFalse(destination.exists())

    def test_run_metadata_refuses_an_api_file_outside_the_original_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self.configured_files(directory)
            files.write(files.raw.replace(str(files.credential).encode(), str(Path(directory) / "outside.private.json").encode()))
            with self.assertRaisesRegex(ConfigurationRejected, "inside"):
                recovery_metadata(files.path, mode="run", source_root=files.root, maximum_bytes=131072)

    def test_aggregate_budget_failure_keeps_private_partial_capture_without_a_ready_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self.configured_files(directory)
            maximum = len(files.raw)
            fields = recovery_metadata(files.path, mode="run", source_root=files.root, maximum_bytes=maximum)
            destination = Path(directory) / "frozen"
            with self.assertRaisesRegex(ConfigurationRejected, "aggregate"):
                capture_recovery(
                    files.path,
                    mode="run",
                    source_root=files.root,
                    destination=destination,
                    expected_config_sha256=fields[2],
                    maximum_bytes=maximum,
                )
            self.assertTrue((destination / "inputs/provider.toml").is_file())
            self.assertFalse((destination / "plan.json").exists())
            for path in destination.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
