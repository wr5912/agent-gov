import { useState, type Dispatch, type FormEvent, type SetStateAction } from "react";
import {
  applyOptimizationExecutionJob,
  createAttributionJob,
  createEvalRun,
  createEvidencePackage,
  createFeedbackOptimizationBatch,
  createOptimizationExecutionJob,
  createOptimizationTask,
  createProposalJob,
  executeFeedbackOptimizationPlanTask,
  generateFeedbackOptimizationBatchPlan,
  generateFeedbackSourceEvalCases,
  markOptimizationTaskApplied,
  notifyExternalGovernanceItem,
  regenerateAttributionJob,
  regenerateProposalJob,
  rejectFeedbackOptimizationBatchPlan,
  revalidateProposalOutput,
  reviewOptimizationProposal,
  runFeedbackOptimizationBatchAttribution,
  runFeedbackOptimizationBatchRegression,
  runOptimizationTaskRegression,
  restoreExecutionCompensation,
  syncFeedbackEvalDataset,
  updateEvalCase,
} from "../../api/runtime";
import type {
  EvalCaseUpdateRequest,
  ExecutionCompensationRecord,
  ExternalFeedbackWorkspaceProps,
  ExternalGovernanceItemRecord,
  FeedbackCaseRecord,
  FeedbackOptimizationBatchRecord,
  FeedbackOptimizationPlanTaskRecord,
  FeedbackSourceRef,
  OptimizationProposalRecord,
  OptimizationProposalReviewAction,
  OptimizationTaskRecord,
} from "../../types/feedback";
import type { AttributionDetailTab, CaseDetailView, CaseDetails } from "./CasesWorkspace";
import {
  isRetryableJobStatus,
  reviewComment,
  shortId,
  type SourceRow,
} from "./selectors";
import type { MenuKey } from "./useFeedbackWorkspaceState";

type StateSetter<T> = Dispatch<SetStateAction<T>>;

interface ProposalRegenerateDraft {
  feedbackCaseId: string;
  instruction: string;
}

interface BatchPlanGenerateDraft {
  batch: FeedbackOptimizationBatchRecord;
  instruction: string;
}

interface ExecutionApplyDraft {
  task: OptimizationTaskRecord;
}

interface ManualApplyDraft {
  task: OptimizationTaskRecord;
}

