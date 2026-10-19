"""Qualify native generation and host retirement in separate contained endpoints.

Success captures generated data, not an experimental geometry or shift reference.
Timeout is an injected hold after actual generation, never an observed RDKit hang.
The existing host launcher owns CID admission, image/profile receipts and cleanup.
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
import traceback
from builtins import BaseExceptionGroup
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider.retirement import HostRetirement, retire_worker_channel
from nmrnet_provider.smiles import GenerationSettings, generate_conformers
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.smiles_protocol import require_generation_deadline
from nmrnet_provider.smiles_worker import serve_smiles_connection
from probe import wait_for

SETTINGS = GenerationSettings(seed=20261019, max_iterations=1280, timeout_seconds=1)
OPERATION_SECONDS = 5
SCENARIO_CASES = {"geometry-smiles": "flexible_multiple", "geometry-smiles-timeout": "implicit_hydrogens"}


@dataclass(frozen=True)
class SmilesInput:
    source: bytes = field(repr=False)
    requested_count: int
    fixture_sha256: str
    expected_json: bytes = field(repr=False)


def source_case(scenario):
    if scenario not in SCENARIO_CASES:
        raise ValueError("Generation qualification requires its declared success or post-generation stall scenario.")
    name = SCENARIO_CASES[scenario]
    raw = Path("/input/smiles.json").read_bytes()
    matches = [item for item in json.loads(raw)["normative"] if item["name"] == name]
    if len(matches) != 1:
        raise RuntimeError(f"Cannot qualify native generation: the authored corpus must contain exactly one {name} case.")
    case = matches[0]
    return SmilesInput(
        case["smiles"].encode(), case["requested_conformers"], hashlib.sha256(raw).hexdigest(), json.dumps(case).encode()
    )


def worker(allocation, scenario):
    for path in (
        "/control",
        "/var/run/docker.sock",
        "/weight",
        "/checkpoints",
        "/input",
        "/fixtures",
        "/package",
        "/runtime-deps",
    ):
        if Path(path).exists():
            raise RuntimeError("Conformer worker received authority outside its private source channel.")
    require_generation_deadline(SETTINGS, OPERATION_SECONDS)
    if scenario == "geometry-smiles-timeout":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("/ipc/geometry.sock")
        listener.listen(1)
        listener.settimeout(15)
        Path("/ipc/ready").write_text(allocation + "\n")
        connection, _ = listener.accept()
        with connection:
            if scenario == "geometry-smiles-timeout":

                def hold_after_generation(*arguments, **keywords):
                    generate_conformers(*arguments, **keywords)
                    Path("/ipc/entered").write_text(allocation + "\n")
                    threading.Event().wait()

                with patch("nmrnet_provider.smiles_worker.generate_conformers", side_effect=hold_after_generation):
                    serve_smiles_connection(connection, allocation=allocation, settings=SETTINGS, io_seconds=OPERATION_SECONDS)
            else:
                serve_smiles_connection(connection, allocation=allocation, settings=SETTINGS, io_seconds=OPERATION_SECONDS)
    if "rdkit" not in sys.modules or "torch" in sys.modules:
        raise RuntimeError("Conformer worker did not use its declared native-only runtime.")


async def generate_case(client, case, allocation, scenario):
    if scenario == "geometry-smiles":
        return await client.generate(case.source, case.requested_count)
    deadline = asyncio.get_running_loop().time() + OPERATION_SECONDS
    task = asyncio.create_task(client.generate(case.source, case.requested_count))
    try:
        # Observe the marker while the exchange is still live. Waiting for it
        # only after timeout could misclassify slow generation as our injected hold.
        marker = await asyncio.to_thread(wait_for, Path("/ipc/entered"), 3)
        if marker != (allocation + "\n").encode():
            raise RuntimeError("Post-generation stall marker belongs to another allocation.")
        if asyncio.get_running_loop().time() >= deadline or task.done():
            try:
                await task
            except Exception as cause:
                raise RuntimeError(
                    "Generation ended before the post-generation stall was observed within its deadline."
                ) from cause
            raise RuntimeError("Generation returned before the controlled stall was observed.")
        try:
            await task
        except TimeoutError:
            return None
        raise RuntimeError("Conformer worker returned instead of remaining in its controlled post-generation stall.")
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        elif not task.cancelled():
            # A missing/foreign marker can race an independent worker failure.
            # Observe that owned task and preserve its cause on the primary error.
            generation_failure = task.exception()
            primary = sys.exception()
            if (
                generation_failure is not None
                and primary is not None
                and primary is not generation_failure
                and primary.__cause__ is not generation_failure
            ):
                primary.add_note("Generation also failed:\n" + "".join(traceback.format_exception(generation_failure)))
                diagnostic = getattr(generation_failure, "diagnostic", None)
                if diagnostic:
                    primary.add_note("Protected generation diagnostic:\n" + diagnostic)


def check_batch(batch, case):
    expected = json.loads(case.expected_json)
    metadata = json.loads(batch.provenance)
    if (
        metadata["source_sha256"] != hashlib.sha256(case.source).hexdigest()
        or metadata["requested_count"] != case.requested_count
        or len(batch.conformers) != case.requested_count
        or [atom["element"] for atom in metadata["source_atoms"]] != expected["source_atoms"]
        or metadata["added_hydrogen_parents"] != expected["expected_added_hydrogen_parents"]
        or metadata["runtime"]["rdkit"] != "2026.09.1"
    ):
        raise RuntimeError("Separate native worker changed the authored source identity, count or hydrogen correspondence.")
    for item in batch.conformers:
        request = json.loads(item.request_bytes)
        if request["atoms"][: len(expected["source_atoms"])] != expected["source_atoms"] or Counter(request["atoms"]) != Counter(
            expected["generated_atom_counts"]
        ):
            raise RuntimeError("Separate native worker changed the authored source atom prefix or generated inventory.")


async def controller(allocation, scenario):
    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("Conformer controller received engine authority.")
    try:
        Path("/control/receipt/forged").write_bytes(b"retired\n")
    except OSError as cause:
        if cause.errno != errno.EROFS:
            raise
    else:
        raise RuntimeError("Conformer controller could forge the host retirement receipt.")
    case = source_case(scenario)
    require_generation_deadline(SETTINGS, OPERATION_SECONDS)
    reader, writer = await asyncio.open_unix_connection("/ipc/geometry.sock")
    client = SmilesClient(reader, writer, allocation=allocation, settings=SETTINGS, operation_seconds=OPERATION_SECONDS)
    host = HostRetirement(
        allocation=allocation,
        request_directory=Path("/control/request"),
        receipt_directory=Path("/control/receipt"),
        operation_seconds=30,
    )
    try:
        batch = await generate_case(client, case, allocation, scenario)
    finally:
        failures = await retire_worker_channel(client, host)
        if failures:
            raise BaseExceptionGroup("Cannot confirm complete conformer worker retirement.", [cause for _, cause in failures])
    if Path("/ipc/geometry.sock").exists():
        raise RuntimeError("Host confirmed retirement before releasing the conformer socket grant.")
    if "rdkit" in sys.modules or "torch" in sys.modules:
        raise RuntimeError("Conformer controller imported native chemistry or prediction dependencies.")
    evidence = {
        "allocation": allocation,
        "scenario": scenario,
        "source_sha256": hashlib.sha256(case.source).hexdigest(),
        "fixture_sha256": case.fixture_sha256,
        "composition_settings": asdict(SETTINGS),
        "host_retirement_confirmed": True,
        "post_generation_stall_observed": batch is None,
        "conformers": [],
    }
    if batch is not None:
        check_batch(batch, case)
        evidence["provenance_utf8"] = batch.provenance.decode()
        evidence["diagnostic"] = batch.diagnostic
        evidence["conformers"] = [
            {
                "conformer_id": item.conformer_id,
                "request_utf8": item.request_bytes.decode(),
                "request_sha256": hashlib.sha256(item.request_bytes).hexdigest(),
                "stereochemistry_utf8": item.stereochemistry.decode(),
                "stereochemistry_sha256": hashlib.sha256(item.stereochemistry).hexdigest(),
            }
            for item in batch.conformers
        ]
    Path("/control/request/evidence.json").write_text(json.dumps(evidence, sort_keys=True) + "\n")
    Path("/control/request/observed").write_text("retired " + allocation + "\n")


if __name__ == "__main__":
    role, allocation, scenario = sys.argv[1:]
    if scenario not in SCENARIO_CASES:
        raise ValueError("Conformer qualification requires a declared scenario.")
    if role == "worker":
        worker(allocation, scenario)
    elif role == "controller":
        asyncio.run(controller(allocation, scenario))
    else:
        raise ValueError("Conformer qualification requires worker or controller mode.")
