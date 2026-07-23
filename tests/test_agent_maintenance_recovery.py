from __future__ import annotations

from pathlib import Path

import app.services.agent_change_set_worktree_lifecycle as cleanup_module
import app.services.agent_release_workflows as release_module
import pytest
from app.runtime.agent_admission import AgentMaintenanceClaim, AgentMaintenanceClaimLost
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import (
    AgentReleaseOperationModel,
    AgentWorktreeCleanupTaskModel,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService

from business_agent_test_utils import ORDINARY_TEST_AGENT_ID
from feedback_store_test_utils import _settings


def _governance(tmp_path):
    settings = _settings(tmp_path)
    git_store = GitAgentVersionStore(
        repository_dir=settings.default_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    git_store.ensure_bootstrap()
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
        agent_version_provider=lambda _aid=None: git_store.current_version_id(),
    )
    governance = AgentGovernanceService(
        feedback_store=feedback_store,
        agent_version_store=git_store,
        runtime_mode=settings.runtime_volume_mode,
        runtime_env={"MCP_SERVER_URL": "http://localhost:58001/mcp"},
    )
    governance.latest_passed_test_run = lambda agent_id, commit_sha: {
        "test_run_id": f"atr-{commit_sha[:12]}",
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "status": "passed",
    }
    return governance, git_store


def _publish(governance, git_store, content: str):
    change_set = governance.create_change_set(title="maintenance recovery", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("CLAUDE.md").write_text(content, encoding="utf-8")
    candidate = git_store.commit_worktree(worktree, message="maintenance recovery candidate")
    governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-maintenance-recovery",
        operator="tester",
    )
    return governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")


def _expire_release_operation(governance) -> AgentReleaseOperationModel:
    with governance.feedback_store.Session.begin() as db:
        operation = db.query(AgentReleaseOperationModel).one()
        operation.claim_expires_at = "2000-01-01T00:00:00+00:00"
        operation_id = operation.operation_id
    with governance.feedback_store.Session() as db:
        persisted = db.get(AgentReleaseOperationModel, operation_id)
        assert persisted is not None
        db.expunge(persisted)
        return persisted


def test_restore_reconciles_crash_after_git_before_operation_persistence(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    first = _publish(governance, git_store, "v1\n")
    _publish(governance, git_store, "v2\n")
    original = release_module._mark_git_applied

    def crash_after_git(*args, **kwargs):
        raise KeyboardInterrupt("simulated process crash")

    monkeypatch.setattr(release_module, "_mark_git_applied", crash_after_git)
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        governance.restore_release(str(first["release_id"]), operator="tester")
    assert git_store.current_commit_sha() == first["commit_sha"]

    monkeypatch.setattr(release_module, "_mark_git_applied", original)
    restored = governance.restore_release(str(first["release_id"]), operator="reconciler")

    assert restored["restore_result"]["reconciled_after_interruption"] is True
    with governance.feedback_store.Session() as db:
        operations = db.query(AgentReleaseOperationModel).all()
        assert len(operations) == 1
        assert operations[0].status == "completed"
        assert operations[0].claim_token is None


def test_restore_expected_head_rejects_intervening_git_change(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    first = _publish(governance, git_store, "v1\n")
    second = _publish(governance, git_store, "v2\n")
    intervening = str(first["previous_commit_sha"])
    original = release_module._apply_or_reconcile_git

    def change_head_before_cas(service, store, claim, *, current_head):
        store.rollback_to_ref(intervening)
        return original(service, store, claim, current_head=current_head)

    monkeypatch.setattr(release_module, "_apply_or_reconcile_git", change_head_before_cas)
    with pytest.raises(AgentGovernanceError, match="HEAD changed"):
        governance.restore_release(str(first["release_id"]), operator="tester")

    assert git_store.current_commit_sha() == intervening
    assert git_store.current_commit_sha() != second["commit_sha"]
    with governance.feedback_store.Session() as db:
        operation = db.query(AgentReleaseOperationModel).one()
        assert operation.status == "failed"
        assert "HEAD changed" in str(operation.error_json.get("detail"))


def test_reconciler_applies_expired_reserved_rollback_before_git(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    release = _publish(governance, git_store, "v2\n")
    original = release_module._apply_or_reconcile_git

    def crash_before_git(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated crash before rollback Git")

    monkeypatch.setattr(release_module, "_apply_or_reconcile_git", crash_before_git)
    with pytest.raises(KeyboardInterrupt, match="before rollback Git"):
        governance.rollback_release(str(release["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)
    assert operation.status == "reserved"
    assert git_store.current_commit_sha() == release["commit_sha"]

    monkeypatch.setattr(release_module, "_apply_or_reconcile_git", original)
    summary = governance.reconcile_release_operations()

    assert summary == {"completed": [operation.operation_id], "failed": [], "deferred": []}
    assert git_store.current_commit_sha() == release["previous_commit_sha"]
    assert governance.get_release(str(release["release_id"]))["status"] == "rolled_back"


def test_reconciler_completes_reserved_rollback_after_git(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    release = _publish(governance, git_store, "v2\n")
    original = release_module._mark_git_applied

    def crash_after_git(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated crash after rollback Git")

    monkeypatch.setattr(release_module, "_mark_git_applied", crash_after_git)
    with pytest.raises(KeyboardInterrupt, match="after rollback Git"):
        governance.rollback_release(str(release["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)
    assert operation.status == "reserved"
    assert git_store.current_commit_sha() == release["previous_commit_sha"]

    monkeypatch.setattr(release_module, "_mark_git_applied", original)
    summary = governance.reconcile_release_operations()

    assert summary["completed"] == [operation.operation_id]
    assert governance.get_release(str(release["release_id"]))["status"] == "rolled_back"


def test_reconciler_completes_expired_git_applied_without_repeating_reset(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    release = _publish(governance, git_store, "v2\n")
    original_complete = release_module._complete_release_operation

    def crash_before_completion(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated crash before metadata completion")

    monkeypatch.setattr(release_module, "_complete_release_operation", crash_before_completion)
    with pytest.raises(KeyboardInterrupt, match="metadata completion"):
        governance.rollback_release(str(release["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)
    assert operation.status == "git_applied"
    expected_head = git_store.current_commit_sha()

    reset_called = False

    def forbidden_reset(*_args, **_kwargs):
        nonlocal reset_called
        reset_called = True
        raise AssertionError("git_applied reconciliation must not repeat reset")

    monkeypatch.setattr(release_module, "_complete_release_operation", original_complete)
    monkeypatch.setattr(git_store, "rollback_to_ref", forbidden_reset)
    summary = governance.reconcile_release_operations()

    assert summary["completed"] == [operation.operation_id]
    assert reset_called is False
    assert git_store.current_commit_sha() == expected_head


def test_reconciler_fails_reserved_rollback_when_head_is_unrelated(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    release = _publish(governance, git_store, "v2\n")
    original = release_module._apply_or_reconcile_git

    def crash_before_git(*_args, **_kwargs):
        raise KeyboardInterrupt("before Git")

    monkeypatch.setattr(release_module, "_apply_or_reconcile_git", crash_before_git)
    with pytest.raises(KeyboardInterrupt):
        governance.rollback_release(str(release["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)
    Path(git_store.repository_dir, "CLAUDE.md").write_text("unrelated\n", encoding="utf-8")
    unrelated_head = git_store.create_snapshot(reason="unrelated")["agent_version_id"]

    monkeypatch.setattr(release_module, "_apply_or_reconcile_git", original)
    summary = governance.reconcile_release_operations()

    assert summary["failed"] == [operation.operation_id]
    assert git_store.current_commit_sha() == unrelated_head
    assert governance.get_release(str(release["release_id"]))["status"] == "rollback_failed"


def test_reconciler_fails_git_applied_when_head_moved_back(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    release = _publish(governance, git_store, "v2\n")
    original_complete = release_module._complete_release_operation

    def crash_after_mark(*_args, **_kwargs):
        raise KeyboardInterrupt("after mark")

    monkeypatch.setattr(release_module, "_complete_release_operation", crash_after_mark)
    with pytest.raises(KeyboardInterrupt):
        governance.rollback_release(str(release["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)
    target_head = git_store.current_commit_sha()
    git_store.rollback_to_ref(str(release["commit_sha"]), expected_current_ref=target_head)

    monkeypatch.setattr(release_module, "_complete_release_operation", original_complete)
    summary = governance.reconcile_release_operations()

    assert summary["failed"] == [operation.operation_id]
    assert git_store.current_commit_sha() == release["commit_sha"]
    assert governance.get_release(str(release["release_id"]))["status"] == "rollback_failed"


def test_reconciler_completes_expired_restore_after_git_without_repeating_reset(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    first = _publish(governance, git_store, "v1\n")
    _publish(governance, git_store, "v2\n")
    original_mark = release_module._mark_git_applied

    def crash_after_restore(*_args, **_kwargs):
        raise KeyboardInterrupt("restore crash")

    monkeypatch.setattr(release_module, "_mark_git_applied", crash_after_restore)
    with pytest.raises(KeyboardInterrupt):
        governance.restore_release(str(first["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)
    restored_head = git_store.current_commit_sha()
    reset_called = False

    def forbidden_reset(*_args, **_kwargs):
        nonlocal reset_called
        reset_called = True
        raise AssertionError("restore reconciliation must not repeat an applied reset")

    monkeypatch.setattr(release_module, "_mark_git_applied", original_mark)
    monkeypatch.setattr(git_store, "rollback_to_ref", forbidden_reset)
    summary = governance.reconcile_release_operations()

    assert summary == {"completed": [operation.operation_id], "failed": [], "deferred": []}
    assert reset_called is False
    assert restored_head == first["commit_sha"] == git_store.current_commit_sha()
    with governance.feedback_store.Session() as db:
        persisted = db.get(AgentReleaseOperationModel, operation.operation_id)
        assert persisted is not None and persisted.status == "completed"
        assert persisted.claim_token is None


def test_release_reconciler_isolates_unexpected_operation_failure(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    release = _publish(governance, git_store, "v2\n")

    monkeypatch.setattr(
        release_module,
        "_apply_or_reconcile_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("reserve rollback")),
    )
    with pytest.raises(KeyboardInterrupt):
        governance.rollback_release(str(release["release_id"]), operator="tester")
    operation = _expire_release_operation(governance)

    monkeypatch.setattr(
        release_module,
        "_apply_or_reconcile_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected reconciler failure")),
    )

    summary = governance.reconcile_release_operations()

    assert summary == {"completed": [], "failed": [], "deferred": [operation.operation_id]}


def test_worktree_cleanup_reconciles_crash_after_idempotent_git_delete(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    change_set = governance.create_change_set(title="cleanup recovery", operator="tester")
    change_set_id = str(change_set["change_set_id"])
    worktree = Path(str(change_set["worktree_path"]))
    original = cleanup_module._complete_cleanup_task

    def crash_after_delete(*args, **kwargs):
        raise KeyboardInterrupt("simulated cleanup crash")

    monkeypatch.setattr(cleanup_module, "_complete_cleanup_task", crash_after_delete)
    with pytest.raises(KeyboardInterrupt, match="simulated cleanup crash"):
        governance.abandon_change_set(change_set_id, operator="tester")
    assert not worktree.exists()

    with governance.feedback_store.Session.begin() as db:
        task = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
        assert task is not None and task.status == "claimed"
        task.claim_expires_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(cleanup_module, "_complete_cleanup_task", original)
    recovered = governance.retry_worktree_cleanup(
        change_set_id,
        operator="reconciler",
        force=True,
    )

    assert recovered["worktree_cleanup_pending"] is False
    assert recovered["worktree_cleanup"]["status"] == "completed"
    with governance.feedback_store.Session() as db:
        task = db.get(AgentWorktreeCleanupTaskModel, change_set_id)
        assert task is not None and task.status == "completed" and task.attempt_count == 2


def test_rollback_rejects_non_current_release_without_resetting_newer_head(tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    second = _publish(governance, git_store, "v2\n")
    third = _publish(governance, git_store, "v3\n")

    with pytest.raises(AgentGovernanceError, match="release commit to be the current"):
        governance.rollback_release(str(second["release_id"]), operator="tester")

    assert git_store.current_commit_sha() == third["commit_sha"]
    assert governance.get_release(str(second["release_id"]))["status"] == "published"


def test_completed_rollback_cache_cannot_report_success_after_restore(tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    _publish(governance, git_store, "v1\n")
    second = _publish(governance, git_store, "v2\n")
    governance.rollback_release(str(second["release_id"]), operator="tester")
    governance.restore_release(str(second["release_id"]), operator="tester")
    assert git_store.current_commit_sha() == second["commit_sha"]

    with pytest.raises(AgentGovernanceError, match="already completed.*current HEAD"):
        governance.rollback_release(str(second["release_id"]), operator="tester")

    assert git_store.current_commit_sha() == second["commit_sha"]


def test_publish_revalidates_durable_claim_before_git_side_effect(monkeypatch, tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    change_set = governance.create_change_set(title="fenced publication", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("CLAUDE.md").write_text("candidate\n", encoding="utf-8")
    candidate = git_store.commit_worktree(worktree, message="fenced candidate")
    governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-fenced-publication",
        operator="tester",
    )
    publish_called = False

    def forbidden_publish(*args, **kwargs):
        nonlocal publish_called
        publish_called = True
        raise AssertionError("publish_commit must not run after durable claim loss")

    class ExpiredLease:
        claim = AgentMaintenanceClaim(
            agent_id=ORDINARY_TEST_AGENT_ID,
            token="expired-token",
            generation=1,
            kind="publish",
            owner_id="tester",
            expires_at="2000-01-01T00:00:00+00:00",
        )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def assert_active(self):
            raise AgentMaintenanceClaimLost("expired before side effect")

        def check(self):
            return None

    monkeypatch.setattr(governance.version_maintenance, "lease", lambda **_kwargs: ExpiredLease())
    monkeypatch.setattr(git_store, "publish_commit", forbidden_publish)

    with pytest.raises(AgentGovernanceError, match="expired before side effect"):
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert publish_called is False
    assert governance.get_change_set(str(change_set["change_set_id"]))["status"] == "candidate_committed"
