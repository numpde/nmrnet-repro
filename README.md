# NMRNet++ for the NMR API

This repository aims to provide reproducible, containerized NMRNet++ H/C
chemical-shift prediction through the NMR API. The provider is not implemented
yet. The fork is the `NMRNetplusplus` Git submodule.

The current work is the provider workflow in
[note 003](notes/003_nmr_api_provider_20261018.txt), following the
[tests-first implementation plan](notes/016_provider_tests_first_20261018.txt).
A local reusable `provider-core` package now installs independently and verifies
exact Job input for the internal NMRNet adapter. Signed Job/Upload API operations,
bounded HTTPS transport and verified Upload acquisition have local contained tests.
Attempt lifecycle and provider composition remain in the
[implementation plan](notes/017_provider_core_plan_20261018.txt).
Its three planned inputs are SMILES plus conformer count, one pasted geometry,
and uploaded structures with instructions. These are not yet public API features.
The internal preparation foundation preserves direct angstrom JSON and converts
an element/x/y/z table or selected plain XYZ frames labelled angstrom, nm or bohr.
Pasted XYZ accepts one frame; file preparation preserves the selected frame order
without checking that frames represent the same molecule. `make test/unit/provider`
checks these readers in an offline container. Upload access and submission remain
unimplemented. A standalone prediction CLI remains deferred.

**The bounded CPU reference is qualified.** Two intact records per element
produce 42 H and 22 C predictions from the selected fold-0 checkpoints; independent
repeats match exactly in the recorded runtime. The public
[numerical reference](artifacts/reference-cpu-float32.json) preserves original
record/atom identities, predictions, input hashes and runtime evidence.
This is the completed reference milestone in
[note 001](notes/001_contained_upstream_baseline_20261013.txt).
The composed callable is now qualified for atom-and-coordinate requests that
state `angstrom`, on the four retained validation fixtures. The provider remains
unimplemented. SMILES generation, readers and their atom mapping will be qualified
separately from this fixed-coordinate scientific evidence.

`make prediction/qualify` runs tensor parity and the real-model callable checks
in the pinned offline inference container. It compares 32 shared tensor values
exactly and matches all 64 reference positions exactly. It also checks full atom
coverage, reuse and synthetic capacity boundaries. The repository adopts
`angstrom` as the H/C input coordinate unit, supported by pinned RDKit
preprocessing evidence and a contained NIST bond-distance comparison on two
retained validation records. This is an operational convention with bounded
evidence; original coordinate producer attribution remains unknown. Qualification
details, limitations and rerun instructions are in
[note 002](notes/002_label_free_coordinate_prediction_20261014.txt) and the
[CPU float32 evidence packet](artifacts/prediction-cpu-float32.json).

The historical [fixture observation](artifacts/prediction-fixture-cpu-float32.json)
retains all 137 H/C results and original atom lists from four release fixtures,
including the 64 historical reference positions, with runtime and byte identities.
It uses test-only unit admission; the qualification packet records the production
`angstrom` admission. The contained lane also verifies that C failure
after real H prediction returns no partial result; an exact-container probe
confirmed TERM cancellation for this workload. These are bounded callable proofs,
not broad chemical-accuracy claims or published ensemble accuracy.

[prediction-models.json](artifacts/prediction-models.json) pins the runnable
dictionary and paired H/C checkpoint/scaler bytes. The release loader checks
both this declaration and extraction provenance before decoding. It supplies
the existing `predict_request` callbacks; it adds no standalone command.
Loader/inference exceptions and nested causes remain available to the authorized caller. They
are diagnostic evidence; the future provider must project them for its recipient.

## Scientific references

