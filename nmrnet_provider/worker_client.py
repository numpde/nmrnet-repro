"""Async scientific exchange, deliberately without a worker-retirement claim."""

from tools.container_guard import require_container

require_container()

import asyncio
import base64
import json
import re

from nmrnet_inference.predict import require_request_batch
from nmrnet_provider.native_session import NativeWorkerExited
from nmrnet_provider.worker_protocol import (
    MAX_FRAME_BYTES,
    WorkerProtocolError,
    document,
    frame,
    frame_size,
    require_allocation,
    require_seconds,
)


class WorkerReportedFailure(RuntimeError):
    """Safe message with protected worker diagnostic evidence."""

    def __init__(self, diagnostic):
        super().__init__("Scientific worker failed; preserve its private diagnostic and confirm worker retirement")
        self.diagnostic = diagnostic


class PredictionExecutionFailed(WorkerReportedFailure):
    """The worker admitted a valid execution token, then prediction failed."""


class ModelCallFailed(PredictionExecutionFailed):
    """The worker reported an exception from the admitted model call."""


class WorkerExchangeTimedOut(TimeoutError):
    """This channel's own exchange deadline elapsed, not a foreign timeout."""


class WorkerResultRejected(WorkerProtocolError):
    """Retain an unexpected decoded result reply without rendering its contents."""

    def __init__(self, reply):
        super().__init__(
            "Scientific worker returned an incomplete or malformed batch; preserve its private reply and confirm retirement."
        )
        self.diagnostic = json.dumps(reply, ensure_ascii=True, allow_nan=False, sort_keys=True)


class WorkerClient:
    """One serial framed channel; closure never proves stopped scientific work.

    The caller owns startup, the admitted native operation and confirmed child/scratch retirement.
    Geometry reading and prediction share these byte/time bounds and retained close
    observation. Their subclasses own operation-specific state and reply admission.
    """

    def __init__(self, reader, writer, *, allocation, operation_seconds, native=None):
        require_allocation(allocation)
        require_seconds(operation_seconds)
        self._reader, self._writer = reader, writer
        self._allocation = allocation
        self._seconds = operation_seconds
        self._native = native
        self._busy = False
        self._closed = False
        self._close_waiter = None

    async def _exchange(self, request):
        if self._closed or self._busy:
            raise ValueError("Scientific connection is closed or busy; it cannot accept new work")
        self._busy = True
        budget = None
        interruption = None
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._seconds
            budget = asyncio.timeout_at(deadline)
            async with budget:
                try:
                    encoded = frame(request | {"allocation": self._allocation})
                    if loop.time() >= deadline:
                        raise WorkerExchangeTimedOut("Scientific exchange deadline elapsed before dispatch")
                    self._writer.write(encoded)
                    await self._writer.drain()
                    size = frame_size(await self._reader.readexactly(4))
                    reply = document(await self._reader.readexactly(size))
                    if loop.time() >= deadline:
                        raise WorkerExchangeTimedOut("Scientific reply arrived after its exchange deadline")
                    if self._closed:
                        raise WorkerProtocolError("Scientific connection closed before the reply could be accepted")
                    if reply.get("allocation") != self._allocation:
                        raise WorkerProtocolError("Scientific reply belongs to another worker allocation")
                    if reply.get("kind") == "failure":
                        if set(reply) != {"kind", "allocation", "diagnostic"} or type(reply["diagnostic"]) is not str:
                            raise WorkerProtocolError("Scientific failure reply has no valid diagnostic")
                        raise WorkerReportedFailure(reply["diagnostic"])
                    return reply
                except asyncio.CancelledError as cause:
                    # Only this cancellation can be converted by our timeout
                    # manager; a dependency's replacement error stays foreign.
                    interruption = cause
                    raise
        except TimeoutError as cause:
            self.close()
            if interruption is not None and budget.expired() and cause.__cause__ is interruption:
                raise WorkerExchangeTimedOut(
                    "Scientific exchange exceeded its configured deadline; its response is unconfirmed."
                ) from cause
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as cause:
            primary = cause
            try:
                if self._native is not None:
                    await self._native.observe_failure(cause)
            except BaseException as failure:
                primary = failure
                raise
            finally:
                self._close_after_failure(primary)
            raise
        except (Exception, asyncio.CancelledError):
            self.close()
            raise
        finally:
            self._busy = False

    def close(self):
        """Close the local channel only; the host still owns worker retirement."""
        if self._native is not None:
            self._native.mark_interruption()
        self._closed = True
        self._writer.close()

    def _close_after_failure(self, primary):
        try:
            self.close()
        except Exception as cleanup:
            if type(primary) is not NativeWorkerExited:
                raise
            primary.retain_cleanup_failure(cleanup)

    async def wait_closed(self):
        """Observe the original local close; caller timeout never cancels it."""
        if not self._closed:
            raise ValueError("Request scientific channel closure before awaiting it.")
        if self._close_waiter is None:
            self._close_waiter = asyncio.create_task(self._writer.wait_closed())
            self._close_waiter.add_done_callback(_observe_close)
        async with asyncio.timeout(self._seconds):
            await asyncio.shield(self._close_waiter)


