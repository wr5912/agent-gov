import type { PlaygroundActiveTurn } from "./playgroundDetachedRun";
import { isTransientApiReadError } from "./api/readRecovery";
import type {
  PlaygroundRunAction,
  PlaygroundRunOutcome,
} from "./playgroundRunState";
import { runOutcome } from "./playgroundRunTerminal";
import type { FeedbackRunRecord } from "./types/feedback";

type ReconcilingAction = Extract<PlaygroundRunAction, { type: "reconciling" }>;
type TerminalAction = Extract<PlaygroundRunAction, { type: "terminal" }>;

export type PlaygroundMonitorFailureEffect =
  | { kind: "ignore" }
  | { kind: "reconcile"; message: string; action: ReconcilingAction };

export type PlaygroundTerminalEffect =
  | { kind: "ignore" }
  | { kind: "keep_monitoring" }
  | { kind: "reject"; message: string }
  | {
    kind: "release";
    outcome: PlaygroundRunOutcome;
    action: TerminalAction;
  };

export function isCurrentPlaygroundTurn(
  activeOperationId: string | null,
  turn: Pick<PlaygroundActiveTurn, "operationId">,
): boolean {
  return activeOperationId === turn.operationId;
}

export function isMutablePlaygroundTurn(
  activeOperationId: string | null,
  turn: Pick<PlaygroundActiveTurn, "operationId" | "sealed">,
): boolean {
  return isCurrentPlaygroundTurn(activeOperationId, turn) && !turn.sealed;
}

export function planPlaygroundMonitorFailure(
  activeOperationId: string | null,
  turn: Pick<PlaygroundActiveTurn, "operationId" | "sealed">,
  error: unknown,
): PlaygroundMonitorFailureEffect {
  if (!isMutablePlaygroundTurn(activeOperationId, turn)) return { kind: "ignore" };
  const detail = error instanceof Error ? error.message : String(error);
  const message = isTransientApiReadError(error)
    ? `AgentGov run 终态监控暂时失败，将继续按精确 run_id 重试：${detail}`
    : `AgentGov run 精确状态读取失败，运行保持锁定并继续重试：${detail}`;
  return {
    kind: "reconcile",
    message,
    action: { type: "reconciling", operationId: turn.operationId, message },
  };
}

export function planPlaygroundTerminalEffect(
  activeOperationId: string | null,
  turn: Pick<
    PlaygroundActiveTurn,
    "completed" | "operationId" | "runtimeRunId" | "sealed" | "sessionId"
  >,
  run: FeedbackRunRecord,
): PlaygroundTerminalEffect {
  if (turn.completed || !isMutablePlaygroundTurn(activeOperationId, turn)) {
    return { kind: "ignore" };
  }
  const runId = turn.runtimeRunId?.trim();
  if (!runId) {
    return { kind: "reject", message: "缺少精确的 AgentGov run_id，拒绝释放运行锁。" };
  }
  if (run.run_id !== runId) {
    return {
      kind: "reject",
      message: `AgentGov 返回了不匹配的 run_id：期望 ${runId}。`,
    };
  }
  if (run.session_id !== turn.sessionId) {
    return {
      kind: "reject",
      message: `AgentGov run ${runId} 不属于当前 session_id。`,
    };
  }
  if (
    run.status !== "succeeded"
    && run.status !== "failed"
    && run.status !== "cancelled"
    && run.status !== "interrupted"
  ) {
    return { kind: "keep_monitoring" };
  }
  const outcome = runOutcome(run);
  return {
    kind: "release",
    outcome,
    action: { type: "terminal", operationId: turn.operationId, outcome },
  };
}
