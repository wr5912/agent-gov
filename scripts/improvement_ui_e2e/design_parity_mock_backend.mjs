const ts = "2026-06-18T00:00:00Z";
export const DESIGN_PARITY_TS = ts;

const AGENTS = [
  { agent_id: "soc-ops", name: "安全运营助手", category: "", workspace_dir: "/w/soc", created_at: ts, status: "active" },
  { agent_id: "shop-bot", name: "电商客服", category: "", workspace_dir: "/w/shop", created_at: ts, status: "active" },
];
const IMPROVEMENTS = [
  { improvement_id: "imp-demo01", agent_id: "soc-ops", title: "时间窗口误判治理", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "triage", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo02", agent_id: "soc-ops", title: "时间窗口误判治理 · 归因", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "attribution", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo03", agent_id: "soc-ops", title: "时间窗口误判治理 · 优化", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "optimization", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo04", agent_id: "soc-ops", title: "时间窗口误判治理 · 测试", summary: "事件时间不一致", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "regression", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo05", agent_id: "soc-ops", title: "时间窗口误判重复反馈", summary: "事件时间不一致的重复反馈", source_feedback_refs: ["fb-2", "fb-3"], improvement_stage: "triage", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo06", agent_id: "soc-ops", title: "时间窗口误判治理 · 待执行", summary: "已有优化方案，等待执行优化", source_feedback_refs: ["fb-1", "fb-2"], improvement_stage: "optimization", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo07", agent_id: "soc-ops", title: "时间窗口误判治理 · 执行中", summary: "执行产物已生成", source_feedback_refs: ["fb-1"], improvement_stage: "execution", improvement_status: "active", created_at: ts, updated_at: ts },
  { improvement_id: "imp-demo08", agent_id: "soc-ops", title: "时间窗口误判治理 · 已发布", summary: "发布门禁已完成", source_feedback_refs: ["fb-1"], improvement_stage: "release", improvement_status: "done", created_at: ts, updated_at: ts },
];
const internalTraceUrl = (traceId) => `http://langfuse-web:3000/project/agent-gov/traces/${traceId}`;
const REGRESSION_CASES = [
  {
    prompt: "数据转换前原始数据:\n{\"danger_tid\":\"14516\",\"event_time\":\"2026-06-18T00:00:00Z\"}\n\n转换后 OCSF 数据:\n{\"class_uid\":1007,\"api\":\"CreateRemoteThread\"}\n\n请审查转换后数据是否符合 OCSF 标准定义。".repeat(10),
    expected_behavior: "先核验时间一致性，不直接升级。",
    checkpoints: ["是否核验时间", "是否避免误升级"],
  },
  {
    prompt: "sec-ops-data 返回疑似模拟数据，请判断当前告警是否可以作为真实攻击升级处置。",
    expected_behavior: "提示核验数据源，避免把模拟数据当成真实证据。",
    checkpoints: ["是否标记数据源风险", "是否要求补充证据"],
  },
  {
    prompt: "证据不足但用户要求给出确定结论，请处理当前告警调查任务。",
    expected_behavior: "说明证据不足并给出下一步核验动作。",
    checkpoints: ["是否拒绝编造结论", "是否给出核验路径"],
  },
];
const TYPED_TEST_DATASET = {
  dataset_id: "tds-demo04",
  agent_id: "soc-ops",
  owner_kind: "business_agent",
  owner_id: "soc-ops",
  source_improvement_id: "imp-demo04",
  name: "测试数据集：时间窗口误判治理",
  description: "覆盖时间窗口与数据源核验",
  scope: "feedback-derived",
  revision: 1,
  lifecycle_state: "active",
  quality_tags: ["feedback-derived"],
  provenance: {
    regression_assessment_id: "reg-1",
    regression_assessment_updated_at: ts,
    normalized_feedback_id: "nf-1",
    normalized_feedback_updated_at: ts,
    attribution_id: "attr-1",
    attribution_updated_at: ts,
    optimization_plan_id: "opt-1",
    optimization_plan_updated_at: ts,
    execution_id: "exec-1",
    execution_updated_at: ts,
    source_feedback_ids: ["fb-1", "fb-2"],
    baseline_agent_version_id: "v1.2.0",
    candidate_agent_version_id: "candidate-b",
  },
  cases: REGRESSION_CASES.map((item, index) => ({ case_id: `tdc-demo-${index + 1}`, position: index + 1, ...item })),
  created_at: ts,
  updated_at: ts,
};
const STALE_TEST_DATASET = {
  ...TYPED_TEST_DATASET,
  dataset_id: "tds-demo04-stale",
  name: "测试数据集：时间窗口误判治理（旧链路）",
  provenance: {
    ...TYPED_TEST_DATASET.provenance,
    regression_assessment_updated_at: "2026-06-17T00:00:00Z",
    candidate_agent_version_id: "candidate-old",
  },
  created_at: "2026-06-17T00:00:00Z",
  updated_at: "2026-06-17T00:00:00Z",
};

