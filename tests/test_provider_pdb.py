"""Read supplied PDB coordinates without changing atom or model correspondence."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import unittest
from pathlib import Path

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.pdb import prepare_pdb

FIXTURES = Path(__file__).parent / "fixtures/preparation"


def atom(serial, name, element, coordinates, *, alternate=" ", residue="LIG", chain="A", number=1):
    """Author fixed-column source records independently of the reader."""
    x, y, z = coordinates
    return (
        f"HETATM{serial:5d} {name:4s}{alternate}{residue:3s} {chain}{number:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}\n"
    ).encode("ascii")


class PDBPreparationTests(unittest.TestCase):
    def prepare(self, source, *, selection="all", alternate_location=None, source_unit="angstrom"):
        return prepare_pdb(source, source_unit=source_unit, selection=selection, alternate_location=alternate_location)

    def test_original_serials_order_elements_and_supplied_h_survive(self):
        source = (FIXTURES / "normative/atom_order.pdb").read_bytes()
        (geometry,) = self.prepare(source, selection=(1,))
        expected = json.loads((FIXTURES / "expected.json").read_bytes())["geometries"]["chloromethane"]
        request = json.loads(geometry.request_bytes)
        self.assertEqual(expected["atoms"], request["atoms"])
        self.assertEqual(expected["coordinates_angstrom"], request["coordinates"])
        self.assertEqual((40, 7, 22, 5, 91), geometry.source_positions)
        self.assertEqual("atom_serial", geometry.source_position_kind)
        self.assertEqual(1, geometry.source_record_number)
        self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)

    def test_later_models_have_independent_coordinates_order_and_actual_serials(self):
        # Nonconsecutive model numbers and unlike atom sets forbid implicit
        # first-model reuse, positional alignment or molecular-equivalence checks.
        source = (
            b"MODEL        4\n" + atom(9, "C1", "C", (0, 0, 0)) + b"ENDMDL\n"
            b"MODEL       17\n" + atom(30, "H1", "H", (2, 3, 4)) + atom(2, "C1", "C", (-1, 0.5, 7)) + b"ENDMDL\nEND\n"
        )
        geometries = self.prepare(source, selection=(17, 4))
        self.assertEqual((17, 4), tuple(g.source_record_number for g in geometries))
        self.assertEqual((30, 2), geometries[0].source_positions)
        self.assertEqual([[2, 3, 4], [-1, 0.5, 7]], json.loads(geometries[0].request_bytes)["coordinates"])
        self.assertEqual(["C"], json.loads(geometries[1].request_bytes)["atoms"])
        self.assertEqual((4, 17), tuple(g.source_record_number for g in self.prepare(source)))
        self.assertTrue(all(g.source_sha256 == hashlib.sha256(source).hexdigest() for g in geometries))

    def test_explicit_alternate_keeps_common_rows_once_without_occupancy_selection(self):
        source = (FIXTURES / "normative/alternate_locations.pdb").read_bytes()
        source = source.replace(b"0.50", b"0.90", 1).replace(b"0.50", b"0.10", 1)
        with self.assertRaisesRegex(InputRejected, "alternate"):
            self.prepare(source)
        (geometry,) = self.prepare(source, alternate_location="B")
        self.assertEqual((40, 7, 23, 5, 91), geometry.source_positions)
        self.assertEqual([0.125, 0, 0], json.loads(geometry.request_bytes)["coordinates"][2])
        self.assertEqual(5, len(json.loads(geometry.request_bytes)["atoms"]))

    def test_missing_alternate_duplicate_site_and_conflicting_common_site_refuse(self):
        a = atom(1, "C1", "C", (0, 0, 0), alternate="A")
        b = atom(2, "C1", "C", (1, 0, 0), alternate="B")
        sources = (
            a,
            a + a.replace(b"    1", b"    3", 1),
            atom(3, "C1", "C", (2, 0, 0)) + a,
            a + atom(3, "C1", "C", (2, 0, 0), alternate="B", residue="OTH"),
        )
        for source in sources:
            with self.subTest(source=source), self.assertRaises(InputRejected):
                self.prepare(source + b"END\n", alternate_location="B")
        with self.assertRaisesRegex(InputRejected, "serial"):
            self.prepare(a + b.replace(b"    2", b"    1", 1) + b"END\n", alternate_location="A")

    def test_unknown_model_selection_or_alternate_is_not_adjusted(self):
        source = atom(1, "C1", "C", (0, 0, 0)) + b"END\n"
        for selection in (None, "first", (), (0,), (2,), (1, 1), (True,)):
            with self.subTest(selection=selection), self.assertRaises(InputRejected):
                self.prepare(source, selection=selection)
        for alternate in ("", " ", "AB", True, 1, "\n"):
            with self.subTest(alternate=alternate), self.assertRaises(InputRejected):
                self.prepare(source, alternate_location=alternate)

    def test_element_columns_are_required_and_atom_name_does_not_override_them(self):
        source = atom(1, "CA", "C", (0, 0, 0)) + b"END\n"
        self.assertEqual(["C"], json.loads(self.prepare(source)[0].request_bytes)["atoms"])
        for element in ("", "D", "T", "d", "t", "C1"):
            with self.subTest(element=element), self.assertRaises(InputRejected):
                self.prepare(atom(1, "CA", element, (0, 0, 0)) + b"END\n")

    def test_alternate_labels_are_not_limited_to_letters_and_unselected_values_stay_opaque(self):
        first = atom(1, "C1", "C", (0, 0, 0), alternate="@")
        # Its source identity is usable, so an unselected alternative needs no
        # coordinate/element parse, even when those values are absent or invalid.
        unselected = atom(2, "C1", "C", (1, 0, 0), alternate="B")[:27] + b"PRIVATE\n"
        (geometry,) = self.prepare(first + unselected + b"END\n", alternate_location="@")
        self.assertEqual((1,), geometry.source_positions)
        self.assertEqual([[0, 0, 0]], json.loads(geometry.request_bytes)["coordinates"])

    def test_crlf_changes_the_source_digest_without_changing_atom_correspondence(self):
        source = atom(19, "C1", "C", (1, 2, 3)) + b"END\n"
        crlf = source.replace(b"\n", b"\r\n")
        first, second = self.prepare(source)[0], self.prepare(crlf)[0]
        self.assertEqual(first.request_bytes, second.request_bytes)
        self.assertEqual(first.source_positions, second.source_positions)
        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(hashlib.sha256(crlf).hexdigest(), second.source_sha256)

    def test_atom_and_residue_columns_reject_nonprintable_ascii(self):
        row = atom(1, "C1", "C", (0, 0, 0))
        for position in (12, 21, 76):
            with self.subTest(position=position), self.assertRaises(InputRejected):
                self.prepare(row[:position] + b"\x7f" + row[position + 1 :] + b"END\n")

    def test_unselected_model_atom_values_are_not_parsed_but_all_model_framing_is_required(self):
        source = (
            b"MODEL        1\nHETATM PRIVATE MALFORMED VALUES\nENDMDL\nMODEL        9\n"
            + atom(88, "C1", "C", (7, 0, 0))
            + b"ENDMDL\nEND\n"
        )
        self.assertEqual((88,), self.prepare(source, selection=(9,))[0].source_positions)
        with self.assertRaisesRegex(InputRejected, "model 1"):
            self.prepare(source)
        with self.assertRaises(InputRejected):
            self.prepare(source.replace(b"ENDMDL\nMODEL", b"MODEL", 1), selection=(9,))

    def test_selected_failure_names_model_and_returns_no_prefix_or_private_values(self):
        first = b"MODEL        1\n" + atom(1, "C1", "C", (0, 0, 0)) + b"ENDMDL\n"
        second = b"MODEL        2\n" + atom(2, "H1", "H", (1, 0, 0)).replace(b"   1.000", b" PRIVATE") + b"ENDMDL\nEND\n"
        source = first + second
        with self.assertRaisesRegex(InputRejected, "model 2") as caught:
            self.prepare(source)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(1, len(self.prepare(source, selection=(1,))))

    def test_bad_framing_cannot_hide_coordinate_records(self):
        row = atom(1, "C1", "C", (0, 0, 0))
        sources = (
            row,
            row + b"END\n" + row,
            b"ENDMDL\n" + row + b"END\n",
            b"MODEL        1\n" + row + b"END\n",
            b"MODEL        1\nMODEL        2\n" + row + b"ENDMDL\nEND\n",
            row + b"MODEL        1\n" + row + b"ENDMDL\nEND\n",
            b"MODEL        1\n" + row + b"ENDMDL\n" + row + b"END\n",
            b"MODEL        1\n" + row + b"ENDMDL\nMODEL        1\n" + row + b"ENDMDL\nEND\n",
            b"MODEL        1junk\n" + row + b"ENDMDL\nEND\n",
            b"MODEL        1\n" + row + b"ENDMDLjunk\nEND\n",
            row + b"END   junk\n",
        )
        for source in sources:
            with self.subTest(source=source), self.assertRaises(InputRejected):
                self.prepare(source)

    def test_explicit_units_convert_once_and_nonfinite_or_overflow_refuse(self):
        source = atom(1, "C1", "C", (0.125, 0, 0)) + b"END\n"
        (geometry,) = self.prepare(source, source_unit="nm")
        self.assertEqual([1.25, 0, 0], json.loads(geometry.request_bytes)["coordinates"][0])
        self.assertEqual(("nm", 10), (geometry.source_unit, geometry.scale_to_angstrom))
        for replacement in (b"     nan", b"     inf", b"   1e308"):
            with self.subTest(replacement=replacement), self.assertRaises(InputRejected):
                self.prepare(source.replace(b"   0.125", replacement), source_unit="nm")
        with self.assertRaises(InputRejected):
            self.prepare(source, source_unit=None)

    def test_source_record_and_selected_atom_work_bounds(self):
        source = (
            b"".join(
                b"MODEL     " + f"{number:4d}\n".encode() + atom(1, "C1", "C", (0, 0, 0)) + b"ENDMDL\n"
                for number in range(1, 129)
            )
            + b"END\n"
        )
        self.assertEqual(128, self.prepare(source, selection=(128,))[0].source_record_number)
        with self.assertRaisesRegex(InputRejected, "128"):
            self.prepare(source.replace(b"END\n", b"MODEL      129\nENDMDL\nEND\n"), selection=(1,))
        for count in (128, 129):
            rows = b"".join(atom(i, f"{i:4d}", "C", (0, 0, 0)) for i in range(1, count + 1)) + b"END\n"
            if count == 128:
                self.assertEqual(128, len(self.prepare(rows)[0].source_positions))
            else:
                with self.assertRaisesRegex(InputRejected, "128"):
                    self.prepare(rows)
        with self.assertRaisesRegex(InputRejected, "1 MiB"):
            self.prepare(b" " * (1048576 + 1))
        minimal = atom(1, "C1", "C", (0, 0, 0)) + b"END\n"
        self.assertEqual(1, len(self.prepare(minimal + b" " * (1048576 - len(minimal)))))
