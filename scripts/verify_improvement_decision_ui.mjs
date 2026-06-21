#!/usr/bin/env node
// 改进事项开发者决策型 UI 验收：当前待决策、来源反馈唯一入口、添加反馈三步确认。
// 默认起 Vite + mock API；设置 RUNTIME_UI_BASE/RUNTIME_API_BASE 时使用真实容器 UI/API。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";
import process from "node:process";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = new URL("..", import.meta.url).pathname;
const ts = "2026-06-21T00:00:00Z";
const REAL = Boolean(process.env.RUNTIME_UI_BASE);
const port = Number(process.env.IMPROVEMENT_DECISION_PORT || 55208);
const uiBase = (process.env.RUNTIME_UI_BASE || `http://127.0.0.1:${port}`).replace(/\/$/, "");
const apiBase = (process.env.RUNTIME_API_BASE || "http://runtime.test").replace(/\/$/, "");
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || "/tmp/agent-gov-ui-verify";

function dockerEnvValue(name) {
  try {
    const content = readFileSync(new URL("../docker/.env", import.meta.url), "utf8");
    for (const rawLine of content.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      const index = line.indexOf("=");
      if (index > 0 && line.slice(0, index).trim() === name) return line.slice(index + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  } catch { /* docker/.env may be absent outside this repo */ }
  return "";
}

const apiKey = process.env.RUNTIME_API_KEY || dockerEnvValue("FRONTEND_RUNTIME_API_KEY") || dockerEnvValue("API_KEY") || "";

function json(route, payload, status = 200) {
  return route.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(payload) });
}

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
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(uiBase);
      if (response.ok) return;
    } catch { await new Promise((resolve) => setTimeout(resolve, 250)); }
  }
  throw new Error(`Vite did not become ready at ${uiBase}`);
}

function authHeaders(extra = {}) {
  return { Accept: "application/json", ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}), ...extra };
}

async function apiJson(path, init = {}) {
  const response = await fetch(`${apiBase}${path}`, { ...init, headers: authHeaders(init.headers || {}) });
  if (!response.ok) throw new Error(`${init.method || "GET"} ${path} failed: ${response.status} ${await response.text().catch(() => "")}`);
  return response.json();
}