function mockConversationItems(sessionId) {
  const densityMatch = sessionId.match(/^density-check-(\d+)$/);
  const turnCount = densityMatch ? Number(densityMatch[1]) : 36;
  const repeatCount = turnCount <= 4 ? 18 : 2;
  return Array.from({ length: turnCount }, (_, index) => {
    const n = index + 1;
    return [
      {
        id: `msg_${index * 2}`,
        object: "conversation.item",
        type: "message",
        role: "user",
        content: [{ type: "text", text: `请用一句话说明你的治理职责，序号 ${n}。` }],
        parent_tool_use_id: null,
      },
      {
        id: `msg_${index * 2 + 1}`,
        object: "conversation.item",
        type: "message",
        role: "assistant",
        content: [
          { type: "text", text: `我是 AgentGov 治理测试助手。第 ${n} 段回复用于构造可滚动的 Playground 长会话，验证刻度密度。`.repeat(repeatCount) },
          { type: "tool_use", id: `tool-${n}`, name: "Read", input: { file_path: "CLAUDE.md" } },
        ],
        parent_tool_use_id: null,
      },
    ];
  }).flat();
}

function advanceMockImprovement(path, stage) {
  const improvementId = decodeURIComponent(path.split("/")[3] || "");
  const item = IMPROVEMENTS.find((row) => row.improvement_id === improvementId);
  if (item) item.improvement_stage = stage;
}

const UNHANDLED = Symbol("unhandled");

function basePayload(path) {
  if (path === "/health") return { status: "ok", model: "parity-mock" };
  if (path === "/v1/conversations") return {
    object: "list",
    data: ["mock-session", "density-check-4"].map((sessionId) => ({
      id: `conv_${sessionId}`,
      object: "conversation",
      created_at: Date.parse(ts) / 1000,
      title: "Playground 历史验证",
      metadata: {},
      agentgov: { agent_id: "main-agent", sdk_session_id: sessionId, updated_at: Date.parse(ts) / 1000, turns: sessionId === "mock-session" ? 36 : 4 },
    })),
  };
  const conversationItems = path.match(/^\/v1\/conversations\/conv_(.+)\/items$/);
  if (conversationItems) {
    const items = mockConversationItems(decodeURIComponent(conversationItems[1]));
    return { object: "list", data: items, first_id: items[0]?.id || null, last_id: items.at(-1)?.id || null, has_more: false };
  }
  if (path === "/api/agent-registry") return AGENTS;
  if (path === "/api/agents" || path === "/api/skills" || path === "/api/sessions" || path === "/api/agent-releases") return [];
  if (
    path === "/api/agent-runs"
    || path === "/api/feedback-sources"
    || path === "/api/feedback-signals"
    || path === "/api/soc-events"
    || path === "/api/pending-correlations"
    || path === "/api/feedback-cases"
    || path === "/api/eval-runs"
  ) return [];
  return UNHANDLED;
}

