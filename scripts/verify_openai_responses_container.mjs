#!/usr/bin/env node
// Real-container OpenAI Responses-first acceptance:
// browser UI loads from the Compose UI container, Playground sends /v1/responses,
// and hostile/boundary requests hit the Compose API container without mocks.
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const dockerEnv = readDockerEnv();
const uiBase = normalizeBase(process.env.RUNTIME_UI_BASE || `http://localhost:${dockerEnv.FRONTEND_HOST_PORT || "55173"}`);
const apiBase = normalizeBase(process.env.RUNTIME_API_BASE || `http://localhost:${dockerEnv.HOST_PORT || "58080"}`);
const browserApiBase = normalizeBase(process.env.RUNTIME_BROWSER_API_BASE || dockerEnv.FRONTEND_RUNTIME_API_BASE || apiBase);
const apiOrigins = new Set([new URL(apiBase).origin, new URL(browserApiBase).origin]);
const apiKey = process.env.RUNTIME_API_KEY || dockerEnv.FRONTEND_RUNTIME_API_KEY || dockerEnv.API_KEY || "";
const liveTimeoutMs = Number(process.env.OPENAI_RESPONSES_E2E_TIMEOUT_MS || 300000);
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || mkdtempSync(join(tmpdir(), "agentgov-openai-responses-"));

function normalizeBase(value) {
  return value.trim().replace(/\/$/, "");
}

function readDockerEnv() {
  const path = join(repoRoot, "docker", ".env");
  if (!existsSync(path)) return {};
  const result = {};
  for (const rawLine of readFileSync(path, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const index = line.indexOf("=");
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    result[key] = value;
  }
  return result;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (apiKey.trim()) headers.Authorization = `Bearer ${apiKey.trim()}`;
  return headers;
}

async function waitForHttpOk(url, label) {
  const deadline = Date.now() + 90000;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { headers: authHeaders() });
      if (response.ok) return;
      lastError = `${response.status} ${response.statusText}`;
    } catch (error) {
      lastError = String(error);
    }
    await delay(1000);
  }
  throw new Error(`${label} did not become ready at ${url}: ${lastError}`);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function api(path, { method = "GET", body, expected = [200], headers = {} } = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    method,
    headers: authHeaders({
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...headers,
    }),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!expected.includes(response.status)) {
    throw new Error(`${method} ${path} expected ${expected.join("/")} got ${response.status}: ${text.slice(0, 500)}`);
  }
  return { status: response.status, data, headers: response.headers };
}

async function expectStatus(path, status, body) {
  await api(path, { method: "POST", body, expected: [status] });
}

async function waitForCondition(check, message, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return;
    await delay(250);
  }
  throw new Error(message);
}

async function runHostileAndBoundaryChecks() {
  await expectStatus("/v1/responses", 422, { input: "hi", agentgov: {} });
  await expectStatus("/v1/responses", 422, { input: "hi", instructions: "replace the governed system prompt" });
  await expectStatus("/v1/responses", 422, { input: "hi", agentgov: { agent_id: "main-agent", updated_input: { command: "rm -rf /" } } });
  await expectStatus("/v1/responses", 422, { input: "hi", agentgov: { agent_id: "main-agent", max_turns: 0 } });
  await expectStatus("/api/chat", 422, { message: "legacy native chat must still require agent_id" });
  await expectStatus("/api/chat/stream", 422, { message: "legacy stream must still require agent_id" });
  await api("/v1/conversations/conv_missing/items", { expected: [404] });
  const created = await api("/v1/conversations", {
    method: "POST",
    body: { metadata: { source: "playwright-container", __agentgov_injected__: "must-strip" } },
  });
  assert(created.data?.metadata?.source === "playwright-container", "conversation metadata source was not preserved");
  assert(!("__agentgov_injected__" in (created.data?.metadata || {})), "reserved conversation metadata leaked back to client");
  await api(`/v1/conversations/${encodeURIComponent(created.data.id)}/items?limit=0`, { expected: [422] });
  await api(`/v1/conversations/${encodeURIComponent(created.data.id)}`, { method: "DELETE", expected: [200] });
}

