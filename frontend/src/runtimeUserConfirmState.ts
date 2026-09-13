import type {
  AgentScopeAgentEvent,
  AgentScopeToolCallBlock,
  AgentScopeUserConfirmResult,
  ChatMessage,
  RuntimeConfirmationScope,
  RuntimeUserConfirmAction,
  RuntimeUserConfirmRequest,
} from "./types/runtime";
import { isRecord } from "./utils/records";

export interface RuntimeUserConfirmSubmission {
  input: AgentScopeUserConfirmResult;
  confirmationScope: RuntimeConfirmationScope;
}

export interface RuntimeRunPermissionScope {
  toolName: string;
  ruleContent: string;
}

export function runtimeRunPermissionScopes(
  request: RuntimeUserConfirmRequest,
): RuntimeRunPermissionScope[] | undefined {
  const scopes = new Map<string, RuntimeRunPermissionScope>();
  for (const toolCall of request.toolCalls) {
    if (!Array.isArray(toolCall.suggested_rules) || !toolCall.suggested_rules.length) return undefined;
    for (const candidate of toolCall.suggested_rules) {
      if (!isRecord(candidate)) return undefined;
      const toolName = candidate.tool_name;
      const ruleContent = candidate.rule_content;
      const source = candidate.source;
      if (
        toolName !== toolCall.name
        || candidate.behavior !== "allow"
        || source !== "workspace_policy.ask_tools"
        || typeof ruleContent !== "string"
        || !isBoundedRunPathRule(toolName, ruleContent)
      ) return undefined;
      const normalized = ruleContent.trim();
      scopes.set(`${toolName}\0${normalized}`, { toolName, ruleContent: normalized });
    }
  }
  return scopes.size ? [...scopes.values()] : undefined;
}

const RUN_SCOPED_PATH_TOOLS = new Set(["Read", "Write", "Edit"]);

function isBoundedRunPathRule(toolName: string, ruleContent: string) {
  if (!RUN_SCOPED_PATH_TOOLS.has(toolName)) return false;
  const pattern = ruleContent.trim();
  if (!pattern || pattern !== ruleContent || pattern.length > 1024 || pattern.includes("\0")) return false;
  if (/[?\[\]\\]/.test(pattern)) return false;
  let fixedPrefix = pattern;
  if (pattern.includes("*")) {
    if (!pattern.endsWith("/**") || pattern.slice(0, -3).includes("*")) return false;
    fixedPrefix = pattern.slice(0, -3);
  }
  if (["", ".", "./", "/", "~"].includes(fixedPrefix) || fixedPrefix.includes("//")) return false;
  const parts = fixedPrefix.replace(/^\.\//, "").split("/").filter(Boolean);
  return parts.length > 0 && parts.every((part) => ![".", "..", "~"].includes(part));
}

export function buildUserConfirmSubmission(
  request: RuntimeUserConfirmRequest,
  action: RuntimeUserConfirmAction,
): RuntimeUserConfirmSubmission {
  if (action === "allow_for_run" && !runtimeRunPermissionScopes(request)) {
    throw new Error("Runtime 未提供安全且有边界的建议规则，不能授权整个 run");
  }
  return {
    input: {
      type: "USER_CONFIRM_RESULT",
      reply_id: request.replyId,
      confirm_results: request.toolCalls.map((toolCall) => ({
        confirmed: action !== "deny",
        tool_call: toolCall,
      })),
    },
    confirmationScope: action === "allow_for_run" ? "run" : "once",
  };
}

export function userConfirmRequestsFromEvent(
  event: AgentScopeAgentEvent,
  workerSessionId?: string,
): RuntimeUserConfirmRequest[] {
  if (event.type !== "REQUIRE_USER_CONFIRM" || !event.reply_id || !Array.isArray(event.tool_calls)) return [];
  const toolCalls = event.tool_calls.map(asToolCall).filter((value): value is AgentScopeToolCallBlock => Boolean(value));
  if (!toolCalls.length || toolCalls.length !== event.tool_calls.length) return [];
  return [{
    requestId: event.id,
    replyId: event.reply_id,
    workerSessionId,
    toolCalls,
    status: "waiting",
  }];
}

export function clearProjectedUserConfirmRequest(
  current: RuntimeUserConfirmRequest[] | undefined,
  workerSessionId: string,
  replyId: string,
) {
  return (current || []).filter((request) => !(
    request.workerSessionId === workerSessionId && request.replyId === replyId
  ));
}

export function mergeUserConfirmRequests(
  current: RuntimeUserConfirmRequest[] | undefined,
  incoming: RuntimeUserConfirmRequest[],
) {
  const byId = new Map((current || []).map((request) => [request.requestId, request]));
  for (const request of incoming) {
    const existing = byId.get(request.requestId);
    byId.set(request.requestId, existing?.status === "resolved" ? existing : { ...existing, ...request });
  }
  return [...byId.values()];
}

export function patchUserConfirmRequest(
  current: RuntimeUserConfirmRequest[] | undefined,
  requestId: string,
  patch: Partial<RuntimeUserConfirmRequest>,
) {
  return (current || []).map((request) => (
    request.requestId === requestId ? { ...request, ...patch } : request
  ));
}

export function cancelWaitingUserConfirmRequests(
  messages: ChatMessage[],
  assistantMessageId: string | undefined,
  resolvedAt: string,
) {
  if (!assistantMessageId) return messages;
  return messages.map((message) => {
    if (message.id !== assistantMessageId || !message.userConfirmRequests?.length) return message;
    return {
      ...message,
      userConfirmRequests: message.userConfirmRequests.map((request) => (
        request.status === "waiting"
          ? { ...request, status: "cancelled" as const, decision: "runtime_interrupted" as const, resolvedAt }
          : request
      )),
    };
  });
}

function asToolCall(value: unknown): AgentScopeToolCallBlock | undefined {
  if (
    !isRecord(value)
    || value.type !== "tool_call"
    || typeof value.id !== "string"
    || typeof value.name !== "string"
    || typeof value.input !== "string"
  ) return undefined;
  return value as unknown as AgentScopeToolCallBlock;
}

function isOverbroadRunRule(ruleContent: string) {
  const normalized = ruleContent.trim();
  if (!normalized) return true;
  // 仅由路径分隔符和 glob 元字符组成的规则等价于“任意输入”，不能升级为
  // 整个 run 的权限；用户仍可选择仅放行当前 ToolCall。
  return !normalized.replace(/[./*?[\]!\\\s]/g, "");
}
