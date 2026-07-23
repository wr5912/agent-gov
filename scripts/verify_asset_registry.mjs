// 四阶段改进治理 W3 资产 Registry 复利中心 UI 验收：沉淀资产 → 跨 Agent 继承复用。
// 默认启动 Vite + mock 后端；设置 RUNTIME_UI_BASE/RUNTIME_API_BASE 时直连真实容器。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import process from "node:process";
const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = new URL("..", import.meta.url).pathname;
const port = Number(process.env.ASSET_UI_PORT || 55193);
const REAL = !!process.env.RUNTIME_UI_BASE;
const ui = (process.env.RUNTIME_UI_BASE || `http://127.0.0.1:${port}`).replace(/\/$/, "");
const apiBase = (process.env.RUNTIME_API_BASE || "http://runtime.test").replace(/\/$/, "");
const apiKey = process.env.RUNTIME_API_KEY || dockerEnvValue("FRONTEND_RUNTIME_API_KEY") || dockerEnvValue("API_KEY") || "";
const ts = "2026-06-17T00:00:00Z";

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

function startVite() {
  const c = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"], detached: true });
  c.stdout.on("data", () => {}); c.stderr.on("data", () => {});
  return c;
}
function killTree(c, s) { try { process.kill(-c.pid, s); } catch { try { c.kill(s); } catch {} } }
async function stopChild(c) { if (!c || c.exitCode !== null) return; killTree(c, "SIGTERM"); await new Promise((r) => { const t = setTimeout(() => { killTree(c, "SIGKILL"); r(); }, 2000); c.once("exit", () => { clearTimeout(t); r(); }); }); }
async function waitForUi() { const d = Date.now() + 30000; while (Date.now() < d) { try { const r = await fetch(ui); if (r.ok) return; } catch { await new Promise((r) => setTimeout(r, 250)); } } throw new Error(`ui not ready: ${ui}`); }

async function assertAssetDesktopContained(page, label, maximumSourceHeight = Number.POSITIVE_INFINITY) {
  const shell = page.getByTestId("asset-registry");
  await shell.evaluate((element) => { element.scrollTop = 0; });
  const layout = await page.evaluate(() => {
    const shell = document.querySelector('[data-testid="asset-registry"]');
    const agentList = document.querySelector('[data-testid="test-agent-list"]');
    const source = document.querySelector('[data-testid="test-source-code"]');
    const scroller = document.querySelector('[data-testid="test-source-code"] .cm-scroller');
    if (!shell || !agentList || !source || !scroller) return null;
    const shellRect = shell.getBoundingClientRect();
    const agentListRect = agentList.getBoundingClientRect();
    const sourceRect = source.getBoundingClientRect();
    const topbar = document.querySelector(".topbar")?.getBoundingClientRect();
    return {
      shellClientHeight: shell.clientHeight,
      shellScrollHeight: shell.scrollHeight,
      shellOverflowY: getComputedStyle(shell).overflowY,
      shellBottom: shellRect.bottom,
      agentListBottom: agentListRect.bottom,
      sourceBottom: sourceRect.bottom,
      sourceHeight: sourceRect.height,
      scrollerClientHeight: scroller.clientHeight,
      scrollerScrollHeight: scroller.scrollHeight,
      topbarHeight: topbar?.height ?? 0,
      viewportHeight: window.innerHeight,
    };
  });
  if (!layout) throw new Error(`${label}资产工作区关键元素缺失`);
  if (layout.shellOverflowY !== "hidden" || layout.shellScrollHeight > layout.shellClientHeight + 1) {
    throw new Error(`${label}资产中心不应产生纵向滚动: ${JSON.stringify(layout)}`);
  }
  if (layout.shellBottom > layout.viewportHeight + 1 || layout.topbarHeight < 55) {
    throw new Error(`${label}资产中心或 Topbar 超出视口约束: ${JSON.stringify(layout)}`);
  }
  if (Math.abs(layout.agentListBottom - layout.sourceBottom) > 2) {
    throw new Error(`${label}Agent 列表与源码区底边未对齐: ${JSON.stringify(layout)}`);
  }
  if (layout.sourceBottom > layout.shellBottom + 1 || layout.sourceHeight > maximumSourceHeight) {
    throw new Error(`${label}源码区高度未收敛到资产中心: ${JSON.stringify(layout)}`);
  }
  if (layout.scrollerScrollHeight <= layout.scrollerClientHeight) {
    throw new Error(`${label}源码应由 CodeMirror 内部滚动: ${JSON.stringify(layout)}`);
  }
}

