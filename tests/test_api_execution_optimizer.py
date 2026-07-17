import importlib
import json
import shutil
import sys
from pathlib import Path

from app.runtime.business_agent_workspace import seed_business_agent_workspace

_REPO_SEED_AGENTS = Path(__file__).resolve().parents[1] / "docker" / "runtime-volume-seeds" / "data" / "business-agents"


def _copy_repo_seeds_into_catalog(catalog_agents: Path) -> None:
    """把仓库声明的 seed 按字节复制进运行态 catalog。

    生产由 bootstrap 的一级同步完成（runtime-init 容器）。用例若只放合成内容，跨 ID 实例化的
    字节比对就会比一份该 Agent 根本没读过的东西。
    """

    if not _REPO_SEED_AGENTS.is_dir():
        return
    for source in sorted(_REPO_SEED_AGENTS.iterdir()):
        workspace = source / "workspace"
        if not workspace.is_dir() or source.is_symlink():
            continue
        destination = catalog_agents / source.name / "workspace"
        if destination.exists():
            continue
        shutil.copytree(workspace, destination, symlinks=False)


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

    monkeypatch.setenv("RUNTIME_CONTAINER", "0")
    monkeypatch.setenv("RUNTIME_VOLUME_MODE", "local-debug")
    main_ws = data / "business-agents" / "main-agent" / "workspace"
    seed_business_agent_workspace(main_ws, agent_id="main-agent", name="Main Agent")
    # 运行态 seed catalog：生产由 bootstrap 从仓库出生配置填充（runtime-init 容器）。它是
    # 「当前运行态声明了哪些 seed」的真相源——origin 判定与跨 ID 实例化都读它，因此夹具必须
    # 建出这一层，否则测到的是一个 catalog 为空的、生产中不存在的状态。
    catalog_agents = data / "seed-catalog" / "data" / "business-agents"
    seed_business_agent_workspace(catalog_agents / "main-agent" / "workspace", agent_id="main-agent", name="Main Agent")
    # 仓库声明的 seed 按字节复制进 catalog，与生产 bootstrap 的一级同步一致——跨 ID 实例化的
    # 用例要比对源 seed 的真实字节，用合成内容会让它比一份生产中不存在的东西。
    _copy_repo_seeds_into_catalog(catalog_agents)
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
    monkeypatch.delenv("RESPONSE_ORCHESTRATOR_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_GIT_REPOSITORY_DIR", str(main_ws))
    monkeypatch.setenv("AGENT_GIT_WORKTREES_DIR", str(agent_worktrees))
    monkeypatch.setenv("AGENT_RELEASE_ARCHIVES_DIR", str(release_archives))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    module = importlib.reload(sys.modules["app.main"]) if "app.main" in sys.modules else importlib.import_module("app.main")
    _register_seeded_business_agents(module)
    return module


def _register_seeded_business_agents(module) -> None:
    """把磁盘上已播种的业务 Agent 登记进注册表。

    main-agent 现在与其他业务 Agent 一样必须在注册表里才能运行——它不再有「预制 profile 直接
    跑、不查表」的豁免。生产由 lifespan 的 sync 完成；不经 TestClient 的用例（直接调 runtime）
    不会触发 lifespan，因此夹具在此补上，与生产语义一致。
    """

    from app.runtime.agent_profiles import build_profiles, discover_seeded_business_agents, seed_business_agent_ids

    settings = module.settings
    profiles = build_profiles(settings)
    for profile in discover_seeded_business_agents(settings):
        profiles.setdefault(profile.name, profile)
    module.agent_registry_store.sync_business_agents(profiles, seed_agent_ids=seed_business_agent_ids(settings))
