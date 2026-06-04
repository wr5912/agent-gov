import { useState } from "react";
import { CheckCircle2, ChevronRight, GitBranch, Loader2, PlayCircle, RotateCcw, ShieldCheck } from "lucide-react";
import { diffAgentChangeSetFile } from "../../api/runtime";
import type {
  EvalRunRecord,
  ExecutionCompensationRecord,
  ExecutionPlannedDiff,
  ExecutionPlannedDiffFile,
  ExecutionPlanOperation,
  OptimizationExecutionJobRecord,
  OptimizationTaskRecord,
} from "../../types/feedback";
import type { AgentGitFileDiff, RuntimeClientConfig } from "../../types/runtime";
import { DetailMetricGrid, DetailRecordList, FormattedText, FormattedTextFields, Pill } from "./common";
import {
  changedPathsFromDiff,
  executionPlanReady,
  fileStatusFromDiff,
  fileStatusText,
  fileStatusTone,
  formatDate,
  jobStatusTone,
  planSnapshotStatusText,
  rawString,
  shortId,
  taskSourceId,
  taskStatusDescription,
} from "./selectors";

export function TasksDetails({
  clientConfig,
  tasks,
  actionId,
  variant = "default",
  onMarkApplied,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onRestoreCompensation,
  onRunRegression,
}: {
  clientConfig?: RuntimeClientConfig;
  tasks: OptimizationTaskRecord[];
  actionId?: string | null;
  variant?: "default" | "batch-plan";
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onCreateExecutionJob?: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob?: (task: OptimizationTaskRecord) => void;
  onRestoreCompensation?: (compensation: ExecutionCompensationRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  return (
    <DetailRecordList
      className={variant === "batch-plan" ? "fw-detail-record-list-batch-plan" : undefined}
      hasItems={tasks.length > 0}
      emptyText="暂无优化任务"
    >
      {tasks.map((task) => (
        <TaskDetailCard
          key={task.optimization_task_id}
          clientConfig={clientConfig}
          task={task}
          actionId={actionId || null}
          onMarkApplied={onMarkApplied}
          onCreateExecutionJob={onCreateExecutionJob}
          onApplyExecutionJob={onApplyExecutionJob}
          onRestoreCompensation={onRestoreCompensation}
          onRunRegression={onRunRegression}
        />
      ))}
    </DetailRecordList>
  );
}

export function TaskDetailCard({
  clientConfig,
  task,
  actionId,
  onMarkApplied,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onRestoreCompensation,
  onRunRegression,
}: {
  clientConfig?: RuntimeClientConfig;
  task: OptimizationTaskRecord;
  actionId?: string | null;
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onCreateExecutionJob?: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob?: (task: OptimizationTaskRecord) => void;
  onRestoreCompensation?: (compensation: ExecutionCompensationRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  const planSnapshot = task.proposal;
  const sourceId = taskSourceId(task);
  const targetPaths = task.target_paths || [];
  const latestRegression = task.latest_regression_run || null;
  const latestExecution = task.latest_execution_job || null;
  const changeSetId = task.latest_change_set_id || rawString(task.latest_change_set, "change_set_id") || rawString(task.latest_execution_application, "change_set_id");
  const candidateCommitSha = task.candidate_commit_sha || rawString(task.latest_change_set, "candidate_commit_sha");
  const canManualMarkApplied = !task.applied_agent_version_id && ["pending_execution", "failed", "needs_human_review"].includes(task.status);
  const canCreateExecution = !task.applied_agent_version_id && ["pending_execution", "execution_failed", "execution_ready", "failed", "needs_human_review"].includes(task.status);
  const canApplyExecution = !task.applied_agent_version_id && executionPlanReady(latestExecution);
  const canRunRegression = Boolean(changeSetId && candidateCommitSha) && task.status !== "regression_running";
  const showManualFallback = Boolean(onMarkApplied && canManualMarkApplied);
  const regressionButtonLabel = latestRegression ? "重新运行回归验证" : "运行回归验证";
  return (
    <article className="fw-task-detail-card">
      <div className="fw-detail-record-head">
        <div>
          <h4>{shortId(task.optimization_task_id)} · optimization-task</h4>
          <small>反馈单 {shortId(task.feedback_case_id)} · 方案任务 {shortId(sourceId)}</small>
        </div>
        <Pill tone={jobStatusTone(task.status)}>{task.status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["执行模式", task.execution_mode],
          ["来源", task.source],
          ["创建时间", formatDate(task.created_at)],
          ["基线版本", shortId(task.baseline_agent_version_id)],
          ["目标文件数", targetPaths.length],
          ["应用版本", shortId(task.applied_agent_version_id)],
          ["最近回归", latestRegression?.result_status || "-"],
        ]}
      />
      <div className="fw-task-targets">
        <strong>目标文件</strong>
        <div>
          {targetPaths.length ? targetPaths.map((path) => <span key={path}>{path}</span>) : <span>-</span>}
        </div>
      </div>
      {planSnapshot ? (
        <section className="fw-task-source">
          <h4>{planSnapshot.title || "来源优化方案"}</h4>
          <FormattedText value={planSnapshot.recommendation || "-"} />
          <DetailMetricGrid items={[["方案状态", planSnapshotStatusText[planSnapshot.status || ""] || planSnapshot.status || "-"]]} />
          <FormattedTextFields
            fields={[
              ["预期效果", planSnapshot.expected_effect || "-"],
              ["验证方式", planSnapshot.validation || "-"],
              ["风险", planSnapshot.risk || "-"],
            ]}
          />
        </section>
      ) : null}
      {latestExecution ? (
        <TaskExecutionPlanSection actionId={actionId} task={task} execution={latestExecution} onRestoreCompensation={onRestoreCompensation} />
      ) : null}
      <TaskRegressionSection task={task} latestRegression={latestRegression} canRunRegression={canRunRegression} />
      <TaskVersionDiffSection clientConfig={clientConfig} task={task} targetPaths={targetPaths} changeSetId={changeSetId} />
      <p className="fw-note-box fw-task-status-note">{taskStatusDescription(task.status)}</p>
      {onCreateExecutionJob || onApplyExecutionJob || onRunRegression ? (
        <div className="fw-detail-action-row">
          {onCreateExecutionJob ? (
            <button
              className="fw-small-secondary"
              type="button"
              disabled={!canCreateExecution || actionId === `execution:${task.optimization_task_id}`}
              onClick={() => onCreateExecutionJob(task, Boolean(latestExecution))}
            >
              {actionId === `execution:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <ShieldCheck size={16} />}
              {latestExecution ? "重新生成执行方案" : "生成执行方案"}
            </button>
          ) : null}
          {onApplyExecutionJob ? (
            <button
              className="fw-small-primary"
              type="button"
              disabled={!canApplyExecution || actionId === `execution-apply:${task.optimization_task_id}`}
              onClick={() => onApplyExecutionJob(task)}
            >
              {actionId === `execution-apply:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
              应用执行方案并创建 Agent 版本
            </button>
          ) : null}
          {onRunRegression ? (
            <button
              className="fw-small-primary"
              type="button"
              disabled={!canRunRegression || actionId === `regression:${task.optimization_task_id}`}
              onClick={() => onRunRegression(task)}
            >
              {actionId === `regression:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <PlayCircle size={16} />}
              {regressionButtonLabel}
            </button>
          ) : null}
        </div>
      ) : null}
      {showManualFallback ? (
        <details className="fw-manual-fallback">
          <summary>兜底操作</summary>
          <div className="fw-manual-fallback-body">
            <p>仅当你已在外部或手工完成优化修改时使用。该操作不会应用执行方案，只会为当前主智能体配置创建快照。</p>
            <button
              className="fw-small-secondary"
              type="button"
              disabled={actionId === `apply:${task.optimization_task_id}`}
              onClick={() => onMarkApplied?.(task)}
            >
              {actionId === `apply:${task.optimization_task_id}` ? <Loader2 size={16} className="fw-spin" /> : <GitBranch size={16} />}
              人工已应用，创建快照
            </button>
          </div>
        </details>
      ) : null}
    </article>
  );
}

function TaskExecutionPlanSection({
  task,
  execution,
  actionId,
  onRestoreCompensation,
}: {
  task: OptimizationTaskRecord;
  execution: OptimizationExecutionJobRecord;
  actionId?: string | null;
  onRestoreCompensation?: (compensation: ExecutionCompensationRecord) => void;
}) {
  const output = execution.validated_output_json;
  const operations = output?.operations || [];
  const plannedDiff = output?.planned_diff || null;
  const planFiles = plannedDiff?.files?.length || operations.length;
  const applied = Boolean(task.applied_agent_version_id);
  const ready = executionPlanReady(execution);
  const createsEvalCase = isEvalCaseExecutionPlan(task, execution);
  const compensations = execution.compensations || [];
  const title = createsEvalCase ? "计划变更：创建评估用例文件" : applied ? "执行方案记录" : "计划变更 / 待应用";
  const description = createsEvalCase
    ? "这里展示的是待写入文件内容，不是回归验证结果。"
    : applied
      ? "这里展示已执行过的受控执行方案；真实生效差异见下方“已生效差异”。"
      : ready
        ? "执行方案已生成，尚未写入 main-agent；点击应用后才会产生 Agent 版本。"
        : "这里展示执行智能体生成的方案状态、操作和人工复核信息。";
  return (
    <section className={`fw-task-source fw-task-execution-section ${createsEvalCase ? "fw-task-execution-section-eval" : ""}`.trim()}>
      <div className="fw-task-section-head">
        <h4>{title}</h4>
        <small>{description}</small>
      </div>
      <DetailMetricGrid
        items={[
          ["execution_job", shortId(execution.execution_job_id)],
          ["状态", execution.status],
          ["基线版本", shortId(execution.baseline_agent_version_id)],
          ["操作数", operations.length],
          ["计划文件", planFiles],
        ]}
      />
      {!applied && ready ? <p className="fw-note-box fw-execution-pending-note">待应用：这些变更尚未写入 main-agent，也尚未生效。</p> : null}
      {output?.summary ? <FormattedText value={output.summary} /> : null}
      {output?.validation || output?.risk || output?.no_action_reason ? (
        <FormattedTextFields
          fields={[
            ["应用前检查", output.validation || "-"],
            ["风险", output.risk || "-"],
            ["未执行原因", output.no_action_reason || "-"],
          ]}
        />
      ) : null}
      {execution.error_json?.message ? <FormattedText className="fw-warning-text" value={String(execution.error_json.message)} /> : null}
      {compensations.length ? (
        <ExecutionCompensationList actionId={actionId} items={compensations} onRestoreCompensation={onRestoreCompensation} />
      ) : null}
      <PlannedDiffPreview plannedDiff={plannedDiff} />
      {operations.length ? (
        <div className="fw-execution-operation-list">
          {operations.map((operation, index) => (
            <ExecutionOperationCard createsEvalCase={createsEvalCase} operation={operation} key={`${operation.path || "operation"}:${index}`} />
          ))}
        </div>
      ) : (
        <p className="fw-note-box">当前执行方案没有可应用操作。</p>
      )}
    </section>
  );
}

function ExecutionCompensationList({
  items,
  actionId,
  onRestoreCompensation,
}: {
  items: ExecutionCompensationRecord[];
  actionId?: string | null;
  onRestoreCompensation?: (compensation: ExecutionCompensationRecord) => void;
}) {
  return (
    <div className="fw-task-execution-compensations">
      <div className="fw-task-section-head">
        <h4>应用补偿记录</h4>
        <small>执行写入后发生异常时，系统记录自动恢复或待人工恢复状态。</small>
      </div>
      {items.map((item) => (
        <details className="fw-note-box" open={item.status === "pending_manual_recovery"} key={item.compensation_id}>
          <summary>
            <strong>{shortId(item.compensation_id)}</strong>
            <Pill tone={compensationStatusTone(item)}>{compensationStatusText(item)}</Pill>
          </summary>
          <DetailMetricGrid
            items={[
              ["创建时间", formatDate(item.created_at)],
              ["恢复状态", item.restore_status],
              ["应用前版本", shortId(item.pre_execution_agent_version_id)],
            ]}
          />
          <FormattedTextFields
            fields={[
              ["原始错误", item.original_error || "-"],
              ["恢复错误", item.restore_error || "-"],
            ]}
          />
          {item.status === "pending_manual_recovery" && onRestoreCompensation ? (
            <div className="fw-current-case-actions">
              <button
                className="fw-small-secondary"
                disabled={actionId === `compensation-restore:${item.compensation_id}`}
                onClick={() => onRestoreCompensation(item)}
                type="button"
              >
                {actionId === `compensation-restore:${item.compensation_id}` ? <Loader2 size={16} className="fw-spin" /> : <RotateCcw size={16} />}
                恢复到应用前版本
              </button>
            </div>
          ) : null}
        </details>
      ))}
    </div>
  );
}

function compensationStatusText(item: ExecutionCompensationRecord): string {
  if (item.status === "resolved") return "已自动恢复";
  if (item.status === "pending_manual_recovery") return "待人工恢复";
  return item.status || item.restore_status || "补偿记录";
}

function compensationStatusTone(item: ExecutionCompensationRecord): "green" | "orange" | "gray" {
  if (item.status === "resolved") return "green";
  if (item.status === "pending_manual_recovery") return "orange";
  return "gray";
}

function PlannedDiffPreview({ plannedDiff }: { plannedDiff?: ExecutionPlannedDiff | null }) {
  const files = plannedDiff?.files || [];
  if (!files.length) return null;
  return (
    <div className="fw-planned-diff-list">
      <div className="fw-task-section-head">
        <h4>计划差异预览</h4>
        <small>这是应用前的预计写入结果；应用后以下方真实版本差异为准。</small>
      </div>
      <DetailMetricGrid
        items={[
          ["新增", plannedDiff?.added ?? 0],
          ["修改", plannedDiff?.modified ?? 0],
          ["未变", plannedDiff?.unchanged ?? 0],
          ["noop", plannedDiff?.noop ?? 0],
        ]}
      />
      {files.map((file, index) => (
        <PlannedDiffFileRow file={file} key={`${file.path || "planned"}:${index}`} />
      ))}
    </div>
  );
}

function PlannedDiffFileRow({ file }: { file: ExecutionPlannedDiffFile }) {
  return (
    <details className="fw-planned-diff-row">
      <summary>
        <span>{file.path || "-"}</span>
        <Pill tone={plannedDiffStatusTone(file.status)}>{plannedDiffStatusText(file.status)}</Pill>
      </summary>
      <DetailMetricGrid
        items={[
          ["操作", file.operation || "-"],
          ["expected_sha", shortId(file.expected_sha256)],
          ["before", shortId(file.before_sha256)],
          ["after", shortId(file.after_sha256)],
        ]}
      />
      {file.rationale ? <small>{file.rationale}</small> : null}
      {file.unified_diff ? (
        <pre>{file.unified_diff}</pre>
      ) : (
        <p className="fw-muted">{file.reason || "该计划变更没有内容级 diff。"}</p>
      )}
      {file.truncated && file.reason ? <p className="fw-warning-text">{file.reason}</p> : null}
    </details>
  );
}

function plannedDiffStatusText(status?: string | null): string {
  if (status === "added") return "新增";
  if (status === "modified") return "修改";
  if (status === "deleted") return "删除";
  if (status === "unchanged") return "未变";
  if (status === "noop") return "noop";
  return status || "unknown";
}

function plannedDiffStatusTone(status?: string | null): "blue" | "green" | "orange" | "red" | "gray" {
  if (status === "added") return "green";
  if (status === "modified") return "orange";
  if (status === "deleted") return "red";
  if (status === "unchanged" || status === "noop") return "gray";
  return "blue";
}

function ExecutionOperationCard({ createsEvalCase, operation }: { createsEvalCase: boolean; operation: ExecutionPlanOperation }) {
  const content = operation.content || operation.append_text || "";
  const contentTitle = createsEvalCase
    ? "查看将创建的评估用例草案"
    : operation.operation === "append_text"
      ? "查看计划追加内容"
      : operation.operation === "replace_file"
        ? "查看计划替换内容"
        : "查看计划写入内容";
  return (
    <div className="fw-execution-operation">
      <span>{operation.operation || "operation"}</span>
      <code>{operation.path || "-"}</code>
      {operation.rationale ? <small>{operation.rationale}</small> : null}
      {content ? (
        <details className="fw-execution-operation-content">
          <summary>{contentTitle}</summary>
          <pre>{content}</pre>
        </details>
      ) : null}
    </div>
  );
}

function TaskRegressionSection({
  task,
  latestRegression,
  canRunRegression,
}: {
  task: OptimizationTaskRecord;
  latestRegression: EvalRunRecord | null;
  canRunRegression: boolean;
}) {
  const evalCaseCount = latestRegression?.eval_case_ids?.length || latestRegression?.summary?.total || 0;
  const statusText = latestRegression ? latestRegression.result_status || latestRegression.status : "尚未运行";
  return (
    <section className="fw-task-source fw-task-regression-section">
      <div className="fw-task-section-head">
        <h4>回归验证</h4>
        <small>这里展示应用优化后的评估运行结果，不展示执行方案 JSON。</small>
      </div>
      <DetailMetricGrid
        items={
          latestRegression
            ? [
                ["eval_run", shortId(latestRegression.eval_run_id)],
                ["结果", statusText],
                ["用例数", evalCaseCount],
                ["通过", latestRegression.summary?.passed ?? 0],
                ["失败", latestRegression.summary?.failed ?? 0],
                ["需复核", latestRegression.summary?.needs_human_review ?? 0],
                ["完成时间", formatDate(latestRegression.completed_at)],
              ]
            : [
                ["状态", statusText],
                ["应用版本", shortId(task.applied_agent_version_id)],
                ["任务状态", task.status],
              ]
        }
      />
      <p className="fw-note-box">
        {latestRegression
          ? "最近一次回归验证已完成；如执行方案或评估用例发生变化，可重新运行。"
          : canRunRegression
            ? "任务已应用，可以手动运行回归验证，使用当前启用的反馈评估用例集。"
            : "任务尚未应用，需先应用执行方案或人工标记已应用后再运行回归验证。"}
      </p>
    </section>
  );
}

function TaskVersionDiffSection({
  clientConfig,
  changeSetId,
  task,
  targetPaths,
}: {
  clientConfig?: RuntimeClientConfig;
  changeSetId?: string | null;
  task: OptimizationTaskRecord;
  targetPaths: string[];
}) {
  const appliedDiff = task.latest_execution_application?.applied_diff || null;
  const targetRows = targetPaths.map((path) => ({ path, status: fileStatusFromDiff(appliedDiff, path) }));
  const nonTargetRows = changedPathsFromDiff(appliedDiff).filter((path) => !targetPaths.includes(path));
  if (!task.applied_agent_version_id) {
    return (
      <section className="fw-task-source">
        <h4>候选差异</h4>
        <p className="fw-note-box">任务尚未生成候选提交；上方“计划变更 / 待应用”展示预计写入内容。</p>
      </section>
    );
  }
  if (!changeSetId) {
    return (
      <section className="fw-task-source">
        <h4>候选差异</h4>
        <p className="fw-note-box">缺少 change set，无法展示候选对比。</p>
      </section>
    );
  }
  if (!clientConfig) {
    return (
      <section className="fw-task-source">
        <h4>候选差异</h4>
        <p className="fw-note-box">当前视图缺少 API 配置，无法加载文件级对比。</p>
      </section>
    );
  }
  return (
    <section className="fw-task-source">
      <h4>候选差异</h4>
      <DetailMetricGrid
        items={[
          ["Change set", shortId(changeSetId)],
          ["新增", appliedDiff?.added?.length ?? "-"],
          ["修改", appliedDiff?.modified?.length ?? "-"],
          ["删除", appliedDiff?.deleted?.length ?? "-"],
        ]}
      />
      <div className="fw-file-diff-list">
        {targetRows.map((row) => (
          <TaskFileDiffRow
            clientConfig={clientConfig}
            changeSetId={changeSetId}
            key={row.path}
            path={row.path}
            statusText={row.status}
          />
        ))}
      </div>
      {nonTargetRows.length ? (
        <details className="fw-nontarget-diff">
          <summary>非目标文件变更 {nonTargetRows.length}</summary>
          <div className="fw-file-diff-list">
            {nonTargetRows.map((path) => (
              <TaskFileDiffRow
                clientConfig={clientConfig}
                changeSetId={changeSetId}
                key={path}
                path={path}
                statusText={fileStatusFromDiff(appliedDiff, path)}
              />
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function isEvalCaseExecutionPlan(task: OptimizationTaskRecord, execution: OptimizationExecutionJobRecord): boolean {
  const operations = execution.validated_output_json?.operations || [];
  if (!operations.length) return false;
  const proposalTargetType = task.proposal?.target_type;
  return proposalTargetType === "eval_case" || operations.some((operation) => (operation.path || "").startsWith("evals/"));
}

function TaskFileDiffRow({
  changeSetId,
  clientConfig,
  path,
  statusText,
}: {
  changeSetId: string;
  clientConfig: RuntimeClientConfig;
  path: string;
  statusText: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [diff, setDiff] = useState<AgentGitFileDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (!next || diff || loading) return;
    setLoading(true);
    setError(null);
    try {
      setDiff(await diffAgentChangeSetFile(clientConfig, changeSetId, path));
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载文件对比失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fw-file-diff-row">
      <button className="fw-file-diff-toggle" type="button" onClick={toggle}>
        <ChevronRight size={15} className={expanded ? "is-open" : ""} />
        <span>{path}</span>
        <Pill tone={fileStatusTone(statusText)}>{fileStatusText(statusText)}</Pill>
      </button>
      {expanded ? (
        <div className="fw-file-diff-body">
          {loading ? <p className="fw-muted">加载对比中...</p> : null}
          {error ? <p className="fw-warning-text">{error}</p> : null}
          {diff ? (
            diff.unified_diff ? <pre>{diff.unified_diff}</pre> : <p className="fw-muted">{diff.reason || fileStatusText(diff.status)}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
