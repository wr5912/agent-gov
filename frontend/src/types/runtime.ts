import type { components } from "./api";
import type { components as AgentScopeComponents } from "./agentscope";
import type { FeedbackEntities } from "./feedback";

type OpenApiAgentSummary = components["schemas"]["AgentSummaryResponse"];
type OpenApiAgentPresentation = components["schemas"]["AgentPresentationResponse"];
type OpenApiAgentLifecycleTransitionRequest = components["schemas"]["AgentLifecycleTransitionRequest"];
type OpenApiAgentDeleteResponse = components["schemas"]["AgentDeleteResponse"];
type OpenApiAgentChangeSetActionRequest = components["schemas"]["AgentChangeSetActionRequest"];
type OpenApiAgentChangeSetApproveRequest = components["schemas"]["AgentChangeSetApproveRequest"];
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
type OpenApiAgentRepositoryStatusResponse = components["schemas"]["AgentRepositoryStatusResponse"];
type OpenApiWorkspaceImportResponse = components["schemas"]["WorkspaceImportResponse"];
type OpenApiNativeAgentDataInput = components["schemas"]["NativeAgentDataInput"];
type OpenApiNativeAgentCandidateRequest = components["schemas"]["NativeAgentCandidateRequest"];
type OpenApiNativeAgentCandidateResponse = components["schemas"]["NativeAgentCandidateResponse"];
type OpenApiNativeAgentCandidateSourceResponse = components["schemas"]["NativeAgentCandidateSourceResponse"];
type OpenApiRuntimeNativeAgentSchemaResponse = components["schemas"]["RuntimeNativeAgentSchemaResponse"];
type OpenApiRuntimeWorkspaceStatusResponse = components["schemas"]["RuntimeWorkspaceStatusResponse"];
type OpenApiRuntimeWorkspaceMcpResponse = components["schemas"]["RuntimeWorkspaceMcpResponse"];
type OpenApiRuntimeWorkspaceSkillResponse = components["schemas"]["RuntimeWorkspaceSkillResponse"];
type OpenApiRuntimeCurrentVersionResponse = components["schemas"]["RuntimeCurrentVersionResponse"];
type OpenApiRuntimeHealth = components["schemas"]["RuntimeHealthResponse"];

export type RuntimeHealth = OpenApiRuntimeHealth;
/** 业务 Agent 治理对象；其 Runtime Agent ID 是不可变发布版本的绑定。 */
export type AgentSummary = OpenApiAgentSummary;
export type RuntimeCurrentVersion = OpenApiRuntimeCurrentVersionResponse;
export type AgentPresentation = OpenApiAgentPresentation;
export type AgentLifecycleTransitionRequest = OpenApiAgentLifecycleTransitionRequest;
export type AgentDeleteResponse = OpenApiAgentDeleteResponse;
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
export type AgentGitRef = OpenApiAgentGitRefResponse;
export type AgentGitFileEntry = OpenApiAgentGitFileEntryResponse;
export type AgentGitDiffEntry = OpenApiAgentGitDiffEntryResponse;
export type AgentGitDiff = Omit<OpenApiAgentGitDiffResponse, "from_version_id" | "to_version_id" | "added" | "modified" | "deleted" | "unchanged_count"> & {
  from_version_id: string;
  to_version_id: string;
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
export type AgentChangeSetCreateRequest = OpenApiAgentChangeSetCreateRequest;
export type AgentChangeSetActionRequest = OpenApiAgentChangeSetActionRequest;
export type AgentChangeSetApproveRequest = OpenApiAgentChangeSetApproveRequest;
export type AgentChangeSetPublishRequest = OpenApiAgentChangeSetPublishRequest;
export type WorkspaceImportResponse = OpenApiWorkspaceImportResponse;
export type NativeAgentDataInput = OpenApiNativeAgentDataInput;
export type NativeAgentCandidateRequest = OpenApiNativeAgentCandidateRequest;
export type NativeAgentCandidateResponse = OpenApiNativeAgentCandidateResponse;
export type NativeAgentCandidateSource = OpenApiNativeAgentCandidateSourceResponse;
export type RuntimeNativeAgentSchema = OpenApiRuntimeNativeAgentSchemaResponse;
export type RuntimeWorkspaceStatus = OpenApiRuntimeWorkspaceStatusResponse;
export type RuntimeWorkspaceMcp = OpenApiRuntimeWorkspaceMcpResponse;
export type RuntimeWorkspaceSkill = OpenApiRuntimeWorkspaceSkillResponse;

export interface AgentActivity {
  tool_names: string[];
  tool_calls: Record<string, unknown>[];
  tool_results: Record<string, unknown>[];
  skill_calls: Record<string, unknown>[];
}

export type AgentScopeSessionStatus = AgentScopeComponents["schemas"]["SessionStatus"];
export type AgentScopeSessionRecord = AgentScopeComponents["schemas"]["SessionRecord"];
export type AgentScopeSessionView = AgentScopeComponents["schemas"]["SessionView"];
export type GovernedRuntimeSessionView = AgentScopeSessionView & {
  /** AgentGov-owned active run fence; never sourced from AgentScope Session. */
  active_run_id?: string | null;
};
export type AgentScopeMessage = AgentScopeComponents["schemas"]["AgentScopeMsg"];
export type AgentScopeContentBlock = AgentScopeMessage["content"][number];
export type AgentScopeToolCallBlock = AgentScopeComponents["schemas"]["AgentScopeToolCallBlock"];
export type AgentScopeError = AgentScopeComponents["schemas"]["AgentScopeErrorInfo"];
export type AgentScopeAgentEvent = AgentScopeComponents["schemas"]["AgentScopeAgentEvent"];
export type AgentScopeReplyStartEvent = AgentScopeComponents["schemas"]["AgentScopeReplyStartEvent"];
export type AgentScopeReplyEndEvent = AgentScopeComponents["schemas"]["AgentScopeReplyEndEvent"];
export type AgentScopeTextBlockDeltaEvent = AgentScopeComponents["schemas"]["AgentScopeTextBlockDeltaEvent"];
export type AgentScopeRequireUserConfirmEvent = AgentScopeComponents["schemas"]["AgentScopeRequireUserConfirmEvent"];
export type AgentScopeRequireExternalExecutionEvent = AgentScopeComponents["schemas"]["AgentScopeRequireExternalExecutionEvent"];
export type AgentScopeMessagesResponse = AgentScopeComponents["schemas"]["ListMessagesResponse"];
export type AgentScopeStatusResponse = AgentScopeComponents["schemas"]["SessionStatusResponse"];
export type AgentScopeChatResponse = AgentScopeComponents["schemas"]["ChatTriggerResponse"];
export type AgentScopeChatInput = AgentScopeComponents["schemas"]["ChatRequest"]["input"];
export type AgentScopeUserMessage = AgentScopeComponents["schemas"]["Msg-Input"];
export type AgentScopeUserConfirmResult = AgentScopeComponents["schemas"]["UserConfirmResultEvent"];
export type AgentScopeExternalExecutionResult = AgentScopeComponents["schemas"]["ExternalExecutionResultEvent"];
export type AgentScopeToolResultState = AgentScopeComponents["schemas"]["ToolResultState"];
export type AgentScopeChatReceipt = AgentScopeChatResponse & {
  /** AgentGov response header projected into the native AgentScope receipt. */
  runId: string;
};
export type ChatRole = AgentScopeMessage["role"];
export type LangfuseTraceStatus = "available" | "not_recorded" | "history_unlinked";

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
  is_running: boolean;
  status: AgentScopeSessionStatus;
  active_run_id?: string | null;
}


