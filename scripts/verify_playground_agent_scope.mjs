#!/usr/bin/env node
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.env.PLAYGROUND_AGENT_SCOPE_UI_PORT || 55219);
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || mkdtempSync(join(tmpdir(), "agentgov-agent-scope-"));
const timestamp = "2026-07-23T00:00:00Z";

const agents = [
  {
    agent_id: "agent-a",
    name: "业务 Agent A",
    category: "business",
    workspace_dir: "/data/business-agents/agent-a/workspace",
    created_at: timestamp,
    status: "active",
    builtin: false,
    default: true,
    protected: false,
    requires_web_hitl: false,
  },
  {
    agent_id: "agent-b",
    name: "业务 Agent B",
    category: "business",
    workspace_dir: "/data/business-agents/agent-b/workspace",
    created_at: timestamp,
    status: "active",
    builtin: false,
    default: false,
    protected: false,
    requires_web_hitl: false,
  },
];

const conversations = [
  {
    id: "conv_session-a",
    title: "Agent A 历史会话",
    created_at: 1784764800,
    metadata: {},
    agentgov: { agent_id: "agent-a", turns: 1, updated_at: timestamp },
  },
  {
    id: "conv_session-b",
    title: "Agent B 历史会话",
    created_at: 1784764800,
    metadata: {},
    agentgov: { agent_id: "agent-b", turns: 1, updated_at: timestamp },
  },
];

function startVite() {
  const child = spawn(
    "pnpm",
    ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"],
    { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"], detached: true },
  );
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  return child;
}

function killTree(child, signal) {
  try {
    process.kill(-child.pid, signal);
  } catch {
    try { child.kill(signal); } catch { /* already stopped */ }
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

async function waitForVite() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(uiBase);
      if (response.ok) return;
    } catch {
      // Wait for Vite.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Vite did not become ready at ${uiBase}`);
}

function json(route, data, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify(data),
  });
}

function sse(route, events) {
  return route.fulfill({
    status: 200,
    contentType: "text/event-stream; charset=utf-8",
    headers: { "access-control-allow-origin": "*" },
    body: events.map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(""),
  });
}

function presentation(agentId) {
  if (agentId === "agent-b") {
    return {
      agent_id: "agent-b",
      name: "业务 Agent B",
      version: "2.0.0",
      language: "zh-CN",
      runtime: "claude-code",
      capabilities: ["analysis"],
      summary: "Agent B 的结构化摘要",
      welcome_message: "**Agent B 已准备好**\n\n- 提供业务背景\n- 说明期望产出",
      composer_placeholder: "输入 Agent B 的任务背景",
      starter_prompts: [{ label: "开始 B 分析", prompt: "请执行 Agent B 的建议任务。" }],
      source: "agent_yaml",
    };
  }
  return {
    agent_id: "agent-a",
    name: "业务 Agent A",
    version: null,
    language: null,
    runtime: null,
    capabilities: [],
    summary: null,
    welcome_message: null,
    composer_placeholder: null,
    starter_prompts: [],
    source: "registry_fallback",
  };
}

function conversationItems(sessionId) {
  const prefix = sessionId === "session-a" ? "A" : "B";
  return {
    object: "list",
    data: [
      { id: `${sessionId}-user`, role: "user", parent_tool_use_id: null, content: [{ type: "text", text: `${prefix} 历史问题` }] },
      { id: `${sessionId}-assistant`, role: "assistant", parent_tool_use_id: null, content: [{ type: "text", text: `${prefix} 历史回答` }] },
    ],
    first_id: `${sessionId}-user`,
    last_id: `${sessionId}-assistant`,
    has_more: false,
  };
}

