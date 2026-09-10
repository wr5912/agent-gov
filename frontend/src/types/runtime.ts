import type { components } from "./api";

type OpenApiAgentInfo = components["schemas"]["AgentInfo"];
type OpenApiAgentSummary = components["schemas"]["AgentSummaryResponse"];
type OpenApiAgentPresentation = components["schemas"]["AgentPresentationResponse"];
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
type OpenApiAgentTestAssetSummaryResponse = components["schemas"]["AgentTestAssetSummaryResponse"];
type OpenApiAgentTestRunHistoryResponse = components["schemas"]["AgentTestRunHistoryResponse"];
type OpenApiAgentTestRunSummaryResponse = components["schemas"]["AgentTestRunSummaryResponse"];
type OpenApiAgentTestScheduleEventResponse = components["schemas"]["AgentTestScheduleEventResponse"];
type OpenApiAgentTestScheduleResponse = components["schemas"]["AgentTestScheduleResponse"];
type OpenApiAgentTestScheduleUpdateRequest = components["schemas"]["AgentTestScheduleUpdateRequest"];
type OpenApiAgentTestSuiteFileResponse = components["schemas"]["AgentTestSuiteFileResponse"];
type OpenApiAgentGitDiffEntryResponse = components["schemas"]["AgentGitDiffEntryResponse"];
type OpenApiAgentGitDiffResponse = components["schemas"]["AgentGitDiffResponse"];
type OpenApiAgentGitFileDiffResponse = components["schemas"]["AgentGitFileDiffResponse"];
type OpenApiAgentGitFileEntryResponse = components["schemas"]["AgentGitFileEntryResponse"];
type OpenApiAgentGitRefResponse = components["schemas"]["AgentGitRefResponse"];
type OpenApiAgentReleaseResponse = components["schemas"]["AgentReleaseResponse"];
type OpenApiAgentReleaseRollbackRequest = components["schemas"]["AgentReleaseRollbackRequest"];
type OpenApiAgentReleaseRestoreRequest = components["schemas"]["AgentReleaseRestoreRequest"];
type OpenApiAgentReleaseRestoreResponse = components["schemas"]["AgentReleaseRestoreResponse"];
type OpenApiAgentConfigFileResponse = components["schemas"]["AgentConfigFileResponse"];
type OpenApiAgentConfigFileUpdateRequest = components["schemas"]["AgentConfigFileUpdateRequest"];
type OpenApiAgentConfigFileUpdateResponse = components["schemas"]["AgentConfigFileUpdateResponse"];
type OpenApiAgentRepositoryDiscardChangesRequest = components["schemas"]["AgentRepositoryDiscardChangesRequest"];
type OpenApiAgentRepositorySnapshotRequest = components["schemas"]["AgentRepositorySnapshotRequest"];
type OpenApiAgentRepositoryStatusResponse = components["schemas"]["AgentRepositoryStatusResponse"];
type OpenApiWorkspaceImportResponse = components["schemas"]["WorkspaceImportResponse"];
type OpenApiWorkspaceRestoreRequest = components["schemas"]["WorkspaceRestoreRequest"];
type OpenApiWorkspaceRestoreResponse = components["schemas"]["WorkspaceRestoreResponse"];
type OpenApiConfigMappingItem = components["schemas"]["ConfigMappingItem"];
type OpenApiConfigMappingResponse = components["schemas"]["ConfigMappingResponse"];
type OpenApiRuntimeHealth = components["schemas"]["RuntimeHealthResponse"];
type OpenApiSkillInfo = components["schemas"]["SkillInfo"];

export type RuntimeHealth = OpenApiRuntimeHealth;
export type AgentInfo = OpenApiAgentInfo;
/** 业务 Agent（治理对象，/api/agent-registry），区别于运行内 Subagent（/api/agents）。 */
export type AgentSummary = OpenApiAgentSummary;

