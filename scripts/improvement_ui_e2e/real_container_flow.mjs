import { createHash } from "node:crypto";

import {
  apiJson,
  assertHostileTestRunRejected,
  getCurrentRuntimeAgent,
  runReviewedScenario,
  seedBaseImprovement,
} from "./runtime_client.mjs";
import {
  assertNoForbiddenUiRequests,
  attachDiagnostics,
  acceptanceError,
  auditState,
  unexpectedDiagnostics,
} from "./page_audit.mjs";
import { reviewAndApprovePassedCandidate } from "./candidate_review.mjs";
import {
  isRuntimeTemplateRestartRequired,
  restartCandidateRuntime,
} from "./candidate_runtime_restart.mjs";
import { configureUiApiConnection } from "./ui_connection.mjs";

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 980 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
];
const MAX_GOVERNOR_PLAN_ATTEMPTS = 3;
const MAX_REGRESSION_DESIGN_ATTEMPTS = 3;
const TERMINAL_TEST_RUN_STATES = new Set(["passed", "failed", "error", "cancelled", "interrupted"]);
const FIXED_TEST_ARGUMENTS = [
  "-I",
  "-m",
  "pytest",
  "-q",
  "-p",
  "agentgov_testkit.pytest_plugin",
  "--noconftest",
  "--import-mode=importlib",
  "-c",
  "/dev/null",
  "tests",
];

async function openImprovement(page, config, seed) {
  await page.goto(config.uiBase, { waitUntil: "domcontentloaded" });
  const switcher = page.getByTestId("topbar-agent-switcher");
  await switcher.waitFor({ timeout: 30000 });
  await switcher.selectOption(seed.agent.agent_id);
  await page.getByTestId("nav-improvement").click();
  await page.getByTestId("improvement-workbench").waitFor({ timeout: 30000 });
  const scope = page.getByTestId("improvement-scope-filter");
  const scopedResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET"
      && url.pathname === "/api/improvements"
      && url.searchParams.get("agent_id") === seed.agent.agent_id;
  }, { timeout: 30000 });
  await scope.selectOption(seed.agent.agent_id);
  const response = await scopedResponse;
  if (!response.ok() || await scope.inputValue() !== seed.agent.agent_id) {
    throw new Error("improvement workbench did not apply the exact Agent scope");
  }
  const target = page.locator('[data-testid="improvement-list-item"][data-item-id="' + seed.item.improvement_id + '"]').first();
  await target.waitFor({ timeout: 30000 });
  await target.click();
  await page.locator('[data-testid="improvement-detail"][data-item-id="' + seed.item.improvement_id + '"]').waitFor({ timeout: 30000 });
}

async function responseFailure(response, code) {
  const error = acceptanceError(code, response.status());
  try {
    error.generatedTestError = /"error_type"\s*:\s*"GeneratedAgentTestError"/.test(await response.text());
  } catch {
    error.generatedTestError = false;
  }
  return error;
}

async function clickPrimaryBusinessAction(page, config, dataAction, endpointSuffix) {
  const button = page.locator('[data-testid="primary-action"][data-action="' + dataAction + '"]').first();
  await button.waitFor({ timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST" && url.pathname.endsWith(endpointSuffix);
  }, { timeout: config.actionTimeoutMs });
  await button.click();
  const response = await responsePromise;
  if (!response.ok()) {
    await page.getByTestId("decision-operation-error").waitFor({ timeout: 15000 }).catch(() => {});
    throw await responseFailure(response, "PRIMARY_BUSINESS_ACTION_FAILED");
  }
  await page.getByTestId("decision-operation-status").waitFor({ state: "detached", timeout: 30000 }).catch(() => {});
  return { action: dataAction, endpoint: new URL(response.url()).pathname, status: response.status() };
}

async function regenerateOptimizationPlan(page, config, improvementId) {
  const button = page.getByTestId("decision-regenerate-optimization-plan");
  await button.waitFor({ timeout: 30000 });
  const endpoint = "/api/improvements/" + improvementId + "/optimization-plan/generate";
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === endpoint
  ), { timeout: config.actionTimeoutMs });
  await button.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw await responseFailure(response, "PLAN_REGENERATION_FAILED");
  }
  await page.getByTestId("decision-operation-status").waitFor({ state: "detached", timeout: 30000 }).catch(() => {});
  return { action: "regenerate-optimization-plan", endpoint, status: response.status() };
}

