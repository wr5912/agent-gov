#!/usr/bin/env node
import { createRequire } from "node:module";
import { withManagedChromium } from "./playwright_browser_authority.mjs";
import process from "node:process";
import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";
import { runRealContainerAcceptance } from "./improvement_ui_e2e/real_container_flow.mjs";
import { runtimeConfigFromEnv } from "./improvement_ui_e2e/runtime_client.mjs";

requireContainerAcceptance();

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

async function main() {
  const config = runtimeConfigFromEnv();
  await withManagedChromium(chromium, { headless: true }, async (browser) => {
    await runRealContainerAcceptance(browser, config);
  });
  console.log("REAL_UI_ACCEPTANCE_OK");
}

main().catch(() => {
  console.error("REAL_UI_ACCEPTANCE_FAIL");
  process.exit(1);
});
