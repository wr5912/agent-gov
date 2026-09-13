from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Optional, TypedDict, cast
from weakref import WeakValueDictionary

from sqlalchemy import JSON, ForeignKey, Index, String, create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import QueuePool

from .json_types import JsonObject
from .protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from .runtime_db_base import Base, begin_sqlite_write_transaction, utc_now
from .sqlite_schema_contract import (
    CURRENT_SCHEMA_CONTRACT_SHA256,
    CURRENT_SCHEMA_EPOCH,
    LEGACY_SCHEMA_EPOCH,
    PREVIOUS_SCHEMA_EPOCH,
    REMOVED_RELEASE_OPERATION_TABLE,
    SqliteReader,
    SqliteSchemaEpochClassification,
    SqliteSchemaEpochInspection,
    inspect_sqlite_schema_epoch,
    physical_schema_contract_sha256,
)

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
    RuntimeChatOperationModel,
    RuntimeCutoverLedgerModel,
    RuntimeEphemeralResourceModel,
    RuntimePendingActionModel,
    RuntimeReceiptModel,
    RuntimeSessionBindingModel,
    RuntimeTeamDeliveryModel,
)
from app.runtime_gateway.operation_identity import (  # noqa: E402
    LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT,
    RuntimeChatOperationKind,
    initial_operation_key,
    session_creation_request_fingerprint,
)

# Fresh-schema 的目标集合必须与调用方 import 顺序无关。集中加载所有声明在
# 独立模块中的 Base model，随后再做精确 schema 校验。
from . import agent_registry_db as _agent_registry_db  # noqa: E402,F401
from . import asset_db as _asset_db  # noqa: E402,F401
from . import improvement_db as _improvement_db  # noqa: E402,F401
from .agent_maintenance_db import (  # noqa: E402,F401
    AgentAdmissionStateModel,
    AgentWorktreeCleanupTaskModel,
)

_SCHEMA_EPOCH = CURRENT_SCHEMA_EPOCH
_PREVIOUS_SCHEMA_EPOCH = PREVIOUS_SCHEMA_EPOCH
_LEGACY_SCHEMA_EPOCH = LEGACY_SCHEMA_EPOCH
_SESSION_INTENT_TABLE = "runtime_session_creation_intents"
_REMOVED_RELEASE_OPERATION_TABLE = REMOVED_RELEASE_OPERATION_TABLE


class _LegacyChatOperationRow(TypedDict):
    operation_key: str
    client_operation_id: str
    operation_kind: str
    request_fingerprint: str
    run_id: str
    root_session_id: str
    action_session_id: str
    runtime_agent_id: str
    reply_id: None
    action_ids_json: list[str]
    tool_call_ids_json: list[str]
    confirmation_scope: None
    response_status: int | None
    response_body: bytes | None
    response_content_type: str | None
    response_headers_json: JsonObject | None
    created_at: str
    updated_at: str


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
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    # ①修复：api/worker 并发冷启动时串行化建表/迁移，避免 create_all 跨进程竞态。
    with _schema_init_lock(db_path):
        ensure_schema(engine)
        # 这是 AgentScope epoch 内部的一次性隐私数据迁移，不是旧 runtime
        # schema 兼容路径。在 schema lock 内写入幂等 marker，在线 store 不做 scrub。
        from app.runtime_gateway.hitl_migration import (
            migrate_hitl_fingerprint_rows,
        )

        migrate_hitl_fingerprint_rows(factory)
    return factory


def _known_data_migration_markers() -> frozenset[str]:
    from app.runtime_gateway.hitl_migration import (
        HITL_FINGERPRINT_DATA_MIGRATION,
    )

    return frozenset({HITL_FINGERPRINT_DATA_MIGRATION})


def _schema_versions(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            str(value)
            for value in connection.execute(
                text("SELECT version FROM schema_migrations"),
            ).scalars()
        }


