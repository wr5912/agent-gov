import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { defaultRuntimeConfig, getAgentChangeSets, getAgentReleases, getAgentRepositoryStatus, getCurrentAgentRef, getHealth, getRuntimeSessionMessages, getRuntimeSessionStatus, getSessions, listBusinessAgents, provisionRuntimeAgent, shouldMigrateStoredApiBase } from "./api/runtime";
import { ChatPanel } from "./components/ChatPanel";
import { ImprovementWorkbench } from "./components/ImprovementWorkbench";
import { AssetRegistry } from "./components/AssetRegistry";
import { EVIDENCE_PANEL_DEFAULT_WIDTH, PlaygroundEvidencePanel } from "./components/PlaygroundEvidencePanel";
import { PlaygroundRuntimeSettingsDrawer } from "./components/PlaygroundRuntimeSettingsDrawer";
import { PlaygroundSessionSidebar } from "./components/PlaygroundSessionSidebar";
import { FeedbackDrawer, type FeedbackContext } from "./components/FeedbackDrawer";
import { SettingsModal } from "./components/SettingsModal";
import { Topbar } from "./components/Topbar";
import { useAgentCatalog } from "./hooks/useAgentCatalog";
import { useAgentPresentation } from "./hooks/useAgentPresentation";
import { useConfigMapping } from "./hooks/useConfigMapping";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { usePlaygroundSessionScope } from "./hooks/usePlaygroundSessionScope";
import { usePlaygroundTrace } from "./hooks/usePlaygroundTrace";
import { usePlaygroundRun } from "./hooks/usePlaygroundRun";
import { cancelWaitingUserConfirmRequests, patchUserConfirmRequest } from "./runtimeUserConfirmState";
import {
  cancelWaitingExternalExecutionRequests,
  patchExternalExecutionRequest,
} from "./runtimeExternalExecutionState";
import { messagesFromAgentScopeMessages } from "./playgroundHistory";
import { usePromptSuggestion } from "./hooks/usePromptSuggestion";
import {
  initialPlaygroundRunState,
  isPlaygroundRunLocked,
  playgroundRunReducer,
} from "./playgroundRunState";
import type { AgentChangeSet, AgentGitRef, AgentRelease, AgentRepositoryStatus, AgentSummary, ChatMessage, RuntimeClientConfig, RuntimeExternalExecutionRequest, RuntimeHealth, RuntimeUserConfirmRequest, SessionInfo } from "./types/runtime";
import { getAgentRunPendingActions, getAgentRuns } from "./api/feedback";
import { defaultLangfuseUrl, makeApiDocsUrl } from "./runtimeUrls";
import "./styles.css";

