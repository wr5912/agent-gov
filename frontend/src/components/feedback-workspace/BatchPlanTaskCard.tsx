import { useState, type FormEvent } from "react";
import { CheckCircle2, ChevronRight, Loader2, Pencil, Save, X } from "lucide-react";
import {
  DetailMetricGrid,
  FormattedText,
  FormattedTextFields,
  FormattedTextSection,
  Pill,
  type PillTone,
} from "./common";
import { shortId } from "./selectors";
import { describeTaskStage, type TaskStageView } from "./taskLifecycle";
import type {
  ExternalGovernanceWebhookRecord,
  FeedbackBatchExecutionRunRecord,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackOptimizationPlanTaskUpdateRequest,
} from "../../types/feedback";

const WORKSPACE_TASK_EDITABLE_STATUSES = new Set(["pending_execution", "execution_ready", "execution_failed", "needs_human_review", "failed"]);
const EXTERNAL_TASK_EDITABLE_STATUSES = new Set(["pending_notification", "notification_failed"]);
const ACTIONABILITY_OPTIONS = [
  "direct_workspace_change",
  "workspace_config_change",
  "eval_only",
  "external_guidance",
  "runtime_fix",
  "needs_human_analysis",
  "not_actionable",
] as const;

interface PlanTaskEditDraft {
  title: string;
  description: string;
  objective: string;
  targetType: string;
  targetPath: string;
  actionability: string;
  owner: string;
  targetSummary: string;
  recommendation: string;
  recommendedActionsText: string;
  acceptanceCriteriaText: string;
  expectedEffect: string;
  validation: string;
  risk: string;
  taskContextText: string;
  evidenceSummary: string;
  evidenceRefsText: string;
  editNote: string;
  error: string | null;
}

type PlanTaskEditDraftField = Exclude<keyof PlanTaskEditDraft, "error">;
type EditableEvidenceRef = NonNullable<FeedbackOptimizationPlanTaskUpdateRequest["evidence_refs"]>[number];
export type BatchExecutionTaskResult = NonNullable<FeedbackBatchExecutionRunRecord["task_results"]>[number];

