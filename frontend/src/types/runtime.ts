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

export interface FeedbackCreateRequest {
  run_id: string;
  session_id: string;
  alert_id?: string;
  case_id?: string;
  feedback_source: "explicit" | "analyst_action" | "case_outcome" | "tool_quality";
  analyst_action?: "accepted" | "partially_accepted" | "rejected" | "modified_conclusion" | "requested_more_evidence";
  final_verdict?: string;
  final_severity?: string;
  labels: string[];
  affected_tools?: string[];
  auto_captured?: boolean;
  confidence?: "low" | "medium" | "high";
  requires_review?: boolean;
  comment?: string;
}

export interface FeedbackResponse {
  feedback: Record<string, unknown>;
  attribution: Record<string, unknown>;
  proposal?: Record<string, unknown> | null;
}

export interface FeedbackEventIngestRequest {
  event_id: string;
  source_system: string;
  event_type:
    | "case.verdict_changed"
    | "case.severity_changed"
    | "recommendation.accepted"
    | "recommendation.rejected"
    | "recommendation.modified"
    | "evidence.added"
    | "tool.manual_query_after_agent";
  timestamp: string;
  run_id?: string;
  session_id?: string;
  alert_id?: string;
  case_id?: string;
  actor_id?: string;
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
  entities?: Record<string, string[]>;
  auto_captured?: boolean;
  confidence?: "low" | "medium" | "high";
  requires_review?: boolean;
  comment?: string;
  metadata?: Record<string, unknown>;
}

export interface FeedbackEventIngestResponse {
  event: Record<string, unknown>;
  correlation_status: "matched" | "pending_correlation" | "duplicate" | "stored_only";
  matched_run_id?: string | null;
  attribution?: Record<string, unknown> | null;
  proposal?: Record<string, unknown> | null;
}

export interface FeedbackQueryResponse {
  feedback: Record<string, unknown>[];
  events: Record<string, unknown>[];
  attributions: Record<string, unknown>[];
  pending_correlations: Record<string, unknown>[];
}

export interface OptimizationProposal {
  proposal_id?: string;
  status?: string;
  title?: string;
  recommendation?: string;
  attribution_type?: string;
  run_id?: string;
  session_id?: string;
  alert_id?: string | null;
  case_id?: string | null;
  target_path?: string;
  created_at?: string;
  [key: string]: unknown;
}
