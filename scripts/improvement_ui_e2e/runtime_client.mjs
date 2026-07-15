export class RuntimeApiError extends Error {
  constructor(method, path, status, body) {
    super(`${method} ${path} failed: ${status} ${body}`);
    this.name = "RuntimeApiError";
    this.method = method;
    this.path = path;
    this.status = status;
    this.body = body;
  }
}

function requiredHttpBase(name) {
  const value = String(process.env[name] || "").trim().replace(/\/$/, "");
  if (!value) throw new Error(`${name} is required and must point to a running real container`);
  const url = new URL(value);
  if (!new Set(["http:", "https:"]).has(url.protocol)) throw new Error(`${name} must use http or https`);
  if (url.hostname === "runtime.test") throw new Error(`${name} must not point to the mock runtime host`);
  return value;
}

export function runtimeConfigFromEnv() {
  const uiBase = requiredHttpBase("RUNTIME_UI_BASE");
  const apiBase = requiredHttpBase("RUNTIME_API_BASE");
  const screenshotDir = String(process.env.VERIFY_SCREENSHOT_DIR || "/tmp/agentgov-ui-feedback-smoke").trim();
  if (!screenshotDir) throw new Error("VERIFY_SCREENSHOT_DIR must not be empty");
  const actionTimeoutMs = Number(process.env.REAL_ACTION_TIMEOUT_MS || 300000);
  if (!Number.isFinite(actionTimeoutMs) || actionTimeoutMs < 1000) {
    throw new Error("REAL_ACTION_TIMEOUT_MS must be a finite number of at least 1000 milliseconds");
  }
  return {
    uiBase,
    apiBase,
    apiKey: String(process.env.RUNTIME_API_KEY || ""),
    screenshotDir,
    actionTimeoutMs,
  };
}

function headers(config, extra = {}) {
  return {
    Accept: "application/json",
    ...(config.apiKey ? { Authorization: `Bearer ${config.apiKey}` } : {}),
    ...extra,
  };
}

