import { useEffect, useState } from "react";
import { getConfigMapping } from "../api/runtime";
import type { ConfigMappingResponse, RuntimeClientConfig } from "../types/runtime";

export function useConfigMapping(
  clientConfig: RuntimeClientConfig,
  agentId: string,
  onError: (message: string) => void,
) {
  const [configMapping, setConfigMapping] = useState<ConfigMappingResponse | null>(null);

  useEffect(() => {
    if (!agentId) {
      setConfigMapping(null);
      return;
    }
    let cancelled = false;
    getConfigMapping(clientConfig, agentId)
      .then((mapping) => {
        if (!cancelled) setConfigMapping(mapping);
      })
      .catch((error) => {
        if (cancelled) return;
        setConfigMapping(null);
        onError(error instanceof Error ? error.message : String(error));
      });
    return () => { cancelled = true; };
  }, [agentId, clientConfig, onError]);

  return configMapping;
}
