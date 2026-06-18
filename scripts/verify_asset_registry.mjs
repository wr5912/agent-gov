// v2.7 W3 资产 Registry 复利中心 UI 验收（mock 后端）：沉淀资产 → 跨 Agent 继承复用。
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import process from "node:process";
const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");
const repoRoot = new URL("..", import.meta.url).pathname;
const port = Number(process.env.ASSET_UI_PORT || 55193);
const ui = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const ts = "2026-06-17T00:00:00Z";

function startVite() {
  const c = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"], detached: true });
  c.stdout.on("data", () => {}); c.stderr.on("data", () => {});
  return c;
}
function killTree(c, s) { try { process.kill(-c.pid, s); } catch { try { c.kill(s); } catch {} } }
async function stopChild(c) { if (c.exitCode !== null) return; killTree(c, "SIGTERM"); await new Promise((r) => { const t = setTimeout(() => { killTree(c, "SIGKILL"); r(); }, 2000); c.once("exit", () => { clearTimeout(t); r(); }); }); }
async function waitForVite() { const d = Date.now() + 30000; while (Date.now() < d) { try { const r = await fetch(ui); if (r.ok) return; } catch { await new Promise((r) => setTimeout(r, 250)); } } throw new Error("vite not ready"); }

const agents = [{ agent_id: "soc-ops", name: "SOC 运营", category: "", workspace_dir: "", created_at: ts, status: "active" }, { agent_id: "shop-bot", name: "电商客服", category: "", workspace_dir: "", created_at: ts, status: "active" }];
function defaultPayload(path) {
  if (path === "/health") return { status: "ok", model: "governance-mock" };
  if (path === "/api/agent-registry") return agents;
  if (path === "/api/sessions" || path === "/api/agents" || path === "/api/skills" || path === "/api/improvements" || path === "/api/agent-change-sets" || path === "/api/agent-releases") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  if (path.startsWith("/api/automation-policy")) return { agent_id: "soc-ops", mode: "off" };
  return {};
}

async function main() {
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("console", (m) => { if (m.type() === "error") console.error("PAGE_CONSOLE_ERR:", m.text()); });
    page.on("pageerror", (e) => console.error("PAGE_ERR:", e.message));
    await page.addInitScript((b) => { window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: b, apiKey: "" })); }, apiBase);
    const stateRef = { assets: [], count: 0 };
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
        const asset = { asset_id: `ast-${++stateRef.count}`, agent_id: b.agent_id, asset_type: b.asset_type, title: b.title, body: b.body || "", source_improvement_id: "", inherited_from: "", created_at: ts, updated_at: ts };
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
    try {
      await page.goto(ui, { waitUntil: "domcontentloaded" });
      await page.getByTestId("topbar-agent-switcher").waitFor({ timeout: 20000 });
      // 资产 Registry 现经 Settings 进入（v2.7 §2 导航收敛，资产不再是一级导航）。
      await page.getByTestId("open-settings").click();
      await page.getByTestId("settings-panel").waitFor({ timeout: 15000 });
      await page.getByTestId("settings-open-asset").click();
      await page.getByTestId("asset-registry").waitFor({ timeout: 20000 });

      // 沉淀一个方法论资产。
      await page.getByTestId("asset-create-type").selectOption("methodology");
      await page.getByTestId("asset-create-title").fill("误报归因法");
      await page.getByTestId("asset-create-submit").click();
      await page.getByTestId("asset-item").first().waitFor({ timeout: 15000 });
      if ((await page.getByTestId("asset-item").count()) !== 1) throw new Error("expected 1 asset after create");

      // 跨 Agent 继承复用 → 出现第 2 条（带「继承」标记）。
      await page.getByTestId("asset-inherit-target").first().selectOption("shop-bot");
      await page.getByTestId("asset-inherit-submit").first().click();
      await page.locator('[data-testid="asset-item"]').nth(1).waitFor({ timeout: 15000 });
      await page.getByTestId("asset-inherited").first().waitFor({ timeout: 15000 });
      if ((await page.getByTestId("asset-item").count()) !== 2) throw new Error("expected 2 assets after inherit");

      console.log(JSON.stringify({ status: "passed", ui_base: ui, scenarios: ["asset_create", "asset_inherit"] }, null, 2));
    } finally {
      await browser.close();
    }
  } finally {
    await stopChild(server);
  }
}
main().then(() => process.exit(0)).catch((e) => { console.error(e instanceof Error ? e.stack || e.message : e); process.exit(1); });
