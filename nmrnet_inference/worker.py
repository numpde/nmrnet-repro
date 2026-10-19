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
import json
import secrets
import traceback

from nmrnet_inference.predict import MAX_GEOMETRIES, require_request_batch
from nmrnet_provider.worker_protocol import (
    WorkerProtocolError,
    receive,
    require_allocation,
    require_seconds,
    send,
)


def serve_connection(connection, prediction, *, allocation, io_seconds):
    """Accept replacement inputs until one execution, then close the exchange.

    Each prepare replaces the previous input and invalidates its execution token.
    Execute consumes the current token; success or a handled failure ends this
    exchange. Original input bytes and private failure causes are preserved.

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
        request = receive(connection, io_seconds)
        if request.get("allocation") != allocation:
            raise WorkerProtocolError("Scientific request belongs to another worker allocation")
        operation = request.get("operation")
        try:
            if operation == "prepare" and set(request) == {"operation", "allocation", "requests_base64"}:
                prepared = token = None
                encoded = request["requests_base64"]
                if (
                    type(encoded) is not list
                    or not 1 <= len(encoded) <= MAX_GEOMETRIES
                    or any(type(item) is not str for item in encoded)
                ):
                    raise WorkerProtocolError("Preparation requires a bounded ordered list of encoded geometry requests")
                try:
                    raw_requests = tuple(base64.b64decode(item, validate=True) for item in encoded)
                except (ValueError, binascii.Error) as cause:
                    raise WorkerProtocolError("Preparation request is not valid base64") from cause
                require_request_batch(raw_requests)
                prepared = prediction.prepare(raw_requests)
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
            send(connection, {"kind": "failure", "allocation": allocation, "diagnostic": diagnostic}, io_seconds)
            return
        try:
            send(connection, reply, io_seconds)
        except Exception as cause:
            if operation == "execute":
                # No retry can establish whether a partially sent reply arrived.
                # The host's protected stderr must retain the completed science.
                try:
                    cause.add_note(json.dumps({"completed_outcomes": result}, sort_keys=True))
                except Exception as recording_failure:
                    raise cause from recording_failure
            raise
        if operation == "execute":
            return


def serve_released_connection(connection, *, allocation, io_seconds, declaration_path, weight_root, checkpoint_root):
    """Compose the verified release loaders and existing scientific session.

    Paths are allocation configuration, never fields accepted from the wire.
    Construction reads no model bytes; preparation and execution retain their
    existing admission/decoding order. The external owner still stops the worker.
    """
    from nmrnet_inference.release import (  # noqa: PLC0415 -- Defer scientific wiring.
        ReleasedPredictionSession,
        SelectedReleaseModels,
    )

    models = SelectedReleaseModels(
        declaration_path=declaration_path,
        weight_root=weight_root,
        checkpoint_root=checkpoint_root,
    )
    prediction = ReleasedPredictionSession(models)
    serve_connection(connection, prediction, allocation=allocation, io_seconds=io_seconds)
