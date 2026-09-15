import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "./api/request";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import { isMutablePlaygroundTurn } from "./playgroundRunLifecycle";
import type { PlaygroundRunOptions } from "./playgroundRunContract";
import { createPlaygroundRunStreamHandlers } from "./playgroundRunStream";
import type {
  AgentScopeTextBlockDeltaEvent,
  AgentScopeAgentEvent,
  ChatMessage,
} from "./types/runtime";

const apiMocks = vi.hoisted(() => ({
  getRun: vi.fn(),
  getPendingActions: vi.fn(),
}));

vi.mock("./api/feedback", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/feedback")>(),
  getAgentRun: apiMocks.getRun,
  getAgentRunPendingActions: apiMocks.getPendingActions,
}));

beforeEach(() => {
  vi.clearAllMocks();
  apiMocks.getPendingActions.mockResolvedValue([]);
});

function textDelta(id: string, delta: string): AgentScopeTextBlockDeltaEvent {
  return {
    id,
    type: "TEXT_BLOCK_DELTA",
    block_id: "block-1",
    reply_id: "reply-1",
    delta,
    created_at: "2026-09-14T00:00:00Z",
    metadata: {},
  };
}

function callbackContract(): PlaygroundRunOptions {
  return {
    clientConfig: { apiBase: "", apiKey: "" },
    input: "",
    runState: { phase: "running" },
    dispatchRun: () => undefined,
    activeSessionId: "session-1",
    activeMessages: [],
    activeMessagesLoaded: true,
    selectedBusinessAgentId: "agent-1",
    runtimeAgentId: "runtime-agent-1",
    promptSuggestion: { clear: () => undefined },
    setInput: () => undefined,
    setStreamingAssistantMessageId: () => undefined,
    setLastError: () => undefined,
    setSessionSidebarOpen: () => undefined,
    setEvidencePanelOpen: () => undefined,
    setActiveTraceMessageId: () => undefined,
    setUserInputErrors: () => undefined,
    setSubmittingUserInputRequests: () => undefined,
    claimLocalSession: () => undefined,
    updateSessionMessages: () => undefined,
    updateUserConfirmRequest: () => undefined,
    updateExternalExecutionRequest: () => undefined,
    cancelUserConfirmForMessage: () => undefined,
    cancelExternalExecutionForMessage: () => undefined,
    calibrateTrace: async () => undefined,
    refresh: async () => undefined,
  };
}

function pendingEvent(kind: "human" | "external"): AgentScopeAgentEvent {
  return {
    id: `event-${kind}`,
    type: kind === "human" ? "REQUIRE_USER_CONFIRM" : "REQUIRE_EXTERNAL_EXECUTION",
    reply_id: `reply-${kind}`,
    tool_calls: [{
      type: "tool_call",
      id: `tool-${kind}`,
      name: "Read",
      input: '{"path":"AGENT.md"}',
      state: "asking",
    }],
    created_at: "2026-09-14T00:00:00Z",
    metadata: {},
  } as AgentScopeAgentEvent;
}

