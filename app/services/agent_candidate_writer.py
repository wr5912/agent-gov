from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_worktree_inspection import inspect_clean_worktree
from app.runtime.business_agent_workspace import WorkspaceProvisionEntry
from app.runtime.execution_targets import MAX_EXECUTION_TARGET_CONTEXT_BYTES, WorkspaceExecutionTargetPolicy
from app.runtime.json_types import JsonObject
from app.services.agent_workspace_git_operations import clear_worktree, write_entries
from app.services.agent_workspace_package_codec import (
    MAX_EXTRACTED_PACKAGE_BYTES,
    MAX_PACKAGE_MEMBERS,
    MAX_SINGLE_MEMBER_BYTES,
    WorkspacePackageError,
    validate_commit_path,
    validate_workspace_config_entries,
)

_EDITABLE_TEXT_PATHS = {"agent.yaml", "AGENT.md"}
_EDITABLE_STATUSES = {"draft", "execution_ready", "candidate_committed", "pending_approval", "approved"}


class _Governance(Protocol):
    version_maintenance: Any

    def get_change_set(self, change_set_id: str) -> JsonObject | None: ...

    def _store_for(self, agent_id: str | None) -> GitAgentVersionStore: ...

    def mark_candidate_committed(
        self,
        change_set_id: str,
        *,
        candidate_commit_sha: str,
        execution_job_id: str | None,
        note: str | None = None,
        operator: str = "runtime",
    ) -> JsonObject: ...


