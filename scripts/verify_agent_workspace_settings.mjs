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
import { exportFilename, importedCommit, importedWorkspaceAgent, packageDigest, pendingDeletionOperationId, pendingDeletionReceipt, previousCommit, restoredCommit, rollbackCommit, secondWorkspaceAgent, timestamp, treeDigest, workspaceAgent } from "./verify_agent_workspace_settings_fixtures.mjs";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const { chromium } = require("playwright");

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const port = Number(process.env.AGENT_WORKSPACE_SETTINGS_UI_PORT || 55231);
const uiBase = `http://127.0.0.1:${port}`;
const apiBase = "http://runtime.test";
const screenshotDir =
  process.env.VERIFY_SCREENSHOT_DIR ||
  mkdtempSync(join(tmpdir(), "agentgov-workspace-settings-"));

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

async function respondJson(context, body, status = 200) {
  await json(context.route, body, status);
  return true;
}

async function handleCorsRequest(context) {
  if (context.method !== "OPTIONS") return false;
  await context.route.fulfill({
    status: 204,
    headers: {
      "access-control-allow-origin": "*",
      "access-control-allow-headers": "*",
      "access-control-allow-methods": "*",
    },
  });
  return true;
}

async function handleAgentQueryRequest(context) {
  const { method, path, state, url } = context;
  if (path === "/api/agent-registry" && method === "GET") {
    return respondJson(context, state.agents);
  }
  if (path === "/api/agent-deletion-operations" && method === "GET") {
    state.deletionDiscoveryRequests += 1;
    if (
      url.searchParams.get("state") !== "cleanup_pending" ||
      url.searchParams.get("limit") !== "20"
    ) {
      throw new Error(`pending deletion discovery used an unbounded query: ${url.search}`);
    }
    return respondJson(context, state.pendingDeletions);
  }
  if (
    path === `/api/agent-deletion-operations/${pendingDeletionOperationId}` &&
    method === "GET"
  ) {
    state.pendingDeletions = [];
    return respondJson(context, {
      ...pendingDeletionReceipt,
      state: "completed",
      workspace_removed: true,
      cleanup_complete: true,
      last_error_code: null,
      attempt_count: 3,
    });
  }
  if (/^\/api\/agent-registry\/[^/]+\/test-suite$/.test(path) && method === "GET") {
    return respondJson(context, testSuitePayload(decodeURIComponent(path.split("/")[3])));
  }
  if (path === "/api/agent-test-runs" && method === "GET") {
    return respondJson(context, []);
  }
  if (path === "/api/settings/openai-compat-agent" && method === "GET") {
    return respondJson(context, {
      agent_id: null,
      configured: false,
      effective_agent_id: "security-operations-expert",
    });
  }
  if (path === "/api/agent-repository/current" && method === "GET") {
    return respondJson(context, defaultPayload(path));
  }
  return false;
}

function testSuitePayload(agentId) {
  return {
    agent_id: agentId,
    commit_sha: previousCommit,
    tests_directory_present: true,
    readme_present: true,
    test_file_count: 2,
    test_files: ["tests/test_smoke.py", "tests/test_contract.py"],
    suite_digest: "mock-suite",
    diagnostics: [],
  };
}

async function handleWorkspaceExportRequest(context) {
  const { method, path, route, state } = context;
  if (path === "/api/agent-registry/workspace-agent/workspace/export" && method === "POST") {
    state.exportRequests += 1;
    await new Promise((resolve) => setTimeout(resolve, 120));
    await route.fulfill({
      status: 200,
      contentType: "application/gzip",
      headers: {
        "access-control-allow-origin": "*",
        "access-control-expose-headers":
          "Content-Disposition, X-Agent-Commit-SHA, X-Workspace-Package-SHA256, X-Workspace-Tree-SHA256",
        "content-disposition": `attachment; filename="${exportFilename}"`,
        "x-agent-commit-sha": previousCommit,
        "x-workspace-package-sha256": packageDigest,
        "x-workspace-tree-sha256": treeDigest,
      },
      body: Buffer.from("mock exported workspace"),
    });
    return true;
  }
  if (
    path === "/api/agent-registry/workspace-agent-2/workspace/export" &&
    method === "POST"
  ) {
    await delayedJson(
      route,
      {
        error_code: "WORKSPACE_MAINTENANCE_CONFLICT",
        detail: "workspace export is busy",
      },
      409,
    );
    return true;
  }
  return false;
}

