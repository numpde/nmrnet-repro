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
import re

from nmrnet_inference.predict import parse_request
from nmrnet_provider.input_geometry import PreparedGeometry

RESULT_SCHEMA_ID = "nmrnet.coordinate_shifts.result.v1"
MAX_RESULT_BYTES = 786432
SCALER_WARNING = {
    "code": "scaler_version_mismatch",
    "message": "The released scaler was loaded by a different scikit-learn version. Predictions are qualified only in the recorded runtime.",
}
ADDITIONAL_WARNING = {
    "code": "additional_scaler_warnings",
    "message": "Additional warnings occurred while loading the listed scalers. Their details are retained in protected diagnostics for review.",
}
SCIENTIFIC_LIMITS = (
    "Predictions use the selected single H/C checkpoints, without checkpoint or conformer averaging.",
    "Only H and C atoms receive shifts; other supplied atoms provide molecular context.",
    "Hydrogen shifts are returned only for H atoms present in the supplied coordinates. Missing or implicit hydrogens are not added.",
    "Coordinates alone do not establish whether hydrogens are missing, partially supplied or genuinely absent.",
    "Omitted atoms can also affect shifts for the supplied atoms by changing the model's molecular context.",
    "If hydrogens were omitted, this is not a complete molecular or proton result. Supply their intended 3D coordinates; "
    "completing or regenerating geometry requires a separately authorized preparation step.",
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
    enters the public result. A warning alone does not invalidate predictions;
    decoder failures and structural/numerical admission still reject results.
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
        supplied_hydrogen_count = request["atoms"].count("H")
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
                    # finish() requires every supplied H/C row before these
                    # counts can reach a receiver. They establish no missing-H
                    # count: coordinate-only inputs carry no molecular graph.
                    "coverage": {
                        "basis": "supplied_coordinates",
                        "supplied_hydrogen_atoms": supplied_hydrogen_count,
                        "predicted_hydrogen_atoms": supplied_hydrogen_count,
                        "hydrogen_completeness": "not_established_from_coordinates",
                    },
                }
            ],
        }
        self._envelope = _encode(envelope)
        # A finite binary64 JSON number needs fewer than 32 ASCII characters.
        # Quoted placeholders reserve more than that, with all punctuation and
        # both heads' largest public notices included in the serialization.
        envelope["geometries"][0]["predictions"] = [
            {"atom_index": i, "element": atom, "shift_ppm": "x" * 32} for i, atom in self._positions
        ]
        envelope["warnings"] = [
            {
                **SCALER_WARNING,
                "head": head,
                "versions": {"original": "9" * 32, "current": "9" * 32},
                "version_details": "unavailable_or_conflicting",
            }
            for head in ("C", "H")
        ] + [{**ADDITIONAL_WARNING, "heads": ["C", "H"]}]
        self._maximum_result_bytes = len(_encode(envelope))
        if self._maximum_result_bytes > MAX_RESULT_BYTES:
            raise ResultRejected(
                "Cannot admit scientific result before inference: its complete reproduction envelope exceeds the API byte limit."
            )

    @property
    def prepared_identity(self):
        """Return ordered input and runtime identities without coordinates or raw input text.

        Project the frozen admitted envelope rather than maintaining a second
        plan. The Attempt record already owns the original Job identity.
        """
        envelope = json.loads(self._envelope)
        return _encode(
            {
                "execution": envelope["execution"],
                "selected_models": envelope["selected_models"],
                "runtime": envelope["runtime"],
                "geometries": [
                    {key: geometry[key] for key in ("geometry_id", "request_sha256", "source", "preparation")}
                    for geometry in envelope["geometries"]
                ],
            }
        )

    @property
    def maximum_result_bytes(self):
        """Expose the fixed reservation without letting callers change admission."""
        return self._maximum_result_bytes

    def finish(self, worker_result):
        """Validate and encode the complete result, or raise without returning bytes.

        Publication belongs to the caller; this method performs no API mutation.
        """
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
        public_warnings = _public_warnings(worker_result["private_warnings"], {atom for _, atom in self._positions})
        envelope = json.loads(self._envelope)
        envelope["geometries"][0]["predictions"] = predictions
        envelope["warnings"] = public_warnings
        content = _encode(envelope)
        if len(content) > self._maximum_result_bytes:
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


def _public_warnings(observed, loaded_heads):
    if type(observed) is not list:
        raise ResultRejected("Cannot admit scientific result: worker warnings must be a list.")
    compatibility = {}
    additional = set()
    for warning in observed:
        _shape(warning, ("category", "message", "filename", "lineno", "head", "versions"), "private warning")
        if (
            type(warning["category"]) is not str
            or type(warning["message"]) is not str
            or type(warning["filename"]) is not str
            or type(warning["lineno"]) is not int
            or warning["head"] not in ("H", "C")
        ):
            raise ResultRejected("Cannot admit scientific result: private warning evidence has malformed fields.")
        head = warning["head"]
        if head not in loaded_heads:
            raise ResultRejected("Cannot admit scientific result: warning names a scaler that was not loaded for this geometry.")
        if warning["category"] != "sklearn.exceptions.InconsistentVersionWarning":
            additional.add(head)
            continue
        versions = _public_versions(warning["versions"])
        if head in compatibility and compatibility[head] != versions:
            compatibility[head] = None
        else:
            compatibility[head] = versions
    result = [
        {
            **SCALER_WARNING,
            "head": head,
            "versions": versions,
            "version_details": "available" if versions is not None else "unavailable_or_conflicting",
        }
        for head, versions in sorted(compatibility.items())
    ]
    if additional:
        result.append({**ADDITIONAL_WARNING, "heads": sorted(additional)})
    return result


def _public_versions(versions):
    """Project bounded release identifiers, never arbitrary warning attributes.

    Missing, unsupported or conflicting version details remain private; the
    receiver still gets the compatibility warning and an explicit limitation.
    sklearn uses pre-0.18 when the serialized estimator has no version field.
    """
    if versions is None:
        return None
    _shape(versions, ("original", "current"), "scaler version evidence")
    for value in versions.values():
        if type(value) is not str or len(value) > 32:
            return None
        if value != "pre-0.18" and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:(?:a|b|rc|\.post|\.dev)[0-9]+)?", value) is None:
            return None
    return dict(versions)
