#!/usr/bin/env node
// v2.7 UI 设计一致性硬门（不是功能可用门）。
// 逐条断言 docs/AgentGov_v2.7_四阶段改进治理工作台UI整改方案.md 的设计规则，输出 per-rule 记分卡。
// 每条规则标注由哪个整改阶段（P0..P4）转绿；未达标如实记 fail，禁止用"功能可用"冒充"设计一致"。
//
// 双模式：
//   - 默认：自己起 vite + mock 后端，验证「结构性」设计规则（确定性，可进 CI / coverage_policy）。
//   - 验收：设 RUNTIME_UI_BASE（如真实容器 http://127.0.0.1:45173）+ RUNTIME_API_BASE 直连真实 UI/API。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import process from "node:process";
import { scrollNavigationMetrics, seedPlaygroundMessages } from "./playground_scroll_test_helpers.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = new URL("..", import.meta.url).pathname;
const ts = "2026-06-18T00:00:00Z";

function dockerEnvValue(name) {
  try {
    const content = readFileSync(new URL("../docker/.env", import.meta.url), "utf8");
    for (const rawLine of content.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      const i = line.indexOf("=");
      if (i <= 0) continue;
      if (line.slice(0, i).trim() === name) return line.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  } catch { /* ignore */ }
  return "";
}

const REAL = !!process.env.RUNTIME_UI_BASE;
const port = Number(process.env.PARITY_PORT || 55197);
const uiBase = (process.env.RUNTIME_UI_BASE || `http://127.0.0.1:${port}`).replace(/\/$/, "");
const apiBase = (process.env.RUNTIME_API_BASE || "http://runtime.test").replace(/\/$/, "");
const apiKey = process.env.RUNTIME_API_KEY || dockerEnvValue("FRONTEND_RUNTIME_API_KEY") || dockerEnvValue("API_KEY") || "";
let auditTargetId = "imp-demo01";
let auditTargets = {
  feedback: "imp-demo01",
  attribution: "imp-demo02",
  optimization: "imp-demo03",
  testRelease: "imp-demo04",
};

// ---- mock 后端（仅默认模式）----
const AGENTS = [
  { agent_id: "soc-ops", name: "安全运营助手", category: "", workspace_dir: "/w/soc", created_at: ts, status: "active" },
  { agent_id: "shop-bot", name: "电商客服", category: "", workspace_dir: "/w/shop", created_at: ts, status: "active" },
];
const IMPROVEMENTS = [
  { improvement_id: "imp-demo01", agent_id: "soc-ops", title: "时间窗口误判治理", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "triage", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo02", agent_id: "soc-ops", title: "时间窗口误判治理 · 归因", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "attribution", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo03", agent_id: "soc-ops", title: "时间窗口误判治理 · 优化", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "optimization", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo04", agent_id: "soc-ops", title: "时间窗口误判治理 · 测试", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "regression", improvement_status: "active", created_at: ts, updated_at: ts },
];
function defaultPayload(path, request = {}) {
  if (path === "/health") return { status: "ok", model: "parity-mock" };
  if (path === "/api/agent-registry") return AGENTS;
  if (path === "/api/agents" || path === "/api/skills" || path === "/api/sessions" || path === "/api/agent-releases") return [];
  if (
    path === "/api/agent-runs"
    || path === "/api/feedback-sources"
    || path === "/api/feedback-signals"
    || path === "/api/soc-events"
    || path === "/api/pending-correlations"
    || path === "/api/feedback-cases"
    || path === "/api/optimization-tasks"
    || path === "/api/external-governance-items"
    || path === "/api/external-governance-webhooks"
    || path === "/api/eval-cases"
    || path === "/api/eval-runs"
    || path === "/api/feedback-optimization-batches"
  ) return [];
  if (path === "/api/agent-change-sets") return [{
    change_set_id: "agc-demo",
    agent_id: "soc-ops",
    created_at: ts,
    updated_at: ts,
    status: "regression_failed",
    optimization_task_id: "opt-task-demo",
    execution_job_id: "job-demo",
    base_commit_sha: "base-demo",
    candidate_commit_sha: "candidate-demo",
    branch_name: "agent-change/agc-demo",
    worktree_path: "/tmp/agc-demo",
    title: "告警误报治理候选变更",
    diff_summary: { modified: 2 },
    publication_blocker: "批次回归存在失败用例",
  }];
  if (/^\/api\/agent-change-sets\/[^/]+\/regression-runs$/.test(path)) return { eval_run_id: "evr-demo", result_status: "passed", items: [], summary: { total: 0, passed: 0, failed: 0 } };
  if (/^\/api\/agent-change-sets\/[^/]+\/publish$/.test(path)) return { release_id: "agr-demo", agent_id: "soc-ops", status: "published", tag_name: "agent-release-demo", commit_sha: "candidate-demo", created_at: ts, updated_at: ts };
  if (path === "/api/assets") return [{
    asset_id: "ast-dataset-1",
    agent_id: "soc-ops",
    asset_type: "test_dataset",
    title: "测试数据集：时间窗口误判治理",
    body: JSON.stringify({ test_dataset_id: "tds-imp-demo04", agent_id: "soc-ops", improvement_id: "imp-demo04", lifecycle: "candidate", baseline_version: "v1.2.0", candidate_version: "ver-cand" }),
    source_improvement_id: "imp-demo04",
    inherited_from: "",
    created_at: ts,
    updated_at: ts,
  }, {
    asset_id: "ast-1",
    agent_id: "soc-ops",
    asset_type: "regression",
    title: "回归保障：时间窗口不一致不得误判",
    body: "当告警时间与事件时间窗口不一致时，Agent 应提示核验数据源。",
    source_improvement_id: "imp-demo01",
    inherited_from: "",
    created_at: ts,
    updated_at: ts,
  }];
  if (path === "/api/improvements") return IMPROVEMENTS;
  if (path === "/api/config") return {
    agent_id: "main-agent",
    claude_config_mode: "native",
    claude_root: "/data/business-agents/main-agent/claude-root",
    claude_home: "/data/business-agents/main-agent/claude-root/.claude",
    claude_global_config_file: "/data/business-agents/main-agent/claude-root/.claude.json",
    claude_config_dir: null,
    setting_sources_effective: null,
    mappings: [
      {
        scope: "project",
        kind: "instructions",
        container_path: "/data/business-agents/main-agent/workspace/CLAUDE.md",
        exists: true,
        loaded_by_default: true,
        load_semantics: "claude_loaded",
        display_group: "agent_project_config",
        safe_to_edit: true,
        git_policy: "tracked",
      },
      {
        scope: "project",
        kind: "mcp",
        container_path: "/data/business-agents/main-agent/workspace/.mcp.json",
        exists: true,
        loaded_by_default: true,
        load_semantics: "claude_loaded",
        display_group: "agent_project_config",
        safe_to_edit: true,
        git_policy: "tracked",
      },
      {
        scope: "runtime",
        kind: "agent-change-set-worktrees",
        container_path: "/data/business-agents/main-agent/version/worktrees",
        exists: true,
        loaded_by_default: false,
        load_semantics: "runtime_used",
        display_group: "versioning_runtime",
        safe_to_edit: false,
        git_policy: "ignored",
      },
    ],
  };
  if (path === "/api/agent-config-file") {
    const body = request.method === "PUT" ? JSON.parse(request.postData || "{}") : {};
    return {
      agent_id: "main-agent",
      path: ".mcp.json",
      container_path: "/data/business-agents/main-agent/workspace/.mcp.json",
      exists: true,
      content: typeof body.content === "string" ? body.content : '{\n  "mcpServers": {}\n}\n',
      sha256: "mock-sha-after",
      size_bytes: 24,
      content_type: "application/json",
      sdk_session_invalidated: request.method === "PUT",
    };
  }
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  if (/^\/api\/improvements\/[^/]+\/similar$/.test(path)) return [{ improvement: { ...IMPROVEMENTS[0], improvement_id: "imp-sim01", title: "告警误报治理(相似项)" }, score: 0.55 }];
  if (/^\/api\/improvements\/[^/]+\/links$/.test(path)) return [];
  if (/^\/api\/improvements\/[^/]+\/feedbacks$/.test(path)) return [{ feedback_id: "fb-1", improvement_id: "imp-demo01", agent_id: "soc-ops", summary: "这个告警其实是误报", source: "playground_run", status: "merged", raw_text: "", run_id: "run-1", session_id: "s-1", agent_version_id: "v1.2.0", scenario: "alert-triage", task_id: "task-1", alert_id: "alert-001", case_id: "case-001", created_at: ts }];
  if (/^\/api\/improvements\/[^/]+\/normalized-feedback$/.test(path)) return { normalized_feedback_id: "nf-1", improvement_id: "imp-demo01", problem: "告警误报", possible_reason: "事件时间与告警时间不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "进入改进处理", user_quote: "这个告警其实是误报", status: "draft", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/attribution$/.test(path)) return { attribution_id: "attr-1", improvement_id: "imp-demo01", summary: "MCP 数据时间不一致导致误判", responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"], evidence: ["list_events 返回的数据时间与告警时间窗口不一致"], status: "draft", generated_by: "governor", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/optimization-plan$/.test(path)) return { optimization_plan_id: "opt-1", improvement_id: "imp-demo01", summary: "针对告警误报：补充时间一致性校验", changes: [{ target: "prompt", change: "新增事件时间与告警时间一致性校验指令" }], status: "confirmed", generated_by: "governor", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/regression-assessment$/.test(path)) return { regression_assessment_id: "reg-1", improvement_id: "imp-demo01", summary: "治理 Agent 生成 1 条回归用例候选。", cases: [{ prompt: "当事件时间与告警时间不一致时如何处置？", expected_behavior: "先核验时间一致性，不直接升级", checkpoints: ["是否核验时间", "是否避免误升级"] }], status: "draft", generated_by: "governor", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/execution$/.test(path)) return { execution_id: "exec-1", improvement_id: "imp-demo01", summary: "已在隔离变更集应用并生成候选版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "ver-cand", status: "draft", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "ver-cand", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts };
  if (/^\/api\/automation-policy/.test(path)) return { agent_id: "soc-ops", mode: "off" };
  const oneImprovement = path.match(/^\/api\/improvements\/([^/]+)$/);
  if (oneImprovement) return IMPROVEMENTS.find((item) => item.improvement_id === decodeURIComponent(oneImprovement[1])) || IMPROVEMENTS[0];
  return {};
}
function startVite() {
  const c = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"], detached: true });
  c.stdout.on("data", () => {}); c.stderr.on("data", () => {});
  return c;
}
function killTree(c, s) { try { process.kill(-c.pid, s); } catch { try { c.kill(s); } catch { /* gone */ } } }
async function stopChild(c) { if (!c || c.exitCode !== null) return; killTree(c, "SIGTERM"); await new Promise((r) => { const t = setTimeout(() => { killTree(c, "SIGKILL"); r(); }, 2000); c.once("exit", () => { clearTimeout(t); r(); }); }); }
async function waitForVite() { const d = Date.now() + 30000; while (Date.now() < d) { try { const r = await fetch(uiBase); if (r.ok) return; } catch { await new Promise((r) => setTimeout(r, 250)); } } throw new Error("vite not ready"); }

// 整改基线（BASELINE 模式）：已落地阶段的规则必须保持全绿（防回归）；尚未落地阶段的规则可红。
// 随 P1..P4 推进，把对应规则 id 加入此基线；真实容器验收用 RUNTIME_UI_BASE，目标是全量基线规则全绿。
const BASELINE_RULES = new Set(
  (process.env.PARITY_BASELINE || "nav-converged,settings-ia,playground-clean,playground-action-semantics,playground-session-sidebar,playground-runtime-settings-drawer,message-actions,playground-scroll-navigation,trace-evidence-panel,panel-size-policy,feedback-drawer-2phase,context-4types,release-merged-into-test-stage,test-release-stage-panels,theme-governance-light,improvement-default-detail,decision-card-slim,four-stage-panels,closed-loop-spine,improvement-content,stage-detail-drawers,improvement-assets,asset-browse-first,attribution-actions,source-feedback-table,detail-collapsed,full-chain,status-filter,merge-basis,trace-summary,optimization-execution,governance-generation-source,execution-version-binding,regression-governor").split(",").map((s) => s.trim()).filter(Boolean),
);

const has = async (page, testid) => (await page.getByTestId(testid).count()) > 0;
const visible = async (page, testid) => (await page.getByTestId(testid).count()) > 0 && (await page.getByTestId(testid).first().isVisible());
const textIncludes = async (locator, value) => (await locator.innerText().catch(() => "")).includes(value);
const scrollDistance = async (page) => page.getByTestId("playground-messages").evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
async function fillJsonEditor(page, value) {
  const root = page.getByTestId("agent-config-file-editor-content");
  const cmContent = root.locator(".cm-content");
  if (await cmContent.count()) {
    await cmContent.click();
    await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
    await page.keyboard.type(value);
    return;
  }
  await root.fill(value);
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

function authHeaders(extra = {}) {
  return {
    Accept: "application/json",
    ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    ...extra,
  };
}

async function apiJson(path, init = {}) {
  const res = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: authHeaders(init.headers || {}),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = await res.text(); } catch { /* ignore */ }
    throw new Error(`${init.method || "GET"} ${path} failed: ${res.status} ${detail}`);
  }
  return res.json();
}

async function postJson(path, body) {
  return apiJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function putJson(path, body) {
  return apiJson(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function advanceImprovement(improvementId, stages) {
  for (const stage of stages) {
    await postJson(`/api/improvements/${improvementId}/lifecycle`, { stage });
  }
}

async function seedImprovementStage({ agentId, stamp, key, title, stagePath }) {
  const prefix = `${stamp}-${key}`;
  const item = await postJson("/api/improvements", {
    agent_id: agentId,
    title,
    summary: "sec-ops-data MCP 数据时间窗口与告警时间不一致，导致 Agent 将误报判断为真实横向移动。",
    source_feedback_refs: [`${prefix}-fb-1`, `${prefix}-fb-2`],
    auto_merge: false,
  });
  await postJson(`/api/improvements/${item.improvement_id}/feedbacks`, {
    summary: "这个横向移动告警其实是误报",
    source: "playground_run",
    raw_text: "Agent 没注意到事件时间和告警时间不一致，导致误报升级。",
    run_id: `${prefix}-run-1`,
    session_id: `${prefix}-session-1`,
    agent_version_id: `${prefix}-baseline`,
    scenario: "alert-triage",
    task_id: `${prefix}-task-1`,
    alert_id: `${prefix}-alert`,
    case_id: `${prefix}-case`,
  });
  await postJson(`/api/improvements/${item.improvement_id}/feedbacks`, {
    summary: "sec-ops-data 返回的数据像是模拟数据",
    source: "trace",
    raw_text: "list_events 返回的数据时间窗口无法支撑当前告警判断。",
    run_id: `${prefix}-run-2`,
    session_id: `${prefix}-session-2`,
    agent_version_id: `${prefix}-baseline`,
    scenario: "alert-triage",
    task_id: `${prefix}-task-2`,
    alert_id: `${prefix}-alert`,
    case_id: `${prefix}-case`,
  });
  await putJson(`/api/improvements/${item.improvement_id}/normalized-feedback`, {
    problem: "告警误报治理",
    possible_reason: "事件时间与告警时间窗口不一致",
    possible_object: "sec-ops-data MCP 数据",
    impact: "中",
    suggestion: "进入归因和回归保障",
    user_quote: "这个横向移动告警其实是误报。",
  });
  await putJson(`/api/improvements/${item.improvement_id}/attribution`, {
    summary: "sec-ops-data MCP 返回的数据时间与告警时间窗口不一致，导致 Agent 误判。",
    responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"],
    evidence: ["list_events 返回的数据时间与告警时间窗口不一致", "来源反馈均指向时间窗口核验缺失"],
  });
  await putJson(`/api/improvements/${item.improvement_id}/optimization-plan`, {
    summary: "补充 sec-ops-data 时间窗口核验 SOP，并在 prompt 中要求先核验事件时间。",
    changes: [{ target: "prompt", change: "新增事件时间与告警时间一致性校验指令" }],
  });
  await putJson(`/api/improvements/${item.improvement_id}/execution`, {
    summary: "已按优化方案形成候选执行记录，关联审计版本。",
    changes_applied: ["prompt：新增时间窗口一致性校验", "SOP：补充 MCP 数据时间核验步骤"],
    agent_version: `${prefix}-candidate`,
  });
  await postJson("/api/assets", {
    agent_id: agentId,
    asset_type: "regression",
    title: `${prefix} 回归保障：时间窗口不一致不得误判`,
    body: "当告警时间与事件时间窗口不一致时，Agent 应提示核验数据源，不得直接升级处置。",
    source_improvement_id: item.improvement_id,
  });
  await postJson("/api/assets", {
    agent_id: agentId,
    asset_type: "methodology",
    title: `${prefix} MCP 数据时间窗口核验 SOP`,
    body: "先比对告警时间、事件时间、查询窗口和数据源时间戳，再下结论。",
    source_improvement_id: item.improvement_id,
  });
  if (key === "testRelease") {
    await postJson("/api/assets", {
      agent_id: agentId,
      asset_type: "test_dataset",
      title: `${prefix} 测试数据集：时间窗口误判治理`,
      body: JSON.stringify({
        test_dataset_id: `tds-${item.improvement_id}`,
        agent_id: agentId,
        improvement_id: item.improvement_id,
        lifecycle: "candidate",
        source_feedback_refs: item.source_feedback_refs,
        baseline_version: `${prefix}-baseline`,
        candidate_version: `${prefix}-candidate`,
        provenance: { created_by: "verify_v27_ui_design_parity", source: "real-container-seed" },
      }),
      source_improvement_id: item.improvement_id,
    });
  }
  if (stagePath.length) await advanceImprovement(item.improvement_id, stagePath);
  return item.improvement_id;
}

async function seedRealAuditData() {
  const agents = await apiJson("/api/agent-registry").catch(() => []);
  const agentId = agents.find((a) => a.status === "active")?.agent_id || agents[0]?.agent_id || "main-agent";
  const stamp = `audit-v27-${Date.now().toString(36)}`;
  const feedbackId = await seedImprovementStage({
    agentId,
    stamp,
    key: "feedback",
    title: `${stamp} 反馈整理 · sec-ops-data 时间窗口误判治理`,
    stagePath: ["triage"],
  });
  const attributionId = await seedImprovementStage({
    agentId,
    stamp,
    key: "attribution",
    title: `${stamp} 归因分析 · sec-ops-data 时间窗口误判治理`,
    stagePath: ["triage", "attribution"],
  });
  const optimizationId = await seedImprovementStage({
    agentId,
    stamp,
    key: "optimization",
    title: `${stamp} 优化执行 · sec-ops-data 时间窗口误判治理`,
    stagePath: ["triage", "attribution", "optimization"],
  });
  const testReleaseId = await seedImprovementStage({
    agentId,
    stamp,
    key: "testRelease",
    title: `${stamp} 测试发布 · sec-ops-data 时间窗口误判治理`,
    stagePath: ["triage", "attribution", "optimization", "execution", "regression"],
  });
  await postJson("/api/improvements", {
    agent_id: agentId,
    title: `${stamp} sec-ops-data 时间窗口误判重复反馈`,
    summary: "sec-ops-data 返回事件时间窗口和告警时间不一致，需要归并到同一改进事项。",
    source_feedback_refs: [`${stamp}-fb-2`, `${stamp}-fb-3`],
    auto_merge: false,
  });
  const changeSet = await postJson("/api/agent-change-sets", {
    title: `${stamp} 告警误报治理候选变更`,
    note: "v2.7 真实容器 UI 设计一致性验收种子：仅用于发布门禁结构检查，不执行强制发布。",
  }).catch(() => null);
  await postJson(`/api/improvements/${optimizationId}/links`, { kind: "change_set", ref_id: changeSet?.change_set_id || `${stamp}-change-set` }).catch(() => null);
  return {
    improvement_id: feedbackId,
    agent_id: agentId,
    stamp,
    targets: {
      feedback: feedbackId,
      attribution: attributionId,
      optimization: optimizationId,
      testRelease: testReleaseId,
    },
  };
}

async function openAuditImprovement(page) {
  await page.getByTestId("nav-improvement").click();
  await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 }).catch(() => {});
  const target = page.locator(`[data-testid="improvement-list-item"][data-item-id="${auditTargetId}"]`).first();
  await target.waitFor({ timeout: 8000 }).catch(() => {});
  if ((await target.count()) > 0) {
    await target.click();
    await page.getByTestId("improvement-detail").waitFor({ timeout: 8000 }).catch(() => {});
    return true;
  }
  const first = page.getByTestId("improvement-list-item").first();
  await first.waitFor({ timeout: 8000 }).catch(() => {});
  if ((await first.count()) === 0) return false;
  await first.click();
  await page.getByTestId("improvement-detail").waitFor({ timeout: 8000 }).catch(() => {});
  return true;
}

async function openImprovementById(page, improvementId) {
  await page.getByTestId("nav-improvement").click();
  await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 }).catch(() => {});
  const target = page.locator(`[data-testid="improvement-list-item"][data-item-id="${improvementId}"]`).first();
  await target.waitFor({ timeout: 8000 }).catch(() => {});
  if ((await target.count()) === 0) return false;
  await target.click();
  await page.getByTestId("improvement-detail").waitFor({ timeout: 8000 }).catch(() => {});
  return true;
}

