import type { AgentVersionSummary, RuntimeClientConfig } from "../../types/runtime";

export interface RuntimeIntegrationContext {
  runId?: string;
  sessionId?: string;
  sdkSessionId?: string;
  agentVersionId?: string;
  alertId?: string;
  caseId?: string;
  actorId?: string;
  eventId?: string;
  sourceSystem?: string;
}

export interface MonitoringIntegrationConfig {
  langfuseUrl?: string;
  traceUrlTemplate?: string;
}

export interface ExternalFeedbackWorkspaceProps {
  clientConfig: RuntimeClientConfig;
  runtimeContext?: RuntimeIntegrationContext;
  monitoringConfig?: MonitoringIntegrationConfig;
  currentAgentVersion?: AgentVersionSummary | null;
  agentVersions?: AgentVersionSummary[];
  versionLoading?: boolean;
  versionError?: string;
  onRefreshVersions?: () => void | Promise<void>;
  refreshToken?: number;
  onFeedbackChanged?: () => void;
}
