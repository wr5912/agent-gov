#!/usr/bin/env node
// Playground Claude native user-input UI contract:
// mock SSE -> render HITL cards -> submit decisions -> keep decision token out of visible/local persisted trace.
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
const port = Number(process.env.CLAUDE_USER_INPUT_UI_PORT || 55218);
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || mkdtempSync(join(tmpdir(), "agentgov-claude-user-input-"));

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
  try { process.kill(-child.pid, signal); } catch { try { child.kill(signal); } catch { /* already gone */ } }
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
      const response = await fetch(uiBase);
      if (response.ok) return;
    } catch { /* wait */ }
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

function routeData(path) {
  if (path === "/health") return { status: "ok", model: "hitl-ui-mock", provider_key_configured: true, claude_web_hitl_enabled: true };
  if (path === "/api/sessions") return [];
  if (path === "/api/agents" || path === "/api/skills" || path === "/api/agent-change-sets" || path === "/api/agent-releases") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-registry") {
    return [
      { agent_id: "main-agent", name: "Main Agent", category: "baseline", workspace_dir: "/runtime/main", created_at: "2026-06-29T00:00:00Z", status: "active" },
      { agent_id: "response-disposal", name: "Response Disposal", category: "soc", workspace_dir: "/runtime/response-disposal", created_at: "2026-06-29T00:00:00Z", status: "active" },
    ];
  }
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v-hitl-ui", commit_sha: "mock", created_at: "2026-06-29T00:00:00Z", reason: "current" };
  return {};
}

function requestEvent(kind) {
  const common = {
    business_agent_id: "response-disposal",
    run_id: `run-${kind}`,
    session_id: "hitl-ui-session",
    api_session_id: "hitl-ui-session",
    sdk_session_id: "sdk-hitl-ui-session",
    status: "waiting",
    context: { tool_use_id: `toolu-${kind}` },
    risk: kind === "question" ? { level: "info", reason: "Claude needs input." } : { level: "high", reason: "Tool execution requires user confirmation." },
    created_at: "2026-06-29T00:00:00Z",
    expires_at: "2026-06-29T00:05:00Z",
  };
  if (kind === "question") {
    return {
      ...common,
      request_id: "cur-question",
      decision_token: "secret-question-token",
      request_type: "ask_user_question",
      tool_name: "AskUserQuestion",
      redacted_input: {
        questions: [
          {
            header: "Scope",
            question: "Which assets should be handled?",
            options: [{ label: "Current alert asset" }, { label: "All related assets" }],
          },
        ],
      },
    };
  }
  return {
    ...common,
    request_id: "cur-tool",
    decision_token: "secret-tool-token",
    request_type: "tool_permission",
    tool_name: "Bash",
    redacted_input: { command: "echo hitl-smoke", api_key: "<redacted>" },
  };
}

async function installMockRoutes(page, streamRequests, decisionRequests) {
  let streamIndex = 0;
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === uiBase) return route.continue();
    if (url.hostname !== "runtime.test") return route.continue();
    const path = url.pathname;
    if (path === "/api/chat/stream" && request.method() === "POST") {
      const body = request.postDataJSON();
      streamRequests.push(body);
      streamIndex += 1;
      const kind = streamIndex === 1 ? "tool" : "question";
      return sse(route, [
        { event: "session", data: { session_id: body.session_id, sdk_session_id: "sdk-hitl-ui-session", run_id: `run-${kind}` } },
        { event: "claude_user_input_required", data: requestEvent(kind) },
      ]);
    }
    const decisionMatch = path.match(/^\/api\/claude-user-input-requests\/([^/]+)\/decision$/);
    if (decisionMatch && request.method() === "POST") {
      const body = request.postDataJSON();
      decisionRequests.push({ requestId: decodeURIComponent(decisionMatch[1]), body });
      return json(route, {
        request_id: decodeURIComponent(decisionMatch[1]),
        status: "resolved",
        decision: body.action,
        resolved_at: "2026-06-29T00:01:00Z",
      });
    }
    return json(route, routeData(path));
  });
}

async function persistedText(page) {
  return page.evaluate(() => Object.values(window.localStorage).join("\n"));
}

