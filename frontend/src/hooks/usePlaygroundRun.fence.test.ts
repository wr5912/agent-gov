import { describe, expect, it, vi } from "vitest";

import { ApiRequestError } from "../api/request";
import type { AgentScopeStreamHandlers, SubagentHitlProjection } from "../api/runtime";
import type { AgentScopeAgentEvent, AgentScopeReplyEndEvent, ChatMessage } from "../types/runtime";
import {
  mocks,
  options,
  pendingActionFor,
  streamConnection,
  terminalRun,
  toolRequest,
} from "./usePlaygroundRun.test-support";
import { usePlaygroundRun } from "./usePlaygroundRun";

describe("usePlaygroundRun session/run fence", () => {
  it("执行槽未 idle 时不建立 SSE/不 POST，并回滚 optimistic messages", async () => {
    mocks.status.mockResolvedValue({ session_id: "session-1", status: "awaiting_permission" });
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(mocks.connect).not.toHaveBeenCalled();
    expect(mocks.chat).not.toHaveBeenCalled();
    expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({ type: "not_submitted" }));
    const lastUpdater = (runOptions.updateSessionMessages as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[1];
    expect(lastUpdater?.([
      { id: "unrelated", role: "assistant", content: "keep", createdAt: "t" },
    ])).toEqual([{ id: "unrelated", role: "assistant", content: "keep", createdAt: "t" }]);
    expect(runOptions.setInput).toHaveBeenCalledWith(expect.any(Function));
  });

  it("reply 早于 POST receipt 到达时仍先绑定 run，终态后阻断旧 stream callback", async () => {
    const order: string[] = [];
    const end = {
      id: "end",
      created_at: "t",
      metadata: {},
      type: "REPLY_END",
      reply_id: "reply-1",
      session_id: "session-1",
    } satisfies AgentScopeReplyEndEvent;
    let handlers: AgentScopeStreamHandlers | undefined;
    const connection = streamConnection(order, Promise.resolve(end));
    mocks.connect.mockImplementation(async (...args: unknown[]) => {
      order.push("connect");
      handlers = args[3] as AgentScopeStreamHandlers;
      return connection;
    });
    let resolveReceipt!: (value: { status: string; session_id: string; runId: string }) => void;
    mocks.chat.mockImplementation(() => {
      order.push("post");
      return new Promise((resolve) => { resolveReceipt = resolve; });
    });
    mocks.getRun.mockImplementation(async () => {
      order.push("getRun");
      return terminalRun();
    });
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    const pending = controller.sendMessage();
    await vi.waitFor(() => expect(mocks.chat).toHaveBeenCalledTimes(1));
    handlers?.onText?.("before", {
      id: "delta-before",
      type: "TEXT_BLOCK_DELTA",
      reply_id: "reply-1",
      block_id: "block-1",
      delta: "before",
      created_at: "t",
      metadata: {},
    });
    resolveReceipt({ status: "started", session_id: "session-1", runId: "run-1" });
    await pending;

    expect(order.indexOf("setRunId")).toBeGreaterThan(order.indexOf("post"));
    expect(order.indexOf("getRun")).toBeGreaterThan(order.indexOf("setRunId"));
    expect(mocks.connect.mock.calls[0][5]).toBeUndefined();
    const updatesAfterTerminal = (runOptions.updateSessionMessages as ReturnType<typeof vi.fn>).mock.calls.length;
    handlers?.onText?.("late", {
      id: "delta-late",
      type: "TEXT_BLOCK_DELTA",
      reply_id: "reply-1",
      block_id: "block-1",
      delta: "late",
      created_at: "t",
      metadata: {},
    });
    expect(runOptions.updateSessionMessages).toHaveBeenCalledTimes(updatesAfterTerminal);
    const terminalActions = (runOptions.dispatchRun as ReturnType<typeof vi.fn>).mock.calls
      .map(([action]) => action)
      .filter((action) => action.type === "terminal");
    expect(terminalActions).toHaveLength(1);
  });

  it("SSE 建连长期无响应也不阻塞 POST 与 exact run 终态收口", async () => {
    mocks.connect.mockReturnValue(new Promise(() => undefined));
    mocks.chat.mockResolvedValue({ status: "started", session_id: "session-1", runId: "run-1" });
    mocks.getRun.mockResolvedValue(terminalRun());
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(mocks.connect).toHaveBeenCalledTimes(1);
    expect(mocks.chat).toHaveBeenCalledTimes(1);
    expect(runOptions.dispatchRun).toHaveBeenCalledWith({
      type: "terminal",
      operationId: expect.stringMatching(/^runtime_/),
      outcome: "succeeded",
    });
  });

  it("SSE 永久不产生 REPLY_END 时仍由 exact run monitor 解锁", async () => {
    const connection = streamConnection([]);
    mocks.chat.mockResolvedValue({ status: "started", session_id: "session-1", runId: "run-1" });
    mocks.getRun.mockResolvedValue(terminalRun());
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(connection.armReply).toHaveBeenCalledTimes(1);
    expect(connection.close).toHaveBeenCalledTimes(1);
    expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "terminal",
      outcome: "succeeded",
    }));
  });

  it("local HITL 的 SSE 关闭后先单飞重连再向同一 run 提交确认", async () => {
    const request = toolRequest();
    const action = await pendingActionFor("action-local", "session-1", request.toolCalls[0]);
    let resolveFirstClosed!: () => void;
    const firstClosed = new Promise<void>((resolve) => { resolveFirstClosed = resolve; });
    const handlers: AgentScopeStreamHandlers[] = [];
    const connections = [
      {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: firstClosed,
      },
      {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: new Promise<void>(() => undefined),
      },
    ];
    mocks.connect.mockImplementation(async (...args: unknown[]) => {
      handlers.push(args[3] as AgentScopeStreamHandlers);
      return connections[Math.min(handlers.length - 1, connections.length - 1)];
    });
    mocks.chat.mockResolvedValue({ status: "started", session_id: "session-1", runId: "run-1" });
    mocks.getRun.mockResolvedValue(terminalRun({ status: "waiting_human" }));
    mocks.getPendingActions.mockResolvedValue([action]);
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);
    const cleanup = mocks.effects[1]();

    const sending = controller.sendMessage();
    await vi.waitFor(() => expect(mocks.chat).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(handlers).toHaveLength(1));
    const event = {
      id: request.requestId,
      type: "REQUIRE_USER_CONFIRM",
      reply_id: request.replyId,
      tool_calls: request.toolCalls,
      created_at: "2026-09-10T00:00:00Z",
      metadata: {},
    } as AgentScopeAgentEvent;
    handlers[0].onUserConfirmRequired?.(event);
    await vi.waitFor(() => expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "awaiting_input",
    })));

    resolveFirstClosed();
    await Promise.resolve();
    await controller.submitUserConfirm(request, "allow_once");

    expect(mocks.connect).toHaveBeenCalledTimes(2);
    expect(connections[1].setRunId).toHaveBeenCalledWith("run-1");
    expect(connections[1].armReply).toHaveBeenCalledTimes(1);
    expect(mocks.chat).toHaveBeenCalledTimes(2);
    expect(mocks.chat.mock.calls[1][4]).toMatchObject({ expectedRunId: "run-1" });
    cleanup?.();
    await sending;
  });

  it("断流重放相同事件时正文不重复且 Trace 始终绑定 exact run", async () => {
    let terminal = false;
    let closeFirstStream!: () => void;
    const firstClosed = new Promise<void>((resolve) => { closeFirstStream = resolve; });
    const handlers: AgentScopeStreamHandlers[] = [];
    const connections = [
      {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: firstClosed,
      },
      {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: new Promise<void>(() => undefined),
      },
    ];
    mocks.connect.mockImplementation(async (...args: unknown[]) => {
      if (args[2] !== "session-replay") throw new Error("unexpected session");
      handlers.push(args[3] as AgentScopeStreamHandlers);
      return connections[Math.min(handlers.length - 1, 1)];
    });
    mocks.chat.mockResolvedValue({ status: "started", session_id: "session-replay", runId: "run-replay" });
    mocks.getRun.mockImplementation(async () => terminal
      ? terminalRun({ run_id: "run-replay", session_id: "session-replay" })
      : terminalRun({ run_id: "run-replay", session_id: "session-replay", status: "running" }));
    mocks.status.mockImplementation(async (_config: unknown, _agent: string, sessionId: string) => ({
      session_id: sessionId,
      status: "idle",
    }));
    mocks.messages.mockResolvedValue({
      messages: [{
        id: "reply-1",
        name: "assistant",
        role: "assistant",
        content: [{ type: "text", text: "AB" }],
        metadata: {},
        created_at: "2026-09-10T00:00:01Z",
        finished_reason: "completed",
      }],
      is_running: false,
      has_more: false,
    });
    let rendered: ChatMessage[] = [];
    const runOptions = options({ phase: "idle" }, {
      activeSessionId: "session-replay",
      updateSessionMessages: vi.fn((_sessionId: string, updater: (current: ChatMessage[]) => ChatMessage[]) => {
        rendered = updater(rendered);
      }),
    });
    const controller = usePlaygroundRun(runOptions);
    const cleanup = mocks.effects[1]();
    const sending = controller.sendMessage();
    await vi.waitFor(() => expect(handlers).toHaveLength(1));
    await vi.waitFor(() => expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({
      type: "run_handle",
      runId: "run-replay",
    })));

    const deltaA = {
      id: "delta-a", type: "TEXT_BLOCK_DELTA", delta: "A", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent;
    const traceA = {
      event_id: "delta-a",
      kind: "text",
      message_index: 0,
      run_id: "pending",
      scope: "main" as const,
      sequence: 1,
      source_event: "TEXT_BLOCK_DELTA",
      payload: deltaA,
    };
    handlers[0].onTraceEvent?.(traceA);
    handlers[0].onText?.("A", deltaA);
    closeFirstStream();
    await vi.waitFor(() => expect(handlers).toHaveLength(2), { timeout: 2_000 });

    handlers[1].onTraceEvent?.(traceA);
    handlers[1].onText?.("A", deltaA);
    const deltaB = {
      id: "delta-b", type: "TEXT_BLOCK_DELTA", delta: "B", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent;
    handlers[1].onTraceEvent?.({ ...traceA, event_id: "delta-b", sequence: 2, payload: deltaB });
    handlers[1].onText?.("B", deltaB);

    const assistant = rendered.find((message) => message.role === "assistant");
    expect(assistant?.content).toBe("AB");
    expect(assistant?.events).toHaveLength(2);
    expect(assistant?.events?.every((event) => (
      (event.data as { run_id?: string }).run_id === "run-replay"
    ))).toBe(true);
    terminal = true;
    await sending;
    const canonical = rendered.find((message) => message.id === "reply-1");
    expect(canonical?.content).toBe("AB");
    expect(canonical?.events?.map((event) => event.id)).toEqual(["delta-a", "delta-b"]);
    expect(runOptions.setActiveTraceMessageId).toHaveBeenLastCalledWith("reply-1");
    cleanup?.();
  });

  it("detached SSE 按 reply 分区并只消费目标 canonical 正文前缀", async () => {
    const handlers: AgentScopeStreamHandlers[] = [];
    mocks.connect.mockImplementation(async (...args: unknown[]) => {
      handlers.push(args[3] as AgentScopeStreamHandlers);
      return {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: new Promise<void>(() => undefined),
      };
    });
    mocks.getRun.mockResolvedValue(terminalRun({ status: "waiting_human" }));
    mocks.status.mockResolvedValue({ session_id: "session-1", status: "awaiting_permission" });
    let rendered: ChatMessage[] = [
      {
        id: "reply-old",
        role: "assistant",
        content: "Old",
        createdAt: "2026-09-10T00:00:00Z",
        sessionId: "session-1",
        runId: "run-1",
      },
      {
        id: "reply-1",
        role: "assistant",
        content: "Hello",
        createdAt: "2026-09-10T00:00:01Z",
        sessionId: "session-1",
        runId: "run-1",
        partial: true,
      },
    ];
    const runOptions = options({
      phase: "awaiting_input",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    }, {
      activeMessages: rendered,
      updateSessionMessages: vi.fn((_sessionId: string, updater: (current: ChatMessage[]) => ChatMessage[]) => {
        rendered = updater(rendered);
      }),
    });
    usePlaygroundRun(runOptions);
    const unmount = mocks.effects[1]();
    const detach = mocks.effects[0]();
    await vi.waitFor(() => expect(handlers).toHaveLength(1));

    handlers[0].onReplyStart?.({
      id: "reply-foreign-start", type: "REPLY_START", reply_id: "reply-foreign", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onText?.("Foreign", {
      id: "reply-foreign-delta", type: "TEXT_BLOCK_DELTA", reply_id: "reply-foreign", delta: "Foreign", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onReplyStart?.({
      id: "reply-old-start", type: "REPLY_START", reply_id: "reply-old", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onText?.("Old", {
      id: "reply-old-delta", type: "TEXT_BLOCK_DELTA", reply_id: "reply-old", delta: "Old", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onReplyStart?.({
      id: "reply-start", type: "REPLY_START", reply_id: "reply-1", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onText?.("Hel", {
      id: "delta-1", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "Hel", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onText?.("lo", {
      id: "delta-2", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "lo", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onText?.("!", {
      id: "delta-3", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "!", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onReplyStart?.({
      id: "reply-new-start", type: "REPLY_START", reply_id: "reply-new", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);
    handlers[0].onText?.("New", {
      id: "reply-new-delta", type: "TEXT_BLOCK_DELTA", reply_id: "reply-new", delta: "New", created_at: "t", metadata: {},
    } as AgentScopeAgentEvent);

    expect(rendered.find((message) => message.id === "reply-old")?.content).toBe("Old");
    expect(rendered.find((message) => message.id === "reply-foreign")).toBeUndefined();
    expect(rendered.find((message) => message.id === "reply-1")?.content).toBe("Hello!");
    expect(rendered.find((message) => message.id === "reply-new")).toMatchObject({
      content: "New",
      runId: "run-1",
      partial: true,
    });
    expect(runOptions.setActiveTraceMessageId).toHaveBeenLastCalledWith("reply-new");
    expect(runOptions.setLastError).not.toHaveBeenCalledWith(expect.stringContaining("replay 正文"));
    detach?.();
    unmount?.();
  });

  it("admission 409 且 exact operation 404 时回滚未提交 turn 并刷新真实 active run", async () => {
    streamConnection([]);
    mocks.chat.mockRejectedValue(new ApiRequestError(
      "http",
      "active run conflict",
      { status: 409, errorCode: "RUNTIMESTATECONFLICT" },
    ));
    mocks.getRunByOperation.mockRejectedValue(new ApiRequestError(
      "http",
      "not found",
      { status: 404, errorCode: "RUNTIMEOBJECTNOTFOUND" },
    ));
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({ type: "not_submitted" }));
    expect(runOptions.dispatchRun).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal" }));
    expect(runOptions.setInput).toHaveBeenCalledWith(expect.any(Function));
    expect(runOptions.refresh).toHaveBeenCalled();
    expect(runOptions.setLastError).toHaveBeenLastCalledWith(expect.stringContaining("本次消息未提交"));
  });

  it.each([401, 404, 422])(
    "初始 POST 明确返回 HTTP %s 且 operation 不存在时立即解除锁定",
    async (status) => {
      streamConnection([]);
      mocks.chat.mockRejectedValue(new ApiRequestError("http", "request rejected", { status }));
      mocks.getRunByOperation.mockRejectedValue(new ApiRequestError(
        "http",
        "not found",
        { status: 404, errorCode: "RUNTIMEOBJECTNOTFOUND" },
      ));
      const runOptions = options({ phase: "idle" });
      const controller = usePlaygroundRun(runOptions);

      await controller.sendMessage();

      expect(mocks.chat).toHaveBeenCalledTimes(1);
      expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({ type: "not_submitted" }));
      expect(runOptions.dispatchRun).not.toHaveBeenCalledWith(expect.objectContaining({ type: "run_handle" }));
      expect(runOptions.refresh).toHaveBeenCalledTimes(1);
      expect(runOptions.setLastError).toHaveBeenLastCalledWith(expect.stringContaining(`HTTP ${status}`));
    },
  );

  it("网络不确定后幂等重试遇 409 且 operation 仍不存在时解除锁定", async () => {
    streamConnection([]);
    mocks.chat
      .mockRejectedValueOnce(new ApiRequestError("network", "connection reset"))
      .mockRejectedValueOnce(new ApiRequestError(
        "http",
        "active run conflict",
        { status: 409, errorCode: "RUNTIMESTATECONFLICT" },
      ));
    mocks.getRunByOperation.mockRejectedValue(new ApiRequestError(
      "http",
      "not found",
      { status: 404, errorCode: "RUNTIMEOBJECTNOTFOUND" },
    ));
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);

    await controller.sendMessage();

    expect(mocks.chat).toHaveBeenCalledTimes(2);
    expect(mocks.getRunByOperation).toHaveBeenCalledTimes(2);
    expect(runOptions.dispatchRun).toHaveBeenCalledWith(expect.objectContaining({ type: "not_submitted" }));
    expect(runOptions.dispatchRun).not.toHaveBeenCalledWith(expect.objectContaining({ type: "run_handle" }));
    expect(runOptions.refresh).toHaveBeenCalledTimes(1);
    expect(runOptions.setLastError).toHaveBeenLastCalledWith(expect.stringContaining("已有其他活动运行"));
  });

  it("Runtime idle 时 detached AgentGov run 仍独立监控并收口", async () => {
    mocks.connect.mockReturnValue(new Promise(() => undefined));
    mocks.getRun.mockResolvedValue(terminalRun());
    mocks.status.mockResolvedValue({ session_id: "session-1", status: "idle" });
    const runOptions = options({
      phase: "running",
      source: "detached",
      operationId: "detached:session-1:run-1",
      sessionId: "session-1",
      runId: "run-1",
    });
    usePlaygroundRun(runOptions);

    const cleanup = mocks.effects[1]();
    mocks.effects[0]();
    await vi.waitFor(() => expect(runOptions.dispatchRun).toHaveBeenCalledWith({
      type: "terminal",
      operationId: "detached:session-1:run-1",
      outcome: "succeeded",
    }));
    cleanup?.();
  });

  it("detached 多 worker pending ledger 缺一张卡时保持单飞并重连补齐", async () => {
    const firstTool = toolRequest().toolCalls[0];
    const secondTool = { ...firstTool, id: "tool-2", input: '{"path":"report.md"}' };
    const firstAction = {
      ...await pendingActionFor("action-1", "worker-1", firstTool),
      run_id: "run-ledger",
    };
    const secondAction = {
      ...await pendingActionFor("action-2", "worker-2", secondTool),
      run_id: "run-ledger",
    };
    const handlers: AgentScopeStreamHandlers[] = [];
    const connections: Array<ReturnType<typeof streamConnection>> = [];
    let releaseFirstConnect!: () => void;
    const firstConnectGate = new Promise<void>((resolve) => { releaseFirstConnect = resolve; });
    let activeConnects = 0;
    let maxConcurrentConnects = 0;
    mocks.connect.mockImplementation(async (...args: unknown[]) => {
      if (args[2] !== "session-ledger") return {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: new Promise<void>(() => undefined),
      };
      const streamHandlers = args[3] as AgentScopeStreamHandlers;
      handlers.push(streamHandlers);
      activeConnects += 1;
      maxConcurrentConnects = Math.max(maxConcurrentConnects, activeConnects);
      if (handlers.length === 1) await firstConnectGate;
      const connection = {
        setRunId: vi.fn(),
        armReply: vi.fn(() => new Promise<AgentScopeAgentEvent>(() => undefined)),
        close: vi.fn(),
        closed: new Promise<void>(() => undefined),
      };
      connections.push(connection);
      activeConnects -= 1;
      return connection;
    });
    mocks.getRun.mockResolvedValue(terminalRun({
      run_id: "run-ledger",
      session_id: "session-ledger",
      status: "waiting_human",
    }));
    mocks.getPendingActions.mockResolvedValue([firstAction, secondAction]);
    mocks.status.mockImplementation(async (_config: unknown, _agent: string, sessionId: string) => ({
      session_id: sessionId,
      status: "awaiting_permission",
    }));
    let rendered: ChatMessage[] = [{
      id: "assistant-1",
      role: "assistant",
      content: "",
      createdAt: "2026-09-10T00:00:00Z",
      sessionId: "session-ledger",
      runId: "run-ledger",
    }];
    const runOptions = options({
      phase: "running",
      source: "detached",
      operationId: "detached:session-ledger:run-ledger",
      sessionId: "session-ledger",
      runId: "run-ledger",
    }, {
      activeSessionId: "session-ledger",
      activeMessages: rendered,
      updateSessionMessages: vi.fn((_sessionId: string, updater: (current: ChatMessage[]) => ChatMessage[]) => {
        rendered = updater(rendered);
      }),
    });
    usePlaygroundRun(runOptions);
    const unmount = mocks.effects[1]();
    const detach = mocks.effects[0]();
    await vi.waitFor(() => expect(handlers).toHaveLength(1));
    const firstEvent = {
      id: "event-1",
      type: "REQUIRE_USER_CONFIRM",
      reply_id: "reply-1",
      tool_calls: [firstTool],
      created_at: "2026-09-10T00:00:00Z",
      metadata: {},
    } as AgentScopeAgentEvent;
    await vi.waitFor(() => expect(mocks.getPendingActions).toHaveBeenCalled());
    expect(maxConcurrentConnects).toBe(1);
    releaseFirstConnect();
    await vi.waitFor(() => expect(connections).toHaveLength(1));
    handlers[0].onUserConfirmRequired?.(firstEvent, {
      worker_session_id: "worker-1",
      worker_agent_id: "worker-agent-1",
      worker_agent_name: "worker-1",
      reply_id: "reply-1",
      event_type: "require_user_confirm",
      event: firstEvent,
      created_at: "2026-09-10T00:00:00Z",
    } as SubagentHitlProjection);
    await vi.waitFor(() => expect(
      rendered[0].userConfirmRequests?.some((request) => request.workerSessionId === "worker-1"),
    ).toBe(true));
    await vi.waitFor(() => expect(connections.length).toBeGreaterThanOrEqual(2), { timeout: 7_000 });
    expect(maxConcurrentConnects).toBe(1);
    expect(connections[0].close).toHaveBeenCalledTimes(1);

    const secondEvent = {
      ...firstEvent,
      id: "event-2",
      tool_calls: [secondTool],
    } as AgentScopeAgentEvent;
    handlers.at(-1)?.onUserConfirmRequired?.(secondEvent, {
      worker_session_id: "worker-2",
      worker_agent_id: "worker-agent-2",
      worker_agent_name: "worker-2",
      reply_id: "reply-1",
      event_type: "require_user_confirm",
      event: secondEvent,
      created_at: "2026-09-10T00:00:01Z",
    } as SubagentHitlProjection);
    await vi.waitFor(() => expect(
      new Set(rendered[0].userConfirmRequests?.map((request) => request.workerSessionId)),
    ).toEqual(new Set(["worker-1", "worker-2"])));
    detach?.();
    unmount?.();
  }, 12_000);

  it("unmount abort 会阻止延迟的 readiness 继续发送", async () => {
    mocks.status.mockImplementation((_config: unknown, _agent: string, _session: string, signal: AbortSignal) => (
      new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      })
    ));
    const runOptions = options({ phase: "idle" });
    const controller = usePlaygroundRun(runOptions);
    const cleanup = mocks.effects[1]();

    const pending = controller.sendMessage();
    await vi.waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(1));
    cleanup?.();
    await pending;

    expect(mocks.connect).not.toHaveBeenCalled();
    expect(mocks.chat).not.toHaveBeenCalled();
  });
});
