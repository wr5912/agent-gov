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
      content: "运行失败：\nmodel unavailable",
      runOutcome: "failed",
      partial: false,
    });
  });

  it("locks a detached worker HITL action when canonical ToolCall is unavailable", async () => {
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
      run_id: "run-worker",
      reply_id: "worker-reply",
      kind: "human",
      tool_call_id: "tool-worker",
      tool_call_name: "Write",
      tool_call_state: "asking",
      tool_call_utf8_length: 128,
      tool_call_sha256: "a".repeat(64),
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const result = await messagesFromAgentScopeMessages([], "leader-session", [run], [action]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-worker",
      sessionId: "leader-session",
      content: expect.stringContaining("已锁定继续操作"),
    });
    expect(result[0].userConfirmRequests).toBeUndefined();
  });

  it("locks detached external execution when canonical ToolCall is unavailable", async () => {
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
      run_id: "run-external",
      reply_id: "worker-reply",
      kind: "external",
      tool_call_id: "tool-browser",
      tool_call_name: "browser",
      tool_call_state: "pending",
      tool_call_utf8_length: 128,
      tool_call_sha256: "b".repeat(64),
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const result = await messagesFromAgentScopeMessages([], "leader-session", [run], [action]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-external",
      sessionId: "leader-session",
      content: expect.stringContaining("已锁定继续操作"),
    });
    expect(result[0].externalExecutionRequests).toBeUndefined();
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
      runId: "run-1",
      events: [replyA, replyB, runLevel, foreign],
    }];
    const canonical: ChatMessage[] = [
      { id: "reply-a", role: "assistant", content: "A", createdAt: "t", runId: "run-1", events: [replyA] },
      { id: "reply-b", role: "assistant", content: "B", createdAt: "t", runId: "run-1", events: [] },
    ];

    const merged = mergeExactRunEventsIntoCanonicalMessages(current, canonical, "run-1", "reply-b");

    expect(terminalAssistantMessageId(canonical, "run-1")).toBe("reply-b");
    expect(merged[0].events?.map((item) => item.id)).toEqual(["event-a"]);
    expect(merged[1].events?.map((item) => item.id)).toEqual(["event-b", "event-run"]);
  });
});
