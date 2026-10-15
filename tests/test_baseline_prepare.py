"""Small reference selections retain released record bytes and atom identity."""

import hashlib
import io
import json
import math
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import baseline_prepare


class ExecutionIdentityTests(unittest.TestCase):
    def test_identity_tracks_actual_tool_bytes_independently_of_revision_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "baseline_prepare.py"
            source.write_bytes(b"original bytes")
            options = {"image_id": "sha256:" + "a" * 64, "repository_revision": "b" * 40, "tool_sources": [source]}
            original = baseline_prepare.execution_identity(**options)
            source.write_bytes(b"edited bytes")
            edited = baseline_prepare.execution_identity(**options)
            self.assertEqual(original["image_id"], edited["image_id"])
            self.assertEqual(original["repository_revision"], edited["repository_revision"])
            self.assertEqual(
                edited["tool_sources"],
                {"tools/baseline_prepare.py": {"size": 12, "sha256": hashlib.sha256(b"edited bytes").hexdigest()}},
            )
            self.assertNotEqual(original["tool_sources"], edited["tool_sources"])

    def test_published_preparation_retains_execution_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dictionary_path = root / "dict.txt"
            dictionary_path.write_bytes(b"C 1\nH 1\n")
            lmdb = mock.MagicMock()
            cursor = lmdb.open.return_value.__enter__.return_value.begin.return_value.__enter__.return_value.cursor
            cursor.return_value.__enter__.return_value = [(b"k", pickle.dumps(record()))]
            data = mock.Mock()
            dictionary = data.Dictionary.load.return_value
            dictionary.symbols = ["C", "H", "O"]
            dictionary.add_symbol.return_value = 3
            options = {
                "image_id": "sha256:" + "a" * 64,
                "repository_revision": "b" * 40,
                "tool_sources": [Path("tools/baseline_prepare.py")],
            }
            with (
                mock.patch.dict("sys.modules", {"lmdb": lmdb, "unicore.data": data}),
                mock.patch.object(
                    baseline_prepare.artifacts,
                    "load_declaration",
                    return_value={"files": [{"name": "data.zip"}, {"name": "weight.zip"}], "record_id": 1, "doi": "fixture"},
                ),
                mock.patch.object(baseline_prepare, "verified_member", return_value=(dictionary_path, {})),
                mock.patch.object(baseline_prepare, "write_split", return_value={}),
            ):
                for name in ("data", "weight"):
                    (root / f"{name}.selection.json").write_text("{}")
                receipt = baseline_prepare.prepare(
                    root,
                    root / "release.json",
                    root / "data.selection.json",
                    root / "weight.selection.json",
                    root / "result",
                    count=1,
                    max_atoms=128,
                    max_tokens=130,
                    **options,
                )
            published = json.loads((root / "result/provenance.json").read_text())
            self.assertEqual(published["execution"], baseline_prepare.execution_identity(**options))
            self.assertEqual(receipt, published)


def record(**changes):
    value = {
        "atoms": ["C", "H", "H", "O"],
        "coordinates": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        "atoms_target": [42.0, 1.25, 2.5, 0.0],
        "atoms_target_mask": [1, 1, 0, 0],
        "nmr_solvent": "CDCl3",
        "smiles": "fixture",
    }
    value.update(changes)
    return value


# silent-defaults: allow-contract -- Synthetic records use pickle protocol 4 unless a case explicitly supplies another protocol; selected record bytes must remain unchanged; proof=tests/test_baseline_prepare.py::SelectionTests.test_requested_count_keeps_first_eligible_records_and_receipts
def packed(key, value=None, *, protocol=4):
    return key, pickle.dumps(record() if value is None else value, protocol=protocol)


