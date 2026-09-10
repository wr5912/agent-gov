import { describe, expect, it } from "vitest";
import { messagesFromAgentScopeMessages } from "./playgroundHistory";
import type { FeedbackRunRecord } from "./types/feedback";
import type { AgentScopeMessage, RuntimePendingAction } from "./types/runtime";

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
  it("reconstructs text, terminal outcome, and run identity from native messages", () => {
    const run = {
      run_id: "run-1",
      session_id: "session-1",
      agent_id: "agent-1",
      agent_version_id: "version-1",
      status: "succeeded",
      reply_ids: ["reply-1"],
      trace_id: "0123456789abcdef0123456789abcdef",
    } as unknown as FeedbackRunRecord;

    const result = messagesFromAgentScopeMessages([
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

  it("reconstructs an awaiting-permission card with the exact stored tool call", () => {
    const toolCall = {
      type: "tool_call" as const,
      id: "tool-1",
      name: "Write",
      input: '{"path":"report.md"}',
      state: "asking",
      suggested_rules: [{ tool: "Write" }],
    };
    const result = messagesFromAgentScopeMessages([
      message({ id: "reply-2", content: [toolCall] }),
    ], "session-1");

    expect(result[0].userConfirmRequests?.[0]).toMatchObject({
      requestId: "history:reply-2",
      replyId: "reply-2",
      status: "waiting",
    });
    expect(result[0].userConfirmRequests?.[0].toolCalls[0]).toBe(toolCall);
  });

  it("renders an error-only reply instead of dropping it", () => {
    const result = messagesFromAgentScopeMessages([
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

  it("reconstructs a worker HITL card from the durable AgentGov pending action", () => {
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
      tool_call: {
        type: "tool_call",
        id: "tool-worker",
        name: "Write",
        input: '{"file_path":"/workspace/outputs/report.md"}',
        state: "asking",
      },
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const result = messagesFromAgentScopeMessages([], "leader-session", [run], [action]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-worker",
      sessionId: "leader-session",
      content: "子智能体正在等待工具确认。",
    });
    expect(result[0].userConfirmRequests?.[0]).toMatchObject({
      requestId: "pending:run-worker:worker-session:worker-reply",
      replyId: "worker-reply",
      workerSessionId: "worker-session",
      status: "waiting",
    });
  });

  it("reconstructs a worker external-execution card from the durable pending action", () => {
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
      tool_call: {
        type: "tool_call",
        id: "tool-browser",
        name: "browser",
        input: '{"url":"https://example.invalid"}',
        state: "pending",
      },
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const result = messagesFromAgentScopeMessages([], "leader-session", [run], [action]);

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      runId: "run-external",
      sessionId: "leader-session",
      content: "子智能体正在等待外部执行结果。",
    });
    expect(result[0].externalExecutionRequests?.[0]).toMatchObject({
      requestId: "pending-external:run-external:worker-session:worker-reply",
      replyId: "worker-reply",
      workerSessionId: "worker-session",
      status: "waiting",
    });
  });

  it("uses an authoritative external action without inventing a human-confirm card", () => {
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
      tool_call: {
        type: "tool_call",
        id: "tool-external-root",
        name: "browser",
        input: '{"url":"https://example.invalid"}',
        state: "asking",
      },
      status: "pending",
      created_at: "2026-09-10T00:00:00Z",
    };

    const result = messagesFromAgentScopeMessages([
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
});
