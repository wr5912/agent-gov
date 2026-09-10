from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Optional
from weakref import WeakValueDictionary

from sqlalchemy import JSON, ForeignKey, Index, String, create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import QueuePool

from .json_types import JsonObject
from .protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from .runtime_db_base import Base, utc_now

_ENGINE_CACHE: WeakValueDictionary[Path, Engine] = WeakValueDictionary()
_ENGINE_CACHE_LOCK = RLock()

from app.agent_testing.models import (  # noqa: E402,F401
    AgentTestRunItemModel,
    AgentTestRunModel,
    AgentTestScheduleEventModel,
    AgentTestScheduleModel,
    AgentWorkspaceImportRecordModel,
)
from app.runtime_gateway.models import (  # noqa: E402,F401
    AgentRunModel,
    RuntimeAgentDeletionIntentModel,
    RuntimeAgentVersionModel,
    RuntimeCutoverLedgerModel,
    RuntimeEphemeralResourceModel,
    RuntimePendingActionModel,
    RuntimeReceiptModel,
    RuntimeSessionBindingModel,
    RuntimeTeamDeliveryModel,
)

# Fresh-schema 的目标集合必须与调用方 import 顺序无关。集中加载所有声明在
# 独立模块中的 Base model，随后再做精确 schema 校验。
from . import agent_registry_db as _agent_registry_db  # noqa: E402,F401
from . import asset_db as _asset_db  # noqa: E402,F401
from . import improvement_db as _improvement_db  # noqa: E402,F401
from .agent_maintenance_db import (  # noqa: E402,F401
    AgentAdmissionStateModel,
    AgentReleaseOperationModel,
    AgentWorktreeCleanupTaskModel,
)

_SCHEMA_EPOCH = "agentscope-runtime-v1"


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(64), default=utc_now)


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
    agent_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
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
    agent_id: Mapped[str] = mapped_column(String(128), default=DEFAULT_BUSINESS_AGENT_ID, index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    priority: Mapped[str] = mapped_column(String(32), index=True)
    current_evidence_package_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    current_attribution_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    source_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    signal_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    event_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    pending_correlation_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    run_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    session_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    alert_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    case_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)


class FeedbackCaseSourceModel(Base):
    __tablename__ = "feedback_case_sources"

    source_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("feedback_cases.feedback_case_id", ondelete="CASCADE"),
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    is_direct: Mapped[bool] = mapped_column(default=True)
    direct_position: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


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


class AgentChangeSetModel(Base):
    __tablename__ = "agent_change_sets"

    change_set_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), default=DEFAULT_BUSINESS_AGENT_ID, index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    execution_job_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    candidate_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    branch_name: Mapped[str] = mapped_column(String(256), index=True)
    worktree_path: Mapped[str] = mapped_column(String(2048))
    payload_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


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
    agent_id: Mapped[str] = mapped_column(String(128), default=DEFAULT_BUSINESS_AGENT_ID, index=True)
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


class AgentReleaseTagClaimModel(Base):
    __tablename__ = "agent_release_tag_claims"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tag_name: Mapped[str] = mapped_column(String(256), primary_key=True)
    change_set_id: Mapped[str] = mapped_column(String(128), index=True)
    release_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


Index("ux_agent_release_tag_claims_change_set", AgentReleaseTagClaimModel.change_set_id, unique=True)
Index("ux_agent_release_tag_claims_release", AgentReleaseTagClaimModel.release_id, unique=True)


class AgentReleaseSourceClaimModel(Base):
    __tablename__ = "agent_release_source_claims"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_improvement_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    change_set_id: Mapped[str] = mapped_column(String(128), index=True)
    release_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[str] = mapped_column(String(64), default=utc_now, index=True)


Index("ux_agent_release_source_claims_change_set", AgentReleaseSourceClaimModel.change_set_id, unique=True)
Index("ux_agent_release_source_claims_release", AgentReleaseSourceClaimModel.release_id, unique=True)


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


@contextmanager
def _schema_init_lock(db_path: Path):
    """跨进程串行化 schema 初始化。

    api 与 worker 冷启动会同时对同一 sqlite 跑 ``create_all``；``checkfirst`` 只是进程内
    TOCTOU，挡不住跨进程并发 → ``sqlite3.OperationalError: table ... already exists``。
    用与 db 同目录的锁文件 + ``flock`` 排他锁串行化（``:memory:`` 等无父目录路径跳过）。
    """
    try:
        import fcntl
    except ImportError:  # 非 Unix 平台无 fcntl：退化为不加锁（仍有 create_all checkfirst）
        yield
        return
    lock_path = db_path.parent / f"{db_path.name}.schema.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def make_session_factory(db_path: Path) -> sessionmaker:
    engine = make_engine(db_path)
    # ①修复：api/worker 并发冷启动时串行化建表/迁移，避免 create_all 跨进程竞态。
    with _schema_init_lock(db_path):
        ensure_schema(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def ensure_schema(engine: Engine) -> None:
    """Create only an empty AgentScope epoch, or validate an exact existing one.

    The atomic cutover deliberately has no legacy migration path.  A database
    with any old/unknown table, a partial table set, or a mismatched column set
    is refused before ``create_all`` can mutate it.
    """

    inspector = inspect(engine)
    expected_tables = set(Base.metadata.tables)
    existing_tables = set(inspector.get_table_names())
    if existing_tables:
        unknown = sorted(existing_tables - expected_tables)
        missing = sorted(expected_tables - existing_tables)
        if unknown or missing:
            raise RuntimeError(
                f"Runtime database is not the exact AgentScope schema epoch (unknown={unknown}, missing={missing})",
            )
        for table_name, table in Base.metadata.tables.items():
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            expected_columns = {column.name for column in table.columns}
            if actual_columns != expected_columns:
                raise RuntimeError(
                    f"Runtime database column contract mismatch for {table_name} (actual={sorted(actual_columns)}, expected={sorted(expected_columns)})",
                )
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            if session.get(SchemaMigration, _SCHEMA_EPOCH) is None:
                raise RuntimeError("Runtime database is missing the AgentScope schema epoch marker")
        return

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory.begin() as session:
        session.add(SchemaMigration(version=_SCHEMA_EPOCH, applied_at=utc_now()))
