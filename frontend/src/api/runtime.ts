import { ApiRequestError, makeUrl, requestBlob, requestJson, runtimeHeaders } from "./request";
import { GOVERNANCE_AGENT_TIMEOUT_MS } from "./timeouts";
export { connectAgentScopeSessionStream } from "./agentScopeStream";
export type {
  AgentScopeStreamConnection,
  AgentScopeStreamHandlers,
  AgentScopeStreamOptions,
  SubagentHitlProjection,
  SubagentHitlResolution,
} from "./agentScopeStream";
export { defaultRuntimeConfig, shouldMigrateStoredApiBase } from "./request";
export * from "./agentTesting";
export * from "./feedback";
import type {
  AgentPresentation,
  AgentSummary,
  AgentDeleteResponse,
  AgentChangeSet,
  AgentChangeSetApproveRequest,
  AgentChangeSetActionRequest,
  AgentChangeSetCreateRequest,
  AgentChangeSetEvent,
  AgentChangeSetPublishRequest,
  AgentGitDiff,
  AgentGitFileDiff,
  AgentGitRef,
  AgentRelease,
  AgentRepositoryStatus,
  AgentScopeChatInput,
  AgentScopeChatReceipt,
  AgentScopeChatResponse,
  AgentScopeMessagesResponse,
  AgentScopeStatusResponse,
  GovernedRuntimeSessionView,
  NativeAgentCandidateRequest,
  NativeAgentCandidateResponse,
  NativeAgentCandidateSource,
  RuntimeClientConfig,
  RuntimeHealth,
  RuntimeNativeAgentSchema,
  RuntimeWorkspaceMcp,
  RuntimeWorkspaceSkill,
  RuntimeWorkspaceStatus,
  SessionInfo,
  WorkspaceImportResponse,
} from "../types/runtime";

export function getHealth(config: RuntimeClientConfig) {
  return requestJson<RuntimeHealth>(config, "/health");
}

export async function createRuntimeSession(
  config: RuntimeClientConfig,
  agentId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<string> {
  const result = await requestJson<{ session_id: string }>(config, "/api/runtime/sessions/", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      ...runtimeHeaders(config),
    },
    body: JSON.stringify({ agent_id: agentId }),
    signal,
  });
  if (!result.session_id) throw new Error("Runtime 创建会话后未返回 session_id。");
  return result.session_id;
}

export async function getSessions(
  config: RuntimeClientConfig,
  governanceAgentId: string,
  signal?: AbortSignal,
): Promise<SessionInfo[]> {
  const query = new URLSearchParams({ governance_agent_id: governanceAgentId });
  const list = await requestJson<{ sessions: GovernedRuntimeSessionView[]; total: number }>(
    config,
    `/api/runtime/sessions/?${query.toString()}`,
    { headers: runtimeHeaders(config), signal },
  );
  return list.sessions.map((session) => sessionViewToSessionInfo(session, governanceAgentId));
}

export async function getRuntimeSessionMessages(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentScopeMessagesResponse> {
  const messages: AgentScopeMessagesResponse["messages"] = [];
  const seenCursors = new Set<string>();
  let before: string | undefined;
  let isRunning = false;

  while (true) {
    const query = new URLSearchParams({ agent_id: agentId, limit: "200" });
    if (before) query.set("before", before);
    const page = await requestJson<AgentScopeMessagesResponse>(
      config,
      `/api/runtime/sessions/${encodeURIComponent(sessionId)}/messages?${query.toString()}`,
      { headers: runtimeHeaders(config), signal },
    );
    const pageMessages = Array.isArray(page.messages) ? page.messages : [];
    messages.unshift(...pageMessages);
    isRunning = page.is_running;
    if (!page.has_more) return { messages, is_running: isRunning, has_more: false };

    const cursor = pageMessages[0]?.id;
    if (!cursor || seenCursors.has(cursor)) {
      throw new Error("Runtime messages 分页返回了无效游标。");
    }
    seenCursors.add(cursor);
    before = cursor;
  }
}

export function getRuntimeSessionStatus(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ agent_id: agentId });
  return requestJson<AgentScopeStatusResponse>(
    config,
    `/api/runtime/sessions/${encodeURIComponent(sessionId)}/status?${query.toString()}`,
    { headers: runtimeHeaders(config), signal },
  );
}

