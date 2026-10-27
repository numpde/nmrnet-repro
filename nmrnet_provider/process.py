"""Run the configured foreground provider using the existing lifetime owners.

Startup/signals follow SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/main.py:67-108. Partial acquisition/independent cleanup
follows NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/provider_main.py:99-193. Host shell alone owns engine/network
grants and forced retirement; this process never invokes qualification fixtures.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import stat
import sys
from builtins import BaseExceptionGroup
from dataclasses import replace
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected, load_provider_config, read_startup_file
from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process_reports import ProviderProcessFailed, failure_evidence
from nmrnet_provider.service import ProviderService
from provider_core.api import ProviderAPI
from provider_core.diagnostics import operator_redactor, write_stderr as _write_stderr, write_notice as _write_api_notice
from provider_core.lifetime import ProviderLifetime
from provider_core.controller import LiveStartPolicy
from provider_core.execution import ConditionRecovery, ObservationRecovery
from provider_core.ownership import AttemptClaims
from provider_core.process_status import ProcessStatus
from provider_core.response_json import ResponseRejected, read_document
from tools.artifacts import safe_member_name


def route_provider_config(config, *, api, interpreter, store):
    """Bind host-granted routes to every HTTP role before acquiring resources.

    TOML identity still describes only its original bytes. These socket grants
    belong to the host run's private evidence, not public scientific results.
    Core and in-code runtime users retain their explicitly absent direct route;
    the process CLI requires all three grants and offers no direct-mode switch.
    """
    if any(path is None for path in (api, interpreter, store)):
        raise ConfigurationRejected("all foreground HTTP roles require an explicit Unix proxy socket.")
    try:
        return replace(
            config,
            api=replace(config.api, http=replace(config.api.http, proxy_socket=api)),
            interpreter=replace(
                config.interpreter,
                endpoints=tuple(
                    replace(endpoint, http=replace(endpoint.http, proxy_socket=interpreter))
                    for endpoint in config.interpreter.endpoints
                ),
            ),
            uploads=replace(config.uploads, http=replace(config.uploads.http, proxy_socket=store)),
        )
    except ValueError as cause:
        raise ConfigurationRejected(str(cause), cause=cause) from None


def _declared_execution(value):
    """Admit caller facts, never infer that well-formed declarations are observed.

    Field vocabulary/grammars come from tools.baseline_prepare.execution_identity;
    the three image roles and upstream revision are the native service's existing
    qualification facts. File admission adds a closed boundary around that shape.
    """
    required = {
        "image_id",
        "geometry_image_id",
        "controller_image_id",
        "repository_revision",
        "upstream_revision",
        "runtime_identity_source",
        "tool_sources",
    }
    if type(value) is not dict or set(value) != required:
        raise ConfigurationRejected(
            "host execution declaration requires exactly the documented image, revision and source facts."
        )
    result = {}
    for name in ("image_id", "geometry_image_id", "controller_image_id", "repository_revision", "upstream_revision"):
        # silent-defaults: allow-contract -- Image IDs and Git revisions have different fixed grammars; the required field name selects which identity is admitted.
        grammar = r"sha256:[0-9a-f]{64}" if name.endswith("image_id") else r"[0-9a-f]{40}"
        if type(value[name]) is not str or re.fullmatch(grammar, value[name]) is None:
            raise ConfigurationRejected(f"host execution {name} requires a full immutable lowercase identity.")
        result[name] = value[name]
    if value["runtime_identity_source"] != "caller":
        raise ConfigurationRejected("host execution runtime_identity_source must be caller; declarations are not observations.")
    sources = value["tool_sources"]
    if type(sources) is not dict or not sources:
        raise ConfigurationRejected("host execution tool_sources requires a nonempty repository-relative file inventory.")
    admitted = {}
    for name, identity in sources.items():
        _source_name(name)
        admitted[name] = _declared_file(identity, minimum_size=0)
    result.update(runtime_identity_source="caller", tool_sources=admitted)
    return result


def _source_name(value):
    if type(value) is not str or value.endswith("/") or value.startswith("/"):
        raise ConfigurationRejected("declared source/member names must be canonical repository/archive-relative file paths.")
    try:
        safe_member_name(value)
    except ValueError as cause:
        raise ConfigurationRejected(
            "declared source/member path is unsafe; traversal and noncanonical paths are refused.", cause=cause
        ) from None


def _declared_file(value, *, minimum_size):
    if type(value) is not dict or set(value) != {"size", "sha256"}:
        raise ConfigurationRejected("declared file identity requires exactly size and sha256.")
    if type(value["size"]) is not int or value["size"] < minimum_size:
        raise ConfigurationRejected(f"declared file size must be an integer of at least {minimum_size} bytes.")
    return {"size": value["size"], "sha256": _declared_sha256(value["sha256"])}


def _declared_sha256(digest):
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ConfigurationRejected("declared file/archive sha256 must contain exactly 64 lowercase hexadecimal characters.")
    return digest


def _declared_member(value, archive_hash):
    if type(value) is not dict or set(value) != {"member", "size", "sha256"}:
        raise ConfigurationRejected("selected model member requires exactly member, size and sha256.")
    _source_name(value["member"])
    archive_hash = _declared_sha256(archive_hash)
    identity = _declared_file({name: value[name] for name in ("size", "sha256")}, minimum_size=1)
    return {"archive_sha256": archive_hash, "member": value["member"], "size": identity["size"], "sha256": identity["sha256"]}


def _declared_models(selection):
    """Project selected identities, excluding archive acquisition metadata.

    This is the public projection qualified by the native service at 3ee113a.
    The scientific owner still verifies the model bytes actually decoded.
    """
    if type(selection) is not dict or set(selection) != {"dictionary", "checkpoint_archive", "elements"}:
        raise ConfigurationRejected("prediction declaration requires exactly dictionary, checkpoint_archive and elements.")
    dictionary, archive, elements = selection["dictionary"], selection["checkpoint_archive"], selection["elements"]
    if (
        type(dictionary) is not dict
        or not {"archive_sha256", "member", "size", "sha256"} <= set(dictionary)
        or set(dictionary) - {"archive", "archive_sha256", "member", "size", "sha256"}
    ):
        raise ConfigurationRejected(
            "dictionary requires its selected member identity; only optional archive acquisition metadata is accepted."
        )
    if type(archive) is not dict or "archive_sha256" not in archive or set(archive) - {"archive", "archive_sha256"}:
        raise ConfigurationRejected(
            "checkpoint_archive requires archive_sha256; only optional archive acquisition metadata is accepted."
        )
    if (
        type(elements) is not dict
        or set(elements) != {"H", "C"}
        or any(type(pair) is not dict or set(pair) != {"checkpoint", "scaler"} for pair in elements.values())
    ):
        raise ConfigurationRejected("prediction elements require exactly H/C with checkpoint/scaler pairs.")
    return {
        "dictionary": _declared_member(
            {name: dictionary[name] for name in ("member", "size", "sha256")}, dictionary["archive_sha256"]
        ),
        "elements": {
            element: {
                role: _declared_member(elements[element][role], archive["archive_sha256"]) for role in ("checkpoint", "scaler")
            }
            for element in ("H", "C")
        },
    }


def _runtime_facts(execution_path, models_path, config):
    execution_bytes = read_startup_file(execution_path, maximum_bytes=65536, label="host execution declaration")
    models_bytes = read_startup_file(models_path, maximum_bytes=65536, label="prediction model declaration")
    try:
        execution = _declared_execution(read_document(execution_bytes, 65536))
        artifacts = _declared_models(read_document(models_bytes, 65536))
        execution["startup_configuration_sha256"] = config.source_sha256
        execution["prediction_declaration_sha256"] = hashlib.sha256(models_bytes).hexdigest()
        execution["execution_declaration_sha256"] = hashlib.sha256(execution_bytes).hexdigest()
    except ResponseRejected as cause:
        raise ConfigurationRejected(
            "host execution or prediction declaration is not strict JSON; " + str(cause), cause=cause
        ) from None
    return execution, artifacts


def _diagnostics_directory(path):
    metadata = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ConfigurationRejected(
            "diagnostics require an absolute private directory owned by the container user with mode 0700."
        )


def _publish(path, name, content):
    """Expose completed per-run evidence without overwriting earlier observations."""

    def private_file(path, flags):
        return os.open(path, flags | os.O_NOFOLLOW, 0o600)

    pending = path / ("." + name + ".pending")
    # Private creation also covers a permission-setting failure before writing;
    # fchmod normalizes a restrictive ambient umask.
    with open(pending, "xb", opener=private_file) as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(content)
    (path / name).hardlink_to(pending)
    pending.unlink()


def _failure_report(failures, *, secrets, held):
    """Write one redacted causal report to Docker's stderr logging stream.

    Failed delivery retains the original and output causes in the owned error.
    The caller's held-owner finally still prevents release of unresolved work.
    There is no diagnostic file fallback or automatic output retry.
    """
    original = tuple(failures)
    error = ProviderProcessFailed(original, requires_host_retirement=held)
    rendered = (json.dumps(failure_evidence(error, secrets=secrets), ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    try:
        _write_stderr(rendered)
    except (OSError, ValueError) as cause:
        raise ProviderProcessFailed((*original, ("stderr diagnostic output", cause)), requires_host_retirement=held) from None


async def _settle_borrowers(service, factory, failures):
    """Join work before factory cleanup, then let service observe released setup."""
    if service is not None:
        try:
            await service.settle()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("service settlement", cause))
    if service is not None and service.work_task is not None and not service.work_task.done():
        return False
    if factory is not None:
        try:
            await factory.settle()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("factory settlement", cause))
    if service is not None and not service.shutdown_ready:
        try:
            await service.settle()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("service settlement after factory", cause))
    # A passive Job exclusion may outlive cleanup. An unhanded report may not
    # disappear merely because settlement returned or all physical I/O stopped.
    return (service is None or service.shutdown_ready) and (factory is None or factory.shutdown_ready)


def _retired_prediction_observation(owner):
    """Join retry output to the original child; a successor proof has no exit code.

    The factory supplies the native owner. The session invokes this recipient
    only after its native retirement and local channel settlement.
    """
    original = owner.session
    return {"role": owner.role, "operation": original.operation, "generation": original.generation,
            "child_returncode": None if original.retirement is None else original.retirement.returncode}


async def run_provider(config, *, execution_identity, selected_artifacts, stop, diagnostics):
    """Keep unresolved owners alive until the deployment retires their containers."""
    lifetime = transport = factory = service = None
    failures = []
    secrets = tuple(endpoint.spec.api_key for endpoint in config.interpreter.endpoints)
    redact = operator_redactor(secrets=secrets)
    notice_failure = None
    process_status = ProcessStatus(diagnostics, "provider")
    status_phase = "starting"
    status_task = None

    async def refresh_status():
        try:
            while True:
                process_status.refresh("stopping" if stop.is_set() and status_phase != "held" else status_phase)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            failures.append(("process readiness publication", cause))
            stop.set()

    def startup_accepted():
        nonlocal status_phase
        process_status.refresh("ready")
        status_phase = "ready"

    def report_operator_notice(failure, facts):
        """Emit protected lifecycle evidence without making a log sink stop the daemon.

        Keep the latest failed output and its original outage for final reporting;
        no per-retry file, journal, unbounded history or availability dependency.
        """
        nonlocal notice_failure
        try:
            facts = {name: redact(value) if isinstance(value, str) else value for name, value in facts.items()}
            content = {**facts, "failure": failure_evidence(failure, secrets=secrets)}
            _write_api_notice((json.dumps(content, separators=(",", ":")) + "\n").encode())
        except Exception as cause:
            # Rendering and output are diagnostic effects. Neither can veto an
            # already selected crash report or erase the original sink failure.
            notice_failure = (facts, failure, cause)

    def report_api_retry(kind, failure, seconds):
        nonlocal status_phase
        facts = {"event": f"api_{kind}_retry", "retry_seconds": seconds}
        if kind == "hello":
            facts["latest_hello_send_confirmed"] = False
            if status_phase == "ready":
                status_phase = "api-unconfirmed"
                process_status.refresh(status_phase)
        report_operator_notice(failure, facts)

    def report_api_recovered(kind, failure):
        nonlocal status_phase
        facts = {"event": f"api_{kind}_recovered"}
        if kind == "hello":
            facts["latest_hello_send_confirmed"] = True
            if status_phase == "api-unconfirmed":
                status_phase = "ready"
                process_status.refresh(status_phase)
        report_operator_notice(failure, facts)

    def report_attempt_notice(attempt, failure, facts):
        report_operator_notice(
            failure,
            {
                **facts,
                "job_ref": redact(attempt.admission.job_ref),
                "execution_attempt_ref": redact(attempt.admission.execution_attempt_ref),
            },
        )

    try:
        process_status.refresh("starting")
        status_task = asyncio.create_task(refresh_status(), name="process-readiness")
        config.lifecycle.lifetime_root.mkdir(mode=0o700, exist_ok=True)
        lifetime = ProviderLifetime(config.lifecycle.lifetime_root)
        lifetime.open()
        transport = config.api.http.new_transport()
        api = ProviderAPI(config.api.identity, transport, expected_topology=config.api.expected_topology)
        factory = NativeAttemptFactory(
            config=config,
            claims=AttemptClaims.live(limits=config.lifecycle.live_limits),
            execution_identity=execution_identity,
            selected_artifacts=selected_artifacts,
            on_operator_notice=report_operator_notice,
            policy=config.service.native,
            observation_recovery=ObservationRecovery(
                config.lifecycle.observation_gap_seconds,
                config.service.policy.api_backoff_max_seconds,
                lambda attempt, failure, seconds: report_attempt_notice(
                    attempt, failure, {"event": "api_observation_retry", "retry_seconds": seconds}
                ),
                lambda attempt, failure: report_attempt_notice(attempt, failure, {"event": "api_observation_recovered"}),
            ),
            # One daemon owns progress for this Attempt. Use the configured API
            # outage envelope, but retain the separate matching-receipt gate:
            # a recovered lifecycle read cannot acknowledge a condition PUT.
            condition_recovery=ConditionRecovery(
                config.lifecycle.observation_gap_seconds,
                config.service.policy.api_backoff_max_seconds,
                lambda attempt, phase, code, failure, seconds: report_attempt_notice(
                    attempt,
                    failure,
                    {
                        "event": "api_condition_retry",
                        "phase": phase,
                        "condition_code": redact(code) if code is not None else None,
                        "condition_receipt_received": False,
                        "retry_seconds": seconds,
                    },
                ),
                lambda attempt, phase, code, failure: report_attempt_notice(
                    attempt,
                    failure,
                    {
                        "event": "api_condition_recovered",
                        "phase": phase,
                        "condition_code": redact(code) if code is not None else None,
                        "condition_receipt_received": True,
                    },
                ),
            ),
            on_unconfirmed_phase_progress=lambda attempt, phase, failure: report_attempt_notice(
                attempt,
                failure,
                {"event": "api_progress_unconfirmed", "phase": phase, "progress_delivery_confirmed": False},
            ),
            on_prediction_retry=lambda attempt, failure, retired: report_attempt_notice(
                attempt,
                failure,
                {"event": "prediction_retry_started", "maximum_executions": 2,
                 **_retired_prediction_observation(retired)},
            ),
            start_policy=LiveStartPolicy(
                cadence_seconds=config.lifecycle.poll_seconds,
                on_retry=lambda pending, failure, seconds: report_operator_notice(
                    failure,
                    {"event": "api_start_retry", "retry_seconds": seconds, "job_ref": redact(pending.input.job_ref)},
                ),
                on_recovered=lambda attempt, failure: report_attempt_notice(attempt, failure, {"event": "api_start_recovered"}),
                maximum_replays=config.lifecycle.start_replays,
                window_seconds=config.lifecycle.start_seconds,
            ),
            on_delivery_retry=lambda attempt, failure, seconds: report_attempt_notice(
                attempt, failure, {"event": "delivery_handoff_retry", "retry_seconds": seconds}
            ),
        )
        service = ProviderService(
            config=config,
            api=api,
            open_attempt=factory,
            claims=factory.claims,
            current_announcement=lambda: config.service.announcement,
            policy=config.service.policy,
            on_started=startup_accepted,
            on_api_retry=report_api_retry,
            on_api_recovered=report_api_recovered,
            on_observed_crash=lambda crash: report_attempt_notice(
                crash.acknowledged, crash,
                {"event": "worker_crash_observed", "phase": crash.phase,
                 "worker_role": crash.native_exit.role, "worker_generation": crash.native_exit.generation,
                 "worker_operation": crash.native_exit.operation, "returncode": crash.native_exit.returncode},
            ),
        )
        await service.run(stop)
    except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
        failures.append(("provider operation", cause))
    stop.set()
    released = await _settle_borrowers(service, factory, failures)
    # silent-defaults: allow-contract -- Startup can fail before a service exists; only an existing service can retain an unconfirmed Hello send.
    unconfirmed_hello = None if service is None else service.unconfirmed_hello
    if released:
        retirements = []
        if transport is not None:
            retirements.append(("lifecycle transport retirement", transport.retire))
        for operation, close in retirements:
            try:
                await close()
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append((operation, cause))
        released = transport is None or transport.retirement_confirmed
        if released and lifetime is not None:
            try:
                lifetime.close()
            except OSError as cause:
                failures.append(("provider lifetime lock release", cause))
                released = False
    status_phase = "stopping" if released else "held"
    try:
        process_status.refresh(status_phase)
    except (OSError, ValueError) as cause:
        failures.append(("final process readiness publication", cause))
    if released and status_task is not None:
        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass
    try:
        observations = ()
        if unconfirmed_hello is not None:
            observations = (("Latest Hello acceptance was not confirmed", unconfirmed_hello),)
        if notice_failure is not None:
            facts, outage, output_failure = notice_failure
            observations += (
                ("last operator notice cause", outage),
                ("operator notice output", output_failure),
            )
            report_operator_notice(
                outage,
                {
                    "event": "provider_shutdown_observation",
                    "operation": "Notice delivery was not confirmed",
                    "notice": facts,
                    "output": failure_evidence(output_failure, secrets=secrets),
                },
            )
        if failures:
            _failure_report(
                (*failures, *observations),
                secrets=secrets,
                held=not released,
            )
        else:
            # Retirement can succeed while a Hello reply or log delivery remains
            # unconfirmed. Report that evidence without inventing process failure
            # or retaining a parallel per-process diagnostic file.
            if unconfirmed_hello is not None:
                report_operator_notice(
                    unconfirmed_hello,
                    {"event": "provider_shutdown_observation", "operation": "Latest Hello acceptance was not confirmed"},
                )
    finally:
        if not released:
            # Even failed reporting cannot release retained owners through
            # asyncio.run's automatic pending-task cancellation/join.
            await asyncio.Event().wait()
    for _, cause in failures:
        if isinstance(cause, asyncio.CancelledError):
            raise cause
    # silent-defaults: allow-contract -- Exit zero requires both completed retirement and no retained operation or cleanup failures.
    return 1 if failures else 0


async def _run_signalled(config, *, execution_identity, selected_artifacts, diagnostics):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number in previous:
            loop.add_signal_handler(number, stop.set)
        return await run_provider(
            config,
            execution_identity=execution_identity,
            selected_artifacts=selected_artifacts,
            stop=stop,
            diagnostics=diagnostics,
        )
    finally:
        for number, handler in previous.items():
            loop.remove_signal_handler(number)
            signal.signal(number, handler)


def main(arguments=None):
    """Run the contained CLI; startup refusal/interruption terminates with nonzero status."""
    parser = argparse.ArgumentParser(
        description="Configured contained NMRNet foreground provider; host launcher owns its grants."
    )
    for name in ("config", "execution", "models", "diagnostics"):
        parser.add_argument("--" + name, type=Path, required=True)
    for role in ("api", "interpreter", "store"):
        parser.add_argument("--" + role + "-proxy-socket", required=True)
    options = parser.parse_args(arguments)
    try:
        _diagnostics_directory(options.diagnostics)
    except (OSError, ConfigurationRejected) as cause:
        # An unadmitted destination must receive no writes. The host's private
        # Docker logs still get the owned reason or OS errno, never its path.
        error = ProviderProcessFailed((("diagnostics admission", cause),), requires_host_retirement=False)
        sys.stderr.write(json.dumps(failure_evidence(error, secrets=()), separators=(",", ":")) + "\n")
        raise SystemExit(2) from None
    try:
        config = load_provider_config(options.config)
        config = route_provider_config(
            config, api=options.api_proxy_socket, interpreter=options.interpreter_proxy_socket, store=options.store_proxy_socket
        )
        execution, artifacts = _runtime_facts(options.execution, options.models, config)
    except (OSError, ConfigurationRejected) as cause:
        _failure_report((("startup admission", cause),), secrets=(), held=False)
        raise SystemExit(2) from None
    try:
        logger = logging.getLogger("nmrnet_provider.process")
        logger.info("Starting provider service; stdout and stderr are collected by Docker.")
        status = asyncio.run(
            _run_signalled(config, execution_identity=execution, selected_artifacts=artifacts, diagnostics=options.diagnostics)
        )
        logger.info("Provider service settled with exit status %s.", status)
        return status
    except asyncio.CancelledError:
        raise SystemExit(130) from None


if __name__ == "__main__":
    from nmrnet_provider.runtime_logging import configure_logging

    configure_logging(os.environ.get("NMRNET_LOG_LEVEL", "INFO"))
    raise SystemExit(main())
