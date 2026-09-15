#!/usr/bin/env node
import { createHash, randomBytes } from "node:crypto";
import { createRequire } from "node:module";
import process from "node:process";
import { pathToFileURL } from "node:url";

import { requireContainerAcceptance } from "./container_acceptance_guard.mjs";
import {
  attachUiDiagnostics,
  cleanupResources,
  failureDiagnostic,
  runtimeConnection,
  waitForUi,
} from "./improvement_ui_e2e/playground_cancel_runtime.mjs";
import { attachCancelNetworkEvidence } from "./improvement_ui_e2e/playground_cancel_evidence.mjs";
import {
  apiJson,
  apiRequest,
  getCurrentRuntimeAgent,
  jsonInit,
  waitForCompleteRuntimeTrace,
  waitForTerminalRuntimeRun,
} from "./improvement_ui_e2e/runtime_client.mjs";
import {
  loadReviewedScenarios,
  requireScenario,
} from "./improvement_ui_e2e/reviewed_scenarios.mjs";
import { browserExecutionPlan } from "./improvement_ui_e2e/browser_acceptance_contract.mjs";
import { reviewAndApprovePassedCandidate } from "./improvement_ui_e2e/candidate_review.mjs";
import { isRuntimeTemplateRestartRequired, restartCandidateRuntime } from "./improvement_ui_e2e/candidate_runtime_restart.mjs";

const TECHNICAL_SCENARIO_AGENT_ID = "runtime-technical-integration-package";
const TERMINAL_RUN_STATES = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const TERMINAL_TEST_STATES = new Set(["passed", "failed", "error", "cancelled", "interrupted"]);
const STREAM_READY_LIMIT_MS = 5_000;

let config;
let reviewed;
let scenario;
let browserPlan;
let chromium;
let firefox;

function initializeAcceptance() {
  requireContainerAcceptance();
  if (process.env.REQUIRE_LIVE_RUNTIME !== "1") {
    throw new Error("REQUIRE_LIVE_RUNTIME=1 is required for candidate lifecycle acceptance");
  }
  if (process.env.AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE !== "langfuse") {
    throw new Error("Candidate lifecycle acceptance requires the isolated langfuse profile");
  }
  const require = createRequire(new URL("../frontend/package.json", import.meta.url));
  ({ chromium, firefox } = require("playwright"));
  browserPlan = browserExecutionPlan(String(process.env.BROWSER || "both"), { formal: false });
  if (browserPlan.length !== 2 || !browserPlan.includes("chromium") || !browserPlan.includes("firefox")) {
    throw new Error("Candidate lifecycle acceptance requires Chromium and Firefox");
  }
  config = {
    ...runtimeConnection(),
    actionTimeoutMs: positiveTimeout("REAL_ACTION_TIMEOUT_MS", 300_000),
    testRunTimeoutMs: positiveTimeout("REAL_TEST_RUN_TIMEOUT_MS", 900_000),
  };
  reviewed = loadReviewedScenarios(
    process.env.TECHNICAL_SCENARIO_FILE,
    TECHNICAL_SCENARIO_AGENT_ID,
  );
  scenario = requireScenario(reviewed, "success");
  if (typeof scenario.input !== "string" || !scenario.input.trim()) {
    throw new Error("The technical success scenario must contain a non-empty input");
  }
}

function positiveTimeout(name, fallback) {
  const value = Number(process.env[name] || fallback);
  if (!Number.isFinite(value) || value < 1_000) {
    throw new Error(`${name} must be a finite number of at least 1000 milliseconds`);
  }
  return value;
}

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function requireCondition(condition, message) {
  if (!condition) throw new Error(message);
}

function safeDiagnostics(events) {
  return events.map((event) => ({
    kind: String(event?.kind || "unknown"),
    ...(Number.isInteger(event?.status) ? { status: event.status } : {}),
  }));
}

function isCommitSha(value) {
  return typeof value === "string" && /^[0-9a-f]{40}$/.test(value);
}

function responseMatches(response, method, path) {
  const url = new URL(response.url());
  return response.request().method() === method && url.pathname === path;
}

async function observeJsonAction(page, method, path, action, timeoutMs = config.actionTimeoutMs) {
  const responsePromise = page.waitForResponse(
    (response) => responseMatches(response, method, path),
    { timeout: timeoutMs },
  );
  void responsePromise.catch(() => undefined);
  await action();
  const response = await responsePromise;
  if (!response.ok()) {
    const error = new Error(`UI action ${method} ${path} failed with HTTP ${response.status()}`);
    error.status = response.status();
    throw error;
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`UI action ${method} ${path} returned invalid JSON`);
  }
  return { response, payload };
}

async function pollValue(read, accept, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  while (Date.now() < deadline) {
    lastValue = await read();
    if (accept(lastValue)) return lastValue;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`${label} did not reach the required state before timeout`);
}

