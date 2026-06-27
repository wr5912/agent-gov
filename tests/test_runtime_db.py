import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore


def test_runtime_db_reuses_engine_for_same_path(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"

    first = make_session_factory(db_path)
    second = make_session_factory(db_path)

    assert first.kw["bind"] is second.kw["bind"]


def test_concurrent_schema_init_no_table_exists_race(tmp_path):
    """①回归：api/worker 冷启动各自 engine 并发对同一 db 建 schema，不得 'table ... already exists'。

    用多个独立 engine（绕过进程内 engine 缓存）模拟跨进程并发；``_schema_init_lock`` 文件锁应串行化，
    否则并发 ``create_all`` 会触发 sqlite TOCTOU 竞态。
    """
    from app.runtime.runtime_db import _schema_init_lock, ensure_schema
    from sqlalchemy import create_engine

    db_path = tmp_path / "runtime.sqlite3"
    errors: list[Exception] = []

    def init(_: int) -> None:
        try:
            engine = create_engine(f"sqlite:///{db_path}", future=True)
            with _schema_init_lock(db_path):
                ensure_schema(engine)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(init, range(8)))

    assert not errors, f"并发 schema 初始化报错（竞态未被串行化）: {errors}"


def test_runtime_db_migrates_agent_jobs_without_output_schema_version(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE agent_jobs (
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
                output_schema_version VARCHAR(128) NOT NULL,
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
        connection.execute(
            """
            INSERT INTO agent_jobs (
                job_id, job_type, scope_kind, scope_id, status, profile_name, created_at,
                input_path, raw_output_path, validated_output_path, error_path, runtime_version,
                schema_version, output_schema_version, timeout_seconds, retry_count
            )
            VALUES (
                'job-old', 'batch_plan', 'optimization_batch', 'fob-old', 'queued',
                'proposal-generator', '2026-06-01T00:00:00+00:00', '/tmp/input.json',
                'sqlite://raw', 'sqlite://validated', 'sqlite://error', '0.0.0',
                'batch_plan-agent-job/v1', 'feedback-optimization-plan-output/v1', 300, 0
            )
            """
        )

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_jobs)").fetchall()}
        row = connection.exec_driver_sql("SELECT job_id, job_type FROM agent_jobs WHERE job_id = 'job-old'").fetchone()

    assert "output_schema_version" not in columns
    assert tuple(row) == ("job-old", "batch_plan")


def test_runtime_db_migrates_generated_by_onto_existing_content_tables(tmp_path):
    """§17.5 0015：旧 attributions / optimization_plans 表（无 generated_by）应被 ALTER 补列并回填 heuristic。"""
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE attributions (attribution_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "summary TEXT, responsibility_boundary_json JSON, evidence_json JSON, status VARCHAR(32), "
            "created_at VARCHAR(64), updated_at VARCHAR(64))"
        )
        connection.execute("INSERT INTO attributions (attribution_id, improvement_id, status) VALUES ('a-old', 'imp-1', 'draft')")
        connection.execute(
            "CREATE TABLE optimization_plans (optimization_plan_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "summary TEXT, changes_json JSON, status VARCHAR(32), created_at VARCHAR(64), updated_at VARCHAR(64))"
        )
        connection.execute("INSERT INTO optimization_plans (optimization_plan_id, improvement_id, status) VALUES ('o-old', 'imp-1', 'draft')")

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        attr_cols = {str(r[1]) for r in connection.exec_driver_sql("PRAGMA table_info(attributions)").fetchall()}
        opt_cols = {str(r[1]) for r in connection.exec_driver_sql("PRAGMA table_info(optimization_plans)").fetchall()}
        attr_val = connection.exec_driver_sql("SELECT generated_by FROM attributions WHERE attribution_id = 'a-old'").fetchone()[0]
        opt_val = connection.exec_driver_sql("SELECT generated_by FROM optimization_plans WHERE optimization_plan_id = 'o-old'").fetchone()[0]

    assert "generated_by" in attr_cols and "generated_by" in opt_cols
    assert attr_val == "heuristic" and opt_val == "heuristic"


