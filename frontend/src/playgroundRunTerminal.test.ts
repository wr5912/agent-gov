import { describe, expect, it, vi } from "vitest";

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

describe("AgentGov run terminal fence", () => {
  it("keeps polling the exact run_id after Runtime reply completion", async () => {
    const getRun = vi.fn()
      .mockResolvedValueOnce(run({ status: "finalizing" }))
      .mockResolvedValueOnce(run({ status: "succeeded" }));
    const wait = vi.fn().mockResolvedValue(undefined);

    const terminal = await waitForAgentGovRunTerminal({
      runId: "run-exact",
      sessionId: "session-1",
      getRun,
      wait,
    });

    expect(terminal.status).toBe("succeeded");
    expect(getRun).toHaveBeenNthCalledWith(1, "run-exact", undefined);
    expect(getRun).toHaveBeenNthCalledWith(2, "run-exact", undefined);
    expect(wait).toHaveBeenCalledOnce();
  });

  it("fails closed when the precise run_id is missing", async () => {
    const getRun = vi.fn();

    await expect(waitForAgentGovRunTerminal({
      runId: undefined,
      sessionId: "session-1",
      getRun,
    })).rejects.toThrow("缺少精确的 AgentGov run_id");
    expect(getRun).not.toHaveBeenCalled();
  });

  it("rejects a response bound to another run or session", async () => {
    await expect(waitForAgentGovRunTerminal({
      runId: "run-exact",
      sessionId: "session-1",
      getRun: async () => run({ run_id: "run-other" }),
    })).rejects.toThrow("不匹配的 run_id");

    await expect(waitForAgentGovRunTerminal({
      runId: "run-exact",
      sessionId: "session-1",
      getRun: async () => run({ session_id: "session-other" }),
    })).rejects.toThrow("不属于当前 session_id");
  });

  it("derives the UI outcome only from a terminal AgentGov status", () => {
    expect(runOutcome(run({ status: "cancelled" }))).toBe("cancelled");
    expect(() => runOutcome(run({ status: "finalizing" }))).toThrow("不是终态");
  });
});
