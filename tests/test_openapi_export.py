import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.openapi_contract import NON_200_SUCCESS_CODES, expected_error_statuses, operation_items
from app.openapi_request_examples import REQUEST_EXAMPLE_CONTRACTS
from app.sse_contracts import (
    CHAT_STREAM_PATH,
    CLAUDE_SDK_EVENTS_PATH,
    RESPONSES_PATH,
    require_registered_sse_event,
)
from fastapi.routing import APIRoute
from scripts.audit_openapi_contract import audit_live_matches_local, audit_schema
from scripts.export_openapi import (
    CONTAINER_RUNTIME_VOLUME_ROOT,
    LOCAL_DEBUG_RUNTIME_VOLUME_ROOT,
    _apply_local_defaults,
    _local_default_volume_root,
    build_openapi_schema,
)


def test_export_openapi_local_defaults_use_debug_volume_unless_container_mode(monkeypatch):
    monkeypatch.delenv("HOST_RUNTIME_VOLUME_ROOT", raising=False)
    monkeypatch.delenv("RUNTIME_VOLUME_MODE", raising=False)
    monkeypatch.delenv("RUNTIME_CONTAINER", raising=False)

    assert _local_default_volume_root() == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT

    monkeypatch.setenv("RUNTIME_CONTAINER", "1")
    assert _local_default_volume_root() == CONTAINER_RUNTIME_VOLUME_ROOT

    monkeypatch.setenv("HOST_RUNTIME_VOLUME_ROOT", "/tmp/custom-runtime-root")
    assert _local_default_volume_root() == Path("/tmp/custom-runtime-root")


