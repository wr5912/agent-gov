from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from app.runtime.config_mapping import build_config_mapping
from app.runtime.schemas import ConfigMappingResponse
from app.runtime.settings import AppSettings


def create_config_router(*, settings: AppSettings, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["config"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/config",
        response_model=ConfigMappingResponse,
        summary="Inspect Claude Code configuration mapping",
        description="Returns path, mount, scope, load, and git-policy metadata without exposing sensitive file contents.",
    )
    async def config_mapping() -> ConfigMappingResponse:
        return build_config_mapping(settings)

    return router
