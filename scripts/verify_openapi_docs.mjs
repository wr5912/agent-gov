#!/usr/bin/env node
// Read-only real Swagger UI acceptance. It never clicks Try it out/Execute and
// never sends a model or authenticated business request.
import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import process from "node:process";
import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";

requireContainerAcceptance();

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const apiBase = (process.env.RUNTIME_API_BASE || "http://localhost:58080").trim().replace(/\/$/, "");
const apiOrigin = new URL(apiBase).origin;
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || mkdtempSync(join(tmpdir(), "agentgov-openapi-docs-"));
const responsesDocsUrl = `${apiBase}/docs#/openai-responses/create_response_v1_responses_post`;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function references(fragment, arrayItem = false) {
  const found = [];
  if (Array.isArray(fragment)) {
    for (const child of fragment) found.push(...references(child, arrayItem));
  } else if (fragment && typeof fragment === "object") {
    if (typeof fragment.$ref === "string" && fragment.$ref.startsWith("#/components/schemas/")) {
      found.push([fragment.$ref.split("/").at(-1), arrayItem]);
    }
    for (const [key, child] of Object.entries(fragment)) {
      found.push(...references(child, arrayItem || key === "items"));
    }
  }
  return [...new Map(found.map((item) => [JSON.stringify(item), item])).values()];
}

function flattenedPaths(root, components) {
  const paths = [];
  function walk(component, prefix, stack) {
    for (const [name, field] of Object.entries(component.properties || {})) {
      const path = prefix ? `${prefix}.${name}` : name;
      paths.push(path);
      for (const [reference, arrayItem] of references(field)) {
        if (stack.has(reference) || !components[reference]) continue;
        walk(components[reference], arrayItem ? `${path}[]` : path, new Set([...stack, reference]));
      }
    }
  }
  walk(root, "", new Set(["ResponsesRequest"]));
  return paths;
}

async function findOperation(page, path, method) {
  const blocks = page.locator(".swagger-ui .opblock");
  const count = await blocks.count();
  for (let index = 0; index < count; index += 1) {
    const block = blocks.nth(index);
    const actualPath = (await block.locator(".opblock-summary-path").first().textContent())?.trim();
    const actualMethod = (await block.locator(".opblock-summary-method").first().textContent())?.trim().toLowerCase();
    if (actualPath === path && actualMethod === method) return block;
  }
  return null;
}

async function openOperation(page, schema, path, method) {
  const contract = schema.paths?.[path]?.[method];
  const tag = contract?.tags?.[0];
  const operationId = contract?.operationId;
  assert(tag && operationId, `OpenAPI operation lacks tag/operationId: ${method.toUpperCase()} ${path}`);
  const expectedHash = `#/${encodeURIComponent(tag)}/${encodeURIComponent(operationId)}`;
  // Swagger UI does not remount another tag when only the hash changes in the
  // existing document. Start from a blank document for each exact operation.
  await page.goto("about:blank");
  await page.goto(`${apiBase}/docs${expectedHash}`, { waitUntil: "networkidle", timeout: 60000 });
  await page.locator(".swagger-ui").waitFor({ timeout: 30000 });
  let block = await findOperation(page, path, method);
  if (block === null) {
    await page.waitForTimeout(500);
    block = await findOperation(page, path, method);
  }
  assert(block !== null, `Swagger operation not found after exact hash navigation: ${method.toUpperCase()} ${path}`);
  if (!(await block.locator(".opblock-body").isVisible().catch(() => false))) {
    await block.locator(".opblock-summary").click();
  }
  await block.locator(".opblock-body").waitFor({ state: "visible", timeout: 15000 });
  return block;
}

