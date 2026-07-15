import {
  apiJson,
  seedBaseImprovement,
  assertHostileAdoptionRejected,
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
const MAX_REGRESSION_ASSESSMENT_ATTEMPTS = 3;
const REJECTED_CASE_BLOCKER = "（1 条用例经人工复核拒绝）";

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
  await switcher.selectOption(seed.agent.agent_id);
  await page.getByTestId("nav-improvement").click();
  await page.getByTestId("improvement-workbench").waitFor({ timeout: 30000 });
  const target = page.locator(`[data-testid="improvement-list-item"][data-item-id="${seed.item.improvement_id}"]`).first();
  await target.waitFor({ timeout: 30000 });
  await target.click();
  await page.locator(`[data-testid="improvement-detail"][data-item-id="${seed.item.improvement_id}"]`).waitFor({ timeout: 30000 });
}

async function responseBody(response) {
  try { return await response.text(); } catch { return ""; }
}

async function clickPrimaryBusinessAction(page, config, dataAction, endpointSuffix) {
  const button = page.locator(`[data-testid="primary-action"][data-action="${dataAction}"]`).first();
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
    throw new Error(`${dataAction} failed with visible detail '${detail}': ${response.status()} ${await responseBody(response)}`);
  }
  await page.getByTestId("decision-operation-status").waitFor({ state: "detached", timeout: 30000 }).catch(() => {});
  return { action: dataAction, endpoint: new URL(response.url()).pathname, status: response.status() };
}

async function regenerateOptimizationPlan(page, config, improvementId) {
  const button = page.getByTestId("decision-regenerate-optimization-plan");
  await button.waitFor({ timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname === `/api/improvements/${improvementId}/optimization-plan/generate`;
  }, { timeout: config.actionTimeoutMs });
  await button.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`governor optimization plan regeneration failed: ${response.status()} ${await responseBody(response)}`);
  }
  await page.getByTestId("decision-operation-status").waitFor({ state: "detached", timeout: 30000 }).catch(() => {});
  return { action: "regenerate-optimization-plan", endpoint: new URL(response.url()).pathname, status: response.status() };
}

async function generateRegressionAssessment(page, config, improvementId, actions, minimumCaseCount) {
  let assessment;
  for (let attempt = 1; attempt <= MAX_REGRESSION_ASSESSMENT_ATTEMPTS; attempt += 1) {
    const action = await clickPrimaryBusinessAction(
      page,
      config,
      "generate-regression",
      `/api/improvements/${improvementId}/regression-assessment/generate`,
    );
    actions.push({ ...action, attempt });
    assessment = await apiJson(config, `/api/improvements/${improvementId}/regression-assessment`);
    if (assessment.regression_assessment_id && assessment.cases?.length >= minimumCaseCount) return assessment;
  }
  throw new Error(
    `regression assessment did not persist at least ${minimumCaseCount} typed cases after ${MAX_REGRESSION_ASSESSMENT_ATTEMPTS} attempts: ${JSON.stringify(assessment)}`,
  );
}

