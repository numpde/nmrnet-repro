"""Authored protocol data; these coordinates are not a native scientific witness."""

from tools.container_guard import require_container

require_container()

import hashlib
import json

from nmrnet_provider.smiles import GeneratedConformer, GeneratedConformers

SOURCE = b"C"
SETTINGS = {"seed": 17, "max_iterations": 200, "timeout_seconds": 1}
NATIVE_SETTINGS = {
    "randomSeed": 17,
    "numThreads": 1,
    "maxIterations": 200,
    "timeout": 1,
    "pruneRmsThresh": -1.0,
    "enforceChirality": True,
    "clearConfs": True,
    "trackFailures": True,
}


def collection(identifiers=(2, 0), *, requested=2):
    """Freeze recognizable IDs and bytes without loading RDKit or a model."""
    metadata = {
        "source_sha256": hashlib.sha256(SOURCE).hexdigest(),
        "requested_count": requested,
        "source_atoms": [
            {
                "source_atom_index": 0,
                "element": "C",
                "isotope": 0,
                "atom_map_number": 0,
                "formal_charge": 0,
                "chiral_tag": "CHI_UNSPECIFIED",
                "cip_code": None,
            }
        ],
        "source_bonds": [],
        "added_hydrogen_parents": [[index, 0] for index in range(1, 5)],
        "component_count": 1,
        "model_projection": "Authored protocol fixture only.",
        "runtime": {"python": "fixture", "rdkit": "fixture"},
        "parser": {"sanitize": True, "removeHs": False, "allowCXSMILES": False, "parseName": False},
        "hydrogen_expansion": {"explicitOnly": False, "addCoords": False},
        "embedding": {"preset": "ETKDGv3", "requested_settings": NATIVE_SETTINGS, "parameters": {}, "failure_counts": [0] * 15},
    }
    stereo = json.dumps({"origin": "generated_coordinates", "method": "fixture", "atoms": [], "bonds": []}).encode()
    conformers = []
    for identifier in identifiers:
        request = json.dumps(
            {
                "coordinate_unit": "angstrom",
                "atoms": ["C", "H", "H", "H", "H"],
                "coordinates": [[identifier, 0, 0], [1, 1, 1], [-1, -1, 1], [-1, 1, -1], [1, -1, -1]],
            }
        ).encode()
        conformers.append(GeneratedConformer(identifier, request, stereo))
    return GeneratedConformers(tuple(conformers), json.dumps(metadata).encode(), "PRIVATE_NATIVE_DIAGNOSTIC")
