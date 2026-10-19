"""MOL2 extraction preserves supplied nuclei and coordinates, without graph claims."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import unittest
from pathlib import Path

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.mol2 import prepare_mol2

FIXTURES = Path(__file__).parent / "fixtures/preparation"


def molecule(rows, *, atom_count=None, metadata=b""):
    """Author independent source rows; graph metadata is outside this reader's input."""
    count = len(rows) if atom_count is None else atom_count
    return (
        b"@<TRIPOS>MOLECULE\nsynthetic\n" + str(count).encode() + b" 0 0 0 0\nSMALL\nNO_CHARGES\n"
        b"@<TRIPOS>ATOM\n" + b"\n".join(rows) + b"\n" + metadata
    )


class MOL2NormativeTests(unittest.TestCase):
    def test_decimal_and_exponent_coordinates_have_explicit_numeric_meaning(self):
        source = molecule([b"1 atom .125 +2. -3e-1 C.3"])
        (geometry,) = prepare_mol2(source, source_unit="angstrom", selection="all")
        self.assertEqual([[0.125, 2, -0.3]], json.loads(geometry.request_bytes)["coordinates"])

    def test_corpus_atom_order_hydrogens_and_exact_source_mapping(self):
        source = (FIXTURES / "normative/atom_order.mol2").read_bytes()
        (geometry,) = prepare_mol2(source, source_unit="angstrom", selection=(1,))
        expected = json.loads((FIXTURES / "expected.json").read_bytes())["geometries"]["chloromethane"]
        request = json.loads(geometry.request_bytes)
        self.assertEqual(expected["atoms"], request["atoms"])
        self.assertEqual(expected["coordinates_angstrom"], request["coordinates"])
        self.assertEqual((1, 2, 3, 4, 5), geometry.source_positions)
        self.assertEqual("atom_id", geometry.source_position_kind)
        self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)
        self.assertEqual(1, geometry.source_record_number)

    def test_all_and_ordered_records_keep_different_and_repeated_geometries(self):
        source = (FIXTURES / "normative/mixed_records.mol2").read_bytes()
        expected = json.loads((FIXTURES / "expected.json").read_bytes())["geometries"]
        for selection, numbers in (("all", (1, 2, 3)), ((3, 1), (3, 1))):
            with self.subTest(selection=selection):
                geometries = prepare_mol2(source, source_unit="angstrom", selection=selection)
                self.assertEqual(numbers, tuple(g.source_record_number for g in geometries))
                for geometry, number in zip(geometries, numbers, strict=True):
                    name = "water" if number == 2 else "chloromethane"
                    self.assertEqual(expected[name]["atoms"], json.loads(geometry.request_bytes)["atoms"])
                    self.assertEqual(expected[name]["coordinates_angstrom"], json.loads(geometry.request_bytes)["coordinates"])
                    self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)

    def test_nonconsecutive_source_ids_stay_in_file_order_without_bond_interpretation(self):
        source = molecule([b"42 H1 1.09 0 0 H", b"7 C1 0 0 0 C.3"], metadata=b"@<TRIPOS>BOND\n1 42 7 1\n")
        (geometry,) = prepare_mol2(source, source_unit="angstrom", selection="all")
        self.assertEqual((42, 7), geometry.source_positions)
        self.assertEqual(["H", "C"], json.loads(geometry.request_bytes)["atoms"])
        self.assertEqual([[1.09, 0, 0], [0, 0, 0]], json.loads(geometry.request_bytes)["coordinates"])

    def test_documented_typed_elements_project_without_sanitizing_or_adding_h(self):
        # Independent transcription of Tripos SYBYL 7.1 pp. 53-54, not a
        # generated expectation from the decoder's vocabulary.
        types = (
            "C.3",
            "C.2",
            "C.1",
            "C.ar",
            "C.cat",
            "N.3",
            "N.2",
            "N.1",
            "N.ar",
            "N.am",
            "N.pl3",
            "N.4",
            "O.3",
            "O.2",
            "O.co2",
            "O.spc",
            "O.t3p",
            "S.3",
            "S.2",
            "S.O",
            "S.O2",
            "P.3",
            "H.spc",
            "H.t3p",
            "Cr.th",
            "Cr.oh",
            "Co.oh",
            "S.o",
            "S.o2",
        )
        rows = [f"{index} atom {index} 0 0 {kind}".encode() for index, kind in enumerate(types, 1)]
        (geometry,) = prepare_mol2(molecule(rows), source_unit="angstrom", selection="all")
        expected = ["C"] * 5 + ["N"] * 7 + ["O"] * 5 + ["S"] * 4 + ["P"] + ["H"] * 2 + ["Cr"] * 2 + ["Co"] + ["S"] * 2
        self.assertEqual(expected, json.loads(geometry.request_bytes)["atoms"])
        self.assertEqual(len(types), len(geometry.source_positions))

    def test_explicit_units_and_opaque_metadata_do_not_change_supplied_atom_inventory(self):
        source = molecule([b"19 name-is-not-an-element 0.125 0 0 C.3"], metadata=b"@<TRIPOS>SUBSTRUCTURE\nPRIVATE_METADATA\n")
        (geometry,) = prepare_mol2(source, source_unit="nm", selection="all")
        self.assertEqual(["C"], json.loads(geometry.request_bytes)["atoms"])
        self.assertEqual([[1.25, 0, 0]], json.loads(geometry.request_bytes)["coordinates"])
        self.assertEqual((19,), geometry.source_positions)
        self.assertEqual(("nm", 10), (geometry.source_unit, geometry.scale_to_angstrom))
        self.assertNotIn("PRIVATE_METADATA", repr(geometry))

    def test_crlf_and_comments_preserve_geometry_but_hash_the_whole_original_source(self):
        source = b"# original source\n" + molecule([b"1 atom 0 0 0 C.3"])
        converted = source.replace(b"\n", b"\r\n")
        original = prepare_mol2(source, source_unit="angstrom", selection="all")[0]
        actual = prepare_mol2(converted, source_unit="angstrom", selection="all")[0]
        self.assertEqual(original.request_bytes, actual.request_bytes)
        self.assertNotEqual(original.source_sha256, actual.source_sha256)
        self.assertEqual(hashlib.sha256(converted).hexdigest(), actual.source_sha256)