async function waitForEnabled(locator, timeoutMs = config.actionTimeoutMs) {
  await pollValue(
    () => locator.isEnabled().catch(() => false),
    Boolean,
    timeoutMs,
    "UI action",
  );
}

async function acceptNextConfirm(page, action) {
  let resolveDialog;
  let rejectDialog;
  const dialogHandled = new Promise((resolve, reject) => {
    resolveDialog = resolve;
    rejectDialog = reject;
  });
  page.once("dialog", (dialog) => {
    if (dialog.type() !== "confirm") {
      void dialog.dismiss().finally(() => rejectDialog(new Error("Unexpected browser dialog")));
      return;
    }
    void dialog.accept().then(resolveDialog, rejectDialog);
  });
  await action();
  await dialogHandled;
}

function candidateIdentity(engineName) {
  const token = `${Date.now().toString(36)}-${randomBytes(3).toString("hex")}`;
  return {
    agentId: `agv-e2e-${engineName.slice(0, 3)}-${token}`,
    initialName: `AgentGov ${engineName} candidate`,
    revisedName: `AgentGov ${engineName} candidate v2`,
  };
}

async function fillNativeCandidateDrawer(page, identity, revised) {
  const drawer = page.getByTestId("settings-native-agent-drawer");
  await drawer.waitFor({ timeout: config.actionTimeoutMs });
  const primaryFields = drawer.getByTestId("settings-native-agent-primary-fields");
  const nameInput = primaryFields.locator('input[type="text"]').first();
  const promptInput = primaryFields.locator("textarea").first();
  requireCondition(await nameInput.count() === 1, "Native Agent name field is unavailable");
  requireCondition(await promptInput.count() === 1, "Native Agent system prompt field is unavailable");
  if (!revised) await drawer.getByTestId("settings-native-agent-id").fill(identity.agentId);
  await nameInput.fill(revised ? identity.revisedName : identity.initialName);
  await promptInput.fill(revised
    ? "你是受 AgentGov 管理的真实技术验收 Agent。直接、简洁地回应用户，不调用工具。版本二。"
    : "你是受 AgentGov 管理的真实技术验收 Agent。直接、简洁地回应用户，不调用工具。");
}

export function assertCandidateReceipt(receipt, identity, expectedAction) {
  requireCondition(receipt?.agent?.agent_id === identity.agentId, "Candidate receipt has a different Agent identity");
  requireCondition(receipt?.agent?.status === "draft", "Unpublished candidate Agent must remain draft");
  requireCondition(receipt?.action === expectedAction, "Candidate receipt has an unexpected action");
  requireCondition(typeof receipt?.change_set_id === "string" && receipt.change_set_id, "Candidate receipt has no change set");
  requireCondition(isCommitSha(receipt?.base_commit_sha), "Candidate receipt has an invalid base commit");
  requireCondition(isCommitSha(receipt?.candidate_commit_sha), "Candidate receipt has an invalid candidate commit");
  requireCondition(receipt.base_commit_sha !== receipt.candidate_commit_sha, "Candidate must not equal the live base commit");
  requireCondition(receipt.published === false, "Candidate receipt must not imply publication");
}

export async function assertLiveHead(configValue, identity, expectedCommit) {
  const ref = await apiJson(
    configValue,
    `/api/agent-repository/current?${new URLSearchParams({ agent_id: identity.agentId })}`,
  );
  requireCondition(ref?.commit_sha === expectedCommit, "Live Agent HEAD changed before publication");
  return ref;
}

async function createAndReviseCandidate(page, identity) {
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-panel").waitFor({ timeout: config.actionTimeoutMs });
  await page.getByTestId("settings-native-agent-open").click();
  await fillNativeCandidateDrawer(page, identity, false);
  const path = `/api/agent-registry/${encodeURIComponent(identity.agentId)}/native-candidate`;
  const first = await observeJsonAction(
    page,
    "POST",
    path,
    () => page.getByTestId("settings-native-agent-submit").click(),
  );
  assertCandidateReceipt(first.payload, identity, "created");
  await page.getByTestId("settings-workspace-import-receipt").waitFor();
  await assertLiveHead(config, identity, first.payload.base_commit_sha);

  await fillNativeCandidateDrawer(page, identity, true);
  const second = await observeJsonAction(
    page,
    "POST",
    path,
    () => page.getByTestId("settings-native-agent-submit").click(),
  );
  assertCandidateReceipt(second.payload, identity, "candidate_committed");
  const requestBody = second.response.request().postDataJSON();
  requireCondition(requestBody?.change_set_id === first.payload.change_set_id, "Candidate continuation omitted its change set identity");
  requireCondition(requestBody?.expected_candidate_commit_sha === first.payload.candidate_commit_sha, "Candidate continuation used a stale commit guard");
  requireCondition(!Object.prototype.hasOwnProperty.call(requestBody || {}, "expected_current_commit_sha"), "Candidate continuation carried the live HEAD guard");
  requireCondition(second.payload.change_set_id === first.payload.change_set_id, "Candidate revision created a second change set");
  requireCondition(second.payload.base_commit_sha === first.payload.base_commit_sha, "Candidate revision changed its live base");
  requireCondition(second.payload.candidate_commit_sha !== first.payload.candidate_commit_sha, "Candidate revision did not create a new candidate commit");
  await assertLiveHead(config, identity, first.payload.base_commit_sha);
  await assertSingleOpenCandidate(identity.agentId, second.payload);
  await page.getByTestId("settings-candidate-open-governance").click();
  await page.getByTestId("release-workbench").waitFor({ timeout: config.actionTimeoutMs });
  return { first: first.payload, revised: second.payload };
}

