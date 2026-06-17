#!/usr/bin/env node
// v2.7 跨代重建验收：改进事项治理工作台（mock /api/improvements）。
// 断言：列表 + Agent scoping、详情阶段 data-state、每态唯一主动作 data-action、阶段推进、
// 获取上下文抽屉（结构化 字段:值 文本）、新建。脚本以 detached 进程组干净退出，不残留端口。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.env.IMPROVEMENT_UI_PORT || "55188");
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const ts = "2026-06-17T00:00:00Z";

function json(route, payload, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
}

function startVite() {
  const child = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  child.stdout.on("data", (chunk) => process.stdout.write(chunk));
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));
  return child;
}

function killProcessTree(child, signal) {
  try {
    process.kill(-child.pid, signal);
  } catch {
    try {
      child.kill(signal);
    } catch {
      // already exited
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

function defaultPayload(path) {
  if (path === "/health") return { status: "ok", model: "governance-mock" };
  if (path === "/api/sessions") return [];
  if (path === "/api/agents") return [];
  if (path === "/api/skills") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  if (path === "/api/agent-change-sets") return [];
  if (path === "/api/agent-releases") return [];
  return {};
}

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

    const stateRef = {
      agents: [
        { agent_id: "soc-ops", name: "安全运营助手", category: "business", workspace_dir: "/data/business-agents/soc-ops", created_at: ts, status: "active" },
        { agent_id: "main-agent", name: "Main Agent", category: "main", workspace_dir: "/main-workspace", created_at: ts, status: "active" },
      ],
      improvements: [
        {
          improvement_id: "imp-seed01",
          agent_id: "soc-ops",
          title: "告警误报治理",
          summary: "事件时间与告警时间不一致",
          source_feedback_refs: ["fbs-1"],
          improvement_stage: "attribution",
          improvement_status: "active",
          created_at: ts,
          updated_at: ts,
        },
      ],
      createCount: 0,
      policyMode: "off",
    };

    await page.addInitScript(
      ({ apiBaseValue }) => {
        window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: apiBaseValue, apiKey: "" }));
      },
      { apiBaseValue: apiBase },
    );

    await page.route("**/*", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      if (url.origin === uiBase) return route.continue();
      const method = req.method();
      const path = url.pathname;

      if (path === "/api/agent-registry" && method === "GET") return json(route, stateRef.agents);

      if (path === "/api/improvements" && method === "GET") {
        const agentId = url.searchParams.get("agent_id");
        const list = agentId ? stateRef.improvements.filter((i) => i.agent_id === agentId) : stateRef.improvements;
        return json(route, list);
      }
      if (path === "/api/improvements" && method === "POST") {
        const body = req.postDataJSON();
        stateRef.createCount += 1;
        const item = {
          improvement_id: `imp-new${stateRef.createCount}`,
          agent_id: body.agent_id,
          title: body.title,
          summary: body.summary || "",
          source_feedback_refs: [],
          improvement_stage: "feedback_intake",
          improvement_status: "active",
          created_at: ts,
          updated_at: ts,
        };
        stateRef.improvements = [item, ...stateRef.improvements];
        return json(route, item, 201);
      }
      const lifecycle = path.match(/^\/api\/improvements\/([^/]+)\/lifecycle$/);
      if (lifecycle && method === "POST") {
        const id = decodeURIComponent(lifecycle[1]);
        const body = req.postDataJSON();
        const item = stateRef.improvements.find((i) => i.improvement_id === id);
        if (!item) return json(route, { detail: "not found" }, 404);
        item.improvement_stage = body.stage;
        item.improvement_status = body.stage === "release" ? "done" : "active";
        item.updated_at = ts;
        return json(route, item);
      }
      if (path === "/api/automation-policy" && method === "GET") {
        return json(route, { agent_id: url.searchParams.get("agent_id"), mode: stateRef.policyMode });
      }
      if (path === "/api/automation-policy" && method === "PUT") {
        const body = req.postDataJSON();
        stateRef.policyMode = body.mode;
        return json(route, { agent_id: body.agent_id, mode: body.mode });
      }
      const autoAdv = path.match(/^\/api\/improvements\/([^/]+)\/auto-advance$/);
      if (autoAdv && method === "POST") {
        const id = decodeURIComponent(autoAdv[1]);
        const item = stateRef.improvements.find((i) => i.improvement_id === id);
        if (!item) return json(route, { detail: "not found" }, 404);
        const ORDER = ["feedback_intake", "triage", "attribution", "optimization", "execution", "regression", "release"];
        const AUTO = new Set(["feedback_intake>triage", "triage>attribution", "execution>regression"]);
        const GATE = new Set(["attribution>optimization", "optimization>execution"]);
        const applied = [];
        let reason = "terminal";
        if (stateRef.policyMode === "off") {
          reason = "policy_off";
        } else if (item.improvement_status === "archived") {
          reason = "archived";
        } else {
          let stage = item.improvement_stage;
          for (;;) {
            const nxt = ORDER[ORDER.indexOf(stage) + 1];
            if (!nxt) { reason = "terminal"; break; }
            const edge = `${stage}>${nxt}`;
            if (AUTO.has(edge) || (GATE.has(edge) && stateRef.policyMode === "full")) { stage = nxt; applied.push(nxt); continue; }
            reason = GATE.has(edge) ? "gate_confirmation" : "release_gate";
            break;
          }
          item.improvement_stage = stage;
          item.improvement_status = stage === "release" ? "done" : "active";
        }
        item.updated_at = ts;
        return json(route, { improvement: item, applied_stages: applied, stopped_reason: reason });
      }
      const archive = path.match(/^\/api\/improvements\/([^/]+)\/archive$/);
      if (archive && method === "POST") {
        const id = decodeURIComponent(archive[1]);
        const item = stateRef.improvements.find((i) => i.improvement_id === id);
        if (!item) return json(route, { detail: "not found" }, 404);
        item.improvement_status = "archived";
        item.updated_at = ts;
        return json(route, item);
      }
      const detail = path.match(/^\/api\/improvements\/([^/]+)$/);
      if (detail && method === "GET") {
        const id = decodeURIComponent(detail[1]);
        const item = stateRef.improvements.find((i) => i.improvement_id === id);
        return item ? json(route, item) : json(route, { detail: "not found" }, 404);
      }

      if (url.origin === apiBase || path.startsWith("/api/") || path === "/health") {
        return json(route, defaultPayload(path));
      }
      return route.continue();
    });

    try {
      await page.goto(uiBase, { waitUntil: "domcontentloaded" });

      // 进入改进工作台。
      await page.getByRole("button", { name: "打开改进工作台" }).click();
      await page.getByTestId("improvement-workbench").waitFor({ timeout: 15_000 });

      // 列表渲染种子事项，data-stage 可断言。
      const seed = page.locator('[data-testid="improvement-list-item"][data-item-id="imp-seed01"]');
      await seed.waitFor({ timeout: 15_000 });
      if ((await seed.getAttribute("data-stage")) !== "attribution") {
        throw new Error("seed item should expose data-stage=attribution");
      }

      // 选中 → 详情 + 当前阶段 data-state。
      await seed.click();
      await page.getByTestId("improvement-detail").waitFor({ timeout: 15_000 });
      await page.getByTestId("improvement-title").waitFor({ timeout: 15_000 });
      const stagePill = page.getByTestId("current-stage");
      if ((await stagePill.getAttribute("data-state")) !== "attribution") {
        throw new Error("detail current-stage should be data-state=attribution");
      }

      // 详情展示「下一步」提示（非空）。
      const nextStep = page.getByTestId("improvement-next-step");
      await nextStep.waitFor({ timeout: 15_000 });
      if (!(await nextStep.innerText()).trim()) {
        throw new Error("detail should show a non-empty 下一步 hint");
      }

      // 每态唯一主动作：attribution -> optimization。
      if ((await page.getByTestId("primary-action").count()) !== 1) {
        throw new Error("each stage must expose exactly one primary action");
      }
      const primary = page.getByTestId("primary-action");
      if ((await primary.getAttribute("data-action")) !== "optimization") {
        throw new Error("attribution stage primary action should advance to optimization");
      }
      await primary.click();
      await page.locator('[data-testid="current-stage"][data-state="optimization"]').waitFor({ timeout: 15_000 });
      if ((await page.getByTestId("primary-action").getAttribute("data-action")) !== "execution") {
        throw new Error("after advancing to optimization, next primary action should target execution");
      }

      // 获取上下文：结构化 字段:值 文本（非原始 JSON），有复制入口。
      await page.getByTestId("open-context-drawer").click();
      const drawer = page.getByTestId("context-drawer");
      await drawer.waitFor({ timeout: 15_000 });
      if ((await drawer.getAttribute("data-state")) !== "open") {
        throw new Error("context drawer should be data-state=open after opening");
      }
      const ctx = (await page.locator(".iw-context-body").innerText()).trim();
      if (!ctx.includes("improvement_id: imp-seed01")) {
        throw new Error("context package should surface improvement_id as 字段: 值 text");
      }
      if (ctx.startsWith("{") || ctx.includes('": ')) {
        throw new Error("context package should be plain 字段: 值 text, not raw JSON");
      }
      await page.getByTestId("context-copy").waitFor({ timeout: 15_000 });

      // 新建改进事项 → 选中、阶段为初始 feedback_intake、主动作指向 triage。
      await page.getByTestId("improvement-create-agent").selectOption("soc-ops");
      await page.getByTestId("improvement-create-title").fill("新的改进事项");
      await page.getByTestId("improvement-create-submit").click();
      await page.locator('[data-testid="current-stage"][data-state="feedback_intake"]').waitFor({ timeout: 15_000 });
      if ((await page.getByTestId("primary-action").getAttribute("data-action")) !== "triage") {
        throw new Error("new improvement at feedback_intake should advance to triage");
      }

      // W2-a 自动化策略：设 semi → 自动推进，feedback_intake 自动到 attribution（停在判断点）。
      await page.getByTestId("automation-mode").selectOption("semi");
      await page.getByTestId("auto-advance").click();
      await page.locator('[data-testid="current-stage"][data-state="attribution"]').waitFor({ timeout: 15_000 });
      await page.getByTestId("auto-advance-result").waitFor({ timeout: 15_000 });

      // 归档为终态状态：归档后状态 archived、主动作消失、显示已归档。
      await page.getByTestId("archive-improvement").click();
      await page.locator('[data-testid="improvement-status"][data-status="archived"]').waitFor({ timeout: 15_000 });
      if ((await page.getByTestId("primary-action").count()) !== 0) {
        throw new Error("archived improvement must expose no primary action");
      }
      await page.getByTestId("improvement-archived").waitFor({ timeout: 15_000 });

      // 业务 Agent scoping：经顶栏全局切换器选 soc-ops，改进列表仍可见其事项。
      await page.getByTestId("topbar-agent-switcher").selectOption("soc-ops");
      await page.locator('[data-testid="improvement-list-item"]').first().waitFor({ timeout: 15_000 });
      const scopedStages = await page.locator('[data-testid="improvement-list-item"]').count();
      if (scopedStages < 1) {
        throw new Error("scoping to soc-ops should still show its improvement items");
      }

      // 发布页可达（消费真实 releases / change-sets；mock 下为空态，门禁为「无待发布变更」）。
      await page.getByTestId("nav-release").click();
      await page.getByTestId("release-workbench").waitFor({ timeout: 15_000 });
      await page.getByTestId("release-gate").waitFor({ timeout: 15_000 });
    } finally {
      await browser.close();
    }
  } finally {
    await stopChild(server);
  }
  console.log(JSON.stringify({ status: "passed", ui_base: uiBase, scenarios: ["improvement_workbench_list_detail_stage_action_context_create_scoping"] }, null, 2));
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error instanceof Error ? error.stack || error.message : error);
    process.exit(1);
  });
