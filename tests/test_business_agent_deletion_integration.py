from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from app.agent_testing.schedule import AgentTestScheduleStore
from app.agent_testing.service import AgentTestingService
from app.agent_testing.store import AgentTestingStore
from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_admission import (
    AgentMaintenanceActiveError,
    acquire_maintenance,
    claim_runtime_admission,
    is_maintenance_active,
)
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import business_agent_layout, business_agent_repository_lock_path
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.business_agent_lifecycle import BusinessAgentLifecycleFenceError
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import ConflictError, DataIntegrityError, RuntimeUnavailableError, SessionConflictError
from app.runtime.runtime_db import (
    AgentRunModel,
    SdkSessionEntryModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    make_session_factory,
    runtime_db_path_from_data_dir,
)
from app.runtime.runtime_initialization import _runtime_agent_ids, _store_for
from app.runtime.sdk_session_migration import ensure_sdk_store_ready
from app.runtime.sdk_session_store import SqliteSdkSessionStore
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_deletion_store import AgentDeletionStore
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.services.business_agent_deletion import BusinessAgentDeletionError, BusinessAgentDeletionService
from sqlalchemy import select


@dataclass
class _IntegrationHarness:
    data_dir: Path
    agent_id: str
    record: AgentRegistryRecord
    registry: AgentRegistryStore
    testing: AgentTestingStore
    schedules: AgentTestScheduleStore
    deletion_store: AgentDeletionStore
    deletion: BusinessAgentDeletionService

    @property
    def layout_root(self) -> Path:
        return business_agent_layout(self.data_dir, self.agent_id).root

    def delete(self, *, key: str = "integration-delete"):
        return self.deletion.delete(
            agent_id=self.agent_id,
            agent_instance_etag=self.record.instance_etag,
            idempotency_key=key,
        )


def _harness(tmp_path: Path) -> _IntegrationHarness:
    data_dir = tmp_path / "data"
    factory = make_session_factory(runtime_db_path_from_data_dir(data_dir))
    registry = AgentRegistryStore(factory)
    agent_id = "deletion-integration"
    layout = business_agent_layout(data_dir, agent_id)
    layout.workspace.mkdir(parents=True)
    layout.workspace.joinpath("CLAUDE.md").write_text("private\n", encoding="utf-8")
    layout.claude_root.mkdir()
    layout.version_base.mkdir()
    record = registry.create_business_agent(name="Deletion integration", agent_id=agent_id, workspace_dir=str(layout.workspace))
    deletion_store = AgentDeletionStore(factory, data_dir=data_dir)
    deletion = BusinessAgentDeletionService(
        deletion_store,
        data_dir=data_dir,
        mutation_guard_for=lambda value: advisory_lock(
            business_agent_repository_lock_path(data_dir, value),
            mode="exclusive",
        ),
    )
    return _IntegrationHarness(
        data_dir=data_dir,
        agent_id=agent_id,
        record=record,
        registry=registry,
        testing=AgentTestingStore(factory),
        schedules=AgentTestScheduleStore(factory),
        deletion_store=deletion_store,
        deletion=deletion,
    )


def _git_store(harness: _IntegrationHarness) -> GitAgentVersionStore:
    layout = business_agent_layout(harness.data_dir, harness.agent_id)
    return GitAgentVersionStore(
        repository_dir=layout.workspace,
        worktrees_dir=layout.version_base / "worktrees",
        releases_dir=layout.version_base / "releases",
        process_lock_path=business_agent_repository_lock_path(harness.data_dir, harness.agent_id),
        mutation_precondition=harness.registry.mutation_precondition(
            agent_id=harness.agent_id,
            expected_instance_etag=harness.record.instance_etag,
        ),
        activation_precondition=harness.registry.mutation_precondition(
            agent_id=harness.agent_id,
            expected_instance_etag=harness.record.instance_etag,
            allow_workspace_activation=True,
        ),
    )


