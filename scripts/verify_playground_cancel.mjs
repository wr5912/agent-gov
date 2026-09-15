#!/usr/bin/env node
import { createRequire } from "node:module";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";

import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";
import {
  attachCancelNetworkEvidence,
  exercisePlaygroundCancellation,
} from "./improvement_ui_e2e/playground_cancel_evidence.mjs";
import {
  attachUiDiagnostics,
  cleanupResources,
  failureDiagnostic,
  runtimeConnection,
  waitForUi,
} from "./improvement_ui_e2e/playground_cancel_runtime.mjs";
import {
  apiJson,
  apiRequest,
  jsonInit,
  getCurrentRuntimeAgent,
} from "./improvement_ui_e2e/runtime_client.mjs";
import {
  loadReviewedScenarios,
  requireScenario,
} from "./improvement_ui_e2e/reviewed_scenarios.mjs";
import {
  browserExecutionPlan,
  FORMAL_BROWSER_REPETITIONS,
  requireFormalBrowserResultMatrix,
} from "./improvement_ui_e2e/browser_acceptance_contract.mjs";

requireContainerAcceptance();
if (process.env.REQUIRE_LIVE_RUNTIME !== "1") {
  throw new Error("REQUIRE_LIVE_RUNTIME=1 is required for Playground acceptance");
}

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, firefox } = require("playwright");
const browserSelection = String(process.env.BROWSER || "both").trim().toLowerCase();
const formalBrowserAcceptance = process.env.AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE === "1";
const browserPlan = browserExecutionPlan(browserSelection, { formal: formalBrowserAcceptance });
const governanceAgentId = String(process.env.REAL_ACCEPTANCE_AGENT_ID || "").trim();
if (!governanceAgentId) throw new Error("REAL_ACCEPTANCE_AGENT_ID is required");

const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR
  || mkdtempSync(join(tmpdir(), "agentgov-playground-cancel-"));
const config = {
  ...runtimeConnection(),
  actionTimeoutMs: Number(process.env.REAL_ACTION_TIMEOUT_MS || 300_000),
};
if (!Number.isFinite(config.actionTimeoutMs) || config.actionTimeoutMs < 1000) {
  throw new Error("REAL_ACTION_TIMEOUT_MS must be a finite number of at least 1000 milliseconds");
}
const reviewedScenarios = loadReviewedScenarios(process.env.REAL_SCENARIO_FILE, governanceAgentId);
const scenarioSet = {
  earlyCancel: requireScenario(reviewedScenarios, "early_cancel"),
  partialCancel: requireScenario(reviewedScenarios, "partial_cancel"),
  retry: requireScenario(reviewedScenarios, "retry"),
  sha256: reviewedScenarios.scenario_file_sha256,
};

async function deleteRuntimeSession(sessionId, runtimeAgentId) {
  const query = new URLSearchParams({ agent_id: runtimeAgentId });
  const result = await apiRequest(
    config,
    `/api/runtime/sessions/${encodeURIComponent(sessionId)}?${query.toString()}`,
    { method: "DELETE", headers: { "X-User-ID": "agentgov-ui" } },
  );
  if (!result.response.ok && result.status !== 404) {
    throw new Error(`Runtime Session cleanup failed with HTTP ${result.status}`);
  }
}

