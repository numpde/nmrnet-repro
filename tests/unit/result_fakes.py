"""Recording scientific facts for result admission, without model execution."""

from tools.container_guard import require_container

require_container()

import hashlib
import json


def scientific_facts():
    member = {"archive_sha256": "a" * 64, "member": "fixture", "size": 1, "sha256": "b" * 64}
    return {
        "job_ref": "job:fixture",
        "execution_attempt_ref": "execution_attempt:sha256:" + "e" * 64,
        "input_fingerprint": "sha256:" + "f" * 64,
        "execution_identity": {"image_id": "fixture"},
        "selected_artifacts": {
            "dictionary": member,
            "elements": {head: {"checkpoint": member, "scaler": member} for head in ("H", "C")},
        },
        "runtime_expectations": {"python": "fixture", "installed_distributions": {"scikit-learn": "1.8.0"}},
    }


def scientific_outcomes(batch, facts):
    """Different per-conformer values expose accidental reordering or averaging."""
    return tuple(
        {
            "request_sha256": hashlib.sha256(conformer.request_bytes).hexdigest(),
            "prediction": {
                "unit": "ppm",
                "predictions": [
                    {"atom_index": index, "element": atom, "shift_ppm": float(ordinal)}
                    for index, atom in enumerate(json.loads(conformer.request_bytes)["atoms"])
                    if atom in {"H", "C"}
                ],
            },
            "provenance": {"artifacts": facts["selected_artifacts"], "runtime": facts["runtime_expectations"]},
            "private_warnings": [],
        }
        for ordinal, conformer in enumerate(batch.conformers, 1)
    )