async function assertAssetMobileReachable(page, label) {
  const shell = page.getByTestId("asset-registry");
  await shell.evaluate((element) => { element.scrollTop = 0; });
  const before = await shell.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: getComputedStyle(element).overflowY,
      shellBottom: rect.bottom,
      viewportHeight: window.innerHeight,
    };
  });
  if (before.overflowY !== "auto" || before.scrollHeight <= before.clientHeight) {
    throw new Error(`${label}资产中心应在固定可视高度内独立滚动: ${JSON.stringify(before)}`);
  }
  if (before.shellBottom > before.viewportHeight + 1) {
    throw new Error(`${label}资产中心超出视口约束: ${JSON.stringify(before)}`);
  }

  await shell.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await page.waitForFunction(() => {
    const element = document.querySelector('[data-testid="asset-registry"]');
    return !!element && element.scrollTop > 0 && element.scrollTop + element.clientHeight >= element.scrollHeight - 1;
  });
  const bottom = await page.evaluate(() => {
    const shellRect = document.querySelector('[data-testid="asset-registry"]')?.getBoundingClientRect();
    const detailRect = document.querySelector('[data-testid="test-asset-detail"]')?.getBoundingClientRect();
    return { shellBottom: shellRect?.bottom ?? 0, detailBottom: detailRect?.bottom ?? Number.POSITIVE_INFINITY };
  });
  if (bottom.detailBottom > bottom.shellBottom + 1) {
    throw new Error(`${label}滚动到底后测试资产详情仍不可达: ${JSON.stringify(bottom)}`);
  }
  await shell.evaluate((element) => { element.scrollTop = 0; });
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

async function ensureRealTargetAgent() {
  if (!REAL) return "shop-bot";
  const stamp = Date.now().toString(36);
  const agentId = `asset-audit-${stamp}`;
  await apiJson("/api/agent-registry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: `资产验收 Agent ${stamp}`, agent_id: agentId }),
  });
  return agentId;
}

const agents = [{ agent_id: "soc-ops", name: "SOC 运营", category: "", workspace_dir: "", created_at: ts, status: "active" }, { agent_id: "shop-bot", name: "电商客服", category: "", workspace_dir: "", created_at: ts, status: "active" }];
const testAssets = Array.from({ length: 24 }, (_, index) => {
  const agentId = index === 0 ? "soc-ops" : `batch-agent-${String(index).padStart(2, "0")}`;
  return {
    agent_id: agentId,
    agent_name: index === 0 ? "SOC 运营" : `批量 Agent ${String(index).padStart(2, "0")}`,
    agent_status: "active",
    suite: { agent_id: agentId, commit_sha: (index + 1).toString(16).padStart(40, "0"), tests_directory_present: true, readme_present: true, test_file_count: 1, test_files: ["tests/test_alert.py"], suite_digest: `suite-${index}`, diagnostics: [] },
    latest_run: null,
    schedule: { schedule_id: null, agent_id: agentId, enabled: false, cron_expression: "0 2 * * *", timezone: "UTC", next_run_at: null, created_at: null, updated_at: null },
  };
});
const testSourceLines = [
  "class TestAlert:",
  "    def test_open(self):",
  "        assert True",
  "",
  ...Array.from({ length: 90 }, (_, index) => `# source filler ${index + 1}`),
  "",
  "async def test_async_tail():",
  "    assert True",
];
const testSourceTailLine = testSourceLines.indexOf("async def test_async_tail():") + 1;
const testSourceSymbols = [
  { kind: "class", name: "TestAlert", qualified_name: "TestAlert", line: 1 },
  { kind: "function", name: "test_open", qualified_name: "TestAlert.test_open", line: 2 },
  { kind: "async_function", name: "test_async_tail", qualified_name: "test_async_tail", line: testSourceTailLine },
];
function defaultPayload(path) {
  if (path === "/health") return { status: "ok", model: "governance-mock" };
  if (path === "/api/agent-registry") return agents;
  if (path === "/api/agent-test-assets") return testAssets;
  if (path === "/api/agent-test-runs/history") return { items: [], next_cursor: null };
  if (path === "/api/agent-registry/soc-ops/test-suite/file") return { agent_id: "soc-ops", commit_sha: testAssets[0].suite.commit_sha, path: "tests/test_alert.py", content: testSourceLines.join("\n"), line_count: testSourceLines.length, symbols: testSourceSymbols };
  if (path === "/api/agent-registry/soc-ops/test-schedule/events") return [];
  if (path === "/api/sessions" || path === "/api/agents" || path === "/api/skills" || path === "/api/improvements" || path === "/api/agent-change-sets" || path === "/api/agent-releases" || path === "/api/agent-test-runs") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  return {};
}