async function generateRegressionTestDesign(
  page,
  config,
  improvementId,
  actions,
  minimumTestCount,
  requiredTestLiterals = [],
  requiredTestCodeFragments = [],
  allowedTargetPaths = null,
) {
  let design;
  for (let attempt = 1; attempt <= MAX_REGRESSION_DESIGN_ATTEMPTS; attempt += 1) {
    let action;
    try {
      action = await clickPrimaryBusinessAction(
        page,
        config,
        "generate-regression",
        "/api/improvements/" + improvementId + "/regression-test-design/generate",
      );
    } catch (error) {
      const retryableGenerationFailure = error?.httpStatus === 503
        && error?.generatedTestError === true;
      actions.push({
        action: "generate-regression-rejected",
        attempt,
        status: error?.httpStatus || 0,
        retryable: retryableGenerationFailure,
      });
      if (retryableGenerationFailure && attempt < MAX_REGRESSION_DESIGN_ATTEMPTS) continue;
      throw error;
    }
    actions.push({ ...action, attempt });
    design = await apiJson(config, "/api/improvements/" + improvementId + "/regression-test-design");
    const tests = design.tests || [];
    const executable = tests.every((item) => {
      const code = item.test_code || "";
      const assertions = code.match(/^\s*assert\b/gm) || [];
      const assertionLines = code.split("\n").filter((line) => /^\s*assert\b/.test(line));
      return /^tests\/test_.*\.py$/.test(item.target_path || "")
        && (allowedTargetPaths === null || allowedTargetPaths.includes(item.target_path))
        && code.includes("agent.run(")
        && (code.includes("result.text") || code.includes("result.raw"))
        && /assert\s+not\s+\w+\.errors/.test(code)
        && assertions.length >= 2
        && !/\bany\s*\(/.test(code)
        && !/\s+or\s+/.test(code)
        && requiredTestCodeFragments.every((fragment) => code.includes(fragment))
        && requiredTestLiterals.every((literal) => assertionLines.some((line) => (
          line.includes(literal) && !line.includes(" not in ")
        )));
    });
    if (design.regression_test_design_id && tests.length >= minimumTestCount && executable) return design;
  }
  throw acceptanceError("REGRESSION_TEST_DESIGN_INVALID");
}

function assertExecutionTargetScope(seed, execution) {
  const allowed = new Set(seed.authorizedTargetPaths || []);
  const diff = execution.applied_diff || {};
  const entries = [
    ...(diff.added || []),
    ...(diff.modified || []),
    ...(diff.deleted || []),
  ];
  const unexpected = entries
    .map((entry) => entry.path)
    .filter((path) => path && !allowed.has(path));
  if (unexpected.length) {
    throw acceptanceError("EXECUTION_OUTSIDE_APPROVED_SCOPE");
  }
  for (const entry of diff.modified || []) {
    const beforeSize = Number(entry.before?.size || 0);
    const afterSize = Number(entry.after?.size || 0);
    if (beforeSize >= 256 && afterSize < beforeSize * 0.5) {
      throw acceptanceError("EXECUTION_TRUNCATED_EXISTING_DOCUMENT");
    }
  }
}

async function confirmAndMaterializeTests(page, config, seed, execution, actions, allowedTargetPaths = null) {
  const improvementId = seed.item.improvement_id;
  const endpoint = "/api/improvements/" + improvementId + "/regression-test-design/confirm";
  const button = page.getByTestId("confirm-regression-tests");
  await button.waitFor({ timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === endpoint
  ), { timeout: config.actionTimeoutMs });
  await button.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw await responseFailure(response, "TEST_MATERIALIZATION_FAILED");
  }
  const confirmed = await response.json();
  const generatedFiles = confirmed.generated_test_files || [];
  if (!generatedFiles.some((path) => /^tests\/test_.*\.py$/.test(path))) {
    throw acceptanceError("CONFIRMED_TEST_FILES_MISSING");
  }
  if (allowedTargetPaths !== null && generatedFiles.some((path) => !allowedTargetPaths.includes(path))) {
    throw acceptanceError("GENERATED_TEST_OUTSIDE_APPROVED_SCOPE");
  }
  if (!confirmed.candidate_commit_sha || confirmed.test_run !== null) {
    throw acceptanceError("TEST_CONFIRMATION_CONTRACT_INVALID");
  }
  const reboundExecution = await apiJson(config, "/api/improvements/" + improvementId + "/execution");
  if (reboundExecution.change_set_id !== execution.change_set_id
      || reboundExecution.applied_agent_version_id !== confirmed.candidate_commit_sha) {
    throw acceptanceError("TEST_CANDIDATE_EXECUTION_BINDING_INVALID");
  }
  actions.push({ action: "confirm-and-materialize-tests", endpoint, status: response.status() });
  return { confirmed, execution: reboundExecution };
}

