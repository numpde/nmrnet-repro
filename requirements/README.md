# Dependency sources and decisions

Geometry preparation has a [separate candidate and qualification record](preparation.md).
Its lock is resolved through `make packages/preparation/lock/stage`; it does not
change the qualified inference closure described below.

Dependency references checked on 2026-10-13; bounded inference qualified on
2026-10-14. The environment is Python 3.12.12, Linux x86-64 and CPU float32.
The real image builds, passes dependency/import checks, and loads both selected
fold-0 models for independently verified repeated H/C inference. Its identity,
scope and limitations are recorded in
[note 001](../notes/001_contained_upstream_baseline_20261013.txt).

[NMRNet++ requirements at the pinned source revision](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/requirements.txt)
provide the starting dependency declarations. [inference.in](inference.in)
selects the dependencies used by the inference path and gives them local exact
pins. These are environment choices, not a claim to reproduce the upstream
CUDA environment. Plotting and RDKit preprocessing are outside this prepared-data
baseline. The upstream entrypoint and task imports are in
[uninmr/infer.py](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/infer.py)
and [uninmr/tasks/uninmr.py](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py).

[Uni-Core setup.py at ace6fae1c8479a9751f2bb1e1d6e4047427bc134](https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/setup.py)
owns its `install_requires` declarations and imports Torch during setup.
[unicore-source.json](unicore-source.json) identifies the exact source archive,
commit, size and SHA-256. [build.in](build.in) supplies locally pinned build
tools; the offline build installs Torch before building Uni-Core with CUDA
extensions disabled. Uni-Core is built from this declared source, rather than
resolved by its package name.

Torch 2.13.0+cpu was selected after advisory review. The
[GitHub-reviewed CVE-2025-3000 advisory](https://github.com/advisories/GHSA-rrmf-rvhw-rf47),
updated 2026-07-17 and checked 2026-10-13, lists Torch versions through 2.12.1 as
affected and 2.13.0 as patched. It links
[upstream issue 149623](https://github.com/pytorch/pytorch/issues/149623) and
[fix b90c94991cdf8b87c8f7439f79518e0ef2c4ca4f](https://github.com/pytorch/pytorch/commit/b90c94991cdf8b87c8f7439f79518e0ef2c4ca4f),
which rejects bare list/tuple value annotations in JIT. The advisory supplies
the patched-release claim; the commit supplies the code change. The exact CPU
wheel URL and hash are recorded beside the Torch requirement in `inference.in`.

[inference.lock](inference.lock) records the resolved versions and permitted
distribution hashes. [advisories.json](advisories.json) retains the check time,
exact-version PyPI URLs and returned advisory entries, plus input and lock
digests. Its check at 2026-10-13T17:54:07.461093+00:00 returned no advisory
entries for the 59 locked packages. Torch's PyPI query uses the public version
2.13.0; the CPU wheel remains identified separately by URL and hash. This receipt
is an advisory check, not a complete security audit or proof that no vulnerabilities
exist. Intentional dependency updates must stage and review a new lock and receipt
through the maintained commands in the [reproduction reference](../docs/reproduction.md#checks-and-dependency-updates).

Editing `inference.in`, `build.in` and `unicore-source.json` before staging an
intentional update is supported. The resolver recipe and execution tools must
match committed HEAD. Apply admits fresh copies of current intent and the
committed tools, then checks the intent hashes against the reviewed stage;
it does not reuse the stage's old input copies.

The selected release scalers were serialized with sklearn 1.5.2 and load under
1.8.0 with `InconsistentVersionWarning`. A contained probe checked both actual
one-feature `StandardScaler` instances against independent affine arithmetic:
selected-label standardization and the Torch-input inverse followed by the
upstream float32 output cast agree exactly. The warning remains in retained
logs; this is a qualification of these dense operations, not general estimator
compatibility across versions. Fitted parameters, probe identities and evidence
are recorded in [note 001](../notes/001_contained_upstream_baseline_20261013.txt).
The source of the paired scaler load and transform is
[upstream data_scaler.py](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/data_scaler.py#L32-L54);
[atom_regloss.py](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L75-L76)
performs the final inverse transformation and cast.

A separate metadata refresh on 2026-10-15 (14:56:51–15:00:41 UTC) again returned
no PyPI advisory entries for the 59 applied lock versions. It also inventoried
the pinned base image: inherited pip 25.0.1 has 12 advisory records representing
six CVEs in its [exact-version PyPI metadata](https://pypi.org/pypi/pip/25.0.1/json).
The fixed versions reported there are:

| Advisory | Reported fixed version |
| --- | --- |
| [CVE-2025-8869](https://github.com/advisories/GHSA-4xh5-x5gv-qwph) | 25.3 |
| [CVE-2026-1703](https://github.com/advisories/GHSA-6vgw-5pg2-w6jp) | 26.0 |
| [CVE-2026-3219](https://github.com/advisories/GHSA-58qw-9mgm-455v) | 26.1 |
| [CVE-2026-6357](https://github.com/advisories/GHSA-jp4c-xjxw-mgf9) | 26.1 |
| [CVE-2026-8643](https://github.com/advisories/GHSA-wf93-45jw-7689) | 26.1.2 |
| [CVE-2026-13346](https://github.com/advisories/GHSA-qwm4-qh6w-59xr) | 26.2 / 26.2.0 |

The build uses `--no-index`, `--disable-pip-version-check` and verified offline
inputs. These controls do not repair pip's archive or entry-point handling.
Pip installs as root inside the build image; the final runtime uses UID 65532.
The base image also has unresolved Debian advisory coverage, including vulnerable
bookworm rows in the official [glibc](https://security-tracker.debian.org/tracker/source-package/glibc)
and [OpenSSL](https://security-tracker.debian.org/tracker/source-package/openssl)
source pages. Empty public advisory lists for CPython and custom Uni-Core do not
establish complete coverage. This refresh preserves the historical applied
`advisories.json`; exact receipt identities, scope and remaining qualification
work are recorded in [note 011](../notes/011_repository_check_ports_20261014.txt).