def _require_marker_table_contract(engine: Engine, existing_tables: set[str]) -> set[str]:
    expected_tables = set(Base.metadata.tables)
    if "schema_migrations" not in existing_tables:
        _raise_table_contract_mismatch(existing_tables, expected_tables)
    inspector = inspect(engine)
    actual_columns = {str(column["name"]) for column in inspector.get_columns("schema_migrations")}
    expected_columns = {column.name for column in Base.metadata.tables["schema_migrations"].columns}
    if actual_columns != expected_columns:
        raise RuntimeError(
            "Runtime database physical schema contract mismatch for schema_migrations",
        )
    versions = _schema_versions(engine)
    known = {
        _LEGACY_SCHEMA_EPOCH,
        _PREVIOUS_SCHEMA_EPOCH,
        _SCHEMA_EPOCH,
        *_known_data_migration_markers(),
    }
    unknown = sorted(versions - known)
    if unknown:
        raise RuntimeError(
            f"Runtime database has unknown schema migration markers: {unknown}",
        )
    return versions


def _raise_table_contract_mismatch(
    existing_tables: set[str],
    expected_tables: set[str],
) -> None:
    unknown = sorted(existing_tables - expected_tables)
    missing = sorted(expected_tables - existing_tables)
    raise RuntimeError(
        f"Runtime database is not the exact AgentScope schema epoch (unknown={unknown}, missing={missing})",
    )


