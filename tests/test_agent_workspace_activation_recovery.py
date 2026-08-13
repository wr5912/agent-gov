from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import app.services.agent_workspace_activation as activation_module
import app.services.agent_workspace_git_operations as git_operations
import pytest
from app.agent_testing.models import AgentWorkspaceImportRecordModel
from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_admission import (
    AgentMaintenanceActiveError,
    claim_runtime_admission,
    is_maintenance_active,
)
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.agent_paths import business_agent_repository_lock_path
from app.runtime.config_file_schemas import AgentConfigFileUpdateRequest
from app.runtime.runtime_db import AgentAdmissionStateModel
from app.runtime.runtime_initialization import _runtime_agent_ids, _store_for
from app.runtime.session_store import LocalSession
from app.runtime.state_machines import validate_transition
from app.services.agent_config_files import AgentConfigFileError, AgentConfigFileService
from app.services.agent_governance import AgentGovernanceError
from app.services.agent_workspace_activation import (
    WorkspaceActivationFailure,
    WorkspaceActivationPersistenceError,
)
from app.services.agent_workspace_activation_contracts import WorkspaceActivationVerificationError
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_workspace_activation_recovery_support import (
    activate_git_only as _activate_git_only,
)
from agent_workspace_activation_recovery_support import (
    authority_projection as _authority_projection,
)
from agent_workspace_activation_recovery_support import (
    begin_import_activation_intent as _begin_import_activation_intent,
)
from agent_workspace_activation_recovery_support import git as _git
from agent_workspace_activation_recovery_support import import_new_agent as _import_new_agent
from agent_workspace_activation_recovery_support import operation as _operation
from agent_workspace_activation_recovery_support import (
    prepare_import_activation as _prepare_import_activation,
)
from agent_workspace_activation_recovery_support import validated_package as _validated_package
from app_test_utils import load_test_app as _load_app


def test_recovery_required_fences_repository_config_and_startup_writers(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="ordinary-writer-fence",
            dirty=True,
        )
        prepared.lease.close(validate_claim=False)
        config = client.get(
            "/api/agent-config-file",
            params={"agent_id": prepared.agent_id, "path": ".mcp.json"},
        ).json()
        with module.workspace_activation_service._Session.begin() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, prepared.operation_id)
            assert operation is not None
            validate_transition("workspace_activation", operation.state, "recovery_required")
            operation.state = "recovery_required"
        before = _authority_projection(module, prepared)

        snapshot = client.post(
            "/api/agent-repository/snapshot",
            params={"agent_id": prepared.agent_id},
            json={"operator": "test"},
        )
        discard = client.post(
            "/api/agent-repository/discard-changes",
            params={"agent_id": prepared.agent_id},
            json={"paths": ["operator-dirty.txt"]},
        )
        config_service = AgentConfigFileService(
            settings=module.settings,
            agent_registry_store=module.agent_registry_store,
            session_store=module.session_store,
        )

        assert snapshot.status_code == discard.status_code == 409
        assert {snapshot.json()["detail"], discard.json()["detail"]} == {"Business Agent repository is no longer mutable"}
        with pytest.raises(AgentConfigFileError, match="no longer mutable"):
            config_service.update_file(
                agent_id=prepared.agent_id,
                path=".mcp.json",
                request=AgentConfigFileUpdateRequest(
                    content='{"mcpServers":{"blocked":{"type":"http","url":"https://blocked.invalid/mcp"}}}\n',
                    expected_sha256=config["sha256"],
                ),
            )
        assert prepared.agent_id not in _runtime_agent_ids(module.settings)
        with pytest.raises(AgentGitError, match="no longer mutable"):
            _store_for(module.settings, prepared.agent_id).ensure_bootstrap()

    assert _authority_projection(module, prepared) == before


