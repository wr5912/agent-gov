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

type WithAgent = { agent_id: string };

export type ReleaseGateState = "pass" | "fail" | "pending" | "not_applicable";

export interface ReleaseGate {
  id: "attribution" | "candidate" | "tests";
  label: string;
  state: ReleaseGateState;
}

export interface ReleaseWorkbenchProps {
  clientConfig: RuntimeClientConfig;
  scopeAgentId: string;
  sourceImprovementId: string;
  preferredChangeSetId?: string;
  releases: AgentRelease[];
  changeSets: AgentChangeSet[];
  readOnly?: boolean;
  onRefresh: () => void | Promise<void>;
}

export interface ReleaseWorkbenchController {
  scopeAgentId: string;
  readOnly: boolean;
  pendingChangeSets: AgentChangeSet[];
  selectedChangeSet: AgentChangeSet | null;
  scopedReleases: AgentRelease[];
  cleanupTargets: AgentChangeSet[];
  suite: AgentTestSuite | null;
  currentTestRun: AgentTestRun | null;
  currentRunIsReleaseEligible: boolean;
  activeTestRun: AgentTestRun | null;
  gates: ReleaseGate[];
  gateLabel: string;
  readyToPublish: boolean;
  retryPublishAvailable: boolean;
  showChanges: boolean;
  showTestOutput: boolean;
  busyAction?: string;
  actionMessage?: string;
  actionError?: string;
  actions: ReleaseWorkbenchActions;
}

export interface ReleaseWorkbenchActions {
  selectChangeSet: (changeSetId: string) => void;
  refresh: () => void;
  runTests: () => void;
  cancelTests: () => void;
  publish: () => void;
  retryPublish: () => void;
  retryCleanup: (changeSetId: string) => void;
  toggleChanges: () => void;
  toggleTestOutput: () => void;
}

const TERMINAL_CHANGE_SET_STATES = new Set(["published", "abandoned", "rejected", "failed"]);
const TEST_RUNNING_STATES = new Set(["queued", "running"]);

function scopedBy<T extends WithAgent>(items: T[], agentId: string): T[] {
  return agentId ? items.filter((item) => item.agent_id === agentId) : items;
}

function latestExactRun(runs: AgentTestRun[], commitSha: string | null | undefined): AgentTestRun | null {
  if (!commitSha) return null;
  return runs.find((run) => run.commit_sha === commitSha) || null;
}

function deriveGates(changeSet: AgentChangeSet | null, testRun: AgentTestRun | null): ReleaseGate[] {
  const attributionStatus = String(changeSet?.source_attribution_status || "");
  const attribution: ReleaseGateState = !changeSet?.source_improvement_id
    ? "not_applicable"
    : attributionStatus === "confirmed"
      ? "pass"
      : "pending";
  const candidate: ReleaseGateState = changeSet?.candidate_commit_sha ? "pass" : "pending";
  const tests: ReleaseGateState = changeSet?.latest_test_run_id
    ? "pass"
    : !testRun || TEST_RUNNING_STATES.has(testRun.status)
      ? "pending"
      : "fail";
  return [
    { id: "attribution", label: "归因证据", state: attribution },
    { id: "candidate", label: "待发布版本", state: candidate },
    { id: "tests", label: "Workspace pytest", state: tests },
  ];
}

function useReleaseSelection(props: ReleaseWorkbenchProps) {
  const [selectedChangeSetId, setSelectedChangeSetId] = useState<string>();
  const scope = useMemo(() => {
    const scopedChangeSets = scopedBy(
      props.changeSets as (AgentChangeSet & WithAgent)[],
      props.scopeAgentId,
    ).filter((changeSet) => changeSet.source_improvement_id === props.sourceImprovementId);
    const pendingChangeSets = scopedChangeSets.filter(
      (changeSet) => !TERMINAL_CHANGE_SET_STATES.has(String(changeSet.status)),
    );
    const relatedChangeSetIds = new Set(scopedChangeSets.map((item) => item.change_set_id));
    const scopedReleases = scopedBy(
      props.releases as (AgentRelease & WithAgent)[],
      props.scopeAgentId,
    ).filter((release) => release.change_set_id && relatedChangeSetIds.has(release.change_set_id));
    return {
      pendingChangeSets,
      scopedReleases,
      cleanupTargets: scopedChangeSets.filter((changeSet) => changeSet.worktree_cleanup_pending),
    };
  }, [props.changeSets, props.releases, props.scopeAgentId, props.sourceImprovementId]);
  const selectedChangeSet = useMemo(
    () => scope.pendingChangeSets.find((item) => item.change_set_id === selectedChangeSetId)
      || scope.pendingChangeSets.find((item) => item.change_set_id === props.preferredChangeSetId)
      || scope.pendingChangeSets[0]
      || null,
    [props.preferredChangeSetId, scope.pendingChangeSets, selectedChangeSetId],
  );

  useEffect(() => {
    if (!scope.pendingChangeSets.length) {
      setSelectedChangeSetId(undefined);
      return;
    }
    if (selectedChangeSetId && scope.pendingChangeSets.some((item) => item.change_set_id === selectedChangeSetId)) return;
    const preferred = scope.pendingChangeSets.find((item) => item.change_set_id === props.preferredChangeSetId);
    setSelectedChangeSetId(preferred?.change_set_id || scope.pendingChangeSets[0].change_set_id);
  }, [props.preferredChangeSetId, scope.pendingChangeSets, selectedChangeSetId]);

  return { ...scope, selectedChangeSet, setSelectedChangeSetId };
}

