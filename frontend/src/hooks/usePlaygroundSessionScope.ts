import { useCallback, useMemo, useRef, useState } from "react";
import type { AgentSummary, ChatMessage, SessionInfo } from "../types/runtime";
import { useLocalStorage } from "./useLocalStorage";

interface PlaygroundSessionScopeOptions {
  sessions: SessionInfo[];
  messagesBySession: Record<string, ChatMessage[]>;
}

export function usePlaygroundSessionScope({
  sessions,
  messagesBySession,
}: PlaygroundSessionScopeOptions) {
  const [selectedBusinessAgentId, setStoredBusinessAgentId] = useLocalStorage(
    "playground-selected-business-agent",
    "",
  );
  const [activeSessionId, setStoredActiveSessionId] = useLocalStorage<string | undefined>(
    "playground-active-session",
    undefined,
  );
  const [localSessionOwners, setLocalSessionOwners] = useState<Record<string, string>>({});

  const selectedAgentRef = useRef(selectedBusinessAgentId);
  const activeSessionRef = useRef(activeSessionId);
  const localOwnersRef = useRef(localSessionOwners);
  selectedAgentRef.current = selectedBusinessAgentId;
  activeSessionRef.current = activeSessionId;
  localOwnersRef.current = localSessionOwners;

  const storeSelectedAgent = useCallback((agentId: string) => {
    selectedAgentRef.current = agentId;
    setStoredBusinessAgentId(agentId);
  }, [setStoredBusinessAgentId]);

  const storeActiveSession = useCallback((sessionId: string | undefined) => {
    activeSessionRef.current = sessionId;
    setStoredActiveSessionId(sessionId);
  }, [setStoredActiveSessionId]);

  const reconcile = useCallback((agents: AgentSummary[], nextSessions: SessionInfo[]) => {
    const availableAgentIds = new Set(agents.map((agent) => agent.agent_id));
    let nextSelectedAgentId = selectedAgentRef.current;
    let nextActiveSessionId = activeSessionRef.current;

    if (nextActiveSessionId) {
      const canonical = nextSessions.find((session) => session.session_id === nextActiveSessionId);
      const owner = canonical?.agent_id || localOwnersRef.current[nextActiveSessionId];
      if (owner && availableAgentIds.has(owner)) {
        nextSelectedAgentId = owner;
      } else {
        nextActiveSessionId = undefined;
      }
    }

    if (!nextActiveSessionId && !availableAgentIds.has(nextSelectedAgentId)) {
      nextSelectedAgentId = agents.find((agent) => agent.default)?.agent_id || agents[0]?.agent_id || "";
    }

    const changed = (
      nextSelectedAgentId !== selectedAgentRef.current
      || nextActiveSessionId !== activeSessionRef.current
    );
    if (nextSelectedAgentId !== selectedAgentRef.current) storeSelectedAgent(nextSelectedAgentId);
    if (nextActiveSessionId !== activeSessionRef.current) storeActiveSession(nextActiveSessionId);
    return changed;
  }, [storeActiveSession, storeSelectedAgent]);

  const switchBusinessAgent = useCallback((agentId: string) => {
    if (!agentId || agentId === selectedAgentRef.current) return false;
    storeSelectedAgent(agentId);
    storeActiveSession(undefined);
    return true;
  }, [storeActiveSession, storeSelectedAgent]);

  const startNewSession = useCallback(() => {
    if (!activeSessionRef.current) return false;
    storeActiveSession(undefined);
    return true;
  }, [storeActiveSession]);

  const selectSession = useCallback((sessionId: string) => {
    const canonical = sessions.find((session) => session.session_id === sessionId);
    const owner = canonical?.agent_id || localOwnersRef.current[sessionId];
    if (!owner || owner !== selectedAgentRef.current || sessionId === activeSessionRef.current) {
      return false;
    }
    storeActiveSession(sessionId);
    return true;
  }, [sessions, storeActiveSession]);

  const claimLocalSession = useCallback((sessionId: string, agentId: string) => {
    if (!sessionId || !agentId) return;
    const nextOwners = { ...localOwnersRef.current, [sessionId]: agentId };
    localOwnersRef.current = nextOwners;
    setLocalSessionOwners(nextOwners);
    storeActiveSession(sessionId);
  }, [storeActiveSession]);

  const forgetSession = useCallback((sessionId: string) => {
    if (sessionId in localOwnersRef.current) {
      const nextOwners = { ...localOwnersRef.current };
      delete nextOwners[sessionId];
      localOwnersRef.current = nextOwners;
      setLocalSessionOwners(nextOwners);
    }
    if (activeSessionRef.current === sessionId) storeActiveSession(undefined);
  }, [storeActiveSession]);

  const scopedSessions = useMemo(() => {
    if (!selectedBusinessAgentId) return [];
    const canonicalIds = new Set(sessions.map((session) => session.session_id));
    const localOnly = Object.entries(messagesBySession)
      .filter(([sessionId]) => !canonicalIds.has(sessionId))
      .filter(([sessionId]) => localSessionOwners[sessionId] === selectedBusinessAgentId)
      .map<SessionInfo>(([sessionId, messages]) => ({
        session_id: sessionId,
        agent_id: selectedBusinessAgentId,
        created_at: messages[0]?.createdAt || new Date().toISOString(),
        updated_at: messages.at(-1)?.createdAt || new Date().toISOString(),
        title: messages.find((message) => message.role === "user")?.content.slice(0, 80) || "本地新会话",
        turns: Math.max(0, Math.floor(messages.length / 2)),
        metadata: { localOnly: true },
      }));
    return [
      ...sessions.filter((session) => session.agent_id === selectedBusinessAgentId),
      ...localOnly,
    ];
  }, [localSessionOwners, messagesBySession, selectedBusinessAgentId, sessions]);

  return {
    selectedBusinessAgentId,
    activeSessionId,
    scopedSessions,
    reconcile,
    switchBusinessAgent,
    startNewSession,
    selectSession,
    claimLocalSession,
    forgetSession,
  };
}
