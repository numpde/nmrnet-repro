"""Exercise real structure reading and exact host retirement in separate containers.

The stalled-reader case deliberately ignores TERM. Only the existing host owner
can kill/remove its exact container and release IPC before confirming retirement.
Native scenarios use the admitted RDKit worker and a separate lightweight
controller. A post-parse stall is deliberate, not evidence of an RDKit hang.
None of these fixed fixtures qualifies the unfinished public offering.
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
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.geometry_protocol import read_source
from nmrnet_provider.geometry_worker import serve_geometry_connection
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.retirement import HostRetirement
from nmrnet_provider.worker_client import WorkerReportedFailure
from probe import wait_for


def blocked_reader(allocation, _request):
    Path("/ipc/entered").write_text(allocation + "\n")
    threading.Event().wait()


def blocked_after_native_read(allocation, request):
    read_source(request)
    if "rdkit" not in sys.modules:
        raise RuntimeError("Post-parse stall did not enter the native reader.")
    blocked_reader(allocation, request)


def worker(allocation, scenario):
    for path in ("/control", "/var/run/docker.sock", "/weight", "/checkpoints", "/input"):
        if Path(path).exists():
            raise RuntimeError("Structure worker received authority outside its source channel")
    if scenario.endswith("timeout"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("/ipc/geometry.sock")
        listener.listen(1)
        listener.settimeout(15)
        Path("/ipc/ready").write_text(allocation + "\n")
        connection, _ = listener.accept()
        with connection:
            if scenario.endswith("timeout"):
                blocking_read = blocked_after_native_read if scenario == "geometry-native-timeout" else blocked_reader
                with patch("nmrnet_provider.geometry_worker.read_source", side_effect=partial(blocking_read, allocation)):
                    serve_geometry_connection(connection, allocation=allocation, io_seconds=5)
            else:
                serve_geometry_connection(connection, allocation=allocation, io_seconds=5)
    if "torch" in sys.modules:
        raise RuntimeError("Structure reading unexpectedly imported model dependencies.")
    if ("rdkit" in sys.modules) != scenario.startswith("geometry-native-"):
        raise RuntimeError("Structure worker's native imports differ from its declared scenario.")


@dataclass(frozen=True)
class GeometryInput:
    source: bytes = field(repr=False)
    source_format: str
    selection: tuple[int, ...] | str | None
    alternate_location: str | None = None


def geometry_input(scenario):
    """Keep fixture selection and the deliberate malformed-byte mutation together."""
    if scenario in ("geometry-success", "geometry-timeout"):
        return GeometryInput(Path("/input/mixed_records.xyz").read_bytes(), "xyz", (3, 1))
    if scenario == "geometry-pdb":
        return GeometryInput(Path("/input/alternate_locations.pdb").read_bytes(), "pdb", (1,), "B")
    if scenario == "geometry-mol2":
        return GeometryInput(Path("/input/mixed_records.mol2").read_bytes(), "mol2", (2, 1))
    if scenario in ("geometry-native-mol", "geometry-native-timeout"):
        return GeometryInput(Path("/input/atom_order_v2000.mol").read_bytes(), "mol", None)
    if scenario == "geometry-native-sdf":
        return GeometryInput(Path("/input/mixed_records.sdf").read_bytes(), "sdf", (3, 1))
    if scenario == "geometry-native-rejected":
        return GeometryInput(Path("/input/invalid_last.sdf").read_bytes(), "sdf", "all")
    if scenario == "geometry-native-failure":
        mol = Path("/input/atom_order_v2000.mol").read_bytes()
        # Matches tests/integration/rdkit_runtime/test_geometry_worker.py: MOL
        # preflight succeeds, but RDKit returns its unclassified empty result.
        malformed = mol.replace(b"    1.0270", b"PRIVATEBAD", 1)
        return GeometryInput(mol + b"$$$$\n" + malformed + b"$$$$\n", "sdf", "all")
    raise ValueError("Structure boundary probe requires a declared fixture scenario.")


def check_geometries(geometries, case):
    corpus = json.loads(Path("/input/expected.json").read_bytes())
    numbers = (1,) if case.source_format in ("mol", "pdb") else (3, 1)
    if case.source_format == "mol2":
        numbers = (2, 1)
    if tuple(geometry.source_record_number for geometry in geometries) != numbers:
        raise RuntimeError("Separate structure worker lost the selected record order.")
    for geometry in geometries:
        name = "water" if case.source_format == "mol2" and geometry.source_record_number == 2 else "chloromethane"
        expected = corpus["geometries"][name]
        if case.source_format == "pdb":
            expected = corpus["native_files"]["normative/alternate_locations.pdb"]["selection_B"]
        if case.source_format == "xyz":
            positions = (15, 16, 17, 18, 19) if geometry.source_record_number == 3 else (3, 4, 5, 6, 7)
            position_kind = "line_number"
        elif case.source_format == "pdb":
            positions = tuple(expected["source_atom_ids"])
            position_kind = "atom_serial"
        else:
            positions = tuple(range(1, len(expected["atoms"]) + 1))
            position_kind = "atom_id"
        request = json.loads(geometry.request_bytes)
        if (
            geometry.source_positions != positions
            or geometry.source_position_kind != position_kind
            or geometry.source_sha256 != hashlib.sha256(case.source).hexdigest()
            or geometry.source_unit != "angstrom"
            or geometry.scale_to_angstrom != 1.0
            or geometry.conversion_reference != "identity (angstrom)"
            or request["atoms"] != expected["atoms"]
            or request["coordinates"] != expected["coordinates_angstrom"]
        ):
            raise RuntimeError("Separate structure worker changed geometry or source correspondence.")


async def read_case(client, case, allocation, scenario):
    """Exercise one expected outcome without converting unexpected failures."""
    try:
        geometries = await client.read(
            case.source,
            source_format=case.source_format,
            source_unit="angstrom",
            selection=case.selection,
            alternate_location=case.alternate_location,
        )
    except TimeoutError as timeout:
        if not scenario.endswith("timeout"):
            raise
        if scenario == "geometry-native-timeout":
            # A marker arriving after timeout must not turn slow native startup
            # into evidence that the worker was already in the deliberate stall.
            try:
                entered = Path("/ipc/entered").read_bytes()
            except FileNotFoundError as missing_marker:
                raise RuntimeError("Native reader timed out before its post-parse stall marker was present.") from missing_marker
        else:
            entered = wait_for(Path("/ipc/entered"), 2)
        if entered != (allocation + "\n").encode():
            raise RuntimeError("Timed-out reader's stall marker belongs to another allocation.") from timeout
    except InputRejected as cause:
        if cause.diagnostic:
            Path("/control/request/reader-diagnostic.txt").write_text(cause.diagnostic)
        if scenario != "geometry-native-rejected":
            raise
        if "SDF record 4" not in str(cause):
            raise RuntimeError("Source rejection lost the failed selected record identity.") from cause
    except WorkerReportedFailure as cause:
        Path("/control/request/reader-diagnostic.txt").write_text(cause.diagnostic)
        if scenario != "geometry-native-failure":
            raise
        if not all(text in cause.diagnostic for text in ("coordinates on line 5", "SDF record 2", "RuntimeError")):
            raise RuntimeError("Native failure lost its protected parser or source-record context.") from cause
        if any(text in str(cause) or text in repr(cause) for text in ("PRIVATEBAD", "coordinates on line 5")):
            raise RuntimeError("Native failure exposed private context in public error text.") from cause
    else:
        if scenario.endswith("timeout") or scenario in ("geometry-native-rejected", "geometry-native-failure"):
            raise RuntimeError("Reader returned geometry instead of its expected failure outcome.")
        check_geometries(geometries, case)
        return geometries
    return ()


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
    seconds = 0.15 if scenario == "geometry-timeout" else 5
    client = GeometryClient(reader, writer, allocation=allocation, operation_seconds=seconds)
    retirement = HostRetirement(
        allocation=allocation,
        request_directory=Path("/control/request"),
        receipt_directory=Path("/control/receipt"),
        operation_seconds=30,
    )
    geometries = ()
    try:
        case = geometry_input(scenario)
        geometries = await read_case(client, case, allocation, scenario)
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
    if "rdkit" in sys.modules or "torch" in sys.modules:
        raise RuntimeError("Structure controller imported native parser or model dependencies.")
    evidence = {
        "allocation": allocation,
        "scenario": scenario,
        "source_sha256": hashlib.sha256(case.source).hexdigest(),
        "request_sha256": [hashlib.sha256(geometry.request_bytes).hexdigest() for geometry in geometries],
        "host_retirement_confirmed": True,
    }
    Path("/control/request/evidence.json").write_text(json.dumps(evidence, sort_keys=True) + "\n")
    Path("/control/request/observed").write_text("retired " + allocation + "\n")


if __name__ == "__main__":
    role, allocation, scenario = sys.argv[1:]
    if scenario not in (
        "geometry-success",
        "geometry-timeout",
        "geometry-pdb",
        "geometry-mol2",
        "geometry-native-mol",
        "geometry-native-sdf",
        "geometry-native-rejected",
        "geometry-native-failure",
        "geometry-native-timeout",
    ):
        raise ValueError("Structure boundary probe requires one of its declared fixture scenarios")
    if role == "worker":
        worker(allocation, scenario)
    elif role == "controller":
        asyncio.run(controller(allocation, scenario))
    else:
        raise ValueError("Structure boundary probe requires worker or controller mode")
