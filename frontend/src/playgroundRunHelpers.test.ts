import { describe, expect, it } from "vitest";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import {
  assistantWithOutcome,
  bindLogEventRunId,
  buildInitialChatSubmission,
  isDefinitiveContinuationRejection,
  postExternalExecution,
  postUserConfirm,
  preparedContinuationTarget,
  prepareExternalExecutionContinuation,
  prepareUserConfirmContinuation,
  rememberContinuationInput,
} from "./playgroundRunHelpers";
import { ApiRequestError } from "./api/request";
import type {
  AgentScopeToolCallBlock,
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmRequest,
} from "./types/runtime";

const options = {
  clientConfig: { apiBase: "http://runtime.invalid", apiKey: "test-key" },
};

function turn(): PlaygroundActiveTurn {
  return {
    sessionId: "session-1",
    agentId: "agent-1",
    userMessageId: "user-message-1",
    assistantMessageId: "message-1",
    operationId: "operation-1",
    controller: new AbortController(),
    completed: false,
    sealed: false,
    stopRequested: false,
    chatSubmitted: true,
  };
}

function toolCall(state: "asking" | "pending" = "asking"): AgentScopeToolCallBlock {
  return {
    type: "tool_call",
    id: "tool-1",
    name: "Read",
    input: '{"path":"AGENT.md"}',
    state,
    suggested_rules: state === "asking" ? [{
      tool_name: "Read",
      rule_content: "reports/**",
      behavior: "allow",
      source: "workspace_policy.ask_tools",
    }] : undefined,
  };
}

