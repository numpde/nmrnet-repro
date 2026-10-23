// Admission for exactly one ordinary GUI submission, not an API lifecycle owner.
// The normal GUI/API own signing, response admission and persistence. This
// witness refuses unintended effects and retains safe observations for recovery.
// GUI df172527409cc08d46b3318cd19e961f6c3be432:
// app/src/api/{job-create,job-upload-set,job-provider-selection-update,
// job-state-update,upload-transfer-api,upload-byte-publication}.ts.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { admitOrigin } from "./gui_result.mjs";

const jobPattern = /^job:[A-Za-z0-9_.-]{1,124}$/u;
const uploadPattern = /^upload:sha256:[0-9a-f]{64}$/u;
const operationPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const successStatus = { reserve: 201, grant: 200, publish: 201, finalize: 200, create: 201, attach: 200, providers: 200, open: 200 };

function exactFields(body, fields) {
  assert.ok(body && typeof body === "object" && !Array.isArray(body) &&
    Object.keys(body).sort().join(",") === [...fields].sort().join(","), "GUI mutation fields differ from the intended operation.");
}

function pageQuery(url, required) {
  const allowed = new Set([...Object.keys(required), "limit", "cursor"]);
  for (const [name, value] of url.searchParams) {
    assert.ok(allowed.has(name) && url.searchParams.getAll(name).length === 1, "GUI read query contains an unrelated or repeated field.");
    if (name === "limit") assert.ok(/^[1-9][0-9]*$/u.test(value) && Number(value) <= 100, "GUI read page size differs.");
  }
  for (const [name, value] of Object.entries(required)) assert.ok(url.searchParams.get(name) === value, "GUI read belongs to another Project or analysis.");
}

export class SubmissionRequests {
  constructor({ origin, projectRef, providerRef, principalRef, specification, kind, archive }) {
    this.origin = admitOrigin(origin);
    assert.ok(["pasted", "smiles", "sdf-zip"].includes(kind), "Select one maintained GUI submission case.");
    assert.ok(typeof projectRef === "string" && /^project:[A-Za-z0-9_.-]{1,120}$/u.test(projectRef), "Select the intended Project.");
    assert.ok(typeof providerRef === "string" && /^provider:[A-Za-z0-9_.-]{1,119}$/u.test(providerRef), "Select the intended provider.");
    assert.ok(typeof principalRef === "string" && operationPattern.test(principalRef), "Identify the imported user credential's principal.");
    assert.ok(typeof specification === "string" && Buffer.byteLength(specification) > 0 && Buffer.byteLength(specification) <= 65536,
      "Use the bounded authored specification.");
    Object.assign(this, { projectRef, providerRef, principalRef, specification });
    this.plan = kind === "sdf-zip" ? ["reserve", "grant", "publish", "finalize", "create", "attach", "providers", "open"] : ["create", "providers", "open"];
    this.effects = [];
    this.uploadRef = this.confirmedUploadRef = this.jobRef = this.observedJobRef = null;
    this.archive = null;
    if (archive !== null) this.selectArchive(archive);
  }

  selectArchive(archive) {
    assert.ok(this.plan[0] === "reserve" && this.effects.length === 0 && this.archive === null && archive &&
      Number.isSafeInteger(archive.byte_length) && archive.byte_length > 0 && archive.byte_length <= 1024 * 1024 &&
      /^[0-9a-f]{64}$/u.test(archive.sha256) && archive.member_name === "butane_conformers.sdf",
    "Admit the actual GUI-prepared ZIP once, before publication.");
    this.archive = { sha256: archive.sha256, byte_length: archive.byte_length, member_name: archive.member_name };
  }

  snapshot() {
    return structuredClone({ job_ref: this.jobRef, observed_job_ref: this.observedJobRef,
      upload_ref: this.confirmedUploadRef, observed_upload_ref: this.uploadRef, archive: this.archive, effects: this.effects });
  }

