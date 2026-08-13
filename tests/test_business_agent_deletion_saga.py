from __future__ import annotations

import errno
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from app.runtime.agent_deletion_db import AgentDeletionOperationModel
from app.runtime.agent_deletion_fs import purge_quarantined_agent_layout, quarantine_agent_layout
from app.runtime.agent_maintenance_db import AgentAdmissionStateModel
from app.runtime.agent_registry_db import AgentRegistryModel
from app.runtime.runtime_db import SessionRecordModel
from app.runtime.stores.agent_deletion_store import (
    AgentDeletionStore,
    AgentDeletionStoreError,
    business_agent_instance_etag,
)
from app.services.business_agent_deletion import BusinessAgentDeletionError, BusinessAgentDeletionService
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from tests.business_agent_deletion_saga_test_support import (
    DeletionSagaHarness as _Harness,
)
from tests.business_agent_deletion_saga_test_support import (
    active_activation as _active_activation,
)
from tests.business_agent_deletion_saga_test_support import (
    active_change_set as _active_change_set,
)
from tests.business_agent_deletion_saga_test_support import active_release as _active_release
from tests.business_agent_deletion_saga_test_support import active_test as _active_test
from tests.business_agent_deletion_saga_test_support import (
    add_running_turn_intent as _add_running_turn_intent,
)
from tests.business_agent_deletion_saga_test_support import build_deletion_saga_harness
from tests.business_agent_deletion_saga_test_support import (
    complete_without_witness_cleanup as _complete_without_witness_cleanup,
)
from tests.business_agent_deletion_saga_test_support import operation_row as _operation_row
from tests.business_agent_deletion_saga_test_support import pending_cleanup as _pending_cleanup
from tests.business_agent_deletion_saga_test_support import waiting_hitl as _waiting_hitl


@pytest.fixture(name="harness")
def _harness_fixture(tmp_path: Path) -> _Harness:
    return build_deletion_saga_harness(tmp_path)


def test_exact_idempotency_retry_and_conflict_are_instance_scoped(harness: _Harness) -> None:
    first = harness.delete()
    retry = harness.delete()

    assert first.state == retry.state == "completed"
    assert first.operation_id == retry.operation_id
    assert first.agent_instance_etag == business_agent_instance_etag(harness.instance_token)
    assert harness.instance_token not in first.agent_instance_etag
    assert not harness.layout_root.exists()
    with pytest.raises(BusinessAgentDeletionError, match="different Agent instance"):
        harness.service.delete(
            agent_id=harness.agent_id,
            agent_instance_etag=business_agent_instance_etag("new-instance"),
            idempotency_key="delete-key",
        )


def test_reconciler_completes_crash_after_atomic_quarantine(harness: _Harness) -> None:
    operation = harness.begin()
    operation.quarantine_path.parent.mkdir(mode=0o700)
    os.rename(operation.workspace_path, operation.quarantine_path)

    result = harness.service.reconcile()
    second = harness.service.reconcile()

    assert result == {"completed": 1, "cleanup_pending": 0}
    assert second == {"completed": 0, "cleanup_pending": 0}
    assert harness.store.get(operation.operation_id).state == "completed"  # type: ignore[union-attr]
    assert not operation.quarantine_path.exists()


def test_reconciler_completes_crash_after_purge_before_db_commit(harness: _Harness) -> None:
    operation = harness.begin()
    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    operation = harness.store.confirm_quarantine(operation.operation_id)
    purged = purge_quarantined_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert purged.state == "completed"
    assert operation.quarantine_path.is_dir()
    assert harness.store.get(operation.operation_id).state == "cleanup_pending"  # type: ignore[union-attr]

    completed = harness.service.reconcile_operation(operation)

    assert completed.state == "completed"
    assert not operation.quarantine_path.exists()


def test_store_refuses_terminal_state_before_quarantine_and_purge(harness: _Harness) -> None:
    operation = harness.begin()

    with pytest.raises(AgentDeletionStoreError, match="cannot complete"):
        harness.store.complete(operation.operation_id)

    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    harness.store.confirm_quarantine(operation.operation_id)
    with pytest.raises(AgentDeletionStoreError, match="cannot complete"):
        harness.store.complete(operation.operation_id)


