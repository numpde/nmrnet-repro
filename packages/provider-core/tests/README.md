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
`make test/unit/provider-core/hello` checks complete announcements, Unicode/byte
bounds, empty offerings and receipt/failure admission. The structured
integration/hello/{normative,adversarial} tests run through
`make test/integration/provider-core/hello`: signed local TLS using an admitted
API-shaped credential, lost acknowledgement, caller-chosen replacement and
cancellation/socket cleanup. This lane exposes no consumer sources and proves
neither a deployed directory nor scientific readiness.
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
