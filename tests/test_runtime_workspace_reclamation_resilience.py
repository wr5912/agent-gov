from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path

import pytest
from agentgov_agentscope_contract import session_workspace_id
from agentgov_harness_digest import harness_content_digest
from agentscope import set_id_factory
from agentscope.app.storage import (
    AgentRecord,
    AsyncSQLAlchemyStorage,
    SessionConfig,
    TeamData,
    TeamOrigin,
    TeamRecord,
)
from agentscope_runtime.workspace_manager import AgentGovLocalWorkspace
from agentscope_runtime.workspace_reclaim_integrity import WorkspaceReclamationIntegrityError
from tests.runtime_workspace_gc_test_utils import (
    DIGEST,
    USER,
    _native_session,
    _project_reclaim_runtime,
    _project_runtime_settings,
    _runtime_workspace,
)


def test_crashed_agent_create_worker_session_remains_authoritative_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, "crashed-agent-create")
        workspace_id = session_workspace_id(f"published-hidden-worker--v-{DIGEST}", uuid.uuid4())
        worker_agent_id = ""
        worker_session_id = ""

        async with _project_reclaim_runtime(
            tmp_path,
            "crashed-agent-create",
            settings=settings,
            bind_deletes=True,
        ) as first:
            storage, _, _, workspaces, _ = first
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            leader_agent, leader_session = await _native_session(storage, workspace_id)
            team = TeamRecord(
                user_id=USER,
                session_id=leader_session.id,
                leader_agent_id=leader_agent.id,
                data=TeamData(name="Crash window", description="AgentCreate durable ordering"),
            )
            await storage.upsert_team(USER, team)
            await AsyncSQLAlchemyStorage.set_session_team_id(storage, USER, leader_session.id, team.id)

            worker = AgentRecord(
                user_id=USER,
                source="team",
                data=leader_agent.data.model_copy(
                    update={"name": "worker", "system_prompt": "Durable worker"},
                ),
            )
            await storage.upsert_agent(USER, worker)
            worker_session = await storage.upsert_session(
                USER,
                worker.id,
                leader_session.config.model_copy(update={"name": "team worker"}),
                origin=TeamOrigin(),
            )
            await AsyncSQLAlchemyStorage.set_session_team_id(storage, USER, worker_session.id, team.id)
            worker_agent_id = worker.id
            worker_session_id = worker_session.id

            assert await storage.delete_session(USER, leader_agent.id, leader_session.id)
            assert await storage.get_team(USER, team.id) is None
            assert await storage.get_session(USER, worker.id, worker_session.id) is not None
            assert target.is_dir() and venv_bytes.is_file()

        async with _project_reclaim_runtime(
            tmp_path,
            "crashed-agent-create",
            settings=settings,
            bind_deletes=True,
        ) as second:
            storage, _, reclaimer, workspaces, _ = second
            await reclaimer.reconcile(USER)
            target = workspaces / workspace_id
            assert target.is_dir()
            assert await storage.get_session(USER, worker_agent_id, worker_session_id) is not None
            assert await storage.delete_session(USER, worker_agent_id, worker_session_id)
            assert not target.exists()

    asyncio.run(scenario())


def test_reference_reservation_and_session_commit_share_the_reclamation_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "reservation-race") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-reservation-race--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, existing = await _native_session(storage, workspace_id)
            assert await storage.delete_session(USER, agent.id, existing.id)
            reservations = manager._native_session_references
            real_reserve = reservations.reserve
            reservation_written = asyncio.Event()
            allow_commit = asyncio.Event()

            async def reserve_then_pause(
                user_id: str,
                agent_id: str,
                session_id: str,
                candidate: str,
            ) -> None:
                await real_reserve(user_id, agent_id, session_id, candidate)
                reservation_written.set()
                await allow_commit.wait()

            monkeypatch.setattr(reservations, "reserve", reserve_then_pause)
            upsert_task = asyncio.create_task(
                storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id)),
            )
            await asyncio.wait_for(reservation_written.wait(), timeout=5)
            reconcile_task = asyncio.create_task(reclaimer.reconcile(USER))
            await asyncio.sleep(0)
            assert not reconcile_task.done()
            allow_commit.set()
            replacement = await asyncio.wait_for(upsert_task, timeout=5)
            await asyncio.wait_for(reconcile_task, timeout=5)

            assert await storage.get_session(USER, agent.id, replacement.id) is not None
            assert target.is_dir() and venv_bytes.is_file()

    asyncio.run(scenario())


