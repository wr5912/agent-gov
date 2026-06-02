from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..integrations.external_governance import ExternalWebhookSender
from ..external_governance_mapping import (
    apply_external_governance_record,
    external_governance_record_from_row,
    external_governance_row_from_record,
)
from ..records.external_governance_records import (
    ExternalGovernanceItemRecord,
    ExternalGovernancePlanTaskDetailRecord,
    ExternalGuidanceInputRecord,
)
from ..records.json_types import JsonObject
from ..runtime_db import ExternalGovernanceItemModel, utc_now
from .store_projection_maps import ExternalGovernanceRowsBySourceIndex


class FeedbackExternalGovernanceStoreMixin:
    """Facade and upsert helpers for external governance workflow items."""

    def list_external_webhooks(self) -> list[JsonObject]:
        return self.external_governance.list_webhooks()

    def list_external_governance_items(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        return self.external_governance.list_items(
            feedback_case_id=feedback_case_id,
            proposal_job_id=proposal_job_id,
            status=status,
            limit=limit,
        )

    def find_external_governance_item(self, external_item_id: str) -> Optional[JsonObject]:
        return self.external_governance.find_item(external_item_id)

    def notify_external_governance_item(
        self,
        external_item_id: str,
        *,
        webhook_alias: str,
        sender: Optional[ExternalWebhookSender] = None,
    ) -> Optional[JsonObject]:
        return self.external_governance.notify_item(external_item_id, webhook_alias=webhook_alias, sender=sender)

    def _upsert_external_governance_items(
        self,
        normalized: JsonObject,
        job: JsonObject,
    ) -> list[JsonObject]:
        guidance_items = self._external_guidance_records(normalized)
        if not guidance_items:
            return []
        with self.Session.begin() as db:
            return self._upsert_external_governance_items_rows(db, normalized, job)

    def _upsert_external_governance_items_rows(
        self,
        db: Any,
        normalized: JsonObject,
        job: JsonObject,
    ) -> list[JsonObject]:
        guidance_items = self._external_guidance_records(normalized)
        if not guidance_items:
            return []
        existing_rows = db.scalars(
            select(ExternalGovernanceItemModel).where(ExternalGovernanceItemModel.proposal_job_id == job["job_id"])
        ).all()
        existing_by_index = self._external_governance_rows_by_source_index(existing_rows)
        result: list[JsonObject] = []
        for index, guidance in enumerate(guidance_items):
            existing = existing_by_index.get(index)
            existing_record = existing[1] if existing else None
            payload = self._external_guidance_payload(index, guidance, job, existing_record)
            if existing:
                row, record = existing
                apply_external_governance_record(row, self._merge_external_governance_record(record, payload))
            else:
                db.add(self._external_governance_row(payload))
            result.append({**guidance.to_payload(), **payload})
        return result

    def _upsert_external_governance_item_for_plan_task(
        self,
        batch: JsonObject,
        plan: JsonObject,
        plan_task: JsonObject,
    ) -> JsonObject:
        existing_id = self._string(plan_task.get("external_item_id"))
        existing = self.find_external_governance_item(existing_id) if existing_id else None
        external_item_id = existing_id or f"egi-{uuid.uuid4()}"
        payload = self._plan_task_external_payload(
            external_item_id=external_item_id,
            existing=existing,
            batch=batch,
            plan=plan,
            plan_task=plan_task,
        )
        with self.Session.begin() as db:
            row = db.get(ExternalGovernanceItemModel, external_item_id)
            if row:
                record = self._merge_external_governance_record(
                    external_governance_record_from_row(row),
                    payload,
                )
                apply_external_governance_record(row, record)
            else:
                db.add(self._external_governance_row(payload))
        return self.find_external_governance_item(external_item_id) or payload

    def _external_guidance_payload(
        self,
        index: int,
        guidance: ExternalGuidanceInputRecord,
        job: JsonObject,
        existing: Optional[ExternalGovernanceItemRecord],
    ) -> JsonObject:
        now = utc_now()
        return ExternalGovernanceItemRecord(
            external_item_id=existing.external_item_id if existing else f"egi-{uuid.uuid4()}",
            created_at=existing.created_at if existing else now,
            updated_at=now,
            status=existing.status if existing else "pending_notification",
            feedback_case_id=job["feedback_case_id"],
            proposal_job_id=job["job_id"],
            source_index=index,
            owner=guidance.owner or "needs_human_analysis",
            actionability=guidance.actionability or "external_guidance",
            recommendation=guidance.recommendation or "",
            reason=guidance.reason,
            latest_notification_id=existing.latest_notification_id if existing else None,
        ).to_payload()

    def _plan_task_external_payload(
        self,
        *,
        external_item_id: str,
        existing: Optional[JsonObject],
        batch: JsonObject,
        plan: JsonObject,
        plan_task: JsonObject,
    ) -> JsonObject:
        now = utc_now()
        feedback_case_id = self._latest(plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids")) or ""
        return ExternalGovernanceItemRecord(
            external_item_id=external_item_id,
            created_at=existing.get("created_at") if existing else now,
            updated_at=now,
            status=existing.get("status") if existing else "pending_notification",
            feedback_case_id=feedback_case_id,
            proposal_job_id=f"batch-plan-task-{batch['batch_id']}-{plan_task['plan_task_id']}",
            source_index=int(plan_task.get("source_index") or 0),
            owner=(
                self._string(plan_task.get("owner"))
                or self._string(plan_task.get("target_type"))
                or "external_system"
            ),
            actionability=self._string(plan_task.get("actionability")) or "external_guidance",
            latest_notification_id=existing.get("latest_notification_id") if existing else None,
            latest_webhook_alias=existing.get("latest_webhook_alias") if existing else None,
            latest_notification=existing.get("latest_notification") if existing else None,
            **self._plan_task_external_detail(batch, plan, plan_task),
        ).to_payload()

    def _plan_task_external_detail(
        self,
        batch: JsonObject,
        plan: JsonObject,
        plan_task: JsonObject,
    ) -> JsonObject:
        return ExternalGovernancePlanTaskDetailRecord(
            title=self._string(plan_task.get("title")) or "外部系统优化任务",
            description=self._string(plan_task.get("description")) or "",
            objective=self._string(plan_task.get("objective")) or "",
            target_summary=self._string(plan_task.get("target_summary")) or "",
            task_context=plan_task.get("task_context") if isinstance(plan_task.get("task_context"), dict) else {},
            recommendation=self._string(plan_task.get("recommendation")) or "",
            recommended_actions=self._string_list(plan_task.get("recommended_actions")),
            acceptance_criteria=self._string_list(plan_task.get("acceptance_criteria")),
            expected_effect=self._string(plan_task.get("expected_effect")) or "",
            validation=self._string(plan_task.get("validation")) or "",
            risk=self._string(plan_task.get("risk")) or "",
            analysis_summary=self._string(plan_task.get("analysis_summary")) or "",
            evidence_summary=self._string(plan_task.get("evidence_summary")) or "",
            evidence_refs=[dict(ref) for ref in plan_task.get("evidence_refs") or [] if isinstance(ref, dict)],
            reason=self._string(plan_task.get("reason")) or self._string(plan_task.get("rationale")),
            batch_id=self._string(batch.get("batch_id")),
            optimization_plan_id=self._string(plan.get("optimization_plan_id")),
            plan_task_id=self._string(plan_task.get("plan_task_id")),
            target_type=self._string(plan_task.get("target_type")),
            target_path=self._string(plan_task.get("target_path")),
            feedback_case_ids=self._string_list(plan_task.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
            eval_case_ids=self._string_list(batch.get("eval_case_ids")),
            source_attribution_job_ids=self._string_list(plan_task.get("attribution_job_ids")),
        ).to_payload()

    @staticmethod
    def _external_guidance_records(normalized: JsonObject) -> list[ExternalGuidanceInputRecord]:
        return [
            ExternalGuidanceInputRecord.model_validate(item)
            for item in normalized.get("external_guidance") or []
            if isinstance(item, dict)
        ]

    def _external_governance_row(self, payload: JsonObject) -> ExternalGovernanceItemModel:
        record = ExternalGovernanceItemRecord.model_validate(payload)
        return external_governance_row_from_record(record)

    def _external_governance_rows_by_source_index(
        self,
        rows: list[ExternalGovernanceItemModel],
    ) -> ExternalGovernanceRowsBySourceIndex:
        indexed: ExternalGovernanceRowsBySourceIndex = {}
        for row in rows:
            record = external_governance_record_from_row(row)
            indexed[record.source_index] = (row, record)
        return indexed

    def _merge_external_governance_record(
        self,
        record: ExternalGovernanceItemRecord,
        payload: JsonObject,
    ) -> ExternalGovernanceItemRecord:
        return ExternalGovernanceItemRecord.model_validate({**record.to_payload(), **payload})
