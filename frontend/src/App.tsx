import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deleteSession, defaultRuntimeConfig, getAgents, getConfigMapping, getHealth, getSessions, getSkills, streamChat } from "./api/runtime";
import { ChatPanel } from "./components/ChatPanel";
import { Inspector } from "./components/Inspector";
import { SettingsModal } from "./components/SettingsModal";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { useLocalStorage } from "./hooks/useLocalStorage";
import type { AgentInfo, ChatMessage, ConfigMappingResponse, RuntimeClientConfig, RuntimeHealth, SessionInfo, SkillInfo, StreamEnvelope, StreamLogEvent } from "./types/runtime";
import "./styles.css";

function newId(prefix: string) {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
  return `${prefix}_${random}`;
}

function parseCsv(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function makeApiDocsUrl(apiBase: string): string {
  const base = apiBase.trim().replace(/\/$/, "");
  if (!base) return "/docs";
  return `${base}/docs`;
}

function defaultLangfuseUrl(): string {
  return (import.meta.env.VITE_LANGFUSE_URL || "http://localhost:53000").trim();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function messageTextFromEnvelope(envelope: StreamEnvelope): string | undefined {
  if (envelope.event !== "message" || !isRecord(envelope.data)) return undefined;
  const text = envelope.data.text;
  return typeof text === "string" ? text : undefined;
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
  const [selectedAgent, setSelectedAgent] = useState("");
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [allowedTools, setAllowedTools] = useState("Read,Grep,Glob");
  const [disallowedTools, setDisallowedTools] = useState("Bash,WebFetch,WebSearch");
  const [skillsMode, setSkillsMode] = useState<"all" | "default" | "none">("default");
  const [maxTurns, setMaxTurns] = useState(8);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamEvents, setStreamEvents] = useState<StreamLogEvent[]>([]);
  const [lastError, setLastError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  const effectiveClientConfig = useMemo<RuntimeClientConfig>(() => ({
    apiBase: clientConfig.apiBase || runtimeDefaults.apiBase,
    apiKey: clientConfig.apiKey || runtimeDefaults.apiKey,
  }), [clientConfig, runtimeDefaults]);
  const apiDocsUrl = useMemo(() => makeApiDocsUrl(effectiveClientConfig.apiBase), [effectiveClientConfig.apiBase]);
  const langfuseUrl = useMemo(() => defaultLangfuseUrl(), []);

  const activeMessages = activeSessionId ? messagesBySession[activeSessionId] || [] : [];

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
    } catch (error) {
      setLastError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, [activeSessionId, effectiveClientConfig, setActiveSessionId]);

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
          message,
          agent: selectedAgent || undefined,
          skills: selectedSkills.length ? selectedSkills : undefined,
          skills_mode: skillsMode,
          allowed_tools: parseCsv(allowedTools),
          disallowed_tools: parseCsv(disallowedTools),
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

  return (
    <div className="app-shell">
      <Topbar
        health={health}
        apiDocsUrl={apiDocsUrl}
        langfuseUrl={langfuseUrl}
        loading={loading}
        onRefresh={refresh}
        onOpenSettings={() => setSettingsOpen(true)}
      />
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
          allowedTools={allowedTools}
          disallowedTools={disallowedTools}
          maxTurns={maxTurns}
          skillsMode={skillsMode}
          onInputChange={setInput}
          onAllowedToolsChange={setAllowedTools}
          onDisallowedToolsChange={setDisallowedTools}
          onMaxTurnsChange={setMaxTurns}
          onSkillsModeChange={setSkillsMode}
          onSend={sendMessage}
          onStop={stopStream}
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
