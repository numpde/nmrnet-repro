"""Bounded private operator evidence without arbitrary payloads or exception text.

Graph traversal ports SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/diagnostics.py::exception_evidence. NMRNet adds its
actual owner fields, separate cause/context edges and admitted worker diagnostics.
This is an operator projection, never an API failure or a molecular result.
"""

from tools.container_guard import require_container

require_container()

import errno
import json
import os
import socket
import ssl
import traceback
from builtins import BaseExceptionGroup
from collections import deque
from pathlib import Path

from nmrnet_provider.admission import AdmissionHeld
from nmrnet_provider.allocation import AllocationUnconfirmed
from nmrnet_provider.config import ConfigurationRejected
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.interpreter import ModelReportedInputProblem, redact_credential_fields
from nmrnet_provider.native_factory import NativeSetupFailed
from nmrnet_provider.result import ResultRejected
from nmrnet_provider.retirement import RetirementUnconfirmed
from nmrnet_provider.service import ServiceFailed, ServiceRefused
from nmrnet_provider.session import PreparationEvidenceFailed, ResultAssemblyFailed, SessionRetirementFailed
from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import ConformerOutputLimit
from nmrnet_provider.worker_client import WorkerReportedFailure, WorkerResultRejected
from nmrnet_provider.worker_protocol import WorkerProtocolError
from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalConfirmed, TerminalHeld
from provider_core.attempt_store import StoreFailed, StoreRefused
from provider_core.chat import ChatFailed
from provider_core.controller import ControllerFailed, ControllerRefused
from provider_core.conversation import ConversationFailed, ConversationRetirementFailed
from provider_core.credential import CredentialRejected
from provider_core.execution import ExecutionFailed, ExecutionRefused, ExecutionStopped
from provider_core.ownership import OwnershipRefused
from provider_core.publication import PublicationFailed, PublicationRefused
from provider_core.recovery import RecoveryFailed, RecoveryRefused
from provider_core.starting import StartFailed, StartRefused
from provider_core.transport import ExchangeFailed, response_request_id
from provider_core.uploads import UploadFailed, UploadInputRejected


class ProviderProcessFailed(RuntimeError):
    """Process-owned failures and current need for enclosing host retirement."""

    def __init__(self, failures, *, requires_host_retirement):
        # silent-defaults: allow-contract -- The operator's next action depends on whether original owners remain held or the process has released its dependencies.
        super().__init__(
            "Provider cleanup remains held; preserve its process, workers and retained state for exact host retirement."
            if requires_host_retirement
            else "Provider stopped after failure; inspect its protected causes and retained work before restart."
        )
        self.failures = tuple(failures)
        self.requires_host_retirement = requires_host_retirement


def _owned_relationships(error):
    """Only reviewed classes may expose their protected nonstandard cause fields."""
    kind = type(error)
    if kind in (ProviderProcessFailed, ServiceFailed, SessionRetirementFailed, PreparationRetirementFailed):
        for operation, cause in error.failures:
            yield operation, cause
    if kind is ConversationRetirementFailed:
        for endpoint_index, cause in error.failures:
            yield f"interpreter endpoint {endpoint_index} cleanup", cause
    if kind in (ConversationFailed, ConversationRetirementFailed):
        for endpoint_index, cause in error.endpoint_failures:
            yield f"interpreter endpoint {endpoint_index} failure", cause
    if kind in (
        ConfigurationRejected,
        CredentialRejected,
        ApiFailure,
        StoreFailed,
        ControllerFailed,
        ExecutionFailed,
        RecoveryFailed,
        PublicationFailed,
        StartFailed,
        ChatFailed,
        UploadFailed,
        UploadInputRejected,
        ConversationFailed,
        PreparationRetirementFailed,
        ConversationRetirementFailed,
        NativeSetupFailed,
    ):
        yield "owned cause", error.cause
    if kind in (ConfigurationRejected, StoreFailed, ControllerFailed, ExecutionFailed, RecoveryFailed, ExchangeFailed):
        for cause in error.cleanup_failures:
            yield "independent cleanup", cause
    if kind is NativeSetupFailed:
        yield from error.cleanup_failures
    if kind in (NativeSetupFailed, PreparationRetirementFailed, ConversationRetirementFailed):
        yield "interruption cause", error.interruption_cause
    if kind in (PublicationFailed, StartFailed):
        for cause in error.settlement_failures:
            yield "independent settlement", cause
    if kind in (ExecutionFailed, PublicationFailed, StartFailed, RecoveryFailed):
        yield "API failure", error.api_failure
    if kind is ExchangeFailed:
        yield "transport primary", error.primary
        yield "transport interruption", error.interruption
    if kind is PreparationEvidenceFailed:
        yield "preparation failure", error.preparation_failure


