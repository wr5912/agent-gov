from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, NoReturn

from app.agent_testing.service import (
    AgentImportAuditPersistenceError,
    AgentTestingService,
    PreparedWorkspaceImportAudit,
)
from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_governance_schemas import agent_summary_response as _summary
from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.agent_workspace_package_schemas import (
    WorkspaceImportResponse,
    WorkspaceRestoreRequest,
    WorkspaceRestoreResponse,
)
from app.runtime.errors import DataIntegrityError, SessionConflictError
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.services import agent_workspace_manifest_identity as manifest_identity
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_version_maintenance import (
    AgentVersionMaintenanceCoordinator,
    AgentVersionMaintenanceLease,
)
from app.services.agent_workspace_activation import WorkspaceActivationService
from app.services.agent_workspace_activation_contracts import (
    WorkspaceActivationFailure,
    WorkspaceActivationPersistenceError,
    activation_error_projection,
)
from app.services.agent_workspace_create_service import create_workspace_from_package
from app.services.agent_workspace_git_operations import (
    GitCommandError as _GitCommandError,
)
from app.services.agent_workspace_git_operations import (
    SnapshotState as _SnapshotState,
)
from app.services.agent_workspace_git_operations import (
    TreeReplacement as _TreeReplacement,
)
from app.services.agent_workspace_git_operations import (
    configure_workspace_git_storage as _configure_raw_git_storage,
)
from app.services.agent_workspace_git_operations import (
    observe_live_workspace as _observe_live_workspace,
)
from app.services.agent_workspace_git_operations import (
    prepare_workspace_snapshot as _prepare_workspace_snapshot,
)
from app.services.agent_workspace_git_operations import (
    replace_tree_from_entries as _replace_tree_from_entries,
)
from app.services.agent_workspace_git_operations import (
    restore_dirty_state_after_failure as _restore_dirty_state_after_failure,
)
from app.services.agent_workspace_git_operations import (
    restore_tree_as_commit as _restore_tree_as_commit,
)
from app.services.agent_workspace_git_operations import (
    run_git as _git,
)
from app.services.agent_workspace_git_operations import (
    snapshot_live_workspace as _snapshot_live_workspace,
)
from app.services.agent_workspace_package_inputs import commit_message as _commit_message
from app.services.agent_workspace_package_inputs import create_provisioning_error as _create_provisioning_error
from app.services.agent_workspace_package_inputs import full_commit as _full_commit
from app.services.agent_workspace_package_inputs import workspace_admission_error as _workspace_admission_error
from app.services.agent_workspace_package_results import build_import_response as _build_import_response
from app.services.agent_workspace_package_results import record_import_failure as _persist_import_failure
from app.services.business_agent_provisioning import BusinessAgentProvisioningFailure

WorkspacePackageError = package_codec.WorkspacePackageError
logger = logging.getLogger(__name__)


class _ActivationAuditedWorkspaceError(WorkspacePackageError):
    """The durable activation operation already owns this failure audit."""


@dataclass(frozen=True)
class WorkspaceExportArtifact:
    path: Path
    filename: str
    commit_sha: str
    package_sha256: str
    tree_sha256: str


@dataclass(frozen=True)
class _OverwriteActivationResult:
    record: AgentRegistryRecord
    snapshot: _SnapshotState
    replacement: _TreeReplacement
    audit: PreparedWorkspaceImportAudit


