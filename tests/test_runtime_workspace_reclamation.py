from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path

import pytest
from agentgov_agentscope_contract import session_workspace_id
from agentscope.app.storage import (
    AsyncSQLAlchemyStorage,
    SessionConfig,
    SessionRecord,
    TeamData,
    TeamMember,
    TeamRecord,
)
from agentscope_runtime import session_workspace_release as release_module
from agentscope_runtime.session_workspace_release import SessionWorkspaceReclaimer
from agentscope_runtime.workspace_manager import AgentGovLocalWorkspace, AgentGovWorkspaceManager
from agentscope_runtime.workspace_reference_fence import SessionWorkspaceReferenceFence
from tests.runtime_workspace_gc_test_utils import (
    DIGEST,
    USER,
    _native_session,
    _project_reclaim_runtime,
    _reclaim_manager,
    _runtime_workspace,
    _workspace,
)


def test_native_session_deletion_reclaims_each_real_per_session_venv_and_keeps_snapshot_source(tmp_path) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-reclaim.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, candidates, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            source = candidates / "published-reclaim/workspace"
            source.mkdir(parents=True)
            source_file = source / "immutable-harness.txt"
            source_file.write_bytes(b"immutable")
            version = f"published-reclaim--v-{DIGEST}"
            first_id = session_workspace_id(version, uuid.uuid4())
            second_id = session_workspace_id(version, uuid.uuid4())
            first_target, first_venv_bytes = _runtime_workspace(workspaces, first_id)
            second_target, second_venv_bytes = _runtime_workspace(workspaces, second_id)
            agent, first = await _native_session(storage, first_id)
            _, second = await _native_session(storage, second_id, agent=agent)

            cached = AgentGovLocalWorkspace(
                workspace_id=first_id,
                host_workdir=str(first_target / ".agentgov-runtime-state"),
                host_cache_dir=str(first_target / ".agentgov-runtime-cache"),
                harness_root=source,
                expected_digest=DIGEST,
            )
            cached.is_alive = True
            manager._cache[first_id] = cached
            manager._session_workspaces[(USER, agent.id, first.id)] = first_id

            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, first.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before, after) == (first_id,)
            assert not first_target.exists()
            assert not first_venv_bytes.exists()
            assert not cached.is_alive
            assert second_target.is_dir()
            assert second_venv_bytes.is_file()
            assert source_file.read_bytes() == b"immutable"

            assert await reclaimer.release_disappeared(USER, after, after) == ()
            before_last = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, second.id)
            after_last = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before_last, after_last) == (second_id,)
            assert not second_target.exists()
            assert not second_venv_bytes.exists()
            assert source_file.read_bytes() == b"immutable"

    asyncio.run(scenario())


def test_new_native_reference_between_delete_and_reclaim_keeps_workspace_until_zero_reference(tmp_path) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-concurrency.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, _, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            workspace_id = session_workspace_id(f"published-race--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, first = await _native_session(storage, workspace_id)

            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, first.id)
            _, replacement = await _native_session(storage, workspace_id, agent=agent)
            after = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            assert target.is_dir()
            assert venv_bytes.is_file()

            before_last = after
            assert await storage.delete_session(USER, agent.id, replacement.id)
            after_last = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before_last, after_last) == (workspace_id,)
            assert not target.exists()

    asyncio.run(scenario())


