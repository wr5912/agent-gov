import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const REPOSITORY_ROOT = realpathSync(fileURLToPath(new URL("../..", import.meta.url)));

function boundAcceptancePython() {
  const python = String(process.env.AGENTGOV_ACCEPTANCE_PYTHON || "").trim();
  if (!isAbsolute(python)) {
    throw new Error("AGENTGOV_ACCEPTANCE_PYTHON must be an absolute bound executable");
  }
  let receipt;
  try {
    receipt = JSON.parse(String(process.env.AGENT_GOV_ACCEPTANCE_TOOLCHAIN_JSON || ""));
  } catch {
    throw new Error("acceptance toolchain receipt is required");
  }
  const pythonIdentity = Array.isArray(receipt?.tools)
    ? receipt.tools.find((item) => item?.name === "python")
    : undefined;
  if (receipt?.stage !== "materialized" || pythonIdentity?.path !== python) {
    throw new Error("AGENTGOV_ACCEPTANCE_PYTHON does not match the materialized toolchain receipt");
  }
  return python;
}

export function loadReviewedScenarios(fileName, expectedAgentId) {
  const scenarioFile = String(fileName || "").trim();
  const agentId = String(expectedAgentId || "").trim();
  if (!scenarioFile) throw new Error("REAL_SCENARIO_FILE is required");
  if (!agentId) throw new Error("REAL_ACCEPTANCE_AGENT_ID is required");
  const python = boundAcceptancePython();
  const result = spawnSync(
    python,
    [
      join(REPOSITORY_ROOT, "scripts/validate_live_acceptance_scenarios.py"),
      "--scenario-file",
      scenarioFile,
      "--expected-agent-id",
      agentId,
    ],
    { cwd: REPOSITORY_ROOT, encoding: "utf8", maxBuffer: 4 * 1024 * 1024 },
  );
  if (result.error) throw new Error(`live acceptance scenario validator failed to start: ${result.error.message}`);
  if (result.status !== 0) {
    throw new Error((result.stderr || "live acceptance scenario validation failed").trim());
  }
  try {
    return JSON.parse(result.stdout);
  } catch {
    throw new Error("live acceptance scenario validator returned invalid JSON");
  }
}

export function requireScenario(scenarioSet, purpose) {
  const scenario = scenarioSet.scenarios.find((candidate) => candidate.purpose === purpose);
  if (!scenario) throw new Error(`REAL_SCENARIO_FILE requires a ${purpose} scenario`);
  return scenario;
}