async function exerciseFourStageActions(page, config, seed, minimumRegressionCaseCount = 1) {
  const improvementId = seed.item.improvement_id;
  const actions = [];
  await page.getByTestId("normalized-feedback").waitFor({ timeout: 30000 });
  actions.push(await clickPrimaryBusinessAction(page, config, "generate-attribution", `/api/improvements/${improvementId}/attribution/generate`));
  const attribution = await apiJson(config, `/api/improvements/${improvementId}/attribution`);
  if (!attribution.attribution_id) throw new Error("attribution side effect was not persisted");

  actions.push(await clickPrimaryBusinessAction(page, config, "generate-optimization-plan", `/api/improvements/${improvementId}/optimization-plan/generate`));
  const plan = await apiJson(config, `/api/improvements/${improvementId}/optimization-plan`);
  if (!plan.optimization_plan_id) throw new Error("optimization plan side effect was not persisted");

  let execution;
  for (let attempt = 1; attempt <= MAX_GOVERNOR_PLAN_ATTEMPTS; attempt += 1) {
    const executionAction = await clickPrimaryBusinessAction(
      page,
      config,
      "apply-execution",
      `/api/improvements/${improvementId}/execution/apply`,
    );
    actions.push({ ...executionAction, attempt });
    execution = await apiJson(config, `/api/improvements/${improvementId}/execution`);
    const bound = execution.change_set_id
      && execution.applied_agent_version_id
      && Object.keys(execution.applied_diff || {}).length;
    if (bound) break;
    if (attempt === MAX_GOVERNOR_PLAN_ATTEMPTS) {
      throw new Error(`governor did not produce a writable execution plan after ${attempt} attempts: ${JSON.stringify(execution)}`);
    }
    const regeneration = await regenerateOptimizationPlan(page, config, improvementId);
    actions.push({ ...regeneration, attempt: attempt + 1 });
  }

  const assessment = await generateRegressionAssessment(
    page,
    config,
    improvementId,
    actions,
    minimumRegressionCaseCount,
  );

  const adopt = page.getByTestId("adopt-regression");
  await adopt.waitFor({ timeout: 30000 });
  const adoptionResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname === `/api/improvements/${improvementId}/test-dataset/adopt`;
  }, { timeout: config.actionTimeoutMs });
  await adopt.click();
  const adoptionResponse = await adoptionResponsePromise;
  if (!adoptionResponse.ok()) throw new Error(`typed TestDataset adoption failed: ${adoptionResponse.status()} ${await responseBody(adoptionResponse)}`);
  let dataset = await adoptionResponse.json();
  await page.getByTestId("test-dataset-id").filter({ hasText: dataset.dataset_id }).waitFor({ timeout: 30000 });
  await page.getByTestId("test-dataset-lifecycle-target").selectOption("active");
  await page.getByTestId("test-dataset-lifecycle-reason").fill("真实容器端到端验收");
  const lifecycleResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname === `/api/test-datasets/${dataset.dataset_id}/lifecycle`;
  }, { timeout: config.actionTimeoutMs });
  await page.getByTestId("test-dataset-lifecycle-submit").click();
  const lifecycleResponse = await lifecycleResponsePromise;
  if (!lifecycleResponse.ok()) throw new Error(`typed TestDataset lifecycle transition failed: ${lifecycleResponse.status()} ${await responseBody(lifecycleResponse)}`);
  const lifecycleRequest = lifecycleResponse.request().postDataJSON();
  if (lifecycleRequest.target_state !== "active"
      || lifecycleRequest.expected_revision !== dataset.revision
      || lifecycleRequest.operator !== "ui"
      || lifecycleRequest.reason !== "真实容器端到端验收") {
    throw new Error(`typed TestDataset lifecycle payload drifted: ${JSON.stringify(lifecycleRequest)}`);
  }
  dataset = await lifecycleResponse.json();
  if (dataset.lifecycle_state !== "active" || dataset.revision < 2) {
    throw new Error(`typed TestDataset lifecycle did not persist: ${JSON.stringify(dataset)}`);
  }
  const fetched = await apiJson(config, `/api/test-datasets/${dataset.dataset_id}?agent_id=${encodeURIComponent(seed.agent.agent_id)}`);
  if (fetched.dataset_id !== dataset.dataset_id || fetched.owner_id !== seed.agent.agent_id) {
    throw new Error(`typed TestDataset owner mismatch: ${JSON.stringify(fetched)}`);
  }
  actions.push({ action: "adopt-test-dataset", endpoint: `/api/improvements/${improvementId}/test-dataset/adopt`, status: adoptionResponse.status() });
  actions.push({ action: "transition-test-dataset", endpoint: `/api/test-datasets/${dataset.dataset_id}/lifecycle`, status: lifecycleResponse.status() });
  return { actions, assessment, dataset, execution };
}

