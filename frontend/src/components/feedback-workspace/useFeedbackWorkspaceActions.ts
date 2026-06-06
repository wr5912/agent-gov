import { useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import {
  createFeedbackOptimizationBatch,
  createFeedbackOptimizationBatchEvalCase,
  createFeedbackOptimizationBatchRegressionPlan,
  discardAgentRepositoryChanges,
  executeFeedbackOptimizationBatchPlanAll,
  executeFeedbackOptimizationPlanTask,
  generateFeedbackOptimizationBatchPlan,
  generateFeedbackSourceEvalCases,
  getAgentJob,
  removeFeedbackOptimizationBatchEvalCase,
  rollbackFeedbackOptimizationBatchExecution,
  runFeedbackOptimizationBatchAttribution,
  runFeedbackOptimizationBatchRegression,
  snapshotAgentRepository,
  updateFeedbackOptimizationBatchEvalCase,
  updateFeedbackOptimizationPlanTask,
} from "../../api/runtime";
import type {
  AgentJobRecord,
  EvalCaseRecord,
  EvalCaseUpdateRequest,
  FeedbackOptimizationBatchEvalCaseCreateRequest,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationBatchExecuteAllRequest,
  FeedbackOptimizationPlanTaskUpdateRequest,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackSourceRef,
} from "../../types/feedback";
import type { AgentRepositoryStatus } from "../../types/runtime";
import type { ExternalFeedbackWorkspaceProps } from "./types";
import {
  shortId,
  type SourceRow,
} from "./selectors";
import type { MenuKey } from "./useFeedbackWorkspaceState";

type StateSetter<T> = Dispatch<SetStateAction<T>>;

const TERMINAL_AGENT_JOB_STATUSES = new Set(["completed", "failed", "needs_human_review", "timeout"]);
const AGENT_JOB_POLL_INTERVAL_MS = 1500;
const DEFAULT_AGENT_JOB_POLL_WINDOW_MS = 120_000;
const MAX_AGENT_JOB_POLL_WINDOW_MS = 330_000;
const AGENT_JOB_POLL_TIMEOUT_BUFFER_SECONDS = 30;

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

interface BatchPlanGenerateDraft {
  batch: FeedbackOptimizationBatchRecord;
  instruction: string;
}

export function useFeedbackWorkspaceActions({
  clientConfig,
  onFeedbackChanged,
  onRefreshVersions,
  selectedSourceIds,
  setSelectedSourceIds,
  setSelectedBatchId,
  setActiveMenu,
  sourceRows,
  refreshWorkbench,
  setToast,
}: {
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  onFeedbackChanged?: () => void;
  onRefreshVersions?: () => void | Promise<void>;
  selectedSourceIds: string[];
  setSelectedSourceIds: StateSetter<string[]>;
  setSelectedBatchId: StateSetter<string | null>;
  setActiveMenu: StateSetter<MenuKey>;
  sourceRows: SourceRow[];
  refreshWorkbench: () => Promise<void>;
  setToast: StateSetter<string | null>;
}) {
  const [actionId, setActionId] = useState<string | null>(null);
  const [batchPlanGenerateDraft, setBatchPlanGenerateDraft] = useState<BatchPlanGenerateDraft | null>(null);
  const batchPlanGenerateBusy = Boolean(actionId?.startsWith("batch-plan:"));

  async function waitForAgentJob(jobId?: string | null) {
    if (!jobId) return null;
    let latest: AgentJobRecord | null = null;
    const startedAt = Date.now();
    let deadline = startedAt + DEFAULT_AGENT_JOB_POLL_WINDOW_MS;
    while (Date.now() < deadline) {
      latest = await getAgentJob(clientConfig, jobId);
      if (TERMINAL_AGENT_JOB_STATUSES.has(String(latest.status))) return latest;
      deadline = Math.max(deadline, startedAt + pollWindowMs(latest));
      await delay(Math.min(AGENT_JOB_POLL_INTERVAL_MS, Math.max(deadline - Date.now(), 0)));
    }
    return latest;
  }

  async function waitForAgentJobs(jobIds: Array<string | null | undefined>) {
    const ids = jobIds.filter((jobId): jobId is string => Boolean(jobId));
    return Promise.all(ids.map((jobId) => waitForAgentJob(jobId)));
  }

  function toggleSource(sourceId: string, checked: boolean) {
    setSelectedSourceIds((current) => {
      if (checked) return current.includes(sourceId) ? current : [...current, sourceId];
      return current.filter((item) => item !== sourceId);
    });
  }

  function selectedSourceRefs(): FeedbackSourceRef[] {
    const selectedRows = sourceRows.filter((row) => selectedSourceIds.includes(row.id));
    return selectedRows.map((row) => ({ source_kind: row.kind, source_id: row.id }));
  }

  async function generateEvalCasesFromSelection() {
    const refs = selectedSourceRefs();
    if (!refs.length) {
      setToast("请先选择反馈信息");
      return;
    }
    setActionId("generate-eval-cases");
    try {
      const result = await generateFeedbackSourceEvalCases(clientConfig, { source_refs: refs });
      const completed = await waitForAgentJob(result.job_id);
      setToast(`回归用例生成 ${completed?.status || result.status}：${shortId(result.job_id)}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "生成回归用例失败");
    } finally {
      setActionId(null);
    }
  }

  async function createBatchFromSelection() {
    const refs = selectedSourceRefs();
    if (!refs.length) {
      setToast("请先选择反馈信息");
      return;
    }
    setActionId("create-batch");
    try {
      const batch = await createFeedbackOptimizationBatch(clientConfig, {
        source_refs: refs,
        priority: refs.length >= 5 ? "high" : "medium",
      });
      const evalJobId = batch.eval_case_generation_job_id;
      setToast(`已创建优化批次 ${shortId(batch.batch_id)}${evalJobId ? ` · 回归用例后台生成中 ${shortId(evalJobId)}` : ""}`);
      setSelectedSourceIds([]);
      setSelectedBatchId(batch.batch_id);
      setActiveMenu("batches");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "创建优化批次失败");
    } finally {
      setActionId(null);
    }
  }

  async function runBatchAttribution(batch: FeedbackOptimizationBatchRecord, force = false) {
    setActionId(`batch-attribution:${batch.batch_id}`);
    try {
      const result = await runFeedbackOptimizationBatchAttribution(clientConfig, batch.batch_id, force ? { force: true } : undefined);
      const completedJobs = await waitForAgentJobs(result.jobs.map((job) => job.job_id));
      const done = completedJobs.filter((job) => job?.status === "completed").length;
      setToast(`${force ? "已重新归因" : "批次归因完成"}：${done}/${result.jobs.length} 个 job`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批次归因失败");
    } finally {
      setActionId(null);
    }
  }

  function openBatchPlanGeneration(batch: FeedbackOptimizationBatchRecord) {
    if (batch.optimization_plan) {
      setBatchPlanGenerateDraft({ batch, instruction: "" });
      return;
    }
    void generateBatchPlan(batch);
  }

  async function submitBatchPlanGeneration(event: FormEvent) {
    event.preventDefault();
    if (!batchPlanGenerateDraft) return;
    const { batch } = batchPlanGenerateDraft;
    const instruction = batchPlanGenerateDraft.instruction.trim();
    setBatchPlanGenerateDraft(null);
    await generateBatchPlan(batch, instruction);
  }

  async function generateBatchPlan(batch: FeedbackOptimizationBatchRecord, instruction?: string) {
    setActionId(`batch-plan:${batch.batch_id}`);
    try {
      const job = await generateFeedbackOptimizationBatchPlan(
        clientConfig,
        batch.batch_id,
        instruction ? { regeneration_instruction: instruction } : undefined,
      );
      setSelectedBatchId(batch.batch_id);
      await refreshWorkbench();
      const completed = await waitForAgentJob(job.job_id);
      const status = String(completed?.status || job.status || "");
      if (!TERMINAL_AGENT_JOB_STATUSES.has(status)) {
        setToast(`优化方案仍在后台处理中 ${status || "running"}：${shortId(job.job_id)}`);
      } else {
        setToast(`${batch.optimization_plan ? "重新生成优化方案" : "优化方案生成"} ${status}：${shortId(job.job_id)}`);
      }
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "生成优化方案失败");
    } finally {
      setActionId(null);
    }
  }

  async function executePlanTask(batch: FeedbackOptimizationBatchRecord, planTask: FeedbackOptimizationPlanTaskRecord, webhookAlias?: string) {
    setActionId(`plan-task:${planTask.plan_task_id}`);
    try {
      const result = await executeFeedbackOptimizationPlanTask(clientConfig, batch.batch_id, planTask.plan_task_id, {
        force: true,
        webhook_alias: webhookAlias || undefined,
      });
      if (result.apply_result || result.optimization_task?.applied_agent_version_id) {
        setToast(`已执行任务，生成版本 ${shortId(result.optimization_task?.applied_agent_version_id)}`);
        await onRefreshVersions?.();
      } else if (result.external_item) {
        setToast(`${result.external_item.status === "notified" ? "已发送外部任务" : "外部任务发送失败"}：${webhookAlias || result.external_item.latest_webhook_alias || "-"}`);
      } else {
        const completed = await waitForAgentJob(result.execution_job?.execution_job_id);
        setToast(`任务执行 ${completed?.status || result.execution_job?.status || result.batch.status}`);
      }
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "执行任务失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function executeBatchPlanAll(batch: FeedbackOptimizationBatchRecord, payload: FeedbackOptimizationBatchExecuteAllRequest = {}) {
    setActionId(`batch-execute-all:${batch.batch_id}`);
    try {
      const result = await executeFeedbackOptimizationBatchPlanAll(clientConfig, batch.batch_id, {
        force: true,
        ...payload,
      });
      const versionId = result.execution_run.applied_agent_version_id;
      const statusText = result.execution_run.status === "completed" ? "已完成" : result.execution_run.status;
      setToast(versionId ? `一键执行${statusText}，生成版本 ${shortId(versionId)}` : `一键执行${statusText}`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "一键执行优化方案失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function discardAgentWorkspaceChanges(repository: AgentRepositoryStatus | null | undefined) {
    const paths = repositoryChangedPaths(repository);
    if (!paths.length) {
      setToast("Main Agent workspace 当前没有需要丢弃的未提交改动");
      return;
    }
    setActionId("agent-repository:discard");
    try {
      await discardAgentRepositoryChanges(clientConfig, { paths });
      setToast(`已丢弃 Main Agent workspace 的 ${paths.length} 个未提交改动`);
      await onRefreshVersions?.();
      await refreshWorkbench();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "丢弃 Main Agent workspace 改动失败");
    } finally {
      setActionId(null);
    }
  }

  async function saveAgentWorkspaceSnapshot(repository: AgentRepositoryStatus | null | undefined) {
    const paths = repositoryChangedPaths(repository);
    if (!paths.length) {
      setToast("Main Agent workspace 当前没有需要保存的未提交改动");
      return;
    }
    setActionId("agent-repository:snapshot");
    try {
      const version = await snapshotAgentRepository(clientConfig, {
        operator: "ui",
        note: `保存一键执行前 Main Agent workspace 的 ${paths.length} 个未提交改动。`,
      });
      setToast(`已保存 Main Agent workspace 为版本 ${shortId(version.agent_version_id)}`);
      await onRefreshVersions?.();
      await refreshWorkbench();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "保存 Main Agent workspace 版本失败");
    } finally {
      setActionId(null);
    }
  }

  async function rollbackBatchExecution(batch: FeedbackOptimizationBatchRecord, executionRunId: string) {
    setActionId(`batch-rollback:${executionRunId}`);
    try {
      const result = await rollbackFeedbackOptimizationBatchExecution(clientConfig, batch.batch_id, executionRunId, {
        note: `从优化批次 ${batch.batch_id} 回滚执行记录 ${executionRunId}`,
      });
      setToast(`已回滚执行记录 ${shortId(result.execution_run.execution_run_id)}`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "回滚批次执行失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function updatePlanTask(
    batch: FeedbackOptimizationBatchRecord,
    planTask: FeedbackOptimizationPlanTaskRecord,
    payload: FeedbackOptimizationPlanTaskUpdateRequest,
  ) {
    setActionId(`plan-task-edit:${planTask.plan_task_id}`);
    try {
      const result = await updateFeedbackOptimizationPlanTask(clientConfig, batch.batch_id, planTask.plan_task_id, payload);
      const invalidatedCount = result.invalidated_execution_job_ids?.length || 0;
      setToast(invalidatedCount ? `已更新优化任务，并清空 ${invalidatedCount} 条旧执行记录` : "已更新优化任务");
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "更新优化任务失败");
      await refreshWorkbench();
      return false;
    } finally {
      setActionId(null);
    }
  }

  async function runBatchRegression(batch: FeedbackOptimizationBatchRecord) {
    setActionId(`batch-regression:${batch.batch_id}`);
    try {
      const plan = await createFeedbackOptimizationBatchRegressionPlan(clientConfig, batch.batch_id);
      const result = await runFeedbackOptimizationBatchRegression(clientConfig, batch.batch_id, {
        regression_plan_id: plan.regression_plan_id,
      });
      const impact = await waitForAgentJob(result.impact_analysis_job?.job_id);
      setToast(`批次回归测试完成：${result.eval_run.result_status || result.eval_run.status}${impact ? ` · 影响分析 ${impact.status}` : ""}`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批次回归测试失败");
    } finally {
      setActionId(null);
    }
  }

  async function createBatchEvalCase(
    batch: FeedbackOptimizationBatchRecord,
    payload: FeedbackOptimizationBatchEvalCaseCreateRequest,
  ) {
    setActionId(`batch-eval-create:${batch.batch_id}`);
    try {
      const evalCase = await createFeedbackOptimizationBatchEvalCase(clientConfig, batch.batch_id, payload);
      setToast(`已新增回归用例 ${shortId(evalCase.eval_case_id)}`);
      setSelectedBatchId(batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "新增回归用例失败");
      return false;
    } finally {
      setActionId(null);
    }
  }

  async function updateBatchEvalCase(
    batch: FeedbackOptimizationBatchRecord,
    evalCase: EvalCaseRecord,
    payload: EvalCaseUpdateRequest,
  ) {
    setActionId(`batch-eval-update:${evalCase.eval_case_id}`);
    try {
      const updated = await updateFeedbackOptimizationBatchEvalCase(clientConfig, batch.batch_id, evalCase.eval_case_id, payload);
      setToast(`已更新回归用例 ${shortId(updated.eval_case_id)}`);
      setSelectedBatchId(batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "更新回归用例失败");
      return false;
    } finally {
      setActionId(null);
    }
  }

  async function archiveBatchEvalCase(batch: FeedbackOptimizationBatchRecord, evalCase: EvalCaseRecord) {
    return updateBatchEvalCase(batch, evalCase, { status: "archived" });
  }

  async function removeBatchEvalCase(batch: FeedbackOptimizationBatchRecord, evalCaseId: string) {
    setActionId(`batch-eval-remove:${evalCaseId}`);
    try {
      const updated = await removeFeedbackOptimizationBatchEvalCase(clientConfig, batch.batch_id, evalCaseId);
      setToast(`已从批次移除回归用例 ${shortId(evalCaseId)}`);
      setSelectedBatchId(updated.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "移除回归用例失败");
      return false;
    } finally {
      setActionId(null);
    }
  }

  return {
    actionId,
    batchPlanGenerateDraft,
    setBatchPlanGenerateDraft,
    batchPlanGenerateBusy,
    toggleSource,
    generateEvalCasesFromSelection,
    createBatchFromSelection,
    runBatchAttribution,
    openBatchPlanGeneration,
    submitBatchPlanGeneration,
    executeBatchPlanAll,
    discardAgentWorkspaceChanges,
    saveAgentWorkspaceSnapshot,
    rollbackBatchExecution,
    executePlanTask,
    updatePlanTask,
    runBatchRegression,
    createBatchEvalCase,
    updateBatchEvalCase,
    archiveBatchEvalCase,
    removeBatchEvalCase,
  };
}

function repositoryChangedPaths(repository: AgentRepositoryStatus | null | undefined): string[] {
  const files = Array.isArray(repository?.changed_files) ? repository.changed_files : [];
  return files
    .map((item) => item?.path)
    .filter((path): path is string => typeof path === "string" && Boolean(path));
}

function pollWindowMs(job: AgentJobRecord): number {
  const timeoutSeconds = Number(job.timeout_seconds || 0);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) return DEFAULT_AGENT_JOB_POLL_WINDOW_MS;
  const withBuffer = (timeoutSeconds + AGENT_JOB_POLL_TIMEOUT_BUFFER_SECONDS) * 1000;
  return Math.min(MAX_AGENT_JOB_POLL_WINDOW_MS, Math.max(DEFAULT_AGENT_JOB_POLL_WINDOW_MS, withBuffer));
}
