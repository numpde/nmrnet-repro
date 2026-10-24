"""Synthetic cgroup OOM through installed serial-runner/session recovery.

Port Magnet9374f1a tests/unit/oom_recovery.py's kernel counter/reap witnesses;
use the serial supervisor's reap/scratch receipt instead of Docker restart.
This fixture loads no released models and contacts no API or interpreter.
"""

import asyncio
import argparse
import hashlib
import json
import os
import signal
import socket
import sys
from datetime import UTC, datetime
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

# Workers keep controller dependencies out of their fixed native budget so the
# deliberate prediction allocation, rather than import startup, triggers OOM.
if sys.argv[1:] == ["controller"]:
    from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
    from nmrnet_provider.process_reports import failure_evidence
    from nmrnet_provider.session import NMRNetSession, PredictionRecovery
    from provider_core.attempt_record import RecoveryScope, StartPending, acknowledge_start
    from provider_core.attempt_request import start_body
    from provider_core.attempt_response import AttemptAdmission
    from provider_core.controller import StartRecovery, TerminalRecovery
    from provider_core.execution import ConditionRecovery, ObservationRecovery
    from provider_core.job_input import FeedInputFacts, VerifiedJobInput

RUNTIME = {"python": sys.version.split()[0], "installed_distributions": {}}
MEMBER = {"archive_sha256": "a" * 64, "member": "synthetic", "size": 1, "sha256": "b" * 64}
ARTIFACTS = {"dictionary": MEMBER, "elements": {"H": {"checkpoint": MEMBER, "scaler": MEMBER}}}
RAW = b'{"atoms":["H"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom"}'


def write(path, value):
    with path.open("x", encoding="ascii") as output:
        json.dump(value, output, sort_keys=True)


def oom_kills():
    return int(dict(line.split() for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines())["oom_kill"])


class SyntheticPrediction:
    def __init__(self, operation):
        self.operation = operation
        self.before = oom_kills()
        try:
            with Path("/proof/observations/first-execution").open("xb"):
                pass
        except FileExistsError:
            self.first = False
        else:
            self.first = True

    def prepare(self, requests):
        self.requests, self.handle = requests, object()
        write(Path("/proof/observations") / (self.operation + ".json"), {
            "requests": [raw.hex() for raw in requests], "oom_kills": self.before,
        })
        return self.handle

    def execute(self, handle):
        if handle is not self.handle:
            raise AssertionError("Synthetic prediction received another preparation's handle")
        if self.first:
            blocks = []
            for _ in range(256):
                block = bytearray(4 * 1024 * 1024)
                for offset in range(0, len(block), 4096):
                    block[offset] = 1
                blocks.append(block)
            raise AssertionError("Synthetic allocation reached 1 GiB without a kernel kill")
        return tuple(
            {
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "prediction": {"unit": "ppm", "predictions": [{"atom_index": 0, "element": "H", "shift_ppm": 1.25}]},
                "provenance": {"artifacts": ARTIFACTS, "runtime": RUNTIME},
                "private_warnings": [],
            }
            for raw in self.requests
        )


def worker(options):
    from nmrnet_inference.worker import serve_connection  # noqa: PLC0415 -- Mounted only into the worker role.
    from nmrnet_provider.worker_protocol import send  # noqa: PLC0415 -- Keep controller dependencies out of the native cgroup.

    # Reuse the normal finite component's fixed 512 MiB profile. The bounded
    # 1 GiB allocation exceeds it; no second launch-policy implementation is needed.
    if Path("/sys/fs/cgroup/memory.max").read_text().strip() != str(512 * 1024 * 1024):
        raise AssertionError("Synthetic OOM requires the declared 512 MiB runner cgroup")
    if Path("/sys/fs/cgroup/memory.swap.max").read_text().strip() != "0":
        raise AssertionError("Synthetic OOM requires zero additional swap")
    if Path("/sys/fs/cgroup/memory.oom.group").read_text().strip() != "0":
        raise AssertionError("Synthetic child OOM requires independent process selection, not whole-cgroup killing")
    with socket.socket(fileno=options.session_fd) as connection:
        send(connection, {"kind": "startup", "allocation": options.operation, "runtime": RUNTIME}, 20)
        serve_connection(connection, SyntheticPrediction(options.operation), allocation=options.operation, io_seconds=20)


