from __future__ import annotations

from typing import Any

from ..errors import BusinessRuleViolation
from ..json_types import JsonObject
from ..records.batch_plan_records import FeedbackOptimizationInternalActionResultRecord
from ..records.eval_case_records import EvalCaseRecord, apply_eval_case_record
from ..runtime_db import EvalCaseModel


class FeedbackInternalActionStoreMixin:
    """Helpers for backend-owned optimization plan internal actions."""

    def _internal_action_eval_case_ids(self, batch: JsonObject, plan_task: JsonObject) -> list[str]:
        eval_case_ids = self._string_list(plan_task.get("eval_case_ids"))
        if not eval_case_ids:
            raise BusinessRuleViolation("promote_eval_cases requires eval_case_ids")
        linked_ids = set(self._string_list(batch.get("eval_case_ids")))
        invalid_ids = [eval_case_id for eval_case_id in eval_case_ids if eval_case_id not in linked_ids]
        if invalid_ids:
            raise BusinessRuleViolation("promote_eval_cases eval_case_ids must belong to the optimization batch")
        return eval_case_ids

    def _promote_eval_cases_for_internal_action(
        self,
        db: Any,
        eval_case_ids: list[str],
        now: str,
        action_reason: str,
    ) -> list[JsonObject]:
        rows_and_records: list[tuple[EvalCaseModel, JsonObject, EvalCaseRecord]] = []
        for eval_case_id in eval_case_ids:
            row = db.get(EvalCaseModel, eval_case_id)
            if not row:
                raise BusinessRuleViolation(f"Eval case not found: {eval_case_id}")
            before_record = EvalCaseRecord.from_row(row)
            before_state = before_record.to_payload()
            next_record = self._promoted_eval_case_record(before_state, now)
            before_record.transition_to(status=next_record.status, promotion_status=next_record.promotion_status)
            rows_and_records.append((row, before_state, next_record))

        updated_cases: list[JsonObject] = []
        for row, before_state, next_record in rows_and_records:
            apply_eval_case_record(row, next_record)
            after_state = next_record.to_payload()
            updated_cases.append(after_state)
            self._add_eval_case_revision_row(db, after_state, created_by="feedback_optimizer", reason=action_reason)
            self._add_eval_case_governance_event_row(
                db,
                eval_case_id=next_record.eval_case_id,
                action="promote",
                operator="feedback_optimizer",
                role="system",
                reason=action_reason,
                before=before_state,
                after=after_state,
            )
        return updated_cases

    def _promoted_eval_case_record(self, before_state: JsonObject, now: str) -> EvalCaseRecord:
        next_state = {
            **before_state,
            "updated_at": now,
            "status": "active",
            "asset_layer": "core_regression",
            "promotion_status": "approved",
            "blocking_policy": "blocking_if_relevant",
        }
        return EvalCaseRecord.model_validate(self._eval_case_with_asset_defaults(next_state))

    def _internal_action_result_payload(self, eval_case_ids: list[str], updated_cases: list[JsonObject], completed_at: str) -> JsonObject:
        return FeedbackOptimizationInternalActionResultRecord(
            action="promote_eval_cases",
            status="completed",
            eval_case_ids=eval_case_ids,
            updated_eval_case_ids=[str(case["eval_case_id"]) for case in updated_cases],
            operator="feedback_optimizer",
            role="system",
            completed_at=completed_at,
        ).to_payload()
