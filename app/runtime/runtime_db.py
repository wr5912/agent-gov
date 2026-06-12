from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Optional

from sqlalchemy import JSON, Float, ForeignKey, Index, String, create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import QueuePool

from .json_types import JsonObject
from .runtime_db_migrations import (
    migrate_0005_agent_governance,
    migrate_0006_remove_agent_job_output_contract_column,
    migrate_0007_agent_registry,
    migrate_0008_feedback_signal_agent_id,
    migrate_0009_agent_registry_status,
)


_ENGINE_CACHE: dict[Path, Engine] = {}
_ENGINE_CACHE_LOCK = RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(64), default=utc_now)


class SessionRecordModel(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sdk_session_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    turns: Mapped[int] = mapped_column(default=0)
    metadata_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class AgentRunModel(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    sdk_session_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    alert_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    langfuse_trace_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    langfuse_trace_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class FeedbackSignalModel(Base):
    __tablename__ = "feedback_signals"

    signal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    matched_run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    alert_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class SocEventModel(Base):
    __tablename__ = "soc_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    source_system: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    matched_run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    alert_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class PendingCorrelationModel(Base):
    __tablename__ = "pending_correlations"

    pending_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class FeedbackSourceAnnotationModel(Base):
    __tablename__ = "feedback_source_annotations"

    annotation_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_feedback_source_annotations_source", FeedbackSourceAnnotationModel.source_kind, FeedbackSourceAnnotationModel.source_id, unique=True)


class FeedbackCaseModel(Base):
    __tablename__ = "feedback_cases"

    feedback_case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    priority: Mapped[str] = mapped_column(String(32), index=True)
    current_evidence_package_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    current_attribution_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    current_proposal_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    pending_correlation_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    run_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    session_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    alert_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    case_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)


class EvidencePackageModel(Base):
    __tablename__ = "evidence_packages"

    evidence_package_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    feedback_case_id: Mapped[str] = mapped_column(String(128), ForeignKey("feedback_cases.feedback_case_id"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    manifest_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class EvidenceFileModel(Base):
    __tablename__ = "evidence_files"

    evidence_package_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("evidence_packages.evidence_package_id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_name: Mapped[str] = mapped_column(String(256), primary_key=True)
    file_type: Mapped[str] = mapped_column(String(128), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[object] = mapped_column(JSON)


class AgentJobModel(Base):
    __tablename__ = "agent_jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    scope_kind: Mapped[str] = mapped_column(String(64), index=True)
    scope_id: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    profile_name: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    started_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_path: Mapped[str] = mapped_column(String(2048))
    raw_output_path: Mapped[str] = mapped_column(String(2048))
    validated_output_path: Mapped[str] = mapped_column(String(2048))
    error_path: Mapped[str] = mapped_column(String(2048))
    runtime_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    timeout_seconds: Mapped[int] = mapped_column(default=300)
    retry_count: Mapped[int] = mapped_column(default=0)
    profile_version_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    input_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    raw_output_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    validated_output_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)
    error_json: Mapped[Optional[JsonObject]] = mapped_column(JSON, nullable=True)


Index("ix_agent_jobs_type_status_created", AgentJobModel.job_type, AgentJobModel.status, AgentJobModel.created_at)
Index("ix_agent_jobs_scope_type_created", AgentJobModel.scope_kind, AgentJobModel.scope_id, AgentJobModel.job_type, AgentJobModel.created_at)


class OptimizationProposalModel(Base):
    __tablename__ = "optimization_proposals"

    proposal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    feedback_case_id: Mapped[str] = mapped_column(String(128), index=True)
    proposal_job_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    actionability: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_path: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class ProposalReviewModel(Base):
    __tablename__ = "proposal_reviews"

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(128), ForeignKey("optimization_proposals.proposal_id"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    action: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class OptimizationTaskModel(Base):
    __tablename__ = "optimization_tasks"

    optimization_task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    feedback_case_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class FeedbackOptimizationBatchModel(Base):
    __tablename__ = "feedback_optimization_batches"

    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class ExecutionCompensationModel(Base):
    __tablename__ = "execution_compensations"

    compensation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    compensation_type: Mapped[str] = mapped_column(String(128), index=True)
    optimization_task_id: Mapped[str] = mapped_column(String(128), index=True)
    execution_job_id: Mapped[str] = mapped_column(String(128), index=True)
    pre_execution_agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    restore_status: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class ExecutionApplicationModel(Base):
    __tablename__ = "execution_applications"

    application_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution_job_id: Mapped[str] = mapped_column(String(128), index=True)
    optimization_task_id: Mapped[str] = mapped_column(String(128), ForeignKey("optimization_tasks.optimization_task_id"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_execution_applications_job_created", ExecutionApplicationModel.execution_job_id, ExecutionApplicationModel.created_at)
Index("ix_execution_applications_task_created", ExecutionApplicationModel.optimization_task_id, ExecutionApplicationModel.created_at)


class AgentChangeSetModel(Base):
    __tablename__ = "agent_change_sets"

    change_set_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    optimization_task_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    execution_job_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    candidate_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    branch_name: Mapped[str] = mapped_column(String(256), index=True)
    worktree_path: Mapped[str] = mapped_column(String(2048))
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_agent_change_sets_task_created", AgentChangeSetModel.optimization_task_id, AgentChangeSetModel.created_at)
Index("ix_agent_change_sets_status_updated", AgentChangeSetModel.status, AgentChangeSetModel.updated_at)


class AgentChangeSetEventModel(Base):
    __tablename__ = "agent_change_set_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    change_set_id: Mapped[str] = mapped_column(String(128), ForeignKey("agent_change_sets.change_set_id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    operator: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    before_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    after_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_agent_change_set_events_change_created", AgentChangeSetEventModel.change_set_id, AgentChangeSetEventModel.created_at)


class AgentReleaseModel(Base):
    __tablename__ = "agent_releases"

    release_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    tag_name: Mapped[str] = mapped_column(String(256), index=True)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    change_set_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    rollback_of_release_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    archive_path: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_agent_releases_status_created", AgentReleaseModel.status, AgentReleaseModel.created_at)


class ExternalGovernanceItemModel(Base):
    __tablename__ = "external_governance_items"

    external_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    feedback_case_id: Mapped[str] = mapped_column(String(128), index=True)
    proposal_job_id: Mapped[str] = mapped_column(String(128), index=True)
    owner: Mapped[str] = mapped_column(String(256), index=True)
    actionability: Mapped[str] = mapped_column(String(128), index=True)
    latest_notification_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class ExternalNotificationModel(Base):
    __tablename__ = "external_notifications"

    notification_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    external_item_id: Mapped[str] = mapped_column(String(128), ForeignKey("external_governance_items.external_item_id"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    webhook_alias: Mapped[str] = mapped_column(String(128), index=True)
    http_status: Mapped[Optional[int]] = mapped_column(nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class EvalCaseModel(Base):
    __tablename__ = "eval_cases"

    eval_case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    source_feedback_case_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    source_run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    asset_layer: Mapped[str] = mapped_column(String(64), default="candidate", index=True)
    promotion_status: Mapped[str] = mapped_column(String(64), default="candidate", index=True)
    blocking_policy: Mapped[str] = mapped_column(String(64), default="non_blocking", index=True)
    scenario_pack: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    severity: Mapped[str] = mapped_column(String(64), default="medium", index=True)
    flaky_status: Mapped[str] = mapped_column(String(64), default="stable", index=True)
    variant_role: Mapped[str] = mapped_column(String(64), default="original_reproduction", index=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    last_run_at: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    last_result_status: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    failure_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    superseded_by_eval_case_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    labels_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index(
    "ix_eval_cases_source_variant_hash",
    EvalCaseModel.source_feedback_case_id,
    EvalCaseModel.variant_role,
    EvalCaseModel.content_hash,
    unique=True,
)


class EvalCaseRevisionModel(Base):
    __tablename__ = "eval_case_revisions"

    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    eval_case_id: Mapped[str] = mapped_column(String(128), ForeignKey("eval_cases.eval_case_id", ondelete="CASCADE"), index=True)
    revision_number: Mapped[int] = mapped_column(index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    snapshot_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_eval_case_revisions_case_number", EvalCaseRevisionModel.eval_case_id, EvalCaseRevisionModel.revision_number, unique=True)


class EvalCaseGovernanceEventModel(Base):
    __tablename__ = "eval_case_governance_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    eval_case_id: Mapped[str] = mapped_column(String(128), ForeignKey("eval_cases.eval_case_id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    operator: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(128), default="developer", index=True)
    reason: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    before_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    after_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class EvalRunModel(Base):
    __tablename__ = "eval_runs"

    eval_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    optimization_task_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    regression_plan_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class EvalRunItemModel(Base):
    __tablename__ = "eval_run_items"

    eval_run_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(String(128), ForeignKey("eval_runs.eval_run_id", ondelete="CASCADE"), index=True)
    eval_case_id: Mapped[str] = mapped_column(String(128), ForeignKey("eval_cases.eval_case_id"), index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class RegressionPlanModel(Base):
    __tablename__ = "regression_plans"

    regression_plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), ForeignKey("feedback_optimization_batches.batch_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), default="created", index=True)
    applied_agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    selection_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_regression_plans_batch_fingerprint", RegressionPlanModel.batch_id, RegressionPlanModel.selection_fingerprint, unique=True)


class RegressionImpactAnalysisModel(Base):
    __tablename__ = "regression_impact_analyses"

    impact_analysis_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(String(128), ForeignKey("eval_runs.eval_run_id", ondelete="CASCADE"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


Index("ix_regression_impact_analyses_eval_run", RegressionImpactAnalysisModel.eval_run_id, unique=True)


class RegressionGateOverrideModel(Base):
    __tablename__ = "regression_gate_overrides"

    override_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), ForeignKey("feedback_optimization_batches.batch_id", ondelete="CASCADE"), index=True)
    eval_run_id: Mapped[str] = mapped_column(String(128), ForeignKey("eval_runs.eval_run_id", ondelete="CASCADE"), index=True)
    operator: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str] = mapped_column(String(2048))
    expires_at: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    before_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    after_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


def runtime_db_path_from_data_dir(data_dir: Path) -> Path:
    return data_dir / "runtime.sqlite3"


def make_engine(db_path: Path) -> Engine:
    resolved_path = db_path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(resolved_path)
        if cached is not None:
            return cached
        engine = create_engine(
            f"sqlite:///{resolved_path}",
            connect_args={"check_same_thread": False, "timeout": 30.0},
            future=True,
            pool_pre_ping=True,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
        )
        _ENGINE_CACHE[resolved_path] = engine

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine


def make_session_factory(db_path: Path) -> sessionmaker:
    engine = make_engine(db_path)
    ensure_schema(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def ensure_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _run_runtime_migrations(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory.begin() as session:
        if not session.get(SchemaMigration, "0001_sqlalchemy_runtime_store"):
            session.add(SchemaMigration(version="0001_sqlalchemy_runtime_store", applied_at=utc_now()))


def _run_runtime_migrations(engine: Engine) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory.begin() as session:
        applied = {str(row.version) for row in session.query(SchemaMigration).all()}
    for version, migrate in (
        ("0002_regression_assets", _migrate_0002_regression_assets),
        ("0003_agent_jobs", _migrate_0003_agent_jobs),
        ("0004_unify_agent_jobs", _migrate_0004_unify_agent_jobs),
        ("0005_agent_governance", migrate_0005_agent_governance),
        ("0006_remove_agent_job_output_contract_column", migrate_0006_remove_agent_job_output_contract_column),
        ("0007_agent_registry", migrate_0007_agent_registry),
        ("0008_feedback_signal_agent_id", migrate_0008_feedback_signal_agent_id),
        ("0009_agent_registry_status", migrate_0009_agent_registry_status),
    ):
        if version in applied:
            continue
        with engine.begin() as connection:
            migrate(connection)
        with factory.begin() as session:
            if not session.get(SchemaMigration, version):
                session.add(SchemaMigration(version=version, applied_at=utc_now()))


def _migrate_0002_regression_assets(connection: Connection) -> None:
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_eval_cases_source_feedback_case_unique")
    columns = _table_columns(connection, "eval_cases")
    for column_name, ddl in {
        "asset_layer": "VARCHAR(64) DEFAULT 'candidate'",
        "promotion_status": "VARCHAR(64) DEFAULT 'candidate'",
        "blocking_policy": "VARCHAR(64) DEFAULT 'non_blocking'",
        "scenario_pack": "VARCHAR(128)",
        "severity": "VARCHAR(64) DEFAULT 'medium'",
        "flaky_status": "VARCHAR(64) DEFAULT 'stable'",
        "variant_role": "VARCHAR(64) DEFAULT 'original_reproduction'",
        "content_hash": "VARCHAR(64)",
        "last_run_at": "VARCHAR(64)",
        "last_result_status": "VARCHAR(64)",
        "failure_rate": "FLOAT",
        "superseded_by_eval_case_id": "VARCHAR(128)",
    }.items():
        if column_name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE eval_cases ADD COLUMN {column_name} {ddl}")
    eval_run_columns = _table_columns(connection, "eval_runs")
    if "regression_plan_id" not in eval_run_columns:
        connection.exec_driver_sql("ALTER TABLE eval_runs ADD COLUMN regression_plan_id VARCHAR(128)")

    connection.exec_driver_sql(
        """
        UPDATE eval_cases
        SET
            asset_layer = CASE
                WHEN status = 'draft' THEN 'candidate'
                WHEN status = 'archived' THEN COALESCE(asset_layer, 'candidate')
                WHEN source_feedback_case_id IS NULL THEN 'batch_specific'
                ELSE 'historical_bug'
            END,
            promotion_status = CASE
                WHEN status = 'active' THEN 'approved'
                WHEN status = 'archived' THEN 'archived'
                ELSE 'candidate'
            END,
            blocking_policy = CASE
                WHEN status = 'active' AND source_feedback_case_id IS NULL THEN 'blocking'
                WHEN status = 'active' THEN 'blocking_if_relevant'
                ELSE 'non_blocking'
            END,
            severity = COALESCE(severity, 'medium'),
            flaky_status = COALESCE(flaky_status, 'stable'),
            variant_role = COALESCE(variant_role, 'original_reproduction')
        WHERE asset_layer IS NULL OR promotion_status IS NULL OR blocking_policy IS NULL
        """
    )

    rows = connection.exec_driver_sql("SELECT eval_case_id, payload_json FROM eval_cases WHERE content_hash IS NULL").fetchall()
    for eval_case_id, payload_json in rows:
        content_hash = _eval_case_content_hash(payload_json, str(eval_case_id))
        connection.exec_driver_sql(
            "UPDATE eval_cases SET content_hash = ? WHERE eval_case_id = ?",
            (content_hash, eval_case_id),
        )

    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_cases_source_feedback_case_id ON eval_cases (source_feedback_case_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_cases_asset_layer ON eval_cases (asset_layer)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_cases_promotion_status ON eval_cases (promotion_status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_cases_blocking_policy ON eval_cases (blocking_policy)")
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_eval_cases_source_variant_hash ON eval_cases (source_feedback_case_id, variant_role, content_hash)"
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_eval_runs_regression_plan_id ON eval_runs (regression_plan_id)")


def _migrate_0003_agent_jobs(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS agent_jobs (
            job_id VARCHAR(128) NOT NULL PRIMARY KEY,
            job_type VARCHAR(64) NOT NULL,
            scope_kind VARCHAR(64) NOT NULL,
            scope_id VARCHAR(256) NOT NULL,
            status VARCHAR(64) NOT NULL,
            profile_name VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            started_at VARCHAR(64),
            completed_at VARCHAR(64),
            input_path VARCHAR(2048) NOT NULL,
            raw_output_path VARCHAR(2048) NOT NULL,
            validated_output_path VARCHAR(2048) NOT NULL,
            error_path VARCHAR(2048) NOT NULL,
            runtime_version VARCHAR(64) NOT NULL,
            schema_version VARCHAR(64) NOT NULL,
            timeout_seconds INTEGER,
            retry_count INTEGER,
            profile_version_json JSON,
            input_json JSON,
            raw_output_json JSON,
            validated_output_json JSON,
            error_json JSON
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_job_type ON agent_jobs (job_type)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_scope_kind ON agent_jobs (scope_kind)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_scope_id ON agent_jobs (scope_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_status ON agent_jobs (status)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_profile_name ON agent_jobs (profile_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_agent_jobs_created_at ON agent_jobs (created_at)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_type_status_created ON agent_jobs (job_type, status, created_at)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_agent_jobs_scope_type_created ON agent_jobs (scope_kind, scope_id, job_type, created_at)"
    )


def _migrate_0004_unify_agent_jobs(connection: Connection) -> None:
    connection.exec_driver_sql("DROP TABLE IF EXISTS feedback_jobs")
    connection.exec_driver_sql("DROP TABLE IF EXISTS optimization_executions")
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS execution_applications (
            application_id VARCHAR(128) NOT NULL PRIMARY KEY,
            execution_job_id VARCHAR(128) NOT NULL,
            optimization_task_id VARCHAR(128) NOT NULL,
            created_at VARCHAR(64) NOT NULL,
            completed_at VARCHAR(64),
            status VARCHAR(64) NOT NULL,
            payload_json JSON NOT NULL,
            FOREIGN KEY(optimization_task_id) REFERENCES optimization_tasks (optimization_task_id)
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_execution_applications_execution_job_id ON execution_applications (execution_job_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_execution_applications_optimization_task_id ON execution_applications (optimization_task_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_execution_applications_created_at ON execution_applications (created_at)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_execution_applications_job_created ON execution_applications (execution_job_id, created_at)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_execution_applications_task_created ON execution_applications (optimization_task_id, created_at)"
    )


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}


def _eval_case_content_hash(payload_json: object, fallback: str) -> str:
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else dict(payload_json or {})
    except (TypeError, ValueError):
        payload = {"eval_case_id": fallback}
    stable = {
        "prompt": payload.get("prompt"),
        "expected_behavior": payload.get("expected_behavior"),
        "checks_json": payload.get("checks_json") or {},
        "labels": sorted(str(item) for item in payload.get("labels") or []),
        "asset_layer": payload.get("asset_layer"),
        "source_feedback_case_id": payload.get("source_feedback_case_id"),
        "source_kind": payload.get("source_kind"),
        "source_id": payload.get("source_id"),
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
