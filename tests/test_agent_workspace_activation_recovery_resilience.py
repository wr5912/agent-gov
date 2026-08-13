from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app.services.agent_workspace_activation as activation_module
import app.services.agent_workspace_git_operations as git_operations
import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.agent_admission import AgentMaintenanceClaim, is_maintenance_active
from app.runtime.agent_git_environment import governed_index_query
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.services.agent_workspace_activation import (
    WorkspaceActivationFailure,
    WorkspaceActivationPersistenceError,
)
from app.services.agent_workspace_activation_journal import new_preparing_operation, touch_reconciliation
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_workspace_activation_recovery_support import (
    activate_git_only as _activate_git_only,
)
from agent_workspace_activation_recovery_support import git as _git
from agent_workspace_activation_recovery_support import operation as _operation
from agent_workspace_activation_recovery_support import (
    prepare_import_activation as _prepare_import_activation,
)
from app_test_utils import load_test_app as _load_app


def _index_entry_offset(index: bytes, expected_path: bytes) -> int:
    if index[:4] != b"DIRC" or int.from_bytes(index[4:8], "big") not in {2, 3}:
        raise AssertionError("Test fixture requires a Git index version with uncompressed paths")
    offset = 12
    for _ in range(int.from_bytes(index[8:12], "big")):
        entry_start = offset
        flags = int.from_bytes(index[offset + 60 : offset + 62], "big")
        path_offset = offset + 62 + (2 if flags & 0x4000 else 0)
        declared_length = flags & 0x0FFF
        if declared_length == 0x0FFF:
            path_end = index.index(b"\0", path_offset)
        else:
            path_end = path_offset + declared_length
        if index[path_offset:path_end] == expected_path:
            return entry_start
        entry_size = path_end + 1 - entry_start
        offset = entry_start + ((entry_size + 7) & ~7)
    raise AssertionError(f"Git index entry not found: {expected_path!r}")


def _replace_index_stat_bytes(
    original: bytes,
    refreshed: bytes,
    *,
    path: str,
    object_format: str,
) -> bytes:
    checksum_size = 32 if object_format == "sha256" else 20
    original_body = bytearray(original[:-checksum_size])
    original_offset = _index_entry_offset(original, path.encode())
    refreshed_offset = _index_entry_offset(refreshed, path.encode())
    original_body[original_offset : original_offset + 40] = refreshed[refreshed_offset : refreshed_offset + 40]
    checksum = hashlib.new(object_format, original_body).digest()
    return bytes(original_body) + checksum


def test_assume_unchanged_and_skip_worktree_flags_restore_byte_exactly(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="index-flags",
            dirty=True,
            index_flags=True,
        )
        original_index = prepared.snapshot.original_index_snapshot
        original_workspace = prepared.snapshot.original_workspace_fingerprint
        assert _git(prepared.workspace, "ls-files", "-v", "--", "CLAUDE.md").startswith("h ")
        assert _git(prepared.workspace, "ls-files", "-v", "--", ".mcp.json").startswith("S ")
        _activate_git_only(prepared)
        resolution = module.workspace_activation_service.reject(
            prepared.operation_id,
            failure=WorkspaceActivationFailure("INJECTED_FAILURE", "restore extended index flags"),
        )
        prepared.lease.close(validate_claim=False)

    assert resolution == "rejected"
    assert git_operations.index_snapshot(prepared.workspace) == original_index
    assert git_operations.workspace_fingerprint(prepared.workspace) == original_workspace
    assert _git(prepared.workspace, "ls-files", "-v", "--", "CLAUDE.md").startswith("h ")
    assert _git(prepared.workspace, "ls-files", "-v", "--", ".mcp.json").startswith("S ")