async function startPlatformTests(page, config, seed, execution, confirmed, actions) {
  const endpoint = "/api/agent-change-sets/" + encodeURIComponent(execution.change_set_id) + "/test-runs";
  const button = page.getByTestId("release-action-run-tests");
  await button.waitFor({ timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === endpoint
  ), { timeout: config.actionTimeoutMs });
  await button.click({ timeout: 30000 });
  const response = await responsePromise;
  if (!response.ok()) throw await responseFailure(response, "PLATFORM_TEST_START_FAILED");
  const run = await response.json();
  if (run.agent_id !== seed.agent.agent_id
      || run.commit_sha !== confirmed.candidate_commit_sha
      || run.change_set_id !== execution.change_set_id) {
    throw acceptanceError("PLATFORM_TEST_BINDING_INVALID");
  }
  actions.push({ action: "run-platform-pytest", endpoint, status: response.status() });
  return run;
}

async function exerciseFourStageActions(page, config, seed, minimumRegressionTestCount = 1, allowedTargetPaths = null) {
  const improvementId = seed.item.improvement_id;
  const actions = [];
  await page.getByTestId("normalized-feedback").waitFor({ timeout: 30000 });
  actions.push(await clickPrimaryBusinessAction(
    page,
    config,
    "generate-attribution",
    "/api/improvements/" + improvementId + "/attribution/generate",
  ));
  const attribution = await apiJson(config, "/api/improvements/" + improvementId + "/attribution");
  if (!attribution.attribution_id) throw new Error("attribution side effect was not persisted");

  actions.push(await clickPrimaryBusinessAction(
    page,
    config,
    "generate-optimization-plan",
    "/api/improvements/" + improvementId + "/optimization-plan/generate",
  ));
  const plan = await apiJson(config, "/api/improvements/" + improvementId + "/optimization-plan");
  if (!plan.optimization_plan_id) throw new Error("optimization plan side effect was not persisted");

  let execution;
  for (let attempt = 1; attempt <= MAX_GOVERNOR_PLAN_ATTEMPTS; attempt += 1) {
    const executionAction = await clickPrimaryBusinessAction(
      page,
      config,
      "apply-execution",
      "/api/improvements/" + improvementId + "/execution/apply",
    );
    actions.push({ ...executionAction, attempt });
    execution = await apiJson(config, "/api/improvements/" + improvementId + "/execution");
    const writable = execution.change_set_id
      && execution.applied_agent_version_id
      && Object.keys(execution.applied_diff || {}).length;
    if (writable) {
      assertExecutionTargetScope(seed, execution);
      break;
    }
    if (attempt === MAX_GOVERNOR_PLAN_ATTEMPTS) {
      throw acceptanceError("GOVERNOR_WRITABLE_PLAN_MISSING");
    }
    const regeneration = await regenerateOptimizationPlan(page, config, improvementId);
    actions.push({ ...regeneration, attempt: attempt + 1 });
  }

  const design = await generateRegressionTestDesign(
    page,
    config,
    improvementId,
    actions,
    minimumRegressionTestCount,
    seed.requiredTestLiterals,
    seed.requiredTestCodeFragments,
    allowedTargetPaths,
  );
  const materialized = await confirmAndMaterializeTests(page, config, seed, execution, actions, allowedTargetPaths);
  const initialRun = await startPlatformTests(
    page,
    config,
    seed,
    materialized.execution,
    materialized.confirmed,
    actions,
  );
  return {
    actions,
    attribution,
    design,
    execution: materialized.execution,
    confirmed: materialized.confirmed,
    initialRun,
    plan,
  };
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForTerminalTestRun(config, testRunId) {
  const deadline = Date.now() + config.testRunTimeoutMs;
  let run;
  while (Date.now() < deadline) {
    run = await apiJson(config, "/api/agent-test-runs/" + encodeURIComponent(testRunId));
    if (TERMINAL_TEST_RUN_STATES.has(run.status)) return run;
    await sleep(1200);
  }
  throw acceptanceError("PLATFORM_TEST_TERMINAL_TIMEOUT");
}

function assertExactTestCandidate(flow, suite, run) {
  if (run.test_run_id !== flow.initialRun.test_run_id
      || run.agent_id !== flow.initialRun.agent_id
      || run.change_set_id !== flow.execution.change_set_id
      || run.commit_sha !== flow.confirmed.candidate_commit_sha) {
    throw acceptanceError("TEST_TERMINAL_CANDIDATE_DRIFT");
  }
  if (!suite.suite_digest
      || suite.agent_id !== run.agent_id
      || suite.commit_sha !== run.commit_sha
      || suite.suite_digest !== flow.initialRun.suite_digest
      || suite.suite_digest !== run.suite_digest) {
    throw acceptanceError("TEST_SUITE_DIGEST_MISMATCH");
  }
}

function assertPassedTestEvidence(flow, suite, run) {
  assertExactTestCandidate(flow, suite, run);
  if (run.status !== "passed") {
    throw acceptanceError("WORKSPACE_PYTEST_NOT_PASSED");
  }
  const [pythonExecutable, ...testArguments] = Array.isArray(run.command) ? run.command : [];
  const pythonName = typeof pythonExecutable === "string" ? pythonExecutable.split("/").at(-1) : "";
  if (!/^python(?:\d+(?:\.\d+)*)?$/.test(pythonName)
      || JSON.stringify(testArguments) !== JSON.stringify(FIXED_TEST_ARGUMENTS)) {
    throw acceptanceError("PLATFORM_TEST_COMMAND_DRIFT");
  }
  if (!suite.tests_directory_present
      || !suite.test_file_count
      || suite.test_files.some((path) => !/^tests\/test_.*\.py$/.test(path))
      || suite.suite_digest !== run.suite_digest) {
    throw acceptanceError("TEST_SUITE_DIGEST_MISMATCH");
  }
  if (!(run.items || []).length || run.items.some((item) => item.outcome !== "passed")) {
    throw acceptanceError("PLATFORM_TEST_ITEM_NOT_PASSED");
  }
  if (!(run.invocations || []).length
      || run.invocations.some((invocation) => invocation.test_run_id !== run.test_run_id
        || invocation.agent_version_id !== run.commit_sha
        || !invocation.langfuse_trace_id
        || (invocation.errors || []).length)) {
    throw acceptanceError("PLATFORM_TEST_INVOCATION_INCOMPLETE");
  }
}

async function waitForPassedGate(page, run) {
  await page.waitForFunction((testRunId) => {
    const details = document.querySelector('[data-testid="release-test-run-details"]');
    const gate = document.querySelector('[data-testid="release-gate-tests"]');
    return details?.textContent?.includes(testRunId) && gate?.getAttribute("data-state") === "pass";
  }, run.test_run_id, { timeout: 60000 });
}

async function assertCandidateDiffScope(config, flow, allowedTargetPaths) {
  const encodedId = encodeURIComponent(flow.execution.change_set_id);
  const diff = await apiJson(config, `/api/agent-change-sets/${encodedId}/diff`);
  const added = (diff.added || []).map((item) => item.path);
  const modified = (diff.modified || []).map((item) => item.path);
  const deleted = (diff.deleted || []).map((item) => item.path);
  const changed = [...added, ...modified, ...deleted];
  const approved = new Set(allowedTargetPaths);
  const expectedTests = flow.confirmed.generated_test_files || [];
  if (diff.to_version_id !== flow.confirmed.candidate_commit_sha
      || changed.length < 2 || new Set(changed).size !== changed.length
      || deleted.length || modified.length !== 1 || modified[0] !== "AGENT.md"
      || !expectedTests.length || expectedTests.some((path) => !added.includes(path))
      || changed.some((path) => !approved.has(path))
      || added.some((path) => !/^tests\/test_.*\.py$/.test(path))) {
    throw acceptanceError("CANDIDATE_DIFF_OUTSIDE_APPROVED_SCOPE");
  }
}

async function publishPassedCandidate(page, config, flow, allowedTargetPaths = null) {
  const changeSetId = flow.execution.change_set_id;
  if (allowedTargetPaths !== null) await assertCandidateDiffScope(config, flow, allowedTargetPaths);
  const approval = await reviewAndApprovePassedCandidate(page, config, {
    changeSetId,
    candidateCommitSha: flow.confirmed.candidate_commit_sha,
    testRunId: flow.terminalRun.test_run_id,
    suiteDigest: flow.suite.suite_digest,
  });
  const endpoint = "/api/agent-change-sets/" + changeSetId + "/publish";
  const button = page.getByTestId("release-action-publish");
  await page.waitForFunction(() => {
    const candidate = document.querySelector('[data-testid="release-action-publish"]');
    return candidate instanceof HTMLButtonElement && !candidate.disabled;
  }, null, { timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === endpoint
  ), { timeout: config.actionTimeoutMs });
  await button.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw await responseFailure(response, "CANDIDATE_PUBLICATION_FAILED");
  }
  const request = response.request().postDataJSON();
  if (request.force !== false
      || request.operator !== "ui"
      || request.force_reason !== undefined
      || request.expected_candidate_commit_sha !== flow.confirmed.candidate_commit_sha
      || request.expected_diff_digest !== approval.diffDigest
      || request.expected_test_run_id !== flow.terminalRun.test_run_id
      || request.expected_suite_digest !== flow.suite.suite_digest) {
    throw acceptanceError("CANDIDATE_PUBLICATION_REQUEST_INVALID");
  }
  const release = await response.json();
  if (release.change_set_id !== changeSetId
      || release.commit_sha !== flow.confirmed.candidate_commit_sha
      || release.force_published) {
    throw acceptanceError("RELEASE_PASSED_COMMIT_BINDING_INVALID");
  }
  await page.getByTestId("release-item").filter({ hasText: release.tag_name || release.release_id }).waitFor({ timeout: 30000 });
  return { ...approval, endpoint, release, status: response.status() };
}

