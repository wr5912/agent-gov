#!/usr/bin/env node
// Settings workspace package UI acceptance: empty state, raw seed hint,
// structured import failure, successful receipt, and restore action.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import process from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.env.AGENT_WORKSPACE_SETTINGS_UI_PORT || 55231);
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const screenshotDir =
  process.env.VERIFY_SCREENSHOT_DIR ||
  mkdtempSync(join(tmpdir(), "agentgov-workspace-settings-"));
const timestamp = "2026-07-16T00:00:00Z";
const previousCommit = "a".repeat(40);
const rollbackCommit = "b".repeat(40);
const importedCommit = "c".repeat(40);
const restoredCommit = "d".repeat(40);
const packageDigest = "e".repeat(64);
const treeDigest = "f".repeat(64);

const workspaceAgent = {
  agent_id: "workspace-agent",
  name: "Workspace Agent",
  category: "",
  workspace_dir: "/runtime/workspace-agent",
  created_at: timestamp,
  status: "active",
  origin: "user",
};
const secondWorkspaceAgent = {
  ...workspaceAgent,
  agent_id: "workspace-agent-2",
  name: "Workspace Agent 2",
  workspace_dir: "/runtime/workspace-agent-2",
};
const importedWorkspaceAgent = {
  ...workspaceAgent,
  agent_id: "imported-new",
  name: "Imported Package Agent",
  workspace_dir: "/runtime/imported-new",
};
const exportFilename = `workspace-agent-workspace-${previousCommit.slice(0, 12)}.tar.gz`;

function startVite() {
  const child = spawn(
    "pnpm",
    ["--dir", "frontend", "exec", "vite", "--host", "127.0.0.1", "--port", String(port), "--strictPort"],
    {
      cwd: repoRoot,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    },
  );
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  return child;
}

function killTree(child, signal) {
  try {
    process.kill(-child.pid, signal);
  } catch {
    try {
      child.kill(signal);
    } catch {
      // Already stopped.
    }
  }
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  killTree(child, "SIGTERM");
  await new Promise((resolve) => {
    const timeout = setTimeout(() => {
      killTree(child, "SIGKILL");
      resolve();
    }, 2000);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

async function waitForVite() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(uiBase);
      if (response.ok) return;
    } catch {
      // Wait for Vite.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Vite did not become ready at ${uiBase}`);
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*" },
    body: JSON.stringify(body),
  });
}

async function delayedJson(route, body, status = 200) {
  await new Promise((resolve) => setTimeout(resolve, 120));
  return json(route, body, status);
}

function defaultPayload(path) {
  if (path === "/health") return { status: "ok", model: "workspace-settings-mock" };
  if (path === "/v1/conversations") return { data: [] };
  if (path === "/api/agent-change-sets" || path === "/api/agent-releases") return [];
  if (path === "/api/config") return { mappings: [] };
  if (path === "/api/agent-repository") {
    return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  }
  if (path === "/api/agent-repository/current") {
    return {
      agent_version_id: previousCommit,
      commit_sha: previousCommit,
      created_at: timestamp,
      reason: "current",
    };
  }
  return {};
}

