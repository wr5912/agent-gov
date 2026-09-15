import { describe, expect, it } from "vitest";
import {
  buildUserConfirmSubmission,
  cancelWaitingUserConfirmRequests,
  clearProjectedUserConfirmRequest,
  mergeUserConfirmRequests,
  runtimeRunPermissionScopes,
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
    suggested_rules: [{
      tool_name: "Read",
      rule_content: "reports/**",
      behavior: "allow",
      source: "workspace_policy.ask_tools",
    }],
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

  it("shows only bounded Runtime-authored run permission scopes", () => {
    const request = {
      requestId: "confirm-1",
      replyId: "reply-1",
      toolCalls: [toolCall()],
      status: "waiting" as const,
    };

    expect(runtimeRunPermissionScopes(request)).toEqual([
      { toolName: "Read", ruleContent: "reports/**" },
    ]);

    for (const ruleContent of [null, "", "*", "**", "./**", "/**/*"]) {
      const unsafe = toolCall();
      unsafe.suggested_rules = [{
        tool_name: "Read",
        rule_content: ruleContent,
        behavior: "allow",
        source: "workspace_policy.ask_tools",
      }];
      expect(runtimeRunPermissionScopes({ ...request, toolCalls: [unsafe] })).toBeUndefined();
    }

    for (const [toolName, ruleContent] of [
      ["Bash", "*a*"],
      ["mcp__security__lookup", "incident-123"],
      ["Glob", "reports/**"],
      ["Read", "**/secret.txt"],
      ["Write", "../outputs/**"],
      ["Unknown", "reports/**"],
    ]) {
      const unsafe = toolCall();
      unsafe.name = toolName;
      unsafe.suggested_rules = [{
        tool_name: toolName,
        rule_content: ruleContent,
        behavior: "allow",
        source: "workspace_policy.ask_tools",
      }];
      expect(runtimeRunPermissionScopes({ ...request, toolCalls: [unsafe] })).toBeUndefined();
    }
  });

  it("disables run scope when any suggested rule is missing or mismatched", () => {
    const missing = toolCall();
    delete missing.suggested_rules;
    const mismatched = toolCall();
    mismatched.suggested_rules = [{
      tool_name: "Write",
      rule_content: "reports/**",
      behavior: "allow",
      source: "workspace_policy.ask_tools",
    }];
    const untrustedSource = toolCall();
    untrustedSource.suggested_rules = [{
      tool_name: "Read",
      rule_content: "reports/**",
      behavior: "allow",
      source: "suggested",
    }];

    expect(runtimeRunPermissionScopes({
      requestId: "confirm-1",
      replyId: "reply-1",
      toolCalls: [missing],
      status: "waiting",
    })).toBeUndefined();
    expect(runtimeRunPermissionScopes({
      requestId: "confirm-2",
      replyId: "reply-1",
      toolCalls: [mismatched],
      status: "waiting",
    })).toBeUndefined();
    expect(runtimeRunPermissionScopes({
      requestId: "confirm-3",
      replyId: "reply-1",
      toolCalls: [untrustedSource],
      status: "waiting",
    })).toBeUndefined();
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

  it("does not duplicate one ledger action when live and history projections use different request IDs", () => {
    const live = {
      requestId: "live-event-id",
      replyId: "reply-1",
      workerSessionId: "worker-1",
      workerRuntimeAgentId: "worker-agent-1",
      toolCalls: [toolCall()],
      status: "waiting" as const,
    };
    const restored = { ...live, requestId: "pending:run:worker:reply" };

    expect(mergeUserConfirmRequests([live], [restored])).toEqual([live]);
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
    const first = userConfirmRequestsFromEvent(event, "worker-1", "worker-agent-1")[0];
    const second = {
      ...first,
      requestId: "confirm-2",
      workerSessionId: "worker-2",
      workerRuntimeAgentId: "worker-agent-2",
    };

    expect(first.workerSessionId).toBe("worker-1");
    expect(first.workerRuntimeAgentId).toBe("worker-agent-1");
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
