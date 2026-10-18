"""Controlled source observations and store replies for acquisition contracts."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from provider_core.transport import HttpResponse
from provider_core.upload_response import ReadGrant, UploadMember, UploadSet
from provider_core.uploads import JobUploads, UploadLimits
from tests.fakes.uploads import UPLOAD_REF


class API:
    def __init__(self, member, grant):
        self.member = member
        self.grant = grant
        self.calls = []
        self.last_failure = None

    async def uploads(self, job_ref):
        self.calls.append("membership")
        return UploadSet(job_ref, (self.member,) if self.member is not None else ())

    async def read_grant(self, _job_ref, _upload_ref):
        self.calls.append("grant")
        if isinstance(self.grant, Exception):
            raise self.grant
        return self.grant


class Store:
    retirement_confirmed = True
    last_failure = None

    def __init__(self, body):
        self.body = body
        self.calls = []
        self.headers = ()

    async def exchange(self, request, *, maximum_bytes, account_bytes):
        self.calls.append(request)
        self.maximum_bytes = maximum_bytes
        account_bytes(len(self.body))
        return HttpResponse(200, self.headers, self.body)


def sources(*, total_bytes=100, source_bytes=100, requests=20, seconds=60):
    body = b"geometry"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    member = UploadMember(UPLOAD_REF, "private-description", len(body), digest)
    grant = ReadGrant(
        "job:example",
        UPLOAD_REF,
        len(body),
        digest,
        "https://store.example/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
        "private-bearer",
        datetime.now(UTC) + timedelta(minutes=1),
    )
    api, store = API(member, grant), Store(body)
    owner = JobUploads(
        "job:example",
        api=api,
        store=store,
        store_origin="https://store.example",
        limits=UploadLimits(source_bytes, total_bytes, requests, seconds),
    )
    return owner, api, store


def store_headers(body):
    return (
        (b"content-type", b"application/octet-stream"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode()),
        (b"content-digest", b"sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()) + b":"),
    )
