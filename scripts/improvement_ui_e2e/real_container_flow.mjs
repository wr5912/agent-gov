import {
  apiJson,
  assertHostileTestRunRejected,
  seedBaseImprovement,
} from "./runtime_client.mjs";
import {
  assertNoForbiddenUiRequests,
  attachDiagnostics,
  screenshotAndAudit,
  unexpectedDiagnostics,
} from "./page_audit.mjs";

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 980 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
];
const MAX_GOVERNOR_PLAN_ATTEMPTS = 3;
const MAX_REGRESSION_DESIGN_ATTEMPTS = 3;
const TERMINAL_TEST_RUN_STATES = new Set(["passed", "failed", "error", "cancelled", "interrupted"]);
const FIXED_TEST_ARGUMENTS = ["-m", "pytest", "-q", "-p", "agentgov_testkit.pytest_plugin", "tests"];

async function configurePage(page, config) {
  await page.addInitScript(([apiBase, apiKey]) => {
    window.localStorage.setItem("runtime-client-config", JSON.stringify({ apiBase, apiKey }));
    window.localStorage.removeItem("playground-active-session");
  }, [config.apiBase, config.apiKey]);
}

async function openImprovement(page, config, seed) {
  await page.goto(config.uiBase, { waitUntil: "domcontentloaded" });
  const switcher = page.getByTestId("topbar-agent-switcher");
  await switcher.waitFor({ timeout: 30000 });
  await page.getByTestId("nav-improvement").click();
  await page.getByTestId("improvement-workbench").waitFor({ timeout: 30000 });
  const target = page.locator('[data-testid="improvement-list-item"][data-item-id="' + seed.item.improvement_id + '"]').first();
  await target.waitFor({ timeout: 30000 });
  await switcher.selectOption(seed.agent.agent_id);
  await target.click();
  await page.locator('[data-testid="improvement-detail"][data-item-id="' + seed.item.improvement_id + '"]').waitFor({ timeout: 30000 });
}

async function responseBody(response) {
  try {
    return await response.text();
  } catch {
    return "";
  }
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
    const detail = await page.getByTestId("decision-operation-error").innerText().catch(() => "failure detail missing");
    const body = await responseBody(response);
    const error = new Error(dataAction + " failed with visible detail '" + detail + "': " + response.status() + " " + body);
    error.httpStatus = response.status();
    error.responseBody = body;
    throw error;
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
    throw new Error("governor optimization plan regeneration failed: " + response.status() + " " + await responseBody(response));
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
) {
  let design;
  let lastError;
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
      lastError = error;
      const retryableGenerationFailure = error?.httpStatus === 503
        && String(error?.responseBody || "").includes('"error_type":"GeneratedAgentTestError"');
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
  throw new Error(
    "regression test design did not persist at least " + minimumTestCount
      + " executable pytest files after " + MAX_REGRESSION_DESIGN_ATTEMPTS + " attempts: "
      + JSON.stringify(design) + (lastError ? "; last_error=" + lastError.message : ""),
  );
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
    throw new Error("execution modified paths outside the confirmed feedback scope: " + JSON.stringify({
      allowed: [...allowed],
      unexpected,
      diff,
    }));
  }
  for (const entry of diff.modified || []) {
    const beforeSize = Number(entry.before?.size || 0);
    const afterSize = Number(entry.after?.size || 0);
    if (beforeSize >= 256 && afterSize < beforeSize * 0.5) {
      throw new Error("execution unexpectedly truncated an existing document: " + JSON.stringify(entry));
    }
  }
}

