"""Host-selected one-use listeners for the existing native worker protocols.

Startup admission/listening moves out of the qualification probes at 02a4f77,
tools/worker_qualification.sh:221-264 and scientific_probe.py:18-40. Exclusive
endpoint ownership follows SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/worker.py:159-172. This worker never removes grants:
the host must stop its exact container and release IPC before confirming retirement.
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
from builtins import BaseExceptionGroup
from functools import partial
from pathlib import Path

from nmrnet_provider.worker_protocol import require_allocation, require_seconds
from tools.container_resources import cgroup_snapshot

IPC_ROOT = Path("/ipc")
INPUT_ROOT = Path("/input")
INFERENCE_ROOT = Path("/opt/inference")
DIAGNOSTICS_ROOT = Path("/diagnostics")
LOGGER = logging.getLogger("nmrnet_provider.worker_main")


def _arguments(arguments):
    parser = argparse.ArgumentParser(description="Host-owned native worker; use the provider's contained launcher.")
    parser.add_argument("role", choices=("prediction", "reader", "smiles"))
    parser.add_argument("allocation")
    parser.add_argument("--accept-seconds", type=float, required=True)
    parser.add_argument("--io-seconds", type=float, required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--embedding-seconds", type=int)
    options = parser.parse_args(arguments)
    require_allocation(options.allocation)
    require_seconds(options.accept_seconds)
    require_seconds(options.io_seconds)
    generation = (options.seed, options.max_iterations, options.embedding_seconds)
    if options.role == "smiles":
        if any(value is None for value in generation):
            parser.error("SMILES workers require explicit seed, max-iterations and embedding-seconds.")
    elif any(value is not None for value in generation):
        parser.error("Generation settings are accepted only for the SMILES worker.")
    return options


def _prediction_handler(options):
    # Match baseline.sh's byte admission before importing any model/decoder
    # wiring. Image identity and build labels are separately admitted by the host.
    for name in ("inference.lock", "unicore-source.json"):
        if (INPUT_ROOT / name).read_bytes() != (INFERENCE_ROOT / name).read_bytes():
            raise ValueError(
                f"Cannot start prediction worker: embedded {name} differs from its mounted declaration; rebuild the admitted inference image."
            )
    from nmrnet_inference.worker import serve_released_connection  # noqa: PLC0415 -- Admission precedes scientific wiring.

    runtime = {
        "python": sys.version.split()[0],
        "installed_distributions": {
            name: importlib.metadata.version(name) for name in ("torch", "numpy", "scikit-learn", "joblib")
        },
    }
    # Observed package versions are diagnostic facts, not image attestation or
    # proof that any model has loaded. Exclusive creation refuses stale evidence.
    with (IPC_ROOT / "metadata.json").open("x", encoding="utf-8") as target:
        target.write(json.dumps({"runtime": runtime}))
    return partial(
        serve_released_connection,
        allocation=options.allocation,
        io_seconds=options.io_seconds,
        declaration_path=INPUT_ROOT / "prediction-models.json",
        weight_root=Path("/weight"),
        checkpoint_root=Path("/checkpoints"),
    )


def _source_handler(options):
    if options.role == "reader":
        from nmrnet_provider.geometry_worker import (  # noqa: PLC0415 -- Reader needs no native import at startup.
            serve_geometry_connection,
        )

        return partial(serve_geometry_connection, allocation=options.allocation, io_seconds=options.io_seconds)
    from nmrnet_provider.smiles import GenerationSettings  # noqa: PLC0415 -- Generation settings belong only to this role.
    from nmrnet_provider.smiles_protocol import require_generation_deadline  # noqa: PLC0415 -- Validate before readiness.
    from nmrnet_provider.smiles_worker import (  # noqa: PLC0415 -- Keep prediction imports out of source workers.
        serve_smiles_connection,
    )

    settings = GenerationSettings(options.seed, options.max_iterations, options.embedding_seconds)
    require_generation_deadline(settings, options.io_seconds)
    return partial(serve_smiles_connection, allocation=options.allocation, settings=settings, io_seconds=options.io_seconds)


def _listen(*, allocation, socket_name, accept_seconds, handler):
    """Accept one connection; channel readiness never confirms scientific state."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(IPC_ROOT / socket_name))
        listener.listen(1)
        listener.settimeout(accept_seconds)
        with (IPC_ROOT / "ready").open("x", encoding="ascii") as target:
            target.write(allocation + "\n")
        connection, _ = listener.accept()
        with connection:
            handler(connection)


def _serve(options):
    if options.role == "prediction":
        handler, socket_name = _prediction_handler(options), "science.sock"
    else:
        handler, socket_name = _source_handler(options), "geometry.sock"
    _listen(
        allocation=options.allocation,
        socket_name=socket_name,
        accept_seconds=options.accept_seconds,
        handler=handler,
    )


def _write_resources(options):
    """Publish only a completed observation; the host separately proves retirement."""
    report = {
        "role": options.role,
        "allocation": options.allocation,
        "scope": "cgroup_through_final_snapshot",
        "cgroup": cgroup_snapshot(),
    }
    pending = DIAGNOSTICS_ROOT / "resources.pending"
    with pending.open("x", encoding="ascii") as target:
        target.write(json.dumps(report, sort_keys=True) + "\n")
    # Both names are inside the fresh diagnostic grant. Linking publishes the
    # complete file atomically and refuses to overwrite earlier evidence.
    (DIAGNOSTICS_ROOT / "resources.json").hardlink_to(pending)
    pending.unlink()


def main(arguments=None):
    """Consume trusted host startup arguments, never Job/interpreter options."""
    options = _arguments(arguments)
    LOGGER.info("Starting %s worker.", options.role)
    try:
        _serve(options)
    except BaseException as primary:
        # Raw stderr preserves the full exception graph in Docker logs, including
        # scientific notes. Do not emit a second copy through logger.exception.
        LOGGER.warning("Native worker failed; inspect its Docker logs and retirement receipts.")
        try:
            _write_resources(options)
        except BaseException as observation:
            raise BaseExceptionGroup("Native work and final resource observation both failed", [primary, observation]) from None
        raise
    _write_resources(options)
    LOGGER.info("Native worker finished; host retirement remains required.")


if __name__ == "__main__":
    from nmrnet_provider.runtime_logging import configure_logging

    # INFO is the process default; only the trusted host can request DEBUG.
    configure_logging(os.environ.get("NMRNET_LOG_LEVEL", "INFO"))
    main()
