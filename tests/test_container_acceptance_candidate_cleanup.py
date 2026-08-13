from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import scripts.agent_test_acceptance_support as acceptance_support
import scripts.container_acceptance_candidate_authority as candidate_authority
import scripts.container_acceptance_candidate_cleanup as candidate_cleanup
import scripts.container_acceptance_candidate_cleanup_fs as cleanup_fs
import scripts.container_acceptance_candidate_storage as candidate_storage

import test_container_acceptance_candidate_authority as candidate_fixtures


class _InterruptedCleanup(BaseException):
    pass


def _reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_id: str,
) -> acceptance_support.CandidateSnapshotReservation:
    repository, env_file = candidate_fixtures._candidate_repository(tmp_path)
    candidate_fixtures._candidate_requirements(monkeypatch, tmp_path)
    return acceptance_support.reserve_candidate_snapshot(
        repository,
        env_file,
        run_id=run_id,
        profile="agent-test",
    )


@pytest.mark.parametrize(
    "phase",
    [
        "marker-file-synced",
        "marker-linked-before-fsync",
        "marker-linked",
        "marker-temp-unlinked-before-fsync",
        "marker-published",
    ],
)
def test_reserved_marker_publish_interruption_recovers_exact_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    reservation = _reservation(monkeypatch, tmp_path, run_id="1700000040-a1b2c3d4e5f6")
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    interrupted = False

    def stop(target: str) -> None:
        nonlocal interrupted
        if target == phase and not interrupted:
            interrupted = True
            raise _InterruptedCleanup

    monkeypatch.setattr(candidate_cleanup, "_checkpoint", stop)
    content = candidate_authority.reservation_marker_bytes(reservation, "d" * 64)
    try:
        with pytest.raises(_InterruptedCleanup):
            candidate_cleanup.write_reservation_marker(root, reservation, content)
    finally:
        os.close(root.descriptor)
    monkeypatch.setattr(candidate_cleanup, "_checkpoint", lambda _phase: None)
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert not any(reservation.parent.iterdir())


def test_reserved_cleanup_recovers_partial_private_marker_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reservation = _reservation(monkeypatch, tmp_path, run_id="1700000041-a1b2c3d4e5f6")
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    descriptor = os.open(
        candidate_cleanup.reservation_marker_temp_name(reservation),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=root.descriptor,
    )
    try:
        os.write(descriptor, b"partial")
    finally:
        os.close(descriptor)
        os.close(root.descriptor)
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert not any(reservation.parent.iterdir())


@pytest.mark.parametrize("linked", [False, True])
def test_reserved_cleanup_resumes_after_marker_leaf_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    linked: bool,
) -> None:
    reservation = _reservation(monkeypatch, tmp_path, run_id="1700000046-a1b2c3d4e5f6")
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    temporary = candidate_cleanup.reservation_marker_temp_name(reservation)
    if linked:
        monkeypatch.setattr(
            candidate_cleanup,
            "_checkpoint",
            lambda phase: (_ for _ in ()).throw(_InterruptedCleanup) if phase == "marker-linked" else None,
        )
        with pytest.raises(_InterruptedCleanup):
            candidate_cleanup.write_reservation_marker(
                root,
                reservation,
                candidate_authority.reservation_marker_bytes(reservation, "d" * 64),
            )
    else:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root.descriptor,
        )
        os.close(descriptor)
    os.close(root.descriptor)
    target = "reserved-marker-linked-temp-unlinked-before-fsync" if linked else "reserved-marker-temp-unlinked-before-fsync"
    monkeypatch.setattr(
        candidate_cleanup,
        "_checkpoint",
        lambda phase: (_ for _ in ()).throw(_InterruptedCleanup) if phase == target else None,
    )
    with pytest.raises(_InterruptedCleanup):
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    monkeypatch.setattr(candidate_cleanup, "_checkpoint", lambda _phase: None)
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert not any(reservation.parent.iterdir())


def test_marker_publish_never_overwrites_competing_final_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reservation = _reservation(monkeypatch, tmp_path, run_id="1700000042-a1b2c3d4e5f6")
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    competitor = b"competing-marker\n"
    candidate_storage.write_snapshot_env(
        root.descriptor,
        competitor,
        name=candidate_authority.SNAPSHOT_MARKER,
    )
    content = candidate_authority.reservation_marker_bytes(reservation, "d" * 64)
    with pytest.raises(candidate_storage.CandidateStorageError, match="already exists"):
        candidate_cleanup.write_reservation_marker(root, reservation, content)
    assert reservation.root.joinpath(candidate_authority.SNAPSHOT_MARKER).read_bytes() == competitor
    os.close(root.descriptor)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely"):
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    reservation.root.joinpath(candidate_authority.SNAPSHOT_MARKER).unlink()
    reservation.root.joinpath(candidate_cleanup.reservation_marker_temp_name(reservation)).unlink()
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)


