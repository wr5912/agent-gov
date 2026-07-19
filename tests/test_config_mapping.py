from app.routers.config import create_config_router
from app.runtime.config_mapping import build_config_mapping
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


class ProjectOnlySettings(AppSettings):
    @property
    def setting_sources(self) -> list[str]:
        return ["project"]


def test_config_mapping_uses_native_claude_code_paths(tmp_path):
    # 默认业务 Agent 的 workspace/claude-root 由 data_dir 派生，host 映射经 data 挂载。
    data = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=data,
        HOST_DATA_MOUNT="./volume-agent-gov/data",
    )
    workspace = settings.default_workspace_dir
    claude_root = settings.default_claude_root
    claude_home = settings.claude_home
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text("# Project", encoding="utf-8")
    (claude_root / ".claude.json").write_text("{}", encoding="utf-8")

    response = build_config_mapping(settings, expose_host_mount=True)
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert response.agent_id == DEFAULT_BUSINESS_AGENT_ID
    assert response.claude_config_mode == "native"
    assert response.claude_root == str(claude_root)
    assert response.claude_config_dir is None
    assert response.claude_global_config_file == str(claude_root / ".claude.json")
    assert response.setting_sources_effective == ["project"]
    assert by_kind[("global", "state")].host_mount == f"volume-agent-gov/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/claude-root/.claude.json"
    assert by_kind[("project", "instructions")].host_mount == f"volume-agent-gov/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/workspace/CLAUDE.md"
    assert by_kind[("global", "state")].exists is True
    assert by_kind[("project", "instructions")].display_group == "agent_project_config"
    assert by_kind[("project", "instructions")].load_semantics == "claude_loaded"
    assert by_kind[("project", "instructions")].safe_to_edit is True
    assert by_kind[("runtime", "agent-change-set-worktrees")].container_path == str(
        data / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "version" / "worktrees"
    )


def test_config_mapping_loaded_flags_follow_sdk_setting_sources(tmp_path):
    data = tmp_path / "volume-agent-gov" / "data"
    settings = ProjectOnlySettings(_env_file=None, DATA_DIR=data)

    response = build_config_mapping(settings)
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert [(item.scope, item.kind) for item in response.mappings[:18]] == [
        ("user", "settings"),
        ("user", "instructions"),
        ("user", "skills"),
        ("user", "agents"),
        ("user", "commands"),
        ("user", "output-styles"),
        ("global", "state"),
        ("project", "instructions"),
        ("local", "instructions"),
        ("project", "mcp"),
        ("project", "worktree-include"),
        ("project", "settings"),
        ("local", "settings"),
        ("project", "rules"),
        ("project", "skills"),
        ("project", "commands"),
        ("project", "agents"),
        ("project", "output-styles"),
    ]
    assert response.setting_sources_effective == ["project"]
    assert by_kind[("project", "instructions")].loaded_by_default is True
    assert by_kind[("project", "mcp")].loaded_by_default is True
    assert by_kind[("project", "skills")].loaded_by_default is True
    assert by_kind[("user", "settings")].loaded_by_default is False
    assert by_kind[("local", "settings")].loaded_by_default is False
    assert by_kind[("runtime", "agent-git-repository")].loaded_by_default is False
    assert all(
        (item.loaded_by_default, item.load_semantics, item.display_group, item.safe_to_edit, item.git_policy)
        == (False, "claude_optional", "agent_user_state", False, "ignored")
        for item in response.mappings[:6]
    )
    project_items = [item for item in response.mappings[7:18] if item.scope == "project" and item.kind != "worktree-include"]
    assert all(
        (item.loaded_by_default, item.load_semantics, item.display_group, item.safe_to_edit, item.git_policy)
        == (True, "claude_loaded", "agent_project_config", True, "tracked")
        for item in project_items
    )
    local_items = [item for item in response.mappings[7:18] if item.scope == "local"]
    assert all(
        (item.loaded_by_default, item.load_semantics, item.display_group, item.safe_to_edit, item.git_policy)
        == (False, "claude_optional", "agent_user_state", False, "ignored")
        for item in local_items
    )
    worktree = by_kind[("project", "worktree-include")]
    assert (worktree.loaded_by_default, worktree.load_semantics, worktree.display_group, worktree.safe_to_edit, worktree.git_policy, worktree.notes) == (
        False,
        "not_applicable",
        "agent_project_config",
        True,
        "tracked",
        "Used by Claude Code worktree creation to copy selected gitignored files.",
    )


def test_config_mapping_is_agent_scoped_and_hides_host_mounts_by_default(tmp_path):
    data = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=data,
        HOST_DATA_MOUNT="./volume-agent-gov/data",
    )
    workspace = data / "business-agents" / "response-disposal" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text("# Response Disposal", encoding="utf-8")

    response = build_config_mapping(settings, agent_id="response-disposal")
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert response.agent_id == "response-disposal"
    assert response.claude_root == str(data / "business-agents" / "response-disposal" / "claude-root")
    assert all(item.host_mount is None for item in response.mappings)
    assert by_kind[("project", "instructions")].container_path == str(workspace / "CLAUDE.md")
    assert by_kind[("runtime", "agent-git-repository")].display_group == "versioning_runtime"
    assert by_kind[("runtime", "agent-change-set-worktrees")].container_path == str(data / "business-agents" / "response-disposal" / "version" / "worktrees")


def test_config_mapping_router_requires_registered_agent(tmp_path):
    data = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(_env_file=None, DATA_DIR=data)
    session_factory = make_session_factory(data / "runtime.sqlite3")
    registry = AgentRegistryStore(session_factory)
    workspace = data / "business-agents" / "response-disposal" / "workspace"
    workspace.mkdir(parents=True)
    registry.create_business_agent(name="Response Disposal", agent_id="response-disposal", workspace_dir=str(workspace))
    app = FastAPI()
    app.include_router(create_config_router(settings=settings, agent_registry_store=registry, require_api_key=lambda: None))
    client = TestClient(app)

    response = client.get("/api/config?agent_id=response-disposal")
    assert response.status_code == 200
    assert response.json()["agent_id"] == "response-disposal"

    missing = client.get("/api/config?agent_id=missing-agent")
    assert missing.status_code == 404
