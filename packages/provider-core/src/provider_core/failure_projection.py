"""Protected projections for exact core-owned failures, without graph traversal.

Ports the core branches of nmrnet-repro37cf6a15cdd58de7bd2ec0a02b33442a2c14f032,
nmrnet_provider/process_reports.py::_owned_relationships/_safe_owner_details
(SHA-256 ee13ebc042ba407d957608477f3343a084cc7ce0143f84ed6971371468e25f7b).
The consumer still owns standard cause/context/groups, cycle and display bounds,
credential redaction and its recipient policy. Returned facts are PROTECTED and
unredacted; borrowed exceptions may contain secrets or private data. This module
never renders raw response bodies, arbitrary exception text or scientific output.
"""

from provider_core.container_guard import require_container

require_container()

from provider_core.api import ApiFailure
from provider_core.attempt_record import (
    AcknowledgedAttempt,
    TerminalConfirmed,
    TerminalHeld,
    TerminalReadNotVisible,
    TerminalReadRequired,
    TerminalStateObserved,
)
from provider_core.attempt_store import StoreFailed, StoreRefused
from provider_core.chat import ChatFailed
from provider_core.controller import ControllerFailed, ControllerRefused, StartRecoveryExpired, TerminalRecoveryExpired
from provider_core.conversation import ConversationFailed, ConversationRetirementFailed, ToolRejected
from provider_core.credential import CredentialRejected
from provider_core.execution import ExecutionFailed, ExecutionRefused, ExecutionStopped, ObservationGapExpired
from provider_core.ownership import OwnershipRefused
from provider_core.publication import PublicationFailed, PublicationRefused
from provider_core.recovery import RecoveryFailed, RecoveryRefused
from provider_core.starting import StartFailed, StartRefused
from provider_core.transport import ExchangeFailed, response_request_id
from provider_core.uploads import UploadFailed, UploadFileReadFailed, UploadInputRejected


def owned_relationships(error):
    """Yield labelled original owner references, including absent/repeated causes.

    Unknown types and subclasses expose nothing. The caller decides how to walk,
    redact and bound this protected graph; ordinary Python chaining is not copied.
    """
    kind = type(error)
    if kind is ConversationRetirementFailed:
        for endpoint_index, cause in error.failures:
            yield f"interpreter endpoint {endpoint_index} cleanup", cause
    if kind in (ConversationFailed, ConversationRetirementFailed):
        for endpoint_index, cause in error.endpoint_failures:
            yield f"interpreter endpoint {endpoint_index} failure", cause
        for turn_index, cause in error.correction_failures:
            yield f"interpreter turn {turn_index} correction failure", cause
    if kind in (
        CredentialRejected,
        ApiFailure,
        StoreFailed,
        ControllerFailed,
        StartRecoveryExpired,
        TerminalRecoveryExpired,
        ExecutionFailed,
        ObservationGapExpired,
        RecoveryFailed,
        PublicationFailed,
        StartFailed,
        ChatFailed,
        UploadFailed,
        UploadInputRejected,
        UploadFileReadFailed,
        ConversationFailed,
        ConversationRetirementFailed,
    ):
        yield "owned cause", error.cause
    if kind in (
        StoreFailed,
        ControllerFailed,
        ExecutionFailed,
        RecoveryFailed,
        ExchangeFailed,
        UploadFailed,
        UploadInputRejected,
        UploadFileReadFailed,
    ):
        for cause in error.cleanup_failures:
            yield "independent cleanup", cause
    if kind in (ConversationRetirementFailed, ChatFailed, ExecutionFailed):
        yield "interruption cause", error.interruption_cause
    if kind is ExecutionFailed:
        yield "analysis operation interruption", error.operation_interruption
    if kind in (PublicationFailed, StartFailed):
        for cause in error.settlement_failures:
            yield "independent settlement", cause
    if kind in (ExecutionFailed, PublicationFailed, StartFailed, RecoveryFailed):
        yield "API failure", error.api_failure
    if kind is ExchangeFailed:
        yield "transport primary", error.primary
        yield "transport interruption", error.interruption
    if kind in (UploadFailed, UploadInputRejected):
        yield "acquisition interruption", error.interruption
    if kind is UploadFileReadFailed:
        yield "source reader interruption", error.interruption