def test_cached_snapshot_waiter_rechecks_new_activation_fence_after_stable_lock(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        agent_id = "cached-snapshot-waiter"
        created = _import_new_agent(client, agent_id=agent_id, name=agent_id)
        assert created.status_code == 200
        workspace = Path(created.json()["agent"]["workspace_dir"])
        workspace.joinpath("waiting-dirty.txt").write_text("must remain dirty\n", encoding="utf-8")
        store = module.agent_governance._store_for(agent_id)
        head_before = _git(workspace, "rev-parse", "HEAD")
        status_before = git_operations.workspace_status(workspace)
        observation = git_operations.observe_live_workspace(store, expected_head=head_before)
        lease = module.agent_governance.version_maintenance.lease(
            agent_id=agent_id,
            kind="workspace_import",
            owner_id="test:cached-waiter",
        )
        lease.__enter__()
        started = Event()
        lock_path = business_agent_repository_lock_path(module.settings.data_dir, agent_id)

        def waiting_snapshot() -> object:
            started.set()
            return module.agent_governance.snapshot_repository(agent_id=agent_id)

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                with advisory_lock(lock_path, mode="exclusive"):
                    pending = executor.submit(waiting_snapshot)
                    assert started.wait(timeout=5)
                    preparation = module.workspace_activation_service.begin_import(
                        agent_id=agent_id,
                        observation=observation,
                        claim=lease.claim,
                        package_sha256="a" * 64,
                        tree_sha256="b" * 64,
                    )
                    assert not pending.done()
                with pytest.raises(AgentGovernanceError, match="no longer mutable"):
                    pending.result(timeout=10)
        finally:
            lease.close(validate_claim=False)

    assert _operation(module, preparation.operation_id).state == "preparing"
    assert _git(workspace, "rev-parse", "HEAD") == head_before
    assert git_operations.workspace_status(workspace) == status_before
    assert workspace.joinpath("waiting-dirty.txt").read_text(encoding="utf-8") == "must remain dirty\n"


def test_exact_recovery_rejects_pre_merge_intent_and_periodic_retry_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="intent-crash",
            dirty=True,
        )
        prepared.lease.close(validate_claim=False)
        with module.workspace_activation_service._Session.begin() as db:
            admission = db.get(AgentAdmissionStateModel, prepared.agent_id)
            assert admission is not None
            admission.maintenance_expires_at = "2000-01-01T00:00:00+00:00"

        first = module.workspace_activation_service.reconcile_operation(
            prepared.operation_id,
            now="2026-08-09T00:00:00+00:00",
        )
        second = module.workspace_activation_service.reconcile(now="2026-08-09T00:00:00+00:00")

    assert first == "rejected"
    assert all(not values for values in second.values())
    assert _operation(module, prepared.operation_id).state == "rejected"
    assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.original_head
    assert prepared.workspace.joinpath("operator-dirty.txt").read_text(encoding="utf-8") == "operator bytes\n"
    assert not is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id=prepared.agent_id,
    )
    with module.agent_testing_store.Session() as db:
        record = db.get(AgentWorkspaceImportRecordModel, prepared.import_id)
        assert record is not None and record.status == "failed"


def test_startup_recovery_completes_merged_candidate_and_invalidates_session(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="merge-crash",
        )
        session = LocalSession(
            session_id="merge-crash-session",
            sdk_session_id="merge-crash-sdk",
            agent_id=prepared.agent_id,
            turns=1,
        )
        module.session_store.save(session)
        _activate_git_only(prepared)
        prepared.lease.close(validate_claim=False)

        module._reconcile_runtime_orphans(startup=True)
        repeated = module.workspace_activation_service.reconcile(force=True)

    operation = _operation(module, prepared.operation_id)
    saved = module.session_store.get(session.session_id)
    assert all(not values for values in repeated.values())
    assert operation.state == "completed"
    assert operation.import_id == prepared.import_id
    assert operation.original_head_sha == prepared.original_head
    assert operation.base_commit_sha == prepared.base_commit
    assert operation.candidate_commit_sha == prepared.candidate_commit
    assert operation.suite_status in {"ready", "warning", "invalid"}
    assert operation.diagnostics_json == operation.suite_json["diagnostics"]
    assert saved is not None and saved.sdk_session_id is None


