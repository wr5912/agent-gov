from __future__ import annotations

from fastapi import HTTPException, status

from app.runtime.agent_profiles import AgentRuntimeProfile
from app.runtime.settings import AppSettings


def require_non_stream_hitl_free(profile: AgentRuntimeProfile, *, surface: str) -> None:
    if profile.requires_web_hitl:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{surface} cannot run an agent that requires Web HITL; use an eligible streaming control surface",
        )


def require_stream_hitl_available(
    profile: AgentRuntimeProfile,
    settings: AppSettings,
    *,
    surface: str,
) -> None:
    if profile.requires_web_hitl and not settings.enable_claude_web_hitl:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{surface} requires ENABLE_CLAUDE_WEB_HITL=true for the selected agent",
        )
