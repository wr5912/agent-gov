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
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services import agent_config_files as agent_config_files_module
from app.services.agent_config_files import AgentConfigFileService
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_AGENT_ID = "test-agent"


def _http_mcp_content(name: str) -> str:
    return (
        '{"mcp_config":{"type":"http_mcp","url":"https://'
        + name
        + '.invalid/mcp"},"credential_refs":[]}\n'
    )


def _test_app(tmp_path: Path) -> tuple[TestClient, AppSettings, AgentRegistryStore]:
    data_dir = tmp_path / "volume-agent-gov" / "data"
    settings = AppSettings(_env_file=None, DATA_DIR=data_dir)
    session_factory = make_session_factory(runtime_db_path_from_data_dir(data_dir))
    registry = AgentRegistryStore(session_factory)
    app = FastAPI()
    app.include_router(
        create_agent_config_files_router(
            settings=settings,
            agent_registry_store=registry,
            require_api_key=lambda: None,
        )
    )
    app.include_router(create_catalog_router(settings=settings, agent_registry_store=registry, require_api_key=lambda: None))
    return TestClient(app), settings, registry


def _register_agent(settings: AppSettings, registry: AgentRegistryStore, agent_id: str) -> Path:
    workspace = business_agent_layout(settings.data_dir, agent_id).workspace
    (workspace / "mcp").mkdir(parents=True, exist_ok=True)
    registry.create_business_agent(name=agent_id, agent_id=agent_id, workspace_dir=str(workspace))
    return workspace


def test_agent_config_file_updates_agentscope_mcp_and_keeps_existing_sessions_pinned(tmp_path: Path) -> None:
    client, settings, registry = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    target = workspace / "mcp" / "demo.json"
    target.write_text(_http_mcp_content("old"), encoding="utf-8")
    target.chmod(0o640)

    read_response = client.get(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
    )
    assert read_response.status_code == 200
    current = read_response.json()

    updated_content = _http_mcp_content("new")
    update_response = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": updated_content, "expected_sha256": current["sha256"]},
    )

    assert update_response.status_code == 200
    assert update_response.json()["existing_sessions_unchanged"] is True
    assert target.read_text(encoding="utf-8") == updated_content
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_agent_config_file_rejects_invalid_json_process_spawn_and_stale_sha(tmp_path: Path) -> None:
    client, settings, registry = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    target = workspace / "mcp" / "demo.json"
    target.write_text(_http_mcp_content("old"), encoding="utf-8")

    invalid = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": "{"},
    )
    wrong_shape = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": "[]"},
    )
    process_spawn = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": '{"command":"node","args":[]}'},
    )
    stale = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": _http_mcp_content("stale"), "expected_sha256": "not-current"},
    )

    assert invalid.status_code == 422
    assert wrong_shape.status_code == 422
    assert process_spawn.status_code == 422
    assert stale.status_code == 409
    assert target.read_text(encoding="utf-8") == _http_mcp_content("old")


def test_agent_config_file_rejects_uneditable_and_unsafe_paths_and_unknown_agents(tmp_path: Path) -> None:
    client, settings, registry = _test_app(tmp_path)
    _register_agent(settings, registry, TEST_AGENT_ID)

    uneditable = client.get("/api/agent-config-file", params={"agent_id": TEST_AGENT_ID, "path": "README.md"})
    hostile_agent = client.get("/api/agent-config-file", params={"agent_id": "../escape", "path": "AGENT.md"})
    hostile_path = client.get("/api/agent-config-file", params={"agent_id": TEST_AGENT_ID, "path": "mcp/../escape.json"})
    missing_agent = client.get("/api/agent-config-file", params={"agent_id": "missing-agent", "path": "AGENT.md"})

    assert uneditable.status_code == 422
    assert hostile_agent.status_code == 422
    assert hostile_path.status_code == 422
    assert missing_agent.status_code == 404


