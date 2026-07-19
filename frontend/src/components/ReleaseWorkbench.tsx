import { useCallback, useEffect, useMemo, useState } from "react";
import {
  cancelAgentTestRun,
  createAgentChangeSetTestRun,
  inspectAgentTestSuite,
  listAgentTestRuns,
  publishAgentChangeSet,
  retryAgentChangeSetWorktreeCleanup,
} from "../api/runtime";
import type {
  AgentChangeSet,
  AgentRelease,
  AgentTestRun,
  AgentTestSuite,
  RuntimeClientConfig,
} from "../types/runtime";
import "../improvement-workbench.css";

type WithAgent = { agent_id: string };
type GateState = "pass" | "fail" | "pending" | "not_applicable";

const TERMINAL_CHANGE_SET_STATES = new Set(["published", "abandoned", "rejected", "failed"]);
const TEST_RUNNING_STATES = new Set(["queued", "running"]);
const FORCEABLE_CHANGE_SET_STATES = new Set(["candidate_committed", "approved"]);

const GATE_TEXT: Record<GateState, string> = {
  pass: "通过",
  fail: "未通过",
  pending: "未完成",
  not_applicable: "不适用",
};

const TEST_STATUS_TEXT: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  passed: "通过",
  failed: "未通过",
  error: "执行错误",
  cancelled: "已取消",
  interrupted: "服务重启中断",
};

function scopedBy<T extends WithAgent>(items: T[], agentId: string): T[] {
  return agentId ? items.filter((item) => item.agent_id === agentId) : items;
}

function latestExactRun(runs: AgentTestRun[], commitSha: string | null | undefined): AgentTestRun | null {
  if (!commitSha) return null;
  return runs.find((run) => run.commit_sha === commitSha) || null;
}

function deriveGates(changeSet: AgentChangeSet | null, testRun: AgentTestRun | null) {
  const attributionStatus = String(changeSet?.source_attribution_status || "");
  const attribution: GateState = !changeSet?.source_improvement_id
    ? "not_applicable"
    : attributionStatus === "confirmed"
      ? "pass"
      : "pending";
  const candidate: GateState = changeSet?.candidate_commit_sha ? "pass" : "pending";
  const tests: GateState = !testRun
    ? "pending"
    : testRun.status === "passed"
      ? "pass"
      : TEST_RUNNING_STATES.has(testRun.status)
        ? "pending"
        : "fail";
  return [
    { id: "attribution", label: "归因证据", state: attribution },
    { id: "candidate", label: "待发布版本", state: candidate },
    { id: "tests", label: "Workspace pytest", state: tests },
  ];
}

