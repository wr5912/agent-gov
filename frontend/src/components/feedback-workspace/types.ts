import type { AgentChangeSet, AgentGitRef, AgentRelease, AgentRepositoryStatus, RuntimeClientConfig } from "../../types/runtime";

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
  agentRepository?: AgentRepositoryStatus | null;
  currentAgentRef?: AgentGitRef | null;
  agentChangeSets?: AgentChangeSet[];
  agentReleases?: AgentRelease[];
  versionLoading?: boolean;
  versionError?: string;
  onRefreshVersions?: () => void | Promise<void>;
  refreshToken?: number;
  onFeedbackChanged?: () => void;
  onOpenImprovement?: () => void;
}
