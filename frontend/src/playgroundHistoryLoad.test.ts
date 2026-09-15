import { describe, expect, it } from "vitest";
import { pendingSessionReadTargets } from "./playgroundHistoryLoad";
import type { RuntimePendingAction } from "./types/runtime";

function action(overrides: Partial<RuntimePendingAction> = {}): RuntimePendingAction {
  return {
    action_id: "action-1",
    session_id: "worker-session",
    runtime_agent_id: "worker-agent",
    run_id: "run-1",
    reply_id: "reply-1",
    kind: "human",
    tool_call_id: "tool-1",
    tool_call_name: "Write",
    tool_call_state: "asking",
    tool_call_utf8_length: 100,
    tool_call_sha256: "a".repeat(64),
    status: "pending",
    created_at: "2026-09-14T00:00:00Z",
    ...overrides,
  };
}

describe("pending canonical Session reads", () => {
  it("deduplicates exact root and worker Runtime bindings", () => {
    expect(pendingSessionReadTargets("root-session", "root-agent", [
      action(),
      action({ action_id: "action-2", tool_call_id: "tool-2" }),
      action({ action_id: "action-root", session_id: "root-session", runtime_agent_id: "root-agent" }),
    ])).toEqual([
      { sessionId: "root-session", runtimeAgentId: "root-agent" },
      { sessionId: "worker-session", runtimeAgentId: "worker-agent" },
    ]);
  });

  it("rejects missing or conflicting durable Session bindings", () => {
    expect(() => pendingSessionReadTargets("root-session", "root-agent", [
      action({ runtime_agent_id: "" }),
    ])).toThrow("缺少精确");
    expect(() => pendingSessionReadTargets("root-session", "root-agent", [
      action(),
      action({ action_id: "action-2", runtime_agent_id: "other-worker-agent" }),
    ])).toThrow("多个 Runtime Agent");
    expect(() => pendingSessionReadTargets("root-session", "root-agent", [
      action({ session_id: "root-session", runtime_agent_id: "other-root-agent" }),
    ])).toThrow("多个 Runtime Agent");
  });
});
