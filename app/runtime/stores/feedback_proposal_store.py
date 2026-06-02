from __future__ import annotations

import uuid
from typing import Any, Optional, TypeAlias

from sqlalchemy import select

from ..external_governance_mapping import apply_external_governance_record, external_governance_record_from_row
from ..records.json_types import JsonObject
from ..records.proposal_records import OptimizationProposalRecord, ProposalReviewRecord
from ..runtime_db import ExternalGovernanceItemModel, OptimizationProposalModel, ProposalReviewModel, utc_now


ProposalReviewsByProposalId: TypeAlias = dict[str, ProposalReviewModel]
ProposalSupersedeCounts: TypeAlias = dict[str, int]


class FeedbackProposalStoreMixin:
    """Store operations for optimization proposal records and reviews."""

    def _normalize_proposal_output(self, output: JsonObject, job: JsonObject) -> JsonObject:
        normalized = {**output, "proposals": [], "external_guidance": list(output.get("external_guidance") or [])}
        for item in output.get("proposals") or []:
            target_path = self._string(item.get("target_path"))
            actionability = self._string(item.get("actionability")) or "needs_human_analysis"
            if not target_path or not self._target_allowed(target_path):
                normalized["external_guidance"].append(
                    {
                        "owner": item.get("target_type") or "needs_human_analysis",
                        "actionability": "needs_human_analysis",
                        "recommendation": item.get("recommendation") or "建议目标路径不在允许范围内，需人工分析。",
                        "reason": "TARGET_PATH_NOT_ALLOWED",
                    }
                )
                continue
            normalized["proposals"].append(
                {
                    **item,
                    "proposal_id": item.get("proposal_id") or f"opp-{uuid.uuid4()}",
                    "created_at": utc_now(),
                    "feedback_case_id": job["feedback_case_id"],
                    "proposal_job_id": job["job_id"],
                    "status": "pending_review",
                    "actionability": actionability,
                    "base_agent_version_id": self._current_agent_version_id(),
                }
            )
        if not normalized["proposals"] and not normalized["external_guidance"]:
            normalized["no_action_reason"] = normalized.get("no_action_reason") or "NO_ACTIONABLE_PROPOSAL"
        return normalized

    def list_proposals(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(OptimizationProposalModel).order_by(OptimizationProposalModel.created_at.desc()).limit(limit)
        if feedback_case_id:
            stmt = stmt.where(OptimizationProposalModel.feedback_case_id == feedback_case_id)
        if status:
            stmt = stmt.where(OptimizationProposalModel.status == status)
        else:
            stmt = stmt.where(OptimizationProposalModel.status != "superseded")
        with self.Session() as db:
            rows = db.scalars(stmt).all()
            latest_reviews = self._latest_reviews_by_proposal_ids(db, [row.proposal_id for row in rows])
            return [self._proposal_to_dict(row, latest_reviews.get(row.proposal_id)) for row in rows]

    def find_proposal(self, proposal_id: str) -> Optional[JsonObject]:
        if not proposal_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationProposalModel, proposal_id)
            if not row:
                return None
            latest_review = self._latest_reviews_by_proposal_ids(db, [proposal_id]).get(proposal_id)
            return self._proposal_to_dict(row, latest_review)

    def review_proposal(self, proposal_id: str, *, action: str, comment: Optional[str] = None) -> Optional[JsonObject]:
        proposal = self.find_proposal(proposal_id)
        if not proposal:
            return None
        status_by_action = {"approve": "approved", "reject": "rejected", "request_more_analysis": "needs_more_analysis"}
        next_status = status_by_action[action]
        review = ProposalReviewRecord.model_validate(
            self._scrub_record(
                {
                    "review_id": f"opr-{uuid.uuid4()}",
                    "proposal_id": proposal_id,
                    "created_at": utc_now(),
                    "action": action,
                    "status": next_status,
                    "comment": comment,
                }
            )
        )
        with self.Session.begin() as db:
            db.add(
                ProposalReviewModel(
                    review_id=review.review_id,
                    proposal_id=review.proposal_id,
                    created_at=review.created_at,
                    action=review.action,
                    status=review.status,
                    payload_json=review.to_payload(),
                )
            )
            row = db.get(OptimizationProposalModel, proposal_id)
            if row:
                record = OptimizationProposalRecord.from_row(row).transition_to(
                    next_status,
                    fields={"latest_review": review.to_payload()},
                )
                self._apply_proposal_record(row, record)
        updated = self.find_proposal(proposal_id) or {**proposal, "status": next_status, "latest_review": review.to_payload()}
        return {"proposal": updated, "review": review.to_payload()}

    def _proposal_model_from_dict(self, proposal: JsonObject) -> OptimizationProposalModel:
        record = OptimizationProposalRecord.model_validate(proposal)
        return OptimizationProposalModel(
            proposal_id=record.proposal_id,
            feedback_case_id=record.feedback_case_id,
            proposal_job_id=record.proposal_job_id,
            status=record.status,
            actionability=record.actionability,
            target_path=record.target_path,
            created_at=record.created_at,
            payload_json=record.to_payload(),
        )

    def _proposal_to_dict(self, row: OptimizationProposalModel, latest_review: ProposalReviewModel | None = None) -> JsonObject:
        review = ProposalReviewRecord.from_row(latest_review) if latest_review else None
        return OptimizationProposalRecord.from_row(row, latest_review=review).to_payload()

    @staticmethod
    def _latest_reviews_by_proposal_ids(db: Any, proposal_ids: list[str]) -> ProposalReviewsByProposalId:
        if not proposal_ids:
            return {}
        rows = db.scalars(
            select(ProposalReviewModel)
            .where(ProposalReviewModel.proposal_id.in_(proposal_ids))
            .order_by(ProposalReviewModel.proposal_id, ProposalReviewModel.created_at.desc())
        ).all()
        latest: ProposalReviewsByProposalId = {}
        for row in rows:
            latest.setdefault(row.proposal_id, row)
        return latest

    def _supersede_case_proposals(
        self,
        feedback_case_id: str,
        *,
        reason: str,
        superseded_by_job_id: str,
    ) -> ProposalSupersedeCounts:
        superseded_at = utc_now()
        proposal_count = 0
        external_count = 0
        with self.Session.begin() as db:
            proposals = db.scalars(
                select(OptimizationProposalModel).where(
                    OptimizationProposalModel.feedback_case_id == feedback_case_id,
                    OptimizationProposalModel.status.in_(("pending_review", "needs_more_analysis")),
                )
            ).all()
            for row in proposals:
                record = OptimizationProposalRecord.from_row(row).transition_to(
                    "superseded",
                    fields={
                        "superseded_at": superseded_at,
                        "superseded_reason": reason,
                        "superseded_by_job_id": superseded_by_job_id,
                    },
                )
                self._apply_proposal_record(row, record)
                proposal_count += 1

            external_items = db.scalars(
                select(ExternalGovernanceItemModel).where(
                    ExternalGovernanceItemModel.feedback_case_id == feedback_case_id,
                    ExternalGovernanceItemModel.status.in_(("pending_notification", "notification_failed")),
                )
            ).all()
            for row in external_items:
                record = external_governance_record_from_row(row).mark_superseded(
                    updated_at=superseded_at,
                    reason=reason,
                    superseded_by_job_id=superseded_by_job_id,
                )
                apply_external_governance_record(row, record)
                external_count += 1
        return {"proposals": proposal_count, "external_guidance_items": external_count}

    @staticmethod
    def _apply_proposal_record(row: OptimizationProposalModel, record: OptimizationProposalRecord) -> None:
        row.status = record.status
        row.actionability = record.actionability
        row.target_path = record.target_path
        row.payload_json = record.to_payload()
