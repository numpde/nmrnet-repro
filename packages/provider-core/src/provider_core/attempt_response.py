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

from provider_core.response_json import ResponseRejected, read_document

ATTEMPT_RESPONSE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    """Independent point-in-time states, not permission to start or repeat work."""

    execution_attempt_ref: str
    job_ref: str
    state: str
    job_state: str


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
