#!/usr/bin/env node
// v2.7 §3 助手回复动作的真实对话验收（message-actions 规则需一条真实助手回复，无法进确定性 mock 门）。
// 连真实容器 UI/API（默认 4173/58081），真实 LLM 对话，断言回复动作行 5 个领域级 data-testid。
// LLM 偶发报错/超时按目标要求重试（默认 3 次）。
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import process from "node:process";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

function envv(name) {
  try {
    for (const l of readFileSync(new URL("../docker/.env", import.meta.url), "utf8").split(/\r?\n/)) {
      const t = l.trim();
      if (!t || t.startsWith("#")) continue;
      const i = t.indexOf("=");
      if (i > 0 && t.slice(0, i).trim() === name) return t.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  } catch { /* ignore */ }
  return "";
}

const ui = (process.env.RUNTIME_UI_BASE || "http://127.0.0.1:4173").replace(/\/$/, "");
const api = (process.env.RUNTIME_API_BASE || "http://127.0.0.1:58081").replace(/\/$/, "");
const key = process.env.RUNTIME_API_KEY || envv("FRONTEND_RUNTIME_API_KEY") || envv("API_KEY") || "";
const RETRIES = Number(process.env.RETRIES || 3);

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  await page.addInitScript(([a, k]) => window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: a, apiKey: k })), [api, key]);
  let ok = false, detail = "";
  try {
    await page.goto(ui, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 20000 });
    for (let attempt = 1; attempt <= RETRIES && !ok; attempt += 1) {
      try {
        await page.locator(".composer textarea").fill("用一句话说明你的角色。");
        await page.getByRole("button", { name: "发送" }).click();
        await page.getByTestId("message-actions").first().waitFor({ timeout: 90000 });
        const counts = {};
        for (const t of ["message-action-create-feedback", "message-action-view-trace", "message-action-get-context", "message-action-rerun"]) {
          counts[t] = await page.getByTestId(t).count();
        }
        ok = Object.values(counts).every((c) => c > 0);
        detail = JSON.stringify(counts);
        if (ok) await page.screenshot({ path: "/tmp/agentgov-v27-ui-after-message-actions.png" });
      } catch (e) {
        detail = `attempt ${attempt}: ${e instanceof Error ? e.message.slice(0, 80) : e}`;
        console.error("retry:", detail);
      }
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ status: ok ? "passed" : "failed", rule: "message-actions", detail }, null, 2));
  process.exit(ok ? 0 : 1);
}
main().catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(2); });
