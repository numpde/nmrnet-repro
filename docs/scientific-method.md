# Scientific method and evidence

This guide describes the selected NMRNet++ calculation, the meaning of its inputs
and outputs, and the evidence available in this repository. Start with the
[README](../README.md) for the short version. Commands and environment preparation
are in the [reproduction reference](reproduction.md).

## The calculation

NMRNet++ extends a 3D neural predictor with semi-supervised training: atom-assigned
examples teach individual shifts, while unassigned literature spectra teach
matching between predicted and observed shift sets. The published architecture
uses an SE(3)-equivariant Transformer with an atom-level regression head. This
repository loads released weights; it does not retrain them. The submission
interpreter is a separate language model that selects preparation operations.
Sources: [paper v1, Sections 3.2–3.3](https://arxiv.org/html/2601.18524v1#S3.SS2),
[released-weight loader](../nmrnet_inference/release.py), and
[input interpreter](../nmrnet_provider/interpreter.py).

The scientific input is an ordered list of element symbols and three-dimensional
coordinates. The selected released H and C models predict chemical shifts in ppm
for the corresponding atoms. Other admitted elements remain part of the geometric
context. The calculation does not take bond orders, charge, isotope, solvent or
temperature as separate prediction features. The exact request fields are
`coordinate_unit`, `atoms` and `coordinates`.
Sources: [request admission and prediction](../nmrnet_inference/predict.py),
[model tensor construction](../nmrnet_inference/model.py), and
[result contract, `SCIENTIFIC_LIMITS` and `GENERATED_LIMITS`](../nmrnet_provider/result.py).

The local selection uses one released fold-0 checkpoint and its paired scaler
for each of H and C. H/C heads run sequentially when present, and the returned
predictions are sorted by the zero-based atom index within that geometry. Inverse scaling
returns the release's ppm convention. A failed required head does not produce a
partial scientific result.
Sources: [exact checkpoint/scaler/archive members and SHA-256 identities](../artifacts/prediction-models.json),
[`SelectedReleaseModels` and release admission](../nmrnet_inference/release.py),
[`predict_request` and `_prediction_result`](../nmrnet_inference/predict.py), and
[upstream inverse scaling, pinned revision, lines 75–76](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L75-L76).

One request admits at most 128 atoms; a batch admits at most 128 geometries.
Byte, result-size and runtime limits may constrain a batch before those count
limits. They are software bounds, not a qualified chemical applicability domain.
Sources: [`MAX_ATOMS`, `MAX_GEOMETRIES`, `MAX_INPUT_BYTES`](../nmrnet_inference/predict.py)
and [`MAX_RESULT_BYTES` and `ResultPlan`](../nmrnet_provider/result.py).

## Applicability and experimental comparison

Software admission checks element tokens and finite coordinates; it does not
validate that a molecule lies within the model's training distribution. Every
atom must have a dedicated token in the pinned release dictionary. Its element
tokens are C, N, O, S, H, Cl, F, Br, I, Si, P, B, Na, K, Al, Ca, Sn, As, Mg, Fe,
Zn, Cr, Se, Gd, Au and Li; only H/C have selected prediction heads here.
Token inventory: [reference packet, `dictionary.symbols_before_task`](../artifacts/reference-cpu-float32.json);
selected heads: [model declaration, `elements`](../artifacts/prediction-models.json).
In particular,
a successful parse or a 128-atom capacity test is not an accuracy qualification
for ions, radicals, metals, unusual bonding or a particular stereoisomer.
Sources: [`require_dictionary_atoms`](../nmrnet_inference/model.py),
[request admission](../nmrnet_inference/predict.py), and
[bounded qualification scope](../artifacts/prediction-cpu-float32.json).

The paper describes filtered molecular datasets, including removal of radicals
and isotope-labelled entries, rather than universal chemical coverage. The
local coordinate-only interface does not reproduce those training-data filters
as a chemical validity assessment.
Sources: [paper v1, Section 3.1 and Appendix A](https://arxiv.org/html/2601.18524v1#S3.SS1)
and [local request fields](../nmrnet_inference/predict.py).

The selected interface has no solvent, temperature or reference-standard
parameter and performs no additional experimental-reference calibration. Ppm
denotes the released target scale after inverse scaling; it does not establish
that your experimental conditions match the data. The paper's solvent-conditioned
experiments are a separate model configuration, not a capability implied by
these selected H/C checkpoints.
Sources: [request fields](../nmrnet_inference/predict.py),
[selected checkpoint names](../artifacts/prediction-models.json),
[scaler implementation](../nmrnet_inference/model.py), and
[paper v1, Sections 3.4 and 4.3](https://arxiv.org/html/2601.18524v1#S3.SS4).

Supplied coordinates are not optimized or repaired by preparation. Generated
coordinates are estimates from the procedure below. The retained comparisons
do not quantify the prediction error due to an inaccurate geometry or the local
generator's difference from the paper's optimization procedure. Preserve that
distinction when comparing a predicted shift with experiment.
Sources: [coordinate preparation](../nmrnet_provider/input_geometry.py),
[generator](../nmrnet_provider/smiles.py), and
[qualification limits](../artifacts/prediction-cpu-float32.json).

## Supplied coordinates and units

Supplied structures preserve their atom order and geometry. Units come from
explicit instructions or a supported format convention; preparation records the
selected unit and converts it into angstroms. Prediction then admits finite
float32 coordinates. Model tensor construction subsequently centers and pads the
input. The result retains both preparation and the numeric input used for
prediction, so source decimals and effective float32 coordinates can be compared.
Sources: [`PreparedGeometry`, `prepare_rows`, `coordinate_conversion`](../nmrnet_provider/input_geometry.py),
[`_float32_coordinate`](../nmrnet_inference/predict.py),
[tensor construction](../nmrnet_inference/model.py), and
[result construction](../nmrnet_provider/result.py).

| Declared source unit | Conversion to angstrom | Authority |
| --- | --- | --- |
| `angstrom` | identity | Local input contract |
| `nm` | multiply by 10, exact unit factor | [NIST SP 811, Appendix B.8, angstrom entry](https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b8) |
| `bohr` | multiply by 0.529177210544 | [CODATA 2022, Table XXXIII, p. 033105-45, Bohr radius](https://physics.nist.gov/cuu/pdf/JPCRD2022CODATA.pdf#page=45) |

The bohr factor is a recommended central value, with standard uncertainty
8.2 × 10⁻¹¹ angstrom per bohr. It is not an exact definition. An exact nm unit
factor also does not imply exact floating-point decimal conversion. The
[authored conversion fixtures](../tests/fixtures/preparation/README.txt) derive
the allowed binary64 rounding error before execution; the
[implementation](../nmrnet_provider/input_geometry.py) records the factor and reference.

The model's angstrom convention is a repository decision supported by upstream
RDKit preprocessing and bond-distance comparisons for two retained molecules,
CH3Cl and CH2Cl2. The exact original producer of the released coordinates remains
unknown. This bounded evidence does not recover the provenance of every release
geometry. Sources: [`coordinate_unit_decision`](../artifacts/prediction-cpu-float32.json)
and [note 002, unit qualification](../notes/002_label_free_coordinate_prediction_20261014.txt).

## Hydrogens: preserve supplied structures, expand only generated ones

For supplied coordinates, the software neither infers nor adds missing H atoms.
It reports the supplied and predicted H counts, while preserving genuinely
H-free inputs. Coordinates alone cannot establish whether hydrogens were omitted,
partially supplied, or intentionally absent. Missing atoms can also change the
predictions for atoms that are present because they change the model's context.
Sources: [`SCIENTIFIC_LIMITS` and hydrogen coverage construction](../nmrnet_provider/result.py)
and [supplied-geometry preparation](../nmrnet_provider/input_geometry.py).

| Input situation | Scientific interpretation |
| --- | --- |
| All intended H coordinates supplied | Shifts are returned for those H atoms and supplied C atoms. |
| Some H coordinates supplied | Only those H atoms receive shifts; this does not establish complete proton coverage. |
| No H coordinates supplied | No proton positions or proton shifts are invented; absence is not proof that the molecule is H-free. |
| Genuinely H-free structure | It remains admissible if the other input/model checks pass. |
| SMILES plus an explicit conformer count | A separate generation step expands the graph with H and generates their coordinates. |

The supplied-structure rows follow the
[result limits and coverage fields](../nmrnet_provider/result.py); the SMILES row
follows [`_embed` and its recorded hydrogen expansion](../nmrnet_provider/smiles.py).
To obtain complete proton coverage from an incomplete supplied structure, supply
the intended H coordinates or explicitly request a separate generation step.
Preserving an incomplete geometry is not evidence that its shifts are accurate.

## Generated conformers

The SMILES route uses RDKit's ETKDGv3 embedding with recorded seed, iteration and
timeout settings. Its parser preserves explicit H atoms; graph expansion adds
the remaining hydrogens. The evidence records original atoms/bonds, added-H
parent mapping, actual conformer IDs, effective embedding parameters, RDKit
version and coordinate-derived stereochemistry. Charge, isotope and graph
stereochemistry remain provenance rather than additional model input fields.
Source: [`generate_conformers`, `_embed`, `_conformer`](../nmrnet_provider/smiles.py).

The conformers are the returned embeddings in their recorded order, not an
energy-ranked or population-weighted ensemble. RMS pruning is disabled
(`pruneRmsThresh = -1.0`). Original graph-atom indices and added-H parent indices
identify the expanded graph across conformers. For supplied Upload records,
interpret an atom index within its geometry together with its retained source
record and source-position mapping; it is not a global index across files.
Sources: [`embedding_settings` and `_embed`](../nmrnet_provider/smiles.py),
[`_planned_geometry` and `_planned_generated`](../nmrnet_provider/result.py).

Each conformer receives its own ordered predictions. The implementation performs
neither conformer averaging nor Boltzmann weighting, and does not guarantee
that generated conformers are distinct. Incomplete generation fails the requested
collection rather than submitting the available subset. Generated coordinates
and their stereo annotations are not independent experimental validation.
Sources: [`ConformerShortfall`](../nmrnet_provider/smiles.py),
[`GENERATED_LIMITS` and generated-result construction](../nmrnet_provider/result.py),
and [generation fixtures and qualification](../tests/fixtures/preparation/README.txt).

This local generator should not be described as the paper's exact coordinate
production procedure: the paper reports RDKit embedding followed by MMFF
optimization, while the local `_embed` path calls `EmbedMultipleConfs` and does
not apply that optimization.
Sources: [paper v1, Appendix A, “3D conformation generation”](https://arxiv.org/html/2601.18524v1#A1)
and [local generator](../nmrnet_provider/smiles.py).

## Input interpretation and format coverage

The provider separates interpretation of the user's instruction from the
deterministic readers and scientific model. For pasted input, the interpreter
passes coordinate text, format (`table` or `xyz`) and source unit to
`prepare_pasted`. Recorded text positions refer to that received text, not to
byte offsets in the original Job. A successful preparation selects its geometry
without another confirmation exchange. SMILES generation and verified Upload
selection use their separate tools.
Sources: [interpreter conversation and tools](../nmrnet_provider/interpreter.py),
[preparation and retirement orchestration](../nmrnet_provider/input_preparation.py),
[pasted source binding](../nmrnet_provider/submission.py),
[tool definitions and terminal selection](../nmrnet_provider/interpreter.py), and
[fixture rationale](../tests/fixtures/preparation/README.txt).

Pasted input accepts one geometry; multi-record XYZ/SDF input uses Upload
selection with explicit order. Selected records are evaluated separately;
molecular or stereochemical equivalence between records is not checked.
Reader support includes coordinate tables, XYZ,
V2000 MOL/SDF, PDB and MOL2, with format-specific constraints. Supporting a
reader does not establish a complete real-interpreter/API run for that format.
The [fixture reference](../tests/fixtures/preparation/README.txt) identifies
authored cases and expected atom/record mappings; the
[preparation reference](../requirements/preparation.md) identifies format
authorities, native-parser restrictions and exact runtime evidence.

## Reading a result: chloromethane

The authored chloromethane fixture supplies these rows in **nm**, in this order:

```text
H   0.1027   0       -0.0363
Cl  0        0        0.178
C   0        0        0
H  -0.0514   0.089   -0.0363
H  -0.0514  -0.089   -0.0363
```

This is an authored preservation/conversion example, not an experimental
structure. The actual API run retained the following predictions, unchanged
from the earlier pinned scientific receipt:

| Atom index | Element | Recorded shift (ppm) |
| ---: | --- | ---: |
| 0 | H | 3.062568426132202 |
| 2 | C | 32.309967041015625 |
| 3 | H | 3.0625784397125244 |
| 4 | H | 3.0625784397125244 |

Sources: [exact supplied text, format and unit](../tests/fixtures/preparation/api_pasted.json),
[authored fixture provenance](../tests/fixtures/preparation/README.txt), and
[note 003, R21, Job `job:f4b3e820d658200c22cebcef9d2e2c60bee36e4c5edc7a05868d1465857b8c3c`](../notes/003_nmr_api_provider_20261018.txt).

Cl at index 1 remains in the geometry but receives no shift. The displayed digits
identify the recorded numerical outputs; they do not express experimental
precision. Per-atom H entries are not grouped into equivalent resonances, and
the predictor does not compute rotational/exchange averaging or a spectrum.
Sources: [`_prediction_result`](../nmrnet_inference/predict.py),
[scientific result limits](../nmrnet_provider/result.py).

To inspect the existing numbers, follow the retained receipt; to repeat the
bounded release reference, use the
[reference command](reproduction.md#run-the-reference). A standalone prediction
CLI for arbitrary geometries remains deferred; API qualification creates persistent
Jobs and is a separate explicit operation described in the
[provider reference](provider.md).

## What the retained numerical evidence establishes

| Evidence | Observed comparison | Interpretation |
| --- | --- | --- |
| [CPU reference](../artifacts/reference-cpu-float32.json), `elements`, `tolerances`, `runtime` | Two intact release records per selected head; 42 H and 22 C reference positions; repeated predictions exact at `atol=rtol=0` | Bounded fold-0 repeatability in the recorded CPU runtime |
| [Callable qualification](../artifacts/prediction-cpu-float32.json), `qualification.run_outcomes` | 32 tensor comparisons; all 64 historical positions exact; 137 total outputs (82 H, 55 C) on four retained fixtures | Adapter correspondence plus full H/C output coverage on those fixtures |
| [Worker qualification](../artifacts/prediction-worker-cpu-float32.json) | Direct and worker results agree on the retained fixtures | Correspondence across the process/socket boundary |
| [Capacity observation](../artifacts/prediction-cpu-float32.json), `qualification.run_outcomes.real_model_callable.real_model_capacity_atoms_per_head` | Real H and C execution at the 128-atom boundary | Execution capacity for the tested construction, not an accuracy domain |
| [Actual API transaction, note 003 R21](../notes/003_nmr_api_provider_20261018.txt) | One chloromethane Job, exact prior-result comparison, authenticated publication/readback and retirement | Delivery of a repeatable calculation using scripted interpretation |

The historical 64 positions are the release's selected reference positions;
137 counts every H/C output across the four complete fixture geometries. Neither
count is a test-set accuracy score. The relevant manifests retain request,
checkpoint/scaler, source, image and runtime identities. Original evidence remains
unchanged when later implementation work adds a new proof.
Sources: [reference packet](../artifacts/reference-cpu-float32.json),
[complete fixture observation](../artifacts/prediction-fixture-cpu-float32.json),
and [callable qualification packet](../artifacts/prediction-cpu-float32.json).

The reference uses Python 3.12.12, CPU float32 and two computation threads.
Its original label-roundtrip allowance is 0.0001 ppm; repeat tolerances are zero.
The released scalers were serialized under scikit-learn 1.5.2 and loaded under
1.8.0 with a retained version warning and separately checked affine arithmetic.
Those observations do not establish cross-platform/GPU equality or remove the
warning. Sources: [reference `runtime` and `tolerances`](../artifacts/reference-cpu-float32.json),
[dependency/scaler review](../requirements/README.md), and
[note 001](../notes/001_contained_upstream_baseline_20261013.txt).

Real-interpreter qualification across the agreed routes, repeated service use
and GUI delivery remain separate work tracked in
[note 003](../notes/003_nmr_api_provider_20261018.txt). A copied endpoint
configuration, installed image or passing software suite does not establish
those scientific or delivery observations.

## Published accuracy and the five-checkpoint ensemble

MAE is the mean absolute prediction error; RMSE is the square root of the mean
squared error and gives more weight to large errors. Atom-level evaluation keeps
the assigned atom correspondence. Sorted molecule-level evaluation compares
within-molecule shift collections and cannot by itself establish that a predicted
value belongs to the correct atom.
Sources: [pinned `reg_metrics`](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/get_result.py#L28-L33)
and [sorted molecule evaluation](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/get_result_unlabel.py#L97-L103).

The published NMRNet + Semi-supervised results below describe benchmark
evaluation. They are not measured errors of this repository's local fold-0
reference or uncertainty bounds for an individual prediction.
Source: [Jin et al., arXiv:2601.18524v1, Table 2](https://arxiv.org/html/2601.18524v1).

| Evaluation | ¹H MAE (ppm) | ¹H RMSE (ppm) | ¹³C MAE (ppm) | ¹³C RMSE (ppm) |
| --- | ---: | ---: | ---: | ---: |
| NMRShiftDB2, atom-level | 0.1709 | 0.4337 | 0.9270 | 1.9128 |
| NMRShiftDB2, molecule-level | 0.1492 | 0.3620 | 0.7765 | 1.5629 |
| ShiftDB-Lit, molecule-level | 0.0559 | 0.1846 | 0.5060 | 2.3494 |

The released logs separately report fold-0 atom-level MAEs of 0.1842 ppm for H
and 0.9876 ppm for C. These are upstream benchmark observations for that fold,
not errors measured by this repository's four-fixture reproducibility checks.
Source: [note 001, “Published accuracy and ensemble averaging”, exact H/C
`result.log` archive members and SHA-256 hashes](../notes/001_contained_upstream_baseline_20261013.txt).

The released evaluation script runs folds 0–4, averages their prediction vectors,
then computes errors. It does not average five error scores. Molecule-level
evaluation additionally sorts predictions and targets within each molecule,
so it measures a different correspondence from assigned atom-level error.
The averaging attribution comes from pinned code and released logs; the paper's
methods do not explicitly spell it out.
Sources: [upstream `infer.sh`, lines 11–28](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/script/infer.sh#L11-L28),
[`get_result.py`, lines 80–84](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/get_result.py#L80-L84),
[`get_result_unlabel.py`, lines 97–103](https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/get_result_unlabel.py#L97-L103), and
[note 001, “Published accuracy and ensemble averaging”, exact log members and hashes](../notes/001_contained_upstream_baseline_20261013.txt).

## Sources and provenance

- Yongqi Jin, Yecheng Wang, Jun-Jie Wang, Rong Zhu, Guolin Ke and Weinan E,
  [*From Human Labels to Literature: Semi-Supervised Learning of NMR Chemical Shifts at Scale*](https://arxiv.org/abs/2601.18524v1),
  arXiv:2601.18524v1, 26 January 2026. Relevant locations: Section 3.3
  (architecture), Section 4.1 (experimental setup), Table 2 (benchmark errors),
  Table 6 (ppm convention), Appendix A (coordinate generation).
- [Zenodo record 18232165](https://zenodo.org/records/18232165),
  DOI `10.5281/zenodo.18232165`. This repository's
  [release declaration](../artifacts/release.json) and
  [selected-model manifest](../artifacts/prediction-models.json) identify archive
  URLs, members, sizes and hashes; names alone are not artifact identity.
- Scientific source: [pinned NMRNet++ revision `3ba8b9c2f3d0a390d00685d238618345c6ab24d7`](https://github.com/numpde/fork-of-NMRNetplusplus/tree/3ba8b9c2f3d0a390d00685d238618345c6ab24d7).
  [Source closure](../artifacts/nmrnet-source.sha256) and
  [source qualification](../artifacts/reference-source-qualification.json)
  identify the retained software bytes and the historical image inspection.
- Unit references: [NIST SP 811, Appendix B.8, angstrom conversion entries](https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b8);
  Peter J. Mohr, David B. Newell, Barry N. Taylor and Eite Tiesinga,
  [*CODATA recommended values of the fundamental physical constants: 2022*](https://physics.nist.gov/cuu/pdf/JPCRD2022CODATA.pdf#page=45),
  *J. Phys. Chem. Ref. Data* **54**, 033105 (2025), DOI `10.1063/5.0279860`,
  Table XXXIII, p. 033105-45, Bohr radius.
- [Artifact attribution](../artifacts/source-attribution.json) and
  [its evidence record](../artifacts/SOURCES.md) distinguish release association
  from original producer identity. The pinned software is MIT-licensed;
  file-specific dataset/checkpoint/scaler licences remain `NOASSERTION`, and
  their exact original producers are not inferred from the paper's authors.

