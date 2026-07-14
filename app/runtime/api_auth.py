from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum

from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials


class ApiPrincipal(StrEnum):
    ANONYMOUS = "anonymous"
    GENERAL_API = "general_api"
    RESPONSE_ORCHESTRATOR = "response_orchestrator"


@dataclass(frozen=True)
class ApiAuthenticator:
    """Resolve the caller once, then let each route enforce its own scope."""

    api_key: str | None
    response_orchestrator_api_key: str | None

    def authenticate(self, credentials: HTTPAuthorizationCredentials | None) -> ApiPrincipal:
        if credentials is None:
            if self.api_key:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
            return ApiPrincipal.ANONYMOUS
        if credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        token = credentials.credentials
        if self.api_key and hmac.compare_digest(token, self.api_key):
            return ApiPrincipal.GENERAL_API
        if self.response_orchestrator_api_key and hmac.compare_digest(token, self.response_orchestrator_api_key):
            return ApiPrincipal.RESPONSE_ORCHESTRATOR
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    def require_general(self, credentials: HTTPAuthorizationCredentials | None) -> None:
        principal = self.authenticate(credentials)
        if principal == ApiPrincipal.RESPONSE_ORCHESTRATOR:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Response orchestrator credential is not authorized for this endpoint",
            )

    def require_response_orchestrator(self, principal: ApiPrincipal) -> None:
        if not self.response_orchestrator_api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Response orchestrator credential is not configured",
            )
        if principal == ApiPrincipal.ANONYMOUS:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Response orchestrator credential is required")
        if principal != ApiPrincipal.RESPONSE_ORCHESTRATOR:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Response orchestrator credential is required",
            )
