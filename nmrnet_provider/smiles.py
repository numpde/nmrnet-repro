"""Generate one requested conformer collection without prediction or reseeding.

This module is qualified in the isolated RDKit runtime. Its private one-use worker
owns native execution; host composition remains responsible for hard retirement.
Sources and rationale: requirements/preparation.md, SMILES generation evidence.
This producer returns generated atom origins, not invented file records/rows.
Job binding, interpreter correction and ResultPlan admission remain separate.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import sys
from contextlib import redirect_stderr
from dataclasses import dataclass, field

from nmrnet_inference.predict import MAX_ATOMS, MAX_GEOMETRIES, MAX_INPUT_BYTES
from nmrnet_provider.input_geometry import InputRejected, coordinate_request
from nmrnet_provider.native_diagnostics import NativeDiagnostic


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """One trusted composition's budgets, frozen across its asynchronous channel."""

    seed: int
    max_iterations: int
    timeout_seconds: int

    def __post_init__(self):
        embedding_settings(self.seed, self.max_iterations, self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class GeneratedConformer:
    """One actual returned RDKit ID and its exact executable coordinates."""

    conformer_id: int
    request_bytes: bytes = field(repr=False)
    stereochemistry: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class GeneratedConformers:
    """Frozen coordinates and metadata; native messages stay private."""

    conformers: tuple[GeneratedConformer, ...]
    provenance: bytes = field(repr=False)
    diagnostic: str = field(repr=False)


class ConformerShortfall(RuntimeError):
    """Incomplete generation, including budget exhaustion; not invalid chemistry."""

    def __init__(self, partial):
        requested = json.loads(partial.provenance)["requested_count"]
        actual = len(partial.conformers)
        super().__init__(
            f"Cannot generate all requested conformers: requested {requested}, RDKit returned {actual}. "
            "This partial collection cannot be submitted.",
        )
        self.partial = partial


def require_smiles_source(source, requested_count):
    """Admit exact UTF-8 source/count without loading a native parser."""
    if type(requested_count) is not int or not 1 <= requested_count <= MAX_GEOMETRIES:
        raise InputRejected(f"Cannot generate conformers: the requested count must be an integer from 1 to {MAX_GEOMETRIES}.")
    if type(source) is not bytes:
        raise TypeError("Cannot generate conformers: SMILES source must be immutable UTF-8 bytes.")
    if len(source) > MAX_INPUT_BYTES:
        raise InputRejected("Cannot generate conformers: the SMILES source exceeds the 1 MiB byte limit.")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as cause:
        raise InputRejected(f"Cannot generate conformers: SMILES has invalid UTF-8 at byte {cause.start}.") from None
    if not text.strip():
        raise InputRejected("Cannot generate conformers: supply a nonempty SMILES source.")
    # The pinned parser can accept the first line of a multi-line string. One
    # plain token prevents unparsed source text receiving that first geometry.
    if "\x00" in text or any(character.isspace() for character in text.strip()):
        raise InputRejected(
            "Cannot generate conformers: plain SMILES must be one token without internal whitespace or NUL characters."
        )
    return text


def embedding_settings(seed, max_iterations, timeout_seconds):
    """Translate trusted composition budgets into the producer's native settings."""
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise ValueError("Cannot configure conformer generation: seed must be an integer from 0 to 2147483647.")
    for name, value in (("max_iterations", max_iterations), ("timeout_seconds", timeout_seconds)):
        if type(value) is not int or not 1 <= value <= 2**31 - 1:
            raise ValueError(f"Cannot configure conformer generation: {name} must be a positive whole-number native integer.")
    return {
        "randomSeed": seed,
        "numThreads": 1,
        "maxIterations": max_iterations,
        "timeout": timeout_seconds,
        "pruneRmsThresh": -1.0,
        "enforceChirality": True,
        "clearConfs": True,
        "trackFailures": True,
    }


def generate_conformers(source, requested_count, *, seed, max_iterations, timeout_seconds):
    """Embed once with explicit budgets and preserve actual IDs in returned order.

    RDKit's timeout is an internal cooperative deadline. The host must still
    confirm that the isolated worker stopped after any outcome.
    Generated requests are unusable on shortfall; the exception retains their
    exact bytes for private evidence rather than inventing missing conformers.
    Budget arguments belong to trusted runtime composition, never Job/interpreter
    options. Composition must admit them against its hard worker deadline; native
    integer bounds here enforce representability, not a public capacity policy.
    """
    text = require_smiles_source(source, requested_count)
    settings = embedding_settings(seed, max_iterations, timeout_seconds)
    from rdkit import rdBase  # noqa: PLC0415 -- Validate source/count/configuration before importing native code.

    diagnostic = NativeDiagnostic()
    rdBase.LogToPythonStderr()
    try:
        with redirect_stderr(diagnostic):
            conformers, metadata = _embed(text, requested_count, settings)
    except InputRejected as cause:
        cause.diagnostic = diagnostic.value()
        raise
    except Exception as cause:
        diagnostic.add_failure_note(cause)
        raise
    finally:
        rdBase.LogToCppStreams()
    metadata["source_sha256"] = hashlib.sha256(source).hexdigest()
    batch = GeneratedConformers(conformers, json.dumps(metadata, sort_keys=True, allow_nan=False).encode(), diagnostic.value())
    if len(conformers) != requested_count:
        raise ConformerShortfall(batch)
    return batch


def _embed(text, requested_count, settings):
    from rdkit import Chem, rdBase  # noqa: PLC0415 -- Only the isolated native producer loads RDKit.
    from rdkit.Chem import rdDistGeom  # noqa: PLC0415 -- Same native boundary.

    parser_flags = {"sanitize": True, "removeHs": False, "allowCXSMILES": False, "parseName": False}
    parser = Chem.SmilesParserParams()
    for name, value in parser_flags.items():
        setattr(parser, name, value)
    molecule = Chem.MolFromSmiles(text, parser)
    if molecule is None or molecule.GetNumAtoms() == 0:
        raise InputRejected("Cannot generate conformers: RDKit could not parse the plain SMILES; check syntax and valence.")
    if molecule.GetNumAtoms() > MAX_ATOMS:
        raise InputRejected(f"Cannot generate conformers: the source graph exceeds {MAX_ATOMS} atoms before hydrogen addition.")
    source_atoms = [
        {
            "source_atom_index": atom.GetIdx(),
            "element": atom.GetSymbol(),
            "atom_map_number": atom.GetAtomMapNum(),
            "formal_charge": atom.GetFormalCharge(),
            "isotope": atom.GetIsotope(),
            "chiral_tag": str(atom.GetChiralTag()),
            "cip_code": atom.GetProp("_CIPCode") if atom.HasProp("_CIPCode") else None,
        }
        for atom in molecule.GetAtoms()
    ]
    expanded = Chem.AddHs(molecule, explicitOnly=False, addCoords=False)
    if expanded.GetNumAtoms() > MAX_ATOMS:
        raise InputRejected(f"Cannot generate conformers: the graph exceeds {MAX_ATOMS} atoms after hydrogen addition.")
    added_hydrogens = [
        [atom.GetIdx(), atom.GetNeighbors()[0].GetIdx()] for atom in expanded.GetAtoms() if atom.GetIdx() >= len(source_atoms)
    ]
    parameters = rdDistGeom.ETKDGv3()
    for name, value in settings.items():
        setattr(parameters, name, value)
    effective = json.loads(rdDistGeom.EmbedParametersToJSON(parameters))
    identifiers = rdDistGeom.EmbedMultipleConfs(expanded, numConfs=requested_count, params=parameters)
    conformers = tuple(_conformer(expanded, int(identifier)) for identifier in identifiers)
    return conformers, {
        "requested_count": requested_count,
        "source_atoms": source_atoms,
        "source_bonds": _bonds(molecule),
        "added_hydrogen_parents": added_hydrogens,
        "component_count": len(Chem.GetMolFrags(molecule)),
        "model_projection": "Coordinate requests contain elements and positions; isotope, charge and graph stereochemistry remain provenance, not separate prediction features.",
        "runtime": {"python": sys.version.split()[0], "rdkit": rdBase.rdkitVersion},
        "parser": parser_flags,
        "hydrogen_expansion": {"explicitOnly": False, "addCoords": False},
        "embedding": {
            "preset": "ETKDGv3",
            "requested_settings": settings,
            "parameters": effective,
            "failure_counts": list(parameters.GetFailureCounts()),
        },
    }


def _bonds(molecule):
    return [
        {
            "begin_atom_index": bond.GetBeginAtomIdx(),
            "end_atom_index": bond.GetEndAtomIdx(),
            "bond_type": str(bond.GetBondType()),
            "stereo": str(bond.GetStereo()),
            "stereo_atom_indices": list(bond.GetStereoAtoms()),
            "direction": str(bond.GetBondDir()),
        }
        for bond in molecule.GetBonds()
    ]


def _conformer(molecule, conformer_id):
    from rdkit import Chem  # noqa: PLC0415 -- Coordinate-derived annotation remains in the native producer.

    request = _request(molecule, conformer_id)
    # Copy just this conformer, retaining its actual ID. Replacement clears stale
    # CIP properties; this annotation describes generated coordinates, not source
    # specification or experimentally assigned stereochemistry.
    annotated = Chem.Mol(molecule, quickCopy=False, confId=conformer_id)
    if not annotated.GetConformer(conformer_id).Is3D():
        raise RuntimeError("Cannot annotate generated stereochemistry: RDKit returned a conformer without its 3D flag.")
    Chem.AssignStereochemistryFrom3D(annotated, confId=conformer_id, replaceExistingTags=True)
    stereo = {
        "origin": "generated_coordinates",
        "method": "RDKit.AssignStereochemistryFrom3D(replaceExistingTags=True)",
        "atoms": [
            {
                "atom_index": atom.GetIdx(),
                "chiral_tag": str(atom.GetChiralTag()),
                "cip_code": atom.GetProp("_CIPCode") if atom.HasProp("_CIPCode") else None,
            }
            for atom in annotated.GetAtoms()
            if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED
        ],
        "bonds": [bond for bond in _bonds(annotated) if bond["stereo"] != "STEREONONE"],
    }
    return GeneratedConformer(conformer_id, request, json.dumps(stereo, sort_keys=True).encode())


def _request(molecule, conformer_id):
    conformer = molecule.GetConformer(conformer_id)
    atoms = [atom.GetSymbol() for atom in molecule.GetAtoms()]
    coordinates = [list(conformer.GetAtomPosition(index)) for index in range(len(atoms))]
    try:
        return coordinate_request(atoms, coordinates)
    except InputRejected as cause:
        raise RuntimeError(
            "RDKit generated coordinates that cannot be admitted; preserve its native outcome and cause."
        ) from cause
