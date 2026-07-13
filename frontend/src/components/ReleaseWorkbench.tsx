import { useEffect, useMemo, useState } from "react";
import {
  publishAgentChangeSet,
  retryAgentChangeSetWorktreeCleanup,
  reviewAgentChangeSetRegression,
  runAgentChangeSetRegression,
  type RegressionReviewDecision,
} from "../api/runtime";
import { listTestDatasets, type TestDataset } from "../api/assets";
import type { AgentChangeSet, AgentRelease } from "../types/runtime";
import type { RuntimeClientConfig } from "../types/runtime";
import { formatEvalResultStatus } from "../utils/domainLabels";
import "../improvement-workbench.css";

// 发布工作台（四阶段改进治理 §12）：回答「能不能发 / 为什么 / 发了包含什么」，并呈现三门门禁与动作。
type WithAgent = { agent_id: string };

const CHANGESET_TERMINAL = new Set(["published", "abandoned", "rejected", "failed"]);
const CHANGESET_BLOCKED = new Set(["regression_failed", "rejected", "failed"]);
const CHANGESET_READY = new Set(["regression_passed", "approved", "candidate_committed"]);
const CHANGESET_FORCEABLE = new Set(["regression_failed"]);
const CHANGESET_REGRESSION_RUNNABLE = new Set(["candidate_committed", "pending_approval", "approved", "regression_review_required", "regression_failed"]);
const EXECUTED = new Set(["candidate_committed", "regression_review_required", "regression_passed", "regression_failed", "approved", "publishing", "published"]);
const REGRESSION_PASS = new Set(["regression_passed", "approved", "published"]);

type GateState = "pass" | "fail" | "pending" | "optional" | "unknown" | "not_applicable";
type ReviewDecision = RegressionReviewDecision["decision"] | "";

function hasUnresolvedRegressionReview(changeSet: AgentChangeSet | null): boolean {
  return changeSet?.latest_eval_run?.gate_result.status === "review_required";
}

function isForcePublishTarget(changeSet: AgentChangeSet | null): changeSet is AgentChangeSet {
  return Boolean(
    changeSet?.candidate_commit_sha
    && CHANGESET_FORCEABLE.has(String(changeSet.status))
    && !changeSet.publication_provenance_blocker
    && !hasUnresolvedRegressionReview(changeSet),
  );
}

function scopedBy<T extends WithAgent>(items: T[], agentId: string): T[] {
  if (!agentId) return items;
  return items.filter((item) => item.agent_id === agentId);
}

function deriveGates(changeSet: AgentChangeSet | null): { id: string; label: string; state: GateState }[] {
  const has = Boolean(changeSet);
  const executed = Boolean(changeSet && EXECUTED.has(String(changeSet.status)));
  const provenanceBlocked = Boolean(changeSet?.publication_provenance_blocker);
  const regPass = Boolean(changeSet && REGRESSION_PASS.has(String(changeSet.status)));
  const regFail = Boolean(changeSet && CHANGESET_BLOCKED.has(String(changeSet.status)));
  const regReview = changeSet?.status === "regression_review_required";
  const attributionStatus = String(changeSet?.source_attribution_status || "");
  const attributionState: GateState = !changeSet
    ? "pending"
    : !changeSet.source_improvement_id
      ? "not_applicable"
      : !changeSet.source_attribution_id || !attributionStatus
        ? "unknown"
        : attributionStatus === "confirmed"
          ? "pass"
          : "pending";
  return [
    { id: "attribution", label: attributionStatus ? `归因（${attributionStatus}）` : "归因证据", state: attributionState },
    { id: "optimization", label: "优化已执行", state: executed && !provenanceBlocked ? "pass" : "pending" },
    { id: "regression", label: "回归测试", state: regFail ? "fail" : regPass ? "pass" : regReview ? "pending" : has ? "optional" : "pending" },
  ];
}

const GATE_TEXT: Record<GateState, string> = {
  pass: "通过",
  fail: "未通过",
  pending: "未完成",
  optional: "可选",
  unknown: "未知",
  not_applicable: "不适用",
};

