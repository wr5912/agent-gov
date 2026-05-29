import {
  CheckCircle2,
  GitBranch,
  Loader2,
  Search,
} from "lucide-react";
import { AgentVersionsWorkspace } from "./AgentVersionsWorkspace";
import { AnalysisJobRecordList, AttributionDetails, AttributionResult } from "./feedback-workspace/AttributionDetails";
import { BatchesPanel } from "./feedback-workspace/BatchesWorkspace";
import { CasesPanel, type CaseDetails } from "./feedback-workspace/CasesWorkspace";
import { EvalCaseDetails, EvalPanel } from "./feedback-workspace/EvalWorkspace";
import { EvidencePackageDetails, RunsDetails } from "./feedback-workspace/EvidenceRunsDetails";
import { ExecutionApplyConfirmModal, InstructionModal, ManualApplyConfirmModal } from "./feedback-workspace/FeedbackModals";
import { ProposalDetails } from "./feedback-workspace/ProposalWorkspace";
import { SignalsPanel } from "./feedback-workspace/SignalsWorkspace";
import { TasksDetails } from "./feedback-workspace/TasksDetails";
import { useFeedbackWorkspaceActions } from "./feedback-workspace/useFeedbackWorkspaceActions";
import { useFeedbackWorkspaceState, visibleMenuItems } from "./feedback-workspace/useFeedbackWorkspaceState";
import {
  DetailMetricGrid,
  FormattedTextSection,
} from "./feedback-workspace/common";
import {
  shortId,
  sourceRowKey,
} from "./feedback-workspace/selectors";
import type { ExternalFeedbackWorkspaceProps } from "../types/feedback";

