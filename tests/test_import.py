from pathlib import Path


def test_import_app(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WORKSPACE_DIR", str(root / "docker" / "volume" / "workspace"))
    monkeypatch.setenv("DATA_DIR", str(root / "docker" / "volume" / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    claude_root = root / "docker" / "volume" / "claude-root"
    monkeypatch.setenv("CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_root / ".claude"))

    import app.main  # noqa: F401
