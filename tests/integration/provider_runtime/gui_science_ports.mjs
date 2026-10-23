// Synthetic comparison inputs only; these are not model predictions or accuracy evidence.
export function suppliedComparison() {
  const geometry = record => ({
    geometry_id: `geometry-${record}`, request_sha256: `${record}`.repeat(64),
    prepared_request: { coordinate_unit: "angstrom", atoms: ["C"], coordinates: [[record, 0, 0]] },
    float32_coordinates: [[record, 0, 0]], prediction_unit: "ppm", loaded_elements: ["C"],
    predictions: [{ atom_index: 0, element: "C", shift_ppm: 10 * record }],
    coverage: { basis: "supplied_coordinates", supplied_hydrogen_atoms: 0, predicted_hydrogen_atoms: 0 },
    preparation: { method: "sdf", source_unit: "angstrom", scale_to_angstrom: 1, conversion_reference: "identity (angstrom)" },
    source: { sha256: "a".repeat(64), byte_length: 1010, positions: [1], position_kind: "atom_id",
      record_number: record, upload_ref: "upload:old" }
  });
  const retained = {
    job_ref: "job:old", execution_attempt_ref: "execution_attempt:old",
    schema_id: "nmrnet.coordinate_shifts.result.v1", selected_models: { C: "authored-comparison-model" },
    runtime: { python: "authored-runtime" }, execution: { image_id: `sha256:${"c".repeat(64)}` },
    geometries: [geometry(1), geometry(2)]
  };
  const scope = { jobRef: "job:new", attemptRef: "execution_attempt:new", specificationFingerprint: `sha256:${"f".repeat(64)}`,
    uploadRef: "upload:new", archive: { sha256: "b".repeat(64), byte_length: 512, member_name: "butane_conformers.sdf" },
    turnLimit: 6, interpreter: { configuration_id: "authored-endpoint", model: "authored-model", configuration_sha256: "e".repeat(64) } };
  const actual = structuredClone(retained);
  Object.assign(actual, { job_ref: scope.jobRef, execution_attempt_ref: scope.attemptRef,
    input_fingerprint: scope.specificationFingerprint, output: { shift_significant_digits: 17 },
    interpretation: { schema_id: "nmrnet.input_interpretation.v1", outcome: "selected", corrections: 0,
      operations: ["inspect_uploads", "inspect_upload", "prepare_upload"], turn_limit: scope.turnLimit, turns_started: 3,
      routes: [{ endpoint: scope.interpreter, turns_started: 3 }] } });
  for (const item of actual.geometries) {
    Object.assign(item.source, { upload_ref: scope.uploadRef, archive: scope.archive });
  }
  return { actual, retained, scope };
}

export function pastedComparison() {
  const fixture = suppliedComparison();
  const { actual, retained, scope } = fixture;
  retained.geometries.length = actual.geometries.length = 1;
  retained.geometries[0].preparation = { method: "coordinate_table", source_unit: "nm", scale_to_angstrom: 10, conversion_reference: "NIST SP 811 Appendix B.8" };
  actual.geometries[0].preparation = structuredClone(retained.geometries[0].preparation);
  actual.geometries[0].source = { job_ref: scope.jobRef, input_fingerprint: scope.specificationFingerprint,
    origin: "interpreter_supplied_coordinates", positions_relative_to: "received_source_text", position_kind: "line_number",
    record_number: 1, format: "table", positions: [1], byte_length: 12, sha256: "d".repeat(64) };
  actual.interpretation.operations = ["prepare_pasted"];
  actual.interpretation.turns_started = actual.interpretation.routes[0].turns_started = 1;
  return fixture;
}

export function smilesComparison() {
  const fixture = suppliedComparison();
  const { actual, retained, scope } = fixture;
  retained.geometries.length = actual.geometries.length = 1;
  retained.geometries[0].source = { kind: "smiles", sha256: "d".repeat(64), conformer_id: 0 };
  retained.geometries[0].preparation = { method: "smiles", coordinate_unit: "angstrom" };
  actual.geometries[0].source = structuredClone(retained.geometries[0].source);
  actual.geometries[0].preparation = structuredClone(retained.geometries[0].preparation);
  retained.generation = { selection: { job_ref: "job:old", input_fingerprint: `sha256:${"a".repeat(64)}`,
    origin: "interpreter_supplied_smiles", byte_length: 1, source_sha256: "d".repeat(64), requested_count: 1 },
  provenance: { added_hydrogen_parents: [], requested_count: 1 }, provenance_sha256: "e".repeat(64) };
  actual.generation = structuredClone(retained.generation);
  Object.assign(actual.generation.selection, { job_ref: scope.jobRef, input_fingerprint: scope.specificationFingerprint });
  actual.interpretation.operations = ["prepare_smiles"];
  actual.interpretation.turns_started = actual.interpretation.routes[0].turns_started = 1;
  return fixture;
}
