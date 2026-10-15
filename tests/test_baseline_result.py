"""Check source alignment before interpreting retained inference predictions."""

import copy
import hashlib
import pickle
import unittest

from tools import baseline_result


class BaselineResultTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.prepared = []
        for index, (atoms, mask, labels) in enumerate(
            (
                (["H", "C", "H"], [1, 1, 1], [1.0, 40.0, 2.0]),
                (["C", "H"], [1, 1], [50.0, 3.0]),
            )
        ):
            key = f"source-{index}".encode()
            raw = pickle.dumps({"atoms": atoms, "atoms_target_mask": mask, "atoms_target": labels})
            indices = [i for i, atom in enumerate(atoms) if atom == "H" and mask[i]]
            self.prepared.append((key, raw))
            self.records.append(
                {
                    "dataset_index": index,
                    "key_hex": key.hex(),
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "atom_count": len(atoms),
                    "selected_atom_indices": indices,
                    "selected_labels": [labels[i] for i in indices],
                }
            )
        # Each token/mask column contains two padded rows of length eight.
        self.batch = {
            "matid": [0, 1],
            "num_task": 1,
            "sample_size": 3,
            "src_token": [[i] for i in (1, 4, 5, 4, 2, 0, 0, 0, 1, 5, 4, 2, 0, 0, 0, 0)],
            "select_atom": [[i] for i in (0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0)],
            "target": [[1.0], [2.0], [3.0]],
            "predict": [[1.25], [2.5], [3.75]],
        }

    # silent-defaults: allow-contract -- Fixture labels require exact agreement unless a case explicitly grants a roundtrip allowance; proof=tests/test_baseline_result.py::BaselineResultTests.test_label_roundtrip_allowance_is_explicit_and_error_is_reported
    def validate(self, batches=None, *, label_atol=0.0):
        # silent-defaults: allow-contract -- Omitted batches select the declared single fixture batch; explicit batches, including an empty list, reach validation unchanged; proof=tests/test_baseline_result.py::BaselineResultTests.test_reordered_batches_are_aligned_by_record_id
        return baseline_result.validate_batches(
            [self.batch] if batches is None else batches,
            self.records,
            self.prepared,
            element="H",
            dictionary_symbols=["<pad>", "<bos>", "<eos>", "<unk>", "H", "C"],
            bos_index=1,
            eos_index=2,
            pad_index=0,
            label_atol=label_atol,
        )

    def test_binds_selected_predictions_to_original_indices_without_rescaling(self):
        result = self.validate()["predictions"]
        self.assertEqual(
            [(self.records[0]["key_hex"], 0, 1.25), (self.records[0]["key_hex"], 2, 2.5), (self.records[1]["key_hex"], 1, 3.75)],
            [(r["key_hex"], r["atom_index"], r["prediction"]) for r in result],
        )

    def test_reordered_batches_are_aligned_by_record_id(self):
        batches = []
        for index, selection in ((1, slice(2, 3)), (0, slice(0, 2))):
            batch = copy.deepcopy(self.batch)
            batch.update(matid=[index], sample_size=selection.stop - selection.start)
            for field in ("src_token", "select_atom"):
                batch[field] = batch[field][index * 8 : (index + 1) * 8]
            for field in ("predict", "target"):
                batch[field] = batch[field][selection]
            batches.append(batch)
        self.assertEqual(self.validate(batches), self.validate())

    def test_missing_duplicate_and_unknown_records_fail(self):
        with self.assertRaisesRegex(ValueError, "missing.*0.*1"):
            self.validate([])
        with self.assertRaisesRegex(ValueError, "duplicate.*0"):
            self.validate([self.batch, self.batch])
        for matid in ([0, 0], [0, 2], [0, -1], [0, True], [0, 1.0]):
            with self.subTest(matid=matid):
                batch = dict(self.batch, matid=matid)
                with self.assertRaises(ValueError):
                    self.validate([batch])

    def test_equal_total_count_cannot_hide_wrong_record_mask(self):
        self.batch["select_atom"][1] = [0]
        self.batch["select_atom"][9] = [1]
        with self.assertRaisesRegex(ValueError, "source-0|736f757263652d30|record 0"):
            self.validate()

    def test_wrong_token_order_boundaries_padding_or_labels_fail(self):
        for field, index, value in (
            ("src_token", 2, [4]),
            ("src_token", 0, [2]),
            ("src_token", 7, [5]),
            ("select_atom", 0, [1]),
            ("target", 0, [2.0]),
            ("target", 2, [float("nan")]),
        ):
            with self.subTest(field=field, index=index):
                batch = copy.deepcopy(self.batch)
                batch[field][index] = value
                with self.assertRaises(ValueError):
                    self.validate([batch])

    def test_bad_shapes_counts_and_nonfinite_predictions_fail(self):
        for field, value in (
            ("predict", [[1.0], [2.0]]),
            ("predict", [[1.0, 2.0]]),
            ("predict", [[1.0], [2.0], [float("inf")]]),
            ("target", [[1.0], [2.0]]),
            ("num_task", 2),
            ("sample_size", 4),
            ("src_token", [[1]]),
            ("select_atom", [[0]]),
            ("matid", []),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate([dict(self.batch, **{field: value})])

    def test_changed_prepared_bytes_or_receipt_mapping_fail(self):
        for field, value in (
            ("raw_sha256", "wrong"),
            ("dataset_index", 1),
            ("selected_atom_indices", [2, 0]),
            ("selected_labels", [2.0, 1.0]),
            ("key_hex", "00"),
            ("atom_count", 2),
        ):
            with self.subTest(field=field):
                original = self.records[0][field]
                self.records[0][field] = value
                with self.assertRaises(ValueError):
                    self.validate()
                self.records[0][field] = original

    def test_comparison_reports_maximum_and_checks_each_prediction(self):
        reference = self.validate()["predictions"]
        repeated = copy.deepcopy(reference)
        repeated[0]["prediction"] += 0.125
        repeated[1]["prediction"] -= 0.125
        summary = baseline_result.compare_runs(reference, repeated, atol=0.125, rtol=0.0)
        self.assertEqual({"prediction_count": 3, "max_abs_difference": 0.125, "atol": 0.125, "rtol": 0.0}, summary)
        with self.assertRaisesRegex(ValueError, "atom 0"):
            baseline_result.compare_runs(reference, repeated, atol=0.1, rtol=0.0)

    def test_relative_tolerance_is_explicit_and_uses_reference_magnitude(self):
        reference = self.validate()["predictions"]
        repeated = copy.deepcopy(reference)
        repeated[0]["prediction"] = 1.375
        self.assertEqual(0.125, baseline_result.compare_runs(reference, repeated, atol=0.0, rtol=0.1)["max_abs_difference"])

    def test_comparison_rejects_different_alignment_nonfinite_and_missing_data(self):
        reference = self.validate()["predictions"]
        for field, value in (("atom_index", 1), ("key_hex", "different"), ("prediction", float("nan")), ("target", 9.0)):
            with self.subTest(field=field):
                repeated = copy.deepcopy(reference)
                repeated[0][field] = value
                with self.assertRaises(ValueError):
                    baseline_result.compare_runs(reference, repeated, atol=0.0, rtol=0.0)
        for repeated in ([], reference[:-1]):
            with self.assertRaises(ValueError):
                baseline_result.compare_runs(reference, repeated, atol=0.0, rtol=0.0)
        for atol in (-1.0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                baseline_result.compare_runs(reference, reference, atol=atol, rtol=0.0)

    def test_label_roundtrip_allowance_is_explicit_and_error_is_reported(self):
        self.batch["target"][0] = [1.0625]
        with self.assertRaises(ValueError):
            self.validate()
        result = self.validate(label_atol=0.0625)
        self.assertEqual(0.0625, result["max_label_abs_difference"])
        self.assertEqual(0.0625, result["label_atol"])
        self.assertEqual(1.0, result["predictions"][0]["target"])
        for allowance in (-1.0, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                self.validate(label_atol=allowance)


if __name__ == "__main__":
    unittest.main()
