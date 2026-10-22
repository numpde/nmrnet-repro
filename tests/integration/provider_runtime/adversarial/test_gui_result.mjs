import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import test from "node:test";
import { admitOrigin, admitReceipt, boundedFile, permitRequest } from "../gui_result.mjs";

test("the external-file reader accepts its exact ceiling and consumes only one excess byte", async () => {
  const directory = await mkdtemp("/tmp/gui-file-bound-");
  try {
    const path = `${directory}/source`;
    await writeFile(path, "1234");
    assert.equal((await boundedFile(path, 4)).toString(), "1234");
    await writeFile(path, "12345");
    await assert.rejects(boundedFile(path, 4), /byte limit/);
  } finally { await rm(directory, { recursive: true }); }
});

test("recipient qualification refuses mutations and unrelated destinations", () => {
  const origins = new Set(["https://nmr.localhost:10443"]);
  const paths = new Set(["/v1/jobs/job:fixture"]);
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    assert.equal(permitRequest("https://nmr.localhost:10443/v1/jobs", method, origins, paths), false);
  }
  assert.equal(permitRequest("https://other.example/v1/jobs", "GET", origins, paths), false);
  assert.equal(permitRequest("https://nmr.localhost:10443/v1/jobs/job:other", "GET", origins, paths), false);
  assert.equal(permitRequest("https://nmr.localhost:10443/v1/jobs/job:fixture?include=other", "GET", origins, paths), false);
});

test("origin admission rejects credentials, paths and misleading ports", () => {
  for (const origin of ["http://example.test", "https://user:secret@example.test", "https://example.test/path", "https://example.test:0"]) {
    assert.throws(() => admitOrigin(origin));
  }
});

test("a corrupt retained read response cannot become the GUI oracle", () => {
  const receipt = {
    job_ref: "job:fixture", analysis_result_ref: "analysis-result:fixture",
    canonical_result_base64: "e30=", result_byte_length: 2,
    result_fingerprint: `sha256:${"0".repeat(64)}`
  };
  assert.throws(() => admitReceipt(receipt), /fingerprint/);
  assert.throws(() => admitReceipt({ ...receipt, result_byte_length: 3 }), /length/);
  assert.throws(() => admitReceipt({ ...receipt, canonical_result_base64: "e30=\n" }), /base64/);
});
