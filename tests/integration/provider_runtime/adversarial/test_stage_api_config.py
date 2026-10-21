"""Changed or overbroad fragments cannot authorize qualification startup."""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path

from stage_api_config import inspect_interpreter, interpreter_fragment, stage
from staging_test_ports import staging_inputs

from nmrnet_provider.config import ConfigurationRejected


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
            inspect_interpreter(fragment, selection)
            (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
            with self.assertRaises(ConfigurationRejected):
                stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment)
            self.assertFalse((root / "source/interpreter.key").exists())

    def test_private_fragment_read_keeps_the_existing_byte_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            _inputs, _selection, fragment, _key = staging_inputs(Path(temporary))
            fragment.write_bytes(b"#" + b"x" * 65536)
            with self.assertRaises(ConfigurationRejected):
                interpreter_fragment(fragment)
