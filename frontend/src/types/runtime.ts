export interface RuntimeHealth {
  status: string;
  api_host?: string;
  api_port?: number;
  host_port?: number;
  workspace_dir?: string;
  data_dir?: string;
  claude_root?: string;
  claude_home?: string;
  claude_config_mode?: string;
  claude_config_dir?: string | null;
  claude_global_config_file?: string;
  setting_sources_effective?: string[] | null;
  model?: string | null;
  default_agent?: string | null;
  default_skills_mode?: "all" | "default" | "none";
  provider_api_url_configured?: boolean;
  provider_api_key_configured?: boolean;
  programmatic_agents?: boolean;
  agent_version_id?: string | null;
  docs?: Record<string, string | null>;
}

export interface AgentInfo {
  name: string;
  path: string;
  description?: string | null;
  model?: string | null;
  tools: string[];
  skills: string[];
}

export interface SkillInfo {
  name: string;
  path: string;
  description?: string | null;
}

export interface SessionInfo {
  session_id: string;
  sdk_session_id?: string | null;
  created_at: string;
  updated_at: string;
  title?: string | null;
  turns: number;
  metadata: Record<string, unknown>;
}

export interface ConfigMappingItem {
  scope: string;
  kind: string;
  container_path: string;
  host_mount?: string | null;
  exists: boolean;
  loaded_by_default: boolean;
  git_policy: string;
  notes?: string | null;
}

export interface ConfigMappingResponse {
  claude_config_mode: string;
  claude_root: string;
  claude_home: string;
  claude_global_config_file: string;
  claude_config_dir?: string | null;
  setting_sources_effective?: string[] | null;
  mappings: ConfigMappingItem[];
}

export interface ChatRequest {
  message: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  agent?: string;
  skills?: string[];
  skills_mode?: "all" | "default" | "none";
  allowed_tools?: string[];
  disallowed_tools?: string[];
  max_turns?: number;
  model?: string;
  permission_mode?: string;
  system_append?: string;
  metadata?: Record<string, unknown>;
}

export interface ChatResponse {
  run_id: string;
  session_id: string;
  sdk_session_id?: string | null;
  agent_version_id?: string | null;
  answer: string;
  messages: Record<string, unknown>[];
  agent_activity: AgentActivity;
  usage?: Record<string, unknown> | null;
  total_cost_usd?: number | null;
  stop_reason?: string | null;
  errors: string[];
}

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

export interface AgentVersionSummary {
  agent_version_id: string;
  parent_version_id?: string | null;
  created_at: string;
  reason: string;
  rollback_of_version_id?: string | null;
  source_proposal_ids?: string[];
  note?: string | null;
  agent_yaml_version?: string | null;
  snapshot_policy_version?: string;
  bundle_sha256?: string;
  bundle_path?: string;
  manifest_path?: string;
  file_count?: number;
  entry_count?: number;
  total_bytes?: number;
}

export interface AgentVersionManifest {
  agent_version_id: string;
  parent_version_id?: string | null;
  created_at: string;
  reason: string;
  rollback_of_version_id?: string | null;
  source_proposal_ids?: string[];
  note?: string | null;
  agent_yaml_version?: string | null;
  snapshot_policy_version?: string;
  included_roots?: Record<string, unknown>[];
  excluded_paths?: Record<string, unknown>[];
  skipped_paths?: Record<string, unknown>[];
  bundle_sha256?: string;
  file_count?: number;
  entry_count?: number;
  total_bytes?: number;
  files: Array<Record<string, unknown>>;
  related_data?: Record<string, unknown>;
}

export interface AgentVersionSnapshotRequest {
  reason?: string;
  source_proposal_ids?: string[];
  note?: string;
}

export interface AgentVersionRestoreRequest {
  note?: string;
}

export interface AgentVersionRestoreResponse {
  restored_from_version: AgentVersionSummary;
  pre_restore_version: AgentVersionSummary;
  current_version: AgentVersionSummary;
  requires_runtime_restart: boolean;
}

export interface AgentVersionDiff {
  from_version_id: string;
  to_version_id: string;
  added: Array<Record<string, unknown>>;
  modified: Array<Record<string, unknown>>;
  deleted: Array<Record<string, unknown>>;
  unchanged_count: number;
}
