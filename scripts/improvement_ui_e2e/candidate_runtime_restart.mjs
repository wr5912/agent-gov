// 真实候选测试的受控维护：只调用同一所选环境的既有公开 Make 入口。
import { execFile } from "node:child_process";
import { isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const attemptedJourneys = new WeakSet();

export class CandidateRuntimeRestartError extends Error {
  constructor(code) {
    super(code);
    this.name = "CandidateRuntimeRestartError";
    this.code = code;
  }
}

export function isRuntimeTemplateRestartRequired(signal) {
  return signal !== null && typeof signal === "object" && !Array.isArray(signal)
    && signal.error_code === "RUNTIME_TEMPLATE_RESTART_REQUIRED";
}

export async function restartCandidateRuntime({ signal, stage, maintenance }) {
  if (!isRuntimeTemplateRestartRequired(signal)) throw new CandidateRuntimeRestartError("RUNTIME_RESTART_SIGNAL_REQUIRED");
  if (stage !== "candidate_test") throw new CandidateRuntimeRestartError("RUNTIME_RESTART_STAGE_INVALID");
  if (!Array.isArray(maintenance) || maintenance.length || attemptedJourneys.has(maintenance)) {
    throw new CandidateRuntimeRestartError("RUNTIME_RESTART_ATTEMPT_LIMIT");
  }
  const envFile = process.env.COMPOSE_ENV_FILE;
  if (process.env.REQUIRE_LIVE_RUNTIME !== "1" || !envFile || !isAbsolute(envFile)) {
    throw new CandidateRuntimeRestartError("RUNTIME_RESTART_CONTEXT_REQUIRED");
  }
  attemptedJourneys.add(maintenance);
  try {
    await execFileAsync("/usr/bin/make", [
      "--no-print-directory", "runtime-recreate", `COMPOSE_ENV_FILE=${envFile}`, "RUNTIME_RECREATE_REQUIRE_IDLE=1",
    ], { cwd: fileURLToPath(new URL("../../", import.meta.url)), env: process.env,
      timeout: 600000, maxBuffer: 1024 * 1024 });
  } catch {
    // Make/Compose 输出可能含所选私有路径，不将 stdout、stderr 或原异常落入验收报告。
    throw new CandidateRuntimeRestartError("RUNTIME_RESTART_MAKE_FAILED");
  }
  const receipt = { operation: "runtime-recreate", completed: true, stage: "candidate_test" };
  maintenance.push(receipt);
  return receipt;
}
