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
from nmrnet_provider.process_reports import DiagnosticOutputRefused, ProviderProcessFailed, failure_evidence
from nmrnet_provider.service import ProviderService
from provider_core.api import ProviderAPI
from provider_core.attempt_store import AttemptStore
from provider_core.ownership import AttemptClaims
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
    for name in ("admitted", "result.json", "failure.json", ".admitted.pending", ".result.json.pending", ".failure.json.pending"):
        if (path / name).exists() or (path / name).is_symlink():
            raise ConfigurationRejected("diagnostics contain earlier process evidence; use a fresh private run directory.")


def _publish(path, name, content):
    """Expose completed per-run evidence without overwriting earlier observations."""

    def private_file(path, flags):
        return os.open(path, flags | os.O_NOFOLLOW, 0o600)

    pending = path / ("." + name + ".pending")
    # Private creation also covers a permission-setting failure before writing;
    # fchmod normalizes a restrictive ambient umask, as AttemptFiles does.
    with open(pending, "xb", opener=private_file) as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(content)
    (path / name).hardlink_to(pending)
    pending.unlink()


def _failure_report(diagnostics, failures, *, secrets, held):
    """Attempt independent protected sinks without discarding any original cause.

    A broken stderr must not prevent file publication. A failed file write gets
    one bounded stderr report; neither sink is retried. If both outputs fail,
    retain every cause in the owned error. run_provider's held-owner finally
    still prevents that reporting failure from releasing unresolved work.
    """
    original = tuple(failures)
    reporting = []

    def content():
        error = ProviderProcessFailed((*original, *reporting), requires_host_retirement=held)
        return (json.dumps(failure_evidence(error, secrets=secrets), ensure_ascii=True, separators=(",", ":")) + "\n").encode()

    rendered = content()
    try:
        _write_stderr(rendered)
    except (OSError, ValueError) as cause:
        reporting.append(("stderr diagnostic output", cause))
    try:
        _publish(diagnostics, "failure.json", content())
    except OSError as cause:
        reporting.append(("failure report file publication", cause))
        if len(reporting) == 1:
            rendered = content()
            try:
                _write_stderr(rendered)
            except (OSError, ValueError) as stderr_failure:
                reporting.append(("stderr diagnostic output", stderr_failure))
        if len(reporting) > 1:
            raise ProviderProcessFailed((*original, *reporting), requires_host_retirement=held) from None
    return tuple(reporting)


async def _settle_borrowers(service, factory, failures):
    """Join work before factory cleanup, then let service observe released setup."""
    service_settled = service is None
    if service is not None:
        try:
            await service.settle()
            service_settled = True
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("service settlement", cause))
    if service is not None and service.work_task is not None and not service.work_task.done():
        return False
    factory_settled = factory is None
    if factory is not None:
        try:
            await factory.settle()
            factory_settled = True
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("factory settlement", cause))
    if service is not None and not service_settled:
        try:
            await service.settle()
            service_settled = True
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("service settlement after factory", cause))
    return service_settled and factory_settled


def _write_stderr(content):
    """Attempt one nonblocking stderr write without changing later writer policy.

    The process owns this descriptor on its event-loop thread; no await occurs
    while flags differ. Full pipes cannot prevent independent private file
    publication. Callers retain failed output and never start a logging task.
    """
    descriptor = sys.stderr.fileno()
    blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        written = os.write(descriptor, content)
        if written != len(content):
            raise DiagnosticOutputRefused(f"Cannot finish protected stderr output: wrote {written} of {len(content)} bytes.")
    finally:
        os.set_blocking(descriptor, blocking)


def _write_api_notice(content):
    """Keep pipe notices atomic so a rejected record cannot corrupt the next one."""
    descriptor = sys.stderr.fileno()
    if stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        maximum = os.fpathconf(descriptor, "PC_PIPE_BUF")
        if len(content) > maximum:
            raise DiagnosticOutputRefused(
                f"Cannot emit complete API read notice: {len(content)} bytes exceed the pipe's {maximum}-byte atomic-write limit."
            )
    _write_stderr(content)


