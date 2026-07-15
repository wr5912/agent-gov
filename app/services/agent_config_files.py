from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.bootstrap_runtime_volume import load_runtime_env

from app.runtime.advisory_lock import advisory_lock
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.config_file_schemas import (
    AgentConfigFileResponse,
    AgentConfigFileUpdateRequest,
    AgentConfigFileUpdateResponse,
)
from app.runtime.errors import SessionConflictError
from app.runtime.execution_targets import MAX_EXECUTION_TARGET_CONTEXT_BYTES, WorkspaceExecutionTargetPolicy
from app.runtime.managed_agent_policy import validate_managed_mcp_content
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore

EDITABLE_AGENT_CONFIG_FILES = {".mcp.json": "application/json"}
_TEMP_FILE_ATTEMPTS = 16


@dataclass(frozen=True)
class _ConfigSnapshot:
    data: bytes | None
    mode: int = 0o600

    @property
    def exists(self) -> bool:
        return self.data is not None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.data).hexdigest() if self.data is not None else None


class _AtomicReplaceFailure(Exception):
    def __init__(self, cause: OSError, *, replaced: bool) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.replaced = replaced


class AgentConfigFileError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AgentConfigFileService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        agent_registry_store: AgentRegistryStore,
        session_store: LocalSessionStore,
    ) -> None:
        self._settings = settings
        self._agent_registry_store = agent_registry_store
        self._session_store = session_store

    def read_file(self, *, agent_id: str, path: str) -> AgentConfigFileResponse:
        safe_agent_id, target = self._resolve_target(agent_id=agent_id, path=path)
        with self._locked_parent(target, exclusive=False) as directory_fd:
            snapshot = self._read_snapshot(directory_fd=directory_fd, target_name=target.name)
        content = self._decode_snapshot(snapshot)
        return AgentConfigFileResponse(
            agent_id=safe_agent_id,
            path=path,
            container_path=str(target),
            exists=snapshot.exists,
            content=content,
            sha256=snapshot.sha256,
            size_bytes=len(snapshot.data or b""),
            content_type=EDITABLE_AGENT_CONFIG_FILES[path],
        )

    def update_file(
        self,
        *,
        agent_id: str,
        path: str,
        request: AgentConfigFileUpdateRequest,
    ) -> AgentConfigFileUpdateResponse:
        safe_agent_id, target = self._resolve_target(agent_id=agent_id, path=path)
        self._validate_content(agent_id=safe_agent_id, path=path, content=request.content)
        replacement_data = request.content.encode("utf-8")
        lock_path = business_agent_layout(self._settings.data_dir, safe_agent_id).version_base / ".repository.lock"
        with advisory_lock(lock_path, mode="exclusive"):
            with self._locked_parent(target, exclusive=True) as directory_fd:
                original = self._read_snapshot(directory_fd=directory_fd, target_name=target.name)
                if request.expected_sha256 is not None and request.expected_sha256 != original.sha256:
                    raise AgentConfigFileError(409, "Config file changed; reload before applying edits")
                try:
                    self._atomic_replace(
                        directory_fd=directory_fd,
                        target_name=target.name,
                        data=replacement_data,
                        mode=original.mode,
                    )
                except _AtomicReplaceFailure as exc:
                    if exc.replaced:
                        self._rollback_replacement(
                            directory_fd=directory_fd,
                            target_name=target.name,
                            original=original,
                            replacement_data=replacement_data,
                            operation="config update",
                        )
                    raise AgentConfigFileError(409, f"Config file update failed: {exc.cause.__class__.__name__}") from exc
                try:
                    invalidated = self._invalidate_session(agent_id=safe_agent_id, session_id=request.session_id)
                except Exception:
                    self._rollback_replacement(
                        directory_fd=directory_fd,
                        target_name=target.name,
                        original=original,
                        replacement_data=replacement_data,
                        operation="session invalidation",
                    )
                    raise
        return AgentConfigFileUpdateResponse(
            agent_id=safe_agent_id,
            path=path,
            container_path=str(target),
            exists=True,
            content=request.content,
            sha256=hashlib.sha256(replacement_data).hexdigest(),
            size_bytes=len(replacement_data),
            content_type=EDITABLE_AGENT_CONFIG_FILES[path],
            sdk_session_invalidated=invalidated,
        )

    def _resolve_target(self, *, agent_id: str, path: str) -> tuple[str, Path]:
        safe_agent_id = self._validate_agent_id(agent_id)
        if path not in EDITABLE_AGENT_CONFIG_FILES:
            raise AgentConfigFileError(422, "Only project .mcp.json is editable from this endpoint")
        record = self._agent_registry_store.get_agent(safe_agent_id)
        if record is None:
            raise AgentConfigFileError(404, f"Business agent not found: {safe_agent_id}")
        workspace = Path(record.workspace_dir)
        try:
            workspace_stat = workspace.lstat()
        except OSError as exc:
            raise AgentConfigFileError(409, f"Business agent workspace is missing: {safe_agent_id}") from exc
        if stat.S_ISLNK(workspace_stat.st_mode):
            raise AgentConfigFileError(409, "Business agent workspace symlink is not editable")
        if not stat.S_ISDIR(workspace_stat.st_mode):
            raise AgentConfigFileError(409, f"Business agent workspace is missing: {safe_agent_id}")
        policy = WorkspaceExecutionTargetPolicy(workspace)
        relative = policy.relative_path(path)
        if relative is None or policy.rel_excluded(relative):
            raise AgentConfigFileError(403, "unsafe_target_path")
        return safe_agent_id, workspace / relative

    def _validate_agent_id(self, agent_id: str) -> str:
        try:
            return validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise AgentConfigFileError(422, str(exc)) from exc

    @contextmanager
    def _locked_parent(self, target: Path, *, exclusive: bool) -> Iterator[int]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            directory_fd = os.open(target.parent, flags)
        except OSError as exc:
            detail = (
                "Business agent workspace symlink is not editable" if exc.errno == errno.ELOOP else f"Config directory open failed: {exc.__class__.__name__}"
            )
            raise AgentConfigFileError(409, detail) from exc
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield directory_fd
        finally:
            os.close(directory_fd)

    def _read_snapshot(self, *, directory_fd: int, target_name: str) -> _ConfigSnapshot:
        try:
            path_stat = os.stat(target_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _ConfigSnapshot(data=None)
        except OSError as exc:
            raise AgentConfigFileError(409, f"Config file stat failed: {exc.__class__.__name__}") from exc
        if stat.S_ISLNK(path_stat.st_mode):
            raise AgentConfigFileError(409, "Config file symlink is not editable")
        if not stat.S_ISREG(path_stat.st_mode):
            raise AgentConfigFileError(409, "Config path is not a regular file")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            file_fd = os.open(target_name, flags, dir_fd=directory_fd)
        except OSError as exc:
            detail = "Config file symlink is not editable" if exc.errno == errno.ELOOP else f"Config file read failed: {exc.__class__.__name__}"
            raise AgentConfigFileError(409, detail) from exc
        try:
            opened_stat = os.fstat(file_fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise AgentConfigFileError(409, "Config path is not a regular file")
            if opened_stat.st_size > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
                raise AgentConfigFileError(413, "Config file is too large to edit inline")
            with os.fdopen(file_fd, "rb") as source:
                file_fd = -1
                data = source.read(MAX_EXECUTION_TARGET_CONTEXT_BYTES + 1)
        except OSError as exc:
            raise AgentConfigFileError(409, f"Config file read failed: {exc.__class__.__name__}") from exc
        finally:
            if file_fd >= 0:
                os.close(file_fd)
        if len(data) > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
            raise AgentConfigFileError(413, "Config file is too large to edit inline")
        return _ConfigSnapshot(data=data, mode=stat.S_IMODE(opened_stat.st_mode) & 0o777)

    def _decode_snapshot(self, snapshot: _ConfigSnapshot) -> str:
        if snapshot.data is None:
            return ""
        try:
            return snapshot.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentConfigFileError(415, "Config file is not UTF-8 text") from exc

    def _atomic_replace(self, *, directory_fd: int, target_name: str, data: bytes, mode: int) -> None:
        temp_name: str | None = None
        temp_fd = -1
        replaced = False
        failure: OSError | None = None
        try:
            for _ in range(_TEMP_FILE_ATTEMPTS):
                candidate = f"{target_name}.tmp-{secrets.token_hex(8)}"
                try:
                    temp_fd = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                temp_name = candidate
                break
            if temp_name is None:
                raise FileExistsError(errno.EEXIST, "could not allocate config temp file")
            os.fchmod(temp_fd, mode)
            remaining = memoryview(data)
            while remaining:
                written = os.write(temp_fd, remaining)
                if written <= 0:  # pragma: no cover - defensive kernel contract guard
                    raise OSError(errno.EIO, "config temp file write made no progress")
                remaining = remaining[written:]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            os.replace(temp_name, target_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            replaced = True
            os.fsync(directory_fd)
        except OSError as exc:
            failure = exc
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_name is not None and not replaced:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    failure = cleanup_exc
        if failure is not None:
            raise _AtomicReplaceFailure(failure, replaced=replaced) from failure

    def _rollback_replacement(
        self,
        *,
        directory_fd: int,
        target_name: str,
        original: _ConfigSnapshot,
        replacement_data: bytes,
        operation: str,
    ) -> None:
        current = self._read_snapshot(directory_fd=directory_fd, target_name=target_name)
        if current.data != replacement_data:
            raise AgentConfigFileError(409, f"Config file changed during {operation}; rollback refused")
        try:
            if original.data is None:
                os.unlink(target_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            else:
                self._atomic_replace(
                    directory_fd=directory_fd,
                    target_name=target_name,
                    data=original.data,
                    mode=original.mode,
                )
        except (_AtomicReplaceFailure, OSError) as exc:
            cause = exc.cause if isinstance(exc, _AtomicReplaceFailure) else exc
            raise AgentConfigFileError(409, f"Config rollback failed after {operation}: {cause.__class__.__name__}") from exc

    def _validate_content(self, *, agent_id: str, path: str, content: str) -> None:
        data = content.encode("utf-8")
        if len(data) > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
            raise AgentConfigFileError(413, "Config file content is too large")
        if path == ".mcp.json":
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise AgentConfigFileError(422, f"Invalid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise AgentConfigFileError(422, ".mcp.json must contain a JSON object")
            env = dict(load_runtime_env(self._settings.settings_env_file)) if self._settings.settings_env_file else dict(os.environ)
            runtime_root = Path("/") if self._settings.data_dir.resolve() == Path("/data") else self._settings.data_dir.resolve().parent
            violations = validate_managed_mcp_content(
                content,
                agent_id=agent_id,
                runtime_mode=self._settings.runtime_volume_mode,
                env=env,
                runtime_root=runtime_root,
            )
            if violations:
                details = "; ".join(f"{item.rule_id}:{item.detail}" for item in violations)
                raise AgentConfigFileError(422, f"Managed MCP policy rejected the update: {details}")

    def _invalidate_session(self, *, agent_id: str, session_id: str | None) -> bool:
        if not session_id:
            return False
        session = self._session_store.get(session_id)
        if session is None:
            return False
        if session.agent_id and session.agent_id != agent_id:
            raise AgentConfigFileError(409, "Session belongs to a different business agent")
        if not session.agent_id:
            raise AgentConfigFileError(409, "Session has no unambiguous business agent owner")
        if not session.sdk_session_id:
            return False
        try:
            self._session_store.clear_sdk_session(session, agent_id=agent_id)
        except SessionConflictError as exc:
            raise AgentConfigFileError(409, str(exc)) from exc
        return True
