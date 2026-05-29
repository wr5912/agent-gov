import { Archive, Database, Loader2, RotateCcw } from "lucide-react";
import type { AttributionOutput, FeedbackAnalysisJobRecord } from "../../types/feedback";
import type { AttributionDetailTab } from "./CasesWorkspace";
import {
  DetailJsonPreview,
  DetailMetricGrid,
  DetailRecordList,
  DetailTabs,
  FormattedText,
  FormattedTextSection,
  Pill,
} from "./common";
import {
  formatDate,
  jobErrorCode,
  jobErrorMessage,
  jobStatusTone,
  latestItem,
  shortId,
  validationErrorItems,
  validationErrorMessage,
  validationErrorPath,
} from "./selectors";

export function AnalysisJobRecordList({ jobs }: { jobs: FeedbackAnalysisJobRecord[] }) {
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

export function AttributionDetails({
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

export function AttributionResult({ output }: { output: AttributionOutput }) {
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
