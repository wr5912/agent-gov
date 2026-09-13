from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agent_testing.service import AgentTestingService
from app.runtime.agent_workspace_package_schemas import (
    NativeAgentCandidateRequest,
    NativeAgentCandidateResponse,
    NativeAgentCandidateSourceResponse,
    WorkspaceImportResponse,
    WorkspaceRestoreRequest,
    WorkspaceRestoreResponse,
)
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.runtime_gateway.native_schema import validate_native_agent_form_schema
from app.services import agent_workspace_package_codec as package_codec
from app.services.agent_candidate_creation import AgentCandidateCreationService
from app.services.agent_candidate_writer import AgentCandidateWriter
from app.services.agent_change_set_queries import has_open_change_sets
from app.services.agent_governance import TERMINAL_CHANGE_SET_STATES, AgentGovernanceService
from app.services.agent_workspace_packages import AgentWorkspacePackageService

_IMPORT_MULTIPART_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["package"],
                    "properties": {
                        "package": {
                            "type": "string",
                            "format": "binary",
                            "description": (
                                "A .tar.gz archive with exactly one workspace/ root. "
                                "workspace/agent.yaml must declare an agent.id that exactly matches the URL agent_id."
                            ),
                        },
                        "name": {"type": "string", "description": "Required only for a new Agent."},
                        "expected_current_commit_sha": {
                            "type": "string",
                            "description": "Required when overwriting an existing Agent.",
                        },
                        "reason": {"type": "string", "description": "Optional overwrite commit message."},
                    },
                }
            }
        },
    }
}


def create_agent_workspace_packages_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    agent_governance: AgentGovernanceService,
    agent_testing: AgentTestingService,
    runtime_client: AgentScopeRuntimeClient,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_api_key)])
    service = _create_workspace_package_service(
        settings=settings,
        agent_registry_store=agent_registry_store,
        agent_governance=agent_governance,
        agent_testing=agent_testing,
    )
    _register_export_route(router, service)
    _register_import_route(router, service)
    _register_restore_route(router, service)
    _register_native_candidate_source_route(router, service, runtime_client)
    _register_native_candidate_route(router, service, runtime_client)
    return router


def _create_workspace_package_service(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    agent_governance: AgentGovernanceService,
    agent_testing: AgentTestingService,
) -> AgentWorkspacePackageService:
    def open_change_sets(agent_id: str) -> bool:
        return has_open_change_sets(
            agent_governance.feedback_store.Session,
            agent_id=agent_id,
            terminal_states=TERMINAL_CHANGE_SET_STATES,
        )

    candidate_creation = AgentCandidateCreationService(
        settings=settings,
        registry_store=agent_registry_store,
        governance=agent_governance,
        candidate_writer=AgentCandidateWriter(agent_governance),
        has_open_change_sets=open_change_sets,
    )
    return AgentWorkspacePackageService(
        settings=settings,
        registry_store=agent_registry_store,
        store_for=agent_governance._store_for,
        version_maintenance=agent_governance.version_maintenance,
        agent_testing=agent_testing,
        candidate_creation=candidate_creation,
        has_open_change_sets=open_change_sets,
    )


def _register_export_route(router: APIRouter, service: AgentWorkspacePackageService) -> None:
    @router.post(
        "/agent-registry/{agent_id}/workspace/export",
        response_class=FileResponse,
        responses={
            200: {
                "content": {"application/gzip": {}},
                "description": "Current Git-backed workspace package.",
                "headers": {
                    "Content-Disposition": {"schema": {"type": "string"}, "description": "Download filename."},
                    "X-Agent-Commit-SHA": {"schema": {"type": "string"}, "description": "Exported full Git commit SHA."},
                    "X-Workspace-Package-SHA256": {
                        "schema": {"type": "string"},
                        "description": "SHA-256 of the returned .tar.gz bytes.",
                    },
                    "X-Workspace-Tree-SHA256": {
                        "schema": {"type": "string"},
                        "description": "SHA-256 of sorted path/mode/content workspace entries.",
                    },
                },
            }
        },
        summary="Export the current live business-Agent workspace",
    )
    def export_workspace(agent_id: str) -> FileResponse:
        artifact = service.export_workspace(agent_id)
        return FileResponse(
            artifact.path,
            media_type="application/gzip",
            filename=artifact.filename,
            headers={
                "Cache-Control": "no-store",
                "X-Agent-Commit-SHA": artifact.commit_sha,
                "X-Workspace-Package-SHA256": artifact.package_sha256,
                "X-Workspace-Tree-SHA256": artifact.tree_sha256,
            },
            background=BackgroundTask(artifact.path.unlink, missing_ok=True),
        )


