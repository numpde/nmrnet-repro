# Dependency sources and decisions

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
through the maintained commands in the [repository README](../README.md).

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