function overwriteImportFailure() {
  return {
    error_code: "WORKSPACE_MANIFEST_AGENT_ID_MISMATCH",
    detail:
      "导入被拒绝：包内来源 Agent ID “source-agent”与请求目标 Agent ID “workspace-agent”不一致；" +
      "系统不会改写包内身份。请确认导入目标，并将 agent.yaml.agent.id 改为与 URL 中的 agent_id 完全一致后重新打包。",
    field: "agent.yaml.agent.id",
    import_action: "overwrite",
    expected_agent_id: "workspace-agent",
    actual_agent_id: "source-agent",
    remediation: "确认导入目标，使 agent.yaml.agent.id 与 URL 中的 agent_id 完全一致后重新打包。",
  };
}

function importReceipt(action, agent, previousCommitSha, rollbackTargetCommitSha, recordId) {
  return {
    action,
    agent,
    previous_commit_sha: previousCommitSha,
    current_commit_sha: importedCommit,
    package_sha256: packageDigest,
    tree_sha256: treeDigest,
    rollback_target_commit_sha: rollbackTargetCommitSha,
    activation_mode: "next_turn",
    import_record_id: recordId,
    test_file_count: 2,
    test_suite_status: "ready",
    test_suite_diagnostics: [],
  };
}

async function handleWorkspaceImportRequest(context) {
  const { method, path, request, route, state } = context;
  if (path === "/api/agent-registry/workspace-agent/workspace/import" && method === "POST") {
    state.importAttempts += 1;
    if (state.importAttempts === 1) {
      await delayedJson(route, overwriteImportFailure(), 409);
    } else {
      await delayedJson(
        route,
        importReceipt("overwritten", workspaceAgent, previousCommit, rollbackCommit, "import-overwrite-1"),
      );
    }
    return true;
  }
  if (path === "/api/agent-registry/imported-new/workspace/import" && method === "POST") {
    state.newImportBodies.push(request.postData() || "");
    state.agents.push(importedWorkspaceAgent);
    await delayedJson(
      route,
      importReceipt("created", importedWorkspaceAgent, null, null, "import-create-1"),
    );
    return true;
  }
  return false;
}

async function handleWorkspaceRestoreRequest(context) {
  const { method, path, request, route, state } = context;
  if (path !== "/api/agent-registry/workspace-agent/workspace/restore" || method !== "POST") {
    return false;
  }
  state.restoreRequests.push(request.postDataJSON());
  await delayedJson(route, {
    action: "restored",
    agent: workspaceAgent,
    previous_commit_sha: importedCommit,
    restored_tree_commit_sha: rollbackCommit,
    current_commit_sha: restoredCommit,
    rollback_target_commit_sha: importedCommit,
    activation_mode: "next_turn",
  });
  return true;
}

const mockRouteHandlers = [
  handleCorsRequest,
  handleAgentQueryRequest,
  handleWorkspaceExportRequest,
  handleWorkspaceImportRequest,
  handleWorkspaceRestoreRequest,
];

async function routeRuntimeRequest(route, state) {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin === uiBase || url.hostname !== "runtime.test") return route.continue();
  const context = {
    method: request.method(),
    path: url.pathname,
    request,
    route,
    state,
    url,
  };
  for (const handler of mockRouteHandlers) {
    if (await handler(context)) return;
  }
  await json(route, defaultPayload(context.path));
}

async function installMockRoutes(page, state) {
  await page.route("**/*", (route) => routeRuntimeRequest(route, state));
}

function createAcceptanceState() {
  return {
    agents: [],
    exportRequests: 0,
    importAttempts: 0,
    newImportBodies: [],
    restoreRequests: [],
    deletionDiscoveryRequests: 0,
    pendingDeletions: [pendingDeletionReceipt],
  };
}

