# Scientific repository check ports

Inspected sources are NMRPeak `f8055ab1065795fd46f8c1539e894cf9773f5495`,
SECS parent `34f5915c1534278bbf61b4c6592afa44cf94ab08`, and its nested package
`0414b5f9d65e61a6ba00aa046e24b8e48ce230b1`. Target at inspection was
`ad7048cbb8f0dd1b4d273bfdf47ac58568d34f74`, with NMRNetplusplus gitlink
`3ba8b9c2f3d0a390d00685d238618345c6ab24d7`.

The initial port added checks only. Required declarations, source ownership and
producer facts remain failures where absent. No production source, dependency
lock, Make target or launcher is repaired by these ports.

## Assertion and method crosswalk

| Original source | Target check | Preserved obligations and adaptation |
| --- | --- | --- |
| NMRPeak `tests/repository/test_nmrpeak_source.py`, nine methods | `test_source_closure.py::SourceClosureDetectionTests` and `ScientificSourceRepositoryTests.test_declared_committed_and_live_source_are_identical` | Actual Git commits, exact declared/committed/live file sets and hashes; changed source, untracked file, extra materialized file, symlink, absolute root, FIFO, wrong revision and manifest drift rejection. Additional empty/duplicate/overlapping/unsafe roots, missing file and symlink ancestor fixtures. Target roots are exactly `NMRNetplusplus/{LICENSE,uninmr}`. |
| NMRPeak `repository_checks/nmrpeak_source.py`, `test_nmrpeak_runtime_sources.py` source methods | `science/source.py` and `test_source_closure.py` | Full declared revision and parent gitlink agreement; safe paths, regular Git entries, readable nonsymlink source inputs, complete live inventory. Target Uni-Core uses its versioned authenticated archive rather than a second submodule: `test_dependency_ownership.py` checks its closed source identity. |
| NMRPeak `test_nmrpeak_provider_image.py`, `nmrpeak_image_inputs.py` | `ScientificSourceRepositoryTests.test_committed_context_contains_exactly_the_declared_inference_inputs`, `SourceClosureDetectionTests.test_context_has_exact_committed_inventory_bytes_modes_and_identity` | Complete committed policy/build-tool/lock/closure inputs, safe sorted unique paths, upstream selected through its manifest, complete Git inventory, committed bytes, executable modes and SHA256 context identity. Empty or nonregular inputs are failures. The target image is inference, not an absent provider image. |
| NMRPeak `test_nmrpeak_product_contract.py` vocabulary method | `test_scientific_product_contract.py::ScientificProductContractTests.test_solvent_categories_unknown_none_and_missing_equal_pinned_source` | Read copied categories from actual pinned upstream AST and execute the authored selector against every category, unknown values, explicit None and missing key. Existing unit constants alone did not bind the copy to upstream. |
| Same original, exact manifest digest method | `ScientificSourceRepositoryTests.test_source_closure_identity_matches_the_qualified_reference` | Exact authenticated manifest identity remains required. The immutable historical reference has a separately qualified retrospective supplement binding its exact digest to the declared manifest and all 37 files observed in the exact historical image. This proves inspected image source closure, not original build-context capture. Strict observation/reference/schema mutations reject fabricated or drifted identity; see the reviewed foundation disposition below. |
| NMRPeak `nmrpeak_lock.py` | `test_dependency_ownership.py` and `science/dependency.py` | Nonempty bounded regular lock, UTF8/LF, canonical headerless producer representation, exact pins and URL hashes, normalized unique names, sorted packages and unique hashes, correct continuations, complete direct intent/version/source/hash equality, no unreviewed transitive URLs. Exactly one PyPI index belongs to `inference.in`; `build.in` cannot override it. An actual maintained `stage()` invocation with synthetic offline resolver/advisory ports proves ordered argv and exact output/report bindings. Its declared Torch URL becomes one exact CPU pin retaining uv's one identical artifact hash; unsupported generated shapes fail. Duplicate hashes, including Torch's historical repeated hash, remain rejected. See the source-specific disposition below; no historical producer attestation is inferred. |
| NMRPeak `nmrpeak_weights.py` declaration check | `ScientificArtifactRepositoryTests.test_public_release_and_all_archive_selections_are_closed_and_owned`, `FixtureProvenanceDetectionTests.test_release_loader_rejects_unknown_archive_and_root_fields` | Exact existing release/archive fields, public record/DOI/URL/size/checksum identity, nonempty archive and selection inventories, unique safe member names and positive budgets. Actual production parser must reject unknown authority, not just a separate checker. |
| NMRPeak `checkpoint_release.py` member identity checks | `ScientificArtifactRepositoryTests.test_retained_reference_is_bound_to_public_artifacts_and_original_producer_bytes` | Both H/C fold-0 models own checkpoint/scaler/infer-log/finetune-log identities and the exact public archive; input data/dictionary parents own exact archive/member/size/hash. Existing archive extractor retains bounded regular-member byte verification. No HF/CHF release schema is invented. |
| SECS `tests/input/normative/test_fixture_provenance.py` corpus/bytes/parent/generator methods | `test_fixture_provenance.py::FixtureProvenanceDetectionTests`, `science/artifact.py` | Execute the actual extractor on a small ZIP, require exactly one provenance record per selected/extracted file, hash/size equality, exact archive parent and producer input set/hash. The archive path is supplied independently: its actual ZIP bytes must match the public size/MD5 and receipt SHA256. Mutations cover extra/missing/changed files, symlinks, duplicate JSON fields, changed archive identity, a well-formed but wrong SHA256, changed ZIP bytes, selection drift, changed/missing/unknown producers. Fixture-only producer facts isolate inventory mutations; the separate actual-extractor check remains red until producer facts exist. |
| SECS `test_frontend_reference_lock.py`, `frontend_reference_lock.py` producer/oracle methods | Reference producer check and generator mutation in `test_fixture_provenance.py`; existing `tests/test_baseline_prepare.py::ExecutionIdentityTests` | Immutable image/full Git revision and actual producer-byte identity. Historical execution and preparation require the complete independently reviewed producer inventories from their original launcher commits (six run modules and two preparation modules); missing/extra mutations reject before Git byte verification. Historical reference producers are compared with their recorded Git revision; today's producer must not rewrite the snapshot's historical authority. Current producer changes invalidate current extraction fixture evidence. Existing tests retain raw source/record/atom alignment. |
| SECS nested `secs/.github/workflows/ci.yml`, `secs/pyproject.toml [tool.ruff]` | `test_scientific_lint.py` and `lint/ruff.toml` | Ruff exactly 0.15.15, all 25 selected rule prefixes and format checking, original eight ignores, original `scripts/*` print exception, unfixable T20/F841 and original directory exclusions. All authored trees enter the gate. Reviewed unittest/early-container-guard adaptations below preserve the locally owned obligations. |

