"""Exercise saved-output reading without loading the inference dependencies."""

import hashlib
import io
import json
import pickle
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import baseline_output


class ArrayValue:
    def __init__(self, value):
        self.value = value

    def tolist(self):
        return self.value


class TensorValue(ArrayValue):
    def detach(self):
        return self

    def cpu(self):
        return self


def identity(raw):
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class BaselineOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.split = self.root / "H"
        self.split.mkdir()
        self.raw = pickle.dumps({"atoms": ["C", "H"], "atoms_target_mask": [1, 1], "atoms_target": [40.0, 2.0]})
        self.key = b"original-key"
        self.symbols = ["<pad>", "<bos>", "<eos>", "<unk>", "C", "H"]
        self.receipt = {
            "dictionary": {
                "symbols_before_task": self.symbols,
                "symbols": [*self.symbols, "[MASK]"],
                "task_special_token": {"symbol": "[MASK]", "index": 6},
            },
            "elements": {
                "H": {
                    "records": [
                        {
                            "dataset_index": 0,
                            "key_hex": self.key.hex(),
                            "raw_sha256": identity(self.raw)["sha256"],
                            "atom_count": 2,
                            "selected_atom_indices": [1],
                            "selected_labels": [2.0],
                        }
                    ],
                    "record_count": 1,
                    "selected_atom_count": 1,
                    "files": {},
                }
            },
        }
        for name, raw in (
            ("valid.lmdb", b"mock LMDB"),
            ("valid_dbid.pkl", pickle.dumps([self.key])),
            ("dict.txt", b"C 1\nH 1\n"),
        ):
            self.write_prepared(name, raw)
        self.write_receipt()
        self.batch = {
            "matid": TensorValue([0]),
            "num_task": 1,
            "sample_size": TensorValue(1),
            "src_token": TensorValue([[1], [4], [5], [2]]),
            "select_atom": TensorValue([[0], [0], [1], [0]]),
            "predict": ArrayValue([[2.25]]),
            "target": ArrayValue([[2.0]]),
            "loss": object(),
        }
        self.saved = self.root / "checkpoint_valid.out.pkl"
        self.saved.write_bytes(pickle.dumps([self.batch]))

        self.dictionary = mock.Mock()
        self.dictionary.symbols = list(self.symbols)
        self.dictionary.add_symbol.side_effect = self.add_mask
        self.dictionary.bos.return_value = 1
        self.dictionary.eos.return_value = 2
        self.dictionary.pad.return_value = 0
        self.dictionary_class = mock.Mock()
        self.dictionary_class.load.return_value = self.dictionary
        self.transaction = mock.MagicMock()
        self.transaction.__enter__.return_value = self.transaction
        self.transaction.stat.return_value = {"entries": 1}
        self.transaction.get.return_value = self.raw
        self.environment = mock.MagicMock()
        self.environment.__enter__.return_value = self.environment
        self.environment.begin.return_value = self.transaction
        self.lmdb = mock.Mock()
        self.lmdb.open.return_value = self.environment
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(
            "sys.modules",
            {
                "lmdb": self.lmdb,
                "unicore.data": SimpleNamespace(Dictionary=self.dictionary_class),
            },
        ).start()

    # inspect_output supplies is_special by keyword. This double models symbol
    # order/index; separate bos/eos/pad mocks provide boundary-token IDs.
    def add_mask(
        self,
        symbol,
        *,
        is_special,  # noqa: ARG002
    ):
        self.dictionary.symbols.append(symbol)
        return len(self.dictionary.symbols) - 1

    def write_prepared(self, name, raw):
        (self.split / name).write_bytes(raw)
        self.receipt["elements"]["H"]["files"][name] = identity(raw)

    def write_receipt(self):
        (self.root / "provenance.json").write_text(json.dumps(self.receipt))

    def inspect(self):
        return baseline_output.inspect_output(self.root, "H", self.saved, label_atol=0.0)

    def test_normalizes_only_verifier_fields_preserving_scalar_and_column_shapes(self):
        batch = baseline_output.normalize_batches([self.batch])[0]
        self.assertEqual([[1], [4], [5], [2]], batch["src_token"])
        self.assertEqual(1, batch["sample_size"])
        self.assertEqual([[2.25]], batch["predict"])
        self.assertNotIn("loss", batch)

    def test_report_binds_original_output_receipt_and_source_atom(self):
        report = self.inspect()
        self.assertEqual(report["upstream_output"]["sha256"], identity(self.saved.read_bytes())["sha256"])
        self.assertEqual(report["prepared_receipt"]["sha256"], identity((self.root / "provenance.json").read_bytes())["sha256"])
        self.assertEqual("H", report["element"])
        self.assertEqual(1, report["predictions"][0]["atom_index"])
        self.assertEqual(report["predictions"][0]["key_hex"], self.key.hex())
        self.assertEqual(2.25, report["predictions"][0]["prediction"])
        self.assertEqual(0.0, report["max_label_abs_difference"])
        json.dumps(report, allow_nan=False)
        self.assertTrue(self.lmdb.open.call_args.kwargs["readonly"])
        self.assertFalse(self.lmdb.open.call_args.kwargs["lock"])

    def test_every_prepared_file_is_verified_before_any_unpickle_or_dependency_read(self):
        for name in ("valid.lmdb", "valid_dbid.pkl", "dict.txt"):
            with self.subTest(name=name):
                path = self.split / name
                original = path.read_bytes()
                path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                with (
                    mock.patch.object(baseline_output.pickle, "loads") as loads,
                    mock.patch.object(baseline_output.pickle, "load") as load,
                ):
                    with self.assertRaisesRegex(ValueError, name):
                        self.inspect()
                    loads.assert_not_called()
                    load.assert_not_called()
                self.lmdb.open.assert_not_called()
                self.dictionary_class.load.assert_not_called()
                path.write_bytes(original)

    def test_companion_order_and_lmdb_completeness_are_verified(self):
        self.write_prepared("valid_dbid.pkl", pickle.dumps([b"other-key"]))
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, "order|keys"):
            self.inspect()
        self.write_prepared("valid_dbid.pkl", pickle.dumps([self.key]))
        self.write_receipt()
        self.transaction.stat.return_value = {"entries": 2}
        with self.assertRaisesRegex(ValueError, "count|entries"):
            self.inspect()

    def test_missing_record_is_rejected_before_result_validation(self):
        self.transaction.get.return_value = None
        with self.assertRaisesRegex(ValueError, "missing.*record|record.*missing"):
            self.inspect()

    def test_same_size_dictionary_with_wrong_order_is_rejected(self):
        self.dictionary.symbols[4:6] = ["H", "C"]
        with self.assertRaisesRegex(ValueError, "dictionary"):
            self.inspect()

    def test_task_added_dictionary_mapping_is_verified(self):
        self.receipt["dictionary"]["task_special_token"]["index"] = 5
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, "dictionary"):
            self.inspect()

    def test_malformed_outputs_are_rejected(self):
        for batches in ({}, [dict(self.batch, matid=[3])], []):
            with self.subTest(batches=batches):
                self.saved.write_bytes(pickle.dumps(batches))
                self.dictionary.symbols = list(self.symbols)
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_unknown_element_fails_before_opening_paths(self):
        with self.assertRaises(ValueError):
            baseline_output.inspect_output(self.root, "../H", self.saved, label_atol=0.0)

    def test_cli_requires_explicit_label_allowance_and_prints_report(self):
        arguments = ["baseline_output", "--prepared", str(self.root), "--element", "H", "--upstream-output", str(self.saved)]
        with mock.patch("sys.argv", arguments), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                baseline_output.main()
            self.assertEqual(2, error.exception.code)
        with (
            mock.patch("sys.argv", [*arguments, "--label-atol", "0"]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            baseline_output.main()
        self.assertEqual(2.25, json.loads(output.getvalue())["predictions"][0]["prediction"])


if __name__ == "__main__":
    unittest.main()
