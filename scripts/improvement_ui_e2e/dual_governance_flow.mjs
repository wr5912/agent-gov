// Python 公共入口负责所选 env、既有 schema 校验、stdin 传参和私有 0600 报告。
import { randomUUID } from "node:crypto";

import { bootstrapImportedWorkspace } from "./candidate_bootstrap.mjs";
import { verifyDualAgentToolConfirmation } from "./dual_agent_tool_confirmation.mjs";
import { runRealContainerAcceptance } from "./real_container_flow.mjs";
import {
  apiJson,
  confirmNormalizedFeedback,
  getCurrentRuntimeAgent,
  jsonInit,
  runReviewedScenario,
  runReviewedScenarioInSession,
} from "./runtime_client.mjs";

const SOURCE_SYSTEM = "agentgov-self-use-acceptance";
const FAILURE_CODES = new Set([
  "REVIEWED_IMPROVEMENT_SCENARIO_REQUIRED", "BASELINE_GAP_NOT_OBSERVED",
  "OBSERVATION_EVENT_NOT_MATCHED", "FEEDBACK_SIGNAL_NOT_MATCHED",
  "FEEDBACK_CASE_SOURCE_MISMATCH", "BUSINESS_AGENT_NOT_ACTIVE",
  "BASELINE_RUN_INCOMPLETE", "IMPROVEMENT_AGENT_MISMATCH",
  "ATTACHED_SOURCE_PROJECTION_MISMATCH", "FEEDBACK_ASSIGNMENT_NOT_PERSISTED",
  "PUBLISHED_FLOW_IDENTITY_MISMATCH", "PREPARED_SEED_NOT_USED",
  "BIDIRECTIONAL_RELEASE_PROVENANCE_MISMATCH", "OLD_NEW_SESSION_VERSION_MISMATCH",
  "DUAL_AGENT_PLAN_INVALID", "INITIAL_WORKSPACE_PUBLICATION_INCOMPLETE",
  "EXISTING_WORKSPACE_NOT_PUBLISHED",
  "EXISTING_WORKSPACE_COMMIT_NOT_CONFIRMED", "EXISTING_WORKSPACE_COMMIT_MISMATCH", "EXPECTED_EXISTING_WORKSPACE_MISSING",
  "SOURCE_RUN_TRACE_INCOMPLETE", "SOURCE_RUN_TRACE_TIMEOUT",
]);

export function safeDualFailure(error) {
  const status = Number.isInteger(error?.status) && error.status >= 400 && error.status <= 599
    ? error.status : null;
  return {
    status: "failed",
    code: FAILURE_CODES.has(error?.code) ? error.code : "DUAL_GOVERNANCE_FLOW_FAILED",
    http_status: status,
  };
}

function requireFact(condition, code) {
  if (!condition) {
    const error = new Error(code);
    error.code = code;
    throw error;
  }
}

function sameMembers(actual, expected) {
  return Array.isArray(actual)
    && actual.length === expected.length
    && new Set(actual).size === expected.length
    && expected.every((value) => actual.includes(value));
}

function sameEntities(actual, expected) {
  if (!actual || !expected || typeof actual !== "object" || typeof expected !== "object") return false;
  const keys = Object.keys(expected);
  return sameMembers(Object.keys(actual), keys)
    && keys.every((key) => sameMembers(actual[key], expected[key]));
}

function reviewedImprovementScenario(casePlan) {
  const scenario = casePlan?.scenario;
  requireFact(scenario?.purpose === "improvement"
    && scenario.capability === "improvement_effect"
    && typeof scenario.input === "string" && scenario.input.length > 0
    && typeof scenario.feedback_comment === "string" && scenario.feedback_comment.length > 0
    && Array.isArray(scenario.acceptance?.allowed_target_paths)
    && scenario.acceptance.allowed_target_paths.length > 0
    && Array.isArray(scenario.acceptance?.required_test_literals)
    && scenario.acceptance.required_test_literals.length > 0,
  "REVIEWED_IMPROVEMENT_SCENARIO_REQUIRED");
  return scenario;
}