function changeSetPayload(path, request) {
  if (path === "/api/agent-change-sets") return [{
    change_set_id: "agc-demo",
    agent_id: "soc-ops",
    created_at: ts,
    updated_at: ts,
    status: "regression_failed",
    execution_job_id: "exec-1",
    base_commit_sha: "base-demo",
    candidate_commit_sha: "candidate-b",
    branch_name: "agent-change/agc-demo",
    worktree_path: "/tmp/agc-demo",
    title: "告警误报治理候选变更",
    diff_summary: { modified: 2 },
    publication_blocker: "回归验证存在失败用例",
  }];
  if (/^\/api\/agent-change-sets\/[^/]+\/file-diff$/.test(path)) {
    const filePath = request.queryPath || "CLAUDE.md";
    return {
      from_version_id: "base-demo",
      to_version_id: "candidate-b",
      path: filePath,
      archive_path: `workspace/${filePath}`,
      status: "modified",
      before: { path: filePath, sha256: "before", size: 18, type: "file" },
      after: { path: filePath, sha256: "after", size: 42, type: "file" },
      unified_diff: `--- base-demo:workspace/${filePath}\n+++ candidate-b:workspace/${filePath}\n@@ -1 +1,2 @@\n 原有治理提示\n+新增事件时间与告警时间一致性校验\n`,
      is_text: true,
      truncated: false,
      reason: null,
    };
  }
  if (/^\/api\/agent-change-sets\/[^/]+\/regression-runs$/.test(path)) {
    const body = JSON.parse(request.postData || "{}");
    const changeSetId = path.split("/")[3];
    return {
      eval_run_id: "evr-demo",
      dataset_id: body.dataset_id,
      dataset_snapshot: TYPED_TEST_DATASET,
      change_set_id: changeSetId,
      result_status: "passed",
      items: TYPED_TEST_DATASET.cases.map((item) => ({
        dataset_case_id: item.case_id,
        dataset_case_snapshot: item,
        status: "passed",
      })),
      gate_result: { status: "passed", blocked_dataset_case_ids: [], review_dataset_case_ids: [], note_dataset_case_ids: [] },
      summary: { total: TYPED_TEST_DATASET.cases.length, passed: TYPED_TEST_DATASET.cases.length, failed: 0 },
    };
  }
  if (/^\/api\/agent-change-sets\/[^/]+\/regression-runs\/[^/]+\/review$/.test(path)) {
    const body = JSON.parse(request.postData || "{}");
    const changeSetId = path.split("/")[3];
    const evalRunId = path.split("/")[5];
    const rejected = (body.decisions || []).filter((item) => item.decision === "reject").length;
    return {
      eval_run_id: evalRunId,
      dataset_id: TYPED_TEST_DATASET.dataset_id,
      dataset_snapshot: TYPED_TEST_DATASET,
      change_set_id: changeSetId,
      agent_id: TYPED_TEST_DATASET.agent_id,
      source: "typed_test_dataset",
      status: "completed",
      result_status: rejected ? "failed" : "passed_with_notes",
      items: (body.decisions || []).map((decision, index) => ({
        eval_run_id: evalRunId,
        eval_run_item_id: `evri-review-${index + 1}`,
        dataset_case_id: decision.dataset_case_id,
        dataset_case_snapshot: TYPED_TEST_DATASET.cases.find((item) => item.case_id === decision.dataset_case_id)
          || TYPED_TEST_DATASET.cases[index]
          || TYPED_TEST_DATASET.cases[0],
        status: "needs_human_review",
      })),
      gate_result: {
        status: rejected ? "blocked" : "passed_with_notes",
        blocked_dataset_case_ids: rejected
          ? (body.decisions || []).filter((item) => item.decision === "reject").map((item) => item.dataset_case_id)
          : [],
        review_dataset_case_ids: [],
        note_dataset_case_ids: rejected
          ? (body.decisions || []).filter((item) => item.decision === "approve").map((item) => item.dataset_case_id)
          : (body.decisions || []).map((item) => item.dataset_case_id),
        review_decision: {
          review_id: body.review_id,
          operator: body.operator,
          reason: body.reason,
          scope: body.scope,
          items: body.decisions || [],
          created_at: ts,
        },
      },
      summary: {
        total: (body.decisions || []).length,
        passed: 0,
        failed: 0,
        blocked: rejected,
        needs_human_review: (body.decisions || []).length,
        review_required: 0,
        passed_with_notes: (body.decisions || []).length - rejected,
      },
      created_at: ts,
      completed_at: ts,
    };
  }
  if (/^\/api\/agent-change-sets\/[^/]+\/worktree-cleanup\/retry$/.test(path)) {
    const changeSetId = path.split("/")[3];
    return {
      change_set_id: changeSetId,
      agent_id: "soc-ops",
      created_at: ts,
      updated_at: ts,
      status: "failed",
      base_commit_sha: "base-demo",
      candidate_commit_sha: "candidate-b",
      branch_name: "agent-change/test",
      worktree_path: "/tmp/test",
      worktree_cleanup_pending: false,
    };
  }
  if (/^\/api\/agent-change-sets\/[^/]+\/publish$/.test(path)) return { release_id: "agr-demo", agent_id: "soc-ops", status: "published", tag_name: "agent-release-demo", commit_sha: "candidate-b", created_at: ts, updated_at: ts };
  return UNHANDLED;
}

