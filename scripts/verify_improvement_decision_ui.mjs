#!/usr/bin/env node
// 改进事项开发者决策型 UI 验收：当前待决策、来源反馈唯一入口、添加反馈三步确认。
// 默认起 Vite + mock API；设置 RUNTIME_UI_BASE/RUNTIME_API_BASE 时使用真实容器 UI/API。
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { createRequire } from "node:module";
import { join } from "node:path";
import { mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import process from "node:process";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = new URL("..", import.meta.url).pathname;
const ts = "2026-06-21T00:00:00Z";
const REAL = Boolean(process.env.RUNTIME_UI_BASE);
const port = Number(process.env.IMPROVEMENT_DECISION_PORT || 55208);
const uiBase = (process.env.RUNTIME_UI_BASE || `http://127.0.0.1:${port}`).replace(/\/$/, "");
const apiBase = (process.env.RUNTIME_API_BASE || "http://runtime.test").replace(/\/$/, "");
const screenshotDir = process.env.VERIFY_SCREENSHOT_DIR || mkdtempSync(join(tmpdir(), "agent-gov-ui-verify-"));

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

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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
  const stamp = `decision-improvement-${Date.now().toString(36)}`;
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
  await postJson(`/api/improvements/${item.improvement_id}/lifecycle`, { stage: "triage" }).catch(() => null);
  return { id: item.improvement_id, title: item.title };
}

