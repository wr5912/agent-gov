import { useState } from "react";
import { CheckCircle2, ChevronRight, GitBranch, Loader2, PlayCircle, RotateCcw, ShieldCheck } from "lucide-react";
import { diffAgentVersionFile } from "../../api/runtime";
import type {
  EvalRunRecord,
  ExecutionCompensationRecord,
  ExecutionPlanOperation,
  OptimizationExecutionJobRecord,
  OptimizationTaskRecord,
} from "../../types/feedback";
import type { AgentVersionFileDiff, RuntimeClientConfig } from "../../types/runtime";
import { DetailMetricGrid, DetailRecordList, FormattedText, FormattedTextFields, Pill } from "./common";
import {
  changedPathsFromDiff,
  fileStatusFromDiff,
  fileStatusText,
  fileStatusTone,
  formatDate,
  jobStatusTone,
  proposalStatusText,
  rawString,
  shortId,
  taskProposalId,
  taskStatusDescription,
} from "./selectors";

export function TasksDetails({
  clientConfig,
  tasks,
  actionId,
  onMarkApplied,
  onCreateExecutionJob,
  onApplyExecutionJob,
  onRestoreCompensation,
  onRunRegression,
}: {
  clientConfig?: RuntimeClientConfig;
  tasks: OptimizationTaskRecord[];
  actionId?: string | null;
  onMarkApplied?: (task: OptimizationTaskRecord) => void;
  onCreateExecutionJob?: (task: OptimizationTaskRecord, force?: boolean) => void;
  onApplyExecutionJob?: (task: OptimizationTaskRecord) => void;
  onRestoreCompensation?: (compensation: ExecutionCompensationRecord) => void;
  onRunRegression?: (task: OptimizationTaskRecord) => void;
}) {
  return (
    <DetailRecordList hasItems={tasks.length > 0} emptyText="暂无优化任务">
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
  const proposal = task.proposal;
  const proposalId = taskProposalId(task);
  const targetPaths = task.target_paths || [];
  const latestRegression = task.latest_regression_run || null;
  const latestExecution = task.latest_execution_job || null;
  const diffFromVersion = task.pre_execution_agent_version_id || rawString(task.applied_agent_version, "parent_version_id");
  const diffToVersion = task.applied_agent_version_id || "";
  const canManualMarkApplied = !task.applied_agent_version_id && ["pending_execution", "failed", "needs_human_review"].includes(task.status);
  const canCreateExecution = !task.applied_agent_version_id && ["pending_execution", "execution_failed", "execution_ready", "failed", "needs_human_review"].includes(task.status);
  const canApplyExecution = !task.applied_agent_version_id && latestExecution?.status === "ready";
  const canRunRegression = Boolean(task.applied_agent_version_id) && task.status !== "regression_running";
  const showManualFallback = Boolean(onMarkApplied && canManualMarkApplied);
  const regressionButtonLabel = latestRegression ? "重新运行回归验证" : "运行回归验证";
  return (
    <article className="fw-task-detail-card">
      <div className="fw-detail-record-head">
        <div>
          <h4>{shortId(task.optimization_task_id)} · optimization-task</h4>
          <small>反馈单 {shortId(task.feedback_case_id)} · 建议 {shortId(proposalId)}</small>
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
      {proposal ? (
        <section className="fw-task-source">
          <h4>{proposal.title || "来源优化方案"}</h4>
          <FormattedText value={proposal.recommendation || "-"} />
          <DetailMetricGrid items={[["审批状态", proposalStatusText[proposal.status] || proposal.status]]} />
          <FormattedTextFields
            fields={[
              ["预期效果", proposal.expected_effect || "-"],
              ["验证方式", proposal.validation || "-"],
              ["风险", proposal.risk || "-"],
            ]}
          />
        </section>
      ) : null}
      {latestExecution ? (
        <TaskExecutionPlanSection actionId={actionId} task={task} execution={latestExecution} onRestoreCompensation={onRestoreCompensation} />
      ) : null}
      <TaskRegressionSection task={task} latestRegression={latestRegression} canRunRegression={canRunRegression} />
      <TaskVersionDiffSection clientConfig={clientConfig} task={task} targetPaths={targetPaths} fromVersionId={diffFromVersion} toVersionId={diffToVersion} />
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
              应用执行方案
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
  const createsEvalCase = isEvalCaseExecutionPlan(task, execution);
  const compensations = execution.compensations || [];
  const title = createsEvalCase ? "执行方案：创建评估用例文件" : "执行方案";
  return (
    <section className={`fw-task-source fw-task-execution-section ${createsEvalCase ? "fw-task-execution-section-eval" : ""}`.trim()}>
      <div className="fw-task-section-head">
        <h4>{title}</h4>
        <small>{createsEvalCase ? "这里展示的是待写入文件内容，不是回归验证结果。" : "这里展示将要修改什么文件以及如何修改。"}</small>
      </div>
      <DetailMetricGrid
        items={[
          ["execution_job", shortId(execution.execution_job_id)],
          ["状态", execution.status],
          ["基线版本", shortId(execution.baseline_agent_version_id)],
          ["操作数", operations.length],
        ]}
      />
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

function ExecutionOperationCard({ createsEvalCase, operation }: { createsEvalCase: boolean; operation: ExecutionPlanOperation }) {
  const content = operation.content || operation.append_text || "";
  return (
    <div className="fw-execution-operation">
      <span>{operation.operation || "operation"}</span>
      <code>{operation.path || "-"}</code>
      {operation.rationale ? <small>{operation.rationale}</small> : null}
      {createsEvalCase && content ? (
        <details className="fw-execution-operation-content">
          <summary>查看将创建的评估用例草案</summary>
          <p>这是执行方案准备写入的 JSON 文件内容；真正的回归验证结果会在“回归验证”区域展示。</p>
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
  task,
  targetPaths,
  fromVersionId,
  toVersionId,
}: {
  clientConfig?: RuntimeClientConfig;
  task: OptimizationTaskRecord;
  targetPaths: string[];
  fromVersionId?: string | null;
  toVersionId?: string | null;
}) {
  const appliedDiff = task.latest_execution_job?.applied_diff || null;
  const targetRows = targetPaths.map((path) => ({ path, status: fileStatusFromDiff(appliedDiff, path) }));
  const nonTargetRows = changedPathsFromDiff(appliedDiff).filter((path) => !targetPaths.includes(path));
  if (!task.applied_agent_version_id) {
    return (
      <section className="fw-task-source">
        <h4>变更对比</h4>
        <p className="fw-note-box">任务尚未应用，暂无修改后版本。生成执行方案后可先查看计划操作，应用后再查看真实文件差异。</p>
      </section>
    );
  }
  if (!fromVersionId || !toVersionId) {
    return (
      <section className="fw-task-source">
        <h4>变更对比</h4>
        <p className="fw-note-box">缺少基线版本，无法展示前后对比。</p>
      </section>
    );
  }
  if (!clientConfig) {
    return (
      <section className="fw-task-source">
        <h4>变更对比</h4>
        <p className="fw-note-box">当前视图缺少 API 配置，无法加载文件级对比。</p>
      </section>
    );
  }
  return (
    <section className="fw-task-source">
      <h4>变更对比</h4>
      <DetailMetricGrid
        items={[
          ["修改前", shortId(fromVersionId)],
          ["修改后", shortId(toVersionId)],
          ["新增", appliedDiff?.added?.length ?? "-"],
          ["修改", appliedDiff?.modified?.length ?? "-"],
          ["删除", appliedDiff?.deleted?.length ?? "-"],
        ]}
      />
      <div className="fw-file-diff-list">
        {targetRows.map((row) => (
          <TaskFileDiffRow
            clientConfig={clientConfig}
            fromVersionId={fromVersionId}
            key={row.path}
            path={row.path}
            statusText={row.status}
            toVersionId={toVersionId}
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
                fromVersionId={fromVersionId}
                key={path}
                path={path}
                statusText={fileStatusFromDiff(appliedDiff, path)}
                toVersionId={toVersionId}
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
  clientConfig,
  fromVersionId,
  path,
  statusText,
  toVersionId,
}: {
  clientConfig: RuntimeClientConfig;
  fromVersionId: string;
  path: string;
  statusText: string;
  toVersionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [diff, setDiff] = useState<AgentVersionFileDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (!next || diff || loading) return;
    setLoading(true);
    setError(null);
    try {
      setDiff(await diffAgentVersionFile(clientConfig, fromVersionId, toVersionId, path));
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
