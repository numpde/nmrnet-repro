"""Check supplied MOL coordinates against independently authored fixture values."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from rdkit import Chem

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.mol import prepare_mol


class MolReaderNormativeTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = Path(__file__).parent / "fixtures"
        self.expected = json.loads((self.fixtures / "expected.json").read_bytes())

    def test_supplied_atoms_coordinates_and_row_ids_are_preserved(self):
        for name in ("atom_order_v2000.mol", "heavy_atoms_only.mol", "planar_3d.mol"):
            with self.subTest(file=name):
                relative = "normative/" + name
                source = (self.fixtures / relative).read_bytes()
                geometry = prepare_mol(source, source_unit="angstrom")
                identity = self.expected["native_files"][relative]
                expected = self.expected["geometries"][identity["geometry_names"][0]]
                actual = json.loads(geometry.request_bytes)
                self.assertEqual(expected["atoms"], actual["atoms"])
                self.assertEqual(expected["coordinates_angstrom"], actual["coordinates"])
                self.assertEqual(tuple(identity["source_atom_ids"][0]), geometry.source_positions)
                self.assertEqual("atom_id", geometry.source_position_kind)
                self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)
                self.assertEqual("angstrom", actual["coordinate_unit"])
                self.assertEqual(1, geometry.source_record_number)

    def test_declared_units_are_converted_once_without_changing_source_identity(self):
        source = (self.fixtures / "normative/heavy_atoms_only.mol").read_bytes()
        geometry = prepare_mol(source, source_unit="nm")
        actual = json.loads(geometry.request_bytes)
        self.assertEqual([0.0, 0.0, 17.8], actual["coordinates"][0])
        self.assertEqual("nm", geometry.source_unit)
        self.assertEqual(10.0, geometry.scale_to_angstrom)
        self.assertEqual("NIST SP 811 Appendix B.8", geometry.conversion_reference)
        self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)

    def test_crlf_and_trailing_blank_lines_preserve_the_same_coordinates(self):
        source = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        original = prepare_mol(source, source_unit="angstrom")
        changed = prepare_mol(source.replace(b"\n", b"\r\n") + b"\r\n", source_unit="angstrom")
        self.assertEqual(original.request_bytes, changed.request_bytes)
        self.assertNotEqual(original.source_sha256, changed.source_sha256)
        lowercase = prepare_mol(source.replace(b"3D\n", b"3d\n", 1), source_unit="angstrom")
        self.assertEqual(original.request_bytes, lowercase.request_bytes)

    def test_native_reader_keeps_hydrogens_and_disables_sanitization(self):
        source = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        with patch.object(Chem, "MolFromMolBlock", wraps=Chem.MolFromMolBlock) as reader:
            prepare_mol(source, source_unit="angstrom")
        self.assertEqual({"sanitize": False, "removeHs": False, "strictParsing": True}, reader.call_args.kwargs)

    def test_native_warnings_on_success_remain_available_without_public_repr(self):
        original = Chem.MolFromMolBlock

        def read_with_warning(*arguments, **keywords):
            sys.stderr.write("PRIVATE_NATIVE_WARNING\n")
            return original(*arguments, **keywords)

        source = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        unwarned = prepare_mol(source, source_unit="angstrom")
        with patch.object(Chem, "MolFromMolBlock", side_effect=read_with_warning):
            geometry = prepare_mol(source, source_unit="angstrom")
        self.assertIn("PRIVATE_NATIVE_WARNING", geometry.diagnostic)
        self.assertNotIn("PRIVATE_NATIVE_WARNING", repr(geometry))
        self.assertEqual(unwarned, geometry)


class MolReaderAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = Path(__file__).parent / "fixtures"
        self.source = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()

    def test_unsupported_versions_and_dimensions_stop_before_native_parsing(self):
        sources = (
            (self.fixtures / "normative/atom_order_v3000.mol").read_bytes(),
            (self.fixtures / "adversarial/declared_2d.mol").read_bytes(),
            self.source.replace(b"3D\n", b"2D\n", 1),
            self.source.replace(b"3D\n", b"  \n", 1),
            self.source.replace(b"V2000", b"V9999", 1),
        )
        with patch("nmrnet_provider.mol._read_atoms") as native:
            for source in sources:
                with self.subTest(source_sha256=hashlib.sha256(source).hexdigest()), self.assertRaises(InputRejected):
                    prepare_mol(source, source_unit="angstrom")
            native.assert_not_called()

    def test_work_bounds_and_invalid_units_stop_before_native_parsing(self):
        sources = (
            self.source.replace(b"  5  4", b"129  4", 1),
            self.source.replace(b"  5  4", b"  0  4", 1),
            self.source.replace(b"  5  4", b"  5 -1", 1),
            b"x" * (1024 * 1024 + 1),
        )
        with patch("nmrnet_provider.mol._read_atoms") as native:
            for source in sources:
                with self.subTest(size=len(source)), self.assertRaises(InputRejected):
                    prepare_mol(source, source_unit="angstrom")
            with self.assertRaises(InputRejected):
                prepare_mol(self.source, source_unit=None)
            native.assert_not_called()

    def test_single_mol_does_not_silently_ignore_a_second_record(self):
        for suffix in (self.source, b"$$$$\nPRIVATE_TRAILING_RECORD\n"):
            with self.subTest(suffix_length=len(suffix)), self.assertRaises(InputRejected):
                prepare_mol(self.source + suffix, source_unit="angstrom")

    def test_malformed_framing_and_counts_stop_before_native_parsing(self):
        sources = (
            b"\xff" + self.source,
            self.source.replace(b"  5  4", b" -1  4", 1),
            self.source.replace(b"  5  4", b"xyz  4", 1),
            self.source.replace(b"  5  4", b"\xc2\xb2 5 4", 1),
            self.source.replace(b"M  END", b"", 1),
            self.source + b"M  END\n",
            b"\n".join(self.source.split(b"\n")[:5]) + b"\nM  END\n",
            self.source.replace(b"\n", "\u2028".encode()),
        )
        with patch("nmrnet_provider.mol._read_atoms") as native:
            for source in sources:
                with self.subTest(source_sha256=hashlib.sha256(source).hexdigest()), self.assertRaises(InputRejected):
                    prepare_mol(source, source_unit="angstrom")
            native.assert_not_called()

    def test_v3000_rejection_in_a_fresh_interpreter_does_not_import_rdkit(self):
        script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.mol import prepare_mol
try:
    prepare_mol(Path(sys.argv[2]).read_bytes(), source_unit='angstrom')
except InputRejected:
    assert not any(name == 'rdkit' or name.startswith('rdkit.') for name in sys.modules)
else:
    raise AssertionError('V3000 was not rejected')
"""
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                script,
                str(Path(__file__).parent),
                str(self.fixtures / "normative/atom_order_v3000.mol"),
            ],
            check=True,
        )

    def test_unexpected_native_failure_remains_an_operational_failure(self):
        cause = RuntimeError("PRIVATE_NATIVE_RUNTIME_FAILURE")

        def fail_after_warning(*_arguments, **_keywords):
            sys.stderr.write("PRIVATE_WARNING_BEFORE_FAILURE\n")
            raise cause

        with patch.object(Chem, "MolFromMolBlock", side_effect=fail_after_warning), self.assertRaises(RuntimeError) as failure:
            prepare_mol(self.source, source_unit="angstrom")
        self.assertIs(cause, failure.exception)
        self.assertIn("PRIVATE_WARNING_BEFORE_FAILURE", "\n".join(cause.__notes__))
        self.assertNotIn("PRIVATE_WARNING_BEFORE_FAILURE", str(cause))

    def test_native_parse_failure_keeps_its_cause_without_blame_on_the_input(self):
        malformed = self.source.replace(b"    1.0270", b"PRIVATE_BAD_COORDINATE", 1)
        with self.assertRaises(RuntimeError) as failure:
            prepare_mol(malformed, source_unit="angstrom")
        self.assertIn("RDKit", str(failure.exception))
        self.assertIn("cannot distinguish", str(failure.exception))
        self.assertTrue(failure.exception.__notes__)
        self.assertNotIn("PRIVATE_BAD_COORDINATE", str(failure.exception))
        self.assertNotIn("PRIVATE_BAD_COORDINATE", repr(failure.exception))

    def test_empty_native_result_without_a_reason_still_is_not_input_rejection(self):
        with patch.object(Chem, "MolFromMolBlock", return_value=None), self.assertRaises(RuntimeError) as failure:
            prepare_mol(self.source, source_unit="angstrom")
        self.assertIn("cannot distinguish", str(failure.exception))

    def test_oversized_native_reason_is_bounded_without_claiming_input_rejection(self):
        def reject_with_log(*_args, **_kwargs):
            sys.stderr.write("PRIVATE_NATIVE_CAUSE " + "x" * 100000)

        with patch.object(Chem, "MolFromMolBlock", side_effect=reject_with_log), self.assertRaises(RuntimeError) as failure:
            prepare_mol(self.source, source_unit="angstrom")
        diagnostic = "\n".join(failure.exception.__notes__)
        self.assertIn("PRIVATE_NATIVE_CAUSE", diagnostic)
        self.assertLess(len(diagnostic), 9000)
        self.assertIn("truncated", diagnostic)
        self.assertNotIn("PRIVATE_NATIVE_CAUSE", str(failure.exception))
