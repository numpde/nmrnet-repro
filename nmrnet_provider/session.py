"""Bind one admitted Job's prepared geometries to science and confirmed retirement.

The protocol owner is provider_core.analysis.AnalysisSession. Core owns current
server observations, execution entry, terminal choice and delivery handoff.
This binding owns the scientific handle, result admission and worker cleanup.
The required preparation owner supplies admitted sources and owns their cleanup.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import copy
import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum, auto

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import InputSelectionTimeLimit, InputWorkerCallLimit, PreparationRetirementFailed
from nmrnet_provider.native_session import NativeWorkerExited
from nmrnet_provider.interpreter import ModelReportedInputProblem
from nmrnet_provider.prepared_input import GeneratedJobInput, PreparedJobInput
from nmrnet_provider.result import RESULT_SCHEMA_ID, ResultCapacityExceeded, ResultPlan
from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import ConformerOutputLimit, conformer_evidence
from nmrnet_provider.worker_client import ModelCallFailed, PredictionExecutionFailed
from provider_core.analysis import AnalysisFailure, AnalysisSuccess, ObservedWorkerCrash, PreparedAnalysis
from provider_core.attempt_record import AcknowledgedAttempt, LocalExecutionPhase, ProtectedEvidence
from provider_core.job_input import VerifiedJobInput
from provider_core.task_boundary import contain_task_exit


class ResultAssemblyFailed(RuntimeError):
    """Keep the raw reply in memory; process reports retain only metadata."""

    def __init__(self, worker_result, prepared_identity):
        super().__init__(
            "Cannot assemble the scientific result; the full worker reply remains only in memory. "
            "The process report contains partial metadata and the underlying cause."
        )
        self.worker_result = worker_result
        self.prepared_identity = prepared_identity


class PreparationEvidenceFailed(RuntimeError):
    """Keep unadmitted evidence in memory for a bounded private process report."""

    def __init__(self, document, *, preparation_failure=None):
        super().__init__(
            "Cannot retain preparation evidence before prediction; its full payload remains only in memory. "
            "The process report contains partial diagnostic evidence and the underlying cause."
        )
        self.document = document
        self.preparation_failure = preparation_failure


class SessionRetirementFailed(RuntimeError):
    """Safe context with ordered private failures from independent cleanup owners."""

    def __init__(self, failures):
        super().__init__("Scientific retirement is unconfirmed; preserve the allocation and all private cleanup failures.")
        self.failures = tuple(failures)
        self.cause = self.failures[0][1]
        self.interruption_cause = None


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


def _scientific_evidence_document(outcomes, identity):
    document = {
        "geometries": [
            {key: item[key] for key in ("request_sha256", "prediction", "provenance", "private_warnings")} for item in outcomes
        ]
    }
    for key in ("preparation_diagnostics", "interpreter_diagnostics"):
        if key in identity:
            document[key] = identity[key]
    return document


def _unrounded_prediction(prediction):
    # ResultPlan already admitted every raw number. Keep its exact binary64
    # value, without the potentially 309-character spelling of a Python int.
    return {
        "unit": prediction["unit"],
        "predictions": [{**row, "shift_ppm": float(row["shift_ppm"])} for row in prediction["predictions"]],
    }


def _preparation_diagnostics(prepared):
    """Project private observations without source text, coordinates or stereo."""
    if type(prepared) is GeneratedJobInput:
        return [
            {
                "source_sha256": prepared.source.source_sha256,
                "provenance_sha256": hashlib.sha256(prepared.generated.provenance).hexdigest(),
                "diagnostic": prepared.generated.diagnostic,
            }
        ]
    return [
        {
            "geometry_id": f"geometry-{index}",
            "source_sha256": geometry.source_sha256,
            "source_record_number": geometry.source_record_number,
            "diagnostic": geometry.diagnostic,
        }
        for index, geometry in enumerate(prepared.geometries, 1)
        if geometry.diagnostic is not None
    ]


def _prepared_identity(plan, prepared, interpreter_diagnostics):
    document = json.loads(plan.prepared_identity)
    diagnostics = _preparation_diagnostics(prepared)
    if diagnostics:
        document["preparation_diagnostics"] = diagnostics
    if interpreter_diagnostics is not None:
        document["interpreter_diagnostics"] = json.loads(interpreter_diagnostics)
    try:
        # Carry these observations in the prepared evidence owned by the live
        # Attempt; core applies its existing evidence limit.
        return ProtectedEvidence(
            "nmrnet.prepared_input.v1",
            json.dumps(document, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii"),
        )
    except Exception as cause:
        raise PreparationEvidenceFailed(document) from cause


def _generation_failure(cause):
    """Keep native provenance and ordered payload identities for failed Job evidence."""
    if isinstance(cause, ConformerShortfall):
        kind = "conformer_shortfall"
        evidence = conformer_evidence(cause.partial)
    else:
        kind = "conformer_output_limit"
        reply = json.loads(cause.diagnostic)
        # Allocation identity belongs to the transport owner. The protocol has
        # already admitted this summary; retain only scientific failure evidence.
        evidence = {key: reply[key] for key in ("coordinates_available", "provenance_utf8", "diagnostic", "conformers")}
    provenance = evidence["provenance_utf8"].encode("utf-8")
    return {
        "kind": kind,
        "requested_count": json.loads(provenance)["requested_count"],
        "actual_count": len(evidence["conformers"]),
        "provenance_sha256": hashlib.sha256(provenance).hexdigest(),
        **evidence,
    }


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedExecution:
    handle: PreparedAnalysis
    requests: tuple[bytes, ...]
    plan: ResultPlan


class NMRNetSession:
    """A single-use session created before preparation acquires worker resources.

    Composition supplies a Job-bound allocator used only after input succeeds.
    Its returned runtime is observed at acquisition. Metadata is copied so later
    caller mutation cannot alter result planning. This object has no API
    credentials or publication authority.
    """

    def __init__(
        self,
        *,
        allocate_prediction,
        execution_identity,
        selected_artifacts,
        input_preparation,
        on_prediction_failure=None,
        max_retries=0,
        prediction_seconds=None,
    ):
        if not callable(allocate_prediction):
            raise ValueError("Scientific session requires a prediction allocation owner.")
        if type(max_retries) is not int or max_retries not in (0, 1):
            raise ValueError("Scientific session permits zero or one prediction retry.")
        if max_retries and (type(prediction_seconds) not in (int, float) or not 0 < prediction_seconds
                            or not math.isfinite(2 * prediction_seconds)):
            raise ValueError("Prediction retry requires a finite positive prediction deadline.")
        self._max_retries = max_retries
        self._prediction_seconds = prediction_seconds
        self._allocate_prediction = allocate_prediction
        self._worker = self._retirement = None
        self._prediction_retirement_task = None
        self._prediction_setup = None
        self._input_preparation = input_preparation
        self._on_prediction_failure = on_prediction_failure
        self._prediction_pending = False
        self._acknowledged = None
        self._facts = copy.deepcopy(
            {
                "execution_identity": execution_identity,
                "selected_artifacts": selected_artifacts,
            }
        )
        self._phase = _Phase.NEW
        self._prepared = None
        self.last_retirement_failure = None

    # Core requires this named port; NMRNet currently emits no preparation conditions.
    async def prepare(self, acknowledged, instructions, *, report_condition):  # noqa: ARG002
        if self._phase is not _Phase.NEW:
            raise ValueError("Scientific session already accepted preparation or retirement; create a new session.")
        self._phase = _Phase.PREPARING
        if (
            type(acknowledged) is not AcknowledgedAttempt
            or type(instructions) is not VerifiedJobInput
            or acknowledged.local_phase is not LocalExecutionPhase.NOT_ENTERED
            or acknowledged.admission.state != "in_progress"
            or acknowledged.start.input != instructions.identity
        ):
            raise ValueError("Scientific preparation requires the active unexecuted Attempt and its exact verified input.")
        self._acknowledged = acknowledged
        try:
            prepared = await self._input_preparation.prepare(instructions)
        except (NativeWorkerExited, PreparationRetirementFailed) as cause:
            original = cause
            while type(original) is PreparationRetirementFailed:
                original = original.operation_failure
            if type(original) is not NativeWorkerExited:
                raise
            raise self._worker_crash(original, cause=cause) from None
        except (
            InputRejected,
            ConformerShortfall,
            ConformerOutputLimit,
            ModelReportedInputProblem,
            InputWorkerCallLimit,
            InputSelectionTimeLimit,
        ) as cause:
            return self._failed_preparation(cause, instructions)
        if self._phase is not _Phase.PREPARING:
            raise ValueError("Input preparation finished after retirement began; scientific dispatch is blocked.")
        if type(prepared) not in (PreparedJobInput, GeneratedJobInput):
            raise TypeError("Scientific preparation requires the input owner's frozen supplied or generated geometry collection.")
        try:
            self._facts["runtime_expectations"] = await self._acquire_prediction(_Phase.PREPARING)
        except NativeWorkerExited as cause:
            raise self._worker_crash(cause, cause=cause) from None
        try:
            plan = ResultPlan(
                prepared,
                job_ref=instructions.job_ref,
                execution_attempt_ref=acknowledged.admission.execution_attempt_ref,
                input_fingerprint=instructions.identity.input_fingerprint,
                **self._facts,
            )
        except ResultCapacityExceeded as cause:
            return self._failed_preparation(cause, instructions)
        diagnostics = self._input_preparation.interpreter_diagnostics
        handle = PreparedAnalysis(_prepared_identity(plan, prepared, diagnostics))
        reservation = _scientific_evidence_document(plan.scientific_evidence_reservation(), json.loads(handle.identity.content))
        try:
            _evidence(reservation)
        except Exception as cause:
            raise PreparationEvidenceFailed({"scientific_evidence_reservation": reservation}) from cause
        # Core binds this identity to the live Attempt before execute(). Keep
        # the admitted bytes here so the worker receives that same preparation.
        if type(prepared) is GeneratedJobInput:
            requests = tuple(item.request_bytes for item in prepared.generated.conformers)
        else:
            requests = tuple(geometry.request_bytes for geometry in prepared.geometries)
        self._prepared = _PreparedExecution(handle, requests, plan)
        self._phase = _Phase.PREPARED
        return handle

    def _failed_preparation(self, cause, instructions):
        if self._phase is not _Phase.PREPARING:
            raise ValueError("Preparation failure arrived after retirement began; it cannot become a Job outcome.") from cause
        self._phase = _Phase.FINISHED
        document = {"input_fingerprint": instructions.identity.input_fingerprint}
        try:
            if self._input_preparation.interpreter_diagnostics is not None:
                document["interpreter_diagnostics"] = json.loads(self._input_preparation.interpreter_diagnostics)
            if type(cause) is InputWorkerCallLimit:
                code = "input_worker_call_limit"
                document[code] = {"maximum_calls": cause.maximum_calls, "calls_started": cause.calls_started}
            elif type(cause) is InputSelectionTimeLimit:
                code = "input_selection_time_limit"
                document[code] = {"seconds": cause.seconds}
            elif isinstance(cause, ModelReportedInputProblem):
                code = "interpreter_reported_input_problem"
                document["model_report"] = {
                    "issue": cause.issue,
                    "explanation": cause.explanation,
                    "independently_verified": False,
                    "interpretation": json.loads(cause.observation),
                }
            elif isinstance(cause, ResultCapacityExceeded):
                code = "result_size_limit"
            elif isinstance(cause, InputRejected):
                code = "invalid_input"
                document.update(preparation_failure=str(cause), diagnostic=cause.diagnostic)
            else:
                generation = _generation_failure(cause)
                code = generation["kind"]
                document["generation_failure"] = generation
            evidence = _evidence(document)
        except Exception as evidence_failure:
            # Retain the original typed failure too: a capacity/encoding error
            # must not erase its complete private observations or partial batch.
            raise PreparationEvidenceFailed(document, preparation_failure=cause) from evidence_failure
        return AnalysisFailure(code, str(cause), evidence)

    # Core requires this named port; NMRNet currently emits no execution conditions.
    async def execute(self, handle, *, report_condition):  # noqa: ARG002
        if self._phase is not _Phase.PREPARED or handle is not self._prepared.handle:
            raise ValueError("Scientific execution requires this session's current prepared handle.")
        prepared = self._prepared
        self._prepared = None
        self._phase = _Phase.EXECUTING
        try:
            result = await self._predict(prepared.requests)
        except PredictionExecutionFailed as cause:
            if self._phase is not _Phase.EXECUTING:
                raise ValueError("Scientific failure arrived after retirement began; it cannot be accepted.") from cause
            try:
                diagnostic = json.dumps(cause.diagnostic, ensure_ascii=True).encode("ascii")
                prediction_failure = {
                    "diagnostic_encoding": "JSON string encoded as ASCII",
                    "diagnostic_bytes": len(diagnostic),
                    "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
                }
                if type(cause) is ModelCallFailed and cause.retry_not_started is not None:
                    prediction_failure["retry_not_started"] = cause.retry_not_started
                evidence = _evidence({"prediction_failure": prediction_failure})
            except Exception as evidence_failure:
                raise evidence_failure from cause
            if self._on_prediction_failure is not None:
                self._on_prediction_failure(cause, self._acknowledged)
            self._phase = _Phase.FINISHED
            return AnalysisFailure("prediction_execution_failed", "Prediction did not return a complete verified result.", evidence)
        try:
            if self._phase is not _Phase.EXECUTING:
                raise ValueError("Scientific result arrived after retirement began; it cannot be accepted.")
            content = prepared.plan.finish(result)
            document = _scientific_evidence_document(
                ({**item, "prediction": _unrounded_prediction(item["prediction"])} for item in result),
                json.loads(prepared.handle.identity.content),
            )
            evidence = _evidence(document)
        except Exception as cause:
            # The model already returned. Keep that exact outcome if assembly or
            # evidence capacity fails; core must not rerun science to recover it.
            raise ResultAssemblyFailed(result, prepared.handle.identity) from cause
        self._phase = _Phase.FINISHED
        return AnalysisSuccess(RESULT_SCHEMA_ID, content, evidence)

    def _require_prediction_active(self):
        if self._phase is not _Phase.EXECUTING:
            raise ValueError("Scientific retirement has begun; prediction dispatch is blocked.")

    async def _predict(self, requests):
        """Execute the frozen batch, retaining original owners across a permitted retry."""
        try:
            if not self._max_retries:
                return await self._predict_once(requests)
            deadline = asyncio.get_running_loop().time() + 2 * self._prediction_seconds
            try:
                async with asyncio.timeout_at(deadline):
                    return await self._predict_once(requests)
            except ModelCallFailed as first:
                if type(first) is not ModelCallFailed:
                    raise
                return await self._retry_prediction(requests, first, deadline)
        except NativeWorkerExited as cause:
            raise self._worker_crash(cause, cause=cause) from None

    async def _retry_prediction(self, requests, first, deadline):
        self._require_prediction_active()
        failures = await self._retire_prediction(self._worker, self._retirement)
        if failures:
            try:
                self._raise_retirement_failure(failures)
            except asyncio.CancelledError as cleanup:
                previous = cleanup.__cause__
                raise cleanup from BaseExceptionGroup("Prediction and retirement interruption", (first, previous))
            except Exception as cleanup:
                raise BaseExceptionGroup("Prediction and its retirement both failed", (first, cleanup)) from None
        self._require_prediction_active()
        if asyncio.get_running_loop().time() >= deadline:
            first.retry_not_started = "The execution budget expired during worker retirement."
            raise first
        self._worker = self._retirement = self._prediction_retirement_task = None
        acquisition_budget = asyncio.timeout_at(deadline)
        try:
            async with acquisition_budget:
                runtime = await self._acquire_prediction(_Phase.EXECUTING)
        except NativeWorkerExited:
            raise
        except asyncio.CancelledError as interruption:
            previous = interruption.__cause__
            if previous is None:
                raise interruption from first
            raise interruption from BaseExceptionGroup("Prediction and replacement interruption", (first, previous))
        except TimeoutError as acquisition:
            cancelled = acquisition.__cause__
            if acquisition_budget.expired() and isinstance(cancelled, asyncio.CancelledError) and cancelled.__cause__ is None:
                first.retry_not_started = "The execution budget expired during replacement setup."
                raise first from acquisition
            raise ExceptionGroup("Prediction failed and replacement could not be acquired", (first, acquisition)) from None
        except Exception as acquisition:
            raise BaseExceptionGroup("Prediction failed and replacement could not be acquired", (first, acquisition)) from None
        if runtime != self._facts["runtime_expectations"]:
            raise ValueError("Cannot retry prediction: replacement runtime differs from the admitted runtime.") from first
        self._require_prediction_active()
        if asyncio.get_running_loop().time() >= deadline:
            first.retry_not_started = "The execution budget expired during replacement setup."
            raise first
        preparation_budget = asyncio.timeout_at(deadline)
        try:
            async with preparation_budget:
                worker_handle = await self._worker.prepare(requests)
        except TimeoutError as preparation:
            cancelled = preparation.__cause__
            if preparation_budget.expired() and isinstance(cancelled, asyncio.CancelledError) and cancelled.__cause__ is None:
                first.retry_not_started = "The execution budget expired during replacement preparation."
                raise first from preparation
            raise ExceptionGroup("Prediction failed and replacement preparation failed", (first, preparation)) from None
        self._require_prediction_active()
        if asyncio.get_running_loop().time() >= deadline:
            first.retry_not_started = "The execution budget expired during replacement preparation."
            raise first
        if self._on_prediction_failure is not None:
            self._on_prediction_failure(first, self._acknowledged)
        self._require_prediction_active()
        if asyncio.get_running_loop().time() >= deadline:
            first.retry_not_started = "The execution budget expired before retry dispatch."
            raise first
        async with asyncio.timeout_at(deadline):
            return await self._worker.execute(worker_handle)

    async def _predict_once(self, requests):
        self._require_prediction_active()
        worker_handle = await self._worker.prepare(requests)
        self._require_prediction_active()
        return await self._worker.execute(worker_handle)

    def _worker_crash(self, failure, *, cause):
        if self._phase is _Phase.PREPARING:
            phase = "preparing"
        elif self._phase is _Phase.EXECUTING:
            phase = "running"
        else:
            raise ValueError("Native exit arrived outside active scientific preparation or execution.") from failure
        runner = failure.runner
        return ObservedWorkerCrash.from_session(
            self._acknowledged,
            phase=phase,
            owner=self,
            runner=runner,
            evidence=_evidence({"native_operation": {
                "role": runner.role, "generation": runner.generation, "operation": runner.operation,
            }}),
            cause=cause,
        )

    async def _acquire_prediction(self, phase):
        self._prediction_pending = True
        try:
            worker, retirement, runtime = await self._allocate_prediction()
            # No await separates handoff from adoption. Even a late return after
            # retirement began remains owned here and must be retired later.
            self._worker, self._retirement = worker, retirement
            if self._phase is not phase:
                raise ValueError("Prediction acquisition finished after retirement began; scientific dispatch is blocked.")
            return copy.deepcopy(runtime)
        except NativeWorkerExited as cause:
            self._prediction_setup = cause.native_operation
            raise
        finally:
            self._prediction_pending = False

    async def retire(self):
        if self._phase is _Phase.RETIRED:
            return
        self._phase = _Phase.RETIRING
        self._prepared = None
        failures = []
        try:
            await self._input_preparation.retire()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("input_retirement", cause))
        if self._prediction_setup is not None:
            try:
                await self._prediction_setup.retire()
            except (Exception, asyncio.CancelledError) as cause:
                failures.append(("prediction_setup_retirement", cause))
            else:
                self._prediction_setup = None
        worker, retirement = self._worker, self._retirement
        if retirement is not None:
            failures.extend(await self._retire_prediction(worker, retirement, retry=True))
        if (self._prediction_pending or self._prediction_setup is not None
                or self._worker is not worker or self._retirement is not retirement):
            failures.append(
                (
                    "prediction_acquisition",
                    RuntimeError(
                        "Prediction acquisition changed or is still completing its handoff; settle the current worker before confirming retirement."
                    ),
                )
            )
        if failures:
            self._raise_retirement_failure(failures)
        self._phase = _Phase.RETIRED

    async def _retire_prediction(self, worker, retirement, *, retry=False):
        task = self._prediction_retirement_task
        failed = task is not None and task.done() and (
            task.cancelled() or task.exception() is not None or bool(task.result())
        )
        if task is None or (retry and failed):
            task = asyncio.create_task(contain_task_exit(retirement.retire_channel, worker))
            self._prediction_retirement_task = task
        return await asyncio.shield(task)

    def _raise_retirement_failure(self, failures):
        self.last_retirement_failure = SessionRetirementFailed(failures)
        # Both replacement and final retirement preserve cancellation, along
        # with the earliest cleanup cause and independent later observations.
        for _, cause in failures:
            if isinstance(cause, asyncio.CancelledError):
                self.last_retirement_failure.interruption_cause = cause.__cause__
                raise cause from self.last_retirement_failure
        raise self.last_retirement_failure from None
