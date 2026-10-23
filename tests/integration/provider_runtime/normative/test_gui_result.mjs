import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { admitOrigin, admitReceipt, permitRequest } from "../gui_result.mjs";

test("existing scientific bytes remain the reference for GUI delivery", () => {
  const bytes = Buffer.from('{"prediction_unit":"ppm","geometries":[]}\n');
  const receipt = {
    job_ref: "job:fixture",
    analysis_result_ref: "analysis-result:fixture",
    execution_attempt_ref: "execution-attempt:fixture",
    provider_ref: "provider:fixture",
    result_schema_id: "nmrnet.coordinate_shifts.result.v1",
    canonical_result_base64: bytes.toString("base64"),
    result_byte_length: bytes.length,
    result_fingerprint: `sha256:${createHash("sha256").update(bytes).digest("hex")}`
  };
  assert.deepEqual(admitReceipt(receipt).bytes, bytes);
  assert.deepEqual(admitReceipt({ ...receipt, result_schema_id: "nmrnet.coordinate_shifts.result.v2" }).bytes, bytes);
  for (const name of ["job_ref", "analysis_result_ref", "execution_attempt_ref", "provider_ref", "result_schema_id"]) {
    const incomplete = { ...receipt };
    delete incomplete[name];
    assert.throws(() => admitReceipt(incomplete), "Missing identity must not pass by comparing undefined values.");
  }
  assert.equal(admitOrigin("https://nmr.localhost:10443"), "https://nmr.localhost:10443");
});

test("GUI assets and signed API reads use only the selected origins", () => {
  const origins = new Set(["https://gui.example", "https://api.example"]);
  const paths = new Set(["/v1/jobs/job:fixture"]);
  assert.ok(permitRequest("https://gui.example/v1b", "GET", origins, paths));
  assert.ok(permitRequest("https://api.example/v1/jobs/job:fixture", "GET", origins, paths));
});