async function installMockRoutes(page, state) {
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === uiBase) return route.continue();
    if (url.hostname !== "runtime.test") return route.continue();

    const path = url.pathname;
    if (path === "/health") {
      return json(route, { status: "ok", runtime_version: "3.0.1", model: "mock-model" });
    }
    if (path === "/v1/conversations") {
      return json(route, { object: "list", data: conversations, first_id: "conv_session-a", last_id: "conv_session-b", has_more: false });
    }
    const itemMatch = path.match(/^\/v1\/conversations\/conv_(session-[ab])\/items$/);
    if (itemMatch) return json(route, conversationItems(itemMatch[1]));
    if (path === "/api/agent-registry") return json(route, agents);
    const presentationMatch = path.match(/^\/api\/agent-registry\/([^/]+)\/presentation$/);
    if (presentationMatch) return json(route, presentation(decodeURIComponent(presentationMatch[1])));
    if (path === "/v1/responses" && request.method() === "POST") {
      const body = request.postDataJSON();
      state.responseRequests.push(body);
      const sessionId = typeof body.conversation === "string" && body.conversation.startsWith("conv_")
        ? body.conversation.slice("conv_".length)
        : "new-agent-b-session";
      await new Promise((resolve) => setTimeout(resolve, 500));
      return sse(route, [
        { event: "agentgov.session", data: { session_id: sessionId } },
        { event: "response.output_text.delta", data: { delta: "Agent B 响应" } },
        { event: "response.completed", data: { response: { status: "completed", output: [{ type: "message", content: [{ type: "output_text", text: "Agent B 响应" }] }] } } },
        { event: "agentgov.done", data: { ok: true } },
      ]);
    }
    if (path === "/api/agents" || path === "/api/skills" || path === "/api/agent-change-sets" || path === "/api/agent-releases") {
      return json(route, []);
    }
    if (path === "/api/config") return json(route, { mappings: [] });
    if (path === "/api/agent-repository") return json(route, { status: "active", dirty: false, changed_files: [], file_diffs: [] });
    if (path === "/api/agent-repository/current") {
      return json(route, { agent_version_id: "v-mock", commit_sha: "mock", created_at: timestamp, reason: "current" });
    }
    return json(route, {});
  });
}

async function waitForSelectValue(page, expected) {
  await page.waitForFunction(
    (value) => document.querySelector('[data-testid="topbar-agent-switcher"]')?.value === value,
    expected,
    { timeout: 10000 },
  );
}

