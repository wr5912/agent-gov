from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.runtime.agent_git_store import GitAgentVersionStore


def commit_squashed_worktree(
    store: GitAgentVersionStore,
    worktree_path: Path,
    *,
    base_ref: str,
    message: str,
) -> str:
    """Create one candidate commit containing every change relative to the pinned base."""
    from app.runtime.agent_git_store import AgentGitError

    with store._mutation_guard():
        safe_path = store._owned_worktree_path(worktree_path)
        if not safe_path.exists() or not (safe_path / ".git").exists():
            raise AgentGitError("Candidate worktree is missing")
        base_commit = store._resolve_ref(base_ref)
        store._configure_repo(safe_path)
        store._write_info_exclude(safe_path)
        store._git(["reset", "--soft", base_commit], cwd=safe_path)
        store._stage_complete_workspace(safe_path)
        if not store._has_staged_changes(safe_path):
            raise AgentGitError("Candidate worktree has no changes relative to its base commit")
        store._git(["commit", "-m", message], cwd=safe_path)
        commit = store._git(["rev-parse", "HEAD"], cwd=safe_path).strip()
        parent = store._git(["rev-parse", "HEAD^"], cwd=safe_path).strip()
        if not commit or parent != base_commit:
            raise AgentGitError("Candidate worktree was not reduced to one commit over its pinned base")
        return commit
