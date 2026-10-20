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

`provider_core.chat` supplies `ChatEndpointSpec`, `ChatEndpoint` and `ChatFailed`
for Chat Completions tool turns. Composition supplies the complete HTTPS URL,
model, key, optional reasoning effort, dedicated `HttpTransport` and explicit
request/response byte limits. One call sends one POST and admits one complete
assistant reply. It never retries a lost reply. The consumer owns prompts, tools,
argument admission, corrections, endpoint selection and the conversation budget.
Composition must await `retire()` before replacement or shutdown; unresolved
retirement remains owned. No ambient credentials or endpoint defaults are read.

Endpoint `identity` contains safe configuration/model IDs and a hash of request
configuration, excluding the key. It does not identify the whole TLS policy or
conversation. URL/key are hidden from repr. `ChatFailed` has safe operation text;
raw response bytes and underlying causes remain private and may contain secrets
or source text. Never put them in model feedback or public results.

The boundary follows [Magnet's chat adapter](https://github.com/numpde/magnet-deploy/blob/600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf/magnet_provider/openai_chat_interpreter.py#L164-L203):
separate endpoint facts, HTTP exchange and conversation decisions. This adapter
was written here, reusing core's stronger retirement owner rather than copying
best-effort cleanup or provider-specific retry policy. It has no scientific or
repository imports. `make test/integration/provider-core/chat` builds and tests
the installed wheel against local TLS peers without mounting consumer sources;
this proves carrier behavior, not external model interpretation.

`ProviderAPI(identity, transport).job_input(feed, analysis_kind_ref=...)` performs
one signed read and returns `VerifiedJobInput`. The caller must obtain `feed`
from an authenticated source; constructing `FeedInputFacts` only checks its shape.
The method admits the pinned response headers and delegates byte/identity checking
to the existing verifier. It neither interprets instructions nor retries failures.
Composition owns the supplied transport and its retirement.

`ProviderAPI.jobs(analysis_kind_ref=..., has_provider_execution_attempt=...,
limit=..., cursor=...)` reads exactly one signed discovery page. All choices are
explicit; pass `cursor=None` for the first page. Returned immutable Job items carry
the existing `FeedInputFacts` for `job_input()`, plus their creation time. Kind/view
echoes, field shapes, identities, calendar values and the requested page size are
admitted before any item is returned. Unknown fields/schema versions fail explicitly.
The whole response is bounded to 65536 bytes; at most 100 items are admitted.

The false presence view lists selected open Jobs with no prior Attempt by this
provider. The true view means a prior Attempt in any state; it is not an inventory
of currently running work. Composition still owns pagination, selection, claims
and fresh state checks. Core validates only the cursor's canonical base64url
representation; the server binds its opaque contents to the provider/kind/view.
No automatic follow-up request or retry occurs.

Authority and rationale: the pinned API's
[discovery route](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/routes.py#L324-L346),
[response schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/jobs_list_response.v1.schema.json),
[request parser](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/jobs_list_request.py)
and [visibility service](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/provider_job_feed_service.py#L32-L106)
own these facts. This operation supplies authenticated observations to the existing
input owner; a second discovery client in each binding would duplicate signing
and wire admission. Local TLS tests prove the connection, not deployed visibility.

`ProviderAPI.execution_attempts(state="in_progress", limit=..., cursor=...)`
reads one signed inventory page for the authenticated provider principal, across
analysis kinds and restricted to visible Jobs. All three choices are explicit;
pass `cursor=None` for the first page. The immutable page preserves server order,
Attempt/Job/kind identities, provider attempt key, state and original start time.
It admits the exact schema, canonical timestamps/cursors and requested count,
within the API's 100-item and 65536-byte response ceilings. Keys and cursors stay
out of repr. Shared paging owns the same wire grammar used by Job discovery.

Inventory is a live observation. A separate `read_attempt()` binds a fresh point
read to the selected Attempt/Job identities and obtains Job state; inventory
contains no Job state or terminal receipt. Cursors remain opaque and bind to the
provider principal and fixed view on the server. No automatic pagination, retry,
claim, resume or abandonment occurs. An absent row does not prove that an Attempt
never existed or that execution/publication completed. Traversal can change while
work progresses; composition owns its request/time budget and recovery policy.

Authority and rationale: the pinned API's
[inventory route](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/routes.py#L350-L365),
[request parser](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/execution_attempts_list_request.py),
[response schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempts_list_response.v1.schema.json),
[cursor owner](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/execution_attempts_list_cursor.py)
and [visibility service](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/provider_execution_attempt_list_service.py#L38-L108)
define this distinct recovery input. Local TLS tests prove signed transmission,
identity-preserving current readback and original-owner retirement, not deployed
visibility or a production recovery decision.

`ApiFailure` carries safe operation text, HTTP status when received, and a reviewed
problem code/request ID when recognised. Original response and cause remain private
for authorized diagnostics. Unknown server text is retained there, not displayed.
Cancellation remains `CancelledError`; `ProviderAPI.last_failure` retains its
operation context and transport evidence. Downstream recipient projections remain
unfinished composition work.

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
The signed read is implemented and locally tested over TLS. `AttemptRecovery`
and `AttemptController` below consume it; startup recovery scheduling and deployed
provider composition remain unfinished.

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
fresh authentication and cancellation closure. These wire tests do not establish
durable recovery; the storage and recovery owners below have separate witnesses.

`ProviderAPI.progress_attempt(execution_attempt_ref, phase=..., condition_code=...)`
publishes one current observation and returns `AttemptProgress`. Either `preparing`
or `running` can be the first phase; running does not prove model execution began.
The condition is null or a public code from the consumer's reviewed vocabulary.
It must not encode input, endpoint or exception text; grammar validation alone
cannot establish that a code is suitable for publication.

The response binds the Attempt, phase and condition to the submitted snapshot.
Repeating stored facts returns their original timestamp, even after terminalization;
the timestamp is not a heartbeat. The caller must decide whether an observation
is still current. Core neither queues old progress nor retries it automatically.
Recognised terminal-progress and phase-regression conflicts establish that this
update made no change. They do not resolve an earlier uncertain update, restart
science or confirm delivery of a particular result. Other uncertain failures retain
the same protected evidence and current-send effect flag as start.

Authority and rationale: pinned API
[progress request schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_progress_request.v1.schema.json),
[progress response schema](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_progress_response.v1.schema.json)
and [progress service](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/execution_attempt_progress.py).
The two refusal meanings come from their explicit
[operation error contracts](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/errors.py),
which are more specific than the shared HTTP effect table. Local TLS tests cover
the PUT body/digest and cancellation closure; `AttemptExecutor` below owns lifecycle
ordering. Deployed workflow qualification remains unfinished.

`complete_body(attempt_ref, result_schema_id, result_bytes)` and
`fail_body(attempt_ref, failure_code, failure_message)` in `attempt_request` build
terminal requests for durable retention. The lifecycle owner must record the chosen
operation and exact body before calling `ProviderAPI.complete_attempt(body,
execution_attempt_ref=...)` or `fail_attempt(...)`. An uncertain completion must not
be replaced with failure, another result, or a fresh scientific run.

Result bytes are opaque, nonempty and limited to 786432 bytes. Core requires
canonical base64 in retained completion requests, but imposes no UTF-8, JSON or
scientific interpretation on their decoded content. The acknowledgement binds the
Attempt, exact schema identity, computed SHA-256 and byte length. The returned
analysis-result reference is separately validated; it is not the content hash.
Failure acknowledgements match the exact code and message. Both records preserve
the original commit timestamp and replay flag.

Schema identities and failure messages preserve permitted whitespace, controls and
Unicode scalar text without trimming or truncation. The consumer must provide a
publishable explanation and protect confidential material beforehand; core does
not guess which text is secret. Message/schema fields are omitted from record repr,
and validation errors never echo their contents. Public error codes explain known
terminal conflicts using local text. Such refusals establish no change for this
send and leave earlier uncertain publication evidence intact.

Wire authorities at the pinned API revision:
[completion request](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_complete_request.v1.schema.json),
[completion response](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_complete_response.v1.schema.json),
[failure request](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_fail_request.v1.schema.json)
and [failure response](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/provider/schemas/execution_attempt_fail_response.v1.schema.json).
The [outcome service](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/execution_attempt_outcome.py)
and [effect vocabulary](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/security/problems.py)
justify exact terminal reconciliation and the current-send refusal distinctions.
Local TLS tests verify loss after full request receipt, explicit identical resend,
body hashes/signatures, maximum result and failure-message transfer, and cancellation
closure. The scripted peer does not commit domain state: these tests do not prove
server idempotency, local persistence/restart recovery or deployed publication.

`attempt_record.StartPending` captures exact start bytes, expected analysis kind
and `FeedInputFacts` under a `RecoveryScope` containing canonical API origin,
provider, consumer/lane identity and binding generation. It contains no signing
credential. `encode_record` emits bounded JSON; `decode_record(raw, scope=...)`
requires the expected scope and rejects foreign identities before returning an
obligation. Request whitespace/order remain unchanged inside the record. Encoding
refuses records over 65536 bytes rather than emitting data its decoder cannot read.

This pending-start codec follows
[NMRPeak's pending-start record](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_journal.py)
and [Magnet's retained owner binding](https://github.com/numpde/magnet-deploy/blob/c5813ee35cfbb1c98a5cc5d20a94787271f26486/magnet_provider/terminal_outbox.py).
The consumer/lane identity prevents accidental cross-lane restore; generation
identity prevents implicit adoption by changed scientific code. These checks are
neither authentication nor a lock. The codec does not persist data, reserve disk
space, dispatch requests or authorize scientific recovery. Composition must supply
a budget covering full future terminal records and their required evidence, beyond
these smaller pending-start records. Headroom checks do not physically reserve
space. Terminal record states and their protected evidence are described below.

`AttemptStore` now retains pending starts in a precreated, private directory. It
uses a local exclusive file lock, bounded inventory, exclusive staging, file sync
and directory sync. An identical admission returns the retained record; different
facts under the same key are refused. The capacity policy accounts for full future
records and staging, but available-space checks cannot reserve blocks against
unrelated writers. Composition must supply the eventual full-record size budget.

The async store owns one filesystem operation at a time. Cancellation or timeout
ends the wait, not the write: `settle()` observes the original operation, while new
work and closure remain blocked. Failed publication preserves staging and any
installed record, blocks reuse, and distinguishes visibility from durability.
Unknown or incomplete staging entries prevent reopening until explicitly reconciled.
A failed descriptor close requires enclosing-process retirement even if other
descriptors close successfully. Safe errors retain original and cleanup causes for
protected diagnostics.

This ordering follows
[NMRPeak's journal storage](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_journal_store.py)
(`admit`, `_replace_record`, `_require_recovery_space`), while preserving its
suppressed cleanup errors. The lock excludes cooperating processes using the same
directory inode; it does not coordinate hosts or protect a replaced directory path.
Storage admission alone grants no API or scientific execution authority.

`AcknowledgedAttempt` retains the original `StartPending`, the server's admitted
start response and a separate local execution phase. A terminal start replay can
be retained but cannot enter execution. `acknowledge_start` and
`mark_execution_entered` construct forward transitions; `AttemptStore.replace`
requires the exact currently retained record before staging and syncing its
replacement. Stale, skipped, repeated and backward transitions are refused. Failed
replacement preserves the original or the installed replacement and blocks reuse
until its outcome is reconciled. The record-size and minimum capacity bounds now
include the acknowledgement envelope; pending starts retain their 65536-byte limit.

Execution entry records the boundary before computation may run; it does not prove
that computation ran. Preparation remains an in-memory, session-owned handle.
Successful `AnalysisSession.prepare` returns a `PreparedAnalysis` instance carrying
binding-owned `ProtectedEvidence` for the exact selected input. Core durably retains
that identity with execution entry before passing the same instance to `execute`.
The binding owns its meaning and omission of source text and secrets. Missing
identity or an unconfirmed write prevents dispatch. The evidence limit is reused;
storage capacity reserves both this identity and independent terminal evidence.
Identity-bearing acknowledgements encode as v2. Historical v1 records keep their
original byte limit and encoding, with no invented candidate identity.
Restored `NOT_ENTERED` is not proof that such a handle exists, and restored
`EXECUTION_ENTERED` cannot authorize another entry. `AttemptExecutor` owns fresh
observations and prepared-handle use; the shared claim registry excludes duplicate
owners. Publication replay is implemented below. Generation replacement, startup
recovery scheduling and deployed recovery qualification remain composition work.

The transition boundary follows
[NMRPeak's journal transitions](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_journal.py)
(`bind_started_attempt`, `mark_execution_entered`) and its
[live prepared execution ownership](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_lifecycle.py)
(`PreparedForExecution`). Retaining terminal start acknowledgements additionally
preserves what the server reported without inventing local execution history.
Fault tests exercise replacement and directory-sync failure; a separate process
exits without cleanup after durable entry and the next owner reads that entry.
This proves local process-restart retention, not power-loss survival or a complete
scientific recovery workflow.

`TerminalPending` retains the original acknowledged Attempt, a COMPLETE or FAIL
operation, its exact request bytes, and required `ProtectedEvidence`. Evidence has
a binding-owned schema and 1–262144 immutable bytes. Core preserves it privately;
the binding owns its meaning, necessary causal and retirement observations, and
removal of secrets. Oversized evidence is refused, never truncated. Its size ceiling
follows [NMRPeak's diagnostic envelope](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_diagnostic.py);
its old-history trimming policy is deliberately not adopted for required evidence.
[SECS's execution owner](https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/src/secs_inference/provider/execution.py)
grounds retention of required failure reports before terminal publication.

`TerminalConfirmed` adds a receipt validated against the exact retained request.
`TerminalHeld` preserves an expired, opposite-outcome or replay-mismatch conflict
for that operation, with optional request correlation. These records cannot change
the original choice or evidence, return to active/pending state, or delete an
obligation. The conflict vocabulary follows the pinned
[API outcome service](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/execution_attempt_outcome.py).

The capacity/read ceiling includes a maximum 2 MiB retained completion request
(including legal whitespace), base64 expansion, protected evidence, the original
start/admission and receipt. Installed storage tests retain and reopen maximum
completion/failure requests with maximum evidence and schema text. Receipt and
hold state preserve execution history and all required evidence. Neither state
proves worker retirement or permits resource reuse. These are durable record
semantics; the binding owns worker stop confirmation, while startup recovery
scheduling remains unfinished composition work.

`TerminalPublisher` sends one exact terminal request only after observing its
matching durable record. It retains a matching receipt or reviewed conflict before
returning success. A lost reply leaves the original pending request available for
an explicit later resend; settlement itself never resends. Confirmed and held
records cannot be published again. The installed integration test exercises reply
loss over verified TLS, storage close/reopen, exact resend and durable confirmation.

`AttemptClaims` excludes simultaneous local owners of the same API/provider/Attempt
across lanes and generations, following
[Magnet's ownership registry](https://github.com/numpde/magnet-deploy/blob/c5813ee35cfbb1c98a5cc5d20a94787271f26486/magnet_provider/provider_attempt_recovery.py).
Composition must share one registry. This is event-loop-local exclusion, not a
distributed lease. Publication pins its claim while its storage or network work
remains unresolved; owner tags prevent settlement from consuming another caller's
storage result or cancelling another exchange. Publication never releases the
lifecycle claim or establishes worker retirement.

`PublicationFailed` exposes safe context and retains protected primary evidence,
ordered settlement failures and any confirmed outcome not yet durably recorded.
Cancellation still propagates. Failed receipt retention requires reconciliation;
it cannot silently become a replacement outcome or permission to rerun science.

`AttemptStarter.start()` retains the pending request before sending it once;
`resume()` requires that exact pending record to exist under the current API,
provider, lane and generation. Neither method reconstructs request bytes or retries
implicitly. Both return only after retaining the validated acknowledgement, including
terminal start replays, with local execution still `NOT_ENTERED`.

Acquire `AttemptClaims.claim_start(pending)` before either call. The start claim
excludes the provider attempt key across Jobs, lanes and generations, matching the
[API's provider-wide key constraint](https://github.com/numpde/nmr-api-v1/blob/6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8/nmr_api/operations/execution_attempt_start.py#L396-L412).
The starter adds the received remote Attempt reference to that same claim without
releasing the key. A conflicting owner preserves the received acknowledgement and
blocks handoff; it never steals the existing claim. The caller keeps both identities
until explicit release.

This ordering follows [NMRPeak's durable admission](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_lifecycle.py#L442-L508),
[start acknowledgement](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_lifecycle.py#L648-L695),
and [generation-sensitive recovery](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_lifecycle.py#L1636-L1664).
A cancelled wait retains its original I/O. `settle()` observes it without sending:
settled initial retention returns no acknowledgement; settled acknowledgement
persistence returns that exact record. Failed persistence or reference binding
keeps protected admission evidence and pins ownership for explicit reconciliation.
No acknowledgement, including successful replay, recreates a prepared handle or
permits science to run. `AttemptRecovery` below handles later retained records;
the finite controller connects NMRNet's real-worker path. Startup recovery and
continuous-service orchestration remain unfinished.

`AttemptExecutor` connects an acknowledged Attempt to an `AnalysisSession` supplied
by the binding. It checks current server state and exact Job input, confirms preparing
progress, and obtains a live prepared handle. Before invoking that handle, it confirms
running progress, durably records execution entry, then reads server state again.
This follows [NMRPeak's execution ordering](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_lifecycle.py#L1130-L1240).
The read is a snapshot; it cannot prevent a later server cancellation.

The executor retains the observed outcome before encoding it, writes terminal bytes
and protected evidence before publication, and asks the session to confirm retirement.
A failed write or retirement preserves the primary cause and any unretained outcome.
`settle()` observes original I/O and attempts outstanding cleanup; it never resumes
preparation or science. A known storage refusal carries no uncertain write effect;
an unretained scientific outcome still independently prevents ownership release.
The caller retains the lifecycle claim for subsequent publication or reconciliation.

The binding must supply bounded asynchronous session operations and actual worker
retirement evidence. The current integration test composes the real signed TLS client,
store, starter, executor and publisher with a recording scientific session. It proves
ordering and retention. The separately selected repository target
`test/integration/provider-scientific-worker` also joins real released NMRNet science
and host-confirmed container retirement to local signed publication. Its API peer
still models wire behavior; deployment and recipient diagnostics remain unfinished.

While preparation or execution runs, the executor observes server state at the
explicit `poll_seconds` interval and reads again before accepting a successful
return. This follows [NMRPeak's generation observation](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_lifecycle.py#L1287-L1340),
extended here to preparation. Polls are sequential and use the API's existing
exchange deadline. Session I/O requires a separate transport; it cannot compete
with observation on the executor's single-exchange API connection.

The same observation continues while the execution-entry record is being written.
A stop or failed poll retires the prepared session without waiting for disk work
to finish. The async entry observer and the original filesystem operation remain
separate owned obligations: cancelling the wait does not cancel or resubmit the
write. Settlement observes the entry waiter before settling storage; unresolved
work keeps the claim held. Even if persistence finishes during a poll, a stop
prevents execution; a later disk error remains secondary evidence. Successful
persistence still requires the existing fresh state read before science.

This monitoring applies to execution-entry persistence. Terminal/suppressed-evidence
retention keeps its existing semantics. An already submitted terminal write may
finish as `TerminalPending`; cancellation cannot revoke it or silently convert it
to suppression. Terminal-write responsiveness and any durable suppression policy
for that boundary remain open work.

On stop, failed observation or caller cancellation, the executor requests task
cancellation once and attempts session retirement. `task_settle_seconds` bounds
observation of the original Python task; a timeout does not cancel it again.
It also bounds observation of the cancelled entry waiter, independently of the
store's own original-I/O deadline and settlement contract.
Task completion and worker retirement are separate requirements. A result arriving
during observation or cleanup remains protected evidence, even when acceptance
fails. Such an unretained result keeps the claim held for reconciliation and is
not automatically published. Neither timing option replaces the session's aggregate
preparation/execution deadlines.

After the original task and API/storage I/O settle,
`retain_suppressed_outcome()` explicitly starts one durable write of an already
returned, admitted `AnalysisSuccess` or `AnalysisFailure`. It does not rerun science
or send an outcome. `SuppressedOutcome` keeps the exact candidate bytes, scientific
evidence and a closed suppression category: server stop, failed observation or
caller cancellation. The claim stays held while evidence is unretained or worker
retirement remains unconfirmed. Cancellation of retention leaves its original write
for `settle()` to observe; settlement never submits a replacement write.

Suppression is absorbing. Recovery returns the unchanged record without a request;
publication refuses both the record and its nested candidate because that candidate
is not the current durable pending-publication record. This preserves bounded
scientific evidence and the suppression category across restart. The full original
acceptance exception remains in memory for protected diagnostics. Arbitrary binding
exception graphs, unencodable outcomes and uncertain earlier writes need separate
reconciliation; they are not silently converted to this disposition.

`AttemptRecovery` handles one selected durable record without recreating scientific
work. Open storage under its explicit retained scope, read the selected inventory,
and reserve it with `AttemptClaims.claim_records(records)` before dispatching any
replay. Reservation checks every start-key and known remote-identity conflict before
installing any claim. `claim_record(record)` is the single-record convenience form.
Each recovery action rechecks the selected record against current storage.

| Retained record | Recovery action |
| --- | --- |
| `StartPending` | Replay the exact start through `AttemptStarter.resume` |
| `AcknowledgedAttempt` | Return the retained record and identity-bound server observation |
| `TerminalPending` | Publish the exact retained outcome through `TerminalPublisher` |
| `TerminalConfirmed` or `TerminalHeld` | Return the unchanged record without an API request |
| `SuppressedOutcome` | Return unchanged; its candidate cannot be published or executed |

This distinction follows [NMRPeak's restart decisions](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/attempt_journal.py#L534-L579).
Core deliberately leaves preparation resumption, interrupted-result policy and
record disposition to the consumer; it never synthesizes a failure or deletes a
retained obligation. Terminal replay uses the existing publisher's receipt/conflict
handling rather than duplicating the sibling's restart planner.

Keep the recovery object for `settle()` after interruption: it observes the original
operation under its original owner and never sends again. Delegated failures retain
the starter/publisher's diagnostics; readback failures preserve their own primary,
API and cleanup evidence. Successful recovery leaves the lifecycle claim with the
caller. An active readback or replay receipt does not authorize science or prove
worker retirement. Composition must exclude the previous controller's mutation
authority and confirm old worker/grant cleanup before resource reuse; a reopened
store or newly constructed claim registry alone cannot establish either fact.

`make test/integration/provider-core` includes three source-grounded Magnet outcome
fixtures: exact source/model-input/product-byte preservation, binding-certified
pre-entry unavailability with an explicit same-request retry, and indeterminate
prediction whose saved execution entry prevents replay after reopening storage.
The fixture adapts Magnet's
[outcome meanings](https://github.com/numpde/magnet-deploy/blob/c5813ee35cfbb1c98a5cc5d20a94787271f26486/magnet_provider/analysis_contract.py#L77-L94)
to binding exceptions and existing durable phases. It models the required future
split in [G01's current processor](https://github.com/numpde/magnet-deploy/blob/c5813ee35cfbb1c98a5cc5d20a94787271f26486/models/g_01_nmr_forward/provider/magnet_provider_bindings/g_01_nmr_forward/processor.py#L182-L283).
Its synthetic payloads and scripted retirement establish neither G01 scientific
validity nor current processor compatibility, real worker stop, readiness ordering
or migration. Those and the SECS/NMRPeak contracts remain separate qualification.

The same lane runs four scripted subprocess cases: success after durable execution
entry, preparation rejection before entry, cancellation during generation, and
output-channel closure while the exact child remains alive. The last case holds
the Attempt claim through failed retirement and explicit process cleanup; only
settling the original executor releases it. Exit/reaping and owned-pipe cleanup
are observed separately from cancellation or EOF. Tests use a fixed authored
peer, synchronization replies and bounded cleanup, with no scientific workload.

This distinction follows
[NMRPeak's channel retirement](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/runner_session.py#L395-L443)
and [SECS's supervisor-owned stop confirmation](https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/src/secs_inference/provider/worker.py#L251-L295).
The cases qualify the reusable session boundary only. They do not qualify either
sibling's current protocol, scientific admission, process tree, grant cleanup or
warm reuse, and do not replace the required sibling adoption tests.

Every `AttemptExecutor` caller must choose `allowed_job_states`, an immutable,
nonempty `frozenset` drawn from `open` and `closed`. Existing NMRNet
callers choose `frozenset({"open"})`. A consumer that continues an existing Attempt
after Job closure can explicitly choose `frozenset({"open", "closed"})`, following
[SECS's policy](https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/src/secs_inference/provider/analysis_run.py#L260-L265).
Terminal Attempts and cancelled Jobs always stop work. The selection
applies to every observation, including preparation, persistence and execution,
and the final observation before accepting a returned result. It grants no new
start or publication authority. Failed observations still stop execution; SECS's
separate temporary-outage policy remains an adoption obligation.

`AttemptController(api, store, claims, session, allowed_job_states=...,
poll_seconds=..., task_settle_seconds=...)` owns one explicitly selected Attempt.
After validating configuration it accepts the scientific session, including
cleanup before a start succeeds. `run(pending)` joins the existing start,
execution and publication owners and returns their durable record. An already
terminal start replay retires the allocated session and returns its acknowledgement
with no scientific execution. Existing unresolved records require explicit
recovery; confirmed records remain subject to ordinary store capacity.

Retain the controller when an operation fails or is cancelled. `settle()` observes
the original operation and confirms cleanup; a late acknowledgement cannot enter
science and a late result cannot initiate publication. Its read-only `operation`
exposes the retained core owner for diagnosis and explicit executor outcome
retention. `last_failure` keeps the original cause and later cleanup failures.
Recovery and scheduling stay explicit. The controller releases its private claim
only after obligations settle and the session's cleanup is confirmed, and accepts
no second run. API/store lifetime remains with the caller.

NMRNet's installed TLS and released-worker tests now consume this runtime owner.
The [controller qualification evidence](../../artifacts/provider-controller-cpu-float32.json)
records one actual scientific invocation and completion, host-confirmed container
retirement and grant cleanup, and a reopened terminal confirmation. Its 81 source
hashes identify the executed implementation. This local API peer does not establish
deployed API/GUI behavior, scientific accuracy or maximum workload capacity.
