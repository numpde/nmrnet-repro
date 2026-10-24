"""Pinned historical inputs for the current Compose scientific qualification.

The retained producer metadata describes the historical export. It must never be
equated with the current runner image/revision. Existing population owners check
these inputs against the independent 001/002 packets and fixed capacity recipe.
"""

import hashlib
from pathlib import Path

from tests.integration.nmrnet.capacity_service import require_capacity_inputs
from tests.integration.nmrnet.reference_fixtures import (
    PRODUCTION_SHA256,
    REFERENCE_SHA256,
    pinned_packet,
    require_reference_inputs,
    verify_fixture,
)

IDENTITIES = {
    "references": ("5f3dd258c942cc3d1c6dc735dd904f7a444cd07b65fe8ec610523a6784f72362",
                   "d8762cf72e329f5d94374287ef5df704e3b29bdf1f449dba8d296cb6d027fa14"),
    "capacity": ("931c6831f212efa3e5357ce1a0078d110ef03e12d2ff18afcf48068a4bb988c1",
                 "fa7b85f57855cc91e285363ce5e7332c93a15f70b27308cf5d8c2cc3291d6759"),
}


def load_scientific_inputs(root, case):
    root = Path(root)
    source_digest, manifest_digest = IDENTITIES[case]
    with (root / (case + ".xyz")).open("rb") as stream:
        source = stream.read(1024 * 1024 + 1)
    if len(source) > 1024 * 1024 or hashlib.sha256(source).hexdigest() != source_digest:
        raise AssertionError("Scientific input bytes differ from the retained original export")
    manifest = pinned_packet(root / (case + ".manifest.json"), manifest_digest)
    verify_fixture(source, manifest)
    if manifest["case"] != case:
        raise AssertionError("Scientific input manifest names a different case")
    if case == "references":
        reference = pinned_packet(root.parent / "reference-cpu-float32.json", REFERENCE_SHA256)
        production = pinned_packet(root.parent / "prediction-cpu-float32.json", PRODUCTION_SHA256)
        require_reference_inputs(manifest, reference, production)
    else:
        require_capacity_inputs(manifest)
    return source, manifest