async function runBrowser(engineName, attempt, browserType, binding) {
  const browser = await browserType.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
  const page = await context.newPage();
  const network = attachCancelNetworkEvidence(page, config.apiBase);
  const diagnostics = attachUiDiagnostics(page, network);
  let runs = [];
  try {
    await page.goto(config.uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 60_000 });
    const switcher = page.getByTestId("topbar-agent-switcher");
    await switcher.waitFor({ timeout: 60_000 });
    await switcher.selectOption(governanceAgentId);
    const acceptance = await exercisePlaygroundCancellation(
      page,
      config,
      binding,
      network,
      scenarioSet,
      5_000,
    );
    runs = acceptance.runs;
    if (Object.values(acceptance.result).some((passed) => passed !== true)) {
      throw new Error(`Playground cancellation assertions failed: ${JSON.stringify(acceptance.result)}`);
    }
    return { engine: engineName, attempt, result: acceptance.result, runs };
  } finally {
    const streamsSettledBeforeCleanup = await network.settle(config.actionTimeoutMs);
    const observedRuns = [...runs];
    for (const event of network.chats.filter((item) => item.runId)) {
      if (observedRuns.some((item) => item.run_id === event.runId)) continue;
      const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(event.runId)}`).catch(() => null);
      if (run) observedRuns.push(run);
    }
    const sessionIds = [...new Set([
      ...observedRuns.map((run) => run.session_id),
      ...network.sessionCreates.map((event) => event.sessionId),
      ...network.chats.map((event) => event.sessionId),
    ].filter(Boolean))];
    const cleanupFailures = [];
    for (const run of observedRuns.filter((item) => !new Set(["succeeded", "failed", "cancelled", "interrupted"]).has(item.status))) {
      const result = await apiRequest(
        config,
        `/api/agent-runs/${encodeURIComponent(run.run_id)}/cancel`,
        jsonInit("POST", {}),
      ).catch(() => null);
      if (!result?.response.ok && result?.status !== 409) cleanupFailures.push(`run:${run.run_id}`);
    }
    for (const sessionId of sessionIds) {
      await deleteRuntimeSession(sessionId, binding.runtime_agent_id)
        .catch(() => cleanupFailures.push(`session:${sessionId}`));
    }
    const resourceFailures = await cleanupResources([
      { name: "browser_context", close: () => context.close() },
      { name: "browser", close: () => browser.close() },
    ]);
    cleanupFailures.push(...resourceFailures);
    if (!streamsSettledBeforeCleanup) cleanupFailures.push("sse_streams_not_closed");
    const expectedDiagnostics = new Set(["expected_stream_cancel", "expected_reload_stream_cancel"]);
    if (diagnostics.some((event) => !expectedDiagnostics.has(event.kind))) {
      cleanupFailures.push(`diagnostics:${JSON.stringify(diagnostics)}`);
    }
    if (cleanupFailures.length) throw new Error(`RESOURCE_CLEANUP_FAILED: ${cleanupFailures.join(",")}`);
  }
}

async function main() {
  let stage = "ui_ready";
  const results = [];
  try {
    await waitForUi(config.uiBase, 60_000);
    stage = "runtime_binding";
    const binding = await getCurrentRuntimeAgent(config, governanceAgentId);
    const browserTypes = { chromium, firefox };
    for (const name of browserPlan) {
      const browserType = browserTypes[name];
      for (let attempt = 1; attempt <= FORMAL_BROWSER_REPETITIONS; attempt += 1) {
        stage = `${name}_playground_${attempt}`;
        results.push(await runBrowser(name, attempt, browserType, binding));
      }
    }
    requireFormalBrowserResultMatrix(results, { formal: formalBrowserAcceptance });
    console.log(JSON.stringify({
      status: "passed",
      mode: "real-container",
      formal_browser_acceptance: formalBrowserAcceptance,
      agent_id: governanceAgentId,
      scenario_sha256: scenarioSet.sha256,
      scenario_ids: [
        scenarioSet.earlyCancel.scenario_id,
        scenarioSet.partialCancel.scenario_id,
        scenarioSet.retry.scenario_id,
      ],
      browsers: results,
      repetitions_per_browser: FORMAL_BROWSER_REPETITIONS,
    }, null, 2));
  } catch (error) {
    console.error(JSON.stringify(failureDiagnostic(error, stage, [])));
    process.exitCode = 2;
  }
}

main().catch((error) => {
  console.error(JSON.stringify(failureDiagnostic(error, "bootstrap", [])));
  process.exit(2);
});
