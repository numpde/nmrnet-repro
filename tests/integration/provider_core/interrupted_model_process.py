"""Exercise real model interruption through the process with recording native ports.

Only native allocation and a head-observation barrier are replaced. Interpreter,
transport, input ownership, controller, signal handling and receiving reports are
the production implementations. No scientific execution or native-runtime claim.
"""

import errno
import sys
from pathlib import Path
from unittest.mock import patch

import httpcore
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import main
from nmrnet_provider.runtime import assemble_attempt


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
    )


async def observed_head(pool, request):
    response = await original_head(pool, request)
    if request.url.target == b"/chat":
        # Returning to _perform assigns its received head without an intervening
        # await. The parent signals only after observing this fixed marker.
        mark("model-head-received")
    return response


if __name__ == "__main__":
    original_head = httpcore.AsyncHTTPProxy.handle_async_request
    with (
        patch.object(NativeAttemptFactory, "__call__", recording_factory),
        patch.object(httpcore.AsyncHTTPProxy, "handle_async_request", observed_head),
    ):
        raise SystemExit(main())
