"""Own one Job's inspected sources, fresh grants and bounded verified bytes.

Sources: the API revision in README, upload_transfer_http.py::_read_response_start
and upload_transfer_http_contract.py; SECS 34f5915c1534278bbf61b4c6592afa44cf94ab08,
src/secs_inference/provider/{job_api,upload_download}.py establish the operation
ordering precedent. No sibling implementation is imported. Membership observation
is not an atomic freeze: changes after the final refresh cannot be excluded.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import base64
import hashlib
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from provider_core.transport import ExchangeFailed
from provider_core.upload_response import require_job_ref, require_upload_ref


class UploadFailed(RuntimeError):
    """Safe acquisition reason with protected original evidence in cause."""

    def __init__(self, reason, *, cause=None):
        super().__init__(f"Cannot acquire Upload: {reason}")
        self.cause = cause
        self.grant_issued = False


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """Explicit policy for one source session; repeated calls do not reset it."""

    per_source_bytes: int
    total_bytes: int
    requests: int
    seconds: float

    def __post_init__(self):
        for value in (self.per_source_bytes, self.total_bytes, self.requests):
            if type(value) is not int or value <= 0:
                raise ValueError("Cannot configure Upload acquisition: byte and request limits must be positive integers")
        if type(self.seconds) not in (int, float) or not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("Cannot configure Upload acquisition: duration must be finite positive seconds")


@dataclass(frozen=True, slots=True)
class VerifiedUpload:
    job_ref: str
    upload_ref: str
    byte_length: int
    content_hash: str
    data: bytes = field(repr=False)

    def __post_init__(self):
        if type(self.data) is not bytes or len(self.data) != self.byte_length:
            raise UploadFailed("downloaded byte length differs from the grant")
        if "sha256:" + hashlib.sha256(self.data).hexdigest() != self.content_hash:
            raise UploadFailed("downloaded SHA-256 differs from the grant")


def _store_origin(value):
    """Require an explicit canonical origin before any bearer can be attached."""
    if type(value) is not str or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("Cannot configure Upload store: origin must be visible ASCII HTTPS text")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("Cannot configure Upload store: origin is not a valid HTTPS URL") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.raw_path != b"/"
        or str(url) != value
        or value.endswith("/")
    ):
        raise ValueError(
            "Cannot configure Upload store: supply a canonical HTTPS origin without credentials, path, query or fragment"
        )
    return value


class JobUploads:
    """One serial source session, with no byte cache or implicit grant retry.

    Composition supplies separate API/store transport authority and owns retirement.
    Successful calls return immutable bytes; partial or unverified bytes stay local.
    Metadata responses have their API route ceilings; the byte budget counts raw
    store response bodies, including failures. The request/deadline limits cover
    membership, issuance and store calls together, starting at construction.
    """

    def __init__(self, job_ref, *, api, store, store_origin, limits):
        require_job_ref(job_ref)
        if type(limits) is not UploadLimits:
            raise TypeError("Cannot configure Upload acquisition: explicit UploadLimits are required")
        self.job_ref = job_ref
        self._api = api
        self._store = store
        self._origin = _store_origin(store_origin)
        self._limits = limits
        self._deadline = time.monotonic() + limits.seconds
        self._requests = 0
        self._received = 0
        self._snapshot = None
        self._active = False
        self._grant_issued = False
        self.last_failure = None

    @property
    def received_bytes(self):
        return self._received

    @property
    def requests_started(self):
        return self._requests

    @asynccontextmanager
    async def _operation(self):
        if self._active:
            raise UploadFailed("another source operation is active")
        if not self._store.retirement_confirmed:
            raise UploadFailed("previous store resource retirement is unconfirmed; retire its transport before more work")
        self._active = True
        self._grant_issued = False
        self.last_failure = None
        try:
            yield
        except BaseException as failure:
            self._snapshot = None
            if self._grant_issued:
                # Successful issuance remains true even if later local checks,
                # transfer, or cancellation prevent a usable byte handoff.
                if isinstance(failure, UploadFailed):
                    evidence = failure
                else:
                    cause = self.last_failure if isinstance(failure, asyncio.CancelledError) else failure
                    evidence = UploadFailed("transfer did not complete after read access was issued", cause=cause)
                evidence.grant_issued = True
                evidence.add_note("Read access was issued before this failure and may have extended Upload retention.")
                self.last_failure = evidence
            elif not isinstance(failure, asyncio.CancelledError):
                self.last_failure = failure
            raise
        finally:
            self._active = False

    def _remaining_seconds(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise UploadFailed("the source session's total deadline expired")
        return remaining

    async def _call(self, owner, operation, *args, **kwargs):
        remaining = self._remaining_seconds()
        if self._requests >= self._limits.requests:
            raise UploadFailed(f"the source session exhausted its {self._limits.requests}-request limit")
        self._requests += 1
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                return await operation(*args, **kwargs)
        except asyncio.CancelledError:
            self.last_failure = owner.last_failure
            raise
        except TimeoutError:
            if not deadline.expired():
                raise
            raise UploadFailed("the source session's total deadline expired", cause=owner.last_failure) from None

    async def inspect(self):
        """Refresh the source list; callers choose from this observation."""
        async with self._operation():
            self._snapshot = await self._call(self._api, self._api.uploads, self.job_ref)
            return self._snapshot

    async def acquire(self, upload_ref):
        """Require unchanged Job membership, obtain access, and verify source bytes.

        Re-read the inspected membership before requesting a fresh read grant;
        reject changes rather than silently selecting from the new list.
        """
        async with self._operation():
            require_upload_ref(upload_ref)
            if self._snapshot is None:
                raise UploadFailed("inspect the current Job Uploads before selecting a source")
            selected = next((item for item in self._snapshot.members if item.upload_ref == upload_ref), None)
            if selected is None:
                raise UploadFailed("selected Upload is not in the inspected Job membership")
            if selected.byte_length > self._limits.per_source_bytes:
                raise UploadFailed(f"selected Upload exceeds the {self._limits.per_source_bytes}-byte per-source limit")
            if selected.byte_length > self._limits.total_bytes - self._received:
                raise UploadFailed("selected Upload exceeds the source session's remaining byte budget")
            refreshed = await self._call(self._api, self._api.uploads, self.job_ref)
            if refreshed != self._snapshot:
                raise UploadFailed("Job Upload membership changed; inspect the current list and select again")
            grant = await self._call(self._api, self._api.read_grant, self.job_ref, upload_ref)
            self._grant_issued = True
            if (
                grant.job_ref != self.job_ref
                or grant.upload_ref != upload_ref
                or grant.byte_length != selected.byte_length
                or (selected.content_hash is not None and grant.content_hash != selected.content_hash)
            ):
                raise UploadFailed("download grant differs from the selected Upload identity or byte facts")
            return await self._download(grant)

    def _account_bytes(self, count):
        # Count the offending chunk too: bytes already consumed cannot be refunded.
        self._received += count
        if self._received > self._limits.total_bytes:
            raise UploadFailed(f"download exhausted the source session's {self._limits.total_bytes}-byte budget")

    async def _download(self, grant):
        target = self._origin + "/upload/v1/uploads/" + grant.upload_ref + "/bytes"
        if grant.download_url != target:
            raise UploadFailed("download grant does not name the configured store and exact Upload path")
        if grant.expires_at <= datetime.now(UTC):
            raise UploadFailed(
                "download grant expired before transfer; inspect sources before explicitly requesting fresh access"
            )
        if not grant.capability or any(ord(c) <= 32 or ord(c) >= 127 for c in grant.capability):
            raise UploadFailed("download bearer cannot be represented as a single HTTP authorization value")
        request = httpx.Request("GET", target, headers={"Authorization": "Bearer " + grant.capability})
        if str(request.url) != target:
            raise UploadFailed("HTTP client rewrote the admitted store target")
        try:
            response = await self._call(
                self._store, self._store.exchange, request, maximum_bytes=grant.byte_length, account_bytes=self._account_bytes
            )
        except ExchangeFailed as cause:
            reason = str(cause.primary) if isinstance(cause.primary, UploadFailed) else str(cause)
            raise UploadFailed(reason, cause=cause) from None
        _verify_store_headers(response, grant)
        verified = VerifiedUpload(self.job_ref, grant.upload_ref, grant.byte_length, grant.content_hash, response.body)
        self._remaining_seconds()
        return verified


def _verify_store_headers(response, grant):
    if response.status != 200:
        raise UploadFailed(f"store returned HTTP {response.status}; no verified bytes were obtained", cause=response)
    digest = base64.b64encode(bytes.fromhex(grant.content_hash.removeprefix("sha256:")))
    for name, expected in (
        (b"content-type", b"application/octet-stream"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(grant.byte_length).encode("ascii")),
        (b"content-digest", b"sha-256=:" + digest + b":"),
    ):
        if [value for key, value in response.headers if key.lower() == name] != [expected]:
            raise UploadFailed(f"store response {name.decode()} does not match the granted whole-file transfer", cause=response)
    if any(name.lower() in {b"content-encoding", b"content-range"} for name, _ in response.headers):
        raise UploadFailed("encoded or partial store responses are not admitted", cause=response)
