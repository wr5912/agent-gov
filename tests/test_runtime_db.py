import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_claude_user_input_db_cold_import_has_no_runtime_db_cycle():
    result = subprocess.run(
        [sys.executable, "-c", "import app.runtime.claude_user_input_db"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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
                'job-old', 'eval_case_generation', 'feedback_dataset', 'feedback-dataset', 'queued',
                'governor', '2026-06-01T00:00:00+00:00', '/tmp/input.json',
                'sqlite://raw', 'sqlite://validated', 'sqlite://error', '0.0.0',
                'agent-job/v1', 'feedback-eval-case-generation-output/v1', 300, 0
            )
            """
        )

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_jobs)").fetchall()}
        row = connection.exec_driver_sql("SELECT job_id, job_type FROM agent_jobs WHERE job_id = 'job-old'").fetchone()

    assert "output_schema_version" not in columns
    assert tuple(row) == ("job-old", "eval_case_generation")


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
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0019_improvement_detail_columns'").fetchone()

    assert {"counter_evidence_json", "uncertainty_factors_json", "verification_suggestions_json"} <= cols["attributions"]
    assert {"generation_trace_id", "generation_trace_url"} <= cols["attributions"]
    assert "risk_level" in cols["optimization_plans"]
    assert {"generation_trace_id", "generation_trace_url"} <= cols["optimization_plans"]
    assert {"risk_level", "rollback_strategy", "rollback_instructions_json"} <= cols["execution_records"]
    assert {"generation_trace_id", "generation_trace_url"} <= cols["execution_records"]
    assert "suggested_gate_thresholds_json" in cols["regression_assessments"]
    assert {"generation_trace_id", "generation_trace_url"} <= cols["regression_assessments"]
    assert migration is not None


def test_runtime_db_migrates_trace_columns_and_drops_legacy_optimization_chain(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    legacy_tables = [
        "regression_gate_overrides",
        "regression_impact_analyses",
        "regression_plans",
        "execution_applications",
        "execution_compensations",
        "external_notifications",
        "external_governance_items",
        "proposal_reviews",
        "optimization_proposals",
        "optimization_tasks",
        "feedback_optimization_batches",
    ]
    with sqlite3.connect(db_path) as connection:
        for table in legacy_tables:
            connection.execute(f"CREATE TABLE {table} (id VARCHAR(128) PRIMARY KEY)")

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        tables = {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0022_remove_legacy_batch_optimization_chain'").fetchone()

    assert set(legacy_tables).isdisjoint(tables)
    assert migration is not None


def test_runtime_db_renames_eval_case_targeted_regression_layer(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE eval_cases (
                eval_case_id VARCHAR(128) PRIMARY KEY,
                created_at VARCHAR(64),
                updated_at VARCHAR(64),
                status VARCHAR(64),
                source VARCHAR(64),
                prompt TEXT,
                expected_behavior TEXT,
                checks_json JSON,
                labels_json JSON,
                source_ids_json JSON,
                signal_ids_json JSON,
                event_ids_json JSON,
                pending_correlation_ids_json JSON,
                run_ids_json JSON,
                session_ids_json JSON,
                alert_ids_json JSON,
                case_ids_json JSON,
                evidence_package_ids_json JSON,
                attribution_job_ids_json JSON,
                asset_layer VARCHAR(64)
            )
            """
        )
        connection.execute("INSERT INTO eval_cases (eval_case_id, status, prompt, asset_layer) VALUES ('evc-old', 'active', 'p', 'batch_specific')")

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        value = connection.exec_driver_sql("SELECT asset_layer FROM eval_cases WHERE eval_case_id = 'evc-old'").fetchone()[0]
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0023_eval_case_targeted_regression_layer'").fetchone()

    assert value == "targeted_regression"
    assert migration is not None


