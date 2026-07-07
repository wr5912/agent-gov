import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.export_openapi import (
    CONTAINER_RUNTIME_VOLUME_ROOT,
    LOCAL_DEBUG_RUNTIME_VOLUME_ROOT,
    _apply_local_defaults,
    _local_default_volume_root,
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
        "/api/improvements/{improvement_id}/regression-assessment/generate",
        "/api/langfuse/traces/{trace_id}",
        "/api/agent-config-file",
        "/api/agent-change-sets/{change_set_id}/publish",
        "/api/agent-releases/{release_id}/restore",
        "/api/claude-user-input-requests/{request_id}/decision",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/responses/{response_id}",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}",
        "/v1/conversations/{conversation_id}/items",
    }
    assert current_paths <= set(schema["paths"])

    legacy_paths = {
        "/api/feedback-optimization-batches",
        "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
        "/api/optimization-proposals",
        "/api/optimization-tasks/{task_id}/execution-jobs",
    }
    assert set(schema["paths"]).isdisjoint(legacy_paths)

    for schema_name in (
        "FeedbackOptimizationBatchResponse",
        "OptimizationTaskResponse",
        "OptimizationProposalResponse",
        "ExternalGovernanceItemResponse",
        "RegressionPlanResponse",
    ):
        assert schema_name not in schema["components"]["schemas"]

    attribution = schema["components"]["schemas"]["AttributionResponse"]
    optimization = schema["components"]["schemas"]["OptimizationPlanResponse"]
    execution = schema["components"]["schemas"]["ExecutionResponse"]
    regression = schema["components"]["schemas"]["RegressionAssessmentResponse"]
    for component in (attribution, optimization, execution, regression):
        assert "generation_trace_id" in component["properties"]
        assert "generation_trace_url" in component["properties"]

    agent_config_file = schema["paths"]["/api/agent-config-file"]
    assert {"get", "put"} <= set(agent_config_file)
    agent_config_update = schema["components"]["schemas"]["AgentConfigFileUpdateResponse"]
    assert "sdk_session_invalidated" in agent_config_update["properties"]
