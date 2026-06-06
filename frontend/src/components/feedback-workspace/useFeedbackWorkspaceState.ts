import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import {
  getFeedbackWorkbenchData,
  runtimeApi,
} from "../../api/runtime";
import type { AgentJobRecord, FeedbackWorkbenchData } from "../../types/feedback";
import type { ExternalFeedbackWorkspaceProps } from "./types";
import {
  buildSourceRows,
  filterBatches,
  filterEvalCases,
  filterSourceRows,
  sourceRowKey,
} from "./selectors";

export type MenuKey = "signals" | "batches" | "regression-assets" | "versions";
export type RuntimeStatus = "idle" | "loading" | "ok" | "error";
export interface WorkspaceToast {
  id: number;
  message: string;
}

export const visibleMenuItems: Array<{ key: MenuKey; label: string }> = [
  { key: "signals", label: "反馈信息" },
  { key: "batches", label: "优化批次" },
  { key: "regression-assets", label: "回归资产" },
  { key: "versions", label: "版本管理" },
];

const EMPTY_WORKBENCH: FeedbackWorkbenchData = {
  sources: [],
  runs: [],
  signals: [],
  events: [],
  pending_correlations: [],
  cases: [],
  tasks: [],
  external_governance_items: [],
  external_webhooks: [],
  eval_cases: [],
  eval_runs: [],
  optimization_batches: [],
};

const TERMINAL_BATCH_JOB_STATUSES = new Set(["completed", "failed", "needs_human_review", "timeout"]);
const BATCH_BACKGROUND_REFRESH_INTERVAL_MS = 2500;

export function useFeedbackWorkspaceState({
  clientConfig,
  refreshToken,
}: {
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  refreshToken: number;
}) {
  const [activeMenu, setActiveMenu] = useState<MenuKey>("signals");
  const [data, setData] = useState<FeedbackWorkbenchData>(EMPTY_WORKBENCH);
  const [query, setQuery] = useState("");
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([]);
  const [selectedSourceKey, setSelectedSourceKey] = useState<string | null>(null);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>("idle");
  const [toast, setToastState] = useState<WorkspaceToast | null>(null);
  const setToast = useCallback<Dispatch<SetStateAction<string | null>>>((nextToast) => {
    setToastState((current) => {
      const currentMessage = current?.message || null;
      const nextMessage = typeof nextToast === "function" ? nextToast(currentMessage) : nextToast;
      if (!nextMessage) return null;
      return {
        id: (current?.id || 0) + 1,
        message: nextMessage,
      };
    });
  }, []);

  const refreshWorkbench = useCallback(async () => {
    try {
      const next = await getFeedbackWorkbenchData(clientConfig, { limit: 500 });
      setData(next);
      setSelectedBatchId((current) => current || next.optimization_batches[0]?.batch_id || null);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "反馈数据加载失败");
    }
  }, [clientConfig]);

  useEffect(() => {
    refreshWorkbench();
  }, [refreshWorkbench, refreshToken]);

  useEffect(() => {
    if (activeMenu !== "batches" || !hasActiveBatchBackgroundJob(data)) return undefined;
    let refreshing = false;
    const intervalId = window.setInterval(() => {
      if (refreshing) return;
      refreshing = true;
      refreshWorkbench().finally(() => {
        refreshing = false;
      });
    }, BATCH_BACKGROUND_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [activeMenu, data, refreshWorkbench]);

  const sourceRows = useMemo(() => buildSourceRows(data), [data]);
  const visibleSources = useMemo(() => filterSourceRows(sourceRows, query), [sourceRows, query]);
  const visibleBatches = useMemo(() => filterBatches(data.optimization_batches, query), [data.optimization_batches, query]);
  const visibleRegressionAssets = useMemo(() => filterEvalCases(data.eval_cases, query), [data.eval_cases, query]);
  const selectedSource = useMemo(() => {
    if (!visibleSources.length) return null;
    if (selectedSourceKey) {
      const matched = visibleSources.find((row) => sourceRowKey(row) === selectedSourceKey);
      if (matched) return matched;
    }
    return visibleSources[0];
  }, [visibleSources, selectedSourceKey]);
  const selectedBatch = useMemo(() => {
    if (!visibleBatches.length) return null;
    if (selectedBatchId) {
      const matched = visibleBatches.find((batch) => batch.batch_id === selectedBatchId);
      if (matched) return matched;
    }
    return visibleBatches[0];
  }, [visibleBatches, selectedBatchId]);

  useEffect(() => {
    if (!visibleSources.length) {
      setSelectedSourceKey(null);
      return;
    }
    setSelectedSourceKey((current) => {
      if (current && visibleSources.some((row) => sourceRowKey(row) === current)) return current;
      return sourceRowKey(visibleSources[0]);
    });
  }, [visibleSources]);

  useEffect(() => {
    if (!visibleBatches.length) {
      setSelectedBatchId(null);
      return;
    }
    setSelectedBatchId((current) => {
      if (current && visibleBatches.some((batch) => batch.batch_id === current)) return current;
      return visibleBatches[0].batch_id;
    });
  }, [visibleBatches]);

  async function checkRuntime() {
    try {
      setRuntimeStatus("loading");
      await runtimeApi.health(clientConfig);
      setRuntimeStatus("ok");
      setToast("Runtime 连接正常");
    } catch (error) {
      setRuntimeStatus("error");
      setToast(error instanceof Error ? error.message : "Runtime 连接失败");
    }
  }

  return {
    activeMenu,
    setActiveMenu,
    data,
    query,
    setQuery,
    selectedSourceIds,
    setSelectedSourceIds,
    setSelectedSourceKey,
    setSelectedBatchId,
    runtimeStatus,
    toast,
    setToast,
    refreshWorkbench,
    checkRuntime,
    sourceRows,
    visibleSources,
    selectedSource,
    visibleBatches,
    selectedBatch,
    visibleRegressionAssets,
  };
}

function hasActiveBatchBackgroundJob(data: FeedbackWorkbenchData): boolean {
  return data.optimization_batches.some(
    (batch) =>
      hasActiveJobRef(batch.eval_case_generation_job, batch.eval_case_generation_job_id) ||
      hasActiveJobRef(batch.optimization_plan_job, batch.optimization_plan_job_id) ||
      (batch.attribution_jobs || []).some(isActiveJob) ||
      hasActiveJobRef(batch.execution_job, batch.execution_job_id) ||
      hasActiveJobRef(batch.optimization_task?.latest_execution_job, batch.optimization_task?.latest_execution_job_id),
  );
}

function hasActiveJobRef(job?: AgentJobRecord | null, jobId?: string | null): boolean {
  if (!job?.job_id && !jobId) return false;
  return job ? isActiveJob(job) : true;
}

function isActiveJob(job: AgentJobRecord): boolean {
  return !TERMINAL_BATCH_JOB_STATUSES.has(String(job.status || ""));
}
