"""Bind one admitted Job's prepared geometries to science and confirmed retirement.

The protocol owner is provider_core.analysis.AnalysisSession. Core owns current
server observations, execution entry, durable terminal choice and publication.
This binding owns the scientific handle, result admission and worker cleanup.
An injected preparation owner handles interpreted sources and their cleanup.
Without one, direct JSON admission preserves the original input bytes.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass
from enum import Enum, auto

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.interpreter import ModelReportedInputProblem
from nmrnet_provider.prepared_input import GeneratedJobInput, PreparedJobInput
from nmrnet_provider.result import RESULT_SCHEMA_ID, ResultPlan
from nmrnet_provider.retirement import retire_worker_channel
from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import ConformerOutputLimit, conformer_evidence
from nmrnet_provider.submission import prepare_direct_job
from provider_core.analysis import AnalysisFailure, AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import AcknowledgedAttempt, LocalExecutionPhase, ProtectedEvidence
from provider_core.job_input import VerifiedJobInput


class ResultAssemblyFailed(RuntimeError):
    """Keep received scientific evidence when result assembly cannot complete."""

    def __init__(self, worker_result, prepared_identity):
        super().__init__("Cannot assemble the scientific result; preserve the received worker evidence and underlying cause.")
        self.worker_result = worker_result
        self.prepared_identity = prepared_identity


class PreparationEvidenceFailed(RuntimeError):
    """Keep private evidence whole when core cannot admit it before prediction."""

    def __init__(self, document, *, preparation_failure=None):
        super().__init__(
            "Cannot retain preparation evidence before prediction; preserve its complete private diagnostic and underlying cause."
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
        # Keep observations in core's existing durable entry, under its single
        # evidence limit. A local-only carrier would lose them after a crash.
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

    Composition supplies allocation-bound worker/retirement ports and admitted
    runtime facts. Metadata is copied now so later caller mutation cannot alter
    result planning. This object has no API credentials or publication authority.
    """

    def __init__(
        self, *, worker, retirement, execution_identity, selected_artifacts, runtime_expectations, input_preparation=None
    ):
        self._worker = worker
        self._retirement = retirement
        self._input_preparation = input_preparation
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
            if self._input_preparation is None:
                prepared = PreparedJobInput((prepare_direct_job(instructions),), "direct_coordinate_json")
            else:
                prepared = await self._input_preparation.prepare(instructions)
        except (InputRejected, ConformerShortfall, ConformerOutputLimit, ModelReportedInputProblem) as cause:
            return self._failed_preparation(cause, instructions)
        if self._phase is not _Phase.PREPARING:
            raise ValueError("Input preparation finished after retirement began; scientific dispatch is blocked.")
        if type(prepared) not in (PreparedJobInput, GeneratedJobInput):
            raise TypeError("Scientific preparation requires the input owner's frozen supplied or generated geometry collection.")
        plan = ResultPlan(
            prepared,
            job_ref=instructions.job_ref,
            execution_attempt_ref=acknowledged.admission.execution_attempt_ref,
            input_fingerprint=instructions.identity.input_fingerprint,
            **self._facts,
        )
        # silent-defaults: allow-contract -- Direct coordinate input has no interpreter; endpoint diagnostics belong only to the interpreted input owner.
        diagnostics = None if self._input_preparation is None else self._input_preparation.interpreter_diagnostics
        handle = PreparedAnalysis(_prepared_identity(plan, prepared, diagnostics))
        reservation = _scientific_evidence_document(plan.scientific_evidence_reservation(), json.loads(handle.identity.content))
        try:
            _evidence(reservation)
        except Exception as cause:
            raise PreparationEvidenceFailed({"scientific_evidence_reservation": reservation}) from cause
        # Core persists this identity before calling execute(). Keep the admitted
        # bytes here until then, so even worker admission follows the durable write.
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
            if self._input_preparation is not None and self._input_preparation.interpreter_diagnostics is not None:
                document["interpreter_diagnostics"] = json.loads(self._input_preparation.interpreter_diagnostics)
            if isinstance(cause, ModelReportedInputProblem):
                code = "interpreter_reported_input_problem"
                document["model_report"] = {
                    "issue": cause.issue,
                    "explanation": cause.explanation,
                    "independently_verified": False,
                    "interpretation": json.loads(cause.observation),
                }
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

    async def execute(self, handle):
        if self._phase is not _Phase.PREPARED or handle is not self._prepared.handle:
            raise ValueError("Scientific execution requires this session's current prepared handle.")
        prepared = self._prepared
        self._prepared = None
        self._phase = _Phase.EXECUTING
        worker_handle = await self._worker.prepare(prepared.requests)
        if self._phase is not _Phase.EXECUTING:
            raise ValueError("Scientific worker preparation finished after retirement began; model execution is blocked.")
        result = await self._worker.execute(worker_handle)
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

    async def retire(self):
        if self._phase is _Phase.RETIRED:
            return
        self._phase = _Phase.RETIRING
        self._prepared = None
        failures = []
        if self._input_preparation is not None:
            try:
                await self._input_preparation.retire()
            except (Exception, asyncio.CancelledError) as cause:
                failures.append(("input_retirement", cause))
        failures.extend(await retire_worker_channel(self._worker, self._retirement))
        if failures:
            self.last_retirement_failure = SessionRetirementFailed(failures)
            # Cancellation remains cancellation; the retained failure still names
            # the earliest cause and every later independent cleanup observation.
            for _, cause in failures:
                if isinstance(cause, asyncio.CancelledError):
                    self.last_retirement_failure.interruption_cause = cause.__cause__
                    raise cause from self.last_retirement_failure
            raise self.last_retirement_failure from None
        self._phase = _Phase.RETIRED
