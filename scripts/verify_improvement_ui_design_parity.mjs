#!/usr/bin/env node
// 四阶段改进治理 UI 设计一致性硬门（不是功能可用门）。
// 原设计规则保持逐条记分；本入口固定使用自启 Vite + mock 后端，可确定性进入 CI。
// 真实容器功能与视觉验收由 verify_improvement_ui_real_container.mjs 单独负责。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import process from "node:process";
import { scrollNavigationMetrics, seedPlaygroundMessages } from "./playground_scroll_test_helpers.mjs";
import { createFoundationRules } from "./improvement_ui_e2e/design_parity_foundation_rules.mjs";
import { createWorkbenchRules } from "./improvement_ui_e2e/design_parity_workbench_rules.mjs";
import { defaultPayload, DESIGN_PARITY_TS as ts } from "./improvement_ui_e2e/design_parity_mock_backend.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = new URL("..", import.meta.url).pathname;
const port = Number(process.env.PARITY_PORT || 55197);
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const apiKey = "";
const auditTargetId = "imp-demo01";
const auditTargets = {
  feedback: "imp-demo01",
  attribution: "imp-demo02",
  optimization: "imp-demo03",
  testRelease: "imp-demo04",
  duplicate: "imp-demo05",
  optimizationPending: "imp-demo06",
};
const observedApiRequests = [];

function startVite() {
  const c = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"], detached: true });
  c.stdout.on("data", () => {}); c.stderr.on("data", () => {});
  return c;
}
function killTree(c, s) { try { process.kill(-c.pid, s); } catch { try { c.kill(s); } catch { /* gone */ } } }
async function stopChild(c) { if (!c || c.exitCode !== null) return; killTree(c, "SIGTERM"); await new Promise((r) => { const t = setTimeout(() => { killTree(c, "SIGKILL"); r(); }, 2000); c.once("exit", () => { clearTimeout(t); r(); }); }); }
async function waitForVite() { const d = Date.now() + 30000; while (Date.now() < d) { try { const r = await fetch(uiBase); if (r.ok) return; } catch { await new Promise((r) => setTimeout(r, 250)); } } throw new Error("vite not ready"); }

// 整改基线（BASELINE 模式）：已落地阶段的规则必须保持全绿（防回归）；尚未落地阶段的规则可红。
// 随 P1..P4 推进，把对应规则 id 加入此基线；mock 基线必须始终全绿。
const BASELINE_RULES = new Set(
  (process.env.PARITY_BASELINE || "nav-converged,settings-ia,playground-clean,playground-action-semantics,playground-session-sidebar,playground-runtime-settings-drawer,message-actions,playground-scroll-navigation,trace-evidence-panel,panel-size-policy,feedback-drawer-2phase,context-4types,release-workbench-target-binding,release-test-run-contract,release-merged-into-test-stage,test-release-stage-panels,theme-governance-light,improvement-default-detail,decision-card-slim,invalid-back-actions-hidden,four-stage-panels,closed-loop-spine,improvement-content,stage-detail-drawers,improvement-assets,asset-browse-first,attribution-actions,decision-card-product-action,source-feedback-table,detail-collapsed,full-chain,status-filter,merge-basis,trace-summary,optimization-execution,optimization-action-semantics,governance-generation-source,execution-version-binding,regression-governor").split(",").map((s) => s.trim()).filter(Boolean),
);

