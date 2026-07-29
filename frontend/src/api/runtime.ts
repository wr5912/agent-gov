import { requestBlob, requestJson } from "./request";
import { GOVERNANCE_AGENT_TIMEOUT_MS } from "./timeouts";
export { streamClaudeSdkChat as streamChat } from "./claudeSdkStream";
export type { StreamChatHandlers } from "./claudeSdkStream";
export { defaultRuntimeConfig, isLegacyDockerApiBase } from "./request";
export * from "./agentTesting";
export * from "./feedback";
import type {
  AgentInfo,
  AgentPresentation,
  AgentSummary,
  AgentDeleteResponse,
  AgentChangeSet,
  AgentChangeSetActionRequest,
  AgentChangeSetCreateRequest,
  AgentChangeSetEvent,
  AgentChangeSetPublishRequest,
  AgentGitDiff,
  AgentGitFileDiff,
  AgentGitRef,
  AgentConfigFileResponse,
  AgentConfigFileUpdateRequest,
  AgentConfigFileUpdateResponse,
  AgentRelease,
  AgentReleaseRollbackRequest,
  AgentReleaseRestoreRequest,
  AgentReleaseRestoreResponse,
  AgentRunCancelResponse,
  AgentRepositoryDiscardChangesRequest,
  AgentRepositorySnapshotRequest,
  AgentRepositoryStatus,
  ClaudeUserInputDecisionPayload,
  ClaudeUserInputDecisionResponse,
  ConfigMappingResponse,
  ConversationItem,
  ConversationItemList,
  OpenAICompatAgentConfig,
  RuntimeClientConfig,
  RuntimeHealth,
  SessionInfo,
  SkillInfo,
  WorkspaceImportResponse,
  WorkspaceRestoreRequest,
  WorkspaceRestoreResponse,
} from "../types/runtime";
import { isRecord } from "../utils/records";

export function getHealth(config: RuntimeClientConfig) {
  return requestJson<RuntimeHealth>(config, "/health");
}

// 会话侧栏走 canonical /v1/conversations（投影自同一 session_store）；映射回 SessionInfo 使侧栏无需改动。
export async function getSessions(config: RuntimeClientConfig): Promise<SessionInfo[]> {
  const list = await requestJson<{ data?: unknown[] }>(config, "/v1/conversations");
  const data = Array.isArray(list.data) ? list.data : [];
  return data.map(conversationToSessionInfo).filter((session): session is SessionInfo => session !== null);
}

export function deleteSession(config: RuntimeClientConfig, sessionId: string) {
  return requestJson<{ deleted: boolean; id: string }>(
    config,
    `/v1/conversations/${encodeURIComponent(`conv_${sessionId}`)}`,
    { method: "DELETE" },
  );
}

export function cancelAgentRun(
  config: RuntimeClientConfig,
  runId: string,
  signal?: AbortSignal,
) {
  return requestJson<AgentRunCancelResponse>(
    config,
    `/api/agent-runs/${encodeURIComponent(runId)}/cancel`,
    {
      method: "POST",
      signal,
      timeoutMs: 15_000,
    },
  );
}

export async function getConversationItems(
  config: RuntimeClientConfig,
  sessionId: string,
  signal?: AbortSignal,
): Promise<ConversationItem[]> {
  // Callers hold the internal session id. Always add the public prefix once,
  // including when a legacy client chose a session id that starts with "conv_".
  const conversationId = `conv_${sessionId}`;
  const items: ConversationItem[] = [];
  const seenCursors = new Set<string>();
  let after: string | undefined;

  while (true) {
    const query = new URLSearchParams({ limit: "100", order: "asc" });
    if (after) query.set("after", after);
    const page = await requestJson<ConversationItemList>(
      config,
      `/v1/conversations/${encodeURIComponent(conversationId)}/items?${query.toString()}`,
      { signal },
    );
    const pageItems = Array.isArray(page.data) ? page.data : [];
    items.push(...pageItems);
    if (!page.has_more) return items;

    const cursor = page.last_id || pageItems.at(-1)?.id;
    if (!cursor || seenCursors.has(cursor)) {
      throw new Error("Conversation items pagination returned an invalid cursor");
    }
    seenCursors.add(cursor);
    after = cursor;
  }
}

