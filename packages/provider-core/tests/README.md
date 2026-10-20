# Test organization

Unit normative tests state required input/identity behavior; adversarial tests
challenge substitutions, parser limits and privacy. Contract tests inspect the
installed wheel, forbidden dependencies, container guard and empty selections.
Repository tests/integration/provider_core consumes installed core through the
NMRNet preparation adapter; it is not shipped with reusable package tests.

Run make test/unit/provider-core (or /normative or /adversarial),
make test/contract/provider-core, and make test/integration/provider-core.
Use make test/unit/provider-core/credential for API-issued artifact admission and
the shared signing-reference boundary, without running unrelated core tests.
`make test/unit/provider-core/admission` selects storage preflight and the affected
store tests. It checks unchanged records, confirmed-record saturation, filesystem
headroom and interrupted original I/O. This observes storage capacity; it does not
reserve disk blocks, retire records, allocate native work or prove model capacity.
`make test/unit/provider-core/hello` checks complete announcements, Unicode/byte
bounds, empty offerings and receipt/failure admission. The structured
integration/hello/{normative,adversarial} tests run through
`make test/integration/provider-core/hello`: signed local TLS using an admitted
API-shaped credential, lost acknowledgement, caller-chosen replacement and
cancellation/socket cleanup. This lane exposes no consumer sources and proves
neither a deployed directory nor scientific readiness.
`make test/unit/provider-core/transport` selects the maintained normative/adversarial
exchange tests. The structured integration/transport/adversarial TLS witness runs
through `make test/integration/provider-core/transport`, without consumer grants.
It checks received status/raw header evidence after truncated or stalled bodies;
failed I/O still yields no complete response. Controlled unit ports separately
prove evidence remains available during cancellation-resistant cleanup, the same
failure receives late observations, and foreign callers cannot release ownership.
`make test/unit/provider-core/api` selects response-environment/correlation and
affected start/progress/outcome/Upload/publication tests. Existing verified-refusal
and durable-hold assertions stay intact; their replies now carry complete API
metadata. `make test/integration/provider-core/api` runs structured API TLS cases
for foreign environment, malformed body, truncation and timeout. Successful signed
receipt behavior is also exercised by the Hello lane; configured consumer binding
is tested by the separate runtime lane. API fixtures provide environment headers;
model/store peers remain generic, and explicit adversarial headers are not repaired.
Every target builds/installs offline in separate temporary environments and runs
outside the source checkout. Only the consumer integration lane exposes consumer
sources. `make test/integration/provider-core/chat` selects the package's
integration/chat/{normative,adversarial} tests, including actual loopback TLS,
without granting access to consumer sources. These tests also join aggregate
integration discovery. Conversation tests cover shared budgets, correction,
fallback without repeating tool effects, private failure retention and retirement
races. `make test/integration/provider-core/interpreter` tests the NMRNet chooser
through local HTTPS and all three actual input/session/result paths, with recording
native/model ports. This does not qualify an external model or native science.
The contract suite checks the installed chat and conversation modules'
independence from repository packages and its own container guard.
`make test/integration/provider-core/config` selects the NMRNet startup tests:
closed TOML, API-shaped credentials, descriptor permissions and cleanup,
CA bytes used by the real HTTPS carrier, and refusal before network or worker work.
`make test/integration/provider-core/runtime` joins configured NMRNet assembly to
signed local discovery, model/source peers and recording scientific allocations;
it checks all input routes, retained-work refusal, cancellation and cleanup gates.
These are synthetic protocol and local preparation tests, not live API/GUI proof.
`make test/integration/provider-core/admission` checks the consumer's preflight,
complete-empty live inventory and fresh server-ordered selection. Local TLS plus
the existing assembly/controller proves selected source/key preservation, not
host allocation ordering. Recording scientific ports already exist in that fixture;
the separate service lane below checks that the coordinator calls this gate before
its allocation factory.
Cursor cycles, continuing pages at the bound, malformed responses, retained work
and cross-kind remote Attempts hold admission without creating a new start.
`make test/integration/provider-core/service` selects the structured NMRNet
coordinator tests. Unlike the assembly-only fixture, it creates the recording
scientific worker inside the trusted factory after service admission/discovery.
It exercises actual core/controllers and local signed TLS: current independent
Hello, stop/drain/cancellation, late factory adoption, real setup-store I/O,
held controller settlement, independent failures and exact lost-completion records.
These are local ordering/ownership proofs, not host engine provisioning, native
capacity or external/deployed delivery. The first coordinator stops on outages
and retains all records; automatic recovery and retention policy are separate.
The service lane also observes an attributed model-input report at the signed TLS
failure recipient, after durable retention and complete recording-worker retirement.
It checks full protected evidence and the operational-error path's original cause,
execution-entered record and absence of a terminal submission. This does not prove
native fault handling, GUI rendering or operator diagnostic delivery.
`make test/integration/provider-core/native-factory` checks partial native setup,
original setup-read settlement, cancellation plus independent retirement failure,
and bounded observation of the original channel close. The fixed native
`make test/integration/provider-service/{uploads,pasted,smiles}` lanes run the actual service with
post-admission host allocation and production worker entrypoints, released H/C,
unchanged route-local molecular/source oracles, host retirement and exact terminal replay.
Its API/store/model peers remain local scripted fixtures; external/deployed
qualification and capacity remain separate.
`make test/integration/provider-core/lifecycle` selects the shared native-fixture
setup, cleanup, source-manifest and cancellation fault tests. Native cases skip
there; the separately selected scientific-worker/{smiles,pasted,uploads} targets
each run one actual native route through that same lifecycle, configured runtime
assembly, signed local API and exact durable replay. Molecular expectations stay
in their route-specific test modules.
The separately selected make test/integration/provider-scientific-worker lane
connects installed core to the real NMRNet worker and exact-container retirement.
Its API peer remains a local wire-contract fixture; it is not deployed API proof.
Use make test/repository/provider-core for the focused guard, source inventory,
Make and cache conventions. It shares the maintained repository-check launcher.
