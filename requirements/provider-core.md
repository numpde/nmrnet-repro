# provider-core requirements

The package requires Python >=3.12; the maintained contained runtime uses Python
3.12.12 on Linux x86-64. Its default installation includes all four direct runtime
dependencies in [the package metadata](../packages/provider-core/pyproject.toml):
`cryptography==50.0.0`, `http-sf==1.2.1`, `httpx==0.28.1` and `httpcore==1.0.9`. The
[runtime lock](provider-core.lock) pins their complete dependency closure and
allowed hashes. The [build lock](provider-core-build.lock) pins `setuptools==84.0.0`.
There is no reduced-dependency installation mode.

The launcher must establish actual container isolation and set
`PROVIDER_CORE_IN_CONTAINER=1`; that marker alone does not establish isolation.
Lifecycle, storage and shared claims require one asyncio event loop. See the
[package requirements](../packages/provider-core/README.md) for the runtime
contract; the dated review below records the dependency-selection rationale.

The initial Job-input-only slice had no third-party runtime dependencies. Its contained offline
build uses the retained setuptools 84.0.0 wheel, checked against
provider-core-build.lock before installation. No wheel package is needed by this
modern backend. The base image's pip limitations remain those in README.md.

The 2026-10-20 controller image also verifies this retained build wheel with the
existing wheelhouse reader. Its lock uses that reader's established multiline
name/version and hash format; the setuptools version and allowed SHA-256 are
unchanged. This is a formatting correction, not dependency re-resolution.

Reviewed 2026-10-18:
- https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf
  concerns PackageIndex download path traversal; 84.0.0 is beyond the affected range.
- https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c
  lists <=82.0.1 and no patched version. This review does not infer a universal
  fix from that range: the lane builds an allowlisted pure-Python wheel on Linux,
  not an sdist using Unicode MANIFEST exclusions on macOS.
- https://setuptools.pypa.io/en/latest/history.html (84.0.0 at review)
  establishes the selected backend version; native compiler regressions do not
  exercise this pure-Python build.

These are scoped advisory observations, not a claim of complete security.

## Runtime dependency selection

The async client uses Ed25519 signing. Exact-version resolution on 2026-10-18 reported
GHSA-g6cj-pr64-35w5 (also PYSEC-2026-3552) for 49.0.0. The maintainer advisory
https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5
identifies PKCS#7 RSA decryption and names 50.0.0 as the patched release. Our
planned operation is Ed25519 signing, not PKCS#7 decryption; nevertheless this new
package selects 50.0.0 instead of carrying an unnecessary affected dependency.
The replacement closure's exact-version PyPI report is retained in
provider-core-advisories.json, with the input and lock hashes. All 11 resolved
versions returned empty advisory lists on 2026-10-18. This is a bounded metadata
review, not proof that the dependencies have no vulnerabilities. Wheels are
acquired separately and checked against provider-core.lock; the installed test
lane exercises native Ed25519 signing before transport qualification.

The transport now directly declares httpcore 1.0.9 in the package metadata to use
its public network-backend interface. That exact version is already in the
reviewed hash-locked closure through HTTPX 0.28.1; no dependency version, wheel,
acquisition input or advisory receipt changed for the socket-ownership fix.
