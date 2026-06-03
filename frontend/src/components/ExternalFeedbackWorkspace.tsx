import {
  CheckCircle2,
  GitBranch,
  Loader2,
  Search,
} from "lucide-react";
import { AgentVersionsWorkspace } from "./AgentVersionsWorkspace";
import { AttributionResult } from "./feedback-workspace/AttributionResult";
import { BatchesPanel } from "./feedback-workspace/BatchesWorkspace";
import { ExecutionApplyConfirmModal, InstructionModal } from "./feedback-workspace/FeedbackModals";
import { RegressionAssetsPanel } from "./feedback-workspace/RegressionAssetsWorkspace";
import { SignalsPanel } from "./feedback-workspace/SignalsWorkspace";
import { TasksDetails } from "./feedback-workspace/TasksDetails";
import { useFeedbackWorkspaceActions } from "./feedback-workspace/useFeedbackWorkspaceActions";
import { useFeedbackWorkspaceState, visibleMenuItems } from "./feedback-workspace/useFeedbackWorkspaceState";
import {
  sourceRowKey,
} from "./feedback-workspace/selectors";
import type { ExternalFeedbackWorkspaceProps } from "./feedback-workspace/types";

export function ExternalFeedbackWorkspace({
  clientConfig,
  runtimeContext,
  monitoringConfig,
  agentRepository,
  currentAgentRef,
  agentChangeSets = [],
  agentReleases = [],
  versionLoading = false,
  versionError,
  onRefreshVersions,
  refreshToken = 0,
  onFeedbackChanged,
}: ExternalFeedbackWorkspaceProps) {
  const {
    activeMenu,
    setActiveMenu,
    data,
    query,
    setQuery,
    selectedSourceIds,
    setSelectedSourceIds,
    setSelectedSourceKey,
    setSelectedBatchId,
    runtimeStatus,
    toast,
    setToast,
    refreshWorkbench,
    checkRuntime,
    sourceRows,
    visibleSources,
    selectedSource,
    visibleBatches,
    selectedBatch,
    visibleRegressionAssets,
  } = useFeedbackWorkspaceState({ clientConfig, refreshToken });
  const {
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
  } = useFeedbackWorkspaceActions({
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
  });

  return (
    <div className="fw-shell">
      <aside className="fw-sidebar">
        {visibleMenuItems.map((item) => (
          <button className={activeMenu === item.key ? "active" : ""} key={item.key} onClick={() => setActiveMenu(item.key)} type="button">
            {item.label}
            {item.key === "versions" && agentChangeSets.length > 0 ? <span className="fw-menu-badge">{agentChangeSets.length}</span> : null}
          </button>
        ))}
      </aside>

      <div className="fw-content">
        {activeMenu !== "versions" ? (
          <header className="fw-topbar fw-unified-topbar">
            <div className="fw-context-strip" aria-label="运行上下文">
              <span title={runtimeContext?.runId ?? "-"}>run_id：{runtimeContext?.runId ?? "-"}</span>
              <span title={runtimeContext?.sessionId ?? "-"}>session_id：{runtimeContext?.sessionId ?? "-"}</span>
              <span title={runtimeContext?.agentVersionId ?? "-"}>agent_version_id：{runtimeContext?.agentVersionId ?? "-"}</span>
              <span title={runtimeContext?.caseId ?? "-"}>case_id：{runtimeContext?.caseId ?? "-"}</span>
            </div>
            <div className="fw-header-actions">
              <label className="fw-local-search fw-signal-search">
                <Search size={16} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 ID、标签、Case" />
              </label>
              <button className="fw-small-secondary" onClick={checkRuntime} type="button">
                {runtimeStatus === "loading" ? <Loader2 size={16} className="fw-spin" /> : <CheckCircle2 size={16} />}
                Runtime
              </button>
            </div>
          </header>
        ) : null}

        {activeMenu === "signals" ? (
          <SignalsPanel
            rows={visibleSources}
            selectedIds={selectedSourceIds}
            selectedSource={selectedSource}
            actionId={actionId}
            onToggle={toggleSource}
            onSelectSource={(row) => setSelectedSourceKey(sourceRowKey(row))}
            onCreateBatch={createBatchFromSelection}
            onGenerateEvalCases={generateEvalCasesFromSelection}
          />
        ) : null}

        {activeMenu === "batches" ? (
          <BatchesPanel
            actionId={actionId}
            batches={visibleBatches}
            evalCases={data.eval_cases}
            externalWebhooks={data.external_webhooks}
            selectedBatch={selectedBatch}
            sources={data.sources}
            onArchiveEvalCase={archiveBatchEvalCase}
            onCreateEvalCase={createBatchEvalCase}
            onExecutePlanTask={executePlanTask}
            onGeneratePlan={openBatchPlanGeneration}
            onRemoveEvalCase={removeBatchEvalCase}
            onRejectPlan={rejectBatchPlan}
            onRunAttribution={runBatchAttribution}
            onRunRegression={runBatchRegression}
            onSelectBatch={(batch) => setSelectedBatchId(batch.batch_id)}
            onUpdateEvalCase={updateBatchEvalCase}
            renderAttributionResult={(output) => <AttributionResult output={output} />}
            renderBatchTasksDetails={(tasks) => (
              <TasksDetails
                clientConfig={clientConfig}
                tasks={tasks}
                actionId={actionId}
                variant="batch-plan"
                onCreateExecutionJob={createExecutionJob}
                onApplyExecutionJob={applyExecutionJob}
                onRestoreCompensation={restoreCompensation}
              />
            )}
          />
        ) : null}

        {activeMenu === "regression-assets" ? (
          <RegressionAssetsPanel
            actionId={actionId}
            assets={visibleRegressionAssets}
            clientConfig={clientConfig}
            onRefresh={refreshWorkbench}
            setToast={setToast}
          />
        ) : null}

        {activeMenu === "versions" ? (
          <AgentVersionsWorkspace
            clientConfig={clientConfig}
            repository={agentRepository || null}
            currentRef={currentAgentRef || null}
            changeSets={agentChangeSets}
            releases={agentReleases}
            loading={versionLoading}
            lastError={versionError}
            onRefresh={onRefreshVersions || (() => undefined)}
            embedded
          />
        ) : null}

        {activeMenu !== "versions" ? (
          <footer className="fw-info-bar">
            <GitBranch size={18} />
            <span>{"当前链路：反馈信息 -> 候选回归用例 -> 回归资产治理 -> 优化批次 -> 归因分析智能体 -> 优化方案生成智能体 -> 执行优化智能体 -> 批次回归测试。"}</span>
            {monitoringConfig?.langfuseUrl ? <a href={monitoringConfig.langfuseUrl} target="_blank" rel="noreferrer">Langfuse</a> : null}
          </footer>
        ) : null}
      </div>

      {batchPlanGenerateDraft ? (
        <InstructionModal
          ariaLabel="重新生成优化方案"
          busy={batchPlanGenerateBusy}
          description="重新生成会覆盖当前未审批优化方案，并使用补充要求生成新的方案。"
          label="补充要求"
          placeholder="补充本次优化方案生成要求，可留空"
          title="重新生成优化方案"
          value={batchPlanGenerateDraft.instruction}
          onCancel={() => setBatchPlanGenerateDraft(null)}
          onChange={(instruction) => setBatchPlanGenerateDraft((current) => (current ? { ...current, instruction } : current))}
          onSubmit={submitBatchPlanGeneration}
        />
      ) : null}

      {executionApplyDraft ? (
        <ExecutionApplyConfirmModal
          busy={executionApplyBusy}
          onCancel={() => setExecutionApplyDraft(null)}
          onConfirm={submitExecutionApply}
          task={executionApplyDraft.task}
        />
      ) : null}

      {toast ? <div className="fw-toast" key={toast.id} onAnimationEnd={() => setToast(null)}>{toast.message}</div> : null}
    </div>
  );
}
