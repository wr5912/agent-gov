from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path


@cache
def _resolved_worktree_prefix(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-prefix"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_project_tree_ref(root: Path, repository_ref: str) -> str | None:
    """Resolve a repository ref to the tree rooted at the current project."""
    prefix = _resolved_worktree_prefix(root.resolve()).rstrip("/")
    if not prefix:
        return repository_ref
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{repository_ref}:{prefix}"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None
