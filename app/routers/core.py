from __future__ import annotations

import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path

from fastapi import APIRouter, FastAPI

from app.runtime.agent_git_store import AgentVersionProvider
from app.runtime.schemas import RuntimeDependencyVersions, RuntimeHealthResponse, RuntimeRootResponse
from app.runtime.settings import AppSettings


def create_core_router(
    *,
    settings: AppSettings,
    app: FastAPI,
    agent_version_store: AgentVersionProvider,
) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def root() -> RuntimeRootResponse:
        return RuntimeRootResponse(
            name="Claude Agent Runtime API",
            health="/health",
            docs=app.docs_url,
            redoc=app.redoc_url,
            openapi=app.openapi_url,
        )

    @router.get(
        "/health",
        tags=["health"],
        response_model=RuntimeHealthResponse,
        summary="Check service health and discover API documentation URLs",
    )
    async def health() -> RuntimeHealthResponse:
        return build_health_payload(settings=settings, app=app, agent_version_store=agent_version_store)

    return router


def build_health_payload(
    *,
    settings: AppSettings,
    app: FastAPI,
    agent_version_store: AgentVersionProvider,
) -> RuntimeHealthResponse:
    repository_status = (
        agent_version_store.repository_status()
        if hasattr(agent_version_store, "repository_status")
        else {}
    )
    return RuntimeHealthResponse(
        status="ok",
        api_host=settings.api_host,
        api_port=settings.api_port,
        host_port=settings.host_port,
        workspace_dir=str(settings.workspace_dir),
        data_dir=str(settings.data_dir),
        runtime_db_backend="sqlite",
        runtime_db_path=str(settings.runtime_db_path),
        legacy_file_store_enabled=False,
        claude_root=str(settings.claude_root),
        claude_home=str(settings.claude_home),
        claude_config_mode=settings.claude_config_mode,
        claude_config_dir=str(settings.resolved_claude_config_dir) if settings.resolved_claude_config_dir else None,
        claude_global_config_file=str(settings.claude_global_config_file),
        setting_sources_effective=settings.setting_sources,
        model=settings.agent_model,
        default_agent=settings.default_agent,
        default_skills_mode=settings.default_skills_mode,
        provider_api_url_configured=bool(settings.provider_api_url),
        provider_api_key_configured=bool(settings.provider_api_key),
        programmatic_agents=False,
        feedback_debug_evidence=settings.enable_feedback_debug_evidence,
        agent_version_id=agent_version_store.current_version_id(),
        runtime_dependency_versions=runtime_dependency_versions(),
        agent_repository_status=repository_status,
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


def runtime_dependency_versions() -> RuntimeDependencyVersions:
    return RuntimeDependencyVersions(
        claude_agent_sdk=package_version("claude-agent-sdk"),
        bundled_claude_code_cli=bundled_claude_code_cli_version(),
        path_claude_code_cli=command_version(shutil.which("claude")),
        langfuse=package_version("langfuse"),
        opentelemetry_sdk=package_version("opentelemetry-sdk"),
        opentelemetry_exporter_otlp_proto_http=package_version("opentelemetry-exporter-otlp-proto-http"),
    )


def package_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


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
