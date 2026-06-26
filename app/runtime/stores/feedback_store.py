from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from sqlalchemy import delete, select

from ...version import APP_VERSION
from ..agent_paths import business_agent_layout
from ..collection_utils import unique_strings
from ..execution_targets import WorkspaceExecutionTargetPolicy
from ..feedback_privacy import SENSITIVE_KEY_PARTS
from ..integrations.external_governance import ExternalGovernanceService
from ..json_types import JsonObject
from ..records.optimization_task_records import OptimizationTaskRecord
from ..runtime_db import (
    AgentJobModel,
    ExecutionApplicationModel,
    ExternalGovernanceItemModel,
    ExternalNotificationModel,
    OptimizationProposalModel,
    OptimizationTaskModel,
    ProposalReviewModel,
    make_session_factory,
    runtime_db_path_from_data_dir,
)
from ..state_machines import JOB_IN_PROGRESS_STATES
from .agent_job_queue_store import AgentJobQueueStoreMixin
from .agent_job_store import AgentJobStoreMixin
from .feedback_batch_eval_case_governance_store import FeedbackBatchEvalCaseGovernanceStoreMixin
from .feedback_batch_execution_store import FeedbackBatchExecutionStoreMixin
from .feedback_batch_plan_store import FeedbackBatchPlanStoreMixin
from .feedback_batch_store import FeedbackBatchStoreMixin
from .feedback_case_store import FeedbackCaseStoreMixin
from .feedback_compensation_store import FeedbackCompensationStoreMixin
from .feedback_eval_store import FeedbackEvalStoreMixin
from .feedback_evidence_store import FeedbackEvidenceStoreMixin
from .feedback_execution_store import FeedbackExecutionStoreMixin
from .feedback_external_governance_store import FeedbackExternalGovernanceStoreMixin
from .feedback_job_store import FeedbackJobStoreMixin
from .feedback_plan_task_edit_store import FeedbackPlanTaskEditStoreMixin
from .feedback_plan_task_store import FeedbackPlanTaskStoreMixin
from .feedback_proposal_store import FeedbackProposalStoreMixin
from .feedback_regression_asset_store import FeedbackRegressionAssetStoreMixin
from .feedback_source_store import FeedbackSourceStoreMixin
from .feedback_task_store import FeedbackTaskStoreMixin


