from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from app.runtime.agent_git_command_mixin import AgentGitCommandMixin
from app.runtime.agent_git_environment import GovernedGitEnvironmentError, require_governed_repository
from app.runtime.agent_git_errors import AgentGitError, AgentGitInitializationConflict
from app.runtime.agent_git_workspace_diff import (
    MAX_FILE_DIFF_BYTES,
    parse_workspace_changes,
    redact_sensitive_diff,
    untracked_workspace_file_diff,
    workspace_diff_error,
)
from app.runtime.agent_repository_guard import AgentRepositoryGuardError, AgentRepositoryMutationGuard
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import utc_now

MAX_REPOSITORY_STATUS_DIFFS = 20


class AgentVersionProvider(Protocol):
    def ensure_bootstrap(self) -> JsonObject: ...

    def current_version_id(self) -> Optional[str]: ...

    def is_maintenance_active(self) -> bool: ...


@dataclass(frozen=True)
class GitWorktreeRef:
    change_set_id: str
    branch_name: str
    worktree_path: Path
    base_commit_sha: str


def _worktree_registration_matches(raw: str, *, worktree_path: Path, head: str, branch: str) -> bool:
    expected = {f"worktree {worktree_path}", f"HEAD {head}", f"branch {branch}"}
    return any(expected.issubset(set(record.splitlines())) for record in raw.strip().split("\n\n"))


def _canonical_nofollow_path(path: Path) -> Path:
    lexical = Path(os.path.abspath(path.expanduser()))
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AgentGitError("Candidate worktree authority rejected") from exc
    if lexical != resolved:
        raise AgentGitError("Candidate worktree authority rejected")
    return lexical


def _read_canonical_pointer(path: Path, *, base: Path, prefix: str = "") -> Path:
    try:
        metadata = path.lstat()
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AgentGitError("Candidate worktree authority rejected") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or len(lines) != 1:
        raise AgentGitError("Candidate worktree authority rejected")
    value = lines[0]
    if prefix and not value.startswith(prefix):
        raise AgentGitError("Candidate worktree authority rejected")
    target = Path(value.removeprefix(prefix))
    return _canonical_nofollow_path(target if target.is_absolute() else base / target)


