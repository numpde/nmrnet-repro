"""Receive immutable structures from one worker, without claiming its retirement."""

from tools.container_guard import require_container

require_container()

from nmrnet_provider.geometry_protocol import prepared_reply, source_request
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.worker_client import WorkerClient
from nmrnet_provider.worker_protocol import WorkerProtocolError


class GeometryClient(WorkerClient):
    """Read one source; the allocation owner must confirm exact worker retirement.

    Close on success, source rejection, cancellation or operational failure. A
    closed stream does not stop native parsing. Callers retain this client for
    close observation and the separate host retirement owner before replacement.
    """

    def __init__(self, reader, writer, *, allocation, operation_seconds):
        super().__init__(reader, writer, allocation=allocation, operation_seconds=operation_seconds)
        self._used = False

    async def read(self, source, *, source_format, source_unit, selection):
        """Return the complete selection or raise; never retry or return a prefix."""
        if self._used:
            raise ValueError("Structure reader accepts one source per worker allocation; retire it before replacement.")
        self._used = True
        try:
            request = source_request(source, source_format=source_format, source_unit=source_unit, selection=selection)
            reply = await self._exchange(request)
            if reply.get("kind") == "rejected":
                if set(reply) != {"kind", "allocation", "message"} or type(reply["message"]) is not str:
                    raise WorkerProtocolError("Structure worker rejection has no valid explanation.")
                raise InputRejected(reply["message"])
            return prepared_reply(reply, source, source_format=source_format, source_unit=source_unit, selection=selection)
        finally:
            self.close()
