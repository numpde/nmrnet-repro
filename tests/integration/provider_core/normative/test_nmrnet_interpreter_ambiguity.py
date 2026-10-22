"""Scripted probe mechanics are separate from a selected real-model decision."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interpreter_ambiguity import admit_selection, batch_budget, load_cases, run_batch
from tests.integration.nmrnet.ambiguity_ports import scripted_carrier, selected_files, settings


class InterpreterAmbiguityProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_fragment_admission_reuses_exact_selected_files_and_original_clock_policies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fragment, key, selection = selected_files(root)
            selected, profile = admit_selection(fragment, selection=selection, key_file=key)
            self.assertEqual(
                (selected.max_turns, selected.endpoints[0].http.exchange_seconds, selected.endpoints[0].http.retirement_seconds),
                (6, 45, 45),
            )
            self.assertEqual(selected.endpoints[0].spec.api_key, "PRIVATE_PROBE_KEY")
            self.assertEqual(profile["authority"], "model.example:443")
            self.assertEqual(profile["process_seconds"], 2400)
            self.assertEqual(profile["host_seconds"], 2420)
            self.assertEqual(profile["configuration_sha256"], json.loads(selection.read_bytes())["configuration_sha256"])
            self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(profile))
            self.assertFalse((root / "unmounted/model.key").exists())

    def test_deadline_covers_every_exchange_close_and_final_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = settings(Path(directory))
            self.assertEqual(batch_budget(selected), {"process_seconds": 2400, "host_seconds": 2420})

    async def test_fixed_batch_records_attributed_categories_without_preparation_or_transcripts(self):
        cases = load_cases(Path("/fixture/interpreter_ambiguity.json"))
        carriers = [scripted_carrier(case) for case in cases]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir(mode=0o700)
            with patch("interpreter_ambiguity.ChatEndpoint", side_effect=carriers):
                result = await run_batch(settings(root), cases, output=output, configuration_sha256="a" * 64)
            self.assertEqual(result, 0)
            summary = json.loads((output / "result.json").read_bytes())
            self.assertFalse(summary["authenticated_api_jobs"])
            self.assertEqual(summary["cases_passed"], 4)
            for case, carrier in zip(cases, carriers, strict=True):
                observed = json.loads((output / (case["id"] + ".json")).read_bytes())
                self.assertEqual(observed["issue"], case["expected_issue"])
                self.assertEqual(observed["expected_issue"], case["expected_issue"])
                self.assertEqual(observed["preparation_attempts"], [])
                self.assertEqual(observed["interpretation"]["outcome"], "model_reported_input_problem")
                self.assertFalse(observed["independently_verified"])
                self.assertNotIn("PRIVATE_PROBE_KEY", json.dumps(observed))
                self.assertNotIn("DISTINCT_REPORT_CANARY", json.dumps(observed))
                self.assertNotIn("messages", observed)
                carrier.retire.assert_awaited_once()
                self.assertEqual(carrier.complete.await_count, 2 if case["attachment"] else 1)