class FeedbackStore(
    AgentJobQueueStoreMixin,
    AgentJobStoreMixin,
    FeedbackCompensationStoreMixin,
    FeedbackBatchExecutionStoreMixin,
    FeedbackBatchPlanStoreMixin,
    FeedbackBatchEvalCaseGovernanceStoreMixin,
    FeedbackPlanTaskEditStoreMixin,
    FeedbackPlanTaskStoreMixin,
    FeedbackExecutionStoreMixin,
    FeedbackTaskStoreMixin,
    FeedbackProposalStoreMixin,
    FeedbackJobStoreMixin,
    FeedbackBatchStoreMixin,
    FeedbackExternalGovernanceStoreMixin,
    FeedbackRegressionAssetStoreMixin,
    FeedbackEvalStoreMixin,
    FeedbackEvidenceStoreMixin,
    FeedbackCaseStoreMixin,
    FeedbackSourceStoreMixin,
):
    """SQLAlchemy-backed store for the feedback optimization loop."""

    def __init__(
        self,
        *,
        data_dir: Path,
        workspace_dir: Optional[Path] = None,
        agent_version_provider: Optional[Callable[[], Optional[str]]] = None,
        runtime_version: str = APP_VERSION,
        enable_debug_evidence: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.main_workspace_dir = workspace_dir or business_agent_layout(data_dir, "main-agent").workspace
        self.execution_targets = WorkspaceExecutionTargetPolicy(self.main_workspace_dir)
        self.db_path = runtime_db_path_from_data_dir(data_dir)
        self.Session = make_session_factory(self.db_path)
        self.external_governance = ExternalGovernanceService(
            session_factory=self.Session,
            webhooks_path=data_dir / "external-governance-webhooks.yaml",
        )
        self.agent_version_provider = agent_version_provider
        self.runtime_version = runtime_version
        self.enable_debug_evidence = enable_debug_evidence
        self.langfuse_trace_fetcher: Optional[Callable[[str], Optional[JsonObject]]] = None
        # Legacy cleanup anchor only. New Agent jobs keep input/output/error in SQLite.
        self.tmp_jobs_dir = data_dir / ".runtime-tmp" / "jobs"
        self._job_create_lock = RLock()

    def set_langfuse_trace_fetcher(self, fetcher: Callable[[str], Optional[JsonObject]]) -> None:
        # The fetcher is owned by the backend so Langfuse credentials never enter
        # internal Agent prompts or Claude Code tool configuration.
        self.langfuse_trace_fetcher = fetcher

    def _current_agent_version_id(self) -> Optional[str]:
        if not self.agent_version_provider:
            return None
        try:
            return self.agent_version_provider()
        except Exception:
            return None

    def _agent_git_paths_context(self) -> JsonObject:
        return {
            "main_agent_repository_path": str(self.main_workspace_dir),
            "agent_change_set_worktrees_path": str(self.data_dir / "agent-governance" / "worktrees"),
            "agent_release_archives_path": str(self.data_dir / "agent-governance" / "releases"),
            "agent_version_source": "git",
        }

    def _execution_target_policy(self) -> JsonObject:
        return self.execution_targets.policy_json()

    def _execution_target_file_contexts(self, target_paths: list[str]) -> list[JsonObject]:
        return self.execution_targets.file_contexts(target_paths)

    def _execution_target_file_context(self, target_path: str) -> JsonObject:
        return self.execution_targets.file_context(target_path)

    def _target_allowed(self, target_path: str) -> bool:
        return self.execution_targets.target_allowed(target_path)

    def _target_denied_reason(self, target_path: str) -> Optional[str]:
        return self.execution_targets.denied_reason(target_path)

    def _workspace_relative_path(self, target_path: str) -> Optional[Path]:
        return self.execution_targets.relative_path(target_path)

    def _workspace_rel_excluded(self, rel: Path) -> bool:
        return self.execution_targets.rel_excluded(rel)

    def _workspace_target_path(self, target_path: str) -> Optional[Path]:
        return self.execution_targets.target_path(target_path)

    def _scrub_record(self, value: Any) -> Any:
        if isinstance(value, dict):
            clean: JsonObject = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                    clean[key] = "[REDACTED]"
                else:
                    clean[key] = self._scrub_record(item)
            return clean
        if isinstance(value, list):
            return [self._scrub_record(item) for item in value]
        return value

    def _filter_records(
        self,
        records: list[JsonObject],
        filters: JsonObject,
        limit: int,
        *,
        any_key_groups: Optional[list[tuple[str, ...]]] = None,
    ) -> list[JsonObject]:
        result: list[JsonObject] = []
        any_key_groups = any_key_groups or []
        for record in records:
            if self._matches_filters(record, filters, any_key_groups):
                result.append(record)
            if len(result) >= limit:
                break
        return result

    def _matches_filters(self, record: JsonObject, filters: JsonObject, any_key_groups: list[tuple[str, ...]]) -> bool:
        grouped_keys = {key for group in any_key_groups for key in group}
        for key, value in filters.items():
            if value in (None, "") or key in grouped_keys:
                continue
            if record.get(key) != value:
                return False
        for group in any_key_groups:
            expected = next((filters.get(key) for key in group if filters.get(key) not in (None, "")), None)
            if expected is None:
                continue
            if not any(record.get(key) == expected for key in group):
                return False
        return True

    def _discard_batch_draft_artifacts(self, batch: JsonObject) -> None:
        cleanup_job_ids: list[str] = []
        with self.Session.begin() as db:
            self._discard_batch_draft_artifacts_row(db, batch, cleanup_job_ids)
        for job_id in cleanup_job_ids:
            self._cleanup_job_tmp(job_id)

    def _batch_draft_artifact_task_ids(self, batch: JsonObject) -> list[str]:
        task_ids: list[str] = []
        for value in [batch.get("optimization_task_id"), *self._string_list(batch.get("optimization_task_ids"))]:
            task_id = self._string(value)
            if task_id:
                task_ids.append(task_id)
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        for item in (plan or {}).get("tasks") or []:
            if not isinstance(item, dict):
                continue
            task_id = self._string(item.get("optimization_task_id"))
            if task_id:
                task_ids.append(task_id)
        return self._unique_strings(task_ids)

    def _batch_draft_artifact_reset_fields(self) -> JsonObject:
        return {
            "internal_proposal_id": None,
            "optimization_task_id": None,
            "optimization_task_ids": [],
            "optimization_task": None,
            "execution_job_id": None,
            "execution_job": None,
            "execution_apply_result": None,
            "execution_runs": [],
            "latest_execution_run": None,
            "applied_agent_version_id": None,
        }

    def _batch_execution_lock_reason(self, batch: JsonObject) -> Optional[str]:
        if batch.get("execution_apply_result") or self._string(batch.get("applied_agent_version_id")):
            return "已应用并产生执行结果"
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        for item in (plan or {}).get("tasks") or []:
            if not isinstance(item, dict):
                continue
            if item.get("applied_agent_version_id"):
                return "已产生 Agent 版本"
            latest_notification = item.get("latest_notification") if isinstance(item.get("latest_notification"), dict) else None
            if self._string(item.get("status")) == "notified" or self._string((latest_notification or {}).get("status")) == "sent":
                return "已有外部通知结果"
        runs = []
        latest_run = batch.get("latest_execution_run") if isinstance(batch.get("latest_execution_run"), dict) else None
        if latest_run:
            runs.append(latest_run)
        runs.extend(item for item in batch.get("execution_runs") or [] if isinstance(item, dict))
        for run in runs:
            status = self._string(run.get("status"))
            if run.get("applied_agent_version_id"):
                return "已产生 Agent 版本"
            if status == "running":
                return "正在一键执行"
            if status in {"completed", "partial_failed", "rollback_failed"}:
                return "已有一键执行记录"
        task_ids = self._batch_draft_artifact_task_ids(batch)
        batch_id = self._string(batch.get("batch_id"))
        with self.Session() as db:
            if batch_id:
                external_items = db.scalars(
                    select(ExternalGovernanceItemModel).where(ExternalGovernanceItemModel.proposal_job_id.like(f"batch-plan-task-{batch_id}-%"))
                ).all()
                for external_item in external_items:
                    payload = external_item.payload_json if isinstance(external_item.payload_json, dict) else {}
                    latest_notification = payload.get("latest_notification") if isinstance(payload.get("latest_notification"), dict) else None
                    notification = db.get(ExternalNotificationModel, external_item.latest_notification_id) if external_item.latest_notification_id else None
                    if (
                        self._string(external_item.status) == "notified"
                        or self._string((latest_notification or {}).get("status")) == "sent"
                        or (notification and self._string(notification.status) == "sent")
                    ):
                        return "已有外部通知结果"
            for task_id in task_ids:
                task = db.get(OptimizationTaskModel, task_id)
                if task and OptimizationTaskRecord.from_row(task).applied_agent_version_id:
                    return "已应用并产生 Agent 版本"
            if task_ids:
                active_execution = db.scalars(
                    select(AgentJobModel).where(
                        AgentJobModel.job_type == "execution",
                        AgentJobModel.scope_kind == "optimization_task",
                        AgentJobModel.scope_id.in_(task_ids),
                        AgentJobModel.status.in_(JOB_IN_PROGRESS_STATES),
                    )
                ).first()
                if active_execution:
                    return "正在生成执行结果"
            execution_job_id = self._string(batch.get("execution_job_id"))
            if execution_job_id:
                job = db.get(AgentJobModel, execution_job_id)
                if job and job.status in JOB_IN_PROGRESS_STATES:
                    return "正在生成执行结果"
        return None

    def _discard_batch_draft_artifacts_row(self, db: Any, batch: JsonObject, cleanup_job_ids: list[str]) -> None:
        task_ids = self._batch_draft_artifact_task_ids(batch)
        execution_job_id = self._string(batch.get("execution_job_id"))
        internal_proposal_id = self._string(batch.get("internal_proposal_id"))
        batch_id = self._string(batch.get("batch_id"))
        if batch_id:
            plan_jobs = db.scalars(
                select(AgentJobModel).where(
                    AgentJobModel.job_type == "batch_plan",
                    AgentJobModel.scope_kind == "optimization_batch",
                    AgentJobModel.scope_id == batch_id,
                )
            ).all()
            for plan_job in plan_jobs:
                db.delete(plan_job)
                self._append_cleanup_job_id(cleanup_job_ids, plan_job.job_id)
            external_items = db.scalars(
                select(ExternalGovernanceItemModel).where(
                    ExternalGovernanceItemModel.proposal_job_id.like(f"batch-plan-task-{batch_id}-%"),
                    ExternalGovernanceItemModel.status.in_(("pending_notification", "notification_failed")),
                )
            ).all()
            for external_item in external_items:
                db.execute(delete(ExternalNotificationModel).where(ExternalNotificationModel.external_item_id == external_item.external_item_id))
                db.delete(external_item)
        for task_id in task_ids:
            db.execute(delete(ExecutionApplicationModel).where(ExecutionApplicationModel.optimization_task_id == task_id))
            execution_rows = db.scalars(
                select(AgentJobModel).where(
                    AgentJobModel.job_type == "execution",
                    AgentJobModel.scope_kind == "optimization_task",
                    AgentJobModel.scope_id == task_id,
                )
            ).all()
            for execution in execution_rows:
                db.delete(execution)
                self._append_cleanup_job_id(cleanup_job_ids, execution.job_id)
            task = db.get(OptimizationTaskModel, task_id)
            if task and not OptimizationTaskRecord.from_row(task).applied_agent_version_id:
                db.delete(task)
        if execution_job_id:
            db.execute(delete(ExecutionApplicationModel).where(ExecutionApplicationModel.execution_job_id == execution_job_id))
            execution = db.get(AgentJobModel, execution_job_id)
            if execution:
                db.delete(execution)
            self._append_cleanup_job_id(cleanup_job_ids, execution_job_id)
        if internal_proposal_id:
            db.execute(delete(ProposalReviewModel).where(ProposalReviewModel.proposal_id == internal_proposal_id))
            db.execute(delete(OptimizationProposalModel).where(OptimizationProposalModel.proposal_id == internal_proposal_id))

    def _append_cleanup_job_id(self, cleanup_job_ids: list[str], job_id: Optional[str]) -> None:
        text = self._string(job_id)
        if text and text not in cleanup_job_ids:
            cleanup_job_ids.append(text)

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _sha256_json(self, value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _unique_strings(self, values: Iterable[Any]) -> list[str]:
        return unique_strings(values)

    def _string_list(self, values: Any) -> list[str]:
        if isinstance(values, str):
            return [values] if values else []
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str) and item]

    def _short_text(self, value: Optional[str], limit: int = 420) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _latest(self, values: Any) -> Optional[str]:
        if not isinstance(values, list) or not values:
            return None
        value = values[-1]
        return value if isinstance(value, str) and value else None

    def _string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