def test_export_openapi_applies_local_debug_env_file_mode(monkeypatch):
    original = os.environ.copy()
    for key in (
        "RUNTIME_VOLUME_MODE",
        "RUNTIME_CONTAINER",
        "HOST_RUNTIME_VOLUME_ROOT",
        "WORKSPACE_DIR",
        "MAIN_WORKSPACE_DIR",
        "GOVERNOR_WORKSPACE_DIR",
        "DATA_DIR",
        "CLAUDE_ROOT",
        "MAIN_CLAUDE_ROOT",
        "GOVERNOR_CLAUDE_ROOT",
        "CLAUDE_HOME",
    ):
        monkeypatch.delenv(key, raising=False)

    try:
        _apply_local_defaults(Path.cwd())

        assert "RUNTIME_VOLUME_MODE" not in os.environ
        assert os.environ["RUNTIME_CONTAINER"] == "0"
        assert os.environ["HOST_RUNTIME_VOLUME_ROOT"] == LOCAL_DEBUG_RUNTIME_VOLUME_ROOT.as_posix()
        assert os.environ["WORKSPACE_DIR"] == (LOCAL_DEBUG_RUNTIME_VOLUME_ROOT / "main-workspace").as_posix()
        assert os.environ["DATA_DIR"] == (LOCAL_DEBUG_RUNTIME_VOLUME_ROOT / "data").as_posix()
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_export_openapi_script_writes_current_schema(tmp_path):
    root = tmp_path / "docker" / "volume"
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE_DIR": str(root / "main-workspace"),
            "MAIN_WORKSPACE_DIR": str(root / "main-workspace"),
            "GOVERNOR_WORKSPACE_DIR": str(root / "governor-workspace"),
            "DATA_DIR": str(root / "data"),
            "CLAUDE_ROOT": str(root / "claude-roots" / "main"),
            "MAIN_CLAUDE_ROOT": str(root / "claude-roots" / "main"),
            "GOVERNOR_CLAUDE_ROOT": str(root / "claude-roots" / "governor"),
            "CLAUDE_HOME": str(root / "claude-roots" / "main" / ".claude"),
            "ANTHROPIC_API_KEY": "",
            "MODEL_PROVIDER_API_KEY": "",
            "API_KEY": "",
        }
    )
    output_path = tmp_path / "openapi.json"

    subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--output", str(output_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )

    schema = json.loads(output_path.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.")
    current_paths = {
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
    assert current_paths <= set(schema["paths"])

    legacy_paths = {
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
    assert set(schema["paths"]).isdisjoint(legacy_paths)
    assert not any(path.startswith(("/api/regression-assets", "/api/scenario-packs", "/api/test-datasets")) for path in schema["paths"])

    for schema_name in (
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
    ):
        assert schema_name not in schema["components"]["schemas"]

    attribution = schema["components"]["schemas"]["AttributionResponse"]
    optimization = schema["components"]["schemas"]["OptimizationPlanResponse"]
    execution = schema["components"]["schemas"]["ExecutionResponse"]
    regression = schema["components"]["schemas"]["RegressionTestDesignResponse"]
    for component in (attribution, optimization, execution, regression):
        assert "generation_trace_id" in component["properties"]
        assert "generation_trace_url" in component["properties"]

    agent_run = schema["components"]["schemas"]["AgentRunResponse"]
    assert "langfuse_trace_id" in agent_run["properties"]
    assert "langfuse_trace_url" in agent_run["properties"]
    assert {"turn_status", "turn_index", "turn_error", "errors"} <= set(agent_run["properties"])
    assert "/api/agent-runs/{run_id}/trace" in schema["paths"]
    trace_response = schema["components"]["schemas"]["AgentRunTraceResponse"]
    assert {"run_id", "completeness", "events", "turn_status", "turn_error"} <= set(trace_response["properties"])
    assert "schema_version" not in trace_response["properties"]
    trace_event = schema["components"]["schemas"]["AgentTraceEvent"]
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
    assert schema["components"]["schemas"]["AgentGovRequestExtension"]["properties"]["include_trace"]["default"] is False
    conversation_item = schema["components"]["schemas"]["ConversationItem"]
    assert "agentgov" in conversation_item["properties"]
    item_extension = schema["components"]["schemas"]["AgentGovConversationItemExtension"]
    assert set(item_extension["properties"]) == {
        "run_id",
        "sdk_session_id",
        "agent_version_id",
        "langfuse_trace_id",
        "langfuse_trace_url",
    }
    assert item_extension["required"] == ["run_id"]

    test_file_symbol = schema["components"]["schemas"]["AgentTestFileSymbol"]
    assert set(test_file_symbol["required"]) == {"kind", "name", "qualified_name", "line"}

    agent_config_file = schema["paths"]["/api/agent-config-file"]
    assert {"get", "put"} <= set(agent_config_file)
    agent_config_update = schema["components"]["schemas"]["AgentConfigFileUpdateResponse"]
    assert "sdk_session_invalidated" in agent_config_update["properties"]


def test_openapi_contract_audit_passes_current_schema():
    schema = dict(build_openapi_schema())
    expected_version = Path("VERSION").read_text(encoding="utf-8").strip()

    assert audit_schema(schema, expected_version=expected_version) == []


def test_every_request_body_has_named_runtime_valid_examples() -> None:
    schema = build_openapi_schema()
    from app.main import app

    body_operations = {(path, method) for path, method, operation in operation_items(schema) if "requestBody" in operation}
    assert body_operations == set(REQUEST_EXAMPLE_CONTRACTS)

    routes = {(route.path, method.lower()): route for route in app.routes if isinstance(route, APIRoute) for method in route.methods}
    for key, contract in REQUEST_EXAMPLE_CONTRACTS.items():
        operation = schema["paths"][key[0]][key[1]]
        assert operation["description"].strip()
        examples = operation["requestBody"]["content"][contract.media_type]["examples"]
        assert examples == dict(contract.examples)
        if contract.media_type != "application/json":
            continue
        route = routes[key]
        assert route.body_field is not None
        for example_name, example in examples.items():
            _, errors = route.body_field.validate(example["value"], {}, loc=("body",))
            assert errors is None, f"{key[1].upper()} {key[0]} example {example_name}: {errors}"


def test_responses_named_examples_explain_strict_and_control_modes() -> None:
    operation = build_openapi_schema()["paths"][RESPONSES_PATH]["post"]
    examples = operation["requestBody"]["content"]["application/json"]["examples"]

    assert set(examples) == {
        "agentgov_control_stream",
        "agentgov_control_structured",
        "strict_openai",
    }
    assert examples["agentgov_control_stream"]["value"]["agentgov"]["agent_id"]
    assert examples["agentgov_control_structured"]["value"]["instructions"]
    assert "agentgov" not in examples["strict_openai"]["value"]
    wording = f"{operation['summary']} {operation['description']}".lower()
    assert "transitional" in wording
    assert "source of truth" in wording
    assert "canonical" not in wording


def test_agent_governance_response_status_domains_are_not_cross_wired() -> None:
    components = build_openapi_schema()["components"]["schemas"]

    assert set(components["AgentRepositoryStatusResponse"]["properties"]["status"]["enum"]) == {
        "active",
        "degraded",
    }
    assert set(components["AgentGitFileDiffResponse"]["properties"]["status"]["enum"]) == {
        "missing",
        "added",
        "deleted",
        "unchanged",
        "modified",
        "binary_or_too_large",
    }
    assert set(components["AgentChangeSetResponse"]["properties"]["status"]["enum"]) == {
        "draft",
        "execution_ready",
        "candidate_committed",
        "pending_approval",
        "approved",
        "rejected",
        "publishing",
        "published",
        "abandoned",
        "failed",
    }
    assert set(components["AgentReleaseResponse"]["properties"]["status"]["enum"]) == {
        "published",
        "archived",
        "rolled_back",
        "rollback_failed",
    }


def test_openapi_requires_typed_improvement_artifact_presence() -> None:
    schema = build_openapi_schema()
    item = schema["components"]["schemas"]["ImprovementItemResponse"]
    presence = schema["components"]["schemas"]["ImprovementArtifactPresence"]
    expected_fields = {
        "normalized_feedback",
        "attribution",
        "optimization_plan",
        "execution",
        "regression_test_design",
    }

    assert "artifact_presence" in item["required"]
    assert item["properties"]["artifact_presence"] == {
        "$ref": "#/components/schemas/ImprovementArtifactPresence",
        "description": "后端按持久化行实时投影的产物存在性；不得由阶段推导。",
    }
    assert set(presence["properties"]) == expected_fields
    assert set(presence["required"]) == expected_fields
    assert all(presence["properties"][field]["type"] == "boolean" for field in expected_fields)


def test_openapi_documents_auth_error_for_secured_operations():
    schema = build_openapi_schema()
    secured_operations = [(path, method, operation) for path, method, operation in operation_items(schema) if operation.get("security")]

    assert secured_operations
    for path, method, operation in secured_operations:
        response = operation["responses"].get("401")
        assert response, f"{method.upper()} {path} missing 401 response"
        assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/HttpErrorResponse"}


def test_openapi_documents_streaming_media_types():
    schema = build_openapi_schema()

    chat_stream = schema["paths"]["/api/chat/stream"]["post"]["responses"]["200"]["content"]
    assert set(chat_stream) == {"text/event-stream"}

    sdk_events = schema["paths"]["/api/agent-runtime/sdk-events"]["post"]["responses"]["200"]["content"]
    assert set(sdk_events) == {"text/event-stream"}

    responses_content = schema["paths"]["/v1/responses"]["post"]["responses"]["200"]["content"]
    assert {"application/json", "text/event-stream"} <= set(responses_content)
    assert responses_content["application/json"]["schema"] == {"$ref": "#/components/schemas/ResponseObject"}
    for path in (
        "/api/chat/stream",
        "/api/agent-runtime/sdk-events",
        "/v1/responses",
    ):
        headers = schema["paths"][path]["post"]["responses"]["200"]["headers"]
        assert {"X-AgentGov-Run-Id", "X-AgentGov-Session-Id"} <= set(headers)

    raw_operation = schema["paths"]["/api/debug/agent-runtime/raw-events"]["post"]
    raw_success = raw_operation["responses"]["200"]
    assert raw_success["content"] == {
        "application/octet-stream": {
            "schema": {
                "type": "string",
                "format": "binary",
                "description": "Unparsed native Runtime stdout bytes.",
            }
        }
    }
    assert {
        "X-AgentGov-Run-Id",
        "X-AgentGov-Session-Id",
        "X-AgentGov-Agent-Id",
        "X-AgentGov-Runtime-Kind",
        "X-AgentGov-Execution-Origin",
        "X-AgentGov-Native-Protocol",
        "X-AgentGov-Runtime-Version",
        "X-AgentGov-Raw-Fidelity",
    } == set(raw_success["headers"])
    assert {"401", "403", "404", "409", "413", "422", "501", "503"} <= set(raw_operation["responses"])


def test_openapi_documents_complete_per_surface_sse_contracts() -> None:
    schema = build_openapi_schema()
    components = schema["components"]["schemas"]

    sdk_properties = components["ClaudeSdkEventsRequest"]["properties"]
    chat_stream_properties = components["ChatStreamRequest"]["properties"]
    targeted_chat_properties = components["AgentTargetedChatRequest"]["properties"]
    raw_properties = components["RuntimeRawEventsRequest"]["properties"]
    response_extension = components["AgentGovRequestExtension"]["properties"]
    assert sdk_properties["with_speech_summary"]["default"] is False
    assert chat_stream_properties["with_speech_summary"]["default"] is False
    assert response_extension["with_speech_summary"]["default"] is False
    assert "with_speech_summary" not in targeted_chat_properties
    assert "with_speech_summary" not in raw_properties

    envelope = components["AgentGovSpeechSummaryEnvelope"]
    assert set(envelope["required"]) == {
        "run_id",
        "ts",
        "seq",
        "payload",
    }
    assert envelope["additionalProperties"] is False
    assert envelope["properties"]["v"]["const"] == 1
    assert envelope["properties"]["type"]["const"] == "agentgov.speech_summary"

    event_names = {
        path: {item["event"] for item in schema["paths"][path]["post"]["x-agentgov-sse-events"]}
        for path in (CHAT_STREAM_PATH, CLAUDE_SDK_EVENTS_PATH, RESPONSES_PATH)
    }
    assert event_names[CHAT_STREAM_PATH] == {
        "session",
        "message",
        "trace_event",
        "prompt_suggestion",
        "claude_user_input_required",
        "claude_user_input_resolved",
        "heartbeat",
        "result",
        "error",
        "cancelled",
        "agentgov.speech_summary",
        "done",
    }
    assert event_names[CLAUDE_SDK_EVENTS_PATH] == {
        "claude.sdk.*",
        "agentgov.session",
        "agentgov.prompt_suggestion",
        "agentgov.confirmation.requested",
        "agentgov.confirmation.resolved",
        "agentgov.speech_summary",
        "agentgov.result",
        "agentgov.error",
        "agentgov.cancelled",
        "agentgov.done",
    }
    assert {
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "agentgov.session",
        "agentgov.sdk_raw",
        "agentgov.trace_event",
        "agentgov.tool_step",
        "agentgov.tool_call.started",
        "agentgov.tool_call.arguments.delta",
        "agentgov.tool_call.arguments.done",
        "agentgov.tool_call.result",
        "agentgov.prompt_suggestion",
        "agentgov.confirmation.requested",
        "agentgov.confirmation.resolved",
        "agentgov.speech_summary",
        "agentgov.result",
        "agentgov.error",
        "agentgov.cancelled",
        "agentgov.done",
        "response.completed",
        "response.failed",
        "response.incomplete",
    } == event_names[RESPONSES_PATH]
    assert schema["paths"][RESPONSES_PATH]["post"]["x-agentgov-contract-status"] == "transitional"
    assert schema["paths"][RESPONSES_PATH]["post"]["x-agentgov-known-deviations"]
    for path in (CHAT_STREAM_PATH, CLAUDE_SDK_EVENTS_PATH, RESPONSES_PATH):
        example = schema["paths"][path]["post"]["responses"]["200"]["content"]["text/event-stream"]["examples"]["event"]["value"]
        assert "\n" in example
        assert "\\n" not in example
    assert "event: message" not in schema["paths"][RESPONSES_PATH]["post"]["responses"]["200"]["content"]["text/event-stream"]["examples"]["event"]["value"]

    for path in (
        "/api/chat",
        "/api/chat/stream",
        "/v1/chat/completions",
    ):
        assert schema["paths"][path]["post"]["deprecated"] is True
    completion_failure = schema["paths"]["/v1/chat/completions"]["post"]["responses"]["502"]
    assert completion_failure["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/OpenAIErrorResponse"}


def test_openapi_public_request_models_express_runtime_validation() -> None:
    components = build_openapi_schema()["components"]["schemas"]

    for name in ("AgentTargetedChatRequest", "ChatStreamRequest", "ClaudeSdkEventsRequest"):
        component = components[name]
        assert {"message", "agent_id"} <= set(component["required"])
        for field in ("message", "agent_id"):
            assert component["properties"][field]["minLength"] == 1
            assert component["properties"][field]["pattern"]

    assert "agent_id" in components["AgentGovRequestExtension"]["required"]
    responses_request = components["ResponsesRequest"]
    assert responses_request["oneOf"] and responses_request["allOf"]
    assert components["OpenAIChatCompletionRequest"]["properties"]["stream"]["const"] is False
    assert components["ConversationCreateRequest"]["additionalProperties"] is False


def test_openapi_reviewed_operations_have_descriptions_deprecation_and_statuses() -> None:
    schema = build_openapi_schema()
    reviewed = (
        ("/v1/conversations", "post"),
        ("/v1/conversations", "get"),
        ("/v1/conversations/{conversation_id}", "get"),
        ("/v1/conversations/{conversation_id}", "delete"),
        ("/v1/conversations/{conversation_id}/items", "get"),
        ("/api/sessions", "get"),
        ("/api/sessions/{session_id}/messages", "get"),
        ("/api/sessions/{session_id}", "delete"),
        ("/api/claude-user-input-requests", "get"),
        ("/v1/agentgov/confirmation-requests/{request_id}/decision", "post"),
    )
    for path, method in reviewed:
        assert schema["paths"][path][method]["description"].strip()

    for path, method in reviewed[5:8]:
        assert schema["paths"][path][method]["deprecated"] is True

    assert "409" in schema["paths"]["/api/chat"]["post"]["responses"]
    assert "409" in schema["paths"]["/v1/conversations/{conversation_id}"]["delete"]["responses"]
    assert "409" in schema["paths"]["/api/sessions/{session_id}"]["delete"]["responses"]
    assert "400" in schema["paths"]["/api/debug/agent-runtime/raw-events"]["post"]["responses"]
    decision_description = schema["paths"]["/v1/agentgov/confirmation-requests/{request_id}/decision"]["post"]["description"]
    assert "Bearer" in decision_description and "decision_token" in decision_description


def test_openapi_non_200_success_operations_do_not_gain_fake_200() -> None:
    schema = build_openapi_schema()
    for (path, method), expected in NON_200_SUCCESS_CODES.items():
        statuses = {status for status in schema["paths"][path][method]["responses"] if status.startswith("2")}
        assert statuses == {expected}


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        (
            lambda schema: schema["components"]["schemas"]["AgentTargetedChatRequest"].update(required=["message"]),
            "must require message and agent_id",
        ),
        (
            lambda schema: schema["components"]["schemas"]["OpenAIChatCompletionRequest"]["properties"]["stream"].pop("const"),
            "stream must declare const=false",
        ),
        (
            lambda schema: schema["paths"]["/api/sessions"]["get"].pop("description"),
            "missing non-empty description",
        ),
        (
            lambda schema: schema["paths"]["/api/sessions"]["get"].update(deprecated=False),
            "must be deprecated",
        ),
        (
            lambda schema: schema["paths"][RESPONSES_PATH]["post"].pop("x-agentgov-sse-events"),
            "differs from the typed per-surface registry",
        ),
        (
            lambda schema: schema["paths"][RESPONSES_PATH]["post"].update(description="Canonical OpenAI Responses endpoint."),
            "must not claim canonical",
        ),
        (
            lambda schema: schema["paths"][RESPONSES_PATH]["post"]["requestBody"]["content"]["application/json"].pop("examples"),
            "named request examples differ",
        ),
        (
            lambda schema: schema["components"]["schemas"]["AgentRepositoryStatusResponse"]["properties"]["status"].update(
                enum=["draft", "published"]
            ),
            "component AgentRepositoryStatusResponse.status enum",
        ),
    ],
)
def test_openapi_audit_rejects_semantic_contract_mutations(mutation, expected_fragment) -> None:
    schema = copy.deepcopy(build_openapi_schema())
    mutation(schema)

    assert any(expected_fragment in issue for issue in audit_schema(schema))


def test_openapi_audit_rejects_placeholder_examples_and_open_finite_values() -> None:
    placeholder_schema = copy.deepcopy(build_openapi_schema())
    placeholder_schema["paths"][RESPONSES_PATH]["post"]["requestBody"]["content"]["application/json"]["examples"]["strict_openai"]["value"]["input"] = "string"
    placeholder_issues = audit_schema(placeholder_schema)
    assert any("generic string placeholder" in issue for issue in placeholder_issues)

    open_enum_schema = copy.deepcopy(build_openapi_schema())
    status_parameter = next(
        parameter for parameter in open_enum_schema["paths"]["/api/agent-change-sets"]["get"]["parameters"] if parameter["name"] == "status"
    )
    status_parameter["schema"] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    enum_issues = audit_schema(open_enum_schema)
    assert any("parameter status enum" in issue for issue in enum_issues)


def test_openapi_audit_rejects_stale_example_registration() -> None:
    schema = copy.deepcopy(build_openapi_schema())
    schema["paths"]["/api/assets"]["post"].pop("requestBody")

    assert any("stale request-example registration" in issue for issue in audit_schema(schema))


def test_live_local_audit_deep_compares_components_and_operation_metadata() -> None:
    local = dict(build_openapi_schema())
    live = copy.deepcopy(local)
    live["components"]["schemas"]["AgentTargetedChatRequest"]["required"] = ["message"]
    live["paths"]["/api/sessions"]["get"]["description"] = "drifted"

    issues = audit_live_matches_local(live, local)

    assert any("/components/schemas/AgentTargetedChatRequest/required" in issue for issue in issues)
    assert any("/paths/~1api~1sessions/get/description" in issue for issue in issues)


def test_sse_registry_rejects_unregistered_events_and_accepts_sdk_family() -> None:
    require_registered_sse_event(CLAUDE_SDK_EVENTS_PATH, "claude.sdk.AssistantMessage")
    with pytest.raises(ValueError, match="Unregistered SSE event"):
        require_registered_sse_event(RESPONSES_PATH, "agentgov.undocumented")


def test_openapi_documents_exact_run_cancellation_contract() -> None:
    schema = build_openapi_schema()
    operation = schema["paths"]["/api/agent-runs/{run_id}/cancel"]["post"]

    assert "requestBody" not in operation
    assert {"200", "401", "404", "409", "422", "504"} <= set(operation["responses"])
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/AgentRunCancelResponse"}
    response = schema["components"]["schemas"]["AgentRunCancelResponse"]
    assert set(response["required"]) == {
        "run_id",
        "session_id",
        "turn_status",
        "cancelled",
    }
    assert response["properties"]["turn_status"]["enum"] == [
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    ]


def test_openapi_documents_ownerless_session_conflicts() -> None:
    schema = build_openapi_schema()

    for path in (
        "/v1/conversations/{conversation_id}/items",
        "/api/sessions/{session_id}/messages",
    ):
        responses = schema["paths"][path]["get"]["responses"]
        assert "409" in responses
        assert "500" not in responses


def test_openapi_documents_expected_domain_error_statuses():
    schema = build_openapi_schema()

    for path, method, operation in operation_items(schema):
        responses = operation["responses"]
        for status_code in expected_error_statuses(path, method, operation):
            assert str(status_code) in responses, f"{method.upper()} {path} missing {status_code}"


def test_openapi_documents_agent_test_domain_errors() -> None:
    schema = build_openapi_schema()

    create_responses = schema["paths"]["/api/agent-test-runs"]["post"]["responses"]
    change_set_responses = schema["paths"]["/api/agent-change-sets/{change_set_id}/test-runs"]["post"]["responses"]
    cancel_responses = schema["paths"]["/api/agent-test-runs/{test_run_id}/cancel"]["post"]["responses"]
    session_responses = schema["paths"]["/api/agent-test-sessions"]["post"]["responses"]
    assert {"400", "401", "404", "409", "422"} <= set(create_responses)
    assert {"400", "401", "404", "409", "422"} <= set(change_set_responses)
    assert {"400", "401", "404", "409"} <= set(cancel_responses)
    assert {"400", "401", "409", "422"} <= set(session_responses)


def test_openapi_documents_feedback_case_unknown_typed_source() -> None:
    schema = build_openapi_schema()

    responses = schema["paths"]["/api/feedback-cases"]["post"]["responses"]
    assert {"400", "404", "409", "422"} <= set(responses)


def test_openapi_requires_non_empty_typed_feedback_case_sources() -> None:
    schema = build_openapi_schema()
    request_schema = schema["components"]["schemas"]["FeedbackCaseCreateRequest"]
    source_refs = request_schema["properties"]["source_refs"]

    assert "source_refs" in request_schema["required"]
    assert source_refs["minItems"] == 1
    assert source_refs["items"] == {"$ref": "#/components/schemas/FeedbackSourceRef"}
    assert request_schema["additionalProperties"] is False
    source_ref_schema = schema["components"]["schemas"]["FeedbackSourceRef"]
    assert source_ref_schema["additionalProperties"] is False
    assert source_ref_schema["properties"]["source_id"]["minLength"] == 1


def test_openapi_success_responses_do_not_have_empty_json_schema():
    schema = build_openapi_schema()

    for path, method, operation in operation_items(schema):
        for status_code, response in operation["responses"].items():
            if not status_code.startswith("2"):
                continue
            content = response.get("content", {})
            json_media = content.get("application/json") if isinstance(content, dict) else None
            assert not (isinstance(json_media, dict) and json_media.get("schema") == {}), f"{method.upper()} {path} {status_code} has empty JSON schema"