function assertObservedBaselineGap(run, scenario) {
  const reply = run.replyText.replace(/\s+/g, "");
  const required = scenario.acceptance.required_test_literals.map((value) => value.replace(/\s+/g, ""));
  requireFact(required.some((value) => !reply.includes(value)), "BASELINE_GAP_NOT_OBSERVED");
}

async function createObservedEvent(config, run, eventType, metadata) {
  const eventId = randomUUID();
  const receipt = await apiJson(config, "/api/feedback-events", jsonInit("POST", {
    event_id: eventId,
    source_system: SOURCE_SYSTEM,
    event_type: eventType,
    timestamp: new Date().toISOString(),
    run_id: run.run_id,
    session_id: run.session_id,
    entities: {},
    metadata,
  }));
  requireFact(receipt?.event?.event_id === eventId
    && receipt.event.source_system === SOURCE_SYSTEM
    && receipt.event.event_type === eventType
    && receipt.event.agent_id === run.agent_id
    && receipt.matched_run_id === run.run_id
    && receipt.correlation_status === "matched", "OBSERVATION_EVENT_NOT_MATCHED");
  return receipt.event;
}

async function createFeedbackCase(config, run, scenario) {
  // 这里的两条 Event 是本验收客户端对真实 Runtime 终态与 canonical reply 的观察，
  // 不代表 SOC 业务事件；没有真实 Read 证明时 entities 必须保持空集合。
  const signal = await apiJson(config, "/api/feedback-signals", jsonInit("POST", {
    signal_id: randomUUID(),
    source_type: "explicit_feedback",
    run_id: run.run_id,
    session_id: run.session_id,
    comment: scenario.feedback_comment,
    entities: {},
  }));
  requireFact(signal?.agent_id === run.agent_id
    && signal.matched_run_id === run.run_id
    && signal.session_id === run.session_id, "FEEDBACK_SIGNAL_NOT_MATCHED");
  const terminalEvent = await createObservedEvent(config, run, "run_terminal_observed", {
    observed_status: run.status,
    agent_version_id: run.agent_version_id,
    trace_id: run.trace_id,
  });
  const replyEvent = await createObservedEvent(config, run, "canonical_reply_observed", {
    reply_id: run.reply_ids.at(-1),
    reply_sha256: run.replyTextSha256,
    reply_utf8_length: run.replyTextLength,
  });
  const eventIds = [terminalEvent.event_id, replyEvent.event_id];
  const feedbackCase = await apiJson(config, "/api/feedback-cases", jsonInit("POST", {
    source_refs: [
      { source_kind: "signal", source_id: signal.signal_id },
      ...eventIds.map((eventId) => ({ source_kind: "event", source_id: eventId })),
    ],
    title: `observed-${scenario.scenario_id}-${randomUUID()}`,
  }));
  requireFact(feedbackCase?.agent_id === run.agent_id
    && sameMembers(feedbackCase.signal_ids, [signal.signal_id])
    && sameMembers(feedbackCase.event_ids, eventIds)
    && feedbackCase.run_ids?.includes(run.run_id)
    && feedbackCase.session_ids?.includes(run.session_id), "FEEDBACK_CASE_SOURCE_MISMATCH");
  return { feedbackCase, signalId: signal.signal_id, eventIds };
}

