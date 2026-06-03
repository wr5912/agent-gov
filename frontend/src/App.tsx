import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createFeedbackSignal, deleteSession, defaultRuntimeConfig, getAgents, getAgentChangeSets, getAgentReleases, getAgentRepositoryStatus, getConfigMapping, getCurrentAgentRef, getHealth, getSessions, getSkills, isLegacyDockerApiBase, streamChat } from "./api/runtime";
import { ChatPanel } from "./components/ChatPanel";
import { ExternalFeedbackWorkspace } from "./components/ExternalFeedbackWorkspace";
import { Inspector } from "./components/Inspector";
import { SettingsModal } from "./components/SettingsModal";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import type { RuntimeIntegrationContext } from "./components/feedback-workspace/types";
import { useLocalStorage } from "./hooks/useLocalStorage";
import type { FeedbackSignalCreateRequest, FeedbackSignalRecord } from "./types/feedback";
import type { AgentActivity, AgentChangeSet, AgentGitRef, AgentInfo, AgentRelease, AgentRepositoryStatus, ChatMessage, ConfigMappingResponse, RuntimeClientConfig, RuntimeHealth, SessionInfo, SkillInfo, StreamEnvelope, StreamLogEvent } from "./types/runtime";
import { isRecord } from "./utils/records";
import "./styles.css";

