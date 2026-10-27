"""Bounded private operator evidence without arbitrary payloads or exception text.

Graph traversal ports SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/diagnostics.py::exception_evidence. NMRNet adds its
actual owner fields, separate cause/context edges and admitted worker diagnostics.
This is an operator projection, never an API failure or a molecular result.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import re

from nmrnet_inference.predict import MAX_ATOMS, MAX_GEOMETRIES
from nmrnet_provider.config import ConfigurationRejected
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import InputSelectionTimeLimit, InputWorkerCallLimit, PreparationRetirementFailed
from nmrnet_provider.interpreter import ModelReportedInputProblem
from nmrnet_provider.native_factory import NativeSetupFailed
from nmrnet_provider.result import ResultRejected
from nmrnet_provider.service import ServiceFailed, ServiceRefused
from nmrnet_provider.session import (
    PredictionRecoveryFailed,
    PreparationEvidenceFailed,
    ResultAssemblyFailed,
    SessionRetirementFailed,
)
from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import ConformerOutputLimit
from nmrnet_provider.worker_client import (
    PredictionExecutionFailed,
    WorkerExchangeTimedOut,
    WorkerReportedFailure,
    WorkerResultRejected,
)
from nmrnet_provider.worker_protocol import WorkerProtocolError
from provider_core.attempt_record import ProtectedEvidence
from provider_core.diagnostics import DiagnosticOutputRefused, failure_evidence as core_failure_evidence, operator_redactor
from provider_core.failure_projection import owned_details as core_owned_details
from provider_core.failure_projection import owned_relationships as core_owned_relationships

_MAX_FAILURE_NODES = 64


class ProviderProcessFailed(RuntimeError):
    """Process-owned failures and current need for enclosing container retirement."""

    def __init__(self, failures, *, requires_host_retirement):
        # silent-defaults: allow-contract -- The operator's next action depends on whether original owners remain held or the process has released its dependencies.
        super().__init__(
            "Provider cleanup remains held; preserve retained state and diagnostics. "
            "Stop the named deployment's provider and native-runner containers with "
            "make provider/deployment/down DEPLOYMENT=<name> before replacement."
            if requires_host_retirement
            else "Provider stopped after failure; inspect its protected causes and retained work before restart."
        )
        self.failures = tuple(failures)
        self.requires_host_retirement = requires_host_retirement


def _owned_relationships(error):
    """Combine protected core edges with NMRNet's scientific/resource owners."""
    yield from core_owned_relationships(error)
    kind = type(error)
    if kind in (ProviderProcessFailed, ServiceFailed, SessionRetirementFailed, PreparationRetirementFailed):
        for operation, cause in error.failures:
            yield operation, cause
    if kind is ModelReportedInputProblem:
        for endpoint_index, cause in error.endpoint_failures:
            yield f"interpreter endpoint {endpoint_index} failure", cause
        for turn_index, cause in error.correction_failures:
            yield f"interpreter turn {turn_index} correction failure", cause
    if kind in (ConfigurationRejected, PreparationRetirementFailed, NativeSetupFailed, InputSelectionTimeLimit):
        yield "owned cause", error.cause
    if kind is ConfigurationRejected:
        for cause in error.cleanup_failures:
            yield "independent cleanup", cause
    if kind is NativeSetupFailed:
        yield from error.cleanup_failures
    if kind in (NativeSetupFailed, PreparationRetirementFailed, SessionRetirementFailed):
        yield "interruption cause", error.interruption_cause
    if kind is PreparationEvidenceFailed:
        yield "preparation failure", error.preparation_failure
    if kind is PredictionRecoveryFailed:
        yield "first prediction failure", error.first_failure
        yield "recovery failure", error.cause
        yield "interruption cause", error.interruption_cause


def _safe_owner_details(error):
    # Core supplies protected owner facts; this reporter still redacts every
    # string before bounds and adds only NMRNet's reviewed scientific summaries.
    core_details = core_owned_details(error)
    if core_details:
        return core_details
    if type(error) not in (
        ProviderProcessFailed,
        DiagnosticOutputRefused,
        ConfigurationRejected,
        ServiceFailed,
        ServiceRefused,
        NativeSetupFailed,
        PreparationRetirementFailed,
        SessionRetirementFailed,
        PredictionRecoveryFailed,
        PreparationEvidenceFailed,
        ResultAssemblyFailed,
        ResultRejected,
        ConformerShortfall,
        ConformerOutputLimit,
        ModelReportedInputProblem,
        WorkerReportedFailure,
        PredictionExecutionFailed,
        WorkerExchangeTimedOut,
        WorkerResultRejected,
        WorkerProtocolError,
        InputRejected,
        InputWorkerCallLimit,
        InputSelectionTimeLimit,
    ):
        return {}
    details = {"message": str(error)}
    if type(error) is ProviderProcessFailed:
        details["requires_host_retirement"] = error.requires_host_retirement
    if type(error) is NativeSetupFailed and type(error.job_ref) is str:
        details["job_ref"] = error.job_ref
    if type(error) is InputWorkerCallLimit:
        details.update(maximum_calls=error.maximum_calls, calls_started=error.calls_started)
    if type(error) is InputSelectionTimeLimit:
        details["seconds"] = error.seconds
    if type(error) is ModelReportedInputProblem:
        details.update(issue=error.issue, independently_verified=False)
    if type(error) is ConformerOutputLimit:
        details.update(requested_count=error.requested_count, actual_count=error.actual_count)
    return details


