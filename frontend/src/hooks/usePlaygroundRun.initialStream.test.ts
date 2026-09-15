import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "../api/request";
import type { PlaygroundRunOptions } from "../playgroundRunContract";
import type { PlaygroundRunState } from "../playgroundRunState";
import type { ChatMessage } from "../types/runtime";

const mocks = vi.hoisted(() => ({
  connect: vi.fn(),
  createSession: vi.fn(),
  messages: vi.fn(),
  status: vi.fn(),
  chat: vi.fn(),
  getRun: vi.fn(),
  getRunByInputIdentity: vi.fn(),
  getRunByNativeInputIdentity: vi.fn(),
  getPendingActions: vi.fn(),
  getAllRuns: vi.fn(),
  cancelRun: vi.fn(),
}));

vi.mock("react", () => ({
  useEffect: () => undefined,
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
  getAgentRunByInputIdentity: mocks.getRunByInputIdentity,
  getAgentRunByNativeInputIdentity: mocks.getRunByNativeInputIdentity,
  getAgentRunPendingActions: mocks.getPendingActions,
  getAllSessionAgentRuns: mocks.getAllRuns,
}));

import { usePlaygroundRun } from "./usePlaygroundRun";

function terminalRun() {
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
    created_at: "2026-09-15T00:00:00Z",
    updated_at: "2026-09-15T00:00:01Z",
    reply_ids: [],
  };
}

function streamConnection(order: string[]) {
  return {
    setRunId: vi.fn(() => order.push("set_run")),
    armReply: vi.fn(() => new Promise(() => undefined)),
    close: vi.fn(() => order.push("close_stream")),
    closed: new Promise<void>(() => undefined),
  };
}

function runOptions(overrides: Partial<PlaygroundRunOptions> = {}): PlaygroundRunOptions {
  let messages: ChatMessage[] = [];
  const state: PlaygroundRunState = { phase: "idle" };
  return {
    clientConfig: { apiBase: "http://runtime.test", apiKey: "" },
    input: "hello",
    runState: state,
    dispatchRun: vi.fn(),
    activeSessionId: "session-1",
    activeMessages: messages,
    activeMessagesLoaded: true,
    selectedBusinessAgentId: "business-1",
    runtimeAgentId: "runtime-1",
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
    updateSessionMessages: vi.fn((_sessionId, updater) => { messages = updater(messages); }),
    updateUserConfirmRequest: vi.fn(),
    updateExternalExecutionRequest: vi.fn(),
    cancelUserConfirmForMessage: vi.fn(),
    cancelExternalExecutionForMessage: vi.fn(),
    calibrateTrace: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.messages.mockResolvedValue({ messages: [], is_running: false, has_more: false });
  mocks.status.mockResolvedValue({ session_id: "session-1", status: "idle" });
  mocks.getRun.mockResolvedValue(terminalRun());
  mocks.getPendingActions.mockResolvedValue([]);
  mocks.getAllRuns.mockResolvedValue([terminalRun()]);
  mocks.chat.mockResolvedValue({ status: "started", session_id: "session-1", runId: "run-1" });
});

describe("Playground 初始 SSE readiness 门", () => {
  it("connect 未确认 readiness 时绝不 POST；ready 后才提交并绑定精确 run", async () => {
    const order: string[] = [];
    const connection = streamConnection(order);
    let resolveReady!: (value: typeof connection) => void;
    mocks.connect.mockImplementation(() => new Promise((resolve) => {
      order.push("connect_requested");
      resolveReady = resolve;
    }));
    mocks.chat.mockImplementation(async () => {
      order.push("chat_posted");
      return { status: "started", session_id: "session-1", runId: "run-1" };
    });
    const options = runOptions();
    const controller = usePlaygroundRun(options);

    const sending = controller.sendMessage();
    await vi.waitFor(() => expect(mocks.connect).toHaveBeenCalledTimes(1));
    expect(mocks.chat).not.toHaveBeenCalled();

    order.push("stream_ready");
    resolveReady(connection);
    await sending;

    expect(mocks.chat).toHaveBeenCalledTimes(1);
    expect(order.indexOf("chat_posted")).toBeGreaterThan(order.indexOf("stream_ready"));
    expect(order.indexOf("set_run")).toBeGreaterThan(order.indexOf("chat_posted"));
    expect(options.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "terminal",
      outcome: "succeeded",
    }));
  });

  it("readiness 建连永久失败时回滚 optimistic turn，且不产生 chat 副作用", async () => {
    mocks.connect.mockRejectedValue(new Error("readiness contract missing"));
    const options = runOptions();
    const controller = usePlaygroundRun(options);

    await controller.sendMessage();

    expect(mocks.chat).not.toHaveBeenCalled();
    expect(options.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({ type: "not_submitted" }));
    expect(options.setLastError).toHaveBeenCalledWith("readiness contract missing");
    const restoreInput = (options.setInput as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0];
    expect(restoreInput("")).toBe("hello");
  });

  it("用户在慢 readiness 期间停止时即使 connect 晚到，也不得越过 fence POST", async () => {
    const order: string[] = [];
    const connection = streamConnection(order);
    let resolveReady!: (value: typeof connection) => void;
    mocks.connect.mockImplementation(() => new Promise((resolve) => { resolveReady = resolve; }));
    const options = runOptions();
    const controller = usePlaygroundRun(options);

    const sending = controller.sendMessage();
    await vi.waitFor(() => expect(mocks.connect).toHaveBeenCalledTimes(1));
    controller.stopStream();
    resolveReady(connection);
    await sending;

    expect(mocks.chat).not.toHaveBeenCalled();
    expect(connection.close).toHaveBeenCalledTimes(1);
    expect(options.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({ type: "not_submitted" }));
  });
});

describe("Playground waiting pending monitor 错误投影", () => {
  it("pending-actions 永久 403 穿透嵌套 catch，进入页面与运行状态后仍按精确 run 重试", async () => {
    const connection = streamConnection([]);
    mocks.connect.mockResolvedValue(connection);
    let runReads = 0;
    let terminal = false;
    mocks.getRun.mockImplementation(async () => {
      runReads += 1;
      return terminal ? terminalRun() : { ...terminalRun(), status: "waiting_human" };
    });
    mocks.getPendingActions.mockRejectedValue(
      new ApiRequestError("http", "pending actions forbidden", { status: 403 }),
    );
    const options = runOptions();
    let visibleError: string | undefined;
    options.setLastError = vi.fn((next) => {
      visibleError = typeof next === "function" ? next(visibleError) : next;
    });
    const controller = usePlaygroundRun(options);

    const sending = controller.sendMessage();
    await vi.waitFor(() => expect(options.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "reconciling",
      message: expect.stringContaining("pending actions forbidden"),
    })));
    expect(options.setLastError).toHaveBeenCalledWith(expect.stringContaining("精确状态读取失败"));
    expect(visibleError).toContain("pending actions forbidden");

    // 下一次精确 run GET 成功但 pending snapshot 仍未成功时，不得提前清除永久错误。
    await new Promise((resolve) => globalThis.setTimeout(resolve, 1_100));
    expect(runReads).toBeGreaterThanOrEqual(3);
    expect(visibleError).toContain("pending actions forbidden");

    terminal = true;
    await sending;
    expect(options.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "terminal",
      outcome: "succeeded",
    }));
  });
});
