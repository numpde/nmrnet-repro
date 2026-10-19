"""Admit frozen conformer collections without repeating native chemistry.

UTF-8 strings preserve the exact source/request/metadata bytes without base64
expansion. Source identity and budgets belong to the caller; graph interpretation
belongs to the qualified producer. This module checks cross-process consistency.
Oversized output retains bounded causal evidence, not failed coordinate payloads,
following note 003's Attempt-local retention contract.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import re

from nmrnet_inference.predict import MAX_ATOMS, MAX_GEOMETRIES, MAX_INPUT_BYTES
from nmrnet_provider.input_geometry import InputRejected, prepare_json
from nmrnet_provider.smiles import (
    ConformerShortfall,
    GeneratedConformer,
    GeneratedConformers,
    GenerationSettings,
    embedding_settings,
    require_smiles_source,
)
from nmrnet_provider.worker_client import WorkerResultRejected
from nmrnet_provider.worker_protocol import (
    MAX_FRAME_BYTES,
    WorkerProtocolError,
    document,
    frame,
    require_allocation,
    require_seconds,
)


def require_generation_deadline(settings, operation_seconds):
    """Admit trusted native budgets before startup or dispatch, without native I/O.

    The cooperative embedding deadline must leave room within the channel's whole
    exchange deadline. Neither confirms native termination; the host still retires
    the exact worker. Composition can call this before acquiring either endpoint.
    """
    if type(settings) is not GenerationSettings:
        raise TypeError("Conformer generation requires one frozen GenerationSettings value from trusted composition.")
    require_seconds(operation_seconds)
    if settings.timeout_seconds >= operation_seconds:
        raise ValueError(
            f"Cannot start conformer generation: its {settings.timeout_seconds}-second cooperative budget "
            f"must be below the {operation_seconds}-second channel deadline."
        )


class ConformerOutputLimit(RuntimeError):
    """Operational capacity failure with private evidence, never usable geometry."""

    def __init__(self, reply, requested_count):
        self.requested_count = requested_count
        self.actual_count = len(reply["conformers"])
        super().__init__(
            f"Cannot return the requested conformer collection: RDKit returned {self.actual_count} of {requested_count}, "
            "but the complete output exceeds the 2 MiB channel limit. "
            "Coordinates were not retained; this collection cannot be submitted."
        )
        self.diagnostic = json.dumps(reply, ensure_ascii=True, allow_nan=False, sort_keys=True)


def smiles_request(source, requested_count):
    """Admit source/count before allocation; interpreter-selected budgets are absent."""
    text = require_smiles_source(source, requested_count)
    return {"operation": "generate_conformers", "smiles_utf8": text, "requested_count": requested_count}


def read_smiles_request(request):
    """Decode only the generation operation, never arbitrary worker settings."""
    if (
        set(request) != {"operation", "smiles_utf8", "requested_count"}
        or request["operation"] != "generate_conformers"
        or type(request["smiles_utf8"]) is not str
    ):
        raise WorkerProtocolError("Conformer request must contain only plain SMILES and the requested count.")
    try:
        source = request["smiles_utf8"].encode("utf-8")
    except UnicodeEncodeError as cause:
        raise WorkerProtocolError("Conformer request does not contain encodable UTF-8 SMILES.") from cause
    require_smiles_source(source, request["requested_count"])
    return source, request["requested_count"]


def conformer_reply(batch, *, allocation):
    """Preflight the whole reply; overflow returns bounded failure evidence.

    Frame admission is authoritative, including escaping and envelope overhead.
    Failed Attempts retain causes and identities, not an archive of coordinates
    (note 003, retention contract). Output-limit evidence preserves native context
    and ordered payload digests; it explicitly offers no coordinates or replay.
    """
    require_allocation(allocation)
    requested = document(batch.provenance).get("requested_count")
    if type(requested) is not int or not 1 <= requested <= MAX_GEOMETRIES or len(batch.conformers) > requested:
        raise ValueError("Cannot encode conformers: the collection contradicts its requested count.")
    reply = {
        # silent-defaults: allow-contract -- The requested and returned counts select complete output or native shortfall; both are explicit outcomes.
        "kind": "conformer_shortfall" if len(batch.conformers) < requested else "conformers",
        "allocation": allocation,
        "provenance_utf8": batch.provenance.decode("utf-8"),
        "diagnostic": batch.diagnostic,
        "conformers": [
            {
                "conformer_id": item.conformer_id,
                "request_utf8": item.request_bytes.decode("utf-8"),
                "stereochemistry_utf8": item.stereochemistry.decode("utf-8"),
            }
            for item in batch.conformers
        ],
    }
    try:
        frame(reply)
    except WorkerProtocolError:
        reply = _output_limit_reply(batch, allocation)
        frame(reply)
    return reply


def _output_limit_reply(batch, allocation):
    return {
        "kind": "conformer_output_limit",
        "allocation": allocation,
        "coordinates_available": False,
        **conformer_evidence(batch),
    }


def conformer_evidence(batch):
    """Retain compact private generation facts without coordinate/stereo payloads.

    Both wire overflow and failed-Job evidence need the same ordered identities;
    neither can reconstruct or submit conformers from these digests.
    """
    return {
        "provenance_utf8": batch.provenance.decode("utf-8"),
        "diagnostic": batch.diagnostic,
        "conformers": [
            {
                "conformer_id": item.conformer_id,
                "request_sha256": hashlib.sha256(item.request_bytes).hexdigest(),
                "request_byte_length": len(item.request_bytes),
                "stereochemistry_sha256": hashlib.sha256(item.stereochemistry).hexdigest(),
                "stereochemistry_byte_length": len(item.stereochemistry),
            }
            for item in batch.conformers
        ],
    }


def prepared_conformers(reply, source, requested_count, *, seed, max_iterations, timeout_seconds):
    """Check a decoded reply from the allocation admitted by WorkerClient.

    No graph parsing, embedding, hydrogen addition or stereo reassignment occurs
    in the controller. Unexpected replies remain private, including their exact
    decoded contents; a valid shortfall remains a non-submittable typed failure.
    """
    require_smiles_source(source, requested_count)
    settings = embedding_settings(seed, max_iterations, timeout_seconds)
    try:
        batch = _prepared_batch(reply, hashlib.sha256(source).hexdigest(), requested_count, settings)
    except (WorkerProtocolError, InputRejected, UnicodeError) as cause:
        raise WorkerResultRejected(reply) from cause
    if reply["kind"] == "conformer_shortfall":
        raise ConformerShortfall(batch)
    return batch


def _json_text(text):
    if type(text) is not str:
        raise WorkerProtocolError("Conformer metadata and coordinate requests must be UTF-8 text.")
    raw = text.encode("utf-8")
    return raw, document(raw)


def _prepared_batch(reply, source_sha256, requested_count, settings):
    output_limit = reply.get("kind") == "conformer_output_limit"
    fields = {"kind", "allocation", "provenance_utf8", "diagnostic", "conformers"}
    if output_limit:
        fields.add("coordinates_available")
    if set(reply) != fields or reply["kind"] not in (
        "conformers",
        "conformer_shortfall",
        "conformer_output_limit",
    ):
        raise WorkerProtocolError("Conformer worker returned no complete collection or typed shortfall.")
    provenance, metadata = _json_text(reply["provenance_utf8"])
    _provenance_fields(metadata)
    embedding = metadata["embedding"]
    if (
        metadata.get("source_sha256") != source_sha256
        or type(metadata.get("requested_count")) is not int
        or metadata["requested_count"] != requested_count
        or embedding["preset"] != "ETKDGv3"
        or type(embedding["requested_settings"]) is not dict
        or set(embedding["requested_settings"]) != set(settings)
        or any(
            type(embedding["requested_settings"][key]) is not type(value) or embedding["requested_settings"][key] != value
            for key, value in settings.items()
        )
    ):
        raise WorkerProtocolError("Generated source identity, count or embedding settings differ from the admitted request.")
    _snapshot_settings(embedding["parameters"], settings)
    records = reply["conformers"]
    if type(records) is not list or not 0 <= len(records) <= requested_count:
        raise WorkerProtocolError("Generated collection has no valid requested count.")
    if not output_limit and (len(records) == requested_count) != (reply["kind"] == "conformers"):
        raise WorkerProtocolError("Generated collection count contradicts its success or shortfall disposition.")
    if type(reply["diagnostic"]) is not str:
        raise WorkerProtocolError("Generated native diagnostic must be private text.")
    atoms = _atom_correspondence(metadata)
    if output_limit:
        _output_limit_records(reply)
        raise ConformerOutputLimit(reply, requested_count)
    identifiers = set()
    conformers = []
    for record in records:
        if type(record) is not dict or set(record) != {"conformer_id", "request_utf8", "stereochemistry_utf8"}:
            raise WorkerProtocolError("Generated conformer has missing or unexpected fields.")
        identifier = record["conformer_id"]
        if type(identifier) is not int or identifier < 0 or identifier in identifiers:
            raise WorkerProtocolError("Generated conformer IDs must be unique nonnegative native identifiers.")
        identifiers.add(identifier)
        raw, request = _json_text(record["request_utf8"])
        prepare_json(raw)
        if request["atoms"] != atoms:
            raise WorkerProtocolError("Generated atom order differs from its source and added-hydrogen map.")
        stereo, annotation = _json_text(record["stereochemistry_utf8"])
        _stereo_references(annotation, len(atoms))
        conformers.append(GeneratedConformer(identifier, raw, stereo))
    return GeneratedConformers(tuple(conformers), provenance, reply["diagnostic"])


def _output_limit_records(reply):
    if reply["coordinates_available"] is not False:
        raise WorkerProtocolError("Conformer output-limit evidence must state that coordinates are unavailable.")
    seen = set()
    for record in reply["conformers"]:
        if type(record) is not dict or set(record) != {
            "conformer_id",
            "request_sha256",
            "request_byte_length",
            "stereochemistry_sha256",
            "stereochemistry_byte_length",
        }:
            raise WorkerProtocolError(
                "Conformer output-limit evidence must retain ordered IDs, payload digests and byte lengths."
            )
        identifier = record["conformer_id"]
        if type(identifier) is not int or identifier < 0 or identifier in seen:
            raise WorkerProtocolError("Conformer output-limit IDs must be unique nonnegative native identifiers.")
        seen.add(identifier)
        for name, limit in (("request", MAX_INPUT_BYTES), ("stereochemistry", MAX_FRAME_BYTES)):
            digest, size = record[f"{name}_sha256"], record[f"{name}_byte_length"]
            if (
                type(digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or type(size) is not int
                or not 1 <= size <= limit
            ):
                raise WorkerProtocolError(
                    "Conformer output-limit evidence requires complete SHA-256 identities and bounded byte lengths."
                )


def _snapshot_settings(snapshot, settings):
    """Reject contradictions in SDK fields that are actually serialized.

    The pinned property-tree serializer emits strings, including booleans and
    numbers (requirements/preparation.md). It does not promise every parameter,
    so absent SDK fields cannot replace the separately retained explicit settings.
    """
    for key, value in settings.items():
        if key not in snapshot:
            continue
        expected = format(value, "g") if type(value) is float else str(value).lower()
        if snapshot[key] != expected:
            raise WorkerProtocolError("Generated parameter snapshot contradicts the admitted embedding settings.")


def _provenance_fields(metadata):
    fields = {
        "source_sha256",
        "requested_count",
        "source_atoms",
        "source_bonds",
        "added_hydrogen_parents",
        "component_count",
        "model_projection",
        "runtime",
        "parser",
        "hydrogen_expansion",
        "embedding",
    }
    if set(metadata) != fields:
        raise WorkerProtocolError("Generated provenance must retain the producer's complete source and runtime evidence.")
    runtime, parser, hydrogen, embedding = (metadata[key] for key in ("runtime", "parser", "hydrogen_expansion", "embedding"))
    if (
        type(runtime) is not dict
        or set(runtime) != {"python", "rdkit"}
        or any(type(value) is not str or not value for value in runtime.values())
        or type(parser) is not dict
        or set(parser) != {"sanitize", "removeHs", "allowCXSMILES", "parseName"}
        or any(type(value) is not bool for value in parser.values())
        or type(hydrogen) is not dict
        or set(hydrogen) != {"explicitOnly", "addCoords"}
        or any(type(value) is not bool for value in hydrogen.values())
        or type(metadata["model_projection"]) is not str
        or not metadata["model_projection"]
        or type(metadata["component_count"]) is not int
        or not 1 <= metadata["component_count"] <= MAX_ATOMS
    ):
        raise WorkerProtocolError("Generated provenance must retain typed runtime, parsing and hydrogen-expansion evidence.")
    if (
        type(embedding) is not dict
        or set(embedding) != {"preset", "requested_settings", "parameters", "failure_counts"}
        or type(embedding["parameters"]) is not dict
        or type(embedding["failure_counts"]) is not list
        or not embedding["failure_counts"]
        or any(type(value) is not int or value < 0 for value in embedding["failure_counts"])
    ):
        raise WorkerProtocolError("Generated provenance must retain the embedding snapshot and aggregate native failure counts.")


def _atom_correspondence(metadata):
    source = metadata.get("source_atoms")
    parents = metadata.get("added_hydrogen_parents")
    if type(source) is not list or not 1 <= len(source) <= MAX_ATOMS or type(parents) is not list:
        raise WorkerProtocolError("Generated provenance has no bounded source atoms and hydrogen-parent map.")
    atoms = []
    for index, atom in enumerate(source):
        if (
            type(atom) is not dict
            or set(atom)
            != {"source_atom_index", "element", "isotope", "atom_map_number", "formal_charge", "chiral_tag", "cip_code"}
            or type(atom.get("source_atom_index")) is not int
            or atom["source_atom_index"] != index
            or type(atom.get("element")) is not str
            or any(type(atom[key]) is not int for key in ("isotope", "atom_map_number", "formal_charge"))
            or atom["isotope"] < 0
            or atom["atom_map_number"] < 0
            or type(atom["chiral_tag"]) is not str
            or (atom["cip_code"] is not None and type(atom["cip_code"]) is not str)
        ):
            raise WorkerProtocolError(
                "Generated source atoms must retain ordered indices, elements, map labels, isotope, charge and stereo fields with their declared types."
            )
        atoms.append(atom["element"])
    _bond_references(metadata["source_bonds"], len(source))
    if len(atoms) + len(parents) > MAX_ATOMS:
        raise WorkerProtocolError("Generated hydrogen expansion exceeds the model atom limit.")
    for index, pair in enumerate(parents, start=len(source)):
        if (
            type(pair) is not list
            or len(pair) != 2
            or any(type(value) is not int for value in pair)
            or pair[0] != index
            or not 0 <= pair[1] < len(source)
            or atoms[pair[1]] == "H"
        ):
            raise WorkerProtocolError("Generated hydrogen indices must follow the source prefix and name a source parent.")
    return atoms + ["H"] * len(parents)


def _stereo_references(annotation, atom_count):
    if (
        set(annotation) != {"origin", "method", "atoms", "bonds"}
        or annotation["origin"] != "generated_coordinates"
        or type(annotation["method"]) is not str
        or type(annotation["atoms"]) is not list
        or type(annotation["bonds"]) is not list
    ):
        raise WorkerProtocolError("Generated stereo must identify its coordinate-derived origin and atom/bond annotations.")
    seen = set()
    for atom in annotation["atoms"]:
        if (
            type(atom) is not dict
            or set(atom) != {"atom_index", "chiral_tag", "cip_code"}
            or type(atom["chiral_tag"]) is not str
            or (atom["cip_code"] is not None and type(atom["cip_code"]) is not str)
        ):
            raise WorkerProtocolError("Generated stereo atoms must retain their index, chiral tag and optional CIP assignment.")
        index = atom["atom_index"]
        if type(index) is not int or not 0 <= index < atom_count or index in seen:
            raise WorkerProtocolError("Generated stereo atom references must be unique and within the generated atom order.")
        seen.add(index)
    _bond_references(annotation["bonds"], atom_count)


def _bond_references(bonds, atom_count):
    if type(bonds) is not list:
        raise WorkerProtocolError("Generated bond evidence must retain a list of source-indexed bonds.")
    for bond in bonds:
        if (
            type(bond) is not dict
            or set(bond) != {"begin_atom_index", "end_atom_index", "bond_type", "stereo", "stereo_atom_indices", "direction"}
            or type(bond["stereo_atom_indices"]) is not list
            or any(type(bond[key]) is not str for key in ("bond_type", "stereo", "direction"))
        ):
            raise WorkerProtocolError(
                "Generated bonds must retain endpoint, type, stereo, direction and controlling-atom evidence."
            )
        indices = [bond.get("begin_atom_index"), bond.get("end_atom_index"), *bond["stereo_atom_indices"]]
        if any(type(index) is not int or not 0 <= index < atom_count for index in indices):
            raise WorkerProtocolError("Generated stereo bond references lie outside the generated atom order.")
