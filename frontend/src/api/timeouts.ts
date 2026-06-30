const DEFAULT_GOVERNANCE_AGENT_TIMEOUT_SECONDS = 300;

function positiveSeconds(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_GOVERNANCE_AGENT_TIMEOUT_SECONDS;
  return parsed;
}

export const GOVERNANCE_AGENT_TIMEOUT_MS =
  positiveSeconds(import.meta.env.VITE_GOVERNANCE_AGENT_TIMEOUT_SECONDS) * 1000;