async function assertSessionList(page, included, excluded) {
  const list = page.getByTestId("playground-session-list");
  const text = await list.innerText();
  if (!text.includes(included) || text.includes(excluded)) {
    throw new Error(`session list owner scope mismatch: ${text}`);
  }
}

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
    const state = { responseRequests: [] };
    const audit = { consoleErrors: [], pageErrors: [], requestFailures: [], httpErrors: [] };
    page.on("console", (message) => {
      if (message.type() === "error") audit.consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => audit.pageErrors.push(error.message));
    page.on("requestfailed", (request) => {
      const failure = request.failure()?.errorText || "unknown";
      const path = new URL(request.url()).pathname;
      if (path.endsWith("/presentation") && /ERR_ABORTED|NS_BINDING_ABORTED/.test(failure)) return;
      audit.requestFailures.push(`${request.method()} ${request.url()}: ${failure}`);
    });
    page.on("response", (response) => {
      if (response.status() >= 400) audit.httpErrors.push(`${response.status()} ${response.url()}`);
    });
    await page.addInitScript((api) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: api, apiKey: "mock-key" }));
      window.localStorage.setItem("playground-selected-business-agent", JSON.stringify("agent-b"));
      window.localStorage.setItem("playground-active-session", JSON.stringify("session-a"));
      window.localStorage.removeItem("playground-session-messages");
    }, apiBase);
    await installMockRoutes(page, state);

    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 10000 });
    await waitForSelectValue(page, "agent-a");
    if ((await page.getByTestId("runtime-version").innerText()) !== "v3.0.1") {
      throw new Error("Topbar did not render the Runtime version from /health");
    }
    await page.getByText("A 历史回答", { exact: true }).waitFor({ timeout: 10000 });

    await page.getByTestId("playground-session-trigger").click();
    await assertSessionList(page, "Agent A 历史会话", "Agent B 历史会话");
    await page.getByTestId("chat-composer-input").fill("Agent A 未发送草稿");
    await page.getByTestId("topbar-agent-switcher").selectOption("agent-b");
    await page.getByTestId("welcome-card").waitFor({ timeout: 10000 });
    if (await page.getByText("A 历史回答", { exact: true }).count()) {
      throw new Error("Agent A history remained visible after switching to Agent B");
    }
    if ((await page.getByTestId("chat-composer-input").inputValue()) !== "") {
      throw new Error("Agent draft was not cleared on Agent switch");
    }
    if ((await page.getByTestId("welcome-summary").innerText()) !== "Agent B 的结构化摘要") {
      throw new Error("Agent B structured summary was not rendered");
    }
    if (!(await page.getByTestId("welcome-message").innerText()).includes("Agent B 已准备好")) {
      throw new Error("Agent B static welcome Markdown was not rendered");
    }
    if (!(await page.getByTestId("welcome-metadata").innerText()).includes("v2.0.0")) {
      throw new Error("Agent B manifest version was not rendered");
    }
    if ((await page.getByTestId("chat-composer-input").getAttribute("placeholder")) !== "输入 Agent B 的任务背景") {
      throw new Error("Agent B composer placeholder was not applied");
    }
    await page.screenshot({ path: join(screenshotDir, "playground-agent-b-welcome.png"), fullPage: true });

    await page.getByTestId("playground-session-trigger").click();
    await assertSessionList(page, "Agent B 历史会话", "Agent A 历史会话");
    await page.getByTestId("starter-prompt").click();
    if ((await page.getByTestId("chat-composer-input").inputValue()) !== "请执行 Agent B 的建议任务。") {
      throw new Error("starter prompt did not fill the composer");
    }
    if (state.responseRequests.length !== 0) throw new Error("starter prompt was sent automatically");

    await page.getByTestId("chat-send").click();
    await page.waitForFunction(
      () => document.querySelector('[data-testid="topbar-agent-switcher"]')?.disabled === true,
      null,
      { timeout: 5000 },
    );
    await page.getByTestId("playground-messages").getByText("Agent B 响应", { exact: true }).waitFor({ timeout: 10000 });
    await page.waitForFunction(
      () => document.querySelector('[data-testid="topbar-agent-switcher"]')?.disabled === false,
      null,
      { timeout: 5000 },
    );
    if (state.responseRequests.length !== 1) throw new Error(`expected one model request, got ${state.responseRequests.length}`);
    const request = state.responseRequests[0];
    if (request.agentgov?.agent_id !== "agent-b") {
      throw new Error(`request used the wrong business Agent: ${JSON.stringify(request)}`);
    }
    if (request.conversation === "conv_session-a" || request.conversation === "conv_session-b") {
      throw new Error(`Agent switch reused a historical conversation: ${request.conversation}`);
    }

    await page.getByTestId("topbar-agent-switcher").selectOption("agent-a");
    await page.getByTestId("welcome-card").waitFor({ timeout: 10000 });
    await page.getByTestId("playground-session-trigger").click();
    await assertSessionList(page, "Agent A 历史会话", "Agent B 历史会话");
    await page.getByText("Agent A 历史会话", { exact: true }).click();
    await page.getByText("A 历史回答", { exact: true }).waitFor({ timeout: 10000 });

    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForSelectValue(page, "agent-a");
    await page.getByText("A 历史回答", { exact: true }).waitFor({ timeout: 10000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    if (overflow) throw new Error("Playground Agent scope UI caused horizontal overflow");
    if (Object.values(audit).some((items) => items.length)) {
      throw new Error(`browser audit failed: ${JSON.stringify(audit)}`);
    }

    await page.screenshot({ path: join(screenshotDir, "playground-agent-scope.png"), fullPage: true });
    await browser.close();
    console.log(JSON.stringify({ ok: true, responseRequests: state.responseRequests.length, screenshotDir }, null, 2));
  } finally {
    await stopChild(server);
  }
}

main().catch((error) => {
  console.error(`verify_playground_agent_scope failed: ${error?.stack || error}`);
  process.exit(1);
});
