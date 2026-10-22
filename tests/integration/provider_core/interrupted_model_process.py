"""Exercise real HTTPS interruption through the process with recording native ports.

Native allocation, a received-head barrier and an optional result-publication
fault are test ports. Interpreter, transport, input ownership, controller, signal
handling and receiving reports are real. No scientific or native-runtime claim.
"""

import errno
import sys
from itertools import count
from pathlib import Path
from unittest.mock import patch

import httpcore
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider import process as process_module
from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import main
from nmrnet_provider.runtime import assemble_attempt

hello_head_numbers = count(1)


def mark(name):
    root = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    (root / name).write_bytes(b"observed\n")


async def forbidden_science(*_args):
    mark("science-entered")
    raise AssertionError("Interrupted interpretation must not prepare or execute prediction")


async def recording_factory(factory, pending):
    ports = SessionPorts()
    ports.worker.prepare = forbidden_science
    ports.worker.execute = forbidden_science
    root = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    if (root / "inject-native-cleanup-failure").exists():
        ports.host.failure = OSError(errno.EIO, "PRIVATE_NATIVE_CLEANUP")
    return await assemble_attempt(
        config=factory.config,
        pending=pending,
        api=factory.api,
        store=factory.store,
        setup_owner=factory,
        claims=factory.claims,
        worker=ports.worker,
        retirement=ports.host,
        execution_identity=factory.execution_identity,
        selected_artifacts=factory.selected_artifacts,
        runtime_expectations=ports.worker.runtime,
        allocate_reader=ports.allocate_reader,
        allocate_generator=ports.allocate_generator,
        observation_recovery=factory.observation_recovery,
        start_recovery=factory.start_recovery,
        on_unconfirmed_phase_progress=factory.on_unconfirmed_phase_progress,
        terminal_recovery=factory.terminal_recovery,
    )


async def observed_head(pool, request):
    response = await original_head(pool, request)
    if request.url.target == b"/chat":
        # Returning to _perform assigns its received head without an intervening
        # await. The parent signals only after observing this fixed marker.
        mark("model-head-received")
    elif request.url.target.endswith(b"/hello"):
        mark(f"hello-head-{next(hello_head_numbers)}-received")
    return response


def publish_with_optional_result_failure(root, name, content):
    if name == "result.json" and (root / "inject-result-publication-failure").exists():
        raise PermissionError(errno.EACCES, "PRIVATE_RESULT_OUTPUT")
    return original_publish(root, name, content)


if __name__ == "__main__":
    original_head = httpcore.AsyncHTTPProxy.handle_async_request
    original_publish = process_module._publish
    with (
        patch.object(NativeAttemptFactory, "__call__", recording_factory),
        patch.object(httpcore.AsyncHTTPProxy, "handle_async_request", observed_head),
        patch.object(process_module, "_publish", publish_with_optional_result_failure),
    ):
        raise SystemExit(main())
