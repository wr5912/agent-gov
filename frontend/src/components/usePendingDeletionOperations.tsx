import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import {
  deleteBusinessAgent,
  getBusinessAgentDeletionOperation,
  listBusinessAgentDeletionOperations,
} from "../api/runtime";
import type { AgentDeleteResponse, AgentSummary, RuntimeClientConfig } from "../types/runtime";
import {
  activatePendingDeletionContext,
  beginPendingDeletionRequest,
  deactivatePendingDeletionContext,
  settlePendingDeletionRequest,
  type PendingDeletionRequestGeneration,
} from "./pendingDeletionDiscovery";

export interface PendingDeletionReceipt {
  operationId: string;
  label: string;
  impactText: string;
  lastErrorCode: string | null;
  attemptCount: number;
  updatedAt: string;
}

type OptionalTextSetter = Dispatch<SetStateAction<string | undefined>>;
type PendingSetter = Dispatch<SetStateAction<string | null>>;

interface UsePendingDeletionOperationsProps {
  open: boolean;
  config: RuntimeClientConfig;
  agents: AgentSummary[];
  setAgents: Dispatch<SetStateAction<AgentSummary[]>>;
  setPending: PendingSetter;
  setError: OptionalTextSetter;
  setSuccessMessage: OptionalTextSetter;
  onAgentsChanged: () => void;
}

function toPendingDeletionReceipt(response: AgentDeleteResponse): PendingDeletionReceipt {
  const impact = response.impact;
  return {
    operationId: response.operation_id,
    label: response.deleted.name
      ? `${response.deleted.name}（${response.deleted.agent_id}）`
      : response.deleted.agent_id,
    impactText: `影响：runs ${impact.runs} · feedback ${impact.feedback_signals} · 改进事项 ${impact.improvements} · tests ${impact.test_runs} · 待发布变更 ${impact.change_sets} · 发布 ${impact.releases}`,
    lastErrorCode: response.last_error_code ?? null,
    attemptCount: response.attempt_count,
    updatedAt: response.updated_at,
  };
}

function upsertPendingDeletion(
  current: PendingDeletionReceipt[],
  receipt: PendingDeletionReceipt,
): PendingDeletionReceipt[] {
  if (!current.some((item) => item.operationId === receipt.operationId)) {
    return [receipt, ...current];
  }
  return current.map((item) => (item.operationId === receipt.operationId ? receipt : item));
}

function prependPendingDeletion(
  current: PendingDeletionReceipt[],
  receipt: PendingDeletionReceipt,
): PendingDeletionReceipt[] {
  return [receipt, ...current.filter((item) => item.operationId !== receipt.operationId)];
}

function usePendingDeletionDiscovery(
  open: boolean,
  config: RuntimeClientConfig,
  generation: { current: PendingDeletionRequestGeneration },
  setPendingDeletions: Dispatch<SetStateAction<PendingDeletionReceipt[]>>,
  setPending: PendingSetter,
  setError: OptionalTextSetter,
  setSuccessMessage: OptionalTextSetter,
) {
  useEffect(() => {
    if (!open) return;
    const context = activatePendingDeletionContext(generation.current);
    const token = beginPendingDeletionRequest(generation.current);
    setPendingDeletions([]);
    setPending(null);
    setError(undefined);
    setSuccessMessage(undefined);
    void settlePendingDeletionRequest(
      generation.current,
      token,
      listBusinessAgentDeletionOperations(config),
      {
        onSuccess: (operations) => setPendingDeletions(operations.map(toPendingDeletionReceipt)),
        onError: (cause) => {
          setError(
            `无法恢复待完成的 Agent 清理回执：${cause instanceof Error ? cause.message : String(cause)}`,
          );
        },
      },
    );
    return () => deactivatePendingDeletionContext(generation.current, context);
  }, [config, generation, open, setError, setPending, setPendingDeletions, setSuccessMessage]);
}

export function usePendingDeletionOperations(props: UsePendingDeletionOperationsProps) {
  const [pendingDeletions, setPendingDeletions] = useState<PendingDeletionReceipt[]>([]);
  const generation = useRef<PendingDeletionRequestGeneration>({ context: 0, request: 0 });
  usePendingDeletionDiscovery(
    props.open,
    props.config,
    generation,
    setPendingDeletions,
    props.setPending,
    props.setError,
    props.setSuccessMessage,
  );

  const runRequest = useCallback(
    <T,>(actionKey: string, request: () => Promise<T>, onSuccess: (value: T) => void) => {
      const token = beginPendingDeletionRequest(generation.current);
      props.setPending(actionKey);
      props.setError(undefined);
      props.setSuccessMessage(undefined);
      void settlePendingDeletionRequest(
        generation.current,
        token,
        Promise.resolve().then(request),
        {
          onSuccess,
          onError: (cause) => props.setError(cause instanceof Error ? cause.message : String(cause)),
          onFinally: () => props.setPending(null),
        },
      );
    },
    [props.setError, props.setPending, props.setSuccessMessage],
  );

  const handleDelete = useDeleteAgent(props, runRequest, setPendingDeletions);
  const handleRefreshDeletion = useRefreshDeletion(props, runRequest, setPendingDeletions);
  return { handleDelete, handleRefreshDeletion, pendingDeletions };
}

