"""Startup refusals happen before network, native allocation or journal creation."""

import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests.integration.nmrnet.config_ports import ConfigFiles

from nmrnet_provider.config import ConfigurationRejected, load_provider_config


class ProviderConfigurationBoundaryTests(unittest.TestCase):
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
