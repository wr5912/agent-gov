import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AgentScopeEventReducer,
  connectAgentScopeSessionStream,
  extractSseFrames,
  subagentHitlProjection,
} from "./agentScopeStream";
import { createRuntimeSession, getSessions, provisionRuntimeAgent, startRuntimeChat } from "./runtime";
import { getAgentRunByClientOperation } from "./feedback";
import { ApiRequestError } from "./request";
import type { AgentScopeAgentEvent } from "../types/runtime";

afterEach(() => {
  vi.unstubAllGlobals();
});

function nativeEvent(overrides: Partial<AgentScopeAgentEvent>): AgentScopeAgentEvent {
  return {
    id: "event-1",
    created_at: "2026-09-09T00:00:00Z",
    metadata: {},
    type: "CUSTOM",
    ...overrides,
  };
}

describe("AgentScopeEventReducer", () => {
  it("keeps an unknown event visible with its exact native payload", () => {
    const reducer = new AgentScopeEventReducer();
    const event = nativeEvent({ type: "FUTURE_EVENT", value: { nested: true } });

    const effects = reducer.reduce(event);

    expect(effects.traceEvent.event_id).toBe("event-1");
    expect(effects.traceEvent.source_event).toBe("FUTURE_EVENT");
    expect(effects.traceEvent.kind).toBe("runtime_event");
    expect(effects.traceEvent.payload).toBe(event);
  });

  it("does not reset accumulated deltas when HITL continuation repeats REPLY_START", () => {
    const reducer = new AgentScopeEventReducer();
    reducer.reduce(nativeEvent({ id: "start-1", type: "REPLY_START", reply_id: "reply-1" }));
    const first = reducer.reduce(nativeEvent({ id: "delta-1", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "前" }));
    reducer.reduce(nativeEvent({ id: "start-2", type: "REPLY_START", reply_id: "reply-1" }));
    const second = reducer.reduce(nativeEvent({ id: "delta-2", type: "TEXT_BLOCK_DELTA", reply_id: "reply-1", delta: "后" }));

    expect(`${first.textDelta}${second.textDelta}`).toBe("前后");
    expect(second.traceEvent.sequence).toBe(4);
  });
});

describe("AgentScope session SSE", () => {
  it("establishes the native stream with X-User-ID before waiting for REPLY_END", async () => {
    const stream = new TransformStream<Uint8Array, Uint8Array>();
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream.readable, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const connection = await connectAgentScopeSessionStream(
      { apiBase: "http://runtime.test", apiKey: "secret" },
      "agent-1",
      "session-1",
    );
    const terminal = connection.armReply();
    const writer = stream.writable.getWriter();
    const encoder = new TextEncoder();
    await writer.write(encoder.encode('data: {"id":"start","created_at":"t","metadata":{},"type":"REPLY_START","session_id":"session-1","reply_id":"reply-1","name":"agent","role":"assistant"}\n\n'));
    await writer.write(encoder.encode(':\n\ndata: {"id":"end","created_at":"t","metadata":{},"type":"REPLY_END","session_id":"session-1","reply_id":"reply-1","finished_reason":"completed","error":null}\n\n'));

    await expect(terminal).resolves.toMatchObject({ type: "REPLY_END", reply_id: "reply-1" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://runtime.test/api/runtime/sessions/session-1/stream?agent_id=agent-1");
    expect(new Headers(init.headers).get("X-User-ID")).toBe("agentgov-ui");
    connection.close();
    await writer.close().catch(() => undefined);
    await connection.closed;
  });

  it("parses data-only frames while ignoring heartbeat comments", () => {
    expect(extractSseFrames(':\n\ndata: {"type":"CUSTOM"}\n\n')).toEqual({
      data: ['{"type":"CUSTOM"}'],
      rest: "",
    });
  });

  it("projects replayed and live Team worker HITL while retaining each outer CUSTOM trace payload", async () => {
    const stream = new TransformStream<Uint8Array, Uint8Array>();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream.readable, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    })));
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
    const writer = stream.writable.getWriter();
    const encoder = new TextEncoder();
    const customRequest = (suffix: string) => ({
      id: `custom-${suffix}`,
      created_at: "2026-09-10T00:00:00Z",
      metadata: { delivery: suffix },
      type: "CUSTOM",
      name: "subagent_require_user_confirm",
      value: {
        worker_session_id: `worker-${suffix}`,
        worker_agent_id: `worker-agent-${suffix}`,
        worker_agent_name: `Worker ${suffix}`,
        reply_id: `worker-reply-${suffix}`,
        event_type: "require_user_confirm",
        event: {
          id: `confirm-${suffix}`,
          created_at: "2026-09-10T00:00:00Z",
          metadata: {},
          type: "REQUIRE_USER_CONFIRM",
          reply_id: `worker-reply-${suffix}`,
          tool_calls: [{
            type: "tool_call",
            id: `tool-${suffix}`,
            name: "Read",
            input: '{"path":"AGENT.md"}',
            state: "asking",
          }],
        },
        created_at: "2026-09-10T00:00:00Z",
      },
    });
    const replayed = customRequest("replay");
    const live = customRequest("live");
    const resolved = {
      id: "custom-result",
      created_at: "2026-09-10T00:00:01Z",
      metadata: {},
      type: "CUSTOM",
      name: "subagent_user_confirm_result",
      value: { worker_session_id: "worker-live", reply_id: "worker-reply-live" },
    };
    await writer.write(encoder.encode(`data: ${JSON.stringify(replayed)}\n\n`));
    await writer.write(encoder.encode(`data: ${JSON.stringify(live)}\n\n`));
    await writer.write(encoder.encode(`data: ${JSON.stringify(resolved)}\n\n`));
    await writer.close();
    await connection.closed;

    expect(requests).toHaveLength(2);
    expect(requests.map((item) => item.worker)).toEqual(["worker-replay", "worker-live"]);
    expect(requests[0].event).toEqual((replayed.value as { event: unknown }).event);
    expect(resolutions).toEqual([{ worker_session_id: "worker-live", reply_id: "worker-reply-live" }]);
    expect(traces.map((trace) => trace.payload)).toEqual([replayed, live, resolved]);
    expect(subagentHitlProjection(replayed as AgentScopeAgentEvent)?.event.type).toBe("REQUIRE_USER_CONFIRM");
  });
});

