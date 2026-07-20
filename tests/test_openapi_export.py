import json
import os
import subprocess
import sys
from pathlib import Path

from app.openapi_contract import expected_error_statuses, operation_items
from scripts.audit_openapi_contract import audit_schema
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
        "/api/agent-registry/{agent_id}/test-schedule",
        "/api/agent-registry/{agent_id}/test-schedule/events",
        "/api/agent-test-assets",
        "/api/agent-test-runs",
        "/api/agent-test-runs/history",
        "/api/agent-change-sets/{change_set_id}/test-runs",
        "/api/agent-test-runs/{test_run_id}",
        "/api/agent-test-runs/{test_run_id}/cancel",
        "/api/agent-test-sessions",
        "/api/agent-test-sessions/{test_session_id}/messages",
        "/api/langfuse/traces/{trace_id}",
        "/api/agent-config-file",
        "/api/agent-change-sets/{change_set_id}/publish",
        "/api/agent-releases/{release_id}/restore",
        "/api/claude-user-input-requests/{request_id}/decision",
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

    agent_config_file = schema["paths"]["/api/agent-config-file"]
    assert {"get", "put"} <= set(agent_config_file)
    agent_config_update = schema["components"]["schemas"]["AgentConfigFileUpdateResponse"]
    assert "sdk_session_invalidated" in agent_config_update["properties"]


def test_openapi_contract_audit_passes_current_schema():
    schema = dict(build_openapi_schema())
    expected_version = Path("VERSION").read_text(encoding="utf-8").strip()

    assert audit_schema(schema, expected_version=expected_version) == []


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

    responses_content = schema["paths"]["/v1/responses"]["post"]["responses"]["200"]["content"]
    assert {"application/json", "text/event-stream"} <= set(responses_content)
    assert responses_content["application/json"]["schema"] == {"$ref": "#/components/schemas/ResponseObject"}


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
