# NMRNet++: reproducible H/C chemical-shift prediction

This repository adapts released **NMRNet++ models to predict per-atom ¹H and
¹³C chemical shifts from molecular elements and 3D coordinates**, with results
in ppm and traceable atom identities. It also develops a provider for the NMR API's
`peaks_from_mol` analysis. The selected calculation uses CPU float32 and one
fold-0 checkpoint per element. [Model and scaler identities](artifacts/prediction-models.json),
[prediction contract](nmrnet_inference/predict.py), [provider result contract](nmrnet_provider/result.py).

The scientific basis is [Jin et al., *From Human Labels to Literature:
Semi-Supervised Learning of NMR Chemical Shifts at Scale*, arXiv:2601.18524v1](https://arxiv.org/abs/2601.18524v1).
The [scientific guide](docs/scientific-method.md) connects the paper, pinned
implementation, released artifacts and local numerical evidence.

The published method learns from both atom-assigned spectra and larger collections
of literature spectra without atom assignments. Its 3D neural network predicts
atom shifts. A separate language model interprets provider submissions and selects
input preparation; the numerical predictor consumes structured coordinates.
[Paper v1, Sections 3.2–3.3](https://arxiv.org/html/2601.18524v1#S3.SS2),
[interpreter tools](nmrnet_provider/interpreter.py), [released-model execution](nmrnet_inference/release.py).

## What goes into a prediction

| Input | Treatment |
| --- | --- |
| Supplied 3D geometry | Preserve the atoms, their order and geometry; convert explicitly declared units to angstroms. |
| SMILES and a requested conformer count | Generate an explicit collection of 3D conformers; record graph expansion, H-parent mapping and embedding settings. |
| Uploaded structures with instructions | Verify the source bytes, select records in the requested order and preserve their atom correspondence. |

These routes are implemented and have contained integration evidence; the
complete real-interpreter/API/GUI workflow remains in development.
[Preparation fixtures and evidence](tests/fixtures/preparation/README.txt),
[provider qualification record, note 003](notes/003_nmr_api_provider_20261018.txt).

**Hydrogens matter.** Supplied coordinates are never silently completed: only
supplied H atoms receive proton shifts. A geometry with no H coordinates does
not establish that its molecule is H-free, and omitted atoms can also affect
shifts of atoms that remain. SMILES generation is a separate, explicit step
that expands hydrogens and generates their positions.
[Hydrogen behavior and its limits](docs/scientific-method.md#hydrogens-preserve-supplied-structures-expand-only-generated-ones),
[source result limits](nmrnet_provider/result.py), [SMILES generator](nmrnet_provider/smiles.py).

Units come from explicit instructions or a supported format convention and are
recorded before conversion. Preparation accepts `angstrom`, `nm` and `bohr`;
the scientific request uses angstroms. Each conformer receives its own
prediction. This implementation performs no checkpoint averaging, conformer
averaging or Boltzmann weighting.
[Units and conversion evidence](docs/scientific-method.md#supplied-coordinates-and-units),
[conformer method](docs/scientific-method.md#generated-conformers).

## What the results mean

Results associate each shift with a zero-based index within its supplied or
generated geometry, retaining source-atom or added-H parent correspondence.
H and C receive predictions; other admitted atoms supply geometric context.
Prepared coordinates, effective float32 inputs, source mapping, selected model
identities and runtime evidence make the calculation inspectable. The predictor
returns no couplings, multiplicities, intensities or confidence intervals, and
does not assign measured peaks or infer bonds.
[Result schema and scientific limits](nmrnet_provider/result.py),
[method and input features](docs/scientific-method.md#the-calculation).

For example, the recorded chloromethane calculation returns one C shift and
three atom-indexed H shifts. Those entries are not a prediction of three resolved
proton resonances: the implementation does not group equivalent atoms or calculate
exchange averaging. [Worked result and exact retained values](docs/scientific-method.md#reading-a-result-chloromethane).

**Repeatability is established on a bounded reference; general chemical accuracy
has not been measured here.** Four retained release fixtures give 137 H/C outputs
(82 H, 55 C); all 64 historical reference positions match exactly in the recorded
CPU runtime. The original reference used two records per element and repeated
each prediction with zero absolute and relative tolerance.
[Reference, `elements` and `tolerances`](artifacts/reference-cpu-float32.json),
[callable qualification, `qualification.run_outcomes`](artifacts/prediction-cpu-float32.json).

The same qualification packet records a separate real-model 128-atom capacity
test. That establishes a software execution bound, not validated accuracy for
all molecules up to that size.

The paper's reported benchmark accuracy uses a five-checkpoint ensemble. For
example, its NMRShiftDB2 atom-level MAEs are 0.1709 ppm for ¹H and 0.9270 ppm
for ¹³C. Those are published benchmark results, not measured errors of this
repository's fold-0 calculation or uncertainty estimates for your molecule.
[Paper v1, Table 2](https://arxiv.org/html/2601.18524v1),
[ensemble implementation, metrics and released-log evidence](docs/scientific-method.md#published-accuracy-and-the-five-checkpoint-ensemble).

## Evidence and current scope

| Question | Evidence and limit |
| --- | --- |
| Do the selected models reproduce the retained numbers? | [CPU reference](artifacts/reference-cpu-float32.json) and [callable qualification](artifacts/prediction-cpu-float32.json): exact comparisons in the recorded runtime. |
| Does the isolated worker preserve those predictions? | [Worker qualification](artifacts/prediction-worker-cpu-float32.json): correspondence on the retained fixtures. |
| Has a prediction gone through the actual API? | [Note 003, R21](notes/003_nmr_api_provider_20261018.txt): one chloromethane Job, exact result readback and confirmed retirement, with scripted interpretation. |
| Is the complete offering qualified? | [Open work in note 003](notes/003_nmr_api_provider_20261018.txt): real-interpreter coverage, repeated service use and GUI delivery require their own evidence. |

These observations establish different things. A successful API transaction
does not measure chemical accuracy; exact reproduction on four fixtures does
not qualify every molecule, solvent, platform or generated conformer.
The [scientific guide](docs/scientific-method.md#what-the-retained-numerical-evidence-establishes)
details the comparisons, units, scaler warning and remaining limits.

## Explore or reproduce

- [Scientific method and evidence](docs/scientific-method.md): calculation,
  geometry, hydrogens, conformers, published accuracy and precise sources.
- [Reproduce the numerical reference](docs/reproduction.md#run-the-reference):
  fixed input selection, declared tolerances, commands and retained evidence.
- [Input fixtures](tests/fixtures/preparation/README.txt): authored geometries,
  formats, atom mappings and independent conversion expectations.
- [Provider operation and diagnostics](docs/provider.md): configuration,
  explicit launch parameters, API qualification and protected failure evidence.
- [Environment and acquisition reference](docs/reproduction.md#prerequisites):
  preparation, software checks and dependency updates. `make help` lists commands.

The [pinned upstream software](https://github.com/numpde/fork-of-NMRNetplusplus/tree/3ba8b9c2f3d0a390d00685d238618345c6ab24d7)
is MIT-licensed. Artifact-specific licences and original producers are recorded
separately; the software licence is not applied by inference to datasets or
trained weights. [Source and attribution record](artifacts/SOURCES.md),
[artifact attribution](artifacts/source-attribution.json).