describe("AgentScope chat dispatch", () => {
  it("reuses the caller-owned Session creation intent key across transport retries", async () => {
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

  it("keeps the native started JSON and reads AgentGov identity only from headers", async () => {
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
      input: { name: "user", role: "user", content: [{ type: "text", text: "hello" }] },
    });
  });

  it("preserves Runtime error_code in ApiRequestError for fixed-failure decisions", async () => {
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
    expect(error).toMatchObject({
      kind: "http",
      status: 503,
      errorCode: "RUNTIMERESTARTREQUIRED",
    });
  });

  it("queries an ambiguous initial POST only by exact session and client operation IDs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      run_id: "run-1",
      session_id: "session-1",
      metadata: { client_operation_id: "operation-1" },
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

  it("submits HITL as a native USER_CONFIRM_RESULT without browser permission rules", async () => {
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
      state: "asking",
      suggested_rules: [{ tool: "Read" }],
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
    expect(body.confirmation_scope).toBe("run");
    expect(body.expected_run_id).toBe("run-1");
    expect(body.client_operation_id).toBe("operation-1");
    expect(body.input.confirm_results).toEqual([{ confirmed: true, tool_call: toolCall }]);
    expect(body.input.confirm_results[0]).not.toHaveProperty("rules");
  });

  it("keeps governance identity out of Runtime agent_id and provisions explicitly", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        governance_agent_id: "business-1",
        agent_version_id: "version-1",
        harness_digest: "a".repeat(64),
        runtime_agent_id: "runtime-1",
        provisioned: true,
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        sessions: [{
          session: { id: "session-1", agent_id: "runtime-1", created_at: "2026-09-10T00:00:00Z" },
          is_running: false,
          status: "idle",
        }],
        total: 1,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await provisionRuntimeAgent(
      { apiBase: "http://runtime.test", apiKey: "" },
      "business-1",
    );
    const sessions = await getSessions(
      { apiBase: "http://runtime.test", apiKey: "" },
      "business-1",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://runtime.test/api/runtime/agents/business-1/provision",
    );
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://runtime.test/api/runtime/sessions/?governance_agent_id=business-1",
    );
    expect(sessions[0]).toMatchObject({
      agent_id: "runtime-1",
      business_agent_id: "business-1",
    });
  });
});
