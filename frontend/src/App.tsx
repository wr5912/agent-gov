import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deleteSession, defaultRuntimeConfig, getAgentChangeSets, getAgentReleases, getAgentRepositoryStatus, getConversationItems, getCurrentAgentRef, getHealth, getSessions, isLegacyDockerApiBase, listBusinessAgents, streamChat, submitClaudeUserInputDecision } from "./api/runtime";
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
import { useConfigMapping } from "./hooks/useConfigMapping";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { cancelWaitingUserInputRequests, claudeUserInputRequestFromData, mergeUserInputRequest, nullableString, patchUserInputRequest, sanitizedEnvelopeData, stringValue } from "./claudeUserInputState";
import { messagesFromConversationItems } from "./playgroundHistory";
import { usePromptSuggestion } from "./hooks/usePromptSuggestion";
import { newId, newSessionId } from "./utils/ids";
import type { AgentActivity, AgentChangeSet, AgentGitRef, AgentRelease, AgentRepositoryStatus, AgentSummary, ChatMessage, ClaudeUserInputDecisionPayload, ClaudeUserInputRequest, RuntimeClientConfig, RuntimeHealth, SessionInfo, StreamEnvelope, StreamLogEvent } from "./types/runtime";
import { isRecord } from "./utils/records";
import "./styles.css";

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
  const [messagesBySession, setMessagesBySession] = useState<Record<string, ChatMessage[]>>({});
  const [activeSessionId, setActiveSessionId] = useLocalStorage<string | undefined>("playground-active-session", undefined);

  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [agentRepository, setAgentRepository] = useState<AgentRepositoryStatus | null>(null);
  const [currentAgentRef, setCurrentAgentRef] = useState<AgentGitRef | null>(null);
  const [agentChangeSets, setAgentChangeSets] = useState<AgentChangeSet[]>([]);
  const [agentReleases, setAgentReleases] = useState<AgentRelease[]>([]);
  const [businessAgents, setBusinessAgents] = useState<AgentSummary[]>([]);
  const [selectedBusinessAgentId, setSelectedBusinessAgentId] = useState("");
  const [alertId, setAlertId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [maxTurns, setMaxTurns] = useState(16);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamingAssistantMessageId, setStreamingAssistantMessageId] = useState<string | undefined>();
  const [streamEvents, setStreamEvents] = useState<StreamLogEvent[]>([]);
  const [userInputErrors, setUserInputErrors] = useState<Record<string, string>>({});
  const [submittingUserInputRequests, setSubmittingUserInputRequests] = useState<Set<string>>(() => new Set());
  const [lastError, setLastError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [versionLoading, setVersionLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeWindow, setActiveWindow] = useState<"chat" | "improvement" | "asset">("chat");
  const [playgroundDrawer, setPlaygroundDrawer] = useState<"runtime-settings" | null>(null);
  const [sessionSidebarOpen, setSessionSidebarOpen] = useState(false);
  const [evidencePanelOpen, setEvidencePanelOpen] = useState(false);
  const [evidencePanelWidth, setEvidencePanelWidth] = useState(EVIDENCE_PANEL_DEFAULT_WIDTH);
  const [activeTraceMessageId, setActiveTraceMessageId] = useState<string | undefined>();
  const [feedbackDrawerOpen, setFeedbackDrawerOpen] = useState(false);
  const [feedbackContext, setFeedbackContext] = useState<FeedbackContext | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const decisionTokensRef = useRef<Record<string, string>>({});
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
  const configMapping = useConfigMapping(effectiveClientConfig, selectedBusinessAgentId, setLastError);
  const { agents, skills } = useAgentCatalog(effectiveClientConfig, selectedBusinessAgentId, setLastError);
  const promptSuggestion = usePromptSuggestion(activeSessionId, setInput);

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
  const activeBackendSessionTurns = useMemo(
    () => sessions.find((session) => session.session_id === activeSessionId)?.turns ?? 0,
    [activeSessionId, sessions],
  );
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
      const healthRequest = getHealth(effectiveClientConfig).then((response) => {
        setHealth(response);
        return response;
      });
      const [, sessionsRes, businessAgentsRes] = await Promise.all([
        healthRequest,
        getSessions(effectiveClientConfig),
        listBusinessAgents(effectiveClientConfig),
      ]);
      setSessions(sessionsRes);
      setBusinessAgents(businessAgentsRes);
      // 全局运行 Agent 必须是具体对象；跨 Agent 聚合视图由各治理页面自己的范围筛选负责。
      setSelectedBusinessAgentId((current) => {
        if (current && businessAgentsRes.some((agent) => agent.agent_id === current)) return current;
        return businessAgentsRes.find((agent) => agent.default)?.agent_id || businessAgentsRes[0]?.agent_id || "";
      });
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
    window.localStorage.removeItem("playground-session-messages");
  }, []);

  useEffect(() => {
    if (!activeSessionId || activeMessageCount > 0 || streaming || activeBackendSessionTurns <= 0) return;

    const controller = new AbortController();
    void getConversationItems(effectiveClientConfig, activeSessionId, controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        const restoredMessages = messagesFromConversationItems(items, activeSessionId);
        if (!restoredMessages.length) return;
        setMessagesBySession((prev) => {
          if ((prev[activeSessionId] || []).length > 0) return prev;
          return { ...prev, [activeSessionId]: restoredMessages };
        });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setLastError(error instanceof Error ? `加载历史会话失败：${error.message}` : `加载历史会话失败：${String(error)}`);
      });

    return () => {
      controller.abort();
    };
  }, [activeBackendSessionTurns, activeMessageCount, activeSessionId, effectiveClientConfig, setMessagesBySession, streaming]);

  function updateSessionMessages(sessionId: string, updater: (messages: ChatMessage[]) => ChatMessage[]) {
    setMessagesBySession((prev) => ({
      ...prev,
      [sessionId]: updater(prev[sessionId] || []),
    }));
  }

  function updateUserInputRequest(requestId: string, patch: Partial<ClaudeUserInputRequest>) {
    setMessagesBySession((prev) => {
      const next: Record<string, ChatMessage[]> = {};
      for (const [sessionId, messages] of Object.entries(prev)) {
        next[sessionId] = messages.map((message) => (
          message.userInputRequests?.some((request) => request.request_id === requestId)
            ? { ...message, userInputRequests: patchUserInputRequest(message.userInputRequests, requestId, patch) }
            : message
        ));
      }
      return next;
    });
  }

  function cancelUserInputForMessage(
    sessionId: string | undefined,
    messageId: string | undefined,
    decision: "client_cancelled" | "runtime_interrupted",
  ) {
    if (!sessionId || !messageId) return;
    const resolvedAt = new Date().toISOString();
    let changed = false;
    setMessagesBySession((prev) => {
      const result = cancelWaitingUserInputRequests(prev[sessionId] || [], messageId, decision, resolvedAt);
      changed = result.requestIds.length > 0;
      return changed ? { ...prev, [sessionId]: result.messages } : prev;
    });
    decisionTokensRef.current = {};
    setUserInputErrors({});
    setSubmittingUserInputRequests(new Set());
  }

  async function submitUserInputDecision(
    request: ClaudeUserInputRequest,
    input: Omit<ClaudeUserInputDecisionPayload, "decision_token">,
  ) {
    const token = decisionTokensRef.current[request.request_id];
    if (!token) {
      setUserInputErrors((prev) => ({ ...prev, [request.request_id]: "当前确认已失效，请重新运行本轮任务。" }));
      return;
    }
    setUserInputErrors((prev) => {
      const next = { ...prev };
      delete next[request.request_id];
      return next;
    });
    setSubmittingUserInputRequests((prev) => new Set(prev).add(request.request_id));
    try {
      const result = await submitClaudeUserInputDecision(effectiveClientConfig, request.request_id, {
        ...input,
        decision_token: token,
      });
      delete decisionTokensRef.current[request.request_id];
      updateUserInputRequest(request.request_id, {
        status: result.status,
        decision: result.decision,
        resolved_at: result.resolved_at || new Date().toISOString(),
      });
    } catch (error) {
      setUserInputErrors((prev) => ({
        ...prev,
        [request.request_id]: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      setSubmittingUserInputRequests((prev) => {
        const next = new Set(prev);
        next.delete(request.request_id);
        return next;
      });
    }
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
      if (session?.active_run_id || (streaming && activeSessionId === sessionId)) {
        setLastError("会话运行中，完成或取消后才能删除。");
        return;
      }
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

  async function sendMessage() {
    const message = input.trim();
    if (!message || streaming) return;

    const sessionId = activeSessionId || newSessionId();
    promptSuggestion.clear(sessionId);
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

    let streamCompleted = false;
    try {
      await streamChat(
        effectiveClientConfig,
        {
          session_id: sessionId,
          alert_id: alertId.trim() || undefined,
          case_id: caseId.trim() || undefined,
          message,
          agent_id: selectedBusinessAgentId || undefined,
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
              data: sanitizedEnvelopeData(envelope),
              createdAt: new Date().toISOString(),
            };
            appendAssistantEvent(event);
            if (envelope.event === "claude_user_input_required") {
              const request = claudeUserInputRequestFromData(envelope.data);
              if (request) {
                if (request.decision_token) decisionTokensRef.current[request.request_id] = request.decision_token;
                setUserInputErrors((prev) => {
                  const next = { ...prev };
                  delete next[request.request_id];
                  return next;
                });
                updateSessionMessages(sessionId, (prev) => {
                  const next = [...prev];
                  const last = next[next.length - 1];
                  if (last?.role === "assistant") {
                    const safeRequest = { ...request, decision_token: undefined };
                    next[next.length - 1] = {
                      ...last,
                      userInputRequests: mergeUserInputRequest(last.userInputRequests, safeRequest),
                    };
                  }
                  return next;
                });
              }
            }
            if (envelope.event === "claude_user_input_resolved" && isRecord(envelope.data)) {
              const requestId = stringValue(envelope.data.request_id);
              if (requestId) {
                delete decisionTokensRef.current[requestId];
                updateUserInputRequest(requestId, {
                  status: envelope.data.status === "cancelled" ? "cancelled" : "resolved",
                  decision: nullableString(envelope.data.decision),
                  resolved_at: nullableString(envelope.data.resolved_at) || new Date().toISOString(),
                });
              }
            }
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
          onPromptSuggestion: (suggestions, runtimeSessionId) => promptSuggestion.receive(runtimeSessionId, suggestions),
          onResult: (result) => {
            if (!isRecord(result)) return;
            const runId = typeof result.run_id === "string" ? result.run_id : undefined;
            const resultSdkSessionId = typeof result.sdk_session_id === "string" ? result.sdk_session_id : undefined;
            const resultAgentVersionId = typeof result.agent_version_id === "string" ? result.agent_version_id : undefined;
            const resultLangfuseTraceId = typeof result.langfuse_trace_id === "string" ? result.langfuse_trace_id : undefined;
            const resultLangfuseTraceUrl = typeof result.langfuse_trace_url === "string" ? result.langfuse_trace_url : undefined;
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
                  langfuseTraceId: resultLangfuseTraceId,
                  langfuseTraceUrl: resultLangfuseTraceUrl,
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
              if (last?.role === "assistant") {
                const failureText = `运行失败：\n${messageText}`;
                next[next.length - 1] = {
                  ...last,
                  content: last.content ? `${last.content}\n\n${failureText}` : failureText,
                };
              }
              return next;
            });
          },
          onDone: () => {
            streamCompleted = true;
            setStreaming(false);
            abortRef.current = null;
            setSubmittingUserInputRequests(new Set());
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
          if (last?.role === "assistant") {
            const failureText = `运行失败：\n${messageText}`;
            next[next.length - 1] = {
              ...last,
              content: last.content ? `${last.content}\n\n${failureText}` : failureText,
            };
          }
          return next;
        });
      }
    } finally {
      if (!streamCompleted) {
        cancelUserInputForMessage(
          sessionId,
          assistantMessage.id,
          controller.signal.aborted ? "client_cancelled" : "runtime_interrupted",
        );
      }
      setStreaming(false);
      setStreamingAssistantMessageId(undefined);
      abortRef.current = null;
    }
  }

  function stopStream() {
    cancelUserInputForMessage(activeSessionId, streamingAssistantMessageId, "client_cancelled");
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

  function showAssetWindow() {
    setActiveWindow("asset");
  }

  const currentAgentName = businessAgents.find((a) => a.agent_id === selectedBusinessAgentId)?.name
    || (selectedBusinessAgentId || "默认业务 Agent");

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
  }

  function rerunMessage(message: ChatMessage) {
    const idx = activeMessages.findIndex((m) => m.id === message.id);
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (activeMessages[i].role === "user") { promptSuggestion.handleInputChange(activeMessages[i].content); break; }
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
        onOpenAsset={showAssetWindow}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {activeWindow === "asset" ? (
        <AssetRegistry
          clientConfig={effectiveClientConfig}
          scopeAgentId={selectedBusinessAgentId}
          businessAgents={businessAgents}
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
              sessions={mergedSessions}
              activeSessionId={activeSessionId}
              onSelectSession={selectSession}
              onNewSession={createSession}
              onDeleteSession={removeSession}
              onRefresh={refresh}
              streaming={streaming}
            />
          ) : null}
          <ChatPanel
            messages={activeMessages}
            input={input}
            streaming={streaming}
            streamingAssistantMessageId={streamingAssistantMessageId}
            activeSessionId={activeSessionId}
            sessionSidebarOpen={sessionSidebarOpen}
            agentName={currentAgentName}
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
            onSubmitUserInput={submitUserInputDecision}
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
              clientConfig={effectiveClientConfig}
              agents={agents}
              skills={skills}
              activeSessionId={activeSessionId}
              alertId={alertId}
              caseId={caseId}
              maxTurns={maxTurns}
              streaming={streaming}
              onAlertIdChange={setAlertId}
              onCaseIdChange={setCaseId}
              onMaxTurnsChange={setMaxTurns}
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
      />
    </div>
  );
}
