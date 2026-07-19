import importlib
import json
import sys

from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID

from business_agent_test_utils import create_test_business_agent_workspace


def _load_app(monkeypatch, tmp_path, *, api_key=""):
    root = tmp_path / "docker" / "volume"
    data = root / "data"
    governor_workspace = root / "governor-workspace"
    governor_root = root / "claude-roots" / "governor"
    agent_worktrees = data / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "version" / "worktrees"
    release_archives = data / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "version" / "releases"
    for path in (
        data,
        governor_workspace,
        governor_root / ".claude",
        agent_worktrees,
        release_archives,
    ):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("RUNTIME_CONTAINER", "0")
    monkeypatch.setenv("RUNTIME_VOLUME_MODE", "local-debug")
    default_ws = data / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "workspace"
    create_test_business_agent_workspace(default_ws, agent_id=DEFAULT_BUSINESS_AGENT_ID, name="Security Operations Expert")
    main_ws = data / "business-agents" / "main-agent" / "workspace"
    create_test_business_agent_workspace(main_ws, agent_id="main-agent", name="Main Agent")
    main_ws.joinpath("CLAUDE.md").write_text("原始规则\n", encoding="utf-8")
    main_ws.joinpath(".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sec-ops-data": {
                        "type": "http",
                        "url": "http://localhost:58001/mcp",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_RUNTIME_VOLUME_ROOT", str(root))
    monkeypatch.setenv("HOST_DATA_MOUNT", str(data))
    monkeypatch.setenv("HOST_GOVERNOR_WORKSPACE_MOUNT", str(governor_workspace))
    monkeypatch.setenv("HOST_GOVERNOR_CLAUDE_ROOT_MOUNT", str(governor_root))
    monkeypatch.setenv("GOVERNOR_WORKSPACE_DIR", str(governor_workspace))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("GOVERNOR_CLAUDE_ROOT", str(governor_root))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("MODEL_PROVIDER_API_KEY", "")
    monkeypatch.setenv("API_KEY", api_key)
    monkeypatch.delenv("RESPONSE_ORCHESTRATOR_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_GIT_REPOSITORY_DIR", str(default_ws))
    monkeypatch.setenv("AGENT_GIT_WORKTREES_DIR", str(agent_worktrees))
    monkeypatch.setenv("AGENT_RELEASE_ARCHIVES_DIR", str(release_archives))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    module = importlib.reload(sys.modules["app.main"]) if "app.main" in sys.modules else importlib.import_module("app.main")
    _register_business_agents(module)
    return module


def _register_business_agents(module) -> None:
    """把磁盘上的业务 Agent Workspace 登记进注册表。

    所有业务 Agent 都必须在注册表里才能运行。生产由 lifespan 的 sync 完成；不经 TestClient 的用例（直接调 runtime）
    不会触发 lifespan，因此夹具在此补上，与生产语义一致。
    """

    from app.runtime.agent_profiles import build_profiles, discover_business_agents

    settings = module.settings
    profiles = build_profiles(settings)
    for profile in discover_business_agents(settings):
        profiles.setdefault(profile.name, profile)
    module.agent_registry_store.sync_business_agents(profiles)
