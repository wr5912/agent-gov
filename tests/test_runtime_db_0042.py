from __future__ import annotations

from app.runtime.runtime_db import AgentJobModel
from app.runtime.runtime_db_migrations_0042 import (
    RETIRED_AGENT_JOB_ERROR_CODE,
    RETIRED_AGENT_JOB_STATES,
    migrate_0042_retire_persisted_agent_job_queue,
)

from feedback_store_test_utils import _store


def _job(job_id: str, status: str) -> AgentJobModel:
    terminal = status in {"completed", "failed", "needs_human_review", "timeout"}
    return AgentJobModel(
        job_id=job_id,
        job_type="attribution",
        scope_kind="improvement",
        scope_id="imp-0042",
        status=status,
        profile_name="governor",
        created_at="2026-07-01T00:00:00+00:00",
        started_at="2026-07-01T00:00:01+00:00" if status not in {"created", "queued"} else None,
        completed_at="2026-07-01T00:00:02+00:00" if terminal else None,
        input_path="input.json",
        raw_output_path="raw.json",
        validated_output_path="validated.json",
        error_path="error.json",
        runtime_version="legacy",
        schema_version="attribution-agent-job/v1",
        timeout_seconds=300,
        retry_count=1,
        profile_version_json={"name": "legacy"},
        input_json={"input": job_id},
        raw_output_json={"raw": job_id},
        validated_output_json={"validated": job_id},
        error_json={"error_code": "LEGACY_ERROR", "message": job_id},
    )


def test_0042_retires_non_terminal_queue_rows_without_losing_evidence(tmp_path) -> None:
    store, _ = _store(tmp_path)
    terminal_states = ("completed", "failed", "needs_human_review", "timeout")
    with store.Session.begin() as db:
        for status in (*RETIRED_AGENT_JOB_STATES, *terminal_states):
            db.add(_job(f"job-{status}", status))

    engine = store.Session.kw["bind"]
    with engine.begin() as connection:
        migrate_0042_retire_persisted_agent_job_queue(connection)

    first_snapshot: dict[str, tuple[object, ...]] = {}
    with store.Session() as db:
        for status in RETIRED_AGENT_JOB_STATES:
            row = db.get(AgentJobModel, f"job-{status}")
            assert row is not None
            assert row.status == "failed"
            assert row.completed_at
            assert row.input_json == {"input": f"job-{status}"}
            assert row.raw_output_json == {"raw": f"job-{status}"}
            assert row.validated_output_json == {"validated": f"job-{status}"}
            assert row.error_json["error_code"] == RETIRED_AGENT_JOB_ERROR_CODE
            assert row.error_json["previous_error"] == {
                "error_code": "LEGACY_ERROR",
                "message": f"job-{status}",
            }
            first_snapshot[row.job_id] = (row.completed_at, row.error_json)

        for status in terminal_states:
            row = db.get(AgentJobModel, f"job-{status}")
            assert row is not None
            assert row.status == status
            assert row.completed_at == "2026-07-01T00:00:02+00:00"
            assert row.error_json == {
                "error_code": "LEGACY_ERROR",
                "message": f"job-{status}",
            }

    with engine.begin() as connection:
        migrate_0042_retire_persisted_agent_job_queue(connection)
    with store.Session() as db:
        second_snapshot = {
            row.job_id: (row.completed_at, row.error_json)
            for row in db.query(AgentJobModel).filter(AgentJobModel.job_id.in_(first_snapshot)).all()
        }
    assert second_snapshot == first_snapshot


def test_0042_is_registered_in_runtime_schema(tmp_path) -> None:
    store, _ = _store(tmp_path)
    with store.Session.kw["bind"].connect() as connection:
        migration = connection.exec_driver_sql(
            "SELECT version FROM schema_migrations WHERE version = '0042_retire_persisted_agent_job_queue'"
        ).fetchone()

    assert migration is not None
