"""Synthetic cgroup OOM through real framing, allocation and session recovery.

Port Magnet9374f1a tests/unit/oom_recovery.py's kernel counter/reap witnesses;
use NMRNet's exact host retirement/consumption instead of Docker restart.
This fixture loads no released models and contacts no API or interpreter.
"""

import asyncio
import hashlib
import json
import os
import signal
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

# Workers keep controller dependencies out of their 64 MiB budget so the
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
    def __init__(self, execution):
        self.execution = execution

    def prepare(self, requests):
        self.requests, self.handle = requests, object()
        write(Path("/diagnostics/requests.json"), [raw.hex() for raw in requests])
        return self.handle

    def execute(self, handle):
        if handle is not self.handle:
            raise AssertionError("Synthetic prediction received another preparation's handle")
        Path("/diagnostics/execution-entered").write_bytes(b"entered\n")
        if self.execution == 1:
            blocks = []
            for _ in range(64):
                block = bytearray(4 * 1024 * 1024)
                for offset in range(0, len(block), 4096):
                    block[offset] = 1
                blocks.append(block)
            raise AssertionError("Synthetic allocation reached 256 MiB without a kernel kill")
        return tuple(
            {
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "prediction": {"unit": "ppm", "predictions": [{"atom_index": 0, "element": "H", "shift_ppm": 1.25}]},
                "provenance": {"artifacts": ARTIFACTS, "runtime": RUNTIME},
                "private_warnings": [],
            }
            for raw in self.requests
        )


def worker(allocation, execution):
    from nmrnet_inference.worker import serve_connection  # noqa: PLC0415 -- Mounted only into the worker role.

    if Path("/sys/fs/cgroup/memory.max").read_text().strip() != str(64 * 1024 * 1024):
        raise AssertionError("Synthetic OOM requires a 64 MiB worker cgroup")
    if Path("/sys/fs/cgroup/memory.swap.max").read_text().strip() != "0":
        raise AssertionError("Synthetic OOM requires zero additional swap")
    before = oom_kills()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind("/ipc/science.sock")
        listener.listen(1)
        listener.settimeout(20)
        write(Path("/ipc/metadata.json"), {"runtime": RUNTIME})
        Path("/ipc/ready").write_text(allocation + "\n")
        connection, _ = listener.accept()
    child = os.fork()
    if child == 0:
        with connection:
            serve_connection(connection, SyntheticPrediction(execution), allocation=allocation, io_seconds=20)
        os._exit(0)
    # Keep this duplicate stream open until the kernel witness is published;
    # otherwise child death could expose EOF and trigger retirement too early.
    _pid, status = os.waitpid(child, 0)
    child_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    child_exit = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    write(
        Path("/diagnostics/kernel.json"),
        {
            "oom_kill_delta": oom_kills() - before,
            "child_signal": child_signal,
            "child_exit": child_exit,
        },
    )
    connection.close()
    raise SystemExit(128 + child_signal if child_signal is not None else child_exit)


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
    tokens = [first._allocation]

    async def allocate():
        # The host only publishes archived after exact retirement/consumption.
        receipt = Path("/native/receipts") / tokens[0]
        if (receipt / "archived").read_text() != f"archived {tokens[0]}\n":
            raise AssertionError("Replacement preceded old allocation consumption")
        worker, owner, observed = await factory.allocate_prediction(job_ref=facts.job_ref)
        tokens.append(worker._allocation)
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
        await session.retire()
        await factory.settle()
    if len(tokens) != 2 or tokens[0] == tokens[1]:
        raise AssertionError("Recovery must allocate exactly one new prediction worker")
    requests = []
    kernels = []
    for token in tokens:
        receipt = Path("/native/receipts") / token
        requests.append(json.loads((receipt / "requests.json").read_bytes()))
        kernels.append(json.loads((receipt / "kernel.json").read_bytes()))
        if (receipt / "archived").read_text() != f"archived {token}\n":
            raise AssertionError("Synthetic worker was not retired and consumed")
    if requests != [[RAW.hex()], [RAW.hex()]]:
        raise AssertionError("Prediction changed its admitted scientific bytes across replacement")
    if kernels != [
        {"oom_kill_delta": 1, "child_signal": signal.SIGKILL, "child_exit": None},
        {"oom_kill_delta": 0, "child_signal": None, "child_exit": 0},
    ]:
        raise AssertionError("Synthetic worker lacks the expected independent kernel and reap witnesses")
    write(
        Path("/output/summary.json"),
        {
            "scope": "synthetic kernel OOM and allocation/session recovery",
            "allocations": tokens,
            "kernels": kernels,
            "executions": 2,
            "scientific_request_sha256": hashlib.sha256(RAW).hexdigest(),
        },
    )


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:] == ["controller"]:
        asyncio.run(controller())
    elif len(sys.argv) == 4 and sys.argv[1] == "worker" and sys.argv[3] in ("1", "2"):
        worker(sys.argv[2], int(sys.argv[3]))
    else:
        raise ValueError("Select the fixed prediction-recovery qualification controller or worker")
