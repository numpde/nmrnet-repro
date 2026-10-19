# Geometry preparation dependencies

`preparation.in` declares a separate RDKit candidate for SMILES embedding and
structure-file reading. It does not change `inference.in`, its lock, or the
qualified prediction image. The existing package owner stages and applies this
closure through `make packages/preparation/lock/stage WLAN_INTERFACE=YOUR_INTERFACE`
and `make packages/preparation/lock/apply STAGE=lock.ID`. Review the complete
`preparation.lock` and `preparation-advisories.json` pair before applying it.
Application validates the selected input filenames and hashes as well as the lock
hash; an inference report cannot authorize preparation outputs.

The preparation lock was staged and reviewed on 2026-10-18; its three wheels were
downloaded and independently verified offline that day. No runtime was qualified
at acquisition time. Lock staging records
PyPI exact-version advisory metadata; that is not a complete native-code audit.
An applied lock alone does not authorize exposing every RDKit reader to uploads.

## Candidate and unresolved native-parser evidence

Sources inspected on 2026-10-18:

- [PyPI RDKit 2026.9.1](https://pypi.org/project/rdkit/2026.9.1/)
  and its [exact-version metadata](https://pypi.org/pypi/rdkit/2026.9.1/json)
  identify a CPython 3.12, manylinux_2_28 x86-64 wheel. Its published SHA-256 is
  `c700cb37954d84bb864d8e02a55cff8b0f5ee6daff6d79a53a1cfe12459fa8c0`.
  This is candidate artifact identity, not evidence of locally verified bytes.
- [RDKit release tag](https://github.com/rdkit/rdkit/releases/tag/Release_2026_09_1)
  identifies source revision `fece8ca`. The
  [wheel project's build description](https://github.com/kuelumbus/rdkit-pypi)
  explains its relationship to upstream RDKit. Version agreement does not prove
  binary-to-source identity for the PyPI wheel.
- [OSV-2022-675](https://osv.dev/vulnerability/OSV-2022-675), rendered record
  modified 2026-10-14T14:42:01Z, lists `Release_2026_09_1` as affected. Its Git
  range starts at `fbbddc6035d74f12edde15bf1a93fa70a3fe6fd1` and has no fixed
  event. The report describes an OSS-Fuzz heap out-of-bounds read through
  `ParseV3000AtomProps`, `ParseV3000AtomBlock` and `ParseV3000CTAB`.
  [The release's parser source](https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/FileParsers/MolFileParser.cpp)
  contains that call path. The report's reproducer was an instrumented i386 build;
  exact-wheel behavior on x86-64 has not been demonstrated here.
- The browser returned older snapshots from the
  [OSV JSON endpoint](https://api.osv.dev/v1/vulns/OSV-2022-675) and
  [OSS-Fuzz YAML](https://github.com/google/oss-fuzz-vulns/blob/main/vulns/rdkit/OSV-2022-675.yaml).
  Their shorter affected-release lists do not override the newer rendered record.
  Recheck the record when qualifying the artifact rather than treating omission
  from an older snapshot as evidence of a fix.
- [OSV-2022-1288](https://osv.dev/vulnerability/OSV-2022-1288) concerns a separate
  CX-SMILES parsing issue and records fixed commit
  `afc44b0192aea809f487af060ffa9b9e60839ee1`. It must not be confused with the open
  V3000 report.

The unresolved V3000 finding matters because uploads are untrusted native-parser
inputs. Qualification must establish a fix or assess the exact candidate in an
isolated worker before enabling that path. The evidence neither establishes that
every V3000 file fails nor proves this wheel safe. No reproducer has been fetched
or run. The repository's plain XYZ reader needs only element symbols and positions
and does not call this RDKit parser. SMILES generation and the remaining readers
still need their own bounded runtime tests and release admission.

## Resolved closure review, 2026-10-18

The applied receipt records RDKit 2026.9.1, NumPy 2.5.4 and Pillow 13.0.0, checked
at `2026-10-18T11:51:06.666100+00:00`. Its lock SHA-256 is
`ea5c3e669a64ee07f3d4c87a6b003eb9e511103b70891b47ecb3e3c0a4fd51ea`.
The three exact-version PyPI responses contain no advisory entries. Preserve that
scope: the RDKit native-parser record above is not negated by this receipt.

Additional primary-source checks:

- [NumPy's maintainer advisory index](https://github.com/numpy/numpy/security/advisories)
  lists no published advisories. This is an index observation, not a universal
  absence claim for NumPy 2.5.4.
- [Pillow 13.0.0 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)
  and its [maintainer advisory index](https://github.com/python-pillow/Pillow/security/advisories)
  describe security fixes announced around this release. The inspected
  [OpenJPEG](https://github.com/python-pillow/Pillow/security/advisories/GHSA-78g4-xqwv-q3g8),
  [GIF save](https://github.com/python-pillow/Pillow/security/advisories/GHSA-fc9g-5qg7-mf45),
  [GIF seek](https://github.com/python-pillow/Pillow/security/advisories/GHSA-65qm-jvmh-6pqg),
  [EPS scale](https://github.com/python-pillow/Pillow/security/advisories/GHSA-vx6w-vv9f-6gvg)
  and [PDF save](https://github.com/python-pillow/Pillow/security/advisories/GHSA-jg86-v3jv-hrpm)
  records identify 13.0.0 as patched. This review does not establish coverage of
  every image codec or bundled library in every platform wheel.

Keep the complete resolver closure; do not silently remove Pillow because the
planned SMILES worker does not use image parsing. The candidate may be acquired
for contained qualification. Native file readers and SMILES generation still need
their distinct behavioral tests; an acquired package is not a public reader grant.

Preparation wheels use `.work/preparation/wheelhouse`, acquired with
`make packages/preparation/wheelhouse WLAN_INTERFACE=YOUR_INTERFACE` and verified
offline with `make packages/preparation/wheelhouse/check`. The only writable host
mount during that acquisition is `.work/preparation`, keeping inference's existing
`.work/wheelhouse/cpu` outside its write authority. Preparation receipts explicitly
record `source_declaration_sha256: null`; Uni-Core archives are not allowed there.

The retained `.complete` receipt has SHA-256
`c3912a14671952fa40210ff59b847f8f381af465a8193b6712e12cc5d434a3a5`.
The verified CPython 3.12 Linux x86-64 wheel bytes are:

- NumPy 2.5.4, manylinux_2_27/manylinux_2_28:
  `fbde6962867ee75b48b0ee29b2b9372ec5d617799dbaf38e82dc0596f2f7738a`.
- Pillow 13.0.0, manylinux_2_27/manylinux_2_28:
  `42d63be3af4f9b951b199b0a9d23365d37ab14687e6d77b74940432c8515c4ca`.
- RDKit 2026.9.1, manylinux_2_28:
  `c700cb37954d84bb864d8e02a55cff8b0f5ee6daff6d79a53a1cfe12459fa8c0`.

These identify retained distribution bytes, not executed imports or a qualified
SMILES/file-reader worker. No package installation was performed during acquisition.

## Advisory recheck before runtime installation, 2026-10-19

Primary-source rechecks retained the candidate versions and the V3000 restriction.
The rendered [OSV-2022-675 record](https://osv.dev/vulnerability/OSV-2022-675)
still lists the selected RDKit release as affected without a fixed event;
exact-version PyPI metadata does not negate that native-parser report.
The [NumPy maintainer index](https://github.com/numpy/numpy/security/advisories)
still provides no published-advisory evidence, rather than a universal safety claim.

Pillow's maintainer index additionally lists
[XBM regular-expression denial of service](https://github.com/python-pillow/Pillow/security/advisories/GHSA-xgh8-jp6q-p5x8),
[GIF open CPU exhaustion](https://github.com/python-pillow/Pillow/security/advisories/GHSA-74r2-7ff9-mjj2)
and [zero-size rank-filter crash](https://github.com/python-pillow/Pillow/security/advisories/GHSA-27h8-gf7j-v5hc).
Each identifies 13.0.0 as patched. These references were consulted to check the
complete retained closure before offline installation; the runtime import check
does not parse image files or enable any untrusted molecular-reader route.

## Offline import evidence, 2026-10-19

`make test/integration/rdkit-runtime` owns a temporary, offline dependency check.
It copies the wheelhouse and lock before verification, installs the verified
copies with hashes enforced into a virtual environment without pip, and runs
isolated Python outside the checkout. The expected package list comes from the
existing lock parser. No separate version table or bootstrap-package exception
list is maintained. Native RDKit, NumPy and Pillow extension origins must lie
inside that environment.

Three integration checks passed with Python 3.12.12, linux-x86_64, in the explicitly
selected cached checker image
`sha256:9164d380c00bf0e013d44357cafb03d4b1f64570a93105f74ac4b85b66cf1b49`.
That was alternate-image import evidence at commit `806d7f9`; the maintained
target then could not start because its pinned Debian Python base was missing
locally. No fallback is implemented. Four focused launcher/guard checks, changed-file lint/format and
existing silent-default scanners also passed. The integration checks initially
failed before installation for missing native packages, a different distribution
set and use of the base interpreter.

This does not qualify the canonical worker image, a native molecular parser,
SMILES generation or any public Upload route. The V3000 restriction above remains.

### Canonical Debian base follow-up, 2026-10-19

After committing the explicit bootstrap target as `5062cf3`,
`make packages/base-images/python/download` returned zero and provisioned
`docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c`.
Local inspection reported image ID
`sha256:9cb180cb36bd45dc786105b834d3cb5f3b1d69a6a63c79ca10fc64f108aa649a`,
linux/amd64, and the same registry digest. This authorized bootstrap used Docker's
default routing, with no forced interface binding; README records that exception.

The maintained `make test/integration/rdkit-runtime` then returned zero in that
base: all three integration checks passed with Python 3.12.12, linux-x86_64, and
pip reported no broken requirements. Installation used only the retained,
hash-verified wheel closure in temporary container storage with no network.
This qualifies those locked imports in the canonical base. It does not build or
qualify the planned molecular-reader worker, exercise molecular files or SMILES,
or enable public Upload submission. V3000 remains blocked pending its own decision.