async function assertCreateDrawerFullyVisible(page) {
  const legacyInlineForm = await page.locator(".iw-list-panel .iw-create").count();
  const duplicateScopeText = await page.getByTestId("improvement-scope-label").locator("strong").count();
  if (legacyInlineForm || duplicateScopeText) {
    throw new Error(`obsolete improvement controls remain: inline=${legacyInlineForm} duplicateScope=${duplicateScopeText}`);
  }
  await page.getByTestId("improvement-create-open").click();
  await page.getByTestId("improvement-create-drawer").waitFor({ timeout: 10000 });
  const visibility = await page.evaluate(() => {
    const drawer = document.querySelector('[data-testid="improvement-create-drawer"]');
    if (!(drawer instanceof HTMLElement)) return { drawer: false, clipped: ["improvement-create-drawer"] };
    const drawerRect = drawer.getBoundingClientRect();
    const clipped = [
      "improvement-create-agent",
      "improvement-create-title",
      "improvement-create-submit",
    ].filter((testId) => {
      const element = document.querySelector('[data-testid="' + testId + '"]');
      if (!(element instanceof HTMLElement)) return true;
      const rect = element.getBoundingClientRect();
      return rect.width <= 0
        || rect.height <= 0
        || rect.left < drawerRect.left
        || rect.right > drawerRect.right
        || rect.top < drawerRect.top
        || rect.bottom > drawerRect.bottom
        || rect.top < 0
        || rect.right > window.innerWidth
        || rect.bottom > window.innerHeight;
    });
    return { drawer: true, clipped };
  });
  if (!visibility.drawer || visibility.clipped.length) {
    throw acceptanceError("IMPROVEMENT_DRAWER_CONTROLS_CLIPPED");
  }
  await page.getByTestId("improvement-create-cancel").click();
  await page.getByTestId("improvement-create-drawer").waitFor({ state: "detached", timeout: 5000 });
}