async function waitForCondition(check, message, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(message);
}

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
    const streamRequests = [];
    const decisionRequests = [];
    await page.addInitScript((api) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: api, apiKey: "mock-key" }));
      window.localStorage.removeItem("playground-session-messages");
      window.localStorage.removeItem("playground-active-session");
    }, apiBase);
    await installMockRoutes(page, streamRequests, decisionRequests);

    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 10000 });
    await page.getByTestId("topbar-agent-switcher").selectOption("response-disposal");
    await page.getByTestId("chat-composer-input").fill("trigger tool confirmation");
    await page.getByTestId("chat-send").click();

    const toolCard = page.locator('[data-testid="claude-user-input-card"][data-request-type="tool_permission"]').first();
    await toolCard.waitFor({ timeout: 10000 });
    if (!(await toolCard.innerText()).includes("Bash")) throw new Error("tool permission card did not render the requested tool");
    const visibleAfterTool = await page.locator("body").innerText();
    const storedAfterTool = await persistedText(page);
    if (visibleAfterTool.includes("secret-tool-token") || storedAfterTool.includes("secret-tool-token")) {
      throw new Error("tool decision token leaked into visible UI or localStorage");
    }
    await toolCard.getByTestId("claude-user-input-allow").click();
    await waitForCondition(() => decisionRequests.length >= 1, "tool decision request was not posted");
    await toolCard.getByText("已处理").waitFor({ timeout: 10000 });

    await page.getByTestId("chat-composer-input").fill("trigger ask user question");
    await page.getByTestId("chat-send").click();
    const questionCard = page.locator('[data-testid="claude-user-input-card"][data-request-type="ask_user_question"]').last();
    await questionCard.waitFor({ timeout: 10000 });
    await questionCard.getByTestId("claude-user-input-other").fill("Only the current alert asset.");
    await questionCard.getByTestId("claude-user-input-submit-answer").click();
    await waitForCondition(() => decisionRequests.length >= 2, "AskUserQuestion decision request was not posted");
    await questionCard.getByText("已处理").waitFor({ timeout: 10000 });

    const visibleAfterQuestion = await page.locator("body").innerText();
    const storedAfterQuestion = await persistedText(page);
    if (visibleAfterQuestion.includes("secret-question-token") || storedAfterQuestion.includes("secret-question-token")) {
      throw new Error("AskUserQuestion decision token leaked into visible UI or localStorage");
    }
    if (streamRequests.length !== 2) throw new Error(`expected 2 stream requests, got ${streamRequests.length}`);
    if (streamRequests.some((item) => item.agent_id !== "response-disposal")) {
      throw new Error(`topbar agent selection did not flow into chat requests: ${JSON.stringify(streamRequests)}`);
    }
    if (decisionRequests.length !== 2) throw new Error(`expected 2 decision requests, got ${decisionRequests.length}`);
    const [toolDecision, questionDecision] = decisionRequests;
    if (toolDecision.requestId !== "cur-tool" || toolDecision.body.action !== "allow_once" || toolDecision.body.decision_token !== "secret-tool-token") {
      throw new Error(`invalid tool decision body: ${JSON.stringify(toolDecision)}`);
    }
    if (questionDecision.requestId !== "cur-question" || questionDecision.body.action !== "answer_question" || questionDecision.body.response !== "Only the current alert asset.") {
      throw new Error(`invalid AskUserQuestion decision body: ${JSON.stringify(questionDecision)}`);
    }
    if (decisionRequests.some((item) => "updated_input" in item.body || item.body.action === "allow_modified")) {
      throw new Error(`decision request exposed mutable tool input: ${JSON.stringify(decisionRequests)}`);
    }
    await page.screenshot({ path: join(screenshotDir, "mock-claude-user-input.png"), fullPage: true });
    await browser.close();
    console.log(JSON.stringify({ ok: true, streamRequests: streamRequests.length, decisionRequests: decisionRequests.length, screenshotDir }, null, 2));
  } finally {
    await stopChild(server);
  }
}

main().catch(async (error) => {
  console.error(`verify_claude_user_input_ui failed: ${error?.stack || error}`);
  process.exit(1);
});