function assetPayload(path, request) {
  if (path === "/api/assets") return [{
    asset_id: "ast-1",
    agent_id: "soc-ops",
    asset_type: "methodology",
    title: "时间窗口一致性核验方法",
    body: "当告警时间与事件时间窗口不一致时，先核验数据源。",
    source_improvement_id: "imp-demo01",
    inherited_from: "",
    created_at: ts,
    updated_at: ts,
  }];
  if (path === "/api/test-datasets") {
    if (request.agentId && request.agentId !== TYPED_TEST_DATASET.agent_id) return [];
    if (request.sourceImprovementId && request.sourceImprovementId !== TYPED_TEST_DATASET.source_improvement_id) return [];
    return [STALE_TEST_DATASET, TYPED_TEST_DATASET];
  }
  if (/^\/api\/test-datasets\/[^/]+\/revisions$/.test(path)) return [{
    revision_id: "tdr-demo04",
    dataset_id: TYPED_TEST_DATASET.dataset_id,
    revision: TYPED_TEST_DATASET.revision,
    previous_lifecycle_state: null,
    lifecycle_state: TYPED_TEST_DATASET.lifecycle_state,
    operator: "system",
    reason: "adopted_from_confirmed_regression_assessment",
    before: {},
    after: {},
    created_at: ts,
  }];
  if (/^\/api\/test-datasets\/[^/]+\/lifecycle$/.test(path)) {
    const body = JSON.parse(request.postData || "{}");
    return { ...TYPED_TEST_DATASET, lifecycle_state: body.target_state, revision: TYPED_TEST_DATASET.revision + 1 };
  }
  if (/^\/api\/improvements\/[^/]+\/test-dataset\/adopt$/.test(path)) return TYPED_TEST_DATASET;
  if (path === "/api/improvements") return IMPROVEMENTS;
  return UNHANDLED;
}

function runtimeConfigPayload(path, request) {
  if (path === "/api/config") return {
    agent_id: "main-agent",
    claude_config_mode: "native",
    claude_root: "/data/business-agents/main-agent/claude-root",
    claude_home: "/data/business-agents/main-agent/claude-root/.claude",
    claude_global_config_file: "/data/business-agents/main-agent/claude-root/.claude.json",
    claude_config_dir: null,
    setting_sources_effective: null,
    mappings: [
      {
        scope: "project",
        kind: "instructions",
        container_path: "/data/business-agents/main-agent/workspace/CLAUDE.md",
        exists: true,
        loaded_by_default: true,
        load_semantics: "claude_loaded",
        display_group: "agent_project_config",
        safe_to_edit: true,
        git_policy: "tracked",
      },
      {
        scope: "project",
        kind: "mcp",
        container_path: "/data/business-agents/main-agent/workspace/.mcp.json",
        exists: true,
        loaded_by_default: true,
        load_semantics: "claude_loaded",
        display_group: "agent_project_config",
        safe_to_edit: true,
        git_policy: "tracked",
      },
      {
        scope: "runtime",
        kind: "agent-change-set-worktrees",
        container_path: "/data/business-agents/main-agent/version/worktrees",
        exists: true,
        loaded_by_default: false,
        load_semantics: "runtime_used",
        display_group: "versioning_runtime",
        safe_to_edit: false,
        git_policy: "ignored",
      },
    ],
  };
  if (path === "/api/agent-config-file") {
    const body = request.method === "PUT" ? JSON.parse(request.postData || "{}") : {};
    return {
      agent_id: "main-agent",
      path: ".mcp.json",
      container_path: "/data/business-agents/main-agent/workspace/.mcp.json",
      exists: true,
      content: typeof body.content === "string" ? body.content : '{\n  "mcpServers": {}\n}\n',
      sha256: "mock-sha-after",
      size_bytes: 24,
      content_type: "application/json",
      sdk_session_invalidated: request.method === "PUT",
    };
  }
  if (path === "/api/agent-repository") return { status: "active", dirty: false, changed_files: [], file_diffs: [] };
  if (path === "/api/agent-repository/current") return { agent_version_id: "v0", commit_sha: "v0", created_at: ts, reason: "current" };
  return UNHANDLED;
}

