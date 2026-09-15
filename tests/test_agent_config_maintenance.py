from __future__ import annotations

from pathlib import Path

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_candidate_writer import AgentCandidateWriter
from app.services.agent_governance import AgentGovernanceService

from feedback_store_test_utils import _settings


def test_candidate_write_leaves_the_live_workspace_unchanged(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    governance = AgentGovernanceService(
        feedback_store=FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.default_workspace_dir),
        agent_version_store=GitAgentVersionStore(
            repository_dir=settings.default_workspace_dir,
            worktrees_dir=settings.agent_git_worktrees_dir,
            releases_dir=settings.agent_release_archives_dir,
        ),
    )
    change_set = governance.create_change_set(title="isolated")
    original = settings.default_workspace_dir.joinpath("AGENT.md").read_text(encoding="utf-8")

    result = AgentCandidateWriter(governance).write_text_files(
        change_set_id=str(change_set["change_set_id"]),
        files=(("AGENT.md", "# Candidate only\n", None, 0o644),),
        expected_candidate_commit_sha=str(change_set["base_commit_sha"]),
        operator="tester",
        note=None,
    )

    assert result["published"] is False
    assert settings.default_workspace_dir.joinpath("AGENT.md").read_text(encoding="utf-8") == original
