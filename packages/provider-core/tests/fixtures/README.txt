Pinned public provider HTTP release, copied 2026-10-22
====================================================
nmr_api_v1 is the COMPLETE release: manifest +17 inventoried artifacts (16 schemas
and OpenAPI), preserved byte for byte. No signing private keys are present.

Release: sha256:719055db7751605c0e5325ca23bcbe66cde631376d4b2b99523c8e4db75f0d01
Manifest SHA-256:
dda8dc36db963f85ec9105567342dae4ba680476c7ab1114bd1e33acba65474b
OpenAPI SHA-256:
0d8939625504e5fd1706e9e9325a2abbc9684087678e6c2b8daca5cb36fa46cc
Declared source revision:12a53e720fc87e1a4144c76203f8853d5afaf753.

Copied from numpde/nmrpeak-repro1697dc84dd1d76ae25b04efc8183fd80614000e5,
contracts/upstream/nmr_api_v1. It matches the API's archived release byte for byte:
https://github.com/numpde/nmrpeak-repro/tree/1697dc84dd1d76ae25b04efc8183fd80614000e5/contracts/upstream/nmr_api_v1
https://github.com/numpde/nmr-api-v1/tree/daec9d978dacfd11b85ef96496dfd2e977e8142a/nmr_api/provider/contract_releases/v1/719055db7751605c0e5325ca23bcbe66cde631376d4b2b99523c8e4db75f0d01
Archive commit daec9d978dacfd11b85ef96496dfd2e977e8142a records this existing
source provenance. Content identity is not publisher authentication; source_revision
is excluded from release_ref's digest material and is not cryptographically attested.
Do not claim these historical snapshots are a live server's supported baseline.
The API's current contract_releases/checks.py explicitly selects no supported
historical baseline (inspected at e7790cf6a922c39261fe736d5a23d17a9a5ea322).

Tests adapt NMRPeak's tests/unit/test_provider_signing.py and the artifact/hash
rules in nmrpeak_provider/provider_http_contract.py at the pinned peer revision.
The complete bundle keeps signing vectors, schemas, route facts and their original
inventory together. It is test data only, excluded from the runtime wheel. Contract
tests check specific consumed rules, not arbitrary JSON Schema or every OpenAPI rule.
No sibling source is imported at runtime; no dependency or schema engine is added.