def _physical_contract_sha256(bind: Engine | Connection) -> str:
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return _physical_contract_sha256(connection)
    driver_connection = bind.connection.driver_connection
    if driver_connection is None:
        raise RuntimeError("Runtime database driver connection is unavailable")
    try:
        return physical_schema_contract_sha256(cast(SqliteReader, driver_connection))
    except (IndexError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise RuntimeError("Runtime database physical schema cannot be inspected safely") from exc


def _inspect_schema_epoch(bind: Engine | Connection) -> SqliteSchemaEpochInspection:
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return _inspect_schema_epoch(connection)
    driver_connection = bind.connection.driver_connection
    if driver_connection is None:
        raise RuntimeError("Runtime database driver connection is unavailable")
    try:
        return inspect_sqlite_schema_epoch(
            cast(SqliteReader, driver_connection),
            known_data_migration_markers=_known_data_migration_markers(),
        )
    except (IndexError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise RuntimeError("Runtime database physical schema cannot be inspected safely") from exc


def _validate_current_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    expected_tables = set(Base.metadata.tables)
    existing_tables = set(inspector.get_table_names())
    if existing_tables != expected_tables:
        _raise_table_contract_mismatch(existing_tables, expected_tables)
    actual_sha256 = _physical_contract_sha256(engine)
    if actual_sha256 != CURRENT_SCHEMA_CONTRACT_SHA256:
        raise RuntimeError(
            "Runtime database physical schema contract mismatch for the AgentScope v3 epoch",
        )


def _validate_source_schema_shape(
    bind: Engine | Connection,
    *,
    source_epoch: str,
) -> bool:
    if source_epoch == _PREVIOUS_SCHEMA_EPOCH:
        expected = {SqliteSchemaEpochClassification.PREVIOUS_MIGRATABLE}
    elif source_epoch == _LEGACY_SCHEMA_EPOCH:
        expected = {
            SqliteSchemaEpochClassification.LEGACY_MIGRATABLE,
            SqliteSchemaEpochClassification.LEGACY_UNSAFE_HISTORY,
        }
    else:  # pragma: no cover - internal call sites are fixed
        raise RuntimeError("Unsupported Runtime schema migration source")
    inspection = _inspect_schema_epoch(bind)
    if inspection.classification not in expected:
        raise RuntimeError(
            f"Runtime database physical schema contract mismatch for the {source_epoch} migration boundary",
        )
    return _REMOVED_RELEASE_OPERATION_TABLE in inspection.tables


def _drop_named_indexes(connection: Connection, table_name: str) -> None:
    for index in inspect(connection).get_indexes(table_name):
        index_name = index.get("name")
        if isinstance(index_name, str) and index_name:
            quoted_name = connection.dialect.identifier_preparer.quote(index_name)
            connection.exec_driver_sql(f"DROP INDEX {quoted_name}")


def _rebuild_session_creation_intents(
    connection: Connection,
    *,
    source_epoch: str,
) -> None:
    table = Base.metadata.tables[_SESSION_INTENT_TABLE]
    backup_table = f"{_SESSION_INTENT_TABLE}__migration_source"
    legacy_fingerprints: dict[str, str] = {}
    if source_epoch == _LEGACY_SCHEMA_EPOCH:
        rows = connection.exec_driver_sql(
            f'SELECT intent_id, runtime_agent_id, session_name FROM "{_SESSION_INTENT_TABLE}"',
        ).all()
        legacy_fingerprints = {
            str(intent_id): session_creation_request_fingerprint(
                str(runtime_agent_id),
                None if name is None else str(name),
            )
            for intent_id, runtime_agent_id, name in rows
        }
    previous_count = connection.exec_driver_sql(
        f'SELECT COUNT(*) FROM "{_SESSION_INTENT_TABLE}"',
    ).scalar_one()
    connection.exec_driver_sql(
        f'ALTER TABLE "{_SESSION_INTENT_TABLE}" RENAME TO "{backup_table}"',
    )
    _drop_named_indexes(connection, backup_table)
    table.create(connection)
    copied_columns = [column.name for column in table.columns if column.name != "request_fingerprint"]
    quoted = ", ".join(connection.dialect.identifier_preparer.quote(name) for name in copied_columns)
    connection.exec_driver_sql(
        f'INSERT INTO "{_SESSION_INTENT_TABLE}" ({quoted}, request_fingerprint) SELECT {quoted}, ? FROM "{backup_table}"',
        (LEGACY_UNKNOWN_SESSION_REQUEST_FINGERPRINT,),
    )
    for intent_id, fingerprint in legacy_fingerprints.items():
        connection.exec_driver_sql(
            f'UPDATE "{_SESSION_INTENT_TABLE}" SET request_fingerprint = ? WHERE intent_id = ?',
            (fingerprint, intent_id),
        )
    migrated_count = connection.exec_driver_sql(
        f'SELECT COUNT(*) FROM "{_SESSION_INTENT_TABLE}"',
    ).scalar_one()
    if migrated_count != previous_count:
        raise RuntimeError("Runtime Session intent migration did not preserve every row")
    connection.exec_driver_sql(f'DROP TABLE "{backup_table}"')


def _legacy_chat_operations(connection: Connection) -> list[_LegacyChatOperationRow]:
    rows = connection.exec_driver_sql(
        """
        SELECT run_id, session_id, runtime_agent_id, client_operation_id,
               input_fingerprint, trigger_response_status,
               trigger_response_body, trigger_response_content_type,
               created_at, updated_at
        FROM agent_runs
        WHERE client_operation_id IS NOT NULL
        """,
    ).mappings()
    operations: list[_LegacyChatOperationRow] = []
    for row in rows:
        client_operation_id = row["client_operation_id"]
        request_fingerprint = row["input_fingerprint"]
        if not isinstance(client_operation_id, str) or not isinstance(request_fingerprint, str):
            raise RuntimeError("Runtime run lacks a replay-safe initial operation identity")
        response_parts = (
            row["trigger_response_status"],
            row["trigger_response_body"],
            row["trigger_response_content_type"],
        )
        if any(value is None for value in response_parts) and any(value is not None for value in response_parts):
            raise RuntimeError("Runtime run has a partial trigger response ledger")
        if response_parts[0] is not None and (
            not isinstance(response_parts[0], int) or not isinstance(response_parts[1], bytes) or not isinstance(response_parts[2], str)
        ):
            raise RuntimeError("Runtime run has an invalid trigger response ledger")
        operations.append(
            {
                "operation_key": initial_operation_key(client_operation_id),
                "client_operation_id": client_operation_id,
                "operation_kind": RuntimeChatOperationKind.INITIAL.value,
                "request_fingerprint": request_fingerprint,
                "run_id": row["run_id"],
                "root_session_id": row["session_id"],
                "action_session_id": row["session_id"],
                "runtime_agent_id": row["runtime_agent_id"],
                "reply_id": None,
                "action_ids_json": [],
                "tool_call_ids_json": [],
                "confirmation_scope": None,
                "response_status": row["trigger_response_status"],
                "response_body": row["trigger_response_body"],
                "response_content_type": row["trigger_response_content_type"],
                "response_headers_json": ({} if row["trigger_response_status"] is not None else None),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )
    return operations


def _rebuild_agent_runs(connection: Connection) -> None:
    table_name = AgentRunModel.__tablename__
    table = Base.metadata.tables[table_name]
    backup_table = f"{table_name}__migration_source"
    previous_count = connection.exec_driver_sql(
        f'SELECT COUNT(*) FROM "{table_name}"',
    ).scalar_one()
    connection.exec_driver_sql(
        f'ALTER TABLE "{table_name}" RENAME TO "{backup_table}"',
    )
    _drop_named_indexes(connection, backup_table)
    table.create(connection)
    quoted_columns = ", ".join(connection.dialect.identifier_preparer.quote(column.name) for column in table.columns)
    connection.exec_driver_sql(
        f'INSERT INTO "{table_name}" ({quoted_columns}) SELECT {quoted_columns} FROM "{backup_table}"',
    )
    migrated_count = connection.exec_driver_sql(
        f'SELECT COUNT(*) FROM "{table_name}"',
    ).scalar_one()
    if migrated_count != previous_count:
        raise RuntimeError("Runtime run migration did not preserve every row")
    connection.exec_driver_sql(f'DROP TABLE "{backup_table}"')


def _migrate_source_schema(engine: Engine, *, source_epoch: str) -> None:
    with engine.begin() as connection:
        begin_sqlite_write_transaction(connection)
        has_removed_release_table = _validate_source_schema_shape(
            connection,
            source_epoch=source_epoch,
        )
        if has_removed_release_table:
            operation_count = connection.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{_REMOVED_RELEASE_OPERATION_TABLE}"',
            ).scalar_one()
            if operation_count:
                raise RuntimeError(
                    "Runtime database has non-empty removed release operations; refusing to discard governance history",
                )
        operations = _legacy_chat_operations(connection)
        _rebuild_session_creation_intents(connection, source_epoch=source_epoch)
        _rebuild_agent_runs(connection)
        operation_table = Base.metadata.tables[RuntimeChatOperationModel.__tablename__]
        operation_table.create(connection)
        if operations:
            connection.execute(operation_table.insert(), operations)
        if has_removed_release_table:
            connection.exec_driver_sql(
                f'DROP TABLE "{_REMOVED_RELEASE_OPERATION_TABLE}"',
            )
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = :version"),
            {"version": source_epoch},
        )
        connection.execute(
            text(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (:version, :applied_at)",
            ),
            {"version": _SCHEMA_EPOCH, "applied_at": utc_now()},
        )


def ensure_schema(engine: Engine) -> None:
    """Create/validate v3 and migrate only exact v1/v2 control schemas."""

    existing_tables = set(inspect(engine).get_table_names())
    if existing_tables:
        versions = _require_marker_table_contract(engine, existing_tables)
        epoch_markers = versions.intersection(
            {_LEGACY_SCHEMA_EPOCH, _PREVIOUS_SCHEMA_EPOCH, _SCHEMA_EPOCH},
        )
        if len(epoch_markers) != 1:
            raise RuntimeError("Runtime database has missing or conflicting schema epoch markers")
        source_epoch = next(iter(epoch_markers))
        inspection = _inspect_schema_epoch(engine)
        classified_epoch = {
            SqliteSchemaEpochClassification.CURRENT: _SCHEMA_EPOCH,
            SqliteSchemaEpochClassification.PREVIOUS_MIGRATABLE: _PREVIOUS_SCHEMA_EPOCH,
            SqliteSchemaEpochClassification.LEGACY_MIGRATABLE: _LEGACY_SCHEMA_EPOCH,
            SqliteSchemaEpochClassification.LEGACY_UNSAFE_HISTORY: _LEGACY_SCHEMA_EPOCH,
        }.get(inspection.classification)
        if classified_epoch != source_epoch:
            raise RuntimeError(
                "Runtime database physical schema contract mismatch for its declared AgentScope epoch",
            )
        if source_epoch != _SCHEMA_EPOCH:
            _migrate_source_schema(engine, source_epoch=source_epoch)
            versions = _schema_versions(engine)
        _validate_current_schema(engine)
        if _SCHEMA_EPOCH not in versions:
            raise RuntimeError("Runtime database is missing the AgentScope schema epoch marker")
        if versions.intersection({_LEGACY_SCHEMA_EPOCH, _PREVIOUS_SCHEMA_EPOCH}):
            raise RuntimeError("Runtime database retained an earlier AgentScope schema epoch marker")
        return

    Base.metadata.create_all(engine)
    _validate_current_schema(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory.begin() as session:
        session.add(SchemaMigration(version=_SCHEMA_EPOCH, applied_at=utc_now()))