const has = async (page, testid) => (await page.getByTestId(testid).count()) > 0;
const visible = async (page, testid) => (await page.getByTestId(testid).count()) > 0 && (await page.getByTestId(testid).first().isVisible());
const textIncludes = async (locator, value) => (await locator.innerText().catch(() => "")).includes(value);
const scrollDistance = async (page) => page.getByTestId("playground-messages").evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
async function stageGridHeightMetrics(page) {
  return page.locator('[data-testid="stage-work-area"] .iw-stage-panel-grid').first().evaluate((grid) => {
    const cards = Array.from(grid.querySelectorAll(":scope > .iw-stage-card"));
    const metrics = cards.map((card) => {
      const rect = card.getBoundingClientRect();
      return {
        testid: card.getAttribute("data-testid") || "",
        top: Math.round(rect.top),
        height: Math.round(rect.height),
        overflowing: card.scrollHeight > card.clientHeight + 1 || card.scrollWidth > card.clientWidth + 1,
      };
    });
    const rows = [];
    for (const metric of metrics) {
      let row = rows.find((entry) => Math.abs(entry.top - metric.top) <= 2);
      if (!row) {
        row = { top: metric.top, heights: [], testids: [] };
        rows.push(row);
      }
      row.heights.push(metric.height);
      row.testids.push(metric.testid);
    }
    const rowSpreads = rows.map((row) => ({
      testids: row.testids,
      heights: row.heights,
      spread: row.heights.length ? Math.max(...row.heights) - Math.min(...row.heights) : 0,
    }));
    return {
      heights: metrics.map((metric) => metric.height),
      rowSpreads,
      maxRowSpread: rowSpreads.length ? Math.max(...rowSpreads.map((row) => row.spread)) : 0,
      overflowing: metrics.filter((metric) => metric.overflowing).map((metric) => metric.testid),
    };
  }).catch(() => ({ heights: [], rowSpreads: [], maxRowSpread: -1, overflowing: ["metrics-error"] }));
}
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
async function waitForObservedRequest(predicate, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (observedApiRequests.some(predicate)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
}

async function renderReleaseWorkbenchHarness(page, changeSets) {
  await page.evaluate(async (items) => {
    const [ReactDOMClientModule, ReactModule, { ReleaseWorkbench }] = await Promise.all([
      import("/node_modules/.vite/deps/react-dom_client.js"),
      import("/node_modules/.vite/deps/react.js"),
      import("/src/components/ReleaseWorkbench.tsx"),
    ]);
    const { createRoot } = ReactDOMClientModule.default;
    const React = ReactModule.default;
    let host = document.getElementById("release-workbench-behavior-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "release-workbench-behavior-host";
      Object.assign(host.style, { position: "fixed", inset: "0", zIndex: "10000", background: "white", overflow: "auto" });
      document.body.append(host);
    }
    window.__releaseWorkbenchRefreshCount = 0;
    window.__releaseWorkbenchBehaviorRoot ||= createRoot(host);
    window.__releaseWorkbenchBehaviorRoot.render(React.createElement(ReleaseWorkbench, {
      clientConfig: { apiBase: "http://runtime.test", apiKey: "" },
      scopeAgentId: "soc-ops",
      sourceImprovementId: items[0]?.source_improvement_id || "imp-release",
      releases: [],
      changeSets: items,
      onRefresh: async () => { window.__releaseWorkbenchRefreshCount += 1; },
    }));
  }, changeSets);
  await page.getByTestId("release-workbench").waitFor({ timeout: 8000 });
}

async function removeReleaseWorkbenchHarness(page) {
  await page.evaluate(() => {
    window.__releaseWorkbenchBehaviorRoot?.unmount();
    delete window.__releaseWorkbenchBehaviorRoot;
    document.getElementById("release-workbench-behavior-host")?.remove();
  });
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
  return auditTargets[key] || mockId;
}

const ruleContext = {
  fillJsonEditor,
  has,
  observedApiRequests,
  openAuditImprovement,
  openImprovementById,
  removeReleaseWorkbenchHarness,
  renderReleaseWorkbenchHarness,
  scrollDistance,
  scrollNavigationMetrics,
  seedPlaygroundMessages,
  stageGridHeightMetrics,
  stageTarget,
  ts,
  textIncludes,
  visible,
  waitForObservedRequest,
  waitNearBottom,
  waitPreviewOpen,
};
const RULES = [
  ...createFoundationRules(ruleContext),
  ...createWorkbenchRules(ruleContext),
];

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
    page.on("request", (request) => {
      try {
        const url = new URL(request.url());
        if (url.hostname === "runtime.test") {
          observedApiRequests.push({ method: request.method(), path: url.pathname, postData: request.postData() || "" });
        }
      } catch { /* ignore non-standard urls */ }
    });
    await page.addInitScript(([base, key]) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: base, apiKey: key }));
      if (window.sessionStorage.getItem("parity-preserve-playground-session") !== "1") {
        window.localStorage.setItem("playground-active-session", JSON.stringify("mock-session"));
      }
    }, [apiBase, apiKey]);
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.hostname !== "runtime.test") return route.continue();
      const payload = defaultPayload(url.pathname, {
        method: route.request().method(),
        postData: route.request().postData() || "",
        queryPath: url.searchParams.get("path") || "",
        agentId: url.searchParams.get("agent_id") || "",
        changeSetId: url.searchParams.get("change_set_id") || "",
        commitSha: url.searchParams.get("commit_sha") || "",
        sourceImprovementId: url.searchParams.get("source_improvement_id") || "",
      });
      const status = payload && typeof payload === "object" && "__status" in payload ? Number(payload.__status) || 200 : 200;
      const body = payload && typeof payload === "object" && "__status" in payload
        ? JSON.stringify(Object.fromEntries(Object.entries(payload).filter(([key]) => key !== "__status")))
        : JSON.stringify(payload);
      return route.fulfill({
        status,
        contentType: "application/json",
        headers: { "access-control-allow-origin": "*" },
        body,
      });
    });
    const results = [];
    try {
      await page.goto(uiBase, { waitUntil: "domcontentloaded" });
      await page.getByTestId("topbar-agent-switcher").waitFor({ timeout: 20000 });
      for (const rule of RULES) {
        console.error(`RULE_START ${rule.id}`);
        let result;
        try {
          result = await rule.fn(page);
        } catch (error) {
          result = { ok: false, detail: `EXC: ${error instanceof Error ? error.message : error}` };
        }
        console.error(`RULE_DONE ${rule.id} ${result.ok ? "ok" : "fail"}`);
        results.push({ id: rule.id, phase: rule.phase, ok: !!result.ok, desc: rule.desc, detail: result.detail });
      }
    } finally {
      await browser.close();
    }
    const passed = results.filter((result) => result.ok).length;
    const baselineFail = results.filter((result) => BASELINE_RULES.has(result.id) && !result.ok);
    console.log(JSON.stringify({
      mode: "mock",
      ui_base: uiBase,
      audit_target_id: auditTargetId,
      passed,
      total: results.length,
      baseline: [...BASELINE_RULES],
      baseline_fail: baselineFail.map((result) => result.id),
      rules: results,
    }, null, 2));
    console.log(`\nDESIGN_PARITY ${passed}/${results.length} passed (mock); baseline ${BASELINE_RULES.size - baselineFail.length}/${BASELINE_RULES.size} held`);
    return baselineFail.length === 0 ? 0 : 1;
  } finally {
    await stopChild(server);
  }
}
main().then((code) => process.exit(code)).catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(2); });