def _register_import_route(router: APIRouter, service: AgentWorkspacePackageService) -> None:
    @router.post(
        "/agent-registry/{agent_id}/workspace/import",
        response_model=WorkspaceImportResponse,
        summary="Create or overwrite a business Agent from an exact workspace package",
        openapi_extra=_IMPORT_MULTIPART_SCHEMA,
    )
    async def import_workspace(
        agent_id: str,
        request: Request,
    ) -> WorkspaceImportResponse:
        _require_import_content_length(request)
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise package_codec.WorkspacePackageError(415, "WORKSPACE_PACKAGE_INVALID", "Content-Type must be multipart/form-data")
        try:
            async with request.form(max_files=1, max_fields=3, max_part_size=64 * 1024) as form:
                package = _require_package_upload(form)
                return await run_in_threadpool(
                    lambda: service.import_workspace(
                        agent_id=agent_id,
                        package_file=package.file,
                        filename=package.filename,
                        name=_optional_form_text(form, "name"),
                        expected_current_commit_sha=_optional_form_text(form, "expected_current_commit_sha"),
                        reason=_optional_form_text(form, "reason"),
                    )
                )
        except StarletteHTTPException as exc:
            raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", str(exc.detail)) from exc


def _register_restore_route(router: APIRouter, service: AgentWorkspacePackageService) -> None:
    @router.post(
        "/agent-registry/{agent_id}/workspace/restore",
        response_model=WorkspaceRestoreResponse,
        summary="Restore a historical workspace tree as a new Git commit",
    )
    def restore_workspace(agent_id: str, request: WorkspaceRestoreRequest) -> WorkspaceRestoreResponse:
        return service.restore_workspace(agent_id=agent_id, request=request)


def _register_native_candidate_route(
    router: APIRouter,
    service: AgentWorkspacePackageService,
    runtime_client: AgentScopeRuntimeClient,
) -> None:
    @router.post(
        "/agent-registry/{agent_id}/native-candidate",
        response_model=NativeAgentCandidateResponse,
        summary="Create a Git candidate from the pinned AgentScope AgentData schema",
    )
    async def create_native_candidate(
        agent_id: str,
        request: NativeAgentCandidateRequest,
    ) -> NativeAgentCandidateResponse:
        upstream = await runtime_client.request_json("GET", "/agent/schema/v2")
        try:
            schema = validate_native_agent_form_schema(upstream.body)
        except RuntimeUpstreamError as exc:
            raise package_codec.WorkspacePackageError(502, "NATIVE_AGENT_SCHEMA_UNSUPPORTED", str(exc)) from exc
        return await run_in_threadpool(
            lambda: service.native_candidate(
                agent_id=agent_id,
                agent_data=request.agent_data,
                schema=schema,
                expected_current_commit_sha=request.expected_current_commit_sha,
                change_set_id=request.change_set_id,
                expected_candidate_commit_sha=request.expected_candidate_commit_sha,
                reason=request.reason,
            )
        )


def _register_native_candidate_source_route(
    router: APIRouter,
    service: AgentWorkspacePackageService,
    runtime_client: AgentScopeRuntimeClient,
) -> None:
    @router.get(
        "/agent-registry/{agent_id}/native-candidate-source",
        response_model=NativeAgentCandidateSourceResponse,
        summary="Read safe native AgentData fields from an open candidate or current live Git commit",
    )
    async def native_candidate_source(agent_id: str) -> NativeAgentCandidateSourceResponse:
        upstream = await runtime_client.request_json("GET", "/agent/schema/v2")
        try:
            schema = validate_native_agent_form_schema(upstream.body)
        except RuntimeUpstreamError as exc:
            raise package_codec.WorkspacePackageError(502, "NATIVE_AGENT_SCHEMA_UNSUPPORTED", str(exc)) from exc
        return await run_in_threadpool(
            lambda: service.native_candidate_source(
                agent_id=agent_id,
                schema=schema,
            )
        )


def _require_import_content_length(request: Request) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        raise package_codec.WorkspacePackageError(411, "WORKSPACE_CONTENT_LENGTH_REQUIRED", "Content-Length is required for workspace import")
    try:
        content_length = int(raw)
    except ValueError as exc:
        raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", "Content-Length must be an integer") from exc
    if content_length <= 0:
        raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", "Workspace import request body is empty")
    if content_length > package_codec.MAX_MULTIPART_REQUEST_BYTES:
        raise package_codec.WorkspacePackageError(
            413,
            "WORKSPACE_PACKAGE_TOO_LARGE",
            f"Workspace import request exceeds {package_codec.MAX_MULTIPART_REQUEST_BYTES} bytes",
        )


def _require_package_upload(form: FormData) -> UploadFile:
    allowed = {"package", "name", "expected_current_commit_sha", "reason"}
    counts: dict[str, int] = {}
    for key, _ in form.multi_items():
        if key not in allowed:
            raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", f"Unexpected multipart field: {key}")
        counts[key] = counts.get(key, 0) + 1
    if any(count != 1 for count in counts.values()):
        raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", "Multipart fields must not be repeated")
    package = form.get("package")
    if not isinstance(package, UploadFile):
        raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", "package file is required")
    return package


def _optional_form_text(form: FormData, field: str) -> str | None:
    value = form.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise package_codec.WorkspacePackageError(422, "WORKSPACE_PACKAGE_INVALID", f"{field} must be a text field")
    return value
