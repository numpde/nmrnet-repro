"""Receive immutable structures from one worker, without claiming its retirement."""

from tools.container_guard import require_container

require_container()

from contextlib import contextmanager

from nmrnet_provider.geometry_protocol import (
    admit_upload_inventory,
    decode_source_bytes,
    diagnostic_text,
    prepared_reply,
    source_request,
    upload_inspection_request,
)
from nmrnet_provider.input_geometry import InputRejected, PreparedUploadMember
from nmrnet_provider.worker_client import WorkerClient
from nmrnet_provider.worker_protocol import WorkerProtocolError


class GeometryClient(WorkerClient):
    """Read one source; the allocation owner must confirm exact worker retirement.

    Close on success, source rejection, cancellation or operational failure. A
    closed stream does not stop native parsing. Callers retain this client for
    the native operation's transport closure and supervisor retirement before replacement.
    """

    def __init__(self, reader, writer, *, allocation, operation_seconds):
        super().__init__(reader, writer, allocation=allocation, operation_seconds=operation_seconds)
        self._used = False

    async def read(self, source, *, source_format, source_unit, selection, alternate_location=None):
        """Return the complete selection or raise; never retry or return a prefix."""
        with self._operation():
            request = source_request(
                source,
                source_format=source_format,
                source_unit=source_unit,
                selection=selection,
                alternate_location=alternate_location,
            )
            reply = await self._reply(request)
            return prepared_reply(reply, source, source_format=source_format, source_unit=source_unit, selection=selection)

    async def inspect_upload(self, source):
        with self._operation():
            reply = await self._reply(upload_inspection_request(source))
            if set(reply) != {"kind", "allocation", "inventory"} or reply["kind"] != "upload_inventory":
                raise WorkerProtocolError("Structure worker returned no Upload inventory.")
            return admit_upload_inventory(reply["inventory"])

    async def read_member(self, source, *, member, source_format, source_unit, selection, alternate_location=None):
        """Return geometry and its exact consumed member bytes, separate from ZIP bytes."""
        with self._operation():
            if type(member) is not str or not member:
                raise InputRejected("Cannot select ZIP content: supply an exact nonempty member name.")
            request = source_request(
                source,
                source_format=source_format,
                source_unit=source_unit,
                selection=selection,
                alternate_location=alternate_location,
            )
            request.update(operation="read_upload_member", member=member)
            reply = await self._reply(request)
            if set(reply) != {"kind", "allocation", "records", "source_base64"} or reply["kind"] != "member_geometries":
                raise WorkerProtocolError("Structure worker returned no complete member geometry collection.")
            selected = decode_source_bytes(reply["source_base64"])
            geometry_reply = {"kind": "geometries", "allocation": reply["allocation"], "records": reply["records"]}
            geometries = prepared_reply(
                geometry_reply,
                selected,
                source_format=source_format,
                source_unit=source_unit,
                selection=selection,
            )
            return PreparedUploadMember(geometries, selected)

    async def _reply(self, request):
        reply = await self._exchange(request)
        if reply.get("kind") == "rejected":
            if set(reply) != {"kind", "allocation", "message", "diagnostic"} or type(reply["message"]) is not str:
                raise WorkerProtocolError("Structure worker rejection has no valid explanation.")
            raise InputRejected(reply["message"], diagnostic=diagnostic_text(reply["diagnostic"]))
        return reply

    @contextmanager
    def _operation(self):
        if self._used:
            raise ValueError("Structure reader accepts one source per worker allocation; retire it before replacement.")
        self._used = True
        try:
            yield
        finally:
            self.close()
