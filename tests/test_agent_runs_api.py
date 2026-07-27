import importlib
import sys

from app.runtime.runtime_db import SessionRecordModel, SessionTurnIntentModel
from fastapi.testclient import TestClient


def _load_app(monkeypatch, tmp_path):
    root = tmp_path / "runtime"
    workspace = root / "main-workspace"
    governor_workspace = root / "governor-workspace"
    data = root / "data"
    claude_root = root / "claude-roots" / "main"
    governor_root = root / "claude-roots" / "governor"
    for path in (
        workspace,
        governor_workspace,
        data,
        claude_root / ".claude",
        governor_root / ".claude",
    ):
        path.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("CLAUDE.md").write_text("测试 workspace\n", encoding="utf-8")

    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("MAIN_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("GOVERNOR_WORKSPACE_DIR", str(governor_workspace))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("MAIN_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("GOVERNOR_CLAUDE_ROOT", str(governor_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_root / ".claude"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("MODEL_PROVIDER_BACKEND", "anthropic_compatible")
    monkeypatch.setenv("MODEL_PROVIDER_API_URL", "http://model-provider.test")
    monkeypatch.setenv("MODEL_PROVIDER_API_KEY", "test-provider-key")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    if "app.main" in sys.modules:
        return importlib.reload(sys.modules["app.main"])
    return importlib.import_module("app.main")


def test_agent_runs_include_messages_is_an_explicit_debug_projection(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    module.feedback_store.record_run(
        {
            "run_id": "run-history",
            "session_id": "sess-history",
            "sdk_session_id": "sdk-history",
            "agent_version_id": "v-history",
            "langfuse_trace_id": "trace-history",
            "langfuse_trace_url": "http://langfuse-web:3000/project/agent-gov/traces/trace-history",
            "message": "请说明当前 workspace 中有哪些 subagents 和 skills。",
            "answer_summary": "当前 Workspace 配置概览",
            "messages": [
                {"event": "AssistantMessage", "content": [{"text": "## 当前 Workspace 配置概览\n\n- subagents: 默认 Agent"}]},
                {"event": "ResultMessage", "result": "完成"},
            ],
            "agent_activity": {"tool_calls": [], "tool_results": [], "tool_names": []},
            "created_at": "2026-06-20T09:44:31+00:00",
            "completed_at": "2026-06-20T09:44:47+00:00",
        }
    )

    with TestClient(module.app) as client:
        default_response = client.get("/api/agent-runs", params={"session_id": "sess-history"})
        assert default_response.status_code == 200
        default_payload = default_response.json()[0]
        assert default_payload.get("messages") in (None, [])
        assert default_payload.get("answer") in (None, "")
        assert default_payload["langfuse_trace_id"] == "trace-history"
        assert default_payload["langfuse_trace_url"].endswith("/project/agent-gov/traces/trace-history")

        restore_response = client.get(
            "/api/agent-runs",
            params={"session_id": "sess-history", "include_messages": True},
        )
        assert restore_response.status_code == 200
        restore_payload = restore_response.json()[0]
        assert restore_payload["messages"][0]["event"] == "AssistantMessage"
        assert restore_payload["answer"].startswith("## 当前 Workspace 配置概览")
        assert restore_payload["langfuse_trace_id"] == "trace-history"


def test_agent_run_trace_is_refresh_safe_and_semantic(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    module.feedback_store.record_run(
        {
            "run_id": "run-trace",
            "session_id": "sess-trace",
            "turn_status": "failed",
            "turn_index": 2,
            "turn_error": {"type": "ProviderError", "message": "offline"},
            "errors": ["ProviderError: offline"],
            "messages": [
                {
                    "event": "SystemMessage:thinking_tokens",
                    "subtype": "thinking_tokens",
                    "data": {"tokens": 128},
                },
                {
                    "event": "AssistantMessage",
                    "content": [
                        {"thinking": "完整思考", "signature": "opaque"},
                        {"name": "Read", "id": "tool-1", "input": {"file_path": "AGENTS.md"}},
                    ],
                },
                {"event": "ResultMessage:error", "subtype": "error", "is_error": True},
            ],
            "created_at": "2026-07-27T01:00:00+00:00",
            "completed_at": "2026-07-27T01:00:01+00:00",
        }
    )

    with TestClient(module.app) as client:
        response = client.get("/api/agent-runs/run-trace/trace")
        missing = client.get("/api/agent-runs/missing/trace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["turn_status"] == "failed"
    assert payload["turn_error"]["type"] == "ProviderError"
    assert payload["completeness"] == "complete"
    assert [event["kind"] for event in payload["events"]] == ["thinking", "tool_use", "result"]
    assert payload["events"][0]["payload"]["thinking"] == "完整思考"
    assert "signature" not in payload["events"][0]["payload"]
    assert missing.status_code == 404


def test_agent_run_trace_reports_unavailable_for_legacy_run_without_messages(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    module.feedback_store.record_run(
        {
            "run_id": "run-legacy",
            "session_id": "sess-legacy",
            "created_at": "2026-07-27T01:00:00+00:00",
        }
    )
    with module.feedback_store.Session.begin() as db:
        db.add(
            SessionRecordModel(
                session_id="sess-legacy",
                sdk_session_id=None,
                agent_id="legacy-agent",
                created_at="2026-07-27T01:00:00+00:00",
                updated_at="2026-07-27T01:00:01+00:00",
                turns=2,
                metadata_json={},
            )
        )
        db.add(
            SessionTurnIntentModel(
                run_id="run-legacy",
                session_id="sess-legacy",
                agent_id="legacy-agent",
                source_sdk_session_id=None,
                attempted_sdk_session_id="sdk-legacy",
                sdk_project_key="legacy-project",
                base_turns=2,
                status="interrupted",
                request_json={},
                error_json={"type": "RuntimeInterrupted", "message": "legacy recovery"},
                created_at="2026-07-27T01:00:00+00:00",
                updated_at="2026-07-27T01:00:01+00:00",
                completed_at="2026-07-27T01:00:01+00:00",
            )
        )

    with TestClient(module.app) as client:
        response = client.get("/api/agent-runs/run-legacy/trace")

    assert response.status_code == 200
    assert response.json()["completeness"] == "unavailable"
    assert response.json()["turn_status"] == "interrupted"
    assert response.json()["turn_index"] == 3
    assert response.json()["turn_error"]["type"] == "RuntimeInterrupted"
    assert response.json().get("events", []) == []