type RunPendingDeletionRequest = <T>(
  actionKey: string,
  request: () => Promise<T>,
  onSuccess: (value: T) => void,
) => void;

function useDeleteAgent(
  props: UsePendingDeletionOperationsProps,
  runRequest: RunPendingDeletionRequest,
  setPendingDeletions: Dispatch<SetStateAction<PendingDeletionReceipt[]>>,
) {
  return useCallback(
    (agentId: string) => {
      const agent = props.agents.find((candidate) => candidate.agent_id === agentId);
      if (!agent) {
        props.setError(`业务 Agent ${agentId} 已不在当前列表，请刷新后重试。`);
        return;
      }
      const label = agent.name ? `${agent.name}（${agentId}）` : agentId;
      const confirmed = window.confirm(
        `确认删除业务 Agent ${label}？\n\n将永久删除它的 Workspace、Claude 用户态和版本历史；运行、反馈与发布记录保留作审计。该操作不可撤销。`,
      );
      if (!confirmed) return;
      runRequest(
        `delete:${agentId}`,
        () => deleteBusinessAgent(props.config, agentId, agent.instance_etag),
        (response) => {
          const receipt = toPendingDeletionReceipt(response);
          if (response.state === "cleanup_pending") {
            setPendingDeletions((current) => prependPendingDeletion(current, receipt));
            props.setSuccessMessage(
              `业务 Agent ${label} 已从可用列表移除，后台清理待完成（operation ${response.operation_id}；${receipt.impactText}）`,
            );
          } else {
            setPendingDeletions((current) =>
              current.filter((item) => item.operationId !== receipt.operationId),
            );
            props.setSuccessMessage(`已彻底删除业务 Agent ${label}（${receipt.impactText}）`);
          }
          props.setAgents((current) => current.filter((item) => item.agent_id !== agentId));
          props.onAgentsChanged();
        },
      );
    },
    [props, runRequest, setPendingDeletions],
  );
}

function useRefreshDeletion(
  props: UsePendingDeletionOperationsProps,
  runRequest: RunPendingDeletionRequest,
  setPendingDeletions: Dispatch<SetStateAction<PendingDeletionReceipt[]>>,
) {
  return useCallback(
    (pendingDeletion: PendingDeletionReceipt) => {
      runRequest(
        `delete-status:${pendingDeletion.operationId}`,
        () => getBusinessAgentDeletionOperation(props.config, pendingDeletion.operationId),
        (response) => {
          if (response.state === "completed") {
            setPendingDeletions((current) =>
              current.filter((item) => item.operationId !== pendingDeletion.operationId),
            );
            props.setSuccessMessage(
              `已彻底删除业务 Agent ${pendingDeletion.label}（${pendingDeletion.impactText}）`,
            );
            return;
          }
          const refreshed = toPendingDeletionReceipt(response);
          setPendingDeletions((current) => upsertPendingDeletion(current, refreshed));
          props.setSuccessMessage(
            `业务 Agent ${pendingDeletion.label} 已从可用列表移除，后台清理仍待完成（operation ${pendingDeletion.operationId}；${pendingDeletion.impactText}）`,
          );
        },
      );
    },
    [props.config, props.setSuccessMessage, runRequest, setPendingDeletions],
  );
}

interface PendingDeletionNoticeProps {
  busy: boolean;
  pendingDeletions: PendingDeletionReceipt[];
  onRefresh: (pendingDeletion: PendingDeletionReceipt) => void;
}

export function PendingDeletionNotice({
  busy,
  pendingDeletions,
  onRefresh,
}: PendingDeletionNoticeProps) {
  if (!pendingDeletions.length) return null;
  return (
    <div className="settings-success" data-testid="settings-pending-deletions" role="status" aria-live="polite">
      <strong>{pendingDeletions.length} 个业务 Agent 已下线，后台清理仍待完成</strong>
      {pendingDeletions.map((pendingDeletion) => (
        <div key={pendingDeletion.operationId}>
          <span>
            {pendingDeletion.label} · operation {pendingDeletion.operationId} · 尝试 {pendingDeletion.attemptCount} 次 · 更新于 {pendingDeletion.updatedAt}
            {pendingDeletion.lastErrorCode ? ` · 最近错误 ${pendingDeletion.lastErrorCode}` : ""}
          </span>
          <button
            className="secondary-button"
            type="button"
            onClick={() => onRefresh(pendingDeletion)}
            disabled={busy}
            data-testid="settings-deletion-status-refresh"
          >
            刷新清理状态
          </button>
        </div>
      ))}
    </div>
  );
}
