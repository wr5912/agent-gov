from app.runtime.json_types import JsonObject

CURRENT_PATHS = {
    "/health",
    "/api/feedback-signals",
    "/api/improvements",
    "/api/improvements/{improvement_id}/attribution/generate",
    "/api/improvements/{improvement_id}/optimization-plan/generate",
    "/api/improvements/{improvement_id}/execution/apply",
    "/api/improvements/{improvement_id}/regression-test-design/generate",
    "/api/agent-registry/{agent_id}/test-suite",
    "/api/agent-registry/{agent_id}/test-suite/file",
    "/api/agent-registry/{agent_id}/presentation",
    "/api/agent-registry/{agent_id}/test-schedule",
    "/api/agent-registry/{agent_id}/test-schedule/events",
    "/api/agent-test-assets",
    "/api/agent-test-runs",
    "/api/agent-test-runs/history",
    "/api/agent-change-sets/{change_set_id}/test-runs",
    "/api/agent-test-runs/{test_run_id}",
    "/api/agent-test-runs/{test_run_id}/cancel",
    "/api/agent-runs/{run_id}/cancel",
    "/api/agent-test-sessions",
    "/api/agent-test-sessions/{test_session_id}/messages",
    "/api/langfuse/traces/{trace_id}",
    "/api/agent-config-file",
    "/api/agent-change-sets/{change_set_id}/publish",
    "/api/agent-releases/{release_id}/restore",
    "/api/claude-user-input-requests",
    "/api/agent-runtime/sdk-events",
    "/api/debug/agent-runtime/raw-events",
    "/v1/agentgov/confirmation-requests/{request_id}/decision",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/responses/{response_id}",
    "/v1/conversations",
    "/v1/conversations/{conversation_id}",
    "/v1/conversations/{conversation_id}/items",
}

LEGACY_PATHS = {
    "/api/automation-policy",
    "/api/eval-datasets/feedback/sync",
    "/api/eval-cases",
    "/api/eval-cases/{eval_case_id}",
    "/api/feedback-sources/eval-cases/generate",
    "/api/improvements/{improvement_id}/auto-advance",
    "/api/feedback-optimization-batches",
    "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
    "/api/optimization-proposals",
    "/api/optimization-tasks/{task_id}/execution-jobs",
    "/api/claude-hitl-requests",
    "/api/claude-hitl-requests/{request_id}/decision",
    "/api/claude-user-input-requests/{request_id}/decision",
}

LEGACY_SCHEMAS = {
    "AutomationPolicyResponse",
    "AutomationPolicyUpdateRequest",
    "AutoAdvanceResponse",
    "FeedbackOptimizationBatchResponse",
    "OptimizationTaskResponse",
    "OptimizationProposalResponse",
    "ExternalGovernanceItemResponse",
    "RegressionPlanResponse",
    "EvalCaseResponse",
    "FeedbackEvalCaseGenerateRequest",
    "FeedbackEvalCaseUpdateRequest",
    "RegressionAssetGovernanceActionRequest",
    "ScenarioPackResponse",
    "TestDatasetResponse",
    "EvalRunResponse",
}


def _assert_path_inventory(paths: JsonObject) -> None:
    assert set(paths) >= CURRENT_PATHS
    assert set(paths).isdisjoint(LEGACY_PATHS)
    assert not any(path.startswith(("/api/regression-assets", "/api/scenario-packs", "/api/test-datasets")) for path in paths)


def _assert_component_inventory(component_schemas: JsonObject) -> None:
    assert LEGACY_SCHEMAS.isdisjoint(component_schemas)
    for component_name in (
        "AttributionResponse",
        "OptimizationPlanResponse",
        "ExecutionResponse",
        "RegressionTestDesignResponse",
    ):
        component = component_schemas[component_name]
        assert isinstance(component, dict)
        assert "generation_trace_id" in component["properties"]
        assert "generation_trace_url" in component["properties"]


def _assert_runtime_trace_contracts(paths: JsonObject, component_schemas: JsonObject) -> None:
    agent_run = component_schemas["AgentRunResponse"]
    assert isinstance(agent_run, dict)
    assert "langfuse_trace_id" in agent_run["properties"]
    assert "langfuse_trace_url" in agent_run["properties"]
    assert {"turn_status", "turn_index", "turn_error", "errors"} <= set(agent_run["properties"])
    assert "/api/agent-runs/{run_id}/trace" in paths
    trace_response = component_schemas["AgentRunTraceResponse"]
    assert isinstance(trace_response, dict)
    assert {"run_id", "completeness", "events", "turn_status", "turn_error"} <= set(trace_response["properties"])
    assert "schema_version" not in trace_response["properties"]
    trace_event = component_schemas["AgentTraceEvent"]
    assert isinstance(trace_event, dict)
    assert {
        "event_id",
        "run_id",
        "sequence",
        "message_index",
        "kind",
        "source_event",
        "scope",
        "payload",
    } <= set(trace_event["properties"])


def _assert_session_and_config_contracts(paths: JsonObject, component_schemas: JsonObject) -> None:
    request_extension = component_schemas["AgentGovRequestExtension"]
    assert isinstance(request_extension, dict)
    assert request_extension["properties"]["include_trace"]["default"] is False
    conversation_item = component_schemas["ConversationItem"]
    assert isinstance(conversation_item, dict)
    assert "agentgov" in conversation_item["properties"]
    item_extension = component_schemas["AgentGovConversationItemExtension"]
    assert isinstance(item_extension, dict)
    assert set(item_extension["properties"]) == {
        "run_id",
        "sdk_session_id",
        "agent_version_id",
        "langfuse_trace_id",
        "langfuse_trace_url",
    }
    assert item_extension["required"] == ["run_id"]
    test_file_symbol = component_schemas["AgentTestFileSymbol"]
    assert isinstance(test_file_symbol, dict)
    assert set(test_file_symbol["required"]) == {"kind", "name", "qualified_name", "line"}
    agent_config_file = paths["/api/agent-config-file"]
    assert isinstance(agent_config_file, dict)
    assert {"get", "put"} <= set(agent_config_file)
    agent_config_update = component_schemas["AgentConfigFileUpdateResponse"]
    assert isinstance(agent_config_update, dict)
    assert "sdk_session_invalidated" in agent_config_update["properties"]


def assert_current_openapi_schema(schema: JsonObject) -> None:
    assert str(schema["openapi"]).startswith("3.")
    paths = schema["paths"]
    components = schema["components"]
    assert isinstance(paths, dict)
    assert isinstance(components, dict)
    component_schemas = components["schemas"]
    assert isinstance(component_schemas, dict)
    _assert_path_inventory(paths)
    _assert_component_inventory(component_schemas)
    _assert_runtime_trace_contracts(paths, component_schemas)
    _assert_session_and_config_contracts(paths, component_schemas)