async function verifyResponsiveStates(browser, config, seed, flow, release, configureConnection = false) {
  const results = [];
  for (const viewport of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
    const diagnostics = attachDiagnostics(page, config.apiBase, config.uiBase);
    try {
      if (configureConnection) await configureUiApiConnection(page, config);
      await openImprovement(page, config, seed);
      await assertCreateDrawerFullyVisible(page);
      await page.getByTestId("improvement-terminal").filter({ hasText: "已完成平台测试并发布" }).waitFor({ timeout: 30000 });
      await page.getByTestId("workspace-test-files").filter({ hasText: "已写入发布版本" }).waitFor({ timeout: 30000 });
      await page.getByTestId("confirm-regression-tests").filter({ hasText: "待发布变更已确认" }).waitFor({ timeout: 30000 });
      await page.locator('[data-testid="closed-loop-step"][data-stage-key="test_release"][data-state="done"]').waitFor({ timeout: 30000 });
      await page.getByTestId("regression-test-code-coverage").waitFor({ timeout: 30000 });
      await page.getByTestId("release-item").filter({ hasText: release.tag_name || release.release_id }).waitFor({ timeout: 30000 });
      const success = await auditState(page, viewport.name + "-success");

      await page.getByTestId("nav-asset").click();
      await page.getByTestId("asset-registry").waitFor({ timeout: 30000 });
      await page.getByTestId("asset-center-tab-governance").click();
      await page.getByTestId("governance-asset-registry").waitFor({ timeout: 30000 });
      await page.getByTestId("asset-source-filter").fill("missing-" + seed.stamp);
      await page.locator(".iw-empty").filter({ hasText: "当前范围还没有沉淀资产" }).waitFor({ timeout: 15000 });
      const empty = await auditState(page, viewport.name + "-empty");

      assertNoForbiddenUiRequests(diagnostics.requests);
      const unexpected = unexpectedDiagnostics(diagnostics);
      if (Object.values(unexpected).some((items) => items.length)) {
        throw acceptanceError("RESPONSIVE_BROWSER_DIAGNOSTICS_FAILED");
      }
      results.push({
        viewport,
        success,
        empty,
        test_run_id: flow.terminalRun.test_run_id,
        candidate_commit_sha: flow.confirmed.candidate_commit_sha,
        diagnostics: { httpErrors: diagnostics.httpErrors },
      });
    } finally {
      await page.close();
    }
  }
  return results;
}

