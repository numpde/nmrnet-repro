"""Synthetic documents for causal qualification comparison tests; no science runs."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import struct
from pathlib import Path
from unittest.mock import patch

from api_prediction import Client, canonicalize_job_create_request_body
from nmr_api.security.user_ids import UserAuthorityRefs


def scientific_documents():
    receipt = {"job_ref": "job:new", "specification_fingerprint": "sha256:" + "a" * 64}
    metadata = {"execution_attempt_ref": "execution_attempt:new"}
    interpreter = {"configuration_id": "selected-fixture", "model": "selected-model", "configuration_sha256": "b" * 64}
    source_text = "H 0.1027 0 -0.0363\nCl 0 0 0.178\nC 0 0 0\nH -0.0514 0.089 -0.0363\nH -0.0514 -0.089 -0.0363\n"
    request = {
        "atoms": ["H", "Cl", "C", "H", "H"],
        "coordinate_unit": "angstrom",
        "coordinates": [[1.027, 0, -0.363], [0, 0, 1.78], [0, 0, 0], [-0.514, 0.89, -0.363], [-0.514, -0.89, -0.363]],
    }
    geometry = {
        "source": {
            "job_ref": receipt["job_ref"],
            "input_fingerprint": receipt["specification_fingerprint"],
            "origin": "interpreter_supplied_coordinates",
            "format": "table",
            "positions_relative_to": "received_source_text",
            "record_number": 1,
            "position_kind": "line_number",
            "positions": [1, 2, 3, 4, 5],
            "sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            "byte_length": len(source_text.encode()),
        },
        "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        "prepared_request": request,
        "float32_coordinates": [
            [struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in request["coordinates"]
        ],
        "preparation": {
            "method": "coordinate_table",
            "source_unit": "nm",
            "scale_to_angstrom": 10,
            "conversion_reference": "fixed-nm-conversion",
        },
        "prediction_unit": "ppm",
        "loaded_elements": ["C", "H"],
        "predictions": [
            {"atom_index": index, "element": atom, "shift_ppm": index + 0.125}
            for index, atom in enumerate(request["atoms"])
            if atom in ("H", "C")
        ],
    }
    retained = {
        "job_ref": "job:retained",
        "execution_attempt_ref": "execution_attempt:retained",
        "input_fingerprint": "sha256:retained",
        "selected_models": {"H": "fixed-checkpoint-h", "C": "fixed-checkpoint-c"},
        "runtime": {"fixed": "runtime"},
        "execution": {"image_id": "sha256:" + "c" * 64},
        "geometries": [geometry],
    }
    actual = copy.deepcopy(retained)
    actual.update(
        job_ref=receipt["job_ref"],
        execution_attempt_ref=metadata["execution_attempt_ref"],
        input_fingerprint=receipt["specification_fingerprint"],
    )
    actual["interpretation"] = {
        "schema_id": "nmrnet.input_interpretation.v1",
        "outcome": "selected",
        "operations": ["prepare_pasted"],
        "turns_started": 1,
        "corrections": 0,
        "routes": [{"endpoint": dict(interpreter), "turns_started": 1}],
    }
    return actual, retained, receipt, metadata, interpreter, source_text


def generated_documents():
    """Synthetic CCl/N1 observations, with no claimed numerical reference."""
    actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
    expected = next(
        case
        for case in json.loads(Path("/fixture/smiles.json").read_bytes())["normative"]
        if case["name"] == "implicit_hydrogens"
    )
    source_sha256 = hashlib.sha256(expected["smiles"].encode()).hexdigest()
    provenance = {
        "source_sha256": source_sha256,
        "requested_count": 1,
        "source_atoms": [
            {"source_atom_index": index, "element": element} for index, element in enumerate(expected["source_atoms"])
        ],
        "added_hydrogen_parents": copy.deepcopy(expected["expected_added_hydrogen_parents"]),
        "runtime": {"python": "fixture-python", "rdkit": "fixture-rdkit"},
        "embedding": {"preset": "ETKDGv3", "requested_settings": {"randomSeed": 42}},
    }
    actual["generation"] = {
        "provenance": provenance,
        "provenance_sha256": hashlib.sha256(json.dumps(provenance, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        "selection": {
            "job_ref": receipt["job_ref"],
            "input_fingerprint": receipt["specification_fingerprint"],
            "origin": "interpreter_supplied_smiles",
            "byte_length": 3,
            "source_sha256": source_sha256,
            "requested_count": 1,
        },
    }
    geometry = actual["geometries"][0]
    stereochemistry = {
        "origin": "generated_coordinates",
        "method": "RDKit.AssignStereochemistryFrom3D(replaceExistingTags=True)",
        "atoms": [],
        "bonds": [],
    }
    request = {
        "coordinate_unit": "angstrom",
        "atoms": ["C", "Cl", "H", "H", "H"],
        "coordinates": [[float(index), 0.1, -0.25] for index in range(5)],
    }
    geometry.update(
        prepared_request=request,
        request_sha256=hashlib.sha256(json.dumps(request, separators=(",", ":")).encode()).hexdigest(),
        float32_coordinates=[[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in request["coordinates"]],
        source={"kind": "smiles", "sha256": source_sha256, "conformer_id": 0},
        preparation={
            "method": "smiles",
            "coordinate_unit": "angstrom",
            "stereochemistry": stereochemistry,
            "stereochemistry_sha256": hashlib.sha256(json.dumps(stereochemistry, sort_keys=True).encode()).hexdigest(),
        },
        coverage={
            "basis": "smiles_graph_expansion",
            "source_explicit_hydrogen_atoms": 0,
            "added_hydrogen_atoms": 3,
            "coordinate_hydrogen_atoms": 3,
            "predicted_hydrogen_atoms": 3,
            "hydrogen_completeness": "not_independently_validated",
        },
        predictions=[
            {"atom_index": index, "element": atom, "shift_ppm": index + 0.25}
            for index, atom in enumerate(request["atoms"])
            if atom in ("C", "H")
        ],
    )
    actual["interpretation"]["operations"] = ["prepare_smiles"]
    return actual, retained, receipt, metadata, interpreter, expected


class QualificationClock:
    """Advance only when the qualification requests its normal polling pause."""

    def __init__(self):
        self.seconds = 0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


def clocked_client(output, *, ready_at):
    """Use the real client/submit/readback with finite in-process response data."""
    actual, _retained, receipt, metadata, interpreter, _text = scientific_documents()
    identity = {
        "origin": "https://api.example:9443",
        "authority": "api.example:9443",
        "topology": "dev-local",
        "provider_ref": "provider:fixture",
        "interpretation_scripted": False,
        "interpreter": interpreter,
    }
    with patch("api_prediction.ssl.create_default_context", return_value=None):
        client = Client(
            output,
            Path("/synthetic/unused-key.json"),
            "192.0.2.1",
            "fixture0",
            identity,
            "project:user-demo",
            UserAuthorityRefs("demo"),
        )
    clock, calls = QualificationClock(), []
    content = json.dumps(actual).encode()
    result = {
        "analysis_result_ref": "analysis_result:fixture",
        "provider_ref": identity["provider_ref"],
        "execution_attempt_ref": metadata["execution_attempt_ref"],
        "result_byte_length": len(content),
        "result_fingerprint": "sha256:" + hashlib.sha256(content).hexdigest(),
    }

    def request(label, method, path, query="", body=None, status=200):
        calls.append((label, method, path, query, body, status))
        if label == "directory":
            return {"providers": [{"provider_ref": identity["provider_ref"]}]}
        if label == "create":
            command = canonicalize_job_create_request_body(body)
            created = {
                name: getattr(command, name)
                for name in (
                    "operation_ref",
                    "project_ref",
                    "analysis_kind_ref",
                    "request_fingerprint",
                    "specification_fingerprint",
                )
            }
            return created | {"job_ref": receipt["job_ref"], "submitter_ref": client.user.principal_ref, "replayed": False}
        if label == "select":
            return {"provider_refs": [identity["provider_ref"]]}
        if label == "open":
            return {"job": {"job_ref": receipt["job_ref"], "state": "open"}}
        if label in ("attempts", "final-attempts"):
            attempts = (
                [{"execution_attempt_ref": metadata["execution_attempt_ref"], "state": "succeeded"}]
                if clock.seconds >= ready_at
                else []
            )
            return {"job_ref": receipt["job_ref"], "next_cursor": None, "attempts": attempts}
        if label == "results":
            return {"job_ref": receipt["job_ref"], "next_cursor": None, "results": [result] if clock.seconds >= ready_at else []}
        if label == "result":
            import base64  # noqa: PLC0415 -- Only the readback response fixture needs its wire encoding.

            return result | {"job_ref": receipt["job_ref"], "canonical_result_base64": base64.b64encode(content).decode()}
        raise AssertionError("The clock fixture received an unexpected API operation")

    client.request = request
    return client, clock, calls
