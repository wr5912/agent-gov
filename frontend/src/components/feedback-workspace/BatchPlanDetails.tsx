import { useState } from "react";
import { CheckCircle2, Loader2 } from "lucide-react";
import {
  DetailJsonPreview,
  DetailMetricGrid,
  FormattedText,
  FormattedTextFields,
  FormattedTextSection,
  Pill,
  type PillTone,
} from "./common";
import {
  batchPlanDisplayTitle,
  formatDate,
  jobStatusTone,
  planTaskTone,
  shortId,
} from "./selectors";
import { BatchExecutionRunPanel } from "./BatchExecutionRunPanel";
import { BatchPlanTaskCard, type BatchExecutionTaskResult } from "./BatchPlanTaskCard";
import type {
  ExternalGovernanceWebhookRecord,
  FeedbackBatchExecutionRunRecord,
  FeedbackOptimizationBatchExecuteAllRequest,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationBlockedItemRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackOptimizationPlanTaskUpdateRequest,
} from "../../types/feedback";
import type { RuntimeClientConfig } from "../../types/runtime";

const PLAN_RUNNING_STATUSES = new Set(["created", "queued", "running", "schema_validating", "evidence_packaging"]);

type PlanTabItem =
  | {
      key: string;
      kind: "task";
      label: string;
      tone: PillTone;
      task: FeedbackOptimizationPlanTaskRecord;
      result?: BatchExecutionTaskResult;
    }
  | {
      key: string;
      kind: "blocked";
      label: string;
      tone: PillTone;
      blockedItem: FeedbackOptimizationBlockedItemRecord;
    };