function stageTarget(key, mockId) {
  return REAL ? auditTargets[key] : mockId;
}

const RULES = [
  { id: "nav-converged", phase: "P0", desc: "一级导航三支柱 Playground/改进事项/资产复利；测试发布归入改进治理第四阶段，旧发布不作为顶级主导航", async fn(page) {
    const nav = await page.locator(".topbar-nav .topbar-nav-button").count();
    const asset = await has(page, "nav-asset");
    const release = await has(page, "nav-release");
    const feedbackTopNav = await page.getByRole("button", { name: "反馈优化", exact: true }).count();
    return { ok: nav === 3 && asset && !release && feedbackTopNav === 0, detail: `topbar-nav=${nav} nav-asset=${asset} nav-release=${release} 反馈优化顶级=${feedbackTopNav}（期望 3/true/false/0）` };
  } },
  { id: "settings-ia", phase: "P0", desc: "Settings 使用宽幅工作台弹窗，含侧栏导航、内容区和 业务Agent/自动化策略/Developer 分组", async fn(page) {
    await (page.getByTestId("open-settings").click().catch(() => page.getByRole("button", { name: "设置" }).first().click()));
    await page.getByTestId("settings-panel").waitFor({ timeout: 8000 });
    const box = await page.getByTestId("settings-panel").boundingBox();
    const wide = (box?.width || 0) >= 1000;
    const tall = (box?.height || 0) >= 760;
    const navigation = await visible(page, "settings-navigation");
    const content = await visible(page, "settings-content");
    const oldHorizontalTabs = await page.locator(".settings-tabs").count();
    const tabs = ["agents", "automation", "developer"];
    const found = [];
    for (const tab of tabs) {
      await page.getByTestId(`settings-tab-${tab}`).click();
      const section = `settings-section-${tab}`;
      await page.getByTestId(section).waitFor({ timeout: 5000 }).catch(() => {});
      if (await visible(page, section)) found.push(section);
    }
    await page.getByTestId("settings-tab-agents").click();
    const agentTable = await visible(page, "settings-agent-table");
    await page.getByTestId("settings-tab-automation").click();
    const modeGroup = await visible(page, "settings-automation-mode");
    await page.getByTestId("settings-tab-developer").click();
    const runtimeInput = await visible(page, "settings-api-base");
    // 关闭设置弹窗，避免 modal-backdrop 拦截后续规则的点击。
    await page.locator(".settings-footer").getByRole("button", { name: "关闭" }).click().catch(() => {});
    await page.getByTestId("settings-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    const ok = wide && tall && navigation && content && oldHorizontalTabs === 0 && found.length === tabs.length && agentTable && modeGroup && runtimeInput;
    return { ok, detail: `size=${Math.round(box?.width || 0)}x${Math.round(box?.height || 0)} nav=${navigation} content=${content} oldTabs=${oldHorizontalTabs} sections=${found.length}/${tabs.length} table=${agentTable} mode=${modeGroup} runtime=${runtimeInput}` };
  } },
  { id: "playground-clean", phase: "P1", desc: "Playground 主区无旧 Subagent/Sessions/Skills 侧栏、无 Inspector、无常显 control-strip", async fn(page) {
    await page.getByTestId("nav-playground").click(); await page.waitForTimeout(400);
    const legacySidebar = await page.locator(".sidebar .panel-section").count();
    const inspector = await page.locator(".inspector").count();
    const controlStrip = await page.locator(".control-strip").count();
    return { ok: legacySidebar === 0 && inspector === 0 && controlStrip === 0, detail: `legacy-sidebar=${legacySidebar} inspector=${inspector} control-strip=${controlStrip}（期望全 0）` };
  } },
  { id: "playground-action-semantics", phase: "P1", desc: "Playground 动作语义分离：无旧配置入口，会话与运行设置分开", async fn(page) {
    await page.getByTestId("nav-playground").click(); await page.waitForTimeout(400);
    const oldConfig = await page.getByTestId("playground-config-trigger").count();
    const sessionTrigger = page.getByTestId("playground-session-trigger");
    const runtimeTrigger = page.getByTestId("playground-runtime-settings-trigger");
    const sessionCount = await sessionTrigger.count();
    const runtimeCount = await runtimeTrigger.count();
    const sessionText = sessionCount ? (await sessionTrigger.first().innerText().catch(() => "")).trim() : "";
    const sessionAria = sessionCount ? await sessionTrigger.first().getAttribute("aria-label").catch(() => "") : "";
    const sessionTitle = sessionCount ? await sessionTrigger.first().getAttribute("title").catch(() => "") : "";
    const sessionExpanded = sessionCount ? await sessionTrigger.first().getAttribute("aria-expanded").catch(() => "") : "";
    const sessionBox = sessionCount ? await sessionTrigger.first().boundingBox().catch(() => null) : null;
    const titleBox = await page.locator(".chat-header h2").first().boundingBox().catch(() => null);
    const runtimeBox = runtimeCount ? await runtimeTrigger.first().boundingBox().catch(() => null) : null;
    const runtimeText = runtimeCount ? await runtimeTrigger.first().innerText().catch(() => "") : "";
    const sessionIsIconOnly = sessionText === "";
    const sessionIsLeft = !!sessionBox && !!titleBox && !!runtimeBox && sessionBox.x < titleBox.x && sessionBox.x < runtimeBox.x;
    const sessionOk = sessionCount === 1
      && sessionIsIconOnly
      && sessionAria === "展开会话栏"
      && sessionTitle === "展开会话栏"
      && sessionExpanded === "false"
      && sessionIsLeft;
    const runtimeOk = runtimeCount === 1 && runtimeText.includes("运行设置") && !runtimeText.includes("会话");
    return { ok: oldConfig === 0 && sessionOk && runtimeOk, detail: `oldConfig=${oldConfig} session=${sessionCount}/iconOnly=${sessionIsIconOnly}/left=${sessionIsLeft}/aria=${sessionAria}/title=${sessionTitle}/expanded=${sessionExpanded} runtime=${runtimeCount}/${runtimeText}` };
  } },
  { id: "playground-session-sidebar", phase: "P1", desc: "Playground 会话管理进入左侧可折叠导航栏，且不混入运行设置", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const trigger = await has(page, "playground-session-trigger");
    const closedBefore = await page.getByTestId("playground-session-sidebar").count() === 0;
    if (trigger) await page.getByTestId("playground-session-trigger").click();
    const sidebar = page.getByTestId("playground-session-sidebar");
    await sidebar.waitFor({ timeout: 8000 }).catch(() => {});
    const open = await visible(page, "playground-session-sidebar");
    const width = open ? ((await sidebar.boundingBox())?.width || 0) : 0;
    const text = open ? await sidebar.innerText().catch(() => "") : "";
    const expandedAria = await page.getByTestId("playground-session-trigger").getAttribute("aria-expanded").catch(() => "");
    const expandedLabel = await page.getByTestId("playground-session-trigger").getAttribute("aria-label").catch(() => "");
    const duplicatedCloseInSidebar = open ? await sidebar.getByLabel("折叠会话栏").count() : 0;
    const hasSessionControls = text.includes("新会话") && text.includes("会话");
    const noRuntimeSettings = !text.includes("Subagent") && !text.includes("Skills Mode") && !text.includes("Allowed Tools");
    if (open) {
      await page.getByTestId("playground-session-trigger").click();
      await sidebar.waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    const closedAfterToggle = await page.getByTestId("playground-session-sidebar").count() === 0;
    const collapsedAria = await page.getByTestId("playground-session-trigger").getAttribute("aria-expanded").catch(() => "");
    return { ok: trigger && closedBefore && open && width >= 260 && width <= 340 && expandedAria === "true" && expandedLabel === "折叠会话栏" && duplicatedCloseInSidebar === 0 && closedAfterToggle && collapsedAria === "false" && hasSessionControls && noRuntimeSettings, detail: `trigger=${trigger} defaultCollapsed=${closedBefore} open=${open} width=${Math.round(width)} expanded=${expandedAria}/${expandedLabel} sidebarCloseButtons=${duplicatedCloseInSidebar} closedAfterToggle=${closedAfterToggle}/${collapsedAria} sessionControls=${hasSessionControls} noRuntimeSettings=${noRuntimeSettings}` };
  } },
  { id: "playground-runtime-settings-drawer", phase: "P1", desc: "Playground 运行设置进入独立抽屉，且不混入会话历史", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const trigger = await has(page, "playground-runtime-settings-trigger");
    if (trigger) await page.getByTestId("playground-runtime-settings-trigger").click();
    const drawer = page.getByTestId("playground-runtime-settings-drawer");
    await drawer.waitFor({ timeout: 8000 }).catch(() => {});
    const open = await visible(page, "playground-runtime-settings-drawer");
    const size = open ? await drawer.getAttribute("data-size") : null;
    const agentSettingsSection = open && await has(page, "runtime-agent-settings");
    const parameterSettingsSection = open && await has(page, "runtime-parameter-settings");
    const maxTurnsControl = open && await drawer.locator('input[type="number"]').count() === 1;
    const hasRuntimeSettings = agentSettingsSection && parameterSettingsSection && maxTurnsControl;
    const noMisleadingControls = open
      && !(await textIncludes(drawer, "Skills Mode"))
      && !(await textIncludes(drawer, "Allowed Tools"))
      && !(await textIncludes(drawer, "Disallowed Tools"));
    const noSessionHistory = open
      && await drawer.getByText("新会话").count() === 0
      && await drawer.getByText("删除会话映射").count() === 0
      && await drawer.getByText("Sessions").count() === 0
      && await drawer.getByTestId("playground-session-list").count() === 0
      && await page.getByTestId("playground-session-sidebar").count() === 0;
    const debug = open ? page.getByTestId("runtime-debug-section") : null;
    const debugClosed = debug ? await debug.evaluate((el) => !el.open).catch(() => false) : false;
    if (debug) await debug.locator("summary").click().catch(() => {});
    const debugVisible = open ? await textIncludes(drawer, "Runtime") && !(await textIncludes(drawer, "Events")) && !(await textIncludes(drawer, "Subagents / Skills")) : false;
    const agentConfigVisible = open ? await textIncludes(drawer, "Agent 配置") && await textIncludes(drawer, "版本治理运行态") : false;
    const mcpEditButton = open ? await has(page, "runtime-config-edit-mcp") : false;
    let mcpEditorOpened = false;
    let mcpEditorApplied = REAL;
    if (mcpEditButton) {
      await page.getByTestId("runtime-config-edit-mcp").click();
      await page.getByTestId("agent-config-file-editor").waitFor({ timeout: 8000 }).catch(() => {});
      mcpEditorOpened = await visible(page, "agent-config-file-editor");
      if (mcpEditorOpened && !REAL) {
        await fillJsonEditor(page, '{"mcpServers":{"parity":{"command":"node","args":["server.js"]}}}\n');
        await page.getByTestId("agent-config-file-editor-format").click();
        await page.getByTestId("agent-config-file-editor-apply").click();
        await page.getByTestId("agent-config-file-editor-status").waitFor({ timeout: 8000 }).catch(() => {});
        mcpEditorApplied = await visible(page, "agent-config-file-editor-status");
      }
      if (mcpEditorOpened) await page.getByTestId("agent-config-file-editor").getByRole("button", { name: "关闭" }).click().catch(() => {});
      await page.getByTestId("agent-config-file-editor").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    const noLegacyGovernancePath = open ? !(await textIncludes(drawer, "/data/agent-governance")) : false;
    if (open) {
      await drawer.getByLabel("关闭").click();
      await drawer.waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    return {
      ok: trigger && open && size === "wide" && hasRuntimeSettings && noMisleadingControls && noSessionHistory && debugClosed && debugVisible && agentConfigVisible && mcpEditorOpened && mcpEditorApplied && noLegacyGovernancePath,
      detail: `trigger=${trigger} open=${open} size=${size} runtimeSettings=${hasRuntimeSettings} sections=${agentSettingsSection}/${parameterSettingsSection} maxTurns=${maxTurnsControl} noMisleadingControls=${noMisleadingControls} noSessionHistory=${noSessionHistory} debugClosed=${debugClosed} debugVisible=${debugVisible} agentConfig=${agentConfigVisible} mcpEditor=${mcpEditorOpened}/${mcpEditorApplied} legacyPath=${!noLegacyGovernancePath}`,
    };
  } },
  { id: "message-actions", phase: "P1", desc: "助手回复动作含 创建反馈/查看Trace/获取上下文（领域级 data-testid）", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const create = await has(page, "message-action-create-feedback");
    const trace = await has(page, "message-action-view-trace");
    const ctx = await has(page, "message-action-get-context");
    return { ok: create && trace && ctx, detail: `create=${create} trace=${trace} get-context=${ctx}` };
  } },
  { id: "playground-scroll-navigation", phase: "P1", desc: "Playground 长对话自动置底、上滚暂停、一键置底与滚动预览导航", async fn(page) {
    await page.getByTestId("nav-playground").click();
    await page.getByTestId("playground-scroll-navigator").waitFor({ timeout: 8000 });
    await waitNearBottom(page);
    const initialDistance = await scrollDistance(page);
    await page.getByTestId("playground-messages").evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await page.getByTestId("playground-jump-to-bottom").waitFor({ timeout: 5000 });
    const jump = await visible(page, "playground-jump-to-bottom");
    await page.getByTestId("playground-scroll-rail").hover();
    await waitPreviewOpen(page);
    const previewItems = await page.getByTestId("playground-scroll-preview-item").count();
    const previewRoles = await page.getByTestId("playground-scroll-preview-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const markCount = await page.getByTestId("playground-scroll-mark").count();
    const markRoles = await page.getByTestId("playground-scroll-mark").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const largeMetrics = await scrollNavigationMetrics(page);
    await page.getByTestId("playground-scroll-preview-item").first().click();
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="playground-messages"]');
      return !!el && el.scrollTop <= 80;
    }, null, { timeout: 5000 });
    const nearTop = await page.getByTestId("playground-messages").evaluate((el) => el.scrollTop <= 80);
    await page.getByTestId("playground-jump-to-bottom").click();
    await waitNearBottom(page);
    const finalDistance = await scrollDistance(page);
    const noPanelMix = await page.getByTestId("playground-evidence-panel").count() === 0
      && await page.getByTestId("feedback-drawer").count() === 0
      && await page.getByTestId("playground-runtime-settings-drawer").count() === 0;
    const anchorRolesOk = previewRoles.every((role) => role === "user") && markRoles.every((role) => role === "user");
    await seedPlaygroundMessages(page, 4);
    await page.getByTestId("playground-messages").evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await page.getByTestId("playground-scroll-rail").hover();
    await waitPreviewOpen(page);
    const fewPreviewItems = await page.getByTestId("playground-scroll-preview-item").count();
    const fewMarkCount = await page.getByTestId("playground-scroll-mark").count();
    const fewPreviewRoles = await page.getByTestId("playground-scroll-preview-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const fewMarkRoles = await page.getByTestId("playground-scroll-mark").evaluateAll((items) => items.map((item) => item.getAttribute("data-message-role")));
    const fewMetrics = await scrollNavigationMetrics(page);
    const fewRolesOk = fewPreviewRoles.every((role) => role === "user") && fewMarkRoles.every((role) => role === "user");
    await page.evaluate(() => window.sessionStorage.removeItem("parity-preserve-playground-session"));
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 8000 });

    const ok = initialDistance <= 24
      && jump
      && previewItems === 36
      && markCount === 24
      && anchorRolesOk
      && largeMetrics.railHeight >= 340
      && largeMetrics.maxGap <= 24
      && largeMetrics.centerDelta <= 24
      && fewPreviewItems === 4
      && fewMarkCount === 4
      && fewRolesOk
      && fewMetrics.railHeight >= 90
      && fewMetrics.railHeight <= 130
      && fewMetrics.avgGap >= 24
      && fewMetrics.avgGap <= 40
      && fewMetrics.centerDelta <= 24
      && nearTop
      && finalDistance <= 24
      && noPanelMix;
    return { ok, detail: `initial=${initialDistance} jump=${jump} large=${previewItems}/${markCount}/${largeMetrics.railHeight}px gap=${largeMetrics.minGap}-${largeMetrics.maxGap} userOnly=${anchorRolesOk} few=${fewPreviewItems}/${fewMarkCount}/${fewMetrics.railHeight}px avgGap=${fewMetrics.avgGap} fewUserOnly=${fewRolesOk} center=${largeMetrics.centerDelta}/${fewMetrics.centerDelta} nearTop=${nearTop} final=${finalDistance} noPanelMix=${noPanelMix}` };
  } },
  { id: "trace-evidence-panel", phase: "P0", desc: "查看 Trace 打开右侧运行证据 tab 面板，旧中心 modal/Trace 抽屉不再出现", async fn(page) {
    await page.getByTestId("nav-playground").click();
    if (!(await has(page, "message-action-view-trace"))) return { ok: false, detail: "无 Trace 入口" };
    await page.getByTestId("message-action-view-trace").first().click();
    await page.getByTestId("playground-evidence-panel").waitFor({ timeout: 8000 });
    const panel = await visible(page, "playground-evidence-panel");
    const traceTab = await visible(page, "evidence-tab-trace");
    const tabCount = await page.locator(".evidence-tab").count();
    const legacy = await page.locator(".detail-modal-card").isVisible().catch(() => false);
    const traceDrawer = await page.getByTestId("trace-drawer").count();
    const langfuse = await has(page, "trace-open-langfuse");
    await page.getByTestId("playground-evidence-panel").getByLabel("折叠运行证据栏").click();
    await page.getByTestId("playground-evidence-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    return { ok: panel && traceTab && tabCount === 1 && !legacy && traceDrawer === 0 && langfuse, detail: `panel=${panel} traceTab=${traceTab} tabCount=${tabCount} legacyModal=${legacy} traceDrawer=${traceDrawer} langfuse=${langfuse}` };
  } },
  { id: "panel-size-policy", phase: "P0", desc: "侧栏、tab 面板与抽屉按职责分档且打开后稳定", async fn(page) {
    await page.getByTestId("nav-playground").click();
    await page.getByTestId("message-action-view-trace").first().click();
    await page.getByTestId("playground-evidence-panel").waitFor({ timeout: 8000 });
    const traceWidth = (await page.getByTestId("playground-evidence-panel").boundingBox())?.width || 0;
    const resizeHandle = page.getByTestId("evidence-panel-resize-handle");
    const resizeBox = await resizeHandle.boundingBox();
    if (resizeBox) {
      await page.mouse.move(resizeBox.x + resizeBox.width / 2, resizeBox.y + 36);
      await page.mouse.down();
      await page.mouse.move(resizeBox.x - 110, resizeBox.y + 36, { steps: 8 });
      await page.mouse.up();
    }
    const resizedTraceWidth = (await page.getByTestId("playground-evidence-panel").boundingBox())?.width || 0;
    const resizeAria = Number(await resizeHandle.getAttribute("aria-valuenow") || 0);
    await page.getByTestId("playground-evidence-panel").getByLabel("折叠运行证据栏").click();
    await page.getByTestId("playground-evidence-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("message-action-create-feedback").first().click();
    await page.getByTestId("feedback-drawer").waitFor({ timeout: 8000 });
    const feedbackSize = await page.getByTestId("feedback-drawer").getAttribute("data-size");
    const feedbackWidth = (await page.getByTestId("feedback-drawer").boundingBox())?.width || 0;
    await page.getByTestId("feedback-drawer").getByLabel("关闭").click();
    await page.getByTestId("feedback-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("playground-session-trigger").click();
    await page.getByTestId("playground-session-sidebar").waitFor({ timeout: 8000 });
    const sessionWidth = (await page.getByTestId("playground-session-sidebar").boundingBox())?.width || 0;
    await page.getByTestId("playground-session-trigger").click();
    await page.getByTestId("playground-session-sidebar").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("playground-runtime-settings-trigger").click();
    await page.getByTestId("playground-runtime-settings-drawer").waitFor({ timeout: 8000 });
    const settingsSize = await page.getByTestId("playground-runtime-settings-drawer").getAttribute("data-size");
    const settingsWidth = (await page.getByTestId("playground-runtime-settings-drawer").boundingBox())?.width || 0;
    await page.getByTestId("playground-runtime-settings-drawer").getByLabel("关闭").click();
    await page.getByTestId("playground-runtime-settings-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    const ok = traceWidth >= 520
      && traceWidth <= 590
      && resizedTraceWidth >= traceWidth + 80
      && resizedTraceWidth <= 680
      && resizeAria === Math.round(resizedTraceWidth)
      && feedbackSize === "narrow"
      && feedbackWidth >= 430
      && sessionWidth >= 260
      && sessionWidth <= 340
      && settingsSize === "wide"
      && settingsWidth >= 860;
    return { ok, detail: `trace-panel=${Math.round(traceWidth)} resized=${Math.round(resizedTraceWidth)} aria=${resizeAria} feedback=${feedbackSize}/${Math.round(feedbackWidth)} session-sidebar=${Math.round(sessionWidth)} settings=${settingsSize}/${Math.round(settingsWidth)}` };
  } },
  { id: "feedback-drawer-2phase", phase: "P1", desc: "创建反馈 Drawer 两阶段：输入态 → 系统理解确认态", async fn(page) {
    await page.getByTestId("nav-playground").click();
    if (!(await has(page, "feedback-drawer-open"))) return { ok: false, detail: "无 feedback-drawer-open 入口" };
    await page.getByTestId("feedback-drawer-open").first().click();
    const open = await visible(page, "feedback-drawer");
    const state = open ? await page.getByTestId("feedback-drawer").getAttribute("data-state") : null;
    if (open) {
      await page.getByTestId("feedback-drawer").getByLabel("关闭").click();
      await page.getByTestId("feedback-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    }
    return { ok: open && state === "input", detail: `drawer 可见=${open} data-state=${state}` };
  } },
  { id: "context-4types", phase: "P2", desc: "获取上下文四类型 + 下载", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项可打开上下文（需种子数据）" };
    await page.getByTestId("open-context-drawer").click().catch(() => {});
    await page.getByTestId("context-drawer").waitFor({ timeout: 8000 }).catch(() => {});
    const drawerSize = await page.getByTestId("context-drawer").getAttribute("data-size").catch(() => null);
    const types = ["context-type-problem", "context-type-ai", "context-type-playwright", "context-type-json"];
    const found = []; for (const t of types) if (await has(page, t)) found.push(t);
    await page.getByTestId("context-type-json").click().catch(() => {});
    const preview = await page.getByTestId("context-preview").innerText().catch(() => "");
    const rich = preview.includes('"attribution_id"')
      && preview.includes('"agent_version_id"')
      && preview.includes('"optimization_plan_id"')
      && preview.includes('"asset_id"')
      && preview.includes('"test_dataset_refs"')
      && !preview.includes('"attribution": null')
      && !preview.includes('"evidence": []');
    const download = await has(page, "context-download");
    await page.getByTestId("context-drawer").getByLabel("关闭").click().catch(() => {});
    await page.getByTestId("context-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    return { ok: drawerSize === "medium" && found.length === 4 && download && rich, detail: `size=${drawerSize} 类型 ${found.length}/4，下载=${download}，证据链JSON=${rich}` };
  } },
  { id: "release-merged-into-test-stage", phase: "P2", desc: "旧发布顶级入口消失，发布门禁预览合入测试发布阶段", async fn(page) {
    const releaseNav = await has(page, "nav-release");
    const opened = await openImprovementById(page, stageTarget("testRelease", "imp-demo04"));
    if (!opened) return { ok: false, detail: "无法打开测试发布阶段改进事项" };
    const stage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    const gate = await has(page, "stage-panel-release-gate");
    const primary = await page.getByTestId("primary-action").innerText().catch(() => "");
    return { ok: !releaseNav && stage === "test_release" && gate && primary.includes("执行回归测试"), detail: `nav-release=${releaseNav} stage=${stage} gate=${gate} primary=${primary}` };
  } },
  { id: "test-release-stage-panels", phase: "P1", desc: "测试发布阶段含测试数据集、回归执行、覆盖场景、执行环境和门禁预览五个面板", async fn(page) {
    const opened = await openImprovementById(page, stageTarget("testRelease", "imp-demo04"));
    if (!opened) return { ok: false, detail: "无法打开测试发布阶段改进事项" };
    const panels = ["test-dataset-asset", "regression-guarantee", "stage-panel-coverage", "stage-panel-execution-baseline", "stage-panel-release-gate"];
    const found = [];
    for (const panel of panels) if (await has(page, panel)) found.push(panel);
    const datasetId = await page.getByTestId("test-dataset-id").innerText().catch(() => "");
    const runRef = await page.getByTestId("regression-run-dataset-ref").innerText().catch(() => "");
    return { ok: found.length === panels.length && datasetId && runRef === datasetId, detail: `panels=${found.length}/${panels.length} dataset=${datasetId} regression_ref=${runRef}` };
  } },
  { id: "improvement-default-detail", phase: "P1", desc: "改进列表有数据时默认展示首个详情，不留空白首屏", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 });
    await page.getByTestId("improvement-detail").waitFor({ timeout: 8000 }).catch(() => {});
    const detail = await has(page, "improvement-detail");
    const emptyVisible = await page.locator(".iw-detail-panel .iw-empty").isVisible().catch(() => false);
    return { ok: detail && !emptyVisible, detail: `detail=${detail} emptyVisible=${emptyVisible}` };
  } },
  { id: "decision-card-slim", phase: "P1", desc: "决策卡只承载主决策/返回/事实变更动作，不混入查看 Trace/Diff/日志/上下文", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    const card = page.getByTestId("current-decision-card");
    const text = await card.innerText().catch(() => "");
    const forbidden = ["查看 Trace", "查看完整 Trace", "查看 Diff", "查看完整 Diff", "查看日志", "查看测试计划", "获取上下文", "查看处理链路"].filter((word) => text.includes(word));
    const primaryCount = await card.getByTestId("primary-action").count();
    const contextOutside = await has(page, "open-context-drawer");
    return { ok: forbidden.length === 0 && primaryCount === 1 && contextOutside, detail: `forbidden=${forbidden.join(",") || "none"} primary=${primaryCount} contextOutside=${contextOutside}` };
  } },
  { id: "four-stage-panels", phase: "P1", desc: "四个内部阶段样例分别映射到四阶段工作面板", async fn(page) {
    const expectations = [
      ["feedback", "imp-demo01", "feedback_sorting", "stage-panel-sorting-result"],
      ["attribution", "imp-demo02", "attribution_analysis", "attribution"],
      ["optimization", "imp-demo03", "optimization_execution", "optimization-plan"],
      ["testRelease", "imp-demo04", "test_release", "test-dataset-asset"],
    ];
    const seen = [];
    for (const [key, mockId, stage, panel] of expectations) {
      const id = stageTarget(key, mockId);
      if (!(await openImprovementById(page, id))) return { ok: false, detail: `无法打开 ${id}` };
      const current = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
      const panelVisible = await has(page, panel);
      seen.push(`${id}:${current}:${panelVisible}`);
      if (current !== stage || !panelVisible) return { ok: false, detail: seen.join(" | ") };
    }
    return { ok: true, detail: seen.join(" | ") };
  } },
  { id: "closed-loop-spine", phase: "P1", desc: "改进详情始终显示四阶段 spine，并支持已完成阶段只读回看、未来阶段禁用", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("closed-loop-spine").waitFor({ timeout: 6000 }).catch(() => {});
    const spine = await has(page, "closed-loop-spine");
    const steps = await page.getByTestId("closed-loop-step").count();
    const labels = await page.getByTestId("closed-loop-step").evaluateAll((nodes) => nodes.map((node) => node.textContent || "").join("|")).catch(() => "");
    const okLabels = ["反馈整理", "归因分析", "优化执行", "测试发布"].every((label) => labels.includes(label));
    const currentBefore = await page.getByTestId("current-decision-card").getAttribute("data-visible-stage").catch(() => "");
    const feedbackStep = page.getByTestId("closed-loop-step").filter({ hasText: "反馈整理" }).first();
    const futureStep = page.getByTestId("closed-loop-step").filter({ hasText: "测试发布" }).first();
    const futureDisabled = await futureStep.isDisabled().catch(() => false);
    await feedbackStep.click();
    await page.getByTestId("stage-review-banner").waitFor({ timeout: 6000 }).catch(() => {});
    const reviewedStage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    const decisionAfterReview = await page.getByTestId("current-decision-card").getAttribute("data-visible-stage").catch(() => "");
    const factActions = await page.locator('[data-testid="stage-work-area"] [data-testid="confirm-attribution"], [data-testid="stage-work-area"] [data-testid="generate-attribution"], [data-testid="stage-work-area"] [data-testid="confirm-optimization-plan"], [data-testid="stage-work-area"] [data-testid="adopt-regression"]').count();
    await page.getByTestId("return-current-stage").click();
    const returnedStage = await page.getByTestId("stage-work-area").getAttribute("data-visible-stage").catch(() => "");
    return {
      ok: spine && steps === 4 && okLabels && currentBefore === "optimization_execution" && futureDisabled && reviewedStage === "feedback_sorting" && decisionAfterReview === currentBefore && factActions === 0 && returnedStage === "optimization_execution",
      detail: `spine=${spine} steps=${steps} futureDisabled=${futureDisabled} reviewed=${reviewedStage} decision=${currentBefore}->${decisionAfterReview} factActions=${factActions} returned=${returnedStage}`,
    };
  } },
  { id: "improvement-content", phase: "P3", desc: "改进详情含系统理解(NormalizedFeedback) + 归因(Attribution 正文/责任边界/证据)", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution").waitFor({ timeout: 6000 }).catch(() => {});
    const attr = await has(page, "attribution");
    const ev = await has(page, "attribution-evidence");
    return { ok: attr && ev, detail: `归因=${attr} 证据=${ev}` };
  } },
  { id: "stage-detail-drawers", phase: "P1", desc: "四阶段面板头部「查看详情/管理」统一打开对应详情抽屉（无死按钮，内容与卡片对应）", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("stage-panel-impact-scope").waitFor({ timeout: 6000 }).catch(() => {});
    const btn = page.getByTestId("stage-panel-impact-scope").getByRole("button", { name: "查看详情" });
    if (!(await btn.count())) return { ok: false, detail: "影响范围卡缺查看详情按钮（疑似死按钮）" };
    await btn.first().click();
    await page.getByTestId("stage-detail-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    const drawer = await visible(page, "stage-detail-drawer");
    const key = await page.getByTestId("stage-detail-content").getAttribute("data-detail-key").catch(() => "");
    await page.locator(".drawer-shell-actions").getByRole("button", { name: "关闭" }).first().click().catch(() => {});
    await page.getByTestId("stage-detail-drawer").waitFor({ state: "detached", timeout: 4000 }).catch(() => {});
    return { ok: drawer && key === "impact-scope", detail: `drawer=${drawer} detailKey=${key}（期望 impact-scope）` };
  } },
  { id: "trace-summary", phase: "P3", desc: "Trace 摘要(§9)：关联运行 + 打开 Langfuse（深色调试区）", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("trace-summary").waitFor({ state: "attached", timeout: 6000 }).catch(() => {});
    const ts = await has(page, "trace-summary");
    const lf = await has(page, "trace-open-langfuse");
    return { ok: ts && lf, detail: `Trace摘要=${ts} 打开Langfuse=${lf}` };
  } },
  { id: "merge-basis", phase: "P3", desc: "相似归并 §8.5：置信度 + 合并依据 + 标记合并不准", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("merge-basis").first().waitFor({ state: "attached", timeout: 6000 }).catch(() => {});
    const basis = await has(page, "merge-basis");
    const mark = await has(page, "mark-merge-inaccurate");
    return { ok: basis && mark, detail: `合并依据=${basis} 标记不准=${mark}` };
  } },
  { id: "status-filter", phase: "P3", desc: "改进列表状态过滤 pills(§5 待确认/处理中/待回归/已完成 + 全部)", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 }).catch(() => {});
    const sf = await has(page, "status-filter");
    let pills = 0;
    for (const k of ["status-filter-all", "status-filter-pending-confirm", "status-filter-in-progress", "status-filter-pending-regression", "status-filter-done"]) if (await has(page, k)) pills += 1;
    return { ok: sf && pills === 5, detail: `过滤区=${sf} pills=${pills}/5` };
  } },
  { id: "full-chain", phase: "P3", desc: "查看完整链路：4 阶段时间线 + 状态", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("full-chain").waitFor({ timeout: 6000 }).catch(() => {});
    const fc = await has(page, "full-chain");
    const steps = await page.getByTestId("full-chain-step").count();
    return { ok: fc && steps === 4, detail: `完整链路=${fc} 阶段数=${steps}` };
  } },
  { id: "detail-collapsed", phase: "P2", desc: "改进详情收纳：自动化/相似/链接进「高级」折叠，默认不在主区可见", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-advanced").waitFor({ timeout: 6000 }).catch(() => {});
    const advanced = await has(page, "improvement-advanced");
    const autoVisible = await visible(page, "automation-mode");
    return { ok: advanced && !autoVisible, detail: `高级折叠=${advanced} 自动化默认隐藏=${!autoVisible}` };
  } },
  { id: "source-feedback-table", phase: "P3", desc: "来源反馈表(§8.4 #/反馈摘要/来源/状态) 进入来源管理抽屉", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    const hiddenInline = await page.getByTestId("source-feedback-table").isVisible().catch(() => false);
    await page.getByTestId("view-all-feedbacks").click().catch(() => {});
    await page.getByTestId("source-management-drawer").waitFor({ timeout: 6000 }).catch(() => {});
    await page.getByTestId("source-feedback-table").waitFor({ timeout: 6000 }).catch(() => {});
    const drawer = await has(page, "source-management-drawer");
    const basis = await has(page, "source-merge-basis");
    const tbl = await has(page, "source-feedback-table");
    const rows = await page.getByTestId("source-feedback-row").count();
    await page.getByTestId("source-management-drawer").getByLabel("关闭").click().catch(() => {});
    return { ok: !hiddenInline && drawer && basis && tbl && rows >= 1, detail: `inlineHidden=${!hiddenInline} drawer=${drawer} basis=${basis} 表=${tbl} 行=${rows}` };
  } },
  { id: "optimization-execution", phase: "P3", desc: "优化方案(§106 方案正文+变更项) + 执行记录(§107) 内容子资源", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("optimization-plan").waitFor({ timeout: 6000 }).catch(() => {});
    const opt = await has(page, "optimization-plan");
    const optChanges = await has(page, "optimization-plan-changes");
    const exec = await has(page, "execution-record");
    return { ok: opt && optChanges && exec, detail: `方案=${opt} 变更项=${optChanges} 执行记录=${exec}` };
  } },
  { id: "regression-governor", phase: "P3", desc: "§11/§17.5 回归保障：治理 Agent 生成回归用例候选（来源徽标 + 生成入口）", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("regression-guarantee").waitFor({ timeout: 6000 }).catch(() => {});
    const gen = await has(page, "generate-regression");
    const sediment = await has(page, "sediment-assets");
    const src = await has(page, "regression-source");
    const srcVal = await page.getByTestId("regression-source").first().getAttribute("data-source").catch(() => "");
    const srcOk = !src || srcVal === "governor" || srcVal === "heuristic";
    return { ok: (gen || sediment) && srcOk, detail: `生成入口=${gen} 来源徽标=${src}(${srcVal}) 沉淀=${sediment}` };
  } },
  { id: "execution-version-binding", phase: "P3", desc: "§17.5 执行记录标治理 Agent 应用来源；governor 成功时绑定候选 Agent 版本/变更集", async fn(page) {
    // 执行来源徽标始终在；版本绑定仅在 governor 成功 apply 时出现（取决于 governor 判断/环境），不强制。
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("execution-record").waitFor({ timeout: 6000 }).catch(() => {});
    const src = await has(page, "execution-source");
    const srcVal = await page.getByTestId("execution-source").first().getAttribute("data-source").catch(() => "");
    const validSrc = srcVal === "governor" || srcVal === "heuristic";
    const binding = await has(page, "execution-version-binding");
    const bindingOk = srcVal === "governor" ? binding : true;
    return { ok: src && validSrc && bindingOk, detail: `执行来源徽标=${src}(${srcVal}) 版本绑定=${binding}` };
  } },
  { id: "governance-generation-source", phase: "P3", desc: "§17.5 归因/方案标注来源（治理 Agent 生成 vs 启发式初步）", async fn(page) {
    // 断言来源徽标存在且取值合法；governor/heuristic 取决于环境 LLM 可用性（代码两态都正确），不强制 governor。
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution-source").waitFor({ timeout: 6000 }).catch(() => {});
    const attrSrc = await has(page, "attribution-source");
    const src = await page.getByTestId("attribution-source").first().getAttribute("data-source").catch(() => "");
    const validSrc = src === "governor" || src === "heuristic";
    if (!(await openImprovementById(page, stageTarget("optimization", "imp-demo03")))) return { ok: false, detail: "无优化事项" };
    const optSrc = await has(page, "optimization-plan-source");
    return { ok: attrSrc && optSrc && validSrc, detail: `归因来源徽标=${attrSrc}(${src}) 方案来源徽标=${optSrc}` };
  } },
  { id: "attribution-actions", phase: "P3", desc: "归因支持 修改/重新整理(§6 [确认][修改][重新整理])", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("attribution", "imp-demo02")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution").waitFor({ timeout: 6000 }).catch(() => {});
    const edit = await has(page, "edit-attribution");
    const regen = await has(page, "regenerate-attribution");
    return { ok: edit && regen, detail: `修改=${edit} 重新整理=${regen}` };
  } },
  { id: "improvement-assets", phase: "P3", desc: "改进详情含回归保障候选(§11.1) + 本事项沉淀资产区(§11.2)", async fn(page) {
    if (!(await openImprovementById(page, stageTarget("testRelease", "imp-demo04")))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-detail").waitFor({ timeout: 6000 }).catch(() => {});
    // §11 能力存在的两种合法态：未采纳→候选卡(regression-guarantee+adopt)；已采纳→沉淀资产区(sediment-assets)。
    const rg = await has(page, "regression-guarantee");
    const adopt = await has(page, "adopt-regression");
    const sediment = await has(page, "sediment-assets");
    return { ok: (rg && adopt) || sediment, detail: `回归保障候选=${rg} 采纳=${adopt} 沉淀资产=${sediment}` };
  } },
  { id: "asset-browse-first", phase: "P1", desc: "资产 Registry 默认浏览/追溯优先，创建资产进入抽屉", async fn(page) {
    await page.getByTestId("nav-asset").click();
    await page.getByTestId("asset-registry").waitFor({ timeout: 8000 });
    const toolbar = await has(page, "asset-browser-toolbar");
    const typeFilter = await has(page, "asset-type-filter");
    const sourceFilter = await has(page, "asset-source-filter");
    const createButton = await has(page, "asset-create-open");
    const titleVisibleBefore = await visible(page, "asset-create-title");
    await page.getByTestId("asset-create-open").click();
    const drawer = await visible(page, "asset-create-drawer");
    const drawerSize = await page.getByTestId("asset-create-drawer").getAttribute("data-size").catch(() => "");
    await page.getByTestId("asset-create-drawer").getByLabel("关闭").click().catch(() => {});
    return { ok: toolbar && typeFilter && sourceFilter && createButton && !titleVisibleBefore && drawer && drawerSize === "narrow", detail: `toolbar=${toolbar} type=${typeFilter} source=${sourceFilter} createBtn=${createButton} titleBefore=${titleVisibleBefore} drawer=${drawer}/${drawerSize}` };
  } },
  { id: "theme-governance-light", phase: "P4", desc: "主工作台统一 Governance Light（主区背景非旧暖色，含背景渐变）", async fn(page) {
    await page.getByTestId("nav-playground").click();
    // 旧暖色：body 用暖色渐变(#fbf7f0/#f4eee5)+ topbar 等用 rgb(255,250,243)；需检查 backgroundImage（渐变在 image 而非 color）。
    const bg = await page.evaluate(() => {
      const s = getComputedStyle(document.body);
      const t = document.querySelector(".topbar");
      const ts = t ? getComputedStyle(t).backgroundColor : "";
      return `${s.backgroundImage} | ${s.backgroundColor} | topbar:${ts}`;
    });
    const warm = /251,\s*247,\s*240|244,\s*238,\s*229|255,\s*250,\s*243|246,\s*240,\s*230/.test(bg);
    return { ok: !warm, detail: `${bg.slice(0, 90)}（不应含暖色调）` };
  } },
];