function mockState() {
  const target = { improvement_id: "imp-decision01", agent_id: "soc-ops", title: "sec-ops-data 时间窗口误判治理", summary: "sec-ops-data 时间窗口不一致导致同类告警误判。", source_feedback_refs: ["fb-1"], improvement_stage: "triage", improvement_status: "active", created_at: ts, updated_at: ts };
  return {
    target,
    agents: [{ agent_id: "soc-ops", name: "安全运营助手", category: "business", workspace_dir: "/w/soc", created_at: ts, status: "active" }],
    improvements: [target],
    feedbacks: {
      [target.improvement_id]: [{ feedback_id: "fb-1", improvement_id: target.improvement_id, agent_id: "soc-ops", summary: "这个告警其实是误报", source: "playground_run", status: "merged", raw_text: "事件时间和告警时间窗口不一致。", run_id: "run-1", session_id: "sess-1", agent_version_id: "v1", scenario: "alert-triage", task_id: "task-1", alert_id: "alert-1", case_id: "case-1", created_at: ts }],
    },
    feedbackSeq: 1,
    attribution: null,
    optimizationPlan: null,
    optimizationGenerateCount: 0,
    execution: null,
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
    if (/^\/api\/improvements\/[^/]+\/normalized-feedback\/confirm$/.test(path)) return json(route, { normalized_feedback_id: "nf-1", improvement_id: state.target.improvement_id, problem: "告警误判", possible_reason: "事件时间与告警时间窗口不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "进入归因分析", user_quote: "这个告警其实是误报。", status: "confirmed", created_at: ts, updated_at: ts });
    if (/^\/api\/improvements\/[^/]+\/normalized-feedback$/.test(path)) return json(route, { normalized_feedback_id: "nf-1", improvement_id: state.target.improvement_id, problem: "告警误判", possible_reason: "事件时间与告警时间窗口不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "进入归因分析", user_quote: "这个告警其实是误报。", status: "draft", created_at: ts, updated_at: ts });
    if (/^\/api\/improvements\/[^/]+\/attribution\/generate$/.test(path)) {
      await delay(900);
      state.attribution = { attribution_id: "attr-1", improvement_id: state.target.improvement_id, summary: "sec-ops-data MCP 返回的数据时间与告警时间窗口不一致。", responsibility_boundary: ["主要是外部数据时间窗口问题"], evidence: ["来源反馈指向同一时间窗口误判问题"], status: "draft", generated_by: "governor", created_at: ts, updated_at: ts };
      return json(route, state.attribution);
    }
    if (/^\/api\/improvements\/[^/]+\/attribution\/confirm$/.test(path)) {
      state.attribution = {
        ...(state.attribution || { attribution_id: "attr-1", improvement_id: state.target.improvement_id, summary: "sec-ops-data MCP 返回的数据时间与告警时间窗口不一致。", responsibility_boundary: ["主要是外部数据时间窗口问题"], evidence: ["来源反馈指向同一时间窗口误判问题"], generated_by: "governor", created_at: ts }),
        status: "confirmed",
        updated_at: ts,
      };
      return json(route, state.attribution);
    }
    if (/^\/api\/improvements\/[^/]+\/attribution$/.test(path)) {
      return state.attribution ? json(route, state.attribution) : json(route, { detail: "not found" }, 404);
    }
    if (/^\/api\/improvements\/[^/]+\/optimization-plan\/generate$/.test(path)) {
      await delay(300);
      state.optimizationGenerateCount += 1;
      state.optimizationPlan = {
        optimization_plan_id: "opt-1",
        improvement_id: state.target.improvement_id,
        summary: `补充时间窗口核验（第 ${state.optimizationGenerateCount} 版）`,
        changes: [{ target: "prompt", change: `补充事件时间与告警时间一致性校验（第 ${state.optimizationGenerateCount} 版）` }],
        status: "draft",
        generated_by: "governor",
        created_at: ts,
        updated_at: ts,
      };
      return json(route, state.optimizationPlan);
    }
    if (/^\/api\/improvements\/[^/]+\/optimization-plan\/confirm$/.test(path)) {
      state.optimizationPlan = {
        ...(state.optimizationPlan || { optimization_plan_id: "opt-1", improvement_id: state.target.improvement_id, summary: "补充时间窗口核验", changes: [], generated_by: "governor", created_at: ts }),
        status: "confirmed",
        updated_at: ts,
      };
      return json(route, state.optimizationPlan);
    }
    if (/^\/api\/improvements\/[^/]+\/optimization-plan$/.test(path)) return state.optimizationPlan ? json(route, state.optimizationPlan) : json(route, { detail: "not found" }, 404);
    if (/^\/api\/improvements\/[^/]+\/execution\/apply$/.test(path)) {
      state.execution = { execution_id: "exec-1", improvement_id: state.target.improvement_id, summary: "已在隔离变更集执行优化", changes_applied: ["补充时间窗口校验"], agent_version: "ver-cand", status: "draft", generated_by: "governor", created_at: ts, updated_at: ts };
      return json(route, state.execution);
    }
    if (/^\/api\/improvements\/[^/]+\/execution$/.test(path)) return state.execution ? json(route, state.execution) : json(route, { detail: "not found" }, 404);
    if (/^\/api\/improvements\/[^/]+\/similar$/.test(path) || /^\/api\/improvements\/[^/]+\/links$/.test(path) || path === "/api/assets") return json(route, []);
    if (/^\/api\/automation-policy/.test(path)) return json(route, { agent_id: "soc-ops", mode: "off" });
    if (/^\/api\/improvements\/[^/]+$/.test(path)) return json(route, state.target);
    if (/^\/api\/improvements\/[^/]+\/lifecycle$/.test(path)) {
      const body = req.postDataJSON();
      if (body.stage) state.target.improvement_stage = body.stage;
      state.target.updated_at = ts;
      return json(route, state.target);
    }
    if (["/api/agents", "/api/skills", "/api/sessions", "/api/agent-releases", "/api/agent-change-sets"].includes(path)) return json(route, []);
    if (path === "/api/config") return json(route, { mappings: [] });
    if (path === "/api/agent-repository") return json(route, { status: "active", dirty: false, changed_files: [], file_diffs: [] });
    if (path === "/api/agent-repository/current") return json(route, { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" });
    return json(route, {});
  });
}

async function assertVisible(page, testId) {
  const locator = page.getByTestId(testId).first();
  await locator.waitFor({ timeout: 8000 });
  if (!(await locator.isVisible())) throw new Error(`${testId} is not visible`);
  return locator;
}

async function main() {
  const state = REAL ? null : mockState();
  const target = REAL ? await seedRealData() : state.target;
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
    const targetId = target.id || target.improvement_id;
    const detailMeta = await assertVisible(page, "improvement-detail-meta");
    await page.waitForFunction(
      () => document.querySelector('[data-testid="improvement-detail-meta"]')?.textContent?.includes("反馈 1 条 / Run 1 个"),
      null,
      { timeout: 10_000 },
    );
    const detailMetaText = await detailMeta.innerText();
    if (!detailMetaText.includes("业务 Agent：")) throw new Error(`missing business agent label in detail meta: ${detailMetaText}`);
    if (!detailMetaText.includes("反馈 1 条 / Run 1 个")) throw new Error(`unexpected feedback/run meta: ${detailMetaText}`);
    const renderedImprovementId = await page.getByTestId("improvement-id-value").innerText();
    if (renderedImprovementId !== targetId) throw new Error(`rendered improvement id mismatch: ${renderedImprovementId} !== ${targetId}`);
    await assertVisible(page, "copy-improvement-id");

    await assertVisible(page, "improvement-list-decision");
    await assertVisible(page, "current-decision-card");
    await assertVisible(page, "current-decision-question");
    await assertVisible(page, "decision-basis");
    await assertVisible(page, "decision-score");
    await assertVisible(page, "stage-panel-source-feedback");
    const primaryCount = await page.getByTestId("current-decision-card").getByTestId("primary-action").count();
    if (primaryCount !== 1) throw new Error(`current-decision-card primary action count=${primaryCount}`);

    mkdirSync(screenshotDir, { recursive: true });
    await page.screenshot({ path: join(screenshotDir, `${REAL ? "real" : "mock"}-improvement-decision.png`), fullPage: true });

    const hiddenTable = await page.getByTestId("source-feedback-table").isVisible().catch(() => false);
    if (hiddenTable) throw new Error("source feedback table should be hidden before opening source drawer");
    await page.getByTestId("view-all-feedbacks").click();
    await assertVisible(page, "source-management-drawer");
    await assertVisible(page, "source-merge-basis");
    await assertVisible(page, "source-feedback-table");
    await page.getByTestId("source-feedback-row").first().waitFor({ timeout: 10_000 });
    const rowsBefore = await page.getByTestId("source-feedback-row").count();
    if (rowsBefore < 1) throw new Error(`source feedback rows before add=${rowsBefore}`);

    await page.getByTestId("add-feedback-to-improvement").click();
    await assertVisible(page, "add-feedback-select-step");
    // Part B（132cbeb）：添加反馈默认进入「选择已有反馈」Tab；录入新反馈须先切到「录入新反馈」Tab，
    // 否则 add-feedback-summary 等录入字段不在 DOM。改这里的步骤/testid 须同步 ImprovementAddFeedbackFlow.tsx。
    await assertVisible(page, "add-feedback-existing");
    await page.getByTestId("add-feedback-mode-new").click();
    await assertVisible(page, "add-feedback-summary");
    await page.getByTestId("add-feedback-summary").fill("新增反馈：时间窗口仍然误判");
    await page.getByTestId("add-feedback-raw-text").fill("新的运行仍然没有核对事件时间和告警时间窗口。");
    await page.getByTestId("add-feedback-next-detail").click();
    await assertVisible(page, "add-feedback-review-step");
    await page.getByTestId("add-feedback-next-confirm").click();
    await assertVisible(page, "add-feedback-confirm-step");
    await assertVisible(page, "add-feedback-consequence");
    await page.screenshot({ path: join(screenshotDir, `${REAL ? "real" : "mock"}-add-feedback-confirm.png`), fullPage: true });
    await page.getByTestId("add-feedback-confirm-submit").click();
    await page.getByTestId("add-feedback-flow").waitFor({ state: "detached", timeout: 10_000 });
    await assertVisible(page, "source-management-drawer");
    await assertVisible(page, "source-feedback-table");
    const rowsAfter = await page.getByTestId("source-feedback-row").count();
    if (rowsAfter <= rowsBefore) throw new Error(`source feedback rows did not increase: ${rowsBefore} -> ${rowsAfter}`);

    if (!REAL) {
      await page.getByLabel("关闭").click();
      await page.getByTestId("source-management-drawer").waitFor({ state: "detached", timeout: 8000 });
      await page.getByTestId("primary-action").click();
      await assertVisible(page, "decision-operation-status");
      await assertVisible(page, "attribution-generation-status");
      const operationText = await page.getByTestId("decision-operation-status").innerText();
      if (!operationText.includes("正在生成归因分析")) throw new Error(`unexpected operation status: ${operationText}`);
      const recordState = await page.getByTestId("stage-local-record-node").filter({ hasText: "生成归因分析" }).first().getAttribute("data-state");
      if (recordState !== "current") throw new Error(`expected generating record state current, got ${recordState}`);
      await page.getByTestId("attribution-source").waitFor({ timeout: 10_000 });

      const firstPlanResponse = page.waitForResponse((response) => response.url().includes("/optimization-plan/generate") && response.request().method() === "POST");
      await page.getByTestId("primary-action").click();
      const firstPlan = await firstPlanResponse;
      if (!firstPlan.ok()) throw new Error(`first optimization plan generation failed: ${firstPlan.status()}`);
      await page.getByTestId("optimization-plan-source").waitFor({ timeout: 10_000 });
      const primaryAction = page.getByTestId("current-decision-card").getByTestId("primary-action").first();
      await primaryAction.waitFor({ timeout: 10_000 });
      const executeLabel = (await primaryAction.innerText()).trim();
      const executeAction = await primaryAction.getAttribute("data-action");
      if (executeLabel !== "执行优化") throw new Error(`unexpected execute optimization label: ${executeLabel}`);
      if (executeAction !== "apply-execution") throw new Error(`unexpected execute action: ${executeAction}`);
      const decisionText = await page.getByTestId("current-decision-card").innerText();
      if (decisionText.includes("自动执行优化")) throw new Error(`decision card still contains old execute copy: ${decisionText}`);
      const decisionRegenerate = await assertVisible(page, "decision-regenerate-optimization-plan");
      const decisionRegenerateLabel = (await decisionRegenerate.innerText()).trim();
      if (decisionRegenerateLabel !== "重新生成优化方案") throw new Error(`unexpected decision regenerate label: ${decisionRegenerateLabel}`);
      const detailRegenerate = await assertVisible(page, "regenerate-optimization-plan");
      const detailRegenerateLabel = (await detailRegenerate.innerText()).trim();
      if (detailRegenerateLabel !== "重新生成优化方案") throw new Error(`unexpected detail regenerate label: ${detailRegenerateLabel}`);

      const secondPlanResponse = page.waitForResponse((response) => response.url().includes("/optimization-plan/generate") && response.request().method() === "POST");
      await decisionRegenerate.click();
      const secondPlan = await secondPlanResponse;
      if (!secondPlan.ok()) throw new Error(`regenerate optimization plan failed: ${secondPlan.status()}`);
      await page.waitForFunction(
        () => document.querySelector('[data-testid="optimization-plan"]')?.textContent?.includes("第 2 版"),
        null,
        { timeout: 10_000 },
      );
      if (state.optimizationGenerateCount < 2) throw new Error(`expected regeneration endpoint to be called twice, got ${state.optimizationGenerateCount}`);
    }

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
