import { describe, expect, it } from "vitest";
import {
  activeAgentGovRun,
  mergeExactRunEventsIntoCanonicalMessages,
  messagesFromAgentScopeMessages,
  terminalAssistantMessageId,
  toolCallMatchesPendingAction,
} from "./playgroundHistory";
import type { FeedbackRunRecord } from "./types/feedback";
import type {
  AgentScopeMessage,
  AgentScopeToolCallBlock,
  ChatMessage,
  RuntimePendingAction,
  StreamLogEvent,
} from "./types/runtime";

function message(overrides: Partial<AgentScopeMessage>): AgentScopeMessage {
  return {
    id: "message-1",
    name: "agent",
    role: "assistant",
    content: [],
    metadata: {},
    created_at: "2026-09-09T00:00:00Z",
    ...overrides,
  };
}

describe("AgentScope message history", () => {
  it("finds an AgentGov active run even when the AgentScope session is idle", () => {
    const runs = [
      { run_id: "run-old", status: "succeeded" },
      { run_id: "run-finalizing", status: "finalizing" },
    ] as FeedbackRunRecord[];

    expect(activeAgentGovRun(runs)?.run_id).toBe("run-finalizing");
  });

  it("reconstructs text, terminal outcome, and run identity from native messages", async () => {
    const run = {
      run_id: "run-1",
      session_id: "session-1",
      agent_id: "agent-1",
      agent_version_id: "version-1",
      status: "succeeded",
      reply_ids: ["reply-1"],
      trace_id: "0123456789abcdef0123456789abcdef",
    } as unknown as FeedbackRunRecord;

    const result = await messagesFromAgentScopeMessages([
      message({
        id: "reply-1",
        content: [{ type: "text", text: "最终回答" }],
        finished_reason: "completed",
      }),
    ], "session-1", [run]);

    expect(result[0]).toMatchObject({
      id: "reply-1",
      content: "最终回答",
      runId: "run-1",
      sessionId: "session-1",
      runOutcome: "succeeded",
      langfuseTraceId: "0123456789abcdef0123456789abcdef",
    });
  });

  it("requires both the durable action and exact canonical AgentScope tool call", async () => {
    const toolCall = {
      type: "tool_call" as const,
      id: "tool-1",
      name: "Write",
      input: '{"path":"report.md"}',
      state: "asking" as const,
    } satisfies AgentScopeToolCallBlock;
    const action: RuntimePendingAction = {
      action_id: "action-root",
      session_id: "session-1",
      runtime_agent_id: "agent-1",
      run_id: "run-root",
      reply_id: "reply-2",
      kind: "human",
      tool_call_id: "tool-1",
      tool_call_name: "Write",
      tool_call_state: "asking",
      tool_call_utf8_length: 101,
      tool_call_sha256: "4a48a1e18682d4179c90ea178f266c6bed45c205e3419e60f03577cb3140db01",
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };
    const run = {
      run_id: "run-root",
      session_id: "session-1",
      status: "waiting_human",
      reply_ids: ["reply-2"],
    } as unknown as FeedbackRunRecord;
    const result = await messagesFromAgentScopeMessages([
      message({ id: "reply-2", content: [toolCall] }),
    ], "session-1", [run], [action]);

    expect(result[0].userConfirmRequests?.[0]).toMatchObject({
      requestId: "pending:run-root:session-1:reply-2",
      replyId: "reply-2",
      status: "waiting",
    });
    expect(result[0].userConfirmRequests?.[0].toolCalls[0]).toBe(toolCall);
    expect(await toolCallMatchesPendingAction(toolCall, action)).toBe(true);
    expect(await toolCallMatchesPendingAction({ ...toolCall, input: "{}" }, action)).toBe(false);
  });

  it("renders an error-only reply instead of dropping it", async () => {
    const result = await messagesFromAgentScopeMessages([
      message({
        id: "reply-3",
        finished_reason: "error",
        error: { type: "upstream", message: "model unavailable" },
      }),
    ], "session-1");

    expect(result[0]).toMatchObject({
      content: "",
      executionError: { type: "upstream", message: "model unavailable" },
      runOutcome: "failed",
      partial: false,
    });
  });

  it.each([
    { reason: "error" as const, content: "partial", error: { type: "upstream" as const, message: "upstream unavailable" }, outcome: "failed", detail: "upstream unavailable" },
    { reason: "error" as const, content: "", error: undefined, outcome: "failed", detail: "运行失败，Runtime 未提供错误详情。" },
    { reason: "exceed_max_iters" as const, content: "partial", error: undefined, outcome: "failed", detail: "运行达到最大迭代次数。" },
    { reason: "interrupted" as const, content: "partial", error: undefined, outcome: "interrupted", detail: undefined },
    { reason: "completed" as const, content: "complete", error: undefined, outcome: "succeeded", detail: undefined },
  ])("keeps $reason details separate from canonical reply text", async ({ reason, content, error, outcome, detail }) => {
    const [result] = await messagesFromAgentScopeMessages([
      message({ content: [{ type: "text", text: content }], finished_reason: reason, error }),
    ], "session-1");

    expect(result.content).toBe(content);
    expect(result.runOutcome).toBe(outcome);
    expect(result.executionError?.message).toBe(detail);
    expect(result.partial).toBe(reason !== "completed" && Boolean(content));
  });

  it("shows a failed run without inventing a native reply or user input", async () => {
    const result = await messagesFromAgentScopeMessages([], "session-1", [{
      run_id: "run-no-reply",
      session_id: "session-1",
      status: "failed",
      reply_ids: [],
      error: { message: "session setup unavailable" },
    } as unknown as FeedbackRunRecord]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      role: "assistant",
      content: "",
      runId: "run-no-reply",
      runOutcome: "failed",
      executionError: { message: "session setup unavailable" },
    });
  });

  it("保留生产 trigger failure 的 type-only 错误及 terminal reason", async () => {
    const result = await messagesFromAgentScopeMessages([], "session-1", [{
      run_id: "run-trigger-failed",
      session_id: "session-1",
      status: "failed",
      terminal_reason: "trigger_failed",
      reply_ids: [],
      error: { type: "RuntimeUpstreamError" },
    } as unknown as FeedbackRunRecord]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-trigger-failed",
      runOutcome: "failed",
      executionError: { type: "RuntimeUpstreamError", message: "trigger_failed" },
    });
  });

  it("retains an authoritative run failure when canonical text has no native error details", async () => {
    const [result] = await messagesFromAgentScopeMessages([
      message({ id: "reply-run-error", content: [{ type: "text", text: "canonical output" }] }),
    ], "session-1", [{
      run_id: "run-error", session_id: "session-1", status: "failed", reply_ids: ["reply-run-error"],
      error: { message: "Runtime finalization failed" },
    } as unknown as FeedbackRunRecord]);

    expect(result).toMatchObject({
      content: "canonical output", runOutcome: "failed", executionError: { message: "Runtime finalization failed" },
    });
  });

  it("canonical reply 无 native error 时仍保留 run 的 type-only 失败", async () => {
    const [result] = await messagesFromAgentScopeMessages([
      message({ id: "reply-type-only", content: [{ type: "text", text: "partial output" }] }),
    ], "session-1", [{
      run_id: "run-type-only", session_id: "session-1", status: "failed",
      terminal_reason: "trigger_failed", reply_ids: ["reply-type-only"],
      error: { type: "RuntimeUpstreamError" },
    } as unknown as FeedbackRunRecord]);

    expect(result).toMatchObject({
      content: "partial output",
      runOutcome: "failed",
      executionError: { type: "RuntimeUpstreamError", message: "trigger_failed" },
    });
  });

  it("uses only an unambiguous run in the same Session, never native metadata claims", async () => {
    const input = message({
      id: "reply-identity",
      metadata: { run_id: "run-claimed", agent_version_id: "version-claimed", trace_id: "trace-claimed" },
    });
    const run = {
      run_id: "run-verified",
      session_id: "session-1",
      status: "running",
      reply_ids: [input.id],
    } as unknown as FeedbackRunRecord;

    const [verified] = await messagesFromAgentScopeMessages([input], "session-1", [run]);
    expect(verified.runId).toBe("run-verified");
    expect(verified.agentVersionId).toBeUndefined();
    expect(verified.langfuseTraceId).toBeUndefined();
    const [foreign] = await messagesFromAgentScopeMessages([input], "session-1", [{ ...run, session_id: "other-session" }]);
    expect(foreign.runId).toBeUndefined();
    const [ambiguous] = await messagesFromAgentScopeMessages([input], "session-1", [run, { ...run, run_id: "run-other" }]);
    expect(ambiguous.runId).toBeUndefined();
  });

  it("preserves the previous turn identity and Trace while replacing text with canonical history", async () => {
    const event: StreamLogEvent = {
      id: "event-first", event: "REPLY_END", createdAt: "2026-09-10T00:00:00Z",
      data: { run_id: "run-first", payload: { reply_id: "reply-first" } },
    };
    const workerEvent: StreamLogEvent = {
      ...event, id: "event-worker", data: { run_id: "run-first", payload: { reply_id: "worker-reply" } },
    };
    const current: ChatMessage[] = [{
      id: "reply-first", role: "assistant", content: "previous presentation",
      createdAt: "2026-09-10T00:00:00Z", sessionId: "session-1",
      runId: "run-first", agentVersionId: "version-session", langfuseTraceId: "trace-first",
      entities: { document: ["doc-first"] },
      traceState: "ready", events: [event, workerEvent], controlError: "old transport failure",
      userConfirmRequests: [{ requestId: "old-action", replyId: "reply-first", toolCalls: [], status: "waiting" }],
    }];
    const canonical = await messagesFromAgentScopeMessages([
      message({ id: "reply-first", content: [{ type: "text", text: "canonical first" }], finished_reason: "completed" }),
      message({ id: "reply-second", content: [{ type: "text", text: "canonical second" }], finished_reason: "completed" }),
    ], "session-1", [{
      run_id: "run-second", session_id: "session-1", status: "succeeded",
      agent_version_id: "version-session", reply_ids: ["reply-second"], trace_id: "trace-second",
    } as unknown as FeedbackRunRecord]);

    const merged = mergeExactRunEventsIntoCanonicalMessages(current, canonical, "run-second");
    expect(merged[0]).toMatchObject({
      content: "canonical first", runId: "run-first", agentVersionId: "version-session",
      langfuseTraceId: "trace-first", traceState: "ready", events: [event, workerEvent],
      entities: { document: ["doc-first"] },
    });
    expect(merged[0].controlError).toBeUndefined();
    expect(merged[0].userConfirmRequests).toBeUndefined();
    expect(merged[1]).toMatchObject({ runId: "run-second", langfuseTraceId: "trace-second", events: [] });
    expect(mergeExactRunEventsIntoCanonicalMessages(merged, canonical, "run-second")[0].events).toEqual([event, workerEvent]);
  });

  it("never transfers prior context across Sessions or a conflicting authoritative run", () => {
    const prior: ChatMessage = {
      id: "same-id", role: "assistant", content: "old", createdAt: "t", sessionId: "session-1",
      runId: "run-old", agentVersionId: "version-old", langfuseTraceId: "trace-old", events: [],
      entities: { document: ["doc-old"] },
    };
    const canonical: ChatMessage = { id: prior.id, role: "assistant", content: "new", createdAt: "t", sessionId: "session-1" };
    const [differentSession] = mergeExactRunEventsIntoCanonicalMessages([{ ...prior, sessionId: "session-other" }], [canonical], "run-current");
    expect(differentSession.runId).toBeUndefined();
    expect(differentSession.entities).toBeUndefined();
    const [differentRun] = mergeExactRunEventsIntoCanonicalMessages([prior], [{ ...canonical, runId: "run-current" }], "run-current");
    expect(differentRun.entities).toBeUndefined();
    expect(differentRun.agentVersionId).toBeUndefined();
    expect(differentRun.langfuseTraceId).toBeUndefined();
  });

  it("restores every exact reply/run association beyond 500 rounds without cached UI context", async () => {
    const runs = Array.from({ length: 501 }, (_, index) => ({
      run_id: `run-${index}`, session_id: "session-long", status: "succeeded",
      agent_version_id: "version-session", reply_ids: [`reply-${index}`], trace_id: `trace-${index}`,
    } as unknown as FeedbackRunRecord));
    const messages = runs.map((_, index) => message({ id: `reply-${index}`, finished_reason: "completed" }));

    const restored = await messagesFromAgentScopeMessages(messages, "session-long", runs);
    expect(restored).toHaveLength(501);
    expect(restored.map((item) => [item.runId, item.agentVersionId, item.langfuseTraceId])).toEqual(
      runs.map((run) => [run.run_id, run.agent_version_id, run.trace_id]),
    );
  });

  it("restores a detached worker HITL action from its exact canonical child Session", async () => {
    const run = {
      run_id: "run-worker",
      session_id: "leader-session",
      agent_id: "agent-1",
      agent_version_id: "version-1",
      status: "waiting_human",
      updated_at: "2026-09-10T00:00:01Z",
    } as unknown as FeedbackRunRecord;
    const action: RuntimePendingAction = {
      action_id: "action-1",
      session_id: "worker-session",
      runtime_agent_id: "worker-agent",
      run_id: "run-worker",
      reply_id: "worker-reply",
      kind: "human",
      tool_call_id: "tool-1",
      tool_call_name: "Write",
      tool_call_state: "asking",
      tool_call_utf8_length: 101,
      tool_call_sha256: "4a48a1e18682d4179c90ea178f266c6bed45c205e3419e60f03577cb3140db01",
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const toolCall = {
      type: "tool_call" as const,
      id: "tool-1",
      name: "Write",
      input: '{"path":"report.md"}',
      state: "asking" as const,
    } satisfies AgentScopeToolCallBlock;
    const canonical = new Map([
      ["leader-session", []],
      ["worker-session", [message({ id: "worker-reply", content: [toolCall] })]],
    ]);
    const result = await messagesFromAgentScopeMessages([], "leader-session", [run], [action], canonical);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-worker",
      sessionId: "leader-session",
      content: "",
    });
    expect(result[0].userConfirmRequests?.[0]).toMatchObject({
      replyId: "worker-reply",
      workerSessionId: "worker-session",
      workerRuntimeAgentId: "worker-agent",
      toolCalls: [toolCall],
      status: "waiting",
    });
  });

  it("restores detached external execution from its exact canonical child Session", async () => {
    const run = {
      run_id: "run-external",
      session_id: "leader-session",
      agent_id: "agent-1",
      agent_version_id: "version-1",
      status: "waiting_external",
      updated_at: "2026-09-10T00:00:01Z",
    } as unknown as FeedbackRunRecord;
    const action: RuntimePendingAction = {
      action_id: "action-external",
      session_id: "worker-session",
      runtime_agent_id: "worker-agent",
      run_id: "run-external",
      reply_id: "worker-reply",
      kind: "external",
      tool_call_id: "tool-external-root",
      tool_call_name: "browser",
      tool_call_state: "asking",
      tool_call_utf8_length: 128,
      tool_call_sha256: "3cc2eb17e64cf4d1f62699b4e60b54bf9fda03747807a4ac7e1b6ad998700fb2",
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const toolCall = {
      type: "tool_call" as const,
      id: "tool-external-root",
      name: "browser",
      input: '{"url":"https://example.invalid"}',
      state: "asking" as const,
    } satisfies AgentScopeToolCallBlock;
    const canonical = new Map([
      ["leader-session", []],
      ["worker-session", [message({ id: "worker-reply", content: [toolCall] })]],
    ]);
    const result = await messagesFromAgentScopeMessages([], "leader-session", [run], [action], canonical);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-external",
      sessionId: "leader-session",
      content: "",
    });
    expect(result[0].externalExecutionRequests?.[0]).toMatchObject({
      replyId: "worker-reply",
      workerSessionId: "worker-session",
      workerRuntimeAgentId: "worker-agent",
      toolCalls: [toolCall],
      status: "waiting",
    });
  });

  it("keeps a worker action locked when the child ToolCall fingerprint differs", async () => {
    const run = {
      run_id: "run-worker-mismatch", session_id: "leader-session", status: "waiting_human",
    } as unknown as FeedbackRunRecord;
    const action: RuntimePendingAction = {
      action_id: "action-mismatch", session_id: "worker-session", runtime_agent_id: "worker-agent",
      run_id: "run-worker-mismatch", reply_id: "worker-reply", kind: "human",
      tool_call_id: "tool-worker", tool_call_name: "Write", tool_call_state: "asking",
      tool_call_utf8_length: 101, tool_call_sha256: "a".repeat(64), status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };
    const canonical = new Map([
      ["leader-session", []],
      ["worker-session", [message({
        id: "worker-reply",
        content: [{ type: "tool_call", id: "tool-worker", name: "Write", input: '{"path":"report.md"}', state: "asking" }],
      })]],
    ]);

    const result = await messagesFromAgentScopeMessages([], "leader-session", [run], [action], canonical);

    expect(result).toHaveLength(1);
    expect(result[0].content).toContain("已锁定继续操作");
    expect(result[0].userConfirmRequests).toBeUndefined();
  });

  it("uses an authoritative external action without inventing a human-confirm card", async () => {
    const run = {
      run_id: "run-external-root",
      session_id: "session-1",
      status: "waiting_external",
      reply_ids: ["reply-external"],
    } as unknown as FeedbackRunRecord;
    const action: RuntimePendingAction = {
      action_id: "action-external-root",
      session_id: "session-1",
      runtime_agent_id: "agent-1",
      run_id: "run-external-root",
      reply_id: "reply-external",
      kind: "external",
      tool_call_id: "tool-external-root",
      tool_call_name: "browser",
      tool_call_state: "asking",
      tool_call_utf8_length: 128,
      tool_call_sha256: "3cc2eb17e64cf4d1f62699b4e60b54bf9fda03747807a4ac7e1b6ad998700fb2",
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const result = await messagesFromAgentScopeMessages([
      message({
        id: "reply-external",
        content: [{
          type: "tool_call",
          id: "tool-external-root",
          name: "browser",
          input: '{"url":"https://example.invalid"}',
          state: "asking",
        }],
      }),
    ], "session-1", [run], [action]);

    expect(result).toHaveLength(1);
    expect(result[0].userConfirmRequests).toBeUndefined();
    expect(result[0].externalExecutionRequests?.[0]).toMatchObject({
      replyId: "reply-external",
      status: "waiting",
    });
  });

  it("merges only exact-run native events into their canonical reply", () => {
    const event = (id: string, runId: string, replyId?: string): StreamLogEvent => ({
      id,
      event: "TEXT_BLOCK_DELTA",
      createdAt: "2026-09-10T00:00:00Z",
      data: { run_id: runId, payload: { ...(replyId ? { reply_id: replyId } : {}) } },
    });
    const replyA = event("event-a", "run-1", "reply-a");
    const replyB = event("event-b", "run-1", "reply-b");
    const runLevel = event("event-run", "run-1");
    const foreign = event("event-foreign", "run-other", "reply-b");
    const current: ChatMessage[] = [{
      id: "temporary",
      role: "assistant",
      content: "streaming",
      createdAt: "2026-09-10T00:00:00Z",
      sessionId: "session-1",
      runId: "run-1",
      events: [replyA, replyB, runLevel, foreign],
    }, {
      id: "foreign-session-message", role: "assistant", content: "", createdAt: "t", sessionId: "other-session",
      runId: "run-1", events: [event("foreign-session-event", "run-1", "reply-b")],
    }];
    const canonical: ChatMessage[] = [
      { id: "reply-a", role: "assistant", content: "A", createdAt: "t", runId: "run-1", sessionId: "session-1", events: [replyA] },
      { id: "reply-b", role: "assistant", content: "B", createdAt: "t", runId: "run-1", sessionId: "session-1", events: [] },
    ];

    const merged = mergeExactRunEventsIntoCanonicalMessages(current, canonical, "run-1", "reply-b");

    expect(terminalAssistantMessageId(canonical, "run-1")).toBe("reply-b");
    expect(merged[0].events?.map((item) => item.id)).toEqual(["event-a"]);
    expect(merged[1].events?.map((item) => item.id)).toEqual(["event-b", "event-run"]);
  });
});
