// Normal /v1b controls only: no injected signer, replaced response or Job-state bridge.
// GUI precedent df172527409cc08d46b3318cd19e961f6c3be432:
// tests/e2e/signed-upload-{workflow.mjs,lifecycle.test.mjs}; ordinary controls,
// excluding installUploadProof/window.uploadProof and their direct API helpers.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { writeFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { admitOrigin, admitReceipt, boundedFile, browserContext, openWorkspace, receiveContent } from "./gui_result.mjs";
import { compareSubmittedScience } from "./gui_science.mjs";
import { SubmissionRequests } from "./gui_submission_requests.mjs";

const referenceHashes = {
  // 003 retained native service receipt, R21/R24; also api_prediction::_retained_science.
  pasted: "9ae5017c42af5c5691554eb3ff6dced7895c6a00ab177975d8aa15ae0c58f59a",
  // 003 R44: original api-real-openai-smiles-011a248 client/030-result.response.json.
  smiles: "574dbf183feb23e9ab28e97ce2fb1bf74cc81b3823f83e84245c532a6cb4ad0a",
  // 003 R46: native-service-UXc3mVpW formats/sdf-carbon-only/scientific-result.json.
  "sdf-zip": "bd5adb6f47ac7b5b739ae4c52ce36d308195bae56b6233c0d07e22918227a3a3"
};
const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");

function parsedJSON(bytes, label) {
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { throw new Error(`The ${label} is not valid UTF-8 JSON; its contents were excluded.`); }
}

export function admitReference(bytes, kind) {
  assert.ok(Object.hasOwn(referenceHashes, kind), "Select one maintained GUI science case.");
  // SMILES uses the actual earlier API read; the two supplied cases use raw
  // native result artifacts. These are explicit forms, not decoding fallbacks.
  const content = kind === "smiles" ? admitReceipt(parsedJSON(bytes, "retained API read")).bytes : bytes;
  assert.equal(sha256(content), referenceHashes[kind], "The retained scientific reference differs from the recorded qualification.");
  return parsedJSON(content, "retained scientific result");
}

export function admitResultHref(href, origin, jobRef, credentialRef) {
  const url = new URL(href, origin), prefix = `/v1b/jobs/${jobRef}/analysis-results/`;
  assert.ok(url.origin === origin && !url.username && !url.password && url.pathname.startsWith(prefix) && url.search === "", "Open only this Job's actual GUI result link.");
  const resultRef = url.pathname.slice(prefix.length);
  assert.match(resultRef, /^analysis_result:sha256:[0-9a-f]{64}$/u);
  const fragment = new URLSearchParams(url.hash.slice(1));
  assert.ok([...fragment.keys()].length === 1 && fragment.get("credential") === credentialRef, "The viewer must use the imported user credential reference.");
  return resultRef;
}

export function renderedFailure({ jobRef, attemptRef, code, message }) {
  const bounded = message.length <= 4096 ? message : message.slice(0, 4096) + " [truncated]";
  return `The GUI reports failed Attempt ${attemptRef} for Job ${jobRef} (${code}): ${bounded} ` +
    "Inspect this existing Job; the qualification will not resubmit it.";
}

function fact(region, label) {
  return region.locator("dt").filter({ hasText: new RegExp(`^${label}$`, "u") }).locator("xpath=following-sibling::dd[1]");
}

async function identifier(region, label) {
  const value = await fact(region, label).locator("code.opaque-identifier-value").getAttribute("aria-label");
  assert.ok(value && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/u.test(value), "The GUI must expose a complete scoped identifier, not compacted text.");
  return value;
}

