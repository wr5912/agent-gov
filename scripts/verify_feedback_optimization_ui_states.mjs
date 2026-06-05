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

function timestamp() {
  return "2026-06-05T00:00:00Z";
}

function agentJob(state) {
  const failed = state === "failed";
  return {
    job_id: "fbp-ui-state",
    job_type: "batch_plan",
    profile_name: "proposal-generator",
    status: failed ? "failed" : "completed",
    created_at: timestamp(),
    started_at: timestamp(),
    completed_at: timestamp(),
    error_json: failed ? planError() : null,
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

function optimizationPlan() {
  return {
    optimization_plan_id: "fop-ui-state",
    status: "pending_approval",
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
        title: "补充工作区配置核查指令",
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

function batchForState(state) {
  const failed = state === "failed";
  const success = state === "success";
  return {
    batch_id: "fob-ui-state",
    title: batchTitle,
    status: success ? "pending_approval" : failed ? "needs_human_review" : "attribution_completed",
    priority: "medium",
    source_refs: [{ source_kind: "signal", source_id: "fbs-ui-state" }],
    feedback_case_ids: ["fbc-ui-state"],
    eval_case_ids: [],
    attribution_job_ids: ["fba-ui-state"],
    attribution_jobs: [attributionJob()],
    attribution_summary: { total: 1, completed: 1, needs_review_or_failed: 0 },
    optimization_plan_job_id: state === "empty" ? null : "fbp-ui-state",
    optimization_plan_job: state === "empty" ? null : agentJob(state),
    optimization_plan_error: failed ? planError() : null,
    optimization_plan: success ? optimizationPlan() : null,
    optimization_task: null,
    execution_job: null,
    created_at: timestamp(),
    updated_at: timestamp(),
  };
}

function apiPayload(path, state) {
  if (path === "/health") return { status: "ok", model: "ui-state-mock" };
  if (path === "/api/sessions") return [];
  if (path === "/api/agents") return [];
  if (path === "/api/skills") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return {};
  if (path === "/api/agent-repository/current") return {};
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
  if (path === "/api/feedback-optimization-batches") return [batchForState(state)];
  if (path === "/api/agent-jobs/fbp-ui-state") return agentJob(state);
  return {};
}

function startVite() {
  const child = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "pipe"],
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

async function stopChild(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  await new Promise((resolve) => {
    const timeout = setTimeout(resolve, 2000);
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
    return;
  }
  await page.getByText("生成失败").first().waitFor({ timeout: 15_000 });
  await page.getByText("AGENT_RUNTIME_ERROR").first().waitFor({ timeout: 15_000 });
  await page.getByText("DSPy output formatter failed for batch_plan").first().waitFor({ timeout: 15_000 });
  await page.getByText("查看错误详情", { exact: true }).click();
  await page.getByText("tasks.0.execution_kind").first().waitFor({ timeout: 15_000 });
}

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const stateRef = { value: "empty", next: "success" };
    await page.addInitScript(
      ({ apiBaseValue }) => {
        window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: apiBaseValue, apiKey: "" }));
      },
      { apiBaseValue: apiBase },
    );
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === uiBase) return route.continue();
      if (route.request().method() === "POST" && url.pathname === "/api/feedback-optimization-batches/fob-ui-state/optimization-plan") {
        stateRef.value = stateRef.next;
        return json(route, agentJob(stateRef.value));
      }
      if (url.origin === apiBase || url.pathname.startsWith("/api/") || url.pathname === "/health") {
        return json(route, apiPayload(url.pathname, stateRef.value));
      }
      return route.continue();
    });
    try {
      await verifyInitialEmptyState(page, stateRef);
      await verifyGenerationTransition(page, stateRef, "success");
      await verifyGenerationTransition(page, stateRef, "failed");
    } finally {
      await browser.close();
    }
  } finally {
    await stopChild(server);
  }
  console.log(JSON.stringify({ status: "passed", ui_base: uiBase, scenarios: ["empty", "success", "failed"] }, null, 2));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : error);
  process.exit(1);
});