async function prepareFeedbackSeed(config, casePlan, progress) {
  const scenario = reviewedImprovementScenario(casePlan);
  const agentId = casePlan.agentId;
  progress.agent_id = agentId;
  const agents = await apiJson(config, "/api/agent-registry");
  const agent = agents.find((item) => item.agent_id === agentId);
  requireFact(agent?.status === "active" && agent.category === "business", "BUSINESS_AGENT_NOT_ACTIVE");
  const binding = await getCurrentRuntimeAgent(config, agentId);
  const baseline = await runReviewedScenario(config, binding, scenario.input);
  progress.baseline_run_id = baseline.run_id;
  requireFact(baseline.status === "succeeded" && baseline.agent_version_id === binding.agent_version_id
    && baseline.trace_status === "complete" && baseline.reply_ids?.length > 0,
  "BASELINE_RUN_INCOMPLETE");
  assertObservedBaselineGap(baseline, scenario);
  const feedback = await createFeedbackCase(config, baseline, scenario);
  progress.feedback_case_id = feedback.feedbackCase.feedback_case_id;
  const item = await apiJson(config, "/api/improvements", jsonInit("POST", {
    agent_id: agentId,
    title: `dual-acceptance-${scenario.scenario_id}-${randomUUID()}`,
    summary: scenario.feedback_comment,
    source_feedback_refs: [],
    auto_merge: false,
  }));
  requireFact(item?.agent_id === agentId && item.improvement_id, "IMPROVEMENT_AGENT_MISMATCH");
  progress.improvement_id = item.improvement_id;
  const feedbackId = feedback.feedbackCase.feedback_case_id;
  const attached = await apiJson(config,
    `/api/improvements/${encodeURIComponent(item.improvement_id)}/attach-feedback-case`,
    jsonInit("POST", { feedback_case_id: feedbackId }));
  const observedEvents = feedback.eventIds.map((eventId, index) => ({
    event_id: eventId,
    source_system: SOURCE_SYSTEM,
    event_type: index === 0 ? "run_terminal_observed" : "canonical_reply_observed",
  }));
  requireFact(attached?.feedback_case_id === feedbackId
    && attached.improvement_id === item.improvement_id
    && attached.agent_id === agentId
    && attached.source === "feedback_inbox"
    && observedEvents.every((event) => attached.source_events?.some((actual) => (
      actual.event_id === event.event_id && actual.source_system === event.source_system
      && actual.event_type === event.event_type)))
    && attached.source_events?.length === observedEvents.length
    && sameEntities(attached.entities, feedback.feedbackCase.entities),
  "ATTACHED_SOURCE_PROJECTION_MISMATCH");
  const assigned = await apiJson(config, `/api/improvements/${encodeURIComponent(item.improvement_id)}`);
  requireFact(assigned.source_feedback_refs?.includes(feedbackId), "FEEDBACK_ASSIGNMENT_NOT_PERSISTED");
  await confirmNormalizedFeedback(config, item, scenario);
  return {
    agent, binding, scenario, item, feedback: attached, feedbacks: [attached],
    sourceRuns: [baseline], stamp: scenario.scenario_id,
    authorizedTargetPaths: [...scenario.acceptance.allowed_target_paths],
    requiredTestLiterals: [...scenario.acceptance.required_test_literals],
    requiredTestCodeFragments: [...(scenario.acceptance.required_code_fragments || [])],
    feedbackCaseId: feedbackId, signalId: feedback.signalId, eventIds: feedback.eventIds,
  };
}

async function verifyPublishedLineage(config, agentId, seed, flow) {
  const releaseId = flow.release?.release_id;
  const commitSha = flow.release?.commit_sha;
  requireFact(releaseId && commitSha && flow.release.force_published === false
    && flow.agent_id === agentId && flow.improvement_id === seed.item.improvement_id,
  "PUBLISHED_FLOW_IDENTITY_MISMATCH");
  requireFact(flow.source_runs?.[0]?.run_id === seed.sourceRuns[0].run_id
    && flow.outcome_comparison?.baseline?.run_id === seed.sourceRuns[0].run_id,
  "PREPARED_SEED_NOT_USED");
  const caseId = seed.feedbackCaseId;
  const [provenance, release, binding] = await Promise.all([
    apiJson(config, `/api/asset-registry/feedback/${encodeURIComponent(caseId)}`),
    apiJson(config, `/api/agent-releases/${encodeURIComponent(releaseId)}`),
    getCurrentRuntimeAgent(config, agentId),
  ]);
  requireFact(binding.agent_version_id === commitSha
    && release.agent_id === agentId && release.status === "published" && release.commit_sha === commitSha
    && release.source_feedback_case_ids?.includes(caseId)
    && provenance.feedback_case_id === caseId
    && provenance.agent_ids?.includes(agentId)
    && provenance.improvements?.some((item) => item.improvement_id === seed.item.improvement_id
      && item.source_feedback_refs?.includes(caseId))
    && provenance.released_versions?.some((item) => item.release_id === releaseId
      && item.agent_id === agentId && item.commit_sha === commitSha),
  "BIDIRECTIONAL_RELEASE_PROVENANCE_MISMATCH");
  const oldRun = await runReviewedScenarioInSession(
    config, seed.binding, seed.scenario.input, seed.sourceRuns[0].session_id,
  );
  const newRun = flow.outcome_comparison?.candidate;
  requireFact(oldRun.status === "succeeded"
    && oldRun.session_id === seed.sourceRuns[0].session_id
    && oldRun.agent_version_id === seed.binding.agent_version_id
    && oldRun.run_id !== seed.sourceRuns[0].run_id
    && newRun?.session_id !== oldRun.session_id
    && newRun.agent_version_id === commitSha
    && newRun.input_sha256 === oldRun.inputSha256
    && seed.binding.agent_version_id !== commitSha,
  "OLD_NEW_SESSION_VERSION_MISMATCH");
  return { oldRun, newRun, release, provenance };
}