class SelectionTests(unittest.TestCase):
    def select(self, records, **changes):
        options = {"element": "H", "dictionary_tokens": ["C", "H", "O"], "count": 1, "max_atoms": 4, "max_tokens": 6}
        options.update(changes)
        return baseline_prepare.select_records(records, **options)

    def test_raw_pickles_and_chosen_order_survive_without_reserialization(self):
        original = [packed(b"z", protocol=2), packed(b"\x00\xff", protocol=5)]
        selected, receipt = self.select(iter(original), count=2)
        self.assertEqual(selected, original)
        self.assertEqual(["7a", "00ff"], [item["key_hex"] for item in receipt])
        self.assertEqual([0, 1], [item["dataset_index"] for item in receipt])
        self.assertEqual([item["raw_sha256"] for item in receipt], [hashlib.sha256(raw).hexdigest() for _, raw in original])

    def test_hydrogen_and_carbon_receipts_follow_element_and_label_mask(self):
        for element, indices, labels in (("H", [1], [1.25]), ("C", [0], [42.0])):
            with self.subTest(element=element):
                _, receipt = self.select([packed(b"molecule")], element=element)
                self.assertEqual(4, receipt[0]["atom_count"])
                self.assertEqual(receipt[0]["selected_atom_indices"], indices)
                self.assertEqual(receipt[0]["selected_labels"], labels)

    def test_requested_count_keeps_first_eligible_records_and_receipts(self):
        original = [packed(key) for key in (b"third", b"first", b"second")]
        selected, receipt = self.select(original, count=2)
        self.assertEqual(selected, original[:2])
        self.assertEqual([item["key_hex"] for item in receipt], [key.hex() for key, _ in original[:2]])
        self.assertEqual([0, 1], [item["dataset_index"] for item in receipt])

    def test_multiple_selected_hydrogens_preserve_atom_and_label_order(self):
        value = record(atoms_target_mask=[1, 1, 1, 0], atoms_target=[42.0, 2.5, 1.25, 0.0])
        _, receipt = self.select([packed(b"multiple-H", value)])
        self.assertEqual([1, 2], receipt[0]["selected_atom_indices"])
        self.assertEqual([2.5, 1.25], receipt[0]["selected_labels"])

    def test_unsuitable_candidates_are_skipped_without_cropping_or_filling(self):
        oversized = record(
            atoms=["C", "H", "H", "O", "H"],
            coordinates=[[0.0, 0.0, 0.0]] * 5,
            atoms_target=[42.0, 1.25, 2.5, 0.0, 3.0],
            atoms_target_mask=[1, 1, 0, 0, 1],
        )
        candidates = [
            packed(b"too-large", oversized),
            packed(b"unlabelled-H", record(atoms_target_mask=[1, 0, 0, 0])),
            packed(b"bad-label", record(atoms_target=[42.0, math.nan, 2.5, 0.0])),
            packed(b"bad-coordinate", record(coordinates=[[math.inf, 0.0, 0.0]] * 4)),
            packed(b"usable"),
        ]
        selected, receipt = self.select(candidates)
        self.assertEqual(selected, candidates[-1:])
        self.assertEqual(0, receipt[0]["dataset_index"])

    def test_token_limit_reserves_both_sequence_boundary_tokens(self):
        with self.assertRaisesRegex(ValueError, "(?i)select.*H.*1.*0"):
            self.select([packed(b"four-atoms")], max_tokens=5)
        selected, _ = self.select([packed(b"four-atoms")], max_tokens=6)
        self.assertEqual(1, len(selected))

    def test_unknown_atom_cannot_be_silently_tokenized_or_skipped(self):
        unknown = record(atoms=["C", "H", "H", "Xe"])
        with self.assertRaisesRegex(ValueError, "(?i)dictionary.*Xe|Xe.*dictionary"):
            self.select([packed(b"unknown", unknown), packed(b"usable")])

    def test_misaligned_record_is_rejected(self):
        for field, value in (("coordinates", [[0.0, 0.0, 0.0]]), ("atoms_target", [42.0]), ("atoms_target_mask", [1])):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.select([packed(b"misaligned", record(**{field: value}))])

    def test_coordinate_shape_and_nonbinary_mask_are_rejected(self):
        for field, value in (("coordinates", [[0.0, 0.0]] * 4), ("atoms_target_mask", [1, 2, 0, 0])):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.select([packed(b"malformed", record(**{field: value}))])

    def test_missing_count_is_not_reported_as_complete(self):
        with self.assertRaisesRegex(ValueError, "(?i)select.*H.*2.*1"):
            self.select([packed(b"one")], count=2)

    def test_duplicate_key_cannot_be_counted_as_two_records(self):
        with self.assertRaisesRegex(ValueError, "(?i)duplicate.*key"):
            self.select([packed(b"same"), packed(b"same")], count=2)

    def test_solvent_receipt_matches_upstream_categories(self):
        cases = [("CDCl3", 1), ("DMSO-d6", 2), ("water", 3), (None, 3)]
        for raw_solvent, category in cases:
            with self.subTest(solvent=raw_solvent):
                _, receipt = self.select([packed(b"solvent", record(nmr_solvent=raw_solvent))])
                self.assertTrue(receipt[0]["solvent_present"])
                self.assertEqual(receipt[0]["raw_solvent"], raw_solvent)
                self.assertEqual(receipt[0]["effective_solvent_category"], category)
        missing = record()
        del missing["nmr_solvent"]
        _, receipt = self.select([packed(b"missing", missing)])
        self.assertFalse(receipt[0]["solvent_present"])
        self.assertIsNone(receipt[0]["raw_solvent"])
        self.assertEqual(0, receipt[0]["effective_solvent_category"])

    def test_invalid_selection_limits_and_element_are_rejected(self):
        for options in ({"count": 0}, {"max_atoms": 0}, {"max_tokens": 2}, {"element": "N"}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, next(iter(options))):
                self.select([packed(b"record")], **options)


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.member = self.root / "data" / "valid.lmdb"
        self.member.parent.mkdir()
        self.member.write_bytes(b"verified extracted fixture")
        self.archive = {"name": "data.zip", "size": 100, "md5": "0" * 32}
        self.selection = {"members": ["data/valid.lmdb"], "max_bytes": 100}
        self.provenance = {
            "archive": self.archive,
            "archive_sha256": "1" * 64,
            "members": {
                "data/valid.lmdb": {
                    "size": self.member.stat().st_size,
                    "sha256": hashlib.sha256(self.member.read_bytes()).hexdigest(),
                }
            },
        }
        self.write_provenance()

    def write_provenance(self):
        (self.root / "provenance.json").write_text(json.dumps(self.provenance))

    def test_source_is_bound_to_declared_archive_member_and_extracted_hash(self):
        path, source = baseline_prepare.verified_member(self.root, self.archive, self.selection)
        self.assertEqual(path, self.member)
        self.assertEqual(source["archive"], self.archive)
        self.assertEqual(source["archive_sha256"], "1" * 64)
        self.assertEqual("data/valid.lmdb", source["member"])
        self.assertEqual(source["sha256"], hashlib.sha256(self.member.read_bytes()).hexdigest())

    def test_modified_or_truncated_extraction_is_rejected(self):
        original = self.member.read_bytes()
        for replacement in (original[:-1], b"X" + original[1:]):
            with self.subTest(replacement=replacement):
                self.member.write_bytes(replacement)
                with self.assertRaisesRegex(ValueError, "(?i)valid.lmdb.*(size|sha256)"):
                    baseline_prepare.verified_member(self.root, self.archive, self.selection)

    def test_provenance_from_another_archive_is_rejected(self):
        self.provenance["archive"] = dict(self.archive, md5="2" * 32)
        self.write_provenance()
        with self.assertRaisesRegex(ValueError, "(?i)archive"):
            baseline_prepare.verified_member(self.root, self.archive, self.selection)

    def test_unreceipted_member_is_rejected(self):
        self.provenance["members"] = {}
        self.write_provenance()
        with self.assertRaisesRegex(ValueError, "(?i)valid.lmdb.*provenance"):
            baseline_prepare.verified_member(self.root, self.archive, self.selection)

    def test_ambiguous_unsafe_or_over_limit_selection_is_rejected(self):
        for selection in (
            {"members": [], "max_bytes": 100},
            {"members": ["a", "b"], "max_bytes": 100},
            {"members": ["../escape"], "max_bytes": 100},
            dict(self.selection, max_bytes=1),
        ):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                baseline_prepare.verified_member(self.root, self.archive, selection)

    def test_existing_prepared_output_is_never_replaced(self):
        output = self.root / "baseline"
        output.mkdir()
        sentinel = output / "keep"
        sentinel.write_bytes(b"prior baseline")
        with self.assertRaises(FileExistsError):
            baseline_prepare.prepare(
                self.root,
                self.root / "missing-release.json",
                self.root / "data.selection.json",
                self.root / "weight.selection.json",
                output,
                image_id="sha256:" + "a" * 64,
                repository_revision="b" * 40,
                tool_sources=[Path("tools/baseline_prepare.py")],
                count=2,
                max_atoms=128,
                max_tokens=130,
            )
        self.assertEqual(b"prior baseline", sentinel.read_bytes())


