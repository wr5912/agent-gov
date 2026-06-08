from __future__ import annotations

from typing import Optional

from ..json_types import JsonObject
from ..records.eval_case_records import ACTIVE_ASSET_LAYERS, BLOCKING_POLICIES, EvalCaseRecord, apply_eval_case_record
from ..runtime_db import EvalCaseModel, FeedbackOptimizationBatchModel, utc_now


class FeedbackBatchEvalCaseGovernanceStoreMixin:
    """Batch-scoped governance actions for regression eval cases."""

    def _batch_regression_asset_eligibility_by_id(self, batch_id: str) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        return self._batch_regression_asset_eligibility(batch)

    def _promote_batch_eval_cases(self, batch_id: str, fields: JsonObject) -> Optional[JsonObject]:
        operator = (self._string(fields.get("operator")) or "system").strip()
        role = (self._string(fields.get("role")) or "developer").strip()
        reason = (self._string(fields.get("reason")) or "晋级本批次候选回归用例").strip()
        asset_layer = self._defaulted_enum(fields.get("asset_layer"), ACTIVE_ASSET_LAYERS, "batch_specific", "asset_layer")
        blocking_policy = self._defaulted_enum(
            fields.get("blocking_policy"),
            BLOCKING_POLICIES,
            self._default_blocking_policy(asset_layer, "approved", "active"),
            "blocking_policy",
        )
        promoted: list[JsonObject] = []
        skipped: list[JsonObject] = []
        now = utc_now()
        with self.Session.begin() as db:
            batch_row = db.get(FeedbackOptimizationBatchModel, batch_id)
            if not batch_row:
                return None
            batch = self._batch_payload_snapshot(batch_row)
            for eval_case_id in self._string_list(batch.get("eval_case_ids")):
                row = db.get(EvalCaseModel, eval_case_id)
                if not row:
                    skipped.append({"eval_case_id": eval_case_id, "reasons": ["missing"]})
                    continue
                before_record = EvalCaseRecord.from_row(row)
                before = before_record.to_payload()
                if self._eval_case_enters_regression_plan(before):
                    skipped.append({"eval_case_id": eval_case_id, "reasons": ["already_eligible"]})
                    continue
                blockers = self._batch_eval_case_promotion_blockers(before)
                if blockers:
                    skipped.append({"eval_case_id": eval_case_id, "reasons": blockers})
                    continue
                payload = {
                    **before,
                    "updated_at": now,
                    "status": "active",
                    "promotion_status": "approved",
                    "asset_layer": asset_layer,
                    "blocking_policy": blocking_policy,
                }
                record = EvalCaseRecord.model_validate(self._eval_case_with_asset_defaults(payload))
                before_record.transition_to(status=record.status, promotion_status=record.promotion_status)
                apply_eval_case_record(row, record)
                projected = record.to_payload()
                self._add_eval_case_revision_row(db, projected, created_by=operator, reason=reason)
                self._add_eval_case_governance_event_row(
                    db,
                    eval_case_id=eval_case_id,
                    action="promote",
                    operator=operator,
                    role=role,
                    reason=reason,
                    before=before,
                    after=projected,
                )
                promoted.append(projected)
        return {
            "batch": self.find_optimization_batch(batch_id),
            "promoted_eval_cases": promoted,
            "skipped_eval_cases": skipped,
            "eligibility_summary": self._batch_regression_asset_eligibility_by_id(batch_id) or {},
        }

    def _batch_regression_asset_empty_error(self, batch: JsonObject) -> tuple[str, JsonObject]:
        eligibility = self._batch_regression_asset_eligibility(batch)
        summary = eligibility.get("summary", {}) if isinstance(eligibility.get("summary"), dict) else {}
        linked_total = int(summary.get("linked_total") or 0)
        promotable_total = int(summary.get("promotable_linked") or 0)
        if promotable_total:
            detail = f"当前批次没有可运行回归资产：{promotable_total} 条候选用例需先晋级为批次专用回归资产。"
        elif linked_total:
            detail = "当前批次关联用例均不符合回归准入条件，请检查用例状态、晋级状态、资产层或稳定性。"
        else:
            detail = "当前批次没有可运行回归资产，请先新增或晋级 active/approved 回归用例。"
        return detail, {
            "regression_asset_eligibility": eligibility,
            "suggested_action": "promote_batch_eval_cases" if promotable_total else "review_regression_assets",
        }

    def _batch_regression_asset_eligibility(self, batch: JsonObject) -> JsonObject:
        linked_case_ids = self._string_list(batch.get("eval_case_ids"))
        linked_cases: list[JsonObject] = []
        eligible_linked: set[str] = set()
        promotable_linked: set[str] = set()
        for eval_case_id in linked_case_ids:
            case = self.find_eval_case(eval_case_id)
            item = self._batch_eval_case_eligibility_item(eval_case_id, case)
            linked_cases.append(item)
            if item["eligible"]:
                eligible_linked.add(eval_case_id)
            if item["promotable"]:
                promotable_linked.add(eval_case_id)
        global_eligible: set[str] = set()
        for case in self.list_eval_cases(status="active", promotion_status="approved", limit=500):
            case_id = str(case.get("eval_case_id") or "")
            if case_id and case.get("asset_layer") in ACTIVE_ASSET_LAYERS and case.get("flaky_status") != "flaky":
                global_eligible.add(case_id)
        eligible_total = len(eligible_linked | global_eligible)
        return {
            "batch_id": batch.get("batch_id"),
            "linked_cases": linked_cases,
            "summary": {
                "linked_total": len(linked_case_ids),
                "eligible_linked": len(eligible_linked),
                "eligible_global": len(global_eligible - eligible_linked),
                "eligible_total": eligible_total,
                "promotable_linked": len(promotable_linked),
                "ineligible_linked": len(linked_case_ids) - len(eligible_linked),
                "missing_linked": sum(1 for item in linked_cases if "missing" in item["reasons"]),
            },
        }

    def _batch_eval_case_eligibility_item(self, eval_case_id: str, case: Optional[JsonObject]) -> JsonObject:
        reasons = self._eval_case_regression_ineligibility_reasons(case)
        blockers = [] if case is None else self._batch_eval_case_promotion_blockers(case)
        return {
            "eval_case_id": eval_case_id,
            "status": (case or {}).get("status"),
            "asset_layer": (case or {}).get("asset_layer"),
            "promotion_status": (case or {}).get("promotion_status"),
            "blocking_policy": (case or {}).get("blocking_policy"),
            "flaky_status": (case or {}).get("flaky_status"),
            "eligible": not reasons,
            "promotable": bool(case) and bool(reasons) and not blockers,
            "reasons": reasons,
            "promotion_blockers": blockers,
        }

    def _eval_case_regression_ineligibility_reasons(self, case: Optional[JsonObject]) -> list[str]:
        if not case:
            return ["missing"]
        reasons: list[str] = []
        if case.get("status") != "active":
            reasons.append(f"status:{case.get('status') or 'unknown'}")
        if case.get("promotion_status") != "approved":
            reasons.append(f"promotion_status:{case.get('promotion_status') or 'unknown'}")
        if case.get("asset_layer") not in ACTIVE_ASSET_LAYERS:
            reasons.append(f"asset_layer:{case.get('asset_layer') or 'unknown'}")
        if case.get("flaky_status") == "flaky":
            reasons.append("flaky")
        return reasons

    def _batch_eval_case_promotion_blockers(self, case: JsonObject) -> list[str]:
        blockers: list[str] = []
        if case.get("status") not in {"draft", "active"}:
            blockers.append(f"status:{case.get('status') or 'unknown'}")
        if case.get("promotion_status") in {"rejected", "superseded", "archived"}:
            blockers.append(f"promotion_status:{case.get('promotion_status')}")
        elif case.get("promotion_status") not in {"candidate", "needs_review", "approved"}:
            blockers.append(f"promotion_status:{case.get('promotion_status') or 'unknown'}")
        if case.get("asset_layer") not in {"candidate", "batch_specific"}:
            blockers.append(f"asset_layer:{case.get('asset_layer') or 'unknown'}")
        if case.get("flaky_status") == "flaky":
            blockers.append("flaky")
        return blockers
