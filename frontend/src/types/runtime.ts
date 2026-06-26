import type { components } from "./api";

type OpenApiAgentInfo = components["schemas"]["AgentInfo"];
type OpenApiAgentSummary = components["schemas"]["AgentSummaryResponse"];
type OpenApiAgentCreateRequest = components["schemas"]["AgentCreateRequest"];
type OpenApiBusinessAgentTemplatesResponse = components["schemas"]["BusinessAgentTemplatesResponse"];
type OpenApiAgentLifecycleTransitionRequest = components["schemas"]["AgentLifecycleTransitionRequest"];
type OpenApiAgentDeleteResponse = components["schemas"]["AgentDeleteResponse"];
type OpenApiAgentChangeSetActionRequest = components["schemas"]["AgentChangeSetActionRequest"];
type OpenApiAgentChangeSetCreateRequest = components["schemas"]["AgentChangeSetCreateRequest"];
type OpenApiAgentChangeSetEventResponse = components["schemas"]["AgentChangeSetEventResponse"];
type OpenApiAgentChangeSetPublishRequest = components["schemas"]["AgentChangeSetPublishRequest"];
type OpenApiAgentChangeSetRegressionRunRequest = components["schemas"]["AgentChangeSetRegressionRunRequest"];
type OpenApiAgentChangeSetResponse = components["schemas"]["AgentChangeSetResponse"];
type OpenApiAgentGitDiffEntryResponse = components["schemas"]["AgentGitDiffEntryResponse"];
type OpenApiAgentGitDiffResponse = components["schemas"]["AgentGitDiffResponse"];
type OpenApiAgentGitFileDiffResponse = components["schemas"]["AgentGitFileDiffResponse"];
type OpenApiAgentGitFileEntryResponse = components["schemas"]["AgentGitFileEntryResponse"];
type OpenApiAgentGitRefResponse = components["schemas"]["AgentGitRefResponse"];
type OpenApiAgentReleaseResponse = components["schemas"]["AgentReleaseResponse"];
type OpenApiAgentReleaseRollbackRequest = components["schemas"]["AgentReleaseRollbackRequest"];
type OpenApiAgentReleaseRestoreRequest = components["schemas"]["AgentReleaseRestoreRequest"];
type OpenApiAgentReleaseRestoreResponse = components["schemas"]["AgentReleaseRestoreResponse"];
type OpenApiAgentRunResponse = components["schemas"]["AgentRunResponse"];
type OpenApiAgentRepositoryDiscardChangesRequest = components["schemas"]["AgentRepositoryDiscardChangesRequest"];
type OpenApiAgentRepositorySnapshotRequest = components["schemas"]["AgentRepositorySnapshotRequest"];
type OpenApiAgentRepositoryStatusResponse = components["schemas"]["AgentRepositoryStatusResponse"];
type OpenApiAgentVersionDiffEntryResponse = components["schemas"]["AgentVersionDiffEntryResponse"];
type OpenApiAgentVersionDiffResponse = components["schemas"]["AgentVersionDiffResponse"];
type OpenApiAgentVersionFileEntryResponse = components["schemas"]["AgentVersionFileEntryResponse"];
type OpenApiAgentVersionSummaryResponse = components["schemas"]["AgentVersionSummaryResponse"];
type OpenApiChatRequest = components["schemas"]["ChatRequest"];
type OpenApiConfigMappingItem = components["schemas"]["ConfigMappingItem"];
type OpenApiConfigMappingResponse = components["schemas"]["ConfigMappingResponse"];
type OpenApiEvalRunResponse = components["schemas"]["EvalRunResponse"];
type OpenApiRuntimeHealth = components["schemas"]["RuntimeHealthResponse"];
type OpenApiSessionInfo = components["schemas"]["SessionInfo"];
type OpenApiSkillInfo = components["schemas"]["SkillInfo"];