async function main() {
  const schemaResponse = await fetch(`${apiBase}/openapi.json`);
  assert(schemaResponse.ok, `OpenAPI JSON returned ${schemaResponse.status}`);
  const schema = await schemaResponse.json();
  const components = schema.components?.schemas || {};
  const responsesOperation = schema.paths?.["/v1/responses"]?.post;
  assert(responsesOperation, "live OpenAPI is missing POST /v1/responses");

  const responseFields = flattenedPaths(components.ResponsesRequest, components);
  assert(responseFields.length === 22, `Responses request graph exposes ${responseFields.length} fields instead of 22`);
  const responseExamples = responsesOperation.requestBody?.content?.["application/json"]?.examples || {};
  assert(
    JSON.stringify(Object.keys(responseExamples).sort())
      === JSON.stringify([
        "agentgov_control_stream",
        "agentgov_control_structured",
        "continue_with_conversation",
        "continue_with_previous_response_id",
        "strict_openai",
      ].sort()),
    "Responses request does not expose the reviewed five named examples",
  );
  const speech = components.AgentGovRequestExtension?.properties?.with_speech_summary;
  assert(speech?.default === false && speech?.examples?.includes(true), "with_speech_summary default/example is incomplete");
  assert(
    String(speech?.description).includes("stream=true")
      && String(speech?.description).includes("422")
      && String(speech?.description).includes("best-effort"),
    "with_speech_summary description omits stream, 422, or best-effort semantics",
  );

  const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
  const unexpectedWrites = [];
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === apiOrigin && request.method() !== "GET") {
      unexpectedWrites.push(`${request.method()} ${url.pathname}`);
    }
  });

  try {
    await page.goto(responsesDocsUrl, { waitUntil: "networkidle", timeout: 60000 });
    await page.locator(".swagger-ui").waitFor({ timeout: 30000 });
    const responses = await openOperation(page, schema, "/v1/responses", "post");
    const responsesText = await responses.innerText();
    assert(responsesText.includes("Parameters"), "Responses Swagger card does not render Parameters");
    assert(responsesText.includes("No parameters"), "Responses Swagger card does not render No parameters");
    assert(
      responsesText.includes("Parameters → No parameters") && responsesText.includes("Request body"),
      "Responses Swagger card does not explain that JSON inputs are under Request body",
    );
    for (const fieldPath of responseFields) {
      assert(responsesText.includes(fieldPath), `Responses Swagger field guide does not render ${fieldPath}`);
    }
    const responseOptionTexts = await responses.locator("select option").allTextContents();
    assert(
      responseOptionTexts.some((value) => value.includes("Control stream with every control switch"))
        || responseOptionTexts.length >= 5,
      "Responses Swagger request-body example selector does not render five scenarios",
    );
    assert(
      responsesText.includes("with_speech_summary")
        && responsesText.includes("stream=true")
        && responsesText.includes("best-effort"),
      "Responses Swagger card does not render the speech-summary condition",
    );
    mkdirSync(screenshotDir, { recursive: true });
    await page.screenshot({ path: join(screenshotDir, "responses-request-documentation.png"), fullPage: true });

    const sdk = await openOperation(page, schema, "/api/agent-runtime/sdk-events", "post");
    const sdkText = await sdk.innerText();
    assert(
      sdkText.includes("with_speech_summary") && sdkText.includes("best-effort"),
      "SDK-native Swagger card omits speech-summary request documentation",
    );

    const runs = await openOperation(page, schema, "/api/agent-runs", "get");
    const runsText = await runs.innerText();
    for (const parameter of ["run_id", "session_id", "alert_id", "case_id", "agent_id", "limit", "include_messages"]) {
      assert(runsText.includes(parameter), `query-heavy Agent runs card omits ${parameter}`);
    }
    const runParameters = schema.paths["/api/agent-runs"].get.parameters;
    assert(
      runParameters.every((parameter) => parameter.description && Object.hasOwn(parameter, "example")),
      "query-heavy Agent runs parameters are missing descriptions or examples",
    );

    const workspaceImport = await openOperation(page, schema, "/api/agent-registry/{agent_id}/workspace/import", "post");
    const importText = await workspaceImport.innerText();
    for (const field of ["package", "name", "expected_current_commit_sha", "reason"]) {
      assert(importText.includes(field), `multipart workspace-import card omits ${field}`);
    }
    const multipartProperties = schema.paths["/api/agent-registry/{agent_id}/workspace/import"].post.requestBody
      .content["multipart/form-data"].schema.properties;
    assert(
      Object.values(multipartProperties).every((property) => property.description && property.examples?.length),
      "multipart request properties are missing descriptions or examples",
    );

  } finally {
    await browser.close();
  }

  assert(unexpectedWrites.length === 0, `Swagger acceptance sent unexpected API writes: ${unexpectedWrites.join(", ")}`);
  assert(pageErrors.length === 0, `Swagger page errors: ${pageErrors.join(" | ")}`);
  console.log(`OPENAPI_DOCS_BROWSER_OK: ${responsesDocsUrl}; screenshot=${screenshotDir}`);
}

await main();