function useActionFeedback() {
  const [busyAction, setBusyAction] = useState<string>();
  const [actionMessage, setActionMessage] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const runAction = useCallback(async (name: string, action: () => Promise<void>) => {
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
  }, []);
  const reset = useCallback(() => {
    setActionError(undefined);
    setActionMessage(undefined);
  }, []);
  const reportError = useCallback((error: unknown) => {
    setActionError(error instanceof Error ? error.message : String(error));
  }, []);
  return {
    busyAction,
    actionMessage,
    actionError,
    setActionMessage,
    runAction,
    reset,
    reportError,
  };
}

function useReleaseTests(
  clientConfig: RuntimeClientConfig,
  selectedChangeSet: AgentChangeSet | null,
  onRefresh: ReleaseWorkbenchProps["onRefresh"],
  resetFeedback: () => void,
  reportError: (error: unknown) => void,
) {
  const [suite, setSuite] = useState<AgentTestSuite | null>(null);
  const [testRuns, setTestRuns] = useState<AgentTestRun[]>([]);
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
  const currentTestRun = latestExactRun(testRuns, selectedChangeSet?.candidate_commit_sha);
  const activeTestRun = currentTestRun && TEST_RUNNING_STATES.has(currentTestRun.status)
    ? currentTestRun
    : null;

  useEffect(() => {
    resetFeedback();
    void refreshTests().catch(reportError);
  }, [refreshTests, reportError, resetFeedback]);
  useEffect(() => {
    if (!activeTestRun) return undefined;
    const timer = window.setInterval(() => {
      void refreshTests().then(() => onRefresh()).catch(reportError);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [activeTestRun?.test_run_id, onRefresh, refreshTests, reportError]);

  return { suite, testRuns, setTestRuns, refreshTests, currentTestRun, activeTestRun };
}

function useReleaseActions({
  clientConfig,
  selectedChangeSet,
  suite,
  activeTestRun,
  readyTarget,
  retryTarget,
  refreshTests,
  setTestRuns,
  setActionMessage,
  runAction,
  onRefresh,
}: {
  clientConfig: RuntimeClientConfig;
  selectedChangeSet: AgentChangeSet | null;
  suite: AgentTestSuite | null;
  activeTestRun: AgentTestRun | null;
  readyTarget: AgentChangeSet | null;
  retryTarget: AgentChangeSet | null;
  refreshTests: () => Promise<void>;
  setTestRuns: React.Dispatch<React.SetStateAction<AgentTestRun[]>>;
  setActionMessage: React.Dispatch<React.SetStateAction<string | undefined>>;
  runAction: (name: string, action: () => Promise<void>) => Promise<void>;
  onRefresh: ReleaseWorkbenchProps["onRefresh"];
}) {
  const runTests = useCallback(() => {
    if (!selectedChangeSet?.candidate_commit_sha || !suite || suite.test_file_count === 0) return;
    void runAction("tests", async () => {
      const run = await createAgentChangeSetTestRun(clientConfig, selectedChangeSet.change_set_id);
      setTestRuns((current) => [run, ...current.filter((item) => item.test_run_id !== run.test_run_id)]);
      setActionMessage(`测试已进入队列：${run.test_run_id}`);
    });
  }, [clientConfig, runAction, selectedChangeSet, setActionMessage, setTestRuns, suite]);
  const cancelTests = useCallback(() => {
    if (!activeTestRun) return;
    void runAction("cancel-tests", async () => {
      const run = await cancelAgentTestRun(clientConfig, activeTestRun.test_run_id);
      setTestRuns((current) => current.map((item) => item.test_run_id === run.test_run_id ? run : item));
      setActionMessage(`已请求取消：${run.test_run_id}`);
    });
  }, [activeTestRun, clientConfig, runAction, setActionMessage, setTestRuns]);
  const publish = useCallback(() => {
    if (!readyTarget) return;
    void runAction("publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, readyTarget.change_set_id, { operator: "ui", force: false });
      setActionMessage(`已发布：${release.release_id}`);
      await onRefresh();
    });
  }, [clientConfig, onRefresh, readyTarget, runAction, setActionMessage]);
  const retryPublish = useCallback(() => {
    if (!retryTarget) return;
    void runAction("retry-publish", async () => {
      const release = await publishAgentChangeSet(clientConfig, retryTarget.change_set_id, { operator: "ui", force: false });
      setActionMessage(`发布已完成：${release.release_id}`);
      await onRefresh();
    });
  }, [clientConfig, onRefresh, retryTarget, runAction, setActionMessage]);
  const retryCleanup = useCallback((changeSetId: string) => {
    void runAction(`cleanup-${changeSetId}`, async () => {
      await retryAgentChangeSetWorktreeCleanup(clientConfig, changeSetId);
      setActionMessage(`工作目录清理已完成：${changeSetId}`);
      await onRefresh();
    });
  }, [clientConfig, onRefresh, runAction, setActionMessage]);
  const refresh = useCallback(() => {
    void Promise.all([refreshTests(), onRefresh()]);
  }, [onRefresh, refreshTests]);
  return { refresh, runTests, cancelTests, publish, retryPublish, retryCleanup };
}

export function useReleaseWorkbenchController(props: ReleaseWorkbenchProps): ReleaseWorkbenchController {
  const selection = useReleaseSelection(props);
  const feedback = useActionFeedback();
  const tests = useReleaseTests(
    props.clientConfig,
    selection.selectedChangeSet,
    props.onRefresh,
    feedback.reset,
    feedback.reportError,
  );
  const gates = deriveGates(selection.selectedChangeSet, tests.currentTestRun);
  const hasFailedGate = gates.some((gate) => gate.state === "fail");
  const allRequiredGatesPassed = gates.every((gate) => gate.state === "pass" || gate.state === "not_applicable");
  const readyTarget = selection.selectedChangeSet?.candidate_commit_sha
    && allRequiredGatesPassed
    && !selection.selectedChangeSet.publication_blocker
    ? selection.selectedChangeSet
    : null;
  const retryTarget = selection.selectedChangeSet?.candidate_commit_sha
    && selection.selectedChangeSet.status === "publishing"
    && !selection.selectedChangeSet.publication_blocker
    ? selection.selectedChangeSet
    : null;
  const gateLabel = !selection.selectedChangeSet
    ? "无待发布变更"
    : allRequiredGatesPassed && !selection.selectedChangeSet.publication_blocker
      ? "可发布"
      : hasFailedGate
        ? "不可发布"
        : "进行中";
  const [showChanges, setShowChanges] = useState(false);
  const [showTestOutput, setShowTestOutput] = useState(false);
  const actionHandlers = useReleaseActions({
    clientConfig: props.clientConfig,
    selectedChangeSet: selection.selectedChangeSet,
    suite: tests.suite,
    activeTestRun: tests.activeTestRun,
    readyTarget,
    retryTarget,
    refreshTests: tests.refreshTests,
    setTestRuns: tests.setTestRuns,
    setActionMessage: feedback.setActionMessage,
    runAction: feedback.runAction,
    onRefresh: props.onRefresh,
  });
  return {
    scopeAgentId: props.scopeAgentId,
    readOnly: props.readOnly ?? false,
    pendingChangeSets: selection.pendingChangeSets,
    selectedChangeSet: selection.selectedChangeSet,
    scopedReleases: selection.scopedReleases,
    cleanupTargets: selection.cleanupTargets,
    suite: tests.suite,
    currentTestRun: tests.currentTestRun,
    currentRunIsReleaseEligible: Boolean(
      tests.currentTestRun
      && selection.selectedChangeSet?.latest_test_run_id === tests.currentTestRun.test_run_id,
    ),
    activeTestRun: tests.activeTestRun,
    gates,
    gateLabel,
    readyToPublish: Boolean(readyTarget),
    retryPublishAvailable: Boolean(retryTarget),
    showChanges,
    showTestOutput,
    busyAction: feedback.busyAction,
    actionMessage: feedback.actionMessage,
    actionError: feedback.actionError,
    actions: {
      ...actionHandlers,
      selectChangeSet: selection.setSelectedChangeSetId,
      toggleChanges: () => setShowChanges((value) => !value),
      toggleTestOutput: () => setShowTestOutput((value) => !value),
    },
  };
}
