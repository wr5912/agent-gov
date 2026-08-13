from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import app.services.agent_workspace_activation as activation_module
import app.services.agent_workspace_git_operations as git_operations
import pytest
from app.agent_testing.models import AgentTestRunModel, AgentWorkspaceImportRecordModel
from app.runtime.agent_admission import is_maintenance_active
from app.runtime.agent_git_environment import governed_index_query
from app.runtime.agent_maintenance_db import AgentWorkspaceActivationOperationModel
from app.runtime.claude_user_input_db import ClaudeUserInputRequestModel
from app.runtime.recovery_cli_support import OperatorRecoveryError
from app.runtime.recovery_read_only_git import HardenedReadOnlyGitRepository
from app.runtime.runtime_db import (
    AgentAdmissionStateModel,
    SessionRecordModel,
    SessionTurnIntentModel,
)
from app.runtime.session_store import LocalSession
from app.runtime.workspace_activation_recovery import WorkspaceActivationRecoveryAttemptStore
from app.services.agent_workspace_activation import WorkspaceActivationFailure
from app.services.agent_workspace_activation_contracts import WorkspaceActivationVerificationError
from app.services.agent_workspace_activation_outcomes import verify_exact_terminal_outcome
from app.services.agent_workspace_activation_verification import verify_operation_graph
from app.services.agent_workspace_index_state import index_snapshot
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from tests.workspace_activation_recovery_test_support import (
    _DIGEST,
    build_recovery_harness,
    git,
    recovery_request,
)
from tests.workspace_activation_terminal_test_support import stage_completed_terminal_for_resume

from agent_workspace_activation_recovery_support import (
    activate_git_only as _activate_git_only,
)
from agent_workspace_activation_recovery_support import git as _git
from agent_workspace_activation_recovery_support import operation as _operation
from agent_workspace_activation_recovery_support import (
    prepare_import_activation as _prepare_import_activation,
)
from app_test_utils import load_test_app as _load_app
from workspace_package_test_utils import import_new_agent as _import_new_agent

_REJECTION = WorkspaceActivationFailure(
    "INJECTED_REJECTION",
    "Reject the staged activation deterministically.",
)


@dataclass(frozen=True)
class _PreparedRestore:
    operation_id: str
    agent_id: str
    workspace: Path
    candidate_commit: str
    snapshot: git_operations.SnapshotState
    lease: object
    store: object


def _stage_outcome(module, prepared, *, target: str) -> None:
    service = module.workspace_activation_service
    if target == "completed":
        _activate_git_only(prepared)
        service._stage_completion(prepared.operation_id)
        assert _operation(module, prepared.operation_id).state == "completing"
        return
    service._stage_rejection(prepared.operation_id, failure=_REJECTION)
    assert _operation(module, prepared.operation_id).state == "rejecting"


def _activation_refs(prepared) -> str:
    return _git(
        prepared.workspace,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        f"refs/agentgov/workspace-activations/{prepared.operation_id}",
    )


def _assert_recovery_fence(module, prepared, *, refs: str) -> None:
    operation = _operation(module, prepared.operation_id)
    assert operation.state == "recovery_required"
    assert _activation_refs(prepared) == refs
    assert is_maintenance_active(
        module.workspace_activation_service._Session,
        agent_id=prepared.agent_id,
    )


@pytest.mark.parametrize("target", ["completed", "rejected"])
@pytest.mark.parametrize("mutation", ["missing", "conflict"])
def test_staged_import_requires_existing_exact_terminal_audit(
    monkeypatch,
    tmp_path: Path,
    target: str,
    mutation: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-audit-{mutation}",
        )
        _stage_outcome(module, prepared, target=target)
        with module.workspace_activation_service._Session.begin() as db:
            audit = db.get(AgentWorkspaceImportRecordModel, prepared.import_id)
            assert audit is not None
            if mutation == "missing":
                db.delete(audit)
            else:
                audit.package_sha256 = "f" * 64
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)
    with module.workspace_activation_service._Session() as db:
        audit = db.get(AgentWorkspaceImportRecordModel, prepared.import_id)
        if mutation == "missing":
            assert audit is None
        else:
            assert audit is not None and audit.package_sha256 == "f" * 64


