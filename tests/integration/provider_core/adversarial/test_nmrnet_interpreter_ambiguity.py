"""Wrong decisions and operational failures stop the batch without science or retry."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interpreter_ambiguity import admit_selection, load_cases, run_batch
from tests.integration.nmrnet.ambiguity_ports import scripted_carrier, selected_files, settings

from nmrnet_provider.config import read_startup_file


class InterpreterAmbiguityProbeRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_preparation_stops_before_argument_feedback_or_a_later_correct_report(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"))
        for name, arguments in (
            ("prepare_smiles", {"smiles": "CCl"}),
            ("prepare_smiles", {"smiles": "CCl", "requested_count": None}),
            ("prepare_pasted", {}),
            ("prepare_upload", {}),
        ):
            with self.subTest(name=name, arguments=arguments), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir(mode=0o700)
                carrier = scripted_carrier(cases[0], prepare=True, prepare_name=name, prepare_arguments=arguments)
                with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                    self.assertEqual(await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64), 1)
                factory.assert_called_once()
                self.assertEqual(carrier.complete.await_count, 1)
                observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
                self.assertEqual(observed["preparation_attempts"], [name])
                self.assertNotIn("issue", observed)
                self.assertFalse((output / (cases[1]["id"] + ".json")).exists())

    async def test_malformed_json_then_expected_report_cannot_qualify(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"))
        carrier = scripted_carrier(cases[0], prepare=True)
        replies = list(carrier.complete.side_effect)
        replies[0]["tool_calls"][0]["function"]["arguments"] = '{"PRIVATE_PROBE_KEY":"DISTINCT_ARGUMENT_CANARY"'
        carrier.complete.side_effect = replies
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                self.assertEqual(await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64), 1)
            factory.assert_called_once()
            self.assertEqual(carrier.complete.await_count, 2)
            observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
            self.assertIn("issue", observed, json.dumps(observed))
            self.assertEqual(observed["issue"], cases[0]["expected_issue"])
            self.assertEqual(observed["interpretation"]["corrections"], 1)
            self.assertEqual(observed["interpreter_diagnostics"][0]["turn_index"], 1)
            self.assertEqual(observed["interpreter_diagnostics"][0]["failure"]["exception_type"], "ToolRejected")
            self.assertIn("JSON object", observed["interpreter_diagnostics"][0]["failure"]["message"])
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
            self.assertNotIn("DISTINCT_ARGUMENT_CANARY", json.dumps(observed))
            self.assertFalse(observed["passed"])
            self.assertFalse((output / (cases[1]["id"] + ".json")).exists())

    async def test_cancellation_keeps_the_original_retirement_until_it_finishes(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"))
        started, release = asyncio.Event(), asyncio.Event()
        carrier = scripted_carrier(cases[0])

        async def retire():
            started.set()
            await release.wait()

        carrier.retire.side_effect = retire
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                task = asyncio.create_task(run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64))
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                carrier.retire.assert_awaited_once()
                factory.assert_called_once()
                release.set()
                self.assertEqual(await asyncio.wait_for(task, 2), 1)
            self.assertFalse((output / (cases[1]["id"] + ".json")).exists())

    def test_changed_fragment_refuses_before_reading_the_granted_key(self):
        with tempfile.TemporaryDirectory() as directory:
            fragment, key, selection = selected_files(Path(directory))
            fragment.write_text(fragment.read_text() + "\n# changed after exact grants selected\n")
            with (
                patch("interpreter_ambiguity.read_startup_file", wraps=read_startup_file) as read_key,
                self.assertRaisesRegex(ValueError, "changed"),
            ):
                admit_selection(fragment, selection=selection, key_file=key)
            self.assertFalse(any(call.args[0] == key for call in read_key.call_args_list))

    async def test_wrong_category_or_attempted_preparation_stops_before_the_next_case(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"))
        for prepare in (False, True):
            with self.subTest(prepare=prepare), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir(mode=0o700)
                carrier = scripted_carrier(cases[0], issue="other_input_problem", prepare=prepare)
                with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                    result = await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64)
                self.assertEqual(result, 1)
                factory.assert_called_once()
                self.assertEqual(carrier.complete.await_count, 1)
                observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
                self.assertEqual(observed["preparation_attempts"], ["prepare_smiles"] if prepare else [])
                self.assertFalse((output / (cases[1]["id"] + ".json")).exists())
                self.assertFalse(json.loads((output / "result.json").read_bytes())["passed"])

    async def test_operational_failure_is_not_an_input_problem_or_retry(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            carrier = scripted_carrier(cases[0], operational=OSError("PRIVATE_PROBE_KEY operator cause"))
            with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                self.assertEqual(await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64), 1)
            factory.assert_called_once()
            self.assertEqual(carrier.complete.await_count, 1)
            observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
            self.assertNotIn("issue", observed)
            self.assertEqual(observed["preparation_attempts"], [])
            self.assertIn("OSError", json.dumps(observed))
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
