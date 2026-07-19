#!/usr/bin/env node
// 四阶段改进治理 §3 助手回复动作验收。
// 默认模式：自启动 Vite + mock SSE，进入 main-flow 硬门，验证回复动作结构不回归。
// 真实模式：设置 RUNTIME_UI_BASE + RUNTIME_API_BASE 后连真实容器 UI/API，跑真实 LLM 对话。
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { scrollNavigationMetrics } from "./playground_scroll_test_helpers.mjs";

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
const RETRIES = Number(process.env.RETRIES || 1);
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || mkdtempSync(join(tmpdir(), "agentgov-message-actions-"));

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

function mockAgentRuns(includeMessages) {
  return Array.from({ length: 14 }, (_, index) => {
    const n = index + 1;
    const createdAt = new Date(Date.parse(ts) + index * 2000).toISOString();
    const completedAt = new Date(Date.parse(ts) + index * 2000 + 1000).toISOString();
    const userPrompt = n === 1
      ? [
        "**Workspace pytest** 是否应该随业务 Agent 版本管理？",
        "",
        "- 测试资产口径: Workspace `tests/` + pytest",
        "- 链接依据: [OKF](https://example.com/okf)",
      ].join("\n")
      : `用一句话说明你的角色，序号 ${n}。`;
    const answer = n === 1
      ? [
        "### 结论",
        "Workspace pytest 应与业务 Agent **同版本治理**，并进入发布前审计。",
        "",
        "| 字段 | 作用 |",
        "| --- | --- |",
        "| `dataset_id` | 稳定标识 |",
        "| `owner` | 责任人 |",
        "",
        "```json",
        "{\"dataset_id\":\"eval-smoke\"}",
        "```",
        "",
        "- 支持回归复用",
        "- 支持发布前审计",
        "",
        "[查看治理入口](https://example.com/governance)",
      ].join("\n")
      : `我是 AgentGov 测试助手。第 ${n} 段回复用于构造可滚动的 Playground 历史，验证自动置底、一键置底和消息滚动导航。`.repeat(2);
    return {
      run_id: `mock-run-${n}`,
      session_id: "mock-session",
      sdk_session_id: "mock-session",
      agent_version_id: "v-mock",
      message: userPrompt,
      answer: includeMessages ? answer : undefined,
      answer_summary: answer,
      messages: includeMessages ? [
        { event: "AssistantMessage", content: [{ text: answer }] },
        { event: "ToolUse", name: "Read", input: { file_path: "CLAUDE.md" } },
        { event: "ResultMessage", content: [{ text: `trace ${n}` }] },
      ] : undefined,
      agent_activity: { tool_calls: [], tool_results: [], tool_names: [] },
      created_at: createdAt,
      completed_at: completedAt,
    };
  });
}

function mockConversationItems() {
  return mockAgentRuns(false).flatMap((run, index) => [
    {
      id: `msg_${index * 2}`,
      object: "conversation.item",
      type: "message",
      role: "user",
      content: [{ type: "text", text: run.message }],
      parent_tool_use_id: null,
    },
    {
      id: `msg_${index * 2 + 1}`,
      object: "conversation.item",
      type: "message",
      role: "assistant",
      content: [
        { type: "text", text: run.answer_summary },
        { type: "tool_use", id: `tool-${index + 1}`, name: "Read", input: { file_path: "CLAUDE.md" } },
      ],
      parent_tool_use_id: null,
    },
  ]);
}