async function runDatasetBoundRegression(page, config, flow) {
  const changeSetId = flow.execution.change_set_id;
  await page.getByTestId("release-workbench").waitFor({ timeout: 30000 });
  const datasetSelect = page.getByTestId("release-regression-dataset");
  await datasetSelect.waitFor({ timeout: 30000 });
  await datasetSelect.selectOption(flow.dataset.dataset_id);
  const runButton = page.getByTestId("release-action-run-regression");
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="release-action-run-regression"]');
    return button instanceof HTMLButtonElement && !button.disabled;
  }, null, { timeout: 30000 });
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname === `/api/agent-change-sets/${changeSetId}/regression-runs`;
  }, { timeout: config.actionTimeoutMs });
  await runButton.click();
  const response = await responsePromise;
  const requestPayload = response.request().postDataJSON();
  if (Object.keys(requestPayload).sort().join(",") !== "dataset_id" || requestPayload.dataset_id !== flow.dataset.dataset_id) {
    throw new Error(`regression UI sent stale or unbound payload: ${JSON.stringify(requestPayload)}`);
  }
  if (!response.ok()) {
    await page.getByTestId("release-action-error").waitFor({ timeout: 15000 }).catch(() => {});
    const detail = await page.getByTestId("release-action-error").innerText().catch(() => "failure detail missing");
    throw new Error(`dataset regression failed with visible detail '${detail}': ${response.status()} ${await responseBody(response)}`);
  }
  const run = await response.json();
  const snapshotCases = run.dataset_snapshot?.cases || [];
  const itemSnapshots = (run.items || []).filter((item) => item.dataset_case_id && item.dataset_case_snapshot?.case_id);
  const gateCaseLists = [
    run.gate_result?.blocked_dataset_case_ids,
    run.gate_result?.review_dataset_case_ids,
    run.gate_result?.note_dataset_case_ids,
  ];
  if (run.dataset_id !== flow.dataset.dataset_id
      || run.change_set_id !== changeSetId
      || run.agent_id !== flow.dataset.agent_id) {
    throw new Error(`regression run lost Agent/dataset/change-set binding: ${JSON.stringify(run)}`);
  }
  if (run.dataset_snapshot?.dataset_id !== flow.dataset.dataset_id
      || run.dataset_snapshot?.lifecycle_state !== "evaluating"
      || snapshotCases.length !== flow.dataset.cases.length
      || itemSnapshots.length !== flow.dataset.cases.length
      || gateCaseLists.some((value) => !Array.isArray(value))) {
    throw new Error(`regression run did not persist the complete typed dataset snapshot: ${JSON.stringify(run)}`);
  }
  const releasedDataset = await apiJson(
    config,
    `/api/test-datasets/${flow.dataset.dataset_id}?agent_id=${encodeURIComponent(flow.dataset.agent_id)}`,
  );
  if (releasedDataset.lifecycle_state !== "active"
      || releasedDataset.revision !== run.dataset_snapshot.revision + 1) {
    throw new Error(`regression run did not release its TestDataset lifecycle: ${JSON.stringify(releasedDataset)}`);
  }
  await page.getByTestId("release-action-message").waitFor({ timeout: 30000 });
  await page.getByTestId("regression-run-status").filter({ hasText: run.eval_run_id }).waitFor({ timeout: 30000 });
  return {
    change_set_id: changeSetId,
    agent_id: run.agent_id,
    dataset_id: run.dataset_id,
    eval_run_id: run.eval_run_id,
    result_status: run.result_status,
    dataset_revision: releasedDataset.revision,
    response_status: response.status(),
    dataset_case_ids: itemSnapshots.map((item) => item.dataset_case_id),
  };
}