async function configureAcceptancePage(page, state) {
  await page.addInitScript((base) => {
    window.localStorage.setItem(
      "runtime-client-config",
      JSON.stringify({ apiBase: base, apiKey: "" }),
    );
  }, apiBase);
  page.on("dialog", (dialog) => dialog.accept());
  await installMockRoutes(page, state);
}

function agentRow(page, agentId) {
  return page
    .getByTestId("settings-agent-item")
    .filter({ has: page.getByText(agentId, { exact: true }) });
}

async function openActionMenu(page, row) {
  await row.getByTestId("settings-agent-actions-trigger").click();
  const menu = page.getByTestId("settings-agent-actions-menu");
  await menu.waitFor();
  return menu;
}

async function verifyPendingDeletionRecovery(page, state) {
  await page.goto(uiBase, { waitUntil: "domcontentloaded" });
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-agent-empty").waitFor({ timeout: 20000 });
  const pendingDeletion = page.getByTestId("settings-pending-deletions");
  await pendingDeletion.filter({ hasText: "Cleanup Pending Agent" }).waitFor();
  const receiptText = (await pendingDeletion.textContent()) || "";
  for (const expected of ["AGENT_DELETION_FILESYSTEM_FENCE", "尝试 2 次", timestamp]) {
    if (!receiptText.includes(expected)) {
      throw new Error(`recovered deletion receipt is missing ${expected}: ${receiptText}`);
    }
  }
  await pendingDeletion.getByTestId("settings-deletion-status-refresh").click();
  await pendingDeletion.waitFor({ state: "detached" });
  if (state.deletionDiscoveryRequests !== 1) {
    throw new Error(
      `Settings did not perform one authoritative pending deletion discovery: ${state.deletionDiscoveryRequests}`,
    );
  }
}

async function verifyEmptyRegistryLayout(page) {
  if (await page.getByTestId("settings-agent-create-source").count()) {
    throw new Error("removed template/seed creation control is still visible");
  }
  if ((await page.getByTestId("settings-agent-table").count()) !== 1) {
    throw new Error("Settings must render exactly one authoritative Agent table");
  }
  if (await page.getByTestId("settings-workspace-agent-list").count()) {
    throw new Error("duplicated Workspace Agent inventory is still visible");
  }
}

async function verifyCreateImport(page, state) {
  await page.getByTestId("settings-agent-import-open").click();
  const drawer = page.getByTestId("settings-agent-import-drawer");
  await drawer.waitFor();
  if ((await drawer.getAttribute("data-state")) !== "create") {
    throw new Error("global import action did not open create mode");
  }
  const agentIdInput = page.getByTestId("settings-workspace-import-agent-id");
  if ((await agentIdInput.getAttribute("maxlength")) !== "128") {
    throw new Error("create Agent ID input does not expose the central 128-character boundary");
  }
  await agentIdInput.fill("imported-new");
  await page.getByTestId("settings-workspace-import-name").fill("Imported Package Agent");
  await page.getByTestId("settings-workspace-import-file").setInputFiles({
    name: "new-agent.tar.gz",
    mimeType: "application/gzip",
    buffer: Buffer.from("new agent workspace archive"),
  });
  await page.getByTestId("settings-workspace-import-submit").click();
  await page
    .getByTestId("settings-workspace-import-receipt")
    .filter({ hasText: "已创建" })
    .filter({ hasText: "测试套件已就绪" })
    .waitFor();
  if (!(await drawer.isVisible())) {
    throw new Error("create drawer closed before the user could inspect the import receipt");
  }
  const body = state.newImportBodies[0] || "";
  if (state.newImportBodies.length !== 1 || !body.includes('name="name"') || !body.includes("Imported Package Agent")) {
    throw new Error(`new Agent import did not submit its name: ${JSON.stringify(state.newImportBodies)}`);
  }
  await drawer.getByLabel("关闭").click();
  await drawer.waitFor({ state: "detached" });
}

