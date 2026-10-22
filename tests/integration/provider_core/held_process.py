"""Hold real factory-owned storage I/O inside the foreground subprocess.

This child-only fault injection acquires no native worker. It detects event-loop
teardown cancelling the original work twice, or dependency cleanup while that
work still borrows the store. Exact subprocess kill belongs to the parent test.
"""

import asyncio
import errno
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider import process as process_module
from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import main
from nmrnet_provider.process_reports import ProviderProcessFailed, failure_evidence
from provider_core.attempt_store import AttemptStore


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


async def held_factory(factory, _pending):
    mark("factory-entered")
    original = asyncio.current_task()
    release = threading.Event()

    def held_read():
        mark("read-entered")
        release.wait()
        raise AssertionError("The parent must kill this exact child before the injected read can finish")

    factory.store._files.records = held_read
    try:
        await factory.store.records(owner=factory)
    except asyncio.CancelledError:
        if asyncio.current_task() is original and factory.store.pending_owner is factory:
            mark("original-owner-retained")
        mark("first-cancellation")
        await asyncio.Event().wait()
    finally:
        mark("factory-exited")


async def forbidden_factory_cleanup(_factory):
    mark("factory-cleanup-entered")
    raise AssertionError("Factory cleanup must not run while its original service work is live")


async def forbidden_store_close(_store):
    mark("store-close-entered")
    raise AssertionError("Storage must not close while original work still borrows it")


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
    diagnostics = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    if (diagnostics / "inject-report-stream-failure").exists():
        process_module._failure_report = fail_reporting_stream
    with (
        patch.object(NativeAttemptFactory, "__call__", held_factory),
        patch.object(NativeAttemptFactory, "settle", forbidden_factory_cleanup),
        patch.object(AttemptStore, "close", forbidden_store_close),
        patch.object(process_module, "_run_signalled", observe_signals),
    ):
        raise SystemExit(main())
