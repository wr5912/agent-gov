import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  approveAgentChangeSet,
  cancelAgentTestRun,
  createAgentChangeSetTestRun,
  diffAgentChangeSet,
  diffAgentChangeSetFile,
  inspectAgentTestSuite,
  listAgentTestRuns,
  publishAgentChangeSet,
  rejectAgentChangeSet,
  retryAgentChangeSetWorktreeCleanup,
} from "../api/runtime";
import type {
  AgentChangeSet,
  AgentGitDiff,
  AgentGitFileDiff,
  AgentRelease,
  AgentTestRun,
  AgentTestSuite,
  RuntimeClientConfig,
} from "../types/runtime";
import "../improvement-workbench.css";
import { evidenceBoundTestRun, publicationRequestEvidence } from "./releasePublicationEvidence";

type WithAgent = { agent_id: string };
type GateState = "pass" | "fail" | "pending" | "not_applicable";
type ChangedDiffFile = { path: string; status: "added" | "modified" | "deleted" };
type ReviewedFileEvidence = { path: string; detail_sha256: string };

const TERMINAL_CHANGE_SET_STATES = new Set(["published", "abandoned", "rejected", "failed"]);
const TEST_RUNNING_STATES = new Set(["queued", "running"]);
const PUBLISHABLE_CHANGE_SET_STATES = new Set(["candidate_committed", "approved"]);
const REJECTABLE_CHANGE_SET_STATES = new Set(["candidate_committed", "pending_approval", "approved"]);

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

function changedDiffFiles(diff: AgentGitDiff): ChangedDiffFile[] {
  return [
    ...diff.added.map((entry) => ({ path: entry.path, status: "added" as const })),
    ...diff.modified.map((entry) => ({ path: entry.path, status: "modified" as const })),
    ...diff.deleted.map((entry) => ({ path: entry.path, status: "deleted" as const })),
  ];
}

function hasActualChangedLine(unifiedDiff: string): boolean {
  return unifiedDiff.split("\n").some((line) => (
    (line.startsWith("+") && !line.startsWith("+++"))
    || (line.startsWith("-") && !line.startsWith("---"))
  ));
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("候选 Diff 包含无法生成审批指纹的数据。");
  return encoded;
}

async function canonicalDigest(value: unknown): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalJson(value)));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function reviewedFileEvidence(detail: AgentGitFileDiff): Promise<ReviewedFileEvidence> {
  return { path: detail.path, detail_sha256: await canonicalDigest(detail) };
}

export function candidateDiffReviewError(
  changeSet: AgentChangeSet | null,
  diff: AgentGitDiff | null,
  fileDiffs: AgentGitFileDiff[],
): string | undefined {
  if (!changeSet?.candidate_commit_sha || !diff) return "候选 Diff 尚未完整加载。";
  if (diff.from_version_id !== changeSet.base_commit_sha || diff.to_version_id !== changeSet.candidate_commit_sha) {
    return "候选 Diff 的版本身份与当前待发布版本不一致。";
  }
  const expected = changedDiffFiles(diff);
  if (!expected.length) return "候选与基准版本没有可审批的文件差异。";
  if (new Set(expected.map((item) => item.path)).size !== expected.length) return "候选 Diff 存在重复文件路径。";
  if (fileDiffs.length !== expected.length) return "逐文件 Diff 尚未完整加载。";
  if (new Set(fileDiffs.map((item) => item.path)).size !== fileDiffs.length) return "逐文件 Diff 存在重复文件路径。";
  for (const item of expected) {
    const detail = fileDiffs.find((entry) => entry.path === item.path);
    if (!detail || detail.from_version_id !== diff.from_version_id || detail.to_version_id !== diff.to_version_id) {
      return `文件 ${item.path} 的 Diff 身份不完整。`;
    }
    if (detail.status !== item.status || detail.truncated !== false || detail.is_text !== true) {
      return `文件 ${item.path} 的 Diff 无法完整审阅。`;
    }
    if (!detail.unified_diff.trim() || !hasActualChangedLine(detail.unified_diff)) {
      return `文件 ${item.path} 缺少实际变更行。`;
    }
  }
  return undefined;
}

