from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import (
    AgentChangeSetEventModel,
    AgentChangeSetModel,
    AgentReleaseModel,
    utc_now,
)
from app.runtime.state_machines import validate_transition
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_change_set_provisioner import ChangeSetProvisionConflict, ChangeSetSource, provision_change_set
from app.services.agent_change_set_worktree_lifecycle import abandon_change_set_and_cleanup, cleanup_published_change_set
from app.services.agent_publication import (
    PublicationFinalizationLost,
    PublicationIntent,
    PublicationReservationLost,
    PublicationTagConflict,
    capture_publication_source,
    commit_publication_intent,
    finalize_intent_source,
    reconcile_publication_failure,
    release_matches_intent,
    release_payload,
    validate_intent_provenance,
    validate_tag_claim,
)
from app.services.agent_publication_provenance import project_current_attribution
from app.services.agent_regression import AgentRegressionMixin

TERMINAL_CHANGE_SET_STATES = {"published", "rejected", "abandoned", "failed"}
# pending_approval 不可直接发布：高风险变更必须先经 approve_change_set 转为 approved（AGV-041）。
PUBLISHABLE_CHANGE_SET_STATES = {"candidate_committed", "approved", "regression_passed"}
REGRESSION_BLOCKING_STATUSES = {"blocked", "review_required", "failed", "needs_human_review"}
MAIN_AGENT_ID = "main-agent"


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


