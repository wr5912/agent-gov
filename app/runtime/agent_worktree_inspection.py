from __future__ import annotations

from pathlib import Path

from app.runtime.agent_git_errors import AgentGitError
from app.runtime.agent_git_read_helpers import run_git_read_only, safe_relative_path
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_git_workspace_diff import parse_workspace_changes


def inspect_clean_worktree(store: GitAgentVersionStore, worktree_path: Path) -> tuple[str, bool]:
    """在版本 store 锁内校验其拥有的候选 worktree HEAD 与 dirty 状态。"""

    with store._lock:  # noqa: SLF001 - 与 GitAgentVersionStore 同属 runtime 内部原语
        safe_path = store._owned_worktree_path(worktree_path)  # noqa: SLF001
        if not safe_path.exists() or not (safe_path / ".git").exists():
            raise AgentGitError("Candidate worktree is missing")
        commit = run_git_read_only(["rev-parse", "--verify", "HEAD^{commit}"], cwd=safe_path).strip()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise AgentGitError("Candidate worktree HEAD is not a full Git commit")
        raw_status = run_git_read_only(
            ["status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "--ignored"],
            cwd=safe_path,
        )
        changes = parse_workspace_changes(raw_status, normalize_path=safe_relative_path)
        return commit, bool(changes)