async function openPopulatedRegistry(page, state) {
  state.agents = [workspaceAgent, secondWorkspaceAgent];
  await page.getByTestId("settings-panel").locator('button[aria-label="关闭"]').click();
  await page.getByTestId("open-settings").click();
  const exportRow = agentRow(page, "workspace-agent");
  const protectedRow = agentRow(page, "workspace-agent-2");
  await exportRow.waitFor();
  if ((await page.getByTestId("settings-agent-item").count()) !== 2) {
    throw new Error("single Agent table did not render the two mocked Agents exactly once");
  }
  await page.getByTestId("settings-agent-test-status").first().waitFor();
  if ((await page.getByTestId("settings-agent-test-status").count()) !== 2) {
    throw new Error("Workspace test status was not merged into every authoritative Agent row");
  }
  return { exportRow, protectedRow };
}

async function verifyAccessibleActionMenu(page, exportRow) {
  const menu = await openActionMenu(page, exportRow);
  if ((await menu.getByRole("menuitem").count()) !== 3) {
    throw new Error("Agent object menu does not expose export, overwrite, and delete actions");
  }
  const trigger = exportRow.getByTestId("settings-agent-actions-trigger");
  if ((await trigger.getAttribute("aria-expanded")) !== "true") {
    throw new Error("Agent action trigger did not expose expanded state");
  }
  await page.waitForFunction(
    () => document.activeElement?.getAttribute("data-testid") === "settings-agent-export",
    null,
    { timeout: 1000 },
  );
  await page.keyboard.press("ArrowDown");
  const focused = await page.evaluate(() =>
    document.activeElement?.getAttribute("data-testid"),
  );
  if (focused !== "settings-agent-overwrite") {
    throw new Error(`ArrowDown did not move focus within the Agent action menu: ${focused}`);
  }
  await page.keyboard.press("Escape");
  await menu.waitFor({ state: "detached" });
  if ((await trigger.getAttribute("aria-expanded")) !== "false") {
    throw new Error("Escape did not close the Agent action menu");
  }
  if (!(await trigger.evaluate((element) => document.activeElement === element))) {
    throw new Error("Escape did not return focus to the Agent action trigger");
  }
}

async function readBrowserVisibleExportHeaders(page) {
  return page.evaluate(
    async ({ base, path }) => {
      const response = await fetch(`${base}${path}`, { method: "POST" });
      return {
        disposition: response.headers.get("content-disposition"),
        commit: response.headers.get("x-agent-commit-sha"),
        packageDigest: response.headers.get("x-workspace-package-sha256"),
        treeDigest: response.headers.get("x-workspace-tree-sha256"),
      };
    },
    { base: apiBase, path: "/api/agent-registry/workspace-agent/workspace/export" },
  );
}

async function verifyWorkspaceExport(page, exportRow) {
  const headers = await readBrowserVisibleExportHeaders(page);
  if (
    !headers.disposition?.includes(exportFilename) ||
    headers.commit !== previousCommit ||
    headers.packageDigest !== packageDigest ||
    headers.treeDigest !== treeDigest
  ) {
    throw new Error(`workspace export headers are not browser-visible: ${JSON.stringify(headers)}`);
  }
  const menu = await openActionMenu(page, exportRow);
  const downloadPromise = page.waitForEvent("download");
  await menu.getByTestId("settings-agent-export").click();
  await exportRow.locator(".settings-spin").waitFor({ timeout: 1000 });
  const download = await downloadPromise;
  if (download.suggestedFilename() !== exportFilename) {
    throw new Error(`workspace download filename mismatch: ${download.suggestedFilename()}`);
  }
  await page
    .getByTestId("settings-workspace-operation-feedback")
    .filter({ hasText: "导出完成" })
    .waitFor();
}

async function verifyProtectedAgentActions(page, protectedRow) {
  const menu = await openActionMenu(page, protectedRow);
  const deleteButton = menu.getByTestId("settings-agent-delete");
  if (!(await deleteButton.isDisabled()) || !(await deleteButton.textContent())?.includes("受保护")) {
    throw new Error("protected Agent delete action does not expose its disabled reason");
  }
  await menu.getByTestId("settings-agent-export").click();
  const feedback = page.getByTestId("settings-workspace-operation-feedback");
  await feedback.filter({ hasText: "[WORKSPACE_MAINTENANCE_CONFLICT]" }).waitFor();
  if ((await feedback.getAttribute("data-operation")) !== "export") {
    throw new Error("workspace export failure is not attributed to export");
  }
}