export function useFeedbackWorkspaceActions({
  clientConfig,
  onFeedbackChanged,
  onRefreshVersions,
  selectedSourceIds,
  setSelectedSourceIds,
  setSelectedCaseId,
  setSelectedBatchId,
  setActiveMenu,
  sourceRows,
  selectedCase,
  caseDetails,
  setCaseDetailView,
  setAttributionDetailTab,
  refreshWorkbench,
  tasksByProposalId,
  setToast,
}: {
  clientConfig: ExternalFeedbackWorkspaceProps["clientConfig"];
  onFeedbackChanged?: () => void;
  onRefreshVersions?: () => void | Promise<void>;
  selectedSourceIds: string[];
  setSelectedSourceIds: StateSetter<string[]>;
  setSelectedCaseId: StateSetter<string | null>;
  setSelectedBatchId: StateSetter<string | null>;
  setActiveMenu: StateSetter<MenuKey>;
  sourceRows: SourceRow[];
  selectedCase: FeedbackCaseRecord | null;
  caseDetails: CaseDetails;
  setCaseDetailView: StateSetter<CaseDetailView>;
  setAttributionDetailTab: StateSetter<AttributionDetailTab>;
  refreshWorkbench: () => Promise<void>;
  tasksByProposalId: Map<string, OptimizationTaskRecord>;
  setToast: StateSetter<string | null>;
}) {
  const [actionId, setActionId] = useState<string | null>(null);
  const [proposalRegenerateDraft, setProposalRegenerateDraft] = useState<ProposalRegenerateDraft | null>(null);
  const [batchPlanGenerateDraft, setBatchPlanGenerateDraft] = useState<BatchPlanGenerateDraft | null>(null);
  const [executionApplyDraft, setExecutionApplyDraft] = useState<ExecutionApplyDraft | null>(null);
  const [manualApplyDraft, setManualApplyDraft] = useState<ManualApplyDraft | null>(null);
  const proposalRegenerateBusy = Boolean(actionId?.startsWith("proposal-regenerate:"));
  const batchPlanGenerateBusy = Boolean(actionId?.startsWith("batch-plan:"));
  const executionApplyBusy = Boolean(actionId?.startsWith("execution-apply:"));
  const manualApplyBusy = Boolean(actionId?.startsWith("apply:"));

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

  async function runCaseAction(action: "evidence" | "attribution" | "proposal") {
    if (!selectedCase) return;
    if (action === "evidence" && selectedCase.evidence_package_ids.length) {
      setCaseDetailView("evidence");
      setToast("证据包已生成，可在详情中查看");
      return;
    }
    if (action === "attribution" && selectedCase.attribution_job_ids.length && !isRetryableJobStatus(caseDetails.attributionJob?.status)) {
      setAttributionDetailTab("result");
      setCaseDetailView("attribution");
      setToast("已有归因分析记录，可在详情中查看");
      return;
    }
    if (action === "proposal" && selectedCase.proposal_job_ids.length && !isRetryableJobStatus(caseDetails.proposalJob?.status)) {
      setCaseDetailView("proposal");
      setToast("已有优化方案生成记录，可在详情中查看");
      return;
    }
    setActionId(`${action}:${selectedCase.feedback_case_id}`);
    try {
      if (action === "evidence") {
        const evidence = await createEvidencePackage(clientConfig, selectedCase.feedback_case_id);
        setToast(`已生成证据包 ${shortId(evidence.evidence_package_id)}`);
        setCaseDetailView("evidence");
      } else if (action === "attribution") {
        const job = await createAttributionJob(clientConfig, selectedCase.feedback_case_id);
        setToast(`已完成归因 job ${shortId(job.job_id)}：${job.status}`);
        setCaseDetailView("attribution");
      } else {
        const job = await createProposalJob(clientConfig, selectedCase.feedback_case_id);
        setToast(`已完成建议 job ${shortId(job.job_id)}：${job.status}`);
        setCaseDetailView("proposal");
      }
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "处置动作失败");
    } finally {
      setActionId(null);
    }
  }

  async function reviewProposal(proposalId: string, action: OptimizationProposalReviewAction) {
    setActionId(`${action}:${proposalId}`);
    try {
      await reviewOptimizationProposal(clientConfig, proposalId, {
        action,
        comment: reviewComment(action),
      });
      setToast(action === "approve" ? "建议已批准" : action === "reject" ? "建议已拒绝" : "已要求补充分析");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "建议审批失败");
    } finally {
      setActionId(null);
    }
  }

  async function revalidateProposalJob(jobId: string) {
    setActionId(`proposal-revalidate:${jobId}`);
    try {
      const job = await revalidateProposalOutput(clientConfig, jobId);
      setToast(`已重新校验建议 job ${shortId(job.job_id)}：${job.status}`);
      setCaseDetailView("proposal");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新校验建议失败");
    } finally {
      setActionId(null);
    }
  }

  async function regenerateProposal(feedbackCaseId: string) {
    setProposalRegenerateDraft({ feedbackCaseId, instruction: "" });
  }

  async function submitProposalRegenerate(event: FormEvent) {
    event.preventDefault();
    if (!proposalRegenerateDraft) return;
    const { feedbackCaseId } = proposalRegenerateDraft;
    const instruction = proposalRegenerateDraft.instruction.trim();
    setActionId(`proposal-regenerate:${feedbackCaseId}`);
    setCaseDetailView("proposal");
    setProposalRegenerateDraft(null);
    setToast("已提交重新生成优化方案请求");
    const regeneratePromise = regenerateProposalJob(clientConfig, feedbackCaseId, {
      regeneration_instruction: instruction,
    });
    window.setTimeout(() => {
      void refreshWorkbench();
    }, 500);
    try {
      const job = await regeneratePromise;
      setToast(`已重新生成建议 job ${shortId(job.job_id)}：${job.status}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新生成建议失败");
      await refreshWorkbench();
    } finally {
      setActionId(null);
    }
  }

  async function regenerateAttribution(feedbackCaseId: string) {
    setActionId(`attribution-regenerate:${feedbackCaseId}`);
    try {
      const job = await regenerateAttributionJob(clientConfig, feedbackCaseId);
      setToast(`已重新启动归因 job ${shortId(job.job_id)}：${job.status}`);
      setAttributionDetailTab("result");
      setCaseDetailView("attribution");
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "重新归因失败");
    } finally {
      setActionId(null);
    }
  }

  function openTask(task: OptimizationTaskRecord) {
    if (task.feedback_case_id) {
      setSelectedCaseId(task.feedback_case_id);
      setCaseDetailView("tasks");
      setActiveMenu("batches");
      return;
    }
    setActiveMenu("batches");
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

  async function createTask(proposal: OptimizationProposalRecord) {
    const existingTask = tasksByProposalId.get(proposal.proposal_id);
    if (existingTask) {
      openTask(existingTask);
      setToast(`已打开优化任务 ${shortId(existingTask.optimization_task_id)}`);
      return;
    }
    setActionId(`task:${proposal.proposal_id}`);
    try {
      const task = await createOptimizationTask(clientConfig, {
        proposal_id: proposal.proposal_id,
        execution_mode: "manual_or_patch",
        comment: `由 proposal ${proposal.proposal_id} 创建。`,
      });
      setToast(`已创建优化任务 ${shortId(task.optimization_task_id)}`);
      openTask(task);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "创建优化任务失败");
    } finally {
      setActionId(null);
    }
  }

  function markTaskApplied(task: OptimizationTaskRecord) {
    if (task.applied_agent_version_id) {
      setToast("当前任务已创建应用版本");
      return;
    }
    setManualApplyDraft({ task });
  }

  async function submitManualApply() {
    if (!manualApplyDraft) return;
    const task = manualApplyDraft.task;
    setActionId(`apply:${task.optimization_task_id}`);
    try {
      const updated = await markOptimizationTaskApplied(clientConfig, task.optimization_task_id, `由反馈处置界面确认任务 ${task.optimization_task_id} 已应用。`);
      setToast(`已创建版本快照 ${shortId(updated.applied_agent_version_id)}`);
      setManualApplyDraft(null);
      await refreshWorkbench();
      await onRefreshVersions?.();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "标记已应用失败");
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

  async function runTaskRegression(task: OptimizationTaskRecord) {
    setActionId(`regression:${task.optimization_task_id}`);
    try {
      const run = await runOptimizationTaskRegression(clientConfig, task.optimization_task_id);
      setToast(`回归验证完成：${run.result_status || run.status}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "回归验证失败");
    } finally {
      setActionId(null);
    }
  }

  async function notifyExternalItem(item: ExternalGovernanceItemRecord, webhookAlias: string) {
    setActionId(`external-notify:${item.external_item_id}`);
    try {
      const updated = await notifyExternalGovernanceItem(clientConfig, item.external_item_id, webhookAlias);
      const statusText = updated.status === "notified" ? "已通知外部系统" : "通知失败";
      setToast(`${statusText}：${webhookAlias}`);
      await refreshWorkbench();
      onFeedbackChanged?.();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "通知外部系统失败");
    } finally {
      setActionId(null);
    }
  }

  async function syncEvalDataset(feedbackCaseId?: string) {
    setActionId(feedbackCaseId ? `sync-eval:${feedbackCaseId}` : "sync-eval");
    try {
      const result = await syncFeedbackEvalDataset(clientConfig, feedbackCaseId);
      setToast(`已同步评估集：新增 ${result.created}，复用 ${result.reused}`);
      await refreshWorkbench();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "同步评估集失败");
    } finally {
      setActionId(null);
    }
  }

  async function runDatasetEval() {
    setActionId("dataset-eval");
    try {
      const run = await createEvalRun(clientConfig);
      setToast(`批量评估完成：${run.result_status || run.status}`);
      await refreshWorkbench();
    } catch (error) {
      setToast(error instanceof Error ? error.message : "批量评估失败");
    } finally {
      setActionId(null);
    }
  }

  async function updateEvalCaseRecord(evalCaseId: string, payload: EvalCaseUpdateRequest): Promise<boolean> {
    setActionId(`eval-case:${evalCaseId}`);
    try {
      const updated = await updateEvalCase(clientConfig, evalCaseId, payload);
      setToast(`已更新评估用例 ${shortId(updated.eval_case_id)}`);
      setCaseDetailView("evals");
      await refreshWorkbench();
      onFeedbackChanged?.();
      return true;
    } catch (error) {
      setToast(error instanceof Error ? error.message : "更新评估用例失败");
      return false;
    } finally {
      setActionId(null);
    }
  }

  return {
    actionId,
    proposalRegenerateDraft,
    setProposalRegenerateDraft,
    batchPlanGenerateDraft,
    setBatchPlanGenerateDraft,
    executionApplyDraft,
    setExecutionApplyDraft,
    manualApplyDraft,
    setManualApplyDraft,
    proposalRegenerateBusy,
    batchPlanGenerateBusy,
    executionApplyBusy,
    manualApplyBusy,
    toggleSource,
    generateEvalCasesFromSelection,
    createBatchFromSelection,
    runCaseAction,
    reviewProposal,
    revalidateProposalJob,
    regenerateProposal,
    submitProposalRegenerate,
    regenerateAttribution,
    openTask,
    runBatchAttribution,
    openBatchPlanGeneration,
    submitBatchPlanGeneration,
    executePlanTask,
    rejectBatchPlan,
    runBatchRegression,
    createTask,
    markTaskApplied,
    submitManualApply,
    createExecutionJob,
    applyExecutionJob,
    submitExecutionApply,
    restoreCompensation,
    runTaskRegression,
    notifyExternalItem,
    syncEvalDataset,
    runDatasetEval,
    updateEvalCaseRecord,
  };
}
