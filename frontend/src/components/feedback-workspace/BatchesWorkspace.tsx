import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ChevronRight, FileText, FolderKanban, Loader2, MessageSquare, PlayCircle, ShieldCheck } from "lucide-react";
import {
  BatchEvalCaseGenerationIcon,
  batchEvalCaseGenerationState,
  type EvalCaseGenerationState,
} from "./BatchEvalCaseGenerationStatus";
import { BatchFeedbackSourcesDetails } from "./BatchFeedbackDetails";
import { BatchPlanDetails } from "./BatchPlanDetails";
import { BatchRegressionDetails } from "./BatchRegressionDetails";
import {
  DetailJsonPreview,
  DetailMetricGrid,
  FormattedText,
  Pill,
} from "./common";
import {
  attributionOutputFromJob,
  batchPlanDisplayTitle,
  attributionStatusText,
  attributionStatusTone,
  batchRegressionStatusText,
  batchStatusTone,
  buildBatchRegressionEligibility,
  buildBatchAttributionJobs,
  buildBatchSourceRows,
  defaultBatchDetail,
  evalStatusTone,
  formatDate,
  jobStatusTone,
  profileDisplayName,
  shortId,
  type BatchRegressionEligibility,
  type BatchDetailView,
} from "./selectors";
import type {
  AttributionOutput,
  EvalCaseRecord,
  ExternalGovernanceWebhookRecord,
  FeedbackAnalysisJobRecord,
  FeedbackOptimizationBatchEvalCaseCreateRequest,
  FeedbackOptimizationBatchExecuteAllRequest,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackOptimizationPlanTaskUpdateRequest,
  FeedbackSourceRecord,
  EvalCaseUpdateRequest,
} from "../../types/feedback";
import type { AgentChangeSet, AgentRepositoryStatus, RuntimeClientConfig } from "../../types/runtime";

