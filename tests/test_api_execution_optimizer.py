import importlib
import sys


def _load_app(monkeypatch, tmp_path, *, api_key=""):
    root = tmp_path / "docker" / "volume"
    workspace = root / "main-workspace"
    data = root / "data"
    claude_root = root / "claude-roots" / "main"
    governor_workspace = root / "governor-workspace"
    governor_root = root / "claude-roots" / "governor"
    agent_worktrees = data / "business-agents" / "main-agent" / "version" / "worktrees"
    release_archives = data / "business-agents" / "main-agent" / "version" / "releases"
    for path in (
        workspace,
        data,
        claude_root / ".claude",
        governor_workspace,
        governor_root / ".claude",
        agent_worktrees,
        release_archives,
    ):
        path.mkdir(parents=True, exist_ok=True)

    main_ws = data / "business-agents" / "main-agent" / "workspace"
    main_ws.mkdir(parents=True, exist_ok=True)
    main_ws.joinpath("CLAUDE.md").write_text("原始规则\n", encoding="utf-8")

    monkeypatch.setenv("RUNTIME_CONTAINER", "0")
    monkeypatch.setenv("RUNTIME_VOLUME_MODE", "local-debug")
    monkeypatch.setenv("HOST_RUNTIME_VOLUME_ROOT", str(root))
    monkeypatch.setenv("HOST_DATA_MOUNT", str(data))
    monkeypatch.setenv("HOST_GOVERNOR_WORKSPACE_MOUNT", str(governor_workspace))
    monkeypatch.setenv("HOST_GOVERNOR_CLAUDE_ROOT_MOUNT", str(governor_root))
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
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.setenv("AGENT_GIT_REPOSITORY_DIR", str(main_ws))
    monkeypatch.setenv("AGENT_GIT_WORKTREES_DIR", str(agent_worktrees))
    monkeypatch.setenv("AGENT_RELEASE_ARCHIVES_DIR", str(release_archives))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    if "app.main" in sys.modules:
        return importlib.reload(sys.modules["app.main"])
    return importlib.import_module("app.main")
