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
  createEvalRun,
  createOptimizationTask,
  createProposalJob,
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
  revalidateProposalOutput,
  reviewOptimizationProposal,
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
  FeedbackRunRecord,
  FeedbackSignalRecord,
  FeedbackWorkbenchData,
  OptimizationProposalRecord,
  OptimizationProposalReviewAction,
  OptimizationTaskRecord,
  PendingCorrelationRecord,
  ProposalOutput,
  SocEventRecord,
} from "../types/feedback";

type MenuKey = "signals" | "cases" | "evals" | "versions";
type SourceKind = "signal" | "event" | "pending";
type CaseDetailView = "summary" | "evidence" | "attribution" | "proposal" | "runs" | "tasks" | "evals";
type ProposalDetailTab = "proposals" | "raw" | "records";
type AttributionDetailTab = "result" | "raw" | "records";

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
  raw: FeedbackSignalRecord | SocEventRecord | PendingCorrelationRecord;
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
};

const menuText: Record<MenuKey, string> = {
  signals: "反馈信息",
  cases: "反馈处置",
  evals: "回归评估",
  versions: "版本管理",
};

const sourceKindText: Record<SourceKind, string> = {
  signal: "Feedback signal",
  event: "SOC event",
  pending: "待关联",
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
  const [caseDetailView, setCaseDetailView] = useState<CaseDetailView>("summary");
  const [attributionDetailTab, setAttributionDetailTab] = useState<AttributionDetailTab>("result");
  const [caseDetails, setCaseDetails] = useState<CaseDetails>({});
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [runtimeStatus, setRuntimeStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [actionId, setActionId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [proposalRegenerateDraft, setProposalRegenerateDraft] = useState<ProposalRegenerateDraft | null>(null);
  const proposalRegenerateBusy = Boolean(actionId?.startsWith("proposal-regenerate:"));

  const refreshWorkbench = useCallback(async () => {
    try {
      const next = await getFeedbackWorkbenchData(clientConfig, { limit: 500 });
      setData(next);
      setSelectedCaseId((current) => current || next.cases[0]?.feedback_case_id || null);
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
  const selectedSource = useMemo(() => {
    if (!visibleSources.length) return null;
    if (selectedSourceKey) {
      const matched = visibleSources.find((row) => sourceRowKey(row) === selectedSourceKey);
      if (matched) return matched;
    }
    return visibleSources[0];
  }, [visibleSources, selectedSourceKey]);
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
      setActiveMenu("cases");
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
      setToast("已有优化建议生成记录，可在详情中查看");
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
    try {
      const job = await regenerateProposalJob(clientConfig, feedbackCaseId, {
        regeneration_instruction: instruction,
      });
      setToast(`已重新生成建议 job ${shortId(job.job_id)}：${job.status}`);
      setCaseDetailView("proposal");
      setProposalRegenerateDraft(null);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新生成建议失败");
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
      setActiveMenu("cases");
      return;
    }
    setActiveMenu("cases");
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

  async function markTaskApplied(task: OptimizationTaskRecord) {
    setActionId(`apply:${task.optimization_task_id}`);
    try {
      const updated = await markOptimizationTaskApplied(clientConfig, task.optimization_task_id, `由反馈处置界面确认任务 ${task.optimization_task_id} 已应用。`);
      setToast(`已创建版本快照 ${shortId(updated.applied_agent_version_id)}`);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "标记已应用失败");
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
        {(Object.keys(menuText) as MenuKey[]).map((key) => (
          <button className={activeMenu === key ? "active" : ""} key={key} onClick={() => setActiveMenu(key)} type="button">
            {menuText[key]}
            {key === "versions" && agentVersions.length > 0 ? <span className="fw-menu-badge">{agentVersions.length}</span> : null}
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
            onCreateCase={createCaseFromSelection}
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
            <span>{"当前链路：feedback signal / SOC event -> feedback case -> evidence package -> attribution job -> proposal job -> approval -> optimization task -> regression eval。"}</span>
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
            aria-label="重新生成优化建议"
            onClick={(event) => event.stopPropagation()}
            onSubmit={submitProposalRegenerate}
          >
            <header className="modal-head">
              <div>
                <h3>重新生成优化建议</h3>
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
  onCreateCase,
}: {
  rows: SourceRow[];
  selectedIds: string[];
  selectedSource: SourceRow | null;
  actionId: string | null;
  onToggle: (sourceId: string, checked: boolean) => void;
  onSelectSource: (row: SourceRow) => void;
  onCreateCase: () => void;
}) {
  return (
    <section className="fw-panel fw-signals-page">
      <div className="fw-panel-header">
        <strong>反馈信息</strong>
        <button className="fw-small-primary" type="button" onClick={onCreateCase} disabled={!selectedIds.length || actionId === "create-case"}>
          {actionId === "create-case" ? <Loader2 size={16} className="fw-spin" /> : <FolderKanban size={16} />}
          创建反馈处置单
        </button>
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
                  disabled={row.kind === "pending"}
                  onChange={(event) => onToggle(row.id, event.target.checked)}
                  type="checkbox"
                />
              </span>
              <span><Pill tone={row.kind === "pending" ? "orange" : row.kind === "event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill></span>
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
          <Pill tone={row.kind === "pending" ? "orange" : row.kind === "event" ? "green" : "blue"}>{sourceKindText[row.kind]}</Pill>
          <h3>{row.label}</h3>
          <small title={row.id}>{row.id}</small>
        </div>
        {row.kind !== "pending" ? (
          <button className={selected ? "fw-small-secondary" : "fw-small-primary"} onClick={() => onToggle(row.id, !selected)} type="button">
            {selected ? "已选择" : "加入处置单"}
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
                <DetailMetric label="优化建议" count={proposalItemCount || proposalJobCount} active={detailView === "proposal"} onClick={() => onSelectDetailView("proposal")} />
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
    proposal: "优化建议详情",
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
          tasks={tasks}
          actionId={actionId}
          onMarkApplied={onMarkTaskApplied}
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
          <span>{jobErrorCode(job)} · {jobErrorMessage(job, "归因 Agent 输出未通过校验。")}</span>
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
          <p>归因 Agent 已返回内容，但没有通过 schema 校验，需要查看原始输出后重新归因或调整输出格式化策略。</p>
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
        <FormattedText value={jobErrorMessage(job, "归因 Agent 输出未通过校验。")} />
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
  const rawOutputTitle = output ? "归因输出" : latestJob?.raw_output_json ? "归因 Agent 原始输出" : "归因校验错误";

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
  const rawOutputTitle = output ? "建议输出" : latestJob?.raw_output_json ? "建议 Agent 原始输出" : "建议校验错误";
  const regenerationInstruction = rawString(latestJob?.input_json, "regeneration_instruction");

  return (
    <div className="fw-proposal-detail">
      <div className="fw-proposal-detail-meta">
        <div className="fw-proposal-detail-meta-main">
          <h4>{shortId(latestJob?.job_id || output?.proposal_job_id)} · {latestJob?.profile_name || "feedback-proposal"}</h4>
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
        label="优化建议详情视图"
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
            {!proposalCount && !hasUnvalidatedSuggestions && !noActionReason ? <div className="fw-empty-inline">暂无优化建议</div> : null}
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
  const title = rawString(item, "title") || rawString(item, "recommendation") || "未入库优化建议";
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
  tasks,
  actionId,
  onMarkApplied,
  onRunRegression,
}: {
  tasks: OptimizationTaskRecord[];
  actionId?: string | null;
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  return (
    <DetailRecordList hasItems={tasks.length > 0} emptyText="暂无优化任务">
      {tasks.map((task) => (
        <TaskDetailCard
          key={task.optimization_task_id}
          task={task}
          actionId={actionId || null}
          onMarkApplied={onMarkApplied}
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

function TaskDetailCard({
  task,
  actionId,
  onMarkApplied,
  onRunRegression,
}: {
  task: OptimizationTaskRecord;
  actionId?: string | null;
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  const proposal = task.proposal;
  const proposalId = taskProposalId(task);
  const targetPaths = task.target_paths || [];
  const latestRegression = task.latest_regression_run || null;
  const canMarkApplied = !task.applied_agent_version_id && ["pending_execution", "failed", "needs_human_review"].includes(task.status);
  const canRunRegression = Boolean(task.applied_agent_version_id) && task.status !== "regression_running";
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
          <h4>{proposal.title || "来源优化建议"}</h4>
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
      {latestRegression ? (
        <section className="fw-task-source">
          <h4>最近回归验证</h4>
          <DetailMetricGrid
            items={[
              ["eval_run", shortId(latestRegression.eval_run_id)],
              ["结果", latestRegression.result_status || latestRegression.status],
              ["通过", latestRegression.summary?.passed ?? 0],
              ["失败", latestRegression.summary?.failed ?? 0],
              ["需复核", latestRegression.summary?.needs_human_review ?? 0],
              ["完成时间", formatDate(latestRegression.completed_at)],
            ]}
          />
        </section>
      ) : null}
      <p className="fw-note-box fw-task-status-note">{taskStatusDescription(task.status)}</p>
      {onMarkApplied || onRunRegression ? (
        <div className="fw-detail-action-row">
          {onMarkApplied ? (
            <button
              className="fw-small-secondary"
              type="button"
              disabled={!canMarkApplied || actionId === `apply:${task.optimization_task_id}`}
              onClick={() => onMarkApplied(task)}
            >
              {actionId === `apply:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <GitBranch size={16} />}
              标记已应用并创建版本
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
              运行回归验证
            </button>
          ) : null}
        </div>
      ) : null}
    </article>
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
        <strong>优化建议审批</strong>
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
              <p className="fw-warning-text">该建议不能自动修改主 Agent workspace。</p>
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
          <p className="fw-warning-text">该建议不能自动修改主 Agent workspace。</p>
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
      {!proposals.length && !externalGuidance.length && !externalItemsAsGuidance.length ? <div className="fw-empty-inline">暂无优化建议</div> : null}
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
    kind: "event",
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
      kind: "pending",
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
  if (status === "failed") return "red";
  if (status === "needs_human_review") return "orange";
  if (status === "queued" || status === "running") return "blue";
  return "gray";
}

function evalStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "passed" || status === "completed") return "green";
  if (status === "failed") return "red";
  if (status === "needs_human_review") return "orange";
  if (status === "running") return "blue";
  return "gray";
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
  if (status === "applied_pending_regression") return "当前任务已确认应用并创建主 Agent 版本快照，等待手动回归验证。";
  if (status === "regression_running") return "当前任务正在运行回归验证。";
  if (status === "completed") return "当前任务已完成。";
  if (status === "failed") return "当前任务回归验证失败，需要继续修复或人工复核。";
  if (status === "needs_human_review") return "当前任务需要人工复核回归结果。";
  if (status === "closed") return "当前任务已关闭。";
  return "当前任务仅记录优化交接信息，具体执行状态以任务状态为准。";
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
