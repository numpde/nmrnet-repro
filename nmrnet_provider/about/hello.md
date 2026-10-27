**¹H and ¹³C chemical-shift prediction**

Predicts per-atom shifts (ppm) using released NMRNet++ models.

**Provide one:**

- Paste element symbols and 3D coordinates for one geometry.
- Attach a 3D structure file and specify which records to use.
- Supply SMILES with the intended protonation state and request 1–128 conformers.

For supplied coordinates, state units: angstrom, nm or bohr. Supplied geometries are not optimized; missing hydrogens are not added and can affect other shifts. SMILES preparation adds hydrogens and generates 3D coordinates.

**Results:** shifts linked to atoms in each geometry; conformers are not averaged. Limits: 128 geometries per request; 128 atoms per geometry, including hydrogens. Solvent and temperature are not model inputs.

**Detail:** `verbosity: low` returns shifts and atom correspondence; `medium` (default) adds coordinates; `max` adds the software source manifest.

[Scientific guide](https://numpde.github.io/nmrnet-repro/)