export function ExternalFeedbackWorkspace({
  clientConfig,
  runtimeContext,
  monitoringConfig,
  currentAgentVersion,
  agentVersions = [],
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
    setSelectedCaseId,
    setSelectedBatchId,
    caseDetailView,
    setCaseDetailView,
    attributionDetailTab,
    setAttributionDetailTab,
    caseDetails,
    detailsLoading,
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
    visibleCases,
    selectedCase,
    selectedCaseRuns,
    selectedCaseProposals,
    selectedCaseTasks,
    selectedCaseExternalItems,
    selectedCaseEvalCases,
    tasksByProposalId,
  } = useFeedbackWorkspaceState({ clientConfig, refreshToken });
  const {
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
  } = useFeedbackWorkspaceActions({
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
  });

  return (
    <div className="fw-shell">
      <aside className="fw-sidebar">
        {visibleMenuItems.map((item) => (
          <button className={activeMenu === item.key ? "active" : ""} key={item.key} onClick={() => setActiveMenu(item.key)} type="button">
            {item.label}
            {item.key === "versions" && agentVersions.length > 0 ? <span className="fw-menu-badge">{agentVersions.length}</span> : null}
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
            externalWebhooks={data.external_webhooks}
            selectedBatch={selectedBatch}
            sources={data.sources}
            onExecutePlanTask={executePlanTask}
            onGeneratePlan={openBatchPlanGeneration}
            onRejectPlan={rejectBatchPlan}
            onRunAttribution={runBatchAttribution}
            onRunRegression={runBatchRegression}
            onSelectBatch={(batch) => setSelectedBatchId(batch.batch_id)}
            renderAttributionResult={(output) => <AttributionResult output={output} />}
            renderBatchTasksDetails={(tasks) => (
              <TasksDetails
                clientConfig={clientConfig}
                tasks={tasks}
                actionId={actionId}
                onCreateExecutionJob={createExecutionJob}
                onApplyExecutionJob={applyExecutionJob}
                onRestoreCompensation={restoreCompensation}
              />
            )}
          />
        ) : null}

        {activeMenu === "cases" ? (
          <CasesPanel
            cases={visibleCases}
            selectedCase={selectedCase}
            selectedCaseProposals={selectedCaseProposals}
            selectedCaseTasks={selectedCaseTasks}
            selectedCaseExternalItems={selectedCaseExternalItems}
            details={caseDetails}
            detailView={caseDetailView}
            detailsLoading={detailsLoading}
            actionId={actionId}
            onSelectCase={(feedbackCase) => {
              setSelectedCaseId(feedbackCase.feedback_case_id);
              setCaseDetailView("summary");
              setAttributionDetailTab("result");
            }}
            onSelectDetailView={setCaseDetailView}
            onOpenAttributionTab={(tab) => {
              setCaseDetailView("attribution");
              setAttributionDetailTab(tab);
            }}
            onCreateEvidence={() => runCaseAction("evidence")}
            onRunAttribution={() => runCaseAction("attribution")}
            onRunProposal={() => runCaseAction("proposal")}
            onRegenerateAttribution={regenerateAttribution}
            onRegenerateProposal={regenerateProposal}
            onRevalidateProposalJob={revalidateProposalJob}
            selectedCaseEvalCases={selectedCaseEvalCases}
            renderDetailContent={(view) => {
              switch (view) {
                case "summary":
                  return <CaseSummaryDetails details={caseDetails} />;
                case "evidence":
                  return <EvidencePackageDetails clientConfig={clientConfig} packages={caseDetails.evidencePackages || []} />;
                case "attribution":
                  return (
                    <AttributionDetails
                      actionId={actionId}
                      activeTab={attributionDetailTab}
                      jobs={caseDetails.attributionJobs || []}
                      output={caseDetails.attribution}
                      onRegenerateAttribution={regenerateAttribution}
                      onTabChange={setAttributionDetailTab}
                    />
                  );
                case "proposal":
                  return (
                    <ProposalDetails
                      actionId={actionId}
                      jobs={caseDetails.proposalJobs || []}
                      output={caseDetails.proposal}
                      proposals={selectedCaseProposals}
                      externalGovernanceItems={selectedCaseExternalItems}
                      externalWebhooks={data.external_webhooks}
                      onCreateTask={createTask}
                      onNotifyExternalItem={notifyExternalItem}
                      onOpenEvidence={() => setCaseDetailView("evidence")}
                      onOpenTask={openTask}
                      onReviewProposal={reviewProposal}
                      renderJobRecords={(jobs) => <AnalysisJobRecordList jobs={jobs} />}
                      tasksByProposalId={tasksByProposalId}
                    />
                  );
                case "runs":
                  return <RunsDetails runs={selectedCaseRuns} />;
                case "tasks":
                  return (
                    <TasksDetails
                      clientConfig={clientConfig}
                      tasks={selectedCaseTasks}
                      actionId={actionId}
                      onMarkApplied={markTaskApplied}
                      onCreateExecutionJob={createExecutionJob}
                      onApplyExecutionJob={applyExecutionJob}
                      onRestoreCompensation={restoreCompensation}
                      onRunRegression={runTaskRegression}
                    />
                  );
                case "evals":
                  return <EvalCaseDetails actionId={actionId} evalCases={selectedCaseEvalCases} evalRuns={data.eval_runs} onUpdateEvalCase={updateEvalCaseRecord} />;
                default:
                  return null;
              }
            }}
          />
        ) : null}

        {activeMenu === "evals" ? (
          <EvalPanel
            evalCases={data.eval_cases}
            evalRuns={data.eval_runs}
            actionId={actionId}
            selectedCase={selectedCase}
            selectedCaseEvalCases={selectedCaseEvalCases}
            onSyncDataset={syncEvalDataset}
            onRunDatasetEval={runDatasetEval}
          />
        ) : null}

        {activeMenu === "versions" ? (
          <AgentVersionsWorkspace
            clientConfig={clientConfig}
            currentVersion={currentAgentVersion || null}
            versions={agentVersions}
            loading={versionLoading}
            lastError={versionError}
            onRefresh={onRefreshVersions || (() => undefined)}
            embedded
          />
        ) : null}

        {activeMenu !== "versions" ? (
          <footer className="fw-info-bar">
            <GitBranch size={18} />
            <span>{"当前链路：反馈信息 -> 默认回归用例 -> 优化批次 -> 归因分析智能体-> 优化方案生成智能体-> 执行优化智能体-> 批次回归测试。"}</span>
            {monitoringConfig?.langfuseUrl ? <a href={monitoringConfig.langfuseUrl} target="_blank" rel="noreferrer">Langfuse</a> : null}
          </footer>
        ) : null}
      </div>

      {proposalRegenerateDraft ? (
        <InstructionModal
          ariaLabel="重新生成优化方案"
          busy={proposalRegenerateBusy}
          description="重新生成会废弃当前反馈单中未审批、未通知的旧建议，并保留历史记录。"
          label="补充指令"
          placeholder="补充本次生成指令，可留空"
          title="重新生成优化方案"
          value={proposalRegenerateDraft.instruction}
          onCancel={() => setProposalRegenerateDraft(null)}
          onChange={(instruction) => setProposalRegenerateDraft((current) => (current ? { ...current, instruction } : current))}
          onSubmit={submitProposalRegenerate}
        />
      ) : null}

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

      {manualApplyDraft ? (
        <ManualApplyConfirmModal
          busy={manualApplyBusy}
          currentVersion={currentAgentVersion || null}
          onCancel={() => setManualApplyDraft(null)}
          onConfirm={submitManualApply}
          task={manualApplyDraft.task}
        />
      ) : null}

      {toast ? <div className="fw-toast" onAnimationEnd={() => setToast(null)}>{toast}</div> : null}
    </div>
  );
}

function CaseSummaryDetails({ details }: { details: CaseDetails }) {
  return (
    <div className="fw-detail-stack">
      <DetailMetricGrid
        items={[
          ["evidence_package_id", shortId(details.evidence?.evidence_package_id)],
          ["main_agent_version_id", shortId(details.evidence?.main_agent_version_id)],
          ["attribution_status", details.attributionJob?.status || "-"],
          ["proposal_status", details.proposalJob?.status || "-"],
          ["problem_type", details.attribution?.problem_type || "-"],
          ["actionability", details.attribution?.actionability || "-"],
        ]}
      />
      {details.attribution ? (
        <FormattedTextSection title="根因摘要" value={details.attribution.rationale || "暂无归因说明"} compact />
      ) : (
        <div className="fw-empty-inline">暂无已校验归因输出</div>
      )}
    </div>
  );
}