async function main() {
  const server = REAL ? null : startVite();
  try {
    await waitForUi();
    const inheritTargetAgentId = await ensureRealTargetAgent();
    const assetTitle = `误报归因法 ${Date.now().toString(36)}`;
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("console", (m) => { if (m.type() === "error") console.error("PAGE_CONSOLE_ERR:", m.text()); });
    page.on("pageerror", (e) => console.error("PAGE_ERR:", e.message));
    await page.addInitScript(([b, key]) => { window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: b, apiKey: key })); }, [apiBase, apiKey]);
    const stateRef = { assets: [], count: 0 };
    const requestCounts = { testAssets: 0, history: 0, scheduleEvents: 0, governance: 0 };
    if (!REAL) {
      await page.route("**/*", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        if (url.hostname !== "runtime.test") return route.continue();
        const path = url.pathname;
        const method = req.method();
        const json = (r, body, status = 200) => r.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
        if (method === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
        if (path === "/api/agent-test-assets" && method === "GET") requestCounts.testAssets += 1;
        if (path === "/api/agent-test-runs/history" && method === "GET") requestCounts.history += 1;
        if (path === "/api/agent-registry/soc-ops/test-schedule/events" && method === "GET") requestCounts.scheduleEvents += 1;
        if (path === "/api/assets" && method === "GET") {
          requestCounts.governance += 1;
          const a = url.searchParams.get("agent_id");
          return json(route, a ? stateRef.assets.filter((x) => x.agent_id === a) : stateRef.assets);
        }
        if (path === "/api/assets" && method === "POST") {
          const b = req.postDataJSON();
          const asset = { asset_id: `ast-${++stateRef.count}`, agent_id: b.agent_id, asset_type: b.asset_type, title: b.title, body: b.body || "", source_improvement_id: b.source_improvement_id || "", inherited_from: "", created_at: ts, updated_at: ts };
          stateRef.assets.push(asset);
          return json(route, asset, 201);
        }
        const inheritM = path.match(/^\/api\/assets\/([^/]+)\/inherit$/);
        if (inheritM && method === "POST") {
          const src = stateRef.assets.find((x) => x.asset_id === decodeURIComponent(inheritM[1]));
          const b = req.postDataJSON();
          if (!src) return json(route, { detail: "not found" }, 404);
          const asset = { ...src, asset_id: `ast-${++stateRef.count}`, agent_id: b.target_agent_id, inherited_from: src.asset_id };
          stateRef.assets.push(asset);
          return json(route, asset, 201);
        }
        return json(route, defaultPayload(path));
      });
    }
    try {
      await page.goto(ui, { waitUntil: "domcontentloaded" });
      await page.getByTestId("topbar-agent-switcher").waitFor({ timeout: 20000 });
      // 资产 Registry 经一级导航「资产复利」(nav-asset) 进入（四阶段改进治理 W3 修订，资产复利为第三支柱）。
      await page.getByTestId("nav-asset").click();
      await page.getByTestId("asset-registry").waitFor({ timeout: 20000 });
      if (!REAL) {
        await page.getByTestId("agent-test-assets").waitFor({ timeout: 15000 });
        await page.getByTestId("test-asset-workspace").waitFor({ timeout: 15000 });
        if (await page.getByTestId("test-asset-card-grid").count()) throw new Error("旧测试资产卡片网格不应继续存在");
        if ((await page.getByTestId("test-asset-agent-item").count()) !== testAssets.length) throw new Error("左侧导航未完整投影多 Agent 测试资产");
        const navigatorBox = await page.getByTestId("test-agent-navigator").boundingBox();
        const detailBox = await page.getByTestId("test-asset-detail").boundingBox();
        if (!navigatorBox || !detailBox || detailBox.x <= navigatorBox.x || detailBox.width <= navigatorBox.width * 2.4) {
          throw new Error(`测试资产应为窄左栏 + 宽右栏布局: navigator=${JSON.stringify(navigatorBox)} detail=${JSON.stringify(detailBox)}`);
        }
        const navigatorScrolls = await page.getByTestId("test-agent-list").evaluate((element) => element.scrollHeight > element.clientHeight);
        if (!navigatorScrolls) throw new Error("多 Agent 导航应在固定高度内独立滚动");
        await page.getByTestId("test-agent-search").fill("批量 Agent 23");
        if ((await page.getByTestId("test-asset-agent-item").count()) !== 1) throw new Error("Agent 搜索没有收窄左侧导航");
        await page.getByTestId("test-agent-search").fill("");
        await page.getByTestId("test-asset-agent-item").first().click();
        await page.getByTestId("test-file-browser").waitFor({ timeout: 15000 });
        await page.getByTestId("test-file-select").waitFor({ timeout: 15000 });
        const compactTitle = await page.evaluate(() => {
          const title = document.querySelector(".test-asset-detail-title h3");
          const commit = document.querySelector(".test-asset-detail-commit");
          if (!title || !commit) return null;
          const titleRect = title.getBoundingClientRect();
          const commitRect = commit.getBoundingClientRect();
          const titleStyle = getComputedStyle(title);
          const commitStyle = getComputedStyle(commit);
          return {
            titleBottom: titleRect.bottom,
            commitBottom: commitRect.bottom,
            titleFontSize: Number.parseFloat(titleStyle.fontSize),
            commitFontSize: Number.parseFloat(commitStyle.fontSize),
            titleColor: titleStyle.color,
            commitColor: commitStyle.color,
          };
        });
        if (!compactTitle || Math.abs(compactTitle.titleBottom - compactTitle.commitBottom) > 3 || compactTitle.commitFontSize >= compactTitle.titleFontSize || compactTitle.commitColor === compactTitle.titleColor) {
          throw new Error(`Agent 名称与生效 commit 应同排且弱化 commit: ${JSON.stringify(compactTitle)}`);
        }
        if (await page.locator(".test-assets-toolbar").count()) throw new Error("测试资产页不应保留重复标题和局部刷新工具栏");
        if ((await page.getByRole("button", { name: "刷新", exact: true }).count()) !== 1) throw new Error("测试资产页只应保留 Topbar 刷新入口");
        const sourceBox = await page.getByTestId("test-source-code").boundingBox();
        if (!sourceBox || sourceBox.width < detailBox.width * 0.9 || sourceBox.height < 500) {
          throw new Error(`源码区未充分占用右栏: source=${JSON.stringify(sourceBox)} detail=${JSON.stringify(detailBox)}`);
        }
        await assertAssetDesktopContained(page, "桌面端");
        await page.setViewportSize({ width: 1280, height: 720 });
        await assertAssetDesktopContained(page, "紧凑桌面端", 500);
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.getByTestId("test-source-symbol-rail").waitFor({ timeout: 15000 });
        const symbolMarks = page.getByTestId("test-source-symbol-mark");
        if ((await symbolMarks.count()) !== testSourceSymbols.length) throw new Error("符号轨道未完整投影 pytest 源码纲要");
        if (await page.locator(".test-source-symbols").count()) throw new Error("旧静态符号标签行不应继续存在");
        const inactiveMark = page.locator('[data-testid="test-source-symbol-mark"]:not(.is-active)').first();
        await inactiveMark.waitFor({ timeout: 5000 });
        const inactiveMarkStyle = await inactiveMark.locator("span").evaluate((element) => {
          const color = getComputedStyle(element).backgroundColor;
          const channels = color.match(/[\d.]+/g)?.map(Number) ?? [];
          return { color, red: channels[0] ?? 0, green: channels[1] ?? 0, blue: channels[2] ?? 0, alpha: channels[3] ?? 1 };
        });
        const inactiveChannels = [inactiveMarkStyle.red, inactiveMarkStyle.green, inactiveMarkStyle.blue];
        if (inactiveMarkStyle.alpha < 0.7 || Math.min(...inactiveChannels) < 120 || Math.max(...inactiveChannels) - Math.min(...inactiveChannels) > 50) {
          throw new Error(`非 active 符号刻度应保持可见浅灰色: ${JSON.stringify(inactiveMarkStyle)}`);
        }
        await symbolMarks.nth(1).hover();
        const symbolPreview = page.getByTestId("test-source-symbol-preview");
        await symbolPreview.waitFor({ timeout: 5000 });
        if (!(await symbolPreview.textContent())?.includes("TestAlert.test_open")) throw new Error("符号预览未展示类方法全限定名");
        const codeScroller = page.getByTestId("test-source-code").locator(".cm-scroller");
        await symbolMarks.last().click();
        await page.waitForFunction(() => (document.querySelector('[data-testid="test-source-code"] .cm-scroller')?.scrollTop || 0) > 100);
        if (!(await symbolMarks.last().evaluate((element) => element.classList.contains("is-active")))) throw new Error("点击符号后目标标记未激活");
        await symbolMarks.first().focus();
        await symbolMarks.first().press("Enter");
        await page.waitForFunction(() => (document.querySelector('[data-testid="test-source-code"] .cm-scroller')?.scrollTop || 0) < 100);
        await codeScroller.evaluate((element) => element.scrollTo({ top: element.scrollHeight, behavior: "auto" }));
        await page.waitForFunction(() => document.querySelectorAll('[data-testid="test-source-symbol-mark"]')[2]?.classList.contains("is-active"));

        const beforeRefresh = { ...requestCounts };
        const refreshedCurrentTab = Promise.all([
          page.waitForResponse((response) => new URL(response.url()).pathname === "/api/agent-test-assets"),
          page.waitForResponse((response) => new URL(response.url()).pathname === "/api/agent-test-runs/history"),
          page.waitForResponse((response) => new URL(response.url()).pathname === "/api/agent-registry/soc-ops/test-schedule/events"),
        ]);
        await page.getByTestId("topbar-refresh").click();
        await refreshedCurrentTab;
        if (requestCounts.testAssets <= beforeRefresh.testAssets || requestCounts.history <= beforeRefresh.history || requestCounts.scheduleEvents <= beforeRefresh.scheduleEvents) {
          throw new Error(`Topbar 未刷新测试资产当前页签: before=${JSON.stringify(beforeRefresh)} after=${JSON.stringify(requestCounts)}`);
        }
        if (requestCounts.governance !== beforeRefresh.governance) throw new Error("刷新测试资产时不应请求隐藏的治理资产页签");
        await page.getByTestId("test-assets-tab-history").click();
        await page.getByTestId("test-run-history").waitFor({ timeout: 15000 });
        await page.locator(".test-history-filters select").first().selectOption("passed");
        await page.getByTestId("test-assets-tab-files").click();
        await page.getByTestId("test-source-code").waitFor({ timeout: 15000 });
        await page.getByTestId("test-assets-tab-schedule").click();
        await page.getByTestId("test-schedule-panel").waitFor({ timeout: 15000 });
        await page.setViewportSize({ width: 390, height: 844 });
        await page.getByTestId("test-assets-tab-files").click();
        await page.getByTestId("test-source-symbol-mark").nth(1).focus();
        const mobilePreviewBox = await page.getByTestId("test-source-symbol-preview").boundingBox();
        const mobileOverflows = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
        if (mobileOverflows || !mobilePreviewBox || mobilePreviewBox.x < 0 || mobilePreviewBox.x + mobilePreviewBox.width > 390) {
          throw new Error(`移动端符号轨道或预览越界: preview=${JSON.stringify(mobilePreviewBox)} overflow=${mobileOverflows}`);
        }
        await assertAssetMobileReachable(page, "移动端");
        await page.setViewportSize({ width: 1440, height: 900 });
      }
      await page.getByTestId("asset-center-tab-governance").click();
      await page.getByTestId("governance-asset-registry").waitFor({ timeout: 15000 });
      if (!REAL) {
        const beforeGovernanceRefresh = { ...requestCounts };
        const governanceRefresh = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/assets" && response.request().method() === "GET");
        await page.getByTestId("topbar-refresh").click();
        await governanceRefresh;
        if (requestCounts.governance <= beforeGovernanceRefresh.governance) throw new Error("Topbar 未刷新治理资产当前页签");
        if (requestCounts.testAssets !== beforeGovernanceRefresh.testAssets) throw new Error("刷新治理资产时不应请求隐藏的测试资产页签");
        if ((await page.getByRole("button", { name: "刷新", exact: true }).count()) !== 1) throw new Error("治理资产页只应保留 Topbar 刷新入口");
      }
      // Playground 的顶栏运行 Agent 必须是具体对象；资产页用自己的范围筛选查看跨 Agent 资产。
      await page.getByTestId("asset-scope-filter").selectOption("");
      await page.getByTestId("asset-browser-toolbar").waitFor({ timeout: 15000 });
      if (await page.getByTestId("asset-create-title").isVisible().catch(() => false)) throw new Error("asset create form should not be visible before opening drawer");
      const initialCount = await page.getByTestId("asset-item").count();

      // 沉淀一个方法论资产。
      await page.getByTestId("asset-create-open").click();
      await page.getByTestId("asset-create-drawer").waitFor({ timeout: 15000 });
      if (REAL) await page.getByTestId("asset-create-agent").selectOption("security-operations-expert");
      if (await page.getByTestId("asset-create-type").locator('option[value="test_dataset"]').count()) {
        throw new Error("removed database-backed test asset must not be creatable through the generic asset form");
      }
      if (await page.getByTestId("asset-create-type").locator('option[value="regression"]').count()) {
        throw new Error("legacy regression assets must not be creatable through the generic asset form");
      }
      await page.getByTestId("asset-create-type").selectOption("methodology");
      await page.getByTestId("asset-create-title").fill(assetTitle);
      await page.getByTestId("asset-create-submit").click();
      await page.getByTestId("asset-create-drawer").waitFor({ state: "detached", timeout: 15000 }).catch(() => {});
      const createdAsset = page.getByTestId("asset-item").filter({ hasText: assetTitle }).first();
      await createdAsset.waitFor({ timeout: 15000 });
      await createdAsset.getByTestId("asset-provenance").waitFor({ timeout: 15000 });
      const afterCreateCount = await page.getByTestId("asset-item").count();
      if (afterCreateCount < initialCount + 1) throw new Error(`expected asset count to grow after create: ${initialCount} -> ${afterCreateCount}`);

      // 跨 Agent 继承复用 → 出现第 2 条（带「继承」标记）。
      await createdAsset.getByTestId("asset-inherit-target").selectOption(inheritTargetAgentId);
      await createdAsset.getByTestId("asset-inherit-submit").click();
      const inheritedAssets = page.getByTestId("asset-item").filter({ hasText: assetTitle }).filter({ has: page.getByTestId("asset-inherited") });
      await inheritedAssets.first().waitFor({ timeout: 15000 });
      const afterInheritCount = await page.getByTestId("asset-item").count();
      if (afterInheritCount < afterCreateCount + 1) throw new Error(`expected asset count to grow after inherit: ${afterCreateCount} -> ${afterInheritCount}`);
      if ((await page.getByTestId("asset-provenance").count()) < afterInheritCount) throw new Error("expected provenance for every visible asset");

      // 负路径（仅 mock）：业务 Agent 列表为空时，「沉淀」按钮应禁用 + 给空态提示 + 不发 POST /api/assets（不静默吞）。
      const scenarios = ["test_asset_projection", "test_asset_master_detail_layout", "test_asset_many_agent_scroll", "test_asset_detail_compact_commit", "test_asset_desktop_contained", "test_asset_compact_desktop_contained", "test_asset_column_bottom_alignment", "test_source_internal_scroll", "test_asset_mobile_vertical_reachability", "test_topbar_fixed_height", "test_source_full_width", "test_source_view", "test_source_symbol_rail", "test_source_symbol_inactive_visibility", "test_source_symbol_keyboard_navigation", "test_source_symbol_scroll_tracking", "test_source_symbol_mobile_bounds", "test_topbar_refresh_current_asset_tab", "test_local_refresh_removed", "test_source_persists_after_history_filter", "test_run_history", "test_schedule_view", "asset_create", "asset_inherit", "asset_provenance"];
      if (!REAL) {
        const page2 = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        await page2.addInitScript(([b, key]) => { window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: b, apiKey: key })); }, [apiBase, apiKey]);
        let assetPosts = 0;
        await page2.route("**/*", async (route) => {
          const req = route.request();
          const url = new URL(req.url());
          if (url.hostname !== "runtime.test") return route.continue();
          const path = url.pathname;
          const method = req.method();
          const j = (r, body, status = 200) => r.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
          if (method === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
          if (path === "/api/agent-registry") return j(route, []); // 业务 Agent 列表为空
          if (path === "/api/assets" && method === "POST") { assetPosts += 1; return j(route, {}, 201); }
          if (path === "/api/assets") return j(route, []);
          return j(route, defaultPayload(path));
        });
        try {
          await page2.goto(ui, { waitUntil: "domcontentloaded" });
          await page2.getByTestId("nav-asset").click();
          await page2.getByTestId("asset-registry").waitFor({ timeout: 20000 });
          await page2.getByTestId("asset-center-tab-governance").click();
          await page2.getByTestId("asset-create-open").click();
          await page2.getByTestId("asset-create-drawer").waitFor({ timeout: 15000 });
          await page2.getByTestId("asset-create-title").fill("无归属也不应静默吞");
          await page2.getByTestId("asset-create-no-agent").waitFor({ timeout: 5000 });
          if (!(await page2.getByTestId("asset-create-submit").isDisabled())) throw new Error("沉淀按钮在无可解析业务 Agent 时应禁用");
          await page2.getByTestId("asset-create-submit").click({ force: true }).catch(() => {});
          await page2.waitForTimeout(500);
          if (assetPosts !== 0) throw new Error(`无可解析业务 Agent 时不应发出 POST /api/assets，实际 ${assetPosts}`);
          scenarios.push("asset_create_blocked_without_agent");
        } finally {
          await page2.close();
        }
      }

      console.log(JSON.stringify({ status: "passed", mode: REAL ? "real-container" : "mock", ui_base: ui, scenarios }, null, 2));
    } finally {
      await browser.close();
    }
  } finally {
    await stopChild(server);
  }
}
main().then(() => process.exit(0)).catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(1); });