async function installMockRoutes(page, state) {
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === uiBase) return route.continue();
    if (url.hostname !== "runtime.test") return route.continue();
    const path = url.pathname;
    const method = request.method();
    if (method === "OPTIONS") {
      return route.fulfill({
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-headers": "*",
          "access-control-allow-methods": "*",
        },
      });
    }
    if (path === "/api/agent-registry" && method === "GET") {
      return json(route, state.agents);
    }
    if (path === "/api/agent-registry" && method === "POST") {
      const payload = request.postDataJSON();
      state.createRequests.push(payload);
      const created = {
        ...workspaceAgent,
        agent_id: payload.agent_id || "generated-agent",
        name: payload.name,
        workspace_dir: `/runtime/${payload.agent_id || "generated-agent"}`,
      };
      state.agents.push(created);
      return json(route, created, 201);
    }
    if (path === "/api/agent-registry/templates" && method === "GET") {
      return json(route, { templates: ["general"], seed_agent_ids: ["main-agent"] });
    }
    if (path === "/api/settings/openai-compat-agent" && method === "GET") {
      return json(route, { agent_id: null, configured: false, effective_agent_id: "main-agent" });
    }
    if (path === "/api/agent-repository/current" && method === "GET") {
      return json(route, defaultPayload(path));
    }
    if (path === "/api/agent-registry/workspace-agent/workspace/export" && method === "POST") {
      state.exportRequests += 1;
      await new Promise((resolve) => setTimeout(resolve, 120));
      return route.fulfill({
        status: 200,
        contentType: "application/gzip",
        headers: {
          "access-control-allow-origin": "*",
          "access-control-expose-headers": "Content-Disposition, X-Agent-Commit-SHA, X-Workspace-Package-SHA256, X-Workspace-Tree-SHA256",
          "content-disposition": `attachment; filename="${exportFilename}"`,
          "x-agent-commit-sha": previousCommit,
          "x-workspace-package-sha256": packageDigest,
          "x-workspace-tree-sha256": treeDigest,
        },
        body: Buffer.from("mock exported workspace"),
      });
    }
    if (path === "/api/agent-registry/workspace-agent-2/workspace/export" && method === "POST") {
      return delayedJson(
        route,
        {
          error_code: "WORKSPACE_MAINTENANCE_CONFLICT",
          detail: "workspace export is busy",
        },
        409,
      );
    }
    if (path === "/api/agent-registry/workspace-agent/workspace/import" && method === "POST") {
      state.importAttempts += 1;
      if (state.importAttempts === 1) {
        return delayedJson(
          route,
          {
            error_code: "WORKSPACE_PACKAGE_PATH_CONFLICT",
            detail: "Package file conflicts with descendant path: workspace/a",
          },
          422,
        );
      }
      return delayedJson(route, {
        action: "overwritten",
        agent: workspaceAgent,
        previous_commit_sha: previousCommit,
        current_commit_sha: importedCommit,
        package_sha256: packageDigest,
        tree_sha256: treeDigest,
        rollback_target_commit_sha: rollbackCommit,
        activation_mode: "next_turn",
      });
    }
    if (path === "/api/agent-registry/imported-new/workspace/import" && method === "POST") {
      state.newImportBodies.push(request.postData() || "");
      state.agents.push(importedWorkspaceAgent);
      return delayedJson(route, {
        action: "created",
        agent: importedWorkspaceAgent,
        previous_commit_sha: null,
        current_commit_sha: importedCommit,
        package_sha256: packageDigest,
        tree_sha256: treeDigest,
        rollback_target_commit_sha: null,
        activation_mode: "next_turn",
      });
    }
    if (path === "/api/agent-registry/workspace-agent/workspace/restore" && method === "POST") {
      state.restoreRequests.push(request.postDataJSON());
      return delayedJson(route, {
        action: "restored",
        agent: workspaceAgent,
        previous_commit_sha: importedCommit,
        restored_tree_commit_sha: rollbackCommit,
        current_commit_sha: restoredCommit,
        rollback_target_commit_sha: importedCommit,
        activation_mode: "next_turn",
      });
    }
    return json(route, defaultPayload(path));
  });
}

