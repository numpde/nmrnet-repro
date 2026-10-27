"""Hold the original factory setup task inside the foreground subprocess.

This child-only port allocates no native worker. It checks that admission stop
sets this invocation's cutoff before cancellation, that a second signal does not
cancel the original task again, and that its provider lifetime remains locked.
Exact subprocess kill belongs to the parent test.
"""

import asyncio
import errno
import json
import sys
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider import process as process_module
from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import main
from nmrnet_provider.process_reports import ProviderProcessFailed, failure_evidence
from provider_core.lifetime import ProviderLifetime


setup_task = None


def mark(name):
    directory = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    (directory / name).write_bytes(b"observed\n")


def fail_reporting_stream(*args, **options):
    with (
        patch.object(process_module, "_write_stderr", side_effect=BrokenPipeError(errno.EPIPE, "PRIVATE_STDERR")),
    ):
        try:
            return failure_report(*args, **options)
        except ProviderProcessFailed as failure:
            # A test-only observation, outside the failed production stream,
            # lets the parent verify the error retained by the held finally.
            directory = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
            (directory / "report-stream-failed").write_text(json.dumps(failure_evidence(failure, secrets=())))
            raise


async def held_factory(factory, _pending, *, stopping):
    global setup_task
    factory._begin_setup()
    setup_task = original = asyncio.current_task()
    mark("factory-entered")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if not stopping.is_set():
            raise AssertionError("Factory setup was cancelled before its effect cutoff")
        mark("cutoff-before-cancellation")
        if asyncio.current_task() is original and factory._operation is original:
            mark("original-owner-retained")
        mark("first-cancellation")
        await asyncio.Event().wait()
    finally:
        factory._operation = None
        mark("factory-exited")


async def observe_factory_cleanup(factory):
    if setup_task is not None and not setup_task.done():
        mark("factory-cleanup-entered")
        raise AssertionError("Factory cleanup entered before original setup finished")
    await settle_factory(factory)


def observe_lifetime_close(lifetime):
    if setup_task is not None and not setup_task.done():
        mark("lifetime-close-entered")
        raise AssertionError("Provider lifetime closed before original setup finished")
    close_lifetime(lifetime)


async def observe_signals(*args, **options):
    original = asyncio.get_running_loop().add_signal_handler
    counts = {}

    def install(number, callback, *arguments):
        def received():
            callback(*arguments)
            counts[number] = counts[number] + 1
            mark(f"signal-{number}-{counts[number]}")

        counts[number] = 0
        original(number, received)

    with patch.object(asyncio.get_running_loop(), "add_signal_handler", install):
        return await run_signalled(*args, **options)


if __name__ == "__main__":
    run_signalled = process_module._run_signalled
    failure_report = process_module._failure_report
    settle_factory = NativeAttemptFactory.settle
    close_lifetime = ProviderLifetime.close
    diagnostics = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    if (diagnostics / "inject-report-stream-failure").exists():
        process_module._failure_report = fail_reporting_stream
    with (
        patch.object(NativeAttemptFactory, "__call__", held_factory),
        patch.object(NativeAttemptFactory, "settle", observe_factory_cleanup),
        patch.object(ProviderLifetime, "close", observe_lifetime_close),
        patch.object(process_module, "_run_signalled", observe_signals),
    ):
        raise SystemExit(main())