function compactRun(run) {
  return {
    run_id: run.run_id, session_id: run.session_id,
    agent_version_id: run.agent_version_id, trace_id: run.trace_id,
    input_sha256: run.inputSha256 || run.input_sha256,
    reply_sha256: run.replyTextSha256 || run.reply_text_sha256,
    reply_utf8_length: run.replyTextLength || run.reply_text_length,
  };
}

function compactApproval(evidence) {
  return {
    candidate_commit_sha: evidence?.candidate_commit_sha,
    diff_digest: evidence?.diff_digest,
    test_run_id: evidence?.test_run_id,
    suite_digest: evidence?.suite_digest,
    review_digest: evidence?.review_digest,
    reviewed_file_count: evidence?.reviewed_file_count,
  };
}

async function verifyOneAgent(browser, config, casePlan, progress) {
  const seed = await prepareFeedbackSeed(config, casePlan, progress);
  const flow = await runRealContainerAcceptance(
    browser, config, casePlan.agentId, seed.scenario, { preparedSeed: seed },
  );
  progress.change_set_id = flow.change_set_id;
  progress.test_run_id = flow.test_run?.test_run_id;
  progress.release_id = flow.release?.release_id;
  const lineage = await verifyPublishedLineage(config, casePlan.agentId, seed, flow);
  return {
    agent_id: casePlan.agentId,
    scenario_file_sha256: casePlan.scenarioFileSha256,
    scenario_id: seed.scenario.scenario_id,
    source_kind: "reviewed_scenario_feedback_plus_actual_acceptance_observations",
    source_system: SOURCE_SYSTEM,
    feedback_case_id: seed.feedbackCaseId,
    signal_id: seed.signalId,
    observed_event_ids: seed.eventIds,
    source_events: seed.feedback.source_events.map(({ event_id, source_system, event_type }) => (
      { event_id, source_system, event_type })),
    improvement_id: seed.item.improvement_id,
    change_set_id: flow.change_set_id,
    candidate_commit_sha: flow.candidate_commit_sha,
    test_run_id: flow.test_run?.test_run_id,
    test_status: flow.test_run?.status,
    suite_digest: flow.suite_digest,
    diff_digest: flow.reviewed_diff?.digest,
    approval_evidence: compactApproval(flow.approval_evidence),
    release_id: flow.release.release_id,
    release_commit_sha: flow.release.commit_sha,
    baseline: compactRun(seed.sourceRuns[0]),
    candidate_new_session: compactRun(lineage.newRun),
    old_session_after_publish: compactRun(lineage.oldRun),
    provenance_verified: true,
    comparison_role: "review_reference_only",
    entities_source: "only_backend_derived_run_entities; no_script_injected_business_object",
    entity_type_count: Object.keys(seed.feedback.entities || {}).length,
    observation_events_are_business_events: false,
    human_quality_judgment: "not_proven_by_automation",
  };
}

