from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from agentgov_agentscope_contract import session_workspace_id
from agentscope.app._tool import TeamDelete
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    ScheduleData,
    ScheduleOrigin,
    ScheduleRecord,
    SessionConfig,
    TeamData,
    TeamMember,
    TeamRecord,
)
from agentscope.message import ToolResultState
from agentscope_runtime import session_workspace_release as release_module
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.session_workspace_release import SessionWorkspaceReclaimer
from fastapi.testclient import TestClient
from starlette.types import ASGIApp
from tests.runtime_workspace_gc_test_utils import (
    DIGEST,
    USER,
    _FailAfterAgentDelete,
    _native_session,
    _project_reclaim_runtime,
    _project_runtime_settings,
    _runtime_request,
    _runtime_workspace,
)


def _install_post_rename_fault(
    monkeypatch: pytest.MonkeyPatch,
    reclaimer: SessionWorkspaceReclaimer,
    target: Path,
    fault: str,
    restore_entered: threading.Event,
    allow_restore: threading.Event,
) -> None:
    real_rename = release_module.os.rename
    real_replace = reclaimer._replace_record
    real_fsync = reclaimer._fsync_directory
    real_restore = reclaimer._restore_tombstone
    state = {"moved": False, "failed": False}

    def observe_rename(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        if Path(source) == target:
            state["moved"] = True

    def fail_replace(path: Path, record) -> None:
        if fault == "record_replace" and state["moved"] and not state["failed"]:
            state["failed"] = True
            raise OSError("injected post-rename record replace failure")
        real_replace(path, record)

    def fail_fsync(path: Path) -> None:
        reclaim_root = target.parent / ".agentgov-session-workspace-reclaim"
        selected = reclaim_root if fault == "record_fsync" else target.parent
        if fault.endswith("fsync") and state["moved"] and path == selected and not state["failed"]:
            state["failed"] = True
            raise OSError("injected post-rename fsync failure")
        real_fsync(path)

    def block_restore(workspace_id: str) -> None:
        restore_entered.set()
        if not allow_restore.wait(timeout=5):
            raise RuntimeError("late Session upsert did not reach the fence")
        real_restore(workspace_id)

    monkeypatch.setattr(release_module.os, "rename", observe_rename)
    monkeypatch.setattr(reclaimer, "_replace_record", fail_replace)
    monkeypatch.setattr(reclaimer, "_fsync_directory", fail_fsync)
    monkeypatch.setattr(reclaimer, "_restore_tombstone", block_restore)


@pytest.mark.parametrize("fault", ("record_replace", "record_fsync", "root_fsync"))
def test_post_rename_bookkeeping_failure_restores_before_late_real_session_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, f"post-rename-{fault}") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-post-rename--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            restore_entered = threading.Event()
            allow_restore = threading.Event()
            _install_post_rename_fault(
                monkeypatch,
                reclaimer,
                target,
                fault,
                restore_entered,
                allow_restore,
            )

            release_task = asyncio.create_task(reclaimer.release_disappeared(USER, before, after))
            assert await asyncio.to_thread(restore_entered.wait, 5)
            upsert_task = asyncio.create_task(
                storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id)),
            )
            await asyncio.sleep(0)
            assert not upsert_task.done()
            allow_restore.set()
            replacement = await asyncio.wait_for(upsert_task, timeout=5)
            assert await asyncio.wait_for(release_task, timeout=5) == ()

            assert replacement.config.workspace_id == workspace_id
            assert target.is_dir() and venv_bytes.is_file()
            manager._validate_existing_target(target, workspace_id, DIGEST)
            assert list((workspaces / ".agentgov-session-workspace-reclaim").iterdir()) == []

    asyncio.run(scenario())


