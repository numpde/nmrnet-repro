"""Serve one serial scientific connection; only the host owner confirms retirement.

PredictionSession retains the actual prepared object here. The wire token only
correlates one preparation on this connection and never crosses into model code.
SECS 34f5915c1534278bbf61b4c6592afa44cf94ab08, worker.py::_child_loop separates
scientific messages from supervisor-owned termination. No stop acknowledgement is
accepted or issued here. The host supervisor must interrupt blocked computation.
"""

from tools.container_guard import require_container

require_container()

import base64
import binascii
import secrets
import time
import traceback

from nmrnet_provider.worker_protocol import (
    WorkerProtocolError,
    document,
    frame,
    frame_size,
    require_allocation,
    require_seconds,
)


def _exact(connection, size, deadline):
    chunks = bytearray()
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Scientific request did not arrive within its I/O deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("Scientific connection ended before the frame was complete")
        chunks.extend(chunk)
    return bytes(chunks)


def _receive(connection, seconds):
    deadline = time.monotonic() + seconds
    return document(_exact(connection, frame_size(_exact(connection, 4, deadline)), deadline))


def _send(connection, reply, seconds):
    encoded = frame(reply)
    connection.settimeout(seconds)
    connection.sendall(encoded)


def serve_connection(connection, prediction, *, allocation, io_seconds):
    """Run prepare/execute once, preserving exact input bytes and private failures.

    I/O deadlines do not interrupt synchronous scientific calls. This function
    requires an external owner able to stop its exact worker. It acquires no API,
    engine, network or artifact authority; the supplied PredictionSession owns
    scientific admission and the existing verified release loaders.
    """
    require_allocation(allocation)
    require_seconds(io_seconds)
    prepared = None
    token = None
    while True:
        request = _receive(connection, io_seconds)
        if request.get("allocation") != allocation:
            raise WorkerProtocolError("Scientific request belongs to another worker allocation")
        operation = request.get("operation")
        try:
            if operation == "prepare" and set(request) == {"operation", "allocation", "request_base64"}:
                prepared = token = None
                encoded = request["request_base64"]
                if type(encoded) is not str:
                    raise WorkerProtocolError("Preparation requires explicitly encoded request bytes")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as cause:
                    raise WorkerProtocolError("Preparation request is not valid base64") from cause
                prepared = prediction.prepare(raw)
                token = secrets.token_hex(16)
                reply = {"kind": "prepared", "allocation": allocation, "token": token}
            elif operation == "execute" and set(request) == {"operation", "allocation", "token"}:
                if token is None or request["token"] != token:
                    raise WorkerProtocolError("Execution requires this connection's current preparation token")
                handle, prepared, token = prepared, None, None
                result = prediction.execute(handle)
                reply = {"kind": "result", "allocation": allocation, "result": result}
            else:
                raise WorkerProtocolError("Scientific worker accepts only prepare and execute with their exact fields")
        except Exception as cause:
            # This private channel preserves the full exception chain. An oversized
            # diagnostic fails frame admission rather than silently truncating it;
            # the host owner must also retain the worker's stderr on abnormal exit.
            diagnostic = "".join(traceback.format_exception(cause))
            _send(connection, {"kind": "failure", "allocation": allocation, "diagnostic": diagnostic}, io_seconds)
            return
        _send(connection, reply, io_seconds)
        if operation == "execute":
            return
