import { describe, expect, it } from "vitest";
import {
  buildExternalExecutionSubmission,
  externalExecutionRequestsFromEvent,
} from "./runtimeExternalExecutionState";
import type { AgentScopeAgentEvent } from "./types/runtime";

describe("AgentScope external execution continuation", () => {
  it("projects the exact native request and preserves worker Session identity", () => {
    const event = {
      id: "external-event-1",
      created_at: "2026-09-10T00:00:00Z",
      metadata: {},
      type: "REQUIRE_EXTERNAL_EXECUTION",
      reply_id: "reply-worker",
      tool_calls: [{
        type: "tool_call",
        id: "tool-browser",
        name: "browser",
        input: '{"url":"https://example.invalid"}',
        state: "pending",
      }],
    } satisfies AgentScopeAgentEvent;

    const requests = externalExecutionRequestsFromEvent(event, "worker-session");

    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({
      requestId: "external-event-1",
      replyId: "reply-worker",
      workerSessionId: "worker-session",
      status: "waiting",
    });
    expect(requests[0].toolCalls).toEqual(event.tool_calls);
  });

  it("builds an exact EXTERNAL_EXECUTION_RESULT without changing tool identity", () => {
    const request = externalExecutionRequestsFromEvent({
      id: "external-event-1",
      created_at: "2026-09-10T00:00:00Z",
      metadata: {},
      type: "REQUIRE_EXTERNAL_EXECUTION",
      reply_id: "reply-1",
      tool_calls: [{ type: "tool_call", id: "tool-1", name: "browser", input: "{}" }],
    })[0];

    expect(buildExternalExecutionSubmission(request, "success", { "tool-1": "external output" })).toEqual({
      type: "EXTERNAL_EXECUTION_RESULT",
      reply_id: "reply-1",
      execution_results: [{
        type: "tool_result",
        id: "tool-1",
        name: "browser",
        output: "external output",
        state: "success",
      }],
    });
  });
});