function newId(prefix: string) {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
  return `${prefix}_${random}`;
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

function defaultLangfuseUrl(): string {
  return (import.meta.env.VITE_LANGFUSE_URL || "http://localhost:53000").trim();
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
  const [selectedAgent, setSelectedAgent] = useState("");
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [allowedTools, setAllowedTools] = useState("");
  const [disallowedTools, setDisallowedTools] = useState("");
  const [skillsMode, setSkillsMode] = useState<"all" | "default" | "none">("default");
  const [alertId, setAlertId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [maxTurns, setMaxTurns] = useState(8);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamEvents, setStreamEvents] = useState<StreamLogEvent[]>([]);
  const [lastError, setLastError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [versionLoading, setVersionLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeWindow, setActiveWindow] = useState<"chat" | "feedback">("chat");
  const [feedbackRefreshToken, setFeedbackRefreshToken] = useState(0);

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
  const feedbackRuntimeContext = useMemo<RuntimeIntegrationContext | undefined>(() => {
    for (let index = activeMessages.length - 1; index >= 0; index -= 1) {
      const message = activeMessages[index];
      if (message.role !== "assistant") continue;
      if (message.runId || message.sessionId || message.sdkSessionId || message.agentVersionId || message.alertId || message.caseId) {
        return {
          runId: message.runId,
          sessionId: message.sessionId || activeSessionId,
          sdkSessionId: message.sdkSessionId,
          agentVersionId: message.agentVersionId,
          alertId: message.alertId,
          caseId: message.caseId,
          sourceSystem: "agent-playground",
        };
      }
    }
    if (!activeSessionId && !alertId.trim() && !caseId.trim() && !currentAgentRef?.agent_version_id) return undefined;
    return {
      sessionId: activeSessionId,
      alertId: alertId.trim() || undefined,
      caseId: caseId.trim() || undefined,
      agentVersionId: currentAgentRef?.agent_version_id,
      sourceSystem: "agent-playground",
    };
  }, [activeMessages, activeSessionId, alertId, caseId, currentAgentRef]);

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
      const [healthRes, sessionsRes, agentsRes, skillsRes, configRes] = await Promise.all([
        getHealth(effectiveClientConfig),
        getSessions(effectiveClientConfig),
        getAgents(effectiveClientConfig),
        getSkills(effectiveClientConfig),
        getConfigMapping(effectiveClientConfig),
      ]);
      setHealth(healthRes);
      setSessions(sessionsRes);
      setAgents(agentsRes);
      setSkills(skillsRes);
      setConfigMapping(configRes);
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
    setFeedbackRefreshToken((prev) => prev + 1);
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

  function updateSessionMessages(sessionId: string, updater: (messages: ChatMessage[]) => ChatMessage[]) {
    setMessagesBySession((prev) => ({
      ...prev,
      [sessionId]: updater(prev[sessionId] || []),
    }));
  }

  function createSession() {
    const sessionId = newId("sess");
    setActiveSessionId(sessionId);
    setMessagesBySession((prev) => ({ ...prev, [sessionId]: [] }));
    setStreamEvents([]);
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

    const sessionId = activeSessionId || newId("sess");
    setActiveSessionId(sessionId);
    setInput("");
    setStreaming(true);
    setLastError(undefined);
    setStreamEvents([]);

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
          skills: selectedSkills.length ? selectedSkills : undefined,
          skills_mode: skillsMode,
          allowed_tools: parseOptionalCsv(allowedTools),
          disallowed_tools: parseOptionalCsv(disallowedTools),
          max_turns: maxTurns,
          metadata: {
            client: "claude-agent-playground-ui",
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
      if ((error as Error).name !== "AbortError") {
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
      abortRef.current = null;
    }
  }

  function stopStream() {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }

  async function submitFeedback(payload: FeedbackSignalCreateRequest): Promise<FeedbackSignalRecord> {
    const result = await createFeedbackSignal(effectiveClientConfig, payload);
    setFeedbackRefreshToken((prev) => prev + 1);
    return result;
  }

  function showFeedbackWindow() {
    setActiveWindow("feedback");
  }

  function showPlaygroundWindow() {
    setActiveWindow("chat");
  }

  return (
    <div className="app-shell">
      <Topbar
        health={health}
        apiDocsUrl={apiDocsUrl}
        langfuseUrl={langfuseUrl}
        activeWindow={activeWindow}
        loading={loading}
        onRefresh={refreshAll}
        onOpenFeedback={showFeedbackWindow}
        onOpenPlayground={showPlaygroundWindow}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {activeWindow === "feedback" ? (
        <ExternalFeedbackWorkspace
          clientConfig={effectiveClientConfig}
          runtimeContext={feedbackRuntimeContext}
          monitoringConfig={{ langfuseUrl }}
          agentRepository={agentRepository}
          currentAgentRef={currentAgentRef}
          agentChangeSets={agentChangeSets}
          agentReleases={agentReleases}
          versionLoading={versionLoading}
          versionError={lastError}
          onRefreshVersions={() => refreshVersions().catch((error) => setLastError(error instanceof Error ? error.message : String(error)))}
          refreshToken={feedbackRefreshToken}
          onFeedbackChanged={() => setFeedbackRefreshToken((prev) => prev + 1)}
        />
      ) : (
        <div className="layout">
          <Sidebar
            sessions={mergedSessions}
            activeSessionId={activeSessionId}
            agents={agents}
            skills={skills}
            selectedAgent={selectedAgent}
            selectedSkills={selectedSkills}
            onSelectSession={(sessionId) => { setActiveSessionId(sessionId); setStreamEvents([]); }}
            onNewSession={createSession}
            onDeleteSession={removeSession}
            onRefresh={refresh}
            onSelectAgent={setSelectedAgent}
            onToggleSkill={toggleSkill}
          />
          <ChatPanel
            messages={activeMessages}
            input={input}
            streaming={streaming}
            activeSessionId={activeSessionId}
            alertId={alertId}
            caseId={caseId}
            allowedTools={allowedTools}
            disallowedTools={disallowedTools}
            maxTurns={maxTurns}
            skillsMode={skillsMode}
            onInputChange={setInput}
            onAlertIdChange={setAlertId}
            onCaseIdChange={setCaseId}
            onAllowedToolsChange={setAllowedTools}
            onDisallowedToolsChange={setDisallowedTools}
            onMaxTurnsChange={setMaxTurns}
            onSkillsModeChange={setSkillsMode}
            onSend={sendMessage}
            onStop={stopStream}
            onCreateFeedback={submitFeedback}
          />
          <Inspector
            health={health}
            agents={agents}
            skills={skills}
            configMapping={configMapping}
            streamEvents={streamEvents}
            lastError={lastError}
          />
        </div>
      )}
      <SettingsModal
        open={settingsOpen}
        config={effectiveClientConfig}
        onClose={() => setSettingsOpen(false)}
        onSave={(next) => {
          setClientConfig(next);
          setSettingsOpen(false);
          setTimeout(refresh, 0);
        }}
      />
    </div>
  );
}
