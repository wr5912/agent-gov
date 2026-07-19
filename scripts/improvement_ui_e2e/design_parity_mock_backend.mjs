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
  { improvement_id: "imp-demo08", agent_id: "soc-ops", title: "时间窗口误判治理 · 已发布", summary: "发布条件已满足", source_feedback_refs: ["fb-1"], improvement_stage: "release", improvement_status: "done", created_at: ts, updated_at: ts },
];
const internalTraceUrl = (traceId) => `http://langfuse-web:3000/project/agent-gov/traces/${traceId}`;
const REGRESSION_TESTS = [
  {
    target_path: "tests/test_feedback_imp_demo04_01_time.py",
    test_code: "def test_time_consistency(agent):\n    result = agent.run('请审查事件时间是否一致')\n    assert '核验' in result.text and '时间' in result.text\n",
    test_intent: "验证时间不一致时先核验再升级",
    assertion_rationale: "回答必须同时包含核验动作和时间边界",
  },
  {
    target_path: "tests/test_feedback_imp_demo04_02_source.py",
    test_code: "def test_source_risk(agent):\n    result = agent.run('模拟数据能否直接作为攻击证据')\n    assert '数据源' in result.text and '证据' in result.text\n",
    test_intent: "验证模拟数据不会直接升级",
    assertion_rationale: "回答必须说明数据源风险和证据边界",
  },
  {
    target_path: "tests/test_feedback_imp_demo04_03_evidence.py",
    test_code: "def test_insufficient_evidence(agent):\n    result = agent.run('证据不足时给出确定结论')\n    assert '证据不足' in result.text and '核验' in result.text\n",
    test_intent: "验证证据不足时拒绝编造",
    assertion_rationale: "回答必须指出证据不足并给出核验动作",
  },
];
const GENERATED_TEST_FILES = REGRESSION_TESTS.map((item) => item.target_path);
const AGENT_TEST_SUITE = {
  agent_id: "soc-ops",
  commit_sha: "candidate-b",
  tests_directory_present: true,
  readme_present: true,
  test_file_count: GENERATED_TEST_FILES.length,
  test_files: GENERATED_TEST_FILES,
  suite_digest: "suite-demo-04",
  diagnostics: [],
};

function agentTestRun({
  testRunId = "atr-demo",
  changeSetId = "agc-demo",
  commitSha = "candidate-b",
  status = "passed",
} = {}) {
  const terminal = !["queued", "running"].includes(status);
  return {
    test_run_id: testRunId,
    agent_id: "soc-ops",
    commit_sha: commitSha,
    change_set_id: changeSetId,
    source: "release_check",
    status,
    cancel_requested: false,
    created_at: ts,
    started_at: status === "queued" ? null : ts,
    completed_at: terminal ? ts : null,
    suite_digest: AGENT_TEST_SUITE.suite_digest,
    command: ["python", "-m", "pytest", "-q", "-p", "agentgov_testkit.pytest_plugin", "tests"],
    suite: AGENT_TEST_SUITE,
    report: terminal ? { summary: { passed: status === "passed" ? 1 : 0, failed: status === "failed" ? 1 : 0 } } : {},
    items: terminal ? [{ nodeid: `${GENERATED_TEST_FILES[0]}::test_feedback_case_1`, outcome: status === "passed" ? "passed" : "failed", phase: "call", duration_seconds: 0.1, detail: null }] : [],
    stdout: status === "passed" ? "1 passed" : status === "failed" ? "1 failed" : "",
    stderr: status === "interrupted" ? "AgentGov service restarted while the test was running." : "",
    error: status === "interrupted" ? { code: "AGENT_TEST_RUN_INTERRUPTED", message: "服务重启中断测试。" } : {},
  };
}

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
      agentgov: { agent_id: "security-operations-expert", sdk_session_id: sessionId, updated_at: Date.parse(ts) / 1000, turns: sessionId === "mock-session" ? 36 : 4 },
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
  ) return [];
  return UNHANDLED;
}