def test_post_rename_reference_failure_restores_before_late_real_session_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "post-rename-reference-failure") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(
                f"published-post-rename-reference--v-{DIGEST}",
                uuid.uuid4(),
            )
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            real_reference_check = manager._workspace_is_referenced_locked
            reference_checks = 0

            async def fail_post_rename_reference_check(
                user_id: str,
                candidate: str,
                ignored_bindings: frozenset[tuple[str, str]],
            ) -> bool:
                nonlocal reference_checks
                reference_checks += 1
                if reference_checks == 2:
                    raise OSError("injected post-rename reference inventory failure")
                return await real_reference_check(user_id, candidate, ignored_bindings)

            restore_entered = threading.Event()
            allow_restore = threading.Event()
            real_restore = reclaimer._restore_tombstone

            def blocked_restore(candidate: str) -> None:
                restore_entered.set()
                if not allow_restore.wait(timeout=5):
                    raise RuntimeError("late Session upsert did not wait for restore")
                real_restore(candidate)

            monkeypatch.setattr(
                manager,
                "_workspace_is_referenced_locked",
                fail_post_rename_reference_check,
            )
            monkeypatch.setattr(reclaimer, "_restore_tombstone", blocked_restore)
            release_task = asyncio.create_task(reclaimer.release_disappeared(USER, before, after))
            assert await asyncio.to_thread(restore_entered.wait, 5)
            upsert_task = asyncio.create_task(
                storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id)),
            )
            await asyncio.sleep(0)
            assert not upsert_task.done()

            allow_restore.set()
            replacement = await asyncio.wait_for(upsert_task, timeout=5)
            assert await asyncio.wait_for(release_task, timeout=5) == ()
            assert replacement.config.workspace_id == workspace_id
            assert target.is_dir() and venv_bytes.is_file()
            manager._validate_existing_target(target, workspace_id, DIGEST)
            assert list((workspaces / ".agentgov-session-workspace-reclaim").iterdir()) == []

    asyncio.run(scenario())


