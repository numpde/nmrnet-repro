"""Retain lifecycle facts and immutable terminal choices under their recovery owner.

Precedents: nmrpeak-repro 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal.py::StartPending and generation-sensitive recovery;
magnet-deploy c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/terminal_outbox.py::recorded_owner. The scope adds consumer-lane
identity for independent async owners. This codec neither persists nor claims work.
"""

from provider_core.container_guard import require_container

require_container()

import binascii
import json
import re
from base64 import b64decode, b64encode
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum

from provider_core.attempt_request import (
    _scalar_text,
    read_complete_body,
    read_fail_body,
    require_analysis_kind,
    require_provider_ref,
    require_start_body,
)
from provider_core.attempt_response import (
    ATTEMPT_RESPONSE_BYTES,
    AttemptAdmission,
    AttemptCompletion,
    AttemptFailure,
    AttemptObservation,
    read_attempt,
    read_complete,
    read_fail,
    read_start,
)
from provider_core.job_input import FeedInputFacts
from provider_core.response_json import ResponseRejected, read_document
from provider_core.signing import _https_origin

# Pending starts only: 4096 request bytes become at most 5464 base64 characters;
# two scope identities of 1024 scalars each need at most 24576 JSON escape bytes.
# The encoder checks total size, including the API origin and metadata. This bound
# is not the future terminal-record retention reservation.
MAX_PENDING_RECORD_BYTES = 65536
# Diagnostic envelope precedent: pinned NMRPeak attempt_diagnostic.py. Unlike its
# history trimming, required evidence here is rejected rather than truncated.
MAX_EVIDENCE_BYTES = 262144
_LEGACY_MAX_ACKNOWLEDGED_BYTES = MAX_PENDING_RECORD_BYTES + ATTEMPT_RESPONSE_BYTES + 256
# Retain the candidate identity with entry, including canonical base64 and the
# worst-case escaped evidence schema. Legacy acknowledgements omit this field.
MAX_ACKNOWLEDGED_BYTES = _LEGACY_MAX_ACKNOWLEDGED_BYTES + 4 * ((MAX_EVIDENCE_BYTES + 2) // 3) + 16384
# Exact complete requests may include legal whitespace up to the 2 MiB wire cap.
# Base64 only the request/evidence, not their enclosing JSON records. Metadata
# allows 1024 evidence-schema scalars at worst-case JSON escape width plus fields.
MAX_TERMINAL_BYTES = MAX_ACKNOWLEDGED_BYTES + 4 * ((2097152 + 2) // 3) + 4 * ((MAX_EVIDENCE_BYTES + 2) // 3) + 16384
MAX_RECORD_BYTES = MAX_TERMINAL_BYTES + ATTEMPT_RESPONSE_BYTES + 1024


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryScope:
    """An explicit recovery binding, not authentication or an ownership lock."""

    api_origin: str
    provider_ref: str
    consumer_id: str
    generation_id: str

    def __post_init__(self):
        origin, _authority = _https_origin(self.api_origin)
        if origin != self.api_origin:
            raise ValueError("Cannot bind recovery scope: API origin must be canonical HTTPS")
        require_provider_ref(self.provider_ref)
        for value in (self.consumer_id, self.generation_id):
            if (
                type(value) is not str
                or not 1 <= len(value) <= 1024
                or any(c == "\x00" or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            ):
                raise ValueError(
                    "Cannot bind recovery scope: consumer and generation identities require 1 through 1024 Unicode scalar values without NUL"
                )


@dataclass(frozen=True, slots=True)
class StartPending:
    """A retained start obligation; dispatch may or may not already have occurred."""

    scope: RecoveryScope = field(repr=False)
    analysis_kind_ref: str
    input: FeedInputFacts
    body: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.scope) is not RecoveryScope or type(self.input) is not FeedInputFacts:
            raise ValueError("Cannot retain pending start: recovery scope and admitted input identity are required")
        require_analysis_kind(self.analysis_kind_ref)
        require_start_body(self.body, job_ref=self.input.job_ref)


class LocalExecutionPhase(Enum):
    NOT_ENTERED = "not_entered"
    EXECUTION_ENTERED = "execution_entered"


def _admission_bytes(admission):
    if type(admission) is not AttemptAdmission:
        raise ValueError("Cannot retain acknowledgement: an admitted Attempt response is required")
    timestamp = admission.started_at
    if type(timestamp) is not datetime or timestamp.utcoffset() != timedelta(0):
        raise ValueError("Cannot retain acknowledgement: its start timestamp must be UTC")
    document = asdict(admission)
    document["started_at"] = timestamp.isoformat().replace("+00:00", "Z")
    document["schema_id"] = "nmr.provider.execution_attempt_start_response.v1"
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class AcknowledgedAttempt:
    """Historical server admission and local entry; neither recreates a live handle.

    Pinned NMRPeak attempt_journal.py::bind_started_attempt/mark_execution_entered
    establishes the entry boundary. Unlike its active-only record, this value also
    retains terminal start replays without granting permission to execute them.
    Live preparation stays in memory; entry retains its binding-owned identity.
    Legacy entered records lack this identity and never authorize another run.
    """

    start: StartPending
    admission: AttemptAdmission
    local_phase: LocalExecutionPhase
    prepared_identity: "ProtectedEvidence | None" = field(default=None, repr=False)

    def __post_init__(self):
        if type(self.start) is not StartPending or type(self.local_phase) is not LocalExecutionPhase:
            raise ValueError("Cannot retain acknowledgement: original start and explicit local execution phase are required")
        if self.prepared_identity is not None and (
            type(self.prepared_identity) is not ProtectedEvidence or self.local_phase is not LocalExecutionPhase.EXECUTION_ENTERED
        ):
            raise ValueError("Cannot retain prepared identity: protected evidence belongs to the execution-entry transition.")
        try:
            read_start(
                _admission_bytes(self.admission),
                job_ref=self.start.input.job_ref,
                analysis_kind_ref=self.start.analysis_kind_ref,
                provider_ref=self.start.scope.provider_ref,
            )
        except ResponseRejected as cause:
            raise ValueError(f"Cannot retain acknowledgement: {cause}") from None
        if self.local_phase is LocalExecutionPhase.EXECUTION_ENTERED and self.admission.state != "in_progress":
            raise ValueError("Cannot record execution entry from a terminal start acknowledgement")


class TerminalOperation(Enum):
    COMPLETE = "complete"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ProtectedEvidence:
    """Required binding-owned diagnostics, retained exactly and never rendered here.

    The binding owns schema meaning, secret removal and necessary causal/retirement
    observations. These bytes are evidence, never a resource-release grant.
    """

    schema_id: str = field(repr=False)
    content: bytes = field(repr=False)

    def __post_init__(self):
        _scalar_text(self.schema_id, "evidence schema identity")
        if type(self.content) is not bytes or not 1 <= len(self.content) <= MAX_EVIDENCE_BYTES:
            raise ValueError(
                "Cannot retain required evidence: immutable content must contain 1 through 262144 bytes; evidence is never truncated"
            )


@dataclass(frozen=True, slots=True)
class TerminalPending:
    """One immutable terminal choice, retained before any publication attempt.

    SECS execution.py at 34f5915c1534278bbf61b4c6592afa44cf94ab08 retains required
    failure reports before publication. Pinned NMRPeak attempt_journal.py retains
    exact terminal commands. No resource is released by constructing this value.
    """

    attempt: AcknowledgedAttempt
    operation: TerminalOperation
    body: bytes = field(repr=False)
    evidence: ProtectedEvidence = field(repr=False)

    def __post_init__(self):
        if type(self.attempt) is not AcknowledgedAttempt or self.attempt.admission.state != "in_progress":
            raise ValueError("Cannot select terminal outcome: an originally in-progress acknowledgement is required")
        if type(self.operation) is not TerminalOperation or type(self.evidence) is not ProtectedEvidence:
            raise ValueError("Cannot select terminal outcome: explicit operation and required protected evidence are needed")
        reference = self.attempt.admission.execution_attempt_ref
        if self.operation is TerminalOperation.COMPLETE:
            read_complete_body(self.body, execution_attempt_ref=reference)
        else:
            read_fail_body(self.body, execution_attempt_ref=reference)


def _receipt_bytes(receipt):
    if type(receipt) is AttemptCompletion:
        operation = "complete"
    elif type(receipt) is AttemptFailure:
        operation = "fail"
    else:
        raise ValueError("Cannot retain terminal confirmation: an admitted outcome receipt is required")
    timestamp = receipt.committed_at
    if type(timestamp) is not datetime or timestamp.utcoffset() != timedelta(0):
        raise ValueError("Cannot retain terminal confirmation: commit timestamp must be UTC")
    document = asdict(receipt)
    document["committed_at"] = timestamp.isoformat().replace("+00:00", "Z")
    document["schema_id"] = f"nmr.provider.execution_attempt_{operation}_response.v1"
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _matching_receipt(pending, raw):
    reference = pending.attempt.admission.execution_attempt_ref
    try:
        if pending.operation is TerminalOperation.COMPLETE:
            schema, result = read_complete_body(pending.body, execution_attempt_ref=reference)
            return read_complete(raw, execution_attempt_ref=reference, result_schema_id=schema, result=result)
        code, message = read_fail_body(pending.body, execution_attempt_ref=reference)
        return read_fail(raw, execution_attempt_ref=reference, failure_code=code, failure_message=message)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot retain terminal confirmation: {cause}") from None


@dataclass(frozen=True, slots=True)
class TerminalConfirmed:
    """Matching API receipt; no claim of worker retirement, deletion or safe reuse."""

    pending: TerminalPending
    receipt: AttemptCompletion | AttemptFailure

    def __post_init__(self):
        if type(self.pending) is not TerminalPending:
            raise ValueError("Cannot retain terminal confirmation without the original pending outcome")
        _matching_receipt(self.pending, _receipt_bytes(self.receipt))


@dataclass(frozen=True, slots=True)
class TerminalHeld:
    """Reviewed terminal conflict blocks replay without changing the original choice.

    Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
    operations/execution_attempt_outcome.py::_existing_complete_result/_existing_fail_result
    and operations/errors.py. Expiry and opposite/mismatched outcomes cannot justify
    inventing another terminal command. Core recovery returns this record unchanged;
    publication refuses to resend it or substitute a different outcome. The
    consumer must reconcile the conflict before arranging any further action.
    """

    pending: TerminalPending
    problem_code: str
    request_id: str | None

    def __post_init__(self):
        if type(self.pending) is not TerminalPending:
            raise ValueError("Cannot hold terminal publication without its original pending outcome")
        operation = self.pending.operation.value
        codes = {
            "execution_attempt_outcome_expired",
            f"execution_attempt_{'completion_after_failure' if operation == 'complete' else 'failure_after_success'}",
            f"execution_attempt_{'completion' if operation == 'complete' else 'failure'}_replay_mismatch",
        }
        if type(self.problem_code) is not str or self.problem_code not in codes:
            raise ValueError("Cannot hold terminal publication: problem code is not a reviewed conflict for this operation")
        if self.request_id is not None:
            _require_correlation(self.request_id)


def _require_correlation(value):
    if type(value) is not str or re.fullmatch(r"[!-~]{1,128}", value) is None:
        raise ValueError("Cannot retain API observation: request correlation requires 1 through 128 visible ASCII characters")


@dataclass(frozen=True, slots=True)
class TerminalReadRequired:
    """Consumer policy forbids resend after this terminal HTTP 404/409 response.

    SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    src/secs_inference/provider/execution.py::_publish_terminal retains its
    terminal_reconciling obligation before reading state. This status records
    the failed send, not a verified domain refusal or absence of API effects.
    The exact original command and evidence remain unresolved across restart.
    """

    pending: TerminalPending
    refused_status: int
    request_id: str | None

    def __post_init__(self):
        if (
            type(self.pending) is not TerminalPending
            or type(self.refused_status) is not int
            or self.refused_status not in (404, 409)
        ):
            raise ValueError("Cannot require terminal state read without the original command and its HTTP 404 or 409 response")
        if self.request_id is not None:
            _require_correlation(self.request_id)


def _observation_document(observation):
    if type(observation) is not AttemptObservation or any(type(value) is not str for value in asdict(observation).values()):
        raise ValueError("Cannot retain terminal readback: an identity-bound Attempt observation is required")
    return {"schema_id": "nmr.provider.execution_attempt_read_response.v1", **asdict(observation)}


def _read_terminal_observation(required, document):
    if type(required) is not TerminalReadRequired:
        raise ValueError("Cannot retain terminal readback without its original read-only obligation")
    try:
        return read_attempt(
            json.dumps(document, separators=(",", ":")).encode(),
            execution_attempt_ref=required.pending.attempt.admission.execution_attempt_ref,
            job_ref=required.pending.attempt.start.input.job_ref,
        )
    except ResponseRejected as cause:
        raise ValueError(f"Cannot retain terminal readback: {cause}") from None


@dataclass(frozen=True, slots=True)
class TerminalStateObserved:
    """Retained state readback; even succeeded does not match a terminal receipt."""

    read_required: TerminalReadRequired
    observation: AttemptObservation

    def __post_init__(self):
        _read_terminal_observation(self.read_required, _observation_document(self.observation))

    @property
    def pending(self):
        return self.read_required.pending


@dataclass(frozen=True, slots=True)
class TerminalReadNotVisible:
    """A verified resource_not_found read, not proof of deletion or nonexistence.

    The API also uses this response when the provider cannot access the Attempt.
    Recovery admits its Problem envelope/correlation before constructing this
    value. No terminal acceptance, resend or scientific replay follows from it.
    """

    read_required: TerminalReadRequired
    request_id: str

    def __post_init__(self):
        if type(self.read_required) is not TerminalReadRequired:
            raise ValueError("Cannot retain an invisible Attempt without its original read-only obligation")
        _require_correlation(self.request_id)

    @property
    def pending(self):
        return self.read_required.pending


class SuppressionReason(Enum):
    SERVER_STOP = "server_stop"
    OBSERVATION_FAILED = "observation_failed"
    CALLER_CANCELLED = "caller_cancelled"


@dataclass(frozen=True, slots=True)
class SuppressedOutcome:
    """Keep returned evidence after interrupted acceptance, without authorizing send.

    The nested candidate reuses exact body/evidence admission and storage capacity.
    It is never the current pending-publication record. Core owns acceptance races;
    note 017's P3 observation contract requires their results to survive restart.
    No transition out of this disposition permits publication or scientific replay.
    """

    candidate: TerminalPending
    reason: SuppressionReason

    def __post_init__(self):
        if type(self.candidate) is not TerminalPending or type(self.reason) is not SuppressionReason:
            raise ValueError(
                "Cannot retain a suppressed outcome without admitted candidate bytes and the reason acceptance was interrupted"
            )


def acknowledge_start(start, admission):
    return AcknowledgedAttempt(start, admission, LocalExecutionPhase.NOT_ENTERED)


def mark_execution_entered(record, prepared_identity):
    """Record intent before invoking a prepared handle; never infer actual execution."""
    if type(record) is not AcknowledgedAttempt or record.local_phase is not LocalExecutionPhase.NOT_ENTERED:
        raise ValueError("Cannot enter execution: an acknowledged Attempt that has not entered is required")
    if type(prepared_identity) is not ProtectedEvidence:
        raise ValueError("Cannot enter execution: protected identity of the prepared candidate is required.")
    return replace(record, local_phase=LocalExecutionPhase.EXECUTION_ENTERED, prepared_identity=prepared_identity)


def pending_start(record):
    """Return the single retained identity owner for supported lifecycle states."""
    if type(record) is StartPending:
        return record
    if type(record) is AcknowledgedAttempt:
        return record.start
    if type(record) is TerminalPending:
        return record.attempt.start
    if type(record) in (TerminalConfirmed, TerminalHeld, TerminalReadRequired, TerminalStateObserved, TerminalReadNotVisible):
        return record.pending.attempt.start
    if type(record) is SuppressedOutcome:
        return record.candidate.attempt.start
    raise ValueError("Cannot use Attempt record: its lifecycle record type is unsupported")


def acknowledged_attempt(record):
    """Return the retained admission from a record with a known remote identity."""
    if type(record) is AcknowledgedAttempt:
        return record
    if type(record) is TerminalPending:
        return record.attempt
    if type(record) in (TerminalConfirmed, TerminalHeld, TerminalReadRequired, TerminalStateObserved, TerminalReadNotVisible):
        return record.pending.attempt
    if type(record) is SuppressedOutcome:
        return record.candidate.attempt
    raise ValueError("Cannot read retained admission: this record has no acknowledged Attempt")


def require_transition(expected, replacement):
    """Permit implemented forward transitions without changing an obligation's facts."""
    if pending_start(expected) != pending_start(replacement):
        raise ValueError("Cannot replace Attempt record: original start facts differ")
    if type(expected) is AcknowledgedAttempt and type(replacement) is TerminalPending and replacement.attempt == expected:
        return
    if (
        type(expected) is AcknowledgedAttempt
        and type(replacement) is SuppressedOutcome
        and replacement.candidate.attempt == expected
    ):
        return
    if (
        type(expected) is TerminalPending
        and type(replacement) in (TerminalConfirmed, TerminalHeld, TerminalReadRequired)
        and replacement.pending == expected
    ):
        return
    if (
        type(expected) is TerminalReadRequired
        and type(replacement) in (TerminalStateObserved, TerminalReadNotVisible)
        and replacement.read_required == expected
    ):
        return
    if (
        type(expected) is StartPending
        and type(replacement) is AcknowledgedAttempt
        and replacement.local_phase is LocalExecutionPhase.NOT_ENTERED
    ):
        return
    if (
        type(expected) is AcknowledgedAttempt
        and type(replacement) is AcknowledgedAttempt
        and expected.admission == replacement.admission
        and expected.local_phase is LocalExecutionPhase.NOT_ENTERED
        and replacement.local_phase is LocalExecutionPhase.EXECUTION_ENTERED
        and type(replacement.prepared_identity) is ProtectedEvidence
    ):
        return
    raise ValueError("Cannot replace Attempt record: transition would skip, repeat or reverse a retained lifecycle boundary")


def _encode_pending(record):
    raw = json.dumps(
        {
            "schema_id": "provider_core.start_pending.v1",
            "scope": asdict(record.scope),
            "analysis_kind_ref": record.analysis_kind_ref,
            "input": asdict(record.input),
            "start_body": b64encode(record.body).decode("ascii"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_PENDING_RECORD_BYTES:
        raise ValueError(
            f"Cannot encode pending start: record requires {len(raw)} bytes, exceeding the {MAX_PENDING_RECORD_BYTES}-byte limit"
        )
    return raw


def _encode_acknowledged(record):
    start = pending_start(record)
    raw_start = _encode_pending(start)
    if type(record) is StartPending:
        return raw_start
    document = {
        "schema_id": "provider_core.acknowledged.v1",
        "start": json.loads(raw_start),
        "admission": json.loads(_admission_bytes(record.admission)),
        "local_phase": record.local_phase.value,
    }
    if record.prepared_identity is not None:
        document["schema_id"] = "provider_core.acknowledged.v2"
        document["prepared_identity"] = {
            "schema_id": record.prepared_identity.schema_id,
            "content": b64encode(record.prepared_identity.content).decode("ascii"),
        }
    raw = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    limit = _LEGACY_MAX_ACKNOWLEDGED_BYTES if record.prepared_identity is None else MAX_ACKNOWLEDGED_BYTES
    if len(raw) > limit:
        raise ValueError("Cannot encode Attempt record: acknowledged record exceeds its byte limit")
    return raw


def _terminal_document(record):
    return {
        "schema_id": "provider_core.terminal_pending.v1",
        "attempt": json.loads(_encode_acknowledged(record.attempt)),
        "operation": record.operation.value,
        "body": b64encode(record.body).decode("ascii"),
        "evidence": {"schema_id": record.evidence.schema_id, "content": b64encode(record.evidence.content).decode("ascii")},
    }


def _read_required_document(record):
    return {
        "schema_id": "provider_core.terminal_read_required.v1",
        "pending": _terminal_document(record.pending),
        "refused_status": record.refused_status,
        "request_id": record.request_id,
    }


def encode_record(record):
    """Encode exact replay bytes; the store must separately confirm durability."""
    if type(record) in (StartPending, AcknowledgedAttempt):
        return _encode_acknowledged(record)
    if type(record) is TerminalPending:
        document = _terminal_document(record)
    elif type(record) is TerminalConfirmed:
        document = {
            "schema_id": "provider_core.terminal_confirmed.v1",
            "pending": _terminal_document(record.pending),
            "receipt": json.loads(_receipt_bytes(record.receipt)),
        }
    elif type(record) is TerminalHeld:
        document = {
            "schema_id": "provider_core.terminal_held.v1",
            "pending": _terminal_document(record.pending),
            "problem_code": record.problem_code,
            "request_id": record.request_id,
        }
    elif type(record) is TerminalReadRequired:
        document = _read_required_document(record)
    elif type(record) is TerminalStateObserved:
        document = {
            "schema_id": "provider_core.terminal_state_observed.v1",
            "read_required": _read_required_document(record.read_required),
            "observation": _observation_document(record.observation),
        }
    elif type(record) is TerminalReadNotVisible:
        document = {
            "schema_id": "provider_core.terminal_read_not_visible.v1",
            "read_required": _read_required_document(record.read_required),
            "request_id": record.request_id,
        }
    elif type(record) is SuppressedOutcome:
        document = {
            "schema_id": "provider_core.suppressed_outcome.v1",
            "candidate": _terminal_document(record.candidate),
            "reason": record.reason.value,
        }
    else:
        raise ValueError("Cannot encode Attempt record: lifecycle record type is unsupported")
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    limit = MAX_TERMINAL_BYTES if type(record) is TerminalPending else MAX_RECORD_BYTES
    if len(raw) > limit:
        raise ValueError("Cannot encode Attempt record: terminal record exceeds its byte limit")
    return raw


def _fields(document, fields, label):
    if type(document) is not dict or set(document) != fields:
        raise ValueError(f"Cannot restore Attempt record: {label} fields do not match the retained schema")


def _decode_pending(raw, *, scope):
    """Reject foreign scope before restoring any obligation; never adopt implicitly."""
    if type(scope) is not RecoveryScope:
        raise ValueError("Cannot restore pending start: an explicit expected recovery scope is required")
    try:
        document = read_document(raw, MAX_PENDING_RECORD_BYTES)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot restore pending start: {cause}") from None
    _fields(document, {"schema_id", "scope", "analysis_kind_ref", "input", "start_body"}, "record")
    if document["schema_id"] != "provider_core.start_pending.v1":
        raise ValueError("Cannot restore pending start: retained schema is unsupported")
    _fields(document["scope"], {"api_origin", "provider_ref", "consumer_id", "generation_id"}, "scope")
    if RecoveryScope(**document["scope"]) != scope:
        raise ValueError(
            "Cannot restore pending start: API, provider, consumer or generation differs from the expected recovery scope"
        )
    _fields(document["input"], {"job_ref", "input_schema_id", "input_fingerprint", "input_byte_length"}, "input")
    identity = FeedInputFacts(**document["input"])
    encoded = document["start_body"]
    if type(encoded) is not str or len(encoded) > 5464:
        raise ValueError("Cannot restore pending start: retained request must be bounded base64 text")
    try:
        body = b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Cannot restore pending start: retained request is not valid standard base64") from None
    if b64encode(body).decode("ascii") != encoded:
        raise ValueError("Cannot restore pending start: retained request base64 is not canonical")
    return StartPending(scope, document["analysis_kind_ref"], identity, body)


def _decode_acknowledged(raw, *, scope):
    """Restore bounded historical facts; no loaded state authorizes scientific replay."""
    try:
        document = read_document(raw, MAX_ACKNOWLEDGED_BYTES)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot restore Attempt record: {cause}") from None
    if type(document) is not dict or document.get("schema_id") not in (
        "provider_core.acknowledged.v1",
        "provider_core.acknowledged.v2",
    ):
        return _decode_pending(raw, scope=scope)
    if document["schema_id"] == "provider_core.acknowledged.v1" and len(raw) > _LEGACY_MAX_ACKNOWLEDGED_BYTES:
        raise ValueError("Cannot restore acknowledgement: legacy record exceeds its byte limit.")
    fields = {"schema_id", "start", "admission", "local_phase"}
    identity = None
    if document["schema_id"] == "provider_core.acknowledged.v2":
        fields.add("prepared_identity")
        _fields(document.get("prepared_identity"), {"schema_id", "content"}, "prepared identity")
        evidence = document["prepared_identity"]
        identity = ProtectedEvidence(
            evidence["schema_id"], _retained_bytes(evidence["content"], MAX_EVIDENCE_BYTES, "prepared identity")
        )
    _fields(document, fields, "acknowledged record")
    start = _decode_pending(json.dumps(document["start"], separators=(",", ":")).encode(), scope=scope)
    try:
        admission = read_start(
            json.dumps(document["admission"], separators=(",", ":")).encode(),
            job_ref=start.input.job_ref,
            analysis_kind_ref=start.analysis_kind_ref,
            provider_ref=start.scope.provider_ref,
        )
    except ResponseRejected as cause:
        raise ValueError(f"Cannot restore acknowledgement: {cause}") from None
    try:
        phase = LocalExecutionPhase(document["local_phase"])
    except ValueError:
        raise ValueError("Cannot restore acknowledgement: local execution phase is unsupported") from None
    return AcknowledgedAttempt(start, admission, phase, identity)


def _retained_bytes(encoded, maximum, label):
    if type(encoded) is not str or len(encoded) > 4 * ((maximum + 2) // 3):
        raise ValueError(f"Cannot restore retained record: {label} exceeds its base64 bound")
    try:
        raw = b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError(f"Cannot restore retained record: {label} is not standard base64") from None
    if len(raw) > maximum or b64encode(raw).decode("ascii") != encoded:
        raise ValueError(f"Cannot restore retained record: {label} exceeds its byte bound or is not canonical base64")
    return raw


def _decode_terminal(document, *, scope):
    _fields(document, {"schema_id", "attempt", "operation", "body", "evidence"}, "terminal record")
    if document["schema_id"] != "provider_core.terminal_pending.v1":
        raise ValueError("Cannot restore terminal record: retained schema is unsupported")
    attempt = _decode_acknowledged(json.dumps(document["attempt"], separators=(",", ":")).encode(), scope=scope)
    try:
        operation = TerminalOperation(document["operation"])
    except ValueError:
        raise ValueError("Cannot restore terminal record: operation is unsupported") from None
    _fields(document["evidence"], {"schema_id", "content"}, "protected evidence")
    evidence = ProtectedEvidence(
        document["evidence"]["schema_id"], _retained_bytes(document["evidence"]["content"], MAX_EVIDENCE_BYTES, "evidence")
    )
    maximum = 2097152 if operation is TerminalOperation.COMPLETE else 16384
    return TerminalPending(attempt, operation, _retained_bytes(document["body"], maximum, "request"), evidence)


def _decode_read_required(document, *, scope):
    _fields(document, {"schema_id", "pending", "refused_status", "request_id"}, "terminal read obligation")
    if document["schema_id"] != "provider_core.terminal_read_required.v1":
        raise ValueError("Cannot restore terminal read obligation: retained schema is unsupported")
    return TerminalReadRequired(
        _decode_terminal(document["pending"], scope=scope), document["refused_status"], document["request_id"]
    )


def decode_record(raw, *, scope):
    """Restore one supported state without treating a receipt as resource retirement."""
    try:
        document = read_document(raw, MAX_RECORD_BYTES)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot restore Attempt record: {cause}") from None
    schema = document.get("schema_id") if type(document) is dict else None
    if schema == "provider_core.terminal_pending.v1":
        if len(raw) > MAX_TERMINAL_BYTES:
            raise ValueError("Cannot restore terminal record: pending record exceeds its byte limit")
        return _decode_terminal(document, scope=scope)
    if schema == "provider_core.terminal_confirmed.v1":
        _fields(document, {"schema_id", "pending", "receipt"}, "confirmed record")
        pending = _decode_terminal(document["pending"], scope=scope)
        receipt = _matching_receipt(pending, json.dumps(document["receipt"], separators=(",", ":")).encode())
        return TerminalConfirmed(pending, receipt)
    if schema == "provider_core.terminal_held.v1":
        _fields(document, {"schema_id", "pending", "problem_code", "request_id"}, "held record")
        return TerminalHeld(_decode_terminal(document["pending"], scope=scope), document["problem_code"], document["request_id"])
    if schema == "provider_core.terminal_read_required.v1":
        return _decode_read_required(document, scope=scope)
    if schema == "provider_core.terminal_state_observed.v1":
        _fields(document, {"schema_id", "read_required", "observation"}, "terminal state readback")
        required = _decode_read_required(document["read_required"], scope=scope)
        return TerminalStateObserved(required, _read_terminal_observation(required, document["observation"]))
    if schema == "provider_core.terminal_read_not_visible.v1":
        _fields(document, {"schema_id", "read_required", "request_id"}, "invisible terminal readback")
        return TerminalReadNotVisible(_decode_read_required(document["read_required"], scope=scope), document["request_id"])
    if schema == "provider_core.suppressed_outcome.v1":
        _fields(document, {"schema_id", "candidate", "reason"}, "suppressed outcome")
        try:
            reason = SuppressionReason(document["reason"])
        except ValueError:
            raise ValueError(
                "Cannot restore suppressed outcome: the reason acceptance was interrupted is not a recognised category"
            ) from None
        return SuppressedOutcome(_decode_terminal(document["candidate"], scope=scope), reason)
    return _decode_acknowledged(raw, scope=scope)