export async function runDualGovernanceAcceptance(browserTypes, config, plan) {
  requireFact(Array.isArray(plan?.cases) && plan.cases.length === 2
    && plan.workspace?.agentId && plan.workspace?.packagePath && plan.workspace?.name
    && plan.workspace.agentId === "documentation-assistant-e2e"
    && plan.cases[0]?.agentId === plan.workspace.agentId
    && plan.cases[1]?.agentId === "security-operations-expert"
    && plan.workspace.agentId !== "security-operations-expert",
  "DUAL_AGENT_PLAN_INVALID");
  const browser = await browserTypes.chromium.launch({ headless: true });
  const progress = {
    stage: "initial_workspace_import",
    initial_workspace_release: null,
    completed_cases: [],
    current_case: null,
  };
  try {
    const agents = await apiJson(config, "/api/agent-registry");
    const existing = agents.find((item) => item.agent_id === plan.workspace.agentId);
    let bootstrap = null;
    if (!existing) {
      requireFact(!plan.workspace.expectedExistingCommitSha, "EXPECTED_EXISTING_WORKSPACE_MISSING");
      bootstrap = await bootstrapImportedWorkspace(browser, config, {
        agentId: plan.workspace.agentId,
        packagePath: plan.workspace.packagePath,
        name: plan.workspace.name,
      });
      requireFact(bootstrap?.agent_id === plan.workspace.agentId
        && bootstrap.binding?.agent_version_id === bootstrap.candidate_commit_sha
        && bootstrap.release_id && bootstrap.test_run_id && bootstrap.suite_digest
        && bootstrap.diff_digest, "INITIAL_WORKSPACE_PUBLICATION_INCOMPLETE");
      progress.initial_workspace_release = {
        agent_id: bootstrap.agent_id,
        change_set_id: bootstrap.change_set_id,
        candidate_commit_sha: bootstrap.candidate_commit_sha,
        test_run_id: bootstrap.test_run_id,
        release_id: bootstrap.release_id,
      };
    } else {
      requireFact(existing.status === "active" && existing.category === "business",
        "EXISTING_WORKSPACE_NOT_PUBLISHED");
      const expected = plan.workspace.expectedExistingCommitSha;
      requireFact(/^[a-f0-9]{40}$/.test(expected || ""), "EXISTING_WORKSPACE_COMMIT_NOT_CONFIRMED");
      const binding = await getCurrentRuntimeAgent(config, plan.workspace.agentId);
      requireFact(binding.agent_version_id === expected, "EXISTING_WORKSPACE_COMMIT_MISMATCH");
    }
    const cases = [];
    for (const casePlan of plan.cases) {
      progress.stage = casePlan.agentId === "documentation-assistant-e2e"
        ? "governance_docs" : "governance_soc";
      progress.current_case = { agent_id: casePlan.agentId };
      cases.push(await verifyOneAgent(browser, config, casePlan, progress.current_case));
      progress.completed_cases.push({ ...progress.current_case });
      progress.current_case = null;
    }
    progress.stage = "tool_confirmation";
    const toolConfirmation = await verifyDualAgentToolConfirmation(browser, config, {
      docs: { agent_version_id: cases[0].release_commit_sha },
      soc: { agent_version_id: cases[1].release_commit_sha },
    });
    return {
      status: "passed",
      mode: "selected-env-real-dual-agent",
      initial_workspace_release: bootstrap ? {
        agent_id: bootstrap.agent_id,
        change_set_id: bootstrap.change_set_id,
        candidate_commit_sha: bootstrap.candidate_commit_sha,
        test_run_id: bootstrap.test_run_id,
        suite_digest: bootstrap.suite_digest,
        diff_digest: bootstrap.diff_digest,
        release_id: bootstrap.release_id,
      } : { agent_id: plan.workspace.agentId, reused_published_baseline: true },
      cases,
      tool_confirmation: toolConfirmation,
      retained_for_audit: true,
    };
  } catch (error) {
    if (error && typeof error === "object") {
      error.acceptanceEvidence = {
        ...progress,
        ...(error.bootstrapEvidence && !progress.initial_workspace_release
          ? { bootstrap_evidence: error.bootstrapEvidence } : {}),
      };
    }
    throw error;
  } finally {
    await browser.close();
  }
}