## Explicit source inventory dispositions

NMRPeak `repository_checks/__init__.py` and `tests/repository/__init__.py` are
package markers. All four original repository test modules are accounted for
above. All fourteen original repository-check modules are accounted for:

- `nmrpeak_source.py`, `nmrpeak_image_inputs.py`, `nmrpeak_lock.py`,
  `nmrpeak_weights.py`, and applicable `checkpoint_release.py` assertions ported.
- `hf_release.py`, `chf_release.py`: lane identities for absent HF/CHF release
  products; shared member/source identity obligations adapted above.
- `checkpoint.py`, `hf_checkpoint.py`, `chf_checkpoint.py`: Docker checkpoint
  volume import/recovery/label/nonce/lifecycle ownership; target uses extracted
  directories, so no volume capability is created. Target archive and publication
  behavior remains covered by its existing artifact/wheelhouse unit tests.
- `deployment_topology.py`, `named_deployment.py`: absent provider/HF/CHF named
  Compose deployment. Applicable inference-container posture belongs to the
  container check port, not new provider objects.
- `nmr_api_projection.py`: absent provider API-contract projection. Exact Git
  inventory/bytes principles are covered by source/image checks.
- `__init__.py`: package marker, no assertion.

BART config, generated SMILES alphabet/symbol width and multiplicity product
assertions require capabilities NMRNet does not have. Numeric predictions and
solvent categories do not pretend to supply those capabilities.

SECS has no separate `repository_checks` suite. Every check/test lane was
inventoried: input normative/adversarial/scenario/adapter tests and support
evidence coverage; integration spectrum/Bruker/JCAMP/formula; published challenges;
qualification tools; provider/diagnostics; provider and deployment e2e; deployment
tests; reference-decoder spike; paid live interpreter/model behavior; contract,
client and public-support projection checks; nested package pytest and Ruff CI.

Absent SECS-specific capabilities retain explicit dispositions:

- `provider/support/check`, `check_support_catalog_boundary.py`,
  `project_support_catalog.py`, `test_support_catalog.py`, `support_coverage.py`,
  `support_evidence.py`: no support catalog, provider presentation owner or semantic
  claim inventory exists in target; no vacuous catalog/import/evidence gate added.
- `provider/contracts/check`, `provider/client/check`, `provider_contract.py`:
  no API provider/client projection exists.
- `test_build_candidate_index.py`, `test_qualify_candidate_index.py`: no FAISS,
  candidate sampling/qualification or Graph GA exists.
- Native NMRium archives, front-end locks and 10000-point reference vectors:
  absent; existing archive parent/producer provenance is adapted instead.