export interface RuntimeCurrentVersion {
  governance_agent_id: string;
  agent_version_id: string;
  harness_digest: string;
  runtime_agent_id?: string | null;
  provisioned: boolean;
}
export type AgentPresentation = OpenApiAgentPresentation;
export type AgentLifecycleTransitionRequest = OpenApiAgentLifecycleTransitionRequest;
export type AgentDeleteResponse = OpenApiAgentDeleteResponse;
export type SkillInfo = OpenApiSkillInfo;
export type ConfigMappingItem = OpenApiConfigMappingItem;
export type ConfigMappingResponse = OpenApiConfigMappingResponse;
export type AgentTestRunCreateRequest = OpenApiAgentTestRunCreateRequest;
export type AgentTestRun = OpenApiAgentTestRunResponse;
export type AgentTestSuite = OpenApiAgentTestSuiteSummary;
export type AgentTestAssetSummary = OpenApiAgentTestAssetSummaryResponse;
export type AgentTestRunHistory = OpenApiAgentTestRunHistoryResponse;
export type AgentTestRunSummary = OpenApiAgentTestRunSummaryResponse;
export type AgentTestScheduleEvent = OpenApiAgentTestScheduleEventResponse;
export type AgentTestSchedule = OpenApiAgentTestScheduleResponse;
export type AgentTestScheduleUpdateRequest = OpenApiAgentTestScheduleUpdateRequest;
export type AgentTestSuiteFile = OpenApiAgentTestSuiteFileResponse;
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
export type AgentConfigFileResponse = OpenApiAgentConfigFileResponse;
export type AgentConfigFileUpdateRequest = OpenApiAgentConfigFileUpdateRequest;
export type AgentConfigFileUpdateResponse = OpenApiAgentConfigFileUpdateResponse;
export type WorkspaceImportResponse = OpenApiWorkspaceImportResponse;
export type WorkspaceRestoreRequest = OpenApiWorkspaceRestoreRequest;
export type WorkspaceRestoreResponse = OpenApiWorkspaceRestoreResponse;

export interface AgentActivity {
  tool_names: string[];
  tool_calls: Record<string, unknown>[];
  tool_results: Record<string, unknown>[];
  skill_calls: Record<string, unknown>[];
}

export type ChatRole = "user" | "assistant" | "system";
export type LangfuseTraceStatus = "available" | "not_recorded" | "history_unlinked";

export type AgentScopeSessionStatus =
  | "running"
  | "idle"
  | "awaiting_permission"
  | "awaiting_external_result";

