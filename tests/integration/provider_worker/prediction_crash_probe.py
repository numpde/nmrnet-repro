"""Opt-in synthetic OOM through one native child and the SDK's crash FAIL path.

The launcher captures private cgroup counters; the public failure uses only the
original native exit and scientific phase. Control and relay receipts below are
synthetic: no deployed API, interpreter, model or durable publication is qualified.
This source port is unrun until its opt-in target completes.
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
    from nmrnet_provider.input_geometry import prepare_json
    from nmrnet_provider.prepared_input import PreparedJobInput
    from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
    from nmrnet_provider.process_reports import failure_evidence
    from nmrnet_provider.session import NMRNetSession
    from provider_core.attempt_record import RecoveryScope, StartPending
    from provider_core.attempt_request import start_body
    from provider_core.attempt_response import AttemptAdmission, AttemptObservation, AttemptProgress
    from provider_core.controller import AttemptController, LiveStartPolicy
    from provider_core.delivery_client import DeliveryEndpoint
    from provider_core.delivery_store import TerminalOperation
    from provider_core.ownership import AttemptClaims, LiveAttemptLimits
    from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
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


class FixedJsonPreparation:
    """Supply only this probe's fixed scientific request; no interpreter effects."""

    interpreter_diagnostics = None

    async def prepare(self, instructions):
        if instructions.specification_bytes != RAW:
            raise AssertionError("Synthetic OOM preparation received changed scientific input")
        return PreparedJobInput((prepare_json(RAW),), "direct_coordinate_json")

    async def retire(self):
        pass


