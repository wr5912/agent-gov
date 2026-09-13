import { useCallback, useMemo, useRef, useState } from "react";
import type { AgentSummary, SessionInfo } from "../types/runtime";
import { useLocalStorage } from "./useLocalStorage";

interface PlaygroundSessionScopeOptions {
  sessions: SessionInfo[];
}

interface LocalSessionOwner {
  businessAgentId: string;
  runtimeAgentId: string;
}

export function usePlaygroundSessionScope({
  sessions,
}: PlaygroundSessionScopeOptions) {
  const [selectedBusinessAgentId, setStoredBusinessAgentId] = useLocalStorage(
    "playground-selected-business-agent",
    "",
  );
  const [activeSessionId, setStoredActiveSessionId] = useLocalStorage<string | undefined>(
    "playground-active-session",
    undefined,
  );
  const [localSessionOwners, setLocalSessionOwners] = useState<Record<string, LocalSessionOwner>>({});

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
    const runtimeOwners = new Map(
      agents
        .filter((agent) => agent.runtime_agent_id)
        .map((agent) => [agent.runtime_agent_id, agent.agent_id]),
    );
    let nextSelectedAgentId = selectedAgentRef.current;
    let nextActiveSessionId = activeSessionRef.current;

    if (nextActiveSessionId) {
      const canonical = nextSessions.find((session) => session.session_id === nextActiveSessionId);
      const owner = canonical?.business_agent_id
        || (canonical?.agent_id ? runtimeOwners.get(canonical.agent_id) : undefined)
        || localOwnersRef.current[nextActiveSessionId]?.businessAgentId;
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
    const owner = canonical?.business_agent_id || localOwnersRef.current[sessionId]?.businessAgentId;
    if (!owner || owner !== selectedAgentRef.current || sessionId === activeSessionRef.current) {
      return false;
    }
    storeActiveSession(sessionId);
    return true;
  }, [sessions, storeActiveSession]);

  const claimLocalSession = useCallback((sessionId: string, businessAgentId: string, runtimeAgentId: string) => {
    if (!sessionId || !businessAgentId || !runtimeAgentId) return;
    const nextOwners = {
      ...localOwnersRef.current,
      [sessionId]: { businessAgentId, runtimeAgentId },
    };
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
    return sessions.filter((session) => session.business_agent_id === selectedBusinessAgentId);
  }, [selectedBusinessAgentId, sessions]);

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