def test_index_stat_refresh_does_not_false_conflict_activation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="index-stat-refresh")
        original_status = git_operations.workspace_status(prepared.workspace)
        original_fingerprint = git_operations.index_fingerprint(prepared.workspace)
        original_snapshot = git_operations.index_snapshot(prepared.workspace)
        assert original_fingerprint == prepared.snapshot.original_index_fingerprint
        fsmonitor_valid_paths = tuple(
            line[2:]
            for line in _git(
                prepared.workspace,
                *governed_index_query(["ls-files", "-f"]),
            ).splitlines()
            if line[:1].islower() and line[1:2] == " "
        )
        claude_path = prepared.workspace / "CLAUDE.md"
        claude_stat = claude_path.stat()
        os.utime(
            claude_path,
            ns=(claude_stat.st_atime_ns, claude_stat.st_mtime_ns - 10_000_000_000),
        )
        _git(prepared.workspace, "update-index", "--refresh")
        refreshed_snapshot = git_operations.index_snapshot(prepared.workspace)
        stat_only_snapshot = _replace_index_stat_bytes(
            original_snapshot,
            refreshed_snapshot,
            path="CLAUDE.md",
            object_format=_git(prepared.workspace, "rev-parse", "--show-object-format"),
        )
        git_operations.restore_index_snapshot(
            prepared.workspace,
            stat_only_snapshot,
            operation_id=prepared.operation_id,
        )
        restored_fsmonitor_valid_paths = tuple(
            line[2:]
            for line in _git(
                prepared.workspace,
                *governed_index_query(["ls-files", "-f"]),
            ).splitlines()
            if line[:1].islower() and line[1:2] == " "
        )

        assert git_operations.index_snapshot(prepared.workspace) != original_snapshot
        assert restored_fsmonitor_valid_paths == fsmonitor_valid_paths
        assert git_operations.workspace_status(prepared.workspace) == original_status
        assert git_operations.index_fingerprint(prepared.workspace) == original_fingerprint
        module.workspace_activation_service.activate(prepared.operation_id, before_activate=lambda: None)
        prepared.lease.close(validate_claim=False)

    assert _operation(module, prepared.operation_id).state == "completed"
    assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.candidate_commit


def test_flags_only_index_restore_phase_reenters_when_base_equals_original(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    crashed = False
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="flags-only-index-crash",
            index_flags=True,
        )
        assert prepared.base_commit == prepared.original_head
        _activate_git_only(prepared)
        original_set_phase = activation_module.set_recovery_phase

        def mark_index_then_crash(session_factory, operation_id: str, phase: str) -> None:
            nonlocal crashed
            original_set_phase(session_factory, operation_id, phase)
            if phase == "index_restore" and not crashed:
                crashed = True
                raise SystemExit("crash before flags-only raw index restore")

        with monkeypatch.context() as scoped:
            scoped.setattr(activation_module, "set_recovery_phase", mark_index_then_crash)
            with pytest.raises(SystemExit, match="flags-only"):
                service.reject(
                    prepared.operation_id,
                    failure=WorkspaceActivationFailure("INJECTED_FAILURE", "restore flags-only index"),
                )
        prepared.lease.close(validate_claim=False)
        summary = service.reconcile(force=True)

    assert crashed and summary["rejected"] == [prepared.operation_id]
    assert git_operations.index_snapshot(prepared.workspace) == prepared.snapshot.original_index_snapshot
    assert _git(prepared.workspace, "ls-files", "-v", "--", "CLAUDE.md").startswith("h ")
    assert _git(prepared.workspace, "ls-files", "-v", "--", ".mcp.json").startswith("S ")