def test_project_storage_session_ids_honor_agentscope_public_factory(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "public-id-factory") as runtime:
            storage, _, _, _, _ = runtime
            workspace_id = session_workspace_id(f"published-id-factory--v-{DIGEST}", uuid.uuid4())
            agent, _ = await _native_session(storage, workspace_id)
            expected = "public-id-factory-value"
            set_id_factory(lambda: expected)
            try:
                created = await storage.upsert_session(
                    USER,
                    agent.id,
                    SessionConfig(workspace_id=workspace_id),
                )
            finally:
                set_id_factory(lambda: uuid.uuid4().hex)
            assert created.id == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("journal_state", ("missing", "corrupt"))
def test_upgrade_or_damaged_reference_journal_never_authorizes_orphan_delete(
    tmp_path: Path,
    journal_state: str,
) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, f"journal-{journal_state}")
        workspace_id = session_workspace_id(f"published-journal-state--v-{DIGEST}", uuid.uuid4())
        target, venv_bytes = _runtime_workspace(settings.workspaces_root, workspace_id)
        async with _project_reclaim_runtime(
            tmp_path,
            f"journal-{journal_state}",
            settings=settings,
        ) as legacy:
            legacy_storage, _, _, _, _ = legacy
            legacy_storage._workspace_reference_reservations = None
            agent, session = await _native_session(legacy_storage, workspace_id, source="team")
        journal = settings.workspaces_root / ".agentgov-native-session-references.json"
        if journal_state == "missing":
            journal.unlink()
        else:
            journal.write_bytes(b"{")

        async with _project_reclaim_runtime(
            tmp_path,
            f"journal-{journal_state}",
            settings=settings,
        ) as current:
            storage, manager, reclaimer, _, _ = current
            if journal_state == "corrupt":
                with pytest.raises(WorkspaceReclamationIntegrityError):
                    await reclaimer.reconcile(USER)
                with pytest.raises(RuntimeError, match="identity has been retired"):
                    await manager.get_workspace(USER, agent.id, session.id, workspace_id)
            else:
                await reclaimer.reconcile(USER)
            assert await storage.get_session(USER, agent.id, session.id) is not None
            assert target.is_dir() and venv_bytes.is_file()

    asyncio.run(scenario())


def test_existing_hidden_workspace_survives_crash_after_durable_confirm_before_memory_map(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, "upgrade-existing-workspace")
        agent_id = ""
        session_id = ""
        workspace_id = ""
        target = Path()
        venv_bytes = Path()
        async with _project_reclaim_runtime(
            tmp_path,
            "upgrade-existing-workspace",
            settings=settings,
        ) as first:
            storage, manager, _, workspaces, _ = first
            source = settings.candidates_root / "candidate-upgrade-existing/workspace"
            source.mkdir(parents=True)
            (source / "agent.yaml").write_text("schema_version: 1\n", encoding="utf-8")
            digest = harness_content_digest(source)
            workspace_id = session_workspace_id(f"candidate-upgrade-existing--v-{digest}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            (target / ".agentgov-runtime-workspace.json").write_text(
                json.dumps({"workspace_id": workspace_id, "harness_digest": digest}),
                encoding="utf-8",
            )
            agent, session = await _native_session(storage, workspace_id, source="team")
            agent_id, session_id = agent.id, session.id
            journal = workspaces / ".agentgov-native-session-references.json"
            journal.unlink()

            cached = AgentGovLocalWorkspace(
                workspace_id=workspace_id,
                host_workdir=str(target / ".agentgov-runtime-state"),
                host_cache_dir=str(target / ".agentgov-runtime-cache"),
                harness_root=source,
                expected_digest=digest,
            )
            manager._cache[workspace_id] = cached

            class CrashAfterDurableConfirm(dict[tuple[str, str, str], str]):
                def __setitem__(self, key: tuple[str, str, str], value: str) -> None:
                    del key, value
                    raise RuntimeError("injected crash before in-memory Workspace map")

            manager._session_workspaces = CrashAfterDurableConfirm()
            with pytest.raises(RuntimeError, match="injected crash"):
                await manager.get_workspace(USER, agent.id, session.id, workspace_id)
            assert workspace_id in await manager.reclaimable_workspace_ids()

        async with _project_reclaim_runtime(
            tmp_path,
            "upgrade-existing-workspace",
            settings=settings,
            bind_deletes=True,
        ) as second:
            storage, _, reclaimer, _, _ = second
            await reclaimer.reconcile(USER)
            assert await storage.get_session(USER, agent_id, session_id) is not None
            assert target.is_dir() and venv_bytes.is_file()
            assert await storage.delete_session(USER, agent_id, session_id)
            assert not target.exists() and not venv_bytes.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("journal_state", ("missing", "corrupt"))