  _read(url) {
    const path = url.pathname;
    if (!path.startsWith("/v1/")) {
      assert.ok(!path.startsWith("/upload/"), "No upload-store read is part of submission.");
      return;
    }
    const lists = {
      "/v1/projects": {}, "/v1/analysis-kinds": { project_ref: this.projectRef },
      "/v1/providers": { project_ref: this.projectRef, analysis_kind_ref: "peaks_from_mol" },
      "/v1/jobs": { project_ref: this.projectRef }, "/v1/uploads": { project_ref: this.projectRef }
    };
    if (Object.hasOwn(lists, path)) { pageQuery(url, lists[path]); return; }
    if (path === `/v1/projects/${this.projectRef}`) { assert.equal(url.search, ""); return; }
    const [job, ...suffix] = path.slice("/v1/jobs/".length).split("/");
    if (path.startsWith("/v1/jobs/") && jobPattern.test(job)) {
      const created = this.effects.find(effect => effect.kind === "create");
      assert.ok(created?.status === 201 && (this.observedJobRef === null || this.observedJobRef === job), "GUI requested another Job.");
      if (suffix.length === 0) assert.equal(url.search, "");
      else if (suffix.length === 1 && ["uploads", "providers", "execution-attempts", "analysis-results"].includes(suffix[0])) pageQuery(url, {});
      else assert.ok(suffix.length === 2 && suffix[0] === "analysis-results" && /^analysis_result:sha256:[0-9a-f]{64}$/u.test(suffix[1]) && url.search === "",
        "GUI Job read is outside this submission.");
      // A follow-up read is an observation, not authority to change that Job.
      this.observedJobRef = job;
      return;
    }
    assert.ok(this.uploadRef && path === `/v1/uploads/${this.uploadRef}` && url.search === "", "GUI read is outside this submission.");
  }

  admit(method, address, bytes = null) {
    const url = new URL(address);
    assert.ok(url.origin === this.origin && !url.username && !url.password && !url.hash, "GUI request left the selected origin.");
    if (method === "GET") { this._read(url); return null; }
    assert.equal(url.search, "", "GUI mutations must not carry query arguments.");
    const kind = this.plan[this.effects.length], previous = this.effects.at(-1);
    assert.ok(kind && (!previous || previous.status === successStatus[previous.kind] && !previous.transport_failure), "Submission stops after an uncertain/refused effect; never resubmit the case.");
    const paths = { reserve: "/v1/uploads", grant: `/v1/uploads/${this.uploadRef}/publish-capability`,
      publish: `/upload/v1/uploads/${this.uploadRef}/bytes`, finalize: `/v1/uploads/${this.uploadRef}/finalize`, create: "/v1/jobs",
      attach: `/v1/jobs/${this.jobRef}/uploads`, providers: `/v1/jobs/${this.jobRef}/providers`, open: `/v1/jobs/${this.jobRef}/state` };
    let observedUpload = this.uploadRef;
    if (kind === "grant") {
      const matched = /^\/v1\/uploads\/([^/]+)\/publish-capability$/u.exec(url.pathname);
      assert.ok(matched && uploadPattern.test(matched[1]), "The GUI publication must identify its Upload.");
      observedUpload = matched[1]; paths.grant = `/v1/uploads/${observedUpload}/publish-capability`;
    }
    assert.ok(method === (["publish", "attach", "providers", "open"].includes(kind) ? "PUT" : "POST") && url.pathname === paths[kind],
      "GUI mutation differs from the next intended operation.");
    let body = null;
    if (!["grant", "publish", "finalize"].includes(kind)) {
      assert.ok(bytes && Buffer.byteLength(bytes) <= 65536, "GUI mutation body exceeds the authored input bound.");
      try { body = JSON.parse(bytes.toString()); } catch { throw new Error("GUI mutation is not a JSON object; its body was excluded."); }
    }
    if (kind === "create" && this.archive) assert.ok(this.confirmedUploadRef, "Confirm the GUI's finalized Upload before creating its Job.");
    this._body(kind, body, bytes);
    this.uploadRef = observedUpload;
    const effect = { kind, method, path: url.pathname, operation_ref: body?.operation_ref ?? null, status: null, request_id: null, transport_failure: null };
    this.effects.push(effect);
    return this.effects.length - 1;
  }