def test_recovery_completes_crash_after_callback_before_db_commit(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="commit-crash",
        )
        original_body = service._complete_transaction_body

        def crash_before_commit(db, operation_id: str) -> None:
            original_body(db, operation_id)
            raise SystemExit("simulated SIGKILL boundary before DB commit")

        with monkeypatch.context() as scoped:
            scoped.setattr(service, "_complete_transaction_body", crash_before_commit)
            with pytest.raises(SystemExit, match="before DB commit"):
                service.activate(prepared.operation_id, before_activate=lambda: None)
        prepared.lease.close(validate_claim=False)

        assert _operation(module, prepared.operation_id).state == "prepared"
        assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.candidate_commit
        summary = service.reconcile(force=True)

    assert summary["completed"] == [prepared.operation_id]
    assert _operation(module, prepared.operation_id).state == "completed"


def test_restore_uses_same_durable_candidate_recovery(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="restore-crash", name="restore crash")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        baseline = _git(workspace, "rev-parse", "HEAD")
        workspace.joinpath("CLAUDE.md").write_text("# historical restore target\n", encoding="utf-8")
        _git(workspace, "add", "--", "CLAUDE.md")
        _git(workspace, "commit", "-m", "Historical restore target")
        target = _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "reset", "--hard", baseline)
        session = LocalSession(
            session_id="restore-crash-session",
            sdk_session_id="restore-crash-sdk",
            agent_id="restore-crash",
            turns=1,
        )
        module.session_store.save(session)
        store = module.agent_governance._store_for("restore-crash")
        lease = module.agent_governance.version_maintenance.lease(
            agent_id="restore-crash",
            kind="workspace_restore",
            owner_id="test:restore-crash",
        )
        lease.__enter__()
        observation = git_operations.observe_live_workspace(store, expected_head=baseline)
        preparation = module.workspace_activation_service.begin_restore(
            agent_id="restore-crash",
            observation=observation,
            claim=lease.claim,
        )
        snapshot = git_operations.prepare_workspace_snapshot(
            store,
            observation=observation,
            operation_id=preparation.operation_id,
        )
        replacement = git_operations.restore_tree_as_commit(
            store,
            base_commit=snapshot.current_head,
            target_commit=target,
            message="Prepare durable restore candidate",
            operation_id=preparation.operation_id,
        )
        module.workspace_activation_service.prepare_restore(
            preparation.operation_id,
            snapshot=snapshot,
            replacement=replacement,
            target_commit_sha=target,
        )
        git_operations.activate_candidate(
            store,
            snapshot=snapshot,
            candidate_commit=replacement.current_commit_sha,
            operation_id=preparation.operation_id,
            before_activate=lambda: None,
        )
        lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    operation = _operation(module, preparation.operation_id)
    saved = module.session_store.get(session.session_id)
    assert summary["completed"] == [preparation.operation_id]
    assert operation.state == "completed" and operation.action == "restore"
    assert operation.import_id is None and operation.target_commit_sha == target
    assert _git(workspace, "rev-parse", "HEAD") == replacement.current_commit_sha
    assert workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == "# historical restore target\n"
    assert saved is not None and saved.sdk_session_id is None


def test_unknown_head_and_compensation_failure_keep_runtime_fenced(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        unknown = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="unknown-head",
        )
        unknown.workspace.joinpath("intervening.txt").write_text("new head\n", encoding="utf-8")
        _git(unknown.workspace, "add", "-A")
        _git(unknown.workspace, "commit", "-m", "Intervening external commit")
        unknown.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

        compensation = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="compensation-failure",
        )
        _activate_git_only(compensation)
        with monkeypatch.context() as scoped:
            scoped.setattr(
                activation_module,
                "compensate_candidate_activation",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(git_operations.GitCommandError("injected reset failure")),
            )
            resolution = module.workspace_activation_service.reject(
                compensation.operation_id,
                failure=WorkspaceActivationFailure("WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed"),
            )
        compensation.lease.close(validate_claim=False)

    assert summary["recovery_required"] == [unknown.operation_id]
    assert resolution == "recovery_required"
    for prepared in (unknown, compensation):
        assert _operation(module, prepared.operation_id).state == "recovery_required"
        record = module.agent_registry_store.get_agent(prepared.agent_id)
        assert record is not None
        with module.workspace_activation_service._Session.begin() as db:
            with pytest.raises(AgentMaintenanceActiveError, match="durable recovery"):
                claim_runtime_admission(
                    db,
                    agent_id=prepared.agent_id,
                    expected_instance_etag=record.instance_etag,
                )


