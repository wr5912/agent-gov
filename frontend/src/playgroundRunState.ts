export type PlaygroundRunPhase =
  | "idle"
  | "starting"
  | "running"
  | "awaiting_input"
  | "cancelling_pending_handle"
  | "cancelling"
  | "reconciling";

export type PlaygroundRunOutcome = "succeeded" | "failed" | "cancelled" | "interrupted";

export interface PlaygroundRunState {
  phase: PlaygroundRunPhase;
  operationId?: string;
  sessionId?: string;
  runId?: string;
  source?: "local" | "detached";
  lastOutcome?: PlaygroundRunOutcome;
  lastRunId?: string;
  lastSessionId?: string;
  controlError?: string;
}

export type PlaygroundRunAction =
  | {
    type: "start";
    operationId: string;
    sessionId: string;
  }
  | {
    type: "observe_backend_run";
    operationId: string;
    sessionId: string;
    runId: string;
  }
  | {
    type: "run_handle";
    operationId: string;
    sessionId: string;
    runId: string;
  }
  | {
    type: "awaiting_input";
    operationId: string;
  }
  | {
    type: "input_resolved";
    operationId: string;
  }
  | {
    type: "stop_requested";
    operationId: string;
  }
  | {
    type: "reconciling";
    operationId: string;
    message: string;
  }
  | {
    type: "terminal";
    operationId: string;
    outcome: PlaygroundRunOutcome;
  }
  | {
    type: "reset";
  };

export const initialPlaygroundRunState: PlaygroundRunState = { phase: "idle" };

export function playgroundRunReducer(
  state: PlaygroundRunState,
  action: PlaygroundRunAction,
): PlaygroundRunState {
  if (action.type === "reset") return initialPlaygroundRunState;
  if (action.type === "start") {
    if (state.phase !== "idle") return state;
    return {
      phase: "starting",
      operationId: action.operationId,
      sessionId: action.sessionId,
      source: "local",
      lastOutcome: state.lastOutcome,
      lastRunId: state.lastRunId,
      lastSessionId: state.lastSessionId,
    };
  }
  if (action.type === "observe_backend_run") {
    if (state.phase !== "idle") return state;
    return {
      phase: "running",
      operationId: action.operationId,
      sessionId: action.sessionId,
      runId: action.runId,
      source: "detached",
      lastOutcome: state.lastOutcome,
      lastRunId: state.lastRunId,
      lastSessionId: state.lastSessionId,
    };
  }
  if (state.operationId !== action.operationId) return state;

  if (action.type === "run_handle") {
    if (state.sessionId && state.sessionId !== action.sessionId) return state;
    return {
      ...state,
      phase: state.phase === "cancelling_pending_handle" || state.phase === "cancelling"
        ? "cancelling"
        : "running",
      sessionId: action.sessionId,
      runId: action.runId,
      controlError: undefined,
    };
  }
  if (action.type === "awaiting_input") {
    if (state.phase !== "running") return state;
    return { ...state, phase: "awaiting_input" };
  }
  if (action.type === "input_resolved") {
    if (state.phase !== "awaiting_input") return state;
    return { ...state, phase: "running" };
  }
  if (action.type === "stop_requested") {
    if (!isPlaygroundRunLocked(state)) return state;
    return {
      ...state,
      phase: state.runId ? "cancelling" : "cancelling_pending_handle",
      controlError: undefined,
    };
  }
  if (action.type === "reconciling") {
    return { ...state, phase: "reconciling", controlError: action.message };
  }
  if (action.type === "terminal") {
    return {
      phase: "idle",
      lastOutcome: action.outcome,
      lastRunId: state.runId,
      lastSessionId: state.sessionId,
    };
  }
  return state;
}

export function isPlaygroundRunLocked(state: PlaygroundRunState): boolean {
  return state.phase !== "idle";
}

export function canStopPlaygroundRun(state: PlaygroundRunState): boolean {
  if (!isPlaygroundRunLocked(state)) return false;
  return state.phase !== "cancelling" && state.phase !== "cancelling_pending_handle";
}

export function canSubmitPlaygroundUserInput(state: PlaygroundRunState): boolean {
  return state.phase === "idle" || state.phase === "awaiting_input";
}

export function playgroundRunStatusText(state: PlaygroundRunState): string {
  if (state.phase === "cancelling_pending_handle") return "等待运行句柄…";
  if (state.phase === "cancelling") return "停止中…";
  if (state.phase === "reconciling") return "状态待核对";
  if (state.phase === "awaiting_input") return "等待输入";
  if (state.phase === "starting") return "正在启动";
  if (state.phase === "running") return "运行中";
  return "Ready";
}
