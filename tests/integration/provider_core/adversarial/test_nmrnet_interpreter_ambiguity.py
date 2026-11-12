"""Wrong decisions and operational failures stop the batch without science or retry."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interpreter_ambiguity import admit_selection, load_cases, run_batch
from tests.integration.nmrnet.ambiguity_ports import format_carrier, scripted_carrier, selected_files, settings

from nmrnet_provider.config import read_startup_file
from provider_core.chat_errors import ChatFailed


class InterpreterAmbiguityProbeRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_preparation_stops_before_argument_feedback_or_a_later_correct_report(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
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
                    self.assertEqual(
                        await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity"), 1
                    )
                factory.assert_called_once()
                self.assertEqual(1, carrier.complete.await_count)
                observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
                self.assertEqual(observed["preparation_attempts"], [name])
                self.assertNotIn("issue", observed)
                self.assertFalse((output / (cases[1]["id"] + ".json")).exists())

    async def test_malformed_json_then_expected_report_cannot_qualify(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
        carrier = scripted_carrier(cases[0], prepare=True)
        replies = list(carrier.complete.side_effect)
        replies[0]["tool_calls"][0]["function"]["arguments"] = '{"PRIVATE_PROBE_KEY":"DISTINCT_ARGUMENT_CANARY"'
        carrier.complete.side_effect = replies
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                self.assertEqual(
                    await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity"), 1
                )
            factory.assert_called_once()
            self.assertEqual(2, carrier.complete.await_count)
            observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
            self.assertIn("issue", observed, msg=json.dumps(observed))
            self.assertEqual(observed["issue"], cases[0]["expected_issue"])
            self.assertEqual(1, observed["interpretation"]["corrections"])
            correction = next(item for item in observed["interpreter_diagnostics"] if item.get("turn_index") == 1)
            self.assertEqual("ToolRejected", correction["failure"]["exception_type"])
            self.assertIn("JSON object", correction["failure"]["message"])
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
            self.assertNotIn("DISTINCT_ARGUMENT_CANARY", json.dumps(observed))
            self.assertFalse(observed["passed"])
            self.assertFalse((output / (cases[1]["id"] + ".json")).exists())

    async def test_cancellation_keeps_the_original_retirement_until_it_finishes(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
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
                task = asyncio.create_task(
                    run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity")
                )
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
                admit_selection(fragment, selection=selection, key_file=key, probe="ambiguity")
            self.assertFalse(any(call.args[0] == key for call in read_key.call_args_list))

    async def test_wrong_category_or_attempted_preparation_stops_before_the_next_case(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
        for prepare in (False, True):
            with self.subTest(prepare=prepare), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir(mode=0o700)
                carrier = scripted_carrier(cases[0], issue="other_input_problem", prepare=prepare)
                with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                    result = await run_batch(
                        settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity"
                    )
                self.assertEqual(result, 1)
                factory.assert_called_once()
                self.assertEqual(1, carrier.complete.await_count)
                observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
                self.assertEqual(observed["preparation_attempts"], ["prepare_smiles"] if prepare else [])
                self.assertFalse((output / (cases[1]["id"] + ".json")).exists())
                self.assertFalse(json.loads((output / "result.json").read_bytes())["passed"])

    async def test_operational_failure_is_not_an_input_problem_or_retry(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            carrier = scripted_carrier(cases[0], operational=OSError("PRIVATE_PROBE_KEY operator cause"))
            with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                self.assertEqual(
                    await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity"), 1
                )
            factory.assert_called_once()
            self.assertEqual(1, carrier.complete.await_count)
            observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
            self.assertNotIn("issue", observed)
            self.assertEqual([], observed["preparation_attempts"])
            self.assertIn("OSError", json.dumps(observed))
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))

    async def test_exhausted_conversation_without_report_stops_and_redacts(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            failure = ChatFailed("request_limit", "PRIVATE_PROBE_KEY operator cause")
            carrier = scripted_carrier(cases[0], operational=failure)
            with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                result = await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity")
            self.assertEqual(1, result)
            factory.assert_called_once()
            observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
            self.assertNotIn("issue", observed)
            self.assertFalse(observed["passed"])
            self.assertEqual("input interpretation", observed["failures"][0]["operation"])
            self.assertEqual("InterpretationFailed", observed["failures"][0]["failure"]["exception_type"])
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
            self.assertFalse((output / (cases[1]["id"] + ".json")).exists())
            carrier.retire.assert_awaited()


class InterpreterFormatChoiceRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_choice_stops_without_coaching_or_next_case_and_redacts_captured_text(self):
        cases = load_cases(Path("/fixture/interpreter_formats.json"), probe="formats")
        for changes in (
            {"source_format": "PRIVATE_PROBE_KEY"},
            {"source_unit": "nm"},
            {"selection": [1]},
            {"upload_ref": "upload:sha256:" + "9" * 64},
            {"member": "unexpected.mol"},
            {"member": "Bearer DISTINCT_CHOICE_CANARY"},
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir(mode=0o700)
                carrier = format_carrier(cases[0], changes=changes)
                with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier) as factory:
                    result = await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="formats")
                self.assertEqual(result, 1)
                factory.assert_called_once()
                self.assertEqual(3, carrier.complete.await_count)
                carrier.retire.assert_awaited()
                observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
                self.assertFalse(observed["passed"])
                self.assertFalse(observed["scientific_preparation_performed"])
                self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
                self.assertNotIn("DISTINCT_CHOICE_CANARY", json.dumps(observed))
                self.assertFalse((output / (cases[1]["id"] + ".json")).exists())

    async def test_valid_reader_selection_in_the_wrong_order_does_not_qualify(self):
        cases = load_cases(Path("/fixture/interpreter_formats.json"), probe="formats")
        for case in cases[1:]:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir(mode=0o700)
                carrier = format_carrier(case, changes={"selection": [1, 2]})
                with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier):
                    result = await run_batch(
                        settings(root), [case], output=output, configuration_sha256="a" * 64, probe="formats"
                    )
                self.assertEqual(result, 1)
                observed = json.loads((output / (case["id"] + ".json")).read_bytes())
                self.assertFalse(observed["passed"])
                self.assertIn("record order", json.dumps(observed))
                self.assertEqual(3, carrier.complete.await_count)

    async def test_wrong_preparation_tool_and_model_problem_are_not_format_choice_success(self):
        cases = load_cases(Path("/fixture/interpreter_formats.json"), probe="formats")
        for problem in (False, True):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir(mode=0o700)
                carrier = scripted_carrier({"attachment": False, "expected_issue": "other_input_problem"}, prepare=not problem)
                with patch("interpreter_ambiguity.ChatEndpoint", return_value=carrier):
                    self.assertEqual(
                        await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="formats"), 1
                    )
                self.assertEqual(1, carrier.complete.await_count)
                carrier.retire.assert_awaited()
                observed = json.loads((output / (cases[0]["id"] + ".json")).read_bytes())
                self.assertFalse(observed["passed"])
                self.assertNotIn("tool_arguments_json", observed)