function conversationToSessionInfo(value: unknown): SessionInfo | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  const sessionId = value.id.startsWith("conv_") ? value.id.slice("conv_".length) : value.id;
  const ag = isRecord(value.agentgov) ? value.agentgov : {};
  const epochToIso = (epoch: unknown): string | undefined =>
    typeof epoch === "number" ? new Date(epoch * 1000).toISOString() : undefined;
  const createdAt = epochToIso(value.created_at) || new Date().toISOString();
  return {
    session_id: sessionId,
    sdk_session_id: typeof ag.sdk_session_id === "string" ? ag.sdk_session_id : null,
    agent_id: typeof ag.agent_id === "string" ? ag.agent_id : null,
    created_at: createdAt,
    updated_at: epochToIso(ag.updated_at) || createdAt,
    title: typeof value.title === "string" ? value.title : undefined,
    turns: typeof ag.turns === "number" ? ag.turns : 0,
    metadata: isRecord(value.metadata) ? value.metadata : {},
    active_run_id: typeof ag.active_run_id === "string" ? ag.active_run_id : null,
    active_run_expires_at: typeof ag.active_run_expires_at === "string" ? ag.active_run_expires_at : null,
  } as SessionInfo;
}

export function getAgents(config: RuntimeClientConfig, agentId?: string) {
  const query = agentId ? `?${new URLSearchParams({ agent_id: agentId }).toString()}` : "";
  return requestJson<AgentInfo[]>(config, `/api/agents${query}`);
}

// 业务 Agent（治理对象，/api/agent-registry），用于顶栏全局 Agent 切换器与 scoping。
export function listBusinessAgents(config: RuntimeClientConfig) {
  return requestJson<AgentSummary[]>(config, "/api/agent-registry");
}

export function getBusinessAgentPresentation(
  config: RuntimeClientConfig,
  agentId: string,
  signal?: AbortSignal,
) {
  return requestJson<AgentPresentation>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/presentation`,
    { signal },
  );
}

export interface WorkspaceImportPayload {
  package: File;
  name?: string;
  expectedCurrentCommitSha?: string;
  reason?: string;
}

export interface WorkspaceExportFile {
  blob: Blob;
  filename: string;
  commitSha: string;
  packageSha256: string;
  treeSha256: string;
}

function responseFilename(headers: Headers): string | undefined {
  const disposition = headers.get("content-disposition") || "";
  const utf8Match = disposition.match(/filename\*=utf-8''([^;]+)/i);
  if (utf8Match?.[1]) return decodeURIComponent(utf8Match[1].trim());
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1]?.trim();
}

export async function exportBusinessAgentWorkspace(
  config: RuntimeClientConfig,
  agentId: string,
): Promise<WorkspaceExportFile> {
  const { blob, headers } = await requestBlob(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/workspace/export`,
    { method: "POST", timeoutMs: 120_000 },
  );
  return {
    blob,
    filename: responseFilename(headers) || `${agentId}-workspace.tar.gz`,
    commitSha: headers.get("x-agent-commit-sha") || "",
    packageSha256: headers.get("x-workspace-package-sha256") || "",
    treeSha256: headers.get("x-workspace-tree-sha256") || "",
  };
}

export function importBusinessAgentWorkspace(
  config: RuntimeClientConfig,
  agentId: string,
  payload: WorkspaceImportPayload,
) {
  const body = new FormData();
  body.append("package", payload.package);
  if (payload.name) body.append("name", payload.name);
  if (payload.expectedCurrentCommitSha) body.append("expected_current_commit_sha", payload.expectedCurrentCommitSha);
  if (payload.reason) body.append("reason", payload.reason);
  return requestJson<WorkspaceImportResponse>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/workspace/import`,
    { method: "POST", body, timeoutMs: 120_000 },
  );
}

export function restoreBusinessAgentWorkspace(
  config: RuntimeClientConfig,
  agentId: string,
  payload: WorkspaceRestoreRequest,
) {
  return requestJson<WorkspaceRestoreResponse>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/workspace/restore`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 120_000,
    },
  );
}

export function setBusinessAgentLifecycle(config: RuntimeClientConfig, agentId: string, status: string) {
  return requestJson<AgentSummary>(config, `/api/agent-registry/${encodeURIComponent(agentId)}/lifecycle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

export function deleteBusinessAgent(config: RuntimeClientConfig, agentId: string) {
  return requestJson<AgentDeleteResponse>(config, `/api/agent-registry/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
}

export function getSkills(config: RuntimeClientConfig, agentId?: string) {
  const query = agentId ? `?${new URLSearchParams({ agent_id: agentId }).toString()}` : "";
  return requestJson<SkillInfo[]>(config, `/api/skills${query}`);
}

// F12：/v1 出口 Agent 配置类型改用 OpenAPI 生成类型（删手写 schema 双轨），从 types/runtime re-export。
export type { OpenAICompatAgentConfig };

export function getOpenAICompatAgent(config: RuntimeClientConfig) {
  return requestJson<OpenAICompatAgentConfig>(config, "/api/settings/openai-compat-agent");
}

export function setOpenAICompatAgent(config: RuntimeClientConfig, agentId: string) {
  return requestJson<OpenAICompatAgentConfig>(config, "/api/settings/openai-compat-agent", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: agentId }),
  });
}

