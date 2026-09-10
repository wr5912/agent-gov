#!/usr/bin/env node
// Playground AgentScope session interruption contract:
// mock mode owns a deliberately blocked Session SSE stream; real mode exercises rebuilt Compose UI/API.
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";
import { provisionRuntimeAgent } from "./improvement_ui_e2e/runtime_client.mjs";
import {
  attachCancelNetworkEvidence,
  exercisePlaygroundCancellation,
  installPendingCancelProbe,
} from "./improvement_ui_e2e/playground_cancel_evidence.mjs";
import {
  attachUiDiagnostics,
  cleanupResources,
  closeMockServer,
  failureDiagnostic,
  listenLoopback,
  runtimeConnection,
  startMockUi,
  waitForUi,
} from "./improvement_ui_e2e/playground_cancel_runtime.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const governanceAgentId = "security-operations-expert";
const runtimeAgentId = "runtime-cancel-mock";
const agentVersionId = "v-cancel-mock";
const real = Boolean(process.env.RUNTIME_UI_BASE);
requireContainerAcceptance(real);

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

function writeCorsHeaders(res, extra = {}) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS");
  res.setHeader(
    "Access-Control-Allow-Headers",
    "Authorization,Content-Type,Idempotency-Key,X-User-ID",
  );
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

function nativeSseEvent(data) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function mockPayload(pathname, state) {
  if (pathname === "/health") {
    return { status: "ok", model: "cancel-mock", provider_key_configured: true };
  }
  if (pathname === "/api/agent-registry") {
    return [{
      agent_id: governanceAgentId,
      name: "Security Operations Expert",
      category: "business",
      workspace_dir: "/runtime/business-agent",
      created_at: "2026-07-28T00:00:00Z",
      status: "active",
      builtin: true,
      default: true,
      protected: true,
      requires_web_hitl: false,
      agent_version_id: agentVersionId,
      harness_digest: "a".repeat(64),
      runtime_agent_id: runtimeAgentId,
    }];
  }
  if (pathname.endsWith("/presentation")) {
    return {
      agent_id: governanceAgentId,
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
      agent_version_id: agentVersionId,
      commit_sha: "cancel-mock",
      created_at: "2026-07-28T00:00:00Z",
      reason: "current",
    };
  }
  if (pathname === "/api/config") return { mappings: [] };
  if (pathname === "/api/agent-runs") return state.runs;
  if (pathname === "/api/agents" || pathname === "/api/skills") return [];
  if (pathname === "/api/agent-change-sets" || pathname === "/api/agent-releases") return [];
  if (/^\/api\/agent-runs\/[^/]+$/.test(pathname)) {
    return state.runs.find((run) => pathname.endsWith(`/${run.run_id}`)) || {};
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
    chatRequests: 0,
    interruptSessions: [],
    firstStream: null,
    interruptRequestedAt: 0,
    firstStreamClosedAt: 0,
    secondCompleted: false,
    status: "idle",
    runs: [],
  };
  const server = createServer(async (req, res) => {
    const url = new URL(req.url || "/", "http://mock-runtime");
    if (req.method === "OPTIONS") {
      writeCorsHeaders(res);
      res.writeHead(204);
      res.end();
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/runtime/sessions/") {
      const body = await readJson(req);
      if (body.agent_id !== runtimeAgentId) return json(res, { detail: "agent mismatch" }, 422);
      state.sessionId ||= "cancel-session";
      json(res, { session_id: state.sessionId }, 201);
      return;
    }
    if (req.method === "GET" && url.pathname === "/api/runtime/sessions/") {
      const sessions = state.sessionId ? [{
        session: {
          id: state.sessionId,
          agent_id: runtimeAgentId,
          name: "取消竞态验收",
          created_at: "2026-09-09T00:00:00Z",
          updated_at: "2026-09-09T00:00:00Z",
          metadata: {},
        },
        is_running: state.status !== "idle",
        status: state.status,
        team: null,
      }] : [];
      json(res, { sessions, total: sessions.length });
      return;
    }
    const sessionRoute = url.pathname.match(/^\/api\/runtime\/sessions\/([^/]+)\/(stream|messages|status|interrupt)$/);
    if (sessionRoute && decodeURIComponent(sessionRoute[1]) === state.sessionId) {
      const action = sessionRoute[2];
      if (req.method === "GET" && action === "messages") {
        const messages = state.runs
          .filter((run) => run.status === "interrupted" || run.status === "succeeded")
          .map((run) => ({
            id: run.reply_ids[0],
            name: "agent",
            role: "assistant",
            content: [{
              type: "text",
              text: run.status === "succeeded" ? "SECOND_OK" : "已生成的部分输出",
            }],
            metadata: { run_id: run.run_id },
            created_at: "2026-09-09T00:00:02Z",
            finished_reason: run.status === "succeeded" ? "completed" : "interrupted",
            error: null,
          }));
        json(res, { messages, is_running: state.status !== "idle", has_more: false });
        return;
      }
      if (req.method === "GET" && action === "status") {
        json(res, { session_id: state.sessionId, status: state.status });
        return;
      }
      if (req.method === "GET" && action === "stream") {
        state.streamRequests += 1;
        writeCorsHeaders(res, {
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-store",
        });
        res.writeHead(200);
        res.flushHeaders();
        res.write(":\n\n");
        state.currentStream = res;
        if (state.streamRequests === 1) {
          state.firstStream = res;
          res.on("close", () => { state.firstStreamClosedAt = Date.now(); });
        }
        return;
      }
      if (req.method === "POST" && action === "interrupt") {
        state.interruptSessions.push(state.sessionId);
        state.interruptRequestedAt = Date.now();
        json(res, { session_id: state.sessionId }, 202);
        setTimeout(() => {
          state.status = "idle";
          state.activeRunId = null;
          const run = state.runs.find((item) => item.run_id === "run-cancel-mock");
          if (run) run.status = "interrupted";
          state.firstStream?.end(nativeSseEvent({
            id: "cancel-end",
            created_at: "2026-09-09T00:00:02Z",
            metadata: {},
            type: "REPLY_END",
            session_id: state.sessionId,
            reply_id: "reply-cancel",
            finished_reason: "interrupted",
            error: null,
          }));
        }, 350);
        return;
      }
    }
    if (req.method === "POST" && url.pathname === "/api/runtime/chat/") {
      const body = await readJson(req);
      state.chatRequests += 1;
      const runId = state.chatRequests === 1 ? "run-cancel-mock" : "run-after-cancel";
      const replyId = state.chatRequests === 1 ? "reply-cancel" : "reply-after-cancel";
      state.activeRunId = runId;
      state.status = "running";
      state.runs.push({
        run_id: runId,
        session_id: state.sessionId,
        agent_id: governanceAgentId,
        agent_version_id: agentVersionId,
        runtime_agent_id: runtimeAgentId,
        client_operation_id: body.client_operation_id,
        status: "running",
        reply_ids: [replyId],
        created_at: "2026-09-09T00:00:00Z",
        updated_at: "2026-09-09T00:00:00Z",
      });
      writeCorsHeaders(res, {
        "Content-Type": "application/json",
        "X-AgentGov-Run-Id": runId,
        "X-AgentGov-Session-Id": state.sessionId,
      });
      res.writeHead(200);
      res.end(JSON.stringify({ status: "started", session_id: state.sessionId }));
      const stream = state.currentStream;
      setTimeout(() => stream?.write(nativeSseEvent({
        id: `start-${state.chatRequests}`,
        created_at: "2026-09-09T00:00:00Z",
        metadata: {},
        type: "REPLY_START",
        session_id: state.sessionId,
        reply_id: replyId,
        name: "agent",
        role: "assistant",
      })), 20);
      setTimeout(() => stream?.write(nativeSseEvent({
        id: `delta-${state.chatRequests}`,
        created_at: "2026-09-09T00:00:01Z",
        metadata: {},
        type: "TEXT_BLOCK_DELTA",
        reply_id: replyId,
        block_id: `block-${state.chatRequests}`,
        delta: state.chatRequests === 1 ? "已生成的部分输出" : "SECOND_OK",
      })), 40);
      if (state.chatRequests === 2) {
        setTimeout(() => {
          state.status = "idle";
          state.activeRunId = null;
          state.secondCompleted = true;
          const run = state.runs.find((item) => item.run_id === runId);
          if (run) run.status = "succeeded";
          stream?.end(nativeSseEvent({
            id: "second-end",
            created_at: "2026-09-09T00:00:02Z",
            metadata: {},
            type: "REPLY_END",
            session_id: state.sessionId,
            reply_id: replyId,
            finished_reason: "completed",
            error: null,
          }));
        }, 60);
      }
      return;
    }
    json(res, mockPayload(url.pathname, state));
  });
  const address = await listenLoopback(server);
  return {
    apiBase: `http://127.0.0.1:${address.port}`,
    state,
    close: () => closeMockServer(server),
  };
}

