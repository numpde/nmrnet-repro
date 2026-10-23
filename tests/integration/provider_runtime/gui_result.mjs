// Adapted JSON/text/download and saved-user route from these exact GUI owners:
// https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/tests/e2e/signed-result-content-proof.mjs#L4-L30
// https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/tests/e2e/signed-job-lifecycle.test.mjs#L55-L63
// https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/app/src/features/result-viewer/result-viewer-route.ts#L36-L39
// Candidate-list molecule assertions from that donor do not apply to shifts.
import assert from "node:assert/strict";
import { createHash, X509Certificate } from "node:crypto";
import { mkdir, open, writeFile } from "node:fs/promises";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import test from "node:test";

export function admitOrigin(value) {
  const url = new URL(value);
  assert.ok(url.protocol === "https:" && url.origin === value && !url.username && !url.password && url.port !== "0",
    "GUI/API inputs must be canonical HTTPS origins without credentials or port zero.");
  return url.origin;
}

export function admitReceipt(receipt) {
  const encoded = receipt.canonical_result_base64;
  assert.equal(typeof encoded, "string", "Retain the original API result-read response.");
  const bytes = Buffer.from(encoded, "base64");
  assert.equal(bytes.toString("base64"), encoded, "Retained base64 must be canonical.");
  assert.equal(bytes.length, receipt.result_byte_length, "Retained result length differs.");
  assert.equal(`sha256:${createHash("sha256").update(bytes).digest("hex")}`, receipt.result_fingerprint,
    "Retained result fingerprint differs.");
  for (const name of ["job_ref", "analysis_result_ref", "execution_attempt_ref", "provider_ref"]) {
    assert.ok(typeof receipt[name] === "string" && /^[A-Za-z0-9:_-]+$/u.test(receipt[name]),
      "The retained read response must identify its Job and result.");
  }
  // Readback verifies retained bytes without translating historical v1 results.
  assert.ok(["nmrnet.coordinate_shifts.result.v1", "nmrnet.coordinate_shifts.result.v2"].includes(receipt.result_schema_id),
    "Retain the historical v1 or current v2 NMRNet result schema identity.");
  return { receipt, bytes, text: new TextDecoder("utf-8", { fatal: true }).decode(bytes) };
}

export function permitRequest(url, method, origins, apiPaths) {
  const destination = new URL(url);
  if (method !== "GET" || !origins.has(destination.origin)) return false;
  if (`${destination.pathname}/`.startsWith("/v1/")) {
    return apiPaths.has(destination.pathname) && destination.search === "";
  }
  return true;
}

export async function boundedFile(path, maximum) {
  const file = await open(path, "r");
  let failure;
  try {
    const bytes = Buffer.alloc(maximum + 1);
    let used = 0;
    while (used < bytes.length) {
      const { bytesRead } = await file.read(bytes, used, bytes.length - used, null);
      if (bytesRead === 0) break;
      used += bytesRead;
    }
    assert.ok(used <= maximum, "Qualification input exceeds its byte limit.");
    return bytes.subarray(0, used);
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try { await file.close(); } catch (cleanup) {
      if (failure) throw new AggregateError([failure, cleanup], "Qualification input read and close both failed.");
      throw cleanup;
    }
  }
}

export async function openWorkspace(context, origin, credentialText) {
  const workspace = await context.newPage();
  await workspace.goto(`${origin}/v1b`);
  console.info("GUI receiver: workspace loaded; importing the user credential.");
  const configuration = await workspace.evaluate(() => ({
    apiOrigin: window.__NMR_GUI_RUNTIME_CONFIG__.nmrApiOrigin,
    sourceCommit: window.__NMR_GUI_RUNTIME_CONFIG__.sourceCommit ?? null
  }));
  assert.equal(configuration.apiOrigin, origin,
    "The deployed GUI must already use the selected API; this proof never overrides its configuration.");
  try {
    await workspace.getByLabel("Use a credential", { exact: true }).fill(credentialText);
  } catch {
    // Playwright's fill failure can echo its argument. Do not retain that key.
    throw new Error("The GUI credential textbox could not be filled; credential contents were excluded from this error.");
  }
  await workspace.getByRole("button", { name: "Import", exact: true }).click();
  await workspace.getByRole("button", { name: /^Show projects for / }).waitFor();
  return { workspace, sourceCommit: configuration.sourceCommit };
}