def test_runtime_db_migrates_improvement_detail_columns_on_existing_tables(tmp_path):
    """0019：旧四阶段详情表缺新增列时，启动迁移后 API/store 写入不得 no such column。"""
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE attributions (attribution_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "summary TEXT, responsibility_boundary_json JSON, evidence_json JSON, status VARCHAR(32), "
            "generated_by VARCHAR(32), created_at VARCHAR(64), updated_at VARCHAR(64))"
        )
        connection.execute(
            "CREATE TABLE optimization_plans (optimization_plan_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "summary TEXT, changes_json JSON, status VARCHAR(32), generated_by VARCHAR(32), created_at VARCHAR(64), updated_at VARCHAR(64))"
        )
        connection.execute(
            "CREATE TABLE execution_records (execution_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "summary TEXT, changes_applied_json JSON, agent_version VARCHAR(128), status VARCHAR(32), generated_by VARCHAR(32), "
            "change_set_id VARCHAR(128), applied_agent_version_id VARCHAR(128), applied_diff_json JSON, created_at VARCHAR(64), updated_at VARCHAR(64))"
        )
        connection.execute(
            "CREATE TABLE regression_assessments (regression_assessment_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "summary TEXT, cases_json JSON, status VARCHAR(32), generated_by VARCHAR(32), created_at VARCHAR(64), updated_at VARCHAR(64))"
        )

    factory = make_session_factory(db_path)
    content = ImprovementContentStore(factory)
    content.upsert_attribution(
        "imp-0019",
        summary="归因",
        counter_evidence=["反证"],
        uncertainty_factors=["不确定性"],
        verification_suggestions=["核验"],
    )
    content.upsert_optimization_plan("imp-0019", summary="方案", risk_level="medium")
    content.upsert_execution(
        "imp-0019",
        summary="执行",
        risk_level="high",
        rollback_strategy="回滚策略",
        rollback_instructions=["恢复版本"],
    )
    content.upsert_regression_assessment("imp-0019", summary="回归", suggested_gate_thresholds={"pass_rate": 1.0})

    with factory.kw["bind"].connect() as connection:
        cols = {
            table: {str(r[1]) for r in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
            for table in ("attributions", "optimization_plans", "execution_records", "regression_assessments")
        }
        migration = connection.exec_driver_sql(
            "SELECT version FROM schema_migrations WHERE version = '0019_improvement_detail_columns'"
        ).fetchone()

    assert {"counter_evidence_json", "uncertainty_factors_json", "verification_suggestions_json"} <= cols["attributions"]
    assert "risk_level" in cols["optimization_plans"]
    assert {"risk_level", "rollback_strategy", "rollback_instructions_json"} <= cols["execution_records"]
    assert "suggested_gate_thresholds_json" in cols["regression_assessments"]
    assert migration is not None


def test_feedback_store_sqlite_handles_concurrent_signal_writes(tmp_path):
    store = FeedbackStore(data_dir=tmp_path / "data", agent_version_provider=lambda _aid=None: "main-v-test")

    def create_signal(index: int) -> str:
        signal = store.create_signal(
            FeedbackSignalCreateRequest(
                session_id=f"session-{index}",
                labels=["concurrency"],
                comment=f"并发反馈 {index}",
            )
        )
        return signal["signal_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        signal_ids = list(executor.map(create_signal, range(24)))

    assert len(signal_ids) == 24
    assert len(set(signal_ids)) == 24
    assert len(store.list_signals(limit=50)) == 24


def test_feedback_store_soc_event_ingest_is_idempotent_under_concurrency(tmp_path):
    store = FeedbackStore(data_dir=tmp_path / "data", agent_version_provider=lambda _aid=None: "main-v-test")

    def ingest_event(_: int) -> str:
        result = store.ingest_soc_event(
            SocEventIngestRequest(
                event_id="event-concurrent",
                event_type="tool.manual_query_after_agent",
                source_system="siem",
                timestamp="2026-05-20T00:03:00+00:00",
                alert_id="alert-concurrent",
                payload={"title": "并发告警"},
            )
        )
        return result["correlation_status"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(ingest_event, range(24)))

    assert statuses.count("pending_correlation") == 1
    assert statuses.count("duplicate") == 23
    assert len(store.list_events(limit=50)) == 1
    assert len(store.list_pending(status="pending", limit=50)) == 1


def test_feedback_store_attribution_job_create_reuses_existing_under_concurrency(tmp_path):
    store = FeedbackStore(data_dir=tmp_path / "data", agent_version_provider=lambda _aid=None: "main-v-test")
    store.record_run({"run_id": "run-1", "session_id": "session-1", "message": "并发归因"})
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["concurrency"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])

    def create_job(_: int) -> str:
        job = store.create_attribution_job(feedback_case["feedback_case_id"])
        return job["job_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        job_ids = list(executor.map(create_job, range(24)))

    assert len(set(job_ids)) == 1
    assert store.find_case(feedback_case["feedback_case_id"])["attribution_job_ids"] == [job_ids[0]]
