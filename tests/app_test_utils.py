from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID

from business_agent_test_utils import create_test_business_agent_workspace


def load_test_app(
    monkeypatch,
    tmp_path,
    *,
    api_key: str = "",
    extra_agent_ids: Sequence[str] = (),
):
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
    default_workspace = data / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "workspace"
    create_test_business_agent_workspace(
        default_workspace,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        name="Security Operations Expert",
    )
    for agent_id in dict.fromkeys(extra_agent_ids):
        if agent_id == DEFAULT_BUSINESS_AGENT_ID:
            continue
        create_test_business_agent_workspace(
            data / "business-agents" / agent_id / "workspace",
            agent_id=agent_id,
            name=f"Test Business Agent {agent_id}",
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
    monkeypatch.setenv("AGENT_GIT_REPOSITORY_DIR", str(default_workspace))
    monkeypatch.setenv("AGENT_GIT_WORKTREES_DIR", str(agent_worktrees))
    monkeypatch.setenv("AGENT_RELEASE_ARCHIVES_DIR", str(release_archives))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    import app.runtime.settings as settings_module

    settings_module.get_settings.cache_clear()
    module = importlib.reload(sys.modules["app.main"]) if "app.main" in sys.modules else importlib.import_module("app.main")
    register_discovered_business_agents(module)
    return module


def register_discovered_business_agents(module) -> None:
    """Mirror lifespan registration for tests that call the app outside TestClient."""

    from app.runtime.agent_profiles import build_profiles, discover_business_agents

    profiles = build_profiles(module.settings)
    for profile in discover_business_agents(module.settings):
        profiles.setdefault(profile.name, profile)
    module.agent_registry_store.sync_business_agents(profiles)
