import { useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import {
  applyOptimizationExecutionJob,
  createFeedbackOptimizationBatch,
  createFeedbackOptimizationBatchEvalCase,
  createFeedbackOptimizationBatchRegressionPlan,
  createOptimizationExecutionJob,
  executeFeedbackOptimizationPlanTask,
  generateFeedbackOptimizationBatchPlan,
  generateFeedbackSourceEvalCases,
  getAgentJob,
  removeFeedbackOptimizationBatchEvalCase,
  rejectFeedbackOptimizationBatchPlan,
  runFeedbackOptimizationBatchAttribution,
  runFeedbackOptimizationBatchRegression,
  restoreExecutionCompensation,
  updateFeedbackOptimizationBatchEvalCase,
} from "../../api/runtime";
import type {
  AgentJobRecord,
  EvalCaseRecord,
  EvalCaseUpdateRequest,
  ExecutionCompensationRecord,
  FeedbackOptimizationBatchEvalCaseCreateRequest,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackSourceRef,
  OptimizationTaskRecord,
} from "../../types/feedback";
import type { ExternalFeedbackWorkspaceProps } from "./types";
import {
  executionPlanReady,
  shortId,
  type SourceRow,
} from "./selectors";
import type { MenuKey } from "./useFeedbackWorkspaceState";

type StateSetter<T> = Dispatch<SetStateAction<T>>;

const TERMINAL_AGENT_JOB_STATUSES = new Set(["completed", "failed", "needs_human_review", "timeout"]);
const AGENT_JOB_POLL_INTERVAL_MS = 1500;
const AGENT_JOB_POLL_ATTEMPTS = 80;

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

interface BatchPlanGenerateDraft {
  batch: FeedbackOptimizationBatchRecord;
  instruction: string;
}

interface ExecutionApplyDraft {
  task: OptimizationTaskRecord;
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
  const [executionApplyDraft, setExecutionApplyDraft] = useState<ExecutionApplyDraft | null>(null);
  const batchPlanGenerateBusy = Boolean(actionId?.startsWith("batch-plan:"));
  const executionApplyBusy = Boolean(actionId?.startsWith("execution-apply:"));

  async function waitForAgentJob(jobId?: string | null) {
    if (!jobId) return null;
    let latest: AgentJobRecord | null = null;
    for (let attempt = 0; attempt < AGENT_JOB_POLL_ATTEMPTS; attempt += 1) {
      latest = await getAgentJob(clientConfig, jobId);
      if (TERMINAL_AGENT_JOB_STATUSES.has(String(latest.status))) return latest;
      await delay(AGENT_JOB_POLL_INTERVAL_MS);
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
      const completed = await waitForAgentJob(job.job_id);
      setToast(`${batch.optimization_plan ? "重新生成优化方案" : "优化方案生成"} ${completed?.status || job.status}：${shortId(job.job_id)}`);
      setSelectedBatchId(batch.batch_id);
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
        setToast(`任务执行方案 ${completed?.status || result.execution_job?.status || result.batch.status}`);
      }
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "执行方案任务失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function rejectBatchPlan(batch: FeedbackOptimizationBatchRecord) {
    setActionId(`batch-reject:${batch.batch_id}`);
    try {
      const updated = await rejectFeedbackOptimizationBatchPlan(clientConfig, batch.batch_id, `拒绝优化批次 ${batch.batch_id}`);
      setToast("已拒绝优化方案");
      setSelectedBatchId(updated.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "拒绝优化方案失败");
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

  async function createExecutionJob(task: OptimizationTaskRecord, force = false) {
    setActionId(`execution:${task.optimization_task_id}`);
    try {
      const job = await createOptimizationExecutionJob(clientConfig, task.optimization_task_id, force);
      const completed = await waitForAgentJob(job.job_id);
      setToast(`执行方案生成 ${completed?.status || job.status}：${shortId(job.job_id)}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "生成执行方案失败");
    } finally {
      setActionId(null);
    }
  }

  function applyExecutionJob(task: OptimizationTaskRecord) {
    const jobId = task.latest_execution_job?.execution_job_id || task.latest_execution_job_id;
    if (!jobId) {
      setToast("当前任务没有可应用的执行方案");
      return;
    }
    if (!executionPlanReady(task.latest_execution_job)) {
      setToast("执行方案尚未 ready，不能应用");
      return;
    }
    setExecutionApplyDraft({ task });
  }

  async function submitExecutionApply() {
    if (!executionApplyDraft) return;
    const task = executionApplyDraft.task;
    const jobId = task.latest_execution_job?.execution_job_id || task.latest_execution_job_id;
    if (!jobId) {
      setToast("当前任务没有可应用的执行方案");
      setExecutionApplyDraft(null);
      return;
    }
    setActionId(`execution-apply:${task.optimization_task_id}`);
    try {
      const result = await applyOptimizationExecutionJob(clientConfig, task.optimization_task_id, jobId);
      setToast(`已应用执行方案，生成版本 ${shortId(result.optimization_task?.applied_agent_version_id)}`);
      setExecutionApplyDraft(null);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "应用执行方案失败");
    } finally {
      setActionId(null);
    }
  }

  async function restoreCompensation(compensation: ExecutionCompensationRecord) {
    setActionId(`compensation-restore:${compensation.compensation_id}`);
    try {
      const updated = await restoreExecutionCompensation(clientConfig, compensation.compensation_id);
      setToast(`已恢复补偿记录 ${shortId(updated.compensation_id)}`);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "恢复补偿记录失败");
    } finally {
      setActionId(null);
    }
  }

  return {
    actionId,
    batchPlanGenerateDraft,
    setBatchPlanGenerateDraft,
    executionApplyDraft,
    setExecutionApplyDraft,
    batchPlanGenerateBusy,
    executionApplyBusy,
    toggleSource,
    generateEvalCasesFromSelection,
    createBatchFromSelection,
    runBatchAttribution,
    openBatchPlanGeneration,
    submitBatchPlanGeneration,
    executePlanTask,
    rejectBatchPlan,
    runBatchRegression,
    createBatchEvalCase,
    updateBatchEvalCase,
    archiveBatchEvalCase,
    removeBatchEvalCase,
    createExecutionJob,
    applyExecutionJob,
    submitExecutionApply,
    restoreCompensation,
  };
}
