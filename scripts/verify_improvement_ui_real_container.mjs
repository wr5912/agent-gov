#!/usr/bin/env node
import { createRequire } from "node:module";
import process from "node:process";
import { runRealContainerAcceptance } from "./improvement_ui_e2e/real_container_flow.mjs";
import { runtimeConfigFromEnv } from "./improvement_ui_e2e/runtime_client.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

async function main() {
  const config = runtimeConfigFromEnv();
  const browser = await chromium.launch({ headless: true });
  try {
    const result = await runRealContainerAcceptance(browser, config);
    console.log(JSON.stringify(result, null, 2));
    console.log(`REAL_UI_ACCEPTANCE passed; screenshots=${config.screenshotDir}`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : error);
  process.exit(1);
});
