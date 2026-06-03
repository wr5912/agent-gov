from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from app.runtime.agent_git_store import AgentGitError, GitAgentVersionStore
from app.runtime.errors import FeedbackStoreError
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetEventModel, AgentChangeSetModel, AgentReleaseModel, OptimizationTaskModel, utc_now
from app.runtime.state_machines import validate_transition
from app.runtime.stores.feedback_store import FeedbackStore


TERMINAL_CHANGE_SET_STATES = {"published", "rejected", "abandoned", "failed"}


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
    ) -> None:
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store

    def repository_status(self) -> JsonObject:
        return self.agent_version_store.repository_status()

    def current_ref(self) -> JsonObject:
        current = self.agent_version_store.current_commit_sha()
        if not current:
            raise AgentGovernanceError(409, "Agent Git repository is not initialized")
        return self.agent_version_store.version_summary(current, reason="current")

    def list_change_sets(self, *, status: str | None = None, optimization_task_id: str | None = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(AgentChangeSetModel).order_by(AgentChangeSetModel.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(AgentChangeSetModel.status == status)
        if optimization_task_id:
            stmt = stmt.where(AgentChangeSetModel.optimization_task_id == optimization_task_id)
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
        optimization_task_id: str | None = None,
        execution_job_id: str | None = None,
        base_commit_sha: str | None = None,
        title: str | None = None,
        note: str | None = None,
        operator: str = "runtime",
    ) -> JsonObject:
        if optimization_task_id:
            existing = self.latest_active_change_set_for_task(optimization_task_id)
            if existing:
                return existing
            task = self.feedback_store.find_task(optimization_task_id)
            if not task:
                raise AgentGovernanceError(404, "Optimization task not found")
            base_commit_sha = base_commit_sha or str(task.get("baseline_agent_version_id") or "")
            title = title or str(task.get("proposal_id") or optimization_task_id)
        base_commit_sha = base_commit_sha or self.agent_version_store.current_commit_sha()
        if not base_commit_sha:
            raise AgentGovernanceError(409, "Agent Git repository has no base commit")
        change_set_id = f"agc-{uuid.uuid4()}"
        try:
            worktree = self.agent_version_store.create_worktree(change_set_id, base_ref=base_commit_sha)
        except AgentGitError as exc:
            raise AgentGovernanceError(409, f"Failed to create Agent change set worktree: {exc}") from exc
        now = utc_now()
        payload = {
            "schema_version": "agent-change-set/v1",
            "change_set_id": change_set_id,
            "created_at": now,
            "updated_at": now,
            "status": "draft",
            "optimization_task_id": optimization_task_id,
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
                created_at=now,
                updated_at=now,
                status="draft",
                optimization_task_id=optimization_task_id,
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
            if optimization_task_id:
                self.feedback_store._update_task_payload_row(
                    db,
                    optimization_task_id,
                    status="execution_ready",
                    fields={
                        "latest_change_set_id": change_set_id,
                        "latest_change_set": payload,
                    },
                )
        return self.get_change_set(change_set_id) or payload

    def latest_active_change_set_for_task(self, optimization_task_id: str) -> JsonObject | None:
        with self.feedback_store.Session() as db:
            rows = db.scalars(
                select(AgentChangeSetModel)
                .where(AgentChangeSetModel.optimization_task_id == optimization_task_id)
                .order_by(AgentChangeSetModel.created_at.desc())
            ).all()
            for row in rows:
                if row.status not in TERMINAL_CHANGE_SET_STATES:
                    return self._change_set_to_payload(row)
        return None

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
        diff = self.agent_version_store.diff_versions(change_set["base_commit_sha"], candidate_commit_sha) or {}
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
        target = "regression_passed" if result_status in {"passed", "passed_with_notes"} else "regression_failed"
        return self._transition_change_set(
            change_set_id,
            target,
            fields={"latest_eval_run_id": eval_run.get("eval_run_id"), "latest_eval_run": eval_run},
            action=target,
            operator=operator,
        )

    def publish_change_set(self, change_set_id: str, *, operator: str = "runtime", tag_name: str | None = None, note: str | None = None) -> JsonObject:
        change_set = self.get_change_set(change_set_id)
        if not change_set:
            raise AgentGovernanceError(404, "Agent change set not found")
        if not change_set.get("candidate_commit_sha"):
            raise AgentGovernanceError(409, "Agent change set has no candidate commit")
        if change_set["status"] not in {"approved", "regression_passed"}:
            raise AgentGovernanceError(409, "Agent change set must be approved or pass regression before publish")
        tag_name = tag_name or f"agent-release-{utc_now().replace(':', '').replace('+', 'Z')}-{change_set_id[-8:]}"
        try:
            result = self.agent_version_store.publish_commit(str(change_set["candidate_commit_sha"]), tag_name=tag_name, message=note or f"Publish {change_set_id}")
        except AgentGitError as exc:
            raise AgentGovernanceError(409, f"Agent publish failed: {exc}") from exc
        release = self._create_release(
            change_set_id=change_set_id,
            tag_name=tag_name,
            commit_sha=str(result["published_commit_sha"]),
            archive=result.get("archive") if isinstance(result.get("archive"), dict) else {},
            note=note,
            operator=operator,
        )
        updated = self._transition_change_set(
            change_set_id,
            "published",
            fields={"latest_release_id": release["release_id"], "latest_release": release},
            action="published",
            operator=operator,
        )
        release["change_set"] = updated
        return release

    def list_releases(self, *, status: str | None = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(AgentReleaseModel).order_by(AgentReleaseModel.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(AgentReleaseModel.status == status)
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
        try:
            result = self.agent_version_store.rollback_to_ref(str(release["commit_sha"]))
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
            task_id = after.get("optimization_task_id")
            if isinstance(task_id, str) and task_id:
                next_task_status = self._task_status_for_change_set(after)
                task_row = db.get(OptimizationTaskModel, task_id)
                if task_row and task_row.status == "completed" and next_task_status != "completed":
                    next_task_status = "completed"
                self.feedback_store._update_task_payload_row(
                    db,
                    task_id,
                    status=next_task_status,
                    fields={
                        "latest_change_set_id": change_set_id,
                        "latest_change_set": after,
                        "candidate_commit_sha": after.get("candidate_commit_sha"),
                    },
                )
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
    ) -> JsonObject:
        now = utc_now()
        release_id = f"agr-{uuid.uuid4()}"
        payload = {
            "schema_version": "agent-release/v1",
            "release_id": release_id,
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
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "optimization_task_id": row.optimization_task_id,
                "execution_job_id": row.execution_job_id,
                "base_commit_sha": row.base_commit_sha,
                "candidate_commit_sha": row.candidate_commit_sha,
                "branch_name": row.branch_name,
                "worktree_path": row.worktree_path,
            }
        )
        return payload

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

    def _task_status_for_change_set(self, change_set: JsonObject) -> str:
        status = str(change_set.get("status") or "")
        if status in {"draft", "execution_ready"}:
            return "execution_ready"
        if status in {"candidate_committed", "pending_approval", "approved"}:
            return "applied_pending_regression"
        if status == "regression_running":
            return "regression_running"
        if status == "regression_passed":
            return "regression_passed"
        if status in {"regression_failed", "rejected"}:
            return "regression_failed"
        if status == "published":
            return "completed"
        if status == "failed":
            return "failed"
        return "needs_human_review"

    def change_set_worktree_path(self, change_set: JsonObject) -> Path:
        return Path(str(change_set.get("worktree_path") or ""))