export async function observeRequests(context, requests, outputPath) {
  const inFlight = new Map();
  let writes = Promise.resolve(), failure = null;
  const record = () => {
    const snapshot = JSON.stringify(requests.snapshot()) + "\n";
    writes = writes.then(() => writeFile(outputPath, snapshot, { mode: 0o600 }));
    // Keep the rejected promise for settlement, without an unhandled callback.
    void writes.catch(() => {});
    return writes;
  };
  context.on("response", response => {
    const index = inFlight.get(response.request());
    if (index === undefined) return;
    try {
      requests.respond(index, response.status(), response.headers()["x-request-id"]);
      void record();
    } catch (cause) { failure ??= cause; }
  });
  context.on("requestfinished", request => { inFlight.delete(request); });
  context.on("requestfailed", request => {
    const index = inFlight.get(request);
    if (index === undefined) return;
    inFlight.delete(request);
    try {
      const cause = requests.transportFailed(index, request.failure()?.errorText);
      failure = failure ? new AggregateError([failure, cause], "GUI request observation and transport failed.") : cause;
      void record();
    }
    catch (cause) { failure ??= cause; }
  });
  await record();
  await context.route("**/*", async route => {
    try {
      await writes;
      if (failure) throw failure;
      const request = route.request(), path = new URL(request.url()).pathname, previousJob = requests.observedJobRef;
      if (path.startsWith("/v1/")) assert.ok(request.headers()["signature-input"], "The normal GUI must sign its own API requests.");
      const index = requests.admit(request.method(), request.url(), request.postDataBuffer());
      if (index !== null || requests.observedJobRef !== previousJob) await record();
      if (index !== null) {
        inFlight.set(request, index);
        console.info("GUI submission dispatched:", JSON.stringify(requests.snapshot().effects[index]));
      }
      await route.continue();
    } catch (cause) {
      failure ??= cause;
      try { await route.abort("blockedbyclient"); }
      catch (cleanup) { failure = new AggregateError([failure, cleanup], "GUI request admission and request retirement failed."); }
    }
  });
  return { record, async settle() {
    let writeFailure;
    try { await writes; } catch (cause) { writeFailure = cause; }
    if (failure && writeFailure && failure !== writeFailure && !(failure instanceof AggregateError && failure.errors.includes(writeFailure))) {
      failure = new AggregateError([failure, writeFailure], "GUI request observation and receipt persistence failed.");
    }
    failure ??= writeFailure;
    if (failure) throw failure;
  } };
}

async function selectProjectAndAnalysis(page, projectRef) {
  await page.getByRole("button", { name: /^Show projects for / }).locator("..").click();
  await page.getByRole("button", { name: `Select Project ${projectRef}`, exact: true }).locator("..").click();
  await page.getByRole("region", { name: "Analyses", exact: true }).locator("article")
    .filter({ hasText: "peaks_from_mol" }).getByRole("button", { name: /^Select / }).locator("..").click();
}

async function uploadSdf(page, requests, observations) {
  const source = await boundedFile("/fixture/butane_conformers.sdf", 1024 * 1024);
  assert.equal(sha256(source), "c8007f5a1a80ad080bb972caf005bbb4fedcd5a9bd15b892e1b640d2f01e4374", "Use the unchanged R46 authored SDF.");
  const uploads = page.getByRole("region", { name: "Uploads", exact: true });
  const details = page.getByRole("region", { name: "Upload details", exact: true });
  await uploads.getByRole("button", { name: "Add upload", exact: true }).click();
  const chooser = details.getByLabel("Choose file to upload", { exact: true });
  await chooser.setInputFiles({ name: "butane_conformers.sdf", mimeType: "chemical/x-mdl-sdfile", buffer: source });
  await details.getByLabel("New Upload description", { exact: true }).fill("NMRNet GUI qualification: supplied two-record SDF");
  const publish = details.getByRole("button", { name: "Upload file", exact: true });
  await publish.and(page.locator(":enabled")).waitFor();
  assert.ok(await chooser.evaluate(input => input.files[0].size <= 1024 * 1024), "The actual GUI archive exceeds the admitted source ceiling.");
  const archiveBytes = Buffer.from(await chooser.evaluate(async input => Array.from(new Uint8Array(await input.files[0].arrayBuffer()))));
  // The existing contained reader owns ZIP decoding. The final scientific
  // source must join this actual outer hash to the unchanged original member
  // hash; the witness needs neither another ZIP decoder nor another download.
  requests.selectArchive({ sha256: sha256(archiveBytes), byte_length: archiveBytes.length, member_name: "butane_conformers.sdf" });
  await observations.record();
  await publish.click();
  // The GUI itself owns reservation receipt admission and automatic transfer.
  // Observe completion, then inspect the selected resource through normal UI.
  await uploads.getByRole("button", { name: "Add upload", exact: true }).and(page.locator(":enabled")).waitFor();
  assert.ok(requests.uploadRef, "The GUI did not identify its publication; retain its original operation without resubmitting.");
  await uploads.getByRole("button", { name: `Show Upload ${requests.uploadRef}`, exact: true }).locator("..").click();
  await details.getByRole("button", { name: "Additional Upload details", exact: true }).click();
  await fact(details, "Finalized").locator("time").waitFor();
  assert.equal((await fact(details, "Size").textContent()).trim(), `${archiveBytes.length.toLocaleString("en-US")} bytes`);
  requests.confirmUpload({ upload_ref: await identifier(details, "Reference"), project_ref: await identifier(details, "Project"),
    creator_ref: await identifier(details, "Created by"), byte_length: archiveBytes.length, finalized: true });
  await observations.record();
}

