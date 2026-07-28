import process from "node:process";

export function requireContainerAcceptance(enabled = true) {
  if (!enabled) return;
  const active = process.env.AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE === "1";
  const runId = String(process.env.AGENT_GOV_ACCEPTANCE_RUN_ID || "").trim();
  if (!active || !runId) {
    throw new Error(
      "Real-container verification must run through its public Make target so images and services are refreshed first.",
    );
  }
}
