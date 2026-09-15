// 由受控公共验收入口提供 browser/config；不读取 env、不自行启动浏览器或容器。
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { assertCandidateReceipt, assertLiveHead, testApproveAndPublish } from "../verify_agent_candidate_lifecycle.mjs";
import { attachUiDiagnostics, waitForUi } from "./playground_cancel_runtime.mjs";
import { attachCancelNetworkEvidence } from "./playground_cancel_evidence.mjs";
import { apiJson } from "./runtime_client.mjs";
import { configureUiApiConnection } from "./ui_connection.mjs";
import { CandidateRuntimeRestartError } from "./candidate_runtime_restart.mjs";

function check(condition, code, status = null) {
  if (!condition) {
    const error = new Error(code);
    error.status = status;
    throw error;
  }
}

async function openSelectedSettings(page, config) {
  await waitForUi(config.uiBase, config.actionTimeoutMs);
  await configureUiApiConnection(page, config);
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-agents").click();
  await page.getByTestId("settings-agent-management").waitFor();
}

async function importWorkspaceThroughUi(page, config, input, evidence) {
  const packageSha256 = createHash("sha256").update(await readFile(input.packagePath)).digest("hex");
  await page.getByTestId("settings-agent-import-open").click();
  const drawer = page.getByTestId("settings-agent-import-drawer");
  await drawer.waitFor();
  await drawer.getByTestId("settings-workspace-import-agent-id").fill(input.agentId);
  await drawer.getByTestId("settings-workspace-import-name").fill(input.name);
  await drawer.getByTestId("settings-workspace-import-file").setInputFiles(input.packagePath);
  const path = `/api/agent-registry/${encodeURIComponent(input.agentId)}/workspace/import`;
  const pending = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && new URL(response.url()).origin === new URL(config.apiBase).origin
    && new URL(response.url()).pathname === path
  ), { timeout: config.actionTimeoutMs });
  void pending.catch(() => undefined);
  await drawer.getByTestId("settings-workspace-import-submit").click();
  const response = await pending;
  check(response.ok(), "WORKSPACE_IMPORT_HTTP_FAILED", response.status());
  const candidate = await response.json();
  // 归属由实际响应及目标一致性建立；不根据猜测 ID 自动删除或覆盖已有 Agent。
  check(candidate?.agent?.agent_id === input.agentId, "WORKSPACE_IMPORT_AGENT_MISMATCH");
  if (typeof candidate.change_set_id === "string") evidence.change_set_id = candidate.change_set_id;
  if (typeof candidate.import_record_id === "string") evidence.import_record_id = candidate.import_record_id;
  if (/^[a-f0-9]{40}$/.test(candidate.candidate_commit_sha || "")) {
    evidence.candidate_commit_sha = candidate.candidate_commit_sha;
  }
  assertCandidateReceipt(candidate, input, "created");
  check(candidate.package_sha256 === packageSha256, "WORKSPACE_IMPORT_PACKAGE_MISMATCH");
  check(candidate.test_suite_status === "ready" && candidate.test_file_count > 0, "WORKSPACE_IMPORT_SUITE_NOT_READY");
  await drawer.getByTestId("settings-workspace-import-receipt").waitFor();
  await assertLiveHead(config, input, candidate.base_commit_sha);
  await drawer.getByTestId("settings-candidate-open-governance").click();
  await drawer.waitFor({ state: "detached" });
  await page.getByTestId("release-workbench").waitFor();
  check(await page.getByTestId("settings-candidate-agent-select").inputValue() === input.agentId,
    "WORKSPACE_IMPORT_GOVERNANCE_AGENT_MISMATCH");
  return candidate;
}

function metadata(candidate, publication) {
  const binding = publication.binding;
  return {
    status: "passed",
    agent_id: candidate.agent.agent_id,
    import_record_id: candidate.import_record_id,
    package_sha256: candidate.package_sha256,
    tree_sha256: candidate.tree_sha256,
    change_set_id: candidate.change_set_id,
    base_commit_sha: candidate.base_commit_sha,
    candidate_commit_sha: candidate.candidate_commit_sha,
    test_run_id: publication.testRun.test_run_id,
    suite_digest: publication.suite.suite_digest,
    diff_digest: publication.diffDigest,
    changed_file_count: publication.changedFileCount,
    release_id: publication.release.release_id,
    binding: {
      governance_agent_id: binding.governance_agent_id,
      runtime_agent_id: binding.runtime_agent_id,
      agent_version_id: binding.agent_version_id,
      harness_digest: binding.harness_digest,
      provisioned: binding.provisioned,
    },
    retained: true,
  };
}

export async function bootstrapImportedWorkspace(browser, config, { agentId, packagePath, name }) {
  const input = { agentId, packagePath, name };
  const evidence = { agent_id: agentId, retained: true };
  let stage = "bootstrap_registry_preflight";
  let context;
  try {
    // 初次导入限定：已注册的身份不自动重试成 overwrite，也不删除后再导入。
    const agents = await apiJson(config, "/api/agent-registry");
    check(Array.isArray(agents), "WORKSPACE_IMPORT_REGISTRY_INVALID");
    check(!agents.some((agent) => agent.agent_id === agentId), "WORKSPACE_IMPORT_AGENT_ALREADY_EXISTS");
    context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
    const page = await context.newPage();
    page.setDefaultTimeout(config.actionTimeoutMs);
    const network = attachCancelNetworkEvidence(page, config.apiBase);
    const diagnostics = attachUiDiagnostics(page, network);
    stage = "bootstrap_ui_settings";
    await openSelectedSettings(page, config);
    stage = "bootstrap_workspace_import";
    const candidate = await importWorkspaceThroughUi(page, config, input, evidence);
    stage = "bootstrap_candidate_test_review_approve_publish";
    // 现有唯一旅程：真实 UI pytest -> 完整 Diff -> 一次确认审批 -> 非 force 发布 -> 精确 binding。
    const publication = await testApproveAndPublish(page, config, candidate, evidence);
    evidence.stage = "bootstrap_postconditions";
    check(diagnostics.length === 0, "WORKSPACE_BOOTSTRAP_UI_DIAGNOSTIC");
    check(await network.settle(config.actionTimeoutMs), "WORKSPACE_BOOTSTRAP_NETWORK_NOT_SETTLED");
    return metadata(candidate, publication);
  } catch (error) {
    // 外层 helper 的错误可能含 HTTP 正文；只带安全阶段及本轮实际收到的 ID，不带 cause。
    const safe = new Error("WORKSPACE_BOOTSTRAP_FAILED");
    safe.code = "WORKSPACE_BOOTSTRAP_FAILED";
    safe.acceptanceStage = evidence.stage || stage;
    safe.bootstrapEvidence = evidence;
    safe.kind = error?.name === "TimeoutError" ? "timeout" : "error";
    safe.status = Number.isInteger(error?.status) ? error.status : null;
    if (error instanceof CandidateRuntimeRestartError) safe.maintenanceCode = error.code;
    throw safe;
  } finally {
    // 仅关闭此 helper 创建的临时浏览器上下文；业务候选/发布保留用于后续双 Agent 旅程。
    if (context) {
      await context.close().catch(() => {
        const safe = new Error("WORKSPACE_BOOTSTRAP_CONTEXT_CLEANUP_FAILED");
        safe.code = "WORKSPACE_BOOTSTRAP_CONTEXT_CLEANUP_FAILED";
        safe.acceptanceStage = "bootstrap_context_cleanup";
        safe.bootstrapEvidence = evidence;
        throw safe;
      });
    }
  }
}
