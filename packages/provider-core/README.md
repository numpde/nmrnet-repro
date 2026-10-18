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