describe("playground run helpers", () => {
  it("preserves terminal assistant projection semantics", () => {
    const message: ChatMessage = {
      id: "message-1",
      role: "assistant",
      content: "partial result",
      createdAt: "2026-09-10T00:00:00Z",
      controlError: "transport failed",
    };

    expect(assistantWithOutcome(message, "interrupted")).toMatchObject({
      content: "partial result",
      runOutcome: "interrupted",
      partial: true,
      controlError: undefined,
    });
    expect(assistantWithOutcome({ ...message, content: "" }, "cancelled")).toMatchObject({
      content: "",
      runOutcome: "cancelled",
      partial: false,
    });
    const executionError = { type: "upstream" as const, message: "upstream unavailable" };
    expect(assistantWithOutcome({ ...message, executionError }, "failed")).toMatchObject({
      content: "partial result",
      executionError,
      controlError: undefined,
    });
  });

  it("binds only provisional trace events to the exact run", () => {
    const pending = {
      id: "event-1",
      event: "tool_use",
      createdAt: "2026-09-10T00:00:00Z",
      data: { run_id: "pending", tool: "Read" },
    };
    const bound = bindLogEventRunId(pending, "run-1");

    expect(bound.data).toEqual({ run_id: "run-1", tool: "Read" });
    expect(bindLogEventRunId({ ...pending, data: { run_id: "run-existing" } }, "run-1").data)
      .toEqual({ run_id: "run-existing" });
  });

  it("keeps stale-continuation guards without sending governance fields in chat", () => {
    const activeTurn = turn();

    expect(() => postUserConfirm(
      options,
      activeTurn,
      {} as RuntimeUserConfirmRequest,
      "deny",
    )).toThrow("缺少精确的 AgentGov run_id");
    expect(() => postExternalExecution(
      options,
      activeTurn,
      {} as RuntimeExternalExecutionRequest,
      "success",
      {},
    )).toThrow("缺少精确的 AgentGov run_id");
  });

  it("reuses the original native Msg.id for retry and distinguishes another identical-text turn", () => {
    const active = turn();
    const first = buildInitialChatSubmission(active, "你好");
    expect(buildInitialChatSubmission(active, "你好")).toEqual(first);
    expect(first.id).toBe(active.userMessageId);
    expect(buildInitialChatSubmission({ ...turn(), userMessageId: "user-message-2" }, "你好").id).not.toBe(first.id);
    expect(() => buildInitialChatSubmission({ ...active, userMessageId: undefined }, "你好")).toThrow("Msg.id");
  });

  it("reuses a native confirmation Event.id only for the same pending action and decision", () => {
    const active = turn();
    const input = { type: "USER_CONFIRM_RESULT" as const, reply_id: "reply-1", confirm_results: [] };
    const first = rememberContinuationInput(active, "request-1:once", input);
    expect(first.id).toMatch(/^event_/);
    expect(rememberContinuationInput(active, "request-1:once", { ...input })).toBe(first);
    expect(rememberContinuationInput(active, "request-1:run", input).id).not.toBe(first.id);
    expect(rememberContinuationInput(active, "request-2:once", input).id).not.toBe(first.id);
    expect(rememberContinuationInput(active, "request-1:once", { ...input, reply_id: "reply-changed" }).id).not.toBe(first.id);
  });

  it("does not reuse an external result Event.id after the actual output changes", () => {
    const active = turn();
    const input = {
      type: "EXTERNAL_EXECUTION_RESULT" as const, reply_id: "reply-1",
      execution_results: [{ type: "tool_result" as const, id: "tool-1", name: "external", output: "first", state: "success" as const }],
    };
    const first = rememberContinuationInput(active, "external-1", input);
    expect(rememberContinuationInput(active, "external-1", input)).toBe(first);
    const changed = { ...input, execution_results: [{ ...input.execution_results[0], output: "second" }] };
    expect(rememberContinuationInput(active, "external-1", changed).id).not.toBe(first.id);
  });

  it("prepares confirmation retries with one stable native Event.id and explicit operation kind", () => {
    const active = { ...turn(), runtimeRunId: "run-1" };
    const request: RuntimeUserConfirmRequest = {
      requestId: "confirm-1",
      replyId: "reply-1",
      workerSessionId: "worker-session-1",
      workerRuntimeAgentId: "worker-agent-1",
      toolCalls: [toolCall()],
      status: "waiting",
    };

    const first = prepareUserConfirmContinuation(active, request, "allow_for_run");
    const retried = prepareUserConfirmContinuation(active, request, "allow_for_run");

    expect(first).toMatchObject({
      operationKind: "user_confirmation",
      confirmationScope: "run",
      targetSessionId: "worker-session-1",
      targetRuntimeAgentId: "worker-agent-1",
    });
    expect(first.input.id).toMatch(/^event_/);
    expect(retried.input).toBe(first.input);
    expect(preparedContinuationTarget(active, first)).toEqual({
      sessionId: "worker-session-1",
      runtimeAgentId: "worker-agent-1",
      expectedRootSessionId: "session-1",
    });
    expect(prepareUserConfirmContinuation(active, request, "deny").input.id).not.toBe(first.input.id);
  });

  it("prepares external retries with one stable native Event.id until the output changes", () => {
    const active = { ...turn(), runtimeRunId: "run-1" };
    const request: RuntimeExternalExecutionRequest = {
      requestId: "external-1",
      replyId: "reply-worker-1",
      workerSessionId: "worker-session-1",
      workerRuntimeAgentId: "worker-agent-1",
      toolCalls: [toolCall("pending")],
      status: "waiting",
    };

    const first = prepareExternalExecutionContinuation(active, request, "success", { "tool-1": "result" });
    const retried = prepareExternalExecutionContinuation(active, request, "success", { "tool-1": "result" });

    expect(first).toMatchObject({
      operationKind: "external_execution",
      targetSessionId: "worker-session-1",
      targetRuntimeAgentId: "worker-agent-1",
    });
    expect(first.input.id).toMatch(/^event_/);
    expect(retried.input).toBe(first.input);
    expect(prepareExternalExecutionContinuation(
      active, request, "success", { "tool-1": "changed" },
    ).input.id).not.toBe(first.input.id);
  });

  it("refuses a worker continuation when only one side of its durable binding is present", () => {
    const active = { ...turn(), runtimeRunId: "run-1" };
    const request: RuntimeUserConfirmRequest = {
      requestId: "confirm-incomplete",
      replyId: "reply-worker",
      workerSessionId: "worker-session-1",
      toolCalls: [toolCall()],
      status: "waiting",
    };

    expect(() => prepareUserConfirmContinuation(active, request, "allow_once"))
      .toThrow("缺少精确的 Session/Runtime Agent 绑定");
  });

  it("unlocks only after a definitive HTTP rejection, not an ambiguous transport failure", () => {
    expect(isDefinitiveContinuationRejection(
      new ApiRequestError("http", "invalid", { status: 422 }),
    )).toBe(true);
    expect(isDefinitiveContinuationRejection(
      new ApiRequestError("http", "unavailable", { status: 503 }),
    )).toBe(false);
    expect(isDefinitiveContinuationRejection(
      new ApiRequestError("network", "connection reset"),
    )).toBe(false);
  });
});
