import assert from "node:assert/strict";
import test from "node:test";
import { compareSubmittedScience } from "../gui_science.mjs";
import { pastedComparison, smilesComparison, suppliedComparison } from "../gui_science_ports.mjs";

test("new Job and ZIP identities are checked separately from unchanged scientific values", () => {
  const { actual, retained, scope } = suppliedComparison();
  compareSubmittedScience(actual, retained, scope, "sdf-zip");
  actual.interpretation.operations = ["inspect_uploads", "prepare_upload"];
  actual.interpretation.turns_started = 2;
  actual.interpretation.routes[0].turns_started = 2;
  assert.doesNotThrow(() => compareSubmittedScience(actual, retained, scope, "sdf-zip"),
    "The known filename needs no redundant archive inspection or extra model exchange.");
});

test("a bounded corrected conversation and harmless repeated inspection retain their real observations", () => {
  const { actual, retained, scope } = suppliedComparison();
  actual.interpretation.operations = ["inspect_uploads", "inspect_uploads", "inspect_upload", "prepare_upload"];
  actual.interpretation.corrections = 1;
  actual.interpretation.turns_started = 5;
  actual.interpretation.routes[0].turns_started = 5;
  assert.doesNotThrow(() => compareSubmittedScience(actual, retained, scope, "sdf-zip"));
});

test("pasted spelling may differ while its conversion and scientific values remain exact", () => {
  const { actual, retained, scope } = pastedComparison();
  compareSubmittedScience(actual, retained, scope, "pasted");
  actual.geometries[0].source.format = "xyz";
  actual.geometries[0].source.positions = [3];
  actual.geometries[0].preparation.method = "xyz";
  assert.doesNotThrow(() => compareSubmittedScience(actual, retained, scope, "pasted"));
});

test("SMILES generation retains graph expansion and scientific evidence under the new Job", () => {
  const { actual, retained, scope } = smilesComparison();
  assert.doesNotThrow(() => compareSubmittedScience(actual, retained, scope, "smiles"));
});