def test_leader_delete_snapshot_captures_and_reclaims_real_cascade_worker_workspace(tmp_path) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-team.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, _, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            version = f"published-team--v-{DIGEST}"
            leader_workspace = session_workspace_id(version, uuid.uuid4())
            worker_workspace = session_workspace_id(version, uuid.uuid4())
            leader_target, _ = _runtime_workspace(workspaces, leader_workspace)
            worker_target, worker_venv_bytes = _runtime_workspace(workspaces, worker_workspace)
            leader, leader_session = await _native_session(storage, leader_workspace)
            worker, worker_session = await _native_session(storage, worker_workspace, source="team")
            team = TeamRecord(
                user_id=USER,
                session_id=leader_session.id,
                leader_agent_id=leader.id,
                data=TeamData(
                    name="Runtime workspace cascade",
                    members=[
                        TeamMember(
                            owner_id=USER,
                            agent_id=worker.id,
                            session_id=worker_session.id,
                            role="created",
                        ),
                    ],
                ),
            )
            await storage.upsert_team(USER, team)
            await storage.set_session_team_id(USER, leader_session.id, team.id)

            before = await manager.snapshot_native_session_workspaces(USER)
            assert before[(worker.id, worker_session.id)] == worker_workspace
            assert await storage.delete_session(USER, leader.id, leader_session.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            assert (worker.id, worker_session.id) not in after
            reclaimed = await reclaimer.release_disappeared(USER, before, after)
            assert set(reclaimed) == {leader_workspace, worker_workspace}
            assert not leader_target.exists()
            assert not worker_target.exists()
            assert not worker_venv_bytes.exists()
            assert await storage.get_agent(USER, worker.id) is None

    asyncio.run(scenario())


def test_interrupted_tombstone_delete_is_retried_by_a_new_runtime_manager(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-restart.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, candidates, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            workspace_id = session_workspace_id(f"published-restart--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, session = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, session.id)
            after = await manager.snapshot_native_session_workspaces(USER)

            real_remove = release_module.remove_private_staging_tree

            def interrupt_delete(path: Path) -> None:
                raise OSError("injected tombstone delete interruption")

            monkeypatch.setattr(release_module, "remove_private_staging_tree", interrupt_delete)
            with caplog.at_level("WARNING"):
                assert await reclaimer.release_disappeared(USER, before, after) == ()
            assert not target.exists()
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            tombstones = [entry for entry in reclaim_root.iterdir() if entry.is_dir()]
            assert len(tombstones) == 1
            assert (tombstones[0] / venv_bytes.relative_to(target)).is_file()
            assert workspace_id not in caplog.text
            assert session.id not in caplog.text

            monkeypatch.setattr(release_module, "remove_private_staging_tree", real_remove)
            restarted = AgentGovWorkspaceManager(
                business_agents_root=tmp_path / "business",
                candidates_root=candidates,
                workspaces_root=workspaces,
            )
            restarted.bind_storage(storage)
            await SessionWorkspaceReclaimer(restarted).reconcile(USER)
            assert list(reclaim_root.iterdir()) == []
            assert not venv_bytes.exists()

    asyncio.run(scenario())


def test_reclaim_rejects_version_workspace_partial_state_symlink_and_missing_tombstone_marker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-invalid.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, candidates, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            version_workspace = f"published-invalid--v-{DIGEST}"
            version_target = _workspace(workspaces, version_workspace)
            agent, version_session = await _native_session(storage, version_workspace)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, version_session.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            assert version_target.is_dir()

            partial_id = session_workspace_id(version_workspace, uuid.uuid4())
            partial = _workspace(workspaces, partial_id)
            (partial / ".agentgov-runtime-cache").rmdir()
            _, partial_session = await _native_session(storage, partial_id, agent=agent)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, partial_session.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            assert partial.is_dir()

            outside = candidates / "published-invalid/workspace"
            outside.mkdir(parents=True)
            outside_file = outside / "must-survive.txt"
            outside_file.write_bytes(b"published source")
            symlink_id = session_workspace_id(version_workspace, uuid.uuid4())
            (workspaces / symlink_id).symlink_to(outside, target_is_directory=True)
            _, symlink_session = await _native_session(storage, symlink_id, agent=agent)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, symlink_session.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            assert outside_file.read_bytes() == b"published source"

            valid_id = session_workspace_id(version_workspace, uuid.uuid4())
            valid_target, _ = _runtime_workspace(workspaces, valid_id)
            _, valid_session = await _native_session(storage, valid_id, agent=agent)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, valid_session.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            real_remove = release_module.remove_private_staging_tree
            monkeypatch.setattr(
                release_module,
                "remove_private_staging_tree",
                lambda path: (_ for _ in ()).throw(OSError("injected delete interruption")),
            )
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            tombstone = next(entry for entry in reclaim_root.iterdir() if entry.is_dir())
            (tombstone / ".agentgov-runtime-workspace.json").unlink()
            monkeypatch.setattr(release_module, "remove_private_staging_tree", real_remove)
            await SessionWorkspaceReclaimer(manager).reconcile(USER)
            assert tombstone.is_dir()
            assert not valid_target.exists()
            assert outside_file.read_bytes() == b"published source"

    asyncio.run(scenario())


def test_session_created_at_quarantine_boundary_restores_workspace_before_delete(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-quarantine-race.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, _, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            workspace_id = session_workspace_id(f"published-quarantine-race--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after_delete = await manager.snapshot_native_session_workspaces(USER)

            callback_entered = threading.Event()
            session_inserted = threading.Event()
            real_quarantine = reclaimer._quarantine

            def quarantine_after_concurrent_insert(
                candidate: str,
                ignored_bindings: frozenset[tuple[str, str]],
            ) -> Path:
                callback_entered.set()
                if not session_inserted.wait(timeout=5):
                    raise RuntimeError("concurrent Session insert did not complete")
                return real_quarantine(candidate, ignored_bindings)

            async def insert_session() -> SessionRecord:
                assert await asyncio.to_thread(callback_entered.wait, 5)
                _, replacement = await _native_session(storage, workspace_id, agent=agent)
                session_inserted.set()
                return replacement

            monkeypatch.setattr(reclaimer, "_quarantine", quarantine_after_concurrent_insert)
            insert_task = asyncio.create_task(insert_session())
            assert await reclaimer.release_disappeared(USER, before, after_delete) == ()
            replacement = await insert_task
            assert target.is_dir()
            assert venv_bytes.is_file()
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            assert list(reclaim_root.iterdir()) == []

            monkeypatch.setattr(reclaimer, "_quarantine", real_quarantine)
            before_last = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, replacement.id)
            after_last = await manager.snapshot_native_session_workspaces(USER)
            assert await reclaimer.release_disappeared(USER, before_last, after_last) == (workspace_id,)
            assert not target.exists()

    asyncio.run(scenario())


def test_project_storage_fence_rejects_upsert_started_after_final_reference_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fence = SessionWorkspaceReferenceFence()
        async with _project_reclaim_runtime(
            tmp_path,
            "fenced",
            fence=fence,
        ) as (storage, manager, reclaimer, workspaces, _):
            workspace_id = session_workspace_id(f"published-fenced-race--v-{DIGEST}", uuid.uuid4())

            # AgentScope persists the Session before get_workspace materializes
            # its target. The fence must allow this normal public create order.
            agent, deleted = await _native_session(storage, workspace_id)
            target = workspaces / workspace_id
            assert not target.exists()
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after_delete = await manager.snapshot_native_session_workspaces(USER)

            final_check_returned = asyncio.Event()
            allow_retirement = asyncio.Event()
            real_reference_check = manager._workspace_is_referenced_locked
            call_count = 0

            async def observe_final_reference_check(
                user_id: str,
                candidate: str,
                ignored_bindings: frozenset[tuple[str, str]],
            ) -> bool:
                nonlocal call_count
                referenced = await real_reference_check(user_id, candidate, ignored_bindings)
                call_count += 1
                if call_count == 2:
                    final_check_returned.set()
                    await allow_retirement.wait()
                return referenced

            monkeypatch.setattr(
                manager,
                "_workspace_is_referenced_locked",
                observe_final_reference_check,
            )
            release_task = asyncio.create_task(
                reclaimer.release_disappeared(USER, before, after_delete),
            )
            await asyncio.wait_for(final_check_returned.wait(), timeout=5)
            upsert_task = asyncio.create_task(
                storage.upsert_session(
                    USER,
                    agent.id,
                    SessionConfig(workspace_id=workspace_id),
                ),
            )
            await asyncio.sleep(0)
            assert not upsert_task.done()

            allow_retirement.set()
            assert await asyncio.wait_for(release_task, timeout=5) == (workspace_id,)
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await asyncio.wait_for(upsert_task, timeout=5)
            assert await storage.list_sessions(USER, agent.id) == []
            assert not target.exists()
            assert not venv_bytes.exists()

    asyncio.run(scenario())


def test_runtime_startup_scan_recovers_delete_committed_before_sidecar_creation(tmp_path) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-pre-sidecar-crash.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, candidates, workspaces = _reclaim_manager(tmp_path, storage)
            workspace_id = session_workspace_id(f"published-pre-sidecar--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, session = await _native_session(storage, workspace_id)
            assert await storage.delete_session(USER, agent.id, session.id)
            assert target.is_dir()

            restarted = AgentGovWorkspaceManager(
                business_agents_root=tmp_path / "business",
                candidates_root=candidates,
                workspaces_root=workspaces,
            )
            restarted.bind_storage(storage)
            await SessionWorkspaceReclaimer(restarted).reconcile(USER)
            assert not target.exists()
            assert not venv_bytes.exists()
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            assert list(reclaim_root.iterdir()) == []

    asyncio.run(scenario())


def test_contents_removed_phase_finishes_marker_unlink_to_rmdir_crash_window(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        native_database = tmp_path / "native-empty-tombstone.sqlite3"
        async with AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{native_database}") as storage:
            manager, candidates, workspaces = _reclaim_manager(tmp_path, storage)
            reclaimer = SessionWorkspaceReclaimer(manager)
            workspace_id = session_workspace_id(f"published-empty-tombstone--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, session = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, session.id)
            after = await manager.snapshot_native_session_workspaces(USER)

            real_rmdir = Path.rmdir

            def interrupt_final_rmdir(path: Path) -> None:
                if path.parent.name == ".agentgov-session-workspace-reclaim" and len(path.name) == 64:
                    raise OSError("injected marker-to-rmdir crash")
                real_rmdir(path)

            monkeypatch.setattr(Path, "rmdir", interrupt_final_rmdir)
            assert await reclaimer.release_disappeared(USER, before, after) == ()
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            tombstone = next(entry for entry in reclaim_root.iterdir() if entry.is_dir())
            record_path = next(entry for entry in reclaim_root.iterdir() if entry.suffix == ".json")
            assert list(tombstone.iterdir()) == []
            assert json.loads(record_path.read_bytes())["phase"] == "contents_removed"
            assert not target.exists()
            assert not venv_bytes.exists()

            monkeypatch.setattr(Path, "rmdir", real_rmdir)
            restarted = AgentGovWorkspaceManager(
                business_agents_root=tmp_path / "business",
                candidates_root=candidates,
                workspaces_root=workspaces,
            )
            restarted.bind_storage(storage)
            await SessionWorkspaceReclaimer(restarted).reconcile(USER)
            assert list(reclaim_root.iterdir()) == []

    asyncio.run(scenario())
