"""Prove bounded, dependency-light admission of one coordinate request."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import os
import subprocess
import sys
import traceback
import unittest
from pathlib import Path
from unittest import mock

from nmrnet_inference import predict
from nmrnet_inference import model as prediction_model


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

    def test_parser_preserves_caller_unit_for_model_admission(self):
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "nm", "C": "nm"}):
            parsed = self.parse(encoded_request(coordinate_unit="nm"))

        # Parsing preserves the declared unit and does not rescale coordinates;
        # request admission checks it against both qualified model heads.
        self.assertEqual("nm", parsed["coordinate_unit"])
        self.assertEqual(1.090000033378601, parsed["coordinates"][1][0])

    def test_request_admission_requires_units_for_every_present_model_head(self):
        cases = (
            (["H"], {"C": "angstrom"}, "H"),
            (["C"], {"H": "angstrom"}, "C"),
            (["C", "H"], {"H": "angstrom"}, "C"),
        )
        for atoms, qualified_units, unqualified_element in cases:
            raw = encoded_request(
                coordinate_unit="angstrom",
                atoms=atoms,
                coordinates=[[0.0, 0.0, 0.0]] * len(atoms),
            )
            with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, qualified_units):
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"{unqualified_element} prediction: model coordinate unit has not been qualified",
                ):
                    self.parse(raw)

        raw = encoded_request(coordinate_unit="angstrom")
        with mock.patch.dict(
            predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "angstrom", "C": "angstrom"}
        ):
            self.assertEqual("angstrom", self.parse(raw)["coordinate_unit"])

        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "nm", "C": "nm"}):
            with self.assertRaisesRegex(ValueError, "coordinate_unit does not match the qualified model unit"):
                self.parse(raw)

    def test_context_atoms_do_not_select_heads_or_hide_a_present_head(self):
        context_only = encoded_request(atoms=["N"], coordinates=[[0.0, 0.0, 0.0]])
        self.assertEqual(["N"], self.parse(context_only)["atoms"])

        carbon_with_context = encoded_request(
            atoms=["C", "N"], coordinates=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        )
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "angstrom"}):
            with self.assertRaisesRegex(RuntimeError, "C prediction: model coordinate unit has not been qualified"):
                self.parse(carbon_with_context)

    def test_different_head_units_only_admit_matching_single_head_requests(self):
        with mock.patch.dict(
            predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "angstrom", "C": "nm"}
        ):
            hydrogen = encoded_request(
                coordinate_unit="angstrom", atoms=["H"], coordinates=[[0.0, 0.0, 0.0]]
            )
            carbon = encoded_request(
                coordinate_unit="nm", atoms=["C"], coordinates=[[0.0, 0.0, 0.0]]
            )
            self.assertEqual("angstrom", self.parse(hydrogen)["coordinate_unit"])
            self.assertEqual("nm", self.parse(carbon)["coordinate_unit"])

            combined = encoded_request(coordinate_unit="angstrom")
            with self.assertRaisesRegex(
                ValueError, "C prediction: coordinate_unit does not match the qualified model unit"
            ):
                self.parse(combined)

    def test_unqualified_model_unit_blocks_tensor_composition(self):
        request = {"coordinate_unit": "angstrom"}
        self.assertEqual({"H": None, "C": None}, predict.QUALIFIED_MODEL_COORDINATE_UNITS)
        for element in ("H", "C"):
            with self.subTest(element=element), self.assertRaisesRegex(
                RuntimeError, f"{element} prediction: model coordinate unit has not been qualified"
            ):
                prediction_model.compose_net_input(request, object(), element)

    def test_composition_requires_exact_qualified_unit_and_allows_any_unit_label(self):
        request = {"coordinate_unit": "nm", "atoms": [], "coordinates": []}
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "nm"}):
            with self.assertRaisesRegex(ValueError, "1 through 128 symbols"):
                prediction_model.compose_net_input(request, object(), "H")
            with self.assertRaisesRegex(RuntimeError, "C prediction: model coordinate unit has not been qualified"):
                prediction_model.compose_net_input(request, object(), "C")

            request["coordinate_unit"] = "angstrom"
            with self.assertRaisesRegex(
                ValueError, "H prediction: coordinate_unit does not match the qualified model unit"
            ):
                prediction_model.compose_net_input(request, object(), "H")

    def test_valid_request_identity_and_parsed_coordinates_share_exact_bytes(self):
        raw = encoded_request()
        retained, identity = predict.read_request_bytes(io.BytesIO(raw))
        with mock.patch.dict(
            predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "angstrom", "C": "angstrom"}
        ):
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

    def test_json_syntax_error_reports_location_without_echoing_request(self):
        private_value = b"PRIVATE_JSON_VALUE_87d1"
        raw = b'{"private":"' + private_value + b'", invalid}'
        with self.assertRaisesRegex(ValueError, "JSON syntax is invalid at line") as raised:
            self.parse(raw)

        error = raised.exception
        rendered = "".join(traceback.format_exception(error))
        expected_column = raw.index(b"invalid") + 1
        self.assertIn(f"line 1, column {expected_column}:", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(private_value.decode(), rendered)

    def test_invalid_utf8_reports_byte_position_without_echoing_request(self):
        private_value = b"PRIVATE_UTF8_VALUE_1c9f"
        raw = b'{"private":"' + private_value + b'","value":"\xff"}'
        with self.assertRaisesRegex(ValueError, "not UTF-8 encoded JSON") as raised:
            self.parse(raw)

        error = raised.exception
        rendered = "".join(traceback.format_exception(error))
        expected_position = raw.index(b"\xff")
        self.assertIn(f"invalid byte at position {expected_position}.", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(private_value.decode(), rendered)

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
from nmrnet_inference.predict import QUALIFIED_MODEL_COORDINATE_UNITS
QUALIFIED_MODEL_COORDINATE_UNITS['C'] = 'angstrom'
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

    def test_request_unit_gate_rejects_before_any_model_artifact_load(self):
        load_element = mock.Mock()
        cases = (
            {"H": None, "C": None},
            {"H": "angstrom", "C": None},
            {"H": None, "C": "angstrom"},
        )
        for units in cases:
            with self.subTest(units=units), mock.patch.dict(
                predict.QUALIFIED_MODEL_COORDINATE_UNITS, units
            ):
                with self.assertRaisesRegex(RuntimeError, "has not been qualified"):
                    predict.predict_request(encoded_request(), load_element)
        load_element.assert_not_called()

    def test_context_only_request_does_not_load_an_absent_prediction_head(self):
        load_element = mock.Mock()
        raw = encoded_request(atoms=["O"], coordinates=[[0.0, 0.0, 0.0]])

        result = predict.predict_request(raw, load_element)

        self.assertEqual({"unit": "ppm", "predictions": []}, result)
        load_element.assert_not_called()

    def test_present_heads_load_sequentially_and_merge_by_original_atom_index(self):
        raw = encoded_request(
            atoms=["H", "C", "O", "H"],
            coordinates=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
        )
        events = []

        def load_element(element):
            events.append(("load", element))
            return (element, f"{element}-dictionary", f"{element}-scaler")

        def run_element(request, element, model, dictionary, scaler):
            events.append(("predict", element))
            self.assertEqual((element, f"{element}-dictionary", f"{element}-scaler"), (model, dictionary, scaler))
            indices = tuple(index for index, atom in enumerate(request["atoms"]) if atom == element)
            return tuple((index, float(index + 1)) for index in indices)

        with (
            mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "angstrom", "C": "angstrom"}),
            mock.patch.object(prediction_model, "predict_element", side_effect=run_element),
        ):
            result = predict.predict_request(raw, load_element)

        self.assertEqual(
            [
                {"atom_index": 0, "element": "H", "shift_ppm": 1.0},
                {"atom_index": 1, "element": "C", "shift_ppm": 2.0},
                {"atom_index": 3, "element": "H", "shift_ppm": 4.0},
            ],
            result["predictions"],
        )
        self.assertEqual([("load", "H"), ("predict", "H"), ("load", "C"), ("predict", "C")], events)

    def test_later_head_failure_does_not_return_earlier_head_predictions(self):
        raw = encoded_request()
        calls = []

        def load_element(element):
            calls.append(("load", element))
            return (element, object(), object())

        def run_element(_request, element, _model, _dictionary, _scaler):
            calls.append(("predict", element))
            if element == "C":
                raise RuntimeError("injected C model failure")
            return ((1, 2.0),)

        with (
            mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": "angstrom", "C": "angstrom"}),
            mock.patch.object(prediction_model, "predict_element", side_effect=run_element),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected C model failure"):
                predict.predict_request(raw, load_element)

        self.assertEqual([("load", "H"), ("predict", "H"), ("load", "C"), ("predict", "C")], calls)


if __name__ == "__main__":
    unittest.main()