async function main() {
  const server = REAL ? null : startVite();
  try {
    if (!REAL) await waitForVite();
    const seeded = REAL ? await seedRealAuditData() : { improvement_id: auditTargetId };
    auditTargetId = seeded.improvement_id || auditTargetId;
    if (seeded.targets) auditTargets = seeded.targets;
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
    await page.addInitScript(([base, key, real]) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: base, apiKey: key }));
      if (window.sessionStorage.getItem("parity-preserve-playground-session") === "1") return;

      const sessionId = real ? "real-container-parity-session" : "mock-session";
      const runId = real ? "run-trace-real-container" : "run-trace-mock";
      const agentVersionId = real ? "v-real-container-parity" : "v-mock";
      const playgroundMessages = Array.from({ length: 36 }, (_, index) => {
        const n = index + 1;
        const createdAt = new Date(Date.parse("2026-06-18T00:00:00Z") + index * 2000).toISOString();
        const completedAt = new Date(Date.parse("2026-06-18T00:00:00Z") + index * 2000 + 1000).toISOString();
        const answer = `我是 AgentGov 治理测试助手。第 ${n} 段回复用于构造可滚动的 Playground 长会话，验证自动置底、一键置底和消息预览导航。`.repeat(2);
        return [
          { id: `msg-user-${n}`, role: "user", content: `请用一句话说明你的治理职责，序号 ${n}。`, createdAt },
          {
            id: `msg-assistant-${n}`,
            role: "assistant",
            content: answer,
            createdAt: completedAt,
            runId: `${runId}-${n}`,
            sessionId,
            agentVersionId,
            events: [
              { id: `evt-${n}-1`, event: "message", text: "开始治理分析。", data: { text: "开始治理分析。" }, createdAt },
              { id: `evt-${n}-2`, event: "tool", data: { type: "tool_use", name: "Read", id: `tool-${n}`, input: { file_path: "CLAUDE.md" } }, createdAt: completedAt },
              { id: `evt-${n}-3`, event: "result", data: { run_id: `${runId}-${n}`, session_id: sessionId, agent_version_id: agentVersionId, agent_activity: { requested_skills: ["project-skill"], allowed_tools: ["Read"], disallowed_tools: [], tool_names: ["Read"], tool_calls: [{ name: "Read", tool_use_id: `tool-${n}` }], tool_results: [{ name: "Read", tool_use_id: `tool-${n}`, content: "ok" }], skill_calls: [] } }, createdAt: completedAt },
            ],
          },
        ];
      }).flat();
      window.localStorage.setItem("playground-active-session", JSON.stringify(sessionId));
      window.localStorage.setItem("playground-session-messages", JSON.stringify({
        [sessionId]: playgroundMessages,
      }));
    }, [apiBase, apiKey, REAL]);
    if (!REAL) {
      await page.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        if (url.hostname !== "runtime.test") return route.continue();
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: { "access-control-allow-origin": "*" },
          body: JSON.stringify(defaultPayload(url.pathname, {
            method: route.request().method(),
            postData: route.request().postData() || "",
          })),
        });
      });
    }
    const results = [];
    try {
      await page.goto(uiBase, { waitUntil: "domcontentloaded" });
      await page.getByTestId("topbar-agent-switcher").waitFor({ timeout: 20000 });
      for (const rule of RULES) {
        let r;
        console.error(`RULE_START ${rule.id}`);
        try { r = await rule.fn(page); } catch (e) { r = { ok: false, detail: `EXC: ${e instanceof Error ? e.message : e}` }; }
        console.error(`RULE_DONE ${rule.id} ${r.ok ? "ok" : "fail"}`);
        results.push({ id: rule.id, phase: rule.phase, ok: !!r.ok, desc: rule.desc, detail: r.detail });
      }
    } finally {
      await browser.close();
    }
    const passed = results.filter((r) => r.ok).length;
    const baselineFail = results.filter((r) => BASELINE_RULES.has(r.id) && !r.ok);
    console.log(JSON.stringify({ mode: REAL ? "real-container" : "mock", ui_base: uiBase, audit_target_id: auditTargetId, passed, total: results.length, baseline: [...BASELINE_RULES], baseline_fail: baselineFail.map((r) => r.id), rules: results }, null, 2));
    console.log(`\nDESIGN_PARITY ${passed}/${results.length} passed (${REAL ? "real-container" : "mock"}); baseline ${BASELINE_RULES.size - baselineFail.length}/${BASELINE_RULES.size} held`);
    // 门：基线规则必须全绿（防回归）；目标是把基线扩到 9/9。
    return baselineFail.length === 0 ? 0 : 1;
  } finally {
    await stopChild(server);
  }
}
main().then((code) => process.exit(code)).catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(2); });