@pytest.mark.parametrize("target", ["completed", "rejected"])
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("maintenance_token", "wrong-token"),
        ("maintenance_generation", 999_999),
        ("maintenance_kind", "wrong-kind"),
    ],
)
def test_staged_outcome_requires_exact_admission_tuple(
    monkeypatch,
    tmp_path: Path,
    target: str,
    field: str,
    replacement: object,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-admission-{field}",
        )
        _stage_outcome(module, prepared, target=target)
        with module.workspace_activation_service._Session.begin() as db:
            admission = db.get(AgentAdmissionStateModel, prepared.agent_id)
            assert admission is not None
            setattr(admission, field, replacement)
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)


@pytest.mark.parametrize("target", ["completed", "rejected"])
def test_staged_outcome_rejects_final_workspace_drift(
    monkeypatch,
    tmp_path: Path,
    target: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-workspace-drift",
        )
        _stage_outcome(module, prepared, target=target)
        prepared.workspace.joinpath("terminal-drift.txt").write_text(
            "external bytes\n",
            encoding="utf-8",
        )
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)


def test_rejected_outcome_requires_original_raw_index_bytes(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="rejected-index-bytes",
        )
        _stage_outcome(module, prepared, target="rejected")
        original = _operation(module, prepared.operation_id)
        flag_rows = _git(
            prepared.workspace,
            *governed_index_query(("ls-files", "-f")),
        ).splitlines()
        fsmonitor_paths = [row[2:] for row in flag_rows if row[:1].islower()]
        os.utime(prepared.workspace / "CLAUDE.md", (2_000_000_000, 2_000_000_000))
        _git(prepared.workspace, *governed_index_query(("update-index", "--refresh")))
        if fsmonitor_paths:
            _git(prepared.workspace, *governed_index_query(("update-index", "--fsmonitor")))
            _git(
                prepared.workspace,
                *governed_index_query(("update-index", "--fsmonitor-valid", "--", *fsmonitor_paths)),
            )
        assert git_operations.index_fingerprint(prepared.workspace) == original.original_index_fingerprint
        assert git_operations.workspace_status(prepared.workspace) == original.original_status_text
        assert git_operations.workspace_fingerprint(prepared.workspace) == original.original_workspace_fingerprint
        assert index_snapshot(prepared.workspace) != original.original_index_snapshot
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)


def test_completion_resume_reinvalidates_new_inactive_sdk_mapping(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="completion-inactive-session-retry",
        )
        _stage_outcome(module, prepared, target="completed")
        session = LocalSession(
            session_id="completion-inactive-session",
            sdk_session_id="stale-sdk-mapping",
            agent_id=prepared.agent_id,
        )
        module.session_store.save(session)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    saved = module.session_store.get(session.session_id)
    assert summary["completed"] == [prepared.operation_id]
    assert saved is not None and saved.sdk_session_id is None
    assert _operation(module, prepared.operation_id).state == "completed"


@pytest.mark.parametrize("blocker", ["active_session", "running_turn", "waiting_hitl"])
def test_completion_outcome_rejects_active_runtime_work(
    monkeypatch,
    tmp_path: Path,
    blocker: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"completion-{blocker}",
        )
        _stage_outcome(module, prepared, target="completed")
        _add_runtime_blocker(module, prepared.agent_id, blocker=blocker)
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)


def test_rejected_outcome_rejects_active_runtime_work(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="rejected-waiting-hitl",
        )
        _stage_outcome(module, prepared, target="rejected")
        _add_runtime_blocker(module, prepared.agent_id, blocker="waiting_hitl")
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)


