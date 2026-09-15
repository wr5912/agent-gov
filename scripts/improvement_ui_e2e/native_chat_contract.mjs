/** 原生 chat 的纯边界检查；只返回身份元数据，不复制输入或回复正文。 */
export class NativeChatContractError extends Error {
  constructor(code) {
    super(code);
    this.name = "NativeChatContractError";
    this.code = code;
  }
}

function check(condition, code) {
  if (!condition) throw new NativeChatContractError(code);
}

function nonempty(value) {
  return typeof value === "string" && value.trim().length > 0;
}

export function nativeChatRequestIdentity(body, confirmationScope) {
  check(body && typeof body === "object" && !Array.isArray(body)
    && JSON.stringify(Object.keys(body).sort()) === JSON.stringify(["agent_id", "input", "session_id"])
    && nonempty(body.agent_id) && nonempty(body.session_id), "NATIVE_CHAT_BODY_INVALID");
  const type = body.input?.type;
  check(confirmationScope == null || (confirmationScope === "run" && type === "USER_CONFIRM_RESULT"),
    "NATIVE_CONFIRMATION_SCOPE_INVALID");
  const operationKind = type === "USER_CONFIRM_RESULT" ? "user_confirmation"
    : type === "EXTERNAL_EXECUTION_RESULT" ? "external_execution" : "initial";
  const items = Array.isArray(body.input) ? body.input : [body.input];
  const ids = items.map((item) => item?.id);
  return {
    agentId: body.agent_id,
    requestedSessionId: body.session_id,
    operationKind,
    inputIds: ids.length > 0 && ids.every(nonempty) ? ids : null,
  };
}

export function nativeChatReceiptIdentity(identity, payload, runId, rootSessionId) {
  check(nonempty(runId) && nonempty(rootSessionId) && rootSessionId === identity.requestedSessionId,
    "NATIVE_CHAT_ROOT_RECEIPT_INVALID");
  check(payload?.status === "started" && nonempty(payload.session_id), "NATIVE_CHAT_RESPONSE_INVALID");
  return { agentId: identity.agentId, requestedSessionId: identity.requestedSessionId,
    operationKind: identity.operationKind, inputIds: identity.inputIds,
    runId, sessionId: rootSessionId, rootSessionId,
    nativeSessionId: payload.session_id, started: true };
}

export function nativeInputLookupPath(identity) {
  check(nonempty(identity.agentId) && nonempty(identity.requestedSessionId)
    && new Set(["initial", "user_confirmation", "external_execution"]).has(identity.operationKind)
    && Array.isArray(identity.inputIds) && identity.inputIds.length > 0 && identity.inputIds.every(nonempty),
  "NATIVE_EXPLICIT_INPUT_ID_REQUIRED");
  const query = new URLSearchParams({ agent_id: identity.agentId,
    session_id: identity.requestedSessionId, operation_kind: identity.operationKind });
  for (const id of identity.inputIds) query.append("input_id", id);
  return `/api/agent-runs/by-input-identity?${query}`;
}
