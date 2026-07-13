from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.config_file_schemas import (
    AgentConfigFileResponse,
    AgentConfigFileUpdateRequest,
    AgentConfigFileUpdateResponse,
)
from app.runtime.errors import SessionConflictError
from app.runtime.execution_targets import MAX_EXECUTION_TARGET_CONTEXT_BYTES, WorkspaceExecutionTargetPolicy
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore

EDITABLE_AGENT_CONFIG_FILES = {".mcp.json": "application/json"}


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
        content, sha256, size_bytes = self._read_target(target)
        return AgentConfigFileResponse(
            agent_id=safe_agent_id,
            path=path,
            container_path=str(target),
            exists=target.exists(),
            content=content,
            sha256=sha256,
            size_bytes=size_bytes,
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
        self._validate_content(path=path, content=request.content)
        _, current_sha256, _ = self._read_target(target)
        if request.expected_sha256 is not None and request.expected_sha256 != current_sha256:
            raise AgentConfigFileError(409, "Config file changed; reload before applying edits")
        target.write_text(request.content, encoding="utf-8")
        content, sha256, size_bytes = self._read_target(target)
        invalidated = self._invalidate_session(agent_id=safe_agent_id, session_id=request.session_id)
        return AgentConfigFileUpdateResponse(
            agent_id=safe_agent_id,
            path=path,
            container_path=str(target),
            exists=True,
            content=content,
            sha256=sha256,
            size_bytes=size_bytes,
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
        if not workspace.is_dir():
            raise AgentConfigFileError(409, f"Business agent workspace is missing: {safe_agent_id}")
        policy = WorkspaceExecutionTargetPolicy(workspace)
        denied = policy.denied_reason(path)
        if denied:
            raise AgentConfigFileError(403, denied)
        target = policy.target_path(path)
        if target is None:
            raise AgentConfigFileError(403, "target_path_escapes_workspace")
        return safe_agent_id, target

    def _validate_agent_id(self, agent_id: str) -> str:
        try:
            return validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise AgentConfigFileError(422, str(exc)) from exc

    def _read_target(self, target: Path) -> tuple[str, str | None, int]:
        if not target.exists():
            return "", None, 0
        try:
            stat = target.lstat()
        except OSError as exc:
            raise AgentConfigFileError(409, f"Config file stat failed: {exc.__class__.__name__}") from exc
        if target.is_symlink():
            raise AgentConfigFileError(409, "Config file symlink is not editable")
        if not target.is_file():
            raise AgentConfigFileError(409, "Config path is not a regular file")
        if stat.st_size > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
            raise AgentConfigFileError(413, "Config file is too large to edit inline")
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise AgentConfigFileError(409, f"Config file read failed: {exc.__class__.__name__}") from exc
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentConfigFileError(415, "Config file is not UTF-8 text") from exc
        return content, hashlib.sha256(data).hexdigest(), len(data)

    def _validate_content(self, *, path: str, content: str) -> None:
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
