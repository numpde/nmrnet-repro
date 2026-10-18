"""Retain pending-start facts under the exact owner allowed to recover them.

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
from dataclasses import asdict, dataclass, field

from provider_core.attempt_request import require_analysis_kind, require_provider_ref, require_start_body
from provider_core.job_input import FeedInputFacts
from provider_core.response_json import ResponseRejected, read_document
from provider_core.signing import _https_origin

# Pending starts only: 4096 request bytes become at most 5464 base64 characters;
# two scope identities of 1024 scalars each need at most 24576 JSON escape bytes.
# The encoder checks total size, including the API origin and metadata. This bound
# is not the future terminal-record retention reservation.
MAX_PENDING_RECORD_BYTES = 65536


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


def encode_record(record):
    """Encode exact replay bytes; the store must separately confirm durability."""
    if type(record) is not StartPending:
        raise ValueError("Cannot encode Attempt record: only pending starts are supported by this record schema")
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


def _fields(document, fields, label):
    if type(document) is not dict or set(document) != fields:
        raise ValueError(f"Cannot restore pending start: {label} fields do not match the retained schema")


def decode_record(raw, *, scope):
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
