#!/usr/bin/env node
// 仅驱动现有真实公共 API/UI，不替代产品执行或审批实现。
import { createRequire } from "node:module";
import process from "node:process";
import { pathToFileURL } from "node:url";

import { runDualGovernanceAcceptance, safeDualFailure } from "./improvement_ui_e2e/dual_governance_flow.mjs";
import { CandidateRuntimeRestartError } from "./improvement_ui_e2e/candidate_runtime_restart.mjs";

const SCOPE = "self_use_dual_agent_governance";
const BOOTSTRAP_STAGES = new Set([
  "bootstrap_registry_preflight", "bootstrap_ui_settings", "bootstrap_workspace_import",
  "bootstrap_candidate_test_review_approve_publish", "bootstrap_context_cleanup", "bootstrap_postconditions",
  "candidate_test", "candidate_review_approve", "candidate_publish", "candidate_binding",
]);
const BOOTSTRAP_CODES = new Set(["WORKSPACE_BOOTSTRAP_FAILED", "WORKSPACE_BOOTSTRAP_CONTEXT_CLEANUP_FAILED"]);
const ID_KEYS = ["agent_id", "import_record_id", "change_set_id", "test_run_id", "release_id"];
const DIGEST_KEYS = ["suite_digest", "diff_digest", "package_sha256", "tree_sha256"];
const DUAL_STAGES = new Set(["initial_workspace_import", "governance_docs", "governance_soc", "tool_confirmation"]);
const CASE_ID_KEYS = ["agent_id", "baseline_run_id", "feedback_case_id", "improvement_id", "change_set_id", "test_run_id", "release_id"];
const MAINTENANCE_CODES = new Set([
  "RUNTIME_RESTART_SIGNAL_REQUIRED", "RUNTIME_RESTART_STAGE_INVALID", "RUNTIME_RESTART_ATTEMPT_LIMIT",
  "RUNTIME_RESTART_CONTEXT_REQUIRED", "RUNTIME_RESTART_MAKE_FAILED",
]);

function projectIds(source, keys) {
  const evidence = {};
  if (!source || typeof source !== "object" || Array.isArray(source)) return evidence;
  for (const key of keys) {
    if (typeof source[key] === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(source[key])) evidence[key] = source[key];
  }
  if (typeof source.candidate_commit_sha === "string" && /^[a-f0-9]{40}$/.test(source.candidate_commit_sha)) {
    evidence.candidate_commit_sha = source.candidate_commit_sha;
  }
  return evidence;
}

export function selfUseFailureReport(error) {
  const report = { scope: SCOPE, ...safeDualFailure(error) };
  if (BOOTSTRAP_CODES.has(error?.code)) report.code = error.code;
  if (error instanceof CandidateRuntimeRestartError && MAINTENANCE_CODES.has(error.code)) report.code = error.code;
  if (MAINTENANCE_CODES.has(error?.maintenanceCode)) report.reason_code = error.maintenanceCode;
  if (BOOTSTRAP_STAGES.has(error?.acceptanceStage)) report.stage = error.acceptanceStage;
  if (error?.kind === "timeout" || error?.kind === "error") report.kind = error.kind;
  const source = error?.bootstrapEvidence;
  if (source && typeof source === "object" && !Array.isArray(source)) {
    const evidence = projectIds(source, ID_KEYS);
    for (const key of DIGEST_KEYS) {
      if (typeof source[key] === "string" && /^[a-f0-9]{64}$/.test(source[key])) evidence[key] = source[key];
    }
    if (source.retained === true) evidence.retained = true;
    if (Object.keys(evidence).length) report.bootstrap_evidence = evidence;
  }
  const dual = error?.acceptanceEvidence;
  if (dual && typeof dual === "object" && !Array.isArray(dual)) {
    const evidence = {};
    if (DUAL_STAGES.has(dual.stage)) evidence.stage = dual.stage;
    const initial = projectIds(dual.initial_workspace_release, ID_KEYS);
    const current = projectIds(dual.current_case, CASE_ID_KEYS);
    if (Object.keys(initial).length) evidence.initial_workspace_release = initial;
    if (Object.keys(current).length) evidence.current_case = current;
    if (Array.isArray(dual.completed_cases)) evidence.completed_cases = dual.completed_cases.slice(0, 2).map((item) => projectIds(item, CASE_ID_KEYS));
    if (Object.keys(evidence).length) report.acceptance_evidence = evidence;
  }
  return report;
}

async function main() {
  let report;
  let config;
  try {
    if (process.env.REQUIRE_LIVE_RUNTIME !== "1") throw new Error("EXPLICIT_LIVE_OPT_IN_REQUIRED");
    process.stdin.setEncoding("utf8");
    let input = "";
    for await (const chunk of process.stdin) {
      input += chunk;
      if (Buffer.byteLength(input) > 1_000_000) throw new Error("ACCEPTANCE_INPUT_TOO_LARGE");
    }
    const parsed = JSON.parse(input);
    config = parsed.config;
    const plan = parsed.plan;
    config.runtimeMaintenance = [];
    const require = createRequire(new URL("../frontend/package.json", import.meta.url));
    const browserTypes = require("playwright");
    const result = await runDualGovernanceAcceptance(browserTypes, config, plan);
    report = { scope: SCOPE, ...result };
  } catch (error) {
    report = selfUseFailureReport(error);
  }
  report.runtime_maintenance = config?.runtimeMaintenance || [];
  process.stdout.write(`${JSON.stringify(report)}\n`);
  process.exitCode = report.status === "passed" ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
