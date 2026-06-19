#!/usr/bin/env node
// v2.7 UI 设计一致性硬门（不是功能可用门）。
// 逐条断言 docs/AgentGov_ASCII_UI_草图方案_v2.7.md 的设计规则，输出 per-rule 记分卡。
// 每条规则标注由哪个整改阶段（P0..P4）转绿；未达标如实记 fail，禁止用"功能可用"冒充"设计一致"。
//
// 双模式：
//   - 默认：自己起 vite + mock 后端，验证「结构性」设计规则（确定性，可进 CI / coverage_policy）。
//   - 验收：设 RUNTIME_UI_BASE（如真实容器 http://127.0.0.1:55173）+ RUNTIME_API_BASE 直连真实 UI/API。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import process from "node:process";

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

// ---- mock 后端（仅默认模式）----
const AGENTS = [
  { agent_id: "soc-ops", name: "安全运营助手", category: "", workspace_dir: "/w/soc", created_at: ts, status: "active" },
  { agent_id: "shop-bot", name: "电商客服", category: "", workspace_dir: "/w/shop", created_at: ts, status: "active" },
];
const IMPROVEMENTS = [
  { improvement_id: "imp-demo01", agent_id: "soc-ops", title: "告警误报治理", summary: "事件时间不一致", source_feedback_refs: ["fb-1"], improvement_stage: "attribution", improvement_status: "active", created_at: ts, updated_at: ts },
];
function defaultPayload(path) {
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
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  if (/^\/api\/improvements\/[^/]+\/similar$/.test(path)) return [{ improvement: { ...IMPROVEMENTS[0], improvement_id: "imp-sim01", title: "告警误报治理(相似项)" }, score: 0.55 }];
  if (/^\/api\/improvements\/[^/]+\/links$/.test(path)) return [];
  if (/^\/api\/improvements\/[^/]+\/feedbacks$/.test(path)) return [{ feedback_id: "fb-1", improvement_id: "imp-demo01", agent_id: "soc-ops", summary: "这个告警其实是误报", source: "playground_run", status: "merged", raw_text: "", run_id: "run-1", session_id: "s-1", agent_version_id: "v1.2.0", scenario: "alert-triage", task_id: "task-1", alert_id: "alert-001", case_id: "case-001", created_at: ts }];
  if (/^\/api\/improvements\/[^/]+\/normalized-feedback$/.test(path)) return { normalized_feedback_id: "nf-1", improvement_id: "imp-demo01", problem: "告警误报", possible_reason: "事件时间与告警时间不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "进入改进处理", user_quote: "这个告警其实是误报", status: "draft", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/attribution$/.test(path)) return { attribution_id: "attr-1", improvement_id: "imp-demo01", summary: "MCP 数据时间不一致导致误判", responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"], evidence: ["list_events 返回的数据时间与告警时间窗口不一致"], status: "draft", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/optimization-plan$/.test(path)) return { optimization_plan_id: "opt-1", improvement_id: "imp-demo01", summary: "针对告警误报：补充时间一致性校验", changes: [{ target: "prompt", change: "新增事件时间与告警时间一致性校验指令" }], status: "confirmed", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/execution$/.test(path)) return { execution_id: "exec-1", improvement_id: "imp-demo01", summary: "已应用变更并生成版本", changes_applied: ["prompt：新增时间校验"], agent_version: "v1.2.0", status: "draft", created_at: ts, updated_at: ts };
  if (/^\/api\/automation-policy/.test(path)) return { agent_id: "soc-ops", mode: "off" };
  if (/^\/api\/improvements\/[^/]+$/.test(path)) return IMPROVEMENTS[0];
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
// 随 P1..P4 推进，把对应规则 id 加入此基线；真实容器验收用 RUNTIME_UI_BASE，目标是 9/9。
const BASELINE_RULES = new Set(
  (process.env.PARITY_BASELINE || "nav-converged,settings-ia,playground-clean,playground-config-drawer,message-actions,trace-drawer,drawer-size-policy,feedback-drawer-2phase,context-4types,release-gates,release-gate-workbench,theme-governance-light,improvement-default-detail,closed-loop-spine,improvement-content,improvement-assets,asset-browse-first,legacy-diagnostic-downgraded,attribution-actions,source-feedback-table,detail-collapsed,full-chain,status-filter,merge-basis,trace-summary,optimization-execution").split(",").map((s) => s.trim()).filter(Boolean),
);