class AgentGovernanceService(AgentRegressionMixin):
    """Coordinates Git-backed Agent change sets, releases, and rollback."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        agent_version_store: GitAgentVersionStore,
    ) -> None:
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store
        # 多租户版本 store 注册表：main-agent 复用传入的主 store（行为不变），
        # 业务 Agent 各自懒初始化一套独立 git 版本链（B3.2/B3.3）。
        self._agent_stores: dict[str, GitAgentVersionStore] = {MAIN_AGENT_ID: agent_version_store}
        # 缺陷④：非 main 业务 Agent 必须在注册表中存在才允许建/取其版本库，杜绝幽灵 Agent。
        # 由 app 装配后注入（None 则不校验，便于单测）。
        self.agent_exists: Callable[[str], bool] | None = None

    def _normalize_agent_id(self, agent_id: str | None) -> str:
        normalized = (agent_id or MAIN_AGENT_ID).strip()
        if normalized == MAIN_AGENT_ID:
            return MAIN_AGENT_ID
        try:
            return validate_agent_id(normalized)
        except InvalidAgentId as exc:
            raise AgentGovernanceError(400, f"Invalid agent_id for version governance: {agent_id!r}") from exc

    def _store_for(self, agent_id: str | None) -> GitAgentVersionStore:
        """按 agent_id 选版本 store。

        main-agent 暂复用主 store（B 阶段并入统一模型）；业务 Agent 的版本库 root 在其
        **workspace**（与 main 同构：git 就地版本化配置），worktrees/releases 落
        ``data_dir/business-agents/{agent_id}/version/`` 兄弟目录，claude-root 因去嵌套
        在 workspace 之外、天然不进版本源。懒初始化并缓存，实现 per-agent 版本治理隔离。
        """
        normalized = self._normalize_agent_id(agent_id)
        existing = self._agent_stores.get(normalized)
        if existing is not None:
            return existing
        # 缺陷④：懒建版本库前校验该业务 Agent 在注册表中存在（main-agent 恒有效）。
        if normalized != MAIN_AGENT_ID and self.agent_exists is not None and not self.agent_exists(normalized):
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
            return [self._event_to_payload(row) for row in rows]

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
        store = self._store_for(agent_id)
        try:
            provisioned_id = provision_change_set(
                session_factory=self.feedback_store.Session,
                store=store,
                agent_id=agent_id,
                execution_job_id=execution_job_id,
                base_commit_sha=base_commit_sha,
                title=title,
                note=note,
                operator=operator,
                change_set_id=change_set_id,
                source=source,
            )
        except (AgentGitError, ChangeSetProvisionConflict) as exc:
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
            if bound_candidate != candidate_commit_sha:
                raise AgentGovernanceError(409, "Agent change set already owns a different candidate commit")
            return change_set
        bound_execution = str(change_set.get("execution_job_id") or "")
        if execution_job_id and bound_execution and bound_execution != execution_job_id:
            raise AgentGovernanceError(409, "Agent change set belongs to a different execution")
        store = self._store_for(change_set.get("agent_id"))
        diff = store.diff_versions(change_set["base_commit_sha"], candidate_commit_sha) or {}
        fields = {
            "candidate_commit_sha": candidate_commit_sha,
            "execution_job_id": execution_job_id or change_set.get("execution_job_id"),
            "note": note or change_set.get("note"),
            "diff_summary": self._diff_summary(diff),
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
        return abandon_change_set_and_cleanup(self, change_set_id, operator=operator, note=note)

    def publish_change_set(
        self,
        change_set_id: str,
        *,
        operator: str = "runtime",
        tag_name: str | None = None,
        note: str | None = None,
        force: bool = False,
    ) -> JsonObject:
        change_set = self.get_change_set(change_set_id)
        if not change_set:
            raise AgentGovernanceError(404, "Agent change set not found")
        if change_set["status"] == "published":
            return cleanup_published_change_set(self, change_set_id, self._published_release(change_set, requested_tag_name=tag_name))
        if tag_name and change_set["status"] != "publishing":
            candidate = str(change_set.get("candidate_commit_sha") or "")
            if not candidate:
                raise AgentGovernanceError(409, "Agent change set has no candidate commit")
            try:
                self._store_for(change_set.get("agent_id")).validate_publication_target(candidate, tag_name)
            except AgentGitError as exc:
                raise AgentGovernanceError(409, f"Agent publish preflight failed: {exc}") from exc
        intent = self._reserve_publication_intent(
            change_set_id,
            operator=operator,
            tag_name=tag_name,
            note=note,
            force=force,
        )
        store = self._store_for(intent.agent_id)
        try:
            result = store.publish_commit(
                intent.commit_sha,
                tag_name=intent.tag_name,
                message=intent.note or f"Publish {change_set_id}",
            )
        except AgentGitError as exc:
            cancelled = reconcile_publication_failure(
                self.feedback_store.Session,
                store,
                intent=intent,
                detail=str(exc),
                updated_at=utc_now(),
                add_event=self._add_event_row,
            )
            suffix = "; publication intent was cancelled before side effects" if cancelled else ""
            raise AgentGovernanceError(409, f"Agent publish failed: {exc}{suffix}") from exc
        archive = result.get("archive") if isinstance(result.get("archive"), dict) else {}
        try:
            return cleanup_published_change_set(self, change_set_id, self._finalize_publication(intent, archive=archive))
        except SQLAlchemyError as exc:
            raise AgentGovernanceError(
                409,
                "Agent Git publication completed, but release metadata is pending reconciliation; retry publish",
            ) from exc

    def list_releases(self, *, status: str | None = None, agent_id: str | None = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(AgentReleaseModel).order_by(AgentReleaseModel.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(AgentReleaseModel.status == status)
        if agent_id:
            stmt = stmt.where(AgentReleaseModel.agent_id == agent_id)
        with self.feedback_store.Session() as db:
            return [self._release_to_payload(row) for row in db.scalars(stmt).all()]

    def get_release(self, release_id: str) -> JsonObject | None:
        with self.feedback_store.Session() as db:
            row = db.get(AgentReleaseModel, release_id)
            return self._release_to_payload(row) if row else None

    def rollback_release(self, release_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        release = self.get_release(release_id)
        if not release:
            raise AgentGovernanceError(404, "Agent release not found")
        store = self._store_for(release.get("agent_id"))
        rollback_target = str(release.get("previous_commit_sha") or "")
        if not rollback_target:
            rollback_target = str(store.version_summary(str(release["commit_sha"]), reason="rollback_target").get("parent_version_id") or "")
        if not rollback_target:
            raise AgentGovernanceError(409, "Agent release has no previous commit to roll back to")
        try:
            result = store.rollback_to_ref(rollback_target)
        except AgentGitError as exc:
            self._transition_release(release_id, "rollback_failed", fields={"rollback_error": str(exc)}, operator=operator)
            raise AgentGovernanceError(409, f"Agent rollback failed: {exc}") from exc
        updated = self._transition_release(
            release_id,
            "rolled_back",
            fields={"rollback_result": result, "rollback_note": note, "rollback_target_commit_sha": rollback_target},
            operator=operator,
        )
        return updated

    def restore_release(self, release_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        release = self.get_release(release_id)
        if not release:
            raise AgentGovernanceError(404, "Agent release not found")
        store = self._store_for(release.get("agent_id"))
        try:
            result = store.rollback_to_ref(str(release["commit_sha"]))
        except AgentGitError as exc:
            raise AgentGovernanceError(409, f"Agent release restore failed: {exc}") from exc
        return {
            "schema_version": "agent-release-restore/v1",
            "release": self.get_release(release_id) or release,
            "restore_result": {
                **result,
                "operator": operator,
                "note": note,
            },
        }

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
            release = self._release_to_payload(row)
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
            self._validate_publication_start(row.status, publication_blocker=publication_blocker, force=force)
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
        except PublicationTagConflict as exc:
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
        now = utc_now()
        with self.feedback_store.Session() as db:
            row = db.get(AgentChangeSetModel, intent.change_set_id)
            if not row:
                raise AgentGovernanceError(404, "Agent change set not found")
            self._validate_publication_intent(row, intent, requested_tag_name=intent.tag_name)
            if row.status == "published":
                return self._published_release(self._change_set_to_payload(row), requested_tag_name=intent.tag_name)
            validate_transition("agent_change_set", row.status, "published")
            previous_updated_at = row.updated_at
            before = self._change_set_to_payload(row)
            release = release_payload(
                intent,
                archive=archive,
                created_at=intent.started_at,
                updated_at=now,
            )
            payload = {
                **dict(row.payload_json or {}),
                "status": "published",
                "updated_at": now,
                "latest_release_id": intent.release_id,
                "latest_release": release,
                "force_published": intent.force,
                "force_publication_blocker": intent.force_publication_blocker,
                "force_publish_note": intent.note if intent.force else None,
                "publication_error": None,
            }
        with self.feedback_store.Session.begin() as db:
            finalize_intent_source(db, intent, completed_at=now)
            finalized = db.execute(
                update(AgentChangeSetModel)
                .where(
                    AgentChangeSetModel.change_set_id == intent.change_set_id,
                    AgentChangeSetModel.status == "publishing",
                    AgentChangeSetModel.updated_at == previous_updated_at,
                    AgentChangeSetModel.candidate_commit_sha == intent.commit_sha,
                )
                .values(status="published", updated_at=now, payload_json=payload)
            ).rowcount
            if finalized != 1:
                raise PublicationFinalizationLost
            try:
                validate_tag_claim(db, intent)
            except PublicationTagConflict as exc:
                raise AgentGovernanceError(409, str(exc)) from exc
            release_row = db.get(AgentReleaseModel, intent.release_id)
            if release_row is None:
                release_row = AgentReleaseModel(
                    release_id=intent.release_id,
                    agent_id=intent.agent_id,
                    created_at=intent.started_at,
                    updated_at=now,
                    status="published",
                    tag_name=intent.tag_name,
                    commit_sha=intent.commit_sha,
                    change_set_id=intent.change_set_id,
                    archive_path=release.get("archive_path"),
                    payload_json=release,
                )
                db.add(release_row)
            elif not release_matches_intent(release_row, intent):
                raise AgentGovernanceError(409, "Existing Agent release metadata conflicts with publication intent")
            else:
                release_row.updated_at = now
                release_row.archive_path = release.get("archive_path")
                release_row.payload_json = release
            self._add_event_row(
                db,
                intent.change_set_id,
                "force_published" if intent.force else "published",
                intent.operator,
                before=before,
                after=payload,
            )
            db.flush()
        return release

    @staticmethod
    def _validate_publication_start(status: str, *, publication_blocker: str | None, force: bool) -> None:
        if publication_blocker and not force:
            raise AgentGovernanceError(409, publication_blocker)
        if force and status not in (PUBLISHABLE_CHANGE_SET_STATES | {"regression_failed"}):
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
        payload["publication_blocker"] = self._eval_run_publication_blocker(payload.get("latest_eval_run")) or payload.get("publication_provenance_blocker")
        return payload

    def _publication_blocker_for_change_set(self, change_set: JsonObject) -> str | None:
        blocker = change_set.get("publication_blocker")
        return str(blocker) if blocker else self._eval_run_publication_blocker(change_set.get("latest_eval_run"))

    @staticmethod
    def _eval_run_publication_blocker(eval_run: object) -> str | None:
        if not isinstance(eval_run, dict):
            return None
        failed_case_ids = [
            str(item.get("eval_case_id"))
            for item in eval_run.get("items") or []
            if isinstance(item, dict) and item.get("eval_case_id") and str(item.get("status") or "") in {"failed", "needs_human_review"}
        ]
        summary = eval_run.get("summary") if isinstance(eval_run.get("summary"), dict) else {}
        summary_failed = _safe_int(summary.get("failed")) + _safe_int(summary.get("needs_human_review"))
        gate_result = eval_run.get("gate_result") if isinstance(eval_run.get("gate_result"), dict) else {}
        status = str(eval_run.get("result_status") or gate_result.get("status") or "")
        if not failed_case_ids and summary_failed <= 0 and status not in REGRESSION_BLOCKING_STATUSES:
            return None
        failed_count = len(failed_case_ids) or summary_failed
        detail = f"{failed_count} 条用例失败" if failed_count else f"状态 {status}"
        return f"回归验证存在失败用例（{detail}），禁止发布。请修复后重新运行回归并确认通过。"

    def _event_to_payload(self, row: AgentChangeSetEventModel) -> JsonObject:
        return {
            "event_id": row.event_id,
            "change_set_id": row.change_set_id,
            "action": row.action,
            "operator": row.operator,
            "created_at": row.created_at,
            "before": row.before_json or {},
            "after": row.after_json or {},
        }

    def _release_to_payload(self, row: AgentReleaseModel) -> JsonObject:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "release_id": row.release_id,
                "agent_id": row.agent_id or "main-agent",
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "tag_name": row.tag_name,
                "commit_sha": row.commit_sha,
                "change_set_id": row.change_set_id,
                "rollback_of_release_id": row.rollback_of_release_id,
                "archive_path": row.archive_path,
            }
        )
        return payload

    def _diff_summary(self, diff: JsonObject) -> JsonObject:
        return {
            "added": len(diff.get("added") or []),
            "modified": len(diff.get("modified") or []),
            "deleted": len(diff.get("deleted") or []),
        }

    def change_set_worktree_path(self, change_set: JsonObject) -> Path:
        return Path(str(change_set.get("worktree_path") or ""))


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
