import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "../api/request";
import type {
  AgentScopeAgentEvent,
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmRequest,
} from "../types/runtime";
import type { PlaygroundRunState } from "../playgroundRunState";

const mocks = vi.hoisted(() => ({
  effects: [] as Array<() => void | (() => void)>,
  connect: vi.fn(),
  createSession: vi.fn(),
  messages: vi.fn(),
  status: vi.fn(),
  interrupt: vi.fn(),
  chat: vi.fn(),
  getRun: vi.fn(),
  getRunByOperation: vi.fn(),
  getPendingActions: vi.fn(),
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
  interruptRuntimeSession: mocks.interrupt,
  startRuntimeChat: mocks.chat,
}));

vi.mock("../api/feedback", () => ({
  getAgentRun: mocks.getRun,
  getAgentRunByClientOperation: mocks.getRunByOperation,
  getAgentRunPendingActions: mocks.getPendingActions,
}));

import { usePlaygroundRun, type PlaygroundRunOptions } from "./usePlaygroundRun";

beforeEach(() => {
  mocks.effects.length = 0;
  vi.clearAllMocks();
  mocks.messages.mockResolvedValue({ messages: [], is_running: false, has_more: false });
  mocks.status.mockResolvedValue({ session_id: "session-1", status: "awaiting_permission" });
  mocks.interrupt.mockResolvedValue({ session_id: "session-1" });
  mocks.getPendingActions.mockResolvedValue([]);
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

function streamConnection(order: string[]) {
  let resolveReply: ((event: AgentScopeAgentEvent) => void) | undefined;
  const reply = new Promise<AgentScopeAgentEvent>((resolve) => {
    resolveReply = resolve;
  });
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
  return { connection, resolveReply };
}

function options(
  runState: PlaygroundRunState,
  overrides: Partial<PlaygroundRunOptions> = {},
) {
  const messages: ChatMessage[] = overrides.activeMessages || [];
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
      updater(messages);
    }),
    updateUserConfirmRequest: vi.fn(),
    updateExternalExecutionRequest: vi.fn(),
    cancelUserConfirmForMessage: vi.fn(),
    cancelExternalExecutionForMessage: vi.fn(),
    calibrateTrace: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as PlaygroundRunOptions;
}

