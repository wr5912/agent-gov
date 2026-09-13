from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.runtime.agent_admission import AgentAdmissionError
from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.errors import ConflictError
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
from app.services.agent_candidate_approval import (
    CANDIDATE_EVIDENCE_EPOCH,
    CandidateApprovalFailure,
    approve_candidate_change_set,
)
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
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_governance_projections import (
    diff_summary,
    event_to_payload,
    manual_approval_paths,
    release_to_payload,
)
from app.services.agent_publication import PublicationFinalizationLost, PublicationIntent
from app.services.agent_publication_finalization import finalize_publication_once
from app.services.agent_publication_projection import project_change_set_publication_state
from app.services.agent_publication_provenance import project_current_attribution
from app.services.agent_publication_reservation import PublicationRequest, reserve_publication_intent
from app.services.agent_publication_validation import (
    complete_internal_publication_arguments,
    require_publication_intent_evidence,
)
from app.services.agent_ref_policy import build_ref_policy_validator
from app.services.agent_release_activation_workflow import publish_change_set
from app.services.agent_version_maintenance import AgentVersionMaintenanceCoordinator

TERMINAL_CHANGE_SET_STATES = {"published", "rejected", "abandoned", "failed"}
PUBLISHABLE_CHANGE_SET_STATES = {"candidate_committed", "approved"}


