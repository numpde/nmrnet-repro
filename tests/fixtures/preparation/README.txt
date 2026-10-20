Fixtures for reading 3D structures and generating conformers from SMILES
======================================================================
Authored 2026-10-19 for 003 steps 2a-2f and note 016's preparation tests.

Purpose and authority
---------------------
These are original repository-authored parsing examples and independent expected
values, not experimental structures, optimized geometries, NMR predictions or
RDKit exports. Coordinates, atom ordering and the simple single-bond graphs were
chosen explicitly. Consulted format specifications are listed below; no molecular
file was copied from them. Existing small_chloromethanes.json/.txt and their
archive/NIST provenance remain unchanged.

expected.json records literal intended atom order, coordinates and source IDs.
Those values must not be regenerated from a reader under test. Native file
expectations are candidate tests, not an offering allowlist or evidence that the
isolated RDKit worker exists or accepts these formats. In particular, V3000 remains
subject to the unresolved native-parser assessment in requirements/preparation.md;
this ordinary example is not a security reproducer or a fix.

All source coordinates in the native fixtures are explicitly declared angstrom
for these test scenarios. Units must be supplied by instructions or an unambiguous
documented convention and recorded by preparation, never inferred from bond size.
The nm/bohr examples separately exercise explicit conversion.

Corpus and independent expectations
------------------------------------
normative/atom_order.{xyz,pdb,mol2} and atom_order_v{2000,3000}.mol:
  The same synthetic chloromethane, ordered H, Cl, C, H, H. Coordinates have only
  three decimal places and remain exactly those listed in expected.json; there is
  no format-rounding allowance or chemical-accuracy assertion. V2000/MOL2 atom
  IDs are 1..5; V3000 IDs are 11,22,33,44,55; PDB serials are 40,7,22,5,91 in file
  order. Preserve that order and explicit H, not canonical atom or serial order.

normative/mixed_records.{xyz,sdf,mol2}:
  Three records: anchor, planar water, repeated anchor. Select all, a later record,
  or (3,1); retain original record identity/order and both repeated records.
  These are deliberately different molecules and repeated geometry, not three
  distinct conformers or a certified ensemble. No molecular-equivalence check.
  SDF includes data fields, which are not coordinate rows.

normative/butane_conformers.{xyz,sdf}:
  Two supplied four-carbon single-bond chain conformers, with different internal
  torsion. The first is planar and the second has nonzero Z. Preserve both original
  geometries and four-atom order without adding absent H. These authored examples
  are neither optimized nor experimental, and are separate from generated SMILES
  references. expected.json holds literal coordinates for each conformer.

normative/heavy_atoms_only.mol and planar_3d.mol:
  Preserve supplied atoms exactly: Cl,C without adding missing H coordinates.
  The planar water explicitly declares 3D; zero Z alone must not trigger embedding.
  adversarial/declared_2d.mol has the same planar values marked 2D. Require a
  scientific-input correction rather than silently treating a drawing as 3D.

normative/partial_hydrogens.mol and hydrogen_free.mol (authored 2026-10-19):
  The partial chloromethane retains only H,Cl,C from atom_order_v2000.mol and the
  two corresponding bonds; its other two H coordinates are deliberately omitted.
  Carbon dioxide supplies O,C,O with two double bonds and zero H atoms. Its linear
  coordinates are synthetic literals, not experimental or optimized values.
  expected.json independently records both atom orders, coordinates and row IDs.
  Both must preserve exactly what was supplied, without adding or removing H.
  Parsing the partial case proves preservation, not scientific qualification of
  shifts for an incomplete molecule. A zero-H atom list alone does not establish
  that H is absent rather than omitted; the fixture author's molecular identity
  is not a completeness claim made by the coordinate reader or result planner.

SMILES H-policy additions (authored 2026-10-19):
  smiles.json supplements implicit CCl and fully explicit [H]C([H])([H])Cl with
  mixed [H]CCl and H-free O=C=O. Source explicit H must survive parsing, implicit
  H must become explicit before embedding, and H-free graphs must add none.
  Counts are independent graph expectations, checked by the native producer lane;
  they are not coordinate references or prediction-accuracy evidence. Note 003
  records exact pinned parser/AddHs references and the required mapping/settings.

