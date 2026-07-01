from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, require_request
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.schemas import (
    EvidencePackageFileResponse,
    EvidencePackageResponse,
    FeedbackCaseCreateRequest,
    FeedbackCaseResponse,
)


def create_feedback_cases_router(
    *,
    feedback_store: FeedbackStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_case_routes(router, feedback_store)
    _register_evidence_routes(router, feedback_store)
    return router


def _register_case_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/feedback-cases",
        response_model=list[FeedbackCaseResponse],
        summary="List feedback disposition cases",
    )
    async def list_feedback_cases(
        agent_id: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[FeedbackCaseResponse]:
        return feedback_store.list_cases(agent_id=agent_id, status=status, q=q, limit=limit)

    @router.get(
        "/feedback-cases/{feedback_case_id}",
        response_model=FeedbackCaseResponse,
        summary="Get one feedback disposition case",
    )
    async def get_feedback_case(feedback_case_id: str) -> FeedbackCaseResponse:
        feedback_case = feedback_store.find_case(feedback_case_id)
        return ensure_found(feedback_case, "Feedback case not found")

    @router.post(
        "/feedback-cases",
        response_model=FeedbackCaseResponse,
        summary="Create one feedback disposition case from feedback signals",
    )
    async def create_feedback_case(req: FeedbackCaseCreateRequest) -> FeedbackCaseResponse:
        require_request(bool(req.source_ids), "source_ids is required")
        feedback_case = feedback_store.create_case(source_ids=req.source_ids, title=req.title, priority=req.priority)
        return ensure_found(feedback_case, "Feedback source not found")


def _register_evidence_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.post(
        "/feedback-cases/{feedback_case_id}/evidence-packages",
        response_model=EvidencePackageResponse,
        summary="Create one immutable evidence package for a feedback case",
    )
    async def create_evidence_package(feedback_case_id: str) -> EvidencePackageResponse:
        evidence_package = feedback_store.create_evidence_package(feedback_case_id)
        return ensure_found(evidence_package, "Feedback case not found")

    @router.get(
        "/evidence-packages/{evidence_package_id}",
        response_model=EvidencePackageResponse,
        summary="Get one evidence package manifest",
    )
    async def get_evidence_package(evidence_package_id: str) -> EvidencePackageResponse:
        evidence_package = feedback_store.get_evidence_package(evidence_package_id)
        return ensure_found(evidence_package, "Evidence package not found")

    @router.get(
        "/evidence-packages/{evidence_package_id}/files/{file_name}",
        response_model=EvidencePackageFileResponse,
        summary="Get one evidence package JSON file",
    )
    async def get_evidence_package_file(evidence_package_id: str, file_name: str) -> EvidencePackageFileResponse:
        evidence_file = feedback_store.get_evidence_package_file(evidence_package_id, file_name)
        return ensure_found(evidence_file, "Evidence package file not found")
