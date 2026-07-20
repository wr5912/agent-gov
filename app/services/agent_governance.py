from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.errors import ConflictError, FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.managed_agent_policy import ManagedAgentPolicyError, require_runtime_workspace_policy
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import (
    AgentChangeSetEventModel,
    AgentChangeSetModel,
    AgentReleaseModel,
    utc_now,
)
from app.runtime.runtime_db_base import begin_sqlite_write_transaction
from app.runtime.state_machines import validate_transition
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_change_set_provisioner import (
    ChangeSetProvisionConflict,
    ChangeSetSource,
    provision_change_set_under_maintenance,
)
from app.services.agent_change_set_worktree_lifecycle import (
    abandon_change_set_and_cleanup,
    execute_worktree_cleanup,
    reconcile_worktree_cleanup_tasks,
)
from app.services.agent_governance_projections import (
    diff_summary,
    event_to_payload,
    release_to_payload,
)
from app.services.agent_publication import (
    PublicationFinalizationLost,
    PublicationIntent,
    PublicationReservationLost,
    PublicationSourceConflict,
    PublicationTagConflict,
    capture_publication_source,
    commit_publication_intent,
    validate_intent_provenance,
)
from app.services.agent_publication_finalization import finalize_publication_once
from app.services.agent_publication_provenance import project_current_attribution
from app.services.agent_ref_policy import build_ref_policy_validator
from app.services.agent_release_workflows import (
    publish_change_set,
    reconcile_release_operations,
    restore_release,
    rollback_release,
)
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator

TERMINAL_CHANGE_SET_STATES = {"published", "rejected", "abandoned", "failed"}
# pending_approval 不可直接发布：高风险变更必须先经 approve_change_set 转为 approved（AGV-041）。
PUBLISHABLE_CHANGE_SET_STATES = {"candidate_committed", "approved"}