function mockPayload(urlOrPath) {
  const url = typeof urlOrPath === "string" ? null : urlOrPath;
  const path = typeof urlOrPath === "string" ? urlOrPath : urlOrPath.pathname;
  if (path === "/health") return { status: "ok", model: "parity-mock", provider_key_configured: true };
  if (path === "/v1/conversations" || path === "/api/sessions") {
    return path === "/v1/conversations"
      ? {
          data: [{
            id: "conv_mock-session",
            created_at: Date.parse(ts) / 1000,
            title: "用一句话说明你的角色。",
            metadata: { client: "agent-gov-ui" },
            agentgov: {
              sdk_session_id: "mock-session",
              agent_id: "security-operations-expert",
              updated_at: Date.parse("2026-06-18T00:00:30Z") / 1000,
              turns: 14,
              active_run_id: "mock-active-run",
              active_run_expires_at: "2099-01-01T00:00:00Z",
            },
          }],
        }
      : [{
          session_id: "mock-session",
          sdk_session_id: "mock-session",
          created_at: ts,
          updated_at: "2026-06-18T00:00:30Z",
          title: "用一句话说明你的角色。",
          turns: 14,
          metadata: { client: "agent-gov-ui" },
        }];
  }
  if (path === "/api/agent-runs") {
    const includeMessages = url?.searchParams.get("include_messages") === "true";
    return mockAgentRuns(includeMessages);
  }
  if (/^\/v1\/conversations\/[^/]+\/items$/.test(path)) {
    const allItems = mockConversationItems();
    const after = url?.searchParams.get("after");
    const items = after ? allItems.slice(14) : allItems.slice(0, 14);
    return {
      object: "list",
      data: items,
      first_id: items[0]?.id || null,
      last_id: items.at(-1)?.id || null,
      has_more: !after,
    };
  }
  if (path === "/api/agents" || path === "/api/skills" || path === "/api/agent-change-sets" || path === "/api/agent-releases") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-registry") return [{ agent_id: "security-operations-expert", name: "Security Operations Expert", category: "business", workspace_dir: "/data/business-agents/security-operations-expert/workspace", created_at: ts, status: "active", builtin: true, default: true, protected: true, requires_web_hitl: true }];
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v-mock", commit_sha: "mock", created_at: ts, reason: "current" };
  return {};
}

function sessionIdFromResponsesBody(body) {
  return typeof body.conversation === "string" && body.conversation.startsWith("conv_")
    ? body.conversation.slice("conv_".length)
    : "mock-session";
}

async function scrollDistance(page) {
  return page.getByTestId("playground-messages").evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
}

async function waitNearBottom(page) {
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="playground-messages"]');
    return !!el && el.scrollHeight > el.clientHeight && el.scrollHeight - el.clientHeight - el.scrollTop <= 24;
  }, null, { timeout: 5000 });
}

async function waitPreviewOpen(page) {
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="playground-scroll-preview"]');
    return !!el && Number(getComputedStyle(el).opacity) > 0.9;
  }, null, { timeout: 5000 });
}

async function mockMarkdownChecks(page) {
  const userMarkdown = page.locator('[data-message-id="history_msg_0_user"]').getByTestId("message-markdown");
  const assistantMarkdown = page.locator('[data-message-id="history_msg_0_assistant"]').getByTestId("message-markdown");
  const userLink = userMarkdown.locator("a").filter({ hasText: "OKF" });
  const assistantLink = assistantMarkdown.locator("a").filter({ hasText: "查看治理入口" });
  const userText = await userMarkdown.innerText();
  const assistantText = await assistantMarkdown.innerText();
  return {
    markdownContainerCount: await page.getByTestId("message-markdown").count(),
    userStrong: await userMarkdown.locator("strong").filter({ hasText: "Workspace pytest" }).count(),
    userListItems: await userMarkdown.locator("li").count(),
    userInlineCode: await userMarkdown.locator("code").filter({ hasText: "tests/" }).count(),
    userLinkTarget: await userLink.first().getAttribute("target").catch(() => ""),
    userRawMarkersHidden: !userText.includes("**") && !userText.includes("[OKF]"),
    assistantHeading: await assistantMarkdown.locator("h3").filter({ hasText: "结论" }).count(),
    assistantStrong: await assistantMarkdown.locator("strong").count(),
    assistantTableRows: await assistantMarkdown.locator("table tr").count(),
    assistantCodeBlock: await assistantMarkdown.locator("pre code").filter({ hasText: "dataset_id" }).count(),
    assistantListItems: await assistantMarkdown.locator("li").count(),
    assistantLinkTarget: await assistantLink.first().getAttribute("target").catch(() => ""),
    assistantRawMarkersHidden: !assistantText.includes("###") && !assistantText.includes("| --- |") && !assistantText.includes("```"),
  };
}

