from __future__ import annotations

import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from app.routers.agent_config_files import create_agent_config_files_router
from app.routers.catalog import create_catalog_router
from app.runtime.agent_paths import business_agent_layout
from app.runtime.config_file_schemas import AgentConfigFileUpdateRequest
from app.runtime.errors import SessionConflictError
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services import agent_config_files as agent_config_files_module
from app.services.agent_config_files import AgentConfigFileError, AgentConfigFileService
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_AGENT_ID = "test-agent"


def _http_mcp_content(name: str) -> str:
    return f'{{"mcpServers": {{"{name}": {{"type": "http", "url": "https://{name}.invalid/mcp"}}}}}}\n'


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
    target.chmod(0o640)
    session = session_store.create()
    session.agent_id = "main-agent"
    session.sdk_session_id = "sdk-session-old"
    session_store.save(session)

    read_response = client.get("/api/agent-config-file", params={"agent_id": "main-agent", "path": ".mcp.json"})
    assert read_response.status_code == 200
    current = read_response.json()
    assert current["content"] == '{"mcpServers": {}}\n'

    updated_content = (
        '{"mcpServers": {'
        '"sec-ops-data": {"type": "http", "url": "http://host.docker.internal:58001/mcp"}, '
        '"demo": {"type": "http", "url": "http://demo.internal/mcp"}'
        "}}\n"
    )
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
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert session_store.get(session.session_id).sdk_session_id is None


def test_agent_config_file_rejects_invalid_json_and_stale_sha(tmp_path: Path) -> None:
    client, settings, registry, _ = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

    invalid = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
        json={"content": "{", "expected_sha256": None},
    )
    assert invalid.status_code == 422

    wrong_shape = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
        json={"content": "[]", "expected_sha256": None},
    )
    assert wrong_shape.status_code == 422

    stdio = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
        json={
            "content": '{"mcpServers": {"demo": {"type": "stdio", "command": "node"}}}',
            "expected_sha256": None,
        },
    )
    assert stdio.status_code == 422

    stale = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
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


def test_agent_config_file_rejects_workspace_and_target_symlinks(tmp_path: Path) -> None:
    client, settings, registry, _ = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside": true}\n', encoding="utf-8")
    (workspace / ".mcp.json").symlink_to(outside)

    target_symlink = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
        json={"content": '{"mcpServers": {}}\n'},
    )

    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    workspace_symlink = tmp_path / "workspace-link"
    workspace_symlink.symlink_to(real_workspace, target_is_directory=True)
    registry.create_business_agent(name="linked-agent", agent_id="linked-agent", workspace_dir=str(workspace_symlink))
    directory_symlink = client.get(
        "/api/agent-config-file",
        params={"agent_id": "linked-agent", "path": ".mcp.json"},
    )

    assert target_symlink.status_code == 409
    assert directory_symlink.status_code == 409
    assert outside.read_text(encoding="utf-8") == '{"outside": true}\n'


def test_agent_config_file_handles_directory_permission_and_cleans_failed_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, settings, registry, _ = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    target = workspace / ".mcp.json"
    original = '{"mcpServers": {}}\n'
    target.write_text(original, encoding="utf-8")

    workspace.chmod(0o500)
    try:
        denied = client.put(
            "/api/agent-config-file",
            params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
            json={"content": _http_mcp_content("denied")},
        )
    finally:
        workspace.chmod(0o700)

    assert denied.status_code == 409
    assert target.read_text(encoding="utf-8") == original
    assert list(workspace.glob(".mcp.json.tmp-*")) == []

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(agent_config_files_module.os, "replace", fail_replace)
    replace_denied = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": ".mcp.json"},
        json={"content": _http_mcp_content("denied")},
    )
    assert replace_denied.status_code == 409
    assert target.read_text(encoding="utf-8") == original
    assert list(workspace.glob(".mcp.json.tmp-*")) == []


def test_agent_config_file_rolls_back_failed_invalidation_and_serializes_expected_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, registry, session_store = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    target = workspace / ".mcp.json"
    original = '{"mcpServers": {}}\n'
    intermediate = _http_mcp_content("intermediate")
    final = _http_mcp_content("final")
    target.write_text(original, encoding="utf-8")
    session = session_store.create()
    session.agent_id = TEST_AGENT_ID
    session.sdk_session_id = "sdk-session-old"
    session_store.save(session)
    invalidation_entered = Event()
    allow_invalidation_failure = Event()
    second_started = Event()

    def fail_invalidation(*args: object, **kwargs: object) -> None:
        invalidation_entered.set()
        assert allow_invalidation_failure.wait(timeout=3)
        raise SessionConflictError("forced invalidation conflict")

    monkeypatch.setattr(session_store, "clear_sdk_session", fail_invalidation)
    service = AgentConfigFileService(settings=settings, agent_registry_store=registry, session_store=session_store)

    def first_update() -> object:
        return service.update_file(
            agent_id=TEST_AGENT_ID,
            path=".mcp.json",
            request=AgentConfigFileUpdateRequest(
                content=intermediate,
                expected_sha256=hashlib.sha256(original.encode()).hexdigest(),
                session_id=session.session_id,
            ),
        )

    def second_update() -> object:
        second_started.set()
        return service.update_file(
            agent_id=TEST_AGENT_ID,
            path=".mcp.json",
            request=AgentConfigFileUpdateRequest(
                content=final,
                expected_sha256=hashlib.sha256(intermediate.encode()).hexdigest(),
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_update)
        assert invalidation_entered.wait(timeout=3)
        second = executor.submit(second_update)
        assert second_started.wait(timeout=3)
        assert not second.done()
        allow_invalidation_failure.set()
        with pytest.raises(AgentConfigFileError, match="forced invalidation conflict"):
            first.result(timeout=3)
        with pytest.raises(AgentConfigFileError, match="reload before applying edits"):
            second.result(timeout=3)

    assert target.read_text(encoding="utf-8") == original
    assert list(workspace.glob(".mcp.json.tmp-*")) == []


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
