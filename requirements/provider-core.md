# provider-core build dependencies

The first slice has no third-party runtime dependencies. Its contained offline
build uses the retained setuptools 84.0.0 wheel, checked against
provider-core-build.lock before installation. No wheel package is needed by this
modern backend. The base image's pip limitations remain those in README.md.

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

The async client follows Magnet's dependency families, but not its historical
cryptography pin. Exact-version resolution on 2026-10-18 reported
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
