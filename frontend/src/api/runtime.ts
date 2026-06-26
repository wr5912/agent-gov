import { authHeaders, makeUrl, readError, requestJson } from "./request";
export { defaultRuntimeConfig, isLegacyDockerApiBase } from "./request";
export * from "./feedback";
export * from "./regressionAssets";
import type {
  AgentInfo,
  AgentSummary,
  AgentCreateRequest,
  AgentDeleteResponse,
  AgentChangeSet,
  AgentChangeSetActionRequest,
  AgentChangeSetCreateRequest,
  AgentChangeSetEvent,
  AgentChangeSetPublishRequest,
  AgentGitDiff,
  AgentGitFileDiff,
  AgentGitRef,
  AgentRelease,
  AgentReleaseRollbackRequest,
  AgentReleaseRestoreRequest,
  AgentReleaseRestoreResponse,
  AgentRepositoryDiscardChangesRequest,
  AgentRepositorySnapshotRequest,
  AgentRepositoryStatus,
  ChatRequest,
  ConfigMappingResponse,
  EvalRunResponse,
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

export function getSessions(config: RuntimeClientConfig) {
  return requestJson<SessionInfo[]>(config, "/api/sessions");
}

export function deleteSession(config: RuntimeClientConfig, sessionId: string) {
  return requestJson<{ deleted: boolean; session_id: string }>(
    config,
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}

export function getAgents(config: RuntimeClientConfig) {
  return requestJson<AgentInfo[]>(config, "/api/agents");
}

// 业务 Agent（治理对象，/api/agent-registry），用于顶栏全局 Agent 切换器与 scoping。
export function listBusinessAgents(config: RuntimeClientConfig) {
  return requestJson<AgentSummary[]>(config, "/api/agent-registry");
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

export function getSkills(config: RuntimeClientConfig) {
  return requestJson<SkillInfo[]>(config, "/api/skills");
}

// /v1/chat/completions 出口 Agent 配置。configured=false 表示从未配置（默认走 main），
// 与显式选 main-agent（configured=true）是不同状态；effective_agent_id 是 /v1 实际运行的 Agent。
export interface OpenAICompatAgentConfig {
  agent_id: string | null;
  configured: boolean;
  effective_agent_id: string;
}

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

export function getConfigMapping(config: RuntimeClientConfig) {
  return requestJson<ConfigMappingResponse>(config, "/api/config");
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
    const res = await fetch(makeUrl(config, "/api/chat/stream"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...authHeaders(config),
      },
      body: JSON.stringify(payload),
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
          const envelope = parseSse(rawEvent);
          if (envelope) dispatchEnvelope(envelope, handlers);
        }
      }

      if (buffer.trim()) {
        const envelope = parseSse(buffer);
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