- Yongqi Jin et al., [*From Human Labels to Literature: Semi-Supervised Learning
  of NMR Chemical Shifts at Scale*](https://arxiv.org/html/2601.18524v1),
  arXiv:2601.18524v1, 26 January 2026. This is the paper identified by the
  [pinned upstream README](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/README.md#L4).
  Table 6 supplies the ppm convention; Section 4.1 describes the NMRShiftDB2
  benchmark split. These motivate the unit and dataset checks in note 001.
- [NMRNet++ model/data release, Zenodo record 18232165](https://zenodo.org/records/18232165),
  DOI `10.5281/zenodo.18232165`. This exact release supplies the dictionary,
  validation records, checkpoints and paired scalers. Archive identities and
  selected members are recorded in [artifacts/release.json](artifacts/release.json)
  and the adjacent selection declarations.
- Scientific implementation at revision
  `3ba8b9c2f3d0a390d00685d238618345c6ab24d7`:
  [infer.sh](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/script/infer.sh)
  supplies the upstream inference invocation, and
  [atom_regloss.py](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L75-L76)
  establishes that saved predictions receive one inverse scaling operation.
  These sources guide the reproduced computation and output interpretation.

## Published ensemble accuracy

The **NMRNet + Semi-supervised** H/C results in
[Table 2 of arXiv:2601.18524v1](https://arxiv.org/html/2601.18524v1)
come from averaging predictions across five checkpoint folds, then computing
the error metrics. The paper's methods do not explicitly describe this averaging;
the evidence is the pinned evaluation code and matching results in the released
logs.

[infer.sh:11–28](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/script/infer.sh#L11-L28)
runs folds 0–4 on the same evaluation data.
[get_result.py:80–84](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/get_result.py#L80-L84)
averages their prediction vectors before calculating mean absolute error (MAE)
and root mean squared error (RMSE). This differs from averaging five fold error
scores. Atom-level evaluation uses assigned atoms; for molecule-level evaluation,
[get_result_unlabel.py:97–103](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/get_result_unlabel.py#L97-L103)
sorts the averaged predictions and target shifts within each molecule before
calculating errors.

The following ensemble results match both Table 2 and the released logs; all
errors are in ppm:

| Evaluation | ¹H MAE | ¹H RMSE | ¹³C MAE | ¹³C RMSE |
| --- | ---: | ---: | ---: | ---: |
| NMRShiftDB2, atom-level | 0.1709 | 0.4337 | 0.9270 | 1.9128 |
| NMRShiftDB2, molecule-level | 0.1492 | 0.3620 | 0.7765 | 1.5629 |
| ShiftDB-Lit, molecule-level | 0.0559 | 0.1846 | 0.5060 | 2.3494 |

In [Zenodo record 18232165](https://zenodo.org/records/18232165),
[checkpoints.zip](https://zenodo.org/api/records/18232165/files/checkpoints.zip/content)
contains `result.log`, `result_unlabel_1.log` and `result_unlabel_2.log`,
respectively, under the H configuration ending `20250930_033837` and C
configuration ending `20251004_182236`. Each log lists five fold results and a
final `metric of mean` matching the corresponding table entry above.
[Note 001, “Published accuracy and ensemble averaging”](notes/001_contained_upstream_baseline_20261013.txt)
records exact member-path identification, all six SHA-256 hashes and the bounded
archive-inspection evidence.

**This repository qualifies a smaller execution reference:** two records per
element using fold 0 only. It does not reproduce the ensemble benchmark scores.
For comparison, the release logs report fold-0 atom-level MAEs of 0.1842 ppm
for H and 0.9876 ppm for C, versus ensemble MAEs of 0.1709 and 0.9270 ppm.
Those release benchmark values are separate from our two-record reference.

## Prerequisites

Run `make help` or simply `make` to list every maintained command. Command
discovery uses Make and its shell without starting Docker or Python. Commands
that acquire external inputs list their required `WLAN_INTERFACE`; the two
base-image pull names print provisioning guidance and refuse daemon downloads.
`ARCHIVE` and `STAGE` also require explicit Make command-line assignments.
Environment-only selectors are not accepted. Make passes admitted values
literally; it does not expand selector text as Make functions.

Use Linux x86-64, Bash, Make, Git, and local Docker with BuildKit named-context
support. Run as a non-root user able to access the local Docker engine. Python,
dependency operations, archive processing, tests, and inference run in containers.

Owned Python workers require `NMRNET_IN_CONTAINER=1`, supplied explicitly by
their maintained container launchers. Use the Make targets rather than invoking
Python directly. The shared [guard](tools/container_guard.py) prevents accidental
host execution; Docker's configured grants enforce isolation. The environment
marker is not proof that a process is contained.

Initialize the pinned source:

```sh
git submodule update --init --recursive
```

The following base image must already be available in the local engine:

```text
docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
```

Provision it through an independently interface-bound process. The repository's
base-image pull targets deliberately refuse daemon downloads: the acquisition
proxy cannot bind Docker daemon traffic to the requested interface.

Repository checks use a separate image containing pinned Git, Make and Ruff.
Prepare it explicitly after provisioning that base image:

```sh
make checks/dependencies/download WLAN_INTERFACE=YOUR_INTERFACE
make checks/dependencies/check
make checks/image/build
```

The [dependency declaration](requirements/checker-dependencies.json) and
[advisory record](requirements/checker-advisories.json) identify the exact
tool closure; [preparation notes](requirements/checker.md) explain its sources
and accepted offline scope. Ordinary checks install nothing. They admit the
recorded immutable image and read only public source, sanitized Git metadata
and the two local object stores. Original Git configuration and hooks are not
granted. Test containers use an init process to reap orphaned probe children
under the existing resource limits.

## Acquire and prepare

Every networked acquisition requires an explicit Make command-line
`WLAN_INTERFACE=YOUR_INTERFACE`. Replace `YOUR_INTERFACE` with the intended
existing interface. An environment-only setting is not accepted. HTTP(S)
connections and IPv4 DNS queries use the selected device through the contained
proxy. DNS requires a configured non-loopback IPv4 resolver; no unbound system
resolver fallback is used.
Offline commands need no interface setting.

Use the checked-in dependency lock:

```sh
make packages/wheelhouse WLAN_INTERFACE=YOUR_INTERFACE
make packages/wheelhouse/check
make packages/image
make packages/import
```

Acquire the three declared release archives, verify them, and extract only the
selected members:

```sh
for archive in weight.zip data.zip checkpoints.zip; do
    make weights/download ARCHIVE="$archive" WLAN_INTERFACE=YOUR_INTERFACE || break
    make weights/check ARCHIVE="$archive" || break
    make weights/extract ARCHIVE="$archive" || break
done
```

Once all three archives have been verified and extracted:

```sh
make baseline/prepare
```

Full downloads total about 6.96 GB. Complete-archive checksum verification is
required before extraction, although the reference consumes only a dictionary,
labelled validation data, two fold-0 checkpoints, their scalers, and release logs.
Allow additional space for extracted artifacts, packages, images, and results.

Downloads resume retained `.part` files. Prepared directories are published
without replacing existing results; extraction and `baseline/prepare` refuse
an existing destination. Generated files remain under ignored `.work/`.
Preparation selects two intact labelled examples for each element, with at most
128 atoms and 130 tokens. It preserves original record bytes and atom order.
Its `provenance.json` also records the preparation image ID, repository revision
and size/SHA-256 of the mounted Python tools. These tool hashes identify the
executed checkout bytes, including uncommitted edits that Git HEAD alone omits.

Keep the operational `.work/` roots (`downloads`, `prepared`, `preparation`, `wheelhouse`,
`inference`, `baseline`, `packages-stage` and `checker`) at their maintained paths. Completed
captures belong under `.work/qualification/<topic-or-run>/`; the original
`.work/qualification/20261014/` baseline evidence stays in place. Captured scripts
are evidence of historical commands and paths; use maintained Make commands to
rerun workflows. Retain reusable inputs while offline execution is needed,
qualification evidence while its claims are published, and
debug captures until the issue is resolved or reviewed replacement evidence is
accepted. Review package stages before adoption or removal.
The separate `preparation/wheelhouse` holds the reviewed RDKit dependency wheels
for offline geometry-preparation development; it does not replace inference's
`wheelhouse/cpu`. Its current evidence and limits are in
[the preparation dependency record](requirements/preparation.md).

## Retrieve failure diagnostics

When a command reports a retained diagnostic path, inspect it first.
[Acquisition](tools/docker.sh) receipts under `/tmp/nmrnet-proxy.*` include create
`stdout`/`stderr`, `preload-N.stderr`, container IDs and readiness
`inspect.stderr`, `logs.stdout` and `logs.stderr`, when produced.
[Checker](tools/checker.sh) and [baseline](tools/baseline.sh) image-admission
receipts are `stdout`/`stderr` under `/tmp/nmrnet-checker-admission.*` and
`/tmp/nmrnet-baseline-admission.*`. Each metadata output file is capped at
4096 bytes. Failed captured operations can retain combined stdout/stderr in
`/tmp/nmrnet-inference-native.*/command.log`, capped at 8 MiB by
[the native client owner](tools/inference_native.sh).
Failed [Docker client preparation](tools/docker_client.sh) can retain
`/tmp/nmrnet-docker-client.*/preparation.log`, capped at 4096 bytes, alongside
an empty or partial config directory. Directory-allocation failure has no
private receipt path. Storage failure can also prevent the final reporter
from starting; the command's exit status remains available.
These directories have mode `0700`; read them as the user who ran the command.
Capped files may be incomplete; storage failures can prevent retention.

Cancellation can finish silently. Ordinary acquisition preflight exits 130/143
also stay silent while retaining inspection receipts. Find surviving temporary
diagnostic directories owned by your user:

```sh
find /tmp -maxdepth 1 -type d -uid "$(id -u)" \
    \( -name 'nmrnet-proxy.*' -o -name 'nmrnet-inference-native.*' \
    -o -name 'nmrnet-docker-client.*' \
    -o -name 'nmrnet-checker-admission.*' -o -name 'nmrnet-baseline-admission.*' \) -print
```

The [contained test launcher](tools/container.sh) can retain failed checker-image
inspection receipts under `.work/checker/.inspect.*/`: `observed-id` and
`diagnostic`, each capped at 4096 bytes. Setup or storage failure can leave either
file absent or incomplete; directory-allocation failure establishes no inspection
receipt. A wrapper setup failure can also leave a captured cause in the native
client owner's `command.log`, described above. These private receipts can survive
silent cancellation too. From the repository root, when `.work/checker` exists:

```sh
find .work/checker -mindepth 1 -maxdepth 1 -type d -uid "$(id -u)" \
    -name '.inspect.*' -print
```

Successful inspection receipts are normally removed during launcher cleanup.
Failed inspections retain their receipts for review; no automatic lifetime is
promised. A failed removal can leave partial evidence or no files, so check
which paths survive before relying on them.

Several attempts may coexist; a matching name alone neither identifies the
attempt nor authorizes process/container cleanup. If retirement is unconfirmed,
preserve resources and receipts until ownership and state are established.

Acquisition workers stream output live; the launcher does not retain a
transcript. Save stdout and stderr when running the command for later use.
Manual or system cleanup or reboot can remove temporary diagnostics. Copy needed
evidence to a private location and review it before deleting it.

## Run the reference

After image construction and artifact preparation, the maintained offline command
preflights both H/C models, runs each twice and compares source-aligned predictions:

```sh
make baseline/run RUN_ID=reference-cpu-1 LABEL_ATOL=0.0001 ATOL=0 RTOL=0
```

Use a fresh `RUN_ID` for each attempt; environment-only settings are not accepted.
`RUN_ID` must start with a letter or digit and contain
1-80 letters, digits, dots, underscores or hyphens. Each tolerance must be finite
and nonnegative. `LABEL_ATOL` allows absolute rounding error when checking labels
after the upstream float32 scaler round trip against the original record labels.
`ATOL` and `RTOL` govern repeated predictions, with each atom requiring
`abs(repeated - reference) <= ATOL + RTOL * abs(reference)`.

These tolerances were declared before execution. The label allowance exceeds
the calculated float32 scaler-rounding bound for the selected labels; repeat
tolerances require exact equality in the recorded CPU runtime. The qualified
run used Python 3.12.12, Torch 2.13.0+cpu, batch size 1 and two computation
threads. Cross-platform or GPU equality has not been established. Investigate
differences rather than relaxing tolerances to make a comparison pass.
[Note 001](notes/001_contained_upstream_baseline_20261013.txt) records the arithmetic
rationale, actual errors, scaler-version warning and scientific source review.

Both baseline commands use the immutable image ID recorded by `packages/image`.
Before importing scientific code, they compare the image's embedded
`inference.lock` and `unicore-source.json` against the current declarations in
`requirements/`. A mismatch requires rebuilding the image: the package lock
alone does not identify the separately built Uni-Core source. `baseline/run`
also requires the image's upstream-revision label to match the recorded submodule
commit and records the source declaration's hash. These checks are implemented
in [tools/baseline.sh](tools/baseline.sh) and
[tools/baseline_run.py](tools/baseline_run.py). Both commands run offline with
read-only inputs.

Evidence is retained under `.work/baseline/runs/<RUN_ID>/`, including model
preflights, separate H/C `run-1` and `run-2` outputs and logs, verification receipts
and comparisons. `attempt.json` and a successful `complete.json` record the
execution image ID, repository revision and mounted tool hashes. The run's
`prepared_receipt` hash links back to the unchanged preparation provenance;
[tools/baseline_prepare.py](tools/baseline_prepare.py) computes these file
identities inside the container. Existing destinations are refused. Failed
attempts preserve their available evidence; use a new `RUN_ID` for another attempt.
After creating a new attempt directory, the runner retains caught failures and
their exception chains in `failure.log`, synchronizes it, then re-raises the
original error. Admission failures create no attempt; abrupt termination or
output-storage failure can prevent diagnostic retention.
`complete.json` is published only after execution, output verification and
repeatability checks succeed. Its `pending_review` entries remain explicit:
this execution receipt alone does not establish scientific qualification. For
the checked-in reference, the separate source/scaler review and independent
runtime audit recorded in note 001 close those reviews without rewriting the
original receipt. This is a fold-0 execution reference, not a five-fold ensemble
or a benchmark accuracy claim.

The provider worker's `PredictionSession` separates preparation from execution.
Preparation parses the request, admits every atom against the selected release
dictionary, and composes all required H/C tensors before returning an opaque handle.
Execution accepts only that session's current handle and consumes it before loading
models. A new preparation invalidates the earlier handle, including when replacement
input is rejected. Model/scaler calculation and output aggregation are shared with
the existing direct prediction path. H/C models still load sequentially; both
bounded prepared tensor sets remain live together.

The current-handle rule follows
[NMRPeak's session-owned generation](https://github.com/numpde/nmrpeak-repro/blob/1697dc84dd1d76ae25b04efc8183fd80614000e5/nmrpeak_provider/runner_session.py#L333-L350).
Contained tests check real tensor preparation and exact agreement with direct
prediction using a deterministic Torch model and scaler. This is a synchronous
scientific boundary for a serial worker. Core now owns durable execution-entry
ordering, but full worker/container integration and released-checkpoint qualification
through this session remain unfinished.

The private worker transport now connects an asynchronous `PredictionClient` to
one serial `serve_connection` worker. It preserves request bytes, binds replies to
the configured allocation, and keeps the scientific prepared object in the worker.
Wire tokens correlate preparations; only the client's current local handle can
request execution. Length-prefixed, bounded frames follow
[SECS's scientific framing](https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/src/secs_inference/provider/worker.py#L348-L374).
Private exception chains remain diagnostic evidence; failed sends are never retried
on a partially written stream. Oversized diagnostics fail rather than being truncated.

`make test/unit/provider` exercises real worker processes and sockets with recording
scientific operations, including cancellation while the worker stays busy. This
proves the exchange boundary, not model inference, credential isolation or container
retirement. Allocation/connection correlation is not yet Attempt/generation binding.
The host supervisor must still own exact-container shutdown and protected stderr;
socket closure is deliberately not exposed as an `AnalysisSession.retire()` result.

## Checks and dependency updates

```sh
make test                 # aggregate: unit and repository convention suites
make test/unit
make test/repository
make test/probe
```

These commands run offline with read-only inputs/root, a non-root identity,
dropped capabilities, no-new-privileges, and bounded CPU, memory, PIDs, and scratch.
They do not execute real checkpoints or establish scientific accuracy.
The repository suite includes strict conventions imported from sibling
repositories. Foundation closure is in progress; remaining failures and their
dispositions are recorded in [note 011](notes/011_repository_check_ports_20261014.txt),
along with source crosswalks, execution requirements and exact run results.
The prepared checker supplies Git, Make and Ruff 0.15.15; the unit lane keeps
the smaller Python base.
[Repository conventions](repository_checks/test_conventions.py) check early
shared guards across owned Python sources, hierarchical Make targets and the
test aggregate. New files under `tools/`, `repository_checks/` and
`nmrnet_inference/` enter that inventory automatically. The suite records the
pinned NMRPeak and Magnet precedents for its organization and source checks.

For an intentional dependency update, stage a new lock and advisory receipt:

```sh
make packages/lock/stage WLAN_INTERFACE=YOUR_INTERFACE
```

Review them, then apply offline with `make packages/lock/apply STAGE=lock.ID`,
using the printed stage ID. Lock resolution also requires the pinned UV image
declared in `tools/packages.sh` to be preloaded. The advisory check covers
exact-version PyPI metadata; it is not a complete security audit.

Image preflight and stage acquisition each wait for their controller's cleanup
path to return on cancellation; that path can report cleanup failure.
An unresponsive controller can keep that wait running indefinitely. Any captured
stage inputs remain available after failure or cancellation. The successful-stage
Review instruction follows only a zero controller result.

Package staging accepts edits to the `requirements/` intent files `inference.in`,
`build.in` and `unicore-source.json`. It captures the committed resolver recipe and tools,
plus the current intent, in the stage's private `.inputs/` directory. Retain
that snapshot with the review stage after success or failure. Apply checks a
fresh snapshot of current intent against the reviewed report; edits since
staging require another stage. Each snapshot reflects admission for that
invocation; edits across the three files are not one transaction.