function changeSetPayload(path, request) {
  if (path === "/api/agent-change-sets") return [{
    change_set_id: "agc-demo",
    agent_id: "soc-ops",
    created_at: ts,
    updated_at: ts,
    status: "candidate_committed",
    execution_job_id: "exec-1",
    base_commit_sha: "base-demo",
    candidate_commit_sha: "candidate-b",
    branch_name: "agent-change/agc-demo",
    worktree_path: "/tmp/agc-demo",
    title: "告警误报治理候选变更",
    diff_summary: { modified: 2 },
    source_improvement_id: "imp-demo04",
    source_attribution_status: "confirmed",
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

function agentTestingPayload(path, request) {
  if (/^\/api\/agent-registry\/[^/]+\/test-suite$/.test(path)) return AGENT_TEST_SUITE;
  const changeSetRun = path.match(/^\/api\/agent-change-sets\/([^/]+)\/test-runs$/);
  if (changeSetRun) {
    const changeSetId = decodeURIComponent(changeSetRun[1]);
    const commits = {
      "agc-target-a": "candidate-a",
      "agc-target-b": "candidate-b",
      "agc-stale": "candidate-current",
      "agc-running": "candidate-running",
      "agc-interrupted": "candidate-interrupted",
    };
    return agentTestRun({
      testRunId: "atr-created",
      changeSetId,
      commitSha: commits[changeSetId] || "candidate-b",
      status: "queued",
    });
  }
  if (path === "/api/agent-test-runs" && request.method === "POST") {
    const body = JSON.parse(request.postData || "{}");
    return agentTestRun({
      testRunId: "atr-manual",
      changeSetId: "",
      commitSha: body.commit_sha || "candidate-b",
      status: "queued",
    });
  }
  if (path === "/api/agent-test-runs") {
    const changeSetId = request.changeSetId || "agc-demo";
    if (changeSetId === "agc-target-a") {
      return [agentTestRun({ testRunId: "atr-target-a", changeSetId, commitSha: "candidate-a", status: "failed" })];
    }
    if (changeSetId === "agc-running") {
      return [agentTestRun({ testRunId: "atr-running", changeSetId, commitSha: "candidate-running", status: "running" })];
    }
    if (changeSetId === "agc-stale") {
      return [agentTestRun({ testRunId: "atr-stale", changeSetId, commitSha: "candidate-old", status: "passed" })];
    }
    if (changeSetId === "agc-interrupted") {
      return [agentTestRun({ testRunId: "atr-interrupted", changeSetId, commitSha: "candidate-interrupted", status: "interrupted" })];
    }
    const commitSha = changeSetId === "agc-target-b" || changeSetId === "agc-test-contract" ? "candidate-b" : "candidate-b";
    return [agentTestRun({ testRunId: `atr-${changeSetId}`, changeSetId, commitSha, status: "passed" })];
  }
  if (/^\/api\/agent-test-runs\/[^/]+\/cancel$/.test(path)) {
    const testRunId = path.split("/")[3];
    return agentTestRun({ testRunId, changeSetId: "agc-running", commitSha: "candidate-running", status: "cancelled" });
  }
  if (/^\/api\/agent-test-runs\/[^/]+$/.test(path)) {
    const testRunId = path.split("/")[3];
    return agentTestRun({ testRunId });
  }
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
  if (path === "/api/improvements") return IMPROVEMENTS;
  return UNHANDLED;
}

function runtimeConfigPayload(path, request) {
  if (path === "/api/config") return {
    agent_id: "security-operations-expert",
    claude_config_mode: "native",
    claude_root: "/data/business-agents/security-operations-expert/claude-root",
    claude_home: "/data/business-agents/security-operations-expert/claude-root/.claude",
    claude_global_config_file: "/data/business-agents/security-operations-expert/claude-root/.claude.json",
    claude_config_dir: null,
    setting_sources_effective: null,
    mappings: [
      {
        scope: "project",
        kind: "instructions",
        container_path: "/data/business-agents/security-operations-expert/workspace/CLAUDE.md",
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
        container_path: "/data/business-agents/security-operations-expert/workspace/.mcp.json",
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
        container_path: "/data/business-agents/security-operations-expert/version/worktrees",
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
      agent_id: "security-operations-expert",
      path: ".mcp.json",
      container_path: "/data/business-agents/security-operations-expert/workspace/.mcp.json",
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
  if (/^\/api\/improvements\/[^/]+\/execution\/apply$/.test(path)) { advanceMockImprovement(path, "execution"); return { execution_id: "exec-1", improvement_id: "imp-demo01", summary: "已在隔离的待发布变更中应用并生成待发布版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "candidate-b", status: "draft", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "candidate-b", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts }; }
  if (/^\/api\/improvements\/[^/]+\/execution\/confirm$/.test(path)) return { execution_id: "exec-1", improvement_id: "imp-demo01", summary: "已在隔离的待发布变更中应用并生成待发布版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "candidate-b", status: "confirmed", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "candidate-b", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts };
  if (/^\/api\/improvements\/[^/]+\/regression-test-design\/generate$/.test(path)) {
    advanceMockImprovement(path, "regression");
    const improvementId = decodeURIComponent(path.split("/")[3] || "imp-demo01");
    return { regression_test_design_id: "reg-1", improvement_id: improvementId, summary: "治理 Agent 生成 3 个 pytest 测试文件候选。", tests: REGRESSION_TESTS, no_action_reason: "", status: "draft", generated_by: "governor", generation_trace_id: "trace-reg-demo", generation_trace_url: internalTraceUrl("trace-reg-demo"), created_at: ts, updated_at: ts, generated_test_files: [], candidate_commit_sha: "", test_run: null };
  }
  if (/^\/api\/improvements\/[^/]+\/regression-test-design\/confirm$/.test(path)) {
    const improvementId = decodeURIComponent(path.split("/")[3] || "imp-demo01");
    return { regression_test_design_id: "reg-1", improvement_id: improvementId, summary: "治理 Agent 生成 3 个 pytest 测试文件候选。", tests: REGRESSION_TESTS, no_action_reason: "", status: "confirmed", generated_by: "governor", generation_trace_id: "trace-reg-demo", generation_trace_url: internalTraceUrl("trace-reg-demo"), created_at: ts, updated_at: ts, generated_test_files: GENERATED_TEST_FILES, candidate_commit_sha: "candidate-b", test_run: null };
  }
  if (/^\/api\/improvements\/[^/]+\/regression-test-design$/.test(path)) {
    const improvementId = decodeURIComponent(path.split("/")[3] || "imp-demo01");
    const materialized = improvementId === "imp-demo04";
    return { regression_test_design_id: "reg-1", improvement_id: improvementId, summary: "治理 Agent 生成 3 个 pytest 测试文件候选。", tests: REGRESSION_TESTS, no_action_reason: "", status: materialized ? "confirmed" : "draft", generated_by: "governor", generation_trace_id: "trace-reg-demo", generation_trace_url: internalTraceUrl("trace-reg-demo"), created_at: ts, updated_at: ts, generated_test_files: materialized ? GENERATED_TEST_FILES : [], candidate_commit_sha: materialized ? "candidate-b" : "", test_run: materialized ? agentTestRun() : null };
  }
  const executionRecord = path.match(/^\/api\/improvements\/([^/]+)\/execution$/);
  if (executionRecord) {
    const improvementId = decodeURIComponent(executionRecord[1] || "");
    if (improvementId === "imp-demo06") return { __status: 404, detail: "not found" };
    return { execution_id: "exec-1", improvement_id: improvementId || "imp-demo01", summary: "已在隔离的待发布变更中应用并生成待发布版本", changes_applied: ["append_text: CLAUDE.md"], agent_version: "candidate-b", status: "draft", generated_by: "governor", change_set_id: "agc-demo", applied_agent_version_id: "candidate-b", applied_diff: { changed_files: ["CLAUDE.md"] }, created_at: ts, updated_at: ts };
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
  for (const handler of [basePayload, changeSetPayload, agentTestingPayload, assetPayload, runtimeConfigPayload, improvementPayload]) {
    const payload = handler(path, request);
    if (payload !== UNHANDLED) return payload;
  }
  return {};
}
