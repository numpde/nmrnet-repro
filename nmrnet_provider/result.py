"""Freeze an ordered geometry collection's result contract before model execution.

Note 003 requires prepared coordinates, atom correspondence and reproduction
identity. Core treats these bytes as opaque. The API ceiling is derived from
nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/execution_attempt_complete_request.v1.schema.json:
1048576 canonical-base64 characters permit 786432 decoded bytes.

This planner does not grant execution or publication authority. The binding must
admit Job/Attempt identity, host execution identity and runtime/model selection,
then retain this plan with its exact prepared worker handle. This owner admits
one whole result; scientific batch execution is a separate worker responsibility.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import math
import re

from nmrnet_inference.predict import parse_request
from nmrnet_provider.input_geometry import PreparedGeometry, UploadMemberSource, UploadSource
from nmrnet_provider.prepared_input import (
    GeneratedJobInput,
    OutputSettings,
    PreparedJobInput,
    SelectedPastedSource,
    SelectedSmilesSource,
)
from nmrnet_provider.smiles import GeneratedConformer, GeneratedConformers

RESULT_SCHEMA_ID = "nmrnet.coordinate_shifts.result.v2"
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
GENERATED_LIMITS = (
    "Predictions use the selected single H/C checkpoints, without checkpoint or conformer averaging.",
    "Each returned conformer receives its own prediction; generated conformers are not guaranteed to be distinct.",
    "Hydrogens are retained or added according to the recorded SMILES graph expansion; completeness is not independently validated.",
    "Generated coordinates are estimates, not experimental structures. Coordinate-derived stereo is not independent validation.",
    "Only H and C atoms receive shifts; other atoms provide molecular context.",
    "Isotopes, formal charge and graph stereo are retained as provenance; they are not model input features.",
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


def _public_envelope(envelope):
    """Project the selected public view without changing the full frozen plan.

    This projects binding-admitted facts, not arbitrary worker fields. The same
    view reserves bytes before inference and serializes the final publication.
    """
    verbosity = envelope["output"]["verbosity"]
    if verbosity == "max":
        return envelope
    if verbosity not in ("low", "medium"):
        raise ResultRejected("Cannot select scientific output: this binding does not implement the requested verbosity.")
    public = {
        **envelope,
        "execution": {key: value for key, value in envelope["execution"].items() if key != "tool_sources"},
    }
    if verbosity == "low":
        public["geometries"] = [
            {key: value for key, value in geometry.items() if key not in ("prepared_request", "float32_coordinates")}
            for geometry in envelope["geometries"]
        ]
    return public


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
        prepared_input,
        *,
        job_ref,
        execution_attempt_ref,
        input_fingerprint,
        execution_identity,
        selected_artifacts,
        runtime_expectations,
    ):
        planned_input = _planned_input(prepared_input, job_ref, input_fingerprint)
        if type(prepared_input.output) is not OutputSettings:
            raise TypeError("Result planning requires the shared immutable output settings.")
        envelope = {
            "schema_id": RESULT_SCHEMA_ID,
            "job_ref": job_ref,
            "execution_attempt_ref": execution_attempt_ref,
            "input_fingerprint": input_fingerprint,
            "execution": execution_identity,
            "selected_models": selected_artifacts,
            "runtime": runtime_expectations,
            "output": prepared_input.output.as_dict(),
            **planned_input,
        }
        required_heads = {head for geometry in envelope["geometries"] for head in geometry["loaded_elements"]}
        if (
            not {"dictionary", "elements"} <= selected_artifacts.keys()
            or not required_heads <= selected_artifacts["elements"].keys()
        ):
            raise ValueError(
                "Cannot plan scientific results: required model selection is missing the dictionary or a required H/C head."
            )
        self._envelope = _encode(envelope)
        # A finite binary64 JSON number needs fewer than 32 ASCII characters.
        # Quoted placeholders reserve more than that, with all punctuation and
        # both heads' largest public notices included in the serialization.
        envelope["warnings"] = []
        for geometry in envelope["geometries"]:
            geometry["predictions"] = _prediction_reservation(geometry)
            envelope["warnings"].extend(_warning_reservation(geometry))
        self._maximum_result_bytes = len(_encode(_public_envelope(envelope)))
        if self._maximum_result_bytes > MAX_RESULT_BYTES:
            raise ResultRejected(
                "Cannot admit scientific result before inference: the selected published result exceeds the API byte limit."
            )

    @property
    def prepared_identity(self):
        """Return ordered input and runtime identities without coordinates or raw input text.

        Project the frozen admitted envelope rather than maintaining a second
        plan. The Attempt record already owns the original Job identity.
        """
        envelope = json.loads(self._envelope)
        identity = {
            "execution": envelope["execution"],
            "selected_models": envelope["selected_models"],
            "runtime": envelope["runtime"],
            "output": envelope["output"],
            "geometries": [_geometry_identity(geometry) for geometry in envelope["geometries"]],
        }
        if "generation" in envelope:
            identity["generation"] = {key: envelope["generation"][key] for key in ("provenance_sha256", "selection")}
        if "interpretation" in envelope:
            identity["interpretation"] = envelope["interpretation"]
        return _encode(identity)

    @property
    def maximum_result_bytes(self):
        """Expose the fixed reservation without letting callers change admission."""
        return self._maximum_result_bytes

    def scientific_evidence_reservation(self):
        """Reserve known unrounded evidence before prediction, not observations.

        The session adds its already-received diagnostics and applies core's
        existing evidence ceiling. Future private warning growth remains unknown.
        """
        envelope = json.loads(self._envelope)
        return tuple(
            {
                "request_sha256": geometry["request_sha256"],
                "prediction": {"unit": "ppm", "predictions": _prediction_reservation(geometry)},
                "provenance": {
                    "artifacts": _geometry_artifacts(geometry, envelope["selected_models"]),
                    "runtime": envelope["runtime"],
                },
                "private_warnings": [],
            }
            for geometry in envelope["geometries"]
        )

    def finish(self, worker_results):
        """Validate and encode the complete result, or raise without returning bytes.

        Publication belongs to the caller; this method performs no API mutation.
        """
        envelope = json.loads(self._envelope)
        geometries = envelope["geometries"]
        if type(worker_results) is not tuple or len(worker_results) != len(geometries):
            raise ResultRejected("Cannot admit scientific result: expected one ordered outcome for every prepared geometry.")
        envelope["warnings"] = []
        for geometry, outcome in zip(geometries, worker_results, strict=True):
            try:
                predictions, warnings = _finish_geometry(geometry, outcome, envelope["selected_models"], envelope["runtime"])
                for row in predictions:
                    # Python 3.12's g format rounds the admitted binary64 value
                    # nearest-even to significant digits. Numeric JSON cannot
                    # retain written trailing zeros; this is no accuracy claim.
                    # https://docs.python.org/3.12/library/string.html#format-specification-mini-language
                    rounded = float(format(row["shift_ppm"], f".{envelope['output']['shift_significant_digits']}g"))
                    if not math.isfinite(rounded):
                        raise ResultRejected("Cannot round the admitted shift to a finite binary64 output value.")
                    row["shift_ppm"] = 0.0 if rounded == 0 else rounded
            except ResultRejected as cause:
                raise ResultRejected(f"{geometry['geometry_id']}: {cause}") from cause
            geometry["predictions"] = predictions
            envelope["warnings"].extend(_located_warnings(geometry, warnings))
        content = _encode(_public_envelope(envelope))
        if len(content) > self._maximum_result_bytes:
            raise ResultRejected("Cannot admit scientific result: final bytes exceed the pre-execution capacity reservation.")
        return content


def _prediction_reservation(geometry):
    # Session evidence normalizes admitted exact integers to unrounded floats,
    # so the same finite binary64 bound covers both public and private rows.
    return [{"atom_index": index, "element": atom, "shift_ppm": "x" * 32} for index, atom in _positions(geometry)]


def _geometry_artifacts(geometry, selected_artifacts):
    return {
        "dictionary": selected_artifacts["dictionary"],
        "elements": {head: selected_artifacts["elements"][head] for head in geometry["loaded_elements"]},
    }


def _planned_input(prepared, job_ref, input_fingerprint):
    if type(prepared) is PreparedJobInput:
        planned = _planned_supplied(prepared, job_ref, input_fingerprint)
    elif type(prepared) is GeneratedJobInput:
        planned = _planned_generated_input(prepared, job_ref, input_fingerprint)
    else:
        raise TypeError("Result planning requires supplied geometries or generated conformers bound to their Job selection.")
    if prepared.interpretation is not None:
        if type(prepared.interpretation) is not bytes:
            raise TypeError("Result planning requires immutable interpreter observation bytes.")
        if len(prepared.interpretation) > MAX_RESULT_BYTES:
            raise ResultRejected("Cannot plan interpreter provenance: its observation alone exceeds the result byte limit.")
        try:
            # The trusted chooser serializes its own projection with json.dumps;
            # model replies are strictly decoded at their separate wire boundary.
            # Retain semantic observation fields, not original JSON byte identity.
            observation = json.loads(prepared.interpretation.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as cause:
            raise ResultRejected("Cannot plan interpreter provenance: its observation is not a UTF-8 JSON object.") from cause
        if type(observation) is not dict:
            raise ResultRejected("Cannot plan interpreter provenance: its observation must be a JSON object.")
        planned["interpretation"] = observation
    return planned


def _planned_generated_input(prepared, job_ref, input_fingerprint):
    source = _selected_source_identity(prepared.source, job_ref, input_fingerprint)
    generated = prepared.generated
    if type(generated) is not GeneratedConformers:
        raise TypeError("Generated Job input requires the native protocol's admitted conformer collection.")
    metadata = json.loads(generated.provenance)
    if (
        type(prepared.source.requested_count) is not int
        or source["requested_count"] != metadata["requested_count"]
        or source["source_sha256"] != metadata["source_sha256"]
    ):
        raise ResultRejected("Cannot plan generated results: the selection differs from the generated source or requested count.")
    if (
        type(generated.conformers) is not tuple
        or not generated.conformers
        or any(type(item) is not GeneratedConformer for item in generated.conformers)
        or len(generated.conformers) != source["requested_count"]
    ):
        raise ResultRejected("Cannot plan generated results: the complete requested conformer collection is required.")
    # Protocol admission owns graph/settings/atom correspondence. This owner
    # joins that evidence to the Job selection without repeating chemistry.
    return {
        "generation": {
            "provenance": metadata,
            "provenance_sha256": hashlib.sha256(generated.provenance).hexdigest(),
            "selection": source,
        },
        "scientific_limits": GENERATED_LIMITS,
        "geometries": [_planned_generated(item, index, metadata) for index, item in enumerate(generated.conformers, 1)],
    }


def _selected_source_identity(source, job_ref, input_fingerprint):
    """Join received SMILES to this Job without claiming extraction from prose."""
    if type(source) is not SelectedSmilesSource:
        raise TypeError("Generated Job input requires its selected SMILES source.")
    if source.job_ref != job_ref or source.input_fingerprint != input_fingerprint:
        raise ResultRejected("Cannot plan generated results: the selected source must match this Job and input fingerprint.")
    if type(source.source_bytes) is not bytes:
        raise ResultRejected("Cannot plan generated results: received SMILES must be immutable bytes.")
    return {
        "job_ref": source.job_ref,
        "input_fingerprint": source.input_fingerprint,
        "origin": "interpreter_supplied_smiles",
        "byte_length": len(source.source_bytes),
        "source_sha256": source.source_sha256,
        "requested_count": source.requested_count,
    }


def _planned_supplied(prepared, job_ref, input_fingerprint):
    geometries, method, source = prepared.geometries, prepared.preparation_method, prepared.source
    if type(geometries) is not tuple or not geometries or any(type(item) is not PreparedGeometry for item in geometries):
        raise TypeError("Result planning requires a nonempty tuple of prepared geometries.")
    if method not in {"direct_coordinate_json", "coordinate_table", "xyz", "mol", "sdf", "pdb", "mol2"}:
        raise ValueError("Result planning requires an implemented supplied-coordinate preparation method.")
    source_identity = {}
    if type(source) in (UploadSource, UploadMemberSource):
        upload = source.upload if type(source) is UploadMemberSource else source
        if type(upload) is not UploadSource:
            raise TypeError("Uploaded member provenance requires its verified outer Upload facts.")
        if upload.job_ref != job_ref or any(geometry.source_sha256 != source.sha256 for geometry in geometries):
            raise ValueError(
                "Cannot plan uploaded geometries: their verified source must belong to this Job and match every source digest."
            )
        source_identity = {"upload_ref": upload.upload_ref, "byte_length": source.byte_length}
        if type(source) is UploadMemberSource:
            if type(source.member_name) is not str or not source.member_name:
                raise ValueError("Uploaded member provenance requires an exact nonempty logical member name.")
            source_identity["archive"] = {
                "sha256": upload.sha256,
                "byte_length": upload.byte_length,
                "member_name": source.member_name,
            }
    elif type(source) is SelectedPastedSource:
        source_identity = _pasted_source_identity(source, geometries, method, job_ref, input_fingerprint)
    elif source is not None:
        raise TypeError("Supplied coordinates require an Upload or selected Job source, or no additional source binding.")
    return {
        "scientific_limits": SCIENTIFIC_LIMITS,
        "geometries": [_planned_geometry(item, index, method, source_identity) for index, item in enumerate(geometries, 1)],
    }


def _pasted_source_identity(source, geometries, method, job_ref, input_fingerprint):
    """Join received table bytes to reader facts without repeating conversion."""
    if source.job_ref != job_ref or source.input_fingerprint != input_fingerprint:
        raise ResultRejected("Cannot plan pasted coordinates: their selection must match this Job and input fingerprint.")
    if type(source.source_bytes) is not bytes:
        raise ResultRejected("Cannot plan pasted coordinates: received table must be immutable bytes.")
    if (
        len(geometries) != 1
        or source.source_format not in ("table", "xyz")
        or method != {"table": "coordinate_table", "xyz": "xyz"}[source.source_format]
        or geometries[0].source_sha256 != source.source_sha256
        or geometries[0].source_unit != source.source_unit
    ):
        raise ResultRejected(
            "Cannot plan pasted coordinates: exactly one geometry must match the selected source, format and unit."
        )
    return {
        "job_ref": source.job_ref,
        "input_fingerprint": source.input_fingerprint,
        "origin": "interpreter_supplied_coordinates",
        "byte_length": len(source.source_bytes),
        "format": source.source_format,
        "positions_relative_to": "received_source_text",
    }


def _planned_request(raw, index):
    request = parse_request(raw)
    return {
        "geometry_id": f"geometry-{index}",
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        # Low output still needs the complete atom order to identify prediction
        # indices and source/H-parent mappings when coordinate arrays are omitted.
        "atoms": request["atoms"],
        "prepared_request": json.loads(raw),
        "float32_coordinates": request["coordinates"],
        "loaded_elements": sorted(set(request["atoms"]) & {"H", "C"}),
        "prediction_unit": "ppm",
    }


def _planned_geometry(geometry, index, method, source_identity):
    planned = _planned_request(geometry.request_bytes, index)
    supplied_hydrogen_count = planned["prepared_request"]["atoms"].count("H")
    return {
        **planned,
        "source": {
            "sha256": geometry.source_sha256,
            "record_number": geometry.source_record_number,
            "position_kind": geometry.source_position_kind,
            "positions": geometry.source_positions,
            **source_identity,
        },
        "preparation": {
            "method": method,
            "source_unit": geometry.source_unit,
            "scale_to_angstrom": geometry.scale_to_angstrom,
            "conversion_reference": geometry.conversion_reference,
        },
        # finish() requires all supplied H/C rows and actual loaded heads before
        # publishing these counts. Coordinates establish no missing-H count.
        "coverage": {
            "basis": "supplied_coordinates",
            "supplied_hydrogen_atoms": supplied_hydrogen_count,
            "predicted_hydrogen_atoms": supplied_hydrogen_count,
            "hydrogen_completeness": "not_established_from_coordinates",
        },
    }


def _planned_generated(conformer, index, metadata):
    planned = _planned_request(conformer.request_bytes, index)
    hydrogen_count = planned["prepared_request"]["atoms"].count("H")
    return {
        **planned,
        "source": {"kind": "smiles", "sha256": metadata["source_sha256"], "conformer_id": conformer.conformer_id},
        "preparation": {
            "method": "smiles",
            "coordinate_unit": "angstrom",
            "stereochemistry": json.loads(conformer.stereochemistry),
            "stereochemistry_sha256": hashlib.sha256(conformer.stereochemistry).hexdigest(),
        },
        "coverage": {
            "basis": "smiles_graph_expansion",
            "source_explicit_hydrogen_atoms": sum(atom["element"] == "H" for atom in metadata["source_atoms"]),
            "added_hydrogen_atoms": len(metadata["added_hydrogen_parents"]),
            "coordinate_hydrogen_atoms": hydrogen_count,
            "predicted_hydrogen_atoms": hydrogen_count,
            "hydrogen_completeness": "not_independently_validated",
        },
    }


def _geometry_identity(geometry):
    return {
        **{key: geometry[key] for key in ("geometry_id", "request_sha256", "source")},
        "preparation": {key: value for key, value in geometry["preparation"].items() if key != "stereochemistry"},
        "required_elements": geometry["loaded_elements"],
    }


def _positions(geometry):
    return tuple((index, atom) for index, atom in enumerate(geometry["prepared_request"]["atoms"]) if atom in {"H", "C"})


def _located_warnings(geometry, warnings):
    """Associate each actual observation with its geometry, without merging loads."""
    return [{**warning, "geometry_id": geometry["geometry_id"]} for warning in warnings]


def _warning_reservation(geometry):
    """Reserve each required head's largest classified notice, including its owner."""
    heads = geometry["loaded_elements"]
    warnings = [
        {
            **SCALER_WARNING,
            "head": head,
            "versions": {"original": "9" * 32, "current": "9" * 32},
            "version_details": "unavailable_or_conflicting",
        }
        for head in heads
    ]
    if heads:
        warnings.append({**ADDITIONAL_WARNING, "heads": heads})
    return _located_warnings(geometry, warnings)


