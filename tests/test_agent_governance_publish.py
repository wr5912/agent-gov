from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService

from feedback_store_test_utils import _settings


def _governance(tmp_path):
    settings = _settings(tmp_path)
    agent_store = GitAgentVersionStore(
        repository_dir=settings.main_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    agent_store.ensure_bootstrap()
    store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.main_workspace_dir,
        agent_version_provider=agent_store.current_version_id,
    )
    return AgentGovernanceService(feedback_store=store, agent_version_store=agent_store), agent_store


def _candidate_change_set(
    governance: AgentGovernanceService,
    agent_store: GitAgentVersionStore,
    *,
    content: str = "# Test Agent\n\n发布候选变更。\n",
):
    change_set = governance.create_change_set(title="候选发布测试", operator="tester")
    worktree_path = Path(str(change_set["worktree_path"]))
    worktree_path.joinpath("CLAUDE.md").write_text(content, encoding="utf-8")
    candidate_commit = agent_store.commit_worktree(worktree_path, message="Commit candidate change")
    return governance.mark_candidate_committed(
        str(change_set["change_set_id"]),
        candidate_commit_sha=candidate_commit,
        execution_job_id="job-publish-test",
        operator="tester",
    )


def test_candidate_committed_change_set_can_publish_directly(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)

    release = governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert release["commit_sha"] == change_set["candidate_commit_sha"]
    assert release["status"] == "published"
    assert agent_store.current_commit_sha() == change_set["candidate_commit_sha"]
    assert governance.get_change_set(str(change_set["change_set_id"]))["status"] == "published"


def test_restore_release_switches_current_workspace_without_mutating_release_history(tmp_path):
    governance, agent_store = _governance(tmp_path)
    first_change_set = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv1\n")
    first_release = governance.publish_change_set(str(first_change_set["change_set_id"]), operator="tester")
    second_change_set = _candidate_change_set(governance, agent_store, content="# Test Agent\n\nv2\n")
    second_release = governance.publish_change_set(str(second_change_set["change_set_id"]), operator="tester")

    assert agent_store.current_commit_sha() == second_release["commit_sha"]

    restore = governance.restore_release(str(first_release["release_id"]), operator="tester", note="切换到 v1")

    assert restore["release"]["release_id"] == first_release["release_id"]
    assert restore["release"]["status"] == "published"
    assert restore["restore_result"]["current_commit_sha"] == first_release["commit_sha"]
    assert agent_store.current_commit_sha() == first_release["commit_sha"]
    assert governance.get_release(str(first_release["release_id"]))["status"] == "published"
    assert governance.get_release(str(second_release["release_id"]))["status"] == "published"


def test_terminal_change_set_cannot_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    governance.reject_change_set(str(change_set["change_set_id"]), operator="tester")

    with pytest.raises(AgentGovernanceError, match="cannot be published from status rejected") as exc:
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert exc.value.status_code == 409


def test_batch_regression_failed_cases_block_publish(tmp_path):
    governance, agent_store = _governance(tmp_path)
    change_set = _candidate_change_set(governance, agent_store)
    governance.mark_regression_running(str(change_set["change_set_id"]), eval_run_id="pending", operator="tester")
    regression_failed = governance.complete_regression(
        str(change_set["change_set_id"]),
        eval_run={
            "eval_run_id": "evr-batch-failed",
            "source": "optimization_batch_regression",
            "change_set_id": change_set["change_set_id"],
            "result_status": "passed_with_notes",
            "summary": {"total": 1, "passed": 0, "failed": 1, "needs_human_review": 0},
            "gate_result": {"status": "passed_with_notes", "note_case_ids": ["evc-failed"]},
            "items": [{"eval_case_id": "evc-failed", "status": "failed"}],
        },
        operator="tester",
    )

    assert regression_failed["status"] == "regression_failed"
    assert "批次回归存在失败用例" in regression_failed["publication_blocker"]
    with pytest.raises(AgentGovernanceError, match="批次回归存在失败用例") as exc:
        governance.publish_change_set(str(change_set["change_set_id"]), operator="tester")

    assert exc.value.status_code == 409
    assert agent_store.current_commit_sha() != change_set["candidate_commit_sha"]