async def run_provider(config, *, execution_identity, selected_artifacts, stop, diagnostics):
    """Serve once; unresolved original owners stay alive for exact host retirement."""
    store = transport = factory = service = None
    failures = []
    secrets = tuple(endpoint.spec.api_key for endpoint in config.interpreter.endpoints)
    notice_failure = None

    def report_api_notice(failure, facts):
        """Emit protected retry state without making a log sink stop the daemon.

        Keep the latest failed output and its original outage for final reporting;
        no per-retry file, journal, unbounded history or availability dependency.
        """
        nonlocal notice_failure
        content = {**facts, "failure": failure_evidence(failure, secrets=secrets)}
        try:
            _write_api_notice((json.dumps(content, separators=(",", ":")) + "\n").encode())
        except (OSError, ValueError) as cause:
            notice_failure = (facts, failure, cause)

    try:
        config.lifecycle.store_root.mkdir(mode=0o700, exist_ok=True)
        store = AttemptStore(
            config.lifecycle.store_root,
            scope=config.lifecycle.scope,
            limits=config.lifecycle.store_limits,
            operation_seconds=config.lifecycle.store_seconds,
        )
        await store.open()
        transport = config.api.http.new_transport()
        api = ProviderAPI(config.api.identity, transport, expected_topology=config.api.expected_topology)
        factory = NativeAttemptFactory(
            config=config,
            api=api,
            store=store,
            claims=AttemptClaims(),
            execution_identity=execution_identity,
            selected_artifacts=selected_artifacts,
            policy=config.service.native,
        )
        service = ProviderService(
            config=config,
            store=store,
            api=api,
            open_attempt=factory,
            current_announcement=lambda: config.service.announcement,
            policy=config.service.policy,
            on_started=lambda: _publish(diagnostics, "admitted", b"startup admission accepted\n"),
            on_api_retry=lambda failure, seconds: report_api_notice(
                failure, {"event": "api_read_retry", "retry_seconds": seconds}
            ),
            on_api_recovered=lambda failure: report_api_notice(failure, {"event": "api_read_recovered"}),
        )
        await service.run(stop)
    except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
        failures.append(("provider operation", cause))
    stop.set()
    released = await _settle_borrowers(service, factory, failures)
    if released and store is not None and store.pending_operation is not None:
        # Before service construction only this process can own store.open.
        # Never claim a controller/factory read merely because its wait expired.
        if store.pending_owner is None:
            try:
                await store.settle(owner=None)
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append(("startup storage settlement", cause))
        released = store.pending_operation is None
    # silent-defaults: allow-contract -- Startup can fail before a service exists; only an existing service can own an interrupted periodic Hello.
    interrupted_hello = None if service is None else service.interrupted_hello
    if released:
        retirements = []
        if transport is not None:
            retirements.append(("lifecycle transport retirement", transport.retire))
        if store is not None:
            retirements.append(("storage close", store.close))
        for operation, close in retirements:
            try:
                await close()
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append((operation, cause))
        released = (transport is None or transport.retirement_confirmed) and (store is None or store.retirement_confirmed)
    if released:
        # silent-defaults: allow-contract -- A released process records whether any operation or independent cleanup failed; successful retirement does not erase those failures.
        state = "stopped_after_failure" if failures else "stopped"
        result = {"state": state, "requires_host_retirement": False}
        if notice_failure is not None:
            facts, outage, output_failure = notice_failure
            result["api_notice_failure"] = {
                **facts,
                "outage": failure_evidence(outage, secrets=secrets),
                "output": failure_evidence(output_failure, secrets=secrets),
            }
        if interrupted_hello is not None:
            result["interrupted_hello"] = {
                "acceptance_confirmed": False,
                "failure": failure_evidence(interrupted_hello, secrets=secrets),
            }
        try:
            _publish(diagnostics, "result.json", (json.dumps(result) + "\n").encode())
        except OSError as cause:
            failures.append(("process result publication", cause))
    try:
        if failures:
            observations = ()
            if interrupted_hello is not None:
                observations = (("Hello publication interrupted; acceptance was not confirmed", interrupted_hello),)
            if notice_failure is not None:
                _facts, outage, output_failure = notice_failure
                observations += (("last API read notice outage", outage), ("API read notice output", output_failure))
            _failure_report(
                diagnostics,
                (*failures, *observations),
                secrets=secrets,
                held=not released,
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
        # stderr capture still gets the owned reason or OS errno, never its path.
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
        _failure_report(options.diagnostics, (("startup admission", cause),), secrets=(), held=False)
        raise SystemExit(2) from None
    try:
        return asyncio.run(
            _run_signalled(config, execution_identity=execution, selected_artifacts=artifacts, diagnostics=options.diagnostics)
        )
    except asyncio.CancelledError:
        raise SystemExit(130) from None


if __name__ == "__main__":
    raise SystemExit(main())
