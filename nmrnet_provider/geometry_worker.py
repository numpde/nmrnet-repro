"""Read one source on the private channel; host orchestration owns termination.

Uses the same frame/deadline and protected-failure contract as the prediction
worker. No models, API credentials, external network or engine control are needed.
Native readers will enter read_source only after their separate qualification.
"""

from tools.container_guard import require_container

require_container()

import traceback

from nmrnet_provider.geometry_protocol import read_source
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.worker_protocol import (
    WorkerProtocolError,
    frame,
    receive,
    require_allocation,
    require_seconds,
    send,
)


def serve_geometry_connection(connection, *, allocation, io_seconds):
    """Return all selected structures or one failure, never a partial selection.

    Socket deadlines bound I/O, not synchronous reader work. The host must stop
    this exact worker on cancellation, parser stalls or lost replies.
    """
    require_allocation(allocation)
    require_seconds(io_seconds)
    request = receive(connection, io_seconds)
    if request.get("allocation") != allocation:
        raise WorkerProtocolError("Structure request belongs to another worker allocation.")
    del request["allocation"]
    try:
        records = read_source(request)
        reply = {"kind": "geometries", "allocation": allocation, "records": records}
        try:
            frame(reply)
        except WorkerProtocolError:
            raise InputRejected(
                "Cannot return selected structures: their complete output exceeds 2 MiB; select fewer records."
            ) from None
    except InputRejected as cause:
        reply = {"kind": "rejected", "allocation": allocation, "message": str(cause)}
    except Exception as cause:
        reply = {"kind": "failure", "allocation": allocation, "diagnostic": "".join(traceback.format_exception(cause))}
    # A partial send cannot be followed by another frame. Failure here propagates
    # to the host's protected stderr capture and exact-container retirement owner.
    send(connection, reply, io_seconds)
