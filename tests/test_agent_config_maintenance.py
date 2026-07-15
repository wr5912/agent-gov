from __future__ import annotations

from app.routers.agent_config_files import create_agent_config_files_router
from app.runtime.agent_paths import business_agent_layout
from app.runtime.runtime_db import SessionRecordModel, make_session_factory, runtime_db_path_from_data_dir
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_config_update_is_fenced_by_same_agent_runtime_but_allows_other_agent(tmp_path) -> None:
    data_dir = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(_env_file=None, DATA_DIR=data_dir)
    factory = make_session_factory(runtime_db_path_from_data_dir(data_dir))
    registry = AgentRegistryStore(factory)
    for agent_id in ("agent-a", "agent-b"):
        workspace = business_agent_layout(data_dir, agent_id).workspace
        workspace.mkdir(parents=True, exist_ok=True)
        workspace.joinpath(".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
        registry.create_business_agent(name=agent_id, agent_id=agent_id, workspace_dir=str(workspace))
    with factory.begin() as db:
        db.add(
            SessionRecordModel(
                session_id="session-a",
                agent_id="agent-a",
                active_run_id="run-a",
                active_run_generation=1,
                active_run_expires_at="2099-01-01T00:00:00+00:00",
                metadata_json={},
            )
        )

    app = FastAPI()
    app.include_router(
        create_agent_config_files_router(
            settings=settings,
            agent_registry_store=registry,
            session_store=LocalSessionStore(settings.session_dir),
            require_api_key=lambda: None,
            version_maintenance=AgentVersionMaintenanceCoordinator(factory),
        )
    )
    client = TestClient(app)
    body = {"content": '{"mcpServers": {"demo": {"type": "http", "url": "https://example.invalid/mcp"}}}\n'}

    blocked = client.put(
        "/api/agent-config-file",
        params={"agent_id": "agent-a", "path": ".mcp.json"},
        json=body,
    )
    allowed = client.put(
        "/api/agent-config-file",
        params={"agent_id": "agent-b", "path": ".mcp.json"},
        json=body,
    )

    assert blocked.status_code == 409
    assert "active runtime turn" in blocked.json()["detail"]
    assert allowed.status_code == 200
    assert business_agent_layout(data_dir, "agent-a").workspace.joinpath(".mcp.json").read_text(encoding="utf-8") == '{"mcpServers": {}}\n'