def _safe_owner_details(error):
    # This list is a disclosure decision, not permission to stringify arbitrary
    # library errors, parser arguments, remote response bodies or exception notes.
    if type(error) not in (
        ProviderProcessFailed,
        AdmissionHeld,
        AllocationUnconfirmed,
        RetirementUnconfirmed,
        ConfigurationRejected,
        CredentialRejected,
        ServiceFailed,
        ServiceRefused,
        NativeSetupFailed,
        PreparationRetirementFailed,
        SessionRetirementFailed,
        PreparationEvidenceFailed,
        ResultAssemblyFailed,
        ResultRejected,
        ConformerShortfall,
        ConformerOutputLimit,
        ModelReportedInputProblem,
        WorkerReportedFailure,
        WorkerResultRejected,
        WorkerProtocolError,
        InputRejected,
        ApiFailure,
        StoreFailed,
        StoreRefused,
        ControllerFailed,
        ControllerRefused,
        ExecutionFailed,
        ExecutionRefused,
        ExecutionStopped,
        OwnershipRefused,
        PublicationFailed,
        PublicationRefused,
        StartFailed,
        StartRefused,
        RecoveryFailed,
        RecoveryRefused,
        ChatFailed,
        ConversationFailed,
        ConversationRetirementFailed,
        UploadFailed,
        UploadInputRejected,
        ExchangeFailed,
    ):
        return {}
    details = {"message": str(error)}
    if type(error) is ProviderProcessFailed:
        details["requires_host_retirement"] = error.requires_host_retirement
    if type(error) is ApiFailure:
        details.update(
            operation=error.operation,
            status=error.status,
            request_id=error.request_id,
            problem_code=error.problem_code,
            issuance_may_have_committed=error.issuance_may_have_committed,
            attempt_mutation_may_have_committed=error.attempt_mutation_may_have_committed,
        )
    if type(error) is StoreFailed:
        details["record_visible"] = error.record_visible
    if type(error) is ExecutionFailed:
        details.update(
            outcome_retained=error.outcome is not None, unconfirmed_record_retained=error.unconfirmed_record is not None
        )
    if type(error) in (UploadFailed, UploadInputRejected):
        details["grant_issued"] = error.grant_issued
    if type(error) is ChatFailed:
        details["reason"] = error.reason
        head = error.response
        if head is None and type(error.cause) is ExchangeFailed:
            head = error.cause.response_head
        if head is not None:
            details.update(status=head.status, request_id=response_request_id(head.headers))
    if type(error) is ExchangeFailed and error.response_head is not None:
        details.update(status=error.response_head.status, request_id=response_request_id(error.response_head.headers))
    if type(error) is ModelReportedInputProblem:
        details.update(issue=error.issue, independently_verified=False)
    if type(error) is ConformerOutputLimit:
        details.update(requested_count=error.requested_count, actual_count=error.actual_count)
    if type(error) is StartFailed:
        details["acknowledgement_received"] = error.acknowledgement is not None
        if error.acknowledgement is not None:
            details["observed_attempt_state"] = error.acknowledgement.admission.state
    if type(error) is PublicationFailed:
        # A held conflict is a verified refusal, not a terminal commit receipt.
        # Neither presence flag establishes that the following storage write
        # completed, nor grants permission to replay or repeat science.
        details["publication_observation_received"] = error.outcome is not None
        details["terminal_receipt_received"] = type(error.outcome) is TerminalConfirmed
        if type(error.outcome) in (TerminalConfirmed, TerminalHeld):
            details["publication_outcome_kind"] = type(error.outcome).__name__
            details["terminal_operation"] = error.outcome.pending.operation.value
    return details


def failure_evidence(error, *, secrets):
    """Retain a bounded causal graph and safe current facts for the private operator."""
    spellings = {
        spelling for secret in secrets for spelling in (secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]) if spelling
    }
    spellings = sorted(spellings, key=len, reverse=True)
    seen, remaining = {}, 64

    def redact(text):
        for secret in spellings:
            text = text.replace(secret, "[redacted]")
        return redact_credential_fields(text)

    def text(value, maximum):
        redacted = redact(value)
        return redacted if len(redacted) <= maximum else redacted[:maximum] + "[truncated]"

    def diagnostic_window(value):
        diagnostic = redact(value)
        return {
            "head": diagnostic[:4096],
            "tail": diagnostic[-4096:] if len(diagnostic) > 8192 else diagnostic[4096:],
            "omitted_characters": max(0, len(diagnostic) - 8192),
        }

    def relationships(current):
        yield "cause", current.__cause__
        yield "context", current.__context__
        yield from _owned_relationships(current)
        if isinstance(current, BaseExceptionGroup):
            for member in current.exceptions:
                yield "group member", member

    def describe(current):
        nonlocal remaining
        remaining -= 1
        if id(current) in seen:
            return {"exception_reference": seen[id(current)]}
        identity = seen[id(current)] = len(seen)
        frames, count = deque(maxlen=32), 0
        for frame, line in traceback.walk_tb(current.__traceback__):
            frames.append(
                {
                    "file": text(Path(frame.f_code.co_filename).name, 128),
                    "line": line,
                    "function": text(frame.f_code.co_name, 128),
                }
            )
            count += 1
        evidence = {"exception_id": identity, "exception_type": text(type(current).__name__, 128), "frames": list(frames)}
        if count > len(frames):
            evidence["omitted_frame_count"] = count - len(frames)
        evidence.update(
            {name: text(value, 1024) if type(value) is str else value for name, value in _safe_owner_details(current).items()}
        )
        if isinstance(current, OSError) and type(current.errno) is int:
            evidence["errno"] = current.errno
            if current.errno in errno.errorcode and not isinstance(current, (ssl.SSLError, socket.gaierror, socket.herror)):
                evidence["reason"] = os.strerror(current.errno)
        if isinstance(current, ssl.SSLCertVerificationError):
            verification_code = getattr(current, "verify_code", None)
            if type(verification_code) is int:
                evidence["certificate_verification_code"] = verification_code
        if type(current) is WorkerReportedFailure:
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
        children = []
        for relationship, child in relationships(current):
            if child is None:
                continue
            if remaining <= 0:
                evidence["additional_relationships_omitted"] = True
                break
            children.append({"relationship": text(relationship, 128), "failure": describe(child)})
        if children:
            evidence["relationships"] = children
        return evidence

    return describe(error)
