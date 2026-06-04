import { useState } from "react";
import { CheckCircle2, ChevronRight, Loader2 } from "lucide-react";
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
  executionPlanReady,
  formatDate,
  jobStatusTone,
  planTaskTone,
  shortId,
} from "./selectors";
import type {
  ExternalGovernanceWebhookRecord,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationBlockedItemRecord,
  FeedbackOptimizationPlanTaskRecord,
} from "../../types/feedback";

const PLAN_RUNNING_STATUSES = new Set(["created", "queued", "running", "schema_validating", "evidence_packaging"]);

export function BatchPlanDetails({
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

export function BatchExecutionSummary({ batch }: { batch: FeedbackOptimizationBatchRecord }) {
  const task = batch.optimization_task || null;
  const execution = task?.latest_execution_job || batch.execution_job || null;
  if (!task && !execution) return null;
  const output = execution?.validated_output_json || null;
  const operations = output?.operations || [];
  const plannedDiff = output?.planned_diff || null;
  const plannedFileCount = plannedDiff?.files?.length || operations.length;
  const appliedVersion = task?.applied_agent_version_id || null;
  const noActionReason = output?.no_action_reason || execution?.error_json?.message || null;
  const ready = executionPlanReady(execution);
  const stateLabel = appliedVersion ? "已应用" : ready ? "待应用" : execution ? execution.status : task?.status || "pending";
  const stateTone: PillTone = appliedVersion ? "green" : ready ? "orange" : execution ? jobStatusTone(execution.status) : "gray";
  const nextStep = appliedVersion
    ? "优化已应用并产生 Agent 版本，可以运行回归测试。"
    : ready
      ? "执行方案已生成，尚未写入 main-agent；应用后才会产生 Agent 版本。"
      : execution
        ? "执行方案尚未可应用，请查看未执行原因或重新生成执行方案。"
        : "优化任务已创建，等待生成执行方案。";
  return (
    <section className="fw-task-source fw-batch-execution-summary">
      <div className="fw-batch-execution-strip">
        <div className="fw-batch-execution-state">
          <h4>执行状态</h4>
          <Pill tone={stateTone}>{stateLabel}</Pill>
        </div>
        <DetailMetricGrid
          items={[
            ["优化任务", shortId(task?.optimization_task_id)],
            ["执行方案", shortId(execution?.execution_job_id)],
            ["计划文件", plannedFileCount],
            ["操作数", operations.length],
            ["应用版本", shortId(appliedVersion)],
          ]}
        />
      </div>
      <p className="fw-note-box">{nextStep}</p>
      {noActionReason ? <FormattedTextSection title="未执行原因" value={String(noActionReason)} compact /> : null}
    </section>
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
        <small>统筹归因结果后生成待执行方案。</small>
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