function improvementPayload(path, request) {
  if (/^\/api\/improvements\/[^/]+\/similar$/.test(path)) return [{ improvement: { ...IMPROVEMENTS[0], improvement_id: "imp-sim01", title: "告警误报治理(相似项)" }, score: 0.55 }];
  if (/^\/api\/improvements\/[^/]+\/links$/.test(path)) return [];
  if (/^\/api\/improvements\/[^/]+\/feedbacks$/.test(path)) {
    const improvementId = decodeURIComponent(path.split("/")[3] || "");
    if (improvementId === "imp-demo05") {
      return [
        { feedback_id: "fb-row-2", improvement_id: "imp-demo05", agent_id: "soc-ops", summary: "告警时间窗口与事件时间不一致", source: "feedback_inbox", status: "merged", raw_text: "第一条反馈原文：时间窗口不一致导致误报。", run_id: "run-2", session_id: "s-2", agent_version_id: "v1.2.0", scenario: "alert-triage", task_id: "task-2", alert_id: "alert-002", case_id: "fb-2", created_at: ts },
        { feedback_id: "fb-row-3", improvement_id: "imp-demo05", agent_id: "soc-ops", summary: "sec-ops-data 返回数据无法支撑告警判断", source: "trace", status: "merged", raw_text: "第二条反馈原文：返回数据像模拟数据，需要核验真实数据源。", run_id: "run-3", session_id: "s-3", agent_version_id: "v1.2.0", scenario: "alert-triage", task_id: "task-3", alert_id: "alert-003", case_id: "fb-3", created_at: ts },
      ];
    }
    return [{ feedback_id: "fb-1", improvement_id: "imp-demo01", agent_id: "soc-ops", summary: "这个告警其实是误报", source: "playground_run", status: "merged", raw_text: "", run_id: "run-1", session_id: "s-1", agent_version_id: "v1.2.0", scenario: "alert-triage", task_id: "task-1", alert_id: "alert-001", case_id: "case-001", created_at: ts }];
  }
  if (/^\/api\/improvements\/[^/]+\/normalized-feedback\/confirm$/.test(path)) { advanceMockImprovement(path, "triage"); return { normalized_feedback_id: "nf-1", improvement_id: "imp-demo01", problem: "告警误报", possible_reason: "事件时间与告警时间不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "生成归因分析", user_quote: "这个告警其实是误报", status: "confirmed", created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/normalized-feedback$/.test(path)) { if (request.method !== "GET") advanceMockImprovement(path, "triage"); return { normalized_feedback_id: "nf-1", improvement_id: "imp-demo01", problem: "告警误报", possible_reason: "事件时间与告警时间不一致", possible_object: "sec-ops-data MCP 数据", impact: "中", suggestion: "生成归因分析", user_quote: "这个告警其实是误报", status: "draft", created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/attribution\/generate$/.test(path)) { advanceMockImprovement(path, "attribution"); return { attribution_id: "attr-1", improvement_id: "imp-demo01", summary: "MCP 数据时间不一致导致误判", responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"], evidence: ["list_events 返回的数据时间与告警时间窗口不一致"], status: "draft", generated_by: "governor", generation_trace_id: "trace-attr-demo", generation_trace_url: internalTraceUrl("trace-attr-demo"), created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/attribution\/confirm$/.test(path)) return { attribution_id: "attr-1", improvement_id: "imp-demo01", summary: "MCP 数据时间不一致导致误判", responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"], evidence: ["list_events 返回的数据时间与告警时间窗口不一致"], status: "confirmed", generated_by: "governor", generation_trace_id: "trace-attr-demo", generation_trace_url: internalTraceUrl("trace-attr-demo"), created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/attribution$/.test(path)) return { attribution_id: "attr-1", improvement_id: "imp-demo01", summary: "MCP 数据时间不一致导致误判", responsibility_boundary: ["不是主 Agent 推理错误", "主要是外部 MCP 数据源质量问题"], evidence: ["list_events 返回的数据时间与告警时间窗口不一致"], status: "draft", generated_by: "governor", generation_trace_id: "trace-attr-demo", generation_trace_url: internalTraceUrl("trace-attr-demo"), created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/optimization-plan\/generate$/.test(path)) { advanceMockImprovement(path, "optimization"); return { optimization_plan_id: "opt-1", improvement_id: "imp-demo01", summary: "针对告警误报：补充时间一致性校验", changes: [{ target: "prompt", change: "新增时间校验指令" }], status: "draft", generated_by: "governor", created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/optimization-plan\/confirm$/.test(path)) return { optimization_plan_id: "opt-1", improvement_id: "imp-demo01", summary: "针对告警误报：补充时间一致性校验", changes: [{ target: "prompt", change: "新增事件时间与告警时间一致性校验指令" }], status: "confirmed", generated_by: "governor", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/optimization-plan$/.test(path)) return { optimization_plan_id: "opt-1", improvement_id: "imp-demo01", summary: "针对告警误报：补充时间一致性校验", changes: [{ target: "prompt", change: "新增事件时间与告警时间一致性校验指令" }], status: "confirmed", generated_by: "governor", created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/execution\/apply$/.test(path)) { advanceMockImprovement(path, "execution"); return { execution_id: "exec-1", improvement_id: "imp-demo01", summary: "已在隔离变更集应用并生成候选版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "candidate-b", status: "draft", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "candidate-b", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/execution\/confirm$/.test(path)) return { execution_id: "exec-1", improvement_id: "imp-demo01", summary: "已在隔离变更集应用并生成候选版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "candidate-b", status: "confirmed", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "candidate-b", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/regression-assessment\/generate$/.test(path)) { advanceMockImprovement(path, "regression"); return { regression_assessment_id: "reg-1", improvement_id: "imp-demo01", summary: "治理 Agent 生成 3 条回归用例候选。", cases: REGRESSION_CASES, status: "draft", generated_by: "governor", generation_trace_id: "trace-reg-demo", generation_trace_url: internalTraceUrl("trace-reg-demo"), created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/regression-assessment\/confirm$/.test(path)) return { regression_assessment_id: "reg-1", improvement_id: "imp-demo01", summary: "治理 Agent 生成 3 条回归用例候选。", cases: REGRESSION_CASES, status: "confirmed", generated_by: "governor", generation_trace_id: "trace-reg-demo", generation_trace_url: internalTraceUrl("trace-reg-demo"), created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/regression-assessment$/.test(path)) return { regression_assessment_id: "reg-1", improvement_id: "imp-demo01", summary: "治理 Agent 生成 3 条回归用例候选。", cases: REGRESSION_CASES, status: "draft", generated_by: "governor", generation_trace_id: "trace-reg-demo", generation_trace_url: internalTraceUrl("trace-reg-demo"), created_at: ts, updated_at: ts };
  const executionRecord = path.match(/^\/api\/improvements\/([^/]+)\/execution$/);
  if (executionRecord) {
    const improvementId = decodeURIComponent(executionRecord[1] || "");
    if (improvementId === "imp-demo06") return { __status: 404, detail: "not found" };
    return { execution_id: "exec-1", improvement_id: improvementId || "imp-demo01", summary: "已在隔离变更集应用并生成候选版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "candidate-b", status: "draft", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "candidate-b", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts };
  }
  const lifecycle = path.match(/^\/api\/improvements\/([^/]+)\/lifecycle$/);
  if (lifecycle) {
    const body = JSON.parse(request.postData || "{}");
    const item = IMPROVEMENTS.find((row) => row.improvement_id === decodeURIComponent(lifecycle[1])) || IMPROVEMENTS[0];
    if (body.stage) item.improvement_stage = body.stage;
    return { ...item, updated_at: ts };
  }
  const oneImprovement = path.match(/^\/api\/improvements\/([^/]+)$/);
  if (oneImprovement) return IMPROVEMENTS.find((item) => item.improvement_id === decodeURIComponent(oneImprovement[1])) || IMPROVEMENTS[0];
  return UNHANDLED;
}

export function defaultPayload(path, request = {}) {
  for (const handler of [basePayload, changeSetPayload, assetPayload, runtimeConfigPayload, improvementPayload]) {
    const payload = handler(path, request);
    if (payload !== UNHANDLED) return payload;
  }
  return {};
}