async function reviewRegressionAndPublish(page, config, flow, regression) {
  const changeSetId = flow.execution.change_set_id;
  const reviewPanel = page.getByTestId("release-regression-review");
  await reviewPanel.waitFor({ timeout: 30000 });
  const reviewItems = reviewPanel.getByTestId("release-regression-review-item");
  const reviewCount = await reviewItems.count();
  if (!reviewCount || reviewCount !== regression.dataset_case_ids.length) {
    throw new Error(`review UI did not bind every pending dataset case: ${reviewCount}`);
  }
  await page.getByTestId("release-review-operator").fill("real-container-reviewer");
  await page.getByTestId("release-review-reason").fill("真实容器逐条核验候选输出与回归期望");
  for (const item of await reviewItems.all()) {
    await item.locator('[data-decision="approve"]').click();
  }
  const reviewEndpoint = `/api/agent-change-sets/${changeSetId}/regression-runs/${regression.eval_run_id}/review`;
  const reviewResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === reviewEndpoint
  ), { timeout: config.actionTimeoutMs });
  await page.getByTestId("release-review-submit").click();
  const reviewResponse = await reviewResponsePromise;
  if (!reviewResponse.ok()) {
    throw new Error(`audited regression review failed: ${reviewResponse.status()} ${await responseBody(reviewResponse)}`);
  }
  const reviewRequest = reviewResponse.request().postDataJSON();
  if (Object.keys(reviewRequest).sort().join(",") !== "decisions,operator,reason,review_id,scope"
      || reviewRequest.scope !== "current_eval_run"
      || reviewRequest.decisions?.length !== reviewCount
      || reviewRequest.decisions.some((item) => item.decision !== "approve")) {
    throw new Error(`audited regression review payload drifted: ${JSON.stringify(reviewRequest)}`);
  }
  const reviewed = await reviewResponse.json();
  if (reviewed.result_status !== "passed_with_notes"
      || reviewed.gate_result?.status !== "passed_with_notes"
      || reviewed.gate_result?.review_decision?.review_id !== reviewRequest.review_id
      || reviewed.items?.some((item) => item.status !== "needs_human_review")) {
    throw new Error(`audited regression review lost original evidence or gate projection: ${JSON.stringify(reviewed)}`);
  }
  const reviewedChangeSet = await apiJson(config, `/api/agent-change-sets/${changeSetId}`);
  if (reviewedChangeSet.status !== "regression_passed" || reviewedChangeSet.publication_blocker) {
    throw new Error(`audited regression review did not atomically open the normal publish gate: ${JSON.stringify(reviewedChangeSet)}`);
  }

  const publishButton = page.getByTestId("release-action-publish");
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="release-action-publish"]');
    return button instanceof HTMLButtonElement && !button.disabled;
  }, null, { timeout: 30000 });
  const publishEndpoint = `/api/agent-change-sets/${changeSetId}/publish`;
  const publishResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === publishEndpoint
  ), { timeout: config.actionTimeoutMs });
  await publishButton.click();
  const publishResponse = await publishResponsePromise;
  if (!publishResponse.ok()) {
    throw new Error(`reviewed candidate normal publish failed: ${publishResponse.status()} ${await responseBody(publishResponse)}`);
  }
  const publishRequest = publishResponse.request().postDataJSON();
  if (publishRequest.force !== false) {
    throw new Error(`reviewed candidate did not use normal publish: ${JSON.stringify(publishRequest)}`);
  }
  const release = await publishResponse.json();
  if (release.change_set_id !== changeSetId || release.commit_sha !== flow.execution.applied_agent_version_id) {
    throw new Error(`published release lost reviewed candidate binding: ${JSON.stringify(release)}`);
  }
  await page.getByTestId("release-item").filter({ hasText: release.tag_name || release.release_id }).waitFor({ timeout: 30000 });
  return {
    review: { endpoint: reviewEndpoint, status: reviewResponse.status(), review_id: reviewRequest.review_id },
    publish: { endpoint: publishEndpoint, status: publishResponse.status(), release_id: release.release_id },
  };
}

function sameMembers(left, right) {
  return JSON.stringify([...(left || [])].sort()) === JSON.stringify([...(right || [])].sort());
}

function validateRejectedReviewRequest(reviewRequest, reviewCount, rejectedCaseId, evalRunId) {
  const decisions = reviewRequest.decisions || [];
  const rejected = decisions.filter((item) => item.decision === "reject");
  const approved = decisions.filter((item) => item.decision === "approve");
  if (Object.keys(reviewRequest).sort().join(",") !== "decisions,operator,reason,review_id,scope"
      || reviewRequest.review_id !== `review-${evalRunId}`
      || reviewRequest.operator !== "real-container-rejection-reviewer"
      || reviewRequest.reason !== "一条候选输出缺少关键来源核验，其余用例满足回归期望"
      || reviewRequest.scope !== "current_eval_run"
      || decisions.length !== reviewCount
      || decisions.some((item) => Object.keys(item).sort().join(",") !== "dataset_case_id,decision")
      || rejected.length !== 1
      || rejected[0].dataset_case_id !== rejectedCaseId
      || approved.length !== reviewCount - 1) {
    throw new Error(`mixed audited regression review payload drifted: ${JSON.stringify(reviewRequest)}`);
  }
  return approved.map((item) => item.dataset_case_id);
}