async function openViewer(context, origin, reference, credentialText) {
  const { sourceCommit } = await openWorkspace(context, origin, credentialText);
  console.info("GUI receiver: credential imported; opening the existing result.");
  const viewer = await context.newPage();
  const credentialRef = JSON.parse(credentialText).credential_ref;
  const { job_ref: job, analysis_result_ref: result } = reference.receipt;
  await viewer.goto(`${origin}/v1b/jobs/${job}/analysis-results/${result}#credential=${encodeURIComponent(credentialRef)}`);
  return { viewer, sourceCommit };
}

export async function receiveContent(viewer) {
  const formatted = viewer.getByRole("tabpanel", { name: "Formatted JSON" });
  await formatted.waitFor();
  const document = JSON.parse(await formatted.textContent());
  await viewer.getByRole("tab", { name: "Text", exact: true }).click();
  const text = await viewer.getByRole("tabpanel", { name: "Text", exact: true }).textContent();
  assert.deepEqual(document, JSON.parse(text), "Formatted JSON and original Text must describe the same result.");
  const link = viewer.getByRole("link", { name: "Download result bytes", exact: true });
  const pending = viewer.waitForEvent("download");
  await link.click();
  const download = await pending;
  assert.equal(await download.failure(), null);
  const path = await download.path();
  assert.ok(path, "The actual browser download must complete.");
  const bytes = await boundedFile(path, 1024 * 1024);
  assert.equal(new TextDecoder("utf-8", { fatal: true }).decode(bytes), text,
    "The browser download must preserve the exact original Text bytes.");
  return { document, bytes, text };
}

export async function browserContext(t, certificateBytes, proxyPort) {
  assert.ok(/^[1-9][0-9]{0,4}$/u.test(proxyPort) && Number(proxyPort) <= 65535, "Use the admitted browser proxy port.");
  const certificate = new X509Certificate(certificateBytes);
  const spki = createHash("sha256").update(certificate.publicKey.export({ type: "spki", format: "der" })).digest("base64");
  const { chromium } = await import("playwright");
  await mkdir("/tmp/home", { mode: 0o700, recursive: true });
  const browser = await chromium.launch({ timeout: 15000, args: [`--ignore-certificate-errors-spki-list=${spki}`],
    proxy: { server: `http://127.0.0.1:${proxyPort}`, bypass: "<-loopback>" } });
  // A close timeout cannot prove retirement; the host retains exact-CID ownership.
  t.after(() => browser.close(), { timeout: 15000 });
  const context = await browser.newContext({ serviceWorkers: "block" });
  context.setDefaultTimeout(10000);
  context.setDefaultNavigationTimeout(10000);
  return context;
}

async function qualifyContent(viewer, reference) {
  const { document, text, bytes } = await receiveContent(viewer);
  assert.deepEqual(document, JSON.parse(reference.text),
    "Displayed atoms, shifts, units, geometries and provenance must match the retained result.");
  assert.equal(text, reference.text);
  assert.deepEqual(bytes, reference.bytes, "Downloaded bytes must equal the earlier API science, without recomputation.");
  await writeFile("/output/downloaded-result.bin", bytes, { flag: "wx", mode: 0o600 });
}

