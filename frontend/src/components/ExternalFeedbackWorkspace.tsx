import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  AlertTriangle,
  Archive,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  Copy,
  Database,
  FileArchive,
  FileText,
  FolderKanban,
  GitBranch,
  Loader2,
  MessageSquare,
  Pencil,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  X,
  XCircle,
} from "lucide-react";
import { AgentVersionsWorkspace } from "./AgentVersionsWorkspace";
import {
  createAttributionJob,
  createEvidencePackage,
  createFeedbackCase,
  createFeedbackOptimizationBatch,
  createEvalRun,
  applyOptimizationExecutionJob,
  createOptimizationTask,
  createOptimizationExecutionJob,
  executeFeedbackOptimizationPlanTask,
  createProposalJob,
  diffAgentVersionFile,
  generateFeedbackOptimizationBatchPlan,
  generateFeedbackSourceEvalCases,
  getAttributionOutput,
  getEvidencePackage,
  getEvidencePackageFile,
  getFeedbackAnalysisJob,
  getFeedbackWorkbenchData,
  getProposalOutput,
  markOptimizationTaskApplied,
  notifyExternalGovernanceItem,
  regenerateAttributionJob,
  regenerateProposalJob,
  rejectFeedbackOptimizationBatchPlan,
  revalidateProposalOutput,
  reviewOptimizationProposal,
  runFeedbackOptimizationBatchAttribution,
  runFeedbackOptimizationBatchRegression,
  runOptimizationTaskRegression,
  runtimeApi,
  syncFeedbackEvalDataset,
  updateEvalCase,
} from "../api/runtime";
import type {
  AttributionOutput,
  EvidencePackageFileRecord,
  EvidencePackageRecord,
  EvalCaseRecord,
  EvalCaseUpdateRequest,
  EvalRunRecord,
  ExternalGovernanceItemRecord,
  ExternalGovernanceWebhookRecord,
  ExternalFeedbackWorkspaceProps,
  FeedbackAnalysisJobRecord,
  FeedbackCaseRecord,
  FeedbackOptimizationBlockedItemRecord,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackRunRecord,
  FeedbackSignalRecord,
  FeedbackSourceKind,
  FeedbackSourceRecord,
  FeedbackSourceRef,
  FeedbackWorkbenchData,
  ExecutionPlanOperation,
  OptimizationExecutionJobRecord,
  OptimizationProposalRecord,
  OptimizationProposalReviewAction,
  OptimizationTaskRecord,
  PendingCorrelationRecord,
  ProposalOutput,
  SocEventRecord,
} from "../types/feedback";
import type { AgentVersionFileDiff } from "../types/runtime";

type MenuKey = "signals" | "batches" | "cases" | "evals" | "versions";
type SourceKind = FeedbackSourceKind;
type CaseDetailView = "summary" | "evidence" | "attribution" | "proposal" | "runs" | "tasks" | "evals";
type ProposalDetailTab = "proposals" | "raw" | "records";
type AttributionDetailTab = "result" | "raw" | "records";
type BatchDetailView = "feedback" | "attribution" | "plan" | "regression";

interface EvalCaseEditDraft {
  prompt: string;
  expectedBehavior: string;
  labelsText: string;
  status: "active" | "draft" | "archived";
  checksText: string;
}

interface ProposalRegenerateDraft {
  feedbackCaseId: string;
  instruction: string;
}

interface BatchPlanGenerateDraft {
  batch: FeedbackOptimizationBatchRecord;
  instruction: string;
}

interface ExecutionApplyDraft {
  task: OptimizationTaskRecord;
}

interface ManualApplyDraft {
  task: OptimizationTaskRecord;
}

interface DetailTabItem<T extends string> {
  key: T;
  label: string;
}

interface SourceRow {
  id: string;
  kind: SourceKind;
  label: string;
  status: string;
  createdAt?: string;
  runId?: string | null;
  sessionId?: string | null;
  alertId?: string | null;
  caseId?: string | null;
  feedbackCaseId?: string | null;
  evalCaseId?: string | null;
  raw: FeedbackSourceRecord | FeedbackSignalRecord | SocEventRecord | PendingCorrelationRecord;
}

interface CaseDetails {
  evidence?: EvidencePackageRecord | null;
  evidencePackages?: EvidencePackageRecord[];
  attributionJob?: FeedbackAnalysisJobRecord | null;
  attributionJobs?: FeedbackAnalysisJobRecord[];
  proposalJob?: FeedbackAnalysisJobRecord | null;
  proposalJobs?: FeedbackAnalysisJobRecord[];
  attribution?: AttributionOutput | null;
  proposal?: ProposalOutput | null;
}

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

const visibleMenuItems: Array<{ key: MenuKey; label: string }> = [
  { key: "signals", label: "反馈信息" },
  { key: "batches", label: "优化批次" },
  { key: "versions", label: "版本管理" },
];

const sourceKindText: Record<SourceKind, string> = {
  signal: "Feedback signal",
  soc_event: "SOC event",
  pending_correlation: "待关联",
};

const caseStatusText: Record<string, string> = {
  pending_evidence: "待生成证据包",
  pending_attribution: "待归因",
  attribution_queued: "归因排队",
  pending_proposal: "待生成建议",
  proposal_queued: "建议排队",
  pending_review: "待审批建议",
  needs_human_review: "需人工复核",
};

const proposalStatusText: Record<string, string> = {
  pending_review: "待审批",
  approved: "已批准",
  rejected: "已拒绝",
  needs_more_analysis: "需补充分析",
  superseded: "已废弃",
};

