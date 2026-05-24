from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import JSON, Float, ForeignKey, Index, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


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
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class FeedbackSignalModel(Base):
    __tablename__ = "feedback_signals"

    signal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    matched_run_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    alert_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(256), index=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PendingCorrelationModel(Base):
    __tablename__ = "pending_correlations"

    pending_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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
    content_json: Mapped[Any] = mapped_column(JSON)


class FeedbackJobModel(Base):
    __tablename__ = "feedback_jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    feedback_case_id: Mapped[str] = mapped_column(String(128), ForeignKey("feedback_cases.feedback_case_id"), index=True)
    evidence_package_id: Mapped[str] = mapped_column(String(128), index=True)
    attribution_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    profile_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    started_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_path: Mapped[str] = mapped_column(String(2048))
    raw_output_path: Mapped[str] = mapped_column(String(2048))
    validated_output_path: Mapped[str] = mapped_column(String(2048))
    error_path: Mapped[str] = mapped_column(String(2048))
    langfuse_trace_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    main_agent_version_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    runtime_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    timeout_seconds: Mapped[int] = mapped_column(default=300)
    retry_count: Mapped[int] = mapped_column(default=0)
    profile_version_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    input_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    raw_output_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    validated_output_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)


Index("ix_feedback_jobs_case_type_created", FeedbackJobModel.feedback_case_id, FeedbackJobModel.job_type, FeedbackJobModel.created_at)


class OptimizationProposalModel(Base):
    __tablename__ = "optimization_proposals"

    proposal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    feedback_case_id: Mapped[str] = mapped_column(String(128), index=True)
    proposal_job_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    actionability: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_path: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProposalReviewModel(Base):
    __tablename__ = "proposal_reviews"

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(128), ForeignKey("optimization_proposals.proposal_id"), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    action: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OptimizationTaskModel(Base):
    __tablename__ = "optimization_tasks"

    optimization_task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    feedback_case_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def runtime_db_path_from_data_dir(data_dir: Path) -> Path:
    return data_dir / "runtime.sqlite3"


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def make_session_factory(db_path: Path) -> sessionmaker:
    engine = make_engine(db_path)
    ensure_schema(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def ensure_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory.begin() as session:
        if not session.get(SchemaMigration, "0001_sqlalchemy_runtime_store"):
            session.add(SchemaMigration(version="0001_sqlalchemy_runtime_store", applied_at=utc_now()))
