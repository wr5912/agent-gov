from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import yaml
from pydantic import TypeAdapter, ValidationError

from app.agent_testing.service import AgentTestingService
from app.runtime.agent_admission import AgentAdmissionError, AgentRunsActiveError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_governance_schemas import agent_summary_response as _summary
from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.agent_workspace_package_schemas import (
    NativeAgentCandidateResponse,
    NativeAgentCandidateSourceResponse,
    NativeAgentDataInput,
    WorkspaceImportResponse,
    WorkspaceRestoreRequest,
    WorkspaceRestoreResponse,
)
from app.runtime.json_types import JsonObject
from app.runtime.settings import AppSettings
from app.runtime.state_machines import is_agent_lifecycle_runnable
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.services import agent_workspace_manifest_identity as manifest_identity
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_candidate_creation import (
    AgentCandidateCreationError,
    AgentCandidateCreationService,
    CandidateStageReceipt,
    OpenCandidateSource,
)
from app.services.agent_native_candidate_mapping import (
    NativeCandidateMappingError,
    native_agent_data_entries,
    native_agent_data_from_harness,
)
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator
from app.services.agent_workspace_git_operations import (
    GitCommandError as _GitCommandError,
)
from app.services.agent_workspace_git_operations import (
    SnapshotState as _SnapshotState,
)
from app.services.agent_workspace_git_operations import (
    configure_workspace_git_storage as _configure_raw_git_storage,
)
from app.services.agent_workspace_git_operations import (
    restore_dirty_state_after_failure as _restore_dirty_state_after_failure,
)
from app.services.agent_workspace_git_operations import (
    run_git as _git,
)
from app.services.agent_workspace_git_operations import (
    snapshot_live_workspace as _snapshot_live_workspace,
)

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
WorkspacePackageError = package_codec.WorkspacePackageError
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspaceExportArtifact:
    path: Path
    filename: str
    commit_sha: str
    package_sha256: str
    tree_sha256: str