async function postJson(path, body) {
  return apiJson(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function putJson(path, body) {
  return apiJson(path, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function seedRealData() {
  const agents = await apiJson("/api/agent-registry").catch(() => []);
  const agentId = agents.find((agent) => agent.status === "active")?.agent_id || agents[0]?.agent_id || "main-agent";
  const stamp = `decision-v27-${Date.now().toString(36)}`;
  const item = await postJson("/api/improvements", {
    agent_id: agentId,
    title: `${stamp} sec-ops-data 时间窗口误判治理`,
    summary: "sec-ops-data 时间窗口不一致导致同类告警误判。",
    source_feedback_refs: [`${stamp}-fb-1`],
    auto_merge: false,
  });
  await postJson(`/api/improvements/${item.improvement_id}/feedbacks`, {
    summary: "这个告警其实是误报",
    source: "playground_run",
    raw_text: "事件时间和告警时间窗口不一致。",
    run_id: `${stamp}-run-1`,
    session_id: `${stamp}-session-1`,
    agent_version_id: `${stamp}-agent-version`,
    scenario: "alert-triage",
    task_id: `${stamp}-task-1`,
    alert_id: `${stamp}-alert`,
    case_id: `${stamp}-case`,
  });
  await putJson(`/api/improvements/${item.improvement_id}/normalized-feedback`, {
    problem: "告警误判",
    possible_reason: "事件时间与告警时间窗口不一致",
    possible_object: "sec-ops-data MCP 数据",
    impact: "中",
    suggestion: "进入归因分析",
    user_quote: "这个告警其实是误报。",
  });
  await putJson(`/api/improvements/${item.improvement_id}/attribution`, {
    summary: "sec-ops-data MCP 返回的数据时间与告警时间窗口不一致。",
    responsibility_boundary: ["主要是外部数据时间窗口问题"],
    evidence: ["来源反馈指向同一时间窗口误判问题"],
  });
  return { id: item.improvement_id, title: item.title };
}

function mockState() {
  const target = { improvement_id: "imp-decision01", agent_id: "soc-ops", title: "sec-ops-data 时间窗口误判治理", summary: "sec-ops-data 时间窗口不一致导致同类告警误判。", source_feedback_refs: ["fb-1"], improvement_stage: "attribution", improvement_status: "active", created_at: ts, updated_at: ts };
  return {
    target,
    agents: [{ agent_id: "soc-ops", name: "安全运营助手", category: "business", workspace_dir: "/w/soc", created_at: ts, status: "active" }],
    improvements: [target],
    feedbacks: {
      [target.improvement_id]: [{ feedback_id: "fb-1", improvement_id: target.improvement_id, agent_id: "soc-ops", summary: "这个告警其实是误报", source: "playground_run", status: "merged", raw_text: "事件时间和告警时间窗口不一致。", run_id: "run-1", session_id: "sess-1", agent_version_id: "v1", scenario: "alert-triage", task_id: "task-1", alert_id: "alert-1", case_id: "case-1", created_at: ts }],
    },
    feedbackSeq: 1,
  };
}

async function installMockRoutes(page, state) {
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === uiBase) return route.continue();
    if (url.hostname !== "runtime.test") return route.continue();
    const method = req.method();
    const path = url.pathname;
    if (path === "/health") return json(route, { status: "ok", model: "decision-mock" });
    if (path === "/api/agent-registry") return json(route, state.agents);
    if (path === "/api/improvements" && method === "GET") return json(route, state.improvements);
    const feedbacks = path.match(/^\/api\/improvements\/([^/]+)\/feedbacks$/);
    if (feedbacks && method === "GET") return json(route, state.feedbacks[decodeURIComponent(feedbacks[1])] || []);
    if (feedbacks && method === "POST") {
      const id = decodeURIComponent(feedbacks[1]);
      const item = state.improvements.find((row) => row.improvement_id === id);
      const body = req.postDataJSON();
      if (!item) return json(route, { detail: "not found" }, 404);
      const row = { ...body, feedback_id: `fb-${++state.feedbackSeq}`, improvement_id: id, agent_id: item.agent_id, status: "merged", created_at: ts };
      state.feedbacks[id] = [...(state.feedbacks[id] || []), row];
      item.source_feedback_refs = [...(item.source_feedback_refs || []), row.feedback_id];
      return json(route, row, 201);
    }
    if (/^\/api\/improvements\/[^/]+\/normalized-feedback$/.test(path)) return json(route, { normalized_feedback_id: "nf-1", improvement_id: state.target.improvement_id, problem: "告警误判", possible_reason: "事件时间与告警时间窗口不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "进入归因分析", user_quote: "这个告警其实是误报。", status: "draft", created_at: ts, updated_at: ts });
    if (/^\/api\/improvements\/[^/]+\/attribution$/.test(path)) return json(route, { attribution_id: "attr-1", improvement_id: state.target.improvement_id, summary: "sec-ops-data MCP 返回的数据时间与告警时间窗口不一致。", responsibility_boundary: ["主要是外部数据时间窗口问题"], evidence: ["来源反馈指向同一时间窗口误判问题"], status: "draft", created_at: ts, updated_at: ts });
    if (/^\/api\/improvements\/[^/]+\/optimization-plan$/.test(path)) return json(route, { optimization_plan_id: "opt-1", improvement_id: state.target.improvement_id, summary: "补充时间窗口核验", changes: [], status: "draft", created_at: ts, updated_at: ts });
    if (/^\/api\/improvements\/[^/]+\/execution$/.test(path)) return json(route, { execution_id: "exec-1", improvement_id: state.target.improvement_id, summary: "尚未执行", changes_applied: [], agent_version: "", status: "draft", created_at: ts, updated_at: ts });
    if (/^\/api\/improvements\/[^/]+\/similar$/.test(path) || /^\/api\/improvements\/[^/]+\/links$/.test(path) || path === "/api/assets") return json(route, []);
    if (/^\/api\/automation-policy/.test(path)) return json(route, { agent_id: "soc-ops", mode: "off" });
    if (/^\/api\/improvements\/[^/]+$/.test(path)) return json(route, state.target);
    if (["/api/agents", "/api/skills", "/api/sessions", "/api/agent-releases", "/api/agent-change-sets"].includes(path)) return json(route, []);
    if (path === "/api/config") return json(route, { mappings: [] });
    if (path === "/api/agent-repository") return json(route, { status: "active", dirty: false, changed_files: [], file_diffs: [] });
    if (path === "/api/agent-repository/current") return json(route, { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" });
    return json(route, {});
  });
}

async function assertVisible(page, testId) {
  const locator = page.getByTestId(testId);
  await locator.waitFor({ timeout: 8000 });
  if (!(await locator.first().isVisible())) throw new Error(`${testId} is not visible`);
  return locator;
}

async function main() {
  const target = REAL ? await seedRealData() : mockState().target;
  const state = REAL ? null : mockState();
  const server = REAL ? null : startVite();
  try {
    if (!REAL) await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.addInitScript(({ apiBaseValue, apiKeyValue }) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: apiBaseValue, apiKey: apiKeyValue }));
    }, { apiBaseValue: apiBase, apiKeyValue: apiKey });
    if (state) await installMockRoutes(page, state);

    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("nav-improvement").click();
    await assertVisible(page, "improvement-workbench");
    const item = page.getByTestId("improvement-list-item").filter({ hasText: target.title }).first();
    await item.waitFor({ timeout: 10_000 });
    await item.click();
    await assertVisible(page, "improvement-detail");

    await assertVisible(page, "improvement-list-decision");
    await assertVisible(page, "current-decision-card");
    await assertVisible(page, "current-decision-question");
    await assertVisible(page, "decision-basis");
    await assertVisible(page, "decision-consequence");
    await assertVisible(page, "improvement-provenance");
    const primaryCount = await page.getByTestId("current-decision-card").getByTestId("primary-action").count();
    if (primaryCount !== 1) throw new Error(`current-decision-card primary action count=${primaryCount}`);

    mkdirSync(screenshotDir, { recursive: true });
    await page.screenshot({ path: `${screenshotDir}/${REAL ? "real" : "mock"}-improvement-decision.png`, fullPage: true });

    const hiddenTable = await page.getByTestId("source-feedback-table").isVisible().catch(() => false);
    if (hiddenTable) throw new Error("source feedback table should be hidden before 查看全部反馈");
    await page.getByTestId("view-all-feedbacks").click();
    await assertVisible(page, "source-feedback-table");
    const rowsBefore = await page.getByTestId("source-feedback-row").count();
    if (rowsBefore < 1) throw new Error(`source feedback rows before add=${rowsBefore}`);

    await page.getByTestId("add-feedback-to-improvement").click();
    await assertVisible(page, "add-feedback-select-step");
    await page.getByTestId("add-feedback-summary").fill("新增反馈：时间窗口仍然误判");
    await page.getByTestId("add-feedback-raw-text").fill("新的运行仍然没有核对事件时间和告警时间窗口。");
    await page.getByTestId("add-feedback-next-detail").click();
    await assertVisible(page, "add-feedback-review-step");
    await page.getByTestId("add-feedback-next-confirm").click();
    await assertVisible(page, "add-feedback-confirm-step");
    await assertVisible(page, "add-feedback-consequence");
    await page.screenshot({ path: `${screenshotDir}/${REAL ? "real" : "mock"}-add-feedback-confirm.png`, fullPage: true });
    await page.getByTestId("add-feedback-confirm-submit").click();
    await page.getByTestId("add-feedback-flow").waitFor({ state: "detached", timeout: 10_000 });
    await assertVisible(page, "source-feedback-table");
    const rowsAfter = await page.getByTestId("source-feedback-row").count();
    if (rowsAfter <= rowsBefore) throw new Error(`source feedback rows did not increase: ${rowsBefore} -> ${rowsAfter}`);

    await browser.close();
    console.log(JSON.stringify({ mode: REAL ? "real-container" : "mock", ui_base: uiBase, api_base: apiBase, improvement_id: target.id || target.improvement_id, rows_before: rowsBefore, rows_after: rowsAfter, screenshots: screenshotDir }, null, 2));
  } finally {
    await stopChild(server);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
