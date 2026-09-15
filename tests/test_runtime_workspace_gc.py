from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
import venv
from pathlib import Path

import pytest
from agentgov_agentscope_contract import session_workspace_id
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    SessionConfig,
    TeamData,
    TeamMember,
    TeamRecord,
)
from scripts import selected_env_source_snapshot as source_snapshot
from scripts.runtime_workspace_gc import plan_workspaces, quarantine_workspaces, read_governance_references
from scripts.runtime_workspace_gc_inventory import collect_native_inventory
from scripts.selected_env_operation_contract import WORKSPACE_GC_OPERATIONS, SelectedEnvError
from scripts.selected_env_workspace_gc import RecoveryCommands, verify_recovery_execution_boundary
from tests.runtime_workspace_gc_test_utils import (
    DIGEST,
    ROOT,
    USER,
    _create_native,
    _plan,
    _scenario,
    _workspace,
)


def test_deleted_native_agent_and_completed_ledger_allow_recoverable_workspace_and_real_venv_move(tmp_path) -> None:
    database, native_database, workspaces, workspace_id, _, _ = _scenario(tmp_path)
    workspace = workspaces / workspace_id
    environment = workspace / ".agentgov-runtime-cache/gateway-venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    original = (environment / "pyvenv.cfg").read_bytes()
    db_bytes = database.read_bytes()
    native_bytes = native_database.read_bytes()

    plan = _plan(database, native_database, workspaces)
    assert plan["native_global_complete"] is False
    assert plan["items"][0]["action"] == "quarantine"
    assert workspace.is_dir()  # 默认清单从不移动。
    result = quarantine_workspaces(workspaces, plan)

    quarantine = workspaces / ".quarantine" / result["quarantine_id"]
    assert not workspace.exists()
    assert (quarantine / workspace_id / ".agentgov-runtime-cache/gateway-venv/pyvenv.cfg").read_bytes() == original
    assert json.loads((quarantine / "manifest.json").read_bytes())["workspace_ids"] == [workspace_id]
    assert quarantine.stat().st_mode & 0o777 == 0o700
    assert (quarantine / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert database.read_bytes() == db_bytes
    assert native_database.read_bytes() == native_bytes
    os.rename(quarantine / workspace_id, workspace)  # 原路径未被占用时可按清单原样恢复。
    assert (environment / "pyvenv.cfg").read_bytes() == original


@pytest.mark.parametrize(("native_deleted", "cleanup_complete"), [(False, True), (True, False), (False, False)])
def test_live_native_reference_or_incomplete_cleanup_keeps_the_workspace(tmp_path, native_deleted, cleanup_complete) -> None:
    database, native_database, workspaces, workspace_id, _, _ = _scenario(tmp_path, native_deleted=native_deleted, cleanup_complete=cleanup_complete)
    plan = _plan(database, native_database, workspaces)
    assert plan["items"][0]["action"] == "retain"
    assert quarantine_workspaces(workspaces, plan) == {"quarantine_id": None, "moved": []}
    assert (workspaces / workspace_id).is_dir()


def test_new_version_binding_retains_a_completed_ephemeral_workspace(tmp_path) -> None:
    database, native_database, workspaces, _, runtime_agent_id, store = _scenario(tmp_path)
    store.bind_agent_version(agent_id="soc", agent_version_id="c" * 40, digest=DIGEST, runtime_agent_id=runtime_agent_id)
    assert _plan(database, native_database, workspaces)["items"][0]["reason"] == "referenced_agent"


def test_public_team_inventory_protects_a_shared_worker_workspace(tmp_path) -> None:
    database, native_database, workspaces, workspace_id, _, _ = _scenario(tmp_path)

    async def create_team() -> None:
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            records = []
            for source in ("user", "team"):
                agent = AgentRecord(
                    user_id=USER,
                    source=source,
                    data=AgentData(name="team contract", context_config=ContextConfig(), react_config=ReActConfig()),
                )
                await storage.upsert_agent(USER, agent)
                session = await storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id if source == "team" else "other-workspace"))
                records.append((agent, session))
            leader, worker = records
            await storage.upsert_team(
                USER,
                TeamRecord(
                    user_id=USER,
                    session_id=leader[1].id,
                    leader_agent_id=leader[0].id,
                    data=TeamData(name="maintenance team", members=[TeamMember(owner_id=USER, agent_id=worker[0].id, session_id=worker[1].id, role="created")]),
                ),
            )

    asyncio.run(create_team())
    assert _plan(database, native_database, workspaces)["items"][0]["reason"] == "referenced_workspace"


def test_pending_creation_intent_retains_the_owning_agent_resources(tmp_path) -> None:
    database, native_database, workspaces, _, runtime_agent_id, store = _scenario(tmp_path)
    intent, created = store.start_session_creation(
        idempotency_key=None,
        agent_id="soc",
        agent_version_id="b" * 40,
        runtime_agent_id=runtime_agent_id,
        digest=DIGEST,
        workspace_id=f"candidate-gc--v-{DIGEST}",
        requested_name=None,
    )
    assert created
    references = read_governance_references(database)
    assert intent.workspace_id in references.protected_workspaces
    assert _plan(database, native_database, workspaces)["items"][0]["action"] == "retain"


def test_unknown_directories_and_untracked_session_workspaces_are_retained(tmp_path) -> None:
    database, native_database, workspaces, _, _, _ = _scenario(tmp_path)
    (workspaces / "unknown").mkdir()
    normal_id = session_workspace_id(f"published-normal--v-{DIGEST}", uuid.uuid4())
    _workspace(workspaces, normal_id)
    plan = _plan(database, native_database, workspaces)
    reasons = {item["workspace_id"]: item["reason"] for item in plan["items"]}
    assert reasons["unknown"] == "unknown_workspace"
    assert reasons[normal_id] == "no_completed_ephemeral_deletion"