export function ExternalFeedbackWorkspace({
  clientConfig,
  runtimeContext,
  monitoringConfig,
  currentAgentVersion,
  agentVersions = [],
  versionLoading = false,
  versionError,
  onRefreshVersions,
  refreshToken = 0,
  onFeedbackChanged,
}: ExternalFeedbackWorkspaceProps) {
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
  const [runtimeStatus, setRuntimeStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [actionId, setActionId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [proposalRegenerateDraft, setProposalRegenerateDraft] = useState<ProposalRegenerateDraft | null>(null);
  const [batchPlanGenerateDraft, setBatchPlanGenerateDraft] = useState<BatchPlanGenerateDraft | null>(null);
  const [executionApplyDraft, setExecutionApplyDraft] = useState<ExecutionApplyDraft | null>(null);
  const [manualApplyDraft, setManualApplyDraft] = useState<ManualApplyDraft | null>(null);
  const proposalRegenerateBusy = Boolean(actionId?.startsWith("proposal-regenerate:"));
  const batchPlanGenerateBusy = Boolean(actionId?.startsWith("batch-plan:"));
  const executionApplyBusy = Boolean(actionId?.startsWith("execution-apply:"));
  const manualApplyBusy = Boolean(actionId?.startsWith("apply:"));

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

  function toggleSource(sourceId: string, checked: boolean) {
    setSelectedSourceIds((current) => {
      if (checked) return current.includes(sourceId) ? current : [...current, sourceId];
      return current.filter((item) => item !== sourceId);
    });
  }

  function selectedSourceRefs(): FeedbackSourceRef[] {
    const selectedRows = sourceRows.filter((row) => selectedSourceIds.includes(row.id));
    return selectedRows.map((row) => ({ source_kind: row.kind, source_id: row.id }));
  }

  async function generateEvalCasesFromSelection() {
    const refs = selectedSourceRefs();
    if (!refs.length) {
      setToast("请先选择反馈信息");
      return;
    }
    setActionId("generate-eval-cases");
    try {
      const result = await generateFeedbackSourceEvalCases(clientConfig, { source_refs: refs });
      setToast(`已生成/复用回归用例：新增 ${result.created}，复用 ${result.reused}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "生成回归用例失败");
    } finally {
      setActionId(null);
    }
  }

  async function createBatchFromSelection() {
    const refs = selectedSourceRefs();
    if (!refs.length) {
      setToast("请先选择反馈信息");
      return;
    }
    setActionId("create-batch");
    try {
      const batch = await createFeedbackOptimizationBatch(clientConfig, {
        source_refs: refs,
        priority: refs.length >= 5 ? "high" : "medium",
      });
      setToast(`已创建优化批次 ${shortId(batch.batch_id)}`);
      setSelectedSourceIds([]);
      setSelectedBatchId(batch.batch_id);
      setActiveMenu("batches");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "创建优化批次失败");
    } finally {
      setActionId(null);
    }
  }

  async function createCaseFromSelection() {
    if (!selectedSourceIds.length) {
      setToast("请先选择反馈信息");
      return;
    }
    setActionId("create-case");
    try {
      const created = await createFeedbackCase(clientConfig, {
        source_ids: selectedSourceIds,
        priority: selectedSourceIds.length >= 5 ? "high" : "medium",
      });
      setToast(`已创建反馈处置单 ${shortId(created.feedback_case_id)}`);
      setSelectedSourceIds([]);
      setSelectedCaseId(created.feedback_case_id);
      setActiveMenu("batches");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "创建反馈处置单失败");
    } finally {
      setActionId(null);
    }
  }

  async function runCaseAction(action: "evidence" | "attribution" | "proposal") {
    if (!selectedCase) return;
    if (action === "evidence" && selectedCase.evidence_package_ids.length) {
      setCaseDetailView("evidence");
      setToast("证据包已生成，可在详情中查看");
      return;
    }
    if (action === "attribution" && selectedCase.attribution_job_ids.length && !isRetryableJobStatus(caseDetails.attributionJob?.status)) {
      setAttributionDetailTab("result");
      setCaseDetailView("attribution");
      setToast("已有归因分析记录，可在详情中查看");
      return;
    }
    if (action === "proposal" && selectedCase.proposal_job_ids.length && !isRetryableJobStatus(caseDetails.proposalJob?.status)) {
      setCaseDetailView("proposal");
      setToast("已有优化方案生成记录，可在详情中查看");
      return;
    }
    setActionId(`${action}:${selectedCase.feedback_case_id}`);
    try {
      if (action === "evidence") {
        const evidence = await createEvidencePackage(clientConfig, selectedCase.feedback_case_id);
        setToast(`已生成证据包 ${shortId(evidence.evidence_package_id)}`);
        setCaseDetailView("evidence");
      } else if (action === "attribution") {
        const job = await createAttributionJob(clientConfig, selectedCase.feedback_case_id);
        setToast(`已完成归因 job ${shortId(job.job_id)}：${job.status}`);
        setCaseDetailView("attribution");
      } else {
        const job = await createProposalJob(clientConfig, selectedCase.feedback_case_id);
        setToast(`已完成建议 job ${shortId(job.job_id)}：${job.status}`);
        setCaseDetailView("proposal");
      }
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "处置动作失败");
    } finally {
      setActionId(null);
    }
  }

  async function reviewProposal(proposalId: string, action: OptimizationProposalReviewAction) {
    setActionId(`${action}:${proposalId}`);
    try {
      await reviewOptimizationProposal(clientConfig, proposalId, {
        action,
        comment: reviewComment(action),
      });
      setToast(action === "approve" ? "建议已批准" : action === "reject" ? "建议已拒绝" : "已要求补充分析");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "建议审批失败");
    } finally {
      setActionId(null);
    }
  }

  async function revalidateProposalJob(jobId: string) {
    setActionId(`proposal-revalidate:${jobId}`);
    try {
      const job = await revalidateProposalOutput(clientConfig, jobId);
      setToast(`已重新校验建议 job ${shortId(job.job_id)}：${job.status}`);
      setCaseDetailView("proposal");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新校验建议失败");
    } finally {
      setActionId(null);
    }
  }

  async function regenerateProposal(feedbackCaseId: string) {
    setProposalRegenerateDraft({ feedbackCaseId, instruction: "" });
  }

  async function submitProposalRegenerate(event: FormEvent) {
    event.preventDefault();
    if (!proposalRegenerateDraft) return;
    const { feedbackCaseId } = proposalRegenerateDraft;
    const instruction = proposalRegenerateDraft.instruction.trim();
    setActionId(`proposal-regenerate:${feedbackCaseId}`);
    setCaseDetailView("proposal");
    setProposalRegenerateDraft(null);
    setToast("已提交重新生成优化方案请求");
    const regeneratePromise = regenerateProposalJob(clientConfig, feedbackCaseId, {
      regeneration_instruction: instruction,
    });
    window.setTimeout(() => {
      void refreshWorkbench();
    }, 500);
    try {
      const job = await regeneratePromise;
      setToast(`已重新生成建议 job ${shortId(job.job_id)}：${job.status}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新生成建议失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function regenerateAttribution(feedbackCaseId: string) {
    setActionId(`attribution-regenerate:${feedbackCaseId}`);
    try {
      const job = await regenerateAttributionJob(clientConfig, feedbackCaseId);
      setToast(`已重新启动归因 job ${shortId(job.job_id)}：${job.status}`);
      setAttributionDetailTab("result");
      setCaseDetailView("attribution");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新归因失败");
    } finally {
      setActionId(null);
    }
  }

  function openTask(task: OptimizationTaskRecord) {
    if (task.feedback_case_id) {
      setSelectedCaseId(task.feedback_case_id);
      setCaseDetailView("tasks");
      setActiveMenu("batches");
      return;
    }
    setActiveMenu("batches");
  }

  async function runBatchAttribution(batch: FeedbackOptimizationBatchRecord, force = false) {
    setActionId(`batch-attribution:${batch.batch_id}`);
    try {
      const result = await runFeedbackOptimizationBatchAttribution(clientConfig, batch.batch_id, force ? { force: true } : undefined);
      setToast(`${force ? "已重新归因" : "批次归因完成"}：${result.jobs.length} 个当前 job`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批次归因失败");
    } finally {
      setActionId(null);
    }
  }

  function openBatchPlanGeneration(batch: FeedbackOptimizationBatchRecord) {
    if (batch.optimization_plan) {
      setBatchPlanGenerateDraft({ batch, instruction: "" });
      return;
    }
    void generateBatchPlan(batch);
  }

  async function submitBatchPlanGeneration(event: FormEvent) {
    event.preventDefault();
    if (!batchPlanGenerateDraft) return;
    const { batch } = batchPlanGenerateDraft;
    const instruction = batchPlanGenerateDraft.instruction.trim();
    setBatchPlanGenerateDraft(null);
    await generateBatchPlan(batch, instruction);
  }

  async function generateBatchPlan(batch: FeedbackOptimizationBatchRecord, instruction?: string) {
    setActionId(`batch-plan:${batch.batch_id}`);
    try {
      const updated = await generateFeedbackOptimizationBatchPlan(
        clientConfig,
        batch.batch_id,
        instruction ? { regeneration_instruction: instruction } : undefined,
      );
      setToast(`${batch.optimization_plan ? "已重新生成优化方案" : "已生成优化方案"}：${updated.optimization_plan?.status || updated.status}`);
      setSelectedBatchId(updated.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "生成优化方案失败");
    } finally {
      setActionId(null);
    }
  }

  async function executePlanTask(batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) {
    setActionId(`plan-task:${planTask.plan_task_id}`);
    try {
      const result = await executeFeedbackOptimizationPlanTask(clientConfig, batch.batch_id, planTask.plan_task_id, {
        force: true,
        webhook_alias: webhookAlias || undefined,
      });
      if (result.apply_result || result.optimization_task?.applied_agent_version_id) {
        setToast(`已执行任务，生成版本 ${shortId(result.optimization_task?.applied_agent_version_id)}`);
        await onRefreshVersions?.();
      } else if (result.external_item) {
        setToast(`${result.external_item.status === "notified" ? "已发送外部任务" : "外部任务发送失败"}：${webhookAlias || result.external_item.latest_webhook_alias || "-"}`);
      } else {
        setToast(`任务已执行：${result.plan_task?.status || result.execution_job?.status || result.batch.status}`);
      }
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "执行方案任务失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function rejectBatchPlan(batch: FeedbackOptimizationBatchRecord) {
    setActionId(`batch-reject:${batch.batch_id}`);
    try {
      const updated = await rejectFeedbackOptimizationBatchPlan(clientConfig, batch.batch_id, `拒绝优化批次 ${batch.batch_id}`);
      setToast("已拒绝优化方案");
      setSelectedBatchId(updated.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "拒绝优化方案失败");
    } finally {
      setActionId(null);
    }
  }

  async function runBatchRegression(batch: FeedbackOptimizationBatchRecord) {
    setActionId(`batch-regression:${batch.batch_id}`);
    try {
      const result = await runFeedbackOptimizationBatchRegression(clientConfig, batch.batch_id);
      setToast(`批次回归测试完成：${result.eval_run.result_status || result.eval_run.status}`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批次回归测试失败");
    } finally {
      setActionId(null);
    }
  }

  async function createTask(proposal: OptimizationProposalRecord) {
    const existingTask = tasksByProposalId.get(proposal.proposal_id);
    if (existingTask) {
      openTask(existingTask);
      setToast(`已打开优化任务 ${shortId(existingTask.optimization_task_id)}`);
      return;
    }
    setActionId(`task:${proposal.proposal_id}`);
    try {
      const task = await createOptimizationTask(clientConfig, {
        proposal_id: proposal.proposal_id,
        execution_mode: "manual_or_patch",
        comment: `由 proposal ${proposal.proposal_id} 创建。`,
      });
      setToast(`已创建优化任务 ${shortId(task.optimization_task_id)}`);
      openTask(task);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "创建优化任务失败");
    } finally {
      setActionId(null);
    }
  }

  function markTaskApplied(task: OptimizationTaskRecord) {
    if (task.applied_agent_version_id) {
      setToast("当前任务已创建应用版本");
      return;
    }
    setManualApplyDraft({ task });
  }

  async function submitManualApply() {
    if (!manualApplyDraft) return;
    const task = manualApplyDraft.task;
    setActionId(`apply:${task.optimization_task_id}`);
    try {
      const updated = await markOptimizationTaskApplied(clientConfig, task.optimization_task_id, `由反馈处置界面确认任务 ${task.optimization_task_id} 已应用。`);
      setToast(`已创建版本快照 ${shortId(updated.applied_agent_version_id)}`);
      setManualApplyDraft(null);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "标记已应用失败");
    } finally {
      setActionId(null);
    }
  }

  async function createExecutionJob(task: OptimizationTaskRecord, force = false) {
    setActionId(`execution:${task.optimization_task_id}`);
    try {
      const job = await createOptimizationExecutionJob(clientConfig, task.optimization_task_id, force);
      setToast(`执行方案 ${shortId(job.execution_job_id)}：${job.status}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "生成执行方案失败");
    } finally {
      setActionId(null);
    }
  }

  function applyExecutionJob(task: OptimizationTaskRecord) {
    const jobId = task.latest_execution_job?.execution_job_id || task.latest_execution_job_id;
    if (!jobId) {
      setToast("当前任务没有可应用的执行方案");
      return;
    }
    if (task.latest_execution_job?.status !== "ready") {
      setToast("执行方案尚未 ready，不能应用");
      return;
    }
    setExecutionApplyDraft({ task });
  }

  async function submitExecutionApply() {
    if (!executionApplyDraft) return;
    const task = executionApplyDraft.task;
    const jobId = task.latest_execution_job?.execution_job_id || task.latest_execution_job_id;
    if (!jobId) {
      setToast("当前任务没有可应用的执行方案");
      setExecutionApplyDraft(null);
      return;
    }
    setActionId(`execution-apply:${task.optimization_task_id}`);
    try {
      const result = await applyOptimizationExecutionJob(clientConfig, task.optimization_task_id, jobId);
      setToast(`已应用执行方案，生成版本 ${shortId(result.optimization_task?.applied_agent_version_id)}`);
      setExecutionApplyDraft(null);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "应用执行方案失败");
    } finally {
      setActionId(null);
    }
  }

  async function runTaskRegression(task: OptimizationTaskRecord) {
    setActionId(`regression:${task.optimization_task_id}`);
    try {
      const run = await runOptimizationTaskRegression(clientConfig, task.optimization_task_id);
      setToast(`回归验证完成：${run.result_status || run.status}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "回归验证失败");
    } finally {
      setActionId(null);
    }
  }

  async function notifyExternalItem(item: ExternalGovernanceItemRecord, webhookAlias: string) {
    setActionId(`external-notify:${item.external_item_id}`);
    try {
      const updated = await notifyExternalGovernanceItem(clientConfig, item.external_item_id, webhookAlias);
      const statusText = updated.status === "notified" ? "已通知外部系统" : "通知失败";
      setToast(`${statusText}：${webhookAlias}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "通知外部系统失败");
    } finally {
      setActionId(null);
    }
  }

  async function syncEvalDataset(feedbackCaseId?: string) {
    setActionId(feedbackCaseId ? `sync-eval:${feedbackCaseId}` : "sync-eval");
    try {
      const result = await syncFeedbackEvalDataset(clientConfig, feedbackCaseId);
      setToast(`已同步评估集：新增 ${result.created}，复用 ${result.reused}`);
      await refreshWorkbench();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "同步评估集失败");
    } finally {
      setActionId(null);
    }
  }

  async function runDatasetEval() {
    setActionId("dataset-eval");
    try {
      const run = await createEvalRun(clientConfig);
      setToast(`批量评估完成：${run.result_status || run.status}`);
      await refreshWorkbench();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批量评估失败");
    } finally {
      setActionId(null);
    }
  }

  async function updateEvalCaseRecord(evalCaseId: string, payload: EvalCaseUpdateRequest): Promise<boolean> {
    setActionId(`eval-case:${evalCaseId}`);
    try {
      const updated = await updateEvalCase(clientConfig, evalCaseId, payload);
      setToast(`已更新评估用例 ${shortId(updated.eval_case_id)}`);
      setCaseDetailView("evals");
      await refreshWorkbench();
      onFeedbackChanged?.();
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "更新评估用例失败");
      return false;
    } finally {
      setActionId(null);
    }
  }

  return (
    <div className="fw-shell">
      <aside className="fw-sidebar">
        {visibleMenuItems.map((item) => (
          <button className={activeMenu === item.key ? "active" : ""} key={item.key} onClick={() => setActiveMenu(item.key)} type="button">
            {item.label}
            {item.key === "versions" && agentVersions.length > 0 ? <span className="fw-menu-badge">{agentVersions.length}</span> : null}
          </button>
        ))}
      </aside>

      <div className="fw-content">
        {activeMenu !== "versions" ? (
          <header className="fw-topbar fw-unified-topbar">
            <div className="fw-context-strip" aria-label="运行上下文">
              <span title={runtimeContext?.runId ?? "-"}>run_id：{runtimeContext?.runId ?? "-"}</span>
              <span title={runtimeContext?.sessionId ?? "-"}>session_id：{runtimeContext?.sessionId ?? "-"}</span>
              <span title={runtimeContext?.agentVersionId ?? "-"}>agent_version_id：{runtimeContext?.agentVersionId ?? "-"}</span>
              <span title={runtimeContext?.caseId ?? "-"}>case_id：{runtimeContext?.caseId ?? "-"}</span>
            </div>
            <div className="fw-header-actions">
              <label className="fw-local-search fw-signal-search">
                <Search size={16} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 ID、标签、Case" />
              </label>
              <button className="fw-small-secondary" onClick={checkRuntime} type="button">
                {runtimeStatus === "loading" ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
                Runtime
              </button>
            </div>
          </header>
        ) : null}

        {activeMenu === "signals" ? (
          <SignalsPanel
            rows={visibleSources}
            selectedIds={selectedSourceIds}
            selectedSource={selectedSource}
            actionId={actionId}
            onToggle={toggleSource}
            onSelectSource={(row) => setSelectedSourceKey(sourceRowKey(row))}
            onCreateBatch={createBatchFromSelection}
            onGenerateEvalCases={generateEvalCasesFromSelection}
          />
        ) : null}

        {activeMenu === "batches" ? (
          <BatchesPanel
            actionId={actionId}
            batches={visibleBatches}
            clientConfig={clientConfig}
            externalWebhooks={data.external_webhooks}
            selectedBatch={selectedBatch}
            sources={data.sources}
            onApplyExecutionJob={applyExecutionJob}
            onCreateExecutionJob={createExecutionJob}
            onExecutePlanTask={executePlanTask}
            onGeneratePlan={openBatchPlanGeneration}
            onRejectPlan={rejectBatchPlan}
            onRunAttribution={runBatchAttribution}
            onRunRegression={runBatchRegression}
            onSelectBatch={(batch) => setSelectedBatchId(batch.batch_id)}
          />
        ) : null}

        {activeMenu === "cases" ? (
          <CasesPanel
            cases={visibleCases}
            selectedCase={selectedCase}
            selectedCaseRuns={selectedCaseRuns}
            selectedCaseProposals={selectedCaseProposals}
            selectedCaseTasks={selectedCaseTasks}
            selectedCaseExternalItems={selectedCaseExternalItems}
            details={caseDetails}
            detailView={caseDetailView}
            attributionDetailTab={attributionDetailTab}
            detailsLoading={detailsLoading}
            actionId={actionId}
            clientConfig={clientConfig}
            onSelectCase={(feedbackCase) => {
              setSelectedCaseId(feedbackCase.feedback_case_id);
              setCaseDetailView("summary");
              setAttributionDetailTab("result");
            }}
            onSelectDetailView={setCaseDetailView}
            onOpenAttributionTab={(tab) => {
              setCaseDetailView("attribution");
              setAttributionDetailTab(tab);
            }}
            onAttributionDetailTabChange={setAttributionDetailTab}
            onCreateEvidence={() => runCaseAction("evidence")}
            onRunAttribution={() => runCaseAction("attribution")}
            onRunProposal={() => runCaseAction("proposal")}
            onReviewProposal={reviewProposal}
            onCreateTask={createTask}
            onCreateExecutionJob={createExecutionJob}
            onApplyExecutionJob={applyExecutionJob}
            onNotifyExternalItem={notifyExternalItem}
            onOpenTask={openTask}
            onMarkTaskApplied={markTaskApplied}
            onRunTaskRegression={runTaskRegression}
            onRegenerateAttribution={regenerateAttribution}
            onRegenerateProposal={regenerateProposal}
            onRevalidateProposalJob={revalidateProposalJob}
            onUpdateEvalCase={updateEvalCaseRecord}
            selectedCaseEvalCases={selectedCaseEvalCases}
            evalRuns={data.eval_runs}
            tasksByProposalId={tasksByProposalId}
            externalWebhooks={data.external_webhooks}
          />
        ) : null}

        {activeMenu === "evals" ? (
          <EvalPanel
            evalCases={data.eval_cases}
            evalRuns={data.eval_runs}
            actionId={actionId}
            selectedCase={selectedCase}
            selectedCaseEvalCases={selectedCaseEvalCases}
            onSyncDataset={syncEvalDataset}
            onRunDatasetEval={runDatasetEval}
          />
        ) : null}

        {activeMenu === "versions" ? (
          <AgentVersionsWorkspace
            clientConfig={clientConfig}
            currentVersion={currentAgentVersion || null}
            versions={agentVersions}
            loading={versionLoading}
            lastError={versionError}
            onRefresh={onRefreshVersions || (() => undefined)}
            embedded
          />
        ) : null}

        {activeMenu !== "versions" ? (
          <footer className="fw-info-bar">
            <GitBranch size={18} />
            <span>{"当前链路：反馈信息 -> 默认回归用例 -> 优化批次 -> 归因分析智能体-> 优化方案生成智能体-> 执行优化智能体-> 批次回归测试。"}</span>
            {monitoringConfig?.langfuseUrl ? <a href={monitoringConfig.langfuseUrl} target="_blank" rel="noreferrer">Langfuse</a> : null}
          </footer>
        ) : null}
      </div>

      {proposalRegenerateDraft ? (
        <div className="modal-backdrop" role="presentation" onClick={() => !proposalRegenerateBusy && setProposalRegenerateDraft(null)}>
          <form
            className="modal-card fw-proposal-regenerate-modal"
            role="dialog"
            aria-modal="true"
            aria-label="重新生成优化方案"
            onClick={(event) => event.stopPropagation()}
            onSubmit={submitProposalRegenerate}
          >
            <header className="modal-head">
              <div>
                <h3>重新生成优化方案</h3>
                <p>重新生成会废弃当前反馈单中未审批、未通知的旧建议，并保留历史记录。</p>
              </div>
              <button className="mini-icon-button" type="button" onClick={() => setProposalRegenerateDraft(null)} aria-label="关闭" disabled={proposalRegenerateBusy}>
                <X size={16} />
              </button>
            </header>
            <label className="form-field">
              <span>补充指令</span>
              <textarea
                maxLength={2000}
                placeholder="补充本次生成指令，可留空"
                value={proposalRegenerateDraft.instruction}
                onChange={(event) =>
                  setProposalRegenerateDraft((current) => (current ? { ...current, instruction: event.target.value } : current))
                }
              />
            </label>
            <div className="fw-modal-inline-meta">
              <span>{proposalRegenerateDraft.instruction.length}/2000</span>
            </div>
            <div className="modal-actions">
              <button className="fw-small-secondary" type="button" onClick={() => setProposalRegenerateDraft(null)} disabled={proposalRegenerateBusy}>
                取消
              </button>
              <button className="fw-small-primary" type="submit" disabled={proposalRegenerateBusy}>
                {proposalRegenerateBusy ? <Loader2 size={16} className="fw-spin" /> : <RotateCcw size={16} />}
                重新生成
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {batchPlanGenerateDraft ? (
        <div className="modal-backdrop" role="presentation" onClick={() => !batchPlanGenerateBusy && setBatchPlanGenerateDraft(null)}>
          <form
            className="modal-card fw-proposal-regenerate-modal"
            role="dialog"
            aria-modal="true"
            aria-label="重新生成优化方案"
            onClick={(event) => event.stopPropagation()}
            onSubmit={submitBatchPlanGeneration}
          >
            <header className="modal-head">
              <div>
                <h3>重新生成优化方案</h3>
                <p>重新生成会覆盖当前未审批优化方案，并使用补充要求生成新的方案。</p>
              </div>
              <button className="mini-icon-button" type="button" onClick={() => setBatchPlanGenerateDraft(null)} aria-label="关闭" disabled={batchPlanGenerateBusy}>
                <X size={16} />
              </button>
            </header>
            <label className="form-field">
              <span>补充要求</span>
              <textarea
                maxLength={2000}
                placeholder="补充本次优化方案生成要求，可留空"
                value={batchPlanGenerateDraft.instruction}
                onChange={(event) =>
                  setBatchPlanGenerateDraft((current) => (current ? { ...current, instruction: event.target.value } : current))
                }
              />
            </label>
            <div className="fw-modal-inline-meta">
              <span>{batchPlanGenerateDraft.instruction.length}/2000</span>
            </div>
            <div className="modal-actions">
              <button className="fw-small-secondary" type="button" onClick={() => setBatchPlanGenerateDraft(null)} disabled={batchPlanGenerateBusy}>
                取消
              </button>
              <button className="fw-small-primary" type="submit" disabled={batchPlanGenerateBusy}>
                {batchPlanGenerateBusy ? <Loader2 size={16} className="fw-spin" /> : <RotateCcw size={16} />}
                重新生成
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {executionApplyDraft ? (
        <ExecutionApplyConfirmModal
          busy={executionApplyBusy}
          onCancel={() => setExecutionApplyDraft(null)}
          onConfirm={submitExecutionApply}
          task={executionApplyDraft.task}
        />
      ) : null}

      {manualApplyDraft ? (
        <ManualApplyConfirmModal
          busy={manualApplyBusy}
          currentVersion={currentAgentVersion || null}
          onCancel={() => setManualApplyDraft(null)}
          onConfirm={submitManualApply}
          task={manualApplyDraft.task}
        />
      ) : null}

      {toast ? <div className="fw-toast" onAnimationEnd={() => setToast(null)}>{toast}</div> : null}
    </div>
  );
}

function SignalsPanel({
  rows,
  selectedIds,
  selectedSource,
  actionId,
  onToggle,
  onSelectSource,
  onCreateBatch,
  onGenerateEvalCases,
}: {
  rows: SourceRow[];
  selectedIds: string[];
  selectedSource: SourceRow | null;
  actionId: string | null;
  onToggle: (sourceId: string, checked: boolean) => void;
  onSelectSource: (row: SourceRow) => void;
  onCreateBatch: () => void;
  onGenerateEvalCases: () => void;
}) {
  return (
    <section className="fw-panel fw-signals-page">
      <div className="fw-panel-header">
        <strong>反馈信息</strong>
        <div className="fw-panel-header-actions">
          <button className="fw-small-secondary" type="button" onClick={onGenerateEvalCases} disabled={!selectedIds.length || actionId === "generate-eval-cases"}>
            {actionId === "generate-eval-cases" ? <Loader2 size={16} className="fw-spin" /> : <Database size={16} />}
            生成回归用例
          </button>
          <button className="fw-small-primary" type="button" onClick={onCreateBatch} disabled={!selectedIds.length || actionId === "create-batch"}>
            {actionId === "create-batch" ? <Loader2 size={16} className="fw-spin" /> : <FolderKanban size={16} />}
            创建优化批次
          </button>
        </div>
      </div>
      <div className="fw-signal-layout">
        <div className="fw-signal-table">
          <div className="fw-signal-head">
            <span>选择</span>
            <span>类型</span>
            <span>反馈信息</span>
            <span>关联上下文</span>
            <span>时间</span>
            <span>状态</span>
          </div>
          {rows.map((row) => (
            <div
              aria-label={`查看 ${row.id} 详情`}
              className={`fw-signal-row ${selectedSource && sourceRowKey(selectedSource) === sourceRowKey(row) ? "is-active" : ""}`}
              key={sourceRowKey(row)}
              onClick={() => onSelectSource(row)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelectSource(row);
                }
              }}
              role="button"
              tabIndex={0}
            >
              <span onClick={(event) => event.stopPropagation()}>
                <input
                  aria-label={`选择 ${row.id}`}
                  checked={selectedIds.includes(row.id)}
                  disabled={row.kind === "pending_correlation" && row.status !== "resolved"}
                  onChange={(event) => onToggle(row.id, event.target.checked)}
                  type="checkbox"
                />
              </span>
              <span><Pill tone={row.kind === "pending_correlation" ? "orange" : row.kind === "soc_event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill></span>
              <span className="fw-signal-main">
                <strong>{row.label}</strong>
                <small title={row.id}>{shortId(row.id)} · {summaryText(row.raw)}</small>
              </span>
              <span className="fw-signal-context">
                <small title={row.runId || ""}>run：{shortId(row.runId)}</small>
                <small title={row.sessionId || ""}>session：{shortId(row.sessionId)}</small>
                <small title={row.caseId || row.alertId || ""}>case/alert：{shortId(row.caseId || row.alertId)}</small>
              </span>
              <span>{formatDate(row.createdAt)}</span>
              <span>{row.status}</span>
            </div>
          ))}
          {!rows.length ? <div className="fw-empty-inline">暂无反馈信息。Playground 会写入 /api/feedback-signals，SOC 系统可写入 /api/soc-events。</div> : null}
        </div>
        <SignalDetailPanel row={selectedSource} selectedIds={selectedIds} onToggle={onToggle} />
      </div>
    </section>
  );
}

function SignalDetailPanel({
  row,
  selectedIds,
  onToggle,
}: {
  row: SourceRow | null;
  selectedIds: string[];
  onToggle: (sourceId: string, checked: boolean) => void;
}) {
  if (!row) {
    return (
      <aside className="fw-signal-detail-panel">
        <div className="fw-empty-inline">选择一条反馈信息后查看详情。</div>
      </aside>
    );
  }
  const selected = selectedIds.includes(row.id);
  return (
    <aside className="fw-signal-detail-panel">
      <div className="fw-signal-detail-head">
        <div>
          <Pill tone={row.kind === "pending_correlation" ? "orange" : row.kind === "soc_event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill>
          <h3>{row.label}</h3>
          <small title={row.id}>{row.id}</small>
        </div>
        {row.kind !== "pending_correlation" || row.status === "resolved" ? (
          <button className={selected ? "fw-small-secondary" : "fw-small-primary"} onClick={() => onToggle(row.id, !selected)} type="button">
            {selected ? "已选择" : "加入批次"}
          </button>
        ) : null}
      </div>
      <div className="fw-signal-detail-grid">
        <Metric label="状态" value={row.status} />
        <Metric label="时间" value={formatDate(row.createdAt)} />
        <Metric label="run_id" value={row.runId || "-"} />
        <Metric label="session_id" value={row.sessionId || "-"} />
        <Metric label="case_id" value={row.caseId || "-"} />
        <Metric label="alert_id" value={row.alertId || "-"} />
        <Metric label="反馈单" value={shortId(row.feedbackCaseId)} />
        <Metric label="回归用例" value={shortId(row.evalCaseId)} />
      </div>
      <div className="fw-json-preview fw-json-preview-standalone">
        <div className="fw-json-preview-header">
          <strong>原始数据</strong>
          <span>{sourceKindText[row.kind]}</span>
        </div>
        <pre>{jsonPreview(row.raw)}</pre>
      </div>
    </aside>
  );
}

function BatchesPanel({
  actionId,
  batches,
  clientConfig,
  externalWebhooks,
  selectedBatch,
  sources,
  onApplyExecutionJob,
  onCreateExecutionJob,
  onExecutePlanTask,
  onGeneratePlan,
  onRejectPlan,
  onRunAttribution,
  onRunRegression,
  onSelectBatch,
}: {
  actionId: string | null;
  batches: FeedbackOptimizationBatchRecord[];
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  selectedBatch: FeedbackOptimizationBatchRecord | null;
  sources: FeedbackSourceRecord[];
  onApplyExecutionJob: (task: OptimizationTaskRecord) => void;
  onCreateExecutionJob: (task: OptimizationTaskRecord, force?: boolean) => void;
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
  onGeneratePlan: (batch: FeedbackOptimizationBatchRecord) => void;
  onRejectPlan: (batch: FeedbackOptimizationBatchRecord) => void;
  onRunAttribution: (batch: FeedbackOptimizationBatchRecord, force?: boolean) => void;
  onRunRegression: (batch: FeedbackOptimizationBatchRecord) => void;
  onSelectBatch: (batch: FeedbackOptimizationBatchRecord) => void;
}) {
  const [activeBatchDetail, setActiveBatchDetail] = useState<BatchDetailView>(() => defaultBatchDetail(selectedBatch));
  const batchSourceRows = useMemo(() => buildBatchSourceRows(selectedBatch, sources), [selectedBatch, sources]);
  const attributionJobs = useMemo(() => buildBatchAttributionJobs(selectedBatch), [selectedBatch]);
  const hasBatchAttribution = Boolean(attributionJobs.length || selectedBatch?.attribution_job_ids?.length);
  const planLocked = Boolean(
    selectedBatch?.optimization_plan?.status === "approved" ||
      selectedBatch?.optimization_task_id ||
      selectedBatch?.execution_job_id ||
      selectedBatch?.execution_apply_result,
  );
  const canRunBatchRegression = Boolean(selectedBatch?.optimization_task?.applied_agent_version_id);
  const regressionDisabledReason = selectedBatch?.optimization_task
    ? "执行方案尚未应用，未产生 Agent 版本，不能运行回归测试。"
    : "尚未执行优化方案，不能运行回归测试。";

  useEffect(() => {
    setActiveBatchDetail(defaultBatchDetail(selectedBatch));
  }, [selectedBatch?.batch_id]);

  return (
    <div className="fw-workspace-grid fw-batch-workspace">
      <section className="fw-panel fw-case-list-panel">
        <div className="fw-panel-header">
          <strong>优化批次</strong>
          <span className="fw-muted">{batches.length} 个</span>
        </div>
        <div className="fw-case-list">
          {batches.map((batch) => (
            <button
              className={`fw-case-card ${selectedBatch?.batch_id === batch.batch_id ? "is-active" : ""}`}
              key={batch.batch_id}
              onClick={() => onSelectBatch(batch)}
              type="button"
            >
              <span className="fw-case-main">
                <span className="fw-case-title"><strong>{shortId(batch.batch_id)}</strong>{batch.title}</span>
                <span className="fw-case-tags">
                  <Pill tone={batchStatusTone(batch.status)}>{batch.status}</Pill>
                  <Pill tone="blue">反馈 {batch.feedback_case_ids?.length || 0}</Pill>
                  <Pill tone="green">用例 {batch.eval_case_ids?.length || 0}</Pill>
                </span>
                <span className="fw-case-cause">更新：{formatDate(batch.updated_at)}</span>
              </span>
            </button>
          ))}
          {!batches.length ? <div className="fw-empty-inline">暂无优化批次。先在反馈信息中选择反馈并创建批次。</div> : null}
        </div>
      </section>

      <main className="fw-center-stack">
        {selectedBatch ? (
          <section className="fw-panel fw-batch-detail-panel">
            <div className="fw-panel-header">
              <div>
                <strong>{selectedBatch.title}</strong>
                <span className="fw-muted" title={selectedBatch.batch_id}> {shortId(selectedBatch.batch_id)}</span>
              </div>
              <Pill tone={batchStatusTone(selectedBatch.status)}>{selectedBatch.status}</Pill>
            </div>
            <BatchResultNav
              active={activeBatchDetail}
              attributionJobs={attributionJobs}
              batch={selectedBatch}
              feedbackCount={batchSourceRows.length || selectedBatch.source_refs?.length || 0}
              onChange={setActiveBatchDetail}
            />
            <div className="fw-current-case-actions fw-batch-actions">
              <button
                className="fw-small-secondary"
                type="button"
                disabled={Boolean(actionId)}
                onClick={() => {
                  setActiveBatchDetail("attribution");
                  onRunAttribution(selectedBatch, hasBatchAttribution);
                }}
              >
                {actionId === `batch-attribution:${selectedBatch.batch_id}` ? <Loader2 size={16} className="fw-spin" /> : <ShieldCheck size={16} />}
                {hasBatchAttribution ? "重新归因" : "运行归因分析"}
              </button>
              <button
                className="fw-small-secondary"
                type="button"
                disabled={Boolean(actionId) || !hasBatchAttribution || planLocked}
                title={planLocked ? "当前优化方案已执行或进入执行链路，请创建新批次后重新生成。" : undefined}
                onClick={() => {
                  setActiveBatchDetail("plan");
                  onGeneratePlan(selectedBatch);
                }}
              >
                {actionId === `batch-plan:${selectedBatch.batch_id}` ? <Loader2 size={16} className="fw-spin" /> : <MessageSquare size={16} />}
                {selectedBatch.optimization_plan ? "重新生成优化方案" : "生成优化方案"}
              </button>
              <button
                className="fw-small-secondary"
                type="button"
                disabled={Boolean(actionId) || !selectedBatch.optimization_plan || selectedBatch.optimization_plan.status !== "pending_approval"}
                onClick={() => {
                  setActiveBatchDetail("plan");
                  onRejectPlan(selectedBatch);
                }}
              >
                <XCircle size={16} />
                拒绝方案
              </button>
              <button
                className="fw-small-primary"
                type="button"
                disabled={Boolean(actionId) || !canRunBatchRegression}
                title={!canRunBatchRegression ? regressionDisabledReason : undefined}
                onClick={() => {
                  setActiveBatchDetail("regression");
                  onRunRegression(selectedBatch);
                }}
              >
                {actionId === `batch-regression:${selectedBatch.batch_id}` ? <Loader2 size={16} className="fw-spin" /> : <PlayCircle size={16} />}
                运行回归测试
              </button>
            </div>
            {activeBatchDetail === "feedback" ? <BatchFeedbackSourcesDetails rows={batchSourceRows} /> : null}
            {activeBatchDetail === "attribution" ? <BatchAttributionDetails jobs={attributionJobs} /> : null}
            {activeBatchDetail === "plan" ? (
              <>
                <BatchPlanDetails
                  actionId={actionId}
                  batch={selectedBatch}
                  externalWebhooks={externalWebhooks}
                  onExecutePlanTask={onExecutePlanTask}
                />
                <BatchExecutionSummary batch={selectedBatch} />
                {selectedBatch.optimization_task ? (
                  <TasksDetails
                    clientConfig={clientConfig}
                    tasks={[selectedBatch.optimization_task]}
                    actionId={actionId}
                    onCreateExecutionJob={onCreateExecutionJob}
                    onApplyExecutionJob={onApplyExecutionJob}
                  />
                ) : null}
              </>
            ) : null}
            {activeBatchDetail === "regression" ? <BatchRegressionDetails batch={selectedBatch} /> : null}
          </section>
        ) : (
          <section className="fw-panel fw-empty-workspace">
            <FolderKanban size={28} />
            <h3>暂无优化批次</h3>
            <p>从反馈信息中选择若干反馈，创建一个批次后再执行归因、优化和回归测试。</p>
          </section>
        )}
      </main>
    </div>
  );
}

function BatchResultNav({
  active,
  attributionJobs,
  batch,
  feedbackCount,
  onChange,
}: {
  active: BatchDetailView;
  attributionJobs: FeedbackAnalysisJobRecord[];
  batch: FeedbackOptimizationBatchRecord;
  feedbackCount: number;
  onChange: (view: BatchDetailView) => void;
}) {
  const attributionTotal = Math.max(attributionJobs.length, batch.attribution_job_ids?.length || 0);
  const items: Array<{
    key: BatchDetailView;
    title: string;
    value: string;
    hint: string;
    tone: "blue" | "green" | "orange" | "red" | "gray" | "purple";
    icon: ReactNode;
  }> = [
    {
      key: "feedback",
      title: "反馈信息",
      value: `${feedbackCount} 条`,
      hint: "查看本批次纳入的反馈原文、标签和关联用例",
      tone: feedbackCount ? "blue" : "gray",
      icon: <FileText size={17} />,
    },
    {
      key: "attribution",
      title: "归因结果",
      value: attributionStatusText(attributionJobs, attributionTotal),
      hint: attributionTotal ? "查看逐条归因、责任边界和引用证据" : "运行归因分析后展示结果",
      tone: attributionStatusTone(attributionJobs, attributionTotal),
      icon: <ShieldCheck size={17} />,
    },
    {
      key: "plan",
      title: "优化方案",
      value: batch.optimization_plan?.status || "未生成",
      hint: batch.optimization_plan ? batchPlanDisplayTitle(batch) : "统筹归因结果后生成待执行方案",
      tone: batch.optimization_plan ? batchStatusTone(batch.optimization_plan.status) : "gray",
      icon: <MessageSquare size={17} />,
    },
    {
      key: "regression",
      title: "回归测试结果",
      value: batchRegressionStatusText(batch),
      hint: batch.latest_eval_run ? "查看用例执行过程、检查结果和错误信息" : "优化应用后运行批次回归测试",
      tone: batch.latest_eval_run ? evalStatusTone(batch.latest_eval_run.result_status || batch.latest_eval_run.status) : "gray",
      icon: <PlayCircle size={17} />,
    },
  ];

  return (
    <div className="fw-batch-result-nav" role="tablist" aria-label="批次详情与结果查看区">
      {items.map((item) => (
        <button
          aria-selected={active === item.key}
          className={`fw-batch-result-tab ${active === item.key ? "is-active" : ""}`}
          key={item.key}
          onClick={() => onChange(item.key)}
          role="tab"
          type="button"
        >
          <span className={`fw-batch-result-icon fw-pill-${item.tone}`}>{item.icon}</span>
          <span className="fw-batch-result-main">
            <span>{item.title}</span>
            <strong>{item.value}</strong>
            <small>{item.hint}</small>
          </span>
          <ChevronRight size={16} />
        </button>
      ))}
    </div>
  );
}

function BatchFeedbackSourcesDetails({ rows }: { rows: SourceRow[] }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const selectedRow = useMemo(() => {
    if (!rows.length) return null;
    if (selectedKey) {
      const matched = rows.find((row) => sourceRowKey(row) === selectedKey);
      if (matched) return matched;
    }
    return rows[0];
  }, [rows, selectedKey]);

  useEffect(() => {
    setSelectedKey((current) => {
      if (current && rows.some((row) => sourceRowKey(row) === current)) return current;
      return rows[0] ? sourceRowKey(rows[0]) : null;
    });
  }, [rows]);

  if (!rows.length) {
    return (
      <section className="fw-task-source fw-batch-feedback-section">
        <div className="fw-task-section-head">
          <h4>反馈信息</h4>
          <small>当前批次没有可展示的反馈来源。</small>
        </div>
      </section>
    );
  }

  return (
    <section className="fw-task-source fw-batch-feedback-section">
      <div className="fw-task-section-head">
        <h4>反馈信息</h4>
        <small>点击左侧列表项查看当前批次中每条反馈的详情和原始数据。</small>
      </div>
      <div className="fw-batch-feedback-layout">
        <div className="fw-batch-feedback-list" role="list">
          {rows.map((row) => (
            <button
              className={selectedRow && sourceRowKey(selectedRow) === sourceRowKey(row) ? "is-active" : ""}
              key={sourceRowKey(row)}
              onClick={() => setSelectedKey(sourceRowKey(row))}
              type="button"
            >
              <span>
                <Pill tone={row.kind === "pending_correlation" ? "orange" : row.kind === "soc_event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill>
                <strong>{row.label}</strong>
              </span>
              <small title={row.id}>{shortId(row.id)} · {row.status} · {formatDate(row.createdAt)}</small>
            </button>
          ))}
        </div>
        <div className="fw-batch-feedback-detail">
          {selectedRow ? (
            <>
              <div className="fw-signal-detail-head">
                <div>
                  <Pill tone={selectedRow.kind === "pending_correlation" ? "orange" : selectedRow.kind === "soc_event" ? "green" : "blue"}>
                    {sourceKindText[selectedRow.kind]}
                  </Pill>
                  <h3>{selectedRow.label}</h3>
                  <small title={selectedRow.id}>{selectedRow.id}</small>
                </div>
              </div>
              <div className="fw-signal-detail-grid">
                <Metric label="状态" value={selectedRow.status} />
                <Metric label="时间" value={formatDate(selectedRow.createdAt)} />
                <Metric label="run_id" value={shortId(selectedRow.runId)} />
                <Metric label="session_id" value={shortId(selectedRow.sessionId)} />
                <Metric label="反馈单" value={shortId(selectedRow.feedbackCaseId)} />
                <Metric label="回归用例" value={shortId(selectedRow.evalCaseId)} />
              </div>
              <div className="fw-json-preview fw-json-preview-standalone">
                <div className="fw-json-preview-header">
                  <strong>反馈原始数据</strong>
                  <span>{sourceKindText[selectedRow.kind]}</span>
                </div>
                <pre>{jsonPreview(selectedRow.raw)}</pre>
              </div>
            </>
          ) : (
            <div className="fw-empty-inline">选择一条反馈信息后查看详情。</div>
          )}
        </div>
      </div>
    </section>
  );
}

function BatchAttributionDetails({
  jobs,
}: {
  jobs: FeedbackAnalysisJobRecord[];
}) {
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const selectedJob = useMemo(() => {
    if (!jobs.length) return null;
    if (selectedJobId) {
      const matched = jobs.find((job) => job.job_id === selectedJobId);
      if (matched) return matched;
    }
    return jobs[0];
  }, [jobs, selectedJobId]);

  useEffect(() => {
    setSelectedJobId((current) => {
      if (current && jobs.some((job) => job.job_id === current)) return current;
      return jobs[0]?.job_id || null;
    });
  }, [jobs]);

  return (
    <section className="fw-task-source fw-batch-attribution-section">
      <div className="fw-task-section-head">
        <h4>归因分析结果</h4>
        <small>点击左侧归因任务查看结构化归因、责任边界、引用证据和错误详情。</small>
      </div>
      {jobs.length ? (
        <div className="fw-batch-attribution-layout">
          <div className="fw-batch-attribution-list" role="list">
            {jobs.map((job) => {
              const output = attributionOutputFromJob(job);
              return (
                <button
                  className={selectedJob?.job_id === job.job_id ? "is-active" : ""}
                  key={job.job_id}
                  onClick={() => setSelectedJobId(job.job_id)}
                  type="button"
                >
                  <span>
                    <Pill tone={jobStatusTone(job.status)}>{job.status}</Pill>
                    <strong>{shortId(job.job_id)}</strong>
                  </span>
                  <small>{output?.problem_type || profileDisplayName(job.profile_name)} · 反馈单 {shortId(job.feedback_case_id)}</small>
                </button>
              );
            })}
          </div>
          <div className="fw-batch-attribution-detail">
            {selectedJob ? <BatchAttributionJobDetail job={selectedJob} /> : <div className="fw-empty-inline">选择一个归因任务后查看详情。</div>}
          </div>
        </div>
      ) : (
        <p className="fw-note-box">归因分析正在启动或等待刷新；完成后这里会显示每条反馈对应的归因结果。</p>
      )}
    </section>
  );
}

function BatchAttributionJobDetail({ job }: { job: FeedbackAnalysisJobRecord }) {
  const output = attributionOutputFromJob(job);
  return (
    <div className="fw-batch-attribution-job-detail">
      <DetailMetricGrid
        items={[
          ["job_id", shortId(job.job_id)],
          ["状态", job.status],
          ["反馈单", shortId(job.feedback_case_id)],
          ["证据包", shortId(job.evidence_package_id)],
          ["创建", formatDate(job.created_at)],
          ["完成", formatDate(job.completed_at)],
        ]}
      />
      {output ? (
        <AttributionResult output={output} />
      ) : job.error_json ? (
        <div className="fw-job-error">
          <strong>{job.error_json.error_code || "ATTRIBUTION_FAILED"}</strong>
          <FormattedText value={job.error_json.message || "归因分析未生成可用结果。"} />
        </div>
      ) : (
        <p className="fw-note-box">当前归因任务状态为 {job.status}，尚未产生结构化归因结果。</p>
      )}
      <details className="fw-batch-attribution-raw">
        <summary>查看原始输出与输入</summary>
        <DetailJsonPreview title="归因输出" value={job.validated_output_json || job.raw_output_json || job.error_json || {}} />
        <DetailJsonPreview title="任务输入" value={job.input_json || {}} />
      </details>
    </div>
  );
}

function BatchPlanDetails({
  actionId,
  batch,
  externalWebhooks,
  onExecutePlanTask,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
}) {
  const plan = batch.optimization_plan;
  if (!plan) {
    return (
      <section className="fw-task-source fw-batch-plan-section">
        <div className="fw-task-section-head">
          <h4>优化方案</h4>
          <small>统筹归因结果后生成待执行方案。</small>
        </div>
        <p className="fw-note-box">尚未生成优化方案。先运行归因分析，再生成统筹优化方案。</p>
      </section>
    );
  }
  const tasks = (plan.tasks || []).filter((task) => task.execution_kind === "workspace_execution" || task.execution_kind === "external_webhook");
  const blockedItems = plan.blocked_items || [];
  const displayTitle = batchPlanDisplayTitle(batch);
  return (
    <section className="fw-task-source fw-batch-plan-section">
      <div className="fw-task-section-head">
        <h4>优化方案</h4>
        <Pill tone={plan.status === "pending_approval" ? "orange" : plan.status === "approved" ? "green" : "gray"}>{plan.status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["优化任务", tasks.length],
          ["未形成任务", blockedItems.length],
          ["关联反馈", plan.feedback_case_ids?.length || batch.feedback_case_ids?.length || 0],
          ["关联用例", plan.eval_case_ids?.length || batch.eval_case_ids?.length || 0],
        ]}
      />
      {plan.regeneration_instruction ? <FormattedTextSection title="补充优化要求" value={plan.regeneration_instruction} compact /> : null}
      <FormattedTextSection title={displayTitle} value={plan.recommendation || "-"} />
      <FormattedTextFields
        fields={[
          ["预期效果", plan.expected_effect || "-"],
          ["回归测试", plan.validation || "-"],
          ["风险", plan.risk || "-"],
        ]}
      />
      {plan.rationale ? (
        <details className="fw-plan-task-disclosure">
          <summary>查看方案分析</summary>
          <FormattedTextSection title="归因依据" value={plan.rationale} compact />
        </details>
      ) : null}
      <div className="fw-plan-task-list">
        <div className="fw-task-section-head">
          <h4>优化任务</h4>
          <small>{tasks.length ? `${tasks.length} 个任务，可按任务类型分别执行` : "当前方案未形成可执行优化任务"}</small>
        </div>
        {tasks.map((task) => (
          <BatchPlanTaskCard
            actionId={actionId}
            batch={batch}
            externalWebhooks={externalWebhooks}
            key={task.plan_task_id}
            planTask={task}
            onExecutePlanTask={onExecutePlanTask}
          />
        ))}
        {!tasks.length ? <p className="fw-note-box">当前优化方案没有可执行任务。请查看下方原因，必要时重新归因或重新生成优化方案。</p> : null}
      </div>
      {blockedItems.length ? (
        <div className="fw-plan-task-list">
          <div className="fw-task-section-head">
            <h4>未形成可执行任务的原因</h4>
            <small>{blockedItems.length} 个阻塞项，仅用于诊断，不作为优化任务执行。</small>
          </div>
          {blockedItems.map((item) => (
            <BatchPlanBlockedItemCard item={item} key={item.blocked_item_id || `${item.target_type}:${item.source_index}`} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function BatchPlanBlockedItemCard({ item }: { item: FeedbackOptimizationBlockedItemRecord }) {
  return (
    <article className="fw-plan-task-card fw-plan-blocked-card">
      <div className="fw-proposal-detail-title">
        <Pill tone="orange">{item.status || "blocked"}</Pill>
        <h4>{item.title || "未形成可执行优化任务"}</h4>
        <small>{item.target_type || "not_actionable"}</small>
      </div>
      <DetailMetricGrid
        items={[
          ["目标类型", item.target_type || "-"],
          ["目标文件", item.target_path || "-"],
          ["负责人", item.owner || "-"],
          ["归因任务", (item.attribution_job_ids || []).map(shortId).join(", ") || "-"],
        ]}
      />
      {item.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={item.reason} /> : null}
      {item.recommendation ? <FormattedText className="fw-proposal-long-text" value={item.recommendation} /> : null}
      {item.analysis_summary || item.evidence_summary ? (
        <details className="fw-plan-task-disclosure">
          <summary>查看分析过程</summary>
          {item.analysis_summary ? <FormattedTextSection title="分析摘要" value={item.analysis_summary} compact /> : null}
          {item.evidence_summary ? <FormattedTextSection title="证据摘要" value={item.evidence_summary} compact /> : null}
        </details>
      ) : null}
      <div className="fw-external-notify-meta">
        <span>阻塞项：{shortId(item.blocked_item_id)}</span>
        <span>操作建议：重新归因或重新生成优化方案</span>
      </div>
    </article>
  );
}

function batchPlanDisplayTitle(batch: FeedbackOptimizationBatchRecord): string {
  const plan = batch.optimization_plan;
  if (!plan) return "统筹归因结果生成优化方案";
  const rawTitle = typeof plan.title === "string" ? plan.title : "";
  const technicalType = String(plan.target_type || plan.optimization_object_type || "");
  if (rawTitle && (!technicalType || !rawTitle.includes(technicalType))) {
    return rawTitle;
  }
  const count = plan.feedback_case_ids?.length || batch.feedback_case_ids?.length || 0;
  return count ? `统筹 ${count} 条反馈生成优化方案` : "统筹归因结果生成优化方案";
}

function PlanTaskListSection({ items, title }: { items?: string[]; title: string }) {
  const visibleItems = (items || []).filter(Boolean);
  if (!visibleItems.length) return null;
  return (
    <section className="fw-plan-task-list-section">
      <h5>{title}</h5>
      <ul>
        {visibleItems.map((item, index) => (
          <li key={`${title}:${index}`}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

function PlanTaskContextSummary({ context }: { context?: Record<string, unknown> }) {
  if (!context || !Object.keys(context).length) return null;
  const text = (key: string) => {
    const value = context[key];
    if (Array.isArray(value)) return value.filter((item) => typeof item === "string" && item).join(", ");
    return typeof value === "string" ? value : "";
  };
  return (
    <DetailMetricGrid
      items={[
        ["MCP Server", text("mcp_server") || "-"],
        ["工具", text("tool_name") || "-"],
        ["接口", text("endpoint") || text("api_path") || text("api_name") || "-"],
        ["查询对象", text("query_ids") || text("dates") || "-"],
        ["字段", text("affected_fields") || "-"],
      ]}
    />
  );
}

function BatchPlanTaskCard({
  actionId,
  batch,
  externalWebhooks,
  planTask,
  onExecutePlanTask,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  planTask: FeedbackOptimizationPlanTaskRecord;
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
}) {
  const [selectedAlias, setSelectedAlias] = useState(externalWebhooks[0]?.alias || "");
  const currentAlias = selectedAlias || externalWebhooks[0]?.alias || "";
  const running = actionId === `plan-task:${planTask.plan_task_id}`;
  const executionKind = planTask.execution_kind || "workspace_execution";
  const workspaceDone = Boolean(planTask.applied_agent_version_id);
  const external = executionKind === "external_webhook";
  const workspace = executionKind === "workspace_execution";
  const canExecute = workspace
    ? !workspaceDone && !running
    : external
      ? Boolean(currentAlias && externalWebhooks.length && !running)
      : false;
  const buttonLabel = workspace
    ? planTask.execution_job_id && !workspaceDone ? "重试执行" : "执行"
    : planTask.status === "notification_failed" ? "重试发送" : "发送任务";
  const targetSummary = planTask.target_summary || (workspace ? `workspace:${planTask.target_path || "-"}` : `external:${planTask.owner || planTask.target_type || "-"}`);
  const feedbackCount = planTask.feedback_case_ids?.length || 0;
  const evalCount = planTask.eval_case_ids?.length || 0;
  const taskScopeLabel = workspace ? "受管 workspace 优化" : external ? "外部系统优化" : "优化任务";
  return (
    <article className="fw-plan-task-card">
      <div className="fw-proposal-detail-title">
        <Pill tone={planTaskTone(planTask)}>{planTask.status || executionKind}</Pill>
        <h4>{planTask.title || shortId(planTask.plan_task_id)}</h4>
        <small>{taskScopeLabel}</small>
      </div>
      <FormattedText className="fw-proposal-long-text fw-plan-task-description" value={planTask.description || planTask.recommendation || "-"} />
      <div className="fw-plan-task-text-grid">
        <FormattedTextSection title="任务目标" value={planTask.objective || "-"} compact />
        <FormattedTextSection title="风险/注意事项" value={planTask.risk || "暂无明显额外风险。"} compact />
      </div>
      <PlanTaskListSection title="验收标准" items={planTask.acceptance_criteria} />
      {planTask.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={planTask.reason} /> : null}
      {planTask.recommended_actions?.length || planTask.analysis_summary || planTask.evidence_summary || planTask.rationale ? (
        <details className="fw-plan-task-disclosure">
          <summary>执行与调试信息</summary>
          <PlanTaskListSection title="执行提示" items={planTask.recommended_actions} />
          <FormattedTextFields
            fields={[
              ["预期效果", planTask.expected_effect || "-"],
              ["回归测试", planTask.validation || "-"],
            ]}
          />
          <PlanTaskContextSummary context={planTask.task_context} />
          <DetailMetricGrid
            items={[
              ["执行对象", targetSummary],
              ["目标类型", planTask.target_type || "-"],
              ["目标文件/系统", workspace ? planTask.target_path || "-" : planTask.owner || "-"],
              ["反馈/用例", `${feedbackCount} / ${evalCount}`],
              ["归因任务", (planTask.attribution_job_ids || []).map(shortId).join(", ") || "-"],
            ]}
          />
          {planTask.analysis_summary ? <FormattedTextSection title="分析摘要" value={planTask.analysis_summary} compact /> : null}
          {planTask.evidence_summary ? <FormattedTextSection title="证据摘要" value={planTask.evidence_summary} compact /> : null}
          {planTask.rationale ? <FormattedTextSection title="归因全文" value={planTask.rationale} compact /> : null}
          <div className="fw-external-notify-meta">
            <span>任务：{shortId(planTask.plan_task_id)}</span>
            {planTask.optimization_task_id ? <span>优化任务：{shortId(planTask.optimization_task_id)}</span> : null}
            {planTask.execution_job_id ? <span>执行方案：{shortId(planTask.execution_job_id)}</span> : null}
            {planTask.external_item_id ? <span>外部任务：{shortId(planTask.external_item_id)}</span> : null}
            {planTask.latest_webhook_alias ? <span>最近目标：{planTask.latest_webhook_alias}</span> : null}
          </div>
        </details>
      ) : null}
      <div className="fw-detail-action-row fw-plan-task-actions">
        {external ? (
          <label className="fw-select-field">
            <span>Webhook</span>
            <select value={currentAlias} onChange={(event) => setSelectedAlias(event.target.value)} disabled={!externalWebhooks.length || running}>
              {!externalWebhooks.length ? <option value="">未配置Webhook，请在 /data/external-governance-webhooks.yaml 文件中增加</option> : null}
              {externalWebhooks.map((webhook) => (
                <option key={webhook.alias} value={webhook.alias}>{webhook.name || webhook.alias}</option>
              ))}
            </select>
          </label>
        ) : null}
        {workspace || external ? (
          <button className="fw-small-primary" type="button" disabled={!canExecute} onClick={() => onExecutePlanTask(batch, planTask, external ? currentAlias : undefined)}>
            {running ? <Loader2 size={16} className="fw-spin" /> : workspace ? <CheckCircle2 size={16} /> : <ChevronRight size={16} />}
            {running ? "执行中" : buttonLabel}
          </button>
        ) : (
          <Pill tone="orange">需人工复核</Pill>
        )}
      </div>
    </article>
  );
}

function BatchExecutionSummary({ batch }: { batch: FeedbackOptimizationBatchRecord }) {
  const task = batch.optimization_task || null;
  const execution = task?.latest_execution_job || batch.execution_job || null;
  if (!task && !execution) return null;
  const output = execution?.validated_output_json || null;
  const operations = output?.operations || [];
  const appliedVersion = task?.applied_agent_version_id || execution?.applied_agent_version_id || null;
  const noActionReason = output?.no_action_reason || execution?.error_json?.message || null;
  const nextStep = appliedVersion
    ? "优化已应用并产生 Agent 版本，可以运行回归测试。"
    : execution?.status === "ready"
      ? "执行方案已 ready，请先应用执行方案以产生 Agent 版本。"
      : execution
        ? "执行方案尚未可应用，请查看未执行原因或重新生成执行方案。"
        : "优化任务已创建，等待生成执行方案。";
  return (
    <section className="fw-task-source fw-batch-execution-summary">
      <div className="fw-task-section-head">
        <h4>执行状态</h4>
        <Pill tone={appliedVersion ? "green" : execution?.status === "ready" ? "blue" : execution ? jobStatusTone(execution.status) : "gray"}>
          {appliedVersion ? "applied" : execution?.status || task?.status || "pending"}
        </Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["优化任务", shortId(task?.optimization_task_id)],
          ["执行方案", shortId(execution?.execution_job_id)],
          ["操作数", operations.length],
          ["应用版本", shortId(appliedVersion)],
        ]}
      />
      <p className="fw-note-box">{nextStep}</p>
      {noActionReason ? <FormattedTextSection title="未执行原因" value={String(noActionReason)} compact /> : null}
    </section>
  );
}

function BatchRegressionDetails({ batch }: { batch: FeedbackOptimizationBatchRecord }) {
  const run = batch.latest_eval_run || null;
  if (!run) {
    return (
      <section className="fw-task-source fw-task-regression-section fw-batch-regression-section">
        <div className="fw-task-section-head">
          <h4>回归测试</h4>
          <small>优化应用后可运行本批次关联的回归用例。</small>
        </div>
        <p className="fw-note-box">尚未运行批次回归测试。</p>
      </section>
    );
  }
  return (
    <section className="fw-task-source fw-task-regression-section fw-batch-regression-section">
      <div className="fw-task-section-head">
        <h4>回归测试结果</h4>
        <Pill tone={evalStatusTone(run.result_status || run.status)}>{run.result_status || run.status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["eval_run", shortId(run.eval_run_id)],
          ["版本", shortId(run.agent_version_id)],
          ["总数", run.summary?.total ?? 0],
          ["通过", run.summary?.passed ?? 0],
          ["失败", run.summary?.failed ?? 0],
          ["需复核", run.summary?.needs_human_review ?? 0],
        ]}
      />
      <div className="fw-batch-regression-list">
        {(run.items || []).map((item) => (
          <details className="fw-eval-item-detail" key={item.eval_run_item_id}>
            <summary>
              <span>{shortId(item.eval_case_id)}</span>
              <Pill tone={evalStatusTone(item.status)}>{item.status}</Pill>
              <strong>查看详情</strong>
            </summary>
            <FormattedText value={evalItemSummary(item)} />
            <DetailJsonPreview title="检查结果" value={item.check_results || []} />
            {item.error_json ? <DetailJsonPreview title="错误信息" value={item.error_json} /> : null}
          </details>
        ))}
      </div>
    </section>
  );
}

function CasesPanel({
  cases,
  selectedCase,
  selectedCaseRuns,
  selectedCaseProposals,
  selectedCaseTasks,
  selectedCaseExternalItems,
  selectedCaseEvalCases,
  evalRuns,
  details,
  detailView,
  attributionDetailTab,
  detailsLoading,
  actionId,
  clientConfig,
  onSelectCase,
  onSelectDetailView,
  onOpenAttributionTab,
  onAttributionDetailTabChange,
  onCreateEvidence,
  onRunAttribution,
  onRunProposal,
  onReviewProposal,
  onCreateTask,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onNotifyExternalItem,
  onOpenTask,
  onMarkTaskApplied,
  onRunTaskRegression,
  onRegenerateAttribution,
  onRegenerateProposal,
  onRevalidateProposalJob,
  onUpdateEvalCase,
  tasksByProposalId,
  externalWebhooks,
}: {
  cases: FeedbackCaseRecord[];
  selectedCase: FeedbackCaseRecord | null;
  selectedCaseRuns: FeedbackRunRecord[];
  selectedCaseProposals: OptimizationProposalRecord[];
  selectedCaseTasks: OptimizationTaskRecord[];
  selectedCaseExternalItems: ExternalGovernanceItemRecord[];
  selectedCaseEvalCases: EvalCaseRecord[];
  evalRuns: EvalRunRecord[];
  details: CaseDetails;
  detailView: CaseDetailView;
  attributionDetailTab: AttributionDetailTab;
  detailsLoading: boolean;
  actionId: string | null;
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  onSelectCase: (feedbackCase: FeedbackCaseRecord) => void;
  onSelectDetailView: (view: CaseDetailView) => void;
  onOpenAttributionTab: (tab: AttributionDetailTab) => void;
  onAttributionDetailTabChange: (tab: AttributionDetailTab) => void;
  onCreateEvidence: () => void;
  onRunAttribution: () => void;
  onRunProposal: () => void;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onCreateExecutionJob: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob: (task: OptimizationTaskRecord) => void;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  onMarkTaskApplied: (task: OptimizationTaskRecord) => void;
  onRunTaskRegression: (task: OptimizationTaskRecord) => void;
  onRegenerateAttribution: (feedbackCaseId: string) => void;
  onRegenerateProposal: (feedbackCaseId: string) => void;
  onRevalidateProposalJob: (jobId: string) => void;
  onUpdateEvalCase: (evalCaseId: string, payload: EvalCaseUpdateRequest) => Promise<boolean>;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
  externalWebhooks: ExternalGovernanceWebhookRecord[];
}) {
  const evidenceCount = selectedCase?.evidence_package_ids.length || 0;
  const attributionCount = selectedCase?.attribution_job_ids.length || 0;
  const proposalJobCount = selectedCase?.proposal_job_ids.length || 0;
  const proposalItemCount = selectedCaseProposals.length + selectedCaseExternalItems.length;
  const attributionStatus = details.attributionJob?.status;
  const proposalStatus = details.proposalJob?.status;
  const attributionNeedsReview = attributionStatus === "needs_human_review" && Boolean(details.attributionJob);
  const attributionLocked = attributionCount > 0 && !isRetryableJobStatus(attributionStatus) && !attributionNeedsReview;
  const proposalLocked = proposalJobCount > 0 && !isRetryableJobStatus(proposalStatus) && proposalStatus !== "needs_human_review";
  const actionRunning = Boolean(actionId);

  return (
    <div className="fw-workspace-grid">
      <section className="fw-panel fw-case-list-panel">
        <div className="fw-panel-header">
          <strong>反馈处置单</strong>
          <FolderKanban size={18} />
        </div>
        <div className="fw-case-list">
          {cases.map((item) => (
            <button
              className={`fw-case-card ${selectedCase?.feedback_case_id === item.feedback_case_id ? "is-active" : ""}`}
              key={item.feedback_case_id}
              onClick={() => onSelectCase(item)}
              type="button"
            >
              <span className="fw-case-main">
                <span className="fw-case-title"><strong>{shortId(item.feedback_case_id)}</strong>{item.title}</span>
                <span className="fw-case-tags">
                  <Pill tone="blue">信号 {item.signal_ids.length}</Pill>
                  <Pill tone="green">事件 {item.event_ids.length}</Pill>
                  <Pill tone={item.status === "needs_human_review" ? "orange" : "gray"}>{caseStatusText[item.status] || item.status}</Pill>
                </span>
                <span className="fw-case-cause">更新：{formatDate(item.updated_at)}</span>
              </span>
            </button>
          ))}
          {!cases.length ? <div className="fw-empty-inline">暂无反馈处置单</div> : null}
        </div>
      </section>

      <main className="fw-center-stack">
        {selectedCase ? (
          <>
            <section className="fw-panel fw-current-case-panel">
              <div className="fw-panel-header">
                <div>
                  <strong>{selectedCase.title}</strong>
                  <span className="fw-muted" title={selectedCase.feedback_case_id}> {shortId(selectedCase.feedback_case_id)}</span>
                </div>
                <Pill tone={selectedCase.priority === "high" ? "red" : selectedCase.priority === "low" ? "gray" : "orange"}>{selectedCase.priority}</Pill>
              </div>
              <div className="fw-current-case-grid">
                <Metric label="状态" value={caseStatusText[selectedCase.status] || selectedCase.status} />
                <DetailMetric label="证据包" count={evidenceCount} active={detailView === "evidence"} onClick={() => onSelectDetailView("evidence")} />
                <DetailMetric label="归因分析" count={attributionCount} active={detailView === "attribution"} onClick={() => onSelectDetailView("attribution")} />
                <DetailMetric label="优化方案" count={proposalItemCount || proposalJobCount} active={detailView === "proposal"} onClick={() => onSelectDetailView("proposal")} />
                <DetailMetric label="关联运行" count={selectedCase.run_ids.length} active={detailView === "runs"} onClick={() => onSelectDetailView("runs")} />
                <DetailMetric label="优化任务" count={selectedCaseTasks.length} active={detailView === "tasks"} onClick={() => onSelectDetailView("tasks")} />
                <DetailMetric label="评估用例" count={selectedCaseEvalCases.length} active={detailView === "evals"} onClick={() => onSelectDetailView("evals")} />
              </div>
              <div className="fw-current-case-actions">
                <button className="fw-small-secondary" type="button" onClick={onCreateEvidence} disabled={actionRunning || evidenceCount > 0}>
                  {actionId?.startsWith("evidence:") ? <Loader2 size={16} className="fw-spin" /> : <FileArchive size={16} />}
                  {evidenceCount > 0 ? "证据包已生成" : "生成证据包"}
                </button>
                <button className="fw-small-secondary" type="button" onClick={onRunAttribution} disabled={actionRunning || attributionLocked}>
                  {actionId?.startsWith("attribution:") ? <Loader2 size={16} className="fw-spin" /> : <ShieldCheck size={16} />}
                  {analysisActionLabel("attribution", attributionStatus, attributionCount)}
                </button>
                <button className="fw-small-primary" type="button" onClick={onRunProposal} disabled={actionRunning || proposalLocked || !details.attribution}>
                  {actionId?.startsWith("proposal:") ? <Loader2 size={16} className="fw-spin" /> : <MessageSquare size={16} />}
                  {analysisActionLabel("proposal", proposalStatus, proposalJobCount)}
                </button>
              </div>
              {attributionNeedsReview && details.attributionJob ? (
                <AttributionReviewNotice
                  busy={actionId?.startsWith("attribution-regenerate:") || false}
                  job={details.attributionJob}
                  onOpenDetails={() => onOpenAttributionTab("result")}
                  onOpenRaw={() => onOpenAttributionTab("raw")}
                  onRegenerate={() => selectedCase && onRegenerateAttribution(selectedCase.feedback_case_id)}
                />
              ) : null}
            </section>

            <CaseDetailPanel
              actionId={actionId}
              attributionDetailTab={attributionDetailTab}
              clientConfig={clientConfig}
              detailView={detailView}
              details={details}
              detailsLoading={detailsLoading}
              onSelectDetailView={onSelectDetailView}
              onCreateTask={onCreateTask}
              onCreateExecutionJob={onCreateExecutionJob}
              onApplyExecutionJob={onApplyExecutionJob}
              onNotifyExternalItem={onNotifyExternalItem}
              onOpenTask={onOpenTask}
              onMarkTaskApplied={onMarkTaskApplied}
              onRunTaskRegression={onRunTaskRegression}
              onReviewProposal={onReviewProposal}
              onRegenerateAttribution={onRegenerateAttribution}
              onAttributionDetailTabChange={onAttributionDetailTabChange}
              onRegenerateProposal={onRegenerateProposal}
              onRevalidateProposalJob={onRevalidateProposalJob}
              onUpdateEvalCase={onUpdateEvalCase}
              evalCases={selectedCaseEvalCases}
              evalRuns={evalRuns}
              runs={selectedCaseRuns}
              tasks={selectedCaseTasks}
              tasksByProposalId={tasksByProposalId}
              proposals={selectedCaseProposals}
              externalGovernanceItems={selectedCaseExternalItems}
              externalWebhooks={externalWebhooks}
            />
          </>
        ) : (
          <section className="fw-panel fw-empty-workspace">
            <MessageSquare size={28} />
            <h3>暂无反馈处置单</h3>
            <p>先在反馈信息中选择 signal 或已关联 SOC event 创建处置单。</p>
          </section>
        )}
      </main>
    </div>
  );
}

function CaseDetailPanel({
  actionId,
  attributionDetailTab,
  clientConfig,
  detailView,
  details,
  detailsLoading,
  runs,
  proposals,
  externalGovernanceItems,
  externalWebhooks,
  tasks,
  evalCases,
  evalRuns,
  onSelectDetailView,
  onReviewProposal,
  onCreateTask,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onNotifyExternalItem,
  onOpenTask,
  onMarkTaskApplied,
  onRunTaskRegression,
  onRegenerateAttribution,
  onAttributionDetailTabChange,
  onRegenerateProposal,
  onRevalidateProposalJob,
  onUpdateEvalCase,
  tasksByProposalId,
}: {
  actionId: string | null;
  attributionDetailTab: AttributionDetailTab;
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  detailView: CaseDetailView;
  details: CaseDetails;
  detailsLoading: boolean;
  runs: FeedbackRunRecord[];
  proposals: OptimizationProposalRecord[];
  externalGovernanceItems: ExternalGovernanceItemRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  tasks: OptimizationTaskRecord[];
  evalCases: EvalCaseRecord[];
  evalRuns: EvalRunRecord[];
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
  onSelectDetailView: (view: CaseDetailView) => void;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onCreateExecutionJob: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob: (task: OptimizationTaskRecord) => void;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  onMarkTaskApplied: (task: OptimizationTaskRecord) => void;
  onRunTaskRegression: (task: OptimizationTaskRecord) => void;
  onRegenerateAttribution: (feedbackCaseId: string) => void;
  onAttributionDetailTabChange: (tab: AttributionDetailTab) => void;
  onRegenerateProposal: (feedbackCaseId: string) => void;
  onRevalidateProposalJob: (jobId: string) => void;
  onUpdateEvalCase: (evalCaseId: string, payload: EvalCaseUpdateRequest) => Promise<boolean>;
}) {
  const [copiedProposalJobId, setCopiedProposalJobId] = useState(false);
  const titleByView: Record<CaseDetailView, string> = {
    summary: "处置摘要",
    evidence: "证据包详情",
    attribution: "归因分析详情",
    proposal: "优化方案详情",
    runs: "关联运行详情",
    tasks: "优化任务详情",
    evals: "评估用例详情",
  };
  const attributionJob = latestItem(details.attributionJobs);
  const proposalJob = latestItem(details.proposalJobs);
  const proposalJobId = proposalJob?.job_id || details.proposal?.proposal_job_id || null;
  const canRegenerateAttribution = Boolean(attributionJob?.feedback_case_id);
  const canRevalidateProposal = Boolean(proposalJob?.raw_output_json && proposalJob?.error_json);
  const canRegenerateProposal = Boolean(proposalJob?.feedback_case_id);
  const attributionBusy = actionId?.startsWith("attribution-regenerate:");
  const proposalBusy = actionId?.startsWith("proposal-revalidate:") || actionId?.startsWith("proposal-regenerate:");

  useEffect(() => {
    if (!copiedProposalJobId) return;
    const timer = window.setTimeout(() => setCopiedProposalJobId(false), 1200);
    return () => window.clearTimeout(timer);
  }, [copiedProposalJobId]);

  async function copyProposalJobId() {
    if (!proposalJobId) return;
    try {
      await navigator.clipboard?.writeText(proposalJobId);
    } finally {
      setCopiedProposalJobId(true);
    }
  }

  return (
    <section className="fw-panel fw-case-detail-panel">
      <div className="fw-panel-header">
        <strong>{titleByView[detailView]}</strong>
        {detailView === "attribution" ? (
          <div className="fw-panel-header-actions">
            <button
              className="fw-small-secondary"
              type="button"
              onClick={() => attributionJob?.feedback_case_id && onRegenerateAttribution(attributionJob.feedback_case_id)}
              disabled={!canRegenerateAttribution || attributionBusy}
            >
              <RotateCcw size={15} className={attributionBusy ? "fw-spin" : ""} /> 重新归因
            </button>
            <button className="fw-small-secondary" type="button" onClick={() => onSelectDetailView("summary")}>
              <X size={15} /> 关闭
            </button>
          </div>
        ) : detailView === "proposal" ? (
          <div className="fw-panel-header-actions">
            <button
              className="fw-small-secondary"
              type="button"
              onClick={() => proposalJobId && onRevalidateProposalJob(proposalJobId)}
              disabled={!canRevalidateProposal || !proposalJobId || proposalBusy}
            >
              <RefreshCw size={15} className={actionId?.startsWith("proposal-revalidate:") ? "fw-spin" : ""} /> 重新校验
            </button>
            <button
              className="fw-small-secondary"
              type="button"
              onClick={() => proposalJob?.feedback_case_id && onRegenerateProposal(proposalJob.feedback_case_id)}
              disabled={!canRegenerateProposal || proposalBusy}
            >
              <RotateCcw size={15} className={actionId?.startsWith("proposal-regenerate:") ? "fw-spin" : ""} /> 重新生成
            </button>
            <button className="fw-small-secondary" type="button" onClick={copyProposalJobId} disabled={!proposalJobId}>
              <Copy size={15} /> {copiedProposalJobId ? "已复制" : "复制ID"}
            </button>
            <button className="fw-small-secondary" type="button" onClick={() => onSelectDetailView("summary")}>
              <X size={15} /> 关闭
            </button>
          </div>
        ) : detailsLoading ? (
          <Loader2 size={16} className="fw-spin" />
        ) : (
          <FileText size={18} />
        )}
      </div>
      {detailView === "summary" ? <CaseSummaryDetails details={details} /> : null}
      {detailView === "evidence" ? <EvidencePackageDetails clientConfig={clientConfig} packages={details.evidencePackages || []} /> : null}
      {detailView === "attribution" ? (
        <AttributionDetails
          actionId={actionId}
          activeTab={attributionDetailTab}
          jobs={details.attributionJobs || []}
          output={details.attribution}
          onRegenerateAttribution={onRegenerateAttribution}
          onTabChange={onAttributionDetailTabChange}
        />
      ) : null}
      {detailView === "proposal" ? (
        <ProposalDetails
          actionId={actionId}
          jobs={details.proposalJobs || []}
          output={details.proposal}
          proposals={proposals}
          externalGovernanceItems={externalGovernanceItems}
          externalWebhooks={externalWebhooks}
          onCreateTask={onCreateTask}
          onNotifyExternalItem={onNotifyExternalItem}
          onOpenTask={onOpenTask}
          onReviewProposal={onReviewProposal}
          onSelectDetailView={onSelectDetailView}
          tasksByProposalId={tasksByProposalId}
        />
      ) : null}
      {detailView === "runs" ? <RunsDetails runs={runs} /> : null}
      {detailView === "tasks" ? (
        <TasksDetails
          clientConfig={clientConfig}
          tasks={tasks}
          actionId={actionId}
          onMarkApplied={onMarkTaskApplied}
          onCreateExecutionJob={onCreateExecutionJob}
          onApplyExecutionJob={onApplyExecutionJob}
          onRunRegression={onRunTaskRegression}
        />
      ) : null}
      {detailView === "evals" ? <EvalCaseDetails actionId={actionId} evalCases={evalCases} evalRuns={evalRuns} onUpdateEvalCase={onUpdateEvalCase} /> : null}
    </section>
  );
}

function CaseSummaryDetails({ details }: { details: CaseDetails }) {
  return (
    <div className="fw-detail-stack">
      <DetailMetricGrid
        items={[
          ["evidence_package_id", shortId(details.evidence?.evidence_package_id)],
          ["main_agent_version_id", shortId(details.evidence?.main_agent_version_id)],
          ["attribution_status", details.attributionJob?.status || "-"],
          ["proposal_status", details.proposalJob?.status || "-"],
          ["problem_type", details.attribution?.problem_type || "-"],
          ["actionability", details.attribution?.actionability || "-"],
        ]}
      />
      {details.attribution ? (
        <FormattedTextSection title="根因摘要" value={details.attribution.rationale || "暂无归因说明"} compact />
      ) : (
        <div className="fw-empty-inline">暂无已校验归因输出</div>
      )}
    </div>
  );
}

function EvidencePackageDetails({
  clientConfig,
  packages,
}: {
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  packages: EvidencePackageRecord[];
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileRecord, setFileRecord] = useState<EvidencePackageFileRecord | null>(null);
  const [fileLoading, setFileLoading] = useState(false);

  const selectedPackage = useMemo(() => packages.find((item) => item.evidence_package_id === selectedId) || latestItem(packages) || null, [packages, selectedId]);
  const includedFiles = useMemo(() => selectedPackage?.included_files || [], [selectedPackage]);

  useEffect(() => {
    const latestPackage = latestItem(packages);
    setSelectedId((current) => current || latestPackage?.evidence_package_id || null);
  }, [packages]);

  useEffect(() => {
    const nextFile = firstEvidenceFileName(includedFiles);
    setSelectedFile((current) => current || nextFile || null);
  }, [includedFiles]);

  useEffect(() => {
    let cancelled = false;
    async function loadFile() {
      if (!selectedPackage || !selectedFile) {
        setFileRecord(null);
        return;
      }
      setFileLoading(true);
      try {
        const next = await getEvidencePackageFile(clientConfig, selectedPackage.evidence_package_id, selectedFile);
        if (!cancelled) setFileRecord(next);
      } catch {
        if (!cancelled) setFileRecord(null);
      } finally {
        if (!cancelled) setFileLoading(false);
      }
    }
    loadFile();
    return () => {
      cancelled = true;
    };
  }, [clientConfig, selectedPackage, selectedFile]);

  if (!packages.length) {
    return <div className="fw-empty-inline">暂无证据包</div>;
  }

  const hasMultiplePackages = packages.length > 1;

  return (
    <div className={`fw-detail-layout ${hasMultiplePackages ? "" : "fw-detail-layout-single"}`}>
      {hasMultiplePackages ? (
        <div className="fw-detail-list">
          {packages.map((item) => (
            <button
              className={`fw-detail-list-item ${selectedPackage?.evidence_package_id === item.evidence_package_id ? "is-active" : ""}`}
              key={item.evidence_package_id}
              onClick={() => {
                setSelectedId(item.evidence_package_id);
                setSelectedFile(firstEvidenceFileName(item.included_files) || null);
              }}
              type="button"
            >
              <strong>{shortId(item.evidence_package_id)}</strong>
              <small>{formatDate(item.created_at)}</small>
            </button>
          ))}
        </div>
      ) : null}
      <div className="fw-detail-main">
        <DetailMetricGrid
          items={[
            ["evidence_package_id", shortId(selectedPackage?.evidence_package_id)],
            ["main_agent_version_id", shortId(selectedPackage?.main_agent_version_id)],
            ["created_at", formatDate(selectedPackage?.created_at)],
            ["included_files", String(includedFiles.length)],
          ]}
        />
        <CompletenessStrip completeness={selectedPackage?.completeness || {}} />
        <div className="fw-evidence-file-layout">
          <div className="fw-evidence-file-list">
            {includedFiles.map((item) => {
              const fileName = evidenceFileName(item);
              if (!fileName) return null;
              return (
                <button
                  className={selectedFile === fileName ? "is-active" : ""}
                  key={fileName}
                  onClick={() => setSelectedFile(fileName)}
                  type="button"
                >
                  <span>{fileName}</span>
                  <small>{shortId(String(item.sha256 || ""))}</small>
                </button>
              );
            })}
          </div>
          <div className="fw-json-preview">
            <div className="fw-json-preview-header">
              <strong>{selectedFile || "未选择文件"}</strong>
              {fileLoading ? <Loader2 size={14} className="fw-spin" /> : null}
            </div>
            <TraceLinks content={fileRecord?.content} />
            {fileRecord && isEmptyJsonValue(fileRecord.content) ? <div className="fw-json-empty-note">无关联数据</div> : null}
            <pre>{fileRecord ? jsonPreview(fileRecord.content) : "暂无文件内容"}</pre>
          </div>
        </div>
      </div>
    </div>
  );
}

function CompletenessStrip({ completeness }: { completeness: Record<string, unknown> }) {
  const entries = Object.entries(completeness);
  if (!entries.length) return null;
  return (
    <div className="fw-completeness-strip">
      {entries.map(([key, value]) => (
        <span className={value ? "is-complete" : "is-empty"} key={key}>
          {key.replace(/^has_/, "")}
        </span>
      ))}
    </div>
  );
}

function TraceLinks({ content }: { content?: unknown }) {
  const refs = traceRefsFromContent(content);
  if (!refs.length) return null;
  return (
    <div className="fw-trace-links">
      {refs.map((ref) => (
        <a href={ref.url} key={`${ref.traceId}:${ref.url}`} target="_blank" rel="noreferrer">
          Langfuse trace {shortId(ref.traceId)}
        </a>
      ))}
    </div>
  );
}

function DetailMetricGrid({ items }: { items: Array<[string, string | number | null | undefined]> }) {
  return (
    <div className="fw-detail-metric-grid">
      {items.map(([label, value]) => (
        <Metric label={label} value={value} key={label} />
      ))}
    </div>
  );
}

function DetailTabs<T extends string>({
  tabs,
  active,
  onChange,
  label,
}: {
  tabs: Array<DetailTabItem<T>>;
  active: T;
  onChange: (key: T) => void;
  label: string;
}) {
  return (
    <div className="fw-detail-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button className={active === tab.key ? "is-active" : ""} type="button" onClick={() => onChange(tab.key)} key={tab.key}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}

function DetailRecordList({ children, emptyText, hasItems }: { children: ReactNode; emptyText: string; hasItems: boolean }) {
  return (
    <div className="fw-detail-record-list">
      {hasItems ? children : <div className="fw-empty-inline">{emptyText}</div>}
    </div>
  );
}

function AnalysisJobRecordList({ jobs }: { jobs: FeedbackAnalysisJobRecord[] }) {
  return (
    <DetailRecordList hasItems={jobs.length > 0} emptyText="暂无执行记录">
      {jobs.map((job) => (
        <article key={job.job_id}>
          <div className="fw-detail-record-head">
            <h4>{shortId(job.job_id)} · {job.profile_name}</h4>
            <Pill tone={jobStatusTone(job.status)}>{job.status}</Pill>
          </div>
          <p>证据包：{shortId(job.evidence_package_id)}</p>
          <small>创建：{formatDate(job.created_at)} · 开始：{formatDate(job.started_at)} · 完成：{formatDate(job.completed_at)}</small>
          {job.langfuse_trace_id ? <small>Langfuse trace：{shortId(job.langfuse_trace_id)}</small> : null}
          {job.error_json ? (
            <div className="fw-job-error">
              <strong>{job.error_json.error_code || (job.status === "failed" ? "JOB_FAILED" : "JOB_NEEDS_REVIEW")}</strong>
              <FormattedText value={job.error_json.message || "分析 job 执行失败"} />
            </div>
          ) : null}
        </article>
      ))}
    </DetailRecordList>
  );
}

function DetailJsonPreview({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="fw-json-preview fw-json-preview-standalone fw-detail-json-output">
      <div className="fw-json-preview-header">
        <strong>{title}</strong>
      </div>
      <pre>{jsonPreview(value)}</pre>
    </div>
  );
}

type FormattedTextBlock =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; lines: string[] }
  | { type: "ol" | "ul"; items: string[] }
  | { type: "table"; headers: string[]; rows: string[][] };

function FormattedText({
  value,
  className = "",
}: {
  value?: string | number | null;
  className?: string;
}) {
  const text = String(value ?? "").trim();
  const blocks = parseFormattedText(text || "-");
  return (
    <div className={`fw-formatted-text ${className}`.trim()}>
      {blocks.map((block, index) => {
        if (block.type === "heading") {
          const HeadingTag = block.level <= 2 ? "h4" : "h5";
          return <HeadingTag key={`heading:${index}`}>{block.text}</HeadingTag>;
        }
        if (block.type === "paragraph") {
          return <p key={`paragraph:${index}`}>{block.lines.join("\n")}</p>;
        }
        if (block.type === "table") {
          return (
            <div className="fw-formatted-table-wrap" key={`table:${index}`}>
              <table>
                <thead>
                  <tr>
                    {block.headers.map((cell, cellIndex) => (
                      <th key={`table:${index}:head:${cellIndex}`}>{cell || "-"}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {block.rows.map((row, rowIndex) => (
                    <tr key={`table:${index}:row:${rowIndex}`}>
                      {block.headers.map((_, cellIndex) => (
                        <td key={`table:${index}:row:${rowIndex}:cell:${cellIndex}`}>{row[cellIndex] || "-"}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        const ListTag = block.type === "ol" ? "ol" : "ul";
        return (
          <ListTag key={`${block.type}:${index}`}>
            {block.items.map((item, itemIndex) => (
              <li key={`${block.type}:${index}:${itemIndex}`}>{item}</li>
            ))}
          </ListTag>
        );
      })}
    </div>
  );
}

function FormattedTextSection({
  title,
  value,
  compact = false,
}: {
  title: string;
  value?: string | number | null;
  compact?: boolean;
}) {
  return (
    <section className={`fw-text-section ${compact ? "fw-text-section-compact" : ""}`.trim()}>
      <h4>{title}</h4>
      <FormattedText value={value} />
    </section>
  );
}

function FormattedTextFields({
  fields,
}: {
  fields: Array<[string, string | number | null | undefined]>;
}) {
  return (
    <div className="fw-text-field-grid">
      {fields.map(([title, value]) => (
        <FormattedTextSection title={title} value={value ?? "-"} compact key={title} />
      ))}
    </div>
  );
}

function AttributionReviewNotice({
  busy,
  job,
  onOpenDetails,
  onOpenRaw,
  onRegenerate,
}: {
  busy: boolean;
  job: FeedbackAnalysisJobRecord;
  onOpenDetails: () => void;
  onOpenRaw: () => void;
  onRegenerate: () => void;
}) {
  const validationCount = validationErrorCount(job);
  return (
    <div className="fw-review-notice">
      <div className="fw-review-notice-main">
        <AlertTriangle size={18} />
        <div>
          <strong>归因分析需要人工复核</strong>
          <span>{jobErrorCode(job)} · {jobErrorMessage(job, "归因分析智能体输出未通过校验。")}</span>
          {validationCount > 0 ? <small>Schema 校验错误 {validationCount} 项{validationFieldSummary(job)}</small> : null}
        </div>
      </div>
      <div className="fw-review-notice-actions">
        <button className="fw-small-secondary" type="button" onClick={onOpenDetails}>
          <FileText size={15} /> 查看归因详情
        </button>
        <button className="fw-small-secondary" type="button" onClick={onOpenRaw}>
          <Database size={15} /> 查看原始输出
        </button>
        <button className="fw-small-primary" type="button" onClick={onRegenerate} disabled={busy}>
          {busy ? <Loader2 size={15} className="fw-spin" /> : <RotateCcw size={15} />} 重新归因
        </button>
      </div>
    </div>
  );
}

function AttributionReviewCard({
  actionId,
  job,
  onOpenRaw,
  onOpenRecords,
  onRegenerateAttribution,
}: {
  actionId: string | null;
  job: FeedbackAnalysisJobRecord;
  onOpenRaw: () => void;
  onOpenRecords: () => void;
  onRegenerateAttribution: (feedbackCaseId: string) => void;
}) {
  const validationErrors = validationErrorItems(job);
  const busy = actionId?.startsWith("attribution-regenerate:") || false;
  return (
    <article className="fw-review-card">
      <div className="fw-review-card-head">
        <div>
          <h4>未生成可用归因结果</h4>
          <p>归因分析智能体已返回内容，但没有通过 schema 校验，需要查看原始输出后重新归因或调整输出格式化策略。</p>
        </div>
        <Pill tone="orange">{job.status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["job_id", shortId(job.job_id)],
          ["profile", job.profile_name],
          ["evidence_package_id", shortId(job.evidence_package_id)],
          ["completed_at", formatDate(job.completed_at)],
        ]}
      />
      <div className="fw-job-error">
        <strong>{jobErrorCode(job)}</strong>
        <FormattedText value={jobErrorMessage(job, "归因分析智能体输出未通过校验。")} />
      </div>
      {validationErrors.length ? (
        <div className="fw-review-validation-list">
          <strong>Schema 校验错误 {validationErrors.length} 项</strong>
          <ul>
            {validationErrors.slice(0, 6).map((item, index) => (
              <li key={`${validationErrorPath(item)}:${index}`}>{validationErrorPath(item)}：{validationErrorMessage(item)}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="fw-detail-action-row">
        <button className="fw-small-secondary" type="button" onClick={onOpenRaw}>
          <Database size={16} /> 查看原始输出
        </button>
        <button className="fw-small-secondary" type="button" onClick={onOpenRecords}>
          <Archive size={16} /> 查看执行记录
        </button>
        <button className="fw-small-primary" type="button" onClick={() => onRegenerateAttribution(job.feedback_case_id)} disabled={busy}>
          {busy ? <Loader2 size={16} className="fw-spin" /> : <RotateCcw size={16} />} 重新归因
        </button>
      </div>
    </article>
  );
}

function AttributionDetails({
  actionId,
  activeTab,
  jobs,
  output,
  onRegenerateAttribution,
  onTabChange,
}: {
  actionId: string | null;
  activeTab: AttributionDetailTab;
  jobs: FeedbackAnalysisJobRecord[];
  output?: AttributionOutput | null;
  onRegenerateAttribution: (feedbackCaseId: string) => void;
  onTabChange: (tab: AttributionDetailTab) => void;
}) {
  const latestJob = latestItem(jobs);
  const rawOutput = output || latestJob?.raw_output_json || latestJob?.error_json || null;
  const rawOutputTitle = output ? "归因输出" : latestJob?.raw_output_json ? "归因分析智能体原始输出" : "归因校验错误";

  return (
    <div className="fw-detail-tabbed">
      <DetailTabs
        active={activeTab}
        label="归因分析详情视图"
        onChange={onTabChange}
        tabs={[
          { key: "result", label: "分析结果" },
          { key: "raw", label: "原始输出" },
          { key: "records", label: "执行记录" },
        ]}
      />
      <div className="fw-detail-tab-body">
        {activeTab === "result" ? (
          output ? (
            <AttributionResult output={output} />
          ) : latestJob?.status === "needs_human_review" ? (
            <AttributionReviewCard
              actionId={actionId}
              job={latestJob}
              onOpenRaw={() => onTabChange("raw")}
              onOpenRecords={() => onTabChange("records")}
              onRegenerateAttribution={onRegenerateAttribution}
            />
          ) : (
            <div className="fw-empty-inline">暂无已校验归因输出</div>
          )
        ) : null}
        {activeTab === "raw" ? (rawOutput ? <DetailJsonPreview title={rawOutputTitle} value={rawOutput} /> : <div className="fw-empty-inline">暂无原始输出</div>) : null}
        {activeTab === "records" ? <AnalysisJobRecordList jobs={jobs} /> : null}
      </div>
    </div>
  );
}

function AttributionResult({ output }: { output: AttributionOutput }) {
  return (
    <div className="fw-detail-result fw-attribution-result">
      <section className="fw-detail-result-summary">
        <DetailMetricGrid
          items={[
            ["status", output.status],
            ["problem_type", output.problem_type],
            ["optimization_object_type", output.optimization_object_type],
            ["actionability", output.actionability],
            ["confidence", output.confidence],
            ["recommended_next_step", output.recommended_next_step],
          ]}
        />
      </section>
      <FormattedTextSection title="根因说明" value={output.rationale || "暂无归因说明"} />
      <section className="fw-text-section fw-attribution-boundary">
        <h4>责任边界</h4>
        <div className="fw-attribution-owner">
          <small>owner</small>
          <strong>{output.responsibility_boundary?.owner || "-"}</strong>
        </div>
        <FormattedText value={output.responsibility_boundary?.reason || "-"} />
      </section>
      <section className="fw-text-section fw-attribution-evidence">
        <h4>引用证据</h4>
        {output.evidence_refs?.length ? (
          <div className="fw-attribution-evidence-list">
            {output.evidence_refs.map((ref, index) => (
              <article key={`${ref.type}:${ref.id}:${index}`}>
                <div>
                  <Pill tone="gray">{ref.type || "evidence"}</Pill>
                  <strong>{ref.id || "-"}</strong>
                </div>
                <FormattedText value={ref.reason || "-"} />
              </article>
            ))}
          </div>
        ) : (
          <FormattedText value="暂无引用证据" />
        )}
      </section>
    </div>
  );
}

function ProposalDetails({
  jobs,
  output,
  proposals,
  externalGovernanceItems,
  externalWebhooks,
  actionId,
  onReviewProposal,
  onCreateTask,
  onNotifyExternalItem,
  onOpenTask,
  onSelectDetailView,
  tasksByProposalId,
}: {
  jobs: FeedbackAnalysisJobRecord[];
  output?: ProposalOutput | null;
  proposals: OptimizationProposalRecord[];
  externalGovernanceItems: ExternalGovernanceItemRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  onSelectDetailView: (view: CaseDetailView) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const [activeTab, setActiveTab] = useState<ProposalDetailTab>("proposals");
  const latestJob = latestItem(jobs);
  const externalGuidance = output?.external_guidance || [];
  const proposalCount = proposals.length + externalGuidance.length;
  const rawProposals = rawRecordArray(latestJob?.raw_output_json, "proposals");
  const rawExternalGuidance = rawRecordArray(latestJob?.raw_output_json, "external_guidance");
  const rawSuggestionCount = rawProposals.length + rawExternalGuidance.length;
  const noActionReason = output?.no_action_reason || rawString(latestJob?.raw_output_json, "no_action_reason");
  const hasUnvalidatedSuggestions = !proposalCount && Boolean(latestJob?.error_json) && rawSuggestionCount > 0;
  const rawOutput = output || latestJob?.raw_output_json || latestJob?.error_json || null;
  const rawOutputTitle = output ? "方案输出" : latestJob?.raw_output_json ? "优化方案生成智能体原始输出" : "方案校验错误";
  const regenerationInstruction = rawString(latestJob?.input_json, "regeneration_instruction");

  return (
    <div className="fw-proposal-detail">
      <div className="fw-proposal-detail-meta">
        <div className="fw-proposal-detail-meta-main">
          <h4>{shortId(latestJob?.job_id || output?.proposal_job_id)} · {profileDisplayName(latestJob?.profile_name || "proposal-generator")}</h4>
          <Pill tone={jobStatusTone(latestJob?.status || output?.status)}>{latestJob?.status || output?.status || "-"}</Pill>
        </div>
        <div className="fw-proposal-detail-meta-line">
          <span>证据包 {shortId(latestJob?.evidence_package_id)}</span>
          <button className="fw-link-button" type="button" onClick={() => onSelectDetailView("evidence")} disabled={!latestJob?.evidence_package_id}>
            查看证据包
          </button>
          <span>创建 {formatDate(latestJob?.created_at)}</span>
          <span>完成 {formatDate(latestJob?.completed_at)}</span>
        </div>
        {regenerationInstruction ? (
          <div className="fw-proposal-regeneration-instruction">
            <span>补充指令</span>
            <FormattedText value={regenerationInstruction} />
          </div>
        ) : null}
      </div>

      <DetailTabs
        active={activeTab}
        label="优化方案详情视图"
        onChange={setActiveTab}
        tabs={[
          { key: "proposals", label: `建议(${proposalCount || rawSuggestionCount})` },
          { key: "raw", label: "原始输出" },
          { key: "records", label: "执行记录" },
        ]}
      />

      <div className="fw-detail-tab-body">
        {activeTab === "proposals" ? (
          <div className="fw-proposal-detail-list">
            {proposals.map((proposal) => (
              <ProposalDetailCard
                actionId={actionId}
                key={proposal.proposal_id}
                proposal={proposal}
                task={tasksByProposalId.get(proposal.proposal_id)}
                onCreateTask={onCreateTask}
                onOpenTask={onOpenTask}
                onReviewProposal={onReviewProposal}
              />
            ))}
            {externalGuidance.map((item, index) => (
              <ExternalGuidanceCard
                actionId={actionId}
                guidance={item}
                item={findExternalGovernanceItem(externalGovernanceItems, item, output?.proposal_job_id, index)}
                key={`${item.owner}:${index}`}
                webhooks={externalWebhooks}
                onNotifyExternalItem={onNotifyExternalItem}
              />
            ))}
            {hasUnvalidatedSuggestions ? (
              <>
                <div className="fw-job-error fw-proposal-validation-error">
                  <strong>建议校验失败</strong>
                  <span>
                    Agent 原始输出包含 {rawSuggestionCount} 条未入库建议，但未通过 schema 校验；以下内容仅供排查，不能审批或创建优化任务。
                  </span>
                </div>
                {rawProposals.map((item, index) => (
                  <RawProposalCard item={item} key={`raw-proposal:${index}`} />
                ))}
                {rawExternalGuidance.map((item, index) => (
                  <RawExternalGuidanceCard item={item} key={`raw-external:${index}`} />
                ))}
              </>
            ) : null}
            {!proposalCount && noActionReason ? (
              <div className="fw-empty-inline fw-empty-inline-formatted">
                <strong>无可执行建议</strong>
                <FormattedText value={noActionReason} />
              </div>
            ) : null}
            {!proposalCount && !hasUnvalidatedSuggestions && !noActionReason ? <div className="fw-empty-inline">暂无优化方案</div> : null}
          </div>
        ) : null}

        {activeTab === "raw" ? (
          rawOutput ? (
            <DetailJsonPreview title={rawOutputTitle} value={rawOutput} />
          ) : (
            <div className="fw-empty-inline">暂无原始输出</div>
          )
        ) : null}

        {activeTab === "records" ? <AnalysisJobRecordList jobs={jobs} /> : null}
      </div>
    </div>
  );
}

function ProposalDetailCard({
  proposal,
  task,
  actionId,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
}: {
  proposal: OptimizationProposalRecord;
  task?: OptimizationTaskRecord;
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
}) {
  const approved = proposal.status === "approved";
  const pending = proposal.status === "pending_review";
  return (
    <article className="fw-proposal-card fw-proposal-detail-card">
      <div className="fw-proposal-detail-title">
        <Pill tone={proposalStatusTone(proposal.status)}>{proposalStatusText[proposal.status] || proposal.status}</Pill>
        <h4>{proposal.title}</h4>
        <small>{shortId(proposal.proposal_id)} · {proposal.target_type} · {proposal.target_path || "-"}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={proposal.recommendation} />
      <div className="fw-proposal-detail-evidence">
        <span>引用证据：</span>
        <strong>{proposalEvidenceText(proposal)}</strong>
      </div>
      <div className="fw-detail-action-row">
        {pending ? (
          <>
            <button className="fw-small-primary" type="button" disabled={actionId === `approve:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "approve")}>
              <CheckCircle2 size={16} /> 批准
            </button>
            <button className="fw-danger-button" type="button" disabled={actionId === `reject:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "reject")}>
              <XCircle size={16} /> 拒绝
            </button>
            <button className="fw-small-secondary" type="button" disabled={actionId === `request_more_analysis:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "request_more_analysis")}>
              <AlertTriangle size={16} /> 要求补充分析
            </button>
          </>
        ) : null}
        {approved ? (
          <button
            className={task ? "fw-small-secondary" : "fw-small-primary"}
            type="button"
            disabled={!task && actionId === `task:${proposal.proposal_id}`}
            onClick={() => (task ? onOpenTask(task) : onCreateTask(proposal))}
          >
            {!task && actionId === `task:${proposal.proposal_id}` ? <Loader2 size={16} className="fw-spin" /> : <ChevronRight size={16} />}
            {task ? "查看优化任务" : "创建优化任务"}
          </button>
        ) : null}
      </div>
    </article>
  );
}

function RawProposalCard({ item }: { item: Record<string, unknown> }) {
  const title = rawString(item, "title") || rawString(item, "recommendation") || "未入库优化方案";
  const recommendation = rawString(item, "recommendation") || "-";
  const rationale = rawString(item, "rationale") || rawString(item, "reason");
  return (
    <article className="fw-proposal-card fw-proposal-detail-card fw-unvalidated-proposal-card">
      <div className="fw-proposal-detail-title">
        <Pill tone="orange">未入库</Pill>
        <h4>{title}</h4>
        <small>{rawString(item, "proposal_id") || rawString(item, "id") || "raw-proposal"} · {rawString(item, "actionability") || "-"} · {rawString(item, "target_path") || "-"}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={recommendation} />
      {rationale ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={rationale} /> : null}
    </article>
  );
}

function RawExternalGuidanceCard({ item }: { item: Record<string, unknown> }) {
  const owner = rawString(item, "owner") || rawString(item, "target") || "外部系统";
  const reason = rawString(item, "reason") || rawString(item, "rationale");
  return (
    <article className="fw-proposal-card fw-proposal-detail-card fw-external-guidance-card fw-unvalidated-proposal-card">
      <div className="fw-proposal-detail-title">
        <Pill tone="orange">未入库外部建议</Pill>
        <h4>{owner}</h4>
        <small>{rawString(item, "actionability") || "external_guidance"}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={rawString(item, "recommendation") || "-"} />
      {reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={reason} /> : null}
    </article>
  );
}

function RunsDetails({ runs }: { runs: FeedbackRunRecord[] }) {
  return (
    <DetailRecordList hasItems={runs.length > 0} emptyText="暂无关联运行">
      {runs.map((run) => (
        <article className="fw-run-card" key={run.run_id}>
          <div className="fw-detail-record-head">
            <h4>{shortId(run.run_id)} · {shortId(run.agent_version_id)}</h4>
            <Pill tone="blue">run</Pill>
          </div>
          <DetailMetricGrid
            items={[
              ["session", shortId(run.session_id)],
              ["agent_version", shortId(run.agent_version_id)],
              ["created", formatDate(run.created_at)],
              ["completed", formatDate(run.completed_at)],
              ["stop_reason", run.stop_reason || "-"],
              ["cost", run.total_cost_usd != null ? `$${run.total_cost_usd.toFixed(6)}` : "-"],
            ]}
          />
          <section className="fw-run-section">
            <h4>回答摘要</h4>
            <FormattedText className="fw-record-long-text" value={run.answer_summary || run.message || "-"} />
          </section>
          <section className="fw-run-section">
            <h4>工具调用</h4>
            <RunToolList tools={run.agent_activity?.tool_names || []} />
          </section>
          {run.errors?.length ? (
            <section className="fw-run-section">
              <h4>错误</h4>
              <FormattedText className="fw-warning-text" value={run.errors.join("\n")} />
            </section>
          ) : null}
        </article>
      ))}
    </DetailRecordList>
  );
}

function RunToolList({ tools }: { tools: string[] }) {
  if (!tools.length) return <div className="fw-empty-inline fw-run-empty-inline">暂无工具调用记录</div>;
  return (
    <div className="fw-run-tool-list">
      {tools.map((tool, index) => (
        <span className="fw-run-tool-pill" title={tool} key={`${tool}:${index}`}>
          {tool}
        </span>
      ))}
    </div>
  );
}

function TasksDetails({
  clientConfig,
  tasks,
  actionId,
  onMarkApplied,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onRunRegression,
}: {
  clientConfig?: ExternalFeedbackWorkspaceProps["clientConfig"];
  tasks: OptimizationTaskRecord[];
  actionId?: string | null;
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onCreateExecutionJob?: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob?: (task: OptimizationTaskRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  return (
    <DetailRecordList hasItems={tasks.length > 0} emptyText="暂无优化任务">
      {tasks.map((task) => (
        <TaskDetailCard
          key={task.optimization_task_id}
          clientConfig={clientConfig}
          task={task}
          actionId={actionId || null}
          onMarkApplied={onMarkApplied}
          onCreateExecutionJob={onCreateExecutionJob}
          onApplyExecutionJob={onApplyExecutionJob}
          onRunRegression={onRunRegression}
        />
      ))}
    </DetailRecordList>
  );
}

function EvalCaseDetails({
  actionId,
  evalCases,
  evalRuns,
  onUpdateEvalCase,
}: {
  actionId: string | null;
  evalCases: EvalCaseRecord[];
  evalRuns: EvalRunRecord[];
  onUpdateEvalCase: (evalCaseId: string, payload: EvalCaseUpdateRequest) => Promise<boolean>;
}) {
  return (
    <DetailRecordList hasItems={evalCases.length > 0} emptyText="暂无评估用例">
      {evalCases.map((evalCase) => (
        <EvalCaseDetailCard
          actionId={actionId}
          key={evalCase.eval_case_id}
          evalCase={evalCase}
          latestRunItem={latestEvalRunItemForCase(evalRuns, evalCase.eval_case_id)}
          onUpdateEvalCase={onUpdateEvalCase}
        />
      ))}
    </DetailRecordList>
  );
}

function EvalCaseDetailCard({
  actionId,
  evalCase,
  latestRunItem,
  onUpdateEvalCase,
}: {
  actionId: string | null;
  evalCase: EvalCaseRecord;
  latestRunItem?: NonNullable<EvalRunRecord["items"]>[number];
  onUpdateEvalCase: (evalCaseId: string, payload: EvalCaseUpdateRequest) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<EvalCaseEditDraft>(() => evalCaseEditDraft(evalCase));
  const [formError, setFormError] = useState<string | null>(null);
  const busy = actionId === `eval-case:${evalCase.eval_case_id}`;
  const archived = evalCase.status === "archived";

  useEffect(() => {
    if (!editing) {
      setDraft(evalCaseEditDraft(evalCase));
      setFormError(null);
    }
  }, [editing, evalCase]);

  async function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = draft.prompt.trim();
    if (!prompt) {
      setFormError("Prompt 不能为空。");
      return;
    }
    let checksJson: Record<string, unknown>;
    try {
      const parsed = JSON.parse(draft.checksText || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setFormError("校验规则必须是 JSON object。");
        return;
      }
      checksJson = parsed as Record<string, unknown>;
    } catch (error) {
      setFormError(error instanceof Error ? `校验规则 JSON 无效：${error.message}` : "校验规则 JSON 无效。");
      return;
    }
    setFormError(null);
    const ok = await onUpdateEvalCase(evalCase.eval_case_id, {
      prompt,
      expected_behavior: draft.expectedBehavior.trim(),
      checks_json: checksJson,
      labels: parseEvalCaseLabels(draft.labelsText),
      status: draft.status,
    });
    if (ok) setEditing(false);
  }

  async function toggleArchived() {
    const nextStatus = archived ? "active" : "archived";
    await onUpdateEvalCase(evalCase.eval_case_id, { status: nextStatus });
  }

  return (
    <article className="fw-eval-card fw-eval-detail-card">
      <div className="fw-detail-record-head">
        <div>
          <h4>{shortId(evalCase.eval_case_id)} · feedback-eval-case</h4>
          <small>反馈单 {shortId(evalCase.source_feedback_case_id)} · 来源运行 {shortId(evalCase.source_run_id)}</small>
        </div>
        <div className="fw-eval-card-actions">
          <Pill tone={evalCase.status === "active" ? "green" : "gray"}>{evalCase.status}</Pill>
          <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => setEditing((current) => !current)}>
            <Pencil size={15} /> {editing ? "取消编辑" : "编辑"}
          </button>
          <button className="fw-small-secondary" type="button" disabled={busy} onClick={toggleArchived}>
            {busy ? <Loader2 size={15} className="fw-spin" /> : archived ? <CheckCircle2 size={15} /> : <Archive size={15} />}
            {archived ? "启用" : "归档"}
          </button>
        </div>
      </div>
      <DetailMetricGrid
        items={[
          ["创建时间", formatDate(evalCase.created_at)],
          ["更新时间", formatDate(evalCase.updated_at)],
          ["标签", evalCase.labels?.join(", ") || "-"],
          ["最近结果", latestRunItem?.status || "-"],
          ["最近得分", latestRunItem?.score ?? "-"],
          ["最近运行", shortId(latestRunItem?.eval_run_id)],
        ]}
      />
      {editing ? (
        <form className="fw-eval-edit-form" onSubmit={submitEdit}>
          <label className="fw-eval-edit-field">
            <span>Prompt</span>
            <textarea value={draft.prompt} onChange={(event) => setDraft((current) => ({ ...current, prompt: event.target.value }))} />
          </label>
          <label className="fw-eval-edit-field">
            <span>期望行为</span>
            <textarea value={draft.expectedBehavior} onChange={(event) => setDraft((current) => ({ ...current, expectedBehavior: event.target.value }))} />
          </label>
          <div className="fw-eval-edit-grid">
            <label className="fw-eval-edit-field">
              <span>状态</span>
              <select value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as EvalCaseEditDraft["status"] }))}>
                <option value="active">active</option>
                <option value="draft">draft</option>
                <option value="archived">archived</option>
              </select>
            </label>
            <label className="fw-eval-edit-field">
              <span>标签</span>
              <input value={draft.labelsText} onChange={(event) => setDraft((current) => ({ ...current, labelsText: event.target.value }))} placeholder="逗号或换行分隔" />
            </label>
          </div>
          <label className="fw-eval-edit-field">
            <span>校验规则 JSON</span>
            <textarea className="fw-eval-json-editor" value={draft.checksText} onChange={(event) => setDraft((current) => ({ ...current, checksText: event.target.value }))} />
          </label>
          {formError ? <p className="fw-warning-text">{formError}</p> : null}
          <div className="fw-detail-action-row">
            <button className="fw-small-secondary" type="button" disabled={busy} onClick={() => setEditing(false)}>
              <X size={15} /> 取消
            </button>
            <button className="fw-small-primary" type="submit" disabled={busy}>
              {busy ? <Loader2 size={15} className="fw-spin" /> : <CheckCircle2 size={15} />} 保存
            </button>
          </div>
        </form>
      ) : (
        <>
          <section className="fw-task-source">
            <h4>Prompt</h4>
            <FormattedText value={evalCase.prompt || "-"} />
          </section>
          <section className="fw-task-source">
            <h4>期望行为</h4>
            <FormattedText value={evalCase.expected_behavior || "-"} />
          </section>
          <DetailJsonPreview title="校验规则" value={evalCase.checks_json || {}} />
        </>
      )}
      {latestRunItem ? (
        <section className="fw-task-source">
          <h4>最近评估结果</h4>
          <FormattedText value={evalItemSummary(latestRunItem)} />
          {latestRunItem.check_results?.length ? <DetailJsonPreview title="检查结果" value={latestRunItem.check_results} /> : null}
        </section>
      ) : null}
    </article>
  );
}

