from pathlib import Path


def test_import_app(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "docker" / "runtime-template" / "main-workspace"
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("MAIN_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    claude_root = runtime_root / "claude-roots" / "main"
    monkeypatch.setenv("CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("MAIN_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_root / ".claude"))

    import app.main

    paths = {route.path for route in app.main.app.routes}
    assert "/api/feedback-cases/{feedback_case_id}/optimization-plan" in paths
    assert "/api/feedback-cases/{feedback_case_id}/proposal-jobs" not in paths
    assert "/api/optimization-proposals/{proposal_id}" not in paths
    assert "/api/agent-repository/current" in paths
    assert "/api/agent-change-sets/{change_set_id}/publish" in paths
    assert "/api/agent-versions/main/current" not in paths
    assert "/api/feedback-cases/{feedback_case_id}/attribution-jobs/regenerate" in paths
    assert "/api/feedback" not in paths
    assert "/api/feedback/events" not in paths
    assert "/api/feedback/attributions/{attribution_id}/review" not in paths
