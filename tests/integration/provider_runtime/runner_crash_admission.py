"""Check real reader grants and one controlled wrong-scratch observation.

The negative control is file admission, not a claim that Docker mounted bad
storage. It changes only the scratch mount in an otherwise admitted observation.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import copy
import json
import os
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected, read_startup_file
from nmrnet_provider.deployment import admit_container, inventory_containers
from provider_core.response_json import read_document


def main():
    parser = argparse.ArgumentParser()
    for name in ("plan", "inspection", "images", "output", "checkout"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("deployment", "container-id"):
        parser.add_argument("--" + name, required=True)
    options = parser.parse_args()

    def read(path):
        return read_startup_file(path, maximum_bytes=2 * 1024 * 1024, label="crash qualification metadata", secret=True)

    # Same literal-dollar wire decoding as tools/provider_deployment.py::creation_plan
    # at 87195c8d924ce6643825e21eaa9f6d9b9be58e72. Nothing evaluates shell text.
    plan = read_document(read(options.plan).replace(b"$$", b"$"), 2 * 1024 * 1024)
    records = read_document(read(options.inspection), 2 * 1024 * 1024)
    images = read_document(read(options.images), 2 * 1024 * 1024)
    inventory_containers(records, requested_ids=[options.container_id], checkout=options.checkout, deployment=options.deployment)
    record, = records
    image, = images
    admit_container(record, role="reader", original_plan=plan, image=image)
    bad = copy.deepcopy(record)
    bad["HostConfig"]["Tmpfs"].pop("/scratch")
    bad["Mounts"] = [mount for mount in bad["Mounts"] if mount["Destination"] != "/scratch"]
    bad["Mounts"].append({"Type": "bind", "Source": "/qualification/wrong-scratch", "Destination": "/scratch",
                          "RW": True, "Mode": "", "Propagation": "rprivate"})
    try:
        admit_container(bad, role="reader", original_plan=plan, image=image)
    except ConfigurationRejected as cause:
        reason = str(cause)  # This exact owner supplies reviewed configuration reasons.
    else:
        raise AssertionError("Original-plan admission accepted scratch replaced by host storage.")
    with options.output.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write((json.dumps({"real_reader_grants_admitted": True, "controlled_scratch_bind_refused": True,
                                  "refusal": reason}, sort_keys=True) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())


if __name__ == "__main__":
    main()
