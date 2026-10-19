"""Generate once on the shared private framing protocol without model authority.

The host supplies fixed budgets and owns hard retirement. The request carries only
SMILES and N. Success, full native shortfall and output-capacity failure share the
existing whole-frame limit; no partially sent reply is followed by another frame.
"""

from tools.container_guard import require_container

require_container()

import traceback
from dataclasses import asdict

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.smiles import ConformerShortfall, generate_conformers
from nmrnet_provider.smiles_protocol import conformer_reply, read_smiles_request, require_generation_deadline
from nmrnet_provider.worker_protocol import WorkerProtocolError, receive, require_allocation, send


def serve_smiles_connection(connection, *, allocation, settings, io_seconds):
    """Serve one generation request; I/O deadlines do not terminate native work."""
    require_allocation(allocation)
    require_generation_deadline(settings, io_seconds)
    request = receive(connection, io_seconds)
    if request.get("allocation") != allocation:
        raise WorkerProtocolError("Conformer request belongs to another worker allocation.")
    del request["allocation"]
    try:
        source, requested_count = read_smiles_request(request)
        try:
            batch = generate_conformers(source, requested_count, **asdict(settings))
        except ConformerShortfall as shortfall:
            batch = shortfall.partial
        reply = conformer_reply(batch, allocation=allocation)
    except InputRejected as cause:
        if cause.diagnostic:
            cause.add_note("Protected conformer diagnostic:\n" + cause.diagnostic)
        send(
            connection,
            {"kind": "rejected", "allocation": allocation, "message": str(cause), "diagnostic": cause.diagnostic},
            io_seconds,
        )
        return
    except Exception as cause:
        # Sending inside this handler retains the original native cause if the
        # failure reply itself cannot be sent. There is no replacement frame.
        send(
            connection,
            {"kind": "failure", "allocation": allocation, "diagnostic": "".join(traceback.format_exception(cause))},
            io_seconds,
        )
        return
    try:
        send(connection, reply, io_seconds)
    except Exception as cause:
        if batch.diagnostic:
            cause.add_note("Protected conformer diagnostic:\n" + batch.diagnostic)
        raise
