# NMRNet++ for the NMR API

Released NMRNet++ models adapted for per-atom ¹H/¹³C chemical-shift prediction
from molecular elements and 3D coordinates. The selected calculation uses one
fold-0 checkpoint per element. [Exact model/scaler identities](artifacts/prediction-models.json).

**[Scientific guide](https://numpde.github.io/nmrnet-repro/)** — calculation,
applicability, geometry and units, hydrogen handling, conformers, worked results,
numerical evidence, published accuracy, and precise sources.

Open the guide from the provider description in the API/GUI.
The page is maintained in [docs/index.html](docs/index.html) and published through
GitHub Pages from `main` → `/docs`.

The original research is [Jin et al., *From Human Labels to Literature:
Semi-Supervised Learning of NMR Chemical Shifts at Scale*, arXiv:2601.18524v1](https://arxiv.org/abs/2601.18524v1).
The guide distinguishes its published five-checkpoint ensemble results from
this repository's bounded fold-0 reproduction evidence.

- [Reproduce the numerical evidence](docs/reproduction.md#run-the-reference).
- [Provider operation and diagnostics](docs/provider.md).
- [Input fixtures and independent expectations](tests/fixtures/preparation/README.txt).
- [Design decisions and experiment records](notes/003_nmr_api_provider_20261018.txt).
- [Source attribution and licence evidence](artifacts/SOURCES.md).

Run `make help` for maintained commands. Environment preparation and dependency
updates are in the [reproduction reference](docs/reproduction.md).