async function main() {
  const server = REAL ? null : startVite();
  if (!REAL) await waitForVite();
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const requestedRuntimeUrls = [];
  await page.addInitScript(([a, k, real]) => {
    window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: a, apiKey: k }));
    if (!real) {
      window.localStorage.setItem("playground-active-session", JSON.stringify("mock-session"));
      window.localStorage.removeItem("playground-session-messages");
    }
  }, [api, key, REAL]);
  let ok = false, detail = "";
  let responsesRequestCount = 0;
  try {
    if (!REAL) {
      await page.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        if (url.hostname !== "runtime.test") return route.continue();
        requestedRuntimeUrls.push(`${url.pathname}${url.search}`);
        if (url.pathname === "/v1/responses") {
          responsesRequestCount += 1;
          const body = route.request().postDataJSON();
          const sessionId = sessionIdFromResponsesBody(body);
          if (body?.input === "触发截断流负测") {
            return sse(route, [
              { event: "agentgov.session", data: { session_id: sessionId } },
              { event: "response.output_text.delta", data: { delta: "半截响应" } },
              { event: "agentgov.done", data: { ok: true } },
            ]);
          }
          return sse(route, [
            { event: "agentgov.session", data: { session_id: sessionId } },
            { event: "response.output_text.delta", data: { delta: "我是 AgentGov 测试助手。" } },
            { event: "agentgov.result", data: { run_id: "mock-run", session_id: sessionId, agent_version_id: "v-mock", agent_activity: { tool_calls: [], tool_results: [], tool_names: [] } } },
            { event: "agentgov.prompt_suggestion", data: { v: 1, type: "agentgov.prompt_suggestion", run_id: "mock-run", ts: Date.now() / 1000, seq: 4, payload: { suggestion: "继续检查失败路径。", suggestions: ["继续检查失败路径。", "看一下日志", "换个角度分析"], session_id: sessionId } } },
            { event: "response.completed", data: { response: { status: "completed" } } },
            { event: "agentgov.done", data: { ok: true } },
          ]);
        }
        return json(route, mockPayload(url));
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
        let markdownChecks = { skipped: REAL };
        if (!REAL) {
          markdownChecks = { skipped: false, ...await mockMarkdownChecks(page) };
        }
        let scrollChecks = { skipped: REAL };
        if (!REAL) {
          await page.getByTestId("playground-scroll-navigator").waitFor({ timeout: 8000 });
          await waitNearBottom(page);
          const initialDistance = await scrollDistance(page);
          await page.getByTestId("playground-messages").evaluate((el) => {
            el.scrollTop = 0;
            el.dispatchEvent(new Event("scroll", { bubbles: true }));
          });
          await page.getByTestId("playground-jump-to-bottom").waitFor({ timeout: 5000 });
          const jumpVisibleAfterUp = await page.getByTestId("playground-jump-to-bottom").isVisible().catch(() => false);
          await page.getByTestId("playground-scroll-rail").hover();
          await waitPreviewOpen(page);
          const previewItemCount = await page.getByTestId("playground-scroll-preview-item").count();
          const previewRoles = await page.getByTestId("playground-scroll-preview-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
          const markCount = await page.getByTestId("playground-scroll-mark").count();
          const markRoles = await page.getByTestId("playground-scroll-mark").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
          const navigationMetrics = await scrollNavigationMetrics(page);
          await page.getByTestId("playground-scroll-preview-item").first().click();
          await page.waitForFunction(() => {
            const el = document.querySelector('[data-testid="playground-messages"]');
            return !!el && el.scrollTop <= 80;
          }, null, { timeout: 5000 });
          const nearTopAfterPreviewClick = await page.getByTestId("playground-messages").evaluate((el) => el.scrollTop <= 80);
          await page.getByTestId("playground-jump-to-bottom").click();
          await waitNearBottom(page);
          scrollChecks = {
            skipped: false,
            initialDistance,
            jumpVisibleAfterUp,
            previewItemCount,
            previewRoles,
            markCount,
            markRoles,
            navigationMetrics,
            nearTopAfterPreviewClick,
            distanceAfterJump: await scrollDistance(page),
          };
        }
        await page.getByTestId("message-action-view-trace").first().click();
        await page.getByTestId("playground-evidence-panel").waitFor({ timeout: 8000 });
        const traceBox = await page.getByTestId("playground-evidence-panel").boundingBox();
        const resizeHandle = page.getByTestId("evidence-panel-resize-handle");
        const resizeBox = await resizeHandle.boundingBox();
        if (resizeBox) {
          await page.mouse.move(resizeBox.x + resizeBox.width / 2, resizeBox.y + 36);
          await page.mouse.down();
          await page.mouse.move(resizeBox.x - 110, resizeBox.y + 36, { steps: 8 });
          await page.mouse.up();
        }
        const resizedTraceBox = await page.getByTestId("playground-evidence-panel").boundingBox();
        const resizeAria = await resizeHandle.getAttribute("aria-valuenow");
        const traceTabCount = await page.locator(".evidence-tab").count();
        const traceTabVisible = await page.getByTestId("evidence-tab-trace").isVisible().catch(() => false);
        const traceDrawerCount = await page.getByTestId("trace-drawer").count();
        const legacyModalVisible = await page.locator(".detail-modal-card").isVisible().catch(() => false);
        await page.getByTestId("playground-evidence-panel").getByLabel("折叠运行证据栏").click();
        await page.getByTestId("playground-evidence-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});

        await page.getByTestId("message-action-create-feedback").first().click();
        await page.getByTestId("feedback-drawer").waitFor({ timeout: 8000 });
        const feedbackSize = await page.getByTestId("feedback-drawer").getAttribute("data-size");
        const feedbackBox = await page.getByTestId("feedback-drawer").boundingBox();
        await page.getByTestId("feedback-drawer").getByLabel("关闭").click();
        await page.getByTestId("feedback-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});

        await page.getByTestId("playground-session-trigger").click();
        await page.getByTestId("playground-session-sidebar").waitFor({ timeout: 8000 });
        const sessionBox = await page.getByTestId("playground-session-sidebar").boundingBox();
        const sessionText = await page.getByTestId("playground-session-sidebar").innerText();
        const activeSessionDeleteDisabled = REAL
          ? true
          : await page.getByTestId("session-sidebar-delete").first().isDisabled();
        await page.getByTestId("playground-session-trigger").click();
        await page.getByTestId("playground-session-sidebar").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});

        await page.getByTestId("playground-runtime-settings-trigger").click();
        await page.getByTestId("playground-runtime-settings-drawer").waitFor({ timeout: 8000 });
        const settingsSize = await page.getByTestId("playground-runtime-settings-drawer").getAttribute("data-size");
        const settingsBox = await page.getByTestId("playground-runtime-settings-drawer").boundingBox();
        const settingsText = await page.getByTestId("playground-runtime-settings-drawer").innerText();
        const debugClosed = await page.getByTestId("runtime-debug-section").evaluate((el) => !el.open).catch(() => false);
        await page.getByTestId("playground-runtime-settings-drawer").getByLabel("关闭").click();
        await page.getByTestId("playground-runtime-settings-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});

        let autoPanelChecks = { skipped: true };
        if (!REAL) {
          await page.getByTestId("playground-session-trigger").click();
          await page.getByTestId("playground-session-sidebar").waitFor({ timeout: 8000 });
          await page.getByTestId("playground-messages").evaluate((el) => {
            el.scrollTop = 0;
            el.dispatchEvent(new Event("scroll", { bubbles: true }));
          });
          const rowsBeforeSend = await page.locator("[data-message-id]").count();
          await page.locator(".composer textarea").fill("请再用一句话说明你的角色。");
          await page.getByRole("button", { name: "发送" }).click();
          await page.getByTestId("playground-evidence-panel").waitFor({ timeout: 8000 });
          await page.waitForFunction((count) => document.querySelectorAll("[data-message-id]").length > count, rowsBeforeSend, { timeout: 90000 });
          await waitNearBottom(page);
          const suggestion = page.getByTestId("prompt-suggestion");
          await suggestion.waitFor({ timeout: 8000 });
          const requestsBeforeSuggestionClick = responsesRequestCount;
          const suggestionChips = suggestion.getByTestId("prompt-suggestion-item");
          // mock 一帧送 3 条 ⇒ 必须渲染 3 个 chip。不测这条的话,多候选退化回单条也照样绿
          // (下面的 first() 点击对单条同样成立)。
          const suggestionChipCount = await suggestionChips.count();
          const suggestionChipTexts = await suggestionChips.allInnerTexts();
          // 多候选下容器内有多个 button,必须按 per-chip testid 取,否则 strict-mode violation
          await suggestionChips.first().click();
          await page.waitForTimeout(100);
          autoPanelChecks = {
            skipped: false,
            sessionCollapsedAfterSend: await page.getByTestId("playground-session-sidebar").count() === 0,
            evidenceOpenAfterSend: await page.getByTestId("playground-evidence-panel").isVisible().catch(() => false),
            traceTabAfterSend: await page.getByTestId("evidence-tab-trace").isVisible().catch(() => false),
            autoBottomAfterSend: await scrollDistance(page) <= 24,
            suggestionRenderedAllCandidates: suggestionChipCount === 3,
            suggestionChipTextsMatchFrame:
              suggestionChipTexts.join("|") === "继续检查失败路径。|看一下日志|换个角度分析",
            suggestionFilledInput: await page.getByTestId("chat-composer-input").inputValue() === "继续检查失败路径。",
            suggestionDidNotAutoSend: responsesRequestCount === requestsBeforeSuggestionClick,
            suggestionClearedAfterUse: await page.getByTestId("prompt-suggestion").count() === 0,
          };
          await page.getByTestId("playground-evidence-panel").getByLabel("折叠运行证据栏").click();
          await page.getByTestId("playground-evidence-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
        }

        const drawerChecks = {
          traceWidth: Math.round(traceBox?.width || 0),
          resizedTraceWidth: Math.round(resizedTraceBox?.width || 0),
          resizeAria: Number(resizeAria || 0),
          traceTabCount,
          traceTabVisible,
          traceDrawerCount,
          feedbackSize,
          feedbackWidth: Math.round(feedbackBox?.width || 0),
          sessionWidth: Math.round(sessionBox?.width || 0),
          settingsSize,
          settingsWidth: Math.round(settingsBox?.width || 0),
          legacyModalVisible,
          sessionNoRuntimeSettings: !sessionText.includes("Subagent") && !sessionText.includes("Skills Mode") && !sessionText.includes("Allowed Tools"),
          activeSessionDeleteDisabled,
          settingsNoSessionHistory: !settingsText.includes("新会话") && !settingsText.includes("删除会话映射") && !settingsText.includes("Sessions"),
          debugClosed,
          markdownChecks,
          scrollChecks,
          autoPanelChecks,
          historySourceChecks: {
            conversationItemsRequested: requestedRuntimeUrls.some((value) => value.startsWith("/v1/conversations/conv_mock-session/items?")),
            conversationItemsPaginated: requestedRuntimeUrls.some((value) => value.startsWith("/v1/conversations/conv_mock-session/items?") && value.includes("after=msg_13")),
            sqliteMessageRestoreAbsent: !requestedRuntimeUrls.some((value) => value.startsWith("/api/agent-runs?") && value.includes("include_messages=true")),
            localMessageCacheAbsent: await page.evaluate(() => window.localStorage.getItem("playground-session-messages") === null),
          },
        };
        ok = Object.values(counts).every((c) => c > 0)
          && (traceBox?.width || 0) >= 520
          && (traceBox?.width || 0) <= 590
          && (resizedTraceBox?.width || 0) >= (traceBox?.width || 0) + 80
          && (resizedTraceBox?.width || 0) <= 680
          && Number(resizeAria || 0) === Math.round(resizedTraceBox?.width || 0)
          && traceTabCount === 1
          && traceTabVisible
          && traceDrawerCount === 0
          && feedbackSize === "narrow"
          && (feedbackBox?.width || 0) >= 430
          && (sessionBox?.width || 0) >= 260
          && (sessionBox?.width || 0) <= 340
          && settingsSize === "wide"
          && (settingsBox?.width || 0) >= 860
          && drawerChecks.sessionNoRuntimeSettings
          && drawerChecks.activeSessionDeleteDisabled
          && drawerChecks.settingsNoSessionHistory
          && debugClosed
          && !legacyModalVisible
          && (REAL || (
            drawerChecks.historySourceChecks.conversationItemsRequested
            && drawerChecks.historySourceChecks.conversationItemsPaginated
            && drawerChecks.historySourceChecks.sqliteMessageRestoreAbsent
            && drawerChecks.historySourceChecks.localMessageCacheAbsent
          ))
          && (REAL || (
            !markdownChecks.skipped
            && markdownChecks.markdownContainerCount >= 28
            && markdownChecks.userStrong > 0
            && markdownChecks.userListItems >= 2
            && markdownChecks.userInlineCode > 0
            && markdownChecks.userLinkTarget === "_blank"
            && markdownChecks.userRawMarkersHidden
            && markdownChecks.assistantHeading > 0
            && markdownChecks.assistantStrong > 0
            && markdownChecks.assistantTableRows >= 3
            && markdownChecks.assistantCodeBlock > 0
            && markdownChecks.assistantListItems >= 2
            && markdownChecks.assistantLinkTarget === "_blank"
            && markdownChecks.assistantRawMarkersHidden
          ))
          && (REAL || (
            !scrollChecks.skipped
            && scrollChecks.initialDistance <= 24
            && scrollChecks.jumpVisibleAfterUp
            && scrollChecks.previewItemCount === 14
            && scrollChecks.previewRoles.every((role) => role === "user")
            && scrollChecks.markCount === 14
            && scrollChecks.markRoles.every((role) => role === "user")
            && scrollChecks.navigationMetrics.railHeight >= 330
            && scrollChecks.navigationMetrics.railHeight <= 360
            && scrollChecks.navigationMetrics.avgGap >= 22
            && scrollChecks.navigationMetrics.avgGap <= 30
            && scrollChecks.nearTopAfterPreviewClick
            && scrollChecks.distanceAfterJump <= 24
          ))
          && (REAL || (
            !autoPanelChecks.skipped
            && autoPanelChecks.sessionCollapsedAfterSend
            && autoPanelChecks.evidenceOpenAfterSend
            && autoPanelChecks.traceTabAfterSend
            && autoPanelChecks.autoBottomAfterSend
            && autoPanelChecks.suggestionRenderedAllCandidates
            && autoPanelChecks.suggestionChipTextsMatchFrame
            && autoPanelChecks.suggestionFilledInput
            && autoPanelChecks.suggestionDidNotAutoSend
            && autoPanelChecks.suggestionClearedAfterUse
          ));
        detail = JSON.stringify({ counts, drawerChecks });
        if (ok) await page.screenshot({ path: join(screenshotDir, "agentgov-improvement-ui-after-message-actions.png") });
        if (ok && !REAL) {
          await page.locator(".composer textarea").fill("触发截断流负测");
          await page.getByRole("button", { name: "发送" }).click();
          await page.waitForFunction(() => {
            const messages = document.querySelectorAll('[data-message-role="assistant"]');
            return messages[messages.length - 1]?.textContent?.includes("Stream ended before terminal event");
          }, undefined, { timeout: 8000 });
          const terminalFailureText = await page.locator('[data-message-role="assistant"]').last().innerText();
          const terminalFailureCheck = {
            partialTextPreserved: terminalFailureText.includes("半截响应"),
            interruptionVisible: terminalFailureText.includes("运行失败")
              && terminalFailureText.includes("Stream ended before terminal event"),
          };
          ok = ok && terminalFailureCheck.partialTextPreserved && terminalFailureCheck.interruptionVisible;
          detail = JSON.stringify({ counts, drawerChecks, terminalFailureCheck });
        }
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