def test_incomplete_reference_journal_defers_valid_hidden_session_tombstone(
    tmp_path: Path,
    journal_state: str,
) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, f"hidden-tombstone-{journal_state}")
        workspace_id = session_workspace_id(f"candidate-hidden-tombstone--v-{DIGEST}", uuid.uuid4())
        async with _project_reclaim_runtime(
            tmp_path,
            f"hidden-tombstone-{journal_state}",
            settings=settings,
        ) as first:
            storage, _, reclaimer, workspaces, _ = first
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, session = await _native_session(storage, workspace_id, source="team")
            relative_payload = venv_bytes.relative_to(target)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())
            journal = workspaces / ".agentgov-native-session-references.json"
            if journal_state == "missing":
                journal.unlink()
            else:
                journal.write_bytes(b"{")

        async with _project_reclaim_runtime(
            tmp_path,
            f"hidden-tombstone-{journal_state}",
            settings=settings,
            bind_deletes=True,
        ) as second:
            storage, manager, reclaimer, _, _ = second
            if journal_state == "corrupt":
                with pytest.raises(WorkspaceReclamationIntegrityError):
                    await reclaimer.reconcile(USER)
            else:
                await reclaimer.reconcile(USER)
            assert await storage.get_session(USER, agent.id, session.id) is not None
            assert not target.exists()
            assert tombstone.is_dir()
            assert (tombstone / relative_payload).is_file()
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await storage.upsert_session(
                    USER,
                    agent.id,
                    SessionConfig(workspace_id=workspace_id),
                )
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await manager.get_workspace(USER, agent.id, session.id, workspace_id)

            journal.unlink(missing_ok=True)

        async with _project_reclaim_runtime(
            tmp_path,
            f"hidden-tombstone-{journal_state}",
            settings=settings,
        ) as repaired:
            repaired_storage, _, _, _, _ = repaired
            unrelated_workspace = session_workspace_id(
                f"candidate-unrelated--v-{DIGEST}",
                uuid.uuid4(),
            )
            await _native_session(repaired_storage, unrelated_workspace)

        async with _project_reclaim_runtime(
            tmp_path,
            f"hidden-tombstone-{journal_state}",
            settings=settings,
        ) as third:
            storage, manager, reclaimer, _, _ = third
            await reclaimer.reconcile(USER)
            assert await storage.get_session(USER, agent.id, session.id) is not None
            assert tombstone.is_dir()
            assert (tombstone / relative_payload).is_file()
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await manager.get_workspace(USER, agent.id, session.id, workspace_id)

    asyncio.run(scenario())


@pytest.mark.parametrize("record_state", ("missing", "truncated", "symlink"))
def test_invalid_final_record_with_tombstone_blocks_exact_workspace_identity(
    tmp_path: Path,
    record_state: str,
) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, f"invalid-final-{record_state}")
        workspace_id = session_workspace_id(f"candidate-invalid-final--v-{DIGEST}", uuid.uuid4())
        outside = tmp_path / f"outside-{record_state}.json"
        async with _project_reclaim_runtime(
            tmp_path,
            f"invalid-final-{record_state}",
            settings=settings,
        ) as first:
            storage, _, reclaimer, workspaces, _ = first
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, session = await _native_session(storage, workspace_id, source="team")
            relative_payload = venv_bytes.relative_to(target)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())
            record = tombstone.parent / f"{tombstone.name}.json"
            if record_state == "missing":
                record.unlink()
            elif record_state == "truncated":
                record.write_bytes(b"{")
            else:
                record.unlink()
                outside.write_bytes(b"must survive")
                record.symlink_to(outside)

        async with _project_reclaim_runtime(
            tmp_path,
            f"invalid-final-{record_state}",
            settings=settings,
        ) as second:
            storage, manager, reclaimer, _, _ = second
            await reclaimer.reconcile(USER)
            assert await storage.get_session(USER, agent.id, session.id) is not None
            assert tombstone.is_dir()
            assert (tombstone / relative_payload).is_file()
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await storage.upsert_session(
                    USER,
                    agent.id,
                    SessionConfig(workspace_id=workspace_id),
                )
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await manager.get_workspace(USER, agent.id, session.id, workspace_id)
            if record_state == "symlink":
                assert outside.read_bytes() == b"must survive"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ("sidecar_scan", "native_inventory"))
