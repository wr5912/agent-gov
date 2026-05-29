import type { components } from "./api";

type OpenApiAgentInfo = components["schemas"]["AgentInfo"];
type OpenApiAgentVersionDiffEntryResponse = components["schemas"]["AgentVersionDiffEntryResponse"];
type OpenApiAgentVersionDiffResponse = components["schemas"]["AgentVersionDiffResponse"];
type OpenApiAgentVersionFileDiffResponse = components["schemas"]["AgentVersionFileDiffResponse"];
type OpenApiAgentVersionFileEntryResponse = components["schemas"]["AgentVersionFileEntryResponse"];
type OpenApiAgentVersionManifestResponse = components["schemas"]["AgentVersionManifestResponse"];
type OpenApiAgentVersionRestoreRequest = components["schemas"]["AgentVersionRestoreRequest"];
type OpenApiAgentVersionRestoreResponse = components["schemas"]["AgentVersionRestoreResponse"];
type OpenApiAgentVersionSnapshotRequest = components["schemas"]["AgentVersionSnapshotRequest"];
type OpenApiAgentVersionSummaryResponse = components["schemas"]["AgentVersionSummaryResponse"];
type OpenApiChatRequest = components["schemas"]["ChatRequest"];
type OpenApiConfigMappingItem = components["schemas"]["ConfigMappingItem"];
type OpenApiConfigMappingResponse = components["schemas"]["ConfigMappingResponse"];
type OpenApiRuntimeHealth = components["schemas"]["RuntimeHealthResponse"];
type OpenApiSessionInfo = components["schemas"]["SessionInfo"];
type OpenApiSkillInfo = components["schemas"]["SkillInfo"];

export type RuntimeHealth = OpenApiRuntimeHealth;
export type AgentInfo = OpenApiAgentInfo;
export type SkillInfo = OpenApiSkillInfo;
export type SessionInfo = OpenApiSessionInfo;
export type ConfigMappingItem = OpenApiConfigMappingItem;
export type ConfigMappingResponse = OpenApiConfigMappingResponse;

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
export type AgentVersionManifest = Omit<OpenApiAgentVersionManifestResponse, "files"> & {
  files?: AgentVersionFileEntry[];
};
export type AgentVersionSnapshotRequest = OpenApiAgentVersionSnapshotRequest;
export type AgentVersionRestoreRequest = OpenApiAgentVersionRestoreRequest;
export type AgentVersionRestoreResponse = Omit<
  OpenApiAgentVersionRestoreResponse,
  "restored_from_version" | "pre_restore_version" | "current_version"
> & {
  restored_from_version: AgentVersionSummary;
  pre_restore_version: AgentVersionSummary;
  current_version: AgentVersionSummary;
};
export type AgentVersionDiff = Omit<OpenApiAgentVersionDiffResponse, "added" | "modified" | "deleted" | "unchanged_count"> & {
  added: AgentVersionFileEntry[];
  modified: AgentVersionDiffEntry[];
  deleted: AgentVersionFileEntry[];
  unchanged_count: number;
};
export type AgentVersionFileDiff = Omit<OpenApiAgentVersionFileDiffResponse, "status" | "before" | "after"> & {
  from_version_id: string;
  to_version_id: string;
  path: string;
  archive_path: string;
  status: "added" | "modified" | "deleted" | "unchanged" | "missing" | "binary_or_too_large" | string;
  before?: AgentVersionFileEntry | null;
  after?: AgentVersionFileEntry | null;
  unified_diff: string;
  is_text: boolean;
  truncated: boolean;
  reason?: string | null;
};
