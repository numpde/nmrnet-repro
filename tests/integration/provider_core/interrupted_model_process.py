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

from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import main
from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.runtime_logging import configure_logging

hello_head_numbers = count(1)


def mark(name):
    root = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    (root / name).write_bytes(b"observed\n")


async def forbidden_science(*_args):
    mark("science-entered")
    raise AssertionError("Interrupted interpretation must not prepare or execute prediction")


async def recording_factory(factory, pending, *, stopping):
    factory._begin_setup()
    try:
        ports = SessionPorts()
        ports.worker.prepare = forbidden_science
        ports.worker.execute = forbidden_science
        root = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
        controller = await assemble_attempt(
            config=factory.config,
            pending=pending,
            claims=factory.claims,
            stopping=stopping,
            admission_stopping=factory.admission_stopping,
            start_policy=factory.start_policy,
            execution_identity=factory.execution_identity,
            selected_artifacts=factory.selected_artifacts,
            allocate_prediction=ports.allocate_prediction,
            on_prediction_retry=factory.on_prediction_retry,
            on_interpreter_failure=lambda index, failure: factory.on_operator_notice(
                failure, {"event": "interpreter_endpoint_failed", "job_ref": pending.input.job_ref, "endpoint_index": index}
            ),
            on_source_retry=lambda failure, seconds: factory.on_operator_notice(
                failure, {"event": "source_retry", "job_ref": pending.input.job_ref, "retry_seconds": seconds}
            ),
            on_source_observation=lambda failure: factory.on_operator_notice(
                failure, {"event": "source_acquisition_observation", "job_ref": pending.input.job_ref}
            ),
            allocate_reader=ports.allocate_reader,
            allocate_generator=ports.allocate_generator,
            observation_recovery=factory.observation_recovery,
            condition_recovery=factory.condition_recovery,
            on_unconfirmed_phase_progress=factory.on_unconfirmed_phase_progress,
            on_delivery_retry=factory.on_delivery_retry,
        )
        if (root / "inject-input-cleanup-failure").exists():
            retire = controller.session._input_preparation.retire

            async def failed_input_retirement():
                await retire()
                raise OSError(errno.EIO, "PRIVATE_INPUT_CLEANUP")

            controller.session._input_preparation.retire = failed_input_retirement
        if (root / "inject-condition-before-input").exists():
            prepare = controller.session.prepare

            async def after_condition_receipt(*args, report_condition, **kwargs):
                await report_condition("waiting_capacity")
                mark("condition-ack-received")
                return await prepare(*args, report_condition=report_condition, **kwargs)

            controller.session.prepare = after_condition_receipt
        return controller

    finally:
        factory._operation = None


async def observed_head(pool, request):
    response = await original_head(pool, request)
    if request.url.target == b"/chat":
        # Returning to _perform assigns its received head without an intervening
        # await. The parent signals only after observing this fixed marker.
        mark("model-head-received")
    elif request.url.target.endswith(b"/hello"):
        mark(f"hello-head-{next(hello_head_numbers)}-received")
    return response


if __name__ == "__main__":
    configure_logging("INFO")
    original_head = httpcore.AsyncHTTPProxy.handle_async_request
    with (
        patch.object(NativeAttemptFactory, "__call__", recording_factory),
        patch.object(httpcore.AsyncHTTPProxy, "handle_async_request", observed_head),
    ):
        raise SystemExit(main())