def test_reserved_cleanup_rejects_marker_from_another_mount_before_deleting_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reservation = _reservation(monkeypatch, tmp_path, run_id="1700000047-a1b2c3d4e5f6")
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    candidate_storage.write_snapshot_env(
        root.descriptor,
        candidate_authority.reservation_marker_bytes(reservation, "d" * 64),
        name=candidate_authority.SNAPSHOT_MARKER,
    )
    candidate_storage.create_private_child(root, candidate_authority.SNAPSHOT_RUNTIME)
    os.close(root.descriptor)
    original_mount_id = cleanup_fs.mount_id

    def split_regular_file_mount(descriptor: int) -> int:
        observed = original_mount_id(descriptor)
        return observed + 1 if stat.S_ISREG(os.fstat(descriptor).st_mode) else observed

    monkeypatch.setattr(cleanup_fs, "mount_id", split_regular_file_mount)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely"):
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert reservation.root.joinpath(candidate_authority.SNAPSHOT_RUNTIME).is_dir()
    monkeypatch.setattr(cleanup_fs, "mount_id", original_mount_id)
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)


@pytest.mark.parametrize(
    "phase",
    [
        "root-renamed-before-fsync",
        "root-renamed",
        "root-chmod",
        "file-unlink",
        "directory-chmod",
        "directory-rmdir",
        "marker-unlink",
        "root-rmdir-before-fsync",
        "root-rmdir",
    ],
)
def test_prepared_cleanup_interruption_resumes_from_same_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    repository, env_file = candidate_fixtures._candidate_repository(tmp_path)
    candidate_fixtures._candidate_requirements(monkeypatch, tmp_path)
    prepared = candidate_fixtures._prepare_candidate(
        repository,
        env_file,
        run_id="1700000043-a1b2c3d4e5f6",
    )
    interrupted = False

    def stop(target: str) -> None:
        nonlocal interrupted
        if target == phase and not interrupted:
            interrupted = True
            raise _InterruptedCleanup

    monkeypatch.setattr(candidate_cleanup, "_checkpoint", stop)
    with pytest.raises(_InterruptedCleanup):
        acceptance_support.cleanup_candidate_snapshot(prepared)
    monkeypatch.setattr(candidate_cleanup, "_checkpoint", lambda _phase: None)
    acceptance_support.recover_and_cleanup_candidate_snapshot(prepared.recovery)
    acceptance_support.recover_and_cleanup_candidate_snapshot(prepared.recovery)
    assert interrupted and not any(prepared.snapshot.parent.iterdir())


def test_cleaning_move_rejects_competing_destination_without_deleting_either_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = candidate_fixtures._candidate_repository(tmp_path)
    candidate_fixtures._candidate_requirements(monkeypatch, tmp_path)
    prepared = candidate_fixtures._prepare_candidate(
        repository,
        env_file,
        run_id="1700000044-a1b2c3d4e5f6",
    )
    original_rename = cleanup_fs.rename_noreplace

    def insert_destination_then_rename(
        *,
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        os.mkdir(destination_name, 0o700, dir_fd=destination_parent_fd)
        original_rename(
            source_parent_fd=source_parent_fd,
            source_name=source_name,
            destination_parent_fd=destination_parent_fd,
            destination_name=destination_name,
        )

    monkeypatch.setattr(cleanup_fs, "rename_noreplace", insert_destination_then_rename)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely"):
        acceptance_support.cleanup_candidate_snapshot(prepared)
    tombstone = next(prepared.snapshot.parent.glob(f".{prepared.snapshot.root.name}.cleaning-*"))
    assert candidate_storage.same_node(
        candidate_storage.lstat_identity(prepared.snapshot.root),
        prepared.snapshot.root_identity,
    )
    assert tombstone.is_dir()
    monkeypatch.setattr(cleanup_fs, "rename_noreplace", original_rename)
    tombstone.rmdir()
    acceptance_support.cleanup_candidate_snapshot(prepared)
    assert not any(prepared.snapshot.parent.iterdir())


def test_cleanup_rejects_parent_mode_drift_and_reserved_foreign_container_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reservation = _reservation(monkeypatch, tmp_path, run_id="1700000045-a1b2c3d4e5f6")
    root = candidate_storage.create_planned_snapshot_root(
        reservation.parent,
        reservation.parent_identity,
        reservation.root,
    )
    os.close(root.descriptor)
    reservation.parent.chmod(0o755)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="authority|private state"):
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    reservation.parent.chmod(0o700)
    cleaning_name = candidate_cleanup._cleaning_name(reservation.root.name, root.authority.root_identity)
    reservation.root.rename(reservation.parent / cleaning_name)
    reservation.parent.joinpath(cleaning_name).chmod(0o755)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely"):
        acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)
    assert reservation.parent.joinpath(cleaning_name).exists()
    reservation.parent.joinpath(cleaning_name).chmod(0o700)
    acceptance_support.cleanup_reserved_candidate(reservation, "d" * 64)


