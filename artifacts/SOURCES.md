# Scientific sources

`nmrnet-source.paths` declares `LICENSE` and the complete `uninmr` tree from
NMRNetplusplus commit `3ba8b9c2f3d0a390d00685d238618345c6ab24d7`.
`nmrnet-source.sha256` records the SHA-256 of each of the 37 Git blobs, in Git
path order. The inventory was derived from that commit, not selected from the
materialized checkout. It includes the full software licence and all runtime
modules, including modules outside the narrow fold-0 execution path. The
repository source check compares declared, committed and materialized bytes
and rejects additional files, missing files, links and revision drift.

Primary source:
https://github.com/numpde/fork-of-NMRNetplusplus/tree/3ba8b9c2f3d0a390d00685d238618345c6ab24d7

The pinned software licence is MIT. Its copyright line says `Copyright (c) 2026`
without naming a holder; no author is inferred from that blank line. The complete
notice is retained as an authenticated blob and copied into the inference image.
The software licence does not establish an artifact-specific licence for public
datasets, trained checkpoints, scalers or logs.

Licence source:
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/LICENSE

The pinned README links the paper and Zenodo record 18232165, and identifies that
record as the source of pretrained weights, datasets and trained checkpoints.
`release.json` owns the exact public archive declarations. The immutable
`reference-cpu-float32.json` owns the selected member identities, archive parents
and historical preparation and qualification consumers. These consumers do not
claim to have trained or originally assembled the public files.

Release association:
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/README.md#L13-L16

Release metadata:
https://zenodo.org/api/records/18232165

Paper:
https://arxiv.org/abs/2601.18524v1

`containers/inference/image-inputs.txt` declares exactly ten authored image
inputs: the Dockerfile and its context policy, four build tools, two dependency
inputs and the two source-closure declarations. Upstream files enter the context
through their closure, rather than as a second independently selected inventory.
The wheelhouse is authenticated separately by its own receipt.

The source and artifact checks adapt the following pinned primary implementations:

- NMRPeak source and image-context ownership at
  https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_source.py
  and
  https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_image_inputs.py
- SECS per-file descriptions, origins, byte identities and archive parents at
  https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tests/input/normative/test_fixture_provenance.py

The target owns its own declarations. It does not inherit a dataset licence,
training producer identity or scientific accuracy claim from either precedent.

`source-attribution.json` supplies one independently authored description and
origin for each of the ten retained source artifacts. Its byte identities,
archive parents and consumer pointers borrow the reference's authority. All
file-specific authors are explicitly unknown (`null`), and artifact licences are
`NOASSERTION`: the retained evidence does not establish those facts. Generator
references explain the published roles and workflow, while explicitly leaving
the exact original producing revision unknown. The software MIT licence and the
paper's authors do not fill those gaps. On 2026-10-14, web-tool requests to the
exact Zenodo record, its API/export and its DOI/DataCite metadata were unavailable;
no inferred licence or file author was substituted. The pinned local README and
scripts, and https://arxiv.org/html/2601.18524v1, supply the associations cited.

`reference-source-qualification.json` separately records a retrospective source
inspection of the exact immutable image named by the retained numerical reference.
Inside that image, an offline, non-root, read-only, bounded standard-library audit
checked the complete `LICENSE`/`uninmr` inventory and every byte hash against the
committed-source manifest. Its observed 37 file hashes agree with the manifest.
The engine inspection independently identifies the actual image and its upstream
revision label. Source checks bind these observations to the unchanged reference
bytes, the declaration and manifest, both historical parent gitlinks and the
current pinned source closure.

The original reference and its preparation/execution receipts remain byte
immutable. The supplement does not claim a manifest existed during the original
execution, does not rerun a model, and does not recover the original build command.
It closes the historical image's source-byte gap by inspecting the retained image
itself. Qualification scripts, narrow container command, image inspection,
complete observed source hashes, stderr and zero exit status are retained under
`.work/qualification/source-closure-20261014/`; the public supplement records their
byte identities. Ordinary repository checks consume the reviewed public record
without requiring ignored local captures or access to a container engine.

`prediction-worker-cpu-float32.json` records the offline
`make prediction/worker-parity` qualification. Its four label-free requests come
from the same receipted validation records authenticated by
`reference-cpu-float32.json`; that packet's SHA-256 is retained. Source coordinates
are omitted because their artifact licence remains `NOASSERTION`. Request/result
hashes and fixture keys permit exact reproduction with the qualified release inputs.

Each request runs sequentially through direct prediction and a fresh spawned worker
using `SelectedReleaseModels`, `PredictionSession` and the bounded socket transport.
Complete results compare exactly, including all H/C predictions; the original 64
reference positions compare independently with zero tolerance. The record includes
selected artifact identities, immutable image ID, actual cgroup limits/peak and
mounted source hashes. The repository revision is the caller-reported parent HEAD;
source hashes identify the tested dirty files. This is process/socket parity for
these fixtures, not an ensemble-accuracy result, credential-isolation qualification
or proof of host-confirmed container retirement. The existing scaler-version warning
remains visible in the runtime log; exact agreement is scoped to this pinned runtime
and these fixtures rather than assumed portability across scikit-learn versions.
