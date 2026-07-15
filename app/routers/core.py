from __future__ import annotations

import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, FastAPI, Response, status

from app.runtime.model_provider import ModelProviderRouter
from app.runtime.schemas import (
    RuntimeDependencyVersions,
    RuntimeHealthResponse,
    RuntimeLivenessResponse,
    RuntimeReadinessResponse,
    RuntimeRootResponse,
)
from app.runtime.settings import AppSettings
from app.version import APP_VERSION


def create_core_router(
    *,
    settings: AppSettings,
    app: FastAPI,
    model_provider_router: ModelProviderRouter,
) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def root() -> RuntimeRootResponse:
        return RuntimeRootResponse(
            name="AgentGov API",
            health="/health",
            liveness="/health/live",
            readiness="/health/ready",
            docs=app.docs_url,
            redoc=app.redoc_url,
            openapi=app.openapi_url,
        )

    @router.get(
        "/health/live",
        tags=["health"],
        response_model=RuntimeLivenessResponse,
        summary="Check API process liveness without external dependencies",
    )
    async def liveness() -> RuntimeLivenessResponse:
        return RuntimeLivenessResponse(runtime_version=APP_VERSION)

    @router.get(
        "/health/ready",
        tags=["health"],
        response_model=RuntimeReadinessResponse,
        responses={503: {"model": RuntimeReadinessResponse}},
        summary="Read cached model provider readiness without starting a probe",
    )
    async def readiness(response: Response) -> RuntimeReadinessResponse:
        provider = model_provider_router.readiness_summary()
        ready = provider.get("status") == "ready"
        response.status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return RuntimeReadinessResponse(
            status="ready" if ready else "not_ready",
            runtime_version=APP_VERSION,
            model_provider=provider,
        )

    @router.get(
        "/health",
        tags=["health"],
        response_model=RuntimeHealthResponse,
        summary="Check service health and discover API documentation URLs",
    )
    async def health() -> RuntimeHealthResponse:
        return build_health_payload(
            settings=settings,
            app=app,
            model_provider_router=model_provider_router,
        )

    return router


def build_health_payload(
    *,
    settings: AppSettings,
    app: FastAPI,
    model_provider_router: ModelProviderRouter | None = None,
) -> RuntimeHealthResponse:
    provider_router = model_provider_router or ModelProviderRouter(settings)
    return RuntimeHealthResponse(
        status="ok",
        api_host=settings.api_host,
        api_port=settings.api_port,
        host_port=settings.host_port,
        workspace_dir=str(settings.workspace_dir),
        data_dir=str(settings.data_dir),
        runtime_db_backend="sqlite",
        runtime_db_path=str(settings.runtime_db_path),
        claude_root=str(settings.claude_root),
        claude_home=str(settings.claude_home),
        claude_config_mode=settings.claude_config_mode,
        claude_config_dir=str(settings.resolved_claude_config_dir) if settings.resolved_claude_config_dir else None,
        claude_global_config_file=str(settings.claude_global_config_file),
        setting_sources_effective=settings.setting_sources,
        model=settings.agent_model,
        provider_api_url_configured=bool(settings.provider_api_url),
        provider_api_key_configured=bool(settings.provider_api_key),
        model_provider_route=provider_router.health_summary(),
        claude_web_hitl_enabled=settings.enable_claude_web_hitl,
        feedback_debug_evidence=settings.enable_feedback_debug_evidence,
        agent_version_id=None,
        runtime_dependency_versions=runtime_dependency_versions(),
        langfuse_enabled=settings.langfuse_enabled,
        langfuse_base_url=settings.langfuse_base_url,
        langfuse_otel_endpoint_configured=bool(settings.langfuse_otel_endpoint),
        langfuse_public_key_configured=bool(settings.langfuse_public_key),
        langfuse_secret_key_configured=bool(settings.langfuse_secret_key),
        langfuse_otel_signals=settings.langfuse_otel_signals,
        docs={
            "swagger": app.docs_url,
            "redoc": app.redoc_url,
            "openapi": app.openapi_url,
        },
    )


def package_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


_RUNTIME_DEPENDENCY_VERSIONS_LOCK = Lock()
_RUNTIME_DEPENDENCY_VERSIONS = RuntimeDependencyVersions(
    claude_agent_sdk=package_version("claude-agent-sdk"),
    langfuse=package_version("langfuse"),
    litellm=package_version("litellm"),
    httpx=package_version("httpx"),
    starlette=package_version("starlette"),
    opentelemetry_sdk=package_version("opentelemetry-sdk"),
    opentelemetry_exporter_otlp_proto_http=package_version("opentelemetry-exporter-otlp-proto-http"),
)


def runtime_dependency_versions() -> RuntimeDependencyVersions:
    with _RUNTIME_DEPENDENCY_VERSIONS_LOCK:
        return _RUNTIME_DEPENDENCY_VERSIONS.model_copy()


def refresh_runtime_dependency_versions() -> RuntimeDependencyVersions:
    global _RUNTIME_DEPENDENCY_VERSIONS

    discovered = RuntimeDependencyVersions(
        claude_agent_sdk=package_version("claude-agent-sdk"),
        bundled_claude_code_cli=bundled_claude_code_cli_version(),
        path_claude_code_cli=command_version(shutil.which("claude")),
        langfuse=package_version("langfuse"),
        litellm=package_version("litellm"),
        httpx=package_version("httpx"),
        starlette=package_version("starlette"),
        opentelemetry_sdk=package_version("opentelemetry-sdk"),
        opentelemetry_exporter_otlp_proto_http=package_version("opentelemetry-exporter-otlp-proto-http"),
    )
    with _RUNTIME_DEPENDENCY_VERSIONS_LOCK:
        _RUNTIME_DEPENDENCY_VERSIONS = discovered
    return discovered.model_copy()


def bundled_claude_code_cli_version() -> str | None:
    spec = find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        return None
    bundled = Path(spec.origin).resolve().parent / "_bundled" / "claude"
    if not bundled.exists():
        return None
    return command_version(str(bundled))


def command_version(command: str | None) -> str | None:
    if not command:
        return None
    try:
        output = subprocess.check_output(
            [command, "--version"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return output.strip() or None