def _received_result_summary(error, text):
    summary = {"complete_payload_in_report": False, "prediction_values_in_report": False}
    result = error.worker_result
    summary["batch_is_tuple"] = type(result) is tuple
    if type(result) in (tuple, list):
        summary.update(received_geometry_count=len(result), omitted_geometry_count=max(0, len(result) - MAX_GEOMETRIES))
        geometries = []
        for index, outcome in enumerate(result[:MAX_GEOMETRIES]):
            item = {"geometry_index": index, "outcome_is_object": type(outcome) is dict}
            if type(outcome) is dict:
                digest = outcome.get("request_sha256")
                if type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest):
                    item["request_sha256"] = text(digest, 64)
                prediction = outcome.get("prediction")
                item["prediction_is_object"] = type(prediction) is dict
                if type(prediction) is dict:
                    item["unit_is_ppm"] = type(prediction.get("unit")) is str and prediction["unit"] == "ppm"
                    rows = prediction.get("predictions")
                    item["prediction_rows_are_list"] = type(rows) is list
                    if type(rows) is list:
                        item.update(prediction_row_count=len(rows), omitted_prediction_row_count=max(0, len(rows) - MAX_ATOMS))
                        item["inspected_prediction_row_count"] = min(len(rows), MAX_ATOMS)
                        item["malformed_inspected_prediction_row_shape_count"] = sum(
                            type(row) is not dict or set(row) != {"atom_index", "element", "shift_ppm"}
                            for row in rows[:MAX_ATOMS]
                        )
            geometries.append(item)
        summary["geometries"] = geometries
    identity = error.prepared_identity
    if type(identity) is ProtectedEvidence:
        summary["prepared_identity"] = {
            "schema_id": text(identity.schema_id, 1024),
            "byte_length": len(identity.content),
            "sha256": text(hashlib.sha256(identity.content).hexdigest(), 64),
        }
    return summary


def _preparation_summary(document, text, diagnostic_window):
    summary = {"complete_payload_in_report": False, "document_is_object": type(document) is dict}
    if type(document) is not dict:
        return summary
    fingerprint = document.get("input_fingerprint")
    if type(fingerprint) is str and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", fingerprint):
        summary["input_fingerprint"] = text(fingerprint, 71)
    reservation = document.get("scientific_evidence_reservation")
    source = reservation if type(reservation) is dict else document
    if type(source.get("geometries")) is list:
        summary["geometry_count"] = len(source["geometries"])
    for field, container, key in (
        ("diagnostic", document, "diagnostic"),
        ("generation_diagnostic", document.get("generation_failure"), "diagnostic"),
        ("model_report_explanation", document.get("model_report"), "explanation"),
    ):
        if type(container) is dict and type(container.get(key)) is str:
            summary[field] = diagnostic_window(container[key])
            if field == "model_report_explanation":
                summary["independently_verified"] = False
    for field in ("preparation_diagnostics", "interpreter_diagnostics"):
        entries = source.get(field)
        if type(entries) is not list:
            continue
        maximum = MAX_GEOMETRIES if field == "preparation_diagnostics" else _MAX_FAILURE_NODES
        summary[field + "_count"] = len(entries)
        summary[field + "_omitted_count"] = max(0, len(entries) - maximum)
        projected = []
        for entry in entries[:maximum]:
            item = {"entry_is_object": type(entry) is dict}
            if type(entry) is dict:
                if field == "preparation_diagnostics":
                    diagnostic = entry.get("diagnostic")
                else:
                    # The interpreter already owns this redacted exception
                    # graph. Window it; never serialize raw model exchanges.
                    try:
                        diagnostic = (
                            json.dumps(entry["failure"], ensure_ascii=True, allow_nan=False)
                            if type(entry.get("failure")) is dict
                            else None
                        )
                    except (TypeError, ValueError, RecursionError):
                        diagnostic = None
                        item["diagnostic_graph_is_json"] = False
                    for name in ("endpoint_index", "turn_index"):
                        if type(entry.get(name)) is int and 0 <= entry[name] <= 2**53 - 1:
                            item[name] = entry[name]
                if type(diagnostic) is str:
                    item["diagnostic"] = diagnostic_window(diagnostic)
            projected.append(item)
        summary[field] = projected
    return summary


def _describe_scientific_owner(current, text, diagnostic_window):
    evidence = _safe_owner_details(current)
    if type(current) in (WorkerReportedFailure, PredictionExecutionFailed):
        evidence["native_worker_reported_diagnostic"] = diagnostic_window(current.diagnostic)
    if type(current) is InputRejected and current.diagnostic is not None:
        evidence["input_rejection_diagnostic"] = diagnostic_window(current.diagnostic)
    if type(current) is ConformerShortfall:
        evidence["conformer_generation_diagnostic"] = diagnostic_window(current.partial.diagnostic)
    if type(current) is ConformerOutputLimit:
        # This field contains the serialized native reply. Project only
        # diagnostic text, keeping coordinates and provenance with their
        # existing scientific evidence owner.
        evidence["conformer_generation_diagnostic"] = diagnostic_window(json.loads(current.diagnostic)["diagnostic"])
    if type(current) is ModelReportedInputProblem:
        evidence["model_reported_explanation"] = diagnostic_window(current.explanation)
    if type(current) is ResultAssemblyFailed:
        evidence["received_scientific_evidence"] = _received_result_summary(current, text)
    if type(current) is PreparationEvidenceFailed:
        evidence["unretained_preparation_evidence"] = _preparation_summary(current.document, text, diagnostic_window)
    return evidence


def failure_evidence(error, *, secrets):
    """Render the shared protected graph with NMRNet's scientific projections."""
    return core_failure_evidence(
        error, secrets=secrets, describe_owner=_describe_scientific_owner, owner_relationships=_owned_relationships
    )
