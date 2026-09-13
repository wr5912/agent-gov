import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AgentScopeEventReducer,
  AgentScopeReplyCompletion,
  connectAgentScopeSessionStream,
  consumeAgentScopeSse,
  extractSseFrames,
  subagentHitlProjection,
  subagentHitlResolution,
} from "./agentScopeStream";
import { getAgentRunByClientOperation } from "./feedback";
import { ApiRequestError } from "./request";
import {
  createRuntimeSession,
  getSessions,
  startRuntimeChat,
} from "./runtime";
import type { AgentScopeAgentEvent } from "../types/runtime";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

type AgentScopeEventType = AgentScopeAgentEvent["type"];
type AgentScopeEventByType<T extends AgentScopeEventType> = Extract<AgentScopeAgentEvent, { type: T }>;

function nativeEvent<T extends AgentScopeEventType>(
  overrides: { type: T } & Partial<AgentScopeEventByType<T>>,
): AgentScopeEventByType<T> {
  const base = {
    id: "event-1",
    created_at: "2026-09-09T00:00:00Z",
    metadata: {},
  };
  const defaults = overrides.type === "CUSTOM"
    ? { name: "test_event" }
    : overrides.type === "REPLY_START"
      ? { name: "assistant", reply_id: "reply-1", session_id: "session-1" }
      : overrides.type === "REPLY_END"
        ? { reply_id: "reply-1", session_id: "session-1" }
        : overrides.type === "TEXT_BLOCK_DELTA"
          ? { block_id: "block-1", reply_id: "reply-1", delta: "" }
          : overrides.type === "REQUIRE_USER_CONFIRM"
            ? { reply_id: "reply-1", tool_calls: [] }
            : {};
  return { ...base, ...defaults, ...overrides } as AgentScopeEventByType<T>;
}

function encodeEvents(...events: AgentScopeAgentEvent[]): Uint8Array {
  return new TextEncoder().encode(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""));
}

