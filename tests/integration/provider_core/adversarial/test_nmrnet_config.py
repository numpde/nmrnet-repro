"""Configuration refusals happen before runtime effects."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.integration.nmrnet.config_ports import ConfigFiles, execution_source_files

from nmrnet_provider.config import ConfigurationRejected, load_provider_config
from nmrnet_provider.launch_capture import capture_execution
from nmrnet_provider.startup_capture import startup_authorities


class ProviderConfigurationBoundaryTests(unittest.TestCase):
    def test_missing_packaged_offering_refuses_startup_and_preserves_the_file_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            failure = FileNotFoundError("packaged offering is missing")
            with patch.object(Path, "read_text", side_effect=failure):
                with self.assertRaisesRegex(ConfigurationRejected, "packaged offering") as raised:
                    load_provider_config(files.path)
            self.assertIs(raised.exception.cause, failure)
            self.assertFalse(files.state.exists())

    def test_live_limits_are_explicit_positive_integers_before_startup_effects(self):
        for field, current in (("maximum_live_attempts", "2"), ("maximum_control_bytes", "262144"),
                               ("maximum_pending_terminal_bytes", "4194304")):
            for value in (None, "0", "-1", "true", '"64"'):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                    files = ConfigFiles(directory)
                    replacement = b"" if value is None else f"{field} = {value}\n".encode()
                    files.write(files.raw.replace(f"{field} = {current}\n".encode(), replacement))
                    with self.assertRaises(ConfigurationRejected):
                        load_provider_config(files.path)
                    self.assertFalse(files.state.exists())

    def test_displaced_history_and_reservation_fields_are_not_accepted_as_compatibility_policy(self):
        for declaration in (b"confirmed_history_records = 64\n", b"filesystem_reserve_bytes = 0\n",
                            b"store_root = \"/legacy\"\n", b"store_seconds = 10\n"):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(directory)
                files.write(files.raw.replace(b"[lifecycle]\n", b"[lifecycle]\n" + declaration))
                with self.assertRaises(ConfigurationRejected):
                    load_provider_config(files.path)
                self.assertFalse(files.state.exists())

    def test_direct_execution_sources_refuse_invalid_grants_or_identities_without_publication(self):
        for fault in ("missing", "linked", "empty_names", "duplicate_names", "escaping_name", "absolute_name", "oversized", "image", "revision"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                source_root, names = execution_source_files(directory)
                destination = Path(directory) / "declaration"
                destination.mkdir(mode=0o700)
                identities = {
                    "image_id": "sha256:" + "b" * 64,
                    "geometry_image_id": "sha256:" + "c" * 64,
                    "controller_image_id": "sha256:" + "d" * 64,
                    "repository_revision": "a" * 40,
                    "upstream_revision": "e" * 40,
                }
                source = source_root / "tools/worker.py"
                if fault == "missing":
                    source.unlink()
                elif fault == "linked":
                    source.unlink()
                    source.symlink_to("reader.py")
                elif fault == "empty_names":
                    names = ()
                elif fault == "duplicate_names":
                    names = (*names, names[-1])
                elif fault == "escaping_name":
                    names = ("../private-source",)
                elif fault == "absolute_name":
                    names = (str(source),)
                elif fault == "oversized":
                    source.write_bytes(b"x" * (1048576 + 1))
                elif fault == "image":
                    identities["image_id"] = "mutable:latest"
                else:
                    identities["repository_revision"] = "invalid-revision"
                with self.assertRaises((ConfigurationRejected, OSError)):
                    capture_execution(source_root, destination, names, **identities)
                self.assertEqual(list(destination.iterdir()), [])

    def test_authority_admission_keeps_canonical_url_refusal_for_explicit_default_ports(self):
        for role in ("model", "store"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(Path(directory))
                files.write(files.raw.replace(f"https://{role}.example".encode(), f"https://{role}.example:443".encode()))
                with self.assertRaisesRegex(ConfigurationRejected, "canonical"):
                    startup_authorities(load_provider_config(files.path))

    def test_authority_admission_refuses_unsupported_destinations_without_rewriting_them(self):
        for role in ("model", "store"):
            for authority in ("example:0", "[::1]"):
                with self.subTest(role=role, authority=authority), tempfile.TemporaryDirectory() as directory:
                    files = ConfigFiles(Path(directory))
                    original = f"https://{role}.example".encode()
                    files.write(files.raw.replace(original, f"https://{authority}".encode()))
                    # These are admitted by the reusable direct-mode config;
                    # the production CONNECT route has a narrower host grant.
                    load_provider_config(files.path)
                    with self.assertRaisesRegex(ValueError, "service proxy authority"):
                        startup_authorities(load_provider_config(files.path))

    def test_service_requires_explicit_presentation_and_policy_before_effects(self):
        replacements = (
            (b"[service]", b"[unknown_service]"),
            (b"[service]", b'[service]\nanalysis_kind_ref = "PRIVATE-UNSUPPORTED"'),
            (b"[service]", b'[service]\noffering_description = "Obsolete inline override"'),
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
            (b"[lifecycle]\n", b"[lifecycle]\nstart_retry_window_seconds = 30\n"),
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
                    patch("nmrnet_provider.process.NativeAttemptFactory") as native,
                    patch("nmrnet_provider.process.ProviderLifetime") as lifetime,
                    self.assertRaises(ConfigurationRejected) as caught,
                ):
                    load_provider_config(files.path)
                native.assert_not_called()
                lifetime.assert_not_called()
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
                patch("provider_core.startup_file.os.fstat", side_effect=wrong_owner),
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

            with patch("provider_core.startup_file.os.read", side_effect=short_read):
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
                patch("provider_core.startup_file.os.read", side_effect=grow_before_read),
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
                patch("provider_core.startup_file.os.close", side_effect=close_then_fail),
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
                patch("provider_core.startup_file.os.read", side_effect=primary),
                patch("provider_core.startup_file.os.close", side_effect=close_then_fail),
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
                files.raw.replace(b'schema_id = "nmrnet.provider.runtime_config.v2"', b'schema_id = "PRIVATE"'),
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
                patch("provider_core.startup_file.os.read", side_effect=primary),
                patch("provider_core.startup_file.os.close", side_effect=close_then_fail),
                self.assertRaises(ConfigurationRejected) as caught,
            ):
                load_provider_config(files.path)
            self.assertIs(caught.exception.cause, primary)
            self.assertEqual(caught.exception.cleanup_failures, (cleanup,))
            self.assertNotIn("PRIVATE", str(caught.exception))
