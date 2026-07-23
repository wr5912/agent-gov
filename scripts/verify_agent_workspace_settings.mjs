#!/usr/bin/env node
// Settings Workspace package UI acceptance: package-only creation, structured
// import failure, successful receipt, export, and restore action.
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
  builtin: false,
  default: false,
  protected: false,
  requires_web_hitl: false,
};
const secondWorkspaceAgent = {
  ...workspaceAgent,
  agent_id: "workspace-agent-2",
  name: "Workspace Agent 2",
  workspace_dir: "/runtime/workspace-agent-2",
  protected: true,
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
    if (/^\/api\/agent-registry\/[^/]+\/test-suite$/.test(path) && method === "GET") {
      const agentId = decodeURIComponent(path.split("/")[3]);
      return json(route, {
        agent_id: agentId,
        commit_sha: previousCommit,
        tests_directory_present: true,
        readme_present: true,
        test_file_count: 2,
        test_files: ["tests/test_smoke.py", "tests/test_contract.py"],
        suite_digest: "mock-suite",
        diagnostics: [],
      });
    }
    if (path === "/api/agent-test-runs" && method === "GET") {
      return json(route, []);
    }
    if (path === "/api/settings/openai-compat-agent" && method === "GET") {
      return json(route, { agent_id: null, configured: false, effective_agent_id: "security-operations-expert" });
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
            error_code: "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH",
            detail:
              "导入被拒绝：包内来源 Agent ID “source-agent”与请求目标 Agent ID “workspace-agent”不一致；" +
              "系统不会改写包内身份。请确认导入目标，并将 agent.yaml.agent.id 改为与 URL 中的 agent_id 完全一致后重新打包。",
            field: "agent.yaml.agent.id",
            import_action: "overwrite",
            expected_agent_id: "workspace-agent",
            actual_agent_id: "source-agent",
            remediation:
              "确认导入目标，使 agent.yaml.agent.id 与 URL 中的 agent_id 完全一致后重新打包。",
          },
          409,
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
        import_record_id: "import-overwrite-1",
        test_file_count: 2,
        test_suite_status: "ready",
        test_suite_warnings: [],
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
        import_record_id: "import-create-1",
        test_file_count: 2,
        test_suite_status: "ready",
        test_suite_warnings: [],
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

      if (await page.getByTestId("settings-agent-create-source").count()) {
        throw new Error("removed template/seed creation control is still visible");
      }
      if ((await page.getByTestId("settings-agent-table").count()) !== 1) {
        throw new Error("Settings must render exactly one authoritative Agent table");
      }
      if (await page.getByTestId("settings-workspace-agent-list").count()) {
        throw new Error("duplicated Workspace Agent inventory is still visible");
      }

      await page.getByTestId("settings-agent-import-open").click();
      const createDrawer = page.getByTestId("settings-agent-import-drawer");
      await createDrawer.waitFor();
      if ((await createDrawer.getAttribute("data-state")) !== "create") {
        throw new Error("global import action did not open create mode");
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
      if (!(await createDrawer.isVisible())) {
        throw new Error("create drawer closed before the user could inspect the import receipt");
      }
      if (
        state.newImportBodies.length !== 1 ||
        !state.newImportBodies[0].includes('name="name"') ||
        !state.newImportBodies[0].includes("Imported Package Agent")
      ) {
        throw new Error(`new Agent import did not submit its name: ${JSON.stringify(state.newImportBodies)}`);
      }
      await createDrawer.getByLabel("关闭").click();
      await createDrawer.waitFor({ state: "detached" });

      state.agents = [workspaceAgent, secondWorkspaceAgent];
      await page.getByTestId("settings-panel").locator('button[aria-label="关闭"]').click();
      await page.getByTestId("open-settings").click();
      const agentRow = (agentId) => page
        .getByTestId("settings-agent-item")
        .filter({ has: page.getByText(agentId, { exact: true }) });
      const openActionMenu = async (row) => {
        await row.getByTestId("settings-agent-actions-trigger").click();
        const menu = page.getByTestId("settings-agent-actions-menu");
        await menu.waitFor();
        return menu;
      };
      const exportRow = agentRow("workspace-agent");
      const protectedRow = agentRow("workspace-agent-2");
      await exportRow.waitFor();
      if ((await page.getByTestId("settings-agent-item").count()) !== 2) {
        throw new Error("single Agent table did not render the two mocked Agents exactly once");
      }
      await page.getByTestId("settings-agent-test-status").first().waitFor();
      if ((await page.getByTestId("settings-agent-test-status").count()) !== 2) {
        throw new Error("Workspace test status was not merged into every authoritative Agent row");
      }

      let menu = await openActionMenu(exportRow);
      if ((await menu.getByRole("menuitem").count()) !== 3) {
        throw new Error("Agent object menu does not expose export, overwrite, and delete actions");
      }
      if ((await exportRow.getByTestId("settings-agent-actions-trigger").getAttribute("aria-expanded")) !== "true") {
        throw new Error("Agent action trigger did not expose expanded state");
      }
      await page.waitForFunction(
        () => document.activeElement?.getAttribute("data-testid") === "settings-agent-export",
        null,
        { timeout: 1000 },
      );
      await page.keyboard.press("ArrowDown");
      const focusedMenuItem = await page.evaluate(() => document.activeElement?.getAttribute("data-testid"));
      if (focusedMenuItem !== "settings-agent-overwrite") {
        throw new Error(`ArrowDown did not move focus within the Agent action menu: ${focusedMenuItem}`);
      }
      await page.keyboard.press("Escape");
      await menu.waitFor({ state: "detached" });
      if ((await exportRow.getByTestId("settings-agent-actions-trigger").getAttribute("aria-expanded")) !== "false") {
        throw new Error("Escape did not close the Agent action menu");
      }
      const focusReturned = await exportRow.getByTestId("settings-agent-actions-trigger").evaluate((element) => document.activeElement === element);
      if (!focusReturned) throw new Error("Escape did not return focus to the Agent action trigger");

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
      menu = await openActionMenu(exportRow);
      const downloadPromise = page.waitForEvent("download");
      const exportButton = menu.getByTestId("settings-agent-export");
      await exportButton.click();
      await exportRow.locator(".settings-spin").waitFor({ timeout: 1000 });
      const download = await downloadPromise;
      if (download.suggestedFilename() !== exportFilename) {
        throw new Error(`workspace download filename mismatch: ${download.suggestedFilename()}`);
      }
      await page.getByTestId("settings-workspace-operation-feedback").filter({ hasText: "导出完成" }).waitFor();

      menu = await openActionMenu(protectedRow);
      const protectedDelete = menu.getByTestId("settings-agent-delete");
      if (!(await protectedDelete.isDisabled()) || !(await protectedDelete.textContent())?.includes("受保护")) {
        throw new Error("protected Agent delete action does not expose its disabled reason");
      }
      await menu.getByTestId("settings-agent-export").click();
      const exportFailure = page.getByTestId("settings-workspace-operation-feedback");
      await exportFailure.filter({ hasText: "[WORKSPACE_MAINTENANCE_CONFLICT]" }).waitFor();
      if ((await exportFailure.getAttribute("data-operation")) !== "export") {
        throw new Error("workspace export failure is not attributed to export");
      }

      menu = await openActionMenu(exportRow);
      await menu.getByTestId("settings-agent-overwrite").click();
      const overwriteDrawer = page.getByTestId("settings-agent-import-drawer");
      await overwriteDrawer.waitFor();
      if ((await overwriteDrawer.getAttribute("data-state")) !== "overwrite") {
        throw new Error("row overwrite action did not open overwrite mode");
      }
      const overwriteAgentId = page.getByTestId("settings-workspace-import-agent-id");
      const overwriteName = page.getByTestId("settings-workspace-import-name");
      if (
        (await overwriteAgentId.inputValue()) !== "workspace-agent" ||
        (await overwriteName.inputValue()) !== "Workspace Agent" ||
        !(await overwriteAgentId.isDisabled()) ||
        !(await overwriteName.isDisabled())
      ) {
        throw new Error("overwrite drawer target identity is not fixed and read-only");
      }
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
      if (!(await overwriteDrawer.getByLabel("关闭").isDisabled())) {
        throw new Error("import drawer remained closable while an import request was pending");
      }
      const error = page.getByTestId("settings-error");
      const localError = page.getByTestId("settings-workspace-operation-feedback");
      await localError.waitFor();
      const errorText = await localError.textContent();
      if (
        !errorText?.includes("[WORKSPACE_MANIFEST_AGENT_ID_MISMATCH]") ||
        !errorText.includes("source-agent") ||
        !errorText.includes("workspace-agent") ||
        !errorText.includes("完全一致后重新打包")
      ) {
        throw new Error(`structured workspace failure code is not visible: ${errorText}`);
      }
      if (await error.count()) {
        throw new Error("import failure leaked from the import drawer into the Settings-global error banner");
      }
      await page.screenshot({
        path: join(screenshotDir, "workspace-import-structured-failure.png"),
        fullPage: true,
      });

      await overwriteDrawer.getByLabel("关闭").click();
      await overwriteDrawer.waitFor({ state: "detached" });
      menu = await openActionMenu(protectedRow);
      await menu.getByTestId("settings-agent-overwrite").click();
      await page.getByTestId("settings-agent-import-drawer").waitFor();
      if (!(await page.getByTestId("settings-workspace-import-submit").isDisabled())) {
        throw new Error("opening another overwrite target must clear the stale failed package");
      }
      if (await page.getByTestId("settings-workspace-operation-feedback").count()) {
        throw new Error("opening another overwrite target retained stale operation feedback");
      }
      await page.getByTestId("settings-agent-import-drawer").getByLabel("关闭").click();
      menu = await openActionMenu(exportRow);
      await menu.getByTestId("settings-agent-overwrite").click();
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

      await page.getByTestId("settings-agent-import-drawer").getByLabel("关闭").click();
      await page.setViewportSize({ width: 390, height: 844 });
      await protectedRow.scrollIntoViewIfNeeded();
      const mobileLayout = await page.evaluate(() => {
        const panel = document.querySelector('[data-testid="settings-panel"]');
        const table = document.querySelector('[data-testid="settings-agent-table"]');
        const trigger = document.querySelector('[data-testid="settings-agent-actions-trigger"]');
        if (!(panel instanceof HTMLElement) || !(table instanceof HTMLElement) || !(trigger instanceof HTMLElement)) return null;
        const panelBox = panel.getBoundingClientRect();
        const triggerBox = trigger.getBoundingClientRect();
        return {
          tableOverflow: table.scrollWidth - table.clientWidth,
          triggerInside: triggerBox.left >= panelBox.left && triggerBox.right <= panelBox.right,
        };
      });
      if (!mobileLayout || mobileLayout.tableOverflow > 1 || !mobileLayout.triggerInside) {
        throw new Error(`mobile authoritative table overflowed or clipped its action trigger: ${JSON.stringify(mobileLayout)}`);
      }
      menu = await openActionMenu(protectedRow);
      const menuBox = await menu.boundingBox();
      if (!menuBox || menuBox.x < 0 || menuBox.y < 0 || menuBox.x + menuBox.width > 390 || menuBox.y + menuBox.height > 844) {
        throw new Error(`mobile Agent action menu is outside the viewport: ${JSON.stringify(menuBox)}`);
      }
      await page.keyboard.press("Escape");
      await page.screenshot({
        path: join(screenshotDir, "workspace-agent-single-table-mobile.png"),
        fullPage: true,
      });

      console.log(
        JSON.stringify(
          {
            status: "passed",
            mode: "mock",
            screenshots: screenshotDir,
            scenarios: [
              "agent_empty_state",
              "single_authoritative_agent_table",
              "package_only_agent_creation",
              "create_import_drawer_receipt",
              "accessible_agent_action_menu",
              "protected_delete_reason",
              "workspace_export_download_and_headers",
              "workspace_export_local_failure",
              "structured_import_failure",
              "overwrite_target_is_read_only",
              "drawer_reset_clears_stale_package_and_feedback",
              "import_receipt",
              "restore_action",
              "new_agent_workspace_import",
              "mobile_table_and_menu_bounds",
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
