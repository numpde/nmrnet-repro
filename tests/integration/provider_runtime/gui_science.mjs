// Scientific comparisons for normal GUI submissions; no inference or API effects.
// R46/R50 in notes/003 name the retained native evidence and the new identities
// that must differ. A new Job is not expected to reproduce the whole document.
import assert from "node:assert/strict";

function suppliedSource(actual, retained, scope, kind) {
  if (kind === "sdf-zip") {
    assert.deepEqual(actual.source, { ...retained.source, upload_ref: scope.uploadRef, archive: scope.archive },
      "The selected original SDF and the GUI's outer ZIP must retain distinct identities.");
    assert.deepEqual(actual.preparation, retained.preparation, "Supplied SDF units and reader method differ.");
    return;
  }
  const source = actual.source;
  assert.equal(source.job_ref, scope.jobRef);
  assert.equal(source.input_fingerprint, scope.specificationFingerprint);
  assert.equal(source.origin, "interpreter_supplied_coordinates");
  assert.equal(source.positions_relative_to, "received_source_text");
  assert.equal(source.position_kind, "line_number");
  assert.equal(source.record_number, 1);
  assert.match(source.sha256, /^[0-9a-f]{64}$/u);
  assert.ok(Number.isSafeInteger(source.byte_length) && source.byte_length > 0 && source.byte_length <= 1024 * 1024);
  assert.equal(source.positions.length, actual.prepared_request.atoms.length);
  assert.ok(source.positions.every((value, index, all) => Number.isSafeInteger(value) && value > 0 && (index === 0 || value > all[index - 1])));
  assert.ok(["table", "xyz"].includes(source.format));
  assert.deepEqual(actual.preparation, { ...retained.preparation, method: source.format === "table" ? "coordinate_table" : "xyz" },
    "The interpreter may spell its table differently, but supplied units and conversion must agree.");
}

function interpretation(actual, scope, kind) {
  const observation = actual.interpretation;
  assert.equal(observation.schema_id, "nmrnet.input_interpretation.v1");
  assert.equal(observation.outcome, "selected");
  assert.equal(observation.turn_limit, scope.turnLimit);
  assert.ok(Number.isSafeInteger(observation.turns_started) && observation.turns_started > 0 && observation.turns_started <= scope.turnLimit);
  assert.ok(Number.isSafeInteger(observation.corrections) && observation.corrections >= 0 && observation.corrections < observation.turns_started);
  const preparation = { pasted: "prepare_pasted", smiles: "prepare_smiles", "sdf-zip": "prepare_upload" }[kind];
  const operations = observation.operations;
  assert.equal(operations.at(-1), preparation);
  // InputInterpreter records successful operations only. A refused preparation
  // can be corrected; the eventual successful preparation ends selection.
  assert.ok(operations.slice(0, -1).every(name => ["inspect_uploads", "inspect_upload"].includes(name)),
    "Successful preparation must occur exactly once, after any bounded metadata inspection.");
  assert.ok(operations.length <= observation.turns_started);
  assert.deepEqual(observation.routes, [{ endpoint: scope.interpreter, turns_started: observation.turns_started }],
    "The selected interpreter must own the observed turns, without a paid fallback.");
}

export function compareSubmittedScience(actual, retained, scope, kind) {
  assert.ok(["pasted", "smiles", "sdf-zip"].includes(kind), "Select one maintained GUI case.");
  assert.equal(actual.schema_id, "nmrnet.coordinate_shifts.result.v2");
  assert.equal(actual.job_ref, scope.jobRef);
  assert.equal(actual.execution_attempt_ref, scope.attemptRef);
  assert.notEqual(scope.jobRef, retained.job_ref, "A new GUI submission must not reuse the retained reference Job.");
  assert.notEqual(scope.attemptRef, retained.execution_attempt_ref, "A new GUI submission must not reuse the retained reference Attempt.");
  assert.equal(actual.input_fingerprint, scope.specificationFingerprint);
  assert.deepEqual(actual.output, { shift_significant_digits: 17, verbosity: "max" });
  for (const field of ["selected_models", "runtime"]) assert.deepEqual(actual[field], retained[field], `Scientific ${field} differs.`);
  assert.equal(actual.execution.image_id, retained.execution.image_id, "The prediction image differs from retained native evidence.");
  assert.equal(actual.geometries.length, retained.geometries.length, "Conformer count differs.");
  for (const [index, geometry] of actual.geometries.entries()) {
    const original = retained.geometries[index];
    for (const field of ["geometry_id", "request_sha256", "prepared_request", "float32_coordinates", "prediction_unit", "loaded_elements", "predictions", "coverage"]) {
      assert.deepEqual(geometry[field], original[field], `Geometry ${index + 1}: ${field} differs from retained scientific evidence.`);
    }
    if (kind === "smiles") {
      assert.deepEqual(geometry.source, original.source);
      assert.deepEqual(geometry.preparation, original.preparation);
    } else suppliedSource(geometry, original, scope, kind);
  }
  if (kind === "smiles") {
    assert.deepEqual(actual.generation, { ...retained.generation, selection: {
      ...retained.generation.selection, job_ref: scope.jobRef, input_fingerprint: scope.specificationFingerprint
    } }, "Generated graph, hydrogen parents and embedding provenance must match the retained fixed-seed case.");
  }
  interpretation(actual, scope, kind);
}