class AgentCandidateWriteError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AgentCandidateWriter:
    """唯一候选文件写入命令；所有调用方落同一 change-set commit 链。"""

    def __init__(self, governance: _Governance) -> None:
        self._governance = governance

    def read_text_file(self, *, change_set_id: str, path: str) -> JsonObject:
        change_set, store, worktree = self._resolve(change_set_id)
        safe_path = self._editable_text_path(path)
        try:
            head, dirty = inspect_clean_worktree(store, worktree)
        except AgentGitError as exc:
            raise AgentCandidateWriteError(409, str(exc)) from exc
        if dirty:
            raise AgentCandidateWriteError(409, "Candidate worktree has an incomplete write; retry the candidate update command")
        target = self._safe_target(worktree, safe_path)
        content, digest, size = self._read_utf8(target)
        return {
            "agent_id": str(change_set["agent_id"]),
            "change_set_id": change_set_id,
            "change_set_status": str(change_set["status"]),
            "base_commit_sha": str(change_set["base_commit_sha"]),
            "candidate_commit_sha": head,
            "path": safe_path.as_posix(),
            "exists": digest is not None,
            "content": content,
            "sha256": digest,
            "size_bytes": size,
            "content_type": self._content_type(safe_path),
            "published": False,
        }

    def write_text_files(
        self,
        *,
        change_set_id: str,
        files: Sequence[tuple[str, str, str | None, int]],
        expected_candidate_commit_sha: str,
        operator: str,
        note: str | None,
    ) -> JsonObject:
        entries: list[WorkspaceProvisionEntry] = []
        expected_files: dict[str, str] = {}
        for path, content, expected_sha256, mode in files:
            safe_path = self._editable_text_path(path)
            data = content.encode("utf-8")
            if len(data) > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
                raise AgentCandidateWriteError(413, f"Candidate file content is too large: {path}")
            self._validate_inline_content(safe_path, content)
            entries.append(WorkspaceProvisionEntry(safe_path, data, mode))
            if expected_sha256 is not None:
                expected_files[safe_path.as_posix()] = expected_sha256
        return self.write_entries(
            change_set_id=change_set_id,
            entries=tuple(entries),
            expected_candidate_commit_sha=expected_candidate_commit_sha,
            expected_file_sha256=expected_files,
            replace_tree=False,
            operator=operator,
            note=note,
        )

    def write_entries(
        self,
        *,
        change_set_id: str,
        entries: tuple[WorkspaceProvisionEntry, ...],
        expected_candidate_commit_sha: str,
        expected_file_sha256: Mapping[str, str] | None = None,
        replace_tree: bool,
        operator: str,
        note: str | None = None,
    ) -> JsonObject:
        """批量 patch 或替换完整树，成功时只产生一个候选 commit。"""

        change_set, store, worktree = self._resolve(change_set_id)
        agent_id = str(change_set["agent_id"])
        try:
            with self._governance.version_maintenance.lease(
                agent_id=agent_id,
                kind="candidate_write",
                owner_id=f"{operator}:{change_set_id}",
            ) as lease:
                result = self._write_locked(
                    change_set=change_set,
                    store=store,
                    worktree=worktree,
                    entries=entries,
                    expected_candidate_commit_sha=expected_candidate_commit_sha,
                    expected_file_sha256=expected_file_sha256 or {},
                    replace_tree=replace_tree,
                    operator=operator,
                    note=note,
                    assert_active=lease.assert_active,
                )
                lease.check()
                return result
        except AgentAdmissionError as exc:
            raise AgentCandidateWriteError(409, str(exc)) from exc

    def _write_locked(
        self,
        *,
        change_set: JsonObject,
        store: GitAgentVersionStore,
        worktree: Path,
        entries: tuple[WorkspaceProvisionEntry, ...],
        expected_candidate_commit_sha: str,
        expected_file_sha256: Mapping[str, str],
        replace_tree: bool,
        operator: str,
        note: str | None,
        assert_active: Any,
    ) -> JsonObject:
        normalized = self._validate_entries(entries, replace_tree=replace_tree)
        bound_commit = str(change_set.get("candidate_commit_sha") or change_set["base_commit_sha"])
        head, dirty = self._inspect_or_error(store, worktree)
        if dirty:
            if head != bound_commit:
                raise AgentCandidateWriteError(409, "Candidate worktree diverged during an incomplete write")
            store.reset_worktree(worktree, base_ref=bound_commit)
            head, dirty = self._inspect_or_error(store, worktree)
        if dirty:
            raise AgentCandidateWriteError(409, "Candidate worktree recovery did not restore a clean tree")
        if head != bound_commit:
            if not self._entries_match(worktree, normalized, replace_tree=replace_tree):
                raise AgentCandidateWriteError(409, "Candidate commit advanced with different content; reload before writing")
            return self._record_candidate(
                change_set,
                head,
                normalized,
                operator=operator,
                note=note,
            )
        if expected_candidate_commit_sha != bound_commit:
            if self._entries_match(worktree, normalized, replace_tree=replace_tree):
                return self._response(change_set, bound_commit, normalized)
            raise AgentCandidateWriteError(409, "Candidate commit changed; reload before writing")
        self._validate_file_cas(worktree, expected_file_sha256)
        assert_active()
        try:
            with store.mutation_guard():
                locked_head, locked_dirty = inspect_clean_worktree(store, worktree)
                if locked_head != bound_commit or locked_dirty:
                    raise AgentCandidateWriteError(409, "Candidate worktree changed before commit")
                self._apply_entries(worktree, normalized, replace_tree=replace_tree)
                candidate = store.commit_worktree(
                    worktree,
                    message=note or f"Update candidate {change_set['change_set_id']}",
                )
        except (AgentGitError, OSError) as exc:
            try:
                store.reset_worktree(worktree, base_ref=bound_commit)
            except AgentGitError as rollback_error:
                raise AgentCandidateWriteError(409, "Candidate write failed and worktree recovery is pending") from rollback_error
            raise AgentCandidateWriteError(409, f"Candidate write failed: {exc}") from exc
        assert_active()
        return self._record_candidate(
            change_set,
            candidate,
            normalized,
            operator=operator,
            note=note,
        )

    def _record_candidate(
        self,
        change_set: JsonObject,
        candidate: str,
        entries: tuple[WorkspaceProvisionEntry, ...],
        *,
        operator: str,
        note: str | None,
    ) -> JsonObject:
        updated = self._governance.mark_candidate_committed(
            str(change_set["change_set_id"]),
            candidate_commit_sha=candidate,
            execution_job_id=(str(change_set["execution_job_id"]) if change_set.get("execution_job_id") else None),
            note=note,
            operator=operator,
        )
        return self._response(updated, candidate, entries)

    def _resolve(self, change_set_id: str) -> tuple[JsonObject, GitAgentVersionStore, Path]:
        change_set = self._governance.get_change_set(change_set_id)
        if change_set is None:
            raise AgentCandidateWriteError(404, "Agent change set not found")
        if str(change_set.get("status") or "") not in _EDITABLE_STATUSES:
            raise AgentCandidateWriteError(409, "Agent change set is not editable")
        store = self._governance._store_for(str(change_set.get("agent_id") or ""))
        worktree = Path(str(change_set.get("worktree_path") or ""))
        return change_set, store, worktree

    def _validate_entries(
        self,
        entries: tuple[WorkspaceProvisionEntry, ...],
        *,
        replace_tree: bool,
    ) -> tuple[WorkspaceProvisionEntry, ...]:
        if not entries:
            raise AgentCandidateWriteError(422, "Candidate update must contain at least one file")
        if len(entries) > MAX_PACKAGE_MEMBERS:
            raise AgentCandidateWriteError(413, "Candidate update contains too many files")
        total = 0
        normalized: list[WorkspaceProvisionEntry] = []
        seen: set[str] = set()
        try:
            for entry in entries:
                path = validate_commit_path(entry.relative_path.as_posix().encode("utf-8"))
                name = path.as_posix()
                if name in seen:
                    raise AgentCandidateWriteError(422, f"Candidate update contains a duplicate path: {name}")
                if entry.mode not in {0o644, 0o755}:
                    raise AgentCandidateWriteError(422, f"Candidate file mode is unsupported: {name}")
                if len(entry.content) > MAX_SINGLE_MEMBER_BYTES:
                    raise AgentCandidateWriteError(413, f"Candidate file is too large: {name}")
                total += len(entry.content)
                seen.add(name)
                normalized.append(WorkspaceProvisionEntry(path, entry.content, entry.mode))
        except WorkspacePackageError as exc:
            raise AgentCandidateWriteError(exc.status_code, str(exc)) from exc
        if total > MAX_EXTRACTED_PACKAGE_BYTES:
            raise AgentCandidateWriteError(413, "Candidate update is too large")
        result = tuple(sorted(normalized, key=lambda entry: entry.relative_path.as_posix()))
        if replace_tree and not {"agent.yaml", "AGENT.md"}.issubset(seen):
            raise AgentCandidateWriteError(422, "A complete candidate tree must contain agent.yaml and AGENT.md")
        try:
            validate_workspace_config_entries(result)
        except WorkspacePackageError as exc:
            raise AgentCandidateWriteError(exc.status_code, str(exc)) from exc
        return result

    @staticmethod
    def _apply_entries(worktree: Path, entries: tuple[WorkspaceProvisionEntry, ...], *, replace_tree: bool) -> None:
        _reject_symlinks(worktree)
        if replace_tree:
            clear_worktree(worktree)
        write_entries(worktree, entries)

    @staticmethod
    def _inspect_or_error(store: GitAgentVersionStore, worktree: Path) -> tuple[str, bool]:
        try:
            return inspect_clean_worktree(store, worktree)
        except AgentGitError as exc:
            raise AgentCandidateWriteError(409, str(exc)) from exc

    @staticmethod
    def _validate_file_cas(worktree: Path, expected: Mapping[str, str]) -> None:
        for path, digest in expected.items():
            safe = validate_commit_path(path.encode("utf-8"))
            target = AgentCandidateWriter._safe_target(worktree, safe)
            actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() and not target.is_symlink() else None
            if actual != digest:
                raise AgentCandidateWriteError(409, f"Candidate file changed; reload before writing: {path}")

    @staticmethod
    def _entries_match(worktree: Path, entries: tuple[WorkspaceProvisionEntry, ...], *, replace_tree: bool) -> bool:
        if replace_tree:
            try:
                current = _workspace_entries(worktree)
            except (AgentCandidateWriteError, OSError):
                return False
            return current == entries
        return all(
            (target := AgentCandidateWriter._safe_target(worktree, entry.relative_path)).is_file()
            and not target.is_symlink()
            and target.read_bytes() == entry.content
            and stat.S_IMODE(target.stat().st_mode) == entry.mode
            for entry in entries
        )

    @staticmethod
    def _response(change_set: JsonObject, candidate: str, entries: tuple[WorkspaceProvisionEntry, ...]) -> JsonObject:
        return {
            "agent_id": str(change_set["agent_id"]),
            "change_set_id": str(change_set["change_set_id"]),
            "change_set_status": str(change_set["status"]),
            "base_commit_sha": str(change_set["base_commit_sha"]),
            "candidate_commit_sha": candidate,
            "changed_paths": [entry.relative_path.as_posix() for entry in entries],
            "published": False,
        }

    @staticmethod
    def _editable_text_path(path: str) -> PurePosixPath:
        try:
            safe = validate_commit_path(path.encode("utf-8"))
        except WorkspacePackageError as exc:
            raise AgentCandidateWriteError(exc.status_code, str(exc)) from exc
        name = safe.as_posix()
        is_mcp = len(safe.parts) == 2 and safe.parts[0] == "mcp" and safe.suffix == ".json"
        if name not in _EDITABLE_TEXT_PATHS and not is_mcp:
            raise AgentCandidateWriteError(422, "Only agent.yaml, AGENT.md, and mcp/<name>.json are editable here")
        return safe

    @staticmethod
    def _safe_target(worktree: Path, path: PurePosixPath) -> Path:
        policy = WorkspaceExecutionTargetPolicy(worktree)
        relative = policy.relative_path(path.as_posix())
        if relative is None or policy.rel_excluded(relative):
            raise AgentCandidateWriteError(403, "unsafe_target_path")
        target = policy.target_path(path.as_posix())
        if target is None:
            raise AgentCandidateWriteError(403, "unsafe_target_path")
        return target

    @staticmethod
    def _read_utf8(target: Path) -> tuple[str, str | None, int]:
        if not target.exists():
            return "", None, 0
        if target.is_symlink() or not target.is_file():
            raise AgentCandidateWriteError(409, "Candidate config path is not a regular file")
        data = target.read_bytes()
        if len(data) > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
            raise AgentCandidateWriteError(413, "Candidate config file is too large to edit inline")
        try:
            return data.decode("utf-8"), hashlib.sha256(data).hexdigest(), len(data)
        except UnicodeDecodeError as exc:
            raise AgentCandidateWriteError(415, "Candidate config file is not UTF-8 text") from exc

    @staticmethod
    def _validate_inline_content(path: PurePosixPath, content: str) -> None:
        try:
            parsed = yaml.safe_load(content) if path.as_posix() == "agent.yaml" else json.loads(content) if path.parts[0] == "mcp" else None
        except (yaml.YAMLError, json.JSONDecodeError) as exc:
            raise AgentCandidateWriteError(422, f"Invalid config content: {path}") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise AgentCandidateWriteError(422, f"Candidate config must contain an object: {path}")
        if path.parts[0] == "mcp" and isinstance(parsed, dict) and any(key in parsed for key in ("command", "args")):
            raise AgentCandidateWriteError(422, "Process-spawning MCP configuration is forbidden")

    @staticmethod
    def _content_type(path: PurePosixPath) -> str:
        if path.parts[0] == "mcp":
            return "application/json"
        return "application/yaml" if path.as_posix() == "agent.yaml" else "text/markdown"


def _reject_symlinks(worktree: Path) -> None:
    if worktree.is_symlink() or not worktree.is_dir():
        raise AgentCandidateWriteError(409, "Candidate worktree is not a safe directory")
    for path in worktree.rglob("*"):
        if path.name == ".git" and path.parent == worktree:
            continue
        if path.is_symlink():
            raise AgentCandidateWriteError(409, "Candidate worktree contains a symlink")


def _workspace_entries(worktree: Path) -> tuple[WorkspaceProvisionEntry, ...]:
    _reject_symlinks(worktree)
    entries: list[WorkspaceProvisionEntry] = []
    for path in sorted(worktree.rglob("*")):
        if path == worktree / ".git" or (worktree / ".git") in path.parents or path.is_dir():
            continue
        relative = PurePosixPath(path.relative_to(worktree).as_posix())
        entries.append(
            WorkspaceProvisionEntry(
                relative_path=relative,
                content=path.read_bytes(),
                mode=0o755 if path.stat().st_mode & 0o111 else 0o644,
            )
        )
    return tuple(entries)