export function ReleaseWorkbench({
  clientConfig,
  scopeAgentId,
  sourceImprovementId,
  preferredChangeSetId,
  releases,
  changeSets,
  readOnly = false,
  onRefresh,
}: {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
  sourceImprovementId: string;
  preferredChangeSetId?: string;
  releases: AgentRelease[];
  changeSets: AgentChangeSet[];
  readOnly?: boolean;
  onRefresh: () => void | Promise<void>;
}) {
  const [selectedChangeSetId, setSelectedChangeSetId] = useState<string>();
  const [suite, setSuite] = useState<AgentTestSuite | null>(null);
  const [testRuns, setTestRuns] = useState<AgentTestRun[]>([]);
  const [showChanges, setShowChanges] = useState(false);
  const [showTestOutput, setShowTestOutput] = useState(false);
  const [busyAction, setBusyAction] = useState<string>();
  const [actionMessage, setActionMessage] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [forceTargetId, setForceTargetId] = useState<string>();
  const [forceReason, setForceReason] = useState("");

  const scopedChangeSets = scopedBy(changeSets as (AgentChangeSet & WithAgent)[], scopeAgentId)
    .filter((changeSet) => changeSet.source_improvement_id === sourceImprovementId);
  const pendingChangeSets = scopedChangeSets.filter(
    (changeSet) => !TERMINAL_CHANGE_SET_STATES.has(String(changeSet.status)),
  );
  const relatedChangeSetIds = new Set(scopedChangeSets.map((changeSet) => changeSet.change_set_id));
  const scopedReleases = scopedBy(releases as (AgentRelease & WithAgent)[], scopeAgentId)
    .filter((release) => release.change_set_id && relatedChangeSetIds.has(release.change_set_id));
  const cleanupTargets = scopedChangeSets.filter((changeSet) => changeSet.worktree_cleanup_pending);
  const selectedChangeSet = useMemo(
    () => pendingChangeSets.find((changeSet) => changeSet.change_set_id === selectedChangeSetId)
      || pendingChangeSets.find((changeSet) => changeSet.change_set_id === preferredChangeSetId)
      || pendingChangeSets[0]
      || null,
    [pendingChangeSets, preferredChangeSetId, selectedChangeSetId],
  );
  const currentTestRun = latestExactRun(testRuns, selectedChangeSet?.candidate_commit_sha);
  const gates = deriveGates(selectedChangeSet, currentTestRun);
  const hasFailedGate = gates.some((gate) => gate.state === "fail");
  const allRequiredGatesPassed = gates.every(
    (gate) => gate.state === "pass" || gate.state === "not_applicable",
  );
  const readyTarget = selectedChangeSet?.candidate_commit_sha
    && allRequiredGatesPassed
    && !selectedChangeSet.publication_blocker
    ? selectedChangeSet
    : null;
  const retryTarget = selectedChangeSet?.candidate_commit_sha && selectedChangeSet.status === "publishing"
    ? selectedChangeSet
    : null;
  const forceTarget = selectedChangeSet?.candidate_commit_sha
    && FORCEABLE_CHANGE_SET_STATES.has(String(selectedChangeSet.status))
    && !selectedChangeSet.publication_provenance_blocker
    && Boolean(selectedChangeSet.publication_blocker)
    ? selectedChangeSet
    : null;
  const confirmedForceTarget = pendingChangeSets.find((changeSet) => (
    changeSet.change_set_id === forceTargetId
    && Boolean(changeSet.candidate_commit_sha)
    && FORCEABLE_CHANGE_SET_STATES.has(String(changeSet.status))
    && !changeSet.publication_provenance_blocker
    && Boolean(changeSet.publication_blocker)
  )) || null;
  const activeTestRun = currentTestRun && TEST_RUNNING_STATES.has(currentTestRun.status)
    ? currentTestRun
    : null;
  const gateLabel = !selectedChangeSet
    ? "无待发布变更"
    : allRequiredGatesPassed && !selectedChangeSet.publication_blocker
      ? "可发布"
      : hasFailedGate
        ? "不可发布"
        : "进行中";

  const refreshTests = useCallback(async () => {
    if (!selectedChangeSet?.candidate_commit_sha) {
      setSuite(null);
      setTestRuns([]);
      return;
    }
    const [nextSuite, nextRuns] = await Promise.all([
      inspectAgentTestSuite(clientConfig, selectedChangeSet.agent_id, selectedChangeSet.candidate_commit_sha),
      listAgentTestRuns(clientConfig, {
        agentId: selectedChangeSet.agent_id,
        changeSetId: selectedChangeSet.change_set_id,
        limit: 20,
      }),
    ]);
    setSuite(nextSuite);
    setTestRuns(nextRuns);
  }, [clientConfig, selectedChangeSet?.agent_id, selectedChangeSet?.candidate_commit_sha, selectedChangeSet?.change_set_id]);

  useEffect(() => {
    setActionError(undefined);
    setActionMessage(undefined);
    void refreshTests().catch((error) => {
      setActionError(error instanceof Error ? error.message : String(error));
    });
  }, [refreshTests]);

  useEffect(() => {
    if (!forceTargetId) return;
    const targetStillForceable = pendingChangeSets.some((changeSet) => (
      changeSet.change_set_id === forceTargetId
      && Boolean(changeSet.candidate_commit_sha)
      && FORCEABLE_CHANGE_SET_STATES.has(String(changeSet.status))
      && !changeSet.publication_provenance_blocker
      && Boolean(changeSet.publication_blocker)
    ));
    if (!targetStillForceable) {
      setForceTargetId(undefined);
      setForceReason("");
    }
  }, [forceTargetId, pendingChangeSets]);

  useEffect(() => {
    if (!activeTestRun) return undefined;
    const timer = window.setInterval(() => {
      void refreshTests()
        .then(() => onRefresh())
        .catch((error) => setActionError(error instanceof Error ? error.message : String(error)));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [activeTestRun?.test_run_id, onRefresh, refreshTests]);

  useEffect(() => {
    if (!pendingChangeSets.length) {
      setSelectedChangeSetId(undefined);
      return;
    }
    if (!selectedChangeSetId || !pendingChangeSets.some((item) => item.change_set_id === selectedChangeSetId)) {
      const preferred = pendingChangeSets.find((item) => item.change_set_id === preferredChangeSetId);
      setSelectedChangeSetId(preferred?.change_set_id || pendingChangeSets[0].change_set_id);
    }
  }, [pendingChangeSets, preferredChangeSetId, selectedChangeSetId]);

  const runAction = async (name: string, action: () => Promise<void>) => {
    setBusyAction(name);
    setActionError(undefined);
    setActionMessage(undefined);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusyAction(undefined);
    }
  };

  const handleRunTests = () => {
    if (!selectedChangeSet?.candidate_commit_sha || !suite || suite.test_file_count === 0) return;
    void runAction("tests", async () => {
      const run = await createAgentChangeSetTestRun(clientConfig, selectedChangeSet.change_set_id);
      setTestRuns((current) => [run, ...current.filter((item) => item.test_run_id !== run.test_run_id)]);
      setActionMessage(`测试已进入队列：${run.test_run_id}`);
    });
  };

  const handleCancelTests = () => {
    if (!activeTestRun) return;
    void runAction("cancel-tests", async () => {
      const run = await cancelAgentTestRun(clientConfig, activeTestRun.test_run_id);
      setTestRuns((current) => current.map((item) => item.test_run_id === run.test_run_id ? run : item));
      setActionMessage(`已请求取消：${run.test_run_id}`);
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

  const handleForcePublish = () => {
    if (!confirmedForceTarget || !forceReason.trim()) return;
    void runAction("force-publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, confirmedForceTarget.change_set_id, {
        operator: "ui",
        force: true,
        force_reason: forceReason.trim(),
      });
      setForceTargetId(undefined);
      setForceReason("");
      setActionMessage(`已强制发布：${release.release_id}`);
      await onRefresh();
    });
  };

  const handleRetryCleanup = (changeSetId: string) => {
    void runAction(`cleanup-${changeSetId}`, async () => {
      await retryAgentChangeSetWorktreeCleanup(clientConfig, changeSetId);
      setActionMessage(`工作目录清理已完成：${changeSetId}`);
      await onRefresh();
    });
  };

  return (
    <section className="release-stage-workbench" data-testid="release-workbench">
      <header className="iw-stage-toolbar">
        <span>测试与发布 · {scopeAgentId}</span>
        <button className="iw-secondary-button" type="button" onClick={() => void Promise.all([refreshTests(), onRefresh()])}>刷新</button>
      </header>
      {actionError ? <div className="iw-error" data-testid="release-action-error">{actionError}</div> : null}
      {actionMessage ? <div className="iw-next-step" data-testid="release-action-message">{actionMessage}</div> : null}

      <div className="release-stage-band" data-testid="release-gate-workbench">
        <div className="release-stage-heading">
          <h4>发布条件</h4>
          <span className={`iw-stage-pill ${gateLabel === "可发布" ? "is-done" : ""}`} data-testid="release-gate">
            {gateLabel}
          </span>
          <span>{String(selectedChangeSet?.publication_blocker || "待发布版本与平台测试记录将按 commit 精确绑定。")}</span>
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
          {gates.map((gate) => (
            <span key={gate.id} className="iw-stage-pill" data-testid={`release-gate-${gate.id}`} data-state={gate.state}>
              {gate.label}：{GATE_TEXT[gate.state]}
            </span>
          ))}
        </div>
        {!readOnly ? (
          <div className="iw-action-row">
            <button
              className="iw-primary-button"
              type="button"
              data-testid="release-action-run-tests"
              disabled={!selectedChangeSet?.candidate_commit_sha || !suite?.test_file_count || Boolean(activeTestRun) || Boolean(busyAction)}
              onClick={handleRunTests}
            >
              {busyAction === "tests" ? "提交中..." : currentTestRun ? "重新运行测试" : "运行测试"}
            </button>
            <button
              className="iw-secondary-button"
              type="button"
              data-testid="release-action-cancel-tests"
              disabled={!activeTestRun || Boolean(busyAction)}
              onClick={handleCancelTests}
            >
              取消测试
            </button>
            <button className="iw-secondary-button" type="button" data-testid="release-action-view-changes" onClick={() => setShowChanges((value) => !value)}>
              {showChanges ? "收起变更" : "展开变更"}
            </button>
            <button className={readyTarget ? "iw-primary-button" : "iw-secondary-button"} type="button" data-testid="release-action-publish" disabled={!readyTarget || Boolean(busyAction)} onClick={handlePublish}>
              {busyAction === "publish" ? "发布中..." : "发布"}
            </button>
            <button className="iw-secondary-button" type="button" data-testid="release-action-retry" disabled={!retryTarget || Boolean(busyAction)} onClick={handleRetryPublish}>
              {busyAction === "retry-publish" ? "重试中..." : "重试发布"}
            </button>
            <button
              className="iw-secondary-button release-force-button"
              type="button"
              data-testid="release-action-force"
              disabled={!forceTarget || Boolean(busyAction)}
              onClick={() => {
                setForceTargetId(forceTarget?.change_set_id);
                setForceReason("");
              }}
            >
              强制发布...
            </button>
          </div>
        ) : null}
      </div>

      <div className="release-stage-band" data-testid="release-test-suite">
        <div className="release-stage-heading">
          <h4>Workspace 测试资产</h4>
          <span className="iw-stage-pill" data-state={suite?.test_file_count ? "pass" : "pending"}>
            {suite ? `${suite.test_file_count} 个测试文件` : "未加载"}
          </span>
          <span>{suite?.suite_digest ? `suite ${suite.suite_digest.slice(0, 12)}` : "tests/ 是唯一测试资产来源"}</span>
        </div>
        {suite?.test_files?.length ? (
          <div className="iw-source-refs">
            {suite.test_files.map((path) => <code key={path}>{path}</code>)}
          </div>
        ) : <div className="iw-empty">待发布版本未提供可运行的 tests/test_*.py。</div>}
        {(suite?.diagnostics ?? []).map((diagnostic) => (
          <div className={diagnostic.level === "error" ? "iw-error" : "iw-next-step"} key={`${diagnostic.code}-${diagnostic.path || ""}`}>
            {diagnostic.code} · {diagnostic.message}
          </div>
        ))}
      </div>

      <div className="release-stage-band" data-testid="release-test-run-details">
        <div className="release-stage-heading">
          <h4>平台测试运行</h4>
          <span className="iw-stage-pill" data-state={currentTestRun?.status === "passed" ? "pass" : currentTestRun ? "pending" : "not_applicable"}>
            {currentTestRun ? TEST_STATUS_TEXT[currentTestRun.status] || currentTestRun.status : "尚未运行"}
          </span>
          <span>{currentTestRun?.test_run_id || "只认可当前待发布 commit 的运行记录"}</span>
        </div>
        {currentTestRun ? (
          <div className="release-candidate-detail">
            <span>commit：{currentTestRun.commit_sha}</span>
            <span>命令：{(currentTestRun.command ?? []).join(" ")}</span>
            <span>开始：{currentTestRun.started_at || "排队中"}</span>
            <span>完成：{currentTestRun.completed_at || "-"}</span>
            <button className="iw-secondary-button" type="button" data-testid="release-action-view-test-output" onClick={() => setShowTestOutput((value) => !value)}>
              {showTestOutput ? "收起输出" : "查看输出"}
            </button>
            {showTestOutput ? (
              <>
                <pre className="iw-context-body release-diff-summary" data-testid="release-test-output">
                  {[currentTestRun.stdout, currentTestRun.stderr, currentTestRun.error?.message]
                    .filter(Boolean)
                    .join("\n") || "暂无输出。"}
                </pre>
                {(currentTestRun.invocations ?? []).map((invocation, index) => {
                  const traceUrl = typeof invocation.langfuse_trace_url === "string" ? invocation.langfuse_trace_url : "";
                  const runId = typeof invocation.run_id === "string" ? invocation.run_id : `调用 ${index + 1}`;
                  return (
                    <span className="iw-next-step" data-testid="release-test-trace" key={`${runId}-${index}`}>
                      Trace：{traceUrl ? <a href={traceUrl} target="_blank" rel="noreferrer">{runId}</a> : runId}
                    </span>
                  );
                })}
              </>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="release-stage-band" data-testid="release-changeset-details">
        <h4>待发布版本与发布记录</h4>
        {selectedChangeSet ? (
          <div className="release-candidate-detail">
            <strong>{selectedChangeSet.title || selectedChangeSet.change_set_id}</strong>
            <span>状态：{selectedChangeSet.status}</span>
            <span>修复前版本：{selectedChangeSet.base_commit_sha}</span>
            <span>待发布版本：{selectedChangeSet.candidate_commit_sha || "-"}</span>
            <span>阻塞项：{String(selectedChangeSet.publication_blocker || "无")}</span>
            <span>发布错误：{selectedChangeSet.publication_error?.detail || "无"}</span>
            {showChanges ? (
              <pre className="iw-context-body release-diff-summary" data-testid="release-diff-summary">
                {JSON.stringify(selectedChangeSet.diff_summary || {}, null, 2)}
              </pre>
            ) : null}
          </div>
        ) : <div className="iw-empty">当前事项尚无待发布版本。</div>}
        {scopedReleases.map((release) => (
          <div className="iw-list-item" data-testid="release-item" data-status={release.status} key={release.release_id}>
            <span className="iw-list-item-title">{release.tag_name || release.release_id}</span>
            <span className="iw-list-item-meta">{release.status} · {release.commit_sha.slice(0, 12)} · {release.created_at}</span>
            {release.force_published ? (
              <span className="iw-error" data-testid="release-force-warning">
                测试条件被管理员绕过 · 阻断项：{release.force_publication_blocker || "未记录"}
                {" · "}原因：{release.force_publish_reason || "未记录"}
                {" · "}操作人：{typeof release.operator === "string" ? release.operator : "未记录"}
              </span>
            ) : null}
          </div>
        ))}
        {cleanupTargets.map((changeSet) => (
          <div className="iw-list-item" data-testid="release-cleanup-pending" key={changeSet.change_set_id}>
            <span className="iw-list-item-title">工作目录清理待恢复 · {changeSet.title || changeSet.change_set_id}</span>
            {!readOnly ? (
              <button className="iw-secondary-button" type="button" data-testid="release-action-retry-cleanup" disabled={Boolean(busyAction)} onClick={() => handleRetryCleanup(changeSet.change_set_id)}>
                {busyAction === `cleanup-${changeSet.change_set_id}` ? "清理中..." : "重试清理"}
              </button>
            ) : null}
          </div>
        ))}
      </div>

      {forceTargetId ? (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-card version-confirm-modal" role="dialog" aria-modal="true" aria-label="确认强制发布" data-testid="release-force-confirm">
            <header className="modal-head">
              <div>
                <h3>确认强制发布</h3>
                <p>该动作会绕过当前发布条件，并把原因与阻塞项永久写入发布审计。</p>
              </div>
            </header>
            <div className="iw-detail-section">
              <div className="iw-next-step">目标变更：{forceTargetId}</div>
              <div className="iw-next-step">阻塞项：{confirmedForceTarget?.publication_blocker || "未知"}</div>
              <label>
                强制发布原因
                <textarea className="iw-input" data-testid="release-force-reason" rows={4} value={forceReason} onChange={(event) => setForceReason(event.target.value)} />
              </label>
            </div>
            <div className="modal-actions">
              <button className="secondary-button" type="button" onClick={() => setForceTargetId(undefined)}>取消</button>
              <button className="primary-button" type="button" data-testid="release-force-confirm-submit" disabled={!confirmedForceTarget || !forceReason.trim() || Boolean(busyAction)} onClick={handleForcePublish}>
                {busyAction === "force-publish" ? "发布中..." : "确认强制发布"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