async function assertSingleOpenCandidate(agentId, expected) {
  const changeSets = await apiJson(config, "/api/agent-change-sets");
  const open = changeSets.filter((item) => item.agent_id === agentId
    && !new Set(["published", "abandoned", "rejected", "failed"]).has(item.status));
  requireCondition(open.length === 1, "Agent has more than one open candidate");
  requireCondition(open[0].change_set_id === expected.change_set_id, "Open candidate identity changed");
  requireCondition(open[0].candidate_commit_sha === expected.candidate_commit_sha, "Open candidate commit changed unexpectedly");
}

async function verifyNativeCandidateDiff(candidate) {
  // Native 表单场景的业务断言；完整 UI Diff 与一次审批由共享 review helper 验证。
  const diff = await apiJson(config, `/api/agent-change-sets/${encodeURIComponent(candidate.change_set_id)}/diff`);
  requireCondition(diff?.from_version_id === candidate.base_commit_sha, "Candidate Diff has a different base commit");
  requireCondition(diff?.to_version_id === candidate.candidate_commit_sha, "Candidate Diff has a different target commit");
  const changedPaths = [
    ...(diff.added || []).map((entry) => entry.path),
    ...(diff.modified || []).map((entry) => entry.path),
    ...(diff.deleted || []).map((entry) => entry.path),
  ];
  for (const requiredPath of ["AGENT.md", "agent.yaml", "tests/README.md", "tests/test_native_agent_harness_contract.py"]) {
    requireCondition(changedPaths.includes(requiredPath), `Candidate Diff is missing ${requiredPath}`);
  }
  const promptDiff = await apiJson(
    config,
    `/api/agent-change-sets/${encodeURIComponent(candidate.change_set_id)}/file-diff?${new URLSearchParams({ path: "AGENT.md" })}`,
  );
  requireCondition(promptDiff?.from_version_id === candidate.base_commit_sha, "Native prompt Diff has a different base commit");
  requireCondition(promptDiff?.to_version_id === candidate.candidate_commit_sha, "Native prompt Diff has a different candidate commit");
  requireCondition(String(promptDiff?.unified_diff || "").includes("版本二。"), "AGENT.md Diff is missing the revised prompt line");
}

async function waitForTestRun(config, testRunId) {
  return pollValue(
    () => apiJson(config, `/api/agent-test-runs/${encodeURIComponent(testRunId)}`),
    (run) => TERMINAL_TEST_STATES.has(run?.status),
    config.testRunTimeoutMs,
    "Candidate Workspace pytest",
  );
}

function assertCandidateTestIdentity(run, candidate, suite) {
  requireCondition(run?.agent_id === candidate.agent.agent_id, "Test run belongs to a different Agent");
  requireCondition(run?.change_set_id === candidate.change_set_id, "Test run belongs to a different change set");
  requireCondition(run?.commit_sha === candidate.candidate_commit_sha, "Test run is not pinned to the exact candidate commit");
  requireCondition(run?.suite_digest === suite.suite_digest, "Test run suite digest changed after scheduling");
}

function assertRealCandidateTest(run, candidate, suite) {
  assertCandidateTestIdentity(run, candidate, suite);
  requireCondition(run?.status === "passed" && run.exit_code === 0, "Candidate Workspace pytest did not pass");
  requireCondition(Array.isArray(run.command) && run.command.includes("pytest") && run.command.at(-1) === "tests", "Candidate test did not execute the fixed pytest suite command");
  requireCondition(typeof run.started_at === "string" && typeof run.completed_at === "string", "Candidate test has no real process timing evidence");
}

