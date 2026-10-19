"""Async scientific exchange, deliberately without a worker-retirement claim."""

from tools.container_guard import require_container

require_container()

import asyncio
import base64
import re

from nmrnet_inference.predict import MAX_INPUT_BYTES
from nmrnet_provider.worker_protocol import (
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


class WorkerClient:
    """One serial framed channel; closure never proves stopped scientific work.

    The caller owns startup, the admitted allocation and exact-container retirement.
    Geometry reading and prediction share these byte/time bounds and retained close
    observation. Their subclasses own operation-specific state and reply admission.
    """

    def __init__(self, reader, writer, *, allocation, operation_seconds):
        require_allocation(allocation)
        require_seconds(operation_seconds)
        self._reader, self._writer = reader, writer
        self._allocation = allocation
        self._seconds = operation_seconds
        self._busy = False
        self._closed = False
        self._close_waiter = None

    async def _exchange(self, request):
        if self._closed or self._busy:
            raise ValueError("Scientific connection is closed or busy; it cannot accept new work")
        self._busy = True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._seconds
            async with asyncio.timeout_at(deadline):
                encoded = frame(request | {"allocation": self._allocation})
                if loop.time() >= deadline:
                    raise TimeoutError("Scientific exchange deadline elapsed before dispatch")
                self._writer.write(encoded)
                await self._writer.drain()
                size = frame_size(await self._reader.readexactly(4))
                reply = document(await self._reader.readexactly(size))
                if loop.time() >= deadline:
                    raise TimeoutError("Scientific reply arrived after its exchange deadline")
                if self._closed:
                    raise WorkerProtocolError("Scientific connection closed before the reply could be accepted")
                if reply.get("allocation") != self._allocation:
                    raise WorkerProtocolError("Scientific reply belongs to another worker allocation")
                if reply.get("kind") == "failure":
                    if set(reply) != {"kind", "allocation", "diagnostic"} or type(reply["diagnostic"]) is not str:
                        raise WorkerProtocolError("Scientific failure reply has no valid diagnostic")
                    raise WorkerReportedFailure(reply["diagnostic"])
                return reply
        except (Exception, asyncio.CancelledError):
            self.close()
            raise
        finally:
            self._busy = False

    def close(self):
        """Close the local channel only; the host still owns worker retirement."""
        self._closed = True
        self._writer.close()

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

    def __init__(self, reader, writer, *, allocation, operation_seconds):
        super().__init__(reader, writer, allocation=allocation, operation_seconds=operation_seconds)
        self._prepared = None
        self._executed = False

    async def prepare(self, raw):
        self._require_idle()
        self._prepared = None
        if type(raw) is not bytes:
            raise TypeError("Scientific preparation requires exact request bytes")
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("Scientific preparation exceeds the existing request-byte limit")
        reply = await self._exchange({"operation": "prepare", "request_base64": base64.b64encode(raw).decode("ascii")})
        if (
            set(reply) != {"kind", "allocation", "token"}
            or reply["kind"] != "prepared"
            or type(reply["token"]) is not str
            or re.fullmatch(r"[0-9a-f]{32}", reply["token"]) is None
        ):
            self.close()
            raise WorkerProtocolError("Scientific worker returned no valid preparation token")
        handle = object()
        self._prepared = handle, reply["token"]
        return handle

    async def execute(self, handle):
        self._require_idle()
        if self._prepared is None or handle is not self._prepared[0]:
            raise ValueError("Execution requires this client's current prepared object")
        token = self._prepared[1]
        self._prepared = None
        self._executed = True
        reply = await self._exchange({"operation": "execute", "token": token})
        if set(reply) != {"kind", "allocation", "result"} or reply["kind"] != "result":
            self.close()
            raise WorkerProtocolError("Scientific worker returned no prediction result")
        return reply["result"]

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