def owned_details(error):
    """Return fresh scalar facts for protected, redacted consumer reporting.

    Only exact reviewed owner types may provide text/fields. Received metadata
    says nothing about durable acceptance; transport correlation keeps its own
    header admission. Redaction and display truncation remain with the consumer.
    """
    kind = type(error)
    if kind not in (
        CredentialRejected,
        ApiFailure,
        StoreFailed,
        StoreRefused,
        ControllerFailed,
        ControllerRefused,
        StartRecoveryExpired,
        TerminalRecoveryExpired,
        ExecutionFailed,
        ExecutionRefused,
        ExecutionStopped,
        ObservationGapExpired,
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
        ToolRejected,
        UploadFailed,
        UploadInputRejected,
        UploadFileReadFailed,
        ExchangeFailed,
    ):
        return {}
    details = {"message": str(error)}
    if kind is ApiFailure:
        details.update(
            operation=error.operation,
            status=error.status,
            request_id=error.request_id,
            problem_code=error.problem_code,
            issuance_may_have_committed=error.issuance_may_have_committed,
            attempt_mutation_may_have_committed=error.attempt_mutation_may_have_committed,
        )
    if kind is StoreFailed:
        details["record_visible"] = error.record_visible
    if kind in (StartRecoveryExpired, TerminalRecoveryExpired):
        details["window_seconds"] = error.window_seconds
    if kind is StartRecoveryExpired:
        details["job_ref"] = error.pending.input.job_ref
    if kind in (ExecutionFailed, TerminalRecoveryExpired) and type(error.attempt) is AcknowledgedAttempt:
        details.update(
            job_ref=error.attempt.admission.job_ref, execution_attempt_ref=error.attempt.admission.execution_attempt_ref
        )
    if kind is ObservationGapExpired:
        details.update(
            gap_seconds=error.gap_seconds, read_started=error.read_started, observation_pending=error.observation_pending
        )
    if kind is ExecutionFailed:
        details.update(
            outcome_in_memory=error.outcome is not None, unconfirmed_record_in_memory=error.unconfirmed_record is not None
        )
    if kind in (UploadFailed, UploadInputRejected):
        details["grant_issued"] = error.grant_issued
    if kind is ChatFailed:
        details["reason"] = error.reason
        head = error.response
        if head is None and type(error.cause) is ExchangeFailed:
            head = error.cause.response_head
        if head is not None:
            details.update(status=head.status, request_id=response_request_id(head.headers))
    if kind is ExchangeFailed and error.response_head is not None:
        details.update(status=error.response_head.status, request_id=response_request_id(error.response_head.headers))
    if kind is StartFailed:
        details["acknowledgement_received"] = error.acknowledgement is not None
        if error.acknowledgement is not None:
            details["observed_attempt_state"] = error.acknowledgement.admission.state
    if kind is PublicationFailed:
        # A held conflict is a refusal, not a terminal commit receipt. None of
        # these received/in-memory facts establishes the following write's durability.
        details["publication_observation_received"] = error.outcome is not None
        details["terminal_receipt_received"] = type(error.outcome) is TerminalConfirmed
        if type(error.outcome) in (TerminalConfirmed, TerminalHeld, TerminalReadRequired):
            details["publication_outcome_kind"] = type(error.outcome).__name__
            details["terminal_operation"] = error.outcome.pending.operation.value
    if kind is RecoveryFailed:
        details["terminal_readback_in_memory"] = error.observed_record is not None
        if type(error.observed_record) is TerminalStateObserved:
            details.update(
                observed_attempt_state=error.observed_record.observation.state,
                observed_job_state=error.observed_record.observation.job_state,
            )
        elif type(error.observed_record) is TerminalReadNotVisible:
            details.update(terminal_read_not_visible=True, request_id=error.observed_record.request_id)
    return details
