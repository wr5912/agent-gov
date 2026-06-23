import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deleteSession, defaultRuntimeConfig, getAgentRuns, getAgents, getAgentChangeSets, getAgentReleases, getAgentRepositoryStatus, getConfigMapping, getCurrentAgentRef, getHealth, getSessions, getSkills, isLegacyDockerApiBase, listBusinessAgents, streamChat } from "./api/runtime";
import { ChatPanel } from "./components/ChatPanel";
import { ImprovementWorkbench } from "./components/ImprovementWorkbench";
import { ReleaseWorkbench } from "./components/ReleaseWorkbench";
import { AssetRegistry } from "./components/AssetRegistry";
import { EVIDENCE_PANEL_DEFAULT_WIDTH, PlaygroundEvidencePanel } from "./components/PlaygroundEvidencePanel";
import { PlaygroundRuntimeSettingsDrawer } from "./components/PlaygroundRuntimeSettingsDrawer";
import { PlaygroundSessionSidebar } from "./components/PlaygroundSessionSidebar";
import { FeedbackDrawer, type FeedbackContext } from "./components/FeedbackDrawer";
import { SettingsModal } from "./components/SettingsModal";
import { Topbar } from "./components/Topbar";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { messagesFromAgentRuns } from "./playgroundHistory";
import type { AgentActivity, AgentChangeSet, AgentGitRef, AgentInfo, AgentRelease, AgentRepositoryStatus, AgentSummary, ChatMessage, ConfigMappingResponse, RuntimeClientConfig, RuntimeHealth, SessionInfo, SkillInfo, StreamEnvelope, StreamLogEvent } from "./types/runtime";
import { isRecord } from "./utils/records";
import "./styles.css";

function newId(prefix: string) {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
  return `${prefix}_${random}`;
}

// 会话 ID 使用裸 UUID（不带 sess_ 前缀），便于后端首次 SDK 调用直接把 session_id 传给 Claude SDK 并对齐 sdk_session_id（整改方案 §4.3 / Phase 1）。消息等临时 UI ID 仍可带前缀。
function newSessionId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
}

