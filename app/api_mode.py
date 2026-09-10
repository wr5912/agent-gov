"""全局 API mutation gate，用于 AgentScope fresh-epoch 原子切换。"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Literal

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

ApiMode = Literal["open", "drain", "acceptance"]
_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_RUNTIME_RECEIPT_PATH: Final = "/internal/runtime-receipts"
ACCEPTANCE_IDENTITY_HEADER: Final = "X-AgentGov-Acceptance-Identity"


def _constant_time_equal(actual: str | None, expected: str | None) -> bool:
    if not actual or not expected:
        return False
    return hmac.compare_digest(actual.encode(), expected.encode())


def _bearer_token(request: Request) -> str | None:
    scheme, separator, credentials = request.headers.get("Authorization", "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not credentials:
        return None
    return credentials


class ApiModeGateMiddleware(BaseHTTPMiddleware):
    """Fail closed for mutations while a fresh-epoch cutover is being validated."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        mode: ApiMode,
        acceptance_identity: str | None,
        acceptance_api_key: str | None,
        state_file: Path | None = None,
    ) -> None:
        super().__init__(app)
        if mode not in {"open", "drain", "acceptance"}:
            raise ValueError(f"Unsupported AGENTGOV_API_MODE={mode!r}")
        if mode == "acceptance" and (not acceptance_identity or not acceptance_api_key):
            raise ValueError("acceptance mode requires one-time identity and API key")
        self._mode = mode
        self._acceptance_identity = acceptance_identity
        self._acceptance_api_key = acceptance_api_key
        self._state_file = state_file

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        mode = self._effective_mode()
        if mode == "open" or request.method in _READ_METHODS:
            return await call_next(request)

        # The route itself verifies the body-bound HMAC. The global gate only avoids
        # blocking the sole Runtime callback required to drain terminal receipts.
        if mode in {"drain", "acceptance"} and request.method == "POST" and request.url.path == _RUNTIME_RECEIPT_PATH:
            return await call_next(request)

        if mode == "acceptance" and self._has_acceptance_identity(request):
            return await call_next(request)

        return JSONResponse(
            status_code=503,
            content={"detail": f"API mutation gate is {mode or 'invalid'}"},
            headers={"Cache-Control": "no-store", "Retry-After": "5"},
        )

    def _has_acceptance_identity(self, request: Request) -> bool:
        return _constant_time_equal(
            request.headers.get(ACCEPTANCE_IDENTITY_HEADER),
            self._acceptance_identity,
        ) and _constant_time_equal(_bearer_token(request), self._acceptance_api_key)

    def _effective_mode(self) -> ApiMode | None:
        if self._state_file is None:
            return self._mode
        try:
            if self._state_file.is_symlink() or not self._state_file.is_file() or self._state_file.stat().st_size > 4096:
                return None
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return None
        state = payload.get("state")
        if state not in {"open", "drain", "acceptance"}:
            return None
        cutover_id = payload.get("cutover_id")
        if cutover_id is None:
            return self._mode
        if not isinstance(cutover_id, str) or not _constant_time_equal(cutover_id, self._acceptance_identity):
            return None
        irreversible_at = payload.get("irreversible_at")
        if state == "open" and (not isinstance(irreversible_at, str) or not irreversible_at.strip()):
            return None
        return state