async function verifyStructuredOverwriteFailure(page, exportRow) {
  const menu = await openActionMenu(page, exportRow);
  await menu.getByTestId("settings-agent-overwrite").click();
  const drawer = page.getByTestId("settings-agent-import-drawer");
  await drawer.waitFor();
  if ((await drawer.getAttribute("data-state")) !== "overwrite") {
    throw new Error("row overwrite action did not open overwrite mode");
  }
  const agentId = page.getByTestId("settings-workspace-import-agent-id");
  const name = page.getByTestId("settings-workspace-import-name");
  if (
    (await agentId.inputValue()) !== "workspace-agent" ||
    (await name.inputValue()) !== "Workspace Agent" ||
    !(await agentId.isDisabled()) ||
    !(await name.isDisabled())
  ) {
    throw new Error("overwrite drawer target identity is not fixed and read-only");
  }
  await page.getByTestId("settings-workspace-import-file").setInputFiles({
    name: "workspace.tar.gz",
    mimeType: "application/gzip",
    buffer: Buffer.from("mock workspace archive"),
  });
  const submit = page.getByTestId("settings-workspace-import-submit");
  await submit.click();
  if ((await submit.getAttribute("aria-busy")) !== "true") {
    throw new Error("workspace import button did not expose pending state");
  }
  if (!(await drawer.getByLabel("关闭").isDisabled())) {
    throw new Error("import drawer remained closable while an import request was pending");
  }
  await assertStructuredImportFailure(page);
  await page.screenshot({
    path: join(screenshotDir, "workspace-import-structured-failure.png"),
    fullPage: true,
  });
  return drawer;
}

async function assertStructuredImportFailure(page) {
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
  if (await page.getByTestId("settings-error").count()) {
    throw new Error("import failure leaked from the import drawer into the Settings-global error banner");
  }
}

async function verifyDrawerReset(page, failedDrawer, exportRow, protectedRow) {
  await failedDrawer.getByLabel("关闭").click();
  await failedDrawer.waitFor({ state: "detached" });
  let menu = await openActionMenu(page, protectedRow);
  await menu.getByTestId("settings-agent-overwrite").click();
  await page.getByTestId("settings-agent-import-drawer").waitFor();
  if (!(await page.getByTestId("settings-workspace-import-submit").isDisabled())) {
    throw new Error("opening another overwrite target must clear the stale failed package");
  }
  if (await page.getByTestId("settings-workspace-operation-feedback").count()) {
    throw new Error("opening another overwrite target retained stale operation feedback");
  }
  await page.getByTestId("settings-agent-import-drawer").getByLabel("关闭").click();
  menu = await openActionMenu(page, exportRow);
  await menu.getByTestId("settings-agent-overwrite").click();
}

async function verifySuccessfulOverwrite(page) {
  await page.getByTestId("settings-workspace-import-file").setInputFiles({
    name: "workspace.tar.gz",
    mimeType: "application/gzip",
    buffer: Buffer.from("mock workspace archive retry"),
  });
  await page.getByTestId("settings-workspace-import-submit").click();
  const receipt = page.getByTestId("settings-workspace-import-receipt");
  await receipt.waitFor();
  const receiptText = (await receipt.textContent()) || "";
  const expectedValues = [
    "已覆盖",
    "测试套件已就绪",
    previousCommit.slice(0, 12),
    importedCommit.slice(0, 12),
    packageDigest.slice(0, 12),
    treeDigest.slice(0, 12),
  ];
  for (const expected of expectedValues) {
    if (!receiptText.includes(expected)) {
      throw new Error(`workspace import receipt is missing ${expected}: ${receiptText}`);
    }
  }
  await page.getByTestId("settings-workspace-restore").waitFor();
  await page.screenshot({
    path: join(screenshotDir, "workspace-import-success-receipt.png"),
    fullPage: true,
  });
}