export async function startRuntimeChat(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  input: AgentScopeChatInput,
  context: {
    alertId?: string;
    caseId?: string;
    confirmationScope?: "once" | "run";
    expectedRunId?: string;
    clientOperationId: string;
  },
  signal?: AbortSignal,
): Promise<AgentScopeChatReceipt> {
  const response = await fetchRuntime(
    config,
    "/api/runtime/chat/",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_id: agentId,
        session_id: sessionId,
        client_operation_id: context.clientOperationId,
        input,
        confirmation_scope: context.confirmationScope ?? "once",
        expected_run_id: context.expectedRunId,
        alert_id: context.alertId,
        case_id: context.caseId,
        metadata: {
          client: "agent-gov-ui",
        },
      }),
      signal,
    },
  );
  const data = await decodeRuntimeJson<AgentScopeChatResponse>(response);
  const runId = response.headers.get("X-AgentGov-Run-Id")?.trim() || "";
  const responseSessionId = response.headers.get("X-AgentGov-Session-Id")?.trim() || "";
  if (!runId || !responseSessionId) {
    throw new ApiRequestError("decode", "Runtime chat 响应缺少 AgentGov 运行标识头。");
  }
  if (responseSessionId !== sessionId || data.session_id !== sessionId) {
    throw new ApiRequestError("decode", "Runtime chat 响应的 session_id 与当前会话不一致。");
  }
  return { ...data, runId };
}

export async function interruptRuntimeSession(
  config: RuntimeClientConfig,
  agentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ agent_id: agentId });
  return requestJson<{ session_id: string }>(
    config,
    `/api/runtime/sessions/${encodeURIComponent(sessionId)}/interrupt?${query.toString()}`,
    {
      method: "POST",
      headers: runtimeHeaders(config),
      body: null,
      signal,
      timeoutMs: 15_000,
    },
  );
}

function runtimeSessionResourcePath(
  sessionId: string,
  runtimeAgentId: string,
  suffix = "",
) {
  const query = new URLSearchParams({ agent_id: runtimeAgentId });
  return `/api/runtime/sessions/${encodeURIComponent(sessionId)}${suffix}?${query.toString()}`;
}

export async function renameRuntimeSession(
  config: RuntimeClientConfig,
  runtimeAgentId: string,
  sessionId: string,
  name: string,
  signal?: AbortSignal,
) {
  await fetchRuntime(
    config,
    runtimeSessionResourcePath(sessionId, runtimeAgentId),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
      signal,
    },
  );
}

export async function deleteRuntimeSession(
  config: RuntimeClientConfig,
  runtimeAgentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  await fetchRuntime(
    config,
    runtimeSessionResourcePath(sessionId, runtimeAgentId),
    { method: "DELETE", signal },
  );
}

export function getRuntimeWorkspaceStatus(
  config: RuntimeClientConfig,
  runtimeAgentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  return requestJson<RuntimeWorkspaceStatus>(
    config,
    runtimeSessionResourcePath(sessionId, runtimeAgentId, "/workspace/status"),
    { signal },
  );
}

export function getRuntimeWorkspaceMcps(
  config: RuntimeClientConfig,
  runtimeAgentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  return requestJson<RuntimeWorkspaceMcp[]>(
    config,
    runtimeSessionResourcePath(sessionId, runtimeAgentId, "/workspace/mcp"),
    // AgentScope 投影该资源时可能建立真实 MCP 连接；一次用户刷新只能触发一次上游操作。
    { signal, retry: false },
  );
}

export function getRuntimeWorkspaceSkills(
  config: RuntimeClientConfig,
  runtimeAgentId: string,
  sessionId: string,
  signal?: AbortSignal,
) {
  return requestJson<RuntimeWorkspaceSkill[]>(
    config,
    runtimeSessionResourcePath(sessionId, runtimeAgentId, "/workspace/skills"),
    { signal },
  );
}

