"""Receive one generated collection; channel closure never proves retirement."""

from tools.container_guard import require_container

require_container()

from dataclasses import asdict

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.smiles_protocol import prepared_conformers, require_generation_deadline, smiles_request
from nmrnet_provider.worker_client import WorkerClient
from nmrnet_provider.worker_protocol import WorkerProtocolError


class SmilesClient(WorkerClient):
    """One generation attempt with operator budgets, never interpreter work knobs.

    After any attempt, retain the client for close observation and the separate
    host allocation for confirmed retirement before replacement or submission.
    """

    def __init__(self, reader, writer, *, allocation, settings, operation_seconds, native=None):
        require_generation_deadline(settings, operation_seconds)
        super().__init__(reader, writer, allocation=allocation, operation_seconds=operation_seconds, native=native)
        self._settings = settings
        self._used = False

    async def generate(self, source, requested_count):
        """Return the whole frozen collection or a truthful failure; never retry."""
        if self._used:
            raise ValueError("Conformer generation accepts one attempt per worker allocation; retire it before replacement.")
        self._used = True
        failure = None
        try:
            reply = await self._exchange(smiles_request(source, requested_count))
            if reply.get("kind") == "rejected":
                if (
                    set(reply) != {"kind", "allocation", "message", "diagnostic"}
                    or type(reply["message"]) is not str
                    or (reply["diagnostic"] is not None and type(reply["diagnostic"]) is not str)
                ):
                    raise WorkerProtocolError(
                        "Conformer worker rejection must retain its explanation and optional private diagnostic."
                    )
                raise InputRejected(reply["message"], diagnostic=reply["diagnostic"])
            return prepared_conformers(reply, source, requested_count, **asdict(self._settings))
        except BaseException as cause:
            failure = cause
            raise
        finally:
            self._close_after_failure(failure)
