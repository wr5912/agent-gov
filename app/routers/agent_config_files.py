from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from app.runtime.config_file_schemas import (
    AgentCandidateFileResponse,
    AgentCandidateFilesWriteRequest,
    AgentCandidateFilesWriteResponse,
)
from app.services.agent_candidate_writer import AgentCandidateWriteError, AgentCandidateWriter


def create_agent_config_files_router(
    *,
    candidate_writer: AgentCandidateWriter,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["config"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agent-change-sets/{change_set_id}/files",
        response_model=AgentCandidateFileResponse,
        summary="Read one UTF-8 file from an isolated unpublished Agent candidate",
    )
    def read_agent_candidate_file(
        change_set_id: str,
        path: str = Query(description="Editable Harness path: agent.yaml, AGENT.md, or mcp/<name>.json."),
    ) -> AgentCandidateFileResponse:
        try:
            return candidate_writer.read_text_file(change_set_id=change_set_id, path=path)
        except AgentCandidateWriteError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @router.put(
        "/agent-change-sets/{change_set_id}/files",
        response_model=AgentCandidateFilesWriteResponse,
        summary="Commit one or more files to an isolated unpublished Agent candidate",
    )
    def update_agent_candidate_files(
        change_set_id: str,
        request: AgentCandidateFilesWriteRequest,
    ) -> AgentCandidateFilesWriteResponse:
        try:
            return candidate_writer.write_text_files(
                change_set_id=change_set_id,
                files=tuple(
                    (item.path, item.content, item.expected_sha256, item.mode)
                    for item in request.files
                ),
                expected_candidate_commit_sha=request.expected_candidate_commit_sha,
                operator=request.operator,
                note=request.note,
            )
        except AgentCandidateWriteError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return router
