"""Export private inputs, never predictions, for the connected scientific proof.

The host qualifier owns the exact producer container and completed output grant.
Reference decoding reuses test_prediction_release.reference_requests at 3bd6114;
synthetic capacity uses that test owner's fixed H128/C128 coordinate recipe.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import importlib.metadata
import json
import os
import sys
import unittest
from pathlib import Path

from reference_fixtures import REFERENCE_SHA256, original_request_bytes, pinned_packet, render_fixture

from tools.baseline_prepare import execution_identity


def main(case):
    for name in ("inference.lock", "unicore-source.json"):
        if (Path("/input") / name).read_bytes() != (Path("/opt/inference") / name).read_bytes():
            raise ValueError("Cannot export scientific inputs: embedded dependencies differ from the admitted declarations.")
    if case == "references":
        from test_prediction_release import reference_requests  # noqa: PLC0415 -- Contained scientific decoder only.

        reference = pinned_packet("/input/reference-cpu-float32.json", REFERENCE_SHA256)
        requests = reference_requests(unittest.TestCase(), reference)
        origins = {
            (element, item["key_hex"]): item["raw_sha256"]
            for element in ("H", "C")
            for item in reference["elements"][element]["records"]
        }
    elif case == "capacity":
        requests = [
            (
                element,
                "313238",
                {
                    "coordinate_unit": "angstrom",
                    "atoms": [element] * 128,
                    "coordinates": [[index * 0.1, 0.0, 0.0] for index in range(128)],
                },
            )
            for element in ("H", "C")
        ]
        origins = {
            (element, key): hashlib.sha256(original_request_bytes(request)).hexdigest() for element, key, request in requests
        }
    else:
        raise ValueError("Select references or capacity through the maintained native service target.")
    source, manifest = render_fixture(requests, origins, case=case)
    sources = [
        ("tests/integration/provider_worker/reference_fixture.py", Path(__file__)),
        (
            "tests/integration/provider_core/reference_fixtures.py",
            Path("/workspace/tests/integration/provider_core/reference_fixtures.py"),
        ),
        ("tests/test_prediction_release.py", Path("/workspace/tests/test_prediction_release.py")),
        ("tests/test_prediction_model.py", Path("/workspace/tests/test_prediction_model.py")),
        ("tools/provider_service_qualification.sh", Path("/workspace/tools/provider_service_qualification.sh")),
    ]
    for directory in ("nmrnet_provider", "nmrnet_inference", "tools"):
        sources.extend((str(path.relative_to("/workspace")), path) for path in (Path("/workspace") / directory).glob("*.py"))
    manifest["producer"] = {
        "execution": execution_identity(
            image_id=os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
            repository_revision=os.environ["NMRNET_QUALIFICATION_REPOSITORY_REVISION"],
            tool_sources=sources,
        ),
        "runtime": {name: importlib.metadata.version(name) for name in ("torch", "numpy", "scikit-learn")},
        "upstream_revision": os.environ["NMRNET_QUALIFICATION_UPSTREAM_REVISION"],
        "resources": {
            name: (Path("/sys/fs/cgroup") / name).read_text().strip()
            for name in ("cpu.max", "memory.max", "memory.peak", "memory.swap.max", "pids.max")
        },
    }
    encoded = (json.dumps(manifest, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(source) > 1024 * 1024 or len(encoded) > 1024 * 1024:
        raise ValueError("Cannot export scientific inputs: source or manifest exceeds its fixed 1 MiB bound.")
    for name, content in (("input.xyz", source), ("manifest.json", encoded)):
        with (Path("/output") / name).open("xb") as target:
            target.write(content)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise ValueError("Select one scientific input fixture through its maintained native service target.")
    main(sys.argv[1])