async function confirmAndMaterializeTests(page, config, seed, execution, actions) {
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
    throw new Error("materializing Workspace pytest files failed: " + response.status() + " " + await responseBody(response));
  }
  const confirmed = await response.json();
  const generatedFiles = confirmed.generated_test_files || [];
  if (!generatedFiles.some((path) => /^tests\/test_.*\.py$/.test(path))) {
    throw new Error("confirmed test design did not create tests/test_*.py: " + JSON.stringify(confirmed));
  }
  if (!confirmed.candidate_commit_sha || confirmed.test_run !== null) {
    throw new Error("confirm must pin the candidate commit without auto-running pytest: " + JSON.stringify(confirmed));
  }
  const reboundExecution = await apiJson(config, "/api/improvements/" + improvementId + "/execution");
  if (reboundExecution.change_set_id !== execution.change_set_id
      || reboundExecution.applied_agent_version_id !== confirmed.candidate_commit_sha) {
    throw new Error("execution record was not rebound to the test-bearing candidate commit: " + JSON.stringify(reboundExecution));
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
  if (!response.ok()) throw new Error("starting platform pytest failed: " + response.status() + " " + await responseBody(response));
  const run = await response.json();
  if (run.agent_id !== seed.agent.agent_id
      || run.commit_sha !== confirmed.candidate_commit_sha
      || run.change_set_id !== execution.change_set_id) {
    throw new Error("platform test run lost Agent/change-set/commit binding: " + JSON.stringify(run));
  }
  actions.push({ action: "run-platform-pytest", endpoint, status: response.status() });
  return run;
}

async function exerciseFourStageActions(page, config, seed, minimumRegressionTestCount = 1) {
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
      throw new Error("governor did not produce a writable execution plan after " + attempt + " attempts: " + JSON.stringify(execution));
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
  );
  const materialized = await confirmAndMaterializeTests(page, config, seed, execution, actions);
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
  throw new Error(
    `agent test run did not finish within ${config.testRunTimeoutMs}ms: ` + JSON.stringify(run),
  );
}

function assertPassedTestEvidence(flow, suite, run) {
  if (run.status !== "passed") {
    throw new Error("Workspace pytest did not pass: " + JSON.stringify({
      status: run.status,
      stdout: run.stdout,
      stderr: run.stderr,
      error: run.error,
      items: run.items,
    }));
  }
  if (run.agent_id !== flow.initialRun.agent_id
      || run.change_set_id !== flow.execution.change_set_id
      || run.commit_sha !== flow.confirmed.candidate_commit_sha) {
    throw new Error("terminal test run drifted from the exact candidate: " + JSON.stringify(run));
  }
  const [pythonExecutable, ...testArguments] = Array.isArray(run.command) ? run.command : [];
  const pythonName = typeof pythonExecutable === "string" ? pythonExecutable.split("/").at(-1) : "";
  if (!/^python(?:\d+(?:\.\d+)*)?$/.test(pythonName)
      || JSON.stringify(testArguments) !== JSON.stringify(FIXED_TEST_ARGUMENTS)) {
    throw new Error("platform test command drifted: " + JSON.stringify(run.command));
  }
  if (!suite.tests_directory_present
      || !suite.test_file_count
      || suite.test_files.some((path) => !/^tests\/test_.*\.py$/.test(path))
      || suite.suite_digest !== run.suite_digest) {
    throw new Error("test suite digest/files do not match the run evidence: " + JSON.stringify({ suite, run }));
  }
  if (!(run.items || []).length || run.items.some((item) => item.outcome !== "passed")) {
    throw new Error("platform test run contains a non-passing pytest item: " + JSON.stringify(run.items));
  }
  if (!(run.invocations || []).length
      || run.invocations.some((invocation) => invocation.agent_version_id !== run.commit_sha
        || !invocation.langfuse_trace_id
        || (invocation.errors || []).length)) {
    throw new Error("business Agent invocation evidence is incomplete or contains runtime errors: "
      + JSON.stringify(run.invocations));
  }
}

async function waitForPassedGate(page, run) {
  await page.waitForFunction((testRunId) => {
    const details = document.querySelector('[data-testid="release-test-run-details"]');
    const gate = document.querySelector('[data-testid="release-gate-tests"]');
    return details?.textContent?.includes(testRunId) && gate?.getAttribute("data-state") === "pass";
  }, run.test_run_id, { timeout: 60000 });
}