export type RuntimeConfirmationScope = "once" | "run";
export type RuntimeUserConfirmAction = "allow_once" | "allow_for_run" | "deny";

export interface RuntimeUserConfirmRequest {
  requestId: string;
  replyId: string;
  /** Present when AgentScope projected a Team worker request onto the leader stream. */
  workerSessionId?: string;
  /** Exact AgentScope Runtime Agent owning workerSessionId; never inferred from the leader. */
  workerRuntimeAgentId?: string;
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
  /** Exact AgentScope Runtime Agent owning workerSessionId; never inferred from the leader. */
  workerRuntimeAgentId?: string;
  toolCalls: AgentScopeToolCallBlock[];
  status: "waiting" | "resolved" | "cancelled";
  resultState?: AgentScopeToolResultState | "runtime_interrupted";
  resolvedAt?: string;
}

export interface RuntimePendingAction {
  action_id: string;
  session_id: string;
  /** Runtime Agent that owns session_id; required to read canonical worker messages. */
  runtime_agent_id: string;
  run_id: string;
  reply_id: string;
  kind: "human" | "external";
  tool_call_id: string;
  tool_call_name: string;
  tool_call_state: "pending" | "asking" | "allowed" | "submitted" | "finished";
  tool_call_utf8_length: number;
  tool_call_sha256: string;
  status: "pending";
  created_at: string;
}

export interface AgentRunRecord {
  run_id: string;
  session_id: string;
  agent_id: string;
  agent_version_id: string;
  runtime_agent_id?: string;
  harness_digest?: string;
  status: "queued" | "running" | "waiting_human" | "waiting_external" | "finalizing" | "succeeded" | "failed" | "cancelled" | "interrupted";
  reply_ids?: string[];
  trace_id?: string | null;
  trace_url?: string | null;
  trace_status?: "pending" | "complete" | "incomplete";
  terminal_reason?: string | null;
  error?: Record<string, unknown> | null;
  entities?: FeedbackEntities;
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
  entities?: FeedbackEntities;
  runOutcome?: "succeeded" | "failed" | "cancelled" | "interrupted";
  partial?: boolean;
  /** Runtime/run 执行错误与原生回复正文分别展示；AgentGov failure type 不限于 AgentScope 枚举。 */
  executionError?: { message: string; type?: string };
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
