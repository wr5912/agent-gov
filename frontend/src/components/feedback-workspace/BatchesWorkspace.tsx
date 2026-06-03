import { useEffect, useMemo, useState, type ReactNode } from "react";
import { CheckCircle2, ChevronRight, FileText, FolderKanban, Loader2, MessageSquare, PlayCircle, ShieldCheck, XCircle } from "lucide-react";
import {
  BatchEvalCaseGenerationIcon,
  batchEvalCaseGenerationState,
  type EvalCaseGenerationState,
} from "./BatchEvalCaseGenerationStatus";
import { BatchFeedbackSourcesDetails } from "./BatchFeedbackDetails";
import { BatchRegressionDetails } from "./BatchRegressionDetails";
import {
  DetailJsonPreview,
  DetailMetricGrid,
  FormattedText,
  FormattedTextFields,
  FormattedTextSection,
  Pill,
} from "./common";
import {
  attributionOutputFromJob,
  attributionStatusText,
  attributionStatusTone,
  batchRegressionStatusText,
  batchStatusTone,
  buildBatchAttributionJobs,
  buildBatchSourceRows,
  defaultBatchDetail,
  executionPlanReady,
  evalStatusTone,
  formatDate,
  jobStatusTone,
  planTaskTone,
  profileDisplayName,
  shortId,
  type BatchDetailView,
} from "./selectors";
import type {
  AttributionOutput,
  EvalCaseRecord,
  ExternalGovernanceWebhookRecord,
  FeedbackAnalysisJobRecord,
  FeedbackOptimizationBatchEvalCaseCreateRequest,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationBlockedItemRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackSourceRecord,
  EvalCaseUpdateRequest,
  OptimizationTaskRecord,
} from "../../types/feedback";

export function BatchesPanel({
  actionId,
  batches,
  evalCases,
  externalWebhooks,
  selectedBatch,
  sources,
  onArchiveEvalCase,
  onCreateEvalCase,
  onExecutePlanTask,
  onGeneratePlan,
  onRemoveEvalCase,
  onRejectPlan,
  onRunAttribution,
  onRunRegression,
  onSelectBatch,
  onUpdateEvalCase,
  renderAttributionResult,
  renderBatchTasksDetails,
}: {
  actionId: string | null;
  batches: FeedbackOptimizationBatchRecord[];
  evalCases: EvalCaseRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  selectedBatch: FeedbackOptimizationBatchRecord | null;
  sources: FeedbackSourceRecord[];
  onArchiveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCase: EvalCaseRecord) => Promise<boolean>;
  onCreateEvalCase: (
    batch: FeedbackOptimizationBatchRecord,
    payload: FeedbackOptimizationBatchEvalCaseCreateRequest,
  ) => Promise<boolean>;
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
  onGeneratePlan: (batch: FeedbackOptimizationBatchRecord) => void;
  onRemoveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCaseId: string) => Promise<boolean>;
  onRejectPlan: (batch: FeedbackOptimizationBatchRecord) => void;
  onRunAttribution: (batch: FeedbackOptimizationBatchRecord, force?: boolean) => void;
  onRunRegression: (batch: FeedbackOptimizationBatchRecord) => void;
  onSelectBatch: (batch: FeedbackOptimizationBatchRecord) => void;
  onUpdateEvalCase: (
    batch: FeedbackOptimizationBatchRecord,
    evalCase: EvalCaseRecord,
    payload: EvalCaseUpdateRequest,
  ) => Promise<boolean>;
  renderAttributionResult: (output: AttributionOutput) => ReactNode;
  renderBatchTasksDetails: (tasks: OptimizationTaskRecord[]) => ReactNode;
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
          {batches.map((batch) => {
            const evalCaseGeneration = batchEvalCaseGenerationState(batch);
            return (
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
                    {evalCaseGeneration ? <Pill tone={evalCaseGeneration.tone}>生成 {evalCaseGeneration.label}</Pill> : null}
                  </span>
                  <span className="fw-case-cause">更新：{formatDate(batch.updated_at)}</span>
                </span>
              </button>
            );
          })}
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
            {activeBatchDetail === "attribution" ? <BatchAttributionDetails jobs={attributionJobs} renderAttributionResult={renderAttributionResult} /> : null}
            {activeBatchDetail === "plan" ? (
              <>
                <BatchPlanDetails
                  actionId={actionId}
                  batch={selectedBatch}
                  externalWebhooks={externalWebhooks}
                  onExecutePlanTask={onExecutePlanTask}
                />
                <BatchExecutionSummary batch={selectedBatch} />
                {selectedBatch.optimization_task ? renderBatchTasksDetails([selectedBatch.optimization_task]) : null}
              </>
            ) : null}
            {activeBatchDetail === "regression" ? (
              <BatchRegressionDetails
                actionId={actionId}
                batch={selectedBatch}
                evalCases={evalCases}
                onArchiveEvalCase={onArchiveEvalCase}
                onCreateEvalCase={onCreateEvalCase}
                onRemoveEvalCase={onRemoveEvalCase}
                onUpdateEvalCase={onUpdateEvalCase}
              />
            ) : null}
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
  const evalCaseGeneration = batchEvalCaseGenerationState(batch);
  const regressionRunStatus = batch.latest_eval_run?.result_status || batch.latest_eval_run?.status;
  const regressionTone = regressionRunStatus ? evalStatusTone(regressionRunStatus) : evalCaseGeneration?.tone || "gray";
  const regressionValue = regressionRunStatus
    ? batchRegressionStatusText(batch)
    : evalCaseGeneration
      ? batchEvalCaseGenerationTabValue(evalCaseGeneration)
      : batchRegressionStatusText(batch);
  const regressionHint = evalCaseGeneration
    ? `${evalCaseGeneration.title} · ${evalCaseGeneration.detail}`
    : "查看和管理本批次关联的回归用例";
  const regressionIcon = evalCaseGeneration && !regressionRunStatus ? <BatchEvalCaseGenerationIcon state={evalCaseGeneration} /> : <PlayCircle size={17} />;
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
      title: "回归测试",
      value: regressionValue,
      hint: batch.latest_eval_run ? "查看用例、执行过程、检查结果和错误信息" : regressionHint,
      tone: regressionTone,
      icon: regressionIcon,
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

