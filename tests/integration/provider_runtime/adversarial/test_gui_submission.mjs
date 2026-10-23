import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { SubmissionRequests } from "../gui_submission_requests.mjs";

function fresh() {
  return new SubmissionRequests({ origin: "https://nmr.localhost:10443", projectRef: "project:fixture",
    providerRef: "provider:fixture", principalRef: "user:fixture", specification: "Authored public input", kind: "pasted", archive: null });
}
const creation = JSON.stringify({ operation_ref: "operation:fixture", project_ref: "project:fixture",
  analysis_kind_ref: "peaks_from_mol", description: "Authored fixture", specification: "Authored public input", state: "closed" });

test("unknown creation outcome refuses a second Job and preserves the original operation", () => {
  const requests = fresh();
  requests.admit("POST", "https://nmr.localhost:10443/v1/jobs", creation);
  assert.throws(() => requests.admit("POST", "https://nmr.localhost:10443/v1/jobs", creation));
  assert.throws(() => requests.admit("PUT", "https://nmr.localhost:10443/v1/jobs/job:other/providers", '{"provider_refs":["provider:fixture"]}'));
  assert.equal(requests.snapshot().effects.length, 1);
  assert.equal(requests.snapshot().effects[0].operation_ref, "operation:fixture");
  assert.equal(requests.snapshot().effects[0].status, null);
  assert.throws(() => requests.complete());
});

test("a follow-up Job path cannot turn a creation status into ownership", () => {
  const requests = fresh();
  requests.admit("POST", "https://nmr.localhost:10443/v1/jobs", creation);
  requests.respond(0, 201, "request:created");
  requests.admit("GET", "https://nmr.localhost:10443/v1/jobs/job:existing");
  assert.throws(() => requests.admit("PUT", "https://nmr.localhost:10443/v1/jobs/job:existing/providers", '{"provider_refs":["provider:fixture"]}'));
  const receipt = { operation_ref: "operation:fixture", project_ref: "project:fixture",
    submitter_ref: "user:fixture", job_ref: "job:existing",
    specification_fingerprint: `sha256:${createHash("sha256").update("Authored public input").digest("hex")}` };
  assert.throws(() => requests.bindJob({ ...receipt, operation_ref: "operation:other" }));
  assert.equal(requests.snapshot().job_ref, null);
  assert.equal(requests.snapshot().observed_job_ref, "job:existing");
});

test("foreign destinations, query scope, resources and initial open Jobs are refused before effects", () => {
  for (const [method, url, body] of [
    ["GET", "https://other.example/v1/projects"],
    ["GET", "https://nmr.localhost:10443/v1/jobs?project_ref=project:other"],
    ["GET", "https://nmr.localhost:10443/v1/jobs/job:existing"],
    ["DELETE", "https://nmr.localhost:10443/v1/jobs/job:existing"],
    ["POST", "https://nmr.localhost:10443/v1/jobs", creation.replace('"closed"', '"open"')],
    ["POST", "https://nmr.localhost:10443/v1/jobs", creation.replace('project:fixture', 'project:other')],
    ["POST", "https://nmr.localhost:10443/v1/jobs", creation.replace('"operation:fixture"', 'null')]
  ]) {
    const requests = fresh();
    assert.throws(() => requests.admit(method, url, body));
    assert.equal(requests.snapshot().effects.length, 0);
  }
});

test("refusal status and safe request identity survive without exposing arbitrary header text", () => {
  const requests = fresh();
  requests.admit("POST", "https://nmr.localhost:10443/v1/jobs", creation);
  requests.respond(0, 429, "Bearer PRIVATE_HEADER_CANARY");
  assert.throws(() => requests.admit("GET", "https://nmr.localhost:10443/v1/jobs/job:other"));
  assert.throws(() => requests.admit("POST", "https://nmr.localhost:10443/v1/jobs", creation));
  assert.equal(requests.snapshot().effects[0].status, 429);
  assert.equal(requests.snapshot().effects[0].request_id, null);
  assert.ok(!JSON.stringify(requests.snapshot()).includes("PRIVATE_HEADER_CANARY"));
});

test("publication cannot substitute other bytes behind the admitted archive descriptor", () => {
  const bytes = Buffer.from("authored-publication-bytes"), upload = `upload:sha256:${"b".repeat(64)}`;
  const requests = new SubmissionRequests({ origin: "https://nmr.localhost:10443", projectRef: "project:fixture",
    providerRef: "provider:fixture", principalRef: "user:fixture", specification: "Authored public input", kind: "sdf-zip",
    archive: { byte_length: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex"), member_name: "butane_conformers.sdf" } });
  requests.admit("POST", "https://nmr.localhost:10443/v1/uploads", JSON.stringify({ schema_id: "nmr.upload.create.request.v2",
    operation_ref: "operation:upload", project_ref: "project:fixture", byte_length: bytes.length,
    filename: "butane_conformers.sdf.zip", media_type: "application/zip", description: "Authored SDF" }));
  requests.respond(0, 201, null);
  requests.admit("POST", `https://nmr.localhost:10443/v1/uploads/${upload}/publish-capability`);
  requests.respond(1, 200, null);
  assert.equal(requests.snapshot().observed_upload_ref, upload);
  assert.equal(requests.snapshot().upload_ref, null, "A path observation does not confirm final publication.");
  assert.throws(() => requests.admit("PUT", `https://nmr.localhost:10443/upload/v1/uploads/${upload}/bytes`, Buffer.alloc(bytes.length)));
  assert.equal(requests.snapshot().effects.length, 2, "Replacement bytes never became an admitted effect.");
});
