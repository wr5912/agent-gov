from __future__ import annotations

from pathlib import Path

from app.routers.agent_config_files import create_agent_config_files_router
from app.routers.catalog import create_catalog_router
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _test_app(tmp_path: Path) -> tuple[TestClient, AppSettings, AgentRegistryStore, LocalSessionStore]:
    data_dir = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(_env_file=None, DATA_DIR=data_dir)
    session_factory = make_session_factory(runtime_db_path_from_data_dir(data_dir))
    registry = AgentRegistryStore(session_factory)
    session_store = LocalSessionStore(settings.session_dir)
    app = FastAPI()
    app.include_router(
        create_agent_config_files_router(
            settings=settings,
            agent_registry_store=registry,
            session_store=session_store,
            require_api_key=lambda: None,
        )
    )
    app.include_router(create_catalog_router(settings=settings, agent_registry_store=registry, require_api_key=lambda: None))
    return TestClient(app), settings, registry, session_store


def _register_agent(settings: AppSettings, registry: AgentRegistryStore, agent_id: str) -> Path:
    workspace = business_agent_layout(settings.data_dir, agent_id).workspace
    workspace.mkdir(parents=True, exist_ok=True)
    registry.create_business_agent(name=agent_id, agent_id=agent_id, workspace_dir=str(workspace))
    return workspace


def test_agent_config_file_updates_mcp_json_and_invalidates_sdk_resume(tmp_path: Path) -> None:
    client, settings, registry, session_store = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, "main-agent")
    target = workspace / ".mcp.json"
    target.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    session = session_store.create()
    session.agent_id = "main-agent"
    session.sdk_session_id = "sdk-session-old"
    session_store.save(session)

    read_response = client.get("/api/agent-config-file", params={"agent_id": "main-agent", "path": ".mcp.json"})
    assert read_response.status_code == 200
    current = read_response.json()
    assert current["content"] == '{"mcpServers": {}}\n'

    updated_content = '{"mcpServers": {"demo": {"command": "node", "args": ["server.js"]}}}\n'
    update_response = client.put(
        "/api/agent-config-file",
        params={"agent_id": "main-agent", "path": ".mcp.json"},
        json={
            "content": updated_content,
            "expected_sha256": current["sha256"],
            "session_id": session.session_id,
        },
    )

    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["content"] == updated_content
    assert updated["sdk_session_invalidated"] is True
    assert target.read_text(encoding="utf-8") == updated_content
    assert session_store.get(session.session_id).sdk_session_id is None


def test_agent_config_file_rejects_invalid_json_and_stale_sha(tmp_path: Path) -> None:
    client, settings, registry, _ = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, "main-agent")
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

    invalid = client.put(
        "/api/agent-config-file",
        params={"agent_id": "main-agent", "path": ".mcp.json"},
        json={"content": "{", "expected_sha256": None},
    )
    assert invalid.status_code == 422

    wrong_shape = client.put(
        "/api/agent-config-file",
        params={"agent_id": "main-agent", "path": ".mcp.json"},
        json={"content": "[]", "expected_sha256": None},
    )
    assert wrong_shape.status_code == 422

    stale = client.put(
        "/api/agent-config-file",
        params={"agent_id": "main-agent", "path": ".mcp.json"},
        json={"content": '{"mcpServers": {}}\n', "expected_sha256": "not-current"},
    )
    assert stale.status_code == 409


def test_agent_config_file_rejects_uneditable_paths_and_unknown_agents(tmp_path: Path) -> None:
    client, settings, registry, _ = _test_app(tmp_path)
    _register_agent(settings, registry, "main-agent")

    uneditable = client.get("/api/agent-config-file", params={"agent_id": "main-agent", "path": "CLAUDE.md"})
    assert uneditable.status_code == 422

    hostile_agent = client.get("/api/agent-config-file", params={"agent_id": "../escape", "path": ".mcp.json"})
    assert hostile_agent.status_code == 422

    missing_agent = client.get("/api/agent-config-file", params={"agent_id": "missing-agent", "path": ".mcp.json"})
    assert missing_agent.status_code == 404


def test_catalog_router_discovers_agent_scoped_project_assets(tmp_path: Path) -> None:
    client, settings, registry, _ = _test_app(tmp_path)
    main_workspace = _register_agent(settings, registry, "main-agent")
    disposal_workspace = _register_agent(settings, registry, "response-disposal")
    _write_agent_asset(main_workspace, "main-subagent", "main-skill")
    _write_agent_asset(disposal_workspace, "disposal-subagent", "disposal-skill")

    main_agents = client.get("/api/agents", params={"agent_id": "main-agent"})
    disposal_agents = client.get("/api/agents", params={"agent_id": "response-disposal"})
    disposal_skills = client.get("/api/skills", params={"agent_id": "response-disposal"})

    assert main_agents.status_code == 200
    assert disposal_agents.status_code == 200
    assert disposal_skills.status_code == 200
    assert [item["name"] for item in main_agents.json()] == ["main-subagent"]
    assert [item["name"] for item in disposal_agents.json()] == ["disposal-subagent"]
    assert [item["name"] for item in disposal_skills.json()] == ["disposal-skill"]


def _write_agent_asset(workspace: Path, agent_name: str, skill_name: str) -> None:
    agents_dir = workspace / ".claude" / "agents"
    skills_dir = workspace / ".claude" / "skills" / skill_name
    agents_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_name}.md").write_text(
        f"---\nname: {agent_name}\ndescription: test agent\n---\nPrompt\n",
        encoding="utf-8",
    )
    (skills_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test skill\n---\nInstructions\n",
        encoding="utf-8",
    )
