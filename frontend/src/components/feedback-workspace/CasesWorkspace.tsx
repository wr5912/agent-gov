import { useEffect, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Copy,
  Database,
  FileArchive,
  FileText,
  FolderKanban,
  Loader2,
  MessageSquare,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  X,
} from "lucide-react";
import { DetailMetric, FormattedText, Metric, Pill } from "./common";
import {
  analysisActionLabel,
  formatDate,
  isRetryableJobStatus,
  jobErrorCode,
  jobErrorMessage,
  latestItem,
  shortId,
  validationErrorCount,
  validationFieldSummary,
} from "./selectors";
import type {
  AttributionOutput,
  EvalCaseRecord,
  EvidencePackageRecord,
  ExternalGovernanceItemRecord,
  FeedbackAnalysisJobRecord,
  FeedbackCaseRecord,
  OptimizationProposalRecord,
  OptimizationTaskRecord,
  ProposalOutput,
} from "../../types/feedback";

export type CaseDetailView = "summary" | "evidence" | "attribution" | "proposal" | "runs" | "tasks" | "evals";
export type AttributionDetailTab = "result" | "raw" | "records";

export interface CaseDetails {
  evidence?: EvidencePackageRecord | null;
  evidencePackages?: EvidencePackageRecord[];
  attributionJob?: FeedbackAnalysisJobRecord | null;
  attributionJobs?: FeedbackAnalysisJobRecord[];
  proposalJob?: FeedbackAnalysisJobRecord | null;
  proposalJobs?: FeedbackAnalysisJobRecord[];
  attribution?: AttributionOutput | null;
  proposal?: ProposalOutput | null;
}

const caseStatusText: Record<string, string> = {
  pending_evidence: "待生成证据包",
  pending_attribution: "待归因",
  attribution_queued: "归因排队",
  pending_proposal: "待生成建议",
  proposal_queued: "建议排队",
  pending_review: "待审批建议",
  needs_human_review: "需人工复核",
};

export function CasesPanel({
  actionId,
  cases,
  details,
  detailsLoading,
  detailView,
  selectedCase,
  selectedCaseEvalCases,
  selectedCaseExternalItems,
  selectedCaseProposals,
  selectedCaseTasks,
  onCreateEvidence,
  onOpenAttributionTab,
  onRegenerateAttribution,
  onRegenerateProposal,
  onRevalidateProposalJob,
  onRunAttribution,
  onRunProposal,
  onSelectCase,
  onSelectDetailView,
  renderDetailContent,
}: {
  actionId: string | null;
  cases: FeedbackCaseRecord[];
  details: CaseDetails;
  detailsLoading: boolean;
  detailView: CaseDetailView;
  selectedCase: FeedbackCaseRecord | null;
  selectedCaseEvalCases: EvalCaseRecord[];
  selectedCaseExternalItems: ExternalGovernanceItemRecord[];
  selectedCaseProposals: OptimizationProposalRecord[];
  selectedCaseTasks: OptimizationTaskRecord[];
  onCreateEvidence: () => void;
  onOpenAttributionTab: (tab: AttributionDetailTab) => void;
  onRegenerateAttribution: (feedbackCaseId: string) => void;
  onRegenerateProposal: (feedbackCaseId: string) => void;
  onRevalidateProposalJob: (jobId: string) => void;
  onRunAttribution: () => void;
  onRunProposal: () => void;
  onSelectCase: (feedbackCase: FeedbackCaseRecord) => void;
  onSelectDetailView: (view: CaseDetailView) => void;
  renderDetailContent: (view: CaseDetailView) => ReactNode;
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
              detailView={detailView}
              details={details}
              detailsLoading={detailsLoading}
              onRegenerateAttribution={onRegenerateAttribution}
              onRegenerateProposal={onRegenerateProposal}
              onRevalidateProposalJob={onRevalidateProposalJob}
              onSelectDetailView={onSelectDetailView}
              renderDetailContent={renderDetailContent}
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
  detailView,
  details,
  detailsLoading,
  onRegenerateAttribution,
  onRegenerateProposal,
  onRevalidateProposalJob,
  onSelectDetailView,
  renderDetailContent,
}: {
  actionId: string | null;
  detailView: CaseDetailView;
  details: CaseDetails;
  detailsLoading: boolean;
  onRegenerateAttribution: (feedbackCaseId: string) => void;
  onRegenerateProposal: (feedbackCaseId: string) => void;
  onRevalidateProposalJob: (jobId: string) => void;
  onSelectDetailView: (view: CaseDetailView) => void;
  renderDetailContent: (view: CaseDetailView) => ReactNode;
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
      {renderDetailContent(detailView)}
    </section>
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