const has = async (page, testid) => (await page.getByTestId(testid).count()) > 0;
const visible = async (page, testid) => (await page.getByTestId(testid).count()) > 0 && (await page.getByTestId(testid).first().isVisible());

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

async function seedRealAuditData() {
  const agents = await apiJson("/api/agent-registry").catch(() => []);
  const agentId = agents.find((a) => a.status === "active")?.agent_id || agents[0]?.agent_id || "main-agent";
  const stamp = `audit-v27-${Date.now().toString(36)}`;
  const target = await postJson("/api/improvements", {
    agent_id: agentId,
    title: `${stamp} sec-ops-data 时间窗口误判治理`,
    summary: "sec-ops-data MCP 数据时间窗口与告警时间不一致，导致 Agent 将误报判断为真实横向移动。",
    source_feedback_refs: [`${stamp}-fb-1`, `${stamp}-fb-2`],
    auto_merge: false,
  });
  await postJson("/api/improvements", {
    agent_id: agentId,
    title: `${stamp} sec-ops-data 时间窗口误判重复反馈`,
    summary: "sec-ops-data 返回事件时间窗口和告警时间不一致，需要归并到同一改进事项。",
    source_feedback_refs: [`${stamp}-fb-2`, `${stamp}-fb-3`],
    auto_merge: false,
  });
  await postJson(`/api/improvements/${target.improvement_id}/feedbacks`, {
    summary: "这个横向移动告警其实是误报",
    source: "playground_run",
    raw_text: "Agent 没注意到事件时间和告警时间不一致，导致误报升级。",
    run_id: `${stamp}-run-1`,
    session_id: `${stamp}-session-1`,
    agent_version_id: `${stamp}-agent-version`,
    scenario: "alert-triage",
    task_id: `${stamp}-task-1`,
    alert_id: `${stamp}-alert`,
    case_id: `${stamp}-case`,
  });
  await postJson(`/api/improvements/${target.improvement_id}/feedbacks`, {
    summary: "sec-ops-data 返回的数据像是模拟数据",
    source: "trace",
    raw_text: "list_events 返回的数据时间窗口无法支撑当前告警判断。",
    run_id: `${stamp}-run-2`,
    session_id: `${stamp}-session-2`,
    agent_version_id: `${stamp}-agent-version`,
    scenario: "alert-triage",
    task_id: `${stamp}-task-2`,
    alert_id: `${stamp}-alert`,
    case_id: `${stamp}-case`,
  });
  await putJson(`/api/improvements/${target.improvement_id}/normalized-feedback`, {
    problem: "告警误报治理",
    possible_reason: "事件时间与告警时间窗口不一致",
    possible_object: "sec-ops-data MCP 数据",
    impact: "中",
    suggestion: "进入归因和回归保障",
    user_quote: "这个横向移动告警其实是误报。",
  });
  await putJson(`/api/improvements/${target.improvement_id}/attribution`, {
    summary: "sec-ops-data MCP 返回的数据时间与告警时间窗口不一致，导致 Agent 误判。",
    responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"],
    evidence: ["list_events 返回的数据时间与告警时间窗口不一致", "来源反馈均指向时间窗口核验缺失"],
  });
  await putJson(`/api/improvements/${target.improvement_id}/optimization-plan`, {
    summary: "补充 sec-ops-data 时间窗口核验 SOP，并在 prompt 中要求先核验事件时间。",
    changes: [{ target: "prompt", change: "新增事件时间与告警时间一致性校验指令" }],
  });
  await putJson(`/api/improvements/${target.improvement_id}/execution`, {
    summary: "已按优化方案形成候选执行记录，关联审计版本。",
    changes_applied: ["prompt：新增时间窗口一致性校验", "SOP：补充 MCP 数据时间核验步骤"],
    agent_version: `${stamp}-agent-version`,
  });
  await postJson("/api/assets", {
    agent_id: agentId,
    asset_type: "regression",
    title: `${stamp} 回归保障：时间窗口不一致不得误判`,
    body: "当告警时间与事件时间窗口不一致时，Agent 应提示核验数据源，不得直接升级处置。",
    source_improvement_id: target.improvement_id,
  });
  await postJson("/api/assets", {
    agent_id: agentId,
    asset_type: "methodology",
    title: `${stamp} MCP 数据时间窗口核验 SOP`,
    body: "先比对告警时间、事件时间、查询窗口和数据源时间戳，再下结论。",
    source_improvement_id: target.improvement_id,
  });
  const changeSet = await postJson("/api/agent-change-sets", {
    title: `${stamp} 告警误报治理候选变更`,
    note: "v2.7 真实容器 UI 设计一致性验收种子：仅用于发布门禁结构检查，不执行强制发布。",
  }).catch(() => null);
  await postJson(`/api/improvements/${target.improvement_id}/links`, { kind: "change_set", ref_id: changeSet?.change_set_id || `${stamp}-change-set` }).catch(() => null);
  return { improvement_id: target.improvement_id, agent_id: agentId, stamp };
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

const RULES = [
  { id: "nav-converged", phase: "P0", desc: "一级导航只含 Playground/改进/发布；资产与反馈优化不作为顶级主导航", async fn(page) {
    const nav = await page.locator(".topbar-nav .topbar-nav-button").count();
    const asset = await has(page, "nav-asset");
    const feedbackTopNav = await page.getByRole("button", { name: "反馈优化", exact: true }).count();
    return { ok: nav === 3 && !asset && feedbackTopNav === 0, detail: `topbar-nav=${nav} nav-asset=${asset} 反馈优化顶级=${feedbackTopNav}（期望 3/false/0）` };
  } },
  { id: "settings-ia", phase: "P0", desc: "Settings 含 业务Agent管理/自动化策略/资产Registry/Developer 区块", async fn(page) {
    await (page.getByTestId("open-settings").click().catch(() => page.getByRole("button", { name: "设置" }).first().click()));
    await page.getByTestId("settings-panel").waitFor({ timeout: 8000 });
    const tabs = ["agents", "automation", "assets", "developer"];
    const found = [];
    for (const tab of tabs) {
      await page.getByTestId(`settings-tab-${tab}`).click();
      const section = tab === "agents" ? "settings-section-agents" : tab === "assets" ? "settings-section-assets" : `settings-section-${tab}`;
      if (await has(page, section)) found.push(section);
    }
    const tabsVisible = await has(page, "settings-tabs");
    // 关闭设置弹窗，避免 modal-backdrop 拦截后续规则的点击。
    await page.getByTestId("settings-panel").getByRole("button", { name: "关闭" }).click().catch(() => {});
    await page.getByTestId("settings-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    return { ok: tabsVisible && found.length === tabs.length, detail: `${found.length}/${tabs.length}: ${found.join(",")} tabs=${tabsVisible}` };
  } },
  { id: "playground-clean", phase: "P1", desc: "Playground 主区无旧 Subagent/Sessions/Skills 侧栏、无 Inspector、无常显 control-strip", async fn(page) {
    await page.getByTestId("nav-playground").click(); await page.waitForTimeout(400);
    const legacySidebar = await page.locator(".sidebar .panel-section").count();
    const inspector = await page.locator(".inspector").count();
    const controlStrip = await page.locator(".control-strip").count();
    return { ok: legacySidebar === 0 && inspector === 0 && controlStrip === 0, detail: `legacy-sidebar=${legacySidebar} inspector=${inspector} control-strip=${controlStrip}（期望全 0）` };
  } },
  { id: "playground-config-drawer", phase: "P1", desc: "Playground 运行配置进入「配置」抽屉", async fn(page) {
    await page.getByTestId("nav-playground").click();
    return { ok: await has(page, "playground-config-trigger"), detail: `playground-config-trigger=${await has(page, "playground-config-trigger")}` };
  } },
  { id: "message-actions", phase: "P1", desc: "助手回复动作含 创建反馈/查看Trace/获取上下文（领域级 data-testid）", async fn(page) {
    await page.getByTestId("nav-playground").click();
    const create = await has(page, "message-action-create-feedback");
    const trace = await has(page, "message-action-view-trace");
    const ctx = await has(page, "message-action-get-context");
    return { ok: create && trace && ctx, detail: `create=${create} trace=${trace} get-context=${ctx}` };
  } },
  { id: "trace-drawer", phase: "P0", desc: "查看 Trace 打开右侧 Trace 抽屉，旧中心 detail modal 不再出现", async fn(page) {
    await page.getByTestId("nav-playground").click();
    if (!(await has(page, "message-action-view-trace"))) return { ok: false, detail: "无 Trace 入口" };
    await page.getByTestId("message-action-view-trace").first().click();
    await page.getByTestId("trace-drawer").waitFor({ timeout: 8000 });
    const drawer = await visible(page, "trace-drawer");
    const legacy = await page.locator(".detail-modal-card").isVisible().catch(() => false);
    const size = await page.getByTestId("trace-drawer").getAttribute("data-size");
    const langfuse = await has(page, "trace-open-langfuse");
    await page.getByTestId("trace-drawer").getByLabel("关闭").click();
    await page.getByTestId("trace-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    return { ok: drawer && !legacy && (size === "medium" || size === "wide") && langfuse, detail: `drawer=${drawer} legacyModal=${legacy} size=${size} langfuse=${langfuse}` };
  } },
  { id: "drawer-size-policy", phase: "P0", desc: "抽屉宽度按 narrow/medium/wide 分档且打开后稳定", async fn(page) {
    await page.getByTestId("nav-playground").click();
    await page.getByTestId("message-action-view-trace").first().click();
    await page.getByTestId("trace-drawer").waitFor({ timeout: 8000 });
    const traceSize = await page.getByTestId("trace-drawer").getAttribute("data-size");
    const traceWidth = (await page.getByTestId("trace-drawer").boundingBox())?.width || 0;
    await page.getByTestId("trace-drawer").getByLabel("关闭").click();
    await page.getByTestId("trace-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("message-action-create-feedback").first().click();
    await page.getByTestId("feedback-drawer").waitFor({ timeout: 8000 });
    const feedbackSize = await page.getByTestId("feedback-drawer").getAttribute("data-size");
    const feedbackWidth = (await page.getByTestId("feedback-drawer").boundingBox())?.width || 0;
    await page.getByTestId("feedback-drawer").getByLabel("关闭").click();
    await page.getByTestId("feedback-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("playground-config-trigger").click();
    await page.getByTestId("playground-config-drawer").waitFor({ timeout: 8000 });
    const configSize = await page.getByTestId("playground-config-drawer").getAttribute("data-size");
    const configWidth = (await page.getByTestId("playground-config-drawer").boundingBox())?.width || 0;
    await page.getByTestId("playground-config-drawer").getByLabel("关闭").click();
    await page.getByTestId("playground-config-drawer").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    const ok = (traceSize === "medium" || traceSize === "wide") && traceWidth >= 650 && feedbackSize === "narrow" && feedbackWidth >= 430 && configSize === "wide" && configWidth >= 860;
    return { ok, detail: `trace=${traceSize}/${Math.round(traceWidth)} feedback=${feedbackSize}/${Math.round(feedbackWidth)} config=${configSize}/${Math.round(configWidth)}` };
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
    const types = ["context-type-problem", "context-type-ai", "context-type-playwright", "context-type-json"];
    const found = []; for (const t of types) if (await has(page, t)) found.push(t);
    await page.getByTestId("context-type-json").click().catch(() => {});
    const preview = await page.getByTestId("context-preview").innerText().catch(() => "");
    const rich = preview.includes('"attribution_id"')
      && preview.includes('"agent_version_id"')
      && preview.includes('"optimization_plan_id"')
      && preview.includes('"asset_id"')
      && !preview.includes('"attribution": null')
      && !preview.includes('"evidence": []');
    return { ok: found.length === 4 && (await has(page, "context-download")) && rich, detail: `类型 ${found.length}/4，下载=${await has(page, "context-download")}，证据链JSON=${rich}` };
  } },
  { id: "release-gates", phase: "P2", desc: "发布页三门门禁 + 去运行回归/查看变更/强制发布动作", async fn(page) {
    await page.getByTestId("nav-release").click();
    await page.getByTestId("release-workbench").waitFor({ timeout: 8000 });
    const gates = ["release-gate-attribution", "release-gate-optimization", "release-gate-regression"];
    const gfound = []; for (const g of gates) if (await has(page, g)) gfound.push(g);
    const actions = ["release-action-run-regression", "release-action-view-changes", "release-action-force"];
    const afound = []; for (const a of actions) if (await has(page, a)) afound.push(a);
    const runEnabled = await page.getByTestId("release-action-run-regression").isEnabled().catch(() => false);
    const forceEnabled = await page.getByTestId("release-action-force").isEnabled().catch(() => false);
    if (!REAL && runEnabled) await page.getByTestId("release-action-run-regression").click().catch(() => {});
    if (!REAL) await page.getByText("已运行回归").waitFor({ timeout: 5000 }).catch(() => {});
    if (!REAL && forceEnabled) {
      await page.getByTestId("release-action-force").click().catch(() => {});
      await page.getByTestId("release-force-confirm").waitFor({ timeout: 5000 }).catch(() => {});
      await page.getByTestId("release-force-confirm-submit").click().catch(() => {});
    }
    if (!REAL) await page.getByText("已强制发布").waitFor({ timeout: 5000 }).catch(() => {});
    const actionResult = await has(page, "release-action-message");
    const ok = gfound.length === 3 && afound.length === 3 && (REAL || (runEnabled && forceEnabled && actionResult));
    return { ok, detail: `门禁 ${gfound.length}/3，动作 ${afound.length}/3，回归可执行=${runEnabled}，强制可执行=${forceEnabled}，结果=${actionResult}，执行动作=${!REAL}` };
  } },
  { id: "release-gate-workbench", phase: "P1", desc: "发布页是门禁台：候选列表 + 门禁详情 + 可展开 diff 摘要", async fn(page) {
    await page.getByTestId("nav-release").click();
    await page.getByTestId("release-workbench").waitFor({ timeout: 8000 });
    const gateWorkbench = await has(page, "release-gate-workbench");
    const details = await has(page, "release-changeset-details");
    const candidates = await page.getByTestId("release-changeset-item").count();
    await page.getByTestId("release-action-view-changes").click().catch(() => {});
    let diff = await has(page, "release-diff-summary");
    if (!diff) {
      await page.getByTestId("release-action-view-changes").click().catch(() => {});
      diff = await has(page, "release-diff-summary");
    }
    return { ok: gateWorkbench && details && candidates >= 1 && diff, detail: `门禁台=${gateWorkbench} 详情=${details} 候选=${candidates} diff=${diff}` };
  } },
  { id: "improvement-default-detail", phase: "P1", desc: "改进列表有数据时默认展示首个详情，不留空白首屏", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 });
    await page.getByTestId("improvement-detail").waitFor({ timeout: 8000 }).catch(() => {});
    const detail = await has(page, "improvement-detail");
    const emptyVisible = await page.locator(".iw-detail-panel .iw-empty").isVisible().catch(() => false);
    return { ok: detail && !emptyVisible, detail: `detail=${detail} emptyVisible=${emptyVisible}` };
  } },
  { id: "closed-loop-spine", phase: "P1", desc: "改进详情始终显示压缩闭环 spine：反馈到资产", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("closed-loop-spine").waitFor({ timeout: 6000 }).catch(() => {});
    const spine = await has(page, "closed-loop-spine");
    const steps = await page.getByTestId("closed-loop-step").count();
    return { ok: spine && steps === 8, detail: `spine=${spine} steps=${steps}` };
  } },
  { id: "improvement-content", phase: "P3", desc: "改进详情含系统理解(NormalizedFeedback) + 归因(Attribution 正文/责任边界/证据)", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("normalized-feedback").waitFor({ timeout: 6000 }).catch(() => {});
    const nf = await has(page, "normalized-feedback");
    const attr = await has(page, "attribution");
    const ev = await has(page, "attribution-evidence");
    return { ok: nf && attr && ev, detail: `系统理解=${nf} 归因=${attr} 证据=${ev}` };
  } },
  { id: "trace-summary", phase: "P3", desc: "Trace 摘要(§9)：关联运行 + 打开 Langfuse（深色调试区）", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
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
  { id: "full-chain", phase: "P3", desc: "查看完整链路：7 阶段时间线 + 状态(§7)", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("full-chain").waitFor({ timeout: 6000 }).catch(() => {});
    const fc = await has(page, "full-chain");
    const steps = await page.getByTestId("full-chain-step").count();
    return { ok: fc && steps === 7, detail: `完整链路=${fc} 阶段数=${steps}` };
  } },
  { id: "detail-collapsed", phase: "P2", desc: "改进详情收纳：自动化/相似/链接进「高级」折叠，默认不在主区可见", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-advanced").waitFor({ timeout: 6000 }).catch(() => {});
    const advanced = await has(page, "improvement-advanced");
    const autoVisible = await visible(page, "automation-mode");
    return { ok: advanced && !autoVisible, detail: `高级折叠=${advanced} 自动化默认隐藏=${!autoVisible}` };
  } },
  { id: "source-feedback-table", phase: "P3", desc: "来源反馈表(§8.4 #/反馈摘要/来源/状态)", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("source-feedback-table").waitFor({ timeout: 6000 }).catch(() => {});
    const tbl = await has(page, "source-feedback-table");
    const rows = await page.getByTestId("source-feedback-row").count();
    return { ok: tbl && rows >= 1, detail: `表=${tbl} 行=${rows}` };
  } },
  { id: "optimization-execution", phase: "P3", desc: "优化方案(§106 方案正文+变更项) + 执行记录(§107) 内容子资源", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("optimization-plan").waitFor({ timeout: 6000 }).catch(() => {});
    const opt = await has(page, "optimization-plan");
    const optChanges = await has(page, "optimization-plan-changes");
    const exec = await has(page, "execution-record");
    return { ok: opt && optChanges && exec, detail: `方案=${opt} 变更项=${optChanges} 执行记录=${exec}` };
  } },
  { id: "attribution-actions", phase: "P3", desc: "归因支持 修改/重新整理(§6 [确认][修改][重新整理])", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("attribution").waitFor({ timeout: 6000 }).catch(() => {});
    const edit = await has(page, "edit-attribution");
    const regen = await has(page, "regenerate-attribution");
    return { ok: edit && regen, detail: `修改=${edit} 重新整理=${regen}` };
  } },
  { id: "improvement-assets", phase: "P3", desc: "改进详情含回归保障候选(§11.1) + 本事项沉淀资产区(§11.2)", async fn(page) {
    if (!(await openAuditImprovement(page))) return { ok: false, detail: "无改进事项" };
    await page.getByTestId("improvement-detail").waitFor({ timeout: 6000 }).catch(() => {});
    // §11 能力存在的两种合法态：未采纳→候选卡(regression-guarantee+adopt)；已采纳→沉淀资产区(sediment-assets)。
    const rg = await has(page, "regression-guarantee");
    const adopt = await has(page, "adopt-regression");
    const sediment = await has(page, "sediment-assets");
    return { ok: (rg && adopt) || sediment, detail: `回归保障候选=${rg} 采纳=${adopt} 沉淀资产=${sediment}` };
  } },
  { id: "asset-browse-first", phase: "P1", desc: "资产 Registry 默认浏览/追溯优先，创建资产进入抽屉", async fn(page) {
    await (page.getByTestId("open-settings").click().catch(() => page.getByRole("button", { name: "设置" }).first().click()));
    await page.getByTestId("settings-panel").waitFor({ timeout: 8000 });
    await page.getByTestId("settings-tab-assets").click();
    await page.getByTestId("settings-open-asset").click();
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
  { id: "legacy-diagnostic-downgraded", phase: "P2", desc: "旧反馈优化工作台降级为开发者诊断，并提供返回改进主流程入口", async fn(page) {
    await page.getByTestId("nav-playground").click().catch(() => {});
    await (page.getByTestId("open-settings").click().catch(() => page.getByRole("button", { name: "设置" }).first().click()));
    await page.getByTestId("settings-panel").waitFor({ timeout: 8000 });
    await page.getByTestId("settings-tab-developer").click();
    await page.getByTestId("settings-open-feedback").click();
    await page.getByTestId("settings-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    await page.getByTestId("feedback-legacy-banner").waitFor({ timeout: 8000 });
    const bannerText = await page.getByTestId("feedback-legacy-banner").innerText().catch(() => "");
    const ret = await has(page, "legacy-return-improvement");
    if (ret) await page.getByTestId("legacy-return-improvement").click().catch(() => {});
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 }).catch(() => {});
    const improvement = await visible(page, "improvement-workbench");
    return { ok: bannerText.includes("开发者诊断视图") && ret && improvement, detail: `banner=${bannerText.slice(0, 30)} return=${ret} improvement=${improvement}` };
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
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
    await page.addInitScript(([base, key, real]) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: base, apiKey: key }));
      const sessionId = real ? "real-container-parity-session" : "mock-session";
      const runId = real ? "run-trace-real-container" : "run-trace-mock";
      const agentVersionId = real ? "v-real-container-parity" : "v-mock";
      window.localStorage.setItem("playground-active-session", JSON.stringify(sessionId));
      window.localStorage.setItem("playground-session-messages", JSON.stringify({
        [sessionId]: [
          { id: "msg-user", role: "user", content: "请用一句话说明你的治理职责。", createdAt: "2026-06-18T00:00:00Z" },
          {
            id: "msg-assistant",
            role: "assistant",
            content: "我是 AgentGov 治理测试助手。",
            createdAt: "2026-06-18T00:00:01Z",
            runId,
            sessionId,
            agentVersionId,
            events: [
              { id: "evt-1", event: "message", text: "开始治理分析。", data: { text: "开始治理分析。" }, createdAt: "2026-06-18T00:00:01Z" },
              { id: "evt-2", event: "tool", data: { type: "tool_use", name: "Read", id: "tool-1", input: { file_path: "CLAUDE.md" } }, createdAt: "2026-06-18T00:00:02Z" },
              { id: "evt-3", event: "result", data: { run_id: runId, session_id: sessionId, agent_version_id: agentVersionId, agent_activity: { requested_skills: ["project-skill"], allowed_tools: ["Read"], disallowed_tools: [], tool_names: ["Read"], tool_calls: [{ name: "Read", tool_use_id: "tool-1" }], tool_results: [{ name: "Read", tool_use_id: "tool-1", content: "ok" }], skill_calls: [] } }, createdAt: "2026-06-18T00:00:03Z" },
            ],
          },
        ],
      }));
    }, [apiBase, apiKey, REAL]);
    if (!REAL) {
      await page.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        if (url.hostname !== "runtime.test") return route.continue();
        return route.fulfill({ status: 200, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(defaultPayload(url.pathname)) });
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
