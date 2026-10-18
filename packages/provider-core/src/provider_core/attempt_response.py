"""Admit lifecycle observations without inferring work or publication history.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/routes.py::EXECUTION_ATTEMPT_READ_ROUTE and
nmr_api/provider/schemas/execution_attempt_read_response.v1.schema.json.
Readback contains no terminal content; exact terminal resend, implemented by
the publication owner, must establish whether retained outcome bytes committed.
"""

from provider_core.container_guard import require_container

require_container()

import re
from dataclasses import dataclass
from datetime import datetime

from provider_core.response_json import ResponseRejected, read_document

ATTEMPT_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    """Independent point-in-time states, not permission to start or repeat work."""

    execution_attempt_ref: str
    job_ref: str
    state: str
    job_state: str


@dataclass(frozen=True, slots=True)
class AttemptAdmission:
    """Confirmed start identity and snapshot; not proof of local execution entry."""

    execution_attempt_ref: str
    job_ref: str
    analysis_kind_ref: str
    provider_ref: str
    state: str
    started_at: datetime
    replayed: bool


def require_attempt_ref(value):
    if type(value) is not str or re.fullmatch(r"execution_attempt:sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(
            "Execution Attempt reference must be execution_attempt:sha256: followed by 64 lowercase hexadecimal digits"
        )


def read_attempt(raw, *, execution_attempt_ref, job_ref):
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {"schema_id", "execution_attempt_ref", "job_ref", "state", "job_state"}:
        raise ResponseRejected("Response fields do not match the supported Execution Attempt read schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_read_response.v1":
        raise ResponseRejected("Response schema does not match the Execution Attempt read contract")
    if document["execution_attempt_ref"] != execution_attempt_ref or document["job_ref"] != job_ref:
        raise ResponseRejected("Execution Attempt or Job identity differs from the request")
    if document["state"] not in ("in_progress", "succeeded", "failed", "expired"):
        raise ResponseRejected("Execution Attempt state must be in_progress, succeeded, failed or expired")
    if document["job_state"] not in ("open", "closed", "cancelled"):
        raise ResponseRejected("Job state must be open, closed or cancelled")
    return AttemptObservation(execution_attempt_ref, job_ref, document["state"], document["job_state"])


def read_start(raw, *, job_ref, analysis_kind_ref, provider_ref):
    """Bind admission to expected catalog identities, including terminal replays.

    Exact wire authority: the pinned API's provider/schemas/
    execution_attempt_start_response.v1.schema.json. Provider identity is checked
    against composition's expectation, never inferred from a credential key name.
    """
    document = read_document(raw, ATTEMPT_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {
        "schema_id",
        "execution_attempt_ref",
        "job_ref",
        "analysis_kind_ref",
        "provider_ref",
        "state",
        "started_at",
        "replayed",
    }:
        raise ResponseRejected("Response fields do not match the supported Execution Attempt start schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_start_response.v1":
        raise ResponseRejected("Response schema does not match the Execution Attempt start contract")
    if (document["job_ref"], document["analysis_kind_ref"], document["provider_ref"]) != (
        job_ref,
        analysis_kind_ref,
        provider_ref,
    ):
        raise ResponseRejected("Started Attempt Job, analysis kind or provider identity differs from the expected start")
    try:
        require_attempt_ref(document["execution_attempt_ref"])
    except ValueError as cause:
        raise ResponseRejected(str(cause)) from None
    if document["state"] not in ("in_progress", "succeeded", "failed", "expired"):
        raise ResponseRejected("Execution Attempt state must be in_progress, succeeded, failed or expired")
    if type(document["replayed"]) is not bool:
        raise ResponseRejected("Attempt replayed must be a boolean")
    started_at = document["started_at"]
    if (
        type(started_at) is not str
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.(?!000000)[0-9]{6})?Z", started_at) is None
    ):
        raise ResponseRejected("Attempt start time must be canonical UTC seconds or six nonzero fractional digits")
    try:
        timestamp = datetime.fromisoformat(started_at)
    except ValueError:
        raise ResponseRejected("Attempt start time is not a valid calendar date and time") from None
    return AttemptAdmission(
        document["execution_attempt_ref"],
        job_ref,
        analysis_kind_ref,
        provider_ref,
        document["state"],
        timestamp,
        document["replayed"],
    )
