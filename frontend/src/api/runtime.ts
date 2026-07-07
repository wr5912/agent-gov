import { authHeaders, makeUrl, readError, requestJson } from "./request";
export { defaultRuntimeConfig, isLegacyDockerApiBase } from "./request";
export * from "./feedback";
export * from "./regressionAssets";
import type {
  AgentInfo,
  AgentSummary,
  AgentCreateRequest,
  BusinessAgentTemplatesResponse,
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
  AgentRepositoryDiscardChangesRequest,
  AgentRepositorySnapshotRequest,
  AgentRepositoryStatus,
  ChatRequest,
  ClaudeUserInputDecisionPayload,
  ClaudeUserInputDecisionResponse,
  ConfigMappingResponse,
  EvalRunResponse,
  OpenAICompatAgentConfig,
  RuntimeClientConfig,
  RuntimeHealth,
  SessionInfo,
  SkillInfo,
  StreamEnvelope,
} from "../types/runtime";
import { isRecord } from "../utils/records";

// 流式空闲超时：60s 对大提示词 + 翻译代理整段缓冲的 Qwen 推理太紧（init 后常 >60s 才吐首个增量），
// 调大到 180s 容纳慢响应；根治需后端 SSE 心跳或代理增量转发（见 v2.8.1 验收记录）。
const STREAM_IDLE_TIMEOUT_MS = 180_000;

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

// 创建业务 Agent 时可选的模板 catalog（E 特性，GET /api/agent-registry/templates）。
export function listBusinessAgentTemplates(config: RuntimeClientConfig) {
  return requestJson<BusinessAgentTemplatesResponse>(config, "/api/agent-registry/templates");
}