class AgentWorkspacePackageService:
    def __init__(
        self,
        *,
        settings: AppSettings,
        registry_store: AgentRegistryStore,
        store_for: Callable[[str], GitAgentVersionStore],
        version_maintenance: AgentVersionMaintenanceCoordinator,
        has_open_change_sets: Callable[[str], bool],
        session_store: LocalSessionStore,
        agent_testing: AgentTestingService,
        activation_service: WorkspaceActivationService,
    ) -> None:
        self._settings = settings
        self._registry = registry_store
        self._store_for = store_for
        self._version_maintenance = version_maintenance
        self._has_open_change_sets = has_open_change_sets
        self._session_store = session_store
        self._agent_testing = agent_testing
        self._activation = activation_service

    def export_workspace(self, agent_id: str) -> WorkspaceExportArtifact:
        try:
            safe_agent_id, record = self._require_agent(agent_id)
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
                self._require_no_open_change_set(safe_agent_id)
                with store.mutation_guard():
                    self._require_same_agent_instance(record)
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
            clean_name, expected_commit, commit_message = _import_parameters(
                existing=existing,
                agent_id=safe_agent_id,
                name=name,
                expected_current_commit_sha=expected_current_commit_sha,
                reason=reason,
            )
            package = self._read_package(package_file, filename=filename)
            return self._apply_validated_import(
                agent_id=safe_agent_id,
                existing=existing,
                package=package,
                clean_name=clean_name,
                expected_commit=expected_commit,
                commit_message=commit_message,
            )
        except AgentAdmissionError as exc:
            error = _workspace_admission_error(exc)
            self._raise_recorded_import_error(safe_agent_id, import_action, package, error, exc)

        except AgentImportAuditPersistenceError as exc:
            error = WorkspacePackageError(
                503,
                "WORKSPACE_IMPORT_AUDIT_FAILED",
                "Workspace import could not be committed safely; no candidate was activated.",
            )
            self._raise_recorded_import_error(safe_agent_id, import_action, package, error, exc)
        except WorkspaceActivationPersistenceError as exc:
            error = WorkspacePackageError(
                503,
                "WORKSPACE_IMPORT_ACTIVATION_INTENT_FAILED",
                "Workspace import activation intent could not be persisted; no candidate was activated.",
            )
            self._raise_recorded_import_error(safe_agent_id, import_action, package, error, exc)
        except (BusinessAgentProvisioningFailure, DataIntegrityError) as exc:
            self._raise_recorded_import_error(
                safe_agent_id,
                import_action,
                package,
                _create_provisioning_error(exc),
                exc,
            )
        except WorkspacePackageError as exc:
            if not isinstance(exc, _ActivationAuditedWorkspaceError):
                _persist_import_failure(
                    agent_testing=self._agent_testing,
                    logger=logger,
                    agent_id=safe_agent_id,
                    action=import_action,
                    package=package,
                    error=exc,
                )
            raise
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            error = WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed")
            self._raise_recorded_import_error(safe_agent_id, import_action, package, error, exc)

    def _raise_recorded_import_error(
        self,
        agent_id: str,
        action: str,
        package: package_codec.ValidatedWorkspacePackage | None,
        error: WorkspacePackageError,
        cause: Exception,
    ) -> NoReturn:
        _persist_import_failure(
            agent_testing=self._agent_testing,
            logger=logger,
            agent_id=agent_id,
            action=action,
            package=package,
            error=error,
        )
        raise error from cause

    def _apply_validated_import(
        self,
        *,
        agent_id: str,
        existing: AgentRegistryRecord | None,
        package: package_codec.ValidatedWorkspacePackage,
        clean_name: str | None,
        expected_commit: str | None,
        commit_message: str | None,
    ) -> WorkspaceImportResponse:
        manifest_identity.validate_workspace_manifest_identity(
            package.entries,
            expected_agent_id=agent_id,
            import_action="overwrite" if existing else "create",
        )
        if existing is None:
            assert clean_name is not None
            return self._create_from_package(agent_id=agent_id, name=clean_name, package=package)
        assert expected_commit is not None and commit_message is not None
        return self._overwrite_from_package(
            record=existing,
            expected_current_commit_sha=expected_commit,
            package=package,
            commit_message=commit_message,
        )

    def restore_workspace(
        self,
        *,
        agent_id: str,
        request: WorkspaceRestoreRequest,
    ) -> WorkspaceRestoreResponse:
        operation_id: str | None = None
        try:
            safe_agent_id, record = self._require_agent(agent_id)
            expected = _full_commit(request.expected_current_commit_sha, field="expected_current_commit_sha")
            target = _full_commit(request.target_commit_sha, field="target_commit_sha")
            self._require_no_open_change_set(safe_agent_id)
            self._require_no_active_session_turn(safe_agent_id)
            lease = self._version_maintenance.lease(
                agent_id=safe_agent_id,
                kind="workspace_restore",
                owner_id="api:workspace-restore",
            )
            lease.__enter__()
            snapshot: _SnapshotState | None = None
            response_record: AgentRegistryRecord | None = None
            try:
                store = self._store_for(safe_agent_id)
                self._require_no_open_change_set(safe_agent_id)
                with store.mutation_guard():
                    response_record = self._require_same_agent_instance(record)
                    observation = _observe_live_workspace(store, expected_head=expected)
                    preparation = self._activation.begin_restore(
                        agent_id=safe_agent_id,
                        observation=observation,
                        claim=lease.claim,
                    )
                    operation_id = preparation.operation_id
                    try:
                        _configure_raw_git_storage(store.repository_dir)
                        lease.assert_active()
                        snapshot = _prepare_workspace_snapshot(
                            store,
                            observation=observation,
                            operation_id=operation_id,
                        )
                        replacement = _restore_tree_as_commit(
                            store,
                            base_commit=snapshot.current_head,
                            target_commit=target,
                            message=request.reason or f"Restore workspace tree from {target[:12]}",
                            operation_id=operation_id,
                        )
                        self._activation.prepare_restore(
                            operation_id,
                            snapshot=snapshot,
                            replacement=replacement,
                            target_commit_sha=target,
                        )
                        self._activation.activate(operation_id, before_activate=lease.assert_active)
                    except Exception as exc:
                        self._raise_activation_failure(operation_id, exc, import_operation=False)
            finally:
                lease.close(validate_claim=False)
            assert snapshot is not None and response_record is not None
            return WorkspaceRestoreResponse(
                agent=_summary(response_record),
                previous_commit_sha=snapshot.original_head,
                current_commit_sha=replacement.current_commit_sha,
                restored_tree_commit_sha=target,
                rollback_target_commit_sha=replacement.previous_commit_sha,
            )
        except WorkspaceActivationPersistenceError as exc:
            raise WorkspacePackageError(
                503,
                "WORKSPACE_RESTORE_ACTIVATION_INTENT_FAILED",
                "Workspace restore activation intent could not be persisted; no candidate was activated.",
            ) from exc
        except AgentAdmissionError as exc:
            raise _workspace_admission_error(exc) from exc
        except (AgentGitError, package_codec.WorkspaceGitReadError, _GitCommandError) as exc:
            raise WorkspacePackageError(409, "WORKSPACE_GIT_OPERATION_FAILED", "Git workspace operation failed") from exc

    def _create_from_package(
        self,
        *,
        agent_id: str,
        name: str,
        package: package_codec.ValidatedWorkspacePackage,
    ) -> WorkspaceImportResponse:
        return create_workspace_from_package(
            settings=self._settings,
            registry=self._registry,
            agent_testing=self._agent_testing,
            agent_id=agent_id,
            name=name,
            package=package,
        )

    def _overwrite_from_package(
        self,
        *,
        record: AgentRegistryRecord,
        expected_current_commit_sha: str,
        package: package_codec.ValidatedWorkspacePackage,
        commit_message: str,
    ) -> WorkspaceImportResponse:
        self._require_no_open_change_set(record.agent_id)
        self._require_no_active_session_turn(record.agent_id)
        lease = self._version_maintenance.lease(
            agent_id=record.agent_id,
            kind="workspace_import",
            owner_id="api:workspace-import",
        )
        lease.__enter__()
        try:
            result = self._activate_overwrite(
                record=record,
                expected_current_commit_sha=expected_current_commit_sha,
                package=package,
                commit_message=commit_message,
                lease=lease,
            )
        finally:
            lease.close(validate_claim=False)
        replacement = result.replacement
        return _build_import_response(
            action="unchanged" if replacement.action == "unchanged" else "overwritten",
            agent=_summary(result.record),
            previous_commit_sha=result.snapshot.original_head,
            current_commit_sha=replacement.current_commit_sha,
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            rollback_target_commit_sha=(
                result.snapshot.original_head
                if replacement.action == "unchanged" and replacement.current_commit_sha != result.snapshot.original_head
                else replacement.previous_commit_sha
                if replacement.action != "unchanged"
                else None
            ),
            prepared_audit=result.audit,
        )

    def _activate_overwrite(
        self,
        *,
        record: AgentRegistryRecord,
        expected_current_commit_sha: str,
        package: package_codec.ValidatedWorkspacePackage,
        commit_message: str,
        lease: AgentVersionMaintenanceLease,
    ) -> _OverwriteActivationResult:
        store = self._store_for(record.agent_id)
        self._require_no_open_change_set(record.agent_id)
        with store.mutation_guard():
            fresh_record = self._require_same_agent_instance(record)
            observation = _observe_live_workspace(store, expected_head=expected_current_commit_sha)
            preparation = self._activation.begin_import(
                agent_id=record.agent_id,
                observation=observation,
                claim=lease.claim,
                package_sha256=package.package_sha256,
                tree_sha256=package.tree_sha256,
            )
            result: _OverwriteActivationResult | None = None
            try:
                _configure_raw_git_storage(store.repository_dir)
                lease.assert_active()
                snapshot = _prepare_workspace_snapshot(
                    store,
                    observation=observation,
                    operation_id=preparation.operation_id,
                )
                replacement = _replace_tree_from_entries(
                    store,
                    base_commit=snapshot.current_head,
                    entries=package.entries,
                    message=commit_message,
                    operation_id=preparation.operation_id,
                )
                audit = self._prepare_overwrite_audit(fresh_record, package, replacement, preparation.import_id)
                self._activation.prepare_import(
                    preparation.operation_id,
                    snapshot=snapshot,
                    replacement=replacement,
                    prepared_audit=audit,
                )
                result = _OverwriteActivationResult(fresh_record, snapshot, replacement, audit)
                self._activation.activate(preparation.operation_id, before_activate=lease.assert_active)
                return result
            except Exception as exc:
                if self._raise_activation_failure(preparation.operation_id, exc, import_operation=True) and result:
                    return result
                raise WorkspaceActivationPersistenceError("Completed Workspace activation result is unavailable") from exc

    def _prepare_overwrite_audit(
        self,
        record: AgentRegistryRecord,
        package: package_codec.ValidatedWorkspacePackage,
        replacement: _TreeReplacement,
        import_id: str | None,
    ) -> PreparedWorkspaceImportAudit:
        audit = self._agent_testing.prepare_import(
            agent_id=record.agent_id,
            action="unchanged" if replacement.action == "unchanged" else "overwritten",
            package_sha256=package.package_sha256,
            tree_sha256=package.tree_sha256,
            commit_sha=replacement.current_commit_sha,
        )
        return replace(audit, import_id=import_id)

    def _raise_activation_failure(
        self,
        operation_id: str,
        exc: Exception,
        *,
        import_operation: bool,
    ) -> bool:
        projected = activation_error_projection(exc)
        try:
            resolution = self._activation.reject(
                operation_id,
                failure=WorkspaceActivationFailure(
                    error_code=projected.error_code,
                    detail=str(projected),
                ),
            )
        except WorkspaceActivationPersistenceError as recovery_error:
            error_type = _ActivationAuditedWorkspaceError if import_operation else WorkspacePackageError
            raise error_type(
                503,
                "WORKSPACE_ACTIVATION_OUTCOME_INDETERMINATE",
                "Workspace activation outcome could not be verified; inspect current version and audit before retrying.",
            ) from recovery_error
        if resolution == "completed":
            return True
        error_type = _ActivationAuditedWorkspaceError if import_operation else WorkspacePackageError
        if resolution == "rejected":
            raise error_type(
                projected.status_code,
                projected.error_code,
                str(projected),
            ) from exc
        raise error_type(
            503,
            "WORKSPACE_ACTIVATION_RECOVERY_REQUIRED",
            "Workspace activation state is pending deterministic recovery; this Agent remains fenced.",
        ) from exc

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

    def _require_same_agent_instance(self, expected: AgentRegistryRecord) -> AgentRegistryRecord:
        current = self._registry.get_agent(expected.agent_id)
        if current is None or current.instance_etag != expected.instance_etag:
            raise WorkspacePackageError(
                409,
                "WORKSPACE_AGENT_INSTANCE_CHANGED",
                f"Business Agent instance changed before Workspace mutation: {expected.agent_id}",
            )
        return current

    def _require_no_open_change_set(self, agent_id: str) -> None:
        if self._has_open_change_sets(agent_id):
            raise WorkspacePackageError(
                409,
                "WORKSPACE_CHANGE_SET_ACTIVE",
                f"Business Agent {agent_id} has an unfinished change set",
            )

    def _require_no_active_session_turn(self, agent_id: str) -> None:
        try:
            self._session_store.require_no_active_turns_for_agent(agent_id=agent_id)
        except SessionConflictError as exc:
            raise WorkspacePackageError(
                409,
                "WORKSPACE_SESSION_INVALIDATION_CONFLICT",
                str(exc),
            ) from exc


def _safe_agent_id(agent_id: str) -> str:
    try:
        normalized = validate_agent_id(agent_id)
    except InvalidAgentId as exc:
        raise WorkspacePackageError(
            422,
            "WORKSPACE_AGENT_ID_INVALID",
            (
                "Workspace 请求被拒绝：URL 中的 agent_id 无效；它必须是非空值，只能包含英文字母、"
                "数字、点、下划线或连字符，长度最多 128 个字符，且不能是 “.” 或 “..”。"
                "请修正 URL 中的目标 ID 后重试。"
            ),
            error_details={
                "field": "url.agent_id",
                "remediation": "使用不超过 128 个字符且不含首尾空白的有效目标 Agent ID 后重试。",
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


def _import_parameters(
    *,
    existing: AgentRegistryRecord | None,
    agent_id: str,
    name: str | None,
    expected_current_commit_sha: str | None,
    reason: str | None,
) -> tuple[str | None, str | None, str | None]:
    if existing is None:
        return _required_new_agent_name(name, expected_current_commit_sha), None, None
    return (
        None,
        _required_overwrite_commit(expected_current_commit_sha, agent_id=agent_id),
        _commit_message(reason, default="Import workspace package"),
    )


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