class AgentGovernanceError(FeedbackStoreError):
    """Route-safe error for Agent governance operations."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        if status_code == 404:
            self.error_code = "NOT_FOUND"
        elif status_code == 409:
            self.error_code = "CONFLICT"
        else:
            self.error_code = "AGENT_GOVERNANCE_ERROR"


class AgentGovernanceService:
    """Coordinates Git-backed Agent change sets, releases, and rollback."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        agent_version_store: GitAgentVersionStore,
        runtime_mode: str = "container",
        runtime_env: Mapping[str, str] | None = None,
    ) -> None:
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store
        self.version_maintenance = AgentVersionMaintenanceCoordinator(feedback_store.Session)
        # 每个业务 Agent 一套独立 git 版本链，懒初始化并缓存。这里曾预置 main-agent 条目：
        # main 是可删除的普通业务 Agent，预置会让它被删除后仍能取到指向已清理目录的悬空 store。
        self._agent_stores: dict[str, GitAgentVersionStore] = {}
        self._runtime_mode = runtime_mode
        self._runtime_env = dict(runtime_env or os.environ)
        # 业务 Agent 必须在注册表中存在才允许建/取其版本库，杜绝幽灵 Agent。
        # 由 app 装配后注入（None 则不校验，便于单测）。
        self.agent_exists: Callable[[str], bool] | None = None
        self.latest_passed_test_run: Callable[[str, str], JsonObject | None] | None = None

    def evict_agent_store(self, agent_id: str) -> None:
        """丢弃某 Agent 的版本 store 缓存。

        删除 Agent 后必须调用：缓存的 store 持有已被 rmtree 的 repository_dir，同 id 重建时
        会命中这个悬空 store，把新 Agent 的版本操作打到一个不存在的目录上。
        """

        self._agent_stores.pop((agent_id or "").strip(), None)

    def _normalize_agent_id(self, agent_id: str | None) -> str:
        normalized = (agent_id or DEFAULT_BUSINESS_AGENT_ID).strip()
        try:
            return validate_agent_id(normalized)
        except InvalidAgentId as exc:
            raise AgentGovernanceError(400, f"Invalid agent_id for version governance: {agent_id!r}") from exc

    def _store_for(self, agent_id: str | None) -> GitAgentVersionStore:
        """按 agent_id 选版本 store。

        每个业务 Agent（含 main-agent）的版本库 root 在其 **workspace**（git 就地版本化配置），
        worktrees/releases 落 ``data_dir/business-agents/{agent_id}/version/`` 兄弟目录，
        claude-root 因去嵌套在 workspace 之外、天然不进版本源。懒初始化并缓存。
        """
        normalized = self._normalize_agent_id(agent_id)
        existing = self._agent_stores.get(normalized)
        if existing is not None:
            return existing
        # 懒建版本库前校验该业务 Agent 在注册表中存在，杜绝幽灵 Agent。main-agent 不再豁免：
        # 它可被删除，删除后对它的版本治理请求应当 404 而不是就地重建版本库。
        if self.agent_exists is not None and not self.agent_exists(normalized):
            raise AgentGovernanceError(404, f"Agent not registered for version governance: {normalized}")
        layout = business_agent_layout(self.feedback_store.data_dir, normalized)
        store = GitAgentVersionStore(
            repository_dir=layout.workspace,
            worktrees_dir=layout.version_base / "worktrees",
            releases_dir=layout.version_base / "releases",
            repository_name=f"{normalized}-config",
        )
        store.ensure_bootstrap()
        self._agent_stores[normalized] = store
        return store

    def repository_status(self, agent_id: str | None = None) -> JsonObject:
        return self._store_for(agent_id).repository_status()

    def discard_repository_changes(self, paths: list[str], agent_id: str | None = None) -> JsonObject:
        try:
            return self._store_for(agent_id).discard_workspace_changes(paths)
        except AgentGitError as exc:
            raise AgentGovernanceError(409, str(exc)) from exc

    def snapshot_repository(self, *, operator: str = "runtime", note: str | None = None, agent_id: str | None = None) -> JsonObject:
        normalized = self._normalize_agent_id(agent_id)
        try:
            return self._store_for(agent_id).create_snapshot(
                reason="manual_workspace_snapshot",
                note=note or f"{operator} 保存 {normalized} workspace 当前未提交改动。",
            )
        except AgentGitError as exc:
            raise AgentGovernanceError(409, str(exc)) from exc

    def current_ref(self, agent_id: str | None = None) -> JsonObject:
        store = self._store_for(agent_id)
        current = store.current_commit_sha()
        if not current:
            raise AgentGovernanceError(409, "Agent Git repository is not initialized")
        return store.version_summary(current, reason="current")

    def change_set_diff(self, change_set: JsonObject, candidate: str) -> JsonObject | None:
        # 按 change_set 归属的 agent_id 路由到对应版本库（缺陷②：不再恒走主库）。
        return self._store_for(change_set.get("agent_id")).diff_versions(str(change_set["base_commit_sha"]), candidate)

    def change_set_file_diff(self, change_set: JsonObject, candidate: str, path: str) -> JsonObject | None:
        return self._store_for(change_set.get("agent_id")).diff_version_file(str(change_set["base_commit_sha"]), candidate, path)

    def list_change_sets(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(AgentChangeSetModel).order_by(AgentChangeSetModel.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(AgentChangeSetModel.status == status)
        if agent_id:
            stmt = stmt.where(AgentChangeSetModel.agent_id == agent_id)
        with self.feedback_store.Session() as db:
            return [self._change_set_to_payload(row) for row in db.scalars(stmt).all()]

    def get_change_set(self, change_set_id: str) -> JsonObject | None:
        if not change_set_id:
            return None
        with self.feedback_store.Session() as db:
            row = db.get(AgentChangeSetModel, change_set_id)
            return self._change_set_to_payload(row) if row else None

    def list_change_set_events(self, change_set_id: str) -> list[JsonObject]:
        with self.feedback_store.Session() as db:
            rows = db.scalars(
                select(AgentChangeSetEventModel)
                .where(AgentChangeSetEventModel.change_set_id == change_set_id)
                .order_by(AgentChangeSetEventModel.created_at.asc())
            ).all()
            return [event_to_payload(row) for row in rows]

    def create_change_set(
        self,
        *,
        execution_job_id: str | None = None,
        base_commit_sha: str | None = None,
        title: str | None = None,
        note: str | None = None,
        agent_id: str | None = None,
        operator: str = "runtime",
        change_set_id: str | None = None,
        source: ChangeSetSource | None = None,
    ) -> JsonObject:
        agent_id = self._normalize_agent_id(agent_id)
        try:
            provisioned_id = provision_change_set_under_maintenance(
                session_factory=self.feedback_store.Session,
                version_maintenance=self.version_maintenance,
                store_for=self._store_for,
                agent_id=agent_id,
                execution_job_id=execution_job_id,
                base_commit_sha=base_commit_sha,
                title=title,
                note=note,
                operator=operator,
                change_set_id=change_set_id,
                source=source,
            )
        except (AgentAdmissionError, AgentGitError, ChangeSetProvisionConflict) as exc:
            raise AgentGovernanceError(409, f"Failed to create Agent change set worktree: {exc}") from exc
        created = self.get_change_set(provisioned_id)
        if created is None:
            raise AgentGovernanceError(409, "Agent change set intent was not persisted")
        return created

    def mark_candidate_committed(
        self,
        change_set_id: str,
        *,
        candidate_commit_sha: str,
        execution_job_id: str | None,
        note: str | None = None,
        operator: str = "runtime",
    ) -> JsonObject:
        change_set = self.get_change_set(change_set_id)
        if not change_set:
            raise AgentGovernanceError(404, "Agent change set not found")
        bound_candidate = str(change_set.get("candidate_commit_sha") or "")
        if bound_candidate:
            if bound_candidate == candidate_commit_sha:
                return change_set
            if str(change_set.get("status") or "") in TERMINAL_CHANGE_SET_STATES | {"publishing"}:
                raise AgentGovernanceError(409, "Published or terminal Agent change set cannot bind a newer candidate commit")
        bound_execution = str(change_set.get("execution_job_id") or "")
        if execution_job_id and bound_execution and bound_execution != execution_job_id:
            raise AgentGovernanceError(409, "Agent change set belongs to a different execution")
        store = self._store_for(change_set.get("agent_id"))
        diff = store.diff_versions(change_set["base_commit_sha"], candidate_commit_sha) or {}
        fields = {
            "candidate_commit_sha": candidate_commit_sha,
            "execution_job_id": execution_job_id or change_set.get("execution_job_id"),
            "note": note or change_set.get("note"),
            "diff_summary": diff_summary(diff),
            "latest_test_run_id": None,
            "latest_test_run": None,
        }
        return self._transition_change_set(
            change_set_id,
            "candidate_committed",
            fields=fields,
            action="candidate_committed",
            operator=operator,
        )

    def request_change_set_approval(
        self,
        change_set_id: str,
        *,
        operator: str = "runtime",
        reason: str,
        impact_scope: str,
        rollback_plan: str,
    ) -> JsonObject:
        """把高风险变更标记为待审批：不经 approve 不得发布（AGV-041）。

        审批请求记录操作人、原因、影响范围和回滚方案，作为审批决策依据。
        """
        return self._transition_change_set(
            change_set_id,
            "pending_approval",
            fields={"approval_reason": reason, "impact_scope": impact_scope, "rollback_plan": rollback_plan},
            action="approval_requested",
            operator=operator,
        )

    def approve_change_set(self, change_set_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        return self._transition_change_set(change_set_id, "approved", fields={"approval_note": note}, action="approved", operator=operator)

    def reject_change_set(self, change_set_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        return self._transition_change_set(change_set_id, "rejected", fields={"rejection_note": note}, action="rejected", operator=operator)

    def abandon_change_set(self, change_set_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        change_set = self.get_change_set(change_set_id)
        if change_set is None:
            raise AgentGovernanceError(404, "Agent change set not found")
        agent_id = self._normalize_agent_id(str(change_set.get("agent_id") or ""))
        try:
            with self.version_maintenance.lease(
                agent_id=agent_id,
                kind="abandon",
                owner_id=f"{operator}:{change_set_id}",
            ) as lease:
                result = abandon_change_set_and_cleanup(
                    self,
                    change_set_id,
                    operator=operator,
                    note=note,
                    assert_maintenance_active=lease.assert_active,
                )
                lease.check()
                return result
        except AgentAdmissionError as exc:
            raise AgentGovernanceError(409, str(exc)) from exc

    def retry_worktree_cleanup(
        self,
        change_set_id: str,
        *,
        operator: str = "runtime",
        force: bool = True,
    ) -> JsonObject:
        change_set = self.get_change_set(change_set_id)
        if change_set is None:
            raise AgentGovernanceError(404, "Agent change set not found")
        agent_id = self._normalize_agent_id(str(change_set.get("agent_id") or ""))
        try:
            with self.version_maintenance.lease(
                agent_id=agent_id,
                kind="worktree_cleanup",
                owner_id=f"{operator}:{change_set_id}",
            ) as lease:
                result = execute_worktree_cleanup(
                    self,
                    change_set_id,
                    force=force,
                    assert_maintenance_active=lease.assert_active,
                )
                lease.check()
                return result
        except AgentAdmissionError as exc:
            raise AgentGovernanceError(409, str(exc)) from exc

    def reconcile_worktree_cleanups(self, *, limit: int = 100) -> JsonObject:
        return reconcile_worktree_cleanup_tasks(self, limit=limit)

    def reconcile_release_operations(self, *, limit: int = 100) -> JsonObject:
        return reconcile_release_operations(self, limit=limit)

    def publish_change_set(
        self,
        change_set_id: str,
        *,
        operator: str = "runtime",
        tag_name: str | None = None,
        note: str | None = None,
        force: bool = False,
    ) -> JsonObject:
        return publish_change_set(
            self,
            change_set_id,
            operator=operator,
            tag_name=tag_name,
            note=note,
            force=force,
        )

    def list_releases(self, *, status: str | None = None, agent_id: str | None = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(AgentReleaseModel).order_by(AgentReleaseModel.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(AgentReleaseModel.status == status)
        if agent_id:
            stmt = stmt.where(AgentReleaseModel.agent_id == agent_id)
        with self.feedback_store.Session() as db:
            return [release_to_payload(row) for row in db.scalars(stmt).all()]

    def get_release(self, release_id: str) -> JsonObject | None:
        with self.feedback_store.Session() as db:
            row = db.get(AgentReleaseModel, release_id)
            return release_to_payload(row) if row else None

    def rollback_release(self, release_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        return rollback_release(self, release_id, operator=operator, note=note)

    def restore_release(self, release_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        return restore_release(self, release_id, operator=operator, note=note)

    def _ref_policy_validator(self, store: GitAgentVersionStore, agent_id: str) -> Callable[[str], None]:
        return build_ref_policy_validator(
            store,
            agent_id,
            data_dir=self.feedback_store.data_dir,
            runtime_mode=self._runtime_mode,
            runtime_env=self._runtime_env,
        )

    def require_workspace_policy(self, workspace: Path, agent_id: str) -> None:
        data_dir = self.feedback_store.data_dir.resolve()
        runtime_root = Path("/") if data_dir == Path("/data") else data_dir.parent
        try:
            require_runtime_workspace_policy(
                workspace=workspace,
                agent_id=self._normalize_agent_id(agent_id),
                runtime_mode=self._runtime_mode,
                env=self._runtime_env,
                runtime_root=runtime_root,
            )
        except ManagedAgentPolicyError as exc:
            raise ConflictError(f"Managed Agent policy rejected workspace: {exc}") from exc

    def _transition_change_set(
        self,
        change_set_id: str,
        status: str,
        *,
        fields: JsonObject,
        action: str,
        operator: str,
        expected_fields: JsonObject | None = None,
        transaction_mutation: Callable[[object], None] | None = None,
    ) -> JsonObject:
        with self.feedback_store.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.get(AgentChangeSetModel, change_set_id)
            if not row:
                raise AgentGovernanceError(404, "Agent change set not found")
            validate_transition("agent_change_set", row.status, status)
            before = self._change_set_to_payload(row)
            previous_payload = dict(row.payload_json or {})
            if expected_fields and any(previous_payload.get(key) != value for key, value in expected_fields.items()):
                raise AgentGovernanceError(409, "Agent change set regression owner changed during transition")
            payload = dict(previous_payload)
            payload.update(fields)
            payload["status"] = status
            payload["updated_at"] = utc_now()
            changed = db.execute(
                update(AgentChangeSetModel)
                .where(
                    AgentChangeSetModel.change_set_id == change_set_id,
                    AgentChangeSetModel.status == row.status,
                    AgentChangeSetModel.updated_at == row.updated_at,
                    AgentChangeSetModel.payload_json == previous_payload,
                )
                .values(
                    status=status,
                    updated_at=payload["updated_at"],
                    execution_job_id=payload.get("execution_job_id"),
                    candidate_commit_sha=payload.get("candidate_commit_sha"),
                    payload_json=payload,
                )
            ).rowcount
            if changed != 1:
                raise AgentGovernanceError(409, "Agent change set changed during transition")
            if transaction_mutation is not None:
                transaction_mutation(db)
            db.expire_all()
            updated_row = db.get(AgentChangeSetModel, change_set_id)
            if updated_row is None:
                raise AgentGovernanceError(404, "Agent change set not found")
            after = self._change_set_to_payload(updated_row)
            self._add_event_row(db, change_set_id, action, operator, before=before, after=after)
        return self.get_change_set(change_set_id) or after

    def _transition_release(self, release_id: str, status: str, *, fields: JsonObject, operator: str) -> JsonObject:
        with self.feedback_store.Session.begin() as db:
            row = db.get(AgentReleaseModel, release_id, with_for_update=True)
            if not row:
                raise AgentGovernanceError(404, "Agent release not found")
            validate_transition("agent_release", row.status, status)
            payload = dict(row.payload_json or {})
            payload.update(fields)
            payload["status"] = status
            payload["updated_at"] = utc_now()
            payload["operator"] = operator
            row.status = status
            row.updated_at = payload["updated_at"]
            row.payload_json = payload
        return self.get_release(release_id) or payload

    def _published_release(self, change_set: JsonObject, *, requested_tag_name: str | None) -> JsonObject:
        with self.feedback_store.Session() as db:
            release_id = str(change_set.get("latest_release_id") or "")
            row = db.get(AgentReleaseModel, release_id) if release_id else None
            row = row or self._release_row_for_change_set(db, str(change_set["change_set_id"]))
            if row is None:
                raise AgentGovernanceError(409, "Published Agent change set has no release metadata")
            release = release_to_payload(row)
        if requested_tag_name and requested_tag_name != release["tag_name"]:
            raise AgentGovernanceError(409, "Agent change set was already published with a different tag")
        return release

    def _reserve_publication_intent(
        self,
        change_set_id: str,
        *,
        operator: str,
        tag_name: str | None,
        note: str | None,
        force: bool,
    ) -> PublicationIntent:
        with self.feedback_store.Session() as db:
            row = db.get(AgentChangeSetModel, change_set_id)
            if not row:
                raise AgentGovernanceError(404, "Agent change set not found")
            payload = self._change_set_to_payload(row)
            if row.status in {"publishing", "published"} and payload.get("publication_intent"):
                intent = self._parse_publication_intent(payload["publication_intent"])
                self._validate_publication_intent(row, intent, requested_tag_name=tag_name)
                validate_intent_provenance(db, intent)
                return intent
            source_revision = capture_publication_source(db, change_set_id)
            candidate = str(row.candidate_commit_sha or "")
            if not candidate:
                raise AgentGovernanceError(409, "Agent change set has no candidate commit")
            publication_blocker = self._publication_blocker_for_change_set(payload)
            self._validate_publication_start(
                row.status, publication_blocker=publication_blocker, force=force, feedback_managed=source_revision is not None
            )
            if force and not (note or "").strip():
                raise AgentGovernanceError(422, "Force publication requires an explicit reason")
            existing_release = self._release_row_for_change_set(db, change_set_id)
            if existing_release and existing_release.commit_sha != candidate:
                raise AgentGovernanceError(409, "Agent change set release metadata points to a different commit")
            if existing_release and tag_name and existing_release.tag_name != tag_name:
                raise AgentGovernanceError(409, "Agent change set already has release metadata for a different tag")
            now = utc_now()
            agent_id = self._normalize_agent_id(row.agent_id)
            previous_status = row.status
            previous_updated_at = row.updated_at
            intent = PublicationIntent(
                release_id=(
                    existing_release.release_id if existing_release else f"agr-{uuid.uuid5(uuid.NAMESPACE_URL, f'agentgov:{agent_id}:{change_set_id}')}"
                ),
                change_set_id=change_set_id,
                agent_id=agent_id,
                commit_sha=candidate,
                tag_name=tag_name or (existing_release.tag_name if existing_release else f"agent-release-{change_set_id}"),
                operator=operator,
                note=note,
                force=force,
                force_publication_blocker=publication_blocker if force else None,
                previous_status=previous_status,
                started_at=existing_release.created_at if existing_release else now,
                previous_commit_sha=str(row.base_commit_sha),
                source_improvement_id=source_revision.improvement_id if source_revision else None,
                source_improvement_updated_at=source_revision.updated_at if source_revision else None,
            )
            validate_transition("agent_change_set", previous_status, "publishing")
            before = dict(payload)
            after = {
                **payload,
                "status": "publishing",
                "updated_at": now,
                "publication_intent": intent.to_payload(),
                "publication_error": None,
            }
        try:
            commit_publication_intent(
                self.feedback_store.Session,
                intent=intent,
                previous_status=previous_status,
                previous_updated_at=previous_updated_at,
                before=before,
                after=after,
                add_event=self._add_event_row,
            )
        except (PublicationSourceConflict, PublicationTagConflict) as exc:
            raise AgentGovernanceError(409, str(exc)) from exc
        except PublicationReservationLost:
            return self._publication_intent_after_reservation_race(change_set_id, requested_tag_name=tag_name)
        return intent

    def _publication_intent_after_reservation_race(self, change_set_id: str, *, requested_tag_name: str | None) -> PublicationIntent:
        with self.feedback_store.Session() as db:
            row = db.get(AgentChangeSetModel, change_set_id)
            if not row:
                raise AgentGovernanceError(404, "Agent change set not found")
            payload = self._change_set_to_payload(row)
            if row.status not in {"publishing", "published"} or not payload.get("publication_intent"):
                raise AgentGovernanceError(409, "Agent change set changed while publication was being reserved")
            intent = self._parse_publication_intent(payload["publication_intent"])
            self._validate_publication_intent(row, intent, requested_tag_name=requested_tag_name)
            return intent

    def _finalize_publication(self, intent: PublicationIntent, *, archive: JsonObject) -> JsonObject:
        try:
            return self._finalize_publication_once(intent, archive=archive)
        except (IntegrityError, PublicationFinalizationLost) as exc:
            change_set = self.get_change_set(intent.change_set_id)
            if change_set and change_set["status"] == "published":
                release = self._published_release(change_set, requested_tag_name=intent.tag_name)
                return release
            raise AgentGovernanceError(
                409,
                "Agent Git publication completed, but release metadata is pending reconciliation; retry publish",
            ) from exc

    def _finalize_publication_once(self, intent: PublicationIntent, *, archive: JsonObject) -> JsonObject:
        return finalize_publication_once(self, intent, archive=archive)

    @staticmethod
    def _validate_publication_start(
        status: str,
        *,
        publication_blocker: str | None,
        force: bool,
        feedback_managed: bool,
    ) -> None:
        if force and feedback_managed:
            raise AgentGovernanceError(
                409,
                "反馈闭环待发布版本必须在精确候选提交上通过完整 Agent 测试集，不能强制绕过测试条件",
            )
        if publication_blocker and not force:
            raise AgentGovernanceError(409, publication_blocker)
        if force and status not in PUBLISHABLE_CHANGE_SET_STATES:
            raise AgentGovernanceError(409, f"Agent change set cannot be force-published from status {status}")
        if not force and status not in PUBLISHABLE_CHANGE_SET_STATES:
            raise AgentGovernanceError(409, f"Agent change set cannot be published from status {status}")

    def _validate_publication_intent(
        self,
        row: AgentChangeSetModel,
        intent: PublicationIntent,
        *,
        requested_tag_name: str | None,
    ) -> None:
        if intent.change_set_id != row.change_set_id or intent.commit_sha != row.candidate_commit_sha:
            raise AgentGovernanceError(409, "Agent publication intent no longer matches its change set")
        if intent.agent_id != self._normalize_agent_id(row.agent_id):
            raise AgentGovernanceError(409, "Agent publication intent has a different Agent owner")
        if requested_tag_name and requested_tag_name != intent.tag_name:
            raise AgentGovernanceError(409, "Agent change set is already publishing with a different tag")

    @staticmethod
    def _parse_publication_intent(value: object) -> PublicationIntent:
        try:
            return PublicationIntent.from_payload(value)
        except ValueError as exc:
            raise AgentGovernanceError(409, "Agent change set has an invalid publication intent") from exc

    @staticmethod
    def _release_row_for_change_set(db: object, change_set_id: str) -> AgentReleaseModel | None:
        rows = list(
            db.scalars(
                select(AgentReleaseModel).where(AgentReleaseModel.change_set_id == change_set_id).order_by(AgentReleaseModel.created_at.desc()).limit(2)
            ).all()
        )
        if len(rows) > 1:
            raise AgentGovernanceError(409, "Agent change set has multiple release records")
        return rows[0] if rows else None

    def _add_event_row(self, db: object, change_set_id: str, action: str, operator: str, *, before: JsonObject, after: JsonObject) -> None:
        now = utc_now()
        db.add(
            AgentChangeSetEventModel(
                event_id=f"age-{uuid.uuid4()}",
                change_set_id=change_set_id,
                action=action,
                operator=operator,
                created_at=now,
                before_json=before,
                after_json=after,
            )
        )

    def _change_set_to_payload(self, row: AgentChangeSetModel) -> JsonObject:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "change_set_id": row.change_set_id,
                "agent_id": row.agent_id or "main-agent",
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "execution_job_id": row.execution_job_id,
                "base_commit_sha": row.base_commit_sha,
                "candidate_commit_sha": row.candidate_commit_sha,
                "branch_name": row.branch_name,
                "worktree_path": row.worktree_path,
            }
        )
        payload = project_current_attribution(self.feedback_store.Session, payload)
        candidate = str(row.candidate_commit_sha or "")
        passed_run = self._matching_passed_test_run(agent_id=str(row.agent_id), commit_sha=candidate)
        payload["latest_test_run_id"] = passed_run.get("test_run_id") if passed_run else None
        payload["latest_test_run"] = passed_run
        payload["publication_blocker"] = payload.get("publication_provenance_blocker") or (
            None if passed_run else "待发布版本缺少 commit_sha 完全匹配且通过的平台测试运行记录。"
        )
        return payload

    def _matching_passed_test_run(self, *, agent_id: str, commit_sha: str) -> JsonObject | None:
        if not commit_sha or self.latest_passed_test_run is None:
            return None
        candidate = self.latest_passed_test_run(agent_id, commit_sha)
        if not isinstance(candidate, dict):
            return None
        if (
            str(candidate.get("agent_id") or "") != agent_id
            or str(candidate.get("commit_sha") or "") != commit_sha
            or str(candidate.get("status") or "") != "passed"
        ):
            return None
        return candidate

    def _publication_blocker_for_change_set(self, change_set: JsonObject) -> str | None:
        blocker = change_set.get("publication_blocker")
        return str(blocker) if blocker else None

    def change_set_worktree_path(self, change_set: JsonObject) -> Path:
        return Path(str(change_set.get("worktree_path") or ""))
