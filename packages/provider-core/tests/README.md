# Test organization

Unit normative tests state required input/identity behavior; adversarial tests
challenge substitutions, parser limits and privacy. Contract tests inspect the
installed wheel, forbidden dependencies, container guard and empty selections.
Repository tests/integration/provider_core consumes installed core through the
NMRNet preparation adapter; it is not shipped with reusable package tests.

Run make test/unit/provider-core (or /normative or /adversarial),
make test/contract/provider-core, and make test/integration/provider-core.
Every target builds/installs offline in separate temporary environments and runs
outside the source checkout. Only the integration lane exposes consumer sources.
These are synthetic protocol and local preparation tests, not live API/GUI proof.
Use make test/repository/provider-core for the focused guard, source inventory,
Make and cache conventions. It shares the maintained repository-check launcher.
