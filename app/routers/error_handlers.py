from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.runtime.errors import FeedbackStoreError
from app.runtime_gateway.client import RuntimeUpstreamError
from app.runtime_gateway.store import RuntimeStoreError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RuntimeStoreError)
    async def runtime_store_error_handler(_: Request, exc: RuntimeStoreError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), "error_code": exc.__class__.__name__.upper()},
        )

    @app.exception_handler(RuntimeUpstreamError)
    async def runtime_upstream_error_handler(_: Request, exc: RuntimeUpstreamError) -> JSONResponse:
        status_code = exc.status_code if 400 <= exc.status_code < 600 else 502
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": "AgentScope Runtime request failed",
                "error_code": "RUNTIME_UPSTREAM_ERROR",
            },
        )

    @app.exception_handler(FeedbackStoreError)
    async def feedback_store_error_handler(_: Request, exc: FeedbackStoreError) -> JSONResponse:
        content = {
            "detail": str(exc),
            "error_code": exc.error_code,
        }
        if isinstance(exc.error_details, dict):
            content.update(exc.error_details)
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        error_code = "UNAUTHORIZED" if exc.status_code == 401 else "HTTP_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail,
                "error_code": error_code,
            },
            headers=getattr(exc, "headers", None),
        )
