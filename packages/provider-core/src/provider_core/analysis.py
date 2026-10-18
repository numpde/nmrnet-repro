"""Binding-owned scientific outcomes and the asynchronous per-Attempt session.

The split follows NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_lifecycle.py::prepare_execution/execute_prepared and
runner_session.py::generate. Its channel retirement does not prove process exit;
each consumer must qualify its actual worker/controller retirement contract.
"""

from provider_core.container_guard import require_container

require_container()

from dataclasses import dataclass, field
from typing import Protocol

from provider_core.attempt_record import AcknowledgedAttempt, ProtectedEvidence
from provider_core.job_input import VerifiedJobInput


@dataclass(frozen=True, slots=True)
class AnalysisSuccess:
    result_schema_id: str
    content: bytes = field(repr=False)
    evidence: ProtectedEvidence = field(repr=False)


@dataclass(frozen=True, slots=True)
class AnalysisFailure:
    """A binding-classified public failure, not a catch-all for runtime exceptions."""

    code: str
    message: str = field(repr=False)
    evidence: ProtectedEvidence = field(repr=False)


class AnalysisSession(Protocol):
    """One bounded async owner existing before preparation can acquire resources.

    Preparation binds the Attempt, current generation and exact admitted inputs to
    a live handle. Execute verifies that handle belongs to this session and consumes
    it before work. Core neither constructs nor serializes this authority.
    Calls must yield while workers run and impose their own aggregate deadlines.
    Session I/O must not share the executor's single-exchange API transport:
    server observation continues concurrently while preparation or execution runs.

    Retire must observe that this exact Attempt's work stopped and required grants
    were released, including partially prepared or interrupted work. It returns
    None only after confirmation, otherwise raises. Warm worker reuse additionally
    requires the binding's qualified quiescence policy; core grants no such reuse.
    A stop request, closed channel or cancelled Python task is not confirmation.
    """

    async def prepare(self, acknowledged: AcknowledgedAttempt, instructions: VerifiedJobInput) -> object | AnalysisFailure: ...

    async def execute(self, prepared: object) -> AnalysisSuccess | AnalysisFailure: ...

    async def retire(self) -> None: ...


async def retire_session(session):
    """Confirm the protocol result for both pre-start and execution cleanup."""
    result = await session.retire()
    if result is not None:
        raise TypeError("Session retirement must return None after confirmed cleanup, otherwise raise")
