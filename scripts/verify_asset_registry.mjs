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
function defaultPayload(path) {
  if (path === "/health") return { status: "ok", model: "governance-mock" };
  if (path === "/api/agent-registry") return agents;
  if (path === "/api/sessions" || path === "/api/agents" || path === "/api/skills" || path === "/api/improvements" || path === "/api/agent-change-sets" || path === "/api/agent-releases" || path === "/api/test-datasets") return [];
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
    if (!REAL) {
      await page.route("**/*", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        if (url.hostname !== "runtime.test") return route.continue();
        const path = url.pathname;
        const method = req.method();
        const json = (r, body, status = 200) => r.fulfill({ status, contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body) });
        if (method === "OPTIONS") return route.fulfill({ status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "*" } });
        if (path === "/api/assets" && method === "GET") {
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
      // Playground 的顶栏运行 Agent 必须是具体对象；资产页用自己的范围筛选查看跨 Agent 资产。
      await page.getByTestId("asset-scope-filter").selectOption("");
      await page.getByTestId("asset-browser-toolbar").waitFor({ timeout: 15000 });
      if (await page.getByTestId("asset-create-title").isVisible().catch(() => false)) throw new Error("asset create form should not be visible before opening drawer");
      const initialCount = await page.getByTestId("asset-item").count();

      // 沉淀一个方法论资产。
      await page.getByTestId("asset-create-open").click();
      await page.getByTestId("asset-create-drawer").waitFor({ timeout: 15000 });
      if (REAL) await page.getByTestId("asset-create-agent").selectOption("main-agent");
      if (await page.getByTestId("asset-create-type").locator('option[value="test_dataset"]').count()) {
        throw new Error("typed TestDataset must not be creatable through the generic asset form");
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
      const scenarios = ["asset_create", "asset_inherit", "asset_provenance"];
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