export function recordCandidateReceiptProgress(progress, candidate, receipt, field) {
  if (!new Set(["test_run_id", "release_id"]).has(field)) throw new Error("Unsupported candidate receipt field");
  if (receipt?.agent_id === candidate.agent.agent_id
      && receipt.change_set_id === candidate.change_set_id
      && receipt.commit_sha === candidate.candidate_commit_sha
      && typeof receipt[field] === "string") progress[field] = receipt[field];
}

async function runCandidateTestThroughUi(page, config, candidate, suite, progress) {
  const testPath = `/api/agent-change-sets/${encodeURIComponent(candidate.change_set_id)}/test-runs`;
  const created = await observeJsonAction(
    page,
    "POST",
    testPath,
    () => page.getByTestId("release-action-run-tests").click(),
    config.actionTimeoutMs,
  );
  requireCondition(created.response.status() === 202, "Candidate test was not accepted asynchronously");
  recordCandidateReceiptProgress(progress, candidate, created.payload, "test_run_id");
  const testRun = await waitForTestRun(config, created.payload.test_run_id);
  assertCandidateTestIdentity(testRun, candidate, suite);
  return testRun;
}

export async function testApproveAndPublish(page, config, candidate, progress = {}) {
  progress.stage = "candidate_test";
  const suite = await apiJson(
    config,
    `/api/agent-registry/${encodeURIComponent(candidate.agent.agent_id)}/test-suite?${new URLSearchParams({ commit_sha: candidate.candidate_commit_sha })}`,
  );
  requireCondition(suite?.commit_sha === candidate.candidate_commit_sha, "Test suite is not pinned to the candidate commit");
  requireCondition(suite?.tests_directory_present === true && suite.test_file_count >= 1, "Candidate has no runnable Workspace pytest suite");
  requireCondition(suite?.readme_present === true && typeof suite.suite_digest === "string", "Candidate test suite lacks governed metadata");
  progress.suite_digest = suite.suite_digest;
  let testRun = await runCandidateTestThroughUi(page, config, candidate, suite, progress);
  if (testRun.status === "error" && isRuntimeTemplateRestartRequired(testRun.error)) {
    await restartCandidateRuntime({ signal: testRun.error, stage: "candidate_test", maintenance: config.runtimeMaintenance });
    testRun = await runCandidateTestThroughUi(page, config, candidate, suite, progress);
  }
  assertRealCandidateTest(testRun, candidate, suite);
  await pollValue(
    () => page.getByTestId("release-gate-tests").getAttribute("data-state"),
    (state) => state === "pass",
    config.actionTimeoutMs,
    "Workspace pytest UI gate",
  );
  progress.stage = "candidate_review_approve";
  const reviewed = await reviewAndApprovePassedCandidate(page, config, {
    changeSetId: candidate.change_set_id,
    candidateCommitSha: candidate.candidate_commit_sha,
    testRunId: testRun.test_run_id,
    suiteDigest: suite.suite_digest,
  });
  progress.diff_digest = reviewed.diffDigest;
  const diffEvidence = {
    changedFileCount: reviewed.reviewedFileCount,
    diffDigest: reviewed.diffDigest,
    reviewedFiles: reviewed.reviewedFiles,
  };

  const publishPath = `/api/agent-change-sets/${encodeURIComponent(candidate.change_set_id)}/publish`;
  progress.stage = "candidate_publish";
  await waitForEnabled(page.getByTestId("release-action-publish"), config.actionTimeoutMs);
  const published = await observeJsonAction(
    page,
    "POST",
    publishPath,
    () => page.getByTestId("release-action-publish").click(),
    config.testRunTimeoutMs,
  );
  const release = published.payload;
  recordCandidateReceiptProgress(progress, candidate, release, "release_id");
  const publicationRequest = published.response.request().postDataJSON();
  requireCondition(publicationRequest?.expected_candidate_commit_sha === candidate.candidate_commit_sha, "Publication request omitted the reviewed candidate commit");
  requireCondition(publicationRequest?.expected_diff_digest === diffEvidence.diffDigest, "Publication request omitted the reviewed Diff digest");
  requireCondition(publicationRequest?.expected_test_run_id === testRun.test_run_id, "Publication request omitted the reviewed test run");
  requireCondition(publicationRequest?.expected_suite_digest === suite.suite_digest, "Publication request omitted the reviewed test suite");
  requireCondition(release?.status === "published", "Candidate publication did not complete");
  requireCondition(release?.agent_id === candidate.agent.agent_id, "Release belongs to a different Agent");
  requireCondition(release?.change_set_id === candidate.change_set_id, "Release belongs to a different change set");
  requireCondition(release?.commit_sha === candidate.candidate_commit_sha, "Release commit differs from the tested candidate");
  requireCondition(release?.force_published === false, "Candidate lifecycle acceptance must not force publication");
  progress.stage = "candidate_binding";
  const binding = await waitForPublishedBinding(
    config,
    candidate.agent.agent_id,
    candidate.change_set_id,
    candidate.candidate_commit_sha,
  );
  return { ...diffEvidence, suite, testRun, release, binding };
}

