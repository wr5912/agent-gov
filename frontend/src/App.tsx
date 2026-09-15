import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import {
  defaultRuntimeConfig,
  deleteRuntimeSession,
  getAgentChangeSets,
  getAgentReleases,
  getHealth,
  getRuntimeWorkspaceMcps,
  getRuntimeWorkspaceSkills,
  getRuntimeWorkspaceStatus,
  getSessions,
  listBusinessAgents,
  renameRuntimeSession,
  shouldMigrateStoredApiBase,
} from "./api/runtime";
import { ChatPanel } from "./components/ChatPanel";
import { ImprovementWorkbench } from "./components/ImprovementWorkbench";
import { AssetRegistry } from "./components/AssetRegistry";
import { EVIDENCE_PANEL_DEFAULT_WIDTH, PlaygroundEvidencePanel } from "./components/PlaygroundEvidencePanel";
import {
  PlaygroundRuntimeSettingsDrawer,
  type RuntimeWorkspaceResources,
} from "./components/PlaygroundRuntimeSettingsDrawer";
import { PlaygroundSessionSidebar } from "./components/PlaygroundSessionSidebar";
import { FeedbackDrawer, type FeedbackContext } from "./components/FeedbackDrawer";
import { SettingsModal } from "./components/SettingsModal";
import { Topbar } from "./components/Topbar";
import { useAgentPresentation } from "./hooks/useAgentPresentation";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { usePlaygroundSessionScope } from "./hooks/usePlaygroundSessionScope";
import { usePlaygroundTrace } from "./hooks/usePlaygroundTrace";
import { usePlaygroundRun } from "./hooks/usePlaygroundRun";
import { cancelWaitingUserConfirmRequests, patchUserConfirmRequest } from "./runtimeUserConfirmState";
import {
  cancelWaitingExternalExecutionRequests,
  patchExternalExecutionRequest,
} from "./runtimeExternalExecutionState";
import { activeAgentGovRun } from "./playgroundHistory";
import { loadPlaygroundHistory, recoverPlaygroundHistory } from "./playgroundHistoryLoad";
import { usePromptSuggestion } from "./hooks/usePromptSuggestion";
import {
  initialPlaygroundRunState,
  isPlaygroundRunLocked,
  playgroundRunReducer,
} from "./playgroundRunState";
import type { AgentChangeSet, AgentRelease, AgentSummary, ChatMessage, RuntimeClientConfig, RuntimeExternalExecutionRequest, RuntimeHealth, RuntimeUserConfirmRequest, SessionInfo } from "./types/runtime";
import { defaultLangfuseUrl, makeApiDocsUrl } from "./runtimeUrls";
import { formatFeedbackEntities } from "./feedbackEntities";
import "./styles.css";

