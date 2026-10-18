"""Admit one authenticated discovery page, without selecting or starting work.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/routes.py:324-346, provider/schemas/jobs_list_response.v1.schema.json,
provider/jobs_list_request.py and operations/provider_job_feed_service.py:32-106.
The false view lists selected open Jobs without this provider's prior Attempt.
The true view means any prior Attempt, not an inventory of active work. Neither
view grants execution. Cursors stay opaque; only their wire encoding is checked.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import binascii
import re
from dataclasses import dataclass, field
from datetime import datetime

from provider_core.attempt_request import require_analysis_kind
from provider_core.job_input import FeedInputFacts, JobInputRejected
from provider_core.response_json import ResponseRejected, read_document, read_timestamp

PAGE_RESPONSE_BYTES = 65536
MAX_PAGE_ITEMS = 100


@dataclass(frozen=True, slots=True)
class DiscoveredJob:
    input: FeedInputFacts
    analysis_kind_ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class JobPage:
    analysis_kind_ref: str
    has_provider_execution_attempt: bool
    jobs: tuple[DiscoveredJob, ...]
    next_cursor: str | None = field(repr=False)


def _require_cursor(value):
    if value is None:
        return
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("Cursor must be canonical unpadded base64url text or None for the first page")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise ValueError("Cursor must be canonical unpadded base64url text") from None
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError("Cursor must be canonical unpadded base64url text")


def jobs_query(*, analysis_kind_ref, has_provider_execution_attempt, limit, cursor):
    """Encode explicit selection choices once for the signed request target."""
    require_analysis_kind(analysis_kind_ref)
    if type(has_provider_execution_attempt) is not bool:
        raise ValueError("Job discovery requires an explicit boolean provider-attempt view")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ITEMS:
        raise ValueError("Job discovery limit must be an integer from 1 through 100")
    _require_cursor(cursor)
    view = "true" if has_provider_execution_attempt else "false"
    query = f"analysis_kind_ref={analysis_kind_ref}&has_provider_execution_attempt={view}&limit={limit}"
    if cursor is not None:
        query += "&cursor=" + cursor
    return query.encode("ascii")


def read_jobs_page(raw, *, analysis_kind_ref, has_provider_execution_attempt, limit):
    """Bind a complete page to admitted request facts; preserve server ordering."""
    document = read_document(raw, PAGE_RESPONSE_BYTES)
    if type(document) is not dict or set(document) != {
        "schema_id", "analysis_kind_ref", "has_provider_execution_attempt", "jobs", "next_cursor",
    }:
        raise ResponseRejected("Job discovery response fields do not match the supported page schema")
    if document["schema_id"] != "nmr.provider.jobs.list.response.v1":
        raise ResponseRejected("Job discovery response schema is not the supported version")
    if (document["analysis_kind_ref"] != analysis_kind_ref
            or type(document["has_provider_execution_attempt"]) is not bool
            or document["has_provider_execution_attempt"] != has_provider_execution_attempt):
        raise ResponseRejected("Job discovery analysis kind or provider-attempt view differs from the request")
    jobs = document["jobs"]
    if type(jobs) is not list or len(jobs) > MAX_PAGE_ITEMS or len(jobs) > limit:
        raise ResponseRejected("Job discovery page exceeds the requested or supported item count")
    try:
        _require_cursor(document["next_cursor"])
    except ValueError as cause:
        raise ResponseRejected(str(cause)) from None
    return JobPage(analysis_kind_ref, has_provider_execution_attempt,
                   tuple(_read_job(item, analysis_kind_ref) for item in jobs), document["next_cursor"])


def _read_job(item, analysis_kind_ref):
    if type(item) is not dict or set(item) != {
        "job_ref", "analysis_kind_ref", "input_fingerprint", "input_schema_id", "input_byte_length", "created_at",
    }:
        raise ResponseRejected("Discovered Job fields do not match the supported item schema")
    if item["analysis_kind_ref"] != analysis_kind_ref:
        raise ResponseRejected("Discovered Job analysis kind differs from the requested page")
    try:
        identity = FeedInputFacts(item["job_ref"], item["input_schema_id"], item["input_fingerprint"], item["input_byte_length"])
    except JobInputRejected as cause:
        raise ResponseRejected(str(cause)) from None
    return DiscoveredJob(identity, analysis_kind_ref, read_timestamp(item["created_at"]))