export type RuntimeHealth = OpenApiRuntimeHealth;
export type AgentInfo = OpenApiAgentInfo;
/** 业务 Agent（治理对象，/api/agent-registry），区别于运行内 Subagent（/api/agents）。 */
export type AgentSummary = OpenApiAgentSummary;
export type AgentCreateRequest = OpenApiAgentCreateRequest;
export type BusinessAgentTemplatesResponse = OpenApiBusinessAgentTemplatesResponse;
export type AgentLifecycleTransitionRequest = OpenApiAgentLifecycleTransitionRequest;
export type AgentDeleteResponse = OpenApiAgentDeleteResponse;
export type SkillInfo = OpenApiSkillInfo;
export type SessionInfo = OpenApiSessionInfo;
export type ConfigMappingItem = OpenApiConfigMappingItem;
export type ConfigMappingResponse = OpenApiConfigMappingResponse;
export type EvalRunResponse = OpenApiEvalRunResponse;
export type AgentRepositoryStatus = OpenApiAgentRepositoryStatusResponse;
export type AgentRepositoryDiscardChangesRequest = OpenApiAgentRepositoryDiscardChangesRequest;
export type AgentRepositorySnapshotRequest = OpenApiAgentRepositorySnapshotRequest;
export type AgentGitRef = OpenApiAgentGitRefResponse;
export type AgentGitFileEntry = OpenApiAgentGitFileEntryResponse;
export type AgentGitDiffEntry = OpenApiAgentGitDiffEntryResponse;
export type AgentGitDiff = Omit<OpenApiAgentGitDiffResponse, "added" | "modified" | "deleted" | "unchanged_count"> & {
  added: AgentGitFileEntry[];
  modified: AgentGitDiffEntry[];
  deleted: AgentGitFileEntry[];
  unchanged_count: number;
};
export type AgentGitFileDiff = Omit<OpenApiAgentGitFileDiffResponse, "status" | "before" | "after"> & {
  from_version_id: string;
  to_version_id: string;
  path: string;
  archive_path: string;
  status: "added" | "modified" | "deleted" | "unchanged" | "missing" | "binary_or_too_large" | string;
  before?: AgentGitFileEntry | null;
  after?: AgentGitFileEntry | null;
  unified_diff: string;
  is_text: boolean;
  truncated: boolean;
  reason?: string | null;
};
export type AgentChangeSet = OpenApiAgentChangeSetResponse;
export type AgentChangeSetEvent = OpenApiAgentChangeSetEventResponse;
export type AgentRelease = OpenApiAgentReleaseResponse;
export type AgentReleaseRestoreResponse = OpenApiAgentReleaseRestoreResponse;
export type AgentChangeSetCreateRequest = OpenApiAgentChangeSetCreateRequest;
export type AgentChangeSetActionRequest = OpenApiAgentChangeSetActionRequest;
export type AgentChangeSetRegressionRunRequest = OpenApiAgentChangeSetRegressionRunRequest;
export type AgentChangeSetPublishRequest = OpenApiAgentChangeSetPublishRequest;
export type AgentReleaseRollbackRequest = OpenApiAgentReleaseRollbackRequest;
export type AgentReleaseRestoreRequest = OpenApiAgentReleaseRestoreRequest;
export type AgentRunRecord = OpenApiAgentRunResponse;

export type ChatRequest = OpenApiChatRequest;

export interface AgentActivity {
  requested_skills: string[];
  skills_mode?: string;
  allowed_tools: string[];
  disallowed_tools: string[];
  tool_names: string[];
  tool_calls: Record<string, unknown>[];
  tool_results: Record<string, unknown>[];
  skill_calls: Record<string, unknown>[];
}

export type ChatRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  runId?: string;
  sessionId?: string;
  sdkSessionId?: string;
  agentVersionId?: string;
  alertId?: string;
  caseId?: string;
  agentActivity?: AgentActivity;
  /** 当前 assistant 回复捕获到的完整 SSE 时间线。 */
  events?: StreamLogEvent[];
}

export interface StreamLogEvent {
  id: string;
  /** 原始 SSE 事件名，例如 session、message、result、error 或 done。 */
  event: string;
  text?: string;
  data?: unknown;
  createdAt: string;
}

export interface StreamEnvelope {
  event: string;
  data: unknown;
}

export interface RuntimeClientConfig {
  apiBase: string;
  apiKey: string;
}

export type AgentVersionSummary = OpenApiAgentVersionSummaryResponse;
export type AgentVersionFileEntry = OpenApiAgentVersionFileEntryResponse;
export type AgentVersionDiffEntry = OpenApiAgentVersionDiffEntryResponse;
export type AgentVersionDiff = Omit<OpenApiAgentVersionDiffResponse, "added" | "modified" | "deleted" | "unchanged_count"> & {
  added: AgentVersionFileEntry[];
  modified: AgentVersionDiffEntry[];
  deleted: AgentVersionFileEntry[];
  unchanged_count: number;
};