async function createAndConfigureJob(page, requests, observations) {
  const details = page.getByRole("region", { name: "Job details", exact: true });
  await page.getByRole("region", { name: "Jobs", exact: true }).getByRole("button", { name: "Add job", exact: true }).click();
  await details.getByLabel("New Job description", { exact: true }).fill("NMRNet GUI qualification");
  await details.getByRole("textbox", { name: "Analysis input", exact: true }).fill(requests.specification);
  await details.getByRole("button", { name: "Create job", exact: true }).click();
  const reveal = details.getByRole("button", { name: "Show more of Additional Job details", exact: true });
  await reveal.click(); await reveal.click();
  requests.bindJob({ job_ref: await identifier(details, "Reference"), project_ref: await identifier(details, "Project"),
    submitter_ref: await identifier(details, "Submitter"), operation_ref: await identifier(details, "Operation"),
    specification_fingerprint: await identifier(details, "Input fingerprint") });
  await observations.record();
  if (requests.confirmedUploadRef) {
    const selection = details.getByRole("region", { name: "Job uploads", exact: true });
    await selection.getByRole("button", { name: "Edit upload selection", exact: true }).click();
    await page.getByRole("region", { name: "Uploads", exact: true })
      .getByRole("checkbox", { name: `Use ${requests.confirmedUploadRef} for this Job`, exact: true }).check();
    await selection.getByRole("button", { name: "Save upload selection", exact: true }).click();
  }
  const providers = details.getByRole("region", { name: "Job providers", exact: true });
  await providers.getByRole("button", { name: "Edit provider selection", exact: true }).click();
  const provider = page.getByRole("region", { name: "Providers", exact: true }).locator("article")
    .filter({ has: page.getByLabel(requests.providerRef, { exact: true }) });
  await provider.getByRole("checkbox").check();
  await providers.getByRole("button", { name: "Save provider selection", exact: true }).click();
}

async function refreshJobRegion(page, region, label, requests, suffix) {
  const refresh = region.getByRole("button", { name: label, exact: true });
  const [request] = await Promise.all([
    page.waitForRequest(request => request.method() === "GET" && new URL(request.url()).origin === requests.origin &&
      new URL(request.url()).pathname === `/v1/jobs/${requests.jobRef}/${suffix}`),
    refresh.click()
  ]);
  const response = await request.response();
  await refresh.and(page.locator(':not([aria-busy="true"])')).waitFor();
  const alerts = region.getByRole("alert"), count = await alerts.count();
  if (response?.status() !== 200 || count > 0) {
    const guidance = count ? (await alerts.first().textContent()).trim().slice(0, 4096) : "No additional GUI guidance was received.";
    return `GUI ${suffix} read for ${requests.jobRef}: ${response ? `HTTP ${response.status()}` : "no HTTP response"}; ${guidance}`;
  }
  return null;
}

