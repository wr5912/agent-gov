import { describe, expect, it } from "vitest";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import {
  assistantWithOutcome,
  bindLogEventRunId,
  postExternalExecution,
  postUserConfirm,
  runContext,
} from "./playgroundRunHelpers";
import type {
  ChatMessage,
  RuntimeExternalExecutionRequest,
  RuntimeUserConfirmRequest,
} from "./types/runtime";

const options = {
  clientConfig: { apiBase: "http://runtime.invalid", apiKey: "test-key" },
  alertId: " alert-1 ",
  caseId: " ",
};

function turn(): PlaygroundActiveTurn {
  return {
    sessionId: "session-1",
    agentId: "agent-1",
    assistantMessageId: "message-1",
    operationId: "operation-1",
    controller: new AbortController(),
    completed: false,
    sealed: false,
    stopRequested: false,
    chatSubmitted: true,
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
      content: "运行已取消。",
      runOutcome: "cancelled",
      partial: false,
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

  it("keeps context normalization and stale-continuation guards unchanged", () => {
    expect(runContext(options)).toEqual({ alertId: "alert-1", caseId: undefined });
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
});
