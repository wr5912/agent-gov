import type { components } from "./api";

type OpenApiAgentInfo = components["schemas"]["AgentInfo"];
type OpenApiAgentSummary = components["schemas"]["AgentSummaryResponse"];
type OpenApiAgentLifecycleTransitionRequest = components["schemas"]["AgentLifecycleTransitionRequest"];
type OpenApiAgentDeleteResponse = components["schemas"]["AgentDeleteResponse"];
type OpenApiAgentChangeSetActionRequest = components["schemas"]["AgentChangeSetActionRequest"];
type OpenApiAgentChangeSetCreateRequest = components["schemas"]["AgentChangeSetCreateRequest"];
type OpenApiAgentChangeSetEventResponse = components["schemas"]["AgentChangeSetEventResponse"];
type OpenApiAgentChangeSetPublishRequest = components["schemas"]["AgentChangeSetPublishRequest"];
type OpenApiAgentChangeSetResponse = components["schemas"]["AgentChangeSetResponse"];
type OpenApiAgentTestRunCreateRequest = components["schemas"]["AgentTestRunCreateRequest"];
type OpenApiAgentTestRunResponse = components["schemas"]["AgentTestRunResponse"];
type OpenApiAgentTestSuiteSummary = components["schemas"]["AgentTestSuiteSummary"];
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
type OpenApiAgentConfigFileResponse = components["schemas"]["AgentConfigFileResponse"];
type OpenApiAgentConfigFileUpdateRequest = components["schemas"]["AgentConfigFileUpdateRequest"];
type OpenApiAgentConfigFileUpdateResponse = components["schemas"]["AgentConfigFileUpdateResponse"];
type OpenApiAgentRepositoryDiscardChangesRequest = components["schemas"]["AgentRepositoryDiscardChangesRequest"];
type OpenApiAgentRepositorySnapshotRequest = components["schemas"]["AgentRepositorySnapshotRequest"];
type OpenApiAgentRepositoryStatusResponse = components["schemas"]["AgentRepositoryStatusResponse"];
type OpenApiWorkspaceImportResponse = components["schemas"]["WorkspaceImportResponse"];
type OpenApiWorkspaceRestoreRequest = components["schemas"]["WorkspaceRestoreRequest"];
type OpenApiWorkspaceRestoreResponse = components["schemas"]["WorkspaceRestoreResponse"];
type OpenApiChatRequest = components["schemas"]["ChatRequest"];
type OpenApiClaudeUserInputDecisionRequest = components["schemas"]["ClaudeUserInputDecisionRequest"];
type OpenApiClaudeUserInputDecisionResponse = components["schemas"]["ClaudeUserInputDecisionResponse"];
type OpenApiClaudeUserInputRequestResponse = components["schemas"]["ClaudeUserInputRequestResponse"];
type OpenApiConfigMappingItem = components["schemas"]["ConfigMappingItem"];
type OpenApiConfigMappingResponse = components["schemas"]["ConfigMappingResponse"];
type OpenApiRuntimeHealth = components["schemas"]["RuntimeHealthResponse"];
type OpenApiSessionInfo = components["schemas"]["SessionInfo"];
type OpenApiSkillInfo = components["schemas"]["SkillInfo"];
type OpenApiConversationItem = components["schemas"]["ConversationItem"];
type OpenApiConversationItemList = components["schemas"]["ConversationItemList"];

export type RuntimeHealth = OpenApiRuntimeHealth;
export type AgentInfo = OpenApiAgentInfo;
/** 业务 Agent（治理对象，/api/agent-registry），区别于运行内 Subagent（/api/agents）。 */
export type AgentSummary = OpenApiAgentSummary;
export type OpenAICompatAgentConfig = components["schemas"]["OpenAICompatAgentConfig"];
export type AgentLifecycleTransitionRequest = OpenApiAgentLifecycleTransitionRequest;
export type AgentDeleteResponse = OpenApiAgentDeleteResponse;
export type SkillInfo = OpenApiSkillInfo;
export type SessionInfo = OpenApiSessionInfo;
export type ConversationItem = OpenApiConversationItem;
export type ConversationItemList = OpenApiConversationItemList;
export type ConfigMappingItem = OpenApiConfigMappingItem;
export type ConfigMappingResponse = OpenApiConfigMappingResponse;
export type AgentTestRunCreateRequest = OpenApiAgentTestRunCreateRequest;
export type AgentTestRun = OpenApiAgentTestRunResponse;
export type AgentTestSuite = OpenApiAgentTestSuiteSummary;
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
export type AgentChangeSetPublishRequest = OpenApiAgentChangeSetPublishRequest;
export type AgentReleaseRollbackRequest = OpenApiAgentReleaseRollbackRequest;
export type AgentReleaseRestoreRequest = OpenApiAgentReleaseRestoreRequest;
export type AgentRunRecord = OpenApiAgentRunResponse;
export type AgentConfigFileResponse = OpenApiAgentConfigFileResponse;
export type AgentConfigFileUpdateRequest = OpenApiAgentConfigFileUpdateRequest;
export type AgentConfigFileUpdateResponse = OpenApiAgentConfigFileUpdateResponse;
export type WorkspaceImportResponse = OpenApiWorkspaceImportResponse;
export type WorkspaceRestoreRequest = OpenApiWorkspaceRestoreRequest;
export type WorkspaceRestoreResponse = OpenApiWorkspaceRestoreResponse;

export type ChatRequest = OpenApiChatRequest;

export interface AgentActivity {
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
  langfuseTraceId?: string;
  langfuseTraceUrl?: string;
  alertId?: string;
  caseId?: string;
  agentActivity?: AgentActivity;
  userInputRequests?: ClaudeUserInputRequest[];
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

export type ClaudeUserInputRequestType = OpenApiClaudeUserInputRequestResponse["request_type"];
export type ClaudeUserInputStatus = OpenApiClaudeUserInputRequestResponse["status"];
export type ClaudeUserInputDecisionAction = OpenApiClaudeUserInputDecisionRequest["action"];

export type ClaudeUserInputRequest = Omit<OpenApiClaudeUserInputRequestResponse, "input" | "context" | "risk" | "decision_payload"> & {
  decision_token?: string;
  input: Record<string, unknown>;
  context: Record<string, unknown>;
  risk: Record<string, unknown>;
  decision_payload?: Record<string, unknown>;
};

export type ClaudeUserInputDecisionPayload = OpenApiClaudeUserInputDecisionRequest;

export type ClaudeUserInputDecisionResponse = OpenApiClaudeUserInputDecisionResponse;

export interface RuntimeClientConfig {
  apiBase: string;
  apiKey: string;
}