export function deriveReleaseGates(changeSet: AgentChangeSet | null, testRun: AgentTestRun | null) {
  const attributionStatus = String(changeSet?.source_attribution_status || "");
  const attribution: GateState = !changeSet?.source_improvement_id
    ? "not_applicable"
    : attributionStatus === "confirmed"
      ? "pass"
      : "pending";
  const candidate: GateState = changeSet?.candidate_commit_sha ? "pass" : "pending";
  const exactTestPassed = Boolean(
    changeSet?.candidate_commit_sha
    && testRun?.status === "passed"
    && testRun.agent_id === changeSet.agent_id
    && testRun.change_set_id === changeSet.change_set_id
    && testRun.commit_sha === changeSet.candidate_commit_sha
    && testRun.test_run_id
    && testRun.suite_digest,
  );
  const tests: GateState = !testRun
    ? "pending"
    : exactTestPassed
      ? "pass"
      : TEST_RUNNING_STATES.has(testRun.status)
        ? "pending"
        : "fail";
  const approval: GateState = changeSet?.status === "pending_approval"
    ? "pending"
    : changeSet?.status === "approved"
      ? "pass"
      : "not_applicable";
  return [
    { id: "attribution", label: "归因证据", state: attribution },
    { id: "candidate", label: "待发布版本", state: candidate },
    { id: "tests", label: "Workspace pytest", state: tests },
    { id: "approval", label: "人工审批", state: approval },
  ];
}

export function scopedReleaseChangeSets(
  changeSets: AgentChangeSet[],
  agentId: string,
  sourceImprovementId?: string,
) {
  const scoped = scopedBy(changeSets as (AgentChangeSet & WithAgent)[], agentId);
  return sourceImprovementId === undefined
    ? scoped
    : scoped.filter((changeSet) => changeSet.source_improvement_id === sourceImprovementId);
}

