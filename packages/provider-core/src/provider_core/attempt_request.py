"""Build once and admit retained start bytes without changing replay identity.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/execution_attempt_start_request.v1.schema.json and
provider/routes.py::EXECUTION_ATTEMPT_START_ROUTE. The lifecycle owner must persist
the body before first dispatch. This module does not generate keys or persist data.
"""

from provider_core.container_guard import require_container

require_container()

import json
import re

from provider_core.response_json import ResponseRejected, read_document
from provider_core.upload_response import require_job_ref


def _require_key(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None:
        raise ValueError(
            "Cannot start Execution Attempt: provider attempt key must contain 1 through 128 ASCII letters, digits, dots, underscores, colons or hyphens"
        )


def start_body(job_ref, provider_attempt_key):
    """Encode explicit start facts once for durable retention and exact replay."""
    require_job_ref(job_ref)
    _require_key(provider_attempt_key)
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_start_request.v1",
            "job_ref": job_ref,
            "provider_attempt_key": provider_attempt_key,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def require_start_body(body, *, job_ref):
    """Check retained content; never replace it with a newly serialized document."""
    try:
        document = read_document(body, 4096)
    except ResponseRejected as cause:
        raise ValueError(f"Cannot start Execution Attempt: retained request is invalid: {cause}") from None
    if type(document) is not dict or set(document) != {"schema_id", "job_ref", "provider_attempt_key"}:
        raise ValueError("Cannot start Execution Attempt: retained request fields do not match the start schema")
    if document["schema_id"] != "nmr.provider.execution_attempt_start_request.v1" or document["job_ref"] != job_ref:
        raise ValueError("Cannot start Execution Attempt: retained request schema or Job differs from the expected start")
    _require_key(document["provider_attempt_key"])


def require_analysis_kind(value):
    if type(value) is not str or len(value) > 128 or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is None:
        raise ValueError("Analysis kind must be a lower snake-case catalog key of at most 128 characters")


def progress_body(phase, condition_code):
    """Encode the caller's current observation, not a historical replay queue.

    Pinned API provider/schemas/execution_attempt_progress_request.v1.schema.json.
    Composition must supply a public condition code, never derive one from raw
    exception/input/endpoint text. Grammar checks cannot establish that provenance.
    """
    if phase not in ("preparing", "running"):
        raise ValueError("Cannot report Attempt progress: phase must be preparing or running")
    if condition_code is not None and (
        type(condition_code) is not str
        or len(condition_code) > 128
        or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", condition_code) is None
    ):
        raise ValueError(
            "Cannot report Attempt progress: condition code must be null or a lower snake-case public code of at most 128 characters"
        )
    return json.dumps(
        {
            "schema_id": "nmr.provider.execution_attempt_progress_request.v1",
            "phase": phase,
            "condition_code": condition_code,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def require_provider_ref(value):
    if type(value) is not str or re.fullmatch(r"provider:[A-Za-z0-9_.-]{1,119}", value) is None:
        raise ValueError(
            "Provider reference must be provider: followed by 1 through 119 letters, digits, underscores, dots or hyphens"
        )
