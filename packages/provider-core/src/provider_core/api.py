"""Own signed API operations; scientific admission remains with the consumer.

Wire authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/routes.py::JOB_INPUT_READ_ROUTE and
nmr_api/security/analysis_kind_refs.py. The response ceiling is the API's
131072-byte route contract, not an estimate from a typical input envelope.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import json
import re
import secrets
import time

from provider_core.job_input import MAX_RESPONSE_BYTES, FeedInputFacts, JobInputRejected, verify_job_input
from provider_core.signing import signed_request
from provider_core.transport import ExchangeFailed
from provider_core.upload_response import (
    GRANT_RESPONSE_BYTES,
    MEMBERSHIP_RESPONSE_BYTES,
    UploadResponseRejected,
    read_grant,
    read_membership,
    require_job_ref,
    require_upload_ref,
)


class ApiFailure(RuntimeError):
    """Safe operation context with private evidence for authorized diagnostics.

    response and cause can contain private instructions or credentials. They must
    not be rendered verbatim. This owner never retries an operation implicitly.
    """

    def __init__(self, operation, reason, *, response=None, cause=None):
        super().__init__(f"Cannot {operation}: {reason}")
        self.operation = operation
        self.response = response
        self.status = response.status if response is not None else None
        self.cause = cause
        self.problem_code = None
        self.request_id = None
        self.issuance_may_have_committed = False


# Reviewed subset of security/problems.py::PUBLIC_PROBLEM_CODES at the API
# revision above. Local text avoids disclosing arbitrary server detail/title.
_PROBLEM_REASONS = {
    (
        401,
        "authentication-failed",
        "authentication_failed",
    ): "API could not authenticate the request; check the configured provider credential",
    (401, "authentication-failed", "authentication_nonce_reused"): "API rejected a reused signature nonce",
    (
        401,
        "authentication-failed",
        "authentication_window_closed",
    ): "the signature acceptance window closed; check the provider clock and request delay",
    (
        401,
        "authentication-failed",
        "signature_expired",
    ): "the request signature expired; check the provider clock and request delay",
    (
        401,
        "authentication-failed",
        "signature_created_in_future",
    ): "the signature timestamp is in the future; check the provider clock",
    (403, "authorization-denied", "authorization_denied"): "API denied this provider access to the requested resource",
    (404, "not-found", "resource_not_found"): "the requested resource was not found or is not accessible to this provider",
    (
        409,
        "operation-conflict",
        "upload_read_not_finalized",
    ): "the Upload is not finalized; ask its owner to check and finalize the existing Upload before requesting access; do not reupload solely because of this error",
    (
        409,
        "operation-conflict",
        "project_purge_in_progress",
    ): "Project removal has started; another request cannot restore access; ask the Job owner or administrator about another authorized source",
    (503, "service-unavailable", "service_unavailable"): "API service is unavailable; no verified response was obtained",
}


def _unique_problem_fields(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("Duplicate problem field")
        document[key] = value
    return document


def _reject_problem_constant(_value):
    raise ValueError("Nonfinite problem number")


def _http_failure(response, operation):
    """Expose only reviewed problem meanings; retain unknown replies privately.

    RFC 9457 envelope and 4096-byte bound: pinned API security/problems.py.
    Request correlation syntax: request_ids.py (1..128 visible ASCII bytes).
    This reads diagnostics, not mutation-effect certainty or retry permission.
    """
    failure = ApiFailure(operation, f"API returned HTTP {response.status}; no verified response was obtained", response=response)
    content_types = [value for key, value in response.headers if key.lower() == b"content-type"]
    if content_types != [b"application/problem+json"] or len(response.body) > 4096:
        return failure
    try:
        problem = json.loads(
            response.body.decode("utf-8"), object_pairs_hook=_unique_problem_fields, parse_constant=_reject_problem_constant
        )
    except (ValueError, RecursionError):
        # The original response remains evidence; malformed diagnostics must not
        # replace the HTTP failure with a parser error or leak its private text.
        return failure
    if type(problem) is not dict or set(problem) != {"type", "title", "status", "instance", "request_id", "code", "detail"}:
        return failure
    if type(problem["status"]) is not int or problem["status"] != response.status:
        return failure
    if any(type(problem[name]) is not str for name in ("type", "title", "instance", "request_id", "code", "detail")):
        return failure
    request_id = problem["request_id"]
    if re.fullmatch(r"[!-~]{1,128}", request_id) is None:
        return failure
    reason = _PROBLEM_REASONS.get((response.status, problem["type"].removeprefix("urn:nmr-api:problem:"), problem["code"]))
    if not problem["type"].startswith("urn:nmr-api:problem:") or reason is None:
        return failure
    failure = ApiFailure(operation, f"{reason} (HTTP {response.status})", response=response)
    failure.problem_code = problem["code"]
    failure.request_id = request_id
    return failure


def _success_envelope(response, operation):
    """Reject ambiguous or encoded success bodies before their wire parser."""
    if response.status != 200:
        raise _http_failure(response, operation)
    for name, expected in ((b"content-type", b"application/json"), (b"cache-control", b"no-store")):
        values = [value for key, value in response.headers if key.lower() == name]
        if values != [expected]:
            raise ApiFailure(
                operation,
                f"response must contain one {name.decode()} header with value {expected.decode()}",
                response=response,
            )
    if any(name.lower() == b"content-encoding" for name, _ in response.headers):
        raise ApiFailure(operation, "encoded response bodies are not supported by the API contract", response=response)


class ProviderAPI:
    """One execution owner's authenticated operations, with retained failure context.

    Composition owns the transport lifetime and awaits its retirement. On caller
    cancellation, last_failure preserves the operation and protected transport
    evidence while CancelledError still reaches the task owner.
    """

    def __init__(self, identity, transport):
        self._identity = identity
        self._transport = transport
        self.last_failure = None

    async def job_input(self, feed, *, analysis_kind_ref):
        """Read once and bind bytes to feed facts authenticated by the caller.

        FeedInputFacts validates shape, not provenance. Composition must obtain
        these observations from its authenticated feed before calling this method.
        """
        if type(feed) is not FeedInputFacts:
            raise ValueError("Cannot read Job input: admitted feed identity is required")
        if (
            type(analysis_kind_ref) is not str
            or len(analysis_kind_ref) > 128
            or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", analysis_kind_ref) is None
        ):
            raise ValueError(
                "Cannot read Job input: analysis kind must be a lower snake-case catalog key of at most 128 characters"
            )
        response = await self._exchange(
            "read Job input",
            method="GET",
            path=f"/provider/v1/jobs/{feed.job_ref}/input",
            query=b"analysis_kind_ref=" + analysis_kind_ref.encode("ascii"),
            maximum_bytes=MAX_RESPONSE_BYTES,
        )
        try:
            return verify_job_input(response.body, feed=feed)
        except JobInputRejected as cause:
            raise self._remember_failure(ApiFailure("read Job input", str(cause), response=response, cause=cause)) from None

    async def uploads(self, job_ref):
        """Observe current membership once; no historical or cached authority."""
        require_job_ref(job_ref)
        response = await self._exchange(
            "read Job Upload membership",
            method="GET",
            path=f"/provider/v1/jobs/{job_ref}/uploads",
            query=b"",
            maximum_bytes=MEMBERSHIP_RESPONSE_BYTES,
        )
        try:
            return read_membership(response.body, job_ref=job_ref)
        except UploadResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("read Job Upload membership", str(cause), response=response, cause=cause)
            ) from None

    async def read_grant(self, job_ref, upload_ref):
        """Request fresh access once; a lost response can follow committed issuance.

        Pinned API provider/routes.py::JOB_UPLOAD_READ_CAPABILITY_ROUTE and
        operations/provider_job_upload_read_capability.py establish this effect:
        issuance can extend retention before a failure reply. Repeating this
        operation issues a new grant, not recovery of the previous bearer.
        """
        require_job_ref(job_ref)
        require_upload_ref(upload_ref)
        response = await self._exchange(
            "issue Upload read grant",
            method="POST",
            path=f"/provider/v1/jobs/{job_ref}/uploads/{upload_ref}/read-capability",
            query=b"",
            maximum_bytes=GRANT_RESPONSE_BYTES,
            issuance=True,
        )
        try:
            return read_grant(response.body, job_ref=job_ref, upload_ref=upload_ref)
        except UploadResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("issue Upload read grant", str(cause), response=response, cause=cause), issuance=True
            ) from None

    def _remember_failure(self, failure, *, issuance=False):
        # Reviewed 401 meanings establish no domain change (security/problems.py).
        # These two grant-specific 409 branches precede issuance in operations/
        # provider_job_upload_read_capability.py; errors.py explicitly promises
        # no new link. Generic 403/404/409 statuses make no such assertion.
        refused = failure.problem_code is not None and (
            failure.status == 401
            or (failure.status == 409 and failure.problem_code in {"upload_read_not_finalized", "project_purge_in_progress"})
        )
        if issuance and not refused:
            failure.issuance_may_have_committed = True
            failure.add_note(
                "Download access may already have been issued and extended Upload retention; no retry was attempted."
            )
        self.last_failure = failure
        return failure

    async def _exchange(self, operation, *, method, path, query, maximum_bytes, issuance=False):
        request = signed_request(
            self._identity,
            method=method,
            path=path,
            query=query,
            body=None,
            created=int(time.time()),
            nonce=secrets.token_bytes(16),
        )
        self.last_failure = None
        try:
            response = await self._transport.exchange(request, maximum_bytes=maximum_bytes)
        except asyncio.CancelledError:
            self._remember_failure(
                ApiFailure(
                    operation,
                    "request cancelled; transport retirement must be confirmed before reuse",
                    cause=self._transport.last_failure,
                ),
                issuance=issuance,
            )
            raise
        except ExchangeFailed as cause:
            raise self._remember_failure(ApiFailure(operation, str(cause), cause=cause), issuance=issuance) from None
        try:
            _success_envelope(response, operation)
        except ApiFailure as failure:
            self._remember_failure(failure, issuance=issuance)
            raise
        return response
