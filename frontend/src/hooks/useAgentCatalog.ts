import { useEffect, useState } from "react";
import { getAgents, getSkills } from "../api/runtime";
import type { AgentInfo, RuntimeClientConfig, SkillInfo } from "../types/runtime";

export function useAgentCatalog(
  clientConfig: RuntimeClientConfig,
  agentId: string,
  onError: (message: string) => void,
) {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);

  useEffect(() => {
    if (!agentId) {
      setAgents([]);
      setSkills([]);
      return;
    }
    let cancelled = false;
    Promise.all([getAgents(clientConfig, agentId), getSkills(clientConfig, agentId)])
      .then(([nextAgents, nextSkills]) => {
        if (cancelled) return;
        setAgents(nextAgents);
        setSkills(nextSkills);
      })
      .catch((error) => {
        if (cancelled) return;
        setAgents([]);
        setSkills([]);
        onError(error instanceof Error ? error.message : String(error));
      });
    return () => { cancelled = true; };
  }, [agentId, clientConfig, onError]);

  return { agents, skills };
}
