import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getAttributionOutput,
  getEvidencePackage,
  getFeedbackAnalysisJob,
  getFeedbackWorkbenchData,
  getProposalOutput,
  runtimeApi,
} from "../../api/runtime";
import type {
  EvidencePackageRecord,
  ExternalFeedbackWorkspaceProps,
  FeedbackAnalysisJobRecord,
  FeedbackWorkbenchData,
} from "../../types/feedback";
import type { AttributionDetailTab, CaseDetailView, CaseDetails } from "./CasesWorkspace";
import {
  buildSourceRows,
  buildTaskByProposalId,
  filterBatches,
  filterCases,
  filterSourceRows,
  latest,
  latestItem,
  sourceRowKey,
} from "./selectors";

export type MenuKey = "signals" | "batches" | "cases" | "evals" | "versions";
export type RuntimeStatus = "idle" | "loading" | "ok" | "error";

export const visibleMenuItems: Array<{ key: MenuKey; label: string }> = [
  { key: "signals", label: "反馈信息" },
  { key: "batches", label: "优化批次" },
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
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const [caseDetailView, setCaseDetailView] = useState<CaseDetailView>("summary");
  const [attributionDetailTab, setAttributionDetailTab] = useState<AttributionDetailTab>("result");
  const [caseDetails, setCaseDetails] = useState<CaseDetails>({});
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>("idle");
  const [toast, setToast] = useState<string | null>(null);

  const refreshWorkbench = useCallback(async () => {
    try {
      const next = await getFeedbackWorkbenchData(clientConfig, { limit: 500 });
      setData(next);
      setSelectedCaseId((current) => current || next.cases[0]?.feedback_case_id || null);
      setSelectedBatchId((current) => current || next.optimization_batches[0]?.batch_id || null);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "反馈数据加载失败");
    }
  }, [clientConfig]);

  useEffect(() => {
    refreshWorkbench();
  }, [refreshWorkbench, refreshToken]);

  const selectedCase = useMemo(() => {
    return data.cases.find((item) => item.feedback_case_id === selectedCaseId) || data.cases[0] || null;
  }, [data.cases, selectedCaseId]);

  const sourceRows = useMemo(() => buildSourceRows(data), [data]);
  const visibleSources = useMemo(() => filterSourceRows(sourceRows, query), [sourceRows, query]);
  const visibleBatches = useMemo(() => filterBatches(data.optimization_batches, query), [data.optimization_batches, query]);
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
  const visibleCases = useMemo(() => filterCases(data.cases, query), [data.cases, query]);
  const selectedCaseRuns = useMemo(() => {
    if (!selectedCase) return [];
    const ids = new Set(selectedCase.run_ids || []);
    return data.runs.filter((run) => ids.has(run.run_id));
  }, [data.runs, selectedCase]);
  const selectedCaseProposals = useMemo(() => {
    if (!selectedCase) return [];
    return data.proposals.filter((proposal) => proposal.feedback_case_id === selectedCase.feedback_case_id);
  }, [data.proposals, selectedCase]);
  const selectedCaseTasks = useMemo(() => {
    if (!selectedCase) return [];
    return data.tasks.filter((task) => task.feedback_case_id === selectedCase.feedback_case_id);
  }, [data.tasks, selectedCase]);
  const selectedCaseExternalItems = useMemo(() => {
    if (!selectedCase) return [];
    return data.external_governance_items.filter((item) => item.feedback_case_id === selectedCase.feedback_case_id);
  }, [data.external_governance_items, selectedCase]);
  const selectedCaseEvalCases = useMemo(() => {
    if (!selectedCase) return [];
    return data.eval_cases.filter((item) => item.source_feedback_case_id === selectedCase.feedback_case_id);
  }, [data.eval_cases, selectedCase]);
  const tasksByProposalId = useMemo(() => buildTaskByProposalId(data.tasks), [data.tasks]);

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

  useEffect(() => {
    let cancelled = false;
    async function loadCaseDetails() {
      if (!selectedCase) {
        setCaseDetails({});
        return;
      }
      setDetailsLoading(true);
      const details: CaseDetails = {};
      try {
        const evidenceIds = selectedCase.evidence_package_ids || [];
        const attributionJobIds = selectedCase.attribution_job_ids || [];
        const proposalJobIds = selectedCase.proposal_job_ids || [];
        const attributionJobId = latest(attributionJobIds);
        const proposalJobId = latest(proposalJobIds);
        const [evidencePackages, attributionJobs, proposalJobs, attribution, proposal] = await Promise.all([
          Promise.all(evidenceIds.map((id) => getEvidencePackage(clientConfig, id).catch(() => null))),
          Promise.all(attributionJobIds.map((id) => getFeedbackAnalysisJob(clientConfig, id).catch(() => null))),
          Promise.all(proposalJobIds.map((id) => getFeedbackAnalysisJob(clientConfig, id).catch(() => null))),
          attributionJobId ? getAttributionOutput(clientConfig, attributionJobId).catch(() => null) : Promise.resolve(null),
          proposalJobId ? getProposalOutput(clientConfig, proposalJobId).catch(() => null) : Promise.resolve(null),
        ]);
        details.evidencePackages = evidencePackages.filter(Boolean) as EvidencePackageRecord[];
        details.attributionJobs = attributionJobs.filter(Boolean) as FeedbackAnalysisJobRecord[];
        details.proposalJobs = proposalJobs.filter(Boolean) as FeedbackAnalysisJobRecord[];
        details.evidence = latestItem(details.evidencePackages);
        details.attribution = attribution;
        details.proposal = proposal;
        details.attributionJob = latestItem(details.attributionJobs) || null;
        details.proposalJob = latestItem(details.proposalJobs) || null;
        if (!cancelled) setCaseDetails(details);
      } finally {
        if (!cancelled) setDetailsLoading(false);
      }
    }
    loadCaseDetails();
    return () => {
      cancelled = true;
    };
  }, [clientConfig, selectedCase]);

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
    setSelectedCaseId,
    setSelectedBatchId,
    caseDetailView,
    setCaseDetailView,
    attributionDetailTab,
    setAttributionDetailTab,
    caseDetails,
    detailsLoading,
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
    visibleCases,
    selectedCase,
    selectedCaseRuns,
    selectedCaseProposals,
    selectedCaseTasks,
    selectedCaseExternalItems,
    selectedCaseEvalCases,
    tasksByProposalId,
  };
}