export function BatchesPanel({
  actionId,
  batches,
  clientConfig,
  evalCases,
  externalWebhooks,
  agentRepository,
  selectedBatch,
  sources,
  onArchiveEvalCase,
  onCreateEvalCase,
  onDiscardAgentWorkspaceChanges,
  onExecuteBatchPlanAll,
  onExecutePlanTask,
  onGeneratePlan,
  onRemoveEvalCase,
  onPromoteEvalCases,
  onPublishBatchChangeSet,
  onRunAttribution,
  onRunRegression,
  onRollbackBatchExecution,
  onSelectBatch,
  onSaveAgentWorkspaceSnapshot,
  onUpdateEvalCase,
  onUpdatePlanTask,
  renderAttributionResult,
}: {
  actionId: string | null;
  batches: FeedbackOptimizationBatchRecord[];
  clientConfig: RuntimeClientConfig;
  evalCases: EvalCaseRecord[];
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  agentRepository: AgentRepositoryStatus | null;
  selectedBatch: FeedbackOptimizationBatchRecord | null;
  sources: FeedbackSourceRecord[];
  onArchiveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCase: EvalCaseRecord) => Promise<boolean>;
  onCreateEvalCase: (
    batch: FeedbackOptimizationBatchRecord,
    payload: FeedbackOptimizationBatchEvalCaseCreateRequest,
  ) => Promise<boolean>;
  onDiscardAgentWorkspaceChanges: (repository: AgentRepositoryStatus | null | undefined) => void;
  onExecuteBatchPlanAll: (batch: FeedbackOptimizationBatchRecord, payload?: FeedbackOptimizationBatchExecuteAllRequest) => void;
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
  onGeneratePlan: (batch: FeedbackOptimizationBatchRecord) => void;
  onRemoveEvalCase: (batch: FeedbackOptimizationBatchRecord, evalCaseId: string) => Promise<boolean>;
  onPromoteEvalCases: (batch: FeedbackOptimizationBatchRecord) => Promise<boolean>;
  onPublishBatchChangeSet: (batch: FeedbackOptimizationBatchRecord) => void;
  onRunAttribution: (batch: FeedbackOptimizationBatchRecord, force?: boolean) => void;
  onRunRegression: (batch: FeedbackOptimizationBatchRecord) => void;
  onRollbackBatchExecution: (batch: FeedbackOptimizationBatchRecord, executionRunId: string) => void;
  onSaveAgentWorkspaceSnapshot: (repository: AgentRepositoryStatus | null | undefined) => void;
  onSelectBatch: (batch: FeedbackOptimizationBatchRecord) => void;
  onUpdateEvalCase: (
    batch: FeedbackOptimizationBatchRecord,
    evalCase: EvalCaseRecord,
    payload: EvalCaseUpdateRequest,
  ) => Promise<boolean>;
  onUpdatePlanTask: (
    batch: FeedbackOptimizationBatchRecord,
    planTask: FeedbackOptimizationPlanTaskRecord,
    payload: FeedbackOptimizationPlanTaskUpdateRequest,
  ) => Promise<boolean>;
  renderAttributionResult: (output: AttributionOutput) => ReactNode;
}) {
  const [activeBatchDetail, setActiveBatchDetail] = useState<BatchDetailView>(() => defaultBatchDetail(selectedBatch));
  const batchSourceRows = useMemo(() => buildBatchSourceRows(selectedBatch, sources), [selectedBatch, sources]);
  const attributionJobs = useMemo(() => buildBatchAttributionJobs(selectedBatch), [selectedBatch]);
  const hasBatchAttribution = Boolean(attributionJobs.length || selectedBatch?.attribution_job_ids?.length);
  const planGenerationFailed = Boolean(selectedBatch?.optimization_plan_error || selectedBatch?.optimization_plan_job?.error_json);
  const planTasks = selectedBatch?.optimization_plan?.tasks || [];
  const regressionEligibility = useMemo(
    () => buildBatchRegressionEligibility(selectedBatch, evalCases),
    [selectedBatch, evalCases],
  );
  const hasAppliedPlanTask = planTasks.some((task) => Boolean(task.applied_agent_version_id || task.internal_action_result));
  const hasActivePlanTaskExecution = planTasks.some((task) => {
    const latestExecutionJob = task.latest_execution_job as { status?: string | null } | null | undefined;
    return ["created", "queued", "running", "schema_validating", "evidence_packaging"].includes(latestExecutionJob?.status || "");
  });
  const latestExecutionRunStatus = selectedBatch?.latest_execution_run?.status || "";
  const hasLockedExecutionRun = ["running", "completed", "partial_failed", "rollback_failed"].includes(latestExecutionRunStatus);
  const planLocked = Boolean(
    selectedBatch?.execution_apply_result ||
      selectedBatch?.applied_agent_version_id ||
      hasAppliedPlanTask ||
      hasActivePlanTaskExecution ||
      hasLockedExecutionRun,
  );
  const hasAppliedOptimization = Boolean(selectedBatch?.latest_execution_run?.applied_agent_version_id || selectedBatch?.optimization_task?.applied_agent_version_id);
  const canRunBatchRegression = hasAppliedOptimization && regressionEligibility.eligibleTotal > 0;
  const regressionDisabledReason = batchRegressionDisabledReason(
    selectedBatch,
    hasAppliedOptimization,
    regressionEligibility,
  );
  const publishAvailability = batchPublishAvailability(selectedBatch);

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
                title={planLocked ? "当前优化方案已有执行结果或正在执行，请先处理执行结果，或创建新批次后重新生成。" : undefined}
                onClick={() => {
                  setActiveBatchDetail("plan");
                  onGeneratePlan(selectedBatch);
                }}
              >
                {actionId === `batch-plan:${selectedBatch.batch_id}` ? <Loader2 size={16} className="fw-spin" /> : <MessageSquare size={16} />}
                {selectedBatch.optimization_plan || planGenerationFailed ? "重新生成优化方案" : "生成优化方案"}
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
              <button
                className="fw-small-primary"
                type="button"
                disabled={Boolean(actionId) || !publishAvailability.canPublish}
                title={!publishAvailability.canPublish ? publishAvailability.reason : undefined}
                onClick={() => onPublishBatchChangeSet(selectedBatch)}
              >
                {actionId === `batch-publish:${selectedBatch.batch_id}` ? <Loader2 size={16} className="fw-spin" /> : <ShieldCheck size={16} />}
                发布
              </button>
            </div>
            {activeBatchDetail === "feedback" ? <BatchFeedbackSourcesDetails rows={batchSourceRows} /> : null}
            {activeBatchDetail === "attribution" ? <BatchAttributionDetails jobs={attributionJobs} renderAttributionResult={renderAttributionResult} /> : null}
            {activeBatchDetail === "plan" ? (
              <BatchPlanDetails
                actionId={actionId}
                agentRepository={agentRepository}
                batch={selectedBatch}
                clientConfig={clientConfig}
                externalWebhooks={externalWebhooks}
                onDiscardAgentWorkspaceChanges={onDiscardAgentWorkspaceChanges}
                onExecuteBatchPlanAll={onExecuteBatchPlanAll}
                onExecutePlanTask={onExecutePlanTask}
                onRollbackBatchExecution={onRollbackBatchExecution}
                onSaveAgentWorkspaceSnapshot={onSaveAgentWorkspaceSnapshot}
                onUpdatePlanTask={onUpdatePlanTask}
              />
            ) : null}
            {activeBatchDetail === "regression" ? (
              <BatchRegressionDetails
                actionId={actionId}
                batch={selectedBatch}
                evalCases={evalCases}
                onArchiveEvalCase={onArchiveEvalCase}
                onCreateEvalCase={onCreateEvalCase}
                onPromoteEvalCases={onPromoteEvalCases}
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
  const planError = batch.optimization_plan_error || batch.optimization_plan_job?.error_json || null;
  const planRunning = batch.optimization_plan_job && ["created", "queued", "running", "schema_validating", "evidence_packaging"].includes(String(batch.optimization_plan_job.status));
  const planValue = batch.optimization_plan?.status || (planError ? "生成失败" : planRunning ? String(batch.optimization_plan_job?.status) : "未生成");
  const planTone = batch.optimization_plan ? batchStatusTone(batch.optimization_plan.status) : planError ? "red" : planRunning ? jobStatusTone(batch.optimization_plan_job?.status) : "gray";
  const planHint = batch.optimization_plan
    ? batchPlanDisplayTitle(batch)
    : planError
        ? "优化方案生成失败，查看错误详情后可重新生成"
        : planRunning
          ? "优化方案正在生成，完成后刷新展示结果"
          : "统筹归因结果后生成待执行任务";
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
      value: planValue,
      hint: planHint,
      tone: planTone,
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

function batchRegressionDisabledReason(
  batch: FeedbackOptimizationBatchRecord | null,
  hasAppliedOptimization: boolean,
  eligibility: BatchRegressionEligibility,
): string {
  if (!hasAppliedOptimization) {
    return batch?.optimization_task
      ? "优化任务尚未应用，未产生 Agent 版本，不能运行回归测试。"
      : "尚未执行优化方案，不能运行回归测试。";
  }
  if (eligibility.promotableLinked) {
    return "当前批次候选用例需先晋级为批次专用回归资产。";
  }
  return "当前批次没有 active/approved 回归资产，不能运行回归测试。";
}

function batchPublishAvailability(batch: FeedbackOptimizationBatchRecord | null): {
  canPublish: boolean;
  reason?: string;
  changeSet?: AgentChangeSet;
} {
  if (!batch) return { canPublish: false, reason: "请选择优化批次。" };
  const changeSet = batch.optimization_task?.latest_change_set || undefined;
  if (!changeSet?.change_set_id || !changeSet.candidate_commit_sha) {
    return { canPublish: false, reason: "先一键执行优化方案，生成候选版本。" };
  }
  const blocker = typeof changeSet.publication_blocker === "string" ? changeSet.publication_blocker.trim() : "";
  if (blocker) return { canPublish: false, reason: blocker, changeSet };
  const evalRunStatus = batch.latest_eval_run?.result_status || batch.latest_eval_run?.status || "";
  if (!evalRunStatus) return { canPublish: false, reason: "先运行批次回归测试。", changeSet };
  if (evalRunStatus !== "passed") {
    if (["running", "created", "queued"].includes(evalRunStatus)) {
      return { canPublish: false, reason: "等待批次回归测试完成。", changeSet };
    }
    return { canPublish: false, reason: "批次回归未通过，禁止发布。", changeSet };
  }
  if (!["regression_passed", "candidate_committed"].includes(changeSet.status)) {
    return { canPublish: false, reason: "等待候选版本完成发布检查。", changeSet };
  }
  return { canPublish: true, changeSet };
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