class AgentWorkspacePackageService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        registry_store: AgentRegistryStore,
        store_for: Callable[[str], GitAgentVersionStore],
        version_maintenance: AgentVersionMaintenanceCoordinator,
        has_open_change_sets: Callable[[str], bool],
        agent_testing: AgentTestingService,
        candidate_creation: AgentCandidateCreationService,
    ) -> None:
        self._settings = settings
        self._registry = registry_store
        self._store_for = store_for
        self._version_maintenance = version_maintenance
        self._has_open_change_sets = has_open_change_sets
        self._agent_testing = agent_testing
        self._candidate_creation = candidate_creation

    def export_workspace(self, agent_id: str) -> WorkspaceExportArtifact:
        try:
            safe_agent_id, _ = self._require_agent(agent_id)
            self._require_no_open_change_set(safe_agent_id)
            lease = self._version_maintenance.lease(
                agent_id=safe_agent_id,
                kind="workspace_export",
                owner_id="api:workspace-export",
            )
            lease.__enter__()
            artifact: WorkspaceExportArtifact | None = None
            snapshot: _SnapshotState | None = None
            try:
                store = self._store_for(safe_agent_id)
                store.ensure_bootstrap()
                self._require_no_open_change_set(safe_agent_id)
                with store.mutation_guard():
                    _configure_raw_git_storage(store.repository_dir)
                    snapshot = _snapshot_live_workspace(store)
                    try:
                        lease.assert_active()
                        artifact = self._archive_current_tree(store, safe_agent_id, snapshot.current_head)
                        lease.assert_active()
                        lease.close(validate_claim=True)
                        return artifact
                    except Exception:
                        if artifact is not None:
                            artifact.path.unlink(missing_ok=True)
                        _restore_dirty_state_after_failure(store, snapshot)
                        raise
            finally:
                lease.close(validate_claim=False)
        except AgentAdmissionError as exc:
            raise _workspace_admission_error(exc) from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            raise WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed") from exc

    def import_workspace(
        self,
        *,
        agent_id: str,
        package_file: BinaryIO,
        filename: str | None,
        name: str | None,
        expected_current_commit_sha: str | None,
        reason: str | None,
    ) -> WorkspaceImportResponse:
        safe_agent_id = _safe_agent_id(agent_id)
        existing = self._registry.get_agent(safe_agent_id)
        import_action = "overwrite" if existing is not None else "create"
        package: package_codec.ValidatedWorkspacePackage | None = None
        try:
            if existing is None:
                clean_name = _required_new_agent_name(name, expected_current_commit_sha)
                expected_commit = None
            else:
                clean_name = None
                expected_commit = _required_overwrite_commit(expected_current_commit_sha, agent_id=safe_agent_id)
            package = self._read_package(package_file, filename=filename)
            manifest_identity.validate_workspace_manifest_identity(
                package.entries,
                expected_agent_id=safe_agent_id,
                import_action=import_action,
            )
            receipt = self._candidate_creation.stage_entries(
                agent_id=safe_agent_id,
                name=clean_name,
                entries=package.entries,
                expected_current_commit_sha=expected_commit,
                replace_tree=True,
                operator="api:workspace-import",
                title=("Create draft Agent from workspace package" if existing is None else "Import workspace package candidate"),
                note=_commit_message(reason, default="Import workspace package candidate"),
            )
            return self._record_candidate_import(receipt=receipt, package=package, audit_action=import_action)
        except AgentAdmissionError as exc:
            error = _workspace_admission_error(exc)
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=error,
            )
            raise error from exc
        except WorkspacePackageError as exc:
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=exc,
            )
            raise
        except AgentCandidateCreationError as exc:
            error = WorkspacePackageError(exc.status_code, exc.error_code, exc.detail)
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=error,
            )
            raise error from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            error = WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed")
            self._record_import_failure(
                agent_id=safe_agent_id,
                action=import_action,
                package=package,
                error=error,
            )
            raise error from exc

    def restore_workspace(
        self,
        *,
        agent_id: str,
        request: WorkspaceRestoreRequest,
    ) -> WorkspaceRestoreResponse:
        try:
            safe_agent_id, record = self._require_agent(agent_id)
            expected = _full_commit(request.expected_current_commit_sha, field="expected_current_commit_sha")
            target = _full_commit(request.target_commit_sha, field="target_commit_sha")
            store = self._store_for(safe_agent_id)
            try:
                entries = package_codec.read_commit_entries(store.repository_dir, target, run_git=_git)
            except WorkspacePackageError as exc:
                raise WorkspacePackageError(
                    exc.status_code,
                    "WORKSPACE_RESTORE_TARGET_INVALID",
                    f"Restore target is not a valid workspace tree: {exc}",
                ) from exc
            receipt = self._candidate_creation.stage_entries(
                agent_id=safe_agent_id,
                name=record.name,
                entries=entries,
                expected_current_commit_sha=expected,
                replace_tree=True,
                operator="api:workspace-restore",
                title=f"Restore workspace tree from {target[:12]}",
                note=request.reason or f"Restore workspace tree from {target[:12]}",
            )
            return WorkspaceRestoreResponse(
                agent=_summary(record, agent_version_id=receipt.base_commit_sha),
                change_set_id=str(receipt.change_set["change_set_id"]),
                change_set_status=str(receipt.change_set["change_set_status"]),
                base_commit_sha=receipt.base_commit_sha,
                candidate_commit_sha=receipt.candidate_commit_sha,
                changed_paths=list(receipt.changed_paths),
                restored_tree_commit_sha=target,
            )
        except AgentCandidateCreationError as exc:
            raise WorkspacePackageError(exc.status_code, exc.error_code, exc.detail) from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            raise WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed") from exc

    def native_candidate(
        self,
        *,
        agent_id: str,
        agent_data: NativeAgentDataInput,
        schema: JsonObject,
        expected_current_commit_sha: str | None,
        reason: str | None,
        change_set_id: str | None = None,
        expected_candidate_commit_sha: str | None = None,
    ) -> NativeAgentCandidateResponse:
        safe_agent_id = _safe_agent_id(agent_id)
        record = self._registry.get_agent(safe_agent_id)
        try:
            expected, continuation = self._native_candidate_base(
                agent_id=safe_agent_id,
                record=record,
                expected_current_commit_sha=expected_current_commit_sha,
                change_set_id=change_set_id,
                expected_candidate_commit_sha=expected_candidate_commit_sha,
            )
            base_manifest = self._candidate_base_manifest(
                agent_id=safe_agent_id,
                expected_current_commit_sha=expected,
                continuation=continuation,
            )
            entries = native_agent_data_entries(
                agent_id=safe_agent_id,
                agent_data=agent_data,
                schema=schema,
                base_manifest=base_manifest,
            )
        except AgentCandidateCreationError as exc:
            raise WorkspacePackageError(exc.status_code, exc.error_code, exc.detail) from exc
        except NativeCandidateMappingError as exc:
            raise WorkspacePackageError(422, "NATIVE_AGENT_DATA_INVALID", str(exc)) from exc
        name = agent_data.name.strip()
        try:
            receipt = self._candidate_creation.stage_entries(
                agent_id=safe_agent_id,
                name=name,
                entries=entries,
                expected_current_commit_sha=expected,
                replace_tree=record is None,
                operator="api:native-agent-candidate",
                title=("Create draft Agent from AgentScope form" if record is None else "Update Agent from AgentScope form"),
                note=_commit_message(reason, default="Update AgentScope native AgentData candidate"),
                change_set_id=continuation.change_set_id if continuation is not None else None,
                expected_candidate_commit_sha=(continuation.candidate_commit_sha if continuation is not None else None),
            )
        except AgentCandidateCreationError as exc:
            raise WorkspacePackageError(exc.status_code, exc.error_code, exc.detail) from exc
        return NativeAgentCandidateResponse(
            action=receipt.action,
            agent=_summary(receipt.agent, agent_version_id=receipt.base_commit_sha),
            change_set_id=str(receipt.change_set["change_set_id"]),
            change_set_status=str(receipt.change_set["change_set_status"]),
            base_commit_sha=receipt.base_commit_sha,
            candidate_commit_sha=receipt.candidate_commit_sha,
            changed_paths=list(receipt.changed_paths),
        )

    def native_candidate_source(
        self,
        *,
        agent_id: str,
        schema: JsonObject,
    ) -> NativeAgentCandidateSourceResponse:
        safe_agent_id, record = self._require_agent(agent_id)
        try:
            candidate = self._candidate_creation.open_candidate_source(agent_id=safe_agent_id)
        except AgentCandidateCreationError as exc:
            raise WorkspacePackageError(exc.status_code, exc.error_code, exc.detail) from exc
        if candidate is not None:
            agent_data = self._native_agent_data_from_candidate(
                agent_id=safe_agent_id,
                candidate=candidate,
                schema=schema,
            )
            return NativeAgentCandidateSourceResponse(
                agent_data=agent_data,
                change_set_id=candidate.change_set_id,
                current_commit_sha=candidate.candidate_commit_sha,
            )
        if not is_agent_lifecycle_runnable(record.status):
            raise WorkspacePackageError(
                409,
                "NATIVE_AGENT_SOURCE_UNPUBLISHED",
                "Native form source is available only for a published runnable Agent",
            )
        store = self._store_for(safe_agent_id)
        try:
            current_commit_sha, dirty = store.inspect_clean_head()
            if dirty:
                raise WorkspacePackageError(
                    409,
                    "NATIVE_AGENT_SOURCE_DIRTY",
                    "Live Agent Workspace must be clean before reading the native form source",
                )
            manifest = self._read_manifest(safe_agent_id, current_commit_sha)
            system_prompt = store.read_text_at_ref(current_commit_sha, "AGENT.md")
            if system_prompt is None:
                raise WorkspacePackageError(409, "NATIVE_AGENT_SOURCE_INVALID", "Current AGENT.md is missing")
            agent_data = native_agent_data_from_harness(
                agent_id=safe_agent_id,
                manifest=manifest,
                system_prompt=system_prompt,
                schema=schema,
            )
        except NativeCandidateMappingError as exc:
            raise WorkspacePackageError(409, "NATIVE_AGENT_SOURCE_INVALID", str(exc)) from exc
        except AgentGitError as exc:
            raise WorkspacePackageError(409, "NATIVE_AGENT_SOURCE_INVALID", "Current Agent Git tree is unavailable") from exc
        return NativeAgentCandidateSourceResponse(
            agent_data=agent_data,
            change_set_id=None,
            current_commit_sha=current_commit_sha,
        )

    def _native_candidate_base(
        self,
        *,
        agent_id: str,
        record: AgentRegistryRecord | None,
        expected_current_commit_sha: str | None,
        change_set_id: str | None,
        expected_candidate_commit_sha: str | None,
    ) -> tuple[str | None, OpenCandidateSource | None]:
        if (change_set_id is None) != (expected_candidate_commit_sha is None):
            raise AgentCandidateCreationError(
                422,
                "CANDIDATE_CONTINUATION_INCOMPLETE",
                "change_set_id and expected_candidate_commit_sha must be provided together",
            )
        if change_set_id is not None and expected_candidate_commit_sha is not None:
            if expected_current_commit_sha is not None:
                raise AgentCandidateCreationError(
                    422,
                    "CANDIDATE_BASE_AMBIGUOUS",
                    "Candidate continuation must not carry expected_current_commit_sha",
                )
            expected_candidate = _full_commit(
                expected_candidate_commit_sha,
                field="expected_candidate_commit_sha",
            )
            candidate = self._candidate_creation.open_candidate_source(
                agent_id=agent_id,
                change_set_id=change_set_id,
            )
            if candidate is None:
                raise AgentCandidateCreationError(404, "CANDIDATE_CHANGE_SET_NOT_FOUND", "Agent change set not found")
            if candidate.candidate_commit_sha != expected_candidate:
                raise AgentCandidateCreationError(
                    409,
                    "CANDIDATE_COMMIT_CONFLICT",
                    "Candidate commit changed; reload before writing",
                )
            return None, candidate
        if record is None:
            if expected_current_commit_sha is not None:
                raise AgentCandidateCreationError(
                    422,
                    "CANDIDATE_BASE_UNEXPECTED",
                    "A new draft Agent must not carry expected_current_commit_sha",
                )
            return None, None
        if self._has_open_change_sets(agent_id):
            raise AgentCandidateCreationError(
                409,
                "CANDIDATE_CONTINUATION_REQUIRED",
                "Agent has an unfinished candidate; reload it and continue the same change set",
            )
        return _required_overwrite_commit(expected_current_commit_sha, agent_id=agent_id), None

    def _candidate_base_manifest(
        self,
        *,
        agent_id: str,
        expected_current_commit_sha: str | None,
        continuation: OpenCandidateSource | None,
    ) -> JsonObject | None:
        if continuation is not None:
            return self._parse_manifest(
                continuation.manifest_text,
                error_code="CANDIDATE_SOURCE_INVALID",
                detail="Candidate agent.yaml is unavailable or invalid",
            )
        if expected_current_commit_sha is not None:
            return self._read_manifest(agent_id, expected_current_commit_sha)
        return None

    def _native_agent_data_from_candidate(
        self,
        *,
        agent_id: str,
        candidate: OpenCandidateSource,
        schema: JsonObject,
    ) -> NativeAgentDataInput:
        manifest = self._parse_manifest(
            candidate.manifest_text,
            error_code="NATIVE_AGENT_SOURCE_INVALID",
            detail="Candidate agent.yaml is unavailable or invalid",
        )
        try:
            return native_agent_data_from_harness(
                agent_id=agent_id,
                manifest=manifest,
                system_prompt=candidate.system_prompt,
                schema=schema,
            )
        except NativeCandidateMappingError as exc:
            raise WorkspacePackageError(409, "NATIVE_AGENT_SOURCE_INVALID", str(exc)) from exc

    def _record_candidate_import(
        self,
        *,
        receipt: CandidateStageReceipt,
        package: package_codec.ValidatedWorkspacePackage,
        audit_action: str,
    ) -> WorkspaceImportResponse:
        import_id, suite = self._agent_testing.record_import(
            agent_id=receipt.agent.agent_id,
            action=audit_action,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            commit_sha=receipt.candidate_commit_sha,
        )
        warnings = [item for item in suite.diagnostics if item.level == "warning"]
        status = "invalid" if any(item.level == "error" for item in suite.diagnostics) else "warning" if warnings else "ready"
        return WorkspaceImportResponse(
            action=receipt.action,
            agent=_summary(receipt.agent, agent_version_id=receipt.base_commit_sha),
            change_set_id=str(receipt.change_set["change_set_id"]),
            change_set_status=str(receipt.change_set["change_set_status"]),
            base_commit_sha=receipt.base_commit_sha,
            candidate_commit_sha=receipt.candidate_commit_sha,
            changed_paths=list(receipt.changed_paths),
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            import_record_id=import_id,
            test_suite_status=status,
            test_file_count=suite.test_file_count,
            test_suite_warnings=warnings,
        )

    def _read_manifest(self, agent_id: str, commit_sha: str) -> JsonObject:
        try:
            source = self._store_for(agent_id).read_text_at_ref(commit_sha, "agent.yaml")
            if source is None:
                raise WorkspacePackageError(409, "CANDIDATE_BASE_INVALID", "Current agent.yaml is missing")
        except AgentGitError as exc:
            raise WorkspacePackageError(409, "CANDIDATE_BASE_INVALID", "Current agent.yaml is unavailable or invalid") from exc
        return self._parse_manifest(
            source,
            error_code="CANDIDATE_BASE_INVALID",
            detail="Current agent.yaml is unavailable or invalid",
        )

    @staticmethod
    def _parse_manifest(source: str, *, error_code: str, detail: str) -> JsonObject:
        try:
            parsed = TypeAdapter(JsonObject).validate_python(yaml.safe_load(source))
        except (UnicodeError, ValidationError, yaml.YAMLError) as exc:
            raise WorkspacePackageError(409, error_code, detail) from exc
        if not isinstance(parsed, dict):
            raise WorkspacePackageError(409, error_code, detail)
        return parsed

    def _record_import_failure(
        self,
        *,
        agent_id: str,
        action: str,
        package: package_codec.ValidatedWorkspacePackage | None,
        error: WorkspacePackageError,
    ) -> None:
        try:
            self._agent_testing.record_import_failure(
                agent_id=agent_id,
                action=action,
                package_sha256=package.package_sha256 if package else None,
                tree_sha256=package.tree_sha256 if package else None,
                error_code=error.error_code,
                detail=str(error),
            )
        except Exception:
            logger.warning(
                "Failed to persist Workspace import failure audit: agent_id=%s action=%s",
                agent_id,
                action,
                exc_info=True,
            )

    def _read_package(self, package_file: BinaryIO, *, filename: str | None) -> package_codec.ValidatedWorkspacePackage:
        temporary = self._temporary_path(suffix=".upload.tar.gz")
        try:
            return package_codec.read_workspace_package(package_file, temporary, filename=filename)
        finally:
            temporary.unlink(missing_ok=True)

    def _archive_current_tree(
        self,
        store: GitAgentVersionStore,
        agent_id: str,
        commit_sha: str,
    ) -> WorkspaceExportArtifact:
        archive_path = self._temporary_path(suffix=".tar.gz")
        try:
            entries = package_codec.read_commit_entries(store.repository_dir, commit_sha, run_git=_git)
            package_codec.write_workspace_archive(archive_path, entries)
            if archive_path.stat().st_size > package_codec.MAX_COMPRESSED_PACKAGE_BYTES:
                raise WorkspacePackageError(
                    413,
                    "WORKSPACE_PACKAGE_TOO_LARGE",
                    f"Compressed workspace package exceeds {package_codec.MAX_COMPRESSED_PACKAGE_BYTES} bytes",
                )
            package_sha256 = package_codec.sha256_file(archive_path)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return WorkspaceExportArtifact(
            path=archive_path,
            filename=f"{agent_id}-workspace-{commit_sha[:12]}.tar.gz",
            commit_sha=commit_sha,
            package_sha256=package_sha256,
            tree_sha256=package_codec.tree_sha256(entries),
        )

    def _temporary_path(self, *, suffix: str) -> Path:
        root = self._settings.data_dir / ".workspace-package-tmp"
        root.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix="agentgov-", suffix=suffix, dir=root)
        os.close(descriptor)
        return Path(raw_path)

    def _require_agent(self, agent_id: str) -> tuple[str, AgentRegistryRecord]:
        safe_agent_id = _safe_agent_id(agent_id)
        record = self._registry.get_agent(safe_agent_id)
        if record is None:
            raise WorkspacePackageError(404, "WORKSPACE_AGENT_NOT_FOUND", f"Business Agent not found: {safe_agent_id}")
        return safe_agent_id, record

    def _require_no_open_change_set(self, agent_id: str) -> None:
        if self._has_open_change_sets(agent_id):
            raise WorkspacePackageError(
                409,
                "WORKSPACE_CHANGE_SET_ACTIVE",
                f"Business Agent {agent_id} has an unfinished change set",
            )