def test_confirmed_quarantine_moved_elsewhere_never_completes(harness: _Harness) -> None:
    operation = harness.begin()
    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    operation = harness.store.confirm_quarantine(operation.operation_id)
    moved = harness.data_dir / "moved-confirmed-quarantine"
    os.rename(operation.quarantine_path, moved)

    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert result.purge_confirmed is False
    assert moved.joinpath("workspace/CLAUDE.md").read_text(encoding="utf-8") == "private\n"


def test_completed_retry_removes_empty_quarantine_witness(harness: _Harness) -> None:
    completed = _complete_without_witness_cleanup(harness, key="witness-retry")
    assert completed.state == "completed" and completed.witness_removed is False
    assert completed.quarantine_path.is_dir()

    retry = harness.delete(key="witness-retry")

    assert retry.state == "completed" and retry.witness_removed is True
    assert not completed.quarantine_path.exists()


def test_startup_reconcile_removes_completed_empty_witness(harness: _Harness) -> None:
    completed = _complete_without_witness_cleanup(harness, key="witness-startup")
    assert completed.quarantine_path.is_dir()

    summary = harness.service.reconcile()

    assert summary == {"completed": 0, "cleanup_pending": 0}
    assert harness.store.get(completed.operation_id).witness_removed is True  # type: ignore[union-attr]
    assert not completed.quarantine_path.exists()


