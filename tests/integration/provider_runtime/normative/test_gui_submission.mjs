import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { SubmissionRequests } from "../gui_submission_requests.mjs";
import { admitResultHref, renderedFailure } from "../gui_submission.mjs";

function submission(kind = "pasted") {
  return new SubmissionRequests({ origin: "https://nmr.localhost:10443", projectRef: "project:fixture",
    providerRef: "provider:fixture", principalRef: "user:fixture", specification: "Authored public input", kind,
    archive: kind === "sdf-zip" ? { byte_length: 123, sha256: "a".repeat(64), member_name: "butane_conformers.sdf" } : null });
}

test("normal GUI submission admits one closed Job, provider selection and opening", () => {
  const requests = submission();
  requests.admit("GET", "https://nmr.localhost:10443/v1/projects?limit=100");
  requests.admit("POST", "https://nmr.localhost:10443/v1/jobs", JSON.stringify({ operation_ref: "operation:fixture",
    project_ref: "project:fixture", analysis_kind_ref: "peaks_from_mol", description: "Authored fixture",
    specification: "Authored public input", state: "closed" }));
  assert.equal(requests.snapshot().effects[0].status, null, "Dispatch alone is not an API acknowledgement.");
  requests.respond(0, 201, "request:created");
  requests.admit("GET", "https://nmr.localhost:10443/v1/jobs/job:created");
  requests.bindJob({ operation_ref: "operation:fixture", project_ref: "project:fixture", submitter_ref: "user:fixture", job_ref: "job:created",
    specification_fingerprint: `sha256:${createHash("sha256").update("Authored public input").digest("hex")}` });
  requests.admit("PUT", "https://nmr.localhost:10443/v1/jobs/job:created/providers", '{"provider_refs":["provider:fixture"]}');
  requests.respond(1, 200, null);
  requests.admit("PUT", "https://nmr.localhost:10443/v1/jobs/job:created/state", '{"state":"open"}');
  requests.respond(2, 200, null);
  assert.equal(requests.snapshot().job_ref, "job:created");
  assert.equal(requests.snapshot().effects[0].operation_ref, "operation:fixture");
  assert.equal(requests.snapshot().effects.length, 3);
  assert.doesNotThrow(() => requests.complete());
});

test("a visible failed Attempt reaches the operator with its actual guidance and existing identities", () => {
  const message = renderedFailure({ jobRef: "job:created", attemptRef: "execution_attempt:failed", code: "input_rejected",
    message: "Specify the coordinate unit as angstrom, nm or bohr." });
  for (const fact of ["job:created", "execution_attempt:failed", "input_rejected", "Specify the coordinate unit", "will not resubmit"]) assert.ok(message.includes(fact));
});

test("the actual viewer link retains literal Job/result refs and the saved credential fragment", () => {
  const result = `analysis_result:sha256:${"a".repeat(64)}`;
  assert.equal(admitResultHref(`/v1b/jobs/job:created/analysis-results/${result}#credential=credential%3Afixture`,
    "https://nmr.localhost:10443", "job:created", "credential:fixture"), result);
});

test("normal GUI Upload publication binds exact archive bytes, one member and one attached Job", () => {
  // Transport-guard bytes, not a ZIP/scientific parsing fixture.
  const bytes = Buffer.from("authored-publication-bytes");
  const archive = { byte_length: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex"), member_name: "butane_conformers.sdf" };
  const requests = new SubmissionRequests({ origin: "https://nmr.localhost:10443", projectRef: "project:fixture",
    providerRef: "provider:fixture", principalRef: "user:fixture", specification: "Authored public input", kind: "sdf-zip", archive });
  const upload = `upload:sha256:${"b".repeat(64)}`;
  const exchanges = [
    ["POST", "/v1/uploads", JSON.stringify({ schema_id: "nmr.upload.create.request.v2", operation_ref: "operation:upload",
      project_ref: "project:fixture", byte_length: bytes.length, filename: "butane_conformers.sdf.zip", media_type: "application/zip", description: "Authored SDF" }), 201],
    ["POST", `/v1/uploads/${upload}/publish-capability`, null, 200],
    ["PUT", `/upload/v1/uploads/${upload}/bytes`, bytes, 201],
    ["POST", `/v1/uploads/${upload}/finalize`, null, 200],
    ["POST", "/v1/jobs", JSON.stringify({ operation_ref: "operation:job", project_ref: "project:fixture", analysis_kind_ref: "peaks_from_mol",
      specification: "Authored public input", description: "Authored SDF", state: "closed" }), 201],
    ["PUT", "/v1/jobs/job:created/uploads", JSON.stringify({ upload_refs: [upload] }), 200],
    ["PUT", "/v1/jobs/job:created/providers", '{"provider_refs":["provider:fixture"]}', 200],
    ["PUT", "/v1/jobs/job:created/state", '{"state":"open"}', 200]
  ];
  for (const [method, path, body, status] of exchanges) {
    const index = requests.admit(method, `https://nmr.localhost:10443${path}`, body);
    requests.respond(index, status, "request:fixture");
    if (path.endsWith("/finalize")) requests.confirmUpload({ project_ref: "project:fixture", upload_ref: upload,
      creator_ref: "user:fixture", byte_length: bytes.length, finalized: true });
    if (path === "/v1/jobs") {
      requests.admit("GET", "https://nmr.localhost:10443/v1/jobs/job:created");
      requests.bindJob({ operation_ref: "operation:job", project_ref: "project:fixture", submitter_ref: "user:fixture", job_ref: "job:created",
        specification_fingerprint: `sha256:${createHash("sha256").update("Authored public input").digest("hex")}` });
    }
  }
  requests.complete();
  const receipt = requests.snapshot();
  assert.equal(receipt.upload_ref, upload);
  assert.equal(receipt.job_ref, "job:created");
  assert.equal(receipt.effects.length, 8);
  assert.ok(receipt.effects.every(effect => !Object.hasOwn(effect, "body") && !Object.hasOwn(effect, "headers")));
});

test("GUI discovery can run before packaging, but reservation waits for the actual prepared archive", () => {
  const requests = new SubmissionRequests({ origin: "https://nmr.localhost:10443", projectRef: "project:fixture",
    providerRef: "provider:fixture", principalRef: "user:fixture", specification: "Authored public input", kind: "sdf-zip", archive: null });
  requests.admit("GET", "https://nmr.localhost:10443/v1/projects?limit=100");
  const body = JSON.stringify({ schema_id: "nmr.upload.create.request.v2", operation_ref: "operation:upload",
    project_ref: "project:fixture", byte_length: 123, filename: "butane_conformers.sdf.zip", media_type: "application/zip", description: "Authored SDF" });
  assert.throws(() => requests.admit("POST", "https://nmr.localhost:10443/v1/uploads", body));
  assert.equal(requests.snapshot().effects.length, 0);
  requests.selectArchive({ byte_length: 123, sha256: "a".repeat(64), member_name: "butane_conformers.sdf" });
  assert.doesNotThrow(() => requests.admit("POST", "https://nmr.localhost:10443/v1/uploads", body));
});