class MOL2AdversarialTests(unittest.TestCase):
    def test_pseudo_query_and_unknown_types_do_not_become_concrete_atoms(self):
        for kind in (
            "LP",
            "Du",
            "Du.C",
            "Any",
            "Hal",
            "Het",
            "Hev",
            "C.PRIVATE_TYPE",
            "UNRECOGNIZED_TYPE",
            "D",
            "T",
            "s.o",
            "S.O3",
        ):
            with self.subTest(kind=kind), self.assertRaises(InputRejected) as caught:
                prepare_mol2(molecule([f"1 carbon-name 0 0 0 {kind}".encode()]), source_unit="angstrom", selection="all")
            self.assertNotIn("PRIVATE_TYPE", str(caught.exception))

    def test_bad_selected_suffix_fails_atomically_while_unselected_types_stay_opaque(self):
        first = (FIXTURES / "normative/atom_order.mol2").read_bytes()
        invalid = (FIXTURES / "adversarial/unsupported_atom_type.mol2").read_bytes()
        source = first + invalid
        with self.assertRaisesRegex(InputRejected, "record 2") as caught:
            prepare_mol2(source, source_unit="angstrom", selection="all")
        self.assertIn("ATOM row 3", str(caught.exception))
        self.assertNotIn("UNRECOGNIZED_TYPE", str(caught.exception))
        self.assertEqual(1, len(prepare_mol2(source, source_unit="angstrom", selection=(1,))))

    def test_atom_counts_sections_and_unique_positive_ids_are_required(self):
        row = b"1 atom 0 0 0 C.3"
        sources = (
            molecule([row], atom_count=2),
            molecule([row], atom_count=0),
            molecule([row], atom_count=129),
            molecule([row, row]),
            molecule([row.replace(b"1 atom", b"0 atom")]),
            molecule([row]) + b"@<TRIPOS>ATOM\n" + row + b"\n",
            molecule([row]) + b"@<TRIPOS>ATOM PRIVATE\n" + row + b"\n",
            molecule([row]).replace(b"@<TRIPOS>ATOM", b"@<TRIPOS>BOND"),
            molecule([row]).replace(b"1 0 0 0 0", b"9" * 5000 + b" 0"),
        )
        for source in sources:
            with self.subTest(source=source[:80]), self.assertRaises(InputRejected):
                prepare_mol2(source, source_unit="angstrom", selection="all")

    def test_section_markers_require_column_one(self):
        source = molecule([b"1 atom 0 0 0 C.3"])
        for marker in (b"@<TRIPOS>MOLECULE", b"@<TRIPOS>ATOM"):
            for padding in (b" ", b"\t"):
                with self.subTest(marker=marker, padding=padding), self.assertRaisesRegex(InputRejected, "column 1"):
                    prepare_mol2(source.replace(marker, padding + marker), source_unit="angstrom", selection="all")

    def test_bad_coordinates_never_echo_values_or_accept_nonfinite_geometry(self):
        for value in (b"PRIVATE_NUMBER", b"nan", b"inf", b"1e308", b"1_0"):
            with self.subTest(value=value):
                with self.assertRaises(InputRejected) as caught:
                    prepare_mol2(molecule([b"1 atom " + value + b" 0 0 C.3"]), source_unit="nm", selection="all")
                self.assertNotIn("PRIVATE_NUMBER", str(caught.exception))

    def test_selection_work_bounds_and_unknown_units_are_not_adjusted(self):
        source = molecule([b"1 atom 0 0 0 C.3"])
        for selection in (None, (), (0,), (2,), (1, 1), (True,), "first"):
            with self.subTest(selection=selection), self.assertRaises(InputRejected):
                prepare_mol2(source, source_unit="angstrom", selection=selection)
        self.assertEqual(128, prepare_mol2(source * 128, source_unit="angstrom", selection=(128,))[0].source_record_number)
        with self.assertRaisesRegex(InputRejected, "128"):
            prepare_mol2(source * 129, source_unit="angstrom", selection=(1,))
        with self.assertRaisesRegex(InputRejected, "1 MiB"):
            prepare_mol2(b" " * (1048576 + 1), source_unit="angstrom", selection="all")
        with self.assertRaises(InputRejected):
            prepare_mol2(source, source_unit=None, selection="all")

    def test_safe_json_atom_ids_and_atom_work_limits_are_enforced_before_output(self):
        maximum = 9007199254740991
        source = molecule([f"{maximum} atom 0 0 0 C.3".encode()])
        self.assertEqual((maximum,), prepare_mol2(source, source_unit="angstrom", selection="all")[0].source_positions)
        with self.assertRaises(InputRejected):
            prepare_mol2(molecule([f"{maximum + 1} atom 0 0 0 C.3".encode()]), source_unit="angstrom", selection="all")
        for count in (128, 129):
            source = molecule([f"{i} atom 0 0 0 C.3".encode() for i in range(1, count + 1)])
            if count == 128:
                self.assertEqual(128, len(prepare_mol2(source, source_unit="angstrom", selection="all")[0].source_positions))
            else:
                with self.assertRaisesRegex(InputRejected, "128"):
                    prepare_mol2(source, source_unit="angstrom", selection="all")