def test_completion_ack_loss_is_resolved_by_operation_reread(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    operation = harness.begin()
    complete = harness.store.complete

    def complete_then_lose_ack(operation_id: str):
        complete(operation_id)
        raise SQLAlchemyError("ack lost")

    monkeypatch.setattr(harness.store, "complete", complete_then_lose_ack)
    result = harness.service.reconcile_operation(operation)

    assert result.state == "completed"
    assert harness.store.get(operation.operation_id).state == "completed"  # type: ignore[union-attr]


def test_begin_commit_ack_loss_is_resolved_by_exact_idempotency_reread(harness: _Harness) -> None:
    engine = harness.session_factory.kw["bind"]

    class _CommitAckLossSession(Session):
        lose_next_ack = True

        def commit(self) -> None:
            super().commit()
            if self.lose_next_ack:
                type(self).lose_next_ack = False
                raise RuntimeError("commit acknowledgement lost")

    ack_loss_factory = sessionmaker(
        bind=engine,
        class_=_CommitAckLossSession,
        expire_on_commit=False,
        future=True,
    )
    store = AgentDeletionStore(ack_loss_factory, data_dir=harness.data_dir)
    service = BusinessAgentDeletionService(store, data_dir=harness.data_dir)

    result = service.delete(
        agent_id=harness.agent_id,
        agent_instance_etag=business_agent_instance_etag(harness.instance_token),
        idempotency_key="begin-ack-loss",
    )

    assert result.state == "completed"
    assert store.get_by_idempotency_key("begin-ack-loss").operation_id == result.operation_id  # type: ignore[union-attr]


def test_persistent_failure_ack_loss_rereads_once_without_recursion(harness: _Harness) -> None:
    operation = harness.begin()
    engine = harness.session_factory.kw["bind"]

    class _AlwaysLoseCommitAckSession(Session):
        def commit(self) -> None:
            super().commit()
            raise RuntimeError("commit acknowledgement lost")

    ack_loss_factory = sessionmaker(
        bind=engine,
        class_=_AlwaysLoseCommitAckSession,
        expire_on_commit=False,
        future=True,
    )
    store = AgentDeletionStore(ack_loss_factory, data_dir=harness.data_dir)

    result = store.record_cleanup_failure(operation.operation_id, error_code="EXPECTED_PENDING")

    assert result.state == "cleanup_pending"
    assert result.error["error_code"] == "EXPECTED_PENDING"


def test_inode_replacement_never_deletes_original_or_replacement(harness: _Harness) -> None:
    operation = harness.begin()
    original = harness.data_dir / "original-layout"
    os.rename(operation.workspace_path, original)
    operation.workspace_path.mkdir()
    operation.workspace_path.joinpath("replacement.txt").write_text("replacement\n", encoding="utf-8")

    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert original.joinpath("workspace/CLAUDE.md").read_text(encoding="utf-8") == "private\n"
    assert operation.workspace_path.joinpath("replacement.txt").exists()


def test_unjournaled_external_move_is_ambiguous_and_never_reported_complete(harness: _Harness) -> None:
    operation = harness.begin()
    externally_moved = harness.data_dir / "externally-moved-layout"
    os.rename(operation.workspace_path, externally_moved)

    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert result.quarantine_confirmed is False
    assert result.error["error_code"] == "AGENT_DELETION_SOURCE_MISSING_BEFORE_QUARANTINE"
    assert externally_moved.joinpath("workspace/CLAUDE.md").exists()


def test_symlink_replacement_never_follows_external_target(harness: _Harness) -> None:
    operation = harness.begin()
    original = harness.data_dir / "original-layout"
    outside = harness.data_dir / "outside"
    outside.mkdir()
    outside.joinpath("keep.txt").write_text("keep\n", encoding="utf-8")
    os.rename(operation.workspace_path, original)
    operation.workspace_path.symlink_to(outside, target_is_directory=True)

    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert outside.joinpath("keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert original.exists()


def test_cross_device_rename_failure_keeps_pending_fence_and_source(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    operation = harness.begin()

    def cross_device(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr("app.runtime.agent_deletion_fs._rename_noreplace", cross_device)
    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert operation.workspace_path.joinpath("workspace/CLAUDE.md").exists()


def test_racing_quarantine_destination_is_never_overwritten(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    operation = harness.begin()
    from app.runtime import agent_deletion_fs

    rename_noreplace = agent_deletion_fs._rename_noreplace

    def create_destination_then_rename(**kwargs):  # type: ignore[no-untyped-def]
        os.mkdir(kwargs["destination_name"], dir_fd=kwargs["destination_parent_fd"])
        rename_noreplace(**kwargs)

    monkeypatch.setattr(agent_deletion_fs, "_rename_noreplace", create_destination_then_rename)
    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert operation.workspace_path.joinpath("workspace/CLAUDE.md").exists()
    assert operation.quarantine_path.is_dir()


def test_fd_walk_unlinks_special_file_without_leaving_layout(harness: _Harness) -> None:
    os.mkfifo(harness.layout_root / "runtime.pipe")

    result = harness.delete()

    assert result.state == "completed"
    assert not harness.layout_root.exists()


def test_late_root_entry_after_initial_list_never_confirms_purge(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = harness.begin(key="late-root-entry")
    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    operation = harness.store.confirm_quarantine(operation.operation_id)
    from app.runtime import agent_deletion_fs

    real_listdir = agent_deletion_fs.os.listdir
    injected = False

    def inject_after_root_snapshot(directory_fd: int) -> list[str]:
        nonlocal injected
        entries = real_listdir(directory_fd)
        observed = os.fstat(directory_fd)
        if (
            not injected
            and operation.expected_identity is not None
            and observed.st_ino == operation.expected_identity.inode
            and observed.st_dev == operation.expected_identity.device
        ):
            injected = True
            late_fd = os.open(
                "late-root-entry.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            os.close(late_fd)
        return entries

    monkeypatch.setattr(agent_deletion_fs.os, "listdir", inject_after_root_snapshot)
    result = harness.service.reconcile_operation(operation)

    assert injected is True
    assert result.state == "cleanup_pending"
    assert result.purge_confirmed is False
    assert result.quarantine_path.joinpath("late-root-entry.txt").is_file()
    reread = harness.store.get(operation.operation_id)
    assert reread is not None and reread.state == "cleanup_pending"
    assert reread.purge_confirmed is False


def test_nested_directory_swap_is_detected_before_recursing_replacement(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = harness.layout_root / "race-directory"
    nested.mkdir()
    nested.joinpath("private.txt").write_text("private\n", encoding="utf-8")
    operation = harness.begin()
    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    operation = harness.store.confirm_quarantine(operation.operation_id)
    from app.runtime import agent_deletion_fs

    open_child = agent_deletion_fs._open_child
    evacuated = harness.data_dir / "evacuated-race-directory"
    swapped = False

    def swap_before_directory_open(parent_fd, name, *, directory, missing_ok):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if name == "race-directory" and directory and not swapped:
            swapped = True
            os.rename(operation.quarantine_path / name, evacuated)
            (operation.quarantine_path / name).mkdir()
            (operation.quarantine_path / name / "replacement.txt").write_text("replacement\n", encoding="utf-8")
        return open_child(parent_fd, name, directory=directory, missing_ok=missing_ok)

    monkeypatch.setattr(agent_deletion_fs, "_open_child", swap_before_directory_open)
    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert evacuated.joinpath("private.txt").read_text(encoding="utf-8") == "private\n"
    assert (operation.quarantine_path / "race-directory/replacement.txt").read_text(encoding="utf-8") == "replacement\n"


def test_file_swap_is_detected_before_unlinking_replacement(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    target = harness.layout_root / "race-file.txt"
    target.write_text("private\n", encoding="utf-8")
    operation = harness.begin()
    quarantined = quarantine_agent_layout(
        data_dir=harness.data_dir,
        workspace_path=operation.workspace_path,
        quarantine_path=operation.quarantine_path,
        expected=operation.expected_identity,
    )
    assert quarantined.state == "quarantined"
    operation = harness.store.confirm_quarantine(operation.operation_id)
    from app.runtime import agent_deletion_fs

    open_child = agent_deletion_fs._open_child
    evacuated = harness.data_dir / "evacuated-race-file.txt"
    opens = 0

    def swap_before_unlink_check(parent_fd, name, *, directory, missing_ok):  # type: ignore[no-untyped-def]
        nonlocal opens
        if name == "race-file.txt" and not directory:
            opens += 1
            if opens == 2:
                os.rename(operation.quarantine_path / name, evacuated)
                (operation.quarantine_path / name).write_text("replacement\n", encoding="utf-8")
        return open_child(parent_fd, name, directory=directory, missing_ok=missing_ok)

    monkeypatch.setattr(agent_deletion_fs, "_open_child", swap_before_unlink_check)
    result = harness.service.reconcile_operation(operation)

    assert result.state == "cleanup_pending"
    assert evacuated.read_text(encoding="utf-8") == "private\n"
    assert (operation.quarantine_path / "race-file.txt").read_text(encoding="utf-8") == "replacement\n"


def test_same_key_dual_contender_converges_to_one_completed_operation(harness: _Harness) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: harness.delete(key="shared-key"), range(2)))

    assert {result.operation_id for result in results} == {results[0].operation_id}
    assert {result.state for result in results} == {"completed"}


def test_pending_reconcile_fairly_reaches_rows_beyond_limit(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with harness.session_factory.begin() as db:
        for index in range(3):
            db.add(_operation_row(index=index, state="cleanup_pending"))
    seen: list[str] = []

    def keep_pending(operation):  # type: ignore[no-untyped-def]
        seen.append(operation.operation_id)
        return harness.store.record_cleanup_failure(operation.operation_id, error_code="EXPECTED_PENDING")

    monkeypatch.setattr(harness.service, "reconcile_operation", keep_pending)
    harness.service.reconcile(limit=2)
    assert seen == ["fair-cleanup_pending-0", "fair-cleanup_pending-1"]

    seen.clear()
    harness.service.reconcile(limit=2)
    assert "fair-cleanup_pending-2" in seen


def test_witness_reconcile_fairly_reaches_rows_beyond_limit(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with harness.session_factory.begin() as db:
        for index in range(3):
            db.add(_operation_row(index=index, state="completed"))
    attempted: list[str] = []

    def keep_witness(*, quarantine_path, expected):  # type: ignore[no-untyped-def]
        del expected
        attempted.append(quarantine_path.name)
        return False

    monkeypatch.setattr("app.services.business_agent_deletion.remove_quarantine_witness", keep_witness)
    harness.service.reconcile(limit=2)
    assert attempted == ["fair-completed-0", "fair-completed-1"]

    attempted.clear()
    harness.service.reconcile(limit=2)
    assert "fair-completed-2" in attempted


def test_reconcile_isolates_unexpected_first_failure_and_continues_batch(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = harness.begin(key="fault-isolation")
    second = replace(first, operation_id="synthetic-completed", state="completed")
    seen: list[str] = []

    def reconcile(operation):  # type: ignore[no-untyped-def]
        seen.append(operation.operation_id)
        if operation.operation_id == first.operation_id:
            raise RuntimeError("unexpected driver failure with private detail")
        return operation

    monkeypatch.setattr(harness.store, "list_pending", lambda *, limit: [first, second])
    monkeypatch.setattr(harness.store, "list_witness_cleanup", lambda *, limit: [])
    monkeypatch.setattr(harness.service, "reconcile_operation", reconcile)

    summary = harness.service.reconcile(limit=2)

    assert seen == [first.operation_id, second.operation_id]
    assert summary == {"completed": 1, "cleanup_pending": 1}
    reread = harness.store.get(first.operation_id)
    assert reread is not None
    assert reread.error == {"error_code": "AGENT_DELETION_RECONCILE_FAILED"}


def test_deletion_holds_injected_repository_guard_through_fence_cleanup_and_eviction(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @contextmanager
    def guard(agent_id: str):  # type: ignore[no-untyped-def]
        assert agent_id == harness.agent_id
        events.append("guard-enter")
        try:
            yield
        finally:
            events.append("guard-exit")

    begin = harness.store.begin

    def observed_begin(**kwargs):  # type: ignore[no-untyped-def]
        assert events == ["guard-enter"]
        events.append("begin")
        return begin(**kwargs)

    from app.services import business_agent_deletion

    quarantine = business_agent_deletion.quarantine_agent_layout

    def observed_quarantine(**kwargs):  # type: ignore[no-untyped-def]
        assert events[-1] == "evict"
        events.append("quarantine")
        return quarantine(**kwargs)

    monkeypatch.setattr(harness.store, "begin", observed_begin)
    monkeypatch.setattr(business_agent_deletion, "quarantine_agent_layout", observed_quarantine)
    service = BusinessAgentDeletionService(
        harness.store,
        data_dir=harness.data_dir,
        mutation_guard_for=guard,
        evict_agent=lambda agent_id: events.append("evict"),
    )

    result = service.delete(
        agent_id=harness.agent_id,
        agent_instance_etag=business_agent_instance_etag(harness.instance_token),
        idempotency_key="stable-lock",
    )

    assert result.state == "completed"
    assert events == ["guard-enter", "begin", "evict", "quarantine", "guard-exit"]


@pytest.mark.parametrize(
    "add_blocker",
    [
        lambda db, h: db.add(SessionRecordModel(session_id="session", agent_id=h.agent_id, active_run_id="run")),
        lambda db, h: _add_running_turn_intent(db, h.agent_id),
        lambda db, h: db.add(_waiting_hitl(h.agent_id)),
        lambda db, h: db.add(_active_test(h.agent_id)),
        lambda db, h: db.add(_active_change_set(h)),
        lambda db, h: db.add(_active_release(h.agent_id)),
        lambda db, h: db.add(_active_release(h.agent_id, status="failed")),
        lambda db, h: db.add(_pending_cleanup(h.agent_id)),
        lambda db, h: db.add(_active_activation(h)),
        lambda db, h: db.add(AgentAdmissionStateModel(agent_id=h.agent_id, maintenance_token="claim", maintenance_generation=1)),
    ],
    ids=[
        "active-turn",
        "running-turn-intent",
        "waiting-hitl",
        "active-test",
        "active-change-set",
        "release",
        "failed-release",
        "cleanup",
        "activation",
        "maintenance",
    ],
)
def test_all_runtime_blockers_abort_same_transaction_before_tombstone(
    harness: _Harness,
    add_blocker: Callable[[Session, _Harness], None],
) -> None:
    with harness.session_factory.begin() as db:
        add_blocker(db, harness)

    with pytest.raises(AgentDeletionStoreError, match="Cannot delete Agent"):
        harness.begin()

    with harness.session_factory() as db:
        registry = db.get(AgentRegistryModel, harness.agent_id)
        pending = db.scalar(select(AgentDeletionOperationModel).where(AgentDeletionOperationModel.agent_id == harness.agent_id))
        assert registry is not None and registry.deleted_at is None
        assert pending is None