function sessionViewToSessionInfo(view: GovernedRuntimeSessionView, businessAgentId?: string): SessionInfo {
  const session = view.session;
  const sessionId = session.id?.trim();
  const createdAt = session.created_at?.trim();
  const updatedAt = session.updated_at?.trim();
  if (!sessionId || !createdAt || !updatedAt) {
    throw new Error("Runtime SessionRecord 缺少 id、created_at 或 updated_at。");
  }
  return {
    session_id: sessionId,
    agent_id: session.agent_id,
    business_agent_id: businessAgentId || null,
    created_at: createdAt,
    updated_at: updatedAt,
    title: session.config.name,
    is_running: view.status === "running",
    status: view.status,
    active_run_id: view.active_run_id?.trim() || null,
  };
}

async function fetchRuntime(config: RuntimeClientConfig, path: string, init: RequestInit) {
  const controller = new AbortController();
  const callerSignal = init.signal;
  let timedOut = false;
  const timeoutId = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort("timeout");
  }, 30_000);
  const abortFromCaller = () => controller.abort(callerSignal?.reason || "caller_aborted");
  if (callerSignal?.aborted) controller.abort(callerSignal.reason || "caller_aborted");
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    const response = await fetch(makeUrl(config, path), {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...runtimeHeaders(config),
        ...(init.headers || {}),
      },
    });
    if (!response.ok) {
      const errorBody: { detail?: string; message?: string; error_code?: string } = await decodeRuntimeJson<{
        detail?: string;
        message?: string;
        error_code?: string;
      }>(response)
        .catch(() => ({}));
      const errorCode = typeof errorBody.error_code === "string" ? errorBody.error_code : undefined;
      const detail = errorBody.detail || errorBody.message || `${response.status} ${response.statusText}`;
      throw new ApiRequestError(
        "http",
        errorCode ? `[${errorCode}] ${detail}` : detail,
        { status: response.status, errorCode },
      );
    }
    return response;
  } catch (error) {
    if (error instanceof ApiRequestError) throw error;
    if (timedOut) throw new ApiRequestError("timeout", "Runtime 请求超时。");
    if (callerSignal?.aborted) throw new ApiRequestError("aborted", "Runtime 请求已取消。");
    throw new ApiRequestError("network", error instanceof Error ? error.message : String(error));
  } finally {
    globalThis.clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

async function decodeRuntimeJson<T>(response: Response): Promise<T> {
  try {
    return await response.json() as T;
  } catch {
    throw new ApiRequestError("decode", "Runtime 返回了无效 JSON。");
  }
}

// 业务 Agent（治理对象，/api/agent-registry），用于顶栏全局 Agent 切换器与 scoping。
export function listBusinessAgents(config: RuntimeClientConfig) {
  return requestJson<AgentSummary[]>(config, "/api/agent-registry");
}

export function getBusinessAgentPresentation(
  config: RuntimeClientConfig,
  agentId: string,
  signal?: AbortSignal,
) {
  return requestJson<AgentPresentation>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/presentation`,
    { signal },
  );
}

export interface WorkspaceImportPayload {
  package: File;
  name?: string;
  expectedCurrentCommitSha?: string;
  reason?: string;
}

export interface WorkspaceExportFile {
  blob: Blob;
  filename: string;
  commitSha: string;
  packageSha256: string;
  treeSha256: string;
}

function responseFilename(headers: Headers): string | undefined {
  const disposition = headers.get("content-disposition") || "";
  const utf8Match = disposition.match(/filename\*=utf-8''([^;]+)/i);
  if (utf8Match?.[1]) return decodeURIComponent(utf8Match[1].trim());
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1]?.trim();
}

export async function exportBusinessAgentWorkspace(
  config: RuntimeClientConfig,
  agentId: string,
): Promise<WorkspaceExportFile> {
  const { blob, headers } = await requestBlob(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/workspace/export`,
    { method: "POST", timeoutMs: 120_000 },
  );
  return {
    blob,
    filename: responseFilename(headers) || `${agentId}-workspace.tar.gz`,
    commitSha: headers.get("x-agent-commit-sha") || "",
    packageSha256: headers.get("x-workspace-package-sha256") || "",
    treeSha256: headers.get("x-workspace-tree-sha256") || "",
  };
}