function overallGate(gates: { state: GateState }[], total: number): { label: string; tone: "success" | "danger" | "primary" | "muted"; reason: string } {
  if (total === 0) return { label: "无待发布变更", tone: "muted", reason: "当前范围还没有候选变更。先在「改进」里把事项推进到执行/回归。" };
  if (gates.some((g) => g.state === "fail")) return { label: "不可发布", tone: "danger", reason: "存在未通过的门禁，需先修复或重跑回归。" };
  if (gates.every((g) => g.state === "pass" || g.state === "optional" || g.state === "not_applicable")) return { label: "可发布", tone: "success", reason: "必需门禁已通过；回归未运行时可选执行。" };
  return { label: "进行中", tone: "primary", reason: "门禁尚未全部通过。" };
}

export function ReleaseWorkbench({
  clientConfig,
  scopeAgentId,
  sourceImprovementId,
  preferredChangeSetId,
  sourceTestDataset,
  releases,
  changeSets,
  readOnly = false,
  onRefresh,
}: {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
  sourceImprovementId: string;
  preferredChangeSetId?: string;
  sourceTestDataset?: TestDataset | null;
  releases: AgentRelease[];
  changeSets: AgentChangeSet[];
  readOnly?: boolean;
  onRefresh: () => void | Promise<void>;
}) {
  const [showChanges, setShowChanges] = useState(false);
  const [busyAction, setBusyAction] = useState<string | undefined>();
  const [confirmForceId, setConfirmForceId] = useState<string | undefined>();
  const [selectedChangeSetId, setSelectedChangeSetId] = useState<string | undefined>();
  const [actionMessage, setActionMessage] = useState<string | undefined>();
  const [actionError, setActionError] = useState<string | undefined>();
  const [testDatasets, setTestDatasets] = useState<TestDataset[]>([]);
  const [selectedDatasetId, setSelectedDatasetId] = useState("");
  const [reviewOperator, setReviewOperator] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [reviewDecisions, setReviewDecisions] = useState<Record<string, ReviewDecision>>({});
  const scopedChangeSets = scopedBy(changeSets as (AgentChangeSet & WithAgent)[], scopeAgentId)
    .filter((changeSet) => changeSet.source_improvement_id === sourceImprovementId);
  const relatedChangeSetIds = new Set(scopedChangeSets.map((changeSet) => changeSet.change_set_id));
  const scopedReleases = scopedBy(releases as (AgentRelease & WithAgent)[], scopeAgentId)
    .filter((release) => release.change_set_id && relatedChangeSetIds.has(release.change_set_id));
  const cleanupTargets = scopedChangeSets.filter((changeSet) => changeSet.worktree_cleanup_pending);
  const pendingChangeSets = scopedChangeSets.filter((cs) => !CHANGESET_TERMINAL.has(String(cs.status)));
  const selectedChangeSet = useMemo(
    () => pendingChangeSets.find((cs) => cs.change_set_id === selectedChangeSetId)
      || pendingChangeSets.find((cs) => cs.change_set_id === preferredChangeSetId)
      || pendingChangeSets[0]
      || null,
    [pendingChangeSets, preferredChangeSetId, selectedChangeSetId],
  );
  const gates = deriveGates(selectedChangeSet);
  const gate = overallGate(gates, selectedChangeSet ? 1 : 0);
  const regressionPending = gates.find((g) => g.id === "regression")?.state !== "pass" && Boolean(selectedChangeSet);
  const regressionTarget = selectedChangeSet?.candidate_commit_sha && CHANGESET_REGRESSION_RUNNABLE.has(String(selectedChangeSet.status)) ? selectedChangeSet : null;
  const latestEvalRun = selectedChangeSet?.latest_eval_run;
  const pendingReviewCaseIds = new Set(latestEvalRun?.gate_result.review_dataset_case_ids ?? []);
  const reviewItems = (latestEvalRun?.items ?? []).filter((item) => (
    item.status === "needs_human_review" && pendingReviewCaseIds.has(item.dataset_case_id)
  ));
  const hasPendingReview = hasUnresolvedRegressionReview(selectedChangeSet);
  const readyTarget = selectedChangeSet?.candidate_commit_sha
    && CHANGESET_READY.has(String(selectedChangeSet.status))
    && !selectedChangeSet.publication_blocker
    && !hasPendingReview
    ? selectedChangeSet
    : null;
  const retryTarget = selectedChangeSet?.candidate_commit_sha && selectedChangeSet.status === "publishing" ? selectedChangeSet : null;
  const forceTarget = isForcePublishTarget(selectedChangeSet) ? selectedChangeSet : null;
  const confirmedForceTarget = pendingChangeSets.find((cs) => (
    cs.change_set_id === confirmForceId
    && isForcePublishTarget(cs)
  )) || null;
  const canForce = Boolean(forceTarget);
  const selectedDataset = testDatasets.find((dataset) => dataset.dataset_id === selectedDatasetId) || null;
  const reviewComplete = hasPendingReview
    && Boolean(reviewOperator.trim())
    && Boolean(reviewReason.trim())
    && reviewItems.every((item) => Boolean(reviewDecisions[item.dataset_case_id]));
  const sourceTestDatasetVersion = sourceTestDataset
    ? `${sourceTestDataset.dataset_id}:${sourceTestDataset.lifecycle_state}:${sourceTestDataset.revision}`
    : "";

  useEffect(() => {
    setSelectedDatasetId("");
    setActionMessage(undefined);
    setActionError(undefined);
    setConfirmForceId(undefined);
  }, [sourceImprovementId]);

  useEffect(() => {
    if (!pendingChangeSets.length) {
      setSelectedChangeSetId(undefined);
      return;
    }
    if (!selectedChangeSetId || !pendingChangeSets.some((cs) => cs.change_set_id === selectedChangeSetId)) {
      const preferred = pendingChangeSets.find((changeSet) => changeSet.change_set_id === preferredChangeSetId);
      setSelectedChangeSetId(preferred?.change_set_id || pendingChangeSets[0].change_set_id);
    }
  }, [pendingChangeSets, preferredChangeSetId, selectedChangeSetId]);

  useEffect(() => {
    setReviewOperator("");
    setReviewReason("");
    setReviewDecisions({});
  }, [latestEvalRun?.eval_run_id, selectedChangeSet?.change_set_id]);

  useEffect(() => {
    const agentId = selectedChangeSet?.agent_id || scopeAgentId;
    if (!agentId) {
      setTestDatasets([]);
      setSelectedDatasetId("");
      return;
    }
    let cancelled = false;
    setTestDatasets([]);
    setSelectedDatasetId("");
    void listTestDatasets(clientConfig, { agentId, sourceImprovementId })
      .then((datasets) => {
        if (cancelled) return;
        const candidateVersion = selectedChangeSet?.candidate_commit_sha || "";
        const executionId = selectedChangeSet?.execution_job_id || "";
        const usable = datasets.filter((dataset) => (
          dataset.lifecycle_state === "active"
          && dataset.provenance.candidate_agent_version_id === candidateVersion
          && dataset.provenance.execution_id === executionId
        ));
        setTestDatasets(usable);
        const sourceMatch = usable.find((dataset) => dataset.source_improvement_id === sourceImprovementId);
        setSelectedDatasetId(sourceMatch?.dataset_id || usable[0]?.dataset_id || "");
      })
      .catch((error) => {
        if (cancelled) return;
        setTestDatasets([]);
        setSelectedDatasetId("");
        setActionError(error instanceof Error ? error.message : String(error));
      });
    return () => { cancelled = true; };
  }, [clientConfig, scopeAgentId, selectedChangeSet?.agent_id, selectedChangeSet?.candidate_commit_sha, selectedChangeSet?.execution_job_id, sourceImprovementId, sourceTestDatasetVersion]);

  const runAction = async (name: string, action: () => Promise<void>) => {
    setBusyAction(name);
    setActionError(undefined);
    setActionMessage(undefined);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
      try { await onRefresh(); } catch { /* keep the original action error visible */ }
    } finally {
      setBusyAction(undefined);
    }
  };

  const handleRunRegression = () => {
    if (!regressionTarget || !selectedDataset) return;
    void runAction("regression", async () => {
      const result = await runAgentChangeSetRegression(
        clientConfig,
        regressionTarget.change_set_id,
        selectedDataset.dataset_id,
        selectedDataset.cases.length,
      );
      setActionMessage(`已运行回归：${result.eval_run_id}（${result.result_status}）`);
      await onRefresh();
    });
  };

  const handleReviewRegression = () => {
    if (!selectedChangeSet || !latestEvalRun || !reviewComplete) return;
    const decisions = reviewItems.map((item): RegressionReviewDecision => ({
      dataset_case_id: item.dataset_case_id,
      decision: reviewDecisions[item.dataset_case_id] as RegressionReviewDecision["decision"],
    }));
    void runAction("regression-review", async () => {
      await reviewAgentChangeSetRegression(
        clientConfig,
        selectedChangeSet.change_set_id,
        latestEvalRun.eval_run_id,
        {
          review_id: `review-${latestEvalRun.eval_run_id}`,
          operator: reviewOperator.trim(),
          reason: reviewReason.trim(),
          scope: "current_eval_run",
          decisions,
        },
      );
      setActionMessage(`人工复核已提交：${latestEvalRun.eval_run_id}`);
      await onRefresh();
    });
  };

  const handleForcePublish = () => {
    if (!forceTarget) return;
    if (confirmForceId !== forceTarget.change_set_id) {
      setConfirmForceId(forceTarget.change_set_id);
      setShowChanges(true);
      return;
    }
    void confirmForcePublish();
  };

  const handleRetryCleanup = (changeSetId: string) => {
    void runAction(`cleanup-${changeSetId}`, async () => {
      await retryAgentChangeSetWorktreeCleanup(clientConfig, changeSetId);
      setActionMessage(`工作目录清理已完成：${changeSetId}`);
      await onRefresh();
    });
  };

  const handlePublish = () => {
    if (!readyTarget) return;
    void runAction("publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, readyTarget.change_set_id, {
        operator: "ui",
        force: false,
      });
      setActionMessage(`已发布：${release.release_id}`);
      await onRefresh();
    });
  };

  const handleRetryPublish = () => {
    if (!retryTarget) return;
    void runAction("retry-publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, retryTarget.change_set_id, {
        operator: "ui",
        force: false,
      });
      setActionMessage(`发布已完成：${release.release_id}`);
      await onRefresh();
    });
  };

  const confirmForcePublish = async () => {
    if (!confirmedForceTarget) return;
    void runAction("force-publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, confirmedForceTarget.change_set_id, {
        operator: "ui",
        force: true,
        note: "UI 强制发布：人工确认发布门禁风险可接受。",
      });
      setConfirmForceId(undefined);
      setActionMessage(`已强制发布：${release.release_id}`);
      await onRefresh();
    });
  };

  return (
    <section className="release-stage-workbench" data-testid="release-workbench">
      <header className="iw-stage-toolbar">
        <span>回归与发布 · {scopeAgentId}</span>
        <button className="iw-secondary-button" type="button" onClick={() => void onRefresh()}>刷新</button>
      </header>
      {actionError ? <div className="iw-error" data-testid="release-action-error">{actionError}</div> : null}
      {actionMessage ? <div className="iw-next-step" data-testid="release-action-message">{actionMessage}</div> : null}

      <div className="release-stage-band" data-testid="release-gate-workbench">
        <div className="release-stage-heading">
          <h4>发布门禁</h4>
          <span className={`iw-stage-pill ${gate.tone === "success" ? "is-done" : ""}`} data-testid="release-gate" data-state={gate.tone}>{gate.label}</span>
          <span>{gate.reason}</span>
        </div>
        {pendingChangeSets.length > 1 ? (
          <select
            className="iw-select select-inline"
            data-testid="release-changeset-select"
            value={selectedChangeSet?.change_set_id || ""}
            onChange={(event) => setSelectedChangeSetId(event.target.value)}
          >
            {pendingChangeSets.map((changeSet) => (
              <option key={changeSet.change_set_id} value={changeSet.change_set_id}>
                {changeSet.title || changeSet.change_set_id} · {changeSet.status}
              </option>
            ))}
          </select>
        ) : null}
        <div className="iw-source-refs">
          {gates.map((item) => (
            <span key={item.id} className="iw-stage-pill" data-testid={`release-gate-${item.id}`} data-state={item.state}>
              {item.label}：{GATE_TEXT[item.state]}
            </span>
          ))}
        </div>
        {!readOnly ? (
          <div className="iw-action-row">
            <select
              className="iw-select select-inline"
              data-testid="release-regression-dataset"
              value={selectedDatasetId}
              disabled={Boolean(busyAction) || testDatasets.length === 0}
              onChange={(event) => setSelectedDatasetId(event.target.value)}
            >
              <option value="">选择测试数据集</option>
              {testDatasets.map((dataset) => (
                <option key={dataset.dataset_id} value={dataset.dataset_id}>
                  {dataset.name} · {dataset.lifecycle_state} · r{dataset.revision}
                </option>
              ))}
            </select>
            <button
              className={regressionPending ? "iw-primary-button" : "iw-secondary-button"}
              type="button"
              data-testid="release-action-run-regression"
              disabled={!regressionPending || !regressionTarget || !selectedDatasetId || Boolean(busyAction)}
              onClick={handleRunRegression}
            >
              {busyAction === "regression" ? "回归中..." : "运行回归"}
            </button>
            <button className="iw-secondary-button" type="button" data-testid="release-action-view-changes" onClick={() => setShowChanges((value) => !value)}>
              {showChanges ? "收起变更" : "展开变更"}
            </button>
            <button
              className={readyTarget ? "iw-primary-button" : "iw-secondary-button"}
              type="button"
              data-testid="release-action-publish"
              disabled={!readyTarget || Boolean(busyAction)}
              onClick={handlePublish}
            >
              {busyAction === "publish" ? "发布中..." : "发布"}
            </button>
            <button className="iw-secondary-button" type="button" data-testid="release-action-retry" disabled={!retryTarget || Boolean(busyAction)} onClick={handleRetryPublish}>
              {busyAction === "retry-publish" ? "重试中..." : "重试发布"}
            </button>
            <button className="iw-secondary-button release-force-button" type="button" data-testid="release-action-force" disabled={!canForce || Boolean(busyAction)} onClick={handleForcePublish}>
              {busyAction === "force-publish" ? "发布中..." : "强制发布..."}
            </button>
          </div>
        ) : null}
      </div>

      {hasPendingReview && latestEvalRun ? (
        <section className="release-stage-band release-review-panel" data-testid="release-regression-review">
          <div className="release-stage-heading">
            <h4>回归人工复核</h4>
            <span className="iw-stage-pill" data-state="pending">{reviewItems.length} 条待复核</span>
            <span>{latestEvalRun.eval_run_id}</span>
          </div>
          <div className="release-review-items">
            {reviewItems.map((item) => (
              <article className="release-review-item" data-testid="release-regression-review-item" key={item.dataset_case_id}>
                <div className="release-review-item-copy">
                  <strong>{item.dataset_case_id}</strong>
                  <span>{item.dataset_case_snapshot.expected_behavior}</span>
                  <small>{item.answer_summary || "该用例需要人工确认自动判定。"}</small>
                </div>
                {!readOnly ? (
                  <div className="release-review-choices" role="group" aria-label={`${item.dataset_case_id} 复核结论`}>
                    {(["approve", "reject"] as const).map((decision) => (
                      <button
                        className={`iw-secondary-button release-review-choice ${reviewDecisions[item.dataset_case_id] === decision ? "is-selected" : ""}`}
                        data-decision={decision}
                        data-testid={`release-review-${decision}-${item.dataset_case_id}`}
                        type="button"
                        aria-pressed={reviewDecisions[item.dataset_case_id] === decision}
                        disabled={Boolean(busyAction)}
                        key={decision}
                        onClick={() => setReviewDecisions((current) => ({ ...current, [item.dataset_case_id]: decision }))}
                      >
                        {decision === "approve" ? "通过" : "拒绝"}
                      </button>
                    ))}
                  </div>
                ) : (
                  <span className="iw-stage-pill" data-state="pending">待复核</span>
                )}
              </article>
            ))}
          </div>
          {!readOnly ? (
            <div className="release-review-form">
              <label>
                复核人
                <input
                  className="iw-input"
                  data-testid="release-review-operator"
                  value={reviewOperator}
                  onChange={(event) => setReviewOperator(event.target.value)}
                />
              </label>
              <label>
                复核理由
                <textarea
                  className="iw-input"
                  data-testid="release-review-reason"
                  rows={3}
                  value={reviewReason}
                  onChange={(event) => setReviewReason(event.target.value)}
                />
              </label>
              <button
                className="iw-primary-button"
                data-testid="release-review-submit"
                type="button"
                disabled={!reviewComplete || Boolean(busyAction)}
                onClick={handleReviewRegression}
              >
                {busyAction === "regression-review" ? "提交中..." : "提交人工复核"}
              </button>
            </div>
          ) : null}
        </section>
      ) : null}

      <div className="release-stage-band" data-testid="release-changeset-details">
        <h4>候选与发布记录</h4>
        {selectedChangeSet ? (
          <div className="release-candidate-detail">
            <strong>{selectedChangeSet.title || selectedChangeSet.change_set_id}</strong>
            <span>状态：{selectedChangeSet.status}</span>
            <span>候选提交：{selectedChangeSet.candidate_commit_sha || "-"}</span>
            <span data-testid="release-latest-eval-run">
              回归运行：{latestEvalRun ? `${latestEvalRun.eval_run_id} · ${formatEvalResultStatus(latestEvalRun.result_status)}` : "尚无持久化结果"}
            </span>
            <span>阻塞项：{String(selectedChangeSet.publication_blocker || "无")}</span>
            <span>回归错误：{selectedChangeSet.regression_error?.error_type || "无"}</span>
            <span>发布错误：{selectedChangeSet.publication_error?.detail || "无"}</span>
            {showChanges ? <pre className="iw-context-body release-diff-summary" data-testid="release-diff-summary">{String(selectedChangeSet.diff_summary || "暂无 diff 摘要。")}</pre> : null}
          </div>
        ) : <div className="iw-empty">当前事项尚无待发布候选变更。</div>}
        {scopedReleases.map((release) => (
          <div className="iw-list-item" data-testid="release-item" data-status={release.status} key={release.release_id}>
            <span className="iw-list-item-title">{release.tag_name || release.release_id}</span>
            <span className="iw-list-item-meta">{release.status} · {release.commit_sha?.slice(0, 12) || "-"} · {release.created_at}</span>
          </div>
        ))}
        {cleanupTargets.map((changeSet) => (
          <div className="iw-list-item" data-testid="release-cleanup-pending" key={`cleanup-${changeSet.change_set_id}`}>
            <span className="iw-list-item-title">工作目录清理待恢复 · {changeSet.title || changeSet.change_set_id}</span>
            <span className="iw-list-item-meta">{changeSet.change_set_id} · {changeSet.status}</span>
            {!readOnly ? (
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-retry-cleanup"
                disabled={Boolean(busyAction)}
                onClick={() => handleRetryCleanup(changeSet.change_set_id)}
              >
                {busyAction === `cleanup-${changeSet.change_set_id}` ? "清理中..." : "重试清理"}
              </button>
            ) : null}
          </div>
        ))}
      </div>
      {confirmForceId ? (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-card version-confirm-modal" role="dialog" aria-modal="true" aria-label="确认强制发布" data-testid="release-force-confirm">
            <header className="modal-head">
              <div>
                <h3>确认强制发布</h3>
                <p>该动作会绕过未完成或失败的发布门禁，并写入审计记录。</p>
              </div>
            </header>
            <div className="iw-detail-section">
              <div className="iw-next-step">目标变更：{confirmForceId}</div>
              <div className="iw-next-step">绕过原因：{confirmedForceTarget?.publication_blocker || "人工确认门禁风险可接受"}</div>
            </div>
            <div className="modal-actions">
              <button className="secondary-button" type="button" onClick={() => setConfirmForceId(undefined)}>取消</button>
              <button className="primary-button" type="button" data-testid="release-force-confirm-submit" disabled={!confirmedForceTarget || Boolean(busyAction)} onClick={() => void confirmForcePublish()}>
                {busyAction === "force-publish" ? "发布中..." : "确认强制发布"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