def test_cancellation_during_post_rename_reference_check_restores_before_late_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "post-rename-reference-cancel") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(
                f"published-post-rename-cancel--v-{DIGEST}",
                uuid.uuid4(),
            )
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            real_reference_check = manager._workspace_is_referenced_locked
            reference_checks = 0
            post_check_entered = asyncio.Event()

            async def block_post_rename_reference_check(
                user_id: str,
                candidate: str,
                ignored_bindings: frozenset[tuple[str, str]],
            ) -> bool:
                nonlocal reference_checks
                reference_checks += 1
                if reference_checks == 2:
                    post_check_entered.set()
                    await asyncio.Event().wait()
                return await real_reference_check(user_id, candidate, ignored_bindings)

            restore_entered = threading.Event()
            allow_restore = threading.Event()
            real_restore = reclaimer._restore_tombstone

            def blocked_restore(candidate: str) -> None:
                restore_entered.set()
                if not allow_restore.wait(timeout=5):
                    raise RuntimeError("late Session upsert did not wait for cancelled restore")
                real_restore(candidate)

            monkeypatch.setattr(
                manager,
                "_workspace_is_referenced_locked",
                block_post_rename_reference_check,
            )
            monkeypatch.setattr(reclaimer, "_restore_tombstone", blocked_restore)
            release_task = asyncio.create_task(reclaimer.release_disappeared(USER, before, after))
            await asyncio.wait_for(post_check_entered.wait(), timeout=5)
            release_task.cancel()
            assert await asyncio.to_thread(restore_entered.wait, 5)
            upsert_task = asyncio.create_task(
                storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id)),
            )
            await asyncio.sleep(0)
            assert not upsert_task.done()

            allow_restore.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(release_task, timeout=5)
            replacement = await asyncio.wait_for(upsert_task, timeout=5)
            assert replacement.config.workspace_id == workspace_id
            assert target.is_dir() and venv_bytes.is_file()
            manager._validate_existing_target(target, workspace_id, DIGEST)
            assert list((workspaces / ".agentgov-session-workspace-reclaim").iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("response_failure", (False, True))
def test_public_agent_delete_reclaims_after_commit_even_if_downstream_response_fails(
    tmp_path: Path,
    response_failure: bool,
) -> None:
    settings = _project_runtime_settings(tmp_path, f"agent-route-{response_failure}")
    app = create_runtime_app(settings)
    client_app: ASGIApp = _FailAfterAgentDelete(app) if response_failure else app
    with TestClient(client_app) as client:
        created_agent = _runtime_request(client, settings, "POST", "/agent/", {"name": "GC route agent"})
        assert created_agent.status_code == 201
        agent_id = created_agent.json()["agent_id"]
        workspace_id = session_workspace_id(f"candidate-agent-route--v-{DIGEST}", uuid.uuid4())
        created_session = _runtime_request(
            client,
            settings,
            "POST",
            "/sessions/",
            {"agent_id": agent_id, "workspace_id": workspace_id},
        )
        assert created_session.status_code == 201
        session_id = created_session.json()["session_id"]
        target, venv_bytes = _runtime_workspace(settings.workspaces_root, workspace_id)
        app.state.workspace_manager._session_workspaces[(USER, agent_id, session_id)] = workspace_id
        path = f"/agent/{agent_id}"

        if response_failure:
            with pytest.raises(RuntimeError, match="downstream response failure"):
                _runtime_request(client, settings, "DELETE", path)
        else:
            assert _runtime_request(client, settings, "DELETE", path).status_code == 204

        assert not target.exists() and not venv_bytes.exists()
        assert workspace_id not in app.state.workspace_manager._session_workspaces.values()


def test_chat_team_delete_reclaims_created_and_invited_worker_workspaces(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "team-delete", bind_deletes=True) as runtime:
            storage, manager, _, workspaces, _ = runtime
            version = f"published-team-delete--v-{DIGEST}"
            leader_id = session_workspace_id(version, uuid.uuid4())
            created_id = session_workspace_id(version, uuid.uuid4())
            invited_id = session_workspace_id(version, uuid.uuid4())
            leader_target, _ = _runtime_workspace(workspaces, leader_id)
            created_target, created_venv = _runtime_workspace(workspaces, created_id)
            invited_target, invited_venv = _runtime_workspace(workspaces, invited_id)
            leader, leader_session = await _native_session(storage, leader_id)
            created, created_session = await _native_session(storage, created_id, source="team")
            invited, invited_session = await _native_session(storage, invited_id)
            team = TeamRecord(
                user_id=USER,
                session_id=leader_session.id,
                leader_agent_id=leader.id,
                data=TeamData(
                    name="Chat TeamDelete reclaim",
                    members=[
                        TeamMember(owner_id=USER, agent_id=created.id, session_id=created_session.id, role="created"),
                        TeamMember(owner_id=USER, agent_id=invited.id, session_id=invited_session.id, role="invited"),
                    ],
                ),
            )
            await storage.upsert_team(USER, team)
            await AsyncSQLAlchemyStorage.set_session_team_id(storage, USER, leader_session.id, team.id)
            manager._session_workspaces[(USER, created.id, created_session.id)] = created_id
            manager._session_workspaces[(USER, invited.id, invited_session.id)] = invited_id

            result = await TeamDelete(
                storage=storage,
                message_bus=InMemoryMessageBus(),
                workspace_manager=manager,
                user_id=USER,
                session_id=leader_session.id,
                agent_id=leader.id,
            )()

            assert result.state != ToolResultState.ERROR
            assert "dissolved" in result.content[0].text
            assert leader_target.is_dir()
            assert not created_target.exists() and not created_venv.exists()
            assert not invited_target.exists() and not invited_venv.exists()
            assert await storage.get_agent(USER, created.id) is None
            assert await storage.get_agent(USER, invited.id) is not None
            assert await storage.get_team(USER, team.id) is None

    asyncio.run(scenario())


def test_storage_schedule_delete_reclaims_real_execution_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "schedule-delete", bind_deletes=True) as runtime:
            storage, _, _, workspaces, settings = runtime
            workspace_id = session_workspace_id(f"published-schedule--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, _ = await _native_session(storage, "unrelated-workspace")
            schedule = ScheduleRecord(
                user_id=USER,
                agent_id=agent.id,
                data=ScheduleData(
                    name="Workspace cleanup schedule",
                    cron_expression="0 * * * *",
                    chat_model_config=ChatModelConfig(
                        type=settings.credential_type,
                        credential_id=settings.credential_id,
                        model="unused-test-model",
                        parameters={},
                    ),
                ),
            )
            await storage.upsert_schedule(USER, schedule)
            execution = await storage.upsert_session(
                USER,
                agent.id,
                SessionConfig(workspace_id=workspace_id),
                origin=ScheduleOrigin(schedule_id=schedule.id),
            )
            assert [record.id for record in await storage.list_sessions_by_schedule(USER, schedule.id)] == [execution.id]

            assert await storage.delete_schedule(USER, schedule.id)
            assert await storage.get_session(USER, agent.id, execution.id) is None
            assert not target.exists() and not venv_bytes.exists()

    asyncio.run(scenario())


def test_reconcile_short_circuits_referenced_workspaces_after_one_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "inventory-once") as runtime:
            storage, _, reclaimer, workspaces, _ = runtime
            for _ in range(3):
                workspace_id = session_workspace_id(f"published-inventory--v-{DIGEST}", uuid.uuid4())
                _runtime_workspace(workspaces, workspace_id)
                await _native_session(storage, workspace_id)
            real_list_agents = storage.list_agents
            calls = 0

            async def counted_list_agents(user_id: str):
                nonlocal calls
                calls += 1
                return await real_list_agents(user_id)

            monkeypatch.setattr(storage, "list_agents", counted_list_agents)
            await reclaimer.reconcile(USER)
            assert calls == 1

    asyncio.run(scenario())