describe("usePlaygroundRun AgentScope recovery", () => {
  it("rebuilds a parked HITL turn and connects, binds, then arms SSE before same-run continuation", async () => {
    const order: string[] = [];
    streamConnection(order);
    mocks.status.mockImplementation(async () => {
      order.push("status");
      return { session_id: "session-1", status: "awaiting_permission" };
    });
    mocks.chat.mockImplementation(async () => {
      order.push("post");
      return { status: "started", session_id: "session-1", runId: "run-1" };
    });
    const request = toolRequest();
    const runState: PlaygroundRunState = {
      phase: "awaiting_input",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    };
    const activeMessages: ChatMessage[] = [{
      id: "assistant-1",
      role: "assistant",
      content: "",
      createdAt: "2026-09-10T00:00:00Z",
      sessionId: "session-1",
      runId: "run-1",
      userConfirmRequests: [request],
    }];
    const runOptions = options(runState, { activeMessages });
    const controller = usePlaygroundRun(runOptions);

    mocks.effects[0](); // React's detached-run connection effect.
    await controller.submitUserConfirm(request, "allow_once");

    expect(order.slice(0, 5)).toEqual(["connect", "setRunId", "armReply", "status", "post"]);
    expect(mocks.chat).toHaveBeenCalledWith(
      runOptions.clientConfig,
      "runtime-1",
      "session-1",
      expect.objectContaining({ type: "USER_CONFIRM_RESULT", reply_id: "reply-1" }),
      expect.objectContaining({
        expectedRunId: "run-1",
      }),
      expect.any(AbortSignal),
    );
    expect(mocks.chat.mock.calls[0][4]).toMatchObject({
      clientOperationId: "detached:session-1:run-1",
      expectedRunId: "run-1",
    });
    expect(runOptions.updateUserConfirmRequest).toHaveBeenCalledWith(
      "confirm-1",
      expect.objectContaining({ status: "resolved", decision: "allow_once" }),
    );
  });

  it("recovers a lost initial POST response only through its exact client operation", async () => {
    const order: string[] = [];
    streamConnection(order);
    mocks.chat.mockRejectedValueOnce(new ApiRequestError("network", "connection reset"));
    mocks.getRunByOperation.mockImplementation(async (
      _config: unknown,
      sessionId: string,
      operationId: string,
    ) => ({
      run_id: "run-recovered",
      session_id: sessionId,
      runtime_agent_id: "runtime-1",
      status: "succeeded",
      client_operation_id: operationId,
    }));
    mocks.getRun.mockResolvedValue({
      run_id: "run-recovered",
      session_id: "session-1",
      runtime_agent_id: "runtime-1",
      status: "succeeded",
      metadata: {},
      reply_ids: [],
    });
    mocks.status.mockResolvedValue({ session_id: "session-1", status: "idle" });
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    const context = mocks.chat.mock.calls[0][4] as { clientOperationId: string };
    expect(context.clientOperationId).toMatch(/^runtime_/);
    expect(mocks.getRunByOperation).toHaveBeenCalledWith(
      runOptions.clientConfig,
      "session-1",
      context.clientOperationId,
      expect.any(AbortSignal),
    );
    expect((runOptions.dispatchRun as ReturnType<typeof vi.fn>).mock.calls).toContainEqual([{
      type: "run_handle",
      operationId: context.clientOperationId,
      sessionId: "session-1",
      runId: "run-recovered",
    }]);
  });

  it("submits recovered external execution output into the same detached run", async () => {
    const order: string[] = [];
    streamConnection(order);
    mocks.status.mockImplementation(async () => {
      order.push("status");
      return { session_id: "session-1", status: "awaiting_external_result" };
    });
    mocks.chat.mockImplementation(async () => {
      order.push("post");
      return { status: "started", session_id: "session-1", runId: "run-1" };
    });
    const request = externalRequest();
    const runState: PlaygroundRunState = {
      phase: "awaiting_input",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    };
    const activeMessages: ChatMessage[] = [{
      id: "assistant-1",
      role: "assistant",
      content: "",
      createdAt: "2026-09-10T00:00:00Z",
      sessionId: "session-1",
      runId: "run-1",
      externalExecutionRequests: [request],
    }];
    const runOptions = options(runState, { activeMessages });
    const controller = usePlaygroundRun(runOptions);

    mocks.effects[0]();
    await controller.submitExternalExecution(request, "success", {
      "tool-external": "verified output",
    });

    expect(order.slice(0, 5)).toEqual(["connect", "setRunId", "armReply", "status", "post"]);
    expect(mocks.chat).toHaveBeenCalledWith(
      runOptions.clientConfig,
      "runtime-1",
      "session-1",
      {
        type: "EXTERNAL_EXECUTION_RESULT",
        reply_id: "reply-1",
        execution_results: [{
          type: "tool_result",
          id: "tool-external",
          name: "ExternalLookup",
          output: "verified output",
          state: "success",
        }],
      },
      expect.objectContaining({
        clientOperationId: "detached:session-1:run-1",
        expectedRunId: "run-1",
      }),
      expect.any(AbortSignal),
    );
    expect(runOptions.updateExternalExecutionRequest).toHaveBeenCalledWith(
      "external-1",
      expect.objectContaining({ status: "resolved", resultState: "success" }),
    );
  });

  it("rotates a session idempotency key only for RUNTIMERESTARTREQUIRED", async () => {
    mocks.createSession
      .mockRejectedValueOnce(new ApiRequestError(
        "http",
        "restart required",
        { status: 503, errorCode: "RUNTIMERESTARTREQUIRED" },
      ))
      .mockRejectedValueOnce(new ApiRequestError("network", "connection reset"))
      .mockRejectedValueOnce(new ApiRequestError("network", "connection reset"));
    const runOptions = options({ phase: "idle" }, {
      activeSessionId: undefined,
      activeMessagesLoaded: false,
    });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();
    await controller.sendMessage();
    await controller.sendMessage();

    const keys = mocks.createSession.mock.calls.map((call) => call[2] as string);
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[1]).toBe(keys[2]);
  });
});
