#!/usr/bin/env node
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.env.FEEDBACK_UI_STATE_PORT || "55174");
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const batchTitle = "UI 状态验证批次";

function json(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

function sse(route, events, status = 200) {
  return route.fulfill({
    status,
    contentType: "text/event-stream; charset=utf-8",
    body: events.map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(""),
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function timestamp() {
  return "2026-06-05T00:00:00Z";
}

function agentJob(state) {
  const failed = state === "failed";
  const timedOut = state === "timeout";
  const active = state === "queued" || state === "running";
  return {
    job_id: "fbp-ui-state",
    job_type: "batch_plan",
    profile_name: "proposal-generator",
    status: failed ? "failed" : timedOut ? "timeout" : active ? state : "completed",
    created_at: timestamp(),
    started_at: timestamp(),
    completed_at: active ? null : timestamp(),
    timeout_seconds: 300,
    error_json: failed ? planError() : timedOut ? planTimeoutError() : null,
    raw_output_json: failed ? { raw_text: "formatter failed", _formatter: { status: "failed", name: "dspy" } } : null,
  };
}

function planError() {
  return {
    error_code: "AGENT_RUNTIME_ERROR",
    message: "DSPy output formatter failed for batch_plan: tasks.0.execution_kind Field required",
    validation_errors: [
      {
        loc: ["tasks", 0, "execution_kind"],
        msg: "Field required",
        type: "missing",
      },
    ],
  };
}

function planTimeoutError() {
  return {
    error_code: "AGENT_TIMEOUT",
    message: "Agent job exceeded timeout_seconds=300",
  };
}

function attributionJob() {
  return {
    job_id: "fba-ui-state",
    job_type: "attribution",
    status: "completed",
    profile_name: "default",
    created_at: timestamp(),
    completed_at: timestamp(),
    error_json: null,
    validated_output_json: {
      status: "completed",
      confidence: "high",
      problem_type: "tool_misuse",
      optimization_object_type: "main_agent_claude_md",
      actionability: "direct_workspace_change",
      rationale: "Agent answered from memory instead of checking workspace configuration.",
      recommended_next_step: "generate_proposal",
      responsibility_boundary: { owner: "main_agent_claude_md", reason: "Instruction missing." },
      evidence_refs: [],
    },
  };
}

function optimizationPlan(edit = {}) {
  const taskTitle = edit.title || "补充工作区配置核查指令";
  const taskDescription = edit.description || "在回答工作区配置类问题前读取当前配置文件。";
  return {
    optimization_plan_id: "fop-ui-state",
    status: "pending_execution",
    generated_by: "proposal-generator",
    title: "补充工作区配置核查指令",
    recommendation: "在回答工作区配置类问题前读取当前配置文件。",
    expected_effect: "同类反馈不再复现。",
    validation: "运行批次回归测试验证回答包含当前配置事实。",
    risk: "回答耗时略有增加。",
    rationale: "归因结果显示主 Agent 未读取当前配置。",
    feedback_case_ids: ["fbc-ui-state"],
    eval_case_ids: [],
    attribution_job_ids: ["fba-ui-state"],
    attribution_summaries: [],
    blocked_items: [],
    tasks: [
      {
        plan_task_id: "fopt-ui-state",
        status: "pending_execution",
        execution_kind: "workspace_execution",
        actionability: "direct_workspace_change",
        target_type: "main_agent_claude_md",
        target_path: "CLAUDE.md",
        title: taskTitle,
        description: taskDescription,
        objective: "要求回答配置类问题前读取当前配置。",
        recommendation: "向 CLAUDE.md 增加配置核查要求。",
        expected_effect: "减少基于记忆回答导致的信息缺失。",
        validation: "运行批次回归测试。",
        risk: "无明显风险。",
        task_context: { target_file: "CLAUDE.md" },
        evidence_refs: [],
      },
    ],
  };
}

function executionRun(status = "completed") {
  const rolledBack = status === "rolled_back";
  return {
    schema_version: "feedback-batch-execution-run/v1",
    execution_run_id: "fbx-ui-state",
    batch_id: "fob-ui-state",
    created_at: timestamp(),
    started_at: timestamp(),
    completed_at: timestamp(),
    status,
    force: true,
    pre_execution_agent_version_id: "agent-before-ui",
    pre_execution_agent_version: { agent_version_id: "agent-before-ui", created_at: timestamp(), reason: "batch_execution_base" },
    applied_agent_version_id: "agent-after-ui",
    applied_agent_version: { agent_version_id: "agent-after-ui", created_at: timestamp(), reason: "batch_candidate_change_set" },
    applied_diff: {
      from_version_id: "agent-before-ui",
      to_version_id: "agent-after-ui",
      added: [],
      modified: [{ path: "CLAUDE.md", before: {}, after: {} }],
      deleted: [],
      unchanged_count: 0,
    },
    change_set_id: "agc-ui-state",
    candidate_commit_sha: "agent-after-ui",
    task_results: [
      {
        plan_task_id: "fopt-ui-state",
        execution_kind: "workspace_execution",
        status: "completed",
        started_at: timestamp(),
        completed_at: timestamp(),
        optimization_task_id: "opt-ui-state",
        execution_job_id: "fbe-ui-state",
        applied_agent_version_id: "agent-after-ui",
      },
    ],
    warnings: [],
    rollback_result: rolledBack
      ? {
          restored_at: timestamp(),
          status: "restored",
          target_agent_version_id: "agent-before-ui",
          restore_result: { current_version: { agent_version_id: "agent-before-ui" } },
        }
      : null,
  };
}

function repositoryStatus(dirty = false) {
  return {
    schema_version: "agent-repository-status/v1",
    provider: "local",
    repository_name: "main-agent-config",
    repository_dir: "/main-workspace",
    worktrees_dir: "/data/agent-git-worktrees",
    releases_dir: "/data/agent-releases",
    status: "active",
    degraded_reason: null,
    current_commit_sha: "agent-before-ui",
    current_branch: "main",
    dirty,
    changed_file_count: dirty ? 1 : 0,
    changed_files: dirty
      ? [
          {
            path: ".mcp.json",
            status: "modified",
            staged: false,
            unstaged: true,
            untracked: false,
            discardable: true,
          },
        ]
      : [],
    file_diffs: dirty
      ? [
          {
            path: ".mcp.json",
            status: "modified",
            unified_diff: '--- HEAD:.mcp.json\n+++ workspace:.mcp.json\n@@\n-{}\n+{"mcpServers":{"sec-ops":{}}}\n',
            is_text: true,
            truncated: false,
            reason: null,
          },
        ]
      : [],
    maintenance_active: false,
  };
}

function batchForState(state, edit = {}) {
  const failed = state === "failed";
  const timedOut = state === "timeout";
  const activePlanJob = state === "queued" || state === "running";
  const pendingExecution = state === "pending_execution_unapplied";
  const success = state === "success" || state === "executed" || state === "rolled_back" || pendingExecution;
  const run = state === "executed" ? executionRun("completed") : state === "rolled_back" ? executionRun("rolled_back") : null;
  const plan = success ? optimizationPlan(edit) : null;
  if (state === "executed" && plan?.tasks?.[0]) {
    plan.tasks[0].status = "applied_pending_regression";
    plan.tasks[0].applied_agent_version_id = "agent-after-ui";
    plan.tasks[0].optimization_task_id = "opt-ui-state";
    plan.tasks[0].execution_job_id = "fbe-ui-state";
  }
  if (pendingExecution && plan?.tasks?.[0]) {
    plan.tasks[0].optimization_task_id = "opt-ui-state";
  }
  return {
    batch_id: "fob-ui-state",
    title: batchTitle,
    status: state === "executed" ? "applied_pending_regression" : state === "rolled_back" || pendingExecution || success ? "pending_execution" : failed || timedOut ? "needs_human_review" : activePlanJob ? "optimization_plan_queued" : "attribution_completed",
    priority: "medium",
    source_refs: [{ source_kind: "signal", source_id: "fbs-ui-state" }],
    feedback_case_ids: ["fbc-ui-state"],
    eval_case_ids: [],
    attribution_job_ids: ["fba-ui-state"],
    attribution_jobs: [attributionJob()],
    attribution_summary: { total: 1, completed: 1, needs_review_or_failed: 0 },
    optimization_plan_job_id: state === "empty" ? null : "fbp-ui-state",
    optimization_plan_job: state === "empty" ? null : agentJob(state),
    optimization_plan_error: failed ? planError() : timedOut ? planTimeoutError() : null,
    optimization_plan: plan,
    optimization_task: pendingExecution ? { optimization_task_id: "opt-ui-state", status: "pending_execution" } : null,
    optimization_task_id: pendingExecution ? "opt-ui-state" : null,
    optimization_task_ids: pendingExecution ? ["opt-ui-state"] : [],
    execution_job: null,
    latest_execution_run: run,
    execution_runs: run ? [run] : [],
    created_at: timestamp(),
    updated_at: timestamp(),
  };
}

function apiPayload(path, stateRef) {
  const state = stateRef.value;
  if (path === "/health") return { status: "ok", model: "ui-state-mock" };
  if (path === "/api/sessions") return [];
  if (path === "/api/agents") return [];
  if (path === "/api/agent-registry") return stateRef.businessAgents || [];
  if (path === "/api/skills") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return repositoryStatus(Boolean(stateRef.repositoryDirty));
  if (path === "/api/agent-repository/current") return { agent_version_id: "agent-before-ui", commit_sha: "agent-before-ui", created_at: timestamp(), reason: "current" };
  if (path === "/api/agent-change-sets") return [];
  if (path === "/api/agent-releases") return [];
  if (path === "/api/feedback-sources") return [];
  if (path === "/api/agent-runs") return [];
  if (path === "/api/feedback-signals") return [];
  if (path === "/api/soc-events") return [];
  if (path === "/api/pending-correlations") return [];
  if (path === "/api/feedback-cases") return [];
  if (path === "/api/optimization-tasks") return [];
  if (path === "/api/external-governance-items") return [];
  if (path === "/api/external-governance-webhooks") return [];
  if (path === "/api/eval-cases") return [];
  if (path === "/api/eval-runs") return [];
  if (path === "/api/feedback-optimization-batches") return [batchForState(state, stateRef.edit || {})];
  if (path === "/api/agent-jobs/fbp-ui-state") return agentJob(state);
  return {};
}

function startVite() {
  const child = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "pipe"],
    // detached：让子进程自成进程组组长，停止时可一次性回收 pnpm + vite + esbuild 整个组，
    // 避免 vite 孙进程残留占用端口、并使 node 事件循环挂住直到 timeout。
    detached: true,
  });
  child.stdout.on("data", (chunk) => process.stdout.write(chunk));
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));
  return child;
}

