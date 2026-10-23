"""Recording scientific facts for result admission, without model execution."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
from dataclasses import replace

from tests.unit.smiles_fakes import collection

from nmrnet_provider.input_geometry import UploadMemberSource, UploadSource, prepare_json
from nmrnet_provider.prepared_input import GeneratedJobInput, OutputSettings, PreparedJobInput, SelectedSmilesSource

JOB_TEXT = b"SMILES C; generate 2 conformers."


def generated_job_input(batch):
    """Bind the authored C/N=2 selection; no interpreter or native claim is made."""
    source = SelectedSmilesSource("job:fixture", "sha256:" + hashlib.sha256(JOB_TEXT).hexdigest(), 2, b"C")
    return GeneratedJobInput(source, batch, output=OutputSettings(shift_significant_digits=17))


def scientific_facts():
    member = {"archive_sha256": "a" * 64, "member": "fixture", "size": 1, "sha256": "b" * 64}
    return {
        "job_ref": "job:fixture",
        "execution_attempt_ref": "execution_attempt:sha256:" + "e" * 64,
        "input_fingerprint": "sha256:" + hashlib.sha256(JOB_TEXT).hexdigest(),
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


def precision_documents(values):
    """Long shifts and a long coordinate separate output rounding from science."""
    batch = collection()
    conformers = []
    for conformer in batch.conformers:
        request = json.loads(conformer.request_bytes)
        request["coordinates"][0][0] = 1.23456789012345
        conformers.append(replace(conformer, request_bytes=json.dumps(request).encode()))
    batch = replace(batch, conformers=tuple(conformers))
    facts = scientific_facts()
    outcomes = scientific_outcomes(batch, facts)
    for outcome in outcomes:
        for row, value in zip(outcome["prediction"]["predictions"], values, strict=True):
            row["shift_ppm"] = value
    prepared = generated_job_input(batch)
    return replace(prepared, output=replace(prepared.output, shift_significant_digits=4)), facts, outcomes


def supplied_documents():
    """Authored member identity and shifts test correspondence, not ZIP acquisition or chemistry."""
    raw = b'{"coordinate_unit":"angstrom","atoms":["C","H","Cl"],"coordinates":[[0,0,0],[1.09,0,0],[0,1.78,0]]}'
    geometry = prepare_json(raw)
    source = UploadMemberSource(
        UploadSource("job:fixture", "upload:fixture", 200, "a" * 64),
        "molecule.json",
        len(raw),
        geometry.source_sha256,
    )
    prepared = PreparedJobInput((geometry,), "direct_coordinate_json", source, output=OutputSettings(shift_significant_digits=17))
    facts = scientific_facts()
    outcomes = (
        {
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "prediction": {
                "unit": "ppm",
                "predictions": [
                    {"atom_index": 0, "element": "C", "shift_ppm": 42.5},
                    {"atom_index": 1, "element": "H", "shift_ppm": 1.25},
                ],
            },
            "provenance": {"artifacts": facts["selected_artifacts"], "runtime": facts["runtime_expectations"]},
            "private_warnings": [],
        },
    )
    return prepared, facts, outcomes