def test_unprovable_reconcile_failure_blocks_all_workspace_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    async def scenario() -> None:
        settings = _project_runtime_settings(tmp_path, f"unprovable-{failure_stage}")
        workspace_id = session_workspace_id(f"candidate-unprovable--v-{DIGEST}", uuid.uuid4())
        async with _project_reclaim_runtime(
            tmp_path,
            f"unprovable-{failure_stage}",
            settings=settings,
        ) as first:
            storage, _, reclaimer, workspaces, _ = first
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, session = await _native_session(storage, workspace_id)
            relative_payload = venv_bytes.relative_to(target)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())

        async with _project_reclaim_runtime(
            tmp_path,
            f"unprovable-{failure_stage}",
            settings=settings,
        ) as second:
            storage, manager, reclaimer, _, _ = second
            if failure_stage == "sidecar_scan":
                monkeypatch.setattr(
                    reclaimer,
                    "_load_records",
                    lambda: (_ for _ in ()).throw(OSError("injected sidecar scan failure")),
                )
            else:

                async def fail_inventory(user_id: str):
                    del user_id
                    raise OSError("injected native inventory failure")

                monkeypatch.setattr(storage, "list_agents", fail_inventory)

            with pytest.raises(WorkspaceReclamationIntegrityError):
                await reclaimer.reconcile(USER)
            assert await storage.get_session(USER, agent.id, session.id) is not None
            assert tombstone.is_dir()
            assert (tombstone / relative_payload).is_file()
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await storage.upsert_session(
                    USER,
                    agent.id,
                    SessionConfig(workspace_id=workspace_id),
                )
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await manager.get_workspace(USER, agent.id, session.id, workspace_id)

    asyncio.run(scenario())


def test_repeated_delete_retries_deferred_workspace_reclamation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(
            tmp_path,
            "repeat-delete-reconcile",
            bind_deletes=True,
        ) as runtime:
            storage, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"candidate-repeat-delete--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            relative_payload = venv_bytes.relative_to(target)
            agent, session = await _native_session(storage, workspace_id)
            real_delete_tombstone = reclaimer._delete_tombstone
            failed = False

            def fail_once(key: str) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected first reclaim failure")
                real_delete_tombstone(key)

            monkeypatch.setattr(reclaimer, "_delete_tombstone", fail_once)
            assert await storage.delete_session(USER, agent.id, session.id)
            assert failed
            reclaim_root = workspaces / ".agentgov-session-workspace-reclaim"
            assert not target.exists()
            assert any(entry.is_dir() and (entry / relative_payload).is_file() for entry in reclaim_root.iterdir())

            monkeypatch.setattr(reclaimer, "_delete_tombstone", real_delete_tombstone)
            assert not await storage.delete_session(USER, agent.id, session.id)
            assert not target.exists() and not venv_bytes.exists()
            assert list(reclaim_root.iterdir()) == []

    asyncio.run(scenario())


def test_cancellation_after_exact_delete_completes_reference_reactivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "cancel-after-delete") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-cancel-delete--v-{DIGEST}", uuid.uuid4())
            target, _ = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            completion_entered = asyncio.Event()
            allow_completion = asyncio.Event()
            real_complete = manager.complete_session_workspace_reclamation

            async def blocked_complete(candidate: str) -> None:
                completion_entered.set()
                await allow_completion.wait()
                await real_complete(candidate)

            monkeypatch.setattr(manager, "complete_session_workspace_reclamation", blocked_complete)
            task = asyncio.create_task(reclaimer.release_disappeared(USER, before, after))
            await asyncio.wait_for(completion_entered.wait(), timeout=5)
            assert not target.exists()
            task.cancel()
            await asyncio.sleep(0)
            allow_completion.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

            replacement = await storage.upsert_session(
                USER,
                agent.id,
                SessionConfig(workspace_id=workspace_id),
            )
            assert replacement.config.workspace_id == workspace_id

    asyncio.run(scenario())


