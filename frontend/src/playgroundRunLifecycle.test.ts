import { describe, expect, it } from "vitest";
import { ApiRequestError } from "./api/request";
import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import {
  planPlaygroundMonitorFailure,
  planPlaygroundTerminalEffect,
} from "./playgroundRunLifecycle";
import {
  isPlaygroundRunLocked,
  playgroundRunReducer,
  type PlaygroundRunState,
} from "./playgroundRunState";
import type { FeedbackRunRecord } from "./types/feedback";

type LifecycleTurn = Pick<
  PlaygroundActiveTurn,
  "completed" | "operationId" | "runtimeRunId" | "sealed" | "sessionId"
>;

function turn(overrides: Partial<LifecycleTurn> = {}): LifecycleTurn {
  return {
    completed: false,
    operationId: "operation-1",
    runtimeRunId: "run-exact",
    sealed: false,
    sessionId: "session-1",
    ...overrides,
  };
}

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
    created_at: "2026-09-14T00:00:00Z",
    updated_at: "2026-09-14T00:00:00Z",
    ...overrides,
  };
}

const runningState: PlaygroundRunState = {
  phase: "running",
  operationId: "operation-1",
  runId: "run-exact",
  sessionId: "session-1",
  source: "local",
};

describe("Playground 生命周期 effect planner", () => {
  it("monitor error 进入 reconciling 后保持发送锁，恢复监控也不提前释放", () => {
    const effect = planPlaygroundMonitorFailure(
      "operation-1",
      turn(),
      new Error("GET run 暂时失败"),
    );

    expect(effect.kind).toBe("reconcile");
    if (effect.kind !== "reconcile") throw new Error("期望 reconcile effect");
    const reconciling = playgroundRunReducer(runningState, effect.action);
    expect(reconciling.phase).toBe("reconciling");
    expect(isPlaygroundRunLocked(reconciling)).toBe(true);

    const recovered = playgroundRunReducer(reconciling, {
      type: "monitor_recovered",
      operationId: "operation-1",
    });
    expect(recovered.phase).toBe("running");
    expect(isPlaygroundRunLocked(recovered)).toBe(true);
  });

  it("永久 read failure 明确保持锁定，不伪装成瞬态故障", () => {
    const effect = planPlaygroundMonitorFailure(
      "operation-1",
      turn(),
      new ApiRequestError("http", "pending actions forbidden", { status: 403 }),
    );

    expect(effect.kind).toBe("reconcile");
    if (effect.kind !== "reconcile") throw new Error("期望 reconcile effect");
    expect(effect.message).toContain("精确状态读取失败");
    expect(effect.message).toContain("pending actions forbidden");
    expect(effect.message).not.toContain("暂时失败");
    expect(isPlaygroundRunLocked(playgroundRunReducer(runningState, effect.action))).toBe(true);
  });

  it("瞬态 API read failure 保留自动重试语义", () => {
    const effect = planPlaygroundMonitorFailure(
      "operation-1",
      turn(),
      new ApiRequestError("http", "service unavailable", { status: 503 }),
    );

    expect(effect.kind).toBe("reconcile");
    if (effect.kind !== "reconcile") throw new Error("期望 reconcile effect");
    expect(effect.message).toContain("暂时失败");
    expect(effect.message).toContain("继续按精确 run_id 重试");
  });

  it.each(["succeeded", "failed", "cancelled", "interrupted"] as const)("只有精确 %s 终态产生释放 effect", (status) => {
    const effect = planPlaygroundTerminalEffect(
      "operation-1",
      turn(),
      run({ status }),
    );

    expect(effect.kind).toBe("release");
    if (effect.kind !== "release") throw new Error("期望 release effect");
    const released = playgroundRunReducer(runningState, effect.action);
    expect(released).toEqual({
      phase: "idle",
      lastOutcome: status,
      lastRunId: "run-exact",
      lastSessionId: "session-1",
    });
    expect(isPlaygroundRunLocked(released)).toBe(false);
  });

  it("同一 run 的非终态只继续监控，不产生 terminal action", () => {
    expect(planPlaygroundTerminalEffect(
      "operation-1",
      turn(),
      run({ status: "finalizing" }),
    )).toEqual({ kind: "keep_monitoring" });
  });

  it.each([
    ["缺 run_id", turn({ runtimeRunId: undefined }), run({ status: "succeeded" })],
    ["错 run_id", turn(), run({ run_id: "run-other", status: "succeeded" })],
    ["错 session_id", turn(), run({ session_id: "session-other", status: "succeeded" })],
  ] as const)("%s 拒绝释放", (_label, activeTurn, observedRun) => {
    const effect = planPlaygroundTerminalEffect("operation-1", activeTurn, observedRun);

    expect(effect.kind).toBe("reject");
    expect(effect).not.toHaveProperty("action");
  });

  it("stale、sealed 与已完成 turn 的监控错误和晚到终态都是 no-op", () => {
    const terminal = run({ status: "succeeded" });

    expect(planPlaygroundMonitorFailure("operation-other", turn(), new Error("late")))
      .toEqual({ kind: "ignore" });
    expect(planPlaygroundTerminalEffect("operation-other", turn(), terminal))
      .toEqual({ kind: "ignore" });
    expect(planPlaygroundMonitorFailure("operation-1", turn({ sealed: true }), new Error("late")))
      .toEqual({ kind: "ignore" });
    expect(planPlaygroundTerminalEffect("operation-1", turn({ sealed: true }), terminal))
      .toEqual({ kind: "ignore" });
    expect(planPlaygroundTerminalEffect("operation-1", turn({ completed: true }), terminal))
      .toEqual({ kind: "ignore" });
  });
});
