"""Run one scientific operation on the descriptor supplied by its supervisor.

Scientific composition moves from worker_main.py at 87c3bad. The binding retains
declaration-before-import admission and actual runtime observations; provider-core
owns process/scratch retirement. Resource snapshots describe the persistent cgroup,
not this operation's individual peak, and go only to bounded Docker logs.
"""

from tools.container_guard import require_container

require_container()

import argparse
import importlib.metadata
import json
import logging
import os
import socket
import sys
from pathlib import Path

from nmrnet_provider.worker_protocol import require_seconds, send
from tools.container_resources import cgroup_snapshot

INPUT_ROOT = Path("/input")
INFERENCE_ROOT = Path("/opt/inference")
LOGGER = logging.getLogger("nmrnet_provider.native_child")


def _prediction(options, connection):
    for name in ("inference.lock", "unicore-source.json"):
        if (INPUT_ROOT / name).read_bytes() != (INFERENCE_ROOT / name).read_bytes():
            raise ValueError(f"Cannot start prediction: embedded {name} differs from its mounted declaration; rebuild the admitted image.")
    from nmrnet_inference.worker import serve_released_connection  # noqa: PLC0415 -- Admit declarations before scientific wiring.

    runtime = {
        "python": sys.version.split()[0],
        "installed_distributions": {
            name: importlib.metadata.version(name) for name in ("torch", "numpy", "scikit-learn", "joblib")
        },
    }
    send(connection, {"kind": "startup", "allocation": options.operation, "runtime": runtime}, options.io_seconds)
    serve_released_connection(
        connection, allocation=options.operation, io_seconds=options.io_seconds,
        declaration_path=INPUT_ROOT / "prediction-models.json", weight_root=Path("/weight"),
        checkpoint_root=Path("/checkpoints"),
    )


def _source(options, connection):
    if options.role == "reader":
        from nmrnet_provider.geometry_worker import serve_geometry_connection  # noqa: PLC0415 -- Keep native imports role-local.

        serve_geometry_connection(connection, allocation=options.operation, io_seconds=options.io_seconds)
    else:
        from nmrnet_provider.smiles import GenerationSettings  # noqa: PLC0415 -- Generation settings belong to this role.
        from nmrnet_provider.smiles_protocol import require_generation_deadline  # noqa: PLC0415 -- Admit before scientific dispatch.
        from nmrnet_provider.smiles_worker import serve_smiles_connection  # noqa: PLC0415 -- Prediction never imports RDKit.

        settings = GenerationSettings(options.seed, options.max_iterations, options.embedding_seconds)
        require_generation_deadline(settings, options.io_seconds)
        serve_smiles_connection(connection, allocation=options.operation, settings=settings, io_seconds=options.io_seconds)


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Serve one inherited scientific channel; use the admitted runner.")
    parser.add_argument("role", choices=("prediction", "reader", "smiles"))
    parser.add_argument("--session-fd", type=int, required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--io-seconds", type=float, required=True)
    for name in ("seed", "max-iterations", "embedding-seconds"):
        parser.add_argument("--" + name, type=int)
    options = parser.parse_args(arguments)
    require_seconds(options.io_seconds)
    generation = options.seed, options.max_iterations, options.embedding_seconds
    if options.role == "smiles" and any(value is None for value in generation):
        parser.error("SMILES child requires explicit seed, max-iterations and embedding-seconds.")
    if options.role != "smiles" and any(value is not None for value in generation):
        parser.error("Generation settings belong only to the SMILES child.")
    # The supervisor admits and passes the raw lease FD. Keep it open until
    # process exit; repeating mode/type checks here would not prove its lock.
    with socket.socket(fileno=options.session_fd) as connection:
        if connection.family != socket.AF_UNIX or connection.type != socket.SOCK_STREAM:
            raise ValueError("Native child requires its inherited Unix scientific stream.")
        try:
            if options.role == "prediction":
                _prediction(options, connection)
            else:
                _source(options, connection)
        finally:
            # A failed observation must not replace the original scientific cause.
            try:
                LOGGER.info("event=native_cgroup_observation operation=%s scope=persistent_container_cumulative facts=%s",
                            options.operation, json.dumps(cgroup_snapshot(), sort_keys=True))
            except Exception:
                LOGGER.exception("Cannot observe native cumulative cgroup resources.")


if __name__ == "__main__":
    from nmrnet_provider.runtime_logging import configure_logging

    configure_logging(os.environ.get("NMRNET_LOG_LEVEL", "INFO"))
    main()
