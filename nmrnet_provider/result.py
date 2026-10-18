"""Freeze one geometry's scientific result contract before model execution.

Note 003 requires prepared coordinates, atom correspondence and reproduction
identity. Core treats these bytes as opaque. The API ceiling is derived from
nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/execution_attempt_complete_request.v1.schema.json:
1048576 canonical-base64 characters permit 786432 decoded bytes.

This planner does not grant execution or publication authority. The binding must
admit Job/Attempt identity, host execution identity and runtime/model selection,
then retain this plan with its exact prepared worker handle. Only one geometry is
enabled here; ordered batch execution still needs whole-batch preparation.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import math

from nmrnet_inference.predict import parse_request
from nmrnet_provider.input_geometry import PreparedGeometry

RESULT_SCHEMA_ID = "nmrnet.coordinate_shifts.result.v1"
MAX_RESULT_BYTES = 786432
SCALER_WARNING = {
    "code": "scaler_version_mismatch",
    "message": "The released scaler was loaded by a different scikit-learn version. Predictions are qualified only in the recorded runtime.",
}
SCIENTIFIC_LIMITS = (
    "Predictions use the selected single H/C checkpoints, without checkpoint or conformer averaging.",
    "Only H and C atoms receive shifts; other supplied atoms provide molecular context.",
    "Coordinates affect predictions. No experimental spectrum, confidence interval or bond assignment is inferred.",
    "Float32 coordinates describe admitted numeric inputs; model centering and padding occur later.",
)


class ResultRejected(ValueError):
    """A safe result-admission reason; the caller retains the private worker outcome."""


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as cause:
        raise ResultRejected("Cannot admit scientific result: a field is not finite JSON data.") from cause


def _shape(value, fields, subject):
    if type(value) is not dict or set(value) != set(fields):
        raise ResultRejected(f"Cannot admit scientific result: {subject} has an unexpected shape.")


class ResultPlan:
    """Immutable serialized invariants, with a pre-execution worst-case byte bound.

    Configuration arguments are binding-admitted facts, not untrusted protocol
    parsing inputs. Worker outcomes are checked here against those frozen facts.
    Raw warning evidence remains with the caller; only classified receiver text
    enters the public result. Unknown warnings prevent publication for review.
    """

    def __init__(
        self,
        geometry,
        *,
        preparation_method,
        job_ref,
        execution_attempt_ref,
        input_fingerprint,
        execution_identity,
        selected_artifacts,
        runtime_expectations,
    ):
        if type(geometry) is not PreparedGeometry:
            raise TypeError("Result planning requires a prepared geometry.")
        if preparation_method not in {"direct_coordinate_json", "coordinate_table", "xyz"}:
            raise ValueError("Result planning requires an implemented supplied-coordinate preparation method.")
        request = parse_request(geometry.request_bytes)
        self._positions = tuple((i, atom) for i, atom in enumerate(request["atoms"]) if atom in {"H", "C"})
        self._request_sha256 = hashlib.sha256(geometry.request_bytes).hexdigest()
        self._artifacts = _encode(
            {
                "dictionary": selected_artifacts["dictionary"],
                "elements": {
                    head: selected_artifacts["elements"][head] for head in sorted({atom for _, atom in self._positions})
                },
            }
        )
        self._runtime = _encode(runtime_expectations)
        envelope = {
            "schema_id": RESULT_SCHEMA_ID,
            "job_ref": job_ref,
            "execution_attempt_ref": execution_attempt_ref,
            "input_fingerprint": input_fingerprint,
            "execution": execution_identity,
            "selected_models": selected_artifacts,
            "runtime": runtime_expectations,
            "scientific_limits": SCIENTIFIC_LIMITS,
            "geometries": [
                {
                    "geometry_id": "geometry-1",
                    "request_sha256": self._request_sha256,
                    "source": {
                        "sha256": geometry.source_sha256,
                        "record_number": geometry.source_record_number,
                        "position_kind": geometry.source_position_kind,
                        "positions": geometry.source_positions,
                    },
                    "preparation": {
                        "method": preparation_method,
                        "source_unit": geometry.source_unit,
                        "scale_to_angstrom": geometry.scale_to_angstrom,
                        "conversion_reference": geometry.conversion_reference,
                    },
                    "prepared_request": json.loads(geometry.request_bytes),
                    "float32_coordinates": request["coordinates"],
                    "loaded_elements": sorted({atom for _, atom in self._positions}),
                    "prediction_unit": "ppm",
                }
            ],
        }
        self._envelope = _encode(envelope)
        # A finite binary64 JSON number needs fewer than 32 ASCII characters.
        # Quoted placeholders reserve more than that, with all punctuation and
        # the only admitted public warning included in the actual serialization.
        envelope["geometries"][0]["predictions"] = [
            {"atom_index": i, "element": atom, "shift_ppm": "x" * 32} for i, atom in self._positions
        ]
        envelope["warnings"] = [SCALER_WARNING]
        self.maximum_result_bytes = len(_encode(envelope))
        if self.maximum_result_bytes > MAX_RESULT_BYTES:
            raise ResultRejected(
                "Cannot admit scientific result before inference: its complete reproduction envelope exceeds the API byte limit."
            )

    def finish(self, worker_result):
        """Return complete result bytes, or refuse without a partial publication."""
        _shape(worker_result, ("request_sha256", "prediction", "provenance", "private_warnings"), "worker outcome")
        if worker_result["request_sha256"] != self._request_sha256:
            raise ResultRejected("Cannot admit scientific result: executed request identity differs from the prepared geometry.")
        provenance = worker_result["provenance"]
        _shape(provenance, ("artifacts", "runtime"), "worker provenance")
        if _encode(provenance["artifacts"]) != self._artifacts:
            raise ResultRejected(
                "Cannot admit scientific result: loaded artifacts differ from the selected geometry's model requirements."
            )
        if _encode(provenance["runtime"]) != self._runtime:
            raise ResultRejected("Cannot admit scientific result: observed runtime metadata differs from the admitted runtime.")
        predictions = self._predictions(worker_result["prediction"])
        public_warnings = _public_warnings(worker_result["private_warnings"])
        envelope = json.loads(self._envelope)
        envelope["geometries"][0]["predictions"] = predictions
        envelope["warnings"] = public_warnings
        content = _encode(envelope)
        if len(content) > self.maximum_result_bytes:
            raise ResultRejected("Cannot admit scientific result: final bytes exceed the pre-execution capacity reservation.")
        return content

    def _predictions(self, prediction):
        _shape(prediction, ("unit", "predictions"), "predictions")
        rows = prediction["predictions"]
        if prediction["unit"] != "ppm" or type(rows) is not list or len(rows) != len(self._positions):
            raise ResultRejected("Cannot admit scientific result: expected one ppm prediction for every eligible H/C atom.")
        admitted = []
        for row, (index, atom) in zip(rows, self._positions, strict=True):
            _shape(row, ("atom_index", "element", "shift_ppm"), "prediction row")
            if type(row["atom_index"]) is not int or (row["atom_index"], row["element"]) != (index, atom):
                raise ResultRejected(
                    "Cannot admit scientific result: predictions do not match the prepared atom order and elements."
                )
            value = row["shift_ppm"]
            if type(value) not in (int, float):
                raise ResultRejected("Cannot admit scientific result: each shift must be a finite number.")
            try:
                number = float(value)
            except OverflowError:
                raise ResultRejected("Cannot admit scientific result: a shift exceeds finite floating-point capacity.") from None
            if not math.isfinite(number) or number != value:
                raise ResultRejected(
                    "Cannot admit scientific result: a shift is nonfinite or not exactly representable as binary64."
                )
            admitted.append({"atom_index": index, "element": atom, "shift_ppm": number})
        return admitted


def _public_warnings(observed):
    if type(observed) is not list:
        raise ResultRejected("Cannot admit scientific result: worker warnings must be a list.")
    for warning in observed:
        _shape(warning, ("category", "message", "filename", "lineno"), "private warning")
        if warning["category"] != "sklearn.exceptions.InconsistentVersionWarning":
            raise ResultRejected(
                "Cannot admit scientific result: an unclassified warning requires review of the protected worker evidence."
            )
        if type(warning["message"]) is not str or type(warning["filename"]) is not str or type(warning["lineno"]) is not int:
            raise ResultRejected("Cannot admit scientific result: private warning evidence has malformed fields.")
    return [SCALER_WARNING] if observed else []
