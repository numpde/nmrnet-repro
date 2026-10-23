# Provider operation and configuration

This is the detailed operator reference. For scientific interpretation, start with the [scientific guide](https://numpde.github.io/nmrnet-repro/). The [README](../README.md) summarizes the supported calculation and its evidence.

## Provider startup configuration

The API/GUI provider description is the entry point to the scientific guide.
Include its URL in the configured `service.description`, for example:

> NMRNet++ predicts per-atom H/C chemical shifts from 3D molecular coordinates.
> Scientific guide, limits, references and source: https://numpde.github.io/nmrnet-repro/

Set the description in the deployment's startup configuration.
`nmrnet_provider.service.ProviderService` publishes the
admitted description through Provider Hello; operators retain ownership of their
configured text. This follows
[SECS's explicit provider-description example](https://github.com/numpde/secs-repro/blob/7356c354368f2355d3ab5cef00cf48c67cea9c2c/config/provider.toml.example#L9-L12)
and [NMRPeak's announcement description](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/provider_main.py#L49-L55).

The internal `nmrnet_provider.config.load_provider_config` loads one explicit
UTF-8 TOML file, up to 64 KiB, into the existing runtime policies. It reads the
configured credential, interpreter keys and TLS roots before any HTTP request,
worker allocation or journal acquisition.
Run its focused, offline container tests with
`make test/integration/provider-core/config`.

The file requires `schema_id = "nmrnet.provider.runtime_config.v1"` and these
closed tables; unknown fields fail startup:

| Table | Required fields | Optional fields |
| --- | --- | --- |
| `api` | `origin`, `profile`, `topology`, `provider_ref`, `credential_file`, `exchange_seconds`, `retirement_seconds` | `ca_file` |
| `interpreter` | `max_turns`, ordered `[[interpreter.endpoints]]` | none |
| each endpoint | `configuration_id`, `url`, `model`, `key_file`, `request_bytes`, `response_bytes`, `exchange_seconds`, `retirement_seconds` | `ca_file`, `reasoning_effort` |
| `uploads` | `store_origin`, `per_source_bytes`, `total_bytes`, `requests`, `seconds`, `exchange_seconds`, `retirement_seconds` | `ca_file` |
| `preparation` | `seed`, `max_iterations`, `timeout_seconds`, `generation_seconds`, `max_worker_calls`, `seconds` | none |
| `lifecycle` | `consumer_id`, `generation_id`, `store_root`, `maximum_records`, `filesystem_reserve_bytes`, `store_seconds`, `allowed_job_states`, `poll_seconds`, `task_settle_seconds`, `observation_gap_seconds`, `start_retry_window_seconds`, `terminal_retry_window_seconds` | none |
| `service` | `display_name`, `description`, `offering_description`, `page_size`, `maximum_pages`, `idle_seconds`, `api_backoff_max_seconds`, `hello_seconds`, `drain_seconds`, `settle_seconds`, `allocation_seconds`, `prediction_seconds`, `source_seconds` | none |

Paths are absolute container paths. Startup inputs must be regular files without
a final-component symlink. Secrets must belong to the container user and have
mode `0400` or `0600`; configuration and CA files must belong to root or that user,
with no group or other write permission. Parent mounts remain host-controlled.
Keys and signing credentials are limited to 16 KiB each; CA files to 1 MiB.
Interpreter keys allow one final LF or CRLF; other whitespace is rejected.
The API-issued signing credential must match the intended profile and provider.
Set API `topology` explicitly to `dev-local`, `dev` or `web`; it is a different
vocabulary from credential profiles (`dev-local`, `dev`, `run`). Every API reply
must carry the matching environment marker. Valid header request IDs remain
available for authorized diagnostics when a reply is malformed or interrupted;
they establish neither acceptance nor a safe retry.
Endpoints, models, identities and budgets have no guessed deployment defaults.
Generation's cooperative timeout must be below its channel deadline. Job states
are an explicit nonempty selection of `open` and `closed`; cancelled Jobs stop work.
Service presentation is validated before runtime resources are opened. The analysis
kind remains the implemented `peaks_from_mol`; it is not a configuration selector.
`source_seconds` controls the supplied-coordinate reader channel. SMILES generation
uses `preparation.generation_seconds`. These waits do not change the host's worker
isolation, resource ceilings or accepted generation settings.

Each HTTP role uses verified system roots unless `ca_file` supplies replacement
roots. Explicit CA bytes are read once and used directly, including when a path
changes afterward. Empty or malformed CA files fail; they never select system
roots. Ambient `SSL_CERT_FILE`, `SSL_CERT_DIR` and `SSLKEYLOGFILE` are rejected.
The analysis kind is the code-owned `peaks_from_mol` offering, following
[G01's identity](https://github.com/numpde/magnet-deploy/blob/600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf/models/g_01_nmr_forward/provider/magnet_provider_bindings/g_01_nmr_forward/identity.py).
`source_sha256` identifies only the consumed TOML bytes, not key/CA contents or
the complete qualified runtime. Owned error explanations remain public; original
file/parser evidence and independent close failures remain protected causes.

This ports [SECS's closed configuration](https://github.com/numpde/secs-repro/blob/7356c354368f2355d3ab5cef00cf48c67cea9c2c/src/secs_inference/provider/config.py#L113-L165)
and [descriptor reader](https://github.com/numpde/secs-repro/blob/7356c354368f2355d3ab5cef00cf48c67cea9c2c/src/secs_inference/provider/main.py#L111-L151),
using [NMRPeak's existing policy construction](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/provider_config.py#L91-L225).
The port retains causes without exposing private paths, completes short reads,
checks secret permissions, and binds TLS trust to the admitted bytes. The ambient
TLS rules follow the pinned runtime's
[CPython 3.12.12 SSL implementation](https://github.com/python/cpython/blob/v3.12.12/Lib/ssl.py).

## Launching the provider

The contained foreground entrypoint is `python -m nmrnet_provider.process` with
required `--config`, `--execution`, `--models` and `--diagnostics` paths, plus
`--api-proxy-socket`, `--interpreter-proxy-socket` and `--store-proxy-socket`.
Every foreground HTTP role uses its explicit Unix route; omission or an invalid
socket path refuses startup before retained storage or network acquisition. Its
installed image is built and checked through the lanes below. Execution and model declarations
are bounded to 64 KiB each and admitted before storage, HTTP or native allocation.
Execution requires exactly `image_id` (prediction), `geometry_image_id`,
`controller_image_id`, `repository_revision`, `upstream_revision`,
`runtime_identity_source = "caller"` and nonempty `tool_sources`. Image IDs use
`sha256:` plus 64 lowercase hexadecimal characters; revisions use 40. Source
names are canonical repository-relative paths with exactly integer `size` and
lowercase `sha256`; empty source files are permitted. These are caller-declared
identities, not independently observed image or source-closure attestations.
The process adds observed `execution_declaration_sha256`,
`prediction_declaration_sha256` and `startup_configuration_sha256`; callers cannot
override them. Model declarations use [the released selection](../artifacts/prediction-models.json).
Results contain only dictionary and H/C checkpoint/scaler member names, positive
sizes, SHA-256 and their archive SHA-256. Archive acquisition metadata is excluded;
other unknown fields are refused. Scientific model-byte verification stays with
the native owner.

The host entrypoint is `make provider/run`, with explicit startup paths and route
choices. `API_ROUTE`, `INTERPRETER_ROUTE` and `STORE_ROUTE` each select `local`
(an observed peer on an internal network), `external` (interface-bound DNS), or
`mapped` (an explicit IPv4 destination on the host network). Mapped routes require
`MAPPED_INTERFACE` and `MAPPED_ADDRESSES='hostname=IPv4 ...'`, with exactly the
hosts used by those routes. They preserve the configured HTTPS origin and port
for TLS and signing and never fall back to DNS. A host-gateway name is not an
address: inspect the actual host publication and supply its literal address.
`EXTERNAL_INTERFACE` and `RESOLVERS` are required only for `external`; they can
use a different interface from the mapped host publication. This follows Magnet's host-gateway
[localhost route](https://github.com/numpde/magnet-deploy/blob/600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf/compose/provider-localhost.yml),
using this repository's existing interface-bound proxy instead of an ambient host
name override. A configured launch profile is distinct from a completed API run.

## Routine logs and private diagnostics

Service and native-worker stdout and stderr are collected by Docker's `journald`
driver, including raw tracebacks and scientific exception notes. Standard Python
logging supplies routine messages. Owned messages default to INFO; add
`LOG_LEVEL=DEBUG` or `LOG_LEVEL=WARNING` to the Make launch command to change them.
Dependency messages below WARNING stay suppressed. Healthy idle polling is silent.

Read the latest 200 messages for a component or a removed native worker using its
full ID from the retained `worker.cid` receipt:

```sh
make provider/logs CONTAINER_ID=<64-character-container-id>
```

The host must provide journald and journal read access. Launch refuses an
unsupported driver; it has no alternate-driver fallback. The 1 MiB nonblocking
delivery buffer protects work from a stalled log sink and may drop messages.
Host journald settings bound shared retention, so logs may expire before an
investigation. Container removal does not itself remove journal entries.
[Docker documents the driver and full-ID filter](https://docs.docker.com/engine/logging/drivers/journald/)
and [nonblocking delivery](https://docs.docker.com/engine/logging/configure/#configure-the-delivery-mode-of-log-messages-from-container-to-log-driver).

There are no parallel stdout/stderr dump files. Raw logs can include supplied
molecular data and completed predictions retained after a failed reply; access
and retention follow the host journal policy. Resource snapshots, scientific
results, control and retirement receipts keep their existing owners; absence of
a journal message proves none of those outcomes. Startup capture/metadata helpers
publish an explicit `host-fields` handoff file, with both streams going to Docker.
Design and qualification are recorded in
[note 019](../notes/019_runtime_logging_20261023.txt).
The opt-in `make test/integration/provider/logging` lane qualifies normal, fatal
and host-interrupted synthetic processes, including post-removal retrieval and
both stdout and stderr without file dumps, without model calls or API Jobs.

## Recovering a stopped store

Stop the original host and confirm it has released the store. Preserve its evidence
and choose a **fresh empty installation root** for each recovery command. Keep the
TOML and `SOURCE_ROOT` unchanged, including the configured existing store, scope and
API identity. The bounds below are examples, not defaults:

```sh
make provider/recovery/inspect \
  CONFIG=/srv/nmrnet/input/provider.toml SOURCE_ROOT=/srv/nmrnet/input \
  INSTALLATION_ROOT=/srv/nmrnet/recovery-inspect \
  MAXIMUM_STARTUP_BYTES=131072 HOST_DRAIN_SECONDS=90
```

Inspection is offline: it grants only the TOML and retained store, without API
credentials, CA files, model keys or worker access. It prints the protected
`result.json` path after cleanup. Set `record_sha256` to the exact
`records[].record_sha256` of one selected record. For this mapped example, set
`api_interface` and `api_addresses` to your previously verified API-only binding:

```sh
make provider/recovery/run \
  CONFIG=/srv/nmrnet/input/provider.toml SOURCE_ROOT=/srv/nmrnet/input \
  INSTALLATION_ROOT=/srv/nmrnet/recovery-run \
  MAXIMUM_STARTUP_BYTES=131072 HOST_DRAIN_SECONDS=90 \
  RECORD_SHA256="$record_sha256" API_ROUTE=mapped \
  MAPPED_INTERFACE="$api_interface" MAPPED_ADDRESSES="$api_addresses"
```

The mapped hostname must match the unchanged TOML's API origin. Other route modes
use their selectors described above. Run adds only the declared API credential,
optional CA and selected API route. Pending terminal commands are replayed exactly;
confirmed records send nothing. A read-required terminal record performs one state
read and retains that observation; it never resends the terminal command.
Already-observed or not-visible records send nothing and remain unresolved: a
state read is not a matching receipt for the original result. Entered work is
observed without rerunning science. A changed record digest refuses selection.
Neither command starts science, deletes records or automatically retries a failure.
Unsettled original I/O keeps its owner and lock held for exact host retirement.

`local_obligations_settled = true` means local records are confirmed; ordinary
startup must still pass storage capacity and provider-wide API admission.
The isolated regression is `make test/integration/provider-service/recovery`:
synthetic loopback TLS, one exact completion replay and empty-discovery restart,
with no real credentials, Jobs, model calls or scientific work.
See the [recovery entrypoint](../nmrnet_provider/recovery.py) and
[qualification record, R38–R39](../notes/003_nmr_api_provider_20261018.txt) for source and evidence.

## Retained history and a full store

`lifecycle.maximum_records` bounds **active storage records**. Before fresh
admission, the service archives confirmed completions and failures after their
controller has returned and released its claim. Clean startup does the same for
an entirely confirmed inventory. If any active record is unresolved, the service
archives none of its neighbors and holds fresh work. Stop prevents new archive
submissions; an archive already in progress keeps its original I/O owner until
settled. Shutdown does not start another archive.

Each archive preserves the record's exact original requests, receipts and
protected scientific evidence under `store_root/history-<scope digest>/` before
releasing its active slot. Files remain mode0600 inside mode0700 directories.
The [core storage contract](../packages/provider-core/README.md)
describes filename lookup and interrupted-copy handling. Recovery inspection
lists active obligations; archived confirmations remain available as portable
files and cannot be used as fresh Attempt keys. No historical evidence expires
or is deleted automatically.

History still consumes disk. The provider rechecks space for full outstanding
records, a temporary full-record write and `filesystem_reserve_bytes`, then the
complete provider-wide API admission view. These checks reserve no physical
blocks. If active obligations or actual disk headroom prevent admission, stop
and settle the provider and inspect the retained evidence. Keep the existing
`store_root`, API/provider identity, `consumer_id` and `generation_id`; do not
discard records or change their scope to make room. Raising a record limit does
not settle an obligation, retire a worker or bypass API admission. Separate host
native-request limits also apply.
The owning [capacity check](../packages/provider-core/src/provider_core/attempt_files.py)
and [service admission](../nmrnet_provider/admission.py) enforce this behavior.

## Checking API delivery

Each opt-in target creates one **persistent Job** on the selected API. Pasted
targets use fixed supplied chloromethane and compare against an existing pinned
scientific receipt; generated-SMILES targets check received generation
provenance and H/C coverage without an independent shift-value oracle. These
targets are excluded from offline test aggregates:

- `make provider/qualify/api/pasted` uses the isolated scripted model peer.
- `make provider/qualify/api/real-pasted` uses one explicitly selected real endpoint.
- `make provider/qualify/api/real-smiles` requests one generated `CCl` conformer
  through one explicitly selected real endpoint.
- `make provider/qualify/api/real-smiles-multiple` requests three generated `CCCC`
  conformers and checks each geometry's provenance and H/C coverage.
- `make provider/qualify/api/real-upload` attaches an XYZ file and compares its
  supplied atoms and predictions with the pinned scientific fixtures.

Command-line selectors for these targets are:

- `API_ORIGIN`, `TOPOLOGY`, `PROJECT_REF` and `USER_ID`: the operator's chosen API,
  expected topology and entitled client/project. Provider identity and credential
  profile derive from the admitted provider artifact.
- `PROVIDER_CREDENTIAL`, `CLIENT_CREDENTIAL_DIRECTORY` and `CA_FILE`: absolute paths
  to the issued provider key, the client's original registration/private pair and
  the API trust certificate. Keys must remain caller-owned and mode 0400/0600.
- `ADDRESS` and `INTERFACE`: the observed publication's literal IPv4 destination
  and explicit host interface. The configured origin/port stays unchanged for TLS
  and signatures. No DNS or direct fallback is attempted.
- `API_CLIENT_IMAGE`: an immutable local API runtime image ID containing the
  API-owned credential validator/signing example. No implicit pull or installation.
- `RETAINED_RESULT`: an absolute path to the existing scientific comparison
  receipt; its expected hash remains pinned in the fixture client. It is never
  regenerated from this run's predictions.
- `OUTPUT`: a fresh absolute private directory outside the checkout, separate
  from all credential/trust inputs. It retains configuration, durable Attempt
  state and protected receipts; inspect them before any new mutation after failure.

The scripted target additionally requires `LOCAL_NETWORK` and `LOCAL_INTERFACE`
for its peer's observed internal network. The real targets instead require:

- `INTERPRETER_CONFIG`: an absolute path to a private TOML fragment containing
  only `[interpreter]` and exactly one `[[interpreter.endpoints]]` entry. Its key
  must be in the separate `key_file`; optional `ca_file` supplies model trust roots.
- `EXTERNAL_INTERFACE` and `RESOLVERS`: the explicit model-egress interface and
  space-separated literal IPv4 DNS resolvers. These remain separate from the
  API publication's `ADDRESS`/`INTERFACE`.

Each real target first inspects fragment metadata without a key grant. It then
grants only the referenced individual key/optional-CA files to offline staging,
uses the existing configuration admission, and freezes their exact bytes under
the private output. It selects no fallback endpoint and starts no scripted peer.
The existing foreground host and native launchers own execution and retirement;
no API signing credential or provider configuration is sent to the model.

The maintained profiles use these finite qualification limits, in seconds:

| Profile | Result-readback budget | Whole client process | Host observation of client |
| --- | ---: | ---: | ---: |
| Scripted | 180 | 240 | 260 |
| Real pasted or SMILES | 900 | 960 | 980 |
| Real Upload | 900 | 1020 | 1040 |

Real staging sets the preparation budget to 240 seconds. Native prediction
prepare, execute and channel closure each have their own 90-second policy; host
retirement and API publication are separate operations. A deadline does not
prove unsettled work retired: the original owners remain held until release is
confirmed. See `tools/provider_api_qualification.sh` and
`tests/integration/provider_runtime/{stage_api_config,api_prediction}.py`.

Job creation and subsequent mutations are sent once. Failure or uncertainty
retains the original requests and receipts; it does not automatically resend a
mutation or failed model request. Each profile defines its expected interpreter
operations, scientific comparisons
and confirmed-cleanup requirements in
[the API fixture client](../tests/integration/provider_runtime/api_prediction.py).
A result can therefore have been accepted by the
API even when the target exits nonzero and writes no `client/passed.json`.

## Diagnostics and shutdown

Diagnostics require a fresh absolute directory, mode `0700`, owned by the
container user. The process publishes `admitted` after startup admission/Hello,
and sends causal error reports to stderr/Docker logs without a file fallback.
Stopped recovery separately publishes
its functional inspect/replay `result.json`. SIGINT/TERM stops new work and requests bounded settlement.
If an original borrower remains active, the process reports
`requires_host_retirement = true`, keeps its original owners alive and waits for
the host to retire it; a normal stop signal does not cancel the same work again.
An unadmitted diagnostics directory receives no files; safe reasons or OS errno
still reach Docker's stderr logging. Neither diagnostics nor the
startup marker establish successful science. Run the focused subprocess proof
with `make test/integration/provider-core/process`.

Execution failures include the acknowledged Job and Attempt references in the
protected report. Native setup can fail before an Attempt exists; when a Job was
already selected, that setup failure reports its Job reference alone. Source-only
setup without Job context leaves those references absent. These fields identify
affected work; they do not establish dispatch or acceptance of a result.

## Images and network isolation

Build and check the installed controller offline:

```sh
make test/unit/provider/image
make provider/image/build
make test/integration/provider-runtime
```

The build uses committed input bytes, the retained core runtime wheelhouse and
the locked setuptools wheel. It first requires the pinned Python base locally;
it never downloads a missing base. The resulting image contains a wheel-installed
provider-core and image-owned binding modules in an isolated, pip-free environment.
Startup requires no source or wheel mounts and installs nothing. The recorded
immutable image ID and committed-input label are checked before image tests.
`/opt/provider/dependency-inputs.json` retains the frozen dependency inventories;
the source/lock label alone does not identify those platform-wheel bytes.
Installed-image checks cover the full dependency set, native signing, import
origins and isolated CLI startup. They contact no API or model and perform no
prediction.

The service network components have focused offline checks:

```sh
make test/unit/provider/proxy
make test/integration/provider/proxy
```

The controller's HTTPS transport can use a fixed Unix CONNECT socket. The service
proxy admits only explicitly granted DNS-name/IPv4 authorities before resolution
or connection. Local routing requires a complete frozen host-to-IPv4 mapping;
external routing uses the existing explicit interface binding. Both preserve
destination TLS. IPv6 literal destinations are refused. The proxy
receives no API/model credentials. These checks cover policy, private listener
startup, diagnostics and actual loopback TLS. The host launcher must still bind
those components to the intended networks and confirm exact container retirement;
these component tests establish neither deployment nor external model access.

## Attempt and service ownership

`nmrnet_provider.runtime.assemble_attempt` connects these settings to the
existing input interpreter, source acquisition, NMRNet session and core controller.
The caller selects a Job from signed `ProviderAPI.jobs`, opens the scoped store
and supplies the prediction allocation and reader/generator factories. Assembly
refuses outstanding retained obligations before constructing source/model owners;
recovery stays explicit. The returned controller must run the same pending start.
Successful return transfers the supplied allocation to that controller, including
cleanup if it is never run. Refusal leaves allocation cleanup with the caller.
The caller retains lifecycle API/store ownership and must settle any interrupted
inventory read before reuse. All HTTP roles have separate transport owners.

`make test/integration/provider-core/runtime` checks signed local discovery through
all three interpreted routes, durable publication and ownership failures, with
recording scientific ports. It verifies that changed Job input cannot reach the
interpreter or native dispatch, and that unconfirmed host retirement blocks
publication. These are local wiring tests. Direct coordinate-JSON admission
retains its separate existing session binding.

`nmrnet_provider.service.ProviderService` coordinates one Attempt at a time:
retained-work/capacity and live-inventory checks precede Hello, fresh discovery
and allocation. Explicit confirmed-history archival precedes capacity checks,
outside API retry loops. It refreshes the current announcement through independent I/O,
drains or cancels original work on stop, and retains unsettled tasks/controllers.
`make test/integration/provider-core/service` checks this ordering with local TLS
and recording workers created inside the allocation factory. Startup acceptance
does not establish scientific readiness.

Startup admission and idle discovery automatically retry reviewed GET failures
after the original API I/O retires. Each retry repeats complete admission and
discovery from the first page, before allocating work. Initial and periodic Hello
announcements also recover from reviewed availability failures: each send rebuilds
the latest complete announcement, including changed or withdrawn offerings. The
first accepted Hello still gates discovery and allocation; a periodic Hello outage
does not cancel an active prediction. The initial delay is
`service.idle_seconds`; it doubles after each failed cycle, capped by the required
`service.api_backoff_max_seconds`, which must be at least `idle_seconds`. Each
actual wait is randomly selected between half and all of that delay. A complete
successful read cycle or accepted Hello resets its own backoff. Requests keep their
configured deadlines; an outage may wait until recovery or stop, and stop interrupts
the backoff wait. A startup
request already in flight still has its exchange/retirement deadlines.

This applies to safe reads, including selected proxy maintenance responses, without
treating an unverified error body as API authority. Credentials, explicit wrong
environment, malformed success, storage/ownership errors and admission holds still
stop. Active Attempt observations also recover within the required finite
`lifecycle.observation_gap_seconds` window. Each fresh-observation wait includes
exchange-lock acquisition, reads, transport retirement and backoff. Its initial
delay is the smaller of `lifecycle.poll_seconds` and `service.api_backoff_max_seconds`,
then doubles with the same half-to-full jitter and cap, clipped by remaining time.
The original interpretation or prediction may continue during that window; another
phase or an accepted result still requires a fresh allowed observation. A reply
that arrives after the window cannot grant that permission.

Window expiry starts scientific retirement independently of HTTP cleanup. The
original read remains owned until it settles; neither elapsed time nor requested
cancellation proves that work stopped. Expiry, observed server stop and other
active Attempt failures still stop the service;
preserve retained records, settle the original owners and use the stopped-store
recovery procedure before restart. No allocation, Attempt
mutation or science is retried by the read loop. A Hello receipt confirms the new
announcement, without resolving whether an earlier send was accepted. Between
settled Jobs, confirmed records move to private scope-bound history with their
original bytes preserved. Unresolved records stay active and block fresh work.

The foreground process emits bounded, redacted `api_read_retry` and
`api_read_recovered` notices to stderr/Docker logs; Hello uses `api_hello_retry` and
`api_hello_recovered`. Their `latest_hello_send_confirmed` flag describes the last
sent snapshot's receipt; it does not attest to later supplier or directory state.
Their failure evidence describes the preceding failed send. Shutdown reports
unconfirmed Hello acceptance through `provider_shutdown_observation` log records,
including stop during initial backoff or a later interrupted send. Pipe notices
fit the atomic-write limit; an oversized notice never leaves partial JSON.
Notice writes and final error reports do not wait for pipe capacity. The latest
failed notice and its output cause remain in memory for shutdown reporting; there
is no file fallback. A notice-output failure alone does not terminate the service
or change science's outcome. A failed final error write retains original, cleanup
and output causes in the owned exception; unresolved work stays held.

Active-read recovery uses `api_observation_retry` and `api_observation_recovered`,
with the acknowledged Job and Attempt references. Recovery confirms a fresh API
observation, not completion or publication; observed server stop still wins.
If reporting fails, these notices use the same bounded in-memory diagnostic owner.
This observation recovery does not retry mutations. Terminal publication has its
own required finite `lifecycle.terminal_retry_window_seconds`: after science has
retired, the controller may replay the same durably retained COMPLETE/FAIL bytes
for reviewed availability failures, HTTP 500, or a protocol failure before any
verified response head. Each send is freshly signed. Explicit refusals, wrong
environment, malformed received receipts, cancellation, failed cleanup and
receipt-storage failures do not trigger replay. The same publisher confirms
original I/O retirement and rechecks durable command identity before another send.

The terminal retry window starts at the first send failure and includes retirement,
diagnostic callbacks and capped jittered waits. No new publisher invocation begins
after expiry; one already admitted may finish its existing bounded I/O later.
Expiry retains the pending command and protected Job/Attempt/send evidence for
stopped recovery. `api_terminal_retry` and `api_terminal_recovered` use the same
protected notices and failed-output fallback; recovery means the matching terminal
receipt was durably retained. These retries never repeat interpretation, geometry
generation or prediction. Unreviewed or exhausted condition-report recovery,
unreviewed phase-delivery failures and operational deadlines still stop the
service; preserve retained records, settle the original owners and follow
the stopped-store recovery procedure before restart. The exact replay authority is the retained
[API contract fixture](../packages/provider-core/tests/fixtures/README.txt) and
its terminal POST `x-nmr-recovery`/HTTP 500 descriptions, identified in that manifest.

For the fixed `preparing` and `running` labels, a reviewed delivery failure can
leave progress unconfirmed while this Job proceeds. The service first confirms
original API exchange retirement, emits the protected `api_progress_unconfirmed`
notice, and obtains a fresh allowed Attempt/Job observation before dependent work.
The notice includes the phase, `progress_delivery_confirmed=false`, real Job and
Attempt references, and safe cause/correlation facts. It does not mean work has
continued yet. That read does not confirm progress delivery; no failed label is
resent. Authentication, wrong environment, conflicts, malformed received receipts,
cancellation and unconfirmed cleanup still stop this path. Session condition
reports keep their acknowledgement requirement. Failed notice output uses the
existing protected final-report fallback. No extra deadline or retry setting is
needed: the fresh read uses `lifecycle.observation_gap_seconds` and the configured
API I/O bounds. The pinned progress PUT's `x-nmr-recovery` describes why stale
progress must not be replayed; see the [contract fixture manifest](../packages/provider-core/tests/fixtures/README.txt).

An awaited condition update can retry a reviewed temporary delivery failure while
that same sequential report is still current. It settles the original exchange
before retrying the same body with fresh signing, and dependent work waits for
the matching receipt. The retry-admission window starts at the first failed send
and uses `lifecycle.observation_gap_seconds`, including retirement, notices,
lock waits and backoff. An admitted exchange still owns its I/O until settled.
The existing lifecycle monitor can stop work during backoff. Protected
`api_condition_retry` and `api_condition_recovered` notices identify the phase,
condition and Attempt; only the latter reports `condition_receipt_received=true`.
No model call or science is retried. Authentication, conflicting state, malformed
receipts, cancellation and unsettled cleanup remain operational failures.
Configure only one progress writer for an Attempt: local store/claim locks do not
fence an independently deployed provider. Source:
[`ConditionRecovery` and `AttemptExecutor`](../packages/provider-core/src/provider_core/execution.py),
[`can_retry_condition`](../packages/provider-core/src/provider_core/api.py), and
the same pinned progress PUT recovery contract above.

Native prediction can recover once within the same Attempt. After an admitted
prediction fails or its response is lost, the provider confirms exact old-worker
retirement, local channel closure and host capacity handoff before allocating a
replacement. The replacement prepares the same ordered geometry bytes with a new
worker-local token and must report the original runtime. Existing result checks
still verify the complete batch and selected artifacts.

The protected `prediction_retry_started` notice retains the first failure and
actual Job/Attempt references, including when recovery succeeds. It announces
replacement, not an OOM diagnosis or a completed prediction. Failed notice output
uses the existing private shutdown fallback. Recovery does not rerun interpretation,
coordinate reading/generation or API lifecycle operations. There are at most two
native executions; input/protocol rejection, caller cancellation, foreign timeouts
and result assembly do not permit replacement. Exhaustion, failed allocation or
unconfirmed cleanup remain operational failures with both causes retained for
stopped recovery. Resource limits and model selection remain fixed. The policy
is implemented by [`NMRNetSession`](../nmrnet_provider/session.py), using the
existing [`NativeAttemptFactory`](../nmrnet_provider/native_factory.py) and
[`WorkerAllocation`](../nmrnet_provider/allocation.py) retirement owners.

The opt-in offline target
`make test/integration/provider/prediction-recovery` forces a real kernel OOM in
a synthetic 64 MiB worker, then checks unchanged requests, one replacement and
both retirement/archive handoffs. It uses the recorded provider image and loads
no released models. This establishes allocation/session recovery, not model
memory capacity, GPU behavior or numerical equivalence. Precise source/image and
retained evidence hashes are in [note 017](../notes/017_provider_core_plan_20261018.txt).

The Job's own `preparation.seconds` limit is different. Once its original model,
source and native resources have retired, expiry produces the public failure
`input_selection_time_limit`. Protected evidence retains the configured limit,
received model response facts and any uncertainty about a cancelled model POST.
Only a durably confirmed FAIL permits the service to accept a later Job. A foreign
timeout, independently observed operation failure or unconfirmed cleanup remains
operational and stops the service. In particular, an Upload session's cumulative
deadline is not reclassified as this Job limit. No model request or scientific
operation is repeated to recover from selection expiry.

START has a separate required `lifecycle.start_retry_window_seconds`. After a
reviewed availability failure, HTTP 500 or unverified pre-head protocol response,
the same starter can resend the exact retained Job/key/body with fresh signing.
It first settles the original exchange and checks the API-owned replay permission.
The retry-admission window includes retirement, diagnostic callbacks and capped
jittered waits; an admitted bounded exchange may finish after the window ends.
Authentication/topology faults, malformed success, cancellation, cleanup failure
and an acknowledgement that could not be saved stop automatic START recovery.
Expiry preserves the original request and protected send evidence for stopped
recovery. A terminal replay admission retires the session without starting science.

Protected `api_start_retry` notices identify only the known Job. Once a verified
acknowledgement is durable, `api_start_recovered` also identifies its actual
Attempt. Both use the existing bounded failed-output fallback. The focused signed
local TLS service witness loses its first accepted START reply, returns the same
admission on exact replay, and performs one interpretation and prediction. Its
scientific/model peers are authored fixtures; this is local lifecycle evidence.
START replay authority is the same pinned API fixture's START POST `x-nmr-recovery`
and HTTP 500 description, not a blanket permission to repeat mutations.

`preparation.max_worker_calls` limits each Job's attempts to read or generate
molecular coordinates. Once exhausted, no additional source grant or worker
allocation begins. That Attempt receives `input_worker_call_limit` with the limit
and safe next steps; protected evidence retains actual call counts and prior
interpreter diagnostics. After original resources retire and its FAIL receipt is
durable, the service can accept the next Job. The limit is separate from an invalid
input diagnosis. Cleanup, storage or API failure still blocks fresh work.

The focused service lane also verifies the actual signed failure request: a
model-reported input problem reaches the local API recipient with unverified
attribution and owned guidance for unclear units, conformer count or Upload record
selection. Its freeform explanation stays in protected evidence. An operational
science error keeps its original cause and sends neither a failure nor a completion.
This does not qualify GUI presentation or an operator diagnostic renderer.

The native service lanes use the production factory and worker entrypoints:

- `make test/integration/provider-service/uploads`: XYZ Upload records 2,1, with
  exact source/atom/coordinate correspondence and H-only versus H/C prediction.
- `make test/integration/provider-service/pasted`: the interpreter-supplied nm
  table, with its independent binary64 conversion bound in angstroms.
- `make test/integration/provider-service/smiles`: CCCC with three generated
  conformers, the fixed atom/hydrogen-parent oracle and exact downstream coordinates.
- `make test/integration/provider-service/references`: four original validation
  inputs through the Upload service, matching all 137 predictions and the original
  64 reference positions exactly, with source and request identities retained.
- `make test/integration/provider-service/capacity`: synthetic H128/C128 inputs
  through the same service, with all 256 atom-index/nucleus outputs and measured
  container CPU usage and peak memory. This tests capacity, not chemistry.

The host creates prediction and reader/generator containers only after signed
admission/discovery, enforces fixed offline profiles, and confirms exact-container
retirement and IPC release. Each lane executes science once and recovers a lost
completion acknowledgement by replaying retained bytes after reopening storage.
Its receipt also records periodic Hello exchanges.
Resource observations retain each native role/allocation after exact retirement,
plus the controller after replay and peer cleanup. Exported input fixtures also
retain their separate producer observation. These are per-container observations
through their sampling instants, not full-lifetime or simultaneous aggregate peaks.
API/store and interpreter peers are local scripted fixtures. These lanes verify
local service composition. Recorded API experiments and their exact evidence
identities are kept in [note 003](../notes/003_nmr_api_provider_20261018.txt).

`make test/unit/provider/allocation` checks request publication, cancellation,
late retirement, exact-CID ownership and exclusive host roots with a recording
engine. `make test/integration/provider-core/native-factory` checks successive
Attempt/source handoffs without retained factory references, original cancellation
causes and later settlement of held setup or channel closure. Host startup requires
a fresh installation root; retain prior allocation evidence for stopped inspection.
It does not erase evidence or retry uncertain creates.

`MAXIMUM_NATIVE_REQUESTS` bounds active native request leaves. After an adopted
channel closes, its original setup settles and exact host retirement is confirmed,
the client acknowledges consumption. The host preserves its original request and
worker diagnostic trees in `INSTALLATION_ROOT/native/history/<token>/`, keeps
receipts at `native/receipts/<token>/`, and confirms the archive before the client
releases that handoff. This renews the active slot without repeating native work.
Failed or unadopted factory setup leaves its evidence unconsumed for stopped
inspection. A partial transfer or uncertain receipt holds renewal and retains the
original owners; it does not authorize another create.

Native history grows with completed work. Its filesystem may differ from the
Attempt store's, and has no separate configured free-space reserve or quota.
Write/transfer failure stops renewal. The host's observed directory moves do not
establish power-loss durability; the fsynced Attempt records have their separate
storage contract. Native service qualification retains complete history alongside
its stable host receipts before removing qualification scratch.

### Check an existing result in the GUI

`provider/qualify/gui-result` opens a completed result using the deployed GUI's
normal user credential import and result viewer. It checks formatted JSON,
original text and the completed download against an earlier API result-read
response. It sends only the selected Job/result API GETs; it creates no Job and
runs no interpreter or inference. It currently requires GUI and API at the same
HTTPS origin, already configured in the GUI.

```sh
make provider/qualify/gui-result \
  GUI_ORIGIN=https://your-api.example API_ORIGIN=https://your-api.example \
  USER_CREDENTIAL=/absolute/private/user-signing.json \
  RESULT_RECEIPT=/absolute/evidence/result.response.json \
  TLS_CERTIFICATE=/absolute/trust/server-certificate.crt \
  ADDRESS=192.0.2.10 INTERFACE=eth0 \
  BROWSER_IMAGE=sha256:<installed-image-id> \
  OUTPUT=/absolute/private/fresh-gui-proof
```

All selectors must be literal Make command-line values. `USER_CREDENTIAL` must be
owned by the caller with mode0400 or0600. `RESULT_RECEIPT` is the original API
read-response envelope containing `canonical_result_base64`, length, fingerprint
and Job/result identities. An immutable locally installed browser image must
contain Node at `/usr/local/bin/node`, Playwright under `/work/node_modules`, and
matching browser binaries. The target never pulls images or installs packages.
The live browser uses the existing GUI envelope:2GiB memory with no extra swap,
512PIDs,512MiB temporary space and256MiB shared memory; its proxy keeps the normal
service limits. Both are nonroot, read-only and retired by exact container ID.
`make test/unit/provider/gui BROWSER_IMAGE=sha256:<installed-image-id>` exercises
the result-reading and submission guards offline without credentials.

The explicit mapped route preserves the original authority/port and binds the
proxy's outbound sockets to `INTERFACE`. The browser uses that cooperative proxy
on host networking; this is not an engine-enforced network allowlist. As in the
[GUI precedent](https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/tests/e2e/signed-job-lifecycle.test.mjs#L55-L63),
Chromium receives a certificate-error exception for the supplied certificate's
SPKI. Normally trusted certificates retain normal validation; this flag does not
enforce strict pinning of those certificates or qualify deployment TLS.

`output/passed.json` records the GUI-reported source commit (null if unavailable),
observed signed GET paths and retained result identity/hash. The completed
`output/downloaded-result.bin` must match the prior result byte for byte. Captured
proof sources, image identity and exact container retirement receipts accompany
them. Process streams remain in Docker logs; no HAR, trace or credential-storage
dump is written. A passing receiver check does not qualify submitting a Job in
the GUI or resolve the remaining real-model input/correction cases in note003.

Recorded readbacks passed for existing pasted coordinates, SMILES N1 and N3,
and an XYZ Upload. See [003 R44](../notes/003_nmr_api_provider_20261018.txt) for
source/image/result identities, failed attempts and exact proof limits. The dev
GUI reported no source commit; its running image was observed, but its source
revision was not attested. This result does not change the nonzero overall exits
of the earlier pasted/Upload producer experiments.

### Submit one fixture through the GUI

`provider/qualify/gui/submission` uses normal `/v1b` controls to create one closed
Job, select the configured provider and open that Job. The `sdf-zip` case first
publishes one SDF through the GUI and attaches its Upload. Opening the Job allows
the configured real interpreter and native prediction to run; this is an opt-in
persistent, potentially paid workflow, outside offline test aggregates.

| `GUI_CASE` | Input | `RETAINED_RESULT` |
|---|---|---|
| `pasted` | The existing supplied chloromethane table in nm | Original raw native scientific result |
| `smiles` | `CCl`, one generated conformer | Earlier API result-read envelope |
| `sdf-zip` | Two supplied four-carbon SDF records; no added H | Original raw R46 native scientific result |

The fixed reference fingerprints and original SDF identity are admitted by
[`gui_submission.mjs::admitReference`](../tests/integration/provider_runtime/gui_submission.mjs).
The SDF is an authored partial-coordinate fixture. Passing this case establishes
delivery and preservation of its supplied carbon predictions, not butane accuracy
or hydrogen completeness. See [003 R46–R50](../notes/003_nmr_api_provider_20261018.txt)
for exact retained artifact identities, source provenance and qualification limits.

```sh
make provider/qualify/gui/submission GUI_CASE=sdf-zip \
  GUI_ORIGIN=https://your-api.example API_ORIGIN=https://your-api.example \
  TOPOLOGY=your-topology PROJECT_REF=project:your-project \
  PROVIDER_CREDENTIAL=/absolute/private/provider-signing.json \
  USER_CREDENTIAL=/absolute/private/user-signing.json \
  CA_FILE=/absolute/trust/api-ca.crt \
  TLS_CERTIFICATE=/absolute/trust/server-certificate.crt \
  BROWSER_IMAGE=sha256:YOUR_INSTALLED_IMAGE_ID \
  INTERPRETER_CONFIG=/absolute/private/interpreter.toml \
  ADDRESS=192.0.2.10 INTERFACE=eth0 \
  EXTERNAL_INTERFACE=eth0 RESOLVERS='192.0.2.53' \
  RETAINED_RESULT=/absolute/evidence/sdf-native-result.json \
  OUTPUT=/absolute/private/fresh-gui-submission
```

Replace example values with the provisioned origin, trust, credentials, immutable
browser image and explicit routes. The browser uses the same cooperative host
proxy and resource grant described above. The model configuration and provider
key stay with their normal owners; the browser receives only the user credential,
public trust, fixed fixture, admitted nonsecret identity and retained science.
Sources are captured from a clean committed Git closure before dispatch.

The browser has a 1200-second whole-run limit and a 1220-second host wait. It
opens the Job only if setup finishes within 120 seconds, reserving the existing
900-second same-Job result-readback allowance. Transient read errors stay in that
loop; a visible failed Attempt reports its public guidance promptly. No mutation
or whole case is automatically retried. If an outcome is uncertain, inspect the
original Job/Upload and operation reference before any further action.

`client/submission-observations.json` records bounded operation references,
response facts and provisional/GUI-confirmed IDs. `client/downloaded-result.bin`
is the actual GUI download; `client/passed.json` is written after scientific and
receiving checks. Atom order, prepared coordinates, binary32 inputs and shifts
must match retained science; new Job/Attempt and outer ZIP identities are checked
separately. The final scientific source must join the actual GUI ZIP hash/length
to the unchanged selected SDF member. The normal reader owns ZIP decoding.
Process streams remain in bounded Docker logs. The host retires the browser
before its route, then drains the foreground service through its existing owner.

See [003 R50](../notes/003_nmr_api_provider_20261018.txt) for qualification evidence.
The separate `gui-result` lane remains read-only.

