"""Offline staging admits one endpoint and freezes its separate private inputs."""

from tools.container_guard import require_container

require_container()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomllib
from stage_api_config import inspect_interpreter, stage
from staging_test_ports import staged_real_config, staging_inputs, upload_sources

from nmrnet_provider.config import load_provider_config


class StageApiConfigTests(unittest.TestCase):
    def test_qualification_offering_uses_the_operator_example_without_replacing_its_config_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged_real_config(root)
            document = tomllib.loads((root / "source/provider.toml").read_text())
            example = tomllib.loads(Path("/fixture/provider.toml.example").read_text())
            for field in ("description", "offering_description"):
                self.assertEqual(document["service"][field], example["service"][field])
            self.assertIn("explicit conformer count", document["service"]["offering_description"])

    def test_file_inspection_does_not_require_or_read_a_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _inputs, selection, fragment, key = staging_inputs(root)
            inspect_interpreter(fragment, selection)
            self.assertFalse(key.exists())
            self.assertEqual((selection / "key_file.txt").read_text(), str(root / "unmounted-selection/model.key") + "\n")
            self.assertEqual((selection / "ca_file.txt").read_text(), "\n")
            self.assertEqual((selection / "interpreter-inputs.json").stat().st_mode & 0o777, 0o600)

    def test_selected_fragment_and_exact_key_are_frozen_before_normal_configuration_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, selection, fragment, key = staging_inputs(root)
            key.write_bytes(b"synthetic-fixture-key\n")
            key.chmod(0o600)
            self.assertFalse((root / "unmounted-selection/model.key").exists())
            inspect_interpreter(fragment, selection)
            (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
            stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment)
            admitted = load_provider_config(root / "source/provider.toml")
            endpoint = admitted.interpreter.endpoints[0]
            self.assertIn("https://numpde.github.io/nmrnet-repro/", admitted.service.announcement.description)
            self.assertEqual((endpoint.spec.configuration_id, endpoint.spec.model), ("selected-fixture", "selected-model"))
            self.assertEqual(endpoint.spec.reasoning_effort, "none")
            self.assertEqual(endpoint.spec.api_key, "synthetic-fixture-key")
            self.assertEqual(
                (admitted.interpreter.max_turns, endpoint.http.exchange_seconds, endpoint.http.retirement_seconds), (6, 25, 5)
            )
            self.assertEqual(
                (endpoint.request_bytes, endpoint.response_bytes, admitted.preparation.seconds), (65536, 262144, 240)
            )
            self.assertEqual(admitted.uploads.limits.seconds, 300)
            self.assertEqual((admitted.api.http.exchange_seconds, admitted.api.http.retirement_seconds), (2, 1))
            self.assertEqual((admitted.uploads.http.exchange_seconds, admitted.uploads.http.retirement_seconds), (2, 1))
            self.assertEqual((root / "source/interpreter.key").read_bytes(), key.read_bytes())
            config = tomllib.loads((root / "source/provider.toml").read_text())
            self.assertEqual(config["interpreter"]["endpoints"][0]["key_file"], str(root / "source/interpreter.key"))
            self.assertNotIn("ca_file", config["interpreter"]["endpoints"][0])
            identity = json.loads((root / "source/identity.json").read_bytes())
            self.assertFalse(identity["interpretation_scripted"])
            self.assertEqual(identity["interpreter_max_turns"], admitted.interpreter.max_turns)
            self.assertEqual(identity["interpreter"]["model"], "selected-model")
            self.assertNotIn("synthetic-fixture-key", json.dumps(identity))

    def test_optional_interpreter_trust_is_frozen_with_the_selected_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, selection, fragment, key = staging_inputs(root)
            key.write_bytes(b"synthetic-fixture-key\n")
            key.chmod(0o600)
            selected_ca = inputs / "selected-ca.pem"
            selected_ca.write_bytes((inputs / "api-ca.crt").read_bytes())
            (inputs / "interpreter-ca.pem").write_bytes(selected_ca.read_bytes())
            with fragment.open("a") as stream:
                stream.write("ca_file = " + json.dumps(str(selected_ca)) + "\n")
            inspect_interpreter(fragment, selection)
            (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
            stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment)
            self.assertEqual((root / "source/model-ca.pem").read_bytes(), selected_ca.read_bytes())
            self.assertEqual((selection / "ca_file.txt").read_text(), str(selected_ca) + "\n")
            admitted = tomllib.loads((root / "source/provider.toml").read_text())
            self.assertEqual(admitted["interpreter"]["endpoints"][0]["ca_file"], str(root / "source/model-ca.pem"))

    def test_preparation_budget_does_not_rewrite_selected_endpoint_deadlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, selection, fragment, key = staging_inputs(root)
            key.write_bytes(b"synthetic-fixture-key\n")
            key.chmod(0o600)
            fragment.write_text(
                fragment.read_text()
                .replace("exchange_seconds = 25", "exchange_seconds = 45")
                .replace("retirement_seconds = 5", "retirement_seconds = 45")
            )
            inspect_interpreter(fragment, selection)
            (inputs / "interpreter-inputs.json").write_bytes((selection / "interpreter-inputs.json").read_bytes())
            stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=fragment)
            admitted = load_provider_config(root / "source/provider.toml")
            self.assertEqual(admitted.preparation.seconds, 240)
            self.assertEqual(
                (
                    admitted.interpreter.endpoints[0].http.exchange_seconds,
                    admitted.interpreter.endpoints[0].http.retirement_seconds,
                ),
                (45, 45),
            )

    def test_scripted_staging_retains_its_fixed_endpoint_and_preparation_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, _selection, _fragment, _key = staging_inputs(root)
            (inputs / "model-ca.pem").write_bytes((inputs / "api-ca.crt").read_bytes())
            (inputs / "model-ready.json").write_text('{"origin":"https://localhost:9443"}\n')
            stage(root, "https://api.example:9443", "dev-local", inputs=inputs, interpreter_config=None)
            admitted = load_provider_config(root / "source/provider.toml")
            self.assertEqual(admitted.interpreter.endpoints[0].spec.model, "fixture-model")
            self.assertEqual(admitted.preparation.seconds, 45)
            self.assertEqual(admitted.uploads.limits.seconds, 10)
            self.assertTrue(json.loads((root / "source/identity.json").read_bytes())["interpretation_scripted"])


class StageUploadBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_upload_session_covers_delayed_inspect_and_acquire_past_the_old_ten_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = staged_real_config(Path(temporary))
            with patch("provider_core.uploads.time") as clock:
                clock.monotonic.return_value = 100
                owner, api, store, upload_ref = upload_sources(config.uploads.limits)
                clock.monotonic.return_value = 111
                await owner.inspect()
                clock.monotonic.return_value = 340
                verified = await owner.acquire(upload_ref)
            self.assertEqual(verified.data, b"synthetic coordinates")
            self.assertEqual(owner.requests_started, 4)
            self.assertEqual(api.uploads.await_count, 2)
            api.read_grant.assert_awaited_once_with("job:example", upload_ref)
            store.exchange.assert_awaited_once()