function streamResponse(body: ReadableStream<Uint8Array>): Response {
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

describe("AgentScope 原生事件投影", () => {
  it("消费开始前 signal 已 aborted 时仍取消真实 Fetch body reader", async () => {
    const response = await fetch(
      "data:text/event-stream,data:%20%7B%22id%22%3A%22event-aborted%22%2C%22type%22%3A%22CUSTOM%22%7D%0A%0A",
    );
    const body = response.body;
    if (!body) throw new Error("Fetch data URL 没有返回 response body");
    const controller = new AbortController();
    controller.abort("already_aborted");

    await consumeAgentScopeSse(body, () => undefined, {}, controller.signal);

    const reader = body.getReader();
    await expect(reader.read()).resolves.toEqual({ done: true, value: undefined });
    reader.releaseLock();
  });

  it("保留未知事件的原始对象且按到达顺序编号", () => {
    const reducer = new AgentScopeEventReducer();
    const unknown = {
      id: "event-1",
      created_at: "2026-09-09T00:00:00Z",
      metadata: {},
      type: "FUTURE_EVENT",
      value: { nested: true },
    } as unknown as AgentScopeAgentEvent;
    const first = reducer.reduce(unknown);
    const second = reducer.reduce(nativeEvent({ id: "delta-1", type: "TEXT_BLOCK_DELTA", delta: "后" }));

    expect(first.traceEvent).toMatchObject({
      event_id: "event-1",
      source_event: "FUTURE_EVENT",
      kind: "runtime_event",
      sequence: 1,
    });
    expect(first.traceEvent.payload).toBe(unknown);
    expect(second.traceEvent.sequence).toBe(2);
    expect(second.textDelta).toBe("后");
  });

  it("HITL 续跑重复 REPLY_START 时不丢失前后 delta", () => {
    const reducer = new AgentScopeEventReducer();
    reducer.reduce(nativeEvent({ id: "start-1", type: "REPLY_START", reply_id: "reply-1" }));
    const first = reducer.reduce(nativeEvent({
      id: "delta-1", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "前",
    }));
    reducer.reduce(nativeEvent({ id: "start-2", type: "REPLY_START", reply_id: "reply-1" }));
    const second = reducer.reduce(nativeEvent({
      id: "delta-2", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "后",
    }));

    expect(`${first.textDelta}${second.textDelta}`).toBe("前后");
    expect(second.traceEvent.sequence).toBe(4);
  });

  it("只提取 data 帧并忽略 AgentScope 心跳注释", () => {
    expect(extractSseFrames(':\r\n\r\ndata: {"type":"CUSTOM"}\r\n\r\n')).toEqual({
      data: ['{"type":"CUSTOM"}'],
      rest: "",
    });
  });
});

describe("AgentScope reply completion 边界", () => {
  it("新发送忽略 arm 前的旧回复，只接受 arm 后 START/END 精确链", async () => {
    const completion = new AgentScopeReplyCompletion();
    completion.observeReplyStart(nativeEvent({ type: "REPLY_START", reply_id: "reply-old" }));
    completion.observeReplyEnd(nativeEvent({ type: "REPLY_END", reply_id: "reply-old" }));

    const terminal = completion.arm();
    const currentEnd = nativeEvent({ type: "REPLY_END", reply_id: "reply-current" });
    completion.observeReplyStart(nativeEvent({ type: "REPLY_START", reply_id: "reply-current" }));
    completion.observeReplyEnd(currentEnd);

    await expect(terminal).resolves.toBe(currentEnd);
  });

  it("detached pre-arm 保留 connect 返回前已完成的本次 START/END", async () => {
    const completion = new AgentScopeReplyCompletion(true);
    const replyEnd = nativeEvent({ type: "REPLY_END", reply_id: "reply-1" });
    completion.observeReplyStart(nativeEvent({ type: "REPLY_START", reply_id: "reply-1" }));
    completion.observeReplyEnd(replyEnd);
    completion.fail(new Error("读取器紧随终态关闭"));

    await expect(completion.arm()).resolves.toBe(replyEnd);
    await expect(completion.arm()).rejects.toThrow("已有回复正在等待 REPLY_END");
  });

  it("detached pre-arm 不用 orphan/旧 REPLY_END 解锁当前回复", async () => {
    const completion = new AgentScopeReplyCompletion(true);
    completion.observeReplyEnd(nativeEvent({ type: "REPLY_END", reply_id: "reply-old" }));
    const terminal = completion.arm();
    completion.observeReplyStart(nativeEvent({ type: "REPLY_START", reply_id: "reply-current" }));
    completion.observeReplyEnd(nativeEvent({ type: "REPLY_END", reply_id: "reply-other" }));
    const currentEnd = nativeEvent({ type: "REPLY_END", reply_id: "reply-current" });
    completion.observeReplyEnd(currentEnd);

    await expect(terminal).resolves.toBe(currentEnd);
  });

  it("detached 已知目标 reply 时忽略目标 START 前完整的旧回复", async () => {
    const completion = new AgentScopeReplyCompletion(true, "reply-current");
    completion.observeReplyStart(nativeEvent({ type: "REPLY_START", reply_id: "reply-old" }));
    completion.observeReplyEnd(nativeEvent({ type: "REPLY_END", reply_id: "reply-old" }));
    const terminal = completion.arm();
    const currentEnd = nativeEvent({ type: "REPLY_END", reply_id: "reply-current" });
    completion.observeReplyStart(nativeEvent({ type: "REPLY_START", reply_id: "reply-current" }));
    completion.observeReplyEnd(currentEnd);

    await expect(terminal).resolves.toBe(currentEnd);
  });
});

describe("AgentScope session SSE", () => {
  it("detached 消费在 connection 返回前完成也能精确交付，并带 Runtime identity", async () => {
    const start = nativeEvent({ type: "REPLY_START", reply_id: "reply-1", session_id: "session-1" });
    const end = nativeEvent({ type: "REPLY_END", reply_id: "reply-1", session_id: "session-1" });
    const fetchMock = vi.fn().mockResolvedValue(streamResponse(new ReadableStream({
      start(controller) {
        controller.enqueue(encodeEvents(start, end));
        controller.close();
      },
    })));
    vi.stubGlobal("fetch", fetchMock);

    const connection = await connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "secret" },
      "agent-1",
      "session-1",
      {},
      undefined,
      { captureReplyBeforeArm: true },
    );

    await expect(connection.armReply()).resolves.toMatchObject({
      type: "REPLY_END", reply_id: "reply-1",
    });
    await connection.closed;
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://runtime.test/api/runtime/sessions/session-1/stream?agent_id=agent-1");
    expect(new Headers(init.headers).get("X-User-ID")).toBe("agentgov-ui");
    expect(new Headers(init.headers).get("Accept")).toBe("text/event-stream");
  });

  it("新发送不接受 connection 返回前的旧 START/END", async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamResponse(new ReadableStream({
      start(controller) {
        controller.enqueue(encodeEvents(
          nativeEvent({ type: "REPLY_START", reply_id: "reply-old" }),
          nativeEvent({ type: "REPLY_END", reply_id: "reply-old" }),
        ));
        controller.close();
      },
    })));
    vi.stubGlobal("fetch", fetchMock);

    const connection = await connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
    );
    await connection.closed;

    await expect(connection.armReply()).rejects.toThrow("REPLY_END 前断开");
  });

  it("拒绝 200 但非 text/event-stream 的响应", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
    )).rejects.toThrow("Content-Type application/json");
  });

  it("保留 replay/live Team HITL 的 outer CUSTOM 原始 trace 与控制回调", async () => {
    const stream = new TransformStream<Uint8Array, Uint8Array>();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse(stream.readable)));
    const traces: Array<Record<string, unknown>> = [];
    const requests: Array<{ event: AgentScopeAgentEvent; worker?: string }> = [];
    const resolutions: Array<{ worker_session_id: string; reply_id: string }> = [];
    const connection = await connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "leader-agent",
      "leader-session",
      {
        onTraceEvent: (event) => traces.push(event as unknown as Record<string, unknown>),
        onUserConfirmRequired: (event, projection) => requests.push({
          event,
          worker: projection?.worker_session_id,
        }),
        onUserConfirmResolved: (projection) => resolutions.push(projection),
      },
    );
    const customRequest = (suffix: string) => nativeEvent({
      id: `custom-${suffix}`,
      type: "CUSTOM",
      name: "subagent_require_user_confirm",
      value: {
        worker_session_id: `worker-${suffix}`,
        worker_agent_id: `worker-agent-${suffix}`,
        worker_agent_name: `Worker ${suffix}`,
        reply_id: `worker-reply-${suffix}`,
        event_type: "require_user_confirm",
        event: nativeEvent({
          id: `confirm-${suffix}`,
          type: "REQUIRE_USER_CONFIRM",
          reply_id: `worker-reply-${suffix}`,
          tool_calls: [],
        }),
        created_at: "2026-09-10T00:00:00Z",
      },
    });
    const replayed = customRequest("replay");
    const live = customRequest("live");
    const resolved = nativeEvent({
      id: "custom-result",
      type: "CUSTOM",
      name: "subagent_user_confirm_result",
      value: { worker_session_id: "worker-live", reply_id: "worker-reply-live" },
    });
    const writer = stream.writable.getWriter();
    await writer.write(encodeEvents(replayed, live, resolved));
    await writer.close();
    await connection.closed;

    expect(requests.map((item) => item.worker)).toEqual(["worker-replay", "worker-live"]);
    expect(requests[0].event).toEqual((replayed.value as { event: unknown }).event);
    expect(resolutions).toEqual([{ worker_session_id: "worker-live", reply_id: "worker-reply-live" }]);
    expect(traces.map((trace) => trace.payload)).toEqual([replayed, live, resolved]);
    expect(subagentHitlProjection(replayed)?.event.type).toBe("REQUIRE_USER_CONFIRM");
    expect(subagentHitlResolution(resolved)).toEqual({
      worker_session_id: "worker-live", reply_id: "worker-reply-live",
    });
  });
});

