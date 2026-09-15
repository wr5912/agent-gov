from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent_testing.store import AgentTestingStore
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_maintenance_db import AgentWorktreeCleanupTaskModel
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import AgentChangeSetModel, utc_now
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService

from agent_release_test_utils import install_release_activation_boundary
from feedback_store_test_utils import _settings


def _governance(tmp_path):
    settings = _settings(tmp_path)
    _write_real_workspace_suite(settings.default_workspace_dir)
    git_store = GitAgentVersionStore(
        repository_dir=settings.default_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    git_store.ensure_bootstrap()
    feedback_store = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.default_workspace_dir)
    governance = AgentGovernanceService(
        feedback_store=feedback_store,
        agent_version_store=git_store,
    )
    testing_store = AgentTestingStore(feedback_store.Session)

    governance.latest_passed_test_run = testing_store.latest_passed_for_commit
    governance.latest_candidate_test_run = testing_store.latest_for_candidate
    install_release_activation_boundary(governance)
    feedback_store.agent_version_provider = governance.current_agent_version_id
    return governance, git_store


def _write_real_workspace_suite(workspace: Path) -> None:
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.joinpath("README.md").write_text("# 维护恢复测试\n", encoding="utf-8")
    tests_dir.joinpath("test_agent.py").write_text(
        "from pathlib import Path\n\n"
        "def test_harness_contains_required_sources():\n"
        "    root = Path(__file__).parents[1]\n"
        "    assert (root / 'AGENT.md').is_file()\n"
        "    assert (root / 'agent.yaml').is_file()\n",
        encoding="utf-8",
    )


def test_worktree_cleanup_reconciles_expired_claim_after_real_git_delete(tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    change_set = governance.create_change_set(title="cleanup recovery", operator="tester")
    change_set_id = str(change_set["change_set_id"])
    worktree = Path(str(change_set["worktree_path"]))
    assert worktree.exists()
    timestamp = utc_now()
    with governance.feedback_store.Session.begin() as db:
        row = db.get(AgentChangeSetModel, change_set_id)
        assert row is not None
        row.status = "abandoned"
        row.updated_at = timestamp
        row.payload_json = {
            **dict(row.payload_json or {}),
            "status": "abandoned",
            "worktree_cleanup_pending": True,
        }
        db.add(
            AgentWorktreeCleanupTaskModel(
                change_set_id=change_set_id,
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
                status="claimed",
                delete_branch=True,
                attempt_count=1,
                claim_token="expired-cleanup-token",
                claim_generation=1,
                claim_expires_at="2000-01-01T00:00:00+00:00",
                next_retry_at=None,
                last_error_json={},
                created_at=timestamp,
                updated_at=timestamp,
                completed_at=None,
            )
        )
    git_store.remove_worktree(change_set_id, delete_branch=True)
    assert not worktree.exists()

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


def test_publish_is_fenced_by_real_durable_maintenance_claim_before_git_side_effect(tmp_path) -> None:
    governance, git_store = _governance(tmp_path)
    change_set = governance.create_change_set(title="fenced publication", operator="tester")
    worktree = Path(str(change_set["worktree_path"]))
    worktree.joinpath("candidate-note.md").write_text("candidate\n", encoding="utf-8")
    candidate = git_store.commit_worktree(worktree, message="fenced candidate")
    committed = governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate,
        execution_job_id="job-fenced-publication",
        operator="tester",
    )
    original_head = git_store.current_commit_sha()

    with governance.version_maintenance.lease(
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        kind="operator-maintenance",
        owner_id="operator",
    ):
        with pytest.raises(AgentGovernanceError, match="maintenance"):
            asyncio.run(
                governance.publish_change_set_async(
                    str(change_set["change_set_id"]),
                    operator="tester",
                    expected_candidate_commit_sha=candidate,
                    expected_diff_digest=str(committed["diff_summary"]["digest"]),
                )
            )

    assert git_store.current_commit_sha() == original_head
    assert governance.get_change_set(str(change_set["change_set_id"]))["status"] == "candidate_committed"