class PreparationArgumentTests(unittest.TestCase):
    def test_missing_paths_or_bounds_reject_before_preparation(self):
        arguments = {
            "--prepared-root": "/prepared",
            "--declaration": "/input/release.json",
            "--data-selection": "/input/data.selection.json",
            "--dictionary-selection": "/input/weight.selection.json",
            "--output": "/baseline/prepared",
            "--image-id": "sha256:" + "a" * 64,
            "--repository-revision": "b" * 40,
            "--tool-source": "/workspace/tools/baseline_prepare.py",
            "--count": "2",
            "--max-atoms": "128",
            "--max-tokens": "130",
        }
        for omitted in (
            "--prepared-root",
            "--declaration",
            "--data-selection",
            "--dictionary-selection",
            "--count",
            "--max-atoms",
            "--max-tokens",
        ):
            argv = [
                "baseline_prepare",
                *(argument for name, value in arguments.items() if name != omitted for argument in (name, value)),
            ]
            diagnostic = io.StringIO()
            with (
                self.subTest(omitted=omitted),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(sys, "stderr", diagnostic),
                mock.patch.object(baseline_prepare, "prepare") as prepare,
                self.assertRaises(SystemExit) as failure,
            ):
                baseline_prepare.main()
            self.assertEqual(2, failure.exception.code)
            self.assertIn(f"the following arguments are required: {omitted}", diagnostic.getvalue())
            prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