async function verifyWorkspaceRestore(page, state) {
  const restore = page.getByTestId("settings-workspace-restore");
  await restore.click();
  if ((await restore.getAttribute("aria-busy")) !== "true") {
    throw new Error("workspace restore button did not expose pending state");
  }
  await page
    .getByTestId("settings-workspace-operation-feedback")
    .filter({ hasText: "恢复完成" })
    .waitFor();
  await restore.waitFor({ state: "detached" });
  if (state.restoreRequests.length !== 1) {
    throw new Error(`expected one restore request, got ${state.restoreRequests.length}`);
  }
  const request = state.restoreRequests[0];
  if (
    request.target_commit_sha !== rollbackCommit ||
    request.expected_current_commit_sha !== importedCommit
  ) {
    throw new Error(`restore request did not preserve receipt commits: ${JSON.stringify(request)}`);
  }
  if (state.exportRequests !== 2) {
    throw new Error(`expected browser header probe and UI export, got ${state.exportRequests} requests`);
  }
  await page.getByTestId("settings-agent-import-drawer").getByLabel("关闭").click();
}

async function readMobileLayout(page) {
  return page.evaluate(() => {
    const panel = document.querySelector('[data-testid="settings-panel"]');
    const table = document.querySelector('[data-testid="settings-agent-table"]');
    const trigger = document.querySelector('[data-testid="settings-agent-actions-trigger"]');
    if (
      !(panel instanceof HTMLElement) ||
      !(table instanceof HTMLElement) ||
      !(trigger instanceof HTMLElement)
    ) {
      return null;
    }
    const panelBox = panel.getBoundingClientRect();
    const triggerBox = trigger.getBoundingClientRect();
    return {
      tableOverflow: table.scrollWidth - table.clientWidth,
      triggerInside: triggerBox.left >= panelBox.left && triggerBox.right <= panelBox.right,
    };
  });
}

async function verifyMobileLayout(page, protectedRow) {
  await page.setViewportSize({ width: 390, height: 844 });
  await protectedRow.scrollIntoViewIfNeeded();
  const layout = await readMobileLayout(page);
  if (!layout || layout.tableOverflow > 1 || !layout.triggerInside) {
    throw new Error(
      `mobile authoritative table overflowed or clipped its action trigger: ${JSON.stringify(layout)}`,
    );
  }
  const menu = await openActionMenu(page, protectedRow);
  const box = await menu.boundingBox();
  if (!box || box.x < 0 || box.y < 0 || box.x + box.width > 390 || box.y + box.height > 844) {
    throw new Error(`mobile Agent action menu is outside the viewport: ${JSON.stringify(box)}`);
  }
  await page.keyboard.press("Escape");
  await page.screenshot({
    path: join(screenshotDir, "workspace-agent-single-table-mobile.png"),
    fullPage: true,
  });
}

function reportAcceptance() {
  console.log(
    JSON.stringify(
      {
        status: "passed",
        mode: "mock",
        screenshots: screenshotDir,
        scenarios: [
          "agent_empty_state",
          "pending_deletion_receipt_recovery",
          "agent_id_128_character_boundary",
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
}

async function runAcceptanceScenarios(page, state) {
  await verifyPendingDeletionRecovery(page, state);
  await verifyEmptyRegistryLayout(page);
  await verifyCreateImport(page, state);
  const { exportRow, protectedRow } = await openPopulatedRegistry(page, state);
  await verifyAccessibleActionMenu(page, exportRow);
  await verifyWorkspaceExport(page, exportRow);
  await verifyProtectedAgentActions(page, protectedRow);
  const failedDrawer = await verifyStructuredOverwriteFailure(page, exportRow);
  await verifyDrawerReset(page, failedDrawer, exportRow, protectedRow);
  await verifySuccessfulOverwrite(page);
  await verifyWorkspaceRestore(page, state);
  await verifyMobileLayout(page, protectedRow);
  reportAcceptance();
}

async function main() {
  mkdirSync(screenshotDir, { recursive: true });
  const server = startVite();
  try {
    await waitForVite();
    const browser = await chromium.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
    try {
      const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
      const state = createAcceptanceState();
      await configureAcceptancePage(page, state);
      await runAcceptanceScenarios(page, state);
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