async function qualify(t) {
  assert.equal(process.env.NMRNET_IN_CONTAINER, "1", "Use make provider/qualify/gui-result for the contained runtime.");
  const { values } = parseArgs({ options: {
    "gui-origin": { type: "string" }, "api-origin": { type: "string" }, "proxy-port": { type: "string" }
  } });
  const origin = admitOrigin(values["gui-origin"]);
  assert.equal(admitOrigin(values["api-origin"]), origin, "This GET-only lane qualifies a GUI and API at the same origin.");
  assert.match(values["proxy-port"], /^[1-9][0-9]{0,4}$/u);
  const rawReceipt = await boundedFile("/input/result-read.json", 1024 * 1024);
  const reference = admitReceipt(JSON.parse(rawReceipt.toString("utf8")));
  const credentialText = (await boundedFile("/input/user.json", 65536)).toString("utf8");
  try { JSON.parse(credentialText); } catch { throw new Error("The user credential is not valid JSON; its contents were excluded."); }
  console.info("GUI receiver: existing result admitted; launching the browser.");
  const context = await browserContext(t, await boundedFile("/input/tls.crt", 65536), values["proxy-port"]);
  console.info("GUI receiver: browser ready; opening the deployed workspace.");
  const denied = [], reads = [];
  const { job_ref: job, analysis_result_ref: result } = reference.receipt;
  const paths = [`/v1/jobs/${job}`, `/v1/jobs/${job}/analysis-results/${result}`];
  context.on("response", response => {
    const url = new URL(response.url());
    if (url.origin === origin && paths.includes(url.pathname)) reads.push(response);
  });
  await context.route("**/*", async route => {
    const request = route.request();
    if (!permitRequest(request.url(), request.method(), new Set([origin]), new Set(paths))) {
      denied.push({ method: request.method(), origin: new URL(request.url()).origin, path: new URL(request.url()).pathname });
      await route.abort("blockedbyclient");
    } else await route.continue();
  });
  const { viewer, sourceCommit } = await openViewer(context, origin, reference, credentialText);
  await viewer.getByRole("tabpanel", { name: "Formatted JSON" }).waitFor();
  const viewerReads = reads.filter(response => response.frame().page() === viewer);
  assert.deepEqual(viewerReads.map(response => new URL(response.url()).pathname), paths);
  for (const response of viewerReads) {
    assert.equal(response.status(), 200, "The viewer must receive its own successful API reads.");
    assert.ok(response.request().headers()["signature-input"], "The GUI must sign its own GET requests.");
  }
  // Assert identities at their receiving surface, without depending on a
  // second body read from a browser-owned response handle.
  // https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/app/src/features/result-details/ResultDetails.tsx#L115-L213
  const details = viewer.locator(".result-details");
  await viewer.getByLabel(reference.receipt.job_ref, { exact: true }).waitFor();
  for (const field of ["analysis_result_ref", "execution_attempt_ref", "provider_ref", "result_fingerprint"]) {
    await details.getByLabel(reference.receipt[field], { exact: true }).waitFor();
  }
  await details.getByText(reference.receipt.result_schema_id, { exact: true }).waitFor();
  await details.getByText(`${reference.bytes.length.toLocaleString("en-US")} bytes`, { exact: true }).waitFor();
  await qualifyContent(viewer, reference);
  assert.deepEqual(denied, [], "No mutation or unrelated destination is permitted.");
  await writeFile("/output/passed.json", JSON.stringify({
    gui_origin: origin, api_origin: origin, gui_source_commit: sourceCommit, job_ref: reference.receipt.job_ref,
    analysis_result_ref: reference.receipt.analysis_result_ref,
    result_fingerprint: reference.receipt.result_fingerprint,
    result_byte_length: reference.bytes.length,
    retained_receipt_sha256: createHash("sha256").update(rawReceipt).digest("hex"),
    rendered_result_identity: true, formatted_json: true, original_text: true, exact_download: true,
    signed_get_paths: paths, api_mutations: 0, denied_requests: denied
  }) + "\n", { flag: "wx", mode: 0o600 });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  test("deployed GUI displays and downloads existing NMRNet science", { timeout: 90000 }, qualify);
}