export function resetOpenAICompatAgent(config: RuntimeClientConfig) {
  return requestJson<OpenAICompatAgentConfig>(config, "/api/settings/openai-compat-agent", {
    method: "DELETE",
  });
}

export const runtimeApi = {
  health: getHealth,
  sessions: getSessions,
  agents: getAgents,
  skills: getSkills,
};

export function getConfigMapping(config: RuntimeClientConfig, agentId?: string) {
  const params = new URLSearchParams();
  if (agentId) params.set("agent_id", agentId);
  const query = params.toString();
  return requestJson<ConfigMappingResponse>(config, `/api/config${query ? `?${query}` : ""}`);
}

export function getAgentConfigFile(config: RuntimeClientConfig, agentId: string, path: string) {
  const params = new URLSearchParams({ agent_id: agentId, path });
  return requestJson<AgentConfigFileResponse>(config, `/api/agent-config-file?${params.toString()}`);
}

export function updateAgentConfigFile(
  config: RuntimeClientConfig,
  agentId: string,
  path: string,
  payload: AgentConfigFileUpdateRequest,
) {
  const params = new URLSearchParams({ agent_id: agentId, path });
  return requestJson<AgentConfigFileUpdateResponse>(config, `/api/agent-config-file?${params.toString()}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getAgentRepositoryStatus(config: RuntimeClientConfig) {
  return requestJson<AgentRepositoryStatus>(config, "/api/agent-repository");
}

export function discardAgentRepositoryChanges(config: RuntimeClientConfig, payload: AgentRepositoryDiscardChangesRequest) {
  return requestJson<AgentRepositoryStatus>(config, "/api/agent-repository/discard-changes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function snapshotAgentRepository(config: RuntimeClientConfig, payload: AgentRepositorySnapshotRequest = { operator: "ui" }) {
  return requestJson<AgentGitRef>(config, "/api/agent-repository/snapshot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getCurrentAgentRef(config: RuntimeClientConfig, agentId?: string) {
  const query = agentId ? `?${new URLSearchParams({ agent_id: agentId }).toString()}` : "";
  return requestJson<AgentGitRef>(config, `/api/agent-repository/current${query}`);
}

export function getAgentChangeSets(config: RuntimeClientConfig) {
  return requestJson<AgentChangeSet[]>(config, "/api/agent-change-sets");
}

export function createAgentChangeSet(config: RuntimeClientConfig, payload: AgentChangeSetCreateRequest) {
  return requestJson<AgentChangeSet>(config, "/api/agent-change-sets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getAgentChangeSet(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentChangeSet>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}`);
}

export function getAgentChangeSetEvents(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentChangeSetEvent[]>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/events`);
}

export function diffAgentChangeSet(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentGitDiff>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/diff`);
}

export function diffAgentChangeSetFile(config: RuntimeClientConfig, changeSetId: string, path: string) {
  const params = new URLSearchParams({ path });
  return requestJson<AgentGitFileDiff>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/file-diff?${params.toString()}`);
}

export function approveAgentChangeSet(config: RuntimeClientConfig, changeSetId: string, payload: AgentChangeSetActionRequest = { operator: "ui" }) {
  return requestJson<AgentChangeSet>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function rejectAgentChangeSet(config: RuntimeClientConfig, changeSetId: string, payload: AgentChangeSetActionRequest = { operator: "ui" }) {
  return requestJson<AgentChangeSet>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/reject`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function retryAgentChangeSetWorktreeCleanup(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentChangeSet>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/worktree-cleanup/retry`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator: "ui" }),
    },
  );
}

export function publishAgentChangeSet(config: RuntimeClientConfig, changeSetId: string, payload: AgentChangeSetPublishRequest = { operator: "ui", force: false }) {
  return requestJson<AgentRelease>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/publish`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function getAgentReleases(config: RuntimeClientConfig) {
  return requestJson<AgentRelease[]>(config, "/api/agent-releases");
}

export function rollbackAgentRelease(config: RuntimeClientConfig, releaseId: string, payload: AgentReleaseRollbackRequest = { operator: "ui" }) {
  return requestJson<AgentRelease>(
    config,
    `/api/agent-releases/${encodeURIComponent(releaseId)}/rollback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function restoreAgentRelease(config: RuntimeClientConfig, releaseId: string, payload: AgentReleaseRestoreRequest = { operator: "ui" }) {
  return requestJson<AgentReleaseRestoreResponse>(
    config,
    `/api/agent-releases/${encodeURIComponent(releaseId)}/restore`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function submitClaudeUserInputDecision(config: RuntimeClientConfig, requestId: string, payload: ClaudeUserInputDecisionPayload) {
  return requestJson<ClaudeUserInputDecisionResponse>(
    config,
    `/v1/agentgov/confirmation-requests/${encodeURIComponent(requestId)}/decision`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}