function validateRejectedReviewProjection(reviewed, reviewRequest, rejectedCaseId, approvedCaseIds) {
  const gate = reviewed.gate_result || {};
  if (reviewed.result_status !== "failed"
      || gate.status !== "blocked"
      || !sameMembers(gate.blocked_dataset_case_ids, [rejectedCaseId])
      || !sameMembers(gate.note_dataset_case_ids, approvedCaseIds)
      || !Array.isArray(gate.review_dataset_case_ids)
      || gate.review_dataset_case_ids.length
      || gate.review_decision?.review_id !== reviewRequest.review_id
      || reviewed.summary?.blocked !== 1
      || reviewed.items?.some((item) => item.status !== "needs_human_review")) {
    throw new Error(`rejected regression review lost original evidence or gate projection: ${JSON.stringify(reviewed)}`);
  }
}

async function reviewRegressionWithOneRejection(page, config, flow, regression) {
  const changeSetId = flow.execution.change_set_id;
  const reviewPanel = page.getByTestId("release-regression-review");
  await reviewPanel.waitFor({ timeout: 30000 });
  const reviewItems = reviewPanel.getByTestId("release-regression-review-item");
  const reviewCount = await reviewItems.count();
  if (reviewCount < 2 || reviewCount !== regression.dataset_case_ids.length) {
    throw new Error(`mixed review requires every pending case and at least two cases: ${reviewCount}`);
  }
  if (!(await page.getByTestId("release-action-force").isDisabled())) {
    throw new Error("pending regression review unexpectedly allowed force publication");
  }
  await page.getByTestId("release-review-operator").fill("real-container-rejection-reviewer");
  await page.getByTestId("release-review-reason").fill("一条候选输出缺少关键来源核验，其余用例满足回归期望");
  const observedCaseIds = [];
  for (let index = 0; index < reviewCount; index += 1) {
    const item = reviewItems.nth(index);
    observedCaseIds.push((await item.locator("strong").first().innerText()).trim());
    await item.locator(`[data-decision="${index === 0 ? "reject" : "approve"}"]`).click();
  }
  if (!sameMembers(observedCaseIds, regression.dataset_case_ids)) {
    throw new Error(`mixed review UI case binding drifted: ${JSON.stringify(observedCaseIds)}`);
  }
  const rejectedCaseId = observedCaseIds[0];
  const reviewEndpoint = `/api/agent-change-sets/${changeSetId}/regression-runs/${regression.eval_run_id}/review`;
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === reviewEndpoint
  ), { timeout: config.actionTimeoutMs });
  await page.getByTestId("release-review-submit").click();
  const response = await responsePromise;
  if (!response.ok()) throw new Error(`mixed regression review failed: ${response.status()} ${await responseBody(response)}`);
  const reviewRequest = response.request().postDataJSON();
  const approvedCaseIds = validateRejectedReviewRequest(reviewRequest, reviewCount, rejectedCaseId, regression.eval_run_id);
  const reviewed = await response.json();
  validateRejectedReviewProjection(reviewed, reviewRequest, rejectedCaseId, approvedCaseIds);
  await reviewPanel.waitFor({ state: "detached", timeout: 30000 });
  const blocked = await apiJson(config, `/api/agent-change-sets/${changeSetId}`);
  if (blocked.status !== "regression_failed"
      || !String(blocked.publication_blocker || "").includes(REJECTED_CASE_BLOCKER)
      || !sameMembers(blocked.latest_eval_run?.gate_result?.blocked_dataset_case_ids, [rejectedCaseId])
      || blocked.latest_eval_run?.gate_result?.review_decision?.review_id !== reviewRequest.review_id) {
    throw new Error(`rejected regression review did not persist the exact publication blocker: ${JSON.stringify(blocked)}`);
  }
  await page.waitForFunction(() => {
    const normal = document.querySelector('[data-testid="release-action-publish"]');
    const force = document.querySelector('[data-testid="release-action-force"]');
    return normal instanceof HTMLButtonElement && normal.disabled
      && force instanceof HTMLButtonElement && !force.disabled;
  }, null, { timeout: 30000 });
  return { blocked, endpoint: reviewEndpoint, rejectedCaseId, reviewId: reviewRequest.review_id, status: response.status() };
}

