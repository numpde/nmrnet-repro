"""Synthetic documents for causal qualification comparison tests; no science runs."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import struct


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