def test_snapshot_verification_binds_dependencies_parent_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = candidate_fixtures._candidate_repository(tmp_path)
    candidate_fixtures._candidate_requirements(monkeypatch, tmp_path)
    prepared = candidate_fixtures._prepare_candidate(
        repository,
        env_file,
        run_id="1700000048-a1b2c3d4e5f6",
    )
    dependencies = prepared.snapshot.root / "dependencies"
    dependencies.chmod(0o700)
    with pytest.raises(acceptance_support.AcceptanceSupportError, match="dependency parent"):
        acceptance_support.verify_candidate_snapshot(prepared)
    dependencies.chmod(0o500)
    acceptance_support.cleanup_candidate_snapshot(prepared)


def test_cleanup_budget_includes_dependency_snapshots_beyond_source_only_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = candidate_fixtures._candidate_repository(tmp_path)
    candidate_fixtures._candidate_requirements(monkeypatch, tmp_path)
    prepared = candidate_fixtures._prepare_candidate(
        repository,
        env_file,
        run_id="1700000049-a1b2c3d4e5f6",
    )
    budgeted_nodes = sum(1 for path in prepared.snapshot.root.rglob("*") if path.parent != prepared.snapshot.root)
    dependency_entries = (
        prepared.snapshot.frontend_dependencies.entries,
        prepared.snapshot.python_dependencies.entries,
        prepared.snapshot.pnpm_dependencies.entries,
    )
    dependency_limit = max(dependency_entries)
    source_nodes = budgeted_nodes - sum(dependency_entries) - candidate_cleanup._FIXED_CANDIDATE_TOPOLOGY_NODES
    monkeypatch.setattr(candidate_cleanup, "MAX_SOURCE_FILES", source_nodes)
    monkeypatch.setattr(candidate_cleanup, "MAX_SOURCE_PATH_DEPTH", 0)
    monkeypatch.setattr(candidate_cleanup, "MAX_DEPENDENCY_ENTRIES", dependency_limit)

    assert len(set(dependency_entries)) == 1
    assert candidate_cleanup._cleanup_node_budget() == budgeted_nodes
    assert budgeted_nodes > source_nodes > 0
    acceptance_support.cleanup_candidate_snapshot(prepared)
    assert not any(prepared.snapshot.parent.iterdir())


def test_cleanup_budget_remains_fail_closed_above_combined_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, env_file = candidate_fixtures._candidate_repository(tmp_path)
    candidate_fixtures._candidate_requirements(monkeypatch, tmp_path)
    prepared = candidate_fixtures._prepare_candidate(
        repository,
        env_file,
        run_id="1700000050-a1b2c3d4e5f6",
    )
    monkeypatch.setattr(candidate_cleanup, "MAX_SOURCE_FILES", 0)
    monkeypatch.setattr(candidate_cleanup, "MAX_SOURCE_PATH_DEPTH", 0)
    monkeypatch.setattr(candidate_cleanup, "MAX_DEPENDENCY_ENTRIES", 0)

    with pytest.raises(acceptance_support.AcceptanceSupportError, match="cleaned safely") as captured:
        acceptance_support.cleanup_candidate_snapshot(prepared)
    causes: list[str] = []
    cause: BaseException | None = captured.value
    while cause is not None:
        causes.append(str(cause))
        cause = cause.__cause__
    assert any("node limit" in message for message in causes)
    assert any(prepared.snapshot.parent.iterdir())
