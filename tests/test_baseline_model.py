"""Model qualification fails before inference on pairing or load differences."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tools import baseline_model, baseline_prepare


class ModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = {"name": "checkpoints.zip", "size": 10, "md5": "a" * 32}
        self.members = []
        for element in ("H", "C"):
            for suffix in ("cv_seed_42_fold_0/checkpoint_last.pt", "target_scaler.ss",
                           "infer.log", "finetune.log"):
                name = f"checkpoints/scratch_{element}_fixture/{suffix}"
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{element}: {suffix}".encode())
                self.members.append(name)
        self.provenance = {"archive": self.archive, "archive_sha256": "b" * 64,
                           "members": {name: baseline_prepare.file_identity(self.root / name)
                                       for name in self.members}}
        self.write_provenance()
        self.selection = {"members": self.members, "max_bytes": 4096}

    def write_provenance(self):
        (self.root / "provenance.json").write_text(json.dumps(self.provenance))

    def test_pairs_checkpoint_scaler_and_logs_from_one_selected_configuration(self):
        paths, identities = baseline_model.model_artifacts(self.root, self.archive, self.selection, "H")
        self.assertEqual(paths["checkpoint"].parent.name, "cv_seed_42_fold_0")
        self.assertEqual(paths["checkpoint"].parent.parent, paths["scaler"].parent)
        self.assertEqual(set(identities), {"checkpoint", "scaler", "infer_log", "finetune_log"})
        self.assertTrue(all("scratch_H_fixture/" in item["member"] for item in identities.values()))

    def test_missing_pair_or_ambiguous_checkpoint_fails(self):
        for members in ([name for name in self.members if not name.endswith("target_scaler.ss")],
                        self.members + ["checkpoints/scratch_H_other/cv_seed_42_fold_0/checkpoint_last.pt"]):
            with self.subTest(members=members), self.assertRaisesRegex(ValueError, "(?i)(pair|configuration|checkpoint|scaler)"):
                baseline_model.model_artifacts(self.root, self.archive, {**self.selection, "members": members}, "H")

    def test_modified_selected_checkpoint_is_rejected_before_loading(self):
        (self.root / self.members[0]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checkpoint_last.pt.*(size|sha256)"):
            baseline_model.model_artifacts(self.root, self.archive, self.selection, "H")

    def test_declared_aggregate_bound_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "max_bytes"):
            baseline_model.model_artifacts(self.root, self.archive, {**self.selection, "max_bytes": 1}, "H")

    def test_argv_uses_receipt_bounds_cpu_and_exact_pair(self):
        paths, _ = baseline_model.model_artifacts(self.root, self.archive, self.selection, "C")
        argv = baseline_model.inference_argv(self.root / "prepared", "C", paths,
                    {"max_atoms": 23, "max_tokens": 25}, self.root / "result", Path("/opt/nmrnet/uninmr"))
        self.assertEqual(argv[0], str(self.root / "prepared" / "C"))
        self.assertEqual(argv[argv.index("--max-atoms") + 1], "23")
        self.assertEqual(argv[argv.index("--max-seq-len") + 1], "25")
        self.assertEqual(argv[argv.index("--saved-dir") + 1], str(paths["scaler"].parent))
        self.assertIn("--cpu", argv)
        self.assertNotIn("--fp16", argv)
        self.assertNotIn("--remove-hydrogen", argv)
        self.assertNotIn("--conformer-augmentation", argv)

    def test_resolved_contract_rejects_semantic_or_precision_drift(self):
        values = dict(baseline_model.CONTRACT, selected_atom="H", max_atoms=23, max_seq_len=25)
        for changes in ({"fp16": True}, {"remove_hydrogen": True}, {"arch": "unimat"},
                        {"selected_atom": "C"}, {"max_atoms": 22}, {"unlabeled_data": "/other"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, next(iter(changes))):
                baseline_model.validate_args(SimpleNamespace(**{**values, **changes}), "H",
                                             {"max_atoms": 23, "max_tokens": 25})
        baseline_model.validate_args(SimpleNamespace(**values), "H", {"max_atoms": 23, "max_tokens": 25})

    def test_dictionary_reordering_fails_even_at_same_size(self):
        expected = {"symbols": ["C", "H", "[MASK]"],
                    "task_special_token": {"symbol": "[MASK]", "index": 2}}
        task = SimpleNamespace(dictionary=SimpleNamespace(symbols=["H", "C", "[MASK]"]), mask_idx=2)
        with self.assertRaisesRegex(ValueError, "dictionary"):
            baseline_model.validate_dictionary(task, expected)
        task.dictionary.symbols = expected["symbols"]
        baseline_model.validate_dictionary(task, expected)
        task.mask_idx = 1
        with self.assertRaisesRegex(ValueError, "dictionary"):
            baseline_model.validate_dictionary(task, expected)

    def test_missing_unexpected_or_unavailable_load_report_fails(self):
        for result in (SimpleNamespace(missing_keys=["encoder.weight"], unexpected_keys=[]),
                       SimpleNamespace(missing_keys=[], unexpected_keys=["old_head.weight"]), None):
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, "checkpoint"):
                baseline_model.validate_load(result)
        self.assertEqual(baseline_model.validate_load(SimpleNamespace(missing_keys=[], unexpected_keys=[])),
                         {"missing_keys": [], "unexpected_keys": []})

    def test_model_device_and_floating_dtype_are_observed_not_converted(self):
        value = SimpleNamespace(device=SimpleNamespace(type="cpu"), dtype="float32",
                                is_floating_point=lambda: True, numel=lambda: 7)
        model = SimpleNamespace(named_parameters=lambda: [("encoder.weight", value)],
                                named_buffers=lambda: [])
        self.assertEqual(baseline_model.validate_model(model, "float32"), 7)
        for field, changed in (("dtype", "float16"), ("device", SimpleNamespace(type="cuda"))):
            original = getattr(value, field)
            setattr(value, field, changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "encoder.weight"):
                baseline_model.validate_model(model, "float32")
            setattr(value, field, original)


if __name__ == "__main__":
    unittest.main()
