"""Changed or overbroad fragments cannot authorize qualification startup."""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stage_api_config import inspect_interpreter, interpreter_fragment, stage
from staging_test_ports import staged_real_config, staging_inputs, upload_sources

from nmrnet_provider.config import ConfigurationRejected
from provider_core.uploads import UploadFailed


class StageApiConfigAdversarialTests(unittest.TestCase):
    def test_changed_fragment_and_multiple_endpoints_cannot_authorize_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, selection, fragment, _key = staging_inputs(root)
            inspect_interpreter(fragment, selection)
            (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
            original = fragment.read_text()
            fragment.write_text(original.replace('model = "selected-model"', 'model = "changed-model"'))
            with self.assertRaisesRegex(ValueError, "changed after"):
                stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment)
            fragment.write_text(original + '\n[[interpreter.endpoints]]\nkey_file = "/ungranted/key"\n')
            with self.assertRaisesRegex(ValueError, "exactly one endpoint"):
                interpreter_fragment(fragment)

    def test_selected_key_still_requires_the_existing_private_descriptor_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, selection, fragment, key = staging_inputs(root)
            key.write_bytes(b"synthetic-fixture-key\n")
            key.chmod(0o644)
            self.assertTrue((inputs / "interpreter.key").is_file())
            inspect_interpreter(fragment, selection)
            (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
            with self.assertRaisesRegex(ConfigurationRejected, "0400 or 0600"):
                stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment)
            self.assertFalse((root / "source/interpreter.key").exists())

    def test_private_fragment_read_keeps_the_existing_byte_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            _inputs, _selection, fragment, _key = staging_inputs(Path(temporary))
            fragment.write_bytes(b"#" + b"x" * 65536)
            with self.assertRaises(ConfigurationRejected):
                interpreter_fragment(fragment)


class StageUploadBudgetAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_deadline_is_not_reset_and_expiry_cannot_issue_another_grant_or_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = staged_real_config(Path(temporary))
            with patch("provider_core.uploads.time") as clock:
                clock.monotonic.return_value = 100
                owner, api, store, upload_ref = upload_sources(config.uploads.limits)
                clock.monotonic.return_value = 111
                await owner.inspect()
                clock.monotonic.return_value = 340
                await owner.acquire(upload_ref)
                clock.monotonic.return_value = 400
                with self.assertRaisesRegex(UploadFailed, "total deadline expired"):
                    await owner.acquire(upload_ref)
            self.assertEqual(owner.requests_started, 4)
            self.assertEqual(api.uploads.await_count, 2)
            api.read_grant.assert_awaited_once_with("job:example", upload_ref)
            store.exchange.assert_awaited_once()
