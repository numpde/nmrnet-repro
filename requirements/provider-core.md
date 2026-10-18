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