async function forcePublishReviewedRegression(page, config, flow, review) {
  const changeSetId = flow.execution.change_set_id;
  await page.getByTestId("release-action-force").click();
  const confirm = page.getByTestId("release-force-confirm");
  await confirm.waitFor({ timeout: 30000 });
  const confirmText = await confirm.innerText();
  if (!confirmText.includes(changeSetId) || !confirmText.includes(REJECTED_CASE_BLOCKER)) {
    throw new Error(`force confirmation lost target or blocker: ${confirmText}`);
  }
  const evidence = await screenshotAndAudit(page, config.screenshotDir, "desktop-rejected-force-confirm");
  const publishEndpoint = `/api/agent-change-sets/${changeSetId}/publish`;
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && new URL(response.url()).pathname === publishEndpoint
  ), { timeout: config.actionTimeoutMs });
  await page.getByTestId("release-force-confirm-submit").click();
  const response = await responsePromise;
  if (!response.ok()) throw new Error(`review-rejected force publish failed: ${response.status()} ${await responseBody(response)}`);
  const request = response.request().postDataJSON();
  const expectedNote = "UI 强制发布：人工确认发布门禁风险可接受。";
  if (Object.keys(request).sort().join(",") !== "force,note,operator"
      || request.operator !== "ui" || request.force !== true || request.note !== expectedNote) {
    throw new Error(`review-rejected force publish payload drifted: ${JSON.stringify(request)}`);
  }
  const release = await response.json();
  if (release.change_set_id !== changeSetId || release.commit_sha !== flow.execution.applied_agent_version_id) {
    throw new Error(`force-published release lost rejected candidate binding: ${JSON.stringify(release)}`);
  }
  await page.getByTestId("release-action-message").filter({ hasText: "已强制发布" }).waitFor({ timeout: 30000 });
  await page.getByTestId("release-item").filter({ hasText: release.tag_name || release.release_id }).waitFor({ timeout: 30000 });
  const persisted = await apiJson(config, `/api/agent-change-sets/${changeSetId}`);
  const events = await apiJson(config, `/api/agent-change-sets/${changeSetId}/events`);
  const forceEvents = events.filter((event) => event.action === "force_published");
  if (persisted.status !== "published" || persisted.force_published !== true
      || persisted.force_publish_note !== expectedNote
      || !String(persisted.force_publication_blocker || "").includes(REJECTED_CASE_BLOCKER)
      || forceEvents.length !== 1 || forceEvents[0].operator !== "ui"
      || forceEvents[0].after?.force_published !== true
      || !String(forceEvents[0].after?.force_publication_blocker || "").includes(REJECTED_CASE_BLOCKER)) {
    throw new Error(`force publication audit did not preserve the exact rejected-case blocker: ${JSON.stringify({ persisted, forceEvents })}`);
  }
  return {
    endpoint: publishEndpoint,
    evidence,
    release_id: release.release_id,
    rejected_case_id: review.rejectedCaseId,
    status: response.status(),
  };
}

async function reviewRegressionRejectAndForcePublish(page, config, flow, regression) {
  const review = await reviewRegressionWithOneRejection(page, config, flow, regression);
  const publish = await forcePublishReviewedRegression(page, config, flow, review);
  return {
    review: { endpoint: review.endpoint, review_id: review.reviewId, rejected_case_id: review.rejectedCaseId, status: review.status },
    publish,
  };
}