export function deriveReleaseActions(
  changeSet: AgentChangeSet | null,
  testRun: AgentTestRun | null,
  diffReviewReady = false,
) {
  const gates = deriveReleaseGates(changeSet, testRun);
  const allRequiredGatesPassed = gates.every(
    (gate) => gate.state === "pass" || gate.state === "not_applicable",
  );
  return {
    gates,
    canApprove: Boolean(
      changeSet?.candidate_commit_sha
      && changeSet.status === "pending_approval"
      && testRun?.status === "passed"
      && diffReviewReady
      && gates.every((gate) => gate.id === "approval" || gate.state === "pass" || gate.state === "not_applicable"),
    ),
    canPublish: Boolean(
      changeSet?.candidate_commit_sha
      && PUBLISHABLE_CHANGE_SET_STATES.has(changeSet.status)
      && allRequiredGatesPassed
      && !changeSet.publication_blocker,
    ),
    canReject: Boolean(changeSet && REJECTABLE_CHANGE_SET_STATES.has(changeSet.status)),
    allRequiredGatesPassed,
  };
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
  /** 传入时严格限定该 Improvement；省略时进入业务 Agent 的通用候选治理。 */
  sourceImprovementId?: string;
  preferredChangeSetId?: string;
  releases: AgentRelease[];
  changeSets: AgentChangeSet[];
  readOnly?: boolean;
  onRefresh: () => void | Promise<void>;
}) {
  const [selectedChangeSetId, setSelectedChangeSetId] = useState<string>();
  const [suite, setSuite] = useState<AgentTestSuite | null>(null);
  const [testRuns, setTestRuns] = useState<AgentTestRun[]>([]);
  const [candidateDiff, setCandidateDiff] = useState<AgentGitDiff | null>(null);
  const [candidateFileDiffs, setCandidateFileDiffs] = useState<AgentGitFileDiff[]>([]);
  const [candidateReviewEvidence, setCandidateReviewEvidence] = useState<ReviewedFileEvidence[]>([]);
  const [loadedDiffIdentity, setLoadedDiffIdentity] = useState<{ changeSetId: string; digest: string } | null>(null);
  const [evidenceErrors, setEvidenceErrors] = useState<Record<"diff" | "suite" | "runs", string | undefined>>({
    diff: undefined,
    suite: undefined,
    runs: undefined,
  });
  const [showChanges, setShowChanges] = useState(false);
  const [showTestOutput, setShowTestOutput] = useState(false);
  const [busyAction, setBusyAction] = useState<string>();
  const [actionMessage, setActionMessage] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const evidenceRequestId = useRef(0);

  const scopedChangeSets = useMemo(
    () => scopedReleaseChangeSets(changeSets, scopeAgentId, sourceImprovementId),
    [changeSets, scopeAgentId, sourceImprovementId],
  );
  const pendingChangeSets = useMemo(
    () => scopedChangeSets.filter((changeSet) => !TERMINAL_CHANGE_SET_STATES.has(String(changeSet.status))),
    [scopedChangeSets],
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
  const currentTestRun = evidenceBoundTestRun(selectedChangeSet, testRuns);
  const diffReviewFailure = candidateDiffReviewError(selectedChangeSet, candidateDiff, candidateFileDiffs);
  const completeReviewEvidence = candidateReviewEvidence.length > 0
    && candidateReviewEvidence.length === candidateFileDiffs.length
    && candidateReviewEvidence.every((item, index) => item.path === candidateFileDiffs[index]?.path
      && /^[0-9a-f]{64}$/.test(item.detail_sha256));
  const exactDiffLoaded = loadedDiffIdentity?.changeSetId === selectedChangeSet?.change_set_id
    && loadedDiffIdentity?.digest === selectedChangeSet?.diff_summary?.digest;
  const exactSuiteLoaded = suite?.agent_id === selectedChangeSet?.agent_id
    && suite?.commit_sha === selectedChangeSet?.candidate_commit_sha
    && suite?.suite_digest === currentTestRun?.suite_digest
    && suite?.test_file_count > 0;
  const actions = deriveReleaseActions(
    selectedChangeSet,
    currentTestRun,
    showChanges && !evidenceErrors.diff && !evidenceErrors.suite && !evidenceErrors.runs
      && !diffReviewFailure && completeReviewEvidence && exactDiffLoaded && exactSuiteLoaded,
  );
  const gates = actions.gates;
  const hasFailedGate = gates.some((gate) => gate.state === "fail");
  const readyTarget = actions.canPublish ? selectedChangeSet : null;
  const approvalTarget = actions.canApprove ? selectedChangeSet : null;
  const retryTarget = selectedChangeSet?.candidate_commit_sha && selectedChangeSet.status === "publishing"
    ? selectedChangeSet
    : null;
  const retryEvidence = publicationRequestEvidence(retryTarget, currentTestRun);
  const rejectTarget = actions.canReject ? selectedChangeSet : null;
  const activeTestRun = currentTestRun && TEST_RUNNING_STATES.has(currentTestRun.status)
    ? currentTestRun
    : null;
  const gateLabel = !selectedChangeSet
    ? "无待发布变更"
    : selectedChangeSet.status === "pending_approval" && currentTestRun?.status === "passed"
      ? "待审批"
      : readyTarget
      ? "可发布"
      : hasFailedGate
        ? "不可发布"
        : "进行中";

  const refreshEvidence = useCallback(async () => {
    const requestId = ++evidenceRequestId.current;
    setCandidateDiff(null);
    setCandidateFileDiffs([]);
    setCandidateReviewEvidence([]);
    setLoadedDiffIdentity(null);
    if (!selectedChangeSet?.candidate_commit_sha) {
      setSuite(null);
      setTestRuns([]);
      setEvidenceErrors({ diff: undefined, suite: undefined, runs: undefined });
      return;
    }
    const [diffResult, suiteResult, runsResult] = await Promise.allSettled([
      diffAgentChangeSet(clientConfig, selectedChangeSet.change_set_id),
      inspectAgentTestSuite(clientConfig, selectedChangeSet.agent_id, selectedChangeSet.candidate_commit_sha),
      listAgentTestRuns(clientConfig, {
        agentId: selectedChangeSet.agent_id,
        changeSetId: selectedChangeSet.change_set_id,
        limit: 20,
      }),
    ] as const);
    const loadedDiff = diffResult.status === "fulfilled" ? diffResult.value : null;
    let fileDiffs: AgentGitFileDiff[] = [];
    let reviewEvidence: ReviewedFileEvidence[] = [];
    let loadedDigest: string | null = null;
    let diffError = diffResult.status === "rejected" ? errorMessage(diffResult.reason) : undefined;
    if (loadedDiff) {
      try {
        loadedDigest = await canonicalDigest(loadedDiff);
        const fileResults = await Promise.allSettled(
          changedDiffFiles(loadedDiff).map((entry) => (
            diffAgentChangeSetFile(clientConfig, selectedChangeSet.change_set_id, entry.path)
          )),
        );
        fileDiffs = fileResults
          .filter((result): result is PromiseFulfilledResult<AgentGitFileDiff> => result.status === "fulfilled")
          .map((result) => result.value);
        reviewEvidence = await Promise.all(fileDiffs.map(reviewedFileEvidence));
        const failedFile = fileResults.find((result) => result.status === "rejected");
        diffError = failedFile?.status === "rejected" ? errorMessage(failedFile.reason) : undefined;
        diffError = diffError || candidateDiffReviewError(selectedChangeSet, loadedDiff, fileDiffs);
        if (loadedDigest !== selectedChangeSet.diff_summary?.digest) diffError = "候选 Diff 指纹与当前候选不一致。";
      } catch (error) {
        diffError = errorMessage(error);
      }
    }
    if (requestId !== evidenceRequestId.current) return;
    setCandidateDiff(loadedDiff);
    setCandidateFileDiffs(fileDiffs);
    setCandidateReviewEvidence(reviewEvidence);
    setLoadedDiffIdentity(loadedDigest ? { changeSetId: selectedChangeSet.change_set_id, digest: loadedDigest } : null);
    setSuite(suiteResult.status === "fulfilled" ? suiteResult.value : null);
    setTestRuns(runsResult.status === "fulfilled" ? runsResult.value : []);
    setEvidenceErrors({
      diff: diffError,
      suite: suiteResult.status === "rejected" ? errorMessage(suiteResult.reason) : undefined,
      runs: runsResult.status === "rejected" ? errorMessage(runsResult.reason) : undefined,
    });
  }, [clientConfig, selectedChangeSet?.agent_id, selectedChangeSet?.candidate_commit_sha, selectedChangeSet?.change_set_id]);

  useEffect(() => {
    setActionError(undefined);
    setActionMessage(undefined);
    setShowChanges(false);
    void refreshEvidence();
  }, [refreshEvidence]);

  useEffect(() => {
    if (!activeTestRun) return undefined;
    const timer = window.setInterval(() => {
      void refreshEvidence()
        .then(() => onRefresh())
        .catch((error) => setActionError(error instanceof Error ? error.message : String(error)));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [activeTestRun?.test_run_id, onRefresh, refreshEvidence]);

  useEffect(() => {
    if (!pendingChangeSets.length) {
      setSelectedChangeSetId(undefined);
      return;
    }
    const preferred = pendingChangeSets.find((item) => item.change_set_id === preferredChangeSetId);
    setSelectedChangeSetId((current) => {
      if (preferred) return preferred.change_set_id;
      return current && pendingChangeSets.some((item) => item.change_set_id === current)
        ? current
        : pendingChangeSets[0].change_set_id;
    });
  }, [pendingChangeSets, preferredChangeSetId]);

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

  const handleApprove = () => {
    const candidateCommitSha = approvalTarget?.candidate_commit_sha;
    const diffDigest = approvalTarget?.diff_summary?.digest;
    const testRunId = currentTestRun?.test_run_id;
    const suiteDigest = currentTestRun?.suite_digest;
    if (
      !approvalTarget
      || typeof candidateCommitSha !== "string"
      || typeof diffDigest !== "string"
      || typeof testRunId !== "string"
      || typeof suiteDigest !== "string"
      || !completeReviewEvidence
    ) return;
    void runAction("approve", async () => {
      await approveAgentChangeSet(clientConfig, approvalTarget.change_set_id, {
        operator: "ui",
        note: "已在候选治理工作台核对 Diff 与精确 commit 测试记录",
        candidate_commit_sha: candidateCommitSha,
        diff_digest: diffDigest,
        test_run_id: testRunId,
        suite_digest: suiteDigest,
        reviewed_files: candidateReviewEvidence,
      });
      setActionMessage(`候选已审批：${approvalTarget.change_set_id}；仍未发布。`);
      await onRefresh();
    });
  };

  const handleReject = () => {
    if (!rejectTarget) return;
    const note = window.prompt("请填写拒绝原因（将写入治理审计）：")?.trim();
    if (!note) return;
    if (!window.confirm(`确认拒绝候选 ${rejectTarget.change_set_id}？\n\n原因：${note}\n拒绝后该候选不能发布。`)) return;
    void runAction("reject", async () => {
      await rejectAgentChangeSet(clientConfig, rejectTarget.change_set_id, { operator: "ui", note });
      setActionMessage(`候选已拒绝：${rejectTarget.change_set_id}；终态已记录。`);
      await onRefresh();
    });
  };

  const handlePublish = () => {
    const evidence = publicationRequestEvidence(readyTarget, currentTestRun);
    if (!readyTarget || !evidence || evidence.force || !evidence.testRunId || !evidence.suiteDigest) return;
    void runAction("publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, readyTarget.change_set_id, {
        operator: "ui",
        force: false,
        expected_candidate_commit_sha: evidence.candidateCommitSha,
        expected_diff_digest: evidence.diffDigest,
        expected_test_run_id: evidence.testRunId,
        expected_suite_digest: evidence.suiteDigest,
      });
      setActionMessage(`已发布：${release.release_id}`);
      await onRefresh();
    });
  };

  const handleRetryPublish = () => {
    if (!retryTarget || !retryEvidence) return;
    void runAction("retry-publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, retryTarget.change_set_id, {
        operator: "ui",
        force: retryEvidence.force,
        force_reason: retryEvidence.force ? "继续执行已持久化的强制发布意图" : undefined,
        tag_name: retryEvidence.tagName,
        expected_candidate_commit_sha: retryEvidence.candidateCommitSha,
        expected_diff_digest: retryEvidence.diffDigest,
        expected_test_run_id: retryEvidence.testRunId,
        expected_suite_digest: retryEvidence.suiteDigest,
      });
      setActionMessage(`发布已完成：${release.release_id}`);
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
        <span>{sourceImprovementId === undefined ? "候选治理" : "测试与发布"} · {scopeAgentId}</span>
        <button className="iw-secondary-button" type="button" onClick={() => void Promise.all([refreshEvidence(), onRefresh()])}>刷新</button>
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
            <button
              className="iw-secondary-button"
              type="button"
              data-testid="release-action-view-changes"
              disabled={!selectedChangeSet?.candidate_commit_sha}
              onClick={() => setShowChanges((value) => !value)}
            >
              {showChanges ? "收起变更" : "展开变更"}
            </button>
            <button
              className={approvalTarget ? "iw-primary-button" : "iw-secondary-button"}
              type="button"
              data-testid="release-action-approve"
              disabled={!approvalTarget || !showChanges || Boolean(busyAction)}
              onClick={handleApprove}
            >
              {busyAction === "approve" ? "审批中..." : "确认审批"}
            </button>
            {selectedChangeSet?.status === "pending_approval" ? (
              <span className="iw-next-step" data-testid="release-approval-confirmation-note">
                {approvalTarget
                  ? `点击“确认审批”即表示已审阅 ${candidateReviewEvidence.length} 个完整文件 Diff；审批不会自动发布。`
                  : "请展开并等待全部文件 Diff 与精确测试证据加载完成；审批不会自动发布。"}
              </span>
            ) : null}
            <button
              className="iw-secondary-button"
              type="button"
              data-testid="release-action-reject"
              disabled={!rejectTarget || Boolean(busyAction)}
              onClick={handleReject}
            >
              {busyAction === "reject" ? "拒绝中..." : "拒绝候选"}
            </button>
            <button className={readyTarget ? "iw-primary-button" : "iw-secondary-button"} type="button" data-testid="release-action-publish" disabled={!readyTarget || Boolean(busyAction)} onClick={handlePublish}>
              {busyAction === "publish" ? "发布中..." : "发布"}
            </button>
            <button className="iw-secondary-button" type="button" data-testid="release-action-retry" disabled={!retryTarget || !retryEvidence || Boolean(busyAction)} onClick={handleRetryPublish}>
              {busyAction === "retry-publish" ? "重试中..." : "重试发布"}
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
        {evidenceErrors.suite ? <div className="iw-error" data-testid="release-test-suite-error">{evidenceErrors.suite}</div> : null}
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
        {evidenceErrors.runs ? <div className="iw-error" data-testid="release-test-runs-error">{evidenceErrors.runs}</div> : null}
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
            <span>基准版本：{selectedChangeSet.base_commit_sha}</span>
            <span>待发布版本：{selectedChangeSet.candidate_commit_sha || "-"}</span>
            <span>阻塞项：{String(selectedChangeSet.publication_blocker || "无")}</span>
            <span>发布错误：{selectedChangeSet.publication_error?.detail || "无"}</span>
            {selectedChangeSet.approval_evidence ? (
              <span data-testid="release-approval-evidence">
                审批证据：test {selectedChangeSet.approval_evidence.test_run_id}
                {" · "}suite {selectedChangeSet.approval_evidence.suite_digest.slice(0, 12)}
                {" · "}diff {selectedChangeSet.approval_evidence.diff_digest.slice(0, 12)}
              </span>
            ) : null}
            {showChanges ? (
              <CandidateDiffSummary
                diff={candidateDiff}
                fileDiffs={candidateFileDiffs}
                error={evidenceErrors.diff}
              />
            ) : null}
          </div>
        ) : <div className="iw-empty">{sourceImprovementId === undefined ? "当前业务 Agent 尚无未完成候选。" : "当前事项尚无待发布版本。"}</div>}
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
        {scopedChangeSets
          .filter((changeSet) => TERMINAL_CHANGE_SET_STATES.has(changeSet.status) && changeSet.status !== "published")
          .map((changeSet) => (
            <div className="iw-list-item" data-testid="release-terminal-candidate" data-status={changeSet.status} key={changeSet.change_set_id}>
              <span className="iw-list-item-title">候选 {changeSet.change_set_id}</span>
              <span className="iw-list-item-meta">终态：{changeSet.status} · {changeSet.candidate_commit_sha || "无候选 commit"}</span>
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

    </section>
  );
}

function CandidateDiffSummary({
  diff,
  fileDiffs,
  error,
}: {
  diff: AgentGitDiff | null;
  fileDiffs: AgentGitFileDiff[];
  error?: string;
}) {
  if (!diff) {
    return error
      ? <div className="iw-error" data-testid="release-diff-error">{error}</div>
      : <div className="iw-empty">正在读取候选 Diff…</div>;
  }
  const statusText = { added: "新增", modified: "修改", deleted: "删除" };
  const rows = changedDiffFiles(diff);
  return (
    <div className="release-diff-summary" data-testid="release-diff-summary">
      {error ? <div className="iw-error" data-testid="release-diff-error">{error}</div> : null}
      <div className="release-diff-identity">
        <code title={diff.from_version_id}>base {diff.from_version_id.slice(0, 12)}</code>
        <span>→</span>
        <code title={diff.to_version_id}>candidate {diff.to_version_id.slice(0, 12)}</code>
      </div>
      {rows.length ? rows.map((row) => {
        const detail = fileDiffs.find((entry) => entry.path === row.path);
        return (
          <article className="release-diff-file" data-testid="release-diff-file" data-path={row.path} key={`${row.status}-${row.path}`}>
            <span>{statusText[row.status]}</span><code>{row.path}</code>
            {detail?.from_version_id === diff.from_version_id
              && detail.to_version_id === diff.to_version_id
              && detail.status === row.status
              && detail.unified_diff && hasActualChangedLine(detail.unified_diff)
              && detail.truncated === false && detail.is_text === true ? (
              <pre className="iw-context-body" data-testid="release-file-unified-diff" style={{ gridColumn: "1 / -1" }}>{detail.unified_diff}</pre>
            ) : <div className="iw-error">{detail?.reason || "逐文件 Diff 尚未完整加载。"}</div>}
          </article>
        );
      }) : <div className="iw-empty">候选与基准版本没有文件差异。</div>}
      <small>{diff.unchanged_count} 个文件未变化</small>
    </div>
  );
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
