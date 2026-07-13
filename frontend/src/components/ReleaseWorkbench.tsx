import { useEffect, useMemo, useState } from "react";
import { publishAgentChangeSet, runAgentChangeSetRegression } from "../api/runtime";
import type { AgentChangeSet, AgentRelease } from "../types/runtime";
import type { RuntimeClientConfig } from "../types/runtime";
import "../improvement-workbench.css";

// 发布工作台（四阶段改进治理 §12）：回答「能不能发 / 为什么 / 发了包含什么」，并呈现三门门禁与动作。
// 按当前业务 Agent 防御式过滤（响应含 agent_id 时按 Agent scoping，否则不过滤避免误隐藏）。
type WithAgent = { agent_id?: string };

const CHANGESET_TERMINAL = new Set(["published", "abandoned", "rejected", "failed"]);
const CHANGESET_BLOCKED = new Set(["regression_failed", "rejected", "failed"]);
const CHANGESET_READY = new Set(["regression_passed", "approved", "candidate_committed"]);
const CHANGESET_FORCEABLE = new Set(["regression_failed"]);
const CHANGESET_REGRESSION_RUNNABLE = new Set(["candidate_committed", "pending_approval", "approved", "regression_failed"]);
const EXECUTED = new Set(["candidate_committed", "regression_passed", "regression_failed", "approved", "publishing", "published"]);
const REGRESSION_PASS = new Set(["regression_passed", "approved", "published"]);

type GateState = "pass" | "fail" | "pending" | "optional" | "unknown" | "not_applicable";

function scopedBy<T extends WithAgent>(items: T[], agentId: string): T[] {
  if (!agentId) return items;
  return items.filter((item) => item.agent_id == null || item.agent_id === agentId);
}

