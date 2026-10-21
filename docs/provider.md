# Provider operation and configuration

This is the detailed operator reference. For scientific interpretation, start with the [scientific guide](scientific-method.md). The [README](../README.md) summarizes the supported calculation and its evidence.

## Provider startup configuration

The internal `nmrnet_provider.config.load_provider_config` loads one explicit
UTF-8 TOML file, up to 64 KiB, into the existing runtime policies. It reads the
configured credential, interpreter keys and TLS roots before any HTTP request,
worker allocation or journal acquisition. The foreground service has one actual
API prediction qualified with scripted interpretation; broader qualification
remains tracked in [note 003, R21 onward](../notes/003_nmr_api_provider_20261018.txt).
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

The contained foreground entrypoint is `python -m nmrnet_provider.process` with
required `--config`, `--execution`, `--models` and `--diagnostics` paths, plus
`--api-proxy-socket`, `--interpreter-proxy-socket` and `--store-proxy-socket`.
Every foreground HTTP role uses its explicit Unix route; omission or an invalid
socket path refuses startup before retained storage or network acquisition. Its
installed image is built and checked through the lanes below. The host launcher
has qualified one complete local API prediction with scripted interpretation;
live language-model interpretation and GUI qualification remain open. The execution and model declarations
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

`make provider/qualify/api/pasted` explicitly creates one **persistent Job** on
the selected API. It is excluded from offline test aggregates. It uses a fixed
chloromethane input and a scripted model reply, then compares the delivered result
with a pinned, already-retained scientific receipt. It does not qualify a real
interpreter or the GUI. Required command-line selectors are:

- `API_ORIGIN`, `TOPOLOGY`, `PROJECT_REF` and `USER_ID`: the operator's chosen API,
  expected topology and entitled client/project. Provider identity and credential
  profile derive from the admitted provider artifact.
- `PROVIDER_CREDENTIAL`, `CLIENT_CREDENTIAL_DIRECTORY` and `CA_FILE`: absolute paths
  to the issued provider key, the client's original registration/private pair and
  the API trust certificate. Keys must remain caller-owned and mode 0400/0600.
- `ADDRESS` and `INTERFACE`: the observed publication's literal IPv4 destination
  and explicit host interface. The configured origin/port stays unchanged for TLS
  and signatures. No DNS or direct fallback is attempted.
- `LOCAL_NETWORK` and `LOCAL_INTERFACE`: the named internal network and explicit
  interface for the isolated scripted model peer.
- `API_CLIENT_IMAGE`: an immutable local API runtime image ID containing the
  API-owned credential validator/signing example. No implicit pull or installation.
- `RETAINED_RESULT`: an absolute path to the existing scientific comparison
  receipt; its expected hash remains pinned in the fixture client. It is never
  regenerated from this run's predictions.
- `OUTPUT`: a fresh absolute private directory outside the checkout, separate
  from all credential/trust inputs. It retains configuration, durable Attempt
  state and protected receipts; inspect them before any new mutation after failure.

The target runs the existing foreground host owner and native worker launchers.
Its separate model, configuration and client containers have distinct grants; the
model sees no API key or provider configuration. Only its fixed scientific fixture
and expected receipt hash are pinned in code. Job creation and subsequent mutations
are sent once; uncertainty stops the qualification and retains the original bytes.

The qualified local run used one supplied chloromethane geometry, one interpreter
exchange, the released CPU float32 fold-0 H/C models, and actual authenticated API
submission/readback. Its ordered predictions exactly matched the previously retained
scientific receipt; the service and all ten containers retired successfully.
This establishes delivery and repeatability, with the limits and exact source/image
evidence recorded in [003, R21](../notes/003_nmr_api_provider_20261018.txt).

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
prediction. The host launcher and one actual API prediction have separate
qualification evidence in [note 003, R21](../notes/003_nmr_api_provider_20261018.txt).

The service network components have focused offline checks:

```sh
make test/unit/provider/proxy
make test/integration/provider/proxy
```

The controller's HTTPS transport can use a fixed Unix CONNECT socket. The service
proxy admits only explicitly granted DNS-name/IPv4 authorities before resolution
or connection. Local routing requires a complete frozen host-to-IPv4 mapping;
external routing uses the existing explicit interface binding. Both preserve
destination TLS. IPv6 literal destinations are currently refused. The proxy
receives no API/model credentials. These checks cover policy, private listener
startup, diagnostics and actual loopback TLS. The host launcher must still bind
those components to the intended networks and confirm exact container retirement;
these component tests establish neither deployment nor external model access.

`nmrnet_provider.runtime.assemble_attempt` now connects these settings to the
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
does not establish scientific readiness. This first coordinator stops on outages
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
API/store and interpreter peers are local scripted fixtures. This is a runnable
local service qualification with scripted peers. The later actual API transaction
is separately recorded in [note 003, R21](../notes/003_nmr_api_provider_20261018.txt);
live language-model interpretation and GUI qualification remain separate. The
earlier scientific-worker lanes retain their separate
prestarted-allocation proofs.

`make test/unit/provider/allocation` checks request publication, cancellation,
late retirement, exact-CID ownership and exclusive host roots with a recording
engine. `make test/integration/provider-core/native-factory` checks successive
Attempt/source handoffs without retained factory references, original cancellation
causes and later settlement of held setup or channel closure. The host refuses prior
allocation roots until stopped inspection/archive; it does not erase evidence or
retry uncertain creates. The actual API run has separate installation/network
grant observations. Outage, record-retention, live interpreter and GUI
qualification remain tracked work;
see [note 003](../notes/003_nmr_api_provider_20261018.txt).

