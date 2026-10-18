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

from provider_core.attempt_inventory import inventory_query, read_attempt_inventory
from provider_core.attempt_request import (
    progress_body,
    read_complete_body,
    read_fail_body,
    require_analysis_kind,
    require_provider_ref,
    require_start_body,
)
from provider_core.attempt_response import (
    ATTEMPT_RESPONSE_BYTES,
    read_attempt,
    read_complete,
    read_fail,
    read_progress,
    read_start,
    require_attempt_ref,
)
from provider_core.job_feed import PAGE_RESPONSE_BYTES, jobs_query, read_jobs_page
from provider_core.job_input import MAX_RESPONSE_BYTES, FeedInputFacts, JobInputRejected, verify_job_input
from provider_core.response_json import ResponseRejected
from provider_core.signing import signed_request
from provider_core.transport import ExchangeFailed
from provider_core.upload_response import (
    GRANT_RESPONSE_BYTES,
    MEMBERSHIP_RESPONSE_BYTES,
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
        self.attempt_mutation_may_have_committed = False


# Reviewed subset of security/problems.py::PUBLIC_PROBLEM_CODES at the API
# revision above. Local text avoids disclosing arbitrary server detail/title.
_PROBLEM_REASONS = {
    (
        409,
        "operation-conflict",
        "execution_attempt_outcome_expired",
    ): "the Attempt expired before this outcome could be accepted; this send made no change; retain prior publication evidence",
    (
        409,
        "operation-conflict",
        "execution_attempt_completion_after_failure",
    ): "the Attempt already has a failure outcome; this completion made no change; do not rerun the analysis to resolve publication",
    (
        409,
        "operation-conflict",
        "execution_attempt_failure_after_success",
    ): "the Attempt already has a successful outcome; this failure publication made no change; retain the original outcome",
    (
        409,
        "operation-conflict",
        "execution_attempt_completion_replay_mismatch",
    ): "the submitted result differs from the recorded completion; this send made no change; reconcile using the original retained publication",
    (
        409,
        "operation-conflict",
        "execution_attempt_failure_replay_mismatch",
    ): "the submitted failure differs from the recorded failure; this send made no change; reconcile using the original retained publication",
    (
        409,
        "operation-conflict",
        "execution_attempt_progress_terminal",
    ): "the Attempt is already terminal and cannot accept changed progress; this update changed neither progress nor outcome; do not retry it or restart the analysis",
    (
        409,
        "operation-conflict",
        "execution_attempt_progress_regression",
    ): "the Attempt already has running progress and cannot return to preparing; this update did not change progress; check progress reporting instead of retrying it or restarting the analysis",
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
    (
        409,
        "operation-conflict",
        "provider_attempt_key_conflict",
    ): "the retained attempt key conflicts with an existing start; this send made no change",
    (
        409,
        "operation-conflict",
        "job_provider_attempt_limit_reached",
    ): "this provider has reached the Job's attempt limit; this send made no change",
    (409, "operation-conflict", "job_attempt_limit_reached"): "the Job has reached its attempt limit; this send made no change",
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

    @property
    def origin(self):
        return self._identity.origin

    @property
    def retirement_confirmed(self):
        return self._transport.retirement_confirmed

    @property
    def pending_owner(self):
        return self._transport.pending_owner

    async def retire(self, *, owner=None):
        """Settle this API owner's transport; no domain mutation is retried."""
        await self._transport.retire(owner=owner)

    async def jobs(self, *, analysis_kind_ref, has_provider_execution_attempt, limit, cursor, owner=None):
        """Read one discovery page; composition owns selection and pagination."""
        query = jobs_query(analysis_kind_ref=analysis_kind_ref, has_provider_execution_attempt=has_provider_execution_attempt,
                           limit=limit, cursor=cursor)
        response = await self._exchange("list Jobs", owner=owner, method="GET", path="/provider/v1/jobs",
                                        query=query, maximum_bytes=PAGE_RESPONSE_BYTES)
        try:
            return read_jobs_page(response.body, analysis_kind_ref=analysis_kind_ref,
                                  has_provider_execution_attempt=has_provider_execution_attempt, limit=limit)
        except ResponseRejected as cause:
            raise self._remember_failure(ApiFailure("list Jobs", str(cause), response=response, cause=cause)) from None

    async def execution_attempts(self, *, state, limit, cursor, owner=None):
        """Read one provider-wide inventory page; composition owns reconciliation."""
        query = inventory_query(state=state, limit=limit, cursor=cursor)
        response = await self._exchange("list in-progress Attempts", owner=owner, method="GET",
                                        path="/provider/v1/execution-attempts", query=query,
                                        maximum_bytes=ATTEMPT_RESPONSE_BYTES)
        try:
            return read_attempt_inventory(response.body, limit=limit)
        except ResponseRejected as cause:
            raise self._remember_failure(ApiFailure("list in-progress Attempts", str(cause), response=response, cause=cause)) from None

    async def job_input(self, feed, *, analysis_kind_ref, owner=None):
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
            owner=owner,
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
        except ResponseRejected as cause:
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
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("issue Upload read grant", str(cause), response=response, cause=cause), issuance=True
            ) from None

    async def read_attempt(self, execution_attempt_ref, *, job_ref, owner=None):
        """Observe both states for the locally owned Attempt and Job.

        The expected Job binds readback to the caller's retained obligation.
        Neither state proves that scientific work ran or that particular result
        bytes committed; recovery must retain its own execution/publication facts.
        """
        require_attempt_ref(execution_attempt_ref)
        require_job_ref(job_ref)
        response = await self._exchange(
            "read Execution Attempt",
            owner=owner,
            method="GET",
            path=f"/provider/v1/execution-attempts/{execution_attempt_ref}",
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
        )
        try:
            return read_attempt(response.body, execution_attempt_ref=execution_attempt_ref, job_ref=job_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("read Execution Attempt", str(cause), response=response, cause=cause)
            ) from None

    async def start_attempt(self, body, *, job_ref, analysis_kind_ref, provider_ref, owner=None):
        """Send retained start bytes once; recovery calls must reuse those bytes.

        Composition persists body before dispatch and the returned identity before
        scientific work. A replay may return a terminal Attempt. Admission alone
        never authorizes execution or resolves local execution-entry uncertainty.
        """
        require_job_ref(job_ref)
        require_analysis_kind(analysis_kind_ref)
        require_provider_ref(provider_ref)
        require_start_body(body, job_ref=job_ref)
        response = await self._exchange(
            "start Execution Attempt",
            method="POST",
            path="/provider/v1/execution-attempts/start",
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_start(response.body, job_ref=job_ref, analysis_kind_ref=analysis_kind_ref, provider_ref=provider_ref)
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("start Execution Attempt", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    async def progress_attempt(self, execution_attempt_ref, *, phase, condition_code, owner=None):
        """Publish current facts once; never replay obsolete progress automatically.

        The lifecycle owner decides whether these facts still describe its latest
        observation. Neither preparing nor running proves model work or liveness.
        Public condition codes must come from the consumer's reviewed vocabulary.
        """
        require_attempt_ref(execution_attempt_ref)
        body = progress_body(phase, condition_code)
        response = await self._exchange(
            "report Attempt progress",
            owner=owner,
            method="PUT",
            path=f"/provider/v1/execution-attempts/{execution_attempt_ref}/progress",
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_progress(
                response.body, execution_attempt_ref=execution_attempt_ref, phase=phase, condition_code=condition_code
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("report Attempt progress", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    async def complete_attempt(self, body, *, execution_attempt_ref, owner=None):
        """Publish or reconcile exact retained result bytes without rerunning work.

        The lifecycle owner persists this terminal choice and body before sending.
        Uncertainty never authorizes replacing it with another result or failure.
        """
        require_attempt_ref(execution_attempt_ref)
        result_schema_id, result = read_complete_body(body, execution_attempt_ref=execution_attempt_ref)
        response = await self._exchange(
            "complete Execution Attempt",
            method="POST",
            path="/provider/v1/execution-attempts/complete",
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_complete(
                response.body, execution_attempt_ref=execution_attempt_ref, result_schema_id=result_schema_id, result=result
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("complete Execution Attempt", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    async def fail_attempt(self, body, *, execution_attempt_ref, owner=None):
        """Publish or reconcile the retained public failure, never a replacement.

        Composition owns publishable text and private evidence retention. Core
        preserves exact accepted text; it does not guess which content is secret.
        """
        require_attempt_ref(execution_attempt_ref)
        failure_code, failure_message = read_fail_body(body, execution_attempt_ref=execution_attempt_ref)
        response = await self._exchange(
            "publish Attempt failure",
            method="POST",
            path="/provider/v1/execution-attempts/fail",
            owner=owner,
            query=b"",
            maximum_bytes=ATTEMPT_RESPONSE_BYTES,
            body=body,
            attempt_mutation=True,
        )
        try:
            return read_fail(
                response.body,
                execution_attempt_ref=execution_attempt_ref,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        except ResponseRejected as cause:
            raise self._remember_failure(
                ApiFailure("publish Attempt failure", str(cause), response=response, cause=cause), attempt_mutation=True
            ) from None

    def _remember_failure(self, failure, *, issuance=False, attempt_mutation=False):
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
        # security/problems.py explicitly establishes no domain change for these
        # start refusals. operations/errors.py also explicitly establishes no
        # progress change for the two progress conflicts. These facts describe
        # this send, never an earlier lost response.
        attempt_refused = failure.problem_code is not None and (
            failure.status == 401
            or (
                failure.status == 409
                and failure.problem_code
                in {
                    "provider_attempt_key_conflict",
                    "job_provider_attempt_limit_reached",
                    "job_attempt_limit_reached",
                    "execution_attempt_progress_terminal",
                    "execution_attempt_progress_regression",
                    "execution_attempt_outcome_expired",
                    "execution_attempt_completion_after_failure",
                    "execution_attempt_failure_after_success",
                    "execution_attempt_completion_replay_mismatch",
                    "execution_attempt_failure_replay_mismatch",
                }
            )
        )
        if attempt_mutation and not attempt_refused:
            failure.attempt_mutation_may_have_committed = True
            failure.add_note(
                "The Attempt change may already have committed; retain the original request for reconciliation. No retry was attempted."
            )
        self.last_failure = failure
        return failure

    async def _exchange(
        self, operation, *, method, path, query, maximum_bytes, issuance=False, body=None, attempt_mutation=False, owner=None
    ):
        request = signed_request(
            self._identity,
            method=method,
            path=path,
            query=query,
            body=body,
            created=int(time.time()),
            nonce=secrets.token_bytes(16),
        )
        self.last_failure = None
        try:
            response = await self._transport.exchange(request, maximum_bytes=maximum_bytes, owner=owner)
        except asyncio.CancelledError:
            self._remember_failure(
                ApiFailure(
                    operation,
                    "request cancelled; transport retirement must be confirmed before reuse",
                    cause=self._transport.last_failure,
                ),
                issuance=issuance,
                attempt_mutation=attempt_mutation,
            )
            raise
        except ExchangeFailed as cause:
            raise self._remember_failure(
                ApiFailure(operation, str(cause), cause=cause), issuance=issuance, attempt_mutation=attempt_mutation
            ) from None
        try:
            _success_envelope(response, operation)
        except ApiFailure as failure:
            self._remember_failure(failure, issuance=issuance, attempt_mutation=attempt_mutation)
            raise
        return response
