"""Exercise the authored preparation corpus through the existing pure XYZ owner.

Native reader and embedding expectations live beside the data until their
isolated worker exists; these tests do not qualify RDKit or batch publication.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import unittest
from pathlib import Path

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.xyz import prepare_pasted_xyz, prepare_xyz

FIXTURES = Path(__file__).parent / "fixtures" / "preparation"


class PreparationFixtureNormativeTests(unittest.TestCase):
    def test_supplied_conformers_keep_distinct_coordinates_without_adding_hydrogens(self):
        source = (FIXTURES / "normative" / "butane_conformers.xyz").read_bytes()
        expected = json.loads((FIXTURES / "expected.json").read_text())
        frames = prepare_xyz(source, source_unit="angstrom", selection="all")
        self.assertEqual((1, 2), tuple(frame.source_record_number for frame in frames))
        for frame, name in zip(frames, ("butane_supplied_a", "butane_supplied_b"), strict=True):
            with self.subTest(record=frame.source_record_number):
                request = json.loads(frame.request_bytes)
                self.assertEqual(expected["geometries"][name]["atoms"], request["atoms"])
                self.assertEqual(expected["geometries"][name]["coordinates_angstrom"], request["coordinates"])
        self.assertEqual((3, 4, 5, 6), frames[0].source_positions)
        self.assertEqual((9, 10, 11, 12), frames[1].source_positions)
        self.assertNotEqual(frames[0].request_bytes, frames[1].request_bytes)

    def test_file_order_repeated_records_and_different_molecules_are_preserved(self):
        source = (FIXTURES / "normative" / "mixed_records.xyz").read_bytes()
        expected = json.loads((FIXTURES / "expected.json").read_text())
        frames = prepare_xyz(source, source_unit="angstrom", selection="all")
        self.assertEqual([1, 2, 3], [frame.source_record_number for frame in frames])
        for frame, name in zip(frames, ("chloromethane", "water", "chloromethane"), strict=True):
            with self.subTest(record=frame.source_record_number):
                wanted = expected["geometries"][name]
                request = json.loads(frame.request_bytes)
                self.assertEqual(wanted["atoms"], request["atoms"])
                self.assertEqual(wanted["coordinates_angstrom"], request["coordinates"])
                self.assertEqual(hashlib.sha256(source).hexdigest(), frame.source_sha256)
        self.assertEqual((3, 4, 5, 6, 7), frames[0].source_positions)
        self.assertEqual((10, 11, 12), frames[1].source_positions)
        self.assertEqual((15, 16, 17, 18, 19), frames[2].source_positions)

    def test_reordered_selection_keeps_original_record_and_atom_locations(self):
        source = (FIXTURES / "normative" / "mixed_records.xyz").read_bytes()
        frames = prepare_xyz(source, source_unit="angstrom", selection=(3, 1))
        self.assertEqual((3, 1), tuple(frame.source_record_number for frame in frames))
        self.assertEqual((15, 16, 17, 18, 19), frames[0].source_positions)
        self.assertEqual((3, 4, 5, 6, 7), frames[1].source_positions)
        self.assertEqual(frames[0].request_bytes, frames[1].request_bytes)

    def test_explicit_nm_and_bohr_factors_match_independent_expected_values(self):
        for filename, unit, distance in (("scale_nm.xyz", "nm", 1.25), ("scale_bohr.xyz", "bohr", 1.058354421088)):
            with self.subTest(unit=unit):
                source = (FIXTURES / "normative" / filename).read_bytes()
                prepared = prepare_pasted_xyz(source, source_unit=unit)
                request = json.loads(prepared.request_bytes)
                self.assertEqual(["C", "H"], request["atoms"])
                self.assertEqual("angstrom", request["coordinate_unit"])
                self.assertEqual([0, 0, 0], request["coordinates"][0])
                self.assertEqual([0, 0], request["coordinates"][1][1:])
                self.assertAlmostEqual(distance, request["coordinates"][1][0], places=12)
                self.assertEqual((3, 4), prepared.source_positions)
                self.assertEqual(unit, prepared.source_unit)

    def test_good_selection_does_not_interpret_unselected_malformed_coordinates(self):
        source = (FIXTURES / "adversarial" / "invalid_last.xyz").read_bytes()
        frames = prepare_xyz(source, source_unit="angstrom", selection=(3, 1))
        self.assertEqual((3, 1), tuple(frame.source_record_number for frame in frames))
        self.assertEqual(frames[0].request_bytes, frames[1].request_bytes)


class PreparationFixtureAdversarialTests(unittest.TestCase):
    def test_selected_invalid_later_record_rejects_the_whole_preparation(self):
        source = (FIXTURES / "adversarial" / "invalid_last.xyz").read_bytes()
        for selection in ("all", (1, 4)):
            with self.subTest(selection=selection), self.assertRaisesRegex(InputRejected, "record 4") as failure:
                prepare_xyz(source, source_unit="angstrom", selection=selection)
            self.assertNotIn("PRIVATE_COORDINATE_CANARY", str(failure.exception))

    def test_pasted_xyz_rejects_multiple_frames(self):
        source = (FIXTURES / "normative" / "mixed_records.xyz").read_bytes()
        with self.assertRaisesRegex(InputRejected, "one XYZ frame"):
            prepare_pasted_xyz(source, source_unit="angstrom")

    def test_extended_xyz_requires_a_separate_supported_reader(self):
        source = (FIXTURES / "adversarial" / "extended.xyz").read_bytes()
        with self.assertRaisesRegex(InputRejected, "extended XYZ"):
            prepare_xyz(source, source_unit="angstrom", selection="all")

    def test_geometry_magnitude_cannot_supply_missing_or_unknown_units(self):
        source = (FIXTURES / "normative" / "scale_nm.xyz").read_bytes()
        for unit in ("", "unspecified"):
            with self.subTest(unit=unit), self.assertRaisesRegex(InputRejected, "source unit"):
                prepare_pasted_xyz(source, source_unit=unit)
