"""Exercise real structure reading and exact host retirement in separate containers.

The stalled-reader case deliberately ignores TERM. Only the existing host owner
can kill/remove its exact container and release IPC before confirming retirement.
This qualifies the process boundary, not RDKit or the unfinished public offering.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import errno
import hashlib
import json
import signal
import socket
import sys
import threading
from functools import partial
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.geometry_worker import serve_geometry_connection
from nmrnet_provider.retirement import HostRetirement
from probe import wait_for


def blocked_reader(allocation, _request):
    Path("/ipc/entered").write_text(allocation + "\n")
    threading.Event().wait()


def worker(allocation, scenario):
    for path in ("/control", "/var/run/docker.sock", "/weight", "/checkpoints", "/input"):
        if Path(path).exists():
            raise RuntimeError("Structure worker received authority outside its source channel")
    if scenario == "geometry-timeout":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("/ipc/geometry.sock")
        listener.listen(1)
        listener.settimeout(15)
        Path("/ipc/ready").write_text(allocation + "\n")
        connection, _ = listener.accept()
        with connection:
            if scenario == "geometry-timeout":
                with patch("nmrnet_provider.geometry_worker.read_source", side_effect=partial(blocked_reader, allocation)):
                    serve_geometry_connection(connection, allocation=allocation, io_seconds=5)
            else:
                serve_geometry_connection(connection, allocation=allocation, io_seconds=5)
    if "torch" in sys.modules or "rdkit" in sys.modules:
        raise RuntimeError("Pure structure reading unexpectedly imported model or native parser dependencies")


async def controller(allocation, scenario):
    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("Structure controller received engine authority")
    try:
        Path("/control/receipt/forged").write_bytes(b"retired\n")
    except OSError as cause:
        if cause.errno != errno.EROFS:
            raise
    else:
        raise RuntimeError("Structure controller could forge the host retirement receipt")
    reader, writer = await asyncio.open_unix_connection("/ipc/geometry.sock")
    seconds = {"geometry-success": 5, "geometry-timeout": 0.15}[scenario]
    client = GeometryClient(reader, writer, allocation=allocation, operation_seconds=seconds)
    retirement = HostRetirement(
        allocation=allocation,
        request_directory=Path("/control/request"),
        receipt_directory=Path("/control/receipt"),
        operation_seconds=30,
    )
    source = Path("/input/mixed_records.xyz").read_bytes()
    geometries = ()
    try:
        if scenario == "geometry-timeout":
            try:
                await client.read(source, source_format="xyz", source_unit="angstrom", selection=(3, 1))
            except TimeoutError:
                if wait_for(Path("/ipc/entered"), 2) != (allocation + "\n").encode():
                    raise RuntimeError("Timed-out reader did not enter the deliberate blocking call") from None
            else:
                raise RuntimeError("Deliberately blocked reader returned geometry instead of timing out")
        else:
            geometries = await client.read(source, source_format="xyz", source_unit="angstrom", selection=(3, 1))
            expected = json.loads(Path("/input/expected.json").read_bytes())["geometries"]["chloromethane"]
            if tuple(geometry.source_record_number for geometry in geometries) != (3, 1):
                raise RuntimeError("Separate structure worker lost the selected record order")
            for geometry, positions in zip(geometries, ((15, 16, 17, 18, 19), (3, 4, 5, 6, 7)), strict=True):
                request = json.loads(geometry.request_bytes)
                if (
                    geometry.source_positions != positions
                    or geometry.source_sha256 != hashlib.sha256(source).hexdigest()
                    or request["atoms"] != expected["atoms"]
                    or request["coordinates"] != expected["coordinates_angstrom"]
                ):
                    raise RuntimeError("Separate structure worker changed geometry or source correspondence")
    finally:
        # Cleanup failures retain the original read/assertion failure in Python's
        # exception chain, captured privately by the existing host supervisor.
        try:
            await retirement.retire()
        finally:
            client.close()
            await client.wait_closed()
    if Path("/ipc/geometry.sock").exists():
        raise RuntimeError("Host confirmed retirement before releasing the structure socket grant")
    evidence = {
        "allocation": allocation,
        "scenario": scenario,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "request_sha256": [hashlib.sha256(geometry.request_bytes).hexdigest() for geometry in geometries],
        "host_retirement_confirmed": True,
    }
    Path("/control/request/evidence.json").write_text(json.dumps(evidence, sort_keys=True) + "\n")
    Path("/control/request/observed").write_text("retired " + allocation + "\n")


if __name__ == "__main__":
    role, allocation, scenario = sys.argv[1:]
    if scenario not in ("geometry-success", "geometry-timeout"):
        raise ValueError("Structure boundary probe requires an explicit success or timeout scenario")
    if role == "worker":
        worker(allocation, scenario)
    elif role == "controller":
        asyncio.run(controller(allocation, scenario))
    else:
        raise ValueError("Structure boundary probe requires worker or controller mode")
