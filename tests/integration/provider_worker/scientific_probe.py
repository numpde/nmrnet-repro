"""One released scientific endpoint for the composed lifecycle qualification.

The host grants only read-only science inputs and a private socket directory.
It owns exact-container retirement and removal of these IPC files. This endpoint
receives no signing key, controller state, host receipts or engine access.
"""

from tools.container_guard import require_container

require_container()

import importlib.metadata
import json
import socket
import sys
from pathlib import Path

from nmrnet_inference.worker import serve_released_connection


def main():
    allocation = sys.argv[1]
    if Path("/control").exists() or Path("/state").exists() or Path("/var/run/docker.sock").exists():
        raise RuntimeError("Scientific worker received controller or engine authority")
    # This is observed worker metadata, not independently attested image contents.
    # The host admits the immutable image and its embedded dependency declarations.
    runtime = {"python": sys.version.split()[0], "installed_distributions": {
        name: importlib.metadata.version(name) for name in ("torch", "numpy", "scikit-learn", "joblib")}}
    Path("/ipc/metadata.json").write_text(json.dumps({"runtime": runtime}))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("/ipc/science.sock")
        listener.listen(1)
        listener.settimeout(90)
        Path("/ipc/ready").write_text(allocation + "\n")
        connection, _ = listener.accept()
        with connection:
            serve_released_connection(connection, allocation=allocation, io_seconds=90,
                                      declaration_path=Path("/input/prediction-models.json"),
                                      weight_root=Path("/weight"), checkpoint_root=Path("/checkpoints"))


if __name__ == "__main__":
    main()
