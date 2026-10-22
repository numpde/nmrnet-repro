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
| `lifecycle` | `consumer_id`, `generation_id`, `store_root`, `maximum_records`, `filesystem_reserve_bytes`, `store_seconds`, `allowed_job_states`, `poll_seconds`, `task_settle_seconds` | none |
| `service` | `display_name`, `description`, `offering_description`, `page_size`, `maximum_pages`, `idle_seconds`, `hello_seconds`, `drain_seconds`, `settle_seconds`, `allocation_seconds`, `prediction_seconds`, `source_seconds` | none |

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

`lifecycle.maximum_records` limits **all retained Attempts**, including confirmed
completions and failures. It is not a limit on simultaneous calculations. The
provider keeps their exact requests, receipts and protected scientific evidence;
it does not automatically expire or delete them. At the limit, it stops admitting
new Jobs before allocating another worker. Confirmed history is preserved.

To resume after this limit is reached, stop and settle the provider, confirm its
host and workers have retired and the store lock is released, then inspect the
store using the recovery command above. Provide enough filesystem space and
increase `lifecycle.maximum_records` in the TOML. Keep every retained file and
the existing `store_root`, API/provider identity, `consumer_id` and `generation_id`.
Start normally with a fresh `INSTALLATION_ROOT` so the launcher captures the updated
configuration. Do not delete or move records, or change their scope, to make room.

Startup rechecks record slots, space for full future records and a staged write,
and any unresolved local or API obligations. Increasing the record limit does
not resolve those obligations, bypass API admission or reserve physical disk
space. This is bounded keep-all retention, with an explicit stop when capacity
is exhausted.
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
`failure.json` for safe private causal evidence and `result.json` only after
dependencies retire. SIGINT/TERM stops new work and requests bounded settlement.
If an original borrower remains active, the process reports
`requires_host_retirement = true`, keeps its original owners alive and waits for
the host to retire it; a normal stop signal does not cancel the same work again.
An unadmitted diagnostics directory receives no files; safe reasons or OS errno
still reach the host's private stderr capture. Neither diagnostics nor the
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
and allocation. It refreshes the current announcement through independent I/O,
drains or cancels original work on stop, and retains unsettled tasks/controllers.
`make test/integration/provider-core/service` checks this ordering with local TLS
and recording workers created inside the allocation factory. Startup acceptance
does not establish scientific readiness. The coordinator stops on outages
or admission holds; confirmed records still consume storage slots.
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

