"""Reference orchestration retains failures and requires independent repeat runs."""

import json
import math
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import baseline_run


class BaselineRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prepared = self.root / "prepared"
        self.prepared.mkdir()
        self.inputs = {}
        for name in ("declaration", "selection", "lock", "source"):
            self.inputs[name] = self.root / f"{name}.json"
            self.inputs[name].write_text("{}")
        (self.prepared / "provenance.json").write_text("{}")
        self.output = self.root / "output"
        self.options = dict(prepared=self.prepared, checkpoint_root=self.root / "checkpoints",
                            declaration_path=self.inputs["declaration"], selection_path=self.inputs["selection"],
                            lock_path=self.inputs["lock"], source_path=self.inputs["source"],
                            output=self.output, user_dir=Path("/opt/nmrnet/uninmr"),
                            image_id="sha256:" + "a" * 64, upstream_revision="b" * 40,
                            label_atol=0.0001, atol=0.00001, rtol=0.000001)
        self.events = []
        self.preflight_reports = {}
        self.preflight = mock.patch.object(baseline_run.baseline_model, "preflight", side_effect=self.fake_preflight).start()
        self.execute = mock.patch.object(baseline_run.subprocess, "run", side_effect=self.fake_execute).start()
        self.inspect = mock.patch.object(baseline_run.baseline_output, "inspect_output", side_effect=self.fake_inspect).start()
        self.addCleanup(mock.patch.stopall)

    def fake_preflight(self, prepared, checkpoint_root, declaration_path, selection_path, element, results, user_dir):
        self.events.append("preflight:" + element)
        report = {"argv": [str(prepared / element), "--selected-atom", element,
                           "--path", str(checkpoint_root / element / "cv_seed_42_fold_0/checkpoint_last.pt"),
                           "--valid-subset", "valid", "--results-path", str(results), "--cpu"],
                  "pending_review": ["release log/configuration agreement", "target units"]}
        self.preflight_reports[element] = report
        return report

    def fake_execute(self, command, *, stdout, stderr, check):
        self.assertFalse((self.output / "complete.json").exists())
        self.assertEqual(self.events[:2], ["preflight:H", "preflight:C"])
        self.assertEqual(command[1:4], ["-B", "-m", "uninmr.infer"])
        self.assertEqual(stderr, subprocess.STDOUT)
        self.assertFalse(check)
        element = command[command.index("--selected-atom") + 1]
        self.events.append("execute:" + element)
        destination = Path(command[command.index("--results-path") + 1])
        self.assertTrue(destination.is_dir())
        filename = destination / "cv_seed_42_fold_0_valid.out.pkl"
        self.assertFalse(filename.exists())
        filename.write_bytes(b"independent output")
        stdout.write("upstream output retained\n")
        return SimpleNamespace(returncode=0)

    def fake_inspect(self, prepared, element, upstream_output, *, label_atol):
        self.assertFalse((self.output / "complete.json").exists())
        self.assertTrue(upstream_output.is_file())
        self.assertEqual(label_atol, self.options["label_atol"])
        self.events.append("verify:" + element)
        return {"predictions": [{"dataset_index": 0, "key_hex": "61", "atom_index": 1,
                                 "token": 4, "target": 1.0, "prediction": 1.25}],
                "label_atol": label_atol}

    def test_four_independent_invocations_and_receipt_last(self):
        report = baseline_run.run(**self.options)
        self.assertEqual(self.preflight.call_count, 2)
        self.assertEqual(self.execute.call_count, 4)
        self.assertEqual(self.inspect.call_count, 4)
        destinations = []
        for call in self.execute.call_args_list:
            command = call.args[0]
            argv = command[4:]
            element = argv[argv.index("--selected-atom") + 1]
            original = self.preflight_reports[element]["argv"]
            index = argv.index("--results-path") + 1
            destinations.append(argv[index])
            self.assertEqual(argv[:index] + argv[index + 1:], original[:index] + original[index + 1:])
        self.assertEqual(len(set(destinations)), 4)
        self.assertEqual(report, json.loads((self.output / "complete.json").read_text()))
        self.assertEqual(report["image_id"], self.options["image_id"])
        self.assertEqual(report["upstream_revision"], self.options["upstream_revision"])
        self.assertEqual(report["inputs"]["unicore_source"], {
            "path": str(self.inputs["source"]),
            **baseline_run.baseline_prepare.file_identity(self.inputs["source"])})
        self.assertEqual(set(report["pending_review"]), {"target units", "release log/configuration agreement"})
        self.assertIn("children_max_rss_kib", report["resources"])
        self.assertIn("process lifetime", report["resources"]["scope"])
        self.assertEqual(report["comparisons"]["H"]["atol"], self.options["atol"])
        self.assertEqual(report["comparisons"]["C"]["rtol"], self.options["rtol"])
        for directory in destinations:
            self.assertIn("upstream output", (Path(directory) / "inference.log").read_text())
            self.assertTrue((Path(directory) / "verified.json").is_file())

    def test_nonzero_exit_preserves_both_outputs_and_stops(self):
        def fail_second(command, **kwargs):
            result = self.fake_execute(command, **kwargs)
            if self.execute.call_count == 2:
                result.returncode = 7
            return result
        self.execute.side_effect = fail_second
        with self.assertRaisesRegex(RuntimeError, "exit.*7"):
            baseline_run.run(**self.options)
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(len(list(self.output.rglob("*.out.pkl"))), 2)
        self.assertEqual(len(list(self.output.rglob("verified.json"))), 1)
        self.assertFalse((self.output / "complete.json").exists())
        self.assertTrue((self.output / "attempt.json").exists())
        statuses = [json.loads(path.read_text())["returncode"] for path in self.output.rglob("execution.json")]
        self.assertEqual(sorted(statuses), [0, 7])

    def test_second_preflight_failure_prevents_every_inference(self):
        def fail_carbon(*args):
            if args[4] == "C":
                raise ValueError("checkpoint mismatch")
            return self.fake_preflight(*args)
        self.preflight.side_effect = fail_carbon
        with self.assertRaisesRegex(ValueError, "checkpoint mismatch"):
            baseline_run.run(**self.options)
        self.execute.assert_not_called()
        self.assertTrue((self.output / "H" / "preflight.json").is_file())
        self.assertFalse((self.output / "complete.json").exists())

    def test_output_validation_failure_is_not_a_completed_run(self):
        self.inspect.side_effect = ValueError("missing prepared records")
        with self.assertRaisesRegex(ValueError, "missing prepared records"):
            baseline_run.run(**self.options)
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(len(list(self.output.rglob("*.out.pkl"))), 1)
        self.assertFalse((self.output / "complete.json").exists())

    def test_repeat_mismatch_retains_evidence_without_relaxing_tolerances(self):
        def changed(*args, **kwargs):
            result = self.fake_inspect(*args, **kwargs)
            if self.inspect.call_count == 2:
                result["predictions"][0]["prediction"] = 99.0
            return result
        self.inspect.side_effect = changed
        with self.assertRaisesRegex(ValueError, "Cannot reproduce"):
            baseline_run.run(**self.options)
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(len(list(self.output.rglob("verified.json"))), 2)
        self.assertFalse((self.output / "complete.json").exists())

    def test_existing_destination_and_symlink_are_not_reused(self):
        self.output.mkdir()
        marker = self.output / "existing"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            baseline_run.run(**self.options)
        self.assertEqual(marker.read_text(), "keep")
        link = self.root / "link"
        link.symlink_to(self.output)
        with self.assertRaises(FileExistsError):
            baseline_run.run(**{**self.options, "output": link})
        self.preflight.assert_not_called()

    def test_invalid_tolerance_rejected_before_creating_output(self):
        for name in ("label_atol", "atol", "rtol"):
            for value in (-1.0, math.nan, math.inf, True):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    baseline_run.run(**{**self.options, name: value})
        self.assertFalse(self.output.exists())
        self.preflight.assert_not_called()

    def test_immutable_runtime_identities_required(self):
        for name, value in (("image_id", "latest"), ("upstream_revision", "main")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                baseline_run.run(**{**self.options, name: value})
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
