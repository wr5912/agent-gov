import { beforeEach, vi } from "vitest";

import type { FeedbackRunRecord } from "../types/feedback";
import type {
  AgentScopeAgentEvent,
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimePendingAction,
  RuntimeUserConfirmRequest,
} from "../types/runtime";
import type { PlaygroundRunState } from "../playgroundRunState";
import type { PlaygroundRunOptions } from "./usePlaygroundRun";

// 测试先导入此模块注册 mocks，再直接导入 usePlaygroundRun。
const mocks = vi.hoisted(() => ({
  effects: [] as Array<() => void | (() => void)>,
  connect: vi.fn(),
  createSession: vi.fn(),
  messages: vi.fn(),
  status: vi.fn(),
  chat: vi.fn(),
  getRun: vi.fn(),
  getRunByOperation: vi.fn(),
  getPendingActions: vi.fn(),
  cancelRun: vi.fn(),
}));

vi.mock("react", () => ({
  useEffect: (effect: () => void | (() => void)) => {
    mocks.effects.push(effect);
  },
  useRef: <T>(initial: T) => ({ current: initial }),
}));

vi.mock("../api/runtime", () => ({
  connectAgentScopeSessionStream: mocks.connect,
  createRuntimeSession: mocks.createSession,
  getRuntimeSessionMessages: mocks.messages,
  getRuntimeSessionStatus: mocks.status,
  startRuntimeChat: mocks.chat,
}));

vi.mock("../api/feedback", () => ({
  cancelAgentRun: mocks.cancelRun,
  getAgentRun: mocks.getRun,
  getAgentRunByClientOperation: mocks.getRunByOperation,
  getAgentRunPendingActions: mocks.getPendingActions,
}));

function terminalRun(overrides: Partial<FeedbackRunRecord> = {}): FeedbackRunRecord {
  return {
    run_id: "run-1",
    session_id: "session-1",
    agent_id: "business-1",
    agent_version_id: "version-1",
    harness_digest: "harness-1",
    runtime_agent_id: "runtime-1",
    status: "succeeded",
    trace_status: "complete",
    team_generation: 0,
    root_persisted_team_generation: 0,
    created_at: "2026-09-10T00:00:00Z",
    updated_at: "2026-09-10T00:00:01Z",
    client_operation_id: "operation-1",
    reply_ids: ["reply-1"],
    ...overrides,
  };
}

beforeEach(() => {
  mocks.effects.length = 0;
  vi.clearAllMocks();
  mocks.messages.mockResolvedValue({ messages: [], is_running: false, has_more: false });
  mocks.status.mockResolvedValue({ session_id: "session-1", status: "idle" });
  mocks.getRun.mockResolvedValue(terminalRun());
  mocks.getPendingActions.mockResolvedValue([]);
  mocks.cancelRun.mockResolvedValue(terminalRun({ status: "cancelled" }));
});

function toolRequest(): RuntimeUserConfirmRequest {
  return {
    requestId: "confirm-1",
    replyId: "reply-1",
    toolCalls: [{
      type: "tool_call",
      id: "tool-1",
      name: "Read",
      input: '{"path":"AGENT.md"}',
      state: "asking",
    }],
    status: "waiting",
  };
}

function externalRequest(): RuntimeExternalExecutionRequest {
  return {
    requestId: "external-1",
    replyId: "reply-1",
    toolCalls: [{
      type: "tool_call",
      id: "tool-external",
      name: "ExternalLookup",
      input: '{"query":"evidence"}',
      state: "asking",
    }],
    status: "waiting",
  };
}

function streamConnection(order: string[], reply = new Promise<AgentScopeAgentEvent>(() => undefined)) {
  const connection = {
    setRunId: vi.fn(() => order.push("setRunId")),
    armReply: vi.fn(() => {
      order.push("armReply");
      return reply;
    }),
    close: vi.fn(),
    closed: new Promise<void>(() => undefined),
  };
  mocks.connect.mockImplementation(async () => {
    order.push("connect");
    return connection;
  });
  return connection;
}

async function pendingActionFor(
  actionId: string,
  sessionId: string,
  toolCall: RuntimeUserConfirmRequest["toolCalls"][number],
): Promise<RuntimePendingAction> {
  const canonical = canonicalJson(toolCall);
  const bytes = new TextEncoder().encode(canonical);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const state = toolCall.state;
  const toolCallState = state === "pending" || state === "asking" || state === "allowed"
    || state === "submitted" || state === "finished"
    ? state
    : "asking";
  return {
    action_id: actionId,
    session_id: sessionId,
    run_id: "run-1",
    reply_id: "reply-1",
    kind: "human",
    tool_call_id: toolCall.id,
    tool_call_name: toolCall.name,
    tool_call_state: toolCallState,
    tool_call_utf8_length: bytes.byteLength,
    tool_call_sha256: [...new Uint8Array(digest)]
      .map((value) => value.toString(16).padStart(2, "0"))
      .join(""),
    status: "pending",
    created_at: "2026-09-10T00:00:00Z",
  };
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().filter((key) => record[key] !== undefined).map((key) => (
    `${JSON.stringify(key)}:${canonicalJson(record[key])}`
  )).join(",")}}`;
}

function options(
  runState: PlaygroundRunState,
  overrides: Partial<PlaygroundRunOptions> = {},
): PlaygroundRunOptions {
  let messages: ChatMessage[] = [...(overrides.activeMessages || [])];
  return {
    clientConfig: { apiBase: "http://runtime.test", apiKey: "" },
    input: "hello",
    runState,
    dispatchRun: vi.fn(),
    activeSessionId: "session-1",
    activeMessages: messages,
    activeMessagesLoaded: true,
    selectedBusinessAgentId: "business-1",
    runtimeAgentId: "runtime-1",
    alertId: "",
    caseId: "",
    promptSuggestion: { clear: vi.fn() },
    setInput: vi.fn(),
    setStreamingAssistantMessageId: vi.fn(),
    setLastError: vi.fn(),
    setSessionSidebarOpen: vi.fn(),
    setEvidencePanelOpen: vi.fn(),
    setActiveTraceMessageId: vi.fn(),
    setUserInputErrors: vi.fn(),
    setSubmittingUserInputRequests: vi.fn(),
    claimLocalSession: vi.fn(),
    updateSessionMessages: vi.fn((_sessionId: string, updater: (current: ChatMessage[]) => ChatMessage[]) => {
      messages = updater(messages);
    }),
    updateUserConfirmRequest: vi.fn(),
    updateExternalExecutionRequest: vi.fn(),
    cancelUserConfirmForMessage: vi.fn(),
    cancelExternalExecutionForMessage: vi.fn(),
    calibrateTrace: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

export {
  externalRequest,
  mocks,
  options,
  pendingActionFor,
  streamConnection,
  terminalRun,
  toolRequest,
};
