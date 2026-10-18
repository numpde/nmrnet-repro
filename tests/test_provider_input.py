"""Preserve supplied geometry and reject unsafe shortcuts before model work."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError

from nmrnet_provider import input_geometry


class ProviderInputTests(unittest.TestCase):
    def test_direct_json_preserves_bytes_and_source_atom_order(self):
        source = b' {"atoms":["H","C"], "coordinates":[[1.09,0,0],[0,0,0]], "coordinate_unit":"angstrom"}\n'
        prepared = input_geometry.prepare_json(source)

        self.assertEqual(source, prepared.request_bytes)
        self.assertEqual(hashlib.sha256(source).hexdigest(), prepared.source_sha256)
        self.assertEqual((0, 1), prepared.source_positions)
        self.assertEqual("atom_index", prepared.source_position_kind)
        self.assertEqual("angstrom", prepared.source_unit)
        self.assertEqual(1.0, prepared.scale_to_angstrom)
        self.assertNotIn("coordinates", repr(prepared))
        with self.assertRaises(FrozenInstanceError):
            prepared.request_bytes = b"changed"

    def test_direct_json_requires_angstrom_even_without_predicted_heads(self):
        for element in ("N", "H", "C"):
            source = json.dumps(
                {
                    "atoms": [element],
                    "coordinates": [[0, 0, 0]],
                    "coordinate_unit": "nm",
                }
            ).encode()
            with self.subTest(element=element), self.assertRaisesRegex(input_geometry.InputRejected, "explicit preparation"):
                input_geometry.prepare_json(source)

    def test_malformed_direct_json_is_a_rejection_not_an_interpretation(self):
        with self.assertRaisesRegex(input_geometry.InputRejected, "JSON") as caught:
            input_geometry.prepare_json(b'{"atoms":')
        self.assertNotIn("convert", str(caught.exception))

    def test_duplicate_fields_are_rejected_by_existing_admission(self):
        with self.assertRaisesRegex(input_geometry.InputRejected, "duplicate") as caught:
            input_geometry.prepare_json(b'{"atoms": [], "atoms": []}')
        self.assertNotIn("convert", str(caught.exception))

    def test_plaintext_conversion_preserves_order_and_line_mapping(self):
        source = b"\nH 0.109 0 0\nC 0 0 0\n"
        prepared = input_geometry.prepare_table(source, source_unit="nm")
        request = json.loads(prepared.request_bytes)

        self.assertEqual(["H", "C"], request["atoms"])
        self.assertAlmostEqual(1.09, request["coordinates"][0][0], places=12)
        self.assertEqual([[0.0, 0.0, 0.0]], request["coordinates"][1:])
        self.assertEqual("angstrom", request["coordinate_unit"])
        self.assertEqual((2, 3), prepared.source_positions)
        self.assertEqual("line_number", prepared.source_position_kind)
        self.assertEqual(hashlib.sha256(source).hexdigest(), prepared.source_sha256)
        self.assertEqual("nm", prepared.source_unit)
        self.assertEqual(10.0, prepared.scale_to_angstrom)

    def test_table_does_not_infer_units_or_accept_an_unlabelled_second_geometry(self):
        cases = (
            (b"C 0 0 0", "", "unit"),
            (b"C 0 0 0", "maybe nm", "unit"),
            (b"C 0 0 0\n\nH 1 0 0", "angstrom", "one geometry"),
            (b"C 0 0", "angstrom", "line 1"),
            (b"C secret-coordinate 0 0", "angstrom", "line 1"),
            (b"C NaN 0 0", "angstrom", "finite"),
        )
        for source, unit, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(input_geometry.InputRejected, reason) as caught:
                    input_geometry.prepare_table(source, source_unit=unit)
                self.assertNotIn("secret-coordinate", str(caught.exception))

    def test_source_limit_is_checked_before_text_decoding(self):
        with self.assertRaisesRegex(input_geometry.InputRejected, "byte limit"):
            input_geometry.prepare_table(b"\xff" * ((1 << 20) + 1), source_unit="angstrom")

    def test_imports_need_neither_rdkit_nor_torch_and_host_execution_is_guarded(self):
        code = (
            "import sys; sys.modules['torch'] = None; sys.modules['rdkit'] = None; "
            "from nmrnet_provider import input_geometry; "
            "input_geometry.prepare_table(b'C 0 0 0', source_unit='angstrom')"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        guarded = subprocess.run(
            [sys.executable, "-B", "-c", "import os; os.environ.pop('NMRNET_IN_CONTAINER'); " + code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, guarded.returncode)
        self.assertIn("maintained Make", guarded.stderr)
