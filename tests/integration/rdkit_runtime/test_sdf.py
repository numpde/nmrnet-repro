"""Select supplied SDF records without generating atoms or interpreting data fields."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.mol import prepare_mol
from nmrnet_provider.sdf import prepare_sdf


class SdfReaderNormativeTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = Path(__file__).parent / "fixtures"
        self.expected = json.loads((self.fixtures / "expected.json").read_bytes())
        self.source = (self.fixtures / "normative/mixed_records.sdf").read_bytes()
        self.mol = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()

    def test_all_records_preserve_different_molecules_and_repeated_geometries(self):
        geometries = prepare_sdf(self.source, source_unit="angstrom", selection="all")
        identity = self.expected["native_files"]["normative/mixed_records.sdf"]
        self.assertEqual((1, 2, 3), tuple(geometry.source_record_number for geometry in geometries))
        for geometry, name, positions in zip(geometries, identity["geometry_names"], identity["source_atom_ids"], strict=True):
            with self.subTest(record=geometry.source_record_number):
                request = json.loads(geometry.request_bytes)
                expected = self.expected["geometries"][name]
                self.assertEqual(expected["atoms"], request["atoms"])
                self.assertEqual(expected["coordinates_angstrom"], request["coordinates"])
                self.assertEqual(tuple(positions), geometry.source_positions)
                self.assertEqual("atom_id", geometry.source_position_kind)
                self.assertEqual(hashlib.sha256(self.source).hexdigest(), geometry.source_sha256)
        self.assertEqual(geometries[0].request_bytes, geometries[2].request_bytes)

    def test_explicit_selection_keeps_requested_order_and_whole_original_source_digest(self):
        with patch("nmrnet_provider.sdf.prepare_mol", wraps=prepare_mol) as reader:
            geometries = prepare_sdf(self.source, source_unit="angstrom", selection=(3, 1))
        self.assertEqual(2, reader.call_count)
        self.assertEqual((3, 1), tuple(geometry.source_record_number for geometry in geometries))
        self.assertTrue(all(geometry.source_sha256 == hashlib.sha256(self.source).hexdigest() for geometry in geometries))

    def test_supplied_carbon_conformers_do_not_gain_hydrogens_or_lose_coordinates(self):
        source = (self.fixtures / "normative/butane_conformers.sdf").read_bytes()
        geometries = prepare_sdf(source, source_unit="angstrom", selection="all")
        for geometry, name in zip(geometries, ("butane_supplied_a", "butane_supplied_b"), strict=True):
            request = json.loads(geometry.request_bytes)
            expected = self.expected["geometries"][name]
            self.assertEqual(expected["atoms"], request["atoms"])
            self.assertEqual(expected["coordinates_angstrom"], request["coordinates"])
        self.assertNotEqual(geometries[0].request_bytes, geometries[1].request_bytes)

    def test_crlf_blank_title_and_empty_or_multiline_data_do_not_change_geometry(self):
        blank_title = self.mol.split(b"\n", 1)[1]
        blank_title = b"\n" + blank_title
        source = blank_title + b">  <EMPTY>\n\n> DT12\nfirst\n> data text, not a header\nlast\n\n$$$$\n\n"
        expected = prepare_mol(blank_title, source_unit="angstrom")
        for original in (source, source.replace(b"\n", b"\r\n")):
            with self.subTest(crlf=b"\r" in original):
                (geometry,) = prepare_sdf(original, source_unit="angstrom", selection="all")
                self.assertEqual(expected.request_bytes, geometry.request_bytes)
                self.assertEqual(hashlib.sha256(original).hexdigest(), geometry.source_sha256)

    def test_units_are_converted_by_the_existing_mol_owner(self):
        (geometry,) = prepare_sdf(self.mol + b"$$$$\n", source_unit="nm", selection=(1,))
        self.assertEqual("nm", geometry.source_unit)
        self.assertEqual(10.0, geometry.scale_to_angstrom)
        self.assertEqual([0.0, 0.0, 17.8], json.loads(geometry.request_bytes)["coordinates"][1])

    def test_delimiter_prefix_allows_suffix_text_but_not_leading_space(self):
        source = self.mol + b"> <VALUE>\n $$$$ opaque value\n\n$$$$ suffix\n" + self.mol + b"$$$$\n"
        geometries = prepare_sdf(source, source_unit="angstrom", selection=(2, 1))
        self.assertEqual((2, 1), tuple(geometry.source_record_number for geometry in geometries))
        self.assertEqual(geometries[0].request_bytes, geometries[1].request_bytes)
        self.assertTrue(all(geometry.source_sha256 == hashlib.sha256(source).hexdigest() for geometry in geometries))

    def test_128_records_are_read_without_redefining_the_bound_as_scientific_capacity(self):
        source = (self.mol + b"$$$$\n") * 128
        geometries = prepare_sdf(source, source_unit="angstrom", selection="all")
        self.assertEqual(tuple(range(1, 129)), tuple(geometry.source_record_number for geometry in geometries))

    def test_source_byte_limit_is_inclusive_and_counts_opaque_data(self):
        prefix, suffix = self.mol + b"> <BOUND>\n", b"\n\n$$$$\n"
        lines, remainder = divmod(1024 * 1024 - len(prefix) - len(suffix), 129)
        source = prefix + (b"x" * 128 + b"\n") * lines + b"x" * remainder + suffix
        self.assertEqual(1024 * 1024, len(source))
        (geometry,) = prepare_sdf(source, source_unit="angstrom", selection="all")
        self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)
        with self.assertRaises(InputRejected):
            prepare_sdf(source + b"\n", source_unit="angstrom", selection="all")

    def test_success_retains_private_native_warnings_without_public_repr(self):
        warned = prepare_mol(self.mol, source_unit="angstrom")
        warned = replace(warned, diagnostic="PRIVATE_SDF_WARNING\n[RDKit diagnostic truncated]\n")
        with patch("nmrnet_provider.sdf.prepare_mol", return_value=warned):
            (geometry,) = prepare_sdf(self.mol + b"$$$$\n", source_unit="angstrom", selection=(1,))
        self.assertEqual(warned.diagnostic, geometry.diagnostic)
        self.assertNotIn("PRIVATE_SDF_WARNING", repr(geometry))


class SdfReaderAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = Path(__file__).parent / "fixtures"
        self.mol = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        self.source = self.mol + b"$$$$\n"

    def test_bad_or_duplicate_selection_is_rejected_before_native_parsing(self):
        with patch("nmrnet_provider.mol._read_atoms") as native:
            for selection in (None, (), [], (True,), (1.0,), ("1",), (0,), (-1,), (2,), (1, 1), (1,) * 129, "first"):
                with self.subTest(selection=selection), self.assertRaises(InputRejected):
                    prepare_sdf(self.source, source_unit="angstrom", selection=selection)
            native.assert_not_called()

    def test_source_record_and_unit_bounds_precede_native_parsing(self):
        with patch("nmrnet_provider.mol._read_atoms") as native:
            for source in (b"x" * (1024 * 1024 + 1), self.source * 129):
                with self.subTest(size=len(source)), self.assertRaises(InputRejected):
                    prepare_sdf(source, source_unit="angstrom", selection=(1,))
            with self.assertRaises(InputRejected):
                prepare_sdf(self.source, source_unit=None, selection="all")
            with self.assertRaises(TypeError):
                prepare_sdf(bytearray(self.source), source_unit="angstrom", selection="all")
            native.assert_not_called()

    def test_unselected_invalid_and_v3000_records_never_reach_native_parser(self):
        invalid = (self.fixtures / "adversarial/invalid_last.sdf").read_bytes()
        geometries = prepare_sdf(invalid, source_unit="angstrom", selection=(3, 1))
        self.assertEqual((3, 1), tuple(geometry.source_record_number for geometry in geometries))
        source = self.source.replace(b"V2000", b"V3000", 1) + self.source
        with patch("nmrnet_provider.sdf.prepare_mol", wraps=prepare_mol) as reader:
            (geometry,) = prepare_sdf(source, source_unit="angstrom", selection=(2,))
        self.assertEqual(1, reader.call_count)
        self.assertNotIn(b"V3000", reader.call_args.args[0])
        self.assertEqual(2, geometry.source_record_number)
        with patch("nmrnet_provider.mol._read_atoms") as native:
            with self.assertRaisesRegex(InputRejected, "SDF record 1"):
                prepare_sdf(source, source_unit="angstrom", selection=(1,))
            native.assert_not_called()

    def test_selected_bad_suffix_rejects_entire_collection_without_private_text(self):
        source = (self.fixtures / "adversarial/invalid_last.sdf").read_bytes()
        with self.assertRaisesRegex(InputRejected, "SDF record 4") as failure:
            prepare_sdf(source, source_unit="angstrom", selection="all")
        self.assertNotIn("Truncated atom block", str(failure.exception))

    def test_missing_or_empty_record_delimiters_do_not_silently_drop_geometry(self):
        for source in (b"", b"$$$$\n", self.mol, self.source + self.mol, self.source + b"$$$$\n"):
            with self.subTest(size=len(source)), patch("nmrnet_provider.mol._read_atoms") as native:
                with self.assertRaises(InputRejected):
                    prepare_sdf(source, source_unit="angstrom", selection=(1,))
                native.assert_not_called()

    def test_unframed_second_mol_and_malformed_data_items_do_not_become_ignored_properties(self):
        for tail in (self.mol, b"PRIVATE_DATA_WITHOUT_HEADER\n", b">\nvalue\n\n", b"> <VALUE>\nunterminated\n"):
            with self.subTest(size=len(tail)), patch("nmrnet_provider.mol._read_atoms") as native:
                with self.assertRaises(InputRejected) as failure:
                    prepare_sdf(self.mol + tail + b"$$$$\n", source_unit="angstrom", selection="all")
                self.assertNotIn("PRIVATE_DATA_WITHOUT_HEADER", str(failure.exception))
                native.assert_not_called()

    def test_native_operational_failure_keeps_type_cause_and_safe_record_context(self):
        failure = RuntimeError("PRIVATE_INCONCLUSIVE_NATIVE_FAILURE")
        with patch("nmrnet_provider.sdf.prepare_mol", side_effect=failure), self.assertRaises(RuntimeError) as raised:
            prepare_sdf(self.source, source_unit="angstrom", selection="all")
        self.assertIs(failure, raised.exception)
        self.assertNotIsInstance(raised.exception, InputRejected)
        self.assertTrue(any("SDF record 1" in note for note in raised.exception.__notes__))

    def test_input_rejection_retains_safe_reason_private_diagnostic_and_original_cause(self):
        failure = InputRejected("Cannot prepare coordinates: value is not finite.", diagnostic="PRIVATE_NATIVE_CAUSE")
        with (
            patch("nmrnet_provider.sdf.prepare_mol", side_effect=failure),
            self.assertRaisesRegex(InputRejected, "SDF record 1") as raised,
        ):
            prepare_sdf(self.source, source_unit="angstrom", selection="all")
        self.assertIn("not finite", str(raised.exception))
        self.assertEqual(failure.diagnostic, raised.exception.diagnostic)
        self.assertIs(failure, raised.exception.__cause__)
        self.assertNotIn("PRIVATE_NATIVE_CAUSE", str(raised.exception))
        self.assertNotIn("PRIVATE_NATIVE_CAUSE", repr(raised.exception))
