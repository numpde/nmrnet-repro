"""Read one source on the private channel; host orchestration owns termination.

Uses the same frame/deadline and protected-failure contract as the prediction
worker. No models, API credentials, external network or engine control are needed.
Qualified MOL/SDF readers import RDKit only inside their native parsing call.
"""

from tools.container_guard import require_container

require_container()

import traceback

from nmrnet_provider.geometry_protocol import diagnostic_text, read_source
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.worker_protocol import (
    WorkerProtocolError,
    frame,
    receive,
    require_allocation,
    require_seconds,
    send,
)


def _reader_diagnostic_summary(records):
    """Keep a bounded private summary when the complete result cannot be returned.

    This rejection has its own 8192-character budget across all records, so even
    JSON-escaped Unicode leaves room for the safe size error. Normal replies keep
    each reader diagnostic verbatim; this summary marks any omitted evidence.
    """
    diagnostic = "\n".join(
        f"Record {record['source_record_number']}:\n{record['diagnostic']}" for record in records if record["diagnostic"]
    )
    if len(diagnostic) > 8192:
        return diagnostic[:8192] + "\n[Batch reader diagnostics truncated]"
    return diagnostic or None


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
                "Cannot return selected structures: their complete output exceeds 2 MiB; select fewer records.",
                diagnostic=_reader_diagnostic_summary(records),
            ) from None
    except InputRejected as cause:
        diagnostic = diagnostic_text(cause.diagnostic)
        if diagnostic:
            # An unsendable rejection must retain its private reader evidence
            # in the exception chain captured by the host's protected stderr.
            cause.add_note("Protected structure diagnostic:\n" + diagnostic)
        send(
            connection,
            {"kind": "rejected", "allocation": allocation, "message": str(cause), "diagnostic": diagnostic},
            io_seconds,
        )
        return
    except Exception as cause:
        # Match prediction's worker: sending inside the handler keeps the
        # original cause active if frame admission or transport also fails.
        send(
            connection,
            {"kind": "failure", "allocation": allocation, "diagnostic": "".join(traceback.format_exception(cause))},
            io_seconds,
        )
        return
    # A partial send cannot be followed by another frame. Failure here propagates
    # to the host's protected stderr capture and exact-container retirement owner.
    try:
        send(connection, reply, io_seconds)
    except Exception as cause:
        diagnostic = _reader_diagnostic_summary(records)
        if diagnostic:
            cause.add_note("Protected structure diagnostic:\n" + diagnostic)
        raise
