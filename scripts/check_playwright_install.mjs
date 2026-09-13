#!/usr/bin/env node
import { createRequire } from "node:module";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.goto("data:text/html,<title>AgentGov Playwright install check</title>");
  if ((await page.title()) !== "AgentGov Playwright install check") {
    throw new Error("Chromium installation lifecycle check failed");
  }
} finally {
  await browser.close();
}

console.log("PLAYWRIGHT_INSTALL_OK: Chromium binary launched, rendered, and closed; no product flow was tested");
