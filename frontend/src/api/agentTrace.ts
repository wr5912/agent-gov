import { requestJson } from "./request";
import type { AgentRunTrace, RuntimeClientConfig } from "../types/runtime";

export function getAgentRunTrace(
  config: RuntimeClientConfig,
  runId: string,
  signal?: AbortSignal,
) {
  return requestJson<AgentRunTrace>(
    config,
    `/api/agent-runs/${encodeURIComponent(runId)}/trace`,
    { signal },
  );
}
