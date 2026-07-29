#!/usr/bin/env node
// Playground run cancellation contract:
// mock mode owns a deliberately blocked SSE stream; real mode exercises rebuilt Compose UI/API.
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const real = Boolean(process.env.RUNTIME_UI_BASE);
requireContainerAcceptance(real);

const uiPort = Number(process.env.PLAYGROUND_CANCEL_UI_PORT || 55248);
const uiBase = (process.env.RUNTIME_UI_BASE || `http://127.0.0.1:${uiPort}`).replace(/\/$/, "");
const configuredApiBase = process.env.RUNTIME_API_BASE?.replace(/\/$/, "");
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR
  || mkdtempSync(join(tmpdir(), "agentgov-playground-cancel-"));

function envValue(name) {
  try {
    const lines = readFileSync(new URL("../docker/.env", import.meta.url), "utf8").split(/\r?\n/);
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const index = trimmed.indexOf("=");
      if (index > 0 && trimmed.slice(0, index).trim() === name) {
        return trimmed.slice(index + 1).trim().replace(/^['"]|['"]$/g, "");
      }
    }
  } catch {
    // The mock path does not require docker/.env.
  }
  return "";
}

function startVite() {
  return spawn(
    "pnpm",
    ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(uiPort), "--strictPort"],
    {
      cwd: repoRoot,
      stdio: ["ignore", "inherit", "inherit"],
      detached: true,
    },
  );
}