async function main() {
  let mockApi, ui, browser;
  let stage = "runtime_start";
  let diagnostics = [];
  try {
    mockApi = real ? null : await startMockApi();
    const connection = runtimeConnection({ real, environment: process.env, mockApiBase: mockApi?.apiBase, readDeploymentEnv: envValue });
    const config = { ...connection, actionTimeoutMs: real ? 120000 : 15000 };
    const { apiBase, apiKey } = connection;
    ui = real ? null : await startMockUi({ frontendRoot: join(repoRoot, "frontend"), apiBase });
    const uiBase = real ? process.env.RUNTIME_UI_BASE.replace(/\/$/, "") : ui.uiBase;
    stage = "ui_ready";
    await waitForUi(uiBase, real ? 60000 : 30000);
    const binding = real ? await provisionRuntimeAgent(config, governanceAgentId) : {
      governance_agent_id: governanceAgentId, runtime_agent_id: runtimeAgentId, agent_version_id: agentVersionId,
    };
    browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
    diagnostics = attachUiDiagnostics(page);
    const network = attachCancelNetworkEvidence(page, apiBase);
    await installPendingCancelProbe(page);
    await page.addInitScript(([base, key]) => {
      window.localStorage.setItem(
        "runtime-client-config",
        JSON.stringify({ apiBase: base, apiKey: key }),
      );
      window.localStorage.removeItem("playground-active-session");
      window.localStorage.removeItem("playground-selected-business-agent");
      window.localStorage.removeItem("playground-session-messages");
    }, [apiBase, apiKey]);
    stage = "playground_ready";
    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 30000 });
    await page.getByTestId("topbar-agent-switcher").waitFor({ timeout: 30000 });
    await page.waitForFunction(() => {
      const selector = document.querySelector('[data-testid="topbar-agent-switcher"]');
      return selector instanceof HTMLSelectElement && Boolean(selector.value);
    }, undefined, { timeout: 30000 });

    await page.getByTestId("topbar-agent-switcher").selectOption(governanceAgentId);
    stage = "cancellation";
    const { result, runs } = await exercisePlaygroundCancellation(page, config, binding, network, real);
    const passed = Object.values(result).every((value) => value === true);
    await page.screenshot({
      path: join(screenshotDir, "playground-cancel-and-retry.png"),
      fullPage: true,
    });
    console.log(JSON.stringify({
      status: passed ? "passed" : "failed",
      mode: real ? "real-container" : "mock",
      result,
      runs,
    }, null, 2));
    if (!passed) process.exitCode = 1;
  } catch (error) {
    console.error(JSON.stringify(failureDiagnostic(error, stage, diagnostics)));
    process.exitCode = 2;
  } finally {
    const failed = await cleanupResources([
      { name: "browser", close: () => browser?.close() },
      { name: "ui", close: () => ui?.close() },
      { name: "mock_api", close: () => mockApi?.close() },
    ]);
    if (failed.length) {
      console.error(JSON.stringify({ status: "failed", stage: "cleanup", code: "RESOURCE_CLEANUP_FAILED", resources: failed }));
      process.exitCode = 2;
    }
  }
}

main().catch((error) => {
  console.error(JSON.stringify(failureDiagnostic(error, "bootstrap", [])));
  process.exit(2);
});
