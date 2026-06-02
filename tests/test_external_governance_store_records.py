from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    ValidationError,
    _attribution_output,
    _record_run,
    _store,
    pytest,
)
from sqlalchemy import select

from app.runtime.runtime_db import ExternalGovernanceItemModel


def test_external_guidance_upsert_rejects_invalid_persisted_payload(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    raw_output = {
        "schema_version": "proposal-output/v1",
        "feedback_case_id": feedback_case["feedback_case_id"],
        "proposal_job_id": proposal_job["job_id"],
        "status": "completed",
        "proposals": [],
        "external_guidance": [
            {
                "owner": "knowledge-base",
                "actionability": "external_guidance",
                "recommendation": "refresh governance payload",
            }
        ],
        "no_action_reason": None,
    }
    store.complete_proposal_job(proposal_job["job_id"], raw_output)

    with store.Session.begin() as db:
        row = db.scalars(select(ExternalGovernanceItemModel)).one()
        row.payload_json = {**row.payload_json, "source_index": {"bad": "index"}}

    with pytest.raises(ValidationError):
        with store.Session.begin() as db:
            store._upsert_external_governance_items_rows(
                db,
                store._normalize_proposal_output(raw_output, proposal_job),
                proposal_job,
            )
