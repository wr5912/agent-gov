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
  if (path === "/api/agents" || path === "/api/skills" || path === "/api/sessions" || path === "/api/agent-change-sets" || path === "/api/agent-releases" || path === "/api/assets") return [];
  if (path === "/api/improvements") return IMPROVEMENTS;
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  if (/^\/api\/improvements\/[^/]+\/similar$/.test(path)) return [];
  if (/^\/api\/improvements\/[^/]+\/links$/.test(path)) return [];
  if (/^\/api\/improvements\/[^/]+\/feedbacks$/.test(path)) return [{ feedback_id: "fb-1", improvement_id: "imp-demo01", agent_id: "soc-ops", summary: "这个告警其实是误报", source: "playground_run", status: "merged", raw_text: "", run_id: "run-1", session_id: "s-1", created_at: ts }];
  if (/^\/api\/improvements\/[^/]+\/normalized-feedback$/.test(path)) return { normalized_feedback_id: "nf-1", improvement_id: "imp-demo01", problem: "告警误报", possible_reason: "事件时间与告警时间不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "进入改进处理", user_quote: "这个告警其实是误报", status: "draft", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/attribution$/.test(path)) return { attribution_id: "attr-1", improvement_id: "imp-demo01", summary: "MCP 数据时间不一致导致误判", responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"], evidence: ["list_events 返回的数据时间与告警时间窗口不一致"], status: "draft", created_at: ts, updated_at: ts };
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
  (process.env.PARITY_BASELINE || "nav-converged,settings-ia,playground-clean,playground-config-drawer,feedback-drawer-2phase,context-4types,release-gates,theme-governance-light,improvement-content,improvement-assets,attribution-actions,source-feedback-table,detail-collapsed,full-chain,status-filter").split(",").map((s) => s.trim()).filter(Boolean),
);