def _safe_agent_id(agent_id: str) -> str:
    try:
        normalized = validate_agent_id(agent_id)
    except InvalidAgentId as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_AGENT_ID_INVALID",
            (
                "Workspace 请求被拒绝：URL 中的 agent_id 无效；它必须是非空值，只能包含英文字母、"
                "数字、点、下划线或连字符，且不能是 “.” 或 “..”。请修正 URL 中的目标 ID 后重试。"
            ),
            error_details={
                "field": "url.agent_id",
                "remediation": "使用有效且不含首尾空白的目标 Agent ID 后重试。",
            },
        ) from exc
    if normalized != agent_id:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_AGENT_ID_INVALID",
            ("Workspace 请求被拒绝：URL 中的 agent_id 不能包含首尾空白；系统不会自动修正目标身份。请移除首尾空白后重试。"),
            error_details={
                "field": "url.agent_id",
                "remediation": "移除 URL 目标 Agent ID 的首尾空白后重试。",
            },
        )
    return normalized


def _required_new_agent_name(name: str | None, expected_current_commit_sha: str | None) -> str:
    clean_name = (name or "").strip()
    if not clean_name:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_IMPORT_NAME_REQUIRED",
            "导入被拒绝：创建新业务 Agent 时必须提供非空 name。请填写业务 Agent 名称后重新导入。",
            error_details={
                "field": "name",
                "import_action": "create",
                "remediation": "填写业务 Agent 名称后重新导入。",
            },
        )
    if len(clean_name) > 120:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_IMPORT_NAME_INVALID",
            "导入被拒绝：name 不能超过 120 个字符。请缩短名称后重新导入。",
            error_details={
                "field": "name",
                "import_action": "create",
                "remediation": "将业务 Agent 名称缩短到 120 个字符以内后重新导入。",
            },
        )
    if expected_current_commit_sha:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_IMPORT_UNEXPECTED_CURRENT_REF",
            ("导入被拒绝：目标 Agent 尚不存在，本请求属于新建导入，不应携带 expected_current_commit_sha。请移除该字段后重新导入。"),
            error_details={
                "field": "expected_current_commit_sha",
                "import_action": "create",
                "remediation": "移除 expected_current_commit_sha 后重新导入。",
            },
        )
    return clean_name


