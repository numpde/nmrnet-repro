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

## Shared deployment identity probe (candidate)

This feature branch delegates the deployment helper's UID/GID probe to the
host `linux-idmap-probe` CLI supplied by `numpde/container-tools`. It requires
the unreleased **0.3.2.dev0** candidate in the host PATH until the next qualified
release is available. Existing selection of the exact Docker Unix endpoint,
provider interpreter and helper policy is unchanged. Its former 64 MiB
memory/swap, eight-PID and 0.25-CPU limits are supplied to the shared probe.

A probe failure or uncertain retirement must not authorize input staging or
follow-up Docker helper creation. The shared library owns its own transient
probe and returns projected IDs only after confirmed retirement. The provider
retains input ownership and cleanup of its configuration helpers. The pinned
acquisition bootstrap remains a separate already-qualified dependency.

Test the shell adapter with the maintained container-launched deployment suite
and the shared candidate's CLI/real Docker tests before deploying this branch.
