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
import type { AgentScopeAgentEvent } from "../types/runtime";

type AgentScopeEventType = AgentScopeAgentEvent["type"];
type AgentScopeEventByType<T extends AgentScopeEventType> = Extract<AgentScopeAgentEvent, { type: T }>;

afterEach(() => {
  vi.unstubAllGlobals();
});

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

  it("保留 worker HITL 的 outer CUSTOM 事件与精确内层回复身份", () => {
    const inner = nativeEvent({ type: "REQUIRE_USER_CONFIRM", reply_id: "worker-reply" });
    const outer = nativeEvent({
      type: "CUSTOM", name: "subagent_require_user_confirm",
      value: {
        worker_session_id: "worker-session", worker_agent_id: "worker-agent", worker_agent_name: "worker",
        reply_id: "worker-reply", created_at: "2026-09-13T00:00:00Z", event_type: "require_user_confirm", event: inner,
      },
    });
    expect(new AgentScopeEventReducer().reduce(outer).traceEvent.payload).toBe(outer);
    expect(subagentHitlProjection(outer)?.event).toBe(inner);
    const mismatched = { ...outer, value: { ...outer.value, reply_id: "different-reply" } };
    expect(subagentHitlProjection(mismatched)).toBeUndefined();
    expect(subagentHitlResolution(nativeEvent({
      type: "CUSTOM", name: "subagent_user_confirm_result", value: { worker_session_id: "worker-session", reply_id: "worker-reply" },
    }))).toEqual({ worker_session_id: "worker-session", reply_id: "worker-reply" });
  });
});

describe("AgentScope SSE readiness 契约", () => {
  it("response 已返回但 readiness comment 尚未完整到达时，connect 保持 pending", async () => {
    let bodyController!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(controller) { bodyController = controller; },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));
    let connected = false;
    const connecting = connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
    ).then((connection) => {
      connected = true;
      return connection;
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(connected).toBe(false);
    bodyController.enqueue(new TextEncoder().encode(":"));
    await Promise.resolve();
    expect(connected).toBe(false);

    bodyController.enqueue(new TextEncoder().encode("\n\n"));
    const connection = await connecting;
    expect(connected).toBe(true);
    connection.close();
    await connection.closed;
  });

  it("同一网络块中的 readiness 后原生事件仍按原对象投影", async () => {
    const event = nativeEvent({ type: "CUSTOM", name: "after_ready", value: { kept: true } });
    const observed: AgentScopeAgentEvent[] = [];
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(`:\n\ndata: ${JSON.stringify(event)}\n\n`));
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));

    const connection = await connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
      { onEvent: (candidate) => observed.push(candidate) },
    );

    expect(observed).toEqual([event]);
    connection.close();
    await connection.closed;
  });

  it("首帧不是 readiness comment 时 fail closed，不把事件误当作已订阅", async () => {
    const event = nativeEvent({ type: "CUSTOM", name: "too_early" });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));

    await expect(connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
    )).rejects.toThrow("未先返回 readiness comment");
  });

  it("等待 readiness 时调用方取消会结束 connect，不能悬挂或误报 ready", async () => {
    const body = new ReadableStream<Uint8Array>({ start: () => undefined });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));
    const caller = new AbortController();
    const connecting = connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "" },
      "agent-1",
      "session-1",
      {},
      caller.signal,
    );
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    caller.abort("cancel_before_ready");

    await expect(connecting).rejects.toThrow("Runtime 事件流已关闭");
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