function deriveGates(changeSet: AgentChangeSet | null): { id: string; label: string; state: GateState }[] {
  const has = Boolean(changeSet);
  const executed = Boolean(changeSet && EXECUTED.has(String(changeSet.status)));
  const provenanceBlocked = Boolean(changeSet?.publication_provenance_blocker);
  const regPass = Boolean(changeSet && REGRESSION_PASS.has(String(changeSet.status)));
  const regFail = Boolean(changeSet && CHANGESET_BLOCKED.has(String(changeSet.status)));
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
    { id: "regression", label: "回归测试", state: regFail ? "fail" : regPass ? "pass" : has ? "optional" : "pending" },
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
  releases,
  changeSets,
  onRefresh,
}: {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
  releases: AgentRelease[];
  changeSets: AgentChangeSet[];
  onRefresh: () => void | Promise<void>;
}) {
  const [showChanges, setShowChanges] = useState(false);
  const [busyAction, setBusyAction] = useState<string | undefined>();
  const [confirmForceId, setConfirmForceId] = useState<string | undefined>();
  const [selectedChangeSetId, setSelectedChangeSetId] = useState<string | undefined>();
  const [actionMessage, setActionMessage] = useState<string | undefined>();
  const [actionError, setActionError] = useState<string | undefined>();
  const scopedChangeSets = scopedBy(changeSets as (AgentChangeSet & WithAgent)[], scopeAgentId);
  const scopedReleases = scopedBy(releases as (AgentRelease & WithAgent)[], scopeAgentId);
  const pendingChangeSets = scopedChangeSets.filter((cs) => !CHANGESET_TERMINAL.has(String(cs.status)));
  const selectedChangeSet = useMemo(
    () => pendingChangeSets.find((cs) => cs.change_set_id === selectedChangeSetId) || pendingChangeSets[0] || null,
    [pendingChangeSets, selectedChangeSetId],
  );
  const gates = deriveGates(selectedChangeSet);
  const gate = overallGate(gates, selectedChangeSet ? 1 : 0);
  const regressionPending = gates.find((g) => g.id === "regression")?.state !== "pass" && Boolean(selectedChangeSet);
  const regressionTarget = selectedChangeSet?.candidate_commit_sha && CHANGESET_REGRESSION_RUNNABLE.has(String(selectedChangeSet.status)) ? selectedChangeSet : null;
  const readyTarget = selectedChangeSet?.candidate_commit_sha && CHANGESET_READY.has(String(selectedChangeSet.status)) && !selectedChangeSet.publication_blocker ? selectedChangeSet : null;
  const retryTarget = selectedChangeSet?.candidate_commit_sha && selectedChangeSet.status === "publishing" ? selectedChangeSet : null;
  const forceTarget = selectedChangeSet?.candidate_commit_sha && CHANGESET_FORCEABLE.has(String(selectedChangeSet.status)) && !selectedChangeSet.publication_provenance_blocker ? selectedChangeSet : null;
  const confirmedForceTarget = pendingChangeSets.find((cs) => cs.change_set_id === confirmForceId && CHANGESET_FORCEABLE.has(String(cs.status))) || null;
  const canForce = Boolean(forceTarget);

  useEffect(() => {
    if (!pendingChangeSets.length) {
      setSelectedChangeSetId(undefined);
      return;
    }
    if (!selectedChangeSetId || !pendingChangeSets.some((cs) => cs.change_set_id === selectedChangeSetId)) {
      setSelectedChangeSetId(pendingChangeSets[0].change_set_id);
    }
  }, [pendingChangeSets, selectedChangeSetId]);

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
    if (!regressionTarget) return;
    void runAction("regression", async () => {
      const result = await runAgentChangeSetRegression(clientConfig, regressionTarget.change_set_id);
      setActionMessage(`已运行回归：${result.eval_run_id}（${result.result_status}）`);
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
    <div className="improvement-workbench release-workbench-grid" data-testid="release-workbench">
      <section className="iw-list-panel release-candidate-panel">
        <div className="iw-panel-head">
          <h3>发布{scopeAgentId ? ` · ${scopeAgentId}` : "（全部业务 Agent）"}</h3>
          <button className="iw-secondary-button" type="button" onClick={() => void onRefresh()}>刷新</button>
        </div>
        <div className="iw-panel-body">
          {actionError ? <div className="iw-error" data-testid="release-action-error">{actionError}</div> : null}
          {actionMessage ? <div className="iw-next-step" data-testid="release-action-message">{actionMessage}</div> : null}
          <div className="iw-detail-section release-gate-summary">
            <h4>能不能发</h4>
            <span className={`iw-stage-pill ${gate.tone === "success" ? "is-done" : ""}`} data-testid="release-gate" data-state={gate.tone}>{gate.label}</span>
            <div className="iw-next-step" style={{ marginTop: 8 }}>{gate.reason}</div>
          </div>

          <div className="iw-detail-section">
            <h4>待发布变更（{pendingChangeSets.length}）</h4>
            {pendingChangeSets.length === 0 ? (
              <div className="iw-empty">当前范围没有待发布的候选变更。</div>
            ) : (
              pendingChangeSets.map((cs) => (
                <button
                  className={`iw-list-item release-changeset-button ${selectedChangeSet?.change_set_id === cs.change_set_id ? "is-active" : ""}`}
                  data-testid="release-changeset-item"
                  data-status={cs.status}
                  key={cs.change_set_id}
                  type="button"
                  onClick={() => setSelectedChangeSetId(cs.change_set_id)}
                >
                  <span className="iw-list-item-title">{cs.title || cs.change_set_id}</span>
                  <span className="iw-list-item-meta">{cs.status} · {cs.change_set_id}{cs.publication_blocker ? ` · 阻塞：${cs.publication_blocker}` : ""}{cs.publication_error?.detail ? ` · 发布错误：${cs.publication_error.detail}` : ""}</span>
                </button>
              ))
            )}
          </div>
        </div>
      </section>

      <section className="iw-detail-panel">
        <div className="iw-panel-body release-detail-stack">
          <div className="iw-detail-section" data-testid="release-gate-workbench">
            <h4>发布门禁</h4>
            <div className="iw-source-refs">
              {gates.map((g) => (
                <span
                  key={g.id}
                  className="iw-stage-pill"
                  data-testid={`release-gate-${g.id}`}
                  data-state={g.state}
                  style={{
                    background: g.state === "pass" ? "var(--color-success-soft)" : g.state === "fail" ? "#fef2f2" : "var(--bg-surface-muted)",
                    color: g.state === "pass" ? "var(--color-success)" : g.state === "fail" ? "#dc2626" : "var(--text-secondary)",
                    borderColor: g.state === "pass" ? "#bbf7d0" : g.state === "fail" ? "#fecaca" : "var(--border-subtle)",
                  }}
                >
                  {g.label}：{GATE_TEXT[g.state]}
                </span>
              ))}
            </div>
            <div className="iw-action-row">
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-run-regression"
                disabled={!regressionPending || !regressionTarget || Boolean(busyAction)}
                title={regressionTarget ? `运行 ${regressionTarget.change_set_id} 的回归` : "无可运行回归的候选变更"}
                onClick={handleRunRegression}
              >
                {busyAction === "regression" ? "回归中..." : "去运行回归"}
              </button>
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-view-changes"
                onClick={() => setShowChanges((v) => !v)}
              >
                {showChanges ? "收起变更" : "展开变更"}
              </button>
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-publish"
                disabled={!readyTarget || Boolean(busyAction)}
                title={readyTarget ? `发布 ${readyTarget.change_set_id}` : "无已就绪候选变更"}
                onClick={handlePublish}
              >
                {busyAction === "publish" ? "发布中..." : "发布"}
              </button>
              <button
                className="iw-secondary-button"
                type="button"
                data-testid="release-action-retry"
                disabled={!retryTarget || Boolean(busyAction)}
                title={retryTarget ? `重试 ${retryTarget.change_set_id} 的发布对账` : "无待对账发布"}
                onClick={handleRetryPublish}
              >
                {busyAction === "retry-publish" ? "重试中..." : "重试发布"}
              </button>
              <button
                className="iw-secondary-button release-force-button"
                type="button"
                data-testid="release-action-force"
                disabled={!canForce || Boolean(busyAction)}
                title={forceTarget ? `强制发布 ${forceTarget.change_set_id}` : "无可发布变更"}
                onClick={handleForcePublish}
              >
                {busyAction === "force-publish" ? "发布中..." : "强制发布..."}
              </button>
            </div>
          </div>

          <div className="iw-detail-section" data-testid="release-changeset-details">
            <h4>候选详情</h4>
            {selectedChangeSet ? (
              <div className="release-candidate-detail">
                <strong>{selectedChangeSet.title || selectedChangeSet.change_set_id}</strong>
                <span>状态：{selectedChangeSet.status}</span>
                <span>候选提交：{selectedChangeSet.candidate_commit_sha || "-"}</span>
                <span>来源改进：{String(selectedChangeSet.source_improvement_id || "-")}</span>
                <span>阻塞项：{String(selectedChangeSet.publication_blocker || "无")}</span>
                <span>发布错误：{selectedChangeSet.publication_error?.detail || "无"}</span>
                {showChanges ? (
                  <pre className="iw-context-body release-diff-summary" data-testid="release-diff-summary">{String(selectedChangeSet.diff_summary || "暂无 diff 摘要。")}</pre>
                ) : null}
              </div>
            ) : (
              <div className="iw-empty">选择一个待发布变更查看门禁、diff 和阻塞项。</div>
            )}
          </div>

          <div className="iw-detail-section">
            <h4>已发布（{scopedReleases.length}）</h4>
            {scopedReleases.length === 0 ? (
              <div className="iw-empty">尚无发布记录。</div>
            ) : (
              scopedReleases.map((rel) => (
                <div className="iw-list-item" data-testid="release-item" data-status={rel.status} key={rel.release_id}>
                  <span className="iw-list-item-title">{rel.tag_name || rel.release_id}</span>
                  <span className="iw-list-item-meta">{rel.status} · {rel.commit_sha?.slice(0, 12) || "-"} · {rel.created_at}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </section>
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
              <button className="primary-button" type="button" data-testid="release-force-confirm-submit" disabled={Boolean(busyAction)} onClick={() => void confirmForcePublish()}>
                {busyAction === "force-publish" ? "发布中..." : "确认强制发布"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