describe("AgentScope chat dispatch", () => {
  it("传输重试复用调用方持有的 Session intent key", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(
      JSON.stringify({ session_id: "session-1" }),
      { status: 201, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await createRuntimeSession(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-create_stable-intent",
    );
    await createRuntimeSession(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-create_stable-intent",
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [, init] of fetchMock.mock.calls as Array<[string, RequestInit]>) {
      expect(new Headers(init.headers).get("Idempotency-Key")).toBe("session-create_stable-intent");
    }
  });

  it("保留原生 started JSON，AgentGov identity 只从 headers 读取", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ status: "started", session_id: "session-1" }),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-AgentGov-Run-Id": "run-1",
          "X-AgentGov-Session-Id": "session-1",
        },
      },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(startRuntimeChat(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
      { name: "user", role: "user", content: [{ type: "text", text: "hello" }] },
      { clientOperationId: "operation-1" },
    )).resolves.toEqual({ status: "started", session_id: "session-1", runId: "run-1" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      agent_id: "agent-1",
      session_id: "session-1",
      client_operation_id: "operation-1",
      confirmation_scope: "once",
      metadata: { client: "agent-gov-ui" },
    });
  });

  it("保留 Runtime error_code 供固定失败分支判断", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "restart required", error_code: "RUNTIMERESTARTREQUIRED" }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    )));

    const error = await startRuntimeChat(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
      { name: "user", role: "user", content: [{ type: "text", text: "hello" }] },
      { clientOperationId: "operation-1" },
    ).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error).toMatchObject({ kind: "http", status: 503, errorCode: "RUNTIMERESTARTREQUIRED" });
  });

  it("丢失初始 POST 回执时只按 session/client operation 精确查询", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      run_id: "run-1",
      session_id: "session-1",
      client_operation_id: "operation-1",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await getAgentRunByClientOperation(
      { apiBase: "http://runtime.test", apiKey: "" },
      "session-1",
      "operation-1",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://runtime.test/api/agent-runs/by-client-operation?session_id=session-1&client_operation_id=operation-1",
    );
  });

  it("用原生 USER_CONFIRM_RESULT 续跑，不夹带浏览器权限规则", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ status: "started", session_id: "session-1" }),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-AgentGov-Run-Id": "run-1",
          "X-AgentGov-Session-Id": "session-1",
        },
      },
    ));
    vi.stubGlobal("fetch", fetchMock);
    const toolCall = {
      type: "tool_call" as const,
      id: "tool-1",
      name: "Read",
      input: '{"path":"AGENT.md"}',
      state: "asking" as const,
    };

    await startRuntimeChat(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
      {
        type: "USER_CONFIRM_RESULT",
        reply_id: "reply-1",
        confirm_results: [{ confirmed: true, tool_call: toolCall }],
      },
      {
        confirmationScope: "run",
        expectedRunId: "run-1",
        clientOperationId: "operation-1",
      },
    );

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(body).toMatchObject({
      expected_run_id: "run-1",
      client_operation_id: "operation-1",
      confirmation_scope: "run",
    });
    expect(body.input.confirm_results).toEqual([{ confirmed: true, tool_call: toolCall }]);
    expect(body.input.confirm_results[0]).not.toHaveProperty("rules");
  });

  it("治理 identity 不冒充 Runtime agent_id", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        sessions: [{
          session: {
            id: "session-1",
            agent_id: "runtime-1",
            user_id: "user-1",
            config: { workspace_id: "workspace-1", name: "原生会话" },
            created_at: "2026-09-10T00:00:00Z",
            updated_at: "2026-09-10T00:00:01Z",
          },
          is_running: false,
          status: "idle",
          active_run_id: "run-active",
        }],
        total: 1,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const sessions = await getSessions(
      { apiBase: "http://runtime.test", apiKey: "" },
      "business-1",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://runtime.test/api/runtime/sessions/?governance_agent_id=business-1",
    );
    expect(sessions[0]).toMatchObject({
      agent_id: "runtime-1",
      business_agent_id: "business-1",
      active_run_id: "run-active",
    });
  });
});