class SyntheticPrediction:
    declaration_sha256 = "d" * 64
    def __init__(self, operation):
        self.operation = operation
        self.before = oom_kills()

    def prepare(self, requests):
        self.requests, self.handle = requests, object()
        write(Path("/proof/observations") / (self.operation + ".json"), {
            "requests": [raw.hex() for raw in requests], "oom_kills": self.before,
        })
        return self.handle

    def execute(self, handle):
        if handle is not self.handle:
            raise AssertionError("Synthetic prediction received another preparation's handle")
        blocks = []
        for _ in range(256):
            block = bytearray(4 * 1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            blocks.append(block)
        raise AssertionError("Synthetic allocation reached 1 GiB without a kernel kill")


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
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Synthetic native-exit proof must not retry prediction, API reads or delivery")

    claims = AttemptClaims.live(limits=LiveAttemptLimits(
        maximum_attempts=1, maximum_control_bytes=131072, maximum_pending_terminal_bytes=16384,
    ))
    start_policy = LiveStartPolicy(1, unexpected, unexpected)
    observation = ObservationRecovery(1, 1, unexpected, unexpected)
    conditions = ConditionRecovery(1, 1, unexpected, unexpected)
    notices = []

    def notice(failure, facts):
        notices.append({"facts": facts, "failure": failure_evidence(failure, secrets=())})

    factory = NativeAttemptFactory(
        config=None, claims=claims, execution_identity={"prediction_declaration_sha256": "d" * 64}, selected_artifacts=ARTIFACTS,
        policy=NativePolicy(15, 20, 5), observation_recovery=observation,
        condition_recovery=conditions, start_policy=start_policy,
        on_delivery_retry=unexpected, on_unconfirmed_phase_progress=unexpected,
        on_operator_notice=notice,
    )
    facts = FeedInputFacts(
        "job:oom-proof", "nmr.job.specification.text.v1", "sha256:" + hashlib.sha256(RAW).hexdigest(), len(RAW)
    )
    pending = StartPending(
        RecoveryScope("https://api.example", "provider:oom-proof", "oom-proof", "one"),
        "peaks_from_mol", facts, start_body(facts.job_ref, "oom-proof"),
    )
    admission = AttemptAdmission(
        execution_attempt_ref="execution_attempt:sha256:" + "a" * 64,
        job_ref=facts.job_ref, analysis_kind_ref=pending.analysis_kind_ref,
        provider_ref=pending.scope.provider_ref, state="in_progress",
        started_at=datetime(2026, 10, 23, tzinfo=UTC), replayed=False,
    )
    owners, starts, deliveries = [], [], []

    async def allocate():
        if owners:
            raise AssertionError("Synthetic native-exit proof attempted another prediction child")
        worker, owner, observed = await factory.allocate_prediction(job_ref=facts.job_ref)
        owners.append(owner)
        return worker, owner, observed

    # These local ports provide bounded control evidence for the real SDK owner.
    # They do not contact an API or establish durable relay acceptance.
    async def start(body, **_arguments):
        starts.append(body)
        if body != pending.body or len(starts) != 1:
            raise AssertionError("Synthetic native-exit proof changed or repeated START")
        return admission

    async def read_attempt(reference, *, job_ref, owner):
        if (reference, job_ref) != (admission.execution_attempt_ref, facts.job_ref):
            raise AssertionError("Synthetic observation changed its Attempt or Job")
        return AttemptObservation(reference, job_ref, "in_progress", "open")

    async def job_input(selected, *, analysis_kind_ref, owner):
        if selected != facts or analysis_kind_ref != pending.analysis_kind_ref:
            raise AssertionError("Synthetic input read changed the selected scientific input")
        return VerifiedJobInput(facts, RAW)

    async def progress(reference, *, phase, condition_code, owner):
        return AttemptProgress(reference, phase, condition_code, datetime(2026, 10, 23, tzinfo=UTC))

    async def retire(**_arguments):
        return None

    async def submit(item):
        if deliveries or item.operation is not TerminalOperation.FAIL:
            raise AssertionError("Synthetic native-exit proof requires exactly one FAIL and no completion")
        deliveries.append(item)
        return item.acceptance()

    api = SimpleNamespace(
        origin=pending.scope.api_origin, exchange_seconds=1, retirement_seconds=1,
        retirement_confirmed=True, pending_owner=None, last_failure=None,
        start_attempt=start, read_attempt=read_attempt, job_input=job_input,
        progress_attempt=progress, retire=retire, can_replay_start=unexpected,
    )
    relay = SimpleNamespace(
        endpoint=DeliveryEndpoint(pending.scope.api_origin, pending.scope.provider_ref, Path("/output/synthetic-relay.sock")),
        retirement_confirmed=True, last_failure=None, submit=submit, retire=retire, can_retry=unexpected,
    )
    session = NMRNetSession(
        allocate_prediction=allocate, execution_identity={"fixture": "synthetic native exit"},
        selected_artifacts=ARTIFACTS, input_preparation=FixedJsonPreparation(),
    )
    lifecycle = AttemptController.live(
        api, claims, session, scope=pending.scope, stopping=asyncio.Event(), start_policy=start_policy,
        allow_active_start_replay=True, maximum_terminal_bytes=16384,
        delivery=relay, on_delivery_retry=unexpected, delivery_retry_initial_seconds=1,
        delivery_retry_max_seconds=30, allowed_job_states=frozenset({"open"}),
        poll_seconds=0.05, task_settle_seconds=10, observation_recovery=observation,
        condition_recovery=conditions, on_unconfirmed_phase_progress=unexpected,
    )
    try:
        accepted = await asyncio.wait_for(lifecycle.run(pending), 60)
        if type(accepted) is not DeliveryAcceptedCheckpoint:
            raise AssertionError("Synthetic native-exit proof did not reach the SDK's accepted FAIL disposition")
    finally:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(factory.settle)
            cleanup.push_async_callback(lifecycle.settle)

    if len(owners) != 1 or len(deliveries) != 1 or len(starts) != 1:
        raise AssertionError("Synthetic native-exit proof requires one START, one prediction child and one FAIL")
    owner, = owners
    crash = lifecycle.operation.observed_crash
    if (crash is None or crash.phase != "running" or crash.runner is not owner.session
            or crash.native_exit.returncode != -signal.SIGKILL):
        raise AssertionError("Synthetic native-exit proof lacks the original prediction-phase SIGKILL receipt")
    body = json.loads(deliveries[0].body)
    public = crash.public_failure()
    if body["failure_code"] != "worker_crashed" or body["failure_message"] != public.message:
        raise AssertionError("Synthetic crash FAIL differs from the SDK's verified exit-and-phase public projection")
    observed = json.loads((Path("/proof/observations") / (owner.allocation + ".json")).read_bytes())
    if observed["requests"] != [RAW.hex()]:
        raise AssertionError("Synthetic prediction changed its admitted scientific bytes")
    if (not owner.session.retirement.scratch_released or not lifecycle.shutdown_ready
            or not factory.shutdown_ready or claims.live_owners()):
        raise AssertionError("Synthetic native-exit proof still holds cleanup, reporting or setup obligations")
    write(Path("/output/failure.json"), body)
    write(Path("/output/native-crash.json"), {
        "failure": failure_evidence(crash, secrets=()), "operator_notices": notices,
        "worker_cgroup_oom_kills_before": observed["oom_kills"],
    })
    write(Path("/output/summary.json"), {
        "scope": "synthetic native exit and SDK-selected FAIL; no released models, deployed API or durable publication",
        "operations": [owner.allocation], "generation": owner.session.generation,
        "retirement_returncodes": [owner.session.retirement.returncode], "executions": 1,
        "failure_code": body["failure_code"], "scientific_request_sha256": hashlib.sha256(RAW).hexdigest(),
    })


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:] == ["controller"]:
        asyncio.run(controller())
    elif sys.argv[1:] == ["oom-kills"]:
        print(oom_kills())
    elif sys.argv[1:2] == ["worker"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--session-fd", type=int, required=True)
        parser.add_argument("--lease-fd", type=int, required=True)
        parser.add_argument("--operation", required=True)
        worker(parser.parse_args(sys.argv[2:]))
    else:
        raise ValueError("Select the fixed native-exit qualification controller, worker or oom-kills reader")