export async function verifyCompletedImprovementAcceptance(browser, config, evidence) {
  const required = [
    evidence?.agent_id,
    evidence?.improvement_id,
    evidence?.candidate_commit_sha,
    evidence?.test_run?.test_run_id,
    evidence?.release?.release_id,
    evidence?.release?.commit_sha,
  ];
  if (required.some((value) => typeof value !== "string" || !value)) {
    throw new Error("completed improvement evidence is missing an exact persisted identity");
  }
  const seed = {
    agent: { agent_id: evidence.agent_id },
    item: { improvement_id: evidence.improvement_id },
    stamp: evidence.improvement_id,
  };
  const flow = {
    terminalRun: { test_run_id: evidence.test_run.test_run_id },
    confirmed: { candidate_commit_sha: evidence.candidate_commit_sha },
  };
  const viewports = await verifyResponsiveStates(browser, config, seed, flow, evidence.release);
  return {
    status: "passed",
    mode: "read-only-completed-loop",
    agent_id: evidence.agent_id,
    improvement_id: evidence.improvement_id,
    release_id: evidence.release.release_id,
    commit_sha: evidence.release.commit_sha,
    viewports,
  };
}

function assertPreparedSeed(seed, governanceAgentId, scenario) {
  const baseline = seed?.sourceRuns?.[0];
  const expectedInputSha = createHash("sha256").update(scenario.input, "utf8").digest("hex");
  const approvedPaths = scenario.acceptance?.allowed_target_paths || [];
  if (seed?.agent?.agent_id !== governanceAgentId
      || seed?.item?.agent_id !== governanceAgentId
      || seed?.binding?.governance_agent_id !== governanceAgentId
      || seed?.scenario?.scenario_id !== scenario.scenario_id
      || seed.scenario.input !== scenario.input
      || baseline?.agent_id !== governanceAgentId
      || baseline.runtime_agent_id !== seed.binding.runtime_agent_id
      || baseline.agent_version_id !== seed.binding.agent_version_id
      || baseline.inputSha256 !== expectedInputSha
      || baseline.status !== "succeeded" || baseline.trace_status !== "complete"
      || !Array.isArray(seed.authorizedTargetPaths)
      || seed.authorizedTargetPaths.length !== approvedPaths.length
      || new Set(seed.authorizedTargetPaths).size !== approvedPaths.length
      || seed.authorizedTargetPaths.some((path) => !approvedPaths.includes(path))
      || JSON.stringify(seed.requiredTestLiterals) !== JSON.stringify(scenario.acceptance.required_test_literals)
      || JSON.stringify(seed.requiredTestCodeFragments) !== JSON.stringify(scenario.acceptance.required_code_fragments || [])
      || seed?.feedback?.feedback_case_id !== seed?.feedbackCaseId) {
    throw acceptanceError("PREPARED_FEEDBACK_SEED_IDENTITY_INVALID");
  }
}