async function main() {
  mkdirSync(screenshotDir, { recursive: true });
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
    const state = {
      agents: [],
      createRequests: [],
      exportRequests: 0,
      importAttempts: 0,
      newImportBodies: [],
      restoreRequests: [],
    };
    await page.addInitScript((base) => {
      window.localStorage.setItem(
        "runtime-client-config",
        JSON.stringify({ apiBase: base, apiKey: "" }),
      );
    }, apiBase);
    page.on("dialog", (dialog) => dialog.accept());
    await installMockRoutes(page, state);
    try {
      await page.goto(uiBase, { waitUntil: "domcontentloaded" });
      await page.getByTestId("open-settings").click();
      await page.getByTestId("settings-agent-empty").waitFor({ timeout: 20000 });

      const sourceSelect = page.getByTestId("settings-agent-create-source");
      await sourceSelect.locator('option[value="seed:main-agent"]').waitFor({ state: "attached", timeout: 10000 });
      await sourceSelect.selectOption("seed:main-agent");
      await page.getByTestId("settings-agent-create-seed-hint").waitFor();
      await page.getByTestId("settings-agent-create-name").fill("Seed Clone");
      await page.getByTestId("settings-agent-create-id").fill("seed-clone");
      await page.getByTestId("settings-agent-create-submit").click();
      await page.getByTestId("settings-success").filter({ hasText: "已创建业务 Agent Seed Clone" }).waitFor();
      if (
        state.createRequests.length !== 1 ||
        state.createRequests[0].source_seed_id !== "main-agent" ||
        Object.hasOwn(state.createRequests[0], "template_id")
      ) {
        throw new Error(`seed creation payload is incorrect: ${JSON.stringify(state.createRequests)}`);
      }

      state.agents = [workspaceAgent, secondWorkspaceAgent];
      await page.getByTestId("settings-panel").locator('button[aria-label="关闭"]').click();
      await page.getByTestId("open-settings").click();
      await page.getByTestId("settings-agent-item").filter({ hasText: "workspace-agent" }).first().waitFor();

      const exposedHeaders = await page.evaluate(async ({ base, path }) => {
        const response = await fetch(`${base}${path}`, { method: "POST" });
        return {
          disposition: response.headers.get("content-disposition"),
          commit: response.headers.get("x-agent-commit-sha"),
          packageDigest: response.headers.get("x-workspace-package-sha256"),
          treeDigest: response.headers.get("x-workspace-tree-sha256"),
        };
      }, { base: apiBase, path: "/api/agent-registry/workspace-agent/workspace/export" });
      if (
        !exposedHeaders.disposition?.includes(exportFilename) ||
        exposedHeaders.commit !== previousCommit ||
        exposedHeaders.packageDigest !== packageDigest ||
        exposedHeaders.treeDigest !== treeDigest
      ) {
        throw new Error(`workspace export headers are not browser-visible: ${JSON.stringify(exposedHeaders)}`);
      }
      const exportRow = page.getByTestId("settings-workspace-agent-item").filter({ hasText: "workspace-agent" }).first();
      const downloadPromise = page.waitForEvent("download");
      const exportButton = exportRow.getByTestId("settings-agent-export");
      await exportButton.click();
      if ((await exportButton.getAttribute("aria-busy")) !== "true") {
        throw new Error("workspace export button did not expose pending state");
      }
      const download = await downloadPromise;
      if (download.suggestedFilename() !== exportFilename) {
        throw new Error(`workspace download filename mismatch: ${download.suggestedFilename()}`);
      }
      await page.getByTestId("settings-workspace-operation-feedback").filter({ hasText: "导出完成" }).waitFor();
      await page.getByTestId("settings-success").filter({ hasText: `commit ${previousCommit.slice(0, 12)}` }).waitFor();

      const failedExportRow = page.getByTestId("settings-workspace-agent-item").filter({ hasText: "workspace-agent-2" });
      await failedExportRow.getByTestId("settings-agent-export").click();
      const exportFailure = page.getByTestId("settings-workspace-operation-feedback");
      await exportFailure.filter({ hasText: "[WORKSPACE_MAINTENANCE_CONFLICT]" }).waitFor();
      if ((await exportFailure.getAttribute("data-operation")) !== "export") {
        throw new Error("workspace export failure is not attributed to export");
      }

      await page.getByTestId("settings-workspace-import-agent-id").fill("workspace-agent");
      await page.getByTestId("settings-workspace-import-file").setInputFiles({
        name: "workspace.tar.gz",
        mimeType: "application/gzip",
        buffer: Buffer.from("mock workspace archive"),
      });
      const importButton = page.getByTestId("settings-workspace-import-submit");
      await importButton.click();
      if ((await importButton.getAttribute("aria-busy")) !== "true") {
        throw new Error("workspace import button did not expose pending state");
      }
      const error = page.getByTestId("settings-error");
      await error.waitFor();
      const errorText = await error.textContent();
      if (!errorText?.includes("[WORKSPACE_PACKAGE_PATH_CONFLICT]")) {
        throw new Error(`structured workspace failure code is not visible: ${errorText}`);
      }
      await page.getByTestId("settings-workspace-operation-feedback").filter({ hasText: "[WORKSPACE_PACKAGE_PATH_CONFLICT]" }).waitFor();
      await page.screenshot({
        path: join(screenshotDir, "workspace-import-structured-failure.png"),
        fullPage: true,
      });

      await page.getByTestId("settings-workspace-import-agent-id").fill("workspace-agent-2");
      if (!(await page.getByTestId("settings-workspace-import-submit").isDisabled())) {
        throw new Error("editing the import target must clear the stale failed package");
      }
      await page.getByTestId("settings-workspace-operation-feedback").waitFor({ state: "detached" });
      await page.getByTestId("settings-error").waitFor({ state: "detached" });
      await page.getByTestId("settings-workspace-import-file").setInputFiles({
        name: "stale-workspace.tar.gz",
        mimeType: "application/gzip",
        buffer: Buffer.from("stale mock workspace archive"),
      });
      const secondRow = page.getByTestId("settings-workspace-agent-item").filter({ hasText: "workspace-agent" }).first();
      const fileChooserPromise = page.waitForEvent("filechooser");
      await secondRow.getByTestId("settings-agent-import").click();
      await fileChooserPromise;
      if (!(await page.getByTestId("settings-workspace-import-submit").isDisabled())) {
        throw new Error("choosing another overwrite target must clear the stale failed package");
      }
      await page.getByTestId("settings-workspace-import-agent-id").fill("workspace-agent");
      await page.getByTestId("settings-workspace-import-file").setInputFiles({
        name: "workspace.tar.gz",
        mimeType: "application/gzip",
        buffer: Buffer.from("mock workspace archive retry"),
      });
      await page.getByTestId("settings-workspace-import-submit").click();
      const receipt = page.getByTestId("settings-workspace-import-receipt");
      await receipt.waitFor();
      const receiptText = (await receipt.textContent()) || "";
      for (const expected of ["overwritten", previousCommit.slice(0, 12), importedCommit.slice(0, 12), packageDigest.slice(0, 12), treeDigest.slice(0, 12)]) {
        if (!receiptText.includes(expected)) {
          throw new Error(`workspace import receipt is missing ${expected}: ${receiptText}`);
        }
      }
      await page.getByTestId("settings-workspace-restore").waitFor();
      await page.screenshot({
        path: join(screenshotDir, "workspace-import-success-receipt.png"),
        fullPage: true,
      });

      await page.getByTestId("settings-workspace-restore").click();
      if ((await page.getByTestId("settings-workspace-restore").getAttribute("aria-busy")) !== "true") {
        throw new Error("workspace restore button did not expose pending state");
      }
      await page.getByTestId("settings-success").filter({ hasText: "已恢复 workspace-agent 导入前 workspace" }).waitFor();
      await page.getByTestId("settings-workspace-operation-feedback").filter({ hasText: "恢复完成" }).waitFor();
      await page.getByTestId("settings-workspace-restore").waitFor({ state: "detached" });
      if (state.restoreRequests.length !== 1) {
        throw new Error(`expected one restore request, got ${state.restoreRequests.length}`);
      }
      const restoreRequest = state.restoreRequests[0];
      if (
        restoreRequest.target_commit_sha !== rollbackCommit ||
        restoreRequest.expected_current_commit_sha !== importedCommit
      ) {
        throw new Error(`restore request did not preserve receipt commits: ${JSON.stringify(restoreRequest)}`);
      }
      if (state.exportRequests !== 2) {
        throw new Error(`expected browser header probe and UI export, got ${state.exportRequests} requests`);
      }

      await page.getByTestId("settings-workspace-import-agent-id").fill("imported-new");
      await page.getByTestId("settings-workspace-import-name").fill("Imported Package Agent");
      await page.getByTestId("settings-workspace-import-file").setInputFiles({
        name: "new-agent.tar.gz",
        mimeType: "application/gzip",
        buffer: Buffer.from("new agent workspace archive"),
      });
      await page.getByTestId("settings-workspace-import-submit").click();
      await page.getByTestId("settings-workspace-import-receipt").filter({ hasText: "created" }).waitFor();
      if (
        state.newImportBodies.length !== 1 ||
        !state.newImportBodies[0].includes('name="name"') ||
        !state.newImportBodies[0].includes("Imported Package Agent")
      ) {
        throw new Error(`new Agent import did not submit its name: ${JSON.stringify(state.newImportBodies)}`);
      }

      console.log(
        JSON.stringify(
          {
            status: "passed",
            mode: "mock",
            screenshots: screenshotDir,
            scenarios: [
              "agent_empty_state",
              "seed_raw_copy_hint",
              "seed_source_creation_payload",
              "workspace_export_download_and_headers",
              "workspace_export_local_failure",
              "structured_import_failure",
              "manual_target_change_clears_stale_package",
              "stale_failed_package_cleared",
              "import_receipt",
              "restore_action",
              "new_agent_workspace_import",
            ],
          },
          null,
          2,
        ),
      );
    } finally {
      await browser.close();
    }
  } finally {
    await stopChild(server);
  }
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error instanceof Error ? error.stack || error.message : error);
    process.exit(1);
  });