export function createBusinessAgent(config: RuntimeClientConfig, payload: AgentCreateRequest) {
  return requestJson<AgentSummary>(config, "/api/agent-registry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
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

export function getCurrentAgentRef(config: RuntimeClientConfig) {
  return requestJson<AgentGitRef>(config, "/api/agent-repository/current");
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

export function runAgentChangeSetRegression(config: RuntimeClientConfig, changeSetId: string, evalCaseIds?: string[]) {
  return requestJson<EvalRunResponse>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/regression-runs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eval_case_ids: evalCaseIds }),
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

export interface StreamChatHandlers {
  onEnvelope?: (envelope: StreamEnvelope) => void;
  onSession?: (sessionId: string, sdkSessionId?: string | null) => void;
  onText?: (text: string, raw: unknown) => void;
  onResult?: (result: unknown) => void;
  onError?: (message: string, raw?: unknown) => void;
  onDone?: () => void;
}

export async function streamChat(
  config: RuntimeClientConfig,
  payload: ChatRequest,
  handlers: StreamChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const controller = new AbortController();
  let timedOut = false;
  let timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort("timeout");
  }, STREAM_IDLE_TIMEOUT_MS);
  const resetIdleTimeout = () => {
    window.clearTimeout(timeoutId);
    timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort("timeout");
    }, STREAM_IDLE_TIMEOUT_MS);
  };
  const abortFromCaller = () => controller.abort(signal?.reason || "aborted");
  if (signal?.aborted) {
    window.clearTimeout(timeoutId);
    throw new Error("Stream request was aborted");
  }
  signal?.addEventListener("abort", abortFromCaller, { once: true });

  try {
    const res = await fetch(makeUrl(config, "/v1/responses"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...authHeaders(config),
      },
      body: JSON.stringify(toResponsesRequest(payload)),
      signal: controller.signal,
    });

    if (!res.ok || !res.body) {
      const detail = await readError(res);
      throw new Error(detail || "Failed to start stream");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetIdleTimeout();
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";
        for (const rawEvent of events) {
          const parsed = parseSse(rawEvent);
          const envelope = parsed ? translateResponsesEnvelope(parsed) : null;
          if (envelope) dispatchEnvelope(envelope, handlers);
        }
      }

      if (buffer.trim()) {
        const parsed = parseSse(buffer);
        const envelope = parsed ? translateResponsesEnvelope(parsed) : null;
        if (envelope) dispatchEnvelope(envelope, handlers);
      }
    } finally {
      reader.releaseLock();
    }
  } catch (error) {
    if (timedOut) {
      throw new Error(`Stream request timed out after ${STREAM_IDLE_TIMEOUT_MS / 1000}s without data`);
    }
    if (signal?.aborted) {
      throw new Error("Stream request was aborted");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

// Playground 走 canonical /v1/responses（control 模式）；ChatRequest -> Responses 请求体。
function toResponsesRequest(payload: ChatRequest): Record<string, unknown> {
  const agentgov: Record<string, unknown> = { agent_id: payload.agent_id };
  if (payload.alert_id) agentgov.alert_id = payload.alert_id;
  if (payload.case_id) agentgov.case_id = payload.case_id;
  if (payload.max_turns != null) agentgov.max_turns = payload.max_turns;
  const body: Record<string, unknown> = { input: payload.message, stream: true, agentgov };
  if (payload.session_id) body.conversation = `conv_${payload.session_id}`;
  if (payload.metadata) body.metadata = payload.metadata;
  return body;
}

// 把 /v1/responses 的 SSE（response.* 标准通道 + agentgov.* 控制信封）翻译回内部事件模型，
// 使 App.tsx / claudeUserInputState / 确认卡无需改动（迁移桥接：Playground 已切到 canonical 入口）。
function translateResponsesEnvelope(env: StreamEnvelope): StreamEnvelope | null {
  const data = env.data;
  const payload = isRecord(data) && isRecord(data.payload) ? data.payload : data;
  switch (env.event) {
    case "agentgov.session":
      return { event: "session", data: payload };
    case "response.output_text.delta":
      return { event: "message", data: { event: "AssistantMessage", text: isRecord(data) ? (data.delta ?? "") : "", raw: {} } };
    case "agentgov.tool_step":
      return { event: "message", data: { event: "AgentGovToolStep", text: "", raw: payload } };
    case "agentgov.sdk_raw":
      return { event: "message", data: { event: "AgentGovSdkRaw", text: "", raw: payload } };
    case "agentgov.result":
      return { event: "result", data: payload };
    case "agentgov.error":
      return { event: "error", data: payload };
    case "response.failed":
      return { event: "error", data: isRecord(data) && isRecord(data.error) ? data.error : data };
    case "agentgov.confirmation.requested":
      return { event: "claude_user_input_required", data: payload };
    case "agentgov.confirmation.resolved":
      return { event: "claude_user_input_resolved", data: payload };
    case "agentgov.done":
      return { event: "done", data: "[DONE]" };
    default:
      // response.created / response.completed / response.in_progress 等：内部数据经 agentgov.* 已下发，丢弃避免重复。
      return null;
  }
}

function parseSse(rawEvent: string): StreamEnvelope | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim() || "message";
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (!dataLines.length) return null;
  const rawData = dataLines.join("\n");
  let data: unknown = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {
    // Keep plain text data.
  }
  return { event, data };
}

function dispatchEnvelope(envelope: StreamEnvelope, handlers: StreamChatHandlers) {
  handlers.onEnvelope?.(envelope);

  if (envelope.event === "session" && isRecord(envelope.data)) {
    const sessionId = stringOrUndefined(envelope.data.session_id);
    const sdkSessionId = stringOrUndefined(envelope.data.sdk_session_id) ?? null;
    if (sessionId) handlers.onSession?.(sessionId, sdkSessionId);
    return;
  }

  if (envelope.event === "message" && isRecord(envelope.data)) {
    const text = stringOrUndefined(envelope.data.text) || "";
    if (text && shouldAppendMessageText(envelope.data)) handlers.onText?.(text, envelope.data.raw ?? envelope.data);
    return;
  }

  if (envelope.event === "result") {
    handlers.onResult?.(envelope.data);
    return;
  }

  if (envelope.event === "error") {
    const errors = isRecord(envelope.data) && Array.isArray(envelope.data.errors)
      ? envelope.data.errors.map(String).join("\n")
      : JSON.stringify(envelope.data);
    handlers.onError?.(errors, envelope.data);
    return;
  }

  if (envelope.event === "done") {
    handlers.onDone?.();
  }
}

function stringOrUndefined(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function shouldAppendMessageText(data: Record<string, unknown>): boolean {
  const sdkEvent = stringOrUndefined(data.event);
  if (!sdkEvent) return true;
  return sdkEvent.startsWith("AssistantMessage");
}
