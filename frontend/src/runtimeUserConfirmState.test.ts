import { describe, expect, it } from "vitest";
import {
  buildUserConfirmSubmission,
  cancelWaitingUserConfirmRequests,
  clearProjectedUserConfirmRequest,
  mergeUserConfirmRequests,
  userConfirmRequestsFromEvent,
} from "./runtimeUserConfirmState";
import type { AgentScopeAgentEvent, AgentScopeToolCallBlock, ChatMessage } from "./types/runtime";

function toolCall(id = "tool-1"): AgentScopeToolCallBlock {
  return {
    type: "tool_call",
    id,
    name: "Read",
    input: '{"path":"AGENT.md"}',
    state: "asking",
    suggested_rules: [{ tool: "Read" }],
  };
}

describe("AgentScope user confirmation state", () => {
  it("maps run approval to a top-level scope without browser-supplied rules", () => {
    const request = {
      requestId: "confirm-1",
      replyId: "reply-1",
      toolCalls: [toolCall()],
      status: "waiting" as const,
    };

    const submission = buildUserConfirmSubmission(request, "allow_for_run");

    expect(submission.confirmationScope).toBe("run");
    expect(submission.input.confirm_results).toEqual([{ confirmed: true, tool_call: request.toolCalls[0] }]);
    expect(submission.input.confirm_results[0]).not.toHaveProperty("rules");
    expect(buildUserConfirmSubmission(request, "deny")).toMatchObject({
      confirmationScope: "once",
      input: { confirm_results: [{ confirmed: false }] },
    });
  });

  it("keeps every native tool call intact in one reply-scoped request", () => {
    const first = toolCall("tool-1");
    const second = toolCall("tool-2");
    const event: AgentScopeAgentEvent = {
      id: "confirm-1",
      created_at: "2026-09-09T00:00:00Z",
      metadata: {},
      type: "REQUIRE_USER_CONFIRM",
      reply_id: "reply-1",
      tool_calls: [first, second],
    };

    const requests = userConfirmRequestsFromEvent(event);

    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({ requestId: "confirm-1", replyId: "reply-1", status: "waiting" });
    expect(requests[0].toolCalls[0]).toBe(first);
    expect(requests[0].toolCalls[1]).toBe(second);
  });

  it("does not reopen a resolved request when the event is observed again", () => {
    const resolved = {
      requestId: "confirm-1",
      replyId: "reply-1",
      toolCalls: [toolCall()],
      status: "resolved" as const,
      decision: "allow_once" as const,
    };
    const repeated = { ...resolved, status: "waiting" as const, decision: undefined };

    expect(mergeUserConfirmRequests([resolved], [repeated])).toEqual([resolved]);
  });

  it("tracks and clears only the matching projected Team worker request", () => {
    const event: AgentScopeAgentEvent = {
      id: "confirm-1",
      created_at: "2026-09-09T00:00:00Z",
      metadata: {},
      type: "REQUIRE_USER_CONFIRM",
      reply_id: "reply-1",
      tool_calls: [toolCall()],
    };
    const first = userConfirmRequestsFromEvent(event, "worker-1")[0];
    const second = { ...first, requestId: "confirm-2", workerSessionId: "worker-2" };

    expect(first.workerSessionId).toBe("worker-1");
    expect(clearProjectedUserConfirmRequest([first, second], "worker-1", "reply-1")).toEqual([second]);
  });

  it("marks only waiting requests as interrupted when a reply is cancelled", () => {
    const messages: ChatMessage[] = [{
      id: "assistant-1",
      role: "assistant",
      content: "partial",
      createdAt: "2026-09-09T00:00:00Z",
      userConfirmRequests: [{
        requestId: "confirm-1",
        replyId: "reply-1",
        toolCalls: [toolCall()],
        status: "waiting",
      }],
    }];

    const next = cancelWaitingUserConfirmRequests(messages, "assistant-1", "2026-09-09T00:01:00Z");

    expect(next[0].userConfirmRequests?.[0]).toMatchObject({
      status: "cancelled",
      decision: "runtime_interrupted",
      resolvedAt: "2026-09-09T00:01:00Z",
    });
  });
});
