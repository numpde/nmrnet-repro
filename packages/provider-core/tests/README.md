# Test organization

Unit normative tests state required input/identity behavior; adversarial tests
challenge substitutions, parser limits and privacy. Contract tests inspect the
installed wheel, forbidden dependencies, container guard and empty selections.
Repository tests/integration/provider_core consumes installed core through the
NMRNet preparation adapter; it is not shipped with reusable package tests.

Run make test/unit/provider-core (or /normative or /adversarial),
make test/contract/provider-core, and make test/integration/provider-core.
Every target builds/installs offline in separate temporary environments and runs
outside the source checkout. Only the consumer integration lane exposes consumer
sources. `make test/integration/provider-core/chat` selects the package's
integration/chat/{normative,adversarial} tests, including actual loopback TLS,
without granting access to consumer sources. These tests also join aggregate
integration discovery. The contract suite checks the installed chat module's
independence from repository packages and its own container guard.
These are synthetic protocol and local preparation tests, not live API/GUI proof.
The separately selected make test/integration/provider-scientific-worker lane
connects installed core to the real NMRNet worker and exact-container retirement.
Its API peer remains a local wire-contract fixture; it is not deployed API proof.
Use make test/repository/provider-core for the focused guard, source inventory,
Make and cache conventions. It shares the maintained repository-check launcher.