async function acceptancePage(browser, config, governanceAgentId, scenario, preparedSeed) {
  if (preparedSeed) assertPreparedSeed(preparedSeed, governanceAgentId, scenario);
  const seed = preparedSeed || await seedBaseImprovement(config, governanceAgentId, scenario);
  const allowedTargetPaths = preparedSeed ? seed.authorizedTargetPaths : null;
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const diagnostics = attachDiagnostics(page, config.apiBase, config.uiBase);
  try {
    if (preparedSeed) await configureUiApiConnection(page, config);
  } catch (error) {
    await page.close();
    throw error;
  }
  return { seed, page, diagnostics, allowedTargetPaths };
}

export async function runRealContainerAcceptance(
  browser, config, governanceAgentId, scenario, { preparedSeed = null } = {},
) {
  const { seed, page, diagnostics, allowedTargetPaths } = await acceptancePage(
    browser, config, governanceAgentId, scenario, preparedSeed,
  );
  let flow;
  let publication;
  let outcomeComparison;
  let negativeBoundary;
  let functionalDiagnostics;
  try {
    await openImprovement(page, config, seed);
    flow = await exerciseFourStageActions(page, config, seed, 1, allowedTargetPaths);
    negativeBoundary = await assertHostileTestRunRejected(
      config,
      seed.agent.agent_id,
      flow.confirmed.candidate_commit_sha,
    );
    const suite = await apiJson(
      config,
      "/api/agent-registry/" + encodeURIComponent(seed.agent.agent_id)
        + "/test-suite?commit_sha=" + encodeURIComponent(flow.confirmed.candidate_commit_sha),
    );
    let terminalRun = await waitForTerminalTestRun(config, flow.initialRun.test_run_id);
    if (preparedSeed && terminalRun.status === "error"
        && isRuntimeTemplateRestartRequired(terminalRun.error)) {
      assertExactTestCandidate(flow, suite, terminalRun);
      await restartCandidateRuntime({
        signal: terminalRun.error,
        stage: "candidate_test",
        maintenance: config.runtimeMaintenance,
      });
      flow.initialRun = await startPlatformTests(
        page, config, seed, flow.execution, flow.confirmed, flow.actions,
      );
      terminalRun = await waitForTerminalTestRun(config, flow.initialRun.test_run_id);
    }
    assertPassedTestEvidence(flow, suite, terminalRun);
    flow.suite = suite;
    flow.terminalRun = terminalRun;
    await waitForPassedGate(page, terminalRun);
    publication = await publishPassedCandidate(page, config, flow, allowedTargetPaths);
    outcomeComparison = await verifyPublishedCandidateOutcome(config, seed, publication.release);
    flow.actions.push({
      action: "platform-pytest",
      endpoint: "/api/agent-test-runs/" + terminalRun.test_run_id,
      status: terminalRun.status,
    });
    flow.actions.push({
      action: "review-and-approve-candidate",
      endpoint: publication.approvalEndpoint,
      status: publication.approvalStatus,
      reviewed_file_count: publication.reviewedFileCount,
      diff_digest: publication.diffDigest,
    });
    flow.actions.push({
      action: "publish-passed-candidate",
      endpoint: publication.endpoint,
      status: publication.status,
    });

    assertNoForbiddenUiRequests(diagnostics.requests);
    const unexpected = unexpectedDiagnostics(diagnostics);
    if (Object.values(unexpected).some((items) => items.length)) {
      throw acceptanceError("FUNCTIONAL_BROWSER_DIAGNOSTICS_FAILED");
    }
    functionalDiagnostics = {
      consoleErrors: diagnostics.consoleErrors,
      pageErrors: diagnostics.pageErrors,
      requestFailures: diagnostics.requestFailures,
      httpErrors: diagnostics.httpErrors,
    };
  } finally {
    await page.close();
  }

  const viewports = await verifyResponsiveStates(browser, config, seed, flow, publication.release, Boolean(preparedSeed));
  return {
    status: "passed",
    mode: "real-container",
    ui_base: config.uiBase,
    api_base: config.apiBase,
    improvement_id: seed.item.improvement_id,
    agent_id: seed.agent.agent_id,
    source_runs: seed.sourceRuns.map((run) => ({
      run_id: run.run_id,
      session_id: run.session_id,
      agent_version_id: run.agent_version_id,
      runtime_agent_id: run.runtime_agent_id,
      status: run.status,
      trace_id: run.trace_id,
      trace_status: run.trace_status,
      input_sha256: run.inputSha256,
      reply_text_sha256: run.replyTextSha256,
      reply_text_length: run.replyTextLength,
    })),
    change_set_id: flow.execution.change_set_id,
    candidate_commit_sha: flow.confirmed.candidate_commit_sha,
    generated_test_files: flow.confirmed.generated_test_files,
    suite_digest: flow.suite.suite_digest,
    approval_evidence: publication.approved.approval_evidence,
    reviewed_diff: {
      digest: publication.diffDigest,
      file_count: publication.reviewedFileCount,
    },
    test_run: {
      test_run_id: flow.terminalRun.test_run_id,
      status: flow.terminalRun.status,
      fixed_command_verified: true,
      item_count: flow.terminalRun.items?.length || 0,
    },
    release: {
      release_id: publication.release.release_id,
      commit_sha: publication.release.commit_sha,
      force_published: publication.release.force_published,
    },
    outcome_comparison: outcomeComparison,
    actions: flow.actions,
    negative_boundary: negativeBoundary,
    functional_diagnostics: functionalDiagnostics,
    viewports,
  };
}

