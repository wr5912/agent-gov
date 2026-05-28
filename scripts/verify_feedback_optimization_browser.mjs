#!/usr/bin/env node
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import process from "node:process";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

function dockerEnvValue(name) {
  try {
    const content = readFileSync(new URL("../docker/.env", import.meta.url), "utf8");
    for (const rawLine of content.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) {
        continue;
      }
      const index = line.indexOf("=");
      if (index <= 0) {
        continue;
      }
      const key = line.slice(0, index).trim();
      if (key !== name) {
        continue;
      }
      const value = line.slice(index + 1).trim();
      return value.replace(/^['"]|['"]$/g, "");
    }
  } catch {
    return "";
  }
  return "";
}

const apiBase = (process.env.RUNTIME_API_BASE || process.env.VITE_RUNTIME_API_BASE || "http://127.0.0.1:58080").replace(/\/$/, "");
const uiBase = (process.env.RUNTIME_UI_BASE || "http://127.0.0.1:55173").replace(/\/$/, "");
const apiKey =
  process.env.RUNTIME_API_KEY ||
  process.env.FRONTEND_RUNTIME_API_KEY ||
  process.env.API_KEY ||
  dockerEnvValue("FRONTEND_RUNTIME_API_KEY") ||
  dockerEnvValue("API_KEY") ||
  "";

function headers(extra = {}) {
  return {
    "Content-Type": "application/json",
    ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    ...extra,
  };
}

async function api(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: headers(options.headers || {}),
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    throw new Error(`${options.method || "GET"} ${path} failed: ${response.status} ${text}`);
  }
  return payload;
}

async function seedBatch() {
  const suffix = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
  const title = `浏览器验证批次 ${suffix}`;
  const signal = await api("/api/feedback-signals", {
    method: "POST",
    body: JSON.stringify({
      session_id: `sess-browser-${suffix}`,
      labels: ["tool_data_incomplete", "browser_e2e"],
      comment: `${title}：回答 workspace 配置时数据不全`,
      confidence: "high",
      metadata: { source: "verify_feedback_optimization_browser" },
    }),
  });
  const batch = await api("/api/feedback-optimization-batches", {
    method: "POST",
    body: JSON.stringify({
      title,
      priority: "medium",
      source_refs: [{ source_kind: "signal", source_id: signal.signal_id }],
    }),
  });
  return { title, signal, batch };
}

async function main() {
  const seeded = await seedBatch();
  const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.addInitScript(
    ({ apiBaseValue, apiKeyValue }) => {
      window.localStorage.setItem(
        "runtime-client-config",
        JSON.stringify({ apiBase: apiBaseValue, apiKey: apiKeyValue }),
      );
    },
    { apiBaseValue: apiBase, apiKeyValue: apiKey },
  );
  await page.goto(uiBase, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "打开反馈优化工作台" }).click();
  await page.getByRole("button", { name: "优化批次", exact: true }).click();
  await page.getByPlaceholder("搜索 ID、标签、Case").fill(seeded.title);
  await page.getByText(seeded.title).first().waitFor({ timeout: 15000 });
  await page.getByText(seeded.title).first().click();
  await page.getByRole("tab", { name: /反馈信息/ }).click();
  await page.getByText("反馈原始数据").waitFor({ timeout: 15000 });
  await page.getByRole("tab", { name: /优化方案/ }).click();
  await page.getByText("尚未生成优化方案").waitFor({ timeout: 15000 });
  await mkdir("artifacts", { recursive: true });
  await page.screenshot({ path: "artifacts/feedback-optimization-browser-e2e.png", fullPage: true });
  await browser.close();
  console.log(
    JSON.stringify(
      {
        status: "passed",
        ui_base: uiBase,
        api_base: apiBase,
        batch_id: seeded.batch.batch_id,
        signal_id: seeded.signal.signal_id,
        screenshot: "artifacts/feedback-optimization-browser-e2e.png",
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : error);
  process.exit(1);
});
