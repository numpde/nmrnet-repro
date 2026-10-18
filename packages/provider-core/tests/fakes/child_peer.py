"""A private scripted peer: distinguish output closure from process exit.

This is not a scientific worker or a sibling's wire protocol. Its preparation
acknowledgement establishes ordering only; no chemical admission is performed.
"""

from provider_core.container_guard import require_container

require_container()

import json
import os
import signal
import sys


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1]
    if mode not in {"success", "reject", "block", "close_output"}:
        raise ValueError("Unknown scripted child mode")
    source = json.loads(sys.stdin.readline())
    if source["operation"] != "prepare":
        raise ValueError("Scripted child expected preparation before generation")
    if mode == "reject":
        emit({"state": "rejected"})
        signal.pause()
    emit({"state": "prepared", "source_fingerprint": source["source_fingerprint"]})
    request = json.loads(sys.stdin.readline())
    if request != {"operation": "generate"}:
        raise ValueError("Scripted child expected exactly one generation request")
    emit({"state": "generating"})
    if mode == "success":
        emit({"state": "result", "source_fingerprint": source["source_fingerprint"], "value": "scripted-only"})
    elif mode == "close_output":
        # A deliberate EOF is not an exit. Keep this exact child alive until its
        # owner kills it, even when the controller closes the other pipe ends.
        os.close(sys.stdout.fileno())
    signal.pause()


if __name__ == "__main__":
    main()