export interface AgentScopeSessionRecord {
  id?: string;
  session_id?: string;
  agent_id?: string | null;
  name?: string | null;
  created_at?: string;
  updated_at?: string;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AgentScopeSessionView {
  session: AgentScopeSessionRecord;
  is_running: boolean;
  status: AgentScopeSessionStatus;
  team?: unknown;
}

/** Playground-side projection of an AgentScope SessionView. */
export interface SessionInfo {
  session_id: string;
  /** AgentScope Runtime Agent ID pinned when the Session was created. */
  agent_id: string | null;
  /** Stable AgentGov governance object owning this Runtime Session. */
  business_agent_id?: string | null;
  created_at: string;
  updated_at: string;
  title?: string;
  turns: number;
  metadata: Record<string, unknown>;
  is_running: boolean;
  status: AgentScopeSessionStatus;
  active_run_id?: string | null;
}

export interface AgentScopeContentBlock {
  type: string;
  id?: string;
  text?: string;
  thinking?: string;
  hint?: string | AgentScopeContentBlock[];
  name?: string;
  input?: string;
  output?: string | AgentScopeContentBlock[];
  state?: string;
  suggested_rules?: unknown[];
  [key: string]: unknown;
}

export interface AgentScopeToolCallBlock extends AgentScopeContentBlock {
  type: "tool_call";
  id: string;
  name: string;
  input: string;
}

export interface AgentScopeMessage {
  name: string;
  role: ChatRole;
  content: AgentScopeContentBlock[];
  id: string;
  metadata: Record<string, unknown>;
  created_at: string;
  usage?: Record<string, unknown> | null;
  finished_at?: string | null;
  finished_reason?: "completed" | "interrupted" | "exceed_max_iters" | "error" | null;
  structured_output?: Record<string, unknown> | null;
  error?: AgentScopeError | null;
}

export interface AgentScopeError {
  type: string;
  message: string;
  [key: string]: unknown;
}

export interface AgentScopeAgentEvent {
  id: string;
  created_at: string;
  metadata: Record<string, unknown>;
  type: string;
  session_id?: string;
  reply_id?: string;
  block_id?: string;
  tool_call_id?: string;
  tool_call_name?: string;
  delta?: string;
  finished_reason?: "completed" | "interrupted" | "exceed_max_iters" | "error" | string;
  error?: AgentScopeError | null;
  tool_calls?: AgentScopeToolCallBlock[];
  name?: string;
  value?: unknown;
  [key: string]: unknown;
}

export interface AgentScopeMessagesResponse {
  messages: AgentScopeMessage[];
  is_running: boolean;
  has_more: boolean;
}

export interface AgentScopeStatusResponse {
  session_id: string;
  status: AgentScopeSessionStatus;
}

export interface AgentScopeChatResponse {
  status: "started";
  session_id: string;
}

export interface AgentScopeChatReceipt extends AgentScopeChatResponse {
  runId: string;
}

export interface AgentScopeUserMessage {
  name: "user";
  role: "user";
  content: Array<{ type: "text"; text: string }>;
}

export interface AgentScopeUserConfirmResult {
  type: "USER_CONFIRM_RESULT";
  reply_id: string;
  confirm_results: Array<{
    confirmed: boolean;
    tool_call: AgentScopeToolCallBlock;
  }>;
}

export type AgentScopeToolResultState = "success" | "error" | "interrupted" | "denied";

export interface AgentScopeExternalExecutionResult {
  type: "EXTERNAL_EXECUTION_RESULT";
  reply_id: string;
  execution_results: Array<{
    type: "tool_result";
    id: string;
    name: string;
    output: string;
    state: AgentScopeToolResultState;
  }>;
}

export type AgentScopeChatInput = AgentScopeUserMessage | AgentScopeUserConfirmResult | AgentScopeExternalExecutionResult | null;

export type RuntimeConfirmationScope = "once" | "run";
export type RuntimeUserConfirmAction = "allow_once" | "allow_for_run" | "deny";

export interface RuntimeUserConfirmRequest {
  requestId: string;
  replyId: string;
  /** Present when AgentScope projected a Team worker request onto the leader stream. */
  workerSessionId?: string;
  toolCalls: AgentScopeToolCallBlock[];
  status: "waiting" | "resolved" | "cancelled";
  decision?: RuntimeUserConfirmAction | "runtime_interrupted";
  resolvedAt?: string;
}

export interface RuntimeExternalExecutionRequest {
  requestId: string;
  replyId: string;
  /** Present when AgentScope projected a Team worker request onto the leader stream. */
  workerSessionId?: string;
  toolCalls: AgentScopeToolCallBlock[];
  status: "waiting" | "resolved" | "cancelled";
  resultState?: AgentScopeToolResultState | "runtime_interrupted";
  resolvedAt?: string;
}

export interface RuntimePendingAction {
  action_id: string;
  session_id: string;
  run_id: string;
  reply_id: string;
  kind: "human" | "external";
  tool_call: Record<string, unknown>;
  status: "pending";
  created_at: string;
}

export interface AgentRunRecord {
  run_id: string;
  session_id: string;
  agent_id: string;
  agent_version_id: string;
  runtime_agent_id?: string;
  client_operation_id?: string | null;
  harness_digest?: string;
  status: "queued" | "running" | "waiting_human" | "waiting_external" | "finalizing" | "succeeded" | "failed" | "cancelled" | "interrupted";
  reply_ids?: string[];
  trace_id?: string | null;
  trace_url?: string | null;
  trace_status?: "pending" | "complete" | "incomplete";
  terminal_reason?: string | null;
  error?: Record<string, unknown> | null;
  alert_id?: string | null;
  case_id?: string | null;
  metadata?: Record<string, unknown>;
  created_at?: string;
  started_at?: string | null;
  updated_at?: string;
  completed_at?: string | null;
}

export interface AgentRunTrace {
  run_id: string;
  trace_id?: string | null;
  trace_url?: string | null;
  trace_status: "pending" | "complete" | "incomplete";
}

/** UI projection only; payload retains the complete native AgentScope event. */
export interface AgentTraceEvent {
  event_id: string;
  kind: string;
  message_index: number;
  run_id: string;
  scope: "main";
  sequence: number;
  source_event: string;
  payload?: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  runId?: string;
  sessionId?: string;
  agentVersionId?: string;
  langfuseTraceId?: string;
  langfuseTraceUrl?: string;
  langfuseTraceStatus?: LangfuseTraceStatus;
  alertId?: string;
  caseId?: string;
  runOutcome?: "succeeded" | "failed" | "cancelled" | "interrupted";
  partial?: boolean;
  controlError?: string;
  agentActivity?: AgentActivity;
  userConfirmRequests?: RuntimeUserConfirmRequest[];
  externalExecutionRequests?: RuntimeExternalExecutionRequest[];
  traceState?: "live" | "calibrating" | "ready" | "unavailable" | "error";
  traceError?: string;
  /** Native AgentScope events observed for this run. */
  events?: StreamLogEvent[];
}

export interface StreamLogEvent {
  id: string;
  /** Semantic event kind, for example thinking, tool_use, hook, task, or result. */
  event: string;
  text?: string;
  data?: unknown;
  createdAt: string;
  sequence?: number;
}

export interface RuntimeClientConfig {
  apiBase: string;
  apiKey: string;
}
