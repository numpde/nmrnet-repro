"""Discard one held payload offline, under the queue's existing exclusive lock.

The operator must stop shared delivery first. Producer sockets deliberately have
no deletion route. No API request, retry, scientific execution or archive occurs.
"""

from provider_core.container_guard import require_container

require_container()

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from provider_core.delivery_store import DeliveryQueue, QueueLimits
from provider_core.diagnostics import failure_evidence
from provider_core.startup_file import read_startup_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--api-origin", required=True)
    parser.add_argument("--provider-ref", required=True)
    parser.add_argument("--execution-attempt-ref", required=True)
    parser.add_argument("--body-sha256", required=True)
    options = parser.parse_args()
    os.umask(0o077)
    document = tomllib.loads(read_startup_file(
        options.config, maximum_bytes=65536, label="delivery configuration"
    ).decode("utf-8"))
    if document["schema_id"] != "provider-core.delivery.v1":
        raise ValueError("Discard requires the installed delivery configuration.")
    matches = [binding for binding in document["bindings"]
               if (binding["api"]["origin"], binding["api"]["provider_ref"]) == (options.api_origin, options.provider_ref)]
    if len(matches) != 1:
        raise ValueError("Discard requires one exact configured API/provider binding.")
    queue = DeliveryQueue(Path(document["state_directory"]), limits=QueueLimits(**document["limits"]))
    try:
        queue.discard_held(options.api_origin, options.provider_ref, options.execution_attempt_ref, options.body_sha256)
    finally:
        queue.close()
    print("Discarded the matching held payload. This does not establish API acceptance or resolve the remote Attempt.")


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        print("Cannot discard held delivery data. Explicitly stop shared delivery first; check the configured binding, Attempt and held digest. No API request was sent.", file=sys.stderr)
        print(json.dumps(failure_evidence(failure, secrets=())), file=sys.stderr)
        raise SystemExit(2) from None
