from app.routers.config import create_config_router
from app.runtime.config_mapping import RUNTIME_CONTRACT, build_config_mapping
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_config_mapping_uses_agentscope_harness_paths(tmp_path) -> None:
    data = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=data,
        HOST_DATA_MOUNT="./volume-agent-gov/data",
        AGENTSCOPE_RUNTIME_URL="http://runtime.internal:8090",
    )
    workspace = settings.default_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENT.md").write_text("# Project", encoding="utf-8")
    (workspace / "agent.yaml").write_text("agent:\n  id: security-operations-expert\n", encoding="utf-8")

    response = build_config_mapping(settings, expose_host_mount=True)
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert response.agent_id == DEFAULT_BUSINESS_AGENT_ID
    assert response.runtime_url == "http://runtime.internal:8090"
    assert response.runtime_contract == RUNTIME_CONTRACT
    assert response.workspace == str(workspace)
    assert by_kind[("harness", "manifest")].container_path == str(workspace / "agent.yaml")
    assert by_kind[("harness", "instructions")].host_mount == (
        f"volume-agent-gov/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/workspace/AGENT.md"
    )
    assert by_kind[("harness", "instructions")].exists is True
    assert by_kind[("governance", "candidate-worktrees")].container_path == str(
        data / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "version" / "worktrees"
    )


def test_config_mapping_declares_runtime_and_governance_semantics(tmp_path) -> None:
    settings = AppSettings(_env_file=None, DATA_DIR=tmp_path / "data")

    response = build_config_mapping(settings)

    assert [(item.scope, item.kind) for item in response.mappings] == [
        ("harness", "manifest"),
        ("harness", "instructions"),
        ("harness", "skills"),
        ("harness", "mcp"),
        ("harness", "subagents"),
        ("harness", "tests"),
        ("governance", "candidate-worktrees"),
        ("governance", "release-archives"),
    ]
    runtime_items = response.mappings[:5]
    assert all(item.loaded_by_default for item in runtime_items)
    assert all(item.display_group == "harness" and item.git_policy == "tracked" for item in response.mappings[:6])
    assert response.mappings[5].load_semantics == "governance_only"
    assert all(not item.loaded_by_default and not item.safe_to_edit for item in response.mappings[6:])


def test_config_mapping_is_agent_scoped_and_hides_host_mounts_by_default(tmp_path) -> None:
    data = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(_env_file=None, DATA_DIR=data, HOST_DATA_MOUNT="./volume-agent-gov/data")
    workspace = data / "business-agents" / "response-disposal" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENT.md").write_text("# Response Disposal", encoding="utf-8")

    response = build_config_mapping(settings, agent_id="response-disposal")
    by_kind = {(item.scope, item.kind): item for item in response.mappings}

    assert response.agent_id == "response-disposal"
    assert all(item.host_mount is None for item in response.mappings)
    assert by_kind[("harness", "instructions")].container_path == str(workspace / "AGENT.md")
    assert by_kind[("governance", "candidate-worktrees")].display_group == "versioning"


def test_config_mapping_router_requires_registered_agent(tmp_path) -> None:
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
    missing = client.get("/api/config?agent_id=missing-agent")

    assert response.status_code == 200
    assert response.json()["agent_id"] == "response-disposal"
    assert missing.status_code == 404
