# NMRNet++ for the NMR API

Per-atom ¹H/¹³C chemical-shift prediction from molecular 3D coordinates, using
the released NMRNet++ fold-0 models.

**[Scientific guide](https://numpde.github.io/nmrnet-repro/)** — inputs, units,
hydrogens, conformers, worked results, numerical evidence, limitations and sources.

[Run the provider](https://numpde.github.io/nmrnet-repro/provider.html) ·
[Reproduce calculations](https://numpde.github.io/nmrnet-repro/reproduction.html) ·
[Technical reference](https://numpde.github.io/nmrnet-repro/reference.html) ·
[Design records](https://numpde.github.io/nmrnet-repro/notes/)

Documentation is maintained directly as HTML in `docs/` and published by GitHub
Pages. Every maintained command that uses Docker accepts `DOCKER_CONTEXT=<name>`
on the Make command line. When omitted in a terminal, it offers a menu of
configured local Unix-socket contexts; unattended commands fail without a
selection. The selected socket stays fixed for the whole operation. Run
`make help` for maintained commands.