def test_incomplete_inventory_is_rejected_without_moving_any_files(tmp_path) -> None:
    database, native_database, workspaces, workspace_id, _, _ = _scenario(tmp_path)
    references = read_governance_references(database)
    native = asyncio.run(collect_native_inventory(native_database, set(references.agent_ids)))
    native["agents"].clear()  # 截断真实只读清单，验证缺页/缺失定位符不能放行。
    with pytest.raises(SelectedEnvError, match="未覆盖"):
        plan_workspaces(workspaces, references, native)
    assert (workspaces / workspace_id).is_dir()


def test_changed_workspace_marker_is_not_moved_after_planning(tmp_path) -> None:
    database, native_database, workspaces, workspace_id, _, _ = _scenario(tmp_path)
    plan = _plan(database, native_database, workspaces)
    marker = workspaces / workspace_id / ".agentgov-runtime-workspace.json"
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(SelectedEnvError, match="隔离前改变"):
        quarantine_workspaces(workspaces, plan)
    assert not (workspaces / ".quarantine").exists()


def test_governance_database_symlink_and_unknown_schema_are_rejected(tmp_path) -> None:
    unknown = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(SelectedEnvError):
        read_governance_references(unknown)
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(unknown)
    with pytest.raises(SelectedEnvError, match="真实 AgentGov"):
        read_governance_references(link)


def test_native_inventory_cli_uses_real_sdk_readonly_database_without_exposing_agent_text(tmp_path) -> None:
    native = tmp_path / "native.sqlite3"
    agent_id = asyncio.run(_create_native(native, f"candidate-gc--v-{DIGEST}", delete=False))
    original = native.read_bytes()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/runtime_workspace_gc_inventory.py"), "--database", str(native), "--agent-ids", agent_id],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["agents"][0]["present"] is True
    assert len(payload["agents"][0]["sessions"]) == 1
    assert "GC contract agent" not in result.stdout
    assert native.read_bytes() == original


def test_gc_operations_remain_explicit_list_and_apply_modes() -> None:
    assert WORKSPACE_GC_OPERATIONS == ("runtime-workspace-gc", "runtime-workspace-gc-apply")


def test_recovery_commands_only_allow_original_service_ids_and_running_state() -> None:
    api_id, runtime_id = "a" * 64, "b" * 64
    commands = RecoveryCommands(api_id, runtime_id, True, True)
    commands.validate(["docker", "info", "--format", "{{json .ID}}"])
    for target in (api_id, runtime_id):
        commands.validate(["docker", "inspect", "--format", "{{json .State}}", target])
    commands.validate(["docker", "unpause", api_id])
    commands.validate(["docker", "start", runtime_id])
    forbidden = (
        ["docker", "start", api_id],
        ["docker", "unpause", runtime_id],
        ["docker", "start", "c" * 64],
        ["docker", "rm", "--force", runtime_id],
        ["docker", "stop", runtime_id],
        ["docker", "compose", "up", "-d"],
        ["docker", "inspect", runtime_id],
    )
    for command in forbidden:
        with pytest.raises(SelectedEnvError, match="恢复"):
            commands.validate(command)
    stopped = RecoveryCommands(api_id, runtime_id, False, False)
    for command in (["docker", "unpause", api_id], ["docker", "start", runtime_id]):
        with pytest.raises(SelectedEnvError, match="恢复"):
            stopped.validate(command)
    for invalid in (RecoveryCommands(api_id, api_id, True, True), RecoveryCommands("api", runtime_id, True, True)):
        with pytest.raises(SelectedEnvError, match="原容器 ID"):
            invalid.validate(["docker", "info", "--format", "{{json .ID}}"])


def test_recovery_binary_guard_checks_real_files_without_loading_unrelated_toolchain(tmp_path) -> None:
    # 只验证执行文件边界，不让临时程序伪装 Docker 或生成 daemon/容器响应。
    binary = tmp_path / "true"
    digest = source_snapshot._copy_verified_binary(Path("/usr/bin/true").resolve(strict=True), binary)
    config = tmp_path / "docker-config"
    plugins = config / "cli-plugins"
    plugins.mkdir(mode=0o700, parents=True)
    compose_plugin = plugins / "docker-compose"
    compose_plugin.write_bytes(b"unexecuted plugin metadata")
    config.chmod(0o500)
    verify_recovery_execution_boundary(binary, digest, config)
    assert subprocess.run([str(binary)], capture_output=True, check=False).returncode == 0

    (tmp_path / "unrelated-selected.env").write_text("changed\n", encoding="utf-8")
    (tmp_path / "unrelated-python").write_text("changed\n", encoding="utf-8")
    compose_plugin.write_bytes(b"changed plugin metadata")
    verify_recovery_execution_boundary(binary, digest, config)
    config.chmod(0o700)
    (config / "config.json").write_text("{}", encoding="utf-8")
    config.chmod(0o500)
    with pytest.raises(SelectedEnvError, match="人工核验"):
        verify_recovery_execution_boundary(binary, digest, config)
    config.chmod(0o700)
    (config / "config.json").unlink()
    config.chmod(0o500)
    binary.chmod(0o700)
    with pytest.raises(SelectedEnvError):
        verify_recovery_execution_boundary(binary, digest, config)
    binary.write_bytes(binary.read_bytes() + b"changed")
    binary.chmod(0o500)
    with pytest.raises(SelectedEnvError):
        verify_recovery_execution_boundary(binary, digest, config)
