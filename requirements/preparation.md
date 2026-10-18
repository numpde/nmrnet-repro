# Geometry preparation dependencies

`preparation.in` declares a separate RDKit candidate for SMILES embedding and
structure-file reading. It does not change `inference.in`, its lock, or the
qualified prediction image. The existing package owner stages and applies this
closure through `make packages/preparation/lock/stage WLAN_INTERFACE=YOUR_INTERFACE`
and `make packages/preparation/lock/apply STAGE=lock.ID`. Review the complete
`preparation.lock` and `preparation-advisories.json` pair before applying it.
Application validates the selected input filenames and hashes as well as the lock
hash; an inference report cannot authorize preparation outputs.

No preparation lock, wheelhouse or runtime is qualified yet. Lock staging records
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
