"""Checked-in acquisition source follows its own pin, independent of SDK wheels."""

import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path.cwd()


class AcquisitionSourceTests(unittest.TestCase):
    def test_checked_in_source_matches_its_reviewed_pin(self):
        bundle = ROOT / "host/bootstrap/acquisition"
        updater = runpy.run_path(str(bundle / "acquisition_tool/source_update.py"))
        manifest = updater["verify"](bundle)
        pin = updater["read_pin"](ROOT / "requirements/provider-tools-source.json")
        self.assertEqual(manifest["release_version"], pin["version"])

    def test_import_archive_input_is_literal_and_private_to_the_import_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
            (root / "tools").mkdir()
            (root / "tools/docker_client.sh").write_text(
                "docker_client_prepare() { NMRNET_DOCKER_CONTEXT_INPUT=fixture; "
                "NMRNET_DOCKER_ENDPOINT_INPUT=unix:///fixture.sock; }\n"
            )
            output = root / "archive-observed"
            arguments = root / "arguments-observed"
            environment = os.environ.copy()
            for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKEFILES", ".EXTRA_PREREQS"):
                if name in environment:
                    del environment[name]
            environment.update(
                {
                    "RECORD_OUTPUT": str(output),
                    "RECORD_ARGUMENTS": str(arguments),
                    "BASH_FUNC_python3%%": '() { printf "%s" "$NMRNET_SOURCE_ARCHIVE_INPUT" > "$RECORD_OUTPUT"; '
                    'printf "%s\\0" "$@" > "$RECORD_ARGUMENTS"; }',
                    "INPUT_RELEASE_ARCHIVE": "ambient-archive",
                    "NMRNET_SOURCE_ARCHIVE_INPUT": "ambient-alias",
                    "NMRNET_SOURCE_ARCHIVE_VALUE": "ambient-value",
                }
            )
            target = "provider-tools/import-checked-in-source"
            missing = subprocess.run(
                ["make", "--no-print-directory", "-C", str(root), target],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(0, missing.returncode)
            self.assertIn("Set INPUT_RELEASE_ARCHIVE", missing.stderr)
            self.assertFalse(output.exists())
            alias_only = subprocess.run(
                ["make", "--no-print-directory", "-C", str(root), target, "NMRNET_SOURCE_ARCHIVE_INPUT=command-alias"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(0, alias_only.returncode)
            self.assertIn("Set INPUT_RELEASE_ARCHIVE", alias_only.stderr)
            self.assertFalse(output.exists())
            archive = "archive ' \" \\ `touch sentinel` $(shell touch sentinel)\n.tar"
            observer = 'observer:; @printf "observer=%s:%s:%s\\n" "$${NMRNET_SOURCE_ARCHIVE_INPUT+set}" "$${NMRNET_SOURCE_ARCHIVE_VALUE+set}" "$${INPUT_RELEASE_ARCHIVE+set}"'
            prerequisite = 'provider-tools/import-checked-in-source: archive-prerequisite\narchive-prerequisite:; @printf "prerequisite=%s:%s:%s\\n" "$${NMRNET_SOURCE_ARCHIVE_INPUT}" "$${NMRNET_SOURCE_ARCHIVE_VALUE+set}" "$${INPUT_RELEASE_ARCHIVE+set}"'
            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "-C",
                    str(root),
                    "--eval",
                    observer,
                    "--eval",
                    prerequisite,
                    target,
                    "observer",
                    "INPUT_RELEASE_ARCHIVE=" + archive,
                    "NMRNET_SOURCE_ARCHIVE_INPUT=command-alias",
                    "NMRNET_SOURCE_ARCHIVE_VALUE=command-value",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(archive, output.read_text())
            self.assertEqual(
                [
                    b"-I",
                    b"-S",
                    b"-B",
                    b"host/bootstrap/acquisition/acquisition_tool/source_update.py",
                    b"import",
                    b"--source-dir",
                    b"host/bootstrap/acquisition",
                    b"--pin-file",
                    b"requirements/provider-tools-source.json",
                    b"--archive",
                    archive.encode(),
                    b"",
                ],
                arguments.read_bytes().split(b"\0"),
            )
            self.assertIn("observer=::\n", result.stdout)
            self.assertIn("prerequisite=::\n", result.stdout)
            self.assertFalse((root / "sentinel").exists())
