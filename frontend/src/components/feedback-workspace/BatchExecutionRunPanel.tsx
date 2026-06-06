import { RotateCcw } from "lucide-react";
import type {
  FeedbackBatchExecutionRunRecord,
  FeedbackOptimizationBatchRecord,
} from "../../types/feedback";
import type { RuntimeClientConfig } from "../../types/runtime";
import {
  DetailJsonPreview,
  DetailMetricGrid,
  FormattedText,
  Pill,
  type PillTone,
} from "./common";
import { FileDiffRow } from "./FileDiffRow";
import { changedPathsFromDiff, fileStatusFromDiff, formatDate, shortId } from "./selectors";

function executionRunTone(status?: string | null): PillTone {
  if (status === "completed" || status === "rolled_back") return "green";
  if (status === "partial_failed" || status === "rollback_failed") return "orange";
  if (status === "failed") return "red";
  if (status === "running") return "blue";
  return "gray";
}

function diffMetric(run: FeedbackBatchExecutionRunRecord, key: "added" | "modified" | "deleted") {
  const value = run.applied_diff?.[key];
  return Array.isArray(value) ? value.length : 0;
}

function taskResultCount(run: FeedbackBatchExecutionRunRecord, statuses: string[]) {
  const statusSet = new Set(statuses);
  return (run.task_results || []).filter((result) => statusSet.has(String(result.status))).length;
}

function canRollback(run: FeedbackBatchExecutionRunRecord) {
  return Boolean(
    run.execution_run_id &&
      run.pre_execution_agent_version_id &&
      run.status !== "running" &&
      run.status !== "rolled_back" &&
      run.status !== "rollback_failed" &&
      run.applied_agent_version_id,
  );
}

export function BatchExecutionRunPanel({
  actionId,
  batch,
  clientConfig,
  onRollbackBatchExecution,
}: {
  actionId: string | null;
  batch: FeedbackOptimizationBatchRecord;
  clientConfig: RuntimeClientConfig;
  onRollbackBatchExecution: (batch: FeedbackOptimizationBatchRecord, executionRunId: string) => void;
}) {
  const run = batch.latest_execution_run;
  if (!run) return null;
  const rollbackBusy = actionId === `batch-rollback:${run.execution_run_id}`;
  const changedPaths = changedPathsFromDiff(run.applied_diff);
  const changeSetId = run.change_set_id || null;
  const completedTasks = taskResultCount(run, ["completed", "applied", "sent"]);
  const failedTasks = taskResultCount(run, ["failed", "execution_failed"]);
  const skippedTasks = taskResultCount(run, ["skipped", "needs_human_review"]);
  return (
    <section className="fw-batch-execution-run-panel">
      <div className="fw-task-section-head">
        <h4>Agent 优化结果</h4>
        <Pill tone={executionRunTone(run.status)}>{run.status}</Pill>
      </div>
      <DetailMetricGrid
        items={[
          ["执行记录", shortId(run.execution_run_id)],
          ["任务成功/失败/跳过", `${completedTasks} / ${failedTasks} / ${skippedTasks}`],
          ["应用前版本", shortId(run.pre_execution_agent_version_id)],
          ["应用后版本", shortId(run.applied_agent_version_id)],
          ["Change set", shortId(changeSetId)],
          ["候选提交", shortId(run.candidate_commit_sha)],
          ["新增/修改/删除", `${diffMetric(run, "added")} / ${diffMetric(run, "modified")} / ${diffMetric(run, "deleted")}`],
          ["完成时间", formatDate(run.completed_at)],
        ]}
      />
      {run.applied_agent_version_id ? <p className="fw-note-box">一键执行已生成 Agent 版本。请在下方查看文件级 diff；如判断不安全，可使用回滚恢复到执行前版本。</p> : null}
      {run.error_json ? (
        <div className="fw-job-error">
          <strong>{run.error_json.error_code || "BATCH_EXECUTION_FAILED"}</strong>
          <FormattedText value={run.error_json.message || "批次执行失败。"} />
        </div>
      ) : null}
      {run.warnings?.length ? (
        <div className="fw-warning-list">
          {run.warnings.map((warning, index) => (
            <FormattedText key={`${run.execution_run_id}:warning:${index}`} value={warning} />
          ))}
        </div>
      ) : null}
      <BatchAgentDiffSection
        changeSetId={changeSetId}
        changedPaths={changedPaths}
        clientConfig={clientConfig}
        run={run}
      />
      <div className="fw-detail-action-row">
        <button
          className="fw-small-secondary"
          type="button"
          disabled={!canRollback(run) || rollbackBusy}
          title="恢复到一键执行前的 Agent 版本；执行记录和外部通知审计保留。"
          onClick={() => onRollbackBatchExecution(batch, run.execution_run_id)}
        >
          <RotateCcw size={16} />
          {rollbackBusy ? "回滚中" : "回滚"}
        </button>
      </div>
      <details className="fw-plan-task-disclosure">
        <summary>查看快照和原始记录</summary>
        <DetailJsonPreview title="执行 diff" value={run.applied_diff || {}} />
        <DetailJsonPreview title="应用前版本" value={run.pre_execution_agent_version || {}} />
        <DetailJsonPreview title="应用后版本" value={run.applied_agent_version || {}} />
        {run.rollback_result ? <DetailJsonPreview title="回滚结果" value={run.rollback_result} /> : null}
      </details>
    </section>
  );
}

function BatchAgentDiffSection({
  changeSetId,
  changedPaths,
  clientConfig,
  run,
}: {
  changeSetId: string | null;
  changedPaths: string[];
  clientConfig: RuntimeClientConfig;
  run: FeedbackBatchExecutionRunRecord;
}) {
  if (!run.applied_agent_version_id) {
    return <p className="fw-note-box">本次执行尚未产生 Agent 版本，暂无可展示 diff。</p>;
  }
  if (!changeSetId) {
    return <p className="fw-note-box">本次执行缺少 change set，无法加载文件级 diff；可展开原始记录查看版本快照。</p>;
  }
  if (!changedPaths.length) {
    return <p className="fw-note-box">本次执行未记录文件差异。</p>;
  }
  return (
    <section className="fw-agent-result-diff">
      <div className="fw-task-section-head">
        <h5>文件 diff</h5>
        <small>{changedPaths.length} 个文件变更</small>
      </div>
      <div className="fw-file-diff-list">
        {changedPaths.map((path) => (
          <FileDiffRow
            changeSetId={changeSetId}
            clientConfig={clientConfig}
            key={path}
            path={path}
            statusText={fileStatusFromDiff(run.applied_diff, path)}
          />
        ))}
      </div>
    </section>
  );
}
