import type {
  AgentInfo,
  ChatRequest,
  ConfigMappingResponse,
  FeedbackCreateRequest,
  FeedbackEventIngestRequest,
  FeedbackEventIngestResponse,
  FeedbackQueryResponse,
  FeedbackResponse,
  OptimizationProposal,
  RuntimeClientConfig,
  RuntimeHealth,
  SessionInfo,
  SkillInfo,
  StreamEnvelope,
} from "../types/runtime";

const DEFAULT_API_BASE = import.meta.env.VITE_RUNTIME_API_BASE || "http://localhost:58080";
const DEFAULT_API_KEY = import.meta.env.VITE_RUNTIME_API_KEY || "";

export function defaultRuntimeConfig(): RuntimeClientConfig {
  return {
    apiBase: DEFAULT_API_BASE,
    apiKey: DEFAULT_API_KEY,
  };
}

function normalizeBase(apiBase: string): string {
  return apiBase.trim().replace(/\/$/, "");
}

function makeUrl(config: RuntimeClientConfig, path: string): string {
  const base = normalizeBase(config.apiBase);
  if (!base) return path;
  return `${base}${path}`;
}

function authHeaders(config: RuntimeClientConfig): HeadersInit {
  const headers: Record<string, string> = {};
  if (config.apiKey.trim()) {
    headers.Authorization = `Bearer ${config.apiKey.trim()}`;
  }
  return headers;
}

async function requestJson<T>(config: RuntimeClientConfig, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(makeUrl(config, path), {
    ...init,
    headers: {
      Accept: "application/json",
      ...authHeaders(config),
      ...(init?.headers || {}),
    },
  });

  if (!res.ok) {
    const detail = await readError(res);
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }

  return (await res.json()) as T;
}

async function readError(res: Response): Promise<string> {
  try {
    const json = await res.json();
    if (typeof json?.detail === "string") return json.detail;
    return JSON.stringify(json);
  } catch {
    try {
      return await res.text();
    } catch {
      return "";
    }
  }
}

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

export function getConfigMapping(config: RuntimeClientConfig) {
  return requestJson<ConfigMappingResponse>(config, "/api/config");
}

export function createFeedback(config: RuntimeClientConfig, payload: FeedbackCreateRequest) {
  return requestJson<FeedbackResponse>(config, "/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function ingestFeedbackEvent(config: RuntimeClientConfig, payload: FeedbackEventIngestRequest) {
  return requestJson<FeedbackEventIngestResponse>(config, "/api/feedback/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getFeedback(config: RuntimeClientConfig) {
  return requestJson<FeedbackQueryResponse>(config, "/api/feedback");
}

export function getOptimizationProposals(config: RuntimeClientConfig) {
  return requestJson<OptimizationProposal[]>(config, "/api/optimization-proposals");
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
