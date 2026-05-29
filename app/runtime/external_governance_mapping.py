from __future__ import annotations

from .records.external_governance_records import ExternalGovernanceItemRecord
from .runtime_db import ExternalGovernanceItemModel
from .state_machines import validate_transition


def external_governance_record_from_row(row: ExternalGovernanceItemModel) -> ExternalGovernanceItemRecord:
    payload = dict(row.payload_json or {})
    payload.update(
        {
            "external_item_id": row.external_item_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "status": row.status,
            "feedback_case_id": row.feedback_case_id,
            "proposal_job_id": row.proposal_job_id,
            "owner": row.owner,
            "actionability": row.actionability,
            "latest_notification_id": row.latest_notification_id,
        }
    )
    return ExternalGovernanceItemRecord.model_validate(payload)


def external_governance_row_from_record(record: ExternalGovernanceItemRecord) -> ExternalGovernanceItemModel:
    return ExternalGovernanceItemModel(
        external_item_id=record.external_item_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        status=record.status,
        feedback_case_id=record.feedback_case_id,
        proposal_job_id=record.proposal_job_id,
        owner=record.owner,
        actionability=record.actionability,
        latest_notification_id=record.latest_notification_id,
        payload_json=record.to_payload(),
    )


def apply_external_governance_record(
    row: ExternalGovernanceItemModel,
    record: ExternalGovernanceItemRecord,
) -> None:
    validate_transition("external_governance_item", row.status, record.status)
    row.updated_at = record.updated_at
    row.status = record.status
    row.owner = record.owner
    row.actionability = record.actionability
    row.latest_notification_id = record.latest_notification_id
    row.payload_json = record.to_payload()