export async function apiRequest(config, path, init = {}) {
  const method = init.method || "GET";
  const response = await fetch(`${config.apiBase}${path}`, {
    ...init,
    headers: headers(config, init.headers || {}),
    signal: init.signal || AbortSignal.timeout(config.actionTimeoutMs),
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  return { method, path, response, status: response.status, text, payload };
}

export async function apiJson(config, path, init = {}) {
  const result = await apiRequest(config, path, init);
  if (!result.response.ok) throw new RuntimeApiError(result.method, path, result.status, result.text);
  return result.payload;
}

export function jsonInit(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

const IMPROVEMENT_FIXTURES = {
  "evidence-conflict": {
    title: "多源证据冲突降级治理",
    summary: "多个权威来源对同一 IOC 给出冲突结论时，必须显式降级置信度并等待复核。",
    feedbackSummary: "多源证据冲突时仍输出高置信度结论",
    rawText: "MCP 与本地知识库对同一 IOC 的结论相反，当前回答却静默选边并标记为高置信度。",
    scenario: "evidence-conflict",
    problem: "同一 IOC 的多个权威来源冲突时，Agent 未列出冲突并降级置信度",
    possibleReason: "根 CLAUDE.md 只要求追溯来源，尚未定义多源证据冲突时的决策规则",
    possibleObject: "目标业务 Agent 根 CLAUDE.md 的默认分析流程（不涉及 skill、settings 或 MCP 配置）",
    suggestion: "仅修改目标业务 Agent 根 CLAUDE.md：多源证据冲突时必须并列来源、采集时间和冲突值，未消解前降为低置信度且不得升级或执行高风险动作；不得静默选边，不涉及 skill、settings 或 MCP 配置",
    userQuote: "两个权威来源结论相反，不能静默选边后仍给出高置信度。",
  },
  "pagination-integrity": {
    title: "分页证据完整性治理",
    summary: "数据查询仍有下一页或被截断时，不得把局部结果表述为全量结论。",
    feedbackSummary: "查询仍有 next_cursor 时错误宣称未发现更多风险",
    rawText: "MCP 返回 next_cursor，但回答忽略后续页并断言已检查全部告警且未发现更多风险。",
    scenario: "pagination-incomplete",
    problem: "分页查询未完成时，Agent 把局部证据错误表述为全量或无风险结论",
    possibleReason: "根 CLAUDE.md 尚未定义 next_cursor、has_more 和 truncated 的完整性检查规则",
    possibleObject: "目标业务 Agent 根 CLAUDE.md 的默认工具结果完整性规则（不涉及 skill、settings 或 MCP 配置）",
    suggestion: "仅修改目标业务 Agent 根 CLAUDE.md：给出全量统计或未发现风险等否定结论前必须检查 limit 是否命中以及 next_cursor、has_more、truncated、partial；未耗尽分页时继续查询或明确标记为局部样本，不涉及 skill、settings 或 MCP 配置",
    userQuote: "返回里还有 next_cursor，不能说已经检查全部告警。",
    additionalFeedbacks: [{
      summary: "工具结果标记 truncated 时错误输出全量统计",
      rawText: "工具明确返回 truncated=true，回答仍把当前计数写成全部资产的最终统计。",
      scenario: "truncated-result",
    }],
  },
};

export async function seedBaseImprovement(config, fixtureName = "evidence-conflict") {
  await apiJson(config, "/health");
  const agents = await apiJson(config, "/api/agent-registry");
  const agent = agents.find((item) => item.status === "active") || agents[0];
  if (!agent?.agent_id) throw new Error("real runtime has no registered business Agent");
  const fixture = IMPROVEMENT_FIXTURES[fixtureName];
  if (!fixture) throw new Error(`unknown real-container improvement fixture: ${fixtureName}`);
  const stamp = `ui-e2e-${Date.now().toString(36)}`;
  const item = await apiJson(config, "/api/improvements", jsonInit("POST", {
    agent_id: agent.agent_id,
    title: `${stamp} ${fixture.title}`,
    summary: fixture.summary,
    source_feedback_refs: [],
    auto_merge: false,
  }));
  const feedbackInputs = [
    { summary: fixture.feedbackSummary, rawText: fixture.rawText, scenario: fixture.scenario },
    ...(fixture.additionalFeedbacks || []),
  ];
  const feedbacks = [];
  for (const [index, input] of feedbackInputs.entries()) {
    const suffix = index ? `-${index + 1}` : "";
    feedbacks.push(await apiJson(config, `/api/improvements/${item.improvement_id}/feedbacks`, jsonInit("POST", {
      summary: input.summary,
      source: "playground_run",
      raw_text: input.rawText,
      run_id: `${stamp}-run${suffix}`,
      session_id: `${stamp}-session${suffix}`,
      agent_version_id: `${stamp}-baseline`,
      scenario: input.scenario,
      task_id: `${stamp}-task${suffix}`,
      alert_id: `${stamp}-alert${suffix}`,
      case_id: `${stamp}-case${suffix}`,
    })));
  }
  await apiJson(config, `/api/improvements/${item.improvement_id}/normalized-feedback`, jsonInit("PUT", {
    problem: fixture.problem,
    possible_reason: fixture.possibleReason,
    possible_object: fixture.possibleObject,
    impact: "中",
    suggestion: fixture.suggestion,
    user_quote: fixture.userQuote,
  }));
  await apiJson(config, `/api/improvements/${item.improvement_id}/normalized-feedback/confirm`, jsonInit("POST", {}));
  return { agent, feedback: feedbacks[0], feedbacks, item, stamp };
}

export async function assertHostileAdoptionRejected(config, improvementId) {
  const result = await apiRequest(
    config,
    `/api/improvements/${improvementId}/test-dataset/adopt`,
    jsonInit("POST", { dataset_id: "client-owned-is-forbidden" }),
  );
  if (result.status !== 422) {
    throw new Error(`typed TestDataset adoption must reject client-owned fields with 422; got ${result.status}`);
  }
  return { status: result.status, path: result.path };
}