export function BatchPlanTaskCard({
  actionId,
  batch,
  executionResult,
  externalWebhooks,
  latestRun,
  planTask,
  selectedWebhookAlias,
  onWebhookAliasChange,
  onExecutePlanTask,
  onUpdatePlanTask,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  executionResult?: BatchExecutionTaskResult;
  externalWebhooks: ExternalGovernanceWebhookRecord[];
  latestRun: FeedbackBatchExecutionRunRecord | null;
  planTask: FeedbackOptimizationPlanTaskRecord;
  selectedWebhookAlias: string;
  onWebhookAliasChange: (alias: string) => void;
  onExecutePlanTask: (batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) => void;
  onUpdatePlanTask: (
    batch: FeedbackOptimizationBatchRecord,
    planTask: FeedbackOptimizationPlanTaskRecord,
    payload: FeedbackOptimizationPlanTaskUpdateRequest,
  ) => Promise<boolean>;
}) {
  const [editDraft, setEditDraft] = useState<PlanTaskEditDraft | null>(null);
  const currentAlias = selectedWebhookAlias || externalWebhooks[0]?.alias || "";
  const running = actionId === `plan-task:${planTask.plan_task_id}`;
  const editing = actionId === `plan-task-edit:${planTask.plan_task_id}`;
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
  const targetSummary = planTask.target_summary || (workspace ? `workspace:${planTask.target_path || "-"}` : external ? `external:${planTask.owner || planTask.target_type || "-"}` : planTask.target_type || "-");
  const feedbackCount = planTask.feedback_case_ids?.length || 0;
  const evalCount = planTask.eval_case_ids?.length || 0;
  const taskScopeLabel = workspace ? "受管 workspace 优化" : external ? "外部系统优化" : "优化任务";
  const editable = canEditPlanTask(planTask);
  const stage = describeTaskStage(planTask);
  const executeDisabledReason = running
    ? "执行进行中，请等待完成。"
    : workspace && workspaceDone
      ? "已应用到 workspace，无需重复执行。"
      : external && !externalWebhooks.length
        ? "未配置 Webhook，请在 /data/external-governance-webhooks.yaml 增加后再发送。"
        : external && !currentAlias
          ? "请先选择 Webhook 再发送任务。"
          : "";

  async function submitEdit(event: FormEvent) {
    event.preventDefault();
    if (!editDraft) return;
    try {
      const saved = await onUpdatePlanTask(batch, planTask, planTaskUpdatePayload(planTask, editDraft));
      if (saved) setEditDraft(null);
    } catch (error) {
      setEditDraft({ ...editDraft, error: error instanceof Error ? error.message : "优化任务内容无效" });
    }
  }

  return (
    <article className="fw-plan-task-card">
      <div className="fw-proposal-detail-title">
        <Pill tone={stage.statusTone}>{stage.statusLabel}</Pill>
        <h4>{planTask.title || shortId(planTask.plan_task_id)}</h4>
        <small>{taskScopeLabel}</small>
      </div>
      {editDraft ? (
        <PlanTaskEditForm
          busy={editing}
          draft={editDraft}
          planTask={planTask}
          onCancel={() => setEditDraft(null)}
          onChange={setEditDraft}
          onSubmit={submitEdit}
        />
      ) : (
        <>
          {stage.stages.length ? <TaskStageStepper stage={stage} /> : null}
          <p className="fw-task-next-action">
            <span className="fw-task-next-action-label">下一步</span>
            <span>{stage.nextActionHint}</span>
          </p>
          <FormattedText className="fw-proposal-long-text fw-plan-task-description" value={planTask.description || planTask.recommendation || "-"} />
          <div className="fw-plan-task-text-grid">
            <FormattedTextSection title="任务目标" value={planTask.objective || "-"} compact />
            <FormattedTextSection title="风险/注意事项" value={planTask.risk || "暂无明显额外风险。"} compact />
          </div>
          <PlanTaskListSection title="验收标准" items={planTask.acceptance_criteria} />
          {planTask.reason ? <FormattedText className="fw-warning-text fw-proposal-long-text" value={planTask.reason} /> : null}
          <TaskExecutionResultSection
            latestRun={latestRun}
            planTask={planTask}
            result={executionResult}
          />
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
                {planTask.execution_job_id ? <span>执行记录：{shortId(planTask.execution_job_id)}</span> : null}
                {planTask.external_item_id ? <span>外部任务：{shortId(planTask.external_item_id)}</span> : null}
                {planTask.latest_webhook_alias ? <span>最近目标：{planTask.latest_webhook_alias}</span> : null}
              </div>
            </details>
          ) : null}
          <div className="fw-detail-action-row fw-plan-task-actions">
            {workspace || external ? (
              <button
                className="fw-small-primary"
                type="button"
                disabled={!canExecute}
                title={canExecute
                  ? (workspace ? "把方案应用到受管 workspace（变更合并为一个 Agent 版本）" : "通知外部系统处理该任务")
                  : executeDisabledReason || "当前状态不可执行。"}
                onClick={() => onExecutePlanTask(batch, planTask, external ? currentAlias : undefined)}
              >
                {running ? <Loader2 size={16} className="fw-spin" /> : workspace ? <CheckCircle2 size={16} /> : <ChevronRight size={16} />}
                {running ? "执行中" : buttonLabel}
              </button>
            ) : (
              <Pill tone="orange">需人工复核</Pill>
            )}
            {editable ? (
              <button className="fw-small-secondary" type="button" disabled={Boolean(actionId)} title="编辑任务内容（标题/目标/执行对象等）后再执行" onClick={() => setEditDraft(planTaskEditDraft(planTask))}>
                <Pencil size={16} />
                编辑
              </button>
            ) : null}
            {external ? (
              <label className="fw-select-field">
                <span>Webhook</span>
                <select value={currentAlias} onChange={(event) => onWebhookAliasChange(event.target.value)} disabled={!externalWebhooks.length || running}>
                  {!externalWebhooks.length ? <option value="">未配置Webhook，请在 /data/external-governance-webhooks.yaml 文件中增加</option> : null}
                  {externalWebhooks.map((webhook) => (
                    <option key={webhook.alias} value={webhook.alias}>{webhook.name || webhook.alias}</option>
                  ))}
                </select>
              </label>
            ) : null}
          </div>
        </>
      )}
    </article>
  );
}

