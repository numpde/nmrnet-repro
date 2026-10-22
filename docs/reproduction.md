# Reproducing the numerical evidence

Use this reference to acquire the pinned release, reproduce the bounded CPU calculation, inspect diagnostics and run the maintained checks. The [scientific guide](https://numpde.github.io/nmrnet-repro/) explains what those comparisons establish; the [provider reference](provider.md) covers API operation.

## Prerequisites

Run `make help` or simply `make` to list every maintained command. Command
discovery uses Make and its shell without starting Docker or Python. Commands
that acquire packages or scientific artifacts list their required `WLAN_INTERFACE`.
The proxy and Debian Python base have separate bootstrap download commands using
Docker's default routing; the two generic base-image pull names still print guidance.
`ARCHIVE` and `STAGE` also require explicit Make command-line assignments.
Environment-only selectors are not accepted. Make passes admitted values
literally; it does not expand selector text as Make functions.

Use Linux x86-64, Bash, Make, Git, and local Docker with BuildKit named-context
support. Run as a non-root user able to access the local Docker engine. Python,
dependency operations, archive processing, tests, and inference run in containers.

Owned Python workers require `NMRNET_IN_CONTAINER=1`, supplied explicitly by
their maintained container launchers. Use the Make targets rather than invoking
Python directly. The shared [guard](../tools/container_guard.py) prevents accidental
host execution; Docker's configured grants enforce isolation. The environment
marker is not proof that a process is contained.

Initialize the pinned source:

```sh
git submodule update --init --recursive
```

The repository uses these pinned images for Python workers and the download proxy:

```text
docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
docker.io/library/python:3.12.12-alpine3.22@sha256:848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32
```

The proxy uses Alpine because it needs only Python's standard library: its
amd64 compressed download is [17.12 MB](https://hub.docker.com/layers/library/python/3.12.12-alpine3.22/images/sha256-5c84c59004e6679bc98a33930344c6ea3da9e2434869b29bc950e46902b6c383),
compared with [43.32 MB for Debian slim](https://hub.docker.com/layers/library/python/3.12.12-slim-bookworm/images/sha256-2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301)
(registry metadata checked 2026-10-19). Scientific workers retain Debian because
the downloaded `manylinux` packages require glibc; Alpine uses musl.
See the [wheel compatibility specification](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/).

To download the proxy image and Debian Python worker base into local Docker, run:

```sh
make packages/base-images/proxy/download
make packages/base-images/python/download
```

These bootstrap commands use the Docker daemon's network settings and default
routing. They do not force traffic onto a particular interface; `WLAN_INTERFACE`
does not control either command. Docker must be running and accessible. These are
explicit bootstrap exceptions to interface-bound acquisition: the proxy cannot
download its own starting image, and a daemon pull preserves the Debian image's
exact registry digest reference for subsequent offline workers. Package and
artifact downloads still require
`WLAN_INTERFACE`. See [digest pulls](https://docs.docker.com/reference/cli/docker/image/pull/#pull-an-image-by-digest-immutable-identifier)
and [daemon proxy settings](https://docs.docker.com/engine/daemon/proxy/).

A failed or interrupted download can leave layers or a complete image in Docker;
stopping the client does not establish that daemon work stopped. Rerun the same
target to ensure the pinned image is available. Neither command removes images.
The generic base-image pull targets still print guidance and refuse downloads.

Repository checks use a separate image containing pinned Git, Make and Ruff.
Prepare it explicitly after provisioning that base image:

```sh
make checks/dependencies/download WLAN_INTERFACE=YOUR_INTERFACE
make checks/dependencies/check
make checks/image/build
```

The [dependency declaration](../requirements/checker-dependencies.json) and
[advisory record](../requirements/checker-advisories.json) identify the exact
tool closure; [preparation notes](../requirements/checker.md) explain its sources
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
for reading molecular structures and generating conformers offline; it does not
replace inference's `wheelhouse/cpu`. Its evidence and limits are in
[the preparation dependency record](../requirements/preparation.md).

Run `make test/integration/rdkit-runtime` to check the retained RDKit dependencies
offline in a temporary Python environment. It checks the complete installed
package list and native extension origins; it does not read molecular files or
generate conformers. This lane requires the Debian Python base listed above.

Build and check the RDKit image before testing native molecular readers:

```sh
make geometry/image/build
make test/integration/rdkit-worker
```

The build copies committed image inputs and the retained wheelhouse, verifies
the complete lock, and installs only those wheels into an isolated Python
environment. Docker builds without network access or implicit image pulls.
The recorded immutable image ID is checked against its local image and a label
identifying the committed build inputs. A changed lock or image recipe requires
a rebuild; unrelated commits do not. The check runs offline with read-only inputs
and temporary scratch, using that image's installed packages. It checks the lock,
interpreter isolation and native extension origins. Molecular reader tests and
conformer generation require their own behavioral evidence.

`make test/integration/mol-reader` runs the direct V2000 MOL reader checks in that
recorded image. The reader preserves supplied atoms and coordinates, requires
declared source units and a 3D header, and rejects V3000 before native parsing.
MOL/SDF reading is also available over the private worker protocol. The shared
Upload/interpreter binding accepts declared formats. This lane checks direct MOL
reading; the service's XYZ comparison is listed under Additional comparisons.

`make test/integration/sdf-reader` checks ordered SDF record selection in the same
isolated image. It preserves supplied atoms, coordinates, original record numbers
and the whole-file digest. Selected records must contain supported 3D V2000 MOL
structures; a failed selected record rejects the whole selection. Associated data
is kept outside geometry input. The reader does not check whether records describe
the same molecule. This lane checks direct SDF reading independently of the XYZ
service comparison.

`make test/integration/native-geometry-worker` runs real MOL/SDF parsing across
the private socket in the recorded RDKit image. It checks original atom/record
identity, source hashes, unit conversion, selected record order and whole-selection
rejection. The controller process does not import RDKit. Exact-container retirement
has separate container-composition lanes below; the native socket tests run child
processes inside one contained test.

`make test/integration/provider-geometry-worker` checks separate checker-image
containers for XYZ success and retirement of a stalled reader.
`make test/integration/provider-native-geometry-worker` checks a separate admitted
RDKit reader and checker-image controller for MOL/SDF success, source rejection,
protected native failure and host-confirmed retirement. Its stall case parses a
real MOL first, then deliberately blocks while ignoring termination. The exact
allocation marker must already be present when the read timeout is handled; a
late marker fails qualification. This tests retirement after a controlled stall,
without claiming an observed RDKit hang. Both lanes are offline and leave engine
access with the host. Image identities, posture, source hashes and retirement
receipts stay under `.work/qualification`; native failure text stays private.

## Retrieve failure diagnostics

When a command reports a retained diagnostic path, inspect it first.
[Acquisition](../tools/docker.sh) receipts under `/tmp/nmrnet-proxy.*` include create
`stdout`/`stderr`, `preload-N.stderr`, container IDs and readiness
`inspect.stderr`, `logs.stdout` and `logs.stderr`, when produced.
[Checker](../tools/checker.sh) and [baseline](../tools/baseline.sh) image-admission
receipts are `stdout`/`stderr` under `/tmp/nmrnet-checker-admission.*` and
`/tmp/nmrnet-baseline-admission.*`. Each metadata output file is capped at
4096 bytes. Failed captured operations can retain combined stdout/stderr in
`/tmp/nmrnet-inference-native.*/command.log`, capped at 8 MiB by
[the native client owner](../tools/inference_native.sh).
Failed [Docker client preparation](../tools/docker_client.sh) can retain
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

The [contained test launcher](../tools/container.sh) can retain failed checker-image
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
[Note 001](../notes/001_contained_upstream_baseline_20261013.txt) records the arithmetic
rationale, actual errors, scaler-version warning and scientific source review.

Both baseline commands use the immutable image ID recorded by `packages/image`.
Before importing scientific code, they compare the image's embedded
`inference.lock` and `unicore-source.json` against the current declarations in
`requirements/`. A mismatch requires rebuilding the image: the package lock
alone does not identify the separately built Uni-Core source. `baseline/run`
also requires the image's upstream-revision label to match the recorded submodule
commit and records the source declaration's hash. These checks are implemented
in [tools/baseline.sh](../tools/baseline.sh) and
[tools/baseline_run.py](../tools/baseline_run.py). Both commands run offline with
read-only inputs.

Evidence is retained under `.work/baseline/runs/<RUN_ID>/`, including model
preflights, separate H/C `run-1` and `run-2` outputs and logs, verification receipts
and comparisons. `attempt.json` and a successful `complete.json` record the
execution image ID, repository revision and mounted tool hashes. The run's
`prepared_receipt` hash links back to the unchanged preparation provenance;
[tools/baseline_prepare.py](../tools/baseline_prepare.py) computes these file
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

## Additional comparisons

Use the maintained lanes to check distinct boundaries:

| Command | Comparison |
| --- | --- |
| `make prediction/worker-parity` | Direct and process/socket predictions on four retained geometries, with zero absolute and relative tolerances |
| `make test/integration/provider-scientific-worker` | Released CPU model execution, retained result bytes and signed publication to a local contract peer |
| `make test/integration/provider-service/references` | Four release geometries through the Upload service; all 137 H/C outputs and 64 historical reference positions |
| `make test/integration/provider-service/pasted` | Supplied nm table, atom order and independently bounded coordinate conversion |
| `make test/integration/provider-service/uploads` | Selected XYZ records in order, including H-only and H/C output coverage |
| `make test/integration/provider-service/smiles` | Three generated butane conformers, H-parent mapping and exact downstream coordinates |
| `make test/integration/provider-service/capacity` | H128/C128 execution and measured resource use; this is a capacity construction, not an accuracy benchmark |

These service lanes use local scripted API/store and interpreter peers. A passing
lane establishes its recorded comparison, not experimental accuracy or
cross-platform equality. The [scientific guide](https://numpde.github.io/nmrnet-repro/)
explains how the numerical references differ from published benchmark errors.
The [operator reference](provider.md) covers deployed API checks and the
[experiment record](../notes/003_nmr_api_provider_20261018.txt) retains individual
run outcomes and source/image identities.

Prediction and input preparation run in separate offline containers. Only the
prediction worker receives checkpoints; the controller owns API credentials and
durable state. The host confirms exact-container retirement before publication.
Successful runs retain source-hashed summaries under `.work/qualification/`;
failed runs retain private receipts, diagnostics and journals at the reported
paths. Preserve those records before attempting recovery.

## Checks and dependency updates

```sh
make test                 # offline unit, repository, core contract and integration suites
make test/unit
make test/repository
make test/probe
```

These commands run offline with read-only inputs/root, a non-root identity,
dropped capabilities, no-new-privileges, and bounded CPU, memory, PIDs, and scratch.
They do not execute real checkpoints or establish scientific accuracy.
The repository suite includes strict conventions imported from sibling
repositories. Their provenance and review decisions are recorded in
[note 011](../notes/011_repository_check_ports_20261014.txt),
along with source crosswalks, execution requirements and exact run results.
The prepared checker supplies Git, Make and Ruff 0.15.15; the unit lane keeps
the smaller Python base.
[Repository conventions](../repository_checks/test_conventions.py) check early
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