async function runRejectedPublicationFlow(page, config) {
  const seed = await seedBaseImprovement(config, "pagination-integrity");
  await openImprovement(page, config, seed);
  const flow = await exerciseFourStageActions(page, config, seed, 2);
  const regression = await runDatasetBoundRegression(page, config, flow);
  flow.actions.push({
    action: "run-regression-for-rejection",
    endpoint: `/api/agent-change-sets/${flow.execution.change_set_id}/regression-runs`,
    status: regression.response_status,
  });
  const publication = await reviewRegressionRejectAndForcePublish(page, config, flow, regression);
  flow.actions.push({ action: "reject-regression-review", ...publication.review });
  flow.actions.push({ action: "force-publish-reviewed-candidate", ...publication.publish });
  return { flow, publication, regression, seed };
}

async function verifyVisibleRegressionFailure(page, config, flow) {
  const changeSetId = flow.execution.change_set_id;
  const endpoint = `/api/agent-change-sets/${changeSetId}/regression-runs`;
  await page.getByTestId("release-workbench").waitFor({ timeout: 30000 });
  await page.getByTestId("release-regression-dataset").selectOption(flow.dataset.dataset_id);
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="release-action-run-regression"]');
    return button instanceof HTMLButtonElement && !button.disabled;
  }, null, { timeout: 30000 });
  const routeUrl = `${config.apiBase}${endpoint}`;
  let injected = false;
  const injectMissingDataset = async (route) => {
    const request = route.request();
    if (request.method() !== "POST" || injected) {
      await route.continue();
      return;
    }
    injected = true;
    const response = await route.fetch({
      headers: { ...request.headers(), "content-type": "application/json" },
      postData: JSON.stringify({ dataset_id: `missing-${Date.now().toString(36)}` }),
    });
    await route.fulfill({ response });
  };
  await page.route(routeUrl, injectMissingDataset);
  let response;
  try {
    [response] = await Promise.all([
      page.waitForResponse((candidate) => (
        candidate.request().method() === "POST" && new URL(candidate.url()).pathname === endpoint
      ), { timeout: config.actionTimeoutMs }),
      page.getByTestId("release-action-run-regression").click(),
    ]);
  } finally {
    await page.unroute(routeUrl, injectMissingDataset);
  }
  if (!injected) throw new Error("controlled missing-dataset regression was not injected into the POST request");
  if (response.ok()) throw new Error("controlled missing-dataset regression unexpectedly succeeded");
  const error = page.getByTestId("release-action-error");
  await error.waitFor({ timeout: 30000 });
  const visibleDetail = (await error.innerText()).trim();
  if (!visibleDetail) throw new Error("regression failure did not render a visible error detail");
  const evidence = await screenshotAndAudit(page, config.screenshotDir, "desktop-failure");
  return {
    http_error: { method: "POST", path: endpoint, status: response.status() },
    visible_detail: visibleDetail,
    evidence,
  };
}

async function assertCreateControlsFullyVisible(page) {
  const visibility = await page.evaluate(() => {
    const panel = document.querySelector(".iw-list-panel");
    if (!(panel instanceof HTMLElement)) return { panel: false, clipped: ["iw-list-panel"] };
    const panelRect = panel.getBoundingClientRect();
    const clipped = [
      "improvement-create-agent",
      "improvement-create-title",
      "improvement-create-submit",
    ].filter((testId) => {
      const element = document.querySelector(`[data-testid="${testId}"]`);
      if (!(element instanceof HTMLElement)) return true;
      const rect = element.getBoundingClientRect();
      return rect.width <= 0
        || rect.height <= 0
        || rect.left < panelRect.left
        || rect.right > panelRect.right
        || rect.top < panelRect.top
        || rect.bottom > panelRect.bottom
        || rect.top < 0
        || rect.bottom > window.innerHeight;
    });
    return { panel: true, clipped };
  });
  if (!visibility.panel || visibility.clipped.length) {
    throw new Error(`new improvement controls are clipped: ${JSON.stringify(visibility)}`);
  }
}

