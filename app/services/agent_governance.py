from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from sqlalchemy import select

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.errors import ConflictError, FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.managed_agent_policy import ManagedAgentPolicyError, require_runtime_workspace_policy
from app.runtime.runtime_db import (
    AgentChangeSetEventModel,
    AgentChangeSetModel,
    AgentReleaseModel,
    utc_now,
)
from app.runtime.state_machines import validate_transition
from app.runtime.stores.feedback_store import FeedbackStore

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
        # 多租户版本 store 注册表：main-agent 复用传入的主 store（行为不变），
        # 业务 Agent 各自懒初始化一套独立 git 版本链（B3.2/B3.3）。
        self._agent_stores: dict[str, GitAgentVersionStore] = {MAIN_AGENT_ID: agent_version_store}
        self._runtime_mode = runtime_mode
        self._runtime_env = dict(runtime_env or os.environ)
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
    ) -> JsonObject:
        agent_id = self._normalize_agent_id(agent_id)
        store = self._store_for(agent_id)
        base_commit_sha = base_commit_sha or store.current_commit_sha()
        if not base_commit_sha:
            raise AgentGovernanceError(409, "Agent Git repository has no base commit")
        change_set_id = f"agc-{uuid.uuid4()}"
        try:
            worktree = store.create_worktree(change_set_id, base_ref=base_commit_sha)
        except AgentGitError as exc:
            raise AgentGovernanceError(409, f"Failed to create Agent change set worktree: {exc}") from exc
        now = utc_now()
        payload = {
            "schema_version": "agent-change-set/v1",
            "change_set_id": change_set_id,
            "agent_id": agent_id,
            "created_at": now,
            "updated_at": now,
            "status": "draft",
            "execution_job_id": execution_job_id,
            "base_commit_sha": worktree.base_commit_sha,
            "candidate_commit_sha": None,
            "branch_name": worktree.branch_name,
            "worktree_path": str(worktree.worktree_path),
            "title": title,
            "note": note,
            "diff_summary": {},
            "latest_eval_run_id": None,
            "latest_release_id": None,
        }
        with self.feedback_store.Session.begin() as db:
            row = AgentChangeSetModel(
                change_set_id=change_set_id,
                agent_id=agent_id,
                created_at=now,
                updated_at=now,
                status="draft",
                execution_job_id=execution_job_id,
                base_commit_sha=worktree.base_commit_sha,
                candidate_commit_sha=None,
                branch_name=worktree.branch_name,
                worktree_path=str(worktree.worktree_path),
                payload_json=payload,
            )
            db.add(row)
            db.flush()
            self._add_event_row(db, change_set_id, "created", operator, before={}, after=payload)
        return self.get_change_set(change_set_id) or payload

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
        return self._transition_change_set(change_set_id, "abandoned", fields={"abandon_note": note}, action="abandoned", operator=operator)

    def mark_regression_running(self, change_set_id: str, *, eval_run_id: str, operator: str = "runtime") -> JsonObject:
        return self._transition_change_set(
            change_set_id,
            "regression_running",
            fields={"latest_eval_run_id": eval_run_id},
            action="regression_running",
            operator=operator,
        )

    def complete_regression(self, change_set_id: str, *, eval_run: JsonObject, operator: str = "runtime") -> JsonObject:
        result_status = str(eval_run.get("result_status") or "")
        target = (
            "regression_passed"
            if result_status in {"passed", "passed_with_notes"} and not self._eval_run_publication_blocker(eval_run)
            else "regression_failed"
        )
        return self._transition_change_set(
            change_set_id,
            target,
            fields={"latest_eval_run_id": eval_run.get("eval_run_id"), "latest_eval_run": eval_run},
            action=target,
            operator=operator,
        )

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
        if not change_set.get("candidate_commit_sha"):
            raise AgentGovernanceError(409, "Agent change set has no candidate commit")
        status = str(change_set["status"])
        publication_blocker = self._publication_blocker_for_change_set(change_set)
        if publication_blocker and not force:
            raise AgentGovernanceError(409, publication_blocker)
        if force and status not in (PUBLISHABLE_CHANGE_SET_STATES | {"regression_failed"}):
            raise AgentGovernanceError(409, f"Agent change set cannot be force-published from status {status}")
        if not force and status not in PUBLISHABLE_CHANGE_SET_STATES:
            raise AgentGovernanceError(409, f"Agent change set cannot be published from status {status}")
        agent_id = self._normalize_agent_id(change_set.get("agent_id"))
        store = self._store_for(agent_id)
        tag_name = tag_name or f"agent-release-{utc_now().replace(':', '').replace('+', 'Z')}-{change_set_id[-8:]}"
        try:
            result = store.publish_commit(
                str(change_set["candidate_commit_sha"]),
                tag_name=tag_name,
                message=note or f"Publish {change_set_id}",
                validate_ref=self._ref_policy_validator(store, agent_id),
            )
        except AgentGitError as exc:
            raise AgentGovernanceError(409, f"Agent publish failed: {exc}") from exc
        release = self._create_release(
            change_set_id=change_set_id,
            agent_id=agent_id,
            tag_name=tag_name,
            commit_sha=str(result["published_commit_sha"]),
            archive=result.get("archive") if isinstance(result.get("archive"), dict) else {},
            note=note,
            operator=operator,
        )
        updated = self._transition_change_set(
            change_set_id,
            "published",
            fields={
                "latest_release_id": release["release_id"],
                "latest_release": release,
                "force_published": force,
                "force_publication_blocker": publication_blocker if force else None,
                "force_publish_note": note if force else None,
            },
            action="force_published" if force else "published",
            operator=operator,
        )
        release["change_set"] = updated
        return release

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
        agent_id = self._normalize_agent_id(release.get("agent_id"))
        store = self._store_for(agent_id)
        try:
            result = store.rollback_to_ref(
                str(release["commit_sha"]),
                validate_ref=self._ref_policy_validator(store, agent_id),
            )
        except AgentGitError as exc:
            self._transition_release(release_id, "rollback_failed", fields={"rollback_error": str(exc)}, operator=operator)
            raise AgentGovernanceError(409, f"Agent rollback failed: {exc}") from exc
        updated = self._transition_release(
            release_id,
            "rolled_back",
            fields={"rollback_result": result, "rollback_note": note},
            operator=operator,
        )
        return updated

    def restore_release(self, release_id: str, *, operator: str = "runtime", note: str | None = None) -> JsonObject:
        release = self.get_release(release_id)
        if not release:
            raise AgentGovernanceError(404, "Agent release not found")
        agent_id = self._normalize_agent_id(release.get("agent_id"))
        store = self._store_for(agent_id)
        try:
            result = store.rollback_to_ref(
                str(release["commit_sha"]),
                validate_ref=self._ref_policy_validator(store, agent_id),
            )
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

    def _ref_policy_validator(self, store: GitAgentVersionStore, agent_id: str) -> Callable[[str], None]:
        managed_paths = [".claude/settings.json", ".mcp.json"]
        if agent_id == "security-operations-expert":
            managed_paths.extend(
                [
                    "CLAUDE.md",
                    "agent.yaml",
                    ".claude/skills/threat-response-disposition/SKILL.md",
                ]
            )

        def validate(ref: str) -> None:
            try:
                with tempfile.TemporaryDirectory(prefix=f"agentgov-policy-{agent_id}-") as temporary:
                    workspace = Path(temporary)
                    for relative in managed_paths:
                        content = store.read_text_at_ref(ref, relative)
                        if content is None:
                            continue
                        target = workspace / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8")
                    data_dir = self.feedback_store.data_dir.resolve()
                    runtime_root = Path("/") if data_dir == Path("/data") else data_dir.parent
                    require_runtime_workspace_policy(
                        workspace=workspace,
                        agent_id=agent_id,
                        runtime_mode=self._runtime_mode,
                        env=self._runtime_env,
                        runtime_root=runtime_root,
                    )
            except ManagedAgentPolicyError as exc:
                raise AgentGitError(f"Managed Agent policy rejected ref {ref}: {exc}") from exc

        return validate

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
    ) -> JsonObject:
        with self.feedback_store.Session.begin() as db:
            row = db.get(AgentChangeSetModel, change_set_id, with_for_update=True)
            if not row:
                raise AgentGovernanceError(404, "Agent change set not found")
            validate_transition("agent_change_set", row.status, status)
            before = self._change_set_to_payload(row)
            payload = dict(row.payload_json or {})
            payload.update(fields)
            payload["status"] = status
            payload["updated_at"] = utc_now()
            row.status = status
            row.updated_at = payload["updated_at"]
            row.execution_job_id = payload.get("execution_job_id")
            row.candidate_commit_sha = payload.get("candidate_commit_sha")
            row.payload_json = payload
            after = self._change_set_to_payload(row)
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

    def _create_release(
        self,
        *,
        change_set_id: str,
        tag_name: str,
        commit_sha: str,
        archive: JsonObject,
        note: str | None,
        operator: str,
        agent_id: str = MAIN_AGENT_ID,
    ) -> JsonObject:
        now = utc_now()
        release_id = f"agr-{uuid.uuid4()}"
        payload = {
            "schema_version": "agent-release/v1",
            "release_id": release_id,
            "agent_id": agent_id,
            "created_at": now,
            "updated_at": now,
            "status": "published",
            "tag_name": tag_name,
            "commit_sha": commit_sha,
            "change_set_id": change_set_id,
            "rollback_of_release_id": None,
            "archive_path": archive.get("archive_path"),
            "archive_sha256": archive.get("sha256"),
            "note": note,
            "operator": operator,
        }
        with self.feedback_store.Session.begin() as db:
            db.add(
                AgentReleaseModel(
                    release_id=release_id,
                    agent_id=agent_id,
                    created_at=now,
                    updated_at=now,
                    status="published",
                    tag_name=tag_name,
                    commit_sha=commit_sha,
                    change_set_id=change_set_id,
                    rollback_of_release_id=None,
                    archive_path=payload.get("archive_path"),
                    payload_json=payload,
                )
            )
        return self.get_release(release_id) or payload

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
        payload["publication_blocker"] = self._eval_run_publication_blocker(payload.get("latest_eval_run"))
        return payload

    def _publication_blocker_for_change_set(self, change_set: JsonObject) -> str | None:
        return self._eval_run_publication_blocker(change_set.get("latest_eval_run"))

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
