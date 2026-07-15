#!/usr/bin/env node
import { createRequire } from "node:module";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.goto("data:text/html,<title>AgentGov Playwright preflight</title>");
  if ((await page.title()) !== "AgentGov Playwright preflight") {
    throw new Error("Chromium page lifecycle validation failed");
  }
} finally {
  await browser.close();
}

console.log("PLAYWRIGHT_RUNTIME_OK: Chromium launched, rendered, and closed");
