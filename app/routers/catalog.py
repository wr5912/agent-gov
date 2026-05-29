from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.schemas import AgentInfo, SkillInfo
from app.runtime.settings import AppSettings


def create_catalog_router(*, settings: AppSettings, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["catalog"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agents",
        response_model=list[AgentInfo],
        summary="List configured Claude subagents",
    )
    async def list_agents() -> list[AgentInfo]:
        return [
            AgentInfo(
                name=item["name"],
                path=item["path"],
                description=item.get("description"),
                model=item.get("model"),
                tools=item.get("tools") or [],
                skills=item.get("skills") or [],
            )
            for item in discover_agents(settings.workspace_dir, settings.claude_home)
        ]

    @router.get(
        "/skills",
        response_model=list[SkillInfo],
        summary="List configured Claude skills",
    )
    async def list_skills() -> list[SkillInfo]:
        return [
            SkillInfo(
                name=item["name"],
                path=item["path"],
                description=item.get("description"),
            )
            for item in discover_skills(settings.workspace_dir, settings.claude_home)
        ]

    return router