export function BatchPlanDetails({
  actionId,
  batch,
  clientConfig,
  externalWebhooks,
  onExecuteBatchPlanAll,
  onExecutePlanTask,
  onRollbackBatchExecution,
  onUpdatePlanTask,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  clientConfig: RuntimeClientConfig;
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  onExecuteBatchPlanAll: (batch: FeedbackOptimizationBatchRecord, payload?: FeedbackOptimizationBatchExecuteAllRequest) => void;
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
  onRollbackBatchExecution: (batch: FeedbackOptimizationBatchRecord, executionRunId: string) => void;
  onUpdatePlanTask: (
    batch: FeedbackOptimizationBatchRecord,
    planTask: FeedbackOptimizationPlanTaskRecord,
    payload: FeedbackOptimizationPlanTaskUpdateRequest,
  ) => Promise<boolean>;
}) {
  const [webhookAliases, setWebhookAliases] = useState<Record<string, string>>({});
  const [selectedPlanItemId, setSelectedPlanItemId] = useState("");
  const plan = batch.optimization_plan;
  const planJob = batch.optimization_plan_job || null;
  const planError = batch.optimization_plan_error || planJob?.error_json || null;
  if (!plan) {
    return (
      <BatchPlanEmptyState
        batch={batch}
        planError={planError}
        planJob={planJob}
      />
    );
  }
  const tasks = (plan.tasks || []).filter((task) => task.execution_kind === "workspace_execution" || task.execution_kind === "external_webhook" || task.execution_kind === "internal_action");
  const blockedItems = plan.blocked_items || [];
  const displayTitle = batchPlanDisplayTitle(batch);
  const latestRun = batch.latest_execution_run || null;
  const resultByTaskId = planExecutionResultByTaskId(latestRun);
  const planItems = buildPlanTabItems(tasks, blockedItems, resultByTaskId);
  const selectedPlanItem = planItems.find((item) => item.key === selectedPlanItemId) || planItems[0] || null;
  const selectedPlanItemKey = selectedPlanItem?.key || "";
  const aliasForTask = (task: FeedbackOptimizationPlanTaskRecord) => webhookAliases[task.plan_task_id] || externalWebhooks[0]?.alias || "";
  const externalTasks = tasks.filter((task) => task.execution_kind === "external_webhook");
  const missingExternalAlias = externalTasks.some((task) => !aliasForTask(task));
  const executeAllBusy = actionId === `batch-execute-all:${batch.batch_id}`;
  const executeAllDisabled = Boolean(actionId) || !tasks.length || missingExternalAlias;
  const executeAllTitle = missingExternalAlias
    ? "存在外部任务未选择 Webhook，不能一键执行。"
    : "生成并应用所有可执行任务，workspace 变更会合并为一个 Agent 版本。";
  const executeAllPayload = (): FeedbackOptimizationBatchExecuteAllRequest => ({
    force: true,
    webhook_alias_by_task_id: Object.fromEntries(
      externalTasks
        .map((task) => [task.plan_task_id, aliasForTask(task)])
        .filter(([, alias]) => Boolean(alias)),
    ),
  });
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
      <div className="fw-plan-task-window">
        <div className="fw-task-section-head">
          <h4>优化任务</h4>
          <small>{planItems.length ? `${tasks.length} 个可执行任务，${blockedItems.length} 个未形成任务` : "当前方案未形成优化任务"}</small>
        </div>
        <PlanItemTabs items={planItems} activeKey={selectedPlanItemKey} onSelect={setSelectedPlanItemId} />
        {selectedPlanItem?.kind === "task" ? (
          <BatchPlanTaskCard
            actionId={actionId}
            batch={batch}
            externalWebhooks={externalWebhooks}
            executionResult={selectedPlanItem.result}
            latestRun={latestRun}
            selectedWebhookAlias={aliasForTask(selectedPlanItem.task)}
            planTask={selectedPlanItem.task}
            onWebhookAliasChange={(alias) => setWebhookAliases((current) => ({ ...current, [selectedPlanItem.task.plan_task_id]: alias }))}
            onExecutePlanTask={onExecutePlanTask}
            onUpdatePlanTask={onUpdatePlanTask}
          />
        ) : null}
        {selectedPlanItem?.kind === "blocked" ? <BatchPlanBlockedItemCard item={selectedPlanItem.blockedItem} /> : null}
        {!planItems.length ? <p className="fw-note-box">当前优化方案没有可展示任务。必要时重新归因或重新生成优化方案。</p> : null}
      </div>
      <div className="fw-batch-one-click-bar">
        <button
          className="fw-small-primary"
          type="button"
          disabled={executeAllDisabled}
          title={executeAllTitle}
          onClick={() => onExecuteBatchPlanAll(batch, executeAllPayload())}
        >
          {executeAllBusy ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
          {executeAllBusy ? "执行中" : "一键执行"}
        </button>
      </div>
      <BatchExecutionRunPanel
        actionId={actionId}
        batch={batch}
        clientConfig={clientConfig}
        onRollbackBatchExecution={onRollbackBatchExecution}
      />
    </section>
  );
}

function planExecutionResultByTaskId(run: FeedbackBatchExecutionRunRecord | null) {
  const resultByTaskId = new Map<string, BatchExecutionTaskResult>();
  for (const result of run?.task_results || []) {
    if (result.plan_task_id) resultByTaskId.set(result.plan_task_id, result);
  }
  return resultByTaskId;
}

function buildPlanTabItems(
  tasks: FeedbackOptimizationPlanTaskRecord[],
  blockedItems: FeedbackOptimizationBlockedItemRecord[],
  resultByTaskId: Map<string, BatchExecutionTaskResult>,
): PlanTabItem[] {
  const taskItems: PlanTabItem[] = tasks.map((task, index) => {
    const result = resultByTaskId.get(task.plan_task_id);
    return {
      key: `task:${task.plan_task_id}`,
      kind: "task",
      label: task.title || `任务 ${index + 1}`,
      tone: result ? taskResultTone(result.status) : planTaskTone(task),
      task,
      result,
    };
  });
  const blockedTabItems: PlanTabItem[] = blockedItems.map((blockedItem, index) => ({
    key: `blocked:${blockedItem.blocked_item_id || index}`,
    kind: "blocked",
    label: blockedItem.title || `未形成任务 ${index + 1}`,
    tone: "orange",
    blockedItem,
  }));
  return [...taskItems, ...blockedTabItems];
}