def test_repository_writer_holds_stable_lock_before_deletion(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    store = _git_store(harness)
    store.ensure_bootstrap()
    started = Event()

    def delete_after_start():
        started.set()
        return harness.delete()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.mutation_guard():
            pending = executor.submit(delete_after_start)
            assert started.wait(timeout=5)
            assert not pending.done()
        completed = pending.result(timeout=10)

    assert completed.state == "completed"
    assert not harness.layout_root.exists()


def test_cached_waiter_and_fresh_constructor_cannot_revive_deleted_root(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    cached = _git_store(harness)
    cached.ensure_bootstrap()
    lock_path = business_agent_repository_lock_path(harness.data_dir, harness.agent_id)
    started = Event()

    def waiting_writer() -> None:
        started.set()
        cached.ensure_bootstrap()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with advisory_lock(lock_path, mode="exclusive"):
            pending = executor.submit(waiting_writer)
            assert started.wait(timeout=5)
            completed = harness.delete()
            assert completed.state == "completed"
        with pytest.raises(AgentGitError, match="no longer mutable"):
            pending.result(timeout=10)

    fresh = _git_store(harness)
    assert not harness.layout_root.exists()
    with pytest.raises(AgentGitError, match="no longer mutable"):
        fresh.ensure_bootstrap()
    assert not harness.layout_root.exists()


def test_existing_repository_guard_never_recreates_missing_active_layout(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    store = _git_store(harness)
    store.ensure_bootstrap()
    moved = harness.data_dir / "moved-active-layout"
    harness.layout_root.rename(moved)

    with pytest.raises(AgentGitError, match="authority is missing"):
        with store.mutation_guard():
            raise AssertionError("unreachable")

    assert not harness.layout_root.exists()
    assert moved.joinpath("workspace/CLAUDE.md").read_text(encoding="utf-8") == "private\n"


def test_test_enqueue_wins_lock_then_deletion_observes_queued_blocker(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    store = _git_store(harness)
    store.ensure_bootstrap()
    started = Event()

    def delete_after_start():
        started.set()
        return harness.delete(key="test-run-blocker")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.mutation_guard():
            run = harness.testing.create_run(
                agent_id=harness.agent_id,
                commit_sha="a" * 40,
                change_set_id=None,
                source="manual",
                command=["pytest"],
                suite={},
                suite_digest=None,
            )
            pending = executor.submit(delete_after_start)
            assert started.wait(timeout=5)
            assert not pending.done()
        with pytest.raises(BusinessAgentDeletionError, match="queued or running Agent test"):
            pending.result(timeout=10)

    assert run["status"] == "queued"
    assert harness.registry.get_agent(harness.agent_id) is not None


def test_deletion_wins_lock_then_waiting_test_enqueue_cannot_persist(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    store = _git_store(harness)
    store.ensure_bootstrap()
    lock_path = business_agent_repository_lock_path(harness.data_dir, harness.agent_id)
    started = Event()

    def waiting_enqueue() -> None:
        started.set()
        with store.mutation_guard():
            harness.testing.create_run(
                agent_id=harness.agent_id,
                commit_sha="a" * 40,
                change_set_id=None,
                source="manual",
                command=["pytest"],
                suite={},
                suite_digest=None,
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with advisory_lock(lock_path, mode="exclusive"):
            pending = executor.submit(waiting_enqueue)
            assert started.wait(timeout=5)
            assert not pending.done()
            assert harness.delete(key="deletion-wins-test-race").state == "completed"
        with pytest.raises(AgentGitError, match="no longer mutable"):
            pending.result(timeout=10)

    assert harness.testing.list_runs(agent_id=harness.agent_id) == []
    assert not harness.layout_root.exists()


def test_pending_deletion_fences_runtime_tests_schedules_and_initialization(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    operation = harness.deletion_store.begin(
        agent_id=harness.agent_id,
        agent_instance_etag=harness.record.instance_etag,
        idempotency_key="pending-fence",
    )
    factory = harness.deletion_store._session_factory
    with factory.begin() as db:
        with pytest.raises(BusinessAgentLifecycleFenceError, match="not available"):
            claim_runtime_admission(
                db,
                agent_id=harness.agent_id,
                expected_instance_etag=harness.record.instance_etag,
            )
    with pytest.raises(AgentMaintenanceActiveError, match="deletion cleanup"):
        acquire_maintenance(factory, agent_id=harness.agent_id, kind="import", owner_id="test", lease_seconds=60)
    assert is_maintenance_active(factory, agent_id=harness.agent_id)
    with pytest.raises(BusinessAgentLifecycleFenceError):
        harness.testing.create_run(
            agent_id=harness.agent_id,
            commit_sha="a" * 40,
            change_set_id=None,
            source="manual",
            command=["pytest"],
            suite={},
            suite_digest=None,
        )
    with pytest.raises(BusinessAgentLifecycleFenceError):
        harness.schedules.upsert_schedule(
            agent_id=harness.agent_id,
            enabled=True,
            cron_expression="0 2 * * *",
            timezone_name="UTC",
            next_run_at="2099-01-01T00:00:00+00:00",
        )
    settings = _RuntimeSettings(harness.data_dir)
    assert harness.agent_id not in _runtime_agent_ids(settings)
    with pytest.raises(AgentGitError, match="no longer mutable"):
        _store_for(settings, harness.agent_id).ensure_bootstrap()
    assert operation.workspace_path.joinpath("workspace/CLAUDE.md").exists()


def test_completed_deletion_between_runtime_precheck_and_admission_creates_no_turn(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    sessions = LocalSessionStore(harness.data_dir / "sessions")
    candidate = sessions.prepare_owned_session("deletion-race", agent_id=harness.agent_id)
    assert sessions.get("deletion-race") is None
    precheck_returned = Event()
    deletion_completed = Event()

    def admit_after_precheck():
        expected_instance_etag = sessions.public_business_agent_instance_etag(harness.agent_id)
        precheck_returned.set()
        assert deletion_completed.wait(timeout=5)
        return sessions.begin_persisted_turn(
            candidate.session,
            run_id="deletion-race-run",
            agent_id=harness.agent_id,
            expected_instance_etag=expected_instance_etag,
            new_sdk_session_id="deletion-race-sdk",
            sdk_project_key="deletion-race-project",
            resolve_agent_version_id=lambda: "version-before-delete",
            request={"message": "must not start"},
            created_at="2026-08-13T00:00:00+00:00",
            create_session_if_missing=candidate.create_if_missing,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(admit_after_precheck)
        assert precheck_returned.wait(timeout=5)
        assert harness.delete(key="delete-after-runtime-precheck").state == "completed"
        deletion_completed.set()
        with pytest.raises(BusinessAgentLifecycleFenceError):
            pending.result(timeout=10)

    assert sessions.get("deletion-race") is None
    with sessions.Session() as db:
        assert db.get(SessionRecordModel, "deletion-race") is None
        assert db.get(SessionTurnIntentModel, "deletion-race-run") is None
        assert db.get(AgentRunModel, "deletion-race-run") is None


def test_runtime_admission_rejects_stale_public_instance_etag_without_turn(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    sessions = LocalSessionStore(harness.data_dir / "sessions")
    candidate = sessions.prepare_owned_session("stale-instance", agent_id=harness.agent_id)

    with sessions.Session.begin() as db:
        row = db.get(AgentRegistryModel, harness.agent_id)
        assert row is not None
        row.provision_completed_token = "replacement-public-instance"

    with pytest.raises(BusinessAgentLifecycleFenceError, match="instance changed"):
        sessions.begin_persisted_turn(
            candidate.session,
            run_id="stale-instance-run",
            agent_id=harness.agent_id,
            expected_instance_etag=harness.record.instance_etag,
            new_sdk_session_id="stale-instance-sdk",
            sdk_project_key="stale-instance-project",
            resolve_agent_version_id=lambda: "must-not-resolve",
            request={"message": "must not start"},
            created_at="2026-08-13T00:00:00+00:00",
            create_session_if_missing=candidate.create_if_missing,
        )

    with sessions.Session() as db:
        assert db.get(SessionRecordModel, "stale-instance") is None
        assert db.get(SessionTurnIntentModel, "stale-instance-run") is None
        assert db.get(AgentRunModel, "stale-instance-run") is None


def test_test_session_invoke_loses_to_completed_deletion_before_runtime_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    git_store = _git_store(harness)
    git_store.ensure_bootstrap()
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=harness.data_dir,
        GOVERNOR_CLAUDE_ROOT=tmp_path / "governor-claude-root",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    sessions = LocalSessionStore(settings.session_dir)
    runtime = ClaudeRuntime(settings, sessions)
    testing = AgentTestingService(
        store=harness.testing,
        store_for=lambda _agent_id: git_store,
        agent_exists=lambda agent_id: harness.registry.get_agent(agent_id) is not None,
        get_change_set=lambda _change_set_id: None,
        run_candidate=runtime.run_candidate,
        artifacts_dir=tmp_path / "agent-testing",
    )
    test_session = testing.create_session(
        agent_id=harness.agent_id,
        commit_sha=str(git_store.current_commit_sha()),
        change_set_id=None,
    )
    precheck_returned = Event()
    allow_admission = Event()
    original_precheck = sessions.public_business_agent_instance_etag

    def paused_precheck(agent_id: str) -> str:
        instance_etag = original_precheck(agent_id)
        precheck_returned.set()
        assert allow_admission.wait(timeout=5)
        return instance_etag

    monkeypatch.setattr(sessions, "public_business_agent_instance_etag", paused_precheck)

    async def race() -> BaseException:
        invoke = asyncio.create_task(
            testing.invoke(
                str(test_session["test_session_id"]),
                message="must not run",
                metadata={},
            )
        )
        assert await asyncio.to_thread(precheck_returned.wait, 5)
        completed = await asyncio.to_thread(
            harness.delete,
            key="delete-during-test-session-invoke",
        )
        assert completed.state == "completed"
        allow_admission.set()
        result = (await asyncio.gather(invoke, return_exceptions=True))[0]
        assert isinstance(result, BaseException)
        return result

    try:
        error = asyncio.run(race())
    finally:
        allow_admission.set()
        testing.close()

    assert isinstance(error, RuntimeUnavailableError)
    with sessions.Session() as db:
        assert db.get(SessionRecordModel, f"agent-test-{test_session['test_session_id']}") is None
        assert list(db.query(SessionTurnIntentModel).filter(SessionTurnIntentModel.agent_id == harness.agent_id)) == []
        assert list(db.query(AgentRunModel)) == []


def _legacy_import_claim(harness: _IntegrationHarness):
    sessions = LocalSessionStore(harness.data_dir / "sessions")
    session = sessions.get_or_create_owned("legacy-import-race", agent_id=harness.agent_id)
    session.sdk_session_id = "legacy-sdk-session"
    sessions.save(session)
    claim = sessions.begin_sdk_store_import(
        session_id=session.session_id,
        expected_instance_etag=harness.record.instance_etag,
        sdk_session_id="legacy-sdk-session",
        sdk_project_key="legacy-project-key",
    )
    assert claim is not None
    adapter = SqliteSdkSessionStore.for_import(
        sessions.Session,
        project_key=claim.sdk_project_key,
        sdk_session_id=claim.sdk_session_id,
        import_id=claim.token,
        session_id=claim.session_id,
        agent_id=claim.agent_id,
        expected_instance_etag=claim.expected_instance_etag,
        claim_marker=claim.marker,
    )
    return sessions, claim, adapter


def test_legacy_import_append_after_completed_deletion_is_rejected_without_rows(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    sessions, claim, adapter = _legacy_import_claim(harness)
    assert harness.delete(key="delete-before-legacy-append").state == "completed"

    with pytest.raises(SessionConflictError, match="migration fence was lost"):
        asyncio.run(
            adapter.append(
                {"project_key": claim.sdk_project_key, "session_id": claim.sdk_session_id},
                [{"type": "user", "uuid": "must-not-stage-after-delete"}],
            )
        )
    with pytest.raises(SessionConflictError, match="migration fence was lost"):
        asyncio.run(adapter.load({"project_key": claim.sdk_project_key, "session_id": claim.sdk_session_id}))
    with pytest.raises(SessionConflictError, match="migration fence was lost"):
        asyncio.run(adapter.list_subkeys({"project_key": claim.sdk_project_key, "session_id": claim.sdk_session_id}))

    with sessions.Session() as db:
        assert db.query(SdkSessionEntryModel).filter_by(entry_uuid="must-not-stage-after-delete").count() == 0


def test_completed_deletion_between_import_etag_read_and_claim_has_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    sessions = LocalSessionStore(harness.data_dir / "sessions")
    session = sessions.get_or_create_owned("legacy-import-claim-race", agent_id=harness.agent_id)
    session.sdk_session_id = "legacy-claim-sdk"
    sessions.save(session)
    persisted = sessions.get(session.session_id)
    assert persisted is not None
    etag_read = Event()
    allow_claim = Event()
    original_etag_read = sessions.public_business_agent_instance_etag

    def paused_etag_read(agent_id: str) -> str:
        etag = original_etag_read(agent_id)
        etag_read.set()
        assert allow_claim.wait(timeout=10)
        return etag

    monkeypatch.setattr(sessions, "public_business_agent_instance_etag", paused_etag_read)

    def migrate() -> None:
        asyncio.run(
            ensure_sdk_store_ready(
                sessions,
                persisted,
                workspace_dir=harness.record.workspace_dir,
                claude_config_dir=tmp_path / "claude-config",
            )
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        migration = executor.submit(migrate)
        assert etag_read.wait(timeout=5)
        assert harness.delete(key="delete-before-import-claim").state == "completed"
        allow_claim.set()
        with pytest.raises(RuntimeUnavailableError):
            migration.result(timeout=10)
    with sessions.Session() as db:
        record = db.get(SessionRecordModel, session.session_id)
        assert record is not None
        assert record.sdk_session_id is None
        assert record.sdk_store_migration_error is None
        assert db.query(SdkSessionEntryModel).count() == 0
        assert db.query(SessionTurnIntentModel).count() == 0
        assert db.query(AgentRunModel).count() == 0


def test_legacy_import_append_wins_then_deletion_discards_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    sessions, claim, adapter = _legacy_import_claim(harness)
    append_has_write_lock = Event()
    allow_append = Event()
    deletion_started = Event()
    original_append_entry = adapter._append_entry

    def paused_append_entry(*args, **kwargs):
        append_has_write_lock.set()
        assert allow_append.wait(timeout=5)
        return original_append_entry(*args, **kwargs)

    monkeypatch.setattr(adapter, "_append_entry", paused_append_entry)

    def append_entry() -> None:
        asyncio.run(
            adapter.append(
                {"project_key": claim.sdk_project_key, "session_id": claim.sdk_session_id},
                [{"type": "user", "uuid": "append-before-delete"}],
            )
        )

    def delete_after_append_lock():
        deletion_started.set()
        return harness.delete(key="delete-after-legacy-append")

    with ThreadPoolExecutor(max_workers=2) as executor:
        append_pending = executor.submit(append_entry)
        assert append_has_write_lock.wait(timeout=5)
        delete_pending = executor.submit(delete_after_append_lock)
        assert deletion_started.wait(timeout=5)
        assert not delete_pending.done()
        allow_append.set()
        append_pending.result(timeout=10)
        assert delete_pending.result(timeout=10).state == "completed"

    with sessions.Session() as db:
        staged = db.query(SdkSessionEntryModel).filter_by(entry_uuid="append-before-delete").one()
        assert staged.committed_at is None
        assert staged.discarded_at is not None


def test_cancelled_legacy_import_cannot_append_after_completed_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    sessions = LocalSessionStore(harness.data_dir / "sessions")
    session = sessions.get_or_create_owned("cancelled-legacy-import", agent_id=harness.agent_id)
    session.sdk_session_id = "cancelled-legacy-sdk"
    sessions.save(session)
    persisted = sessions.get(session.session_id)
    assert persisted is not None
    first_append_done = Event()
    allow_late_append = Event()
    importer_done = Event()
    late_errors: list[BaseException] = []

    async def cancelled_import(_sdk_session_id, adapter, **_kwargs):
        key = {"project_key": adapter.binding.project_key, "session_id": adapter.binding.sdk_session_id}
        await adapter.append(key, [{"type": "user", "uuid": "before-import-cancel"}])
        first_append_done.set()
        await asyncio.to_thread(allow_late_append.wait, 5)
        try:
            await adapter.append(key, [{"type": "user", "uuid": "after-import-cancel"}])
        except BaseException as exc:
            late_errors.append(exc)
        finally:
            importer_done.set()

    monkeypatch.setattr("app.runtime.sdk_session_migration.sdk.import_session_to_store", cancelled_import)

    async def cancel_then_delete() -> None:
        task = asyncio.create_task(
            ensure_sdk_store_ready(
                sessions,
                persisted,
                workspace_dir=harness.record.workspace_dir,
                claude_config_dir=tmp_path / "claude-config",
            )
        )
        assert await asyncio.to_thread(first_append_done.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        completed = await asyncio.to_thread(harness.delete, key="delete-after-import-cancel")
        assert completed.state == "completed"
        allow_late_append.set()
        assert await asyncio.to_thread(importer_done.wait, 5)

    try:
        asyncio.run(cancel_then_delete())
    finally:
        allow_late_append.set()

    assert len(late_errors) == 1 and isinstance(late_errors[0], SessionConflictError)
    with sessions.Session() as db:
        first = db.query(SdkSessionEntryModel).filter_by(entry_uuid="before-import-cancel").one()
        assert first.discarded_at is not None
        assert db.query(SdkSessionEntryModel).filter_by(entry_uuid="after-import-cancel").count() == 0


def test_completed_deletion_permanently_reserves_id_but_different_id_can_be_created(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    assert harness.delete(key="permanent-tombstone").state == "completed"
    with pytest.raises(ConflictError, match="already reserved"):
        harness.registry.reserve_business_agent(
            name="Replacement",
            agent_id=harness.agent_id,
            workspace_dir=str(business_agent_layout(harness.data_dir, harness.agent_id).workspace),
        )
    with pytest.raises(ConflictError, match="already reserved"):
        harness.registry.create_business_agent(
            name="Replacement",
            agent_id=harness.agent_id,
            workspace_dir=str(business_agent_layout(harness.data_dir, harness.agent_id).workspace),
        )
    different = business_agent_layout(harness.data_dir, "different-id")
    created = harness.registry.create_business_agent(
        name="Different",
        agent_id="different-id",
        workspace_dir=str(different.workspace),
    )
    assert created.agent_id == "different-id"


def test_registry_read_missing_ready_token_fails_without_rotating_identity(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    factory = make_session_factory(runtime_db_path_from_data_dir(data_dir))
    registry = AgentRegistryStore(factory)
    with factory.begin() as db:
        db.add(
            AgentRegistryModel(
                agent_id="missing-token",
                name="Missing token",
                category="business",
                workspace_dir=str(data_dir / "business-agents/missing-token/workspace"),
                created_at="2026-08-09T00:00:00+00:00",
                provision_state="ready",
                provision_completed_token=None,
            )
        )
    with pytest.raises(DataIntegrityError, match="token is missing"):
        registry.get_agent("missing-token")
    with pytest.raises(DataIntegrityError, match="token is missing"):
        registry.list_agents()
    with factory() as db:
        assert db.scalar(select(AgentRegistryModel.provision_completed_token).where(AgentRegistryModel.agent_id == "missing-token")) is None


class _RuntimeSettings:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime_volume_mode = "named"
        self.runtime_db_path = runtime_db_path_from_data_dir(data_dir)
        self.agent_git_user_name = "AgentGov"
        self.agent_git_user_email = "agent-runtime@example.local"
