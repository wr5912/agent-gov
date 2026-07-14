#!/usr/bin/env node
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { createRequire } from "node:module";
import process from "node:process";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const uiBase = requiredBase("RUNTIME_UI_BASE");
const apiBase = requiredBase("RUNTIME_API_BASE");
const apiKey = String(process.env.RUNTIME_API_KEY || "");
const screenshotDir = String(process.env.VERIFY_SCREENSHOT_DIR || "/tmp/agentgov-provider-health-e2e");
const viewports = [
  { name: "tablet", width: 900, height: 1100 },
  { name: "mobile", width: 390, height: 844 },
  { name: "desktop", width: 1440, height: 920 },
];

function requiredBase(name) {
  const value = String(process.env[name] || "").trim().replace(/\/$/, "");
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function api(path) {
  const started = Date.now();
  try {
    const response = await fetch(`${apiBase}${path}`, {
      headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
      signal: AbortSignal.timeout(3000),
    });
    const payload = await response.json();
    return { status: response.status, payload, durationMs: Date.now() - started };
  } catch (error) {
    throw new Error(`${path} request failed after ${Date.now() - started}ms: ${error?.message || error}`, { cause: error });
  }
}

async function waitForDegradedReadiness() {
  const deadline = Date.now() + 20000;
  let last;
  while (Date.now() < deadline) {
    last = await api("/health/ready");
    if (last.payload?.model_provider?.status === "degraded") return last;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`provider readiness did not become degraded: ${JSON.stringify(last?.payload)}`);
}

function attachAudit(page) {
  const audit = { consoleErrors: [], pageErrors: [], requestFailures: [], httpErrors: [], apiResponses: [] };
  page.on("console", (message) => {
    if (message.type() === "error") audit.consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => audit.pageErrors.push(error.message));
  page.on("requestfailed", (request) => audit.requestFailures.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`));
  page.on("response", (response) => {
    if (response.url().startsWith(apiBase)) {
      audit.apiResponses.push(`${response.status()} ${response.request().method()} ${response.url()}`);
    }
    if (response.status() >= 400) audit.httpErrors.push(`${response.status()} ${response.request().method()} ${response.url()}`);
  });
  return audit;
}

async function openRuntime(page) {
  await page.addInitScript(([base, key]) => {
    window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: base, apiKey: key }));
    window.localStorage.removeItem("playground-session-messages");
    window.localStorage.removeItem("playground-active-session");
  }, [apiBase, apiKey]);
  await page.goto(uiBase, { waitUntil: "domcontentloaded" });
  await page.getByTestId("playground").waitFor({ timeout: 30000 });
}

async function openProviderDiagnostic(page, expectedCode) {
  await page.getByTestId("playground-runtime-settings-trigger").click();
  const drawer = page.getByTestId("playground-runtime-settings-drawer");
  await drawer.waitFor({ timeout: 10000 });
  await drawer.locator("summary").click();
  const diagnostic = drawer.getByTestId("model-provider-diagnostic");
  await diagnostic.waitFor({ timeout: 10000 });
  const text = await diagnostic.innerText();
  assert(text.includes(expectedCode), `provider diagnostic did not show ${expectedCode}: ${text}`);
  assert(text.includes("reason=timeout"), `provider diagnostic did not show timeout reason: ${text}`);
  return drawer;
}

async function assertNoHorizontalOverflow(page, viewportName) {
  const layout = await page.evaluate(() => ({
    documentClientWidth: document.documentElement.clientWidth,
    documentScrollWidth: document.documentElement.scrollWidth,
    bodyClientWidth: document.body.clientWidth,
    bodyScrollWidth: document.body.scrollWidth,
  }));
  assert(layout.documentScrollWidth <= layout.documentClientWidth + 1, `${viewportName} document overflows: ${JSON.stringify(layout)}`);
  assert(layout.bodyScrollWidth <= layout.bodyClientWidth + 1, `${viewportName} body overflows: ${JSON.stringify(layout)}`);
}

async function main() {
  mkdirSync(screenshotDir, { recursive: true });
  const live = await api("/health/live");
  const diagnostic = await api("/health");
  assert(live.status === 200 && live.durationMs < 2000, `liveness was blocked by provider: ${JSON.stringify(live)}`);
  assert(diagnostic.status === 200 && diagnostic.durationMs < 2000, `health snapshot was blocked by provider: ${JSON.stringify(diagnostic)}`);
  const readiness = await waitForDegradedReadiness();
  const provider = readiness.payload.model_provider;
  assert(readiness.status === 503, `degraded readiness must return 503: ${readiness.status}`);
  assert(provider.error_code === "VLLM_VERSION_PROBE_FAILED", `unexpected readiness code: ${JSON.stringify(provider)}`);
  assert(provider.reason === "timeout", `unexpected readiness reason: ${JSON.stringify(provider)}`);
  const protectedEndpoint = await api("/api/agent-registry");
  assert(protectedEndpoint.status === 200, `E2E API key did not authorize protected endpoints: ${JSON.stringify(protectedEndpoint)}`);

  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of viewports) {
      const page = await browser.newPage({ viewport });
      const audit = attachAudit(page);
      try {
        await openRuntime(page);
        const runtimeStatus = page.locator(".topbar-left .status-dot.ok").first();
        try {
          await runtimeStatus.waitFor({ timeout: 10000 });
        } catch (error) {
          const visibleErrors = await page.locator(".error-box").allTextContents();
          await page.screenshot({ path: join(screenshotDir, `${viewport.name}-runtime-offline-failure.png`), fullPage: true });
          throw new Error(
            `${viewport.name} did not keep Runtime online; visible_errors=${JSON.stringify(visibleErrors)} audit=${JSON.stringify(audit)}`,
            { cause: error },
          );
        }
        if (viewport.name !== "mobile") {
          const runtimeText = await page.locator(".topbar-left").innerText();
          assert(runtimeText.includes("Runtime online"), `${viewport.name} did not show Runtime online: ${runtimeText}`);
        }
        if (viewport.name === "desktop") {
          const providerText = await page.getByTestId("model-provider-status").innerText();
          assert(providerText.includes("degraded"), `topbar did not separate provider degradation: ${providerText}`);
        }
        const drawer = await openProviderDiagnostic(page, "VLLM_VERSION_PROBE_FAILED");
        await assertNoHorizontalOverflow(page, viewport.name);
        await page.screenshot({ path: join(screenshotDir, `${viewport.name}-provider-degraded.png`), fullPage: true });

        if (viewport.name === "desktop") {
          await drawer.getByRole("button", { name: "关闭" }).click();
          const agentSelect = page.getByTestId("topbar-agent-switcher");
          await agentSelect.waitFor({ timeout: 10000 });
          await agentSelect.selectOption("main-agent");
          await page.getByTestId("chat-composer-input").fill("provider health failure acceptance");
          const modelRequest = page.waitForRequest(
            (request) => new URL(request.url()).pathname === "/v1/responses" && request.method() === "POST",
            { timeout: 10000 },
          );
          await page.getByTestId("chat-send").click();
          await modelRequest;
          const errorMessage = page
            .locator('[data-message-role="assistant"] [data-testid="message-markdown"]')
            .filter({ hasText: /VLLM_|MODEL_PROVIDER_/ })
            .last();
          try {
            await errorMessage.waitFor({ timeout: 20000 });
          } catch (error) {
            const assistantTexts = await page
              .locator('[data-message-role="assistant"] [data-testid="message-markdown"]')
              .allTextContents();
            await page.screenshot({ path: join(screenshotDir, "desktop-model-action-timeout.png"), fullPage: true });
            throw new Error(`model action did not render a structured failure: ${JSON.stringify(assistantTexts)}`, { cause: error });
          }
          const errorText = await errorMessage.innerText();
          assert(errorText.includes("probe="), `visible model error omitted probe: ${errorText}`);
          assert(errorText.includes("reason="), `visible model error omitted reason: ${errorText}`);
          assert(errorText.includes("endpoint=http://slow-vllm:8000"), `visible model error omitted sanitized endpoint: ${errorText}`);
          assert(errorText.includes("action="), `visible model error omitted action: ${errorText}`);
          assert((errorText.match(/probe=vllm_version/g) || []).length === 1, `visible model error duplicated probe details: ${errorText}`);
          assert((errorText.match(/action=/g) || []).length === 1, `visible model error duplicated action details: ${errorText}`);
          assert(!errorText.includes(apiKey), `visible model error leaked the API key: ${errorText}`);
          await page.locator(".idle-status", { hasText: "Ready" }).waitFor({ timeout: 10000 });
          assert(await page.getByTestId("chat-stop").count() === 0, "failed model action left the Playground in a running state");
          const liveAfterFailure = await api("/health/live");
          assert(liveAfterFailure.status === 200 && liveAfterFailure.durationMs < 2000, "model failure broke API liveness");
          await page.screenshot({ path: join(screenshotDir, "desktop-model-action-failure.png"), fullPage: true });
        }

        const browserErrors = {
          consoleErrors: audit.consoleErrors,
          pageErrors: audit.pageErrors,
          requestFailures: audit.requestFailures,
          httpErrors: audit.httpErrors,
        };
        assert(
          Object.values(browserErrors).every((items) => items.length === 0),
          `${viewport.name} browser audit failed: ${JSON.stringify({ ...browserErrors, apiResponses: audit.apiResponses })}`,
        );
        results.push({ viewport: viewport.name, audit });
      } finally {
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ ok: true, provider, screenshots: screenshotDir, results }, null, 2));
}

main().catch((error) => {
  console.error(`verify_provider_health_container failed: ${error?.stack || error}`);
  process.exit(1);
});