async def controller():
    def unexpected(*_args):
        raise AssertionError("Synthetic allocation proof must not enter API recovery or conditions")

    factory = NativeAttemptFactory(
        config=None,
        api=None,
        store=SimpleNamespace(pending_owner=None),
        claims=None,
        execution_identity={},
        selected_artifacts=ARTIFACTS,
        policy=NativePolicy(15, 20, 5),
        observation_recovery=ObservationRecovery(1, 1, unexpected, unexpected),
        condition_recovery=ConditionRecovery(1, 1, unexpected, unexpected),
        start_recovery=StartRecovery(1, 1, unexpected, unexpected),
        terminal_recovery=TerminalRecovery(1, 1, unexpected, unexpected),
        on_unconfirmed_phase_progress=unexpected,
        on_prediction_retry=unexpected,
        on_native_notice=unexpected,
    )
    facts = FeedInputFacts(
        "job:oom-proof", "nmr.job.specification.text.v1", "sha256:" + hashlib.sha256(RAW).hexdigest(), len(RAW)
    )
    pending = StartPending(
        RecoveryScope("https://api.example", "provider:oom-proof", "oom-proof", "one"),
        "peaks_from_mol",
        facts,
        start_body(facts.job_ref, "oom-proof"),
    )
    admitted = acknowledge_start(
        pending,
        AttemptAdmission(
            "execution_attempt:sha256:" + "a" * 64,
            facts.job_ref,
            "peaks_from_mol",
            "provider:oom-proof",
            "in_progress",
            datetime(2026, 10, 23, tzinfo=UTC),
            False,
        ),
    )
    first, retirement, runtime = await factory.allocate_prediction(job_ref=facts.job_ref)
    owners = [retirement]

    async def allocate():
        original, = owners
        if (original.session.retirement.returncode != -signal.SIGKILL
                or not original.session.retirement.scratch_released
                or not original._retirement.done() or original._retirement.result() is not None
                or not original._science_closure.done() or original._science_closure.result() is not None):
            raise AssertionError("Replacement preceded the original OOM reap, scratch release or local closure")
        worker, owner, observed = await factory.allocate_prediction(job_ref=facts.job_ref)
        owners.append(owner)
        return worker, owner, observed

    def notice(attempt, failure):
        if attempt is not admitted:
            raise AssertionError("Prediction recovery changed the synthetic Attempt")
        write(Path("/output/retry.json"), failure_evidence(failure, secrets=()))

    session = NMRNetSession(
        worker=first,
        retirement=retirement,
        execution_identity={"fixture": "synthetic OOM"},
        selected_artifacts=ARTIFACTS,
        runtime_expectations=runtime,
        prediction_recovery=PredictionRecovery(allocate, notice),
    )
    try:
        prepared = await session.prepare(admitted, VerifiedJobInput(facts, RAW), report_condition=unexpected)
        result = await session.execute(prepared, report_condition=unexpected)
        write(Path("/output/result.json"), json.loads(result.content))
    finally:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(factory.settle)
            for owner in owners:
                cleanup.push_async_callback(owner.settle)
                cleanup.push_async_callback(owner.retire)
            cleanup.push_async_callback(session.retire)
    operations = [owner.allocation for owner in owners]
    if len(owners) != 2 or operations[0] == operations[1]:
        raise AssertionError("Recovery must acquire exactly one new prediction child")
    if owners[0].session.generation != owners[1].session.generation:
        raise AssertionError("Synthetic OOM unexpectedly replaced the runner generation")
    observations = [json.loads((Path("/proof/observations") / (operation + ".json")).read_bytes()) for operation in operations]
    if [item["requests"] for item in observations] != [[RAW.hex()], [RAW.hex()]]:
        raise AssertionError("Prediction changed its admitted scientific bytes across replacement")
    if observations[1]["oom_kills"] - observations[0]["oom_kills"] != 1:
        raise AssertionError("The same persistent cgroup did not independently report exactly one kernel OOM kill")
    if any(not owner.session.retirement.scratch_released for owner in owners) or factory.allocations or factory.clients:
        raise AssertionError("Completed native ownership did not settle and transfer cleanly")
    write(
        Path("/output/summary.json"),
        {
            "scope": "synthetic kernel OOM and serial-runner/session recovery; no released models or API",
            "operations": operations,
            "generation": owners[0].session.generation,
            "oom_kill_delta": 1,
            "retirement_returncodes": [owner.session.retirement.returncode for owner in owners],
            "executions": 2,
            "scientific_request_sha256": hashlib.sha256(RAW).hexdigest(),
        },
    )


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:] == ["controller"]:
        asyncio.run(controller())
    elif sys.argv[1:2] == ["worker"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--session-fd", type=int, required=True)
        parser.add_argument("--lease-fd", type=int, required=True)
        parser.add_argument("--operation", required=True)
        worker(parser.parse_args(sys.argv[2:]))
    else:
        raise ValueError("Select the fixed prediction-recovery qualification controller or worker")