@pytest.mark.parametrize("target", ["completed", "rejected"])
@pytest.mark.parametrize("test_status", ["queued", "running"])
def test_terminal_outcome_rejects_active_agent_test_without_changing_refs_or_fence(
    monkeypatch,
    tmp_path: Path,
    target: str,
    test_status: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-{test_status}-agent-test",
        )
        _stage_outcome(module, prepared, target=target)
        with module.workspace_activation_service._Session.begin() as db:
            db.add(
                AgentTestRunModel(
                    test_run_id=f"run-{uuid.uuid4()}",
                    agent_id=prepared.agent_id,
                    commit_sha=prepared.candidate_commit,
                    source="manual",
                    status=test_status,
                )
            )
        refs = _activation_refs(prepared)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)

    assert summary["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=refs)


@pytest.mark.parametrize("target", ["completed", "rejected"])
def test_staged_outcome_recovers_crash_after_exact_ref_cleanup(
    monkeypatch,
    tmp_path: Path,
    target: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-post-ref-cleanup-crash",
        )
        _stage_outcome(module, prepared, target=target)
        finalize_name = "_finalize_completion" if target == "completed" else "_finalize_rejection"
        with monkeypatch.context() as scoped:
            scoped.setattr(
                service,
                finalize_name,
                lambda _operation_id: (_ for _ in ()).throw(SystemExit("crash after exact ref cleanup")),
            )
            with pytest.raises(SystemExit, match="exact ref cleanup"):
                service.reconcile_operation(prepared.operation_id, force=True)
        assert _activation_refs(prepared) == ""
        expected_staged_state = "completing" if target == "completed" else "rejecting"
        assert _operation(module, prepared.operation_id).state == expected_staged_state
        prepared.lease.close(validate_claim=False)
        summary = service.reconcile(force=True)

    assert summary[target] == [prepared.operation_id]
    assert _operation(module, prepared.operation_id).state == target
    assert not is_maintenance_active(service._Session, agent_id=prepared.agent_id)


@pytest.mark.parametrize("target", ["completed", "rejected"])
def test_terminal_transaction_rechecks_active_work_after_ref_cleanup(
    monkeypatch,
    tmp_path: Path,
    target: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    injected = False
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-terminal-active-work",
        )
        _stage_outcome(module, prepared, target=target)
        original_cleanup = activation_module.cleanup_workspace_operation_temporary_files

        def cleanup_then_add_active_work(*args, **kwargs) -> None:
            nonlocal injected
            original_cleanup(*args, **kwargs)
            if not injected and kwargs.get("include_refs", True):
                injected = True
                _add_runtime_blocker(module, prepared.agent_id, blocker="waiting_hitl")

        monkeypatch.setattr(
            activation_module,
            "cleanup_workspace_operation_temporary_files",
            cleanup_then_add_active_work,
        )
        prepared.lease.close(validate_claim=False)
        summary = service.reconcile(force=True)

    assert injected and summary["recovery_required"] == [prepared.operation_id]
    assert _activation_refs(prepared) == ""
    assert _operation(module, prepared.operation_id).state == "recovery_required"
    assert is_maintenance_active(service._Session, agent_id=prepared.agent_id)


@pytest.mark.parametrize("target", ["completed", "rejected"])
def test_partial_staged_refs_never_reach_terminal_state(
    monkeypatch,
    tmp_path: Path,
    target: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-partial-refs",
        )
        _stage_outcome(module, prepared, target=target)
        _git(
            prepared.workspace,
            "update-ref",
            "-d",
            f"refs/agentgov/workspace-activations/{prepared.operation_id}/candidate",
        )
        partial_refs = _activation_refs(prepared)
        assert partial_refs
        prepared.lease.close(validate_claim=False)
        first = service.reconcile(force=True)
        second = service.reconcile(force=True)

    assert first["recovery_required"] == [prepared.operation_id]
    assert second["recovery_required"] == [prepared.operation_id]
    _assert_recovery_fence(module, prepared, refs=partial_refs)


def test_preoutcome_rejection_requires_all_durable_refs(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id="rejection-preoutcome-missing-refs",
        )
        for line in _activation_refs(prepared).splitlines():
            ref_name, _object_sha = line.split(" ", 1)
            _git(prepared.workspace, "update-ref", "-d", ref_name)
        assert _activation_refs(prepared) == ""
        resolution = service.reject(prepared.operation_id, failure=_REJECTION)
        prepared.lease.close(validate_claim=False)

    operation = _operation(module, prepared.operation_id)
    assert resolution == "recovery_required"
    assert operation.state == "recovery_required" and operation.recovery_phase == "none"
    assert is_maintenance_active(service._Session, agent_id=prepared.agent_id)
    with service._Session() as db:
        assert db.get(AgentWorkspaceImportRecordModel, prepared.import_id) is None


def test_restore_completion_outcome_has_no_import_audit_contract(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_restore(module, client, agent_id="restore-audit-na")
        with prepared.store.workspace_activation_guard():
            git_operations.activate_candidate(
                prepared.store,
                snapshot=prepared.snapshot,
                candidate_commit=prepared.candidate_commit,
                operation_id=prepared.operation_id,
                before_activate=lambda: None,
            )
        service._stage_completion(prepared.operation_id)
        operation = _operation(module, prepared.operation_id)
        assert operation.state == "completing"
        assert operation.action == "restore" and operation.import_id is None
        with service._Session() as db:
            before = db.scalar(select(func.count()).select_from(AgentWorkspaceImportRecordModel))
        service._persist_accepted_import = lambda *_args, **_kwargs: pytest.fail("restore resume fabricated an import audit")
        prepared.lease.close(validate_claim=False)
        summary = service.reconcile(force=True)
        with service._Session() as db:
            after = db.scalar(select(func.count()).select_from(AgentWorkspaceImportRecordModel))

    assert summary["completed"] == [prepared.operation_id]
    assert before == after


@pytest.mark.parametrize("target", ["completed", "rejected"])
def test_terminal_commit_ack_loss_resolves_from_durable_state(
    monkeypatch,
    tmp_path: Path,
    target: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    service = module.workspace_activation_service
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-terminal-ack-loss",
        )
        _stage_outcome(module, prepared, target=target)
        original_commit = service._commit_terminal

        def commit_then_lose_ack(operation_id: str, *, target: str) -> None:
            original_commit(operation_id, target=target)
            raise RuntimeError("terminal commit acknowledgement lost")

        monkeypatch.setattr(service, "_commit_terminal", commit_then_lose_ack)
        prepared.lease.close(validate_claim=False)
        summary = service.reconcile(force=True)

    assert summary[target] == [prepared.operation_id]
    assert _operation(module, prepared.operation_id).state == target


@pytest.mark.parametrize(
    "mutation",
    [
        "status_only",
        "audit",
        "admission",
        "refs",
        "active_test",
        "workspace",
        "index_assume_unchanged",
        "index_skip_worktree",
        "graph",
    ],
)
def test_operator_resume_rejects_partial_terminal_evidence_and_retains_attempt(
    tmp_path: Path,
    mutation: str,
) -> None:
    harness = build_recovery_harness(tmp_path)
    request = recovery_request(harness)
    attempts = WorkspaceActivationRecoveryAttemptStore(harness.Session)
    attempts.reserve(request)
    attempts.mark_started(
        request.recovery_id,
        observed_state_digest=request.state_digest,
        observed_context_digest=_DIGEST,
    )
    stage_completed_terminal_for_resume(harness, request, mutation=mutation)

    with pytest.raises(OperatorRecoveryError) as exc_info:
        harness.service.resume(request.recovery_id)

    attempt = attempts.get(request.recovery_id)
    assert exc_info.value.code == "ACTIVATION_TERMINAL_EVIDENCE_MISMATCH"
    assert attempt is not None and attempt.state == "failed"
    assert attempt.error == {"code": "ACTIVATION_TERMINAL_EVIDENCE_MISMATCH"}
    assert attempt.observed_state_digest == request.state_digest
    assert attempt.observed_context_digest == _DIGEST
    assert attempt.started_at is not None


@pytest.mark.parametrize("target", ["completed", "rejected"])
def test_terminal_graph_journal_is_immutable_after_preparation(
    monkeypatch,
    tmp_path: Path,
    target: str,
) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        prepared = _prepare_import_activation(
            module,
            client,
            tmp_path,
            agent_id=f"{target}-invalid-snapshot-graph",
        )
        _stage_outcome(module, prepared, target=target)
        prepared.lease.close(validate_claim=False)
        summary = module.workspace_activation_service.reconcile(force=True)
        assert summary[target] == [prepared.operation_id]
        with pytest.raises(IntegrityError, match="graph journal is immutable"):
            with module.workspace_activation_service._Session.begin() as db:
                operation = db.get(AgentWorkspaceActivationOperationModel, prepared.operation_id)
                assert operation is not None and not operation.snapshot_created
                operation.snapshot_created = True
        with module.workspace_activation_service._Session() as db:
            operation = db.get(AgentWorkspaceActivationOperationModel, prepared.operation_id)
            assert operation is not None and not operation.snapshot_created
            terminal = verify_exact_terminal_outcome(db, operation, store=prepared.store)
            assert terminal.outcome == target


@pytest.mark.parametrize(
    "mutation",
    ["target_tree", "symbolic_target", "index_commit", "merge_candidate"],
)
def test_operation_graph_rejects_noncanonical_object_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    harness = build_recovery_harness(tmp_path)
    with harness.Session() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, harness.operation_id)
        assert operation is not None
        db.expunge(operation)
    if mutation in {"target_tree", "symbolic_target"}:
        operation.action = "restore"
        operation.target_commit_sha = operation.candidate_tree_sha if mutation == "target_tree" else "HEAD~1"
    elif mutation == "index_commit":
        operation.original_index_tree_sha = operation.candidate_commit_sha
    else:
        assert operation.base_commit_sha and operation.candidate_commit_sha and operation.candidate_tree_sha
        operation.candidate_commit_sha = git(
            harness.workspace,
            "commit-tree",
            operation.candidate_tree_sha,
            "-p",
            operation.base_commit_sha,
            "-p",
            operation.candidate_commit_sha,
            "-m",
            "hostile merge candidate",
        )
    with pytest.raises(WorkspaceActivationVerificationError, match="commit graph"):
        verify_operation_graph(operation, harness.store)


def test_governed_graph_ignores_git_replace_objects(tmp_path: Path) -> None:
    harness = build_recovery_harness(tmp_path)
    with harness.Session() as db:
        operation = db.get(AgentWorkspaceActivationOperationModel, harness.operation_id)
        assert operation is not None
        db.expunge(operation)
    assert operation.base_commit_sha and operation.candidate_commit_sha and operation.candidate_tree_sha
    original_tree = git(harness.workspace, "rev-parse", f"{operation.original_head_sha}^{{tree}}")
    replacement = git(
        harness.workspace,
        "commit-tree",
        original_tree,
        "-p",
        operation.base_commit_sha,
        "-m",
        "hostile replacement",
    )
    git(harness.workspace, "replace", operation.candidate_commit_sha, replacement)
    assert git(harness.workspace, "rev-parse", f"{operation.candidate_commit_sha}^{{tree}}") == original_tree

    verify_operation_graph(operation, harness.store)
    operation.candidate_tree_sha = original_tree
    with pytest.raises(WorkspaceActivationVerificationError, match="commit graph"):
        verify_operation_graph(operation, harness.store)


def test_governed_index_reads_fsmonitor_flags_without_running_repository_hook(
    monkeypatch,
    tmp_path: Path,
) -> None:
    harness = build_recovery_harness(tmp_path)
    marker = tmp_path / "fsmonitor-executed"
    hook = tmp_path / "hostile-fsmonitor"
    hook.write_text(
        f'#!/bin/sh\ntouch "{marker}"\nprintf "token\\n"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    git(harness.workspace, "config", "core.fsmonitor", str(hook))
    git(harness.workspace, "update-index", "--fsmonitor")
    git(harness.workspace, "update-index", "--fsmonitor-valid", "--", "CLAUDE.md")
    marker.unlink(missing_ok=True)

    core_digest = git_operations.index_fingerprint(harness.workspace)
    hardened_digest = HardenedReadOnlyGitRepository(harness.workspace).index_fingerprint()
    hostile_index = tmp_path / "hostile-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(hostile_index))
    git(harness.workspace, "read-tree", "--empty")
    harness.workspace.joinpath("CLAUDE.md").write_text("dirty but visible\n", encoding="utf-8")

    assert git_operations.index_fingerprint(harness.workspace) == core_digest == hardened_digest
    assert git_operations.workspace_status(harness.workspace)
    assert not marker.exists()


def _add_runtime_blocker(module, agent_id: str, *, blocker: str) -> None:
    with module.workspace_activation_service._Session.begin() as db:
        if blocker == "active_session":
            db.add(
                SessionRecordModel(
                    session_id=f"session-{uuid.uuid4()}",
                    sdk_session_id="active-sdk",
                    agent_id=agent_id,
                    active_run_id=f"run-{uuid.uuid4()}",
                    active_run_expires_at="2099-01-01T00:00:00+00:00",
                    active_run_generation=1,
                )
            )
            return
        if blocker == "running_turn":
            session_id = f"session-{uuid.uuid4()}"
            db.add(SessionRecordModel(session_id=session_id, agent_id=agent_id))
            db.add(
                SessionTurnIntentModel(
                    run_id=f"run-{uuid.uuid4()}",
                    session_id=session_id,
                    agent_id=agent_id,
                    attempted_sdk_session_id="attempted-sdk",
                    sdk_project_key="project",
                    base_turns=0,
                    status="running",
                )
            )
            return
        db.add(
            ClaudeUserInputRequestModel(
                request_id=f"hitl-{uuid.uuid4()}",
                decision_token_hash="hash",
                business_agent_id=agent_id,
                run_id=f"run-{uuid.uuid4()}",
                api_session_id=f"session-{uuid.uuid4()}",
                request_type="ask_user_question",
                tool_name="AskUserQuestion",
                status="waiting",
                expires_at="2099-01-01T00:00:00+00:00",
            )
        )


def _prepare_restore(module, client: TestClient, *, agent_id: str) -> _PreparedRestore:
    created = _import_new_agent(client, agent_id=agent_id, name=agent_id)
    workspace = Path(created.json()["agent"]["workspace_dir"])
    baseline = _git(workspace, "rev-parse", "HEAD")
    workspace.joinpath("CLAUDE.md").write_text("# restore target\n", encoding="utf-8")
    _git(workspace, "add", "--", "CLAUDE.md")
    _git(workspace, "commit", "-m", "Create restore target")
    target = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "reset", "--hard", baseline)
    store = module.agent_governance._store_for(agent_id)
    lease = module.agent_governance.version_maintenance.lease(
        agent_id=agent_id,
        kind="workspace_restore",
        owner_id=f"test:{agent_id}",
    )
    lease.__enter__()
    with store.mutation_guard():
        observation = git_operations.observe_live_workspace(store, expected_head=baseline)
        preparation = module.workspace_activation_service.begin_restore(
            agent_id=agent_id,
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
            message="Prepare restore terminal invariant",
            operation_id=preparation.operation_id,
        )
        module.workspace_activation_service.prepare_restore(
            preparation.operation_id,
            snapshot=snapshot,
            replacement=replacement,
            target_commit_sha=target,
        )
    return _PreparedRestore(
        operation_id=preparation.operation_id,
        agent_id=agent_id,
        workspace=workspace,
        candidate_commit=replacement.current_commit_sha,
        snapshot=snapshot,
        lease=lease,
        store=store,
    )