class GitAgentVersionStore(AgentGitCommandMixin):
    """Git-backed per-Agent version provider with stable mutation authority."""

    def __init__(
        self,
        *,
        repository_dir: Path,
        worktrees_dir: Path,
        releases_dir: Path,
        service_provider: str = "local",
        service_url: str | None = None,
        service_public_url: str | None = None,
        repository_name: str = "business-agent-config",
        git_user_name: str = "AgentGov",
        git_user_email: str = "agent-runtime@example.local",
        process_lock_path: Path | None = None,
        mutation_precondition: Callable[[], bool] | None = None,
        activation_precondition: Callable[[], bool] | None = None,
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
        self._activation_precondition = activation_precondition or mutation_precondition
        self._mutation = AgentRepositoryMutationGuard(
            lock_path=process_lock_path or self.worktrees_dir.parent / ".repository.lock",
            repository_dir=self.repository_dir,
            worktrees_dir=self.worktrees_dir,
            releases_dir=self.releases_dir,
            thread_lock=self._lock,
            precondition=mutation_precondition,
        )

    def is_maintenance_active(self) -> bool:
        return self._maintenance

    def ensure_bootstrap(self) -> JsonObject:
        with self.initialization_guard():
            self._ensure_git_available()
            if not (self.repository_dir / ".git").exists():
                self._git(["init"], cwd=self.repository_dir)
            self._configure_repo(self.repository_dir)
            self._write_info_exclude(self.repository_dir)
            if not self._has_head(self.repository_dir):
                self._stage_complete_workspace(self.repository_dir)
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

    def resolve_commit_sha(self, ref: str) -> str:
        """Resolve one ref to a commit owned by this Agent repository."""

        self._ensure_repo_ready()
        return self._resolve_commit(ref)

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
            self._ensure_repo_ready()
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
            self._stage_complete_workspace(self.repository_dir)
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
            self._git(["clean", "-fdx", "--", *requested], cwd=self.repository_dir, check=False)
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
        diff = self._git(
            ["diff", "--no-ext-diff", "--no-renames", "HEAD", "--", safe_path],
            cwd=self.repository_dir,
            check=False,
            optional_locks=False,
        )
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
        if not change_set_id or any(part in change_set_id for part in ("/", "\\", "..")):
            raise AgentGitError("Invalid change set id for worktree creation")
        with self._mutation_guard():
            self._ensure_repo_ready()
            base_commit = self._resolve_ref(base_ref or "HEAD")
            branch_name = f"change-set/{change_set_id}"
            worktree_path = self._owned_worktree_path(self.worktrees_dir / change_set_id)
            if worktree_path.exists() and (worktree_path / ".git").exists():
                return self._require_existing_worktree_authority(
                    change_set_id,
                    worktree_path,
                    expected_head=base_commit,
                )
            if worktree_path.exists():
                raise AgentGitError("Candidate worktree path already exists without linked Git authority")
            self._git(["worktree", "prune"], cwd=self.repository_dir, check=False)
            branch_exists = bool(self._git(["show-ref", "--verify", f"refs/heads/{branch_name}"], cwd=self.repository_dir, check=False).strip())
            if branch_exists:
                if self._resolve_ref(branch_name) != base_commit:
                    raise AgentGitError("Candidate worktree branch conflicts with its requested base commit")
                self._git(["worktree", "add", str(worktree_path), branch_name], cwd=self.repository_dir)
            else:
                self._git(["worktree", "add", "-b", branch_name, str(worktree_path), base_commit], cwd=self.repository_dir)
            self._configure_repo(worktree_path)
            self._write_info_exclude(worktree_path)
            return self._require_existing_worktree_authority(
                change_set_id,
                worktree_path,
                expected_head=base_commit,
            )

    def worktree_commit_sha(self, worktree_path: Path) -> str | None:
        """Return a candidate worktree HEAD so interrupted commits can be reconciled."""
        with self._mutation_guard():
            safe_path = self._owned_worktree_path(worktree_path)
            if not safe_path.exists() or not (safe_path / ".git").exists():
                return None
            authority = self._require_existing_worktree_authority(
                safe_path.name,
                safe_path,
                expected_head=None,
            )
            return authority.base_commit_sha

    def _require_existing_worktree_authority(
        self,
        change_set_id: str,
        worktree_path: Path,
        *,
        expected_head: str | None,
    ) -> GitWorktreeRef:
        """Require one exact linked worktree owned by this repository and change set."""

        if not change_set_id or any(part in change_set_id for part in ("/", "\\", "..")) or expected_head == "":
            raise AgentGitError("Candidate worktree authority rejected")
        with self._mutation_guard():
            repository_path = _canonical_nofollow_path(self.repository_dir)
            worktrees_path = _canonical_nofollow_path(self.worktrees_dir)
            expected_path = _canonical_nofollow_path(worktrees_path / change_set_id)
            safe_path = _canonical_nofollow_path(worktree_path)
            if safe_path != expected_path:
                raise AgentGitError("Candidate worktree authority rejected")
            try:
                main_scope = require_governed_repository(self.repository_dir)
                linked_scope = require_governed_repository(safe_path)
                main_common = main_scope.common_git_dir.resolve(strict=True)
                linked_common = linked_scope.common_git_dir.resolve(strict=True)
                linked_git_dir = linked_scope.git_dir.resolve(strict=True)
            except (GovernedGitEnvironmentError, OSError) as exc:
                raise AgentGitError("Candidate worktree authority rejected") from exc
            pointer_git_dir = _read_canonical_pointer(safe_path / ".git", base=safe_path, prefix="gitdir: ")
            pointer_common = _read_canonical_pointer(linked_git_dir / "commondir", base=linked_git_dir)
            if (
                main_common != repository_path / ".git"
                or linked_git_dir != pointer_git_dir
                or linked_common != pointer_common
                or linked_git_dir == linked_common
                or linked_common != main_common
                or linked_git_dir.parent != main_common / "worktrees"
            ):
                raise AgentGitError("Candidate worktree authority rejected")
            expected_branch = f"refs/heads/change-set/{change_set_id}"
            branch = self._git(["symbolic-ref", "-q", "HEAD"], cwd=safe_path, check=False).strip()
            head = self._git(["rev-parse", "--verify", "HEAD"], cwd=safe_path, check=False).strip()
            registrations = self._git(["worktree", "list", "--porcelain"], cwd=self.repository_dir)
            try:
                post_scope = require_governed_repository(safe_path)
            except GovernedGitEnvironmentError as exc:
                raise AgentGitError("Candidate worktree authority rejected") from exc
            if (
                post_scope != linked_scope
                or branch != expected_branch
                or (expected_head is not None and head != expected_head)
                or not _worktree_registration_matches(
                    registrations,
                    worktree_path=safe_path,
                    head=head,
                    branch=expected_branch,
                )
            ):
                raise AgentGitError("Candidate worktree authority rejected")
            return GitWorktreeRef(change_set_id, expected_branch.removeprefix("refs/heads/"), safe_path, head)

    def reset_worktree(self, worktree_path: Path, *, base_ref: str) -> None:
        """Discard an interrupted, uncommitted automatic apply before its fenced retry."""
        with self._mutation_guard():
            safe_path = self._owned_worktree_path(worktree_path)
            if not safe_path.exists() or not (safe_path / ".git").exists():
                raise AgentGitError("Candidate worktree is missing")
            authority = self._require_existing_worktree_authority(
                safe_path.name,
                safe_path,
                expected_head=None,
            )
            base_commit = self._resolve_ref(base_ref)
            self._git(["reset", "--hard", base_commit], cwd=authority.worktree_path)
            self._git(["clean", "-fd"], cwd=authority.worktree_path)

    def remove_worktree(self, change_set_id: str, *, delete_branch: bool = True) -> None:
        """Compensate an abandoned automatic change set outside the DB transaction."""
        if not change_set_id or any(part in change_set_id for part in ("/", "\\", "..")):
            raise AgentGitError("Invalid change set id for worktree cleanup")
        with self._mutation_guard():
            worktree_path = self._owned_worktree_path(self.worktrees_dir / change_set_id)
            branch_name = f"change-set/{change_set_id}"
            if worktree_path.exists():
                self._require_existing_worktree_authority(
                    change_set_id,
                    worktree_path,
                    expected_head=None,
                )
            self._git(["worktree", "remove", "--force", str(worktree_path)], cwd=self.repository_dir, check=False)
            if worktree_path.exists():
                shutil.rmtree(worktree_path)
            self._git(["worktree", "prune"], cwd=self.repository_dir, check=False)
            if delete_branch:
                self._git(["branch", "-D", branch_name], cwd=self.repository_dir, check=False)

    def commit_worktree(self, worktree_path: Path, *, message: str) -> str:
        with self._mutation_guard():
            safe_path = self._owned_worktree_path(worktree_path)
            authority = self._require_existing_worktree_authority(
                safe_path.name,
                safe_path,
                expected_head=None,
            )
            self._configure_repo(authority.worktree_path)
            self._write_info_exclude(authority.worktree_path)
            self._stage_complete_workspace(authority.worktree_path)
            if self._has_staged_changes(authority.worktree_path):
                self._git(["commit", "-m", message], cwd=authority.worktree_path)
            commit = self._git(["rev-parse", "HEAD"], cwd=authority.worktree_path).strip()
            if not commit:
                raise AgentGitError("Candidate worktree has no commit")
            return commit

    def commit_squashed_worktree(self, worktree_path: Path, *, base_ref: str, message: str) -> str:
        from app.runtime.agent_git_worktree_operations import commit_squashed_worktree

        return commit_squashed_worktree(self, worktree_path, base_ref=base_ref, message=message)

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
                    raise AgentGitError("Business Agent Workspace has uncommitted changes")
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
        with self._mutation_guard():
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
                    raise AgentGitError("Business Agent Workspace has uncommitted changes")
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
    def workspace_activation_guard(self) -> Iterator[None]:
        """Hold the stable lock under exact activation/recovery authority."""

        try:
            with self._mutation.activation(precondition=self._activation_precondition):
                yield
        except AgentRepositoryGuardError as exc:
            raise AgentGitError(str(exc)) from exc

    @contextmanager
    def _mutation_guard(self) -> Iterator[None]:
        try:
            with self._mutation.existing():
                yield
        except AgentRepositoryGuardError as exc:
            raise AgentGitError(str(exc)) from exc

    @contextmanager
    def initialization_guard(self, *, require_new_repository: bool = False) -> Iterator[None]:
        """Create the repository layout under an explicit lifecycle authority."""
        try:
            with self._mutation.initialization(require_new_repository=require_new_repository):
                yield
        except AgentRepositoryGuardError as exc:
            raise AgentGitInitializationConflict(str(exc)) from exc

    def _workspace_changes(self) -> list[JsonObject]:
        raw = self._git(
            ["status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "--ignored"],
            cwd=self.repository_dir,
            optional_locks=False,
        )
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
        try:
            self._mutation.validate_existing()
        except AgentRepositoryGuardError as exc:
            raise AgentGitError(str(exc)) from exc