async function verifyVisibleTestRunFailure(page, config, flow) {
  const endpoint = "/api/agent-change-sets/" + encodeURIComponent(flow.execution.change_set_id) + "/test-runs";
  const routeUrl = config.apiBase + endpoint;
  let intercepted = false;
  let originalPayload;
  const injectStructuredFailure = async (route) => {
    const request = route.request();
    if (request.method() !== "POST" || intercepted) {
      await route.continue();
      return;
    }
    intercepted = true;
    originalPayload = request.postDataJSON() || {};
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "待发布提交在目标业务 Agent 中不可用",
        error_code: "AGENT_TEST_COMMIT_NOT_FOUND",
      }),
    });
  };
  await page.route(routeUrl, injectStructuredFailure);
  let response;
  try {
    [response] = await Promise.all([
      page.waitForResponse((candidate) => (
        candidate.request().method() === "POST" && new URL(candidate.url()).pathname === endpoint
      ), { timeout: config.actionTimeoutMs }),
      page.getByTestId("release-action-run-tests").click(),
    ]);
  } finally {
    await page.unroute(routeUrl, injectStructuredFailure);
  }
  if (!intercepted || response.ok()) {
    throw new Error("controlled unknown-commit test run did not fail");
  }
  if (Object.keys(originalPayload).length !== 0) {
    throw new Error("release UI submitted client-owned test identity: " + JSON.stringify(originalPayload));
  }
  const error = page.getByTestId("release-action-error");
  await error.waitFor({ timeout: 30000 });
  const visibleDetail = (await error.innerText()).trim();
  if (!visibleDetail) throw new Error("test run failure did not render a visible error detail");
  const evidence = await screenshotAndAudit(page, config.screenshotDir, "desktop-test-run-failure");
  return {
    http_error: { method: "POST", path: endpoint, status: response.status() },
    visible_detail: visibleDetail,
    evidence,
  };
}

async function publishPassedCandidate(page, config, flow) {
  const changeSetId = flow.execution.change_set_id;
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
    throw new Error("normal publication failed: " + response.status() + " " + await responseBody(response));
  }
  const request = response.request().postDataJSON();
  if (request.force !== false || request.operator !== "ui" || request.force_reason !== undefined) {
    throw new Error("passed candidate did not use normal publication: " + JSON.stringify(request));
  }
  const release = await response.json();
  if (release.change_set_id !== changeSetId
      || release.commit_sha !== flow.confirmed.candidate_commit_sha
      || release.force_published) {
    throw new Error("published release lost exact passed commit binding: " + JSON.stringify(release));
  }
  await page.getByTestId("release-item").filter({ hasText: release.tag_name || release.release_id }).waitFor({ timeout: 30000 });
  return { endpoint, release, status: response.status() };
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
    throw new Error("new improvement drawer controls are clipped: " + JSON.stringify(visibility));
  }
  await page.getByTestId("improvement-create-cancel").click();
  await page.getByTestId("improvement-create-drawer").waitFor({ state: "detached", timeout: 5000 });
}