class AgentGovernanceService:
    """Coordinates Git-backed Agent change sets and releases."""

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
        # 每个业务 Agent 独立缓存版本链；不可预置可删除的 main-agent，避免留下悬空 store。
        self._agent_stores: dict[str, GitAgentVersionStore] = {}
        self._runtime_mode = runtime_mode
        self._runtime_env = dict(runtime_env or os.environ)
        # 业务 Agent 必须在注册表中存在才允许建/取其版本库，杜绝幽灵 Agent。
        self.agent_exists: Callable[[str], bool] | None = None
        self.latest_passed_test_run: Callable[..., JsonObject | None] | None = None
        self.latest_candidate_test_run: Callable[..., JsonObject | None] | None = None
        self.test_run_by_id: Callable[[str], JsonObject | None] | None = None
        self.release_activator: Callable[..., object] | None = None
        self.release_activation_committer: Callable[..., object] | None = None
        self.release_activation_compensator: Callable[..., object] | None = None

    def evict_agent_store(self, agent_id: str) -> None:
        """丢弃某 Agent 的版本 store 缓存。

        删除 Agent 后必须调用，避免同 id 重建时命中已被清理的 repository_dir。
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
        worktrees/releases 落 ``data_dir/business-agents/{agent_id}/version/`` 兄弟目录；
        AgentScope 的 Session Workspace 由独立 Runtime 管理，不进入该版本库。懒初始化并缓存。
        """
        normalized = self._normalize_agent_id(agent_id)
        existing = self._agent_stores.get(normalized)
        if existing is not None:
            return existing
        # 懒建前校验注册表；可删除的 main-agent 同样不得被版本操作隐式重建。
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

    def _store_for_read_only(self, agent_id: str | None) -> GitAgentVersionStore:
        """构造不 bootstrap、不创建目录的 Runtime 查询句柄。"""
        normalized = self._normalize_agent_id(agent_id)
        existing = self._agent_stores.get(normalized)
        if existing is not None:
            return existing
        if self.agent_exists is not None and not self.agent_exists(normalized):
            raise AgentGovernanceError(404, f"Agent not registered for version governance: {normalized}")
        layout = business_agent_layout(self.feedback_store.data_dir, normalized)
        return GitAgentVersionStore(
            repository_dir=layout.workspace,
            worktrees_dir=layout.version_base / "worktrees",
            releases_dir=layout.version_base / "releases",
            repository_name=f"{normalized}-config",
            create_directories=False,
        )

    def current_agent_version_id(self, agent_id: str | None = None) -> str | None:
        return self._store_for(agent_id or DEFAULT_BUSINESS_AGENT_ID).current_version_id()

    def repository_status(self, agent_id: str | None = None) -> JsonObject:
        return self._store_for(agent_id).repository_status()

    def current_ref(self, agent_id: str | None = None) -> JsonObject:
        store = self._store_for(agent_id)
        current = store.current_commit_sha()
        if not current:
            raise AgentGovernanceError(409, "Agent Git repository is not initialized")
        return store.version_summary(current, reason="current")

    def change_set_diff(self, change_set: JsonObject, candidate: str) -> JsonObject | None:
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
        diff = store.diff_versions(change_set["base_commit_sha"], candidate_commit_sha)
        if diff is None:
            raise AgentGovernanceError(409, "Unable to inspect candidate paths for mandatory approval")
        try:
            sensitive_paths = manual_approval_paths(diff)
        except ValueError as exc:
            raise AgentGovernanceError(409, str(exc)) from exc
        fields = {
            "candidate_commit_sha": candidate_commit_sha,
            "execution_job_id": execution_job_id or change_set.get("execution_job_id"),
            "note": note or change_set.get("note"),
            "diff_summary": diff_summary(diff),
            "latest_test_run_id": None,
            "latest_test_run": None,
            "approval_note": None,
            "approval_evidence": None,
            "candidate_evidence_epoch": CANDIDATE_EVIDENCE_EPOCH,
            "evidence_not_before": None,
        }
        if sensitive_paths:
            fields.update(
                {
                    "approval_reason": "Sensitive Harness paths changed: " + ", ".join(sensitive_paths),
                    "impact_scope": "Agent instructions, skills, MCP, manifest, or subagent execution boundary",
                    "rollback_plan": f"Restore base commit {change_set['base_commit_sha']}",
                },
            )
        return self._transition_change_set(
            change_set_id,
            "pending_approval" if sensitive_paths else "candidate_committed",
            fields=fields,
            action="approval_requested" if sensitive_paths else "candidate_committed",
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
            fields={
                "approval_reason": reason,
                "impact_scope": impact_scope,
                "rollback_plan": rollback_plan,
                "approval_note": None,
                "approval_evidence": None,
                "candidate_evidence_epoch": CANDIDATE_EVIDENCE_EPOCH,
                "evidence_not_before": None,
            },
            action="approval_requested",
            operator=operator,
        )

    def approve_change_set(
        self,
        change_set_id: str,
        *,
        candidate_commit_sha: str,
        diff_digest: str,
        test_run_id: str,
        suite_digest: str,
        reviewed_files: list[JsonObject],
        operator: str = "runtime",
        note: str | None = None,
    ) -> JsonObject:
        try:
            return approve_candidate_change_set(
                self,
                change_set_id,
                operator=operator,
                note=note,
                candidate_commit_sha=candidate_commit_sha,
                diff_digest=diff_digest,
                test_run_id=test_run_id,
                suite_digest=suite_digest,
                reviewed_files=reviewed_files,
            )
        except CandidateApprovalFailure as exc:
            raise AgentGovernanceError(exc.status_code, exc.detail) from exc

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

    async def publish_change_set_async(
        self,
        change_set_id: str,
        *,
        operator: str = "runtime",
        tag_name: str | None = None,
        note: str | None = None,
        force: bool = False,
        expected_candidate_commit_sha: str,
        expected_diff_digest: str,
        expected_test_run_id: str | None = None,
        expected_suite_digest: str | None = None,
    ) -> JsonObject:
        return await publish_change_set(
            self,
            change_set_id,
            operator=operator,
            tag_name=tag_name,
            note=note,
            force=force,
            expected_candidate_commit_sha=expected_candidate_commit_sha,
            expected_diff_digest=expected_diff_digest,
            expected_test_run_id=expected_test_run_id,
            expected_suite_digest=expected_suite_digest,
        )

    def publish_change_set(self, change_set_id: str, **kwargs: object) -> JsonObject:
        """同步 application-service 入口；HTTP 路由使用同一实现的 async 入口。"""
        kwargs = complete_internal_publication_arguments(self, self.feedback_store.Session, change_set_id, kwargs)
        return asyncio.run(self.publish_change_set_async(change_set_id, **kwargs))

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
        transaction_mutation: Callable[[Session], None] | None = None,
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
        expected_candidate_commit_sha: str,
        expected_diff_digest: str,
        expected_test_run_id: str | None,
        expected_suite_digest: str | None,
    ) -> PublicationIntent:
        request = PublicationRequest(
            change_set_id=change_set_id,
            operator=operator,
            tag_name=tag_name,
            note=note,
            force=force,
            expected_candidate_commit_sha=expected_candidate_commit_sha,
            expected_diff_digest=expected_diff_digest,
            expected_test_run_id=expected_test_run_id,
            expected_suite_digest=expected_suite_digest,
        )
        return reserve_publication_intent(self, request)

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
        if force and not publication_blocker:
            raise AgentGovernanceError(409, "Force publication requires an explicit, bypassable platform test blocker")
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
        db: Session | None = None,
    ) -> None:
        if intent.change_set_id != row.change_set_id or intent.commit_sha != row.candidate_commit_sha:
            raise AgentGovernanceError(409, "Agent publication intent no longer matches its change set")
        if intent.agent_id != self._normalize_agent_id(row.agent_id):
            raise AgentGovernanceError(409, "Agent publication intent has a different Agent owner")
        if requested_tag_name and requested_tag_name != intent.tag_name:
            state = "published" if row.status == "published" else "publishing"
            raise AgentGovernanceError(409, f"Agent change set is already {state} with a different tag")
        if db is None:
            with self.feedback_store.Session() as evidence_db:
                require_publication_intent_evidence(
                    evidence_db,
                    row=row,
                    intent=intent,
                    store=self._store_for(row.agent_id),
                )
        else:
            require_publication_intent_evidence(
                db,
                row=row,
                intent=intent,
                store=self._store_for(row.agent_id),
            )

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
        return project_change_set_publication_state(
            row,
            payload,
            test_run_by_id=self.test_run_by_id,
            latest_candidate_test_run=self.latest_candidate_test_run,
        )

    def change_set_worktree_path(self, change_set: JsonObject) -> Path:
        return Path(str(change_set.get("worktree_path") or ""))
