import type { ReactNode } from "react";
import { CheckCircle2, FileText, Loader2, XCircle } from "lucide-react";
import type { PillTone } from "./common";
import { formatDate, jobStatusTone, shortId } from "./selectors";
import type { AgentJobRecord, FeedbackOptimizationBatchRecord } from "../../types/feedback";

const EVAL_CASE_GENERATION_ACTIVE_STATUSES = new Set(["created", "queued", "running", "schema_validating", "evidence_packaging"]);

export interface EvalCaseGenerationState {
  active: boolean;
  detail: string;
  generatedCount: number;
  job: AgentJobRecord | null;
  jobId: string;
  label: string;
  status: string;
  title: string;
  tone: PillTone;
}

export function BatchEvalCaseGenerationIcon({ state }: { state: EvalCaseGenerationState }) {
  return batchEvalCaseGenerationIcon(state);
}

export function batchEvalCaseGenerationState(batch: FeedbackOptimizationBatchRecord): EvalCaseGenerationState | null {
  const job = batch.eval_case_generation_job || null;
  const jobId = String(job?.job_id || batch.eval_case_generation_job_id || "");
  if (!jobId) return null;
  const generatedCount = Math.max(batch.eval_case_ids?.length || 0, batch.eval_case_generation?.eval_cases?.length || 0);
  const status = String(job?.status || (generatedCount ? "completed" : "created"));
  const active = EVAL_CASE_GENERATION_ACTIVE_STATUSES.has(status);
  const tone = batchEvalCaseGenerationTone(status, generatedCount);
  const label = batchEvalCaseGenerationLabel(status, generatedCount);
  const errorMessage = batchEvalCaseGenerationErrorMessage(job);
  const detailParts = [`job ${shortId(jobId)}`];
  if (generatedCount) detailParts.push(`当前 ${generatedCount} 个用例`);
  if (job?.completed_at) {
    detailParts.push(`完成 ${formatDate(job.completed_at)}`);
  } else if (job?.updated_at) {
    detailParts.push(`更新 ${formatDate(String(job.updated_at))}`);
  }
  if (errorMessage) detailParts.push(errorMessage);
  return {
    active,
    detail: detailParts.join(" · "),
    generatedCount,
    job,
    jobId,
    label,
    status,
    title: batchEvalCaseGenerationTitle(status),
    tone,
  };
}

function batchEvalCaseGenerationIcon(state: EvalCaseGenerationState): ReactNode {
  if (state.active) return <Loader2 size={17} className="fw-spin" />;
  if (state.status === "completed" || state.generatedCount) return <CheckCircle2 size={17} />;
  if (state.tone === "red") return <XCircle size={17} />;
  return <FileText size={17} />;
}

function batchEvalCaseGenerationTone(status: string, generatedCount: number): PillTone {
  if (status === "timeout" || status === "failed") return "red";
  if (EVAL_CASE_GENERATION_ACTIVE_STATUSES.has(status)) return "blue";
  if (generatedCount) return "green";
  return jobStatusTone(status);
}

function batchEvalCaseGenerationLabel(status: string, generatedCount: number): string {
  if (status === "completed") return "已完成";
  if (status === "failed") return "失败";
  if (status === "timeout") return "超时";
  if (status === "needs_human_review") return "需复核";
  if (status === "queued" || status === "created") return "排队中";
  if (EVAL_CASE_GENERATION_ACTIVE_STATUSES.has(status)) return "生成中";
  if (generatedCount) return "已生成";
  return status || "未知";
}

function batchEvalCaseGenerationTitle(status: string): string {
  if (status === "completed") return "回归用例生成已完成";
  if (status === "failed" || status === "timeout") return "回归用例生成失败";
  if (status === "needs_human_review") return "回归用例生成需复核";
  if (EVAL_CASE_GENERATION_ACTIVE_STATUSES.has(status)) return "回归用例后台生成中";
  return "回归用例生成任务";
}

function batchEvalCaseGenerationErrorMessage(job: AgentJobRecord | null): string {
  const error = job?.error_json;
  if (!error || typeof error !== "object" || Array.isArray(error)) return "";
  const message = (error as Record<string, unknown>).message;
  return typeof message === "string" ? message : "";
}