def test_dirty_snapshot_reset_failure_enters_recovery_required(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="dirty-reset-failure",
            dirty=True,
        )
        assert prepared.base_commit != prepared.original_head
        _activate_git_only(prepared)
        original_run_git = git_operations.run_git

        def fail_reset(repository: Path, args: list[str], *, check: bool = True) -> bytes:
            if args[:2] == ["reset", "--mixed"]:
                raise git_operations.GitCommandError("injected strict reset failure")
            return original_run_git(repository, args, check=check)

        with monkeypatch.context() as scoped:
            scoped.setattr(git_operations, "run_git", fail_reset)
            resolution = module.workspace_activation_service.reject(
                prepared.operation_id,
                failure=WorkspaceActivationFailure("WORKSPACE_ACTIVATION_FAILED", "Workspace activation failed"),
            )
        prepared.lease.close(validate_claim=False)

    operation = _operation(module, prepared.operation_id)
    assert resolution == "recovery_required"
    assert operation.state == "recovery_required"
    assert operation.error_json["cause_type"] == "GitCommandError"


@pytest.mark.parametrize("crash_phase", ["after_snapshot", "after_candidate"])
def test_preparing_intent_recovers_crash_before_candidate_journal_finalize(
    monkeypatch,
    tmp_path: Path,
    crash_phase: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id=f"prepare-{crash_phase}", name=crash_phase)
        agent_id = created.json()["agent"]["agent_id"]
        workspace = Path(created.json()["agent"]["workspace_dir"])
        workspace.joinpath("dirty.txt").write_text("durable dirty bytes\n", encoding="utf-8")
        original_head = _git(workspace, "rev-parse", "HEAD")
        store = module.agent_governance._store_for(agent_id)
        lease = module.agent_governance.version_maintenance.lease(
            agent_id=agent_id,
            kind="workspace_import",
            owner_id=f"test:{crash_phase}",
        )
        lease.__enter__()
        observation = git_operations.observe_live_workspace(store, expected_head=original_head)
        package = _validated_package(tmp_path, agent_id=agent_id, content=b"# candidate\n")
        preparation = module.workspace_activation_service.begin_import(
            agent_id=agent_id,
            observation=observation,
            claim=lease.claim,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
        )
        snapshot = git_operations.prepare_workspace_snapshot(
            store,
            observation=observation,
            operation_id=preparation.operation_id,
        )
        if crash_phase == "after_candidate":
            git_operations.replace_tree_from_entries(
                store,
                base_commit=snapshot.current_head,
                entries=package.entries,
                message="Candidate left before journal finalize",
                operation_id=preparation.operation_id,
            )
        lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    operation = _operation(module, preparation.operation_id)
    assert summary["rejected"] == [preparation.operation_id]
    assert operation.state == "rejected"
    assert operation.import_id == preparation.import_id
    assert _git(workspace, "rev-parse", "HEAD") == original_head
    assert workspace.joinpath("dirty.txt").read_text(encoding="utf-8") == "durable dirty bytes\n"
    with module.agent_testing_store.Session() as db:
        audit = db.get(AgentWorkspaceImportRecordModel, preparation.import_id)
        assert audit is not None and audit.status == "failed"


def test_staged_and_unstaged_index_state_is_restored_exactly(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="staged-partial",
            staged_partial=True,
        )
        original_status = git_operations.workspace_status(prepared.workspace)
        original_index = git_operations.index_fingerprint(prepared.workspace)
        original_workspace = git_operations.workspace_fingerprint(prepared.workspace)
        _activate_git_only(prepared)
        resolution = module.workspace_activation_service.reject(
            prepared.operation_id,
            failure=WorkspaceActivationFailure("INJECTED_FAILURE", "restore the exact index"),
        )
        prepared.lease.close(validate_claim=False)

    assert resolution == "rejected"
    assert git_operations.workspace_status(prepared.workspace) == original_status
    assert git_operations.index_fingerprint(prepared.workspace) == original_index
    assert git_operations.workspace_fingerprint(prepared.workspace) == original_workspace
    assert prepared.workspace.joinpath("CLAUDE.md").read_text(encoding="utf-8") == "# unstaged bytes\n"


