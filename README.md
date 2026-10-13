# NMRNet++ reference inference

Contained reproduction of the pinned NMRNet++ H/C inference path. The fork is
the `NMRNetplusplus` Git submodule. Implementation and execution evidence are
recorded in [note 001](notes/001_contained_upstream_baseline_20261013.txt).

**Qualification is in progress.** Tooling and fixture tests are implemented;
the real inference image builds and passes dependency/import checks. Selected
checkpoints and repeated H/C predictions still require qualification. This
repository does not yet provide an NMR API provider or a SMILES prediction interface.

## Prerequisites

Use Linux x86-64, Bash, Make, Git, and local Docker with BuildKit named-context
support. Run as a non-root user able to access the local Docker engine. Python,
dependency operations, archive processing, tests, and inference run in containers.

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

## Acquire and prepare

Every networked acquisition requires an explicit Make command-line
`WLAN_INTERFACE=YOUR_INTERFACE`. Replace `YOUR_INTERFACE` with the intended
existing interface. An environment-only setting is not accepted. HTTP(S)
connections use a contained interface-bound proxy; DNS uses the host resolver.
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

## Run the reference

After image construction and artifact preparation, the maintained offline command
preflights both H/C models, runs each twice and compares source-aligned predictions:

```text
make baseline/run RUN_ID=<new-run-id> LABEL_ATOL=<label-allowance> ATOL=<absolute-tolerance> RTOL=<relative-tolerance>
```

Replace the placeholders with explicit command-line values; environment-only
settings are not accepted. `RUN_ID` must start with a letter or digit and contain
1-80 letters, digits, dots, underscores or hyphens. Each tolerance must be finite
and nonnegative. `LABEL_ATOL` allows absolute rounding error when checking labels
after the upstream float32 scaler round trip against the original record labels.
`ATOL` and `RTOL` govern repeated predictions, with each atom requiring
`abs(repeated - reference) <= ATOL + RTOL * abs(reference)`.

**No tolerance values or reference outputs have been qualified yet.** Actual
execution awaits completed downloads. Establish and document the rounding and
repeatability allowances for the selected runtime before running; fixture-test
values are not scientific defaults. Do not relax them to make a failed comparison
pass.

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
this execution receipt alone does not establish scientific qualification.

## Checks and dependency updates

```sh
make test
make test/probe
```

These commands run offline with read-only inputs/root, a non-root identity,
dropped capabilities, no-new-privileges, and bounded CPU, memory, PIDs, and scratch.
They do not execute real checkpoints or establish scientific accuracy.

For an intentional dependency update, stage a new lock and advisory receipt:

```sh
make packages/lock/stage WLAN_INTERFACE=YOUR_INTERFACE
```

Review them, then apply offline with `make packages/lock/apply STAGE=lock.ID`,
using the printed stage ID. Lock resolution also requires the pinned UV image
declared in `tools/packages.sh` to be preloaded. The advisory check covers
exact-version PyPI metadata; it is not a complete security audit.
