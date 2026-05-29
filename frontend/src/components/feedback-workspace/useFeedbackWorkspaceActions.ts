import { useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import {
  applyOptimizationExecutionJob,
  createFeedbackOptimizationBatch,
  createOptimizationExecutionJob,
  executeFeedbackOptimizationPlanTask,
  generateFeedbackOptimizationBatchPlan,
  generateFeedbackSourceEvalCases,
  rejectFeedbackOptimizationBatchPlan,
  runFeedbackOptimizationBatchAttribution,
  runFeedbackOptimizationBatchRegression,
  restoreExecutionCompensation,
} from "../../api/runtime";
import type {
  ExecutionCompensationRecord,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackSourceRef,
  OptimizationTaskRecord,
} from "../../types/feedback";
import type { ExternalFeedbackWorkspaceProps } from "./types";
import {
  shortId,
  type SourceRow,
} from "./selectors";
import type { MenuKey } from "./useFeedbackWorkspaceState";

type StateSetter<T> = Dispatch<SetStateAction<T>>;

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
      setToast(`已生成/复用回归用例：新增 ${result.created}，复用 ${result.reused}`);
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
      setToast(`已创建优化批次 ${shortId(batch.batch_id)}`);
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
      setToast(`${force ? "已重新归因" : "批次归因完成"}：${result.jobs.length} 个当前 job`);
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
      const updated = await generateFeedbackOptimizationBatchPlan(
        clientConfig,
        batch.batch_id,
        instruction ? { regeneration_instruction: instruction } : undefined,
      );
      setToast(`${batch.optimization_plan ? "已重新生成优化方案" : "已生成优化方案"}：${updated.optimization_plan?.status || updated.status}`);
      setSelectedBatchId(updated.batch_id);
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
        setToast(`任务已执行：${result.plan_task?.status || result.execution_job?.status || result.batch.status}`);
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
      const result = await runFeedbackOptimizationBatchRegression(clientConfig, batch.batch_id);
      setToast(`批次回归测试完成：${result.eval_run.result_status || result.eval_run.status}`);
      setSelectedBatchId(result.batch?.batch_id || batch.batch_id);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批次回归测试失败");
    } finally {
      setActionId(null);
    }
  }

  async function createExecutionJob(task: OptimizationTaskRecord, force = false) {
    setActionId(`execution:${task.optimization_task_id}`);
    try {
      const job = await createOptimizationExecutionJob(clientConfig, task.optimization_task_id, force);
      setToast(`执行方案 ${shortId(job.execution_job_id)}：${job.status}`);
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
    if (task.latest_execution_job?.status !== "ready") {
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
    createExecutionJob,
    applyExecutionJob,
    submitExecutionApply,
    restoreCompensation,
  };
}
