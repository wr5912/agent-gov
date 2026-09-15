import { getAgentRunPendingActions, getAllSessionAgentRuns } from "./api/feedback";
import { isTransientApiReadError, waitForApiReadRecovery } from "./api/readRecovery";
import type { ApiRequestError } from "./api/request";
import { getRuntimeSessionMessages, getRuntimeSessionStatus } from "./api/runtime";
import { messagesFromAgentScopeMessages } from "./playgroundHistory";
import type {
  AgentScopeMessage,
  RuntimeClientConfig,
  RuntimePendingAction,
} from "./types/runtime";

export interface PendingSessionReadTarget {
  sessionId: string;
  runtimeAgentId: string;
}

export function pendingSessionReadTargets(
  rootSessionId: string,
  rootRuntimeAgentId: string,
  actions: RuntimePendingAction[],
): PendingSessionReadTarget[] {
  const targets = new Map<string, string>([[rootSessionId, rootRuntimeAgentId]]);
  for (const action of actions) {
    const sessionId = action.session_id?.trim();
    const runtimeAgentId = action.runtime_agent_id?.trim();
    if (!sessionId || !runtimeAgentId) {
      throw new Error("pending action 缺少精确的 Session/Runtime Agent 绑定。");
    }
    const current = targets.get(sessionId);
    if (current && current !== runtimeAgentId) {
      throw new Error("同一 pending Session 对应多个 Runtime Agent，拒绝读取 canonical messages。");
    }
    targets.set(sessionId, runtimeAgentId);
  }
  return [...targets.entries()].map(([sessionId, runtimeAgentId]) => ({ sessionId, runtimeAgentId }));
}

export async function loadPendingCanonicalMessages(
  config: RuntimeClientConfig,
  rootSessionId: string,
  rootRuntimeAgentId: string,
  rootMessages: AgentScopeMessage[],
  actions: RuntimePendingAction[],
  signal?: AbortSignal,
) {
  const entries = await Promise.all(pendingSessionReadTargets(
    rootSessionId, rootRuntimeAgentId, actions,
  ).map(async ({ sessionId, runtimeAgentId }) => {
    if (sessionId === rootSessionId) return [sessionId, rootMessages] as const;
    const history = await getRuntimeSessionMessages(config, runtimeAgentId, sessionId, signal);
    return [sessionId, history.messages] as const;
  }));
  return new Map<string, readonly AgentScopeMessage[]>(entries);
}

export async function loadPlaygroundHistory(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  const [history, status, runs] = await Promise.all([
    getRuntimeSessionMessages(config, agentId, sessionId, signal),
    getRuntimeSessionStatus(config, agentId, sessionId, signal),
    getAllSessionAgentRuns(config, sessionId, signal),
  ]);
  const waitingRun = runs.find((run) => ["waiting_human", "waiting_external"].includes(String(run.status || "")));
  const pendingActions = waitingRun?.run_id
    ? await getAgentRunPendingActions(config, waitingRun.run_id, signal)
    : [];
  const canonicalMessages = await loadPendingCanonicalMessages(
    config, sessionId, agentId, history.messages, pendingActions, signal,
  );
  return {
    history,
    status,
    runs,
    restoredMessages: await messagesFromAgentScopeMessages(
      history.messages, sessionId, runs, pendingActions, canonicalMessages,
    ),
  };
}

/** 初载成功前不创建消息占位，也不把瞬态读取失败伪装成已加载的空历史。 */
export async function recoverPlaygroundHistory(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal: AbortSignal,
  onTransientFailure: (error: ApiRequestError) => void,
) {
  while (true) {
    signal.throwIfAborted();
    const attempt = new AbortController();
    const abortAttempt = () => attempt.abort();
    signal.addEventListener("abort", abortAttempt, { once: true });
    try {
      return await loadPlaygroundHistory(config, agentId, sessionId, attempt.signal);
    } catch (error) {
      signal.throwIfAborted();
      if (!isTransientApiReadError(error)) throw error;
      onTransientFailure(error);
    } finally {
      // Promise.all 的一个读取失败后，不让其余请求与下一轮重复并行。
      attempt.abort();
      signal.removeEventListener("abort", abortAttempt);
    }
    await waitForApiReadRecovery(500, signal);
  }
}