function ExecutionApplyConfirmModal({
  busy,
  onCancel,
  onConfirm,
  task,
}: {
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  task: OptimizationTaskRecord;
}) {
  const execution = task.latest_execution_job || null;
  const plan = execution?.validated_output_json || null;
  const operations = plan?.operations || [];
  const targetPaths = Array.from(
    new Set([
      ...((task.target_paths || []) as string[]),
      ...operations.map((operation) => operation.path || "").filter(Boolean),
    ]),
  );
  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <div
        className="modal-card fw-execution-apply-modal"
        role="dialog"
        aria-modal="true"
        aria-label="应用执行方案确认"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div>
            <h3>应用执行方案</h3>
            <p>确认后会修改主智能体受管配置，并创建执行前、执行后两个版本快照。</p>
          </div>
          <button className="mini-icon-button" type="button" onClick={onCancel} aria-label="关闭" disabled={busy}>
            <X size={16} />
          </button>
        </header>
        <div className="fw-execution-apply-body">
          <DetailMetricGrid
            items={[
              ["优化任务", shortId(task.optimization_task_id)],
              ["执行方案", shortId(execution?.execution_job_id)],
              ["状态", execution?.status || "-"],
              ["基线版本", shortId(execution?.baseline_agent_version_id || task.baseline_agent_version_id)],
              ["操作数", operations.length],
            ]}
          />
          {plan?.summary ? (
            <section className="fw-execution-apply-section">
              <h4>方案摘要</h4>
              <FormattedText value={plan.summary} />
            </section>
          ) : null}
          <section className="fw-execution-apply-section">
            <h4>目标文件</h4>
            <div className="fw-execution-apply-targets">
              {targetPaths.length ? targetPaths.map((path) => <span key={path}>{path}</span>) : <span>-</span>}
            </div>
          </section>
          <section className="fw-execution-apply-section">
            <h4>计划操作</h4>
            {operations.length ? (
              <div className="fw-execution-apply-list">
                {operations.map((operation, index) => (
                  <article className="fw-execution-apply-operation" key={`${operation.path || "operation"}:${index}`}>
                    <div>
                      <strong>{operation.operation || "operation"}</strong>
                      <code>{operation.path || "-"}</code>
                    </div>
                    {operation.rationale ? <FormattedText value={operation.rationale} /> : null}
                  </article>
                ))}
              </div>
            ) : (
              <p className="fw-note-box">当前执行方案没有可应用操作。</p>
            )}
          </section>
          <p className="fw-modal-warning">
            应用前系统会检查当前版本是否仍等于执行方案基线；如已发生变更，将拒绝应用并要求重新生成执行方案。
          </p>
        </div>
        <div className="modal-actions">
          <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="fw-small-primary" type="button" onClick={onConfirm} disabled={busy || !operations.length}>
            {busy ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
            确认应用
          </button>
        </div>
      </div>
    </div>
  );
}