async function verifyPublishedCandidateOutcome(config, seed, release) {
  const baseline = seed.sourceRuns[0];
  const candidateBinding = await getCurrentRuntimeAgent(config, seed.agent.agent_id);
  if (candidateBinding.agent_version_id !== release.commit_sha) {
    throw new Error("published candidate is not the exact current Runtime Agent version");
  }
  const candidate = await runReviewedScenario(config, candidateBinding, seed.scenario.input);
  if (candidate.inputSha256 !== baseline.inputSha256) {
    throw new Error("baseline and candidate Runtime runs did not use the exact same reviewed input");
  }
  if (candidate.agent_version_id === baseline.agent_version_id
      || candidate.replyTextSha256 === baseline.replyTextSha256) {
    throw new Error("published candidate did not produce a distinct same-input Runtime outcome");
  }
  const compactBaseline = baseline.replyText.replace(/\s+/g, "");
  const compactCandidate = candidate.replyText.replace(/\s+/g, "");
  const requiredLiterals = seed.requiredTestLiterals.map((literal) => literal.replace(/\s+/g, ""));
  if (!requiredLiterals.length) {
    throw new Error("reviewed improvement scenario requires at least one effect literal");
  }
  if (requiredLiterals.every((literal) => compactBaseline.includes(literal))) {
    throw new Error("reviewed improvement scenario does not reproduce the claimed baseline failure");
  }
  if (requiredLiterals.some((literal) => !compactCandidate.includes(literal))) {
    throw new Error("published candidate response does not satisfy the reviewed required literals");
  }
  return {
    same_input: true,
    baseline: runtimeOutcomeEvidence(baseline),
    candidate: runtimeOutcomeEvidence(candidate),
    required_literals_checked: requiredLiterals.length,
  };
}

function runtimeOutcomeEvidence(run) {
  return {
    run_id: run.run_id,
    session_id: run.session_id,
    agent_version_id: run.agent_version_id,
    trace_id: run.trace_id,
    trace_status: run.trace_status,
    input_sha256: run.inputSha256,
    reply_text_sha256: run.replyTextSha256,
    reply_text_length: run.replyTextLength,
  };
}