function killTree(child, signal) {
  try {
    process.kill(-child.pid, signal);
  } catch {
    try { child.kill(signal); } catch { /* already gone */ }
  }
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  killTree(child, "SIGTERM");
  await new Promise((resolve) => {
    const timeout = setTimeout(() => {
      killTree(child, "SIGKILL");
      resolve();
    }, 2000);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

async function waitForUrl(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Retry while Vite/Compose becomes ready.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`URL did not become ready: ${url}`);
}

function writeCorsHeaders(res, extra = {}) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Authorization,Content-Type");
  res.setHeader(
    "Access-Control-Expose-Headers",
    "X-AgentGov-Run-Id,X-AgentGov-Session-Id",
  );
  for (const [name, value] of Object.entries(extra)) res.setHeader(name, value);
}

function json(res, body, status = 200) {
  writeCorsHeaders(res, { "Content-Type": "application/json" });
  res.writeHead(status);
  res.end(JSON.stringify(body));
}

function nativeSseEvent(name, data) {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function mockPayload(pathname, state) {
  const now = Math.floor(Date.now() / 1000);
  if (pathname === "/health") {
    return { status: "ok", model: "cancel-mock", provider_key_configured: true };
  }
  if (pathname === "/v1/conversations") {
    return state.sessionId
      ? {
          data: [{
            id: `conv_${state.sessionId}`,
            created_at: now,
            title: "取消竞态验收",
            metadata: {},
            agentgov: {
              sdk_session_id: "sdk-cancel-mock",
              agent_id: "security-operations-expert",
              updated_at: now,
              turns: state.secondCompleted ? 1 : 0,
              active_run_id: state.activeRunId,
              active_run_expires_at: state.activeRunId ? "2099-01-01T00:00:00Z" : null,
            },
          }],
        }
      : { data: [] };
  }
  if (pathname === "/api/agent-registry") {
    return [{
      agent_id: "security-operations-expert",
      name: "Security Operations Expert",
      category: "business",
      workspace_dir: "/runtime/business-agent",
      created_at: "2026-07-28T00:00:00Z",
      status: "active",
      builtin: true,
      default: true,
      protected: true,
      requires_web_hitl: false,
    }];
  }
  if (pathname.endsWith("/presentation")) {
    return {
      agent_id: "security-operations-expert",
      version: "cancel-mock",
      summary: "Playground cancellation acceptance agent.",
      starter_prompts: [],
    };
  }
  if (pathname === "/api/agent-repository") {
    return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  }
  if (pathname === "/api/agent-repository/current") {
    return {
      agent_version_id: "v-cancel-mock",
      commit_sha: "cancel-mock",
      created_at: "2026-07-28T00:00:00Z",
      reason: "current",
    };
  }
  if (pathname === "/api/config") return { mappings: [] };
  if (pathname === "/api/agent-runs") return [];
  if (pathname === "/api/agents" || pathname === "/api/skills") return [];
  if (pathname === "/api/agent-change-sets" || pathname === "/api/agent-releases") return [];
  if (/^\/v1\/conversations\/[^/]+\/items$/.test(pathname)) {
    return { object: "list", data: [], first_id: null, last_id: null, has_more: false };
  }
  if (/^\/api\/agent-runs\/[^/]+\/trace$/.test(pathname)) {
    return { run_id: "run-cancel-mock", status: "unavailable", events: [] };
  }
  return {};
}

async function startMockApi() {
  const state = {
    sessionId: "",
    activeRunId: null,
    streamRequests: 0,
    cancelRunIds: [],
    firstStream: null,
    cancelRequestedAt: 0,
    firstStreamClosedAt: 0,
    secondCompleted: false,
  };
  const server = createServer(async (req, res) => {
    const url = new URL(req.url || "/", "http://mock-runtime");
    if (req.method === "OPTIONS") {
      writeCorsHeaders(res);
      res.writeHead(204);
      res.end();
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/agent-runtime/sdk-events") {
      const body = await readJson(req);
      state.streamRequests += 1;
      state.sessionId = String(body.session_id || "cancel-session");
      const runId = state.streamRequests === 1 ? "run-cancel-mock" : "run-after-cancel";
      state.activeRunId = runId;
      writeCorsHeaders(res, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
        "X-AgentGov-Run-Id": runId,
        "X-AgentGov-Session-Id": state.sessionId,
      });
      res.writeHead(200);
      res.flushHeaders();
      res.write(nativeSseEvent("agentgov.session", {
        run_id: runId,
        session_id: state.sessionId,
        sdk_session_id: "sdk-cancel-mock",
        agent_version_id: "v-cancel-mock",
      }));
      if (state.streamRequests === 1) {
        state.firstStream = res;
        res.on("close", () => { state.firstStreamClosedAt = Date.now(); });
        res.write(nativeSseEvent("claude.sdk.StreamEvent", {
          uuid: "cancel-partial",
          session_id: "sdk-cancel-mock",
          parent_tool_use_id: null,
          event: {
            type: "content_block_delta",
            index: 0,
            delta: { type: "text_delta", text: "已生成的部分输出" },
          },
        }));
        return;
      }
      const answer = "SECOND_OK";
      res.write(nativeSseEvent("claude.sdk.AssistantMessage", {
        message_id: "after-cancel-message",
        parent_tool_use_id: null,
        session_id: "sdk-cancel-mock",
        model: "mock",
        content: [{ text: answer }],
      }));
      res.write(nativeSseEvent("agentgov.result", {
        run_id: runId,
        session_id: state.sessionId,
        agent_version_id: "v-cancel-mock",
        agent_activity: { tool_calls: [], tool_results: [], tool_names: [] },
      }));
      state.activeRunId = null;
      state.secondCompleted = true;
      res.end(nativeSseEvent("agentgov.done", {}));
      return;
    }
    const cancelMatch = url.pathname.match(/^\/api\/agent-runs\/([^/]+)\/cancel$/);
    if (req.method === "POST" && cancelMatch) {
      const runId = decodeURIComponent(cancelMatch[1]);
      state.cancelRunIds.push(runId);
      state.cancelRequestedAt = Date.now();
      await new Promise((resolve) => setTimeout(resolve, 350));
      state.activeRunId = null;
      state.firstStream?.write(nativeSseEvent("agentgov.cancelled", {
        run_id: runId,
        session_id: state.sessionId,
        turn_status: "cancelled",
      }));
      state.firstStream?.end(nativeSseEvent("agentgov.done", {}));
      json(res, {
        run_id: runId,
        session_id: state.sessionId,
        turn_status: "cancelled",
        cancelled: true,
        completed_at: "2026-07-28T00:01:00Z",
        session_active_run_id: null,
      });
      return;
    }
    json(res, mockPayload(url.pathname, state));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("mock API did not bind a TCP port");
  return {
    apiBase: `http://127.0.0.1:${address.port}`,
    state,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

async function main() {
  const mockApi = real ? null : await startMockApi();
  const apiBase = configuredApiBase || mockApi?.apiBase;
  if (!apiBase) throw new Error("RUNTIME_API_BASE is required in real-container mode");
  const apiKey = process.env.RUNTIME_API_KEY
    || envValue("FRONTEND_RUNTIME_API_KEY")
    || envValue("API_KEY");
  const vite = real ? null : startVite();
  const requestedPaths = [];
  let browser;
  try {
    await waitForUrl(uiBase, real ? 60000 : 30000);
    browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
    page.on("request", (request) => {
      if (!request.url().startsWith(apiBase)) return;
      const url = new URL(request.url());
      requestedPaths.push(`${request.method()} ${url.pathname}`);
    });
    await page.addInitScript(([base, key]) => {
      window.localStorage.setItem(
        "runtime-client-config",
        JSON.stringify({ apiBase: base, apiKey: key }),
      );
      window.localStorage.removeItem("playground-active-session");
      window.localStorage.removeItem("playground-selected-business-agent");
      window.localStorage.removeItem("playground-session-messages");
    }, [apiBase, apiKey]);
    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 30000 });
    await page.getByTestId("topbar-agent-switcher").waitFor({ timeout: 30000 });
    await page.waitForFunction(() => {
      const selector = document.querySelector('[data-testid="topbar-agent-switcher"]');
      return selector instanceof HTMLSelectElement && Boolean(selector.value);
    }, undefined, { timeout: 30000 });

    const input = page.getByTestId("chat-composer-input");
    await input.fill(
      real
        ? "请生成一份较长的分步排查清单，至少 80 条，每条给出解释。"
        : "生成长任务用于取消竞态验收",
    );
    await page.getByTestId("chat-send").click();
    const stop = page.getByTestId("chat-stop");
    await stop.waitFor({ timeout: 30000 });
    if (!real) {
      await page.getByText("已生成的部分输出", { exact: true }).waitFor({ timeout: 10000 });
    }
    await stop.click();

    let pendingLocked = true;
    if (!real) {
      await page.waitForFunction(() => {
        const button = document.querySelector('[data-testid="chat-stop"]');
        return button?.hasAttribute("disabled") && button.textContent?.includes("停止中");
      }, undefined, { timeout: 5000 });
      await input.fill("停止确认前不得发送");
      await input.press(process.platform === "darwin" ? "Meta+Enter" : "Control+Enter");
      await page.waitForTimeout(100);
      pendingLocked = mockApi.state.streamRequests === 1;
    }

    await page.getByTestId("chat-send").waitFor({ timeout: real ? 120000 : 15000 });
    const firstAssistant = page.locator('[data-message-role="assistant"]').last();
    const firstText = await firstAssistant.innerText();
    const cancellationNotRenderedAsFailure = !firstText.includes("运行失败")
      && !firstText.includes("SESSION_CONFLICT");

    await input.fill(real ? "只回复 SECOND_OK" : "取消后立即重试");
    await page.getByTestId("chat-send").click();
    await page.getByTestId("chat-send").waitFor({ timeout: real ? 120000 : 15000 });
    const bodyText = await page.locator("body").innerText();
    const secondAssistantText = await page.locator('[data-message-role="assistant"]').last().innerText();
    const exactCancelPath = requestedPaths.some((path) => (
      path === "POST /api/agent-runs/run-cancel-mock/cancel"
    )) || (real && requestedPaths.some((path) => /^POST \/api\/agent-runs\/[^/]+\/cancel$/.test(path)));
    const result = {
      pendingLocked,
      exactCancelPath,
      secondRequestSent: requestedPaths.filter(
        (path) => path === "POST /api/agent-runtime/sdk-events",
      ).length === 2,
      cancellationNotRenderedAsFailure,
      noSessionConflict: !bodyText.includes("SESSION_CONFLICT"),
      followUpCompleted: real ? secondAssistantText.trim().length > 0 : secondAssistantText.includes("SECOND_OK"),
      cancelBeforeFirstStreamClose: real
        ? true
        : mockApi.state.cancelRequestedAt > 0
          && mockApi.state.firstStreamClosedAt >= mockApi.state.cancelRequestedAt,
      cancelRunIds: real ? undefined : mockApi.state.cancelRunIds,
    };
    const passed = Object.entries(result)
      .filter(([key]) => key !== "cancelRunIds")
      .every(([, value]) => value === true);
    await page.screenshot({
      path: join(screenshotDir, "playground-cancel-and-retry.png"),
      fullPage: true,
    });
    console.log(JSON.stringify({
      status: passed ? "passed" : "failed",
      mode: real ? "real-container" : "mock",
      result,
    }, null, 2));
    if (!passed) process.exitCode = 1;
  } finally {
    await browser?.close();
    await stopChild(vite);
    await mockApi?.close();
  }
}

main().catch((error) => {
  console.error(`verify_playground_cancel failed: ${error?.stack || error}`);
  process.exit(2);
});
