#!/usr/bin/env node
// Verify that a UI opened through a non-loopback host does not keep using localhost as the browser API base.
import { spawn } from "node:child_process";
import { networkInterfaces } from "node:os";
import { createRequire } from "node:module";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.env.REMOTE_API_BASE_UI_PORT || 55222);
const host = process.env.REMOTE_API_BASE_HOST || firstRoutableIPv4();
if (!host) {
  throw new Error("No non-loopback IPv4 address found for remote API base verification");
}
const uiBase = `http://${host}:${port}`;
const expectedApiBase = `http://${host}:58080`;
const forbiddenApiBases = new Set(["http://localhost:58080", "http://127.0.0.1:58080"]);

function firstRoutableIPv4() {
  for (const entries of Object.values(networkInterfaces())) {
    for (const entry of entries || []) {
      if (entry.family === "IPv4" && !entry.internal) return entry.address;
    }
  }
  return "";
}

function startVite() {
  const child = spawn("pnpm", ["--dir", "frontend", "exec", "vite", "--host", "0.0.0.0", "--port", String(port), "--strictPort"], {
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
    env: {
      ...process.env,
      VITE_RUNTIME_API_BASE: "http://localhost:58080",
      VITE_RUNTIME_API_KEY: "",
    },
  });
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  return child;
}

function killTree(child, signal) {
  try { process.kill(-child.pid, signal); } catch { try { child.kill(signal); } catch { /* already gone */ } }
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  killTree(child, "SIGTERM");
  await new Promise((resolve) => {
    const timeout = setTimeout(() => { killTree(child, "SIGKILL"); resolve(); }, 2000);
    child.once("exit", () => { clearTimeout(timeout); resolve(); });
  });
}

async function waitForUi() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(uiBase);
      if (response.ok) return;
    } catch { /* wait */ }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Vite did not become ready at ${uiBase}`);
}

function json(route, data, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify(data),
  });
}

function routeData(path) {
  if (path === "/health") return { status: "ok", model: "remote-api-base-mock", provider_key_configured: true };
  if (path === "/api/sessions" || path === "/api/agents" || path === "/api/skills" || path === "/api/agent-change-sets" || path === "/api/agent-releases") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-registry") return [
    { agent_id: "main-agent", name: "Main Agent", category: "baseline", workspace_dir: "/runtime/main", created_at: "2026-06-29T00:00:00Z", status: "active" },
  ];
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v-remote-api-base", commit_sha: "mock", created_at: "2026-06-29T00:00:00Z", reason: "current" };
  return {};
}

async function waitForCondition(check, message, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(message);
}

async function main() {
  const server = startVite();
  const seenPaths = new Set();
  const forbiddenRequests = [];
  try {
    await waitForUi();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 } });
    await page.addInitScript(() => {
      window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase: "http://localhost:58080", apiKey: "" }));
      window.localStorage.removeItem("playground-session-messages");
      window.localStorage.removeItem("playground-active-session");
    });
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === uiBase) return route.continue();
      if (forbiddenApiBases.has(url.origin)) {
        forbiddenRequests.push(request.url());
        return json(route, { detail: "forbidden localhost API base" }, 599);
      }
      if (url.origin === expectedApiBase) {
        seenPaths.add(url.pathname);
        return json(route, routeData(url.pathname));
      }
      return route.continue();
    });
    await page.goto(uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 15000 });
    await waitForCondition(() => seenPaths.has("/health") && seenPaths.has("/api/agent-registry"), "UI did not call the API through the browser host");
    await waitForCondition(async () => {
      const stored = await page.evaluate(() => JSON.parse(window.localStorage.getItem("runtime-client-config") || "{}").apiBase);
      return stored === expectedApiBase;
    }, "legacy localhost apiBase was not migrated to the browser host");
    await browser.close();
    if (forbiddenRequests.length) {
      throw new Error(`UI still called loopback API base: ${JSON.stringify(forbiddenRequests)}`);
    }
    console.log(JSON.stringify({ ok: true, uiBase, expectedApiBase, seenPaths: Array.from(seenPaths).sort() }, null, 2));
  } finally {
    await stopChild(server);
  }
}

main().catch((error) => {
  console.error(`verify_remote_api_base failed: ${error?.stack || error}`);
  process.exit(1);
});
