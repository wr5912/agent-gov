from pathlib import Path


def test_import_app(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WORKSPACE_DIR", str(root / "docker" / "volume" / "main-workspace"))
    monkeypatch.setenv("MAIN_WORKSPACE_DIR", str(root / "docker" / "volume" / "main-workspace"))
    monkeypatch.setenv("DATA_DIR", str(root / "docker" / "volume" / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    claude_root = root / "docker" / "volume" / "claude-roots" / "main"
    monkeypatch.setenv("CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("MAIN_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_root / ".claude"))

    import app.main

    paths = {route.path for route in app.main.app.routes}
    assert "/api/optimization-proposals/{proposal_id}" in paths
    assert "/api/agent-versions/main/current" in paths
    assert "/api/feedback" not in paths
    assert "/api/feedback/events" not in paths
    assert "/api/feedback/attributions/{attribution_id}/review" not in paths
