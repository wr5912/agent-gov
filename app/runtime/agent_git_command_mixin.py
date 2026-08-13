from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from app.runtime.agent_git_environment import (
    GovernedGitEnvironmentError,
    governed_git_command,
    governed_git_environment,
    require_governed_repository,
)
from app.runtime.agent_git_errors import AgentGitError
from app.runtime.agent_git_raw_storage import (
    RawGitStorageError,
    configure_raw_git_storage,
    update_git_info_exclude,
)
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import utc_now
from app.runtime.workspace_policy import WORKSPACE_EXCLUDED_NAMES, WORKSPACE_EXCLUDED_PATTERNS


class AgentGitCommandMixin:
    """Low-level Git and path primitives shared by the per-Agent store."""

    repository_dir: Path
    worktrees_dir: Path
    git_user_name: str
    git_user_email: str

    def _ensure_git_available(self) -> None:
        if shutil.which("git") is None:
            raise AgentGitError("git executable is not available")

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        optional_locks: bool | None = None,
    ) -> str:
        try:
            if not (args and args[0] == "init"):
                require_governed_repository(cwd)
            command = governed_git_command(
                cwd,
                args,
                allow_initialization=bool(args and args[0] == "init"),
            )
        except GovernedGitEnvironmentError as exc:
            raise AgentGitError("Agent Git command authority rejected the repository") from exc
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=governed_git_environment(repository=cwd, optional_locks=optional_locks),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise AgentGitError(detail or f"git {' '.join(args)} failed with {proc.returncode}")
        return proc.stdout

    def _configure_repo(self, cwd: Path) -> None:
        self._ensure_safe_directory(cwd)
        try:
            configure_raw_git_storage(
                cwd,
                run_git=lambda args, repository: self._git(args, cwd=repository),
            )
        except RawGitStorageError as exc:
            raise AgentGitError(str(exc)) from exc
        self._git(["config", "user.name", self.git_user_name], cwd=cwd)
        self._git(["config", "user.email", self.git_user_email], cwd=cwd)

    def _ensure_safe_directory(self, cwd: Path) -> None:
        try:
            require_governed_repository(cwd)
        except GovernedGitEnvironmentError as exc:
            raise AgentGitError("Agent Git command authority rejected the repository") from exc

    def _write_info_exclude(self, cwd: Path) -> None:
        lines = ["# Agent runtime managed excludes"]
        lines.extend(sorted(WORKSPACE_EXCLUDED_NAMES))
        lines.extend(WORKSPACE_EXCLUDED_PATTERNS)
        addition = "\n".join(lines) + "\n"
        try:
            update_git_info_exclude(
                cwd,
                marker="Agent runtime managed excludes",
                addition=addition,
            )
        except RawGitStorageError as exc:
            raise AgentGitError(str(exc)) from exc

    def _has_head(self, cwd: Path) -> bool:
        return bool(self._git(["rev-parse", "--verify", "HEAD"], cwd=cwd, check=False).strip())

    def _stage_complete_workspace(self, cwd: Path) -> None:
        self._git(["config", "core.fileMode", "true"], cwd=cwd)
        self._git(["add", "-A", "-f", "--", "."], cwd=cwd)
        self._git(["add", "--renormalize", "--ignore-errors", "--", "."], cwd=cwd)

    def _has_staged_changes(self, cwd: Path) -> bool:
        try:
            require_governed_repository(cwd)
            command = governed_git_command(cwd, ["diff", "--cached", "--quiet"])
        except GovernedGitEnvironmentError as exc:
            raise AgentGitError("Agent Git command authority rejected the repository") from exc
        process = subprocess.run(
            command,
            cwd=str(cwd),
            env=governed_git_environment(repository=cwd),
            capture_output=True,
            check=False,
        )
        if process.returncode not in {0, 1}:
            raise AgentGitError("Agent Git staged-change inspection failed")
        return process.returncode == 1

    def _commit_empty(self, message: str, *, cwd: Path) -> None:
        self._git(["commit", "--allow-empty", "-m", message], cwd=cwd)

    def _resolve_ref(self, ref: str) -> str:
        value = self._git(["rev-parse", "--verify", ref], cwd=self.repository_dir).strip()
        if not value:
            raise AgentGitError(f"Unknown git ref: {ref}")
        return value

    def _resolve_commit(self, ref: str) -> str:
        value = self._git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=self.repository_dir, check=False).strip()
        if not value:
            raise AgentGitError(f"Unknown git commit: {ref}")
        return value

    def _validate_tag_name(self, tag_name: str) -> None:
        if not tag_name or tag_name.startswith("-"):
            raise AgentGitError(f"Invalid release tag name: {tag_name!r}")
        try:
            self._git(["check-ref-format", f"refs/tags/{tag_name}"], cwd=self.repository_dir)
        except AgentGitError as exc:
            raise AgentGitError(f"Invalid release tag name: {tag_name!r}") from exc

    def _commit_created_at(self, commit_sha: str) -> str:
        raw = self._git(["show", "-s", "--format=%cI", commit_sha], cwd=self.repository_dir, check=False).strip()
        return raw or utc_now()

    def _commit_parent(self, commit_sha: str) -> str | None:
        raw = self._git(["rev-list", "--parents", "-n", "1", commit_sha], cwd=self.repository_dir, check=False).strip()
        parts = raw.split()
        return parts[1] if len(parts) > 1 else None

    def _tracked_file_count(self, commit_sha: str) -> int:
        raw = self._git(["ls-tree", "-r", "--name-only", commit_sha], cwd=self.repository_dir, check=False)
        return sum(1 for line in raw.splitlines() if line.strip())

    def _file_entry(self, ref: str, path: str) -> JsonObject | None:
        data = self._read_file_at_ref(ref, path)
        if data is None:
            return None
        return {"path": path, "type": "file", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def _read_file_at_ref(self, ref: str, path: str) -> bytes | None:
        safe_path = self._safe_relative_path(path)
        if not safe_path:
            return None
        try:
            require_governed_repository(self.repository_dir)
            command = governed_git_command(
                self.repository_dir,
                ["show", f"{ref}:{safe_path}"],
            )
        except GovernedGitEnvironmentError as exc:
            raise AgentGitError("Agent Git command authority rejected the repository") from exc
        proc = subprocess.run(
            command,
            cwd=str(self.repository_dir),
            env=governed_git_environment(repository=self.repository_dir),
            capture_output=True,
            check=False,
        )
        return None if proc.returncode != 0 else proc.stdout

    def _file_diff_status(self, before: bytes | None, after: bytes | None) -> str:
        if before is None and after is None:
            return "missing"
        if before is None:
            return "added"
        if after is None:
            return "deleted"
        return "unchanged" if before == after else "modified"

    def _safe_relative_path(self, path: str) -> str | None:
        raw = str(path or "").strip().replace("\\", "/")
        if raw.startswith("workspace/"):
            raw = raw.removeprefix("workspace/")
        rel = Path(raw)
        if not raw or rel.is_absolute() or ".." in rel.parts:
            return None
        return rel.as_posix()

    def _owned_worktree_path(self, path: Path) -> Path:
        expanded = path.expanduser()
        lexical = Path(os.path.abspath(expanded))
        resolved = expanded.resolve()
        root_expanded = self.worktrees_dir.expanduser()
        root_lexical = Path(os.path.abspath(root_expanded))
        root_resolved = root_expanded.resolve()
        if lexical != resolved or root_lexical != root_resolved or lexical.parent != root_lexical:
            raise AgentGitError("Candidate worktree path escapes the governed worktree root")
        return lexical

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
