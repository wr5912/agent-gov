from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..errors import BusinessRuleViolation
from ..json_types import JsonObject
from ..records.eval_case_records import (
    ASSET_LAYERS,
    BLOCKING_POLICIES,
    FLAKY_STATUSES,
    PROMOTION_STATUSES,
    EvalCaseGovernanceEventRecord,
    EvalCaseRecord,
    EvalCaseRevisionRecord,
    apply_eval_case_record,
)
from ..records.eval_run_records import EvalRunItemRecord, EvalRunRecord
from ..runtime_db import (
    EvalCaseGovernanceEventModel,
    EvalCaseModel,
    EvalCaseRevisionModel,
    EvalRunItemModel,
    EvalRunModel,
    utc_now,
)


class FeedbackRegressionAssetStoreMixin:
    def _add_eval_case_row(self, db: Any, payload: JsonObject) -> None:
        record = EvalCaseRecord.model_validate(self._eval_case_with_asset_defaults(payload))
        db.add(
            EvalCaseModel(
                eval_case_id=record.eval_case_id,
                created_at=record.created_at,
                updated_at=record.updated_at,
                status=record.status,
                source_feedback_case_id=record.source_feedback_case_id,
                source_run_id=record.source_run_id,
                asset_layer=record.asset_layer,
                promotion_status=record.promotion_status,
                blocking_policy=record.blocking_policy,
                scenario_pack=record.scenario_pack,
                severity=record.severity,
                flaky_status=record.flaky_status,
                variant_role=record.variant_role,
                content_hash=record.content_hash,
                last_run_at=record.last_run_at,
                last_result_status=record.last_result_status,
                failure_rate=record.failure_rate,
                superseded_by_eval_case_id=record.superseded_by_eval_case_id,
                labels_json=list(record.labels),
                payload_json=record.to_payload(),
            )
        )
        self._add_eval_case_revision_row(db, record.to_payload(), created_by="system", reason="initial")

    def list_eval_cases(
        self,
        *,
        status: Optional[str] = None,
        source_feedback_case_id: Optional[str] = None,
        asset_layer: Optional[str] = None,
        promotion_status: Optional[str] = None,
        blocking_policy: Optional[str] = None,
        scenario_pack: Optional[str] = None,
        flaky_status: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(EvalCaseModel).order_by(EvalCaseModel.updated_at.desc()).limit(limit)
        for column, value in (
            (EvalCaseModel.status, status),
            (EvalCaseModel.source_feedback_case_id, source_feedback_case_id),
            (EvalCaseModel.asset_layer, asset_layer),
            (EvalCaseModel.promotion_status, promotion_status),
            (EvalCaseModel.blocking_policy, blocking_policy),
            (EvalCaseModel.scenario_pack, scenario_pack),
            (EvalCaseModel.flaky_status, flaky_status),
        ):
            if value:
                stmt = stmt.where(column == value)
        with self.Session() as db:
            return [self._eval_case_to_dict(row) for row in db.scalars(stmt).all()]

    def find_eval_case(
        self,
        eval_case_id: Optional[str] = None,
        *,
        source_feedback_case_id: Optional[str] = None,
    ) -> Optional[JsonObject]:
        with self.Session() as db:
            row: EvalCaseModel | None = None
            if eval_case_id:
                row = db.get(EvalCaseModel, eval_case_id)
            elif source_feedback_case_id:
                row = db.scalars(
                    select(EvalCaseModel).where(EvalCaseModel.source_feedback_case_id == source_feedback_case_id).order_by(EvalCaseModel.updated_at.desc())
                ).first()
            return self._eval_case_to_dict(row) if row else None

    def update_eval_case(self, eval_case_id: str, fields: JsonObject) -> Optional[JsonObject]:
        updated_at = utc_now()
        operator = (self._string(fields.get("operator")) or "system").strip()
        reason = (self._string(fields.get("reason")) or "eval case updated").strip()
        with self.Session.begin() as db:
            row = db.get(EvalCaseModel, eval_case_id)
            if not row:
                return None
            before_record = EvalCaseRecord.from_row(row)
            before = before_record.to_payload()
            payload = dict(before)
            self._apply_eval_case_update_fields(payload, fields)
            payload["updated_at"] = updated_at
            record = EvalCaseRecord.model_validate(self._eval_case_with_asset_defaults(payload))
            before_record.transition_to(status=record.status, promotion_status=record.promotion_status)
            apply_eval_case_record(row, record)
            self._add_eval_case_revision_row(db, record.to_payload(), created_by=operator, reason=reason)
            self._add_eval_case_governance_event_row(
                db,
                eval_case_id=eval_case_id,
                action=str(fields.get("action") or "update"),
                operator=operator,
                role=str(fields.get("role") or "developer"),
                reason=reason,
                before=before,
                after=record.to_payload(),
            )
        return self.find_eval_case(eval_case_id)

    def promote_eval_case(self, eval_case_id: str, fields: JsonObject) -> Optional[JsonObject]:
        asset_layer = self._string(fields.get("asset_layer")) or "core_regression"
        blocking_policy = self._string(fields.get("blocking_policy")) or (
            "blocking" if asset_layer in {"targeted_regression", "smoke", "safety"} else "blocking_if_relevant"
        )
        return self.update_eval_case(
            eval_case_id,
            {
                **fields,
                "action": "promote",
                "status": "active",
                "promotion_status": "approved",
                "asset_layer": asset_layer,
                "blocking_policy": blocking_policy,
            },
        )

    def archive_eval_case(self, eval_case_id: str, fields: JsonObject) -> Optional[JsonObject]:
        return self.update_eval_case(
            eval_case_id,
            {**fields, "action": "archive", "status": "archived", "promotion_status": "archived"},
        )

    def mark_eval_case_flaky(self, eval_case_id: str, fields: JsonObject, *, flaky: bool) -> Optional[JsonObject]:
        return self.update_eval_case(
            eval_case_id,
            {**fields, "action": "mark_flaky" if flaky else "unmark_flaky", "flaky_status": "flaky" if flaky else "stable"},
        )

    def supersede_eval_case(self, eval_case_id: str, fields: JsonObject) -> Optional[JsonObject]:
        target_id = self._string(fields.get("superseded_by_eval_case_id"))
        if not target_id:
            raise BusinessRuleViolation("superseded_by_eval_case_id is required")
        if not self.find_eval_case(target_id):
            raise BusinessRuleViolation("superseded target eval case not found")
        return self.update_eval_case(
            eval_case_id,
            {
                **fields,
                "action": "supersede",
                "status": "archived",
                "promotion_status": "superseded",
                "superseded_by_eval_case_id": target_id,
            },
        )

    def list_eval_case_revisions(self, eval_case_id: str) -> list[JsonObject]:
        with self.Session() as db:
            rows = db.scalars(
                select(EvalCaseRevisionModel).where(EvalCaseRevisionModel.eval_case_id == eval_case_id).order_by(EvalCaseRevisionModel.revision_number.desc())
            ).all()
            return [self._eval_case_revision_to_dict(row) for row in rows]

    def list_eval_case_governance_events(self, eval_case_id: str) -> list[JsonObject]:
        with self.Session() as db:
            rows = db.scalars(
                select(EvalCaseGovernanceEventModel)
                .where(EvalCaseGovernanceEventModel.eval_case_id == eval_case_id)
                .order_by(EvalCaseGovernanceEventModel.created_at.desc())
            ).all()
            return [self._eval_case_governance_event_to_dict(row) for row in rows]

    def _update_eval_case_row(self, db: Any, payload: JsonObject) -> bool:
        row = db.get(EvalCaseModel, payload["eval_case_id"])
        if not row:
            return False
        before = EvalCaseRecord.from_row(row)
        record = EvalCaseRecord.model_validate(self._eval_case_with_asset_defaults(payload))
        before.transition_to(status=record.status, promotion_status=record.promotion_status)
        apply_eval_case_record(row, record)
        self._add_eval_case_revision_row(db, record.to_payload(), created_by="system", reason="sync")
        return True

    def _eval_case_to_dict(self, row: EvalCaseModel) -> JsonObject:
        return EvalCaseRecord.from_row(row).to_payload()

    def _eval_case_with_asset_defaults(self, payload: JsonObject) -> JsonObject:
        normalized = dict(payload)
        labels = self._unique_strings([str(item).strip() for item in normalized.get("labels") or [] if str(item).strip()])
        normalized["labels"] = labels
        source = self._string(normalized.get("source"))
        source_kind = self._string(normalized.get("source_kind"))
        is_batch_manual = source == "optimization_batch_manual" or source_kind == "optimization_batch"
        status = self._string(normalized.get("status")) or ("active" if is_batch_manual else "draft")
        if status not in {"active", "draft", "archived"}:
            raise BusinessRuleViolation("Eval case status must be active, draft, or archived")
        normalized["status"] = status
        normalized["asset_layer"] = self._defaulted_enum(
            normalized.get("asset_layer"),
            ASSET_LAYERS,
            "targeted_regression" if is_batch_manual else "candidate",
            "asset_layer",
        )
        normalized["promotion_status"] = self._defaulted_enum(
            normalized.get("promotion_status"),
            PROMOTION_STATUSES,
            "approved" if status == "active" and is_batch_manual else ("archived" if status == "archived" else "candidate"),
            "promotion_status",
        )
        normalized["blocking_policy"] = self._defaulted_enum(
            normalized.get("blocking_policy"),
            BLOCKING_POLICIES,
            self._default_blocking_policy(normalized["asset_layer"], normalized["promotion_status"], status),
            "blocking_policy",
        )
        normalized["flaky_status"] = self._defaulted_enum(normalized.get("flaky_status"), FLAKY_STATUSES, "stable", "flaky_status")
        normalized["severity"] = (self._string(normalized.get("severity")) or "medium").strip() or "medium"
        normalized["variant_role"] = (self._string(normalized.get("variant_role")) or "original_reproduction").strip()
        normalized["scenario_pack"] = self._string(normalized.get("scenario_pack")) or None
        normalized["superseded_by_eval_case_id"] = self._string(normalized.get("superseded_by_eval_case_id")) or None
        normalized["content_hash"] = self._eval_case_content_hash(normalized)
        return normalized

    def _apply_eval_case_update_fields(self, payload: JsonObject, fields: JsonObject) -> None:
        if "prompt" in fields:
            prompt = (self._string(fields.get("prompt")) or "").strip()
            if not prompt:
                raise BusinessRuleViolation("Eval case prompt cannot be empty")
            payload["prompt"] = prompt
        if "expected_behavior" in fields:
            payload["expected_behavior"] = (self._string(fields.get("expected_behavior")) or "").strip()
        if "checks_json" in fields:
            checks = fields.get("checks_json")
            if checks is not None and not isinstance(checks, dict):
                raise BusinessRuleViolation("Eval case checks_json must be an object")
            payload["checks_json"] = dict(checks or {})
        if "labels" in fields:
            labels = fields.get("labels")
            if labels is not None and not isinstance(labels, list):
                raise BusinessRuleViolation("Eval case labels must be a list")
            payload["labels"] = self._unique_strings([str(item).strip() for item in labels or [] if str(item).strip()])
        if "status" in fields:
            payload["status"] = self._defaulted_enum(fields.get("status"), {"active", "draft", "archived"}, "draft", "status")
        for field_name, allowed in {
            "asset_layer": ASSET_LAYERS,
            "promotion_status": PROMOTION_STATUSES,
            "blocking_policy": BLOCKING_POLICIES,
            "flaky_status": FLAKY_STATUSES,
        }.items():
            if field_name in fields:
                payload[field_name] = self._defaulted_enum(fields.get(field_name), allowed, "", field_name)
        if "scenario_pack" in fields:
            payload["scenario_pack"] = self._string(fields.get("scenario_pack")) or None
        if "severity" in fields:
            payload["severity"] = (self._string(fields.get("severity")) or "medium").strip() or "medium"
        if "variant_role" in fields:
            payload["variant_role"] = (self._string(fields.get("variant_role")) or "original_reproduction").strip()
        if "superseded_by_eval_case_id" in fields:
            payload["superseded_by_eval_case_id"] = self._string(fields.get("superseded_by_eval_case_id")) or None

    def _add_eval_case_revision_row(self, db: Any, payload: JsonObject, *, created_by: str, reason: str) -> None:
        eval_case_id = str(payload["eval_case_id"])
        latest = db.scalars(
            select(EvalCaseRevisionModel).where(EvalCaseRevisionModel.eval_case_id == eval_case_id).order_by(EvalCaseRevisionModel.revision_number.desc())
        ).first()
        record = EvalCaseRevisionRecord.model_validate(
            {
                "revision_id": f"ecr-{uuid.uuid4()}",
                "eval_case_id": eval_case_id,
                "revision_number": (latest.revision_number if latest else 0) + 1,
                "created_at": utc_now(),
                "created_by": created_by,
                "reason": reason,
                "content_hash": payload.get("content_hash"),
                "snapshot": dict(payload),
            }
        )
        db.add(
            EvalCaseRevisionModel(
                revision_id=record.revision_id,
                eval_case_id=record.eval_case_id,
                revision_number=record.revision_number,
                created_at=record.created_at,
                created_by=record.created_by,
                reason=record.reason,
                content_hash=record.content_hash,
                snapshot_json=record.snapshot,
            )
        )

    def _add_eval_case_governance_event_row(
        self,
        db: Any,
        *,
        eval_case_id: str,
        action: str,
        operator: str,
        role: str,
        reason: str,
        before: JsonObject,
        after: JsonObject,
    ) -> None:
        record = EvalCaseGovernanceEventRecord.model_validate(
            {
                "event_id": f"ecg-{uuid.uuid4()}",
                "eval_case_id": eval_case_id,
                "action": action,
                "operator": operator,
                "role": role,
                "reason": reason,
                "created_at": utc_now(),
                "before": before,
                "after": after,
            }
        )
        db.add(
            EvalCaseGovernanceEventModel(
                event_id=record.event_id,
                eval_case_id=record.eval_case_id,
                action=record.action,
                operator=record.operator,
                role=record.role,
                reason=record.reason,
                created_at=record.created_at,
                before_json=record.before,
                after_json=record.after,
            )
        )

    def _eval_case_revision_to_dict(self, row: EvalCaseRevisionModel) -> JsonObject:
        return EvalCaseRevisionRecord.from_row(row).to_payload()

    def _eval_case_governance_event_to_dict(self, row: EvalCaseGovernanceEventModel) -> JsonObject:
        return EvalCaseGovernanceEventRecord.from_row(row).to_payload()

    def _gate_result_for_items(self, items: list[EvalRunItemModel]) -> JsonObject:
        blocked: list[str] = []
        review: list[str] = []
        notes: list[str] = []
        for item in items:
            record = EvalRunItemRecord.from_row(item)
            snapshot = record.eval_case_snapshot
            policy = str(snapshot.get("blocking_policy") or "non_blocking")
            case_id = str(item.eval_case_id)
            if item.status == "needs_human_review":
                review.append(case_id)
            elif item.status == "failed" and policy == "blocking":
                blocked.append(case_id)
            elif item.status == "failed" and policy == "blocking_if_relevant":
                review.append(case_id)
            elif item.status == "failed":
                notes.append(case_id)
        if blocked:
            status = "blocked"
        elif review:
            status = "review_required"
        elif notes:
            status = "passed_with_notes"
        else:
            status = "passed"
        return {"status": status, "blocked_case_ids": blocked, "review_case_ids": review, "note_case_ids": notes}

    def _update_eval_case_run_stats(self, db: Any, items: list[EvalRunItemModel], completed_at: str) -> None:
        by_case: dict[str, list[str]] = {}
        for item in items:
            by_case.setdefault(str(item.eval_case_id), []).append(str(item.status))
        for eval_case_id, statuses in by_case.items():
            row = db.get(EvalCaseModel, eval_case_id)
            if not row:
                continue
            payload = EvalCaseRecord.from_row(row).to_payload()
            payload["last_run_at"] = completed_at
            payload["last_result_status"] = "failed" if "failed" in statuses else ("needs_human_review" if "needs_human_review" in statuses else "passed")
            payload["failure_rate"] = statuses.count("failed") / len(statuses)
            record = EvalCaseRecord.model_validate(self._eval_case_with_asset_defaults(payload))
            apply_eval_case_record(row, record)

    def _default_blocking_policy(self, asset_layer: str, promotion_status: str, status: str) -> str:
        if status != "active" or promotion_status != "approved":
            return "non_blocking"
        if asset_layer in {"targeted_regression", "smoke", "safety"}:
            return "blocking"
        if asset_layer in {"core_regression", "scenario_pack", "historical_bug"}:
            return "blocking_if_relevant"
        return "non_blocking"

    def _defaulted_enum(self, value: Any, allowed: set[str], default: str, field_name: str) -> str:
        text = (self._string(value) or default).strip()
        if text not in allowed:
            raise BusinessRuleViolation(f"Invalid eval case {field_name}: {text}")
        return text

    def _eval_case_content_hash(self, payload: JsonObject) -> str:
        stable = {
            "prompt": payload.get("prompt"),
            "expected_behavior": payload.get("expected_behavior"),
            "checks_json": payload.get("checks_json") or {},
            "labels": sorted(str(item) for item in payload.get("labels") or []),
            "asset_layer": payload.get("asset_layer"),
            "source_feedback_case_id": payload.get("source_feedback_case_id"),
            "source_kind": payload.get("source_kind"),
            "source_id": payload.get("source_id"),
            "variant_role": payload.get("variant_role"),
        }
        encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