function batchEvalCaseGenerationTabValue(state: EvalCaseGenerationState): string {
  if (state.status === "completed" && state.generatedCount) return `用例${state.generatedCount}个`;
  if (state.status === "failed") return "生成失败";
  if (state.status === "timeout") return "生成超时";
  return state.label;
}

function BatchAttributionDetails({
  jobs,
  renderAttributionResult,
}: {
  jobs: FeedbackAnalysisJobRecord[];
  renderAttributionResult: (output: AttributionOutput) => ReactNode;
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
            {selectedJob ? (
              <BatchAttributionJobDetail job={selectedJob} renderAttributionResult={renderAttributionResult} />
            ) : (
              <div className="fw-empty-inline">选择一个归因任务后查看详情。</div>
            )}
          </div>
        </div>
      ) : (
        <p className="fw-note-box">归因分析正在启动或等待刷新；完成后这里会显示每条反馈对应的归因结果。</p>
      )}
    </section>
  );
}

function BatchAttributionJobDetail({
  job,
  renderAttributionResult,
}: {
  job: FeedbackAnalysisJobRecord;
  renderAttributionResult: (output: AttributionOutput) => ReactNode;
}) {
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
        renderAttributionResult(output)
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
  const appliedVersion = task?.applied_agent_version_id || null;
  const noActionReason = output?.no_action_reason || execution?.error_json?.message || null;
  const nextStep = appliedVersion
    ? "优化已应用并产生 Agent 版本，可以运行回归测试。"
    : executionPlanReady(execution)
      ? "执行方案已 ready，请先应用执行方案以产生 Agent 版本。"
      : execution
        ? "执行方案尚未可应用，请查看未执行原因或重新生成执行方案。"
        : "优化任务已创建，等待生成执行方案。";
  return (
    <section className="fw-task-source fw-batch-execution-summary">
      <div className="fw-task-section-head">
        <h4>执行状态</h4>
        <Pill tone={appliedVersion ? "green" : executionPlanReady(execution) ? "blue" : execution ? jobStatusTone(execution.status) : "gray"}>
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
