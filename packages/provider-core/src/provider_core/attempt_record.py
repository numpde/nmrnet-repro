"""Retain start, admission and execution-entry facts under their recovery owner.

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
from base64 import b64decode, b64encode
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum

from provider_core.attempt_request import require_analysis_kind, require_provider_ref, require_start_body
from provider_core.attempt_response import ATTEMPT_RESPONSE_BYTES, AttemptAdmission, read_start
from provider_core.job_input import FeedInputFacts
from provider_core.response_json import ResponseRejected, read_document
from provider_core.signing import _https_origin

# Pending starts only: 4096 request bytes become at most 5464 base64 characters;
# two scope identities of 1024 scalars each need at most 24576 JSON escape bytes.
# The encoder checks total size, including the API origin and metadata. This bound
# is not the future terminal-record retention reservation.
MAX_PENDING_RECORD_BYTES = 65536
# One bounded pending document plus one bounded admission and its small envelope.
MAX_RECORD_BYTES = MAX_PENDING_RECORD_BYTES + ATTEMPT_RESPONSE_BYTES + 256


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
    Preparation stays in memory, as in attempt_lifecycle.py::PreparedForExecution.
    """

    start: StartPending
    admission: AttemptAdmission
    local_phase: LocalExecutionPhase

    def __post_init__(self):
        if type(self.start) is not StartPending or type(self.local_phase) is not LocalExecutionPhase:
            raise ValueError("Cannot retain acknowledgement: original start and explicit local execution phase are required")
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


def acknowledge_start(start, admission):
    return AcknowledgedAttempt(start, admission, LocalExecutionPhase.NOT_ENTERED)


def mark_execution_entered(record):
    """Record intent before invoking a prepared handle; never infer actual execution."""
    if type(record) is not AcknowledgedAttempt or record.local_phase is not LocalExecutionPhase.NOT_ENTERED:
        raise ValueError("Cannot enter execution: an acknowledged Attempt that has not entered is required")
    return replace(record, local_phase=LocalExecutionPhase.EXECUTION_ENTERED)


def pending_start(record):
    """Return the single retained identity owner for supported lifecycle states."""
    if type(record) is StartPending:
        return record
    if type(record) is AcknowledgedAttempt:
        return record.start
    raise ValueError("Cannot use Attempt record: its lifecycle record type is unsupported")


def require_transition(expected, replacement):
    """Permit implemented forward transitions without changing an obligation's facts."""
    if pending_start(expected) != pending_start(replacement):
        raise ValueError("Cannot replace Attempt record: original start facts differ")
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


def encode_record(record):
    """Encode exact replay bytes; the store must separately confirm durability."""
    start = pending_start(record)
    raw_start = _encode_pending(start)
    if type(record) is StartPending:
        return raw_start
    raw = json.dumps(
        {
            "schema_id": "provider_core.acknowledged.v1",
            "start": json.loads(raw_start),
            "admission": json.loads(_admission_bytes(record.admission)),
            "local_phase": record.local_phase.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Cannot encode Attempt record: acknowledged record exceeds its byte limit")
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


def decode_record(raw, *, scope):
    """Restore bounded historical facts; no loaded state authorizes scientific replay."""
    try:
        document = read_document(raw, MAX_RECORD_BYTES)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot restore Attempt record: {cause}") from None
    if type(document) is not dict or document.get("schema_id") != "provider_core.acknowledged.v1":
        return _decode_pending(raw, scope=scope)
    _fields(document, {"schema_id", "start", "admission", "local_phase"}, "acknowledged record")
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
    return AcknowledgedAttempt(start, admission, phase)