describe("Playground SSE 生命周期围栏", () => {
  it("已 sealed 或 stale 的晚到 TEXT_BLOCK_DELTA 不再修改消息，也不登记为已展示", () => {
    const turn: PlaygroundActiveTurn = {
      sessionId: "session-1",
      agentId: "runtime-agent-1",
      assistantMessageId: "reply-1",
      operationId: "operation-1",
      controller: new AbortController(),
      completed: false,
      sealed: false,
      stopRequested: false,
      chatSubmitted: true,
      presentedTextEventIds: new Set(),
    };
    let activeOperationId: string | null = "operation-1";
    let assistant: ChatMessage = {
      id: "reply-1",
      role: "assistant",
      content: "",
      createdAt: "2026-09-14T00:00:00Z",
      sessionId: "session-1",
      events: [],
    };
    const handlers = createPlaygroundRunStreamHandlers({
      options: callbackContract(),
      turn,
      isMutable: () => isMutablePlaygroundTurn(activeOperationId, turn),
      updateAssistant: (updater) => { assistant = updater(assistant); },
      appendTraceEvent: () => undefined,
    });

    handlers.onText("已接收", textDelta("delta-accepted", "已接收"));
    turn.sealed = true;
    handlers.onText("sealed 晚到", textDelta("delta-sealed", "sealed 晚到"));
    turn.sealed = false;
    activeOperationId = "operation-other";
    handlers.onText("stale 晚到", textDelta("delta-stale", "stale 晚到"));

    expect(assistant.content).toBe("已接收");
    expect([...turn.presentedTextEventIds || []]).toEqual(["delta-accepted"]);
  });

  it.each([
    ["human", "HTTP 401", new ApiRequestError("http", "精确 pending 读取被拒绝", { status: 401 }), "人工确认"],
    ["external", "HTTP 403", new ApiRequestError("http", "精确 pending 读取被拒绝", { status: 403 }), "外部执行"],
    ["human", "HTTP 422", new ApiRequestError("http", "精确 pending 读取被拒绝", { status: 422 }), "人工确认"],
    ["external", "decode", new ApiRequestError("decode", "精确 pending 响应无法解码"), "外部执行"],
  ] as const)("%s pending 永久 %s 失败进入可见 reconciling", async (kind, _label, error, subject) => {
    apiMocks.getRun.mockRejectedValue(error);
    const options = callbackContract();
    options.setLastError = vi.fn();
    options.dispatchRun = vi.fn();
    const turn: PlaygroundActiveTurn = {
      sessionId: "session-1",
      agentId: "runtime-agent-1",
      assistantMessageId: "assistant-1",
      operationId: "operation-1",
      runtimeRunId: "run-1",
      controller: new AbortController(),
      completed: false,
      sealed: false,
      stopRequested: false,
      chatSubmitted: true,
    };
    let assistant: ChatMessage = {
      id: "assistant-1", role: "assistant", content: "", createdAt: "", sessionId: "session-1",
    };
    const handlers = createPlaygroundRunStreamHandlers({
      options,
      turn,
      isMutable: () => true,
      updateAssistant: (updater) => { assistant = updater(assistant); },
      appendTraceEvent: () => undefined,
    });

    if (kind === "human") handlers.onUserConfirmRequired(pendingEvent(kind));
    else handlers.onExternalExecutionRequired(pendingEvent(kind));

    await vi.waitFor(() => expect(options.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "reconciling",
      operationId: "operation-1",
    })));
    expect(turn.pendingProjectionError).toContain(subject);
    expect(turn.pendingProjectionError).toContain(error.message);
    expect(options.setLastError).toHaveBeenCalledWith(turn.pendingProjectionError);
    expect(assistant.controlError).toBe(turn.pendingProjectionError);
  });

  it("瞬态 pending 读取失败保持静默，交给下一轮精确 monitor 恢复", async () => {
    apiMocks.getRun.mockRejectedValue(new ApiRequestError(
      "http",
      "暂时不可用",
      { status: 503 },
    ));
    const options = callbackContract();
    options.setLastError = vi.fn();
    options.dispatchRun = vi.fn();
    const turn: PlaygroundActiveTurn = {
      sessionId: "session-1", agentId: "runtime-agent-1", assistantMessageId: "assistant-1",
      operationId: "operation-1", runtimeRunId: "run-1", controller: new AbortController(),
      completed: false, sealed: false, stopRequested: false, chatSubmitted: true,
    };
    const handlers = createPlaygroundRunStreamHandlers({
      options,
      turn,
      isMutable: () => true,
      updateAssistant: () => undefined,
      appendTraceEvent: () => undefined,
    });

    handlers.onUserConfirmRequired(pendingEvent("human"));

    await vi.waitFor(() => expect(apiMocks.getRun).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(options.setLastError).not.toHaveBeenCalled();
    expect(options.dispatchRun).not.toHaveBeenCalled();
    expect(turn.pendingProjectionError).toBeUndefined();
  });

  it("永久错误晚于 turn 失效到达时不得污染下一轮 UI", async () => {
    let rejectRead!: (error: unknown) => void;
    apiMocks.getRun.mockReturnValue(new Promise((_resolve, reject) => { rejectRead = reject; }));
    const options = callbackContract();
    options.setLastError = vi.fn();
    options.dispatchRun = vi.fn();
    let mutable = true;
    const turn: PlaygroundActiveTurn = {
      sessionId: "session-1", agentId: "runtime-agent-1", assistantMessageId: "assistant-1",
      operationId: "operation-1", runtimeRunId: "run-1", controller: new AbortController(),
      completed: false, sealed: false, stopRequested: false, chatSubmitted: true,
    };
    const handlers = createPlaygroundRunStreamHandlers({
      options,
      turn,
      isMutable: () => mutable,
      updateAssistant: () => undefined,
      appendTraceEvent: () => undefined,
    });

    handlers.onUserConfirmRequired(pendingEvent("human"));
    mutable = false;
    rejectRead(new ApiRequestError("decode", "晚到坏响应"));

    await vi.waitFor(() => expect(apiMocks.getRun).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(options.setLastError).not.toHaveBeenCalled();
    expect(options.dispatchRun).not.toHaveBeenCalled();
  });
});