  _body(kind, body, bytes) {
    if (kind === "reserve") {
      assert.ok(this.archive, "Select and verify the GUI-prepared ZIP before its reservation.");
      exactFields(body, ["schema_id", "filename", "media_type", "operation_ref", "project_ref", "byte_length", "description"]);
      assert.ok(typeof body.operation_ref === "string" && operationPattern.test(body.operation_ref) && body.project_ref === this.projectRef &&
        body.schema_id === "nmr.upload.create.request.v2" &&
        body.byte_length === this.archive.byte_length && body.filename === `${this.archive.member_name}.zip` && body.media_type === "application/zip",
      "GUI reservation does not match the selected Project and prepared ZIP.");
    } else if (kind === "create") {
      exactFields(body, ["operation_ref", "project_ref", "analysis_kind_ref", "description", "specification", "state"]);
      assert.ok(typeof body.operation_ref === "string" && operationPattern.test(body.operation_ref) && body.project_ref === this.projectRef && body.analysis_kind_ref === "peaks_from_mol" &&
        body.specification === this.specification && body.state === "closed", "GUI must create one closed Job with the exact authored input.");
    } else if (kind === "publish") {
      assert.ok(Buffer.isBuffer(bytes) && bytes.length === this.archive.byte_length &&
        createHash("sha256").update(bytes).digest("hex") === this.archive.sha256, "GUI must publish its exact prepared archive bytes.");
    } else if (kind === "grant" || kind === "finalize") {
      assert.ok(bytes === null || bytes.length === 0, "GUI publication control calls must have no replacement body.");
    } else if (kind === "attach" || kind === "providers") {
      const field = kind === "attach" ? "upload_refs" : "provider_refs", expected = kind === "attach" ? this.uploadRef : this.providerRef;
      exactFields(body, [field]);
      assert.ok(this.jobRef && expected && Array.isArray(body[field]) && body[field].length === 1 && body[field][0] === expected,
        "GUI must select exactly this submission's Upload or admitted provider.");
    } else {
      exactFields(body, ["state"]);
      assert.ok(this.jobRef && body.state === "open", "Only the newly configured Job may be opened.");
    }
  }

  respond(index, status, requestId) {
    const effect = this.effects[index];
    assert.ok(effect && effect.status === null && Number.isInteger(status) && status >= 100 && status <= 599, "GUI response observation is inconsistent.");
    effect.status = status;
    effect.request_id = typeof requestId === "string" && operationPattern.test(requestId) ? requestId : null;
  }

  transportFailed(index, errorText) {
    const effect = this.effects[index];
    assert.ok(effect, "Browser transport failure must belong to an admitted mutation.");
    // Chromium's code contains no destination or peer body; arbitrary failure
    // descriptions are not copied into this safe observation surface.
    effect.transport_failure = typeof errorText === "string" && /^net::ERR_[A-Z0-9_]{1,80}$/u.test(errorText)
      ? errorText : "Browser transport failed without a recognized error code";
    return new Error(`GUI ${effect.kind} request failed (${effect.transport_failure}); ` +
      `HTTP status ${effect.status ?? "not received"}. Keep the original operation; do not resubmit the case.`);
  }

  bindJob(receipt) {
    // Explicit projection of scoped rendered Job facts, not a raw API response
    // or browser-internal object. PersistedJobDetails exposes these identifiers.
    const creation = this.effects.find(effect => effect.kind === "create");
    const fingerprint = `sha256:${createHash("sha256").update(this.specification).digest("hex")}`;
    assert.ok(creation?.status === 201 && receipt && receipt.operation_ref === creation.operation_ref &&
      receipt.project_ref === this.projectRef && receipt.submitter_ref === this.principalRef && receipt.specification_fingerprint === fingerprint &&
      this.jobRef === null && receipt.job_ref === this.observedJobRef && typeof receipt.job_ref === "string" && jobPattern.test(receipt.job_ref),
    "The scoped GUI Job receipt does not identify this submission's operation, user and exact input.");
    this.jobRef = receipt.job_ref;
  }

  confirmUpload(receipt) {
    // ColumnUploadDetails exposes finalization, scope, creator and length. It
    // does not expose the stored content hash: the later scientific result must
    // independently bind source.archive to the exact GUI-prepared ZIP hash.
    const finalized = this.effects.find(effect => effect.kind === "finalize");
    assert.ok(finalized?.status === 200 && receipt && this.confirmedUploadRef === null &&
      receipt.upload_ref === this.uploadRef && receipt.project_ref === this.projectRef && receipt.creator_ref === this.principalRef &&
      receipt.byte_length === this.archive.byte_length && receipt.finalized === true,
    "The scoped GUI Upload receipt does not confirm this user's finalized file.");
    this.confirmedUploadRef = receipt.upload_ref;
  }

  complete() {
    assert.ok(this.jobRef && (!this.archive || this.confirmedUploadRef) && this.effects.length === this.plan.length && this.effects.every(effect => effect.status === successStatus[effect.kind] && !effect.transport_failure),
      "The GUI did not confirm every intended mutation; retain this submission without retrying it.");
  }
}
