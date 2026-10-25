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

    def test_json_converts_declared_units_before_float32_model_admission(self):
        for unit, values, expected, factor in (
            ("nm", [0.100000001, -0.25, 1.5], [1.00000001, -2.5, 15.0], 10.0),
            ("bohr", [1, -1, 0], [0.529177210544, -0.529177210544, 0], 0.529177210544),
        ):
            source = json.dumps({"coordinate_unit": unit, "atoms": ["C"], "coordinates": [values]}).encode()
            with self.subTest(unit=unit):
                geometry = input_geometry.prepare_json(source)
                request = json.loads(geometry.request_bytes)
                self.assertEqual(request, {"coordinate_unit": "angstrom", "atoms": ["C"], "coordinates": [expected]})
                self.assertEqual(geometry.source_sha256, hashlib.sha256(source).hexdigest())
                self.assertEqual(geometry.source_positions, (0,))
                self.assertEqual((geometry.source_unit, geometry.scale_to_angstrom), (unit, factor))

    def test_json_file_unit_is_required_and_cannot_be_overridden(self):
        source = b'{"coordinate_unit":"nm","atoms":["C"],"coordinates":[[0.1,0,0]]}'
        self.assertEqual(input_geometry.prepare_json(source).request_bytes,
                         input_geometry.prepare_json(source, source_unit="nm").request_bytes)
        with self.assertRaisesRegex(input_geometry.InputRejected, "conflict"):
            input_geometry.prepare_json(source, source_unit="angstrom")
        for unit in (None, "unknown", ""):
            document = {"atoms": ["C"], "coordinates": [[0, 0, 0]]}
            if unit is not None:
                document["coordinate_unit"] = unit
            with self.subTest(unit=unit), self.assertRaises(input_geometry.InputRejected):
                input_geometry.prepare_json(json.dumps(document).encode())

    def test_json_float32_range_is_checked_after_source_conversion(self):
        source = {"coordinate_unit": "bohr", "atoms": ["C"], "coordinates": [[4e38, 0, 0]]}
        prepared = input_geometry.prepare_json(json.dumps(source).encode())
        self.assertEqual(json.loads(prepared.request_bytes)["coordinates"][0][0], 4e38 * 0.529177210544)
        source.update(coordinate_unit="nm", coordinates=[[4e37, 0, 0]])
        with self.assertRaisesRegex(input_geometry.InputRejected, "float32"):
            input_geometry.prepare_json(json.dumps(source).encode())

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
