import { useEffect, useState } from "react";
import { getBusinessAgentPresentation } from "../api/runtime";
import type { AgentPresentation, RuntimeClientConfig } from "../types/runtime";

export function useAgentPresentation(
  clientConfig: RuntimeClientConfig,
  agentId: string,
) {
  const [presentation, setPresentation] = useState<AgentPresentation | null>(null);

  useEffect(() => {
    setPresentation(null);
    if (!agentId) return;

    const controller = new AbortController();
    void getBusinessAgentPresentation(clientConfig, agentId, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setPresentation(value);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setPresentation(null);
        console.warn(
          `Business Agent presentation unavailable for ${agentId}:`,
          error instanceof Error ? error.message : String(error),
        );
      });
    return () => controller.abort();
  }, [agentId, clientConfig]);

  return presentation?.agent_id === agentId ? presentation : null;
}