async function main() {
  await waitForHttpOk(uiBase, "UI container");
  await waitForHttpOk(`${apiBase}/health`, "API container");
  await runHostileAndBoundaryChecks();

  const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  const apiRequests = [];
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!apiOrigins.has(url.origin)) return;
    apiRequests.push({ method: request.method(), path: url.pathname, postData: request.postData() || "" });
  });

  try {
    await page.addInitScript(({ base, key }) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: base, apiKey: key }));
      window.localStorage.removeItem("playground-session-messages");
      window.localStorage.removeItem("playground-active-session");
    }, { base: browserApiBase, key: apiKey });

    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 30000 });
    await waitForCondition(
      () => apiRequests.some((item) => item.path === "/v1/conversations") && apiRequests.some((item) => item.path === "/api/agent-registry"),
      "UI did not load sessions through /v1/conversations and agent registry",
    );

    const agentSelect = page.getByTestId("topbar-agent-switcher");
    await agentSelect.waitFor({ timeout: 30000 });
    await waitForCondition(
      async () => {
        const values = await agentSelect.locator("option").evaluateAll((options) => options.map((option) => option.value).filter(Boolean));
        return values.length > 0;
      },
      "business agent options did not render in topbar",
    );
    const optionValues = await agentSelect.locator("option").evaluateAll((options) => options.map((option) => option.value).filter(Boolean));
    const selectedAgent = optionValues.includes("main-agent") ? "main-agent" : optionValues[0];
    assert(selectedAgent, "no runnable business agent option found in topbar");
    await agentSelect.selectOption(selectedAgent);

    const requestPromise = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return apiOrigins.has(url.origin) && url.pathname === "/v1/responses" && request.method() === "POST";
    }, { timeout: 30000 });
    await page.getByTestId("chat-composer-input").fill("请只回复一行：AGENTGOV_OPENAI_E2E_OK。不要使用工具。");
    await page.getByTestId("chat-send").click();
    const runRequest = await requestPromise;
    const runBody = JSON.parse(runRequest.postData() || "{}");
    assert(runBody.stream === true, "Playground did not request stream=true");
    assert(runBody.agentgov?.agent_id === selectedAgent, "Playground did not pass the selected business agent through agentgov.agent_id");
    assert(typeof runBody.conversation === "string" && runBody.conversation.startsWith("conv_"), "Playground did not send a /v1 conversation id");

    await page.getByTestId("message-actions").last().waitFor({ timeout: liveTimeoutMs });
    const assistantTexts = await page.locator('[data-message-role="assistant"] [data-testid="message-markdown"]').allTextContents();
    const assistantText = (assistantTexts.at(-1) || "").trim();
    assert(assistantText.length > 0, "assistant response text did not render");
    assert(!assistantText.includes("运行失败"), `assistant response rendered as failure: ${assistantText.slice(0, 300)}`);
    assert(
      assistantText.includes("AGENTGOV_OPENAI_E2E_OK"),
      `assistant response did not contain the requested live-model sentinel: ${assistantText.slice(0, 300)}`,
    );

    const sessionId = runBody.conversation.slice("conv_".length);
    const runs = await api(`/api/agent-runs?session_id=${encodeURIComponent(sessionId)}&limit=1`);
    const latestRun = Array.isArray(runs.data) ? runs.data[0] : null;
    assert(latestRun?.run_id, "persisted run did not expose a run id after response completion");
    const localMessageCache = await page.evaluate(() => window.localStorage.getItem("playground-session-messages"));
    assert(localMessageCache === null, "Playground still persisted a parallel local message history");

    const response = await api(`/v1/responses/${encodeURIComponent(`resp_${latestRun.run_id}`)}`);
    assert(response.data?.object === "response", "retrieve did not return a response object");
    assert(response.data?.status === "completed", `retrieve status was not completed: ${response.data?.status}`);
    assert(response.data?.agentgov?.run_id === latestRun.run_id, "retrieve did not map resp_<run_id> back to the run");

    const items = await api(`/v1/conversations/${encodeURIComponent(`conv_${sessionId}`)}/items?limit=1&order=asc&include=messages`);
    assert(items.data?.object === "list", "conversation items did not return a list object");

    const paths = apiRequests.map((item) => `${item.method} ${item.path}`);
    assert(paths.includes("POST /v1/responses"), "UI did not call POST /v1/responses");
    assert(!paths.includes("POST /api/chat/stream"), "UI still called legacy POST /api/chat/stream");
    assert(!paths.includes("GET /api/sessions"), "UI still called legacy GET /api/sessions for the session sidebar");
    assert(pageErrors.length === 0, `browser page errors: ${pageErrors.join("\n")}`);

    await page.screenshot({ path: join(screenshotDir, "openai-responses-container.png"), fullPage: true });
    console.log(JSON.stringify({
      ok: true,
      uiBase,
      apiBase,
      browserApiBase,
      selectedAgent,
      sawResponses: paths.includes("POST /v1/responses"),
      sawConversations: paths.includes("GET /v1/conversations"),
      legacyChatStreamCalls: paths.filter((path) => path === "POST /api/chat/stream").length,
      hostileBoundaryChecks: 8,
    }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(`verify_openai_responses_container failed: ${error?.stack || error}`);
  process.exit(1);
});