def test_reconcile_bounds_reference_checks_for_a_real_unreferenced_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "inventory-orphan") as runtime:
            storage, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-inventory-orphan--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            real_list_agents = storage.list_agents
            calls = 0

            async def counted_list_agents(user_id: str):
                nonlocal calls
                calls += 1
                return await real_list_agents(user_id)

            monkeypatch.setattr(storage, "list_agents", counted_list_agents)
            await reclaimer.reconcile(USER)
            assert calls == 3
            assert not target.exists() and not venv_bytes.exists()

    asyncio.run(scenario())


def test_phase_contract_allows_crash_skip_and_rejects_backward_transition(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "phase-contract") as runtime:
            _, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-phase--v-{DIGEST}", uuid.uuid4())
            _runtime_workspace(workspaces, workspace_id)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())
            key = tombstone.name
            record_path = tombstone.parent / f"{key}.json"
            record = reclaimer._load_record(record_path, key)
            assert record.phase == "quarantined"
            with pytest.raises(RuntimeError, match="phase transition is invalid"):
                reclaimer._transition_record(record, "prepared")
            assert reclaimer._load_record(record_path, key).phase == "quarantined"

            prepared = replace(record, phase="prepared")
            reclaimer._replace_record(record_path, prepared)
            reclaimer._delete_tombstone(key)
            assert list(tombstone.parent.iterdir()) == []

    asyncio.run(scenario())


def test_restart_removes_valid_stale_temporary_sidecar_and_finishes_tombstone(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "stale-temp") as runtime:
            _, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-temp--v-{DIGEST}", uuid.uuid4())
            _runtime_workspace(workspaces, workspace_id)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())
            key = tombstone.name
            record_path = tombstone.parent / f"{key}.json"
            record = reclaimer._load_record(record_path, key)
            temporary = tombstone.parent / f".{key}.deadbeef.tmp"
            temporary.write_bytes(reclaimer._record_bytes(replace(record, phase="contents_removed")))

            await reclaimer.reconcile(USER)
            assert not temporary.exists()
            assert list(tombstone.parent.iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", (b"{", b""))
def test_restart_removes_incomplete_initial_temporary_and_reclaims_live_orphan(
    tmp_path: Path,
    payload: bytes,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "initial-temp-crash") as runtime:
            _, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-initial-temp--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            key = reclaimer._record_key(workspace_id)
            reclaim_root = reclaimer._ensure_reclaim_root()
            temporary = reclaim_root / f".{key}.deadbeef.tmp"
            temporary.write_bytes(payload)

            await reclaimer.reconcile(USER)
            assert not target.exists() and not venv_bytes.exists()
            assert list(reclaim_root.iterdir()) == []

    asyncio.run(scenario())


def test_restart_removes_truncated_replace_temporary_and_finishes_tombstone(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "truncated-replace-temp") as runtime:
            _, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-replace-temp--v-{DIGEST}", uuid.uuid4())
            _runtime_workspace(workspaces, workspace_id)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())
            key = tombstone.name
            temporary = tombstone.parent / f".{key}.deadbeef.tmp"
            temporary.write_bytes(b"{")

            await reclaimer.reconcile(USER)
            assert list(tombstone.parent.iterdir()) == []

    asyncio.run(scenario())


def test_restart_replaces_invalid_legacy_initial_record_for_intact_live_orphan(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "legacy-initial-record") as runtime:
            _, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-legacy-record--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            key = reclaimer._record_key(workspace_id)
            reclaim_root = reclaimer._ensure_reclaim_root()
            record_path = reclaim_root / f"{key}.json"
            record_path.write_bytes(b"{")

            await reclaimer.reconcile(USER)
            assert not target.exists() and not venv_bytes.exists()
            assert list(reclaim_root.iterdir()) == []

    asyncio.run(scenario())


def test_restart_preserves_and_fails_closed_on_symlink_temporary_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "unsafe-temp") as runtime:
            _, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-unsafe-temp--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())
            key = tombstone.name
            real_remove_record = reclaimer._remove_record
            monkeypatch.setattr(reclaimer, "_remove_record", lambda candidate: None)
            reclaimer._restore_tombstone(workspace_id)
            monkeypatch.setattr(reclaimer, "_remove_record", real_remove_record)
            outside = tmp_path / "outside-sidecar"
            outside.write_bytes(b"must survive")
            temporary = tombstone.parent / f".{key}.deadbeef.tmp"
            temporary.symlink_to(outside)

            await reclaimer.reconcile(USER)
            assert temporary.is_symlink()
            assert target.is_dir() and venv_bytes.is_file()
            assert not tombstone.exists()
            assert (tombstone.parent / f"{key}.json").is_file()
            assert outside.read_bytes() == b"must survive"

    asyncio.run(scenario())