def test_same_status_different_bytes_never_release_prepared_fence(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="same-status-bytes",
            dirty=True,
        )
        original_status = git_operations.workspace_status(prepared.workspace)
        prepared.workspace.joinpath("operator-dirty.txt").write_text("changed bytes\n", encoding="utf-8")
        assert git_operations.workspace_status(prepared.workspace) == original_status
        with pytest.raises(git_operations.GitCommandError, match="bytes changed"):
            module.workspace_activation_service.activate(
                prepared.operation_id,
                before_activate=lambda: None,
            )
        resolution = module.workspace_activation_service.reject(
            prepared.operation_id,
            failure=WorkspaceActivationFailure("WORKSPACE_CHANGED", "concurrent bytes changed"),
        )
        prepared.lease.close(validate_claim=False)

    assert resolution == "recovery_required"
    assert _operation(module, prepared.operation_id).state == "recovery_required"
    assert prepared.workspace.joinpath("operator-dirty.txt").read_text(encoding="utf-8") == "changed bytes\n"
    assert is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id=prepared.agent_id,
    )


@pytest.mark.parametrize("state", ["preparing", "prepared"])
def test_temporary_artifact_cleanup_failure_keeps_activation_fenced(
    monkeypatch,
    tmp_path: Path,
    state: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        if state == "preparing":
            activation = _begin_import_activation_intent(
                module,
                client,
                agent_id="cleanup-preparing",
            )
        else:
            activation = _prepare_import_activation(
                module,
                client,
                tmp_path,
                agent_id="cleanup-prepared",
            )

        def fail_cleanup(*_args, **_kwargs) -> None:
            raise git_operations.GitCommandError("injected strict temp cleanup failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(activation_module, "cleanup_workspace_operation_temporary_files", fail_cleanup)
            if state == "prepared":
                with pytest.raises(WorkspaceActivationPersistenceError):
                    module.workspace_activation_service.activate(
                        activation.operation_id,
                        before_activate=lambda: None,
                    )
            summary = module.workspace_activation_service.reconcile(force=True)
        activation.lease.close(validate_claim=False)

    assert summary["recovery_required"] == [activation.operation_id]
    assert _operation(module, activation.operation_id).state == "recovery_required"
    assert is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id=activation.agent_id,
    )


def test_operation_index_temporary_file_is_removed_before_terminal_release(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="index-temp-cleanup")
        index_path = Path(_git(prepared.workspace, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = prepared.workspace / index_path
        artifact = index_path.parent / f"agentgov-index-{prepared.operation_id}-residue"
        artifact.write_bytes(b"interrupted index restoration")

        module.workspace_activation_service.activate(prepared.operation_id, before_activate=lambda: None)
        prepared.lease.close(validate_claim=False)

    assert not artifact.exists()
    assert _operation(module, prepared.operation_id).state == "completed"
    assert not is_maintenance_active(module.workspace_activation_service._Session, agent_id=prepared.agent_id)


def test_nonregular_index_temporary_artifact_keeps_activation_fenced(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="index-temp-symlink")
        index_path = Path(_git(prepared.workspace, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = prepared.workspace / index_path
        artifact = index_path.parent / f"agentgov-index-{prepared.operation_id}-residue"
        artifact.symlink_to(prepared.workspace / "CLAUDE.md")

        with pytest.raises(WorkspaceActivationPersistenceError, match="outcome staging failed"):
            service.activate(prepared.operation_id, before_activate=lambda: None)
        summary = service.reconcile(force=True)
        prepared.lease.close(validate_claim=False)

    assert summary["recovery_required"] == [prepared.operation_id]
    assert artifact.is_symlink()
    assert _operation(module, prepared.operation_id).state == "recovery_required"
    assert is_maintenance_active(service._Session, agent_id=prepared.agent_id)


def test_rejection_commit_ack_loss_returns_durable_rejected_outcome(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    acknowledged = False
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(module, client, tmp_path, agent_id="reject-ack-loss", dirty=True)
        original_finish = service._finish_rejection_under_guard

        def finish_then_lose_ack(operation_id: str, store: GitAgentVersionStore) -> str:
            nonlocal acknowledged
            result = original_finish(operation_id, store)
            if not acknowledged:
                acknowledged = True
                raise RuntimeError("injected rejection commit acknowledgement loss")
            return result

        monkeypatch.setattr(service, "_finish_rejection_under_guard", finish_then_lose_ack)
        resolution = service.reject(
            prepared.operation_id,
            failure=WorkspaceActivationFailure("INJECTED_FAILURE", "reject after committed acknowledgement loss"),
        )
        prepared.lease.close(validate_claim=False)

    assert acknowledged and resolution == "rejected"
    assert _operation(module, prepared.operation_id).state == "rejected"
    assert _git(prepared.workspace, "rev-parse", "HEAD") == prepared.original_head
    assert prepared.workspace.joinpath("operator-dirty.txt").read_text(encoding="utf-8") == "operator bytes\n"
    assert not is_maintenance_active(service._Session, agent_id=prepared.agent_id)
    with module.agent_testing_store.Session() as db:
        audit = db.get(AgentWorkspaceImportRecordModel, prepared.import_id)
        assert audit is not None and audit.status == "failed"


@pytest.mark.parametrize("terminal_state", ["completed", "rejected", "recovery_required"])
def test_activate_checks_terminal_state_before_any_git_side_effect(
    monkeypatch,
    tmp_path: Path,
    terminal_state: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"reactivate-{terminal_state}",
            dirty=True,
            staged_partial=True,
        )
        if terminal_state == "completed":
            service.activate(prepared.operation_id, before_activate=lambda: None)
        elif terminal_state == "rejected":
            assert (
                service.reject(
                    prepared.operation_id,
                    failure=WorkspaceActivationFailure("INJECTED_FAILURE", "terminal rejection"),
                )
                == "rejected"
            )
        else:
            with service._Session.begin() as db:
                operation = db.get(AgentWorkspaceActivationOperationModel, prepared.operation_id)
                assert operation is not None
                operation.state = "recovery_required"
        before = _authority_projection(module, prepared)
        shutil.rmtree(prepared.store.worktrees_dir)
        shutil.rmtree(prepared.store.releases_dir)
        lock_path = business_agent_repository_lock_path(module.settings.data_dir, prepared.agent_id)
        lock_path.unlink(missing_ok=True)
        if terminal_state == "completed":
            service.activate(prepared.operation_id, before_activate=lambda: pytest.fail("completed callback ran"))
        else:
            with pytest.raises(WorkspaceActivationVerificationError, match=f"state {terminal_state}"):
                service.activate(prepared.operation_id, before_activate=lambda: pytest.fail("terminal callback ran"))
        after = _authority_projection(module, prepared)
        prepared.lease.close(validate_claim=False)

    assert after == before
    assert not prepared.store.worktrees_dir.exists()
    assert not prepared.store.releases_dir.exists()
    assert not lock_path.exists()


def test_begin_activation_rejects_cross_agent_and_wrong_kind_claims(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = _import_new_agent(client, agent_id="claim-boundary", name="claim boundary")
        workspace = Path(created.json()["agent"]["workspace_dir"])
        store = module.agent_governance._store_for("claim-boundary")
        lease = module.agent_governance.version_maintenance.lease(
            agent_id="claim-boundary",
            kind="workspace_import",
            owner_id="test:claim-boundary",
        )
        lease.__enter__()
        observation = git_operations.observe_live_workspace(store)
        with module.workspace_activation_service._Session() as db:
            before = db.scalar(select(func.count()).select_from(AgentWorkspaceActivationOperationModel))
        with pytest.raises(WorkspaceActivationVerificationError, match="belong to claim-boundary"):
            module.workspace_activation_service.begin_import(
                agent_id="claim-boundary",
                observation=observation,
                claim=replace(lease.claim, agent_id="different-agent"),
                package_sha256="a" * 64,
                tree_sha256="b" * 64,
            )
        with pytest.raises(WorkspaceActivationVerificationError, match="workspace_restore"):
            module.workspace_activation_service.begin_restore(
                agent_id="claim-boundary",
                observation=observation,
                claim=lease.claim,
            )
        with module.workspace_activation_service._Session() as db:
            after = db.scalar(select(func.count()).select_from(AgentWorkspaceActivationOperationModel))
        lease.close(validate_claim=False)

    assert workspace.is_dir() and before == after