async function waitForPublishedBinding(config, agentId, changeSetId, candidateCommit) {
  const agent = await pollValue(
    async () => (await apiJson(config, "/api/agent-registry")).find((item) => item.agent_id === agentId),
    (item) => item?.status === "active" && item.provisioned === true && Boolean(item.runtime_agent_id),
    config.testRunTimeoutMs,
    "Published Agent activation",
  );
  requireCondition(agent.agent_version_id === candidateCommit, "Active Agent version differs from the released candidate");
  const binding = await getCurrentRuntimeAgent(config, agentId);
  requireCondition(binding.agent_version_id === candidateCommit, "Runtime binding differs from the released candidate");
  requireCondition(binding.runtime_agent_id === agent.runtime_agent_id, "Registry and Runtime bindings disagree");
  await assertLiveHead(config, { agentId }, candidateCommit);
  const changeSet = await apiJson(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}`);
  requireCondition(changeSet?.status === "published", "Published change set regressed to a nonterminal state");
  requireCondition(changeSet?.candidate_commit_sha === candidateCommit, "Published change set lost its candidate binding");
  return binding;
}

async function canonicalReplyEvidence(run) {
  const query = new URLSearchParams({ agent_id: run.runtime_agent_id, limit: "200" });
  const payload = await apiJson(
    config,
    `/api/runtime/sessions/${encodeURIComponent(run.session_id)}/messages?${query}`,
  );
  const replyId = run.reply_ids?.at(-1);
  const reply = payload?.messages?.find((message) => message.id === replyId && message.role === "assistant");
  const text = reply?.content
    ?.filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("")
    .trim();
  requireCondition(replyId && reply?.finished_reason === "completed" && !reply.error && text, "Canonical Runtime reply is incomplete");
  return {
    replyId,
    replyUtf8Bytes: Buffer.byteLength(text, "utf8"),
    replySha256: sha256(text),
  };
}

async function runPlaygroundMessage(page, network, agentId, binding, state) {
  await page.locator('.settings-panel [aria-label="关闭"]').click();
  await page.getByTestId("settings-panel").waitFor({ state: "detached" });
  const switcher = page.getByTestId("topbar-agent-switcher");
  await switcher.locator(`option[value="${agentId}"]`).waitFor({ timeout: config.actionTimeoutMs });
  await switcher.selectOption(agentId);
  await page.getByTestId("nav-playground").click();
  const composer = page.getByTestId("chat-composer-input");
  await waitForEnabled(composer, config.actionTimeoutMs);
  await page.getByTestId("playground-session-trigger").click();
  await page.getByTestId("welcome-card").waitFor({ timeout: config.actionTimeoutMs });

  const sessionResponse = page.waitForResponse((response) => responseMatches(response, "POST", "/api/runtime/sessions/"), { timeout: config.actionTimeoutMs });
  const streamResponse = page.waitForResponse((response) => {
    const path = new URL(response.url()).pathname;
    return response.request().method() === "GET" && /^\/api\/runtime\/sessions\/[^/]+\/stream$/.test(path);
  }, { timeout: config.actionTimeoutMs });
  const chatResponse = page.waitForResponse((response) => responseMatches(response, "POST", "/api/runtime/chat/"), { timeout: config.actionTimeoutMs });
  await composer.fill(scenario.input);
  await page.getByTestId("chat-send").click();
  const capturedSession = sessionResponse.then(async (response) => {
    const payload = await response.json();
    if (response.ok() && typeof payload?.session_id === "string") state.sessionId = payload.session_id;
    return { response, payload };
  });
  const capturedChat = chatResponse.then(async (response) => {
    const [payload, headers] = await Promise.all([response.json(), response.allHeaders()]);
    if (response.ok() && headers["x-agentgov-run-id"]) state.runId = headers["x-agentgov-run-id"];
    return { response, payload, headers };
  });
  const settled = await Promise.allSettled([capturedSession, streamResponse, capturedChat]);
  const rejected = settled.find((item) => item.status === "rejected");
  if (rejected) throw rejected.reason;
  const [{ response: sessionHttp, payload: sessionPayload }, streamHttp, {
    response: chatHttp,
    payload: chatPayload,
    headers: chatHeaders,
  }] = settled.map((item) => item.value);
  const sessionId = sessionPayload?.session_id;
  const runId = chatHeaders["x-agentgov-run-id"];
  requireCondition(sessionHttp.ok() && typeof sessionId === "string", "Playground did not create a real Runtime Session");
  requireCondition(streamHttp.ok() && streamHttp.headers()["content-type"]?.startsWith("text/event-stream"), "Playground Runtime event stream is invalid");
  requireCondition(chatHttp.ok() && chatPayload?.session_id === sessionId && runId, "Playground chat receipt is incomplete");
  const chatBody = chatHttp.request().postDataJSON();
  requireCondition(chatBody?.agent_id === binding.runtime_agent_id && chatBody?.session_id === sessionId, "Playground chat used a different Runtime binding");
  const expected = { ...binding, run_id: runId, session_id: sessionId };
  const terminal = await waitForTerminalRuntimeRun(config, expected);
  requireCondition(terminal.status === "succeeded" && terminal.reply_ids?.length && /^[0-9a-f]{32}$/.test(terminal.trace_id || ""), "Real provider run did not succeed with reply and Trace references");
  const traced = await waitForCompleteRuntimeTrace(config, terminal);
  const reply = await canonicalReplyEvidence(traced);
  const rendered = page.locator(`[data-message-id="${reply.replyId}"] [data-testid="message-markdown"]`);
  await rendered.waitFor({ timeout: config.actionTimeoutMs });
  requireCondition((await rendered.innerText()).trim().length > 0, "Canonical assistant reply was not rendered in Playground");
  const streamsClosed = await network.settle(config.actionTimeoutMs);
  requireCondition(streamsClosed, "Playground Runtime event stream did not close after the terminal run");
  const timing = assertStreamTiming(network, runId, sessionId);
  return { run: traced, reply, timing };
}

function assertStreamTiming(network, runId, sessionId) {
  const chat = network.chats.find((event) => event.runId === runId);
  const path = `/api/runtime/sessions/${encodeURIComponent(sessionId)}/stream`;
  const stream = network.streams.filter((event) => event.path === path && chat && event.at <= chat.at).at(-1)
    || network.streams.find((event) => event.path === path);
  requireCondition(chat && stream?.responseAt, "No exact Playground stream connection evidence was observed");
  const readyMs = stream.responseAt - stream.at;
  requireCondition(stream.at <= chat.at, "Playground chat started before requesting its Runtime stream");
  requireCondition(readyMs >= 0 && readyMs <= STREAM_READY_LIMIT_MS, "Runtime event stream was not ready within the technical limit");
  requireCondition(Boolean(stream.closedAt), "Runtime event stream has no closure evidence");
  return { streamReadyMs: Math.round(readyMs), streamPrecededChat: true, streamClosed: true };
}

async function renameReloadAndDeleteSession(page, agentId, binding, runEvidence) {
  const sessionId = runEvidence.run.session_id;
  const replyId = runEvidence.reply.replyId;
  const sidebar = page.getByTestId("playground-session-sidebar");
  if (!await sidebar.isVisible().catch(() => false)) await page.getByTestId("playground-session-trigger").click();
  const item = page.locator(`[data-testid="playground-session-item"][data-session-id="${sessionId}"]`);
  await item.waitFor({ timeout: config.actionTimeoutMs });
  await item.getByRole("button", { name: "重命名会话" }).click();
  const renamedTitle = `candidate-${sha256(sessionId).slice(0, 12)}`;
  await item.getByTestId("playground-session-rename-input").fill(renamedTitle);
  const sessionPath = `/api/runtime/sessions/${encodeURIComponent(sessionId)}`;
  const renamedPromise = page.waitForResponse(
    (response) => responseMatches(response, "PATCH", sessionPath),
    { timeout: config.actionTimeoutMs },
  );
  await item.getByRole("button", { name: "保存会话名称" }).click();
  const renamed = await renamedPromise;
  requireCondition(renamed.ok(), "Runtime Session rename failed");
  await sidebar.getByRole("button", { name: "刷新" }).click();
  await pollValue(() => item.innerText(), (text) => text.includes(renamedTitle), config.actionTimeoutMs, "Renamed Session UI");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByTestId("playground").waitFor({ timeout: config.actionTimeoutMs });
  await pollValue(() => page.getByTestId("topbar-agent-switcher").inputValue(), (value) => value === agentId, config.actionTimeoutMs, "Selected Agent recovery");
  await page.getByTestId("playground-session-trigger").click();
  const recoveredItem = page.locator(`[data-testid="playground-session-item"][data-session-id="${sessionId}"]`);
  await recoveredItem.waitFor({ timeout: config.actionTimeoutMs });
  requireCondition((await recoveredItem.innerText()).includes(renamedTitle), "Reload did not recover the renamed Session");
  await page.locator(`[data-message-id="${replyId}"]`).waitFor({ timeout: config.actionTimeoutMs });

  const deletionPromise = page.waitForResponse((response) => responseMatches(response, "DELETE", sessionPath), { timeout: config.actionTimeoutMs });
  await acceptNextConfirm(page, () => recoveredItem.getByRole("button", { name: "删除会话" }).click());
  const deletion = await deletionPromise;
  requireCondition(deletion.ok(), "Runtime Session deletion failed");
  await recoveredItem.waitFor({ state: "detached", timeout: config.actionTimeoutMs });
  const sessions = await apiJson(config, `/api/runtime/sessions/?${new URLSearchParams({ governance_agent_id: agentId })}`);
  requireCondition(Array.isArray(sessions?.sessions)
    && !sessions.sessions.some((entry) => entry?.session?.id === sessionId), "Deleted Session remains in canonical history");
  requireCondition(binding.runtime_agent_id === runEvidence.run.runtime_agent_id, "Session lifecycle used a different Runtime Agent");
  return { renamed: true, historyRecovered: true, sessionDeleted: true };
}

async function deleteAgentThroughUi(page, agentId) {
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-panel").waitFor({ timeout: config.actionTimeoutMs });
  const row = page.getByTestId("settings-agent-item").filter({ hasText: agentId });
  await row.waitFor({ timeout: config.actionTimeoutMs });
  requireCondition(await row.count() === 1, "Temporary Agent row is ambiguous");
  await row.getByTestId("settings-agent-actions-trigger").click();
  const deletePath = `/api/agent-registry/${encodeURIComponent(agentId)}`;
  const deletionPromise = page.waitForResponse((response) => responseMatches(response, "DELETE", deletePath), { timeout: config.testRunTimeoutMs });
  await acceptNextConfirm(page, () => page.getByTestId("settings-agent-delete").click());
  const deletion = await deletionPromise;
  requireCondition(deletion.ok(), "Temporary Agent deletion failed");
  const payload = await deletion.json();
  requireCondition(payload?.deleted?.agent_id === agentId, "Agent deletion receipt has a different identity");
  requireCondition(payload?.workspace_removed === true && payload?.cleanup_complete === true, "Temporary Agent storage cleanup was incomplete");
  await row.waitFor({ state: "detached", timeout: config.testRunTimeoutMs });
  const agents = await apiJson(config, "/api/agent-registry");
  requireCondition(!agents.some((agent) => agent.agent_id === agentId), "Deleted Agent remains visible in the registry");
  return { agentDeleted: true, workspaceRemoved: true };
}

async function cancelRunForCleanup(state) {
  if (!state.runId) return;
  const run = await apiJson(config, `/api/agent-runs/${encodeURIComponent(state.runId)}`).catch(() => null);
  if (!run || TERMINAL_RUN_STATES.has(run.status)) return;
  await apiRequest(config, `/api/agent-runs/${encodeURIComponent(state.runId)}/cancel`, jsonInit("POST", {})).catch(() => null);
  await pollValue(
    () => apiJson(config, `/api/agent-runs/${encodeURIComponent(state.runId)}`).catch(() => null),
    (item) => !item || TERMINAL_RUN_STATES.has(item.status),
    config.actionTimeoutMs,
    "Cleanup run cancellation",
  ).catch(() => null);
}

async function cleanupAcceptanceState(state) {
  const failures = [];
  await cancelRunForCleanup(state).catch(() => failures.push("run"));
  if (state.sessionId && state.runtimeAgentId && !state.sessionDeleted) {
    const query = new URLSearchParams({ agent_id: state.runtimeAgentId });
    const result = await apiRequest(
      config,
      `/api/runtime/sessions/${encodeURIComponent(state.sessionId)}?${query}`,
      { method: "DELETE", headers: { "X-User-ID": "agentgov-ui" } },
    ).catch(() => null);
    const sessions = await apiJson(
      config,
      `/api/runtime/sessions/?${new URLSearchParams({ governance_agent_id: state.agentId })}`,
    ).catch(() => null);
    if (!result || (!result.response.ok && result.status !== 404)
      || !Array.isArray(sessions?.sessions)
      || sessions.sessions.some((entry) => entry?.session?.id === state.sessionId)) {
      failures.push("session");
    }
  }
  if (state.agentId && !state.agentDeleted) {
    const result = await apiRequest(
      config,
      `/api/agent-registry/${encodeURIComponent(state.agentId)}`,
      { method: "DELETE" },
    ).catch(() => null);
    const accepted = result?.status === 404 || (result?.response.ok
      && result.payload?.deleted?.agent_id === state.agentId
      && result.payload?.workspace_removed === true
      && result.payload?.cleanup_complete === true);
    const agents = await apiJson(config, "/api/agent-registry").catch(() => null);
    if (!accepted || !agents || agents.some((agent) => agent.agent_id === state.agentId)) failures.push("agent");
  }
  return [...new Set(failures)];
}

async function runBrowser(engineName, browserType) {
  const identity = candidateIdentity(engineName);
  const state = { agentId: identity.agentId };
  const browser = await browserType.launch({ headless: process.env.PLAYWRIGHT_HEADLESS !== "0" });
  const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
  const page = await context.newPage();
  page.setDefaultTimeout(config.actionTimeoutMs);
  const network = attachCancelNetworkEvidence(page, config.apiBase);
  const diagnostics = attachUiDiagnostics(page, network);
  let stage = `${engineName}:ui_ready`;
  let result;
  let caught;
  try {
    await waitForUi(config.uiBase, 60_000);
    await page.goto(config.uiBase, { waitUntil: "domcontentloaded" });
    await page.getByTestId("playground").waitFor({ timeout: 60_000 });
    stage = `${engineName}:candidate_create_and_revision`;
    const candidate = await createAndReviseCandidate(page, identity);
    stage = `${engineName}:candidate_test_approve_publish`;
    await verifyNativeCandidateDiff(candidate.revised);
    const release = await testApproveAndPublish(page, config, candidate.revised);
    state.runtimeAgentId = release.binding.runtime_agent_id;
    stage = `${engineName}:real_playground_run`;
    const playground = await runPlaygroundMessage(page, network, identity.agentId, release.binding, state);
    state.runId = playground.run.run_id;
    state.sessionId = playground.run.session_id;
    stage = `${engineName}:session_lifecycle`;
    const session = await renameReloadAndDeleteSession(page, identity.agentId, release.binding, playground);
    state.sessionDeleted = session.sessionDeleted;
    stage = `${engineName}:agent_cleanup`;
    const cleanup = await deleteAgentThroughUi(page, identity.agentId);
    state.agentDeleted = cleanup.agentDeleted;
    requireCondition(diagnostics.length === 0, "Browser observed an unexpected UI or network diagnostic");
    result = safeBrowserEvidence(engineName, identity, candidate, release, playground, session, cleanup);
  } catch (error) {
    caught = error;
  }
  const cleanupFailures = await cleanupAcceptanceState(state);
  const streamsSettled = await network.settle(config.actionTimeoutMs).catch(() => false);
  const resourceFailures = await cleanupResources([
    { name: "browser_context", close: () => context.close() },
    { name: "browser", close: () => browser.close() },
  ]);
  if (cleanupFailures.length || !streamsSettled || resourceFailures.length) {
    const error = new Error("RESOURCE_CLEANUP_FAILED");
    error.acceptanceStage = `${engineName}:cleanup`;
    error.acceptanceEvents = safeDiagnostics(diagnostics);
    throw error;
  }
  if (caught) {
    caught.acceptanceStage = stage;
    caught.acceptanceEvents = safeDiagnostics(diagnostics);
    throw caught;
  }
  return result;
}

function safeBrowserEvidence(engineName, identity, candidate, release, playground, session, cleanup) {
  return {
    engine: engineName,
    agent_id: identity.agentId,
    change_set_id: candidate.revised.change_set_id,
    base_commit_sha: candidate.revised.base_commit_sha,
    first_candidate_commit_sha: candidate.first.candidate_commit_sha,
    candidate_commit_sha: candidate.revised.candidate_commit_sha,
    changed_file_count: release.changedFileCount,
    diff_digest: release.diffDigest,
    suite_digest: release.suite.suite_digest,
    test_run_id: release.testRun.test_run_id,
    test_duration_seconds: release.testRun.duration_seconds,
    release_id: release.release.release_id,
    runtime_agent_id: release.binding.runtime_agent_id,
    agent_version_id: release.binding.agent_version_id,
    run_id: playground.run.run_id,
    session_id: playground.run.session_id,
    trace_id: playground.run.trace_id,
    reply_id: playground.reply.replyId,
    reply_utf8_bytes: playground.reply.replyUtf8Bytes,
    reply_sha256: playground.reply.replySha256,
    ...playground.timing,
    ...session,
    ...cleanup,
  };
}

async function main() {
  const results = [];
  try {
    initializeAcceptance();
    const browserTypes = { chromium, firefox };
    for (const engineName of browserPlan) {
      results.push(await runBrowser(engineName, browserTypes[engineName]));
    }
    console.log(JSON.stringify({
      status: "passed",
      mode: "real-container-technical",
      formal_release_acceptance: false,
      scenario_id: scenario.scenario_id,
      scenario_file_sha256: reviewed.scenario_file_sha256,
      input_utf8_bytes: Buffer.byteLength(scenario.input, "utf8"),
      input_sha256: sha256(scenario.input),
      browsers: results,
    }, null, 2));
  } catch (error) {
    console.error(JSON.stringify(failureDiagnostic(
      error,
      error?.acceptanceStage || "bootstrap",
      error?.acceptanceEvents || [],
    )));
    process.exitCode = 2;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(JSON.stringify(failureDiagnostic(error, "bootstrap", [])));
    process.exit(2);
  });
}
