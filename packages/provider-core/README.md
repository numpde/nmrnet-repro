# provider-core

Local, independently installed NMR API provider infrastructure. Implementation
proceeds in the slices recorded in ../../notes/017_provider_core_plan_20261018.txt.
Scientific interpretation and execution remain consumer-owned.

The supported test targets run offline in containers. The runtime launcher must
set PROVIDER_CORE_IN_CONTAINER=1; this accidental-host-execution guard does not
attest to isolation. The host/container composition owns actual grants.

Source contracts: nmr-api-v1 at 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
nmr_api/provider/schemas/job_input_read_response.v1.schema.json and
nmr_api/job_specification.py; Magnet at c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/job_input.py. Exact Job bytes are preserved before science-specific
admission. The verifier binds caller-supplied observations; it does not authenticate
a network peer or grant authority.

Signing follows the API's [RFC 9421 profile](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/security/authentication/_rfc9421_profile.py)
and [credential syntax](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/security/credential_refs.py).
Request construction adapts [Magnet's signing implementation](https://github.com/numpde/magnet-deploy/blob/c5813ee35cfbb1c98a5cc5d20a94787271f26486/magnet_provider/provider_http.py#L611-L686):
Ed25519, exact method/authority/path/query, a digest for every supplied body,
16-byte nonce and 300-second expiry. Time and nonce generation belong to the API
operation caller. HTTPX target rewriting is rejected before sending. A constructed
signature is not evidence that a server accepted a request.

`HttpTransport` owns one serial HTTPS exchange at a time and uses a fresh client
for each. It disables redirects and ambient proxy/auth configuration, bounds raw
response bytes and the total exchange duration, and allows a separately configured
finite retirement grace. Composition supplies verified TLS trust and must await
`retire()` before shutdown or replacement. Pending or failed closure blocks reuse;
process retirement remains the fallback authority when closure cannot be confirmed.
This deliberately trades connection pooling for simpler ownership. It follows
[Magnet's exchange boundary](https://github.com/numpde/magnet-deploy/blob/c5813ee35cfbb1c98a5cc5d20a94787271f26486/magnet_provider/provider_http.py#L688-L770),
but retains cleanup failures instead of adopting its best-effort release policy.
HTTPcore's public network backend retains each TCP stream before TLS begins,
so cancellation during the handshake cannot leave a socket outside cleanup's
ownership. HTTPX constructs requests; HTTPcore performs the exchange without
automatic redirects or ambient proxies. Native I/O exceptions remain protected
causes, not a public promise of HTTPX exception types.
`ExchangeFailed` exposes safe text; its original causes and interruption evidence
are for protected diagnostics and must not be rendered directly to users.

`ProviderAPI(identity, transport).job_input(feed, analysis_kind_ref=...)` performs
one signed read and returns `VerifiedJobInput`. The caller must obtain `feed`
from an authenticated source; constructing `FeedInputFacts` only checks its shape.
The method admits the pinned response headers and delegates byte/identity checking
to the existing verifier. It neither interprets instructions nor retries failures.
Composition owns the supplied transport and its retirement.

`ApiFailure` carries safe operation text, HTTP status when received, and a reviewed
problem code/request ID when recognised. Original response and cause remain private
for authorized diagnostics. Unknown server text is retained there, not displayed.
Cancellation remains `CancelledError`; `ProviderAPI.last_failure` retains its
operation context and transport evidence. Downstream recipient projections remain
part of the unfinished lifecycle/composition work.

Wire and diagnostic rationale: the API's exact
[Job-input route](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/routes.py),
[catalog-key grammar](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/security/analysis_kind_refs.py),
[problem vocabulary](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/security/problems.py)
and [request correlation syntax](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/request_ids.py)
control this boundary. Local finite-TLS tests establish transmission, socket closure
and exact-byte admission; they do not establish deployed API acceptance.

`ProviderAPI.uploads(job_ref)` returns an immutable `UploadSet` bound to the
requested Job. It preserves descriptions, ordering and pending content hashes.
`read_grant(job_ref, upload_ref)` makes one bodyless POST and returns an inert
`ReadGrant`. Its Job reference records request context; the API response itself
only echoes the Upload reference. Resource identity is distinct from content hash.
These methods do not contact the Upload store. The subsequent acquisition owner
must check configured store origin/path, current membership, expiry and byte facts.

A failed grant request can already have extended Upload retention. The failure's
`issuance_may_have_committed` is conservative current-call evidence, including on
cancellation via `last_failure`; it does not resolve any earlier request. Recognised
authentication refusals and the exact unfinished-Upload/Project-purge refusals
establish no new issuance. Generic 403/404, unavailable replies, malformed grants
and transport failures remain uncertain. No request is retried automatically.
The API's [grant service](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/provider_job_upload_read_capability.py)
and [specific refusal contracts](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/errors.py)
establish these distinctions. Bearer, URL and descriptions stay out of record repr;
they are private data, not public diagnostics.

`JobUploads(job_ref, api=..., store=..., store_origin=..., limits=UploadLimits(...))`
owns one source session. Call `inspect()`, select an Upload reference, then call
`acquire(ref)` for immutable `VerifiedUpload.data`. Acquisition refreshes the whole
observed membership, requires explicit reinspection/reselection after a change or
failure, obtains a fresh grant, and checks identity, byte facts, expiry and exact
configured store target before sending a bearer. It has no byte cache or implicit
retry. Pending membership hashes can be finalized by the grant. Changes after the
last membership observation cannot be atomically excluded by the API.

Limits cover per-source bytes, total consumed store-body bytes, total requests and
elapsed session time. Inspection, refresh, grant and transfer share the request
and time limits. Metadata uses its separate API route byte ceilings. Failed and
cancelled downloads retain consumed-byte charges; an offending received chunk is
counted even when it exceeds the limit. This is application-body accounting, not
an operating-system network-traffic quota. Response bytes remain in private memory
under the supplied byte limits until status, headers, length and SHA-256 pass.
There is no plaintext staging directory. The final deadline check rejects late
verification results; synchronous hashing is bounded by the per-source byte limit
and is not preempted mid-hash. Transport retirement grace remains separate.

The API and store use distinct supplied transports. Store requests carry only the
bearer, never provider signing material; redirects are not followed. Composition
owns both transports' retirement. `retirement_confirmed` exposes the transport's
actual ownership state, and an unresolved store retirement blocks source work.
`UploadFailed.cause` is protected evidence. `grant_issued` records successful
issuance before a later failure; uncertain issuance remains in the underlying
`ApiFailure`. Caller cancellation still propagates, with evidence in `last_failure`.

The exact store response contract comes from pinned API
[upload_transfer_http.py](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/upload_transfer_http.py)
and [upload_transfer_http_contract.py](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/upload_transfer_http_contract.py).
Membership→grant→verified bytes follows the source-ordering precedent in
[SECS job_api.py](https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/src/secs_inference/provider/job_api.py)
and [upload_download.py](https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/src/secs_inference/provider/upload_download.py).
The local implementation is async and introduces shared session accounting; it
does not import sibling code or claim sibling migration.

`ProviderAPI.read_attempt(execution_attempt_ref, job_ref=...)` reads the current
Attempt and Job states, checking both identities against the caller's retained
obligation. `AttemptObservation` preserves those states independently: an
in-progress Attempt can belong to a closed or cancelled Job. This observation
does not say whether scientific execution began or whether particular result or
failure bytes committed. The lifecycle owner must retain execution history;
publication recovery must resend the exact retained terminal operation.

The pinned [Attempt read route](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/routes.py)
and [response schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_read_response.v1.schema.json)
define these limits. Attempt and Upload responses share bounded JSON decoding
and safe `ResponseRejected` diagnostics; their schema checks stay separate.
The signed read is implemented and locally tested over TLS. Progress/outcome
mutations, durable recovery and scientific lifecycle composition remain unfinished.

`attempt_request.start_body(job_ref, provider_attempt_key)` builds a start request
from an explicit key. The lifecycle owner must persist these bytes before calling
`ProviderAPI.start_attempt(body, job_ref=..., analysis_kind_ref=..., provider_ref=...)`.
The API checks the retained bytes, sends them unchanged, and checks the returned
Job, analysis kind and provider against those expectations. Provider identity in
the server remains derived from the signing credential; it is never supplied in
the request body. Persist the returned Attempt identity before scientific work.

Recovery calls reuse the original body and key, with fresh signatures. An
`AttemptAdmission` can describe an already terminal Attempt; it does not establish
whether local scientific work began. No call generates a new key, retries, writes
durable state or starts computation. `ApiFailure.attempt_mutation_may_have_committed`
records uncertainty for this send, including cancellation through `last_failure`.
Recognised authentication refusals and the three reviewed start-conflict codes
establish no change for this send; they cannot resolve an earlier lost response.
Unknown, malformed and unavailable replies retain uncertainty and private evidence.

These rules follow the pinned API's
[start request schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_start_request.v1.schema.json),
[start response schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_start_response.v1.schema.json),
[replay ordering](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/execution_attempt_start.py)
and [current-send effect classification](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/security/problems.py).
Local TLS tests verify exact replay bytes, their digest and Ed25519 signature,
fresh authentication and cancellation closure. Durable recovery remains a separate
unfinished obligation; these tests do not claim a persisted or deployed workflow.
