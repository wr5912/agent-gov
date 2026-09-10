from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import httpx
from fastapi import APIRouter, FastAPI, Response, status

from app.runtime.schemas import (
    RuntimeDependencyVersions,
    RuntimeHealthResponse,
    RuntimeLivenessResponse,
    RuntimeReadinessResponse,
    RuntimeRootResponse,
    RuntimeServiceReadiness,
)
from app.runtime.settings import AppSettings
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeUpstreamError
from app.version import APP_VERSION


def create_core_router(*, settings: AppSettings, app: FastAPI, runtime_client: AgentScopeRuntimeClient) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def root() -> RuntimeRootResponse:
        return RuntimeRootResponse(
            name="AgentGov API",
            health="/health",
            liveness="/health/live",
            readiness="/health/ready",
            docs="/docs",
            redoc="/redoc",
            openapi=app.openapi_url,
        )

    @router.get("/health/live", tags=["health"], response_model=RuntimeLivenessResponse)
    async def liveness() -> RuntimeLivenessResponse:
        return RuntimeLivenessResponse(runtime_version=APP_VERSION)

    @router.get("/health/ready", tags=["health"], response_model=RuntimeReadinessResponse)
    async def readiness(response: Response) -> RuntimeReadinessResponse:
        runtime_ready = await _runtime_readiness(runtime_client)
        response.status_code = status.HTTP_200_OK if runtime_ready.status == "ready" else status.HTTP_503_SERVICE_UNAVAILABLE
        return RuntimeReadinessResponse(
            status=runtime_ready.status,
            runtime_version=APP_VERSION,
            runtime_service=runtime_ready,
        )

    @router.get("/health", tags=["health"], response_model=RuntimeHealthResponse)
    async def health() -> RuntimeHealthResponse:
        return await build_health_payload(settings=settings, app=app, runtime_client=runtime_client)

    return router


async def _runtime_readiness(client: AgentScopeRuntimeClient) -> RuntimeServiceReadiness:
    try:
        response = await client.request_json("GET", "/health", timeout=5.0)
    except (RuntimeUpstreamError, httpx.RequestError, OSError, TimeoutError) as exc:
        return RuntimeServiceReadiness(status="not_ready", reason=str(exc), retryable=True)
    body = response.body if isinstance(response.body, dict) else {}
    ready = response.status_code == 200 and body.get("status") == "ok"
    return RuntimeServiceReadiness(
        status="ready" if ready else "not_ready",
        message="AgentScope Runtime ready" if ready else "AgentScope Runtime reported not_ready",
        route=settings_safe_url(client.base_url),
        status_code=response.status_code,
        retryable=not ready,
    )


def settings_safe_url(value: str) -> str:
    """Runtime URL 不应含凭据；仍去除 user-info 以防配置错误泄露。"""
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


async def build_health_payload(
    *,
    settings: AppSettings,
    app: FastAPI,
    runtime_client: AgentScopeRuntimeClient,
) -> RuntimeHealthResponse:
    runtime_ready = await _runtime_readiness(runtime_client)
    return RuntimeHealthResponse(
        status="ok" if runtime_ready.status == "ready" else "degraded",
        runtime_version=APP_VERSION,
        api_host=settings.api_host,
        api_port=settings.api_port,
        host_port=settings.host_port,
        workspace_dir=str(settings.workspace_dir),
        data_dir=str(settings.data_dir),
        runtime_db_backend="sqlite",
        runtime_db_path=str(settings.runtime_db_path),
        runtime_url=settings_safe_url(settings.agentscope_runtime_url),
        runtime_service=runtime_ready,
        model=settings.agentscope_model_name,
        feedback_debug_evidence=settings.enable_feedback_debug_evidence,
        runtime_dependency_versions=runtime_dependency_versions(),
        langfuse_enabled=settings.langfuse_enabled,
        langfuse_base_url=settings.langfuse_base_url,
        langfuse_public_key_configured=bool(settings.langfuse_public_key),
        langfuse_secret_key_configured=bool(settings.langfuse_secret_key),
        docs={"swagger": "/docs", "redoc": "/redoc", "openapi": app.openapi_url},
    )


def package_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def runtime_dependency_versions() -> RuntimeDependencyVersions:
    return RuntimeDependencyVersions(
        agentscope=package_version("agentscope"),
        langfuse=package_version("langfuse"),
        httpx=package_version("httpx"),
        starlette=package_version("starlette"),
        opentelemetry_sdk=package_version("opentelemetry-sdk"),
        opentelemetry_exporter_otlp_proto_http=package_version("opentelemetry-exporter-otlp-proto-http"),
    )