export default function App() {
  const runtimeDefaults = useMemo(() => defaultRuntimeConfig(), []);
  const [clientConfig, setClientConfig] = useLocalStorage<RuntimeClientConfig>("runtime-client-config", runtimeDefaults);
  const [messagesBySession, setMessagesBySession] = useState<Record<string, ChatMessage[]>>({});

  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [agentRepository, setAgentRepository] = useState<AgentRepositoryStatus | null>(null);
  const [currentAgentRef, setCurrentAgentRef] = useState<AgentGitRef | null>(null);
  const [agentChangeSets, setAgentChangeSets] = useState<AgentChangeSet[]>([]);
  const [agentReleases, setAgentReleases] = useState<AgentRelease[]>([]);
  const [businessAgents, setBusinessAgents] = useState<AgentSummary[]>([]);
  const {
    activeSessionId,
    selectedBusinessAgentId,
    scopedSessions,
    reconcile: reconcilePlaygroundScope,
    switchBusinessAgent,
    startNewSession,
    selectSession: selectScopedSession,
    claimLocalSession,
  } = usePlaygroundSessionScope({ sessions, messagesBySession });
  const [alertId, setAlertId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [input, setInput] = useState("");
  const [runState, dispatchRun] = useReducer(playgroundRunReducer, initialPlaygroundRunState);
  const streaming = isPlaygroundRunLocked(runState);
  const [streamingAssistantMessageId, setStreamingAssistantMessageId] = useState<string | undefined>();
  const [userInputErrors, setUserInputErrors] = useState<Record<string, string>>({});
  const [submittingUserInputRequests, setSubmittingUserInputRequests] = useState<Set<string>>(() => new Set());
  const [lastError, setLastError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [runtimeProvisioning, setRuntimeProvisioning] = useState(false);
  const [versionLoading, setVersionLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeWindow, setActiveWindow] = useState<"chat" | "improvement" | "asset">("chat");
  const [assetRefreshRevision, setAssetRefreshRevision] = useState(0);
  const [playgroundDrawer, setPlaygroundDrawer] = useState<"runtime-settings" | null>(null);
  const [sessionSidebarOpen, setSessionSidebarOpen] = useState(false);
  const [evidencePanelOpen, setEvidencePanelOpen] = useState(false);
  const [evidencePanelWidth, setEvidencePanelWidth] = useState(EVIDENCE_PANEL_DEFAULT_WIDTH);
  const [activeTraceMessageId, setActiveTraceMessageId] = useState<string | undefined>();
  const [feedbackDrawerOpen, setFeedbackDrawerOpen] = useState(false);
  const [feedbackContext, setFeedbackContext] = useState<FeedbackContext | null>(null);

  const shouldMigrateLegacyApiBase = shouldMigrateStoredApiBase(clientConfig.apiBase, runtimeDefaults.apiBase);
  const migratedClientConfig = useMemo<RuntimeClientConfig>(() => {
    if (!shouldMigrateLegacyApiBase) return clientConfig;
    return {
      apiBase: runtimeDefaults.apiBase,
      apiKey: clientConfig.apiKey || runtimeDefaults.apiKey,
    };
  }, [clientConfig, runtimeDefaults.apiBase, runtimeDefaults.apiKey, shouldMigrateLegacyApiBase]);

  const effectiveClientConfig = useMemo<RuntimeClientConfig>(() => ({
    apiBase: migratedClientConfig.apiBase || runtimeDefaults.apiBase,
    apiKey: migratedClientConfig.apiKey || runtimeDefaults.apiKey,
  }), [migratedClientConfig, runtimeDefaults]);
  const configMapping = useConfigMapping(effectiveClientConfig, selectedBusinessAgentId, setLastError);
  const { agents, skills } = useAgentCatalog(effectiveClientConfig, selectedBusinessAgentId, setLastError);
  const agentPresentation = useAgentPresentation(effectiveClientConfig, selectedBusinessAgentId);
  const promptSuggestion = usePromptSuggestion(activeSessionId, setInput);
  const calibrateTrace = usePlaygroundTrace(effectiveClientConfig, setMessagesBySession);

  const resetPlaygroundTransientState = useCallback(() => {
    setAlertId("");
    setCaseId("");
    setInput("");
    setStreamingAssistantMessageId(undefined);
    setUserInputErrors({});
    setSubmittingUserInputRequests(new Set());
    setLastError(undefined);
    setPlaygroundDrawer(null);
    setSessionSidebarOpen(false);
    setEvidencePanelOpen(false);
    setActiveTraceMessageId(undefined);
    setFeedbackDrawerOpen(false);
    setFeedbackContext(null);
  }, []);

  useEffect(() => {
    if (!shouldMigrateLegacyApiBase) return;
    setClientConfig((current) => {
      if (!shouldMigrateStoredApiBase(current.apiBase, runtimeDefaults.apiBase)) return current;
      return migratedClientConfig;
    });
  }, [migratedClientConfig, runtimeDefaults.apiBase, setClientConfig, shouldMigrateLegacyApiBase]);
  const apiDocsUrl = useMemo(() => makeApiDocsUrl(effectiveClientConfig.apiBase), [effectiveClientConfig.apiBase]);
  const langfuseUrl = useMemo(() => defaultLangfuseUrl(), []);

  const activeMessages = activeSessionId ? messagesBySession[activeSessionId] || [] : [];
  const activeMessagesLoaded = Boolean(
    activeSessionId && Object.prototype.hasOwnProperty.call(messagesBySession, activeSessionId),
  );
  const activeMessageCount = activeMessages.length;
  const activeBackendSession = useMemo(
    () => sessions.find((session) => session.session_id === activeSessionId),
    [activeSessionId, sessions],
  );
  const selectedBusinessAgent = useMemo(
    () => businessAgents.find((agent) => agent.agent_id === selectedBusinessAgentId),
    [businessAgents, selectedBusinessAgentId],
  );
  const activeRuntimeAgentId = activeBackendSession?.agent_id || selectedBusinessAgent?.runtime_agent_id || "";
  const activeBackendRunId = activeBackendSession?.active_run_id || undefined;
  const activeTraceMessage = useMemo(() => {
    if (activeTraceMessageId) {
      const selected = activeMessages.find((message) => message.id === activeTraceMessageId);
      if (selected?.role === "assistant") return selected;
    }
    if (streamingAssistantMessageId) {
      const streamingMessage = activeMessages.find((message) => message.id === streamingAssistantMessageId);
      if (streamingMessage?.role === "assistant") return streamingMessage;
    }
    return undefined;
  }, [activeMessages, activeTraceMessageId, streamingAssistantMessageId]);
  const activeTraceEvents = activeTraceMessage?.events || [];

  const refresh = useCallback(async () => {
    setLoading(true);
    setLastError(undefined);
    try {
      const [healthRes, businessAgentsRes] = await Promise.all([
        getHealth(effectiveClientConfig),
        listBusinessAgents(effectiveClientConfig),
      ]);
      setHealth(healthRes);
      const sessionGroups = await Promise.all(
        businessAgentsRes.map((agent) => getSessions(effectiveClientConfig, agent.agent_id)),
      );
      const sessionsRes = sessionGroups.flat();
      setSessions(sessionsRes);
      setBusinessAgents(businessAgentsRes);
      if (reconcilePlaygroundScope(businessAgentsRes, sessionsRes)) resetPlaygroundTransientState();
      const [repositoryRes, currentRefRes, changeSetsRes, releasesRes] = await Promise.all([
        getAgentRepositoryStatus(effectiveClientConfig),
        getCurrentAgentRef(effectiveClientConfig),
        getAgentChangeSets(effectiveClientConfig),
        getAgentReleases(effectiveClientConfig),
      ]);
      setAgentRepository(repositoryRes);
      setCurrentAgentRef(currentRefRes);
      setAgentChangeSets(changeSetsRes);
      setAgentReleases(releasesRes);
    } catch (error) {
      setLastError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, [effectiveClientConfig, reconcilePlaygroundScope, resetPlaygroundTransientState]);

  const refreshAll = useCallback(() => { setAssetRefreshRevision((value) => value + 1); return refresh(); }, [refresh]);

  const refreshVersions = useCallback(async () => {
    setVersionLoading(true);
    try {
      const [repositoryRes, currentRefRes, changeSetsRes, releasesRes] = await Promise.all([
        getAgentRepositoryStatus(effectiveClientConfig),
        getCurrentAgentRef(effectiveClientConfig),
        getAgentChangeSets(effectiveClientConfig),
        getAgentReleases(effectiveClientConfig),
      ]);
      setAgentRepository(repositoryRes);
      setCurrentAgentRef(currentRefRes);
      setAgentChangeSets(changeSetsRes);
      setAgentReleases(releasesRes);
    } finally {
      setVersionLoading(false);
    }
  }, [effectiveClientConfig]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    window.localStorage.removeItem("playground-session-messages");
  }, []);

  useEffect(() => {
    if (
      runState.phase !== "idle"
      || !activeSessionId
      || !activeBackendRunId
      || runState.lastRunId === activeBackendRunId
    ) {
      return;
    }
    dispatchRun({
      type: "observe_backend_run",
      operationId: `detached:${activeSessionId}:${activeBackendRunId}`,
      sessionId: activeSessionId,
      runId: activeBackendRunId,
    });
  }, [activeBackendRunId, activeSessionId, runState.lastRunId, runState.phase]);

  useEffect(() => {
    if (
      !activeSessionId
      || !activeRuntimeAgentId
      || activeMessageCount > 0
      || (streaming && runState.source !== "detached")
    ) return;

    const controller = new AbortController();
    void loadPlaygroundHistory(
      effectiveClientConfig,
      activeRuntimeAgentId,
      activeSessionId,
      controller.signal,
    )
      .then(({ history, status, runs, restoredMessages }) => {
        if (controller.signal.aborted) return;
        setMessagesBySession((prev) => {
          if ((prev[activeSessionId] || []).length > 0) return prev;
          return { ...prev, [activeSessionId]: restoredMessages };
        });
        setSessions((current) => current.map((session) => (
          session.session_id === activeSessionId
            ? { ...session, status: status.status, is_running: history.is_running }
            : session
        )));
        const activeRun = [...runs].reverse().find((run) => {
          const value = typeof run.status === "string" ? run.status : run.turn_status;
          return ["queued", "running", "waiting_human", "waiting_external", "finalizing"].includes(String(value || ""));
        });
        if (status.status !== "idle" && activeRun?.run_id) {
          const operationId = `detached:${activeSessionId}:${activeRun.run_id}`;
          if (runState.phase === "idle") {
            dispatchRun({
              type: "observe_backend_run",
              operationId,
              sessionId: activeSessionId,
              runId: activeRun.run_id,
            });
          }
          if (
            status.status === "awaiting_permission"
            || status.status === "awaiting_external_result"
            || activeRun.status === "waiting_human"
            || activeRun.status === "waiting_external"
          ) {
            dispatchRun({ type: "awaiting_input", operationId });
          }
        }
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setLastError(error instanceof Error ? `加载历史会话失败：${error.message}` : `加载历史会话失败：${String(error)}`);
      });

    return () => {
      controller.abort();
    };
  }, [
    activeMessageCount,
    activeRuntimeAgentId,
    activeSessionId,
    effectiveClientConfig,
    runState.phase,
    runState.source,
    setMessagesBySession,
    streaming,
  ]);

  const refreshPlayground = useCallback(async () => {
    try {
      await refresh();
      if (!activeSessionId || !activeRuntimeAgentId || streaming) return;
      const { history, status, restoredMessages } = await loadPlaygroundHistory(
        effectiveClientConfig,
        activeRuntimeAgentId,
        activeSessionId,
      );
      setMessagesBySession((current) => ({ ...current, [activeSessionId]: restoredMessages }));
      setSessions((current) => current.map((session) => (
        session.session_id === activeSessionId
          ? { ...session, status: status.status, is_running: history.is_running }
          : session
      )));
    } catch (error) {
      setLastError(error instanceof Error ? `刷新会话失败：${error.message}` : `刷新会话失败：${String(error)}`);
    }
  }, [activeRuntimeAgentId, activeSessionId, effectiveClientConfig, refresh, streaming]);

  const provisionSelectedRuntime = useCallback(async () => {
    if (!selectedBusinessAgentId || streaming || runtimeProvisioning) return;
    setRuntimeProvisioning(true);
    setLastError(undefined);
    try {
      const current = await provisionRuntimeAgent(effectiveClientConfig, selectedBusinessAgentId);
      if (!current.provisioned || !current.runtime_agent_id) {
        throw new Error("Runtime 供给完成后未返回 AgentScope Agent ID。");
      }
      await refresh();
    } catch (error) {
      setLastError(error instanceof Error ? error.message : String(error));
    } finally {
      setRuntimeProvisioning(false);
    }
  }, [effectiveClientConfig, refresh, runtimeProvisioning, selectedBusinessAgentId, streaming]);

  function updateSessionMessages(sessionId: string, updater: (messages: ChatMessage[]) => ChatMessage[]) {
    setMessagesBySession((prev) => ({
      ...prev,
      [sessionId]: updater(prev[sessionId] || []),
    }));
  }

  function updateUserConfirmRequest(requestId: string, patch: Partial<RuntimeUserConfirmRequest>) {
    setMessagesBySession((prev) => {
      const next: Record<string, ChatMessage[]> = {};
      for (const [sessionId, messages] of Object.entries(prev)) {
        next[sessionId] = messages.map((message) => (
          message.userConfirmRequests?.some((request) => request.requestId === requestId)
            ? { ...message, userConfirmRequests: patchUserConfirmRequest(message.userConfirmRequests, requestId, patch) }
            : message
        ));
      }
      return next;
    });
  }

  function updateExternalExecutionRequest(requestId: string, patch: Partial<RuntimeExternalExecutionRequest>) {
    setMessagesBySession((prev) => {
      const next: Record<string, ChatMessage[]> = {};
      for (const [sessionId, messages] of Object.entries(prev)) {
        next[sessionId] = messages.map((message) => (
          message.externalExecutionRequests?.some((request) => request.requestId === requestId)
            ? {
                ...message,
                externalExecutionRequests: patchExternalExecutionRequest(
                  message.externalExecutionRequests,
                  requestId,
                  patch,
                ),
              }
            : message
        ));
      }
      return next;
    });
  }

  function cancelUserConfirmForMessage(sessionId: string, messageId: string) {
    const resolvedAt = new Date().toISOString();
    setMessagesBySession((prev) => {
      const messages = cancelWaitingUserConfirmRequests(prev[sessionId] || [], messageId, resolvedAt);
      return { ...prev, [sessionId]: messages };
    });
    setUserInputErrors({});
    setSubmittingUserInputRequests(new Set());
  }

  function cancelExternalExecutionForMessage(sessionId: string, messageId: string) {
    const resolvedAt = new Date().toISOString();
    setMessagesBySession((prev) => {
      const messages = cancelWaitingExternalExecutionRequests(prev[sessionId] || [], messageId, resolvedAt);
      return { ...prev, [sessionId]: messages };
    });
    setUserInputErrors({});
    setSubmittingUserInputRequests(new Set());
  }

  const { sendMessage, stopStream, submitUserConfirm, submitExternalExecution } = usePlaygroundRun({
    clientConfig: effectiveClientConfig,
    input,
    runState,
    dispatchRun,
    activeSessionId,
    activeMessages,
    activeMessagesLoaded,
    selectedBusinessAgentId,
    runtimeAgentId: activeRuntimeAgentId,
    alertId,
    caseId,
    promptSuggestion,
    setInput,
    setStreamingAssistantMessageId,
    setLastError,
    setSessionSidebarOpen,
    setEvidencePanelOpen,
    setActiveTraceMessageId,
    setUserInputErrors,
    setSubmittingUserInputRequests,
    claimLocalSession,
    updateSessionMessages,
    updateUserConfirmRequest,
    updateExternalExecutionRequest,
    cancelUserConfirmForMessage,
    cancelExternalExecutionForMessage,
    calibrateTrace,
    refresh,
  });

  function createSession() {
    if (streaming) return;
    startNewSession();
    resetPlaygroundTransientState();
  }

  function selectSession(sessionId: string) {
    if (streaming) return;
    if (selectScopedSession(sessionId)) resetPlaygroundTransientState();
  }

  function showPlaygroundWindow() {
    setActiveWindow("chat");
  }

  function showImprovementWindow() {
    setActiveWindow("improvement");
  }

  function showAssetWindow() {
    setActiveWindow("asset");
  }

  function selectBusinessAgent(agentId: string) {
    if (streaming) return;
    if (switchBusinessAgent(agentId)) resetPlaygroundTransientState();
  }

  const currentAgentName = selectedBusinessAgent?.name || (selectedBusinessAgentId || "默认业务 Agent");

  function openFeedbackDrawer(message?: ChatMessage) {
    const feedbackAlertId = message?.alertId || alertId.trim() || undefined;
    const feedbackCaseId = message?.caseId || caseId.trim() || undefined;
    setFeedbackContext({
      runId: message?.runId,
      sessionId: message?.sessionId || activeSessionId,
      agentVersionId: message?.agentVersionId || currentAgentRef?.agent_version_id,
      scenario: feedbackCaseId ? `case:${feedbackCaseId}` : feedbackAlertId ? `alert:${feedbackAlertId}` : "playground",
      taskId: message?.runId || activeSessionId || undefined,
      alertId: feedbackAlertId,
      caseId: feedbackCaseId,
      // selectedBusinessAgentId 已由实际 Agent 列表解析（优先默认业务 Agent，再取首个可用项）。
      agentId: selectedBusinessAgentId,
      agentName: currentAgentName,
    });
    setFeedbackDrawerOpen(true);
  }

  function getContextForMessage(message: ChatMessage) {
    // P1：简单拷贝消息上下文到剪贴板；ContextPackage 四类型在 P2。
    const text = [
      "# Playground 上下文",
      "",
      `Agent: ${currentAgentName}`,
      `Agent Version: ${message.agentVersionId || currentAgentRef?.agent_version_id || "-"}`,
      `Session: ${message.sessionId || activeSessionId || "-"}`,
      `Run: ${message.runId || "-"}`,
      `Alert: ${message.alertId || alertId.trim() || "-"}`,
      `Case: ${message.caseId || caseId.trim() || "-"}`,
      "",
      message.content,
    ].join("\n");
    void navigator.clipboard?.writeText(text).catch(() => {});
  }

  function openTracePanel(message: ChatMessage) {
    setActiveTraceMessageId(message.id);
    setEvidencePanelOpen(true);
    const sessionId = message.sessionId || activeSessionId;
    if (sessionId && message.runId && message.id !== streamingAssistantMessageId) {
      void calibrateTrace(sessionId, message.id, message.runId);
    }
  }

  function rerunMessage(message: ChatMessage) {
    if (streaming) return;
    const source = precedingUserInput(activeMessages, message.id);
    if (source !== undefined) promptSuggestion.handleInputChange(source);
  }

  return (
    <div className="app-shell">
      <Topbar
        health={health}
        activeWindow={activeWindow}
        loading={loading}
        businessAgents={businessAgents}
        selectedBusinessAgentId={selectedBusinessAgentId}
        agentSwitchDisabled={streaming}
        onSelectBusinessAgent={selectBusinessAgent}
        onRefresh={refreshAll}
        onOpenPlayground={showPlaygroundWindow}
        onOpenImprovement={showImprovementWindow}
        onOpenAsset={showAssetWindow}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {activeWindow === "asset" ? (
        <AssetRegistry
          clientConfig={effectiveClientConfig}
          scopeAgentId={selectedBusinessAgentId}
          businessAgents={businessAgents}
          refreshRevision={assetRefreshRevision}
        />
      ) : activeWindow === "improvement" ? (
        <ImprovementWorkbench
          clientConfig={effectiveClientConfig}
          scopeAgentId={selectedBusinessAgentId}
          langfuseUrl={langfuseUrl}
          releases={agentReleases}
          changeSets={agentChangeSets}
          onGovernanceRefresh={refreshAll}
        />
      ) : (
        <div className="playground-shell" data-testid="playground-shell">
          {sessionSidebarOpen ? (
            <PlaygroundSessionSidebar
              sessions={scopedSessions}
              activeSessionId={activeSessionId}
              onSelectSession={selectSession}
              onNewSession={createSession}
              onRefresh={refreshPlayground}
              streaming={streaming}
            />
          ) : null}
          <ChatPanel
            messages={activeMessages}
            input={input}
            streaming={streaming}
            runState={runState}
            streamingAssistantMessageId={streamingAssistantMessageId}
            activeSessionId={activeSessionId}
            sessionSidebarOpen={sessionSidebarOpen}
            agentName={currentAgentName}
            agentPresentation={agentPresentation}
            runtimeReady={Boolean(activeRuntimeAgentId)}
            runtimeProvisioning={runtimeProvisioning}
            promptSuggestions={promptSuggestion.suggestions}
            onInputChange={promptSuggestion.handleInputChange}
            onUsePromptSuggestion={promptSuggestion.apply}
            onSend={sendMessage}
            onProvisionRuntime={() => { void provisionSelectedRuntime(); }}
            onStop={stopStream}
            onToggleSession={() => { setSessionSidebarOpen((open) => !open); setPlaygroundDrawer(null); }}
            onOpenRuntimeSettings={() => { setSessionSidebarOpen(false); setPlaygroundDrawer("runtime-settings"); }}
            onOpenFeedback={openFeedbackDrawer}
            onOpenTrace={openTracePanel}
            onGetContext={getContextForMessage}
            onRerun={rerunMessage}
            userInputErrors={userInputErrors}
            submittingUserInputRequests={submittingUserInputRequests}
            onSubmitUserInput={submitUserConfirm}
            onSubmitExternalExecution={submitExternalExecution}
          />
          {evidencePanelOpen ? (
            <PlaygroundEvidencePanel
              message={activeTraceMessage}
              events={activeTraceEvents}
              streaming={streaming}
              langfuseUrl={langfuseUrl}
              width={evidencePanelWidth}
              onWidthChange={setEvidencePanelWidth}
              onRetryTrace={() => { if (activeTraceMessage) openTracePanel(activeTraceMessage); }}
              onClose={() => setEvidencePanelOpen(false)}
            />
          ) : null}
          {playgroundDrawer === "runtime-settings" ? (
            <PlaygroundRuntimeSettingsDrawer
              clientConfig={effectiveClientConfig}
              agents={agents}
              skills={skills}
              alertId={alertId}
              caseId={caseId}
              streaming={streaming}
              onAlertIdChange={setAlertId}
              onCaseIdChange={setCaseId}
              health={health}
              configMapping={configMapping}
              selectedBusinessAgentId={selectedBusinessAgentId}
              lastError={lastError}
              onConfigApplied={() => setTimeout(refresh, 0)}
              onClose={() => setPlaygroundDrawer(null)}
            />
          ) : null}
          <FeedbackDrawer
            open={feedbackDrawerOpen}
            context={feedbackContext}
            clientConfig={effectiveClientConfig}
            onClose={() => setFeedbackDrawerOpen(false)}
            onCreated={() => { setFeedbackDrawerOpen(false); setActiveWindow("improvement"); setTimeout(refresh, 0); }}
          />
        </div>
      )}
      <SettingsModal
        open={settingsOpen}
        config={effectiveClientConfig}
        apiDocsUrl={apiDocsUrl}
        langfuseUrl={langfuseUrl}
        onClose={() => setSettingsOpen(false)}
        onSave={(next) => {
          setClientConfig(next);
          setSettingsOpen(false);
          setTimeout(refresh, 0);
        }}
        onAgentsChanged={() => setTimeout(refresh, 0)}
        onOpenAgentTestAssets={(agentId) => { selectBusinessAgent(agentId); setSettingsOpen(false); setActiveWindow("asset"); }}
      />
    </div>
  );
}

function precedingUserInput(messages: ChatMessage[], messageId: string) {
  const index = messages.findIndex((message) => message.id === messageId);
  for (let current = index - 1; current >= 0; current -= 1) {
    if (messages[current].role === "user") return messages[current].content;
  }
  return undefined;
}

async function loadPlaygroundHistory(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  const [history, status, runs] = await Promise.all([
    getRuntimeSessionMessages(config, agentId, sessionId, signal),
    getRuntimeSessionStatus(config, agentId, sessionId, signal),
    getAgentRuns(config, { session_id: sessionId, limit: 500 }, signal),
  ]);
  const waitingRun = runs.find((run) => ["waiting_human", "waiting_external"].includes(String(run.status || "")));
  const pendingActions = waitingRun?.run_id
    ? await getAgentRunPendingActions(config, waitingRun.run_id, signal)
    : [];
  return {
    history,
    status,
    runs,
    restoredMessages: messagesFromAgentScopeMessages(history.messages, sessionId, runs, pendingActions),
  };
}