function taskResultTone(status?: string | null): PillTone {
  if (status === "completed" || status === "applied" || status === "sent") return "green";
  if (status === "failed" || status === "execution_failed") return "red";
  if (status === "skipped" || status === "partial_failed" || status === "needs_human_review") return "orange";
  if (status === "running" || status === "queued") return "blue";
  return "gray";
}

function PlanItemTabs({
  activeKey,
  items,
  onSelect,
}: {
  activeKey: string;
  items: PlanTabItem[];
  onSelect: (key: string) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="fw-plan-item-tabs" role="tablist" aria-label="优化任务">
      {items.map((item) => (
        <button
          aria-selected={item.key === activeKey}
          className={`fw-plan-item-tab ${item.key === activeKey ? "is-active" : ""}`}
          key={item.key}
          onClick={() => onSelect(item.key)}
          role="tab"
          title={item.label}
          type="button"
        >
          <Pill tone={item.tone}>{item.kind === "blocked" ? "未形成任务" : item.result?.status || item.task.status || "任务"}</Pill>
          <span>{item.label}</span>
        </button>
      ))}
    </div>
  );
}

function BatchPlanEmptyState({
  batch,
  planError,
  planJob,
}: {
  batch: FeedbackOptimizationBatchRecord;
  planError: FeedbackOptimizationBatchRecord["optimization_plan_error"] | NonNullable<FeedbackOptimizationBatchRecord["optimization_plan_job"]>["error_json"] | null;
  planJob: FeedbackOptimizationBatchRecord["optimization_plan_job"] | null;
}) {
  const running = planJob ? PLAN_RUNNING_STATUSES.has(String(planJob.status)) : false;
  if (planError || planJob?.status === "failed" || planJob?.status === "timeout") {
    return (
      <section className="fw-task-source fw-batch-plan-section">
        <div className="fw-task-section-head">
          <h4>优化方案</h4>
          <Pill tone="red">生成失败</Pill>
        </div>
        <div className="fw-job-error">
          <strong>{planError?.error_code || "PLAN_GENERATION_FAILED"}</strong>
          <FormattedText value={planError?.message || "优化方案生成任务未产生可用结果。"} />
        </div>
        <DetailMetricGrid
          items={[
            ["批次", shortId(batch.batch_id)],
            ["job_id", shortId(planJob?.job_id || batch.optimization_plan_job_id)],
            ["状态", planJob?.status || batch.status],
            ["创建", formatDate(planJob?.created_at)],
            ["完成", formatDate(planJob?.completed_at)],
          ]}
        />
        <p className="fw-note-box">归因分析已完成，但优化方案生成失败。可以重新生成优化方案；若再次失败，请展开错误详情查看 formatter 或 Agent 输出。</p>
        <details className="fw-batch-attribution-raw">
          <summary>查看错误详情</summary>
          <DetailJsonPreview title="错误信息" value={planError || {}} />
          {planJob?.raw_output_json ? <DetailJsonPreview title="原始输出" value={planJob.raw_output_json} /> : null}
          {planJob?.input_json ? <DetailJsonPreview title="任务输入" value={planJob.input_json} /> : null}
        </details>
      </section>
    );
  }
  if (planJob && running) {
    return (
      <section className="fw-task-source fw-batch-plan-section">
        <div className="fw-task-section-head">
          <h4>优化方案</h4>
          <Pill tone={jobStatusTone(planJob.status)}>{planJob.status}</Pill>
        </div>
        <DetailMetricGrid
          items={[
            ["批次", shortId(batch.batch_id)],
            ["job_id", shortId(planJob.job_id)],
            ["创建", formatDate(planJob.created_at)],
            ["开始", formatDate(planJob.started_at)],
          ]}
        />
        <p className="fw-note-box">优化方案正在生成；完成后这里会显示待执行任务或失败原因。</p>
      </section>
    );
  }
  return (
    <section className="fw-task-source fw-batch-plan-section">
      <div className="fw-task-section-head">
        <h4>优化方案</h4>
        <small>统筹归因结果后生成待执行任务。</small>
      </div>
      <p className="fw-note-box">尚未生成优化方案。先运行归因分析，再生成统筹优化方案。</p>
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
