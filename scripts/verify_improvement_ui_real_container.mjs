#!/usr/bin/env node
import { createRequire } from "node:module";
import process from "node:process";
import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";
import {
  runRealContainerAcceptance,
  verifyCompletedImprovementAcceptance,
} from "./improvement_ui_e2e/real_container_flow.mjs";
import { runtimeConfigFromEnv } from "./improvement_ui_e2e/runtime_client.mjs";
import { safeAcceptanceFailure } from "./improvement_ui_e2e/page_audit.mjs";
import {
  browserExecutionPlan,
  FORMAL_BROWSER_REPETITIONS,
  requireFormalBrowserResultMatrix,
} from "./improvement_ui_e2e/browser_acceptance_contract.mjs";
import {
  loadReviewedScenarios,
  requireScenario,
} from "./improvement_ui_e2e/reviewed_scenarios.mjs";

requireContainerAcceptance();

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium, firefox } = require("playwright");

async function main() {
  const baseConfig = runtimeConfigFromEnv();
  const governanceAgentId = String(process.env.REAL_ACCEPTANCE_AGENT_ID || "").trim();
  const reviewed = loadReviewedScenarios(process.env.REAL_SCENARIO_FILE, governanceAgentId);
  const scenario = requireScenario(reviewed, "improvement");
  const formal = process.env.AGENT_GOV_FORMAL_BROWSER_ACCEPTANCE === "1";
  const selection = String(process.env.BROWSER || "chromium").trim().toLowerCase();
  const plan = browserExecutionPlan(selection, { formal });
  const browserTypes = { chromium, firefox };
  const results = [];
  const mutationEngine = plan[0];
  const mutationBrowser = await browserTypes[mutationEngine].launch({
    headless: process.env.PLAYWRIGHT_HEADLESS !== "0",
  });
  let completedEvidence;
  try {
    completedEvidence = await runRealContainerAcceptance(
      mutationBrowser,
      baseConfig,
      governanceAgentId,
      scenario,
    );
    results.push({ engine: mutationEngine, attempt: 1, mode: "mutating-closed-loop", result: completedEvidence });
  } finally {
    await mutationBrowser.close();
  }

  if (formal) {
    for (const engine of plan) {
      for (let attempt = 1; attempt <= FORMAL_BROWSER_REPETITIONS; attempt += 1) {
        if (engine === mutationEngine && attempt === 1) continue;
        const browser = await browserTypes[engine].launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
        try {
          results.push({
            engine,
            attempt,
            mode: "read-only-completed-loop",
            result: await verifyCompletedImprovementAcceptance(
              browser,
              baseConfig,
              completedEvidence,
            ),
          });
        } finally {
          await browser.close();
        }
      }
    }
  }
  requireFormalBrowserResultMatrix(results, { formal });
  console.log(JSON.stringify({
    status: "passed",
    formal_browser_acceptance: formal,
    scenario_sha256: reviewed.scenario_file_sha256,
    mutation_runs: 1,
    browsers: results,
  }, null, 2));
  console.log("REAL_UI_ACCEPTANCE passed; metadata-only evidence");
}

main().catch((error) => {
  console.error(JSON.stringify(safeAcceptanceFailure(error)));
  process.exit(1);
});
