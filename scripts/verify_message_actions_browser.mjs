#!/usr/bin/env node
// v2.7 §3 助手回复动作验收。
// 默认模式：自启动 Vite + mock SSE，进入 main-flow 硬门，验证回复动作结构不回归。
// 真实模式：设置 RUNTIME_UI_BASE + RUNTIME_API_BASE 后连真实容器 UI/API，跑真实 LLM 对话。
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const ts = "2026-06-18T00:00:00Z";

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

const REAL = !!process.env.RUNTIME_UI_BASE;
const port = Number(process.env.MESSAGE_ACTIONS_PORT || 55198);
const ui = (process.env.RUNTIME_UI_BASE || `http://127.0.0.1:${port}`).replace(/\/$/, "");
const api = (process.env.RUNTIME_API_BASE || "http://runtime.test").replace(/\/$/, "");
const key = process.env.RUNTIME_API_KEY || envv("FRONTEND_RUNTIME_API_KEY") || envv("API_KEY") || "";
const RETRIES = Number(process.env.RETRIES || 3);

function startVite() {
  const child = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  return child;
}

function killTree(child, signal) {
  try { process.kill(-child.pid, signal); } catch { try { child.kill(signal); } catch { /* gone */ } }
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  killTree(child, "SIGTERM");
  await new Promise((resolve) => {
    const timeout = setTimeout(() => { killTree(child, "SIGKILL"); resolve(); }, 2000);
    child.once("exit", () => { clearTimeout(timeout); resolve(); });
  });
}

async function waitForVite() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(ui);
      if (res.ok) return;
    } catch { /* wait */ }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("vite not ready");
}

function json(route, payload) {
  return route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(payload) });
}

function sse(route, events) {
  return route.fulfill({
    status: 200,
    contentType: "text/event-stream; charset=utf-8",
    headers: { "access-control-allow-origin": "*" },
    body: events.map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(""),
  });
}

function mockPayload(path) {
  if (path === "/health") return { status: "ok", model: "parity-mock", provider_key_configured: true };
  if (path === "/api/sessions" || path === "/api/agents" || path === "/api/skills" || path === "/api/agent-change-sets" || path === "/api/agent-releases") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-registry") return [{ agent_id: "main-agent", name: "默认 Agent", category: "", workspace_dir: "/main-workspace", created_at: ts, status: "active" }];
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v-mock", commit_sha: "mock", created_at: ts, reason: "current" };
  return {};
}

async function main() {
  const server = REAL ? null : startVite();
  if (!REAL) await waitForVite();
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  await page.addInitScript(([a, k, real]) => {
    window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: a, apiKey: k }));
    if (!real) {
      window.localStorage.setItem("playground-active-session", JSON.stringify("mock-session"));
      window.localStorage.setItem("playground-session-messages", JSON.stringify({
        "mock-session": [
          { id: "msg-user", role: "user", content: "用一句话说明你的角色。", createdAt: "2026-06-18T00:00:00Z" },
          {
            id: "msg-assistant",
            role: "assistant",
            content: "我是 AgentGov 测试助手。",
            createdAt: "2026-06-18T00:00:01Z",
            runId: "mock-run",
            sessionId: "mock-session",
            agentVersionId: "v-mock",
            events: [{ id: "evt-1", event: "message", text: "我是 AgentGov 测试助手。", data: { text: "我是 AgentGov 测试助手。" }, createdAt: "2026-06-18T00:00:01Z" }],
          },
        ],
      }));
    }
  }, [api, key, REAL]);
  let ok = false, detail = "";
  try {
    if (!REAL) {
      await page.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        if (url.hostname !== "runtime.test") return route.continue();
        if (url.pathname === "/api/chat/stream") {
          return sse(route, [
            { event: "session", data: { session_id: "mock-session" } },
            { event: "message", data: { text: "我是 AgentGov 测试助手。" } },
            { event: "result", data: { run_id: "mock-run", session_id: "mock-session", agent_version_id: "v-mock" } },
            { event: "done", data: { ok: true } },
          ]);
        }
        return json(route, mockPayload(url.pathname));
      });
    }
    await page.goto(ui, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 20000 });
    const maxAttempts = REAL ? RETRIES : 1;
    for (let attempt = 1; attempt <= maxAttempts && !ok; attempt += 1) {
      try {
        if (REAL) {
          await page.locator(".composer textarea").fill("用一句话说明你的角色。");
          await page.getByRole("button", { name: "发送" }).click();
        }
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
    await stopChild(server);
  }
  console.log(JSON.stringify({ status: ok ? "passed" : "failed", mode: REAL ? "real-container" : "mock", rule: "message-actions", detail }, null, 2));
  process.exit(ok ? 0 : 1);
}
main().catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(2); });