def _required_overwrite_commit(value: str | None, *, agent_id: str) -> str:
    if not value:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_IMPORT_CURRENT_REF_REQUIRED",
            (
                f"导入被拒绝：业务 Agent “{agent_id}”已经存在，本请求属于覆盖导入，但缺少 "
                "expected_current_commit_sha。请先获取该 Agent 当前提交版本，再通过覆盖导入入口"
                "携带该版本重试。"
            ),
            error_details={
                "field": "expected_current_commit_sha",
                "import_action": "overwrite",
                "expected_agent_id": agent_id,
                "remediation": "先获取该 Agent 当前提交版本，再通过覆盖导入入口携带该版本重试。",
            },
        )
    return _full_commit(value, field="expected_current_commit_sha")


def _full_commit(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _FULL_COMMIT_RE.fullmatch(normalized):
        raise WorkspacePackageError(422, "WORKSPACE_COMMIT_INVALID", f"{field} must be a full 40-character Git commit SHA")
    return normalized


def _commit_message(value: str | None, *, default: str) -> str:
    normalized = (value or "").strip() or default
    if len(normalized) > 512:
        raise WorkspacePackageError(422, "WORKSPACE_IMPORT_REASON_INVALID", "reason must not exceed 512 characters")
    return normalized


def _workspace_admission_error(exc: AgentAdmissionError) -> WorkspacePackageError:
    code = "WORKSPACE_SESSION_INVALIDATION_CONFLICT" if isinstance(exc, AgentRunsActiveError) else "WORKSPACE_MAINTENANCE_CONFLICT"
    return WorkspacePackageError(409, code, str(exc))
