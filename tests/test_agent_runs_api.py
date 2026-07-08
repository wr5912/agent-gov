import importlib
import sys

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
    monkeypatch.setenv("MODEL_PROVIDER_API_KEY", "")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    if "app.main" in sys.modules:
        return importlib.reload(sys.modules["app.main"])
    return importlib.import_module("app.main")


def test_agent_runs_can_include_messages_for_playground_session_restore(monkeypatch, tmp_path):
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