async function failedAttempt(page, requests) {
  const attempts = page.getByRole("region", { name: "Attempts", exact: true });
  const readError = await refreshJobRegion(page, attempts, "Refresh attempts", requests, "execution-attempts");
  if (readError) return { readError };
  const own = attempts.locator("article.column-execution-attempt").filter({ has: page.getByLabel(requests.providerRef, { exact: true }) });
  if (await own.count() === 0) return null;
  const current = own.and(page.locator('[data-execution-attempt-state="in_progress"]'));
  if (await current.count() > 0) return { reference: await identifier(current.first(), "Reference"), state: "in_progress" };
  const failed = own.and(page.locator('[data-execution-attempt-state="failed"]'));
  if (await failed.count() === 0) return { reference: await identifier(own.first(), "Reference"), state: await own.first().getAttribute("data-execution-attempt-state") };
  // Newest-first list, using only the provider's existing public failure surface.
  const latest = failed.first();
  throw new Error(renderedFailure({ jobRef: requests.jobRef, attemptRef: await identifier(latest, "Reference"),
    code: (await fact(latest, "Failure code").textContent()).trim().slice(0, 128),
    message: (await fact(latest, "Failure message").locator(".progressive-text__content").textContent()).trim() }));
}

async function openResult(page, requests, credentialRef, observations) {
  const results = page.getByRole("region", { name: "Results", exact: true }), deadline = Date.now() + 900000;
  const links = results.getByRole("link", { name: /^Open Result / });
  let lastAttempt = null, lastReadError = null;
  while (await links.count() === 0) {
    await observations.settle();
    assert.ok(Date.now() < deadline, `The GUI received no result for ${requests.jobRef} within 900 seconds; ` +
      `last visible Attempt ${lastAttempt?.reference ?? "not received"}, state ${lastAttempt?.state ?? "unknown"}. ` +
      `Last GUI read error: ${lastReadError ?? "none observed"}. Retain this Job without resubmitting.`);
    const readError = await refreshJobRegion(page, results, "Refresh results", requests, "analysis-results");
    if (readError) lastReadError = readError;
    if (await links.count() === 0) {
      const observed = await failedAttempt(page, requests);
      if (observed?.readError) lastReadError = observed.readError;
      else if (observed) lastAttempt = observed;
      await page.waitForTimeout(2000);
    }
  }
  assert.equal(await links.count(), 1, "The selected provider must deliver one result for this Job.");
  const resultRef = admitResultHref(await links.getAttribute("href"), requests.origin, requests.jobRef, credentialRef);
  const pending = page.waitForEvent("popup");
  await links.click();
  return { viewer: await pending, resultRef };
}