export default function App() {
  const runtimeDefaults = useMemo(() => defaultRuntimeConfig(), []);
  const [clientConfig, setClientConfig] = useLocalStorage<RuntimeClientConfig>("runtime-client-config", runtimeDefaults);
  const [messagesBySession, setMessagesBySession] = useState<Record<string, ChatMessage[]>>({});

  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
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
    forgetSession,
  } = usePlaygroundSessionScope({ sessions });
  const [input, setInput] = useState("");
  const [runState, dispatchRun] = useReducer(playgroundRunReducer, initialPlaygroundRunState);
  const streaming = isPlaygroundRunLocked(runState);
  const [streamingAssistantMessageId, setStreamingAssistantMessageId] = useState<string | undefined>();
  const [userInputErrors, setUserInputErrors] = useState<Record<string, string>>({});
  const [submittingUserInputRequests, setSubmittingUserInputRequests] = useState<Set<string>>(() => new Set());
  const [lastError, setLastError] = useState<string | undefined>();
  const [refreshError, setRefreshError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeWindow, setActiveWindow] = useState<"chat" | "improvement" | "asset">("chat");
  const [assetRefreshRevision, setAssetRefreshRevision] = useState(0);
  const [playgroundDrawer, setPlaygroundDrawer] = useState<"runtime-settings" | null>(null);
  const [runtimeResources, setRuntimeResources] = useState<RuntimeWorkspaceResources>({
    status: null,
    mcps: [],
    skills: [],
    loading: { status: false, mcp: false, skills: false },
    errors: {},
  });
  const [runtimeResourcesRevision, setRuntimeResourcesRevision] = useState(0);
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
  const agentPresentation = useAgentPresentation(effectiveClientConfig, selectedBusinessAgentId);
  const promptSuggestion = usePromptSuggestion(activeSessionId, setInput);
  const calibrateTrace = usePlaygroundTrace(effectiveClientConfig, setMessagesBySession);

  const resetPlaygroundTransientState = useCallback(() => {
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

  useEffect(() => {
    if (playgroundDrawer !== "runtime-settings" || !activeBackendSession?.agent_id) {
      setRuntimeResources({
        status: null,
        mcps: [],
        skills: [],
        loading: { status: false, mcp: false, skills: false },
        errors: {},
      });
      return;
    }
    const controller = new AbortController();
    setRuntimeResources({
      status: null,
      mcps: [],
      skills: [],
      loading: { status: true, mcp: true, skills: true },
      errors: {},
    });
    const load = async <K extends "status" | "mcp" | "skills", T>(
      key: K,
      request: Promise<T>,
      apply: (current: RuntimeWorkspaceResources, value: T) => RuntimeWorkspaceResources,
    ) => {
      try {
        const value = await request;
        if (!controller.signal.aborted) setRuntimeResources((current) => apply(current, value));
      } catch (error) {
        if (!controller.signal.aborted) {
          const message = error instanceof Error ? error.message : String(error);
          setRuntimeResources((current) => ({
            ...current,
            errors: { ...current.errors, [key]: message },
          }));
        }
      } finally {
        if (!controller.signal.aborted) {
          setRuntimeResources((current) => ({
            ...current,
            loading: { ...current.loading, [key]: false },
          }));
        }
      }
    };
    void load(
      "status",
      getRuntimeWorkspaceStatus(
        effectiveClientConfig,
        activeBackendSession.agent_id,
        activeBackendSession.session_id,
        controller.signal,
      ),
      (current, status) => ({ ...current, status }),
    );
    void load(
      "mcp",
      getRuntimeWorkspaceMcps(
        effectiveClientConfig,
        activeBackendSession.agent_id,
        activeBackendSession.session_id,
        controller.signal,
      ),
      (current, mcps) => ({ ...current, mcps }),
    );
    void load(
      "skills",
      getRuntimeWorkspaceSkills(
        effectiveClientConfig,
        activeBackendSession.agent_id,
        activeBackendSession.session_id,
        controller.signal,
      ),
      (current, skills) => ({ ...current, skills }),
    );
    return () => controller.abort();
  }, [activeBackendSession, effectiveClientConfig, playgroundDrawer, runtimeResourcesRevision]);

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
    setRefreshError(undefined);
    try {
      const [healthResult, agentsResult, changeSetsResult, releasesResult] = await Promise.allSettled([
        getHealth(effectiveClientConfig),
        listBusinessAgents(effectiveClientConfig),
        getAgentChangeSets(effectiveClientConfig),
        getAgentReleases(effectiveClientConfig),
      ]);
      const errors: string[] = [];
      if (healthResult.status === "fulfilled") setHealth(healthResult.value);
      else errors.push(`Runtime 状态加载失败：${errorMessage(healthResult.reason)}`);
      if (changeSetsResult.status === "fulfilled") setAgentChangeSets(changeSetsResult.value);
      else errors.push(`待发布更新加载失败：${errorMessage(changeSetsResult.reason)}`);
      if (releasesResult.status === "fulfilled") setAgentReleases(releasesResult.value);
      else errors.push(`发布版本加载失败：${errorMessage(releasesResult.reason)}`);
      if (agentsResult.status === "fulfilled") {
        const agents = agentsResult.value;
        setBusinessAgents(agents);
        const sessionResult = resolveSessionGroups(
          agents.map((agent) => agent.agent_id),
          await Promise.allSettled(agents.map((agent) => getSessions(effectiveClientConfig, agent.agent_id))),
        );
        if (sessionResult.ok) {
          setSessions(sessionResult.sessions);
          if (reconcilePlaygroundScope(agents, sessionResult.sessions)) resetPlaygroundTransientState();
        } else {
          errors.push(sessionResult.message);
        }
      } else {
        errors.push(`业务 Agent 列表加载失败，会话列表未刷新：${errorMessage(agentsResult.reason)}`);
      }
      if (errors.length > 0) setRefreshError(errors.join("；"));
    } catch (error) {
      setRefreshError(`刷新失败：${errorMessage(error)}`);
    } finally {
      setLoading(false);
    }
  }, [effectiveClientConfig, reconcilePlaygroundScope, resetPlaygroundTransientState]);

  const refreshAll = useCallback(() => { setAssetRefreshRevision((value) => value + 1); return refresh(); }, [refresh]);

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
    let recoveryMessage: string | undefined;
    void recoverPlaygroundHistory(
      effectiveClientConfig,
      activeRuntimeAgentId,
      activeSessionId,
      controller.signal,
      (error) => {
        if (controller.signal.aborted) return;
        recoveryMessage = `加载历史会话暂时失败：${error.message}；正在继续恢复同一会话。`;
        setLastError(recoveryMessage);
      },
    )
      .then(({ history, status, runs, restoredMessages }) => {
        if (controller.signal.aborted) return;
        if (recoveryMessage) setLastError((current) => current === recoveryMessage ? undefined : current);
        setMessagesBySession((prev) => {
          if ((prev[activeSessionId] || []).length > 0) return prev;
          return { ...prev, [activeSessionId]: restoredMessages };
        });
        setSessions((current) => current.map((session) => (
          session.session_id === activeSessionId
            ? { ...session, status: status.status, is_running: history.is_running }
            : session
        )));
        const activeRun = activeAgentGovRun(runs);
        if (activeRun?.run_id) {
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

  async function renameSession(sessionId: string, name: string) {
    const session = scopedSessions.find((item) => item.session_id === sessionId);
    if (!session?.agent_id) throw new Error("当前会话缺少 Runtime Agent 绑定，无法重命名。");
    await renameRuntimeSession(effectiveClientConfig, session.agent_id, sessionId, name);
    await refresh();
  }

  async function deleteSession(sessionId: string) {
    const session = scopedSessions.find((item) => item.session_id === sessionId);
    if (!session?.agent_id) throw new Error("当前会话缺少 Runtime Agent 绑定，无法删除。");
    await deleteRuntimeSession(effectiveClientConfig, session.agent_id, sessionId);
    setSessions((current) => current.filter((item) => item.session_id !== sessionId));
    setMessagesBySession((current) => {
      const next = { ...current };
      delete next[sessionId];
      return next;
    });
    forgetSession(sessionId);
    if (activeSessionId === sessionId) resetPlaygroundTransientState();
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
    setFeedbackContext({
      runId: message?.runId,
      sessionId: message?.sessionId || activeSessionId,
      agentVersionId: message?.agentVersionId || selectedBusinessAgent?.agent_version_id || undefined,
      scenario: "playground",
      taskId: message?.runId || activeSessionId || undefined,
      entities: message?.entities,
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
      `Agent Version: ${message.agentVersionId || selectedBusinessAgent?.agent_version_id || "-"}`,
      `Session: ${message.sessionId || activeSessionId || "-"}`,
      `Run: ${message.runId || "-"}`,
      `业务对象引用: ${formatFeedbackEntities(message.entities)}`,
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
      {refreshError ? <div className="error-box app-refresh-error" role="alert" data-testid="app-refresh-error">{refreshError}</div> : null}
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
              onRenameSession={renameSession}
              onDeleteSession={deleteSession}
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
            error={lastError}
            promptSuggestions={promptSuggestion.suggestions}
            onInputChange={promptSuggestion.handleInputChange}
            onUsePromptSuggestion={promptSuggestion.apply}
            onSend={sendMessage}
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
              session={activeBackendSession || null}
              businessAgent={selectedBusinessAgent || null}
              resources={runtimeResources}
              onRefresh={() => setRuntimeResourcesRevision((value) => value + 1)}
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
        changeSets={agentChangeSets}
        releases={agentReleases}
        apiDocsUrl={apiDocsUrl}
        langfuseUrl={langfuseUrl}
        onClose={() => setSettingsOpen(false)}
        onSave={(next) => {
          setClientConfig(next);
          setSettingsOpen(false);
          setTimeout(refresh, 0);
        }}
        onAgentsChanged={() => setTimeout(refresh, 0)}
        onGovernanceRefresh={refreshAll}
        onOpenAgentTestAssets={(agentId) => { selectBusinessAgent(agentId); setSettingsOpen(false); setActiveWindow("asset"); }}
      />
    </div>
  );
}

export function resolveSessionGroups(
  agentIds: string[],
  results: PromiseSettledResult<SessionInfo[]>[],
): { ok: true; sessions: SessionInfo[] } | { ok: false; message: string } {
  const failures = results.flatMap((result, index) => (
    result.status === "rejected" ? [`${agentIds[index]}：${errorMessage(result.reason)}`] : []
  ));
  if (failures.length > 0) {
    return { ok: false, message: `会话列表加载失败，保留上次成功加载的会话；${failures.join("；")}` };
  }
  return { ok: true, sessions: results.flatMap((result) => result.status === "fulfilled" ? result.value : []) };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function precedingUserInput(messages: ChatMessage[], messageId: string) {
  const index = messages.findIndex((message) => message.id === messageId);
  for (let current = index - 1; current >= 0; current -= 1) {
    if (messages[current].role === "user") return messages[current].content;
  }
  return undefined;
}
