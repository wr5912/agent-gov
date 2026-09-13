import { describe, expect, it } from "vitest";

import { runOutcome, waitForAgentGovRunTerminal } from "./playgroundRunTerminal";
import type { FeedbackRunRecord } from "./types/feedback";

function run(overrides: Partial<FeedbackRunRecord> = {}): FeedbackRunRecord {
  return {
    run_id: "run-exact",
    session_id: "session-1",
    agent_id: "agent-1",
    agent_version_id: "version-1",
    harness_digest: "harness-1",
    runtime_agent_id: "runtime-agent-1",
    status: "running",
    trace_status: "pending",
    team_generation: 0,
    root_persisted_team_generation: 0,
    created_at: "2026-09-09T00:00:00Z",
    updated_at: "2026-09-09T00:00:00Z",
    ...overrides,
  };
}

describe("AgentGov run 终态围栏", () => {
  it("不从非终态推导 UI 结果", () => {
    expect(runOutcome(run({ status: "interrupted" }))).toBe("interrupted");
    expect(() => runOutcome(run({ status: "finalizing" }))).toThrow("不是终态");
  });

  it("null 轮询上限可跨越长时间 HITL 直到精确 run 终态", async () => {
    let reads = 0;
    const terminal = await waitForAgentGovRunTerminal({
      runId: "run-exact",
      sessionId: "session-1",
      maxAttempts: null,
      wait: async () => undefined,
      getRun: async () => {
        reads += 1;
        return run({ status: reads > 125 ? "succeeded" : "waiting_human" });
      },
    });

    expect(reads).toBe(126);
    expect(terminal.status).toBe("succeeded");
  });
});
