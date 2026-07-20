import type {
  AgentTestAssetSummary,
  AgentTestRun,
  AgentTestRunCreateRequest,
  AgentTestRunHistory,
  AgentTestSchedule,
  AgentTestScheduleEvent,
  AgentTestScheduleUpdateRequest,
  AgentTestSuite,
  AgentTestSuiteFile,
  RuntimeClientConfig,
} from "../types/runtime";
import { requestJson } from "./request";

export function inspectAgentTestSuite(config: RuntimeClientConfig, agentId: string, commitSha?: string) {
  const params = new URLSearchParams();
  if (commitSha) params.set("commit_sha", commitSha);
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<AgentTestSuite>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/test-suite${query}`,
  );
}

export function listAgentTestAssets(config: RuntimeClientConfig) {
  return requestJson<AgentTestAssetSummary[]>(config, "/api/agent-test-assets");
}

export function getAgentTestSuiteFile(
  config: RuntimeClientConfig,
  agentId: string,
  path: string,
  commitSha?: string,
) {
  const params = new URLSearchParams({ path });
  if (commitSha) params.set("commit_sha", commitSha);
  return requestJson<AgentTestSuiteFile>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/test-suite/file?${params.toString()}`,
  );
}

export function getAgentTestSchedule(config: RuntimeClientConfig, agentId: string) {
  return requestJson<AgentTestSchedule>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/test-schedule`,
  );
}

export function updateAgentTestSchedule(
  config: RuntimeClientConfig,
  agentId: string,
  payload: AgentTestScheduleUpdateRequest,
) {
  return requestJson<AgentTestSchedule>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/test-schedule`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function listAgentTestScheduleEvents(
  config: RuntimeClientConfig,
  agentId: string,
  limit = 100,
) {
  return requestJson<AgentTestScheduleEvent[]>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/test-schedule/events?${new URLSearchParams({ limit: String(limit) }).toString()}`,
  );
}

export function createAgentTestRun(
  config: RuntimeClientConfig,
  payload: AgentTestRunCreateRequest,
) {
  return requestJson<AgentTestRun>(
    config,
    "/api/agent-test-runs",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function createAgentChangeSetTestRun(
  config: RuntimeClientConfig,
  changeSetId: string,
) {
  return requestJson<AgentTestRun>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/test-runs`,
    { method: "POST" },
  );
}

export function listAgentTestRuns(
  config: RuntimeClientConfig,
  filters: { agentId?: string; changeSetId?: string; limit?: number } = {},
) {
  const params = new URLSearchParams();
  if (filters.agentId) params.set("agent_id", filters.agentId);
  if (filters.changeSetId) params.set("change_set_id", filters.changeSetId);
  if (filters.limit) params.set("limit", String(filters.limit));
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<AgentTestRun[]>(config, `/api/agent-test-runs${query}`);
}

export function listAgentTestRunHistory(
  config: RuntimeClientConfig,
  filters: {
    agentId?: string;
    status?: string;
    source?: string;
    commitSha?: string;
    cursor?: string;
    limit?: number;
  } = {},
) {
  const params = new URLSearchParams();
  if (filters.agentId) params.set("agent_id", filters.agentId);
  if (filters.status) params.set("status", filters.status);
  if (filters.source) params.set("source", filters.source);
  if (filters.commitSha) params.set("commit_sha", filters.commitSha);
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.limit) params.set("limit", String(filters.limit));
  const query = params.size ? `?${params.toString()}` : "";
  return requestJson<AgentTestRunHistory>(config, `/api/agent-test-runs/history${query}`);
}

export function getAgentTestRun(config: RuntimeClientConfig, testRunId: string) {
  return requestJson<AgentTestRun>(config, `/api/agent-test-runs/${encodeURIComponent(testRunId)}`);
}

export function cancelAgentTestRun(config: RuntimeClientConfig, testRunId: string) {
  return requestJson<AgentTestRun>(
    config,
    `/api/agent-test-runs/${encodeURIComponent(testRunId)}/cancel`,
    { method: "POST", headers: { "Content-Type": "application/json" } },
  );
}