export function importBusinessAgentWorkspace(
  config: RuntimeClientConfig,
  agentId: string,
  payload: WorkspaceImportPayload,
) {
  const body = new FormData();
  body.append("package", payload.package);
  if (payload.name) body.append("name", payload.name);
  if (payload.expectedCurrentCommitSha) body.append("expected_current_commit_sha", payload.expectedCurrentCommitSha);
  if (payload.reason) body.append("reason", payload.reason);
  return requestJson<WorkspaceImportResponse>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/workspace/import`,
    { method: "POST", body, timeoutMs: 120_000 },
  );
}

export function getRuntimeNativeAgentSchema(
  config: RuntimeClientConfig,
  signal?: AbortSignal,
) {
  return requestJson<RuntimeNativeAgentSchema>(config, "/api/runtime/agent-schema", { signal });
}

export function createNativeAgentCandidate(
  config: RuntimeClientConfig,
  agentId: string,
  payload: NativeAgentCandidateRequest,
  signal?: AbortSignal,
) {
  return requestJson<NativeAgentCandidateResponse>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/native-candidate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
      timeoutMs: 120_000,
    },
  );
}

export function getNativeAgentCandidateSource(
  config: RuntimeClientConfig,
  agentId: string,
  signal?: AbortSignal,
) {
  return requestJson<NativeAgentCandidateSource>(
    config,
    `/api/agent-registry/${encodeURIComponent(agentId)}/native-candidate-source`,
    { signal },
  );
}

export function setBusinessAgentLifecycle(config: RuntimeClientConfig, agentId: string, status: string) {
  return requestJson<AgentSummary>(config, `/api/agent-registry/${encodeURIComponent(agentId)}/lifecycle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

export function deleteBusinessAgent(config: RuntimeClientConfig, agentId: string) {
  return requestJson<AgentDeleteResponse>(config, `/api/agent-registry/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
}

export const runtimeApi = {
  health: getHealth,
  sessions: getSessions,
};

export function getAgentRepositoryStatus(config: RuntimeClientConfig) {
  return requestJson<AgentRepositoryStatus>(config, "/api/agent-repository");
}

export function getCurrentAgentRef(config: RuntimeClientConfig, agentId?: string) {
  const query = agentId ? `?${new URLSearchParams({ agent_id: agentId }).toString()}` : "";
  return requestJson<AgentGitRef>(config, `/api/agent-repository/current${query}`);
}

export function getAgentChangeSets(config: RuntimeClientConfig) {
  return requestJson<AgentChangeSet[]>(config, "/api/agent-change-sets");
}

export function createAgentChangeSet(config: RuntimeClientConfig, payload: AgentChangeSetCreateRequest) {
  return requestJson<AgentChangeSet>(config, "/api/agent-change-sets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getAgentChangeSet(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentChangeSet>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}`);
}

export function getAgentChangeSetEvents(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentChangeSetEvent[]>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/events`);
}

export function diffAgentChangeSet(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentGitDiff>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/diff`);
}

export function diffAgentChangeSetFile(config: RuntimeClientConfig, changeSetId: string, path: string) {
  const params = new URLSearchParams({ path });
  return requestJson<AgentGitFileDiff>(config, `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/file-diff?${params.toString()}`);
}

export function approveAgentChangeSet(config: RuntimeClientConfig, changeSetId: string, payload: AgentChangeSetApproveRequest) {
  return requestJson<AgentChangeSet>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function rejectAgentChangeSet(config: RuntimeClientConfig, changeSetId: string, payload: AgentChangeSetActionRequest = { operator: "ui" }) {
  return requestJson<AgentChangeSet>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/reject`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function retryAgentChangeSetWorktreeCleanup(config: RuntimeClientConfig, changeSetId: string) {
  return requestJson<AgentChangeSet>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/worktree-cleanup/retry`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator: "ui" }),
    },
  );
}

export function publishAgentChangeSet(config: RuntimeClientConfig, changeSetId: string, payload: AgentChangeSetPublishRequest) {
  return requestJson<AgentRelease>(
    config,
    `/api/agent-change-sets/${encodeURIComponent(changeSetId)}/publish`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

export function getAgentReleases(config: RuntimeClientConfig) {
  return requestJson<AgentRelease[]>(config, "/api/agent-releases");
}
