import assert from "node:assert/strict";
import test from "node:test";
import { compareSubmittedScience } from "../gui_science.mjs";
import { pastedComparison, smilesComparison, suppliedComparison } from "../gui_science_ports.mjs";

test("receiving proof refuses changed atom mapping, shifts, model bytes and source attribution", () => {
  const corruptions = [
    actual => { actual.geometries.reverse(); },
    actual => { actual.geometries[0].predictions[0].atom_index = 1; },
    actual => { actual.geometries[0].predictions[0].shift_ppm += 0.000001; },
    actual => { actual.geometries[0].prepared_request.coordinates[0][0] += 0.01; },
    actual => { actual.geometries[0].float32_coordinates[0][0] += 0.01; },
    actual => { actual.geometries[0].source.sha256 = "b".repeat(64); },
    actual => { actual.geometries[0].source.archive.member_name = "different.sdf"; },
    actual => { actual.geometries[0].source.archive.sha256 = "a".repeat(64); },
    actual => { actual.geometries[0].source.upload_ref = "upload:other"; },
    actual => { actual.selected_models.C = "different-model"; },
    actual => { actual.job_ref = "job:other"; },
    actual => { actual.execution_attempt_ref = "execution_attempt:other"; },
    actual => { actual.output.shift_significant_digits = 6; },
    actual => { actual.interpretation.operations.push("prepare_upload"); },
    actual => { actual.interpretation.routes[0].endpoint.model = "other-model"; }
  ];
  for (const corrupt of corruptions) {
    const { actual, retained, scope } = suppliedComparison();
    // Actual and expected source/endpoint facts must be independent objects.
    const independent = structuredClone(actual);
    corrupt(independent);
    assert.throws(() => compareSubmittedScience(independent, retained, scope, "sdf-zip"));
  }
});

test("pasted source attribution and units cannot drift behind unchanged shifts", () => {
  for (const corrupt of [
    actual => { actual.geometries[0].source.job_ref = "job:old"; },
    actual => { actual.geometries[0].source.positions_relative_to = "original_Job"; },
    actual => { actual.geometries[0].source.byte_length = 0; },
    actual => { actual.geometries[0].source.positions = [0]; },
    actual => { actual.geometries[0].preparation.scale_to_angstrom = 1; }
  ]) {
    const { actual, retained, scope } = pastedComparison();
    corrupt(actual);
    assert.throws(() => compareSubmittedScience(actual, retained, scope, "pasted"));
  }
});

test("SMILES count and hydrogen parent provenance remain part of receiving proof", () => {
  for (const corrupt of [
    actual => { actual.generation.selection.requested_count = 2; },
    actual => { actual.generation.selection.job_ref = "job:old"; },
    actual => { actual.generation.provenance.added_hydrogen_parents.push([1, 0]); }
  ]) {
    const { actual, retained, scope } = smilesComparison();
    corrupt(actual);
    assert.throws(() => compareSubmittedScience(actual, retained, scope, "smiles"));
  }
});

test("the GUI witness cannot claim a new submission by reusing the retained Job or Attempt", () => {
  for (const field of ["job", "attempt"]) {
    const { actual, retained, scope } = suppliedComparison();
    if (field === "job") actual.job_ref = scope.jobRef = retained.job_ref;
    else actual.execution_attempt_ref = scope.attemptRef = retained.execution_attempt_ref;
    assert.throws(() => compareSubmittedScience(actual, retained, scope, "sdf-zip"));
  }
});
