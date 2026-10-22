// Adapted JSON/text/download and saved-user route from these exact GUI owners:
// https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/tests/e2e/signed-result-content-proof.mjs#L4-L30
// https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/tests/e2e/signed-job-lifecycle.test.mjs#L55-L63
// https://github.com/numpde/nmr-gui-v1/blob/df172527409cc08d46b3318cd19e961f6c3be432/app/src/features/result-viewer/result-viewer-route.ts#L36-L39
// Candidate-list molecule assertions from that donor do not apply to shifts.
import assert from "node:assert/strict";
import { createHash, X509Certificate } from "node:crypto";
import { mkdir, open, readFile, writeFile } from "node:fs/promises";
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
  assert.equal(receipt.result_schema_id, "nmrnet.coordinate_shifts.result.v1", "Retain the NMRNet result schema identity.");
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

async function openViewer(context, origin, reference, credentialText) {
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
  console.info("GUI receiver: credential imported; opening the existing result.");
  const viewer = await context.newPage();
  const credentialRef = JSON.parse(credentialText).credential_ref;
  const { job_ref: job, analysis_result_ref: result } = reference.receipt;
  await viewer.goto(`${origin}/v1b/jobs/${job}/analysis-results/${result}#credential=${encodeURIComponent(credentialRef)}`);
  return { viewer, sourceCommit: configuration.sourceCommit };
}

async function qualifyContent(viewer, reference) {
  const formatted = viewer.getByRole("tabpanel", { name: "Formatted JSON" });
  await formatted.waitFor();
  assert.deepEqual(JSON.parse(await formatted.textContent()), JSON.parse(reference.text),
    "Displayed atoms, shifts, units, geometries and provenance must match the retained result.");
  await viewer.getByRole("tab", { name: "Text", exact: true }).click();
  assert.equal(await viewer.getByRole("tabpanel", { name: "Text", exact: true }).textContent(), reference.text);
  const link = viewer.getByRole("link", { name: "Download result bytes", exact: true });
  const pending = viewer.waitForEvent("download");
  await link.click();
  const download = await pending;
  assert.equal(await download.failure(), null);
  const path = await download.path();
  assert.ok(path, "The actual browser download must complete.");
  const bytes = await readFile(path);
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
  const certificate = new X509Certificate(await boundedFile("/input/tls.crt", 65536));
  const spki = createHash("sha256").update(certificate.publicKey.export({ type: "spki", format: "der" })).digest("base64");
  const { chromium } = await import("playwright");
  await mkdir("/tmp/home", { mode: 0o700, recursive: true });
  console.info("GUI receiver: existing result admitted; launching the browser.");
  const browser = await chromium.launch({ timeout: 15000, args: [`--ignore-certificate-errors-spki-list=${spki}`],
    proxy: { server: `http://127.0.0.1:${values["proxy-port"]}`, bypass: "<-loopback>" } });
  // A close timeout cannot prove retirement; the host retains exact-CID ownership.
  t.after(() => browser.close(), { timeout: 15000 });
  console.info("GUI receiver: browser ready; opening the deployed workspace.");
  const context = await browser.newContext({ serviceWorkers: "block" });
  context.setDefaultTimeout(10000);
  context.setDefaultNavigationTimeout(10000);
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
  await qualifyContent(viewer, reference);
  const viewerReads = reads.filter(response => response.frame().page() === viewer);
  assert.deepEqual(viewerReads.map(response => new URL(response.url()).pathname), paths);
  for (const response of viewerReads) {
    assert.equal(response.status(), 200, "The viewer must receive its own successful API reads.");
    assert.ok(response.request().headers()["signature-input"], "The GUI must sign its own GET requests.");
  }
  const received = await viewerReads[1].json();
  for (const field of ["job_ref", "analysis_result_ref", "execution_attempt_ref", "provider_ref", "result_schema_id",
    "result_fingerprint", "result_byte_length"]) {
    assert.equal(received[field], reference.receipt[field], `The live result's ${field} differs from the retained receipt.`);
  }
  assert.deepEqual(denied, [], "No mutation or unrelated destination is permitted.");
  await writeFile("/output/passed.json", JSON.stringify({
    gui_origin: origin, api_origin: origin, gui_source_commit: sourceCommit, job_ref: reference.receipt.job_ref,
    analysis_result_ref: reference.receipt.analysis_result_ref,
    result_fingerprint: reference.receipt.result_fingerprint,
    result_byte_length: reference.bytes.length,
    retained_receipt_sha256: createHash("sha256").update(rawReceipt).digest("hex"),
    formatted_json: true, original_text: true, exact_download: true,
    signed_get_paths: paths, api_mutations: 0, denied_requests: denied
  }) + "\n", { flag: "wx", mode: 0o600 });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  test("deployed GUI displays and downloads existing NMRNet science", { timeout: 90000 }, qualify);
}