def test_cancellation_during_quarantine_settles_retirement_before_late_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "cancel-quarantine") as runtime:
            storage, manager, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-cancel-quarantine--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            before = await manager.snapshot_native_session_workspaces(USER)
            assert await storage.delete_session(USER, agent.id, deleted.id)
            after = await manager.snapshot_native_session_workspaces(USER)
            quarantine_entered = threading.Event()
            allow_quarantine = threading.Event()
            real_quarantine = reclaimer._quarantine

            def blocked_quarantine(
                candidate: str,
                ignored_bindings: frozenset[tuple[str, str]],
            ) -> Path:
                quarantine_entered.set()
                if not allow_quarantine.wait(timeout=5):
                    raise RuntimeError("quarantine cancellation test timed out")
                return real_quarantine(candidate, ignored_bindings)

            monkeypatch.setattr(reclaimer, "_quarantine", blocked_quarantine)
            release_task = asyncio.create_task(reclaimer.release_disappeared(USER, before, after))
            assert await asyncio.to_thread(quarantine_entered.wait, 5)
            release_task.cancel()
            upsert_task = asyncio.create_task(
                storage.upsert_session(USER, agent.id, SessionConfig(workspace_id=workspace_id)),
            )
            await asyncio.sleep(0)
            assert not upsert_task.done()
            allow_quarantine.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(release_task, timeout=5)
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await asyncio.wait_for(upsert_task, timeout=5)
            assert not target.exists() and not venv_bytes.exists()
            assert any(path.is_dir() for path in (workspaces / ".agentgov-session-workspace-reclaim").iterdir())

            monkeypatch.setattr(reclaimer, "_quarantine", real_quarantine)
            await reclaimer.reconcile(USER)
            replacement = await storage.upsert_session(
                USER,
                agent.id,
                SessionConfig(workspace_id=workspace_id),
            )
            assert replacement.config.workspace_id == workspace_id

    asyncio.run(scenario())


def test_delete_commit_then_cancellation_still_completes_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(
            tmp_path,
            "cancel-delete-commit",
            bind_deletes=True,
        ) as runtime:
            storage, _, _, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-cancel-commit--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, deleted = await _native_session(storage, workspace_id)
            committed = asyncio.Event()
            allow_return = asyncio.Event()
            real_delete = AsyncSQLAlchemyStorage.delete_session

            async def commit_then_pause(
                current: AsyncSQLAlchemyStorage,
                user_id: str,
                agent_id: str,
                session_id: str,
            ) -> bool:
                result = await real_delete(current, user_id, agent_id, session_id)
                committed.set()
                await allow_return.wait()
                return result

            monkeypatch.setattr(AsyncSQLAlchemyStorage, "delete_session", commit_then_pause)
            task = asyncio.create_task(storage.delete_session(USER, agent.id, deleted.id))
            await asyncio.wait_for(committed.wait(), timeout=5)
            task.cancel()
            allow_return.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

            assert await storage.get_session(USER, agent.id, deleted.id) is None
            assert not target.exists() and not venv_bytes.exists()

    asyncio.run(scenario())


def test_restart_restore_failure_retires_identity_before_new_session_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with _project_reclaim_runtime(tmp_path, "restart-restore-failure") as runtime:
            storage, _, reclaimer, workspaces, _ = runtime
            workspace_id = session_workspace_id(f"published-restore-failure--v-{DIGEST}", uuid.uuid4())
            target, venv_bytes = _runtime_workspace(workspaces, workspace_id)
            agent, durable = await _native_session(storage, workspace_id)
            tombstone = reclaimer._quarantine(workspace_id, frozenset())

            def fail_restore(candidate: str) -> None:
                assert candidate == workspace_id
                raise OSError("injected startup restore failure")

            monkeypatch.setattr(reclaimer, "_restore_tombstone", fail_restore)
            await reclaimer.reconcile(USER)

            assert await storage.get_session(USER, agent.id, durable.id) is not None
            assert tombstone.is_dir()
            assert (tombstone / venv_bytes.relative_to(target)).is_file()
            assert not target.exists()
            with pytest.raises(RuntimeError, match="identity has been retired"):
                await storage.upsert_session(
                    USER,
                    agent.id,
                    SessionConfig(workspace_id=workspace_id),
                )

    asyncio.run(scenario())