@pytest.mark.parametrize("checkpoint", ["candidate_reset", "base_reset", "head_reset", "index_restore"])
def test_compensation_checkpoint_crash_converges_on_reentry(
    monkeypatch,
    tmp_path: Path,
    checkpoint: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    crashed = False
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"compensation-{checkpoint}",
            dirty=True,
            staged_partial=True,
        )
        original_status = prepared.snapshot.original_status
        original_index = prepared.snapshot.original_index_snapshot
        original_workspace = prepared.snapshot.original_workspace_fingerprint
        _activate_git_only(prepared)
        original_compensate = activation_module.compensate_candidate_activation
        original_run_git = git_operations.run_git
        original_restore_index = git_operations.restore_index_snapshot

        def compensate_then_crash(*args, **kwargs) -> None:
            nonlocal crashed
            original_compensate(*args, **kwargs)
            if checkpoint == "candidate_reset" and not crashed:
                crashed = True
                raise SystemExit("crash after candidate reset")

        def run_git_then_crash(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            nonlocal crashed
            result = original_run_git(repository, args, check=check)
            expected = ["reset", "--hard"] if checkpoint == "base_reset" else ["reset", "--mixed"]
            if checkpoint in {"base_reset", "head_reset"} and args[:2] == expected and not crashed:
                crashed = True
                raise SystemExit(f"crash after {checkpoint}")
            return result

        def restore_index_then_crash(
            repository: Path,
            content: bytes,
            *,
            operation_id: str | None = None,
        ) -> None:
            nonlocal crashed
            original_restore_index(repository, content, operation_id=operation_id)
            if checkpoint == "index_restore" and not crashed:
                crashed = True
                raise SystemExit("crash after index restore")

        with monkeypatch.context() as scoped:
            scoped.setattr(activation_module, "compensate_candidate_activation", compensate_then_crash)
            scoped.setattr(git_operations, "run_git", run_git_then_crash)
            scoped.setattr(git_operations, "restore_index_snapshot", restore_index_then_crash)
            with pytest.raises(SystemExit, match="crash after"):
                service.reject(
                    prepared.operation_id,
                    failure=WorkspaceActivationFailure("INJECTED_FAILURE", "checkpoint crash"),
                )
        prepared.lease.close(validate_claim=False)
        first = service.reconcile(force=True)
        second = service.reconcile(force=True)

    assert crashed and first["rejected"] == [prepared.operation_id]
    assert all(not values for values in second.values())
    assert _operation(module, prepared.operation_id).state == "rejected"
    assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.original_head
    assert git_operations.workspace_status(prepared.workspace) == original_status
    assert git_operations.index_snapshot(prepared.workspace) == original_index
    assert git_operations.workspace_fingerprint(prepared.workspace) == original_workspace
    assert not is_maintenance_active(service._Session, agent_id=prepared.agent_id)


def test_durable_refs_survive_aggressive_gc_until_rejection_finishes(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="activation-ref-gc",
            dirty=True,
            staged_partial=True,
        )
        _git(prepared.workspace, "reflog", "expire", "--expire=now", "--all")
        _git(prepared.workspace, "gc", "--prune=now")
        assert _git(prepared.workspace, "cat-file", "-t", prepared.candidate_commit) == "commit"
        assert _git(prepared.workspace, "cat-file", "-t", prepared.snapshot.original_index_tree_sha or "") == "tree"
        _activate_git_only(prepared)
        resolution = module.workspace_activation_service.reject(
            prepared.operation_id,
            failure=WorkspaceActivationFailure("INJECTED_FAILURE", "prove durable refs"),
        )
        prepared.lease.close(validate_claim=False)

    assert resolution == "rejected"
    assert (
        _git(
            prepared.workspace,
            "for-each-ref",
            "--format=%(refname)",
            f"refs/agentgov/workspace-activations/{prepared.operation_id}",
        )
        == ""
    )
    assert git_operations.index_snapshot(prepared.workspace) == prepared.snapshot.original_index_snapshot


def test_snapshot_head_cas_crash_recovers_without_losing_dirty_state(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    crashed = False
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="snapshot-cas-crash",
            dirty=True,
            staged_partial=True,
        )
        original_run_git = git_operations.run_git

        def cas_then_crash(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            nonlocal crashed
            result = original_run_git(repository, args, check=check)
            if args[:2] == ["update-ref", "HEAD"] and not crashed:
                crashed = True
                raise SystemExit("crash after snapshot HEAD CAS")
            return result

        with monkeypatch.context() as scoped:
            scoped.setattr(git_operations, "run_git", cas_then_crash)
            with pytest.raises(SystemExit, match="HEAD CAS"):
                service.activate(prepared.operation_id, before_activate=lambda: None)
        assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.base_commit
        prepared.lease.close(validate_claim=False)
        summary = service.reconcile(force=True)

    assert crashed and summary["rejected"] == [prepared.operation_id]
    assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.original_head
    assert git_operations.index_snapshot(prepared.workspace) == prepared.snapshot.original_index_snapshot
    assert git_operations.workspace_fingerprint(prepared.workspace) == prepared.snapshot.original_workspace_fingerprint


def test_ref_oid_mismatch_keeps_completing_operation_fenced(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="activation-ref-mismatch")
        _activate_git_only(prepared)
        service._stage_completion(prepared.operation_id)
        candidate_ref = f"refs/agentgov/workspace-activations/{prepared.operation_id}/candidate"
        _git(prepared.workspace, "update-ref", candidate_ref, prepared.original_head)
        summary = service.reconcile(force=True)
        prepared.lease.close(validate_claim=False)

    assert summary["recovery_required"] == [prepared.operation_id]
    assert _operation(module, prepared.operation_id).state == "recovery_required"
    assert is_maintenance_active(service._Session, agent_id=prepared.agent_id)


def test_concurrent_reconcilers_finalize_one_audit_idempotently(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="dual-reconciler")
        _activate_git_only(prepared)
        prepared.lease.close(validate_claim=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: service.reconcile(force=True), range(2)))

    assert _operation(module, prepared.operation_id).state == "completed"
    assert sum(prepared.operation_id in result["completed"] for result in results) >= 1
    with module.agent_testing_store.Session() as db:
        audits = list(db.scalars(select(AgentWorkspaceImportRecordModel).where(AgentWorkspaceImportRecordModel.import_id == prepared.import_id)).all())
    assert len(audits) == 1 and audits[0].status == "accepted"


def test_reconciler_isolates_missing_workspace_and_continues_later_operation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        missing = _prepare_import_activation(module, client, tmp_path, agent_id="missing-store")
        healthy = _prepare_import_activation(module, client, tmp_path, agent_id="healthy-store")
        missing.lease.close(validate_claim=False)
        healthy.lease.close(validate_claim=False)
        original_store_for = service._store_for

        def fail_one_store(agent_id: str) -> GitAgentVersionStore:
            if agent_id == missing.agent_id:
                raise RuntimeError("injected missing Workspace")
            return original_store_for(agent_id)

        monkeypatch.setattr(service, "_store_for", fail_one_store)
        summary = service.reconcile(force=True)

    assert summary["recovery_required"] == [missing.operation_id]
    assert summary["rejected"] == [healthy.operation_id]
    assert _operation(module, missing.operation_id).state == "recovery_required"
    assert _operation(module, healthy.operation_id).state == "rejected"


def test_recovery_state_persistence_failure_never_claims_recovery_required(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="recovery-write-failure")

        def fail_compensation(*_args, **_kwargs) -> None:
            raise git_operations.GitCommandError("injected compensation failure")

        def fail_transaction_begin():
            raise RuntimeError("injected recovery state commit failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(activation_module, "restore_dirty_state_after_failure", fail_compensation)
            scoped.setattr(service._Session, "begin", fail_transaction_begin)
            with pytest.raises(WorkspaceActivationPersistenceError, match="could not be persisted"):
                service.reject(
                    prepared.operation_id,
                    failure=WorkspaceActivationFailure("INJECTED_FAILURE", "must retain exact state"),
                )
        prepared.lease.close(validate_claim=False)

    assert _operation(module, prepared.operation_id).state == "prepared"
    assert is_maintenance_active(service._Session, agent_id=prepared.agent_id)


def test_reconciliation_candidate_order_rotates_touched_rows(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    operations: list[AgentWorkspaceActivationOperationModel] = []
    observation = git_operations.WorkspaceObservation(
        original_head="a" * 40,
        original_status="",
        original_index_fingerprint="b" * 64,
        original_index_snapshot=b"index",
        original_workspace_fingerprint="c" * 64,
    )
    with TestClient(module.app):
        for index in range(3):
            claim = AgentMaintenanceClaim(
                agent_id=f"rotation-{index}",
                token=uuid.uuid4().hex,
                generation=1,
                kind="workspace_import",
                owner_id="test:rotation",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            operation = new_preparing_operation(
                agent_id=claim.agent_id,
                action="import_overwrite",
                observation=observation,
                claim=claim,
                import_id=f"awi-{uuid.uuid4()}",
                package_sha256="d" * 64,
                tree_sha256="e" * 64,
            )
            service._persist_new_operation(operation)
            operations.append(operation)
        first = service._reconciliation_candidates(limit=2)
        for operation_id in first:
            touch_reconciliation(service._Session, operation_id)
        second = service._reconciliation_candidates(limit=2)

    assert operations[2].operation_id not in first
    assert operations[2].operation_id in second


def test_ref_cleanup_failure_keeps_completion_fenced_until_retry(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="ref-cleanup-failure")
        original_delete = git_operations.delete_workspace_operation_refs

        def fail_ref_cleanup(*_args, **_kwargs) -> None:
            raise git_operations.GitCommandError("injected durable ref cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(git_operations, "delete_workspace_operation_refs", fail_ref_cleanup)
            with pytest.raises(WorkspaceActivationPersistenceError, match="metadata commit failed"):
                service.activate(prepared.operation_id, before_activate=lambda: None)
        operation = _operation(module, prepared.operation_id)
        assert operation.state == "completing"
        assert is_maintenance_active(service._Session, agent_id=prepared.agent_id)
        assert _git(
            prepared.workspace,
            "for-each-ref",
            "--format=%(refname)",
            f"refs/agentgov/workspace-activations/{prepared.operation_id}",
        )
        monkeypatch.setattr(git_operations, "delete_workspace_operation_refs", original_delete)
        summary = service.reconcile(force=True)
        prepared.lease.close(validate_claim=False)

    assert summary["completed"] == [prepared.operation_id]
    assert _operation(module, prepared.operation_id).state == "completed"
    assert not is_maintenance_active(service._Session, agent_id=prepared.agent_id)