const has = async (page, testid) => (await page.getByTestId(testid).count()) > 0;
const visible = async (page, testid) => (await page.getByTestId(testid).count()) > 0 && (await page.getByTestId(testid).first().isVisible());

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
    const secs = ["settings-section-agents", "settings-section-automation", "settings-section-assets", "settings-section-developer"];
    const found = []; for (const s of secs) if (await has(page, s)) found.push(s);
    // 关闭设置弹窗，避免 modal-backdrop 拦截后续规则的点击。
    await page.getByTestId("settings-panel").getByRole("button", { name: "关闭" }).click().catch(() => {});
    await page.getByTestId("settings-panel").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
    return { ok: found.length === secs.length, detail: `${found.length}/${secs.length}: ${found.join(",")}` };
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
  { id: "feedback-drawer-2phase", phase: "P1", desc: "创建反馈 Drawer 两阶段：输入态 → 系统理解确认态", async fn(page) {
    await page.getByTestId("nav-playground").click();
    if (!(await has(page, "feedback-drawer-open"))) return { ok: false, detail: "无 feedback-drawer-open 入口" };
    await page.getByTestId("feedback-drawer-open").first().click();
    const open = await visible(page, "feedback-drawer");
    const state = open ? await page.getByTestId("feedback-drawer").getAttribute("data-state") : null;
    return { ok: open && state === "input", detail: `drawer 可见=${open} data-state=${state}` };
  } },
  { id: "context-4types", phase: "P2", desc: "获取上下文四类型 + 下载", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    await page.getByTestId("improvement-workbench").waitFor({ timeout: 8000 }).catch(() => {});
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项可打开上下文（需种子数据）" };
    await first.click();
    await page.getByTestId("open-context-drawer").click().catch(() => {});
    const types = ["context-type-problem", "context-type-ai", "context-type-playwright", "context-type-json"];
    const found = []; for (const t of types) if (await has(page, t)) found.push(t);
    return { ok: found.length === 4 && (await has(page, "context-download")), detail: `类型 ${found.length}/4，下载=${await has(page, "context-download")}` };
  } },
  { id: "release-gates", phase: "P2", desc: "发布页三门门禁 + 去运行回归/查看变更/强制发布动作", async fn(page) {
    await page.getByTestId("nav-release").click();
    await page.getByTestId("release-workbench").waitFor({ timeout: 8000 });
    const gates = ["release-gate-attribution", "release-gate-optimization", "release-gate-regression"];
    const gfound = []; for (const g of gates) if (await has(page, g)) gfound.push(g);
    const actions = ["release-action-run-regression", "release-action-view-changes", "release-action-force"];
    const afound = []; for (const a of actions) if (await has(page, a)) afound.push(a);
    return { ok: gfound.length === 3 && afound.length === 3, detail: `门禁 ${gfound.length}/3，动作 ${afound.length}/3` };
  } },
  { id: "improvement-content", phase: "P3", desc: "改进详情含系统理解(NormalizedFeedback) + 归因(Attribution 正文/责任边界/证据)", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项" };
    await first.click();
    await page.getByTestId("normalized-feedback").waitFor({ timeout: 6000 }).catch(() => {});
    const nf = await has(page, "normalized-feedback");
    const attr = await has(page, "attribution");
    const ev = await has(page, "attribution-evidence");
    return { ok: nf && attr && ev, detail: `系统理解=${nf} 归因=${attr} 证据=${ev}` };
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
    await page.getByTestId("nav-improvement").click();
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项" };
    await first.click();
    await page.getByTestId("full-chain").waitFor({ timeout: 6000 }).catch(() => {});
    const fc = await has(page, "full-chain");
    const steps = await page.getByTestId("full-chain-step").count();
    return { ok: fc && steps === 7, detail: `完整链路=${fc} 阶段数=${steps}` };
  } },
  { id: "detail-collapsed", phase: "P2", desc: "改进详情收纳：自动化/相似/链接进「高级」折叠，默认不在主区可见", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项" };
    await first.click();
    await page.getByTestId("improvement-advanced").waitFor({ timeout: 6000 }).catch(() => {});
    const advanced = await has(page, "improvement-advanced");
    const autoVisible = await visible(page, "automation-mode");
    return { ok: advanced && !autoVisible, detail: `高级折叠=${advanced} 自动化默认隐藏=${!autoVisible}` };
  } },
  { id: "source-feedback-table", phase: "P3", desc: "来源反馈表(§8.4 #/反馈摘要/来源/状态)", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项" };
    await first.click();
    await page.getByTestId("source-feedback-table").waitFor({ timeout: 6000 }).catch(() => {});
    const tbl = await has(page, "source-feedback-table");
    const rows = await page.getByTestId("source-feedback-row").count();
    return { ok: tbl && rows >= 1, detail: `表=${tbl} 行=${rows}` };
  } },
  { id: "attribution-actions", phase: "P3", desc: "归因支持 修改/重新整理(§6 [确认][修改][重新整理])", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项" };
    await first.click();
    await page.getByTestId("attribution").waitFor({ timeout: 6000 }).catch(() => {});
    const edit = await has(page, "edit-attribution");
    const regen = await has(page, "regenerate-attribution");
    return { ok: edit && regen, detail: `修改=${edit} 重新整理=${regen}` };
  } },
  { id: "improvement-assets", phase: "P3", desc: "改进详情含回归保障候选(§11.1) + 本事项沉淀资产区(§11.2)", async fn(page) {
    await page.getByTestId("nav-improvement").click();
    const first = page.getByTestId("improvement-list-item").first();
    await first.waitFor({ timeout: 8000 }).catch(() => {});
    if ((await first.count()) === 0) return { ok: false, detail: "无改进事项" };
    await first.click();
    await page.getByTestId("improvement-detail").waitFor({ timeout: 6000 }).catch(() => {});
    // §11 能力存在的两种合法态：未采纳→候选卡(regression-guarantee+adopt)；已采纳→沉淀资产区(sediment-assets)。
    const rg = await has(page, "regression-guarantee");
    const adopt = await has(page, "adopt-regression");
    const sediment = await has(page, "sediment-assets");
    return { ok: (rg && adopt) || sediment, detail: `回归保障候选=${rg} 采纳=${adopt} 沉淀资产=${sediment}` };
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
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
    await page.addInitScript(([base, key]) => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: base, apiKey: key }));
    }, [apiBase, apiKey]);
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
        try { r = await rule.fn(page); } catch (e) { r = { ok: false, detail: `EXC: ${e instanceof Error ? e.message : e}` }; }
        results.push({ id: rule.id, phase: rule.phase, ok: !!r.ok, desc: rule.desc, detail: r.detail });
      }
    } finally {
      await browser.close();
    }
    const passed = results.filter((r) => r.ok).length;
    const baselineFail = results.filter((r) => BASELINE_RULES.has(r.id) && !r.ok);
    console.log(JSON.stringify({ mode: REAL ? "real-container" : "mock", ui_base: uiBase, passed, total: results.length, baseline: [...BASELINE_RULES], baseline_fail: baselineFail.map((r) => r.id), rules: results }, null, 2));
    console.log(`\nDESIGN_PARITY ${passed}/${results.length} passed (${REAL ? "real-container" : "mock"}); baseline ${BASELINE_RULES.size - baselineFail.length}/${BASELINE_RULES.size} held`);
    // 门：基线规则必须全绿（防回归）；目标是把基线扩到 9/9。
    return baselineFail.length === 0 ? 0 : 1;
  } finally {
    await stopChild(server);
  }
}
main().then((code) => process.exit(code)).catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(2); });
