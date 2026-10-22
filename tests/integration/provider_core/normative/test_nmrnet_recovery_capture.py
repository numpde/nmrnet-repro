"""Recovery captures only its admitted original TOML and explicit API files."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.config_ports import ConfigFiles

from nmrnet_provider.recovery_capture import capture_recovery, recovery_metadata
from nmrnet_provider.recovery_capture import main as recovery_capture_main


class RecoveryCaptureTests(unittest.IsolatedAsyncioTestCase):
    def test_metadata_and_capture_publish_exact_handoffs_without_stream_payloads_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self.configured_files(directory)
            expected = recovery_metadata(files.path, mode="inspect", source_root=files.root, maximum_bytes=65536)
            for operation in ("metadata", "capture"):
                handoff = Path(directory) / operation
                arguments = [
                    operation,
                    "--mode",
                    "inspect",
                    "--config",
                    str(files.path),
                    "--source-root",
                    str(files.root),
                    "--maximum-bytes",
                    "65536",
                    "--handoff",
                    str(handoff),
                ]
                if operation == "capture":
                    arguments += ["--destination", str(Path(directory) / "snapshot"), "--expected-config-sha256", expected[2]]
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(recovery_capture_main(arguments), 0)
                self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
                self.assertEqual(handoff.read_bytes(), b"".join(value.encode() + b"\0" for value in expected))
                self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
                original = handoff.read_bytes()
                with redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(recovery_capture_main(arguments), 2)
                self.assertEqual(handoff.read_bytes(), original)
                self.assertEqual(output.getvalue(), "")

    def configured_files(self, directory):
        source = Path(directory) / "source"
        source.mkdir(mode=0o700)
        files = ConfigFiles(source)
        files.raw = files.raw.replace(str(files.state).encode(), str(Path(directory) / "state").encode())
        files.write(files.raw)
        return files

    def test_inspection_captures_toml_without_reading_any_credential_or_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self.configured_files(directory)
            files.key.unlink()
            files.credential.unlink()
            destination = Path(directory) / "frozen"
            fields = recovery_metadata(files.path, mode="inspect", source_root=files.root, maximum_bytes=65536)
            self.assertEqual(
                fields,
                (
                    str(files.root),
                    "provider.toml",
                    hashlib.sha256(files.raw).hexdigest(),
                    str(Path(directory) / "state"),
                    "",
                    "",
                    "",
                ),
            )
            captured = capture_recovery(
                files.path,
                mode="inspect",
                source_root=files.root,
                destination=destination,
                expected_config_sha256=fields[2],
                maximum_bytes=65536,
            )
            self.assertEqual(captured, fields)
            self.assertEqual((destination / "inputs/provider.toml").read_bytes(), files.raw)
            self.assertEqual(
                [path.relative_to(destination / "inputs") for path in (destination / "inputs").rglob("*") if path.is_file()],
                [Path("provider.toml")],
            )
            self.assertFalse((Path(directory) / "state").exists())
            for path in destination.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_run_preserves_original_bytes_and_grants_only_the_api_signing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self.configured_files(directory)
            files.key.unlink()
            original = files.credential.read_bytes()
            fields = recovery_metadata(files.path, mode="run", source_root=files.root, maximum_bytes=131072)
            self.assertEqual(fields[4:], ("api.example:443", str(files.credential), ""))
            destination = Path(directory) / "frozen"
            self.assertEqual(
                capture_recovery(
                    files.path,
                    mode="run",
                    source_root=files.root,
                    destination=destination,
                    expected_config_sha256=fields[2],
                    maximum_bytes=131072,
                ),
                fields,
            )
            self.assertEqual((destination / "inputs/provider.toml").read_bytes(), files.raw)
            self.assertEqual((destination / "inputs/signing.private.json").read_bytes(), original)
            self.assertFalse((destination / "inputs/interpreter.key").exists())
            self.assertFalse((Path(directory) / "state").exists())

    async def test_run_captures_only_the_explicit_api_ca_bytes(self):
        async with TlsPeer("success") as peer:
            with tempfile.TemporaryDirectory() as directory:
                files = self.configured_files(directory)
                files.key.unlink()
                ca = files.root / "api-ca.pem"
                original = (Path(peer.scratch.name) / "cert.pem").read_bytes()
                ca.write_bytes(original)
                files.raw = files.raw.replace(b"[api]\n", b"[api]\nca_file = " + json.dumps(str(ca)).encode() + b"\n")
                files.write(files.raw)
                fields = recovery_metadata(files.path, mode="run", source_root=files.root, maximum_bytes=131072)
                self.assertEqual(fields[4:], ("api.example:443", str(files.credential), str(ca)))
                destination = Path(directory) / "frozen"
                self.assertEqual(
                    capture_recovery(
                        files.path,
                        mode="run",
                        source_root=files.root,
                        destination=destination,
                        expected_config_sha256=fields[2],
                        maximum_bytes=131072,
                    ),
                    fields,
                )
                self.assertEqual((destination / "inputs/api-ca.pem").read_bytes(), original)
                self.assertEqual((destination / "inputs/provider.toml").read_bytes(), files.raw)
                self.assertEqual(peer.requests, [])
