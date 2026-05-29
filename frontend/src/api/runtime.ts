import { authHeaders, makeUrl, readError, requestJson } from "./request";
export { defaultRuntimeConfig } from "./request";
export * from "./feedback";
import type {
  AgentInfo,
  AgentVersionDiff,
  AgentVersionFileDiff,
  AgentVersionManifest,
  AgentVersionRestoreRequest,
  AgentVersionRestoreResponse,
  AgentVersionSnapshotRequest,
  AgentVersionSummary,
  ChatRequest,
  ConfigMappingResponse,
  RuntimeClientConfig,
  RuntimeHealth,
  SessionInfo,
  SkillInfo,
  StreamEnvelope,
} from "../types/runtime";

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

export function getSkills(config: RuntimeClientConfig) {
  return requestJson<SkillInfo[]>(config, "/api/skills");
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

export function getCurrentAgentVersion(config: RuntimeClientConfig) {
  return requestJson<AgentVersionSummary>(config, "/api/agent-versions/main/current");
}

export function getAgentVersions(config: RuntimeClientConfig) {
  return requestJson<AgentVersionSummary[]>(config, "/api/agent-versions/main");
}

export function getAgentVersion(config: RuntimeClientConfig, versionId: string) {
  return requestJson<AgentVersionManifest>(config, `/api/agent-versions/main/${encodeURIComponent(versionId)}`);
}

export function createAgentVersionSnapshot(config: RuntimeClientConfig, payload: AgentVersionSnapshotRequest) {
  return requestJson<AgentVersionSummary>(config, "/api/agent-versions/main/snapshots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function restoreAgentVersion(config: RuntimeClientConfig, versionId: string, payload: AgentVersionRestoreRequest) {
  return requestJson<AgentVersionRestoreResponse>(
    config,
    `/api/agent-versions/main/${encodeURIComponent(versionId)}/rollback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function diffAgentVersions(config: RuntimeClientConfig, fromVersionId: string, toVersionId: string) {
  const params = new URLSearchParams({ from_version_id: fromVersionId, to_version_id: toVersionId });
  return requestJson<AgentVersionDiff>(config, `/api/agent-versions/main/diff?${params.toString()}`);
}

export function diffAgentVersionFile(config: RuntimeClientConfig, fromVersionId: string, toVersionId: string, path: string) {
  const params = new URLSearchParams({ from_version_id: fromVersionId, to_version_id: toVersionId, path });
  return requestJson<AgentVersionFileDiff>(config, `/api/agent-versions/main/file-diff?${params.toString()}`);
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
  const res = await fetch(makeUrl(config, "/api/chat/stream"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...authHeaders(config),
    },
    body: JSON.stringify(payload),
    signal,
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function stringOrUndefined(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function shouldAppendMessageText(data: Record<string, unknown>): boolean {
  const sdkEvent = stringOrUndefined(data.event);
  if (!sdkEvent) return true;
  return sdkEvent.startsWith("AssistantMessage");
}