def test_agent_config_file_rejects_workspace_and_target_symlinks(tmp_path: Path) -> None:
    client, settings, registry = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    outside = tmp_path / "outside.json"
    outside.write_text(_http_mcp_content("outside"), encoding="utf-8")
    (workspace / "mcp" / "demo.json").symlink_to(outside)

    target_symlink = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": _http_mcp_content("new")},
    )

    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    workspace_symlink = tmp_path / "workspace-link"
    workspace_symlink.symlink_to(real_workspace, target_is_directory=True)
    registry.create_business_agent(name="linked-agent", agent_id="linked-agent", workspace_dir=str(workspace_symlink))
    directory_symlink = client.get(
        "/api/agent-config-file",
        params={"agent_id": "linked-agent", "path": "AGENT.md"},
    )

    assert target_symlink.status_code == 409
    assert directory_symlink.status_code == 409
    assert outside.read_text(encoding="utf-8") == _http_mcp_content("outside")


def test_agent_config_file_cleans_failed_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, settings, registry = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    target = workspace / "mcp" / "demo.json"
    original = _http_mcp_content("old")
    target.write_text(original, encoding="utf-8")

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(agent_config_files_module.os, "replace", fail_replace)
    response = client.put(
        "/api/agent-config-file",
        params={"agent_id": TEST_AGENT_ID, "path": "mcp/demo.json"},
        json={"content": _http_mcp_content("new")},
    )

    assert response.status_code == 409
    assert target.read_text(encoding="utf-8") == original
    assert list((workspace / "mcp").glob("demo.json.tmp-*")) == []


def test_agent_config_file_serializes_compare_and_swap_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, registry = _test_app(tmp_path)
    workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    target = workspace / "mcp" / "demo.json"
    original = _http_mcp_content("old")
    intermediate = _http_mcp_content("intermediate")
    final = _http_mcp_content("final")
    target.write_text(original, encoding="utf-8")
    service = AgentConfigFileService(settings=settings, agent_registry_store=registry)
    original_replace = service._atomic_replace
    first_entered = Event()
    allow_first = Event()
    second_started = Event()

    def controlled_replace(*, directory_fd: int, target_name: str, data: bytes, mode: int) -> None:
        if data == intermediate.encode():
            first_entered.set()
            assert allow_first.wait(timeout=3)
        original_replace(directory_fd=directory_fd, target_name=target_name, data=data, mode=mode)

    monkeypatch.setattr(service, "_atomic_replace", controlled_replace)

    def update(content: str, expected: str) -> object:
        if content == final:
            second_started.set()
        return service.update_file(
            agent_id=TEST_AGENT_ID,
            path="mcp/demo.json",
            request=AgentConfigFileUpdateRequest(content=content, expected_sha256=expected),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(update, intermediate, hashlib.sha256(original.encode()).hexdigest())
        assert first_entered.wait(timeout=3)
        second = executor.submit(update, final, hashlib.sha256(intermediate.encode()).hexdigest())
        assert second_started.wait(timeout=3)
        assert not second.done()
        allow_first.set()
        assert first.result(timeout=3).content == intermediate
        assert second.result(timeout=3).content == final

    assert target.read_text(encoding="utf-8") == final


def test_catalog_router_discovers_agent_scoped_agentscope_assets(tmp_path: Path) -> None:
    client, settings, registry = _test_app(tmp_path)
    test_workspace = _register_agent(settings, registry, TEST_AGENT_ID)
    disposal_workspace = _register_agent(settings, registry, "response-disposal")
    _write_agent_asset(test_workspace, "test-subagent", "test-skill")
    _write_agent_asset(disposal_workspace, "disposal-subagent", "disposal-skill")

    test_agents = client.get("/api/agents", params={"agent_id": TEST_AGENT_ID})
    disposal_agents = client.get("/api/agents", params={"agent_id": "response-disposal"})
    disposal_skills = client.get("/api/skills", params={"agent_id": "response-disposal"})

    assert test_agents.status_code == 200
    assert disposal_agents.status_code == 200
    assert disposal_skills.status_code == 200
    assert [item["name"] for item in test_agents.json()] == ["test-subagent"]
    assert [item["name"] for item in disposal_agents.json()] == ["disposal-subagent"]
    assert [item["name"] for item in disposal_skills.json()] == ["disposal-skill"]


def _write_agent_asset(workspace: Path, agent_name: str, skill_name: str) -> None:
    agent_dir = workspace / "subagents" / agent_name
    skill_dir = workspace / "skills" / skill_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        f"agent:\n  id: {agent_name}\n  name: {agent_name}\n  description: test agent\n",
        encoding="utf-8",
    )
    (agent_dir / "AGENT.md").write_text("Prompt\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test skill\n---\nInstructions\n",
        encoding="utf-8",
    )