def test_runtime_db_backfills_feedback_case_agent_id_from_signals(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE feedback_signals (
                signal_id VARCHAR(128) PRIMARY KEY,
                agent_id VARCHAR(128)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE feedback_cases (
                feedback_case_id VARCHAR(128) PRIMARY KEY,
                created_at VARCHAR(64),
                updated_at VARCHAR(64),
                status VARCHAR(64),
                title VARCHAR(512),
                priority VARCHAR(32),
                current_evidence_package_id VARCHAR(128),
                current_attribution_job_id VARCHAR(128),
                source_ids_json JSON,
                signal_ids_json JSON,
                event_ids_json JSON,
                pending_correlation_ids_json JSON,
                run_ids_json JSON,
                session_ids_json JSON,
                alert_ids_json JSON,
                case_ids_json JSON
            )
            """
        )
        connection.execute("INSERT INTO feedback_signals (signal_id, agent_id) VALUES ('fbs-a', 'agent-alpha')")
        connection.execute(
            """
            INSERT INTO feedback_cases (
                feedback_case_id, status, title, priority, source_ids_json, signal_ids_json
            ) VALUES (
                'fbc-a', 'pending_evidence', 'case', 'medium', '["fbs-a"]', '["fbs-a"]'
            )
            """
        )
        connection.execute(
            """
            UPDATE feedback_cases
            SET created_at = '2026-06-12T00:00:00Z', updated_at = '2026-06-12T00:00:00Z'
            WHERE feedback_case_id = 'fbc-a'
            """
        )

    factory = make_session_factory(db_path)
    store = FeedbackStore(data_dir=tmp_path / "data", agent_version_provider=lambda _aid=None: "main-v-test")
    store.Session = factory
    case = store.find_case("fbc-a")

    assert case is not None
    assert case["agent_id"] == "agent-alpha"


def test_runtime_db_migrates_legacy_agent_governance_paths(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    applied_before_0025 = (
        "0002_regression_assets",
        "0003_agent_jobs",
        "0004_unify_agent_jobs",
        "0005_agent_governance",
        "0006_remove_agent_job_output_contract_column",
        "0007_agent_registry",
        "0008_feedback_signal_agent_id",
        "0009_agent_registry_status",
        "0010_scenario_packs",
        "0011_change_set_release_agent_id",
        "0012_eval_run_agent_id",
        "0014_improvement_feedback_context",
        "0015_improvement_content_generated_by",
        "0016_execution_application_binding",
        "0017_regression_assessments",
        "0018_agent_registry_origin_tombstone",
        "0019_improvement_detail_columns",
        "0020_claude_user_input_requests",
        "0021_improvement_generation_trace_refs",
        "0022_remove_legacy_batch_optimization_chain",
        "0023_eval_case_targeted_regression_layer",
        "0024_feedback_case_agent_id",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_migrations (version VARCHAR(128) PRIMARY KEY, applied_at VARCHAR(64) NOT NULL)")
        connection.executemany(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-07-01T00:00:00+00:00')",
            [(version,) for version in applied_before_0025],
        )
        connection.execute(
            """
            CREATE TABLE agent_change_sets (
                change_set_id VARCHAR(128) PRIMARY KEY,
                worktree_path VARCHAR(2048) NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE agent_releases (
                release_id VARCHAR(128) PRIMARY KEY,
                archive_path VARCHAR(2048)
            )
            """
        )
        connection.execute("INSERT INTO agent_change_sets (change_set_id, worktree_path) VALUES ('cs-old', '/data/agent-governance/worktrees/cs-old')")
        connection.execute("INSERT INTO agent_releases (release_id, archive_path) VALUES ('rel-old', '/data/agent-governance/releases/rel-old.tar.gz')")

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        worktree_path = connection.exec_driver_sql("SELECT worktree_path FROM agent_change_sets WHERE change_set_id = 'cs-old'").fetchone()[0]
        archive_path = connection.exec_driver_sql("SELECT archive_path FROM agent_releases WHERE release_id = 'rel-old'").fetchone()[0]
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0025_agent_governance_legacy_paths'").fetchone()

    assert worktree_path == "/data/business-agents/main-agent/version/worktrees/cs-old"
    assert archive_path == "/data/business-agents/main-agent/version/releases/rel-old.tar.gz"
    assert migration is not None


def test_runtime_db_creates_claude_user_input_requests_table(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        columns = {str(r[1]) for r in connection.exec_driver_sql("PRAGMA table_info(claude_user_input_requests)").fetchall()}
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0020_claude_user_input_requests'").fetchone()
        index_rows = connection.exec_driver_sql("PRAGMA index_list(claude_user_input_requests)").fetchall()
        indexes = {str(row[1]) for row in index_rows}

    assert {
        "request_id",
        "decision_token_hash",
        "business_agent_id",
        "run_id",
        "api_session_id",
        "request_type",
        "tool_name",
        "redacted_input_json",
        "status",
        "decision",
        "decision_payload_json",
        "expires_at",
    } <= columns
    assert "ix_claude_user_input_agent_status" in indexes
    assert "ix_claude_user_input_run_status" in indexes
    assert migration is not None


def test_runtime_db_creates_response_disposition_claims_table(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(response_disposition_claims)").fetchall()}
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0028_response_disposition_claims'").fetchone()
        indexes = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA index_list(response_disposition_claims)").fetchall()}

    assert {
        "approval_request_id",
        "case_id",
        "playbook_digest",
        "execution_run_id",
        "agent_run_id",
        "status",
        "create_authorized",
        "manual_authorized",
        "failure_reason",
    } <= columns
    assert "ix_response_disposition_claim_status" in indexes
    assert "ix_response_disposition_claim_case" in indexes
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


def test_runtime_db_migrates_normalized_feedback_provenance_columns(tmp_path):
    """旧 normalized_feedbacks 表（无 provenance 列）经 0026 迁移幂等补列，旧行保留、默认 heuristic。"""
    db_path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE normalized_feedbacks (normalized_feedback_id VARCHAR(128) PRIMARY KEY, improvement_id VARCHAR(128), "
            "problem TEXT, possible_reason TEXT, possible_object TEXT, impact TEXT, suggestion TEXT, user_quote TEXT, "
            "status VARCHAR(32), created_at VARCHAR(64), updated_at VARCHAR(64))"
        )
        connection.execute("INSERT INTO normalized_feedbacks VALUES ('nf-1','imp-1','p','','','','','q','draft','t','t')")

    factory = make_session_factory(db_path)
    with factory.kw["bind"].connect() as connection:
        cols = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(normalized_feedbacks)").fetchall()}
        row = connection.exec_driver_sql("SELECT generated_by FROM normalized_feedbacks WHERE normalized_feedback_id='nf-1'").fetchone()
        migration = connection.exec_driver_sql("SELECT version FROM schema_migrations WHERE version = '0026_normalized_feedback_generation_refs'").fetchone()

    assert {"generated_by", "generation_trace_id", "generation_trace_url"} <= cols
    assert row is not None and row[0] == "heuristic"  # 旧行保留、默认值
    assert migration is not None