def _finish_geometry(geometry, worker_result, selected_artifacts, runtime):
    _shape(worker_result, ("request_sha256", "prediction", "provenance", "private_warnings"), "worker outcome")
    if worker_result["request_sha256"] != geometry["request_sha256"]:
        raise ResultRejected("Cannot admit scientific result: executed request identity differs from the prepared geometry.")
    provenance = worker_result["provenance"]
    _shape(provenance, ("artifacts", "runtime"), "worker provenance")
    expected_artifacts = _geometry_artifacts(geometry, selected_artifacts)
    if _encode(provenance["artifacts"]) != _encode(expected_artifacts):
        raise ResultRejected(
            "Cannot admit scientific result: loaded artifacts differ from the selected geometry's model requirements."
        )
    if _encode(provenance["runtime"]) != _encode(runtime):
        raise ResultRejected("Cannot admit scientific result: observed runtime metadata differs from the admitted runtime.")
    predictions = _predictions(worker_result["prediction"], _positions(geometry))
    public_warnings = _public_warnings(worker_result["private_warnings"], set(geometry["loaded_elements"]))
    return predictions, public_warnings


def _predictions(prediction, positions):
    _shape(prediction, ("unit", "predictions"), "predictions")
    rows = prediction["predictions"]
    if prediction["unit"] != "ppm" or type(rows) is not list or len(rows) != len(positions):
        raise ResultRejected("Cannot admit scientific result: expected one ppm prediction for every eligible H/C atom.")
    admitted = []
    for row, (index, atom) in zip(rows, positions, strict=True):
        _shape(row, ("atom_index", "element", "shift_ppm"), "prediction row")
        if type(row["atom_index"]) is not int or (row["atom_index"], row["element"]) != (index, atom):
            raise ResultRejected("Cannot admit scientific result: predictions do not match the prepared atom order and elements.")
        value = row["shift_ppm"]
        if type(value) not in (int, float):
            raise ResultRejected("Cannot admit scientific result: each shift must be a finite number.")
        try:
            number = float(value)
        except OverflowError:
            raise ResultRejected("Cannot admit scientific result: a shift exceeds finite floating-point capacity.") from None
        if not math.isfinite(number) or number != value:
            raise ResultRejected("Cannot admit scientific result: a shift is nonfinite or not exactly representable as binary64.")
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
