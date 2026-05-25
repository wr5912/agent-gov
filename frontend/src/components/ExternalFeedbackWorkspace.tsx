import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Copy,
  FileArchive,
  FileText,
  FolderKanban,
  GitBranch,
  Loader2,
  MessageSquare,
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
  createOptimizationTask,
  createProposalJob,
  getAttributionOutput,
  getEvidencePackage,
  getEvidencePackageFile,
  getFeedbackAnalysisJob,
  getFeedbackWorkbenchData,
  getProposalOutput,
  reviewOptimizationProposal,
  runtimeApi,
} from "../api/runtime";
import type {
  AttributionOutput,
  EvidencePackageFileRecord,
  EvidencePackageRecord,
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

type MenuKey = "signals" | "cases" | "proposals" | "tasks" | "versions";
type SourceKind = "signal" | "event" | "pending";
type CaseDetailView = "summary" | "evidence" | "attribution" | "proposal" | "runs" | "tasks";
type ProposalDetailTab = "proposals" | "raw" | "records";
type AttributionDetailTab = "result" | "raw" | "records";

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
};

const menuText: Record<MenuKey, string> = {
  signals: "反馈信息",
  cases: "反馈处置",
  proposals: "优化建议",
  tasks: "优化任务",
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
  const [caseDetails, setCaseDetails] = useState<CaseDetails>({});
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [runtimeStatus, setRuntimeStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [actionId, setActionId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

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

  function openTask(task: OptimizationTaskRecord) {
    if (task.feedback_case_id) {
      setSelectedCaseId(task.feedback_case_id);
      setCaseDetailView("tasks");
      setActiveMenu("cases");
      return;
    }
    setActiveMenu("tasks");
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
            details={caseDetails}
            detailView={caseDetailView}
            detailsLoading={detailsLoading}
            actionId={actionId}
            clientConfig={clientConfig}
            onSelectCase={(feedbackCase) => {
              setSelectedCaseId(feedbackCase.feedback_case_id);
              setCaseDetailView("summary");
            }}
            onSelectDetailView={setCaseDetailView}
            onCreateEvidence={() => runCaseAction("evidence")}
            onRunAttribution={() => runCaseAction("attribution")}
            onRunProposal={() => runCaseAction("proposal")}
            onReviewProposal={reviewProposal}
            onCreateTask={createTask}
            onOpenTask={openTask}
            tasksByProposalId={tasksByProposalId}
          />
        ) : null}

        {activeMenu === "proposals" ? (
          <ProposalsPanel
            proposals={data.proposals}
            actionId={actionId}
            onReviewProposal={reviewProposal}
            onCreateTask={createTask}
            onOpenTask={openTask}
            tasksByProposalId={tasksByProposalId}
          />
        ) : null}

        {activeMenu === "tasks" ? (
          <TasksPanel tasks={data.tasks} />
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
            <span>{"当前链路：feedback signal / SOC event -> feedback case -> evidence package -> attribution job -> proposal job -> approval -> optimization task。"}</span>
            {monitoringConfig?.langfuseUrl ? <a href={monitoringConfig.langfuseUrl} target="_blank" rel="noreferrer">Langfuse</a> : null}
          </footer>
        ) : null}
      </div>

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
  details,
  detailView,
  detailsLoading,
  actionId,
  clientConfig,
  onSelectCase,
  onSelectDetailView,
  onCreateEvidence,
  onRunAttribution,
  onRunProposal,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
  tasksByProposalId,
}: {
  cases: FeedbackCaseRecord[];
  selectedCase: FeedbackCaseRecord | null;
  selectedCaseRuns: FeedbackRunRecord[];
  selectedCaseProposals: OptimizationProposalRecord[];
  selectedCaseTasks: OptimizationTaskRecord[];
  details: CaseDetails;
  detailView: CaseDetailView;
  detailsLoading: boolean;
  actionId: string | null;
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  onSelectCase: (feedbackCase: FeedbackCaseRecord) => void;
  onSelectDetailView: (view: CaseDetailView) => void;
  onCreateEvidence: () => void;
  onRunAttribution: () => void;
  onRunProposal: () => void;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const evidenceCount = selectedCase?.evidence_package_ids.length || 0;
  const attributionCount = selectedCase?.attribution_job_ids.length || 0;
  const proposalJobCount = selectedCase?.proposal_job_ids.length || 0;
  const attributionLocked = attributionCount > 0 && !isRetryableJobStatus(details.attributionJob?.status);
  const proposalLocked = proposalJobCount > 0 && !isRetryableJobStatus(details.proposalJob?.status);
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
                <DetailMetric label="优化建议" count={proposalJobCount} active={detailView === "proposal"} onClick={() => onSelectDetailView("proposal")} />
                <DetailMetric label="关联运行" count={selectedCase.run_ids.length} active={detailView === "runs"} onClick={() => onSelectDetailView("runs")} />
                <DetailMetric label="优化任务" count={selectedCaseTasks.length} active={detailView === "tasks"} onClick={() => onSelectDetailView("tasks")} />
              </div>
              <div className="fw-current-case-actions">
                <button className="fw-small-secondary" type="button" onClick={onCreateEvidence} disabled={actionRunning || evidenceCount > 0}>
                  {actionId?.startsWith("evidence:") ? <Loader2 size={16} className="fw-spin" /> : <FileArchive size={16} />}
                  {evidenceCount > 0 ? "证据包已生成" : "生成证据包"}
                </button>
                <button className="fw-small-secondary" type="button" onClick={onRunAttribution} disabled={actionRunning || attributionLocked}>
                  {actionId?.startsWith("attribution:") ? <Loader2 size={16} className="fw-spin" /> : <ShieldCheck size={16} />}
                  {details.attributionJob?.status === "failed" ? "重试归因" : attributionCount > 0 ? "归因已启动" : "启动归因"}
                </button>
                <button className="fw-small-primary" type="button" onClick={onRunProposal} disabled={actionRunning || proposalLocked || !details.attribution}>
                  {actionId?.startsWith("proposal:") ? <Loader2 size={16} className="fw-spin" /> : <MessageSquare size={16} />}
                  {details.proposalJob?.status === "failed" ? "重试建议" : proposalJobCount > 0 ? "建议已生成" : "生成建议"}
                </button>
              </div>
            </section>

            <CaseDetailPanel
              actionId={actionId}
              clientConfig={clientConfig}
              detailView={detailView}
              details={details}
              detailsLoading={detailsLoading}
              onSelectDetailView={onSelectDetailView}
              onCreateTask={onCreateTask}
              onOpenTask={onOpenTask}
              onReviewProposal={onReviewProposal}
              runs={selectedCaseRuns}
              tasks={selectedCaseTasks}
              tasksByProposalId={tasksByProposalId}
              proposals={selectedCaseProposals}
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
  clientConfig,
  detailView,
  details,
  detailsLoading,
  runs,
  proposals,
  tasks,
  onSelectDetailView,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
  tasksByProposalId,
}: {
  actionId: string | null;
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  detailView: CaseDetailView;
  details: CaseDetails;
  detailsLoading: boolean;
  runs: FeedbackRunRecord[];
  proposals: OptimizationProposalRecord[];
  tasks: OptimizationTaskRecord[];
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
  onSelectDetailView: (view: CaseDetailView) => void;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
}) {
  const [copiedProposalJobId, setCopiedProposalJobId] = useState(false);
  const titleByView: Record<CaseDetailView, string> = {
    summary: "处置摘要",
    evidence: "证据包详情",
    attribution: "归因分析详情",
    proposal: "优化建议详情",
    runs: "关联运行详情",
    tasks: "优化任务详情",
  };
  const proposalJobId = latestItem(details.proposalJobs)?.job_id || details.proposal?.proposal_job_id || null;

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
        {detailView === "proposal" ? (
          <div className="fw-panel-header-actions">
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
      {detailView === "attribution" ? <AttributionDetails jobs={details.attributionJobs || []} output={details.attribution} /> : null}
      {detailView === "proposal" ? (
        <ProposalDetails
          actionId={actionId}
          jobs={details.proposalJobs || []}
          output={details.proposal}
          proposals={proposals}
          onCreateTask={onCreateTask}
          onOpenTask={onOpenTask}
          onReviewProposal={onReviewProposal}
          onSelectDetailView={onSelectDetailView}
          tasksByProposalId={tasksByProposalId}
        />
      ) : null}
      {detailView === "runs" ? <RunsDetails runs={runs} /> : null}
      {detailView === "tasks" ? <TasksDetails tasks={tasks} /> : null}
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
      {details.attribution ? <p className="fw-note-box">{details.attribution.rationale}</p> : <div className="fw-empty-inline">暂无已校验归因输出</div>}
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
              <span>{job.error_json.message || "分析 job 执行失败"}</span>
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

function AttributionDetails({ jobs, output }: { jobs: FeedbackAnalysisJobRecord[]; output?: AttributionOutput | null }) {
  const [activeTab, setActiveTab] = useState<AttributionDetailTab>("result");
  const latestJob = latestItem(jobs);
  const rawOutput = output || latestJob?.raw_output_json || latestJob?.error_json || null;
  const rawOutputTitle = output ? "归因输出" : latestJob?.raw_output_json ? "归因 Agent 原始输出" : "归因校验错误";

  return (
    <div className="fw-detail-tabbed">
      <DetailTabs
        active={activeTab}
        label="归因分析详情视图"
        onChange={setActiveTab}
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
    <div className="fw-detail-result">
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
        <p className="fw-note-box fw-detail-rationale">{output.rationale || "暂无归因说明"}</p>
      </section>
      <div className="fw-detail-info-grid">
        <article>
          <h4>责任边界</h4>
          <p>{output.responsibility_boundary?.owner || "-"}：{output.responsibility_boundary?.reason || "-"}</p>
        </article>
        <article>
          <h4>引用证据</h4>
          {output.evidence_refs?.length ? (
            <ul>
              {output.evidence_refs.map((ref, index) => (
                <li key={`${ref.type}:${ref.id}:${index}`}>{ref.type} / {shortId(ref.id)}：{ref.reason}</li>
              ))}
            </ul>
          ) : (
            <p>暂无引用证据</p>
          )}
        </article>
      </div>
    </div>
  );
}

function ProposalDetails({
  jobs,
  output,
  proposals,
  actionId,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
  onSelectDetailView,
  tasksByProposalId,
}: {
  jobs: FeedbackAnalysisJobRecord[];
  output?: ProposalOutput | null;
  proposals: OptimizationProposalRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  onSelectDetailView: (view: CaseDetailView) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const [activeTab, setActiveTab] = useState<ProposalDetailTab>("proposals");
  const latestJob = latestItem(jobs);
  const externalGuidance = output?.external_guidance || [];
  const proposalCount = proposals.length + externalGuidance.length;
  const rawOutput = output || latestJob?.raw_output_json || latestJob?.error_json || null;
  const rawOutputTitle = output ? "建议输出" : latestJob?.raw_output_json ? "建议 Agent 原始输出" : "建议校验错误";

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
      </div>

      <DetailTabs
        active={activeTab}
        label="优化建议详情视图"
        onChange={setActiveTab}
        tabs={[
          { key: "proposals", label: `建议(${proposalCount})` },
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
              <article className="fw-proposal-card fw-proposal-detail-card" key={`${item.owner}:${index}`}>
                <div className="fw-proposal-detail-title">
                  <Pill tone="gray">{item.actionability}</Pill>
                  <h4>{item.owner}</h4>
                  <small>external_guidance</small>
                </div>
                <p>{item.recommendation}</p>
                {item.reason ? <p className="fw-warning-text">{item.reason}</p> : null}
              </article>
            ))}
            {!proposalCount ? <div className="fw-empty-inline">暂无优化建议</div> : null}
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
      <p>{proposal.recommendation}</p>
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

function RunsDetails({ runs }: { runs: FeedbackRunRecord[] }) {
  return (
    <DetailRecordList hasItems={runs.length > 0} emptyText="暂无关联运行">
      {runs.map((run) => (
        <article key={run.run_id}>
          <div className="fw-detail-record-head">
            <h4>{shortId(run.run_id)} · {shortId(run.agent_version_id)}</h4>
            <Pill tone="blue">run</Pill>
          </div>
          <p>{run.answer_summary || run.message || "-"}</p>
          <small>session：{shortId(run.session_id)} · tools：{run.agent_activity?.tool_names?.join(", ") || "-"}</small>
        </article>
      ))}
    </DetailRecordList>
  );
}

function TasksDetails({ tasks }: { tasks: OptimizationTaskRecord[] }) {
  return (
    <DetailRecordList hasItems={tasks.length > 0} emptyText="暂无优化任务">
      {tasks.map((task) => (
        <TaskDetailCard key={task.optimization_task_id} task={task} />
      ))}
    </DetailRecordList>
  );
}

function TaskDetailCard({ task }: { task: OptimizationTaskRecord }) {
  const proposal = task.proposal;
  const proposalId = taskProposalId(task);
  const targetPaths = task.target_paths || [];
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
          <p>{proposal.recommendation || "-"}</p>
          <DetailMetricGrid
            items={[
              ["预期效果", proposal.expected_effect || "-"],
              ["验证方式", proposal.validation || "-"],
              ["风险", proposal.risk || "-"],
              ["审批状态", proposalStatusText[proposal.status] || proposal.status],
            ]}
          />
        </section>
      ) : null}
      <p className="fw-note-box fw-task-status-note">{taskStatusDescription(task.status)}</p>
    </article>
  );
}

function ProposalsPanel({
  proposals,
  actionId,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
  tasksByProposalId,
}: {
  proposals: OptimizationProposalRecord[];
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  return (
    <section className="fw-panel fw-proposal-panel">
      <div className="fw-panel-header">
        <strong>优化建议审批</strong>
        <span className="fw-muted">{proposals.length} 条</span>
      </div>
      <ProposalList
        proposals={proposals}
        actionId={actionId}
        onReviewProposal={onReviewProposal}
        onCreateTask={onCreateTask}
        onOpenTask={onOpenTask}
        tasksByProposalId={tasksByProposalId}
      />
    </section>
  );
}

function ProposalList({
  proposals,
  proposalOutput,
  actionId,
  onReviewProposal,
  onCreateTask,
  onOpenTask,
  tasksByProposalId,
}: {
  proposals: OptimizationProposalRecord[];
  proposalOutput?: ProposalOutput | null;
  actionId: string | null;
  onReviewProposal: (proposalId: string, action: OptimizationProposalReviewAction) => void;
  onCreateTask: (proposal: OptimizationProposalRecord) => void;
  onOpenTask: (task: OptimizationTaskRecord) => void;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
}) {
  const externalGuidance = proposalOutput?.external_guidance || [];
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
            <p>{proposal.recommendation}</p>
            <DetailMetricGrid
              items={[
                ["预期效果", proposal.expected_effect || "-"],
                ["验证方式", proposal.validation || "-"],
                ["风险", proposal.risk || "-"],
                ["base_version", shortId(proposal.base_agent_version_id)],
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
          <p>{item.recommendation}</p>
          <p className="fw-warning-text">该建议不能自动修改主 Agent workspace。</p>
          {item.reason ? <small>{item.reason}</small> : null}
        </article>
      ))}
      {!proposals.length && !externalGuidance.length ? <div className="fw-empty-inline">暂无优化建议</div> : null}
    </div>
  );
}

function TasksPanel({ tasks }: { tasks: OptimizationTaskRecord[] }) {
  return (
    <section className="fw-panel fw-task-panel">
      <div className="fw-panel-header">
        <strong>优化任务</strong>
        <span className="fw-muted">{tasks.length} 个</span>
      </div>
      <div className="fw-proposal-list fw-task-list">
        {tasks.map((task) => (
          <TaskDetailCard key={task.optimization_task_id} task={task} />
        ))}
        {!tasks.length ? <div className="fw-empty-inline">暂无优化任务</div> : null}
      </div>
    </section>
  );
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

function isRetryableJobStatus(status?: string | null): boolean {
  return status === "failed" || status === "needs_human_review";
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

function jobStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "completed") return "green";
  if (status === "failed") return "red";
  if (status === "needs_human_review") return "orange";
  if (status === "queued" || status === "running") return "blue";
  return "gray";
}

function proposalStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" | "purple" {
  if (status === "approved") return "green";
  if (status === "rejected") return "red";
  if (status === "needs_more_analysis") return "purple";
  if (status === "pending_review") return "orange";
  return "gray";
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
  if (status === "executing") return "当前任务正在执行中。";
  if (status === "completed") return "当前任务已完成。";
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
