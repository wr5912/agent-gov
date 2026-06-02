from feedback_store_test_utils import ValidationError, _store, pytest

from app.runtime.runtime_db import ExecutionCompensationModel


def test_execution_compensation_rejects_invalid_persisted_payload(tmp_path):
    store, _ = _store(tmp_path)
    compensation = store.record_execution_compensation(
        optimization_task_id="opt-invalid-compensation",
        execution_job_id="fbe-invalid-compensation",
        pre_execution_agent_version_id="main-before",
        restore_status="restore_failed",
        original_error="write failed",
        restore_error="restore failed",
    )
    with store.Session.begin() as db:
        row = db.get(ExecutionCompensationModel, compensation["compensation_id"])
        row.payload_json = {**row.payload_json, "original_error": ["not", "text"]}

    with pytest.raises(ValidationError):
        store.find_execution_compensation(compensation["compensation_id"])
