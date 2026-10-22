"""Inspect or recover one stopped provider obligation without scientific work.

Core owns each lifecycle transition and exact replay. This binding selects one
current admitted record under the existing store lock and keeps original I/O
alive until it settles. NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/journal_inspect.py::inspect_journal and
nmrpeak_provider/attempt_journal.py::prepared_terminal_replay
motivate stopped metadata inspection and exact retained-body reuse. SECS
7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/execution.py::_recover_active
does not rerun interrupted inference; its synthesized failure is not imported.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import hashlib
import json
import re
import signal
import sys
from builtins import BaseExceptionGroup
from dataclasses import replace
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected, load_recovery_config, load_recovery_inspection
from nmrnet_provider.process import _diagnostics_directory, _failure_report, _publish
from nmrnet_provider.process_reports import ProviderProcessFailed, failure_evidence
from provider_core.api import ProviderAPI
from provider_core.attempt_record import (
    StartPending,
    SuppressedOutcome,
    TerminalConfirmed,
    TerminalHeld,
    TerminalPending,
    acknowledged_attempt,
    encode_record,
    pending_start,
)
from provider_core.attempt_store import AttemptStore
from provider_core.ownership import AttemptClaims
from provider_core.recovery import AttemptRecovery, ObservedAttempt, RecoveryRefused


def record_facts(record):
    """Project owned metadata; the digest identifies core's admitted snapshot.

    It is not a digest of arbitrary original file spelling. Never print terminal
    bodies, prepared molecular data or protected diagnostics during inspection.
    """
    start = pending_start(record)
    facts = {
        "record_sha256": hashlib.sha256(encode_record(record)).hexdigest(),
        "phase": type(record).__name__,
        "job_ref": start.input.job_ref,
    }
    if type(record) is not StartPending:
        active = acknowledged_attempt(record)
        facts["execution_attempt_ref"] = active.admission.execution_attempt_ref
        facts["local_execution_phase"] = active.local_phase.value
    if type(record) is TerminalPending:
        facts.update(
            terminal_operation=record.operation.value,
            terminal_body_sha256=hashlib.sha256(record.body).hexdigest(),
            terminal_body_bytes=len(record.body),
            evidence_bytes=len(record.evidence.content),
        )
    if type(record) is TerminalHeld:
        facts.update(problem_code=record.problem_code, request_id=record.request_id)
    if type(record) is SuppressedOutcome:
        facts["suppression_reason"] = record.reason.value
    return facts


async def inspect_recovery(config, *, diagnostics):
    """Read the stopped scoped store without creating an API transport."""
    return await _operate(config, diagnostics=diagnostics, selected=None)


async def recover_record(config, *, record_sha256, diagnostics):
    """Dispatch one selected current snapshot once, never fresh scientific work."""
    return await _operate(config, diagnostics=diagnostics, selected=record_sha256)


async def _operate(config, *, diagnostics, selected):
    store = api = recovery = None
    claims = ()
    failures = []
    result = None
    try:
        if selected is not None and (type(selected) is not str or re.fullmatch(r"[0-9a-f]{64}", selected) is None):
            raise RecoveryRefused("Select the lowercase 64-character record_sha256 returned by stopped inspection.")
        settings = config.lifecycle
        store = AttemptStore(
            settings.store_root, scope=settings.scope, limits=settings.store_limits, operation_seconds=settings.store_seconds
        )
        await store.open()
        records = await store.records()
        if selected is None:
            result = {"operation": "inspect", "records": [record_facts(record) for record in records]}
        else:
            matching = [index for index, record in enumerate(records) if record_facts(record)["record_sha256"] == selected]
            if len(matching) != 1:
                raise RecoveryRefused(
                    "The selected record snapshot is not uniquely current; inspect the stopped store again before recovery."
                )
            # Reserve the complete inventory before dispatch: another retained
            # alias must not grant a competing mutation identity.
            claims = AttemptClaims().claim_records(records)
            claim = claims[matching[0]]
            api = ProviderAPI(
                config.api.identity, config.api.http.new_transport(), expected_topology=config.api.expected_topology
            )
            recovery = AttemptRecovery(api, store, claim)
            outcome = await recovery.recover(records[matching[0]])
            result = {"operation": "recover", "selected_record_sha256": selected}
            if type(outcome) is ObservedAttempt:
                result["observation"] = {
                    "attempt_state": outcome.observation.state,
                    "job_state": outcome.observation.job_state,
                    "execution_resumed": False,
                }
            records = await store.records()
            result["records"] = [record_facts(record) for record in records]
        result["local_obligations_settled"] = all(type(record) is TerminalConfirmed for record in records)
        result["next_action"] = (
            "Normal startup must still check storage capacity and current provider-wide API inventory."
            if result["local_obligations_settled"]
            else "Retain these records. Acknowledged or entered work is not resumed; unresolved obligations still block fresh work."
        )
        result["new_science_started"] = False
        result["configuration_sha256"] = config.source_sha256
    except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
        failures.append(("stopped recovery operation", cause))

    # Settlement observes original I/O only. It never invokes recover again.
    if recovery is not None and recovery.claim.operation_owner is not None:
        try:
            await recovery.settle()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("original recovery settlement", cause))
    if recovery is not None and recovery.last_failure is not None:
        retained = recovery.last_failure
        if not any(cause is retained for _operation, cause in failures):
            # Cancellation remains the original exception; the existing core
            # owner also retains API correlation and mutation-effect evidence.
            failures.append(("retained recovery evidence", retained))
    if store is not None and store.pending_operation is not None and store.pending_owner is None:
        try:
            await store.settle(owner=None)
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("original storage settlement", cause))
    released = (
        all(claim.operation_owner is None for claim in claims)
        and (store is None or store.pending_operation is None)
        and (api is None or api.pending_owner is None)
    )
    if released:
        for claim in claims:
            claim.release()
        if api is not None:
            try:
                await api.retire()
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append(("recovery API retirement", cause))
        # Keep the store lock if the transport still owns cleanup uncertainty.
        if store is not None and (api is None or api.retirement_confirmed):
            try:
                await store.close()
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append(("recovery storage close", cause))
        released = (api is None or api.retirement_confirmed) and (store is None or store.retirement_confirmed)
    if released:
        if result is None:
            result = {"operation": "inspect" if selected is None else "recover", "local_obligations_settled": False}
        result.update(state="stopped_after_failure" if failures else "stopped", requires_host_retirement=False)
        try:
            _publish(diagnostics, "result.json", (json.dumps(result, sort_keys=True) + "\n").encode())
        except OSError as cause:
            failures.append(("recovery result publication", cause))
    try:
        if failures:
            _failure_report(diagnostics, failures, secrets=(), held=not released)
    finally:
        if not released:
            # The host owns exact process retirement. Returning would let
            # asyncio.run discard original storage/transport ownership.
            held = asyncio.Event()
            while True:
                try:
                    await held.wait()
                except asyncio.CancelledError:
                    # A first signal may arrive after an ordinary I/O timeout
                    # already held this process. It cannot discard that owner.
                    continue
    return 1 if failures else 0


async def _run_signalled(operation):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    interrupted = False

    def stop():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            task.cancel()

    try:
        for number in previous:
            loop.add_signal_handler(number, stop)
        return await operation
    finally:
        for number, handler in previous.items():
            loop.remove_signal_handler(number)
            signal.signal(number, handler)


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Inspect or recover one stopped NMRNet Attempt; never execute science.")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("inspect", "run"):
        command = commands.add_parser(operation)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--diagnostics", type=Path, required=True)
        if operation == "run":
            command.add_argument("--record-sha256", required=True)
            command.add_argument("--api-proxy-socket", required=True)
    options = parser.parse_args(arguments)
    try:
        _diagnostics_directory(options.diagnostics)
    except (OSError, ConfigurationRejected) as cause:
        error = ProviderProcessFailed((("recovery diagnostics admission", cause),), requires_host_retirement=False)
        sys.stderr.write(json.dumps(failure_evidence(error, secrets=())) + "\n")
        return 2
    try:
        if options.operation == "inspect":
            config = load_recovery_inspection(options.config)
            operation = inspect_recovery(config, diagnostics=options.diagnostics)
        else:
            config = load_recovery_config(options.config)
            config = replace(
                config, api=replace(config.api, http=replace(config.api.http, proxy_socket=options.api_proxy_socket))
            )
            operation = recover_record(config, record_sha256=options.record_sha256, diagnostics=options.diagnostics)
    except (OSError, ConfigurationRejected, ValueError) as cause:
        _failure_report(options.diagnostics, (("recovery startup admission", cause),), secrets=(), held=False)
        return 2
    return asyncio.run(_run_signalled(operation))


if __name__ == "__main__":
    raise SystemExit(main())