normative/models.pdb and alternate_locations.pdb:
  Models 1 and 2 contain repeated anchor geometry. Explicit model selection must
  preserve both identities; never silently consume only the first.
  Alternate C locations A/B have equal occupancy: require explicit selection.
  A uses serial 22 at (0,0,0); B uses 23 at (0.125,0,0). Keep common atoms once.
  Element columns, including CL, are authority for element identity; atom names
  and serial numbers are separate source identifiers. CONECT is supplied.

normative/scale_nm.xyz and scale_bohr.xyz:
  Synthetic two-atom scale markers, not complete molecules. With explicit nm,
  0.125 becomes 1.25 angstrom exactly. With explicit bohr, 2 becomes
  1.058354421088 angstrom using CODATA 2022's central value. The latter factor is
  not exact; its standard uncertainty is 8.2e-11 angstrom/bohr. Preserve the
  reference/factor and original line numbers; missing/unknown units reject.
  Unit factors and scientific interpretation remain owned by input_geometry.py.

Configured native pasted/Upload lifecycle (authored 2026-10-20):
  tests/integration/provider_core/normative/test_nmrnet_supplied_lifecycle.py
  contains NM_TABLE, the same authored chloromethane in nm. A UTF-8 prefix
  checks byte-based selection; expected.json supplies the independent angstrom
  literals. This is a conversion/wiring test, not experimental chemistry.
  The exact nm decimals must equal the oracle's angstrom decimals divided by 10.
  Binary64 parsing and multiplication can introduce rounding. For exact nm x,
  parsed binary64 p = RN(x), and y = RN(10p), the triangle inequality gives
      |y - 10x| <= 10|p - x| + 0.5 ULP(RN(10p)).
  nm_conversion_oracle computes that bound with exact Fraction arithmetic from
  the fixed literals before receiving reader output, on radix-2/53-bit,
  round-to-nearest Python. Half an ULP bounds the single multiplication for these
  fixed finite normal values; 10 is exactly representable. Zero has zero bound.
  This allowance is arithmetic error, not a tuned decimal or chemical tolerance.
  Unit-factor authority remains reference 6 and input_geometry.py; no conversion
  constant is estimated from a reader. Atom/record/span/hash/replay identities
  stay exact. The selected Upload records (2,1), water then chloromethane from
  mixed_records.xyz, use exact angstrom-coordinate equality without conversion
  allowance. The native receipts retain actual reader options, source/oracle
  hashes and the precomputed rational error bounds.

adversarial/invalid_last.{xyz,sdf}, extended.xyz, unsupported_atom_type.mol2:
  A fourth record is deliberately malformed while the first three remain usable.
  Selecting (1,4) rejects the entire preparation; selecting (3,1) must not parse
  unselected invalid coordinates as selected input. Readable framing is still
  required. The XYZ failure carries a harmless private-data sentinel; diagnostics
  must name record/field without echoing it. Extended XYZ and the deliberately
  unknown MOL2 atom type must not silently become supported input.

SMILES and complete-batch cases
-------------------------------
smiles.json contains nine intended valid source cases, nine rejected declarations,
and three explicitly injected worker outcomes. They are test descriptions, not a
new public request schema. The source_atoms arrays describe literal SMILES atoms
before hydrogen addition with explicit source H retained. Generated inventories
are independent graph expectations, not generated-coordinate references.

The expected_added_hydrogen_parents arrays (authored 2026-10-19) name every
appended H index and its exact source parent. They follow source atom order and
the plain graphs' valences: CCl has three H on C0; [H]CCl adds two on C1;
butane has 3/2/2/3 H on C0/C1/C2/C3; fully explicit and H-free cases add none.
The two specified and one unspecified halomethane centers add H on C1; methane
adds four on C0. These literal expectations are not regenerated from AddHs.
Pinned AddHs append-order sources/rationale: requirements/preparation.md,
SMILES generation evidence. The native corpus test asserts exact pairs, so
attaching CCl H to Cl or all butane H to C0 fails even though both are non-H.

Cover implicit/explicit/mixed H and an H-free graph, flexible butane with N=3, opposite specified stereo,
unspecified stereo and a symmetric molecule with N>1. Keep source-to-output maps;
new H needs parent identity. Preserve specified stereo and disclose unspecified
stereo without claiming NMRNet validates stereochemistry. Never require symmetric
outputs to have distinct geometry. N missing/null/bool/string/fractional/zero/
negative rejects before embedding, as do empty or malformed SMILES.