def test_restored_target_with_stale_record_survives_restart_and_can_be_deleted_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, "restore-record-crash")
        workspace_id = session_workspace_id(f"published-restore-crash--v-{DIGEST}", uuid.uuid4())
        real_remove_tree = release_module.remove_private_staging_tree
        agent_id = ""

        async with _project_reclaim_runtime(tmp_path, "restore-record-crash", settings=settings) as first:
            storage, manager, reclaimer, workspaces, _ = first
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            agent_id = agent.id
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            monkeypatch.setattr(
                release_module,
                "remove_private_staging_tree",
                lambda path: (_ for _ in ()).throw(OSError("injected delete interruption")),
            )
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            assert not target.exists() and not venv_bytes.exists()

        monkeypatch.setattr(release_module, "remove_private_staging_tree", real_remove_tree)
        async with _project_reclaim_runtime(tmp_path, "restore-record-crash", settings=settings) as second:
            storage, _, reclaimer, workspaces, _ = second
            replacement = await storage.upsert_session(
                USER,
                agent_id,
                SessionConfig(workspace_id=workspace_id),
            )
            real_remove_record = reclaimer._remove_record
            failed = False

            def fail_after_restore(key: str) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected restored-record unlink crash")
                real_remove_record(key)

            monkeypatch.setattr(reclaimer, "_remove_record", fail_after_restore)
            await reclaimer.reconcile(USER)
            restored = workspaces / workspace_id
            assert restored.is_dir()
            assert (restored / ".agentgov-runtime-state/.agentscope/.venv/workspace-owned-bytes.bin").is_file()
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            assert any(path.suffix == ".json" for path in reclaim_root.iterdir())

        async with _project_reclaim_runtime(
            tmp_path,
            "restore-record-crash",
            settings=settings,
            bind_deletes=True,
        ) as third:
            storage, manager, reclaimer, workspaces, _ = third
            await reclaimer.reconcile(USER)
            assert list((workspaces / ".agentgov-session-workspace-reclaim").iterdir()) == []
            assert (await storage.get_session(USER, agent_id, replacement.id)) is not None
            manager._validate_existing_target(workspaces / workspace_id, workspace_id, DIGEST)
            assert await storage.delete_session(USER, agent_id, replacement.id)
            assert not (workspaces / workspace_id).exists()

    asyncio.run(scenario())


def test_unprovable_post_rename_restore_retires_before_waiting_session_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "unprovable-restore") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-unprovable--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            restore_entered = threading.Event()
            allow_failure = threading.Event()

            monkeypatch.setattr(
                reclaimer,
                "_replace_record",
                lambda path, record: (_ for _ in ()).throw(OSError("injected record replace failure")),
            )

            def fail_restore(workspace_id: str) -> None:
                restore_entered.set()
                if not allow_failure.wait(timeout=5):
                    raise RuntimeError("late upsert did not wait on the fence")
                raise OSError("injected unprovable restore failure")

            monkeypatch.setattr(reclaimer, "_restore_tombstone", fail_restore)
            release_task = asyncio.create_task(reclaimer.release_disappeared(USER, before, after))
            assert await asyncio.to_thread(restore_entered.wait, 5)
            upsert_task = asyncio.create_task(
                storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id)),
            )
            await asyncio.sleep(0)
            assert not upsert_task.done()
            allow_failure.set()
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await asyncio.wait_for(upsert_task, timeout=5)
            assert await asyncio.wait_for(release_task, timeout=5) == ()
            assert not target.exists() and not venv_bytes.exists()
            assert any(path.is_dir() for path in (workspaces / ".agentgov-session-workspace-reclaim").iterdir())
            assert await storage.list_sessions(USER, agent.id) == []

    asyncio.run(scenario())