- Remaining parser/provider/deployment and eleven nested scientific test modules:
  domain runtime assertions, not generic repository-convention scanners. Their
  absence does not justify weakening an applicable port.
- Nested pytest `xfail_strict=true` has no target unittest equivalent or existing
  xfail behavior; no pytest dependency is added merely to mimic the setting.

## Required contained execution inputs

Mount read-only `tools/`, `repository_checks/`, `tests/`, `Makefile`, `artifacts/`,
`requirements/`, `containers/`, `NMRNetplusplus/`, and sanitized Git metadata plus
the two admitted object stores. Original host config, hooks, indexes, refs and
logs are not granted. Include `nmrnet_inference/` if present.
No `.work` checkpoint, downloaded archive, model import, network or engine access
is required. Temporary Git repositories and small extraction fixtures use the
container scratch directory only.

Git and Ruff 0.15.15 must exist in the maintained container. Missing Git fails
explicitly; fixture setup errors are dependency failures, not successful mutation
proofs. Missing Ruff fails both lint and format dependency gates. Tool installation
and image/dependency changes require a separate implementation discussion.

The initial maintained launcher did not grant parent `.git/` or the submodule
object database. Installing Git alone therefore cannot enable the committed-byte
and historical-producer assertions. A separately reviewed read-only Git database
grant remains an execution prerequisite; no source parser or ambient host Git
substitutes for those assertions.

Observed initial policy failures: missing `artifacts/nmrnet-source.paths`,
`artifacts/nmrnet-source.sha256`, `containers/inference/image-inputs.txt`; missing
reference source-closure identity; current lock header; production release/source
parsers accepting unknown fields; actual extraction receipts omitting exact
producer sources; potentially Ruff lint/format findings. These are deliberate
review results, never skip/xfail/empty-inventory exceptions.

## Reviewed foundation dispositions, 2026-10-14

Source closure now declares the complete pinned `LICENSE`/`uninmr` tree: 37 Git
blobs, exact committed modes and live hashes, plus ten authored image inputs.
The actual historical immutable image was inspected offline with metadata-only
standard-library code; its 37 source hashes match the new manifest. A strict
supplement records this retrospective observation while the original reference
keeps SHA-256 `8268206ad12003dea5ef57e1a7392de69325e110b54f2b91ee0f6314c4fef5ae`.
Both historical producer gitlinks and original producer bytes remain checked.
Precise scientific/artifact sources, unknown attribution and qualification limits
are in `artifacts/SOURCES.md`; no paper author or MIT software licence is silently
assigned to the released dataset/checkpoint files. Independent fresh contained
review passed 17 mutation proofs and five actual source/reference/attribution
gates. The authored image-input gate requires the reviewed committed revision.

Ruff PT009/PT027 prescribe pytest assertion/context spelling. Both local suites
use standard-library unittest, so those two preferences are inapplicable only
within the test trees; the rest of the PT rules remain selected. Ruff E402 treats
the mandatory early container guard as an effect before imports. Its authored-
tree exception is paired with an independent module import-order gate that
allows only the canonical first guard call before the import section. Later
effects, late guards and a second guard call cannot reopen that section. Thus
imports stay ordered without moving execution-capable imports ahead of admission.
No authored tree is excluded; lint/format violations remain failures until fixed.

## Headerless lock producer and independent index ownership

NMRPeak `repository_checks/nmrpeak_lock.py:138-146` requires its own generated
header to name the stage command and index:
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_lock.py#L138-L146

The local producer deliberately invokes uv with `--no-header`,
`--no-emit-index-url` and `--no-emit-find-links`. Requiring NMRPeak's representation
would reject correctly generated local locks. The local parser therefore admits
only headerless requirements and generated indented comments, rejecting top-level
headers and resolver options. Index authority is checked independently: exactly
one `--index-url https://pypi.org/simple` in `inference.in`, with no resolver option
in `build.in`. Every pin, hash, ordering, continuation, byte-budget, direct-input
and advisory-identity obligation remains enforced.

`PackageLockStageTests` executes today's actual producer with explicitly synthetic
offline resolver and advisory ports. It checks the complete ordered uv invocation,
exact emitted lock bytes and actual input/output report hashes. Its Torch fixture
has one declared CPU wheel URL and one generated hash; normalization changes only
the URL line to the matching CPU pin. The URL fragment and emitted hash must agree
with the independently read declaration and CPython/Linux ABI. Only `%2B` versus
`+` within the filename may vary. Additional, missing, mismatched or duplicated
Torch/hash output fails before advisory calls or publication; no broad hash
deduplication occurs. The strict parser separately rejects duplicate Torch hashes.
Synthetic fixture text does not reconstruct or authenticate discarded historical
uv output. Historical lock/report/image bytes and current operational adoption
remain separate from this producer-contract qualification.
