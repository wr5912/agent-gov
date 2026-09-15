#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import { getCurrentRuntimeAgent } from "./improvement_ui_e2e/runtime_client.mjs";
import { requireDeployedCheck as check, safeBrowserFailure } from "./improvement_ui_e2e/deployed_playground_evidence.mjs";
import { runPlaygroundRecoveryBrowser } from "./improvement_ui_e2e/playground_recovery_flow.mjs";

const SCOPE = "deployed_playground_receipt_disconnect_refresh";
const GUARD = fileURLToPath(new URL("./verify_deployed_browser_context.py", import.meta.url));

function verifiedContext() {
  const python = String(process.env.AGENTGOV_OPERATION_PYTHON || "");
  check(isAbsolute(python), "DEPLOYED_CONTEXT_REQUIRED");
  let context;
  try {
    context = JSON.parse(execFileSync(python, [GUARD], {
      encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 120_000, maxBuffer: 1024 * 1024,
    }));
  } catch { check(false, "DEPLOYED_CONTEXT_INVALID"); }
  check(context.scope === SCOPE && context.agent_id === "security-operations-expert", "DEPLOYED_CONTEXT_SCOPE_INVALID");
  check(typeof context.acceptance_id === "string" && /^[0-9a-f]{64}$/.test(context.source_sha256), "DEPLOYED_IDENTITY_INVALID");
  check(isAbsolute(context.playwright_module_path || "")
    && JSON.stringify(context.browsers) === JSON.stringify(["chromium", "firefox"]), "DEPLOYED_BROWSER_MATRIX_INVALID");
  return context;
}

function bindingIdentity(binding) {
  return { agent_id: binding.governance_agent_id, runtime_agent_id: binding.runtime_agent_id,
    agent_version_id: binding.agent_version_id, harness_digest: binding.harness_digest };
}

async function main() {
  const result = { status: "failed", scope: SCOPE, browsers: [] };
  let context;
  let stage = "context";
  try {
    context = verifiedContext();
    result.acceptance_id = context.acceptance_id;
    const apiKey = String(process.env.AGENTGOV_DEPLOYED_API_KEY || "");
    check(apiKey.length > 0, "DEPLOYED_API_CREDENTIAL_REQUIRED");
    const config = { apiBase: context.api_base, uiBase: context.ui_base, apiKey, actionTimeoutMs: 300_000 };
    const require = createRequire(context.playwright_module_path);
    check(require.resolve("playwright") === context.playwright_module_path, "DEPLOYED_PLAYWRIGHT_RESOLUTION_CHANGED");
    const browserTypes = require("playwright");
    stage = "published_binding";
    const binding = await getCurrentRuntimeAgent(config, context.agent_id);
    result.binding = bindingIdentity(binding);
    for (const engine of context.browsers) {
      stage = engine;
      const browser = await browserTypes[engine].launch({ headless: true });
      try {
        const evidence = await runPlaygroundRecoveryBrowser(browser, config, binding);
        result.browsers.push({ engine, ...evidence });
        check(evidence.status !== "failed", "RECOVERY_BROWSER_JOURNEY_FAILED");
      } finally { await browser.close(); }
    }
    stage = "final_binding";
    check(JSON.stringify(bindingIdentity(await getCurrentRuntimeAgent(config, context.agent_id)))
      === JSON.stringify(result.binding), "PUBLISHED_BINDING_CHANGED");
    result.status = result.browsers.every((browser) => browser.status === "passed") ? "passed" : "not_proven";
  } catch (error) { result.failure = safeBrowserFailure(error, stage); }
  finally {
    if (context) {
      try { check(JSON.stringify(verifiedContext()) === JSON.stringify(context), "DEPLOYED_CONTEXT_CHANGED"); }
      catch (error) {
        result.status = "failed";
        result.context_failure = safeBrowserFailure(error, "final_context");
      }
    }
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (result.status !== "passed") process.exitCode = result.status === "not_proven" ? 2 : 1;
}

await main();