function ManualApplyConfirmModal({
  busy,
  currentVersion,
  onCancel,
  onConfirm,
  task,
}: {
  busy: boolean;
  currentVersion?: ExternalFeedbackWorkspaceProps["currentAgentVersion"];
  onCancel: () => void;
  onConfirm: () => void;
  task: OptimizationTaskRecord;
}) {
  const targetPaths = task.target_paths || [];
  return (
    <div className="modal-backdrop" role="presentation" onClick={() => !busy && onCancel()}>
      <div
        className="modal-card fw-execution-apply-modal fw-manual-apply-modal"
        role="dialog"
        aria-modal="true"
        aria-label="人工已应用确认"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <div>
            <h3>人工已应用，创建快照</h3>
            <p>仅当你已在外部或手工完成优化修改时使用；此操作不会执行任何优化方案。</p>
          </div>
          <button className="mini-icon-button" type="button" onClick={onCancel} aria-label="关闭" disabled={busy}>
            <X size={16} />
          </button>
        </header>
        <div className="fw-execution-apply-body">
          <DetailMetricGrid
            items={[
              ["优化任务", shortId(task.optimization_task_id)],
              ["任务状态", task.status],
              ["当前版本", shortId(currentVersion?.agent_version_id)],
              ["基线版本", shortId(task.baseline_agent_version_id)],
              ["目标文件数", targetPaths.length],
            ]}
          />
          <section className="fw-execution-apply-section">
            <h4>目标文件</h4>
            <div className="fw-execution-apply-targets">
              {targetPaths.length ? targetPaths.map((path) => <span key={path}>{path}</span>) : <span>-</span>}
            </div>
          </section>
          <p className="fw-modal-warning">
            确认后系统只会对当前主智能体受管配置创建版本快照，并把任务推进到回归验证阶段。它不会写入文件，也不会应用执行优化智能体的计划操作。
          </p>
        </div>
        <div className="modal-actions">
          <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className="fw-small-primary" type="button" onClick={onConfirm} disabled={busy}>
            {busy ? <Loader2 size={16} className="fw-spin" /> : <GitBranch size={16} />}
            确认创建快照
          </button>
        </div>
      </div>
    </div>
  );
}

