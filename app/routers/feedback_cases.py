from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, require_request
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_analysis_response_schemas import FeedbackAnalysisJobResponse
from app.runtime.feedback_output_response_schemas import AttributionOutputResponse, ProposalOutputResponse
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import (
    EvidencePackageFileResponse,
    EvidencePackageResponse,
    FeedbackCaseCreateRequest,
    FeedbackCaseResponse,
    FeedbackProposalRegenerateRequest,
)


def create_feedback_cases_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/feedback-cases",
        response_model=list[FeedbackCaseResponse],
        summary="List feedback disposition cases",
    )
    async def list_feedback_cases(
        status: str | None = None,
        q: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_cases(status=status, q=q, limit=limit)

    @router.get(
        "/feedback-cases/{feedback_case_id}",
        response_model=FeedbackCaseResponse,
        summary="Get one feedback disposition case",
    )
    async def get_feedback_case(feedback_case_id: str) -> dict[str, Any]:
        feedback_case = feedback_store.find_case(feedback_case_id)
        return ensure_found(feedback_case, "Feedback case not found")

    @router.post(
        "/feedback-cases",
        response_model=FeedbackCaseResponse,
        summary="Create one feedback disposition case from feedback signals",
    )
    async def create_feedback_case(req: FeedbackCaseCreateRequest) -> dict[str, Any]:
        require_request(bool(req.source_ids), "source_ids is required")
        feedback_case = feedback_store.create_case(source_ids=req.source_ids, title=req.title, priority=req.priority)
        return ensure_found(feedback_case, "Feedback source not found")

    @router.post(
        "/feedback-cases/{feedback_case_id}/evidence-packages",
        response_model=EvidencePackageResponse,
        summary="Create one immutable evidence package for a feedback case",
    )
    async def create_evidence_package(feedback_case_id: str) -> dict[str, Any]:
        evidence_package = feedback_store.create_evidence_package(feedback_case_id)
        return ensure_found(evidence_package, "Feedback case not found")

    @router.get(
        "/evidence-packages/{evidence_package_id}",
        response_model=EvidencePackageResponse,
        summary="Get one evidence package manifest",
    )
    async def get_evidence_package(evidence_package_id: str) -> dict[str, Any]:
        evidence_package = feedback_store.get_evidence_package(evidence_package_id)
        return ensure_found(evidence_package, "Evidence package not found")

    @router.get(
        "/evidence-packages/{evidence_package_id}/files/{file_name}",
        response_model=EvidencePackageFileResponse,
        summary="Get one evidence package JSON file",
    )
    async def get_evidence_package_file(evidence_package_id: str, file_name: str) -> dict[str, Any]:
        evidence_file = feedback_store.get_evidence_package_file(evidence_package_id, file_name)
        return ensure_found(evidence_file, "Evidence package file not found")

    @router.post(
        "/feedback-cases/{feedback_case_id}/attribution-jobs",
        response_model=FeedbackAnalysisJobResponse,
        summary="Run one attribution job for a feedback case",
    )
    async def create_attribution_job(feedback_case_id: str) -> dict[str, Any]:
        job = await runtime.run_attribution_job(feedback_case_id)
        return ensure_found(job, "Feedback case not found or missing evidence")

    @router.post(
        "/feedback-cases/{feedback_case_id}/attribution-jobs/regenerate",
        response_model=FeedbackAnalysisJobResponse,
        summary="Force regenerate one attribution job for a feedback case",
    )
    async def regenerate_attribution_job(feedback_case_id: str) -> dict[str, Any]:
        job = await runtime.run_attribution_job(feedback_case_id, force=True)
        return ensure_found(job, "Feedback case not found or missing evidence")

    @router.post(
        "/feedback-cases/{feedback_case_id}/proposal-jobs",
        response_model=FeedbackAnalysisJobResponse,
        summary="Run one optimization proposal job for a feedback case",
    )
    async def create_proposal_job(feedback_case_id: str) -> dict[str, Any]:
        job = await runtime.run_proposal_job(feedback_case_id)
        return ensure_found(job, "Feedback case not found or missing attribution")

    @router.post(
        "/feedback-cases/{feedback_case_id}/proposal-jobs/regenerate",
        response_model=FeedbackAnalysisJobResponse,
        summary="Force regenerate one optimization proposal job and supersede unused existing proposals",
    )
    async def regenerate_proposal_job(feedback_case_id: str, req: FeedbackProposalRegenerateRequest | None = None) -> dict[str, Any]:
        job = await runtime.run_proposal_job(
            feedback_case_id,
            force=True,
            regeneration_instruction=req.regeneration_instruction if req else None,
        )
        return ensure_found(job, "Feedback case not found or missing attribution")

    @router.get(
        "/feedback-analysis/jobs/{job_id}",
        response_model=FeedbackAnalysisJobResponse,
        summary="Get one feedback analysis job",
    )
    async def get_feedback_analysis_job(job_id: str) -> dict[str, Any]:
        job = feedback_store.get_job(job_id)
        return ensure_found(job, "Feedback analysis job not found")

    @router.get(
        "/feedback-analysis/jobs/{job_id}/attribution",
        response_model=AttributionOutputResponse,
        summary="Get one attribution job validated output",
    )
    async def get_attribution_output(job_id: str) -> dict[str, Any]:
        output = feedback_store.get_job_output(job_id, "attribution")
        return ensure_found(output, "Attribution output not found")

    @router.get(
        "/feedback-analysis/jobs/{job_id}/proposal",
        response_model=ProposalOutputResponse,
        summary="Get one proposal job validated output",
    )
    async def get_proposal_output(job_id: str) -> dict[str, Any]:
        output = feedback_store.get_job_output(job_id, "proposal")
        return ensure_found(output, "Proposal output not found")

    @router.post(
        "/feedback-analysis/jobs/{job_id}/proposal/revalidate",
        response_model=FeedbackAnalysisJobResponse,
        summary="Revalidate one proposal job raw output without rerunning the Agent",
    )
    async def revalidate_proposal_output(job_id: str) -> dict[str, Any]:
        job = feedback_store.revalidate_proposal_job(job_id)
        return ensure_found(job, "Proposal job raw output not found")

    return router