function parseCsv(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function parseOptionalCsv(value: string): string[] | undefined {
  const items = parseCsv(value);
  return items.length ? items : undefined;
}

function makeApiDocsUrl(apiBase: string): string {
  const base = apiBase.trim().replace(/\/$/, "");
  if (!base) return "/docs";
  return `${base}/docs`;
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "0.0.0.0" || hostname === "::1";
}

function defaultLangfuseUrl(): string {
  const configured = (import.meta.env.VITE_LANGFUSE_URL || "http://localhost:53000").trim();
  let parsed: URL | null = null;
  try {
    parsed = new URL(configured);
  } catch {
    parsed = null;
  }
  // 运维已把 Langfuse 地址显式指向非本机的可达地址时，直接采用该配置。
  if (parsed && !isLoopbackHost(parsed.hostname)) return configured;
  // 配置仍是本机/缺省地址：按当前浏览器访问的 host 派生，使远端用户跳转到可达的 Langfuse 地址（端口沿用配置值）。
  if (typeof window !== "undefined" && window.location?.hostname) {
    const protocol = window.location.protocol === "https:" ? "https" : "http";
    const port = parsed?.port || "53000";
    return `${protocol}://${window.location.hostname}${port ? `:${port}` : ""}`;
  }
  return configured;
}

function messageTextFromEnvelope(envelope: StreamEnvelope): string | undefined {
  if (envelope.event !== "message" || !isRecord(envelope.data)) return undefined;
  const text = envelope.data.text;
  return typeof text === "string" ? text : undefined;
}

function agentActivityFromResult(value: unknown): AgentActivity | undefined {
  if (!isRecord(value) || !isRecord(value.agent_activity)) return undefined;
  const activity = value.agent_activity;
  if (!Array.isArray(activity.tool_calls) || !Array.isArray(activity.tool_results)) return undefined;
  return activity as unknown as AgentActivity;
}

export default function App() {
  const runtimeDefaults = useMemo(() => defaultRuntimeConfig(), []);
  const [clientConfig, setClientConfig] = useLocalStorage<RuntimeClientConfig>("runtime-client-config", runtimeDefaults);
  const [messagesBySession, setMessagesBySession] = useLocalStorage<Record<string, ChatMessage[]>>("playground-session-messages", {});
  const [activeSessionId, setActiveSessionId] = useLocalStorage<string | undefined>("playground-active-session", undefined);

  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [configMapping, setConfigMapping] = useState<ConfigMappingResponse | null>(null);
  const [agentRepository, setAgentRepository] = useState<AgentRepositoryStatus | null>(null);
  const [currentAgentRef, setCurrentAgentRef] = useState<AgentGitRef | null>(null);
  const [agentChangeSets, setAgentChangeSets] = useState<AgentChangeSet[]>([]);
  const [agentReleases, setAgentReleases] = useState<AgentRelease[]>([]);
  const [businessAgents, setBusinessAgents] = useState<AgentSummary[]>([]);
  const [selectedBusinessAgentId, setSelectedBusinessAgentId] = useState("");
  const [selectedAgent, setSelectedAgent] = useState("");
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [allowedTools, setAllowedTools] = useState("");
  const [disallowedTools, setDisallowedTools] = useState("");
  const [skillsMode, setSkillsMode] = useState<"all" | "default" | "none">("default");
  const [alertId, setAlertId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [maxTurns, setMaxTurns] = useState(16);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamingAssistantMessageId, setStreamingAssistantMessageId] = useState<string | undefined>();
  const [streamEvents, setStreamEvents] = useState<StreamLogEvent[]>([]);
  const [lastError, setLastError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [versionLoading, setVersionLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeWindow, setActiveWindow] = useState<"chat" | "improvement" | "release" | "asset">("chat");
  const [playgroundDrawer, setPlaygroundDrawer] = useState<"runtime-settings" | null>(null);
  const [sessionSidebarOpen, setSessionSidebarOpen] = useState(false);
  const [evidencePanelOpen, setEvidencePanelOpen] = useState(false);
  const [evidencePanelWidth, setEvidencePanelWidth] = useState(EVIDENCE_PANEL_DEFAULT_WIDTH);
  const [activeTraceMessageId, setActiveTraceMessageId] = useState<string | undefined>();
  const [feedbackDrawerOpen, setFeedbackDrawerOpen] = useState(false);
  const [feedbackContext, setFeedbackContext] = useState<FeedbackContext | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const shouldMigrateLegacyApiBase = isLegacyDockerApiBase(clientConfig.apiBase) && !isLegacyDockerApiBase(runtimeDefaults.apiBase);
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

  useEffect(() => {
    if (!shouldMigrateLegacyApiBase) return;
    setClientConfig((current) => {
      if (!isLegacyDockerApiBase(current.apiBase)) return current;
      return migratedClientConfig;
    });
  }, [migratedClientConfig, setClientConfig, shouldMigrateLegacyApiBase]);
  const apiDocsUrl = useMemo(() => makeApiDocsUrl(effectiveClientConfig.apiBase), [effectiveClientConfig.apiBase]);
  const langfuseUrl = useMemo(() => defaultLangfuseUrl(), []);

  const activeMessages = activeSessionId ? messagesBySession[activeSessionId] || [] : [];
  const activeMessageCount = activeMessages.length;
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

  const mergedSessions = useMemo(() => {
    const localOnly = Object.keys(messagesBySession)
      .filter((sessionId) => !sessions.some((session) => session.session_id === sessionId))
      .map<SessionInfo>((sessionId) => ({
        session_id: sessionId,
        created_at: messagesBySession[sessionId]?.[0]?.createdAt || new Date().toISOString(),
        updated_at: messagesBySession[sessionId]?.at(-1)?.createdAt || new Date().toISOString(),
        title: messagesBySession[sessionId]?.find((message) => message.role === "user")?.content.slice(0, 80) || "本地新会话",
        turns: Math.max(0, Math.floor((messagesBySession[sessionId]?.length || 0) / 2)),
        metadata: { localOnly: true },
      }));
    return [...sessions, ...localOnly];
  }, [messagesBySession, sessions]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLastError(undefined);
    try {
      const [healthRes, sessionsRes, agentsRes, skillsRes, configRes, businessAgentsRes] = await Promise.all([
        getHealth(effectiveClientConfig),
        getSessions(effectiveClientConfig),
        getAgents(effectiveClientConfig),
        getSkills(effectiveClientConfig),
        getConfigMapping(effectiveClientConfig),
        listBusinessAgents(effectiveClientConfig),
      ]);
      setHealth(healthRes);
      setSessions(sessionsRes);
      setAgents(agentsRes);
      setSkills(skillsRes);
      setConfigMapping(configRes);
      setBusinessAgents(businessAgentsRes);
      if (!activeSessionId && sessionsRes[0]?.session_id) {
        setActiveSessionId(sessionsRes[0].session_id);
      }
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
  }, [activeSessionId, effectiveClientConfig, setActiveSessionId]);

  const refreshAll = useCallback(async () => {
    await refresh();
  }, [refresh]);

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
    if (!activeSessionId || activeMessageCount > 0 || streaming) return;
    const backendSession = sessions.find((session) => session.session_id === activeSessionId);
    if (!backendSession || backendSession.turns <= 0) return;

    let cancelled = false;
    void getAgentRuns(effectiveClientConfig, { session_id: activeSessionId, limit: 100, include_messages: true })
      .then((runs) => {
        if (cancelled) return;
        const restoredMessages = messagesFromAgentRuns(runs);
        if (!restoredMessages.length) return;
        setMessagesBySession((prev) => {
          if ((prev[activeSessionId] || []).length > 0) return prev;
          return { ...prev, [activeSessionId]: restoredMessages };
        });
      })
      .catch((error) => {
        if (cancelled) return;
        setLastError(error instanceof Error ? `加载历史会话失败：${error.message}` : `加载历史会话失败：${String(error)}`);
      });

    return () => {
      cancelled = true;
    };
  }, [activeMessageCount, activeSessionId, effectiveClientConfig, sessions, setMessagesBySession, streaming]);

  function updateSessionMessages(sessionId: string, updater: (messages: ChatMessage[]) => ChatMessage[]) {
    setMessagesBySession((prev) => ({
      ...prev,
      [sessionId]: updater(prev[sessionId] || []),
    }));
  }

  function createSession() {
    const sessionId = newSessionId();
    setActiveSessionId(sessionId);
    setMessagesBySession((prev) => ({ ...prev, [sessionId]: [] }));
    setStreamEvents([]);
    setActiveTraceMessageId(undefined);
    setEvidencePanelOpen(false);
    setSessionSidebarOpen(false);
  }

  function selectSession(sessionId: string) {
    setActiveSessionId(sessionId);
    setStreamEvents([]);
    setActiveTraceMessageId(undefined);
    setEvidencePanelOpen(false);
    setSessionSidebarOpen(false);
  }

  async function removeSession(sessionId: string) {
    setLastError(undefined);
    try {
      const session = sessions.find((item) => item.session_id === sessionId);
      if (session) await deleteSession(effectiveClientConfig, sessionId);
      setMessagesBySession((prev) => {
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
      if (activeSessionId === sessionId) setActiveSessionId(undefined);
      if (activeSessionId === sessionId) {
        setActiveTraceMessageId(undefined);
        setEvidencePanelOpen(false);
      }
      await refresh();
    } catch (error) {
      setLastError(error instanceof Error ? error.message : String(error));
    }
  }

  function toggleSkill(skillName: string) {
    setSelectedSkills((prev) => prev.includes(skillName) ? prev.filter((name) => name !== skillName) : [...prev, skillName]);
  }

  async function sendMessage() {
    const message = input.trim();
    if (!message || streaming) return;

    const sessionId = activeSessionId || newSessionId();
    setActiveSessionId(sessionId);
    setInput("");
    setStreaming(true);
    setStreamingAssistantMessageId(undefined);
    setLastError(undefined);
    setStreamEvents([]);
    setSessionSidebarOpen(false);
    setEvidencePanelOpen(true);

    const userMessage: ChatMessage = {
      id: newId("msg"),
      role: "user",
      content: message,
      createdAt: new Date().toISOString(),
    };
    const assistantMessage: ChatMessage = {
      id: newId("msg"),
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString(),
      sessionId,
      alertId: alertId.trim() || undefined,
      caseId: caseId.trim() || undefined,
      events: [],
    };
    setStreamingAssistantMessageId(assistantMessage.id);
    setActiveTraceMessageId(assistantMessage.id);

    updateSessionMessages(sessionId, (prev) => [...prev, userMessage, assistantMessage]);

    const controller = new AbortController();
    abortRef.current = controller;

    const appendAssistantEvent = (event: StreamLogEvent) => {
      setStreamEvents((prev) => [...prev.slice(-199), event]);
      updateSessionMessages(sessionId, (prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === "assistant") {
          next[next.length - 1] = {
            ...last,
            events: [...(last.events || []), event],
          };
        }
        return next;
      });
    };

    try {
      await streamChat(
        effectiveClientConfig,
        {
          session_id: sessionId,
          alert_id: alertId.trim() || undefined,
          case_id: caseId.trim() || undefined,
          message,
          agent: selectedAgent || undefined,
          agent_id: selectedBusinessAgentId || undefined,
          skills: selectedSkills.length ? selectedSkills : undefined,
          skills_mode: skillsMode,
          allowed_tools: parseOptionalCsv(allowedTools),
          disallowed_tools: parseOptionalCsv(disallowedTools),
          max_turns: maxTurns,
          metadata: {
            client: "agent-gov-ui",
          },
        },
        {
          onSession: (runtimeSessionId) => {
            if (runtimeSessionId && runtimeSessionId !== sessionId) {
              setActiveSessionId(runtimeSessionId);
            }
          },
          onEnvelope: (envelope) => {
            const event: StreamLogEvent = {
              id: newId("evt"),
              event: envelope.event,
              text: messageTextFromEnvelope(envelope),
              data: envelope.data,
              createdAt: new Date().toISOString(),
            };
            appendAssistantEvent(event);
          },
          onText: (text) => {
            updateSessionMessages(sessionId, (prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                next[next.length - 1] = {
                  ...last,
                  content: `${last.content}${text}`,
                };
              }
              return next;
            });
          },
          onResult: (result) => {
            if (!isRecord(result)) return;
            const runId = typeof result.run_id === "string" ? result.run_id : undefined;
            const resultSdkSessionId = typeof result.sdk_session_id === "string" ? result.sdk_session_id : undefined;
            const resultAgentVersionId = typeof result.agent_version_id === "string" ? result.agent_version_id : undefined;
            const resultSessionId = typeof result.session_id === "string" ? result.session_id : sessionId;
            const resultAlertId = typeof result.alert_id === "string" ? result.alert_id : alertId.trim() || undefined;
            const resultCaseId = typeof result.case_id === "string" ? result.case_id : caseId.trim() || undefined;
            const agentActivity = agentActivityFromResult(result);
            updateSessionMessages(sessionId, (prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                next[next.length - 1] = {
                  ...last,
                  runId,
                  sdkSessionId: resultSdkSessionId,
                  agentVersionId: resultAgentVersionId,
                  sessionId: resultSessionId,
                  alertId: resultAlertId,
                  caseId: resultCaseId,
                  agentActivity,
                };
              }
              return next;
            });
          },
          onError: (messageText) => {
            setLastError(messageText);
            updateSessionMessages(sessionId, (prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant" && !last.content) {
                next[next.length - 1] = { ...last, content: `运行失败：\n${messageText}` };
              }
              return next;
            });
          },
          onDone: () => {
            setStreaming(false);
            abortRef.current = null;
            refresh();
          },
        },
        controller.signal,
      );
    } catch (error) {
      if (!controller.signal.aborted && (error as Error).name !== "AbortError") {
        const messageText = error instanceof Error ? error.message : String(error);
        appendAssistantEvent({
          id: newId("evt"),
          event: "error",
          data: { message: messageText },
          createdAt: new Date().toISOString(),
        });
        setLastError(messageText);
        updateSessionMessages(sessionId, (prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant" && !last.content) {
            next[next.length - 1] = { ...last, content: `运行失败：\n${messageText}` };
          }
          return next;
        });
      }
    } finally {
      setStreaming(false);
      setStreamingAssistantMessageId(undefined);
      abortRef.current = null;
    }
  }

  function stopStream() {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setStreamingAssistantMessageId(undefined);
  }

  function showPlaygroundWindow() {
    setActiveWindow("chat");
  }

  function showImprovementWindow() {
    setActiveWindow("improvement");
  }

  function showReleaseWindow() {
    setActiveWindow("release");
  }

  function showAssetWindow() {
    setActiveWindow("asset");
  }

  const currentAgentName = businessAgents.find((a) => a.agent_id === selectedBusinessAgentId)?.name
    || (selectedBusinessAgentId || "默认 Agent");

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
      agentId: selectedBusinessAgentId || "main-agent",
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
  }

  function rerunMessage(message: ChatMessage) {
    const idx = activeMessages.findIndex((m) => m.id === message.id);
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (activeMessages[i].role === "user") { setInput(activeMessages[i].content); break; }
    }
  }

  return (
    <div className="app-shell">
      <Topbar
        health={health}
        activeWindow={activeWindow}
        loading={loading}
        businessAgents={businessAgents}
        selectedBusinessAgentId={selectedBusinessAgentId}
        onSelectBusinessAgent={setSelectedBusinessAgentId}
        onRefresh={refreshAll}
        onOpenPlayground={showPlaygroundWindow}
        onOpenImprovement={showImprovementWindow}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {activeWindow === "asset" ? (
        <AssetRegistry
          clientConfig={effectiveClientConfig}
          scopeAgentId={selectedBusinessAgentId}
          businessAgents={businessAgents}
        />
      ) : activeWindow === "release" ? (
        <ReleaseWorkbench
          clientConfig={effectiveClientConfig}
          scopeAgentId={selectedBusinessAgentId}
          releases={agentReleases}
          changeSets={agentChangeSets}
          onRefresh={refreshAll}
        />
      ) : activeWindow === "improvement" ? (
        <ImprovementWorkbench clientConfig={effectiveClientConfig} scopeAgentId={selectedBusinessAgentId} langfuseUrl={langfuseUrl} />
      ) : (
        <div className="playground-shell" data-testid="playground-shell">
          {sessionSidebarOpen ? (
            <PlaygroundSessionSidebar
              sessions={mergedSessions}
              activeSessionId={activeSessionId}
              onSelectSession={selectSession}
              onNewSession={createSession}
              onDeleteSession={removeSession}
              onRefresh={refresh}
              onClose={() => setSessionSidebarOpen(false)}
            />
          ) : null}
          <ChatPanel
            messages={activeMessages}
            input={input}
            streaming={streaming}
            streamingAssistantMessageId={streamingAssistantMessageId}
            activeSessionId={activeSessionId}
            agentName={currentAgentName}
            onInputChange={setInput}
            onSend={sendMessage}
            onStop={stopStream}
            onOpenSession={() => { setSessionSidebarOpen((open) => !open); setPlaygroundDrawer(null); }}
            onOpenRuntimeSettings={() => { setSessionSidebarOpen(false); setPlaygroundDrawer("runtime-settings"); }}
            onOpenFeedback={openFeedbackDrawer}
            onOpenTrace={openTracePanel}
            onGetContext={getContextForMessage}
            onRerun={rerunMessage}
          />
          {evidencePanelOpen ? (
            <PlaygroundEvidencePanel
              message={activeTraceMessage}
              events={activeTraceEvents}
              streaming={streaming}
              langfuseUrl={langfuseUrl}
              width={evidencePanelWidth}
              onWidthChange={setEvidencePanelWidth}
              onClose={() => setEvidencePanelOpen(false)}
            />
          ) : null}
          {playgroundDrawer === "runtime-settings" ? (
            <PlaygroundRuntimeSettingsDrawer
              agents={agents}
              skills={skills}
              selectedAgent={selectedAgent}
              selectedSkills={selectedSkills}
              onSelectAgent={setSelectedAgent}
              onToggleSkill={toggleSkill}
              alertId={alertId}
              caseId={caseId}
              allowedTools={allowedTools}
              disallowedTools={disallowedTools}
              maxTurns={maxTurns}
              skillsMode={skillsMode}
              onAlertIdChange={setAlertId}
              onCaseIdChange={setCaseId}
              onAllowedToolsChange={setAllowedTools}
              onDisallowedToolsChange={setDisallowedTools}
              onMaxTurnsChange={setMaxTurns}
              onSkillsModeChange={setSkillsMode}
              health={health}
              configMapping={configMapping}
              streamEvents={streamEvents}
              lastError={lastError}
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
        onOpenAsset={showAssetWindow}
      />
    </div>
  );
}