async function qualify(t) {
  assert.equal(process.env.NMRNET_IN_CONTAINER, "1", "Use the maintained contained GUI submission target.");
  const started = performance.now();
  const { values } = parseArgs({ options: { "gui-origin": { type: "string" }, "api-origin": { type: "string" },
    "proxy-port": { type: "string" }, "project-ref": { type: "string" }, "case-kind": { type: "string" } } });
  const origin = admitOrigin(values["gui-origin"]), kind = values["case-kind"];
  assert.equal(admitOrigin(values["api-origin"]), origin, "The selected GUI must already use the same public API origin.");
  const retained = admitReference(await boundedFile("/input/retained-result.json", 1024 * 1024), kind);
  const fixture = parsedJSON(await boundedFile("/fixture/case.json", 65536), "authored GUI case");
  const identity = parsedJSON(await boundedFile("/input/identity.json", 65536), "admitted startup identity");
  assert.equal(identity.origin, origin);
  assert.ok(identity.interpretation_scripted === false && identity.interpreter && Number.isSafeInteger(identity.interpreter_max_turns) && identity.interpreter_max_turns > 0,
    "Use the admitted real-interpreter configuration and its turn limit.");
  const credentialText = (await boundedFile("/input/user.json", 65536)).toString("utf8");
  let credential;
  try { credential = JSON.parse(credentialText); } catch { throw new Error("The user credential is not valid JSON; its contents were excluded."); }
  assert.ok(typeof credential.credential_ref === "string", "The imported user credential must identify its saved GUI reference.");
  const requests = new SubmissionRequests({ origin, projectRef: values["project-ref"], providerRef: identity.provider_ref,
    principalRef: credential.principal_ref, specification: fixture.specification, kind, archive: null });
  const context = await browserContext(t, await boundedFile("/input/tls.crt", 65536), values["proxy-port"]);
  const observations = await observeRequests(context, requests, "/output/submission-observations.json");
  let failure;
  try {
    const { workspace, sourceCommit } = await openWorkspace(context, origin, credentialText);
    await selectProjectAndAnalysis(workspace, requests.projectRef);
    if (kind === "sdf-zip") await uploadSdf(workspace, requests, observations);
    await createAndConfigureJob(workspace, requests, observations);
    assert.ok(performance.now() - started <= 120000, "GUI setup exceeded its allowance; keep the created Job closed rather than start work outside the readback budget.");
    await workspace.getByRole("button", { name: "Resume Job", exact: true }).click();
    const { viewer, resultRef } = await openResult(workspace, requests, credential.credential_ref, observations);
    const received = await receiveContent(viewer), details = viewer.locator(".result-details");
    const attemptRef = await identifier(details, "Attempt");
    assert.equal(await identifier(details, "Reference"), resultRef);
    assert.equal(await identifier(details, "Provider"), identity.provider_ref);
    const fingerprint = `sha256:${sha256(received.bytes)}`;
    assert.equal(await identifier(details, "SHA-256"), fingerprint);
    assert.equal((await fact(details, "Size").textContent()).trim(), `${received.bytes.length.toLocaleString("en-US")} bytes`);
    assert.equal(await details.locator(".result-details__schema-heading strong").getAttribute("title"), "nmrnet.coordinate_shifts.result.v2");
    await viewer.getByLabel(requests.jobRef, { exact: true }).waitFor();
    await writeFile("/output/downloaded-result.bin", received.bytes, { flag: "wx", mode: 0o600 });
    compareSubmittedScience(received.document, retained, { jobRef: requests.jobRef, attemptRef,
      specificationFingerprint: `sha256:${sha256(Buffer.from(requests.specification))}`, uploadRef: requests.confirmedUploadRef,
      archive: requests.archive, interpreter: identity.interpreter, turnLimit: identity.interpreter_max_turns }, kind);
    await observations.settle();
    requests.complete();
    await writeFile("/output/passed.json", JSON.stringify({ case_kind: kind, gui_origin: origin, gui_source_commit: sourceCommit,
      job_ref: requests.jobRef, upload_ref: requests.confirmedUploadRef, analysis_result_ref: resultRef, execution_attempt_ref: attemptRef,
      result_fingerprint: fingerprint, result_byte_length: received.bytes.length, reference_sha256: referenceHashes[kind],
      archive: requests.archive, interpretation: received.document.interpretation, formatted_json: true, original_text: true, exact_download: true,
      exact_retained_science: true, effects: requests.snapshot().effects }) + "\n", { flag: "wx", mode: 0o600 });
  } catch (cause) { failure = cause; }
  try { await observations.settle(); }
  catch (cause) { if (failure !== cause) failure = failure ? new AggregateError([failure, cause], "GUI qualification and observation settlement failed.") : cause; }
  console.info("GUI submission observations:", JSON.stringify(requests.snapshot()));
  if (failure) throw failure;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  // setup120 + readback900 + final poll30 + receiving90 + close15 < whole1200;
  // the host allows1220, then preserves its original retirement ownership.
  test("normal GUI submission delivers retained NMRNet science", { timeout: 1200000 }, qualify);
}
