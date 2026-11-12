"""Scripted probe mechanics are separate from a selected real-model decision."""

import asyncio
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interpreter_ambiguity import _run_signalled, admit_selection, batch_budget, load_cases, run_batch
from tests.integration.nmrnet.ambiguity_ports import format_carrier, scripted_carrier, selected_files, settings


class InterpreterAmbiguityProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_signal_cancels_original_operation_and_repeated_signal_preserves_cleanup(self):
        loop = asyncio.get_running_loop()
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        callbacks = {}
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        with (
            patch.object(loop, "add_signal_handler", side_effect=callbacks.__setitem__),
            patch.object(loop, "remove_signal_handler") as remove,
            patch("interpreter_ambiguity.signal.signal") as restore,
        ):
            task = asyncio.create_task(_run_signalled(operation()))
            await entered.wait()
            callbacks[signal.SIGTERM]()
            await cleaning.wait()
            callbacks[signal.SIGINT]()
            self.assertEqual(1, task.cancelling())
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual({call.args[0] for call in remove.call_args_list}, set(previous))
            self.assertEqual({call.args for call in restore.call_args_list}, set(previous.items()))

    def test_fragment_admission_reuses_exact_selected_files_and_original_clock_policies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fragment, key, selection = selected_files(root)
            selected, profile = admit_selection(fragment, selection=selection, key_file=key, probe="ambiguity")
            self.assertEqual(
                (selected.timeout_seconds, selected.endpoints[0].retirement_seconds),
                (240, 5),
            )
            self.assertEqual("PRIVATE_PROBE_KEY", selected.endpoints[0].spec.api_key)
            self.assertEqual("model.example:443", profile["authority"])
            self.assertEqual(1040, profile["process_seconds"])
            self.assertEqual(1060, profile["host_seconds"])
            self.assertEqual(profile["configuration_sha256"], json.loads(selection.read_bytes())["configuration_sha256"])
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(profile))
            self.assertFalse((root / "unmounted/model.key").exists())

    def test_deadline_covers_every_exchange_close_and_final_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = settings(Path(directory))
            self.assertEqual({"process_seconds": 260, "host_seconds": 280}, batch_budget(selected, probe="ambiguity"))

    async def test_fixed_batch_records_attributed_categories_without_preparation_or_transcripts(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"), probe="ambiguity")
        carriers = [scripted_carrier(case) for case in cases]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            with patch("interpreter_ambiguity.ChatEndpoint", side_effect=carriers):
                result = await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="ambiguity")
            self.assertEqual(result, 0)
            summary = json.loads((output / "result.json").read_bytes())
            self.assertFalse(summary["authenticated_api_jobs"])
            self.assertEqual(4, summary["cases_passed"])
            for case, carrier in zip(cases, carriers, strict=True):
                observed = json.loads((output / (case["id"] + ".json")).read_bytes())
                self.assertEqual(observed["issue"], case["expected_issue"])
                self.assertEqual(observed["expected_issue"], case["expected_issue"])
                self.assertEqual([], observed["preparation_attempts"])
                self.assertEqual("model_reported_input_problem", observed["interpretation"]["outcome"])
                self.assertFalse(observed["independently_verified"])
                self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
                self.assertNotIn("DISTINCT_REPORT_CANARY", json.dumps(observed))
                self.assertNotIn("messages", observed)
                carrier.retire.assert_awaited()
                self.assertEqual(carrier.complete.await_count, 3 if case["attachment"] else 1)
                if case["attachment"]:
                    messages = carrier.complete.await_args.args[0]
                    content = next(message["content"] for message in messages if message.get("tool_call_id") == "source")
                    self.assertEqual(
                        json.loads(content)["value"],
                        {
                            "job_ref": "job:ambiguity-" + case["id"],
                            "upload_ref": "upload:sha256:" + "1" * 64,
                            "byte_length": 512,
                            "content_hash": "sha256:" + "2" * 64,
                            "container": "direct",
                            "members": [],
                        },
                    )
                    self.assertEqual(0, observed["interpretation"]["corrections"])


class InterpreterFormatChoiceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_choices_use_production_tools_and_capture_arguments_without_preparation(self):
        cases = load_cases(Path("/fixture/interpreter_formats.json"), probe="formats")
        carriers = [
            format_carrier(case, changes={"alternate_location": "A", "member": None} if case["id"] == "pdb" else {})
            for case in cases
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            with patch("interpreter_ambiguity.ChatEndpoint", side_effect=carriers):
                result = await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64, probe="formats")
            self.assertEqual(result, 0)
            summary = json.loads((output / "result.json").read_bytes())
            self.assertEqual(3, summary["cases_passed"])
            self.assertEqual("prepare_upload_arguments", summary["evidence_scope"])
            self.assertFalse(summary["scientific_work_performed"])
            for case, carrier in zip(cases, carriers, strict=True):
                observed = json.loads((output / (case["id"] + ".json")).read_bytes())
                arguments = json.loads(observed["tool_arguments_json"])
                self.assertEqual(arguments["source_format"], case["source_format"])
                self.assertEqual(arguments["selection"], case["selection"])
                self.assertEqual("angstrom", arguments["source_unit"])
                self.assertEqual(3, observed["interpretation"]["visits"][-1]["turns_started"])
                self.assertTrue(observed["passed"])
                self.assertFalse(observed["scientific_preparation_performed"])
                self.assertNotIn("messages", observed)
                self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
                carrier.retire.assert_awaited()
                self.assertEqual(3, carrier.complete.await_count)

    def test_three_case_budget_and_admission_profile_cannot_be_mistaken_for_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fragment, key, selection = selected_files(root)
            selected, profile = admit_selection(fragment, selection=selection, key_file=key, probe="formats")
            self.assertEqual({"process_seconds": 795, "host_seconds": 815}, batch_budget(selected, probe="formats"))
            self.assertEqual("formats", profile["probe"])
            self.assertEqual(3, profile["case_count"])