async function waitForVite() {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(uiBase);
      if (response.ok) return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw new Error(`Vite did not become ready at ${uiBase}`);
}

function killProcessTree(child, signal) {
  try {
    // 负号 PID：向整个进程组（detached 组长 pnpm + vite + esbuild）发信号。
    process.kill(-child.pid, signal);
  } catch {
    try {
      child.kill(signal);
    } catch {
      // 进程已退出，忽略。
    }
  }
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  killProcessTree(child, "SIGTERM");
  await new Promise((resolve) => {
    const timeout = setTimeout(() => {
      killProcessTree(child, "SIGKILL");
      resolve();
    }, 2000);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

async function openPlanTab(page, title) {
  await page.goto(uiBase, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "打开反馈优化工作台" }).click();
  await page.getByRole("button", { name: "优化批次", exact: true }).click();
  await page.getByPlaceholder("搜索 ID、标签、Case").fill(title);
  await page.getByText(title).first().click();
  await page.getByRole("tab", { name: /优化方案/ }).click();
}

async function resetPlayground(page) {
  await page.goto(uiBase, { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    window.localStorage.removeItem("playground-session-messages");
    window.localStorage.removeItem("playground-active-session");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
}

async function verifyPlaygroundAssistantStreamingBubble(page, stateRef) {
  stateRef.chatStreamMode = "text";
  stateRef.chatStreamDelayMs = 800;
  stateRef.chatStreamCount = 0;
  await resetPlayground(page);
  await page.getByPlaceholder("输入任务或问题，Ctrl/⌘ + Enter 发送...").fill("验证消息气泡等待态");
  await page.getByRole("button", { name: "发送" }).click();
  await page.locator(".message-assistant-streaming .message-stream-indicator .spin").waitFor({ timeout: 10_000 });
  if (await page.getByText("正在连接 Claude Agent...").count()) {
    throw new Error("Streaming bubble should only show the spinner, not waiting copy");
  }
  if (await page.getByText("等待首个响应片段...").count()) {
    throw new Error("Streaming bubble should only show the spinner, not progress copy");
  }
  if (await page.getByText("停止生成").count()) {
    throw new Error("Streaming bubble should not duplicate the stop action inside the message");
  }
  await page.getByText("第一段回答").waitFor({ timeout: 15_000 });
  await page.locator(".message-assistant-streaming").waitFor({ state: "detached", timeout: 15_000 });
  if (stateRef.chatStreamCount !== 1) {
    throw new Error(`Expected one chat stream request, got ${stateRef.chatStreamCount}`);
  }
}

async function verifyPlaygroundAssistantEmptyResult(page, stateRef) {
  stateRef.chatStreamMode = "empty";
  stateRef.chatStreamDelayMs = 300;
  stateRef.chatStreamCount = 0;
  await resetPlayground(page);
  await page.getByPlaceholder("输入任务或问题，Ctrl/⌘ + Enter 发送...").fill("验证空文本结果");
  await page.getByRole("button", { name: "发送" }).click();
  await page.locator(".message-assistant-streaming .message-stream-indicator .spin").waitFor({ timeout: 10_000 });
  await page.locator(".message-assistant-streaming").waitFor({ state: "detached", timeout: 15_000 });
  if (await page.getByText("本次运行未返回文本结果。").count()) {
    throw new Error("Empty assistant result should not show extra placeholder copy");
  }
  if (stateRef.chatStreamCount !== 1) {
    throw new Error(`Expected one empty chat stream request, got ${stateRef.chatStreamCount}`);
  }
}

async function verifyInitialEmptyState(page, stateRef) {
  stateRef.value = "empty";
  await openPlanTab(page, batchTitle);
  await page.getByText("尚未生成优化方案").waitFor({ timeout: 15_000 });
}

async function verifyGenerationTransition(page, stateRef, targetState) {
  stateRef.value = "empty";
  stateRef.next = targetState;
  await openPlanTab(page, batchTitle);
  await page.getByText("尚未生成优化方案").waitFor({ timeout: 15_000 });
  await page.getByRole("button", { name: "生成优化方案" }).click();
  if (targetState === "success") {
    await page.getByText("补充工作区配置核查指令").first().waitFor({ timeout: 15_000 });
    await page.getByText("运行批次回归测试验证回答包含当前配置事实").waitFor({ timeout: 15_000 });
    // #6 掌控感：阶段 stepper、下一步提示、可见的主操作（不再埋在「高级操作」折叠里）。
    await page.locator(".fw-task-stepper").first().waitFor({ timeout: 15_000 });
    await page.getByText("待执行").first().waitFor({ timeout: 15_000 });
    await page.getByText("下一步").first().waitFor({ timeout: 15_000 });
    const executeButton = page.getByRole("button", { name: "执行", exact: true });
    await executeButton.waitFor({ timeout: 15_000 });
    if (await executeButton.isDisabled()) {
      throw new Error("Pending-execution task should expose an enabled 执行 action directly (not behind a collapse)");
    }
    return;
  }
  await page.getByText("生成失败").first().waitFor({ timeout: 15_000 });
  await page.getByText("AGENT_RUNTIME_ERROR").first().waitFor({ timeout: 15_000 });
  await page.getByText("DSPy output formatter failed for batch_plan").first().waitFor({ timeout: 15_000 });
  await page.getByText("查看错误详情", { exact: true }).click();
  await page.getByText("tasks.0.execution_kind").first().waitFor({ timeout: 15_000 });
  // #7 框选复制友好：原始数据以「字段: 值」可读视图为主，原始 JSON 收进折叠。
  const readableBody = page.locator(".fw-readable-body").first();
  await readableBody.waitFor({ timeout: 15_000 });
  const readableText = (await readableBody.innerText()).trim();
  if (readableText.startsWith("{") || readableText.includes('": ')) {
    throw new Error(`Readable preview should be plain 字段: 值 text, got JSON-like: ${readableText.slice(0, 80)}`);
  }
  if (!readableText.includes("error_code")) {
    throw new Error("Readable preview should surface error_code as a 字段: 值 line");
  }
  await page.getByText("查看原始 JSON").first().waitFor({ timeout: 15_000 });
}

async function verifyOptimizationPlanJobBackgroundFailureRefresh(page, stateRef) {
  stateRef.value = "queued";
  await openPlanTab(page, batchTitle);
  await page.getByText("优化方案正在生成").first().waitFor({ timeout: 15_000 });
  await page.getByText("queued").first().waitFor({ timeout: 15_000 });
  stateRef.value = "failed";
  await page.getByText("生成失败").first().waitFor({ timeout: 15_000 });
  await page.getByText("AGENT_RUNTIME_ERROR").first().waitFor({ timeout: 15_000 });
  await page.getByText("DSPy output formatter failed for batch_plan").first().waitFor({ timeout: 15_000 });
}

async function verifyOptimizationPlanJobTimeoutRefresh(page, stateRef) {
  stateRef.value = "queued";
  await openPlanTab(page, batchTitle);
  await page.getByText("优化方案正在生成").first().waitFor({ timeout: 15_000 });
  await page.getByText("queued").first().waitFor({ timeout: 15_000 });
  stateRef.value = "timeout";
  await page.getByText("生成失败").first().waitFor({ timeout: 15_000 });
  await page.getByText("AGENT_TIMEOUT").first().waitFor({ timeout: 15_000 });
  await page.getByText("Agent job exceeded timeout_seconds=300").first().waitFor({ timeout: 15_000 });
}

async function verifyPlanTaskEditSuccess(page, stateRef) {
  stateRef.value = "success";
  stateRef.edit = {};
  stateRef.editPatchCount = 0;
  await openPlanTab(page, batchTitle);
  await page.getByRole("button", { name: "编辑" }).first().click();
  await page.getByLabel("标题").fill("人工编辑后的优化任务");
  await page.getByLabel("描述").fill("人工修订后的任务说明。");
  await page.getByRole("button", { name: "保存" }).click();
  await page.getByText("人工编辑后的优化任务").first().waitFor({ timeout: 15_000 });
  await page.getByText("人工修订后的任务说明。").first().waitFor({ timeout: 15_000 });
  if (stateRef.editPatchCount !== 1) {
    throw new Error(`Expected one edit PATCH request, got ${stateRef.editPatchCount}`);
  }
}

async function verifyPlanTaskEditInvalidJson(page, stateRef) {
  stateRef.value = "success";
  stateRef.edit = {};
  stateRef.editPatchCount = 0;
  await openPlanTab(page, batchTitle);
  await page.getByRole("button", { name: "编辑" }).first().click();
  await page.getByLabel("任务上下文").fill("{bad-json");
  await page.getByRole("button", { name: "保存" }).click();
  await page.locator(".fw-job-error").getByText(/JSON|Unexpected|Expected/).waitFor({ timeout: 15_000 });
  if (stateRef.editPatchCount !== 0) {
    throw new Error(`Invalid JSON should not call edit API, got ${stateRef.editPatchCount}`);
  }
}

async function verifyOneClickExecutionAndRollback(page, stateRef) {
  stateRef.value = "success";
  stateRef.executeAllCount = 0;
  stateRef.rollbackCount = 0;
  await openPlanTab(page, batchTitle);
  await page.getByRole("button", { name: "一键执行" }).click();
  await page.getByText("Agent 优化结果").first().waitFor({ timeout: 15_000 });
  await page.getByText("completed").first().waitFor({ timeout: 15_000 });
  const publishButton = page.getByRole("button", { name: "发布" });
  await publishButton.waitFor({ timeout: 15_000 });
  if (!(await publishButton.isDisabled())) {
    throw new Error("Batch publish should stay disabled before batch regression passes");
  }
  await page.getByText("查看快照和原始记录", { exact: true }).click();
  await page.getByText("agent-after-ui").first().waitFor({ timeout: 15_000 });
  await page.getByText("CLAUDE.md").first().waitFor({ timeout: 15_000 });
  await page.getByRole("button", { name: "回滚" }).click();
  await page.getByText("rolled_back").first().waitFor({ timeout: 15_000 });
  if (stateRef.executeAllCount !== 1) {
    throw new Error(`Expected one execute-all request, got ${stateRef.executeAllCount}`);
  }
  if (stateRef.rollbackCount !== 1) {
    throw new Error(`Expected one rollback request, got ${stateRef.rollbackCount}`);
  }
}

async function verifyWorkspaceDirtyPreflight(page, stateRef) {
  stateRef.value = "success";
  stateRef.repositoryDirty = true;
  stateRef.discardCount = 0;
  await openPlanTab(page, batchTitle);
  await page.getByText("MAIN_WORKSPACE_DIRTY").waitFor({ timeout: 15_000 });
  await page.getByText(".mcp.json").first().waitFor({ timeout: 15_000 });
  await page.getByText('{"mcpServers":{"sec-ops":{}}}').first().waitFor({ timeout: 15_000 });
  const oneClick = page.getByRole("button", { name: "一键执行" });
  if (!(await oneClick.isDisabled())) {
    throw new Error("One-click execution should be disabled while main workspace is dirty");
  }
  await page.getByRole("button", { name: "丢弃未提交改动" }).click();
  await page.getByText("MAIN_WORKSPACE_DIRTY").waitFor({ state: "detached", timeout: 15_000 });
  await waitForButtonEnabled(page, oneClick, 15_000);
  if (await oneClick.isDisabled()) {
    throw new Error("One-click execution should be enabled after discarding dirty workspace changes");
  }
  if (stateRef.discardCount !== 1) {
    throw new Error(`Expected one discard request, got ${stateRef.discardCount}`);
  }
}

async function waitForButtonEnabled(page, locator, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await locator.isDisabled())) return;
    await page.waitForTimeout(100);
  }
}

async function verifyPendingExecutionPlanStillAllowsRegenerateWithoutReject(page, stateRef) {
  stateRef.value = "pending_execution_unapplied";
  await openPlanTab(page, batchTitle);
  const regenerate = page.getByRole("button", { name: "重新生成优化方案" });
  await regenerate.waitFor({ timeout: 15_000 });
  if (await regenerate.isDisabled()) {
    throw new Error("Pending execution without applied result should allow plan regeneration");
  }
  if (await page.getByRole("button", { name: "拒绝方案" }).count()) {
    throw new Error("Batch optimization plan should not expose reject action");
  }
}

async function verifyBusinessAgentSelectorRoutesChat(page, stateRef) {
  // #5：业务 Agent 创建入口与 Playground 选择——侧栏选择器把对话路由到 agent_id，管理弹窗可达。
  stateRef.chatStreamMode = "text";
  stateRef.chatStreamDelayMs = 0;
  stateRef.lastChatBody = null;
  stateRef.businessAgents = [
    { agent_id: "biz-ui", name: "业务UI助手", category: "business", status: "active", workspace_dir: "/data/business-agents/biz-ui", created_at: timestamp() },
    { agent_id: "main-agent", name: "Main Agent", category: "main", status: "active", workspace_dir: "/main-workspace", created_at: timestamp() },
  ];
  await resetPlayground(page);

  const selector = page.locator(".sidebar select").first();
  await selector.waitFor({ timeout: 15_000 });
  // 默认即主智能体（value 为空）；main-agent 不在业务下拉里，由「默认 main-agent」代表，
  // 故仅 2 个选项（默认 + biz-ui），证明 main-agent 被排除、未重复成普通业务项。
  if ((await selector.inputValue()) !== "") {
    throw new Error("Business agent selector should default to main-agent (empty value)");
  }
  const optionCount = await selector.locator("option").count();
  if (optionCount !== 2) {
    throw new Error(`Business agent selector should expose exactly 默认 + biz-ui (main-agent excluded), got ${optionCount} options`);
  }

  // 管理弹窗可达：创建表单 + 已注册列表。
  await page.locator('button[title^="管理业务 Agent"]').click();
  await page.getByText("业务 Agent 管理").waitFor({ timeout: 15_000 });
  await page.getByText("新建业务 Agent").first().waitFor({ timeout: 15_000 });
  await page.getByRole("button", { name: "关闭" }).click();

  // 选中业务 Agent 后发送，对话请求应携带 agent_id=biz-ui（路由到该治理对象）。
  await selector.selectOption("biz-ui");
  await page.getByPlaceholder("输入任务或问题，Ctrl/⌘ + Enter 发送...").fill("路由到业务 Agent");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByText("第一段回答").waitFor({ timeout: 15_000 });
  if (!stateRef.lastChatBody || stateRef.lastChatBody.agent_id !== "biz-ui") {
    throw new Error(`Chat request should carry agent_id=biz-ui, got ${JSON.stringify(stateRef.lastChatBody && stateRef.lastChatBody.agent_id)}`);
  }

  stateRef.businessAgents = [];
}

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const stateRef = {
      value: "empty",
      next: "success",
      edit: {},
      editPatchCount: 0,
      executeAllCount: 0,
      rollbackCount: 0,
      repositoryDirty: false,
      discardCount: 0,
      snapshotCount: 0,
      chatStreamMode: "text",
      chatStreamDelayMs: 0,
      chatStreamCount: 0,
      businessAgents: [],
      lastChatBody: null,
    };
    await page.addInitScript(
      ({ apiBaseValue }) => {
        window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: apiBaseValue, apiKey: "" }));
      },
      { apiBaseValue: apiBase },
    );
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === uiBase) return route.continue();
      if (route.request().method() === "POST" && url.pathname === "/api/chat/stream") {
        stateRef.chatStreamCount += 1;
        stateRef.lastChatBody = route.request().postDataJSON();
        await delay(stateRef.chatStreamDelayMs);
        const events = [];
        if (stateRef.chatStreamMode !== "empty") {
          events.push({ event: "message", data: { text: "第一段回答", event: "AssistantMessage" } });
        }
        events.push({ event: "result", data: { run_id: "run-ui-chat" } });
        events.push({ event: "done", data: {} });
        return sse(route, events);
      }
      if (route.request().method() === "POST" && url.pathname === "/api/feedback-optimization-batches/fob-ui-state/optimization-plan") {
        stateRef.value = stateRef.next;
        return json(route, agentJob(stateRef.value));
      }
      if (route.request().method() === "PATCH" && url.pathname === "/api/feedback-optimization-batches/fob-ui-state/optimization-plan/tasks/fopt-ui-state") {
        const payload = route.request().postDataJSON();
        stateRef.value = "success";
        stateRef.editPatchCount += 1;
        stateRef.edit = {
          title: payload.title || "补充工作区配置核查指令",
          description: payload.description || "在回答工作区配置类问题前读取当前配置文件。",
        };
        const batch = batchForState("success", stateRef.edit);
        return json(route, {
          batch,
          plan_task: batch.optimization_plan.tasks[0],
          optimization_task: null,
          invalidated_execution_job_ids: ["fbe-stale-ui"],
          external_item: null,
        });
      }
      if (route.request().method() === "POST" && url.pathname === "/api/feedback-optimization-batches/fob-ui-state/optimization-plan/execute-all") {
        stateRef.executeAllCount += 1;
        stateRef.value = "executed";
        const batch = batchForState("executed", stateRef.edit || {});
        return json(route, { batch, execution_run: batch.latest_execution_run });
      }
      if (route.request().method() === "POST" && url.pathname === "/api/agent-repository/discard-changes") {
        stateRef.discardCount += 1;
        stateRef.repositoryDirty = false;
        return json(route, repositoryStatus(false));
      }
      if (route.request().method() === "POST" && url.pathname === "/api/agent-repository/snapshot") {
        stateRef.snapshotCount += 1;
        stateRef.repositoryDirty = false;
        return json(route, { agent_version_id: "agent-snapshot-ui", commit_sha: "agent-snapshot-ui", created_at: timestamp(), reason: "manual_workspace_snapshot" });
      }
      if (route.request().method() === "POST" && url.pathname === "/api/feedback-optimization-batches/fob-ui-state/optimization-plan/executions/fbx-ui-state/rollback") {
        stateRef.rollbackCount += 1;
        stateRef.value = "rolled_back";
        const batch = batchForState("rolled_back", stateRef.edit || {});
        return json(route, { batch, execution_run: batch.latest_execution_run });
      }
      if (url.origin === apiBase || url.pathname.startsWith("/api/") || url.pathname === "/health") {
        return json(route, apiPayload(url.pathname, stateRef));
      }
      return route.continue();
    });
    try {
      await verifyPlaygroundAssistantStreamingBubble(page, stateRef);
      await verifyPlaygroundAssistantEmptyResult(page, stateRef);
      await verifyInitialEmptyState(page, stateRef);
      await verifyGenerationTransition(page, stateRef, "success");
      await verifyGenerationTransition(page, stateRef, "failed");
      await verifyOptimizationPlanJobBackgroundFailureRefresh(page, stateRef);
      await verifyOptimizationPlanJobTimeoutRefresh(page, stateRef);
      await verifyPlanTaskEditSuccess(page, stateRef);
      await verifyPlanTaskEditInvalidJson(page, stateRef);
      await verifyPendingExecutionPlanStillAllowsRegenerateWithoutReject(page, stateRef);
      await verifyWorkspaceDirtyPreflight(page, stateRef);
      await verifyOneClickExecutionAndRollback(page, stateRef);
      await verifyBusinessAgentSelectorRoutesChat(page, stateRef);
    } finally {
      await browser.close();
    }
  } finally {
    await stopChild(server);
  }
  console.log(JSON.stringify({ status: "passed", ui_base: uiBase, scenarios: ["playground_spinner_only", "playground_empty_result", "empty", "success", "failed", "background_failed_refresh", "timeout_refresh", "edit_success", "edit_invalid_json", "pending_execution_unapplied", "workspace_dirty_preflight", "execute_all", "rollback", "business_agent_selector_routes_chat"] }, null, 2));
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error instanceof Error ? error.stack || error.message : error);
    process.exit(1);
  });
