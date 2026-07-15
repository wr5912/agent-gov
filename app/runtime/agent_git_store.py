from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_git_workspace_diff import (
    MAX_FILE_DIFF_BYTES,
    parse_workspace_changes,
    redact_sensitive_diff,
    untracked_workspace_file_diff,
    workspace_diff_error,
)
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import utc_now
from app.runtime.workspace_policy import WORKSPACE_EXCLUDED_NAMES, WORKSPACE_EXCLUDED_PATTERNS

MAX_REPOSITORY_STATUS_DIFFS = 20


class AgentVersionProvider(Protocol):
    def ensure_bootstrap(self) -> JsonObject: ...

    def current_version_id(self) -> Optional[str]: ...

    def is_maintenance_active(self) -> bool: ...


class AgentGitError(RuntimeError):
    """Raised when Git-backed Agent governance cannot complete an operation."""


@dataclass(frozen=True)
class GitWorktreeRef:
    change_set_id: str
    branch_name: str
    worktree_path: Path
    base_commit_sha: str


class GitAgentVersionStore:
    """Git-backed Agent version provider.

    The repository is rooted at the main Agent workspace. Candidate changes are
    applied in separate Git worktrees and only merged into the main workspace at
    publish time.
    """

    def __init__(
        self,
        *,
        repository_dir: Path,
        worktrees_dir: Path,
        releases_dir: Path,
        service_provider: str = "local",
        service_url: str | None = None,
        service_public_url: str | None = None,
        repository_name: str = "main-agent-config",
        git_user_name: str = "AgentGov",
        git_user_email: str = "agent-runtime@example.local",
    ) -> None:
        self.repository_dir = repository_dir
        self.worktrees_dir = worktrees_dir
        self.releases_dir = releases_dir
        self.service_provider = service_provider
        self.service_url = service_url
        self.service_public_url = service_public_url
        self.repository_name = repository_name
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self._maintenance = False
        self._lock = threading.RLock()
        self._process_lock_path = self.worktrees_dir.parent / ".repository.lock"
        self.repository_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.releases_dir.mkdir(parents=True, exist_ok=True)

    def is_maintenance_active(self) -> bool:
        return self._maintenance

    def ensure_bootstrap(self) -> JsonObject:
        with self._mutation_guard():
            self._ensure_git_available()
            if not (self.repository_dir / ".git").exists():
                self._git(["init"], cwd=self.repository_dir)
            self._configure_repo(self.repository_dir)
            self._write_info_exclude(self.repository_dir)
            if not self._has_head(self.repository_dir):
                self._git(["add", "-A", "--", "."], cwd=self.repository_dir)
                if self._has_staged_changes(self.repository_dir):
                    self._git(["commit", "-m", "Initialize main agent configuration"], cwd=self.repository_dir)
                else:
                    self._commit_empty("Initialize empty main agent configuration", cwd=self.repository_dir)
            return self.version_summary(self._current_commit_sha_no_bootstrap() or "", reason="current")

    def current_version_id(self) -> Optional[str]:
        try:
            return self.current_commit_sha()
        except AgentGitError:
            return None

    def current_commit_sha(self) -> Optional[str]:
        self._ensure_repo_ready()
        return self._current_commit_sha_no_bootstrap()

    def _current_commit_sha_no_bootstrap(self) -> Optional[str]:
        commit = self._git(["rev-parse", "HEAD"], cwd=self.repository_dir).strip()
        return commit or None

    def repository_status(self) -> JsonObject:
        status: JsonObject = {
            "schema_version": "agent-repository-status/v1",
            "provider": self.service_provider,
            "repository_name": self.repository_name,
            "repository_dir": str(self.repository_dir),
            "worktrees_dir": str(self.worktrees_dir),
            "releases_dir": str(self.releases_dir),
            "service_url": self.service_url,
            "service_public_url": self.service_public_url,
            "status": "active",
            "degraded_reason": None,
            "current_commit_sha": None,
            "current_branch": None,
            "dirty": False,
            "changed_file_count": 0,
            "changed_files": [],
            "file_diffs": [],
            "maintenance_active": self._maintenance,
        }
        try:
            self.ensure_bootstrap()
            changes = self._workspace_changes()
            status["current_commit_sha"] = self.current_commit_sha()
            status["current_branch"] = self._git(["branch", "--show-current"], cwd=self.repository_dir).strip() or None
            status["dirty"] = bool(changes)
            status["changed_file_count"] = len(changes)
            status["changed_files"] = changes
            status["file_diffs"] = [self.workspace_file_diff(str(item["path"])) for item in changes[:MAX_REPOSITORY_STATUS_DIFFS]]
        except Exception as exc:
            status["status"] = "degraded"
            status["degraded_reason"] = f"{exc.__class__.__name__}: {exc}"
        return status

    def create_snapshot(
        self,
        *,
        reason: str = "manual_snapshot",
        source_change_set_ids: Optional[list[str]] = None,
        note: Optional[str] = None,
        parent_version_id: Optional[str] = None,
        rollback_of_version_id: Optional[str] = None,
    ) -> JsonObject:
        with self._mutation_guard():
            self._ensure_repo_ready()
            self._git(["add", "-A", "--", "."], cwd=self.repository_dir)
            if self._has_staged_changes(self.repository_dir):
                self._git(["commit", "-m", note or reason], cwd=self.repository_dir)
            commit_sha = self._current_commit_sha_no_bootstrap() or ""
            summary = self.version_summary(commit_sha, reason=reason, note=note, rollback_of_version_id=rollback_of_version_id)
            summary["source_change_set_ids"] = source_change_set_ids or []
            if parent_version_id:
                summary["parent_version_id"] = parent_version_id
            return summary

    def discard_workspace_changes(self, paths: list[str]) -> JsonObject:
        with self._mutation_guard():
            self._ensure_repo_ready()
            current = {str(item["path"]): item for item in self._workspace_changes()}
            requested = self._requested_dirty_paths(paths, current)
            if not requested:
                return self.repository_status()
            tracked_paths = [path for path in requested if not bool(current[path].get("untracked"))]
            if tracked_paths:
                self._git(["restore", "--staged", "--", *tracked_paths], cwd=self.repository_dir, check=False)
                self._git(["restore", "--worktree", "--", *tracked_paths], cwd=self.repository_dir, check=False)
            self._git(["clean", "-fd", "--", *requested], cwd=self.repository_dir, check=False)
            remaining = {str(item["path"]) for item in self._workspace_changes()} & set(requested)
            if remaining:
                raise AgentGitError(f"Failed to discard workspace changes: {', '.join(sorted(remaining))}")
            return self.repository_status()

    def workspace_file_diff(self, path: str) -> JsonObject:
        safe_path = self._safe_relative_path(path)
        if not safe_path:
            return workspace_diff_error(path, "invalid_path", "路径不是合法的 workspace 相对路径。")
        changes = {str(item["path"]): item for item in self._workspace_changes()}
        change = changes.get(safe_path)
        status = str((change or {}).get("status") or "unchanged")
        result: JsonObject = {
            "path": safe_path,
            "status": status,
            "unified_diff": "",
            "is_text": False,
            "truncated": False,
            "reason": None,
        }
        if not change:
            result["reason"] = "文件没有未提交变化。"
            return result
        if bool(change.get("untracked")):
            return untracked_workspace_file_diff(self.repository_dir, safe_path, status)
        diff = self._git(["diff", "--no-ext-diff", "--no-renames", "HEAD", "--", safe_path], cwd=self.repository_dir, check=False)
        if len(diff.encode("utf-8")) > MAX_FILE_DIFF_BYTES:
            result.update({"status": "binary_or_too_large", "truncated": True, "reason": f"diff 超过 {MAX_FILE_DIFF_BYTES} bytes，未展开内容。"})
            return result
        result["is_text"] = True
        result["unified_diff"] = redact_sensitive_diff(diff)
        if not result["unified_diff"]:
            result["reason"] = "文件变化无法生成文本 diff。"
        return result

    def restore_version(self, version_id: str, *, note: Optional[str] = None) -> Optional[JsonObject]:
        target = self.version_summary(version_id, reason="rollback_target")
        pre_restore = self.version_summary(self.current_commit_sha() or "", reason="pre_restore")
        result = self.rollback_to_ref(version_id)
        current = self.version_summary(str(result.get("current_commit_sha") or ""), reason="rollback", note=note)
        return {
            "restored_from_version": target,
            "pre_restore_version": pre_restore,
            "current_version": current,
            "requires_runtime_restart": True,
        }

    def version_summary(
        self,
        commit_sha: str,
        *,
        reason: str = "git_commit",
        note: str | None = None,
        rollback_of_version_id: str | None = None,
    ) -> JsonObject:
        if not commit_sha:
            return {
                "agent_version_id": "",
                "created_at": utc_now(),
                "reason": reason,
                "note": note,
            }
        created_at = self._commit_created_at(commit_sha)
        parent = self._commit_parent(commit_sha)
        return {
            "agent_version_id": commit_sha,
            "commit_sha": commit_sha,
            "parent_version_id": parent,
            "created_at": created_at,
            "reason": reason,
            "rollback_of_version_id": rollback_of_version_id,
            "source_change_set_ids": [],
            "note": note,
            "repository_dir": str(self.repository_dir),
            "file_count": self._tracked_file_count(commit_sha),
        }

    def create_worktree(self, change_set_id: str, *, base_ref: str | None = None) -> GitWorktreeRef:
        with self._mutation_guard():
            self._ensure_repo_ready()
            base_commit = self._resolve_ref(base_ref or "HEAD")
            branch_name = f"change-set/{change_set_id}"
            worktree_path = self.worktrees_dir / change_set_id
            if worktree_path.exists() and (worktree_path / ".git").exists():
                return GitWorktreeRef(change_set_id, branch_name, worktree_path, base_commit)
            if worktree_path.exists():
                shutil.rmtree(worktree_path)
            self._git(["worktree", "prune"], cwd=self.repository_dir, check=False)
            branch_exists = bool(self._git(["show-ref", "--verify", f"refs/heads/{branch_name}"], cwd=self.repository_dir, check=False).strip())
            if branch_exists:
                self._git(["worktree", "add", str(worktree_path), branch_name], cwd=self.repository_dir)
            else:
                self._git(["worktree", "add", "-b", branch_name, str(worktree_path), base_commit], cwd=self.repository_dir)
            self._configure_repo(worktree_path)
            self._write_info_exclude(worktree_path)
            return GitWorktreeRef(change_set_id, branch_name, worktree_path, base_commit)

    def worktree_commit_sha(self, worktree_path: Path) -> str | None:
        """Return a candidate worktree HEAD so interrupted commits can be reconciled."""
        with self._lock:
            safe_path = self._owned_worktree_path(worktree_path)
            if not safe_path.exists() or not (safe_path / ".git").exists():
                return None
            commit = self._git(["rev-parse", "HEAD"], cwd=safe_path, check=False).strip()
            return commit or None

    def reset_worktree(self, worktree_path: Path, *, base_ref: str) -> None:
        """Discard an interrupted, uncommitted automatic apply before its fenced retry."""
        with self._lock:
            safe_path = self._owned_worktree_path(worktree_path)
            if not safe_path.exists() or not (safe_path / ".git").exists():
                raise AgentGitError("Candidate worktree is missing")
            base_commit = self._resolve_ref(base_ref)
            self._git(["reset", "--hard", base_commit], cwd=safe_path)
            self._git(["clean", "-fd"], cwd=safe_path)

    def remove_worktree(self, change_set_id: str, *, delete_branch: bool = True) -> None:
        """Compensate an abandoned automatic change set outside the DB transaction."""
        if not change_set_id or any(part in change_set_id for part in ("/", "\\", "..")):
            raise AgentGitError("Invalid change set id for worktree cleanup")
        with self._lock:
            worktree_path = self._owned_worktree_path(self.worktrees_dir / change_set_id)
            branch_name = f"change-set/{change_set_id}"
            self._git(["worktree", "remove", "--force", str(worktree_path)], cwd=self.repository_dir, check=False)
            if worktree_path.exists():
                shutil.rmtree(worktree_path)
            self._git(["worktree", "prune"], cwd=self.repository_dir, check=False)
            if delete_branch:
                self._git(["branch", "-D", branch_name], cwd=self.repository_dir, check=False)

    def commit_worktree(self, worktree_path: Path, *, message: str) -> str:
        with self._mutation_guard():
            self._configure_repo(worktree_path)
            self._write_info_exclude(worktree_path)
            self._git(["add", "-A", "--", "."], cwd=worktree_path)
            if self._has_staged_changes(worktree_path):
                self._git(["commit", "-m", message], cwd=worktree_path)
            commit = self._git(["rev-parse", "HEAD"], cwd=worktree_path).strip()
            if not commit:
                raise AgentGitError("Candidate worktree has no commit")
            return commit

    def diff_versions(self, from_version_id: str, to_version_id: str) -> Optional[JsonObject]:
        try:
            left = self._resolve_ref(from_version_id)
            right = self._resolve_ref(to_version_id)
            name_status = self._git(["diff", "--name-status", "--no-renames", left, right], cwd=self.repository_dir)
        except AgentGitError:
            return None
        added: list[JsonObject] = []
        modified: list[JsonObject] = []
        deleted: list[JsonObject] = []
        for line in name_status.splitlines():
            if not line.strip():
                continue
            status, _, path = line.partition("\t")
            before = self._file_entry(left, path) if status in {"M", "D"} else None
            after = self._file_entry(right, path) if status in {"M", "A"} else None
            if status == "A" and after:
                added.append(after)
            elif status == "D" and before:
                deleted.append(before)
            elif status == "M":
                modified.append({"path": path, "before": before, "after": after})
        return {
            "from_version_id": left,
            "to_version_id": right,
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "unchanged_count": 0,
        }

    def diff_version_file(self, from_version_id: str, to_version_id: str, path: str) -> Optional[JsonObject]:
        safe_path = self._safe_relative_path(path)
        if not safe_path:
            return None
        try:
            left = self._resolve_ref(from_version_id)
            right = self._resolve_ref(to_version_id)
        except AgentGitError:
            return None
        before = self._read_file_at_ref(left, safe_path)
        after = self._read_file_at_ref(right, safe_path)
        status = self._file_diff_status(before, after)
        result: JsonObject = {
            "from_version_id": left,
            "to_version_id": right,
            "path": safe_path,
            "archive_path": safe_path,
            "status": status,
            "before": self._file_entry(left, safe_path) if before is not None else None,
            "after": self._file_entry(right, safe_path) if after is not None else None,
            "unified_diff": "",
            "is_text": False,
            "truncated": False,
            "reason": None,
        }
        if status in {"missing", "unchanged"}:
            result["reason"] = "文件未变化或未出现在两个版本中。"
            return result
        if len(before or b"") > MAX_FILE_DIFF_BYTES or len(after or b"") > MAX_FILE_DIFF_BYTES:
            result["status"] = "binary_or_too_large"
            result["truncated"] = True
            result["reason"] = f"文件超过 {MAX_FILE_DIFF_BYTES} bytes，未展开内容。"
            return result
        if b"\x00" in (before or b"") or b"\x00" in (after or b""):
            result["status"] = "binary_or_too_large"
            result["reason"] = "文件包含二进制内容，未展开内容。"
            return result
        try:
            before_text = (before or b"").decode("utf-8")
            after_text = (after or b"").decode("utf-8")
        except UnicodeDecodeError:
            result["status"] = "binary_or_too_large"
            result["reason"] = "文件不是 UTF-8 文本，未展开内容。"
            return result
        result["is_text"] = True
        result["unified_diff"] = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"{left}:{safe_path}",
                tofile=f"{right}:{safe_path}",
                lineterm="\n",
            )
        )
        return result

    def publish_commit(
        self,
        commit_sha: str,
        *,
        tag_name: str,
        message: str,
        validate_ref: Callable[[str], None] | None = None,
    ) -> JsonObject:
        with self._mutation_guard():
            self._maintenance = True
            try:
                self._ensure_repo_ready()
                candidate = self._resolve_commit(commit_sha)
                self._validate_tag_name(tag_name)
                current = self.current_commit_sha()
                tag_ref = f"refs/tags/{tag_name}"
                tagged_commit = self._git(["rev-parse", "--verify", f"{tag_ref}^{{commit}}"], cwd=self.repository_dir, check=False).strip()
                if tagged_commit and tagged_commit != candidate:
                    raise AgentGitError(f"Release tag {tag_name!r} already points to a different commit")
                if self._git(["status", "--porcelain"], cwd=self.repository_dir).strip():
                    raise AgentGitError("Main Agent workspace has uncommitted changes")
                if validate_ref is not None:
                    validate_ref(candidate)
                candidate_was_published = (
                    tagged_commit and self._git(["merge-base", candidate, str(current)], cwd=self.repository_dir, check=False).strip() == candidate
                )
                if self.current_commit_sha() != candidate and not candidate_was_published:
                    self._git(["merge", "--ff-only", candidate], cwd=self.repository_dir)
                    if self.current_commit_sha() != candidate:
                        raise AgentGitError("Agent candidate is no longer the active fast-forward target")
                if not tagged_commit:
                    try:
                        self._git(["tag", "-a", tag_name, "-m", message, candidate], cwd=self.repository_dir)
                    except AgentGitError:
                        concurrently_tagged = self._git(
                            ["rev-parse", "--verify", f"{tag_ref}^{{commit}}"],
                            cwd=self.repository_dir,
                            check=False,
                        ).strip()
                        if concurrently_tagged != candidate:
                            raise
                archive = self.archive_ref(tag_name)
                return {
                    "previous_commit_sha": current,
                    "published_commit_sha": candidate,
                    "tag_name": tag_name,
                    "archive": archive,
                    "requires_runtime_restart": True,
                }
            finally:
                self._maintenance = False

    def validate_publication_target(self, commit_sha: str, tag_name: str) -> None:
        with self._lock:
            self._ensure_repo_ready()
            candidate = self._resolve_commit(commit_sha)
            self._validate_tag_name(tag_name)
            tagged_commit = self._git(
                ["rev-parse", "--verify", f"refs/tags/{tag_name}^{{commit}}"],
                cwd=self.repository_dir,
                check=False,
            ).strip()
            if tagged_commit and tagged_commit != candidate:
                raise AgentGitError(f"Release tag {tag_name!r} already points to a different commit")

    def publication_side_effects_present(self, commit_sha: str, tag_name: str) -> bool:
        with self._lock:
            self._ensure_repo_ready()
            candidate = self._resolve_commit(commit_sha)
            tagged_commit = self._git(
                ["rev-parse", "--verify", f"refs/tags/{tag_name}^{{commit}}"],
                cwd=self.repository_dir,
                check=False,
            ).strip()
            current = str(self.current_commit_sha() or "")
            if current == candidate:
                return True
            merge_base = self._git(["merge-base", candidate, current], cwd=self.repository_dir, check=False).strip()
            return tagged_commit == candidate and merge_base == candidate

    def archive_ref(self, ref: str) -> JsonObject:
        resolved = self._resolve_commit(ref)
        ref_digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:16]
        archive_path = self.releases_dir / f"release-{ref_digest}-{resolved[:16]}.tar.gz"
        temporary_path = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self._git(
                ["archive", "--format=tar.gz", "-o", str(temporary_path), resolved],
                cwd=self.repository_dir,
            )
            os.replace(temporary_path, archive_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return {
            "ref": ref,
            "commit_sha": resolved,
            "archive_path": str(archive_path),
            "sha256": self._sha256_file(archive_path),
        }

    def rollback_to_ref(
        self,
        ref: str,
        *,
        expected_current_ref: str | None = None,
        validate_ref: Callable[[str], None] | None = None,
    ) -> JsonObject:
        with self._mutation_guard():
            self._maintenance = True
            try:
                self._ensure_repo_ready()
                target = self._resolve_ref(ref)
                if self._git(["status", "--porcelain"], cwd=self.repository_dir).strip():
                    raise AgentGitError("Main Agent workspace has uncommitted changes")
                if validate_ref is not None:
                    validate_ref(target)
                previous = self.current_commit_sha()
                if expected_current_ref is not None:
                    expected = self._resolve_ref(expected_current_ref)
                    if previous != expected:
                        raise AgentGitError(f"Agent workspace HEAD changed before version maintenance (expected {expected}, found {previous or 'missing'})")
                self._git(["reset", "--hard", target], cwd=self.repository_dir)
                return {
                    "previous_commit_sha": previous,
                    "current_commit_sha": self.current_commit_sha(),
                    "rollback_target_ref": ref,
                    "requires_runtime_restart": True,
                }
            finally:
                self._maintenance = False

    def workspace_changes(self) -> list[JsonObject]:
        with self._mutation_guard():
            self._ensure_repo_ready()
            return list(self._workspace_changes())

    def reset_to_ref_for_managed_migration(self, ref: str) -> None:
        """Recover a journaled platform migration while the runtime phase lock is exclusive."""

        with self._mutation_guard():
            self._ensure_repo_ready()
            target = self._resolve_ref(ref)
            self._git(["reset", "--hard", target], cwd=self.repository_dir)
            self._git(["clean", "-fd"], cwd=self.repository_dir)

    def read_text_at_ref(self, ref: str, path: str) -> str | None:
        safe_path = self._safe_relative_path(path)
        if not safe_path:
            raise AgentGitError(f"Invalid workspace path: {path!r}")
        raw = self._read_file_at_ref(self._resolve_ref(ref), safe_path)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentGitError(f"Workspace file is not UTF-8: {safe_path}") from exc

    @contextmanager
    def mutation_guard(self) -> Iterator[None]:
        """Hold this Agent repository's in-process and cross-process mutation lease."""

        with self._mutation_guard():
            yield

    @contextmanager
    def _mutation_guard(self) -> Iterator[None]:
        with self._lock:
            with advisory_lock(self._process_lock_path, mode="exclusive"):
                yield

    def _workspace_changes(self) -> list[JsonObject]:
        raw = self._git(["status", "--porcelain=v1", "--untracked-files=all", "--no-renames"], cwd=self.repository_dir)
        return parse_workspace_changes(raw, normalize_path=self._safe_relative_path)

    def _requested_dirty_paths(self, paths: list[str], current: dict[str, JsonObject]) -> list[str]:
        requested: list[str] = []
        for path in paths:
            safe_path = self._safe_relative_path(path)
            if not safe_path:
                raise AgentGitError(f"Invalid workspace path: {path}")
            if safe_path not in current:
                raise AgentGitError(f"Workspace path has no uncommitted changes: {safe_path}")
            if safe_path not in requested:
                requested.append(safe_path)
        return requested

    def _ensure_repo_ready(self) -> None:
        self.ensure_bootstrap()

    def _ensure_git_available(self) -> None:
        if shutil.which("git") is None:
            raise AgentGitError("git executable is not available")

    def _git(self, args: list[str], *, cwd: Path, check: bool = True) -> str:
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env,
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
        self._git(["config", "user.name", self.git_user_name], cwd=cwd)
        self._git(["config", "user.email", self.git_user_email], cwd=cwd)

    def _ensure_safe_directory(self, cwd: Path) -> None:
        safe_path = str(cwd.resolve())
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return
        diagnostic = f"{probe.stdout}\n{probe.stderr}".lower()
        if "dubious ownership" not in diagnostic and "safe.directory" not in diagnostic:
            # This helper only owns safe.directory recovery.  A missing or otherwise
            # invalid repository is reported by the following repository-local Git
            # command with its normal, more precise error; it must not grow the global
            # safe.directory list.
            return
        existing = self._git(["config", "--global", "--get-all", "safe.directory"], cwd=cwd, check=False)
        if safe_path in existing.splitlines() or "*" in existing.splitlines():
            return
        self._git(["config", "--global", "--add", "safe.directory", safe_path], cwd=cwd, check=False)

    def _write_info_exclude(self, cwd: Path) -> None:
        git_dir = self._git(["rev-parse", "--git-dir"], cwd=cwd).strip()
        exclude_path = (cwd / git_dir / "info" / "exclude").resolve()
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        lines = ["# Agent runtime managed excludes"]
        lines.extend(sorted(WORKSPACE_EXCLUDED_NAMES))
        lines.extend(WORKSPACE_EXCLUDED_PATTERNS)
        addition = "\n".join(lines) + "\n"
        if "Agent runtime managed excludes" not in existing:
            exclude_path.write_text(existing.rstrip() + "\n" + addition if existing else addition, encoding="utf-8")

    def _has_head(self, cwd: Path) -> bool:
        return bool(self._git(["rev-parse", "--verify", "HEAD"], cwd=cwd, check=False).strip())

    def _has_staged_changes(self, cwd: Path) -> bool:
        proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(cwd), check=False)
        return proc.returncode == 1

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

    def _commit_parent(self, commit_sha: str) -> Optional[str]:
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
        return {
            "path": path,
            "type": "file",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }

    def _read_file_at_ref(self, ref: str, path: str) -> bytes | None:
        safe_path = self._safe_relative_path(path)
        if not safe_path:
            return None
        proc = subprocess.run(
            ["git", "show", f"{ref}:{safe_path}"],
            cwd=str(self.repository_dir),
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout

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
        resolved = path.expanduser().resolve()
        worktrees_root = self.worktrees_dir.expanduser().resolve()
        if resolved.parent != worktrees_root:
            raise AgentGitError("Candidate worktree path escapes the governed worktree root")
        return resolved

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