class PredictionClient(WorkerClient):
    """Retain one connection's current prediction handle until its single execution.

    No reconnect or retry reconstructs a live handle. The caller still owns exact
    worker retirement when this channel closes or an exchange fails.
    """

    def __init__(self, reader, writer, *, allocation, operation_seconds, declaration_sha256, native=None):
        super().__init__(reader, writer, allocation=allocation, operation_seconds=operation_seconds, native=native)
        if type(declaration_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", declaration_sha256) is None:
            raise ValueError("Prediction requires the admitted model declaration SHA-256.")
        self._declaration_sha256 = declaration_sha256
        self._prepared = None
        self._executed = False

    async def prepare(self, raw_requests):
        self._require_idle()
        self._prepared = None
        require_request_batch(raw_requests)
        # Raw bytes alone cannot exceed a frame that must also hold base64 and
        # JSON. This necessary bound limits encoding allocation; frame() remains
        # the authority for exact fit, including all expansion and envelope bytes.
        if sum(len(raw) for raw in raw_requests) > MAX_FRAME_BYTES:
            raise ValueError("Scientific preparation cannot fit the 2 MiB channel frame: request bytes alone exceed it.")
        reply = await self._exchange(
            {"operation": "prepare", "requests_base64": [base64.b64encode(raw).decode("ascii") for raw in raw_requests]}
        )
        if (
            set(reply) != {"kind", "allocation", "token", "declaration_sha256"}
            or reply["kind"] != "prepared"
            or type(reply["token"]) is not str
            or re.fullmatch(r"[0-9a-f]{32}", reply["token"]) is None
            or type(reply["declaration_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", reply["declaration_sha256"]) is None
        ):
            self.close()
            raise WorkerProtocolError("Scientific worker returned an invalid preparation token or model declaration identity")
        if reply["declaration_sha256"] != self._declaration_sha256:
            self.close()
            raise WorkerProtocolError("Scientific worker's model declaration differs from the admitted declaration; this worker's prediction was not dispatched")
        handle = object()
        self._prepared = handle, reply["token"], len(raw_requests)
        return handle

    async def execute(self, handle):
        self._require_idle()
        if self._prepared is None or handle is not self._prepared[0]:
            raise ValueError("Execution requires this client's current prepared object")
        token, count = self._prepared[1:]
        self._prepared = None
        self._executed = True
        reply = await self._exchange({"operation": "execute", "token": token})
        if reply.get("kind") in ("execution_failed", "model_call_failed"):
            self.close()
            if set(reply) != {"kind", "allocation", "diagnostic"} or type(reply["diagnostic"]) is not str:
                raise WorkerProtocolError("Scientific execution failure reply has no valid diagnostic")
            failure = ModelCallFailed if reply["kind"] == "model_call_failed" else PredictionExecutionFailed
            raise failure(reply["diagnostic"])
        if (
            set(reply) != {"kind", "allocation", "result"}
            or reply["kind"] != "result"
            or type(reply["result"]) is not list
            or len(reply["result"]) != count
        ):
            self.close()
            raise WorkerResultRejected(reply)
        return tuple(reply["result"])

    def _require_idle(self):
        if self._closed or self._busy or self._executed:
            raise ValueError("Scientific connection is closed, busy or already executed; it cannot accept new work")

    def close(self):
        self._prepared = None
        super().close()


def _observe_close(task):
    # A cancelled caller must not cause late private stream failures to be logged.
    if not task.cancelled():
        task.exception()
