import process from "node:process";
import { execFileSync } from "node:child_process";

export function requireContainerAcceptance(enabled = true) {
  if (!enabled) return;
  const active = process.env.AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE === "1";
  const runId = String(process.env.AGENT_GOV_ACCEPTANCE_RUN_ID || "").trim();
  if (!active || !runId) {
    throw new Error(
      "Real-container verification must run through its public Make target so images and services are refreshed first.",
    );
  }
  const python = String(process.env.PYTHON || ".venv/bin/python").trim();
  try {
    execFileSync(python, ["scripts/verify_container_acceptance_context.py"], {
      cwd: process.cwd(),
      env: process.env,
      stdio: ["ignore", "ignore", "pipe"],
    });
  } catch {
    throw new Error(
      "Real-container verification context is stale or did not come from the public Make runner.",
    );
  }
}