async function verifyResponsiveStates(browser, config, seed, datasetId, evalRunId) {
  const results = [];
  for (const viewport of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
    const diagnostics = attachDiagnostics(page, config.apiBase);
    await configurePage(page, config);
    try {
      await openImprovement(page, config, seed);
      await assertCreateControlsFullyVisible(page);
      await page.getByTestId("test-dataset-id").filter({ hasText: datasetId }).waitFor({ timeout: 30000 });
      await page.getByTestId("regression-run-status").filter({ hasText: evalRunId }).waitFor({ timeout: 30000 });
      const success = await screenshotAndAudit(page, config.screenshotDir, `${viewport.name}-success`);

      await page.getByTestId("nav-asset").click();
      await page.getByTestId("asset-registry").waitFor({ timeout: 30000 });
      await page.getByTestId("asset-source-filter").fill(`missing-${seed.stamp}`);
      await page.locator(".iw-empty").filter({ hasText: "当前范围还没有沉淀资产" }).waitFor({ timeout: 15000 });
      const empty = await screenshotAndAudit(page, config.screenshotDir, `${viewport.name}-empty`);

      assertNoForbiddenUiRequests(diagnostics.requests);
      const unexpected = unexpectedDiagnostics(diagnostics);
      if (Object.values(unexpected).some((items) => items.length)) {
        throw new Error(`browser diagnostics failed for ${viewport.name}: ${JSON.stringify({ unexpected, allHttpErrors: diagnostics.httpErrors })}`);
      }
      results.push({ viewport, success, empty, diagnostics: { httpErrors: diagnostics.httpErrors } });
    } finally {
      await page.close();
    }
  }
  return results;
}

export async function runRealContainerAcceptance(browser, config) {
  const seed = await seedBaseImprovement(config);
  const negativeBoundary = await assertHostileAdoptionRejected(config, seed.item.improvement_id);
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const diagnostics = attachDiagnostics(page, config.apiBase);
  await configurePage(page, config);
  let flow;
  let failureEvidence;
  let regression;
  let rejectedAcceptance;
  let functionalDiagnostics;
  try {
    await openImprovement(page, config, seed);
    flow = await exerciseFourStageActions(page, config, seed);
    failureEvidence = await verifyVisibleRegressionFailure(page, config, flow);
    regression = await runDatasetBoundRegression(page, config, flow);
    flow.actions.push({
      action: "run-regression",
      endpoint: `/api/agent-change-sets/${flow.execution.change_set_id}/regression-runs`,
      status: regression.response_status,
    });
    const reviewedPublication = await reviewRegressionAndPublish(page, config, flow, regression);
    flow.actions.push({ action: "review-regression", ...reviewedPublication.review });
    flow.actions.push({ action: "publish-reviewed-candidate", ...reviewedPublication.publish });

    rejectedAcceptance = await runRejectedPublicationFlow(page, config);
    assertNoForbiddenUiRequests(diagnostics.requests);
    const unexpected = unexpectedDiagnostics(diagnostics, [failureEvidence.http_error]);
    if (Object.values(unexpected).some((items) => items.length)) {
      throw new Error(`functional browser diagnostics failed: ${JSON.stringify({ unexpected, allHttpErrors: diagnostics.httpErrors })}`);
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
  const viewports = await verifyResponsiveStates(browser, config, seed, flow.dataset.dataset_id, regression.eval_run_id);
  return {
    status: "passed",
    mode: "real-container",
    ui_base: config.uiBase,
    api_base: config.apiBase,
    improvement_id: seed.item.improvement_id,
    agent_id: seed.agent.agent_id,
    dataset_id: flow.dataset.dataset_id,
    actions: flow.actions,
    regression,
    rejected_review_force_publish: {
      improvement_id: rejectedAcceptance.seed.item.improvement_id,
      dataset_id: rejectedAcceptance.flow.dataset.dataset_id,
      actions: rejectedAcceptance.flow.actions,
      regression: rejectedAcceptance.regression,
      ...rejectedAcceptance.publication,
    },
    negative_boundary: negativeBoundary,
    visible_failure: failureEvidence,
    functional_diagnostics: functionalDiagnostics,
    viewports,
  };
}