function TaskStageStepper({ stage }: { stage: TaskStageView }) {
  return (
    <ol className="fw-task-stepper" aria-label="任务阶段">
      {stage.stages.map((label, index) => {
        const state = index < stage.stageIndex
          ? "done"
          : index === stage.stageIndex
            ? (stage.failed ? "failed" : "current")
            : "todo";
        return (
          <li className={`fw-task-step is-${state}`} key={label}>
            <span className="fw-task-step-dot">{index + 1}</span>
            <span className="fw-task-step-label">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function TaskExecutionResultSection({
  latestRun,
  planTask,
  result,
}: {
  latestRun: FeedbackBatchExecutionRunRecord | null;
  planTask: FeedbackOptimizationPlanTaskRecord;
  result?: BatchExecutionTaskResult;
}) {
  const status = result?.status || (latestRun ? "本次批次执行未含此任务" : "尚未执行");
  const plannedFiles = Array.isArray(result?.planned_diff?.files) ? result.planned_diff.files.length : 0;
  return (
    <section className="fw-task-execution-result">
      <div className="fw-task-section-head">
        <h5>执行结果</h5>
        <Pill tone={taskExecutionResultTone(result?.status || null)}>{status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["批次执行", shortId(latestRun?.execution_run_id)],
          ["优化任务", shortId(result?.optimization_task_id || planTask.optimization_task_id)],
          ["执行 job", shortId(result?.execution_job_id || planTask.execution_job_id)],
          ["外部任务", shortId(result?.external_item_id || planTask.external_item_id)],
          ["Webhook", result?.webhook_alias || planTask.latest_webhook_alias || "-"],
          ["应用版本", shortId(result?.applied_agent_version_id || planTask.applied_agent_version_id)],
          ["计划文件", plannedFiles || "-"],
        ]}
      />
      {result?.summary ? <FormattedText className="fw-proposal-long-text" value={result.summary} /> : null}
      {result?.error_json ? (
        <div className="fw-job-error">
          <strong>{result.error_json.error_code || "TASK_EXECUTION_FAILED"}</strong>
          <FormattedText value={result.error_json.message || "任务执行失败。"} />
        </div>
      ) : null}
      {result?.rollback_note ? <FormattedText className="fw-warning-text" value={result.rollback_note} /> : null}
    </section>
  );
}

function taskExecutionResultTone(status?: string | null): PillTone {
  if (!status) return "gray";
  if (status === "completed" || status === "applied" || status === "sent") return "green";
  if (status === "failed" || status === "execution_failed") return "red";
  if (status === "skipped" || status === "partial_failed" || status === "needs_human_review") return "orange";
  if (status === "running" || status === "queued") return "blue";
  return "gray";
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

function canEditPlanTask(planTask: FeedbackOptimizationPlanTaskRecord): boolean {
  if (planTask.applied_agent_version_id) return false;
  if (planTask.execution_kind === "workspace_execution") return WORKSPACE_TASK_EDITABLE_STATUSES.has(String(planTask.status || ""));
  if (planTask.execution_kind === "external_webhook") {
    return EXTERNAL_TASK_EDITABLE_STATUSES.has(String(planTask.status || ""));
  }
  return false;
}

function planTaskEditDraft(planTask: FeedbackOptimizationPlanTaskRecord): PlanTaskEditDraft {
  return {
    title: planTask.title || "",
    description: planTask.description || "",
    objective: planTask.objective || "",
    targetType: planTask.target_type || "",
    targetPath: planTask.target_path || "",
    actionability: planTask.actionability || "",
    owner: planTask.owner || "",
    targetSummary: planTask.target_summary || "",
    recommendation: planTask.recommendation || "",
    recommendedActionsText: listToText(planTask.recommended_actions),
    acceptanceCriteriaText: listToText(planTask.acceptance_criteria),
    expectedEffect: planTask.expected_effect || "",
    validation: planTask.validation || "",
    risk: planTask.risk || "",
    taskContextText: JSON.stringify(planTask.task_context || {}, null, 2),
    evidenceSummary: planTask.evidence_summary || "",
    evidenceRefsText: JSON.stringify(planTask.evidence_refs || [], null, 2),
    editNote: planTask.edit_note || "",
    error: null,
  };
}

function listToText(items?: string[]) {
  return (items || []).filter(Boolean).join("\n");
}

function textToList(text: string) {
  return text.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function parseJsonObject(text: string, label: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  const parsed: unknown = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function parseEvidenceRefs(text: string): EditableEvidenceRef[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const parsed: unknown = JSON.parse(trimmed);
  if (!Array.isArray(parsed)) {
    throw new Error("证据引用必须是 JSON 对象数组");
  }
  return parsed.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("证据引用必须是 JSON 对象数组");
    }
    const record = item as Record<string, unknown>;
    const id = typeof record.id === "string" ? record.id.trim() : "";
    const type = typeof record.type === "string" && record.type.trim() ? record.type.trim() : "evidence_file";
    const reason = typeof record.reason === "string" ? record.reason.trim() : "";
    if (!id) throw new Error("证据引用缺少 id");
    return { type, id, reason };
  });
}

function planTaskUpdatePayload(planTask: FeedbackOptimizationPlanTaskRecord, draft: PlanTaskEditDraft): FeedbackOptimizationPlanTaskUpdateRequest {
  const payload: FeedbackOptimizationPlanTaskUpdateRequest = {
    title: draft.title,
    description: draft.description,
    objective: draft.objective,
    target_type: draft.targetType,
    target_summary: draft.targetSummary,
    recommendation: draft.recommendation,
    recommended_actions: textToList(draft.recommendedActionsText),
    acceptance_criteria: textToList(draft.acceptanceCriteriaText),
    expected_effect: draft.expectedEffect,
    validation: draft.validation,
    risk: draft.risk,
    task_context: parseJsonObject(draft.taskContextText, "任务上下文"),
    evidence_summary: draft.evidenceSummary,
    evidence_refs: parseEvidenceRefs(draft.evidenceRefsText),
    edit_note: draft.editNote,
  };
  if (draft.actionability) {
    payload.actionability = draft.actionability as FeedbackOptimizationPlanTaskUpdateRequest["actionability"];
  }
  if (planTask.execution_kind === "workspace_execution") {
    payload.target_path = draft.targetPath;
  }
  if (planTask.execution_kind === "external_webhook") {
    payload.owner = draft.owner;
  }
  return payload;
}

function PlanTaskEditForm({
  busy,
  draft,
  planTask,
  onCancel,
  onChange,
  onSubmit,
}: {
  busy: boolean;
  draft: PlanTaskEditDraft;
  planTask: FeedbackOptimizationPlanTaskRecord;
  onCancel: () => void;
  onChange: (draft: PlanTaskEditDraft) => void;
  onSubmit: (event: FormEvent) => void;
}) {
  const workspace = planTask.execution_kind === "workspace_execution";
  const external = planTask.execution_kind === "external_webhook";
  const updateField = (field: PlanTaskEditDraftField, value: string) => onChange({ ...draft, [field]: value, error: null });
  return (
    <form className="fw-eval-edit-form fw-plan-task-edit-form" onSubmit={onSubmit}>
      {draft.error ? <div className="fw-job-error"><strong>EDIT_TASK_FAILED</strong><FormattedText value={draft.error} /></div> : null}
      <div className="fw-eval-edit-grid">
        <label className="fw-eval-edit-field">
          <span>标题</span>
          <input value={draft.title} onChange={(event) => updateField("title", event.target.value)} disabled={busy} required />
        </label>
        <label className="fw-eval-edit-field">
          <span>目标类型</span>
          <input value={draft.targetType} onChange={(event) => updateField("targetType", event.target.value)} disabled={busy} required />
        </label>
        <label className="fw-eval-edit-field">
          <span>可执行性</span>
          <select value={draft.actionability} onChange={(event) => updateField("actionability", event.target.value)} disabled={busy} required>
            {ACTIONABILITY_OPTIONS.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        {workspace ? (
          <label className="fw-eval-edit-field">
            <span>目标文件</span>
            <input value={draft.targetPath} onChange={(event) => updateField("targetPath", event.target.value)} disabled={busy} required />
          </label>
        ) : null}
        {external ? (
          <label className="fw-eval-edit-field">
            <span>负责人</span>
            <input value={draft.owner} onChange={(event) => updateField("owner", event.target.value)} disabled={busy} />
          </label>
        ) : null}
        <label className="fw-eval-edit-field">
          <span>目标摘要</span>
          <input value={draft.targetSummary} onChange={(event) => updateField("targetSummary", event.target.value)} disabled={busy} />
        </label>
      </div>
      <label className="fw-eval-edit-field">
        <span>描述</span>
        <textarea value={draft.description} onChange={(event) => updateField("description", event.target.value)} disabled={busy} rows={3} />
      </label>
      <label className="fw-eval-edit-field">
        <span>任务目标</span>
        <textarea value={draft.objective} onChange={(event) => updateField("objective", event.target.value)} disabled={busy} rows={3} />
      </label>
      <label className="fw-eval-edit-field">
        <span>建议</span>
        <textarea value={draft.recommendation} onChange={(event) => updateField("recommendation", event.target.value)} disabled={busy} rows={4} />
      </label>
      <div className="fw-eval-edit-grid">
        <label className="fw-eval-edit-field">
          <span>执行提示</span>
          <textarea value={draft.recommendedActionsText} onChange={(event) => updateField("recommendedActionsText", event.target.value)} disabled={busy} rows={5} />
        </label>
        <label className="fw-eval-edit-field">
          <span>验收标准</span>
          <textarea value={draft.acceptanceCriteriaText} onChange={(event) => updateField("acceptanceCriteriaText", event.target.value)} disabled={busy} rows={5} />
        </label>
      </div>
      <div className="fw-eval-edit-grid">
        <label className="fw-eval-edit-field">
          <span>预期效果</span>
          <textarea value={draft.expectedEffect} onChange={(event) => updateField("expectedEffect", event.target.value)} disabled={busy} rows={3} />
        </label>
        <label className="fw-eval-edit-field">
          <span>验证方式</span>
          <textarea value={draft.validation} onChange={(event) => updateField("validation", event.target.value)} disabled={busy} rows={3} />
        </label>
      </div>
      <label className="fw-eval-edit-field">
        <span>风险</span>
        <textarea value={draft.risk} onChange={(event) => updateField("risk", event.target.value)} disabled={busy} rows={3} />
      </label>
      <label className="fw-eval-edit-field">
        <span>任务上下文</span>
        <textarea className="fw-eval-json-editor" value={draft.taskContextText} onChange={(event) => updateField("taskContextText", event.target.value)} disabled={busy} rows={7} />
      </label>
      <label className="fw-eval-edit-field">
        <span>证据摘要</span>
        <textarea value={draft.evidenceSummary} onChange={(event) => updateField("evidenceSummary", event.target.value)} disabled={busy} rows={3} />
      </label>
      <label className="fw-eval-edit-field">
        <span>证据引用</span>
        <textarea className="fw-eval-json-editor" value={draft.evidenceRefsText} onChange={(event) => updateField("evidenceRefsText", event.target.value)} disabled={busy} rows={6} />
      </label>
      <label className="fw-eval-edit-field">
        <span>编辑说明</span>
        <textarea value={draft.editNote} onChange={(event) => updateField("editNote", event.target.value)} disabled={busy} rows={2} />
      </label>
      <div className="fw-detail-action-row fw-plan-task-actions">
        <button className="fw-small-secondary" type="button" onClick={onCancel} disabled={busy}>
          <X size={16} />
          取消
        </button>
        <button className="fw-small-primary" type="submit" disabled={busy}>
          {busy ? <Loader2 size={16} className="fw-spin" /> : <Save size={16} />}
          保存
        </button>
      </div>
    </form>
  );
}
