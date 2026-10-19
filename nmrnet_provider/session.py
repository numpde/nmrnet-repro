"""Bind one admitted direct-coordinate Job to science and confirmed retirement.

The protocol owner is provider_core.analysis.AnalysisSession. Core owns current
server observations, execution entry, durable terminal choice and publication.
This binding owns the scientific handle, result admission and worker cleanup.
Interpreted, generated and uploaded inputs need their own preparation integration;
this direct route never guesses their meaning or silently substitutes geometry.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import copy
import json
from enum import Enum, auto

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.result import RESULT_SCHEMA_ID, ResultPlan
from nmrnet_provider.submission import prepare_direct_job
from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import AcknowledgedAttempt, LocalExecutionPhase, ProtectedEvidence
from provider_core.job_input import VerifiedJobInput


class ResultAssemblyFailed(RuntimeError):
    """Keep received scientific evidence when result assembly cannot complete."""

    def __init__(self, worker_result):
        super().__init__("Cannot assemble the scientific result; preserve the received worker evidence and underlying cause.")
        self.worker_result = worker_result


class SessionRetirementFailed(RuntimeError):
    """Safe context with ordered private failures from independent cleanup owners."""

    def __init__(self, failures):
        super().__init__("Scientific retirement is unconfirmed; preserve the allocation and all private cleanup failures.")
        self.failures = tuple(failures)
        self.cause = self.failures[0][1]


class _Phase(Enum):
    NEW = auto()
    PREPARING = auto()
    PREPARED = auto()
    EXECUTING = auto()
    FINISHED = auto()
    RETIRING = auto()
    RETIRED = auto()


def _evidence(document):
    return ProtectedEvidence(
        "nmrnet.scientific_evidence.v1",
        json.dumps(document, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii"),
    )


class NMRNetSession:
    """A single-use session created before preparation acquires worker resources.

    Composition supplies allocation-bound worker/retirement ports and admitted
    runtime facts. Metadata is copied now so later caller mutation cannot alter
    result planning. This object has no API credentials or publication authority.
    """

    def __init__(self, *, worker, retirement, execution_identity, selected_artifacts, runtime_expectations):
        self._worker = worker
        self._retirement = retirement
        self._facts = copy.deepcopy(
            {
                "execution_identity": execution_identity,
                "selected_artifacts": selected_artifacts,
                "runtime_expectations": runtime_expectations,
            }
        )
        self._phase = _Phase.NEW
        self._prepared = None
        self.last_retirement_failure = None

    async def prepare(self, acknowledged, instructions):
        if self._phase is not _Phase.NEW:
            raise ValueError("Scientific session already accepted preparation or retirement; create a new owned allocation.")
        self._phase = _Phase.PREPARING
        if (
            type(acknowledged) is not AcknowledgedAttempt
            or type(instructions) is not VerifiedJobInput
            or acknowledged.local_phase is not LocalExecutionPhase.NOT_ENTERED
            or acknowledged.admission.state != "in_progress"
            or acknowledged.start.input != instructions.identity
        ):
            raise ValueError("Scientific preparation requires the active unexecuted Attempt and its exact verified input.")
        try:
            geometry = prepare_direct_job(instructions)
        except InputRejected as cause:
            self._phase = _Phase.FINISHED
            return AnalysisFailure(
                "invalid_input",
                str(cause),
                _evidence(
                    {
                        "input_fingerprint": instructions.identity.input_fingerprint,
                        "preparation_failure": str(cause),
                    }
                ),
            )
        plan = ResultPlan(
            geometry,
            preparation_method="direct_coordinate_json",
            job_ref=instructions.job_ref,
            execution_attempt_ref=acknowledged.admission.execution_attempt_ref,
            input_fingerprint=instructions.identity.input_fingerprint,
            **self._facts,
        )
        handle = PreparedAnalysis(ProtectedEvidence("nmrnet.prepared_input.v1", plan.prepared_identity))
        worker_handle = await self._worker.prepare(geometry.request_bytes)
        if self._phase is not _Phase.PREPARING:
            raise ValueError("Scientific preparation finished after retirement began; its handle cannot be used.")
        self._prepared = handle, worker_handle, plan
        self._phase = _Phase.PREPARED
        return handle

    async def execute(self, handle):
        if self._phase is not _Phase.PREPARED or handle is not self._prepared[0]:
            raise ValueError("Scientific execution requires this session's current prepared handle.")
        _, worker_handle, plan = self._prepared
        self._prepared = None
        self._phase = _Phase.EXECUTING
        result = await self._worker.execute(worker_handle)
        try:
            if self._phase is not _Phase.EXECUTING:
                raise ValueError("Scientific result arrived after retirement began; it cannot be accepted.")
            content = plan.finish(result)
            evidence = _evidence(
                {
                    "request_sha256": result["request_sha256"],
                    "provenance": result["provenance"],
                    "private_warnings": result["private_warnings"],
                }
            )
        except Exception as cause:
            # The model already returned. Keep that exact outcome if assembly or
            # evidence capacity fails; core must not rerun science to recover it.
            raise ResultAssemblyFailed(result) from cause
        self._phase = _Phase.FINISHED
        return AnalysisSuccess(RESULT_SCHEMA_ID, content, evidence)

    async def retire(self):
        if self._phase is _Phase.RETIRED:
            return
        self._phase = _Phase.RETIRING
        self._prepared = None
        failures = []
        try:
            self._worker.close()
        except Exception as cause:
            failures.append(("channel_close", cause))
        try:
            await self._retirement.retire()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("host_retirement", cause))
        try:
            await self._worker.wait_closed()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("channel_observation", cause))
        if failures:
            self.last_retirement_failure = SessionRetirementFailed(failures)
            # Cancellation remains cancellation; the retained failure still names
            # the earliest cause and every later independent cleanup observation.
            for _, cause in failures:
                if isinstance(cause, asyncio.CancelledError):
                    raise cause from self.last_retirement_failure
            raise self.last_retirement_failure from None
        self._phase = _Phase.RETIRED