function TaskDetailCard({
  clientConfig,
  task,
  actionId,
  onMarkApplied,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onRunRegression,
}: {
  clientConfig?: ExternalFeedbackWorkspaceProps["clientConfig"];
  task: OptimizationTaskRecord;
  actionId?: string | null;
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onCreateExecutionJob?: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob?: (task: OptimizationTaskRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  const proposal = task.proposal;
  const proposalId = taskProposalId(task);
  const targetPaths = task.target_paths || [];
  const latestRegression = task.latest_regression_run || null;
  const latestExecution = task.latest_execution_job || null;
  const diffFromVersion = task.pre_execution_agent_version_id || rawString(task.applied_agent_version, "parent_version_id");
  const diffToVersion = task.applied_agent_version_id || "";
  const canManualMarkApplied = !task.applied_agent_version_id && ["pending_execution", "failed", "needs_human_review"].includes(task.status);
  const canCreateExecution = !task.applied_agent_version_id && ["pending_execution", "execution_failed", "execution_ready", "failed", "needs_human_review"].includes(task.status);
  const canApplyExecution = !task.applied_agent_version_id && latestExecution?.status === "ready";
  const canRunRegression = Boolean(task.applied_agent_version_id) && task.status !== "regression_running";
  const showManualFallback = Boolean(onMarkApplied && canManualMarkApplied);
  const regressionButtonLabel = latestRegression ? "重新运行回归验证" : "运行回归验证";
  return (
    <article className="fw-task-detail-card">
      <div className="fw-detail-record-head">
        <div>
          <h4>{shortId(task.optimization_task_id)} · optimization-task</h4>
          <small>反馈单 {shortId(task.feedback_case_id)} · 建议 {shortId(proposalId)}</small>
        </div>
        <Pill tone={jobStatusTone(task.status)}>{task.status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["执行模式", task.execution_mode],
          ["来源", task.source],
          ["创建时间", formatDate(task.created_at)],
          ["基线版本", shortId(task.baseline_agent_version_id)],
          ["目标文件数", targetPaths.length],
          ["应用版本", shortId(task.applied_agent_version_id)],
          ["最近回归", latestRegression?.result_status || "-"],
        ]}
      />
      <div className="fw-task-targets">
        <strong>目标文件</strong>
        <div>
          {targetPaths.length ? targetPaths.map((path) => <span key={path}>{path}</span>) : <span>-</span>}
        </div>
      </div>
      {proposal ? (
        <section className="fw-task-source">
          <h4>{proposal.title || "来源优化方案"}</h4>
          <FormattedText value={proposal.recommendation || "-"} />
          <DetailMetricGrid items={[["审批状态", proposalStatusText[proposal.status] || proposal.status]]} />
          <FormattedTextFields
            fields={[
              ["预期效果", proposal.expected_effect || "-"],
              ["验证方式", proposal.validation || "-"],
              ["风险", proposal.risk || "-"],
            ]}
          />
        </section>
      ) : null}
      {latestExecution ? (
        <TaskExecutionPlanSection task={task} execution={latestExecution} />
      ) : null}
      <TaskRegressionSection task={task} latestRegression={latestRegression} canRunRegression={canRunRegression} />
      <TaskVersionDiffSection clientConfig={clientConfig} task={task} targetPaths={targetPaths} fromVersionId={diffFromVersion} toVersionId={diffToVersion} />
      <p className="fw-note-box fw-task-status-note">{taskStatusDescription(task.status)}</p>
      {onCreateExecutionJob || onApplyExecutionJob || onRunRegression ? (
        <div className="fw-detail-action-row">
          {onCreateExecutionJob ? (
            <button
              className="fw-small-secondary"
              type="button"
              disabled={!canCreateExecution || actionId === `execution:${task.optimization_task_id}`}
              onClick={() => onCreateExecutionJob(task, Boolean(latestExecution))}
            >
              {actionId === `execution:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <ShieldCheck size={16} />}
              {latestExecution ? "重新生成执行方案" : "生成执行方案"}
            </button>
          ) : null}
          {onApplyExecutionJob ? (
            <button
              className="fw-small-primary"
              type="button"
              disabled={!canApplyExecution || actionId === `execution-apply:${task.optimization_task_id}`}
              onClick={() => onApplyExecutionJob(task)}
            >
              {actionId === `execution-apply:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
              应用执行方案
            </button>
          ) : null}
          {onRunRegression ? (
            <button
              className="fw-small-primary"
              type="button"
              disabled={!canRunRegression || actionId === `regression:${task.optimization_task_id}`}
              onClick={() => onRunRegression(task)}
            >
              {actionId === `regression:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <PlayCircle size={16} />}
              {regressionButtonLabel}
            </button>
          ) : null}
        </div>
      ) : null}
      {showManualFallback ? (
        <details className="fw-manual-fallback">
          <summary>兜底操作</summary>
          <div className="fw-manual-fallback-body">
            <p>仅当你已在外部或手工完成优化修改时使用。该操作不会应用执行方案，只会为当前主智能体配置创建快照。</p>
            <button
              className="fw-small-secondary"
              type="button"
              disabled={actionId === `apply:${task.optimization_task_id}`}
              onClick={() => onMarkApplied?.(task)}
            >
              {actionId === `apply:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <GitBranch size={16} />}
              人工已应用，创建快照
            </button>
          </div>
        </details>
      ) : null}
    </article>
  );
}

function TaskExecutionPlanSection({ task, execution }: { task: OptimizationTaskRecord; execution: OptimizationExecutionJobRecord }) {
  const output = execution.validated_output_json;
  const operations = output?.operations || [];
  const createsEvalCase = isEvalCaseExecutionPlan(task, execution);
  const title = createsEvalCase ? "执行方案：创建评估用例文件" : "执行方案";
  return (
    <section className={`fw-task-source fw-task-execution-section ${createsEvalCase ? "fw-task-execution-section-eval" : ""}`.trim()}>
      <div className="fw-task-section-head">
        <h4>{title}</h4>
        <small>{createsEvalCase ? "这里展示的是待写入文件内容，不是回归验证结果。" : "这里展示将要修改什么文件以及如何修改。"}</small>
      </div>
      <DetailMetricGrid
        items={[
          ["execution_job", shortId(execution.execution_job_id)],
          ["状态", execution.status],
          ["基线版本", shortId(execution.baseline_agent_version_id)],
          ["操作数", operations.length],
        ]}
      />
      {output?.summary ? <FormattedText value={output.summary} /> : null}
      {output?.validation || output?.risk || output?.no_action_reason ? (
        <FormattedTextFields
          fields={[
            ["应用前检查", output.validation || "-"],
            ["风险", output.risk || "-"],
            ["未执行原因", output.no_action_reason || "-"],
          ]}
        />
      ) : null}
      {execution.error_json?.message ? <FormattedText className="fw-warning-text" value={String(execution.error_json.message)} /> : null}
      {operations.length ? (
        <div className="fw-execution-operation-list">
          {operations.map((operation, index) => (
            <ExecutionOperationCard createsEvalCase={createsEvalCase} operation={operation} key={`${operation.path || "operation"}:${index}`} />
          ))}
        </div>
      ) : (
        <p className="fw-note-box">当前执行方案没有可应用操作。</p>
      )}
    </section>
  );
}

function ExecutionOperationCard({ createsEvalCase, operation }: { createsEvalCase: boolean; operation: ExecutionPlanOperation }) {
  const content = operation.content || operation.append_text || "";
  return (
    <div className="fw-execution-operation">
      <span>{operation.operation || "operation"}</span>
      <code>{operation.path || "-"}</code>
      {operation.rationale ? <small>{operation.rationale}</small> : null}
      {createsEvalCase && content ? (
        <details className="fw-execution-operation-content">
          <summary>查看将创建的评估用例草案</summary>
          <p>这是执行方案准备写入的 JSON 文件内容；真正的回归验证结果会在“回归验证”区域展示。</p>
          <pre>{content}</pre>
        </details>
      ) : null}
    </div>
  );
}

function TaskRegressionSection({
  task,
  latestRegression,
  canRunRegression,
}: {
  task: OptimizationTaskRecord;
  latestRegression: EvalRunRecord | null;
  canRunRegression: boolean;
}) {
  const evalCaseCount = latestRegression?.eval_case_ids?.length || latestRegression?.summary?.total || 0;
  const statusText = latestRegression ? latestRegression.result_status || latestRegression.status : "尚未运行";
  return (
    <section className="fw-task-source fw-task-regression-section">
      <div className="fw-task-section-head">
        <h4>回归验证</h4>
        <small>这里展示应用优化后的评估运行结果，不展示执行方案 JSON。</small>
      </div>
      <DetailMetricGrid
        items={
          latestRegression
            ? [
                ["eval_run", shortId(latestRegression.eval_run_id)],
                ["结果", statusText],
                ["用例数", evalCaseCount],
                ["通过", latestRegression.summary?.passed ?? 0],
                ["失败", latestRegression.summary?.failed ?? 0],
                ["需复核", latestRegression.summary?.needs_human_review ?? 0],
                ["完成时间", formatDate(latestRegression.completed_at)],
              ]
            : [
                ["状态", statusText],
                ["应用版本", shortId(task.applied_agent_version_id)],
                ["任务状态", task.status],
              ]
        }
      />
      <p className="fw-note-box">
        {latestRegression
          ? "最近一次回归验证已完成；如执行方案或评估用例发生变化，可重新运行。"
          : canRunRegression
            ? "任务已应用，可以手动运行回归验证，使用当前启用的反馈评估用例集。"
            : "任务尚未应用，需先应用执行方案或人工标记已应用后再运行回归验证。"}
      </p>
    </section>
  );
}

function TaskVersionDiffSection({
  clientConfig,
  task,
  targetPaths,
  fromVersionId,
  toVersionId,
}: {
  clientConfig?: ExternalFeedbackWorkspaceProps["clientConfig"];
  task: OptimizationTaskRecord;
  targetPaths: string[];
  fromVersionId?: string | null;
  toVersionId?: string | null;
}) {
  const appliedDiff = task.latest_execution_job?.applied_diff || null;
  const targetRows = targetPaths.map((path) => ({ path, status: fileStatusFromDiff(appliedDiff, path) }));
  const nonTargetRows = changedPathsFromDiff(appliedDiff).filter((path) => !targetPaths.includes(path));
  if (!task.applied_agent_version_id) {
    return (
      <section className="fw-task-source">
        <h4>变更对比</h4>
        <p className="fw-note-box">任务尚未应用，暂无修改后版本。生成执行方案后可先查看计划操作，应用后再查看真实文件差异。</p>
      </section>
    );
  }
  if (!fromVersionId || !toVersionId) {
    return (
      <section className="fw-task-source">
        <h4>变更对比</h4>
        <p className="fw-note-box">缺少基线版本，无法展示前后对比。</p>
      </section>
    );
  }
  if (!clientConfig) {
    return (
      <section className="fw-task-source">
        <h4>变更对比</h4>
        <p className="fw-note-box">当前视图缺少 API 配置，无法加载文件级对比。</p>
      </section>
    );
  }
  return (
    <section className="fw-task-source">
      <h4>变更对比</h4>
      <DetailMetricGrid
        items={[
          ["修改前", shortId(fromVersionId)],
          ["修改后", shortId(toVersionId)],
          ["新增", appliedDiff?.added?.length ?? "-"],
          ["修改", appliedDiff?.modified?.length ?? "-"],
          ["删除", appliedDiff?.deleted?.length ?? "-"],
        ]}
      />
      <div className="fw-file-diff-list">
        {targetRows.map((row) => (
          <TaskFileDiffRow
            clientConfig={clientConfig}
            fromVersionId={fromVersionId}
            key={row.path}
            path={row.path}
            statusText={row.status}
            toVersionId={toVersionId}
          />
        ))}
      </div>
      {nonTargetRows.length ? (
        <details className="fw-nontarget-diff">
          <summary>非目标文件变更 {nonTargetRows.length}</summary>
          <div className="fw-file-diff-list">
            {nonTargetRows.map((path) => (
              <TaskFileDiffRow
                clientConfig={clientConfig}
                fromVersionId={fromVersionId}
                key={path}
                path={path}
                statusText={fileStatusFromDiff(appliedDiff, path)}
                toVersionId={toVersionId}
              />
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function isEvalCaseExecutionPlan(task: OptimizationTaskRecord, execution: OptimizationExecutionJobRecord): boolean {
  const operations = execution.validated_output_json?.operations || [];
  if (!operations.length) return false;
  const proposalTargetType = task.proposal?.target_type;
  return proposalTargetType === "eval_case" || operations.some((operation) => (operation.path || "").startsWith("evals/"));
}

function TaskFileDiffRow({
  clientConfig,
  fromVersionId,
  path,
  statusText,
  toVersionId,
}: {
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  fromVersionId: string;
  path: string;
  statusText: string;
  toVersionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [diff, setDiff] = useState<AgentVersionFileDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (!next || diff || loading) return;
    setLoading(true);
    setError(null);
    try {
      setDiff(await diffAgentVersionFile(clientConfig, fromVersionId, toVersionId, path));
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载文件对比失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fw-file-diff-row">
      <button className="fw-file-diff-toggle" type="button" onClick={toggle}>
        <ChevronRight size={15} className={expanded ? "is-open" : ""} />
        <span>{path}</span>
        <Pill tone={fileStatusTone(statusText)}>{fileStatusText(statusText)}</Pill>
      </button>
      {expanded ? (
        <div className="fw-file-diff-body">
          {loading ? <p className="fw-muted">加载对比中...</p> : null}
          {error ? <p className="fw-warning-text">{error}</p> : null}
          {diff ? (
            diff.unified_diff ? <pre>{diff.unified_diff}</pre> : <p className="fw-muted">{diff.reason || fileStatusText(diff.status)}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ProposalsPanel({
  proposals,
  externalGovernanceItems,
  externalWebhooks,
  actionId,
  onReviewProposal,
  onCreateTask,
  onNotifyExternalItem,
  onOpenTask,
  tasksByProposalId,
}: {
  proposals: OptimizationProposalRecord[];
  externalGovernanceItems: ExternalGovernanceItemRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const totalCount = proposals.length + externalGovernanceItems.length;
  return (
    <section className="fw-panel fw-proposal-panel">
      <div className="fw-panel-header">
        <strong>优化方案审批</strong>
        <span className="fw-muted">{totalCount} 条</span>
      </div>
      <ProposalList
        proposals={proposals}
        externalGovernanceItems={externalGovernanceItems}
        externalWebhooks={externalWebhooks}
        actionId={actionId}
        onReviewProposal={onReviewProposal}
        onCreateTask={onCreateTask}
        onNotifyExternalItem={onNotifyExternalItem}
        onOpenTask={onOpenTask}
        tasksByProposalId={tasksByProposalId}
      />
    </section>
  );
}

function ProposalList({
  proposals,
  proposalOutput,
  externalGovernanceItems = [],
  externalWebhooks = [],
  actionId,
  onReviewProposal,
  onCreateTask,
  onNotifyExternalItem,
  onOpenTask,
  tasksByProposalId,
}: {
  proposals: OptimizationProposalRecord[];
  proposalOutput?: ProposalOutput | null;
  externalGovernanceItems?: ExternalGovernanceItemRecord[];
  externalWebhooks?: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onNotifyExternalItem?: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const externalGuidance = proposalOutput?.external_guidance || [];
  const externalItemsAsGuidance = externalGovernanceItems.map(externalGuidanceFromItem);
  return (
    <div className="fw-proposal-list">
      {proposals.map((proposal) => {
        const approved = proposal.status === "approved";
        const pending = proposal.status === "pending_review";
        const task = tasksByProposalId.get(proposal.proposal_id);
        return (
          <article className="fw-proposal-card" key={proposal.proposal_id}>
            <div className="fw-panel-header">
              <div>
                <h4>{proposal.title}</h4>
                <small>{shortId(proposal.proposal_id)} · {proposal.target_type} · {proposal.target_path || "-"}</small>
              </div>
              <Pill tone={proposal.status === "approved" ? "green" : proposal.status === "rejected" ? "red" : "orange"}>
                {proposalStatusText[proposal.status] || proposal.status}
              </Pill>
            </div>
            <FormattedText className="fw-proposal-long-text" value={proposal.recommendation} />
            <DetailMetricGrid items={[["base_version", shortId(proposal.base_agent_version_id)]]} />
            <FormattedTextFields
              fields={[
                ["预期效果", proposal.expected_effect || "-"],
                ["验证方式", proposal.validation || "-"],
                ["风险", proposal.risk || "-"],
              ]}
            />
            {proposal.actionability === "external_guidance" ? (
              <p className="fw-warning-text">该方案不能自动修改主智能体 workspace。</p>
            ) : null}
            <div className="fw-detail-action-row">
              {pending ? (
                <>
                  <button className="fw-small-primary" type="button" disabled={actionId === `approve:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "approve")}>
                    <CheckCircle2 size={16} /> 批准
                  </button>
                  <button className="fw-danger-button" type="button" disabled={actionId === `reject:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "reject")}>
                    <XCircle size={16} /> 拒绝
                  </button>
                  <button className="fw-small-secondary" type="button" disabled={actionId === `request_more_analysis:${proposal.proposal_id}`} onClick={() => onReviewProposal(proposal.proposal_id, "request_more_analysis")}>
                    <AlertTriangle size={16} /> 补充分析
                  </button>
                </>
              ) : null}
              {approved ? (
                <button
                  className={task ? "fw-small-secondary" : "fw-small-primary"}
                  type="button"
                  disabled={!task && actionId === `task:${proposal.proposal_id}`}
                  onClick={() => (task ? onOpenTask(task) : onCreateTask(proposal))}
                >
                  {!task && actionId === `task:${proposal.proposal_id}` ? <Loader2 size={16} className="fw-spin" /> : <ChevronRight size={16} />}
                  {task ? "查看优化任务" : "创建优化任务"}
                </button>
              ) : null}
            </div>
          </article>
        );
      })}
      {externalGuidance.map((item, index) => (
        <article className="fw-proposal-card" key={`${item.owner}:${index}`}>
          <div className="fw-panel-header">
            <h4>{item.owner}</h4>
            <Pill tone="gray">{item.actionability}</Pill>
          </div>
          <FormattedText className="fw-proposal-long-text" value={item.recommendation} />
          <p className="fw-warning-text">该方案不能自动修改主智能体 workspace。</p>
          {item.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={item.reason} /> : null}
        </article>
      ))}
      {externalItemsAsGuidance.map(({ item, guidance }) => (
        <ExternalGuidanceCard
          actionId={actionId}
          guidance={guidance}
          item={item}
          key={item.external_item_id}
          webhooks={externalWebhooks}
          onNotifyExternalItem={onNotifyExternalItem || (() => undefined)}
        />
      ))}
      {!proposals.length && !externalGuidance.length && !externalItemsAsGuidance.length ? <div className="fw-empty-inline">暂无优化方案</div> : null}
    </div>
  );
}

function TasksPanel({
  tasks,
  actionId,
  onMarkApplied,
  onRunRegression,
}: {
  tasks: OptimizationTaskRecord[];
  actionId: string | null;
  onMarkApplied: (task: OptimizationTaskRecord) => void;
  onRunRegression: (task: OptimizationTaskRecord) => void;
}) {
  return (
    <section className="fw-panel fw-task-panel">
      <div className="fw-panel-header">
        <strong>优化任务</strong>
        <span className="fw-muted">{tasks.length} 个</span>
      </div>
      <div className="fw-proposal-list fw-task-list">
        {tasks.map((task) => (
          <TaskDetailCard
            key={task.optimization_task_id}
            task={task}
            actionId={actionId}
            onMarkApplied={onMarkApplied}
            onRunRegression={onRunRegression}
          />
        ))}
        {!tasks.length ? <div className="fw-empty-inline">暂无优化任务</div> : null}
      </div>
    </section>
  );
}

function EvalPanel({
  evalCases,
  evalRuns,
  actionId,
  selectedCase,
  selectedCaseEvalCases,
  onSyncDataset,
  onRunDatasetEval,
}: {
  evalCases: EvalCaseRecord[];
  evalRuns: EvalRunRecord[];
  actionId: string | null;
  selectedCase: FeedbackCaseRecord | null;
  selectedCaseEvalCases: EvalCaseRecord[];
  onSyncDataset: (feedbackCaseId?: string) => void;
  onRunDatasetEval: () => void;
}) {
  const latestRun = evalRuns[0] || null;
  const activeCases = evalCases.filter((item) => item.status === "active");
  const summary = latestRun?.summary || {};
  const total = Number(summary.total || 0);
  const passed = Number(summary.passed || 0);
  const passRate = total > 0 ? `${Math.round((passed / total) * 100)}%` : "-";
  return (
    <section className="fw-panel fw-eval-panel">
      <div className="fw-panel-header">
        <div>
          <strong>回归评估</strong>
          <span className="fw-muted"> {activeCases.length} 个 active case</span>
        </div>
        <div className="fw-panel-header-actions">
          <button
            className="fw-small-secondary"
            type="button"
            disabled={!selectedCase || actionId === `sync-eval:${selectedCase?.feedback_case_id}`}
            onClick={() => selectedCase && onSyncDataset(selectedCase.feedback_case_id)}
          >
            {actionId === `sync-eval:${selectedCase?.feedback_case_id}` ? <Loader2 size={16} className="fw-spin" /> : <Database size={16} />}
            同步当前处置单
          </button>
          <button className="fw-small-secondary" type="button" disabled={actionId === "sync-eval"} onClick={() => onSyncDataset()}>
            {actionId === "sync-eval" ? <Loader2 size={16} className="fw-spin" /> : <Database size={16} />}
            同步反馈数据集
          </button>
          <button className="fw-small-primary" type="button" disabled={!activeCases.length || actionId === "dataset-eval"} onClick={onRunDatasetEval}>
            {actionId === "dataset-eval" ? <Loader2 size={16} className="fw-spin" /> : <PlayCircle size={16} />}
            运行批量评估
          </button>
        </div>
      </div>
      <DetailMetricGrid
        items={[
          ["active_cases", activeCases.length],
          ["selected_case_cases", selectedCaseEvalCases.length],
          ["eval_runs", evalRuns.length],
          ["latest_result", latestRun?.result_status || "-"],
          ["latest_version", shortId(latestRun?.agent_version_id)],
          ["pass_rate", passRate],
        ]}
      />
      <div className="fw-eval-grid">
        <section className="fw-eval-column">
          <div className="fw-eval-column-title">
            <Database size={16} />
            <strong>反馈评估集</strong>
          </div>
          <div className="fw-eval-list">
            {evalCases.map((item) => (
              <article className="fw-eval-card" key={item.eval_case_id}>
                <div className="fw-detail-record-head">
                  <h4>{shortId(item.eval_case_id)} · {shortId(item.source_feedback_case_id)}</h4>
                  <Pill tone={item.status === "active" ? "green" : "gray"}>{item.status}</Pill>
                </div>
                <FormattedText className="fw-eval-card-text" value={item.prompt} />
                <small>{item.labels?.join(", ") || "-"}</small>
                <FormattedText className="fw-eval-card-text" value={item.expected_behavior || "-"} />
              </article>
            ))}
            {!evalCases.length ? <div className="fw-empty-inline">暂无评估用例</div> : null}
          </div>
        </section>
        <section className="fw-eval-column">
          <div className="fw-eval-column-title">
            <BarChart3 size={16} />
            <strong>评估运行</strong>
          </div>
          <div className="fw-eval-list">
            {evalRuns.map((run) => (
              <article className="fw-eval-card" key={run.eval_run_id}>
                <div className="fw-detail-record-head">
                  <h4>{shortId(run.eval_run_id)} · {shortId(run.agent_version_id)}</h4>
                  <Pill tone={evalStatusTone(run.result_status || run.status)}>{run.result_status || run.status}</Pill>
                </div>
                <DetailMetricGrid
                  items={[
                    ["total", run.summary?.total ?? 0],
                    ["passed", run.summary?.passed ?? 0],
                    ["failed", run.summary?.failed ?? 0],
                    ["review", run.summary?.needs_human_review ?? 0],
                  ]}
                />
                <small>创建：{formatDate(run.created_at)} · 完成：{formatDate(run.completed_at)}</small>
                {run.items?.slice(0, 3).map((item) => (
                  <div className="fw-eval-item-line" key={item.eval_run_item_id}>
                    <strong>{shortId(item.eval_case_id)}：{item.status}</strong>
                    <FormattedText value={evalItemSummary(item)} />
                  </div>
                ))}
              </article>
            ))}
            {!evalRuns.length ? <div className="fw-empty-inline">暂无评估运行</div> : null}
          </div>
        </section>
      </div>
    </section>
  );
}

function ExternalGuidanceCard({
  guidance,
  item,
  webhooks,
  actionId,
  onNotifyExternalItem,
}: {
  guidance: {
    owner: string;
    actionability: string;
    recommendation: string;
    reason?: string | null;
  };
  item?: ExternalGovernanceItemRecord;
  webhooks: ExternalGovernanceWebhookRecord[];
  actionId: string | null;
  onNotifyExternalItem: (item: ExternalGovernanceItemRecord, webhookAlias: string) => void;
}) {
  const [selectedAlias, setSelectedAlias] = useState(webhooks[0]?.alias || "");
  const currentAlias = selectedAlias || webhooks[0]?.alias || "";
  const running = item ? actionId === `external-notify:${item.external_item_id}` : false;
  const canNotify = Boolean(item && currentAlias && webhooks.length && !running);
  const notification = item?.latest_notification;
  return (
    <article className="fw-proposal-card fw-proposal-detail-card fw-external-guidance-card">
      <div className="fw-proposal-detail-title">
        <Pill tone={externalGovernanceTone(item?.status)}>{item?.status || "external_guidance"}</Pill>
        <h4>{guidance.owner}</h4>
        <small>{guidance.actionability}</small>
      </div>
      <FormattedText className="fw-proposal-long-text" value={guidance.recommendation} />
      {guidance.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={guidance.reason} /> : null}
      <div className="fw-external-notify-row">
        <label className="fw-select-field">
          <span>通知目标</span>
          <select value={currentAlias} onChange={(event) => setSelectedAlias(event.target.value)} disabled={!webhooks.length || running}>
            {!webhooks.length ? <option value="">未配置Webhook，请在 /data/external-governance-webhooks.yaml 文件中增加</option> : null}
            {webhooks.map((webhook) => (
              <option key={webhook.alias} value={webhook.alias}>{webhook.name || webhook.alias}</option>
            ))}
          </select>
        </label>
        <button
          className="fw-small-secondary"
          type="button"
          disabled={!canNotify}
          onClick={() => item && onNotifyExternalItem(item, currentAlias)}
        >
          {running ? <Loader2 size={16} className="fw-spin" /> : <ChevronRight size={16} />}
          {item?.status === "notification_failed" ? "重试通知" : "通知外部系统"}
        </button>
      </div>
      <div className="fw-external-notify-meta">
        <span>治理项：{shortId(item?.external_item_id)}</span>
        <span>最近目标：{item?.latest_webhook_alias || "-"}</span>
        <span>通知状态：{notification?.status || "-"}</span>
        {notification?.http_status ? <span>HTTP {notification.http_status}</span> : null}
      </div>
      {notification?.error ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={notification.error} /> : null}
      {!item ? <p className="fw-warning-text">当前建议还没有外部治理项，需重新生成建议或查看原始输出。</p> : null}
    </article>
  );
}

function externalGuidanceFromItem(item: ExternalGovernanceItemRecord): {
  item: ExternalGovernanceItemRecord;
  guidance: { owner: string; actionability: string; recommendation: string; reason?: string | null };
} {
  return {
    item,
    guidance: {
      owner: item.owner,
      actionability: item.actionability,
      recommendation: item.recommendation,
      reason: item.reason,
    },
  };
}

function Metric({ label, value }: { label: string; value?: string | number | null }) {
  return (
    <span className="fw-case-status-item">
      <small>{label}</small>
      <strong title={String(value ?? "-")}>{value ?? "-"}</strong>
    </span>
  );
}

function DetailMetric({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`fw-case-status-item fw-case-detail-trigger ${active ? "is-active" : ""}`} onClick={onClick} type="button">
      <small>{label}</small>
      <strong>{count} 条</strong>
      <span>详情</span>
    </button>
  );
}

function Pill({ children, tone = "blue" }: { children: ReactNode; tone?: "blue" | "green" | "orange" | "red" | "gray" | "purple" }) {
  return <span className={`fw-pill fw-pill-${tone}`}>{children}</span>;
}

function buildSourceRows(data: FeedbackWorkbenchData): SourceRow[] {
  if (data.sources?.length) {
    return data.sources
      .map<SourceRow>((item) => ({
        id: item.source_id,
        kind: item.source_kind,
        label: item.label || item.labels?.join(", ") || item.source_kind,
        status: item.status,
        createdAt: item.created_at || item.updated_at || undefined,
        runId: item.run_id,
        sessionId: item.session_id,
        alertId: item.alert_id,
        caseId: item.case_id,
        feedbackCaseId: item.feedback_case_id,
        evalCaseId: item.eval_case_id,
        raw: item,
      }))
      .sort((left, right) => String(right.createdAt || "").localeCompare(String(left.createdAt || "")));
  }
  const signalRows = data.signals.map<SourceRow>((item) => ({
    id: item.signal_id,
    kind: "signal",
    label: item.labels?.join(", ") || item.source_type || "feedback signal",
    status: item.requires_review ? "requires_review" : "collected",
    createdAt: item.created_at || item.timestamp || undefined,
    runId: item.run_id || item.matched_run_id,
    sessionId: item.session_id,
    alertId: item.alert_id,
    caseId: item.case_id,
    raw: item,
  }));
  const eventRows = data.events.map<SourceRow>((item) => ({
    id: item.event_id,
    kind: "soc_event",
    label: item.event_type,
    status: item.matched_run_id || item.run_id ? "matched" : "pending_correlation",
    createdAt: item.created_at || item.timestamp,
    runId: item.run_id || item.matched_run_id,
    sessionId: item.session_id,
    alertId: item.alert_id,
    caseId: item.case_id,
    raw: item,
  }));
  const pendingRows = data.pending_correlations
    .filter((item) => item.status !== "resolved")
    .map<SourceRow>((item) => ({
      id: item.pending_id,
      kind: "pending_correlation",
      label: item.event_type || "pending correlation",
      status: item.status || "pending",
      createdAt: item.created_at,
      sessionId: item.session_id,
      alertId: item.alert_id,
      caseId: item.case_id,
      raw: item,
    }));
  return [...signalRows, ...eventRows, ...pendingRows].sort((left, right) => String(right.createdAt || "").localeCompare(String(left.createdAt || "")));
}

function buildBatchSourceRows(batch: FeedbackOptimizationBatchRecord | null, sources: FeedbackSourceRecord[]): SourceRow[] {
  if (!batch) return [];
  const sourceByKey = new Map(sources.map((source) => [`${source.source_kind}:${source.source_id}`, source]));
  return (batch.source_refs || []).map<SourceRow>((ref) => {
    const source = sourceByKey.get(`${ref.source_kind}:${ref.source_id}`);
    if (source) {
      return {
        id: source.source_id,
        kind: source.source_kind,
        label: source.label || source.labels?.join(", ") || source.source_kind,
        status: source.status,
        createdAt: source.created_at || source.updated_at || undefined,
        runId: source.run_id,
        sessionId: source.session_id,
        alertId: source.alert_id,
        caseId: source.case_id,
        feedbackCaseId: source.feedback_case_id,
        evalCaseId: source.eval_case_id,
        raw: source,
      };
    }
    return {
      id: ref.source_id,
      kind: ref.source_kind,
      label: ref.source_id,
      status: "source_ref",
      raw: ref as unknown as FeedbackSourceRecord,
    };
  });
}

function buildBatchAttributionJobs(batch: FeedbackOptimizationBatchRecord | null): FeedbackAnalysisJobRecord[] {
  if (!batch) return [];
  const jobs = Array.isArray(batch.attribution_jobs) ? batch.attribution_jobs.filter(Boolean) : [];
  const byId = new Map(jobs.map((job) => [job.job_id, job]));
  for (const jobId of batch.attribution_job_ids || []) {
    if (byId.has(jobId)) continue;
    byId.set(jobId, {
      job_id: jobId,
      job_type: "attribution",
      feedback_case_id: "",
      evidence_package_id: "",
      status: "unknown",
      profile_name: "attribution-analyzer",
      created_at: "",
      input_path: "",
      raw_output_path: "",
      validated_output_path: "",
      error_path: "",
    });
  }
  return Array.from(byId.values());
}

function attributionOutputFromJob(job: FeedbackAnalysisJobRecord): AttributionOutput | null {
  const output = job.validated_output_json || job.raw_output_json;
  if (!output || typeof output !== "object" || Array.isArray(output)) return null;
  if ((output as Record<string, unknown>).schema_version !== "attribution-output/v1") return null;
  return output as AttributionOutput;
}

function defaultBatchDetail(batch: FeedbackOptimizationBatchRecord | null): BatchDetailView {
  if (!batch) return "feedback";
  if (batch.latest_eval_run) return "regression";
  if (batch.optimization_plan || batch.optimization_task || batch.execution_job) return "plan";
  if (batch.attribution_jobs?.length || batch.attribution_job_ids?.length) return "attribution";
  return "feedback";
}

function attributionStatusText(jobs: FeedbackAnalysisJobRecord[], total: number): string {
  if (!total) return "未运行";
  const completed = jobs.filter((job) => job.status === "completed").length;
  const failed = jobs.filter((job) => job.status === "failed" || job.status === "timeout").length;
  const review = jobs.filter((job) => job.status === "needs_human_review").length;
  const running = jobs.filter((job) => ["created", "queued", "running", "schema_validating", "evidence_packaging"].includes(String(job.status))).length;
  if (failed) return `失败 ${failed}/${total}`;
  if (review) return `复核 ${review}/${total}`;
  if (running) return `运行中 ${running}/${total}`;
  if (completed === total) return `完成 ${completed}/${total}`;
  return `${jobs.length}/${total} 条`;
}

function attributionStatusTone(jobs: FeedbackAnalysisJobRecord[], total: number): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (!total) return "gray";
  if (jobs.some((job) => job.status === "failed" || job.status === "timeout")) return "red";
  if (jobs.some((job) => job.status === "needs_human_review")) return "orange";
  if (jobs.some((job) => ["created", "queued", "running", "schema_validating", "evidence_packaging"].includes(String(job.status)))) return "blue";
  if (jobs.filter((job) => job.status === "completed").length === total) return "green";
  return "gray";
}

function batchRegressionStatusText(batch: FeedbackOptimizationBatchRecord): string {
  const run = batch.latest_eval_run;
  if (!run) return "未运行";
  const total = run.summary?.total ?? run.items?.length ?? 0;
  const passed = run.summary?.passed ?? 0;
  const failed = run.summary?.failed ?? 0;
  const review = run.summary?.needs_human_review ?? 0;
  if (total) return `${run.result_status || run.status} · ${passed}/${total} 通过`;
  if (failed) return `${run.result_status || run.status} · ${failed} 失败`;
  if (review) return `${run.result_status || run.status} · ${review} 复核`;
  return run.result_status || run.status || "已运行";
}

function sourceRowKey(row: SourceRow): string {
  return `${row.kind}:${row.id}`;
}

function filterSourceRows(rows: SourceRow[], query: string): SourceRow[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return rows;
  return rows.filter((row) => JSON.stringify(row.raw, null, 0).toLowerCase().includes(normalized));
}

function filterCases(cases: FeedbackCaseRecord[], query: string): FeedbackCaseRecord[] {
  const normalized = query.trim().toLowerCase();
  const sorted = [...cases].sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  if (!normalized) return sorted;
  return sorted.filter((item) => JSON.stringify(item, null, 0).toLowerCase().includes(normalized));
}

function filterBatches(batches: FeedbackOptimizationBatchRecord[], query: string): FeedbackOptimizationBatchRecord[] {
  const normalized = query.trim().toLowerCase();
  const sorted = [...batches].sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  if (!normalized) return sorted;
  return sorted.filter((item) => JSON.stringify(item, null, 0).toLowerCase().includes(normalized));
}

function latest(values?: string[]): string | undefined {
  if (!Array.isArray(values) || !values.length) return undefined;
  return values[values.length - 1];
}

function latestItem<T>(values?: T[]): T | null {
  if (!Array.isArray(values) || !values.length) return null;
  return values[values.length - 1];
}

function rawRecordArray(value: unknown, key: string): Array<Record<string, unknown>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const items = (value as Record<string, unknown>)[key];
  return Array.isArray(items) ? items.filter(isRecord) : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function rawString(value: unknown, key: string): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const item = (value as Record<string, unknown>)[key];
  return typeof item === "string" ? item : "";
}

function parseFormattedText(text: string): FormattedTextBlock[] {
  const blocks: FormattedTextBlock[] = [];
  const paragraph: string[] = [];
  let listBlock: Extract<FormattedTextBlock, { type: "ol" | "ul" }> | null = null;
  let tableLines: string[] = [];

  function flushParagraph() {
    if (!paragraph.length) return;
    blocks.push({ type: "paragraph", lines: [...paragraph] });
    paragraph.length = 0;
  }

  function flushList() {
    if (!listBlock) return;
    blocks.push(listBlock);
    listBlock = null;
  }

  function flushTable() {
    if (!tableLines.length) return;
    const parsed = parseMarkdownTable(tableLines);
    if (parsed) {
      blocks.push(parsed);
    } else {
      blocks.push({ type: "paragraph", lines: [...tableLines] });
    }
    tableLines = [];
  }

  for (const rawLine of normalizeFormattedText(text).split("\n")) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushTable();
      continue;
    }

    if (trimmed === "---") {
      flushParagraph();
      flushList();
      flushTable();
      continue;
    }

    if (isMarkdownTableLine(trimmed)) {
      flushParagraph();
      flushList();
      tableLines.push(trimmed);
      continue;
    }

    flushTable();

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ type: "heading", level: heading[1].length, text: cleanFormattedText(heading[2]) });
      continue;
    }

    const ordered = trimmed.match(/^(\d+)[.)]\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      if (!listBlock || listBlock.type !== "ol") {
        flushList();
        listBlock = { type: "ol", items: [] };
      }
      listBlock.items.push(cleanFormattedText(ordered[2]));
      continue;
    }

    const unordered = trimmed.match(/^[-*]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      if (!listBlock || listBlock.type !== "ul") {
        flushList();
        listBlock = { type: "ul", items: [] };
      }
      listBlock.items.push(cleanFormattedText(unordered[1]));
      continue;
    }

    flushList();
    paragraph.push(cleanFormattedText(line));
  }

  flushParagraph();
  flushList();
  flushTable();
  return blocks.length ? blocks : [{ type: "paragraph", lines: ["-"] }];
}

function normalizeFormattedText(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .replace(/\s+---\s+/g, "\n\n---\n\n")
    .replace(/\s+(#{1,4}\s+)/g, "\n\n$1")
    .replace(/(#{1,4}\s+[^\n|]+?)\s{2,}(\|)/g, "$1\n$2")
    .replace(/([^\n|])\s{2,}(\|)/g, "$1\n$2")
    .replace(/\|\s+(?=\|)/g, "|\n")
    .replace(/\|\s+(?=(?:---|#{1,4}\s))/g, "|\n\n");
}

function isMarkdownTableLine(line: string): boolean {
  return line.startsWith("|") && line.endsWith("|") && line.split("|").length >= 4;
}

function isMarkdownTableDivider(line: string): boolean {
  const cells = line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
  return Boolean(cells.length) && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function splitMarkdownTableRow(line: string): string[] {
  return line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cleanFormattedText(cell.trim()));
}

function parseMarkdownTable(lines: string[]): Extract<FormattedTextBlock, { type: "table" }> | null {
  const rows = lines.filter(isMarkdownTableLine);
  if (rows.length < 2) return null;
  const header = splitMarkdownTableRow(rows[0]);
  const dividerOffset = isMarkdownTableDivider(rows[1]) ? 1 : -1;
  const bodyRows = rows
    .slice(dividerOffset === 1 ? 2 : 1)
    .map(splitMarkdownTableRow)
    .filter((row) => row.some(Boolean));
  if (!header.length || !bodyRows.length) return null;
  return { type: "table", headers: header, rows: bodyRows };
}

function cleanFormattedText(text: string): string {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .trim();
}

function jobErrorCode(job?: FeedbackAnalysisJobRecord | null): string {
  return job?.error_json?.error_code || (job?.status === "failed" ? "JOB_FAILED" : "JOB_NEEDS_REVIEW");
}

function jobErrorMessage(job: FeedbackAnalysisJobRecord | null | undefined, fallback: string): string {
  return job?.error_json?.message || fallback;
}

function validationErrorItems(job?: FeedbackAnalysisJobRecord | null): Array<Record<string, unknown>> {
  const errors = job?.error_json?.validation_errors;
  if (!Array.isArray(errors)) return [];
  return errors.filter((item): item is Record<string, unknown> => isRecord(item));
}

function validationErrorCount(job?: FeedbackAnalysisJobRecord | null): number {
  return validationErrorItems(job).length;
}

function validationFieldSummary(job?: FeedbackAnalysisJobRecord | null): string {
  const fields = validationErrorItems(job)
    .map(validationErrorPath)
    .filter((item) => item && item !== "-")
    .slice(0, 3);
  if (!fields.length) return "";
  const suffix = validationErrorCount(job) > fields.length ? " 等" : "";
  return `：${fields.join("、")}${suffix}`;
}

function validationErrorPath(error: Record<string, unknown>): string {
  const loc = error.loc;
  if (Array.isArray(loc)) return loc.map((item) => String(item)).join(".");
  if (typeof loc === "string") return loc;
  return "-";
}

function validationErrorMessage(error: Record<string, unknown>): string {
  return typeof error.msg === "string" ? error.msg : "校验失败";
}

function isRetryableJobStatus(status?: string | null): boolean {
  return status === "failed";
}

function analysisActionLabel(kind: "attribution" | "proposal", status: string | null | undefined, count: number): string {
  const noun = kind === "attribution" ? "归因" : "建议";
  if (status === "failed") return `重试${noun}`;
  if (status === "needs_human_review") return `${noun}需复核`;
  if (status === "queued" || status === "running" || status === "schema_validating") {
    return kind === "attribution" ? "归因执行中" : "建议生成中";
  }
  if (count > 0) return kind === "attribution" ? "归因已启动" : "建议已生成";
  return kind === "attribution" ? "启动归因" : "生成建议";
}

function evidenceFileName(item: Record<string, unknown>): string | null {
  return typeof item.path === "string" ? item.path : null;
}

function firstEvidenceFileName(items?: Array<Record<string, unknown>>): string | undefined {
  for (const item of items || []) {
    const fileName = evidenceFileName(item);
    if (fileName) return fileName;
  }
  return undefined;
}

function jsonPreview(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function isEmptyJsonValue(value: unknown): boolean {
  if (Array.isArray(value)) return value.length === 0;
  if (value && typeof value === "object") return Object.keys(value as Record<string, unknown>).length === 0;
  return false;
}

function traceRefsFromContent(content: unknown): Array<{ traceId: string; url: string }> {
  const values = Array.isArray(content) ? content : content && typeof content === "object" ? [content] : [];
  const refs: Array<{ traceId: string; url: string }> = [];
  for (const value of values) {
    if (!value || typeof value !== "object") continue;
    const record = value as Record<string, unknown>;
    const traceId = typeof record.trace_id === "string" ? record.trace_id : "";
    const url = typeof record.trace_url === "string" ? record.trace_url : "";
    if (traceId && url) refs.push({ traceId, url });
  }
  return refs;
}

function summaryText(value: Record<string, unknown>): string {
  const comment = typeof value.comment === "string" ? value.comment : "";
  if (comment) return comment.slice(0, 120);
  const reason = typeof value.reason === "string" ? value.reason : "";
  if (reason) return reason;
  return JSON.stringify(value).slice(0, 120);
}

function evalItemSummary(item: NonNullable<EvalRunRecord["items"]>[number]): string {
  if (item.answer_summary) return item.answer_summary;
  const message = item.error_json?.message;
  return typeof message === "string" ? message : "-";
}

function latestEvalRunItemForCase(evalRuns: EvalRunRecord[], evalCaseId: string): NonNullable<EvalRunRecord["items"]>[number] | undefined {
  for (const run of evalRuns) {
    const item = run.items?.find((candidate) => candidate.eval_case_id === evalCaseId);
    if (item) return item;
  }
  return undefined;
}

function evalCaseEditDraft(evalCase: EvalCaseRecord): EvalCaseEditDraft {
  const status = evalCase.status === "draft" || evalCase.status === "archived" ? evalCase.status : "active";
  return {
    prompt: evalCase.prompt || "",
    expectedBehavior: evalCase.expected_behavior || "",
    labelsText: (evalCase.labels || []).join(", "),
    status,
    checksText: JSON.stringify(evalCase.checks_json || {}, null, 2),
  };
}

function parseEvalCaseLabels(value: string): string[] {
  const seen = new Set<string>();
  const labels: string[] = [];
  for (const label of value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean)) {
    if (seen.has(label)) continue;
    seen.add(label);
    labels.push(label);
  }
  return labels;
}

function jobStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "completed") return "green";
  if (status === "failed" || status === "execution_failed") return "red";
  if (status === "needs_human_review" || status === "execution_ready" || status === "ready") return "orange";
  if (status === "queued" || status === "running" || status === "execution_planning") return "blue";
  return "gray";
}

function profileDisplayName(profileName?: string | null): string {
  if (profileName === "main-agent") return "主智能体";
  if (profileName === "attribution-analyzer") return "归因分析智能体";
  if (profileName === "proposal-generator") return "优化方案生成智能体";
  if (profileName === "execution-optimizer") return "执行优化智能体";
  return profileName || "-";
}

function evalStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "passed" || status === "completed") return "green";
  if (status === "failed") return "red";
  if (status === "needs_human_review") return "orange";
  if (status === "running") return "blue";
  return "gray";
}

function batchStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "completed" || status === "applied_pending_regression" || status === "passed") return "green";
  if (status === "failed" || status === "rejected" || status === "execution_failed") return "red";
  if (status === "pending_approval" || status === "needs_human_review" || status === "execution_ready") return "orange";
  if (status === "draft" || status === "attribution_running" || status === "execution_planning" || status === "regression_running") return "blue";
  return "gray";
}

function fileStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "modified") return "orange";
  if (status === "added") return "green";
  if (status === "deleted") return "red";
  if (status === "unchanged") return "gray";
  return "blue";
}

function fileStatusText(status?: string | null): string {
  if (status === "modified") return "已修改";
  if (status === "added") return "新增";
  if (status === "deleted") return "删除";
  if (status === "unchanged") return "未变化";
  if (status === "missing") return "未纳入快照";
  if (status === "binary_or_too_large") return "不可预览";
  return status || "未知";
}

function proposalStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "approved") return "green";
  if (status === "rejected") return "red";
  if (status === "needs_more_analysis") return "purple";
  if (status === "pending_review") return "orange";
  return "gray";
}

function externalGovernanceTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "notified") return "green";
  if (status === "notification_failed") return "red";
  if (status === "pending_notification") return "orange";
  return "gray";
}

function planTaskTone(task: FeedbackOptimizationPlanTaskRecord): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (task.applied_agent_version_id || task.status === "notified") return "green";
  if (task.status === "failed" || task.status === "execution_failed" || task.status === "notification_failed") return "red";
  if (task.status === "needs_human_review" || task.status === "pending_notification") return "orange";
  if (task.status === "pending_execution" || task.status === "execution_planning" || task.status === "queued" || task.status === "running") return "blue";
  return "gray";
}

function findExternalGovernanceItem(
  items: ExternalGovernanceItemRecord[],
  guidance: { external_item_id?: string; source_index?: number },
  proposalJobId?: string | null,
  index?: number,
): ExternalGovernanceItemRecord | undefined {
  if (guidance.external_item_id) {
    const matched = items.find((item) => item.external_item_id === guidance.external_item_id);
    if (matched) return matched;
  }
  const sourceIndex = typeof guidance.source_index === "number" ? guidance.source_index : index;
  return items.find((item) => item.proposal_job_id === proposalJobId && item.source_index === sourceIndex);
}

function buildTaskByProposalId(tasks: OptimizationTaskRecord[]): Map<string, OptimizationTaskRecord> {
  const tasksByProposalId = new Map<string, OptimizationTaskRecord>();
  for (const task of tasks) {
    const proposalId = taskProposalId(task);
    if (proposalId && !tasksByProposalId.has(proposalId)) {
      tasksByProposalId.set(proposalId, task);
    }
  }
  return tasksByProposalId;
}

function taskProposalId(task: OptimizationTaskRecord): string | null {
  return task.proposal_id || task.proposal_ids?.[0] || task.proposal?.proposal_id || null;
}

function taskStatusDescription(status?: string | null): string {
  if (status === "pending_execution") return "当前任务已创建，等待人工或后续 patch 执行；系统尚未自动修改文件。";
  if (status === "execution_planning") return "执行优化智能体正在生成受控执行方案，尚未修改文件。";
  if (status === "execution_ready") return "执行方案已生成，等待确认应用。";
  if (status === "execution_failed") return "执行方案生成或应用失败，需要重新生成或人工复核。";
  if (status === "applied_pending_regression") return "当前任务已确认应用并创建主智能体版本快照，等待手动回归验证。";
  if (status === "regression_running") return "当前任务正在运行回归验证。";
  if (status === "completed") return "当前任务已完成。";
  if (status === "failed") return "当前任务回归验证失败，需要继续修复或人工复核。";
  if (status === "needs_human_review") return "当前任务需要人工复核回归结果。";
  if (status === "closed") return "当前任务已关闭。";
  return "当前任务仅记录优化交接信息，具体执行状态以任务状态为准。";
}

function fileStatusFromDiff(diff: unknown, targetPath: string): string {
  if (!diff || typeof diff !== "object") return "unknown";
  const archivePath = toArchivePath(targetPath);
  const record = diff as { added?: Array<Record<string, unknown>>; modified?: Array<Record<string, unknown>>; deleted?: Array<Record<string, unknown>> };
  if ((record.added || []).some((item) => rawString(item, "path") === archivePath)) return "added";
  if ((record.deleted || []).some((item) => rawString(item, "path") === archivePath)) return "deleted";
  if ((record.modified || []).some((item) => rawString(item, "path") === archivePath)) return "modified";
  return "unchanged";
}

function changedPathsFromDiff(diff: unknown): string[] {
  if (!diff || typeof diff !== "object") return [];
  const record = diff as { added?: Array<Record<string, unknown>>; modified?: Array<Record<string, unknown>>; deleted?: Array<Record<string, unknown>> };
  const paths = [
    ...(record.added || []).map((item) => fromArchivePath(rawString(item, "path"))),
    ...(record.deleted || []).map((item) => fromArchivePath(rawString(item, "path"))),
    ...(record.modified || []).map((item) => fromArchivePath(rawString(item, "path"))),
  ].filter(Boolean);
  return Array.from(new Set(paths));
}

function toArchivePath(path: string): string {
  return path.startsWith("workspace/") ? path : `workspace/${path}`;
}

function fromArchivePath(path: string): string {
  return path.startsWith("workspace/") ? path.slice("workspace/".length) : path;
}

function proposalEvidenceText(proposal: OptimizationProposalRecord): string {
  const evidenceRefs = proposal.evidence_refs;
  if (Array.isArray(evidenceRefs)) {
    const labels = evidenceRefs.map(proposalEvidenceRefText).filter(Boolean);
    if (labels.length) return labels.slice(0, 4).join("、");
  }
  return "agent run、evidence package、feedback signal";
}

function proposalEvidenceRefText(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "";
  const id = typeof record.id === "string" ? shortId(record.id) : "";
  const reason = typeof record.reason === "string" ? record.reason : "";
  return [type, id, reason].filter(Boolean).join(" / ");
}

function shortId(value?: string | null): string {
  if (!value) return "-";
  if (value.length <= 16) return value;
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

function formatDate(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function reviewComment(action: OptimizationProposalReviewAction): string {
  if (action === "approve") return "Feedback 工作台批准该 optimization proposal。";
  if (action === "reject") return "Feedback 工作台拒绝该 optimization proposal。";
  return "Feedback 工作台要求补充分析。";
}