The worker must record algorithm/version/full settings, actual coordinates and
conformer IDs, requested/returned counts, shortfall and prepared-byte identity.
No specific seed, algorithm default, pruning threshold or generated coordinates
are frozen by this corpus. Version-matched native producer and process-channel
checks now run in the maintained smiles-generator/native-smiles-worker lanes;
their evidence and limits are recorded in note 003. The injected shortfall uses
IDs 0,2 to expose index-vs-ID confusion; it is not a measured native shortfall.

batch_cases.json specifies actual source files/selections plus later fault-injection
contracts: reject a bad selected suffix before inference; later geometry failure
publishes no partial success; uncertain delivery replays the exact complete batch
without preparation/science again. Result-byte limits apply before inference.
Boundary recipes use admitted limits and limit+1 instead of adding large redundant
files or inventing a conformer/service-capacity limit. These scenarios need their
real preparation/batch/publication owners before they are passing runtime tests.

Verification
-------------
tests/test_provider_preparation_fixtures.py feeds the actual XYZ files through the
existing pure reader, asserting independent geometry, record/line order, conversion,
selection and rejection. Its normative/adversarial classes stay in the existing
test/unit/provider discovery lane; all data is mounted read-only with tests.
No native reader, embedding, Upload authority or batch-publication qualification
follows from those checks. Subsequent real-worker tests must consume these files,
not replace them with preconstructed toolkit molecule objects.

Precise references and rationale (consulted 2026-10-19)
-------------------------------------------------------
1. BIOVIA, CTfile Formats, BIOVIA Databases 2020:
   https://discover.3ds.com/sites/default/files/2020-08/biovia_ctfileformats_2020.pdf
   Chapter 2, V3000 atom/bond blocks (printed pp. 7-12); Chapter 3, molfile header
   and dimensional code (pp. 32-34); Chapter 6, V2000 CTAB (pp. 41-45);
   Chapter 10, SDfiles (pp. 75-76).
   These define the authored headers, field widths, identifiers and record framing.
2. wwPDB, PDB format v3.3, coordinate section, MODEL/ATOM/HETATM/ENDMDL:
   https://www.wwpdb.org/documentation/file-format-content/format33/sect9.html
   Atom serial 7-11, alternate location 17, X/Y/Z 31-54, element 77-78.
   Fixed columns distinguish source identity, coordinates and selection.
   Connectivity section, CONECT, specifies sorted reciprocal serial references:
   https://www.wwpdb.org/documentation/file-format-content/format33/sect10.html
   Connectivity order is distinct from the deliberately unusual atom-row order.
3. Tripos, SYBYL 7.1 (Mid-2005), Mol2 File Format, ATOM/BOND data records,
   printed pp. 12-13; MOLECULE section:
   https://zhanggroup.org/DockRMSD/mol2.pdf
   This is a mirror of the Tripos manual, not an RDKit compatibility guarantee.
4. RDKit 2026.09.1 reader documentation, MolFromMolBlock, ForwardSDMolSupplier,
   MolFromPDBBlock and MolFromMol2Block:
   https://www.rdkit.org/docs/source/rdkit.Chem.rdmolfiles.html
   Defaults may remove H; PDB can infer proximity bonds; MOL2 expects Corina
   atom typing and has optional substructure cleanup. Test the chosen settings.
   Embedding/parameters and failure reporting:
   https://www.rdkit.org/docs/source/rdkit.Chem.rdDistGeom.html
   Record actual algorithm/settings; a seed alone is not a portable numeric oracle.
5. Open Babel, plain XYZ count/comment/element-X-Y-Z record description:
   https://openbabel.org/docs/FileFormats/XYZ_cartesian_coordinates_format.html
   This explains the plain framing; extended metadata needs a different reader.
6. NIST SP 811 Appendix B.8, angstrom conversion (inverse gives nm -> angstrom):
   https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b8
   CODATA recommended values 2022, table XXXII, Bohr radius:
   https://physics.nist.gov/cuu/pdf/JPCRD2022CODATA.pdf
   Existing production conversion references are reused, not fitted to these points.
