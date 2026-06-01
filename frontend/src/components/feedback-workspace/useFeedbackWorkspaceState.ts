import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import {
  getFeedbackWorkbenchData,
  runtimeApi,
} from "../../api/runtime";
import type { FeedbackWorkbenchData } from "../../types/feedback";
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
  proposals: [],
  tasks: [],
  external_governance_items: [],
  external_webhooks: [],
  eval_cases: [],
  eval_runs: [],
  optimization_batches: [],
};

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
