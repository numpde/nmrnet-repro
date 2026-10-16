"""Prove bounded, dependency-light admission of one coordinate request."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from nmrnet_inference import predict


def encoded_request(**changes):
    request = {
        "coordinate_unit": "angstrom",
        "atoms": ["C", "H"],
        "coordinates": [[0.0, 0.0, 0.0], [1.09, 0.0, 0.0]],
    }
    request.update(changes)
    return json.dumps(request, separators=(",", ":")).encode()


class GrowingInput(io.BytesIO):
    def __init__(self, initial, appended):
        self.available = bytearray(initial)
        self.appended = appended
        self.calls = 0
        self.requested = []
        self.consumed = 0

    def read(self, size=-1):
        self.calls += 1
        self.requested.append(size)
        if self.calls == 2:
            self.available.extend(self.appended)
        read_size = min(size, 7 if self.calls == 2 else len(self.available))
        chunk = bytes(self.available[:read_size])
        del self.available[:read_size]
        self.consumed += len(chunk)
        return chunk


class FailingInput:
    def __init__(self):
        self.calls = 0

    def read(self, _size):
        self.calls += 1
        if self.calls == 1:
            return b"{"
        raise OSError("injected input read failure")


class TemporarilyUnavailableInput:
    def read(self, _size):
        return None


class PredictionInputTests(unittest.TestCase):
    def parse(self, raw):
        return predict.parse_request(raw)

    def test_valid_request_identity_and_parsed_coordinates_share_exact_bytes(self):
        raw = encoded_request()
        retained, identity = predict.read_request_bytes(io.BytesIO(raw))
        parsed = self.parse(retained)
        self.assertEqual(raw, retained)
        self.assertEqual({"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, identity)
        self.assertEqual(["C", "H"], parsed["atoms"])
        self.assertEqual(1.090000033378601, parsed["coordinates"][1][0])

    def test_input_larger_than_limit_is_rejected_after_only_one_extra_byte(self):
        source = io.BytesIO(b"x" * (predict.MAX_INPUT_BYTES + 100))
        with self.assertRaisesRegex(ValueError, "1 MiB byte limit"):
            predict.read_request_bytes(source)
        self.assertEqual(predict.MAX_INPUT_BYTES + 1, source.tell())

    def test_input_exactly_at_limit_is_accepted_and_identified(self):
        source = io.BytesIO(b"x" * predict.MAX_INPUT_BYTES)
        raw, identity = predict.read_request_bytes(source)
        self.assertEqual(predict.MAX_INPUT_BYTES, len(raw))
        self.assertEqual(predict.MAX_INPUT_BYTES, identity["size_bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), identity["sha256"])

    def test_input_that_grows_while_being_read_stops_at_limit_plus_one(self):
        initial = b" " * 3
        source = GrowingInput(initial, b"x" * (predict.MAX_INPUT_BYTES + 10))
        with self.assertRaisesRegex(ValueError, "1 MiB byte limit"):
            predict.read_request_bytes(source)
        self.assertEqual(predict.MAX_INPUT_BYTES + 1, source.consumed)
        self.assertTrue(all(0 < size <= predict.MAX_INPUT_BYTES + 1 for size in source.requested))
        self.assertGreater(source.calls, 2)

    def test_read_failure_remains_an_operational_error(self):
        with self.assertRaisesRegex(OSError, "injected input read failure"):
            predict.read_request_bytes(FailingInput())

    def test_temporary_unavailability_is_not_treated_as_end_of_input(self):
        with self.assertRaisesRegex(BlockingIOError, "source is not ready"):
            predict.read_request_bytes(TemporarilyUnavailableInput())

    def test_bad_json_and_duplicate_keys_are_rejected_without_echoing_input(self):
        for raw in (b"not-json", b'{"coordinate_unit":"angstrom","atoms":["C"],"atoms":["H"],"coordinates":[[0,0,0]]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as raised:
                self.parse(raw)
            self.assertNotIn(raw.decode(errors="replace"), str(raised.exception))

    def test_excessive_json_nesting_has_an_input_diagnostic(self):
        raw = b"[" * 10000 + b"0" + b"]" * 10000
        with self.assertRaisesRegex(ValueError, "JSON nesting exceeds the supported depth"):
            self.parse(raw)

    def test_integer_digit_limit_has_an_input_diagnostic(self):
        raw = (
            b'{"coordinate_unit":"angstrom","atoms":["C"],"coordinates":[['
            + b"9" * 5000
            + b",0,0]]}"
        )
        with self.assertRaisesRegex(ValueError, "integer literal exceeds the supported digit limit"):
            self.parse(raw)

    def test_request_requires_exact_fields_and_nonempty_unit_text(self):
        valid = json.loads(encoded_request())
        cases = (
            ({"atoms": valid["atoms"], "coordinates": valid["coordinates"]}, "coordinate_unit"),
            ({**valid, "labels": [1.0]}, "only coordinate_unit"),
            ({**valid, "coordinate_unit": ""}, "coordinate_unit"),
            ([valid], "top-level JSON value"),
        )
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                self.parse(json.dumps(value).encode())

    def test_atom_count_and_coordinate_rows_are_bounded_before_conversion(self):
        for atoms in ([], ["C"] * (predict.MAX_ATOMS + 1)):
            with self.subTest(atom_count=len(atoms)), self.assertRaisesRegex(ValueError, "1 through 128"):
                self.parse(encoded_request(atoms=atoms, coordinates=[[0, 0, 0]] * len(atoms)))
        for atoms, coordinates in ((["C"], []), (["C"], [[0, 0]]), (["C"], [[0, 0, 0, 0]])):
            with self.subTest(coordinates=coordinates), self.assertRaises(ValueError):
                self.parse(encoded_request(atoms=atoms, coordinates=coordinates))

    def test_special_token_spellings_booleans_nonfinite_and_float32_overflow_reject(self):
        for atom in ("[MASK]", "<s>"):
            with self.subTest(atom=atom), self.assertRaisesRegex(ValueError, "element-symbol"):
                self.parse(encoded_request(atoms=[atom], coordinates=[[0, 0, 0]]))
        for coordinate in (True, float("nan"), float("inf"), 3.5e38, 10**1000):
            with self.subTest(coordinate=type(coordinate).__name__), self.assertRaises(ValueError):
                self.parse(encoded_request(atoms=["C"], coordinates=[[coordinate, 0, 0]]))
        for raw in (b"NaN", b"Infinity", b"-Infinity", b"1e400"):
            invalid = encoded_request().replace(b"0.0", raw, 1)
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "finite"):
                self.parse(invalid)

    def test_fresh_import_and_admission_need_no_torch_or_unicore(self):
        script = """
import sys
class BlockScientificImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'torch' or fullname.startswith('torch.') or fullname == 'unicore' or fullname.startswith('unicore.'):
            raise AssertionError('scientific dependency imported during request admission: ' + fullname)
sys.meta_path.insert(0, BlockScientificImports())
from nmrnet_inference.predict import parse_request
parse_request(b'{"coordinate_unit":"angstrom","atoms":["C"],"coordinates":[[0,0,0]]}')
"""
        environment = os.environ.copy()
        environment.pop("NMRNET_IN_CONTAINER", None)
        result = subprocess.run([sys.executable, "-c", script], cwd=Path.cwd(), env=environment, text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("maintained Make target", result.stderr)

        environment["NMRNET_IN_CONTAINER"] = "1"
        result = subprocess.run([sys.executable, "-c", script], cwd=Path.cwd(), env=environment, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
