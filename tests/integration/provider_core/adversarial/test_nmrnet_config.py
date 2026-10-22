"""Startup refusals happen before network, native allocation or journal creation."""

import errno
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tests.integration.nmrnet.config_ports import ConfigFiles, native_capture_files, startup_capture_files

from nmrnet_provider.config import ConfigurationRejected, load_provider_config, read_startup_file
from nmrnet_provider.launch_capture import capture_execution
from nmrnet_provider.startup_capture import capture_startup, main


class ProviderConfigurationBoundaryTests(unittest.TestCase):
    def test_execution_capture_refuses_changed_missing_linked_or_mismatched_inputs_without_publication(self):
        for fault in ("changed", "missing", "linked", "revision", "inventory", "identity"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                native = native_capture_files(directory)
                destination = Path(directory) / "declaration"
                destination.mkdir(mode=0o700)
                identities = {
                    "image_id": "sha256:" + "b" * 64,
                    "geometry_image_id": "sha256:" + "c" * 64,
                    "controller_image_id": "sha256:" + "d" * 64,
                    "repository_revision": "a" * 40,
                    "upstream_revision": "e" * 40,
                }
                source = native / "sources" / "tools/worker.py"
                if fault == "changed":
                    source.write_bytes(b"changed\n")
                elif fault == "missing":
                    source.unlink()
                elif fault == "linked":
                    source.unlink()
                    source.symlink_to("reader.py")
                elif fault == "revision":
                    identities["repository_revision"] = "f" * 40
                elif fault == "inventory":
                    (native / "inputs.paths").write_text("tools/reader.py\n")
                else:
                    identities["image_id"] = "mutable:latest"
                with self.assertRaises((ConfigurationRejected, OSError)):
                    capture_execution(native, destination, **identities)
                self.assertFalse((destination / "execution.json").exists())

    def test_capture_cli_reports_symlink_loops_without_printing_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            files, snapshot = startup_capture_files(directory)
            loop = files.root / "PRIVATE_LOOP_PATH"
            loop.symlink_to(loop.name)
            files.write(files.raw.replace(str(files.key).encode(), str(loop).encode()))
            received = io.StringIO()
            with redirect_stderr(received), self.assertRaises(SystemExit) as stopped:
                main(
                    [
                        "--config",
                        str(files.path),
                        "--source-root",
                        str(files.root),
                        "--destination",
                        str(snapshot),
                        "--maximum-bytes",
                        "65536",
                    ]
                )
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("symlink loop", received.getvalue())
            self.assertNotIn("PRIVATE_LOOP_PATH", received.getvalue())
            self.assertIsInstance(json.loads(received.getvalue()), dict)
            self.assertFalse((snapshot / "plan.json").exists())

    def test_startup_capture_refuses_paths_outside_the_grant_including_parent_and_intermediate_link_escapes(self):
        for selection in ("outside", "parent", "intermediate-link"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                root = Path(directory)
                outside = root / "outside.key"
                outside.write_bytes(b"PRIVATE_OUTSIDE_KEY")
                outside.chmod(0o600)
                if selection == "outside":
                    selected = outside
                elif selection == "parent":
                    selected = files.root / ".." / "outside.key"
                else:
                    (files.root / "link").symlink_to(root, target_is_directory=True)
                    selected = files.root / "link" / "outside.key"
                files.write(files.raw.replace(str(files.key).encode(), str(selected).encode()))
                with self.assertRaises(ConfigurationRejected):
                    capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                self.assertFalse((snapshot / "plan.json").exists())
                self.assertFalse((root / "state").exists())

    def test_repeated_inputs_repeat_owner_admission_and_refuse_changed_bytes(self):
        for change in ("permissions", "bytes"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                endpoint = files.raw.split(b"[[interpreter.endpoints]]", 1)[1].split(b"[uploads]", 1)[0]
                second = b"[[interpreter.endpoints]]" + endpoint.replace(
                    b'configuration_id = "primary"', b'configuration_id = "second"'
                )
                files.write(files.raw.replace(b"[uploads]", second + b"[uploads]"))
                secret_calls = []

                def change_after_read(path, *, key=files.key, calls=secret_calls, change=change, **settings):
                    if Path(path) == key:
                        calls.append(settings["secret"])
                    raw = read_startup_file(path, **settings)
                    if Path(path) == key and len(calls) == 1:
                        if change == "permissions":
                            key.chmod(0o644)
                        else:
                            key.write_bytes(b"CHANGED_KEY\n")
                    return raw

                with (
                    patch("nmrnet_provider.startup_capture.read_startup_file", side_effect=change_after_read),
                    self.assertRaises(ConfigurationRejected),
                ):
                    capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                self.assertEqual(secret_calls, [True, True])
                self.assertFalse((snapshot / "plan.json").exists())
                self.assertEqual((snapshot / "inputs/interpreter.key").read_bytes(), b"PRIVATE_MODEL_KEY\n")

    def test_capture_ceiling_and_write_failure_leave_no_ready_plan(self):
        for failure_kind in ("ceiling", "write"):
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                total = sum(path.stat().st_size for path in (files.path, files.key, files.credential))
                failure = OSError(errno.ENOSPC, "synthetic capture write failure")
                if failure_kind == "ceiling":
                    with self.assertRaisesRegex(ConfigurationRejected, "aggregate byte ceiling"):
                        capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=total - 1)
                else:
                    with (
                        patch("nmrnet_provider.startup_capture._private_file", side_effect=failure),
                        self.assertRaises(OSError) as caught,
                    ):
                        capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=total)
                    self.assertIs(caught.exception, failure)
                self.assertFalse((snapshot / "plan.json").exists())
                self.assertFalse((Path(directory) / "state").exists())

    def test_nested_capture_paths_are_private_under_every_supported_ambient_umask(self):
        for mask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                nested_key = files.root / "keys" / "nested" / "model.key"
                nested_key.parent.mkdir(parents=True)
                files.key.rename(nested_key)
                files.write(files.raw.replace(str(files.key).encode(), str(nested_key).encode()))
                previous = os.umask(mask)
                try:
                    capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                finally:
                    os.umask(previous)
                for path in (snapshot, *snapshot.rglob("*")):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_capture_refuses_readonly_and_writable_grant_overlap_without_a_plan(self):
        for overlap in (
            "destination",
            "store",
            "parent-store",
            "linked-store",
            "ancestor-store",
            "snapshot-store",
            "snapshot-parent-store",
        ):
            with self.subTest(overlap=overlap), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                if overlap == "destination":
                    snapshot = files.root / "snapshot"
                else:
                    store = files.root / "state"
                    if overlap == "parent-store":
                        sibling = Path(directory) / "sibling"
                        sibling.mkdir()
                        store = sibling / ".." / "source" / "state"
                    elif overlap == "linked-store":
                        alias = Path(directory) / "alias"
                        alias.symlink_to(files.root, target_is_directory=True)
                        store = alias / "state"
                    elif overlap == "ancestor-store":
                        store = Path(directory)
                    elif overlap == "snapshot-store":
                        store = snapshot / "inputs" / "state"
                    elif overlap == "snapshot-parent-store":
                        store = snapshot
                    files.write(files.raw.replace(str(Path(directory) / "state").encode(), str(store).encode()))
                with self.assertRaises(ConfigurationRejected):
                    capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                self.assertFalse((snapshot / "plan.json").exists())

    def test_capture_keeps_existing_canonical_url_refusal_for_explicit_default_ports(self):
        for role in ("model", "store"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                files, snapshot = startup_capture_files(directory)
                files.write(files.raw.replace(f"https://{role}.example".encode(), f"https://{role}.example:443".encode()))
                with self.assertRaisesRegex(ConfigurationRejected, "canonical"):
                    capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                self.assertFalse((snapshot / "plan.json").exists())

    def test_capture_refuses_unsupported_authorities_without_rewriting_or_publishing_a_plan(self):
        for role in ("model", "store"):
            for authority in ("example:0", "[::1]"):
                with self.subTest(role=role, authority=authority), tempfile.TemporaryDirectory() as directory:
                    files, snapshot = startup_capture_files(directory)
                    original = f"https://{role}.example".encode()
                    files.write(files.raw.replace(original, f"https://{authority}".encode()))
                    # These are admitted by the reusable direct-mode config;
                    # the production CONNECT route has a narrower host grant.
                    load_provider_config(files.path)
                    with self.assertRaisesRegex(ValueError, "service proxy authority"):
                        capture_startup(files.path, source_root=files.root, destination=snapshot, maximum_bytes=65536)
                    self.assertFalse((snapshot / "plan.json").exists())

    def test_service_requires_explicit_presentation_and_policy_before_effects(self):
        replacements = (
            (b"[service]", b"[unknown_service]"),
            (b"[service]", b'[service]\nanalysis_kind_ref = "PRIVATE-UNSUPPORTED"'),
            (b'display_name = "NMRNet local fixture"\n', b""),
            (b'display_name = "NMRNet local fixture"', b'display_name = ""'),
            (b'display_name = "NMRNet local fixture"', b'display_name = "' + b"PRIVATE" * 22 + b'"'),
            (b"page_size = 1", b"page_size = 0"),
            (b"maximum_pages = 2", b"maximum_pages = 0"),
            (b"idle_seconds = 0.2", b"idle_seconds = true"),
            (b"observation_gap_seconds = 30\n", b""),
            (b"observation_gap_seconds = 30", b"observation_gap_seconds = 0"),
            (b"observation_gap_seconds = 30", b"observation_gap_seconds = inf"),
            (b"observation_gap_seconds = 30", b"observation_gap_seconds = true"),
            (b"api_backoff_max_seconds = 30\n", b""),
            (b"api_backoff_max_seconds = 30", b"api_backoff_max_seconds = 0.1"),
            (b"api_backoff_max_seconds = 30", b"api_backoff_max_seconds = inf"),
            (b"hello_seconds = 2", b"hello_seconds = nan"),
            (b"drain_seconds = 1", b"drain_seconds = -1"),
            (b"settle_seconds = 5", b"settle_seconds = 0"),
            (b"allocation_seconds = 30", b"allocation_seconds = 0"),
            (b"prediction_seconds = 90", b"prediction_seconds = inf"),
            (b"source_seconds = 10", b"source_seconds = 0"),
        )
        for before, after in replacements:
            with self.subTest(field=before.split(b"=")[0]), tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(directory)
                files.write(files.raw.replace(before, after))
                with (
                    patch("nmrnet_provider.native_factory.NativeAttemptFactory") as native,
                    patch("provider_core.attempt_store.AttemptStore") as store,
                    self.assertRaises(ConfigurationRejected) as caught,
                ):
                    load_provider_config(files.path)
                native.assert_not_called()
                store.assert_not_called()
                self.assertFalse(files.state.exists())
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_api_topology_is_explicit_and_distinct_from_credential_profile(self):
        for declaration in (b"", b'topology = "run"\n', b'topology = "PRIVATE"\n'):
            with self.subTest(declaration_length=len(declaration)), tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(directory)
                files.write(files.raw.replace(b'topology = "dev-local"\n', declaration))
                with self.assertRaises(ConfigurationRejected) as caught:
                    load_provider_config(files.path)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertFalse(files.state.exists())

    def test_secret_owner_and_readonly_mode_are_admitted_from_the_open_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            files.key.chmod(0o400)
            load_provider_config(files.path)
            real_stat = os.fstat
            key_identity = files.key.stat()

            def wrong_owner(descriptor):
                observed = real_stat(descriptor)
                if (observed.st_dev, observed.st_ino) != (key_identity.st_dev, key_identity.st_ino):
                    return observed
                values = list(observed)
                values[stat.ST_UID] = os.geteuid() + 1
                return os.stat_result(values)

            with (
                patch("nmrnet_provider.config.os.fstat", side_effect=wrong_owner),
                self.assertRaisesRegex(ConfigurationRejected, "interpreter key must belong"),
            ):
                load_provider_config(files.path)

    def test_nonregular_input_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            files.key.unlink()
            os.mkfifo(files.key, 0o600)
            with self.assertRaisesRegex(ConfigurationRejected, "regular file"):
                load_provider_config(files.path)

    def test_short_reads_are_completed_but_growth_is_not_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            real_read = os.read

            def short_read(descriptor, count):
                return real_read(descriptor, min(count, 17))

            with patch("nmrnet_provider.config.os.read", side_effect=short_read):
                load_provider_config(files.path)
            changed = False

            def grow_before_read(descriptor, count):
                nonlocal changed
                if not changed:
                    changed = True
                    with files.path.open("ab") as stream:
                        stream.write(b"\n")
                return real_read(descriptor, count)

            with (
                patch("nmrnet_provider.config.os.read", side_effect=grow_before_read),
                self.assertRaisesRegex(ConfigurationRejected, "changed length"),
            ):
                load_provider_config(files.path)

    def test_close_failure_after_a_successful_read_refuses_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            cleanup = OSError("PRIVATE close")
            real_close = os.close

            def close_then_fail(descriptor):
                real_close(descriptor)
                raise cleanup

            with (
                patch("nmrnet_provider.config.os.close", side_effect=close_then_fail),
                self.assertRaises(ConfigurationRejected) as caught,
            ):
                load_provider_config(files.path)
            self.assertIsNone(caught.exception.cause)
            self.assertEqual(caught.exception.cleanup_failures, (cleanup,))
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_interruption_identity_and_secondary_close_failure_are_both_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            primary, cleanup = KeyboardInterrupt(), OSError("PRIVATE close")
            previous_cause = ValueError("PRIVATE prior interruption evidence")
            primary.__cause__ = previous_cause
            real_close = os.close

            def close_then_fail(descriptor):
                real_close(descriptor)
                raise cleanup

            with (
                patch("nmrnet_provider.config.os.read", side_effect=primary),
                patch("nmrnet_provider.config.os.close", side_effect=close_then_fail),
                self.assertRaises(KeyboardInterrupt) as caught,
            ):
                load_provider_config(files.path)
            self.assertIs(caught.exception, primary)
            self.assertEqual(caught.exception.__cause__.cleanup_failures, (cleanup,))
            self.assertIs(caught.exception.__cause__.cause, previous_cause)

    def test_malformed_closed_tables_and_owned_limits_refuse_without_private_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            cases = (
                b"PRIVATE broken TOML",
                b"x" * 65537,
                b"PRIVATE = " + b"9" * 6000,
                files.raw.replace(b"max_turns = 6", b"PRIVATE_UNKNOWN_KEY = 'PRIVATE_VALUE'"),
                files.raw.replace(b"max_turns = 6", b"max_turns = 0"),
                files.raw.replace(b"max_worker_calls = 2", b"max_worker_calls = true"),
                files.raw.replace(b"generation_seconds = 5", b"generation_seconds = 1"),
                files.raw.replace(b"exchange_seconds = 2", b"exchange_seconds = nan"),
                files.raw.replace(b"exchange_seconds = 2", b"exchange_seconds = " + b"9" * 400),
                files.raw.replace(b"request_bytes = 65536", b"request_bytes = -1"),
                files.raw.replace(b'allowed_job_states = ["open"]', b'allowed_job_states = ["cancelled"]'),
                files.raw.replace(b'provider_ref = "provider:example"', b'provider_ref = "provider:other"'),
                files.raw.replace(b'profile = "dev-local"', b'profile = "run"'),
                files.raw.replace(b"https://model.example/chat", b"http://model.example/chat"),
                files.raw.replace(b'schema_id = "nmrnet.provider.runtime_config.v1"', b'schema_id = "PRIVATE"'),
                files.raw.replace(b"[api]", b'PRIVATE = "unrecognised"\n[api]'),
                files.raw.replace(
                    b'consumer_id = "lane:fixture"', b'analysis_kind_ref = "PRIVATE"\nconsumer_id = "lane:fixture"'
                ),
            )
            for candidate in cases:
                with self.subTest(length=len(candidate)):
                    files.write(candidate)
                    with self.assertRaises(ConfigurationRejected) as caught:
                        load_provider_config(files.path)
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    self.assertNotIn(directory, str(caught.exception))
                    self.assertFalse(files.state.exists())

    def test_private_input_permissions_and_final_symlinks_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            files.key.chmod(0o644)
            with self.assertRaisesRegex(ConfigurationRejected, "interpreter key"):
                load_provider_config(files.path)
            files.key.chmod(0o600)
            replacement = files.root / "replacement.key"
            files.key.rename(replacement)
            files.key.symlink_to(replacement)
            with self.assertRaisesRegex(ConfigurationRejected, "interpreter key"):
                load_provider_config(files.path)

    def test_key_text_allows_one_text_file_newline_but_no_extra_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            for value in (b" PRIVATE_MODEL_KEY\n", b"PRIVATE_MODEL_KEY\n\n", b"PRIVATE_MODEL_KEY\t", b"\xff"):
                with self.subTest(byte_length=len(value)):
                    files.key.write_bytes(value)
                    with self.assertRaises(ConfigurationRejected) as caught:
                        load_provider_config(files.path)
                    self.assertNotIn("PRIVATE_MODEL_KEY", str(caught.exception))
            files.key.write_bytes(b"PRIVATE_MODEL_KEY\r\n")
            self.assertEqual(load_provider_config(files.path).interpreter.endpoints[0].spec.api_key, "PRIVATE_MODEL_KEY")

    def test_ambient_trust_overrides_cannot_change_configured_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            with (
                patch.dict(os.environ, {"SSL_CERT_FILE": "/PRIVATE/not-configured.pem"}),
                self.assertRaises(ConfigurationRejected) as caught,
            ):
                load_provider_config(files.path)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_ambient_tls_key_logging_cannot_acquire_a_path_or_log_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            keylog = files.root / "PRIVATE-keylog"
            with patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog)}), self.assertRaises(ConfigurationRejected) as caught:
                load_provider_config(files.path)
            self.assertFalse(keylog.exists())
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_missing_key_and_empty_or_invalid_ca_refuse_with_owned_context(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            files.key.unlink()
            with self.assertRaisesRegex(ConfigurationRejected, "interpreter key"):
                load_provider_config(files.path)
            ca = files.root / "PRIVATE-ca.pem"
            files = ConfigFiles(directory, ca_file=ca)
            for content in (b"", b"PRIVATE not a certificate"):
                ca.write_bytes(content)
                with self.assertRaisesRegex(ConfigurationRejected, "API CA") as caught:
                    load_provider_config(files.path)
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_read_and_close_failure_retain_both_private_causes(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            primary, cleanup = OSError("PRIVATE read"), OSError("PRIVATE close")
            real_close = os.close

            def close_then_fail(descriptor):
                real_close(descriptor)
                raise cleanup

            with (
                patch("nmrnet_provider.config.os.read", side_effect=primary),
                patch("nmrnet_provider.config.os.close", side_effect=close_then_fail),
                self.assertRaises(ConfigurationRejected) as caught,
            ):
                load_provider_config(files.path)
            self.assertIs(caught.exception.cause, primary)
            self.assertEqual(caught.exception.cleanup_failures, (cleanup,))
            self.assertNotIn("PRIVATE", str(caught.exception))