async function verifyResponsiveStates(browser, config, seed, flow, release) {
  const results = [];
  for (const viewport of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
    const diagnostics = attachDiagnostics(page, config.apiBase);
    await configurePage(page, config);
    try {
      await openImprovement(page, config, seed);
      await assertCreateDrawerFullyVisible(page);
      await page.getByTestId("improvement-terminal").filter({ hasText: "已完成平台测试并发布" }).waitFor({ timeout: 30000 });
      await page.getByTestId("workspace-test-files").filter({ hasText: "已写入发布版本" }).waitFor({ timeout: 30000 });
      await page.getByTestId("confirm-regression-tests").filter({ hasText: "待发布变更已确认" }).waitFor({ timeout: 30000 });
      await page.locator('[data-testid="closed-loop-step"][data-stage-key="test_release"][data-state="done"]').waitFor({ timeout: 30000 });
      await page.getByTestId("regression-test-code-coverage").waitFor({ timeout: 30000 });
      await page.getByTestId("release-item").filter({ hasText: release.tag_name || release.release_id }).waitFor({ timeout: 30000 });
      const success = await screenshotAndAudit(page, config.screenshotDir, viewport.name + "-success");

      await page.getByTestId("nav-asset").click();
      await page.getByTestId("asset-registry").waitFor({ timeout: 30000 });
      await page.getByTestId("asset-center-tab-governance").click();
      await page.getByTestId("governance-asset-registry").waitFor({ timeout: 30000 });
      await page.getByTestId("asset-source-filter").fill("missing-" + seed.stamp);
      await page.locator(".iw-empty").filter({ hasText: "当前范围还没有沉淀资产" }).waitFor({ timeout: 15000 });
      const empty = await screenshotAndAudit(page, config.screenshotDir, viewport.name + "-empty");

      assertNoForbiddenUiRequests(diagnostics.requests);
      const unexpected = unexpectedDiagnostics(diagnostics);
      if (Object.values(unexpected).some((items) => items.length)) {
        throw new Error("browser diagnostics failed for " + viewport.name + ": " + JSON.stringify({
          unexpected,
          allHttpErrors: diagnostics.httpErrors,
        }));
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

export async function runRealContainerAcceptance(browser, config) {
  const seed = await seedBaseImprovement(config);
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const diagnostics = attachDiagnostics(page, config.apiBase);
  await configurePage(page, config);
  let flow;
  let failureEvidence;
  let publication;
  let negativeBoundary;
  let functionalDiagnostics;
  try {
    await openImprovement(page, config, seed);
    flow = await exerciseFourStageActions(page, config, seed);
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
    const terminalRun = await waitForTerminalTestRun(config, flow.initialRun.test_run_id);
    assertPassedTestEvidence(flow, suite, terminalRun);
    flow.suite = suite;
    flow.terminalRun = terminalRun;
    await waitForPassedGate(page, terminalRun);
    failureEvidence = await verifyVisibleTestRunFailure(page, config, flow);
    publication = await publishPassedCandidate(page, config, flow);
    flow.actions.push({
      action: "platform-pytest",
      endpoint: "/api/agent-test-runs/" + terminalRun.test_run_id,
      status: terminalRun.status,
    });
    flow.actions.push({
      action: "publish-passed-candidate",
      endpoint: publication.endpoint,
      status: publication.status,
    });

    assertNoForbiddenUiRequests(diagnostics.requests);
    const unexpected = unexpectedDiagnostics(diagnostics, [failureEvidence.http_error]);
    if (Object.values(unexpected).some((items) => items.length)) {
      throw new Error("functional browser diagnostics failed: " + JSON.stringify({
        unexpected,
        allHttpErrors: diagnostics.httpErrors,
      }));
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

  const viewports = await verifyResponsiveStates(browser, config, seed, flow, publication.release);
  return {
    status: "passed",
    mode: "real-container",
    ui_base: config.uiBase,
    api_base: config.apiBase,
    improvement_id: seed.item.improvement_id,
    agent_id: seed.agent.agent_id,
    change_set_id: flow.execution.change_set_id,
    candidate_commit_sha: flow.confirmed.candidate_commit_sha,
    generated_test_files: flow.confirmed.generated_test_files,
    suite_digest: flow.suite.suite_digest,
    test_run: {
      test_run_id: flow.terminalRun.test_run_id,
      status: flow.terminalRun.status,
      command: flow.terminalRun.command,
      item_count: flow.terminalRun.items?.length || 0,
    },
    release: {
      release_id: publication.release.release_id,
      commit_sha: publication.release.commit_sha,
      force_published: publication.release.force_published,
    },
    actions: flow.actions,
    negative_boundary: negativeBoundary,
    visible_failure: failureEvidence,
    functional_diagnostics: functionalDiagnostics,
    viewports,
  };
}
