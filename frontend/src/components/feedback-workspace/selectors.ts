import type { PillTone } from "./common";
import { formatDate, shortId } from "../../utils/format";
import { isRecord } from "../../utils/records";
import type {
  AttributionOutput,
  EvalCaseRecord,
  EvalRunRecord,
  ExternalGovernanceItemRecord,
  FeedbackAnalysisJobRecord,
  FeedbackCaseRecord,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackSignalRecord,
  FeedbackSourceKind,
  FeedbackSourceRecord,
  FeedbackWorkbenchData,
  OptimizationProposalRecord,
  OptimizationProposalReviewAction,
  OptimizationTaskRecord,
  PendingCorrelationRecord,
  SocEventRecord,
} from "../../types/feedback";

type Tone = PillTone;
export { formatDate, shortId };

export type BatchDetailView = "feedback" | "attribution" | "plan" | "regression";

export const proposalStatusText: Record<string, string> = {
  pending_review: "待审批",
  approved: "已批准",
  rejected: "已拒绝",
  needs_more_analysis: "需补充分析",
  superseded: "已废弃",
};

export interface EvalCaseEditDraft {
  prompt: string;
  expectedBehavior: string;
  labelsText: string;
  status: "active" | "draft" | "archived";
  checksText: string;
}

export interface SourceRow {
  id: string;
  kind: FeedbackSourceKind;
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

export const sourceKindText: Record<FeedbackSourceKind, string> = {
  signal: "Feedback signal",
  soc_event: "SOC event",
  pending_correlation: "待关联",
};

export function sourceKindTone(kind: FeedbackSourceKind): Tone {
  if (kind === "pending_correlation") return "orange";
  if (kind === "soc_event") return "green";
  return "blue";
}

export function buildSourceRows(data: FeedbackWorkbenchData): SourceRow[] {
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

export function buildBatchSourceRows(batch: FeedbackOptimizationBatchRecord | null, sources: FeedbackSourceRecord[]): SourceRow[] {
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

export function buildBatchAttributionJobs(batch: FeedbackOptimizationBatchRecord | null): FeedbackAnalysisJobRecord[] {
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

export function attributionOutputFromJob(job: FeedbackAnalysisJobRecord): AttributionOutput | null {
  const output = job.validated_output_json || job.raw_output_json;
  if (!output || typeof output !== "object" || Array.isArray(output)) return null;
  if ((output as Record<string, unknown>).schema_version !== "attribution-output/v1") return null;
  return output as AttributionOutput;
}

export function defaultBatchDetail(batch: FeedbackOptimizationBatchRecord | null): BatchDetailView {
  if (!batch) return "feedback";
  if (batch.latest_eval_run) return "regression";
  if (batch.eval_case_ids?.length) return "regression";
  if (batch.optimization_plan || batch.optimization_task || batch.execution_job) return "plan";
  if (batch.attribution_jobs?.length || batch.attribution_job_ids?.length) return "attribution";
  return "feedback";
}

export function attributionStatusText(jobs: FeedbackAnalysisJobRecord[], total: number): string {
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

export function attributionStatusTone(jobs: FeedbackAnalysisJobRecord[], total: number): Tone {
  if (!total) return "gray";
  if (jobs.some((job) => job.status === "failed" || job.status === "timeout")) return "red";
  if (jobs.some((job) => job.status === "needs_human_review")) return "orange";
  if (jobs.some((job) => ["created", "queued", "running", "schema_validating", "evidence_packaging"].includes(String(job.status)))) return "blue";
  if (jobs.filter((job) => job.status === "completed").length === total) return "green";
  return "gray";
}

export function batchRegressionStatusText(batch: FeedbackOptimizationBatchRecord): string {
  const run = batch.latest_eval_run;
  if (!run) return batch.eval_case_ids?.length ? `用例 ${batch.eval_case_ids.length}` : "未运行";
  const total = run.summary?.total ?? run.items?.length ?? 0;
  const passed = run.summary?.passed ?? 0;
  const failed = run.summary?.failed ?? 0;
  const review = run.summary?.needs_human_review ?? 0;
  if (total) return `${run.result_status || run.status} · ${passed}/${total} 通过`;
  if (failed) return `${run.result_status || run.status} · ${failed} 失败`;
  if (review) return `${run.result_status || run.status} · ${review} 复核`;
  return run.result_status || run.status || "已运行";
}

export function sourceRowKey(row: SourceRow): string {
  return `${row.kind}:${row.id}`;
}

export function filterSourceRows(rows: SourceRow[], query: string): SourceRow[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return rows;
  return rows.filter((row) => JSON.stringify(row.raw, null, 0).toLowerCase().includes(normalized));
}

export function filterCases(cases: FeedbackCaseRecord[], query: string): FeedbackCaseRecord[] {
  const normalized = query.trim().toLowerCase();
  const sorted = [...cases].sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  if (!normalized) return sorted;
  return sorted.filter((item) => JSON.stringify(item, null, 0).toLowerCase().includes(normalized));
}

export function filterBatches(batches: FeedbackOptimizationBatchRecord[], query: string): FeedbackOptimizationBatchRecord[] {
  const normalized = query.trim().toLowerCase();
  const sorted = [...batches].sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  if (!normalized) return sorted;
  return sorted.filter((item) => JSON.stringify(item, null, 0).toLowerCase().includes(normalized));
}

export function filterEvalCases(evalCases: EvalCaseRecord[], query: string): EvalCaseRecord[] {
  const normalized = query.trim().toLowerCase();
  const sorted = [...evalCases].sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  if (!normalized) return sorted;
  return sorted.filter((item) => JSON.stringify(item, null, 0).toLowerCase().includes(normalized));
}

export function latest(values?: string[]): string | undefined {
  if (!Array.isArray(values) || !values.length) return undefined;
  return values[values.length - 1];
}

export function latestItem<T>(values?: T[]): T | null {
  if (!Array.isArray(values) || !values.length) return null;
  return values[values.length - 1];
}

export function rawRecordArray(value: unknown, key: string): Array<Record<string, unknown>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const items = (value as Record<string, unknown>)[key];
  return Array.isArray(items) ? items.filter(isRecord) : [];
}

export function rawString(value: unknown, key: string): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const item = (value as Record<string, unknown>)[key];
  return typeof item === "string" ? item : "";
}

export function jobErrorCode(job?: FeedbackAnalysisJobRecord | null): string {
  return job?.error_json?.error_code || (job?.status === "failed" ? "JOB_FAILED" : "JOB_NEEDS_REVIEW");
}

export function jobErrorMessage(job: FeedbackAnalysisJobRecord | null | undefined, fallback: string): string {
  return job?.error_json?.message || fallback;
}

export function validationErrorItems(job?: FeedbackAnalysisJobRecord | null): Array<Record<string, unknown>> {
  const errors = job?.error_json?.validation_errors;
  if (!Array.isArray(errors)) return [];
  return errors.filter((item): item is Record<string, unknown> => isRecord(item));
}

export function validationErrorCount(job?: FeedbackAnalysisJobRecord | null): number {
  return validationErrorItems(job).length;
}

export function validationFieldSummary(job?: FeedbackAnalysisJobRecord | null): string {
  const fields = validationErrorItems(job)
    .map(validationErrorPath)
    .filter((item) => item && item !== "-")
    .slice(0, 3);
  if (!fields.length) return "";
  const suffix = validationErrorCount(job) > fields.length ? " 等" : "";
  return `：${fields.join("、")}${suffix}`;
}

export function validationErrorPath(error: Record<string, unknown>): string {
  const loc = error.loc;
  if (Array.isArray(loc)) return loc.map((item) => String(item)).join(".");
  if (typeof loc === "string") return loc;
  return "-";
}

export function validationErrorMessage(error: Record<string, unknown>): string {
  return typeof error.msg === "string" ? error.msg : "校验失败";
}

export function isRetryableJobStatus(status?: string | null): boolean {
  return status === "failed";
}

export function analysisActionLabel(kind: "attribution" | "proposal", status: string | null | undefined, count: number): string {
  const noun = kind === "attribution" ? "归因" : "建议";
  if (status === "failed") return `重试${noun}`;
  if (status === "needs_human_review") return `${noun}需复核`;
  if (status === "queued" || status === "running" || status === "schema_validating") {
    return kind === "attribution" ? "归因执行中" : "建议生成中";
  }
  if (count > 0) return kind === "attribution" ? "归因已启动" : "建议已生成";
  return kind === "attribution" ? "启动归因" : "生成建议";
}

export function evidenceFileName(item: Record<string, unknown>): string | null {
  return typeof item.path === "string" ? item.path : null;
}

export function firstEvidenceFileName(items?: Array<Record<string, unknown>>): string | undefined {
  for (const item of items || []) {
    const fileName = evidenceFileName(item);
    if (fileName) return fileName;
  }
  return undefined;
}

export function traceRefsFromContent(content: unknown): Array<{ traceId: string; url: string }> {
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

export function summaryText(value: Record<string, unknown>): string {
  const comment = typeof value.comment === "string" ? value.comment : "";
  if (comment) return comment.slice(0, 120);
  const reason = typeof value.reason === "string" ? value.reason : "";
  if (reason) return reason;
  return JSON.stringify(value).slice(0, 120);
}

export function evalItemSummary(item: NonNullable<EvalRunRecord["items"]>[number]): string {
  if (item.answer_summary) return item.answer_summary;
  const message = item.error_json?.message;
  return typeof message === "string" ? message : "-";
}

export function latestEvalRunItemForCase(evalRuns: EvalRunRecord[], evalCaseId: string): NonNullable<EvalRunRecord["items"]>[number] | undefined {
  for (const run of evalRuns) {
    const item = run.items?.find((candidate) => candidate.eval_case_id === evalCaseId);
    if (item) return item;
  }
  return undefined;
}

export function evalCaseEditDraft(evalCase: EvalCaseRecord): EvalCaseEditDraft {
  const status = evalCase.status === "draft" || evalCase.status === "archived" ? evalCase.status : "active";
  return {
    prompt: evalCase.prompt || "",
    expectedBehavior: evalCase.expected_behavior || "",
    labelsText: (evalCase.labels || []).join(", "),
    status,
    checksText: JSON.stringify(evalCase.checks_json || {}, null, 2),
  };
}

export function parseEvalCaseLabels(value: string): string[] {
  const seen = new Set<string>();
  const labels: string[] = [];
  for (const label of value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean)) {
    if (seen.has(label)) continue;
    seen.add(label);
    labels.push(label);
  }
  return labels;
}

export function jobStatusTone(status?: string | null): Tone {
  if (status === "completed") return "green";
  if (status === "failed" || status === "execution_failed") return "red";
  if (status === "needs_human_review" || status === "execution_ready" || status === "ready") return "orange";
  if (status === "queued" || status === "running" || status === "execution_planning") return "blue";
  return "gray";
}

export function profileDisplayName(profileName?: string | null): string {
  if (profileName === "main-agent") return "主智能体";
  if (profileName === "attribution-analyzer") return "归因分析智能体";
  if (profileName === "proposal-generator") return "优化方案生成智能体";
  if (profileName === "execution-optimizer") return "执行优化智能体";
  if (profileName === "eval-case-governor") return "用例治理智能体";
  if (profileName === "regression-impact-analyzer") return "回归影响分析智能体";
  return profileName || "-";
}

export function evalStatusTone(status?: string | null): Tone {
  if (status === "passed" || status === "completed" || status === "passed_with_notes") return "green";
  if (status === "failed" || status === "blocked") return "red";
  if (status === "needs_human_review" || status === "review_required") return "orange";
  if (status === "running") return "blue";
  return "gray";
}

export function batchStatusTone(status?: string | null): Tone {
  if (status === "completed" || status === "applied_pending_regression" || status === "passed") return "green";
  if (status === "failed" || status === "rejected" || status === "execution_failed") return "red";
  if (status === "pending_approval" || status === "needs_human_review" || status === "execution_ready") return "orange";
  if (status === "draft" || status === "attribution_running" || status === "execution_planning" || status === "regression_running") return "blue";
  return "gray";
}

export function fileStatusTone(status?: string | null): Tone {
  if (status === "modified") return "orange";
  if (status === "added") return "green";
  if (status === "deleted") return "red";
  if (status === "unchanged") return "gray";
  return "blue";
}

export function fileStatusText(status?: string | null): string {
  if (status === "modified") return "已修改";
  if (status === "added") return "新增";
  if (status === "deleted") return "删除";
  if (status === "unchanged") return "未变化";
  if (status === "missing") return "未纳入快照";
  if (status === "binary_or_too_large") return "不可预览";
  return status || "未知";
}

export function proposalStatusTone(status?: string | null): Tone {
  if (status === "approved") return "green";
  if (status === "rejected") return "red";
  if (status === "needs_more_analysis") return "purple";
  if (status === "pending_review") return "orange";
  return "gray";
}

export function externalGovernanceTone(status?: string | null): Tone {
  if (status === "notified") return "green";
  if (status === "notification_failed") return "red";
  if (status === "pending_notification") return "orange";
  return "gray";
}

export function planTaskTone(task: FeedbackOptimizationPlanTaskRecord): Tone {
  if (task.applied_agent_version_id || task.status === "notified") return "green";
  if (task.status === "failed" || task.status === "execution_failed" || task.status === "notification_failed") return "red";
  if (task.status === "needs_human_review" || task.status === "pending_notification") return "orange";
  if (task.status === "pending_execution" || task.status === "execution_planning" || task.status === "queued" || task.status === "running") return "blue";
  return "gray";
}

export function findExternalGovernanceItem(
  items: ExternalGovernanceItemRecord[],
  guidance: { external_item_id?: string | null; source_index?: number | null },
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

export function buildTaskByProposalId(tasks: OptimizationTaskRecord[]): Map<string, OptimizationTaskRecord> {
  const tasksByProposalId = new Map<string, OptimizationTaskRecord>();
  for (const task of tasks) {
    const proposalId = taskProposalId(task);
    if (proposalId && !tasksByProposalId.has(proposalId)) {
      tasksByProposalId.set(proposalId, task);
    }
  }
  return tasksByProposalId;
}

export function taskProposalId(task: OptimizationTaskRecord): string | null {
  return task.proposal_id || task.proposal_ids?.[0] || task.proposal?.proposal_id || null;
}

export function taskStatusDescription(status?: string | null): string {
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

export function fileStatusFromDiff(diff: unknown, targetPath: string): string {
  if (!diff || typeof diff !== "object") return "unknown";
  const archivePath = toArchivePath(targetPath);
  const record = diff as { added?: Array<Record<string, unknown>>; modified?: Array<Record<string, unknown>>; deleted?: Array<Record<string, unknown>> };
  if ((record.added || []).some((item) => rawString(item, "path") === archivePath)) return "added";
  if ((record.deleted || []).some((item) => rawString(item, "path") === archivePath)) return "deleted";
  if ((record.modified || []).some((item) => rawString(item, "path") === archivePath)) return "modified";
  return "unchanged";
}

export function changedPathsFromDiff(diff: unknown): string[] {
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

export function proposalEvidenceText(proposal: OptimizationProposalRecord): string {
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

export function reviewComment(action: OptimizationProposalReviewAction): string {
  if (action === "approve") return "Feedback 工作台批准该 optimization proposal。";
  if (action === "reject") return "Feedback 工作台拒绝该 optimization proposal。";
  return "Feedback 工作台要求补充分析。";
}
